"""Dated, reviewed local facts and narrow tool routing for known unsupported tasks."""
import json
import re
from functools import lru_cache
from pathlib import Path


LOCAL_REPLIES = {
    "greeting": "你好，我是德创数字的航班助手小德，可以帮您查航班、火车票和各类交通出行方式。",
    "identity": "我是德创数字的航班助手小德，可以帮您查航班、火车票，比较交通出行方式，也能查询酒店、景点和旅游产品。",
    "hotel_dates": "请问您哪天入住、哪天离店？我再按日期查询酒店。",
}


@lru_cache(maxsize=1)
def airport_facts():
    return json.loads((Path(__file__).parent / "knowledge" / "jiaxing-airport.json").read_text())


def ground_reply_issue(text):
    # FlyAI has no dedicated navigation endpoint. Product/nearby distances are
    # not evidence for a route's distance, fare or duration, even as estimates.
    numeric = r"(?:\d+(?:\.\d+)?\s*(?:[—–\-~～至到]\s*\d+(?:\.\d+)?)?|[一二三四五六七八九十百]+)\s*(?:公里|千米|km|分钟|小时|元|块钱)|[¥￥]\s*\d"
    if re.search(numeric, text, re.I):
        return "unverified_route_numbers"
    if re.search(r"(?:没有|暂无|无).{0,12}直达|(?:需要|必须|(?<!无)需)换乘|并非.{0,12}直接到达", text):
        return "unverified_no_direct_transit"
    return None


def grounded_transport_reply(history, searched, max_tokens=4096):
    """Safe fallback to dated verified facts when a route draft overclaims."""
    if max_tokens < 512:
        return "可选择打车或网约车；送机到二楼出发层，接机在一楼按现场指引。公交可核对98路等机场接驳。尚未取得可靠的具体换乘、耗时或车费，请以实时地图为准。"
    recent = "\n".join(m.get("content", "") for m in history[-8:] if m.get("role") == "user")
    facts = airport_facts()
    guide = facts["sources"]["airport-transport-guide"]
    address = ""
    if re.search(r"中国航信|中航信|1177", recent):
        address = "您提到的中国航信，如指嘉兴数据中心，地图登记地址为南湖区亚太路1177号，可用该地址与嘉兴南湖机场作为导航起终点。\n\n"
    checked = "本次补充搜索仍未取得" if searched else "目前未取得"
    return (
        address
        + "可选择打车或网约车；机场送机到二楼出发层，接机在一楼按出租车、网约车上客区指引。\n\n"
        + "已公布的机场公交接驳包括98路（嘉兴南站）、61路（秀洲区行政中心公交枢纽）、快3路（运河新区公交枢纽）及古镇旅游专线G2。"
        + f"这是[{guide['date']}发布的交通指南]({guide['url']})中的接驳信息，具体站点、班次及是否适合您的起终点，请以公交运营方或实时地图为准。\n\n"
        + checked + "可核实的具体换乘路线、距离、耗时和车费，出发前请使用实时地图核对。"
    )


def request_context(history):
    users = [m["content"] for m in history if m.get("role") == "user"]
    latest = users[-1] if users else ""
    compact = re.sub(r"[\s，。！？,.!?～~]+", "", latest)
    if re.fullmatch(r"(?:你好|您好|嗨|哈喽)(?:呀|啊)?(?:小德|小格)?", compact):
        return "greeting", "最新消息只是问候；只以德创数字航班助手小德身份简短回应，不重复历史查询。"
    if re.fullmatch(r"(?:重新说一下|再说一下|请问)?(?:你是谁(?:你?能(?:干|做)什么)?|你叫什么(?:名字)?|你?能(?:干|做)什么|你有什么功能|介绍一下你自己)", compact):
        return "identity", "最新消息只询问服务身份或能力；介绍德创数字航班助手小德，不调用查询工具。"

    # A simple place/hotel search with no stay dates needs one clarification,
    # not invented prices or example booking URLs. Preserve dated follow-ups.
    hotel = r"酒店|民宿|住宿|宾馆|客栈"
    dates = r"\d{1,4}[-/年月]\d{1,2}|今天|明天|后天|今晚|明晚|下周|周[一二三四五六日天末]|星期|住[一二三四五六七八九十\d]+[天晚]"
    positive_latest = re.sub(r"(?:别|不要|不看|不推荐)(?:推荐)?(?:酒店|住宿|景点)", "", latest)
    simple_hotel = re.search(r"(?:酒店|民宿|宾馆|客栈)[吧啊吗呢？?]*$", positive_latest.strip())
    policy_question = re.search(r"套餐|团购|通兑|退改|取消|政策|发票|手续|宠物|介绍|是什么|已订|订单|上次|之前|上一|同样|一样", latest)
    dated_hotel_history = any(re.search(hotel, text) and re.search(dates, text) for text in users[-4:])
    if simple_hotel and not policy_question and not re.search(dates, latest) and not dated_hotel_history:
        return "hotel_dates", "最新消息是未给入住和离店日期的酒店查询，允许使用官方工具搜索参考商品；无日期结果不代表指定日期房价或库存。"

    recent = "\n".join(users[-4:])
    # Jiaxing alone is insufficient: a restaurant/hotel query must retain tools.
    if not re.search(r"嘉兴|南湖机场|嘉湖机场|\bJNH\b", recent, re.I):
        return None, ""
    facts = airport_facts()
    context = "本地人工核验资料（核验日期不是实时查询时间；按每项来源日期使用，不能当作当日班次或报价）：\n" + json.dumps(facts, ensure_ascii=False)
    # Explicit product/flight requests keep all official travel tools available.
    product = r"航班|机票|飞往|直飞|余票|飞机票|火车|高铁|车次|酒店|民宿|住宿|套餐|门票|景点|预订|预约|租车|包车|接送机|订票|买票|餐厅|餐馆|烧烤|网吧|美食|饭店"
    for question in reversed(users[-4:]):
        # New intent takes precedence over older searches; short confirmations
        # can inherit the most recent explicit business intent.
        positive = re.sub(r"(?:别|不要|不看|不推荐)(?:推荐)?(?:酒店|住宿|景点)", "", question)
        if re.search(product, positive):
            return None, context
        if re.search(r"怎么走|怎么去|如何去|交通|路线|公交|地铁|打车|自驾|前往|到机场|去机场|到中国航信", question):
            return "jiaxing_ground", context + "\n本轮是地面问路。优先说明已核实的可选方式与接驳点，缺少具体起终点时只追问一个必要地点。需要补充信息时可使用一次ai_search，不保证它具有实时地图/公交能力。返回不相关酒店、异地接送商品时说明未取得匹配路线，不换词追加商品搜索、不展示无关商品，不猜费用、道路或耗时。"
        if re.search(r"介绍|通航|开通|开放|客运|货运|位置|在哪|地址|机场信息|跑道|规模|机场代码|三字码", question):
            return "jiaxing_background", context + "\n本轮是机场基本情况问题，直接使用带日期的核验资料；无需商品检索。"
    return None, context
