import { $, FAMILY_RU, GROUP_RU, el } from './dom.js';
import { configureWindowTableCallbacks, recentWindows, renderWindowTable } from './window-table.js';
import { currentPair, pairHasLocalData, state, targetKey } from './state.js';
import {
  activeStrategyName,
  cancelSearch,
  clearGroup,
  fillCalendarStrategies,
  fillStrategy,
  mappedStrategy,
  refreshGroupProfile,
  restoreForm,
  syncChartScope,
  syncEditorScope,
  syncGroupStrategyOptions,
  tuneButton,
  tuneState,
} from './strategy-form.js';

export {
  cacheWindowProof,
  paintWindowReadings,
  refreshWindowReadings,
  stopWindowReadings,
} from './window-table.js';
export { windowGates } from './window-gates.js';

let pickerCallbacks = {
  scheduleShow: () => {},
  refreshGlobalSettings: async () => {},
};

export function configurePickerCallbacks(next) {
  pickerCallbacks = { ...pickerCallbacks, ...next };
  configureWindowTableCallbacks({
    openLeaf,
    refreshGlobalSettings: pickerCallbacks.refreshGlobalSettings,
    setChecks,
  });
}

function addLeaf(b, target, meta, enabled = true) {
  const key = targetKey(target);
  const period = meta.from && meta.to ? `${meta.from} — ${meta.to}` : 'Нет локальной истории';
  const leaf = { key, branch: b.key, target, meta, period, from: meta.from, to: meta.to, observations: meta.observations, enabled };
  state.leaves.set(key, leaf);
  b.children.push(leaf);
}

function checkboxChanged() {
  cancelSearch();
  clearGroup();
  tuneState('');
  syncEditorScope();
  refreshChecks();
  refreshGroupProfile();
  syncChartScope();
}

export function setChecks(entries, on) {
  for (const l of entries) {
    if (on) state.checked.add(l.key);
    else state.checked.delete(l.key);
  }
  checkboxChanged();
}

function initAll(id, entries) {
  const cb = $(id);
  cb._leaves = entries;
  cb.onchange = () => setChecks(entries(), cb.checked);
}

function pickerRow(text, entries, kind, open) {
  const row = el('div', 'picker-row ' + kind);
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.className = 'pick-check';
  cb._leaves = () => entries;
  cb.setAttribute('aria-label', 'Включить в подбор: ' + text);
  cb.addEventListener('change', () => setChecks(entries, cb.checked));
  const button = el('button', 'pick-name', text);
  button.type = 'button';
  button.title = text;
  if (kind === 'leaf' && !entries[0].enabled) row.classList.add('unavailable');
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

export function markOpened() {
  for (const row of document.querySelectorAll('.picker-row[data-branch],.picker-row[data-leaf]')) {
    const on = row.dataset.branch === state.branchId || row.dataset.leaf === state.leafId;
    row.classList.toggle('current', on);
    row.querySelector('button').setAttribute('aria-current', String(on));
  }
}

export function openLeaf(key, preferred = '', run = true) {
  state.leafId = key;
  const want = mappedStrategy(preferred || activeStrategyName());
  if (state.universe === 'pair_screen') fillStrategy(want);
  else fillCalendarStrategies(want);
  const branch = state.branches.get(state.branchId);
  if (branch) branch.lastPair = currentPair();
  restoreForm();
  markOpened();
  refreshChecks();
  if (run) pickerCallbacks.scheduleShow(0);
}

export function openBranch(key, wanted = '', strategy = '', run = true) {
  const b = state.branches.get(key);
  if (!b) return;
  const prior = state.universe === 'pair_screen' ? $('strategy').value : $('cstrategy').value;
  state.branchId = key;
  state.universe = b.kind;
  $('pair-controls').classList.toggle('hidden', state.universe !== 'pair_screen');
  $('calendar-controls').classList.toggle('hidden', state.universe !== 'calendar_native');
  $('window-label').textContent = state.universe === 'pair_screen' ? 'Исторические окна' : 'Календарные окна';
  const list = $('window-list');
  renderWindowTable(b);
  const sorted = recentWindows(b);
  const selected = sorted.find(l => l.target.pair === (wanted || b.lastPair)) || sorted.find(l => l.enabled) || sorted[0];
  openLeaf(selected?.key || '', strategy || prior, run);
  list.scrollTop = 0;
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
    const key = 'pair_screen|' + p.near;
    const b = state.branches.get(key) || create('pair_screen', p.near, FAMILY_RU[p.family] || p.family, p.near);
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
