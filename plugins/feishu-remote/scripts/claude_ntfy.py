#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code -> Feishu notification entrypoint.
Usage: python claude_ntfy.py <event_type> <fallback message>

The hook JSON payload on stdin adds the session title and what the user has
to decide (question and options, plan, pending tool call, ...).

Stdout stays empty because some hook events read stdout as a decision.
Diagnostics go to stderr and hook runs always exit 0, so a failed reminder
never blocks or answers anything in Claude Code.
"""

from __future__ import annotations

import functools
import json
import os
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu_notify
import paths
import reply_bridge
from feishu_notify import limit_text


EMOJI_MAP = {
    "approval": "[!]",
    "error": "[x]",
    "complete": "[ok]",
    "input": "[?]",
    "decision": "[?]",
}

EVENT_NAME_MAP = {
    "approval": "审批请求",
    "error": "发生错误",
    "complete": "任务完成",
    "input": "等你回复",
    "decision": "等你决策",
}

# Used when hooks.json passes only the event type (keeps non-ASCII out of hook commands).
DEFAULT_FALLBACK = {
    "approval": "Claude Code 等你审批",
    "input": "MCP 弹窗需要你填写信息",
    "error": "Claude Code 出错了",
}

WRAP_UP_TOOL = "mcp__ccd_turn__wrap_up"
# These tools always send their own reminder from PreToolUse, so a permission
# prompt raised for them would be a duplicate. wrap_up is not listed: it only
# reminds when blocked, so its permission prompt must not be swallowed.
# AskUserQuestion is not listed either: claude_permission.py covers it with a card.
SELF_NOTIFYING_TOOLS = {"ExitPlanMode"}
# Transcripts reach hundreds of MB; the pending call and title are at the end.
TAIL_BYTES = 2 * 1024 * 1024


# FEISHU_HOOK_LOG: offline tests point this elsewhere so the real log shows only real hooks.
HOOK_LOG = str(paths.HOOK_LOG)
HOOK_LOG_MAX_BYTES = 1_000_000


def log(text: str) -> None:
    """stderr for the hook runner, plus hooks.log because background hooks' stderr is not shown."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {os.getpid()} {text}\n"
    sys.stderr.buffer.write((text + "\n").encode("utf-8", errors="replace"))
    sys.stderr.flush()
    try:
        mode = "w" if os.path.getsize(HOOK_LOG) > HOOK_LOG_MAX_BYTES else "a"
    except OSError:
        mode = "a"
    try:
        with open(HOOK_LOG, mode, encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def read_payload() -> dict[str, Any]:
    if sys.stdin is None or sys.stdin.isatty():
        return {}
    raw = sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log(f"[Feishu] bad hook payload: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@functools.lru_cache(maxsize=2)
def read_transcript_tail(path: str) -> tuple[dict[str, Any], ...]:
    if not path or not os.path.isfile(path):
        return ()
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - TAIL_BYTES))
        lines = f.read().split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]  # the first line is cut in the middle
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return tuple(entries)


def session_title(transcript_path: str) -> str:
    if not transcript_path:
        return ""
    side_file = os.path.join(os.path.splitext(transcript_path)[0], "custom-title.json")
    try:
        with open(side_file, encoding="utf-8") as f:
            title = _as_dict(json.load(f)).get("customTitle")
        if title:
            return str(title)
    except (OSError, ValueError):
        pass
    entries = read_transcript_tail(transcript_path)
    for entry_type, field in (
        ("custom-title", "customTitle"),
        ("agent-name", "agentName"),
        ("last-prompt", "lastPrompt"),
    ):
        for entry in reversed(entries):
            if entry.get("type") == entry_type and entry.get(field):
                return str(entry[field])
    return ""


def _content_blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    content = _as_dict(entry.get("message")).get("content")
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def pending_tool_uses(entries: tuple[dict[str, Any], ...]) -> list[dict[str, Any]]:
    """Tool calls without a result, limited to the newest assistant message."""
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for entry in entries:
        message_id = str(_as_dict(entry.get("message")).get("id") or "")
        for block in _content_blocks(entry):
            if block.get("type") == "tool_use" and block.get("id"):
                pending[str(block["id"])] = (message_id, block)
            elif block.get("type") == "tool_result":
                pending.pop(str(block.get("tool_use_id")), None)
    if not pending:
        return []
    newest_message_id = list(pending.values())[-1][0]
    return [block for mid, block in pending.values() if mid == newest_message_id]


def describe_tool_use(name: str, tool_input: dict[str, Any]) -> list[str]:
    lines = [f"工具: {name}"]
    if tool_input.get("description"):
        lines.append(f"说明: {limit_text(str(tool_input['description']), 300)}")
    for key in ("command", "file_path", "url", "prompt"):
        if tool_input.get(key):
            lines.append(f"内容: {limit_text(str(tool_input[key]), 800)}")
            return lines
    extra = {k: v for k, v in tool_input.items() if k != "description"}
    if extra:
        lines.append(f"参数: {limit_text(json.dumps(extra, ensure_ascii=False), 400)}")
    return lines


