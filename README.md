# test_task_innotech

LangGraph-агент для проактивного банковского совета в мобильном приложении
(пуш + карточка): по входной карточке решения формулирует текст, который
проходит через creator (LLM) → validator (Python + LLM) → постобработку,
прежде чем уйти клиенту. Приоритет — детерминированность и безопасность:
любое неопределённое состояние отбрасывается, а не «чинится».

Полная спецификация — в [`CLAUDE.md`](CLAUDE.md) и [`spec/`](spec); ниже —
только то, что нужно, чтобы запустить и потрогать проект руками.

## Pipeline

```
START → prefilter (Python) → creator (LLM) → format-контроль (Python)
      → validator (Python + LLM) → postprocessing (Python) → END
```

- **prefilter** — валидация входной карточки, изоляция prompt injection
  (`filters/prefilter.py`, [spec/01](spec/01-input-filters.md)).
- **creator** — LLM формулирует PUSH + CARD по промптам из `prompt/creator.py`
  ([spec/02](spec/02-output-contract.md), [spec/05](spec/05-prompt-assembly.md)).
- **format-контроль** — проверка контракта ответа (`filters/format_control.py`).
- **validator** — Python-проверки (коды B1/F1/F2/R1/P1/T1/T2) + LLM-вердикт
  (`filters/validator.py`, [spec/03](spec/03-validation.md)).
- **postprocessing** — код добавляет дисклеймер и делает финальный скан
  (`filters/postprocessing.py`, [spec/04](spec/04-postprocessing.md)).
- Retry: до 3 попыток на карточку, после первой creator получает ревью
  упавших пунктов. Сбой LLM или исчерпанные попытки → отброс
  (`REJECT_INPUT` / `REJECT_VALIDATION`), см. [spec/00](spec/00-main.md).

Граф собран в [`agent.py`](agent.py) на `langgraph` (`StateGraph`).

## Структура

```
test_task_innotech/
├── agent.py                    # граф pipeline (StateGraph) и точка входа (CLI)
├── common/
│   ├── llm_client.py           # клиент Open Router
│   ├── prompt_assembly.py      # сборка промптов с минимальным контекстом
│   └── logging_utils.py        # события по этапам pipeline
├── filters/
│   ├── prefilter.py            # входная валидация + изоляция prompt injection
│   ├── format_control.py       # контроль формата ответа creator'а
│   ├── validator.py            # Python-проверки (B1/F1/F2/R1/P1/T1/T2) + LLM-вердикт
│   └── postprocessing.py       # вставка дисклеймера, финальный скан
├── prompt/
│   ├── creator.py               # системный/пользовательский промпт creator (default)
│   ├── creator_creative.py      # вариант creator с creative-параметрами
│   └── validator.py             # промпт validator'а
├── data/
│   ├── cards.json               # 12 карточек для приёмки
│   ├── rules.csv                # каталог офферов
│   ├── loyalty_level.csv        # уровни лояльности
│   ├── log.json / log_creative.json        # структурные логи последнего прогона
│   └── results.md / results_creative.md    # тексты последнего прогона (см. ниже)
├── spec/                         # спецификации по этапам (00–07, см. таблицу в CLAUDE.md)
├── tests/                        # pytest, по одному файлу на spec (test_0N_*.py)
├── CLAUDE.md                     # полная спецификация проекта
└── pyproject.toml / uv.lock      # зависимости (uv)
```

Тесты (`tests/`) по одному файлу на spec, LLM в тестах всегда заглушен
(`tests/conftest.py`, `stub_llm_boundary`).

## Установка

Нужен Python 3.13 (см. [`.python-version`](.python-version)). Зависимости
и lock-файл — под [`uv`](https://docs.astral.sh/uv/):

```bash
uv sync
```

Без uv — через pip (обычный venv):

```bash
pip install -e . --group dev
```

Скопировать `.env.example` в `.env` и указать ключ Open Router:

```bash
cp .env.example .env
```

```
OPENROUTER_API_KEY=<ваш ключ>
OPENROUTER_MODEL=deepseek/deepseek-v4.1-flash
```

Без `OPENROUTER_API_KEY` реальные LLM-вызовы (creator/validator) не
пройдут — для прогона без сети используйте тесты (там LLM всегда
заглушен).

## Запуск

Прогнать все 12 карточек из `data/cards.json` (пишет `data/log.json` и
`data/results.md`):

```bash
uv run agent.py
```

Одна карточка по `client_ref` (не трогает `data/log*.json`/`results*.md`,
печатает PUSH/CARD и лог в консоль):

```bash
uv run agent.py c-8f21
```

Вариант creator'а `creative` (temperature 0.7 / top_p 0.9 вместо
default 0.2 / 0.2; см. [spec/05 §5](spec/05-prompt-assembly.md)) —
результаты пишутся в `*_creative` файлы:

