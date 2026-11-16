"""Offline tests for viewing sessions from Feishu: session_detail.py (transcript -> what a
session is doing), the detail card (session_list.detail_info / decision_card.detail_card),
long replies a page at a time (decision_card.paged_card, reply_bridge.save_doc) and
followed sessions. System python, no lark-oapi; nothing is sent to Feishu.
"""
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from _testenv import isolate

WORK = isolate("feishu_view_")
CLAUDE_HOME = os.path.join(WORK, "claude-home")
os.environ["CLAUDE_CONFIG_DIR"] = CLAUDE_HOME

import decision_card as DC  # noqa: E402
import reply_bridge as RB  # noqa: E402
import session_detail as D  # noqa: E402
import session_list as S  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def iso(seconds_ago):
    return datetime.fromtimestamp(time.time() - seconds_ago, timezone.utc).isoformat().replace("+00:00", "Z")


def human(text, ago, pid="p1", mode=None):
    entry = {"type": "user", "timestamp": iso(ago), "promptId": pid, "origin": {"kind": "human"},
             "message": {"role": "user", "content": text}}
    if mode:
        entry["permissionMode"] = mode
    return entry


def assistant(blocks, ago, pid="p1", mid="m1", usage=None, model="claude-opus-5-5", sidechain=False):
    message = {"id": mid, "model": model, "content": blocks}
    if usage:
        message["usage"] = usage
    return {"type": "assistant", "timestamp": iso(ago), "promptId": pid, "isSidechain": sidechain, "message": message}


def tool(name, tid, **inp):
    return {"type": "tool_use", "id": tid, "name": name, "input": inp}


def result(tid, ago, pid="p1"):
    return {"type": "user", "timestamp": iso(ago), "promptId": pid,
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "ok"}]}}


def write_transcript(session_id, entries, project="C--proj", pad_after=0):
    folder = os.path.join(CLAUDE_HOME, "projects", project)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{session_id}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        for _ in range(pad_after):
            f.write(json.dumps({"type": "progress", "pad": "x" * 4000}) + "\n")
    return Path(path)


def card_texts(card):
    return [e["text"]["content"] for e in card["body"]["elements"] if e.get("tag") == "div"]


def buttons(card):
    return [(e["text"]["content"], e["behaviors"][0]["value"]) for e in card["body"]["elements"]
            if e.get("tag") == "button"]


def request_bytes(card):
    return len(json.dumps(json.dumps(card, ensure_ascii=False), ensure_ascii=False).encode("utf-8"))


print("== the turn: start, prompt, Claude's words, pending tools")
entries = [
    human("上一轮的问题", 3600, pid="p0", mode="default"),
    assistant([{"type": "text", "text": "上一轮的回答"}], 3590, pid="p0", mid="m0"),
    human("跑一下测试并修好", 600, pid="p1", mode="acceptEdits"),
    {"type": "user", "isMeta": True, "timestamp": iso(599), "promptId": "p1",
     "message": {"content": "<system-reminder>注入</system-reminder>"}},
    assistant([{"type": "thinking", "thinking": "想一想"}, {"type": "text", "text": "先看看测试"},
               tool("Bash", "t1", command="pytest -q", description="Run tests")], 590, mid="m1",
              usage={"input_tokens": 1000, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 40000,
                     "output_tokens": 500}),
    result("t1", 500),
    assistant([tool("Read", "t2", file_path=r"E:\proj\a.py"), tool("Grep", "t3", pattern="def main")], 400, mid="m2"),
    result("t2", 399),
    assistant([tool("Agent", "s1", description="子代理干的活")], 300, mid="sub", sidechain=True),
]
path = write_transcript("v-1", entries)
tail = D.main_line(D.read_tail(path))
start = D.turn_start(tail)
check(start is not None and 595 < time.time() - start < 605, "  turn start = earliest entry of the newest promptId")
check(D.last_prompt(tail)[0] == "跑一下测试并修好", "  newest typed prompt; system reminders are not prompts")
check(D.latest_text(tail) == "先看看测试", "  Claude's newest text block (thinking skipped)")
check([b["id"] for b in D.pending_tools(tail)] == ["t3"], "  pending: the newest message's call without a result")
check(D.permission_mode(tail) == "acceptEdits", "  permission mode of the newest prompt")
check(D.context_tokens(tail) == 43500, f"  context = all input kinds + output of the newest reply: {D.context_tokens(tail)}")
check(D.context_window("claude-opus-5-5", 43500) == 200_000 and D.context_window("claude-opus-5-5[1m]", 10) == 1_000_000
      and D.context_window("x", 250_000) == 1_000_000, "  window: 200k, or 1M for [1m] / more than 200k used")
