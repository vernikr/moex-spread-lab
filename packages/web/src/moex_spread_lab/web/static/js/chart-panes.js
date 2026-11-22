/** Chart pane scaffolding: collapsible panes, validation markers, marker colours. */

import { COLORS, axisDateParts, axisDateText, chartTime, el } from './dom.js';
import { state } from './state.js';

export function makePane(parent, heightClass, title, tagline, legend, collapsible = true) {
  const head = el('div', 'panel-title');
  const left = el('span');
  const div = el('div', 'chart ' + heightClass);
  if (collapsible) {
    const initialExpanded = state.expandedPanes.has(heightClass);
    div.id = 'chart-' + heightClass;
    div.classList.toggle('hidden', !initialExpanded);
    const toggle = el('button', 'pane-toggle');
    toggle.type = 'button';
    toggle.setAttribute('aria-expanded', String(initialExpanded));
    toggle.setAttribute('aria-controls', div.id);
    toggle.setAttribute('aria-label', `${initialExpanded ? 'Свернуть' : 'Развернуть'} ${title}`);
    const chevron = el('span', 'pane-chevron', '▸');
    chevron.setAttribute('aria-hidden', 'true');
    toggle.append(chevron, el('b', '', title));
    toggle.addEventListener('click', () => {
      const expanded = toggle.getAttribute('aria-expanded') !== 'true';
      const targetRange = expanded ? state.primarySpreadChart?.timeScale().getVisibleLogicalRange() : null;
      if (expanded) {
        state.expandedPanes.add(heightClass);
        div._revealing = true;
      } else state.expandedPanes.delete(heightClass);
      toggle.setAttribute('aria-expanded', String(expanded));
      toggle.setAttribute('aria-label', `${expanded ? 'Свернуть' : 'Развернуть'} ${title}`);
      div.classList.toggle('hidden', !expanded);
      if (expanded) {
        requestAnimationFrame(() =>
          requestAnimationFrame(() => {
            try {
              if (targetRange) div._chart?.timeScale().setVisibleLogicalRange(targetRange);
              else div._chart?.timeScale().fitContent();
            } catch (_) {
              try {
                div._chart?.timeScale().fitContent();
              } catch (_err) {}
            }
            requestAnimationFrame(() => {
              div._revealing = false;
            });
          }),
        );
      }
    });
    left.append(toggle);
    if (tagline) left.append(el('span', 'tagline', '  ' + tagline));
  } else {
    left.append(el('b', '', title));
    if (tagline) left.append(el('span', 'tagline', '  ' + tagline));
  }
  head.append(left);
  if (legend?.length > 1) {
    const lg = el('span', 'legend');
    for (const [label, color] of legend) {
      const s = el('span');
      const i = el('i');
      i.style.cssText = `display:inline-block;width:14px;height:3px;border-radius:2px;background:${color};margin-right:5px;vertical-align:middle`;
      s.append(i, label);
      lg.append(s);
    }
    head.append(lg);
  }
  parent.append(head, div);
  return div;
}

export function markValidationStart(chart, pane, day) {
  if (!day) return;
  const line = el('div', 'validation-line');
  const scale = chart.timeScale();
  line.hidden = true;
  line.setAttribute('aria-hidden', 'true');
  pane.append(line);
  const place = () => {
    if (!pane.isConnected) return;
    const x = scale.timeToCoordinate(chartTime(day));
    line.hidden = x == null || x < 0 || x >= scale.width();
    if (!line.hidden) {
      line.style.left = `${chart.priceScale('left').width() + x}px`;
      line.style.bottom = scale.height() + 'px';
    }
  };
  scale.subscribeVisibleLogicalRangeChange(place);
  scale.subscribeSizeChange(place);
  requestAnimationFrame(place);
}

export function markerColor(t, sel) {
  if (sel) return '#ffffff';
  if (t.open_at_end) return COLORS.warn;
  return t.net_rub > 0 ? COLORS.win : t.net_rub < 0 ? COLORS.loss : '#d7e6f3';
}
