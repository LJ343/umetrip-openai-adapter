import copy
import json
import tempfile
import unittest
from datetime import date

from app.agent import Agent, AgentFailure
from app.contracts import Contract
from app.grounding import ground_reply_issue, request_context
from app.results import render_results, neutralize_visible_text
from app.tools import FlyAI, REGISTRY
from test_service import settings

TODAY = date(2026, 11, 11)


def history(*questions):
    return [{"role": "user", "content": q} for q in questions]


def train_item(day="2026-11-13", train="D2212", price="24.00", link="https://example.com/13-D2212"):
    return {"price": price, "jumpUrl": link, "journeys": [{"journeyType": "直达", "segments": [{
        "depCityName": "嘉兴", "depStationName": "嘉兴南站", "depStationCode": "EPH", "depDateTime": day + " 06:33:00",
        "arrCityName": "上海", "arrStationName": "上海虹桥站", "arrStationCode": "AOH", "arrDateTime": day + " 07:35:00",
        "marketingTransportNo": train, "seatClassName": "二等座"}]}]}


class ContractTests(unittest.TestCase):
    def test_long_outbound_return_and_repeated_search(self):
        qs = ["帮我查一下明天新疆飞往西安的航班", "乌鲁木齐出发，经济舱，直飞。", "只看下午出发的，先给两个便宜的。", "改后天，其他不变。", "换成嘉兴到北京。", "落地只看首都机场，给我一个。", "再查返程，从去程那天算，三天后回来。", "你好小德", "刚才返程是哪天？先别重新查价格。", "不对，我说的是嘉兴南湖机场，不要替我换上海，后天去北京首都。", "然后从北京首都返回嘉兴南湖，去程三天后，经济舱直飞。", "只看下午的，给一个，重新查。"]
        expected = {
            2: ("乌鲁木齐", "西安", "2026-11-12"),
            3: ("乌鲁木齐", "西安", "2026-11-12"),
            4: ("乌鲁木齐", "西安", "2026-11-13"),
            5: ("嘉兴南湖机场", "北京", "2026-11-13"),
            6: ("嘉兴南湖机场", "北京首都机场", "2026-11-13"),
            7: ("北京首都机场", "嘉兴南湖机场", "2026-11-16"),
            10: ("嘉兴南湖机场", "北京首都机场", "2026-11-13"),
            11: ("北京首都机场", "嘉兴南湖机场", "2026-11-16"),
            12: ("北京首都机场", "嘉兴南湖机场", "2026-11-16"),
        }
        for turn, values in expected.items():
            with self.subTest(turn=turn):
                c = Contract(history(*qs[:turn]), TODAY)
                self.assertEqual(tuple(c.conditions[k] for k in ("origin", "destination", "dep_date")), values)
                self.assertEqual(c.conditions["seat_class_name"], "经济舱")
        self.assertIsNotNone(Contract(history(qs[0]), TODAY).clarification())
        self.assertIsNone(Contract(history(*qs[:2]), TODAY).clarification())
        self.assertEqual(Contract(history(*qs[:9]), TODAY).no_lookup, "history")

    def test_local_ledger_does_not_replace_model_airport_arguments(self):
        h = history("后天嘉兴到北京的航班，经济舱直飞")
        h += [{"role": "assistant", "content": "嘉兴没有机场，给你改成上海虹桥，报价999元。"}]
        h += history("落地只看首都机场")
        args = Contract(h, TODAY).enforce("search_flight", {"origin": "上海虹桥", "destination": "北京", "dep_date": "2026-11-12"})
        self.assertEqual(args, {"origin": "上海虹桥", "destination": "北京", "dep_date": "2026-11-12"})

    def test_train_time_and_repeated_date_changes(self):
        qs = ["明天从嘉兴南去上海虹桥的高铁有吗？", "二等座，下午两点以后，便宜的给两个。", "改成后天，别的条件照旧。", "下午太晚了，换上午十点以前。", "换后天之后一天，还是上午十点前，其他不变，再查一次。"]
        c = Contract(history(*qs), TODAY)
        self.assertEqual(c.kind, "search_train")
        self.assertEqual(c.conditions, {"origin": "嘉兴南", "destination": "上海虹桥", "dep_date": "2026-11-14", "seat_class_name": "二等座", "dep_hour_start": 0, "dep_hour_end": 9, "sort_type": 3})

    def test_hotel_conditions_and_result_count_not_price(self):
        qs = ["嘉兴南湖区的酒店", "明天入住，后天退房，两个人，南湖附近，三百以内，给两个。", "换成双床，其他不变。", "入住往后挪一天，住两晚。", "预算加到五百，住得离南湖近一点。", "改看杭州西湖附近，同样日期和床型，最多两个。"]
        c = Contract(history(*qs), TODAY)
        self.assertEqual(c.conditions["check_in_date"], "2026-11-13")
        self.assertEqual(c.conditions["check_out_date"], "2026-11-15")
        self.assertEqual(c.conditions["max_price"], 500)
        self.assertEqual(c.conditions["hotel_bed_types"], "双床房")
        self.assertEqual(c.limit, 2)

    def test_hotel_dates_are_not_recomputed_by_local_ledger(self):
        c = Contract(history("换杭州，2026-11-18到，玩三天，两个人，酒店每晚500以内"), TODAY)
        args = c.enforce("search_hotel", {"dest_name": "杭州", "check_in_date": "2026-11-18", "check_out_date": "2026-11-21"})
        self.assertEqual(args["check_out_date"], "2026-11-21")
        text, _ = render_results([{"tool": "search_hotel", "arguments": args, "ok": True, "result": {"data": {"itemList": []}}}])
        self.assertIn("2026-11-21离店，3晚", text)
        self.assertNotIn(c.assumption, text)

    def test_marriott_brand_date_bed_and_budget_survive_city_change(self):
        c = Contract(history("上海万豪酒店，2026-11-18入住，2026-11-20退房，每晚1000以内", "只看万怡，双床，给两个", "换杭州，日期不变，预算改成800"), TODAY)
        self.assertEqual(c.kind, "search_marriott_hotel")
        self.assertEqual(c.conditions["hotel_brands"], "万怡")
        self.assertEqual(c.conditions["hotel_bed_types"], "双床房")
        self.assertEqual(c.conditions["max_price"], 800)

    def test_negated_hotel_keeps_ground_route_policy(self):
        h = history("嘉兴南湖机场到中国航信怎么走", "只说公交怎么换乘，别推荐酒店")
        self.assertEqual(request_context(h)[0], "jiaxing_ground")
        self.assertIsNone(ground_reply_issue("目前不确定是否无需换乘，请以实时地图为准。"))
        self.assertEqual(ground_reply_issue("公交并非直接到达1177号"), "unverified_no_direct_transit")

    def test_plain_jiaxing_flight_also_receives_airport_facts(self):
        self.assertIn("JNH", request_context(history("明天嘉兴到北京的航班"))[1])

    def test_optional_empty_fields_and_unknown_limit_do_not_break_search(self):
        c = Contract(history("后天嘉兴到北京的航班，给一个"), TODAY)
        args = c.enforce("search_flight", {"origin": "上海", "destination": "北京", "dep_date": "2026-11-12", "transport_no": "", "limit": "1", "max_price": 2000})
        self.assertEqual(args, {"origin": "上海", "destination": "北京", "dep_date": "2026-11-12", "max_price": 2000})

    def test_marriott_queries_are_not_rewritten_by_local_classification(self):
        c = Contract(history("查上海的万豪酒店套餐，便宜的给两个"), TODAY)
        self.assertEqual(c.enforce("search_marriott_package", {"keyword": "上海 万豪", "sort_type": "price_asc"})["keyword"], "上海 万豪")
        c = Contract(history("想看看上海万豪集团旗下的酒店"), TODAY)
        self.assertEqual(c.enforce("search_marriott_hotel", {"dest_name": "上海", "hotel_brands": "万豪"})["hotel_brands"], "万豪")

    def test_transport_number_passes_through_and_airport_cli_is_pinned(self):
        c = Contract(history("明天嘉兴南到上海虹桥的高铁"), TODAY)
        self.assertEqual(c.enforce("search_train", {"origin": "嘉兴南", "transport_no": "G"})["transport_no"], "G")
        with tempfile.TemporaryDirectory() as tmp:
            argv = FlyAI(settings(tmp)).argv("search_flight", {"origin": "嘉兴南湖机场", "destination": "北京首都机场"})
            self.assertIn("--origin=JNH", argv)
            self.assertIn("--destination=PEK", argv)

    def test_model_updated_time_parameters_are_not_repopulated_from_ledger(self):
        c = Contract(history("明天嘉兴到北京的航班，只看下午", "取消下午时段限制，全天都看"), TODAY)
        self.assertNotIn("dep_hour_start", c.conditions)
        self.assertNotIn("dep_hour_end", c.conditions)
        args = c.enforce("search_flight", {"origin": "嘉兴"})
        self.assertNotIn("dep_hour_start", args)
        self.assertNotIn("dep_hour_end", args)

    def test_explicit_round_trip_and_date_range_are_preserved(self):
        c = Contract(history("2027年1月18日北京到上海，1月20日回来，查往返机票"), date(2027, 1, 1))
        args = c.enforce("search_flight", {"origin": "北京", "destination": "上海", "dep_date": "2027-01-18", "back_date": "2027-01-20"})
        self.assertEqual(args["dep_date"], "2027-01-18")
        self.assertEqual(args["origin"], "北京")
        self.assertEqual(args["back_date"], "2027-01-20")
        c = Contract(history("北京到上海航班，2026-11-18至2026-11-20之间任意一天"), TODAY)
        args = c.enforce("search_flight", {"origin": "北京", "dep_date_start": "2026-11-18", "dep_date_end": "2026-11-20"})
        self.assertNotIn("dep_date", args)
        self.assertEqual(args["dep_date_end"], "2026-11-20")

    def test_unparsed_new_week_does_not_override_resolved_date_with_old_tomorrow(self):
        c = Contract(history("明天北京到上海航班", "改下周三，其他不变"), TODAY)
        args = c.enforce("search_flight", {"origin": "北京", "destination": "上海", "dep_date": "2026-11-18"})
        self.assertEqual(args["dep_date"], "2026-11-18")

    def test_semantic_query_cannot_carry_model_invented_third_night(self):
        c = Contract(history("2026-11-18到杭州，玩三天，酒店每晚500以内"), TODAY)
        args = c.enforce("ai_search", {"query": "杭州三天游，2026-11-18入住，2026-11-21离店，住3晚"})
        self.assertNotIn("2026-11-21", args["query"])
        self.assertIn("玩三天", args["query"])
        self.assertNotIn("有效住宿日期", args["query"])


