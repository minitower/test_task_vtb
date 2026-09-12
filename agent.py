"""Pipeline (langgraph) по spec/00:

START → prefilter → creator (LLM/заглушка) → format_check
      → validator (Python + LLM) → postprocessing → END(ACCEPT)

Ветки: prefilter fail → END(REJECT_INPUT); fail на format/validator/
postprocess → retry с review (max 3 попытки) → END(REJECT_VALIDATION).
"""

from __future__ import annotations

import dataclasses
import json
import sys
import time
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from filters.format_control import check_format
import common.llm_client as llm_client_mod
from common.llm_client import call_creator
from common.logging_utils import make_event
from filters.postprocessing import run_postprocessing
from filters.prefilter import run_prefilter
from common.prompt_assembly import build_creator_prompts, get_card_by_ref, load_cards
from filters.validator import run_validator

MAX_ATTEMPTS = 3

# Коды Python-проверок validator'а (spec/03 §1) — используются, чтобы
# разложить один ValidatorResult на два stage-события лога (spec/07):
# `python_checks` (детерминированные) и `validator_llm` (LLM-коды).
_PYTHON_CHECK_CODES = ("B1", "F1", "F2", "R1", "P1", "T1", "T2")


def _dur_ms(t0: float) -> float:
    return round((time.perf_counter() - t0) * 1000, 3)


def _say(state: "PipelineState", msg: str) -> None:
    """Живой прогресс в консоль по ходу выполнения (не лог spec/07 — тот
    остаётся структурным JSON в state['log']). Включается через
    verbose=True в run_card/run_all — без него ничего не печатает, чтобы
    не шуметь в тестах, которые вызывают run_card напрямую."""
    if state.get("verbose"):
        print(msg, flush=True)


class PipelineState(TypedDict, total=False):
    client_ref: str
    card: dict
    variant: str
    verbose: bool
    attempt: int
    prior_attempt: str
    review: list
    creator_response: str
    push: str
    card_text: str
    failed_items: list
    validation: dict
    post: dict
    final_push: str
    final_card: str
    status: str
    log: list
    llm_calls: int
    total_dur_ms: float


def _n_prefilter(state: PipelineState) -> dict:
    card = state["card"]
    client_ref = state["client_ref"]
    _say(state, f"\n=== Карточка {client_ref} ===")
    t0 = time.perf_counter()
    res = run_prefilter(card)
    dur = _dur_ms(t0)
    log = list(state.get("log", [])) + [
        make_event(
            "input", client_ref, 1,
            ok=res.ok, reason=res.reason, detail=res.detail, dur_ms=dur,
        )
    ]
    if not res.ok:
        # Это штатное решение, не ошибка входа (spec/01 §4): в логе отдельно.
        _say(state, f"[1] Входные фильтры: REJECT_INPUT ({res.reason}) — {res.detail}")
        log = log + [
            make_event(
                "final", client_ref, 1,
                result="REJECT_INPUT", reason=res.reason,
                total_attempts=0, total_llm_calls=0, total_dur_ms=dur,
            )
        ]
        return {"status": "REJECT_INPUT", "log": log}
    d = card["decision"]
    _say(
        state,
        f"[1] Входные фильтры пройдены. Ожидаемый результат: оффер {d['offer_id']}, "
        f"выгода {d['benefit_month_rub']} ₽/мес ({d['benefit_confidence']}), "
        f"условий: {len(d['conditions'])}, уровень {card['loyalty']['current_tier']}"
        f" → {card['loyalty']['target_tier']}",
    )
    return {
        "attempt": 1,
        "review": [],
        "prior_attempt": "",
        "creator_response": "",
        "push": "",
        "card_text": "",
        "failed_items": [],
        "status": "",
        "log": log,
        "llm_calls": 0,
        "total_dur_ms": dur,
    }


