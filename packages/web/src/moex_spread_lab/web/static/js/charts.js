/** Lightweight-charts orchestration: panes, chart creation, series wiring and crosshair binding. */

import { $, cls, COLORS, chartTime, displayTime, escapeHtml, fmt, humanCost, rub, signed } from './dom.js';
import { NORM_PANEL, applyNormHint, spreadDisplayValues } from './normalizations.js';
import { isIntraday, legPrice, state } from './state.js';
import { buildMarkers, buildSeries, chartOpts, makePane, markValidationStart, paneOptions } from './chart-series.js';

export { applyNormHint };

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

  const { nearSeries, farSeries, rawSpreadSeries, sRaw, spreadSeries, sDisp, volSeries, volumeOf, fundSeries, cumSeries } = buildSeries({
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
  const eventsByDate = {};
  for (const t of state.payload.trades) {
    for (const [kind, leg] of [['ENTRY', t.entry], ['EXIT', t.exit]]) (eventsByDate[leg.date] ||= []).push({ kind, t });
  }
  subscribeTradeClick([cSpreadRaw, cSpread], seriesByTime, eventsByDate);

  const tipDiv = tip;
  const chartMeta = new Map([
    [cNear, { pane: nearPane, series: nearSeries, value: r => r.near }],
    [cFar, { pane: farPane, series: farSeries, value: r => r.far }],
    [cSpreadRaw, { pane: rawSpreadPane, series: rawSpreadSeries, value: (_, i) => sRaw[i] }],
    [cSpread, { pane: spreadPane, series: spreadSeries, value: (_, i) => sDisp[i] }],
    [cVol, { pane: volPane, series: volSeries, value: volumeOf }],
  ]);
  if (cFund && fundSeries) chartMeta.set(cFund, { pane: fundingPane, series: fundSeries, value: r => r.funding_rub });
  chartMeta.set(cCum, { pane: cumPane, series: cumSeries, value: r => L[r.date]?.cumulative_net_rub });
  chartMeta.forEach(({ pane }, chart) => {
    chart._pane = pane;
    markValidationStart(chart, pane, state.payload.evaluation.periods.validation?.[0]);
  });

  syncVisibleRanges(state.charts);
  bindCrosshairTooltips({ root, tipDiv, chartMeta, L, seriesByTime, eventsByDate });
  root.append(tipDiv);
  return fitChartsToVisibleArea();
}

function subscribeTradeClick(charts, seriesByTime, eventsByDate) {
  const onSpreadClick = param => {
    const day = param.time == null ? null : seriesByTime.get(param.time)?.r.date;
    if (!day) return;
    const hits = [...new Set(eventsByDate[day]?.map(e => e.t))];
    if (!hits.length) return;
    if (state.dayClick.day === day) state.dayClick.index = (state.dayClick.index + 1) % hits.length;
    else state.dayClick = { day, index: 0 };
    state.selectedTrade = hits[state.dayClick.index].number;
    refreshSelection();
  };
  for (const chart of charts) chart.subscribeClick(onSpreadClick);
}

function syncVisibleRanges(charts) {
  let syncing = false;
  charts.forEach(chart => {
    chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (syncing || !range || chart._pane?.classList.contains('hidden') || chart._pane?._revealing) return;
      syncing = true;
      for (const other of charts) {
        if (other !== chart && !other._pane?.classList.contains('hidden') && !other._pane?._revealing) {
          try {
            other.timeScale().setVisibleLogicalRange(range);
          } catch (_) {}
        }
      }
      syncing = false;
    });
  });
}

