"""Regressions for the same-request route/date corruption seen on 8002."""
import unittest
from datetime import date

from app.contracts import Contract, route_in


QUESTION = ("2026年11月19日从嘉兴南湖机场飞北京首都机场，11月21日返回，"
            "两个人经济舱，北京玩三天。安排去返航班和每天的简要行程，不查酒店；"
            "当天票价、余票没有字段就写未确认，不用旧航季代替指定日期班次。")


def contract(*questions):
    return Contract([{"role": "user", "content": q} for q in questions], date(2026, 11, 12))


class RouteLegTests(unittest.TestCase):
    def test_plain_fly_route_precedes_instruction_phrase(self):
        self.assertEqual(route_in(QUESTION), ("嘉兴南湖机场", "北京首都机场"))
        c = contract(QUESTION)
        self.assertEqual(c.conditions["destination"], "北京首都机场")
        self.assertNotIn('"destination": "返"', c.enforce("ai_search", {})["query"])

    def test_instructions_are_not_places(self):
        for text in ("安排去返航班", "帮我安排去返航班", "怎么安排去返航班", "看看能不能去", "想去北京玩三天", "上海的航班查到什么了"):
            with self.subTest(text=text):
                self.assertIsNone(route_in(text))

    def test_two_model_legs_keep_their_own_direction_and_date(self):
        c = contract(QUESTION)
        for original in (
            {"origin": "JNH", "destination": "PEK", "dep_date": "2026-11-19", "seat_class_name": "经济舱", "sort_type": 6},
            {"origin": "PEK", "destination": "JNH", "dep_date": "2026-11-21", "seat_class_name": "经济舱", "sort_type": 3},
        ):
            with self.subTest(original=original):
                args = c.enforce("search_flight", original)
                self.assertEqual(args["dep_date"], original["dep_date"])
                self.assertEqual(args["origin"], original["origin"])
                self.assertEqual(args["destination"], original["destination"])
                self.assertNotIn("back_date", args)

    def test_day_two_followup_does_not_reapply_outbound_to_return(self):
        c = contract(QUESTION, "第二天只安排室内博物馆，少走路，航班去返日期不变，请重新查并给简版。")
        args = c.enforce("search_flight", {"origin": "PEK", "destination": "JNH", "dep_date": "2026-11-21"})
        self.assertEqual((args["origin"], args["destination"], args["dep_date"]), ("PEK", "JNH", "2026-11-21"))
        self.assertNotIn("back_date", args)

    def test_return_call_is_not_reinterpreted_by_local_ledger(self):
        args = contract(QUESTION).enforce("search_flight", {"origin": "PEK", "destination": "JNH", "dep_date": "2026-11-19", "back_date": "2026-11-21"})
        self.assertEqual(args, {"origin": "PEK", "destination": "JNH", "dep_date": "2026-11-19", "back_date": "2026-11-21"})

    def test_explicit_combined_roundtrip_still_supported(self):
        c = contract(QUESTION)
        for original in ({"origin": "JNH", "destination": "PEK"}, {"origin": "JNH", "destination": "PEK", "dep_date": "2026-11-19", "back_date": "2026-11-21"}):
            args = c.enforce("search_flight", original)
            self.assertEqual(args, original)

    def test_train_roundtrip_uses_independent_leg(self):
        c = contract("11月19日嘉兴南到上海虹桥的高铁，11月21日返回，二等座")
        args = c.enforce("search_train", {"origin": "上海虹桥站", "destination": "嘉兴南站", "dep_date": "2026-11-21"})
        self.assertEqual((args["origin"], args["destination"], args["dep_date"]), ("上海虹桥站", "嘉兴南站", "2026-11-21"))
        self.assertNotIn("back_date", args)

    def test_followup_return_changes_ledger_to_single_return(self):
        c = contract(QUESTION, "返程改为11月23日，从北京首都机场回嘉兴南湖机场，经济舱直飞，重新查一个。")
        args = c.enforce("search_flight", {"origin": "PEK", "destination": "JNH", "dep_date": "2026-11-23"})
        self.assertEqual((args["origin"], args["destination"], args["dep_date"]), ("PEK", "JNH", "2026-11-23"))
        self.assertNotIn("back_date", args)

    def test_airport_arguments_are_never_replaced_by_regex_ledger(self):
        args = contract(QUESTION).enforce("search_flight", {"origin": "上海虹桥", "destination": "北京大兴机场", "dep_date": "2026-11-19"})
        self.assertEqual((args["origin"], args["destination"]), ("上海虹桥", "北京大兴机场"))

    def test_repeated_return_date_changes_do_not_flip_direction(self):
        c = contract("查2026年11月19日嘉兴南湖机场到北京首都机场的航班，经济舱直飞",
                     "返程11月21日，从北京首都机场回嘉兴南湖机场，经济舱直飞，请重新查一个。",
                     "返程改成11月23日，其余条件不变，重新查一个。")
        args = c.enforce("search_flight", {"origin": "PEK", "destination": "JNH", "dep_date": "2026-11-23"})
        self.assertEqual((args["origin"], args["destination"], args["dep_date"]), ("PEK", "JNH", "2026-11-23"))
        self.assertNotIn("back_date", args)

    def test_return_only_conversation_keeps_direction_without_inventing_outbound_date(self):
        c = contract("11月21日从北京首都机场返回嘉兴南湖机场的机票，经济舱", "返程改11月23日，其他不变")
        args = c.enforce("search_flight", {"origin": "PEK", "destination": "JNH", "dep_date": "2026-11-23"})
        self.assertEqual((args["origin"], args["destination"], args["dep_date"]), ("PEK", "JNH", "2026-11-23"))


if __name__ == "__main__":
    unittest.main()
