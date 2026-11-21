import {
  $,
  COLORS,
  axisDateParts,
  axisDateText,
  chartTime,
  cls,
  displayTime,
  el,
  fmt,
  humanCost,
  px,
  rub,
  signed,
} from './dom.js';
import { isIntraday, legPrice, legQuoteIsRuble, state } from './state.js';
import { NORM_HINTS, NORM_PANEL, applyNormHint, spreadDisplayValues } from './normalizations.js';

export { NORM_HINTS, NORM_PANEL, applyNormHint, spreadDisplayValues };

export function ledgerIndex() {
  const m = {};
  for (const row of state.payload.ledger) m[row.date] = row;
  return m;
}

function chartOpts() {
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

function makePane(parent, heightClass, title, tagline, legend, collapsible = true) {
  const head = el('div', 'panel-title');
  const left = el('span');
  const div = el('div', 'chart ' + heightClass);
  if (collapsible) {
    const initialExpanded = state.expandedPanes.has(heightClass);
    div.id = 'chart-' + heightClass;
    div.classList.toggle('hidden', !initialExpanded);
    const toggle = el('button', 'pane-toggle');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', String(initialExpanded));
    toggle.setAttribute('aria-controls', div.id);
    toggle.setAttribute('aria-label', `${initialExpanded ? 'Свернуть' : 'Развернуть'} ${title}`);
    const chevron = el('span', 'pane-chevron', '▸');
    chevron.setAttribute('aria-hidden', 'true');
    toggle.append(chevron, el('b', '', title));
    toggle.addEventListener('click', () => {
      const expanded = toggle.getAttribute('aria-expanded') !== 'true';
      const targetRange = expanded ? state.primarySpreadChart?.timeScale().getVisibleLogicalRange() : null;
      if (expanded) {
        state.expandedPanes.add(heightClass);
        div._revealing = true;
      } else state.expandedPanes.delete(heightClass);
      toggle.setAttribute('aria-expanded', String(expanded));
      toggle.setAttribute('aria-label', `${expanded ? 'Свернуть' : 'Развернуть'} ${title}`);
      div.classList.toggle('hidden', !expanded);
      if (expanded) {
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            try {
              if (targetRange) div._chart?.timeScale().setVisibleLogicalRange(targetRange);
              else div._chart?.timeScale().fitContent();
            } catch (_) {
              try {
                div._chart?.timeScale().fitContent();
              } catch (_err) {}
            }
            requestAnimationFrame(() => {
              div._revealing = false;
            });
          }),
        );
      }
    });
    left.append(toggle);
    if (tagline) left.append(el('span', 'tagline', '  ' + tagline));
  } else {
    left.append(el('b', '', title));
    if (tagline) left.append(el('span', 'tagline', '  ' + tagline));
  }
  head.append(left);
  if (legend?.length > 1) {
    const lg = el('span', 'legend');
    for (const [label, color] of legend) {
      const s = el('span');
      const i = el('i');
      i.style.cssText = `display:inline-block;width:14px;height:3px;border-radius:2px;background:${color};margin-right:5px;vertical-align:middle`;
      s.append(i, label);
      lg.append(s);
    }
    head.append(lg);
  }
  parent.append(head, div);
  return div;
}

function markValidationStart(chart, pane, day) {
  if (!day) return;
  const line = el('div', 'validation-line');
  const scale = chart.timeScale();
  line.hidden = true;
  line.setAttribute('aria-hidden', 'true');
  pane.append(line);
  const place = () => {
    if (!pane.isConnected) return;
    const x = scale.timeToCoordinate(chartTime(day));
    line.hidden = x == null || x < 0 || x >= scale.width();
    if (!line.hidden) {
      line.style.left = `${chart.priceScale('left').width() + x}px`;
      line.style.bottom = scale.height() + 'px';
    }
  };
  scale.subscribeVisibleLogicalRangeChange(place);
  scale.subscribeSizeChange(place);
  requestAnimationFrame(place);
}