class RenderingTests(unittest.TestCase):
    def test_supplier_words_normalized_but_exact_booking_url_preserved(self):
        original = "https://example.com/飞猪/原地址"
        text = neutralize_visible_text("飞猪酒店详情，来自FlyAI。[点击预订]("+original+")")
        self.assertIn(original, text)
        self.assertTrue(text.startswith("查询平台酒店详情，来自查询服务"))

    def test_upstream_itinerary_is_preserved_without_local_date_veto(self):
        conditions = {"check_in_date": "2026-11-18", "check_out_date": "2026-11-20"}
        bad = "11/18入住、11/21离店，共3晚。[酒店](https://example.com/wrong-stay)"
        text, _ = render_results([{"tool": "ai_search", "arguments": {"query": "杭州三天游"}, "conditions": conditions, "ok": True, "result": {"data": bad}}])
        self.assertIn(bad, text)
        self.assertNotIn("未采用该行程", text)

    def test_semantic_query_internal_conditions_are_not_shown_as_product_text(self):
        text, _ = render_results([{"tool": "ai_search", "arguments": {"query": '杭州三天行程\n用户明确的有效查询条件：{"check_in_date":"2026-11-18"}'}, "ok": True, "result": {"data": "以上信息基于飞猪搜索结果，退改以详情为准。"}}])
        self.assertNotIn("check_in_date", text)
        self.assertIn("杭州三天行程", text)
        self.assertNotIn("基于飞猪", text)
        self.assertIn("退改以详情为准", text)

    def test_round_trip_displays_actual_legs_even_if_query_dates_differ(self):
        item = train_item()
        returning = train_item(day="2026-11-16", train="D2213")
        segment = returning["journeys"][0]["segments"][0]
        for suffix in ("CityName", "StationName", "StationCode"):
            segment["dep"+suffix], segment["arr"+suffix] = segment["arr"+suffix], segment["dep"+suffix]
        item["journeys"].extend(returning["journeys"])
        args = {"origin": "嘉兴南", "destination": "上海虹桥", "dep_date": "2026-11-13", "back_date": "2026-11-16"}
        text, bindings = render_results([{"tool": "search_train", "arguments": args, "ok": True, "result": {"data": {"itemList": [item]}}}])
        self.assertEqual(len(bindings), 1)
        self.assertIn("D2213", text)
        args["back_date"] = "2026-11-17"
        text, bindings = render_results([{"tool": "search_train", "arguments": args, "ok": True, "result": {"data": {"itemList": [item]}}}])
        self.assertEqual(len(bindings), 1)
        self.assertIn("2026-11-16 06:33:00", text)

    def test_old_day_or_different_train_link_cannot_be_relabelled(self):
        item = train_item()
        args = {"origin": "嘉兴南", "destination": "上海虹桥", "dep_date": "2026-11-14", "seat_class_name": "二等座"}
        text, bindings = render_results([{"tool": "search_train", "arguments": args, "ok": True, "result": {"data": {"itemList": [item]}}}])
        self.assertEqual(len(bindings), 1)
        self.assertIn(item["jumpUrl"], text)
        self.assertIn("2026-11-13 06:33:00", text)
        self.assertIn("查询条件：2026-11-14", text)
        args["dep_date"] = "2026-11-13"
        text, bindings = render_results([{"tool": "search_train", "arguments": args, "ok": True, "result": {"data": {"itemList": [item]}}}])
        self.assertIn("D2212", text)
        self.assertNotIn("D2288", text)
        self.assertIn(item["jumpUrl"], text)
        self.assertEqual(bindings[0]["item"], item)

    def test_route_seat_time_and_airport_do_not_locally_hide_source_items(self):
        for changes in [{"origin": "北京"}, {"seat_class_name": "一等座"}, {"dep_hour_start": 12}, {"destination": "上海浦东"}]:
            with self.subTest(changes=changes):
                args = {"origin": "嘉兴南", "destination": "上海虹桥", "dep_date": "2026-11-13", **changes}
                text, bindings = render_results([{"tool": "search_train", "arguments": args, "ok": True, "result": {"data": {"itemList": [train_item()]}}}])
                self.assertEqual(len(bindings), 1)
                self.assertIn("D2212", text)

    def test_raw_price_image_and_url_stay_bound_and_ordered(self):
        item = {"name": "万怡A", "price": "¥437起/晚", "mainPic": "https://example.com/image.jpg", "detailUrl": "https://example.com/hotel-A"}
        outcome = {"tool": "search_marriott_hotel", "arguments": {"dest_name": "上海", "hotel_brands": "万怡"}, "ok": True, "result": {"data": {"itemList": [item, {**item, "name": "其他品牌B"}]}}}
        text, bindings = render_results([outcome], 2)
        self.assertEqual(len(bindings), 2)
        self.assertIn(item["price"], text)
        self.assertLess(text.index("![]("), text.index("[点击预订]"))
        self.assertIn("其他品牌B", text)

    def test_failure_is_not_no_inventory_or_successful_price(self):
        text, bindings = render_results([{"tool": "search_flight", "arguments": {}, "ok": False, "error": "上游超时"}])
        self.assertIn("查询未完成", text)
        self.assertIn("不能据此判断没有库存", text)
        self.assertEqual(bindings, [])

    def test_other_city_and_district_source_items_remain_visible(self):
        outcomes = [{"tool": "search_marriott_package", "arguments": {"keyword": "上海"}, "ok": True, "result": {"data": {"itemList": [{"title": "成都酒店餐饮套餐", "sellPoint": "豪华冰上海鲜", "price": "￥10", "detailUrl": "https://example.com/chengdu"}]}}}, {"tool": "search_hotel", "arguments": {"dest_name": "嘉兴市南湖区"}, "ok": True, "result": {"data": {"itemList": [{"name": "嘉兴汽车北站酒店", "address": "秀洲区", "price": "¥26", "detailUrl": "https://example.com/xiuzhou"}]}}}]
        text, bindings = render_results(outcomes)
        self.assertEqual(len(bindings), 2)
        self.assertIn("https://example.com/chengdu", text)
        self.assertIn("https://example.com/xiuzhou", text)


