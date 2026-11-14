#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Small steps of setup.ps1 that are easier in Python. Run with the system python.

    setup_helper.py has-credentials      exit 0 when app_id and app_secret are set
    setup_helper.py save-credentials     read FEISHU_SETUP_APP_ID / FEISHU_SETUP_APP_SECRET
                                         from the environment (kept out of the command
                                         line), check them against Feishu, save them
    setup_helper.py record-root          write this plugin folder to plugin_root.txt
    setup_helper.py wait-listener SECS   wait until a listener worker has started
                                         after this call; exit 1 on timeout
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu_notify
import paths

TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"


def has_credentials() -> int:
    # config.json only, like ready(): FEISHU_APP_ID in the environment must not skip the prompt.
    try:
        found = all(feishu_notify.read_config_field(name) for name in ("app_id", "app_secret"))
    except (OSError, ValueError, RuntimeError):
        return 1
    return 0 if found else 1


def record_root() -> int:
    """Same spelling as claude_session_start.py writes, so the listener sees no move."""
    paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
    paths.PLUGIN_ROOT_FILE.write_text(str(paths.PLUGIN_ROOT), encoding="utf-8")
    return 0


def save_credentials() -> int:
    app_id = os.environ.get("FEISHU_SETUP_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_SETUP_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        print("App ID 或 App Secret 是空的。")
        return 1
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode("utf-8")
    request = urllib.request.Request(TOKEN_URL, data=body, headers={"Content-Type": "application/json"},
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read().decode("utf-8"))
    except OSError as exc:
        print(f"连不上飞书开放平台：{exc}（如果开着代理，检查代理是否在运行）")
        return 1
    if result.get("code") != 0:
        print(f"飞书拒绝了这对 App ID / App Secret：{result.get('msg')}（code {result.get('code')}）")
        return 1
    values = {"app_id": app_id, "app_secret": app_secret}
    previous = feishu_notify.read_config_field("app_id")
    if previous and previous != app_id:
        # chat_id and open_id belong to one app: the old binding would silently break every card.
        values.update({"chat_id": "", "allowed_open_id": ""})
        print("换了一个应用，原来的账号绑定已清除，稍后需要重新绑定。")
    feishu_notify.write_config_fields(values)
    print("App ID 和 App Secret 有效，已保存。")
    return 0


def wait_listener(seconds: float) -> int:
    started = time.time()
    deadline = started + seconds
    while time.time() < deadline:
        try:
            if paths.WORKER_LOG.stat().st_mtime >= started - 1:
                tail = paths.WORKER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-20:]
                if any("worker connecting" in line for line in tail):
                    return 0
        except OSError:
            pass
        time.sleep(1)
    return 1


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "has-credentials":
        return has_credentials()
    if command == "save-credentials":
        return save_credentials()
    if command == "record-root":
        return record_root()
    if command == "wait-listener" and len(sys.argv) > 2:
        return wait_listener(float(sys.argv[2]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
