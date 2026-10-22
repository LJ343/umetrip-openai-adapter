"""协议及内容保留回归测试，不调用真实上游，不需要密钥。"""
import asyncio
import json
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import main as proxy


def packet(content=None, finish=None, delta=None, **extra):
    return proxy.sse({"id": "source", "object": "chat.completion.chunk", "created": 1, "model": "qianrang",
                      "choices": [{"index": 0, "delta": delta if delta is not None else (
                          {} if content is None else {"content": content}), "finish_reason": finish}], **extra}).decode()


def event(name, payload):
    return "event: " + name + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def complete(text="答案"):
    return packet(text) + packet(finish="stop") + "data: [DONE]\n\n"


def chunks(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: {')]


def stream_text(response):
    return "".join(c["delta"].get("content") or "" for obj in chunks(response) for c in obj.get("choices", []))


class ByteStream(httpx.AsyncByteStream):
    def __init__(self, raw):
        self.raw = raw.encode()
        self.closed = False

    async def __aiter__(self):
        for i in range(0, len(self.raw), 11):
            yield self.raw[i:i + 11]

    async def aclose(self):
        self.closed = True


class PreservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_separate_first_event_reasoning_text_and_tools(self):
        response = httpx.Response(200)
        source = proxy.UpstreamStream(response, 'diagnostics', 'qianrang')
        with patch.object(proxy.time, 'monotonic') as now:
            now.return_value = 11.0
            source._handle('tool_call_start', json.dumps({'data': {'name': 'lookup'}}))
            now.return_value = 12.0
            source._handle('thinking_delta', json.dumps({'data': '思考文字'}))
            now.return_value = 13.0
            source._handle('tool_result_end', json.dumps({'data': {'content': '业务结果'}}))
            now.return_value = 14.0
            source._handle(None, json.dumps({'choices': [{'delta': {'content': '最终正文'}}]}))
            now.return_value = 15.0
            source._handle(None, json.dumps({'choices': [{'delta': {'reasoning_content': '补充'}}]}))
        fields = source.diagnostic_fields(10.0)
        for expected in ('first_upstream_event_ms=1000', 'first_reasoning_ms=2000',
                         'first_upstream_text_ms=4000', 'reasoning_chars=6',
                         'source_events=5', 'tool_starts=1', 'tool_results=1'):
            self.assertIn(expected, fields)
        for private in ('思考文字', '业务结果', '最终正文', '补充'):
            self.assertNotIn(private, fields)

    async def request(self, raw=None, stream=False, updates=None, status=200, content_type="text/event-stream", byte_stream=None, request_headers=None):
        self.seen = []
        self.stream = byte_stream or ByteStream(raw if raw is not None else complete())

        async def handler(request):
            self.seen.append(request)
            return httpx.Response(status, headers={"content-type": content_type}, stream=self.stream)

        with patch.multiple(proxy, PROXY_API_KEYS={"proxy-test"}, UPSTREAM_API_KEY="upstream-test",
                            NONSTREAM_FINAL_SEGMENT_ONLY=False, TEXT_END_SEPARATOR="", STRIP_CARDS=True,
                            EMIT_REASONING=True):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
                proxy.app.state.client = upstream
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url="http://proxy") as client:
                    body = {"model": "qianrang", "messages": [{"role": "user", "content": "查航班"}], "stream": stream}
                    body.update(updates or {})
                    headers = {"Authorization": "Bearer proxy-test", "x-session-id": "user-session"}
                    headers.update(request_headers or {})
                    return await client.post('/v1/chat/completions', json=body, headers=headers)

    async def assert_both(self, raw, expected):
        ordinary = await self.request(raw)
        self.assertEqual(ordinary.status_code, 200, ordinary.text)
        self.assertEqual(ordinary.json()["choices"][0]["message"]["content"], expected)
        streamed = await self.request(raw, stream=True)
        self.assertEqual(stream_text(streamed), expected)
        self.assertIn("data: [DONE]", streamed.text)

    async def test_real_flight_and_weather_only_remove_cards(self):
        for name in ("flight", "weather", "train", "airfare"):
            raw = (ROOT / 'tests/fixtures' / ('upstream-' + name + '.sse.txt')).read_text()
            original = ''.join((choice.get('delta') or {}).get('content') or ''
                               for line in raw.splitlines() if line.startswith('data: {')
                               for choice in json.loads(line[6:]).get('choices', []))
            expected = re.sub(r'<([A-Za-z0-9_]+_card)>.*?</\1>', '', original, flags=re.S)
            await self.assert_both(raw, expected)

    async def test_all_segments_and_whitespace_preserved(self):
        pieces = ['  正在查询。\n', '暂时没有结果。', '\n后来查到两班。 \n']
        raw = ''.join(packet(piece) + event('text_end', {'data': None}) for piece in pieces)
        await self.assert_both(raw + packet(finish='stop') + 'data: [DONE]\n\n', ''.join(pieces))

    async def test_card_split_across_chunks_and_text_end(self):
        raw = packet('前文<jump_') + event('text_end', {'data': None}) + packet('card>{"pageId":"1"}')
        raw += packet('</jump_') + packet('card> 后文\n') + packet(finish='stop') + 'data: [DONE]\n\n'
        await self.assert_both(raw, '前文 后文\n')

    async def test_unclosed_card_and_ordinary_angle_brackets(self):
        await self.assert_both(complete('a<b，<普通文本>正文<jump_card>丢弃未闭合卡片'), 'a<b，<普通文本>正文')

    async def test_tool_result_kept_but_metrics_and_start_hidden(self):
        raw = event('prepare_model_input', {'type': 'prepare_model_input', 'duration_ms': 777})
        raw += event('ttft', {'type': 'ttft', 'duration_ms': 888})
        raw += event('tool_call_start', {'data': {'parameters': {'private_input': 'not a result'}}})
        business = {'state': 'success', 'content': '查到两班', 'result': {'flightNo': 'CA1831', 'status': '已到达'}}
        raw += event('tool_result_end', {'data': {'name': '航班查询', 'call_id': 'internal-call', **business}})
        raw += complete('模型最终答案')
        expected = '\n\n' + json.dumps(business, ensure_ascii=False, indent=2) + '\n\n模型最终答案'
        await self.assert_both(raw, expected)

    async def test_placeholder_status_not_business_but_failures_kept(self):
        status = event('tool_result_end', {'data': {'tool_key': 'flight', 'state': 'success', 'content': '查询结果：是否成功'}})
        await self.assert_both(status + complete(), '答案')
        failed = {'state': 'error', 'content': '本次查询失败，可稍后查询'}
        await self.assert_both(event('tool_result_end', {'data': failed}) + complete(),
                               '\n\n' + json.dumps(failed, ensure_ascii=False, indent=2) + '\n\n答案')

    async def test_new_business_event_and_nested_card(self):
        result = {'order_id': 'test-order', 'message': '待确认<jump_card>{"pageId":"1"}</jump_card>', 'amount': 50}
        expected = {**result, 'message': '待确认'}
        raw = event('confirmation_required', result) + complete('请确认')
        await self.assert_both(raw, '\n\n' + json.dumps(expected, ensure_ascii=False, indent=2) + '\n\n请确认')

    async def test_reasoning_refusal_annotations_logprobs_preserved(self):
        annotation = {'type': 'url_citation', 'url_citation': {'url': 'https://example.com', 'title': '说明', 'start_index': 0, 'end_index': 1}}
        raw = event('thinking_delta', {'data': '思考一'}) + event('thinking_end', {'data': None})
        raw += event('thinking_delta', {'data': '思考二<jump_card>卡片</jump_card>'})
        obj = json.loads(packet(delta={'content': '答案', 'refusal': '拒绝说明', 'annotations': [annotation]}, system_fingerprint='fp')[6:])
        obj['choices'][0]['logprobs'] = {'content': [{'token': '答', 'logprob': -0.1, 'bytes': None, 'top_logprobs': []}]}
        raw += proxy.sse(obj).decode() + packet(finish='stop') + 'data: [DONE]\n\n'
        r = await self.request(raw)
        message = r.json()['choices'][0]['message']
        self.assertEqual(message['reasoning_content'], '思考一思考二')
        self.assertEqual(message['refusal'], '拒绝说明')
        self.assertEqual(message['annotations'], [annotation])
        self.assertEqual(r.json()['system_fingerprint'], 'fp')
        self.assertEqual(r.json()['choices'][0]['logprobs'], obj['choices'][0]['logprobs'])
        r = await self.request(raw, stream=True)
        reasoning = ''.join(c['delta'].get('reasoning_content', '') for obj in chunks(r) for c in obj.get('choices', []))
        self.assertEqual(reasoning, '思考一思考二')

    async def test_standard_tool_call_fragments_are_not_reexecuted(self):
        raw = packet(delta={'tool_calls': [{'index': 0, 'id': 'call-1', 'type': 'function', 'function': {'name': 'query', 'arguments': '{"city":'}}]})
        raw += packet(delta={'tool_calls': [{'index': 0, 'function': {'arguments': '"北京"}'}}]})
        raw += packet(finish='tool_calls') + 'data: [DONE]\n\n'
        r = await self.request(raw)
        self.assertEqual(r.json()['choices'][0]['message']['tool_calls'], [
            {'id': 'call-1', 'type': 'function', 'function': {'name': 'query', 'arguments': '{"city":"北京"}'}}])
        self.assertIsNone(r.json()['choices'][0]['message']['content'])
        self.assertEqual(len(self.seen), 1)

    async def test_unknown_chunk_fields_become_visible_business_content(self):
        raw = packet('原文', business_result={'gate': 'C01'}) + packet(delta={'confirmation': '请确认'})
        raw += packet(finish='stop') + 'data: [DONE]\n\n'
        r = await self.request(raw)
        text = r.json()['choices'][0]['message']['content']
        self.assertIn('C01', text)
        self.assertIn('请确认', text)
        self.assertIn('原文', text)

    async def test_data_only_tool_event_and_explicit_card_event(self):
        raw = proxy.sse({'type': 'ttft', 'duration_ms': 999}).decode()
        raw += proxy.sse({'event_type': 'tool_result_end', 'data': {'content': '业务结果'}}).decode()
        raw += event('jump_card', {'pageId': 'hidden-page'}) + complete('模型答案')
        await self.assert_both(raw, '\n\n业务结果\n\n模型答案')

    async def test_reasoning_only_length_result_is_preserved(self):
        raw = packet(delta={'reasoning_content': '推理达到长度限制'}) + packet(finish='length') + 'data: [DONE]\n\n'
        r = await self.request(raw)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()['choices'][0]['finish_reason'], 'length')
        self.assertEqual(r.json()['choices'][0]['message']['reasoning_content'], '推理达到长度限制')

    async def test_done_does_not_wait_for_network_eof(self):
        class OpenConnection(httpx.AsyncByteStream):
            closed = False
            async def __aiter__(self):
                yield complete().encode()
                raise AssertionError('应在 [DONE] 处停止读取')
            async def aclose(self):
                self.closed = True
        source = OpenConnection()
        r = await self.request(byte_stream=source)
        self.assertEqual(r.status_code, 200)
        self.assertTrue(source.closed)

    async def test_interrupted_stream_never_reports_success(self):
        raw = packet('部分答案') + packet(finish='stop')
        r = await self.request(raw)
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()['error']['code'], 'incomplete_stream')
        self.assertEqual(r.headers['x-should-retry'], 'false')
        r = await self.request(raw, stream=True)
        self.assertNotIn('data: [DONE]', r.text)
        self.assertNotIn('"finish_reason": "stop"', r.text)
        self.assertIn('incomplete_stream', r.text)

    async def test_error_after_source_finish_is_not_success(self):
        raw = packet('部分答案') + packet(finish='stop') + event('error', {'message': '工具失败'})
        r = await self.request(raw, stream=True)
        self.assertNotIn('data: [DONE]', r.text)
        self.assertNotIn('"finish_reason": "stop"', r.text)
        self.assertIn('工具失败', r.text)

    async def test_invalid_json_empty_and_missing_finish_fail(self):
        for raw, code in [('data: broken\n\n', 'invalid_upstream_json'), ('data: [DONE]\n\n', 'incomplete_stream'),
                          (packet('正文') + 'data: [DONE]\n\n', 'incomplete_stream'),
                          (packet(finish='stop') + 'data: [DONE]\n\n', 'empty_completion')]:
            with self.subTest(code=code):
                r = await self.request(raw)
                self.assertEqual(r.status_code, 502)
                self.assertEqual(r.json()['error']['code'], code)

    async def test_usage_only_packet_preserved_no_fabricated_counts(self):
        usage = {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15, 'completion_tokens_details': {'reasoning_tokens': 3}}
        raw = packet('答案') + packet(finish='stop') + proxy.sse({'choices': [], 'usage': usage}).decode() + 'data: [DONE]\n\n'
        r = await self.request(raw)
        self.assertEqual(r.json()['usage'], usage)
        r = await self.request(raw, stream=True, updates={'stream_options': {'include_usage': True}})
        self.assertEqual(chunks(r)[-1]['usage'], usage)
        self.assertEqual(chunks(r)[-1]['choices'], [])
        r = await self.request()
        self.assertIsNone(r.json()['usage'])
        r = await self.request(stream=True, updates={'stream_options': {'include_usage': True}})
        self.assertIsNone(chunks(r)[-1]['usage'])

    async def test_history_session_auth_and_parameters_preserved(self):
        messages = [{'role': 'system', 'content': '自定义身份'}, {'role': 'user', 'content': '查昨天'},
                    {'role': 'assistant', 'content': '昨天结果'}, {'role': 'user', 'content': '那今天呢'}]
        updates = {'messages': messages, 'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high',
                   'stream_options': {'include_usage': True}, 'user': 'business-user'}
        await self.request(updates=updates)
        sent = json.loads(self.seen[0].content)
        for key, value in updates.items():
            self.assertEqual(sent[key], value)
        self.assertTrue(sent['stream'])
        self.assertEqual(self.seen[0].headers['session_id'], 'user-session')
        self.assertEqual(self.seen[0].headers['authorization'], 'Bearer upstream-test')

    async def test_v2_session_id_header_takes_priority_over_legacy_alias(self):
        await self.request(request_headers={'session_id': 'v2-conversation', 'x-session-id': 'legacy-conversation'})
        self.assertEqual(self.seen[0].headers['session_id'], 'v2-conversation')
        self.assertNotIn('x-session-id', self.seen[0].headers)

    async def test_session_id_does_not_change_between_stream_and_nonstream(self):
        for streaming in (False, True):
            await self.request(stream=streaming, request_headers={'session_id': 'same-conversation'})
            self.assertEqual(self.seen[0].headers['session_id'], 'same-conversation')

    async def test_shared_client_cookie_not_sent_as_another_user_identity(self):
        self.requests = []
        async def handler(request):
            self.requests.append(request)
            return httpx.Response(200, headers={'content-type': 'text/event-stream', 'set-cookie': 'session=first-user; Path=/'}, stream=ByteStream(complete()))
        with patch.multiple(proxy, PROXY_API_KEYS={'proxy-test'}, UPSTREAM_API_KEY='upstream-test'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
                proxy.app.state.client = upstream
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url='http://proxy') as client:
                    for _ in range(2):
                        r = await client.post('/v1/chat/completions', headers={'Authorization': 'Bearer proxy-test'},
                                              json={'model': 'qianrang', 'messages': [{'role': 'user', 'content': '你好'}]})
                        self.assertEqual(r.status_code, 200)
        self.assertTrue(all('cookie' not in request.headers for request in self.requests))
        self.assertNotEqual(self.requests[0].headers['session_id'], self.requests[1].headers['session_id'])

    async def test_bad_stream_options_rejected_before_upstream(self):
        for update in [{'stream': 'false'}, {'stream_options': []}, {'stream_options': {'include_usage': 1}}, {'n': 2}, {'n': True}]:
            r = await self.request(updates=update)
            self.assertEqual(r.status_code, 400, str(update))
            self.assertEqual(self.seen, [])

    async def test_content_type_and_upstream_errors(self):
        r = await self.request(content_type='text/html')
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()['error']['code'], 'invalid_content_type')
        self.assertTrue(self.stream.closed)
        for status in [401, 429, 504]:
            r = await self.request(raw='{"message":"上游错误"}', status=status, content_type='application/json')
            self.assertEqual(r.status_code, status)

    async def test_explicit_message_event_and_sse_multiline(self):
        raw = ': heartbeat\r\nevent: message\r\ndata: {"choices":[{\r\ndata: "index":0,"delta":{"content":"中文 ✈"}}]}\r\n\r\n'
        raw += 'event: message\n' + packet(finish='stop') + 'event: message\ndata: [DONE]\n\n'
        await self.assert_both(raw, '中文 ✈')

    async def test_malformed_delta_fields_are_errors(self):
        for delta in [{'content': []}, {'tool_calls': 'bad'}, {'annotations': {}}, {'function_call': 3}]:
            r = await self.request(packet(delta=delta) + packet(finish='stop') + 'data: [DONE]\n\n')
            self.assertEqual(r.status_code, 502)
            self.assertEqual(r.json()['error']['code'], 'invalid_upstream_response')


if __name__ == '__main__':
    unittest.main()
