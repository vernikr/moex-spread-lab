import { $, GROUP_RU, el, emit } from './dom.js';
import { refreshGroupProfile } from './plateau-lamps.js';
import { currentPair, pairHasLocalData, state, targetKey } from './state.js';
import {
  fillCalendarStrategies,
  fillStrategy,
  restoreForm,
  syncChartScope,
  syncEditorScope,
  syncGroupStrategyOptions,
} from './strategy-form.js';
import { activeStrategyName, cancelSearch, clearGroup, mappedStrategy, tuneButton, tuneState } from './strategy-plans.js';
import { recentWindows, renderWindowTable, windowHeading } from './window-table.js';

const FAMILY_RU = {
  perpetual_quarterly_same_asset: 'вечный/квартальный',
  dated_calendar_same_asset: 'датированный календарь',
};

function addLeaf(b, target, meta, enabled = true) {
  const key = targetKey(target);
  const period = meta.from && meta.to ? `${meta.from} — ${meta.to}` : 'Нет локальной истории';
  const leaf = { key, branch: b.key, target, meta, period, from: meta.from, to: meta.to, observations: meta.observations, enabled };
  state.leaves.set(key, leaf);
  b.children.push(leaf);
}

function setChecks(entries, on) {
  for (const l of entries) {
    if (on) state.checked.add(l.key);
    else state.checked.delete(l.key);
  }
  cancelSearch();
  clearGroup();
  tuneState('');
  syncEditorScope();
  refreshChecks();
  refreshGroupProfile();
  syncChartScope();
}

function initAll(id, entries) {
  const cb = $(id);
  cb._leaves = entries;
  cb.onchange = () => setChecks(entries(), cb.checked);
}

function pickerRow(text, entries, kind, open) {
  const row = el('div', 'picker-row ' + kind + (kind === 'leaf' && !entries[0].enabled ? ' unavailable' : ''));
  const cb = Object.assign(document.createElement('input'), { type: 'checkbox', className: 'pick-check', _leaves: () => entries });
  cb.setAttribute('aria-label', 'Включить в подбор: ' + text);
  cb.addEventListener('change', () => setChecks(entries, cb.checked));
  const button = Object.assign(el('button', 'pick-name', text), { type: 'button', title: text });
  button.addEventListener('click', open);
  row.addEventListener('click', e => {
    if (e.target === row) open();
  });
  row.append(cb, button);
  return row;
}

export function refreshChecks() {
  for (const cb of document.querySelectorAll('.pick-check')) {
    const list = cb._leaves?.() || [];
    const count = list.reduce((s, l) => s + Number(state.checked.has(l.key)), 0);
    cb.disabled = !list.length;
    cb.checked = !!list.length && count === list.length;
    cb.indeterminate = count > 0 && count < list.length;
    cb.setAttribute('aria-checked', cb.indeterminate ? 'mixed' : String(cb.checked));
  }
  tuneButton(state.tuning);
}

function openLeaf(key, preferred = '', run = true) {
  state.leafId = key;
  const want = mappedStrategy(preferred || activeStrategyName());
  if (state.universe === 'pair_screen') fillStrategy(want);
  else fillCalendarStrategies(want);
  const branch = state.branches.get(state.branchId);
  if (branch) branch.lastPair = currentPair();
  restoreForm();
  for (const row of document.querySelectorAll('.picker-row[data-branch],.picker-row[data-leaf]')) {
    const on = row.dataset.branch === state.branchId || row.dataset.leaf === state.leafId;
    row.classList.toggle('current', on);
    row.querySelector('button').setAttribute('aria-current', String(on));
  }
  refreshChecks();
  if (run) emit('moex:schedule', 0);
}

export function openBranch(key, wanted = '', strategy = '', run = true) {
  const b = state.branches.get(key);
  if (!b) return;
  const prior = activeStrategyName();
  state.branchId = key;
  state.universe = b.kind;
  $('pair-controls').classList.toggle('hidden', state.universe !== 'pair_screen');
  $('calendar-controls').classList.toggle('hidden', state.universe !== 'calendar_native');
  $('window-label').textContent = windowHeading();
  renderWindowTable(b, openLeaf, setChecks);
  const sorted = recentWindows(b);
  const selected = sorted.find(l => l.target.pair === (wanted || b.lastPair)) || sorted.find(l => l.enabled) || sorted[0];
  openLeaf(selected?.key || '', strategy || prior, run);
  $('window-list').scrollTop = 0;
}

