import { PARAM_RU, px, rubPx } from './dom.js';
import { FALLBACK_STRATEGIES } from './strategy-registry.js';


export const state = {
  catalog: null,
  strategies: new Map(FALLBACK_STRATEGIES.map(s => [s.id, s])),
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
  sharedMR: 'mean_reversion',
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

export function getStrategyMeta(id) {
  if (state.strategies.has(id)) return state.strategies.get(id);
  const catalogPlugin = (state.catalog?.pair_screen?.proof_plugins || []).find(p => p.id === id);
  if (catalogPlugin) {
    return {
      id: catalogPlugin.id,
      family_id: catalogPlugin.family_id,
      label: catalogPlugin.label,
      short_label: catalogPlugin.label,
      supported_universes: ['pair_screen'],
      supported_fills: catalogPlugin.supported_fills || [],
      requires_daily_bars: !!catalogPlugin.requires_daily_bars,
      timeframe_supported: catalogPlugin.timeframe_supported !== false,
      proof_adapter: catalogPlugin.adapter || null,
      proof_enabled: true,
      is_builtin: false,
      ui_order: (catalogPlugin.parameters || []).map(s => s.name),
      parameters: catalogPlugin.parameters || [],
      context_parameters: {},
    };
  }
  return null;
}

export const isMR = s => s === 'mean_reversion' || s === 'zscore_mean_reversion';

export function orderedStrategySpecs(strategyId, options = {}) {
  const meta = getStrategyMeta(strategyId);
  if (!meta) return [];
  const tf = options.timeframe || state.preferences.timeframe || '1d';
  const u = options.universe || (meta.supported_universes?.[0] ?? state.universe);
  const monthlyKey = options.monthly ? 'monthly' : 'quarterly';
  let rawSpecs = null;
  if (u === 'calendar_native') {
    rawSpecs = meta.context_parameters?.calendar_native?.[tf]?.[monthlyKey];
    if (!rawSpecs && meta.parameters?.length) {
      rawSpecs = meta.parameters.map(s => {
        if (s.name === 'window') {
          return {
            ...s,
            default: options.monthly ? 7 : 20,
            label_ru: tf === '1h' ? 'Окно · баров' : 'Окно · дней',
          };
        }
        return s;
      });
    }
  } else {
    rawSpecs = meta.context_parameters?.[u]?.[tf]?.default || meta.parameters;
  }
  const specs = rawSpecs || meta.parameters || [];
  const byName = new Map(specs.map(s => [s.name, s]));
  const order = meta.ui_order?.length ? meta.ui_order : specs.map(s => s.name);
  const result = [];
  for (const name of order) {
    if (byName.has(name)) {
      result.push(byName.get(name));
      byName.delete(name);
    }
  }
  for (const s of byName.values()) result.push(s);
  return result;
}

export function defaultParametersFor(strategyId, options = {}) {
  const specs = orderedStrategySpecs(strategyId, options);
  return Object.fromEntries(specs.map(s => [s.name, s.default]));
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

export function pairPlugin(name) {
  const fromCat = (state.catalog?.pair_screen?.proof_plugins || []).find(p => p.id === name);
  if (fromCat) return fromCat;
  const meta = getStrategyMeta(name);
  if (meta && !meta.is_builtin && meta.proof_enabled && meta.supported_universes?.includes('pair_screen')) {
    const tfOk = state.preferences.timeframe === '1d' || !meta.requires_daily_bars;
    return {
      id: meta.id,
      family_id: meta.family_id,
      label: meta.label,
      adapter: meta.proof_adapter,
      supported_fills: meta.supported_fills,
      requires_daily_bars: meta.requires_daily_bars,
      timeframe_supported: tfOk,
      parameters: meta.parameters,
    };
  }
  return undefined;
}

export function calendarProofPlugins() {
  const out = [];
  for (const meta of state.strategies.values()) {
    if (!meta.is_builtin && meta.proof_enabled && meta.supported_universes?.includes('calendar_native')) {
      const tfOk = state.preferences.timeframe === '1d' || !meta.requires_daily_bars;
      out.push({ ...meta, timeframe_supported: tfOk });
    }
  }
  return out;
}

export function calendarStrategySupported(assetObj, name) {
  if (name === 'zscore_mean_reversion') return true;
  if (name === 'convergence_to_ruonia') return !!assetObj?.convergence;
  const meta = getStrategyMeta(name);
  if (!meta || !meta.proof_enabled || !meta.supported_universes?.includes('calendar_native')) return false;
  return state.preferences.timeframe === '1d' || !meta.requires_daily_bars;
}

export function pairStrategyAvailable(p, name, proof = false) {
  const plugin = pairPlugin(name);
  const entries = proof ? p?.proof_strategies : researchStrategies(p);
  const base = plugin?.adapter === 'carry_settlement_intents.v1' ? 'funding_only' : 'mean_reversion';
  return plugin ? !!(plugin.timeframe_supported && entries?.[base]) : !!entries?.[name];
}

export function strategyFill(name) {
  const meta = getStrategyMeta(name);
  const plugin = pairPlugin(name);
  const fills = meta?.supported_fills || plugin?.supported_fills || [];
  return name === 'funding_only' || (fills.length === 1 && fills[0] === 'same_day_settlement')
    ? 'same_day_settlement'
    : state.preferences.price_fill;
}

export function strategyLabel(name) {
  return getStrategyMeta(name)?.label || pairPlugin(name)?.label || name;
}

export function paramLabel(strategyId, axisName) {
  if (PARAM_RU[axisName]) return PARAM_RU[axisName];
  const meta = getStrategyMeta(strategyId);
  return meta?.parameters?.find(p => p.name === axisName)?.label_ru || axisName;
}

export function pairUnavailable(p, name) {
  return p?.proof_strategies?.[name] ? 'не пройдена проверка на объём' : p?.data_reasons?.[name] || 'нет локальных данных';
}

export function pairIsAvailable(p) {
  return !!(p?.strategies?.mean_reversion || p?.strategies?.funding_only);
}

export function pairHasLocalData(p) {
  return !!(p?.proof_strategies?.mean_reversion || p?.proof_strategies?.funding_only || pairIsAvailable(p));
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