def _n_creator(state: PipelineState) -> dict:
    card = state["card"]
    client_ref = state["client_ref"]
    attempt = state.get("attempt", 1)
    total_dur = state.get("total_dur_ms", 0.0)
    llm_calls = state.get("llm_calls", 0) + 1
    variant = state.get("variant", "default")
    system, user = build_creator_prompts(
        card, attempt, state.get("prior_attempt"), state.get("review"),
        variant=variant,
    )
    _say(state, f"[{attempt}] Формирование промпта завершено, отправка в LLM (creator, variant={variant})...")
    t0 = time.perf_counter()
    try:
        response = call_creator(card, system, user, variant=variant)
    except Exception as exc:
        # Сбой LLM (spec/00): не чиним — идём в retry_gate тем же путём,
        # что и провал формат-контроля/валидатора.
        dur = _dur_ms(t0)
        _say(state, f"[{attempt}] Сбой LLM за {dur:.0f} мс: {exc}")
        log = list(state.get("log", [])) + [
            make_event(
                "creator_llm", client_ref, attempt,
                ok=False, dur_ms=dur, ttft_ms=dur, model=llm_client_mod.OPENROUTER_MODEL,
                error=str(exc)[:200],
            )
        ]
        return {
            "creator_response": "",
            "failed_items": [{"code": "LLM_ERROR", "blocking": True, "reason": str(exc)}],
            "log": log,
            "llm_calls": llm_calls,
            "total_dur_ms": total_dur + dur,
        }
    dur = _dur_ms(t0)
    _say(state, f"[{attempt}] LLM ответил за {dur:.0f} мс ({len(response)} символов)")
    log = list(state.get("log", [])) + [
        make_event(
            "creator_llm", client_ref, attempt,
            ok=True, dur_ms=dur, ttft_ms=dur, response_len=len(response),
            model=llm_client_mod.OPENROUTER_MODEL,
        )
    ]
    return {
        "creator_response": response,
        "failed_items": [],
        "log": log,
        "llm_calls": llm_calls,
        "total_dur_ms": total_dur + dur,
    }


def _creator_ok(state: PipelineState) -> str:
    return "format_check" if state.get("creator_response") else "retry_gate"


def _format_ok(state: PipelineState) -> str:
    return "validator" if state.get("push") and state.get("card_text") else "retry_gate"


def _n_format_check(state: PipelineState) -> dict:
    client_ref = state["client_ref"]
    attempt = state.get("attempt", 1)
    t0 = time.perf_counter()
    res = check_format(state["creator_response"], state["card"]["decision"]["offer_id"])
    dur = _dur_ms(t0)
    total_dur = state.get("total_dur_ms", 0.0) + dur
    log = list(state.get("log", [])) + [
        make_event(
            "format_check", client_ref, attempt,
            ok=res.ok, violation=(res.violation if not res.ok else ""), dur_ms=dur,
        )
    ]
    if not res.ok:
        _say(state, f"[{attempt}] Формат-контроль: FAIL — {res.violation}")
        return {
            "failed_items": [{"code": "FORMAT", "blocking": True, "reason": res.violation}],
            "log": log,
            "total_dur_ms": total_dur,
        }
    _say(state, f"[{attempt}] Формат-контроль: OK")
    return {"push": res.push, "card_text": res.card, "log": log, "total_dur_ms": total_dur}


def _validator_ok(state: PipelineState) -> str:
    v = state.get("validation")
    return "postprocess" if v and v["pass"] else "retry_gate"


def _n_validator_node(state: PipelineState) -> dict:
    client_ref = state["client_ref"]
    attempt = state.get("attempt", 1)
    t0 = time.perf_counter()
    result = run_validator(state["card"], state["push"], state["card_text"])
    dur = _dur_ms(t0)
    total_dur = state.get("total_dur_ms", 0.0) + dur
    llm_calls = state.get("llm_calls", 0) + 1

    items_d = result.items_d
    python_failed = [c for c in _PYTHON_CHECK_CODES if c in items_d and not items_d[c].pass_]
    parsed = False
    if result.llm_raw:
        try:
            json.loads(result.llm_raw)
            parsed = True
        except (json.JSONDecodeError, TypeError):
            parsed = False

    # Python-часть (детерминированные коды, spec/03 §1) — своё событие;
    # dur_ms этой части отдельно не инструментирован внутри run_validator
    # (единый вызов на Python+LLM), поэтому вся длительность отнесена к
    # `validator_llm`, где сосредоточена сетевая задержка.
    log = list(state.get("log", [])) + [
        make_event(
            "python_checks", client_ref, attempt,
            ok=not python_failed, codes=python_failed, dur_ms=0,
        ),
        make_event(
            "validator_llm", client_ref, attempt,
            ok=bool(result.llm_raw), parsed=parsed, dur_ms=dur, ttft_ms=dur,
            verdict=("PASS" if result.pass_ else "FAIL"),
        ),
    ]
    if result.pass_:
        _say(state, f"[{attempt}] Validator: PASS")
    else:
        codes = ", ".join(f["code"] for f in result.failures())
        _say(state, f"[{attempt}] Validator: FAIL — {codes}")
    return {
        "validation": {"pass": result.pass_, "items": result.items, "failures": result.failures()},
        "failed_items": result.failures(),
        "log": log,
        "llm_calls": llm_calls,
        "total_dur_ms": total_dur,
    }


