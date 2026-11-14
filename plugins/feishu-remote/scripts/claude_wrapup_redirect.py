#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code PreToolUse hook (synchronous) for the desktop "next step" box
(mcp__ccd_turn__wrap_up).

A blocked "next step" question answered in Feishu can only reach the model as
a notification that Claude Code marks NOT USER INPUT, so it never counts as
the user's approval. An AskUserQuestion answered in Feishu goes back through
Claude Code's own answer channel (claude_permission.py) and does count.

So a blocked wrap_up that carries a question is denied, and the reason tells
the model to ask the same question with AskUserQuestion instead. If the model
insists (the same question again within REPEAT_SECONDS, e.g. because
AskUserQuestion is not available to it), the call is let through and
claude_decision.py sends the old card.

claude_decision.py runs on the same call in parallel and waits for the verdict
written here (keyed by tool_use_id) before it sends anything, so a redirected
question never gets a second, useless card. The "deny" verdict is written last:
whatever fails earlier leaves no verdict, claude_decision.py then sends its
card, and the worst case is a denial plus one extra card, never a lost question.

Stdout carries only the deny JSON. Errors before it let the call through; exit code is 0.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu_notify
import reply_bridge
from claude_ntfy import _as_dict, log, read_payload

REPEAT_SECONDS = 120

REASON = (
    "这不是用户拒绝，而是用户设置的本地钩子给这个带提问的“下一步”框改了道"
    "（见会话开头注入的“飞书远程回复规则”）：用户常不在电脑前，只有 AskUserQuestion 的回答"
    "能从飞书作为用户本人的答复送回来。请现在改用 AskUserQuestion 问同一个问题，"
    "选项用你刚才 actions 里的按钮，拿到回答后照做；这个问题不要再用“下一步”框提。"
    "这次的“下一步”框没有显示出来，之后需要汇报进展时再正常调用 wrap_up（不带 blocked 提问）。"
)


def main() -> int:
    payload = read_payload()
    tool_input = _as_dict(payload.get("tool_input"))
    if tool_input.get("blocked") is not True:
        return 0  # claude_decision.py sends nothing for these either
    if not feishu_notify.ready():
        return 0  # setup.ps1 not run yet; the decision hook stays silent too
    session_id = str(payload.get("session_id") or "")
    tool_use_id = str(payload.get("tool_use_id") or "")
    ask = str(tool_input.get("ask") or "").strip()
    # Notifications switched off: the user is at the computer, so the box stays a box.
    redirect = (bool(ask) and feishu_notify.notifications_enabled()
                and reply_bridge.valid_id(tool_use_id) and reply_bridge.valid_id(session_id))
    if redirect and reply_bridge.redirected_recently(session_id, ask, REPEAT_SECONDS):
        log("[Feishu] same next-step question asked again right after a redirect; letting it through")
        redirect = False
    if not redirect:
        reply_bridge.mark_redirect_verdict(tool_use_id, False)  # unsafe id: raises, decision hook waits it out
        return 0
    reply_bridge.mark_redirected(session_id, ask)
    body = {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": REASON}}
    sys.stdout.buffer.write(json.dumps(body).encode("ascii"))  # ASCII-escaped: any console encoding
    sys.stdout.flush()
    reply_bridge.mark_redirect_verdict(tool_use_id, True)  # last: see the module docstring
    log("[Feishu] next-step question redirected to AskUserQuestion")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # unsafe or missing tool_use_id, disk errors: let wrap_up through
        log(f"[Feishu] redirect hook skipped: {exc}")
        code = 0
    raise SystemExit(code)
