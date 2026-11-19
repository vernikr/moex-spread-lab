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
* [x] Iteration 2 — upstream `metrics_only` в генераторах replay (`proof.py`, `analytics.py`, `carry.py`, `calendar_spreads.py`, `s3_reference.py`), z-score/through/series кэширование на титрах, `MOEX_PROOF_REFERENCE_EVAL` по умолчанию `"0"`, `(resolved_path, st_mtime_ns, st_size)` кэш для `carry.fetch_history`
* [x] Iteration 3 — долгоживущий `spawn`-пул (`CohortWorkerPool`) с `Sticky Sharding`, `LPT`-балансировкой, двухфазным `Canary + Fan-Out`, пакетным `plateau` / `report` / `/api/proof/windows`, кооперативной отменой и кэшем JSON-метаданных (`_benchmark_marks`, `_load_instruments`, `_sha256_bytes`)
* [x] Бит-в-бит эквивалентность подтверждена (тесты + реальные датасеты `CNYRUBF 1h` 17 пар и все воркер-конфигурации `1/4/6/8/10`)
* [ ] Опционально: дальнейшая оптимизация холодного `_intraday_windows_for` в `catalog(1h)`

## 7. Состав изменений (Итерация 1, коммит `f568c8a`)

* `src/moex_spread_lab/position_sizing.py` — `metrics_only` в `size_proof`, исправление `funding_rub` в журнале, `notes`/`run_id` только вне `metrics_only`
* `src/moex_spread_lab/proof.py` — `reference_eval()`, `ProofService(calendar=...)`, shared `CalendarData`, частичные calendar-реплеи с `universe`/`run_id`
* `src/moex_spread_lab/proof_batch.py` — передача `calendar=service._calendar` в `_prepare_cohort`
* `src/moex_spread_lab/proof_search.py` — `StudyWindow.full` `cached_property`
* `src/moex_spread_lab/proof_strategy_search.py` — `CalendarData` через сервис, поддержка timeframe в `_families`
* `src/moex_spread_lab/proof_windows.py`, `screener_app.py`, `w0.py` — мелочи wiring
* `tests/test_perf_equivalence.py` — новый набор тестов (Iteration 0)

Принято решение: качество не деградирует; Optuna-траектория, IEEE-754 арифметика, `max_affordable`, риск/допуск, `canonical reason`, `plateau` и `run_id` сохраняются точно.

## 8. Итерация 2 — сквозной fast-path симуляции и кэширование (готово)

### 8.1 Что оптимизировано

Профилирование (`cProfile`) на реальных данных Mac показало, что после Итерации 1 основное время в 1h/1d поиске тратилось **до** `size_proof` — на построение полных презентационных payload (`_clean`, `attach_entry_levels`, `json.dumps` для `_run_id`, `_price_ledger`, `_rel_pct`), повторный расчёт rolling z-score (`_rolling_stats` / `statistics.stdev`), повторную фильтрацию `through`, `deepcopy` в `size_proof`, `ThreadPoolExecutor` над локальными JSON и некэшированные чтения EOD-кэша `carry`.

* `src/moex_spread_lab/analytics.py`:
  * Добавлен `compute_rolling_z(spread_ticks, lookback, decision_delay_bars)` (точная арифметика `_rolling_stats` с list comprehension вместо генератора — 100% бит-в-бит).
  * `run_mean_reversion_baseline(..., metrics_only=False, precomputed_z=None)` — использует кэшированный `precomputed_z`, а при `metrics_only and sizing_intents` пропускает расчёт комиссий/LQDT/дневного скругления.
* `src/moex_spread_lab/s3_reference.py`:
  * `zscore_trades`: кэш `z_cache` по `(z_cache_key, window, include_current)`, префиксные суммы `signal_eligible`, кэш парсинга дат для `max_hold`.
  * `account_trades`: принимает предсортированный `benchmark_dates`.
* `src/moex_spread_lab/carry.py`:
  * Выделенный ограниченный LRU-кэш `_HISTORY_JSON_CACHE` (4096 записей) и `_BENCHMARK_PARSED_CACHE` (64 записи) по `(resolved_path, st_mtime_ns, st_size)`.
  * В `build_carry_strategy(..., metrics_only=False)`: пропуск `statistics.median` для `strategy_variant == "funding_only"`, а при `metrics_only=True` пропуск `assess_vtb_pair`, `_benchmark_rows`, `lqdt_overlay_values` и тяжёлой `daily`-серии.
