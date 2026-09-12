"""Постобработка финального ответа (Python, не LLM) — spec/04.

Дисклеймер (idempotent-вставка кодом в конец CARD), финальный контроль
D1 + перескан на инъекции/служебные поля/тег, финальные длины.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from filters.prefilter import disclaimer_of
from filters.format_control import CARD_MAX, PUSH_MAX


@dataclass
class PostResult:
    ok: bool
    push: str = ""
    card: str = ""
    failure: str = ""


# Служебные поля, которые не могут попасть в финальный текст (spec/04 §2).
_SERVICE_FIELDS = (
    "client_ref", "offer_id", "flags", "loyalty", "tenure_months",
    "segment", "benefit_month_rub", "benefit_confidence",
    "vulnerable_client", "no_marketing_consent", "debt_collection",
    "gap_criteria", "conditions",
)


def run_postprocessing(push: str, card_text: str, offer_id: str) -> PostResult:
    text = card_text.strip()
    if len(push.strip()) > PUSH_MAX or len(text) > CARD_MAX:
        return PostResult(False, "", "", "финальные длины нарушены")

    disclaimer = disclaimer_of(offer_id)
    if disclaimer in text:
        # Idempotent-вставка: дисклеймер уже есть (дубль не должен пройтись
        # формат-контроль, но на всякий случай не дублируем).
        final = text
    else:
        final = text + "\n" + disclaimer

    # Финальный скан на тег и служебные поля.
    if "<user_input>" in push + final or "</user_input>" in push + final:
        return PostResult(False, push, final, "тэг <user_input> в финальном тексте")
    for field in _SERVICE_FIELDS:
        if re.search(rf"\b{re.escape(field)}\b", push + final, re.I):
            return PostResult(False, push, final, f"служебное поле {field!r} в финальном тексте")

    # D1: посимвольное дословное вхождение дисклеймера в финальном тексте.
    if disclaimer not in final:
        return PostResult(False, push, final, "D1: дисклеймер не найден посимвольно")

    return PostResult(True, push.strip(), final)
