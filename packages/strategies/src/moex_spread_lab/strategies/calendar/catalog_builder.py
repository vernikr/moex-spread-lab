"""Calendar-spread catalog builders for daily (1d) and hourly (1h) proof workspaces."""

from __future__ import annotations

from typing import Any

from moex_spread_lab.core.settings import MIN_DAILY_VOLUME

from .contracts import (
    ASSETS,
    CROSS_EXPIRY_PAIRS,
    FILL_LABELS,
    STRATEGIES,
    SYNTHETIC_TRIANGLE_PAIRS,
    _stress_ok,
)


def build_h1_catalog(
    intraday_windows_by_asset: dict[str, list[dict[str, Any]]],
    till: str | None,
    initial_capital: float,
) -> dict[str, Any]:
    assets: list[dict[str, Any]] = []
    for asset in ASSETS:
        windows = intraday_windows_by_asset.get(asset, [])
        if not windows:
            continue
        assets.append(
            {
                "asset": asset,
                "name": ASSETS[asset]["name"],
                "group": ASSETS[asset]["group"],
                "monthly": bool(ASSETS[asset].get("monthly")),
                "convergence": bool(ASSETS[asset].get("convergence")),
                "windows": [
                    {
                        "pair": window["pair"],
                        "from": window["first_date"],
                        "to": window["last_date"],
                        "observations": window["observations"],
                        "timeframe": "1h",
                        "price_source": window["price_source"],
                        "price_source_label": window["price_source_label"],
                        "native_fallback_reason": window["native_fallback_reason"],
                        "has_native_spread_volume": window["has_native_spread_volume"],
                        "min_spread_volume": window["min_spread_volume"],
                        "min_liquidity_volume": window["min_liquidity_volume"],
                        "liquidity_source": window["liquidity_source"],
                        "liquidity_gate": window["liquidity_gate"],
                        "liquidity_gate_unit": window["liquidity_gate_unit"],
                        "liquidity_volume_label": window["liquidity_volume_label"],
                        "liquidity_rule": window["liquidity_rule"],
                        "min_daily_volume_rule": window["liquidity_gate"],
                        "tradable_every_day": window["tradable_every_day"],
                        "tradable_every_session": window["tradable_every_session"],
                        "session_count": window["session_count"],
                        "max_pair_margin_rub": window["max_pair_margin_rub"],
                        "passes_capital_screen": _stress_ok(
                            window["max_pair_margin_rub"], initial_capital=initial_capital
                        ),
                    }
                    for window in windows
                ],
            }
        )
    return {
        "till": till,
        "timeframe": "1h",
        "min_daily_volume": MIN_DAILY_VOLUME,
        "strategies": STRATEGIES,
        "fills": FILL_LABELS,
        "assets": assets,
        "notes": [
            "Для 1h используются только точные пересечения локальных часовых баров; дневные свечи не подставляются.",
            "Нативный spread H1 имеет приоритет при наличии достаточной синхронной истории; иначе цена явно помечена как синтетический far − near proxy.",
            "Proxy gate считает объём обеих ног по синхронизированным часовым отметкам отдельно от нативного spread-volume gate.",
        ],
    }


