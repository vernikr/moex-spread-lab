/** Lightweight-charts series construction: per-pane data, price formats and trade markers. */

import { COLORS, axisDateParts, axisDateText, chartTime, fmt, px, rub } from './dom.js';
import { spreadDisplayValues } from './normalizations.js';
import { markerColor } from './chart-panes.js';
import { isIntraday, legQuoteIsRuble, state } from './state.js';

/** Base chart options; leg panes hide the time axis, the cumulative pane adds a left scale. */
export function chartOpts() {
  return {
    layout: { background: { color: 'transparent' }, textColor: '#8ea3b6', fontSize: 10, attributionLogo: false },
    grid: { vertLines: { color: '#1b2a3a' }, horzLines: { color: '#1b2a3a' } },
    crosshair: {
      vertLine: { color: '#cfe3f2b3', width: 1, style: 2, labelBackgroundColor: '#2a4a63' },
      horzLine: { color: '#9bbbd499', width: 1, style: 2, labelBackgroundColor: '#2a4a63' },
    },
    handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: true },
    handleScale: { mouseWheel: false },
    timeScale: {
      borderColor: '#293b4e',
      rightOffset: 3,
      timeVisible: false,
      secondsVisible: false,
      tickMarkFormatter: (time, type) => {
        const p = axisDateParts(time);
        if (!p) return null;
        return type === 0 ? p.year : type === 1 ? p.month + '.' + p.year : p.day + '.' + p.month;
      },
    },
    rightPriceScale: { borderColor: '#293b4e' },
    autoSize: true,
    localization: { locale: 'ru-RU', timeFormatter: time => axisDateText(time) ?? String(time) },
  };
}

export function paneOptions(opts) {
  const cloneOpts = o => {
    const c = { ...o };
    for (const k of ['layout', 'grid', 'crosshair', 'handleScroll', 'handleScale', 'timeScale', 'rightPriceScale', 'localization']) {
      if (c[k]) c[k] = { ...c[k] };
    }
    return c;
  };
  const legFrom = () => {
    const o = cloneOpts(opts);
    o.timeScale.visible = false;
    return o;
  };
  const cumFrom = () => ({ ...cloneOpts(opts), leftPriceScale: { visible: true, borderColor: '#293b4e' } });
  return { legFrom, cumFrom };
}

export function buildMarkers() {
  const m = [];
  for (const t of state.payload.trades) {
    const dir = t.side === 'long_spread' ? 1 : -1;
    const sel = t.number === state.selectedTrade;
    const size = sel ? 2 : 1;
    m.push({
      time: chartTime(t.entry.date),
      position: dir > 0 ? 'belowBar' : 'aboveBar',
      color: sel ? '#ffffff' : COLORS.entry,
      shape: dir > 0 ? 'arrowUp' : 'arrowDown',
      text: `#${t.number} ${dir > 0 ? 'ПОК' : 'ПРОД'} · ${t.quantity}@${t.entry.spread_fill_rel != null ? fmt(t.entry.spread_fill_rel, 2) + '%' : px(t.entry.spread_fill)}`,
      size,
    });
    m.push({
      time: chartTime(t.exit.date),
      position: dir > 0 ? 'aboveBar' : 'belowBar',
      color: markerColor(t, sel),
      shape: 'circle',
      text: t.open_at_end ? `#${t.number} ОТКР · форсировано` : `#${t.number} ${rub(t.net_rub)}`,
      size,
    });
  }
  m.sort((a, b) => (a.time < b.time ? -1 : a.time > b.time ? 1 : 0));
  return m;
}

