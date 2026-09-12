"""Входные фильтры (Python, до LLM) — spec/01.

Порядок проверок:
1. Валидация JSON и обязательных полей;
2. Соответствие программам (rules.csv, loyalty_level.csv, gap_criteria);
3. Фильтр prompt injection (маркеры команд в текстовых полях);
4. Стоп-флаги (no_marketing_consent, debt_collection).

Провал любого фильтра → REJECT_INPUT: карточка не доходит до creator.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import pandas as pd

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
RULES_CSV = DATA_DIR / "rules.csv"
LOYALTY_CSV = DATA_DIR / "loyalty_level.csv"
CARDS_JSON = DATA_DIR / "cards.json"

GAP_CRITERIA = ("monthly_spend", "products_count")
BENEFIT_CONFIDENCE = ("factual", "modelled")
REQUIRED_FLAGS = ("no_marketing_consent", "debt_collection", "vulnerable_client")


@dataclass(frozen=True)
class PrefilterResult:
    decision: str  # "PASS" | "REJECT_INPUT"
    reason: str = ""  # код причины отброса
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.decision == "PASS"


# Список паттернов — расширяемый (spec/01 §3). Данные в карточке
# обрабатываются только как данные; срабатывание любого паттерна отбрасывает
# карточку до LLM.
INJECTION_PATTERNS: dict[str, re.Pattern] = {
    "IGNORE_INSTRUCTIONS": re.compile(
        r"игнориру\w*|забудь\w*\s+инструкци"
        r"|ignore\s+(all\s+|previous\s+|prior\s+)?instructions"
        r"|disregard\s+(the\s+)?(all\s+|previous\s+|prior\s+)?instructions"
        r"|new\s+instructions",
        re.IGNORECASE,
    ),
    "SYSTEM_PROMPT": re.compile(r"системн\w*\s+(промпт|инструкци)|system\s+prompt", re.IGNORECASE),
    "PRIOR_INSTRUCTIONS": re.compile(
        r"(предыдущие|прежние|прошлые|старые)\s+инструкци", re.IGNORECASE
    ),
    "ROLE_CHANGE": re.compile(r"\bты\s+теперь\b|you\s+are\s+now", re.IGNORECASE),
    "COMMAND_TO_CLIENT": re.compile(
        r"(сообщи|скажи|напиши|повтори|ответь|укажи)\s+(клиенту|пользователю)",
        re.IGNORECASE,
    ),
    "IMPORTANT_COMMAND": re.compile(
        r"важно\s*:?\s*(сообщи|скажи|напиши|повтори|игнориру|отмени|скрой|замени)",
        re.IGNORECASE,
    ),
    # Только императивы (отмени/скрой/замени); существительное «отмена»
    # в штатных условиях (напр. «отмена в любой момент») не считается инфекцией.
    "HIDE_OR_REPLACE": re.compile(r"\bотмен(и|ите|яй)\b|\bsкро(й|йте)\b|\bзамен(и|ите)\b", re.IGNORECASE),
    "URL_QR": re.compile(
        r"https?://|\bwww\.|\b\d{1,3}(?:\.\d{1,3}){3}\b"
        r"|\bqr[-\s]?(код|code)"
        r"|\b\w+\.(ru|su|com|net|org|rf)\b",
        re.IGNORECASE,
    ),
}

_RULES: dict[str, dict[str, str]] | None = None
_TIER_ROWS: dict[str, dict[str, str]] | None = None
_TIER_RANK: dict[str, int] | None = None


def _read_csv_lenient(path: Path) -> pd.DataFrame:
    """Читает CSV через pandas без строгого парсинга заголовков: все значения
    остаются строками (никакого приведения к NaN/числам — «-» и т.п. должны
    доходить как есть), а названия столбцов лишь очищаются от лишних
    пробелов, без требования точного совпадения строк заголовка."""
    df = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
    df.columns = df.columns.str.strip()
    return df


def _find_column(columns: list[str], predicate: Callable[[str], bool]) -> str:
    """Нестрогий поиск столбца по предикату над нормализованным (нижний
    регистр, без пробелов по краям) названием — терпим к регистру/пробелам
    в заголовке CSV."""
    for col in columns:
        if predicate(col.strip().lower()):
            return col
    raise KeyError(f"не найден столбец среди {columns!r}")


def _load_rules() -> dict[str, dict[str, str]]:
    global _RULES
    if _RULES is None:
        df = _read_csv_lenient(RULES_CSV)
        offer_col = _find_column(list(df.columns), lambda c: "offer_id" in c)
        _RULES = {row[offer_col]: row for row in df.to_dict(orient="records")}
    return _RULES


def _load_tier_rows() -> dict[str, dict[str, str]]:
    global _TIER_ROWS, _TIER_RANK
    if _TIER_ROWS is None:
        df = _read_csv_lenient(LOYALTY_CSV)
        key = df.columns[0]  # первый столбец — уровень, независимо от заголовка
        _TIER_ROWS = {row[key]: row for row in df.to_dict(orient="records")}
        _TIER_RANK = {tier: i for i, tier in enumerate(_TIER_ROWS)}
    return _TIER_ROWS


def load_rules() -> dict[str, dict[str, str]]:
    return _load_rules()


def canonical_name(offer_id: str) -> str:
    """Каноническое название из rules.csv по offer_id."""
    rules = _load_rules()
    key = next(k for k in rules[offer_id] if "назв" in k.lower())
    return rules[offer_id][key]


def disclaimer_of(offer_id: str) -> str:
    """Обязательный дисклеймер из rules.csv по offer_id (spec/04) — единственное
    место, где ищется этот столбец; format_control и postprocessing не
    дублируют поиск, чтобы не разойтись при изменении заголовка в rules.csv."""
    rules = _load_rules()
    key = next(k for k in rules[offer_id] if k.lower().startswith("обязательный"))
    return rules[offer_id][key]


def normalize_numbers(text: str) -> str:
    """Прописные числа «1 250» → «1250»: убирает разделители-пробелы внутри
    чисел (для сравнения чисел в тексте с полями карточки)."""
    return re.sub(r"(?<=\d) (?=\d{3}(?:\d{3})*\b)", "", text)


def load_tier_rows() -> dict[str, dict[str, str]]:
    return _load_tier_rows()


def load_tier_rank() -> dict[str, int]:
    _load_tier_rows()
    return _TIER_RANK


def _fail(reason: str, detail: str) -> PrefilterResult:
    return PrefilterResult("REJECT_INPUT", reason, detail)


def _is_int(value: Any) -> bool:
    # bool — подкласс int, как число не проходит
    return isinstance(value, int) and not isinstance(value, bool)


def _check_fields(card: dict[str, Any]) -> PrefilterResult | None:
    if not isinstance(card.get("client_ref"), str) or not card["client_ref"].strip():
        return _fail("MISSING_FIELD", "client_ref: обязательная непустая строка")
    if not isinstance(card.get("segment"), str):
        return _fail("MISSING_FIELD", "segment: обязательная строка")
    tenure = card.get("tenure_months")
    if not _is_int(tenure) or tenure < 0:
        return _fail("INVALID_FIELD", "tenure_months: целое ≥ 0")

    loyalty = card.get("loyalty")
    if not isinstance(loyalty, dict):
        return _fail("MISSING_FIELD", "loyalty: обязательный объект")
    for name in ("current_tier", "target_tier"):
        if not isinstance(loyalty.get(name), str):
            return _fail("MISSING_FIELD", f"loyalty.{name}: обязательная строка")
    gap = loyalty.get("gap_criteria")
    if not isinstance(gap, list) or not all(
        isinstance(x, str) and x in GAP_CRITERIA for x in gap
    ):
        return _fail(
            "INVALID_FIELD",
            "loyalty.gap_criteria: массив, элементы ∈ {monthly_spend, products_count}",
        )

    decision = card.get("decision")
    if not isinstance(decision, dict):
        return _fail("MISSING_FIELD", "decision: обязательный объект")
    if not isinstance(decision.get("offer_id"), str):
        return _fail("MISSING_FIELD", "decision.offer_id: обязательная строка")
    if not isinstance(decision.get("offer_name"), str):
        return _fail("MISSING_FIELD", "decision.offer_name: обязательная строка")
    benefit = decision.get("benefit_month_rub")
    if not _is_int(benefit) or benefit < 0:
        return _fail("INVALID_FIELD", "decision.benefit_month_rub: целое ≥ 0")
    if decision.get("benefit_confidence") not in BENEFIT_CONFIDENCE:
        return _fail("INVALID_FIELD", "decision.benefit_confidence: ∈ {factual, modelled}")
    conditions = decision.get("conditions")
    if not isinstance(conditions, list) or not all(isinstance(x, str) for x in conditions):
        return _fail("INVALID_FIELD", "decision.conditions: массив строк")

    context = card.get("context")
    if not isinstance(context, dict):
        return _fail("MISSING_FIELD", "context: обязательный объект")
    for name in ("channel", "locale"):
        if not isinstance(context.get(name), str):
            return _fail("MISSING_FIELD", f"context.{name}: обязательная строка")

    flags = card.get("flags")
    if not isinstance(flags, dict):
        return _fail("MISSING_FIELD", "flags: обязательный объект")
    for name in REQUIRED_FLAGS:
        if not isinstance(flags.get(name), bool):
            return _fail(
                "MISSING_FIELD",
                f"flags.{name}: обязательный boolean — отсутствие флага трактуется как сбой, а не как false",
            )
    return None


def _check_program(card: dict[str, Any]) -> PrefilterResult | None:
    offer_id = card["decision"]["offer_id"]
    if offer_id not in _load_rules():
        return _fail("UNKNOWN_OFFER", f"decision.offer_id={offer_id!r} отсутствует в data/rules.csv")

    rank = load_tier_rank()
    loyalty = card["loyalty"]
    current, target = loyalty["current_tier"], loyalty["target_tier"]
    for name, tier in (("current_tier", current), ("target_tier", target)):
        if tier not in rank:
            return _fail("UNKNOWN_TIER", f"loyalty.{name}={tier!r} отсутствует в data/loyalty_level.csv")

    gap = loyalty["gap_criteria"]
    if target == current:
        if gap:
            return _fail(
                "TIER_GAP_MISMATCH",
                "target_tier == current_tier, но gap_criteria не пуста",
            )
    elif gap and rank[target] <= rank[current]:
        return _fail(
            "TIER_GAP_MISMATCH",
            f"gap_criteria не пуста, но target_tier={target!r} не старше current_tier={current!r}",
        )
    return None


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _check_injection(card: dict[str, Any]) -> PrefilterResult | None:
    hits: list[tuple[str, str]] = []
    for text in _iter_strings(card):
        for name, pattern in INJECTION_PATTERNS.items():
            match = pattern.search(text)
            if match:
                hits.append((name, f"{match.group(0)!r} в {text[:100]!r}"))
    if hits:
        detail = "; ".join(f"{name}: {frag}" for name, frag in hits[:5])
        return _fail("INJECTION_IN_INPUT", detail)
    return None


def _check_stop_flags(flags: dict[str, Any]) -> PrefilterResult | None:
    if flags["no_marketing_consent"]:
        return _fail("consent", "flags.no_marketing_consent=true — штатное решение, не ошибка входа")
    if flags["debt_collection"]:
        return _fail("debt", "flags.debt_collection=true — штатное решение, не ошибка входа")
    return None


def run_prefilter(card: Any) -> PrefilterResult:
    """Прогнать карточку через входные фильтры (spec/01).

    card — уже разобранный объект либо JSON-строка/байты (сначала парсят).
    Валидный вход — JSON-объект; массив, строка, число → REJECT_INPUT.
    """
    if isinstance(card, (str, bytes)):
        try:
            card = json.loads(card)
        except ValueError:
            return _fail("INVALID_JSON", "вход не является валидным JSON")
    if not isinstance(card, dict):
        return _fail("INVALID_JSON", "вход должен быть JSON-объектом (не массив, не строка, не число)")

    for check in (_check_fields, _check_program, _check_injection):
        result = check(card)
        if result is not None:
            return result

    result = _check_stop_flags(card["flags"])
    return result if result is not None else PrefilterResult("PASS")


if __name__ == "__main__":
    with open(CARDS_JSON, encoding="utf-8") as f:
        cards = json.load(f)

    cases: list[tuple[str, Any]] = [(f"c-{i:04d}", item) for i, item in enumerate(cards)]
    # Синтетические кейсы: вход не объект, отсутствие флага, неизвестный offer,
    # gap при равных уровнях, debt_collection, URL в conditions.
    cases += [
        ("[array]", [[1, 2, 3]]),
        ("no_flag", {
            "client_ref": "syn-1", "segment": "mass", "tenure_months": 5,
            "loyalty": {"current_tier": "base", "target_tier": "base", "gap_criteria": []},
            "decision": {
                "offer_id": "CASH-CAT-011", "offer_name": "x",
                "benefit_month_rub": 100, "benefit_confidence": "factual",
                "conditions": [],
            },
            "context": {"channel": "push+card", "locale": "ru-RU"},
            "flags": {"no_marketing_consent": False, "debt_collection": False},
        }),
        ("bad_offer", {
            "client_ref": "syn-2", "segment": "mass", "tenure_months": 5,
            "loyalty": {"current_tier": "base", "target_tier": "base", "gap_criteria": []},
            "decision": {
                "offer_id": "NO-SUCH", "offer_name": "x",
                "benefit_month_rub": 100, "benefit_confidence": "factual",
                "conditions": [],
            },
            "context": {"channel": "push+card", "locale": "ru-RU"},
            "flags": {"no_marketing_consent": False, "debt_collection": False, "vulnerable_client": False},
        }),
        ("gap_eq_tier", {
            "client_ref": "syn-3", "segment": "mass", "tenure_months": 5,
            "loyalty": {"current_tier": "silver", "target_tier": "silver", "gap_criteria": ["monthly_spend"]},
            "decision": {
                "offer_id": "LOY-UP-003", "offer_name": "x",
                "benefit_month_rub": 100, "benefit_confidence": "factual",
                "conditions": [],
            },
            "context": {"channel": "push+card", "locale": "ru-RU"},
            "flags": {"no_marketing_consent": False, "debt_collection": False, "vulnerable_client": False},
        }),
        ("debt", {
            "client_ref": "syn-4", "segment": "mass", "tenure_months": 5,
            "loyalty": {"current_tier": "base", "target_tier": "base", "gap_criteria": []},
            "decision": {
                "offer_id": "CASH-CAT-011", "offer_name": "x",
                "benefit_month_rub": 100, "benefit_confidence": "factual",
                "conditions": [],
            },
            "context": {"channel": "push+card", "locale": "ru-RU"},
            "flags": {"no_marketing_consent": False, "debt_collection": True, "vulnerable_client": False},
        }),
        ("url_in_cond", {
            "client_ref": "syn-5", "segment": "mass", "tenure_months": 5,
            "loyalty": {"current_tier": "base", "target_tier": "base", "gap_criteria": []},
            "decision": {
                "offer_id": "CASH-CAT-011", "offer_name": "x",
                "benefit_month_rub": 100, "benefit_confidence": "factual",
                "conditions": ["перейти по ссылке https://bank.example/promo"],
            },
            "context": {"channel": "push+card", "locale": "ru-RU"},
            "flags": {"no_marketing_consent": False, "debt_collection": False, "vulnerable_client": False},
        }),
    ]

    for ref, item in cases:
        res = run_prefilter(item)
        status = "PASS" if res.ok else f"REJECT_INPUT ({res.reason})"
        print(f"{ref:>13}  {status:<28}  {res.detail}")