* `src/moex_spread_lab/calendar_spreads.py`:
  * `fetch_inputs`: при `opener is not None` (локальный `_offline` режим) не создаёт `ThreadPoolExecutor(max_workers=1)` на 1770 futures.
  * `_load()` и `_intraday_window()`: предсортированный `benchmark_dates`, кэш `date.fromisoformat(session_day)`.
  * `proof()` и `_proof_payload()`: request-scoped `_cache` (`through`, `h1`, `daily_eligible`, `z`, `series`), пропуск `_clean`/`notes`/`_rel_pct`/`ledger` при `metrics_only=True`.
* `src/moex_spread_lab/position_sizing.py`:
  * `size_proof(..., metrics_only=False, copy_unit=True)`: при `metrics_only=True` не делает `deepcopy`, не мутирует строки `series` (безопасное переиспользование `series` между титрами), строит 4-польный `ledger`, пропускает `summary`/`_cost_totals`/`notes`/`_run_id`; `copy_unit=False` для свежих unit из `sized_replay`.
* `src/moex_spread_lab/proof.py` и `src/moex_spread_lab/proof_search.py`:
  * `reference_eval()` теперь по умолчанию `"0"` (opt-out: `MOEX_PROOF_REFERENCE_EVAL=1`).
  * Контракт прямого `replay(req, through=cutoff)` сохранён: `metrics_only=False` по умолчанию; `metrics_only=True` включает только `StudyWindow.proof(..., training=True)` при `not reference_eval()`.
  * В `ProofService._prepare_pair` / `_mean_reversion_proof` / `_funding_proof` / `prepare`: request-scoped кэширование `through`-срезов, `marks`, `z_cache`, `signal_dates`, неизменяемых `base_series` и `window_meta`, пропуск `_clean`/`attach_entry_levels`/`_run_id` на обучающих титрах.

### 8.2 Замеры на реальных данных (MacBook Pro 16" 2019, i7-9750H, CPython 3.12.14)

Сравнение на реальных кэшах `data/` (`validation_percent=20`, `allow_failed_volume=True`, `allow_failed_capital=True`, 2 инструмента на сценарий):

| Сценарий (2 инструмента) | Итерация 1 (`cProfile`) | Итерация 2 (`ref=1`, с кэшами) | Итерация 2 (`ref=0`, fast-path по умолчанию) |
|--------------------------|-------------------------|---------------------------------|----------------------------------------------|
| `catalog(1h)` холодный | 12.575 с (`cProfile`) | — | **7.710 с** (wall-clock) |
| `PAIR 1h` (`CNYRUBF/CRZ6`, `GLDRUBF/GLZ6`) | 84.422 с (`cProfile`) | 29.692 с | **4.979 с** (**5.96×** к `ref=1`, **~17×** к `cProfile` Итер. 1) |
| `CALENDAR 1h` (`SRZ4/SRH5`, `SRH5/SRM5`, холодный `_load`) | 46.568 с (`cProfile`) | 8.317 с | **4.636 с** (**1.79×** к `ref=1`, **~10×** к `cProfile` Итер. 1) |
| `PAIR 1d` (`CNYRUBF/CRZ6`, `IMOEXF/MMZ6`, `optimize_strategy=True`) | 18.228 с (`cProfile`) | 9.384 с | **4.474 с** (**2.10×** к `ref=1`, доминирует Optuna TPE ~3 с) |
| `CAL 1d` (`SRM3/SRU3`, `SRU3/SRZ3`, холодный `_load`) | 5.780 с (`cProfile`) | 2.148 с | **2.471 с** (доминирует Optuna TPE + холодный `_load`) |

Бит-в-бит эквивалентность (`ref=1` == `ref=0`) подтверждена как на синтетическом наборе `tests/test_perf_equivalence.py`, так и на всех 4 реальных прогонах `data/`.

## 9. Итерация 3 — `spawn`-пул воркеров (`CohortWorkerPool`) и пакетный параллелизм (готово)

### 9.1 Архитектура

* `src/moex_spread_lab/proof.py`:
  * `_sha256_bytes`, `_benchmark_marks`, `_load_instruments` переведены на `read_hashed_json` + `_HashedJSONCache` по `(resolved_path, st_mtime_ns, st_size)` — исключено повторное чтение `current-intake.json` / `historical-contracts.json` / `lqdt-lineage.json` в когорте и в воркерах.
  * `ProofService.worker_pool()` / `close()` — ленивое владение пулом `CohortWorkerPool`.
