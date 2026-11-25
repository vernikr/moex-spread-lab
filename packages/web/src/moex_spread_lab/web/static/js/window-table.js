/** Historical window table, volume/capital gate indicators, and streamed /api/proof/windows readings. */

import { apiStream } from './api.js';
import {
  $,
  FILL_RU,
  cls,
  el,
  finishProgress,
  fmt,
  signed,
  startProgress,
  updateProgressChannel,
} from './dom.js';
import { settingsKey, state, strategyLabel, targetKey } from './state.js';
import { leafPlan, proofPlanKey } from './strategy-form.js';

function gateIndicator(ok, text) {
  const n = el('span', 'window-gate ' + (ok == null ? 'unknown' : ok ? 'pass' : 'fail'), ok == null ? '—' : ok ? '✓' : '×');
  n.dataset.state = ok == null ? 'unknown' : ok ? 'pass' : 'fail';
  n.title = text;
  n.setAttribute('role', 'img');
  n.setAttribute('aria-label', text);
  n.tabIndex = 0;
  return n;
}

export function windowGates(row, l) {
  const c = l.meta || {};
  const cal = l.target.universe === 'calendar_native';
  const volume = cal ? (c.min_liquidity_volume ?? c.min_spread_volume) : c.min_leg_daily_volume;
  const rule = cal
    ? (c.liquidity_gate ?? c.min_daily_volume_rule ?? state.catalog.min_daily_volume ?? 100)
    : (c.min_daily_volume_rule ?? state.catalog.min_daily_volume ?? 100);
  const unit = cal ? c.liquidity_gate_unit || 'контр./день' : 'контр./день';
  const label = cal ? c.liquidity_volume_label || 'объём календарного спреда' : 'минимальный дневной объём ног';
  const volumeOk = cal
    ? (c.tradable_every_session ?? c.tradable_every_day)
    : (c.tradable_every_day ?? (volume != null ? volume >= rule : null));
  const margin = c.max_pair_margin_rub;
  const capitalOk = margin != null ? 3 * margin + 5000 + 5000 <= state.preferences.initial_capital_rub : null;
  row
    .querySelector('[data-gate=volume]')
    .replaceChildren(
      gateIndicator(
        volumeOk,
        volume == null ? 'V: нет данных для проверки объёма.' : `V: ${volumeOk ? 'пройдена' : 'не пройдена'}. ${label}: ${fmt(volume)}; порог ${fmt(rule)} ${unit}.`,
      ),
    );
  row
    .querySelector('[data-gate=capital]')
    .replaceChildren(
      gateIndicator(
        capitalOk,
        capitalOk == null
          ? 'C: нет параметров для стресс-проверки капитала.'
          : `C: ${capitalOk ? 'пройдена' : 'не пройдена'}. ГО одного спреда ${margin == null ? 'неизвестно' : fmt(margin, 2) + ' ₽'}; стресс 3×ГО и рабочие буферы против ${fmt(state.preferences.initial_capital_rub, 2)} ₽. Это проверка одного спреда; допуск в настройках её не меняет.`,
      ),
    );
}

let windowCallbacks = {
  openLeaf: () => {},
  refreshGlobalSettings: async () => {},
  setChecks: () => {},
};

export function configureWindowTableCallbacks(next) {
  windowCallbacks = { ...windowCallbacks, ...next };
}

export function recentWindows(b) {
  return [...b.children].sort(
    (a, x) => String(x.to || '').localeCompare(String(a.to || '')) || String(x.from || '').localeCompare(String(a.from || '')) || a.key.localeCompare(x.key),
  );
}

function periodMonth(d) {
  return d ? d.slice(0, 7).replace('-', '.') : '';
}

