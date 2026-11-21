import { apiStream } from './api.js';
import {
  $,
  el,
  finishProgress,
  fmt,
  pct,
  signed,
  startProgress,
  updateProgressChannel,
} from './dom.js';
import { settingsKey, state } from './state.js';

export function hidePlateauTip() {
  if (state.openLamp) {
    state.openLamp.removeAttribute('aria-describedby');
    state.openLamp = null;
  }
  $('plateau-tip').hidden = true;
}

export function showPlateauTip(lamp) {
  hidePlateauTip();
  state.openLamp = lamp;
  const tip = $('plateau-tip');
  tip.replaceChildren(el('strong', '', lamp.dataset.heading), document.createTextNode(lamp.dataset.detail || 'Нет оценки.'));
  tip.hidden = false;
  lamp.setAttribute('aria-describedby', 'plateau-tip');
  const r = lamp.getBoundingClientRect();
  const w = tip.offsetWidth;
  const h = tip.offsetHeight;
  tip.style.left = `${Math.max(8, Math.min(innerWidth - w - 8, r.right - w / 2))}px`;
  tip.style.top = `${Math.max(8, r.bottom + h + 12 > innerHeight ? r.top - h - 5 : r.bottom + 5)}px`;
}

export function drawLamps(inputs, plateau, empty, group) {
  for (const input of inputs) {
    const k = input.dataset.axis;
    const field = !input.closest('.hidden') ? plateau?.fields?.[k] : null;
    const lamp = input.nextElementSibling;
    const score = field?.score;
    const level = score == null ? 'unknown' : score >= plateau.green ? 'green' : score >= plateau.amber ? 'amber' : 'red';
    const name = input.labels[0].textContent;
    const verdict = { green: 'устойчиво', amber: 'на краю плато', red: 'неустойчиво', unknown: 'нет оценки' }[level];
    lamp.dataset.level = level;
    lamp.dataset.score = score ?? '';
    lamp.dataset.heading = `${name} · ${score == null ? 'нет оценки' : fmt(score, 2) + ' / 100 · ' + verdict}`;
    const lines = group
      ? [
          `Отмеченные окна: ${group.count} · равные веса. Обучение: свой период для каждого окна.`,
          group.percent ? `Проверка ${pct(group.percent)} не входит в светофор.` : 'Проверка отключена (0%).',
        ]
      : [];
    if (group ? group.reference : state.payload?.execution_reference) {
      lines.push('Справочный same-bar расчёт, не проверка исполнимой доходности.');
    }
    if (field) {
      lines.push(`Текущее значение: ${fmt(field.value, Number.isInteger(field.value) ? 0 : 1)}`);
      const e = !group && state.payload?.evaluation;
      const reportReason = e?.result?.reason;
      if (e) {
        lines.push(
          `Обучение: ${e.periods.training.join(' — ')}`,
          e.periods.validation ? `Проверка: ${e.periods.validation.join(' — ')}` : 'Проверка отключена (0%).',
        );
        if (reportReason) lines.push(reportReason);
      }
      if (field.reason && field.reason !== reportReason) lines.push(`На обучении: ${field.reason}`);
      if (field.center?.utility != null) {
        lines.push(`${group ? 'Среднее в центре' : 'Центр на обучении'}: ${signed(field.center.utility)} п.п./год сверх LQDT.`);
      }
      if (field.neighbors.length) {
        lines.push(group ? 'Средние у соседей на обучении:' : 'Соседи на обучении, остальные параметры неизменны:');
        for (const n of field.neighbors) {
          lines.push(
            `${fmt(n.value, Number.isInteger(n.value) ? 0 : 1)} → ${n.utility == null ? 'нет оценки: ' + n.reason : signed(n.utility) + ' п.п./год'}`,
          );
        }
      }
      if (!field.reason && score != null) lines.push('Балл — доля результата центра, сохранённая худшим соседом (0–100).');
    } else lines.push(empty);
    lamp.dataset.detail = lines.join('\n');
    lamp.setAttribute('aria-label', lamp.dataset.heading);
    if (lamp === state.openLamp) showPlateauTip(lamp);
  }
}

