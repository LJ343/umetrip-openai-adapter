"""Private bounded per-request evidence; never log authorization headers or API keys."""
import contextvars
import json
import os
import time

current_trace = contextvars.ContextVar("flyai_trace", default=None)
active_files = set()


def record(stage, **data):
    trace = current_trace.get()
    if trace:
        trace.write(stage, **data)


class Trace:
    def __init__(self, settings, request_id):
        self.secrets = (settings.llm_key, settings.flyai_key, *settings.proxy_keys)
        directory = settings.state_dir / "conversations"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        files = sorted(directory.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for path in files:
            if path in active_files:
                continue
            if total <= 64 * 1024 * 1024 and path.stat().st_mtime > time.time() - 86400:
                continue
            size = path.stat().st_size
            path.unlink()
            total -= size
        self.path = directory / (request_id + ".jsonl")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self.file = os.fdopen(fd, "w")
        active_files.add(self.path)
        self.size, self.limited = 0, False

    def write(self, stage, **data):
        if self.limited or self.file.closed:
            return
        line = json.dumps({"time_utc": time.time(), "stage": stage, **data}, ensure_ascii=False) + "\n"
        for secret in self.secrets:
            if secret:
                line = line.replace(secret, "<redacted>")
        size = len(line.encode())
        if self.size + size > 4 * 1024 * 1024:
            line = '{"stage":"trace_capacity_reached","capture_complete":false}\n'
            self.limited = True
        self.file.write(line)
        self.file.flush()
        self.size += size

    def close(self):
        self.file.close()
        active_files.discard(self.path)
