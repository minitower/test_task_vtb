"""spec/07-logging.md — структурное логирование по этапам.

spec/07 требует по одному структурному JSON-событию (record) на каждый
шаг pipeline: поля `ts`, `client_ref`, `attempt`, `stage` + метрики этапа
(TTFT, dur_ms, prompt_tokens, ...), разделение потоков PIPELINE/ERROR/REJECT,
запрет на полный текст ответа/сырые данные карточки в логе.

Текущая реализация (agent.py: `state["log"]`) — это список
человекочитаемых строк ("[prefilter] c-8f21: PASS", "[creator] попытка 1:
ответ получен", ...), без единого структурного формата, без `ts`, без
разделения stage-полей, без TTFT/dur_ms/token-метрик и без разделения
потоков PIPELINE/ERROR/REJECT.

Тест ниже фиксирует это расхождение явно (а не молчаливым ослаблением):
он ожидает, что хотя бы одна запись лога, если её распарсить как JSON,
содержит обязательные по spec/07 §1 поля. Ожидается, что тест будет
падать на текущем коде — это осознанно задокументированный gap, а не
баг одной строки; see also spec-test-runner report.
"""

from __future__ import annotations

import json

import agent


def test_log_records_are_structured_events_per_spec_07(stub_llm_boundary, cards_by_ref):
    state = agent.run_card(cards_by_ref["c-8f21"])
    log = state.get("log", [])
    assert log, "лог пустой — нечего проверять"

    structured = []
    for line in log:
        try:
            structured.append(json.loads(line))
        except (json.JSONDecodeError, TypeError):
            continue

    assert structured, (
        "spec/07 §1 требует структурные JSON-события с полями ts/client_ref/"
        "attempt/stage на каждом шаге pipeline; текущий `state['log']` — "
        "список свободных человекочитаемых строк, ни одна из которых не "
        "парсится как JSON-событие. Логирование по spec/07 фактически не "
        "реализовано (это задокументированный gap, см. отчёт spec-test-runner)."
    )
    for event in structured:
        assert "ts" in event and "stage" in event and "attempt" in event


def test_reject_stream_final_decision_has_reason(stub_llm_boundary, cards_by_ref):
    """spec/07 §1: отдельный stream REJECT — итоговое решение (SEND /
    REJECT_INPUT / REJECT_VALIDATION) с причиной. Ищем хотя бы событие
    финального решения с явной причиной для отклонённой карточки."""
    state = agent.run_card(cards_by_ref["c-8d44"])
    assert state["status"] == "REJECT_INPUT"
    log = state.get("log", [])
    assert any("REJECT" in line for line in log)
    # причина (consent) должна быть видна в итоговой записи
    assert any("consent" in line for line in log)
