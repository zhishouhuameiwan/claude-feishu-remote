#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Feishu long-connection listener: receives card button clicks and hands the
answer to the waiting Claude hook through reply_bridge files.

Started by the scheduled task through launch_listener.py with the venv in the
data folder (lark-oapi lives there), see paths.py:
    <data>\\venv\\Scripts\\pythonw.exe <data>\\launch_listener.py      supervisor (default)
    <data>\\venv\\Scripts\\python.exe  feishu_listener.py --worker    one connection, foreground

Only one connection may exist: Feishu delivers each callback to one random
connection of the app. The supervisor and the worker each hold their own lock,
and a worker exits when the supervisor that started it is gone.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import msvcrt
import os
import subprocess
import sys
import threading
import time
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import decision_card
import feishu_notify
import paths
import reply_bridge
from decision_card import short

WORKER_LOG = str(paths.WORKER_LOG)
SUPERVISOR_LOG = str(paths.SUPERVISOR_LOG)
SUPERVISOR_LOCK = str(paths.SUPERVISOR_LOCK)
WORKER_LOCK = str(paths.WORKER_LOCK)
# Worker exit code: the plugin now lives in another folder (paths.PLUGIN_ROOT_FILE).
# The supervisor then exits too, and the scheduled task restarts both through
# launch_listener.py with the new code within 5 minutes.
CODE_MOVED = 5
# Only for requests without a recorded waiter pid: a waiting hook refreshes its
# pending file every 2 s, so older than this means it is gone.
WAITER_STALE_SECONDS = 20
CLEANUP_EVERY_SECONDS = 3600
WATCH_SECONDS = 5
# The SDK retries a lost connection about every 2 minutes, but gives up for good on
# some errors while the process lives on, deaf (2026-11-12: after a clock change).
# Disconnected this long, the worker exits and the supervisor starts a fresh one.
DISCONNECTED_LIMIT_SECONDS = 600
CREATE_NO_WINDOW = 0x08000000

log = logging.getLogger("feishu_listener")


