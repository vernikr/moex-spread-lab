export const $ = id => document.getElementById(id);

export const emit = (name, detail) => document.dispatchEvent(new CustomEvent(name, { detail }));

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
export const pp = v => (v == null ? 'нет оценки' : `${signed(v)} п.п./год`);

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

const REASON_RU = {
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

const COST_RU = {
  exchange_clearing: 'биржа и клиринг',
  broker: 'брокер',
  slippage: 'проскальзывание',
};

export const humanReason = r => (r ? REASON_RU[r] || String(r).replaceAll('_', ' ') : '');
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
  const n = $('status');
  n.textContent = text;
  n.className = 'status' + (ok ? ' ok' : '');
}

const PROGRESS_PRIORITY = ['search', 'chart', 'plateau', 'windows', 'boot'];
const progressChannels = new Map();
let progressTicker = null;
let progressHideTimer = null;

function renderProgressUi() {
  const ch = PROGRESS_PRIORITY.map(k => progressChannels.get(k)).find(Boolean);
  const barWrap = $('render-progress');
  const bar = $('render-progress-bar');
  const pill = $('progress-pill');
  if (!ch) {
    clearInterval(progressTicker);
    progressTicker = null;
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
  if (ch.ceiling > ch.floor && ch.tauMs) {
    const ease = 1 - Math.exp(-Math.max(0, performance.now() - ch.startedAt) / ch.tauMs);
    ch.visual = Math.min(ch.ceiling, Math.max(ch.visual, ch.floor + (ch.ceiling - ch.floor) * ease));
  } else {
    ch.visual = ch.floor;
  }
  const pctVal = Math.max(0, Math.min(100, Math.round(ch.visual)));
  const text = ch.label ? `${ch.label} · ${pctVal}%` : `${pctVal}%`;
  barWrap.classList.add('active');
  barWrap.setAttribute('aria-hidden', 'false');
  barWrap.setAttribute('aria-valuenow', String(pctVal));
  barWrap.setAttribute('aria-valuetext', text);
  bar.style.width = pctVal + '%';
  pill.hidden = false;
  pill.textContent = text;
}

function writeProgress(channel, label, floor, ceiling, tauMs) {
  const ceil = Math.max(floor, ceiling);
  progressChannels.set(channel, { label, floor, ceiling: ceil, tauMs, startedAt: performance.now(), visual: floor });
  if (ceil > floor && tauMs > 0 && !progressTicker) progressTicker = setInterval(renderProgressUi, 80);
  renderProgressUi();
}

export function startProgress(channel, label, floor = 2, ceiling = floor, tauMs = 0) {
  writeProgress(channel, label, floor, ceiling, tauMs);
}

export function updateProgressChannel(channel, pctVal, label = '', ceiling = null, tauMs = 0) {
  const prev = progressChannels.get(channel);
  const floor = prev ? Math.max(prev.floor, prev.visual, pctVal) : pctVal;
  writeProgress(channel, label || prev?.label || '', floor, ceiling ?? floor, tauMs);
}

export function finishProgress(channel) {
  if (!progressChannels.delete(channel)) return;
  if (progressChannels.size) {
    renderProgressUi();
    return;
  }
  const barWrap = $('render-progress');
  barWrap.classList.add('active');
  barWrap.setAttribute('aria-valuenow', '100');
  $('render-progress-bar').style.width = '100%';
  clearTimeout(progressHideTimer);
  progressHideTimer = setTimeout(() => {
    progressHideTimer = null;
    renderProgressUi();
  }, 160);
}

export function chartTime(value) {
  if (typeof value !== 'string' || !value.includes('T')) return value;
  const ms = Date.parse(/(?:Z|[+-]\d\d:\d\d)$/i.test(value) ? value : value + '+03:00');
  return Number.isFinite(ms) ? Math.floor(ms / 1000) : value;
}

const MOSCOW_DATE = { timeZone: 'Europe/Moscow', year: 'numeric', month: '2-digit', day: '2-digit' };
const moscowDateFormatter = new Intl.DateTimeFormat('ru-RU', MOSCOW_DATE);
const moscowDateTimeFormatter = new Intl.DateTimeFormat('ru-RU', { ...MOSCOW_DATE, hour: '2-digit', minute: '2-digit' });

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
    return m ? { year: m[1], month: m[2], day: m[3] } : null;
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
  return p ? `${p.day}.${p.month}.${p.year}` : null;
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
  sel.replaceChildren(
    ...[
      ['reference_same_bar', 'Справочное закрытие (тот же бар)'],
      ['next_open', 'Следующее открытие (прокси исполнения)'],
      ['next_close', 'Следующее закрытие'],
    ].map(([value, label]) => Object.assign(el('option', '', label), { value })),
  );
  sel.value = [...sel.options].some(o => o.value === prev) ? prev : 'reference_same_bar';
}

async function readJson(r) {
  const d = await r.json();
  if (!r.ok) throw Error(d.error || `Request failed (${r.status})`);
  return d;
}

export async function api(path, body, signal) {
  const r = await window.fetch(
    path,
    body ? { signal, method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined,
  );
  return readJson(r);
}

export async function apiStream(path, body, signal, onProgress) {
  const r = await window.fetch(path, {
    signal,
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Progress-Stream': '1' },
    body: JSON.stringify(body),
  });
  if (!r.ok || !(r.headers.get('Content-Type') || '').toLowerCase().includes('ndjson') || !r.body) {
    return readJson(r);
  }
  const reader = r.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  let result = null;
  for (;;) {
    const { value, done } = await reader.read();
    if (value || done) buf += dec.decode(value, { stream: !done });
    const lines = (done ? buf + '\n' : buf).split('\n');
    buf = lines.pop();
    for (const raw of lines) {
      const line = raw.trim();
      if (!line) continue;
      const msg = JSON.parse(line);
      if (msg.type === 'progress') onProgress?.(msg);
      else if (msg.type === 'error') throw Error(msg.error || 'Request failed');
      else if (msg.type === 'result') result = msg.data;
    }
    if (done) break;
  }
  if (result == null) throw Error('Пустой ответ сервера');
  return result;
}
