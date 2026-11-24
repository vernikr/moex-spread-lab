import { api } from './api.js';
import { $, FILL_RU, bindBackdropClose, fmt, pct, priceFillOptions, status } from './dom.js';
import { buildPicker, openBranch, stopWindowReadings, windowGates } from './picker.js';
import {
  installStrategyRegistry,
  settingsEventKey,
  settingsKey,
  state,
  supportsCalendar,
  targetKey,
  timeframeLabel,
} from './state.js';
import {
  activeStrategyName,
  applyToForm,
  cancelSearch,
  clearGroup,
  currentRequest,
  fillStrategy,
  refreshGroupProfile,
  restoreForm,
  stopProfile,
  syncCalendarParams,
} from './strategy-form.js';

let modalCallbacks = {
  scheduleShow: () => {},
  show: async () => {},
  invalidateProof: () => {},
};

export function configureSettingsCallbacks(next) {
  modalCallbacks = { ...modalCallbacks, ...next };
}

export function setSegmentedTimeframe(id) {
  const valid = state.timeframeOptions.some(item => item.id === id) ? id : state.timeframeOptions[0]?.id || '1d';
  state.selectedTimeframe = valid;
  const group = $('global-timeframe');
  if (!group) return;
  group.dataset.value = valid;
  for (const btn of group.querySelectorAll('[role=radio]')) {
    const active = btn.dataset.value === valid;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-checked', String(active));
  }
}

export function installTimeframeOptions(items) {
  if (!Array.isArray(items) || !items.length) return;
  state.timeframeOptions = items;
  const group = $('global-timeframe');
  group.replaceChildren(
    ...items.map((item, idx) => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'seg-option';
      btn.setAttribute('role', 'radio');
      btn.dataset.value = item.id;
      btn.textContent = item.label;
      btn.addEventListener('click', () => setSegmentedTimeframe(item.id));
      btn.addEventListener('keydown', e => {
        const btns = [...group.querySelectorAll('[role=radio]')];
        if (!btns.length) return;
        let nextIdx = null;
        if (e.key === 'ArrowRight' || e.key === 'ArrowDown') nextIdx = (idx + 1) % btns.length;
        else if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') nextIdx = (idx - 1 + btns.length) % btns.length;
        if (nextIdx != null) {
          e.preventDefault();
          const nextBtn = btns[nextIdx];
          setSegmentedTimeframe(nextBtn.dataset.value);
          nextBtn.focus();
        }
      });
      return btn;
    }),
  );
  setSegmentedTimeframe(state.preferences.timeframe);
}

export function setPreferences(x) {
  const changed = settingsKey() !== settingsKey(x);
  state.preferences = {
    validation_percent: x.validation_percent,
    allow_failed_volume: x.allow_failed_volume,
    allow_failed_capital: x.allow_failed_capital,
    slippage_ticks: x.slippage_ticks,
    initial_capital_rub: x.initial_capital_rub,
    timeframe: x.timeframe || '1d',
    price_fill: x.price_fill || 'reference_same_bar',
    random_seed: Number(x.random_seed) || 0,
  };
  setSegmentedTimeframe(state.preferences.timeframe);
  const fillSel = $('global-fill');
  if (fillSel && [...fillSel.options].some(o => o.value === state.preferences.price_fill)) {
    fillSel.value = state.preferences.price_fill;
  }
  $('settings-badge').textContent = `${timeframeLabel(state.preferences.timeframe)} · ${FILL_RU[state.preferences.price_fill] || state.preferences.price_fill} · Капитал: ${fmt(x.initial_capital_rub, 2)} ₽ · Проверка: ${pct(x.validation_percent)} · Проскальзывание: ${fmt(x.slippage_ticks, 1)} т. · Сид: ${state.preferences.random_seed === 0 ? 'случайный' : state.preferences.random_seed}`;
  if (changed) {
    state.windowReadings.clear();
    stopWindowReadings();
    cancelSearch();
    clearGroup();
    if (state.catalog && state.sharedStrategy) refreshGroupProfile(0);
    if (!supportsCalendar(state.preferences.timeframe) && state.universe === 'calendar_native' && state.catalog) {
      const candidate =
        (state.catalog.pair_screen?.pairs || []).find(row => Object.values(row.proof_strategies || {}).some(Boolean)) ||
        state.catalog.pair_screen?.pairs?.[0];
      const branch = candidate && state.branches.get('pair_screen|' + candidate.near);
      if (branch) openBranch(branch.key, candidate.id, '', false);
    }
    for (const row of $('window-list').querySelectorAll('tr[data-leaf]')) {
      const l = state.leaves.get(row.dataset.leaf);
      if (l) windowGates(row, l);
    }
    if (state.catalog && state.leafId) {
      if (state.universe === 'pair_screen') {
        const values = currentRequest();
        fillStrategy();
        if (activeStrategyName() === values.strategy) applyToForm(values);
        else restoreForm();
      } else {
        const values = currentRequest();
        syncCalendarParams();
        if (activeStrategyName() === values.strategy) applyToForm(values);
      }
    }
  }
  return changed;
}

export function previewSplit() {
  const n = $('validation-percent');
  const v = n.valueAsNumber;
  $('split-preview').textContent = !n.validity.valid
    ? 'Введите число от 0 до 50%.'
    : v === 0
      ? 'Обучение — 100%. Проверка отключена. Результат считается по всей истории обучения.'
      : `Обучение — ${pct(100 - v)}, проверка — ${pct(v)}. Результат считается только по проверке.`;
  if (n.validity.valid) $('validation-range').value = v;
}