function windowRow(l) {
  const row = el('tr', 'picker-row window-row' + (!l.enabled ? ' unavailable' : ''));
  row.dataset.leaf = l.key;
  row.dataset.from = l.from || '';
  row.dataset.to = l.to || '';
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'pick-check';
  cb._leaves = () => [l];
  cb.setAttribute('aria-label', 'Включить в подбор: ' + l.period);
  cb.addEventListener('change', () => windowCallbacks.setChecks([l], cb.checked));
  const check = el('td');
  check.append(cb);
  const cell = el('td', 'window-period');
  const button = el('button', 'pick-name');
  button.type = 'button';
  button.textContent = l.from && l.to ? `${periodMonth(l.from)}-${periodMonth(l.to)}` : 'Нет истории';
  const sourceHint = l.meta?.price_source_label ? ` · ${l.meta.price_source_label}` : '';
  button.title = (l.observations ? `${l.period} · ${l.observations} наблюдений` : l.period) + sourceHint;
  button.addEventListener('click', () => windowCallbacks.openLeaf(l.key));
  cell.append(button);
  if (!l.enabled) {
    const missing = el('span', 'window-download', '⇩');
    missing.dataset.icon = 'download';
    missing.title = 'Нужна загрузка локальных данных. ' + Object.values(l.meta?.data_reasons || {}).filter(Boolean).join(' ');
    missing.setAttribute('role', 'img');
    missing.setAttribute('aria-label', missing.title);
    cell.append(missing);
  }
  row.append(check, cell);
  for (const name of ['volume', 'capital']) {
    const gateTd = el('td', 'window-check');
    gateTd.dataset.gate = name;
    row.append(gateTd);
  }
  for (const name of ['trades', 'rate']) {
    const metricTd = el('td', 'window-number', '—');
    metricTd.dataset.metric = name;
    row.append(metricTd);
  }
  row.addEventListener('click', e => {
    if (e.target.closest('button,input') || e.target.closest('[data-gate]')) return;
    windowCallbacks.openLeaf(l.key);
  });
  windowGates(row, l);
  return row;
}

export function renderWindowTable(b) {
  const table = el('table', 'window-table');
  table.setAttribute('aria-label', 'Исторические окна и текущая симуляция');
  const head = el('thead');
  const tr = el('tr');
  for (const [label, title] of [
    ['', 'Отметить для совместного подбора'],
    ['Период', 'Период локальной истории, без тикеров'],
    ['V', 'Исходная проверка объёма'],
    ['C', 'Исходная стресс-проверка капитала'],
    ['Сд.', 'Число принятых входов во всей текущей симуляции; открытые позиции учитываются'],
    ['п.п./г', 'Годовая доходность сверх LQDT в процентных пунктах: проверка, если включена, иначе обучение'],
  ]) {
    const n = el('th', '', label);
    n.scope = 'col';
    n.title = title;
    tr.append(n);
  }
  head.append(tr);
  const body = el('tbody');
  for (const l of recentWindows(b)) body.append(windowRow(l));
  table.append(head, body);
  $('window-list').replaceChildren(table);
}

function windowReadingFromProof(p) {
  return {
    status: 'measured',
    selection: p.selection,
    run_id: p.run_id,
    trades: p.summary.trades,
    closed_trades: p.summary.closed_trades,
    open_positions: p.summary.open_positions,
    annualized_excess_pp: p.evaluation.result.annualized_excess_pp,
    evaluation: p.evaluation,
    window: p.window,
    execution_reference: p.execution_reference,
  };
}

export function cacheWindowProof(req, p) {
  state.windowReadings.set(proofPlanKey(req), windowReadingFromProof(p));
  paintWindowReadings();
}

