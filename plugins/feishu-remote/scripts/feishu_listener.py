#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Feishu long-connection listener: receives card button clicks and hands the
answer to the waiting Claude hook through reply_bridge files. It also answers
the commands the bound user sends to the bot ("会话": session_list.py).

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
import session_list
from decision_card import short

WORKER_LOG = str(paths.WORKER_LOG)
SUPERVISOR_LOG = str(paths.SUPERVISOR_LOG)
SUPERVISOR_LOCK = str(paths.SUPERVISOR_LOCK)
WORKER_LOCK = str(paths.WORKER_LOCK)
# Worker exit code: the plugin now lives in another folder (paths.PLUGIN_ROOT_FILE).
# The supervisor then exits too, and the scheduled task restarts both through
# launch_listener.py with the new code within 5 minutes.
CODE_MOVED = 5
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
    """Private text messages to the bot.

    Before binding: setup shows a one-time code on the computer; the user sends it
    to the bot in a private chat. That one message gives both the chat to write to
    and the only account allowed to answer. Every other message is ignored.
    After binding: the bound account's messages are commands (see COMMANDS) or text
    for a session (handle_text); all other senders are ignored."""
    try:
        event = data.event
        message, sender = event.message, event.sender
        if message.chat_type != "p2p" or message.message_type != "text" or sender.sender_type != "user":
            return
        text = str(json.loads(message.content or "{}").get("text") or "")
        open_id = str(sender.sender_id.open_id or "")
        allowed = feishu_notify.read_config_field("allowed_open_id")
        if allowed:
            if open_id == allowed:
                replied_to = [str(getattr(message, name, "") or "") for name in ("parent_id", "root_id")]
                _in_background(handle_text, text, replied_to)
            else:
                log.warning("ignored message from open_id=%s", open_id)
            return
        if not reply_bridge.take_bind_code(text):
            return
        feishu_notify.write_config_fields({"chat_id": str(message.chat_id), "allowed_open_id": open_id})
        log.info("bound open_id=%s chat_id=%s", open_id, message.chat_id)
        feishu_notify.send_feishu_message("Claude 远程回复 · 绑定成功",
                                          "以后 Claude Code 等你审批或回答时，会在这里给你发卡片。"
                                          "发送“会话”可以查看活跃会话并给它们发指令，发送“帮助”查看全部命令。")
    except Exception:
        log.exception("message event failed")


# ---------------------------------------------------------------- commands

# What the bound user can send to the bot (case and a leading "/" are ignored; the
# first word is the command, the rest its argument).
COMMANDS = {"会话": "sessions", "sessions": "sessions", "帮助": "help", "help": "help",
            "通知": "notify", "notify": "notify"}
HELP_TITLE = "Claude 远程回复 · 可用命令"
HELP_BODY = ("会话：列出本机的活跃会话（工作中、等你处理、空闲但能接收指令的），"
             "每个会话下面有输入框，可以直接给它发指令，Claude 做完会把最后的回复发回这里；"
             "点“查看”看它正在做什么，详情卡上可以“关注”它（之后每轮结束都推送）；"
             "“停止”叫停它正在跑的这一轮（在它下一次调用工具时生效）\n"
             "通知 [分钟数|关]：任何一轮运行超过这么久，结束时把最后的回复推到这里（默认 5 分钟）\n"
             "帮助：显示这条说明\n\n"
             "给会话发指令的另外两种方式：在聊天里“回复”某个会话发来的消息或卡片，文字就发给那个会话；"
             "或在会话列表上点“设为当前会话”，之后 30 分钟内直接打的字都发给它。"
             "会话正忙时，指令在它下一次调用工具时送达。回答审批和提问请直接在卡片上操作。")
NOTIFY_MAX_MINUTES = 24 * 60


