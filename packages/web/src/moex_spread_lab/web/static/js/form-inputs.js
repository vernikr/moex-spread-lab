import { $ } from './dom.js';
import { state } from './state.js';

export const activeInputs = () => state.numericInputs.filter(n => n.isConnected && !n.closest('.hidden'));
export const sharedInputs = () => [...$('cohort-editor').querySelectorAll('.number-shell input')];

export function activeStrategyName() {
  return state.universe === 'pair_screen' ? $('strategy').value : $('cstrategy').value;
}

export const formKey = (key, name) => key + '|' + name;
