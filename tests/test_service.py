import asyncio
import json
import os
import re
import subprocess
import shutil
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from app.agent import Agent, AgentFailure, Model
from app.config import ROOT, Settings
from app.grounding import request_context
from app.main import create_app
from app.sessions import SessionConflict, SessionStore
from app.tools import FlyAI, REGISTRY, TOOLS, ToolFailure, validate
from app.tracing import Trace

NODE = os.getenv("NODE_BIN") or shutil.which("node") or "/usr/bin/node"
TOMORROW = (date.today() + timedelta(days=2)).isoformat()


def settings(directory):
    return Settings("https://llm.example/v1", "test-model", "model-secret", "flyai-secret", ("proxy-a", "proxy-b"), "flyai-travel", NODE, ROOT / "runtime/flyai-bundle.cjs", Path(directory), extra_body={})


class FakeTools:
    calls = []

    def __init__(self, s):
        pass

    async def call(self, name, arguments):
        validate(name, arguments)
        self.calls.append((name, arguments))
        return {"status": 0, "data": {"itemList": [{"name": "测试酒店", "price": "¥618", "jumpUrl": "https://example.com/book"}]}, "systemMessage": "测试提示"}


class FakeModel:
    histories = []

    def __init__(self, s, client):
        pass

    async def turn(self, messages, stream, tokens, generation=None):
        self.histories.append(messages.copy())
        if messages[-1]["role"] != "tool":
            yield "message", {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "search_hotel", "arguments": '{"dest_name":"杭州"}'}}]}
        else:
            text = "测试酒店，618元。[预订](https://example.com/book)\n测试提示"
            for piece in (text[:8], text[8:]):
                yield "text", piece
            yield "message", {"role": "assistant", "content": text}
        yield "usage", {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}


class LimitedModel(FakeModel):
    async def turn(self, messages, stream, tokens, generation=None):
        yield "text", "Hello"
        yield "finish_reason", "length"
        yield "message", {"role": "assistant", "content": "Hello"}
        yield "usage", {"prompt_tokens": 20, "completion_tokens": 1, "total_tokens": 21}


class RequestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = settings(self.tmp.name)
        self.app = create_app(self.s, FakeModel, FakeTools)
        self.client = TestClient(self.app).__enter__()
        FakeModel.histories.clear()
        FakeTools.calls.clear()

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.tmp.cleanup()

    def request(self, messages=None, session=None, key="proxy-a", **extra):
        headers = {"Authorization": "Bearer " + key}
        if session:
            headers["session_id"] = session
        return self.client.post("/v1/chat/completions", headers=headers, json={"model": "flyai-travel", "messages": messages or [{"role": "user", "content": "明天入住、后天离店，杭州酒店"}], **extra})

    def test_auth_and_models(self):
        self.assertEqual(self.request(key="wrong").status_code, 401)
        self.assertEqual(self.client.get("/v1/models", headers={"Authorization": "Bearer proxy-a"}).json()["data"][0]["id"], "flyai-travel")
        self.assertEqual(self.client.get("/healthz").status_code, 200)

    def test_nonstream_tool_loop(self):
        response = self.request()
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("618", data["choices"][0]["message"]["content"])
        self.assertEqual(len(FakeTools.calls), 1)
        self.assertEqual(data["usage"]["total_tokens"], 30)
        self.assertEqual(data["choices"][0]["finish_reason"], "stop")

    def test_stream_no_internal_tool_leak_and_correct_end(self):
        response = self.request(stream=True, stream_options={"include_usage": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.text.endswith("data: [DONE]\n\n"))
        chunks = [json.loads(x[6:]) for x in response.text.splitlines() if x.startswith("data: {")]
        self.assertEqual(len({c["id"] for c in chunks}), 1)
        self.assertEqual(chunks[-1]["usage"]["total_tokens"], 30)
        self.assertNotIn("tool_calls", response.text)
        self.assertIn("618", response.text)

    def test_session_keeps_internal_tools_across_rounds(self):
        first = self.request(session="one").json()["choices"][0]["message"]
        self.assertEqual(self.request([{"role": "user", "content": "更便宜一点"}], session="one").status_code, 200)
        history = FakeModel.histories[-1]
        self.assertTrue(any(m["role"] == "tool" for m in history[:-1]))
        self.assertEqual(history[-1]["content"], "更便宜一点")
        self.assertTrue(any(m.get("content") == first["content"] for m in history))

    def test_same_session_different_key_isolated(self):
        self.request(session="shared")
        self.request([{"role": "user", "content": "新用户"}], session="shared", key="proxy-b")
        self.assertFalse(any(m["role"] == "tool" for m in FakeModel.histories[-1]))

    def test_full_history_session_merge(self):
        user = {"role": "user", "content": "明天入住、后天离店，杭州酒店"}
        assistant = self.request([user], session="merge").json()["choices"][0]["message"]
        self.assertEqual(self.request([user, assistant, {"role": "user", "content": "换双床"}], session="merge").status_code, 200)

    def test_history_conflict_and_stateless_no_guessing(self):
        self.request(session="one")
        conflict = self.request([{"role": "user", "content": "不同首句"}, {"role": "assistant", "content": "不同回答"}, {"role": "user", "content": "继续"}], session="one")
        self.assertEqual(conflict.status_code, 409)
        self.request([{"role": "user", "content": "无session新请求"}])
        self.assertFalse(any(m["role"] == "tool" for m in FakeModel.histories[-1]))

    def test_busy_does_not_release_other_session(self):
        key = self.app.state.sessions.identity("proxy-a", "busy")
        self.app.state.sessions.claim(key)
        self.assertEqual(self.request(session="busy").status_code, 409)
        self.assertIn(key, self.app.state.sessions.active)
        self.assertEqual(self.app.state.slots.qsize(), self.s.concurrency)

    def test_capacity_rejects_without_upstream(self):
        for _ in range(self.s.concurrency):
            self.app.state.slots.get_nowait()
        self.assertEqual(self.request().status_code, 429)
        self.assertEqual(FakeModel.histories, [])

    def test_validation(self):
        for extra in [{"tools": []}, {"model": "wrong"}, {"stream": 1}, {"max_tokens": True}, {"temperature": 3}, {"stream_options": {"x": True}}]:
            self.assertEqual(self.request(**extra).status_code, 400, extra)
        self.assertEqual(self.request(messages=[{"role": "user", "content": [{"type": "text", "text": "hi"}]}]).status_code, 400)
        self.s.max_request = 10
        self.assertEqual(self.request().status_code, 413)

    def test_missing_config_no_calls(self):
        self.s.llm_key = ""
        self.assertEqual(self.request().status_code, 503)
        self.assertEqual(FakeModel.histories, [])

    def test_keepalive_nonstream_fast(self):
        response = self.client.post("/v1/keepalive/chat/completions", headers={"Authorization": "Bearer proxy-a"}, json={"model": "flyai-travel", "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(response.json()["object"], "chat.completion")


class ModelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = settings(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    async def run_model(self, wire, status=200, stream=True):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(status, text=wire, headers={"content-type": "text/event-stream" if stream else "application/json"}))) as client:
            return [e async for e in Model(self.s, client).turn([{"role": "user", "content": "hi"}], stream, 256)]

    @staticmethod
    def event(delta, finish=None):
        return "data: " + json.dumps({"choices": [{"delta": delta, "finish_reason": finish}]}) + "\n\n"

    async def test_stream_tool_argument_fragments(self):
        wire = self.event({"tool_calls": [{"index": 0, "id": "x", "type": "function", "function": {"name": "search_hotel", "arguments": '{"dest_'}}]})
        wire += self.event({"tool_calls": [{"index": 0, "function": {"arguments": 'name":"杭州"}'}}]}, "tool_calls") + "data: [DONE]\n\n"
        result = await self.run_model(wire)
        call = next(v for k, v in result if k == "message")["tool_calls"][0]
        self.assertEqual(json.loads(call["function"]["arguments"]), {"dest_name": "杭州"})

    async def test_stream_requires_done_and_valid_finish(self):
        for wire in [self.event({"content": "partial"}, "stop"), self.event({"content": "partial"}) + "data: [DONE]\n\n", 'data: {"error":{"message":"secret"}}\n\n']:
            with self.assertRaises(AgentFailure):
                await self.run_model(wire)

    async def test_stream_usage_and_comment(self):
        wire = ": heartbeat\n\n" + self.event({"content": "你好"}, "stop")
        wire += 'data: {"choices":[],"usage":{"total_tokens":12}}\n\ndata: [DONE]\n\n'
        self.assertIn(("usage", {"total_tokens": 12}), await self.run_model(wire))

    async def test_text_length_and_content_filter_preserved(self):
        for finish in ["length", "content_filter"]:
            for text in ["Hello", ""]:
                for stream in [False, True]:
                    wire = (self.event({"content": text}, finish) + "data: [DONE]\n\n") if stream else json.dumps({"choices": [{"message": {"content": text}, "finish_reason": finish}]})
                    result = await self.run_model(wire, stream=stream)
                    self.assertIn(("finish_reason", finish), result)
                    self.assertEqual(next(v for k, v in result if k == "message")["content"] or "", text)

    async def test_length_tool_calls_are_never_executed(self):
        for arguments in ['{"dest_name":"杭州"}', '{"dest_name":']:
            call = {"index": 0, "id": "c1", "type": "function", "function": {"name": "search_hotel", "arguments": arguments}}
            for stream in [False, True]:
                wire = (self.event({"tool_calls": [call]}, "length") + "data: [DONE]\n\n") if stream else json.dumps({"choices": [{"message": {"content": None, "tool_calls": [call]}, "finish_reason": "length"}]})
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, text=wire, headers={"content-type": "text/event-stream" if stream else "application/json"}))) as client:
                    FakeTools.calls.clear()
                    with self.assertRaises(AgentFailure):
                        _ = [e async for e in Agent(self.s, Model(self.s, client), FakeTools(self.s)).run([{"role": "user", "content": "hi"}], stream, 1)]
                    self.assertEqual(FakeTools.calls, [])

    async def test_error_body_does_not_leak(self):
        with self.assertRaises(AgentFailure) as caught:
            await self.run_model("model-secret flyai-secret", status=401)
        self.assertNotIn("secret", str(caught.exception))

    async def test_agent_tool_error_is_not_success(self):
        class BrokenTools:
            async def call(self, *args):
                raise ToolFailure("查询超时")
        class InspectModel(FakeModel):
            async def turn(self, messages, *args):
                if messages[-1]["role"] == "tool":
                    self.payload = json.loads(messages[-1]["content"])
                async for event in super().turn(messages, *args):
                    yield event
        model = InspectModel(self.s, None)
        result = [e async for e in Agent(self.s, model, BrokenTools()).run([{"role": "user", "content": "test"}], False, 256)]
        answer = next(v for k, v in result if k == "result")
        self.assertIn("查询未完成", answer["text"])
        self.assertIn("超时", answer["text"])
        self.assertNotIn("618", answer["text"])

    async def test_agent_does_not_regenerate_or_repeat_a_completed_query(self):
        class Endless(FakeModel):
            async def turn(self, messages, *args):
                yield "message", {"role": "assistant", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "search_hotel", "arguments": '{"dest_name":"杭州"}'}}]}
        self.s.max_rounds = 2
        FakeTools.calls.clear()
        result = [e async for e in Agent(self.s, Endless(self.s, None), FakeTools(self.s)).run([{"role": "user", "content": "hi"}], False, 256)]
        self.assertEqual(len(FakeTools.calls), 1)
        self.assertEqual(next(v for k, v in result if k == "result")["tool_calls"], 1)