function bindCrosshairTooltips({ root, tipDiv, chartMeta, L, seriesByTime, eventsByDate }) {
  let syncingCrosshair = false;
  let activeHoverChart = null;
  const clearCrosshairs = (except = null) => {
    syncingCrosshair = true;
    for (const other of state.charts) {
      if (other !== except) {
        try {
          other.clearCrosshairPosition();
        } catch (_) {}
      }
    }
    syncingCrosshair = false;
  };
  root.onmouseleave = () => {
    activeHoverChart = null;
    tipDiv.style.display = 'none';
    clearCrosshairs();
  };
  chartMeta.forEach(({ pane: paneEl }, chart) => {
    chart.subscribeCrosshairMove(param => {
      if (syncingCrosshair) return;
      const day = param.time;
      const pt = param.point;
      const entry = !day || !pt || pt.x < 0 || pt.y < 0 ? null : seriesByTime.get(day);
      if (!entry) {
        if (activeHoverChart === chart) {
          activeHoverChart = null;
          tipDiv.style.display = 'none';
          clearCrosshairs(chart);
        }
        return;
      }
      activeHoverChart = chart;
      syncingCrosshair = true;
      chartMeta.forEach((otherMeta, other) => {
        if (other === chart || otherMeta.pane.classList.contains('hidden')) return;
        const v = otherMeta.value(entry.r, entry.i);
        try {
          other.setCrosshairPosition(v != null && isFinite(v) ? v : NaN, day, otherMeta.series);
        } catch (_) {}
      });
      syncingCrosshair = false;
      tipDiv.innerHTML = tipHtml(entry.r, L[entry.r.date], entry.i, eventsByDate[entry.r.date] || []);
      tipDiv.style.display = 'block';
      const wb = root.getBoundingClientRect();
      const leftScaleW = (() => {
        try {
          return chart.priceScale('left').width() || 0;
        } catch (_) {
          return 0;
        }
      })();
      const anchorX = paneEl.offsetLeft + leftScaleW + pt.x;
      let left = anchorX + 16;
      let top = paneEl.offsetTop + pt.y + 8;
      if (left + tipDiv.offsetWidth > wb.width - 8) left = Math.max(8, anchorX - tipDiv.offsetWidth - 16);
      const minTop = root.scrollTop + 4;
      const maxTop = Math.max(minTop, root.scrollTop + wb.height - tipDiv.offsetHeight - 4);
      top = Math.min(Math.max(minTop, top), maxTop);
      tipDiv.style.left = left + 'px';
      tipDiv.style.top = top + 'px';
    });
  });
}

/* Crosshair tooltip body: one series point plus every entry/exit event on that date. */

