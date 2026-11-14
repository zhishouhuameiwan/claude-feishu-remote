# -*- coding: utf-8 -*-
"""
Stable entry point for the scheduled task. setup.ps1 copies this file into the
data folder (%LOCALAPPDATA%\\FeishuRemote) and the task runs it with the venv's
pythonw.exe every 5 minutes and at logon.

The plugin folder can change (a new version unzipped elsewhere), so this reads
the current one from plugin_root.txt, which every Claude Code session start
refreshes, and runs that folder's feishu_listener.py. A listener that is
already running holds its lock, so the duplicate exits at once.

pythonw has no console: a failure before the listener's own logging starts is
written to listener-supervisor.log, or the task would fail silently every 5 minutes.
"""

import os
import runpy
import sys
import time
import traceback
from pathlib import Path

here = Path(__file__).resolve().parent
os.environ["FEISHU_REMOTE_HOME"] = str(here)  # the listener and its workers use this data folder
try:
    root = Path((here / "plugin_root.txt").read_text(encoding="utf-8").strip())
    script = root / "scripts" / "feishu_listener.py"
    if not script.is_file():
        raise FileNotFoundError(f"{script} is missing; open a Claude Code session or re-run setup.ps1")
except Exception:
    with open(here / "listener-supervisor.log", "a", encoding="utf-8") as log:
        log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} launcher: cannot find the listener\n")
        log.write(traceback.format_exc())
    raise SystemExit(1)
sys.argv = [str(script)] + sys.argv[1:]
sys.path.insert(0, str(script.parent))
runpy.run_path(str(script), run_name="__main__")
