#!/usr/bin/env python3
"""Interactive secret entry; never put API keys in CLI arguments or output."""
import getpass
import json
import os
import secrets
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    from dotenv import dotenv_values
    path = ROOT / ".env"
    values = dict(dotenv_values(path if path.exists() else ROOT / ".env.example"))
    base = input("大模型Base URL（回车保留当前地址）：").strip()
    if base:
        values["LLM_BASE_URL"] = base
    model = input("大模型具体模型名（必填；已有可回车保留）：").strip()
    if model:
        values["LLM_MODEL"] = model
    for key, label in [("LLM_API_KEY", "大模型API Key"), ("FLYAI_API_KEY", "FlyAI正式API Key")]:
        value = getpass.getpass(label + "（输入隐藏；已有可回车保留）：").strip()
        if value:
            values[key] = value
    values["PROXY_API_KEYS"] = values.get("PROXY_API_KEYS") or secrets.token_urlsafe(32)
    if not values.get("LLM_MODEL") or not values.get("LLM_API_KEY") or not values.get("FLYAI_API_KEY"):
        raise SystemExit("必要配置未填全，没有保存。")
    fd, name = tempfile.mkstemp(dir=ROOT, prefix=".config-", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as out:
            for key, value in values.items():
                out.write(key + "=" + json.dumps(value or "", ensure_ascii=False) + "\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)
    print("独立配置已保存，权限0600。密钥未显示；尚未调用上游。")


if __name__ == "__main__":
    main()
