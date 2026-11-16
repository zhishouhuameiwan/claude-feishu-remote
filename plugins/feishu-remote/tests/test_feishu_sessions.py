"""Offline tests for the "会话" command (session_list.py) and the command waiter (claude_command_wait.py).
System python, no lark-oapi.

`claude agents --json` is replaced by a fake claude.bat that prints a prepared
file, so the real subprocess path runs; transcripts live in a temp CLAUDE_CONFIG_DIR.
Nothing is sent to Feishu.
"""
import json
import os
import shutil
import sys
import time

from _testenv import isolate

WORK = isolate("feishu_sessions_")
CLAUDE_HOME = os.path.join(WORK, "claude-home")
os.environ["CLAUDE_CONFIG_DIR"] = CLAUDE_HOME

import reply_bridge as RB  # noqa: E402
import session_list as S  # noqa: E402

FAILS = []


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


FAKE_BIN = os.path.join(WORK, "bin")
os.makedirs(FAKE_BIN)
AGENTS_JSON = os.path.join(FAKE_BIN, "agents.json")


def fake_claude(script):
    path = os.path.join(FAKE_BIN, "claude.bat")
    with open(path, "w", encoding="ascii") as f:
        f.write("@echo off\r\n" + script + "\r\n")
    S.shutil.which = lambda name: path if name == "claude" else None


def agents(entries):
    with open(AGENTS_JSON, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    fake_claude(f'type "{AGENTS_JSON}"')


def transcript(session_id, lines, project="C--proj", age=0.0):
    folder = os.path.join(CLAUDE_HOME, "projects", project)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{session_id}.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(json.dumps(line, ensure_ascii=False) for line in lines) + "\n")
    when = time.time() - age
    os.utime(path, (when, when))
    return path


def reply(model):
    return {"type": "assistant", "message": {"model": model, "content": [{"type": "text", "text": "hi"}]}}


def card_texts(card):
    return [e["text"]["content"] for e in card["body"]["elements"] if e.get("tag") == "div"]


def entries_of(card):
    return card_texts(card)[1:]


def open_card(session_id, kind=None, alive=True, closed=False, title="", cwd=""):
    rid = RB.new_id()
    pid = os.getpid() if alive else 999999  # 999999: no such process
    data = {"session_id": session_id, "info": {"title": title, "cwd": cwd}, "message_id": "om_x",
            "waiter_pid": pid, "waiter_created": RB.process_created(pid) if alive else 1}
    if kind:
        data["type"] = kind
    RB.create_request(rid, data, update_session=False)
    if closed:
        RB.close_request(rid, "answered_on_computer")
    return rid


print("== claude agents --json is run and read")
agents([{"pid": 1, "sessionId": "s-busy", "name": "会话甲 busy", "cwd": r"E:\Work\A", "kind": "interactive",
         "status": "busy", "startedAt": 1}])
rows = S.run_agents()
check(len(rows) == 1 and rows[0]["name"] == "会话甲 busy", "  UTF-8 output parsed, Chinese names intact")
fake_claude("echo boom 1>&2\r\nexit /b 3")
try:
    S.run_agents()
    check(False, "  non-zero exit raises")
except S.SessionListError as exc:
    check("退出码 3" in str(exc) and "boom" in str(exc), f"  non-zero exit -> error with stderr: {exc}")
fake_claude("echo hello")
try:
    S.run_agents()
    check(False, "  non-JSON output raises")
except S.SessionListError as exc:
    check("不是 JSON" in str(exc), "  non-JSON output -> error")
fake_claude('echo {"a": 1}')
try:
    S.run_agents()
    check(False, "  JSON that is not a list raises")
except S.SessionListError as exc:
    check("不是会话列表" in str(exc), "  JSON object instead of a list -> error")
S.shutil.which = lambda name: None
try:
    S.run_agents()
    check(False, "  missing claude raises")
except S.SessionListError as exc:
    check("找不到 claude" in str(exc), "  claude not on PATH -> error naming it")
saved_timeout = S.AGENTS_TIMEOUT_SECONDS
S.AGENTS_TIMEOUT_SECONDS = 1
# Like an npm install's claude.cmd: cmd.exe with a long-running child holding the pipes.
fake_claude("ping -n 30 127.0.0.1 >nul")
t0 = time.time()
try:
    S.run_agents()
    check(False, "  slow claude raises")
