"""
OpenAI 兼容适配服务

对外：标准 OpenAI Chat Completions 协议（/v1/chat/completions、/v1/models）
对内：调用现有 SSE 接口，过滤自定义事件、聚合非流式结果、统一错误格式
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .conversation_log import ConversationAuditMiddleware, safe_headers
from .card_hints import CardHintFilter


def _env_bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- 配置
UPSTREAM_URL = os.getenv(
    "UPSTREAM_URL",
    "https://grayassistant.umetrip.com/gateway/api/web/sse/v1/chat/completions",
)
# 允许的模型，逗号分隔；留空表示不校验
ALLOWED_MODELS = [m.strip() for m in os.getenv("ALLOWED_MODELS", "qianrang").split(",") if m.strip()]
# 可选：对外单独发放的 key（逗号分隔）。设置后客户端用这些 key 访问，代理用 UPSTREAM_API_KEY 调上游；
# 留空则为透传模式：客户端的 Authorization 原样转发给上游鉴权
PROXY_API_KEYS = {k.strip() for k in os.getenv("PROXY_API_KEYS", "").split(",") if k.strip()}
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "")
CONNECT_TIMEOUT = float(os.getenv("UPSTREAM_CONNECT_TIMEOUT", "10"))
# 仅连接建立前的失败可安全重连；已收到响应、读写失败均不自动重放业务。
UPSTREAM_CONNECT_RETRIES = min(1, max(0, int(os.getenv("UPSTREAM_CONNECT_RETRIES", "1"))))
UPSTREAM_CONNECT_RETRY_DELAY = float(os.getenv("UPSTREAM_CONNECT_RETRY_DELAY", "0.25"))
# 包含连接、读取和聚合的整轮上限；0 可关闭。成功的慢响应允许在此期间完整返回。
UPSTREAM_TOTAL_TIMEOUT = float(os.getenv("UPSTREAM_TOTAL_TIMEOUT", "180"))
# 只在可选 keepalive 入口的普通文本流式请求启用可播报失败收尾。
VOICE_ERROR_FALLBACK_ENABLED = _env_bool("VOICE_ERROR_FALLBACK_ENABLED", False)
# 两次收到上游数据之间的最长等待；整轮生成时长不受此限制
READ_TIMEOUT = float(os.getenv("UPSTREAM_READ_TIMEOUT", "300"))
# 默认不插入或删除正文字符；显式设置时仍可选择段间分隔符。
TEXT_END_SEPARATOR = os.getenv("TEXT_END_SEPARATOR", "").replace("\\n", "\n")
# 旧配置兼容开关；默认保留全部段落，不把最后一段假定为唯一结论。
NONSTREAM_FINAL_SEGMENT_ONLY = _env_bool("NONSTREAM_FINAL_SEGMENT_ONLY", False)
# 删除自家 App 渲染用的卡片标签，例如 <flightstatuslist_card>{...}</flightstatuslist_card>
STRIP_CARDS = _env_bool("STRIP_CARDS", True)
STRIP_CARD_HINTS = _env_bool("STRIP_CARD_HINTS", False)
CARD_HINT_MAX_CHARS = int(os.getenv("CARD_HINT_MAX_CHARS", "128"))
CARD_HINT_MAX_WAIT_MS = float(os.getenv("CARD_HINT_MAX_WAIT_MS", "200"))
CARD_TAG_PATTERN = os.getenv("CARD_TAG_PATTERN", r"[A-Za-z0-9_]+_card")
# 把上游的思考过程（thinking_delta）转成 delta.reasoning_content 输出
EMIT_REASONING = _env_bool("EMIT_REASONING", True)
# 流式输出时，超过这么多秒没有任何输出就发一个空内容 chunk 保活；0 表示关闭
HEARTBEAT_INTERVAL = float(os.getenv("HEARTBEAT_INTERVAL", "3"))
# 只影响流式请求：尚无正文时，延迟发送一次等待提示；0 或空文本可关闭。
STREAM_WAIT_AFTER = float(os.getenv("STREAM_WAIT_AFTER", "6"))
STREAM_WAIT_MESSAGE = os.getenv("STREAM_WAIT_MESSAGE", "正在处理，请稍等。\n\n").replace("\\n", "\n")
# 仅可选 /v1/keepalive 入口：先等快速完整响应，慢响应按 JSON 空白保活。
JSON_KEEPALIVE_AFTER = float(os.getenv("JSON_KEEPALIVE_AFTER", "3"))
JSON_KEEPALIVE_INTERVAL = float(os.getenv("JSON_KEEPALIVE_INTERVAL", "3"))
# v2 文档指定上游用 session_id；客户端旧的 x-session-id 也可作为 ID 来源。
SESSION_HEADER = os.getenv("SESSION_HEADER", "session_id")
# 客户端只能发 messages 而不能带会话头时，可启用经验证的纯文本历史适配。
# 默认关闭；明确的会话头、工具调用和多模态记录始终透传。
PACK_HISTORY_WITHOUT_SESSION = _env_bool("PACK_HISTORY_WITHOUT_SESSION", False)
CONVERSATION_LOG_ENABLED = _env_bool("CONVERSATION_LOG_ENABLED", False)
CONVERSATION_LOG_DIR = os.getenv("CONVERSATION_LOG_DIR", "./logs/conversations")
CONVERSATION_LOG_MAX_BYTES = int(os.getenv("CONVERSATION_LOG_MAX_BYTES", str(16 * 1024 * 1024)))
CONVERSATION_LOG_RETENTION_DAYS = int(os.getenv("CONVERSATION_LOG_RETENTION_DAYS", "3"))
CONVERSATION_LOG_TOTAL_BYTES = int(os.getenv("CONVERSATION_LOG_TOTAL_BYTES", str(256 * 1024 * 1024)))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("openai-proxy")
logging.getLogger("httpx").setLevel(logging.WARNING)  # 不逐条记录上游请求

# 仅覆盖 stream，其他请求字段（包括 stream_options）原样交给上游。
_STRIP_FIELDS = {"stream"}


def adapt_text_history(messages: List[Any], session_source: str) -> Tuple[List[Any], str]:
    """仅将没有稳定会话标识的普通文本历史附在本轮输入；不推测或共享会话。"""
    if not PACK_HISTORY_WITHOUT_SESSION or session_source != "generated":
        return messages, "original"
    if not all(isinstance(message, dict) and set(message) <= {"role", "content", "name"}
               and isinstance(message.get("content"), str)
               and message.get("role") in ("system", "developer", "user", "assistant")
               for message in messages):
        return messages, "original"
    # 只处理位于对话开头的系统/开发者消息，保持其独立角色及优先级。
    prefix = 0
    while prefix < len(messages) and messages[prefix]["role"] in ("system", "developer"):
        prefix += 1
    history = messages[prefix:]
    if (len(history) < 2 or history[-1]["role"] != "user"
            or any(message["role"] not in ("user", "assistant") for message in history)):
        return messages, "original"
    text = ("你需要处理下面的聊天记录。之前的消息仅作为上下文，请回答最后一条user消息，"
            "不要重新回答较早消息。\n<chat_history>\n"
            + json.dumps(history, ensure_ascii=False) + "\n</chat_history>")
    return messages[:prefix] + [{"role": "user", "content": text}], "packed_text"


# ---------------------------------------------------------------- 工具函数
def openai_error(status: int, message: str, code: str, err_type: str = "invalid_request_error") -> JSONResponse:
    return JSONResponse(
        {"error": {"message": message, "type": err_type, "param": None, "code": code}},
        status_code=status,
        headers={"X-Should-Retry": "false"},
    )


def map_upstream_error(status: int, raw: bytes) -> JSONResponse:
    """把上游的非 200 响应转成 OpenAI 错误格式。"""
    message = f"Upstream returned HTTP {status}"
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            message = data.get("message") or data.get("msg") or message
    except ValueError:
        pass  # 非 JSON（如 CDN 的 HTML 错误页）只保留状态码
    if status in (401, 403):
        return openai_error(401, message, "invalid_api_key")
    if status == 429:
        return openai_error(429, message, "rate_limit_exceeded", "rate_limit_error")
    if status == 504:
        return openai_error(504, message, "upstream_timeout", "server_error")
    if 400 <= status < 500:
        return openai_error(status, message, "upstream_bad_request")
    return openai_error(502, message, "upstream_error", "server_error")


def sse(data: Any) -> bytes:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n".encode("utf-8")


class CardStripper:
    """流式删除 <xxx_card>...</xxx_card> 标签。标签可能被拆在多个 chunk 里，可疑的部分先缓冲。"""

    _MAX_PENDING_TAG = 64  # 开标签最长长度，超过就当普通文本

    def __init__(self, name_pattern: str):
        self._open_full = re.compile(r"<(" + name_pattern + r")>")
        self._open_prefix = re.compile(r"<[A-Za-z0-9_]*$")
        self._buf = ""

    def feed(self, text: str) -> str:
        self._buf += text
        out: List[str] = []
        while self._buf:
            i = self._buf.find("<")
            if i < 0:
                out.append(self._buf)
                self._buf = ""
                break
            out.append(self._buf[:i])
            self._buf = self._buf[i:]
            m = self._open_full.match(self._buf)
            if m:
                close = f"</{m.group(1)}>"
                j = self._buf.find(close, m.end())
                if j < 0:
                    break  # 卡片还没结束，继续缓冲
                self._buf = self._buf[j + len(close):]
                continue
            if self._open_prefix.match(self._buf) and len(self._buf) < self._MAX_PENDING_TAG:
                break  # 可能是开标签的前半截，等后续内容
            out.append("<")  # 普通的 "<"
            self._buf = self._buf[1:]
        return "".join(out)

    def flush(self) -> str:
        """结束时调用：没闭合的卡片丢弃，其余原样输出。"""
        buf, self._buf = self._buf, ""
        if self._open_full.match(buf):
            return ""
        return buf


class UpstreamStreamError(Exception):
    def __init__(self, message: str, code: str = "upstream_error"):
        super().__init__(message)
        self.code = code


def record_upstream_stream_error(upstream_req: httpx.Request, exc: Exception, code: str):
    """把流读取异常写入同一份问答记录，不依赖系统日志拼接根因。"""
    trace = upstream_req.extensions.get("conversation_trace")
    if trace is not None:
        # 不保存异常原文，避免把连接信息或上游内部异常夹带到记录中。
        trace.record("upstream_stream_error", error_type=type(exc).__name__,
                     code=code, source_complete=False)


def strip_cards(value: Any) -> Any:
    """处理完整业务结果中的卡片；正文分片使用独立的持续过滤器。"""
    if not STRIP_CARDS:
        return value
    if isinstance(value, str):
        stripper = CardStripper(CARD_TAG_PATTERN)
        return stripper.feed(value) + stripper.flush()
    if isinstance(value, list):
        return [strip_cards(item) for item in value]
    if isinstance(value, dict):
        return {key: strip_cards(item) for key, item in value.items()}
    return value


class UpstreamStream:
    """
    读取上游 SSE，转成标准 chat.completion.chunk：
      - 正文 chunk：仅删除卡片，保留全部段落及空白，统一 id/created/model
      - thinking_delta：转成 delta.reasoning_content（可关闭）
      - 隐藏耗时、准备和启动通知；业务结果转换成 content 文本块
      - 只有收到 [DONE] 才提交终止块，断流或错误不伪装成功
    chunks() 产出 (段号, chunk)，流式和非流式消费相同的结果。
    """

    def __init__(self, resp: httpx.Response, request_id: str, model: str, allow_hint_filter: bool = True):
        self.resp = resp
        self.trace = resp.extensions.get("conversation_trace")
        self.request_id = request_id
        self.id = f"chatcmpl-{request_id}"
        self.created = int(time.time())
        self.model = model
        self.usage: Optional[Dict[str, Any]] = None
        self.ttft_ms: Optional[int] = None
        self.segment = 0
        self._segment_has_text = False
        self._any_text = False
        self._pending_text_sep = False
        self._stripper = CardStripper(CARD_TAG_PATTERN) if STRIP_CARDS else None
        self._other_strippers: Dict[str, CardStripper] = {}
        self._hints = CardHintFilter(CARD_HINT_MAX_CHARS, CARD_HINT_MAX_WAIT_MS / 1000,
                                    self._record_hint_removal) if STRIP_CARDS and STRIP_CARD_HINTS and allow_hint_filter else None
        self.done = False
        self.finish_reason: Optional[str] = None
        self.has_payload = False
        self.metadata: Dict[str, Any] = {}
        # 只记录时间和数量，用于区分上游沉默、先思考、先工具调用与正文生成。
        self.first_event_at: Optional[float] = None
        self.first_reasoning_at: Optional[float] = None
        self.first_text_at: Optional[float] = None
        self.source_events = self.tool_starts = self.tool_results = 0
        self.reasoning_chars = 0

    def diagnostic_fields(self, started: float) -> str:
        def elapsed(value):
            return round((value - started) * 1000) if value is not None else None
        return (f" first_upstream_event_ms={elapsed(self.first_event_at)}"
                f" first_reasoning_ms={elapsed(self.first_reasoning_at)}"
                f" first_upstream_text_ms={elapsed(self.first_text_at)}"
                f" reasoning_chars={self.reasoning_chars} source_events={self.source_events}"
                f" tool_starts={self.tool_starts} tool_results={self.tool_results}"
                + (f" card_hints_removed={self._hints.removals} hint_timeouts={self._hints.timeouts}"
                   f" hint_max_hold_ms={self._hints.max_held_ms:.3f} hint_cpu_ms={self._hints.cpu_ns / 1e6:.3f}"
                   if self._hints else ""))

    def _chunk(self, delta: Dict[str, Any], finish_reason: Optional[str] = None) -> Dict[str, Any]:
        return {
            "id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
            "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}],
            **self.metadata,
        }

    def heartbeat(self) -> Dict[str, Any]:
        return self._chunk({"content": ""})

    def _text(self, text: str) -> str:
        """正文文本经过卡片过滤和段间分隔后的结果。"""
        if text and self.first_text_at is None:
            self.first_text_at = time.monotonic()
        if self._stripper:
            text = self._stripper.feed(text)
        if self._hints:
            text = self._hints.feed(text)
        return self._decorate(text)

    def _record_hint_removal(self, **fields):
        if self.trace is not None:
            self.trace.record("proxy_card_hint_removed", offset_scope="model_content_after_card_removal", **fields)

    def _flush_hint(self, complete=False):
        return self._decorate(self._hints.flush(complete)) if self._hints else ""

    def _decorate(self, text: str) -> str:
        if not text:
            return ""
        if self._pending_text_sep and self._any_text:
            text = TEXT_END_SEPARATOR + text
        self._pending_text_sep = False
        self._segment_has_text = self._any_text = True
        return text

    def _flush_text(self) -> str:
        text = self._stripper.flush() if self._stripper else ""
        if self._hints:
            text = self._hints.feed(text) + self._hints.flush(complete=True)
        return self._decorate(text)

    def _other_text(self, field: str, value: str) -> str:
        if not STRIP_CARDS:
            return value
        stripper = self._other_strippers.setdefault(field, CardStripper(CARD_TAG_PATTERN))
        return stripper.feed(value)

    def _finish(self) -> List[Tuple[int, Dict[str, Any]]]:
        if self.finish_reason is None:
            raise UpstreamStreamError("Upstream ended without a finish_reason", "incomplete_stream")
        if not self.has_payload:
            raise UpstreamStreamError("Upstream returned no answer", "empty_completion")
        delta = {}
        text = self._flush_text()
        if text:
            delta["content"] = text
        for field, stripper in self._other_strippers.items():
            tail = stripper.flush()
            if tail:
                delta[field] = tail
        out = [(self.segment, self._chunk(delta))] if delta else []
        out.append((self.segment, self._chunk({}, self.finish_reason)))
        return out

    async def _source_lines(self):
        # A timer must wake even if the next upstream line never arrives. Shield the
        # one pending read instead of cancelling httpx's iterator on each deadline.
        iterator = self.resp.aiter_lines().__aiter__()
        pending_read = None
        try:
            while True:
                deadline = self._hints.deadline if self._hints else None
                if pending_read is None and deadline is None:
                    yield await iterator.__anext__()
                    continue
                if pending_read is None:
                    pending_read = asyncio.create_task(iterator.__anext__())
                timeout = max(0, deadline - time.monotonic()) if deadline is not None else None
                ready, _ = await asyncio.wait({pending_read}, timeout=timeout)
                if not ready:
                    yield None
                    continue
                result, pending_read = pending_read, None
                yield result.result()
        except StopAsyncIteration:
            return
        finally:
            if pending_read is not None:
                pending_read.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await pending_read
            await iterator.aclose()

    async def chunks(self) -> AsyncIterator[Tuple[int, Dict[str, Any]]]:
        event: Optional[str] = None
        data_lines: List[str] = []
        lines = self._source_lines()
        try:
            async for line in lines:
                if line is None:
                    text = self._decorate(self._hints.expire())
                    if text:
                        yield self.segment, self._chunk({"content": text})
                    continue
                if self.trace is not None:
                    self.trace.record("upstream_sse_line", text=line)
                if line == "":
                    if data_lines:
                        for item in self._handle(event, "\n".join(data_lines)):
                            yield item
                        if self.done:
                            return
                    event, data_lines = None, []
                    continue
                if line.startswith(":"):
                    continue
                field, _, value = line.partition(":")
                value = value[1:] if value.startswith(" ") else value
                if field == "event":
                    event = value.strip()
                elif field == "data":
                    data_lines.append(value)
            if data_lines:
                for item in self._handle(event, "\n".join(data_lines)):
                    yield item
            if not self.done:
                raise UpstreamStreamError("Upstream disconnected before [DONE]", "incomplete_stream")
        except Exception:
            # Failure must not silently discard buffered, unconfirmed business text.
            tail = self._flush_hint()
            if tail:
                yield self.segment, self._chunk({"content": tail})
            raise
        finally:
            await lines.aclose()
            if self._hints and self.trace is not None:
                self.trace.record("proxy_card_hint_summary", removed=self._hints.removals,
                                  timeouts=self._hints.timeouts, overflows=self._hints.overflows,
                                  max_pending_chars=self._hints.max_pending_chars,
                                  max_hold_ms=round(self._hints.max_held_ms, 3),
                                  scan_cpu_ms=round(self._hints.cpu_ns / 1e6, 4))

    def _handle(self, event: Optional[str], data: str) -> List[Tuple[int, Dict[str, Any]]]:
        if self.first_event_at is None:
            self.first_event_at = time.monotonic()
        self.source_events += 1
        if event and event != "message":
            return self._handle_event(event, data)
        if data.strip() == "[DONE]":
            if self.trace is not None:
                self.trace.record("upstream_done")
            out = self._finish()
            self.done = True
            return out
        try:
            raw = json.loads(data)
        except ValueError:
            raise UpstreamStreamError("Upstream returned invalid JSON", "invalid_upstream_json") from None
        if not isinstance(raw, dict):
            raise UpstreamStreamError("Upstream returned a non-object chunk", "invalid_upstream_response")
        if raw.get("error") or raw.get("type") == "error":
            raise UpstreamStreamError(json.dumps(raw.get("error") or raw, ensure_ascii=False))
        event_type = raw.get("event_type") or raw.get("type")
        if event_type in ("prepare_model_input", "ttft", "thinking_delta", "thinking_end", "tool_call_start", "tool_result_end", "text_end") and "choices" not in raw:
            return self._handle_event(event_type, data)
        if raw.get("usage") is not None:
            usage = raw["usage"]
            if not isinstance(usage, dict) or not all(
                isinstance(usage.get(k), int) and not isinstance(usage[k], bool) and usage[k] >= 0
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            ):
                raise UpstreamStreamError("Upstream returned invalid usage", "invalid_upstream_response")
            self.usage = usage
        for key in ("system_fingerprint", "service_tier"):
            if key in raw:
                self.metadata[key] = raw[key]
        if "choices" not in raw:
            if "usage" in raw and set(raw) <= {"usage", "id", "object", "created", "model"}:
                return []
            return self._business(raw)
        # 这些字段不属于包装元数据；有新增业务数据时不能静默丢弃。
        extra = {k: v for k, v in raw.items() if k not in {
            "id", "object", "created", "model", "usage", "choices", "system_fingerprint", "service_tier"}}
        out = self._business(extra) if extra else []
        if not isinstance(raw["choices"], list):
            raise UpstreamStreamError("Upstream choices must be an array", "invalid_upstream_response")

        for choice in raw.get("choices") or []:
            if not isinstance(choice, dict) or choice.get("index", 0) != 0:
                raise UpstreamStreamError("Only one upstream choice is supported", "invalid_upstream_response")
            src = choice.get("delta") or {}
            if not isinstance(src, dict):
                raise UpstreamStreamError("Upstream delta must be an object", "invalid_upstream_response")
            finish = choice.get("finish_reason")
            if finish is not None:
                if not isinstance(finish, str) or finish not in {"stop", "length", "content_filter", "tool_calls", "function_call"}:
                    raise UpstreamStreamError("Unsupported upstream finish_reason", "invalid_upstream_response")
                self.finish_reason = finish
            delta: Dict[str, Any] = {}
            for key, value in src.items():
                if key in {"content", "reasoning_content", "refusal"}:
                    if value is not None and not isinstance(value, str):
                        raise UpstreamStreamError("Upstream returned non-text content", "invalid_upstream_response")
                    if key == "reasoning_content" and value:
                        if self.first_reasoning_at is None:
                            self.first_reasoning_at = time.monotonic()
                        self.reasoning_chars += len(value)
                    if key == "reasoning_content" and not EMIT_REASONING:
                        continue
                    if value:
                        self.has_payload = True
                        cleaned = self._text(value) if key == "content" else self._other_text(key, value)
                        if cleaned:
                            delta[key] = cleaned
                elif key in {"role", "tool_calls", "function_call", "annotations"}:
                    if key in {"tool_calls", "annotations"} and value is not None and not isinstance(value, list):
                        raise UpstreamStreamError("Invalid upstream " + key, "invalid_upstream_response")
                    if key == "tool_calls" and value is not None:
                        for call in value:
                            if not isinstance(call, dict) or type(call.get("index", 0)) is not int:
                                raise UpstreamStreamError("Invalid upstream tool call", "invalid_upstream_response")
                            for field in ("function", "custom"):
                                if field in call and not isinstance(call[field], dict):
                                    raise UpstreamStreamError("Invalid upstream tool call", "invalid_upstream_response")
                    if key == "function_call" and value is not None and not isinstance(value, dict):
                        raise UpstreamStreamError("Invalid upstream function_call", "invalid_upstream_response")
                    # 函数参数不是展示卡片，不能改写其字符串，否则会改变调用语义。
                    delta[key] = value if key in {"tool_calls", "function_call"} else strip_cards(value)
                    if key in {"tool_calls", "function_call"} and value:
                        self.has_payload = True
                else:
                    # 未知扩展字段用标准 content 保留，不要求平台认识新字段。
                    out.extend(self._business({key: value}))
            if delta or choice.get("logprobs") is not None:
                if choice.get("logprobs") is not None and not isinstance(choice["logprobs"], dict):
                    raise UpstreamStreamError("Invalid upstream logprobs", "invalid_upstream_response")
                chunk = self._chunk(delta)
                chunk["choices"][0]["logprobs"] = choice.get("logprobs")
                out.append((self.segment, chunk))
            choice_extra = {k: v for k, v in choice.items() if k not in {"index", "delta", "finish_reason", "logprobs"}}
            if choice_extra:
                out.extend(self._business(choice_extra))
        return out

    def _business(self, value: Any) -> List[Tuple[int, Dict[str, Any]]]:
        tail = self._flush_hint()
        out = [(self.segment, self._chunk({"content": tail}))] if tail else []
        if value is None or value == {} or value == "":
            return out
        self.has_payload = True
        cleaned = strip_cards(value)
        text = cleaned if isinstance(cleaned, str) else json.dumps(cleaned, ensure_ascii=False, indent=2)
        if not text:
            return out
        # 独立业务结果必须与模型正文分开，内部字段不伪造成客户端待执行的 tool_calls。
        self._segment_has_text = self._any_text = True
        self._pending_text_sep = False
        return out + [(self.segment, self._chunk({"content": "\n\n" + text + "\n\n"}))]

    def _handle_event(self, event: str, data: str) -> List[Tuple[int, Dict[str, Any]]]:
        if event == "tool_call_start":
            self.tool_starts += 1
        elif event == "tool_result_end":
            self.tool_results += 1
        if STRIP_CARDS and re.fullmatch(CARD_TAG_PATTERN, event):
            self.has_payload = True
            return []
        payload: Any = None
        try:
            payload = json.loads(data)
        except ValueError:
            if event == "error":
                raise UpstreamStreamError(data)
            raise UpstreamStreamError("Upstream event contains invalid JSON", "invalid_upstream_json") from None
        if event == "ttft":
            if self.ttft_ms is None and isinstance(payload, dict):
                self.ttft_ms = payload.get("duration_ms")
        elif event == "thinking_delta":
            text = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(text, str) and text:
                if self.first_reasoning_at is None:
                    self.first_reasoning_at = time.monotonic()
                self.reasoning_chars += len(text)
            if EMIT_REASONING and isinstance(text, str) and text:
                self.has_payload = True
                text = self._other_text("reasoning_content", text)
                return [(self.segment, self._chunk({"reasoning_content": text}))] if text else []
            if EMIT_REASONING and text is not None and not isinstance(text, str):
                return self._business(text)
        elif event in {"prepare_model_input", "thinking_end", "tool_call_start"}:
            return []
        elif event == "text_end":
            tail = self._flush_hint(complete=True)
            out = [(self.segment, self._chunk({"content": tail}))] if tail else []
            if self._segment_has_text:
                self.segment += 1
                self._segment_has_text = False
                self._pending_text_sep = True
            if isinstance(payload, dict) and payload.get("data") is not None:
                return out + self._business(payload["data"])
            return out
        elif event == "error":
            raise UpstreamStreamError(data)
        elif event == "tool_result_end":
            value = payload.get("data") if isinstance(payload, dict) else payload
            if isinstance(value, dict):
                value = {k: v for k, v in value.items() if k not in {"name", "id", "call_id", "tool_key", "parameters"}}
                if value.get("content") == "查询结果：是否成功" and set(value) <= {"content", "state"} and value.get("state") in (None, "success"):
                    return []  # 已观察到的占位状态通知，没有业务结果
                if set(value) == {"content"}:
                    value = value["content"]
            return self._business(value)
        else:
            # 新的确认、订单、链接等事件保留数据，避免默认过滤业务结果。
            return self._business(payload)
        return []


async def open_upstream(client: httpx.AsyncClient, upstream_req: httpx.Request,
                        request_id: str) -> Tuple[Optional[httpx.Response], Optional[JSONResponse]]:
    """两种下游模式共用上游鉴权、错误映射和连接清理。"""
    trace = upstream_req.extensions.get("conversation_trace")
    for attempt in range(1, UPSTREAM_CONNECT_RETRIES + 2):
        if trace is not None:
            trace.record("upstream_attempt", attempt=attempt)
        try:
            resp = await client.send(upstream_req, stream=True)
            break
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout) as exc:
            if attempt <= UPSTREAM_CONNECT_RETRIES:
                log.warning("request_id=%s stage=upstream_connect_retry attempt=%d error_type=%s",
                            request_id, attempt, type(exc).__name__)
                if trace is not None:
                    trace.record("upstream_connect_retry", attempt=attempt, error_type=type(exc).__name__)
                await asyncio.sleep(max(0, UPSTREAM_CONNECT_RETRY_DELAY))
                continue
            if trace is not None:
                trace.record("upstream_connection_error", attempt=attempt, error_type=type(exc).__name__)
            timeout = isinstance(exc, (httpx.ConnectTimeout, httpx.PoolTimeout))
            return None, openai_error(504 if timeout else 502, "Upstream timeout" if timeout else "Upstream unavailable",
                                     "upstream_timeout" if timeout else "upstream_unavailable", "server_error")
        except httpx.TimeoutException as exc:
            if trace is not None:
                trace.record("upstream_connection_error", attempt=attempt, error_type=type(exc).__name__)
            return None, openai_error(504, "Upstream timeout", "upstream_timeout", "server_error")
        except httpx.HTTPError as exc:
            if trace is not None:
                trace.record("upstream_connection_error", attempt=attempt, error_type=type(exc).__name__)
            return None, openai_error(502, "Upstream unavailable", "upstream_unavailable", "server_error")
    if trace is not None:
        resp.extensions["conversation_trace"] = trace
        trace.record("upstream_response", status=resp.status_code, headers=safe_headers(resp.headers))
    if resp.status_code != 200:
        try:
            raw = await resp.aread()
            if trace is not None:
                trace.record("upstream_error_body", text=raw.decode("utf-8", errors="replace"))
        except httpx.TimeoutException:
            return None, openai_error(504, "Upstream timeout", "upstream_timeout", "server_error")
        except httpx.HTTPError:
            return None, openai_error(502, "Upstream unavailable", "upstream_unavailable", "server_error")
        finally:
            await resp.aclose()
        log.warning("request_id=%s upstream status=%s", request_id, resp.status_code)
        return None, map_upstream_error(resp.status_code, raw)
    if "text/event-stream" not in resp.headers.get("content-type", "").lower():
        await resp.aclose()
        return None, openai_error(502, "Upstream did not return text/event-stream", "invalid_content_type", "server_error")
    return resp, None


def streaming_response(client: httpx.AsyncClient, upstream_req: httpx.Request,
                       request_id: str, model: str, started: float,
                       include_usage: bool, allow_wait_notice: bool = True, voice_errors: bool = False) -> StreamingResponse:
    """先建立合法 SSE，再等待上游；不占用结果队列，不把等待提示当作完成。"""
    response_id, created = f"chatcmpl-{request_id}", int(time.time())
    wait_after = STREAM_WAIT_AFTER if allow_wait_notice else 0

    async def gen() -> AsyncIterator[bytes]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        resp: Optional[httpx.Response] = None
        upstream: Optional[UpstreamStream] = None
        headers_ms = first_content_ms = None

        def chunk(delta: Dict[str, Any], finish_reason: Optional[str] = None) -> Dict[str, Any]:
            item = upstream._chunk(delta, finish_reason) if upstream else {
                "id": response_id, "object": "chat.completion.chunk", "created": created, "model": model,
                "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}],
            }
            if include_usage:
                item["usage"] = None
            return item

        async def read_source():
            nonlocal resp, upstream, headers_ms
            resp, error = await open_upstream(client, upstream_req, request_id)
            headers_ms = round((time.monotonic() - started) * 1000)
            if error is not None:
                await queue.put(("mapped_error", json.loads(error.body)))
                return
            assert resp is not None
            upstream = UpstreamStream(resp, request_id, model,
                                      allow_hint_filter=upstream_req.extensions.get("card_hint_filter_allowed", True))
            upstream.created = created
            async for _, item in upstream.chunks():
                await queue.put(("chunk", item))
            await queue.put(("done", None))

        async def produce():
            try:
                if UPSTREAM_TOTAL_TIMEOUT > 0:
                    await asyncio.wait_for(read_source(), UPSTREAM_TOTAL_TIMEOUT)
                else:
                    await read_source()
            except asyncio.TimeoutError:
                await queue.put(("error", UpstreamStreamError("Upstream total response timeout", "upstream_total_timeout")))
            except Exception as exc:
                await queue.put(("error", exc))

        task = asyncio.create_task(produce())
        status, heartbeats, notice_sent, answered, finished = "ok", 0, False, False, False
        machine_output = False

        def failure_packets(error):
            # 语音兼容入口生成明确的本地失败说明；日志仍记上游失败，绝不伪造业务结果。
            # JSON/音频格式和部分原生工具参数不能用自然语言收尾。
            if not (voice_errors and allow_wait_notice and not machine_output):
                return [sse(error)]
            code = error["error"].get("code", "upstream_error")
            if answered:
                message = "\n\n出行服务连接中断，以上回答尚未完整返回，最终结果未确认。"
            elif code in {"upstream_total_timeout", "upstream_timeout"}:
                message = "\n出行服务响应超时，本次未能取得完整结果。涉及业务办理时，请先核实办理状态。"
            elif code == "invalid_api_key":
                message = "\n出行服务暂时不可用，请联系管理员检查接口配置。"
            else:
                message = "\n出行服务暂时异常，本次未能取得完整结果。涉及业务办理时，请先核实办理状态。"
            trace = upstream_req.extensions.get("conversation_trace")
            if trace is not None:
                trace.record("proxy_voice_failure", code=code, partial_answer=answered,
                             source_complete=False, text=message)
            log.warning("request_id=%s stage=voice_error_fallback code=%s partial_answer=%s source_complete=False",
                        request_id, code, answered)
            packets = [sse(chunk({"content": message})), sse(chunk({}, "stop"))]
            if include_usage:
                packets.append(sse({"id": response_id, "object": "chat.completion.chunk", "created": created,
                                    "model": model, "choices": [], "usage": None}))
            packets.append(b"data: [DONE]\n\n")
            return packets

        last_output = time.monotonic()
        notice_at = started + wait_after
        try:
            yield sse(chunk({"role": "assistant", "content": ""}))
            while True:
                deadlines = []
                if HEARTBEAT_INTERVAL > 0:
                    deadlines.append(last_output + HEARTBEAT_INTERVAL)
                if wait_after > 0 and STREAM_WAIT_MESSAGE and not answered and not notice_sent:
                    deadlines.append(notice_at)
                timeout = max(0.001, min(deadlines) - time.monotonic()) if deadlines else None
                try:
                    if timeout is None:
                        kind, item = await queue.get()
                    else:
                        kind, item = await asyncio.wait_for(queue.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    # 超时边界已有真正结果时，先消费结果，避免多余提示。
                    try:
                        kind, item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        now = time.monotonic()
                        if (wait_after > 0 and STREAM_WAIT_MESSAGE and not answered
                                and not notice_sent and now >= notice_at):
                            notice_sent = True
                            trace = upstream_req.extensions.get("conversation_trace")
                            if trace is not None:
                                trace.record("proxy_wait_notice", text=STREAM_WAIT_MESSAGE)
                            last_output = now
                            yield sse(chunk({"content": STREAM_WAIT_MESSAGE}))
                        elif HEARTBEAT_INTERVAL > 0 and now >= last_output + HEARTBEAT_INTERVAL:
                            heartbeats += 1
                            last_output = now
                            yield sse(chunk({"content": ""}))
                        continue
                if kind == "done":
                    break
                if kind == "mapped_error":
                    status = item["error"].get("code", "upstream_error")
                    for packet in failure_packets(item):
                        yield packet
                    break
                if kind == "error":
                    status = type(item).__name__
                    if isinstance(item, UpstreamStreamError):
                        msg, code = str(item), item.code
                    elif isinstance(item, httpx.TimeoutException):
                        msg, code = "Upstream read timeout", "upstream_timeout"
                    else:
                        log.error("request_id=%s stream error: %s", request_id, type(item).__name__)
                        msg, code = "Upstream connection error", "upstream_error"
                    record_upstream_stream_error(upstream_req, item, code)
                    for packet in failure_packets({"error": {"message": msg, "type": "server_error", "param": None, "code": code}}):
                        yield packet
                    break
                machine_output = machine_output or any(
                    any(choice.get("delta", {}).get(field) for field in ("tool_calls", "function_call"))
                    for choice in item.get("choices", []))
                has_answer = any(
                    any(choice.get("delta", {}).get(field) for field in ("content", "refusal", "tool_calls", "function_call"))
                    for choice in item.get("choices", [])
                )
                if has_answer and not answered:
                    first_content_ms = round((time.monotonic() - started) * 1000)
                answered = answered or has_answer
                if include_usage:
                    item["usage"] = None
                last_output = time.monotonic()
                yield sse(item)
            if status == "ok":
                assert upstream is not None
                if include_usage:
                    yield sse({"id": response_id, "object": "chat.completion.chunk", "created": created,
                               "model": model, "choices": [], "usage": upstream.usage})
                finished = True
                yield b"data: [DONE]\n\n"
        finally:
            if not finished and status == "ok":
                status = "client_disconnected"
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            if resp is not None:
                await resp.aclose()
            log.info("request_id=%s model=%s stream=True status=%s upstream_headers_ms=%s first_content_ms=%s "
                     "total_ms=%d heartbeats=%d wait_notice=%s%s", request_id, model, status, headers_ms,
                     first_content_ms, (time.monotonic() - started) * 1000, heartbeats, notice_sent,
                     upstream.diagnostic_fields(started) if upstream else "")

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "X-Request-Id": request_id,
        "X-Should-Retry": "false",
    })


# ---------------------------------------------------------------- 应用
@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=30, pool=10),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=50),
    )
    log.info("upstream=%s allowed_models=%s auth_mode=%s strip_cards=%s reasoning=%s heartbeat=%ss",
             UPSTREAM_URL, ALLOWED_MODELS or "*", "proxy-keys" if PROXY_API_KEYS else "passthrough",
             STRIP_CARDS, EMIT_REASONING, HEARTBEAT_INTERVAL)
    yield
    await app.state.client.aclose()


app = FastAPI(title="OpenAI Compatible Proxy", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def trace_http_request(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    request_id = getattr(request.state, "proxy_request_id", None) or uuid.uuid4().hex
    request.state.proxy_request_id = request_id
    started = time.monotonic()
    log.info("request_id=%s stage=http_received method=%s path=%s client=%s", request_id,
             request.method, request.url.path, request.client.host if request.client else "unknown")
    try:
        response = await call_next(request)
    except Exception as exc:
        log.error("request_id=%s stage=http_exception error_type=%s total_ms=%d", request_id,
                  type(exc).__name__, (time.monotonic() - started) * 1000)
        raise
    response.headers["X-Request-Id"] = request_id
    log.info("request_id=%s stage=http_headers http_status=%d headers_ms=%d", request_id,
             response.status_code, (time.monotonic() - started) * 1000)
    return response


def resolve_upstream_auth(request: Request) -> Optional[str]:
    """返回转发给上游的 Authorization；返回 None 表示鉴权失败。"""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer ") or not auth[7:].strip():
        return None
    if PROXY_API_KEYS:
        if auth[7:].strip() not in PROXY_API_KEYS:
            return None
        return f"Bearer {UPSTREAM_API_KEY}"
    return auth


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/v1/models")
@app.get("/v1/keepalive/models")
async def list_models(request: Request):
    if resolve_upstream_auth(request) is None:
        return openai_error(401, "Invalid or missing API key", "invalid_api_key")
    return {
        "object": "list",
        "data": [{"id": m, "object": "model", "created": 0, "owned_by": "umetrip"} for m in ALLOWED_MODELS],
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    request_id = getattr(request.state, "proxy_request_id", None) or uuid.uuid4().hex
    request.state.proxy_request_id = request_id
    started = time.monotonic()

    upstream_auth = resolve_upstream_auth(request)
    if upstream_auth is None:
        return openai_error(401, "Invalid or missing API key", "invalid_api_key")

    try:
        body = await request.json()
    except ValueError:
        return openai_error(400, "Request body must be valid JSON", "invalid_json")
    if not isinstance(body, dict):
        return openai_error(400, "Request body must be a JSON object", "invalid_json")

    model = body.get("model")
    if not model:
        return openai_error(400, "'model' is required", "missing_model")
    if ALLOWED_MODELS and model not in ALLOWED_MODELS:
        return openai_error(404, f"The model '{model}' does not exist", "model_not_found")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return openai_error(400, "'messages' must be a non-empty array", "invalid_messages")

    stream = body.get("stream", False)
    options = body.get("stream_options")
    if options is None:
        options = {}
    if not isinstance(stream, bool):
        return openai_error(400, "'stream' must be a boolean", "invalid_stream")
    if not isinstance(options, dict) or not isinstance(options.get("include_usage", False), bool):
        return openai_error(400, "'stream_options.include_usage' must be a boolean", "invalid_stream_options")
    if type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
        return openai_error(400, "Only n=1 is supported", "unsupported_n")
    include_usage = options.get("include_usage", False)

    session_id = (request.headers.get("session_id") or request.headers.get("x-session-id")
                  or request.headers.get(SESSION_HEADER) or request_id)
    session_source = next((name for name in ("session_id", "x-session-id", SESSION_HEADER)
                           if request.headers.get(name)), "generated")
    upstream_messages, history_mode = adapt_text_history(messages, session_source)
    thinking = body.get("thinking")
    thinking_mode = "enabled" if thinking is True else "disabled" if thinking is False else "unspecified"
    if isinstance(thinking, dict):
        thinking_mode = thinking.get("type") if thinking.get("type") in ("enabled", "disabled") else "nonstandard"
    effort = body.get("reasoning_effort")
    effort_label = effort if isinstance(effort, str) and effort in {"none", "minimal", "low", "medium", "high", "xhigh"} else "unspecified" if effort is None else "nonstandard"
    # journald 只保留结构和摘要；显式启用的会话记录另存到私有目录。
    shapes = []
    last_user_text = None
    for message in messages:
        if not isinstance(message, dict):
            shapes.append("invalid")
            continue
        role = message.get("role")
        role = role if isinstance(role, str) and role in {"system", "developer", "user", "assistant", "tool", "function"} else "other"
        content = message.get("content")
        kind = "text" if isinstance(content, str) else "parts" if isinstance(content, list) else "null" if content is None else "other"
        shapes.append(f"{role}:{kind}:{len(content) if isinstance(content, (str, list)) else 0}")
        if role == "user" and isinstance(content, str):
            last_user_text = content
    last_user_hash = hashlib.sha256(last_user_text.encode()).hexdigest()[:16] if last_user_text is not None else "none"
    # 常规运行日志不记录正文、原始会话 ID 或密钥。
    log.info("request_id=%s stage=accepted path=%s model=%s stream=%s thinking_mode=%s reasoning_effort=%s "
             "session_source=%s session_hash=%s messages=%d user_messages=%d history_mode=%s "
             "upstream_messages=%d message_shapes=%s last_user_hash=%s",
             request_id, request.url.path, model, stream, thinking_mode, effort_label, session_source,
             hashlib.sha256(session_id.encode()).hexdigest()[:16], len(messages),
             sum(isinstance(message, dict) and message.get("role") == "user" for message in messages),
             history_mode, len(upstream_messages), ",".join(shapes[-12:]), last_user_hash)

    upstream_body = {k: v for k, v in body.items() if k not in _STRIP_FIELDS}
    upstream_body["messages"] = upstream_messages
    upstream_body["stream"] = True  # 上游只支持流式，统一按流式读取

    client: httpx.AsyncClient = request.app.state.client
    upstream_req = client.build_request(
        "POST",
        UPSTREAM_URL,
        json=upstream_body,
        headers={
            "Authorization": upstream_auth,
            "Accept": "text/event-stream",
            # 传递客户端会话标识；是否真正隔离，仍由上游实现决定。
            SESSION_HEADER: session_id,
        },
    )
    # 连接池中的 Cookie 不能自动成为另一个调用方的用户身份。
    upstream_req.headers.pop("cookie", None)
    trace = getattr(request.state, "conversation_trace", None)
    if trace is not None:
        trace.record("upstream_request", method="POST", url=UPSTREAM_URL, body=upstream_body,
                     headers=safe_headers(upstream_req.headers), history_mode=history_mode,
                     session_source=session_source, session_hash=hashlib.sha256(session_id.encode()).hexdigest()[:16])
        upstream_req.extensions["conversation_trace"] = trace
    response_format = body.get("response_format")
    modalities = body.get("modalities")
    upstream_req.extensions["card_hint_filter_allowed"] = (
        (response_format is None or isinstance(response_format, dict) and response_format.get("type", "text") == "text")
        and (modalities is None or isinstance(modalities, list) and "text" in modalities))
    if stream:
        response_format = body.get("response_format")
        allow_wait = response_format is None or (
            isinstance(response_format, dict) and response_format.get("type", "text") == "text")
        modalities = body.get("modalities")
        if modalities is not None and (not isinstance(modalities, list) or "text" not in modalities):
            allow_wait = False
        return streaming_response(client, upstream_req, request_id, model, started, include_usage, allow_wait,
                                  voice_errors=VOICE_ERROR_FALLBACK_ENABLED and request.url.path == "/v1/keepalive/chat/completions")

    try:
        operation = nonstream_response(client, upstream_req, request_id, model, started)
        return await asyncio.wait_for(operation, UPSTREAM_TOTAL_TIMEOUT) if UPSTREAM_TOTAL_TIMEOUT > 0 else await operation
    except asyncio.TimeoutError as exc:
        record_upstream_stream_error(upstream_req, exc, "upstream_total_timeout")
        trace = upstream_req.extensions.get("conversation_trace")
        if trace is not None:
            trace.record("upstream_total_timeout", source_complete=False)
        log.warning("request_id=%s stage=upstream_total_timeout stream=False", request_id)
        return openai_error(504, "Upstream total response timeout", "upstream_total_timeout", "server_error")


async def nonstream_response(client, upstream_req, request_id, model, started):
    stream = False
    resp, error = await open_upstream(client, upstream_req, request_id)
    upstream_headers_ms = round((time.monotonic() - started) * 1000)
    if error is not None:
        log.info("request_id=%s stage=upstream_open_failed result_http_status=%d total_ms=%d",
                 request_id, error.status_code, upstream_headers_ms)
        return error
    assert resp is not None

    upstream = UpstreamStream(resp, request_id, model,
                                      allow_hint_filter=upstream_req.extensions.get("card_hint_filter_allowed", True))

    def log_done(status: str, extra: str = ""):
        log.info("request_id=%s model=%s stream=%s status=%s ttft_ms=%s upstream_headers_ms=%d total_ms=%d%s%s",
                 request_id, model, stream, status, upstream.ttft_ms, upstream_headers_ms, (time.monotonic() - started) * 1000,
                 upstream.diagnostic_fields(started), extra)

    # ------------------------------------------------ 非流式：聚合成完整响应
    segments: Dict[int, List[str]] = {}
    reasoning: List[str] = []
    refusal: List[str] = []
    annotations: List[Any] = []
    tool_calls: Dict[int, Dict[str, Any]] = {}
    function_call: Dict[str, Any] = {}
    logprobs: Optional[Dict[str, Any]] = None
    finish_reason: Optional[str] = None

    def merge_fields(target: Dict[str, Any], fragment: Dict[str, Any]):
        for key, value in fragment.items():
            if key == "index":
                continue
            if isinstance(value, dict):
                merge_fields(target.setdefault(key, {}), value)
            elif isinstance(value, list):
                target.setdefault(key, []).extend(value)
            elif key in {"name", "arguments", "input"} and isinstance(value, str):
                target[key] = target.get(key, "") + value
            else:
                target[key] = value
    try:
        async for seg, chunk in upstream.chunks():
            for choice in chunk["choices"]:
                delta = choice["delta"]
                if delta.get("content"):
                    segments.setdefault(seg, []).append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                if delta.get("refusal"):
                    refusal.append(delta["refusal"])
                if delta.get("annotations"):
                    annotations.extend(delta["annotations"])
                for tc in delta.get("tool_calls") or []:
                    merge_fields(tool_calls.setdefault(tc.get("index", 0), {}), tc)
                if delta.get("function_call"):
                    merge_fields(function_call, delta["function_call"])
                if choice.get("logprobs") is not None:
                    if logprobs is None:
                        logprobs = {}
                    merge_fields(logprobs, choice["logprobs"])
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]
    except UpstreamStreamError as e:
        record_upstream_stream_error(upstream_req, e, e.code)
        log_done("upstream_stream_error")
        return openai_error(502, str(e), e.code, "server_error")
    except httpx.TimeoutException as e:
        record_upstream_stream_error(upstream_req, e, "upstream_timeout")
        log_done("upstream_read_timeout")
        return openai_error(504, "Upstream read timeout", "upstream_timeout", "server_error")
    except httpx.HTTPError as e:
        record_upstream_stream_error(upstream_req, e, "upstream_error")
        log.error("request_id=%s read error: %s", request_id, e)
        log_done("upstream_http_error")
        return openai_error(502, "Upstream connection error", "upstream_error", "server_error")
    finally:
        await resp.aclose()

    texts = ["".join(segments[k]) for k in sorted(segments)]
    if NONSTREAM_FINAL_SEGMENT_ONLY:
        non_empty = [t for t in texts if t.strip()]
        content = non_empty[-1] if non_empty else ""
    else:
        content = "".join(texts)

    message: Dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if refusal:
        message["refusal"] = "".join(refusal)
    if annotations:
        message["annotations"] = annotations
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]
    if function_call:
        message["function_call"] = function_call
    if not content and (tool_calls or function_call or refusal):
        message["content"] = None
    log_done("ok", f" segments={len(texts)}")
    return JSONResponse(
        {
            "id": upstream.id,
            "object": "chat.completion",
            "created": upstream.created,
            "model": model,
            "choices": [{"index": 0, "message": message, "logprobs": logprobs,
                         "finish_reason": finish_reason or "stop"}],
            "usage": upstream.usage,
            **upstream.metadata,
        },
        headers={"X-Request-Id": request_id},
    )


@app.post("/v1/keepalive/chat/completions")
async def chat_completions_json_keepalive(request: Request):
    """显式可选的非流式读取超时适配；成功时仍是单份 JSON，绝不返回 SSE。"""
    try:
        body = await request.json()
    except ValueError:
        return await chat_completions(request)
    if not isinstance(body, dict) or body.get("stream", False) is not False:
        # 在读完请求体后再决定模式，不能因上传慢而把流式请求错误包装成 JSON。
        return await chat_completions(request)
    task = asyncio.create_task(chat_completions(request))
    try:
        # 快速结果/校验失败不改变 HTTP 状态、内容和响应方式；stream:true 也直接返回原 SSE。
        return await asyncio.wait_for(asyncio.shield(task), timeout=max(0.001, JSON_KEEPALIVE_AFTER))
    except asyncio.TimeoutError:
        pass
    except BaseException:
        task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
        raise

    async def gen() -> AsyncIterator[bytes]:
        try:
            yield b"\n"
            while not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=max(0.001, JSON_KEEPALIVE_INTERVAL))
                except asyncio.TimeoutError:
                    yield b"\n"
            response = task.result()
            if not isinstance(response, JSONResponse):
                # stream:true 正常会在初始等待内返回 SSE，不可将其混入 JSON 通道。
                raise RuntimeError("Unexpected response type for JSON keepalive")
            log.info("request_id=%s stage=json_keepalive_result result_http_status=%d",
                     request.state.proxy_request_id, response.status_code)
            yield response.body
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("request_id=%s json keepalive error: %s", request.state.proxy_request_id, type(exc).__name__)
            yield openai_error(502, "Upstream unavailable", "upstream_error", "server_error").body
        finally:
            if not task.done():
                log.info("request_id=%s status=client_disconnected json_keepalive=True", request.state.proxy_request_id)
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    # 此后 HTTP 200 已提交，后续错误只能保留 OpenAI error JSON，无法再修改状态码。
    return StreamingResponse(gen(), media_type="application/json", headers={
        "Cache-Control": "no-store", "X-Request-Id": request.state.proxy_request_id,
        "X-Should-Retry": "false",
    })


# 外层纯 ASGI 记录器只旁观数据，不提前消费请求或重组发给平台的响应字节。
app.add_middleware(
    ConversationAuditMiddleware,
    authorized=lambda scope: resolve_upstream_auth(Request(scope)) is not None,
    settings=lambda: {
        "enabled": CONVERSATION_LOG_ENABLED, "directory": CONVERSATION_LOG_DIR,
        "secrets": [UPSTREAM_API_KEY, *PROXY_API_KEYS],
        "max_bytes": CONVERSATION_LOG_MAX_BYTES, "retention_days": CONVERSATION_LOG_RETENTION_DAYS,
        "total_bytes": CONVERSATION_LOG_TOTAL_BYTES,
    },
)
