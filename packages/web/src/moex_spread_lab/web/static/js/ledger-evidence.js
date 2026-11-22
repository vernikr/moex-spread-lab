import { $, FILL_RU, displayTime, el, fmt } from './dom.js';
import { state, strategyLabel, timeframeLabel } from './state.js';

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