steps = D.steps(tail)
check([s["who"] for s in steps] == ["工具", "工具", "工具", "Claude", "用户", "Claude", "用户"],
      f"  steps newest first, subagent entries left out: {[s['who'] for s in steps]}")
check(steps[0]["text"] == "Grep：def main" and steps[2]["text"] == "Bash：Run tests",
      f"  a tool step says what it does: {steps[0]['text']!r}, {steps[2]['text']!r}")
for entry, why in (({"type": "user", "origin": {"kind": "peer"}, "message": {"content": "另一个会话的消息"}}, "peer message"),
                   ({"type": "user", "message": {"content": "<command-name>/model</command-name>"}}, "slash command"),
                   ({"type": "user", "message": {"content": [{"type": "image"}]}}, "image")):
    check(D.prompt_text(entry) is None, f"  not a typed prompt: {why}")
check(D.prompt_text({"type": "user", "message": {"content": [{"type": "text", "text": "块形式"}]}}) == "块形式",
      "  a prompt given as text blocks counts")

print("== facts written once per turn are found before the tail")
path = write_transcript("v-old", [human("很早以前输入的", 7200, mode="plan"),
                                  {"type": "last-prompt", "lastPrompt": "很早以前输入的"}], pad_after=700)
check(os.path.getsize(path) > D.TAIL_BYTES, "  transcript larger than the tail")
v = D.view(path, "claude-opus-5-5")
check(v["permission_mode"] == "plan" and v["prompt"] == "很早以前输入的" and v["prompt_at"] is None,
      f"  mode and prompt from a backwards scan: {v['permission_mode']!r}, {v['prompt']!r}")
fake = write_transcript("v-fake", [human("x", 10, mode="default"),
                                   {"type": "user", "message": {"content": [{"type": "tool_result",
                                    "content": '{"permissionMode": "bypassPermissions"}'}]}}])
check(D.last_entry_with(fake, "permissionMode", "user")["permissionMode"] == "default",
      "  text that merely mentions the field is not taken for it")
check(D.last_entry_with(fake, "permissionMode", "user", max_bytes=0) is None, "  the scan stops at its limit")

print("== task list: TodoWrite, or the desktop plan replayed")
plan = [
    assistant([tool("mcp__ccd_turn__update_plan", "u1", items=[{"content": "读代码", "status": "in_progress"},
                                                                 {"content": "写测试", "status": "pending"},
                                                                 {"content": "实现", "status": "pending"}])], 50),
    assistant([tool("mcp__ccd_turn__update_plan", "u2", advance=True)], 40, mid="m3"),
]
check(D.task_list(plan) == [("读代码", "completed"), ("写测试", "in_progress"), ("实现", "pending")],
      f"  advance completes the current step and starts the next: {D.task_list(plan)}")
plan.append(assistant([tool("mcp__ccd_turn__update_plan", "u3", done=[2], start=3)], 30, mid="m4"))
check(D.task_list(plan) == [("读代码", "completed"), ("写测试", "completed"), ("实现", "in_progress")],
      "  done / start by number")
plan.append(assistant([tool("TodoWrite", "w1", todos=[{"content": "新清单", "status": "pending"}])], 20, mid="m5"))
check(D.task_list(plan) == [("新清单", "pending")], "  the newest list wins, whichever tool wrote it")
check(D.task_list([assistant([tool("mcp__ccd_turn__update_plan", "u9", advance=True)], 5)]) == [],
      "  a step without a list before it -> empty")

print("== the detail card")
FAKE_BIN = os.path.join(WORK, "bin")
os.makedirs(FAKE_BIN)


def agents(entries_):
    listing = os.path.join(FAKE_BIN, "agents.json")
    with open(listing, "w", encoding="utf-8") as f:
        json.dump(entries_, f, ensure_ascii=False)
    script = os.path.join(FAKE_BIN, "claude.bat")
    with open(script, "w", encoding="ascii") as f:
        f.write(f'@echo off\r\ntype "{listing}"\r\n')
    S.shutil.which = lambda name: script if name == "claude" else None