export function buildPicker() {
  state.branches.clear();
  state.leaves.clear();
  syncGroupStrategyOptions();
  const create = (kind, id, family, title, asset = null) => {
    const b = { key: kind + '|' + id, kind, id, family, title, asset, children: [] };
    state.branches.set(b.key, b);
    return b;
  };
  for (const p of state.catalog.pair_screen?.pairs || []) {
    const b = state.branches.get('pair_screen|' + p.near) || create('pair_screen', p.near, FAMILY_RU[p.family] || p.family, p.near);
    addLeaf(b, { universe: 'pair_screen', pair: p.id }, p, pairHasLocalData(p));
  }
  for (const a of state.catalog.assets || []) {
    const b = create(
      'calendar_native',
      a.asset,
      'Календарные спреды',
      `${a.asset} · ${a.name} · ${GROUP_RU[a.group] || a.group}${a.monthly ? ' · месячный' : ''}`,
      a,
    );
    for (const w of [...a.windows].reverse()) addLeaf(b, { universe: 'calendar_native', asset: a.asset, pair: w.pair }, w);
  }
  const top = $('main-asset');
  top.replaceChildren();
  const groups = new Map();
  for (const b of state.branches.values()) {
    if (!groups.has(b.family)) groups.set(b.family, []);
    groups.get(b.family).push(b);
  }
  for (const [family, bs] of groups) {
    top.append(
      pickerRow(family, bs.flatMap(b => b.children), 'family', () =>
        openBranch((bs.find(b => b.children.some(l => l.enabled)) || bs[0]).key),
      ),
    );
    for (const b of bs) {
      const row = pickerRow(b.title, b.children, 'branch', () => openBranch(b.key));
      row.dataset.branch = b.key;
      top.append(row);
    }
  }
  initAll('all-assets', () => [...state.leaves.values()]);
  initAll('all-windows', () => state.branches.get(state.branchId)?.children || []);
}

const COLUMN_LAYOUT_KEY = 'moex-proof-layout-v1';
const RESIZABLE_COLUMNS = [
  { key: 'selection', cssVar: '--selection-width', handleId: 'resize-selection', min: 220, max: 560, initial: 296 },
  { key: 'norm', cssVar: '--norm-width', handleId: 'resize-norm', min: 190, max: 480, initial: 262 },
];
const desiredColumnWidths = { selection: 296, norm: 262 };
const columnWidths = { selection: 296, norm: 262 };
const clamp = (v, min, max) => Math.max(min, Math.min(max, v));

function applyColumnWidth(name, value, remember = true) {
  const col = RESIZABLE_COLUMNS.find(x => x.key === name);
  if (!col) return;
  const requested = clamp(Math.round(value), col.min, col.max);
  if (remember) desiredColumnWidths[name] = requested;
  const other = columnWidths[col.key === 'selection' ? 'norm' : 'selection'];
  const maximum = window.innerWidth <= 940 ? col.max : clamp($('layout').clientWidth - 336 - other, col.min, col.max);
  const width = clamp(requested, col.min, maximum);
  columnWidths[name] = width;
  $('layout').style.setProperty(col.cssVar, `${width}px`);
  const handle = $(col.handleId);
  if (handle) {
    handle.setAttribute('aria-valuemin', String(col.min));
    handle.setAttribute('aria-valuemax', String(maximum));
    handle.setAttribute('aria-valuenow', String(width));
    handle.setAttribute('aria-valuetext', `${width} пикселей`);
  }
}

function applyResponsiveWidths() {
  applyColumnWidth('norm', desiredColumnWidths.norm, false);
  applyColumnWidth('selection', desiredColumnWidths.selection, false);
}

function persistColumnWidths() {
  try {
    localStorage.setItem(COLUMN_LAYOUT_KEY, JSON.stringify(desiredColumnWidths));
  } catch (_) {}
}

export function initColumnResizers() {
  let saved = {};
  try {
    saved = JSON.parse(localStorage.getItem(COLUMN_LAYOUT_KEY) || '{}') || {};
  } catch (_) {}
  for (const col of RESIZABLE_COLUMNS) {
    const w = Number(saved[col.key]);
    columnWidths[col.key] = desiredColumnWidths[col.key] = clamp(
      Math.round(Number.isFinite(w) && saved[col.key] != null ? w : col.initial),
      col.min,
      col.max,
    );
  }
  applyResponsiveWidths();
  for (const col of RESIZABLE_COLUMNS) {
    const handle = $(col.handleId);
    if (!handle) continue;
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      const { pointerId, clientX: startX } = event;
      const startWidth = columnWidths[col.key];
      handle.classList.add('active');
      document.body.classList.add('is-resizing');
      try {
        handle.setPointerCapture(pointerId);
      } catch (_) {}
      const move = e => {
        if (e.pointerId === pointerId) applyColumnWidth(col.key, startWidth + e.clientX - startX);
      };
      const finish = e => {
        if (e.pointerId !== pointerId) return;
        if (e.type === 'pointerup') move(e);
        for (const t of ['pointermove', 'pointerup', 'pointercancel']) handle.removeEventListener(t, t === 'pointermove' ? move : finish);
        handle.classList.remove('active');
        document.body.classList.remove('is-resizing');
        persistColumnWidths();
        try {
          if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId);
        } catch (_) {}
      };
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', finish);
      handle.addEventListener('pointercancel', finish);
    });
    handle.addEventListener('keydown', event => {
      const next =
        event.key === 'ArrowLeft' || event.key === 'ArrowRight'
          ? columnWidths[col.key] + (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 40 : 10)
          : event.key === 'Home'
            ? col.min
            : event.key === 'End'
              ? col.max
              : null;
      if (next == null) return;
      event.preventDefault();
      applyColumnWidth(col.key, next);
      persistColumnWidths();
    });
  }
  window.addEventListener('resize', applyResponsiveWidths);
}
