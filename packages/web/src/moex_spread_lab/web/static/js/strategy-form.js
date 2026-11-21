import { $, CATSTATUS_RU, bindBackdropClose, el, fmt, setSearchProgress } from './dom.js';
import {
  bindLamp,
  drawLamps,
  hidePlateauTip,
  runGroupProfileStream,
  showGroupLampsReading,
  stopProfile,
} from './plateau-lamps.js';
import {
  calendarProofPlugins,
  calendarStrategySupported,
  currentAsset,
  currentPair,
  defaultParametersFor,
  getStrategyMeta,
  isMR,
  orderedStrategySpecs,
  pairAvailability,
  pairHasLocalData,
  pairPlugin,
  pairStrategyAvailable,
  pairUnavailable,
  selectedRoot,
  settingsKey,
  state,
  strategyFill,
  strategyLabel,
  targetKey,
} from './state.js';

export { hidePlateauTip, stopProfile };

let callbacks = {
  scheduleShow: () => {},
  invalidateProof: () => {},
  refreshGlobalSettings: async () => {},
  refreshWindowReadings: () => {},
};

export function configureStrategyFormCallbacks(next) {
  callbacks = { ...callbacks, ...next };
}

const dirtyInputs = new WeakSet();

const SOLO_BUILTIN_IDS = {
  pair_screen: { entry_z: 'entry', exit_z: 'exit', lookback: 'lookback' },
  calendar_native: { entry_z: 'centry', exit_z: 'cexit', window: 'clookback', threshold_pp: 'cthreshold' },
};

const GROUP_SHORT_LABELS = {
  funding_only: 'Фондирование',
  convergence_to_ruonia: 'Сходимость к RUONIA',
};

function soloInputId(universe, strategyId, paramName) {
  const meta = getStrategyMeta(strategyId);
  const builtin = meta
    ? meta.is_builtin === true && !pairPlugin(strategyId)
    : !pairPlugin(strategyId) &&
      strategyId in { mean_reversion: 1, funding_only: 1, zscore_mean_reversion: 1, convergence_to_ruonia: 1 };
  if (builtin) {
    const mapped = SOLO_BUILTIN_IDS[universe]?.[paramName];
    if (mapped) return mapped;
  }
  return (universe === 'calendar_native' ? 'cplugin-' : 'plugin-') + paramName;
}

export function createParameterField(spec, inputId, point, onBind) {
  const box = el('div', 'control');
  const label = el('label', '', spec.label_ru);
  const shell = el('span', 'number-shell');
  const input = document.createElement('input');
  const lamp = el('button', 'plateau-dot');
  input.id = inputId;
  input.dataset.axis = spec.name;
  input.type = 'number';
  input.min = spec.min_value;
  input.max = spec.max_value;
  input.step = spec.step;
  input.defaultValue = spec.default;
  input.value = point?.[spec.name] ?? spec.default;
  label.htmlFor = input.id;
  lamp.type = 'button';
  lamp.setAttribute('aria-label', 'Устойчивость: нет оценки');
  shell.append(input, lamp);
  box.append(label, shell);
  onBind(input);
  return box;
}

function detachContainerInputs(container) {
  if (!container) return;
  for (const input of container.querySelectorAll('input')) {
    const idx = state.numericInputs.indexOf(input);
    if (idx >= 0) state.numericInputs.splice(idx, 1);
  }
  container.replaceChildren();
  delete container.dataset.mountedKey;
}

