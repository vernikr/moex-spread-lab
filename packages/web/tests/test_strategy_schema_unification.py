"""The web form consumes one strategy schema: `/api/strategies` (#149).

The browser smokes cover the rendered controls end to end. This module pins the
schema helpers that drive those controls: field order, defaults, family variants
and the parameter translation between universe variants, all evaluated in Node
against the real registry payload.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from moex_spread_lab.web.routes_proof import strategy_registry_payload

_JS_DIR = Path(__file__).resolve().parents[1] / "src" / "moex_spread_lab" / "web" / "static" / "js"

#: Loads `state.js` without a DOM and exercises the schema helpers against the
#: payload written by the test. `vm.SourceTextModule` keeps the ES-module graph.
_HARNESS = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const root = process.argv[2];
const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const cache = new Map();
function getModule(file) {
  const p = path.resolve(file);
  if (!cache.has(p)) cache.set(p, new vm.SourceTextModule(fs.readFileSync(p, 'utf8'), { identifier: p }));
  return cache.get(p);
}
const norm = v => JSON.stringify(v, (k, x) =>
  x && typeof x === 'object' && !Array.isArray(x)
    ? Object.keys(x).sort().reduce((o, kk) => (o[kk] = x[kk], o), {})
    : x);
(async () => {
  const mod = getModule(path.join(root, 'state.js'));
  await mod.link(async (spec, ref) => getModule(path.resolve(path.dirname(ref.identifier), spec)));
  await mod.evaluate();
  const api = mod.namespace;
  const checks = [];
  const eq = (name, got, want) => {
    if (norm(got) !== norm(want)) checks.push(`${name}: ${norm(got)} != ${norm(want)}`);
  };
  // No frontend mirror: before the registry arrives there is no strategy schema.
  eq('empty before install', api.getStrategyMeta('mean_reversion'), null);
  api.installStrategyRegistry(payload);
  eq('pair field order', api.orderedStrategySpecs('mean_reversion').map(s => s.name), ['entry_z', 'exit_z', 'lookback']);
  eq('pair defaults', api.defaultParametersFor('mean_reversion'), { entry_z: 2, exit_z: 0.5, lookback: 20 });
  eq('calendar field order', api.orderedStrategySpecs('zscore_mean_reversion', { universe: 'calendar_native', timeframe: '1d', monthly: false }).map(s => s.name), ['entry_z', 'exit_z', 'window']);
  eq('calendar monthly defaults', api.defaultParametersFor('zscore_mean_reversion', { universe: 'calendar_native', timeframe: '1d', monthly: true }), { entry_z: 1.5, exit_z: 0.5, window: 7 });
  eq('family variant', api.familyVariant('mean_reversion', 'calendar_native')?.id, 'zscore_mean_reversion');
  eq('family translate', api.familyParameters({ lookback: 30, entry_z: 2, exit_z: 0.5 }, 'mean_reversion', 'zscore_mean_reversion'), { window: 30, entry_z: 2, exit_z: 0.5 });
  eq('family translate back', api.familyParameters({ window: 30, entry_z: 2, exit_z: 0.5 }, 'zscore_mean_reversion', 'mean_reversion'), { lookback: 30, entry_z: 2, exit_z: 0.5 });
  eq('convergence gated by asset', api.calendarStrategySupported({ convergence: false }, 'convergence_to_ruonia'), false);
  eq('convergence allowed by asset', api.calendarStrategySupported({ convergence: true }, 'convergence_to_ruonia'), true);
  eq('pair option order', api.strategyOptions('pair_screen').map(m => m.id), ['funding_only', 'mean_reversion']);
  eq('calendar option order', api.strategyOptions('calendar_native').map(m => m.id), ['convergence_to_ruonia', 'zscore_mean_reversion']);
  eq('carry fill', api.strategyFill('funding_only'), 'same_day_settlement');
  eq('price fill', api.strategyFill('mean_reversion'), 'reference_same_bar');
  if (checks.length) {
    console.error(checks.join('\n'));
    process.exitCode = 1;
  } else {
    console.log('strategy schema harness: ok');
  }
})().catch(err => {
  console.error(err);
  process.exitCode = 1;
});
"""


