"""Offline tests for controlling a running session from Feishu: stop a turn, add a
command to a busy turn (claude_turn_control.py, the PreToolUse hook), keep taking
commands after a stop (claude_stop_wait.py, its asyncRewake partner), stop while
an approval card waits, and the reply_bridge records behind "reply to a message"
and "current session". System python, no lark-oapi; nothing is sent to Feishu.
"""
import json
import os
import shutil
import subprocess
import sys
import time

from _testenv import isolate

WORK = isolate("feishu_control_")

import claude_permission as P  # noqa: E402
import reply_bridge as RB  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
CONTROL = os.path.join(SCRIPTS, "claude_turn_control.py")
STOP_WAIT = os.path.join(SCRIPTS, "claude_stop_wait.py")
SENT = os.path.join(WORK, "sent.jsonl")
RUNNER = r'''
import json, runpy, sys
script, sent = sys.argv[1:3]
sys.argv = [script]
sys.path.insert(0, __import__("os").path.dirname(script))
import feishu_notify
def rec(title, body):
    with open(sent, "a", encoding="utf-8") as f:
        f.write(json.dumps({"title": title, "body": body}, ensure_ascii=False) + "\n")
    return "om_receipt"
feishu_notify.send_reply_text = rec
runpy.run_path(script, run_name="__main__")
'''


