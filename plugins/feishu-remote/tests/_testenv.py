"""Shared setup for the offline tests.

Puts the plugin's scripts on sys.path and points every data path (config,
replies, logs, plugin_root.txt) at a fresh temp folder holding a fake, complete
config, so the hooks behave as after setup.ps1 while nothing real is touched.
No test sends anything to Feishu: the senders are replaced in each test.
"""
import json
import os
import sys
import tempfile

HOOK_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
FAKE_CONFIG_FILE = {"app_id": "cli_test", "app_secret": "test-secret", "chat_id": "oc_test",
                    "allowed_open_id": "ou_me"}


def isolate(prefix: str) -> str:
    work = tempfile.mkdtemp(prefix=prefix)
    os.environ["FEISHU_REMOTE_HOME"] = work
    os.environ["FEISHU_REPLY_DIR"] = os.path.join(work, "replies")
    os.environ["FEISHU_HOOK_LOG"] = os.path.join(work, "hooks.log")
    with open(os.path.join(work, "config.json"), "w", encoding="utf-8") as f:
        json.dump(FAKE_CONFIG_FILE, f)
    sys.path.insert(0, HOOK_DIR)
    return work
