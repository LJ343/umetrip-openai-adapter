"""Eight official FlyAI commands. Never execute model-produced shell text."""
import asyncio
import json
import os
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime

from jsonschema import Draft202012Validator
from .tracing import record


def string(description, enum=None):
    p = {"type": "string", "description": description, "minLength": 1, "maxLength": 2000}
    if enum:
        p["enum"] = enum
    return p


def integer(description, low, high):
    return {"type": "integer", "description": description, "minimum": low, "maximum": high}


DATE = string("北京时间的绝对日期YYYY-MM-DD，按用户指定日期查询")
DATE["format"] = "date"
SORT = string("排序", ["distance_asc", "rate_desc", "price_asc", "price_desc", "no_rank"])
PRICE = integer("最高价格，人民币元；酒店为每晚", 0, 10000000)
TRANSPORT = {
    "origin": string("出发城市/机场/车站，优先中文全称"),
    "destination": string("到达城市/机场/车站，优先中文全称"),
    **{k: DATE for k in ["dep_date", "dep_date_start", "dep_date_end", "back_date", "back_date_start", "back_date_end"]},
    "journey_type": integer("1直达，2中转", 1, 2),
    "seat_class_name": string("舱位或席别；不得把商务舱报价当成经济舱"),
    "transport_no": string("航班号/车次；火车可用逗号分隔多个车次"),
    "transfer_city": string("中转城市"),
    **{k: integer("小时，24小时制", 0, 23) for k in ["dep_hour_start", "dep_hour_end", "arr_hour_start", "arr_hour_end"]},
    "total_duration_hour": integer("最长总行程小时数", 1, 720),
    "max_price": PRICE,
    "sort_type": integer("1价格降序 2推荐 3价格升序 4耗时升序 5耗时降序 6早出发 7晚出发 8直达优先", 1, 8),
}
HOTEL = {
    "dest_name": string("目的地国家/省/市/区，中文全称"),
    "key_words": string("搜索关键词"), "poi_name": string("周边地标/景点"),
    "hotel_bed_types": string("大床房/双床房/多床房，可逗号分隔"),
    "max_price": PRICE, "sort": SORT,
    "check_in_date": DATE, "check_out_date": DATE,
}
POI_CATEGORIES = "自然风光 山湖田园 森林丛林 峡谷瀑布 沙滩海岛 沙漠草原 人文古迹 古镇古村 历史古迹 园林花园 宗教场所 公园乐园 主题乐园 水上乐园 影视基地 动物园 植物园 海洋馆 体育场馆 演出赛事 剧院剧场 博物馆 纪念馆 展览馆 地标建筑 市集 文创街区 城市观光 户外活动 滑雪 漂流 冲浪 潜水 露营 温泉".split()


def definition(command, description, properties, required):
    return {"type": "function", "function": {
        "name": command.replace("-", "_"), "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required, "additionalProperties": False},
    }}


def semantic_query():
    return {**string("用户完整原话、必要多轮上下文和日期条件"), "maxLength": 30000}


TOOLS = [
    definition("keyword-search", "全品类旅游商品搜索：酒店、机票、门票、演出、体育赛事、跟团游、邮轮、签证、保险、电话卡、租车、接送机等。仅搜索和返回链接，不办理订单。", {"query": semantic_query()}, ["query"]),
    definition("ai-search", "自然语言复杂旅游搜索、行程规划所需产品、多品类组合搜索。把上下文补全为独立完整问题，明确绝对日期/预算/人数/偏好。", {"query": semantic_query()}, ["query"]),
    definition("search-flight", "结构化机票搜索与比较，可按机场/日期/舱位/价格/时刻筛选；目的地和日期可选，允许探索性查询。", TRANSPORT, ["origin"]),
    definition("search-train", "结构化火车票搜索与比较，支持席别、车次、中转及时间价格筛选。", TRANSPORT, ["origin"]),
    definition("search-hotel", "结构化酒店/民宿/客栈查询与比较。按目的地、周边地标、入住离店日、星级、床型和价格筛选。", {**HOTEL, "hotel_types": string("住宿类型", ["酒店", "民宿", "客栈"]), "hotel_stars": string("1至5，多个星级用逗号分隔")}, ["dest_name"]),
    definition("search-poi", "查询城市景点、兴趣点、演出与活动。", {"city_name": string("所在城市中文全称"), "poi_level": integer("景区等级1至5", 1, 5), "keyword": string("景点关键词"), "category": string("单选分类", POI_CATEGORIES)}, ["city_name"]),
    definition("search-marriott-hotel", "万豪集团旗下酒店专属搜索。当前官方CLI通过关键词匹配品牌/酒店名，并非严格品牌筛选；必须核对返回brandName/name是否符合要求。", {**HOTEL, "hotel_brands": string("品牌关键词，逗号分隔；转换为key_words查询，匹配程度须核对结果"), "hotel_name": string("酒店全称或模糊关键词，匹配程度须核对结果")}, ["dest_name"]),
    definition("search-marriott-package", "万豪集团酒店套餐商品搜索；关键词每次只使用一个维度（省/市/品牌/酒店名/卖点）。", {"keyword": string("单个搜索维度"), "sort_type": string("价格排序", ["price_asc", "price_desc"])}, ["keyword"]),
]
REGISTRY = {t["function"]["name"]: t["function"] for t in TOOLS}


