import copy
import json
import tempfile
import unittest
from datetime import date

from app.agent import Agent
from app.contracts import Contract, route_in
from app.results import neutralize_visible_text, render_results
from app.tools import REGISTRY, validate
from test_service import settings


class PassthroughTests(unittest.TestCase):
    def test_empty_upstream_text_is_reported_without_inventing_a_result(self):
        text, _ = render_results([{"tool": "ai_search", "arguments": {}, "ok": True, "result": {"data": "", "status": 0}}])
        self.assertIn("上游本次返回空内容", text)

    def test_keyword_search_keeps_distinct_product_subqueries(self):
        c = Contract([{"role": "user", "content": "三亚景点、2026年双11酒店套餐、租车都看看"}], date(2026, 11, 11))
        for q in ("三亚 2026 双11 酒店套餐", "三亚 租车"):
            self.assertEqual(c.enforce("keyword_search", {"query": q}), {"query": q})

    def test_opening_effective_dates_and_metro_lines_are_not_trip_vetoes(self):
        data = "11月19日入住、20日离店，共1晚。11月16日起7:30开放。地铁1/4/6号线。\n[酒店](https://example.com/a)"
        text, _ = render_results([{"tool": "ai_search", "arguments": {"query": "苏州行程"}, "conditions": {"check_in_date": "2026-11-19", "check_out_date": "2026-11-20"}, "ok": True, "result": {"data": data}}])
        self.assertIn(data, text)
        self.assertNotIn("未采用", text)

    def test_each_actual_tool_keeps_results_after_another_category_uses_its_limit(self):
        outcomes = [{"tool": "search_poi", "arguments": {"city_name": "三亚"}, "ok": True, "result": {"data": {"itemList": [{"name": "景点"+str(i)} for i in range(4)]}}}, {"tool": "keyword_search", "arguments": {"query": "2026年双11"}, "ok": True, "result": {"data": {"itemList": [{"info": {"title": "26年双11真实商品", "jumpUrl": "https://example.com/current"}}]}}}]
        text, _ = render_results(outcomes, 3)
        self.assertIn("26年双11真实商品", text)
        self.assertIn("https://example.com/current", text)

    def test_brand_neutralization_preserves_source_and_original_link(self):
        source = {"data": "飞猪 / 飛豬 / 飞 猪 / flyai，\n[飞猪预订](https://example.com/飞猪/a)"}
        before = copy.deepcopy(source)
        text, _ = render_results([{"tool": "ai_search", "arguments": {}, "ok": True, "result": source}])
        self.assertEqual(source, before)
        self.assertIn("https://example.com/飞猪/a", text)
        visible = text.replace("https://example.com/飞猪/a", "")
        for word in ("飞猪", "飛豬", "飞 猪", "flyai"):
            self.assertNotIn(word, visible)

    def test_entity_closing_tag_variants_keep_actual_name(self):
        for closing in ("spot_entity", "stop_entity"):
            data = "<spot_entity>name:中国苏州评弹博物馆;type:poi;id:171601</"+closing+">"
            text, _ = render_results([{"tool": "ai_search", "arguments": {}, "ok": True, "result": {"data": data}}])
            self.assertIn("中国苏州评弹博物馆", text)
            self.assertNotIn("entity>", text)

    def test_ordinary_verbs_do_not_overwrite_transport_conditions(self):
        for tail in ("没查到就说未确认", "没得到当天公告就说明", "找不到就别编"):
            self.assertIsNone(route_in(tail))
            c = Contract([{"role": "user", "content": q} for q in ("2026-12-01嘉兴到北京的航班", tail)], date(2026, 11, 11))
            self.assertEqual(c.conditions["origin"], "嘉兴南湖机场")
            self.assertEqual(c.conditions["destination"], "北京")

    def test_dont_search_old_prices_does_not_block_an_explicit_new_query(self):
        c = Contract([{"role": "user", "content": "不用查旧价格，请重新查今天的航班"}], date(2026, 11, 11))
        self.assertIsNone(c.no_lookup)
        c = Contract([{"role": "user", "content": "刚才返程是哪天？先别重新查价格。"}], date(2026, 11, 11))
        self.assertEqual(c.no_lookup, "history")

    def test_complete_query_preserves_freshness_requirements_and_only_human_context(self):
        question = "只查11月14日当天，没查到就说未确认；要来源发布日期。"
        c = Contract([{"role": "user", "content": "杭州西湖音乐喷泉"}, {"role": "assistant", "content": "旧报价999元"}, {"role": "user", "content": question}], date(2026, 11, 11))
        args = c.enforce("ai_search", {"query": "喷泉"})
        self.assertIn(question, args["query"])
        self.assertIn("2026-11-11", args["query"])
        self.assertIn("杭州西湖音乐喷泉", args["query"])
        self.assertNotIn("999", args["query"])
        validate("ai_search", {"query": args["query"] + "说明"*1500})


