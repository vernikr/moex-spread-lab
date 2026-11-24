import { $, CATSTATUS_RU, el, fmt } from './dom.js';
import { sharedInputs } from './form-inputs.js';
import { bindLamp, hidePlateauTip, runGroupProfileStream, showGroupLampsReading, stopProfile } from './plateau-lamps.js';
import {
  calendarStrategySupported, currentAsset, currentPair, familyMembers, familyVariant,
  getStrategyMeta, hasFamilyVariants, orderedStrategySpecs, pairAvailability, pairCatalogSlot,
  pairHasLocalData, pairStrategyAvailable, pairUnavailable, settingsKey, state,
  strategyFamily, strategyLabel, strategyOptions, timeframeSupported,
} from './state.js';
import { currentRequest, pointError, sharedRequest } from './strategy-plans.js';

let callbacks = {
  bindFocusInput: () => {},
  sharedEdited: () => {},
  refreshGlobalSettings: async () => {},
};

export function configureStrategyControlsCallbacks(next) {
  callbacks = { ...callbacks, ...next };
}

// Built-in parameters keep their legacy DOM ids (short axis names, `c`-prefixed
// in the calendar universe); drop-in strategies use the `plugin-` scheme.
const BUILTIN_AXIS_IDS = { entry_z: 'entry', exit_z: 'exit', lookback: 'lookback', window: 'lookback', threshold_pp: 'threshold' };

function paramInputId(universe, meta, spec) {
  if (!meta.is_builtin) return (universe === 'calendar_native' ? 'cplugin-' : 'plugin-') + spec.name;
  return (universe === 'calendar_native' ? 'c' : '') + (BUILTIN_AXIS_IDS[spec.name] || spec.name);
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

function refreshMountedSpecs(container, specs) {
  for (const spec of specs) {
    const input = container.querySelector(`input[data-axis="${spec.name}"]`);
    if (!input) continue;
    input.min = spec.min_value;
    input.max = spec.max_value;
    input.step = spec.step;
    input.defaultValue = spec.default;
    if (input.labels?.[0]) input.labels[0].textContent = spec.label_ru;
  }
}

function mountSoloControls(universe, strategyId) {
  if (!strategyId) return;
  const meta = getStrategyMeta(strategyId);
  if (!meta) return;
  const monthly = universe === 'calendar_native' ? !!currentAsset()?.monthly : false;
  const tf = state.preferences.timeframe || '1d';
  const specs = orderedStrategySpecs(strategyId, { universe, timeframe: tf, monthly });
  const builtin = meta.is_builtin === true;
  const container = universe === 'pair_screen' && !builtin ? $('pair-plugin-params') : universe === 'pair_screen' ? $('pair-params') : $('calendar-params');
  const idle = universe === 'pair_screen' ? (builtin ? $('pair-plugin-params') : $('pair-params')) : null;
  detachContainerInputs(idle);
  const mountedKey = `${strategyId}|${tf}|${monthly ? 'm' : 'q'}`;
  if (container.dataset.mountedKey === mountedKey) {
    refreshMountedSpecs(container, specs);
    return;
  }
  detachContainerInputs(container);
  container.replaceChildren(
    ...specs.map(spec => createParameterField(spec, paramInputId(universe, meta, spec), null, input => {
      state.numericInputs.push(input);
      callbacks.bindFocusInput(input);
    })),
  );
  container.dataset.mountedKey = mountedKey;
}

const checkedTargets = () => [...state.checked].sort().map(k => state.leaves.get(k).target);

export function syncPairHint() {
  const p = pairAvailability(currentPair());
  const selected = $('strategy').value;
  const reasonKey = pairCatalogSlot(selected);
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
    const o = el('option', '', `${meta.label}${ok ? '' : ' · ' + (tfOk ? pairUnavailable(p, meta.id) : 'интервал не поддерживается')}`);
    o.value = meta.id;
    o.disabled = !tfOk || (!pairStrategyAvailable(p, meta.id, true) && pairHasLocalData(p));
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
  for (const meta of strategyOptions('calendar_native')) {
    if (!calendarStrategySupported(a, meta.id)) continue;
    const o = el('option', '', strategyLabel(meta.id));
    o.value = meta.id;
    s.append(o);
  }
  if ([...s.options].some(o => o.value === prev)) s.value = prev;
  syncCalendarParams();
}

/** Legacy cohort value for the family that spans both universes. */
const COHORT_FAMILY_VALUE = 'mr';

const cohortValue = meta => (hasFamilyVariants(meta.id) ? COHORT_FAMILY_VALUE : meta.id);

function groupLabel(meta) {
  const label = meta.short_label || meta.label || meta.id;
  return label.charAt(0).toUpperCase() + label.slice(1);
}

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
    const o = el('option', '', groupLabel(variants.length > 1 ? variants[0] : meta));
    o.value = value;
    if (variants.length <= 1) {
      if (!meta.is_builtin) o.dataset.plugin = 'true';
      o.disabled = !timeframeSupported(meta);
    }
    gs.append(o);
  }
  if ([...gs.options].some(o => o.value === current && !o.disabled)) gs.value = current;
}

function resolveGroupStrategy(chosen) {
  const family = [...state.strategies.values()].find(
    meta => cohortValue(meta) === chosen && hasFamilyVariants(meta.id),
  );
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
  const monthly = u === 'calendar_native' ? !!currentAsset()?.monthly : false;
  const specs = orderedStrategySpecs(req.strategy, { universe: u, timeframe: state.preferences.timeframe, monthly });
  const point = { ...req.parameters };
  $('group-params').replaceChildren(
    ...specs.map(spec =>
      createParameterField(spec, 'group-' + spec.name, point, input => {
        bindLamp(input);
        input.addEventListener('input', () => callbacks.sharedEdited(220));
        input.addEventListener('keydown', e => {
          if (e.key === 'Enter') { e.preventDefault(); callbacks.sharedEdited(0); }
        });
      }),
    ),
  );
}

export function changeSharedStrategy() {
  const old = sharedRequest();
  if (!pointError(sharedInputs())) state.sharedDrafts.set(state.sharedStrategy, old);
  const name = resolveGroupStrategy($('group-strategy').value);
  const next = state.sharedDrafts.get(name) || { strategy: name, parameters: {} };
  renderSharedEditor(next);
  callbacks.sharedEdited(0);
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
      for (const meta of state.strategies.values()) {
        if (familyMembers(meta.family_id).length > 1 && !state.sharedVariants.has(meta.family_id)) {
          const variant = familyVariant(meta.family_id, state.universe);
          if (variant) state.sharedVariants.set(meta.family_id, variant.id);
        }
      }
      renderSharedEditor(currentRequest());
    }
  } else {
    stopProfile();
    state.sharedStrategy = null;
    state.sharedDrafts.clear();
    hidePlateauTip();
  }
}