export function buildSeries({
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
}) {
  const lineSeries = (chart, color, width = 2, dashed = false, priceFormat = null) =>
    chart.addSeries(LWC.LineSeries, {
      color,
      lineWidth: width,
      lineStyle: dashed ? 2 : 0,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: true,
      ...(priceFormat ? { priceFormat } : {}),
    });
  const axisRub = v => (Number.isFinite(v) ? Math.round(v) + ' ₽' : '—');
  const legPriceFormat = legQuoteIsRuble()
    ? { type: 'custom', formatter: axisRub, minMove: 1 }
    : { type: 'price', precision: 0, minMove: 1 };
  const nearSeries = lineSeries(cNear, COLORS.near, 2, false, legPriceFormat);
  const farSeries = lineSeries(cFar, COLORS.far, 2, false, legPriceFormat);
  nearSeries.setData(S.map(r => ({ time: chartTime(r.date), value: r.near })));
  farSeries.setData(S.map(r => ({ time: chartTime(r.date), value: r.far })));

  const rawSpreadFormat = { type: 'percent', precision: 0, minMove: 1 };
  const rawSpreadSeries = lineSeries(cSpreadRaw, COLORS.spread, 2, false, rawSpreadFormat);
  const sRaw = spreadDisplayValues(S, 'raw');
  rawSpreadSeries.setData(S.map((r, i) => (sRaw[i] != null ? { time: chartTime(r.date), value: sRaw[i] } : { time: chartTime(r.date) })));
  rawSpreadPane._signalLevels = {};
  for (const [name, key, color] of [
    ['buy', 'buy_level_rel', '#65adf1'],
    ['sell', 'sell_level_rel', '#f0a36d'],
  ]) {
    if (S.some(r => r[key] != null)) {
      const line = lineSeries(cSpreadRaw, color, 1, false, rawSpreadFormat);
      line.setData(S.map(r => (r[key] != null ? { time: chartTime(r.date), value: r[key] } : { time: chartTime(r.date) })));
      rawSpreadPane._signalLevels[name] = line;
    }
  }
  rawSpreadPane.dataset.signalLines = String(Object.keys(rawSpreadPane._signalLevels).length);
  if (hasLast) {
    const rawLt = lineSeries(cSpreadRaw, '#88a1b5', 1, true, rawSpreadFormat);
    rawLt.setData(S.map(r => (r.spread_last_trade_rel != null ? { time: chartTime(r.date), value: r.spread_last_trade_rel } : { time: chartTime(r.date) })));
  }

  const spreadDisplayFormat = { type: 'price', precision: 0, minMove: 1 };
  const spreadSeries = lineSeries(cSpread, COLORS.spread, 2, false, spreadDisplayFormat);
  const sDisp = spreadDisplayValues(S);
  spreadSeries.setData(S.map((r, i) => (sDisp[i] != null ? { time: chartTime(r.date), value: sDisp[i] } : { time: chartTime(r.date) })));
  if (showLast) {
    const lt = lineSeries(cSpread, '#88a1b5', 1, true, spreadDisplayFormat);
    lt.setData(S.map(r => (r.spread_last_trade_rel != null ? { time: chartTime(r.date), value: r.spread_last_trade_rel } : { time: chartTime(r.date) })));
  }

  const volSeries = cVol.addSeries(LWC.HistogramSeries, {
    priceFormat: { type: 'volume', precision: 0, minMove: 1 },
    priceLineVisible: false,
    lastValueVisible: false,
  });
  volSeries.setData(
    S.map(r => ({
      time: chartTime(r.date),
      value: (state.universe === 'pair_screen' || calendarProxy ? Math.min(r.near_volume ?? 0, r.far_volume ?? 0) : r.spread_volume) ?? 0,
      color: (L[r.date]?.mark_pnl_rub ?? 0) >= 0 ? '#2f8f6a' : '#a24d47',
    })),
  );
  const rule = state.payload.window.min_daily_volume_rule;
  if (rule != null && !isIntraday(state.payload.selection?.timeframe)) {
    volSeries.createPriceLine({ price: rule, color: COLORS.loss, lineStyle: 2, lineWidth: 1, axisLabelVisible: true, title: 'правило' });
  }

  let fundSeries = null;
  if (cFund) {
    fundSeries = cFund.addSeries(LWC.HistogramSeries, {
      priceLineVisible: false,
      lastValueVisible: false,
      base: 0,
      priceFormat: { type: 'custom', formatter: axisRub, minMove: 1 },
    });
    fundSeries.setData(
      S.map(r => (r.funding_rub != null ? { time: chartTime(r.date), value: r.funding_rub, color: r.funding_rub >= 0 ? '#5b8f6a' : '#a24d47' } : { time: chartTime(r.date) })),
    );
    fundSeries.createPriceLine({ price: 0, color: '#3b5367', lineStyle: 0, lineWidth: 1, axisLabelVisible: false, title: '' });
  }

  const cumSeries = cCum.addSeries(LWC.LineSeries, {
    color: COLORS.spread,
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: false,
    priceFormat: { type: 'custom', formatter: axisRub, minMove: 1 },
  });
  cumSeries.setData(state.payload.ledger.map(r => ({ time: chartTime(r.date), value: r.cumulative_net_rub })));
  cumSeries.createPriceLine({ price: 0, color: '#3b5367', lineStyle: 0, lineWidth: 1, axisLabelVisible: false, title: '' });

  let peak = -Infinity;
  state.drawdownByDate = {};
  const ddPoints = state.payload.ledger.map(r => {
    peak = Math.max(peak, r.cumulative_net_rub);
    const dd = r.cumulative_net_rub - peak;
    state.drawdownByDate[r.date] = dd;
    return { time: chartTime(r.date), value: dd };
  });
  const ddSeries = cCum.addSeries(LWC.LineSeries, {
    color: COLORS.warn,
    lineWidth: 1,
    lineStyle: 2,
    priceLineVisible: false,
    lastValueVisible: false,
    crosshairMarkerVisible: false,
    priceScaleId: 'left',
    priceFormat: { type: 'custom', formatter: axisRub, minMove: 1 },
  });
  ddSeries.setData(ddPoints);

  return { nearSeries, farSeries, rawSpreadSeries, sRaw, spreadSeries, sDisp, volSeries, fundSeries, cumSeries, ddSeries };
}
