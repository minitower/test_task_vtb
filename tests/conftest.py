"""Общие фикстуры для spec-test-runner.

Ключевой принцип: тесты не делают сетевых вызовов. Там, где пайплайн
по умолчанию обращается к `common.llm_client.chat_completion` (реальный
Open Router), тесты подставляют детерминированные заглушки
`_creator_stub` / `_validator_stub` — они и есть «граница LLM» для этого
набора тестов (см. task.txt / инструкцию spec-test-runner).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def base_card() -> dict[str, Any]:
    """Минимальная валидная карточка (проходит все входные фильтры)."""
    return {
        "client_ref": "t-0001",
        "segment": "mass",
        "tenure_months": 12,
        "loyalty": {
            "current_tier": "base",
            "target_tier": "base",
            "gap_criteria": [],
        },
        "decision": {
            "offer_id": "CASH-CAT-011",
            "offer_name": "Повышенный кэшбэк в категории «Супермаркеты»",
            "benefit_month_rub": 350,
            "benefit_confidence": "factual",
            "conditions": ["выбрать категорию до конца месяца"],
        },
        "context": {
            "channel": "push+card",
            "locale": "ru-RU",
            "last_contact_days_ago": 5,
            "recent_events": [],
        },
        "flags": {
            "no_marketing_consent": False,
            "debt_collection": False,
            "vulnerable_client": False,
        },
    }


@pytest.fixture
def make_card(base_card):
    """Фабрика: глубокая копия base_card + точечные переопределения через
    dotted-путь (например make_card(**{"flags.debt_collection": True}))."""

    def _make(**overrides: Any) -> dict[str, Any]:
        card = copy.deepcopy(base_card)
        for dotted, value in overrides.items():
            parts = dotted.split(".")
            node = card
            for p in parts[:-1]:
                node = node[p]
            node[parts[-1]] = value
        return card

    return _make


@pytest.fixture(scope="session")
def real_cards() -> list[dict[str, Any]]:
    with open(REPO_ROOT / "data" / "cards.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def cards_by_ref(real_cards) -> dict[str, dict[str, Any]]:
    out = {}
    for c in real_cards:
        ref = c.get("client_ref")
        if ref:
            out[ref] = c
    return out


@pytest.fixture
def stub_llm_boundary(monkeypatch):
    """Подменяет узел creator (agent.call_creator) и LLM-часть валидатора
    (filters.validator.llm_validate) на детерминированные заглушки
    common.llm_client._creator_stub / _validator_stub, и считает число
    вызовов каждой стороны — чтобы тесты приёмки могли проверить
    "0 обращений к LLM" для карточек, отброшенных входным фильтром.

    Явно НЕ трогает common.llm_client.chat_completion — если код где-то
    всё же попытается дойти до реального HTTP-вызова, тест должен упасть
    (а не тихо уйти в сеть), поэтому дополнительно ставим "сторожа".
    """
    import agent
    import filters.validator as validator_mod
    from common.llm_client import _creator_stub, _validator_stub

    calls = {"creator": 0, "validator": 0}

    def fake_call_creator(card, system, user):
        calls["creator"] += 1
        return _creator_stub(card)

    def fake_llm_validate(card, push, card_text, system_prompt, user_prompt):
        calls["validator"] += 1
        return _validator_stub(card, push, card_text)

    def guard_chat_completion(*args, **kwargs):
        raise AssertionError(
            "Тест попытался вызвать common.llm_client.chat_completion "
            "(реальный HTTP к Open Router) — запрещено в тестах."
        )

    monkeypatch.setattr(agent, "call_creator", fake_call_creator)
    monkeypatch.setattr(validator_mod, "llm_validate", fake_llm_validate)
    monkeypatch.setattr("common.llm_client.chat_completion", guard_chat_completion)

    return calls