class ForwardingTests(unittest.IsolatedAsyncioTestCase):
    async def test_changed_arrival_with_two_day_trip_keeps_planning_and_hotel_search(self):
        first = "2026年11月19日到苏州，玩两天，两个人，住一晚，每晚500以内，帮我安排轻松行程和住宿。"
        latest = "出发改为2026年11月21日，仍玩两天住一晚，两人双床500以内，请重新查询。"
        class Planner:
            async def turn(self, *args):
                yield "message", {"role": "assistant", "tool_calls": [{"id": "hotel", "type": "function", "function": {"name": "search_hotel", "arguments": '{"dest_name":"苏州","check_in_date":"2026-11-21","check_out_date":"2026-11-22"}'}}]}
        class Search:
            def __init__(self):
                self.calls = []
            async def call(self, name, args):
                self.calls.append((name,args))
                return {"status":0, "data":"21日抵达、22日离开；本次行程详情" if name == "ai_search" else {"itemList":[]}}
        with tempfile.TemporaryDirectory() as tmp:
            search = Search()
            events = [e async for e in Agent(settings(tmp), Planner(), search).run([{"role":"user","content":q} for q in (first,latest)], True, 4096)]
            self.assertEqual([n for n,a in search.calls], ["ai_search","search_hotel"])
            self.assertIn(latest, search.calls[0][1]["query"])
            self.assertEqual(search.calls[1][1], {"dest_name": "苏州", "check_in_date": "2026-11-21", "check_out_date": "2026-11-22"})
            self.assertIn("本次行程详情", next(v for k,v in events if k == "result")["text"])

    async def test_exact_day_question_keeps_poi_command_and_adds_complete_query(self):
        question = "只查2026年11月14日当天西湖喷泉是否开放，没得到当天公告就说明。"
        class Planner:
            async def turn(self, *args):
                yield "message", {"role": "assistant", "tool_calls": [{"id": "poi", "type": "function", "function": {"name": "search_poi", "arguments": '{"city_name":"杭州","keyword":"西湖音乐喷泉"}'}}]}
        class Search:
            def __init__(self):
                self.calls = []
            async def call(self, name, args):
                self.calls.append((name, args))
                return {"status": 0, "data": "档案信息" if name == "search_poi" else "当天公告未确认"}
        with tempfile.TemporaryDirectory() as tmp:
            search = Search()
            events = [e async for e in Agent(settings(tmp), Planner(), search).run([{"role": "user", "content": question}], True, 4096)]
            self.assertEqual([n for n,a in search.calls], ["ai_search", "search_poi"])
            self.assertIn(question, search.calls[0][1]["query"])
            text = next(v for k,v in events if k == "result")["text"]
            self.assertIn("档案信息", text)
            self.assertIn("当天公告未确认", text)

    async def test_food_question_keeps_keyword_ability_and_full_semantic_intent(self):
        question = "换成烧烤，别给我酒店。"
        class Planner:
            async def turn(self, *args):
                yield "message", {"role": "assistant", "tool_calls": [{"id": "kw", "type": "function", "function": {"name": "keyword_search", "arguments": '{"query":"杭州西湖 烧烤"}'}}]}
        class Search:
            def __init__(self):
                self.calls = []
            async def call(self, name, args):
                self.calls.append((name, args))
                return {"status": 0, "data": "没有核实到匹配的烧烤店" if name == "ai_search" else {"itemList": []}}
        with tempfile.TemporaryDirectory() as tmp:
            search = Search()
            events = [e async for e in Agent(settings(tmp), Planner(), search).run([{"role": "user", "content": "杭州西湖边找吃饭地方"}, {"role": "user", "content": question}], True, 4096)]
            self.assertEqual([n for n,a in search.calls], ["ai_search", "keyword_search"])
            self.assertIn(question, search.calls[0][1]["query"])
            self.assertEqual(search.calls[1][1]["query"], "杭州西湖 烧烤")
            self.assertIn("没有核实到匹配的烧烤店", next(v for k,v in events if k == "result")["text"])

    async def test_all_eight_commands_remain_callable_despite_local_classification(self):
        cases = {
            "ai_search": {"query": "综合方案"},
            "keyword_search": {"query": "日本 上网卡"},
            "search_flight": {"origin": "北京"},
            "search_train": {"origin": "嘉兴南"},
            "search_hotel": {"dest_name": "杭州"},
            "search_poi": {"city_name": "苏州"},
            "search_marriott_hotel": {"dest_name": "上海"},
            "search_marriott_package": {"keyword": "上海"},
        }
        for name, arguments in cases.items():
            class Planner:
                async def turn(self, *args):
                    self.seen = {t["function"]["name"] for t in self.available_tools}
                    yield "message", {"role": "assistant", "tool_calls": [{"id": "x", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}
            class Search:
                async def call(self, tool, args):
                    validate(tool, args)
                    self.actual = tool, args
                    return {"data": "本次真实上游内容", "status": 0}
            with self.subTest(tool=name), tempfile.TemporaryDirectory() as tmp:
                model, search = Planner(), Search()
                events = [e async for e in Agent(settings(tmp), model, search).run([{"role": "user", "content": "请综合搜索旅行方案"}], True, 4096)]
                self.assertEqual(model.seen, set(REGISTRY))
                self.assertEqual(search.actual[0], name)
                self.assertIn("本次真实上游内容", next(v for k,v in events if k == "result")["text"])

    async def test_identical_semantic_calls_within_one_request_execute_once(self):
        class Double:
            async def turn(self, *args):
                yield "message", {"role": "assistant", "tool_calls": [{"id": str(i), "type": "function", "function": {"name": "ai_search", "arguments": json.dumps({"query": q})}} for i,q in enumerate(("酒店", "行程"))]}
        class Search:
            count = 0
            async def call(self, name, args):
                self.count += 1
                return {"data": "本次综合真实返回", "status": 0}
        with tempfile.TemporaryDirectory() as tmp:
            search = Search()
            events = [e async for e in Agent(settings(tmp), Double(), search).run([{"role": "user", "content": "2026-12-01到苏州玩两天，酒店500以内"}], True, 4096)]
            result = next(v for k,v in events if k == "result")
            self.assertEqual(search.count, 1)
            self.assertEqual(result["text"].count("本次综合真实返回"), 1)
            self.assertEqual(len([m for m in result["history"] if m["role"] == "tool"]), 2)

    async def test_supplier_name_split_across_tiny_probe_chunks_cannot_leak(self):
        class Probe:
            async def turn(self, *args):
                yield "text", "飞"
                yield "text", "猪"
                yield "finish_reason", "length"
                yield "message", {"role": "assistant", "content": "飞猪"}
        with tempfile.TemporaryDirectory() as tmp:
            events = [e async for e in Agent(settings(tmp), Probe(), None).run([{"role": "user", "content": "回复一个词"}], True, 5)]
            self.assertNotIn("飞猪", "".join(v for k,v in events if k == "text"))
            self.assertEqual(next(v for k,v in events if k == "result")["finish_reason"], "length")

    async def test_all_previously_blocked_business_questions_reach_semantic_search(self):
        questions = [
            "请查杭州西湖音乐喷泉，没查到就说未确认。",
            "杭州演出，没得到当天公告就直接说明。",
            "嘉兴南湖机场最新公交变动，请查近期来源。",
            "嘉兴酒店",
            "明天新疆飞西安的航班",
            "明天嘉兴到北京",
            "杭州西湖周边两家真实餐馆，核实是否仍营业。",
        ]
        class DraftOnly:
            async def turn(self, *args):
                yield "text", "模型草稿9999元"
                yield "message", {"role": "assistant", "content": "模型草稿9999元"}
        class Search:
            async def call(self, name, args):
                validate(name, args)
                self.actual = name, args
                return {"data": "飞猪返回：未核实具体报价。[查看](https://example.com/source)", "status": 0}
        for q in questions:
            with self.subTest(question=q), tempfile.TemporaryDirectory() as tmp:
                search = Search()
                events = [e async for e in Agent(settings(tmp), DraftOnly(), search).run([{"role": "user", "content": q}], True, 4096)]
                self.assertEqual(search.actual[0], "ai_search")
                self.assertIn(q, search.actual[1]["query"])
                text = next(v for k,v in events if k == "result")["text"]
                self.assertIn("https://example.com/source", text)
                self.assertNotIn("9999", text)
                self.assertNotIn("飞猪", text)

    async def test_invalid_model_parameters_fall_back_instead_of_blocking_question(self):
        class Broken:
            async def turn(self, *args):
                yield "message", {"role": "assistant", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "search_flight", "arguments": "{invalid"}}]}
        class Search:
            async def call(self, name, args):
                self.actual = name, args
                return {"data": "上游真实回答", "status": 0}
        with tempfile.TemporaryDirectory() as tmp:
            search = Search()
            events = [e async for e in Agent(settings(tmp), Broken(), search).run([{"role": "user", "content": "2026-12-01北京到上海航班"}], True, 4096)]
            self.assertEqual(search.actual[0], "ai_search")
            self.assertIn("上游真实回答", next(v for k,v in events if k == "result")["text"])