def format_questions(tool_input: dict[str, Any]) -> list[str]:
    lines = []
    for number, question in enumerate(tool_input.get("questions") or [], 1):
        question = _as_dict(question)
        lines.append(f"问题{number}: {limit_text(str(question.get('question', '')), 600)}")
        for index, option in enumerate(question.get("options") or [], 1):
            option = _as_dict(option)
            description = option.get("description")
            suffix = f" - {limit_text(str(description), 200)}" if description else ""
            label = limit_text(str(option.get("label", "")), 100)
            lines.append(f"  {index}. {label}{suffix}")
    return lines


def format_plan(tool_input: dict[str, Any]) -> list[str]:
    plan = tool_input.get("plan")
    return [f"计划:\n{limit_text(str(plan), 1500)}"] if plan else []


def format_wrap_up(tool_input: dict[str, Any]) -> list[str]:
    lines = []
    if tool_input.get("recap"):
        lines.append(f"进展: {limit_text(str(tool_input['recap']), 400)}")
    if tool_input.get("ask"):
        lines.append(f"问题: {limit_text(str(tool_input['ask']), 300)}")
    labels = [
        str(_as_dict(action).get("label") or _as_dict(action).get("action") or "")
        for action in tool_input.get("actions") or []
    ]
    labels = [limit_text(label, 100) for label in labels if label]
    if labels:
        lines.append("选项: " + "  ".join(f"{i}. {label}" for i, label in enumerate(labels, 1)))
    return lines


def format_permission_prompt(payload: dict[str, Any]) -> Optional[list[str]]:
    pending = pending_tool_uses(read_transcript_tail(str(payload.get("transcript_path") or "")))
    if pending and all(block.get("name") in SELF_NOTIFYING_TOOLS for block in pending):
        return None
    card_covers = not any(block.get("name") in reply_bridge.CARD_SKIPPED_TOOLS for block in pending)
    if card_covers and reply_bridge.permission_marked_recently(str(payload.get("session_id") or "")):
        return None  # claude_permission.py handles this prompt with a Feishu card
    lines = []
    if payload.get("message"):
        lines.append(f"提示: {limit_text(str(payload['message']), 300)}")
    for block in pending[:3]:
        name, tool_input = str(block.get("name") or "未知工具"), _as_dict(block.get("input"))
        lines.extend(format_questions(tool_input) if name == "AskUserQuestion"
                     else describe_tool_use(name, tool_input))
    return lines


def build_detail(event_type: str, payload: dict[str, Any]) -> Optional[list[str]]:
    """Lines describing what needs the user. None means: do not notify."""
    if payload.get("hook_event_name") == "PreToolUse":
        tool_name = payload.get("tool_name")
        tool_input = _as_dict(payload.get("tool_input"))
        if tool_name == WRAP_UP_TOOL:
            return format_wrap_up(tool_input) if tool_input.get("blocked") is True else None
        if tool_name == "AskUserQuestion":
            return format_questions(tool_input)
        if tool_name == "ExitPlanMode":
            return format_plan(tool_input)
        return []
    if event_type == "approval":
        return format_permission_prompt(payload)
    return [
        f"提示: {limit_text(str(payload[key]), 600)}"
        for key in ("message", "error", "reason")
        if payload.get(key)
    ]


def main() -> int:
    if len(sys.argv) < 2:
        log("Usage: python claude_ntfy.py <event_type> [fallback message]")
        return 1
    if not feishu_notify.ready():
        return 0  # setup.ps1 not run yet: stay silent

    event_type = sys.argv[1]
    fallback = " ".join(sys.argv[2:]) or DEFAULT_FALLBACK.get(event_type, event_type)
    # A broken payload or transcript must not kill the reminder: each step
    # that fails only drops its own part of the message.
    payload = read_payload()
    try:
        detail = build_detail(event_type, payload)
    except Exception as exc:
        log(f"[Feishu] could not build detail: {exc}")
        detail = []
    if detail is None:
        log(f"[Feishu] not needed: {event_type}")
        return 0
    try:
        title = session_title(str(payload.get("transcript_path") or ""))
    except Exception as exc:
        log(f"[Feishu] could not read session title: {exc}")
        title = ""

    lines = []
    if title:
        lines.append(f"会话: {limit_text(title, 100)}")
    if payload.get("cwd"):
        lines.append(f"项目: {limit_text(str(payload['cwd']), 300)}")
    lines.extend(detail or [fallback])

    header = f"{EMOJI_MAP.get(event_type, '[i]')} Claude Code · {EVENT_NAME_MAP.get(event_type, event_type)}"
    try:
        sent = feishu_notify.send_feishu_message(header, "\n".join(lines))
    except Exception as exc:
        log(f"[Feishu] send failed: {exc}")
        return 0

    log(f"[Feishu] {'sent' if sent else 'skipped'}: {event_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
