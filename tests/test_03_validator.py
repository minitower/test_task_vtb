"""spec/03-validation.md — validator: Python-коды (B1,F1,F2,R1,P1) +
LLM-коды (L1,L2,L3,I1,V3,V4,V5) + порог PASS.

Порог: PASS = (B1 ∧ F1 ∧ F2 ∧ R1 ∧ D1 ∧ P1 ∧ I1 ∧ L3) ∧ (>=2 из {L1,L2,V3}).
D1 проверяется в постобработке (spec/04), не здесь.
"""

from __future__ import annotations

import json

from filters.validator import (
    ValidatorResult,
    ItemResult,
    _check_b1,
    _check_f1,
    _check_f2,
    _check_p1,
    _check_r1,
    run_validator,
)


# --------------------------------------------------------------------------- #
# B1 — блокировка повышения уровня                                           #
# --------------------------------------------------------------------------- #

def test_b1_blocks_tier_offer_when_current_equals_target(make_card):
    card = make_card(**{
        "decision.offer_id": "LOY-UP-002",
        "loyalty.current_tier": "silver",
        "loyalty.target_tier": "silver",
    })
    res = _check_b1(card, "Переход на Silver", "Переход на уровень Silver.")
    assert not res.pass_


def test_b1_does_not_false_positive_on_c77b2(cards_by_ref):
    """spec/06 #3 (c-77b2): silver->silver, но оффер SAV-RATE-004 — НЕ
    оффер уровня. B1 не должен ложно блокировать."""
    card = cards_by_ref["c-77b2"]
    assert card["loyalty"]["current_tier"] == card["loyalty"]["target_tier"]
    assert card["decision"]["offer_id"] not in ("LOY-UP-002", "LOY-UP-003")
    res = _check_b1(card, "Надбавка к ставке", "Надбавка к ставке по накопительному счёту.")
    assert res.pass_, f"B1 ложно сработал на не-tier оффере: {res.reason}"


def test_b1_does_not_false_positive_on_c3ab8(cards_by_ref):
    """spec/06 #5 (c-3ab8): SAV-RATE-004, target=silver!=current=base —
    легитимный переход по gap_criteria, не оффер уровня. B1 pass."""
    card = cards_by_ref["c-3ab8"]
    res = _check_b1(card, "Надбавка к ставке", "Надбавка к ставке по накопительному счёту.")
    assert res.pass_, f"B1 ложно сработал: {res.reason}"


def test_b1_legit_upgrade_offer_passes(make_card):
    card = make_card(**{
        "decision.offer_id": "LOY-UP-002",
        "loyalty.current_tier": "base",
        "loyalty.target_tier": "silver",
    })
    res = _check_b1(card, "Переход на Silver", "Вы можете перейти на уровень Silver.")
    assert res.pass_


# --------------------------------------------------------------------------- #
# F1 — факт-чек: цифры                                                       #
# --------------------------------------------------------------------------- #

def test_f1_passes_when_numbers_match_card(make_card):
    card = make_card(**{"decision.benefit_month_rub": 350})
    res = _check_f1(card, "Кэшбэк 350 ₽/мес", "Условия: выбрать категорию до конца месяца.")
    assert res.pass_


def test_f1_fails_on_hallucinated_number(make_card):
    card = make_card(**{"decision.benefit_month_rub": 350})
    res = _check_f1(card, "Кэшбэк 999 ₽/мес", "Условия: выбрать категорию до конца месяца.")
    assert not res.pass_


def test_f1_zero_benefit_should_not_be_mentioned(cards_by_ref):
    """spec/06 #5 (c-3ab8): benefit_month_rub=0 -> выгоду в тексте не
    упоминать; число из текста, не совпадающее с карточкой (в т.ч.
    случайное упоминание какой-то суммы выгоды), должно быть FAIL."""
    card = cards_by_ref["c-3ab8"]
    assert card["decision"]["benefit_month_rub"] == 0
    res = _check_f1(card, "Надбавка к ставке", "Надбавка к ставке по накопительному счёту: около 500 ₽/мес.")
    assert not res.pass_, "F1 должен ловить упоминание выгоды, которой в карточке нет (0 ₽)"


# --------------------------------------------------------------------------- #
# F2 — факт-чек: условия (в т.ч. выдумывание условий)                        #
# --------------------------------------------------------------------------- #

def test_f2_passes_when_condition_reflected(make_card):
    card = make_card(**{"decision.conditions": ["выбрать категорию до конца месяца"]})
    res = _check_f2(card, "Кэшбэк 350 ₽/мес", "Условие: выбрать категорию до конца месяца.")
    assert res.pass_


def test_f2_fails_when_condition_missing_from_text(make_card):
    card = make_card(**{"decision.conditions": ["выбрать категорию до конца месяца"]})
    res = _check_f2(card, "Кэшбэк 350 ₽/мес", "Обслуживание стандартное.")
    assert not res.pass_


