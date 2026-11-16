#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feishu card JSON 2.0 builders for decision requests and account binding.

All text that comes from Claude or the user is rendered as plain_text, never
markdown, so links, mentions and formatting in it are shown literally.
"""

from __future__ import annotations

from typing import Any

from feishu_notify import limit_text

MAX_BUTTONS = 6
BUTTON_CHARS = 24


def short(text: str, max_chars: int) -> str:
    """One-line cut for buttons and status lines."""
    text = " ".join(str(text).split())
    return text if len(text) <= max_chars else text[: max_chars - 1] + "…"


def _text(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def _button(label: str, value: dict[str, Any], primary: bool) -> dict[str, Any]:
    return {
        "tag": "button",
        "type": "primary" if primary else "default",
        "text": {"tag": "plain_text", "content": label},
        "behaviors": [{"type": "callback", "value": value}],
    }


def _card(title: str, subtitle: str, template: str, elements: list[dict[str, Any]]) -> dict[str, Any]:
    header: dict[str, Any] = {
        "title": {"tag": "plain_text", "content": title},
        "template": template,
    }
    if subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": short(subtitle, 60)}
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": header,
        "body": {"elements": elements},
    }


def _intro(info: dict[str, Any]) -> list[dict[str, Any]]:
    """Project line, plus the note on a background subagent's request when there is one."""
    lines = [_text(f"项目：{limit_text(str(info.get('cwd', '')), 200)}")]
    if info.get("note"):
        lines.append(_text(str(info["note"])))
    return lines


def question_card(rid: str, info: dict[str, Any]) -> dict[str, Any]:
    """info: title, cwd, recap, ask, options (list of labels).
    Buttons carry only the option index; the listener maps it back to the full label."""
    options = list(info.get("options", []))[:MAX_BUTTONS]
    elements = [_text(line) for line in _info_lines(info)]
    if options:
        # Same 300-char cap as the text injected into Claude, so what you read is what it gets.
        listing = "\n".join(f"{i}. {limit_text(o, 300)}" for i, o in enumerate(options, 1))
        elements.append(_text(f"选项：\n{listing}"))
    for index, label in enumerate(options):
        value = {"rid": rid, "kind": "choice", "index": index}
        elements.append(_button(f"{index + 1}. {short(label, BUTTON_CHARS)}", value, index == 0))
    elements.append({
        "tag": "form",
        "name": "reply_form",
        "elements": [
            {
                "tag": "input",
                "name": "reply_text",
                "max_length": 1000,
                "placeholder": {"tag": "plain_text", "content": "也可以直接输入回复"},
            },
            {
                "tag": "button",
                "name": "reply_submit",
                "type": "default",
                "form_action_type": "submit",
                "text": {"tag": "plain_text", "content": "发送文字回复"},
                "behaviors": [{"type": "callback", "value": {"rid": rid, "kind": "text"}}],
            },
        ],
    })
    return _card("Claude Code · 等你决策", info.get("title", ""), "orange", elements)


APPROVAL_HEADER = "Claude Code · 等你审批"
DECISION_HEADER = "Claude Code · 等你决策"
ASK_HEADER = "Claude Code · 等你回答"


# Display caps for question cards: four questions of four options stay well under
# Feishu's 30 KB request limit. Answers always carry the full labels.
QUESTION_CHARS, LABEL_CHARS, DESCRIPTION_CHARS = 300, 100, 150


def _options_listing(question: dict[str, Any]) -> str:
    lines = []
    for number, option in enumerate(question.get("options", []), 1):
        line = f"{number}. {limit_text(str(option.get('label', '')), LABEL_CHARS)}"
        if option.get("description"):
            line += f" —— {limit_text(str(option['description']), DESCRIPTION_CHARS)}"
        lines.append(line)
    return "\n".join(lines)


def _question_title(index: int, total: int, question: dict[str, Any]) -> str:
    number = f"问题{index + 1}" if total > 1 else "问题"
    kind = "（可多选）" if question.get("multiSelect") else ""
    tag = f"【{limit_text(str(question['header']), 30)}】" if question.get("header") else ""
    return f"{number}{kind}：{tag}{limit_text(str(question.get('question', '')), QUESTION_CHARS)}"


def ask_card(rid: str, info: dict[str, Any]) -> dict[str, Any]:
    """Card for AskUserQuestion. info: title, cwd, questions (as Claude sent them).

    One single-choice question: a button per option (one tap) plus a free-text box.
    Otherwise one form: per question a dropdown (multi-select where allowed) and a
    free-text box, submitted together. Buttons and dropdowns carry only option
    numbers; the listener maps them back to the labels Claude offered."""
    questions = list(info.get("questions", []))
    elements: list[dict[str, Any]] = _intro(info)
    if len(questions) == 1 and not questions[0].get("multiSelect"):
        question = questions[0]
        elements += [_text(_question_title(0, 1, question)), _text(_options_listing(question))]
        for index, option in enumerate(question.get("options", [])):
            value = {"rid": rid, "kind": "choice", "index": index}
            elements.append(_button(f"{index + 1}. {short(option.get('label', ''), BUTTON_CHARS)}", value, index == 0))
        elements.append(_free_text_form(rid, "也可以直接输入回答（相当于选“其他”）", "发送文字回答"))
        return _card(ASK_HEADER, info.get("title", ""), "orange", elements)

    form: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        form += [_text(_question_title(index, len(questions), question)), _text(_options_listing(question))]
        form.append({
            "tag": "multi_select_static" if question.get("multiSelect") else "select_static",
            "name": f"q{index}",
            "required": False,
            "width": "fill",
            "placeholder": {"tag": "plain_text", "content": "选择"},
            "options": [{"text": {"tag": "plain_text", "content": f"{n + 1}. {short(o.get('label', ''), 60)}"},
                         "value": str(n)} for n, o in enumerate(question.get("options", []))],
            "behaviors": [{"type": "callback", "value": {"rid": rid, "kind": "select_change"}}],
        })
        form.append({
            "tag": "input",
            "name": f"q{index}_other",
            "required": False,
            "max_length": 1000,
            "placeholder": {"tag": "plain_text", "content": "或者填写其他回答（填了就以它为准）"},
        })
    form.append({
        "tag": "button",
        "name": "ask_submit",
        "type": "primary",
        "form_action_type": "submit",
        "text": {"tag": "plain_text", "content": "提交回答"},
        "behaviors": [{"type": "callback", "value": {"rid": rid, "kind": "form"}}],
    })
    elements.append({"tag": "form", "name": "ask_form", "elements": form})
    return _card(ASK_HEADER, info.get("title", ""), "orange", elements)


