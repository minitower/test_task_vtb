"""Pipeline (langgraph) по spec/00:

START → prefilter → creator (LLM/заглушка) → format_check
      → validator (Python + LLM) → postprocessing → END(ACCEPT)

Ветки: prefilter fail → END(REJECT_INPUT); fail на format/validator/
postprocess → retry с review (max 3 попытки) → END(REJECT_VALIDATION).
"""

from __future__ import annotations

import json
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from format_control import check_format
from llm_client import call_creator
from postprocessing import run_postprocessing
from prefilter import run_prefilter
from prompt_assembly import build_creator_prompts, get_card_by_ref, load_cards
from validator import run_validator

MAX_ATTEMPTS = 3


class PipelineState(TypedDict, total=False):
    client_ref: str
    card: dict
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


def _n_prefilter(state: PipelineState) -> dict:
    card = state["card"]
    res = run_prefilter(card)
    log = list(state.get("log", [])) + [
        f"[prefilter] {state['client_ref']}: {res.decision}"
        + (f" ({res.reason}) {res.detail}" if res.reason else "")
    ]
    if not res.ok:
        # Это штатное решение, не ошибка входа (spec/01 §4): в логе отдельно.
        return {"status": "REJECT_INPUT", "log": log}
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
    }


def _n_creator(state: PipelineState) -> dict:
    card = state["card"]
    attempt = state.get("attempt", 1)
    system, user = build_creator_prompts(
        card, attempt, state.get("prior_attempt"), state.get("review")
    )
    response = call_creator(card, system, user)
    log = list(state.get("log", [])) + [f"[creator] попытка {attempt}: ответ получен"]
    return {"creator_response": response, "log": log}


def _format_ok(state: PipelineState) -> str:
    return "validator" if state.get("push") and state.get("card_text") else "retry_gate"


def _n_format_check(state: PipelineState) -> dict:
    res = check_format(state["creator_response"], state["card"]["decision"]["offer_id"])
    log = list(state.get("log", [])) + [
        f"[format] попытка {state.get('attempt')}: {'OK' if res.ok else 'FAIL: ' + res.violation}"
    ]
    if not res.ok:
        return {
            "failed_items": [{"code": "FORMAT", "blocking": True, "reason": res.violation}],
            "log": log,
        }
    return {"push": res.push, "card_text": res.card, "log": log}


def _validator_ok(state: PipelineState) -> str:
    v = state.get("validation")
    return "postprocess" if v and v["pass"] else "retry_gate"


def _n_validator_node(state: PipelineState) -> dict:
    result = run_validator(state["card"], state["push"], state["card_text"])
    log = list(state.get("log", [])) + [
        f"[validator] попытка {state.get('attempt')}: {'PASS' if result.pass_ else 'FAIL: ' + '; '.join(f['code'] for f in result.failures())}"
    ]
    return {
        "validation": {"pass": result.pass_, "items": result.items, "failures": result.failures()},
        "failed_items": result.failures(),
        "log": log,
    }


def _post_ok(state: PipelineState) -> str:
    p = state.get("post")
    return "accept" if p and p["ok"] else "retry_gate"


def _n_postprocess(state: PipelineState) -> dict:
    res = run_postprocessing(state["push"], state["card_text"], state["card"]["decision"]["offer_id"])
    log = list(state.get("log", [])) + [
        f"[post] {'OK' if res.ok else 'FAIL: ' + res.failure}"
    ]
    return {"post": {"ok": res.ok, "push": res.push, "card": res.card, "failure": res.failure}, "log": log}


def _n_accept(state: PipelineState) -> dict:
    p = state["post"]
    log = list(state.get("log", [])) + [
        f"[accept] {state['client_ref']}: PASS после {state.get('attempt')} попыток",
        "PUSH: " + p["push"],
        "CARD: " + p["card"],
    ]
    return {"final_push": p["push"], "final_card": p["card"], "status": "ACCEPT", "log": log}


def _retry_to_creator(state: PipelineState) -> str:
    return "creator" if state.get("attempt", 1) < MAX_ATTEMPTS else "reject_validation"


def _n_retry_gate(state: PipelineState) -> dict:
    attempt = state.get("attempt", 1)
    failed = state.get("failed_items", [])
    review_text = "\n".join(f"{i}. {f['code']}: {f['reason']}" for i, f in enumerate(failed, 1))
    log = list(state.get("log", [])) + [f"[retry] попытка {attempt}, review: {review_text or '-'}"]
    if attempt >= MAX_ATTEMPTS:
        return {"status": "REJECT_VALIDATION", "log": log}
    return {
        "attempt": attempt + 1,
        "prior_attempt": state.get("creator_response", ""),
        "review": failed,
        "log": log,
    }


def _n_reject_validation(state: PipelineState) -> dict:
    log = list(state.get("log", [])) + [f"[reject] {state['client_ref']}: исчерпаны {MAX_ATTEMPTS} попытки"]
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
    g.add_edge("creator", "format_check")
    g.add_conditional_edges("format_check", _format_ok, {"validator": "validator_node", "retry_gate": "retry_gate"})
    g.add_conditional_edges("validator_node", _validator_ok, {"postprocess": "postprocess", "retry_gate": "retry_gate"})
    g.add_conditional_edges("postprocess", _post_ok, {"accept": "accept", "retry_gate": "retry_gate"})
    g.add_conditional_edges("retry_gate", _retry_to_creator, {"creator": "creator", "reject_validation": "reject_validation"})
    g.add_edge("accept", END)
    g.add_edge("reject_validation", END)
    return g


PIPELINE = build_graph().compile()


def run_card(card: dict | None = None, client_ref: str | None = None, cards: list[dict] | None = None) -> dict:
    """Прогнать одну карточку (или по client_ref из cards.json) через pipeline."""
    if card is None:
        cards = cards or load_cards()
        card = get_card_by_ref(cards, client_ref)
    state = {"client_ref": card["client_ref"], "card": card, "log": []}
    return PIPELINE.invoke(state)


def run_all() -> dict[str, dict]:
    return {c["client_ref"]: run_card(c) for c in load_cards()}


if __name__ == "__main__":
    results = run_all()
    for ref, st in results.items():
        print(f"{ref} -> {st.get('status')}")
        for line in st.get("log", []):
            print("   " + line)
