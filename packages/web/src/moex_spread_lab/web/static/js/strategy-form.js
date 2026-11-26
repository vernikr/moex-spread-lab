import { $, bindBackdropClose, el, emit, fmt } from './dom.js';
import { bindLamp, hidePlateauTip, paintPlateaus, refreshGroupProfile, stopProfile } from './plateau-lamps.js';
import {
  calendarStrategySupported,
  currentAsset,
  currentPair,
  familyMembers,
  familyVariant,
  getStrategyMeta,
  hasFamilyVariants,
  orderedStrategySpecs,
  pairAvailability,
  pairCatalogSlot,
  pairHasLocalData,
  pairStrategyAvailable,
  pairUnavailable,
  state,
  strategyFamily,
  strategyLabel,
  strategyOptions,
  timeframeSupported,
} from './state.js';
import {
  activeInputs,
  activeStrategyName,
  cancelSearch,
  chartPlan,
  clearGroup,
  currentRequest,
  finishReview,
  formKey,
  invalidForm,
  pointError,
  sharedInputs,
  sharedRequest,
  tuneState,
} from './strategy-plans.js';
import { refreshWindowReadings } from './window-table.js';

const BUILTIN_AXIS_IDS = { entry_z: 'entry', exit_z: 'exit', lookback: 'lookback', window: 'lookback', threshold_pp: 'threshold' };
const CATSTATUS_RU = {
  local_inputs_available: 'локальные входы доступны',
  intake_required: 'нужна загрузка данных',
  excluded_thin_leg: 'объём ниже порога',
  proof_ready_thin_leg: 'данные есть, но нога тонкая',
};
const COHORT_FAMILY_VALUE = 'mr';
const dirtyInputs = new WeakSet();

function paramInputId(universe, meta, spec) {
  if (!meta.is_builtin) return (universe === 'calendar_native' ? 'cplugin-' : 'plugin-') + spec.name;
  return (universe === 'calendar_native' ? 'c' : '') + (BUILTIN_AXIS_IDS[spec.name] || spec.name);
}

function createParameterField(spec, inputId, point, onBind) {
  const box = el('div', 'control');
  const label = el('label', '', spec.label_ru);
  const shell = el('span', 'number-shell');
  const input = Object.assign(document.createElement('input'), {
    id: inputId,
    type: 'number',
    min: spec.min_value,
    max: spec.max_value,
    step: spec.step,
    defaultValue: spec.default,
    value: point?.[spec.name] ?? spec.default,
  });
  input.dataset.axis = spec.name;
  label.htmlFor = inputId;
  const lamp = el('button', 'plateau-dot');
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
  const meta = strategyId && getStrategyMeta(strategyId);
  if (!meta) return;
  const monthly = universe === 'calendar_native' && !!currentAsset()?.monthly;
  const tf = state.preferences.timeframe || '1d';
  const specs = orderedStrategySpecs(strategyId, { universe, timeframe: tf, monthly });
  const builtin = meta.is_builtin === true;
  const container = universe !== 'pair_screen' ? $('calendar-params') : builtin ? $('pair-params') : $('pair-plugin-params');
  if (universe === 'pair_screen') detachContainerInputs(builtin ? $('pair-plugin-params') : $('pair-params'));
  const mountedKey = `${strategyId}|${tf}|${monthly ? 'm' : 'q'}`;
  if (container.dataset.mountedKey === mountedKey) {
    for (const spec of specs) {
      const input = container.querySelector(`input[data-axis="${spec.name}"]`);
      if (!input) continue;
      Object.assign(input, { min: spec.min_value, max: spec.max_value, step: spec.step, defaultValue: spec.default });
      if (input.labels?.[0]) input.labels[0].textContent = spec.label_ru;
    }
    return;
  }
  detachContainerInputs(container);
  container.replaceChildren(
    ...specs.map(spec =>
      createParameterField(spec, paramInputId(universe, meta, spec), null, input => {
        state.numericInputs.push(input);
        bindFocusInput(input);
      }),
    ),
  );
  container.dataset.mountedKey = mountedKey;
}