export function paintWindowReadings() {
  for (const row of $('window-list').querySelectorAll('tr[data-leaf]')) {
    const l = state.leaves.get(row.dataset.leaf);
    if (!l) continue;
    const plan = leafPlan(l);
    const reading = state.windowReadings.get(proofPlanKey(plan.req));
    const count = row.querySelector('[data-metric=trades]');
    const rate = row.querySelector('[data-metric=rate]');
    row.dataset.reading = reading?.status || (!l.enabled ? 'missing' : 'pending');
    count.textContent = reading?.trades == null ? '—' : String(reading.trades);
    rate.textContent = reading?.annualized_excess_pp == null ? '—' : signed(reading.annualized_excess_pp);
    rate.className = 'window-number ' + (reading?.execution_reference ? 'warn' : cls(reading?.annualized_excess_pp));
    const e = reading?.evaluation;
    const period = e?.periods?.[e.result_basis];
    const reason = reading?.reason || e?.result?.reason || (!l.enabled ? 'Нужна загрузка локальных данных.' : 'Выполняется расчёт для этих параметров.');
    const cash = e?.account_checks;
    const cashNote = cash?.cash_shortfall_rows
      ? `Дефицит свободных средств: ${cash.cash_shortfall_rows} сесс.; впервые ${cash.first_cash_shortfall_date}; минимум ${fmt(cash.minimum_free_cash_rub, 2)} ₽. Входы могли пройти раньше, но годовая оценка такого счёта недопустима.`
      : '';
    const settings = `${strategyLabel(plan.req.strategy)} · ${FILL_RU[plan.req.fill] || plan.req.fill || 'справочное закрытие'} · ${JSON.stringify(plan.req.parameters)} · ${plan.common ? 'общие параметры' : 'параметры окна'} · капитал ${fmt(e?.initial_capital_rub ?? state.preferences.initial_capital_rub, 2)} ₽`;
    count.title = reading?.trades == null ? reason : `Вся симуляция: ${reading.trades} входов; ${reading.closed_trades} закрыто, ${reading.open_positions} открыто на конце. ${settings}`;
    rate.title = [
      reading?.status === 'measured' && reading.annualized_excess_pp == null ? 'Годовая оценка недоступна.' : '',
      cashNote,
      reading?.execution_reference ? 'Справочный same-bar результат, не исполнимая доходность.' : '',
      reading?.annualized_excess_pp == null ? reason : `${signed(reading.annualized_excess_pp)} п.п./год сверх LQDT · ${e.result_basis === 'validation' ? 'проверка' : 'обучение'}: ${period.join(' — ')}.`,
      e?.result?.reason || '',
      settings,
    ]
      .filter((text, i, items) => text && items.indexOf(text) === i)
      .join(' ');
    rate.tabIndex = 0;
    rate.setAttribute('aria-label', rate.title);
    row.dataset.rateState =
      reading?.annualized_excess_pp != null
        ? 'measured'
        : reading?.status === 'measured'
          ? 'unavailable'
          : reading?.status || (!l.enabled ? 'missing' : 'pending');
    row.dataset.runId = reading?.run_id || '';
    row.dataset.reference = String(!!reading?.execution_reference);
  }
}

export function stopWindowReadings() {
  clearTimeout(state.windowReadTimer);
  state.windowReadTimer = null;
  state.windowReadVersion++;
  state.windowReadController?.abort();
  state.windowReadController = null;
  finishProgress('windows');
  const wl = $('window-label');
  if (wl) wl.textContent = state.universe === 'pair_screen' ? 'Исторические окна' : 'Календарные окна';
}

export function refreshWindowReadings(delay = 220) {
  stopWindowReadings();
  paintWindowReadings();
  const version = state.windowReadVersion;
  const branch = state.branchId;
  state.windowReadTimer = setTimeout(async () => {
    const pending = (state.branches.get(branch)?.children || [])
      .filter(l => l.enabled)
      .map(l => ({ leaf: l, ...leafPlan(l) }))
      .filter(p => !state.windowReadings.has(proofPlanKey(p.req)));
    if (!pending.length) return;
    const controller = new AbortController();
    state.windowReadController = controller;
    const wanted = new Map(pending.map(p => [p.leaf.key, p]));
    const baseLabel = state.universe === 'pair_screen' ? 'Исторические окна' : 'Календарные окна';
    startProgress('windows', `Окна: 0/${pending.length}`, 4, 12, 600);
    $('window-label').textContent = `${baseLabel} · 0/${pending.length}`;
    try {
      const r = await apiStream('/api/proof/windows', { requests: pending.map(p => p.req) }, controller.signal, ev => {
        if (version !== state.windowReadVersion || branch !== state.branchId) return;
        const pctVal = Math.round(ev.percent || 0);
        updateProgressChannel('windows', ev.percent || 0, ev.label || `Окна: ${ev.done}/${ev.total}`);
        $('window-label').textContent = `${baseLabel} · ${ev.done}/${ev.total} (${pctVal}%)`;
      });
      if (version !== state.windowReadVersion || branch !== state.branchId) return;
      if (settingsKey(r) !== settingsKey()) {
        void windowCallbacks.refreshGlobalSettings(true);
        return;
      }
      for (const reading of r.rows) {
        const p = wanted.get(targetKey(reading.target));
        if (p) state.windowReadings.set(proofPlanKey(p.req), reading);
      }
      paintWindowReadings();
    } catch (e) {
      if (version === state.windowReadVersion) {
        for (const p of pending) state.windowReadings.set(proofPlanKey(p.req), { status: 'unavailable', reason: e.message });
        paintWindowReadings();
      }
    } finally {
      if (version === state.windowReadVersion) {
        state.windowReadController = null;
        finishProgress('windows');
        $('window-label').textContent = state.universe === 'pair_screen' ? 'Исторические окна' : 'Календарные окна';
      }
    }
  }, delay);
}

