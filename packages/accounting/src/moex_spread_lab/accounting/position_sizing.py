"""Causal, maximum-affordable sizing of proof signal intents on one finite account.

The strategy supplies unit-sized intents, not an unconstrained second portfolio. Only
accepted entries create exposure, costs or funding. Their quantity is fixed until exit.
Legacy fixed-quantity analytical reports are deliberately outside this adapter.
"""

from __future__ import annotations

import math
from array import array
from copy import deepcopy

from moex_spread_lab.core.w0 import W0Error, _run_id
from .account_risk import DEFAULT_STRESS_VM_BUFFER, DEFAULT_UNCERTAINTY_BUFFER
from .costs import _cost_totals, side_cost, tariff_profile

AUTO_SIZING = "max_affordable"
SIZING_POLICY = "max-affordable.v1"


def automatic(request):
    return request.get("sizing") == AUTO_SIZING


def _positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise W0Error(f"Нет пригодного значения для расчёта размера: {name}.")
    return float(value)


def _money(value):
    return round(value, 2)


def maximum_quantity(budget, margin, unit_costs, *, stress, native_spread=False):
    """Largest integer paying margin, entry and an entry-priced exit-cost reserve.

    Never use the eventual exit price, full-window maximum margin or future P&L.
    Preserve each engine's stress convention: VM scales with quantity; uncertainty
    is per pair for native calendars and per account for the ordinary pair book.
    There is no former input-field ceiling of 50 contracts.
    """
    margin = _positive(margin, "ГО пары")
    if not math.isfinite(budget) or budget <= 0:
        return 0
    if any(not math.isfinite(v) or v < 0 for v in unit_costs.values()):
        raise W0Error("Неизвестные издержки не могут стать нулём при расчёте размера.")
    fixed_reserve = DEFAULT_UNCERTAINTY_BUFFER if stress and not native_spread else 0
    per_pair_reserve = (
        DEFAULT_STRESS_VM_BUFFER + (DEFAULT_UNCERTAINTY_BUFFER if native_spread else 0)
        if stress
        else 0
    )
    required_margin = (3 if stress else 1) * margin + per_pair_reserve

    def requirement(q):
        fees = sum(_money(v * q) for v in unit_costs.values())
        return required_margin * q + fixed_reserve + 2 * fees

    q = max(
        0,
        math.floor(
            (budget - fixed_reserve) / (required_margin + 2 * sum(unit_costs.values()))
        ),
    )
    # Component rounding can move the exact boundary by a kopeck.
    while q and requirement(q) > budget + 1e-9:
        q -= 1
    while requirement(q + 1) <= budget + 1e-9:
        q += 1
    return q