class StrategySchemaTests(unittest.TestCase):
    """The form schema helpers are driven by the served registry payload."""

    def test_schema_helpers_consume_the_registry_payload(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not found; strategy schema harness skipped")
        payload = strategy_registry_payload()
        with tempfile.TemporaryDirectory() as tmp:
            payload_path = Path(tmp) / "strategies.json"
            payload_path.write_text(json.dumps(payload), encoding="utf-8")
            harness_path = Path(tmp) / "harness.js"
            harness_path.write_text(_HARNESS, encoding="utf-8")
            result = subprocess.run(
                [
                    node,
                    "--no-warnings",
                    "--experimental-vm-modules",
                    str(harness_path),
                    str(_JS_DIR),
                    str(payload_path),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertIn("strategy schema harness: ok", result.stdout)

    def test_form_modules_have_no_per_strategy_branches(self) -> None:
        """No built-in id lists or per-strategy switches survive in the form modules."""
        forbidden = {
            "strategy-form.js": (
                "mean_reversion",
                "zscore_mean_reversion",
                "funding_only",
                "convergence_to_ruonia",
                "SOLO_BUILTIN_IDS",
                "GROUP_SHORT_LABELS",
            ),
            "strategy-plans.js": (
                "mean_reversion",
                "zscore_mean_reversion",
                "translateParams",
            ),
        }
        for name, needles in forbidden.items():
            source = (_JS_DIR / name).read_text(encoding="utf-8")
            for needle in needles:
                self.assertNotIn(needle, source, f"{name} still hardcodes {needle!r}")
        self.assertFalse(
            (_JS_DIR / "strategy-registry.js").exists(), "frontend registry mirror removed"
        )


#: Drives `strategy-plans.js` / `state.js` routing for two family variants whose
#: parameter grids disagree (float step 0.1 vs 0.5) and a calendar-only plugin.
_ROUTING_HARNESS = r"""
const fs = require('fs'), vm = require('vm'), path = require('path');
const root = process.argv[2];
const cache = new Map();
function getModule(file) {
  const p = path.resolve(file);
  if (!cache.has(p)) cache.set(p, new vm.SourceTextModule(fs.readFileSync(p, 'utf8'), { identifier: p }));
  return cache.get(p);
}
globalThis.document = {
  getElementById: id => id === 'cohort-editor'
    ? ({ querySelectorAll: () => [{ dataset: { axis: 'band_z' }, value: '1.3', valueAsNumber: 1.3, validity: { valid: true } }] })
    : ({ value: 'plugin_cal', hidden: false, textContent: '' }),
  dispatchEvent: () => {},
};
globalThis.window = { fetch: () => {} };
globalThis.performance = { now: () => 0 };
(async () => {
  const state = getModule(path.join(root, 'state.js'));
  const plans = getModule(path.join(root, 'strategy-plans.js'));
  const link = async (spec, ref) => getModule(path.resolve(path.dirname(ref.identifier), spec));
  await state.link(link); await state.evaluate();
  await plans.link(link); await plans.evaluate();
  const api = state.namespace;
  api.installStrategyRegistry({ strategies: [
    { id: 'plugin_pair', family_id: 'fam', is_builtin: false, proof_enabled: true,
      supported_universes: ['pair_screen'], ui_order: ['band_z'],
      parameters: [{ name: 'band_z', kind: 'float', default: 2.0, min_value: 1.0, max_value: 3.0, step: 0.1 }] },
    { id: 'plugin_cal', family_id: 'fam', is_builtin: false, proof_enabled: true,
      supported_universes: ['calendar_native'], ui_order: ['band_z'],
      parameters: [{ name: 'band_z', kind: 'float', default: 2.0, min_value: 1.0, max_value: 3.0, step: 0.5 }],
      context_parameters: { calendar_native: { '1d': { quarterly: [
        { name: 'band_z', kind: 'float', default: 2.0, min_value: 1.0, max_value: 3.0, step: 0.5 }], monthly: [] } } } },
    { id: 'cal_only_plugin', family_id: 'cal_only_plugin', is_builtin: false, proof_enabled: true,
      proof_adapter: 'calendar_price_intents.v1', supported_universes: ['calendar_native'],
      parameters: [{ name: 'band_z', kind: 'float', default: 2, min_value: 1, max_value: 3, step: 0.1 }] },
  ]});
  api.state.branches.set('calendar_native|SR', { key: 'calendar_native|SR', asset: { asset: 'SR', monthly: false } });
  const leaf = { key: 'calendar_native|SR|SRH3/SRM3', branch: 'calendar_native|SR',
    target: { universe: 'calendar_native', asset: 'SR', pair: 'SRH3/SRM3' } };
  api.state.leaves.set(leaf.key, leaf);
  api.state.checked.add(leaf.key);
  api.state.sharedStrategy = 'plugin_pair';
  const offGrid = plans.namespace.leafPlan(leaf);
  console.log(JSON.stringify({
    offGridCommon: offGrid.common,
    offGridParams: offGrid.req.parameters,
    calendarOnlyOnPair: api.pairStrategyAvailable(
      { id: 'CNYRUBF/CRZ6', proof_strategies: { mean_reversion: true }, strategies: { mean_reversion: true } },
      'cal_only_plugin',
    ),
  }));
})().catch(err => { console.error(err); process.exitCode = 1; });
"""


class StrategyRoutingTests(unittest.TestCase):
    """Shared-plan routing honours the per-window parameter grid and universes (##335, #336)."""

    def test_shared_plan_falls_back_when_the_window_grid_rejects_the_point(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node not found; strategy routing harness skipped")
        with tempfile.TemporaryDirectory() as tmp:
            harness_path = Path(tmp) / "routing.js"
            harness_path.write_text(_ROUTING_HARNESS, encoding="utf-8")
            result = subprocess.run(
                [
                    node,
                    "--no-warnings",
                    "--experimental-vm-modules",
                    str(harness_path),
                    str(_JS_DIR),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = json.loads(result.stdout)
        # #335: 1.3 is off the calendar variant's 0.5 grid, so the window keeps its own plan.
        self.assertFalse(payload["offGridCommon"], payload)
        self.assertEqual(payload["offGridParams"], {"band_z": 2}, payload)
        # #336: a calendar-only plugin must not be routed onto a pair window.
        self.assertFalse(payload["calendarOnlyOnPair"], payload)


if __name__ == "__main__":
    unittest.main()
