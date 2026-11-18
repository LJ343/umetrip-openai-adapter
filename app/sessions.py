"""Bounded, expiring internal tool history. Identity = proxy credential + session ID."""
import hashlib
import json
import os
import sqlite3
import time


class SessionConflict(Exception):
    pass


class SessionStore:
    def __init__(self, settings):
        self.s = settings
        self.s.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.s.state_dir / "sessions.sqlite3"
        with self.connect() as db:
            db.execute("PRAGMA max_page_count=32768")  # about 128 MiB, bounded independently of logs
            db.execute("CREATE TABLE IF NOT EXISTS sessions (key TEXT PRIMARY KEY, updated REAL, public TEXT, internal TEXT)")
        os.chmod(self.path, 0o600)
        self.active = set()  # service explicitly uses one worker

    def connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.execute("PRAGMA max_page_count=32768")
        db.execute("PRAGMA secure_delete=ON")
        return db

    @staticmethod
    def identity(credential, session_id):
        return hashlib.sha256((credential + "\0" + session_id).encode()).hexdigest()

    def claim(self, key):
        if key in self.active:
            raise SessionConflict("同一会话已有请求正在处理，请等待完成")
        self.active.add(key)

    def release(self, key):
        self.active.discard(key)

    def history(self, key, incoming):
        with self.connect() as db:
            db.execute("DELETE FROM sessions WHERE updated < ?", (time.time() - self.s.session_ttl,))
            row = db.execute("SELECT public, internal FROM sessions WHERE key=?", (key,)).fetchone()
        if not row:
            return incoming.copy(), incoming.copy()
        public, internal = map(json.loads, row)
        # A single new user message is an explicit session-mode continuation.
        if len(incoming) == 1 and incoming[0]["role"] == "user":
            return public + incoming, internal + incoming
        if incoming[:len(public)] == public and len(incoming) > len(public):
            suffix = incoming[len(public):]
            return incoming.copy(), internal + suffix
        raise SessionConflict("会话历史与已保存记录不一致；请传完整历史或使用新的session_id")

    def save(self, key, public, internal):
        p = json.dumps(public, ensure_ascii=False)
        i = json.dumps(internal, ensure_ascii=False)
        if len((p + i).encode()) > self.s.max_session_bytes:
            raise SessionConflict("会话已达到容量上限，请开启新会话；当前回答未写入历史")
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO sessions VALUES(?,?,?,?)", (key, time.time(), p, i))
            db.execute("DELETE FROM sessions WHERE key IN (SELECT key FROM sessions ORDER BY updated DESC LIMIT -1 OFFSET ?)", (self.s.max_sessions,))
