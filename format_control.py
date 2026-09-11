"""Формат-контроль ответа creator (Python, детерминированный) — spec/02.

Ровно две секции PUSH: и CARD: в этом порядке; PUSH ≤ 70, CARD ≤ 350;
без дисклеймера, без тэга <user_input>, без маркеров инъекций.
Провал → retry с шаблонным review (не отброс).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from prefilter import INJECTION_PATTERNS, load_rules

PUSH_MAX = 70
CARD_MAX = 350

_FORMAT_RE = re.compile(r"^PUSH: ([^\n\r]+)\r?\nCARD: ([^\n\r]+)$")

FORMAT_REVIEW = (
    "Формат не распознан. Шаблон: PUSH: <одна строка ≤ 70 символов>\n"
    "CARD: <один абзац ≤ 350 символов>; без JSON, без дисклеймера, без "
    "ссылок и тэгов."
)


@dataclass
class FormatResult:
    ok: bool
    push: str = ""
    card: str = ""
    violation: str = ""


def _disclaimer(offer_id: str) -> str:
    rules = load_rules()
    key = next(k for k in rules[offer_id] if k.lower().startswith("обязательный"))
    return rules[offer_id][key]


def check_format(response: str, offer_id: str) -> FormatResult:
    response = response.strip()
    if not response or response[0] == "{" or response.startswith("```"):
        return FormatResult(False, violation="ответ не распознан как блоки PUSH:/CARD: — " + FORMAT_REVIEW)

    match = _FORMAT_RE.match(response)
    if match is None:
        return FormatResult(False, violation="разбор секций не удался — " + FORMAT_REVIEW)
    push, card = match.group(1).strip(), match.group(2).strip()
    if not push or not card:
        return FormatResult(False, violation="пустая секция PUSH или CARD — " + FORMAT_REVIEW)
    if "\n" in push or "\n" in card:
        return FormatResult(False, violation="PUSH и CARD должны быть по одной строке/абзацу — " + FORMAT_REVIEW)
    if len(push) > PUSH_MAX:
        return FormatResult(False, f"PUSH длиннее {PUSH_MAX} символов (было {len(push)}) — " + FORMAT_REVIEW)
    if len(card) > CARD_MAX:
        return FormatResult(False, f"CARD длиннее {CARD_MAX} символов (было {len(card)}) — " + FORMAT_REVIEW)
    if response.count("PUSH:") != 1 or response.count("CARD:") != 1:
        return FormatResult(False, "секции PUSH:/CARD: обнаружены не ровно по разу — " + FORMAT_REVIEW)
    if (push + card).count("!") > 1:
        return FormatResult(False, "более одного восклицательного знака — " + FORMAT_REVIEW)
    if _disclaimer(offer_id) in response:
        return FormatResult(False, "дисклеймер уже присутствует в тексте creator (дубль) — " + FORMAT_REVIEW)
    if "<user_input>" in response or "</user_input>" in response:
        return FormatResult(False, "в ответе присутствует тег <user_input> — " + FORMAT_REVIEW)
    for name, pattern in INJECTION_PATTERNS.items():
        if pattern.search(push) or pattern.search(card):
            return FormatResult(False, f"в ответе найден маркер инъекции {name} — " + FORMAT_REVIEW)
    return FormatResult(True, push, card)
