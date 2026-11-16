#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
What one Claude Code session is doing, read from its transcript only
(<claude home>/projects/<project>/<sessionId>.jsonl), for the session detail card
the listener shows on "查看" and for the completion notice of the Stop hook.
Standard library only; nothing here writes to the transcript or the session.

What the transcript holds (Claude Code 2.1.325, measured 2026-11-15):
  - every entry of one turn carries the same promptId; the turn's first entry is
    its start, whether a typed prompt, a Feishu command or a background wake began it;
  - a typed prompt is a "user" entry with origin.kind == "human";
  - "assistant" entries hold thinking / tool_use / text blocks, message.usage and
    message.model; "user" entries hold tool_result blocks and permissionMode;
  - the task list is the newest TodoWrite input, or the desktop app's
    mcp__ccd_turn__update_plan calls replayed from its last full item list.
Only the tail is read: transcripts reach hundreds of MB.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional

TAIL_BYTES = 2 * 1024 * 1024
SCAN_CHUNK = 1024 * 1024
SCAN_MAX_BYTES = 64 * 1024 * 1024
PLAN_TOOL = "mcp__ccd_turn__update_plan"
STEP_PAGE = 12


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def read_tail(path: Path, tail_bytes: int = TAIL_BYTES) -> list[dict[str, Any]]:
    """Complete entries in the last tail_bytes of the file (never cached: the listener lives on)."""
    with path.open("rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - tail_bytes))
        lines = f.read().split(b"\n")
    if size > tail_bytes:
        lines = lines[1:]  # cut in the middle
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _entries_backwards(path: Path, needle: bytes, max_bytes: int) -> Iterator[Optional[dict[str, Any]]]:
    """Entries whose line contains needle, newest first, reading at most max_bytes from
    the end; a final None means the limit was reached before the start of the file."""
    with path.open("rb") as f:
        size = f.seek(0, os.SEEK_END)
        end, carry = size, b""
        while end > 0:
            if size - end >= max_bytes:
                yield None
                return
            start = max(0, end - SCAN_CHUNK)
            f.seek(start)
            lines = (f.read(end - start) + carry).split(b"\n")
            carry = lines.pop(0) if start > 0 else b""  # may be cut: joined with the next chunk
            for line in reversed(lines):
                if needle not in line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    yield entry
            end = start


def last_entry_with(path: Path, field: str, entry_type: str,
                    max_bytes: int = SCAN_MAX_BYTES) -> Optional[dict[str, Any]]:
    """The newest entry of entry_type that has a top-level field, reading backwards
    from the end (some facts are written once per turn and lie far before the tail)."""
    for entry in _entries_backwards(path, json.dumps(field).encode("ascii"), max_bytes):
        if entry is not None and entry.get("type") == entry_type and entry.get(field):
            return entry
    return None


def turn_start_of(path: Path, max_bytes: int = SCAN_MAX_BYTES) -> tuple[Optional[float], bool]:
    """Start of the newest turn, reading backwards until an entry of the turn before it:
    a long turn's output can be far larger than the tail. (start, complete): complete is
    False when the limit was reached first, so start is only a lower bound of the time."""
    newest, earliest = None, None
    for entry in _entries_backwards(path, b'"promptId"', max_bytes):
        if entry is None:
            return earliest, False
        prompt_id = entry.get("promptId")
        if not prompt_id or entry.get("isSidechain"):
            continue
        if newest is None:
            newest = prompt_id
        elif prompt_id != newest:
            break
        when = timestamp(entry)
        if when is not None and (earliest is None or when < earliest):
            earliest = when
    return earliest, True


def timestamp(entry: dict[str, Any]) -> Optional[float]:
    raw = entry.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _blocks(entry: dict[str, Any]) -> list[dict[str, Any]]:
    content = _as_dict(entry.get("message")).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def main_line(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The session's own entries: subagent (sidechain) entries are left out."""
    return [e for e in entries if not e.get("isSidechain")]


def prompt_text(entry: dict[str, Any]) -> Optional[str]:
    """The text the user typed, when this entry is a typed prompt."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return None
    if _as_dict(entry.get("origin")).get("kind") not in (None, "human"):
        return None
    content = _as_dict(entry.get("message")).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list) and content and all(_as_dict(b).get("type") == "text" for b in content):
        text = "\n".join(str(_as_dict(b).get("text") or "") for b in content)
    else:
        return None  # tool results, images, ...
    text = text.strip()
    if not text or text.startswith("<"):  # <command-name>, <system-reminder>, <task-notification>, ...
        return None
    return text


def turn_start(entries: list[dict[str, Any]]) -> Optional[float]:
    """Start of the newest turn: the earliest entry carrying the newest promptId."""
    newest = None
    for entry in reversed(entries):
        if entry.get("promptId"):
            newest = entry["promptId"]
            break
    if newest is None:
        return None
    times = [t for e in entries if e.get("promptId") == newest for t in [timestamp(e)] if t is not None]
    return min(times) if times else None


def last_prompt(entries: list[dict[str, Any]]) -> tuple[str, Optional[float]]:
    for entry in reversed(entries):
        text = prompt_text(entry)
        if text is not None:
            return text, timestamp(entry)
    return "", None


def latest_text(entries: list[dict[str, Any]]) -> str:
    """Claude's newest text block."""
    for entry in reversed(entries):
        if entry.get("type") != "assistant":
            continue
        for block in reversed(_blocks(entry)):
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                return str(block["text"]).strip()
    return ""


def permission_mode(entries: list[dict[str, Any]]) -> str:
    for entry in reversed(entries):
        if entry.get("type") == "user" and entry.get("permissionMode"):
            return str(entry["permissionMode"])
    return ""


def context_tokens(entries: list[dict[str, Any]]) -> Optional[int]:
    """Tokens in the context at the newest reply: everything that was sent plus what came
    back. Replies with no usage at all (synthetic ones, after an interrupt) are skipped."""
    for entry in reversed(entries):
        usage = _as_dict(_as_dict(entry.get("message")).get("usage"))
        if entry.get("type") != "assistant" or not usage:
            continue
        total = 0
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                total += value
        if total:
            return total
    return None


def context_window(model: str, tokens: Optional[int]) -> int:
    """1M for models run with the long context ("[1m]"), or when more than 200k is already used."""
    if "[1m]" in model.lower() or (tokens or 0) > 200_000:
        return 1_000_000
    return 200_000


def pending_tools(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Tool calls of the newest assistant message that have no result yet."""
    pending: dict[str, tuple[str, dict[str, Any]]] = {}
    for entry in entries:
        message_id = str(_as_dict(entry.get("message")).get("id") or "")
        for block in _blocks(entry):
            if block.get("type") == "tool_use" and block.get("id"):
                pending[str(block["id"])] = (message_id, block)
            elif block.get("type") == "tool_result":
                pending.pop(str(block.get("tool_use_id")), None)
    if not pending:
        return []
    newest = list(pending.values())[-1][0]
    return [block for mid, block in pending.values() if mid == newest]


def tool_summary(name: str, tool_input: dict[str, Any]) -> str:
    """One line on what a tool call does, from its own parameters."""
    for key in ("description", "command", "file_path", "path", "pattern", "url", "query", "prompt", "skill"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}：{' '.join(value.split())}"
    if name == PLAN_TOOL or name == "TodoWrite":
        return f"{name}：更新任务清单"
    return name


# ---------------------------------------------------------------- task list

def _plan_items(items: Any) -> Optional[list[tuple[str, str]]]:
    if not isinstance(items, list):
        return None
    out = []
    for item in items:
        item = _as_dict(item)
        content = str(item.get("content") or "").strip()
        if content:
            out.append((content, str(item.get("status") or "pending")))
    return out


def _apply_plan_step(state: list[tuple[str, str]], tool_input: dict[str, Any]) -> list[tuple[str, str]]:
    """update_plan without "items": advance / done / start, applied to a copy."""
    items = list(state)
    if tool_input.get("advance") is True:
        current = next((i for i, (_, s) in enumerate(items) if s == "in_progress"), None)
        if current is not None:
            items[current] = (items[current][0], "completed")
        start = (current + 1) if current is not None else 0
        upcoming = next((i for i in range(start, len(items)) if items[i][1] == "pending"), None)
        if upcoming is not None:
            items[upcoming] = (items[upcoming][0], "in_progress")
    done = tool_input.get("done")
    for number in done if isinstance(done, list) else []:
        if isinstance(number, int) and 1 <= number <= len(items):
            items[number - 1] = (items[number - 1][0], "completed")
    start = tool_input.get("start")
    if isinstance(start, int) and 1 <= start <= len(items):
        items = [(c, "pending" if s == "in_progress" else s) for c, s in items]
        items[start - 1] = (items[start - 1][0], "in_progress")
    return items


def task_list(entries: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(content, status) of the newest task list: pending / in_progress / completed."""
    state: list[tuple[str, str]] = []
    for entry in main_line(entries):
        if entry.get("type") != "assistant":
            continue
        for block in _blocks(entry):
            if block.get("type") != "tool_use":
                continue
            tool_input = _as_dict(block.get("input"))
            if block.get("name") == "TodoWrite":
                state = _plan_items(tool_input.get("todos")) or []
            elif block.get("name") == PLAN_TOOL:
                items = _plan_items(tool_input.get("items"))
                state = items if items is not None else _apply_plan_step(state, tool_input)
    return state


# ---------------------------------------------------------------- steps

def steps(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """What happened, newest first: typed prompts, tool calls and Claude's text."""
    out: list[dict[str, Any]] = []
    for entry in main_line(entries):
        when = timestamp(entry)
        text = prompt_text(entry)
        if text is not None:
            out.append({"at": when, "who": "用户", "text": text})
            continue
        if entry.get("type") != "assistant":
            continue
        for block in _blocks(entry):
            if block.get("type") == "tool_use":
                out.append({"at": when, "who": "工具",
                            "text": tool_summary(str(block.get("name") or "?"), _as_dict(block.get("input")))})
            elif block.get("type") == "text" and str(block.get("text") or "").strip():
                out.append({"at": when, "who": "Claude", "text": str(block["text"]).strip()})
    out.reverse()
    return out


def view(path: Path, model: str = "") -> dict[str, Any]:
    """Everything the detail card shows, from the transcript at path."""
    entries = main_line(read_tail(path))
    prompt, prompt_at = last_prompt(entries)
    if not prompt:  # typed before the tail: Claude Code also keeps it as a last-prompt entry
        prompt = str((last_entry_with(path, "lastPrompt", "last-prompt") or {}).get("lastPrompt") or "")
    mode = permission_mode(entries) or str(
        (last_entry_with(path, "permissionMode", "user") or {}).get("permissionMode") or "")
    tokens = context_tokens(entries)
    started, complete = turn_start_of(path)
    return {
        "prompt": prompt,
        "prompt_at": prompt_at,
        "turn_start": started,
        "turn_complete": complete,
        "pending": [tool_summary(str(b.get("name") or "?"), _as_dict(b.get("input"))) for b in pending_tools(entries)],
        "tasks": task_list(entries),
        "latest_text": latest_text(entries),
        "permission_mode": mode,
        "context_tokens": tokens,
        "context_window": context_window(model, tokens),
        "steps": steps(entries),
        "last_activity": path.stat().st_mtime,
    }
