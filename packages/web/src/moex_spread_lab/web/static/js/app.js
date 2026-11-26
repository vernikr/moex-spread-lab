import { applyNormHint, buildCharts, destroyCharts, fitChartsToVisibleArea } from './charts.js';
import {
  $,
  api,
  apiStream,
  el,
  finishProgress,
  fmt,
  signed,
  startProgress,
  status,
  updateProgressChannel,
} from './dom.js';
import {
  copyRunId,
  downloadCsv,
  renderEvidence,
  renderPeriodResult,
  renderSummary,
  renderTable,
  toggleSummaryBlock,
} from './ledger.js';
import {
  buildPicker,
  cacheWindowProof,
  configurePickerCallbacks,
  initColumnResizers,
  openBranch,
  paintWindowReadings,
  refreshChecks,
  refreshWindowReadings,
  stopWindowReadings,
} from './picker.js';
import {
  bindSettings,
  configureSettingsCallbacks,
  installTimeframeOptions,
  refreshGlobalSettings,
  setPreferences,
} from './settings-modal.js';
import {
  currentPair,
  installStrategyRegistry,
  pairAvailability,
  pairUnavailable,
  settingsKey,
  state,
  supportsCalendar,
  targetKey,
} from './state.js';
import {
  activeStrategyName,
  bindStrategyGlobalListeners,
  cancelSearch,
  chartPlan,
  configureStrategyFormCallbacks,
  hasSelection,
  invalidForm,
  paintPlateaus,
  sharedRequest,
  tuneButton,
  tuneState,
} from './strategy-form.js';
import { bindSearchButton, configureSearchCallbacks } from './search-tuning.js';

export function switchTab(which) {
  for (const [id, w] of [
    ['tab-charts', 'charts'],
    ['tab-table', 'table'],
  ]) {
    $(id).classList.toggle('active', w === which);
  }
  $('tabpage-charts').classList.toggle('hidden', which !== 'charts');
  $('tabpage-table').classList.toggle('hidden', which !== 'table');
  if (which === 'charts') void fitChartsToVisibleArea();
}

export function invalidateProof(fromSearch = false) {
  if (!fromSearch && !state.searchUsesChecks) cancelSearch();
  stopWindowReadings();
  if (state.catalog && state.leafId) paintWindowReadings();
  clearTimeout(state.autoRunTimer);
  state.autoRunTimer = null;
  state.analysisRequestId++;
  state.proofController?.abort();
  state.proofController = null;
  finishProgress('chart');
  tuneButton();
  if (!state.tuning) tuneState('');
  paintPlateaus();
}

export function scheduleShow(delay = 90) {
  invalidateProof();
  state.autoRunTimer = setTimeout(() => {
    state.autoRunTimer = null;
    void show();
  }, delay);
}

export function emptyProof(message) {
  $('period-result').hidden = true;
  $('chart-scope').textContent = 'Вся история';
  state.payload = null;
  state.selectedTrade = null;
  destroyCharts();
  const empty = el('div', 'empty', message);
  empty.id = 'charts-empty';
  const chartTip = $('tip');
  chartTip.style.display = 'none';
  $('charts').replaceChildren(empty, chartTip);
  const notes = $('notes');
  notes.textContent = '';
  $('ledger-table').replaceChildren(el('div', 'empty', message), notes);
  $('evidence').classList.remove('show');
  $('flags').replaceChildren();
  $('tiles').replaceChildren();
  applyNormHint();
}

export function render() {
  renderPeriodResult();
  renderEvidence();
  renderSummary();
  $('charts-empty')?.remove();
  const fit = buildCharts();
  renderTable();
  $('notes').textContent = state.payload.notes.join(' · ');
  applyNormHint();
  return fit;
}

export async function show(fromSearch = false) {
  invalidateProof(fromSearch);
  const id = state.analysisRequestId;
  const plan = chartPlan();
  const request = plan.req;
  const fresh = () => id === state.analysisRequestId;
  state.chartKey = plan.key;
  state.chartWithLamps = !state.checked.size;
  const message = plan.common
    ? ''
    : !hasSelection()
      ? `Для выбранного инструмента: ${pairUnavailable(pairAvailability(currentPair()), activeStrategyName())}.`
      : invalidForm();
  if (message) {
    status(message);
    paintPlateaus(null, message);
    emptyProof(message);
    refreshWindowReadings();
    return;
  }
  const controller = new AbortController();
  state.proofController = controller;
  $('period-result').hidden = true;
  startProgress('chart', 'Запуск расчёта окна…', 8, 32, 900);
  status('');
  paintPlateaus(null, 'Выполняется расчёт…');
  try {
    const next = await apiStream('/api/proof/run', { ...request, include_plateau: state.chartWithLamps }, controller.signal, ev => {
      if (!fresh()) return;
      const ceil = ev.stage === 'prepare' ? 32 : ev.stage === 'run' ? 56 : 95;
      updateProgressChannel('chart', ev.percent || 0, ev.label || 'Расчёт окна…', ceil, ev.stage === 'run' ? 900 : 0);
      paintPlateaus(null, `${ev.label || 'Выполняется расчёт…'} (${Math.round(ev.percent || 0)}%)`);
    });
    if (!fresh()) return;
    if (settingsKey(next.evaluation) !== settingsKey()) {
      void refreshGlobalSettings(true);
      return;
    }
    updateProgressChannel('chart', 96, 'Отрисовка графиков и таблицы…', 99, 300);
    state.payload = next;
    cacheWindowProof(request, next);
    state.selectedTrade = null;
    state.dayClick = { day: null, index: 0 };
    paintPlateaus(next.plateau);
    switchTab('charts');
    await render();
    if (fresh()) status('');
  } catch (e) {
    if (fresh()) {
      status(e.message);
      paintPlateaus(null, e.message);
      emptyProof('Нет результата для текущих параметров.');
    }
  } finally {
    if (fresh()) {
      state.proofController = null;
      finishProgress('chart');
      tuneButton();
      refreshWindowReadings();
    }
  }
}