export function bindLamp(input) {
  const shell = input.parentElement;
  const lamp = input.nextElementSibling;
  shell.addEventListener('pointerenter', () => showPlateauTip(lamp));
  shell.addEventListener('pointerleave', hidePlateauTip);
  shell.addEventListener('focusin', () => showPlateauTip(lamp));
  shell.addEventListener('focusout', hidePlateauTip);
  lamp.addEventListener('click', () => showPlateauTip(lamp));
}

export function paintGroupLamps(inputs, isReference, plateau, message, st) {
  if (!state.sharedStrategy || !state.checked.size) return;
  const n = $('group-profile-state');
  n.dataset.state = st;
  n.textContent = message;
  n.title = message;
  n.setAttribute('aria-busy', String(st === 'loading'));
  drawLamps(inputs, plateau, message, {
    count: state.checked.size,
    percent: state.preferences.validation_percent,
    reference: isReference,
  });
}

export function showGroupLampsReading(inputs, isReference, plateau) {
  const fields = Object.values(plateau.fields);
  const any = fields.some(f => f.score != null);
  const reason = fields.find(f => f.reason)?.reason;
  paintGroupLamps(
    inputs,
    isReference,
    plateau,
    any ? 'Светофор: по средней доходности на обучении.' : `Нет групповой оценки. ${reason || ''}`,
    any ? 'ready' : 'unavailable',
  );
}

export function stopProfile() {
  clearTimeout(state.profileTimer);
  state.profileTimer = null;
  state.profileVersion++;
  state.profileController?.abort();
  state.profileController = null;
  state.profileKey = '';
  finishProgress('plateau');
}

export function runGroupProfileStream({ body, key, error, getInputs, isReference, onStaleSettings, delay = 140 }) {
  if (!state.sharedStrategy || !state.checked.size) return;
  if (state.profileKey === key) return;
  stopProfile();
  state.profileKey = key;
  const version = state.profileVersion;
  if (error) {
    paintGroupLamps(getInputs(), isReference(), null, error, 'invalid');
    return;
  }
  paintGroupLamps(getInputs(), isReference(), null, 'Светофор группы: подготовка 0%…', 'loading');
  state.profileTimer = setTimeout(async () => {
    const controller = new AbortController();
    state.profileController = controller;
    startProgress('plateau', 'Светофор группы: подготовка…', 4, 38, 1500);
    try {
      const r = await apiStream('/api/proof/plateau', body, controller.signal, ev => {
        if (version !== state.profileVersion) return;
        const pctVal = Math.round(ev.percent || 0);
        const ceil = ev.stage === 'prepare' ? 45 : 98;
        updateProgressChannel('plateau', ev.percent || 0, ev.label || 'Светофор группы…', ceil, ev.stage === 'plateau' ? 1200 : 0);
        const n = $('group-profile-state');
        n.dataset.state = 'loading';
        n.textContent = `${ev.label || 'Светофор группы…'} (${pctVal}%)`;
        n.title = n.textContent;
      });
      if (version !== state.profileVersion) return;
      if (settingsKey(r) !== settingsKey()) {
        void onStaleSettings();
        return;
      }
      if (r.plateau) showGroupLampsReading(getInputs(), isReference(), r.plateau);
      else {
        const xs = r.excluded || [];
        const first = xs[0];
        paintGroupLamps(
          getInputs(),
          isReference(),
          null,
          `Нет групповой оценки: не прошли проверку ${xs.length} из ${r.member_count}. ${first ? first.target.pair + ' — ' + first.reason : ''} Отметки сохранены.`,
          'unavailable',
        );
      }
    } catch (e) {
      if (version === state.profileVersion) {
        state.profileKey = '';
        paintGroupLamps(getInputs(), isReference(), null, 'Не удалось рассчитать светофор группы: ' + e.message, 'unavailable');
      }
    } finally {
      if (version === state.profileVersion) {
        state.profileController = null;
        finishProgress('plateau');
      }
    }
  }, delay);
}
