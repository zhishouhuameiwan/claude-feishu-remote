#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
The active Claude Code sessions on this computer, for the "会话" command the bound
user sends to the bot (built inside the listener, standard library only).

`claude agents --json` is Claude Code's supported way to read session state from
outside (https://code.claude.com/docs/en/agent-view): one entry per live session
with sessionId, pid, name, cwd, kind, status (busy / waiting / idle) and waitingFor.
Added here from this computer:
  - the model of the session's newest reply and the time of its last activity,
    from the tail of its transcript (<claude home>/projects/<project>/<sessionId>.jsonl);
  - the Feishu cards of the session that still wait for an answer;
  - whether its command waiter (claude_command_wait.py) is running, and the
    commands queued for it.
Active means working, waiting for the user, or idle with a running command
waiter. Each active session gets a box for sending it a command. A session with
an open card that `claude agents` does not list is shown too, so no unanswered
card goes unseen.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import decision_card
import reply_bridge
import session_detail
from decision_card import short
from feishu_notify import limit_text

AGENTS_TIMEOUT_SECONDS = 30
CREATE_NO_WINDOW = 0x08000000  # the listener runs under pythonw: no console window may flash
TAIL_BYTES = 512 * 1024
# One card holds at most this many sessions, each with its send box: with every field
# at its cap an entry takes about 1.9 KB of the 30 KB Feishu accepts. The rest, the
# least urgent ones, are only counted.
MAX_ENTRIES = 12

# status of a live session; background sessions may carry only state (working / blocked / done / ...).
STATUS_LABELS = {"waiting": "等你处理", "busy": "工作中", "idle": "空闲", "working": "工作中",
                 "blocked": "等你处理", "done": "已完成", "failed": "失败", "stopped": "已停止"}
# Statuses that make a session active; idle ones are active only while they can take a command.
URGENCY = {"waiting": 0, "blocked": 0, "busy": 1, "working": 1}
WAITING_FOR_LABELS = {
    "permission prompt": "审批",
    "input needed": "回答问题",
    "sandbox request": "沙箱请求",
    "worker request": "子任务请求",
    "dialog open": "弹窗",
}
CARD_LABELS = {"permission": "审批", "ask": "提问"}  # requests without a type are "next step" questions


class SessionListError(Exception):
    """Why the list could not be read; the text is shown to the user."""


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def run_agents() -> list[dict[str, Any]]:
    """Live sessions as `claude agents --json` reports them."""
    exe = shutil.which("claude")
    if not exe:
        raise SessionListError("找不到 claude 命令：listener 的 PATH 里没有它。"
                               "确认在新开的 PowerShell 里能运行 claude，然后重新运行 setup.ps1。")
    try:
        process = subprocess.Popen([exe, "agents", "--json"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, creationflags=CREATE_NO_WINDOW)
    except OSError as exc:
        raise SessionListError(f"无法运行 claude agents --json：{exc}") from None
    try:
        stdout, stderr = process.communicate(timeout=AGENTS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        # An npm install's claude.cmd runs node under cmd.exe: killing cmd.exe alone
        # leaves node holding the pipes, and reading them would block until it exits.
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)], capture_output=True,
                       creationflags=CREATE_NO_WINDOW)
        process.communicate()
        raise SessionListError(f"claude agents --json 在 {AGENTS_TIMEOUT_SECONDS} 秒内没有返回。") from None
    if process.returncode != 0:
        detail = limit_text(stderr.decode("utf-8", "replace"), 300)
        raise SessionListError(f"claude agents --json 出错（退出码 {process.returncode}）：{detail}")
    try:
        data = json.loads(stdout.decode("utf-8", "replace"))
    except ValueError:
        raise SessionListError("claude agents --json 的输出不是 JSON，可能是 Claude Code 版本太旧。") from None
    if not isinstance(data, list):
        raise SessionListError("claude agents --json 的输出不是会话列表。")
    return [entry for entry in data if isinstance(entry, dict)]


