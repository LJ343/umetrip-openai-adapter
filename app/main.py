import asyncio
import contextlib
import hmac
import json
import logging
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from .agent import Agent, AgentFailure, Model
from .config import Settings
from .sessions import SessionConflict, SessionStore
from .tools import FlyAI
from .tracing import Trace, current_trace

log = logging.getLogger("flyai-agent")
log.setLevel(logging.INFO)
if not log.handlers:
    log.addHandler(logging.StreamHandler())
log.propagate = False


def failure(status, message, code="invalid_request", request_id=None):
    return JSONResponse({"error": {"message": message, "type": "invalid_request_error" if status < 500 else "server_error", "param": None, "code": code}}, status_code=status, headers={"X-Request-Id": request_id} if request_id else None)


def encode_event(value):
    return "data: " + json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n\n"


def validate_request(body, settings):
    if not isinstance(body, dict):
        raise ValueError("请求必须为JSON对象")
    supported = {"model", "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens", "n", "user", "temperature", "top_p", "presence_penalty", "frequency_penalty", "seed"}
    if set(body) - supported:
        raise ValueError("包含暂不支持的参数；此入口支持文本对话，工具由服务内部调度")
    if body.get("model") != settings.public_model:
        raise ValueError("未知模型，请使用 " + settings.public_model)
    if type(body.get("stream", False)) is not bool or type(body.get("n", 1)) is not int or body.get("n", 1) != 1:
        raise ValueError("stream必须为布尔值，仅支持n=1")
    options = body.get("stream_options", {})
    if not isinstance(options, dict) or set(options) - {"include_usage"} or type(options.get("include_usage", False)) is not bool:
        raise ValueError("stream_options只支持include_usage布尔值")
    if "max_tokens" in body and "max_completion_tokens" in body:
        raise ValueError("max_tokens与max_completion_tokens只能指定一个")
    tokens = body.get("max_tokens", body.get("max_completion_tokens", settings.max_tokens))
    if type(tokens) is not int or not 1 <= tokens <= settings.max_tokens:
        raise ValueError("max_tokens必须为配置上限内的正整数")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 200:
        raise ValueError("messages须包含1至200条消息")
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or set(message) - {"role", "content"} or message.get("role") not in {"system", "developer", "user", "assistant"} or not isinstance(message.get("content"), str) or not message["content"].strip():
            raise ValueError("当前仅支持非空的纯文本system/developer/user/assistant消息")
        if message["role"] in {"system", "developer"} and any(m["role"] in {"user", "assistant"} for m in messages[:index]):
            raise ValueError("system/developer消息必须位于对话开头")
    if messages[-1]["role"] != "user":
        raise ValueError("最后一条消息必须是用户问题")
    # Provider compatibility: developer guidance is merged as a system message.
    messages = [{"role": "system" if m["role"] == "developer" else m["role"], "content": m["content"]} for m in messages]
    generation = {}
    for k, low, high in [("temperature", 0, 2), ("top_p", 0, 1), ("presence_penalty", -2, 2), ("frequency_penalty", -2, 2)]:
        if k in body:
            if type(body[k]) not in (int, float) or not low <= body[k] <= high:
                raise ValueError(k + "超出支持范围")
            generation[k] = body[k]
    if "seed" in body:
        if type(body["seed"]) is not int or not -(2**63) <= body["seed"] < 2**63:
            raise ValueError("seed必须是64位整数")
        generation["seed"] = body["seed"]
    return messages, tokens, generation