function mountSoloControls(universe, strategyId) {
  if (!strategyId) return;
  const meta = getStrategyMeta(strategyId);
  const monthly = universe === 'calendar_native' ? !!currentAsset()?.monthly : false;
  const tf = state.preferences.timeframe || '1d';
  const specs = orderedStrategySpecs(strategyId, { universe, timeframe: tf, monthly });
  if (universe === 'pair_screen') {
    const builtin = meta ? meta.is_builtin === true && !pairPlugin(strategyId) : !pairPlugin(strategyId);
    const activeContainer = builtin ? $('pair-params') : $('pair-plugin-params');
    const idleContainer = builtin ? $('pair-plugin-params') : $('pair-params');
    detachContainerInputs(idleContainer);
    const mountedKey = `${strategyId}|${tf}`;
    if (activeContainer.dataset.mountedKey === mountedKey) {
      for (const spec of specs) {
        const input = activeContainer.querySelector(`input[data-axis="${spec.name}"]`);
        if (input) {
          input.min = spec.min_value;
          input.max = spec.max_value;
          input.step = spec.step;
          input.defaultValue = spec.default;
          if (input.labels?.[0]) input.labels[0].textContent = spec.label_ru;
        }
      }
      return;
    }
    detachContainerInputs(activeContainer);
    activeContainer.replaceChildren(
      ...specs.map(spec =>
        createParameterField(spec, soloInputId('pair_screen', strategyId, spec.name), null, input => {
          state.numericInputs.push(input);
          bindFocusInput(input);
        }),
      ),
    );
    activeContainer.dataset.mountedKey = mountedKey;
  } else {
    const container = $('calendar-params');
    const mountedKey = `${strategyId}|${tf}|${monthly ? 'm' : 'q'}`;
    if (container.dataset.mountedKey === mountedKey) {
      for (const spec of specs) {
        const input = container.querySelector(`input[data-axis="${spec.name}"]`);
        if (input) {
          input.min = spec.min_value;
          input.max = spec.max_value;
          input.step = spec.step;
          input.defaultValue = spec.default;
          if (input.labels?.[0]) input.labels[0].textContent = spec.label_ru;
        }
      }
      return;
    }
    detachContainerInputs(container);
    container.replaceChildren(
      ...specs.map(spec =>
        createParameterField(spec, soloInputId('calendar_native', strategyId, spec.name), null, input => {
          state.numericInputs.push(input);
          bindFocusInput(input);
        }),
      ),
    );
    container.dataset.mountedKey = mountedKey;
  }
}

export const activeInputs = () => state.numericInputs.filter(n => n.isConnected && !n.closest('.hidden'));
export const sharedInputs = () => [...$('cohort-editor').querySelectorAll('.number-shell input')];
const checkedTargets = () => [...state.checked].sort().map(k => state.leaves.get(k).target);

export function activeStrategyName() {
  return state.universe === 'pair_screen' ? $('strategy').value : $('cstrategy').value;
}

export function mappedStrategy(name) {
  if (name === 'mean_reversion' || name === 'zscore_mean_reversion') {
    return state.universe === 'pair_screen' ? 'mean_reversion' : 'zscore_mean_reversion';
  }
  return name;
}

export const formKey = (key, name) => key + '|' + name;

export function rememberForm() {
  if (state.leafId && !invalidForm()) state.formStates.set(formKey(state.leafId, activeStrategyName()), currentRequest());
}

export function applyToForm(selection) {
  const settings = { ...selection.parameters };
  for (const n of activeInputs()) {
    if (Object.hasOwn(settings, n.dataset.axis)) n.value = settings[n.dataset.axis];
  }
}

export function restoreForm() {
  const saved = state.formStates.get(formKey(state.leafId, activeStrategyName()));
  if (saved) {
    applyToForm(saved);
    return;
  }
  for (const n of activeInputs()) n.value = n.defaultValue;
}

export function syncPairHint() {
  const p = pairAvailability(currentPair());
  const selected = $('strategy').value;
  const plugin = pairPlugin(selected);
  const reasonKey = plugin ? (plugin.adapter === 'carry_settlement_intents.v1' ? 'funding_only' : 'mean_reversion') : selected;
  $('pair-hint').textContent = p
    ? `${p.data_reasons?.[reasonKey] || CATSTATUS_RU[p.status] || p.status.replaceAll('_', ' ')} · маржа ≈ ${p.max_pair_margin_rub != null ? fmt((p.max_pair_margin_rub / state.preferences.initial_capital_rub) * 100, 2) + '% капитала' : '—'}${p.expiry ? ' · экспирация дальней ноги ' + p.expiry : ''}`
    : '';
}