class _Terms:
    def __init__(self, proof, contracts, settings):
        self.native = proof.get("universe") == "calendar_native"
        self.native_order = self.native and proof.get("selection", {}).get("price_source") != "synthetic_leg_proxy"
        self.rows = proof["series"]
        self.open_fill = proof["selection"].get("fill") == "next_open"
        self.profile = tariff_profile(settings.slippage_ticks)
        self.contract = proof["contract"]
        if self.native:
            self.rate = _positive(self.contract.get("margin_rate"), "коэффициент ГО")
            self.tick_size = _positive(self.contract.get("tick_size"), "шаг цены")
            self.reference_point = _positive(
                self.contract.get("point_value_rub"), "стоимость пункта"
            )
            self.group = self.contract["tariff_group"]
        else:
            instrument = proof["instrument"]
            try:
                self.legs = [contracts[instrument[k]] for k in ("near", "far")]
            except KeyError as exc:
                raise W0Error("Нет параметров обеих ног для расчёта размера.") from exc
            self.margin = sum(
                _positive(c.get("initial_margin"), "ГО ноги") for c in self.legs
            )
            for c in self.legs:
                _positive(c.get("tick_size"), "шаг цены ноги")
                _positive(c.get("tick_value"), "стоимость шага ноги")

    def point(self, index):
        return (
            _positive(
                self.rows[index].get("point_value_rub"), "дневная стоимость пункта"
            )
            if self.native
            else 1.0
        )

    def fill_point(self, index, *, at_open):
        # Today's FX clearing/settlement is not known at the next-open proxy.
        if self.native and at_open:
            return self.point(index - 1) if index else self.reference_point
        return self.point(index)

    def at_fill(self, fill, index, *, at_open):
        near, far = (_positive(fill.get(k), k) for k in ("near_price", "far_price"))
        point = self.fill_point(index, at_open=at_open)
        if self.native:
            common = {
                "tick_size": self.tick_size,
                "tick_value": point * self.tick_size,
                "tariff_group": self.group,
            }
            legs = [
                ({**common, "secid": "near"}, near),
                ({**common, "secid": "far"}, far),
            ]
            margin = self.rate * (near + far) * point
        else:
            legs = list(zip(self.legs, (near, far)))
            margin = self.margin
        return margin, side_cost(self.profile, legs, 1, native_spread=self.native_order)

    def posted_margin(self, index, quantity):
        if self.native:
            row = self.rows[index]
            return self.rate * (row["near"] + row["far"]) * self.point(index) * quantity
        return self.margin * quantity


_STD_COST_KEYS = frozenset({"exchange_clearing", "broker", "slippage"})


