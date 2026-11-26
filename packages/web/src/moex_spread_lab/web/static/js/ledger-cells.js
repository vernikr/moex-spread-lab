/** Table scaffolding and per-column cell renderers for the proof ledger view. */

import { cls, displayTime, el, fmt, humanCost, humanReason, pct, pp, px, rub, signed } from './dom.js';
import { isIntraday, state } from './state.js';

export function td(tr, html, { num = false, klass = '' } = {}) {
  const c = el('td', (num ? 'num ' : '') + klass);
  c.innerHTML = html;
  tr.append(c);
  return c;
}

export function th(labels) {
  const tr = el('tr');
  for (const l of labels) {
    const h = el('th');
    h.innerHTML = l;
    tr.append(h);
  }
  const thead = el('thead');
  thead.append(tr);
  return thead;
}

export function cellRow(tr, html, opts) {
  return td(tr, html, opts);
}

export function dayVolumeCell(r) {
  const nv = r.near_volume != null ? fmt(r.near_volume) : '—';
  return `${px(r.near)}<small>цена · объём ${nv}</small>`;
}

export function spreadCell(r) {
  return `${r.spread_rel == null ? '—' : fmt(r.spread_rel, 2) + '%'}<small>откр. ${r.spread_open_rel == null ? '—' : fmt(r.spread_open_rel, 2) + '%'} · абс. ${px(r.spread)}</small>`;
}

export function signalCell(r) {
  if (r.signal_z != null) return `z ${fmt(r.signal_z, 2)}`;
  if (r.dev_pp != null) return `${fmt(r.dev_pp, 2)} pp`;
  if ((r.funding_rub ?? 0) !== 0) return `фонд ${signed(r.funding_rub)}`;
  return '';
}

export function fundingCell(r, l) {
  if (r.funding_rate_kind === 'no_clearing') {
    return (
      'Нет платежа<small>не было отдельного клиринга</small>' +
      (r.benchmark_mark_date ? `<small>LQDT: последняя отметка за ${r.benchmark_mark_date}</small>` : '')
    );
  }
  if (l.funding_rub == null) return '—';
  const amount = Number(l.funding_rub);
  const rate = r.funding_swaprate_rub_per_unit;
  const detail = [];
  if (r.funding_rate_kind === 'midpoint') {
    detail.push(
      `Расчётная ставка ${signed(rate)} ₽/ед.: среднее за ${r.funding_left_date} (${signed(r.funding_left_rate)}) и ${r.funding_right_date} (${signed(r.funding_right_rate)})`,
    );
  } else if (r.funding_rate_kind === 'rechecked') detail.push('Подтверждено повторной выгрузкой MOEX');
  else if (rate === 0) detail.push('Опубликованная ставка: 0');
  if (amount !== 0 && rate) {
    detail.push(
      `−(${signed(-Math.sign(amount / rate))}) × SWAPRATE ${signed(rate)} ₽/ед. × лот ${fmt(r.funding_lot_size)} × ${fmt(r.funding_contracts)} контр.`,
    );
  }
  return (amount === 0 ? '0,00' : `${amount > 0 ? 'начислено' : 'списано'} ${signed(amount)}`) + detail.map(t => `<small>${t}</small>`).join('');
}