def pre_tool(session, tool_use_id="toolu_a1", agent_id=None, script=CONTROL, env=None):
    payload = {"hook_event_name": "PreToolUse", "session_id": session, "tool_use_id": tool_use_id,
               "tool_name": "Bash", "tool_input": {"command": "echo hi"}, "cwd": r"E:\P", "transcript_path": ""}
    if agent_id:
        payload["agent_id"] = agent_id
    return subprocess.Popen([sys.executable, "-c", RUNNER, script, SENT], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def control(session, tool_use_id="toolu_a1", agent_id=None, env=None):
    p = pre_tool(session, tool_use_id, agent_id, CONTROL, env)
    payload = {"hook_event_name": "PreToolUse", "session_id": session, "tool_use_id": tool_use_id,
               "tool_name": "Bash", "tool_input": {"command": "echo hi"}, "cwd": r"E:\P", "transcript_path": ""}
    if agent_id:
        payload["agent_id"] = agent_id
    t0 = time.time()
    out, err = p.communicate(json.dumps(payload).encode("utf-8"), timeout=30)
    return p.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace"), time.time() - t0


def sent():
    return [json.loads(line) for line in open(SENT, encoding="utf-8")] if os.path.exists(SENT) else []


print("== stop flags in reply_bridge")
RB.request_stop("s-1")
check(RB.stop_requested("s-1") and not RB.stop_requested("s-2"), "  a stop request is per session")
check(RB.claim_stop("s-1", "toolu_x") and not RB.stop_requested("s-1") and RB.was_stopped("s-1", "toolu_x"),
      "  claiming moves the request to the tool call that stops")
check(not RB.claim_stop("s-1", "toolu_y"), "  only one tool call can claim it")
RB.take_stopped("s-1", "toolu_x")
check(not RB.was_stopped("s-1", "toolu_x"), "  the stopped marker is taken once")
RB.request_stop("s-1")
RB.clear_stop("s-1")
RB.clear_stop("s-1")
check(not RB.stop_requested("s-1"), "  cleared (twice is fine)")
check(not RB.stop_requested("../x") and not RB.claim_stop("s-1", "../x") and not RB.was_stopped("../x", "t"),
      "  unsafe ids never match a file")
RB.request_stop("s-old")
RB._path("stop-s-old.flag").write_text(repr(time.time() - RB.STOP_MAX_AGE_SECONDS - 5), encoding="utf-8")
check(not RB.claim_stop("s-old", "toolu_late") and not RB.stop_requested("s-old"),
      "  a stop request older than its limit is dropped, not claimed")
try:
    RB.request_stop("../x")
    check(False, "  unsafe id refused")
except ValueError:
    check(True, "  unsafe id refused")

print("== the PreToolUse hook (claude_turn_control.py)")
code, out, err, took = control("c-1")
check(code == 0 and out == "" and took < 5, f"  nothing for this session -> silent, exit 0 ({took:.2f} s)")
RB.request_stop("c-1")
code, out, err, took = control("c-1", "toolu_stop1")
body = json.loads(out) if out.startswith("{") else {}
spec = body.get("hookSpecificOutput") or {}
check(code == 0 and body.get("continue") is False and spec.get("permissionDecision") == "deny",
      f"  a stop request -> continue false and the call denied: {out[:120]!r}")
check("飞书" in body.get("stopReason", "") and "只停这一轮" in body.get("stopReason", ""),
      f"  the reason says it was Feishu and only this turn: {body.get('stopReason')!r}")
check(RB.was_stopped("c-1", "toolu_stop1") and not RB.stop_requested("c-1"), "  the request is claimed by this call")
RB.take_stopped("c-1", "toolu_stop1")  # the stop's async hook took over
code, out, _, _ = control("c-1", "toolu_batchmate")
body = json.loads(out) if out.startswith("{") else {}
check(body.get("continue") is False and (body.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny",
      "  another call of the same message (a parallel batch) is denied too (measured: it ran otherwise)")
RB.mark_prompt("c-1")  # the next turn starts (typed, or woken by a Feishu command)
code, out, _, _ = control("c-1", "toolu_next")
check(out == "" and not RB._path("session-c-1.stopped_at").exists(),
      "  after the next prompt, calls run again (and the record is removed, so they start lean)")

RB.queue_command("c-2", "顺便把 README 也改了", "ou_me")
RB.queue_command("c-2", "第二条", "ou_me")
code, out, err, _ = control("c-2")
spec = (json.loads(out) if out.startswith("{") else {}).get("hookSpecificOutput") or {}
context = spec.get("additionalContext", "")
check(code == 0 and spec.get("hookEventName") == "PreToolUse" and context.startswith("【飞书指令】"),
      f"  a command queued while busy -> added to this turn: {context[:40]!r}")
check("这一轮进行中" in context and context.index("顺便把 README 也改了") < context.index("第二条"),
      "  ... says it arrived mid-turn, both commands in order")
check("permissionDecision" not in spec and "continue" not in json.loads(out), "  ... and the tool call goes ahead")
check(RB.queued_commands("c-2") == 0 and RB.take_relay("c-2") is not None,
      "  the commands left the queue; the turn's last reply will be reported")

RB.request_stop("c-3")
RB.queue_command("c-3", "停下之后再做这个", "ou_me")
code, out, _, _ = control("c-3", "toolu_both")
check(json.loads(out).get("continue") is False and RB.queued_commands("c-3") == 1,
      "  stop and a command together -> stop; the command waits for the next waiter")
RB.take_stopped("c-3", "toolu_both")
RB.take_commands("c-3")

RB.request_stop("c-7")
RB.queue_command("c-7", "批里另一个调用不该拿走我", "ou_me")
RB.claim_stop("c-7", "toolu_first")  # call A of a parallel batch stopped the turn
code, out, _, _ = control("c-7", "toolu_second")
check(out == "" and RB.queued_commands("c-7") == 1,
      "  another call of the stopped batch does not take the commands: they wait for the stop's waiter")
RB.take_stopped("c-7", "toolu_first")
RB.take_commands("c-7")

RB.request_stop("c-8")
code, out, _, _ = control("c-8", "")
check(json.loads(out or "{}").get("continue") is False and RB.was_stopped("c-8", RB.NO_TOOL_USE_ID),
      "  no tool_use_id in the payload -> still stopped, under a fixed id both hooks use")
RB.take_stopped("c-8", RB.NO_TOOL_USE_ID)
sys.path.insert(0, SCRIPTS)
import claude_stop_wait  # noqa: E402
check(claude_stop_wait.NO_TOOL_USE_ID == RB.NO_TOOL_USE_ID, "  ... the async hook uses the same fixed id")

RB.request_stop("c-4")
RB.queue_command("c-4", "给主线程的", "ou_me")
code, out, _, _ = control("c-4", "toolu_sub", agent_id="agent-7")
check(out == "" and RB.stop_requested("c-4") and RB.queued_commands("c-4") == 1,
      "  a subagent's tool call is left alone: stop and commands wait for the session's own call")
RB.clear_stop("c-4")
RB.take_commands("c-4")

RB.request_stop("c-5")
code, out, _, _ = control("c-5", "../bad")
check(out == "" and RB.stop_requested("c-5"), "  an unsafe tool_use_id -> nothing claimed")
RB.clear_stop("c-5")
RB.request_stop("c-9")
RB._path("stop-c-9.flag").write_text(repr(time.time() - RB.STOP_MAX_AGE_SECONDS - 5), encoding="utf-8")
code, out, _, _ = control("c-9", "toolu_stale")
check(out == "" and not RB.stop_requested("c-9"), "  a stale stop request is dropped by the hook, the call goes ahead")
code, out, _, _ = control("../x")
check(code == 0 and out == "", "  an unsafe session id -> silent")

unready = dict(os.environ, FEISHU_REMOTE_HOME=os.path.join(WORK, "no-setup"))
RB.request_stop("c-6")
code, out, _, _ = control("c-6", "toolu_u", env=unready)
check(out == "" and RB.stop_requested("c-6"), "  setup not run (no config) -> does nothing at all")
RB.clear_stop("c-6")

print("== after a stop, the same call keeps taking commands (claude_stop_wait.py)")


def stop_wait(session, tool_use_id):
    p = pre_tool(session, tool_use_id, script=STOP_WAIT)
    payload = {"hook_event_name": "PreToolUse", "session_id": session, "tool_use_id": tool_use_id,
               "tool_name": "Bash", "tool_input": {}, "cwd": r"E:\P", "transcript_path": ""}
    p.stdin.write(json.dumps(payload).encode("utf-8"))
    p.stdin.close()
    return p


def finish(p, timeout=20):
    try:
        code = p.wait(timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        return None, ""
    return code, p.stderr.read().decode("utf-8", "replace")


t0 = time.time()
code, err = finish(stop_wait("w-1", "toolu_plain"))
check(code == 0 and time.time() - t0 < 5, f"  an ordinary tool call -> ends at once ({time.time() - t0:.2f} s)")

RB.request_stop("w-2")
p = stop_wait("w-2", "toolu_s2")
time.sleep(1.0)  # the sync hook claims the stop while this one is starting
check(RB.claim_stop("w-2", "toolu_s2"), "  (the sync hook claims the stop)")
end = time.time() + 15
while time.time() < end and (RB.command_waiter("w-2") or {}).get("waiter_pid") != p.pid:
    time.sleep(0.2)
check((RB.command_waiter("w-2") or {}).get("waiter_pid") == p.pid and not RB.was_stopped("w-2", "toolu_s2"),
      "  the stopped call's async hook registers as the session's command waiter")
check(any("已停下" in s["title"] for s in sent()), f"  the user is told the turn stopped: {sent()}")
RB.queue_command("w-2", "换个方向：先写测试", "ou_me")
code, err = finish(p)
check(code == 2 and err.startswith("【飞书指令】") and err.endswith("换个方向：先写测试"),
      f"  a command after the stop wakes the session: exit {code}, {err[:20]!r}")
check(RB.take_relay("w-2") is not None, "  ... and its reply will be reported")

RB.request_stop("w-4")
p = stop_wait("w-4", "toolu_slow")
time.sleep(8)  # the sync hook is slow to claim (antivirus, a busy disk)
check(p.poll() is None, "  while the stop request is unclaimed, the async hook keeps waiting past 5 s")
RB.claim_stop("w-4", "toolu_slow")
end = time.time() + 15
while time.time() < end and (RB.command_waiter("w-4") or {}).get("waiter_pid") != p.pid:
    time.sleep(0.2)
check((RB.command_waiter("w-4") or {}).get("waiter_pid") == p.pid, "  ... and still takes over once it is claimed")
p.kill()

RB.request_stop("w-3")
p = stop_wait("w-3", "toolu_other")
time.sleep(1.0)
RB.claim_stop("w-3", "toolu_claimer")  # a different call of the batch claims it
t0 = time.time()
code, err = finish(p)
check(code == 0 and time.time() - t0 < 5 and RB.command_waiter("w-3") is None,
      "  another call claimed the stop -> this one ends soon, without waiting for commands")
RB.take_stopped("w-3", "toolu_claimer")

print("== stop while an approval card waits (claude_permission.decision_output)")
out = json.loads(P.decision_output({"choice": "stop"}))
decision = out["hookSpecificOutput"]["decision"]
check(decision["behavior"] == "deny" and "interrupt" not in decision and "叫停" in decision["message"],
      f"  'stop' -> deny without interrupt (an interrupt fires no Stop hook): {decision}")
check("结束这一轮" in decision["message"], "  ... telling Claude to end the turn")

print("== the Stop and UserPromptSubmit hooks clear a stop that was never reached")
RB.request_stop("u-1")
p = subprocess.run([sys.executable, os.path.join(SCRIPTS, "claude_cancel_pending.py")],
                   input=json.dumps({"session_id": "u-1"}).encode(), capture_output=True, timeout=30)
check(p.returncode == 0 and p.stdout == b"" and not RB.stop_requested("u-1"), "  a new prompt clears it")

print("== which session a Feishu message belongs to")
RB.remember_message("om_abc", "m-1")
check(RB.session_of_message("om_abc") == "m-1" and RB.session_of_message("om_none") is None,
      "  a message sent about a session maps back to it")
check(RB.session_of_message("../x") is None, "  unsafe message id -> nothing")
RB.remember_message("../x", "m-1")
RB.remember_message("om_ok", "../x")
check(RB.session_of_message("om_ok") is None, "  unsafe ids are not recorded")

print("== the current session (plain chat text goes to it for 30 minutes)")
check(RB.current_session() is None, "  none at first")
RB.set_current("k-1")
check(RB.current_session() == "k-1", "  set")
data = json.loads(RB._path("current.json").read_text(encoding="utf-8"))
data["at"] = time.time() - RB.CURRENT_SESSION_SECONDS - 5
RB._path("current.json").write_text(json.dumps(data), encoding="utf-8")
check(RB.current_session() is None, "  unused for 30 minutes -> gone")
RB.set_current("k-1")
data["at"] = time.time() - RB.CURRENT_SESSION_SECONDS + 60
data["sid"] = "k-1"
RB._path("current.json").write_text(json.dumps(data), encoding="utf-8")
RB.touch_current()
check(RB.current_session() == "k-1" and time.time() - json.loads(RB._path("current.json").read_text())["at"] < 5,
      "  using it restarts the 30 minutes")
RB.set_current(None)
check(RB.current_session() is None, "  cleared")
RB._path("current.json").write_text('{"sid": "../x", "at": 9e99}', encoding="utf-8")
check(RB.current_session() is None, "  a tampered file with an unsafe id -> none")

shutil.rmtree(WORK, ignore_errors=True)
print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S)")
sys.exit(1 if FAILS else 0)
