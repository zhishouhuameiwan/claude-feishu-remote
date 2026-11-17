#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
File protocol between the Feishu listener and the Claude hooks.

replies/<rid>.pending.json  written by the waiting hook; its mtime is the waiter's heartbeat
replies/<rid>.done.json     the single outcome, published with a hard link so the first
                            one wins: {"kind": "answer", ...} from the listener, or
                            {"kind": "closed", "reason": ...} when it ends elsewhere
replies/session-<sid>.txt   newest request id of a Claude session
replies/session-<sid>.prompt_at  time the user last typed in that session on the computer
replies/bind.json           token of the pending "bind my Feishu account" card

Commands the bound user sends to a session from Feishu (claude_command_wait.py):
replies/cmd-<sid>.<ns>-<hex>.json  one queued command; "." cannot occur in a session id,
                                   so "cmd-<sid>.*" never matches another session's files
replies/cmdwait-<sid>.json  the session's command waiter (newest one wins)
replies/session-<sid>.relay the running turn was started from Feishu: report its last reply

Viewing (session_detail.py, claude_command_wait.py):
replies/doc-<id>.json       a long reply, shown a page at a time on a card
replies/follow-<sid>.flag   the user follows this session: every turn's last reply goes to Feishu

Control while a turn runs (claude_turn_control.py, claude_stop_wait.py):
replies/stop-<sid>.flag     the user asked to stop the running turn; the next tool call claims it
replies/stopped-<sid>.<tool_use_id>.flag  that tool call stopped the turn; its async hook waits for commands
replies/msg-<message_id>.sid  the session a Feishu message is about, for replies to it
replies/current.json        the session plain chat text goes to, and when it was last used
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any, Optional

import paths

REPLY_DIR = paths.REPLY_DIR
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,80}")


def _is_safe(identifier: Any) -> bool:
    return isinstance(identifier, str) and _SAFE_ID.fullmatch(identifier) is not None


def _path(name: str) -> Path:
    return REPLY_DIR / name


def _safe(identifier: str) -> str:
    if not _is_safe(identifier):
        raise ValueError(f"unsafe id: {identifier!r}")
    return identifier


def _tmp_for(path: Path) -> Path:
    return path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")


REPLACE_ATTEMPTS = 20  # about 2 s in all


