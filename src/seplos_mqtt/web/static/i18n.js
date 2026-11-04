// Texts live in static/i18n/{de,en}.json (same keys in both). German for de*, English otherwise.

let texts = {};
let lang = 'en';

export function detectLanguage() {
  const name = String((typeof navigator !== 'undefined' && navigator.language) || 'en').toLowerCase();
  return name.startsWith('de') ? 'de' : 'en';
}

export function currentLanguage() {
  return lang;
}

export async function loadLanguage() {
  lang = detectLanguage();
  document.documentElement.lang = lang;
  const response = await fetch(`static/i18n/${lang}.json`, { cache: 'no-cache' });
  if (!response.ok) throw new Error(`texts ${lang}: HTTP ${response.status}`);
  texts = await response.json();
}

// {name} placeholders; an unknown key shows the key itself, so a gap is visible and not silent.
export function t(key, params = {}) {
  const raw = texts[key];
  if (raw === undefined) return key;
  return raw.replace(/\{(\w+)\}/g, (match, name) => (params[name] !== undefined ? String(params[name]) : match));
}

// API error keys map to `err.<key>`; anything unknown gets the generic text that names the key.
export function errorText(error) {
  const key = error && error.key ? error.key : 'unknown';
  if (texts[`err.${key}`] !== undefined) return t(`err.${key}`, error.params || {});
  return t('err.generic', { key });
}

// Elements with data-i18n="key" (static shell) get their text; data-i18n-label sets aria-label.
export function localiseDocument() {
  for (const el of document.querySelectorAll('[data-i18n]')) el.textContent = t(el.dataset.i18n);
  for (const el of document.querySelectorAll('[data-i18n-label]')) el.setAttribute('aria-label', t(el.dataset.i18nLabel));
  document.title = t('app.title');
}

const DASH = '–';

export function formatNumber(value, digits = 3) {
  return new Intl.NumberFormat(lang, { maximumFractionDigits: digits }).format(value);
}

// A value with its unit; unknown values are a dash and never look like a measurement.
export function fmt(value, unit, digits = 2) {
  if (value === null || value === undefined || typeof value !== 'number' || !Number.isFinite(value)) return DASH;
  return unit ? `${formatNumber(value, digits)} ${unit}` : formatNumber(value, digits);
}

export function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return DASH;
  const s = Math.max(0, seconds);
  if (s < 90) return `${Math.round(s)} s`;
  if (s < 5400) return `${Math.floor(s / 60)} min ${Math.round(s % 60)} s`;
  return `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`;
}

export function orDash(value) {
  return value === null || value === undefined || value === '' ? DASH : String(value);
}

// "3,65" and "3.65" both work. Anything that is not a plain decimal number is NaN.
export function parseNumber(text) {
  const clean = String(text).trim().replace(',', '.');
  if (!/^[+-]?(\d+\.?\d*|\.\d+)$/.test(clean)) return Number.NaN;
  return Number(clean);
}
