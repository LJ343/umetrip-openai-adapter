"""Conservative, deletion-only cleanup of plain-text UI instructions.

No network/model calls. Unknown grammar and protected content pass unchanged.
Offsets refer to model content after the existing card-tag remover, not SSE bytes.
"""
import re
import time
from typing import Callable, Optional


_END = frozenset("。！？!?：:\n")
_BOUNDARY = frozenset("。！？!?\n")
_SUBJECTS = ("", "您", "你", "请", "可", "可以", "您可", "您可以", "你可", "你可以",
             "还可", "还可以", "也可", "也可以", "您还可", "您还可以", "您也可", "您也可以", "你也可以", "你还可以", "详情请")
_CONDITIONAL_LEADS = ("如需", "如想", "若需", "若想", "如果需要", "需要", "想要")
_LEADS = tuple(subject + action for subject in _SUBJECTS
               for action in ("点击", "直接点击", "按需点击", "点开", "点选", "点下方", "轻触", "点按", "在下方", "在下面", "通过", "参考", "查看", "查阅")) + (
    "下面为", "下面展示", "下面提供", "下方为", "下方展示", "下方提供", "以下为", "以下展示", "以下提供", "以下是", "下面是", "下方是",
    "航班动态详情卡片", "航班卡片", "机票卡片", "酒店卡片", "天气卡片", "火车票卡片", "以上为可查询到的航班信息",
    "更多航班信息", "具体航班详情", "详情请见", "详情见", "详情参见") + _CONDITIONAL_LEADS
_CLAUSE_LEADS = _LEADS
_LEAD_SET = frozenset(_LEADS)
_LEAD_INITIALS = frozenset(lead[0] for lead in _LEADS)
_LEAD_PREFIXES = frozenset(lead[:i] for lead in _LEADS for i in range(len(lead) + 1))
_CLAUSE_PREFIXES = frozenset(lead[:i] for lead in _CLAUSE_LEADS for i in range(len(lead) + 1))
_SUBJECT = r"(?:(?:您|你)(?:还|也)?(?:可以|可)?|请|还(?:可以|可)|也(?:可以|可)|可以|可)?"
_REF = r"(?:(?:下方|下面|以下|上方|该|此|这张)(?:的)?(?:推荐的)?(?:航班动态|航班|机票|酒店|天气|火车票)?(?:卡片|入口|列表)|(?:下方|下面|以下)(?:的)?推荐(?:的)?(?:航班|酒店|车次)|推荐(?:航班|酒店|车次))"
_DETAILS = r"(?:航班时刻|起降时间|时刻|票价|余票|舱位|退改规则|经停信息)"
_TARGET = rf"(?:完整航班动态|航班动态查询|航班动态|航班实时状态|航班状态|航班查询|机票预订|机票查询|机场天气详情|机场天气|天气详情|舱位详情|舱位价格|舱位|票价详情|票价|航班详情|航班|机票|火车票|列车|酒店|这两家详情|这些酒店详情|这几班详情|这些航班的详情|上述航班详情|详情房型报价|房型报价|房型|详情|{_DETAILS}(?:[、及和]{_DETAILS}){{0,3}})(?:信息|详情|页面|页|列表|入口)?"
_VERB = r"(?:进入|查看|浏览|了解|查询|选择|预订|订票|购票|查看更多|查阅)"
_ACTION = rf"(?:{_VERB}(?:更全的|更多的|更多|各)?(?:{_TARGET})?(?:(?:并|或|并且|及|和){_VERB}(?:{_TARGET})?)?)"
_PURPOSE = r"(?:购票|查看或预订|直达舱位详情|查看更全的航班或票价详情|查看更全的航班|查看更多航班|查看具体舱位价格|打开火车票页面查看或下单|查看机场天气详情|预订|查看详情|了解具体票价及舱位)"
_CONDITION = rf"(?:(?:如需|如想|若需|若想|如果需要|需要|想要)(?:{_PURPOSE}|(?:进一步)?{_ACTION})[，,]?|需要的话)?"
_INTRO = r"(?:以上为可查询到的航班信息[，,])?"
_RULES = (
    ("ui_click", re.compile(rf"{_INTRO}{_CONDITION}(?:详情)?{_SUBJECT}(?:直接|按需)?(?:通过)?(?:点击|点开|点选|点|打开|轻触|点按){_REF}(?:(?:即可|就可|可以|可)?(?:{_ACTION}|查看预订|预订|购票))?(?:[，,](?:(?:也可以|也可|可|或)(?:查看更多(?:航班|酒店|车次)|查看各航班实时状态)|按航班号或起降地进一步查询|自行查看最新航线安排))?")),
    ("ui_location", re.compile(rf"{_SUBJECT}在{_REF}(?:中|里)?{_ACTION}")),
    ("ui_reference", re.compile(rf"{_SUBJECT}(?:通过|参考|查看|查阅){_REF}(?:{_ACTION})?")),
    ("ui_reverse", re.compile(rf"(?:更多航班信息可在|具体航班详情请查看|详情(?:请见|见|参见)){_REF}(?:查看)?")),
    ("ui_list", re.compile(r"(?:下面|下方|以下)(?:为您|为你)?(?:展示|提供|附上|列出)(?:了)?(?:可点击的)?(?:航班动态|航班|机票|酒店|天气|火车票)(?:详情)?(?:列表|卡片)(?:[，,]可查看各航班实时状态)?")),
    ("ui_list_intro", re.compile(r"(?:下面|下方|以下)(?:是|为)(?:相关的?|为您推荐的)?(?:航班动态|航班|机票|酒店|天气|火车票)(?:详情)?卡片")),
    ("ui_result_heading", re.compile(r"(?:下面|下方|以下)(?:为|是)(?:查询结果|推荐结果|搜索结果)卡片(?:[，,](?:点击|点开|轻触)(?:即可|可|可以)?(?:查看|浏览)(?:详情|房型详情|航班详情|房型报价))?")),
    ("ui_card_heading", re.compile(r"(?:航班动态详情|航班|机票|酒店|天气|火车票)卡片(?:如下|见下方|在下方)")),
)


