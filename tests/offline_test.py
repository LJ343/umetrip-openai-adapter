"""
离线测试：起一个模拟上游（按真实抓到的事件顺序输出），再起适配服务指向它，用 OpenAI SDK 验证转换结果。
不需要访问真实上游。

用法（在项目根目录）：python tests/offline_test.py
依赖：fastapi uvicorn httpx openai
"""
import asyncio
import json
import os
import random
import re
import socket
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

FAKE_PORT, PROXY_PORT = 18100, 18101
SILENCE = 3.5  # 模拟上游调用工具时的静默时长（秒）

CARD = '<flightstatuslist_card>{"pageId":"1","list":[{"flightNo":"CA1886","note":"<b>"}]}</flightstatuslist_card>'
SEGMENTS = [
    "正在查询嘉兴到北京的航班信息，先确认一下机场编码。",
    "查到嘉兴南湖机场明天没有直飞北京的航班。",
    "查到了，明天有 3 班直飞：\n\n| 航班 | 起飞 |\n|---|---|\n| CA1886 | 12:00 |\n\n点击查看完整列表：\n\n" + CARD + "\n\n航程 a<b 也要保留",
]
TOOL_RESULT = {"content": "检索到机场编码", "result": {"departure": "JIA", "arrival": "PEK"}}
TOOL_TEXT = "\n\n" + json.dumps(TOOL_RESULT, ensure_ascii=False, indent=2) + "\n\n"


def fake_upstream_app():
    from fastapi import FastAPI, Request
    from fastapi.responses import StreamingResponse

    app = FastAPI()

    def ev(name, data):
        return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def chunk(content=None, role=None, finish=None):
        delta = {}
        if role:
            delta["role"] = role
        if content is not None:
            delta["content"] = content
        return "data: " + json.dumps({"id": "chatcmpl-up", "object": "chat.completion.chunk", "created": 1,
                                      "model": "qianrang", "usage": None,
                                      "choices": [{"index": 0, "delta": delta, "logprobs": None,
                                                   "finish_reason": finish}]}, ensure_ascii=False) + "\n\n"

    def think(t):
        return ev("thinking_delta", {"role": "航旅调度员", "agent_id": "supervisor",
                                     "event_type": "thinking_delta", "data": t})

    def end(name):
        return ev(name, {"role": "航旅调度员", "agent_id": "supervisor", "event_type": name, "data": None})

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        body = await request.json()
        if body['messages'][-1]['content'] in ('模拟断流', '模拟半途断流'):
            async def incomplete():
                if body['messages'][-1]['content'] == '模拟半途断流':
                    yield chunk('部分正文。', role='assistant')
                else:
                    yield ': connected\n\n'
                await asyncio.sleep(0.2)
                # 干净 EOF 仍缺少 finish/[DONE]，不能被适配为业务成功。
            return StreamingResponse(incomplete(), media_type='text/event-stream')
        if body['messages'][-1]['content'] == '模拟慢首包':
            await asyncio.sleep(1.4)  # 连 HTTP 响应头都还没有，中转仍应保活。
            async def slow_headers():
                yield chunk('上游原始答案', role='assistant')
                yield chunk(finish='stop')
                yield 'data: [DONE]\n\n'
            return StreamingResponse(slow_headers(), media_type='text/event-stream')

        async def gen():
            yield ev("prepare_model_input", {"type": "prepare_model_input", "duration_ms": 30})
            yield ev("ttft", {"type": "ttft", "duration_ms": 2000, "round": 1})
            yield think("用户想查")
            yield think("嘉兴到北京")
            yield end("thinking_end")
            yield chunk(SEGMENTS[0], role="assistant")
            yield end("text_end")
            yield ev("tool_call_start", {"event_type": "tool_call_start", "data": {"name": "关键信息梳理"}})
            yield ev("tool_result_end", {"event_type": "tool_result_end", "data": {"name": "关键信息梳理", **TOOL_RESULT}})
            await asyncio.sleep(SILENCE)
            yield think("再确认周边机场")
            yield end("thinking_end")
            yield chunk(SEGMENTS[1])
            yield end("text_end")
            # 第三段故意把卡片标签拆在多个 chunk 中间
            s = SEGMENTS[2]
            cut = [0, s.index("<flight") + 5, s.index("CA1886", s.index("<flight")), s.index("</flight") + 9,
                   s.index("_card>", s.index("</flight")) + 3, len(s)]
            for a, b in zip(cut, cut[1:]):
                yield chunk(s[a:b])
            yield end("text_end")
            yield chunk(finish="stop")
            yield "data: [DONE]\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