export function fillStrategy(preferred) {
  const p = pairAvailability(currentPair());
  const s = $('strategy');
  const prev = preferred || s.value;
  s.replaceChildren();
  const meta = state.catalog.pair_screen.strategies;
  for (const [name, m] of Object.entries(meta)) {
    const label = getStrategyMeta(name)?.label || m.label;
    const ok = pairStrategyAvailable(p, name);
    const o = el('option', '', `${label}${ok ? '' : ' · ' + pairUnavailable(p, name)}`);
    o.value = name;
    o.disabled = !pairStrategyAvailable(p, name, true) && pairHasLocalData(p);
    s.append(o);
  }
  const seenIds = new Set(Object.keys(meta));
  const pluginEntries = [...(state.catalog.pair_screen.proof_plugins || [])];
  for (const regMeta of state.strategies.values()) {
    if (!regMeta.is_builtin && regMeta.proof_enabled && regMeta.supported_universes?.includes('pair_screen') && !pluginEntries.some(x => x.id === regMeta.id)) {
      pluginEntries.push({
        id: regMeta.id,
        label: regMeta.label,
        adapter: regMeta.proof_adapter,
        timeframe_supported: state.preferences.timeframe === '1d' || !regMeta.requires_daily_bars,
      });
    }
  }
  for (const m of pluginEntries) {
    if (seenIds.has(m.id)) continue;
    seenIds.add(m.id);
    const ok = pairStrategyAvailable(p, m.id);
    const baseKey = m.adapter === 'carry_settlement_intents.v1' ? 'funding_only' : 'mean_reversion';
    const o = el('option', '', `${m.label}${ok ? '' : m.timeframe_supported ? ' · ' + pairUnavailable(p, baseKey) : ' · интервал не поддерживается'}`);
    o.value = m.id;
    o.disabled = !m.timeframe_supported || (!pairStrategyAvailable(p, m.id, true) && pairHasLocalData(p));
    s.append(o);
  }
  s.value =
    [...s.options].find(o => !o.disabled && o.value === prev)?.value ||
    [...s.options].find(o => !o.disabled)?.value ||
    s.options[0]?.value ||
    '';
  mountSoloControls('pair_screen', s.value);
  syncPairHint();
}

export function syncCalendarParams() {
  mountSoloControls('calendar_native', $('cstrategy').value);
}

export function fillCalendarStrategies(preferred) {
  const a = currentAsset();
  const s = $('cstrategy');
  const prev = preferred || s.value;
  s.replaceChildren();
  const names = [...(state.catalog.strategies || [])];
  for (const extra of calendarProofPlugins()) {
    if (!names.includes(extra.id)) names.push(extra.id);
  }
  for (const name of names) {
    if (!calendarStrategySupported(a, name)) continue;
    const o = el('option', '', strategyLabel(name));
    o.value = name;
    s.append(o);
  }
  if ([...s.options].some(o => o.value === prev)) s.value = prev;
  syncCalendarParams();
}

export function syncGroupStrategyOptions() {
  const gs = $('group-strategy');
  const current = gs.value;
  gs.replaceChildren();
  const mrOpt = el('option', '', 'Возврат к среднему');
  mrOpt.value = 'mr';
  gs.append(mrOpt);
  const added = new Set(['mean_reversion', 'zscore_mean_reversion']);
  for (const meta of state.strategies.values()) {
    if (added.has(meta.id) || !meta.proof_enabled) continue;
    added.add(meta.id);
    const o = el('option', '', GROUP_SHORT_LABELS[meta.id] || meta.label);
    o.value = meta.id;
    if (!meta.is_builtin) o.dataset.plugin = 'true';
    o.disabled = !(state.preferences.timeframe === '1d' || !meta.requires_daily_bars);
    gs.append(o);
  }
  for (const p of state.catalog?.pair_screen?.proof_plugins || []) {
    if (added.has(p.id)) continue;
    added.add(p.id);
    const o = el('option', '', p.label);
    o.value = p.id;
    o.dataset.plugin = 'true';
    o.disabled = !p.timeframe_supported;
    gs.append(o);
  }
  if ([...gs.options].some(o => o.value === current && !o.disabled)) gs.value = current;
}

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
  setSearchProgress(false);
  tuneButton();
}

export function clearGroup() {
  $('batch-result').hidden = true;
  $('batch-result').replaceChildren();
}

export function paintPlateaus(plateau = null, empty = 'Нет оценки для текущего значения.') {
  if (!state.checked.size) drawLamps(activeInputs(), plateau, empty, null);
}

function bindFocusInput(input) {
  bindLamp(input);
  input.addEventListener('input', () => {
    rememberForm();
    clearGroup();
    dirtyInputs.add(input);
    callbacks.invalidateProof();
    paintPlateaus(null, 'Значение изменено. Нажмите Enter или покиньте поле для пересчёта.');
  });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      dirtyInputs.delete(input);
      callbacks.scheduleShow(0);
    }
  });
  input.addEventListener('blur', () => {
    if (dirtyInputs.delete(input)) callbacks.scheduleShow(140);
  });
}