def _size_proof_fast_c(
    fc,
    proof,
    terms,
    marks,
    settings,
    initial,
    strategy_name,
    _scan_cache=None,
    _scan_key=None,
    *,
    scalar_summary: tuple[str, str] | None = None,
):
    rows = proof["series"]
    n = len(rows)
    if n == 0:
        return None
    scan_cols = _scan_cache.get(_scan_key) if _scan_cache is not None else None
    if scan_cols is not None and len(scan_cols[0]) == n:
        dates, prices, spreads, points, posted_units, elig, entry_elig, exit_elig, date_to_idx = scan_cols
    else:
        dates = [r["date"] for r in rows]
        prices = array("d", (_positive(marks[d], "отметка LQDT") for d in dates))
        spreads = array("d", (float(r["spread"]) for r in rows))
        if terms.native:
            points = array("d", (terms.point(i) for i in range(n)))
            posted_units = array("d", (terms.posted_margin(i, 1) for i in range(n)))
        else:
            points = array("d", [1.0]) * n
            posted_units = array("d", [terms.margin]) * n
        elig_list = [1 if r.get("volume_eligible", True) else 0 for r in rows]
        elig = array("B", elig_list)
        entry_elig = array("B", (1 if r.get("entry_eligible", bool(e)) else 0 for r, e in zip(rows, elig_list)))
        exit_elig = array("B", (1 if r.get("exit_eligible", bool(e)) else 0 for r, e in zip(rows, elig_list)))
        date_to_idx = {d: i for i, d in enumerate(dates)}
        if _scan_cache is not None:
            _scan_cache[_scan_key] = (
                dates,
                prices,
                spreads,
                points,
                posted_units,
                elig,
                entry_elig,
                exit_elig,
                date_to_idx,
            )

    intents = {t["entry"]["date"]: t for t in proof["trades"]}
    intent_list = []
    trade_specs = []
    for entry_day, t in intents.items():
        entry_idx = date_to_idx.get(entry_day)
        if entry_idx is None:
            continue
        exit_day = t["exit"]["date"]
        exit_idx = date_to_idx.get(exit_day, -1)
        same_day = exit_idx == entry_idx
        open_at_end = bool(t.get("open_at_end"))
        entry_margin, entry_costs = terms.at_fill(
            t["entry"], entry_idx, at_open=terms.open_fill
        )
        if not set(entry_costs) <= _STD_COST_KEYS:
            return None
        _positive(entry_margin, "ГО пары")
        if any(not math.isfinite(v) or v < 0 for v in entry_costs.values()):
            raise W0Error(
                "Неизвестные издержки не могут стать нулём при расчёте размера."
            )
        if exit_idx >= 0:
            _, exit_costs = terms.at_fill(
                t["exit"],
                exit_idx,
                at_open=False if same_day else (terms.open_fill and not open_at_end),
            )
            if not set(exit_costs) <= _STD_COST_KEYS:
                return None
            exit_point = (
                terms.point(exit_idx)
                if same_day
                else terms.fill_point(
                    exit_idx, at_open=terms.open_fill and not open_at_end
                )
            )
        else:
            exit_costs = {}
            exit_point = 1.0
        orig_idx = len(intent_list)
        intent_list.append(t)
        trade_specs.append(
            (
                orig_idx,
                entry_idx,
                exit_idx,
                1 if t["side"] == "long_spread" else -1,
                open_at_end,
                float(t["entry"]["spread_fill"]),
                float(t["exit"]["spread_fill"]),
                float(entry_margin),
                float(entry_costs.get("exchange_clearing", 0.0)),
                float(entry_costs.get("broker", 0.0)),
                float(entry_costs.get("slippage", 0.0)),
                float(exit_costs.get("exchange_clearing", 0.0)),
                float(exit_costs.get("broker", 0.0)),
                float(exit_costs.get("slippage", 0.0)),
                float(exit_point),
            )
        )
    trade_specs.sort(key=lambda item: item[1])
    has_funding = any(
        r.get("funding_rub") is not None or r.get("funding_contracts", 0)
        for r in rows
    )

    use_scalar = False
    start_idx = 0
    end_idx = n - 1
    if scalar_summary is not None:
        s_day, e_day = scalar_summary
        s_i = date_to_idx.get(s_day)
        e_i = date_to_idx.get(e_day)
        if s_i is not None and e_i is not None and s_i <= e_i:
            use_scalar = True
            start_idx = s_i
            end_idx = e_i

    ledger_or_summary, acc_tuples = fc.size_proof_metrics(
        dates,
        prices,
        spreads,
        points,
        posted_units,
        elig,
        entry_elig,
        exit_elig,
        rows,
        trade_specs,
        float(initial),
        not settings.allow_failed_capital,
        terms.native_order,
        strategy_name == "funding_only",
        has_funding,
        use_scalar,
        start_idx,
        end_idx,
    )
    if ledger_or_summary == "overlap":
        raise W0Error(
            "Перекрывающиеся сигналы не поддерживаются одним счётом окна."
        )
    if use_scalar:
        p0, end_price, end_wealth, end_cum_net, any_window_shortfall = ledger_or_summary
        exposed = any(
            e_i <= end_idx and (x_i < 0 or x_i >= start_idx)
            for e_i, x_i in acc_tuples
        )
        proof["selection"]["sizing"] = AUTO_SIZING
        proof["initial_capital_rub"] = initial
        proof["sizing"] = {"initial_capital_rub": initial}
        proof["_scalar_train_summary"] = (
            initial,
            p0,
            end_price,
            end_wealth,
            end_cum_net,
            bool(any_window_shortfall),
            exposed,
        )
        return proof

    ledger = ledger_or_summary
    accepted = []
    for (
        orig_idx,
        q,
        margin_rub,
        gross_rub,
        ec0,
        ec1,
        ec2,
        xc0,
        xc1,
        xc2,
        has_xc,
    ) in acc_tuples:
        t = intent_list[orig_idx]
        t["number"] = len(accepted) + 1
        t["quantity"] = q
        t["margin_rub"] = margin_rub
        t["gross_rub"] = gross_rub
        t["entry"]["costs"] = {
            "exchange_clearing": ec0,
            "broker": ec1,
            "slippage": ec2,
        }
        if has_xc:
            t["exit"]["costs"] = {
                "exchange_clearing": xc0,
                "broker": xc1,
                "slippage": xc2,
            }
        else:
            t["exit"]["costs"] = {}
        t["costs_rub"] = _money(
            ec0 + ec1 + ec2 + (xc0 + xc1 + xc2 if has_xc else 0.0)
        )
        t["net_rub"] = _money(gross_rub - t["costs_rub"])
        t["lqdt_on_margin_rub"] = _money(
            margin_rub
            * (marks[t["exit"]["date"]] / marks[t["entry"]["date"]] - 1)
        )
        accepted.append(t)

    proof["selection"]["sizing"] = AUTO_SIZING
    proof["initial_capital_rub"] = initial
    proof["trades"] = accepted
    proof["ledger"] = ledger
    proof["sizing"] = {"initial_capital_rub": initial}
    return proof