agents([{"sessionId": "v-1", "name": "修测试", "cwd": r"E:\proj", "kind": "interactive", "status": "busy", "pid": 7}])
info = S.detail_info("v-1", 0, time.time())
card = DC.detail_card(info)
texts = card_texts(card)
check(texts[0].startswith("【工作中】修测试") and "项目：E:\\proj" in texts[0], f"  state and name: {texts[0][:30]!r}")
check("权限模式：自动接受编辑" in texts[0] and "上下文：约 44k / 200k（21%）" in texts[0],
      f"  mode and context: {texts[0].splitlines()[2]!r}")
check(any("这一轮已运行 10 分钟" in t for t in texts) and any("最近一次输入" in t and "跑一下测试并修好" in t for t in texts),
      "  this turn's prompt and how long it has run")
check(any(t.startswith("正在执行：\n· Grep：def main") for t in texts), "  the running tool call")
check(any(t.startswith("Claude 最新的话：\n先看看测试") for t in texts), "  Claude's newest words")
labels = [b[0] for b in buttons(card)]
check(labels == ["刷新", "停止这一轮", "看 Claude 最新的话全文", "关注（每轮结束都推送）", "设为当前会话（直接打字发给它）"],
      f"  buttons of a working session: {labels}")
check(dict(buttons(card))["停止这一轮"] == {"kind": "stop", "sid": "v-1"}
      and dict(buttons(card))["设为当前会话（直接打字发给它）"] == {"kind": "current", "sid": "v-1", "on": True,
                                                         "card": "detail"}, "  stop and current carry the session id")
check(buttons(card)[0][1] == {"kind": "detail", "sid": "v-1", "page": 0}, "  refresh carries the session id")
check(all(e["text"]["tag"] == "plain_text" for e in card["body"]["elements"] if e.get("tag") == "div"),
      "  every text is plain_text")
agents([{"sessionId": "v-1", "name": "修测试", "cwd": r"E:\proj", "kind": "interactive", "status": "idle", "pid": 7}])
idle_card = DC.detail_card(S.detail_info("v-1", 0, time.time()))
texts = card_texts(idle_card)
check(not any("已运行" in t or "正在执行" in t for t in texts), "  idle: no running time, no running tool")
check("停止这一轮" not in [b[0] for b in buttons(idle_card)], "  idle: nothing to stop")
RB.set_current("v-1")
check("取消当前会话" in [b[0] for b in buttons(DC.detail_card(S.detail_info("v-1", 0, time.time())))],
      "  the current session offers to cancel it")
RB.set_current(None)
agents([])
texts = card_texts(DC.detail_card(S.detail_info("v-1", 0, time.time())))
check("claude agents 没有列出" in texts[0], "  not listed -> says it may have ended")
check(card_texts(S.detail_card("v-none"))[0].startswith("查看会话失败：找不到"), "  no transcript -> error card")
check(card_texts(S.detail_card("../x"))[0].startswith("查看会话失败"), "  unsafe id -> error card, nothing globbed")

print("== older steps a page at a time")
many = [human("开始", 5000)] + [assistant([tool("Bash", f"t{n}", command=f"echo {n}")], 4000 - n, mid=f"m{n}")
                                for n in range(30)]
write_transcript("v-many", many)
agents([{"sessionId": "v-many", "name": "很多步", "cwd": "E:\\", "kind": "interactive", "status": "idle"}])
first = DC.detail_card(S.detail_info("v-many", 0, time.time()))
check([b[0] for b in buttons(first)][:2] == ["刷新", "更早的步骤"] and "echo 29" in card_texts(first)[-1],
      "  first page: newest steps and an 'older' button")
second = DC.detail_card(S.detail_info("v-many", 1, time.time()))
labels = [b[0] for b in buttons(second)]
check("更早的步骤" in labels and "更新的步骤" in labels and "第 2 页" in card_texts(second)[-1]
      and "echo 17" in card_texts(second)[-1], f"  second page: {labels}")
last = DC.detail_card(S.detail_info("v-many", 2, time.time()))
check("更早的步骤" not in [b[0] for b in buttons(last)] and "开始" in card_texts(last)[-1],
      "  last page: no 'older' button, reaches the first prompt")

print("== worst case stays under Feishu's 30 KB")
worst = {"sid": "s" * 36, "title": "名" * 60, "summary": ["概" * 200] * 4, "turn": ["输" * 600, "跑" * 60],
         "pending": ["工" * 200] * S.MAX_PENDING, "tasks": [("任" * 80, "in_progress")] * 40,
         "latest_text": "话" * S.LATEST_CHARS, "steps": ["步" * (S.STEP_CHARS + 20)] * D.STEP_PAGE,
         "page": 3, "has_older": True, "following": True}
