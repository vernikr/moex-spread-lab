/** Chart synchronisation: click-to-select trades, linked time ranges and linked crosshairs. */

import { state } from './state.js';
import { tipHtml } from './chart-tooltip.js';

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