def _in_background(fn: Any, *args: Any) -> None:
    """Run a slow reply off the SDK's event thread, so the event is acknowledged at once.
    When the reply fails (network, Feishu refusing it), the user is told so instead
    of waiting for an answer that never comes."""
    def run() -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("background reply failed")
            try:
                feishu_notify.send_reply_text("Claude 远程回复 · 出错了",
                                              "处理你的命令时出错了，详情见电脑上的 listener.log。")
            except Exception:
                log.exception("could not report the failure either")
    threading.Thread(target=run, daemon=True).start()


def _sessions_card() -> dict[str, Any]:
    try:
        return session_list.sessions_card()
    except Exception as exc:  # a bug here must still answer the user
        log.exception("session list failed")
        return session_list.error_card(f"插件内部错误（{type(exc).__name__}），详情见 listener.log。")


def parse_command(text: str) -> tuple[Optional[str], str]:
    """(command, argument); command None when the first word is no command."""
    word, _, argument = text.replace("\u3000", " ").strip().lstrip("/").partition(" ")  # full-width space
    command = COMMANDS.get(word.lower())
    if command is None and word.startswith("通知"):  # "通知10": no space needed in Chinese
        command, argument = "notify", word[len("通知"):]
    return command, argument.strip()


def handle_text(text: str, replied_to: list[str]) -> None:
    """Chat text of the bound user. A reply to a message about a session goes to that
    session, whatever it says; otherwise a command word is a command; otherwise the
    text goes to the current session, if one is chosen; otherwise the help."""
    target = next((sid for sid in map(reply_bridge.session_of_message, replied_to) if sid), None)
    if target:
        send_text_command(target, text, via_current=False)
        return
    if parse_command(text)[0] is None:
        current = reply_bridge.current_session()
        if current:
            reply_bridge.touch_current()
            send_text_command(current, text, via_current=True)
            return
    handle_command(text)


def send_text_command(session_id: str, text: str, via_current: bool) -> None:
    """Queue chat text as a command for the session and say where it went. The receipt
    is remembered too, so replying to it reaches the same session."""
    full = text.strip()
    text = full[:ANSWER_TEXT_LIMIT]
    if not text:
        return
    reply_bridge.queue_command(session_id, text, feishu_notify.read_config_field("allowed_open_id"))
    name, listed = session_list.session_label(session_id)
    if reply_bridge.command_waiter_alive(session_id):
        how = "它空闲着，马上开始处理；做完会把结果发到这里。"
    elif listed:
        how = "它正在忙：在它下一次调用工具时送达，最晚这一轮结束时送达。"
    else:
        how = "这个会话可能已经结束：指令已排队，12 小时内它再次运行时会收到。"
    note = ("\n（这是你选的当前会话：30 分钟内直接打的字都发给它；在会话列表上可以换或取消。）"
            if via_current else "")
    if len(full) > ANSWER_TEXT_LIMIT:
        note += f"\n（太长了：只发出了前 {ANSWER_TEXT_LIMIT} 字。）"
    log.info("chat text queued for session %s (%s)", session_id, "current" if via_current else "reply")
    receipt = feishu_notify.send_reply_text("Claude 远程回复 · 指令已发出",
                                            f"已发给「{short(name or session_id[:8], 60)}」：{how}{note}")
    reply_bridge.remember_message(receipt or "", session_id)


def handle_command(text: str) -> None:
    command, argument = parse_command(text)
    log.info("command %s", command or "unknown")
    if command == "sessions" and not argument:
        feishu_notify.send_reply_card(_sessions_card())
        return
    if command == "notify":
        feishu_notify.send_reply_text("Claude 远程回复 · 完成通知", set_notify(argument))
        return
    intro = "" if command == "help" else f"没有“{short(text, 30)}”这个命令。\n\n"
    feishu_notify.send_reply_text(HELP_TITLE, intro + HELP_BODY)