def _post_ok(state: PipelineState) -> str:
    p = state.get("post")
    return "accept" if p and p["ok"] else "retry_gate"


def _n_postprocess(state: PipelineState) -> dict:
    client_ref = state["client_ref"]
    attempt = state.get("attempt", 1)
    t0 = time.perf_counter()
    res = run_postprocessing(state["push"], state["card_text"], state["card"]["decision"]["offer_id"])
    dur = _dur_ms(t0)
    total_dur = state.get("total_dur_ms", 0.0) + dur
    log = list(state.get("log", [])) + [
        make_event(
            "postprocess", client_ref, attempt,
            ok=res.ok, failure=(res.failure if not res.ok else ""), dur_ms=dur,
        )
    ]
    if res.ok:
        _say(state, f"[{attempt}] Постобработка: OK")
        return {
            "post": {"ok": True, "push": res.push, "card": res.card, "failure": ""},
            "log": log,
            "total_dur_ms": total_dur,
        }
    _say(state, f"[{attempt}] Постобработка: FAIL — {res.failure}")
    return {
        "post": {"ok": False, "push": res.push, "card": res.card, "failure": res.failure},
        "failed_items": [{"code": "POSTPROCESS", "blocking": True, "reason": res.failure}],
        "log": log,
        "total_dur_ms": total_dur,
    }


def _n_accept(state: PipelineState) -> dict:
    p = state["post"]
    client_ref = state["client_ref"]
    attempt = state.get("attempt", 1)
    log = list(state.get("log", [])) + [
        make_event(
            "final", client_ref, attempt,
            result="SEND", reason="", total_attempts=attempt,
            total_llm_calls=state.get("llm_calls", 0),
            total_dur_ms=state.get("total_dur_ms", 0.0),
            # Только длины — не полный текст (spec/07 §1, §5).
            push_len=len(p["push"]), card_len=len(p["card"]),
        )
    ]
    _say(state, f"[{attempt}] ACCEPT ✔\n    PUSH: {p['push']}\n    CARD: {p['card']}")
    return {"final_push": p["push"], "final_card": p["card"], "status": "ACCEPT", "log": log}


def _retry_to_creator(state: PipelineState) -> str:
    return "creator" if state.get("attempt", 1) < MAX_ATTEMPTS else "reject_validation"


def _n_retry_gate(state: PipelineState) -> dict:
    client_ref = state["client_ref"]
    attempt = state.get("attempt", 1)
    failed = state.get("failed_items", [])
    codes = [f["code"] for f in failed]
    reason = codes[0] if codes else "unknown"
    next_attempt = attempt + 1 if attempt < MAX_ATTEMPTS else attempt
    log = list(state.get("log", [])) + [
        make_event("retry", client_ref, next_attempt, reason=reason, codes=codes, dur_ms=0)
    ]
    if attempt >= MAX_ATTEMPTS:
        _say(state, f"[{attempt}] Попытки исчерпаны ({MAX_ATTEMPTS}) — REJECT_VALIDATION")
        return {"status": "REJECT_VALIDATION", "log": log}
    _say(state, f"[{attempt}] Попытка провалена ({reason}) → retry, попытка {attempt + 1}")
    return {
        "attempt": attempt + 1,
        "prior_attempt": state.get("creator_response", ""),
        "review": failed,
        "log": log,
    }


