"""Private, per-request conversation traces. No changes to forwarded ASGI bytes."""
import codecs
from contextlib import suppress
import datetime
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import stat
import time
import uuid

log = logging.getLogger("openai-proxy")
PATHS = {"/v1/chat/completions", "/v1/keepalive/chat/completions"}
SAFE_HEADERS = {"content-type", "content-encoding", "accept", "x-request-id", "x-should-retry"}


def safe_headers(headers):
    return {key: value for key, value in headers.items() if key.lower() in SAFE_HEADERS}


class ConversationTrace:
    def __init__(self, directory, request_id, secrets=(), max_bytes=16 * 1024 * 1024,
                 retention_days=3, total_bytes=256 * 1024 * 1024):
        self.request_id = request_id
        self.started = time.monotonic()
        self.secrets = sorted({value for value in secrets if value}, key=len, reverse=True)
        self.max_bytes = max(2048, max_bytes)
        self.bytes_written = self.seq = 0
        self.truncated = False
        self.file = None
        self.path = None
        try:
            directory = Path(directory)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise OSError("Invalid conversation log directory")
            os.chmod(directory, 0o700)
            self.cleanup(directory, retention_days, total_bytes)
            stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            self.path = directory / (stamp + "_" + request_id + ".jsonl")
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.file = os.fdopen(fd, "w", encoding="utf-8", buffering=65536)
            self.flushed = self.started
        except OSError as exc:
            log.error("request_id=%s stage=conversation_log_unavailable error_type=%s", request_id, type(exc).__name__)

    @staticmethod
    def cleanup(directory, retention_days, total_bytes):
        # Cross-worker lock; once per minute. Active files hold their own lock.
        fd = os.open(directory / ".cleanup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return
            if time.time() - os.fstat(fd).st_mtime < 60 and os.fstat(fd).st_size:
                return
            os.write(fd, b"1")
            os.utime(fd, None)
            files = []
            for path in directory.glob("*.jsonl"):
                st = path.lstat()
                if stat.S_ISREG(st.st_mode):
                    files.append((st.st_mtime, st.st_size, path))
            size = sum(item[1] for item in files)
            cutoff = time.time() - max(1, retention_days) * 86400
            for modified, length, path in sorted(files):
                if modified >= cutoff and size <= total_bytes:
                    continue
                try:
                    with path.open("rb") as stream:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        path.unlink()
                    size -= length
                except (FileNotFoundError, BlockingIOError):
                    pass
        finally:
            os.close(fd)

    def redact(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)Bearer\s+[^\s\"\\<>]+", "Bearer [REDACTED]", value)
            return re.sub(r"sk-[A-Za-z0-9_-]{16,}", "[REDACTED]", value)
        if isinstance(value, dict):
            return {key: "[REDACTED]" if key.lower() in {
                "authorization", "api_key", "apikey", "access_token", "refresh_token", "password", "cookie", "set-cookie"
            } else self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        return value

    def record(self, stage, **fields):
        if self.file is None or self.truncated:
            return
        self.seq += 1
        item = {"seq": self.seq, "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "elapsed_ms": round((time.monotonic() - self.started) * 1000),
                "request_id": self.request_id, "stage": stage, **self.redact(fields)}
        line = json.dumps(item, ensure_ascii=False) + "\n"
        length = len(line.encode())
        if self.bytes_written + length > self.max_bytes:
            self.truncated = True
            line = json.dumps({key: item[key] for key in ("seq", "time", "elapsed_ms", "request_id")}
                              | {"stage": "capture_truncated", "limit_bytes": self.max_bytes}) + "\n"
        try:
            self.file.write(line)
            self.bytes_written += len(line.encode())
            if self.truncated or time.monotonic() - self.flushed >= 1 or stage in {
                    "platform_request", "upstream_request", "upstream_response", "platform_response_start"}:
                self.file.flush()
                self.flushed = time.monotonic()
        except OSError as exc:
            log.error("request_id=%s stage=conversation_log_write_failed error_type=%s", self.request_id, type(exc).__name__)
            with suppress(OSError):
                self.file.close()
            self.file = None

    def close(self):
        if self.file is not None:
            try:
                self.file.close()
            except OSError:
                log.error("request_id=%s stage=conversation_log_close_failed", self.request_id)
            self.file = None


class ResponseBodyCapture:
    """Reassemble UTF-8 lines so split multibyte characters/credentials stay intact."""
    def __init__(self, trace):
        self.trace = trace
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending = ""

    def feed(self, data, final=False):
        if self.trace.truncated:
            self.pending = ""
            return
        self.pending += self.decoder.decode(data, final=final)
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.trace.record("platform_response_body", text=line + "\n")
        if len(self.pending.encode()) > self.trace.max_bytes:
            # Record an explicit truncation rather than buffer an unbounded line.
            self.trace.record("platform_response_body", text=self.pending)
            self.pending = ""
        if final and self.pending:
            self.trace.record("platform_response_body", text=self.pending)
            self.pending = ""


class ConversationAuditMiddleware:
    def __init__(self, app, settings, authorized):
        self.app, self.settings, self.authorized = app, settings, authorized

    async def __call__(self, scope, receive, send):
        config = self.settings()
        if (scope["type"] != "http" or scope.get("path") not in PATHS or scope.get("method") != "POST"
                or not config.pop("enabled") or not self.authorized(scope)):
            return await self.app(scope, receive, send)
        state = scope.setdefault("state", {})
        request_id = state.setdefault("proxy_request_id", uuid.uuid4().hex)
        authorization = dict(scope.get("headers", [])).get(b"authorization", b"").decode("latin-1")
        if authorization.lower().startswith("bearer "):
            config["secrets"] = [*config.get("secrets", ()), authorization[7:].strip()]
        trace = state["conversation_trace"] = ConversationTrace(request_id=request_id, **config)
        trace.record("request_start", path=scope["path"], method=scope["method"])
        body, body_size = [], 0
        outgoing = ResponseBodyCapture(trace)
        complete = False

        async def observed_receive():
            nonlocal body_size
            message = await receive()
            if message["type"] == "http.request":
                data = message.get("body", b"")
                body_size += len(data)
                if body_size <= trace.max_bytes:
                    body.append(data)
                if not message.get("more_body", False):
                    if body_size <= trace.max_bytes:
                        raw = b"".join(body).decode("utf-8", errors="replace")
                        try:
                            value = json.loads(raw)
                        except ValueError:
                            value = raw
                        trace.record("platform_request", body=value)
                    else:
                        trace.record("platform_request_omitted", reason="request_exceeds_capture_limit", bytes=body_size)
                    body.clear()
            elif message["type"] == "http.disconnect":
                trace.record("client_disconnect")
            return message

        async def observed_send(message):
            nonlocal complete
            if message["type"] == "http.response.start":
                headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in message.get("headers", [])}
                trace.record("platform_response_start", status=message["status"], headers=safe_headers(headers))
            elif message["type"] == "http.response.body":
                final = not message.get("more_body", False)
                outgoing.feed(message.get("body", b""), final=final)
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                complete = True

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            trace.record("request_exception", error_type=type(exc).__name__)
            raise
        finally:
            outgoing.feed(b"", final=True)
            trace.record("request_end", response_sent_complete=complete)
            trace.close()