def set_notify(argument: str) -> str:
    """The "通知" command: show or change after how many minutes a turn's end is reported."""
    if argument.lower() in ("关", "关闭", "off", "0"):
        feishu_notify.write_config_field("notify_after_minutes", "0")
        return "已关闭：一轮结束时不再推送（飞书指令的结果和关注的会话照常推送）。发“通知 5”可以重新打开。"
    if argument:
        try:
            minutes = float(argument)
        except ValueError:
            minutes = -1
        if not 0 < minutes <= NOTIFY_MAX_MINUTES:
            return f"“{short(argument, 20)}”不是 {NOTIFY_MAX_MINUTES} 以内的正分钟数。例：通知 10；通知 关"
        feishu_notify.write_config_field("notify_after_minutes", f"{minutes:g}")
        return f"已设置：运行超过 {minutes:g} 分钟的一轮，结束时把最后的回复推送到这里。"
    current = feishu_notify.read_config_field("notify_after_minutes") or "5"
    state = "已关闭" if current in ("0", "0.0") else f"运行超过 {current} 分钟的一轮结束时推送"
    return f"现在：{state}。\n修改：通知 10（改成 10 分钟）；通知 关（关闭）。"


def refresh_sessions(message_id: str) -> None:
    """The refresh button: replace the clicked card with a fresh list."""
    card = _sessions_card()
    if message_id:
        feishu_notify.update_feishu_card(message_id, card)
    else:
        feishu_notify.send_reply_card(card)


def _closed_card(request: dict[str, Any], outcome: dict[str, Any]) -> dict[str, Any]:
    info = request.get("info", {})
    if outcome.get("kind") == "answer":
        answer = {"allow": "允许", "deny": "拒绝", "always": "允许并在本会话始终允许", "stop": "叫停"}.get(
            outcome.get("choice", ""), "") \
            if request.get("type") == "permission" else ""
        answer = answer or outcome.get("choice") or outcome.get("text") or ""
        return decision_card.result_card(info, f"已在飞书回复：{short(answer, 60)}", "green")
    return decision_card.result_card(info, "这个问题已经结束，不再接受回复", "grey")


def _waiter_gone(rid: str, request: dict[str, Any]) -> bool:
    return not reply_bridge.waiter_alive(rid, request)


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


def handle_sessions_click(open_id: str, message_id: str) -> Any:
    """The session list's refresh button. Built in the background: Feishu waits only
    3 s for this response, and `claude agents` can be slow right after a reboot."""
    allowed = feishu_notify.read_config_field("allowed_open_id")
    if not allowed or open_id != allowed:
        log.warning("rejected refresh from open_id=%s", open_id)
        return _response("error", "你没有权限查看会话")
    _in_background(refresh_sessions, message_id)
    return _response("info", "正在刷新…")


ANSWER_TEXT_LIMIT = 1000  # the card's text boxes allow 1000 characters


def handle_send(open_id: str, value: dict[str, Any], form_value: dict[str, Any], message_id: str) -> Any:
    """A send box on the session list: queue the command for that session's command
    waiter (claude_command_wait.py), which wakes Claude with it."""
    allowed = feishu_notify.read_config_field("allowed_open_id")
    if not allowed or open_id != allowed:
        log.warning("rejected command from open_id=%s", open_id)
        return _response("error", "你没有权限给会话发指令")
    session_id = str(value.get("sid") or "")
    if not reply_bridge.valid_id(session_id):
        return _response("error", "这个按钮不认识")
    text = str(form_value.get(str(value.get("field") or "")) or "").strip()[:ANSWER_TEXT_LIMIT]
    if not text:
        return _response("error", "指令是空的")
    waiting = reply_bridge.command_waiter_alive(session_id)
    pid = value.get("pid")
    if not waiting and isinstance(pid, int) and not reply_bridge.process_alive(pid):
        _in_background(refresh_sessions, message_id)
        return _response("error", "这个会话已经结束了，指令没有发出")
    reply_bridge.queue_command(session_id, text, open_id)
    log.info("queued a command for session %s (waiter %s)", session_id, "running" if waiting else "not running")
    _in_background(refresh_sessions, message_id)
    if waiting:
        return _response("success", "已送达，Claude 马上开始处理；做完会把结果发到这里")
    return _response("success", "已排队：这个会话现在没在等指令，它这一轮结束后就会收到")


