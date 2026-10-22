"""Holdout wording, business qualifiers and randomized SSE segmentation."""
import itertools
import random
import unittest

from app.card_hints import CardHintFilter


class CardHintBoundaryTests(unittest.TestCase):
    def verify(self, source, expected):
        rng = random.Random(20261022)
        splits = [[source], list(source)]
        for _ in range(4):
            points = sorted({0, len(source), *(rng.randrange(len(source) + 1) for _ in range(5))})
            splits.append([source[a:b] for a, b in zip(points, points[1:])])
        for parts in splits:
            removals = []
            filt = CardHintFilter(clock=lambda: 1, on_delete=lambda **v: removals.append(v))
            actual = ''.join(filt.feed(p) for p in parts) + filt.flush(True)
            self.assertEqual(actual, expected, (source, parts))
            last = 0
            kept = []
            for item in removals:
                a, b = item['start_char'], item['end_char']
                self.assertGreaterEqual(a, last)
                self.assertEqual(source[a:b], item['text'])
                kept.append(source[last:a]);last = b
            self.assertEqual(''.join(kept) + source[last:], actual)

    def test_combinations_of_pure_navigation(self):
        subjects = ('', '请', '您可以', '你可以', '您也可以', '您还可以', '也可以', '还可以')
        verbs = ('点击', '直接点击', '点开', '轻触', '通过点击')
        refs = ('下方卡片', '下面的航班卡片', '下方的机票卡片', '这张卡片')
        actions = ('查看详情', '查看航班时刻和票价', '查看航班时刻、票价及余票', '查看这些航班的详情')
        for subject, verb, ref, action in itertools.product(subjects, verbs, refs, actions):
            self.verify(subject + verb + ref + action + '。', '')

    def test_more_navigation_and_mixed_facts(self):
        for source, expected in [
            ('如需了解具体票价及舱位，可点击下方航班卡片查看详情。', ''),
            ('请通过下方卡片查看航班详情。', ''),
            ('请参考下方卡片。', ''),
            ('具体航班详情请查看下方卡片。', ''),
            ('更多航班信息可在下方卡片查看。', ''),
            ('详情请见下方的航班卡片。', ''),
            ('以下是相关航班卡片：', ''),
            ('含税1050元,点击下方卡片查看航班详情。', '含税1050元。'),
            ('点击下方卡片查看航班详情（含税1050元，非免费退改）。', '（含税1050元，非免费退改）。'),
            ('点击下方卡片查看详情（唯一可免费退改的航班），需要先登录。', '（唯一可免费退改的航班），需要先登录。'),
            ('广州到成都有航班。需要的话可以点下方卡片查看更多广州—成都的航班：', '广州到成都有航班。'),
        ]:
            self.verify(source, expected)

    def test_pure_navigation_before_explicit_no_order_status(self):
        for status in ('暂不预订', '仅查询', '不下单', '未下单', '不自动下单', '仅查询，不下单', '只查询，不预订'):
            for comma in ('，', ','):
                self.verify('您可按需点击下方卡片查看详情' + comma + status + '。', status + '。')
        self.verify('您可按需点击下方卡片查看详情。', '')
        for source in ('您可按需点击下方卡片查看详情，但订单已提交。', '您可按需点击下方卡片查看详情，未下单但已锁座。'):
            self.verify(source, source)

    def test_route_relationship_and_complete_flight_identifier(self):
        for source in (
            '广州到北京，上海到成都。需要的话可以点下方卡片查看更多广州—成都的航班：',
            '成都到广州有航班。需要的话可以点下方卡片查看更多广州—成都的航班：',
            'MU67880是错误示例。您可以点击下方卡片查看 MU6788 的完整航班动态：',
            'XMU6788不是有效编号。您可以点击下方卡片查看 MU6788 的完整航班动态：',
        ):
            self.verify(source, source)

    def test_business_qualifiers_and_unknown_conditions(self):
        conditions = ('如果需要免费退改', '如需儿童票', '若需轮椅服务', '如想托运宠物', '需要先核实身份', '想要机场接送')
        for condition in conditions:
            for comma in ('，', ','):
                source = condition + comma + '请点击下方卡片查看详情。'
                self.verify(source, source)
        qualifiers = ('但该航班已取消', '价格实时变动', '不含机场建设费', '需要先确认退改规则', '该票不支持退票', '仅剩两张票', '需要实名认证', '须在起飞前两小时办理', '已为您提交申请', '此操作需要支付费用')
        for qualifier in qualifiers:
            for comma in ('，', ','):
                source = '点击下方卡片查看航班详情' + comma + qualifier + '。'
                self.verify(source, source)
        for source in (
            '点击下方卡片查看已取消航班。',
            '点击下方卡片查看需补税的票价。',
            '请点击下方卡片，先核对退改规则再支付。',
            '暂时查不到嘉兴到西安的航班。',
            '这个问题暂时帮不上忙。',
            '该航班无余票，请更换日期。',
            '您也可以点击下方卡片查看1010元的航班。',
            '点击下方卡片查看详情是否会收费？',
            '您可以点击链接https://example.com查看航班。',
            '您可以在航旅纵横App中查询退改签条件。',
            '点击下方卡片查看详情，订单号ABC123。',
            '请勿点击下方卡片。',
            '无需点击下方卡片。',
            '现在获取跳转页面参数，准备出卡片。',
        ):
            self.verify(source, source)

    def test_protected_formatting_and_literals(self):
        hint = '您也可以点击下方的航班卡片查看详情。'
        for source in (
            '```text\n' + hint + '\n```',
            '> ' + hint,
            '| 字段 | 示例 |\n| 说明 | ' + hint + ' |',
            '“这是模型原话。\n' + hint + '\n”',
            '{"text": "' + hint + '"}',
            '["' + hint + '"]',
            '- ' + hint,
            '**' + hint + '**',
        ):
            self.verify(source, source)

    def test_overflow_timeout_and_following_sentence(self):
        clock = [1.0]
        filt = CardHintFilter(max_wait=.2, clock=lambda: clock[0])
        self.assertEqual(filt.feed('您也可以点击'), '')
        clock[0] += .201
        first = filt.expire()
        rest = '下方卡片查看详情。如需购票，可点击下方卡片进入机票预订页。'
        actual = first + filt.feed(rest) + filt.flush(True)
        self.assertEqual(actual, '您也可以点击下方卡片查看详情。')
        self.assertEqual(filt.timeouts, 1)


if __name__ == '__main__':
    unittest.main()
