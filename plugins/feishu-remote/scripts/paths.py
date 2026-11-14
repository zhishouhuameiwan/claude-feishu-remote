# -*- coding: utf-8 -*-
"""
Where the Feishu remote-reply plugin keeps its per-user state.

The plugin code lives in Claude Code's plugin cache, whose folder changes with
every plugin update, so nothing personal may live next to the code. Everything
else goes to one data folder that survives updates and reinstalls:

    %LOCALAPPDATA%\\FeishuRemote          (override: FEISHU_REMOTE_HOME)
        config.json        app_id, app_secret, chat_id, allowed_open_id
        replies\\           hook <-> listener hand-over files
        venv\\              Python with lark-oapi, for the listener only
        plugin_root.txt    the current plugin folder (written at session start)
        launch_listener.py stable entry point for the scheduled task
        *.log, *.lock      listener and hook logs, single-instance locks
"""

from __future__ import annotations

import os
from pathlib import Path

CODE_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = CODE_DIR.parent


def _default_home() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "FeishuRemote"


DATA_DIR = Path(os.environ.get("FEISHU_REMOTE_HOME") or _default_home())
CONFIG_FILE = DATA_DIR / "config.json"
SWITCH_FILE = DATA_DIR / "notify_disabled.flag"
REPLY_DIR = Path(os.environ.get("FEISHU_REPLY_DIR") or DATA_DIR / "replies")
HOOK_LOG = Path(os.environ.get("FEISHU_HOOK_LOG") or DATA_DIR / "hooks.log")
WORKER_LOG = DATA_DIR / "listener.log"
SUPERVISOR_LOG = DATA_DIR / "listener-supervisor.log"
SUPERVISOR_LOCK = DATA_DIR / "listener.lock"
WORKER_LOCK = DATA_DIR / "listener-worker.lock"
PLUGIN_ROOT_FILE = DATA_DIR / "plugin_root.txt"