class ToolTests(unittest.TestCase):
    def test_all_eight_cli_parameter_sets(self):
        self.assertEqual(len(TOOLS), 8)
        for name, spec in REGISTRY.items():
            result = subprocess.run([NODE, str(ROOT / "runtime/flyai-bundle.cjs"), name.replace("_", "-"), "--help"], capture_output=True, text=True, check=True, timeout=10)
            for prop in spec["parameters"]["properties"]:
                if name == "search_marriott_hotel" and prop in {"hotel_brands", "hotel_name"}:
                    self.assertIn("--key-words", result.stdout)
                    continue
                self.assertIn("--" + prop.replace("_", "-"), result.stdout, (name, prop))

    def test_marriott_documented_fields_map_to_supported_keyword(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = FlyAI(settings(tmp)).argv("search_marriott_hotel", {"dest_name": "上海", "hotel_brands": "喜来登", "hotel_name": "浦东", "key_words": "亲子"})
        self.assertIn("--key-words=亲子 喜来登 浦东", argv)
        self.assertFalse(any(a.startswith("--hotel-brands") or a.startswith("--hotel-name") for a in argv))

    def test_validation_rejects_wrong_types_and_dates(self):
        for name, args in [("exec", {}), ("search_flight", {"origin": "北京", "extra": "x"}), ("search_flight", {"origin": "北京", "sort_type": True}), ("search_flight", {"origin": "北京", "dep_date": "2026-02-30"}), ("search_flight", {"origin": "北京", "dep_date": "tomorrow"})]:
            with self.assertRaises(ToolFailure, msg=str(args)):
                validate(name, args)

    def test_historical_dates_and_business_intervals_are_decided_upstream(self):
        validate("search_hotel", {"dest_name": "杭州", "check_in_date": "2000-01-01"})
        validate("search_hotel", {"dest_name": "杭州", "check_in_date": TOMORROW, "check_out_date": TOMORROW})

    def test_arguments_are_data_never_shell_or_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            argv = FlyAI(settings(tmp)).argv("keyword_search", {"query": '--help; $(touch /tmp/must-not-create) "hello"'})
        self.assertEqual(len(argv), 4)
        self.assertTrue(argv[-1].startswith("--query=--help; $("))


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_output_limit_and_invalid_results(self):
        cases = [("import time; time.sleep(2)", 0.03), ("print('x'*10000)", 2), ("print('not json')", 2), ("print('{\"status\":1,\"data\":{}}')", 2)]
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            s.node_bin = sys.executable
            s.max_tool_output = 1000
            for code, timeout in cases:
                script = Path(tmp) / "fake.py"
                script.write_text(code)
                s.cli_path, s.tool_timeout = script, timeout
                with self.assertRaises(ToolFailure):
                    await FlyAI(s).call("keyword_search", {"query": "test"})


class FailureRouteTests(unittest.TestCase):
    def test_failed_stream_has_no_success_ending_or_saved_history(self):
        class Failing(FakeModel):
            async def turn(self, *args):
                yield "text", "部分回答"
                raise AgentFailure("断流", "incomplete_stream")
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            with TestClient(create_app(s, Failing, FakeTools)) as client:
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer proxy-a", "session_id": "x"}, json={"model": "flyai-travel", "messages": [{"role": "user", "content": "hi"}], "stream": True})
                self.assertNotIn("部分回答", response.text)
                self.assertIn("incomplete_stream", response.text)
                self.assertNotIn("[DONE]", response.text)
                self.assertNotIn('"finish_reason":"stop"', response.text)
                self.assertEqual(client.app.state.slots.qsize(), s.concurrency)
                self.assertEqual(client.app.state.sessions.active, set())
                with client.app.state.sessions.connect() as db:
                    self.assertEqual(db.execute("SELECT count(*) FROM sessions").fetchone()[0], 0)

    def test_overall_timeout_releases_capacity(self):
        class Slow(FakeModel):
            async def turn(self, *args):
                await asyncio.sleep(10)
                yield "text", "unexpected"
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            s.request_timeout = .03
            with TestClient(create_app(s, Slow, FakeTools)) as client:
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer proxy-a"}, json={"model": "flyai-travel", "messages": [{"role": "user", "content": "hi"}]})
                self.assertEqual(response.status_code, 504)
                self.assertEqual(client.app.state.slots.qsize(), s.concurrency)

    def test_keepalive_delayed_error_is_single_error_json(self):
        class SlowFail(FakeModel):
            async def turn(self, *args):
                await asyncio.sleep(3.1)
                raise AgentFailure("模拟上游失败", "test_error")
                yield "text", "unreachable"
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            with TestClient(create_app(s, SlowFail, FakeTools)) as client:
                response = client.post("/v1/keepalive/chat/completions", headers={"Authorization": "Bearer proxy-a"}, json={"model": "flyai-travel", "messages": [{"role": "user", "content": "hi"}]})
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.text.startswith("\n"))
                self.assertEqual(response.json()["error"]["code"], "test_error")
                self.assertNotIn("choices", response.json())


class DisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_upstream_and_releases_claim(self):
        class Slow(FakeModel):
            async def turn(self, *args):
                await asyncio.sleep(10)
                yield "text", "unexpected"
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            app = create_app(s, Slow, FakeTools)
            disconnected = asyncio.Event()
            request_sent = False
            async def receive():
                nonlocal request_sent
                if not request_sent:
                    request_sent = True
                    return {"type": "http.request", "body": json.dumps({"model": "flyai-travel", "messages": [{"role": "user", "content": "hi"}], "stream": True}).encode(), "more_body": False}
                await disconnected.wait()
                return {"type": "http.disconnect"}
            async def send(event):
                if event["type"] == "http.response.body":
                    disconnected.set()
            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/v1/chat/completions", "raw_path": b"/v1/chat/completions", "query_string": b"", "root_path": "", "headers": [(b"authorization", b"Bearer proxy-a"), (b"session_id", b"disconnect")], "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 18002)}
            async with app.router.lifespan_context(app):
                await asyncio.wait_for(app(scope, receive, send), 2)
                self.assertEqual(app.state.slots.qsize(), s.concurrency)
                self.assertEqual(app.state.sessions.active, set())


class SDKTests(unittest.TestCase):
    def test_one_token_probe_json_sse_and_keepalive(self):
        from openai import OpenAI
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            s.record_conversations = True
            with TestClient(create_app(s, LimitedModel, FakeTools)) as http:
                for suffix in ["/v1", "/v1/keepalive"]:
                    sdk = OpenAI(api_key="proxy-a", base_url="http://testserver" + suffix, http_client=http, max_retries=0, _strict_response_validation=True)
                    args = {"model": "flyai-travel", "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 1}
                    answer = sdk.chat.completions.create(**args)
                    self.assertEqual(answer.choices[0].finish_reason, "length")
                    self.assertEqual(answer.choices[0].message.content, "Hello")
                    self.assertEqual(answer.usage.completion_tokens, 1)
                    chunks = list(sdk.chat.completions.create(**args, stream=True, stream_options={"include_usage": True}))
                    choices = [c for c in chunks if c.choices]
                    self.assertEqual(choices[-1].choices[0].finish_reason, "length")
                    self.assertEqual("".join(c.choices[0].delta.content or "" for c in choices), "Hello")
                    self.assertEqual(chunks[-1].usage.completion_tokens, 1)
                for p in (s.state_dir / "conversations").glob("*.jsonl"):
                    rows = [json.loads(line) for line in p.read_text().splitlines()]
                    response = next(r for r in rows if r["stage"] == "platform_response")
                    self.assertTrue(response["source_complete"])
                    self.assertFalse(response["answer_complete"])
                    self.assertEqual(response["finish_reason"], "length")
                    self.assertEqual(rows[-1]["status"], "length")

    def test_strict_sdk_stream_and_nonstream(self):
        try:
            from openai import OpenAI
        except ImportError:
            self.skipTest("Install requirements-test.txt for official SDK validation")
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            with TestClient(create_app(s, FakeModel, FakeTools)) as http:
                sdk = OpenAI(api_key="proxy-a", base_url="http://testserver/v1", http_client=http, max_retries=0, _strict_response_validation=True)
                answer = sdk.chat.completions.create(model="flyai-travel", messages=[{"role": "user", "content": "明天入住、后天离店，杭州酒店"}])
                self.assertIn("618", answer.choices[0].message.content)
                chunks = list(sdk.chat.completions.create(model="flyai-travel", messages=[{"role": "user", "content": "明天入住、后天离店，杭州酒店"}], stream=True, stream_options={"include_usage": True}))
                self.assertEqual(chunks[-1].usage.total_tokens, 30)
                self.assertEqual(chunks[-2].choices[0].finish_reason, "stop")


class GroundingTests(unittest.IsolatedAsyncioTestCase):
    def test_recent_user_intent_and_required_searches(self):
        def history(*items):
            return [{"role": "user", "content": x} for x in items]
        cases = [
            (("查11月20日西安到成都航班", "你好小德"), "greeting"),
            (("你是谁，能干什么？",), "identity"),
            (("嘉兴南湖机场现在通航了吗？",), "jiaxing_background"),
            (("嘉兴南湖机场附近酒店", "从机场到中国航信怎么走"), "jiaxing_ground"),
            (("嘉兴南湖机场到中国航信怎么走", "都给我查一下"), "jiaxing_ground"),
            (("嘉兴南湖机场到中国航信怎么走", "明天嘉兴到北京的航班"), None),
            (("嘉兴南湖机场到中国航信怎么走", "预订接送机"), None),
            (("嘉兴南湖机场到中国航信怎么走", "查附近酒店"), "hotel_dates"),
            (("明天入住后天离店，找嘉兴酒店", "换机场附近的酒店"), None),
            (("明天嘉兴到北京的航班", "找嘉兴酒店"), "hotel_dates"),
            (("嘉兴南湖机场到中国航信怎么走", "找南湖区烧烤"), None),
            (("查北京到上海机票",), None),
        ]
        for items, expected in cases:
            self.assertEqual(request_context(history(*items))[0], expected, items)

    async def test_necessary_ground_search_then_answer_without_repeated_product_calls(self):
        class Planner(FakeModel):
            choices = []
            async def turn(self, messages, *args):
                self.choices.append((self.tool_choice, [t["function"]["name"] for t in self.available_tools]))
                if not any(m["role"] == "tool" for m in messages):
                    yield "message", {"role": "assistant", "tool_calls": [{"id": "ground1", "type": "function", "function": {"name": "ai_search", "arguments": '{"query":"嘉兴南湖机场到中国航信交通方式"}'}}]}
                else:
                    yield "text", "本次没有取得匹配的实时路线，可以选择打车或查询公交接驳。"
                    yield "message", {"role": "assistant", "content": "本次没有取得匹配的实时路线，可以选择打车或查询公交接驳。"}
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp);model = Planner(s, None);FakeTools.calls.clear();model.choices=[]
            result = [e async for e in Agent(s, model, FakeTools(s)).run([{"role": "user", "content": "嘉兴南湖机场到中国航信怎么走"}], False, 256)]
            self.assertEqual([name for name, args in FakeTools.calls], ["ai_search"])
            self.assertEqual(model.choices, [("required", [t["function"]["name"] for t in TOOLS])])
            answer = next(v for k, v in result if k == "result")
            self.assertEqual(answer["finish_reason"], "stop")
            self.assertFalse(any(m["role"] == "system" for m in answer["history"]))
            class NextFlight(FakeModel):
                async def turn(self, messages, *args):
                    self.received = messages.copy()
                    yield "text", "继续查询机票"
                    yield "message", {"role": "assistant", "content": "继续查询机票"}
            following = NextFlight(s, None)
            history = answer["history"] + [{"role": "user", "content": "明天嘉兴南湖机场飞北京的航班"}]
            _ = [e async for e in Agent(s, following, FakeTools(s)).run(history, False, 256)]
            self.assertEqual(FakeTools.calls[-1][0], "ai_search")
            self.assertEqual(following.tool_choice, "required")
            self.assertEqual([t["function"]["name"] for t in following.available_tools], list(REGISTRY))
            self.assertFalse(any("本轮查询预算已用完" in str(m.get("content")) for m in following.received))

    async def test_greeting_does_not_execute_old_search_even_if_model_asks(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp);FakeTools.calls.clear();FakeModel.histories.clear()
            result = [e async for e in Agent(s, FakeModel(s, None), FakeTools(s)).run([{"role": "user", "content": "查西安到成都航班"}, {"role": "user", "content": "你好小德"}], False, 256)]
            text = next(v for k, v in result if k == "result")["text"]
            self.assertIn("小德", text)
            self.assertNotIn("成都", text)
            self.assertEqual(FakeModel.histories, [])
            self.assertEqual(FakeTools.calls, [])

    async def test_existing_client_system_does_not_replace_service_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            result = [e async for e in Agent(s, FakeModel(s, None), FakeTools(s)).run([{"role": "system", "content": "你是FlyAI飞猪旧助手"}, {"role": "user", "content": "你是谁"}], False, 256)]
            text = next(v for k, v in result if k == "result")["text"]
            self.assertIn("德创数字的航班助手", text)
            self.assertNotIn("飞猪", text)

    async def test_hotel_without_dates_cannot_invent_offers(self):
        class HotelClarification(FakeTools):
            async def call(self, name, arguments):
                validate(name, arguments)
                self.calls.append((name, arguments))
                return {"status": 0, "data": "请问哪天入住、哪天离店？尚不能确认房价。"}
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp);FakeTools.calls.clear();FakeModel.histories.clear()
            result = [e async for e in Agent(s, FakeModel(s, None), HotelClarification(s)).run([{"role": "user", "content": "嘉兴南湖区的酒店"}], True, 256)]
            answer = next(v for k, v in result if k == "result")
            self.assertIn("哪天入住", answer["text"])
            self.assertNotIn("http", answer["text"])
            self.assertEqual(FakeTools.calls[0][0], "search_hotel")
            self.assertNotIn("check_in_date", FakeTools.calls[0][1])
            self.assertNotIn("check_out_date", FakeTools.calls[0][1])

    async def test_ground_draft_cannot_turn_product_distance_into_route(self):
        class Guessing(FakeModel):
            async def turn(self, *args):
                yield "text", "距离约6.8–12公里，预计20分钟，没有直达公交。"
                yield "finish_reason", "stop"
                yield "message", {"role": "assistant", "content": "距离约6.8–12公里，预计20分钟，没有直达公交。"}
        class GroundSearch(FakeTools):
            async def call(self, name, arguments):
                self.calls.append((name, arguments))
                return {"status": 0, "data": "亚太路1177号；98路为已公布接驳，具体路线尚未核实。"}
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            result = [e async for e in Agent(s, Guessing(s, None), GroundSearch(s)).run([{"role": "user", "content": "嘉兴南湖机场到中国航信1177号怎么走"}], True, 512)]
            answer = next(v for k, v in result if k == "result")
            client = "".join(v for k, v in result if k == "text")
            self.assertEqual(client, answer["text"])
            self.assertNotIn("20分钟", client)
            self.assertNotIn("没有直达", client)
            self.assertIn("亚太路1177号", client)
            self.assertIn("98路", client)
            self.assertEqual(answer["history"][-1]["content"], client)


