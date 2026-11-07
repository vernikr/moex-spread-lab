"""Local-only research screener over the existing analytical engines."""

from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .account_risk import assess_vtb_pair
from .analytics import get_cost_scenario, run_mean_reversion_baseline
from .carry import build_carry_strategy, build_carry_study, fetch_history
from .screener import SCREEN_PAIRS, _basis_daily_pnl, _normalized_series
from .w0 import W0Error

PAIRS = ({"pair_id": "CNYRUBF/CRZ6", "near": "CNYRUBF", "far": "CRZ6"}, *SCREEN_PAIRS)
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
HISTORY_WINDOWS = {
    "CNYRUBF/CRZ6": ("2025-06-17", "2026-10-27"),
    **{pair["pair_id"]: ("2026-03-23", "2026-10-27") for pair in SCREEN_PAIRS},
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

    def candles(self, pair_id: str, secid: str) -> Path:
        folder = "candles" if pair_id == "CNYRUBF/CRZ6" else "candles-screen"
        return self.root / "data/w1" / folder / f"{secid}_1d.json"

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


def catalog(inputs: ScreenerInputs) -> dict[str, Any]:
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
        available = {
            "mean_reversion": all(path.is_file() for path in price_files),
            "funding_only": all(path.is_file() for path in funding_files),
        }
        pairs.append(
            {
                "id": pair_id,
                "strategies": available,
                "status": "local_inputs_available"
                if any(available.values())
                else "intake_required",
            }
        )
    return {
        "pairs": pairs,
        "strategies": STRATEGIES,
        "cost_scenarios": ("low", "base", "stress"),
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
    cost = selection.get("cost", "stress")
    if not isinstance(cost, str) or cost not in {"low", "base", "stress"}:
        raise ScreenerError("unknown illustrative cost scenario")
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
        risk = assess_vtb_pair(
            inputs.contracts, near, far, observed_daily_pnl=_basis_daily_pnl(series)
        )
        if not risk["summary"]["all_scenarios_passed"]:
            raise ScreenerError("pair fails the working capital stress screen")
        report = run_mean_reversion_baseline(
            series,
            lookback=params["lookback"],
            entry_z=params["entry_z"],
            exit_z=params["exit_z"],
            analysis_role="diagnostic_only",
            cost_profile=get_cost_scenario(cost),
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
        price_role = "ISS daily candle close proxy"
        notes = [
            "Price-only control: perpetual funding is not included; not a complete carry result.",
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
            transaction_cost_scenario=cost,
            warmup_observations=params["lookback"],
            strategy_variant="funding_only",
        )
        risk = report["account_risk"]
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
        notes = (
            ["Non-CNY funding-rule caps are not reconstructed."]
            if pair_id != "CNYRUBF/CRZ6"
            else []
        )
    first, last = daily[0], daily[-1]
    initial = 30_000.0
    result = {
        "selection": {
            "pair": pair_id,
            "strategy": strategy,
            "parameters": params,
            "cost": cost,
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
            "illustrative_costs_rub": costs,
            "current_pair_margin_rub": risk["pair"]["pair_margin_no_offset_rub"],
            "stress_remaining_capital_rub": risk["summary"][
                "most_severe_remaining_capital_rub"
            ],
        },
        "daily": daily,
        "notes": [
            "Matched LQDT price path is not an account-statement reconciliation.",
            "Current contract/margin snapshots are fallback assumptions, not dated broker terms.",
            "Costs are illustrative, not historical execution evidence.",
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

    def list_runs(self) -> list[dict[str, Any]]:
        try:
            lines = self.inputs.history.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        return [json.loads(line) for line in lines[-20:] if line]

    def save(self, result: dict[str, Any]) -> None:
        with self.lock:
            self.inputs.history.parent.mkdir(parents=True, exist_ok=True)
            if any(run["run_id"] == result["run_id"] for run in self.list_runs()):
                return
            with self.inputs.history.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n"
                )


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
            elif self.path == "/api/catalog":
                self._json(200, catalog(store.inputs))
            elif self.path == "/api/runs":
                self._json(200, store.list_runs())
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
            if self.path != "/api/run":
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
                result = build_result(store.inputs, selection)
                store.save(result)
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