def _replace(tmp: Path, path: Path) -> None:
    """os.replace, retried. On Windows it fails with PermissionError while another
    process is replacing or reading the same file, e.g. two prompts of one session
    marking it at the same moment."""
    for attempt in range(1, REPLACE_ATTEMPTS + 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS:
                tmp.unlink(missing_ok=True)
                raise
            time.sleep(0.01 * attempt)


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    REPLY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _replace(tmp, path)


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def new_id() -> str:
    return secrets.token_hex(8)


# ---------------------------------------------------------------- requests

def create_request(rid: str, data: dict[str, Any], update_session: bool = True) -> None:
    """Write the pending file. update_session=False only refreshes it, so a slow
    older waiter cannot point the session back at itself."""
    _write_json_atomic(_path(f"{_safe(rid)}.pending.json"), {**data, "rid": rid})
    session_id = data.get("session_id")
    if update_session and _is_safe(session_id):
        _write_text_atomic(_path(f"session-{session_id}.txt"), rid)


def _write_text_atomic(path: Path, text: str) -> None:
    REPLY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_for(path)
    tmp.write_text(text, encoding="utf-8")
    _replace(tmp, path)


def get_request(rid: str) -> Optional[dict[str, Any]]:
    return _read_json(_path(f"{rid}.pending.json")) if _is_safe(rid) else None


def touch_request(rid: str) -> None:
    """Heartbeat: the waiting hook is still alive."""
    os.utime(_path(f"{_safe(rid)}.pending.json"))


def request_mtime(rid: str) -> float:
    try:
        return _path(f"{_safe(rid)}.pending.json").stat().st_mtime
    except (OSError, ValueError):
        return 0.0


def get_outcome(rid: str) -> Optional[dict[str, Any]]:
    return _read_json(_path(f"{rid}.done.json")) if _is_safe(rid) else None


def _finish(rid: str, outcome: dict[str, Any]) -> bool:
    """Publish the outcome unless another one already won. True if this one won.
    os.link never replaces an existing file, so exactly one publisher succeeds,
    and the outcome is complete the moment it becomes visible."""
    REPLY_DIR.mkdir(parents=True, exist_ok=True)
    final = _path(f"{_safe(rid)}.done.json")
    tmp = _tmp_for(final)
    tmp.write_text(json.dumps({**outcome, "at": time.time()}, ensure_ascii=False), encoding="utf-8")
    try:
        os.link(tmp, final)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def save_answer(rid: str, choice: str, text: str, open_id: str) -> bool:
    return _finish(rid, {"kind": "answer", "choice": choice, "text": text, "open_id": open_id})


def save_answers(rid: str, answers: dict[str, Any], summary: str, open_id: str) -> bool:
    """Answers to an AskUserQuestion card: question text -> label, free text or labels."""
    return _finish(rid, {"kind": "answer", "answers": answers, "choice": summary, "text": "",
                         "open_id": open_id})


def close_request(rid: str, reason: str) -> bool:
    return _finish(rid, {"kind": "closed", "reason": reason})


# Only for requests without a recorded waiter pid: a waiting hook refreshes its
# pending file every 2 s, so older than this means it is gone.
WAITER_STALE_SECONDS = 20


def waiter_alive(rid: str, request: dict[str, Any]) -> bool:
    """Is the hook that waits for this request still running? Process identity
    decides; a late heartbeat alone (sleep/resume, a busy machine) must not end a
    waiter that is still running."""
    pid = request.get("waiter_pid")
    if pid is not None:
        return process_alive(pid, request.get("waiter_created"))
    # Requests written before waiters recorded their pid: heartbeat only.
    return time.time() - request_mtime(rid) <= WAITER_STALE_SECONDS


def open_requests() -> list[dict[str, Any]]:
    """Requests still waiting for an answer: no outcome yet and their hook alive."""
    if not REPLY_DIR.exists():
        return []
    found = []
    for path in REPLY_DIR.glob("*.pending.json"):
        rid = path.name[: -len(".pending.json")]
        request = get_request(rid)
        if request is not None and get_outcome(rid) is None and waiter_alive(rid, request):
            found.append(request)
    return found


# ---------------------------------------------------------------- sessions

def session_request(session_id: str) -> Optional[str]:
    if not _is_safe(session_id):
        return None
    try:
        rid = _path(f"session-{session_id}.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return rid if _is_safe(rid) else None


def mark_prompt(session_id: str) -> None:
    if _is_safe(session_id):
        _write_text_atomic(_path(f"session-{session_id}.prompt_at"), repr(time.time()))


def prompt_time(session_id: str) -> float:
    return _read_time(f"session-{session_id}.prompt_at") if _is_safe(session_id) else 0.0


# claude_permission.py leaves prompts for these tools alone: plan approval and the
# desktop "next step" box keep their own reminders. (AskUserQuestion gets a question card.)
CARD_SKIPPED_TOOLS = frozenset({"ExitPlanMode", "mcp__ccd_turn__wrap_up"})


def mark_permission(session_id: str) -> None:
    """The approval hook is handling a permission prompt of this session."""
    if _is_safe(session_id):
        _write_text_atomic(_path(f"session-{session_id}.perm_at"), repr(time.time()))


def clear_permission(session_id: str) -> None:
    if _is_safe(session_id):
        _path(f"session-{session_id}.perm_at").unlink(missing_ok=True)


def permission_marked_recently(session_id: str, within: float = 120) -> bool:
    if not _is_safe(session_id):
        return False
    marked = _read_time(f"session-{session_id}.perm_at")
    return 0 <= time.time() - marked <= within


def valid_id(identifier: Any) -> bool:
    return _is_safe(identifier)


def mark_redirect_verdict(tool_use_id: str, redirected: bool) -> None:
    """Verdict of the redirect hook on one blocked wrap_up call; claude_decision.py waits for it."""
    _write_text_atomic(_path(f"call-{_safe(tool_use_id)}.redirect"), "deny" if redirected else "allow")


def redirect_verdict(tool_use_id: str) -> Optional[str]:
    try:
        return _path(f"call-{_safe(tool_use_id)}.redirect").read_text(encoding="utf-8")
    except OSError:  # not written (yet); an unsafe id raises ValueError
        return None


def mark_redirected(session_id: str, ask: str, name: str = "redirect") -> None:
    """name: one record per kind of redirect ("redirect": the next-step box;
    "offer_redirect": a sub-session offer), so they do not overwrite each other."""
    _write_json_atomic(_path(f"session-{_safe(session_id)}.{_safe(name)}.json"), {"at": time.time(), "ask": ask})


def redirected_recently(session_id: str, ask: str, within: float, name: str = "redirect") -> bool:
    """This very question of this session was redirected less than `within` seconds ago."""
    last = _read_json(_path(f"session-{_safe(session_id)}.{_safe(name)}.json")) or {}
    try:
        age = time.time() - float(last.get("at", 0))
    except (TypeError, ValueError):
        return False
    return last.get("ask") == ask and 0 <= age <= within


# ---------------------------------------------------------------- commands to a session

def queue_command(session_id: str, text: str, open_id: str) -> None:
    """Queue a command for the session; its command waiter delivers it (oldest first)."""
    name = f"cmd-{_safe(session_id)}.{time.time_ns():020d}-{secrets.token_hex(4)}.json"
    _write_json_atomic(_path(name), {"text": text, "at": time.time(), "open_id": open_id})


def queued_commands(session_id: str) -> int:
    if not _is_safe(session_id) or not REPLY_DIR.exists():
        return 0
    return len(list(REPLY_DIR.glob(f"cmd-{session_id}.*.json")))


# A command nobody picked up in this time (the session ended, or never finished its
# turn) is dropped rather than run days later in a resumed session.
COMMAND_MAX_AGE_SECONDS = 12 * 3600


def take_commands(session_id: str) -> list[dict[str, Any]]:
    """Claim and remove the session's queued commands, oldest first. A rename claims
    each file, so two waiters of one session can never deliver the same command."""
    if not REPLY_DIR.exists():
        return []
    taken = []
    oldest = time.time() - COMMAND_MAX_AGE_SECONDS
    for path in sorted(REPLY_DIR.glob(f"cmd-{_safe(session_id)}.*.json")):
        claimed = path.with_name(f"{path.name}.{os.getpid()}.taken")
        try:
            os.rename(path, claimed)
        except OSError:
            continue  # another waiter took it, or a writer still holds it: next round
        command = _read_json(claimed)
        claimed.unlink(missing_ok=True)
        if command is None or not str(command.get("text") or "").strip():
            continue
        try:
            fresh = float(command.get("at", 0)) >= oldest
        except (TypeError, ValueError):
            fresh = False
        if fresh:
            taken.append(command)
    return taken


def register_command_waiter(session_id: str, data: dict[str, Any]) -> None:
    _write_json_atomic(_path(f"cmdwait-{_safe(session_id)}.json"), data)


def command_waiter(session_id: str) -> Optional[dict[str, Any]]:
    """The session's registered command waiter (alive or not), None if there is none."""
    return _read_json(_path(f"cmdwait-{session_id}.json")) if _is_safe(session_id) else None


def command_waiter_alive(session_id: str) -> bool:
    waiter = command_waiter(session_id)
    return waiter is not None and process_alive(waiter.get("waiter_pid"), waiter.get("waiter_created"))


def mark_relay(session_id: str) -> None:
    _write_text_atomic(_path(f"session-{_safe(session_id)}.relay"), repr(time.time()))


def take_relay(session_id: str) -> Optional[float]:
    """Once after mark_relay, the time it was marked: a turn was started from Feishu.
    None when nothing is marked."""
    path = _path(f"session-{_safe(session_id)}.relay")
    marked = _read_time(path.name)
    try:
        path.unlink()
    except FileNotFoundError:
        return None
    return marked


def command_text(commands: list[dict[str, Any]], during_turn: bool = False) -> str:
    """The commands as Claude receives them: woken by the Stop hook, or added to a
    running turn at its next tool call (during_turn). The marker is the one the
    rule injected by claude_session_start.py explains."""
    texts = [str(c.get("text") or "").strip() for c in commands]
    if len(texts) == 1:
        body = texts[0]
    else:
        body = "\n".join(f"第 {n} 条：\n{text}" for n, text in enumerate(texts, 1))
    when = ("（这一轮进行中送达：先看它是否改变你正在做的事，再继续）" if during_turn else "")
    # Nothing follows the user's words, so quotes or newlines in them cannot blur where they end.
    return (
        f"【飞书指令】用户在已绑定的飞书账号上给这个会话发来指令{when}（见会话开头注入的“飞书远程回复规则”）。"
        "不需要批准的事直接照做；需要用户拍板的事（提交、推送、删除、在几个方向里选一个等），"
        "先用 AskUserQuestion 确认，用户可以直接在飞书上作答。这一轮结束时，你最后的回复会发回用户的飞书，"
        f"请把结果写在最后的回复里。\n指令原文（到本消息结尾为止）：\n{body}"
    )


# ---------------------------------------------------------------- stopping a running turn

# A stop is meant for the turn running when the user tapped "停止". Stop and
# UserPromptSubmit clear it; this limit is the backstop should both be missed
# (a turn that writes text for long without calling a tool still gets stopped).
STOP_MAX_AGE_SECONDS = 30 * 60
# Both PreToolUse hooks use this when a payload carries no tool_use_id.
NO_TOOL_USE_ID = "no-tool-use-id"


def request_stop(session_id: str) -> None:
    """The user asked to stop the session's running turn (claude_turn_control.py acts on it)."""
    _write_text_atomic(_path(f"stop-{_safe(session_id)}.flag"), repr(time.time()))


def stop_requested(session_id: str) -> bool:
    return _is_safe(session_id) and _path(f"stop-{session_id}.flag").is_file()


def claim_stop(session_id: str, tool_use_id: str) -> bool:
    """Move the stop request to the tool call that stops the turn. A rename, so of the
    tool calls running at once exactly one claims it. A request past its limit is dropped."""
    if not (_is_safe(session_id) and _is_safe(tool_use_id)):
        return False
    request = _path(f"stop-{session_id}.flag")
    if not 0 <= time.time() - _read_time(request.name) <= STOP_MAX_AGE_SECONDS:
        request.unlink(missing_ok=True)
        return False
    try:
        os.rename(request, _path(f"stopped-{session_id}.{tool_use_id}.flag"))
        return True
    except OSError:  # nothing requested, or another call claimed it
        return False


STOPPED_TURN_SECONDS = 120  # a backstop: the next prompt ends it in any case


def mark_turn_stopped(session_id: str) -> None:
    """The running turn was stopped: further tool calls of it (the rest of a parallel
    batch) are denied until the next prompt (mark_prompt, also run for woken turns)."""
    _write_text_atomic(_path(f"session-{_safe(session_id)}.stopped_at"), repr(time.time()))


def turn_stopped(session_id: str) -> bool:
    if not _is_safe(session_id):
        return False
    stopped = _read_time(f"session-{session_id}.stopped_at")
    return stopped > prompt_time(session_id) and 0 <= time.time() - stopped <= STOPPED_TURN_SECONDS


def clear_turn_stopped(session_id: str) -> None:
    if _is_safe(session_id) and not turn_stopped(session_id):
        _path(f"session-{session_id}.stopped_at").unlink(missing_ok=True)


def stopped_anywhere(session_id: str) -> bool:
    """Some tool call of the session stopped the turn and its waiter has not taken over yet."""
    return _is_safe(session_id) and REPLY_DIR.exists() and any(REPLY_DIR.glob(f"stopped-{session_id}.*.flag"))


def was_stopped(session_id: str, tool_use_id: str) -> bool:
    return (_is_safe(session_id) and _is_safe(tool_use_id)
            and _path(f"stopped-{session_id}.{tool_use_id}.flag").is_file())


def take_stopped(session_id: str, tool_use_id: str) -> None:
    _path(f"stopped-{_safe(session_id)}.{_safe(tool_use_id)}.flag").unlink(missing_ok=True)


def clear_stop(session_id: str) -> None:
    """A new turn began or the turn ended: a stop that no tool call reached is void."""
    if _is_safe(session_id):
        _path(f"stop-{session_id}.flag").unlink(missing_ok=True)


# ---------------------------------------------------------------- chat text to a session

def remember_message(message_id: str, session_id: str) -> None:
    """A Feishu message about this session: replying to it sends the reply there."""
    if _is_safe(message_id) and _is_safe(session_id):
        _write_text_atomic(_path(f"msg-{message_id}.sid"), session_id)


def session_of_message(message_id: str) -> Optional[str]:
    if not _is_safe(message_id):
        return None
    try:
        session_id = _path(f"msg-{message_id}.sid").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return session_id if _is_safe(session_id) else None


CURRENT_SESSION_SECONDS = 30 * 60


def set_current(session_id: Optional[str]) -> None:
    """The session plain chat text goes to; None clears it."""
    path = _path("current.json")
    if session_id is None:
        path.unlink(missing_ok=True)
    else:
        _write_json_atomic(path, {"sid": _safe(session_id), "at": time.time()})


def current_session() -> Optional[str]:
    """The current session, unless it went unused for CURRENT_SESSION_SECONDS."""
    data = _read_json(_path("current.json")) or {}
    session_id = data.get("sid")
    try:
        age = time.time() - float(data.get("at", 0))
    except (TypeError, ValueError):
        return None
    return session_id if _is_safe(session_id) and 0 <= age <= CURRENT_SESSION_SECONDS else None


def touch_current() -> None:
    session_id = current_session()
    if session_id:
        set_current(session_id)


# ---------------------------------------------------------------- long replies, followed sessions

MAX_DOC_CHARS = 200_000  # about 67 pages; more is not read on a phone


def save_doc(doc: dict[str, Any]) -> str:
    """Keep a reply too long for one card: the paged card reads it back by id.
    Removed by cleanup() with the other files after 7 days."""
    text = str(doc.get("text") or "")
    if len(text) > MAX_DOC_CHARS:
        doc = {**doc, "text": f"{text[:MAX_DOC_CHARS]}\n……（后面还有 {len(text) - MAX_DOC_CHARS} 字没有保存）"}
    doc_id = new_id()
    _write_json_atomic(_path(f"doc-{doc_id}.json"), doc)
    return doc_id


def load_doc(doc_id: str) -> Optional[dict[str, Any]]:
    return _read_json(_path(f"doc-{doc_id}.json")) if _is_safe(doc_id) else None


def set_following(session_id: str, on: bool) -> None:
    """Followed sessions send every turn's last reply to Feishu (claude_command_wait.py)."""
    path = _path(f"follow-{_safe(session_id)}.flag")
    if on:
        _write_text_atomic(path, repr(time.time()))
    else:
        path.unlink(missing_ok=True)


def following(session_id: str, touch: bool = False) -> bool:
    """touch: refresh the flag, so cleanup() keeps it while the session is in use."""
    if not _is_safe(session_id):
        return False
    path = _path(f"follow-{session_id}.flag")
    try:
        if touch:
            os.utime(path)
        return path.is_file()
    except OSError:
        return False


def _read_time(name: str) -> float:
    try:
        return float(_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0.0


# ---------------------------------------------------------------- processes

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _FileTime(ctypes.Structure):
    _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]


def process_created(pid: Any) -> Optional[int]:
    """Windows: creation time of a running process, None if it is not running.
    Together with the pid it identifies the process even after the pid is reused."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != _STILL_ACTIVE:
            return None
        created, exited, kernel, user = _FileTime(), _FileTime(), _FileTime(), _FileTime()
        if not kernel32.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited),
                                        ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return (created.high << 32) | created.low
    finally:
        kernel32.CloseHandle(handle)


def process_alive(pid: Any, created: Optional[int] = None) -> bool:
    """Is this process (and, when created is given, this very process) still running?"""
    now = process_created(pid)
    return now is not None and (created is None or now == created)


# ---------------------------------------------------------------- binding

BIND_CODE_MAX_AGE = 1800
BIND_CODE_MAX_TRIES = 5
_BIND_CODE = re.compile(r"\s*(\d{8})\s*")


def create_bind_code() -> str:
    """One-time 8-digit code that setup shows on the computer; the user sends it to the bot."""
    code = f"{secrets.randbelow(10 ** 8):08d}"
    _write_json_atomic(_path("bind.json"), {"code": code, "at": time.time(), "tries": 0})
    return code


def take_bind_code(text: str) -> bool:
    """True once for the current unexpired code; the code is then used up.
    Only 8-digit messages count as tries, and a few wrong ones void the code."""
    match = _BIND_CODE.fullmatch(text or "")
    data = _read_json(_path("bind.json"))
    if not match or not data or time.time() - float(data.get("at", 0)) > BIND_CODE_MAX_AGE:
        return False
    if match.group(1) != data.get("code"):
        tries = int(data.get("tries", 0)) + 1
        if tries >= BIND_CODE_MAX_TRIES:
            _path("bind.json").unlink(missing_ok=True)
        else:
            _write_json_atomic(_path("bind.json"), {**data, "tries": tries})
        return False
    try:
        _path("bind.json").unlink()
    except OSError:
        return False
    return True


def cleanup(max_age_days: float = 7) -> None:
    """Drop request files older than max_age_days."""
    if not REPLY_DIR.exists():
        return
    cutoff = time.time() - max_age_days * 86400
    for path in REPLY_DIR.iterdir():
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            continue
