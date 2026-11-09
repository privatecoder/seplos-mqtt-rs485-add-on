// Settings tab: the connection settings document (GET/PUT api/connection with If-Match).

import { api, ApiError } from './api.js';
import { t, errorText, currentLanguage, parseNumber } from './i18n.js';
import { h, uid, busy } from './ui.js';
import { storeBanner } from './overview.js';
import { feedSection } from './feed.js';

// keys the form knows; everything else in the loaded document is sent back untouched
const KNOWN = new Set([
  'transport', 'host', 'port', 'device', 'baud', 'pack_count', 'maint_firmware',
  'min_cell_voltage', 'max_cell_voltage', 'nominal_cell_voltage', 'update_interval',
  'bus', 'bus_quiet_ms',
]);

const FIELDS = [
  { name: 'transport', kind: 'select', options: ['tcp', 'serial'] },
  { name: 'host', kind: 'text', group: 'tcp' },
  { name: 'port', kind: 'text', group: 'tcp', inputmode: 'numeric' },
  { name: 'device', kind: 'text', group: 'serial' },
  { name: 'baud', kind: 'select', options: ['auto', '9600', '19200'], unit: 'Bd' },
  { name: 'pack_count', kind: 'text', inputmode: 'numeric' },
  { name: 'bus', kind: 'select', options: ['uart0', 'uart1'] },
  { name: 'bus_quiet_ms', kind: 'text', inputmode: 'numeric', unit: 'ms' },
  { name: 'maint_firmware', kind: 'checkbox' },
  { name: 'min_cell_voltage', kind: 'text', inputmode: 'decimal', unit: 'V' },
  { name: 'max_cell_voltage', kind: 'text', inputmode: 'decimal', unit: 'V' },
  { name: 'nominal_cell_voltage', kind: 'text', inputmode: 'decimal', unit: 'V' },
  { name: 'update_interval', kind: 'text', inputmode: 'numeric', unit: 's' },
];

const INT = /^\d+$/;

// The exact number as editable text: no rounding, no grouping (display formatting is for the
// Overview only). German shows a decimal comma; parseNumber accepts both separators.
export function exactText(value) {
  const text = expandExponent(String(value));
  return currentLanguage() === 'de' ? text.replace('.', ',') : text;
}

// "1e-25" -> "0.0000000000000000000000001", "1.5e-7" -> "0.00000015": the digits of String(value)
// are moved around, never rounded (parseNumber does not take exponent notation).
export function expandExponent(text) {
  const match = /^([+-]?)(\d+)(?:\.(\d+))?e([+-]?\d+)$/i.exec(text);
  if (!match) return text;
  const [, sign, whole, fraction = '', exponent] = match;
  const digits = whole + fraction;
  const point = whole.length + Number(exponent);
  if (point <= 0) return `${sign}0.${'0'.repeat(-point)}${digits}`;
  if (point >= digits.length) return `${sign}${digits}${'0'.repeat(point - digits.length)}`;
  return `${sign}${digits.slice(0, point)}.${digits.slice(point)}`;
}

export function toRaw(conn) {
  const volt = exactText;
  return {
    transport: conn.transport === 'serial' ? 'serial' : 'tcp',
    host: conn.host ?? '',
    port: String(conn.port ?? 4196),
    device: conn.device ?? '',
    baud: String(conn.baud ?? 'auto'),
    pack_count: String(conn.pack_count ?? 1),
    maint_firmware: Boolean(conn.maint_firmware),
    min_cell_voltage: volt(conn.min_cell_voltage ?? 2.5),
    max_cell_voltage: volt(conn.max_cell_voltage ?? 3.65),
    nominal_cell_voltage: volt(conn.nominal_cell_voltage ?? 3.2),
    update_interval: String(conn.update_interval ?? 1),
    bus: conn.bus === 'uart1' || (conn.pack_count ?? 1) > 1 ? 'uart1' : 'uart0',
    bus_quiet_ms: String(conn.bus_quiet_ms ?? 1000),
  };
}