```bash
uv run agent.py --variant creative
```

`--quiet` отключает построчный прогресс в консоли (по умолчанию включён):

```bash
uv run agent.py --quiet
```

Флаги можно сочетать: `uv run agent.py c-8f21 --variant creative`.

## Тесты

```bash
uv run pytest
```

Тесты не делают сетевых вызовов: `common.llm_client.chat_completion`
подменяется на «сторож», который роняет тест, если пайплайн всё же
попытается дойти до реального HTTP. Файл [`tests/test_06_acceptance.py`](tests/test_06_acceptance.py)
сверяет прогон всех 12 карточек с таблицей приёмки
([spec/06](spec/06-acceptance.md)).

## Логи и результаты

После `uv run agent.py` результаты — в `data/results.md` (что реально
ушло бы клиенту, PUSH/CARD по каждой карточке) и `data/log.json`
(структурные события по этапам pipeline: `input`, `creator_llm`,
`format_check`, `python_checks`, `validator_llm`, `postprocess`, `retry`,
`final` — без текста ответов и PII, см. [spec/07](spec/07-logging.md)).

### Итоги последнего прогона

Прогон всех 12 карточек (`data/cards.json`) для обоих вариантов creator'а
([`data/results.md`](data/results.md) и
[`data/results_creative.md`](data/results_creative.md)):

| Вариант | PASS | REJECT_INPUT |
| --- | --- | --- |
| default | 9 / 12 | 3 / 12 (`c-8d44`, `c-0c92`, `c-5f30`) |
| creative | 9 / 12 | 3 / 12 (те же карточки) |

- Отброшенные карточки совпадают в обоих вариантах — `prefilter` отбраковывает
  их до creator'а, поэтому temperature/top_p варианта на это не влияют.
- На прошедших карточках `creative` даёт более «живые» формулировки (например,
  цифра выгоды выносится в начало PUSH), `default` — более нейтральные и
  предсказуемые; факты, правила уровней и посимвольный дисклеймер совпадают —
  оба варианта проходят один и тот же порог валидатора (см. CLAUDE.md).
- REJECT_VALIDATION (отброс после 3 неудачных попыток) в этом прогоне не
  встретился ни разу — creator с первой попытки укладывается в контракт на
  всех карточках, прошедших prefilter.

## TODO

1) Возможность добавления для промпт-инъекций отдельного классификатора для более точной его классификации (настроили простые REGEXP и <user_input> тэг для защиты, что, конечно, недостаточно);
2) Не реализован доступ к агенту извне, сейчас он просто запускается через консоль. Есть возможность добавить endpoint для каждой карточки (т. к. есть run_card функция в agents.py, которая может быть перенесена в API с сохранением карточки в cards.json, либо дописать функцию с принятием JSON-а через body), а также упаковка этого в Docker;
3) Поиск в настоящий момент основан на обычных CSV. Это удобно для тестового задания, в реальности желательно перенести все эти наработки в БД и использовать для заполнения промптов;
4) Сейчас я тестил данное заявление на небольших карточках: PUSH <= 70 символов, CARD <= 350 символам (т. к. это пуш уведомление, оно должно быть коротким). В будущем можно было бы расширить применение алгоритма на большие объемы текста;
5) Также дополнительно было бы возможно добавить NER модель, которая бы обезличивала данные (если бы такие данные попадали в модель), либо использовать regexp (как сделано сейчас). Второй вариант, конечно, предпочтительнее;
6) Т. к. количество тестов мало мы отбрасываем результат, если он 3 раза не прошел валидацию. На проде такое, конечно, не допустимо, такое должно уходить на ручные проверки;
7) Следует сделать больше обработок для ситуаций недоступности LLM: экспоненциальный backoff, проверка LLM на доступность, проверка возможности LLM принимать показатели внутри запроса.