except S.SessionListError as exc:
    check("没有返回" in str(exc) and time.time() - t0 < 6,
          f"  timeout -> error at once, child processes killed too ({time.time() - t0:.1f} s)")
S.AGENTS_TIMEOUT_SECONDS = saved_timeout

print("== model and last activity come from the transcript tail")
transcript("s-busy", [reply("claude-opus-5-5"), reply("claude-sonnet-5-5"),
                      {"type": "assistant", "message": {"model": "<synthetic>"}}, {"type": "summary"}], age=300)
model, active = S._transcript_facts("s-busy")
check(model == "claude-sonnet-5-5", f"  newest real model, synthetic replies skipped: {model}")
check(active is not None and 280 < time.time() - active < 400, "  last activity = transcript time")
check(S._transcript_facts("s-none") == ("", None), "  no transcript -> unknown")
for bad in ("../x", "*", "s?busy", ""):
    check(S.transcript_of(bad) is None, f"  unsafe session id {bad!r} is not globbed")
big = transcript("s-big", [reply("claude-haiku-5-5")] + [{"type": "user", "pad": "x" * 2000}] * 400)
check(os.path.getsize(big) > S.TAIL_BYTES and S._transcript_facts("s-big")[0] == "",
      "  model older than the tail -> unknown, file not read whole")

print("== rows: open Feishu cards, command waiters, sorting, unlisted sessions")
UUID = "8ebe85ee-65bf-4fc2-8f9e-fcf2a3824503"  # real session ids look like this
agents([
    {"sessionId": "s-idle-old", "name": "旧的空闲", "cwd": r"E:\A", "kind": "interactive", "status": "idle", "pid": 11},
    {"sessionId": "s-idle-new", "name": "新的空闲", "cwd": r"E:\A", "kind": "interactive", "status": "idle", "pid": 12},
    {"sessionId": "s-ready", "name": "可发指令", "cwd": r"E:\A", "kind": "interactive", "status": "idle", "pid": 13},
    {"sessionId": "s-busy", "name": "干活中", "cwd": r"E:\B", "kind": "interactive", "status": "busy", "pid": 14},
    {"sessionId": UUID, "name": "等审批", "cwd": r"E:\C", "kind": "interactive", "status": "waiting",
     "waitingFor": "permission prompt", "pid": 15},
    {"sessionId": "s-card", "name": "有卡片", "cwd": r"E:\D", "kind": "interactive", "status": "idle", "pid": 16},
    {"sessionId": "s-bg", "name": "后台任务", "cwd": r"E:\E", "kind": "background", "state": "working"},
])
transcript("s-idle-old", [reply("claude-opus-5-5")], age=3 * 86400)
transcript("s-idle-new", [reply("claude-opus-5-5")], age=7200)
transcript("s-ready", [reply("claude-opus-5-5")], age=600)
transcript(UUID, [reply("claude-opus-5-5")], project="E--C", age=30)
open_card("s-card", "permission")
open_card("s-card")  # a "next step" card has no type
open_card("s-card", "ask", closed=True)  # answered: not open any more
open_card("s-card", "ask", alive=False)  # its hook is gone: not open any more
open_card("s-gone", "ask", title="终端里的会话", cwd=r"F:\Term")
RB.register_command_waiter("s-ready", {"waiter_pid": os.getpid(), "waiter_created": RB.process_created(os.getpid())})
RB.register_command_waiter("s-idle-new", {"waiter_pid": 999999, "waiter_created": 1})  # that waiter has ended
RB.queue_command("s-busy", "跑完测试告诉我结果", "ou_me")
rows = S.collect()
names = [r["name"] for r in rows]
check(names[:3] == ["等审批", "有卡片", "终端里的会话"],
      f"  waiting sessions and sessions with open cards first, known activity first: {names[:3]}")
check(names[3:5] == ["干活中", "后台任务"], f"  then working (background state 'working' too): {names[3:5]}")
check(names[5:] == ["可发指令", "新的空闲", "旧的空闲"], f"  then idle, most recent first: {names[5:]}")
by_name = {r["name"]: r for r in rows}
check(by_name["可发指令"]["commandable"] and not by_name["新的空闲"]["commandable"],
      "  a running command waiter makes an idle session commandable; an ended one does not")
