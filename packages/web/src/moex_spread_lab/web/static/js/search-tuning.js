import { apiStream } from './api.js';
import {
  $,
  el,
  fmt,
  pp,
  setSearchProgress,
  signed,
  startProgress,
  status,
  updateProgressChannel,
} from './dom.js';
import { th } from './ledger-cells.js';
import { paramLabel, settingsKey, state, strategyLabel, targetKey } from './state.js';
import {
  activeStrategyName,
  applyToForm,
  cancelSearch,
  clearGroup,
  currentRequest,
  fillCalendarStrategies,
  fillStrategy,
  formKey,
  groupSpec,
  invalidForm,
  paintPlateaus,
  pointError,
  refreshGroupProfile,
  renderSharedEditor,
  sharedInputs,
  sharedRequest,
  showGroupReading,
  stopProfile,
  syncEditorScope,
  tuneButton,
  tuneState,
} from './strategy-form.js';

let searchCallbacks = {
  show: async () => {},
  invalidateProof: () => {},
  refreshGlobalSettings: async () => {},
  stopWindowReadings: () => {},
  refreshChecks: () => {},
};

export function configureSearchCallbacks(next) {
  searchCallbacks = { ...searchCallbacks, ...next };
}

export function adoptSearchProfile(result) {
  if (!state.sharedStrategy || !result.selected_parameters) return;
  if (result.selected_strategy && result.selected_strategy !== state.sharedStrategy) {
    state.sharedDrafts.set(state.sharedStrategy, sharedRequest());
    renderSharedEditor({
      strategy: result.selected_strategy,
      parameters: result.selected_parameters,
      fill: result.selected_fill,
    });
  } else {
    for (const n of sharedInputs()) {
      if (n.dataset.axis in result.selected_parameters) n.value = result.selected_parameters[n.dataset.axis];
    }
  }
  stopProfile();
  if (result.plateau && settingsKey(result.group) === settingsKey()) {
    state.profileKey = groupSpec().key;
    showGroupReading(result.plateau);
  } else refreshGroupProfile(0);
}

export function displayGroup(result) {
  const g = result.group;
  if (!g) return;
  const root = $('batch-result');
  root.hidden = false;
  root.replaceChildren();
  const label = g.result_basis === 'validation' ? 'проверка' : 'обучение';
  const heading = el(
    'summary',
    '',
    `Последний подбор${result.selected_strategy ? ' · ' + strategyLabel(result.selected_strategy) : ''}${g.execution_reference ? ' · справочно' : ''} · окон: ${g.member_count} · ${label}: ${pp(g.result_mean_pp)}`,
  );
  root.append(heading);
  if (g.execution_reference) {
    root.append(
      el(
        'div',
        'result-warning',
        'Справочный same-bar подбор: закрытие сигнального бара уже известно. Все оценки группы — ориентир, не проверка исполнимой доходности.',
      ),
    );
  }
  root.append(
    el(
      'div',
      'group-note',
      `Среднее на обучении: ${pp(g.training_mean_pp)}. Вес каждого окна одинаков. Это независимые расчёты, не доходность портфеля. График относится к открытому окну; при отметках светофор относится ко всей группе. Годовые ставки пересчитаны по фактическим датам, в том числе для неполного года.`,
    ),
  );
  if (result.strategy_search) {
    root.append(
      el(
        'div',
        'group-note',
        result.strategy_search.alternatives
          .map(
            a =>
              `${strategyLabel(a.strategy)}: ${a.trial_count} испытаний · ${a.training_best_pp == null ? 'нет допустимого результата' : pp(a.training_best_pp)}`,
          )
          .join(' · '),
      ),
    );
  }
  if (result.selected_parameters) {
    root.append(
      el(
        'div',
        'group-note',
        'Общие параметры · ' +
          Object.entries(result.selected_parameters)
            .map(([k, v]) => `${paramLabel(result.selected_strategy, k)}: ${fmt(v, Number.isInteger(v) ? 0 : 1)}`)
            .join(' · '),
      ),
    );
  }
  const table = el('table');
  table.append(th(['Окно', 'Обучение, п.п./год', 'Результат, п.п./год', 'Расчётный период']));
  for (const r of g.members) {
    const tr = el('tr');
    const e = r.evaluation;
    const range = e?.periods?.[e.result_basis];
    for (const text of [
      (r.target.universe === 'calendar_native' ? 'Календарь · ' : 'Пара · ') + r.target.pair,
      r.training_annualized_excess_pp == null ? '—' : signed(r.training_annualized_excess_pp),
      e?.result.annualized_excess_pp == null ? '—' : signed(e.result.annualized_excess_pp),
      range ? range.join(' — ') + (e.result.calendar_days ? ' · ' + e.result.calendar_days + ' кал. дн.' : '') : '—',
    ]) {
      tr.append(el('td', '', text));
    }
    table.append(tr);
  }
  root.append(table);
  if (result.plateau) {
    root.append(
      el(
        'div',
        'group-note',
        'Устойчивость на обучении: ' +
          Object.entries(result.plateau.fields)
            .map(([k, v]) => `${paramLabel(result.selected_strategy, k)}: ${v.score == null ? 'нет оценки' : fmt(v.score, 0) + '/100'}`)
            .join(' · ') +
          '. Не влияет на выбор максимума.',
      ),
    );
  }
}

