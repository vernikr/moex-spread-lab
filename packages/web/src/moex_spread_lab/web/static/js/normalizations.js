import { $ } from './dom.js';

export const NORM_HINTS = {
  raw: 'Сырой спред в % от ноционала дальней ноги. График показывает фактическое процентное расстояние — дрейф здесь означает смещение самого соотношения, а не автоматический неверный прайсинг.',
  expanding: 'Спред минус среднее с момента начала выборки. Самая длинная причинная память; хороший первый фильтр без модели режима. Остаток всё равно будет трендить, если «экономика пары» дрейфует.',
  rm20: 'Спред минус скользящее среднее за 20 наблюдений (~торговый месяц). Быстро центрируется, чувствителен к смене режима — но широкий 20-дневный ход может «притвориться нормой».',
  rm60: 'Спред минус скользящее среднее за 60 наблюдений (~квартал). Более устойчивый центр, медленнее адаптируется. Берите, если короткий шум не должен открывать сделки.',
  ema10: 'Спред минус экспоненциально взвешенное среднее (полураспад ≈ 10 набл.). Идея скользящего среднего без жёсткого края окна — свежие бары доминируют плавно, только прошлые данные.',
  ols20: 'Спред минус линейная регрессия по трейлинговым 20 точкам с оценкой в последней точке. Короткий детренд — убирает свежий локальный наклон, может переобучиться на двухнедельной «волне».',
  ols60: 'Спред минус линейная регрессия по трейлинговым 60 точкам с оценкой в последней точке. Самый дисциплинированный стабилизатор уровня на дрейфующих сериях (GLD/GLZ6), но съедает реальный структурный сдвиг как «тренд» — судите по маркерам сделок, не по качеству подгонки.',
  ou60: 'Спред минус равновесный уровень ОУ, подогнанный по трейлинговым 60 точкам (x[t] = a + φ·x[t−1]; остаток vs a/(1−φ)). Если окно не показывает возврата (|φ| ≥ 1) — откат на остаток vs среднее окна. φ и half-life шумные при n=60 — верьте форме, а не точной цифре half-life.',
  rz20: '(Спред − скользящее среднее за 20) / скользящая σ за 20. Безразмерные «σ от недавней нормы» — удобно сравнивать пары, но масштабирование σ прячет большой чистый дрейф, если вола растёт вместе с ним.',
  ewma10: '(Спред − EWMA-среднее) / EWMA-σ (полураспад ≈ 10 набл.). rz20 без жёсткого края окна — свежие бары доминируют; быстрее реагирует в кластерах волы.',
  mad20: '(Спред − медиана за 20) / (1.4826 × MAD). Устойчив к плохим тикам и гэпам — меньше фантомных ±3σ; немного приглушён к реальным всплескам волы.',
  garch: 'Стандартизированный остаток трейлинговой рекурсии GARCH(1,1) (α=0.10, β=0.85, ω→0). Отвечает на «необычно ли это с учётом кластеризации волы», не «необычно ли vs среднего»; грубая подгонка, без ковариат.',
  kalman: 'Инновация Калмана в единицах своей σ — local-level модель (уровень — случайное блуждание, фиксированные Q/R). Без окна вообще — адаптируется непрерывно через смены режима; ровно настолько хороша, насколько верны фиксированные Q/R.',
  stoch20: '(Спред − min окна)/(max окна − min окна) за 20 точек, в %. Ограничено 0–100 — «где внутри недавнего диапазона», читать проще всего; про σ и half-life не говорит ничего.',
};

export const NORM_PANEL = {
  raw: 'спред в % от дальней ноги',
  expanding: 'остаток к expanding-среднему · причинный',
  rm20: 'остаток к среднему · 20 набл. · причинный',
  rm60: 'остаток к среднему · 60 набл. · причинный',
  ema10: 'остаток к EMA · полураспад 10 · причинный',
  ols20: 'остаток к OLS · 20 набл. · причинный',
  ols60: 'остаток к OLS · 60 набл. · причинный',
  ou60: 'остаток ОУ от равновесия · 60 набл. · причинный',
  rz20: 'z-скор · среднее и σ за 20 · причинный',
  ewma10: 'z-скор · EWMA · полураспад 10 · причинный',
  mad20: 'робастный z · медиана/MAD · 20 · причинный',
  garch: 'GARCH(1,1) z · трейлинг-рекурсия · причинный',
  kalman: 'z инновации Калмана · локальный уровень · причинный',
  stoch20: 'stochastic · 20 набл. · 0–100 · причинный',
};

export function applyNormHint() {
  const h = $('norm-hint');
  const mode = $('norm')?.value || 'raw';
  if (h) h.textContent = NORM_HINTS[mode] || '';
}