def _free_text_form(rid: str, placeholder: str, submit: str) -> dict[str, Any]:
    return {
        "tag": "form",
        "name": "reply_form",
        "elements": [
            {
                "tag": "input",
                "name": "reply_text",
                "max_length": 1000,
                "placeholder": {"tag": "plain_text", "content": placeholder},
            },
            {
                "tag": "button",
                "name": "reply_submit",
                "type": "default",
                "form_action_type": "submit",
                "text": {"tag": "plain_text", "content": submit},
                "behaviors": [{"type": "callback", "value": {"rid": rid, "kind": "text"}}],
            },
        ],
    }


def approval_card(rid: str, info: dict[str, Any]) -> dict[str, Any]:
    """info: title, cwd, tool, params (every parameter, already size-bounded),
    description (the model's own words), complete (False: params were cut).
    The parameters are what gets approved, so they come first and in full;
    when they could not be shown in full, Allow is not offered."""
    elements = _intro(info) + [_text(f"工具：{info.get('tool', '')}"),
                               _text(f"参数（批准的就是这些）：\n{info.get('params') or '（无）'}")]
    if info.get("description"):
        elements.append(_text(f"Claude 的说明（仅供参考）：{limit_text(str(info['description']), 300)}"))
    if info.get("complete", True):
        elements.append(_button("允许", {"rid": rid, "kind": "allow"}, True))
    else:
        elements.append(_text("参数太长，卡片里只显示了一部分。为安全起见这里不能允许，只能拒绝；要允许请回电脑上处理。"))
    deny = _button("拒绝", {"rid": rid, "kind": "deny"}, False)
    deny["type"] = "danger"
    elements.append(deny)
    elements.append({
        "tag": "form",
        "name": "deny_form",
        "elements": [
            {
                "tag": "input",
                "name": "reply_text",
                "max_length": 1000,
                "placeholder": {"tag": "plain_text", "content": "拒绝并告诉 Claude 改怎么做"},
            },
            {
                "tag": "button",
                "name": "deny_submit",
                "type": "default",
                "form_action_type": "submit",
                "text": {"tag": "plain_text", "content": "拒绝并发送说明"},
                "behaviors": [{"type": "callback", "value": {"rid": rid, "kind": "text"}}],
            },
        ],
    })
    return _card(APPROVAL_HEADER, info.get("title", ""), "red", elements)


SESSIONS_HEADER = "Claude Code · 活跃会话"


def sessions_card(summary: str, entries: list[dict[str, Any]], footer: str) -> dict[str, Any]:
    """Answer to the "会话" command. entries: {"text": one session, already formatted
    and size-bounded by session_list.py, "send": None or {"sid", "pid"}: a box for
    sending that session a command}. A button at the end refreshes this card."""
    elements = [_text(summary)]
    for index, entry in enumerate(entries):
        elements += [{"tag": "hr"}, _text(entry["text"])]
        if entry.get("send"):
            elements.append(_send_form(index, entry["send"]))
    if footer:
        elements += [{"tag": "hr"}, _text(footer)]
    elements.append(_button("刷新", {"kind": "sessions"}, True))
    return _card(SESSIONS_HEADER, "", "blue", elements)


def _send_form(index: int, target: dict[str, Any]) -> dict[str, Any]:
    """Names are unique in the card; the button tells the listener which box is its own."""
    field = f"cmd_text_{index}"
    return {
        "tag": "form",
        "name": f"send_form_{index}",
        "elements": [
            {
                "tag": "input",
                "name": field,
                "max_length": 1000,
                "placeholder": {"tag": "plain_text", "content": "给这个会话发指令（Claude 会照做，结果发回飞书）"},
            },
            {
                "tag": "button",
                "name": f"send_submit_{index}",
                "type": "default",
                "form_action_type": "submit",
                "text": {"tag": "plain_text", "content": "发送"},
                "behaviors": [{"type": "callback", "value": {"kind": "send", "field": field, **target}}],
            },
        ],
    }


def result_card(info: dict[str, Any], status: str, template: str) -> dict[str, Any]:
    """The request card with buttons removed and a status line added."""
    elements = [_text(line) for line in _info_lines(info)]
    elements.append(_text(f"▶ {status}"))
    return _card(info.get("header") or DECISION_HEADER, info.get("title", ""), template, elements)


_INFO_FIELDS = (
    ("cwd", "项目", 200),
    ("tool", "工具", 100),
    ("description", "说明", 300),
    ("content", "内容", 800),
    ("params", "参数", 800),
    ("recap", "进展", 400),
    ("ask", "问题", 300),
)


def _info_lines(info: dict[str, Any]) -> list[str]:
    return [
        f"{label}：{limit_text(str(info[key]), max_chars)}"
        for key, label, max_chars in _INFO_FIELDS
        if info.get(key)
    ]
