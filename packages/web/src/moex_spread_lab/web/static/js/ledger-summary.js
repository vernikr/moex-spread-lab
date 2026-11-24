/** Ledger summary block: period result, flags and tile summaries. */

import {
  $, GROUP_RU,
  cls, displayTime, el, fmt, pct, rub,
} from './dom.js';
import { isIntraday, state } from './state.js';

export function toggleSummaryBlock() {
  const button = $('toggle-summary');
  const block = $('summary-block');
  const visible = button.getAttribute('aria-expanded') !== 'true';
  button.setAttribute('aria-expanded', String(visible));
  button.textContent = visible ? 'Скрыть сводку' : 'Показать сводку';
  block.classList.toggle('hidden', !visible);
}

export function renderPeriodResult() {
  const e = state.payload.evaluation;
  const r = e.result;
  const p = e.periods;
  const n = e.observations;
  const elRoot = $('period-result');
  const check = e.result_basis === 'validation';
  elRoot.dataset.basis = e.result_basis;
  elRoot.hidden = false;
  const head = el('div', 'result-head');
  head.append(el('strong', '', check ? 'Результат на проверке' : 'Результат на обучении'));
  for (const [label, value] of [
    ['Нетто', r.net_rub],
    ['Сверх LQDT', r.excess_rub],
  ]) {
    const item = el('span', '', label + ' ');
    item.append(el('b', cls(value), rub(value, 2)));
    head.append(item);
  }
  const rate = el('span', '', `Годовых сверх LQDT: ${pp(r.annualized_excess_pp)}`);
  rate.title = `База — весь расчётный счёт: на старте ${fmt(e.initial_capital_rub, 2)} ₽ на окно, на проверке — стоимость счёта на конец обучения. Вычитается годовая доходность LQDT за те же даты.`;
  head.append(rate);
  const period = (label, dates, count) => `${label}: ${dates.map(displayTime).join(' — ')} (${count} набл.)`;
  elRoot.replaceChildren(
    head,
    el(
      'div',
      'result-meta',
      period('Обучение', p.training, n.training) +
        ' · ' +
        (check ? period(`Проверка ${pct(e.validation_percent)}`, p.validation, n.validation) : 'Проверка отключена (0%)'),
    ),
  );
  $('chart-scope').textContent = check ? 'Вся история · пунктир — начало проверки' : 'Вся история · обучение';
}

function tile(label, value, klass = '', sub = null) {
  const t = el('div', 'tile');
  t.append(el('span', '', label));
  t.append(el('b', klass, value));
  if (sub) t.append(el('small', '', sub));
  return t;
}

export function renderSummary() {
  const s = state.payload.summary;
  const w = state.payload.window;
  const f = $('flags');
  f.replaceChildren();
  const flag = (text, ok) => f.append(el('span', 'flag ' + (ok ? 'ok' : 'bad'), text));
  const cal = state.universe === 'calendar_native';
  const vol = cal ? (w.min_liquidity_volume ?? w.min_spread_volume) : w.min_leg_volume;
  const volLabel = cal ? w.liquidity_volume_label || 'объём календарного инструмента' : 'минимальный дневной объём ноги';
  const rule = cal ? (w.liquidity_gate ?? w.min_daily_volume_rule) : (w.min_daily_volume_rule ?? state.catalog.min_daily_volume);
  const unit = cal ? w.liquidity_gate_unit || 'контр./день' : 'контр./день';
  const volumeOk = cal ? (w.tradable_every_session ?? w.tradable_every_day) : w.tradable_every_day;
  const obs = w.observations ?? state.payload.series.length;
  flag(`Вся история: ${displayTime(w.from)} → ${displayTime(w.to)} · ${obs} ${isIntraday(w.timeframe) ? 'баров' : 'дней'}`, true);
  flag(
    volumeOk
      ? `Ликвидность пройдена: ${volLabel} ${fmt(vol)} ≥ ${rule} ${unit}`
      : `Ликвидность НЕ пройдена: ${volLabel} ${fmt(vol)} < ${rule} ${unit}`,
    volumeOk,
  );
  if (cal) flag(`Источник цены: ${w.price_source_label || 'календарный spread'}`, true);
  flag(
    w.passes_capital_screen
      ? `Скрининг капитала пройден (маржа ≤ ${fmt(w.max_pair_margin_rub)} ₽)`
      : `Скрининг капитала НЕ пройден (маржа до ${fmt(w.max_pair_margin_rub)} ₽ со стрессовыми буферами)`,
    w.passes_capital_screen,
  );
  flag(
    `Комиссия ${state.payload.contract.taker_rate_percent ?? '—'}% от стоимости контракта за ногу и сторону (${GROUP_RU[state.payload.contract.tariff_group] || state.payload.contract.tariff_group}); брокерская 0`,
    true,
  );
  if (s.skipped_entries) flag(`Пропущено входов по ограничениям счёта: ${s.skipped_entries}`, false);
  if (s.open_positions) flag(`${s.open_positions} открытая позиция в конце данных — отмечена как принудительный выход, не исполняемая сделка`, false);

  const t = $('tiles');
  t.replaceChildren();
  t.append(
    tile('Сделки', `${s.trades}`, '', `${s.closed_trades} закрыто · ${s.winners} в плюсе`),
    tile('Гросс P&L', rub(s.gross_rub), cls(s.gross_rub)),
    tile(
      'Издержки',
      `−${fmt(s.costs_rub, 2)} ₽`,
      '',
      `бирж ${fmt(s.cost_totals.exchange_clearing, 2)} · брок ${fmt(s.cost_totals.broker, 2)} · проск ${fmt(s.cost_totals.slippage, 2)}`,
    ),
    tile('Нетто P&L', rub(s.net_rub), cls(s.net_rub)),
    tile('LQDT на маржу', `−${fmt(s.lqdt_on_margin_rub)} ₽`),
    tile('Сверх LQDT', rub(s.excess_over_lqdt_rub), cls(s.excess_over_lqdt_rub)),
  );
  if (s.displayed_trade_net_rounding_delta_rub != null) {
    t.append(tile('Округление сделок', rub(s.displayed_trade_net_rounding_delta_rub, 2), '', 'сумма показанных сделок − нетто без промежуточного округления'));
  }
  if (s.max_excess_drawdown_rub != null) t.append(tile('Макс. изб. просадка', `−${fmt(s.max_excess_drawdown_rub, 2)} ₽`, 'warn'));
  const reconOk = Math.abs(s.ledger_reconciliation_rub) < 0.5;
  t.append(tile('Сверка журнала', rub(s.ledger_reconciliation_rub, 2), reconOk ? 'pos' : 'warn', 'нетто − кумулятив'));
  if (s.fx_point_value_drift_rub != null && Math.abs(s.fx_point_value_drift_rub) >= 0.005) {
    t.append(tile('Дрейф стоимости пункта', rub(s.fx_point_value_drift_rub, 2), 'warn'));
  }
}
