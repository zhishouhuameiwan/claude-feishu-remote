#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code hooks for the desktop app's sub-session tool
(mcp__ccd_session__start_session), on PreToolUse and PostToolUse.

The tool shows its own cards in the desktop app, and no hook can click them:
  "offer"           asks "New session / Do it here" and waits for the choice;
  "own_initiative"  leaves a "new task" card that runs nothing until it is started.
Before these hooks Feishu heard of such a call only when it needed approval, and
then as a plain approval card; a call that needed none sent nothing (seen
2026-11-16: an offer allowed from Feishu still waited for its choice at the
computer, and a proposal allowed from Feishu was started at the computer 12 hours
later).

PreToolUse: an offer is denied, and the reason tells the model to ask the same
thing with AskUserQuestion, whose answer does come back from Feishu
(claude_permission.py). The same offer again within REPEAT_SECONDS is let through,
as in claude_wrapup_redirect.py. Not while reminders are off: the user is at the
computer then, and the card stays a card.

PostToolUse: once a proposal exists, a notice goes to Feishu. Claude Code runs
PostToolUse only after a call succeeded (an MCP error result goes to
PostToolUseFailure), and a successful "own_initiative" call is a proposal, so the
tool's answer is not looked at. The notice has no button: the desktop app starts a
proposal only from its own card.

