import { ledgerIndex, refreshSelection } from './charts.js';
import { cellRow, dayVolumeCell, fundingCell, signalCell, spreadCell, td, th, toggleCard } from './ledger-cells.js';
import {
  $, FILL_RU, GROUP_RU,
  cls, displayTime, el, fmt, humanCost, humanReason, pct, pp, px, rub, signed, status,
} from './dom.js';
import { isIntraday, proofLabel, rowPeriod, state, strategyLabel, timeframeLabel } from './state.js';

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
    cellRow(tr, signalCell(r), { num: true });
    cellRow(tr, dayVolumeCell(r), { num: true });
    cellRow(tr, `${px(r.far)}<small>цена · объём ${r.far_volume != null ? fmt(r.far_volume) : '—'}</small>`, { num: true });
    if (showSpreadVolume) cellRow(tr, r.spread_volume != null ? fmt(r.spread_volume) : '—', { num: true });
    cellRow(tr, spreadCell(r), { num: true });
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
    cellRow(tr, rub(l.mark_pnl_rub, 2), { num: true, klass: cls(l.mark_pnl_rub) });
    cellRow(tr, fundingCell(r, l), { num: true, klass: cls(l.funding_rub) });
    cellRow(tr, l.costs_rub ? `−${fmt(l.costs_rub, 2)}` : '', { num: true });
    cellRow(tr, rub(l.cumulative_net_rub, 2), { num: true, klass: cls(l.cumulative_net_rub) });
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
  function fallback() {
    const ta = document.createElement('textarea');
    ta.value = v;
    document.body.append(ta);
    ta.select();
    try {
      document.execCommand('copy');
      done();
    } catch (_) {}
    ta.remove();
  }
  if (navigator.clipboard?.writeText) navigator.clipboard.writeText(v).then(done).catch(() => fallback());
  else fallback();
}

export { renderPeriodResult, renderSummary, toggleSummaryBlock } from './ledger-summary.js';
