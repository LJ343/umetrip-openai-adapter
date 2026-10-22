"""Compare all four captured boundaries against real ASGI/HTTPX traffic."""
import asyncio
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main as proxy
from app.conversation_log import ConversationTrace, ResponseBodyCapture
from test_preservation import ByteStream, complete, event, stream_text


class ConversationLogTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, directory, streaming=False, path='/v1/chat/completions', raw=None,
                       status=200, body=None, delay=0, enabled=True, authorized=True):
        self.seen = []
        async def handler(request):
            self.seen.append(json.loads(request.content))
            if delay:
                await asyncio.sleep(delay)
            return httpx.Response(status, headers={'content-type': 'text/event-stream',
                                                   'set-cookie': 'private-cookie'},
                                  stream=ByteStream(raw if raw is not None else complete('首都 T3')))
        payload = body or {'model': 'qianrang', 'stream': streaming, 'messages': [
            {'role': 'user', 'content': '航班从首都 T3 出发'},
            {'role': 'assistant', 'content': '首都 T3'},
            {'role': 'user', 'content': '它从哪个航站楼出发？'}]}
        with patch.multiple(proxy, CONVERSATION_LOG_ENABLED=enabled, CONVERSATION_LOG_DIR=directory,
                            PROXY_API_KEYS={'private-proxy-key'}, UPSTREAM_API_KEY='private-upstream-key',
                            PACK_HISTORY_WITHOUT_SESSION=True, STREAM_WAIT_AFTER=0.015,
                            HEARTBEAT_INTERVAL=0.01, JSON_KEEPALIVE_AFTER=0.01, JSON_KEEPALIVE_INTERVAL=0.01):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
                proxy.app.state.client = upstream
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url='http://proxy') as client:
                    response = await client.post(path, json=payload, headers={
                        'authorization': 'Bearer private-proxy-key' if authorized else 'Bearer wrong-key',
                        'cookie': 'private-client-cookie'})
        files = list(Path(directory).glob('*.jsonl'))
        rows = [json.loads(line) for file in files for line in file.read_text().splitlines()]
        return response, rows, payload

    async def test_stream_and_nonstream_capture_before_and_after_card_and_event_conversion(self):
        raw = ': keepalive\n\n' + event('tool_call_start', {'data': {'name': '查航班'}})
        raw += event('thinking_delta', {'data': '公开测试思考'})
        raw += event('tool_result_end', {'data': {'content': '业务结果：T3'}})
        raw += complete('正文<jump_card>{"pageId":"test"}</jump_card>尾文')
        for streaming in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                response, rows, payload = await self.exercise(directory, streaming=streaming, raw=raw)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(next(r for r in rows if r['stage']=='platform_request')['body'], payload)
                sent = next(r for r in rows if r['stage']=='upstream_request')
                self.assertEqual(sent['body'], self.seen[0])
                self.assertEqual(sent['history_mode'], 'packed_text')
                self.assertIn('它从哪个航站楼出发？', sent['body']['messages'][0]['content'])
                self.assertTrue(sent['body']['stream'])
                self.assertEqual('\n'.join(r['text'] for r in rows if r['stage']=='upstream_sse_line')+'\n', raw)
                self.assertEqual(''.join(r['text'] for r in rows if r['stage']=='platform_response_body'), response.text)
                text = stream_text(response) if streaming else response.json()['choices'][0]['message']['content']
                self.assertIn('业务结果：T3', text)
                self.assertIn('正文尾文', text)
                self.assertNotIn('jump_card', response.text)
                self.assertTrue(any(r['stage']=='upstream_done' for r in rows))
                self.assertTrue(rows[-1]['response_sent_complete'])
                self.assertEqual({r['request_id'] for r in rows}, {response.headers['x-request-id']})
                self.assertEqual([r['seq'] for r in rows], list(range(1, len(rows)+1)))
                raw_log = ''.join(p.read_text() for p in Path(directory).glob('*.jsonl'))
                for secret in ('private-proxy-key', 'private-upstream-key', 'private-client-cookie', 'private-cookie'):
                    self.assertNotIn(secret, raw_log)
                self.assertEqual(stat.S_IMODE(Path(directory).stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(next(Path(directory).glob('*.jsonl')).stat().st_mode), 0o600)

    async def test_wait_notice_and_json_whitespace_are_captured_as_actually_sent(self):
        for streaming, path in [(True, '/v1/chat/completions'), (False, '/v1/keepalive/chat/completions')]:
            with tempfile.TemporaryDirectory() as directory:
                response, rows, _ = await self.exercise(directory, streaming, path, delay=0.055)
                self.assertEqual(''.join(r['text'] for r in rows if r['stage']=='platform_response_body'), response.text)
                notices = [r for r in rows if r['stage']=='proxy_wait_notice']
                self.assertEqual(len(notices), int(streaming))
                if streaming:
                    self.assertIn('正在处理，请稍等。', response.text)
                else:
                    self.assertTrue(response.text.startswith('\n'))
                    self.assertEqual(response.json()['choices'][0]['message']['content'], '首都 T3')

    async def test_errors_and_incomplete_source_are_recorded_without_fabricating_done(self):
        for streaming in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                response, rows, _ = await self.exercise(directory, streaming, raw='data: {broken-json}\n\n')
                self.assertIn('invalid_upstream_json', response.text)
                self.assertIn('{broken-json}', ''.join(r['text'] for r in rows if r['stage']=='upstream_sse_line'))
                self.assertFalse(any(r['stage']=='upstream_done' for r in rows))
                self.assertEqual(''.join(r['text'] for r in rows if r['stage']=='platform_response_body'), response.text)
            with tempfile.TemporaryDirectory() as directory:
                response, rows, _ = await self.exercise(directory, streaming, raw='{"message":"source failed"}', status=502)
                self.assertEqual(next(r for r in rows if r['stage']=='upstream_error_body')['text'], '{"message":"source failed"}')
                self.assertIn('source failed', response.text)

    async def test_disabled_or_unauthorized_requests_do_not_create_conversation_files(self):
        for enabled, authorized in [(False, True), (True, False)]:
            with tempfile.TemporaryDirectory() as directory:
                response, rows, _ = await self.exercise(directory, enabled=enabled, authorized=authorized)
                self.assertFalse(rows)
                self.assertEqual(response.status_code, 200 if authorized else 401)

    async def test_unwritable_capture_does_not_change_model_response(self):
        with tempfile.TemporaryDirectory() as directory:
            file = Path(directory)/'not-a-directory'
            file.write_text('test')
            response, _, _ = await self.exercise(str(file), raw=complete('仍然回答'))
            self.assertEqual(response.json()['choices'][0]['message']['content'], '仍然回答')

    async def test_limit_is_explicit_and_retention_preserves_active_files(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = ConversationTrace(directory, 'active', max_bytes=4096)
            trace.record('platform_request', body='small')
            for _ in range(20):
                trace.record('upstream_sse_line', text='大'*300)
            trace.close()
            rows = [json.loads(s) for s in trace.path.read_text().splitlines()]
            self.assertEqual(rows[-1]['stage'], 'capture_truncated')
            self.assertLess(trace.path.stat().st_size, 4600)
            active = ConversationTrace(directory, 'still-active')
            active.record('platform_request', body='active')
            old = time.time()-5*86400
            os.utime(trace.path, (old,old))
            os.utime(active.path, (old,old))
            lock = Path(directory)/'.cleanup.lock'
            os.utime(lock, (old,old))
            ConversationTrace.cleanup(Path(directory), 3, 256*1024*1024)
            self.assertFalse(trace.path.exists())
            self.assertTrue(active.path.exists())
            active.close()

    async def test_capture_reassembles_utf8_and_redacts_a_split_key(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = ConversationTrace(directory, 'split', secrets=['private-key'])
            body = ResponseBodyCapture(trace)
            raw = '中文 Bearer private-key\n尾文'.encode()
            for byte in raw:
                body.feed(bytes([byte]))
            body.feed(b'', final=True)
            trace.close()
            rows = [json.loads(s) for s in trace.path.read_text().splitlines()]
            self.assertEqual(''.join(row['text'] for row in rows), '中文 Bearer [REDACTED]\n尾文')


if __name__ == '__main__':
    unittest.main()