const NUMERIC = new Set(['port', 'pack_count', 'bus_quiet_ms', 'update_interval', 'min_cell_voltage', 'max_cell_voltage',
  'nominal_cell_voltage']);

// One field as it would be saved: a number however it is written ("3,2" = "3.2", " 1000 " = "1000"), the bus as it
// applies (several packs: always the CAN socket); an invalid entry stays as typed, so it still counts as a change.
function normalised(raw, name) {
  const value = raw[name];
  if (name === 'maint_firmware') return Boolean(value);
  const text = String(value ?? '').trim();
  if (name === 'bus') return Number(String(raw.pack_count ?? '').trim()) > 1 ? 'uart1' : text;
  if (NUMERIC.has(name)) {
    const number = parseNumber(text);
    return Number.isNaN(number) ? text : String(number);
  }
  return text;
}

// The fields of the draft that differ from the loaded document, field by field (never by key order), with the old
// and new value: [{ name, from, to }]. Only the fields of the chosen transport count.
export function changes(baseline, raw) {
  const out = [];
  for (const spec of FIELDS) {
    const name = spec.name;
    if (spec.group && spec.group !== raw.transport && spec.group !== baseline.transport) continue;
    const from = normalised(baseline, name);
    const to = normalised(raw, name);
    if (from !== to) out.push({ name, from: String(from), to: String(to) });
  }
  return out;
}

// One change as a line of the leave guard: "Busruhe: 1000 → 200 ms".
export function describeChange(c, translate = t) {
  const spec = FIELDS.find((f) => f.name === c.name) || {};
  const value = (v) => {
    if (spec.kind === 'checkbox') return translate(v === 'true' ? 'guard.on' : 'guard.off');
    if (spec.kind === 'select') return translate(`settings.opt.${c.name}.${v}`);
    return v === '' ? '–' : v;
  };
  const unit = spec.unit ? ` ${spec.unit}` : '';
  return `${translate(`settings.f.${c.name}.label`)}: ${value(c.from)} → ${value(c.to)}${unit}`;
}

// The settings document to save: unknown keys of the loaded one travel back unchanged, known ones come from the form.
export function payload(loaded, values) {
  const out = {};
  for (const [key, value] of Object.entries(loaded || {})) if (!KNOWN.has(key)) out[key] = value;
  const names = ['transport', 'baud', 'pack_count', 'bus', 'bus_quiet_ms', 'maint_firmware', 'min_cell_voltage', 'max_cell_voltage',
    'nominal_cell_voltage', 'update_interval', ...(values.transport === 'tcp' ? ['host', 'port'] : ['device'])];
  for (const name of names) out[name] = values[name];
  return out;
}

