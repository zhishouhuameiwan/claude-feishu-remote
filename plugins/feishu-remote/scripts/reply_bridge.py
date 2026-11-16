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


def mark_redirected(session_id: str, ask: str) -> None:
    _write_json_atomic(_path(f"session-{_safe(session_id)}.redirect.json"), {"at": time.time(), "ask": ask})


def redirected_recently(session_id: str, ask: str, within: float) -> bool:
    """This very question of this session was redirected less than `within` seconds ago."""
    last = _read_json(_path(f"session-{_safe(session_id)}.redirect.json")) or {}
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
