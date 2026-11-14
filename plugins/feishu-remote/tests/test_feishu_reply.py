"""Offline tests for the Feishu reply loop (run with the hooks venv python). No real Feishu calls."""
import json
import os
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from _testenv import HOOK_DIR, isolate

WORK = isolate("feishu_reply_")
CALLS = os.path.join(WORK, "calls.jsonl")
sys.path.insert(0, HOOK_DIR)

import feishu_notify  # noqa: E402

FAKE_CONFIG = {"allowed_open_id": "ou_me"}
feishu_notify.read_config_field = lambda name: FAKE_CONFIG.get(name, "")
feishu_notify.write_config_field = lambda name, value: FAKE_CONFIG.__setitem__(name, value)
feishu_notify.write_config_fields = lambda values: FAKE_CONFIG.update(values)
SENT = []
feishu_notify.send_feishu_message = lambda title, body: SENT.append((title, body)) or True
import feishu_listener as L  # noqa: E402
import reply_bridge as RB  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


RUNNER = r'''
import json, runpy, sys
hook_dir, script, calls, mode = sys.argv[1:5]
sys.argv = [script]
sys.path.insert(0, hook_dir)
import feishu_notify
def rec(kind, **kw):
    with open(calls, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": kind, **kw}, ensure_ascii=False) + "\n")
import time
def send_card(card):
    if mode == "card_fail":
        raise RuntimeError("card rejected")
    if mode == "slow_send":
        time.sleep(3)
    rec("send_card", card=card); return "om_fake"
feishu_notify.send_feishu_card = send_card
feishu_notify.update_feishu_card = lambda mid, card: rec("update", mid=mid, card=card)
feishu_notify.send_feishu_message = lambda t, b: rec("text", title=t, body=b) or True
runpy.run_path(script, run_name="__main__")
'''


def calls():
    if not os.path.exists(CALLS):
        return []
    return [json.loads(l) for l in open(CALLS, encoding="utf-8")]