def _n_reject_validation(state: PipelineState) -> dict:
    client_ref = state["client_ref"]
    attempt = state.get("attempt", 1)
    failed = state.get("failed_items", [])
    reason = failed[0]["code"] if failed else "unknown"
    log = list(state.get("log", [])) + [
        make_event(
            "final", client_ref, attempt,
            result="REJECT_VALIDATION", reason=f"{reason} after {MAX_ATTEMPTS} attempts",
            total_attempts=attempt, total_llm_calls=state.get("llm_calls", 0),
            total_dur_ms=state.get("total_dur_ms", 0.0),
        )
    ]
    return {"status": "REJECT_VALIDATION", "log": log}


def _prefilter_route(state: PipelineState) -> str:
    return "creator" if state.get("status", "") != "REJECT_INPUT" else END


def build_graph() -> StateGraph:
    g = StateGraph(PipelineState)
    g.add_node("prefilter", _n_prefilter)
    g.add_node("creator", _n_creator)
    g.add_node("format_check", _n_format_check)
    g.add_node("validator_node", _n_validator_node)
    g.add_node("postprocess", _n_postprocess)
    g.add_node("accept", _n_accept)
    g.add_node("retry_gate", _n_retry_gate)
    g.add_node("reject_validation", _n_reject_validation)

    g.add_edge(START, "prefilter")
    g.add_conditional_edges("prefilter", _prefilter_route, {"creator": "creator", END: END})
    g.add_conditional_edges("creator", _creator_ok, {"format_check": "format_check", "retry_gate": "retry_gate"})
    g.add_conditional_edges("format_check", _format_ok, {"validator": "validator_node", "retry_gate": "retry_gate"})
    g.add_conditional_edges("validator_node", _validator_ok, {"postprocess": "postprocess", "retry_gate": "retry_gate"})
    g.add_conditional_edges("postprocess", _post_ok, {"accept": "accept", "retry_gate": "retry_gate"})
    g.add_conditional_edges("retry_gate", _retry_to_creator, {"creator": "creator", "reject_validation": "reject_validation"})
    g.add_edge("accept", END)
    g.add_edge("reject_validation", END)
    return g


PIPELINE = build_graph().compile()


def run_card(
    card: dict | None = None,
    client_ref: str | None = None,
    cards: list[dict] | None = None,
    variant: str = "default",
    verbose: bool = False,
) -> dict:
    """Прогнать одну карточку (или по client_ref из cards.json) через pipeline.

    `variant` выбирает набор промптов creator'а (common/prompt_assembly.py,
    "default" или "creative") — validator и порог прохода не меняются.
    `verbose=True` печатает живой прогресс по шагам (см. `_say`) вместо
    молчаливого ожидания финального состояния — полезно для CLI-прогона,
    где иначе не видно ничего до конца всей карточки (несколько LLM-вызовов
    подряд на retry). По умолчанию выключено, чтобы не шуметь при вызове
    из тестов/кода.
    """
    if card is None:
        cards = cards or load_cards()
        card = get_card_by_ref(cards, client_ref)
    state = {
        "client_ref": card["client_ref"], "card": card, "variant": variant,
        "verbose": verbose, "log": [],
    }
    return PIPELINE.invoke(state)


def run_all(variant: str = "default", verbose: bool = False) -> dict[str, dict]:
    return {c["client_ref"]: run_card(c, variant=variant, verbose=verbose) for c in load_cards()}