check([r["name"] for r in rows if S.is_active(r)] == names[:6], "  active: waiting, cards, working, commandable")
card_row = by_name["有卡片"]
check(len(card_row["cards"]) == 2, f"  only cards still waiting count: {len(card_row['cards'])}")
now = time.time()
text = S.format_row(card_row, now)
check("飞书待答卡片：审批 1 张、决策 1 张" in text, f"  card kinds named: {text.splitlines()[-1]}")
text = S.format_row(by_name["等审批"], now)
check(text.startswith("【等你处理·审批】等审批") and "模型：claude-opus-5-5" in text and "最近活动：刚刚" in text,
      f"  waiting row: {text!r}")
text = S.format_row(by_name["后台任务"], now)
check(text.startswith("【后台·工作中】") and "模型：未知" in text and "最近活动：未知" in text, f"  background row: {text!r}")
text = S.format_row(by_name["终端里的会话"], now)
check("claude agents 没有列出" in text and r"F:\Term" in text, f"  unlisted session with an open card: {text!r}")
check(S.format_row(by_name["可发指令"], now).startswith("【空闲·可发指令】"), "  commandable idle row")
check("排队中的指令 1 条" in S.format_row(by_name["干活中"], now), "  queued commands counted")
check("3 天前" in S.format_row(by_name["旧的空闲"], now), "  days-old activity")
check("2 小时前" in S.format_row(by_name["新的空闲"], now), "  hours-old activity")

print("== card: active sessions only, a send box for each live one")
card = S.sessions_card()
texts = card_texts(card)
check(texts[0].startswith("活跃会话 6 个：等你处理 3，工作中 2，空闲可发指令 1。"), f"  summary: {texts[0]}")
check(len(texts) == 8 and texts[1].startswith("【等你处理") and "另有 2 个空闲会话没有列出" in texts[-1],
      f"  6 active sessions, the 2 idle ones without a waiter only counted: {texts[-1]}")
forms = [e for e in card["body"]["elements"] if e.get("tag") == "form"]
values = [f["elements"][1]["behaviors"][0]["value"] for f in forms]
check(len(forms) == 5 and "s-gone" not in [v["sid"] for v in values],
      "  a send box per listed session; none for an unlisted one without a waiter")
check(values[0] == {"kind": "send", "field": "cmd_text_0", "sid": UUID, "pid": 15}
      and forms[0]["elements"][0]["name"] == "cmd_text_0", f"  the box posts session id, pid and its field: {values[0]}")
check(len({f["name"] for f in forms}) == len(forms) and len({v["field"] for v in values}) == len(forms),
      "  form and field names unique in the card")
button = card["body"]["elements"][-1]
check(button["tag"] == "button" and button["behaviors"][0]["value"] == {"kind": "sessions"}, "  refresh button")
check(card["config"]["update_multi"] is True, "  card can be updated in place (refresh)")
agents([{"sessionId": "s-idle-old", "name": "旧的空闲", "cwd": r"E:\A", "kind": "interactive", "status": "idle"}])
for path in RB.REPLY_DIR.glob("*.pending.json"):
    path.unlink()
card = S.sessions_card()
check(card_texts(card)[1] == "现在没有活跃的会话。" and "另有 1 个空闲会话" in card_texts(card)[-1],
      "  nothing active -> says so")
fake_claude("exit /b 1")
card = S.sessions_card()
check(card_texts(card)[0].startswith("读取会话列表失败：") and len(card_texts(card)) == 1, "  failure -> error card")

print("== many sessions, long names: capped, under Feishu's 30 KB")
many = [{"sessionId": f"s-{n}", "name": "很长的会话名" * 40, "cwd": "D:\\" + "深" * 300, "kind": "interactive",
         "status": "busy", "pid": 1000 + n} for n in range(40)]
agents(many)
card = S.sessions_card()
size = len(json.dumps(json.dumps(card, ensure_ascii=False), ensure_ascii=False).encode("utf-8"))
check(len(card_texts(card)) == 1 + S.MAX_ENTRIES + 1, f"  {S.MAX_ENTRIES} sessions listed, then the footer")
check(f"还有 {40 - S.MAX_ENTRIES} 个活跃会话没有列出" in card_texts(card)[-1],
      f"  the rest counted: {card_texts(card)[-1]}")
