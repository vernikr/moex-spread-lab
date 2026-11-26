import { px } from './dom.js';

const PARAM_RU = {
  lookback: 'Окно',
  window: 'Окно',
  entry_z: 'Вход z',
  exit_z: 'Выход z',
  threshold_pp: 'Порог RUONIA',
};

export const state = {
  catalog: null,
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
  for (const item of list) state.strategies.set(item.id, item);
}

export const getStrategyMeta = id => state.strategies.get(id) || null;
export const strategyFamily = id => getStrategyMeta(id)?.family_id || id;
export const familyMembers = familyId => (familyId ? [...state.strategies.values()].filter(m => m.family_id === familyId) : []);
export const familyVariant = (familyId, universe) => familyMembers(familyId).find(m => m.supported_universes?.includes(universe)) || null;

export function strategyOptions(universe) {
  const builtins = [];
  const plugins = [];
  for (const meta of state.strategies.values()) {
    if (meta.proof_enabled && meta.supported_universes?.includes(universe)) {
      (meta.is_builtin ? builtins : plugins).push(meta);
    }
  }
  // Alphabetical built-in order preserves default option selection before drop-in plugins.
  builtins.sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return [...builtins, ...plugins];
}

export const hasFamilyVariants = id => familyMembers(strategyFamily(id)).length > 1;

// Positional axis mapping across family variants (`lookback` <-> `window` in `ui_order`).
export function familyParameters(params, fromId, toId) {
  const from = getStrategyMeta(fromId);
  const to = getStrategyMeta(toId);
  if (!from || !to || from.family_id !== to.family_id || fromId === toId) return { ...params };
  const fromOrder = orderedStrategySpecs(fromId).map(s => s.name);
  const toOrder = orderedStrategySpecs(toId).map(s => s.name);
  if (fromOrder.length !== toOrder.length) return { ...params };
  return Object.fromEntries(
    toOrder.flatMap((name, i) => (Object.hasOwn(params, fromOrder[i]) ? [[name, params[fromOrder[i]]]] : [])),
  );
}

export function orderedStrategySpecs(strategyId, options = {}) {
  const meta = getStrategyMeta(strategyId);
  if (!meta) return [];
  const tf = options.timeframe || state.preferences.timeframe || '1d';
  const universe = options.universe || meta.supported_universes?.[0] || state.universe;
  const ctx = meta.context_parameters?.[universe]?.[tf];
  const specs =
    (universe === 'calendar_native' ? ctx?.[options.monthly ? 'monthly' : 'quarterly'] : ctx?.default) ||
    meta.parameters ||
    [];
  const byName = new Map(specs.map(s => [s.name, s]));
  const ordered = (meta.ui_order || [])
    .map(name => {
      const s = byName.get(name);
      byName.delete(name);
      return s;
    })
    .filter(Boolean);
  return [...ordered, ...byName.values()];
}

export const defaultParametersFor = (strategyId, options = {}) =>
  Object.fromEntries(orderedStrategySpecs(strategyId, options).map(s => [s.name, s.default]));

const timeframeOption = id => state.timeframeOptions.find(item => item.id === id);
export const isIntraday = id => !!timeframeOption(id)?.intraday;
export const supportsCalendar = id => (timeframeOption(id) ? timeframeOption(id).supports_calendar === true : id === '1d');
export const timeframeLabel = id => timeframeOption(id)?.label || id || '1 день';

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

export const targetKey = t => [t.universe, t.asset || '', t.pair].join('|');
export const currentAsset = () => state.branches.get(state.branchId)?.asset || null;
export const currentPair = () => state.leaves.get(state.leafId)?.target.pair || '';
export const pairAvailability = id => (state.catalog?.pair_screen?.pairs || []).find(p => p.id === id) || null;
export const timeframeSupported = meta => state.preferences.timeframe === '1d' || !meta.requires_daily_bars;

export function pairCatalogSlot(name) {
  const meta = getStrategyMeta(name);
  if (!meta) return name;
  return meta.is_builtin ? meta.id : meta.proof_adapter === 'carry_settlement_intents.v1' ? 'funding_only' : 'mean_reversion';
}

export function calendarStrategySupported(assetObj, name) {
  const meta = getStrategyMeta(name);
  if (!meta?.proof_enabled || !meta.supported_universes?.includes('calendar_native') || !timeframeSupported(meta)) return false;
  return Object.entries(assetObj || {})
    .filter(([, v]) => typeof v === 'boolean')
    .every(([flag, v]) => !String(meta.family_id).startsWith(flag) || v);
}

export function pairStrategyAvailable(p, name, proof = false) {
  const meta = getStrategyMeta(name);
  if (!meta || !timeframeSupported(meta)) return false;
  const useProof = proof || state.preferences.validation_percent || state.preferences.allow_failed_volume;
  return !!(useProof ? p?.proof_strategies : p?.strategies)?.[pairCatalogSlot(name)];
}

export function strategyFill(name) {
  const fills = getStrategyMeta(name)?.supported_fills || [];
  return fills.length === 1 && fills[0] === 'same_day_settlement' ? 'same_day_settlement' : state.preferences.price_fill;
}

export const strategyLabel = name => getStrategyMeta(name)?.label || name;
export const paramLabel = (strategyId, axisName) =>
  getStrategyMeta(strategyId)?.parameters?.find(p => p.name === axisName)?.label_ru || PARAM_RU[axisName] || axisName;

export function pairUnavailable(p, name) {
  const slot = pairCatalogSlot(name);
  return p?.proof_strategies?.[slot] ? 'не пройдена проверка на объём' : p?.data_reasons?.[slot] || 'нет локальных данных';
}

export const pairHasLocalData = p =>
  Object.values(p?.proof_strategies || {}).some(Boolean) || Object.values(p?.strategies || {}).some(Boolean);

export function legQuoteIsRuble() {
  if (state.universe === 'pair_screen') return true;
  const a = currentAsset();
  return a?.group === 'stock' || (a?.group === 'currency' && String(a.name || '').endsWith('/RUB'));
}

export const legPrice = v => (v == null ? '—' : px(v) + (legQuoteIsRuble() ? ' ₽' : ''));
export const proofLabel = () =>
  state.payload ? state.payload.selection.spread_instrument || state.payload.selection.pair || state.universe : '';

export function rowPeriod(r) {
  const v = state.payload.evaluation.periods.validation;
  return r.volume_eligible === false ? 'journal_only' : v && r.date >= v[0] ? 'validation' : 'training';
}