def test_f2_should_fail_on_hallucinated_condition_not_in_card(cards_by_ref):
    """spec/03 F2 (текст спеки): "Добавлены условия, которых нет в
    карточке -> FAIL." spec/06 #10 (c-b0a7): conditions=[] — тест кейс
    явно называет это сценарием "F2: в условиях нет ... — его не
    выдумывать". Если creator всё же придумал условие, которого нет в
    карточке, F2 обязан его поймать.

    ВНИМАНИЕ: текущая реализация `_check_f2` только проверяет, что
    существующие карточные условия отражены в тексте (`for cond in
    card["decision"]["conditions"]: ...`) — при conditions=[] цикл не
    выполняется ни разу, и функция возвращает pass_=True независимо от
    того, что написано в тексте. Она НЕ детектирует придуманные условия,
    которых нет в списке. Это прямое расхождение со spec/03 (см. отчёт
    spec-test-runner) — тест ниже описывает требуемое поведение и, как
    ожидается, будет падать на текущем коде.
    """
    card = cards_by_ref["c-b0a7"]
    assert card["decision"]["conditions"] == []
    card_text = "Кэшбэк начисляется. Условие: выбрать категорию до конца месяца."
    res = _check_f2(card, "Кэшбэк около 280 ₽/мес", card_text)
    assert not res.pass_, (
        "F2 должен FAIL-ить придуманное условие, которого нет в карточке "
        "(conditions=[]), но текущая реализация не проверяет 'лишние' "
        "условия — только 'отсутствующие' (spec/03 §1 F2, вторая часть "
        "требования: 'Добавлены условия, которых нет в карточке -> FAIL')."
    )


def test_f2_empty_conditions_with_clean_text_passes(cards_by_ref):
    """Позитивный вариант того же кейса c-b0a7: пустой conditions и
    текст, ничего не выдумывающий, должен пройти F2."""
    card = cards_by_ref["c-b0a7"]
    res = _check_f2(card, "Кэшбэк около 280 ₽/мес", "Дополнительных условий нет.")
    assert res.pass_


# --------------------------------------------------------------------------- #
# R1 — правила уровней                                                       #
# --------------------------------------------------------------------------- #

def test_r1_passes_when_tier_threshold_present(make_card):
    card = make_card(**{
        "decision.offer_id": "LOY-UP-002",
        "loyalty.current_tier": "base",
        "loyalty.target_tier": "silver",
    })
    res = _check_r1(card, "Переход на Silver", "Для уровня Silver нужны траты от 40 000 ₽/мес.")
    assert res.pass_


def test_r1_fails_when_tier_mentioned_without_threshold(make_card):
    card = make_card(**{
        "decision.offer_id": "LOY-UP-003",
        "loyalty.current_tier": "silver",
        "loyalty.target_tier": "gold",
        "decision.conditions": [],
    })
    res = _check_r1(card, "Переход на Gold", "Уровень Gold даёт больше привилегий.")
    assert not res.pass_


def test_r1_c2e64_gold_thresholds_not_distorted(cards_by_ref):
    """spec/06 #8 (c-2e64): пороги Gold (100000 ₽, 3 продукта) не искажены."""
    card = cards_by_ref["c-2e64"]
    text = "Переход на Gold: траты по карте от 100 000 ₽/мес, не менее трёх продуктов банка."
    res = _check_r1(card, "Переход на Gold", text)
    assert res.pass_


# --------------------------------------------------------------------------- #
# P1 — «Дополнительно запрещено» + универсальный стоп-список                 #
# --------------------------------------------------------------------------- #

def test_p1_loy_up_002_forbids_premium_words(make_card):
    card = make_card(**{"decision.offer_id": "LOY-UP-002"})
    res = _check_p1(card, "Премиальный уровень Silver", "Элитный статус ждёт вас.")
    assert not res.pass_


def test_p1_sub_bundle_007_paid_renewal_not_omitted_c4d19(cards_by_ref):
    """spec/06 #4 (c-4d19): платное продление (199 ₽/мес) присутствует в
    conditions — текст обязан его отразить, иначе P1 FAIL (умолчание)."""
    card = cards_by_ref["c-4d19"]
    text_with_paid = "Подписка Мультибонус. Далее 199 ₽/мес, отмена в любой момент."
    res_ok = _check_p1(card, "Подписка Мультибонус", text_with_paid)
    assert res_ok.pass_, f"P1 ложно сработал при упоминании платного продления: {res_ok.reason}"

    text_omitting_paid = "Подписка Мультибонус на 3 месяца в подарок. Активация в приложении."
    res_fail = _check_p1(card, "Подписка Мультибонус", text_omitting_paid)
    assert not res_fail.pass_, "P1 должен FAIL-ить умолчание о платном продлении"


