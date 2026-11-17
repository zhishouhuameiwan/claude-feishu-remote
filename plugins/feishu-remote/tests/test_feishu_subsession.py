"""Offline tests for sub-session requests (mcp__ccd_session__start_session): the offer
redirect, the proposal notice and the approval card's wording. Run with the system
python, as the real hooks do. No real Feishu calls."""
import json
import os
import shutil
import subprocess
import sys

from _testenv import HOOK_DIR, isolate

WORK = isolate("feishu_sub_")
PLUGIN_DIR = os.path.dirname(HOOK_DIR)
HOOK = os.path.join(HOOK_DIR, "claude_subsession.py")
CALLS = os.path.join(WORK, "calls.jsonl")
TOOL = "mcp__ccd_session__start_session"
CWD = r"E:\Work\demo-app"
FAILS = []

import claude_permission as P  # noqa: E402
import claude_session_start  # noqa: E402
import decision_card as D  # noqa: E402
import feishu_notify  # noqa: E402
import reply_bridge as RB  # noqa: E402

RUNNER = r'''
import json, runpy, sys
hook_dir, script, calls, mode = sys.argv[1:5]
sys.argv = [script]
sys.path.insert(0, hook_dir)
import feishu_notify
def rec(kind, **kw):
    with open(calls, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, **kw}, ensure_ascii=False) + "\n")
def send_card(card):
    if mode in ("card_fail", "all_fail"):
        raise RuntimeError("card rejected")
    rec("send_card", card=card); return "om_sub_1"
def send_text(title, body):
    if mode == "all_fail":
        raise RuntimeError("text rejected")
    rec("text", title=title, body=body); return True
if mode == "real":  # the plugin's own senders (they hold the reminders switch); only the transport is faked
    feishu_notify._send_to_chat = lambda msg_type, content: rec("transport", msg_type=msg_type) or "om_real"
else:
    feishu_notify.send_feishu_card = send_card
    feishu_notify.send_feishu_message = send_text
runpy.run_path(script, run_name="__main__")
'''


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def run(payload, mode="ok", raw=None, env=None):
    """(exit code, the JSON on stdout or None, what was sent to Feishu)."""
    if os.path.exists(CALLS):
        os.remove(CALLS)
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    proc = subprocess.run([sys.executable, "-c", RUNNER, HOOK_DIR, HOOK, CALLS, mode],
                          input=data, capture_output=True, timeout=60, env={**os.environ, **(env or {})})
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    sent = [json.loads(line) for line in open(CALLS, encoding="utf-8")] if os.path.exists(CALLS) else []
    return proc.returncode, out, sent


def card_text(card):
    return "\n".join(e["text"]["content"] for e in card["body"]["elements"] if e.get("tag") == "div")


def buttons(card):
    return [e["text"]["content"] for e in card["body"]["elements"] if e.get("tag") == "button"]


def hook_log():
    path = os.environ["FEISHU_HOOK_LOG"]
    return open(path, encoding="utf-8").read() if os.path.exists(path) else ""


TITLED = os.path.join(WORK, "titled.jsonl")
with open(TITLED, "w", encoding="utf-8") as f:
    f.write(json.dumps({"type": "custom-title", "customTitle": "处理本地化审查"}, ensure_ascii=False) + "\n")

OFFER = {"initiation": "offer", "title": "数据看板元数据：未分类条目归类"}
OWN = {"initiation": "own_initiative", "title": "修复测中新发现的越界", "context": "fresh", "use_worktree": False,
       "prompt": "在 E:\\Work\\demo-app 处理复测时新发现的界面越界问题。\n先出方案，等用户批准再改。",
       "background": "上一批改动 38 个文件，还没提交。"}
ASKED = {**OWN, "initiation": "user_asked"}
START = {"proposal_id": "task_490bc9a8", "prompt": "把未分类的条目都归好类。", "background": "数据看板分三端。"}
PROPOSED = ('Proposed "修复测中新发现的越界" (task_id: task_fd6090a2). The user sees a card and nothing runs '
            "until they start it — with a click, or by telling you (then call start_session with proposal_id "
            "task_fd6090a2 and nothing else). It expires in 7 days unless started.")
CHOSE_NEW = ('The user chose "New session" — the session is already opening for "数据看板元数据：未分类条目归类" '
             "(task_id: task_490bc9a8). Write its brief now.")
STARTED = 'Started session "修复测中新发现的越界" (session_id: local_b3e6bdd1-b7da-439d-b7da-ad1fa09928ca).'