def transcript_of(session_id: str) -> Optional[Path]:
    if not reply_bridge.valid_id(session_id):  # also keeps glob characters out of the pattern
        return None
    found = list((claude_home() / "projects").glob(f"*/{session_id}.jsonl"))
    return max(found, key=lambda p: p.stat().st_mtime) if found else None


def newest_model(path: Path) -> str:
    """Model of the newest reply in the transcript's tail, '' when there is none."""
    with path.open("rb") as f:
        f.seek(max(0, path.stat().st_size - TAIL_BYTES))
        lines = f.read().split(b"\n")
    for line in reversed(lines):
        if b'"model"' not in line:
            continue
        try:
            message = json.loads(line).get("message")
        except (ValueError, AttributeError):
            continue
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and model != "<synthetic>":
            return model
    return ""


def _transcript_facts(session_id: str) -> tuple[str, Optional[float]]:
    """(model, last activity) from the transcript; ('', None) when it cannot be read."""
    try:
        path = transcript_of(session_id)
        return (newest_model(path), path.stat().st_mtime) if path else ("", None)
    except OSError:
        return "", None


def collect() -> list[dict[str, Any]]:
    """Every session, most urgent first; build_card shows only the active ones."""
    cards: dict[str, list[dict[str, Any]]] = {}
    for request in reply_bridge.open_requests():
        cards.setdefault(str(request.get("session_id") or ""), []).append(request)
    rows = []
    for session in run_agents():
        session_id = str(session.get("sessionId") or "")
        pid = session.get("pid")
        rows.append(_row(session_id, name=str(session.get("name") or ""), cwd=str(session.get("cwd") or ""),
                         kind=str(session.get("kind") or ""),
                         status=str(session.get("status") or session.get("state") or ""),
                         waiting_for=str(session.get("waitingFor") or ""),
                         pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                         cards=cards.pop(session_id, []), listed=True))
    for session_id, requests in cards.items():  # open cards of sessions claude agents did not list
        info = requests[0].get("info") or {}
        rows.append(_row(session_id, name=str(info.get("title") or ""), cwd=str(info.get("cwd") or ""),
                         kind="", status="", waiting_for="", pid=None, cards=requests, listed=False))
    rows.sort(key=lambda row: (_urgency(row), -(row["active"] or 0)))
    return rows


def _row(session_id: str, **fields: Any) -> dict[str, Any]:
    model, active = _transcript_facts(session_id)
    return {**fields, "session_id": session_id, "model": model, "active": active,
            "commandable": reply_bridge.command_waiter_alive(session_id),
            "queued": reply_bridge.queued_commands(session_id)}


def is_active(row: dict[str, Any]) -> bool:
    """Working, waiting for the user, or idle but able to take a command from Feishu."""
    return bool(row["cards"]) or row["status"] in URGENCY or row["commandable"]


def _urgency(row: dict[str, Any]) -> int:
    return 0 if row["cards"] else URGENCY.get(row["status"], 2)


def _ago(when: Optional[float], now: float) -> str:
    if when is None:
        return "未知"
    seconds = max(0, now - when)
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


def _state(row: dict[str, Any]) -> str:
    if not row["listed"]:
        return "有待答卡片（claude agents 没有列出这个会话）"
    state = STATUS_LABELS.get(row["status"], short(row["status"], 40) or "状态未知")
    if row["waiting_for"]:
        state += "·" + WAITING_FOR_LABELS.get(row["waiting_for"], short(row["waiting_for"], 40))
    elif row["commandable"] and row["status"] not in URGENCY:
        state += "·可发指令"
    return ("后台·" if row["kind"] == "background" else "") + state