class CardHintFilter:
    def __init__(self, max_chars: int = 128, max_wait: float = 0.2,
                 on_delete: Optional[Callable] = None, clock=time.monotonic):
        self.max_chars = max(16, min(512, max_chars))
        self.max_wait = max(0.001, min(1.0, max_wait))
        self.on_delete, self.clock = on_delete, clock
        self.pending = ""
        self.pending_since = None
        self.pending_start = self.position = 0
        self.at_boundary = True
        self.blocked_line = self.structured = self.fenced = False
        self.line_prefix = ""
        self.line_fence_seen = False
        self.quote_depth = 0
        self.double_quote = self.escaped = self.passthrough_sentence = False
        self.first_nonspace_seen = False
        self._seen = self._candidate_context = ""
        self._lead_complete = False
        self.removals = self.timeouts = self.overflows = 0
        self.max_held_ms = self.cpu_ns = self.max_pending_chars = 0

    @property
    def deadline(self):
        return self.pending_since + self.max_wait if self.pending_since is not None else None

    def _observe(self, char):
        if not self.first_nonspace_seen and not char.isspace():
            self.first_nonspace_seen = True
            self.structured = char in "{[<"
        if len(self.line_prefix) < 12:
            self.line_prefix += char
        prefix = self.line_prefix.lstrip()
        if not self.line_fence_seen and (prefix.startswith("```") or prefix.startswith("~~~")):
            self.fenced = not self.fenced
            self.line_fence_seen = True
            self.blocked_line = True
        if char in "`~>|\"'“”「」『』<":
            self.blocked_line = True
        if char in "“「『":
            self.quote_depth += 1
        elif char in "”」』":
            self.quote_depth = max(0, self.quote_depth - 1)
        if char == '"' and not self.fenced and not self.escaped:
            self.double_quote = not self.double_quote
        self.escaped = char == "\\" and not self.escaped
        if char in _BOUNDARY:
            self.at_boundary = True
            self.passthrough_sentence = False
        elif not char.isspace():
            self.at_boundary = False
        if char == "\n":
            self.blocked_line = False
            self.line_prefix = ""
            self.line_fence_seen = False

    def _release(self):
        value = self.pending
        if self.pending_since is not None:
            self.max_held_ms = max(self.max_held_ms, (self.clock() - self.pending_since) * 1000)
        self.pending, self.pending_since = "", None
        self._lead_complete = False
        return value

    def _decision(self, value):
        # Preserve any trailing parenthesis verbatim, e.g. “仅查询，不自动下单”.
        end = len(value)
        while end and value[end - 1] in "。！!：:\n\r ":
            end -= 1
        core = value[:end]
        paren = re.search(r"[（(].*[）)]$", core)
        constraint = re.search(r"[，,](?:暂不预订|仅查询|只查询|不下单|未下单|不自动下单|仅查询[，,]不下单|只查询[，,]不预订)$", core)
        keep_from = paren.start() if paren else (constraint.start() + 1 if constraint else None)
        test = core[:paren.start()] if paren else (core[:constraint.start()] if constraint else core)
        normalized = re.sub(r"\s+", "", test)
        if normalized.startswith(("，", ",")):
            normalized = normalized[1:]
        # Only repeated identifiers may occupy these narrowly defined UI slots.
        # A new flight, route, price, date or qualifier must stay in the answer.
        def repeated_flight(match):
            flight = match.group(1)
            exists = re.search(r"(?<![A-Za-z0-9])" + re.escape(flight) + r"(?![A-Za-z0-9])", self._candidate_context)
            return "查看" + match.group(2) if exists else match.group(0)
        normalized = re.sub(r"查看([A-Z][A-Z0-9]\d{3,4})的(完整航班动态|航班动态|航班状态)", repeated_flight, normalized)
        def repeated_route(match):
            origin, destination = match.groups()
            route = re.escape(origin) + r"(?:[—→-]|到|至|飞往|飞)" + re.escape(destination)
            exists = re.search(route, re.sub(r"\s+", "", self._candidate_context))
            return "查看更多航班" if exists else match.group(0)
        normalized = re.sub(r"查看更多([\u4e00-\u9fff]{1,8})[—→-]([\u4e00-\u9fff]{1,8}?)的航班", repeated_route, normalized)
        if not normalized or re.search(r"[0-9A-Za-z￥¥$%？！?!\"'“”「」『』`<>]", normalized):
            return None
        for rule, pattern in _RULES:
            if pattern.fullmatch(normalized):
                return rule, keep_from
        return None

    def _complete(self, allow_delete):
        value = self.pending
        decision = self._decision(value) if allow_delete else None
        start = self.pending_start
        self._release()
        if decision is None:
            return value
        rule, keep_from = decision
        end = keep_from if keep_from is not None else len(value)
        if value.startswith(("，", ",")) and keep_from is None and value.endswith("。"):
            # Keep the original sentence terminator when deleting only its tail.
            end -= 1
        self.removals += 1
        if self.on_delete:
            self.on_delete(rule_id=rule, start_char=start, end_char=start + end, text=value[:end])
        return value[end:]

    def feed(self, text):
        began = time.perf_counter_ns()
        output = []
        if self.pending_since is not None and self.clock() >= self.deadline:
            self.timeouts += 1
            output.append(self._release())
            self.at_boundary = False
            self.passthrough_sentence = True
        for char in text:
            offset = self.position
            self.position += 1
            protected = self.structured or self.blocked_line or self.fenced or self.quote_depth or self.double_quote or self.passthrough_sentence
            if self.pending:
                self.pending += char
                self._observe(char)
                self.max_pending_chars = max(self.max_pending_chars, len(self.pending))
                if char in "，," and not self.pending.startswith(("，", ",")) and not self._decision(self.pending[:-1]) and not re.fullmatch(_INTRO + _CONDITION, re.sub(r"\s+", "", self.pending[:-1])) and self.pending != "以上为可查询到的航班信息，":
                    value = self._release()
                    output.append(value[:-1])
                    if value.startswith(_CONDITIONAL_LEADS):
                        # An unrecognized condition may contain the only fare or
                        # eligibility qualifier. Preserve the whole sentence.
                        output[-1] = value
                        self.passthrough_sentence = True
                        continue
                    self.pending, self.pending_since, self.pending_start = char, self.clock(), offset
                    self._candidate_context = (self._seen + "".join(output))[-8192:]
                elif char in "（(" and self._decision(self.pending[:-1]):
                    # A clear navigation prefix can be deleted while the entire
                    # parenthetical, including unique prices/limits, passes through.
                    value, start = self.pending, self.pending_start
                    rule = self._decision(value[:-1])[0]
                    self._release()
                    self.removals += 1
                    if self.on_delete:
                        self.on_delete(rule_id=rule, start_char=start, end_char=start + len(value) - 1, text=value[:-1])
                    output.append(char)
                elif char in _END:
                    output.append(self._complete(True))
                elif len(self.pending) >= self.max_chars:
                    self.overflows += 1
                    output.append(self._release())
                    self.passthrough_sentence = True
                elif char in "￥¥$%`\"'“”「」『』<>?？":
                    output.append(self._release())
                else:
                    leads = _LEAD_SET
                    prefixes = _CLAUSE_PREFIXES if self.pending.startswith(("，", ",")) else _LEAD_PREFIXES
                    candidate = self.pending[1:] if self.pending.startswith(("，", ",")) else self.pending
                    if not self._lead_complete:
                        if candidate in leads:
                            self._lead_complete = True
                        elif candidate not in prefixes:
                            output.append(self._release())
                continue
            if not protected and (char in "，," or self.at_boundary and not char.isspace() and char in _LEAD_INITIALS):
                self.pending, self.pending_since, self.pending_start = char, self.clock(), offset
                self._candidate_context = (self._seen + "".join(output))[-8192:]
                self.max_pending_chars = max(self.max_pending_chars, 1)
            else:
                output.append(char)
            self._observe(char)
        result = "".join(output)
        self._seen = (self._seen + result)[-8192:]
        self.cpu_ns += time.perf_counter_ns() - began
        return result

    def flush(self, complete=False):
        return self._complete(complete) if self.pending else ""

    def expire(self):
        if self.pending_since is None or self.clock() < self.deadline:
            return ""
        self.timeouts += 1
        self.at_boundary = False
        self.passthrough_sentence = True
        return self._release()