def pre(session, tool_input, **extra):
    return {"hook_event_name": "PreToolUse", "tool_name": TOOL, "session_id": session, "cwd": CWD,
            "transcript_path": TITLED, "tool_use_id": "toolu_s1", "tool_input": tool_input, **extra}


def post(session, tool_input, response=None, **extra):
    payload = {"hook_event_name": "PostToolUse", "tool_name": TOOL, "session_id": session, "cwd": CWD,
               "transcript_path": TITLED, "tool_use_id": "toolu_s2", "tool_input": tool_input, **extra}
    if response is not None:
        payload["tool_response"] = response
    return payload


print("== hooks.json wiring")
settings = json.loads(open(os.path.join(PLUGIN_DIR, "hooks", "hooks.json"), "rb").read().decode("utf-8"))
for event, limit in (("PreToolUse", 10), ("PostToolUse", 45)):
    groups = [g for g in settings["hooks"].get(event, []) if g.get("matcher") == TOOL]
    hooks = groups[0]["hooks"] if len(groups) == 1 else []
    check(len(hooks) == 1 and hooks[0]["args"][0].endswith("/scripts/claude_subsession.py")
          and not hooks[0].get("asyncRewake") and not hooks[0].get("async") and 0 < hooks[0].get("timeout", 0) <= limit,
          f"  {event}: {TOOL} -> claude_subsession.py, synchronous, timeout at most {limit} s")
check(TOOL not in RB.CARD_SKIPPED_TOOLS, "  its permission prompt still gets an approval card")

print("== PreToolUse: an offer is redirected to AskUserQuestion")
code, out, sent = run(pre("sub-1", OFFER))
hso = (out or {}).get("hookSpecificOutput", {})
check(code == 0 and hso.get("hookEventName") == "PreToolUse" and hso.get("permissionDecision") == "deny",
      f"  denied (exit {code}, output {out})")
reason = hso.get("permissionDecisionReason", "")
check(all(word in reason for word in ("不是用户拒绝", "AskUserQuestion", "新开会话", "就在这里做", "user_asked",
                                      "own_initiative")), "  the reason says what to ask and what each answer means")
check(not sent, "  nothing is sent to Feishu for the redirect itself")
check(RB.redirected_recently("sub-1", OFFER["title"], 5, name="offer_redirect"), "  the redirect is remembered")
check(not RB.redirected_recently("sub-1", OFFER["title"], 5),
      "  ... apart from the next-step box's redirect record")
code, out, _ = run(pre("sub-1", OFFER))
check(code == 0 and out is None and "letting it through" in hook_log(),
      "  the same offer again right away is let through, and logged")
record = os.path.join(os.environ["FEISHU_REPLY_DIR"], "session-sub-1.offer_redirect.json")
aged = {**json.load(open(record, encoding="utf-8")), "at": __import__("time").time() - 125}
with open(record, "w", encoding="utf-8") as f:
    json.dump(aged, f, ensure_ascii=False)
code, out, _ = run(pre("sub-1", OFFER))
check(out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny",
      "  ... but redirected again once 120 s have passed")
