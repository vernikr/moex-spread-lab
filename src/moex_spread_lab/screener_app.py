"""Local-only research screener over the existing analytical engines."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .account_risk import assess_vtb_pair
from .analytics import run_mean_reversion_baseline
from .carry import build_carry_strategy, build_carry_study, fetch_history
from .costs import tariff_profile
from .screener import SCREEN_PAIRS, _normalized_series
from .w0 import W0Error

EXTRA_PAIRS = (
    {
        "pair_id": "SLVRUBF/S2Z6",
        "near": "SLVRUBF",
        "far": "S2Z6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "EURRUBF/EuZ6",
        "near": "EURRUBF",
        "far": "EuZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "BTCUSDF/BTZ6",
        "near": "BTCUSDF",
        "far": "BTZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    {
        "pair_id": "GLZ6/GLH7",
        "near": "GLZ6",
        "far": "GLH7",
        "family": "dated_calendar_same_asset",
    },
    {
        "pair_id": "CRZ6/CRH7",
        "near": "CRZ6",
        "far": "CRH7",
        "family": "dated_calendar_same_asset",
    },
)
PAIRS = (
    {
        "pair_id": "CNYRUBF/CRZ6",
        "near": "CNYRUBF",
        "far": "CRZ6",
        "family": "perpetual_quarterly_same_asset",
    },
    *SCREEN_PAIRS,
    *EXTRA_PAIRS,
)
STRATEGIES = {
    "mean_reversion": {
        "label": "Spread mean reversion",
        "parameters": {"lookback": 20, "entry_z": 2.0, "exit_z": 0.5},
    },
    "funding_only": {
        "label": "Perpetual funding carry",
        "parameters": {"lookback": 20},
    },
}
MIN_DAILY_VOLUME = 100
MAX_QUANTITY = 50
INITIAL_CAPITAL = 30_000.0
HISTORY_WINDOWS = {
    "CNYRUBF/CRZ6": ("2025-06-17", "2026-10-27"),
    **{
        pair["pair_id"]: ("2026-03-23", "2026-10-27")
        for pair in (*SCREEN_PAIRS, *EXTRA_PAIRS)
    },
}


class ScreenerError(W0Error):
    """Raised for unavailable data or invalid screener selections."""


@dataclass(frozen=True)
class ScreenerInputs:
    root: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", self.root.resolve())

    @property
    def benchmark(self) -> Path:
        return self.root / "data/benchmark/lqdt-lineage.json"

    @property
    def contracts(self) -> Path:
        return self.root / "data/w1/current-intake.json"

    @property
    def history(self) -> Path:
        return self.root / "data/analytics/screener-runs.jsonl"

    @property
    def screen_history(self) -> Path:
        return self.root / "data/analytics/screener-screens.jsonl"

    @property
    def search_history(self) -> Path:
        return self.root / "data/analytics/screener-searches.jsonl"

    def candles(self, pair_id: str, secid: str) -> Path:
        primary = self.root / "data/w1/candles-screen" / f"{secid}_1d.json"
        original = self.root / "data/w1/candles" / f"{secid}_1d.json"
        if pair_id == "CNYRUBF/CRZ6" or not primary.is_file():
            return original
        return primary

    def history_cache(self, pair_id: str) -> Path:
        folder = (
            "carry-history-cache"
            if pair_id == "CNYRUBF/CRZ6"
            else "carry-screen-history-cache"
        )
        return self.root / "data" / folder


def _pair(pair_id: Any) -> dict[str, str]:
    for pair in PAIRS:
        if pair["pair_id"] == pair_id:
            return pair
    raise ScreenerError("pair is not in the admitted research registry")


def _offline_only(*_args: Any, **_kwargs: Any) -> Any:
    raise OSError(
        "local screener does not download data; run the documented intake first"
    )


def _leg_volumes(inputs: ScreenerInputs, pair: dict[str, str]) -> dict[str, int] | None:
    """Smallest daily volume of each leg over the days both legs traded; None if candles are missing."""
    try:
        legs = {
            secid: {
                row["begin"][:10]: row["volume"]
                for row in json.loads(
                    inputs.candles(pair["pair_id"], secid).read_text(encoding="utf-8")
                )["rows"]
            }
            for secid in (pair["near"], pair["far"])
        }
    except (OSError, ValueError, KeyError, TypeError):
        return None
    days = set(legs[pair["near"]]) & set(legs[pair["far"]])
    return {secid: min(volumes[day] for day in days) for secid, volumes in legs.items()} if days else None


def catalog(inputs: ScreenerInputs) -> dict[str, Any]:
    try:
        instruments = {
            row["secid"]: row
            for row in json.loads(inputs.contracts.read_text(encoding="utf-8"))[
                "instruments"
            ]
            if isinstance(row, dict) and row.get("secid")
        }
    except (OSError, ValueError, KeyError, TypeError):
        instruments = {}
    pairs = []
    for pair in PAIRS:
        pair_id = pair["pair_id"]
        near, far = pair["near"], pair["far"]
        price_files = (
            inputs.candles(pair_id, near),
            inputs.candles(pair_id, far),
            inputs.benchmark,
            inputs.contracts,
        )
        start, end = HISTORY_WINDOWS[pair_id]
        funding_files = (
            inputs.history_cache(pair_id) / f"{near}-{start}-{end}-0.json",
            inputs.history_cache(pair_id) / f"{far}-{start}-{end}-0.json",
            inputs.benchmark,
            inputs.contracts,
        )
        volumes = _leg_volumes(inputs, pair)
        tradable = volumes is not None and min(volumes.values()) >= MIN_DAILY_VOLUME
        available = {
            "mean_reversion": tradable and all(path.is_file() for path in price_files),
            "funding_only": tradable
            and pair["family"] == "perpetual_quarterly_same_asset"
            and all(path.is_file() for path in funding_files),
        }
        near_terms, far_terms = instruments.get(near, {}), instruments.get(far, {})
        margins = (near_terms.get("initial_margin"), far_terms.get("initial_margin"))
        margin_pct = (
            round(sum(float(value) for value in margins) / 30_000 * 100, 2)
            if all(isinstance(value, (int, float)) for value in margins)
            else None
        )
        pairs.append(
            {
                "id": pair_id,
                "family": pair["family"],
                "expiry": far_terms.get("expiry"),
                "current_margin_pct": margin_pct,
                "min_leg_daily_volume": min(volumes.values()) if volumes else None,
                "strategies": available,
                "status": "excluded_thin_leg"
                if volumes is not None and not tradable
                else "local_inputs_available"
                if any(available.values())
                else "intake_required",
            }
        )
    return {
        "pairs": pairs,
        "strategies": STRATEGIES,
        "execution_defaults": {"slippage_ticks": 1.0, "quantity": 1},
        "min_daily_volume": MIN_DAILY_VOLUME,
        "price_warning": "Historical candle/settlement values are analytical price proxies, not fills.",
    }


def _parameters(selection: dict[str, Any], strategy: str) -> dict[str, float | int]:
    defaults = STRATEGIES[strategy]["parameters"]
    provided = selection.get("parameters", {})
    if not isinstance(provided, dict) or set(provided) - set(defaults):
        raise ScreenerError("unknown strategy parameter")
    values = {**defaults, **provided}
    lookback = values["lookback"]
    if (
        isinstance(lookback, bool)
        or not isinstance(lookback, int)
        or not 5 <= lookback <= 80
    ):
        raise ScreenerError("lookback must be an integer between 5 and 80 observations")
    if strategy == "mean_reversion":
        for field in ("entry_z", "exit_z"):
            value = values[field]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ScreenerError(f"{field} must be a finite number")
        if (
            not 0.5 <= values["entry_z"] <= 5
            or not 0 <= values["exit_z"] < values["entry_z"]
        ):
            raise ScreenerError(
                "entry must be 0.5–5 and exit must be non-negative and below entry"
            )
    return values


def execution_settings(selection: dict[str, Any]) -> dict[str, float | int]:
    slippage, quantity = selection.get("slippage_ticks", 1.0), selection.get("quantity", 1)
    if isinstance(slippage, bool) or not isinstance(slippage, (int, float)) or not 0 <= slippage <= 10:
        raise ScreenerError("slippage must be 0–10 ticks per leg and side")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= MAX_QUANTITY:
        raise ScreenerError(f"contracts per leg must be an integer between 1 and {MAX_QUANTITY}")
    return {"slippage_ticks": float(slippage), "quantity": quantity}


def _history_source(inputs: ScreenerInputs, pair_id: str, secid: str) -> dict[str, Any]:
    start, end = HISTORY_WINDOWS[pair_id]
    return fetch_history(
        secid,
        start,
        end,
        cache_dir=inputs.history_cache(pair_id),
        delay_seconds=0,
        opener=_offline_only,
    )


def build_result(inputs: ScreenerInputs, selection: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(selection, dict):
        raise ScreenerError("selection must be an object")
    pair = _pair(selection.get("pair"))
    pair_id, near, far = pair["pair_id"], pair["near"], pair["far"]
    strategy = selection.get("strategy")
    if not isinstance(strategy, str) or strategy not in STRATEGIES:
        raise ScreenerError("unknown strategy")
    execution = execution_settings(selection)
    params = _parameters(selection, strategy)
    availability = next(
        item["strategies"] for item in catalog(inputs)["pairs"] if item["id"] == pair_id
    )
    if not availability[strategy]:
        raise ScreenerError("local inputs are missing for this pair and strategy")

    if strategy == "mean_reversion":
        series = _normalized_series(
            inputs.candles(pair_id, near),
            inputs.candles(pair_id, far),
            inputs.benchmark,
            inputs.contracts,
            near=near,
            far=far,
        )
        risk = assess_vtb_pair(inputs.contracts, near, far, quantity=execution["quantity"])
        if not risk["summary"]["all_scenarios_passed"]:
            raise ScreenerError("pair fails the working capital stress screen")
        report = run_mean_reversion_baseline(
            series,
            lookback=params["lookback"],
            entry_z=params["entry_z"],
            exit_z=params["exit_z"],
            quantity=execution["quantity"],
            analysis_role="diagnostic_only",
            cost_profile=tariff_profile(execution["slippage_ticks"]),
            fill="open",
            cash_in_lqdt=True,
        )
        daily = [
            {
                "date": row["date"],
                "V": row["strategy_value"],
                "B": row["benchmark_value"],
                "X": row["X"],
                "D": row["D"],
            }
            for row in report["daily"]
        ]
        summary = report["summary"]
        trades = summary["trade_count"]
        open_positions = 0
        costs = summary["modeled_costs"]
        provenance = report["input_provenance"]
        price_role = "ISS daily candle open fill, close marks"
        notes = [
            "Price-only control: perpetual funding is not included; not a complete carry result.",
            "Signal at a daily close, fill at the next daily open (minutes-to-hours reaction proxy).",
            "An open position at the sample end is marked as a forced proxy exit, not an executable trade.",
        ]
    else:
        near_source = _history_source(inputs, pair_id, near)
        far_source = _history_source(inputs, pair_id, far)
        study = build_carry_study(
            near_source,
            far_source,
            inputs.contracts,
            near=near,
            far=far,
            funding_regimes=None if pair_id == "CNYRUBF/CRZ6" else (),
        )
        report = build_carry_strategy(
            study,
            inputs.benchmark,
            inputs.contracts,
            transaction_cost_scenario="tariff",
            warmup_observations=params["lookback"],
            strategy_variant="funding_only",
            quantity=execution["quantity"],
            execution_delay_rows=0,
            cash_in_lqdt=True,
            slippage_ticks=execution["slippage_ticks"],
        )
        risk = assess_vtb_pair(inputs.contracts, near, far, quantity=execution["quantity"])
        if not risk["summary"]["all_scenarios_passed"]:
            raise ScreenerError("pair fails the working capital stress screen")
        daily = [
            {
                "date": row["date"],
                "V": row["strategy_value_proxy"],
                "B": row["benchmark_value"],
                "X": row["X"],
                "D": row["D"],
            }
            for row in report["daily"]
        ]
        summary = report["summary"]
        trades = summary["closed_trade_count"]
        open_positions = summary["open_trade_count"]
        costs = summary["transaction_costs"]
        provenance = {
            "near_history_hashes": near_source["source"]["raw_response_sha256"],
            "far_history_hashes": far_source["source"]["raw_response_sha256"],
            "benchmark_sha256": hashlib.sha256(
                inputs.benchmark.read_bytes()
            ).hexdigest(),
            "contracts_sha256": hashlib.sha256(
                inputs.contracts.read_bytes()
            ).hexdigest(),
        }
        price_role = "ISS EOD settlement proxy"
        notes = [
            "Funding signal uses funding through the previous clearing; fill proxy is the same-day settlement; a same-day exit forgoes that day's funding.",
            *(
                ["Non-CNY funding-rule caps are not reconstructed."]
                if pair_id != "CNYRUBF/CRZ6"
                else []
            ),
        ]
    first, last = daily[0], daily[-1]
    initial = INITIAL_CAPITAL
    result = {
        "selection": {
            "pair": pair_id,
            "strategy": strategy,
            "parameters": params,
            "execution": execution,
        },
        "status": "exploratory_proxy_not_live",
        "price_role": price_role,
        "period": {
            "from": first["date"],
            "to": last["date"],
            "observations": len(daily),
        },
        "metrics": {
            "strategy_return_pct": round((last["V"] / initial - 1) * 100, 3),
            "lqdt_return_pct": round((last["B"] / initial - 1) * 100, 3),
            "excess_rub": last["X"],
            "excess_pct_points": round(last["X"] / initial * 100, 3),
            "max_excess_drawdown_rub": round(max(row["D"] for row in daily), 2),
            "worst_lqdt_shortfall_rub": round(min(row["X"] for row in daily), 2),
            "closed_trades": trades,
            "open_positions": open_positions,
            "modeled_costs_rub": costs,
            "current_pair_margin_rub": risk["pair"]["pair_margin_no_offset_rub"],
            "stress_remaining_capital_rub": risk["summary"][
                "most_severe_remaining_capital_rub"
            ],
        },
        "daily": daily,
        "notes": [
            "Matched LQDT price path is not an account-statement reconciliation.",
            "Current contract/margin snapshots are fallback assumptions, not dated broker terms.",
            "Costs: published MOEX taker tariff, zero VTB broker fee to 2026-12-31, slippage as a parameter.",
            "Free cash is held in LQDT, margin cash earns nothing (A-11); the benchmark holds all capital in LQDT.",
            "Capital screening uses fixed working buffers and current margin snapshots, not future-known losses.",
            *notes,
        ],
        "provenance": provenance,
    }
    identity = {
        "selection": result["selection"],
        "daily": daily,
        "source_hashes": {
            key: value
            for key, value in provenance.items()
            if "sha256" in key or "hashes" in key
        },
    }
    canonical = json.dumps(
        identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    result["run_id"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    return result


class ScreenerStore:
    def __init__(self, inputs: ScreenerInputs):
        self.inputs = inputs
        self.lock = threading.Lock()
        self._calendar: Any = None

    @property
    def calendar(self) -> Any:
        if self._calendar is None:
            from .calendar_spreads import CalendarData

            self._calendar = CalendarData(self.inputs.root)
        return self._calendar

    @staticmethod
    def _recent(path: Path) -> list[dict[str, Any]]:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines[-20:] if line]

    def list_runs(self) -> list[dict[str, Any]]:
        return self._recent(self.inputs.history)

    def list_searches(self) -> list[dict[str, Any]]:
        return self._recent(self.inputs.search_history)

    def _save(self, path: Path, result: dict[str, Any]) -> None:
        with self.lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if any(run["run_id"] == result["run_id"] for run in self._recent(path)):
                return
            record = {**result, "recorded_at": datetime.now(UTC).isoformat()}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
                )

    def save(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.history, result)

    def save_screen(self, result: dict[str, Any]) -> None:
        compact = {
            key: result[key] for key in ("run_id", "schema_version", "selection")
        }
        compact["pairs"] = [
            {key: row[key] for key in ("id", "status", "metrics") if key in row}
            for row in result["pairs"]
        ]
        self._save(self.inputs.screen_history, compact)

    def save_search(self, result: dict[str, Any]) -> None:
        self._save(self.inputs.search_history, result)


def make_handler(store: ScreenerStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, value: Any) -> None:
            self._send(
                status,
                json.dumps(value, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def _local_request(self) -> bool:
            host = self.headers.get("Host", "")
            port = self.server.server_port
            return host in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def do_GET(self) -> None:
            if not self._local_request():
                self._json(403, {"error": "local host only"})
            elif self.path == "/":
                self._send(
                    200,
                    Path(__file__).with_name("screener_ui.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif self.path == "/proof":
                self._send(
                    200,
                    Path(__file__).with_name("proof_ui.html").read_bytes(),
                    "text/html; charset=utf-8",
                )
            elif self.path == "/api/proof/catalog":
                try:
                    self._json(200, store.calendar.catalog())
                except (W0Error, OSError) as exc:
                    self._json(400, {"error": str(exc)})
            elif self.path == "/api/catalog":
                self._json(200, catalog(store.inputs))
            elif self.path == "/api/runs":
                self._json(200, store.list_runs())
            elif self.path == "/api/searches":
                self._json(200, store.list_searches())
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._local_request() or self.headers.get("Origin") not in (
                None,
                f"http://127.0.0.1:{self.server.server_port}",
                f"http://localhost:{self.server.server_port}",
            ):
                self._json(403, {"error": "local origin only"})
                return
            if self.path not in {"/api/run", "/api/screen", "/api/optimize", "/api/proof/run"}:
                self._json(404, {"error": "not found"})
                return
            if (
                self.headers.get("Content-Type", "").split(";", 1)[0]
                != "application/json"
            ):
                self._json(415, {"error": "JSON body required"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 8192:
                    raise ScreenerError("request size must be 1–8192 bytes")
                selection = json.loads(self.rfile.read(size))
                if self.path == "/api/proof/run":
                    if not isinstance(selection, dict):
                        raise ScreenerError("selection must be an object")
                    result = store.calendar.proof(selection)
                elif self.path == "/api/run":
                    result = build_result(store.inputs, selection)
                    store.save(result)
                else:
                    from .screener_search import optimize, screen_table

                    if self.path == "/api/screen":
                        result = screen_table(store.inputs, selection)
                        store.save_screen(result)
                    else:
                        result = optimize(store.inputs, selection)
                        store.save_search(result)
            except (ValueError, json.JSONDecodeError, W0Error, OSError) as exc:
                self._json(400, {"error": str(exc)})
                return
            self._json(200, result)

        def log_message(self, _format: str, *_args: Any) -> None:
            pass

    return Handler


def serve(inputs: ScreenerInputs, port: int = 8765) -> None:
    if not 0 <= port <= 65535:
        raise ScreenerError("port must be 0–65535")
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), make_handler(ScreenerStore(inputs))
    )
    print(f"Local research screener: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