function markerColor(t, sel) {
  if (sel) return '#ffffff';
  if (t.open_at_end) return COLORS.warn;
  return t.net_rub > 0 ? COLORS.win : t.net_rub < 0 ? COLORS.loss : '#d7e6f3';
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

export function tipHtml(s, l) {
  let h = `<b>${displayTime(s.date)}</b>${s.days_to_near_expiry != null ? ` <span class="k">· ${s.days_to_near_expiry}д до экспирации ближней</span>` : ''}<hr>`;
  h += `<span class="k">ближняя</span> ${legPrice(s.near)} <span class="k">(откр. ${legPrice(s.near_open)}, объём ${fmt(s.near_volume)})</span><br><span class="k">дальняя</span> ${legPrice(s.far)} <span class="k">(откр. ${legPrice(s.far_open)}, объём ${fmt(s.far_volume)})</span><br>`;
  h += `<span class="k">спред</span> <b>${s.spread_rel != null ? fmt(s.spread_rel, 2) + '%' : '—'}</b> <span class="k">(абс. ${legPrice(s.spread)}${s.spread_open_rel != null ? ` · откр. ${fmt(s.spread_open_rel, 2)}%` : ''}${s.spread_last_trade_rel != null ? ` · посл. ${fmt(s.spread_last_trade_rel, 2)}%` : ''})</span><br>`;
  const nmode = $('norm')?.value || 'raw';
  if (nmode !== 'raw') {
    const i = state.payload.series.indexOf(s);
    const v = spreadDisplayValues(state.payload.series)[i];
    const unit = { stoch20: '%', rz20: 'σ', ewma10: 'σ', mad20: 'σ', garch: 'σ', kalman: 'σ' }[nmode] || 'pp';
    h += `<span class="k">нормализован · ${NORM_PANEL[nmode] || nmode}</span> ${v == null ? '—' : fmt(v, 2)}${unit === 'pp' ? '%' : unit}<br>`;
  }
  h += `<span class="k">${isIntraday(s.timeframe) ? 'объём за бар' : 'объём за день'}</span> ближняя ${fmt(s.near_volume)} · дальняя ${fmt(s.far_volume)}${s.spread_volume != null ? ` · спред ${fmt(s.spread_volume)}${s.spread_trades != null ? ' за ' + fmt(s.spread_trades) + ' сделок' : ''}` : ''}`;
  if (s.buy_level_rel != null) {
    h += `<br><span class="k">Порог покупки / продажи</span> ${fmt(s.buy_level_rel, 3)}% / ${fmt(s.sell_level_rel, 3)}% · на закрытии сигнального бара`;
  }
  if (s.funding_rub != null) {
    h += `<br><span class="k">фандинг: ${s.funding_rub >= 0 ? 'начислено' : 'списано'}</span> ${signed(s.funding_rub)} ₽`;
  }
  if (s.signal_z != null) h += `<br><span class="k">сигнал z</span> ${fmt(s.signal_z, 2)}`;
  if (s.dev_pp != null) h += `<br><span class="k">имплицитная ставка − RUONIA</span> ${fmt(s.dev_pp, 2)} pp`;
  if (s.ruonia_rate_date) h += `<br><span class="k">RUONIA as-of</span> ${s.ruonia_rate_date} · ${fmt(s.ruonia_rate_pct, 2)}%`;
  if (l) {
    h += `<hr><span class="k">позиция</span> ${l.position > 0 ? 'лонг' : l.position < 0 ? 'шорт' : 'флэт'}${l.quantity ? ` · ${l.quantity} контр.` : ''} · <span class="k">${isIntraday(s.timeframe) ? 'результат бара' : 'результат дня'}</span> ${rub(l.mark_pnl_rub, 2)} · <span class="k">издержки</span> −${fmt(l.costs_rub, 2)} ₽${l.funding_rub != null ? ` · <span class="k">фондирование</span> ${signed(l.funding_rub)} ₽` : ''} · <span class="k">кумулятивно</span> <b class="${cls(l.cumulative_net_rub)}">${rub(l.cumulative_net_rub, 2)}</b>${state.drawdownByDate[s.date] != null ? ` · <span class="k">просадка</span> <b class="warn">−${fmt(-state.drawdownByDate[s.date], 2)} ₽</b>` : ''}`;
  }
  const dayEvents = state.payload.trades.flatMap(t =>
    [
      ['ENTRY', t.entry],
      ['EXIT', t.exit],
    ]
      .filter(([, leg]) => leg.date === s.date)
      .map(([kind]) => ({ kind, t })),
  );
  for (const { kind, t } of dayEvents) {
    const leg = kind === 'ENTRY' ? t.entry : t.exit;
    const fees = Object.entries(leg.costs || {})
      .filter(([, v]) => v)
      .map(([k, v]) => `${humanCost(k)} ${fmt(v, 2)} ₽`)
      .join(', ');
    h += `<hr><b>#${t.number} ${kind === 'ENTRY' ? 'ВХОД' : 'ВЫХОД'}</b> ${t.side === 'long_spread' ? 'покупка ближней / продажа дальней' : 'продажа ближней / покупка дальней'} · контрактов: ${t.quantity}${t.open_at_end && kind === 'EXIT' ? ' · принудительная отметка' : ''}<br><span class="k">исполнение спреда</span> <b>${legPrice(leg.spread_fill)}</b> · <span class="k">ноги</span> ${legPrice(leg.near_price)} / ${legPrice(leg.far_price)}<br><span class="k">${isIntraday(state.payload.selection?.timeframe) ? 'объём за бар' : 'объём за день'}</span> ближняя ${fmt(leg.near_volume)} · дальняя ${fmt(leg.far_volume)}${leg.spread_volume != null ? ` · спред ${fmt(leg.spread_volume)}` : ''}<br><span class="k">комиссии</span> ${fees || '—'}`;
    if (kind === 'EXIT') h += `<br><span class="k">результат</span> гросс ${rub(t.gross_rub, 2)} · нетто <b class="${cls(t.net_rub)}">${rub(t.net_rub, 2)}</b>`;
  }
  return h;
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
  const opts = chartOpts();
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
  const cNear = LWC.createChart(nearPane, legFrom());
  const cFar = LWC.createChart(farPane, legFrom());
  const cSpreadRaw = LWC.createChart(rawSpreadPane, legFrom());
  const cSpread = LWC.createChart(spreadPane, opts);
  const cVol = LWC.createChart(volPane, opts);
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

  const markers = buildMarkers();
  state.markersApis.push(LWC.createSeriesMarkers(rawSpreadSeries, markers), LWC.createSeriesMarkers(spreadSeries, markers));

  const seriesByTime = new Map(S.map((r, i) => [chartTime(r.date), { r, i }]));
  const onSpreadClick = param => {
    const day = param.time == null ? null : seriesByTime.get(param.time)?.r.date;
    if (!day) return;
    const hits = state.payload.trades.filter(t => t.entry.date === day || t.exit.date === day);
    if (!hits.length) return;
    if (state.dayClick.day === day) state.dayClick.index = (state.dayClick.index + 1) % hits.length;
    else state.dayClick = { day, index: 0 };
    state.selectedTrade = hits[state.dayClick.index].number;
    refreshSelection();
  };
  cSpreadRaw.subscribeClick(onSpreadClick);
  cSpread.subscribeClick(onSpreadClick);

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

  let syncing = false;
  state.charts.forEach(chart => {
    chart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (syncing || !range || chart._pane?.classList.contains('hidden') || chart._pane?._revealing) return;
      syncing = true;
      for (const other of state.charts) {
        if (other !== chart && !other._pane?.classList.contains('hidden') && !other._pane?._revealing) {
          try {
            other.timeScale().setVisibleLogicalRange(range);
          } catch (_) {}
        }
      }
      syncing = false;
    });
  });

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
      if (!day || !pt || pt.x < 0 || pt.y < 0) {
        if (activeHoverChart === chart) {
          activeHoverChart = null;
          tipDiv.style.display = 'none';
          clearCrosshairs(chart);
        }
        return;
      }
      const entry = seriesByTime.get(day);
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
        const v = otherMeta.value(day);
        try {
          other.setCrosshairPosition(v != null && isFinite(v) ? v : NaN, day, otherMeta.series);
        } catch (_) {}
      });
      syncingCrosshair = false;
      tipDiv.innerHTML = tipHtml(entry.r, L[entry.r.date]);
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
  root.append(tipDiv);
  return fitChartsToVisibleArea();
}
