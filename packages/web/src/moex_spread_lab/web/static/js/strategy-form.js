import { $, bindBackdropClose, setSearchProgress } from './dom.js';
import { configureStrategyControlsCallbacks, changeSharedStrategy, fillStrategy, refreshGroupProfile, syncCalendarParams } from './strategy-controls.js';
import { bindLamp, drawLamps, hidePlateauTip } from './plateau-lamps.js';
import { familyVariant, state, strategyFamily } from './state.js';
import {
  activeInputs,
  activeStrategyName,
  calendarRequest,
  chartPlan,
  commonPlan,
  currentRequest,
  defaultLeafRequest,
  finishReview,
  formKey,
  hasSelection,
  invalidForm,
  leafPlan,
  pairRequest,
  pointError,
  proofPlanKey,
  sharedRequest,
} from './strategy-plans.js';

export { hidePlateauTip };
export { stopProfile } from './plateau-lamps.js';
export { activeInputs, activeStrategyName, formKey, sharedInputs } from './strategy-plans.js';
export {
  changeSharedStrategy,
  createParameterField,
  fillCalendarStrategies,
  fillStrategy,
  groupSpec,
  refreshGroupProfile,
  renderSharedEditor,
  showGroupReading,
  syncCalendarParams,
  syncEditorScope,
  syncGroupStrategyOptions,
  syncPairHint,
} from './strategy-controls.js';
export {
  calendarRequest,
  chartPlan,
  commonPlan,
  currentRequest,
  defaultLeafRequest,
  finishReview,
  hasSelection,
  invalidForm,
  leafPlan,
  pairRequest,
  pointError,
  proofPlanKey,
  sharedRequest,
};

let callbacks = {
  scheduleShow: () => {},
  invalidateProof: () => {},
  refreshGlobalSettings: async () => {},
  refreshWindowReadings: () => {},
};

export function configureStrategyFormCallbacks(next) {
  callbacks = { ...callbacks, ...next };
  configureStrategyControlsCallbacks({
    bindFocusInput,
    sharedEdited,
    refreshGlobalSettings: callbacks.refreshGlobalSettings,
  });
}

const dirtyInputs = new WeakSet();

/** Resolve a strategy to its variant in the currently open universe. */
export function mappedStrategy(name) {
  return familyVariant(strategyFamily(name), state.universe)?.id || name;
}

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

export function sharedEdited(delay) {
  cancelSearch();
  clearGroup();
  tuneState('');
  refreshGroupProfile(delay);
  syncChartScope(delay);
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

export function syncChartScope(delay = 0) {
  const plan = chartPlan();
  if (state.chartKey !== plan.key || (!state.checked.size && !state.chartWithLamps)) callbacks.scheduleShow(delay);
  else if (!state.checked.size && state.payload?.plateau) paintPlateaus(state.payload.plateau);
  callbacks.refreshWindowReadings();
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