export function spreadDisplayValues(S, modeOverride) {
  const raw = S.map(r => r.spread_rel);
  const mode = modeOverride || $('norm')?.value || 'raw';
  const n = raw.length;
  const out = new Array(n);
  const mean = a => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : 0);
  const std = a => {
    const m = mean(a);
    return Math.sqrt(mean(a.map(v => (v - m) * (v - m))));
  };
  const median = a => {
    if (!a.length) return 0;
    const b = [...a].sort((x, y) => x - y);
    const m = b.length >> 1;
    return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2;
  };
  const mad = a => {
    const m = median(a);
    return median(a.map(v => Math.abs(v - m)));
  };
  const olsFit = pts => {
    const k = pts.length;
    if (k === 1) return { a: pts[0][1], b: 0 };
    let sx = 0, sy = 0, sxy = 0, sxx = 0;
    for (const [x, y] of pts) {
      sx += x; sy += y; sxy += x * y; sxx += x * x;
    }
    const vx = k * sxx - sx * sx;
    const b = vx !== 0 ? (k * sxy - sx * sy) / vx : 0;
    return { a: (sy - b * sx) / k, b };
  };
  const emaState = (series, hl) => {
    const a = 1 - Math.pow(0.5, 1 / hl);
    let m = null, v = null;
    const ms = [], sds = [];
    for (const v0 of series) {
      if (v0 == null) {
        ms.push(m);
        sds.push(v == null ? null : Math.sqrt(v));
        continue;
      }
      if (m == null) {
        m = v0; v = 0; ms.push(m); sds.push(0);
        continue;
      }
      const d = v0 - m;
      m = m + a * d;
      v = (1 - a) * (v + a * d * d);
      ms.push(m);
      sds.push(Math.sqrt(v));
    }
    return { ms, sds };
  };
  const winStats = (W, i) => raw.slice(Math.max(0, i - W + 1), i + 1).filter(v => v != null);
  const ema = emaState(raw, 10);
  const garchZ = (() => {
    let sig2 = null;
    const z = new Array(n).fill(null);
    const d0 = raw[0] != null && raw[1] != null ? raw[1] - raw[0] : null;
    if (d0 != null && isFinite(d0)) sig2 = d0 * d0;
    for (let t = 1; t < n; t++) {
      const d = raw[t] != null && raw[t - 1] != null ? raw[t] - raw[t - 1] : null;
      if (d == null || !isFinite(d)) { z[t] = null; continue; }
      if (sig2 == null) sig2 = d * d;
      const prev = sig2;
      z[t] = prev > 0 ? d / Math.sqrt(prev) : 0;
      sig2 = 1e-10 + 0.1 * d * d + 0.85 * prev;
    }
    return z;
  })();
  const kalmanZ = (() => {
    let lvl = null, P = 1;
    const Q = 1e-4, R = 1e-2;
    const zs = new Array(n).fill(null);
    for (let i = 0; i < n; i++) {
      const v = raw[i];
      if (v == null) continue;
      if (lvl == null) { lvl = v; P = 1; zs[i] = 0; continue; }
      const Pp = P + Q;
      const innov = v - lvl;
      const S2 = Pp + R;
      const K = Pp / S2;
      lvl = lvl + K * innov;
      P = (1 - K) * Pp;
      zs[i] = Math.sqrt(S2) > 0 ? innov / Math.sqrt(S2) : 0;
    }
    return zs;
  })();
  for (let t = 0; t < n; t++) {
    const cur = raw[t];
    if (cur == null) { out[t] = null; continue; }
    if (mode === 'raw') { out[t] = cur; continue; }
    if (mode === 'expanding') {
      let s = 0, c = 0;
      for (let i = 0; i <= t; i++) if (raw[i] != null) { s += raw[i]; c++; }
      out[t] = cur - s / c;
      continue;
    }
    if (mode === 'rm20' || mode === 'rm60') {
      out[t] = cur - mean(winStats(mode === 'rm20' ? 20 : 60, t));
      continue;
    }
    if (mode === 'ema10') {
      const m = ema.ms[t];
      out[t] = m == null ? 0 : cur - m;
      continue;
    }
    if (mode === 'ols20' || mode === 'ols60') {
      const W = mode === 'ols20' ? 20 : 60;
      const from = Math.max(0, t - W + 1);
      const pts = [];
      for (let i = from; i <= t; i++) if (raw[i] != null) pts.push([i - from, raw[i]]);
      const k = pts.length;
      if (k < 3) { out[t] = cur - mean(pts.map(q => q[1])); continue; }
      const f = olsFit(pts);
      out[t] = cur - (f.a + f.b * (k - 1));
      continue;
    }
    if (mode === 'ou60') {
      const pts = winStats(60, t);
      const k = pts.length;
      if (k < 10) { out[t] = null; continue; }
      const m = mean(pts);
      let sx = 0, sy = 0, sxy = 0, sxx = 0;
      for (let i = 1; i < k; i++) {
        sx += pts[i - 1]; sy += pts[i]; sxy += pts[i - 1] * pts[i]; sxx += pts[i - 1] * pts[i - 1];
      }
      const cnt = k - 1;
      const vx = cnt * sxx - sx * sx;
      const phi = vx !== 0 ? (cnt * sxy - sx * sy) / vx : 0;
      const a = (sy - phi * sx) / cnt;
      if (!isFinite(phi) || Math.abs(phi) >= 0.999 || Math.abs(1 - phi) < 1e-6 || phi < 0) {
        out[t] = cur - m;
        continue;
      }
      out[t] = cur - a / (1 - phi);
      continue;
    }
    if (mode === 'rz20') {
      const a = winStats(20, t);
      const m = mean(a), sd = std(a);
      out[t] = sd > 0 ? (cur - m) / sd : 0;
      continue;
    }
    if (mode === 'ewma10') {
      const sd = ema.sds[t];
      out[t] = sd && sd > 0 ? (cur - ema.ms[t]) / sd : 0;
      continue;
    }
    if (mode === 'mad20') {
      const a = winStats(20, t);
      const med = median(a), sd = 1.4826 * mad(a);
      out[t] = sd > 0 ? (cur - med) / sd : 0;
      continue;
    }
    if (mode === 'garch') { out[t] = garchZ[t]; continue; }
    if (mode === 'kalman') { out[t] = kalmanZ[t]; continue; }
    if (mode === 'stoch20') {
      const a = winStats(20, t);
      if (a.length < 2) { out[t] = 50; continue; }
      const hi = Math.max(...a), lo = Math.min(...a);
      out[t] = hi > lo ? (100 * (cur - lo)) / (hi - lo) : 50;
      continue;
    }
    out[t] = cur;
  }
  return out;
}