function tipHtml(s, l, i, dayEvents) {
  let h = `<b>${displayTime(s.date)}</b>${s.days_to_near_expiry != null ? ` <span class=\"k\">· ${s.days_to_near_expiry}д до экспирации ближней</span>` : ''}<hr>`;
  h += `<span class=\"k\">ближняя</span> ${legPrice(s.near)} <span class=\"k\">(откр. ${legPrice(s.near_open)}, объём ${fmt(s.near_volume)})</span><br><span class=\"k\">дальняя</span> ${legPrice(s.far)} <span class=\"k\">(откр. ${legPrice(s.far_open)}, объём ${fmt(s.far_volume)})</span><br>`;
  h += `<span class=\"k\">спред</span> <b>${s.spread_rel != null ? fmt(s.spread_rel, 2) + '%' : '—'}</b> <span class=\"k\">(абс. ${legPrice(s.spread)}${s.spread_open_rel != null ? ` · откр. ${fmt(s.spread_open_rel, 2)}%` : ''}${s.spread_last_trade_rel != null ? ` · посл. ${fmt(s.spread_last_trade_rel, 2)}%` : ''})</span><br>`;
  const nmode = $('norm')?.value || 'raw';
  if (nmode !== 'raw') {
    const v = i == null ? null : spreadDisplayValues(state.payload.series)[i];
    const unit = { stoch20: '%', rz20: 'σ', ewma10: 'σ', mad20: 'σ', garch: 'σ', kalman: 'σ' }[nmode] || 'pp';
    h += `<span class=\"k\">нормализован · ${NORM_PANEL[nmode] || nmode}</span> ${v == null ? '—' : fmt(v, 2)}${unit === 'pp' ? '%' : unit}<br>`;
  }
  h += `<span class=\"k\">${escapeHtml(isIntraday(s.timeframe) ? 'объём за бар' : 'объём за день')}</span> ближняя ${fmt(s.near_volume)} · дальняя ${fmt(s.far_volume)}${s.spread_volume != null ? ` · спред ${fmt(s.spread_volume)}${s.spread_trades != null ? ' за ' + fmt(s.spread_trades) + ' сделок' : ''}` : ''}`;
  if (s.buy_level_rel != null) {
    h += `<br><span class=\"k\">Порог покупки / продажи</span> ${fmt(s.buy_level_rel, 3)}% / ${fmt(s.sell_level_rel, 3)}% · на закрытии сигнального бара`;
  }
  if (s.funding_rub != null) {
    h += `<br><span class=\"k\">фандинг: ${s.funding_rub >= 0 ? 'начислено' : 'списано'}</span> ${signed(s.funding_rub)} ₽`;
  }
  if (s.signal_z != null) h += `<br><span class=\"k\">сигнал z</span> ${fmt(s.signal_z, 2)}`;
  if (s.dev_pp != null) h += `<br><span class=\"k\">имплицитная ставка − RUONIA</span> ${fmt(s.dev_pp, 2)} pp`;
  if (s.ruonia_rate_date) h += `<br><span class=\"k\">RUONIA as-of</span> ${s.ruonia_rate_date} · ${fmt(s.ruonia_rate_pct, 2)}%`;
  if (l) {
    h += `<hr><span class=\"k\">позиция</span> ${l.position > 0 ? 'лонг' : l.position < 0 ? 'шорт' : 'флэт'}${l.quantity ? ` · ${l.quantity} контр.` : ''} · <span class=\"k\">${isIntraday(s.timeframe) ? 'результат бара' : 'результат дня'}</span> ${rub(l.mark_pnl_rub, 2)} · <span class=\"k\">издержки</span> −${fmt(l.costs_rub, 2)} ₽${l.funding_rub != null ? ` · <span class=\"k\">фондирование</span> ${signed(l.funding_rub)} ₽` : ''} · <span class=\"k\">кумулятивно</span> <b class=\"${cls(l.cumulative_net_rub)}\">${rub(l.cumulative_net_rub, 2)}</b>${state.drawdownByDate[s.date] != null ? ` · <span class=\"k\">просадка</span> <b class=\"warn\">−${fmt(-state.drawdownByDate[s.date], 2)} ₽</b>` : ''}`;
  }
  for (const { kind, t } of dayEvents) {
    const leg = kind === 'ENTRY' ? t.entry : t.exit;
    const fees = Object.entries(leg.costs || {})
      .filter(([, v]) => v)
      .map(([k, v]) => `${escapeHtml(humanCost(k))} ${fmt(v, 2)} ₽`)
      .join(', ');
    h += `<hr><b>#${t.number} ${kind === 'ENTRY' ? 'ВХОД' : 'ВЫХОД'}</b> ${t.side === 'long_spread' ? 'покупка ближней / продажа дальней' : 'продажа ближней / покупка дальней'} · контрактов: ${t.quantity}${t.open_at_end && kind === 'EXIT' ? ' · принудительная отметка' : ''}<br><span class=\"k\">исполнение спреда</span> <b>${legPrice(leg.spread_fill)}</b> · <span class=\"k\">ноги</span> ${legPrice(leg.near_price)} / ${legPrice(leg.far_price)}<br><span class=\"k\">${escapeHtml(isIntraday(state.payload.selection?.timeframe) ? 'объём за бар' : 'объём за день')}</span> ближняя ${fmt(leg.near_volume)} · дальняя ${fmt(leg.far_volume)}${leg.spread_volume != null ? ` · спред ${fmt(leg.spread_volume)}` : ''}<br><span class=\"k\">комиссии</span> ${fees || '—'}`;
    if (kind === 'EXIT') h += `<br><span class=\"k\">результат</span> гросс ${rub(t.gross_rub, 2)} · нетто <b class=\"${cls(t.net_rub)}\">${rub(t.net_rub, 2)}</b>`;
  }
  return h;
}
