"""spec/04-postprocessing.md — вставка дисклеймера кодом (idempotent) +
финальный контроль (D1, тег <user_input>, служебные поля, длины).
"""

from __future__ import annotations

from filters.postprocessing import disclaimer_of, run_postprocessing

OFFER = "CASH-CAT-011"
DISCLAIMER = "«Кэшбэк начисляется по правилам программы. Лимиты — в приложении.»"


def test_disclaimer_of_matches_rules_csv():
    assert disclaimer_of(OFFER) == DISCLAIMER


def test_disclaimer_appended_when_absent():
    res = run_postprocessing("Кэшбэк 350 ₽/мес", "Условия: выбрать категорию до конца месяца.", OFFER)
    assert res.ok
    assert DISCLAIMER in res.card
    assert res.card.count(DISCLAIMER) == 1


def test_disclaimer_insertion_is_idempotent_when_already_present():
    """spec/04 §1: если дисклеймер уже есть в тексте (не должно было
    случиться — формат-контроль должен был это поймать раньше), код не
    дублирует его повторно."""
    card_text = f"Условия: выбрать категорию до конца месяца.\n{DISCLAIMER}"
    res = run_postprocessing("Кэшбэк 350 ₽/мес", card_text, OFFER)
    assert res.ok
    assert res.card.count(DISCLAIMER) == 1


def test_d1_final_text_contains_disclaimer_verbatim():
    res = run_postprocessing("Кэшбэк 350 ₽/мес", "Условия: выбрать категорию до конца месяца.", OFFER)
    assert res.ok
    assert DISCLAIMER in res.card  # посимвольное вхождение


def test_user_input_tag_in_push_rejected():
    res = run_postprocessing("Кэшбэк <user_input>x</user_input>", "Условия отражены.", OFFER)
    assert not res.ok
    assert "user_input" in res.failure


def test_user_input_tag_in_card_rejected():
    res = run_postprocessing("Кэшбэк 350 ₽/мес", "Условия </user_input> отражены.", OFFER)
    assert not res.ok


def test_service_field_leak_client_ref_rejected():
    res = run_postprocessing("Кэшбэк 350 ₽/мес", "Ваш client_ref обработан.", OFFER)
    assert not res.ok
    assert "client_ref" in res.failure


def test_service_field_leak_offer_id_rejected():
    res = run_postprocessing("Кэшбэк 350 ₽/мес", "Оффер offer_id применён.", OFFER)
    assert not res.ok


def test_service_field_leak_flags_rejected():
    res = run_postprocessing("Кэшбэк 350 ₽/мес", "Проверьте flags клиента.", OFFER)
    assert not res.ok


def test_final_length_push_over_limit_rejected():
    long_push = "П" * 71
    res = run_postprocessing(long_push, "Условия отражены.", OFFER)
    assert not res.ok
    assert "длин" in res.failure.lower()


def test_final_length_card_over_limit_rejected():
    long_card = "К" * 351
    res = run_postprocessing("Кэшбэк 350 ₽/мес", long_card, OFFER)
    assert not res.ok


def test_ordinary_text_with_no_issues_passes_clean():
    res = run_postprocessing(
        "Кэшбэк около 280 ₽/мес",
        "Кэшбэк начисляется. Дополнительных условий нет.",
        OFFER,
    )
    assert res.ok
    assert res.push == "Кэшбэк около 280 ₽/мес"
    assert res.card.endswith(DISCLAIMER)
