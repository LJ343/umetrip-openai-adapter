"""Real failure fixture and result visibility regressions for the open policy."""
import copy
import json
import re
import unittest
from datetime import date
from pathlib import Path

from app.contracts import Contract
from app.results import render_results, source_items


FIXTURE = json.loads((Path(__file__).parent / "fixtures/xian-beijing-afternoon-20261118.json").read_text())


class AllResultsTests(unittest.TestCase):
    def test_correct_model_destination_is_not_overridden_by_bad_ledger(self):
        c = Contract([{"role": "user", "content": FIXTURE["question"]}], date(2026, 11, 18))
        self.assertEqual(c.enforce("search_flight", FIXTURE["model_arguments"]), FIXTURE["model_arguments"])
        self.assertNotIn("北京下午", c.guidance())
        self.assertNotIn("北京下午", c.enforce("ai_search", {})["query"])

    def test_same_original_response_keeps_all_ten_items_and_bound_links(self):
        original = copy.deepcopy(FIXTURE["result"])
        for args in (FIXTURE["model_arguments"], FIXTURE["broken_arguments"]):
            text, bindings = render_results([{"tool": "search_flight", "arguments": args, "ok": True, "result": original}], limit=1)
            self.assertEqual(len(bindings), 10)
            self.assertEqual([b["item"] for b in bindings], source_items(original))
            for item in source_items(original):
                self.assertIn(item["jumpUrl"], text)
                self.assertIn(item["ticketPrice"], text)
            self.assertNotIn("未取得可核实且满足", text)
        self.assertEqual(original, FIXTURE["result"])

    def test_upstream_order_and_prices_are_not_locally_resorted_or_capped(self):
        items = [{"name": "商品" + str(i), "price": 900-i, "jumpUrl": "https://example.com/" + str(i)} for i in range(7)]
        text, bindings = render_results([{"tool": "search_hotel", "arguments": {"dest_name": "北京", "max_price": 1, "sort": "price_asc"}, "ok": True, "result": {"data": {"itemList": items}}}])
        self.assertEqual([b["item"] for b in bindings], items)
        self.assertIn("商品6", text)
        self.assertLess(text.index("商品0"), text.index("商品6"))

    def test_long_description_and_unrecognized_nonempty_data_remain_visible(self):
        description = "完整介绍" * 180 + "末尾不能丢失"
        text, _ = render_results([{"tool": "search_poi", "arguments": {"city_name": "北京"}, "ok": True, "result": {"data": {"itemList": [{"name": "景点", "description": description}]}}},
                                  {"tool": "keyword_search", "arguments": {"query": "测试"}, "ok": True, "result": {"data": {"newField": "上游新结构内容"}}}])
        self.assertIn(description, text)
        self.assertIn("上游新结构内容", text)

    def test_only_visible_brand_is_normalized_while_original_urls_survive(self):
        link = "https://example.com/飞猪/original"
        result = {"data": {"itemList": [{"name": "飞猪商品", "jumpUrl": link}]}, "systemMessage": "飞猪返回的使用条件"}
        before = copy.deepcopy(result)
        text, bindings = render_results([{"tool": "keyword_search", "arguments": {"query": "商品"}, "ok": True, "result": result}])
        self.assertIn(link, text)
        self.assertNotIn("飞猪", text.replace(link, ""))
        self.assertEqual(result, before)
        self.assertEqual(len(bindings), 1)


if __name__ == "__main__":
    unittest.main()