code, out, _ = run(pre("sub-1", {**OFFER, "title": "另一件事"}))
check(out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny", "  another offer is redirected")
RB.mark_redirected("sub-2", "下一步框里的问题")
code, out, _ = run(pre("sub-2", OFFER))
check(out is not None and RB.redirected_recently("sub-2", "下一步框里的问题", 5),
      "  a next-step redirect of the same session neither blocks nor loses its record")

print("== PreToolUse: everything else is left alone")
for name, tool_input in (("own_initiative", OWN), ("user_asked", ASKED), ("proposal_id", START), ("no input", {})):
    code, out, sent = run(pre("sub-3", tool_input))
    check(code == 0 and out is None and not sent, f"  {name}: no output")
code, out, _ = run(pre("sub-3", OFFER, agent_id="agent-1", agent_type="general-purpose"))
check(code == 0 and out is None, "  a subagent's call: no output")
code, out, _ = run(pre("../evil", OFFER))
check(code == 0 and out is None, "  unsafe session id: no output")
for name, tool_input in (("initiation is a list", {"initiation": ["offer"], "title": 5}),
                         ("tool_input is a list", ["offer"])):
    code, out, sent = run(pre("sub-3", tool_input))
    check(code == 0 and out is None and not sent, f"  {name}: exit 0, no output")
code, out, _ = run(pre("sub-3", {"initiation": "offer", "title": 5}))
check(code == 0 and out is not None, "  an offer whose title is not text is still redirected")
code, out, _ = run(None, raw=json.dumps(pre("sub-3", {"initiation": "offer", "title": "坏字符\ud800"})).encode("ascii"))
check(code == 0, "  a title with a lone surrogate: exit 0")
code, out, _ = run(None, raw=b"{not json")
check(code == 0 and out is None, "  broken payload: exit 0, no output")
code, out, _ = run({"hook_event_name": "Stop", "tool_name": TOOL, "session_id": "sub-3", "tool_input": OFFER})
check(code == 0 and out is None, "  another hook event: no output")
open(feishu_notify.SWITCH_FILE, "w").close()
try:
    code, out, _ = run(pre("sub-4", OFFER))
    check(code == 0 and out is None and not RB.redirected_recently("sub-4", OFFER["title"], 5, name="offer_redirect"),
          "  reminders off (the user is at the computer): the offer card stays")
finally:
    os.remove(feishu_notify.SWITCH_FILE)
empty = os.path.join(WORK, "not_set_up")
os.makedirs(empty)
code, out, _ = run(pre("sub-5", OFFER), env={"FEISHU_REMOTE_HOME": empty})
check(code == 0 and out is None, "  setup.ps1 not run: no output")

print("== PostToolUse: a proposal is announced in Feishu")
code, out, sent = run(post("sub-6", OWN, [{"type": "text", "text": PROPOSED}]))
check(code == 0 and out is None and [c["kind"] for c in sent] == ["send_card"],
      f"  one card, empty stdout ({[c['kind'] for c in sent]})")
card = sent[0]["card"] if sent else {"header": {"title": {}, "subtitle": {}}, "body": {"elements": []}}
text = card_text(card)
check(card["header"]["title"].get("content") == "Claude Code · 提议开子会话"
      and card["header"].get("subtitle", {}).get("content") == "处理本地化审查", "  header and the session's title")
check(f"项目：{CWD}" in text and f"子会话：{OWN['title']}" in text and "先出方案，等用户批准再改。" in text,
      "  project, the sub-session's title and its task")
check("回电脑" in text and "开始" in text and not buttons(card), "  says it is started at the computer; no buttons")
check(0 < text.index("▶ 子会话还没开始") < text.index("Claude 写的任务说明："),
      "  the status line comes before the task, which is marked as Claude's text")
check(RB.session_of_message("om_sub_1") == "sub-6", "  replying to the notice goes to that session")
print(text)

for name, response in (("no tool_response", None), ("a plain string", PROPOSED), ("differently worded", {"ok": True})):
    code, out, sent = run(post("sub-6", OWN, response))
    check(code == 0 and out is None and len(sent) == 1,
          f"  {name}: announced all the same (a successful own_initiative call is a proposal)")
for name, tool_input, response in (("offer", OFFER, CHOSE_NEW), ("user_asked", ASKED, STARTED),
                                   ("proposal_id", START, STARTED), ("initiation is a list", {"initiation": ["own_initiative"]}, PROPOSED)):
    code, out, sent = run(post("sub-6", tool_input, [{"type": "text", "text": response}]))
    check(code == 0 and out is None and not sent, f"  {name}: nothing sent")
code, out, sent = run({**post("sub-6", OWN, PROPOSED), "hook_event_name": "PostToolUseFailure"})
check(code == 0 and not sent, "  a failed call (PostToolUseFailure): nothing sent")
code, out, sent = run(post("sub-6", {"initiation": "own_initiative", "title": 5, "prompt": ["x"]}, PROPOSED))
check(code == 0 and len(sent) == 1 and "子会话：5" in card_text(sent[0]["card"]), "  title and prompt that are not text: exit 0")
code, out, sent = run(post("sub-6", OWN, PROPOSED, agent_id="agent-1"))
check(code == 0 and len(sent) == 1, "  a subagent's proposal is announced too")

long_prompt = "很长的任务说明。" * 2000
code, out, sent = run(post("sub-6", {**OWN, "prompt": long_prompt}, PROPOSED))
size = len(json.dumps(json.dumps(sent[0]["card"], ensure_ascii=False), ensure_ascii=False).encode("utf-8")) if sent else 0
check(0 < size < 20000 and "truncated" in card_text(sent[0]["card"]), f"  a long task is cut: {size} bytes")
code, out, sent = run(post("sub-6", {"initiation": "own_initiative", "title": "只有标题"}, PROPOSED))
check(len(sent) == 1 and "子会话：只有标题" in card_text(sent[0]["card"]), "  a proposal without a prompt")

code, out, sent = run(post("sub-7", OWN, PROPOSED), mode="card_fail")
check(code == 0 and out is None and [c["kind"] for c in sent] == ["text"] and "提议开子会话" in sent[0]["title"]
      and OWN["title"] in sent[0]["body"] and "回电脑" in sent[0]["body"], "  card rejected: plain text instead")
code, out, sent = run(post("sub-7", OWN, PROPOSED), mode="all_fail")
check(code == 0 and out is None and not sent and "proposal text notice failed" in hook_log(),
      "  card and text both rejected: exit 0, logged")
code, out, sent = run(post("sub-8", OWN, PROPOSED), mode="real")
check(code == 0 and [c.get("msg_type") for c in sent] == ["interactive"] and RB.session_of_message("om_real") == "sub-8",
      "  through the plugin's own sender: one interactive message")
open(feishu_notify.SWITCH_FILE, "w").close()
try:
    code, out, sent = run(post("sub-9", OWN, PROPOSED), mode="real")
    check(code == 0 and out is None and not sent and RB.session_of_message("om_real") == "sub-8",
          "  reminders off: nothing reaches the transport, exit 0")
finally:
    os.remove(feishu_notify.SWITCH_FILE)
code, out, sent = run(post("sub-9", OWN, PROPOSED), env={"FEISHU_REMOTE_HOME": empty})
check(code == 0 and not sent, "  setup.ps1 not run: nothing sent")

print("== the approval card of a sub-session request")


def approval(tool_input, tool=TOOL):
    info = P.build_info({"tool_name": tool, "tool_input": tool_input, "cwd": CWD, "transcript_path": TITLED})
    return info, D.approval_card("rid00001", info)


info, card = approval(OWN)
text = card_text(card)
check(card["header"]["title"]["content"] == "Claude Code · 申请开子会话", "  its own header")
check(f"Claude 想开一个子会话：{OWN['title']}" in text, "  says what is asked in plain words")
check("own_initiative" in text and "新任务" in text and "回电脑" in text,
      "  own_initiative: allowing only creates the card that is started at the computer")
check(all(f"{key}：" in text for key in OWN) and OWN["prompt"] in text and info["complete"],
      "  every parameter is still shown in full")
check(buttons(card)[:3] == ["允许", "直接开始", "拒绝"], f"  Allow, start now and Deny: {buttons(card)}")
check(D.result_card(info, "已在飞书允许", "green")["header"]["title"]["content"] == "Claude Code · 申请开子会话",
      "  the closed card keeps the header")
print(text)

_, card = approval(OFFER)
check("方式：询问放在哪里做（offer）。允许后电脑上会弹出“新开会话 / 就在这里做”的选择，还要回电脑选。" in card_text(card),
      "  offer: the choice is still made at the computer")
_, card = approval(ASKED)
check("方式：Claude 称这是你要求的（user_asked）。允许后子会话立即开始。" in card_text(card)
      and "回电脑" not in card_text(card), "  user_asked: worded as Claude's claim; allowing starts it")
_, card = approval(START)
check("Claude 想开始之前提议的子会话（task_490bc9a8）" in card_text(card)
      and "方式：开始之前提议的子会话（proposal_id）。允许后子会话立即开始。" in card_text(card),
      "  proposal_id: allowing starts the proposed session")
_, card = approval({**START, "initiation": "own_initiative", "title": "带着提议编号的提议"})
check("方式：开始之前提议的子会话（proposal_id）。允许后子会话立即开始。" in card_text(card)
      and "新任务" not in card_text(card),
      "  proposal_id together with an initiation: still described as starting the proposed session")
_, card = approval({"initiation": "something_new", "title": "标题"})
check("something_new" in card_text(card) and "允许后" not in card_text(card),
      "  an unknown kind: shown as it is, no claim about what allowing does")
_, card = approval({"initiation": "own_initiative", "title": "第一行\n第二行" + "长" * 400})
summary = card_text(card).split("\n参数")[0]
check("第一行 第二行" in summary and len(summary) < 700, "  a long or multi-line title is cut to one line")
info, card = approval({**OWN, "prompt": "x" * 2500})
check(not info["complete"] and "允许" not in buttons(card), "  a parameter too long to show: Allow is not offered")
info, card = approval({"command": "git push", "description": "推送"}, tool="Bash")
check(card["header"]["title"]["content"] == D.APPROVAL_HEADER and "子会话" not in card_text(card),
      "  other tools keep the plain approval card")

print("== the rule injected at session start")
check("新开会话" in claude_session_start.RULE and "子会话" in claude_session_start.RULE,
      "  names the offer redirect and the proposal notice")

shutil.rmtree(WORK, ignore_errors=True)
print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S)")
sys.exit(1 if FAILS else 0)