function syncPairHint() {
  const p = pairAvailability(currentPair());
  const reasonKey = pairCatalogSlot($('strategy').value);
  $('pair-hint').textContent = p
    ? `${p.data_reasons?.[reasonKey] || CATSTATUS_RU[p.status] || p.status.replaceAll('_', ' ')} · маржа ≈ ${p.max_pair_margin_rub != null ? fmt((p.max_pair_margin_rub / state.preferences.initial_capital_rub) * 100, 2) + '% капитала' : '—'}${p.expiry ? ' · экспирация дальней ноги ' + p.expiry : ''}`
    : '';
}

export function fillStrategy(preferred) {
  const p = pairAvailability(currentPair());
  const s = $('strategy');
  const prev = preferred || s.value;
  s.replaceChildren();
  for (const meta of strategyOptions('pair_screen')) {
    const tfOk = timeframeSupported(meta);
    const ok = pairStrategyAvailable(p, meta.id);
    const o = Object.assign(
      el('option', '', `${meta.label}${ok ? '' : ' · ' + (tfOk ? pairUnavailable(p, meta.id) : 'интервал не поддерживается')}`),
      { value: meta.id, disabled: !tfOk || (!pairStrategyAvailable(p, meta.id, true) && pairHasLocalData(p)) },
    );
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
  s.replaceChildren(
    ...strategyOptions('calendar_native')
      .filter(meta => calendarStrategySupported(a, meta.id))
      .map(meta => Object.assign(el('option', '', strategyLabel(meta.id)), { value: meta.id })),
  );
  if ([...s.options].some(o => o.value === prev)) s.value = prev;
  syncCalendarParams();
}

const cohortValue = meta => (hasFamilyVariants(meta.id) ? COHORT_FAMILY_VALUE : meta.id);
const groupLabel = meta => {
  const label = meta.short_label || meta.label || meta.id;
  return label.charAt(0).toUpperCase() + label.slice(1);
};

export function syncGroupStrategyOptions() {
  const gs = $('group-strategy');
  const current = gs.value;
  gs.replaceChildren();
  const seen = new Set();
  for (const meta of state.strategies.values()) {
    if (!meta.proof_enabled) continue;
    const value = cohortValue(meta);
    if (seen.has(value)) continue;
    seen.add(value);
    const variants = familyMembers(meta.family_id);
    const o = Object.assign(el('option', '', groupLabel(variants.length > 1 ? variants[0] : meta)), { value });
    if (variants.length <= 1) {
      if (!meta.is_builtin) o.dataset.plugin = 'true';
      o.disabled = !timeframeSupported(meta);
    }
    gs.append(o);
  }
  if ([...gs.options].some(o => o.value === current && !o.disabled)) gs.value = current;
}

function resolveGroupStrategy(chosen) {
  const family = [...state.strategies.values()].find(meta => cohortValue(meta) === chosen && hasFamilyVariants(meta.id));
  if (!family) return chosen;
  return state.sharedVariants.get(family.family_id) || familyVariant(family.family_id, state.universe)?.id || family.family_id;
}

export function renderSharedEditor(req) {
  hidePlateauTip();
  state.sharedStrategy = req.strategy;
  if (hasFamilyVariants(req.strategy)) state.sharedVariants.set(strategyFamily(req.strategy), req.strategy);
  syncGroupStrategyOptions();
  const meta = getStrategyMeta(req.strategy);
  $('group-strategy').value = meta ? cohortValue(meta) : req.strategy;
  const u = meta?.supported_universes?.[0] || state.universe;
  const specs = orderedStrategySpecs(req.strategy, {
    universe: u,
    timeframe: state.preferences.timeframe,
    monthly: u === 'calendar_native' && !!currentAsset()?.monthly,
  });
  $('group-params').replaceChildren(
    ...specs.map(spec =>
      createParameterField(spec, 'group-' + spec.name, req.parameters, input => {
        bindLamp(input);
        input.addEventListener('input', () => sharedEdited(220));
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') {
            e.preventDefault();
            sharedEdited(0);
          }
        });
      }),
    ),
  );
}