function reviewExclusions(result) {
  const d = $('compatibility');
  const rows = $('compatibility-list');
  rows.replaceChildren();
  for (const x of result.excluded) rows.append(el('li', '', `${x.target.pair} — ${x.reason}`));
  $('compatibility-message').textContent =
    (result.strategy_search
      ? `Ни одна из сравниваемых стратегий не может учесть ${result.excluded.length} из ${state.checked.size} отмеченных окон. `
      : `Для стратегии «${strategyLabel(state.sharedStrategy || activeStrategyName())}» при этих условиях исполнения не подходят ${result.excluded.length} из ${state.checked.size} отмеченных окон. `) +
    (result.remaining_count ? `После снятия отметок в подборе останется ${result.remaining_count}.` : 'Совместимых окон не останется. Подбор не запустится.');
  $('compatibility-yes').textContent = result.remaining_count ? 'Снять отметки и продолжить' : 'Снять отметки';
  d.showModal();
  return new Promise(resolve => {
    state.resolveCompatibility = resolve;
  });
}

export async function startTuning() {
  const own = state.checked.size ? sharedRequest() : currentRequest();
  const request = { ...own, optimize_strategy: true, price_fill: state.preferences.price_fill };
  const validation = state.checked.size ? pointError(sharedInputs()) : invalidForm();
  if (validation) {
    status(validation);
    return;
  }
  if (!state.checked.size && !state.leafId) return;
  const explicit = state.checked.size > 0;
  if (state.tuning) return;
  searchCallbacks.stopWindowReadings();
  if (!explicit) searchCallbacks.invalidateProof();
  const id = ++state.searchVersion;
  const fresh = () => id === state.searchVersion;
  const controller = new AbortController();
  state.searchController = controller;
  state.searchUsesChecks = explicit;
  state.tuning = true;
  tuneButton(true, 2);
  setSearchProgress(true);
  startProgress('search', 'Подготовка к подбору Optuna…', 2, 10, 1500);
  status('');
  clearGroup();
  try {
    let result;
    while (fresh()) {
      const targets = [...state.checked].map(k => state.leaves.get(k).target);
      tuneState(`Optuna: подготовка окон (${explicit ? targets.length : 1}) · 2%`);
      result = await apiStream('/api/proof/optimize', { ...request, ...(explicit ? { targets } : {}) }, controller.signal, ev => {
        if (!fresh()) return;
        const pctVal = Math.max(0, Math.min(99, Math.round(ev.percent || 0)));
        const ceil = ev.stage === 'prepare' ? 11 : ev.stage === 'search' ? Math.min(88, ev.percent + 1) : 99;
        updateProgressChannel('search', ev.percent || 0, ev.label || 'Optuna…', ceil, ev.stage === 'prepare' ? 1500 : 0);
        tuneButton(true, pctVal);
        tuneState(`${ev.label || 'Optuna…'} · ${pctVal}%`);
      });
      if (!fresh()) return;
      if (settingsKey(result.group || result.evaluation || result) !== settingsKey()) {
        cancelSearch();
        void searchCallbacks.refreshGlobalSettings(true);
        tuneState('Настройки расчёта изменились. Запустите подбор заново.');
        return;
      }
      if (result.status !== 'selection_requires_confirmation') break;
      setSearchProgress(false);
      const ok = await reviewExclusions(result);
      if (!fresh()) return;
      if (!ok) {
        tuneState('Подбор отменён. Отметки не изменены.');
        return;
      }
      for (const x of result.excluded) state.checked.delete(targetKey(x.target));
      syncEditorScope();
      searchCallbacks.refreshChecks();
      refreshGroupProfile();
      if (!state.checked.size) {
        await searchCallbacks.show(true);
        if (fresh()) tuneState('Совместимых отмеченных окон нет. Подбор не запускался.');
        return;
      }
      setSearchProgress(true);
      startProgress('search', 'Повторная подготовка окон…', 2, 12, 1200);
    }
    if (!fresh()) return;
    if (result.selected_parameters) {
      for (const row of result.group.members) {
        if (!row.selection) continue;
        const k = targetKey(row.target);
        state.formStates.set(formKey(k, row.selection.strategy), row.selection);
        if (k === state.leafId) {
          if (state.universe === 'pair_screen') fillStrategy(row.selection.strategy);
          else fillCalendarStrategies(row.selection.strategy);
          applyToForm(row.selection);
        }
      }
    }
    adoptSearchProfile(result);
    updateProgressChannel('search', 99, 'Обновление графика и сводки…');
    await searchCallbacks.show(true);
    if (!fresh()) return;
    displayGroup(result);
    tuneState(result.selected_parameters ? '' : result.reason || 'Нет допустимого результата. Параметры сохранены.', false);
  } catch (e) {
    if (fresh()) tuneState(`Подбор недоступен: ${e.message}`);
  } finally {
    if (fresh()) {
      state.tuning = false;
      state.searchController = null;
      setSearchProgress(false);
      tuneButton();
      if (state.payload) paintPlateaus(state.payload.plateau);
    }
  }
}

export function bindSearchButton() {
  $('auto-tune').addEventListener('click', () => void startTuning());
}
