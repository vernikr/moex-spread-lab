/** Request builders and cohort plans for the strategy form. */

import { $ } from './dom.js';
import { activeInputs, activeStrategyName, formKey, sharedInputs } from './form-inputs.js';
import {
  defaultParametersFor,
  isMR,
  orderedStrategySpecs,
  pairAvailability,
  pairStrategyAvailable,
  selectedRoot,
  settingsKey,
  state,
  strategyFill,
  targetKey,
  currentPair,
  calendarStrategySupported,
} from './state.js';

export function pairRequest() {
  const strategy = $('strategy').value;
  const parameters = Object.fromEntries(activeInputs().map(n => [n.dataset.axis, n.value === '' ? NaN : Number(n.value)]));
  return { universe: 'pair_screen', pair: currentPair(), strategy, fill: strategyFill(strategy), sizing: 'max_affordable', parameters };
}

export function calendarRequest() {
  const strategy = $('cstrategy').value;
  const root = selectedRoot();
  const parameters = Object.fromEntries(activeInputs().map(n => [n.dataset.axis, n.value === '' ? NaN : Number(n.value)]));
  return {
    universe: 'calendar_native',
    timeframe: state.preferences.timeframe,
    asset: root.id,
    pair: currentPair(),
    strategy,
    fill: strategyFill(strategy),
    sizing: 'max_affordable',
    parameters,
  };
}

export const currentRequest = () => (state.universe === 'pair_screen' ? pairRequest() : calendarRequest());
export const hasSelection = () =>
  !!state.leafId && (state.universe === 'calendar_native' || pairStrategyAvailable(pairAvailability(currentPair()), activeStrategyName()));

export function proofPlanKey(req) {
  const fill = req.fill || strategyFill(req.strategy);
  return JSON.stringify([targetKey(req), req.strategy, fill, req.sizing, Object.entries(req.parameters || {}).sort(), settingsKey()]);
}

export function defaultLeafRequest(l) {
  const cal = l.target.universe === 'calendar_native';
  const tf = cal ? { timeframe: state.preferences.timeframe } : {};
  const name = activeStrategyName();
  const saved = state.formStates.get(formKey(l.key, name));
  const fill = strategyFill(name);
  if (saved) return { ...l.target, ...tf, strategy: saved.strategy, parameters: saved.parameters, sizing: 'max_affordable', fill };
  const monthly = cal ? !!state.branches.get(l.branch)?.asset?.monthly : false;
  const parameters = defaultParametersFor(name, { universe: l.target.universe, timeframe: state.preferences.timeframe, monthly });
  return { ...l.target, ...tf, strategy: name, parameters, sizing: 'max_affordable', fill };
}

function translateParams(params, strategy) {
  const p = { ...params };
  if (strategy === 'mean_reversion' && 'window' in p) { p.lookback = p.window; delete p.window; }
  if (strategy === 'zscore_mean_reversion' && 'lookback' in p) { p.window = p.lookback; delete p.lookback; }
  return p;
}

export function sharedRequest() {
  const parameters = Object.fromEntries(sharedInputs().map(n => [n.dataset.axis, n.valueAsNumber]));
  return { strategy: state.sharedStrategy, parameters, sizing: 'max_affordable', fill: strategyFill(state.sharedStrategy) };
}

export function pointError(inputs) {
  const n = inputs.find(x => x.value === '' || !x.validity.valid);
  if (n) return `Проверьте поле «${n.labels[0].textContent}»: диапазон ${n.min}–${n.max}, шаг ${n.step}.`;
  const entry = inputs.find(x => x.dataset.axis === 'entry_z');
  const exit = inputs.find(x => x.dataset.axis === 'exit_z');
  return entry && exit && exit.valueAsNumber >= entry.valueAsNumber ? 'Порог выхода должен быть ниже порога входа.' : '';
}

export function invalidForm() {
  const n = activeInputs().find(x => x.value === '' || !x.validity.valid);
  return n ? `Проверьте поле «${n.labels[0].textContent}»: диапазон ${n.min}–${n.max}, шаг ${n.step}.` : '';
}

function specsAcceptPoint(strategyId, params, options) {
  const specs = orderedStrategySpecs(strategyId, options);
  if (!specs.length) return false;
  for (const s of specs) {
    const v = params[s.name];
    if (typeof v !== 'number' || !Number.isFinite(v) || v < s.min_value || v > s.max_value) return false;
  }
  if ('entry_z' in params && 'exit_z' in params && params.exit_z >= params.entry_z) return false;
  return true;
}

export function commonPlan(own, target, key) {
  let req = own;
  let common = false;
  if (target && state.sharedStrategy && state.checked.has(key) && !pointError(sharedInputs())) {
    const g = sharedRequest();
    const cal = target.universe === 'calendar_native';
    const name = isMR(g.strategy) ? (cal ? 'zscore_mean_reversion' : 'mean_reversion') : g.strategy;
    const p = translateParams(g.parameters, name);
    const assetObj = cal ? state.branches.get('calendar_native|' + target.asset)?.asset : null;
    const supported = cal ? calendarStrategySupported(assetObj, name) : pairStrategyAvailable(pairAvailability(target.pair), name);
    if (supported && specsAcceptPoint(name, p, { universe: target.universe, timeframe: state.preferences.timeframe, monthly: !!assetObj?.monthly })) {
      req = { ...target, strategy: name, parameters: p, sizing: 'max_affordable', fill: strategyFill(name) };
      common = true;
    }
  }
  return { req, common, key: proofPlanKey(req) };
}

export const leafPlan = l => commonPlan(l.key === state.leafId ? currentRequest() : defaultLeafRequest(l), l.target, l.key);

export function chartPlan() {
  const target = state.leaves.get(state.leafId)?.target;
  const plan = commonPlan(currentRequest(), target, state.leafId);
  const note = $('chart-selection-scope');
  note.hidden = !state.checked.size;
  note.textContent = `График и журнал: ${target?.pair || '—'} · ${plan.common ? 'общие параметры' : 'параметры окна'}`;
  return plan;
}

export function finishReview(yes) {
  if (!$('compatibility').open) return;
  $('compatibility').close();
  const resolve = state.resolveCompatibility;
  state.resolveCompatibility = null;
  resolve?.(yes);
}
