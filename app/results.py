"""Render business facts and URLs from the SAME tool item, never model prose."""
import json
import re
from datetime import date
from urllib.parse import urlsplit

from .contracts import HOTELS, TRANSPORT
from .tracing import record


def neutralize_visible_text(text):
    # Keep original booking/image URLs, including any non-ASCII path segment.
    parts = re.split(r"(https?://[^\s<>\[\]()]+)", text)
    return "".join(p if p.startswith(("http://", "https://")) else re.sub(r"fly\s*ai", "查询服务", re.sub(r"飞\s*猪|飛\s*豬", "查询平台", p), flags=re.I) for p in parts)


def plain(value):
    # External text is data, not executable Markdown or a source of new links.
    return re.sub(r"[\[\]<>`!*]", "", str(value or "")).strip()


def url(value):
    if not isinstance(value, str) or re.search(r"[\s<>\\\[\]()]", value):
        return None
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    return value if parts.scheme in {"http", "https"} and parts.hostname and not parts.username and not parts.password else None


def amount(value):
    found = re.search(r"\d+(?:\.\d+)?", str(value or ""))
    return float(found[0]) if found else None


def source_items(result):
    data = result.get("data")
    if isinstance(data, dict) and isinstance(data.get("itemList"), list):
        items = [i.get("info", i) for i in data["itemList"] if isinstance(i, dict)]
        return [i for i in items if isinstance(i, dict)]
    return []


def query_label(name, args):
    if name in TRANSPORT:
        label = " · ".join(plain(args[k]) for k in ("dep_date", "origin", "destination", "seat_class_name") if args.get(k))
        if args.get("back_date"):
            label += " · 返程" + plain(args["back_date"])
        if args.get("dep_date_start"):
            label += " · 出发日期范围" + plain(args["dep_date_start"]) + "至" + plain(args.get("dep_date_end"))
        return label
    if name in HOTELS:
        a, b = args.get("check_in_date"), args.get("check_out_date")
        nights = f"，{(date.fromisoformat(b) - date.fromisoformat(a)).days}晚" if a and b else ""
        return plain(args.get("dest_name")) + (f" · {a}入住，{b}离店{nights}" if a and b else "")
    return plain(args.get("city_name") or args.get("keyword") or str(args.get("query") or "搜索结果").split("\n", 1)[0])


def render_item(name, item, index):
    lines = []
    if name in TRANSPORT:
        segments = [s for j in item.get("journeys", []) for s in j.get("segments", [])]
        lines.append(f"{index}. " + " / ".join(plain(s.get("marketingTransportNo")) for s in segments))
        for s in segments:
            lines.append("   " + " → ".join(plain(s.get(k)) for k in ("depStationName", "arrStationName"))
                         + "；" + " → ".join(plain(s.get(k)) for k in ("depDateTime", "arrDateTime"))
                         + "；" + plain(s.get("seatClassName")))
    else:
        lines.append(f"{index}. " + plain(item.get("name") or item.get("title") or "搜索返回商品"))
    price = item.get("ticketPrice", item.get("price"))
    if price is not None:
        value = str(price)
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            value += "元"
        lines.append("搜索返回参考价：" + plain(value))
    for key, label in [("address", "地址"), ("interestsPoi", "周边"), ("nearbyPoi", "周边"), ("star", "类型"), ("sellPoint", "包含及限制"), ("benefit", "权益")]:
        if item.get(key):
            lines.append(label + "：" + plain(item[key]))
    if name == "search_poi":
        for key, label in [("category", "类别"), ("poiLevel", "景区等级")]:
            if item.get(key):
                lines.append(label + "：" + plain(item[key]))
        ticket = item.get("ticketInfo")
        if isinstance(ticket, dict):
            lines.append("门票：" + plain(ticket.get("ticketName")) + "；搜索返回参考价：" + plain(ticket.get("price") or "未提供"))
            for label, sku in (ticket.get("skuCommissionStruct") or {}).items():
                if isinstance(sku, dict) and sku.get("price") is not None:
                    lines.append(plain(label) + "：" + plain(sku["price"]) + "元")
        elif ticket:
            lines.append("门票信息：" + plain(ticket))
        if item.get("description"):
            lines.append("介绍：" + plain(item["description"]))
    picture = url(item.get("picUrl") or item.get("mainPic"))
    booking = url(item.get("jumpUrl") or item.get("detailUrl"))
    if picture:
        lines.append("![](" + picture + ")")
    if booking:
        lines.append("[点击预订](" + booking + ")")
    return "\n\n".join(lines)


