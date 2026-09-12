"""LLM-клиент. Реальный вызов Open Router (OpenAI-совместимый API) — в
`chat_completion()`; заглушки (`_creator_stub`, `_validator_stub`)
оставлены для тестов/офлайн-прогонов, но узлы по умолчанию используют
реальный вызов.
"""

from __future__ import annotations

import json
import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-v4.1-flash")
REQUEST_TIMEOUT_S = 30

# Гиперпараметры LLM по variant creator'а (spec/05 §5). "default" — ближе
# к детерминированному, точному тексту (низкие temperature/top_p);
# "creative" (spec/05 §4) — более разнообразная формулировка, поэтому
# выше. reasoning_effort=None → reasoning отключён совсем (`enabled:
# false`): проверено на API — даже "low" effort у этой модели тратит
# сотни токенов на путаный chain-of-thought (вплоть до сомнений в том,
# какая инструкция настоящая), что и медленнее, и дороже, и на практике
# не «ниже», а не то, что просит spec/00 (приоритет — быстрый ответ).
_VARIANT_MODEL_PARAMS: dict[str, dict[str, float | str | None]] = {
    "default": {"temperature": 0.2, "top_p": 0.2, "reasoning_effort": None},
    "creative": {"temperature": 0.7, "top_p": 0.9, "reasoning_effort": None},
}

_session: requests.Session | None = None


def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def chat_completion(
    system: str,
    user: str,
    model: str | None = None,
    temperature: float = 0.0,
    top_p: float | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Вызов LLM через Open Router (spec/00: простейшая модель, быстрый
    ответ). `reasoning_effort` — уровень thinking модели ("low"/"high"/…,
    зависит от модели); без него reasoning отключается совсем
    (`enabled: false`) — так поступает validator (детерминированность
    важнее стиля). Любой сбой (нет ключа, сеть, неожиданный формат
    ответа, пустой ответ) → RuntimeError — по политике сбоев (spec/00
    «Любая ошибка LLM → отбрасываем») это ловится на уровне узла графа
    и уходит в retry/reject, а не чинится здесь."""
    api_key = os.environ.get(OPENROUTER_API_KEY_ENV)
    if not api_key:
        raise RuntimeError(f"{OPENROUTER_API_KEY_ENV} не задан (.env).")

    payload = {
        "model": model or OPENROUTER_MODEL,
        "temperature": temperature,
        "reasoning": {"effort": reasoning_effort} if reasoning_effort else {"enabled": False},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if top_p is not None:
        payload["top_p"] = top_p

    try:
        resp = _get_session().post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Open Router: ошибка запроса: {exc}") from exc

    try:
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Open Router: неожиданный формат ответа: {exc}") from exc

    content = (content or "").strip()
    if not content:
        raise RuntimeError("Open Router: пустой ответ модели")
    return content


# --------------------------------------------------------------------------- #
# Creator: реальный вызов Open Router                                        #
# --------------------------------------------------------------------------- #

def call_creator(card: dict, system: str, user: str, variant: str = "default") -> str:
    """Creator (LLM) — реальный вызов Open Router. Гиперпараметры берутся
    по `variant` из `_VARIANT_MODEL_PARAMS` (spec/05 §5). `_creator_stub(card)`
    остаётся доступной для офлайн-тестов (spec-test-runner)."""
    params = _VARIANT_MODEL_PARAMS.get(variant, _VARIANT_MODEL_PARAMS["default"])
    return chat_completion(
        system, user,
        temperature=params["temperature"],
        top_p=params["top_p"],
        reasoning_effort=params["reasoning_effort"],
    )


def _benefit_phrase(b: int, conf: str) -> str:
    if b == 0:
        return ""
    prefix = "около" if conf == "modelled" else "ровно"
    return f"{prefix} {b:,} ₽/мес".replace(",", " ")


def _creator_stub(card: dict) -> str:
    """Детерминированный черновик PUSH/CARD (замена — LLM через call_creator)."""
    from filters.prefilter import canonical_name

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
    from filters.prefilter import canonical_name

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
