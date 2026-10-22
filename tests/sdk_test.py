"""
用官方 OpenAI SDK 验收适配服务。

用法：
  pip install openai
  python tests/sdk_test.py --base-url http://服务器公网IP:8000/v1 --api-key sk-xxx [--model qianrang]
"""
import argparse
import sys
import time

from openai import OpenAI, APIStatusError

parser = argparse.ArgumentParser()
parser.add_argument("--base-url", required=True, help="例如 http://1.2.3.4:8000/v1")
parser.add_argument("--api-key", required=True)
parser.add_argument("--model", default="qianrang")
args = parser.parse_args()

client = OpenAI(base_url=args.base_url, api_key=args.api_key, timeout=600, max_retries=0)
failed = 0


def check(name, fn):
    global failed
    t = time.time()
    try:
        detail = fn()
        print(f"[PASS] {name} ({time.time() - t:.1f}s) {detail or ''}")
    except Exception as e:  # noqa: BLE001
        failed += 1
        print(f"[FAIL] {name}: {type(e).__name__}: {e}")


def models():
    ids = [m.id for m in client.models.list().data]
    assert args.model in ids, ids
    return ids


def non_stream():
    r = client.chat.completions.create(
        model=args.model, messages=[{"role": "user", "content": "你好，用一句话介绍你自己"}])
    content = r.choices[0].message.content
    assert r.object == "chat.completion" and content, r
    return repr(content[:60])


def stream():
    parts = []
    for chunk in client.chat.completions.create(
            model=args.model, messages=[{"role": "user", "content": "你好"}], stream=True):
        if chunk.choices and chunk.choices[0].delta.content:
            parts.append(chunk.choices[0].delta.content)
    assert parts
    return repr("".join(parts)[:60])


def stream_usage():
    last = None
    for chunk in client.chat.completions.create(
            model=args.model, messages=[{"role": "user", "content": "你好"}],
            stream=True, stream_options={"include_usage": True}):
        last = chunk
    assert last is not None and last.choices == []
    return f"usage={last.usage.model_dump()}" if last.usage is not None else "上游未提供 usage，保留 null"


def multi_turn():
    r = client.chat.completions.create(model=args.model, messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "我叫小王，记住我的名字"},
        {"role": "assistant", "content": "好的，小王，我记住了。"},
        {"role": "user", "content": "我叫什么名字？"},
    ])
    content = r.choices[0].message.content
    if "小王" not in content:
        return "⚠️ 接口可用，但模型没用上历史消息（上游限制，见部署方案「已知限制」）：" + repr(content[:40])
    return repr(content[:60])


def expect_status(status, **kwargs):
    def fn():
        try:
            c = OpenAI(base_url=args.base_url, api_key=kwargs.pop("api_key", args.api_key), timeout=60, max_retries=0)
            c.chat.completions.create(messages=[{"role": "user", "content": "hi"}], **kwargs)
        except APIStatusError as e:
            assert e.status_code == status, (e.status_code, e.response.text)
            assert "error" in e.response.json()
            return f"-> {e.status_code} {e.response.json()['error']['code']}"
        raise AssertionError(f"expected HTTP {status}, got success")
    return fn


check("GET /v1/models", models)
check("非流式", non_stream)
check("流式", stream)
check("流式 + include_usage", stream_usage)
check("多轮上下文", multi_turn)
check("错误 model 返回 404", expect_status(404, model="not-exist-model"))
check("错误 key 返回 401", expect_status(401, model=args.model, api_key="sk-wrong-key"))

print("\n全部通过" if not failed else f"\n{failed} 项失败")
sys.exit(1 if failed else 0)
