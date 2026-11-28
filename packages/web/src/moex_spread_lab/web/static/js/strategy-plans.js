import { $, finishProgress } from './dom.js';
import {
  calendarStrategySupported,
  currentPair,
  defaultParametersFor,
  familyParameters,
  familyVariant,
  orderedStrategySpecs,
  pairAvailability,
  pairStrategyAvailable,
  settingsKey,
  state,
  strategyFamily,
  strategyFill,
  targetKey,
} from './state.js';

export const activeInputs = () => state.numericInputs.filter(n => n.isConnected && !n.closest('.hidden'));
export const sharedInputs = () => [...$('cohort-editor').querySelectorAll('.number-shell input')];
export const activeStrategyName = () => (state.universe === 'pair_screen' ? $('strategy').value : $('cstrategy').value);
export const formKey = (key, name) => key + '|' + name;
export const mappedStrategy = name => familyVariant(strategyFamily(name), state.universe)?.id || name;

export function currentRequest() {
  const strategy = activeStrategyName();
  const parameters = Object.fromEntries(activeInputs().map(n => [n.dataset.axis, n.value === '' ? NaN : Number(n.value)]));
  const cal = state.universe === 'calendar_native';
  return {
    universe: state.universe,
    ...(cal ? { timeframe: state.preferences.timeframe, asset: state.branches.get(state.branchId)?.id } : {}),
    pair: currentPair(),
    strategy,
    fill: strategyFill(strategy),
    sizing: 'max_affordable',
    parameters,
  };
}

export const hasSelection = () =>
  !!state.leafId && (state.universe === 'calendar_native' || pairStrategyAvailable(pairAvailability(currentPair()), activeStrategyName()));

export function proofPlanKey(req) {
  const fill = req.fill || strategyFill(req.strategy);
  return JSON.stringify([targetKey(req), req.strategy, fill, req.sizing, Object.entries(req.parameters || {}).sort(), settingsKey()]);
}

function defaultLeafRequest(l) {
  const cal = l.target.universe === 'calendar_native';
  const name = activeStrategyName();
  const saved = state.formStates.get(formKey(l.key, name));
  const parameters =
    saved?.parameters ||
    defaultParametersFor(name, {
      universe: l.target.universe,
      timeframe: state.preferences.timeframe,
      monthly: cal && !!state.branches.get(l.branch)?.asset?.monthly,
    });
  return {
    ...l.target,
    ...(cal ? { timeframe: state.preferences.timeframe } : {}),
    strategy: saved ? saved.strategy : name,
    parameters,
    sizing: 'max_affordable',
    fill: strategyFill(name),
  };
}

export function sharedRequest() {
  const parameters = Object.fromEntries(sharedInputs().map(n => [n.dataset.axis, n.valueAsNumber]));
  return { strategy: state.sharedStrategy, parameters, sizing: 'max_affordable', fill: strategyFill(state.sharedStrategy) };
}

export function groupSpec() {
  const body = { ...sharedRequest(), targets: [...state.checked].sort().map(k => state.leaves.get(k).target) };
  return { body, key: JSON.stringify([settingsKey(), body]) };
}

function fieldRangeError(inputs) {
  const n = inputs.find(x => x.value === '' || !x.validity.valid);
  return n ? `Проверьте поле «${n.labels[0].textContent}»: диапазон ${n.min}–${n.max}, шаг ${n.step}.` : '';
}

export const invalidForm = () => fieldRangeError(activeInputs());

export function pointError(inputs) {
  const err = fieldRangeError(inputs);
  if (err) return err;
  const entry = inputs.find(x => x.dataset.axis === 'entry_z');
  const exit = inputs.find(x => x.dataset.axis === 'exit_z');
  return entry && exit && exit.valueAsNumber >= entry.valueAsNumber ? 'Порог выхода должен быть ниже порога входа.' : '';
}

function specAccepts(s, value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return false;
  if (s.kind === 'int' && !Number.isInteger(value)) return false;
  if (value < s.min_value || value > s.max_value) return false;
  const grid = (value - s.min_value) / s.step;
  return Math.abs(grid - Math.round(grid)) <= 1e-6;
}

function specsAcceptPoint(strategyId, params, options) {
  const specs = orderedStrategySpecs(strategyId, options);
  if (!specs.length) return false;
  if (!specs.every(s => specAccepts(s, params[s.name]))) return false;
  return !('entry_z' in params && 'exit_z' in params && params.exit_z >= params.entry_z);
}

function commonPlan(own, target, key) {
  let req = own;
  let common = false;
  if (target && state.sharedStrategy && state.checked.has(key) && !pointError(sharedInputs())) {
    const g = sharedRequest();
    const cal = target.universe === 'calendar_native';
    const name = familyVariant(strategyFamily(g.strategy), target.universe)?.id || g.strategy;
    const p = familyParameters(g.parameters, g.strategy, name);
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

export function tuneState(message, ok = false) {
  const n = $('tune-state');
  n.textContent = message;
  n.classList.toggle('ok', ok);
}

export function tuneButton(busy = state.tuning, pctVal = null) {
  const btn = $('auto-tune');
  btn.disabled = busy || (!state.leafId && !state.checked.size);
  btn.setAttribute('aria-busy', String(busy));
  if (!busy) {
    btn.style.removeProperty('--tune-pct');
    $('tune-label').textContent = 'Подобрать стратегию и параметры';
    return;
  }
  const p = pctVal == null ? null : Math.max(0, Math.min(100, Math.round(pctVal)));
  btn.style.setProperty('--tune-pct', (p ?? 0) + '%');
  $('tune-label').textContent = p == null ? 'Идёт подбор…' : `Подбор · ${p}%`;
}

export function cancelSearch() {
  if (!state.tuning) return;
  state.searchVersion++;
  state.searchController?.abort();
  state.searchController = null;
  state.tuning = false;
  finishReview(false);
  finishProgress('search');
  tuneButton();
}

export function clearGroup() {
  $('batch-result').hidden = true;
  $('batch-result').replaceChildren();
}