def test_p1_fee_waive_009_free_without_condition_fails(make_card):
    card = make_card(**{
        "decision.offer_id": "FEE-WAIVE-009",
        "decision.conditions": [],
    })
    res = _check_p1(card, "Бесплатное обслуживание", "Обслуживание карты бесплатно для вас.")
    assert not res.pass_


def test_p1_universal_stop_credit_word(make_card):
    card = make_card()
    res = _check_p1(card, "Кэшбэк 350 ₽/мес", "Вам одобрен кредит на выгодных условиях.")
    assert not res.pass_


def test_p1_cash_cat_011_forbids_all_purchases(make_card):
    card = make_card(**{"decision.offer_id": "CASH-CAT-011"})
    res = _check_p1(card, "Кэшбэк на все покупки", "Кэшбэк начисляется всегда.")
    assert not res.pass_


# --------------------------------------------------------------------------- #
# Порог PASS/FAIL (ValidatorResult.pass_)                                    #
# --------------------------------------------------------------------------- #

def _item(code, blocking, ok):
    return ItemResult(code, blocking, ok)


def test_threshold_all_mandatory_pass_and_two_important_pass():
    result = ValidatorResult(items=[
        _item("B1", True, True), _item("F1", True, True), _item("F2", True, True),
        _item("R1", True, True), _item("P1", True, True), _item("I1", True, True),
        _item("L3", True, True),
        _item("L1", False, True), _item("L2", False, True), _item("V3", False, False),
    ])
    assert result.pass_


def test_threshold_fails_if_any_mandatory_fails():
    result = ValidatorResult(items=[
        _item("B1", True, False), _item("F1", True, True), _item("F2", True, True),
        _item("R1", True, True), _item("P1", True, True), _item("I1", True, True),
        _item("L3", True, True),
        _item("L1", False, True), _item("L2", False, True), _item("V3", False, True),
    ])
    assert not result.pass_


def test_threshold_fails_if_only_one_important_passes():
    result = ValidatorResult(items=[
        _item("B1", True, True), _item("F1", True, True), _item("F2", True, True),
        _item("R1", True, True), _item("P1", True, True), _item("I1", True, True),
        _item("L3", True, True),
        _item("L1", False, True), _item("L2", False, False), _item("V3", False, False),
    ])
    assert not result.pass_


# --------------------------------------------------------------------------- #
# LLM-часть: парсинг вердикта, сбой -> отброс блокирующих пунктов            #
# --------------------------------------------------------------------------- #

def test_run_validator_unparsable_llm_json_fails_blocking_items(monkeypatch, base_card):
    import filters.validator as validator_mod

    monkeypatch.setattr(validator_mod, "llm_validate", lambda *a, **k: "not json at all {{{")
    result = run_validator(base_card, "Кэшбэк 350 ₽/мес", "Условия: выбрать категорию до конца месяца.")
    items = result.items_d
    assert items["L3"].pass_ is False
    assert items["I1"].pass_ is False
    assert not result.pass_


def test_run_validator_llm_exception_treated_as_failure(monkeypatch, base_card):
    """spec/00: сбой LLM -> отбрасываем, не чиним. Ошибка при вызове
    LLM-валидатора должна давать блокирующий FAIL, а не крашить пайплайн."""
    import filters.validator as validator_mod

    def raising(*a, **k):
        raise RuntimeError("simulated LLM outage")

    monkeypatch.setattr(validator_mod, "llm_validate", raising)
    result = run_validator(base_card, "Кэшбэк 350 ₽/мес", "Условия: выбрать категорию до конца месяца.")
    assert not result.pass_
    assert result.items_d["L3"].pass_ is False


def test_v3_recomputed_from_pct_all(monkeypatch, base_card):
    """spec/03 §4: итоговый вердикт V3 пересчитывается Python из pct_all
    (>=75 -> true), а не берётся из LLM-поля pass как есть."""
    import filters.validator as validator_mod

    verdict = {
        "L1": {"pass": True, "reason": "ok"},
        "L2": {"pass": True, "reason": "ok"},
        "L3": {"pass": True, "reason": "ok", "tier": "base"},
        "I1": {"pass": True, "reason": "ok"},
        # LLM говорит pass=True, но pct_all=50 < 75 -> Python должен
        # переопределить итог на False.
        "V3": {"pass": True, "pct_push": 50, "pct_all": 50, "reason": "ok"},
        "V4": {"pass": True, "reason": "ok"},
        "V5": {"pass": True, "reason": "ok"},
    }
    monkeypatch.setattr(validator_mod, "llm_validate", lambda *a, **k: json.dumps(verdict))
    result = run_validator(base_card, "Кэшбэк 350 ₽/мес", "Условия: выбрать категорию до конца месяца.")
    assert result.items_d["V3"].pass_ is False
