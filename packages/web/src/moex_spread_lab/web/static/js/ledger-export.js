/** Ledger export helpers: CSV download and run_id copy. */

import { ledgerIndex } from './charts.js';
import { $, status } from './dom.js';
import { proofLabel, rowPeriod, state } from './state.js';

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
