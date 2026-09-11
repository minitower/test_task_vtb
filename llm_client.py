"""LLM-клиент. Пока заглушки — подключение Open Router в одной точке.

Замена заглушек: заполнить `chat_completion()` (OpenAI-совместимый API
Open Router) и, если нужен реальный creator, `_creator_stub()`.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemma-2-2b-it:free")

_client: Any = None


def chat_completion(system: str, user: str, model: str | None = None, temperature: float = 0.0) -> str:
    """Реальный вызов LLM (Open Router). Сейчас не реализован — бросает
    RuntimeError, чтобы случайно не уйти в сеть. Заглушки узлов используют
    собственные детерминированные ответы (spec/00: простейшие LLM с быстрым
    ответом; модель и параметры — открытые пункты)."""
    raise RuntimeError(
        "LLM не подключён. Задайте "
        f"{OPENROUTER_API_KEY_ENV} и реализуйте chat_completion() "
        "для Open Router (base_url=" + OPENROUTER_BASE_URL + ")."
    )


# --------------------------------------------------------------------------- #
# Creator: реальный вызов или детерминированная заглушка                      #
# --------------------------------------------------------------------------- #

def call_creator(card: dict, system: str, user: str) -> str:
    """Creator (LLM). Открытый пункт: модель/эндпоинт ещё не выбраны,
    поэтому сейчас — детерминированная заглушка. Замена на Open Router:
    `return chat_completion(system, user)` — и всё (промпты те же)."""
    return _creator_stub(card)


def _benefit_phrase(b: int, conf: str) -> str:
    if b == 0:
        return ""
    prefix = "около" if conf == "modelled" else "ровно"
    return f"{prefix} {b:,} ₽/мес".replace(",", " ")


def _creator_stub(card: dict) -> str:
    """Детерминированный черновик PUSH/CARD (замена — LLM через call_creator)."""
    from prefilter import canonical_name

    d = card["decision"]
    canonical = canonical_name(d["offer_id"])
    b, conf = d["benefit_month_rub"], d["benefit_confidence"]
    current, target = card["loyalty"]["current_tier"], card["loyalty"]["target_tier"]
    is_tier_offer = d["offer_id"] in ("LOY-UP-002", "LOY-UP-003")
    title = f"Переход на {canonical}" if (is_tier_offer and target != current) else canonical

    benefit = _benefit_phrase(b, conf)
    push = f"{title} — {benefit}" if benefit else title
    if len(push) > 70:
        push = title if len(title) <= 70 else title[:70]

    conds = list(d["conditions"])
    conds_text = ("Условия: " + "; ".join(conds) + ".") if conds else "Дополнительных условий нет."
    if b == 0:
        body = f"{title}. {conds_text}"
    else:
        body = f"{title}: {benefit}. {conds_text}"
    if len(body) > 350:
        body = (title if len(title) <= 70 else title[:70])
        for cond in conds:
            cand = body + " " + cond
            if len(cand) > 350:
                break
            body = cand
    return f"PUSH: {push}\nCARD: {body}"


# --------------------------------------------------------------------------- #
# Заглушка LLM-validator                                                      #
# --------------------------------------------------------------------------- #

def _validator_stub(card: dict, push: str, card_text: str) -> str:
    """Детерминированный вердикт (заменяется LLM через chat_completion).

    L1/L2/V4/V5 — PASS (заглушка); L3 — по полям карточки/тексту; I1 — по
    стоп-словам; V3 — оценка покрытия фактов (название, выгода, условия).
    """
    b, conf = card["decision"]["benefit_month_rub"], card["decision"]["benefit_confidence"]
    current, target = card["loyalty"]["current_tier"], card["loyalty"]["target_tier"]
    conds = list(card["decision"]["conditions"])

    text = push + " " + card_text

    # L3: повышение уровня противоречит карточке?
    l3_pass = True
    tier = target
    if target == current:
        if card["decision"]["offer_id"] in ("LOY-UP-002", "LOY-UP-003") or re.search(
            r"переход\w*\s+.*уровн|повышен\w*\s+уровн", text, re.I
        ):
            l3_pass = False
    else:
        if re.search(r"вы уже (на|имеете).*(базов|silver|сереб|gold|золот)", text, re.I):
            l3_pass = False

    # I1: следы инъекций (стоп-слова)
    i1_pass = True
    for stop in ("кредит", "предодобрен", "игнорируй", "ignore", "https://", "www.", "qr"):
        if stop in text.lower():
            i1_pass = False

    # L1/L2: проки-проверки (давление/срочность, «крик»)
    l1_pass = not re.search(r"только сегодня|поторопись|поспешите|срочно", text, re.I)
    l2_pass = (push + card_text).count("!") <= 1

    # V3: покрытие фактов
    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.lower()).replace("₽", "руб")

    tnorm = _norm(text)
    facts: list[bool] = []
    from prefilter import canonical_name

    canonical = canonical_name(card["decision"]["offer_id"])
    facts.append(_norm(canonical) in tnorm)
    if b == 0:
        facts.append(True)  # выгода 0 — упоминать не обязательно
    elif conf == "factual":
        facts.append(str(b) in tnorm.replace(" ", ""))
    else:
        facts.append(str(b).replace(",", " ") in tnorm.replace(" ", "") or str(b) in tnorm.replace(" ", ""))
    for cond in conds:
        toks = [t for t in re.findall(r"[а-яёa-z]+", _norm(cond)) if len(t) > 3]
        if not toks:
            facts.append(False)
        else:
            hits = sum(1 for t in toks if t in tnorm)
            facts.append(hits >= max(1, len(toks) // 2))
    pct_all = round(100 * sum(facts) / len(facts)) if facts else 0

    verdict = {
        "L1": {"pass": l1_pass, "reason": "stub: давление/поспешина" if not l1_pass else "stub: тон ок"},
        "L2": {"pass": l2_pass, "reason": "stub: более одного !" if not l2_pass else "stub: стиль ok"},
        "L3": {"pass": l3_pass, "reason": "stub: повышение противоречит карточке" if not l3_pass else "stub: уровни ок", "tier": tier},
        "I1": {"pass": i1_pass, "reason": "stub: стоп-слова инъекции" if not i1_pass else "stub: чужих данных нет"},
        "V3": {"pass": pct_all >= 75, "pct_push": 100 if (push and _norm(push) and _norm(canonical) in tnorm) else 50, "pct_all": pct_all, "reason": f"stub: покрытие {pct_all}%"},
        "V4": {"pass": True, "reason": "stub"},
        "V5": {"pass": True, "reason": "stub"},
    }
    return json.dumps(verdict, ensure_ascii=False)