def setup_logging(path: str) -> None:
    """One log file per process (Windows cannot rotate a file another process holds)."""
    handler = logging.handlers.RotatingFileHandler(
        path, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(process)d %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    # pythonw has no console: give stray prints a sink; SDK records reach the file via logging.
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _lock(path: str) -> Optional[Any]:
    handle = open(path, "a+")
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


# ---------------------------------------------------------------- responses

def _response(toast_type: str, toast: str, card: Optional[dict[str, Any]] = None) -> Any:
    from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse

    body: dict[str, Any] = {"toast": {"type": toast_type, "content": toast}}
    if card is not None:
        body["card"] = {"type": "raw", "data": card}
    return P2CardActionTriggerResponse(body)


# ---------------------------------------------------------------- handlers

def on_message(data: Any) -> None:
    """Binding: setup shows a one-time code on the computer; the user sends it to the
    bot in a private chat. That one message gives both the chat to write to and the
    only account allowed to answer. Every other message is ignored."""
    try:
        event = data.event
        message, sender = event.message, event.sender
        if message.chat_type != "p2p" or message.message_type != "text" or sender.sender_type != "user":
            return
        text = str(json.loads(message.content or "{}").get("text") or "")
        if feishu_notify.read_config_field("allowed_open_id") or not reply_bridge.take_bind_code(text):
            return
        open_id = str(sender.sender_id.open_id or "")
        feishu_notify.write_config_fields({"chat_id": str(message.chat_id), "allowed_open_id": open_id})
        log.info("bound open_id=%s chat_id=%s", open_id, message.chat_id)
        feishu_notify.send_feishu_message("Claude 远程回复 · 绑定成功",
                                          "以后 Claude Code 等你审批或回答时，会在这里给你发卡片。")
    except Exception:
        log.exception("message event failed")


def _closed_card(request: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    info = request.get("info", {})
    if outcome.get("kind") == "answer":
        answer = {"allow": "允许", "deny": "拒绝"}.get(outcome.get("choice", ""), "") \
            if request.get("type") == "permission" else ""
        answer = answer or outcome.get("choice") or outcome.get("text") or ""
        return decision_card.result_card(info, f"已在飞书回复：{short(answer, 60)}", "green")
    return decision_card.result_card(info, "这个问题已经结束，不再接受回复", "grey")


def _waiter_gone(rid: str, request: dict[str, Any]) -> bool:
    """Process identity decides; a late heartbeat alone (sleep/resume, a busy
    machine) must not end a waiter that is still running."""
    pid = request.get("waiter_pid")
    if pid is not None:
        return not reply_bridge.process_alive(pid, request.get("waiter_created"))
    # Requests written before waiters recorded their pid: heartbeat only.
    return time.time() - reply_bridge.request_mtime(rid) > WAITER_STALE_SECONDS


def _picked_option(request: dict[str, Any], value: dict[str, Any]) -> Optional[str]:
    """Map the button back to the full option label; only labels the card offered count."""
    options = [str(o) for o in request.get("info", {}).get("options", [])]
    index = value.get("index")
    if isinstance(index, int) and 0 <= index < len(options):
        return options[index]
    legacy = value.get("choice")  # cards sent before buttons carried an index
    return legacy if isinstance(legacy, str) and legacy in options else None


def handle_answer(open_id: str, value: dict[str, Any], form_value: dict[str, Any]) -> Any:
    allowed = feishu_notify.read_config_field("allowed_open_id")
    if not allowed or open_id != allowed:
        log.warning("rejected click from open_id=%s", open_id)
        return _response("error", "你没有权限回复这个问题")

    rid = str(value.get("rid") or "")
    request = reply_bridge.get_request(rid)
    if request is None:
        return _response("error", "找不到这个问题，可能已经过期")

    outcome = reply_bridge.get_outcome(rid)
    if outcome is not None:
        return _response("info", "这个问题已经处理过了", _closed_card(request, outcome))

    info = request.get("info", {})
    if _waiter_gone(rid, request):
        reply_bridge.close_request(rid, "waiter_gone")
        return _response("error", "Claude 那边已经不在等这个回复了，请回电脑上处理",
                         decision_card.result_card(info, "Claude 已不在等待，请回电脑上处理", "grey"))

    if request.get("type") == "ask":
        parsed_ask = _ask_answer(request, value, form_value)
        if isinstance(parsed_ask, str):
            return _response("error", parsed_ask)
        answers, summary = parsed_ask
        saved = reply_bridge.save_answers(rid, answers, summary, open_id)
        status, template = f"已在飞书回答：{summary}（如果电脑上已先回答，以电脑上的为准）", "green"
    else:
        parsed = (_permission_answer if request.get("type") == "permission" else _decision_answer)(
            request, value, form_value)
        if isinstance(parsed, str):
            return _response("error", parsed)
        choice, text, status, template = parsed
        saved = reply_bridge.save_answer(rid, choice, text, open_id)

    if not saved:
        return _response("info", "这个问题已经处理过了")
    log.info("answered rid=%s type=%s kind=%s", rid, request.get("type", "decision"), value.get("kind"))
    return _response("success", "已发送给 Claude", decision_card.result_card(info, status, template))


ANSWER_TEXT_LIMIT = 1000  # the card's text boxes allow 1000 characters


def _option_label(question: dict[str, Any], picked: Any) -> Optional[str]:
    """Map a button index (int) or dropdown value ("0", "1", ...) to the offered label."""
    if isinstance(picked, str) and picked.isdigit():
        picked = int(picked)
    options = question.get("options", [])
    if isinstance(picked, int) and not isinstance(picked, bool) and 0 <= picked < len(options):
        return str(options[picked].get("label", ""))
    return None


def _ask_answer(request: dict[str, Any], value: dict[str, Any],
                form_value: dict[str, Any]) -> tuple[dict[str, Any], str] | str:
    """(answers keyed by the exact question text, one-line summary), or a toast error.
    A free-text answer wins over a pick for single-choice questions and is added
    to the picks for multi-select ones, as Claude Code's own dialog does."""
    questions = request.get("info", {}).get("questions", [])
    kind = value.get("kind")
    answers: dict[str, Any] = {}
    if kind in ("choice", "text"):
        if len(questions) != 1:
            return "这个按钮不认识"
        question = questions[0]
        if kind == "text":
            answer = _form_text(form_value)[:ANSWER_TEXT_LIMIT]
            if not answer:
                return "回答是空的"
        else:
            answer = _option_label(question, value.get("index"))
            if answer is None:
                return "这个按钮对应的选项不存在"
        answers[question["question"]] = answer
    elif kind == "form":
        for number, question in enumerate(questions):
            other = str(form_value.get(f"q{number}_other") or "").strip()[:ANSWER_TEXT_LIMIT]
            picked = form_value.get(f"q{number}")
            if question.get("multiSelect"):
                picks = picked if isinstance(picked, list) else ([] if picked in (None, "") else [picked])
                labels = [_option_label(question, p) for p in picks]
                if any(label is None for label in labels):
                    return f"第 {number + 1} 题的选项不存在"
                answer = labels + ([other] if other else [])
            else:
                answer = other or _option_label(question, picked)
            if not answer:
                return f"第 {number + 1} 题还没回答"
            answers[question["question"]] = answer
    else:
        return "这个按钮不认识"
    summary = "；".join(
        (f"{short(q, 20)} → " if len(answers) > 1 else "") + short(a if isinstance(a, str) else "、".join(a), 40)
        for q, a in answers.items())
    return answers, summary


def _form_text(form_value: dict[str, Any]) -> str:
    return str((form_value or {}).get("reply_text") or "").strip()


def _decision_answer(request: dict[str, Any], value: dict[str, Any],
                     form_value: dict[str, Any]) -> tuple[str, str, str, str] | str:
    """(choice, text, status, template), or an error message for the toast."""
    if value.get("kind") == "text":
        text = _form_text(form_value)
        if not text:
            return "回复内容是空的"
        return "", text, f"已在飞书回复：{short(text, 60)}，Claude 继续处理中", "green"
    choice = _picked_option(request, value)
    if not choice:
        return "这个按钮对应的选项不存在"
    return choice, "", f"已在飞书回复：{short(choice, 60)}，Claude 继续处理中", "green"


def _permission_answer(request: dict[str, Any], value: dict[str, Any],
                       form_value: dict[str, Any]) -> tuple[str, str, str, str] | str:
    kind = value.get("kind")
    # The tool may already be running if the dialog was answered at the computer
    # first; the hook's late answer is then ignored by Claude Code.
    note = "（如果电脑上已先处理，以电脑上的为准）"
    if kind == "allow":
        if not request.get("info", {}).get("complete", True):
            return "参数没有完整显示，不能在飞书允许；请回电脑上处理"
        return "allow", "", f"已在飞书允许{note}", "green"
    if kind == "deny":
        return "deny", "", f"已在飞书拒绝{note}", "red"
    if kind == "text":
        text = _form_text(form_value)
        if not text:
            return "说明是空的；不想写说明就直接点“拒绝”"
        return "deny", text, f"已在飞书拒绝，并说明：{short(text, 60)}{note}", "red"
    return "这个按钮不认识"


def on_card_action(data: Any) -> Any:
    try:
        event = data.event
        open_id = str(getattr(event.operator, "open_id", "") or "")
        action = event.action
        value = action.value if isinstance(action.value, dict) else {}
        if value.get("kind") == "select_change":
            # A dropdown inside a form changed; only the submit button counts.
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
            return P2CardActionTriggerResponse({})
        return handle_answer(open_id, value, action.form_value or {})
    except Exception:
        log.exception("card action failed")
        return _response("error", "处理失败，请回电脑上回复")


def ignore_event(_data: Any) -> None:
    """The app also subscribes to some chat events; acknowledge them so Feishu does not retry."""


# ---------------------------------------------------------------- processes

def _connected(client: Any) -> bool:
    """lark-oapi 1.7.5 keeps the live websocket in the private _conn (None while
    disconnected). Should a later SDK drop it, count as connected: no false restarts."""
    return getattr(client, "_conn", True) is not None


# Everything the listener imports: a folder missing any of them (half unzipped) is not moved to.
LISTENER_FILES = ("feishu_listener.py", "paths.py", "reply_bridge.py", "feishu_notify.py", "decision_card.py")


def _same_folder(a: str, b: str) -> bool:
    """realpath on both sides: junctions, 8.3 names and subst drives must not look like a move."""
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _code_moved() -> bool:
    """A session start recorded another complete plugin folder (update or move)."""
    try:
        root = paths.PLUGIN_ROOT_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not root or _same_folder(root, str(paths.PLUGIN_ROOT)):
        return False
    return all(os.path.isfile(os.path.join(root, "scripts", name)) for name in LISTENER_FILES)


def _exit(code: int) -> None:
    try:
        logging.shutdown()
    finally:
        os._exit(code)


def _watch(parent_pid: int, client: Any) -> None:
    """Exit when the supervisor is gone (no orphan connection) or the connection has
    been down too long; clean up hourly. Monotonic time: clock changes do not count.
    Errors are logged and the loop goes on: a dead watcher would leave a deaf worker."""
    last_cleanup = 0.0
    down_since: Optional[float] = None
    while True:
        try:
            if parent_pid and not reply_bridge.process_alive(parent_pid):
                log.warning("supervisor %s is gone; worker exiting", parent_pid)
                _exit(0)
            if _code_moved():
                log.warning("plugin is now in %s; worker exiting so the new code starts",
                            paths.PLUGIN_ROOT_FILE.read_text(encoding="utf-8").strip())
                _exit(CODE_MOVED)
            if _connected(client):
                down_since = None
            elif down_since is None:
                down_since = time.monotonic()
            elif time.monotonic() - down_since > DISCONNECTED_LIMIT_SECONDS:
                log.warning("no Feishu connection for %d s; worker exiting to reconnect",
                            time.monotonic() - down_since)
                _exit(4)
            if time.time() - last_cleanup > CLEANUP_EVERY_SECONDS:
                last_cleanup = time.time()
                reply_bridge.cleanup()
        except Exception:
            log.exception("watch round failed")
        time.sleep(WATCH_SECONDS)


def run_worker(parent_pid: int) -> int:
    setup_logging(WORKER_LOG)  # before importing the SDK: its log handler binds to sys.stderr
    lock = _lock(WORKER_LOCK)
    if lock is None:
        log.warning("another worker holds the connection; exiting")
        return 3
    import lark_oapi as lark

    app_id, app_secret = feishu_notify.load_app_credentials()
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(on_card_action)
        .register_p2_im_message_receive_v1(on_message)
        .register_p2_im_message_message_read_v1(ignore_event)
        .register_p2_im_chat_access_event_bot_p2p_chat_entered_v1(ignore_event)
        .build()
    )
    # WARNING: at INFO the SDK logs the connection URL, which carries an access key.
    client = lark.ws.Client(app_id, app_secret, event_handler=handler, log_level=lark.LogLevel.WARNING)
    if not hasattr(client, "_conn"):
        log.warning("lark-oapi has no Client._conn any more: the disconnect watchdog is blind")
    threading.Thread(target=_watch, args=(parent_pid, client), daemon=True).start()
    log.info("worker connecting (supervisor %s)", parent_pid)
    client.start()
    return 0


def run_supervisor() -> int:
    # Lock first: the watchdog task starts this every 5 minutes, and a duplicate
    # should leave no trace (nor hold the log file open).
    lock = _lock(SUPERVISOR_LOCK)
    if lock is None:
        return 0
    setup_logging(SUPERVISOR_LOG)
    log.info("supervisor started")
    while True:
        started = time.time()
        code = subprocess.call(
            [sys.executable, os.path.abspath(__file__), "--worker", "--parent", str(os.getpid())],
            creationflags=CREATE_NO_WINDOW,
        )
        log.warning("worker exited with code %s", code)
        # Our own folder may be gone (old version pruned, folder deleted) while the
        # worker was down: then restarting it from here can never work again.
        if code == CODE_MOVED or _code_moved() or not os.path.isfile(os.path.abspath(__file__)):
            log.info("supervisor exiting; the scheduled task restarts the listener from the recorded folder")
            return 0
        time.sleep(5 if time.time() - started > 300 else 60)


def _parent_arg() -> int:
    if "--parent" in sys.argv:
        try:
            return int(sys.argv[sys.argv.index("--parent") + 1])
        except (IndexError, ValueError):
            return 0
    return 0


if __name__ == "__main__":
    if "--worker" in sys.argv:
        sys.exit(run_worker(_parent_arg()))
    sys.exit(run_supervisor())
