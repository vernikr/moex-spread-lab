"""Validated pair-plugin unit intents and funded sizing for proof replays.

Daily perpetual windows require near-leg funding evidence; hourly windows follow
native proof policy and explicitly exclude daily funding, not interpolate it.
Only trusted offline inputs are accepted.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import date as date
from datetime import datetime as datetime
from typing import TYPE_CHECKING, Any, NotRequired, Protocol, TypedDict, Unpack, cast

from moex_spread_lab.accounting.position_sizing import size_proof
from moex_spread_lab.core.contracts import InstrumentContext, StrategyPlugin, WindowSnapshot
from moex_spread_lab.core.settings import ResearchSettings
from moex_spread_lab.core.w0 import W0Error, run_id
from moex_spread_lab.strategies.pair_screener.intents import pair_price_intent_trades

from . import pair_funding_replay


def _choose_hook[T](public: T, private: T, original: T) -> T:
    return private if private is not original else public

def _is_runtime_instance(
    value: object, expected_type: type[object]
) -> bool:
    return isinstance(value, expected_type)


def _has_non_mapping_rows(
    rows: Iterable[object],
) -> bool:
    return any(not isinstance(row, Mapping) for row in rows)


def _eligible(
    row: Mapping[str, Any],
) -> bool:
    values = [row.get("near_volume"), row.get("far_volume")]
    if any(
        value is not None
        and (type(value) not in (int, float) or not math.isfinite(value) or value < 0)
        for value in values
    ):
        raise ValueError
    return all(value is not None and value > 0 for value in values)


def valid_date_key_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
    key: object, daily: bool
) -> bool:
    if not isinstance(key, str):
        return False
    if daily:
        return len(key) == 10 and date.fromisoformat(key).isoformat() == key
    return len(key) == 19 and datetime.fromisoformat(key).isoformat(timespec="seconds") == key


def _validated_rows(
    window: WindowSnapshot,
    params: dict[str, Any],
    benchmark_marks: Mapping[str, float],
    settings: ResearchSettings,
    through: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    is_instance = _is_runtime_instance
    window_input: object = window
    if not is_instance(window_input, WindowSnapshot):
        raise W0Error("Для расчёта требуется корректное дневное окно пары.")
    params_input: object = params
    marks_input: object = benchmark_marks
    settings_input: object = settings
    if (
        window.timeframe not in ("1d", "1h")
        or not is_instance(settings_input, ResearchSettings)
        or settings.timeframe != window.timeframe
        or not is_instance(params_input, dict)
        or not window.near
        or not window.far
        or window.near == window.far
        or not is_instance(marks_input, Mapping)
        or not window.rows
        or _has_non_mapping_rows(
            window.rows
        )
    ):
        raise W0Error("Для расчёта требуется корректное дневное окно пары.")
    try:
        dates: list[str] = [row["date"] for row in window.rows]
        if (
            any(
                not valid_date_key_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
                    day, window.timeframe == "1d"
                )
                for day in dates
            )
            or dates != sorted(set(dates))
            or (
                through is not None
                and not valid_date_key_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
                    through, len(through) == 10
                )
            )
        ):
            raise ValueError
        prefix: list[dict[str, Any]] = [
            row
            for row in window.rows
            if through is None
            or (row["date"][:10] if len(through) == 10 else row["date"]) <= through
        ]

        analysis_rows = [
            row
            for row in prefix
            if _eligible(
                row
            )
        ]
        if len(analysis_rows) < 2:
            raise ValueError
    except (ValueError, KeyError, TypeError, OverflowError):
        raise W0Error("Некорректный дневной период или объём пары.") from None
    return prefix, analysis_rows


def positive_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting(
    value: Any,
) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError
    return float(value)


def _priced_rows(
    window: WindowSnapshot,
    prefix: list[dict[str, Any]],
    benchmark_marks: Mapping[str, float],
) -> tuple[list[dict[str, Any]], float]:
    positive = positive_p72_packages_proof_src_moex_spread_lab_proof_runtime_pair_plugin_accounting
    try:
        legs: list[Mapping[str, Any]] = [
            cast(Mapping[str, Any], window.contracts[window.near]),
            cast(Mapping[str, Any], window.contracts[window.far]),
        ]
        multipliers: list[float] = [
            positive(contract["tick_value"]) / positive(contract["tick_size"]) for contract in legs
        ]
        lot = positive(legs[0]["lot_size"])
        if not all(math.isfinite(mult) and mult > 0 for mult in multipliers):
            raise ValueError
        rows: list[dict[str, Any]] = []
        for raw in prefix:
            near, far = positive(raw["near_close"]), positive(raw["far_close"])
            near_open, far_open = positive(raw["near_open"]), positive(raw["far_open"])
            spread = near * multipliers[0] - far * multipliers[1]
            if not math.isfinite(spread) or not math.isfinite(
                near_open * multipliers[0] - far_open * multipliers[1]
            ):
                raise ValueError
            positive(benchmark_marks[raw["date"]])
            rows.append(
                {
                    "date": raw["date"],
                    "timestamp": raw.get("timestamp", raw["date"]),
                    "session_date": raw.get("session_date", raw["date"]),
                    "benchmark_mark_date": raw.get("benchmark_mark_date"),
                    "near": near,
                    "far": far,
                    "near_open": near_open,
                    "far_open": far_open,
                    "spread": round(spread, 8),
                    "volume_eligible": _eligible(
                        raw
                    ),
                }
            )
    except (TypeError, KeyError, ValueError, OverflowError, ZeroDivisionError):
        raise W0Error("Нет достоверных цен или параметров обеих ног пары.") from None
    return rows, lot

_COMPATIBILITY_HOOKS = (_eligible, _has_non_mapping_rows)

if TYPE_CHECKING:
    from moex_spread_lab.strategies.funding_carry.proof_funding import FundingWindow


class _DailyFundingKeywords(TypedDict):
    near_lot_size: float
    through: NotRequired[str | None]


class _DailyUnitFundingAttacher(Protocol):
    def __call__(
        self,
        rows: Sequence[Mapping[str, Any]],
        trades: Sequence[Mapping[str, Any]],
        window: FundingWindow,
        **_kwargs: Unpack[_DailyFundingKeywords],
    ) -> dict[str, Any]: ...


_ATTACH_DAILY_UNIT_FUNDING: _DailyUnitFundingAttacher = cast(
    _DailyUnitFundingAttacher,
    pair_funding_replay.attach_daily_unit_funding,
)


# Preserve both the historical public binding and the newer private hook.
attach_daily_unit_funding = _ATTACH_DAILY_UNIT_FUNDING
_original_attach = _ATTACH_DAILY_UNIT_FUNDING
_run_id = _original_run_id = run_id


def _daily_funding_journal(
    window: WindowSnapshot,
    funding_window: FundingWindow,
    rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    through: str | None,
    lot: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from moex_spread_lab.data import universe as catalog_universe
    from moex_spread_lab.strategies.funding_carry.proof_funding import FundingWindow

    declared_pairs = cast(Sequence[Mapping[str, str]], catalog_universe.PAIRS)
    declared = next((pair for pair in declared_pairs if pair["pair_id"] == window.pair), None)
    if declared is not None and (
        declared["near"] != window.near
        or declared["far"] != window.far
        or declared["family"] != "perpetual_quarterly_same_asset"
    ):
        raise W0Error("История фондирования неприменима к этой паре.")
    funding_window_input: object = funding_window
    if (
        not _is_runtime_instance(funding_window_input, FundingWindow)
        or funding_window.book.secid != window.near
    ):
        raise W0Error("История фондирования не соответствует ближней ноге пары.")
    eligible_rows = [row for row in rows if row["volume_eligible"]]
    prepared = _choose_hook(
        attach_daily_unit_funding, _ATTACH_DAILY_UNIT_FUNDING, _original_attach
    )(eligible_rows, trades, funding_window_input, through=through, near_lot_size=lot)
    prepared_series = cast(list[dict[str, Any]], prepared["series"])
    by_date: dict[str, dict[str, Any]] = {cast(str, row["date"]): row for row in prepared_series}
    journal = [
        by_date.get(
            cast(str, row["date"]),
            {**row, "funding_rub": None, "funding_contracts": 0},
        )
        for row in rows
    ]
    return prepared, journal


def _funding_journal(
    window: WindowSnapshot,
    funding_window: FundingWindow | None,
    rows: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    through: str | None,
    lot: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if window.timeframe == "1h":
        if funding_window is not None:
            raise W0Error("Дневную ставку нельзя применять к каждому часовому бару.")
        from moex_spread_lab.strategies.pair_screener.pair_screener import _pair

        declared = _pair(window.pair)
        if declared["near"] != window.near or declared["far"] != window.far:
            raise W0Error("Идентификатор пары не совпадает с ногами.")
        prepared: dict[str, Any] = {
            "provenance": {"funding_applicability": "intraday_not_included"}
        }
        journal: list[dict[str, Any]] = [
            {**row, "funding_rub": None, "funding_contracts": 0} for row in rows
        ]
    elif funding_window is None:
        # Only known dated-contract pairs may omit rates; a missing perpetual
        # funding source must not silently become zero.
        from moex_spread_lab.strategies.pair_screener.pair_screener import _pair

        declared = _pair(window.pair)
        if (
            declared["near"] != window.near
            or declared["far"] != window.far
            or declared["family"] not in {"dated_calendar_same_asset", "cross_contract_same_expiry"}
        ):
            raise W0Error("Для вечного фьючерса требуется история фондирования.")
        prepared = {"provenance": {"funding_applicability": "no_perpetual_leg"}}
        journal = [{**row, "funding_rub": None, "funding_contracts": 0} for row in rows]
    else:
        return _daily_funding_journal(window, funding_window, rows, trades, through, lot)
    return prepared, journal


def prepare_daily_pair_unit(
    plugin: StrategyPlugin,
    window: WindowSnapshot,
    params: dict[str, Any],
    fill: str,
    funding_window: FundingWindow | None,
    benchmark_marks: Mapping[str, float],
    settings: ResearchSettings,
    *,
    through: str | None = None,
    enforce_compatibility: bool = True,
    isolate: bool = True,
    memo: dict[str | None, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, float]]:
    """Validate decisions and size them against resolved funding and one account.

    Zero-volume rows stay in the journal without revaluation or funding. Only the
    eligible prefix reaches the plugin; no later bars or benchmark marks are used.
    Inputs are not mutated; plugin-supplied money fields are ignored.
    Daily perpetual funding evidence must match the near contract. Missing daily
    evidence is allowed only for registry-confirmed non-perpetual pairs; hourly
    prices follow native proof and omit funding rather than invent clearing times.
    `isolate=False` (first-party built-ins only) skips the defensive input copies.
    A request-scoped `memo` (first-party built-ins only) reuses the validated and
    priced rows of each cut-off across trials and leaves the unit `run_id` unset:
    built-in payloads derive their identity from the full replay, not the unit.
    """
    window_rows = memo.get(through) if memo is not None else None
    if window_rows is None:
        prefix, analysis_rows = _validated_rows(window, params, benchmark_marks, settings, through)
        window_rows = (prefix, analysis_rows, *_priced_rows(window, prefix, benchmark_marks))
        if memo is not None:
            memo[through] = window_rows
    prefix, analysis_rows, rows, lot = window_rows
    # Do not hand a plugin the original snapshot: benchmark/funding/extra fields
    # may contain future observations or unvalidated external data.
    analysis_window = WindowSnapshot(
        universe=window.universe,
        pair=window.pair,
        timeframe=window.timeframe,
        asset=window.asset,
        rows=deepcopy(analysis_rows) if isolate else analysis_rows,
        contracts=deepcopy(window.contracts) if isolate else window.contracts,
        near=window.near,
        far=window.far,
        initial_capital_rub=settings.initial_capital_rub,
        slippage_ticks=settings.slippage_ticks,
    )
    context = InstrumentContext(
        universe=window.universe, pair=window.pair, timeframe=window.timeframe
    )
    validated = plugin.validate_parameters(deepcopy(params) if isolate else params, context)
    validated_input: object = validated
    if not _is_runtime_instance(validated_input, dict):
        raise W0Error("Плагин вернул некорректные параметры.")
    if enforce_compatibility:
        verdict = plugin.check_compatibility(deepcopy(analysis_window), settings)
        if not verdict.compatible:
            raise W0Error(verdict.reason or "Стратегия несовместима с окном.")
    trades = pair_price_intent_trades(plugin, analysis_window, validated, fill, isolate=isolate)
    if any(
        not math.isfinite(trade[event]["spread_fill"])
        for trade in trades
        for event in ("entry", "exit")
    ):
        raise W0Error("Переполнение цены исполнения пары.")
    prepared, journal = _funding_journal(window, funding_window, rows, trades, through, lot)
    marks: dict[str, float] = {
        cast(str, row["date"]): benchmark_marks[cast(str, row["date"])] for row in rows
    }
    selection: dict[str, Any] = {
        "universe": window.universe,
        "pair": window.pair,
        "strategy": plugin.strategy_id,
        "timeframe": window.timeframe,
        "fill": fill,
        "parameters": validated,
    }
    try:
        identity = (
            None
            if memo is not None
            else _choose_hook(run_id, _run_id, _original_run_id)(
                {
                    "selection": selection,
                    "series": journal,
                    "trades": trades,
                    "contracts": window.contracts,
                    "benchmark_marks": marks,
                    "funding_provenance": prepared["provenance"],
                }
            )
        )
    except (TypeError, ValueError, OverflowError):
        raise W0Error("Некорректные данные идентичности расчёта.") from None
    unit: dict[str, Any] = {
        "run_id": identity,
        "universe": window.universe,
        "selection": selection,
        "instrument": {"near": window.near, "far": window.far},
        "contract": {},
        "window": {},
        "series": journal,
        "trades": trades,
        "provenance": prepared["provenance"],
        "summary": {},
        "notes": [],
    }
    return unit, marks


def size_daily_pair_plugin(
    plugin: StrategyPlugin,
    window: WindowSnapshot,
    params: dict[str, Any],
    fill: str,
    funding_window: FundingWindow | None,
    benchmark_marks: Mapping[str, float],
    settings: ResearchSettings,
    *,
    through: str | None = None,
    enforce_compatibility: bool = True,
) -> dict[str, Any]:
    unit, marks = prepare_daily_pair_unit(
        plugin,
        window,
        params,
        fill,
        funding_window,
        benchmark_marks,
        settings,
        through=through,
        enforce_compatibility=enforce_compatibility,
    )
    return size_proof(unit, marks, settings, cast(dict[str, Any], window.contracts))