export function renderSharedEditor(req) {
  hidePlateauTip();
  state.sharedStrategy = req.strategy;
  if (isMR(req.strategy)) state.sharedMR = req.strategy;
  syncGroupStrategyOptions();
  $('group-strategy').value = isMR(req.strategy) ? 'mr' : req.strategy;
  const meta = getStrategyMeta(req.strategy);
  const u = meta?.supported_universes?.[0] || state.universe;
  const monthly = u === 'calendar_native' ? !!currentAsset()?.monthly : false;
  const specs = orderedStrategySpecs(req.strategy, { universe: u, timeframe: state.preferences.timeframe, monthly });
  const point = { ...req.parameters };
  $('group-params').replaceChildren(
    ...specs.map(spec =>
      createParameterField(spec, 'group-' + spec.name, point, input => {
        bindLamp(input);
        input.addEventListener('input', () => sharedEdited(220));
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); sharedEdited(0); }
        });
      }),
    ),
  );
}

export function changeSharedStrategy() {
  const old = sharedRequest();
  if (!pointError(sharedInputs())) state.sharedDrafts.set(state.sharedStrategy, old);
  const chosen = $('group-strategy').value;
  const name = chosen === 'mr' ? state.sharedMR : chosen;
  const next = state.sharedDrafts.get(name) || { strategy: name, parameters: {} };
  renderSharedEditor(next);
  sharedEdited(0);
}

export function groupSpec() {
  const body = { ...sharedRequest(), targets: checkedTargets() };
  return { body, key: JSON.stringify([settingsKey(), body]) };
}

export function showGroupReading(plateau) {
  showGroupLampsReading(sharedInputs(), sharedRequest().fill === 'reference_same_bar', plateau);
}

export function refreshGroupProfile(delay = 140) {
  if (!state.sharedStrategy || !state.checked.size) return;
  const { body, key } = groupSpec();
  runGroupProfileStream({
    body,
    key,
    error: pointError(sharedInputs()),
    getInputs: sharedInputs,
    isReference: () => sharedRequest().fill === 'reference_same_bar',
    onStaleSettings: () => callbacks.refreshGlobalSettings(true),
    delay,
  });
}

export function syncEditorScope() {
  const on = state.checked.size > 0;
  $('focus-editor').hidden = on;
  $('cohort-editor').hidden = !on;
  if (on) {
    if (!state.sharedStrategy) {
      const req = currentRequest();
      state.sharedMR = isMR(req.strategy) ? req.strategy : state.universe === 'calendar_native' ? 'zscore_mean_reversion' : 'mean_reversion';
      renderSharedEditor(req);
    }
  } else {
    stopProfile();
    state.sharedStrategy = null;
    state.sharedDrafts.clear();
    hidePlateauTip();
  }
}

export function syncChartScope(delay = 0) {
  const plan = chartPlan();
  if (state.chartKey !== plan.key || (!state.checked.size && !state.chartWithLamps)) callbacks.scheduleShow(delay);
  else if (!state.checked.size && state.payload?.plateau) paintPlateaus(state.payload.plateau);
  callbacks.refreshWindowReadings();
}

export function sharedEdited(delay) {
  cancelSearch();
  clearGroup();
  tuneState('');
  refreshGroupProfile(delay);
  syncChartScope(delay);
}

export function bindStrategyGlobalListeners() {
  document.addEventListener('keydown', e => { if (e.key === 'Escape') hidePlateauTip(); });
  window.addEventListener('resize', hidePlateauTip);
  $('col-norm').addEventListener('scroll', hidePlateauTip);
  $('compatibility-no').addEventListener('click', () => finishReview(false));
  $('compatibility-yes').addEventListener('click', () => finishReview(true));
  $('compatibility').addEventListener('cancel', e => { e.preventDefault(); finishReview(false); });
  bindBackdropClose($('compatibility'), () => finishReview(false));
  $('group-strategy').addEventListener('change', changeSharedStrategy);
  $('strategy').addEventListener('change', () => {
    fillStrategy();
    restoreForm();
    clearGroup();
    callbacks.scheduleShow(100);
  });
  $('cstrategy').addEventListener('change', () => {
    syncCalendarParams();
    restoreForm();
    clearGroup();
    callbacks.scheduleShow(100);
  });
}
