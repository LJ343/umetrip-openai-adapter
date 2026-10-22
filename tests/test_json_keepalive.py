"""可选 JSON 空白保活入口的边界和错误语义。"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx
from fastapi.responses import JSONResponse, StreamingResponse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main as proxy
from test_preservation import ByteStream, complete


class JsonKeepaliveTests(unittest.IsolatedAsyncioTestCase):
    def request(self, streaming=False):
        async def read_json():
            return {'stream': streaming}
        return SimpleNamespace(state=SimpleNamespace(proxy_request_id='json-test'), json=read_json)

    async def consume(self, response):
        return b''.join([part async for part in response.body_iterator])

    async def test_slow_json_is_one_json_text_with_only_whitespace_prefix(self):
        answer = {'object': 'chat.completion', 'choices': [{'message': {'content': '完整原文'}}]}
        async def handle(request):
            await asyncio.sleep(0.085)
            return JSONResponse(answer)
        with patch.multiple(proxy, chat_completions=handle, JSON_KEEPALIVE_AFTER=0.015, JSON_KEEPALIVE_INTERVAL=0.015):
            response = await proxy.chat_completions_json_keepalive(self.request())
            first = await asyncio.wait_for(response.body_iterator.__anext__(), 0.04)
            self.assertEqual(first, b'\n')
            raw = first + await self.consume(response)
        self.assertEqual(json.loads(raw), answer)
        self.assertTrue(raw.startswith(b'\n\n'))
        self.assertEqual(response.media_type, 'application/json')
        self.assertNotIn(b'data:', raw)
        self.assertNotIn('请稍等', raw.decode())

    async def test_fast_result_and_fast_validation_error_keep_original_status(self):
        for status in (200, 400, 401, 404, 429, 502):
            expected = JSONResponse({'value': 'fast'}, status_code=status)
            async def handle(request):
                return expected
            with patch.object(proxy, 'chat_completions', handle):
                result = await proxy.chat_completions_json_keepalive(self.request())
            self.assertIs(result, expected)
            self.assertEqual(result.status_code, status)

    async def test_late_error_has_explicit_committed_http_status_limit(self):
        async def handle(request):
            await asyncio.sleep(0.05)
            return proxy.openai_error(504, 'Upstream timeout', 'upstream_timeout', 'server_error')
        with patch.multiple(proxy, chat_completions=handle, JSON_KEEPALIVE_AFTER=0.01, JSON_KEEPALIVE_INTERVAL=0.01):
            response = await proxy.chat_completions_json_keepalive(self.request())
            raw = await self.consume(response)
        self.assertEqual(response.status_code, 200)  # 提前发空白后不能修改 HTTP 状态。
        self.assertEqual(json.loads(raw)['error']['code'], 'upstream_timeout')
        self.assertNotIn(b'[DONE]', raw)

    async def test_closed_client_cancels_the_single_pending_operation(self):
        cancelled = asyncio.Event()
        calls = 0
        async def handle(request):
            nonlocal calls
            calls += 1
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        with patch.multiple(proxy, chat_completions=handle, JSON_KEEPALIVE_AFTER=0.01):
            response = await proxy.chat_completions_json_keepalive(self.request())
            await response.body_iterator.__anext__()
            await response.body_iterator.aclose()
        self.assertTrue(cancelled.is_set())
        self.assertEqual(calls, 1)

    async def test_late_exception_still_returns_one_error_json(self):
        async def handle(request):
            await asyncio.sleep(0.04)
            raise RuntimeError('internal')
        with patch.multiple(proxy, chat_completions=handle, JSON_KEEPALIVE_AFTER=0.01, JSON_KEEPALIVE_INTERVAL=0.01):
            response = await proxy.chat_completions_json_keepalive(self.request())
            raw = await self.consume(response)
        self.assertEqual(json.loads(raw)['error']['code'], 'upstream_error')

    async def test_streaming_response_is_not_wrapped_as_json(self):
        async def body():
            yield b'data: [DONE]\n\n'
        expected = StreamingResponse(body(), media_type='text/event-stream')
        async def handle(request):
            return expected
        with patch.object(proxy, 'chat_completions', handle):
            response = await proxy.chat_completions_json_keepalive(self.request(streaming=True))
        self.assertIs(response, expected)

    async def test_real_routes_and_private_diagnostics(self):
        async def handler(request):
            await asyncio.sleep(0.05)
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=ByteStream(complete('实际答案')))
        with patch.multiple(proxy, PROXY_API_KEYS={'test-secret'}, UPSTREAM_API_KEY='upstream-secret',
                            JSON_KEEPALIVE_AFTER=0.01, JSON_KEEPALIVE_INTERVAL=0.01):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
                proxy.app.state.client = upstream
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url='http://proxy') as client:
                    for path, whitespace in [('/v1/chat/completions', False), ('/v1/keepalive/chat/completions', True)]:
                        with self.assertLogs('openai-proxy', level='INFO') as captured:
                            r = await client.post(path, headers={'Authorization': 'Bearer test-secret', 'session_id': 'private-session'},
                                json={'model': 'qianrang', 'stream': False, 'messages': [{'role': 'user', 'content': '不应进入日志的私密正文'}]})
                        self.assertEqual(r.status_code, 200)
                        self.assertEqual(r.json()['choices'][0]['message']['content'], '实际答案')
                        self.assertEqual(r.content.startswith(b'\n'), whitespace)
                        lines = '\n'.join(captured.output)
                        self.assertIn('session_source=session_id', lines)
                        self.assertIn('thinking_mode=unspecified reasoning_effort=unspecified', lines)
                        self.assertIn('message_shapes=user:text:', lines)
                        self.assertIn('last_user_hash=', lines)
                        self.assertIn('messages=1 user_messages=1', lines)
                        for secret in ('test-secret', 'upstream-secret', 'private-session', '不应进入日志的私密正文'):
                            self.assertNotIn(secret, lines)
                    models = await client.get('/v1/keepalive/models', headers={'Authorization': 'Bearer test-secret'})
                    self.assertEqual(models.status_code, 200)
                    self.assertEqual(models.json()['data'][0]['id'], 'qianrang')

    async def test_auth_validation_and_unknown_routes_have_correlated_diagnostics(self):
        with patch.multiple(proxy, PROXY_API_KEYS={'test-secret'}, UPSTREAM_API_KEY='upstream-secret'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url='http://proxy') as client:
                cases = [('/v1/chat/completions', {}, {}, 401),
                         ('/v1/keepalive/chat/completions', {'Authorization': 'Bearer test-secret'}, {}, 400),
                         ('/v1/missing', {}, {}, 404)]
                ids = []
                for path, headers, payload, status in cases:
                    with self.assertLogs('openai-proxy', level='INFO') as captured:
                        r = await client.post(path, headers=headers, json=payload)
                    self.assertEqual(r.status_code, status)
                    request_id = r.headers['X-Request-Id']
                    ids.append(request_id)
                    lines = '\n'.join(captured.output)
                    self.assertIn('request_id=' + request_id + ' stage=http_received', lines)
                    self.assertIn('http_status=' + str(status), lines)
                    self.assertNotIn('test-secret', lines)
                self.assertEqual(len(set(ids)), len(cases))

    async def test_malformed_json_has_a_trace_without_logging_the_body(self):
        with patch.multiple(proxy, PROXY_API_KEYS={'test-secret'}, UPSTREAM_API_KEY='upstream-secret'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url='http://proxy') as client:
                with self.assertLogs('openai-proxy', level='INFO') as captured:
                    r = await client.post('/v1/keepalive/chat/completions', content='secret-broken-json',
                                          headers={'Authorization': 'Bearer test-secret'})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(r.json()['error']['code'], 'invalid_json')
                self.assertTrue(r.headers['X-Request-Id'])
                self.assertNotIn('secret-broken-json', '\n'.join(captured.output))


if __name__ == '__main__':
    unittest.main()
