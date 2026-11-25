/** Lightweight-charts orchestration: panes, chart creation, series wiring and crosshair binding. */

import { $, COLORS, chartTime, rub } from './dom.js';
import { NORM_HINTS, NORM_PANEL, applyNormHint, spreadDisplayValues } from './normalizations.js';
import { isIntraday, state } from './state.js';
import { buildMarkers, buildSeries, chartOpts, makePane, markValidationStart, paneOptions } from './chart-series.js';
import { bindCrosshairTooltips, subscribeTradeClick, syncVisibleRanges } from './chart-sync.js';

export { NORM_HINTS, NORM_PANEL, applyNormHint, spreadDisplayValues };

export function ledgerIndex() {
  const m = {};
  for (const row of state.payload.ledger) m[row.date] = row;
  return m;
}

export function destroyCharts() {
  for (const c of state.charts.splice(0)) {
    try {
      c.remove();
    } catch (_) {}
  }
  state.markersApis.length = 0;
  state.primarySpreadChart = null;
}

export function fitChartsToVisibleArea() {
  const snapshot = state.charts.slice();
  return new Promise(resolve =>
    requestAnimationFrame(() =>
      requestAnimationFrame(() => {
        if (snapshot.length === state.charts.length && snapshot.every((chart, idx) => state.charts[idx] === chart)) {
          for (const chart of snapshot) {
            if (chart._pane?.classList.contains('hidden')) continue;
            try {
              chart.timeScale().fitContent();
            } catch (_) {}
          }
        }
        resolve();
      }),
    ),
  );
}

export function applySelectionToRows() {
  const sel = state.selectedTrade == null ? null : state.payload.trades.find(t => t.number === state.selectedTrade);
  document.querySelectorAll('#ledger-table tbody tr').forEach(tr => {
    const key = tr.dataset.dateKey;
    tr.classList.toggle('sel', !!sel && (sel.entry.date === key || sel.exit.date === key));
  });
}

export function refreshSelection() {
  for (const apiObj of state.markersApis) apiObj.setMarkers(buildMarkers());
  applySelectionToRows();
}

