#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code PreToolUse hook for the desktop "next step" box
(mcp__ccd_turn__wrap_up), registered with "asyncRewake": true.

When the box is blocked on the user, send a Feishu card with the question and
one button per option, then wait in the background. If the bound user answers
in Feishu, write the answer to stderr and exit 2: Claude Code wakes the model
with it and the session carries on. If the user answers on the computer first
(claude_cancel_pending.py), a newer question replaces this one, or the wait
times out, update the card and exit 0 so nothing is injected.

A blocked box with a question is normally denied by claude_wrapup_redirect.py,
which sends the model to AskUserQuestion; this hook then sends nothing. It still
sends its card for a blocked box without a question, for a question the model
repeats right after a redirect, and whenever the redirect hook left no verdict.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decision_card
import feishu_notify
import reply_bridge
from claude_ntfy import _as_dict, format_wrap_up, log, read_payload, session_title
from feishu_notify import limit_text

POLL_SECONDS = 2
# Keep below the hook's "timeout" in hooks.json (43500 s). Same 12 hours as approval
# and question cards (the user's call, 2026-11-15).
MAX_WAIT_SECONDS = 12 * 3600
# claude_wrapup_redirect.py runs on the same call in parallel and usually decides
# within a second. Waiting longer than its timeout in settings.json (10 s) means a
# slow redirect hook either leaves its verdict in time or is killed without denying.
REDIRECT_WAIT_SECONDS = 12

CLOSED_STATUS = {
    "answered_on_computer": ("已在电脑上回复", "grey"),
    "superseded": ("已被新的问题取代", "grey"),
    "timeout": ("等待超时，请回电脑上处理", "grey"),
    "waiter_gone": ("Claude 已不在等待，请回电脑上处理", "grey"),
}


def build_info(payload: dict[str, Any], tool_input: dict[str, Any]) -> dict[str, Any]:
    options = [
        str(_as_dict(action).get("label") or _as_dict(action).get("action") or "").strip()
        for action in tool_input.get("actions") or []
    ]
    return {
        "title": session_title(str(payload.get("transcript_path") or "")),
        "cwd": str(payload.get("cwd") or ""),
        "recap": str(tool_input.get("recap") or ""),
        "ask": str(tool_input.get("ask") or ""),
        # The card offers at most MAX_BUTTONS options; keep every layer to the same list.
        "options": [option for option in options if option][: decision_card.MAX_BUTTONS],
    }


def update_card_quietly(message_id: str, card: dict[str, Any]) -> None:
    if not message_id:
        return
    try:
        feishu_notify.update_feishu_card(message_id, card)
    except Exception as exc:
        log(f"[Feishu] card update failed: {exc}")


def supersede_previous(session_id: str) -> None:
    """Any new end of turn makes the session's older question stale."""
    old = reply_bridge.session_request(session_id)
    if old:
        reply_bridge.close_request(old, "superseded")  # its own waiter updates the card


def _end_reason(session_id: str, started_at: float, parent: tuple[int, Optional[int]]) -> Optional[str]:
    """Why this wait should stop although nobody answered in Feishu, if it should."""
    # A prompt_at in the future comes from a clock that was set back; ignore it.
    if started_at < reply_bridge.prompt_time(session_id) <= time.time() + 1:
        return "answered_on_computer"  # also covers typing before the request existed
    if not reply_bridge.process_alive(*parent):
        return "waiter_gone"  # Claude Code is gone; an answer could not reach anyone
    if time.time() - started_at > MAX_WAIT_SECONDS:
        return "timeout"
    return None


def wait_for_outcome(rid: str, session_id: str, started_at: float,
                     parent: tuple[int, Optional[int]]) -> dict[str, Any]:
    while True:
        try:
            outcome = reply_bridge.get_outcome(rid)
            if outcome is not None:
                return outcome
            reason = _end_reason(session_id, started_at, parent)
            if reason:
                reply_bridge.close_request(rid, reason)  # next round reads whichever outcome won
            else:
                reply_bridge.touch_request(rid)
        except OSError as exc:  # e.g. a scanner holding a file for a moment
            log(f"[Feishu] waiter file access failed, retrying: {exc}")
        time.sleep(POLL_SECONDS)