The approval card of the tool's permission prompt takes its wording from
summary_lines() (claude_permission.py). The card cannot turn a proposal into a
start. Measured 2026-11-16 in a desktop session, hook answering at once:
  allow alone                                   -> the proposal is made;
  allow + updatedInput equal to the input       -> the call fails ("... requested
  allow + updatedInput, initiation "user_asked"    permissions ... haven't granted it yet").
What does start a session from Feishu is a "user_asked" call allowed on its card,
in a turn the user began at the computer; in a turn begun by a Feishu command or
another session's message the desktop app refuses it even after that approval.

Stdout carries only the deny JSON. Errors go to hooks.log; exit code is 0.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decision_card
import feishu_notify
import reply_bridge
from claude_ntfy import _as_dict, log, read_payload, session_title
from decision_card import short
from feishu_notify import limit_text

TOOL = "mcp__ccd_session__start_session"
REPEAT_SECONDS = 120
REDIRECT_NAME = "offer_redirect"
TITLE_CHARS = 200
TASK_CHARS = 1500

# initiation -> (what kind of request it is, what allowing the call leads to).
# The kind is the model's own statement, so "user_asked" is worded as its claim.
KINDS = {
    "own_initiative": ("Claude 自己提议",
                       "允许后只会在电脑上生成一张“新任务”卡片，子会话要等你回电脑点“开始”才会运行。"),
    "offer": ("询问放在哪里做", "允许后电脑上会弹出“新开会话 / 就在这里做”的选择，还要回电脑选。"),
    "user_asked": ("Claude 称这是你要求的", "允许后子会话立即开始。"),
}

OFFER_REASON = (
    "这不是用户拒绝，而是用户设置的本地钩子给“放到新会话还是就在这里做”的询问（offer）改了道"
    "（见会话开头注入的“飞书远程回复规则”）：用户常不在电脑前，桌面端的这张选择卡片在飞书上点不了，"
    "这次调用会一直等下去。请现在改用 AskUserQuestion 问同一件事：问题里写明任务标题，"
    "选项是「新开会话」和「就在这里做」。用户选「就在这里做」：和在卡片上选 Do it here 一样，"
    "直接在本会话里做。用户选「新开会话」：这是用户本人要求现在开始，请再调用 start_session，"
    "initiation 用 \"user_asked\"，带上 title、完整的 prompt 和 background。例外：这一轮如果不是用户在电脑上"
    "输入发起的（由【飞书指令】、别的会话的消息或自动事件发起），桌面端不允许直接开会话，"
    "这时直接用 \"own_initiative\" 提议，并告诉用户要回电脑上点卡片开始；用 \"user_asked\" 被桌面端拒绝时"
    "也这样处理。这件事不要再用 offer 问。"
)

START_HINT = "子会话还没开始。要开始，请回电脑，在这个会话里点“新任务”卡片上的“开始”。"


def summary_lines(tool_input: dict[str, Any]) -> list[str]:
    """What the call asks for, in plain words, for the top of its approval card."""
    title = short(str(tool_input.get("title") or ""), TITLE_CHARS)
    initiation = tool_input.get("initiation")
    if tool_input.get("proposal_id"):  # whatever else it says: such a call starts the proposed session
        what = f"Claude 想开始之前提议的子会话（{short(str(tool_input['proposal_id']), 60)}）"
        return [f"{what}：{title}" if title else what,
                "方式：开始之前提议的子会话（proposal_id）。允许后子会话立即开始。"]
    lines = [f"Claude 想开一个子会话：{title or '（没有标题）'}"]
    kind = KINDS.get(initiation) if isinstance(initiation, str) else None
    if kind:
        lines.append(f"方式：{kind[0]}（{initiation}）。{kind[1]}")
    elif initiation:  # a kind this plugin does not know: no claim about what allowing does
        lines.append(f"方式：{short(str(initiation), 60)}")
    return lines


def redirect_offer(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The deny decision for an offer, None when the call is left alone."""
    tool_input = _as_dict(payload.get("tool_input"))
    session_id = str(payload.get("session_id") or "")
    if (tool_input.get("initiation") != "offer" or payload.get("agent_id")
            or not reply_bridge.valid_id(session_id)
            or not feishu_notify.ready() or not feishu_notify.notifications_enabled()):
        return None
    title = str(tool_input.get("title") or "").strip()
    if reply_bridge.redirected_recently(session_id, title, REPEAT_SECONDS, name=REDIRECT_NAME):
        log("[Feishu] same sub-session offer again right after a redirect; letting it through")
        return None
    reply_bridge.mark_redirected(session_id, title, name=REDIRECT_NAME)
    log("[Feishu] sub-session offer redirected to AskUserQuestion")
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": OFFER_REASON}}


def _send_text(info: dict[str, Any]) -> None:
    lines = [f"会话: {info['title']}"] if info.get("title") else []
    lines += [f"项目: {info['cwd']}"] if info.get("cwd") else []
    lines.append(f"子会话: {info['subsession']}")
    lines += [f"任务说明: {limit_text(info['task'], 600)}"] if info.get("task") else []
    lines.append(info["status"])
    try:
        feishu_notify.send_feishu_message("[i] " + decision_card.PROPOSAL_HEADER, "\n".join(lines))
    except Exception as exc:
        log(f"[Feishu] proposal text notice failed: {exc}")


def announce_proposal(payload: dict[str, Any]) -> None:
    """Tell Feishu that Claude proposed a sub-session, which waits for a click at the computer."""
    tool_input = _as_dict(payload.get("tool_input"))
    if tool_input.get("initiation") != "own_initiative" or not feishu_notify.ready():
        return
    session_id = str(payload.get("session_id") or "")
    info = {
        "title": session_title(str(payload.get("transcript_path") or "")),
        "cwd": str(payload.get("cwd") or ""),
        "subsession": short(str(tool_input.get("title") or ""), TITLE_CHARS) or "（没有标题）",
        "task": limit_text(str(tool_input.get("prompt") or ""), TASK_CHARS),
        "status": START_HINT,
    }
    try:
        message_id = feishu_notify.send_feishu_card(decision_card.proposal_card(info))
    except Exception as exc:
        log(f"[Feishu] proposal card send failed, falling back to text: {exc}")
        _send_text(info)
        return
    if message_id:  # None: reminders are switched off
        reply_bridge.remember_message(message_id, session_id)  # a reply to the notice goes to this session
        log("[Feishu] sub-session proposal announced")


def main() -> int:
    payload = read_payload()
    if payload.get("tool_name") != TOOL:
        return 0
    event = payload.get("hook_event_name")
    if event == "PreToolUse":
        body = redirect_offer(payload)
        if body is not None:
            sys.stdout.buffer.write(json.dumps(body).encode("ascii"))  # ASCII-escaped: any console encoding
            sys.stdout.flush()
    elif event == "PostToolUse":
        announce_proposal(payload)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # never let a broken hook disturb the call
        log(f"[Feishu] sub-session hook failed: {exc}")
        code = 0
    raise SystemExit(code)
