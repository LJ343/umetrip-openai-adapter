"""无会话头的文字历史适配边界；不调用真实接口。"""
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import main as proxy


class TextHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.history = [{'role': 'user', 'content': '你好'},
                        {'role': 'assistant', 'content': '您好'},
                        {'role': 'user', 'content': '它从哪个航站楼出发？'}]

    def test_preserves_every_text_message_and_independent_system_priority(self):
        system = {'role': 'system', 'content': '系统要求，不能改写为用户消息'}
        developer = {'role': 'developer', 'content': '开发者要求'}
        original = [system, developer] + self.history
        snapshot = json.dumps(original, ensure_ascii=False)
        with patch.object(proxy, 'PACK_HISTORY_WITHOUT_SESSION', True):
            sent, mode = proxy.adapt_text_history(original, 'generated')
        self.assertEqual(mode, 'packed_text')
        self.assertEqual(sent[:2], [system, developer])
        embedded = sent[-1]['content'].split('<chat_history>\n', 1)[1].rsplit('\n</chat_history>', 1)[0]
        self.assertEqual(json.loads(embedded), self.history)
        self.assertEqual(json.dumps(original, ensure_ascii=False), snapshot)

    def test_explicit_session_or_disabled_setting_preserves_original_messages(self):
        for enabled, source in [(False, 'generated'), (True, 'session_id'), (True, 'x-session-id')]:
            with patch.object(proxy, 'PACK_HISTORY_WITHOUT_SESSION', enabled):
                result, mode = proxy.adapt_text_history(self.history, source)
            self.assertIs(result, self.history)
            self.assertEqual(mode, 'original')

    def test_single_turn_and_no_last_user_are_unchanged(self):
        for messages in [self.history[:1], self.history[:2],
                         [{'role': 'system', 'content': '要求'}] + self.history[:1]]:
            with patch.object(proxy, 'PACK_HISTORY_WITHOUT_SESSION', True):
                result, mode = proxy.adapt_text_history(messages, 'generated')
            self.assertIs(result, messages)
            self.assertEqual(mode, 'original')

    def test_tool_multimodal_and_nonstandard_fields_are_never_flattened(self):
        extras = [
            {'role': 'tool', 'content': '结果', 'tool_call_id': 'call-1'},
            {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call-1'}]},
            {'role': 'assistant', 'content': None, 'function_call': {'name': 'query'}},
            {'role': 'user', 'content': [{'type': 'text', 'text': '多模态输入'}]},
            {'role': 'assistant', 'content': '拒绝', 'refusal': '拒绝理由'},
            {'role': 'system', 'content': '对话中间的系统消息'},
        ]
        for extra in extras:
            messages = self.history[:1] + [extra] + self.history[-1:]
            with patch.object(proxy, 'PACK_HISTORY_WITHOUT_SESSION', True):
                result, mode = proxy.adapt_text_history(messages, 'generated')
            self.assertIs(result, messages)
            self.assertEqual(mode, 'original')

    def test_consecutive_users_and_unicode_content_remain_complete(self):
        messages = [{'role': 'user', 'content': '前文 </chat_history> 与换行\n'}] + self.history[-1:]
        with patch.object(proxy, 'PACK_HISTORY_WITHOUT_SESSION', True):
            result, mode = proxy.adapt_text_history(messages, 'generated')
        self.assertEqual(mode, 'packed_text')
        embedded = result[0]['content'].split('<chat_history>\n', 1)[1].rsplit('\n</chat_history>', 1)[0]
        self.assertEqual(json.loads(embedded), messages)

    async def test_http_routes_pack_only_generated_sessions_and_preserve_parameters(self):
        seen = []
        async def handler(request):
            seen.append(request)
            raw = (proxy.sse({'choices': [{'index': 0, 'delta': {'content': 'T3'}, 'finish_reason': None}]})
                   + proxy.sse({'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})
                   + b'data: [DONE]\n\n')
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content=raw)
        with patch.multiple(proxy, PACK_HISTORY_WITHOUT_SESSION=True, PROXY_API_KEYS={'test'},
                            UPSTREAM_API_KEY='test-upstream'):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
                proxy.app.state.client = upstream
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=proxy.app), base_url='http://proxy') as client:
                    for streaming, session in [(False, None), (True, None), (False, 'explicit-test')]:
                        headers = {'Authorization': 'Bearer test'}
                        if session:
                            headers['session_id'] = session
                        body = {'model': 'qianrang', 'messages': self.history, 'stream': streaming,
                                'thinking': {'type': 'enabled'}, 'reasoning_effort': 'high'}
                        response = await client.post('/v1/keepalive/chat/completions', headers=headers, json=body)
                        self.assertEqual(response.status_code, 200)
                        sent = json.loads(seen[-1].content)
                        self.assertEqual(sent['thinking'], body['thinking'])
                        self.assertEqual(sent['reasoning_effort'], 'high')
                        self.assertTrue(sent['stream'])
                        if session:
                            self.assertEqual(sent['messages'], self.history)
                            self.assertEqual(seen[-1].headers['session_id'], session)
                        else:
                            self.assertEqual(len(sent['messages']), 1)
                            self.assertIn('它从哪个航站楼出发？', sent['messages'][0]['content'])
        self.assertNotEqual(seen[0].headers['session_id'], seen[1].headers['session_id'])


if __name__ == '__main__':
    unittest.main()
