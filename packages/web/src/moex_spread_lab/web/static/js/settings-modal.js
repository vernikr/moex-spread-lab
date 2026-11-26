import { $, FILL_RU, api, bindBackdropClose, el, emit, fmt, pct, status } from './dom.js';
import { buildPicker, openBranch } from './picker.js';
import { refreshGroupProfile, stopProfile } from './plateau-lamps.js';
import { installStrategyRegistry, settingsKey, state, supportsCalendar, targetKey, timeframeLabel } from './state.js';
import { applyToForm, fillStrategy, restoreForm, syncCalendarParams } from './strategy-form.js';
import { activeStrategyName, cancelSearch, clearGroup, currentRequest } from './strategy-plans.js';
import { stopWindowReadings, windowGates } from './window-table.js';

const SETTINGS_EVENT_KEY = 'moex-proof-settings-updated';

function setSegmentedTimeframe(id) {
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
      const btn = Object.assign(el('button', 'seg-option', item.label), { type: 'button' });
      btn.setAttribute('role', 'radio');
      btn.dataset.value = item.id;
      btn.addEventListener('click', () => setSegmentedTimeframe(item.id));
      btn.addEventListener('keydown', e => {
        const btns = [...group.querySelectorAll('[role=radio]')];
        const delta = e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 0;
        if (!delta || !btns.length) return;
        e.preventDefault();
        const nextBtn = btns[(idx + delta + btns.length) % btns.length];
        setSegmentedTimeframe(nextBtn.dataset.value);
        nextBtn.focus();
      });
      return btn;
    }),
  );
  setSegmentedTimeframe(state.preferences.timeframe);
}

function fallbackPair() {
  const pairs = state.catalog?.pair_screen?.pairs || [];
  return pairs.find(r => Object.values(r.proof_strategies || {}).some(Boolean)) || pairs[0] || null;
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
  const p = state.preferences;
  $('settings-badge').textContent = `${timeframeLabel(p.timeframe)} · ${FILL_RU[p.price_fill] || p.price_fill} · Капитал: ${fmt(x.initial_capital_rub, 2)} ₽ · Проверка: ${pct(x.validation_percent)} · Проскальзывание: ${fmt(x.slippage_ticks, 1)} т. · Сид: ${p.random_seed === 0 ? 'случайный' : p.random_seed}`;
  if (changed) {
    state.windowReadings.clear();
    stopWindowReadings();
    cancelSearch();
    clearGroup();
    if (state.catalog && state.sharedStrategy) refreshGroupProfile(0);
    if (!supportsCalendar(p.timeframe) && state.universe === 'calendar_native' && state.catalog) {
      const candidate = fallbackPair();
      const branch = candidate && state.branches.get('pair_screen|' + candidate.near);
      if (branch) openBranch(branch.key, candidate.id, '', false);
    }
    for (const row of $('window-list').querySelectorAll('tr[data-leaf]')) {
      const l = state.leaves.get(row.dataset.leaf);
      if (l) windowGates(row, l);
    }
    if (state.catalog && state.leafId) {
      const values = currentRequest();
      if (state.universe === 'pair_screen') fillStrategy();
      else syncCalendarParams();
      if (activeStrategyName() === values.strategy) applyToForm(values);
      else if (state.universe === 'pair_screen') restoreForm();
    }
  }
  return changed;
}

function previewSplit() {
  const n = $('validation-percent');
  const v = n.valueAsNumber;
  $('split-preview').textContent = !n.validity.valid
    ? 'Введите число от 0 до 50%.'
    : v === 0
      ? 'Обучение — 100%. Проверка отключена. Результат считается по всей истории обучения.'
      : `Обучение — ${pct(100 - v)}, проверка — ${pct(v)}. Результат считается только по проверке.`;
  if (n.validity.valid) $('validation-range').value = v;
}

async function refreshCatalog() {
  let target = state.leaves.get(state.leafId)?.target;
  const oldBranch = state.branchId;
  const oldStrategy = activeStrategyName();
  const [nextCatalog, nextStrategies] = await Promise.all([
    api('/api/proof/catalog'),
    api('/api/strategies').catch(() => null),
  ]);
  if (nextStrategies) installStrategyRegistry(nextStrategies);
  state.catalog = nextCatalog;
  buildPicker();
  if (!supportsCalendar(state.preferences.timeframe) && target?.universe === 'calendar_native') {
    const candidate = fallbackPair();
    target = candidate ? { universe: 'pair_screen', pair: candidate.id } : null;
  }
  let key = (target && state.leaves.get(targetKey(target))?.branch) || oldBranch;
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
      emit('moex:schedule', 0);
    }
  } catch (e) {
    status(e.message);
  }
}

export function bindSettings(runShow) {
  const dlg = $('global-settings');
  const n = $('validation-percent');
  const slip = $('global-slippage');
  const capital = $('global-capital');
  const fill = $('global-fill');
  const seed = $('global-seed');
  const submit = $('apply-settings');
  fill.replaceChildren(
    ...[
      ['reference_same_bar', 'Справочное закрытие (тот же бар)'],
      ['next_open', 'Следующее открытие (прокси исполнения)'],
      ['next_close', 'Следующее закрытие'],
    ].map(([value, label]) => Object.assign(el('option', '', label), { value })),
  );
  fill.value = state.preferences.price_fill;
  $('open-settings').addEventListener('click', () => {
    const p = state.preferences;
    n.value = p.validation_percent;
    slip.value = p.slippage_ticks;
    capital.value = p.initial_capital_rub;
    seed.value = p.random_seed;
    setSegmentedTimeframe(p.timeframe);
    fill.value = p.price_fill;
    $('allow-failed-volume').checked = p.allow_failed_volume;
    $('allow-failed-capital').checked = p.allow_failed_capital;
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
    if (state.savingSettings || ![n, slip, capital, fill, seed].every(x => x.reportValidity())) return;
    ++state.settingRevision;
    state.savingSettings = true;
    submit.disabled = $('cancel-settings').disabled = true;
    $('settings-error').textContent = '';
    cancelSearch();
    emit('moex:invalidate');
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
        localStorage.setItem(SETTINGS_EVENT_KEY, JSON.stringify([Date.now(), settingsKey()]));
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
        void runShow();
      } catch (err) {
        console.error(err);
      }
    }
  });
  window.addEventListener('storage', e => {
    if (e.key === SETTINGS_EVENT_KEY) void refreshGlobalSettings();
  });
  document.addEventListener('visibilitychange', () => {
    if (state.catalog && !document.hidden) void refreshGlobalSettings();
  });
}
