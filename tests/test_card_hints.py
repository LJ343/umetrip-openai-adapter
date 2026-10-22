import asyncio
import json
from pathlib import Path
import sys
import time
import unittest
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.card_hints import CardHintFilter
from app import main as proxy


class HintGrammarTests(unittest.TestCase):
    def check_all_splits(self, source, expected):
        for parts in ([source], list(source), *([source[:i], source[i:]] for i in range(len(source) + 1))):
            removed = []
            f = CardHintFilter(on_delete=lambda **fields: removed.append(fields), clock=lambda: 1)
            output = ''.join(f.feed(part) for part in parts) + f.flush(complete=True)
            self.assertEqual(output, expected, (source, parts))
            rebuilt = source
            for deletion in reversed(removed):
                self.assertEqual(source[deletion['start_char']:deletion['end_char']], deletion['text'])
                rebuilt = rebuilt[:deletion['start_char']] + rebuilt[deletion['end_char']:]
            self.assertEqual(output, rebuilt)

    def test_ui_variants_and_mixed_parenthetical_constraints(self):
        examples = [
            ('如需购票，可点击下方卡片进入机票预订页。', ''),
            ('您可点击下方卡片查看机场天气详情：', ''),
            ('航班动态详情卡片如下：', ''),
            ('您点击推荐航班即可预订，也可以查看更多航班。', ''),
            ('您可以点击下方卡片进入航班动态查询页面，按航班号或起降地进一步查询：', ''),
            ('下面为您展示可点击的航班动态列表，可查看各航班实时状态：', ''),
            ('如需直达舱位详情，可点击下方卡片查看预订。', ''),
            ('如需查看或预订，可点击下方卡片进入：', ''),
            ('以上为可查询到的航班信息，如需查看更全的航班或票价详情，可点击下方卡片进入航班查询页：', ''),
            ('点击下方卡片即可查看舱位并预订（仅查询，不自动下单）。', '（仅查询，不自动下单）。'),
            ('点击下方卡片查看航班（含税1010元，需人工确认）。', '（含税1010元，需人工确认）。'),
            ('MU6788，11:10起飞。详情请点击下方卡片。', 'MU6788，11:10起飞。'),
            ('MU6788，11:10起飞。您可以点击下方卡片查看 MU6788 的完整航班动态：', 'MU6788，11:10起飞。'),
            ('广州—成都航班已查到。需要的话可以点下方卡片查看更多广州—成都的航班：', '广州—成都航班已查到。'),
            ('你可以点开下面的航班卡片查看详情。', ''),
            ('这趟是当天唯一的经济舱直飞，点击下方卡片即可查看详情或预订。', '这趟是当天唯一的经济舱直飞。'),
            ('您可以点击下方卡片进入航班动态查询页，自行查看最新航线安排：', ''),
            ('含税1010元，点击下方卡片查看航班（仅查询，不自动下单）。', '含税1010元（仅查询，不自动下单）。'),
            ('全季酒店约242元，也在300元档，可在下方卡片中查看。', '全季酒店约242元，也在300元档。'),
            ('点击推荐酒店可查看详情房型报价，或查看更多酒店。', ''),
            ('您可轻触下面的机票卡片查看详情。', ''),
            ('您只查询不预订，如需进一步查看舱位或预订可点击下方卡片。', '您只查询不预订。'),
            ('点击下方卡片可查看这两家详情（仅查询，不下单）。', '（仅查询，不下单）。'),
        ]
        for source, expected in examples:
            with self.subTest(source=source):self.check_all_splits(source, expected)

    def test_business_uncertainty_quotes_code_and_structured_content_unchanged(self):
        examples = [
            '暂时查不到航班，建议换个日期。',
            '您可以点击下方卡片查看 MU6788 的完整航班动态：',
            '需要的话可以点下方卡片查看更多广州—成都的航班：', '我正在查询航班信息。',
            '如需退票，起飞前收取票价的5%。', '您可以在航旅纵横App内完成退票。',
            '您可以点击下方卡片查看MU6788，含税1010元。',
            '请勿点击下方卡片，订单尚未确认。', '卡片中价格不含税，请以付款页为准。',
            '这趟是当天唯一的经济舱直飞，点击下方卡片即可查看详情或预订，但票价可能变化。',
            '含税1010元，可点击下方卡片查看新航班MU6788。',
            '查询成功，价格为1010元，起飞11:10。',
            '全季酒店约242元，可在下方卡片中查看，但需先确认退款条件。',
            '点击推荐酒店可查看详情房型报价，报价不含税。',
            '您只查询不预订，如需进一步查看舱位或预订可点击下方卡片，但需要先确认身份。',
            '如需购票，可点击下方卡片进入机票预订页，但该航班已取消。',
            '您可点击下方卡片查看机场天气详情吗？',
            '您可点击下方卡片查看机场天气详情，不过机场当前关闭。',
            '如需查看或预订，可点击下方卡片进入退款规则：',
            '如需购票，可点击下方卡片进入机票预订页，订单未确认。',
            '> 如需购票，可点击下方卡片进入机票预订页。',
            '示例：“如需购票，可点击下方卡片进入机票预订页。”',
            '"这是引用。\n如需购票，可点击下方卡片进入机票预订页。\n"',
            '“这是引用。\n如需购票，可点击下方卡片进入机票预订页。\n”',
            '```text\n如需购票，可点击下方卡片进入机票预订页。\n```',
            '~~~text\n如需购票，可点击下方卡片进入机票预订页。\n~~~',
            '{\n "content": "如需购票，可点击下方卡片进入机票预订页。"\n}',
            '["如需购票，可点击下方卡片进入机票预订页。"]',
            '| 描述 | 如需购票，可点击下方卡片进入机票预订页。 |',
            '现在获取跳转页面参数，准备出卡片。',
        ]
        for source in examples:
            with self.subTest(source=source):self.check_all_splits(source, source)

    def test_boundaries_after_tables_and_code(self):
        for prefix in ('| 航班 | 价格 |\n| MU6788 | 1010 |\n\n', '```\n说明\n```\n\n'):
            self.check_all_splits(prefix + '如需购票，可点击下方卡片进入机票预订页。', prefix)

    def test_deadline_and_overflow_release_entire_candidate(self):
        clock = [1.0]
        f = CardHintFilter(max_wait=.2, clock=lambda:clock[0])
        self.assertEqual(f.feed('您可以点击'), '')
        clock[0] += .201
        self.assertEqual(f.expire(), '您可以点击')
        self.assertEqual(f.feed('下方卡片查看航班。'), '下方卡片查看航班。')
        f = CardHintFilter(max_chars=16, clock=lambda:1)
        source='您可以点击'+'普通业务文字'*30+'。'
        self.assertEqual(f.feed(source)+f.flush(True), source)
        self.assertEqual(f.overflows, 1)
        f = CardHintFilter(max_wait=.2, clock=lambda:clock[0])
        self.assertEqual(f.feed('如需购票'), '')
        clock[0] += .201
        self.assertEqual(f.expire(), '如需购票')
        self.assertEqual(f.feed('，可点击下方卡片进入机票预订页。'), '，可点击下方卡片进入机票预订页。')


