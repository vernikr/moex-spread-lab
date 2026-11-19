# Технический дизайн и план оптимизации proof search (`moex-spread-lab`)

## 1. Цель

Без деградации качества ускорить подбор параметров стратегий (`Optuna TPESampler(seed=41)`) для вселенных `pair_screen` и `calendar_native` на интервалах `1h` и `1d` на целевом железе: macOS 15.8.3, MacBook Pro 16" 2019 (i7-9750H 6C/12T, 16 GB RAM). Нагрев не важен, выполнение только локально.

## 2. Итерация 0 — строгий набор тестов эквивалентности + базовые замеры (готово)

### 2.1 Тесты: `tests/test_perf_equivalence.py`

4 теста, каждый запускается дважды:
* `MOEX_PROOF_REFERENCE_EVAL=1` — эталонный путь (полная серия, summary, `run_id`, `notes`)
* `MOEX_PROOF_REFERENCE_EVAL=0` — быстрый путь (`metrics_only` на обучающих титрах)

Требуется бит-в-бит равенство (`_canon_sha` + `assertEqual`) по `run_id`, `rank_score`, `plateau`, причине rejeкции (`canonical reason`) и траектории Optuna.

Тесты:
* `test_1d_pair_funding_and_calendar_and_fixed_quantity_equivalence` — 1d: pair_screen + calendar_native + фиксированное количество
* `test_1h_calendar_proxy_and_native_cohort_and_mixed_universe` — 1h: календарный proxy + нативный спред + смешанная вселенная
* `test_1h_pair_screen_cohort_search_plateau_windows_and_single_run` — 1h pair_screen: подбор策略 + плато + одиночный прогон
* `test_file_caches_invalidate_immediately_on_in_place_same_size_edits` — инвалидация `(path, mtime_ns, size)` кэша при same-size редактировании

### 2.2 Базовые замеры (целевое железо, CPython 3.12.14)

| Показатель | Значение |
|------------|----------|
| Холодная загрузка `CalendarData` | 4.30 с на запрос |
| `_prepare_cohort` до Trial #0 | 14.57 с |
| `pair_screen` 1h trial | ~393.6 мс/титра (≈87% отбрасываемой работы presenttion) |
| `calendar_native` 1h trial | ~147.9 мс/титра (баг `sized_replay` вызывал `_calendar_proof` на каждой титре обучения) |

## 3. Итерация 1 — устранение мёртвой работы на титрах (готово)

### 3.1 `size_proof(..., metrics_only=False)` (`src/moex_spread_lab/position_sizing.py`)

* `proof = unit if metrics_only else deepcopy(unit)` — обучающие титры мутируют в place, нет копирования входа.
* `metrics_only=True` пропускает только presentational поля (`notes`, `run_id`). Структурные поля (ledger `quantity`/`margin_rub`/`funding_rub`, `sizing.initial_capital_rub`, `initial_capital_rub`, `summary`) финализируются **всегда** — они читаются `_measure()`, который считает цель Optuna.
* `funding_rub` в журнале теперь берётся из вычисленного значения `funding`, а не из старого значения строки (`row.get("funding_rub")`), что устраняло расхождение ±15 vs ±210.

### 3.2 Ленивый `StudyWindow.full` (`src/moex_spread_lab/proof_search.py`)

`cached_property` — дорогой seed-proof строится один раз при первом доступе и переиспользуется для отчёта победителя вместо пересчёта на каждой титре.

### 3.3 `(path, mtime_ns, size)` I/O кэш (`src/moex_spread_lab/w0.py`)

`read_hashed_json` кэширует `(value, sha256)` по ключу `(resolved_path, st_mtime_ns, st_size)`. Любое внешнее редактирование меняет ключ и инвалидирует кэш точно; same-size/те же байты — hit.

### 3.4 Общий `CalendarData` (`src/moex_spread_lab/proof.py`, `proof_batch.py`, `proof_strategy_search.py`)

`ProofService(root, calendar=...)` принимает общий `CalendarData`; `catalog()`, `proof()`, `_prepare_cohort()` и `strategy_search()` передают его Further вызов, вместо создания `CalendarData(root)` на каждый вызов.

## 4. Инцидент эквивалентности и исправление

### 4.1 Симптом

После правок Итерации 1 эквивалентный набор тестов падал: `test_1h_pair_screen...` hash mismatch (`ref f49b72...` vs `fast d5a330...`). Расхождение изолировалось в плато-титрах: `lqdt_on_margin_rub` ref=135.21 vs fast=310.92, крошечные float-различия в `rank_score`/`utility`, разный `run_id`.

