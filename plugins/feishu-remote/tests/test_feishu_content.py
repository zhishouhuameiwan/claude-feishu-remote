"""Verify title + decision content in Feishu reminders, without sending real messages."""
import json
import os
import shutil
import subprocess
import sys
import tempfile

from _testenv import HOOK_DIR, isolate

WORK = isolate("feishu_test_")
PLUGIN_DIR = os.path.dirname(HOOK_DIR)
CLAUDE = os.path.join(HOOK_DIR, "claude_ntfy.py")
RECORD = os.path.join(WORK, "record.json")
FAILS = []

RUNNER = r'''
import json, runpy, sys
hook_dir, script, record, mode = sys.argv[1:5]
sys.argv = [script] + sys.argv[5:]
sys.path.insert(0, hook_dir)
import feishu_notify
def fake(title, body):
    if mode == "raise":
        raise RuntimeError("simulated network failure")
    with open(record, "w", encoding="utf-8") as f:
        json.dump({"title": title, "body": body}, f, ensure_ascii=False)
    return True
feishu_notify.send_feishu_message = fake
try:
    runpy.run_path(script, run_name="__main__")
except SystemExit as e:
    sys.exit(e.code)
'''


def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def run(script, args=(), payload=None, mode="ok", env=None, raw=None):
    if os.path.exists(RECORD):
        os.remove(RECORD)
    stdin = raw if raw is not None else json.dumps(payload or {}, ensure_ascii=False).encode("utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", RUNNER, HOOK_DIR, script, RECORD, mode, *args],
        input=stdin, capture_output=True, timeout=60, env={**os.environ, **(env or {})},
    )
    rec = json.load(open(RECORD, encoding="utf-8")) if os.path.exists(RECORD) else None
    # Every hook run must exit 0 with empty stdout.
    check(proc.returncode == 0 and proc.stdout == b"",
          f"  exit 0 + empty stdout (rc={proc.returncode}, stdout={proc.stdout[:80]!r})")
    return rec, proc