def packet(content=None, finish=None, **delta):
    if content is not None:delta['content']=content
    return proxy.sse({'choices':[{'index':0,'delta':delta,'finish_reason':finish}]})


class TimedStream(httpx.AsyncByteStream):
    def __init__(self, items):self.items=items;self.closed=False
    async def __aiter__(self):
        for delay, value in self.items:
            await asyncio.sleep(delay)
            yield value
    async def aclose(self):self.closed=True


class HintIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def collect(self, items, **options):
        stream=TimedStream(items)
        response=httpx.Response(200, stream=stream)
        chunks=[]
        with patch.multiple(proxy, STRIP_CARD_HINTS=True, CARD_HINT_MAX_WAIT_MS=60):
            converter=proxy.UpstreamStream(response,'test','qianrang',**options)
            async for _, chunk in converter.chunks():chunks.append(chunk)
        return chunks

    def contents(self, chunks):
        return ''.join(c['delta'].get('content','') for x in chunks for c in x['choices'])

    async def test_deadline_fires_while_upstream_is_silent(self):
        stream=TimedStream([(0, packet('您可以点击')), (.3, packet('下方卡片查看航班。')),
                            (0, packet(finish='stop')+b'data: [DONE]\n\n')])
        with patch.multiple(proxy, STRIP_CARD_HINTS=True, CARD_HINT_MAX_WAIT_MS=60):
            converter=proxy.UpstreamStream(httpx.Response(200,stream=stream),'test','qianrang')
            began=time.monotonic();iterator=converter.chunks()
            first=await asyncio.wait_for(iterator.__anext__(), .2)
            elapsed=time.monotonic()-began
            rest=[chunk async for chunk in iterator]
        self.assertLess(elapsed,.18)
        self.assertGreater(elapsed,.04)
        self.assertEqual(self.contents([first[1]]+[x[1] for x in rest]),'您可以点击下方卡片查看航班。')
        self.assertEqual(converter._hints.timeouts,1)

    async def test_reasoning_business_and_native_tool_arguments_are_preserved(self):
        hint='如需购票，可点击下方卡片进入机票预订页。'
        business={'content':hint,'price':1010}
        event=b'event: tool_result_end\ndata: '+json.dumps({'data':business},ensure_ascii=False).encode()+b'\n\n'
        chunks=await self.collect([(0,packet(hint, reasoning_content=hint)),(0,event),
             (0,packet(tool_calls=[{'index':0,'id':'call','type':'function','function':{'name':'lookup','arguments':json.dumps({'note':hint},ensure_ascii=False)}}])),
             (0,packet(finish='tool_calls')+b'data: [DONE]\n\n')])
        self.assertEqual(self.contents(chunks),'\n\n'+json.dumps(business,ensure_ascii=False,indent=2)+'\n\n')
        self.assertEqual(''.join(c['delta'].get('reasoning_content','') for x in chunks for c in x['choices']),hint)
        self.assertIn(hint, ''.join(c['delta']['tool_calls'][0]['function']['arguments'] for x in chunks for c in x['choices'] if c['delta'].get('tool_calls')))

    async def test_structured_request_bypass_and_toggle_off(self):
        hint='如需购票，可点击下方卡片进入机票预订页。'
        chunks=await self.collect([(0,packet(hint)),(0,packet(finish='stop')+b'data: [DONE]\n\n')],allow_hint_filter=False)
        self.assertEqual(self.contents(chunks),hint)
        with patch.object(proxy,'STRIP_CARD_HINTS',False):
            converter=proxy.UpstreamStream(httpx.Response(200,content=packet(hint)+packet(finish='stop')+b'data: [DONE]\n\n'),'test','qianrang')
            self.assertIsNone(converter._hints)

    async def test_unconfirmed_tail_survives_disconnect_and_error(self):
        for final in (b'',b'event: error\ndata: {"message":"failure"}\n\n'):
            chunks=[]
            with patch.object(proxy,'STRIP_CARD_HINTS',True):
                source=proxy.UpstreamStream(httpx.Response(200,content=packet('您可以查看')+final),'test','qianrang')
                with self.assertRaises(proxy.UpstreamStreamError):
                    async for _, chunk in source.chunks():chunks.append(chunk)
            self.assertEqual(self.contents(chunks),'您可以查看')


if __name__=='__main__':unittest.main()