def size_proof(
    unit,
    marks,
    settings,
    contracts,
    *,
    metrics_only=False,
    copy_unit=True,
    _scan_cache=None,
    _scan_key=None,
    scalar_summary: tuple[str, str] | None = None,
):
    """Replay independent signal intents; all account observations are prefix-causal.

    When `metrics_only` is True (training probes), the output keeps only the fields
    the objective reader needs: thin trades (entry/exit dates, margin, lqdt hurdle)
    and a thin ledger (date, cumulative_net_rub, account_value_rub, cash_shortfall).
    Presentation (series mutation, sizing/summary/notes/run_id) and the input copy
    are skipped; ephemeral training units may be mutated in place.
    """
    initial = _positive(settings.initial_capital_rub, "начальный капитал")
    proof = (
        {**unit, "selection": dict(unit["selection"])}
        if metrics_only
        else deepcopy(unit)
        if copy_unit
        else unit
    )
    terms = _Terms(proof, contracts, settings)
    rows = proof["series"]
    strategy_name = proof["selection"]["strategy"]
    if metrics_only:
        from moex_spread_lab.core._fastcore_loader import load_fastcore

        fc = load_fastcore()
        if fc is not None:
            result = _size_proof_fast_c(
                fc,
                proof,
                terms,
                marks,
                settings,
                initial,
                strategy_name,
                _scan_cache=_scan_cache,
                _scan_key=_scan_key,
                scalar_summary=scalar_summary,
            )
            if result is not None:
                return result
    prices = [_positive(marks[r["date"]], "отметка LQDT") for r in rows]
    intents = {t["entry"]["date"]: t for t in proof["trades"]}
    accepted, rejected, ledger = [], [], []
    position = None
    cumulative, free, posted = 0.0, initial, 0.0
    wealth = initial
    peak = drawdown = 0.0
    paused = False
    shortfalls = 0

    def scaled(costs, q):
        return {k: _money(v * q) for k, v in costs.items()}

    def move(pos, end, point):
        change = _money(
            pos["sign"] * (end - pos["mark"]) * point * pos["trade"]["quantity"]
        )
        pos["trade"]["_gross"] += change
        pos["mark"] = end
        return change

    for i, row in enumerate(rows):
        day = row["date"]
        unit_funding = row.get("funding_rub")
        unit_funding_contracts = row.get("funding_contracts", 0)
        eligible = row.get("volume_eligible", True)
        entry_eligible = row.get("entry_eligible", eligible)
        exit_eligible = row.get("exit_eligible", eligible)
        daily = cost = 0.0
        events = None if metrics_only else []
        funding_owner = None
        # Account resources observable at this row's fill, not at its later close.
        fill_wealth = wealth
        if position is not None:
            t = position["trade"]
            exiting = day == t["exit"]["date"]
            should_exit = exiting and (exit_eligible or t.get("open_at_end"))
            if should_exit or (eligible and not exiting):
                end = t["exit"]["spread_fill"] if should_exit else row["spread"]
                at_open = should_exit and terms.open_fill and not t.get("open_at_end")
                point = terms.fill_point(i, at_open=at_open) if should_exit else terms.point(i)
                change = move(position, end, point)
                daily += change
                if should_exit:
                    # Forced end marks can still include the last proxy clearing payment.
                    if unit_funding_contracts:
                        funding_owner = t
                    _, unit_costs = terms.at_fill(
                        t["exit"],
                        i,
                        at_open=terms.open_fill and not t.get("open_at_end"),
                    )
                    t["exit"]["costs"] = (
                        {}
                        if t.get("open_at_end")
                        and strategy_name == "funding_only"
                        else scaled(unit_costs, t["quantity"])
                    )
                    fee = sum(t["exit"]["costs"].values())
                    cost += fee
                    fill_wealth += change - fee
                    if events is not None:
                        events.append(f"EXIT #{t['number']}")
                    position = None
            if position is not None:
                funding_owner = t

        candidate = intents.get(day)
        if candidate is not None and entry_eligible:
            if position is not None:
                raise W0Error(
                    "Перекрывающиеся сигналы не поддерживаются одним счётом окна."
                )
            margin, entry_costs = terms.at_fill(
                candidate["entry"], i, at_open=terms.open_fill
            )
            budget = max(0.0, min(initial, fill_wealth))
            q = (
                0
                if paused
                else maximum_quantity(
                    budget,
                    margin,
                    entry_costs,
                    stress=not settings.allow_failed_capital,
                    native_spread=terms.native_order,
                )
            )
            if not q:
                if not metrics_only:
                    why = (
                        "Недостаточно свободных средств для маржи; новые входы остановлены."
                        if paused
                        else "Недостаточно средств на один спред, издержки и обязательный резерв."
                    )
                    rejected.append(
                        {
                            "date": day,
                            "quantity": 0,
                            "reason": why,
                            "available_capital_rub": _money(budget),
                            "margin_per_pair_rub": _money(margin),
                            "signal": candidate.get("signal"),
                        }
                    )
                    events.append("SKIP: " + why)
            else:
                t = candidate
                t.update(
                    number=len(accepted) + 1,
                    quantity=q,
                    margin_rub=_money(margin * q),
                    _gross=0.0,
                    _funding=0.0,
                )
                if not metrics_only:
                    t["sizing"] = {
                        "mode": AUTO_SIZING,
                        "available_capital_rub": _money(budget),
                        "margin_per_pair_rub": margin,
                        "entry_cost_per_pair_rub": sum(entry_costs.values()),
                        "stress_reserve_enforced": not settings.allow_failed_capital,
                    }
                    t["entry"]["margin_rub"] = t["margin_rub"]
                t["entry"]["costs"] = scaled(entry_costs, q)
                fee = sum(t["entry"]["costs"].values())
                cost += fee
                accepted.append(t)
                if events is not None:
                    events.append(f"ENTRY #{t['number']}")
                sign = 1 if t["side"] == "long_spread" else -1
                position = {"trade": t, "sign": sign, "mark": t["entry"]["spread_fill"]}
                funding_owner = t
                same_day_exit = t["exit"]["date"] == day
                if eligible:
                    daily += move(
                        position,
                        t["exit"]["spread_fill"] if same_day_exit else row["spread"],
                        terms.point(i),
                    )
                else:
                    position["mark"] = t["entry"]["spread_fill"]
                if same_day_exit:
                    _, exit_costs = terms.at_fill(t["exit"], i, at_open=False)
                    t["exit"]["costs"] = (
                        {}
                        if t.get("open_at_end")
                        and strategy_name == "funding_only"
                        else scaled(exit_costs, q)
                    )
                    cost += sum(t["exit"]["costs"].values())
                    if events is not None:
                        events.append(f"EXIT #{t['number']}")
                    position = None

        funding_q = (
            funding_owner["quantity"]
            if funding_owner is not None and unit_funding_contracts
            else 0
        )
        funding = _money(float(unit_funding or 0.0) * funding_q)
        if funding_owner is not None:
            funding_owner["_gross"] += funding
            funding_owner["_funding"] += funding
        daily = _money(daily + funding)
        cost = _money(cost)
        cumulative = _money(cumulative + daily - cost)
        growth = prices[i] / prices[i - 1] if i else 1.0
        wealth = free * growth + posted + daily - cost
        if position is None:
            posted = 0.0
        elif eligible or entry_eligible:
            posted = terms.posted_margin(i, position["trade"]["quantity"])
        # Journal-only zero-volume rows cannot revalue the position or its margin.
        free = wealth - posted
        shortfall = free < -1e-7
        shortfalls += shortfall
        excess = wealth - initial * prices[i] / prices[0]
        peak = max(peak, excess)
        drawdown = max(drawdown, peak - excess)
        # Only a cash shortfall stops new entries: without free cash no margin can
        # be posted. Benchmark-relative drawdown limits no longer pause the book.
        paused |= shortfall
        if metrics_only:
            ledger.append(
                {
                    "date": day,
                    "cumulative_net_rub": cumulative,
                    "account_value_rub": wealth,
                    "cash_shortfall": shortfall,
                }
            )
            continue
        q_held = position["trade"]["quantity"] if position is not None else 0
        row.update(
            quantity=q_held,
            margin_rub=_money(posted),
            funding_rub=funding if unit_funding is not None else None,
        )
        if "funding_contracts" in row:
            row["funding_contracts"] = funding_q
        ledger.append(
            {
                "date": day,
                "timestamp": row.get("timestamp", day),
                "session_date": row.get("session_date", day[:10]),
                "benchmark_mark_date": row.get("benchmark_mark_date"),
                "position": position["sign"] if position else 0,
                "quantity": q_held,
                "mark_pnl_rub": daily,
                "costs_rub": cost,
                "funding_rub": funding if unit_funding is not None else None,
                "lqdt_on_margin_rub": None,
                "cumulative_net_rub": cumulative,
                "event": " + ".join(events) or None,
                "posted_margin_rub": posted,
                "free_cash_rub": free,
                "account_value_rub": wealth,
                "cash_shortfall": shortfall,
            }
        )

    if metrics_only:
        for t in accepted:
            t["gross_rub"] = _money(t.pop("_gross"))
            t.pop("_funding")
            t["costs_rub"] = _money(
                sum(sum(t[s]["costs"].values()) for s in ("entry", "exit"))
            )
            t["net_rub"] = _money(t["gross_rub"] - t["costs_rub"])
            t["lqdt_on_margin_rub"] = _money(
                t["margin_rub"] * (marks[t["exit"]["date"]] / marks[t["entry"]["date"]] - 1)
            )
        proof["selection"]["sizing"] = AUTO_SIZING
        proof["initial_capital_rub"] = initial
        proof["trades"] = accepted
        proof["ledger"] = ledger
        proof["sizing"] = {"initial_capital_rub": initial}
        return proof

    for t in accepted:
        t["gross_rub"] = _money(t.pop("_gross"))
        total_funding = _money(t.pop("_funding"))
        if t.get("funding_rub") is not None or not terms.native:
            t["funding_rub"] = total_funding
        t["costs_rub"] = _money(
            sum(sum(t[s]["costs"].values()) for s in ("entry", "exit"))
        )
        t["net_rub"] = _money(t["gross_rub"] - t["costs_rub"])
        t["lqdt_on_margin_rub"] = _money(
            t["margin_rub"] * (marks[t["exit"]["date"]] / marks[t["entry"]["date"]] - 1)
        )
        t["excess_rub"] = _money(t["net_rub"] - t["lqdt_on_margin_rub"])
        t["result"] = (
            "win" if t["net_rub"] > 0 else "loss" if t["net_rub"] < 0 else "flat"
        )
        if "gross_rub_static" in t:
            t["gross_rub_static"] = _money(t["gross_rub_static"] * t["quantity"])
    closed = [t for t in accepted if not t.get("open_at_end")]
    gross = _money(sum(t["gross_rub"] for t in accepted))
    costs = _money(sum(t["costs_rub"] for t in accepted))
    hurdle = _money(sum(t["lqdt_on_margin_rub"] for t in accepted))
    quantities = [t["quantity"] for t in accepted]
    proof["selection"].update(
        sizing=AUTO_SIZING,
        quantity=None,
        slippage_ticks=settings.slippage_ticks,
        initial_capital_rub=initial,
    )
    if terms.native:
        editable = (
            {"entry_z", "exit_z", "window"}
            if proof["selection"]["strategy"] == "zscore_mean_reversion"
            else {"threshold_pp"}
        )
        parameters = proof["selection"]["parameters"]
        proof["selection"]["strategy_conventions"] = {
            k: v for k, v in parameters.items() if k not in editable
        }
        proof["selection"]["parameters"] = {
            k: v for k, v in parameters.items() if k in editable
        }
    proof.update(
        initial_capital_rub=initial,
        trades=accepted,
        ledger=ledger,
        skipped_entries=rejected,
        sizing={
            "policy": SIZING_POLICY,
            "mode": AUTO_SIZING,
            "initial_capital_rub": initial,
            "entry_capital_ceiling_rub": initial,
            "stress_reserve_enforced": not settings.allow_failed_capital,
            "exit_cost_reserve": "entry_priced_one_side",
            "uncertainty_buffer_basis": "per_spread" if terms.native_order else "per_account",
            "additional_funding_rub": 0,
        },
    )
    proof["summary"].update(
        trades=len(accepted),
        closed_trades=len(closed),
        open_positions=len(accepted) - len(closed),
        winners=sum(t["net_rub"] > 0 for t in closed),
        gross_rub=gross,
        costs_rub=costs,
        net_rub=_money(gross - costs),
        cost_totals=_cost_totals(accepted),
        lqdt_on_margin_rub=hurdle,
        excess_over_lqdt_rub=_money(gross - costs - hurdle),
        max_excess_drawdown_rub=_money(drawdown),
        ledger_reconciliation_rub=_money(gross - costs - cumulative),
        skipped_entries=len(rejected),
        min_quantity=min(quantities, default=0),
        max_quantity=max(quantities, default=0),
        cash_shortfall_rows=shortfalls,
    )
    if terms.native:
        proof["summary"]["fx_point_value_drift_rub"] = _money(
            sum(
                t["gross_rub"] - t.get("gross_rub_static", t["gross_rub"])
                for t in accepted
            )
        )
    if not metrics_only:
        # Presentation fields (notes, run_id) are only required for the published
        # report. Training probes (`metrics_only`) are consumed by _measure, which
        # reads ledger/trades/sizing — never run_id or notes. Skipping them here
        # also avoids requiring `unit["run_id"]`, which partial calendar replays
        # do not carry (only the full _calendar_proof sets it).
        proof["notes"].extend(
            [
                f"Размер каждого входа — максимум целых спредов по последней доступной стоимости счёта, не выше исходного капитала {initial:,.2f} ₽ из общих настроек; размер удерживается до выхода. Прибыль не расширяет исходный лимит, пополнений нет.",
                "Размер покрывает ГО, комиссию и проскальзывание входа плюс резерв выхода по ценам входа. При обязательном стресс-гейте также учитываются 3×ГО, VM-буфер 5 000 ₽ на спред и буфер неопределённости 5 000 ₽ (для календаря — на спред, для пары — на счёт).",
                "Это максимум по модели капитала, не гарантия доступного объёма в стакане. Пропуски записаны отдельно; новая попытка — на следующем входном сигнале стратегии.",
            ]
        )
        proof["run_id"] = _run_id(
            {
                "unit_intents": unit["run_id"],
                "sizing": proof["sizing"],
                "settings": settings.metadata(),
                "trades": accepted,
                "ledger": ledger,
                "skipped_entries": rejected,
            }
        )

    return proof
