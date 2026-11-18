import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent.parent


def bounded(name, default, minimum, maximum):
    value = int(os.getenv(name, str(default)))
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} outside supported range")
    return value


@dataclass
class Settings:
    llm_base: str
    llm_model: str
    llm_key: str
    flyai_key: str
    proxy_keys: tuple
    public_model: str
    node_bin: str
    cli_path: Path
    state_dir: Path
    concurrency: int = 2
    request_timeout: int = 240
    llm_timeout: int = 90
    tool_timeout: int = 100
    max_rounds: int = 8
    max_calls: int = 16
    session_ttl: int = 86400
    max_sessions: int = 500
    max_session_bytes: int = 1048576
    max_tool_output: int = 524288
    max_request: int = 524288
    max_tokens: int = 4096
    extra_body: dict = None
    record_conversations: bool = False

    @classmethod
    def load(cls):
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
        extra = json.loads(os.getenv("LLM_EXTRA_BODY_JSON", "{}"))
        forbidden = {"model", "messages", "tools", "tool_choice", "stream", "stream_options", "n"}
        if not isinstance(extra, dict) or forbidden.intersection(extra):
            raise ValueError("LLM_EXTRA_BODY_JSON contains reserved fields")
        return cls(
            llm_base=os.getenv("LLM_BASE_URL", "").rstrip("/"),
            llm_model=os.getenv("LLM_MODEL", ""), llm_key=os.getenv("LLM_API_KEY", ""),
            flyai_key=os.getenv("FLYAI_API_KEY", ""),
            proxy_keys=tuple(k.strip() for k in os.getenv("PROXY_API_KEYS", "").split(",") if k.strip()),
            public_model=os.getenv("PUBLIC_MODEL", "flyai-travel"),
            node_bin=os.getenv("NODE_BIN", "/usr/bin/node"),
            cli_path=(ROOT / os.getenv("FLYAI_CLI_PATH", "runtime/flyai-bundle.cjs")).resolve(),
            state_dir=(ROOT / os.getenv("STATE_DIR", "state")).resolve(),
            concurrency=bounded("MAX_CONCURRENT", 2, 1, 8),
            request_timeout=bounded("REQUEST_TIMEOUT_SECONDS", 240, 10, 600),
            llm_timeout=bounded("LLM_TIMEOUT_SECONDS", 90, 5, 180),
            tool_timeout=bounded("TOOL_TIMEOUT_SECONDS", 100, 5, 180),
            max_rounds=bounded("MAX_TOOL_ROUNDS", 8, 1, 12),
            max_calls=bounded("MAX_TOOL_CALLS", 16, 1, 24),
            session_ttl=bounded("SESSION_TTL_SECONDS", 86400, 60, 604800),
            max_sessions=bounded("MAX_SESSIONS", 500, 1, 1000),
            max_session_bytes=bounded("MAX_SESSION_BYTES", 1048576, 4096, 2097152),
            max_tool_output=bounded("MAX_TOOL_OUTPUT_BYTES", 524288, 1024, 1048576),
            max_request=bounded("MAX_REQUEST_BYTES", 524288, 1024, 1048576),
            max_tokens=bounded("MAX_OUTPUT_TOKENS", 4096, 128, 16384), extra_body=extra,
            record_conversations=os.getenv("RECORD_CONVERSATIONS", "false").lower() == "true",
        )

    def missing(self):
        out = [name for name, value in [("LLM_BASE_URL", self.llm_base), ("LLM_MODEL", self.llm_model), ("LLM_API_KEY", self.llm_key), ("FLYAI_API_KEY", self.flyai_key), ("PROXY_API_KEYS", self.proxy_keys)] if not value]
        parsed = urlparse(self.llm_base)
        if self.llm_base and (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment):
            out.append("LLM_BASE_URL must be a credential-free HTTPS base URL")
        if not Path(self.node_bin).is_file():
            out.append("NODE_BIN")
        if not self.cli_path.is_file():
            out.append("FLYAI_CLI_PATH")
        return out