def format_row(row: dict[str, Any], now: float) -> str:
    lines = [f"【{_state(row)}】{short(row['name'] or '（未命名会话）', 60)}"]
    if row["cwd"]:
        lines.append(f"项目：{short(row['cwd'], 120)}")
    facts = [f"模型：{short(row['model'], 60) or '未知'}", f"最近活动：{_ago(row['active'], now)}"]
    if row["cards"]:
        counts: dict[str, int] = {}
        for request in row["cards"]:
            label = CARD_LABELS.get(str(request.get("type") or ""), "决策")
            counts[label] = counts.get(label, 0) + 1
        facts.append("飞书待答卡片：" + "、".join(
            f"{label} {counts[label]} 张" for label in ("审批", "提问", "决策") if label in counts))
    if row["queued"]:
        facts.append(f"排队中的指令 {row['queued']} 条")
    lines.append(" · ".join(facts))
    return "\n".join(lines)


def _send_target(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """What the row's send box posts: the session id, and its pid to tell a session
    that has ended since the card was built. None: no box (a session that
    claude agents did not list and that waits for no command may be gone)."""
    if not (row["commandable"] or row["listed"]):
        return None
    return {"sid": row["session_id"], "pid": row["pid"]}


def build_card(rows: list[dict[str, Any]], now: float) -> dict[str, Any]:
    active = [row for row in rows if is_active(row)]
    counts: dict[str, int] = {}
    for row in active:
        urgency = _urgency(row)
        label = "等你处理" if urgency == 0 else "工作中" if urgency == 1 else "空闲可发指令"
        counts[label] = counts.get(label, 0) + 1
    breakdown = "，".join(f"{label} {n}" for label, n in counts.items())
    summary = (f"活跃会话 {len(active)} 个" + (f"：{breakdown}" if breakdown else "")
               + f"。查询时间 {time.strftime('%H:%M:%S', time.localtime(now))}")
    notes = []
    if len(active) > MAX_ENTRIES:
        notes.append(f"还有 {len(active) - MAX_ENTRIES} 个活跃会话没有列出（排在最后、最不急的那些）。")
    if len(rows) > len(active):
        notes.append(f"另有 {len(rows) - len(active)} 个空闲会话没有列出：它们收不到指令"
                     "（上一轮结束已超过 12 小时，或是装新版插件之前开的会话）。")
    entries = [{"text": format_row(row, now), "send": _send_target(row),
                "sid": row["session_id"] if reply_bridge.valid_id(row["session_id"]) else None}
               for row in active[:MAX_ENTRIES]]
    if not active:
        entries = [{"text": "现在没有活跃的会话。", "send": None}]
    return decision_card.sessions_card(summary, entries, "\n".join(notes))


def error_card(reason: str) -> dict[str, Any]:
    return decision_card.sessions_card(f"读取会话列表失败：{reason}", [], "")


# ---------------------------------------------------------------- one session in detail

MODE_LABELS = {"default": "默认（逐项审批）", "manual": "手动审批", "acceptEdits": "自动接受编辑", "plan": "计划模式",
               "auto": "自动模式", "dontAsk": "不询问", "bypassPermissions": "跳过所有审批"}
BUSY = {"busy", "working", "waiting", "blocked"}
PROMPT_CHARS, PENDING_CHARS, LATEST_CHARS, STEP_CHARS = 500, 200, 1500, 160
MAX_PENDING = 5


def _clock(when: Optional[float], now: float) -> str:
    if when is None:
        return "?"
    same_day = time.localtime(when)[:3] == time.localtime(now)[:3]
    return time.strftime("%H:%M" if same_day else "%m-%d %H:%M", time.localtime(when))


def duration_text(seconds: float) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟"
    return f"{int(seconds // 3600)} 小时 {int(seconds % 3600 // 60)} 分钟"


def _live_entry(session_id: str) -> tuple[Optional[dict[str, Any]], str]:
    """The session as claude agents lists it (None if not listed), and why it could not be read."""
    try:
        for entry in run_agents():
            if str(entry.get("sessionId") or "") == session_id:
                return entry, ""
        return None, ""
    except SessionListError as exc:
        return None, str(exc)


def detail_info(session_id: str, page: int, now: float) -> dict[str, Any]:
    """What decision_card.detail_card shows; every text bounded here."""
    path = transcript_of(session_id)
    if path is None:
        raise SessionListError("找不到这个会话的记录文件。")
    live, agents_error = _live_entry(session_id)
    model = newest_model(path)
    v = session_detail.view(path, model)
    status = str((live or {}).get("status") or (live or {}).get("state") or "")
    if live:
        row = {"listed": True, "status": status, "waiting_for": str(live.get("waitingFor") or ""),
               "commandable": reply_bridge.command_waiter_alive(session_id), "kind": str(live.get("kind") or "")}
        state = _state(row)
    else:
        state = "状态未知：" + short(agents_error, 80) if agents_error else "claude agents 没有列出（可能已结束）"
    title = str((live or {}).get("name") or "")
    tokens = v["context_tokens"]
    context = (f"上下文：约 {tokens / 1000:.0f}k / {v['context_window'] // 1000}k"
               f"（{tokens * 100 // v['context_window']}%）") if tokens else "上下文：未知"
    mode = MODE_LABELS.get(v["permission_mode"], short(v["permission_mode"], 30) or "未知")
    cwd = str((live or {}).get("cwd") or "")
    summary = [f"【{state}】{short(title or '（未命名会话）', 60)}"]
    summary += [f"项目：{short(cwd, 120)}"] if cwd else []
    summary.append(f"模型：{short(model, 60) or '未知'} · 权限模式：{mode} · {context}")
    summary.append(f"最近活动：{_ago(v['last_activity'], now)} · 查询时间 {time.strftime('%H:%M:%S', time.localtime(now))}")
    busy = status in BUSY
    turn = []
    if v["prompt"]:
        when = f"（{_clock(v['prompt_at'], now)}）" if v["prompt_at"] else ""
        turn.append(f"最近一次输入{when}：{limit_text(v['prompt'], PROMPT_CHARS)}")
    if busy and v["turn_start"]:
        at_least = "" if v["turn_complete"] else "至少 "
        turn.append(f"这一轮已运行 {at_least}{duration_text(now - v['turn_start'])}"
                    f"（从 {_clock(v['turn_start'], now)} 开始）")
    pending = [limit_text(line, PENDING_CHARS) for line in v["pending"][:MAX_PENDING]] if busy else []
    first = page * session_detail.STEP_PAGE
    shown = v["steps"][first:first + session_detail.STEP_PAGE]
    steps = [f"{_clock(s['at'], now)} {s['who']}：{short(s['text'], STEP_CHARS)}" for s in shown]
    return {"sid": session_id, "title": title, "summary": summary, "turn": turn, "pending": pending,
            "tasks": [(short(c, 80), s) for c, s in v["tasks"]], "latest_text": limit_text(v["latest_text"], LATEST_CHARS),
            "steps": steps, "page": page, "has_older": len(v["steps"]) > first + len(shown),
            "following": reply_bridge.following(session_id)}


def detail_card(session_id: str, page: int = 0) -> dict[str, Any]:
    """The card for "查看": the session in detail, or why it could not be read."""
    try:
        return decision_card.detail_card(detail_info(session_id, max(0, page), time.time()))
    except (SessionListError, OSError) as exc:
        reason = str(exc) if isinstance(exc, SessionListError) else f"读取会话记录失败（{type(exc).__name__}）。"
        return decision_card.sessions_card(f"查看会话失败：{reason}", [], "")


def latest_text_doc(session_id: str) -> Optional[dict[str, Any]]:
    """Claude's newest text in full, as a document for decision_card.paged_card."""
    path = transcript_of(session_id)
    if path is None:
        return None
    live, _ = _live_entry(session_id)
    text = session_detail.latest_text(session_detail.main_line(session_detail.read_tail(path)))
    title = str((live or {}).get("name") or "")
    return {"header": "Claude Code · 最新的话", "title": title, "template": "blue",
            "lines": [f"会话：{limit_text(title, 100)}"] if title else [], "text": text or "（没有文字）"}


def sessions_card() -> dict[str, Any]:
    """The card answering "会话": the list, or why it could not be read."""
    try:
        return build_card(collect(), time.time())
    except SessionListError as exc:
        return error_card(str(exc))