check(size < 30000, f"  request size {size} bytes")
# Every field at its cap, through build_card directly (claude agents cannot send all of these).
worst = {"name": "很" * 300, "cwd": "深" * 300, "kind": "background", "status": "状" * 100, "waiting_for": "等" * 100,
         "pid": 123456, "model": "模" * 100, "active": time.time(), "cards": [{"type": "permission"}, {"type": "ask"}, {}],
         "listed": True, "commandable": True, "queued": 99}
card = S.build_card([dict(worst, session_id=f"s-{n:036d}") for n in range(40)], time.time())
size = len(json.dumps(json.dumps(card, ensure_ascii=False), ensure_ascii=False).encode("utf-8"))
check(size < 28000, f"  every field at its cap: request size {size} bytes, under 30 KB with room to spare")

print("== command queue")
RB.queue_command("abc", "一", "ou_me")
RB.queue_command("abc", "二", "ou_me")
RB.queue_command("abc-def", "别人的", "ou_me")
check(RB.queued_commands("abc") == 2, "  queued per session; a session id that is a prefix of another stays apart")
check([c["text"] for c in RB.take_commands("abc")] == ["一", "二"], "  taken oldest first")
check(RB.take_commands("abc") == [] and RB.queued_commands("abc-def") == 1, "  taken once; the other session's kept")
for bad in ("../x", "a.b", "*"):
    try:
        RB.queue_command(bad, "x", "ou_me")
        check(False, f"  unsafe session id {bad!r} refused")
    except ValueError:
        check(True, f"  unsafe session id {bad!r} refused")
RB.queue_command("old", "上周的指令", "ou_me")
RB.queue_command("old", "刚发的指令", "ou_me")
first = sorted(RB.REPLY_DIR.glob("cmd-old.*.json"))[0]
RB._write_json_atomic(first, {"text": "上周的指令", "at": time.time() - RB.COMMAND_MAX_AGE_SECONDS - 60})
check([c["text"] for c in RB.take_commands("old")] == ["刚发的指令"] and RB.queued_commands("old") == 0,
      "  a command older than 12 hours is dropped, not delivered")

print("== command waiter (claude_command_wait.py, the Stop hook)")
import subprocess  # noqa: E402

WAITER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts",
                      "claude_command_wait.py")
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
feishu_notify.send_reply_text = rec
def card(kind):
    def send(c):
        texts = [e["text"]["content"] for e in c["body"]["elements"] if e.get("tag") == "div"]
        with open(sent, "a", encoding="utf-8") as f:
            f.write(json.dumps({"title": c["header"]["title"]["content"], "body": "\n".join(texts), "kind": kind,
                                "buttons": [e["behaviors"][0]["value"] for e in c["body"]["elements"]
                                            if e.get("tag") == "button"]}, ensure_ascii=False) + "\n")
        return "om_x"
    return send