// Client-side checks mirror the server model (which stays authoritative). Returns { errors, values }.
export function checkRaw(raw) {
  const errors = {};
  const values = { transport: raw.transport };
  if (raw.transport === 'tcp') {
    const host = raw.host.trim();
    if (!host) errors.host = 'required';
    else if (/\s/.test(host)) errors.host = 'format';
    values.host = host;
    if (!INT.test(raw.port.trim())) errors.port = 'type';
    else if (Number(raw.port) < 1 || Number(raw.port) > 65535) errors.port = 'range';
    values.port = Number(raw.port);
  } else {
    const device = raw.device.trim();
    if (!device) errors.device = 'required';
    else if (!device.startsWith('/')) errors.device = 'format';
    values.device = device;
  }
  values.baud = raw.baud === 'auto' ? 'auto' : Number(raw.baud);
  if (!INT.test(raw.pack_count.trim())) errors.pack_count = 'type';
  else if (Number(raw.pack_count) < 1 || Number(raw.pack_count) > 16) errors.pack_count = 'range';
  values.pack_count = Number(raw.pack_count);
  values.maint_firmware = raw.maint_firmware;
  for (const name of ['min_cell_voltage', 'max_cell_voltage', 'nominal_cell_voltage']) {
    const number = parseNumber(raw[name]);
    if (Number.isNaN(number)) errors[name] = 'type';
    else if (!(number > 0 && number <= 5)) errors[name] = 'range';
    values[name] = number;
  }
  if (!errors.min_cell_voltage && !errors.max_cell_voltage && !(values.min_cell_voltage < values.max_cell_voltage)) {
    errors.min_cell_voltage = 'order';
    errors.max_cell_voltage = 'order';
  }
  if (!INT.test(raw.update_interval.trim())) errors.update_interval = 'type';
  values.update_interval = Number(raw.update_interval);
  // several packs are always read on the CAN socket (the server enforces it as well)
  values.bus = values.pack_count > 1 ? 'uart1' : raw.bus;
  if (!INT.test(raw.bus_quiet_ms.trim())) errors.bus_quiet_ms = 'type';
  else if (Number(raw.bus_quiet_ms) < 200 || Number(raw.bus_quiet_ms) > 2000) errors.bus_quiet_ms = 'range';
  values.bus_quiet_ms = Number(raw.bus_quiet_ms);
  return { errors, values };
}