class EnforcementTests(unittest.IsolatedAsyncioTestCase):
    async def test_day_two_itinerary_change_keeps_semantic_search_and_stay_dates(self):
        class Planner:
            async def turn(self, *args):
                yield "message", {"role": "assistant", "tool_calls": [{"id": "c", "type": "function", "function": {"name": "ai_search", "arguments": '{"query":"杭州三日游第二天改为西湖，双床"}'}}]}
        class Tools:
            async def call(self, name, args):
                self.received = (name, args)
                return {"data": "第二天改为西湖边轻松游览；住宿日期不变。", "status": 0}
        with tempfile.TemporaryDirectory() as tmp:
            model, tools = Planner(), Tools()
            events = [e async for e in Agent(settings(tmp), model, tools).run(history("2026-12-18到杭州，玩三天，酒店每晚500以内", "第二天不去寺庙，改成西湖边走走，住双床，其他不变"), True, 4096)]
            self.assertEqual([t["function"]["name"] for t in model.available_tools], list(REGISTRY))
            self.assertIn("2026-12-18", tools.received[1]["query"])
            self.assertIn("玩三天", tools.received[1]["query"])
            self.assertIn("双床", tools.received[1]["query"])
            self.assertIn("第二天改为西湖", next(v for k,v in events if k == "result")["text"])

    async def test_ambiguous_new_route_is_sent_to_flyai_without_local_veto(self):
        class NoCall:
            async def turn(self, *args):
                yield "message", {"role": "assistant", "content": "请先确认交通方式"}
        class Tools:
            async def call(self, name, args):
                self.received = name, args
                return {"data": "请问选择航班还是火车？", "status": 0}
        with tempfile.TemporaryDirectory() as tmp:
            tools = Tools()
            events = [e async for e in Agent(settings(tmp), NoCall(), tools).run(history("明天嘉兴到北京"), True, 4096)]
            text = next(v for k,v in events if k == "result")["text"]
            self.assertEqual(tools.received[0], "ai_search")
            self.assertIn("明天嘉兴到北京", tools.received[1]["query"])
            self.assertIn("航班还是火车", text)

    async def test_model_skipped_search_falls_back_to_real_query_without_draft_leak(self):
        class Skips:
            async def turn(self, *args):
                yield "text", "CA1234，780元。https://example.com/fake"
                yield "message", {"role": "assistant", "content": "CA1234，780元。https://example.com/fake"}
        class Tools:
            async def call(self, *args):
                self.received = args
                return {"data": "未核实到该条件的报价", "status": 0}
        with tempfile.TemporaryDirectory() as tmp:
            model, tools = Skips(), Tools()
            emitted = [e async for e in Agent(settings(tmp), model, tools).run(history("2026-12-01嘉兴到北京的航班，重新查"), True, 4096)]
            self.assertEqual(model.tool_choice, "required")
            self.assertEqual(tools.received[0], "ai_search")
            text = next(v for k,v in emitted if k == "result")["text"]
            self.assertIn("未核实", text)
            self.assertNotIn("780", text)
            self.assertNotIn("example.com/fake", text)

    async def test_model_parameters_reach_tool_without_local_replacement(self):
        class WrongOrigin:
            async def turn(self, *args):
                yield "text", "先给你报9999元"
                yield "message", {"role": "assistant", "content": "先给你报9999元", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "search_flight", "arguments": json.dumps({"origin": "上海虹桥", "destination": "北京", "dep_date": "2026-12-02"})}}]}
        class Tools:
            async def call(self, name, args):
                self.received = (name, args)
                return {"data": {"itemList": []}, "status": 0}
        with tempfile.TemporaryDirectory() as tmp:
            tools = Tools()
            events = [e async for e in Agent(settings(tmp), WrongOrigin(), tools).run(history("2026-12-01嘉兴到北京的航班", "只要首都机场"), True, 4096)]
            self.assertEqual(tools.received[1], {"origin": "上海虹桥", "destination": "北京", "dep_date": "2026-12-02"})
            self.assertNotIn("9999", "".join(v for k, v in events if k == "text"))


if __name__ == "__main__":
    unittest.main()