size = request_bytes(DC.detail_card(worst))
check(size < 28000, f"  detail card with every field at its cap: {size} bytes")

print("== long replies, a page at a time")
text = ("第一段\n" + "字" * 2900 + "\n") + "乙" * 5000
pages = DC.split_pages(text)
check(len(pages) == 3 and pages[0].endswith("\n") and "".join(pages) == text,
      f"  split at a line break near the end of a page, nothing lost: {[len(p) for p in pages]}")
check(DC.split_pages("短") == ["短"] and DC.split_pages("") == [""], "  short and empty text: one page")
doc = {"header": "Claude Code · 一轮完成", "title": "会话", "lines": ["会话：会话"], "text": text}
doc_id = RB.save_doc(doc)
check(RB.load_doc(doc_id) == doc and RB.load_doc("../x") is None and RB.load_doc("nope") is None,
      "  stored and read back by id; unsafe or unknown ids give nothing")
card = DC.paged_card(doc_id, doc, 0)
check(card_texts(card)[1] == f"第 1/3 页（共 {len(text)} 字）" and buttons(card) == [
    ("下一页", {"kind": "page", "doc": doc_id, "page": 1})], "  first page: page count and a next button")
card = DC.paged_card(doc_id, doc, 1)
check([b[0] for b in buttons(card)] == ["上一页", "下一页"], "  middle page: both buttons")
card = DC.paged_card(doc_id, doc, 99)
check(card_texts(card)[1].startswith("第 3/3 页") and [b[0] for b in buttons(card)] == ["上一页"],
      "  a page past the end shows the last one")
card = DC.paged_card(doc_id, {"text": "短回复", "lines": []}, 0)
check(card_texts(card) == ["短回复"] and not buttons(card), "  one page: no page line, no buttons")
check(request_bytes(DC.paged_card(doc_id, {"text": "\"\\字" * 3000, "lines": ["行" * 300] * 3}, 0)) < 28000,
      "  a page of characters that need escaping stays under 30 KB")

print("== followed sessions")
check(not RB.following("v-1"), "  not followed at first")
RB.set_following("v-1", True)
flag = RB._path("follow-v-1.flag")
os.utime(flag, (time.time() - 6 * 86400,) * 2)
check(RB.following("v-1", touch=True) and time.time() - flag.stat().st_mtime < 60,
      "  followed; touching keeps the flag fresh for cleanup")
check(dict(buttons(DC.detail_card(S.detail_info("v-1", 0, time.time())))).get("取消关注")
      == {"kind": "follow", "sid": "v-1", "on": False}, "  the detail card offers to unfollow")
RB.set_following("v-1", False)
RB.set_following("v-1", False)
check(not RB.following("v-1") and not RB.following("../x"), "  unfollowed (twice is fine); unsafe ids never followed")
try:
    RB.set_following("../x", True)
    check(False, "  unsafe id refused")
except ValueError:
    check(True, "  unsafe id refused")

print("== a long turn's start lies before the tail")
long_turn = [human("上一轮", 9000, pid="p0"), assistant([{"type": "text", "text": "好"}], 8990, pid="p0", mid="m0"),
             human("读很多大文件", 1500, pid="p1")]
long_turn += [{"type": "user", "timestamp": iso(1400 - n), "promptId": "p1",
               "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}", "content": "大" * 2000}]}}
              for n in range(600)]
path = write_transcript("v-long", long_turn)
check(os.path.getsize(path) > D.TAIL_BYTES, "  the turn's output is larger than the tail")
start, complete = D.turn_start_of(path)
check(start is not None and complete and 1495 < time.time() - start < 1505,
      f"  scanned back to the turn's first entry: {None if start is None else time.time() - start:.0f} s ago")
start, complete = D.turn_start_of(path, max_bytes=D.SCAN_CHUNK)
check(start is not None and not complete and time.time() - start < 1500, "  the scan limit -> a lower bound, marked as such")
check(D.turn_start_of(write_transcript("v-empty", [])) == (None, True), "  no entries -> no start")
v = D.view(path, "m")
check(1495 < time.time() - v["turn_start"] < 1505 and v["turn_complete"], "  the detail view uses the scan")

