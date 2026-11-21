export const $ = id => document.getElementById(id);

export const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text != null) n.textContent = text;
  return n;
};

export const fmt = (v, d = 0) =>
  v == null ? '—' : Number(v).toLocaleString('ru-RU', { minimumFractionDigits: d, maximumFractionDigits: d });

export const rub = (v, d = 0) => (v == null ? '—' : `${v > 0 ? '+' : ''}${fmt(v, d)} ₽`);
export const px = v => (v == null ? '—' : fmt(v, Math.abs(v) < 100 ? 4 : Math.abs(v) < 10000 ? 2 : 0));
export const rubPx = v => (v == null ? '—' : `${px(v)} ₽`);
export const cls = v => (v > 0 ? 'pos' : v < 0 ? 'neg' : '');
export const signed = v => (v == null ? '—' : `${v > 0 ? '+' : ''}${fmt(v, 2)}`);
export const pct = v => Number(v).toLocaleString('ru-RU', { maximumFractionDigits: 10 }) + '%';
export function pp(v) {
  return v == null ? 'нет оценки' : `${signed(v)} п.п./год`;
}

export const FILL_RU = {
  next_open: 'следующее открытие',
  settlement: 'расчётная цена',
  same_day_settlement: 'расчёт в тот же день',
  next_close: 'следующее закрытие',
  reference_same_bar: 'справочное закрытие',
};

export const FAMILY_RU = {
  perpetual_quarterly_same_asset: 'вечный/квартальный',
  dated_calendar_same_asset: 'датированный календарь',
};

export const CATSTATUS_RU = {
  local_inputs_available: 'локальные входы доступны',
  intake_required: 'нужна загрузка данных',
  excluded_thin_leg: 'объём ниже порога',
  proof_ready_thin_leg: 'данные есть, но нога тонкая',
};

export const GROUP_RU = {
  stock: 'акции',
  index: 'индекс',
  interest: 'ставки',
  currency: 'валюта',
  commodity: 'сырьё',
};

export const REASON_RU = {
  mean_reversion_exit: 'возврат к среднему',
  end_of_sample: 'конец выборки',
  end_of_window: 'конец окна',
  open_at_data_end_no_executable_exit: 'принудительная отметка в конце данных',
  perpetual_execution_window: 'окно исполнения перпетуала',
  basis_reached_trailing_median: 'базис достиг скользящей медианы',
  trailing_funding_non_positive: 'фондирование стало неположительным',
  expiry_buffer: 'буфер до экспирации',
  positive_trailing_funding: 'положительное фондирование',
  positive_trailing_funding_and_low_basis: 'фондирование положительно и базис низкий',
};

export const humanReason = r => (r ? REASON_RU[r] || String(r).replaceAll('_', ' ') : '');

export const COST_RU = {
  exchange_clearing: 'биржа и клиринг',
  broker: 'брокер',
  slippage: 'проскальзывание',
};

export const humanCost = k => COST_RU[k] || String(k).replaceAll('_', ' ');

export const PARAM_RU = {
  lookback: 'Окно',
  window: 'Окно',
  entry_z: 'Вход z',
  exit_z: 'Выход z',
  threshold_pp: 'Порог RUONIA',
};

export const COLORS = {
  near: '#6fb7ff',
  far: '#f3b36b',
  spread: '#70e1b9',
  vol: '#4d7fa6',
  volWin: '#2f8f6a',
  volLoss: '#a24d47',
  win: '#78e0b4',
  loss: '#f0928a',
  entry: '#d7e6f3',
  warn: '#e7bd82',
  fund: '#b39ddb',
};

export function status(text, ok = false) {
  $('status').textContent = text;
  $('status').className = 'status' + (ok ? ' ok' : '');
}

const PROGRESS_PRIORITY = ['search', 'chart', 'plateau', 'windows', 'boot'];
const progressChannels = new Map();
let progressTicker = null;
let progressHideTimer = null;

function currentProgressChannel() {
  for (const key of PROGRESS_PRIORITY) if (progressChannels.has(key)) return progressChannels.get(key);
  return null;
}

function computeChannelPct(ch) {
  if (!ch) return 0;
  if (ch.ceiling <= ch.floor || !ch.tauMs) return ch.floor;
  const elapsed = Math.max(0, performance.now() - ch.startedAt);
  const ease = 1 - Math.exp(-elapsed / ch.tauMs);
  return Math.min(ch.ceiling, Math.max(ch.visual, ch.floor + (ch.ceiling - ch.floor) * ease));
}

