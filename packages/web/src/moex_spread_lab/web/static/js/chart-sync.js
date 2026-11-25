/** Chart synchronisation: click-to-select trades, linked time ranges, linked crosshairs and their tooltip body. */

import { $, cls, displayTime, fmt, humanCost, rub, signed } from './dom.js';
import { NORM_PANEL, spreadDisplayValues } from './normalizations.js';
import { isIntraday, legPrice, state } from './state.js';

/**
 * Wire click-to-select on the given charts. `onSelectionChange` re-paints markers and
 * ledger rows; the caller supplies it so this module never imports the orchestrator.
 */
export function subscribeTradeClick(charts, seriesByTime, onSelectionChange) {
  const onSpreadClick = param => {
    const day = param.time == null ? null : seriesByTime.get(param.time)?.r.date;
    if (!day) return;
    const hits = state.payload.trades.filter(t => t.entry.date === day || t.exit.date === day);
    if (!hits.length) return;
    if (state.dayClick.day === day) state.dayClick.index = (state.dayClick.index + 1) % hits.length;
    else state.dayClick = { day, index: 0 };
    state.selectedTrade = hits[state.dayClick.index].number;
    onSelectionChange();
  };
  for (const chart of charts) chart.subscribeClick(onSpreadClick);
}

export function syncVisibleRanges(charts) {
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

export function bindCrosshairTooltips({ root, tipDiv, chartMeta, L, seriesByTime }) {
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
}

/* Crosshair tooltip body: one series point plus every entry/exit event on that date. */

const NORM_UNITS = { stoch20: '%', rz20: 'σ', ewma10: 'σ', mad20: 'σ', garch: 'σ', kalman: 'σ' };

function tipHtml(s, l) {
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