def create_app(settings=None, model_factory=None, tool_factory=None):
    s = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        app.state.client = httpx.AsyncClient(follow_redirects=False, trust_env=False, limits=httpx.Limits(max_connections=s.concurrency * 2, max_keepalive_connections=s.concurrency))
        app.state.sessions = SessionStore(s)
        app.state.slots = asyncio.Queue(maxsize=s.concurrency)
        for _ in range(s.concurrency):
            app.state.slots.put_nowait(True)
        yield
        await app.state.client.aclose()

    app = FastAPI(title="FlyAI OpenAI-compatible Travel Agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = s

    def authorize(request):
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer "):
            return None
        candidate = auth[7:]
        for key in s.proxy_keys:
            if hmac.compare_digest(candidate.encode(), key.encode()):
                return key
        return None

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "service": "flyai-openai", "business_verified": False}

    @app.get("/readyz")
    async def ready(request: Request):
        if not authorize(request):
            return failure(401, "Invalid or missing API key", "invalid_api_key")
        missing = s.missing()
        return JSONResponse({"ready": not missing, "missing_configuration": missing, "upstream_verified": False}, status_code=503 if missing else 200)

    @app.get("/v1/models")
    @app.get("/v1/keepalive/models")
    async def models(request: Request):
        if not authorize(request):
            return failure(401, "Invalid or missing API key", "invalid_api_key")
        return {"object": "list", "data": [{"id": s.public_model, "object": "model", "created": 0, "owned_by": "flyai-adapter"}]}

    @app.post("/v1/chat/completions")
    @app.post("/v1/keepalive/chat/completions")
    async def completions(request: Request):
        rid, started = uuid.uuid4().hex, time.monotonic()
        credential = authorize(request)
        if not credential:
            return failure(401, "Invalid or missing API key", "invalid_api_key", rid)
        if s.missing():
            return failure(503, "新服务尚未完成独立配置", "not_configured", rid)
        raw = bytearray()
        try:
            async for chunk in request.stream():
                raw.extend(chunk)
                if len(raw) > s.max_request:
                    return failure(413, "请求超过容量限制", "request_too_large", rid)
            body = json.loads(raw)
            incoming, max_tokens, generation = validate_request(body, s)
        except (ValueError, UnicodeDecodeError) as exc:
            # JSON parser errors may include user text; don't echo parser output.
            message = "请求不是有效JSON" if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)) else str(exc)
            return failure(400, message, "invalid_request", rid)
        session_id = request.headers.get("session_id") or request.headers.get("x-session-id")
        if session_id and (len(session_id) > 256 or not session_id.strip()):
            return failure(400, "session_id无效", "invalid_session", rid)
        store, key = app.state.sessions, None
        try:
            app.state.slots.get_nowait()
        except asyncio.QueueEmpty:
            return failure(429, "新服务并发已满，请稍后重试", "busy", rid)
        try:
            if session_id:
                key = store.identity(credential, session_id)
                store.claim(key)
                try:
                    public, history = store.history(key, incoming)
                except Exception:
                    store.release(key)
                    raise
            else:
                public, history = incoming, incoming
        except (SessionConflict, OSError, sqlite3.Error) as exc:
            app.state.slots.put_nowait(True)
            # Never release an already-active claim belonging to another request.
            return failure(409 if isinstance(exc, SessionConflict) else 503, str(exc) if isinstance(exc, SessionConflict) else "会话存储暂不可用", "session_conflict", rid)

        queue = asyncio.Queue(maxsize=16)
        model = model_factory(s, app.state.client) if model_factory else Model(s, app.state.client)
        tools = tool_factory(s) if tool_factory else FlyAI(s)
        agent = Agent(s, model, tools)
        released = False

        def release_resources():
            nonlocal released
            if not released:
                released = True
                if key:
                    store.release(key)
                app.state.slots.put_nowait(True)

        async def produce():
            status, result = "error", None
            trace, token = None, None
            try:
                if s.record_conversations:
                    trace = Trace(s, rid)
                    token = current_trace.set(trace)
                    trace.write("platform_request", body=body, session_hash=key)
                    trace.write("agent_history", messages=history)
                async def run():
                    nonlocal result
                    async for kind, value in agent.run(history, body.get("stream", False), max_tokens, generation):
                        if kind == "result":
                            result = value
                        else:
                            await queue.put((kind, value))
                await asyncio.wait_for(run(), s.request_timeout)
                if key:
                    store.save(key, public + [{"role": "assistant", "content": result["text"]}], result["history"])
                if trace:
                    trace.write("platform_response", content=result["text"], usage=result["usage"], tool_calls=result["tool_calls"], source_complete=True,
                                finish_reason=result["finish_reason"], answer_complete=result["finish_reason"] == "stop")
                await queue.put(("result", result))
                status = "complete" if result["finish_reason"] == "stop" else result["finish_reason"]
            except AgentFailure as exc:
                if trace:
                    trace.write("request_error", code=exc.code, source_complete=False)
                await queue.put(("error", exc))
            except asyncio.TimeoutError:
                await queue.put(("error", AgentFailure("本轮处理超时，未取得完整回答", "request_timeout", 504)))
            except SessionConflict as exc:
                await queue.put(("error", AgentFailure(str(exc), "session_limit", 409)))
            except asyncio.CancelledError:
                status = "cancelled"
                raise
            except Exception as exc:
                # No exception text, credentials, user text, subprocess stderr or remote body in journal.
                log.error("request_id=%s exception_type=%s", rid, type(exc).__name__)
                await queue.put(("error", AgentFailure("新服务暂时异常，本次未取得完整结果", "internal_error", 500)))
            finally:
                try:
                    if trace:
                        trace.write("request_end", status=status, elapsed_ms=int((time.monotonic() - started)*1000))
                except OSError:
                    log.error("request_id=%s trace_write_failed", rid)
                finally:
                    if trace:
                        with contextlib.suppress(OSError):
                            trace.close()
                    if token is not None:
                        current_trace.reset(token)
                    release_resources()
                log.info("request_id=%s result=%s elapsed_ms=%d", rid, status, (time.monotonic() - started) * 1000)

        task = asyncio.create_task(produce())
        cid, created = "chatcmpl-" + rid, int(time.time())

        def chunk(delta, finish=None):
            return {"id": cid, "object": "chat.completion.chunk", "created": created, "model": s.public_model,
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

        async def stop_task():
            try:
                if not task.done():
                    task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            finally:
                # Cancellation can occur before the producer coroutine even starts.
                release_resources()

        async def consume():
            try:
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        item = await asyncio.wait_for(queue.get(), 3)
                    except asyncio.TimeoutError:
                        yield "heartbeat", None
                        continue
                    yield item
                    if item[0] in ("result", "error"):
                        return
            finally:
                await stop_task()

        if body.get("stream", False):
            async def events():
                try:
                    yield encode_event(chunk({"role": "assistant", "content": ""}))
                    async for kind, value in consume():
                        if kind == "heartbeat":
                            yield ": keepalive\n\n"
                        elif kind == "text":
                            yield encode_event(chunk({"content": value}))
                        elif kind == "error":
                            yield encode_event({"error": {"message": value.message, "type": "server_error", "code": value.code}})
                            # No success finish_reason or DONE after failure.
                        elif kind == "result":
                            yield encode_event(chunk({}, value["finish_reason"]))
                            if body.get("stream_options", {}).get("include_usage") and value["usage"]:
                                yield encode_event({"id": cid, "object": "chat.completion.chunk", "created": created, "model": s.public_model, "choices": [], "usage": value["usage"]})
                            yield "data: [DONE]\n\n"
                finally:
                    await stop_task()
            return StreamingResponse(events(), media_type="text/event-stream", headers={"X-Request-Id": rid, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        async def aggregate():
            async for kind, value in consume():
                if kind == "error":
                    return failure(value.status, value.message, value.code, rid)
                if kind == "result":
                    response = {"id": cid, "object": "chat.completion", "created": created, "model": s.public_model,
                                "choices": [{"index": 0, "message": {"role": "assistant", "content": value["text"]}, "finish_reason": value["finish_reason"]}]}
                    if value["usage"]:
                        response["usage"] = value["usage"]
                    return JSONResponse(response, headers={"X-Request-Id": rid})
            return failure(499, "客户端连接已断开", "client_disconnected", rid)

        if request.url.path == "/v1/keepalive/chat/completions":
            pending = asyncio.create_task(aggregate())
            try:
                return await asyncio.wait_for(asyncio.shield(pending), 3)
            except asyncio.CancelledError:
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await pending
                await stop_task()
                raise
            except asyncio.TimeoutError:
                async def whitespace():
                    try:
                        while not pending.done():
                            yield b"\n"
                            try:
                                await asyncio.wait_for(asyncio.shield(pending), 3)
                            except asyncio.TimeoutError:
                                pass
                        response = await pending
                        yield response.body
                    finally:
                        if not pending.done():
                            pending.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await pending
                # After headers commit, a later error is represented by an error object in the JSON body.
                return StreamingResponse(whitespace(), media_type="application/json", headers={"X-Request-Id": rid, "X-Accel-Buffering": "no", "Cache-Control": "no-cache"})
        return await aggregate()

    return app


app = create_app()
