"""User-only query conditions. Assistant suggestions are never user consent.

The ledger covers explicit incremental constraints; unfamiliar phrasing still
goes to the model. It never extracts prices or URLs from assistant messages.
"""
import re
from datetime import date, timedelta

NUMBERS = {c: i for i, c in enumerate("零一二三四五六七八九")}
NUMBERS.update({"两": 2, "十": 10})
TRANSPORT = {"search_flight", "search_train"}
HOTELS = {"search_hotel", "search_marriott_hotel"}


def number(value):
    if value.isdigit():
        return int(value)
    if value in NUMBERS:
        return NUMBERS[value]
    if "十" in value:
        a, b = value.split("十", 1)
        return NUMBERS.get(a, 1) * 10 + NUMBERS.get(b, 0)
    return None


def dates_in(text, today):
    pattern = r"\d{4}年\d{1,2}月\d{1,2}[日号]?|\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}月\d{1,2}[日号]?|大后天|后天|明天|今天|明晚|今晚"
    found = []
    for match in re.finditer(pattern, text):
        raw = match.group()
        try:
            if re.match(r"\d{4}", raw):
                y, m, d = map(int, re.findall(r"\d+", raw))
                value = date(y, m, d)
            elif "月" in raw:
                m, d = map(int, re.findall(r"\d+", raw))
                value = date(today.year + (1 if "明年" in text else 0), m, d)
            else:
                value = today + timedelta(days={"大后天": 3, "后天": 2, "明天": 1, "明晚": 1}.get(raw, 0))
            found.append((match.start(), value))
        except ValueError:
            continue
    return found


def clean_place(text):
    if "从" in text:
        text = text.rsplit("从", 1)[1]
    text = re.sub(r"^(?:帮我|请|查一下|查查|查询|查|看看|看一下|想看看|想看|明天|后天|今天|那|换成|改成|换|从)+", "", text.strip())
    return re.split(r"的|航班|机票|飞机|高铁|火车|车票|，|,|。|？|\?|；|经济舱|还是|二等座|\s", text)[0].strip()


def route_in(text):
    # Only a user's explicit A→B expression is authoritative. Never promote an
    # assistant's alternative airport, even when the user later narrows B.
    text = re.sub(r"\d{4}年\d{1,2}月\d{1,2}[日号]?|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}[日号]?|大后天|后天|明天|今天", "", text)
    for clause in re.split(r"[，。！？,!?；;\n]", text):
        # These are ordinary verbs, never an authoritative user route.
        if re.search(r"查到|找到|搜到|得到|想到|看不到|查不到|找不到", clause):
            continue
        match = re.search(r"(?:从)?([\u4e00-\u9fffA-Za-z]{2,22}?)(?:飞往|飞到|飞|返回|回到|回|到|去|→)([\u4e00-\u9fffA-Za-z]{2,20})", clause)
        if not match:
            continue
        origin, destination = (clean_place(x) for x in match.groups())
        bad = {"只看", "只要", "改", "改成", "落地只看", "然后", "再查返程", "去程", "时候"}
        # A partial phrase such as "安排去返航班" is not two places. An
        # uncertain parse must leave the model's arguments alone, not invent
        # authoritative conditions or reject the user's question.
        instruction = r"安排|怎么|如何|能不能|想|去返|往返"
        if (len(origin) >= 2 and len(destination) >= 2 and origin not in bad
                and not re.search(instruction, origin + destination)):
            return origin, destination
    return None


def canonical_place(value, flight=False):
    aliases = {"JNH": "嘉兴南湖机场", "PEK": "北京首都机场", "PKX": "北京大兴机场"}
    value = aliases.get(value.upper(), value)
    if flight and value in {"嘉兴", "嘉兴机场", "南湖机场", "嘉兴南湖"}:
        return "嘉兴南湖机场"
    if value in {"首都", "首都机场", "北京首都"}:
        return "北京首都机场"
    if value in {"大兴", "大兴机场", "北京大兴"}:
        return "北京大兴机场"
    return value