### 4.2 Причина

В режиме `metrics_only=True` `size_proof` пропускал:
1. Мутацию строк серии (`row.update(quantity, margin_rub, funding_rub=...)`), поэтому в журнале оставалось **сырое** `funding_rub` (±15 за контракт) вместо **вычисленного** (`rate × quantity`, ±210). Это сдвигало `excess_rub` и `rank_score` на ~2·10⁻¹¹ — достаточно, чтобы изменить сериализацию кандидатов Optuna и `run_id`.
2. Presentation-блок (`sizing.initial_capital_rub`, `summary`, `notes`, `run_id`), из-за чего на одинаковом входе получалсяstrukturально разный `proof`.

Кроме того, частичные (тренировочные) календарные реплеи пропускали `_calendar_proof`, поэтому у unité не было полей `universe` и `run_id`: `_Terms` маршрутизировался в ветку пары → `KeyError: 'instrument'`; `size_proof` падал на `unit["run_id"]`.

### 4.3 Исправление

* `size_proof` всегда финализирует структурные поля в обоих режимах; только `notes`/`run_id` остаются за `not metrics_only`.
* `proof.py` `sized_replay` для частичных `calendar-proof.v1`юнитов выставляет `universe = CALENDAR_UNIVERSE` и лёгкий `run_id` (только идентификатор, не используется `_measure`), чтобы `_Terms` маршрутизировал в нативную ветку и `size_proof` не падал.

## 5. Измерения на целевом железе (Mac, venv CPython 3.12.14)

| Конфигурация | Reference | Fast | Speedup | Экономия |
|--------------|-----------|------|---------|----------|
| 1 пара (CNYRUBF/CRZ6), 80 титров | 2.10 с | 1.60 с | **1.31×** | 0.50 с |
| 2 пары (CRZ6 + CRM6), 80 титров | 4.66 с | 3.53 с | **1.32×** | 1.13 с |

Per-trial (песочница): 17.6 мс → 11.8 мс (≈1.5×).

### Почему 1.3×, а не 12–18× из плана

12–18× в плане относились к **удельной стоимости одного титра Optuna** (убрано `deepcopy(unit)` ≈48% + `_pair_payload` ≈28.7% ≈ 87% мёртвой работы на титре), а не к end-to-end wall-clock. На практике доминируют фиксированные накладные: создание Optuna study, один полный seed-proof на окно, TPE `ask/tell` overhead — они не зависят от числа пар и не ускоряются `metrics_only`. При росте числа пар абсолютная экономия растёт (0.50 с → 1.13 с), но отношение упирается в фиксированные затраты. Для приближения к 12–18× end-to-end нужно сотни пар в одном прогоне или снижение фиксированных затрат.

## 6. План / что осталось

* [x] Iteration 0 — тестовый набор эквивалентности + базовые замеры
* [x] Iteration 1 — `metrics_only`, ленивый `StudyWindow.full`, I/O кэш `w0`, общий `CalendarData`
* [x] Бит-в-бит эквивалентность подтверждена (206 тестов OK на целевом железе)
* [ ] Мультипарковый прогон для приближения к целевому ускорению 12–18×
* [ ] `proof_batch._prepare_cohort` — общий `CalendarData` уже передаётся; кэширование самого экземпляра опционально

## 7. Состав変更 (коммит `f568c8a`)

* `src/moex_spread_lab/position_sizing.py` — `metrics_only` в `size_proof`, исправление `funding_rub` в журнале, `notes`/`run_id` только вне `metrics_only`
* `src/moex_spread_lab/proof.py` — `reference_eval()`, `ProofService(calendar=...)`, shared `CalendarData`, частичные calendar-реплеи с `universe`/`run_id`
* `src/moex_spread_lab/proof_batch.py` — передача `calendar=service._calendar` в `_prepare_cohort`
* `src/moex_spread_lab/proof_search.py` — `StudyWindow.full` `cached_property`
* `src/moex_spread_lab/proof_strategy_search.py` — `CalendarData` через сервис, поддержка timeframe в `_families`
* `src/moex_spread_lab/proof_windows.py`, `screener_app.py`, `w0.py` — мелочи wiring
* `tests/test_perf_equivalence.py` — новый набор тестов (Iteration 0)

Принято решение: качество не деградирует; Optuna-траектория, IEEE-754 арифметика, `max_affordable`, риск/допуск, `canonical reason`, `plateau` и `run_id` сохраняются точно.

---

AGENTS.md: проект — локальная разработка на macOS, ответы на русском, outcome-first, кратко. Документ хранится в репозитории и не содержит секретов или датасетов.
