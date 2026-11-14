#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Enable, disable, or inspect Feishu notifications."""

from __future__ import annotations

import os
import sys
from pathlib import Path


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import paths  # noqa: E402

SWITCH_FILE = paths.SWITCH_FILE


def is_enabled() -> bool:
    return not SWITCH_FILE.exists()


def enable() -> None:
    if SWITCH_FILE.exists():
        SWITCH_FILE.unlink()
    print("Notifications enabled")


def disable() -> None:
    SWITCH_FILE.parent.mkdir(parents=True, exist_ok=True)
    SWITCH_FILE.write_text("", encoding="utf-8")
    print("Notifications disabled")


def status() -> None:
    print("Notification status: enabled" if is_enabled() else "Notification status: disabled")


def main() -> int:
    if len(sys.argv) < 2:
        status()
        return 0

    cmd = sys.argv[1].lower()
    if cmd in ("on", "enable", "开启"):
        enable()
    elif cmd in ("off", "disable", "关闭"):
        disable()
    elif cmd in ("status", "state", "状态"):
        status()
    else:
        print(f"Unknown command: {cmd}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