* `src/moex_spread_lab/proof_pool.py`:
  * Долгоживущий `spawn`-пул (`multiprocessing.get_context("spawn")`, по 1 `ProcessPoolExecutor(max_workers=1)` на слот для явной привязки шардов). По умолчанию `min(8, os.cpu_count() or 6)` воркеров (переопределяется через `MOEX_PROOF_WORKERS`).
  * Включается при `N >= 3` валидных окнах и достаточном объёме (`estimated_bars >= 1500` или `N >= 4`; либо `MOEX_PROOF_FORCE_POOL=1`).
  * **LPT-балансировка (`_estimate_weight`)** по размеру файлов `1h`-свечей с сортировкой каждого шарда по каноническому `idx` и **`Sticky Sharding`**: `StudyWindow` создаётся один раз внутри своего воркера (`_worker_init_cohort`) и остаётся в `_WORKER_COHORTS[cohort_id]` на весь поиск, `plateau` и `report`.
  * **Двухфазный `Canary + Fan-Out` (`eval_point`)**: сначала проверяется `canary_idx` (1 IPC); при отказе `canary == 0` — мгновенный short-circuit без пробуждения остальных воркеров; при `canary > 0` проверяются только индексы `< canary`, гарантируя канонический `reason` минимального отказавшего индекса.
  * **Пакетный `plateau` (`eval_plateau_batch`)**, параллельный финальный `report_cohort` и параллельный `/api/proof/windows` (`window_summaries`).
  * **Кооперативная отмена (`_await_futures`)**: поллинг `wait(..., timeout=0.05)` с `check_cancelled()`, немедленная отмена futures и освобождение `release_cohort` в `finally`, `ScreenerStore.close()` и `atexit`.
* `src/moex_spread_lab/proof_batch.py`, `proof_strategy_search.py`, `proof_windows.py`, `screener_app.py`:
  * Интеграция `_RemoteCohort`, гарантированное закрытие когорт в `finally` (включая `strategy_search` по всем семействам), авто-fallback на однопоточный путь при моках `StudyWindow`/`CalendarData`.

### 9.2 Замеры на реальных данных (MacBook Pro 16" 2019, i7-9750H 6C/12T, 16 GB RAM)

#### 17 готовых пар `CNYRUBF 1h` (`validation_percent=20`, ~50.5 тыс. баров)

Все конфигурации `w = 1, 4, 6, 8, 10` дали **побитово идентичный SHA-256** (`0965a34d3fb3520c`) по `optimize`, `checked_plateau` и `window_summaries`:

| Воркеры (`MOEX_PROOF_WORKERS`) | `optimize` (cold pool) | `optimize` (warm pool) | `checked_plateau` | `window_summaries` |
|--------------------------------|------------------------|------------------------|-------------------|--------------------|
| `1` (последовательно, Итер. 2) | 53.746 с (ранее 23.18 с без доп. нагрузки) | 53.463 с | 13.522 с | 12.847 с |
| `4` | 22.022 с | 20.051 с | 4.443 с | 3.361 с |
| `6` | 16.942 с | 11.617 с | 2.116 с | 1.237 с |
| **`8` (дефолт)** | **6.635 с** | **5.654 с** | **1.320 с** | **0.860 с** |
| `10` | 7.159 с | 5.950 с | 1.618 с | 1.469 с |

*(Примечание: исходный прогон до Итерации 1 занимал ~380 с — итоговое ускорение **>55–67×**.)*

#### Полная когорта из всех `110` готовых пар `pair_screen 1h`

| Конфигурация | `optimize` (110 пар) | `checked_plateau` | `window_summaries` (110 окон) | Суммарный RSS (родитель + воркеры) |
|--------------|----------------------|-------------------|-------------------------------|------------------------------------|
| `w = 6` | 30.694 с | 11.651 с | 10.169 с | 3.35 GB |
| **`w = 8` (дефолт)** | **22.088 с** | **5.878 с** | **5.890 с** | 4.25 GB (при 16 GB RAM) |

---

AGENTS.md: проект — локальная разработка на macOS, ответы на русском, outcome-first, кратко. Документ хранится в репозитории и не содержит секретов или датасетов.
