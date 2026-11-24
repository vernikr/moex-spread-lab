/** Volume and capital gate indicators for historical window rows. */

import { el, fmt } from './dom.js';
import { state } from './state.js';

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

