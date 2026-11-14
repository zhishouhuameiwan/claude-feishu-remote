#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code UserPromptSubmit hook: the user typed on the computer, so the
Feishu decision card of this session is no longer needed. Marks it closed;
the waiting claude_decision.py hook then updates the card and exits quietly.

Stdout must stay empty: UserPromptSubmit stdout is added to the prompt.
Works offline (file operations only) and always exits 0.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu_notify
import reply_bridge
from claude_ntfy import log, read_payload


def main() -> None:
    session_id = str(read_payload().get("session_id") or "")
    if not feishu_notify.ready():
        return  # setup.ps1 not run yet: nothing to cancel
    # Recorded even when no request exists yet, so a waiter that registers a
    # moment later sees the user already answered on the computer.
    reply_bridge.mark_prompt(session_id)
    rid = reply_bridge.session_request(session_id)
    if rid and reply_bridge.get_outcome(rid) is None and reply_bridge.get_request(rid):
        reply_bridge.close_request(rid, "answered_on_computer")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log(f"[Feishu] cancel hook failed: {exc}")
    raise SystemExit(0)
