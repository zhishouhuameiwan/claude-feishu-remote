#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code PreToolUse hook (every tool): control a running turn from Feishu.

1. Stop: the listener wrote replies/stop-<sid>.flag ("停止" on a card). The first
   tool call of the session that sees it claims it and answers continue:false,
   which ends the turn before the tool runs. A turn ended this way fires no Stop
   hook (measured 2026-11-16), so claude_stop_wait.py, the asyncRewake hook on the
   same call, takes over waiting for Feishu commands.
2. Add a command to a busy turn: commands the user queued for this session while
   it was busy are handed to Claude now, as additionalContext of this tool call,
   instead of after the turn (measured 2026-11-16: the text reaches the model in
   the same turn, right after the call's result).

Both act only on the session's own tool calls: a subagent's call (agent_id in the
payload) is left alone, so a command never lands in a subagent's context.

This runs before every tool call, so it starts lean: standard library and paths
only, until a file for this session says there is something to do (about 60 ms).
Stdout carries only the decision JSON; errors go to hooks.log; exit code is 0.
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
STOP_REASON = ("用户在飞书上叫停了这一轮。只停这一轮：之后用户在电脑上或飞书上发来的新指令照常处理。")


def _pending(session_id: str) -> tuple[bool, bool]:
    """(a stop is requested or was just carried out, commands are queued) for this
    session, from file names alone."""
    folder = paths.REPLY_DIR
    try:
        stop = ((folder / f"stop-{session_id}.flag").is_file()
                or (folder / f"session-{session_id}.stopped_at").is_file())
        queued = any(name.startswith(f"cmd-{session_id}.") and name.endswith(".json")
                     for name in os.listdir(folder))
    except OSError:  # no replies folder yet
        return False, False
    return stop, queued


def _stop_output() -> dict[str, Any]:
    return {"continue": False, "stopReason": STOP_REASON,
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": STOP_REASON}}


def _write(body: dict[str, Any]) -> None:
    sys.stdout.buffer.write(json.dumps(body).encode("ascii"))  # ASCII-escaped: any console encoding
    sys.stdout.flush()


def act(payload: dict[str, Any]) -> None:
    session_id = str(payload.get("session_id") or "")
    if payload.get("agent_id") or not SAFE_ID.fullmatch(session_id):
        return
    stop, queued = _pending(session_id)
    if not (stop or queued):
        return
    import feishu_notify
    import reply_bridge
    from claude_ntfy import log

    if not feishu_notify.ready():
        return
    tool_use_id = str(payload.get("tool_use_id") or "") or reply_bridge.NO_TOOL_USE_ID
    if stop and reply_bridge.claim_stop(session_id, tool_use_id):
        reply_bridge.mark_turn_stopped(session_id)
        log("[Feishu] turn stopped from Feishu at a tool call")
        _write(_stop_output())
        return
    if stop and reply_bridge.turn_stopped(session_id):
        # Another call of the same message (a parallel batch) already stopped the turn;
        # measured 2026-11-16: without this the rest of the batch still runs.
        _write(_stop_output())
        return
    reply_bridge.clear_turn_stopped(session_id)  # over: later calls start lean again
    if reply_bridge.stop_requested(session_id) or reply_bridge.stopped_anywhere(session_id):
        return  # another call of this batch is stopping the turn: its waiter delivers the commands
    commands = reply_bridge.take_commands(session_id) if queued else []
    if not commands:
        return
    reply_bridge.mark_relay(session_id)  # the turn's last reply goes back to Feishu
    log(f"[Feishu] added {len(commands)} Feishu command(s) to the running turn")
    _write({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "additionalContext": reply_bridge.command_text(commands, during_turn=True)}})


def main() -> int:
    try:
        payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    except ValueError:
        return 0
    if isinstance(payload, dict):
        act(payload)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # never let a broken hook disturb the session
        try:
            from claude_ntfy import log
            log(f"[Feishu] turn control hook failed: {exc}")
        except Exception:
            pass
        code = 0
    raise SystemExit(code)