print("== malformed and synthetic entries")
synthetic = assistant([{"type": "text", "text": "Request interrupted"}], 5, mid="ms", model="<synthetic>",
                      usage={"input_tokens": 0, "output_tokens": 0})
real = assistant([{"type": "text", "text": "x"}], 10, mid="mr", usage={"input_tokens": 3000, "output_tokens": 100})
check(D.context_tokens([real, synthetic]) == 3100, "  a zero-usage synthetic reply does not hide the real context")
check(D.task_list([assistant([tool("mcp__ccd_turn__update_plan", "u1", items=[{"content": "a", "status": "pending"}])], 9),
                   assistant([tool("mcp__ccd_turn__update_plan", "u2", done=2, start="x")], 8, mid="m9")])
      == [("a", "pending")], "  done / start of the wrong type are ignored")
odd = [{"type": "assistant", "message": "not a dict"}, {"type": "user", "message": {"content": 5}},
       {"type": "assistant", "timestamp": "not a time", "message": {"content": [{"type": "tool_use"}]}}]
try:
    D.steps(odd), D.pending_tools(odd), D.task_list(odd), D.context_tokens(odd), D.turn_start(odd)
    check(True, "  malformed entries are skipped")
except Exception as exc:  # noqa: BLE001
    check(False, f"  malformed entries are skipped: {exc!r}")

print("== very long replies")
huge = ("行\n" * 20 + "字" * 2990) * 100
t0 = time.time()
pages = DC.split_pages(huge)
check("".join(pages) == huge and all(len(p) <= DC.PAGE_CHARS for p in pages) and time.time() - t0 < 0.5,
      f"  {len(huge)} characters split in {time.time() - t0:.2f} s, nothing lost")
doc_id = RB.save_doc({"header": "h", "lines": [], "text": "长" * (RB.MAX_DOC_CHARS + 10)})
stored = RB.load_doc(doc_id)["text"]
check(len(stored) < RB.MAX_DOC_CHARS + 100 and stored.endswith("（后面还有 10 字没有保存）"),
      "  a stored reply is capped, saying how much was left out")

print("== the session list offers 查看 for every listed session")
card = DC.sessions_card("x", [{"text": "a", "sid": "v-1", "send": None}, {"text": "b", "sid": None, "send": None}], "")
check(buttons(card)[:1] == [("查看", {"kind": "detail", "sid": "v-1", "open": True})] and len(buttons(card)) == 2,
      "  a 查看 button per session with an id, then refresh")
card = DC.sessions_card("x", [{"text": "a", "sid": "v-1", "stop": True, "current": False,
                               "send": {"sid": "v-1", "pid": 7}}], "")
labels = [b[0] for b in buttons(card)]
check(labels == ["查看", "停止这一轮", "设为当前会话（直接打字发给它）", "刷新"],
      f"  a working session: 查看, 停止, its send box, 设为当前会话: {labels}")
agents([{"sessionId": "v-1", "name": "修测试", "cwd": r"E:\proj", "kind": "interactive", "status": "busy", "pid": 7},
        {"sessionId": "v-many", "name": "很多步", "cwd": "E:\\", "kind": "interactive", "status": "idle", "pid": 8}])
RB.register_command_waiter("v-many", {"waiter_pid": os.getpid(), "waiter_created": RB.process_created(os.getpid())})
RB.set_current("v-many")
listed = S.build_card(S.collect(), time.time())
values = [b[1] for b in buttons(listed)]
check({"kind": "stop", "sid": "v-1"} in values and {"kind": "stop", "sid": "v-many"} not in values,
      "  the list offers 停止 for the working session only")
check({"kind": "current", "sid": "v-many", "on": False, "card": "list"} in values,
      "  ... and marks the current session")
RB.set_current(None)
worst_row = {"session_id": "s" * 36, "name": "名" * 60, "cwd": "路" * 120, "kind": "background", "status": "busy",
             "waiting_for": "permission prompt", "pid": 1, "cards": [{"type": "permission"}] * 3, "listed": True,
             "model": "m" * 60, "active": time.time(), "commandable": True, "queued": 9}
size = request_bytes(S.build_card([dict(worst_row) for _ in range(S.MAX_ENTRIES + 3)], time.time()))
check(size < 28000, f"  a full session list with every button stays under 30 KB: {size} bytes")

shutil.rmtree(WORK, ignore_errors=True)
print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S)")
sys.exit(1 if FAILS else 0)