def start_waiter(session, tool_input, mode="ok", extra=None):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "mcp__ccd_turn__wrap_up",
               "session_id": session, "cwd": r"C:\proj", "tool_input": tool_input, **(extra or {})}
    p = subprocess.Popen([sys.executable, "-c", RUNNER, HOOK_DIR, os.path.join(HOOK_DIR, "claude_decision.py"),
                          CALLS, mode], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    p.stdin.close()
    return p


def wait_rid(session, old=None, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        rid = RB.session_request(session)
        if rid and rid != old and (RB.get_request(rid) or {}).get("message_id"):
            return rid
        time.sleep(0.2)
    return None


def click(open_id, value, form=None):
    data = SimpleNamespace(event=SimpleNamespace(operator=SimpleNamespace(open_id=open_id),
                                                 action=SimpleNamespace(value=value, form_value=form)))
    resp = L.on_card_action(data)
    return json.loads(json.dumps(resp, default=lambda o: o.__dict__))


ASK = {"recap": "做完了第一步", "ask": "下一步做哪个？", "blocked": True,
       "actions": [{"label": "继续第二步"}, {"label": "先停下"}]}

print("== not blocked -> exits at once, no card")
p = start_waiter("s-none", {**ASK, "blocked": False})
check(p.wait(10) == 0 and not calls(), "  exit 0, nothing sent")

print("== blocked -> card with buttons; bound user's click wakes Claude (exit 2)")
p = start_waiter("s-1", ASK)
rid = wait_rid("s-1")
check(rid is not None, "  pending request created with message_id")
card = [c for c in calls() if c["kind"] == "send_card"][-1]["card"]
flat = json.dumps(card, ensure_ascii=False)
check("1. 继续第二步" in flat and "2. 先停下" in flat and '"reply_text"' in flat and "下一步做哪个？" in flat,
      "  card has both options, the input box and the question")
r = click("ou_stranger", {"rid": rid, "kind": "choice", "index": 1})
check(r["toast"]["type"] == "error" and RB.get_outcome(rid) is None, "  stranger's click rejected")
r = click("ou_me", {"rid": rid, "kind": "choice", "index": 0})
check(r["toast"]["type"] == "success" and "继续第二步" in json.dumps(r, ensure_ascii=False), "  owner's click accepted, card updated")
code = p.wait(15)
err = p.stderr.read().decode("utf-8")
check(code == 2 and err.endswith("\n用户选择了：继续第二步") and "下一步做哪个？" in err, f"  waiter exit 2 with UTF-8 answer: {err[:80]!r}")
check(err.startswith("【飞书回复】") and "不能当作批准" in err and "AskUserQuestion" in err,
      "  wake text: marker named in CLAUDE.md feishu_replies, not approval, re-ask via AskUserQuestion")
check(p.stdout.read() == b"", "  waiter stdout empty")
r = click("ou_me", {"rid": rid, "kind": "choice", "index": 1})
check(r["toast"]["type"] == "info" and RB.get_outcome(rid)["choice"] == "继续第二步", "  second click ignored")

print("== text reply through the form")
p = start_waiter("s-2", ASK)
rid = wait_rid("s-2")
r = click("ou_me", {"rid": rid, "kind": "text"}, {"reply_text": "  先跑测试再说  "})
code = p.wait(15)
err = p.stderr.read().decode("utf-8")
check(code == 2 and err.endswith("用户的文字回复（原话，到本消息结尾为止）：\n先跑测试再说"), "  text answer delivered")
r = click("ou_me", {"rid": "deadbeefdeadbeef", "kind": "text"}, {"reply_text": ""})
check(r["toast"]["type"] == "error", "  unknown request rejected")

print("== user answers on the computer -> card closed, nothing injected")
p = start_waiter("s-3", ASK)
rid = wait_rid("s-3")
c = subprocess.run([sys.executable, os.path.join(HOOK_DIR, "claude_cancel_pending.py")],
                   input=json.dumps({"session_id": "s-3", "prompt": "hi"}).encode(), capture_output=True)
check(c.returncode == 0 and c.stdout == b"", "  cancel hook exit 0, empty stdout")
code = p.wait(15)
check(code == 0 and p.stderr.read() == b"", "  waiter exit 0, no wake text")
upd = [x for x in calls() if x["kind"] == "update"][-1]
check("已在电脑上回复" in json.dumps(upd["card"], ensure_ascii=False), "  card says answered on the computer")
r = click("ou_me", {"rid": rid, "kind": "choice", "index": 1})
check(r["toast"]["type"] == "info", "  late Feishu click says already handled")

print("== newer question supersedes the older one")
pa = start_waiter("s-4", ASK)
rid_a = wait_rid("s-4")
pb = start_waiter("s-4", {**ASK, "ask": "第二个问题"})
rid_b = wait_rid("s-4", old=rid_a)
check(pa.wait(15) == 0, "  older waiter exits 0")
check(any(x["kind"] == "update" and "已被新的问题取代" in json.dumps(x["card"], ensure_ascii=False) for x in calls()),
      "  older card says superseded")
click("ou_me", {"rid": rid_b, "kind": "choice", "index": 1})
check(pb.wait(15) == 2, "  newer waiter still answerable")

print("== waiter gone (stale heartbeat) -> click refused")
RB.create_request("abcdabcdabcdabcd", {"session_id": "s-5", "info": {"ask": "q"}, "message_id": "om_x"})
old = time.time() - 600
os.utime(os.path.join(os.environ["FEISHU_REPLY_DIR"], "abcdabcdabcdabcd.pending.json"), (old, old))
r = click("ou_me", {"rid": "abcdabcdabcdabcd", "kind": "choice", "index": 0})
check(r["toast"]["type"] == "error" and RB.get_outcome("abcdabcdabcdabcd")["reason"] == "waiter_gone", "  stale request closed")

print("== card send failure -> text fallback, exit 0")
p = start_waiter("s-6", ASK, mode="card_fail")
check(p.wait(15) == 0, "  exit 0")
check(any(x["kind"] == "text" and "下一步做哪个？" in x["body"] for x in calls()), "  text reminder sent instead")

print("== binding: the one-time code sent to the bot in a private chat")


def message(text, open_id="ou_new", chat_id="oc_new", chat_type="p2p", message_type="text", sender_type="user"):
    content = json.dumps({"text": text}, ensure_ascii=False)
    event = SimpleNamespace(
        message=SimpleNamespace(chat_type=chat_type, message_type=message_type, content=content, chat_id=chat_id),
        sender=SimpleNamespace(sender_type=sender_type, sender_id=SimpleNamespace(open_id=open_id)))
    L.on_message(SimpleNamespace(event=event))


def bind_tries():
    return (RB._read_json(RB._path("bind.json")) or {}).get("tries")


FAKE_CONFIG.update({"allowed_open_id": "", "chat_id": ""})
code = RB.create_bind_code()
check(len(code) == 8 and code.isdigit(), f"  code is 8 digits: {code}")
message(code, chat_type="group")
message(code, message_type="image")
message(code, sender_type="bot")
check(not FAKE_CONFIG["allowed_open_id"] and bind_tries() == 0, "  group chat, non-text, bot sender: ignored")
message("你好，在吗")
check(not FAKE_CONFIG["allowed_open_id"] and bind_tries() == 0, "  ordinary chat text: ignored, not a try")
message("1" * 8 if code != "1" * 8 else "2" * 8)
check(not FAKE_CONFIG["allowed_open_id"] and bind_tries() == 1, "  wrong 8-digit code: refused, counted")
SENT.clear()
message(f"  {code}\n")
check(FAKE_CONFIG["allowed_open_id"] == "ou_new" and FAKE_CONFIG["chat_id"] == "oc_new",
      "  right code binds this account and this chat")
check(len(SENT) == 1 and "绑定成功" in SENT[0][0], "  confirmation sent to the new chat")
message(code, open_id="ou_evil", chat_id="oc_evil")
check(FAKE_CONFIG["allowed_open_id"] == "ou_new", "  already bound: another account cannot take over")
FAKE_CONFIG.update({"allowed_open_id": "", "chat_id": ""})
message(code, open_id="ou_evil", chat_id="oc_evil")
check(FAKE_CONFIG["allowed_open_id"] == "", "  a used code cannot bind again")
code = RB.create_bind_code()
wrong = "0" * 8 if code != "0" * 8 else "9" * 8
for _ in range(RB.BIND_CODE_MAX_TRIES):
    message(wrong, open_id="ou_evil")
message(code, open_id="ou_evil")
check(FAKE_CONFIG["allowed_open_id"] == "", f"  {RB.BIND_CODE_MAX_TRIES} wrong tries void the code")
code = RB.create_bind_code()
RB._write_json_atomic(RB._path("bind.json"), {"code": code, "at": time.time() - RB.BIND_CODE_MAX_AGE - 5, "tries": 0})
message(code)
check(FAKE_CONFIG["allowed_open_id"] == "", "  an expired code cannot bind")
try:
    L.on_message(SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(chat_type="p2p", message_type="text", content="not json", chat_id="oc_x"),
        sender=SimpleNamespace(sender_type="user", sender_id=SimpleNamespace(open_id="ou_x")))))
    raised = False