feishu_notify.send_reply_card = card("reply")
feishu_notify.send_feishu_card = card("notice")
runpy.run_path(script, run_name="__main__")
'''


def sent():
    return [json.loads(line) for line in open(SENT, encoding="utf-8")] if os.path.exists(SENT) else []


def start_waiter(session, last="", transcript_path=""):
    payload = {"hook_event_name": "Stop", "session_id": session, "cwd": r"E:\Work\P", "transcript_path": transcript_path,
               "last_assistant_message": last, "stop_hook_active": False}
    p = subprocess.Popen([sys.executable, "-c", RUNNER, WAITER, SENT], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    p.stdin.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    p.stdin.close()
    return p


def waiting_for(session, p, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if (RB.command_waiter(session) or {}).get("waiter_pid") == p.pid and RB.command_waiter_alive(session):
            return True
        time.sleep(0.2)
    return False


def finish(p, timeout=15):
    try:
        code = p.wait(timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        return None, ""
    return code, p.stderr.read().decode("utf-8", "replace")


p = start_waiter("w-1")
check(waiting_for("w-1", p), "  waits after the turn, registered as the session's waiter")
check(not sent(), "  a turn not started from Feishu: nothing relayed")
RB.queue_command("w-1", "帮我看下测试结果\n第二行 “引号”", "ou_me")
t0 = time.time()
code, err = finish(p)
check(code == 2 and time.time() - t0 < 6, f"  a queued command wakes Claude: exit {code} after {time.time() - t0:.1f} s")
check(err.startswith("【飞书指令】") and err.endswith("指令原文（到本消息结尾为止）：\n帮我看下测试结果\n第二行 “引号”"),
      f"  stderr is the command alone, the user's words last: {err[:40]!r} … {err[-30:]!r}")
check("飞书远程回复规则" in err and "AskUserQuestion" in err, "  ... and points at the injected rule")
check(RB.queued_commands("w-1") == 0, "  the command left the queue")
p = start_waiter("w-1", last="测试全部通过，共 12 项。")
check(waiting_for("w-1", p), "  the woken turn's Stop starts the next waiter")
relayed = sent()
check(len(relayed) == 1 and "测试全部通过，共 12 项。" in relayed[0]["body"] and r"E:\Work\P" in relayed[0]["body"]
      and relayed[0]["kind"] == "reply" and relayed[0]["title"].endswith("飞书指令处理完了"),
      f"  ... after relaying that turn's last reply to Feishu as a reply card: {relayed}")
RB.mark_prompt("w-1")
code, err = finish(p)
check(code == 0 and err == "", f"  typed on the computer -> ends quietly (exit {code}, stderr {err!r})")
p = start_waiter("w-1", last="这一轮是电脑上发起的")
check(waiting_for("w-1", p) and len(sent()) == 1, "  a turn started on the computer: its reply is not relayed")
q = start_waiter("w-1")
check(waiting_for("w-1", q), "  a newer waiter of the session takes over")
RB.queue_command("w-1", "给新等待者的指令", "ou_me")
code, err = finish(p)
check(code == 0, f"  ... the older one ends without taking the command (exit {code})")
code, err = finish(q)
check(code == 2 and err.endswith("给新等待者的指令"), f"  ... the newer one delivers it (exit {code})")
RB._write_text_atomic(RB._path("session-w-1.relay"), repr(time.time() - 100))  # woken 100 s ago, interrupted,
RB._write_text_atomic(RB._path("session-w-1.prompt_at"), repr(time.time() - 50))  # then typed on the computer
p = start_waiter("w-1", last="被 Esc 打断后电脑上那一轮的回复")
check(waiting_for("w-1", p) and len(sent()) == 1,
      "  woken turn interrupted (no Stop), then the user typed on the computer: next reply not relayed")
RB.mark_prompt("w-1")
finish(p)
RB.mark_relay("w-1")
RB._write_text_atomic(RB._path("session-w-1.relay"), repr(time.time() - 100))  # woken 100 s ago,
RB._write_text_atomic(RB._path("session-w-1.prompt_at"), repr(time.time() - 98))  # a prompt 2 s after the wake
p = start_waiter("w-1", last="唤醒后这一轮的回复")
check(waiting_for("w-1", p) and len(sent()) == 2 and "唤醒后这一轮的回复" in sent()[-1]["body"],
      "  a prompt right at the wake (the wake itself) does not stop the relay")
RB.mark_prompt("w-1")
finish(p)
RB.queue_command("w-2", "第一件事", "ou_me")
RB.queue_command("w-2", "第二件事", "ou_me")
p = start_waiter("w-2")
code, err = finish(p)
check(code == 2 and err.endswith("第 1 条：\n第一件事\n第 2 条：\n第二件事"),
      f"  commands queued while busy are delivered together, in order: {err[-30:]!r}")
p = start_waiter("../x")
code, err = finish(p)
check(code == 0 and RB.command_waiter("../x") is None, "  unsafe session id -> nothing registered, exit 0")
parent = subprocess.Popen(
    [sys.executable, "-c", "import subprocess, sys; p = subprocess.Popen(sys.argv[1:], stdin=subprocess.PIPE); "
     "p.stdin.write(b'{\"session_id\": \"w-3\"}'); p.stdin.close()", sys.executable, "-c", RUNNER, WAITER, SENT])
parent.wait(15)
end = time.time() + 20
while time.time() < end and not RB.command_waiter("w-3"):
    time.sleep(0.2)
registered = RB.command_waiter("w-3") is not None
while time.time() < end and RB.command_waiter_alive("w-3"):
    time.sleep(0.2)
check(registered and not RB.command_waiter_alive("w-3"), "  Claude Code gone -> the waiter ends")

print("== turn end notice (long turns, followed sessions)")
import feishu_notify  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def turn_transcript(session, minutes_ago):
    started = datetime.fromtimestamp(time.time() - minutes_ago * 60, timezone.utc).isoformat().replace("+00:00", "Z")
    return transcript(session, [{"type": "user", "timestamp": started, "promptId": "pp", "origin": {"kind": "human"},
                                 "message": {"content": "做一件很久的事"}},
                                {"type": "assistant", "timestamp": started, "promptId": "pp",
                                 "message": {"model": "m", "content": [{"type": "text", "text": "好"}]}}])


def iso_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_turn_end(session, minutes_ago, last="这一轮做完了。"):
    before = len(sent())
    p = start_waiter(session, last=last, transcript_path=turn_transcript(session, minutes_ago))
    check(waiting_for(session, p), f"  ({session}: the waiter still starts)")
    RB.mark_prompt(session)
    finish(p)
    return sent()[before:]


new = run_turn_end("n-long", 10)
check(len(new) == 1 and new[0]["kind"] == "notice" and new[0]["title"].endswith("一轮完成")
      and "这一轮用时：10 分钟" in new[0]["body"] and "这一轮做完了。" in new[0]["body"],
      f"  a 10-minute turn (default limit 5) -> notice with its duration and last reply: {new}")
check(run_turn_end("n-short", 1) == [], "  a 1-minute turn -> nothing")
check(run_turn_end("n-none", 10, last="") and "（这一轮没有文字回复）" in sent()[-1]["body"], "  no text -> says so")
feishu_notify.write_config_field("notify_after_minutes", "0")
check(run_turn_end("n-long", 10) == [], "  limit 0 (\"通知 关\") -> long turns are not reported")
RB.set_following("n-short", True)
new = run_turn_end("n-short", 1)
check(len(new) == 1 and "你关注了这个会话" in new[0]["body"] and new[0]["kind"] == "reply",
      "  a followed session reports even short turns, even while notifications are switched off")
feishu_notify.write_config_field("notify_after_minutes", "15")
check(run_turn_end("n-long", 10) == [], "  limit 15 -> a 10-minute turn is not reported")
feishu_notify.write_config_field("notify_after_minutes", "abc")
check(len(run_turn_end("n-long", 10)) == 1, "  a broken setting falls back to 5 minutes")
feishu_notify.write_config_field("notify_after_minutes", "5")
new = run_turn_end("n-long", 10, last="长" * 8000)
pages = [b for b in new[0]["buttons"] if b.get("kind") == "page"]
check(len(pages) == 1 and pages[0]["page"] == 1 and RB.load_doc(pages[0]["doc"])["text"] == "长" * 8000
      and "第 1/3 页" in new[0]["body"], "  a long reply: first page sent, all of it kept for the page buttons")
big = turn_transcript("n-big", 12)
with open(big, "a", encoding="utf-8") as f:  # the turn's start lies far before the 2 MB tail
    for n in range(800):
        f.write(json.dumps({"type": "user", "promptId": "pp", "timestamp": iso_now(),
                            "message": {"content": [{"type": "tool_result", "tool_use_id": f"t{n}",
                                                     "content": "x" * 4000}]}}) + "\n")
before = len(sent())
p = start_waiter("n-big", last="读了很多大文件", transcript_path=big)
check(waiting_for("n-big", p), "  (n-big: the waiter still starts)")
RB.mark_prompt("n-big")
finish(p)
new = sent()[before:]
check(len(new) == 1 and "这一轮用时：12 分钟" in new[0]["body"],
      f"  a long turn with more than 2 MB of output still counts from its start: {[n['body'][:80] for n in new]}")
RB.mark_relay("n-long")
new = run_turn_end("n-long", 10, last="飞书指令的结果")
check(len(new) == 1 and new[0]["kind"] == "reply", "  a turn started from Feishu: only the relay, no second notice")
p = start_waiter("n-gone", last="x", transcript_path=os.path.join(WORK, "missing.jsonl"))
check(waiting_for("n-gone", p), "  missing transcript -> no notice, the waiter still starts")
RB.mark_prompt("n-gone")
finish(p)

shutil.rmtree(WORK, ignore_errors=True)
print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S)")
sys.exit(1 if FAILS else 0)