def _allowed(open_id: str, what: str) -> bool:
    allowed = feishu_notify.read_config_field("allowed_open_id")
    if allowed and open_id == allowed:
        return True
    log.warning("rejected %s from open_id=%s", what, open_id)
    return False


def _detail_card(session_id: str, page: int) -> dict[str, Any]:
    try:
        return session_list.detail_card(session_id, page)
    except Exception as exc:  # a bug here must still answer the user
        log.exception("session detail failed")
        return session_list.error_card(f"插件内部错误（{type(exc).__name__}），详情见 listener.log。")


def show_detail(session_id: str, page: int, message_id: str, new_card: bool) -> None:
    card = _detail_card(session_id, page)
    if message_id and not new_card:
        feishu_notify.update_feishu_card(message_id, card)
    else:
        reply_bridge.remember_message(feishu_notify.send_reply_card(card), session_id)


def handle_detail(open_id: str, value: dict[str, Any], message_id: str) -> Any:
    """"查看" on the session list (a new detail card), or refresh / older / newer steps
    on a detail card (updated in place). Built in the background: claude agents is slow."""
    if not _allowed(open_id, "detail"):
        return _response("error", "你没有权限查看会话")
    session_id = str(value.get("sid") or "")
    page = value.get("page", 0)
    if not reply_bridge.valid_id(session_id) or not isinstance(page, int) or isinstance(page, bool) or page < 0:
        return _response("error", "这个按钮不认识")
    _in_background(show_detail, session_id, page, message_id, value.get("open") is True)
    return _response("info", "正在读取…")


def handle_follow(open_id: str, value: dict[str, Any], message_id: str) -> Any:
    if not _allowed(open_id, "follow"):
        return _response("error", "你没有权限关注会话")
    session_id = str(value.get("sid") or "")
    if not reply_bridge.valid_id(session_id) or not isinstance(value.get("on"), bool):
        return _response("error", "这个按钮不认识")
    reply_bridge.set_following(session_id, value["on"])
    log.info("session %s followed: %s", session_id, value["on"])
    _in_background(show_detail, session_id, 0, message_id, False)
    return _response("success", "已关注：它每轮结束都会推送到这里" if value["on"] else "已取消关注")


def send_latest_text(session_id: str) -> None:
    doc = session_list.latest_text_doc(session_id)
    if doc is None:
        feishu_notify.send_reply_text("Claude 远程回复", "找不到这个会话的记录文件。")
        return
    doc_id = reply_bridge.save_doc(doc)
    reply_bridge.remember_message(feishu_notify.send_reply_card(decision_card.paged_card(doc_id, doc, 0)), session_id)


def handle_fulltext(open_id: str, value: dict[str, Any]) -> Any:
    if not _allowed(open_id, "full text"):
        return _response("error", "你没有权限查看会话")
    session_id = str(value.get("sid") or "")
    if not reply_bridge.valid_id(session_id):
        return _response("error", "这个按钮不认识")
    _in_background(send_latest_text, session_id)
    return _response("info", "正在读取…")


def handle_page(open_id: str, value: dict[str, Any]) -> Any:
    """Page buttons of a long reply: the stored text is read back and the card replaced."""
    if not _allowed(open_id, "page"):
        return _response("error", "你没有权限查看")
    page = value.get("page")
    if not isinstance(page, int) or isinstance(page, bool) or page < 0:
        return _response("error", "这个按钮不认识")
    doc_id = str(value.get("doc") or "")
    doc = reply_bridge.load_doc(doc_id)
    if doc is None:
        return _response("error", "这段内容已经过期（只保存 7 天）")
    return _response("info", f"第 {page + 1} 页", decision_card.paged_card(doc_id, doc, page))


STOP_REQUESTED = ("已请求停止：它在下一次调用工具时停下（正在写长段文字或等子代理时，要等到它调用工具，"
                  "最晚这一轮结束）；正在等审批或回答的，替你答“停止”。停下后会通知你。")