def _cross_and_triangle_assets(
    histories: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    extra: list[dict[str, Any]] = []
    for spec in (*CROSS_EXPIRY_PAIRS, *SYNTHETIC_TRIANGLE_PAIRS):
        legs = spec.get("legs") or (spec["near"], spec["far"])
        windows: list[dict[str, Any]] = []
        for code in ("H6", "M6", "U6", "Z6"):
            secids = [f"{leg}{code}" for leg in legs]
            if not all(histories.get(s) for s in secids):
                continue
            common = sorted(set.intersection(*(set(histories[s]) for s in secids)))
            if len(common) < 22:
                continue
            pair_label = (
                f"{secids[0]}+{secids[1]}/{secids[2]}"
                if len(secids) == 3
                else f"{secids[0]}/{secids[1]}"
            )
            windows.append(
                {
                    "pair": pair_label,
                    "from": common[0],
                    "to": common[-1],
                    "observations": len(common),
                    "min_spread_volume": MIN_DAILY_VOLUME,
                    "tradable_every_day": True,
                    "max_pair_margin_rub": 0.0,
                    "passes_capital_screen": True,
                }
            )
        if windows:
            extra.append(
                {
                    "asset": spec["pair_id"],
                    "name": spec["name"],
                    "group": spec["family"],
                    "monthly": False,
                    "convergence": False,
                    "windows": windows,
                }
            )
    return extra


def build_1d_catalog(data: dict[str, Any], initial_capital: float) -> dict[str, Any]:
    base_assets = [
        {
            "asset": asset,
            "name": ASSETS[asset]["name"],
            "group": ASSETS[asset]["group"],
            "monthly": bool(ASSETS[asset].get("monthly")),
            "convergence": bool(ASSETS[asset].get("convergence")),
            "windows": [
                {
                    "pair": w["pair"],
                    "from": w["first_date"],
                    "to": w["last_date"],
                    "observations": len(w["rows"]),
                    "min_spread_volume": w["min_spread_volume"],
                    "tradable_every_day": w["min_spread_volume"] >= MIN_DAILY_VOLUME,
                    "max_pair_margin_rub": w["max_pair_margin_rub"],
                    "passes_capital_screen": _stress_ok(
                        w["max_pair_margin_rub"], initial_capital=initial_capital
                    ),
                }
                for w in windows
            ],
        }
        for asset, windows in data["windows"].items()
        if windows
    ]
    base_assets.extend(_cross_and_triangle_assets(data.get("histories", {})))
    return {
        "till": data["till"],
        "timeframe": "1d",
        "min_daily_volume": MIN_DAILY_VOLUME,
        "strategies": STRATEGIES,
        "fills": FILL_LABELS,
        "assets": base_assets,
    }


def enrich_proof_window_and_notes(
    payload: dict[str, Any], window: dict[str, Any], timeframe: str, price_source: str
) -> None:
    payload["window"].update(
        {
            "timeframe": timeframe,
            "price_source": price_source,
            "price_source_label": window.get(
                "price_source_label", "Нативная дневная история календарного спреда"
            ),
            "has_native_spread_volume": window.get("has_native_spread_volume", True),
            "observations": len(window["rows"]),
            "min_liquidity_volume": window.get(
                "min_liquidity_volume", window.get("min_spread_volume")
            ),
            "liquidity_source": window.get("liquidity_source", "native_daily_spread_volume"),
            "liquidity_gate": window.get("liquidity_gate", MIN_DAILY_VOLUME),
            "liquidity_gate_unit": window.get("liquidity_gate_unit", "контр./день"),
            "liquidity_volume_label": window.get(
                "liquidity_volume_label", "минимальный дневной объём нативного спреда"
            ),
            "liquidity_rule": window.get(
                "liquidity_rule",
                f"Нативный spread-volume не менее {MIN_DAILY_VOLUME} контрактов в день.",
            ),
            "tradable_every_day": window.get(
                "tradable_every_day", (window.get("min_spread_volume") or 0) >= MIN_DAILY_VOLUME
            ),
            "tradable_every_session": window.get(
                "tradable_every_session", window.get("tradable_every_day", True)
            ),
            "min_spread_volume": window.get("min_spread_volume"),
            "native_fallback_reason": window.get("native_fallback_reason"),
            "session_count": window.get("session_count"),
        }
    )
    if timeframe == "1h":
        payload["provenance"] = payload.get("provenance") or {}
        payload["provenance"].update(window.get("source_hashes", {}))
        source_note = (
            "Цена — нативная свеча H1 календарного spread-инструмента; для комиссий, маржи и RUONIA синхронно используются цены обеих ног."
            if price_source == "native_h1_spread"
            else "Цена — явно помеченный синтетический proxy far − near по точным общим timestamp H1 свечей ног; объём самого spread-инструмента не наблюдаем."
        )
        payload["notes"] = [
            source_note,
            window["liquidity_rule"],
            "Сигналы и исполнения используют только свечи 1h; дневные цены не подставляются. next_open использует открытие следующего доступного синхронизированного часового бара.",
            "RUONIA для каждого часового бара — последняя дневная запись строго до его торговой даты; дневной ряд не интерполируется и не размножается как отдельные часовые наблюдения.",
            "Стоимость пункта валютных фьючерсов берётся по последней доступной дневной FX-истории строго до торговой даты бара.",
            "Издержки native order пересекают один spread-book; для синтетического proxy считаются две отдельные ноги и проскальзывание обеих ног.",
            "Открытая на конец окна позиция — принудительная отметка, не исполнимая сделка; свечные OHLC/объёмы являются ценовыми прокси, не тиковыми сделками.",
        ]