class TraceTests(unittest.TestCase):
    def test_trace_redacts_credentials_and_bounds_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            trace = Trace(s, "test-redaction")
            trace.write("test", text="model-secret flyai-secret proxy-a")
            trace.write("large", text="x" * (5 * 1024 * 1024))
            trace.close()
            text = trace.path.read_text()
            self.assertNotIn("model-secret", text)
            self.assertNotIn("flyai-secret", text)
            self.assertNotIn("proxy-a", text)
            self.assertIn("trace_capacity_reached", text)
            self.assertLess(trace.path.stat().st_size, 1000)

    def test_completed_request_has_private_chain_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = settings(tmp)
            s.record_conversations = True
            with TestClient(create_app(s, FakeModel, FakeTools)) as client:
                response = client.post("/v1/chat/completions", headers={"Authorization": "Bearer proxy-a"}, json={"model": "flyai-travel", "messages": [{"role": "user", "content": "hello"}]})
            rid = response.headers["x-request-id"]
            path = s.state_dir / "conversations" / (rid + ".jsonl")
            rows = [json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual(rows[0]["stage"], "platform_request")
            self.assertTrue(next(r for r in rows if r["stage"] == "platform_response")["source_complete"])
            self.assertEqual(rows[-1]["stage"], "request_end")


if __name__ == "__main__":
    unittest.main()