def answer_cards_with_stop(session_id: str, open_id: str) -> None:
    """A session waiting on an approval or question card gets it answered "stop":
    claude_permission.py denies it and tells Claude to end the turn."""
    for request in reply_bridge.open_requests():
        if request.get("session_id") != session_id or request.get("type") not in ("permission", "ask"):
            continue
        if reply_bridge.save_answer(str(request.get("rid")), "stop", "", open_id):
            log.info("stop answered card %s", request.get("rid"))
            message_id = str(request.get("message_id") or "")
            if message_id:
                feishu_notify.update_feishu_card(
                    message_id, decision_card.result_card(request.get("info", {}), "已在飞书叫停这一轮", "grey"))


def handle_stop(open_id: str, value: dict[str, Any]) -> Any:
    """"停止": leave a stop request for claude_turn_control.py, which stops the next tool
    call, and answer any card the session waits on (in the background: the file scan
    must not delay this answer). An idle session (a stale card) has nothing to stop,
    and a request left on it would stop a later turn."""
    if not _allowed(open_id, "stop"):
        return _response("error", "你没有权限停止会话")
    session_id = str(value.get("sid") or "")
    if not reply_bridge.valid_id(session_id):
        return _response("error", "这个按钮不认识")
    if reply_bridge.command_waiter_alive(session_id):
        return _response("info", "它现在空闲着，没有正在跑的一轮，不用停止")
    reply_bridge.request_stop(session_id)
    log.info("stop requested for session %s", session_id)
    _in_background(answer_cards_with_stop, session_id, open_id)
    return _response("success", STOP_REQUESTED)


def handle_current(open_id: str, value: dict[str, Any], message_id: str) -> Any:
    """"设为当前会话": plain chat text goes to this session for 30 minutes."""
    if not _allowed(open_id, "current"):
        return _response("error", "你没有权限选择会话")
    session_id = str(value.get("sid") or "")
    if not reply_bridge.valid_id(session_id) or not isinstance(value.get("on"), bool):
        return _response("error", "这个按钮不认识")
    reply_bridge.set_current(session_id if value["on"] else None)
    log.info("current session: %s", session_id if value["on"] else "none")
    if value.get("card") == "detail":
        _in_background(show_detail, session_id, 0, message_id, False)
    elif value.get("card") == "list":
        _in_background(refresh_sessions, message_id)
    if value["on"]:
        return _response("success", "已设为当前会话：30 分钟内直接在聊天里打字都发给它")
    return _response("success", "已取消当前会话")


VIEW_KINDS = ("detail", "follow", "fulltext", "page", "stop", "current")


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
    if kind == "always":
        always = request.get("info", {}).get("always") or []
        if not request.get("info", {}).get("complete", True) or not always:
            return "这张卡片没有提供“始终允许”"
        return "always", "", f"已在飞书允许，并在本会话始终允许：{short('、'.join(always), 120)}{note}", "green"
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
        kind = value.get("kind")
        if kind in ("sessions", "send") or kind in VIEW_KINDS:
            message_id = str(getattr(getattr(event, "context", None), "open_message_id", "") or "")
            if kind == "send":
                return handle_send(open_id, value, action.form_value or {}, message_id)
            if kind == "detail":
                return handle_detail(open_id, value, message_id)
            if kind == "follow":
                return handle_follow(open_id, value, message_id)
            if kind == "fulltext":
                return handle_fulltext(open_id, value)
            if kind == "page":
                return handle_page(open_id, value)
            if kind == "stop":
                return handle_stop(open_id, value)
            if kind == "current":
                return handle_current(open_id, value, message_id)
            return handle_sessions_click(open_id, message_id)
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
LISTENER_FILES = ("feishu_listener.py", "paths.py", "reply_bridge.py", "feishu_notify.py", "decision_card.py",
                  "session_list.py", "session_detail.py")


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
