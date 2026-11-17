#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Claude Code PermissionRequest hook: answer approval dialogs from Feishu.

Claude's question dialog (AskUserQuestion) arrives here too: it gets a question
card instead, and the answers go back as "allow" with updatedInput = the original
input plus "answers" (question text -> label, free text, or a list for
multi-select). On this path Claude Code only checks the tool's input schema, then
reads each answer by its exact question text and silently drops any other key, so
the question text must never be trimmed or rewritten; measured to work 2026-11-12.

Claude Code shows its dialog while this hook runs, and whichever answer comes
first wins (measured 2026-11-12). The hook:
  1. waits a short grace period, so nothing is sent when the user answers at
     the computer right away;
  2. sends a Feishu card with Allow / Deny / Deny-with-a-note. The card shows
     every tool parameter; when they do not fit, Allow is not offered;
  3. waits for the bound user's click, then prints the decision on stdout;
  4. stops quietly when the dialog was answered at the computer (Claude Code
     does not stop this hook then, so it watches the transcripts for the tool
     call's result), when Claude Code is gone, or after MAX_WAIT_SECONDS.

Subagent prompts (the payload has agent_id) get a note on the card and only
SUBAGENT_WAIT_SECONDS. A background subagent's prompt has no dialog at all:
Claude Code waits for this hook and denies once it ends without a decision, so
the subagent would otherwise be held for hours (2026-11-12: one waited the full
two hours). agent_id does not tell background from foreground subagents, whose
dialog stays open at the computer, so the wording on the card covers both.

Stdout carries only the decision JSON. Errors go to hooks.log; exit code is 0.
Once this hook has told the plain-text reminder to stand down (perm_at), every
failure sends a plain-text reminder itself, so a prompt is never left unannounced.
"""

from __future__ import annotations

import glob
import json
import os
import sys
import time
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import claude_subsession
import decision_card
import feishu_notify
import reply_bridge
from claude_ntfy import _as_dict, format_questions, log, read_payload, session_title
from feishu_notify import limit_text

# Decided before Claude Code's own "needs your permission" notification (~6 s after
# the dialog opens), so the plain-text safety net below is in place by then.
GRACE_SECONDS = float(os.environ.get("FEISHU_PERMISSION_GRACE_SECONDS") or 5)
POLL_SECONDS = 2
# Keep below the hook's "timeout" in hooks.json (43500 s). 12 hours: the user's call
# (2026-11-15; 2 hours was too short, cards timed out before they were seen).
MAX_WAIT_SECONDS = 12 * 3600
# Background subagent prompts: five minutes, the user's call (2026-11-12).
SUBAGENT_WAIT_SECONDS = float(os.environ.get("FEISHU_SUBAGENT_WAIT_SECONDS") or 300)
TAIL_BYTES = 2 * 1024 * 1024
# The call being approved is written to its transcript ~0.1 s before this hook
# starts. Candidates are collected until this moment, then frozen.
FREEZE_AFTER_SECONDS = 2.0
# What the card can show in full; beyond this, Allow is not offered.
FIELD_LIMIT = 2000
KEY_LIMIT = 100
# Feishu rejects card requests over 30 KB; the parameters get at most this many
# bytes, counted after the two layers of JSON escaping the request adds.
PARAMS_BYTE_LIMIT = 12000
# For these tools 'description' is the model's own summary; for any other tool it
# is real content (an issue body, say) and is shown and checked like the rest.
SUMMARY_DESCRIPTION_TOOLS = {"Bash", "PowerShell", "Agent", "Task"}
# Claude Code may rewrite paths before the hook sees them (relative -> absolute,
# / -> \), so paths are compared by file name only.
PATH_KEYS = ("file_path", "path", "notebook_path")
# Claude's question dialog: answered with a question card instead of Allow/Deny.
ASK_TOOL = "AskUserQuestion"

CLOSED_STATUS = {
    "answered_on_computer": "已在电脑上处理",
    "waiter_gone": "Claude 已不在等待，请回电脑上处理",
    "timeout": "等待超时，请回电脑上处理",
    "subagent_timeout": f"{SUBAGENT_WAIT_SECONDS / 60:g} 分钟内没有回复，已交回 Claude Code："
                        "后台子代理会被拒绝，电脑上有弹窗的请回电脑处理",
}


def _normalized(tool_input: dict[str, Any]) -> dict[str, str]:
    """Parameter values as compared between the hook payload and the transcript."""
    out = {}
    for key, value in tool_input.items():
        if key in PATH_KEYS and isinstance(value, str):
            out[key] = value.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1].lower()
        elif key == "questions" and isinstance(value, list):
            # AskUserQuestion: Claude Code fills schema defaults (multiSelect, kind)
            # into the hook's copy, so only the question texts are compared.
            out[key] = json.dumps([_as_dict(q).get("question") for q in value], ensure_ascii=False)
        else:
            out[key] = json.dumps(value, sort_keys=True, ensure_ascii=False)
    return out


class ToolUseWatch:
    """Notices when the tool call this prompt is about has been answered elsewhere.

    The payload does not say which call that is, so this works with every
    transcript call of the same tool whose parameters agree with the payload
    (paths by file name; the payload may carry extra defaulted parameters):

    * Candidates are the calls seen *without* a result during the first
      FREEZE_AFTER_SECONDS. The call being approved has no result until the
      user acts and is written ~0.1 s before this hook starts, so it is among
      them. A call first seen already answered is someone else's.
    * After that the set is frozen: later look-alikes never join, so another
      agent's identical command cannot close this card.
    * "Every candidate has a result" then proves the call was answered; with
      several look-alikes it only closes the card later, never wrongly.

    Where this could still be wrong (the call missing from the transcripts, or
    its parameters rewritten beyond file names), the caller keeps a safety net:
    it lets the plain-text reminder speak again when it decides "answered".

    Files are read incrementally: the tail of each file once, then only new lines."""

    def __init__(self, transcript_path: str, tool_name: str, tool_input: dict[str, Any],
                 started_at: float) -> None:
        self.transcript_path = transcript_path
        self.tool_name = tool_name
        self.hook_input = _normalized(tool_input)
        self.started_at = started_at
        self.files: dict[str, list[Any]] = {}  # path -> [offset, unfinished last line]
        self.candidates: set[str] = set()
        self.ignored: set[str] = set()
        self.results: set[str] = set()

    def _paths(self) -> list[str]:
        if not self.transcript_path:
            return []
        base = os.path.splitext(self.transcript_path)[0]
        nested = glob.glob(os.path.join(base, "subagents", "**", "*.jsonl"), recursive=True)
        return [self.transcript_path, *nested]

    def _same_call(self, transcript_input: Any) -> bool:
        if not isinstance(transcript_input, dict):
            return False
        theirs = _normalized(transcript_input)
        return set(theirs) <= set(self.hook_input) and all(
            self.hook_input[key] == value for key, value in theirs.items())

    def _new_entries(self, path: str) -> list[dict[str, Any]]:
        """Complete lines added since the last successful read of this file."""
        state = self.files.get(path)
        start = state[0] if state else max(0, os.path.getsize(path) - TAIL_BYTES)
        with open(path, "rb") as f:
            f.seek(start)
            chunk = f.read()
        lines = ((state[1] if state else b"") + chunk).split(b"\n")
        # Recorded only now, after the read worked; the unfinished last line waits for more.
        self.files[path] = [start + len(chunk), lines.pop()]
        entries = []
        for line in lines:
            try:
                entries.append(_as_dict(json.loads(line)))
            except ValueError:
                continue
        return entries

    def _frozen(self) -> bool:
        return time.time() - self.started_at >= FREEZE_AFTER_SECONDS

    def _ingest(self, entries: list[dict[str, Any]]) -> None:
        calls: list[str] = []
        for entry in entries:
            content = _as_dict(entry.get("message")).get("content")
            for block in content if isinstance(content, list) else []:
                block = _as_dict(block)
                if (block.get("type") == "tool_use" and block.get("name") == self.tool_name
                        and block.get("id") and self._same_call(block.get("input"))):
                    calls.append(str(block["id"]))
                elif block.get("type") == "tool_result":
                    self.results.add(str(block.get("tool_use_id")))
        if self._frozen():
            return
        for tid in calls:
            if tid in self.ignored or tid in self.candidates:
                continue
            if tid in self.results:
                self.ignored.add(tid)  # never seen unanswered: not the call being approved
            else:
                self.candidates.add(tid)

    def resolved(self) -> bool:
        """True once the call being approved has a result (answered at the computer or interrupted)."""
        try:
            for path in self._paths():
                if os.path.isfile(path):
                    self._ingest(self._new_entries(path))
        except OSError as exc:  # e.g. a scanner holding the file: just look again later
            log(f"[Feishu] transcript read failed, retrying: {exc}")
            return False
        if not self._frozen():
            return False
        return bool(self.candidates) and self.candidates <= self.results


def _shown(text: str) -> str:
    """Head and tail of an over-long parameter, with how much is left out."""
    return f"{text[:1200]}\n…（中间省略 {len(text) - 1600} 字）…\n{text[-400:]}"


def _request_bytes(text: str) -> int:
    """Bytes this text takes in the send request: JSON inside JSON inside the body."""
    once = json.dumps(text, ensure_ascii=False)
    return len(json.dumps(once, ensure_ascii=False).encode("utf-8", "replace"))


# "本会话始终允许同类操作" offers only these kinds of Claude Code's own suggestions
# (permission_suggestions), always with destination "session". Measured 2026-11-16 in
# a desktop session: addRules and addDirectories take effect, setMode does not.
MAX_ALWAYS = 5  # lines on the card: rules and directories together
ALWAYS_LINE_CHARS = 300


def _text_value(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value.strip() else None


def _rebuilt(suggestion: dict[str, Any]) -> Optional[dict[str, Any]]:
    """One suggestion rebuilt from its known fields only, for this session; None if it
    is not an allow rule or directory list in the expected shape."""
    kind = suggestion.get("type")
    if kind == "addDirectories":
        directories = suggestion.get("directories")
        if not isinstance(directories, list) or not directories or not all(map(_text_value, directories)):
            return None
        return {"type": kind, "directories": list(directories), "destination": "session"}
    rules = suggestion.get("rules")
    if kind != "addRules" or suggestion.get("behavior") != "allow" or not isinstance(rules, list) or not rules:
        return None
    rebuilt_rules = []
    for rule in rules:
        rule = _as_dict(rule)
        content = rule.get("ruleContent")
        if not _text_value(rule.get("toolName")) or not (content is None or isinstance(content, str)):
            return None
        rebuilt_rules.append({"toolName": rule["toolName"], **({"ruleContent": content} if content else {})})
    return {"type": kind, "rules": rebuilt_rules, "behavior": "allow", "destination": "session"}


def describe_permissions(permissions: list[dict[str, Any]]) -> list[str]:
    """What "始终允许" adds, one line each, written the way Claude Code writes rules."""
    lines = []
    for permission in permissions:
        if permission["type"] == "addRules":
            lines += [f"{r['toolName']}({r['ruleContent']})" if r.get("ruleContent")
                      else f"{r['toolName']}（整个工具：它的所有调用）" for r in permission["rules"]]
        else:
            lines += [f"可访问的目录：{directory}" for directory in permission["directories"]]
    return lines


def session_permissions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """What "始终允许" applies: Claude Code's own allow rules and directories, each
    forced to this session (never a settings file), rebuilt from the hook's own
    payload, so nothing a card click carries can add to them. Offered only when the
    card can show every one of them in full: what is shown is exactly what is added."""
    suggestions = payload.get("permission_suggestions")
    usable = [p for p in (_rebuilt(_as_dict(s)) for s in (suggestions if isinstance(suggestions, list) else []))
              if p is not None]
    lines = describe_permissions(usable)
    if len(lines) > MAX_ALWAYS or any(len(line) > ALWAYS_LINE_CHARS for line in lines):
        return []
    return usable


def build_info(payload: dict[str, Any]) -> dict[str, Any]:
    """Card content. 'params' lists every tool parameter (for Bash-like tools all
    but the model's own description); 'complete' is False when anything was cut."""
    tool_name = str(payload.get("tool_name") or "")
    tool_input = _as_dict(payload.get("tool_input"))
    summary_only = tool_name in SUMMARY_DESCRIPTION_TOOLS
    params, used, complete, omitted = [], 0, True, 0
    for key, value in tool_input.items():
        if key == "description" and summary_only:
            continue
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        if len(text) > FIELD_LIMIT:
            complete, text = False, _shown(text)
        if len(str(key)) > KEY_LIMIT:
            complete = False
        line = f"{limit_text(str(key), KEY_LIMIT)}：{text}"
        size = _request_bytes(line)
        if omitted or used + size > PARAMS_BYTE_LIMIT:
            omitted += 1  # once one does not fit, the rest are only counted
            continue
        used += size
        params.append(line)
    if omitted:
        complete = False
        params.append(f"（还有 {omitted} 个参数卡片放不下，已省略）")
    # A sub-session request says what it is in plain words; its parameters stay as they are.
    subsession = tool_name == claude_subsession.TOOL
    return {
        "header": decision_card.SUBSESSION_HEADER if subsession else decision_card.APPROVAL_HEADER,
        "summary": claude_subsession.summary_lines(tool_input) if subsession else [],
        "title": session_title(str(payload.get("transcript_path") or "")),
        "cwd": str(payload.get("cwd") or ""),
        "tool": tool_name,
        "description": str(tool_input.get("description") or "") if summary_only else "",
        "params": "\n".join(params),
        "complete": complete,
        # Offered only when every parameter is shown, like Allow itself.
        "always": describe_permissions(session_permissions(payload)) if complete else [],
    }


STOP_MESSAGE = ("用户在飞书上叫停了这一轮，这次操作没有执行。请直接结束这一轮，不要再调用工具；"
                "只停这一轮，之后用户在电脑上或飞书上发来的新指令照常处理。")


def decision_output(outcome: dict[str, Any], permissions: Optional[list[dict[str, Any]]] = None) -> bytes:
    """permissions: session_permissions() of this prompt, applied when the user chose "always"."""
    if outcome.get("choice") == "allow":
        decision: dict[str, Any] = {"behavior": "allow"}
    elif outcome.get("choice") == "always":
        decision = {"behavior": "allow", **({"updatedPermissions": permissions} if permissions else {})}
    elif outcome.get("choice") == "stop":
        # No "interrupt": an interrupted turn fires no Stop hook (measured 2026-11-16), so
        # the session would stop taking Feishu commands. The listener also leaves a stop
        # request, so a further tool call is stopped by claude_turn_control.py.
        decision = {"behavior": "deny", "message": STOP_MESSAGE}
    else:
        message = "用户在飞书上拒绝了这次操作。"
        if outcome.get("text"):
            # The rule injected by claude_session_start.py quotes this wording; the
            # note comes last so nothing in it can blur where it ends.
            message += ("按会话开头注入的“飞书远程回复规则”，下面的说明等同于用户在本对话里直接输入。"
                        f"用户的说明（原话，到结尾为止）：\n{limit_text(str(outcome['text']), 1000)}")
        decision = {"behavior": "deny", "message": message}
    body = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}
    return json.dumps(body).encode("ascii")  # ASCII-escaped: safe for any console encoding


def wait_for_outcome(rid: str, watch: ToolUseWatch, started_at: float,
                     parent: tuple[int, Optional[int]], subagent: bool) -> dict[str, Any]:
    limit, timeout_reason = ((SUBAGENT_WAIT_SECONDS, "subagent_timeout") if subagent
                             else (MAX_WAIT_SECONDS, "timeout"))
    while True:
        try:
            outcome = reply_bridge.get_outcome(rid)
            if outcome is not None:
                return outcome
            reason = None
            if watch.resolved():
                reason = "answered_on_computer"
            elif not reply_bridge.process_alive(*parent):
                reason = "waiter_gone"
            elif time.time() - started_at > limit:
                reason = timeout_reason
            if reason:
                reply_bridge.close_request(rid, reason)  # next round reads whichever outcome won
            else:
                reply_bridge.touch_request(rid)
        except OSError as exc:
            log(f"[Feishu] approval waiter file access failed, retrying: {exc}")
        time.sleep(POLL_SECONDS)


def send_text_reminder(payload: dict[str, Any]) -> None:
    """Plain-text reminder built straight from the payload (used when the card path fails)."""
    tool_name = str(payload.get("tool_name") or "")
    tool_input = _as_dict(payload.get("tool_input"))
    lines = [f"项目: {payload.get('cwd') or ''}"]
    if tool_name == ASK_TOOL:
        title, lines = "[?] Claude Code · 等你回复", lines + format_questions(tool_input)
    else:
        title = "[!] Claude Code · 审批请求"
        lines.append(f"工具: {tool_name}")
        lines += [f"参数: {limit_text(json.dumps(tool_input, ensure_ascii=False), 1500)}"] if tool_input else []
    try:
        feishu_notify.send_feishu_message(title, "\n".join(lines))
    except Exception as exc:
        log(f"[Feishu] approval text reminder failed: {exc}")


def build_ask_info(payload: dict[str, Any]) -> dict[str, Any]:
    questions = [
        {
            "question": str(_as_dict(q).get("question") or ""),
            "header": str(_as_dict(q).get("header") or ""),
            "multiSelect": _as_dict(q).get("multiSelect") is True,
            "options": [
                {"label": str(_as_dict(o).get("label") or ""),
                 "description": str(_as_dict(o).get("description") or "")}
                for o in _as_dict(q).get("options") or []
            ],
        }
        for q in _as_dict(payload.get("tool_input")).get("questions") or []
    ]
    return {
        "header": decision_card.ASK_HEADER,
        "title": session_title(str(payload.get("transcript_path") or "")),
        "cwd": str(payload.get("cwd") or ""),
        "questions": questions,
        "ask": " / ".join(q["question"] for q in questions),  # shown on the closed card
    }


def answer_output(tool_input: dict[str, Any], outcome: dict[str, Any]) -> bytes:
    """Allow, with the answers filled in the way Claude Code's question dialog does it."""
    decision = {"behavior": "allow", "updatedInput": {**tool_input, "answers": outcome.get("answers", {})}}
    body = {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}
    return json.dumps(body).encode("ascii")


def run(payload: dict[str, Any], started_at: float) -> int:
    parent_pid = os.getppid()  # claude.exe for hooks started by Claude Code
    parent = (parent_pid, reply_bridge.process_created(parent_pid))
    watch = ToolUseWatch(str(payload.get("transcript_path") or ""), str(payload.get("tool_name")),
                         _as_dict(payload.get("tool_input")), started_at)

    while time.time() - started_at < GRACE_SECONDS:
        if watch.resolved():
            # Answered at the computer right away, so no card. Safety net in case
            # that judgement is wrong: let the plain-text reminder speak again.
            # Claude Code raises its "needs your permission" notification only
            # while the dialog is still open, so a correct judgement stays quiet.
            reply_bridge.clear_permission(str(payload.get("session_id") or ""))
            return 0
        time.sleep(0.5)

    asking = payload.get("tool_name") == ASK_TOOL
    subagent = bool(payload.get("agent_id"))
    info = build_ask_info(payload) if asking else build_info(payload)
    if subagent:
        agent = limit_text(str(payload.get("agent_type") or "未知"), 60)
        log(f"[Feishu] subagent prompt ({agent}): waiting at most {SUBAGENT_WAIT_SECONDS:g}s")
        info = {**info, "note": f"来自子代理（{agent}）：飞书这边最多等 {SUBAGENT_WAIT_SECONDS / 60:g} 分钟。"
                                "后台子代理在电脑上没有弹窗，过时会被拒绝。"}
    rid = reply_bridge.new_id()
    base = {"type": "ask" if asking else "permission", "session_id": str(payload.get("session_id") or ""),
            "info": info, "message_id": "", "waiter_pid": os.getpid(),
            "waiter_created": reply_bridge.process_created(os.getpid()), "started_at": started_at}
    reply_bridge.create_request(rid, base, update_session=False)
    card = decision_card.ask_card(rid, info) if asking else decision_card.approval_card(rid, info)
    try:
        message_id = feishu_notify.send_feishu_card(card)
    except Exception as exc:
        log(f"[Feishu] approval card send failed, falling back to text: {exc}")
        reply_bridge.close_request(rid, "send_failed")
        send_text_reminder(payload)
        return 0
    if message_id is None:  # notifications switched off
        reply_bridge.close_request(rid, "disabled")
        return 0
    reply_bridge.create_request(rid, {**base, "message_id": message_id}, update_session=False)
    reply_bridge.remember_message(message_id, base["session_id"])  # a reply to the card goes to this session

    outcome = wait_for_outcome(rid, watch, started_at, parent, subagent)
    if outcome.get("kind") == "answer":
        output = (answer_output(_as_dict(payload.get("tool_input")), outcome)
                  if asking and outcome.get("choice") != "stop"
                  else decision_output(outcome, session_permissions(payload)))
        sys.stdout.buffer.write(output)
        sys.stdout.flush()
        return 0
    status = CLOSED_STATUS.get(str(outcome.get("reason")), "这个审批已经结束")
    try:
        feishu_notify.update_feishu_card(message_id, decision_card.result_card(info, status, "grey"))
    except Exception as exc:
        log(f"[Feishu] approval card update failed: {exc}")
    return 0


def main() -> int:
    started_at = time.time()
    payload = read_payload()
    tool_name = str(payload.get("tool_name") or "")
    if not tool_name or tool_name in reply_bridge.CARD_SKIPPED_TOOLS or not feishu_notify.ready():
        return 0  # not ready: setup.ps1 not run yet, Claude Code shows its own dialog as usual
    try:
        reply_bridge.mark_permission(str(payload.get("session_id") or ""))  # text reminder stands down
    except OSError as exc:  # the marker only silences a duplicate reminder; never let it stop the card
        log(f"[Feishu] could not mark the prompt, continuing: {exc}")
    try:
        return run(payload, started_at)
    except Exception as exc:  # the text reminder was told to stand down: announce it ourselves
        log(f"[Feishu] approval hook failed, sending text reminder: {exc}")
        send_text_reminder(payload)
        return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception as exc:  # never let a broken reminder disturb the approval
        log(f"[Feishu] approval hook failed: {exc}")
        code = 0
    raise SystemExit(code)
