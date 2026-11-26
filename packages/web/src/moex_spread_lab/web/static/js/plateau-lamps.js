import { $, apiStream, el, emit, finishProgress, fmt, pct, signed, startProgress, updateProgressChannel } from './dom.js';
import { settingsKey, state } from './state.js';
import { activeInputs, groupSpec, pointError, sharedInputs, sharedRequest } from './strategy-plans.js';

export function hidePlateauTip() {
  if (state.openLamp) {
    state.openLamp.removeAttribute('aria-describedby');
    state.openLamp = null;
  }
  $('plateau-tip').hidden = true;
}

function showPlateauTip(lamp) {
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

function drawLamps(inputs, plateau, empty, group) {
  for (const input of inputs) {
    const field = !input.closest('.hidden') ? plateau?.fields?.[input.dataset.axis] : null;
    const lamp = input.nextElementSibling;
    const score = field?.score;
    const level = score == null ? 'unknown' : score >= plateau.green ? 'green' : score >= plateau.amber ? 'amber' : 'red';
    const verdict = { green: 'устойчиво', amber: 'на краю плато', red: 'неустойчиво', unknown: 'нет оценки' }[level];
    lamp.dataset.level = level;
    lamp.dataset.score = score ?? '';
    lamp.dataset.heading = `${input.labels[0].textContent} · ${score == null ? 'нет оценки' : fmt(score, 2) + ' / 100 · ' + verdict}`;
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
      const fmtAxis = v => fmt(v, Number.isInteger(v) ? 0 : 1);
      lines.push(`Текущее значение: ${fmtAxis(field.value)}`);
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
          lines.push(`${fmtAxis(n.value)} → ${n.utility == null ? 'нет оценки: ' + n.reason : signed(n.utility) + ' п.п./год'}`);
        }
      }
      if (!field.reason && score != null) lines.push('Балл — доля результата центра, сохранённая худшим соседом (0–100).');
    } else {
      lines.push(empty);
    }
    lamp.dataset.detail = lines.join('\n');
    lamp.setAttribute('aria-label', lamp.dataset.heading);
    if (lamp === state.openLamp) showPlateauTip(lamp);
  }
}

export function paintPlateaus(plateau = null, empty = 'Нет оценки для текущего значения.') {
  if (!state.checked.size) drawLamps(activeInputs(), plateau, empty, null);
}

export function bindLamp(input) {
  const shell = input.parentElement;
  const lamp = input.nextElementSibling;
  const open = () => showPlateauTip(lamp);
  shell.addEventListener('pointerenter', open);
  shell.addEventListener('pointerleave', hidePlateauTip);
  shell.addEventListener('focusin', open);
  shell.addEventListener('focusout', hidePlateauTip);
  lamp.addEventListener('click', open);
}

function setGroupState(st, message) {
  const n = $('group-profile-state');
  n.dataset.state = st;
  n.textContent = message;
  n.title = message;
  n.setAttribute('aria-busy', String(st === 'loading'));
}

function paintGroupLamps(plateau, message, st) {
  if (!state.sharedStrategy || !state.checked.size) return;
  setGroupState(st, message);
  drawLamps(sharedInputs(), plateau, message, {
    count: state.checked.size,
    percent: state.preferences.validation_percent,
    reference: sharedRequest().fill === 'reference_same_bar',
  });
}

export function showGroupReading(plateau) {
  const fields = Object.values(plateau.fields);
  const any = fields.some(f => f.score != null);
  const reason = fields.find(f => f.reason)?.reason;
  paintGroupLamps(
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

export function refreshGroupProfile(delay = 140) {
  if (!state.sharedStrategy || !state.checked.size) return;
  const { body, key } = groupSpec();
  if (state.profileKey === key) return;
  stopProfile();
  state.profileKey = key;
  const version = state.profileVersion;
  const error = pointError(sharedInputs());
  if (error) {
    paintGroupLamps(null, error, 'invalid');
    return;
  }
  paintGroupLamps(null, 'Светофор группы: подготовка 0%…', 'loading');
  state.profileTimer = setTimeout(async () => {
    const controller = new AbortController();
    state.profileController = controller;
    startProgress('plateau', 'Светофор группы: подготовка…', 4, 38, 1500);
    try {
      const r = await apiStream('/api/proof/plateau', body, controller.signal, ev => {
        if (version !== state.profileVersion) return;
        const pctVal = Math.round(ev.percent || 0);
        updateProgressChannel('plateau', ev.percent || 0, ev.label || 'Светофор группы…', ev.stage === 'prepare' ? 45 : 98, ev.stage === 'plateau' ? 1200 : 0);
        setGroupState('loading', `${ev.label || 'Светофор группы…'} (${pctVal}%)`);
      });
      if (version !== state.profileVersion) return;
      if (settingsKey(r) !== settingsKey()) {
        emit('moex:stale-settings');
        return;
      }
      if (r.plateau) {
        showGroupReading(r.plateau);
      } else {
        const xs = r.excluded || [];
        const first = xs[0];
        paintGroupLamps(
          null,
          `Нет групповой оценки: не прошли проверку ${xs.length} из ${r.member_count}. ${first ? first.target.pair + ' — ' + first.reason : ''} Отметки сохранены.`,
          'unavailable',
        );
      }
    } catch (e) {
      if (version === state.profileVersion) {
        state.profileKey = '';
        paintGroupLamps(null, 'Не удалось рассчитать светофор группы: ' + e.message, 'unavailable');
      }
    } finally {
      if (version === state.profileVersion) {
        state.profileController = null;
        finishProgress('plateau');
      }
    }
  }, delay);
}
