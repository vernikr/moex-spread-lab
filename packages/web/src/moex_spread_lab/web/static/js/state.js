import { PARAM_RU, px, rubPx } from './dom.js';


export const state = {
  catalog: null,
  /** Strategy schemas from `/api/strategies`; the only source for form fields. */
  strategies: new Map(),
  payload: null,
  selectedTrade: null,
  universe: 'pair_screen',
  dayClick: { day: null, index: 0 },
  markersApis: [],
  primarySpreadChart: null,
  drawdownByDate: {},
  autoRunTimer: null,
  analysisRequestId: 0,
  expandedPanes: new Set(['h-spread-raw', 'h-spread', 'h-fund', 'h-cum']),
  timeframeOptions: [
    { id: '1h', label: '1 час', intraday: true, supports_calendar: true },
    { id: '1d', label: '1 день', intraday: false, supports_calendar: true },
  ],
  selectedTimeframe: '1d',
  preferences: {
    validation_percent: 0,
    allow_failed_volume: true,
    allow_failed_capital: true,
    slippage_ticks: 1,
    initial_capital_rub: 30000,
    timeframe: '1d',
    price_fill: 'reference_same_bar',
    random_seed: 0,
  },
  settingRevision: 0,
  savingSettings: false,
  branches: new Map(),
  leaves: new Map(),
  checked: new Set(),
  formStates: new Map(),
  branchId: '',
  leafId: '',
  tuning: false,
  resolveCompatibility: null,
  windowReadings: new Map(),
  windowReadVersion: 0,
  windowReadTimer: null,
  windowReadController: null,
  sharedStrategy: null,
  /** Last variant chosen per multi-universe family (family id -> strategy id). */
  sharedVariants: new Map(),
  sharedDrafts: new Map(),
  profileKey: '',
  profileVersion: 0,
  profileTimer: null,
  profileController: null,
  chartKey: '',
  chartWithLamps: false,
  proofController: null,
  openLamp: null,
  searchController: null,
  searchVersion: 0,
  searchUsesChecks: false,
  charts: [],
  numericInputs: [],
};

export function installStrategyRegistry(registryPayload) {
  const list = registryPayload?.strategies;
  if (!Array.isArray(list) || !list.length) return;
  state.strategies.clear();
  for (const item of list) {
    state.strategies.set(item.id, item);
  }
}

/** Strategy metadata comes from the `/api/strategies` registry only. */
export function getStrategyMeta(id) {
  return state.strategies.get(id) || null;
}

export function strategyFamily(id) {
  return getStrategyMeta(id)?.family_id || id;
}

/** Every universe variant declared by one family (e.g. pair vs calendar MR). */
export function familyMembers(familyId) {
  if (!familyId) return [];
  return [...state.strategies.values()].filter(meta => meta.family_id === familyId);
}

export function familyVariant(familyId, universe) {
  return familyMembers(familyId).find(meta => meta.supported_universes?.includes(universe)) || null;
}

/**
 * Solo strategy options: built-in engines first in their historical alphabetical
 * order (this keeps the default selection unchanged), then drop-in plugins in
 * registry order.
 */
export function strategyOptions(universe) {
  const builtins = [];
  const plugins = [];
  for (const meta of state.strategies.values()) {
    if (!meta.proof_enabled || !meta.supported_universes?.includes(universe)) continue;
    (meta.is_builtin ? builtins : plugins).push(meta);
  }
  builtins.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return [...builtins, ...plugins];
}

export const hasFamilyVariants = id => familyMembers(strategyFamily(id)).length > 1;

/**
 * Translate parameters between two variants of one family by schema position.
 * Both variants publish the same axes in `ui_order` order under different names
 * (`lookback` vs `window`), so a positional map is the family alias.
 */
export function familyParameters(params, fromId, toId) {
  const from = getStrategyMeta(fromId);
  const to = getStrategyMeta(toId);
  if (!from || !to || from.family_id !== to.family_id || fromId === toId) return { ...params };
  const fromOrder = orderedStrategySpecs(fromId).map(spec => spec.name);
  const toOrder = orderedStrategySpecs(toId).map(spec => spec.name);
  if (fromOrder.length !== toOrder.length) return { ...params };
  const out = {};
  toOrder.forEach((name, index) => {
    const source = fromOrder[index];
    if (Object.hasOwn(params, source)) out[name] = params[source];
  });
  return out;
}

export function orderedStrategySpecs(strategyId, options = {}) {
  const meta = getStrategyMeta(strategyId);
  if (!meta) return [];
  const tf = options.timeframe || state.preferences.timeframe || '1d';
  const universe = options.universe || meta.supported_universes?.[0] || state.universe;
  const context = meta.context_parameters?.[universe]?.[tf];
  const specs =
    (universe === 'calendar_native' ? context?.[options.monthly ? 'monthly' : 'quarterly'] : context?.default) ||
    meta.parameters ||
    [];
  const byName = new Map(specs.map(spec => [spec.name, spec]));
  const order = meta.ui_order?.length ? meta.ui_order : specs.map(spec => spec.name);
  const result = [];
  for (const name of order) {
    const spec = byName.get(name);
    if (spec) {
      result.push(spec);
      byName.delete(name);
    }
  }
  for (const spec of byName.values()) result.push(spec);
  return result;
}