def wait_port(port, timeout=15):
    t = time.time()
    while time.time() - t < timeout:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise RuntimeError(f"port {port} not ready")


def test_card_stripper():
    from app.main import CardStripper
    expected = re.sub(r"<([A-Za-z0-9_]+_card)>.*?</\1>", "", SEGMENTS[2], flags=re.S)
    extra = ["<", "<flight", "a<b", "<flightstatuslist_card>未闭合"]
    rnd = random.Random(42)
    for _ in range(2000):
        s = SEGMENTS[2]
        cuts = sorted(rnd.sample(range(1, len(s)), rnd.randint(1, 20)))
        parts = [s[a:b] for a, b in zip([0] + cuts, cuts + [len(s)])]
        st = CardStripper(r"[A-Za-z0-9_]+_card")
        out = "".join(st.feed(p) for p in parts) + st.flush()
        assert out == expected, (parts, out)
    for t, want in zip(extra, ["<", "<flight", "a<b", ""]):
        st = CardStripper(r"[A-Za-z0-9_]+_card")
        assert st.feed(t) + st.flush() == want, t
    print("[PASS] CardStripper 随机切分 2000 次 + 边界情况")


def main():
    import uvicorn
    from openai import OpenAI

    test_card_stripper()

    import threading
    fake_server = uvicorn.Server(uvicorn.Config(fake_upstream_app(), host="127.0.0.1", port=FAKE_PORT,
                                                log_level="warning", loop="asyncio", http="h11"))
    fake_thread = threading.Thread(target=fake_server.run, daemon=True)
    fake_thread.start()
    wait_port(FAKE_PORT)
    env = dict(os.environ, UPSTREAM_URL=f"http://127.0.0.1:{FAKE_PORT}/v1/chat/completions",
               PROXY_API_KEYS="sk-test", UPSTREAM_API_KEY="sk-up", HEARTBEAT_INTERVAL="0.2", LOG_LEVEL="WARNING",
               STREAM_WAIT_AFTER="0.6", STREAM_WAIT_MESSAGE="正在处理，请稍等。\\n\\n",
               JSON_KEEPALIVE_AFTER="0.15", JSON_KEEPALIVE_INTERVAL="0.15",
               TEXT_END_SEPARATOR="", NONSTREAM_FINAL_SEGMENT_ONLY="false", VOICE_ERROR_FALLBACK_ENABLED="true")
    proxy = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
                              "--port", str(PROXY_PORT)], cwd=ROOT, env=env)
    try:
        wait_port(PROXY_PORT)
        client = OpenAI(base_url=f"http://127.0.0.1:{PROXY_PORT}/v1", api_key="sk-test", max_retries=0,
                        _strict_response_validation=True)
        clean_last = re.sub(r"<([A-Za-z0-9_]+_card)>.*?</\1>", "", SEGMENTS[2], flags=re.S)

        # 上游 1.4 秒才给响应头，客户端读超时仅 0.5 秒：必须在连接等待阶段也发送心跳。
        import httpx
        impatient = client.with_options(timeout=httpx.Timeout(connect=2, read=0.5, write=2, pool=2))
        started = time.monotonic()
        slow = impatient.chat.completions.create(model='qianrang', stream=True,
            messages=[{'role': 'user', 'content': '模拟慢首包'}], stream_options={'include_usage': True})
        parts, first_at, notices, stable = [], None, 0, set()
        for part in slow:
            if first_at is None:
                first_at = time.monotonic() - started
            stable.add((part.id, part.created))
            if part.choices:
                value = part.choices[0].delta.content or ''
                parts.append(value)
                notices += int(value == '正在处理，请稍等。\n\n')
        duration = time.monotonic() - started
        assert first_at < 0.5, first_at
        assert duration >= 1.4, duration
        assert notices == 1 and len(stable) == 1
        assert ''.join(parts) == '正在处理，请稍等。\n\n上游原始答案'
        print(f'[PASS] 慢响应头：首事件 {first_at:.3f}s，完整答案 {duration:.2f}s，读超时 0.5s 仍完成；等待提示仅一次、标准 SDK 严格解析通过')
        ordinary = client.chat.completions.create(model='qianrang', messages=[{'role': 'user', 'content': '模拟慢首包'}])
        assert ordinary.choices[0].message.content == '上游原始答案'
        print('[PASS] 非流式慢首包仍为单份完整 JSON，不添加等待提示')
        padded_client = impatient.with_options(base_url=f'http://127.0.0.1:{PROXY_PORT}/v1/keepalive')
        began = time.monotonic()
        padded = padded_client.chat.completions.create(model='qianrang', stream=False,
            messages=[{'role': 'user', 'content': '模拟慢首包'}])
        assert padded.choices[0].message.content == '上游原始答案'
        assert time.monotonic() - began >= 1.4
        print('[PASS] 可选 JSON 保活：读取超时 0.5s 的非流式 SDK 请求，等待上游 1.4s 后仍严格解析为单份 chat.completion')

        # 相同真实 HTTP/标准 SDK：语音入口结束失败说明，原入口保留 error 语义。
        for query, expected in [('模拟断流', '本次未能取得完整结果'), ('模拟半途断流', '以上回答尚未完整返回')]:
            received, finish = [], []
            for part in padded_client.chat.completions.create(model='qianrang', stream=True,
                    messages=[{'role':'user','content':query}], stream_options={'include_usage':True}):
                for choice in part.choices:
                    received.append(choice.delta.content or '')
                    if choice.finish_reason:
                        finish.append(choice.finish_reason)
            assert expected in ''.join(received)
            assert finish == ['stop'], finish
            if query == '模拟半途断流':
                assert ''.join(received).startswith('部分正文。')
        from openai import APIError
        try:
            for _ in client.chat.completions.create(model='qianrang', stream=True,
                    messages=[{'role':'user','content':'模拟断流'}]):
                pass
        except APIError:
            pass
        else:
            raise AssertionError('原入口必须保留错误，不能伪装成成功回答')
        print('[PASS] 真实 HTTP/严格 SDK：语音断流完整播报失败并结束；半途断流保留原文并标注未完成；原入口仍抛出 APIError')

        # 流式
        content, reasoning, heartbeats, ids, gaps, last = [], [], 0, set(), [], time.time()
        for ch in client.chat.completions.create(model="qianrang", stream=True,
                                                 messages=[{"role": "user", "content": "查航班"}]):
            now = time.time(); gaps.append(now - last); last = now
            ids.add(ch.id)
            d = ch.choices[0].delta
            if d.content == "" and not getattr(d, "reasoning_content", None):
                heartbeats += 1
            content.append(d.content or "")
            r = (d.model_extra or {}).get("reasoning_content")
            if r:
                reasoning.append(r)
        text = "".join(content)
        expected = SEGMENTS[0] + TOOL_TEXT + SEGMENTS[1] + clean_last
        assert text == expected, repr(text)
        assert "_card" not in text and "a<b" in text
        assert "".join(reasoning) == "用户想查嘉兴到北京再确认周边机场", repr("".join(reasoning))
        assert heartbeats >= 2, heartbeats
        assert max(gaps) < 1.6, f"最长输出间隔 {max(gaps):.1f}s"
        assert len(ids) == 1, ids
        print(f"[PASS] 流式：原样拼接全部正文、保留工具结果、删卡片、思考保留、静默 {SILENCE}s 期间心跳 {heartbeats} 次"
              f"（最长间隔 {max(gaps):.1f}s）、id 统一")

        # 非流式
        r = client.chat.completions.create(model="qianrang", messages=[{"role": "user", "content": "查航班"}])
        msg = r.choices[0].message
        assert msg.content == expected, repr(msg.content)
        assert (msg.model_extra or {}).get("reasoning_content") == "用户想查嘉兴到北京再确认周边机场"
        assert r.usage is None
        print("[PASS] 非流式：与流式内容一致、保留全部段落及工具结果、不编造 token 用量")
        print("\n全部通过")
        impatient.close()
        client.close()
    finally:
        proxy.terminate()
        proxy.wait(timeout=10)
        fake_server.should_exit = True
        fake_thread.join(timeout=10)
        assert not fake_thread.is_alive(), "模拟上游未正常退出"


if __name__ == "__main__":
    main()