export function renderProgressUi() {
  const ch = currentProgressChannel();
  const barWrap = $('render-progress');
  const bar = $('render-progress-bar');
  const pill = $('progress-pill');
  if (!ch) {
    if (progressTicker) {
      clearInterval(progressTicker);
      progressTicker = null;
    }
    if (!progressHideTimer) {
      barWrap.classList.remove('active');
      barWrap.setAttribute('aria-hidden', 'true');
      bar.style.width = '0%';
      barWrap.setAttribute('aria-valuenow', '0');
      barWrap.setAttribute('aria-valuetext', 'Ожидание');
      pill.hidden = true;
      pill.textContent = '';
    }
    return;
  }
  clearTimeout(progressHideTimer);
  progressHideTimer = null;
  ch.visual = computeChannelPct(ch);
  const pctVal = Math.max(0, Math.min(100, Math.round(ch.visual)));
  barWrap.classList.add('active');
  barWrap.setAttribute('aria-hidden', 'false');
  bar.style.width = pctVal + '%';
  barWrap.setAttribute('aria-valuenow', String(pctVal));
  const text = ch.label ? `${ch.label} · ${pctVal}%` : `${pctVal}%`;
  barWrap.setAttribute('aria-valuetext', text);
  pill.hidden = false;
  pill.textContent = text;
}

function ensureProgressTicker() {
  if (!progressTicker) progressTicker = setInterval(renderProgressUi, 80);
}

export function startProgress(channel, label, floorPct = 2, ceilingPct = floorPct, tauMs = 0) {
  progressChannels.set(channel, {
    label,
    floor: floorPct,
    ceiling: Math.max(floorPct, ceilingPct),
    tauMs,
    startedAt: performance.now(),
    visual: floorPct,
  });
  if (ceilingPct > floorPct && tauMs > 0) ensureProgressTicker();
  renderProgressUi();
}

export function updateProgressChannel(channel, pctVal, label = '', ceilingPct = null, tauMs = 0) {
  const prev = progressChannels.get(channel);
  const now = performance.now();
  const floor = prev ? Math.max(prev.floor, prev.visual, pctVal) : pctVal;
  const ceil = ceilingPct != null ? Math.max(floor, ceilingPct) : floor;
  progressChannels.set(channel, {
    label: label || prev?.label || '',
    floor,
    ceiling: ceil,
    tauMs,
    startedAt: now,
    visual: floor,
  });
  if (ceil > floor && tauMs > 0) ensureProgressTicker();
  renderProgressUi();
}

export function finishProgress(channel) {
  const had = progressChannels.delete(channel);
  if (!had) return;
  if (!progressChannels.size) {
    const barWrap = $('render-progress');
    const bar = $('render-progress-bar');
    barWrap.classList.add('active');
    bar.style.width = '100%';
    barWrap.setAttribute('aria-valuenow', '100');
    clearTimeout(progressHideTimer);
    progressHideTimer = setTimeout(() => {
      progressHideTimer = null;
      renderProgressUi();
    }, 160);
  } else renderProgressUi();
}

export function setRenderProgress(value) {
  if (!value) finishProgress('chart');
}

export function setSearchProgress(value) {
  if (!value) finishProgress('search');
}

export function chartTime(value) {
  if (typeof value !== 'string' || !value.includes('T')) return value;
  const zoned = /(?:Z|[+-][0-9]{2}:[0-9]{2})$/i.test(value) ? value : value + '+03:00';
  const ms = Date.parse(zoned);
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : value;
}

const moscowDateFormatter = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
});
const moscowDateTimeFormatter = new Intl.DateTimeFormat('ru-RU', {
  timeZone: 'Europe/Moscow',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
});

export function displayTime(value) {
  const stamp = chartTime(value);
  return typeof stamp === 'number' ? moscowDateTimeFormatter.format(new Date(stamp * 1000)) : value;
}

export function axisDateParts(time) {
  if (typeof time === 'number') {
    const [day, month, year] = moscowDateFormatter.format(new Date(time * 1000)).split('.');
    return { day, month, year };
  }
  if (typeof time === 'string') {
    const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(time);
    if (m) return { year: m[1], month: m[2], day: m[3] };
  }
  if (time && typeof time === 'object' && time.year && time.month && time.day) {
    return {
      year: String(time.year),
      month: String(time.month).padStart(2, '0'),
      day: String(time.day).padStart(2, '0'),
    };
  }
  return null;
}

export function axisDateText(time) {
  const p = axisDateParts(time);
  return p ? p.day + '.' + p.month + '.' + p.year : null;
}

export function bindBackdropClose(dlg, onClose, guard = () => false) {
  dlg.addEventListener('click', e => {
    if (guard() || e.target !== dlg) return;
    const r = dlg.getBoundingClientRect();
    if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) onClose();
  });
}

export function priceFillOptions(sel) {
  const prev = sel.value;
  sel.replaceChildren();
  for (const [value, label] of [
    ['reference_same_bar', 'Справочное закрытие (тот же бар)'],
    ['next_open', 'Следующее открытие (прокси исполнения)'],
    ['next_close', 'Следующее закрытие'],
  ]) {
    const o = el('option', '', label);
    o.value = value;
    sel.append(o);
  }
  sel.value = [...sel.options].some(o => o.value === prev) ? prev : 'reference_same_bar';
}