class Contract:
    def __init__(self, history, today):
        self.users = [m["content"] for m in history if m.get("role") == "user"]
        self.latest = self.users[-1] if self.users else ""
        self.today = today
        self.kind, self.conditions, self.assumption = None, {}, ""
        self.limit = 3
        state, active, outbound, cleared = {}, None, None, {}
        for text in self.users:
            kind = self.detect_kind(text, active)
            if kind != active and kind:
                old = state.get(active, {})
                if kind in TRANSPORT:
                    state.setdefault(kind, {}).update({k: v for k, v in old.items() if k in {"origin", "destination", "dep_date"}})
                active = kind
            if not active:
                continue
            args = state.setdefault(active, {})
            removed = cleared.setdefault(active, set())
            parsed = dates_in(text, today)
            values = [d for _, d in parsed]
            # Do not overwrite a model-resolved new date with an old pinned
            # date when this small deterministic parser cannot resolve it.
            if not values and re.search(r"下周|本周|这周|星期|周[一二三四五六日天末]|下个月|下月|明日", text):
                keys = {"dep_date", "dep_date_start", "dep_date_end"} if active in TRANSPORT else {"check_in_date", "check_out_date"}
                for key in keys:
                    args.pop(key, None)
            if active in TRANSPORT:
                prior_transport = args.copy()
                route = route_in(text)
                if route:
                    args.update(origin=route[0], destination=route[1])
                departure = re.search(r"([\u4e00-\u9fffA-Za-z]{2,15})(?:出发|起飞)", text)
                if departure and not re.search(r"下午|上午|晚上|中午|早上|凌晨|早晨|点|\d", departure[1]):
                    args["origin"] = clean_place(departure[1])
                correction = re.search(r"(?:我说的是|出发地是|从)(嘉兴南湖机场|南湖机场|嘉兴机场)", text)
                if correction:
                    args["origin"] = "嘉兴南湖机场"
                    outbound = None
                if re.search(r"首都|\bPEK\b", text, re.I):
                    if not route:
                        args["destination"] = "北京首都机场"
                if re.search(r"大兴|\bPKX\b", text, re.I):
                    if not route:
                        args["destination"] = "北京大兴机场"
                returning = bool(re.search(r"返程|返回|回来", text)) and not re.search(r"刚才.*哪天|别.*查", text)
                round_trip = len(values) >= 2 and bool(re.search(r"往返|来回|返程|返回|回来", text))
                if round_trip:
                    returning = False
                    args.update(dep_date=values[0].isoformat(), back_date=values[1].isoformat())
                elif returning:
                    args.pop("back_date", None)
                if returning:
                    # Capture the previous outbound BEFORE applying an explicit
                    # return route. Saving the just-updated return as outbound
                    # would flip the direction again on the next date change.
                    if not outbound and route:
                        outbound = {"origin": route[1], "destination": route[0]}
                        if all(canonical_place(prior_transport.get(k, ""), active == "search_flight") == canonical_place(outbound[k], active == "search_flight") for k in ("origin", "destination")) and prior_transport.get("dep_date"):
                            outbound["dep_date"] = prior_transport["dep_date"]
                    elif not outbound and all(k in prior_transport for k in ("origin", "destination", "dep_date")):
                        outbound = {k: prior_transport[k] for k in ("origin", "destination", "dep_date")}
                    if outbound and not route:
                        args.update(origin=outbound["destination"], destination=outbound["origin"])
                    days = re.search(r"([一二两三四五六七八九十\d]+)天后", text)
                    if outbound and outbound.get("dep_date") and days:
                        n = number(days[1])
                        if n is not None:
                            args["dep_date"] = (date.fromisoformat(outbound["dep_date"]) + timedelta(days=n)).isoformat()
                    elif values:
                        args["dep_date"] = values[0].isoformat()
                elif values:
                    args["dep_date"] = values[0].isoformat()
                    if re.search(r"后天之后一天", text):
                        args["dep_date"] = (values[0] + timedelta(days=1)).isoformat()
                    if route:
                        outbound = None
                if len(values) >= 2 and not round_trip and re.search(r"之间|期间|任选|任意一天|区间", text):
                    args.pop("dep_date", None)
                    args.update(dep_date_start=values[0].isoformat(), dep_date_end=values[1].isoformat())
                for token, value in [("经济舱", "经济舱"), ("商务舱", "商务舱"), ("二等座", "二等座"), ("一等座", "一等座")]:
                    if token in text:
                        args["seat_class_name"] = value
                if re.search(r"直飞|直达", text):
                    args["journey_type"] = 1
                    removed.discard("journey_type")
                if re.search(r"下午|上午|早上|晚上|中午", text):
                    removed.difference_update({"dep_hour_start", "dep_hour_end"})
                    for k in ("dep_hour_start", "dep_hour_end"):
                        args.pop(k, None)
                    # A negated earlier time is not the new time interval.
                    time_text = text.split("换")[-1] if "换" in text else text
                    if "下午" in time_text:
                        args.update(dep_hour_start=12, dep_hour_end=18)
                    elif "上午" in time_text or "早上" in time_text:
                        args.update(dep_hour_start=0, dep_hour_end=11)
                    elif "晚上" in time_text:
                        args.update(dep_hour_start=18, dep_hour_end=23)
                    clock = re.search(r"([一二两三四五六七八九十\d]+)点(以前|之前|以后|之后|前|后)", time_text)
                    if clock:
                        h = number(clock[1])
                        if h is not None:
                            h += 12 if "下午" in time_text and h < 12 else 0
                            args["dep_hour_end" if clock[2] in {"以前", "之前", "前"} else "dep_hour_start"] = max(0, h - 1) if clock[2] in {"以前", "之前", "前"} else h
                if re.search(r"不限时段|不限时间|全天|取消.*(?:上午|下午|时间|时段).*限制", text):
                    args.pop("dep_hour_start", None)
                    args.pop("dep_hour_end", None)
                    removed.update({"dep_hour_start", "dep_hour_end"})
                if re.search(r"可以中转|允许中转|不一定直飞|不限直达", text):
                    args.pop("journey_type", None)
                    removed.add("journey_type")
            if active in HOTELS:
                if "万怡" in text:
                    args["hotel_brands"] = "万怡"
                if len(values) >= 2:
                    args.update(check_in_date=values[0].isoformat(), check_out_date=values[1].isoformat())
                elif values:
                    if re.search(r"离店|退房", text) and not re.search(r"入住|到", text):
                        args["check_out_date"] = values[0].isoformat()
                    else:
                        prior_nights = ((date.fromisoformat(args["check_out_date"]) - date.fromisoformat(args["check_in_date"])).days
                                        if args.get("check_in_date") and args.get("check_out_date") else None)
                        args["check_in_date"] = values[0].isoformat()
                        if prior_nights:
                            args["check_out_date"] = (values[0] + timedelta(days=prior_nights)).isoformat()
                        else:
                            args.pop("check_out_date", None)
                shift = re.search(r"入住.*(?:往后|后挪|推迟).*?([一二两三四五六七八九十\d]+)天", text)
                if shift and args.get("check_in_date"):
                    n = number(shift[1])
                    if n is not None:
                        args["check_in_date"] = (date.fromisoformat(args["check_in_date"]) + timedelta(days=n)).isoformat()
                nights = re.search(r"住([一二两三四五六七八九十\d]+)晚", text)
                days = re.search(r"(?:玩|游玩)([一二两三四五六七八九十\d]+)天", text)
                if args.get("check_in_date") and (nights or days):
                    n = number((nights or days)[1])
                    if n is not None and n > 0:
                        n = n if nights else max(1, n - 1)
                        args["check_out_date"] = (date.fromisoformat(args["check_in_date"]) + timedelta(days=n)).isoformat()
                        self.assumption = "行程按到达日为第1天、最后一天离店计算住宿；如需多住一晚，请告诉我。" if days else ""
                if "双床" in text:
                    args["hotel_bed_types"] = "双床房"
                elif "大床" in text:
                    args["hotel_bed_types"] = "大床房"
            budget = re.search(r"(?:预算(?:改成|加到|改为)?|每晚|不超过)([一二两三四五六七八九十百千万\d]+)|([一二两三四五六七八九十百千万\d]+)(?:元)?以内", text)
            if budget:
                raw = next(x for x in budget.groups() if x)
                # Common spoken budgets, with no dependency on assistant prices.
                value = number(raw)
                if value is None and re.fullmatch(r"[一二两三四五六七八九]百", raw):
                    value = number(raw[0]) * 100
                if value is not None:
                    args["max_price"] = value
            count = re.search(r"(?:给|找|最多|只看|还是|要)(?:我|我们)?([一二两三四五六七八九十\d]+)(?:个|家|班|条)", text)
            if count and number(count[1]):
                self.limit = min(number(count[1]), 20)
            if "便宜" in text or "价格升序" in text:
                args["sort_type" if active in TRANSPORT or active == "search_marriott_package" else "sort"] = 3 if active in TRANSPORT else "price_asc"
            if "贵到便宜" in text or "价格降序" in text:
                args["sort_type" if active in TRANSPORT or active == "search_marriott_package" else "sort"] = 1 if active in TRANSPORT else "price_desc"
        self.kind = active
        self.conditions = state.get(active, {}).copy()
        self.cleared = cleared.get(active, set())
        if active == "search_flight":
            for k in ("origin", "destination"):
                if k in self.conditions:
                    self.conditions[k] = canonical_place(self.conditions[k], True)

    @staticmethod
    def detect_kind(text, previous):
        positive = re.sub(r"(?:别|不要|不看|不推荐|不去)(?:推荐)?(?:酒店|寺庙|景点|露营地)", "", text)
        if re.search(r"万豪.*套餐|万豪酒店套餐|换.*套餐", positive) or ("套餐" in positive and previous == "search_marriott_hotel"):
            return "search_marriott_package"
        if re.search(r"万豪|万怡|喜来登|万丽|万枫|瑞吉|丽思", positive) and "套餐" not in positive:
            return "search_marriott_hotel"
        if re.search(r"烧烤|餐馆|餐厅|上网卡|电话卡|签证|接送机|租车|邮轮|保险", positive):
            return "keyword_search"
        if re.search(r"航班|机票|飞机|飞往|直飞", positive):
            return "search_flight"
        if re.search(r"火车|高铁|车次|二等座|一等座", positive):
            return "search_train"
        if re.search(r"酒店|民宿|住宿|宾馆|入住|退房|双床|大床", positive):
            return previous if previous in HOTELS else "search_hotel"
        if re.search(r"好玩|景点|门票|游览", positive):
            return "search_poi"
        return previous

    @property
    def no_lookup(self):
        # "不用查旧价，请重新查今天" is a search, not a no-lookup request.
        remainder = re.sub(r"(?:先|暂时)?(?:别|不要|不用|不必)(?:重新|再)?(?:查询|搜索|查)(?:价|价格|报价|票价|余票|库存)?", "", self.latest)
        if re.search(r"重新查|重新搜索|再查|请查|帮我查|要查|查一下|查询|搜索", remainder):
            return None
        if re.search(r"(?:先|暂时)?(?:别|不要|不用|不必)(?:重新|再)?查|刚才.*哪天|之前.*哪天", self.latest):
            return "history"
        if re.search(r"没声音|戴耳机|音量", self.latest) and not re.search(r"航班|机票|火车|高铁|酒店|景点|餐馆", self.latest):
            return "device"
        if re.search(r"只是看看|先不要预订|暂时不订|不用订了", self.latest):
            return "pause"
        return None

    def clarification(self):
        if not self.kind and route_in(self.latest):
            return "您想查询航班还是火车？确认出行方式后，我再按您的地点和日期查询。"
        if self.kind in TRANSPORT and self.conditions.get("origin") in {"新疆", "浙江", "广东", "江苏", "四川"}:
            return "请问您从哪座城市或机场出发？省内有多个机场或车站，需要先确认具体出发地。"
        return None

    @property
    def semantic_search(self):
        """Uncertain classification is a semantic query, never a local veto."""
        text = self.latest
        if not self.kind or self.clarification():
            return True
        if re.search(r"综合搜索|语义搜索|行程|攻略|玩法|最新|近期|来源|发布日期|开放|营业|餐馆|餐厅|烧烤|公交|怎么走|怎么去|第[一二两三四五六七八九十\d]+天|(?:玩|游玩)[一二两三四五六七八九十\d]+天", text):
            return True
        if self.kind in TRANSPORT:
            return not all(self.conditions.get(k) for k in ("origin", "destination", "dep_date")) and not self.conditions.get("dep_date_start")
        if self.kind in HOTELS:
            return not all(self.conditions.get(k) for k in ("check_in_date", "check_out_date"))
        return False

    @property
    def needs_semantic_answer(self):
        # A POI/category schema cannot carry an exact-day opening question or
        # a complete day-by-day itinerary. Keep any dedicated searches and add
        # the full question, rather than replace or prohibit those commands.
        return bool(re.search(r"行程|攻略|第[一二两三四五六七八九十\d]+天|(?:玩|游玩)[一二两三四五六七八九十\d]+天|当天|当日|截至|公告|发布日期|近期|最新|怎么走|怎么去|换乘|餐馆|餐厅|烧烤|吃饭", self.latest))

    def enforce(self, name, arguments):
        """Normalize the official schema only; never impose parsed conditions.

        The model receives the human history and chooses query parameters. The
        small regex ledger is retained for explicit history/no-lookup replies,
        but has no authority over business calls or result visibility.
        """
        from .tools import REGISTRY
        allowed = REGISTRY[name]["parameters"]["properties"]
        args = {k: v for k, v in arguments.items() if k in allowed and v is not None and (not isinstance(v, str) or v.strip())}
        if name == "keyword_search":
            args.setdefault("query", self.latest)
        if name == "ai_search":
            parts = [self.latest, "检索基准日期（北京时间）：" + self.today.isoformat()]
            if self.users[:-1]:
                parts.append("此前用户原话（最新输入优先）：\n" + "\n".join(self.users[:-1]))
            parts.append("请按完整用户原话实际搜索并回答；未取得的价格、库存和日期证据请如实说明，不编造。不要复述检索指令。")
            args["query"] = "\n".join(parts)
        return args

    def guidance(self):
        return ("全部八个工具始终可用，依据完整用户原话和多轮历史选择、组合查询。"
                "工具参数由你理解用户意图后填写；服务不会用本地正则提取条件覆盖它们。"
                "日期、时间词与城市机场分别填写，不把下午、上午、明天等拼入地点。"
                "用户指定的机场和日期必须遵守，返程按真实方向与日期查询，不擅自换地点。"
                "未提供的官方可选字段可以省略。上游返回内容由服务完整展示，不在本地二次筛选或截为默认三条。")
