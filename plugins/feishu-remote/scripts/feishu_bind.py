#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bind the one Feishu account that may answer Claude Code remotely.

Shows a one-time 8-digit code; the user sends it to the bot in a private chat.
The listener (which must be running) records that chat and that account.

Usage: python feishu_bind.py          bind (code valid 30 minutes, single use)
       python feishu_bind.py --reset  forget the bound account first
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feishu_notify
import reply_bridge

WAIT_SECONDS = 600


def main() -> int:
    if "--reset" in sys.argv:
        feishu_notify.write_config_fields({"allowed_open_id": "", "chat_id": ""})
        print("已清除绑定的飞书账号。")
    if feishu_notify.read_config_field("allowed_open_id"):
        print("已经绑定过飞书账号；要换人请加 --reset 重新运行。")
        return 0
    code = reply_bridge.create_bind_code()
    print()
    print(f"    验证码：{code}")
    print()
    print("请在飞书里打开和你的机器人的单聊，把上面 8 位数字原样发过去（30 分钟内有效，只能用一次）。")
    print("正在等待 ...")
    deadline = time.time() + WAIT_SECONDS
    while time.time() < deadline:
        if feishu_notify.ready():
            print("绑定成功，飞书里会收到一条确认消息。")
            return 0
        time.sleep(2)
    print("10 分钟内没有收到验证码。检查 listener 是否在运行（listener.log），然后重新运行本脚本。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
