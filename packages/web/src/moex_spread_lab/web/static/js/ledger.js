import { ledgerIndex, refreshSelection } from './charts.js';
import {
  $, FILL_RU, GROUP_RU,
  cls, displayTime, el, fmt, humanCost, humanReason, pct, pp, px, rub, signed, status,
} from './dom.js';
import { isIntraday, proofLabel, rowPeriod, state, strategyLabel, timeframeLabel } from './state.js';

function td(tr, html, { num = false, klass = '' } = {}) {
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

function dayVolumeCell(r) {
  const nv = r.near_volume != null ? fmt(r.near_volume) : '—';
  return `${px(r.near)}<small>цена · объём ${nv}</small>`;
}

function spreadCell(r) {
  return `${r.spread_rel == null ? '—' : fmt(r.spread_rel, 2) + '%'}<small>откр. ${r.spread_open_rel == null ? '—' : fmt(r.spread_open_rel, 2) + '%'} · абс. ${px(r.spread)}</small>`;
}

function signalCell(r) {
  if (r.signal_z != null) return `z ${fmt(r.signal_z, 2)}`;
  if (r.dev_pp != null) return `${fmt(r.dev_pp, 2)} pp`;
  if ((r.funding_rub ?? 0) !== 0) return `фонд ${signed(r.funding_rub)}`;
  return '';
}

function fundingCell(r, l) {
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

function fillCard(t, side) {
  const isEntry = side === 'entry';
  const b = isEntry ? t.entry : t.exit;
  const g = el('div', 'cardgroup');
  const rows = `<h4>${isEntry ? 'Вход' : 'Выход'} #${t.number} — ${t.side === 'long_spread' ? 'лонг спред' : 'шорт спред'} · контрактов: ${t.quantity}</h4>` + (isEntry ? `
     <div><span class="k">ноги</span> ${t.legs}</div>
     <div><span class="k">сигнал</span> ${t.signal.kind} ${t.signal.value != null ? fmt(t.signal.value, 2) : ''}${t.signal.reason ? ` · ${humanReason(t.signal.reason)}` : ''} · ${displayTime(t.signal.date)}</div>` : '') + `
     <div><span class="k">дата</span> <b>${displayTime(b.date)}</b> · <span class="k">дней в позиции</span> ${t.days_held != null ? t.days_held : '—'}${!isEntry && t.open_at_end ? ' · <b class="warn">принудительная отметка в конце данных</b>' : ''}</div>
     <div><span class="k">исполнение спреда</span> <b>${px(b.spread_fill)}</b>${b.spread_fill_rel != null ? ` <span class="k">(${fmt(b.spread_fill_rel, 2)}%)</span>` : ''} · <span class="k">ноги ближняя / дальняя</span> <b>${px(b.near_price)} / ${px(b.far_price)}</b></div>
     <div><span class="k">${isIntraday(state.payload.selection?.timeframe) ? 'объём за бар' : 'объём за день'}</span> ближняя ${fmt(b.near_volume)} · дальняя ${fmt(b.far_volume)}${b.spread_volume != null ? ` · спред ${fmt(b.spread_volume)}` : ''}</div>` + (!isEntry ? `
     <div><span class="k">причина выхода</span> ${humanReason(t.exit_reason)}</div>
     <hr style="border:0;border-top:1px solid #233648;margin:6px 0">
     <div><span class="k">гросс</span> <b>${rub(t.gross_rub, 2)}</b>${t.gross_pts != null ? ` (${fmt(t.gross_pts, 3)} п.)` : ''}</div>
     <div><span class="k">нетто</span> <b class="${cls(t.net_rub)}">${rub(t.net_rub, 2)}</b> · <span class="${t.result === 'win' ? 'win-chip' : t.result === 'loss' ? 'loss-chip' : 'flat-chip'}">${t.result === 'win' ? 'ПРИБЫЛЬ' : t.result === 'loss' ? 'УБЫТОК' : 'В НОЛЬ'}</span></div>
     <div><span class="k">LQDT на маржу</span> −${fmt(t.lqdt_on_margin_rub, 2)} ₽ · <span class="k">сверх LQDT</span> <b class="${cls(t.excess_rub)}">${rub(t.excess_rub, 2)}</b></div>
     <div><span class="k">маржа</span> <b>${fmt(t.margin_rub)} ₽</b></div>` : '');
  const feeRows = Object.entries(b.costs || {})
    .map(([k, v]) => `<div><span class="k">${humanCost(k)}</span> ${fmt(v, 2)} ₽</div>`)
    .join('');
  const feeTotal = Object.values(b.costs || {}).reduce((a, c) => a + c, 0);
  g.innerHTML =
    rows +
    `<hr style="border:0;border-top:1px solid #233648;margin:6px 0"><h4 style="margin-top:0">Комиссии — эта сторона</h4>${feeRows}<div><span class="k">итого</span> <b>${fmt(feeTotal, 2)} ₽</b></div>`;
  return g;
}

function toggleCard(day, tr, ev) {
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

export function renderEvidence() {
  const ev = $('evidence');
  ev.classList.add('show');
  $('ev-runid').textContent = state.payload.run_id;
  $('ev-period').textContent = `Вся история: ${displayTime(state.payload.window.from)} → ${displayTime(state.payload.window.to)}`;
  const s = state.payload.selection;
  const inst = state.payload.instrument || {};
  const pairLabel = s.pair || inst.spread_instrument || '';
  $('ev-selection').textContent = `${state.universe === 'pair_screen' ? 'пара' : 'календарь'} ${pairLabel} · ${timeframeLabel(s.timeframe || '1d')} · ${strategyLabel(s.strategy)} · ${FILL_RU[s.fill] || s.fill || ''} · ${s.sizing === 'max_affordable' ? `размер: авто (${state.payload.summary.min_quantity}–${state.payload.summary.max_quantity} контр.)` : `контрактов: ${s.quantity}`} · проскальзывание ${s.slippage_ticks} т.`;
  $('ev-role').textContent = state.payload.price_role || '';
  const prov = state.payload.provenance || {};
  const provKeys = Object.keys(prov);
  const provEl = $('ev-provenance');
  provEl.textContent = provKeys.length ? `происхождение: ${provKeys.length} хэшей входных данных` : 'происхождение: не записано';
  provEl.title = provKeys.map(k => `${k}: ${String(prov[k])}`).join('\n');
  const reconOk = Math.abs(state.payload.summary.ledger_reconciliation_rub) < 0.5;
  $('ev-recon').innerHTML = '';
  const chip = el(
    'span',
    reconOk ? 'pos' : 'warn',
    reconOk
      ? `✓ сверяется (${fmt(state.payload.summary.ledger_reconciliation_rub, 2)} ₽)`
      : `⚠ расхождение ${fmt(state.payload.summary.ledger_reconciliation_rub, 2)} ₽`,
  );
  $('ev-recon').append(chip);
}

export function renderTable() {
  const root = $('ledger-table');
  const notes = $('notes');
  root.replaceChildren();
  const S = state.payload.series;
  const L = ledgerIndex();
  const events = {};
  for (const t of state.payload.trades) {
    (events[t.entry.date] ||= []).push({ t, side: 'entry' });
    (events[t.exit.date] ||= []).push({ t, side: 'exit' });
  }
  const showSpreadVolume = state.universe === 'calendar_native' && state.payload.window?.has_native_spread_volume !== false;
  const extra = showSpreadVolume ? 1 : 0;
  const headers = [
    'Дата',
    'Сигнал',
    'Ближняя · цена · объём',
    'Дальняя · цена · объём',
    ...(showSpreadVolume ? ['Объём спреда'] : []),
    'Спред % от дальней · откр.',
    'Событие',
    'P&L дня ₽',
    'Фондирование ₽',
    'Издержки ₽',
    'Кумулятивно ₽',
  ];
  const table = el('table');
  table.append(th(headers));
  const tbody = el('tbody');
  for (const r of S) {
    const l = L[r.date] || {};
    const ev = events[r.date];
    const clsRow = [];
    if (ev) {
      clsRow.push('fill');
      if (ev.some(e => e.side === 'entry')) clsRow.push('entry');
      if (ev.some(e => e.side === 'exit')) clsRow.push('exit');
      if (ev.some(e => e.t.number === state.selectedTrade)) clsRow.push('sel');
    }
    const boundary = r.date === state.payload.evaluation.periods.validation?.[0];
    if (boundary) clsRow.push('validation-start');
    const tr = el('tr', clsRow.join(' '));
    tr.dataset.period = rowPeriod(r);
    tr.dataset.dateKey = r.date;
    if (ev) tr.addEventListener('click', () => toggleCard(r.date, tr, ev));
    td(
      tr,
      `${displayTime(r.date)}${boundary ? '<small class="validation-tag">Начало проверки</small>' : ''}<small>${r.days_to_near_expiry != null ? r.days_to_near_expiry + 'д до экспирации ближней' : ''}</small>`,
    );
    td(tr, signalCell(r), { num: true });
    td(tr, dayVolumeCell(r), { num: true });
    td(tr, `${px(r.far)}<small>цена · объём ${r.far_volume != null ? fmt(r.far_volume) : '—'}</small>`, { num: true });
    if (showSpreadVolume) td(tr, r.spread_volume != null ? fmt(r.spread_volume) : '—', { num: true });
    td(tr, spreadCell(r), { num: true });
    td(
      tr,
      ev
        ? ev
            .map(
              e =>
                `<span class="badge ${e.side}${e.side === 'exit' && e.t.open_at_end ? ' open' : ''}" data-trade="${e.t.number}" title="клик — подсветка сделки на графике">${e.side === 'entry' ? 'ВХОД' : 'ВЫХОД'} #${e.t.number} · ${e.t.quantity} контр.</span>`,
            )
            .join(' ')
        : (state.payload.skipped_entries || [])
            .filter(x => x.date === r.date)
            .map(x => `<span class="warn">Пропуск: ${x.reason}</span>`)
            .join(' '),
      { num: false },
    );
    tr.querySelectorAll('.badge[data-trade]').forEach(b =>
      b.addEventListener('click', e => {
        e.stopPropagation();
        state.selectedTrade = Number(b.dataset.trade);
        refreshSelection();
      }),
    );
    td(tr, rub(l.mark_pnl_rub, 2), { num: true, klass: cls(l.mark_pnl_rub) });
    td(tr, fundingCell(r, l), { num: true, klass: cls(l.funding_rub) });
    td(tr, l.costs_rub ? `−${fmt(l.costs_rub, 2)}` : '', { num: true });
    td(tr, rub(l.cumulative_net_rub, 2), { num: true, klass: cls(l.cumulative_net_rub) });
    tbody.append(tr);
  }
  const s = state.payload.summary;
  const tot = el('tr', 'totals');
  td(tot, 'Итого · вся история', { klass: '' });
  for (let i = 0; i < 5 + extra; i++) td(tot, '');
  td(tot, rub(s.gross_rub, 2), { num: true, klass: cls(s.gross_rub) });
  const totalFunding = state.payload.ledger.reduce((sum, row) => sum + (row.funding_rub ?? 0), 0);
  td(tot, signed(totalFunding), { num: true, klass: cls(totalFunding) });
  td(tot, `−${fmt(s.costs_rub, 2)}`, { num: true });
  td(tot, rub(s.net_rub, 2), { num: true, klass: cls(s.net_rub) });
  tbody.append(tot);

  const recon = el('tr', 'recon ' + (Math.abs(s.ledger_reconciliation_rub) < 0.5 ? 'ok' : 'warn'));
  td(recon, 'Сверка', { klass: '' });
  const rctd = el('td', '');
  rctd.colSpan = 9 + extra;
  rctd.innerHTML = `нетто − кумулятив = <b>${rub(s.ledger_reconciliation_rub, 2)}</b>${s.fx_point_value_drift_rub != null ? ` &nbsp;·&nbsp; дрейф стоимости пункта FX ${rub(s.fx_point_value_drift_rub, 2)}` : ''} &nbsp;·&nbsp; издержки по сторонам: биржа + проскальзывание; брокер 0`;
  recon.append(rctd);
  tbody.append(recon);

  if (s.open_positions) {
    const nt = el('tr', 'note');
    const ntd = el('td', '');
    ntd.colSpan = 10 + extra;
    ntd.textContent = 'Есть принудительная отметка в конце данных — это не исполняемая сделка и не реализованный результат.';
    nt.append(ntd);
    tbody.append(nt);
  }
  table.append(tbody);
  root.append(table);
  root.append(notes);
}

function csvCell(v) {
  const s = String(v ?? '');
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

export function downloadCsv() {
  if (!state.payload) return;
  const L = ledgerIndex();
  const entryQty = Object.fromEntries(state.payload.trades.map(t => [t.entry.date, t.quantity]));
  const exitQty = Object.fromEntries(state.payload.trades.map(t => [t.exit.date, t.quantity]));
  const head = [
    'date',
    'period',
    'signal',
    'near',
    'near_open',
    'near_volume',
    'far',
    'far_open',
    'far_volume',
    'spread',
    'spread_open',
    'spread_last_trade',
    'spread_rel',
    'spread_open_rel',
    'spread_last_trade_rel',
    'buy_level',
    'sell_level',
    'buy_level_rel',
    'sell_level_rel',
    'spread_volume',
    'funding_rub',
    'funding_swaprate_rub_per_unit',
    'funding_lot_size',
    'funding_contracts',
    'funding_source',
    'funding_rate_kind',
    'funding_left_date',
    'funding_left_rate',
    'funding_right_date',
    'funding_right_rate',
    'position',
    'contracts_held',
    'entry_contracts',
    'exit_contracts',
    'event',
    'day_pnl_rub',
    'costs_rub',
    'cumulative_net_rub',
  ];
  const lines = [
    `# moex-spread-lab execution-proof ${state.payload.schema_version}`,
    `# run_id: ${state.payload.run_id}`,
    `# universe: ${state.universe}`,
    `# scope: full_history`,
    `# evaluation: ${JSON.stringify(state.payload.evaluation)}`,
    `# selection: ${JSON.stringify(state.payload.selection)}`,
    `# price_role: ${state.payload.price_role}`,
    `# provenance: ${JSON.stringify(state.payload.provenance)}`,
    `# notes: ${state.payload.notes.join(' | ')}`,
    head.join(','),
  ];
  for (const r of state.payload.series) {
    const l = L[r.date];
    lines.push(
      [
        r.date,
        rowPeriod(r),
        r.signal_z != null ? `z ${r.signal_z}` : r.dev_pp != null ? `pp ${r.dev_pp}` : '',
        r.near,
        r.near_open,
        r.near_volume,
        r.far,
        r.far_open,
        r.far_volume,
        r.spread,
        r.spread_open,
        r.spread_last_trade,
        r.spread_rel,
        r.spread_open_rel,
        r.spread_last_trade_rel,
        r.buy_level,
        r.sell_level,
        r.buy_level_rel,
        r.sell_level_rel,
        r.spread_volume,
        l?.funding_rub,
        r.funding_swaprate_rub_per_unit,
        r.funding_lot_size,
        r.funding_contracts,
        r.funding_source,
        r.funding_rate_kind ?? (r.funding_swaprate_rub_per_unit != null ? 'published' : ''),
        r.funding_left_date,
        r.funding_left_rate,
        r.funding_right_date,
        r.funding_right_rate,
        l?.position,
        l?.quantity,
        entryQty[r.date] ?? 0,
        exitQty[r.date] ?? 0,
        l?.event,
        l?.mark_pnl_rub,
        l?.costs_rub,
        l?.cumulative_net_rub,
      ]
        .map(csvCell)
        .join(','),
    );
  }
  const blob = new Blob([lines.join('\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `proof-${state.universe}-${proofLabel().replaceAll('/', '-')}-${state.payload.run_id}.csv`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

export function copyRunId() {
  const v = state.payload?.run_id;
  if (!v) return;
  const done = () => status('run_id скопирован в буфер обмена', true);
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(v).then(done).catch(() => {});
}

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
