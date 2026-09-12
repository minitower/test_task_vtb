"""Структурное логирование pipeline (spec/07).

Одно событие (record) — одна JSON-строка со стандартными полями `ts`
(ISO-8601, UTC), `client_ref`, `attempt`, `stage` + поля конкретного
этапа. Логгер детерминирован (не зависит от LLM) и не должен ронять
pipeline: при сбое сериализации отдаём минимальную запись вместо
исключения (spec/07 §5 — "логгер не чинит и не влияет на результат").

В лог не пишутся полные тексты creator/validator и сырые данные карточки
— только поля решения и длины (spec/07 §1).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def make_event(stage: str, client_ref: str, attempt: int, **fields: Any) -> str:
    """Собрать одну структурную JSON-запись события pipeline (spec/07 §1)."""
    event: dict[str, Any] = {
        "ts": _now_iso(),
        "client_ref": client_ref,
        "attempt": attempt,
        "stage": stage,
        **fields,
    }
    try:
        return json.dumps(event, ensure_ascii=False)
    except (TypeError, ValueError):
        # Логгер не должен ронять pipeline (spec/07 §5) — запись
        # пропускается по содержимому, но не по факту существования.
        return json.dumps(
            {
                "ts": event["ts"],
                "client_ref": client_ref,
                "attempt": attempt,
                "stage": stage,
                "log_error": "serialization_failed",
            },
            ensure_ascii=False,
        )
