// Shared pieces of the Cells, Packs and Overview views: check and balancer badges, cell type names, texts.

import { t } from './i18n.js';
import { chip } from './ui.js';

const RESULT_KIND = { ok: 'avail-online', near: 'avail-unknown', over: 'avail-offline', not_evaluated: '' };
const BALANCER_KIND = { active: 'avail-online', inactive: '', unknown: 'avail-unknown' };

// Same colours as the existing state badges: ok green, near amber, over red, not evaluated neutral.
export function resultChip(result) {
  const known = Object.hasOwn(RESULT_KIND, result) ? result : 'not_evaluated';
  return chip(t(`check.result.${known}`), RESULT_KIND[known]);
}

export function balancerChip(state) {
  const known = Object.hasOwn(BALANCER_KIND, state) ? state : 'unknown';
  return chip(t(`balancer.state.${known}`), BALANCER_KIND[known]);
}

// A text for a key family, or the key itself when this build has no text for it (never blank).
export function textOr(prefix, key) {
  if (key === null || key === undefined || key === '') return '';
  const text = t(`${prefix}.${key}`);
  return text === `${prefix}.${key}` ? String(key) : text;
}

export function invalidText(key) {
  return textOr('invalid', key);
}

// "EVE LF280K Rev B (2021)"
export function cellName(entry) {
  return ['manufacturer', 'model', 'variant'].map((k) => entry && entry[k]).filter((x) => x).join(' ');
}

export function nominalAh(entry) {
  const value = entry && entry.capacity ? entry.capacity.nominal_ah : null;
  return typeof value === 'number' ? value : null;
}

// Only web links are followed; anything else (javascript:, data:, garbage) is shown as plain text.
export function safeHttpUrl(text) {
  try {
    const url = new URL(String(text));
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

export function todayIso() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

// The id "<id>-custom", then "-custom-2", "-custom-3" …, free among the given ids.
export function proposeId(base, taken) {
  const stem = base.replace(/-custom(-\d+)?$/, '');
  let candidate = `${stem}-custom`;
  for (let n = 2; taken.has(candidate); n += 1) candidate = `${stem}-custom-${n}`;
  return candidate;
}

// round(rated / nominal) when the rated capacity is within 2 % of that many cells in parallel; else null.
export function suggestParallel(ratedAh, nominal) {
  if (typeof ratedAh !== 'number' || typeof nominal !== 'number' || !(nominal > 0) || !(ratedAh > 0)) return null;
  const n = Math.round(ratedAh / nominal);
  if (n < 1) return null;
  return Math.abs(ratedAh - n * nominal) <= 0.02 * n * nominal ? n : null;
}

// Server problem paths such as `cell_types[2].voltage.charge_cutoff_v` → { index: 2, field: 'voltage.charge_cutoff_v' }.
export function splitProblemPath(path) {
  const match = /^cell_types\[(\d+)\](?:\.(.+))?$/.exec(String(path));
  return match ? { index: Number(match[1]), field: match[2] || '' } : { index: null, field: String(path) };
}
