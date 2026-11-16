#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code PreToolUse hook (every tool, asyncRewake): keep a stopped session
reachable from Feishu.

A turn stopped from Feishu (claude_turn_control.py answers continue:false) ends
without a Stop hook, so claude_command_wait.py never starts its command waiter.
This hook runs in the background on every tool call, next to the sync one:
  - nothing to do (the usual case): it ends at once, from file names alone;
  - a stop is pending for the session: it waits a moment for the sync hook to
    claim it; if this very call was the one stopped, it tells the user the turn
    stopped and then waits for commands exactly like the Stop hook does (exit 2
    with the command on stderr wakes Claude). A measured run (2026-11-16): an
    asyncRewake PreToolUse hook outlives the stopped turn and wakes the session.

Errors go to hooks.log; exit code 0 unless waking Claude.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths

SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")
NO_TOOL_USE_ID = "no-tool-use-id"  # the same as reply_bridge.NO_TOOL_USE_ID (not imported: start lean)
# While the request is still unclaimed this waits for the sync hook, which may start
# slowly (antivirus, a busy disk); once someone claimed it, the answer comes at once.
CLAIM_WAIT_SECONDS = 20
POLL_SECONDS = 0.2


def _stopped_here(session_id: str, tool_use_id: str) -> bool:
    folder = paths.REPLY_DIR
    request = folder / f"stop-{session_id}.flag"
    marker = folder / f"stopped-{session_id}.{tool_use_id}.flag"
    deadline = time.time() + CLAIM_WAIT_SECONDS
    while time.time() < deadline:
        if marker.is_file():
            return True
        if not request.is_file():  # claimed by another call, or cleared: the rename is atomic
            return marker.is_file()
        time.sleep(POLL_SECONDS)
    return marker.is_file()


def main() -> int:
    started_at = time.time()
    payload = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "{}")
    if not isinstance(payload, dict) or payload.get("agent_id"):
        return 0
    session_id = str(payload.get("session_id") or "")
    tool_use_id = str(payload.get("tool_use_id") or "") or NO_TOOL_USE_ID
    if not (SAFE_ID.fullmatch(session_id) and SAFE_ID.fullmatch(tool_use_id)):
        return 0
    if not _stopped_here(session_id, tool_use_id):
        return 0
    import claude_command_wait as waiter  # heavy: only for the call that stopped the turn
    sys.stderr = open(os.devnull, "w", encoding="utf-8")  # see claude_command_wait.WAKE_STREAM
    if not waiter.feishu_notify.ready():
        return 0
    waiter.reply_bridge.take_stopped(session_id, tool_use_id)
    title = waiter.session_title(str(payload.get("transcript_path") or ""))
    try:
        message_id = waiter.feishu_notify.send_reply_text(
            "[ok] Claude Code · 已停下",
            (f"会话：{waiter.limit_text(title, 100)}\n" if title else "")
            + "这一轮已在它调用工具前停下。可以在会话列表或回复这条消息给它发新的指令。")
        waiter.reply_bridge.remember_message(message_id or "", session_id)
    except Exception as exc:  # the waiter matters more than the notice
        waiter.log(f"[Feishu] stop notice failed: {exc}")
    return waiter.serve_commands(session_id, started_at)


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # never let a broken hook disturb the session
        try:
            from claude_ntfy import log
            log(f"[Feishu] stop waiter failed: {exc}")
        except Exception:
            pass
        code = 0
    raise SystemExit(code)