def _json_default(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def _summarize(client_ref: str, state: dict) -> dict:
    """Сводка по карточке для log.json — без сырых данных карточки и
    без полного текста push/card (spec/07 §1/§5: только поля решения и
    длины, лог не должен течь персональными/офферными данными). Сам
    финальный текст — не лог, а результат pipeline: он уходит в
    data/results.md (см. `_to_markdown`), не сюда.
    """
    summary: dict[str, Any] = {
        "client_ref": client_ref,
        "status": state.get("status"),
        "total_attempts": state.get("attempt"),
        "total_llm_calls": state.get("llm_calls"),
        "total_dur_ms": state.get("total_dur_ms"),
        "log": state.get("log", []),
    }
    if state.get("status") == "ACCEPT":
        summary["push_len"] = len(state.get("final_push", ""))
        summary["card_len"] = len(state.get("final_card", ""))
    return summary


def _to_markdown(results: dict[str, dict]) -> str:
    """Итоговый текст по карточкам (не лог): client_ref, PUSH, CARD —
    то, что реально уходит клиенту. Для отброшенных карточек текста нет —
    указывается статус отброса вместо PUSH/CARD."""
    blocks = []
    for client_ref, state in results.items():
        status = state.get("status")
        if status == "ACCEPT":
            body = f"PUSH: {state.get('final_push', '')}\nCARD: {state.get('final_card', '')}"
        else:
            body = f"_{status}_"
        blocks.append(f"## {client_ref}\n\n{body}")
    return "\n\n".join(blocks) + "\n"


LOG_JSON = Path(__file__).resolve().parent / "data" / "log.json"
RESULTS_MD = Path(__file__).resolve().parent / "data" / "results.md"


if __name__ == "__main__":
    # На Windows консоль по умолчанию не в UTF-8 — без этого print падает
    # на кириллице/₽ (UnicodeEncodeError).
    sys.stdout.reconfigure(encoding="utf-8")

    # --variant creative (в любом месте argv) → creator берёт другой набор
    # промптов (common/prompt_assembly.py); validator и порог те же.
    # --quiet отключает живой построчный прогресс (см. _say) — по
    # умолчанию он включён, иначе при нескольких карточках/retry не видно
    # вообще ничего, пока не завершится весь прогон.
    argv = sys.argv[1:]
    variant = "default"
    if "--variant" in argv:
        i = argv.index("--variant")
        variant = argv[i + 1]
        del argv[i:i + 2]
    verbose = "--quiet" not in argv
    if not verbose:
        argv.remove("--quiet")

    # --print-prompt <client_ref> [--attempt N] — не гоняет pipeline и не
    # трогает LLM, просто печатает итоговый system+user промпт creator'а
    # для конкретной карточки (common/prompt_assembly.build_creator_prompts),
    # ровно то, что реально уйдёт в модель — удобно для ручной проверки.
    if "--print-prompt" in argv:
        argv.remove("--print-prompt")
        attempt = 1
        if "--attempt" in argv:
            i = argv.index("--attempt")
            attempt = int(argv[i + 1])
            del argv[i:i + 2]
        if not argv:
            print("Использование: agent.py <client_ref> --print-prompt [--attempt N] [--variant creative]")
            sys.exit(1)
        client_ref = argv[0]
        card = get_card_by_ref(load_cards(), client_ref)
        system, user = build_creator_prompts(card, attempt, variant=variant)
        print(f"=== SYSTEM (variant={variant}) ===\n")
        print(system)
        print(f"\n=== USER (attempt {attempt}) ===\n")
        print(user)
        sys.exit(0)

    # Один client_ref позиционным аргументом → быстрый прогон одной
    # карточки для отладки (не трогает data/log*.json и data/results*.md,
    # печатает и текст ответа).
    if argv:
        client_ref = argv[0]
        st = run_card(client_ref=client_ref, variant=variant, verbose=verbose)
        print(f"\n{client_ref} -> {st.get('status')}")
        for line in st.get("log", []):
            print("   " + line)
        if st.get("status") == "ACCEPT":
            print("\nPUSH:", st.get("final_push"))
            print("CARD:", st.get("final_card"))
        sys.exit(0)

    results = run_all(variant=variant, verbose=verbose)
    print("\n--- Итог ---")
    for ref, st in results.items():
        print(f"{ref} -> {st.get('status')}")

    suffix = "" if variant == "default" else f"_{variant}"
    log_json = LOG_JSON.with_stem(LOG_JSON.stem + suffix)
    results_md = RESULTS_MD.with_stem(RESULTS_MD.stem + suffix)

    summaries = {ref: _summarize(ref, st) for ref, st in results.items()}
    with open(log_json, "w", encoding="utf-8") as f:
        json.dump(summaries, f, ensure_ascii=False, indent=2, default=_json_default)

    with open(results_md, "w", encoding="utf-8") as f:
        f.write(_to_markdown(results))

    print(f"\nСохранено: {log_json}")
    print(f"Сохранено: {results_md}")
