#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code Stop hook, registered with "asyncRewake": true: lets the bound user
send commands to this session from Feishu (the "会话" card's send box).

When a turn ends:
  1. if that turn was started by a Feishu command, its last reply
     (last_assistant_message) is sent to Feishu;
  2. the hook then waits in the background for a command the listener queued
     for this session (reply_bridge.queue_command). A command waits in the queue
     while the session is busy, so it is delivered as soon as the turn ends.
     On a command: write it to stderr and exit 2, which wakes Claude with it.

It stops quietly when the user types on the computer (claude_cancel_pending.py
marks the prompt; that turn's own Stop starts a new waiter), when a newer waiter
of the same session takes over, when Claude Code is gone, or after
MAX_WAIT_SECONDS. Claude Code delivers the command as a notification it marks
NOT USER INPUT; the rule injected by claude_session_start.py names the
【飞书指令】 marker and says how to treat it. Change both together.

Stdout stays empty. Errors go to hooks.log; exit code is 0 unless waking Claude.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu_notify
import reply_bridge
from claude_ntfy import log, read_payload, session_title
from feishu_notify import limit_text

# On exit 2 Claude reads this hook's whole stderr, so it must hold the command alone:
# log() writes to stderr too, and is pointed at nothing in main() (hooks.log keeps it).
WAKE_STREAM = sys.stderr.buffer
POLL_SECONDS = 2
# Keep below the hook's "timeout" in hooks.json (43500 s); the same 12 hours as the cards.
MAX_WAIT_SECONDS = 12 * 3600
RELAY_CHARS = 1500
# The turn woken by a command ends with this Stop, unless it was interrupted (Esc
# fires no Stop) and the user typed on the computer since. A prompt this long after
# the wake means the turn ending now is the user's own: its reply is not relayed.
RELAY_PROMPT_GRACE_SECONDS = 10


def relay_last_reply(payload: dict[str, Any], session_id: str, title: str) -> None:
    """The turn that just ended was started from Feishu: send its last reply there."""
    marked = reply_bridge.take_relay(session_id)
    if marked is None:
        return
    if reply_bridge.prompt_time(session_id) > marked + RELAY_PROMPT_GRACE_SECONDS:
        log("[Feishu] typed on the computer since the Feishu command; its reply is not relayed")
        return
    reply = str(payload.get("last_assistant_message") or "").strip() or "（这一轮没有文字回复）"
    lines = [f"会话: {limit_text(title, 100)}"] if title else []
    lines += [f"项目: {limit_text(str(payload['cwd']), 300)}"] if payload.get("cwd") else []
    lines.append(f"\n{limit_text(reply, RELAY_CHARS)}")
    try:
        feishu_notify.send_reply_text("[ok] Claude Code · 飞书指令处理完了", "\n".join(lines))
    except Exception as exc:
        log(f"[Feishu] last reply relay failed: {exc}")


def wake_message(commands: list[dict[str, Any]]) -> str:
    texts = [str(c.get("text") or "").strip() for c in commands]
    if len(texts) == 1:
        body = texts[0]
    else:
        body = "\n".join(f"第 {n} 条：\n{text}" for n, text in enumerate(texts, 1))
    # Nothing follows the user's words, so quotes or newlines in them cannot blur where they end.
    return (
        "【飞书指令】用户在已绑定的飞书账号上给这个会话发来指令（见会话开头注入的“飞书远程回复规则”）。"
        "不需要批准的事直接照做；需要用户拍板的事（提交、推送、删除、在几个方向里选一个等），"
        "先用 AskUserQuestion 确认，用户可以直接在飞书上作答。这一轮结束时，你最后的回复会发回用户的飞书，"
        f"请把结果写在最后的回复里。\n指令原文（到本消息结尾为止）：\n{body}"
    )


def _end_reason(session_id: str, me: dict[str, Any], parent: tuple[int, Optional[int]]) -> Optional[str]:
    """Why this wait should stop without a command, if it should."""
    started_at = me["started_at"]
    # A prompt_at in the future comes from a clock that was set back; ignore it.
    if started_at < reply_bridge.prompt_time(session_id) <= time.time() + 1:
        return "typed on the computer"
    current = reply_bridge.command_waiter(session_id) or {}
    if current.get("waiter_pid") != me["waiter_pid"]:
        return "a newer waiter took over"
    if not reply_bridge.process_alive(*parent):
        return "Claude Code is gone"
    if time.time() - started_at > MAX_WAIT_SECONDS:
        return "timeout"
    return None


def wait_for_commands(session_id: str, me: dict[str, Any],
                      parent: tuple[int, Optional[int]]) -> list[dict[str, Any]]:
    while True:
        try:
            # Ending first: a waiter that was replaced, or whose session the user is
            # typing in, must not take a command away from the next waiter.
            reason = _end_reason(session_id, me, parent)
            if reason:
                log(f"[Feishu] command waiter ends: {reason}")
                return []
            commands = reply_bridge.take_commands(session_id)
            if commands:
                return commands
        except OSError as exc:  # e.g. a scanner holding a file for a moment
            log(f"[Feishu] command waiter file access failed, retrying: {exc}")
        time.sleep(POLL_SECONDS)


def main() -> int:
    started_at = time.time()
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # see WAKE_STREAM
    payload = read_payload()
    session_id = str(payload.get("session_id") or "")
    if not feishu_notify.ready() or not reply_bridge.valid_id(session_id):
        return 0  # setup.ps1 not run yet: stay silent
    title = session_title(str(payload.get("transcript_path") or ""))
    relay_last_reply(payload, session_id, title)
    parent_pid = os.getppid()  # claude.exe for hooks started by Claude Code
    parent = (parent_pid, reply_bridge.process_created(parent_pid))
    me = {"waiter_pid": os.getpid(), "waiter_created": reply_bridge.process_created(os.getpid()),
          "started_at": started_at}
    reply_bridge.register_command_waiter(session_id, me)
    commands = wait_for_commands(session_id, me, parent)
    if not commands:
        return 0
    reply_bridge.mark_relay(session_id)  # before waking: the woken turn's Stop reports back
    WAKE_STREAM.write(wake_message(commands).encode("utf-8"))
    WAKE_STREAM.flush()
    log(f"[Feishu] delivered {len(commands)} Feishu command(s) to the session")
    return 2


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # never let a broken waiter disturb the session
        log(f"[Feishu] command waiter failed: {exc}")
        code = 0
    raise SystemExit(code)