class ToolFailure(Exception):
    pass


def validate(name, arguments, today=None):
    if name not in REGISTRY:
        raise ToolFailure("不支持的工具；只允许公开的8类FlyAI搜索命令")
    errors = sorted(Draft202012Validator(REGISTRY[name]["parameters"], format_checker=Draft202012Validator.FORMAT_CHECKER).iter_errors(arguments), key=lambda e: str(e.path))
    if errors:
        # Validation messages can echo arbitrary input. Return the field only.
        field = ".".join(str(x) for x in errors[0].path) or "arguments"
        raise ToolFailure(f"参数不符合工具定义：{field} ({errors[0].validator})")
    # Calendar syntax/types remain official-schema checks. Whether a date or
    # interval is searchable is decided by the upstream, not a local veto.


class FlyAI:
    def __init__(self, settings):
        self.s = settings

    def argv(self, name, arguments):
        validate(name, arguments)
        arguments = arguments.copy()
        if name == "search_flight":
            # Use reviewed, unambiguous IATA identifiers for these airports;
            # never substitute a nearby airport to make a query succeed.
            airports = {"嘉兴南湖机场": "JNH", "北京首都机场": "PEK", "北京大兴机场": "PKX"}
            for key in ("origin", "destination"):
                if key in arguments:
                    arguments[key] = airports.get(arguments[key], arguments[key])
        if name == "search_marriott_hotel":
            # Official 1.0.16 --help exposes key-words, but the Skill docs
            # additionally describe hotel-brands/hotel-name. Never send invalid flags.
            terms = [arguments.pop(k) for k in ("key_words", "hotel_brands", "hotel_name") if arguments.get(k)]
            if terms:
                arguments["key_words"] = " ".join(terms)
        args = [self.s.node_bin, str(self.s.cli_path), name.replace("_", "-")]
        for key, value in arguments.items():
            # Equals syntax prevents values such as --help becoming options.
            args.append("--" + key.replace("_", "-") + "=" + str(value))
        return args

    async def call(self, name, arguments):
        argv = self.argv(name, arguments)
        record("tool_request", tool=name, arguments=arguments, argv=argv)
        # Do not inherit proxy keys, model keys, debugging overrides or LD_PRELOAD.
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT", "HOME") if k in os.environ}
        env["FLYAI_API_KEY"] = self.s.flyai_key
        proc = await asyncio.create_subprocess_exec(*argv, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=65536)

        async def read_limited(pipe, limit):
            chunks, size = [], 0
            while data := await pipe.read(16384):
                size += len(data)
                if size > limit:
                    raise ToolFailure("FlyAI响应超过安全容量，本次未取得完整结果；请缩小查询范围")
                chunks.append(data)
            return b"".join(chunks)

        out_task = asyncio.create_task(read_limited(proc.stdout, self.s.max_tool_output))
        err_task = asyncio.create_task(read_limited(proc.stderr, 65536))
        try:
            out, err, _ = await asyncio.wait_for(asyncio.gather(out_task, err_task, proc.wait()), self.s.tool_timeout)
            record("tool_response", tool=name, returncode=proc.returncode, stdout=out.decode(errors="replace"), stderr=err.decode(errors="replace"))
            if proc.returncode:
                raise ToolFailure("FlyAI调用失败，请稍后重试；没有完成搜索")
            try:
                result = json.loads(out)
            except (ValueError, UnicodeDecodeError):
                raise ToolFailure("FlyAI未返回有效JSON，本次未取得完整结果") from None
            if not isinstance(result, dict) or result.get("isError") is True or result.get("error") or ("status" in result and result["status"] not in (0, "0")):
                raise ToolFailure("FlyAI返回业务错误，本次搜索未成功")
            if not any(key in result for key in ("data", "content")):
                raise ToolFailure("FlyAI响应缺少业务数据")
            if name == "search_marriott_hotel" and any(k in arguments for k in ("hotel_brands", "hotel_name")):
                result["adapter_notes"] = "当前官方CLI仅提供品牌/酒店名关键词搜索，不保证严格筛选；应核对原始brandName/name，无法确认时明确说明。"
            return result
        except asyncio.TimeoutError:
            record("tool_failure", tool=name, code="timeout")
            raise ToolFailure("FlyAI查询超时，本次未取得完整结果") from None
        finally:
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            for task in (out_task, err_task):
                task.cancel()
            await asyncio.gather(out_task, err_task, return_exceptions=True)
