"""Request mapping and common admission helpers for cohort preparation."""

from __future__ import annotations

from moex_spread_lab.core.contracts import InstrumentContext
from moex_spread_lab.core.universe import CALENDAR_UNIVERSE
from moex_spread_lab.core.w0 import W0Error

from .axis import Axis
from .request_validation import _identity, execution_axes
from .strategy_catalog import get_strategy_axes, resolve_strategy_registry

MAX_TARGETS = 1024


def _translate(params, strategy):
    result = dict(params)
    if strategy == "mean_reversion" and "window" in result:
        result["lookback"] = result.pop("window")
    if strategy == "zscore_mean_reversion" and "lookback" in result:
        result["window"] = result.pop("lookback")
    return result


def _native_request(active, target, *, default_price_fill="reference_same_bar", registry=None):
    from moex_spread_lab.strategies.calendar.calendar_spreads import ASSETS

    registry = resolve_strategy_registry(registry)
    strategy_axes = get_strategy_axes(registry)
    strategy = active.get("strategy", "zscore_mean_reversion")
    if strategy not in strategy_axes:
        raise W0Error("Неизвестная стратегия подбора")
    plugin = registry.get(strategy)
    adapters = [
        p
        for p in registry
        if p.strategy_id in strategy_axes
        and p.family_id == plugin.family_id
        and target["universe"] in p.supported_universes
    ]
    if not adapters:
        raise W0Error("Выбранная стратегия неприменима к этому типу пары.")
    strategy = adapters[0].strategy_id
    if strategy == "convergence_to_ruonia" and not ASSETS.get(target["asset"], {}).get(
        "convergence"
    ):
        raise W0Error("Для этого актива не предусмотрена стратегия RUONIA.")
    fill = active.get("fill", default_price_fill)
    return {
        **target,
        "strategy": strategy,
        **({"fill": fill} if strategy != "funding_only" else {}),
        **{k: active[k] for k in ("sizing", *execution_axes(active)) if k in active},
        "parameters": _translate(active.get("parameters", {}), strategy),
    }


def _targets(request):
    raw = request.get("targets")
    if "targets" not in request or raw == []:
        return [_identity(request)]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TARGETS:
        raise W0Error(f"Выберите от 1 до {MAX_TARGETS} окон.")
    unique = {}
    for item in raw:
        if not isinstance(item, dict) or item.keys() - {"universe", "asset", "pair"}:
            raise W0Error("Ожидается список идентификаторов окон.")
        t = _identity(item)
        key = t["universe"], t.get("asset", ""), t["pair"]
        if key in unique:
            raise W0Error("Окно указано дважды.")
        unique[key] = t
    return [unique[k] for k in sorted(unique)]


def _active_seed(request, active, *, registry=None):
    from moex_spread_lab.strategies.calendar.calendar_spreads import ASSETS

    context = InstrumentContext(
        universe=request.get("universe", CALENDAR_UNIVERSE),
        pair=request.get("pair", ""),
        asset=request.get("asset"),
        monthly=bool(ASSETS.get(request.get("asset"), {}).get("monthly")),
    )
    plugin = resolve_strategy_registry(registry).get(active)
    defaults = {
        spec.name: spec.default for spec in plugin.parameter_schema(context) if spec.tunable
    }
    return {
        **defaults,
        **request.get("parameters", {}),
        **(
            {
                "quantity": request.get("quantity", 1),
                "slippage_ticks": request.get("slippage_ticks", 1.0),
            }
            if execution_axes(request)
            else {}
        ),
    }


def _intersect_axes(axes, strategy_axes, active):
    for name, other in _translate(strategy_axes, active).items():
        a = axes[name]
        axes[name] = Axis(max(a.low, other.low), min(a.high, other.high), a.step)


def _filter_training(members, axes, rejected):
    # Every window must fit the intersection, not merely its individual domain.
    minimum = max((a.low + 2 for k, a in axes.items() if k in ("window", "lookback")), default=2)
    compatible = []
    for target, case in members:
        if len(case.split.training) >= minimum:
            compatible.append((target, case))
        else:
            rejected.append(
                {
                    "target": target,
                    "reason": f"Для общего окна сигнала нужно не менее {minimum} наблюдений на обучении.",
                }
            )
    return compatible


def _confirmation(settings, rejected, members):
    return {
        "status": "selection_requires_confirmation",
        **settings.metadata(),
        "excluded": rejected,
        "remaining_count": len(members),
        "trial_count": 0,
        "selected_parameters": None,
    }