export async function refreshCatalog() {
  const oldTarget = state.leaves.get(state.leafId)?.target;
  const oldBranch = state.branchId;
  const oldStrategy = activeStrategyName();
  const [nextCatalog, nextStrategies] = await Promise.all([
    api('/api/proof/catalog'),
    api('/api/strategies').catch(() => null),
  ]);
  if (nextStrategies) installStrategyRegistry(nextStrategies);
  state.catalog = nextCatalog;
  buildPicker();
  let target = oldTarget;
  if (!supportsCalendar(state.preferences.timeframe) && target?.universe === 'calendar_native') {
    const candidate =
      (state.catalog.pair_screen?.pairs || []).find(row => Object.values(row.proof_strategies || {}).some(Boolean)) ||
      state.catalog.pair_screen?.pairs?.[0];
    target = candidate ? { universe: 'pair_screen', pair: candidate.id } : null;
  }
  const leaf = target && state.leaves.get(targetKey(target));
  let key = leaf?.branch || oldBranch;
  if (!state.branches.has(key)) {
    key = [...state.branches.values()].find(b => b.children.some(item => item.enabled))?.key || [...state.branches.keys()][0];
  }
  if (key) openBranch(key, target?.pair || state.leafId?.split('|').pop() || '', oldStrategy, false);
  return state.catalog;
}

export async function refreshGlobalSettings(force = false) {
  if (state.savingSettings) return;
  const rev = ++state.settingRevision;
  try {
    const x = await api('/api/settings');
    if (rev !== state.settingRevision || state.savingSettings) return;
    const changed = setPreferences(x);
    if (force && !changed && state.sharedStrategy) {
      stopProfile();
      refreshGroupProfile(0);
    }
    if (state.catalog && (changed || force)) {
      await refreshCatalog();
      modalCallbacks.scheduleShow(0);
    }
  } catch (e) {
    status(e.message);
  }
}

export function bindSettings() {
  const dlg = $('global-settings');
  const n = $('validation-percent');
  const slip = $('global-slippage');
  const capital = $('global-capital');
  const fill = $('global-fill');
  const seed = $('global-seed');
  const submit = $('apply-settings');
  priceFillOptions(fill);
  fill.value = state.preferences.price_fill;
  $('open-settings').addEventListener('click', () => {
    n.value = state.preferences.validation_percent;
    slip.value = state.preferences.slippage_ticks;
    capital.value = state.preferences.initial_capital_rub;
    seed.value = state.preferences.random_seed;
    setSegmentedTimeframe(state.preferences.timeframe);
    fill.value = state.preferences.price_fill;
    $('allow-failed-volume').checked = state.preferences.allow_failed_volume;
    $('allow-failed-capital').checked = state.preferences.allow_failed_capital;
    $('settings-error').textContent = '';
    previewSplit();
    dlg.showModal();
  });
  $('cancel-settings').addEventListener('click', () => dlg.close());
  dlg.addEventListener('cancel', e => {
    if (state.savingSettings) e.preventDefault();
  });
  bindBackdropClose(dlg, () => dlg.close(), () => state.savingSettings);
  $('validation-range').addEventListener('input', e => {
    n.value = Number(e.target.valueAsNumber.toFixed(1));
    previewSplit();
  });
  n.addEventListener('input', previewSplit);
  $('settings-form').addEventListener('submit', async e => {
    e.preventDefault();
    if (
      state.savingSettings ||
      !n.reportValidity() ||
      !slip.reportValidity() ||
      !capital.reportValidity() ||
      !fill.reportValidity() ||
      !seed.reportValidity()
    ) {
      return;
    }
    ++state.settingRevision;
    state.savingSettings = true;
    submit.disabled = $('cancel-settings').disabled = true;
    $('settings-error').textContent = '';
    cancelSearch();
    modalCallbacks.invalidateProof();
    let saved = false;
    try {
      const x = await api('/api/settings', {
        validation_percent: n.valueAsNumber,
        allow_failed_volume: $('allow-failed-volume').checked,
        allow_failed_capital: $('allow-failed-capital').checked,
        slippage_ticks: slip.valueAsNumber,
        initial_capital_rub: capital.valueAsNumber,
        timeframe: state.selectedTimeframe,
        price_fill: fill.value,
        random_seed: seed.valueAsNumber,
      });
      setPreferences(x);
      dlg.close();
      saved = true;
      try {
        localStorage.setItem(settingsEventKey, JSON.stringify([Date.now(), settingsKey()]));
      } catch (_) {}
    } catch (err) {
      $('settings-error').textContent = err.message;
    } finally {
      submit.disabled = $('cancel-settings').disabled = false;
      state.savingSettings = false;
    }
    if (saved && state.catalog) {
      try {
        await refreshCatalog();
        void modalCallbacks.show();
      } catch (err) {
        console.error(err);
      }
    }
  });
  window.addEventListener('storage', e => {
    if (e.key === settingsEventKey) void refreshGlobalSettings();
  });
  document.addEventListener('visibilitychange', () => {
    if (state.catalog && !document.hidden) void refreshGlobalSettings();
  });
}
