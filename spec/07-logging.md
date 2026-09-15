# 07. Логирование (наблюдаемость)

Логирование — по одному **событию** (record) на каждый шаг pipeline.
Событие — структурный record (JSON-строка в строке), чтобы можно было
парсить в метрики/дашборд. Логгер не зависит от LLM: он детерминирован.

## 1. Общие требования
- Каждое событие: `ts` (ISO-8601, utc), `client_ref`, `attempt` (1..3),
  `stage` (см. ниже), и полей конкретного этапа.
- События пишутся в поток `PIPELINE` (в лог, отдельный от ошибок `ERROR`).
  Не пишутся в лог **тексты creator/validator** целиком и сырые данные
  карточки — только поля решения и **длины**, чтобы не течь персональными
  данными и не раздувать лог.
- Связь событий в карточку: `client_ref`; связь попыток внутри карточки:
  `attempt`.
- Отдельный stream `REJECT` — итоговое решение по карточке (SEND /
  REJECT_INPUT / REJECT_VALIDATION) с причиной.

## 2. Этапы (stage) и их метрики

| stage | Что логируется |
|-------|----------------|
| `input` | проход входных фильтров (spec/01): какие фильтры прошли/упали, `reason` при DROP, длительность фильтров `dur_ms`. |
| `creator_llm` | **TTFT** (time-to-first-token, от запроса до первого символа), `dur_ms` (полный ответ), `prompt_tokens`, `completion_tokens`, `model`, `max_attempts_sofar` ( номер попытки), `ok`; при `ok=false` — `error`, `error_code` (см. §2.1). |
| `format_check` | PASS/FAIL формат-контроля (spec/02): какие проверки упали (например «PUSH len 82>70»), `dur_ms`. |
| `python_checks` | PASS/FAIL детерминированных проверок (spec/03, кодом): список кодов, которые упали (B1, F1, F2, R1, P1…), `dur_ms`. |
| `validator_llm` | TTFT, `dur_ms`, `prompt_tokens`, `completion_tokens`, `ok`, `parsed` (JSON распарсился?), `verdict` (PASS/FAIL по схеме); при сбое LLM-вызова — `error_code` (см. §2.1). |
| `postprocess` | проход постобработки (spec/04): вставка дисклеймера (ok/dup), финальный контроль (тэг, служебные поля, длина), `dur_ms`. |
| `retry` | переход на новую попытку: `attempt`, причина (какой stage сработал), список `codes` упавших пунктов. |
| `final` | итог по карточке: `result` (SEND / REJECT_*), `reason`, `total_attempts`, `total_llm_calls`, `total_dur_ms`. |

### 2.1 Коды ошибок LLM-вызова (`error_code` на `creator_llm`/`validator_llm`)

Спецификация — spec/03 §5. Два статуса, один новый:

| `error_code` | Значение |
|--------------|----------|
| `LLM_ERROR` | Прочий сбой вызова (пустой ответ, неожиданный формат, нераспарсируемый JSON) — обычный content-retry, как раньше. |
| `LLM_UNREACHABLE` | Сетевая ошибка или HTTP-код не 2xx (402, 5xx и т.п.) повторились подряд, и внутренние ретраи транспортного уровня (`common/llm_client.py`, `LLM_MAX_RETRIES`) исчерпаны — сервис не дал пригодного ответа ни разу. |

`LLM_UNREACHABLE` — инфраструктурный, не content-сбой: retry creator'а
(`retry_gate`) для него пропускается, попытки не расходуются (spec/03
§5) — `final.reason` для таких карточек не содержит «after N attempts».

## 3. Основные KPI (вычисляются из событий `creator_llm`/`validator_llm`/`final`)
- **TTFT** (time-to-first-token): отдельно для creator и для validator,
  мс. (цель — задержка первого символа, чтобы понимать «как быстро начали
  отвечать»).
- **Response speed** = `dur_ms` полного ответа LLM; отдельно P50/P95.
- **Retry count**: `total_attempts` по карточке и распределение
  (сколько карточек: 1 / 2 / 3 попытки).
- **Throughput**: карточек / минуту (по `ts` событий `final`).
- **Success rate**: доля `final == SEND` от всех карточек; отдельно
  `REJECT_INPUT` и `REJECT_VALIDATION`.
- **Token cost**: `prompt_tokens` + `completion_tokens` суммарно (creator
  и validator), на карточку.
- **First-attempt pass rate**: доля карточек, прошедших с 1-й попытки
  (`total_attempts == 1` и `result == SEND`).

## 4. Пример записи событий (схема, не данные)
```json
{"ts":"…","client_ref":"c-8f21","stage":"creator_llm","attempt":1,
 "ttft_ms":180,"dur_ms":940,"prompt_tokens":410,"completion_tokens":72,
 "model":"gemma-flash","ok":true}
{"ts":"…","client_ref":"c-8f21","stage":"format_check","attempt":1,"ok":true,"dur_ms":1}
{"ts":"…","client_ref":"c-8f21","stage":"python_checks","attempt":1,
 "ok":false,"codes":["F1"],"dur_ms":2}
{"ts":"…","client_ref":"c-8f21","stage":"retry","attempt":2,
 "reason":"F1","dur_ms":0}
{"ts":"…","client_ref":"c-8f21","stage":"final","result":"REJECT_VALIDATION",
 "reason":"F1 after 3 attempts","total_attempts":3,"total_llm_calls":6,
 "total_dur_ms":2740}
```

## 5. Ограничения
- В логе нет персональных данных клиента (`client_ref` — псевдоним, а
  не PII). Нет полного текста ответа.
- Поля, зависящие от LLM (`ttft_ms`, `prompt_tokens`, …) появляются
  только когда есть `stage` с LLM-вызовом; для чисто Python-стадий —
  только `dur_ms`.
- Логгер не «чинит» и не влияет на результат: он только наблюдает.
  Если логгер упал — запись пропущена, но pipeline продолжается.