except Exception:
    raised = True
check(not raised and FAKE_CONFIG["allowed_open_id"] == "", "  malformed message content: logged, never raised")
FAKE_CONFIG.update({"chat_id": "oc_test"})

print("== nobody bound -> every answer refused")
p = start_waiter("s-7", ASK)
rid = wait_rid("s-7")
r = click("ou_me", {"rid": rid, "kind": "choice", "index": 1})
check(r["toast"]["type"] == "error" and RB.get_outcome(rid) is None, "  refused while unbound")
subprocess.run([sys.executable, os.path.join(HOOK_DIR, "claude_cancel_pending.py")],
               input=json.dumps({"session_id": "s-7"}).encode(), capture_output=True)
p.wait(15)
FAKE_CONFIG["allowed_open_id"] = "ou_me"

# ---------------- regressions from the independent review ----------------
def closed_reason(rid, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        o = RB.get_outcome(rid)
        if o:
            return o.get("reason") or o.get("kind")
        time.sleep(0.2)
    return None


print("== [M3] buttons carry an index; legacy/forged labels must match an offered option")
long_a = "Run the full test suite, then open a PR for review"
long_b = "Run the full test suite, then merge to main directly"
p = start_waiter("r-1", {**ASK, "actions": [{"label": long_a}, {"label": long_b}]})
rid = wait_rid("r-1")
card = [c for c in calls() if c["kind"] == "send_card"][-1]["card"]
buttons = [e for e in card["body"]["elements"] if e.get("tag") == "button"]
check([b["behaviors"][0]["value"].get("index") for b in buttons] == [0, 1], "  button values are indexes")
check(all("choice" not in b["behaviors"][0]["value"] for b in buttons), "  no label inside button value")
check(long_a in json.dumps(card, ensure_ascii=False) and long_b in json.dumps(card, ensure_ascii=False),
      "  full option texts listed in the card body")
check(buttons[0]["text"]["content"] != buttons[1]["text"]["content"], "  the two buttons look different")
r = click("ou_me", {"rid": rid, "kind": "choice", "choice": "rm -rf everything"})
check(r["toast"]["type"] == "error" and RB.get_outcome(rid) is None, "  forged legacy label refused")
r = click("ou_me", {"rid": rid, "kind": "choice", "index": 7})
check(r["toast"]["type"] == "error", "  out-of-range index refused")
r = click("ou_me", {"rid": rid, "kind": "choice", "choice": long_b})
check(p.wait(15) == 2 and long_b in p.stderr.read().decode("utf-8"), "  legacy exact label still works, full text delivered")

print("== [M2a] a non-blocked end of turn retires the older question")
p = start_waiter("r-2", ASK)
rid = wait_rid("r-2")
q = start_waiter("r-2", {**ASK, "blocked": False})
check(q.wait(10) == 0 and p.wait(15) == 0 and closed_reason(rid) == "superseded", "  old question superseded")

print("== [M2b] user typed on the computer while the request was not findable yet")
p = start_waiter("r-3", ASK, mode="slow_send")
end = time.time() + 10
while not RB.session_request("r-3") and time.time() < end:
    time.sleep(0.05)
rid = RB.session_request("r-3")
os.remove(os.path.join(os.environ["FEISHU_REPLY_DIR"], "session-r-3.txt"))  # cancel hook cannot find it
subprocess.run([sys.executable, os.path.join(HOOK_DIR, "claude_cancel_pending.py")],
               input=json.dumps({"session_id": "r-3"}).encode(), capture_output=True)
check(RB.get_outcome(rid) is None, "  cancel hook could not close it directly")
check(p.wait(15) == 0, "  waiter ends on its own via the prompt timestamp")
check(closed_reason(rid) == "answered_on_computer", f"  closed as answered_on_computer ({closed_reason(rid)})")

print("== [M1] a slow older card send cannot steal the session pointer")
pa = start_waiter("r-4", ASK, mode="slow_send")
time.sleep(1.0)
pb = start_waiter("r-4", {**ASK, "ask": "新问题"})
rid_b = wait_rid("r-4", timeout=15)
check(pa.wait(20) == 0, "  slow older waiter ends (superseded)")
check(RB.session_request("r-4") == rid_b and (RB.get_request(rid_b) or {}).get("info", {}).get("ask") == "新问题",
      "  pointer still names the newer question")
subprocess.run([sys.executable, os.path.join(HOOK_DIR, "claude_cancel_pending.py")],
               input=json.dumps({"session_id": "r-4"}).encode(), capture_output=True)
check(pb.wait(15) == 0 and closed_reason(rid_b) == "answered_on_computer", "  computer reply closes the newer one")

print("== [H2] waiter process dead -> click refused even with a fresh heartbeat")
dead = subprocess.Popen([sys.executable, "-c", "pass"]); dead.wait()
RB.create_request("feedfeedfeedfeed", {"session_id": "r-5", "info": {"ask": "q", "options": ["a"]},
                                       "message_id": "om_x", "waiter_pid": dead.pid})
r = click("ou_me", {"rid": "feedfeedfeedfeed", "kind": "choice", "index": 0})
check(r["toast"]["type"] == "error" and closed_reason("feedfeedfeedfeed") == "waiter_gone", "  dead waiter detected")

print("== [H2] Claude Code (the waiter's parent) gone -> waiter closes itself")
WRAP = ("import subprocess,sys,time; d=sys.stdin.buffer.read(); "
        "p=subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "p.stdin.write(d); p.stdin.close(); print(p.pid, flush=True); time.sleep(120)")
payload = {"hook_event_name": "PreToolUse", "tool_name": "mcp__ccd_turn__wrap_up", "session_id": "r-6",
           "tool_input": ASK}
# Base interpreter, like the real hooks (C:\Python314\python.exe): a venv launcher
# would sit between the wrapper and the waiter and keep the "parent" alive.
w = subprocess.Popen([sys.executable, "-c", WRAP, sys._base_executable, "-c", RUNNER, HOOK_DIR,
                      os.path.join(HOOK_DIR, "claude_decision.py"), CALLS, "ok"],
                     stdin=subprocess.PIPE, stdout=subprocess.PIPE)
w.stdin.write(json.dumps(payload).encode()); w.stdin.close()
child = int(w.stdout.readline())
rid = wait_rid("r-6")
w.kill(); w.wait()
check(closed_reason(rid, timeout=15) == "waiter_gone", "  orphaned waiter closes as waiter_gone")
end = time.time() + 10
while RB.process_alive(child) and time.time() < end:
    time.sleep(0.2)
check(not RB.process_alive(child), "  orphaned waiter process exited")

print("== [re-review 1] live waiter with a late heartbeat (sleep/resume) still accepts the click")
p = start_waiter("q-1", ASK)
rid = wait_rid("q-1")
req = RB.get_request(rid)
check(isinstance(req.get("waiter_created"), int), "  waiter recorded its process creation time")
old = time.time() - 3600
os.utime(os.path.join(os.environ["FEISHU_REPLY_DIR"], f"{rid}.pending.json"), (old, old))
r = click("ou_me", {"rid": rid, "kind": "choice", "index": 1})
check(r["toast"]["type"] == "success" and p.wait(15) == 2, "  answer delivered despite the stale heartbeat")

print("== [re-review 2] reused pid (same number, different process) counts as gone")
me = os.getpid()
RB.create_request("cafecafecafecafe", {"session_id": "q-2", "info": {"ask": "q", "options": ["a"]},
                                       "message_id": "om_x", "waiter_pid": me,
                                       "waiter_created": RB.process_created(me) + 1})
r = click("ou_me", {"rid": "cafecafecafecafe", "kind": "choice", "index": 0})
check(r["toast"]["type"] == "error" and closed_reason("cafecafecafecafe") == "waiter_gone", "  pid reuse detected")
check(RB.process_alive(me, RB.process_created(me)) and not RB.process_alive(me, 1), "  process_alive compares identity")

print("== [re-review 3] a prompt time from the future (clock set back) is ignored")
RB.REPLY_DIR.mkdir(exist_ok=True)
(RB.REPLY_DIR / "session-q-3.prompt_at").write_text(repr(time.time() + 3600), encoding="utf-8")
p = start_waiter("q-3", ASK)
rid = wait_rid("q-3")
time.sleep(3)
check(RB.get_outcome(rid) is None, "  question still open")
click("ou_me", {"rid": rid, "kind": "choice", "index": 0})
check(p.wait(15) == 2, "  and still answerable")

print("== [re-review 5] options beyond the card's buttons are dropped everywhere")
many = {**ASK, "actions": [{"label": f"选项{i}"} for i in range(9)]}
p = start_waiter("q-4", many)
rid = wait_rid("q-4")
check(len(RB.get_request(rid)["info"]["options"]) == 6, "  only 6 options stored")
r = click("ou_me", {"rid": rid, "kind": "choice", "index": 7})
check(r["toast"]["type"] == "error", "  hidden option index refused")
click("ou_me", {"rid": rid, "kind": "choice", "index": 5})
check(p.wait(15) == 2, "  last visible option works")

print("== [L] status line is plain text; ids must fully match")
card = __import__("decision_card").result_card({"ask": "q"}, "[x](http://evil) <at id=all></at>", "green")
check(all(e.get("tag") != "markdown" for e in card["body"]["elements"]), "  no markdown elements in result card")
check(RB.get_request("abc\n") is None and not RB._is_safe("abc\n"), "  trailing newline id rejected")

print("== worker watchdog: a connection down too long ends the worker, so the supervisor reconnects")
WATCH = r'''
import sys, types
sys.path.insert(0, sys.argv[1])
import feishu_listener as L
L.WATCH_SECONDS, L.DISCONNECTED_LIMIT_SECONDS = 0.2, 1.5
state = sys.argv[2]
if state == "down_cleanup_fails":
    def boom(*a, **k):
        raise OSError("replies dir unreadable")
    L.reply_bridge.cleanup = boom
    L.CLEANUP_EVERY_SECONDS = 0
conn = {"down": None, "down_cleanup_fails": None, "up": object(), "missing": "missing"}[state]
client = object() if conn == "missing" else types.SimpleNamespace(_conn=conn)
L._watch(0, client)
'''
for state, expect_exit in (("down", True), ("down_cleanup_fails", True), ("up", False), ("missing", False)):
    w = subprocess.Popen([sys.executable, "-c", WATCH, HOOK_DIR, state],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        code = w.wait(6)
    except subprocess.TimeoutExpired:
        code = None
        w.kill(); w.wait()
    ok = code == 4 if expect_exit else code is None
    check(ok, f"  {state}: {'exits with 4' if expect_exit else 'keeps running'} (got {code})")
check(L._connected(SimpleNamespace(_conn=object())) and not L._connected(SimpleNamespace(_conn=None))
      and L._connected(object()), "  _connected: live / none / attribute missing")

print("== plugin moved or updated: the worker exits so the new code starts")
root_file = L.paths.PLUGIN_ROOT_FILE
new_root = os.path.join(WORK, "new_plugin")
check(not L._code_moved(), "  no plugin_root.txt yet -> stays")
root_file.write_text(str(L.paths.PLUGIN_ROOT), encoding="utf-8")
check(not L._code_moved(), "  same folder -> stays")
root_file.write_text(new_root, encoding="utf-8")
check(not L._code_moved(), "  recorded folder has no listener (deleted, half unzipped) -> stays")
os.makedirs(os.path.join(new_root, "scripts"))
open(os.path.join(new_root, "scripts", "feishu_listener.py"), "w").close()
check(not L._code_moved(), "  recorded folder has only feishu_listener.py (half unzipped) -> stays")
for name in L.LISTENER_FILES:
    open(os.path.join(new_root, "scripts", name), "w").close()
check(L._code_moved(), "  recorded folder holds the complete listener -> moves")
root_file.write_text(str(L.paths.PLUGIN_ROOT).upper() + "\\.\\", encoding="utf-8")
check(not L._code_moved(), "  same folder spelled differently (case, trailing parts) -> stays")
root_file.write_text(new_root, encoding="utf-8")
w = subprocess.Popen([sys.executable, "-c", WATCH, HOOK_DIR, "up"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
try:
    code = w.wait(6)
except subprocess.TimeoutExpired:
    code = None
    w.kill(); w.wait()
check(code == L.CODE_MOVED, f"  watchdog exits with {L.CODE_MOVED} (got {code})")
root_file.unlink()
saved = (L._lock, L.setup_logging, L.subprocess.call)
L._lock, L.setup_logging = (lambda path: object()), (lambda path: None)
L.subprocess.call = lambda *a, **k: L.CODE_MOVED
try:
    check(L.run_supervisor() == 0, "  supervisor exits after such a worker (the task restarts it)")
finally:
    L._lock, L.setup_logging, L.subprocess.call = saved

print("== redirect hook: a blocked next-step question goes to AskUserQuestion")
REDIRECT = os.path.join(HOOK_DIR, "claude_wrapup_redirect.py")


def redirect(session, tool_input, tool_use_id="toolu_r1", raw=None):
    payload = {"hook_event_name": "PreToolUse", "tool_name": "mcp__ccd_turn__wrap_up",
               "session_id": session, "tool_input": tool_input}
    if tool_use_id is not None:
        payload["tool_use_id"] = tool_use_id
    data = raw if raw is not None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    proc = subprocess.run([sys.executable, REDIRECT], input=data, capture_output=True, timeout=20)
    out = json.loads(proc.stdout) if proc.stdout.strip() else None
    return proc.returncode, out


code, out = redirect("rd-1", ASK, "toolu_r1")
hso = (out or {}).get("hookSpecificOutput", {})
check(code == 0 and hso.get("permissionDecision") == "deny" and hso.get("hookEventName") == "PreToolUse"
      and "AskUserQuestion" in hso.get("permissionDecisionReason", ""), f"  blocked + ask -> deny with reason: {out}")
check(RB.redirect_verdict("toolu_r1") == "deny" and RB.redirected_recently("rd-1", ASK["ask"], 5),
      "  verdict 'deny' and the question recorded")
code, out = redirect("rd-1", {**ASK, "ask": "另一个新问题？"}, "toolu_r1b")
check(out is not None and RB.redirect_verdict("toolu_r1b") == "deny",
      "  a different question right after a redirect is redirected too")
code, out = redirect("rd-1", {**ASK, "ask": "另一个新问题？"}, "toolu_r2")
check(code == 0 and out is None and RB.redirect_verdict("toolu_r2") == "allow",
      "  the same question again within the window -> let through (no loop)")
for name, tool_input in (("not blocked", {**ASK, "blocked": False}), ("blocked, no ask", {**ASK, "ask": "  "}),
                         ("blocked as a string", {**ASK, "blocked": "true"})):
    tid = "toolu_" + name.replace(" ", "_").replace(",", "")
    code, out = redirect("rd-2", tool_input, tid)
    check(code == 0 and out is None and not RB.redirected_recently("rd-2", ASK["ask"], 5)
          and RB.redirect_verdict(tid) == ("allow" if tool_input["blocked"] is True else None),
          f"  {name} -> let through ('allow' only for blocked calls, the only ones the decision hook reads)")
code, out = redirect("rd-3", ASK, None)
check(code == 0 and out is None and not RB.redirected_recently("rd-3", ASK["ask"], 5), "  no tool_use_id -> never denied")
code, out = redirect("rd-3", ASK, "../evil")
check(code == 0 and out is None and not RB.redirected_recently("rd-3", ASK["ask"], 5), "  unsafe tool_use_id -> never denied")
code, out = redirect("rd-3", ASK, raw=b"{not json")
check(code == 0 and out is None, "  broken payload -> exit 0, no output")
with open(os.path.join(RB.REPLY_DIR, "session-rd-4.redirect.json"), "w", encoding="utf-8") as f:
    json.dump({"at": time.time() - 600, "ask": ASK["ask"]}, f, ensure_ascii=False)
code, out = redirect("rd-4", ASK, "toolu_r4")
check(out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny",
      "  the same question long after a redirect is redirected again")
os.makedirs(os.path.join(RB.REPLY_DIR, "session-rd-6.redirect.json"))  # cannot be written
code, out = redirect("rd-6", ASK, "toolu_r6")
check(code == 0 and out is None and RB.redirect_verdict("toolu_r6") is None,
      "  session record fails -> not denied, no verdict (decision hook sends its card)")
os.makedirs(os.path.join(RB.REPLY_DIR, "call-toolu_r7.redirect"))  # verdict cannot be written
code, out = redirect("rd-7", ASK, "toolu_r7")
check(code == 0 and out is not None and RB.redirect_verdict("toolu_r7") is None,
      "  verdict fails after the denial -> denied, no verdict: worst case is one extra card")

print("== decision hook honours the redirect verdict")
before = len(calls())
t0 = time.time()
p = start_waiter("rd-1", ASK, extra={"tool_use_id": "toolu_r1"})
code = p.wait(15)
check(code == 0 and len(calls()) == before and time.time() - t0 < 6,
      f"  verdict 'deny' -> no card, exits at once with 0 (stderr is only a log line then): {code}")
p = start_waiter("rd-1", ASK, extra={"tool_use_id": "toolu_r2"})
rid = wait_rid("rd-1")
check(rid is not None and calls()[-1]["kind"] == "send_card", "  verdict 'allow' -> old card sent")
r = click("ou_me", {"rid": rid, "kind": "choice", "index": 0})
try:
    code = p.wait(15)
except subprocess.TimeoutExpired:
    code = None
    p.kill()
check(code == 2, f"  ... and its answer still wakes the model (exit {code}, click {r})")
t0 = time.time()
p = start_waiter("rd-5", ASK, extra={"tool_use_id": "toolu_never_marked"})
rid = wait_rid("rd-5", timeout=25)
waited = time.time() - t0
check(rid is not None and waited >= 11, f"  no verdict -> card sent after the wait ({waited:.1f} s)")
RB.close_request(rid, "superseded")
check(p.wait(15) == 0, "  waiter closed")

print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S)")
sys.exit(1 if FAILS else 0)