export function buildCharts() {
  const LWC = window.LightweightCharts;
  const root = $('charts');
  const tip = $('tip');
  for (const chart of state.charts) {
    try {
      chart.remove();
    } catch (_) {}
  }
  root.replaceChildren();
  state.charts.length = 0;
  state.markersApis.length = 0;
  state.primarySpreadChart = null;
  const S = state.payload.series;
  const L = ledgerIndex();
  const nearName = state.payload.instrument?.near ?? state.payload.window?.near ?? '';
  const farName = state.payload.instrument?.far ?? state.payload.window?.far ?? '';
  const nearPane = makePane(root, 'h-near', '1 · БЛИЖНЯЯ НОГА — ' + nearName, null, [['ближняя нога', COLORS.near]], true);
  const farPane = makePane(root, 'h-far', '2 · ДАЛЬНЯЯ НОГА — ' + farName, null, [['дальняя нога', COLORS.far]], true);
  const hasLast = S.some(r => r.spread_last_trade_rel != null);
  const normMode = $('norm')?.value || 'raw';
  const showLast = normMode === 'raw' && hasLast;
  const rawSpreadPane = makePane(
    root,
    'h-spread-raw',
    '3 · СПРЕД (ненормализованный)',
    '% от дальней ноги',
    [['расчётный', COLORS.spread]]
      .concat(hasLast ? [['последняя сделка', '#88a1b5']] : [])
      .concat(
        state.payload.signal_levels?.kind === 'entry_bands' && S.some(r => r.buy_level_rel != null)
          ? [
              ['порог покупки', '#65adf1'],
              ['порог продажи', '#f0a36d'],
            ]
          : [],
      ),
    true,
  );
  rawSpreadPane.previousElementSibling.title = state.payload.signal_levels?.note || '';
  const spreadPane = makePane(
    root,
    'h-spread',
    '4 · СПРЕД — входы и выходы',
    null,
    [['расчётный', COLORS.spread]].concat(showLast ? [['последняя сделка', '#88a1b5']] : []),
  );
  const calendarProxy = state.universe === 'calendar_native' && state.payload.window?.price_source === 'synthetic_leg_proxy';
  const volTagline =
    state.universe === 'pair_screen' || calendarProxy
      ? 'min(ближняя, дальняя) — синхронные ноги / proxy'
      : 'нативный инструмент спреда';
  const volPane = makePane(
    root,
    'h-vol',
    isIntraday(state.payload.selection?.timeframe) ? '5 · ОБЪЁМ за бар' : '5 · ОБЪЁМ за день',
    volTagline,
    [['объём', COLORS.vol]],
    true,
  );
  const showFunding = S.some(r => r.funding_rub != null) || state.payload.ledger.some(r => r.funding_rub != null);
  const totalFunding = showFunding ? state.payload.ledger.reduce((acc, r) => acc + (r.funding_rub ?? 0), 0) : null;
  const fundingPane = showFunding
    ? makePane(root, 'h-fund', '6 · ФАНДИНГ: начислено (+) / списано (−)', `по удерживаемой позиции · нетто ${rub(totalFunding, 2)}`, [['фондирование', COLORS.fund]])
    : null;
  const cumNo = showFunding ? 7 : 6;
  const cumPane = makePane(
    root,
    'h-cum',
    `${cumNo} · КУМУЛЯТИВНЫЙ НЕТТО — ₽ после издержек`,
    'просадка от текущего пика — по левой оси',
    [
      ['кумулятив', COLORS.spread],
      ['просадка', COLORS.warn],
    ],
  );
  const { legFrom, cumFrom } = paneOptions(chartOpts());
  const cNear = LWC.createChart(nearPane, legFrom());
  const cFar = LWC.createChart(farPane, legFrom());
  const cSpreadRaw = LWC.createChart(rawSpreadPane, legFrom());
  const cSpread = LWC.createChart(spreadPane, chartOpts());
  const cVol = LWC.createChart(volPane, chartOpts());
  nearPane._chart = cNear;
  farPane._chart = cFar;
  rawSpreadPane._chart = cSpreadRaw;
  spreadPane._chart = cSpread;
  volPane._chart = cVol;
  state.primarySpreadChart = cSpread;
  const cFund = fundingPane ? LWC.createChart(fundingPane, legFrom()) : null;
  const cCum = LWC.createChart(cumPane, cumFrom());
  state.charts.push(cNear, cFar, cSpreadRaw, cSpread, cVol);
  if (cFund) state.charts.push(cFund);
  state.charts.push(cCum);

  const { nearSeries, farSeries, rawSpreadSeries, sRaw, spreadSeries, sDisp, volSeries, fundSeries, cumSeries } = buildSeries({
    LWC,
    S,
    L,
    cNear,
    cFar,
    cSpreadRaw,
    cSpread,
    cVol,
    cFund,
    cCum,
    rawSpreadPane,
    hasLast,
    showLast,
    calendarProxy,
  });
  const markers = buildMarkers();
  state.markersApis.push(LWC.createSeriesMarkers(rawSpreadSeries, markers), LWC.createSeriesMarkers(spreadSeries, markers));

  const seriesByTime = new Map(S.map((r, i) => [chartTime(r.date), { r, i }]));
  subscribeTradeClick([cSpreadRaw, cSpread], seriesByTime, refreshSelection);

  const tipDiv = tip;
  const chartMeta = new Map([
    [cNear, { pane: nearPane, series: nearSeries, value: d => seriesByTime.get(d)?.r.near }],
    [cFar, { pane: farPane, series: farSeries, value: d => seriesByTime.get(d)?.r.far }],
    [cSpreadRaw, { pane: rawSpreadPane, series: rawSpreadSeries, value: d => { const e = seriesByTime.get(d); return e ? sRaw[e.i] : null; } }],
    [cSpread, { pane: spreadPane, series: spreadSeries, value: d => { const e = seriesByTime.get(d); return e ? sDisp[e.i] : null; } }],
    [cVol, { pane: volPane, series: volSeries, value: d => { const r = seriesByTime.get(d)?.r; return r ? ((state.universe === 'pair_screen' ? Math.min(r.near_volume ?? 0, r.far_volume ?? 0) : r.spread_volume) ?? 0) : null; } }],
  ]);
  if (cFund && fundSeries) chartMeta.set(cFund, { pane: fundingPane, series: fundSeries, value: d => seriesByTime.get(d)?.r.funding_rub });
  chartMeta.set(cCum, { pane: cumPane, series: cumSeries, value: d => L[seriesByTime.get(d)?.r.date]?.cumulative_net_rub });
  chartMeta.forEach(({ pane }, chart) => {
    chart._pane = pane;
    markValidationStart(chart, pane, state.payload.evaluation.periods.validation?.[0]);
  });

  syncVisibleRanges(state.charts);
  bindCrosshairTooltips({ root, tipDiv, chartMeta, L, seriesByTime });
  root.append(tipDiv);
  return fitChartsToVisibleArea();
}