def make_transcript(name, entries, title=None, filler_bytes=0):
    path = os.path.join(WORK, name + ".jsonl")
    with open(path, "w", encoding="utf-8") as f:
        if filler_bytes:
            filler = json.dumps({"type": "user", "message": {"content": "x" * 1000}}) + "\n"
            for _ in range(filler_bytes // len(filler) + 1):
                f.write(filler)
        for e in entries:
            f.write(json.dumps(e, ensure_ascii=False) + "\n")
    if title is not None:
        os.makedirs(path[:-6], exist_ok=True)
        with open(os.path.join(path[:-6], "custom-title.json"), "w", encoding="utf-8") as f:
            json.dump({"customTitle": title}, f, ensure_ascii=False)
    return path


def tool_use(mid, tid, name, inp):
    return {"type": "assistant", "message": {"id": mid, "content": [
        {"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def tool_result(tid):
    return {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": tid}]}}


CWD = r"E:\Work\demo-app"
WRAP = "mcp__ccd_turn__wrap_up"
ASK = {"recap": "已查清来源。", "ask": "要不要提醒？", "blocked": True,
       "actions": [{"label": "只在卡住等我时"}, {"label": "有提问就发"}, {"action": "commit"}]}

# ---------- hooks/hooks.json wiring ----------
settings = json.loads(open(os.path.join(PLUGIN_DIR, "hooks", "hooks.json"), "rb").read().decode("utf-8"))
all_hooks = [(ev, h) for ev, groups in settings["hooks"].items() for g in groups for h in g["hooks"]]
check(all(h["command"] == "python" and h["args"][0].startswith("${CLAUDE_PLUGIN_ROOT}/scripts/")
          and os.path.isfile(os.path.join(HOOK_DIR, h["args"][0].rsplit("/", 1)[1])) for _, h in all_hooks),
      "hooks.json: every hook runs an existing script under ${CLAUDE_PLUGIN_ROOT}/scripts with python")
check({ev for ev, _ in all_hooks} == {"SessionStart", "PermissionRequest", "Notification", "PreToolUse",
                                     "PostToolUse", "UserPromptSubmit", "Stop", "StopFailure"},
      "hooks.json: the expected events")
pre = {g["matcher"]: g["hooks"][0] for g in settings["hooks"]["PreToolUse"]}
check(pre[WRAP]["args"][0].endswith("claude_decision.py") and pre[WRAP].get("asyncRewake") is True,
      "settings: wrap_up -> claude_decision.py with asyncRewake")
wrap_hooks = next(g["hooks"] for g in settings["hooks"]["PreToolUse"] if g["matcher"] == WRAP)
redirect_hook = wrap_hooks[1] if len(wrap_hooks) > 1 else {}
check(str((redirect_hook.get("args") or [""])[0]).endswith("claude_wrapup_redirect.py")
      and not redirect_hook.get("asyncRewake") and 0 < redirect_hook.get("timeout", 0) < 12,
      "settings: wrap_up -> claude_wrapup_redirect.py, synchronous, timeout below the decision hook's 12 s wait")
check(settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["args"][0].endswith("claude_cancel_pending.py"),
      "settings: UserPromptSubmit -> claude_cancel_pending.py")
# No "task complete" reminder at every turn end: the only Stop hook is the command waiter,
# which relays a reply only for turns started from Feishu (test_feishu_sessions.py).
check([h["args"][0].rsplit("/", 1)[1] for ev, h in all_hooks if ev == "Stop"] == ["claude_command_wait.py"],
      "settings: the only Stop hook is the Feishu command waiter")
for name in (".claude-plugin/plugin.json", "../../.claude-plugin/marketplace.json"):
    json.loads(open(os.path.join(PLUGIN_DIR, name), encoding="utf-8").read())
check(True, "plugin.json and marketplace.json parse")

titled = make_transcript("titled", [{"type": "custom-title", "customTitle": "旧标题"}], title="飞书机器人提醒频率优化")

print("== wrap_up blocked -> sends title + question + options")
rec, _ = run(CLAUDE, ["decision", "fb"], {"hook_event_name": "PreToolUse", "tool_name": WRAP,
                                          "tool_input": ASK, "transcript_path": titled, "cwd": CWD})
body = rec and rec["body"] or ""
check(rec and rec["title"] == "[?] Claude Code · 等你决策", f"  title {rec and rec['title']!r}")
check("会话: 飞书机器人提醒频率优化" in body, "  session title from custom-title.json")
check(f"项目: {CWD}" in body, "  project line")
check("问题: 要不要提醒？" in body and "1. 只在卡住等我时" in body and "3. commit" in body, "  question + options")
check("进展: 已查清来源。" in body, "  recap included")
print(body)

print("== wrap_up NOT blocked -> no message")
for inp in ({**ASK, "blocked": False}, {k: v for k, v in ASK.items() if k != "blocked"}):
    rec, _ = run(CLAUDE, ["decision", "fb"], {"hook_event_name": "PreToolUse", "tool_name": WRAP,
                                              "tool_input": inp, "transcript_path": titled})
    check(rec is None, f"  blocked={inp.get('blocked')!r}: nothing sent")

print("== AskUserQuestion -> questions + options")
q = {"questions": [{"question": "这个文件谁来传？", "header": "改XML", "multiSelect": False,
                    "options": [{"label": "我自己上传", "description": "我传完告诉你"}, {"label": "给你凭证"}]}]}
rec, _ = run(CLAUDE, ["input", "fb"], {"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                                       "tool_input": q, "transcript_path": titled, "cwd": CWD})
body = rec and rec["body"] or ""
check("问题1: 这个文件谁来传？" in body and "1. 我自己上传 - 我传完告诉你" in body and "2. 给你凭证" in body,
      "  question with options")

print("== ExitPlanMode -> plan, truncated")
rec, _ = run(CLAUDE, ["approval", "fb"], {"hook_event_name": "PreToolUse", "tool_name": "ExitPlanMode",
                                          "tool_input": {"plan": "第一步\n" + "长" * 3000}, "transcript_path": titled})
body = rec and rec["body"] or ""
check("计划:\n第一步" in body and "[truncated]" in body and len(body) < 1800, f"  plan truncated (len {len(body)})")

print("== permission_prompt: pending Bash in a >2MB transcript, title from JSONL fallback")
big = make_transcript("big", [
    {"type": "custom-title", "customTitle": "修复登录页 bug 1234"},
    tool_use("m1", "t1", "Read", {"file_path": "a.txt"}), tool_result("t1"),
    tool_use("m2", "t2", "Bash", {"command": "git push origin main", "description": "推送到远端"}),
], filler_bytes=3 * 1024 * 1024)
check(os.path.getsize(big) > 3 * 1024 * 1024, "  fake transcript is >3MB")
rec, _ = run(CLAUDE, ["approval", "fb"], {"hook_event_name": "Notification", "transcript_path": big, "cwd": CWD,
                                          "message": "Claude needs your permission to use Bash"})
body = rec and rec["body"] or ""
check(rec and rec["title"] == "[!] Claude Code · 审批请求", "  approval title")
check("会话: 修复登录页 bug 1234" in body, "  title from JSONL custom-title entry")
check("工具: Bash" in body and "内容: git push origin main" in body and "说明: 推送到远端" in body, "  pending command")
check("Read" not in body, "  finished tool calls are not listed")
check("提示: Claude needs your permission to use Bash" in body, "  original message kept")
print(body)

print("== permission_prompt for ExitPlanMode -> skipped (already notified by PreToolUse)")
t = make_transcript("dup_plan", [tool_use("m9", "t9", "ExitPlanMode", {})], title="x")
rec, _ = run(CLAUDE, ["approval", "fb"], {"hook_event_name": "Notification", "transcript_path": t})
check(rec is None, "  ExitPlanMode: no duplicate message")
print("== permission_prompt for AskUserQuestion with no card marker -> text with the question (safety net)")
t = make_transcript("dup_ask", [tool_use("m9", "t9", "AskUserQuestion",
                                         {"questions": [{"question": "选哪个？", "options": [{"label": "甲"}]}]})], title="x")
rec, _ = run(CLAUDE, ["approval", "fb"], {"hook_event_name": "Notification", "transcript_path": t, "session_id": "no-card"})
check(rec is not None and "选哪个？" in rec["body"] and "甲" in rec["body"], "  AskUserQuestion: text shows the question")
t = make_transcript("dup_wrap", [tool_use("m9", "t9", WRAP, {"ask": "q", "blocked": False})], title="x")
rec, _ = run(CLAUDE, ["approval", "fb"], {"hook_event_name": "Notification", "transcript_path": t})
check(rec is not None and WRAP in rec["body"], "  wrap_up permission prompt is NOT swallowed")

print("== title lookup crashing keeps the decision content")
TITLE_RAISE = r'''
import importlib.util, io, json, sys
hook_dir, script, record = sys.argv[1:4]
sys.path.insert(0, hook_dir)
import feishu_notify
def fake(title, body):
    with open(record, "w", encoding="utf-8") as f:
        json.dump({"title": title, "body": body}, f, ensure_ascii=False)
    return True
feishu_notify.send_feishu_message = fake
spec = importlib.util.spec_from_file_location("claude_ntfy", script)
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
def boom(path): raise OSError("transcript locked")
mod.session_title = boom
sys.argv = [script, "input", "fb"]
sys.exit(mod.main())
'''
if os.path.exists(RECORD):
    os.remove(RECORD)
proc = subprocess.run([sys.executable, "-c", TITLE_RAISE, HOOK_DIR, CLAUDE, RECORD],
                      input=json.dumps({"hook_event_name": "PreToolUse", "tool_name": "AskUserQuestion",
                                        "tool_input": q, "cwd": CWD}, ensure_ascii=False).encode("utf-8"),
                      capture_output=True, timeout=60)
rec = json.load(open(RECORD, encoding="utf-8")) if os.path.exists(RECORD) else None
check(proc.returncode == 0 and proc.stdout == b"" and b"transcript locked" in proc.stderr, "  exit 0, error on stderr")
check(rec and "问题1: 这个文件谁来传？" in rec["body"] and f"项目: {CWD}" in rec["body"] and "会话:" not in rec["body"],
      "  question + project kept, only title dropped")

print("== permission_prompt with parallel calls only lists the newest assistant message")
t = make_transcript("parallel", [
    tool_use("old", "s1", "Bash", {"command": "stale-interrupted"}),
    tool_use("new", "p1", "Bash", {"command": "cmd-one"}),
    tool_use("new", "p2", "mcp__tracker__create_bug", {"title": "bug"}),
], title="并行")
rec, _ = run(CLAUDE, ["approval", "fb"], {"hook_event_name": "Notification", "transcript_path": t})
body = rec and rec["body"] or ""
check("cmd-one" in body and "mcp__tracker__create_bug" in body and '"title": "bug"' in body, "  both new calls listed")
check("stale-interrupted" not in body, "  stale call ignored")

print("== no transcript at all -> falls back to argv message, last-prompt title fallback")
rec, _ = run(CLAUDE, ["approval", "Claude Code needs your approval"], {"hook_event_name": "Notification"})
check(rec and rec["body"] == "Claude Code needs your approval", f"  fallback body {rec and rec['body']!r}")
t = make_transcript("lp", [{"type": "last-prompt", "lastPrompt": "查看缺陷列表"}])
rec, _ = run(CLAUDE, ["input", "fb"], {"hook_event_name": "Notification", "transcript_path": t,
                                       "message": "MCP server needs input"})
body = rec and rec["body"] or ""
check("会话: 查看缺陷列表" in body and "提示: MCP server needs input" in body, "  last-prompt title + MCP message")

print("== StopFailure -> error text")
rec, _ = run(CLAUDE, ["error", "Claude Code encountered an error"],
             {"hook_event_name": "StopFailure", "error": "rate_limit", "transcript_path": titled})
check(rec and "提示: rate_limit" in rec["body"] and rec["title"] == "[x] Claude Code · 发生错误", "  error shown")

print("== robustness")
rec, _ = run(CLAUDE, ["approval", "fallback text"], raw=b"not json")
check(rec and rec["body"] == "fallback text", "  garbage stdin still reminds with fallback")
rec, proc = run(CLAUDE, ["decision", "fb"], {"hook_event_name": "PreToolUse", "tool_name": WRAP,
                                             "tool_input": ASK, "transcript_path": titled}, mode="raise")
check(b"send failed" in proc.stderr, "  send failure -> stderr only")

print("== plugin installed but setup.ps1 not run: every hook stays silent and writes nothing")
fresh = os.path.join(WORK, "never_set_up")  # does not exist, like on a machine before setup
fresh_env = {"FEISHU_REMOTE_HOME": fresh, "FEISHU_REPLY_DIR": os.path.join(fresh, "replies"),
             "FEISHU_HOOK_LOG": os.path.join(fresh, "hooks.log")}
cases = [
    ("claude_ntfy.py", ["approval"], {"hook_event_name": "Notification", "message": "needs approval"}),
    ("claude_ntfy.py", ["error"], {"hook_event_name": "StopFailure", "error": "boom"}),
    ("claude_permission.py", [], {"hook_event_name": "PermissionRequest", "session_id": "nr",
                                  "tool_name": "Bash", "tool_input": {"command": "ls"}}),
    ("claude_decision.py", [], {"hook_event_name": "PreToolUse", "tool_name": WRAP, "session_id": "nr",
                                "tool_use_id": "toolu_nr", "tool_input": ASK}),
    ("claude_wrapup_redirect.py", [], {"hook_event_name": "PreToolUse", "tool_name": WRAP, "session_id": "nr",
                                       "tool_use_id": "toolu_nr", "tool_input": ASK}),
    ("claude_cancel_pending.py", [], {"session_id": "nr"}),
]
for script, args, payload in cases:
    rec, proc = run(os.path.join(HOOK_DIR, script), args, payload, env=fresh_env)
    check(rec is None and not os.path.exists(fresh), f"  {script} {' '.join(args)}: nothing sent, nothing written")
proc = subprocess.run([sys.executable, os.path.join(HOOK_DIR, "claude_session_start.py")], input=b"{}",
                      capture_output=True, timeout=30, env={**os.environ, **fresh_env})
check(proc.returncode == 0 and proc.stdout == b"" and not os.path.exists(fresh),
      "  claude_session_start.py: no rule injected, nothing written")

print("== session start once set up: rule injected, plugin folder recorded for the listener")
root_file = os.path.join(WORK, "plugin_root.txt")
proc = subprocess.run([sys.executable, os.path.join(HOOK_DIR, "claude_session_start.py")], input=b"{}",
                      capture_output=True, timeout=30)
out = json.loads(proc.stdout or b"{}").get("hookSpecificOutput", {})
check(proc.returncode == 0 and out.get("hookEventName") == "SessionStart"
      and "飞书远程回复规则" in out.get("additionalContext", "") and "AskUserQuestion" in out.get("additionalContext", ""),
      "  additionalContext carries the Feishu reply rule")
check(open(root_file, encoding="utf-8").read() == PLUGIN_DIR, "  plugin_root.txt names this plugin folder")
print("== setup_helper")
helper = os.path.join(HOOK_DIR, "setup_helper.py")
env_only = {**fresh_env, "FEISHU_APP_ID": "cli_env", "FEISHU_APP_SECRET": "env-secret"}
proc = subprocess.run([sys.executable, helper, "has-credentials"], capture_output=True, timeout=30,
                      env={**os.environ, **env_only})
check(proc.returncode == 1, "  credentials only in the environment -> setup still asks (ready() reads config.json)")
proc = subprocess.run([sys.executable, helper, "has-credentials"], capture_output=True, timeout=30)
check(proc.returncode == 0, "  credentials in config.json -> setup skips the prompt")
os.remove(root_file)
proc = subprocess.run([sys.executable, helper, "record-root"], capture_output=True, timeout=30)
check(proc.returncode == 0 and open(root_file, encoding="utf-8").read() == PLUGIN_DIR,
      "  record-root writes the same spelling as the session-start hook")

sys.path.insert(0, HOOK_DIR)
import io  # noqa: E402
import setup_helper  # noqa: E402
import feishu_notify  # noqa: E402


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


setup_helper.urllib.request.urlopen = lambda request, timeout=0: _Resp(b'{"code": 0, "tenant_access_token": "t"}')
for app_id, keeps in (("cli_test", True), ("cli_other", False)):
    feishu_notify.write_config_fields({"app_id": "cli_test", "chat_id": "oc_test", "allowed_open_id": "ou_me"})
    os.environ.update({"FEISHU_SETUP_APP_ID": app_id, "FEISHU_SETUP_APP_SECRET": "new-secret"})
    setup_helper.save_credentials()
    bound = feishu_notify.read_config_field("allowed_open_id") == "ou_me" and feishu_notify.read_config_field("chat_id")
    check(bool(bound) == keeps and feishu_notify.read_config_field("app_id") == app_id,
          f"  save-credentials with {'the same' if keeps else 'another'} app: binding {'kept' if keeps else 'cleared'}")
os.environ.pop("FEISHU_SETUP_APP_ID"); os.environ.pop("FEISHU_SETUP_APP_SECRET")

for script, phrase in (("claude_decision.py", "飞书远程回复规则"), ("claude_wrapup_redirect.py", "飞书远程回复规则"),
                       ("claude_permission.py", "飞书远程回复规则"), ("reply_bridge.py", "飞书远程回复规则")):
    text = open(os.path.join(HOOK_DIR, script), encoding="utf-8").read()
    check(phrase in text and "CLAUDE.md" not in text, f"  {script} points at the injected rule, not a CLAUDE.md")

print("== answers to the user's own commands ignore the reminder switch")
sent_raw = []
saved_send = feishu_notify._send_to_chat
feishu_notify._send_to_chat = lambda msg_type, content: sent_raw.append(msg_type) or "om_x"
open(feishu_notify.SWITCH_FILE, "w").close()
try:
    check(feishu_notify.send_feishu_card({"x": 1}) is None, "  reminders off: a reminder card is not sent")
    feishu_notify.send_reply_card({"x": 1})
    feishu_notify.send_reply_text("t", "b")
    check(sent_raw == ["interactive", "text"], f"  ... but command replies are: {sent_raw}")
finally:
    os.remove(feishu_notify.SWITCH_FILE)
    feishu_notify._send_to_chat = saved_send

print("== card waits: 12 hours, below the hook timeouts in hooks.json")
with open(os.path.join(PLUGIN_DIR, "hooks", "hooks.json"), encoding="utf-8") as f:
    HOOKS = json.load(f)["hooks"]


def hook_timeout(script):
    return next((hook.get("timeout") for groups in HOOKS.values() for group in groups for hook in group["hooks"]
                 if any(str(arg).endswith("/scripts/" + script) for arg in hook.get("args", []))), None)


import claude_command_wait  # noqa: E402
import claude_decision  # noqa: E402
import claude_permission  # noqa: E402
import claude_session_start  # noqa: E402

for module, script in ((claude_permission, "claude_permission.py"), (claude_decision, "claude_decision.py"),
                       (claude_command_wait, "claude_command_wait.py")):
    timeout = hook_timeout(script)
    check(module.MAX_WAIT_SECONDS == 12 * 3600 and timeout is not None and timeout - module.MAX_WAIT_SECONDS >= 60,
          f"  {script}: waits {module.MAX_WAIT_SECONDS} s, hook timeout {timeout} s")
stop_hook = next(hook for group in HOOKS["Stop"] for hook in group["hooks"])
check(stop_hook.get("asyncRewake") is True, "  the command waiter runs in the background and may wake Claude")
marker = "【飞书指令】"
check(claude_command_wait.wake_message([{"text": "x"}]).startswith(marker) and marker in claude_session_start.RULE,
      "  the injected rule names the marker the command waiter uses")
pre_all = [hook for group in HOOKS["PreToolUse"] if group.get("matcher", "") == "" for hook in group["hooks"]]
control = [h for h in pre_all if h["args"][-1].endswith("claude_turn_control.py")]
stop_wait = [h for h in pre_all if h["args"][-1].endswith("claude_stop_wait.py")]
check(len(control) == 1 and not control[0].get("asyncRewake") and not control[0].get("async")
      and control[0]["timeout"] <= 10, "  every tool call: the sync turn control hook, short timeout")
check(len(stop_wait) == 1 and stop_wait[0].get("asyncRewake") is True
      and stop_wait[0]["timeout"] - claude_command_wait.MAX_WAIT_SECONDS >= 60,
      "  ... and its async partner, which may wait 12 hours for a command after a stop")
import claude_permission as permission_module  # noqa: E402
import claude_turn_control  # noqa: E402
import reply_bridge as bridge_module  # noqa: E402
stop_phrase = "用户在飞书上叫停了这一轮"
check(stop_phrase in claude_turn_control.STOP_REASON and stop_phrase in permission_module.STOP_MESSAGE
      and stop_phrase in claude_session_start.RULE, "  the rule explains the stop wording both hooks use")
during = bridge_module.command_text([{"text": "x"}], during_turn=True)
check(during.startswith(marker) and "这一轮进行中送达" in during and "这一轮进行中送达" in claude_session_start.RULE,
      "  ... and commands added to a running turn")

shutil.rmtree(WORK, ignore_errors=True)
print()
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILURE(S)")
sys.exit(1 if FAILS else 0)