export function settingsView(ctx) {
  const session = ctx.session;
  const readOnly = !session.user;
  const storeOk = session.store.status === 'valid';
  const inputs = {};
  const groups = {};
  const errorEls = {};
  let loaded = null;       // the connection object as loaded (unknown keys travel back unchanged)
  let revision = null;
  let baseline = null;     // the raw values as loaded (or as last saved): what counts as unchanged
  // Request state: a load or a save in flight freezes the controls; every load and every save
  // bumps the generation, so a load that finishes after a newer request began is ignored entirely.
  let generation = 0;
  let loading = false;
  let saving = false;
  let loadFailed = false;

  const notice = h('div', { class: 'banner', role: 'status', hidden: true });
  const form = h('form', { class: 'stack', novalidate: true });
  const saveButton = h('button', { class: 'btn primary', type: 'submit' }, t('settings.save'));
  const resetButton = h('button', { class: 'btn', type: 'button', onClick: () => load() }, t('settings.discard'));

  function field(spec) {
    const id = uid(`f-${spec.name}`);
    const helpId = `${id}-help`;
    const errId = `${id}-err`;
    let input;
    if (spec.kind === 'select') {
      input = h('select', { id }, spec.options.map((value) => h('option', { value }, t(`settings.opt.${spec.name}.${value}`))));
    } else if (spec.kind === 'checkbox') {
      input = h('input', { id, type: 'checkbox' });
    } else {
      input = h('input', { id, type: 'text', inputmode: spec.inputmode, autocomplete: 'off', spellcheck: 'false' });
    }
    input.setAttribute('aria-describedby', `${helpId} ${errId}`);
    const errorEl = h('p', { class: 'field-error', id: errId, hidden: true });
    inputs[spec.name] = input;
    errorEls[spec.name] = errorEl;
    const label = h('label', { for: id }, t(`settings.f.${spec.name}.label`), spec.unit ? h('span', { class: 'unit' }, ` (${spec.unit})`) : null);
    const help = h('p', { class: 'help', id: helpId }, t(`settings.f.${spec.name}.help`));
    const wrap = spec.kind === 'checkbox'
      ? h('div', { class: 'field check' }, h('div', { class: 'check-row' }, input, label), help, errorEl)
      : h('div', { class: 'field' }, label, input, help, errorEl);
    if (spec.group) {
      groups[spec.name] = spec.group;
    }
    return wrap;
  }

  const fieldEls = {};
  for (const spec of FIELDS) fieldEls[spec.name] = field(spec);
  form.append(
    h('fieldset', { class: 'card' }, h('legend', {}, t('settings.group.connection')),
      fieldEls.transport, fieldEls.host, fieldEls.port, fieldEls.device, fieldEls.baud),
    h('fieldset', { class: 'card' }, h('legend', {}, t('settings.group.bank')),
      fieldEls.pack_count, fieldEls.bus, fieldEls.bus_quiet_ms, fieldEls.maint_firmware, fieldEls.update_interval),
    h('fieldset', { class: 'card' }, h('legend', {}, t('settings.group.cells')),
      fieldEls.min_cell_voltage, fieldEls.max_cell_voltage, fieldEls.nominal_cell_voltage),
    h('div', { class: 'actions' }, saveButton, resetButton));

  const feed = feedSection(ctx);
  const el = h('section', { 'aria-labelledby': 'view-title' },
    h('h2', { id: 'view-title', tabindex: '-1' }, t('tab.settings')),
    storeBanner(session),
    readOnly ? h('div', { class: 'banner info', role: 'status' }, t('readonly.hint')) : null,
    notice,
    // one stack: the same gap between the form's buttons, the hardware card and the feed card
    h('div', { class: 'stack' },
    form,
    h('section', { class: 'card', 'aria-labelledby': 'hw-title' },
      h('h3', { id: 'hw-title' }, t('settings.hw.title')),
      h('dl', { class: 'kvs' }, ['generation', 'platform', 'variant', 'hardware'].map((key) =>
        h('div', { class: 'kv' }, h('dt', {}, t(`settings.hw.${key}`)), h('dd', {}, t(`settings.hw.${key}.value`))))),
      h('p', { class: 'help' }, t('settings.hw.note'))),
    feed.el));

  function readRaw() {
    const raw = {};
    for (const spec of FIELDS) {
      raw[spec.name] = spec.kind === 'checkbox' ? inputs[spec.name].checked : inputs[spec.name].value;
    }
    return raw;
  }

  function writeRaw(raw) {
    singleBus = null;
    for (const spec of FIELDS) {
      if (spec.kind === 'checkbox') inputs[spec.name].checked = raw[spec.name];
      else inputs[spec.name].value = raw[spec.name];
    }
    applyTransport();
  }

  function applyTransport() {
    const tcp = inputs.transport.value === 'tcp';
    for (const [name, group] of Object.entries(groups)) fieldEls[name].hidden = (group === 'tcp') !== tcp;
    applyBus();
  }

  // the bus choice exists only for a single pack
  // the bus choice exists only for a single pack: several force the CAN socket, and going back to one pack restores
  // the choice made for it
  let singleBus = null;
  function applyBus() {
    const several = Number(inputs.pack_count.value) > 1;
    if (several && singleBus === null) singleBus = inputs.bus.value;
    if (!several && singleBus !== null) { inputs.bus.value = singleBus; singleBus = null; }
    if (several) inputs.bus.value = 'uart1';
    inputs.bus.disabled = several || controlsDisabled();
  }

  function pending() {
    return loaded !== null && baseline !== null && !lockedReason() ? changes(baseline, readRaw()) : [];
  }

  function setDisabled(disabled) {
    for (const input of Object.values(inputs)) input.disabled = disabled;
    if (Number(inputs.pack_count.value) > 1) inputs.bus.disabled = true;
    saveButton.disabled = disabled;
    resetButton.disabled = disabled;
  }

  // never editable: read-only session, unusable store, restart in progress
  function lockedReason() {
    return readOnly || !storeOk || ctx.restarting;
  }

  // what the controls show: also frozen while loading, saving, or after a failed load
  function controlsDisabled() {
    return lockedReason() || loading || saving || loadFailed;
  }

  function showNotice(text, kind = 'info', extra = null) {
    notice.className = `banner ${kind}`;
    notice.replaceChildren(text, extra ? ' ' : '', extra || '');
    notice.hidden = false;
  }

  function clearErrors() {
    for (const [name, errorEl] of Object.entries(errorEls)) {
      errorEl.hidden = true;
      errorEl.textContent = '';
      inputs[name].removeAttribute('aria-invalid');
    }
  }

  function showErrors(errors) {
    let first = null;
    for (const [name, key] of Object.entries(errors)) {
      if (!errorEls[name]) continue;
      errorEls[name].textContent = t(`invalid.${key}`, { field: t(`settings.f.${name}.label`) });
      errorEls[name].hidden = false;
      inputs[name].setAttribute('aria-invalid', 'true');
      if (!first && !fieldEls[name].hidden) first = inputs[name];
    }
    if (first) first.focus();
  }

  async function load() {
    if (saving) return;               // a save in flight owns the form
    const mine = ++generation;
    clearErrors();
    notice.hidden = true;
    loadFailed = false;
    if (!storeOk) {
      setDisabled(true);
      return;
    }
    loading = true;
    setDisabled(true);
    let data;
    try {
      data = await api('GET', 'api/connection');
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (mine !== generation) return;      // outdated: touches nothing
      loading = false;
      loadFailed = true;
      showNotice(errorText(error), 'error', reloadHint());     // the form stays frozen; reload is the way out
      setDisabled(true);
      return;
    }
    if (mine !== generation) return;        // a newer load or a save began meanwhile
    loading = false;
    loaded = data.connection;
    revision = data.revision;
    const raw = toRaw(loaded);
    if (ctx.pendingPackCount !== null && ctx.pendingPackCount !== undefined) {
      raw.pack_count = String(ctx.pendingPackCount);
      ctx.pendingPackCount = null;
      showNotice(t('settings.prefilled', { n: raw.pack_count }), 'info');
    }
    baseline = toRaw(loaded);
    writeRaw(raw);
    setDisabled(controlsDisabled());
  }

  function reloadHint() {
    return h('button', { class: 'btn small', type: 'button', onClick: () => load() }, t('settings.reload'));
  }

  async function save(event) {
    event.preventDefault();
    if (controlsDisabled()) return;
    clearErrors();
    notice.hidden = true;
    const submitted = readRaw();
    const { errors, values } = checkRaw(submitted);
    if (Object.keys(errors).length > 0) {
      showErrors(errors);
      showNotice(t('settings.fix_errors'), 'error');
      return;
    }
    // freeze the whole form while the request is pending: what was sent is what counts as saved.
    // Any load still in flight is outdated from now on (its answer must not touch the form).
    generation += 1;
    loading = false;
    saving = true;
    setDisabled(true);
    busy(saveButton, true);
    try {
      const result = await api('PUT', 'api/connection', { connection: payload(loaded, values) }, { revision });
      baseline = submitted;     // only the submitted draft is clean now
      showNotice(t('settings.saved'), 'ok');
      ctx.awaitRestart(result.revision);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (error.key === 'revision_stale') {
        showNotice(t('err.revision_stale'), 'warn', reloadHint());
      } else if (error.key === 'invalid_fields') {
        const serverErrors = {};
        for (const item of error.params.fields || []) serverErrors[String(item.path).replace(/^connection\./, '')] = item.key;
        showErrors(serverErrors);
        showNotice(errorText(error), 'error');
      } else {
        showNotice(errorText(error), 'error');
      }
    } finally {
      saving = false;
      busy(saveButton, false);
      setDisabled(controlsDisabled());
    }
  }

  inputs.transport.addEventListener('change', applyTransport);
  inputs.pack_count.addEventListener('input', applyBus);
  form.addEventListener('submit', save);
  applyTransport();
  setDisabled(true);
  load();

  return {
    el,
    dirty: () => pending().length > 0,
    // the lines the leave guard names
    changes: () => pending().map((c) => describeChange(c)),
    discardDraft() { baseline = readRaw(); },
    destroy() { feed.destroy(); },
  };
}