function changeSharedStrategy() {
  const old = sharedRequest();
  if (!pointError(sharedInputs())) state.sharedDrafts.set(state.sharedStrategy, old);
  const name = resolveGroupStrategy($('group-strategy').value);
  renderSharedEditor(state.sharedDrafts.get(name) || { strategy: name, parameters: {} });
  sharedEdited(0);
}

export function syncEditorScope() {
  const on = state.checked.size > 0;
  $('focus-editor').hidden = on;
  $('cohort-editor').hidden = !on;
  if (!on) {
    stopProfile();
    state.sharedStrategy = null;
    state.sharedDrafts.clear();
    hidePlateauTip();
    return;
  }
  if (!state.sharedStrategy) {
    for (const meta of state.strategies.values()) {
      if (familyMembers(meta.family_id).length > 1 && !state.sharedVariants.has(meta.family_id)) {
        const variant = familyVariant(meta.family_id, state.universe);
        if (variant) state.sharedVariants.set(meta.family_id, variant.id);
      }
    }
    renderSharedEditor(currentRequest());
  }
}

function rememberForm() {
  if (state.leafId && !invalidForm()) state.formStates.set(formKey(state.leafId, activeStrategyName()), currentRequest());
}

export function applyToForm(selection) {
  for (const n of activeInputs()) {
    if (Object.hasOwn(selection.parameters, n.dataset.axis)) n.value = selection.parameters[n.dataset.axis];
  }
}

export function restoreForm() {
  const saved = state.formStates.get(formKey(state.leafId, activeStrategyName()));
  if (saved) applyToForm(saved);
  else for (const n of activeInputs()) n.value = n.defaultValue;
}

function sharedEdited(delay) {
  cancelSearch();
  clearGroup();
  tuneState('');
  refreshGroupProfile(delay);
  syncChartScope(delay);
}

function bindFocusInput(input) {
  bindLamp(input);
  input.addEventListener('input', () => {
    rememberForm();
    clearGroup();
    dirtyInputs.add(input);
    emit('moex:invalidate');
    paintPlateaus(null, 'Значение изменено. Нажмите Enter или покиньте поле для пересчёта.');
  });
  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') {
      e.preventDefault();
      dirtyInputs.delete(input);
      emit('moex:schedule', 0);
    }
  });
  input.addEventListener('blur', () => {
    if (dirtyInputs.delete(input)) emit('moex:schedule', 140);
  });
}

export function syncChartScope(delay = 0) {
  const plan = chartPlan();
  if (state.chartKey !== plan.key || (!state.checked.size && !state.chartWithLamps)) emit('moex:schedule', delay);
  else if (!state.checked.size && state.payload?.plateau) paintPlateaus(state.payload.plateau);
  refreshWindowReadings();
}

export function bindStrategyGlobalListeners() {
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape') hidePlateauTip();
  });
  window.addEventListener('resize', hidePlateauTip);
  $('col-norm').addEventListener('scroll', hidePlateauTip);
  $('compatibility-no').addEventListener('click', () => finishReview(false));
  $('compatibility-yes').addEventListener('click', () => finishReview(true));
  $('compatibility').addEventListener('cancel', e => {
    e.preventDefault();
    finishReview(false);
  });
  bindBackdropClose($('compatibility'), () => finishReview(false));
  $('group-strategy').addEventListener('change', changeSharedStrategy);
  const onSoloChange = syncFn => () => {
    syncFn();
    restoreForm();
    clearGroup();
    emit('moex:schedule', 100);
  };
  $('strategy').addEventListener('change', onSoloChange(fillStrategy));
  $('cstrategy').addEventListener('change', onSoloChange(syncCalendarParams));
}