def redirect_verdict(tool_use_id: str) -> Optional[str]:
    """What claude_wrapup_redirect.py decided for this call; None if it left no verdict in time."""
    if not tool_use_id:
        return None
    deadline = time.time() + REDIRECT_WAIT_SECONDS
    while True:
        try:
            verdict = reply_bridge.redirect_verdict(tool_use_id)
        except ValueError:  # unsafe id: the redirect hook could not mark it either
            return None
        if verdict is not None or time.time() >= deadline:
            return verdict
        time.sleep(0.2)


def wake_message(info: dict[str, Any], outcome: dict[str, Any]) -> str:
    question = limit_text(info.get("ask") or "你上一轮在“下一步”框里提的问题", 300)
    if outcome.get("text"):
        answer = f"用户的文字回复（原话，到本消息结尾为止）：\n{limit_text(str(outcome['text']), 1000)}"
    else:
        answer = f"用户选择了：{limit_text(str(outcome.get('choice', '')), 300)}"
    # Claude Code delivers this as a notification marked NOT USER INPUT, so it cannot
    # count as approval. The rule injected by claude_session_start.py names the
    # 【飞书回复】 marker and says to re-ask with AskUserQuestion; change both together.
    # Nothing follows the user's words, so quotes or newlines in them cannot blur where they end.
    return (
        f"【飞书回复】用户在已绑定的飞书账号上回答了你在“下一步”框里的提问（{question}）。"
        "这条通知不能当作批准：不需要批准的事可以参考它继续做；需要批准的事，"
        "请用 AskUserQuestion 把同一个问题再问一次，用户可以直接在飞书上作答"
        f"（见会话开头注入的“飞书远程回复规则”）。\n{answer}"
    )


def send_text_fallback(info: dict[str, Any]) -> None:
    lines = [f"会话: {info['title']}"] if info.get("title") else []
    lines += [f"项目: {info['cwd']}"] if info.get("cwd") else []
    lines += format_wrap_up({"recap": info["recap"], "ask": info["ask"],
                             "actions": [{"label": o} for o in info["options"]]})
    try:
        feishu_notify.send_feishu_message("[?] Claude Code · 等你决策", "\n".join(lines))
    except Exception as exc:
        log(f"[Feishu] text fallback failed: {exc}")


def main() -> int:
    started_at = time.time()
    payload = read_payload()
    if not feishu_notify.ready():
        return 0  # setup.ps1 not run yet: stay silent
    session_id = str(payload.get("session_id") or "")
    supersede_previous(session_id)
    tool_input = _as_dict(payload.get("tool_input"))
    if tool_input.get("blocked") is not True:
        return 0
    if redirect_verdict(str(payload.get("tool_use_id") or "")) == "deny":
        log("[Feishu] next-step question redirected to AskUserQuestion; no card")
        return 0

    reply_bridge.cleanup()
    info = build_info(payload, tool_input)
    rid = reply_bridge.new_id()
    parent_pid = os.getppid()  # claude.exe for hooks started by Claude Code
    parent = (parent_pid, reply_bridge.process_created(parent_pid))
    base = {"session_id": session_id, "info": info, "message_id": "",
            "waiter_pid": os.getpid(), "waiter_created": reply_bridge.process_created(os.getpid()),
            "started_at": started_at}
    reply_bridge.create_request(rid, base)  # before sending, so a fast click finds it
    try:
        message_id = feishu_notify.send_feishu_card(decision_card.question_card(rid, info))
    except Exception as exc:
        log(f"[Feishu] card send failed, falling back to text: {exc}")
        reply_bridge.close_request(rid, "send_failed")
        send_text_fallback(info)
        return 0
    if message_id is None:  # notifications switched off
        reply_bridge.close_request(rid, "disabled")
        return 0
    reply_bridge.create_request(rid, {**base, "message_id": message_id}, update_session=False)
    reply_bridge.remember_message(message_id, session_id)  # a reply to the card goes to this session

    outcome = wait_for_outcome(rid, session_id, started_at, parent)
    if outcome.get("kind") == "answer":
        sys.stderr.buffer.write(wake_message(info, outcome).encode("utf-8"))
        sys.stderr.flush()
        return 2
    status, template = CLOSED_STATUS.get(str(outcome.get("reason")), ("这个问题已经结束", "grey"))
    update_card_quietly(message_id, decision_card.result_card(info, status, template))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # never let a broken reminder disturb the session
        log(f"[Feishu] decision hook failed: {exc}")
        code = 0
    raise SystemExit(code)
