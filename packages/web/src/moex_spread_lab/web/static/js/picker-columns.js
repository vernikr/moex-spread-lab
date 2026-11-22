/** Persisted, resizable picker column widths. */

import { $ } from './dom.js';

const COLUMN_LAYOUT_KEY = 'moex-proof-layout-v1';
const RESIZABLE_COLUMNS = [
  { key: 'selection', cssVar: '--selection-width', handleId: 'resize-selection', min: 220, max: 560, initial: 296 },
  { key: 'norm', cssVar: '--norm-width', handleId: 'resize-norm', min: 190, max: 480, initial: 262 },
];
const desiredColumnWidths = { selection: 296, norm: 262 };
const columnWidths = { selection: 296, norm: 262 };
const MAIN_MIN_WIDTH = 320;
const RESIZER_GUTTERS = 16;

function maxColumnWidth(column) {
  if (window.innerWidth <= 940) return column.max;
  const otherName = column.key === 'selection' ? 'norm' : 'selection';
  return Math.max(column.min, Math.min(column.max, $('layout').clientWidth - RESIZER_GUTTERS - MAIN_MIN_WIDTH - columnWidths[otherName]));
}

function applyColumnWidth(name, value, remember = true) {
  const column = RESIZABLE_COLUMNS.find(x => x.key === name);
  if (!column) return;
  const requested = Math.max(column.min, Math.min(column.max, Math.round(value)));
  if (remember) desiredColumnWidths[name] = requested;
  const maximum = maxColumnWidth(column);
  const width = Math.max(column.min, Math.min(maximum, requested));
  columnWidths[name] = width;
  $('layout').style.setProperty(column.cssVar, `${width}px`);
  const handle = $(column.handleId);
  if (handle) {
    handle.setAttribute('aria-valuemin', String(column.min));
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
  for (const column of RESIZABLE_COLUMNS) {
    const savedWidth = Number(saved[column.key]);
    const requested = Number.isFinite(savedWidth) && saved[column.key] != null ? savedWidth : column.initial;
    desiredColumnWidths[column.key] = Math.max(column.min, Math.min(column.max, Math.round(requested)));
    columnWidths[column.key] = desiredColumnWidths[column.key];
  }
  applyResponsiveWidths();
  for (const column of RESIZABLE_COLUMNS) {
    const handle = $(column.handleId);
    if (!handle) continue;
    handle.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.preventDefault();
      const pointerId = event.pointerId;
      const startX = event.clientX;
      const startWidth = columnWidths[column.key];
      handle.classList.add('active');
      document.body.classList.add('is-resizing');
      try {
        handle.setPointerCapture(pointerId);
      } catch (_) {}
      const move = moveEvent => {
        if (moveEvent.pointerId === pointerId) applyColumnWidth(column.key, startWidth + moveEvent.clientX - startX);
      };
      const finish = endEvent => {
        if (endEvent.pointerId !== pointerId) return;
        if (endEvent.type === 'pointerup') move(endEvent);
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', finish);
        handle.removeEventListener('pointercancel', finish);
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
      let next;
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
        event.preventDefault();
        next = columnWidths[column.key] + (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 40 : 10);
      } else if (event.key === 'Home') {
        event.preventDefault();
        next = column.min;
      } else if (event.key === 'End') {
        event.preventDefault();
        next = column.max;
      } else return;
      applyColumnWidth(column.key, next);
      persistColumnWidths();
    });
  }
  window.addEventListener('resize', applyResponsiveWidths);
}
