import { applyNormHint, buildCharts, destroyCharts, fitChartsToVisibleArea } from './charts.js';
import { $, api, apiStream, el, finishProgress, fmt, signed, startProgress, status, updateProgressChannel } from './dom.js';
import { copyRunId, downloadCsv, renderPeriodResult, renderEvidence, renderSummary, renderTable, toggleSummaryBlock } from './ledger.js';
import { buildPicker, initColumnResizers, openBranch } from './picker.js';
import { paintPlateaus } from './plateau-lamps.js';
import { bindSearchButton } from './search-tuning.js';
import { bindSettings, installTimeframeOptions, refreshGlobalSettings, setPreferences } from './settings-modal.js';
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
import { bindStrategyGlobalListeners } from './strategy-form.js';
import {
  activeStrategyName,
  cancelSearch,
  chartPlan,
  hasSelection,
  invalidForm,
  sharedRequest,
  tuneButton,
  tuneState,
} from './strategy-plans.js';
import { cacheWindowProof, paintWindowReadings, refreshWindowReadings, stopWindowReadings } from './window-table.js';

function switchTab(which) {
  $('tab-charts').classList.toggle('active', which === 'charts');
  $('tab-table').classList.toggle('active', which === 'table');
  $('tabpage-charts').classList.toggle('hidden', which !== 'charts');
  $('tabpage-table').classList.toggle('hidden', which !== 'table');
  if (which === 'charts') void fitChartsToVisibleArea();
}

function invalidateProof(fromSearch = false) {
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

function scheduleShow(delay = 90) {
  invalidateProof();
  state.autoRunTimer = setTimeout(() => {
    state.autoRunTimer = null;
    void show();
  }, delay);
}

function emptyProof(message) {
  $('period-result').hidden = true;
  $('chart-scope').textContent = 'Вся история';
  state.payload = null;
  state.selectedTrade = null;
  destroyCharts();
  const empty = Object.assign(el('div', 'empty', message), { id: 'charts-empty' });
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

function render() {
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

async function show(fromSearch = false) {
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

document.addEventListener('moex:schedule', e => scheduleShow(e.detail));
document.addEventListener('moex:invalidate', () => invalidateProof());
document.addEventListener('moex:stale-settings', () => void refreshGlobalSettings(true));

Object.assign(window, {
  branches: state.branches,
  leaves: state.leaves,
  checked: state.checked,
  windowReadings: state.windowReadings,
  refreshWindowReadings,
  renderPeriodResult,
  sharedRequest,
  targetKey,
  signed,
  fmt,
});
Object.defineProperties(window, {
  payload: { get: () => state.payload, configurable: true },
  leafId: { get: () => state.leafId, configurable: true },
});

initColumnResizers();
bindSettings(show);

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
    if (c.error) {
      status(`Кэш календарей недоступен: ${c.error}. Пары работают как обычно.`);
    } else {
      status(`${(c.assets || []).length} календарных активов · ${(c.pair_screen?.pairs || []).length} пар в реестре · кэш до ${c.till || '—'}`, true);
    }
    buildPicker();
    bindStrategyGlobalListeners();
    bindSearchButton(show);
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
