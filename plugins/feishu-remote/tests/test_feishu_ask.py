"""Offline tests for Feishu question cards (AskUserQuestion). Run with the hooks venv python.
No real Feishu calls; FEISHU_REPLY_DIR is isolated."""
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from _testenv import HOOK_DIR, isolate

WORK = isolate("feishu_ask_")
os.environ["FEISHU_PERMISSION_GRACE_SECONDS"] = "3.5"
CALLS = os.path.join(WORK, "calls.jsonl")

import feishu_notify  # noqa: E402

FAKE_CONFIG = {"allowed_open_id": "ou_me"}
feishu_notify.read_config_field = lambda name: FAKE_CONFIG.get(name, "")
import feishu_listener as L  # noqa: E402
import reply_bridge as RB  # noqa: E402
import claude_permission as CP  # noqa: E402
import decision_card as DC  # noqa: E402

PERM = os.path.join(HOOK_DIR, "claude_permission.py")
FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


RUNNER = r'''
import json, runpy, sys
hook_dir, script, calls, mode = sys.argv[1:5]
sys.argv = [script] + sys.argv[5:]
sys.path.insert(0, hook_dir)
import feishu_notify
def rec(kind, **kw):
    with open(calls, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, **kw}, ensure_ascii=False) + "\n")
def send_card(card):
    if mode == "card_fail":
        raise RuntimeError("card rejected")
    rec("send_card", card=card); return "om_fake"
feishu_notify.send_feishu_card = send_card
feishu_notify.update_feishu_card = lambda mid, card: rec("update", mid=mid, card=card)
feishu_notify.send_feishu_message = lambda t, b: rec("text", title=t, body=b) or True
if mode == "mark_fail":  # the session marker file stays busy
    import reply_bridge
    def busy(_session_id):
        raise PermissionError(5, "Access is denied")
    reply_bridge.mark_permission = busy
runpy.run_path(script, run_name="__main__")
'''


def calls():
    return [json.loads(l) for l in open(CALLS, encoding="utf-8")] if os.path.exists(CALLS) else []


def now_iso():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ask_entry(tid, inp):
    return {"type": "assistant", "timestamp": now_iso(),
            "message": {"id": "m-" + tid, "content": [{"type": "tool_use", "id": tid, "name": "AskUserQuestion",
                                                       "input": inp}]}}