function exposeWindowBindings() {
  Object.defineProperties(window, {
    payload: { get: () => state.payload, set: v => { state.payload = v; }, configurable: true },
    catalog: { get: () => state.catalog, set: v => { state.catalog = v; }, configurable: true },
    branches: { get: () => state.branches, configurable: true },
    leaves: { get: () => state.leaves, configurable: true },
    checked: { get: () => state.checked, configurable: true },
    leafId: { get: () => state.leafId, set: v => { state.leafId = v; }, configurable: true },
    branchId: { get: () => state.branchId, set: v => { state.branchId = v; }, configurable: true },
    universe: { get: () => state.universe, set: v => { state.universe = v; }, configurable: true },
    windowReadings: { get: () => state.windowReadings, configurable: true },
    refreshWindowReadings: { value: refreshWindowReadings, configurable: true },
    renderPeriodResult: { value: renderPeriodResult, configurable: true },
    sharedRequest: { value: sharedRequest, configurable: true },
    targetKey: { value: targetKey, configurable: true },
    signed: { value: signed, configurable: true },
    fmt: { value: fmt, configurable: true },
  });
}

configureStrategyFormCallbacks({
  scheduleShow,
  invalidateProof,
  refreshGlobalSettings,
  refreshWindowReadings,
});
configureSearchCallbacks({
  show,
  invalidateProof,
  refreshGlobalSettings,
  stopWindowReadings,
  refreshChecks,
});
configurePickerCallbacks({
  scheduleShow,
  refreshGlobalSettings,
});
configureSettingsCallbacks({
  scheduleShow,
  show,
  invalidateProof,
});

exposeWindowBindings();
initColumnResizers();
bindSettings();

(async () => {
  startProgress('boot', 'Загрузка каталога и настроек…', 8, 68, 3500);
  try {
    const [settings, c, strategiesRegistry, timeframes] = await Promise.all([
      api('/api/settings').catch(() => null),
      api('/api/proof/catalog'),
      api('/api/strategies').catch(() => null),
      api('/api/timeframes').catch(() => null),
    ]);
    updateProgressChannel('boot', 74, 'Построение списка инструментов…', 78, 200);
    if (strategiesRegistry) installStrategyRegistry(strategiesRegistry);
    installTimeframeOptions(timeframes || c.timeframes || []);
    if (settings) setPreferences(settings);
    $('open-settings').disabled = false;
    state.catalog = c;
    if (state.catalog.error) {
      status(`Кэш календарей недоступен: ${state.catalog.error}. Пары работают как обычно.`);
    } else {
      status(
        `${(state.catalog.assets || []).length} календарных активов · ${(state.catalog.pair_screen?.pairs || []).length} пар в реестре · кэш до ${state.catalog.till || '—'}`,
        true,
      );
    }
    buildPicker();
    bindStrategyGlobalListeners();
    bindSearchButton();
    $('toggle-summary').addEventListener('click', toggleSummaryBlock);
    $('copy-runid').addEventListener('click', copyRunId);
    $('download-csv').addEventListener('click', downloadCsv);
    $('tab-charts').addEventListener('click', () => switchTab('charts'));
    $('tab-table').addEventListener('click', () => switchTab('table'));
    $('norm').addEventListener('change', () => {
      applyNormHint();
      if (state.payload) buildCharts();
    });
    applyNormHint();

    const q = new URLSearchParams(location.search);
    const kind = q.get('universe');
    const pair = q.get('pair');
    const desired =
      kind === 'calendar_native' && supportsCalendar(state.preferences.timeframe)
        ? 'calendar_native|' + q.get('asset')
        : kind === 'pair_screen'
          ? 'pair_screen|' + pairAvailability(pair)?.near
          : null;
    const first = [...state.branches.values()].find(b => b.children.some(l => l.enabled)) || [...state.branches.values()][0];
    openBranch(state.branches.has(desired) ? desired : first?.key, pair, q.get('strategy'), false);
    finishProgress('boot');
    void show();
  } catch (e) {
    finishProgress('boot');
    $('open-settings').disabled = false;
    status(e.message);
  }
})();
