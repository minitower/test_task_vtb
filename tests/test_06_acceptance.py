"""spec/06-acceptance.md — сквозной тест по 12 карточкам data/cards.json.

Пайплайн (agent.PIPELINE) прогоняется через LLM-границу — заглушки
`_creator_stub`/`_validator_stub` (см. tests/conftest.py:stub_llm_boundary),
чтобы результат был детерминированным и не уходил в сеть (реальный
`chat_completion` сейчас вызывает Open Router — см. отчёт spec-test-runner
про несоответствие с описанием "заглушки по умолчанию").

Ожидаемые исходы взяты дословно из таблицы spec/06:
9 SEND, 3 DROP_FILTER (c-8d44 consent, c-0c92 injection, c-5f30 missing flag).
"""

from __future__ import annotations

import pytest

import agent

# (client_ref, expected_status, must_be_zero_llm_calls)
ACCEPTANCE_TABLE = [
    ("c-8f21", "ACCEPT", False),
    ("c-1a03", "ACCEPT", False),
    ("c-77b2", "ACCEPT", False),
    ("c-4d19", "ACCEPT", False),
    ("c-3ab8", "ACCEPT", False),
    ("c-9c55", "ACCEPT", False),
    ("c-6ff1", "ACCEPT", False),
    ("c-2e64", "ACCEPT", False),
    ("c-8d44", "REJECT_INPUT", True),   # DROP_FILTER: no_marketing_consent
    ("c-b0a7", "ACCEPT", False),
    ("c-0c92", "REJECT_INPUT", True),   # DROP_FILTER: injection
    ("c-5f30", "REJECT_INPUT", True),   # DROP_FILTER: missing flag
]

SEND_REFS = [ref for ref, status, _ in ACCEPTANCE_TABLE if status == "ACCEPT"]
DROP_REFS = [ref for ref, status, _ in ACCEPTANCE_TABLE if status == "REJECT_INPUT"]

assert len(SEND_REFS) == 9
assert len(DROP_REFS) == 3


@pytest.mark.parametrize("client_ref,expected_status,zero_llm", ACCEPTANCE_TABLE)
def test_acceptance_table_outcome(stub_llm_boundary, cards_by_ref, client_ref, expected_status, zero_llm):
    card = cards_by_ref[client_ref]
    state = agent.run_card(card)
    assert state.get("status") == expected_status, (
        f"{client_ref}: ожидался статус {expected_status} (spec/06), получен "
        f"{state.get('status')}. Лог:\n" + "\n".join(state.get("log", []))
    )
    if zero_llm:
        assert stub_llm_boundary["creator"] == 0, f"{client_ref}: creator LLM был вызван, хотя должен быть 0 обращений (DROP_FILTER до creator)"
        assert stub_llm_boundary["validator"] == 0, f"{client_ref}: validator LLM был вызван, хотя должен быть 0 обращений"


def test_c8d44_consent_reason_logged(stub_llm_boundary, cards_by_ref):
    state = agent.run_card(cards_by_ref["c-8d44"])
    assert state["status"] == "REJECT_INPUT"
    assert any("consent" in line for line in state.get("log", []))


def test_c0c92_injection_reason_logged(stub_llm_boundary, cards_by_ref):
    state = agent.run_card(cards_by_ref["c-0c92"])
    assert state["status"] == "REJECT_INPUT"
    assert any("INJECTION_IN_INPUT" in line for line in state.get("log", []))


def test_c5f30_missing_flag_reason_logged(stub_llm_boundary, cards_by_ref):
    state = agent.run_card(cards_by_ref["c-5f30"])
    assert state["status"] == "REJECT_INPUT"
    assert any("MISSING_FIELD" in line for line in state.get("log", []))


def test_c0c92_injection_does_not_leak_offer_name_even_if_reached_creator(cards_by_ref):
    """spec/06 note under the table: даже если входной фильтр гипотетически
    не сработал, offer_name всё равно не должен доходить до промпта
    creator (второй контур защиты, spec/05). Тестируем сборку промпта
    напрямую на этой карточке, независимо от prefilter."""
    from common.prompt_assembly import build_creator_prompts

    card = cards_by_ref["c-0c92"]
    malicious = card["decision"]["offer_name"]
    _, user = build_creator_prompts(card, attempt=1)
    assert malicious not in user


@pytest.mark.parametrize("client_ref", SEND_REFS)
def test_send_cards_final_text_contains_disclaimer(stub_llm_boundary, cards_by_ref, client_ref):
    card = cards_by_ref[client_ref]
    state = agent.run_card(card)
    assert state["status"] == "ACCEPT"
    from filters.postprocessing import disclaimer_of

    disclaimer = disclaimer_of(card["decision"]["offer_id"])
    assert disclaimer in state["final_card"]


@pytest.mark.parametrize("client_ref", SEND_REFS)
def test_send_cards_pass_within_three_attempts(stub_llm_boundary, cards_by_ref, client_ref):
    card = cards_by_ref[client_ref]
    state = agent.run_card(card)
    assert state["status"] == "ACCEPT"
    assert 1 <= state.get("attempt", 1) <= 3
