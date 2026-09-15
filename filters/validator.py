"""Validator: Python-проверки (B1, F1, F2, R1, P1) + LLM (L1, L2, L3, I1, V3, V4, V5).

spec/03. LLM — заглушка: её ответ заменяется позже на Open Router вызов
(одна функция `LLMValidator`). Python-коды считаются кодом и не зависят
от LLM-ответа; LLM используется только для пунктов, которые Python не может
решить детерминированно (тон, стиль, «повышался ли уровень», следы инъекций
в тексте, покрытие V3).

Порог: PASS = (B1 ∧ F1 ∧ F2 ∧ R1 ∧ D1 ∧ P1 ∧ I1 ∧ L3 ∧ T1 ∧ T2) ∧ (≥ 2 из {L1, L2, V3}).
D1 считается в постобработке (spec/04) — здесь, на тексте creator без
дисклеймера, он не проверяется; на retry-пути его нет по определению
(format_control).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from filters.prefilter import canonical_name, load_rules, load_tier_rank, normalize_numbers


@dataclass
class ItemResult:
    code: str
    blocking: bool
    pass_: bool
    reason: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class ValidatorResult:
    items: list[ItemResult] = field(default_factory=list)
    llm_raw: str = ""
    # Код инфраструктурного сбоя LLM-вызова validator'а (spec/03 §5):
    # "LLM_UNREACHABLE" — retry_gate пропускается (agent.py), в отличие
    # от обычного "нераспознан ответ" (пусто).
    llm_error_code: str = ""

    @property
    def items_d(self) -> dict[str, ItemResult]:
        return {i.code: i for i in self.items}

    @property
    def pass_(self) -> bool:
        d = self.items_d
        mandatory = [c for c in ("B1", "F1", "F2", "R1", "P1", "I1", "L3", "T1", "T2") if c in d]
        if not all(d[c].pass_ for c in mandatory):
            return False
        important = [c for c in ("L1", "L2", "V3") if c in d]
        return sum(1 for c in important if d[c].pass_) >= 2

    def failures(self) -> list[dict]:
        return [
            {"code": i.code, "blocking": i.blocking, "reason": i.reason}
            for i in self.items if not i.pass_
        ]


# --------------------------------------------------------------------------- #
# Python-проверки (детерминированные)                                         #
# --------------------------------------------------------------------------- #

_TIER_MENTION = {
    "base": re.compile(r"\bbase\b|базов\w*", re.I),
    "silver": re.compile(r"\bsilver\b|сильвер|серебр\w*", re.I),
    "gold": re.compile(r"\bgold\b|голд|золот\w*", re.I),
}


def _check_b1(card: dict, push: str, card_text: str) -> ItemResult:
    """Блокировка повышения: current == target и offer из линейки уровня → FAIL."""
    text = push + " " + card_text
    offer_id = card["decision"]["offer_id"]
    current = card["loyalty"]["current_tier"]
    target = card["loyalty"]["target_tier"]
    if target != current:
        # Легитимный переход (target старше current, проверено префильтром).
        return ItemResult("B1", True, True, "legit transition")
    if offer_id in ("LOY-UP-002", "LOY-UP-003"):
        return ItemResult("B1", True, False, f"current == target ({current}) и offer из линейки уровня")
    # Любое предложение повышения при равных уровнях — нарушение.
    upgrade_words = re.search(r"переход|повышен\w*\s+уровн", text, re.I)
    if target == current and upgrade_words:
        return ItemResult("B1", True, False, "предложение перехода при равных уровнях")
    return ItemResult("B1", True, True)


def _extract_all_numbers(text: str) -> list[int]:
    return [int(m) for m in re.findall(r"\d+", normalize_numbers(text))]


def _check_f1(card: dict, push: str, card_text: str) -> ItemResult:
    """Факт-чек (spec/03 F1): числа в тексте сверяются с карточкой.

    Легитимные числа: benefit_month_rub, числа из conditions, пороги уровней
    лояльности (loyalty_level.csv) для текущего/целевого уровня (R1 допускает
    их в тексте). Число из текста, не совпадающее ни с одним — FAIL.
    """
    text = push + " " + card_text
    legit = set()
    b = card["decision"]["benefit_month_rub"]
    if b > 0:
        legit.add(b)
    legit.update(_extract_all_numbers(" ".join(card["decision"]["conditions"])))
    tier_thresholds = {"base": (), "silver": (40000,), "gold": (100000,)}
    for tier in (card["loyalty"]["current_tier"], card["loyalty"]["target_tier"]):
        legit.update(tier_thresholds.get(tier, ()))
    text_nums = set(_extract_all_numbers(text))
    extra = text_nums - legit
    if extra:
        return ItemResult("F1", True, False, f"числа в тексте, не совпадающие с карточкой: {extra}")
    return ItemResult("F1", True, True)


_COND_MARKER_RE = re.compile(r"услови\w*", re.I)
# Слово-отрицание может стоять не сразу после «условия» («условий в данных
# нет», «дополнительные условия не указаны», «условий по нему не заявлено») —
# поэтому ищем его в любом месте всего предложения-клаузы, а не только сразу
# после маркера (баг: раньше матчился только вариант «условия нет» вплотную).
_COND_NEGATION_RE = re.compile(
    r"нет\b|не\s+предусмотр\w*|отсутству\w*|никаких|не\s+требу\w*"
    r"|не\s+указан\w*|не\s+заявлен\w*|не\s+установлен\w*|не\s+описан\w*",
    re.I,
)


def _key_tokens(s: str) -> set[str]:
    return {t for t in re.findall(r"[а-яёa-z]+", s.lower()) if len(t) > 3}


def _check_f2(card: dict, push: str, card_text: str) -> ItemResult:
    """Каждое условие должно быть отражено; не выдумывать условия (spec/03 F2).

    Две части проверки:
    1) каждое условие из `conditions` должно быть отражено в тексте
       (ключевые слова присутствуют) — иначе FAIL («условие не отражено»);
    2) в тексте не должно быть условий, которых нет в карточке, включая
       случай `conditions == []` — иначе цикл по пустому списку никогда
       не находит нарушений и функция ошибочно проходит (баг: FAIL не
       детектировался при пустых `conditions`). Ищем в тексте маркеры
       вида «услови…»; отрицание («условий нет», «условия не указаны»,
       «условий по нему не заявлено») ищем по всей клаузе до конца
       предложения, а не только вплотную к маркеру — в естественной речи
       слово-отрицание почти всегда стоит дальше в предложении. Если
       отрицания нет — сверяем упомянутую формулировку с реальными
       условиями карточки: несовпадение = придуманное условие → FAIL.
    """
    text = push + " " + card_text
    text_norm = re.sub(r"\s+", " ", text.lower())
    conditions = card["decision"]["conditions"]

    for cond in conditions:
        cond_norm = re.sub(r"\s+", " ", cond.lower())
        # Ключевые слова условия должны присутствовать
        key_tokens = [t for t in re.findall(r"[а-яёa-z]+", cond_norm) if len(t) > 3]
        if not key_tokens:
            continue
        found = sum(1 for t in key_tokens if t in text_norm)
        if found < max(1, len(key_tokens) // 2):
            return ItemResult("F2", True, False, f"условие не отражено: {cond!r}")

    all_cond_tokens: set[str] = set()
    for cond in conditions:
        all_cond_tokens |= _key_tokens(cond)

    for m in _COND_MARKER_RE.finditer(text):
        rest = text[m.end():]
        end = re.search(r"[.!?]|$", rest)
        clause = rest[: end.start()] if end else rest
        if _COND_NEGATION_RE.search(clause):
            continue
        clause = clause.strip(" :;-—")
        clause_tokens = _key_tokens(clause)
        if not clause_tokens:
            continue
        overlap = clause_tokens & all_cond_tokens
        if len(overlap) < max(1, len(clause_tokens) // 2):
            return ItemResult(
                "F2", True, False,
                f"выдуманное условие, которого нет в карточке: {clause!r}",
            )

    return ItemResult("F2", True, True)


def _check_r1(card: dict, push: str, card_text: str) -> ItemResult:
    """Правила уровней (spec/03 R1): пороги silver/gold не искажены.

    Порог уровня считается «упомянутым», если число встречается в тексте
    (в любом виде: «100 000», «100000»). Если уровень упомянут словом,
    а его порог из loyalty_level.csv отсутствует в тексте и в conditions
    (которые обязаны покрыть его — см. F2) — нарушение.
    """
    text = push + " " + card_text
    text_nums = set(_extract_all_numbers(text))
    # Числа, легитимно присутствующие в тексте: conditions + benefit
    legit = set(_extract_all_numbers(" ".join(card["decision"]["conditions"])))
    b = card["decision"]["benefit_month_rub"]
    if b > 0:
        legit.add(b)
    tier_thresholds = {"base": (), "silver": (40000,), "gold": (100000,)}
    for tier in {card["loyalty"]["current_tier"], card["loyalty"]["target_tier"]}:
        if not _TIER_MENTION[tier].search(text):
            continue
        for threshold in tier_thresholds.get(tier, ()):
            if threshold not in text_nums and threshold not in legit:
                return ItemResult("R1", True, False, f"уровень {tier!r} упомянут без порога {threshold}")
    return ItemResult("R1", True, True)


def _check_p1(card: dict, push: str, card_text: str) -> ItemResult:
    """Дополнительно запрещено по offer_id + универсальный стоп-список."""
    text = push + " " + card_text
    offer_id = card["decision"]["offer_id"]
    rules = load_rules()
    rule = rules[offer_id]
    # Извлекаем запрещённые слова из «Дополнительно запрещено»
    banned_col = [k for k in rule if k.startswith("Дополнительно")][0]
    banned_raw = rule[banned_col]
    # Универсальные стоп-слова
    universal_stops = [
        "кредит", "предодобрен", "игнорируй", "ignore",
        "вы уже на уровне", "вы уже имеете",
    ]
    for stop in universal_stops:
        if stop.lower() in text.lower():
            return ItemResult("P1", True, False, f"универсальный стоп: {stop!r}")
    # Специфичные по offer
    if offer_id == "LOY-UP-002":
        for stop in ("премиальн", "элитн"):
            if stop in text.lower():
                return ItemResult("P1", True, False, f"LOY-UP-002: запрещено {stop!r}")
    elif offer_id == "LOY-UP-003":
        if re.search(r"друг(их|ые)\s+банк|сравни\w*\s+с\s+банк", text, re.I):
            return ItemResult("P1", True, False, "LOY-UP-003: сравнение с другими банками")
    elif offer_id == "CASH-CAT-011":
        if "на все покупки" in text.lower():
            return ItemResult("P1", True, False, "CASH-CAT-011: «кэшбэк на все покупки»")
    elif offer_id == "SAV-RATE-004":
        for stop in ("доход", "заработаете"):
            if stop in text.lower():
                return ItemResult("P1", True, False, f"SAV-RATE-004: запрещено {stop!r}")
    elif offer_id == "SUB-BUNDLE-007":
        has_paid = any("199" in c or "платн" in c.lower() for c in card["decision"]["conditions"])
        if has_paid:
            if "199" not in text and "платн" not in text.lower():
                return ItemResult("P1", True, False, "SUB-BUNDLE-007: платное продление не упомянуто")
    elif offer_id == "FEE-WAIVE-009":
        if "бесплатн" in text.lower():
            has_condition = any(c for c in card["decision"]["conditions"])
            if not has_condition:
                return ItemResult("P1", True, False, "FEE-WAIVE-009: «бесплатно» без условия")
    return ItemResult("P1", True, True)


_LATIN_WORD_RE = re.compile(r"[A-Za-z]+")


def _canonical_latin_whitelist(card: dict) -> set[str]:
    """Латинские слова из самого канонического названия оффера («Silver»,
    «Gold») — часть обязательного факта (spec/05), а не посторонний язык,
    поэтому единственное разрешённое исключение из проверки T1."""
    name = canonical_name(card["decision"]["offer_id"])
    return {w.lower() for w in _LATIN_WORD_RE.findall(name)}


def _check_lang(card: dict, push: str, card_text: str) -> ItemResult:
    """T1 (spec/03): текст PUSH/CARD — только на русском.

    Допустимы: кириллица, цифры, стандартная пунктуация/валюта и латинские
    слова, входящие в каноническое название оффера (Silver/Gold — часть
    бренда, не смешение языка). Любая другая буква не из кириллицы
    (латиница, посторонний алфавит, иероглифы и т.п.) — FAIL: утечка
    чужого языка/мусора в клиентский текст недопустима даже при
    некорректном ответе модели (CLAUDE.md, приоритет — безопасность).
    """
    whitelist = _canonical_latin_whitelist(card)
    for label, text in (("PUSH", push), ("CARD", card_text)):
        scrubbed = text
        for word in whitelist:
            scrubbed = re.sub(re.escape(word), "", scrubbed, flags=re.I)
        for ch in scrubbed:
            if ch.isalpha() and ch.lower() not in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя":
                return ItemResult(
                    "T1", True, False,
                    f"{label}: нерусский символ {ch!r} — текст должен быть только на русском",
                )
    return ItemResult("T1", True, True)


# Символ, повторённый подряд 3+ раза — признак сбоя генерации («ааа»,
# «!!!»). Цифры и пробелы исключены: легитимные числа вида «300 000» или
# «40 000» не должны ложно попадать под срабатывание.
_DUP_CHAR_RE = re.compile(r"([^\d\s])\1{2,}")


def _check_dup_chars(card: dict, push: str, card_text: str) -> ItemResult:
    """T2 (spec/03): нет аномальных повторов одного символа подряд в тексте
    PUSH/CARD — признак сбоя генерации модели (зацикливание/«заикание»),
    а не осмысленного текста. Числа исключены из проверки (см. `_DUP_CHAR_RE`).
    """
    for label, text in (("PUSH", push), ("CARD", card_text)):
        m = _DUP_CHAR_RE.search(text)
        if m:
            return ItemResult(
                "T2", True, False,
                f"{label}: символ {m.group(1)!r} повторён подряд {len(m.group(0))} раз — похоже на сбой генерации",
            )
    return ItemResult("T2", True, True)


# --------------------------------------------------------------------------- #
# LLM-validator                                                               #
# --------------------------------------------------------------------------- #

def llm_validate(card: dict, push: str, card_text: str, system_prompt: str, user_prompt: str) -> str:
    """Вызывает LLM-validator через Open Router (llm_client.chat_completion).

    `llm_client._validator_stub` остаётся доступной отдельно для офлайн-тестов
    (spec-test-runner), но здесь по умолчанию используется реальный вызов.
    """
    from common.llm_client import chat_completion

    return chat_completion(system_prompt, user_prompt)


def _parse_llm_verdict(raw: str) -> dict | None:
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return d
    except (json.JSONDecodeError, TypeError):
        pass
    # Попробуём извлечь JSON из markdown-блока
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            d = json.loads(m.group(0))
            if isinstance(d, dict):
                return d
        except json.JSONDecodeError:
            pass
    return None


def _item_from_llm(code: str, blocking: bool, data: dict) -> ItemResult:
    passed = data.get("pass", False)
    reason = data.get("reason", "")
    extra = {k: v for k, v in data.items() if k not in ("pass", "reason")}
    return ItemResult(code, blocking, passed, reason, extra)


# --------------------------------------------------------------------------- #
# Оркестрация                                                                 #
# --------------------------------------------------------------------------- #

def run_validator(card: dict, push: str, card_text: str) -> ValidatorResult:
    """Запускает Python + LLM валидатор (spec/03)."""
    from common.prompt_assembly import build_validator_prompts

    result = ValidatorResult()

    # Python-проверки
    for check in (_check_b1, _check_f1, _check_f2, _check_r1, _check_p1,
                  _check_lang, _check_dup_chars):
        item = check(card, push, card_text)
        result.items.append(item)

    # LLM-проверки
    system_prompt, user_prompt = build_validator_prompts(card, push, card_text)
    from common.llm_client import LLMUnreachableError

    try:
        raw = llm_validate(card, push, card_text, system_prompt, user_prompt)
    except LLMUnreachableError as exc:
        # Инфраструктурный сбой (spec/03 §5): llm_client.py уже отретраил
        # запрос и не получил пригодного ответа — код прокидывается в
        # agent.py, чтобы retry_gate был пропущен (в отличие от обычного
        # LLM_ERROR ниже).
        raw = ""
        llm_error = str(exc)
        result.llm_error_code = "LLM_UNREACHABLE"
    except Exception as exc:
        # Сбой LLM (spec/00): не чиним, доводим до тех же блокирующих FAIL,
        # что и нераспознанный JSON — дальше решает retry/reject.
        raw = ""
        llm_error = str(exc)
    else:
        llm_error = None
    result.llm_raw = raw
    verdict = _parse_llm_verdict(raw) if raw else None

    if verdict is None:
        reason = f"ошибка LLM: {llm_error}" if llm_error else "LLM-ответ не распознан"
        for code, blocking in [("L1", False), ("L2", False), ("L3", True),
                               ("I1", True), ("V3", False), ("V4", False), ("V5", False)]:
            result.items.append(ItemResult(code, blocking, False, reason))
    else:
        for code, blocking in [("L1", False), ("L2", False), ("L3", True),
                               ("I1", True), ("V3", False), ("V4", False), ("V5", False)]:
            if code in verdict:
                result.items.append(_item_from_llm(code, blocking, verdict[code]))
            else:
                result.items.append(ItemResult(code, blocking, False, f"ключ {code} отсутствует в LLM-ответе"))

        # Пересчёт V3 из pct_all (≥ 75 → pass)
        v3 = result.items_d.get("V3")
        if v3 and "pct_all" in v3.extra:
            try:
                v3.pass_ = v3.extra["pct_all"] >= 75
            except (TypeError, ValueError):
                v3.pass_ = False

    return result
