"""Fault injection: voice completion, strict errors, safe reconnect and deadlines."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main as proxy
from test_preservation import ByteStream, complete, packet, stream_text, chunks


class BrokenStream(httpx.AsyncByteStream):
    def __init__(self, prefix='', delay=0):
        self.prefix, self.delay, self.closed = prefix, delay, False

    async def __aiter__(self):
        if self.prefix:
            yield self.prefix.encode()
        if self.delay:
            await asyncio.sleep(self.delay)
        raise httpx.RemoteProtocolError('incomplete chunked read')

    async def aclose(self):
        self.closed = True


class ResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, handler, path='/v1/keepalive/chat/completions', streaming=True, extra=None, updates=None):
        config = dict(PROXY_API_KEYS={'test'}, UPSTREAM_API_KEY='source', VOICE_ERROR_FALLBACK_ENABLED=True,
                      HEARTBEAT_INTERVAL=0.01, STREAM_WAIT_AFTER=0.02,
                      UPSTREAM_CONNECT_RETRIES=1, UPSTREAM_CONNECT_RETRY_DELAY=0,
                      UPSTREAM_TOTAL_TIMEOUT=1, JSON_KEEPALIVE_AFTER=0.01, JSON_KEEPALIVE_INTERVAL=0.01)
        config.update(updates or {})
        body = {'model': 'qianrang', 'stream': streaming,
                'messages': [{'role': 'user', 'content': '查询航班'}], **(extra or {})}
        with patch.multiple(proxy, **config):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
                proxy.app.state.client = upstream
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url='http://proxy') as client:
                    return await client.post(path, headers={'Authorization': 'Bearer test'}, json=body)

    async def test_voice_failure_is_spoken_and_done_but_trace_marks_source_failed(self):
        calls = 0
        stream = BrokenStream(delay=0.05)
        async def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, stream=stream)
        with tempfile.TemporaryDirectory() as directory:
            response = await self.request(handler, updates={'CONVERSATION_LOG_ENABLED': True, 'CONVERSATION_LOG_DIR': directory})
            text = stream_text(response)
            self.assertIn('正在处理，请稍等。', text)
            self.assertIn('本次未能取得完整结果', text)
            self.assertIn('核实办理状态', text)
            self.assertEqual(calls, 1)  # Read-only wording is never guessed as replay authorization.
            self.assertTrue(response.text.endswith('data: [DONE]\n\n'))
            self.assertEqual(chunks(response)[-1]['choices'][0]['finish_reason'], 'stop')
            self.assertFalse(any(item.get('error') for item in chunks(response)))
            rows=[json.loads(s) for p in Path(directory).glob('*.jsonl') for s in p.read_text().splitlines()]
            fallback=next(row for row in rows if row['stage']=='proxy_voice_failure')
            self.assertFalse(fallback['source_complete'])
            self.assertFalse(fallback['partial_answer'])
            self.assertFalse(any(row['stage']=='upstream_done' for row in rows))
        self.assertTrue(stream.closed)

    async def test_partial_answer_is_kept_once_and_labeled_incomplete_without_replay(self):
        stream=BrokenStream(packet('已查到部分信息。'))
        calls=0
        async def handler(request):
            nonlocal calls
            calls+=1
            return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=stream)
        response=await self.request(handler)
        self.assertTrue(stream_text(response).startswith('已查到部分信息。'))
        self.assertIn('以上回答尚未完整返回', stream_text(response))
        self.assertEqual(stream_text(response).count('已查到部分信息。'),1)
        self.assertEqual(calls,1)

    async def test_each_conversation_records_transport_failure_for_both_modes(self):
        for streaming in (True, False):
            async def handler(request):
                return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                      stream=BrokenStream(packet('部分结果')))
            with tempfile.TemporaryDirectory() as directory:
                response=await self.request(handler, streaming=streaming,
                    updates={'CONVERSATION_LOG_ENABLED': True, 'CONVERSATION_LOG_DIR': directory})
                rows=[json.loads(s) for p in Path(directory).glob('*.jsonl') for s in p.read_text().splitlines()]
                errors=[row for row in rows if row['stage']=='upstream_stream_error']
                self.assertEqual(len(errors),1)
                self.assertEqual(errors[0]['error_type'],'RemoteProtocolError')
                self.assertEqual(errors[0]['code'],'upstream_error')
                self.assertFalse(errors[0]['source_complete'])
                self.assertNotIn('incomplete chunked read',json.dumps(errors))
                if streaming:
                    self.assertTrue(stream_text(response).startswith('部分结果'))
                    self.assertIn('[DONE]',response.text)
                else:
                    self.assertEqual(response.json()['error']['code'],'upstream_error')

    async def test_each_conversation_distinguishes_deadline_from_incomplete_source(self):
        for streaming in (True, False):
            for deadline in (True, False):
                async def handler(request):
                    if deadline:
                        return httpx.Response(200,headers={'content-type':'text/event-stream'},
                            stream=BrokenStream(packet('部分结果'),delay=0.2))
                    return httpx.Response(200,headers={'content-type':'text/event-stream'},
                        stream=ByteStream(packet('部分结果')))
                with tempfile.TemporaryDirectory() as directory:
                    await self.request(handler,streaming=streaming,updates={
                        'CONVERSATION_LOG_ENABLED':True,'CONVERSATION_LOG_DIR':directory,
                        'UPSTREAM_TOTAL_TIMEOUT':0.04 if deadline else 1})
                    rows=[json.loads(s) for p in Path(directory).glob('*.jsonl') for s in p.read_text().splitlines()]
                    errors=[row for row in rows if row['stage']=='upstream_stream_error']
                    self.assertEqual(len(errors),1)
                    self.assertEqual(errors[0]['code'],'upstream_total_timeout' if deadline else 'incomplete_stream')
                    self.assertFalse(errors[0]['source_complete'])

    async def test_original_route_and_disabled_voice_policy_keep_strict_error_contract(self):
        for path,enabled in [('/v1/chat/completions',True),('/v1/keepalive/chat/completions',False)]:
            async def handler(request):
                return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=BrokenStream())
            response=await self.request(handler,path=path,updates={'VOICE_ERROR_FALLBACK_ENABLED':enabled})
            self.assertEqual(chunks(response)[-1]['error']['code'],'upstream_error')
            self.assertNotIn('[DONE]',response.text)
            self.assertNotIn('本次未能取得完整结果',response.text)

    async def test_json_audio_and_partial_native_tool_call_never_get_natural_language_fallback(self):
        cases=[({'response_format':{'type':'json_object'}},''),({'modalities':['audio']},''),
               ({},packet(delta={'tool_calls':[{'index':0,'id':'call','type':'function','function':{'name':'lookup','arguments':'{'}}]}))]
        for extra,prefix in cases:
            async def handler(request):
                return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=BrokenStream(prefix))
            response=await self.request(handler,extra=extra)
            self.assertIn('error',chunks(response)[-1])
            self.assertNotIn('[DONE]',response.text)
            self.assertNotIn('本次未能取得完整结果',response.text)

    async def test_successful_voice_output_has_no_failure_text_and_usage_ids_are_valid(self):
        async def handler(request):
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=ByteStream(complete('完整原文')))
        response=await self.request(handler,extra={'stream_options':{'include_usage':True}})
        self.assertEqual(stream_text(response),'完整原文')
        data=chunks(response)
        self.assertEqual(len({obj['id'] for obj in data}),1)
        self.assertEqual(len({obj['created'] for obj in data}),1)
        self.assertTrue(all('usage' in obj for obj in data))

    async def test_http_errors_voice_finish_without_replaying_and_no_raw_sensitive_error(self):
        for status in (401,429,502,504):
            calls=0
            async def handler(request):
                nonlocal calls
                calls+=1
                return httpx.Response(status,json={'message':'sensitive source exception'})
            response=await self.request(handler)
            self.assertEqual(calls,1)
            self.assertIn('[DONE]',response.text)
            self.assertNotIn('sensitive source exception',response.text)
            self.assertTrue(stream_text(response))

    async def test_preconnect_failure_reconnects_once_with_same_body(self):
        for failure in (httpx.ConnectError('network'),httpx.ConnectTimeout('network'),httpx.PoolTimeout('pool')):
            seen=[]
            async def handler(request):
                seen.append(bytes(request.content))
                if len(seen)==1:raise failure
                return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=ByteStream(complete('连接已恢复')))
            response=await self.request(handler)
            self.assertEqual(stream_text(response),'连接已恢复')
            self.assertEqual(len(seen),2)
            self.assertEqual(seen[0],seen[1])

    async def test_read_write_and_protocol_errors_are_not_replayed(self):
        for failure in (httpx.ReadTimeout('read'),httpx.WriteTimeout('write'),httpx.WriteError('write'),httpx.RemoteProtocolError('peer')):
            calls=0
            async def handler(request):
                nonlocal calls
                calls+=1
                raise failure
            response=await self.request(handler)
            self.assertEqual(calls,1)
            self.assertIn('[DONE]',response.text)

    async def test_total_deadline_cancels_source_and_finishes_voice_failure(self):
        closed=asyncio.Event()
        async def handler(request):
            try:await asyncio.Event().wait()
            finally:closed.set()
        response=await self.request(handler,updates={'UPSTREAM_TOTAL_TIMEOUT':0.04})
        self.assertTrue(closed.is_set())
        self.assertIn('响应超时',stream_text(response))
        self.assertIn('[DONE]',response.text)

    async def test_disconnect_during_reconnect_delay_cancels_without_a_second_request(self):
        entered=asyncio.Event()
        calls=0
        async def handler(request):
            nonlocal calls
            calls+=1
            entered.set()
            raise httpx.ConnectError('not connected')
        with patch.multiple(proxy,UPSTREAM_CONNECT_RETRIES=1,UPSTREAM_CONNECT_RETRY_DELAY=0.2):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                request=client.build_request('POST','http://upstream/chat/completions',json={'stream':True})
                response=proxy.streaming_response(client,request,'cancel-reconnect','qianrang',proxy.time.monotonic(),False,voice_errors=True)
                await response.body_iterator.__anext__()
                await asyncio.wait_for(entered.wait(),0.3)
                await response.body_iterator.aclose()
                self.assertEqual(calls,1)

    async def test_nonstream_deadline_returns_real_error_and_closes_partial_source(self):
        stream=BrokenStream(packet('半份数据'),delay=0.2)
        async def handler(request):
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=stream)
        for path in ('/v1/chat/completions','/v1/keepalive/chat/completions'):
            stream=BrokenStream(packet('半份数据'),delay=0.2)
            response=await self.request(handler,path=path,streaming=False,updates={'UPSTREAM_TOTAL_TIMEOUT':0.04})
            self.assertEqual(response.json()['error']['code'],'upstream_total_timeout')
            self.assertTrue(stream.closed)
            self.assertNotIn('choices',response.json())
            self.assertEqual(response.status_code,504 if '/keepalive/' not in path else 200)

    @unittest.skipUnless(importlib.util.find_spec('openai'),'SDK optional, install requirements-dev.txt')
    async def test_standard_sdk_parses_voice_failure_to_a_complete_failure_utterance(self):
        from openai import AsyncOpenAI
        async def handler(request):
            return httpx.Response(200,headers={'content-type':'text/event-stream'},stream=BrokenStream())
        with patch.multiple(proxy,PROXY_API_KEYS={'test'},UPSTREAM_API_KEY='source',VOICE_ERROR_FALLBACK_ENABLED=True):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
                proxy.app.state.client=upstream
                async with AsyncOpenAI(api_key='test',base_url='http://proxy/v1/keepalive',max_retries=0,
                    http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app))) as sdk:
                    stream=await sdk.chat.completions.create(model='qianrang',messages=[{'role':'user','content':'查航班'}],stream=True)
                    text=[];finishes=[]
                    async for item in stream:
                        for choice in item.choices:
                            text.append(choice.delta.content or '')
                            if choice.finish_reason:finishes.append(choice.finish_reason)
                    self.assertIn('本次未能取得完整结果',''.join(text))
                    self.assertEqual(finishes,['stop'])


if __name__=='__main__':unittest.main()
