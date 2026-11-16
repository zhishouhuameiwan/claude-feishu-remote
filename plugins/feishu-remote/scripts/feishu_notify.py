#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared Feishu notification helper for the Claude Code hooks and the listener.
Configuration is loaded from environment variables first, then config.json in
the data folder (paths.py); ready() only looks at config.json.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional


import paths

CONFIG_FILE = paths.CONFIG_FILE
SWITCH_FILE = paths.SWITCH_FILE
READY_FIELDS = ("app_id", "app_secret", "chat_id", "allowed_open_id")

MESSAGES_URL = "https://open.feishu.cn/open-apis/im/v1/messages"

_TOKEN_CACHE: dict[str, Any] = {"token": None, "expire": 0.0}


def _load_config_file() -> dict[str, str]:
    if not CONFIG_FILE.exists():
        return {}

    with CONFIG_FILE.open("r", encoding="utf-8") as f:
        raw = json.load(f)

    if not isinstance(raw, dict):
        raise RuntimeError(f"{CONFIG_FILE} must contain a JSON object")

    return {str(k): str(v) for k, v in raw.items() if v is not None}


def read_config_field(name: str) -> str:
    """One field of config.json, '' when missing."""
    return _load_config_file().get(name, "").strip()


def write_config_fields(values: dict[str, str]) -> None:
    """Set fields of config.json, keeping the others (atomic replace)."""
    raw: dict[str, Any] = {}
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    raw.update(values)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, CONFIG_FILE)


def write_config_field(name: str, value: str) -> None:
    write_config_fields({name: value})


def ready() -> bool:
    """Set up and bound. Hooks do nothing at all before that, so installing the
    plugin without running setup.ps1 leaves Claude Code exactly as it was."""
    try:
        config = _load_config_file()
    except (OSError, ValueError, RuntimeError):
        return False
    return all(config.get(name, "").strip() for name in READY_FIELDS)


def _get_config_value(env_name: str, file_name: str, config: dict[str, str]) -> str:
    value = os.environ.get(env_name) or config.get(file_name) or ""
    return value.strip()


def load_config() -> tuple[str, str, str]:
    config = _load_config_file()
    app_id = _get_config_value("FEISHU_APP_ID", "app_id", config)
    app_secret = _get_config_value("FEISHU_APP_SECRET", "app_secret", config)
    chat_id = _get_config_value("FEISHU_CHAT_ID", "chat_id", config)

    missing = [
        name
        for name, value in (
            ("FEISHU_APP_ID/app_id", app_id),
            ("FEISHU_APP_SECRET/app_secret", app_secret),
            ("FEISHU_CHAT_ID/chat_id", chat_id),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "Missing Feishu configuration: "
            + ", ".join(missing)
            + f". Set environment variables or create {CONFIG_FILE}."
        )

    return app_id, app_secret, chat_id


def load_app_credentials() -> tuple[str, str]:
    """app_id and app_secret only: the listener and the token need no chat yet (binding)."""
    config = _load_config_file()
    app_id = _get_config_value("FEISHU_APP_ID", "app_id", config)
    app_secret = _get_config_value("FEISHU_APP_SECRET", "app_secret", config)
    if not app_id or not app_secret:
        raise RuntimeError(f"Missing Feishu app_id/app_secret in {CONFIG_FILE}; run setup.ps1.")
    return app_id, app_secret


def notifications_enabled() -> bool:
    return not SWITCH_FILE.exists()


def limit_text(value: str, max_chars: int) -> str:
    """Trim a message field so one long field cannot flood the chat."""
    value = value.strip()
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 20].rstrip() + "\n... [truncated]"


def get_access_token() -> str:
    """Fetch tenant_access_token with an in-process cache."""
    now = time.time()
    if _TOKEN_CACHE["token"] and float(_TOKEN_CACHE["expire"]) > now + 300:
        return str(_TOKEN_CACHE["token"])

    app_id, app_secret = load_app_credentials()
    payload = json.dumps(
        {"app_id": app_id, "app_secret": app_secret}, ensure_ascii=False
    ).encode("utf-8")

    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if result.get("code") != 0:
        raise RuntimeError(f"Failed to get Feishu token: {result}")

    token = str(result["tenant_access_token"])
    expire_seconds = int(result.get("expire", 7200))
    _TOKEN_CACHE["token"] = token
    _TOKEN_CACHE["expire"] = now + expire_seconds - 300
    return token


def _im_request(method: str, url: str, body: dict[str, Any]) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        # 'replace': text taken from tool input or transcripts can hold lone
        # surrogates (e.g. "\ud800"), which strict UTF-8 refuses to encode.
        data=json.dumps(body, ensure_ascii=False).encode("utf-8", "replace"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {get_access_token()}",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode("utf-8"))
    if result.get("code") != 0:
        raise RuntimeError(f"Feishu {method} failed: {result}")
    return result


def _send_to_chat(msg_type: str, content: str) -> str:
    _, _, chat_id = load_config()
    result = _im_request(
        "POST",
        f"{MESSAGES_URL}?receive_id_type=chat_id",
        {"receive_id": chat_id, "msg_type": msg_type, "content": content},
    )
    return str(result.get("data", {}).get("message_id") or "")


def send_feishu_message(title: str, body: str) -> bool:
    """Send a text message to the configured Feishu chat."""
    if not notifications_enabled():
        return False
    text = f"{title}\n\n{body}".strip()
    _send_to_chat("text", json.dumps({"text": text}, ensure_ascii=False))
    return True


def send_feishu_card(card: dict[str, Any]) -> Optional[str]:
    """Send an interactive card; returns its message_id, or None when disabled."""
    if not notifications_enabled():
        return None
    return _send_to_chat("interactive", json.dumps(card, ensure_ascii=False))


def send_reply_card(card: dict[str, Any]) -> str:
    """Answer a command the user sent to the bot. Unlike the reminders, this is
    sent even while notifications are switched off: the user just asked for it."""
    return _send_to_chat("interactive", json.dumps(card, ensure_ascii=False))


def send_reply_text(title: str, body: str) -> None:
    """Plain-text answer to a command (sent even while notifications are off)."""
    _send_to_chat("text", json.dumps({"text": f"{title}\n\n{body}".strip()}, ensure_ascii=False))


def update_feishu_card(message_id: str, card: dict[str, Any]) -> None:
    """Replace a card this app sent earlier (JSON 2.0 cards are shared)."""
    _im_request(
        "PATCH",
        f"{MESSAGES_URL}/{message_id}",
        {"content": json.dumps(card, ensure_ascii=False)},
    )
