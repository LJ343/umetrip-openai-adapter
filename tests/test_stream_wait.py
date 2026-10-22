"""慢首包、等待提示、队列顺序和取消的回归；不访问真实接口。"""
import asyncio
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main as proxy


def source(delta=None, finish=None):
    return proxy.sse({'choices': [{'index': 0, 'delta': delta or {}, 'finish_reason': finish}]})


class ControlledStream(httpx.AsyncByteStream):
    def __init__(self, pieces):
        self.pieces = pieces
        self.closed = False

    async def __aiter__(self):
        for delay, raw in self.pieces:
            if delay:
                await asyncio.sleep(delay)
            yield raw

    async def aclose(self):
        self.closed = True


def objects(raw):
    return [json.loads(part[6:]) for part in raw.decode().splitlines() if part.startswith('data: {')]


def text(raw):
    return ''.join(choice['delta'].get('content') or '' for obj in objects(raw) for choice in obj.get('choices', []))


class StreamWaitTests(unittest.IsolatedAsyncioTestCase):
    def response(self, client, usage=False):
        request = client.build_request('POST', 'http://upstream/chat/completions', json={'stream': True})
        return proxy.streaming_response(client, request, 'one-response', 'qianrang', time.monotonic(), usage)

    async def consume(self, response):
        return b''.join([raw async for raw in response.body_iterator])

    async def test_slow_headers_immediate_role_wait_once_and_original_answer_in_order(self):
        release = asyncio.Event()
        stream = ControlledStream([(0, source({'content': '前段'})), (0, source({'content': '后段'})),
                                   (0, source(finish='stop') + b'data: [DONE]\n\n')])
        async def handler(request):
            await release.wait()
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with patch.multiple(proxy, HEARTBEAT_INTERVAL=0.02, STREAM_WAIT_AFTER=0.055, STREAM_WAIT_MESSAGE='请稍等。\n\n'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                response = self.response(client, usage=True)
                first = await asyncio.wait_for(response.body_iterator.__anext__(), timeout=0.3)
                self.assertFalse(release.is_set())
                self.assertEqual(objects(first)[0]['choices'][0]['delta']['role'], 'assistant')
                parts = [first]
                while '请稍等' not in text(b''.join(parts)):
                    parts.append(await asyncio.wait_for(response.body_iterator.__anext__(), timeout=0.3))
                self.assertNotIn(b'[DONE]', b''.join(parts))
                release.set()
                parts.append(await self.consume(response))
        raw = b''.join(parts)
        self.assertEqual(text(raw), '请稍等。\n\n前段后段')
        self.assertEqual(raw.count(b'data: [DONE]'), 1)
        self.assertTrue(stream.closed)
        objs = objects(raw)
        self.assertEqual(len({obj['id'] for obj in objs}), 1)
        self.assertEqual(len({obj['created'] for obj in objs}), 1)
        self.assertTrue(all('usage' in obj for obj in objs))
        self.assertEqual(objs[-1]['choices'], [])

    async def test_fast_answer_followed_by_long_silence_never_gets_wait_text(self):
        stream = ControlledStream([(0, source({'content': '真实开始'})), (0.12, source({'content': '真实结束'})),
                                   (0, source(finish='stop') + b'data: [DONE]\n\n')])
        async def handler(request):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with patch.multiple(proxy, HEARTBEAT_INTERVAL=0.02, STREAM_WAIT_AFTER=0.04, STREAM_WAIT_MESSAGE='不应出现'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                raw = await self.consume(self.response(client))
        self.assertEqual(text(raw), '真实开始真实结束')
        self.assertGreater(sum(obj.get('choices', [{}])[0].get('delta', {}).get('content') == '' for obj in objects(raw)), 2)

    async def test_reasoning_traffic_does_not_postpone_notice(self):
        stream = ControlledStream([(0.025, source({'reasoning_content': '思'})) for _ in range(5)] +
                                  [(0, source({'content': '原始答案'})), (0, source(finish='stop') + b'data: [DONE]\n\n')])
        async def handler(request):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with patch.multiple(proxy, HEARTBEAT_INTERVAL=0.03, STREAM_WAIT_AFTER=0.045, STREAM_WAIT_MESSAGE='请稍等\n'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                raw = await self.consume(self.response(client))
        self.assertEqual(text(raw), '请稍等\n原始答案')
        reasoning = ''.join(c['delta'].get('reasoning_content', '') for obj in objects(raw) for c in obj.get('choices', []))
        self.assertEqual(reasoning, '思' * 5)

    async def test_upstream_http_errors_remain_sse_errors_without_done(self):
        for status in (401, 429, 504):
            async def handler(request):
                return httpx.Response(status, json={'message': '上游错误'})
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                raw = await self.consume(self.response(client))
            self.assertIn('error', objects(raw)[-1])
            self.assertNotIn(b'[DONE]', raw)
            self.assertTrue(all(c.get('finish_reason') is None for obj in objects(raw) for c in obj.get('choices', [])))

    async def test_disconnect_cancels_pending_upstream_open(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def handler(request):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response = self.response(client)
            await response.body_iterator.__anext__()
            await asyncio.wait_for(entered.wait(), 0.3)
            await response.body_iterator.aclose()
            self.assertTrue(cancelled.is_set())

    async def test_notice_is_not_a_successful_answer_when_upstream_fails(self):
        stream = ControlledStream([(0.07, b'event: error\ndata: {"message":"failed"}\n\n')])
        async def handler(request):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with patch.multiple(proxy, HEARTBEAT_INTERVAL=0.015, STREAM_WAIT_AFTER=0.03, STREAM_WAIT_MESSAGE='请稍等'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                raw = await self.consume(self.response(client))
        self.assertEqual(text(raw), '请稍等')
        self.assertIn('error', objects(raw)[-1])
        self.assertNotIn(b'[DONE]', raw)

    async def test_notice_can_be_disabled_without_disabling_heartbeats(self):
        stream = ControlledStream([(0.07, source({'content': '纯原文'})),
                                   (0, source(finish='stop') + b'data: [DONE]\n\n')])
        async def handler(request):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with patch.multiple(proxy, HEARTBEAT_INTERVAL=0.015, STREAM_WAIT_AFTER=0, STREAM_WAIT_MESSAGE='不应出现'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                raw = await self.consume(self.response(client))
        self.assertEqual(text(raw), '纯原文')
        self.assertGreater(len(objects(raw)), 4)

    async def test_native_tool_call_does_not_trigger_wait_during_later_silence(self):
        call = {'tool_calls': [{'index': 0, 'id': 'call-test', 'type': 'function',
                               'function': {'name': 'query', 'arguments': '{}'}}]}
        stream = ControlledStream([(0, source(call)), (0.08, source(finish='tool_calls') + b'data: [DONE]\n\n')])
        async def handler(request):
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with patch.multiple(proxy, HEARTBEAT_INTERVAL=0.02, STREAM_WAIT_AFTER=0.035, STREAM_WAIT_MESSAGE='不应出现'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                raw = await self.consume(self.response(client))
        self.assertEqual(text(raw), '')
        calls = [c['delta']['tool_calls'] for obj in objects(raw) for c in obj.get('choices', []) if c['delta'].get('tool_calls')]
        self.assertEqual(calls, [call['tool_calls']])

    async def test_json_output_requests_do_not_receive_visible_wait_text(self):
        seen = []
        stream = ControlledStream([(0.07, source({'content': '{"ok":true}'})),
                                   (0, source(finish='stop') + b'data: [DONE]\n\n')])
        async def handler(request):
            seen.append(json.loads(request.content))
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with patch.multiple(proxy, PROXY_API_KEYS={'test'}, UPSTREAM_API_KEY='upstream',
                            HEARTBEAT_INTERVAL=0.015, STREAM_WAIT_AFTER=0.03, STREAM_WAIT_MESSAGE='不应出现'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
                proxy.app.state.client = upstream
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url='http://proxy') as client:
                    r = await client.post('/v1/chat/completions', headers={'Authorization': 'Bearer test'}, json={
                        'model': 'qianrang', 'messages': [{'role': 'user', 'content': '输出 JSON'}],
                        'stream': True, 'response_format': {'type': 'json_object'}})
        self.assertEqual(json.loads(text(r.content)), {'ok': True})
        self.assertEqual(seen[0]['response_format'], {'type': 'json_object'})

    async def test_connection_failures_become_standard_stream_errors(self):
        for exception, code in ((httpx.ConnectTimeout('slow'), 'upstream_timeout'),
                                (httpx.ConnectError('unavailable'), 'upstream_unavailable')):
            async def handler(request):
                raise exception
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                raw = await self.consume(self.response(client))
            self.assertEqual(objects(raw)[-1]['error']['code'], code)
            self.assertNotIn(b'[DONE]', raw)


if __name__ == '__main__':
    unittest.main()