export function defaultParametersFor(strategyId, options = {}) {
  const specs = orderedStrategySpecs(strategyId, options);
  return Object.fromEntries(specs.map(spec => [spec.name, spec.default]));
}

export function timeframeOption(id) {
  return state.timeframeOptions.find(item => item.id === id);
}

export function isIntraday(id) {
  return !!timeframeOption(id)?.intraday;
}

export function supportsCalendar(id) {
  const item = timeframeOption(id);
  return item ? item.supports_calendar === true : id === '1d';
}

export function timeframeLabel(id) {
  return timeframeOption(id)?.label || id || '1 день';
}

export const settingsKey = (x = state.preferences) =>
  JSON.stringify([
    x.validation_percent,
    x.allow_failed_volume,
    x.allow_failed_capital,
    x.slippage_ticks,
    x.initial_capital_rub,
    x.timeframe,
    x.price_fill,
    x.random_seed,
  ]);

export const settingsEventKey = 'moex-proof-settings-updated';

export const targetKey = t => [t.universe, t.asset || '', t.pair].join('|');

export function selectedRoot() {
  const b = state.branches.get(state.branchId);
  return { kind: b?.kind, id: b?.id };
}

export function currentAsset() {
  return state.branches.get(state.branchId)?.asset || null;
}

export function currentPair() {
  return state.leaves.get(state.leafId)?.target.pair || '';
}

export function pairAvailability(id) {
  return (state.catalog?.pair_screen?.pairs || []).find(p => p.id === id) || null;
}

export function researchStrategies(p) {
  return state.preferences.validation_percent || state.preferences.allow_failed_volume
    ? p?.proof_strategies
    : p?.strategies;
}

/** A strategy runs on the selected timeframe unless it needs daily bars. */
export const timeframeSupported = meta => state.preferences.timeframe === '1d' || !meta.requires_daily_bars;

/**
 * Legacy `/api/proof/catalog` slot that records availability for a strategy.
 * Built-ins use their own id; a drop-in uses the slot of its proof adapter.
 */
export function pairCatalogSlot(name) {
  const meta = getStrategyMeta(name);
  if (!meta) return name;
  return meta.is_builtin ? meta.id : meta.proof_adapter === 'carry_settlement_intents.v1' ? 'funding_only' : 'mean_reversion';
}

export function calendarStrategySupported(assetObj, name) {
  const meta = getStrategyMeta(name);
  if (!meta?.proof_enabled || !meta.supported_universes?.includes('calendar_native')) return false;
  if (!timeframeSupported(meta)) return false;
  // Calendar assets publish boolean capability flags; a family requires every flag
  // its id starts with (e.g. `convergence` for `convergence_to_ruonia`).
  return Object.entries(assetObj || {})
    .filter(([, value]) => typeof value === 'boolean')
    .every(([flag, value]) => !String(meta.family_id).startsWith(flag) || value);
}

export function pairStrategyAvailable(p, name, proof = false) {
  const meta = getStrategyMeta(name);
  if (!meta || !timeframeSupported(meta)) return false;
  const entries = proof ? p?.proof_strategies : researchStrategies(p);
  return !!entries?.[pairCatalogSlot(name)];
}

export function strategyFill(name) {
  const fills = getStrategyMeta(name)?.supported_fills || [];
  return fills.length === 1 && fills[0] === 'same_day_settlement'
    ? 'same_day_settlement'
    : state.preferences.price_fill;
}

export function strategyLabel(name) {
  return getStrategyMeta(name)?.label || name;
}

export function paramLabel(strategyId, axisName) {
  const meta = getStrategyMeta(strategyId);
  return meta?.parameters?.find(p => p.name === axisName)?.label_ru || PARAM_RU[axisName] || axisName;
}

export function pairUnavailable(p, name) {
  const slot = pairCatalogSlot(name);
  return p?.proof_strategies?.[slot] ? 'не пройдена проверка на объём' : p?.data_reasons?.[slot] || 'нет локальных данных';
}

export function pairIsAvailable(p) {
  return Object.values(p?.strategies || {}).some(Boolean);
}

export function pairHasLocalData(p) {
  return Object.values(p?.proof_strategies || {}).some(Boolean) || pairIsAvailable(p);
}

export function legQuoteIsRuble() {
  if (state.universe === 'pair_screen') return true;
  const a = currentAsset();
  return a?.group === 'stock' || (a?.group === 'currency' && String(a.name || '').endsWith('/RUB'));
}

export const legPrice = v => (legQuoteIsRuble() ? rubPx(v) : px(v));

export const proofLabel = () =>
  state.payload ? state.payload.selection.spread_instrument || state.payload.selection.pair || state.universe : '';

export function rowPeriod(r) {
  const v = state.payload.evaluation.periods.validation;
  return r.volume_eligible === false ? 'journal_only' : v && r.date >= v[0] ? 'validation' : 'training';
}