def result_entry(tid):
    return {"type": "user", "timestamp": now_iso(),
            "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "answered"}]}}


def append(path, *entries):
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")


def new_transcript(name, *entries):
    path = os.path.join(WORK, name + ".jsonl")
    open(path, "w").close()
    append(path, {"type": "custom-title", "customTitle": "提问测试会话"}, *entries)
    return path


def start(session, transcript, inp, mode="ok", extra=None):
    payload = {"hook_event_name": "PermissionRequest", "session_id": session, "cwd": r"E:\Work\Proj",
               "transcript_path": transcript, "tool_name": "AskUserQuestion", "tool_input": inp, **(extra or {})}
    p = subprocess.Popen([sys.executable, "-c", RUNNER, HOOK_DIR, PERM, CALLS, mode],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    p.stdin.close()
    return p


def ask_rid(session, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        for f in glob.glob(os.path.join(os.environ["FEISHU_REPLY_DIR"], "*.pending.json")):
            d = json.load(open(f, encoding="utf-8"))
            if d.get("session_id") == session and d.get("type") == "ask" and d.get("message_id"):
                return d["rid"]
        time.sleep(0.2)
    return None


def click(open_id, value, form=None):
    data = SimpleNamespace(event=SimpleNamespace(operator=SimpleNamespace(open_id=open_id),
                                                 action=SimpleNamespace(value=value, form_value=form)))
    return json.loads(json.dumps(L.on_card_action(data), default=lambda o: o.__dict__))


def last_card():
    return [c for c in calls() if c["kind"] == "send_card"][-1]["card"]


def hook_decision(p):
    out = p.stdout.read()
    return json.loads(out)["hookSpecificOutput"]["decision"] if out else None


def closed_reason(rid, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        o = RB.get_outcome(rid)
        if o:
            return o.get("reason") or o.get("kind")
        time.sleep(0.2)
    return None


def accepted(shown, updated):
    """What Claude Code 2.1.321 really checks when a PermissionRequest hook allows
    AskUserQuestion (default "launcher" mode): only the tool's input schema, not the
    card-answer rules. It then looks answers up by exact question text and treats a key
    that does not match as unanswered, without any error. So: the shown input plus
    answers, every question answered under its exact text, values text or text lists."""
    rest = {k: v for k, v in updated.items() if k != "answers"}
    answers = updated.get("answers")
    return (rest == shown and isinstance(answers, dict)
            and set(answers) == {q["question"] for q in shown["questions"]}
            and all(isinstance(a, str) or (isinstance(a, list) and all(isinstance(x, str) for x in a))
                    for a in answers.values()))


ONE = {"questions": [{"question": "部署到哪个环境？", "header": "环境", "multiSelect": False,
                      "options": [{"label": "测试环境", "description": "先在测试服验证"},
                                  {"label": "正式环境", "description": "直接上线"}]}]}
TWO = {"questions": [
    {"question": "用哪个分支？", "header": "分支", "multiSelect": False,
     "options": [{"label": "main", "description": "主干"}, {"label": "release", "description": "发布分支"}]},
    {"question": "要跑哪些检查？", "header": "检查", "multiSelect": True,
     "options": [{"label": "单元测试", "description": ""}, {"label": "Lint", "description": ""},
                 {"label": "E2E", "description": "慢"}]}]}

print("== one single-choice question: buttons; owner's tap answers Claude")
t = new_transcript("one", ask_entry("a1", ONE))
p = start("s-one", t, ONE)
rid = ask_rid("s-one")
flat = json.dumps(last_card(), ensure_ascii=False)
check(rid and "等你回答" in flat and "部署到哪个环境？" in flat and "先在测试服验证" in flat, "  card shows question and descriptions")
check('"index": 0' in flat and '"index": 1' in flat and '"reply_text"' in flat, "  a button per option and a free-text box")
check('"choice": ' not in flat and "测试环境\"" not in json.dumps([e.get("behaviors") for e in last_card()["body"]["elements"]], ensure_ascii=False),
      "  buttons carry indexes, not labels")
r = click("ou_stranger", {"rid": rid, "kind": "choice", "index": 1})
check(r["toast"]["type"] == "error" and RB.get_outcome(rid) is None, "  stranger refused")
r = click("ou_me", {"rid": rid, "kind": "choice", "index": True})
check(r["toast"]["type"] == "error", "  a boolean index is refused")
r = click("ou_me", {"rid": rid, "kind": "choice", "index": 1})
check(r["toast"]["type"] == "success" and "已在飞书回答：正式环境" in json.dumps(r, ensure_ascii=False), "  owner answered, card updated")
d = p.wait(15) == 0 and hook_decision(p)
check(d and d["behavior"] == "allow" and d["updatedInput"] == {**ONE, "answers": {"部署到哪个环境？": "正式环境"}},
      f"  hook returned allow + answers: {d}")
check(d and accepted(ONE, d["updatedInput"]), "  every question answered under its exact text")

print("== free-text answer for a single question")
t = new_transcript("txt", ask_entry("a2", ONE))
p = start("s-txt", t, ONE)
rid = ask_rid("s-txt")
r = click("ou_me", {"rid": rid, "kind": "text"}, {"reply_text": "   "})
check(r["toast"]["type"] == "error" and RB.get_outcome(rid) is None, "  empty text refused")
click("ou_me", {"rid": rid, "kind": "text"}, {"reply_text": "先别部署，等我回来"})
d = p.wait(15) == 0 and hook_decision(p)
check(d and d["updatedInput"]["answers"] == {"部署到哪个环境？": "先别部署，等我回来"} and accepted(ONE, d["updatedInput"]),
      "  free text delivered as the answer")

print("== several questions with a multi-select: one form")
t = new_transcript("two", ask_entry("a3", TWO))
p = start("s-two", t, TWO)
rid = ask_rid("s-two")
card = last_card()
form = [e for e in card["body"]["elements"] if e.get("tag") == "form"][0]
tags = {e.get("name"): e.get("tag") for e in form["elements"] if e.get("name")}
check(tags.get("q0") == "select_static" and tags.get("q1") == "multi_select_static"
      and tags.get("q0_other") == "input" and tags.get("q1_other") == "input"
      and tags.get("ask_submit") == "button", f"  form controls: {tags}")
check(all(e.get("required") is False for e in form["elements"] if e.get("tag") in ("select_static", "multi_select_static", "input")),
      "  nothing marked required (the listener says which question is missing)")
r = click("ou_me", {"rid": rid, "kind": "select_change"})
check(not r.get("toast") and RB.get_outcome(rid) is None, "  dropdown change callbacks are ignored")
r = click("ou_me", {"rid": rid, "kind": "form"}, {"q0": "1"})
check(r["toast"]["type"] == "error" and "第 2 题还没回答" in r["toast"]["content"], "  missing answer named")
r = click("ou_me", {"rid": rid, "kind": "form"}, {"q0": "1", "q1": ["0", "9"]})
check(r["toast"]["type"] == "error" and RB.get_outcome(rid) is None, "  unknown option refused")
r = click("ou_me", {"rid": rid, "kind": "form"}, {"q0": "abc", "q1": ["0"]})
check(r["toast"]["type"] == "error" and RB.get_outcome(rid) is None, "  forged single pick refused")
click("ou_me", {"rid": rid, "kind": "form"}, {"q0": "1", "q0_other": "", "q1": ["0", "2"], "q1_other": "也跑一下安全扫描"})
d = p.wait(15) == 0 and hook_decision(p)
want = {"用哪个分支？": "release", "要跑哪些检查？": ["单元测试", "E2E", "也跑一下安全扫描"]}
check(d and d["updatedInput"]["answers"] == want, f"  answers: {d and d['updatedInput']['answers']}")
check(d and accepted(TWO, d["updatedInput"]), "  every question answered under its exact text")

print("== free text wins over a pick for single-choice questions")
t = new_transcript("two2", ask_entry("a4", TWO))
p = start("s-two2", t, TWO)
rid = ask_rid("s-two2")
click("ou_me", {"rid": rid, "kind": "form"}, {"q0": "0", "q0_other": "用 hotfix 分支", "q1": ["1"]})
d = p.wait(15) == 0 and hook_decision(p)
check(d and d["updatedInput"]["answers"] == {"用哪个分支？": "用 hotfix 分支", "要跑哪些检查？": ["Lint"]}, "  other text used")

print("== answered at the computer in the grace period -> no card, safety net armed")
t = new_transcript("grace", ask_entry("a5", ONE))
p = start("s-grace", t, ONE)
time.sleep(0.8)
append(t, result_entry("a5"))
check(p.wait(15) == 0 and p.stdout.read() == b"" and ask_rid("s-grace", timeout=1) is None, "  no card, no decision")
check(not RB.permission_marked_recently("s-grace"), "  text reminder allowed to speak again")

print("== answered at the computer after the card; the model omitted multiSelect")
bare = {"questions": [{"question": "部署到哪个环境？", "header": "环境",
                       "options": [{"label": "测试环境", "description": "x"}, {"label": "正式环境", "description": "y"}]}]}
filled = {"questions": [{**bare["questions"][0], "multiSelect": False}]}
t = new_transcript("late", ask_entry("a6", bare))
p = start("s-late", t, filled)
rid = ask_rid("s-late")
append(t, result_entry("a6"))
check(p.wait(15) == 0 and closed_reason(rid) == "answered_on_computer", "  matched by question text and closed")
check(any(c["kind"] == "update" and "已在电脑上处理" in json.dumps(c["card"], ensure_ascii=False) for c in calls()),
      "  card says handled on the computer")

print("== card send failure -> question text reminder")
t = new_transcript("fail", ask_entry("a7", ONE))
p = start("s-fail", t, ONE, mode="card_fail")
check(p.wait(15) == 0 and p.stdout.read() == b"", "  exit 0, no decision")
texts = [c for c in calls() if c["kind"] == "text"]
check(texts and "等你回复" in texts[-1]["title"] and "部署到哪个环境？" in texts[-1]["body"] and "正式环境" in texts[-1]["body"],
      "  text shows the question and options")

print("== plain-text reminder for a pending question: silent while a card covers it")
for sid, expect in (("s-one", False), ("s-nocard", True)):
    tr = new_transcript("n-" + sid, ask_entry("n1", ONE))
    before = len(calls())
    subprocess.run([sys.executable, "-c", RUNNER, HOOK_DIR, os.path.join(HOOK_DIR, "claude_ntfy.py"), CALLS, "ok",
                    "approval", "Claude Code needs your approval"],
                   input=json.dumps({"hook_event_name": "Notification", "session_id": sid, "transcript_path": tr,
                                     "message": "Claude needs your permission"}).encode(), capture_output=True)
    sent = [c for c in calls()[before:] if c["kind"] == "text"]
    check(bool(sent) == expect and (not sent or "部署到哪个环境？" in sent[0]["body"]),
          f"  {sid}: text {'sent with the question' if expect else 'skipped'}")

print("== four long questions stay under Feishu's 30 KB request limit")
big = {"questions": [{"question": f"问题{i}" + "很长的问题" * 200, "header": "头", "multiSelect": i % 2 == 1,
                      "options": [{"label": f"选项{j}" + "长" * 400, "description": "说明" * 600} for j in range(4)]}
                     for i in range(4)]}
info = CP.build_ask_info({"tool_name": "AskUserQuestion", "tool_input": big, "cwd": "C:\\"})
body = {"receive_id": "oc_x", "msg_type": "interactive", "content": json.dumps(DC.ask_card("r", info), ensure_ascii=False)}
size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
check(size < 22000, f"  worst case request {size} bytes (limit 30000)")
noted = {**info, "note": "来自子代理（" + "长" * 60 + "）：飞书这边最多等 5 分钟。后台子代理在电脑上没有弹窗，过时会被拒绝。"}
body = {"receive_id": "oc_x", "msg_type": "interactive", "content": json.dumps(DC.ask_card("r", noted), ensure_ascii=False)}
size = len(json.dumps(body, ensure_ascii=False).encode("utf-8"))
check(size < 22500, f"  with the subagent note: {size} bytes")

print("== a background subagent's question: note on the card, answered within the window")
t = new_transcript("subq", ask_entry("a11", ONE))
p = start("s-subq", t, ONE, extra={"agent_id": "q1w2", "agent_type": "general-purpose"})
rid = ask_rid("s-subq")
check(rid is not None and "来自子代理（general-purpose）" in json.dumps(last_card(), ensure_ascii=False),
      "  question card carries the subagent note")
click("ou_me", {"rid": rid, "kind": "choice", "index": 1})
d = p.wait(15) == 0 and hook_decision(p)
check(d and d["updatedInput"]["answers"] == {"部署到哪个环境？": "正式环境"}, "  answer delivered")

print("== answers keyed by the exact question text, spaces and line breaks included")
odd = {"questions": [{"question": "  先部署哪个服务？\n（换行后还有半句） ", "header": "服务", "multiSelect": False,
                      "options": [{"label": "api", "description": ""}, {"label": "web", "description": ""}]}]}
t = new_transcript("odd", ask_entry("a9", odd))
p = start("s-odd", t, odd)
rid = ask_rid("s-odd")
click("ou_me", {"rid": rid, "kind": "choice", "index": 1})
d = p.wait(15) == 0 and hook_decision(p)
check(d and accepted(odd, d["updatedInput"]) and d["updatedInput"]["answers"] == {odd["questions"][0]["question"]: "web"},
      "  question text used as is")

print("== a busy marker file is retried (Windows os.replace vs. another writer or reader)")
real_replace = os.replace
busy = {"left": 3}


def flaky_replace(src, dst):
    if busy["left"]:
        busy["left"] -= 1
        raise PermissionError(5, "Access is denied")
    return real_replace(src, dst)


os.replace = flaky_replace
try:
    RB.mark_permission("s-retry")
    check(busy["left"] == 0 and RB.permission_marked_recently("s-retry"), "  written once the file is free")
    busy["left"] = 10 ** 6
    try:
        RB.mark_permission("s-retry2")
        gave_up = False
    except PermissionError:
        gave_up = True
    check(gave_up and not glob.glob(os.path.join(os.environ["FEISHU_REPLY_DIR"], "session-s-retry2.perm_at*")),
          "  still busy after every retry: error raised, temp file removed")
finally:
    os.replace = real_replace

print("== the session marker cannot be written -> the card still goes out")
t = new_transcript("busy", ask_entry("a10", ONE))
p = start("s-busy", t, ONE, mode="mark_fail")
rid = ask_rid("s-busy")
check(rid is not None, "  card sent although the marker failed")
click("ou_me", {"rid": rid, "kind": "choice", "index": 0})
d = p.wait(15) == 0 and hook_decision(p)
check(d and d["updatedInput"]["answers"] == {"部署到哪个环境？": "测试环境"}, "  answer delivered")

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S)")
sys.exit(1 if FAILS else 0)
