#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code Stop hook, registered with "asyncRewake": true: lets the bound user
send commands to this session from Feishu (the "会话" card's send box).

When a turn ends:
  1. if that turn was started by a Feishu command, its last reply
     (last_assistant_message) is sent to Feishu; otherwise it is sent when the
     turn ran longer than the "通知" setting (config.json notify_after_minutes,
     default 5, 0 = off) or the user follows the session from its detail card.
     Either way the card shows the first page; the whole reply is stored for its
     page buttons (reply_bridge.save_doc);
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
from pathlib import Path
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decision_card
import feishu_notify
import reply_bridge
import session_detail
import session_list
from claude_ntfy import log, read_payload, session_title
from feishu_notify import limit_text

# On exit 2 Claude reads this hook's whole stderr, so it must hold the command alone:
# log() writes to stderr too, and is pointed at nothing in main() (hooks.log keeps it).
WAKE_STREAM = sys.stderr.buffer
POLL_SECONDS = 2
# Keep below the hook's "timeout" in hooks.json (43500 s); the same 12 hours as the cards.
MAX_WAIT_SECONDS = 12 * 3600
DEFAULT_NOTICE_MINUTES = 5.0
# The turn woken by a command ends with this Stop, unless it was interrupted (Esc
# fires no Stop) and the user typed on the computer since. A prompt this long after
# the wake means the turn ending now is the user's own: its reply is not relayed.
RELAY_PROMPT_GRACE_SECONDS = 10


def _reply_doc(payload: dict[str, Any], title: str, header: str, extra: list[str]) -> dict[str, Any]:
    """The turn's last reply in full, for a paged card (decision_card.paged_card)."""
    lines = [f"会话：{limit_text(title, 100)}"] if title else []
    lines += [f"项目：{limit_text(str(payload['cwd']), 300)}"] if payload.get("cwd") else []
    reply = str(payload.get("last_assistant_message") or "").strip() or "（这一轮没有文字回复）"
    return {"header": header, "title": title, "template": "green", "lines": lines + extra, "text": reply}


def send_reply_doc(doc: dict[str, Any], as_reply: bool) -> bool:
    """First page of the reply; the rest is stored for the card's page buttons.
    as_reply: the user asked for it (a Feishu command, a followed session), so it goes
    out even while notifications are switched off; a long-turn notice follows the switch.
    False when it was not sent because notifications are off."""
    if not as_reply and not feishu_notify.notifications_enabled():
        return False
    doc_id = reply_bridge.save_doc(doc)
    card = decision_card.paged_card(doc_id, doc, 0)
    if as_reply:
        feishu_notify.send_reply_card(card)
        return True
    return feishu_notify.send_feishu_card(card) is not None


def relay_last_reply(payload: dict[str, Any], session_id: str, title: str) -> bool:
    """The turn that just ended was started from Feishu: send its last reply there.
    True when this turn was such a turn (whether or not sending worked)."""
    marked = reply_bridge.take_relay(session_id)
    if marked is None:
        return False
    if reply_bridge.prompt_time(session_id) > marked + RELAY_PROMPT_GRACE_SECONDS:
        log("[Feishu] typed on the computer since the Feishu command; its reply is not relayed")
        return False
    try:
        send_reply_doc(_reply_doc(payload, title, "[ok] Claude Code · 飞书指令处理完了", []), as_reply=True)
    except Exception as exc:
        log(f"[Feishu] last reply relay failed: {exc}")
    return True


def notice_after_seconds() -> float:
    """config.json "notify_after_minutes": turns at least this long are reported when
    they end; 0 turns it off. Set from Feishu with the bot command "通知"."""
    raw = feishu_notify.read_config_field("notify_after_minutes")
    try:
        minutes = float(raw) if raw else DEFAULT_NOTICE_MINUTES
    except ValueError:
        minutes = DEFAULT_NOTICE_MINUTES
    return max(0.0, minutes) * 60


def turn_seconds(payload: dict[str, Any], now: float) -> tuple[Optional[float], bool]:
    """How long the turn that just ended ran, from its first transcript entry, and whether
    that entry was reached (False: the turn ran at least this long)."""
    path = str(payload.get("transcript_path") or "")
    if not path or not os.path.isfile(path):
        return None, True
    started, complete = session_detail.turn_start_of(Path(path))
    return (None if started is None else max(0.0, now - started)), complete


def notify_done(payload: dict[str, Any], session_id: str, title: str) -> None:
    """Report the end of a long turn, or of any turn of a followed session. A followed
    session was asked for explicitly, so it is sent even while notifications are off."""
    followed = reply_bridge.following(session_id, touch=True)
    limit = notice_after_seconds()
    if not followed and not limit:
        return
    seconds, complete = turn_seconds(payload, time.time())
    long_turn = seconds is not None and (seconds >= limit or not complete)
    if not followed and not long_turn:
        return
    extra = [f"这一轮用时：{'' if complete else '至少 '}{session_list.duration_text(seconds)}"] \
        if seconds is not None else []
    extra.append("（你关注了这个会话：它每轮结束都会推送）" if followed
                 else f"（运行超过 {limit / 60:g} 分钟的一轮结束时推送；发“通知”可以调整或关闭）")
    if send_reply_doc(_reply_doc(payload, title, "[ok] Claude Code · 一轮完成", extra), as_reply=followed):
        log(f"[Feishu] turn end sent ({'followed' if followed else 'long turn'})")


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
    if not relay_last_reply(payload, session_id, title):
        try:
            notify_done(payload, session_id, title)
        except Exception as exc:  # a failed notice must not keep the waiter from starting
            log(f"[Feishu] turn end notice failed: {exc}")
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