def render_results(outcomes, limit=None, assumption=""):
    blocks, bindings = [], []
    if assumption:
        blocks.append(assumption)
    for outcome in outcomes:
        name, args = outcome["tool"], outcome["arguments"]
        blocks.append("查询条件：" + query_label(name, args))
        if not outcome.get("ok"):
            blocks.append("本次查询未完成：" + plain(outcome["error"]).replace("FlyAI", "查询服务") + "。不能据此判断没有库存。")
            continue
        result = outcome["result"]
        data = result.get("data")
        if isinstance(data, str):
            # ai-search already returns an upstream natural-language answer.
            # Keep its actual facts/conditions; neutralize supplier identity only.
            data = data.replace("飞猪平台", "查询平台").replace("飞猪数据", "查询数据").replace("经飞猪", "经查询服务").replace("FlyAI", "查询服务")
            data = data.replace("基于飞猪搜索结果", "基于本次搜索结果").replace("由飞猪提供的信息", "本次查询返回的信息")
            # Do not infer itinerary validity from arbitrary dates in prose:
            # opening notices and metro line numbers are not trip dates.
            record("source_content_passthrough", tool=name, conditions=outcome.get("conditions", {}))
            data = re.sub(r"<spot_entity>name:([^;]+);.*?</(?:spot|stop)_entity>", lambda m: plain(m[1]), data, flags=re.S)
            blocks.append(data if data.strip() else "上游本次返回空内容，未取得可展示的查询结果，不能据此判断没有相关商品或信息。")
        else:
            if name == "keyword_search":
                blocks.append("以下为关键词检索返回的商品，可能包含不同品类或规格；不代表已确认符合全部条件，价格及适用规格请核对详情。")
            # Every returned item is visible, in upstream order. Query/date/
            # category/price constraints are upstream search parameters, not
            # reasons for a second local exclusion. No default top-3 truncation.
            items = source_items(result)
            if not items:
                if data:
                    blocks.append("本次返回内容：\n\n" + json.dumps(data, ensure_ascii=False, indent=2))
                else:
                    blocks.append("上游本次未返回查询条目；这不代表机场、线路或商品不存在。")
            for index, item in enumerate(items, 1):
                blocks.append(render_item(name, item, index))
                bindings.append({"tool": name, "arguments": args, "item": item})
            record("source_items_passthrough", tool=name, returned_count=len(items), displayed_count=len(items), local_business_filter=False)
            if name in HOTELS and items:
                if not all(args.get(k) for k in ("check_in_date", "check_out_date")):
                    blocks.append("未指定完整入住与离店日期；以上仅为搜索参考信息，不代表指定日期的房价或可订库存。")
                bed = args.get("hotel_bed_types")
                if bed:
                    blocks.append("已按" + plain(bed) + "作为查询条件；返回列表未明确具体房型库存，实际床型请在详情确认。")
                blocks.append("早餐、取消和退改条件如未在返回字段中明确提供，需在商品详情确认；不据酒店名称推断。")
            if name == "search_marriott_package" and items:
                blocks.append("套餐适用日期、预约、补差、退改及是否含住宿，以各商品所列内容和详情为准；套餐搜索不保证指定入住日有房。")
        message = result.get("systemMessage")
        if message:
            blocks.append(plain(message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)))
    blocks.append("以上为本次搜索返回结果，请按各条实际日期、地点及规格核对；价格与可用情况可能变化，未执行预订或支付。")
    return neutralize_visible_text("\n\n".join(blocks)), bindings