export function fillCard(t, side) {
  const isEntry = side === 'entry';
  const b = isEntry ? t.entry : t.exit;
  const g = el('div', 'cardgroup');
  const rows = isEntry
    ? `<h4>Вход #${t.number} — ${t.side === 'long_spread' ? 'лонг спред' : 'шорт спред'} · контрактов: ${t.quantity}</h4>
     <div><span class="k">ноги</span> ${t.legs}</div>
     <div><span class="k">сигнал</span> ${t.signal.kind} ${t.signal.value != null ? fmt(t.signal.value, 2) : ''}${t.signal.reason ? ` · ${humanReason(t.signal.reason)}` : ''} · ${displayTime(t.signal.date)}</div>
     <div><span class="k">дата</span> <b>${displayTime(b.date)}</b> · <span class="k">дней в позиции</span> ${t.days_held != null ? t.days_held : '—'}</div>
     <div><span class="k">исполнение спреда</span> <b>${px(b.spread_fill)}</b>${b.spread_fill_rel != null ? ` <span class="k">(${fmt(b.spread_fill_rel, 2)}%)</span>` : ''} · <span class="k">ноги ближняя / дальняя</span> <b>${px(b.near_price)} / ${px(b.far_price)}</b></div>
     <div><span class="k">${isIntraday(state.payload.selection?.timeframe) ? 'объём за бар' : 'объём за день'}</span> ближняя ${fmt(b.near_volume)} · дальняя ${fmt(b.far_volume)}${b.spread_volume != null ? ` · спред ${fmt(b.spread_volume)}` : ''}</div>`
    : `<h4>Выход #${t.number} — ${t.side === 'long_spread' ? 'лонг спред' : 'шорт спред'} · контрактов: ${t.quantity}</h4>
     <div><span class="k">дата</span> <b>${displayTime(b.date)}</b> · <span class="k">дней в позиции</span> ${t.days_held != null ? t.days_held : '—'}${t.open_at_end ? ' · <b class="warn">принудительная отметка в конце данных</b>' : ''}</div>
     <div><span class="k">исполнение спреда</span> <b>${px(b.spread_fill)}</b>${b.spread_fill_rel != null ? ` <span class="k">(${fmt(b.spread_fill_rel, 2)}%)</span>` : ''} · <span class="k">ноги ближняя / дальняя</span> <b>${px(b.near_price)} / ${px(b.far_price)}</b></div>
     <div><span class="k">${isIntraday(state.payload.selection?.timeframe) ? 'объём за бар' : 'объём за день'}</span> ближняя ${fmt(b.near_volume)} · дальняя ${fmt(b.far_volume)}${b.spread_volume != null ? ` · спред ${fmt(b.spread_volume)}` : ''}</div>
     <div><span class="k">причина выхода</span> ${humanReason(t.exit_reason)}</div>
     <hr style="border:0;border-top:1px solid #233648;margin:6px 0">
     <div><span class="k">гросс</span> <b>${rub(t.gross_rub, 2)}</b>${t.gross_pts != null ? ` (${fmt(t.gross_pts, 3)} п.)` : ''}</div>
     <div><span class="k">нетто</span> <b class="${cls(t.net_rub)}">${rub(t.net_rub, 2)}</b> · <span class="${t.result === 'win' ? 'win-chip' : t.result === 'loss' ? 'loss-chip' : 'flat-chip'}">${t.result === 'win' ? 'ПРИБЫЛЬ' : t.result === 'loss' ? 'УБЫТОК' : 'В НОЛЬ'}</span></div>
     <div><span class="k">LQDT на маржу</span> −${fmt(t.lqdt_on_margin_rub, 2)} ₽ · <span class="k">сверх LQDT</span> <b class="${cls(t.excess_rub)}">${rub(t.excess_rub, 2)}</b></div>
     <div><span class="k">маржа</span> <b>${fmt(t.margin_rub)} ₽</b></div>`;
  const feeRows = Object.entries(b.costs || {})
    .map(([k, v]) => `<div><span class="k">${humanCost(k)}</span> ${fmt(v, 2)} ₽</div>`)
    .join('');
  const feeTotal = Object.values(b.costs || {}).reduce((a, c) => a + c, 0);
  g.innerHTML =
    rows +
    `<hr style="border:0;border-top:1px solid #233648;margin:6px 0"><h4 style="margin-top:0">Комиссии — эта сторона</h4>${feeRows}<div><span class="k">итого</span> <b>${fmt(feeTotal, 2)} ₽</b></div>`;
  return g;
}

export function toggleCard(day, tr, ev) {
  const existing = tr.nextElementSibling;
  if (existing && existing.classList.contains('card')) {
    existing.remove();
    return;
  }
  const card = el('tr', 'card');
  const cell = el('td');
  cell.colSpan = 10 + (state.universe === 'calendar_native' && state.payload.window?.has_native_spread_volume !== false ? 1 : 0);
  card.append(cell);
  const wrap = el('div', 'cardgrid');
  for (const { t, side } of ev) wrap.append(fillCard(t, side));
  cell.append(wrap);
  tr.after(card);
}
