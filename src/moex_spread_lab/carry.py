"""Read-only MOEX EOD history and funding-aware CNY carry decomposition."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from bisect import bisect_left
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import urlopen

from .account_risk import assess_vtb_pair
from .analytics import load_contracts
from .costs import side_cost, tariff_profile
from .portfolio import lqdt_overlay_values
from .w0 import W0Error


CARRY_SCHEMA = "carry-study.v1"
HISTORY_SCHEMA = "iss-history.v1"
STRATEGY_SCHEMA = "carry-strategy.v1"
HISTORY_PAGE_SIZE = 100
FUNDING_REGIMES = (
    {
        "id": "cnyrubf-k1-003-k2-035",
        "effective_from": "2023-08-22",
        "k1": 0.0003,
        "k2": 0.0035,
        "source": "MOEX notice n63166 and perpetual-futures page",
    },
    {
        "id": "cnyrubf-k1-000-k2-035",
        "effective_from": "2026-01-19",
        "k1": 0.0,
        "k2": 0.0035,
        "source": "MOEX notice n96856 and perpetual-futures page",
    },
)
HISTORY_FIELDS = (
    "BOARDID", "TRADEDATE", "SECID", "CLOSE", "SETTLEPRICE", "SWAPRATE",
    "OPENPOSITION", "VALUE", "VOLUME", "WAPRICE", "NUMTRADES", "ASSETCODE",
)


class CarryError(W0Error):
    """Raised when EOD carry inputs fail the funding-aware study contract."""


class CarryStrategyError(CarryError):
    """Raised when a frozen carry strategy cannot be evaluated safely."""


def _round(value: float) -> float:
    return round(float(value), 8)


def _funding_regime(date_key: str, regimes: tuple[dict[str, Any], ...]) -> dict[str, Any] | None:
    if not regimes:
        return None
    regime = regimes[0]
    for candidate in regimes:
        if date_key >= candidate["effective_from"]:
            regime = candidate
    return regime


def _fetch_json(url: str, *, timeout: float = 30.0, opener: Callable[..., Any] = urlopen) -> tuple[dict[str, Any], str]:
    try:
        with opener(url, timeout=timeout) as response:
            raw = response.read()
    except OSError as exc:
        raise CarryError(f"history request failed: {url}: {exc}") from exc
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CarryError(f"history response is not JSON: {url}") from exc
    if not isinstance(value, dict):
        raise CarryError(f"history response is not an object: {url}")
    return value, hashlib.sha256(raw).hexdigest()


def _rows(block: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(block, dict) or not isinstance(block.get("columns"), list) or not isinstance(block.get("data"), list):
        raise CarryError(f"malformed ISS block: {name}")
    return [dict(zip(block["columns"], values)) for values in block["data"] if isinstance(values, list)]


def _history_url(secid: str, from_date: str, till_date: str, start: int, fields: tuple[str, ...] = HISTORY_FIELDS) -> str:
    query = urlencode({
        "iss.meta": "off",
        "from": from_date,
        "till": till_date,
        "start": start,
        "limit": HISTORY_PAGE_SIZE,
        "history.columns": ",".join(fields),
    })
    return f"https://iss.moex.com/iss/history/engines/futures/markets/forts/securities/{secid}.json?{query}"


def fetch_history(
    secid: str,
    from_date: str,
    till_date: str,
    *,
    cache_dir: Path | None = None,
    delay_seconds: float = 0.1,
    timeout: float = 30.0,
    opener: Callable[..., Any] = urlopen,
    fields: tuple[str, ...] = HISTORY_FIELDS,
) -> dict[str, Any]:
    if not secid or delay_seconds < 0:
        raise CarryError("invalid history arguments")
    all_rows: list[dict[str, Any]] = []
    hashes: list[str] = []
    page = 0
    total: int | None = None
    while total is None or len(all_rows) < total:
        start = page * HISTORY_PAGE_SIZE
        url = _history_url(secid, from_date, till_date, start, fields)
        cache_path = cache_dir / f"{secid}-{from_date}-{till_date}-{start}.json" if cache_dir else None
        payload = None
        raw_hash = None
        if cache_path and cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cached, dict) and cached.get("url") == url:
                    payload = cached.get("payload")
                    raw_hash = cached.get("raw_response_sha256")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                payload = None
        if payload is None:
            if delay_seconds:
                time.sleep(delay_seconds)
            payload, raw_hash = _fetch_json(url, timeout=timeout, opener=opener)
            if cache_path:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(json.dumps({
                    "url": url,
                    "payload": payload,
                    "raw_response_sha256": raw_hash,
                }, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        block_rows = _rows(payload.get("history"), "history")
        cursor_rows = _rows(payload.get("history.cursor"), "history.cursor") if payload.get("history.cursor") else []
        if cursor_rows and cursor_rows[0].get("TOTAL") is not None:
            total = int(cursor_rows[0]["TOTAL"])
        if not block_rows:
            break
        all_rows.extend(block_rows)
        hashes.append(raw_hash or "")
        page += 1
        if len(block_rows) < HISTORY_PAGE_SIZE:
            break
    deduped = {row.get("TRADEDATE"): row for row in all_rows if row.get("TRADEDATE")}
    rows = [deduped[key] for key in sorted(deduped)]
    if not rows:
        raise CarryError(f"empty EOD history for {secid}")
    return {
        "schema_version": HISTORY_SCHEMA,
        "secid": secid,
        "from": from_date,
        "till": till_date,
        "row_count": len(rows),
        "page_count": page,
        "rows": rows,
        "source": {
            "endpoint": "MOEX ISS futures history",
            "raw_response_sha256": hashes,
            "rights_status": "accepted_project_assumption_personal_use",
        },
    }


def _normalize_history(source: dict[str, Any], secid: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if source.get("schema_version") != HISTORY_SCHEMA or source.get("secid") != secid:
        raise CarryError(f"unsupported history input for {secid}")
    normalized = {}
    for row in source.get("rows", []):
        if row.get("TRADEDATE") is None or row.get("SETTLEPRICE") is None:
            continue
        try:
            settle = float(row["SETTLEPRICE"])
            swaprate = float(row["SWAPRATE"]) if row.get("SWAPRATE") is not None else None
        except (TypeError, ValueError) as exc:
            raise CarryError(f"non-numeric history field for {secid} {row.get('TRADEDATE')}") from exc
        normalized[row["TRADEDATE"]] = {
            "date": row["TRADEDATE"],
            "settle": settle,
            "swaprate": swaprate,
            "open_position": row.get("OPENPOSITION"),
            "volume": row.get("VOLUME"),
            "value": row.get("VALUE"),
        }
    if len(normalized) < 2:
        raise CarryError(f"fewer than two settlement rows for {secid}")
    return normalized, {
        "secid": secid,
        "source": source.get("source", {}),
        "row_count": len(normalized),
        "first_date": min(normalized),
        "last_date": max(normalized),
    }


def build_carry_study(
    near_source: dict[str, Any],
    far_source: dict[str, Any],
    contracts_path: Path,
    *,
    near: str = "CNYRUBF",
    far: str = "CRZ6",
    funding_regimes: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    near_rows, near_provenance = _normalize_history(near_source, near)
    far_rows, far_provenance = _normalize_history(far_source, far)
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    near_contract = contracts[near]
    far_contract = contracts[far]
    common_dates = sorted(set(near_rows) & set(far_rows))
    if len(common_dates) < 2:
        raise CarryError("fewer than two common EOD settlement dates")
    near_multiplier = near_contract["tick_value"] / near_contract["tick_size"]
    far_multiplier = far_contract["tick_value"] / far_contract["tick_size"]
    regimes = FUNDING_REGIMES if funding_regimes is None and near == "CNYRUBF" else (funding_regimes or ())
    rows = []
    cumulative_mark = 0.0
    cumulative_funding = 0.0
    previous_date = None
    for date_key in common_dates:
        near_row = near_rows[date_key]
        far_row = far_rows[date_key]
        if previous_date is None:
            daily_basis_pnl = 0.0
        else:
            daily_basis_pnl = (
                (far_row["settle"] - far_rows[previous_date]["settle"]) * far_multiplier
                - (near_row["settle"] - near_rows[previous_date]["settle"]) * near_multiplier
            )
        swaprate = near_row["swaprate"]
        if swaprate is None:
            raise CarryError(f"missing {near} SWAPRATE on {date_key}")
        funding_received = 0.0 if previous_date is None else swaprate * near_contract["lot_size"]
        regime = _funding_regime(date_key, regimes)
        funding_cap_per_unit = (
            None
            if previous_date is None or regime is None or regime.get("k2") is None
            else regime["k2"] * near_rows[previous_date]["settle"]
        )
        cap_status = "not_modelled" if regime is None else "not_applicable_initial_observation"
        if funding_cap_per_unit is not None:
            cap_status = "within_rounded_k2_cap" if abs(swaprate) <= funding_cap_per_unit + 0.00001 else "above_k2_cap_review_required"
        cumulative_mark += daily_basis_pnl
        cumulative_funding += funding_received
        rows.append({
            "date": date_key,
            "near_settle": near_row["settle"],
            "far_settle": far_row["settle"],
            "basis_far_minus_near": _round(far_row["settle"] - near_row["settle"]),
            "daily_basis_pnl_short_near_long_far": _round(daily_basis_pnl),
            "daily_funding_received_short_near": _round(funding_received),
            "daily_gross_carry_pnl": _round(daily_basis_pnl + funding_received),
            "funding_regime_id": regime["id"] if regime else "not_modelled",
            "funding_k1": regime.get("k1") if regime else None,
            "funding_k2": regime.get("k2") if regime else None,
            "funding_cap_per_unit": _round(funding_cap_per_unit) if funding_cap_per_unit is not None else None,
            "funding_cap_status": cap_status,
            "cumulative_basis_pnl": _round(cumulative_mark),
            "cumulative_funding_received": _round(cumulative_funding),
            "cumulative_gross_carry_pnl": _round(cumulative_mark + cumulative_funding),
            "swaprate_source_unit": "RUB_per_contract_day_assumed_from_R06_and_ISS_history",
            "near_volume": near_row.get("volume"),
            "far_volume": far_row.get("volume"),
        })
        previous_date = date_key
    funding_values = [row["daily_funding_received_short_near"] for row in rows]
    return {
        "schema_version": CARRY_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "study_role": "funding_aware_carry_decomposition_not_execution_backtest",
        "near": near,
        "far": far,
        "direction": "short_near_long_far",
        "price_role": "EOD_SETTLEPRICE",
        "funding_role": f"{near}_SWAPRATE_received_by_short_near",
        "rows": rows,
        "summary": {
            "common_row_count": len(rows),
            "first_date": rows[0]["date"],
            "last_date": rows[-1]["date"],
            "total_basis_pnl": rows[-1]["cumulative_basis_pnl"],
            "total_funding_received": rows[-1]["cumulative_funding_received"],
            "total_gross_carry_pnl": rows[-1]["cumulative_gross_carry_pnl"],
            "mean_daily_funding_received": _round(sum(funding_values) / len(funding_values)),
            "positive_funding_rows": sum(value > 0 for value in funding_values),
            "negative_funding_rows": sum(value < 0 for value in funding_values),
            "zero_funding_rows": sum(value == 0 for value in funding_values),
            "funding_regime_counts": {
                regime["id"]: sum(row["funding_regime_id"] == regime["id"] for row in rows)
                for regime in regimes
            },
            "funding_cap_review_rows": sum(row["funding_cap_status"] == "above_k2_cap_review_required" for row in rows),
            "transaction_costs_included": False,
            "historical_executable_fills": False,
        },
        "economics": {
            "near_lot_size": near_contract["lot_size"],
            "far_lot_size": far_contract["lot_size"],
            "tick_size": near_contract["tick_size"],
            "tick_value_rub": near_contract["tick_value"],
            "near_tick_size": near_contract["tick_size"],
            "near_tick_value_rub": near_contract["tick_value"],
            "far_tick_size": far_contract["tick_size"],
            "far_tick_value_rub": far_contract["tick_value"],
            "near_rub_per_price_unit": _round(near_multiplier),
            "far_rub_per_price_unit": _round(far_multiplier),
            "rub_per_price_unit": _round(near_multiplier),
            "historical_specification_status": "current_snapshot_fallback_assumed",
            "fixing_mismatch_risk": "CNYRUBF_TOM_vs_CRZ6_CNYFIXME",
            "funding_regimes": list(regimes),
        },
        "provenance": {
            "near_history": near_provenance,
            "far_history": far_provenance,
            "contracts_sha256": contracts_hash,
            "contracts_path": str(contracts_path),
        },
    }


def write_carry_study(result: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _benchmark_rows(path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CarryStrategyError(f"cannot read LQDT benchmark {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "lqdt-lineage.v1":
        raise CarryStrategyError("unsupported LQDT benchmark schema")
    rows = {}
    for row in payload.get("rows", []):
        if not isinstance(row, dict) or not isinstance(row.get("date"), str):
            raise CarryStrategyError("malformed LQDT benchmark row")
        try:
            price = float(row["price"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CarryStrategyError(f"invalid LQDT benchmark price on {row.get('date')}") from exc
        if price <= 0:
            raise CarryStrategyError(f"LQDT benchmark price must be positive on {row['date']}")
        rows[row["date"]] = {
            "date": row["date"],
            "price": price,
            "source_kind": row.get("source_kind", "unknown"),
            "source_ticker": row.get("source_ticker", "unknown"),
        }
    if not rows:
        raise CarryStrategyError("empty LQDT benchmark")
    return rows, hashlib.sha256(raw).hexdigest()


def _transaction_cost_profile(
    name: str,
    tick_value: float,
    *,
    near_price: float | None = None,
    far_price: float | None = None,
    near_lot_size: int = 1000,
    far_lot_size: int = 1000,
    far_tick_value: float | None = None,
) -> dict[str, Any]:
    if name in {"moex_current_taker", "vtb_online_prime_current"}:
        if near_price is None or far_price is None:
            raise CarryStrategyError("MOEX tariff scenario requires reference prices")
        rate = 0.0000462
        near_side = near_price * near_lot_size * rate
        far_side = far_price * far_lot_size * rate
        round_trip = 2 * (near_side + far_side)
        return {
            "scenario": name,
            "exchange_clearing_rate": rate,
            "vtb_broker_rate": 0.0,
            "vtb_broker_commission": 0.0,
            "vtb_tariff_effective_to": "2026-12-31",
            "near_reference_price": _round(near_price),
            "far_reference_price": _round(far_price),
            "near_leg_side_cost": _round(near_side),
            "far_leg_side_cost": _round(far_side),
            "entry_cost": _round(near_side + far_side),
            "exit_cost": _round(near_side + far_side),
            "round_trip_cost": _round(round_trip),
            "status": (
                "current_moex_rate_plus_user_confirmed_vtb_zero_derivatives_broker_fee"
                if name == "vtb_online_prime_current"
                else "current_official_moex_taker_rate_account_broker_unverified"
            ),
            "source": ["https://www.moex.com/s93", "docs/inputs/moy_online_praym.pdf"],
            "funding_component": "actual_SWAPRATE_included;_broker_and_other_costs_not_included",
        }
    profiles = {
        "low": {"exchange": 0.5, "clearing": 0.5, "broker": 0.5, "slippage_ticks": 0.5, "exit": 0.5},
        "base": {"exchange": 1.0, "clearing": 1.0, "broker": 1.0, "slippage_ticks": 1.0, "exit": 1.0},
        "stress": {"exchange": 2.0, "clearing": 2.0, "broker": 2.0, "slippage_ticks": 2.0, "exit": 2.0},
    }
    try:
        values = profiles[name].copy()
    except KeyError as exc:
        raise CarryStrategyError(f"unknown transaction cost scenario: {name}") from exc
    far_tick_value = tick_value if far_tick_value is None else far_tick_value
    leg_base = values["exchange"] + values["clearing"] + values["broker"]
    values["entry_cost"] = round(
        2 * leg_base + values["slippage_ticks"] * (tick_value + far_tick_value), 8
    )
    values["exit_cost"] = round(
        2 * leg_base
        + values["slippage_ticks"] * (tick_value + far_tick_value)
        + 2 * values["exit"],
        8,
    )
    values["round_trip_cost"] = round(values["entry_cost"] + values["exit_cost"], 8)
    values["scenario"] = name
    values["funding_component"] = "actual_SWAPRATE_included;_illustrative_funding_cost_not_added"
    values["status"] = "illustrative_unverified"
    return values


def build_carry_strategy(
    carry_result: dict[str, Any],
    benchmark_path: Path,
    contracts_path: Path,
    *,
    transaction_cost_scenario: str = "base",
    initial_capital: float = 30000.0,
    warmup_observations: int = 20,
    expiry_buffer_days: int = 5,
    assignment_window_trading_days: int = 10,
    strategy_variant: str = "basis_median",
    quantity: int = 1,
    execution_delay_rows: int = 1,
    cash_in_lqdt: bool = False,
    slippage_ticks: float = 1.0,
) -> dict[str, Any]:
    """Evaluate a frozen carry hypothesis on settlement proxies.

    `execution_delay_rows=0` fills at the settlement of the decision row; it is allowed only for the
    funding-only rule, whose signal uses funding through the previous row. A same-row exit earns that
    row's price change but not its funding (exit before the clearing that pays it). `cash_in_lqdt`
    values the account as free cash in LQDT plus margin cash (A-11).
    """
    if carry_result.get("schema_version") != CARRY_SCHEMA:
        raise CarryStrategyError("unsupported carry decomposition schema")
    rows = carry_result.get("rows")
    if not isinstance(rows, list) or len(rows) <= warmup_observations:
        raise CarryStrategyError("carry decomposition does not contain the required warmup")
    if (
        initial_capital <= 0
        or warmup_observations < 1
        or expiry_buffer_days < 0
        or assignment_window_trading_days < 1
        or strategy_variant not in {"basis_median", "funding_only"}
        or quantity < 1
        or execution_delay_rows not in (0, 1)
        or (execution_delay_rows == 0 and strategy_variant != "funding_only")
    ):
        raise CarryStrategyError("invalid carry strategy parameters")
    near = carry_result.get("near")
    far = carry_result.get("far")
    if not isinstance(near, str) or not isinstance(far, str):
        raise CarryStrategyError("carry contract identities are required")
    contracts, contracts_hash = load_contracts(contracts_path, near, far)
    far_expiry = contracts[far].get("expiry")
    try:
        expiry_date = date.fromisoformat(far_expiry)
    except (TypeError, ValueError) as exc:
        raise CarryStrategyError(f"invalid expiry for {far}") from exc
    expiry_cutoff = expiry_date - timedelta(days=expiry_buffer_days)
    observed_dates = [date.fromisoformat(row["date"]) for row in rows if date.fromisoformat(row["date"]) < expiry_date]
    assignment_window_start: date | None = None
    assignment_window_status = "not_observable_in_input_window"
    if observed_dates and observed_dates[-1] >= expiry_date - timedelta(days=15) and len(observed_dates) >= assignment_window_trading_days:
        assignment_window_start = observed_dates[-assignment_window_trading_days]
        assignment_window_status = "observed_common_eod_calendar"
    last_row = rows[-1]
    near_reference_price = float(last_row["near_settle"]) if "near_settle" in last_row else None
    far_reference_price = float(last_row["far_settle"]) if "far_settle" in last_row else None
    if transaction_cost_scenario == "tariff":
        tariff = tariff_profile(slippage_ticks)
        side = sum(side_cost(tariff, [(contracts[near], near_reference_price), (contracts[far], far_reference_price)], 1).values())
        cost_profile = {
            **tariff,
            "entry_cost": round(side, 8),
            "exit_cost": round(side, 8),
            "round_trip_cost": round(2 * side, 8),
            "near_reference_price": near_reference_price,
            "far_reference_price": far_reference_price,
        }
    else:
        cost_profile = _transaction_cost_profile(
            transaction_cost_scenario,
            contracts[near]["tick_value"],
            near_price=near_reference_price,
            far_price=far_reference_price,
            near_lot_size=contracts[near]["lot_size"],
            far_lot_size=contracts[far]["lot_size"],
            far_tick_value=contracts[far]["tick_value"],
        )
    entry_cost = cost_profile["entry_cost"] * quantity
    exit_cost = cost_profile["exit_cost"] * quantity
    account_risk = assess_vtb_pair(
        contracts_path,
        near,
        far,
        initial_capital=initial_capital,
        observed_daily_pnl=(float(row["daily_gross_carry_pnl"]) for row in rows[1:]),
        quantity=quantity,
    )
    benchmark, benchmark_hash = _benchmark_rows(benchmark_path)
    benchmark_dates = sorted(benchmark)
    if not benchmark_dates:
        raise CarryStrategyError("benchmark has no dates")

    def benchmark_mark(date_key: str) -> dict[str, Any]:
        index = bisect_left(benchmark_dates, date_key)
        if index < len(benchmark_dates) and benchmark_dates[index] == date_key:
            return {**benchmark[date_key], "mark_kind": "daily_close_same_date"}
        if index == 0:
            raise CarryStrategyError(f"benchmark has no mark on or before {date_key}")
        prior = benchmark[benchmark_dates[index - 1]]
        return {**prior, "mark_kind": "prior_daily_close"}

    start_benchmark = benchmark_mark(rows[0]["date"])
    position = False
    entry_index: int | None = None
    entry_decision_date: str | None = None
    pending: dict[str, Any] | None = None
    cumulative_gross = 0.0
    cumulative_cost = 0.0
    trade_gross = 0.0
    trades: list[dict[str, Any]] = []
    daily = []
    signals = []

    def execute(order: dict[str, Any], index: int, date_key: str) -> None:
        nonlocal position, entry_index, entry_decision_date, cumulative_cost, trade_gross
        if order["action"] == "entry":
            if position:
                raise CarryStrategyError("entry attempted while a position is open")
            position, entry_index, entry_decision_date, trade_gross = True, index, order["decision_date"], 0.0
            cumulative_cost += entry_cost
        else:
            if not position or entry_index is None or entry_decision_date is None:
                raise CarryStrategyError("exit attempted without an open position")
            cumulative_cost += exit_cost
            trades.append({
                "entry_decision_date": entry_decision_date,
                "entry_execution_date": rows[entry_index]["date"],
                "exit_decision_date": order["decision_date"],
                "exit_execution_date": date_key,
                "status": "closed_proxy_round_trip",
                "gross_pnl": round(trade_gross, 8),
                "transaction_costs": (entry_cost + exit_cost),
                "net_pnl": round(trade_gross - entry_cost - exit_cost, 8),
            })
            position, entry_index, entry_decision_date = False, None, None

    for index, row in enumerate(rows):
        date_key = row["date"]
        current_date = date.fromisoformat(date_key)
        increment = 0.0
        funding_included = 0.0
        if position and entry_index is not None and index > entry_index:
            increment = quantity * float(row["daily_gross_carry_pnl"])
            funding_included = quantity * float(row["daily_funding_received_short_near"])
            cumulative_gross += increment
            trade_gross += increment

        if pending is not None and pending["execute_index"] == index:
            execute(pending, index, date_key)
            pending = None

        if index >= warmup_observations:
            trailing = rows[index - warmup_observations:index]
            trailing_funding = statistics.fmean(float(item["daily_funding_received_short_near"]) for item in trailing)
            trailing_basis_median = statistics.median(float(item["basis_far_minus_near"]) for item in trailing)
            expiry_signal = current_date >= expiry_cutoff
            assignment_window_signal = assignment_window_start is not None and current_date >= assignment_window_start
            if position:
                exit_reason = None
                if assignment_window_signal:
                    exit_reason = "perpetual_execution_window"
                elif strategy_variant == "basis_median" and float(row["basis_far_minus_near"]) >= trailing_basis_median:
                    exit_reason = "basis_reached_trailing_median"
                elif trailing_funding <= 0:
                    exit_reason = "trailing_funding_non_positive"
                elif expiry_signal:
                    exit_reason = "expiry_buffer"
                if exit_reason and pending is None:
                    available = index + execution_delay_rows < len(rows)
                    if available:
                        pending = {"action": "exit", "execute_index": index + execution_delay_rows, "decision_date": date_key, "reason": exit_reason}
                    signals.append({"date": date_key, "action": "exit", "reason": exit_reason, "execution_available": available})
                    if available and execution_delay_rows == 0:
                        funding_today = quantity * float(row["daily_funding_received_short_near"])
                        increment -= funding_today
                        funding_included = 0.0
                        cumulative_gross -= funding_today
                        trade_gross -= funding_today
                        execute(pending, index, date_key)
                        pending = None
            elif (
                not expiry_signal
                and not assignment_window_signal
                and trailing_funding > 0
                and (strategy_variant == "funding_only" or float(row["basis_far_minus_near"]) <= trailing_basis_median)
                and pending is None
            ):
                available = index + 1 < len(rows)
                entry_reason = "positive_trailing_funding" if strategy_variant == "funding_only" else "positive_trailing_funding_and_low_basis"
                if available:
                    pending = {"action": "entry", "execute_index": index + execution_delay_rows, "decision_date": date_key, "reason": entry_reason}
                signals.append({"date": date_key, "action": "entry", "reason": entry_reason, "execution_available": available})
                if available and execution_delay_rows == 0:
                    execute(pending, index, date_key)
                    pending = None

        benchmark_mark_row = benchmark_mark(date_key)
        strategy_value = initial_capital + cumulative_gross - cumulative_cost
        benchmark_value = initial_capital * benchmark_mark_row["price"] / start_benchmark["price"]
        daily.append({
            "date": date_key,
            "position": 1 if position else 0,
            "gross_pnl_increment": round(increment, 8),
            "cumulative_gross_pnl": round(cumulative_gross, 8),
            "cumulative_transaction_costs": round(cumulative_cost, 8),
            "strategy_value_proxy": round(strategy_value, 8),
            "benchmark_value": round(benchmark_value, 8),
            "X": round(strategy_value - benchmark_value, 8),
            "benchmark_source_kind": benchmark_mark_row["source_kind"],
            "benchmark_source_ticker": benchmark_mark_row["source_ticker"],
            "benchmark_mark_kind": benchmark_mark_row["mark_kind"],
            "valuation_status": "EOD_settlement_proxy_not_executable_fill",
            "near_settle": row.get("near_settle"),
            "far_settle": row.get("far_settle"),
            "near_volume": row.get("near_volume"),
            "far_volume": row.get("far_volume"),
            "daily_basis_pnl_contract": float(row.get("daily_basis_pnl_short_near_long_far") or 0.0),
            "daily_funding_contract": float(row.get("daily_funding_received_short_near") or 0.0),
            "daily_funding_included_rub": round(funding_included, 8),
        })

    if position and entry_index is not None and entry_decision_date is not None:
        gross_open = trade_gross
        trades.append({
            "entry_decision_date": entry_decision_date,
            "entry_execution_date": rows[entry_index]["date"],
            "exit_decision_date": None,
            "exit_execution_date": None,
            "status": "open_at_data_end_no_executable_exit",
            "gross_pnl": round(gross_open, 8),
            "transaction_costs": entry_cost,
            "net_pnl": round(gross_open - entry_cost, 8),
        })

    if cash_in_lqdt:
        margin = (contracts[near]["initial_margin"] + contracts[far]["initial_margin"]) * quantity
        values, shortfalls = lqdt_overlay_values(
            initial_capital,
            [row["cumulative_gross_pnl"] - row["cumulative_transaction_costs"] for row in daily],
            [margin if row["position"] else 0.0 for row in daily],
            [benchmark_mark(row["date"])["price"] for row in daily],
        )
        for row, value in zip(daily, values):
            row["strategy_value_proxy"] = round(value, 8)
            row["X"] = round(value - row["benchmark_value"], 8)
    else:
        shortfalls = 0

    peak = 0.0
    for row in daily:
        peak = max(peak, row["X"])
        row["H"] = round(peak, 8)
        row["D"] = round(peak - row["X"], 8)
    closed = [trade for trade in trades if trade["status"] == "closed_proxy_round_trip"]
    assignment_event = {
        "event_type": "perpetual_to_quarterly_execution_window",
        "far_contract": far,
        "far_expiry": far_expiry,
        "window_length_trading_days": assignment_window_trading_days,
        "window_start_date": assignment_window_start.isoformat() if assignment_window_start else None,
        "status": assignment_window_status,
        "policy": "exit_before_perpetual_execution_window",
        "payment_rate": 0.03,
        "payment_modelled": False,
        "payment_note": "No 3% execution payment is added unless the window is observable in the input.",
        "source": "MOEX notice n89495",
    }
    return {
        "schema_version": STRATEGY_SCHEMA,
        "release_state": "exploratory_inconclusive",
        "study_role": "frozen_funding_filtered_basis_strategy_proxy_backtest",
        "near": near,
        "far": far,
        "direction": "short_near_long_far",
        "rule_id": "H2-S1" if strategy_variant == "basis_median" else "H3-F1",
        "hypothesis": "funding_filtered_basis_reversion" if strategy_variant == "basis_median" else "funding_only_carry",
        "parameters": {
            "warmup_observations": warmup_observations,
            "trailing_funding_condition": f"mean_previous_{warmup_observations}_daily_funding_received > 0",
            "entry_condition": (
                f"current_basis <= median_previous_{warmup_observations}_basis"
                if strategy_variant == "basis_median"
                else f"mean_previous_{warmup_observations}_daily_funding_received > 0"
            ),
            "exit_conditions": [
                *([f"current_basis >= median_previous_{warmup_observations}_basis"] if strategy_variant == "basis_median" else []),
                "mean_previous_20_daily_funding_received <= 0",
                "date >= far_expiry - 5 calendar days",
            ],
            "decision_delay_observations": 1,
            "execution_delay_rows": execution_delay_rows,
            "accounting": "free_cash_in_lqdt_margin_cash_idle" if cash_in_lqdt else "idle_cash_no_yield",
            "quantity_per_leg": quantity,
            "expiry": far_expiry,
            "expiry_buffer_days": expiry_buffer_days,
            "assignment_window_trading_days": assignment_window_trading_days,
            "assignment_policy": "exit_before_perpetual_execution_window",
            "assignment_window_start_date": assignment_window_start.isoformat() if assignment_window_start else None,
            "assignment_window_status": assignment_window_status,
            "assignment_event": assignment_event,
        },
        "cost_scenario": cost_profile,
        "account_risk": account_risk,
        "summary": {
            "first_date": daily[0]["date"],
            "last_date": daily[-1]["date"],
            "row_count": len(daily),
            "signal_count": len(signals),
            "closed_trade_count": len(closed),
            "open_trade_count": sum(trade["status"] == "open_at_data_end_no_executable_exit" for trade in trades),
            "cash_shortfall_rows": shortfalls,
            "final_strategy_value_proxy": daily[-1]["strategy_value_proxy"],
            "final_benchmark_value": daily[-1]["benchmark_value"],
            "final_X": daily[-1]["X"],
            "final_H": daily[-1]["H"],
            "final_D": daily[-1]["D"],
            "gross_pnl": round(cumulative_gross, 8),
            "transaction_costs": round(cumulative_cost, 8),
            "historical_executable_fills": False,
            "lqdt_benchmark_observed_or_proxy": sorted({row["benchmark_source_kind"] for row in daily}),
            "perpetual_assignment_event_observed": any(signal["reason"] == "perpetual_execution_window" for signal in signals),
            "account_risk_gate_current_snapshot": account_risk["summary"]["current_snapshot_passed"],
            "account_risk_gate_all_working_scenarios": account_risk["summary"]["all_scenarios_passed"],
        },
        "trades": trades,
        "signals": signals,
        "daily": daily,
        "provenance": {
            "carry_decomposition_schema": CARRY_SCHEMA,
            "benchmark_sha256": benchmark_hash,
            "benchmark_path": str(benchmark_path),
            "contracts_sha256": contracts_hash,
            "contracts_path": str(contracts_path),
            "historical_specification_status": "current_snapshot_fallback_assumed",
            "execution_evidence_status": "historical_bid_ask_unavailable",
        },
    }


def write_carry_strategy(result: dict[str, Any], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
