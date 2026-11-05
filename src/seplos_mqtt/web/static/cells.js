// Cells tab: the cell type catalog (bundled, read-only) and the own types (GET/PUT api/cells, If-Match revision).
// Own types: create, copy as template, edit (common fields; every other schema field is kept untouched),
// rename (id conflict with a bundled type), delete, JSON import and export.

import { api, ApiError } from './api.js';
import { t, errorText, fmt, orDash, packName, parseNumber } from './i18n.js';
import { h, uid, chip } from './ui.js';
import { storeBanner } from './overview.js';
import { cellName, nominalAh, safeHttpUrl, todayIso, proposeId, splitProblemPath, invalidText, textOr } from './cellui.js';

const CHEMISTRIES = ['LFP', 'NMC', 'LTO', 'NA-ION', 'OTHER'];
const FORM_FACTORS = ['prismatic', 'cylindrical', 'pouch'];
const SOURCE_TYPES = ['manufacturer_pdf', 'manufacturer_web', 'distributor_pdf'];
const ID_PATTERN = /^[a-z0-9][a-z0-9-]*$/;

// The form fields: dotted path into the entry. `kind`: text | number | enum; `req`: required by the schema.
export const FIELDS = [
  { path: 'id', kind: 'text', req: true, newOnly: true },
  { path: 'manufacturer', kind: 'text', req: true },
  { path: 'model', kind: 'text', req: true },
  { path: 'variant', kind: 'text' },
  { path: 'chemistry', kind: 'enum', req: true, options: CHEMISTRIES },
  { path: 'form_factor', kind: 'enum', options: FORM_FACTORS },
  { path: 'capacity.nominal_ah', kind: 'number', unit: 'Ah' },
  { path: 'capacity.nominal_energy_wh', kind: 'number', unit: 'Wh' },
  { path: 'voltage.nominal_v', kind: 'number', unit: 'V' },
  { path: 'voltage.charge_cutoff_v', kind: 'number', unit: 'V' },
  { path: 'voltage.discharge_cutoff_v', kind: 'number', unit: 'V' },
  { path: 'current.max_continuous_charge_a', kind: 'number', unit: 'A' },
  { path: 'current.max_continuous_discharge_a', kind: 'number', unit: 'A' },
  { path: 'temperature.charge_min_c', kind: 'number', unit: '°C' },
  { path: 'temperature.charge_max_c', kind: 'number', unit: '°C' },
  { path: 'temperature.discharge_min_c', kind: 'number', unit: '°C' },
  { path: 'temperature.discharge_max_c', kind: 'number', unit: '°C' },
  { path: 'datasheet.title', kind: 'text', req: true },
  { path: 'datasheet.url', kind: 'text', req: true },
  { path: 'datasheet.source_type', kind: 'enum', req: true, options: SOURCE_TYPES },
  { path: 'notes', kind: 'text' },
];

function getPath(object, path) {
  let node = object;
  for (const key of path.split('.')) {
    if (node === null || typeof node !== 'object' || !Object.hasOwn(node, key)) return undefined;
    node = node[key];
  }
  return node;
}

function setPath(object, path, value) {
  const keys = path.split('.');
  let node = object;
  for (const key of keys.slice(0, -1)) {
    if (node[key] === null || typeof node[key] !== 'object' || Array.isArray(node[key])) node[key] = {};
    node = node[key];
  }
  node[keys[keys.length - 1]] = value;
}

function deletePath(object, path) {
  const keys = path.split('.');
  const chain = [object];
  for (const key of keys.slice(0, -1)) {
    const next = chain[chain.length - 1][key];
    if (next === null || typeof next !== 'object') return;
    chain.push(next);
  }
  delete chain[chain.length - 1][keys[keys.length - 1]];
  // a group the form emptied disappears (the schema has no required group)
  for (let i = chain.length - 1; i > 0; i -= 1) {
    if (Object.keys(chain[i]).length === 0) delete chain[i - 1][keys[i - 1]];
  }
}

// Text the form shows for a stored value. Numbers keep their exact digits.
export function fieldText(entry, field) {
  const value = getPath(entry, field.path);
  return value === undefined || value === null ? '' : String(value);
}

// The entry the form describes: the base entry with the form's fields set or removed, nothing else touched.
// Returns { entry, errors } where errors maps a field path to an error key (client side checks only).
export function buildEntry(base, texts) {
  const entry = structuredClone(base);
  const errors = {};
  for (const field of FIELDS) {
    if (field.newOnly && !texts.isNew) continue;
    const raw = (texts[field.path] ?? '').trim();
    if (raw === '') {
      if (field.req) errors[field.path] = 'required';
      else deletePath(entry, field.path);
      continue;
    }
    if (field.kind === 'number') {
      const number = parseNumber(raw);
      if (Number.isNaN(number)) errors[field.path] = 'type';
      else setPath(entry, field.path, number);
    } else {
      setPath(entry, field.path, raw);
    }
  }
  if (texts.isNew && texts.id && !ID_PATTERN.test(texts.id.trim())) errors.id = 'format';
  return { entry, errors };
}

function toDate(entry) {
  return entry.updated || '';
}

export function cellsView(ctx) {
  const session = ctx.session;
  const readOnly = !session.user;
  const storeOk = session.store.status === 'valid';
  let cells = [];
  let manifest = null;
  let revision = null;
  let search = '';
  let loaded = false;
  let loading = false;
  let saving = false;
  let loadFailed = false;
  let generation = 0;
  // screen: 'list' | { detail: id } | { form: {...} } | { rename: id } | { importing: {...} } | { confirmDelete: id }
  let screen = 'list';
  let form = null;            // { base, isNew, texts, initial, errors, problems }
  let importPlan = null;      // { entries: [...], rows: [{ id, replaces }] }
  let problemList = [];       // [{ id, path, key }] shown above the screen
  let focusSelector = null;

  const notice = h('div', { class: 'banner', role: 'status', hidden: true });
  const body = h('div', { class: 'stack' });
  const fileInput = h('input', { type: 'file', accept: '.json,application/json', hidden: true, 'aria-hidden': 'true', tabindex: '-1' });
  const el = h('section', { 'aria-labelledby': 'view-title' },
    h('h2', { id: 'view-title', tabindex: '-1' }, t('tab.cells')),
    storeBanner(session),
    readOnly ? h('div', { class: 'banner info', role: 'status' }, t('readonly.hint')) : null,
    h('p', {}, t('cells.intro')),
    notice,
    body,
    fileInput);

  const lockedReason = () => readOnly || !storeOk || ctx.restarting;
  const controlsDisabled = () => lockedReason() || loading || saving || loadFailed;
  const ownEntries = () => cells.filter((c) => c.source === 'own').map((c) => c.entry);
  const byId = (id) => cells.find((c) => c.entry.id === id && !(c.source === 'bundled' && c.conflict));

  function showNotice(text, kind = 'info', extra = null) {
    notice.className = `banner ${kind}`;
    notice.replaceChildren(text, extra ? ' ' : '', extra || '');
    notice.hidden = false;
  }

  function formDirty() {
    if (screen === null || typeof screen !== 'object' || !screen.form || form === null) return false;
    return JSON.stringify(form.texts) !== form.initial;
  }

  function go(next, focus = null) {
    screen = next;
    problemList = [];
    focusSelector = focus;
    render();
  }

  // -- list ----------------------------------------------------------------------------------

  function matches(entry) {
    const needle = search.trim().toLowerCase();
    if (!needle) return true;
    const hay = [entry.manufacturer, entry.model, entry.variant, entry.id, String(nominalAh(entry) ?? '')].join(' ').toLowerCase();
    return needle.split(/\s+/).every((word) => hay.includes(word));
  }

  function sourceChips(item) {
    return [
      chip(t(`cells.source.${item.source}`), item.source === 'own' ? 'avail-unknown' : ''),
      item.conflict ? chip(t('cells.conflict'), 'avail-offline') : null,
    ];
  }

  function listItem(item) {
    const entry = item.entry;
    return h('li', { class: 'row' },
      h('div', { class: 'row-main' },
        h('button', {
          class: 'btn small', type: 'button', 'data-fk': `open-${entry.id}`,
          onClick: () => go({ detail: entry.id }, 'heading'),
        }, cellName(entry) || entry.id),
        h('span', { class: 'chips' }, sourceChips(item)),
        h('span', { class: 'muted' }, [entry.chemistry, fmt(nominalAh(entry), 'Ah', 1),
          fmt(getPath(entry, 'voltage.nominal_v'), 'V', 2)].filter((x) => x !== '–').join(' · '))));
  }

  function renderList() {
    const visible = cells.filter((c) => matches(c.entry));
    const searchInput = h('input', { type: 'text', id: 'cells-search', autocomplete: 'off', spellcheck: 'false', 'data-fk': 'search' });
    searchInput.value = search;
    searchInput.addEventListener('input', () => {
      search = searchInput.value;
      const position = searchInput.selectionStart;
      renderRows();
      searchInput.focus();
      searchInput.setSelectionRange(position, position);
    });
    const rows = h('ul', { class: 'rows' });
    const count = h('p', { class: 'help', role: 'status' });
    function renderRows() {
      const shown = cells.filter((c) => matches(c.entry));
      rows.replaceChildren(...shown.map(listItem));
      count.textContent = shown.length === 0 && loaded ? t('cells.none_found') : t('cells.count', { n: shown.length, total: cells.length });
    }
    renderRows();
    const locked = controlsDisabled();
    return [
      h('div', { class: 'actions' },
        h('button', { class: 'btn primary', type: 'button', disabled: locked, 'data-fk': 'new', onClick: () => openForm(null) }, t('cells.new')),
        h('button', { class: 'btn', type: 'button', disabled: locked, onClick: () => fileInput.click() }, t('cells.import')),
        h('button', { class: 'btn', type: 'button', disabled: loadFailed || ownEntries().length === 0, onClick: () => download(ownEntries(), 'seplos-cell-types.json') },
          t('cells.export_own'))),
      h('div', { class: 'field' },
        h('label', { for: 'cells-search' }, t('cells.search')), searchInput,
        h('p', { class: 'help' }, t('cells.search.help'))),
      count,
      visible.length === 0 && !loaded ? h('p', { class: 'muted', role: 'status' }, t('boot.loading')) : rows,
      manifest ? h('p', { class: 'help' }, t('cells.catalog_version', { version: orDash(manifest.version ?? manifest.commit ?? '') })) : null,
    ];
  }

  // -- detail --------------------------------------------------------------------------------

  function provenanceChip(entry, path) {
    const record = entry.provenance && entry.provenance[path];
    if (!record) return null;
    return chip(t(record.verified ? 'cells.verified' : 'cells.unverified'), record.verified ? 'avail-online' : 'avail-unknown');
  }

  function rate(value) {
    if (!value || typeof value.value !== 'number') return '–';
    return t(`cells.rate.${value.basis === 'P' ? 'P' : 'C'}`, { v: fmt(value.value, '', 3) });
  }

  function detailRow(entry, label, text, provPath) {
    if (text === '–' || text === '' || text === null || text === undefined) return null;
    return h('div', { class: 'kv' }, h('dt', {}, label), h('dd', {}, text, ' ', provPath ? provenanceChip(entry, provPath) : null));
  }

  function deratingBlock(entry) {
    const d = entry.charge_derating;
    if (!d) return null;
    const bound = (lo, hi, unit) => (lo === undefined && hi === undefined ? '–'
      : `${lo === undefined ? '' : fmt(lo, '', 1)} … ${hi === undefined ? '' : fmt(hi, '', 1)} ${unit}`.trim());
    const rows = d.points.map((p) => h('tr', {},
      h('td', {}, p.temp_c !== undefined ? fmt(p.temp_c, '°C', 1) : bound(p.temp_min_c, p.temp_max_c, '°C')),
      h('td', {}, bound(p.soc_min_pct, p.soc_max_pct, '%')),
      h('td', {}, fmt(p.value, d.basis, 3))));
    return h('div', { class: 'stack' },
      h('h4', {}, t('cells.derating'), ' ', provenanceChip(entry, 'charge_derating')),
      h('p', { class: 'help' }, t(`cells.derating.${d.basis === 'P' ? 'P' : 'C'}`),
        d.interpolation === 'linear' ? ` ${t('cells.derating.linear')}` : ` ${t('cells.derating.lower')}`),
      h('table', { class: 'table' },
        h('thead', {}, h('tr', {},
          h('th', { scope: 'col' }, t('cells.derating.temp')), h('th', { scope: 'col' }, t('cells.derating.soc')),
          h('th', { scope: 'col' }, t('cells.derating.rate')))),
        h('tbody', {}, rows)));
  }

  function datasheetBlock(entry) {
    const ds = entry.datasheet || {};
    const url = safeHttpUrl(ds.url);
    return h('div', { class: 'stack' },
      h('h4', {}, t('cells.datasheet')),
      h('dl', { class: 'kvs' },
        detailRow(entry, t('cells.f.datasheet_title'), ds.title),
        detailRow(entry, t('cells.f.datasheet_source'), ds.source_type ? textOr('cells.source_type', ds.source_type) : ''),
        detailRow(entry, t('cells.f.datasheet_revision'), ds.revision),
        detailRow(entry, t('cells.f.datasheet_date'), ds.document_date)),
      // rendered as text; a web link also opens, in a new tab without access to this page
      url
        ? h('p', {}, h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, ds.url))
        : h('p', { class: 'muted' }, ds.url || '–'));
  }

  function renderDetail(id) {
    const item = byId(id);
    if (!item) {
      return [h('p', { class: 'muted' }, t('cells.gone')), backButton()];
    }
    const e = item.entry;
    const num = (path, unit, digits = 3) => fmt(getPath(e, path), unit, digits);
    const own = item.source === 'own';
    const allProv = e.provenance ? Object.values(e.provenance) : [];
    const verified = allProv.length > 0 && allProv.every((p) => p.verified);
    const locked = controlsDisabled();
    const actions = [
      backButton(),
      h('button', { class: 'btn', type: 'button', disabled: locked, onClick: () => openForm(e, { template: true }) }, t('cells.copy')),
      h('button', { class: 'btn', type: 'button', onClick: () => download([e], `${e.id}.json`) }, t('cells.export_one')),
      own ? h('button', { class: 'btn', type: 'button', disabled: locked, onClick: () => openForm(e) }, t('cells.edit')) : null,
      own && item.conflict ? h('button', { class: 'btn', type: 'button', disabled: locked, onClick: () => go({ rename: e.id }, 'rename-to') }, t('cells.rename')) : null,
      own ? h('button', { class: 'btn danger', type: 'button', disabled: locked, onClick: () => go({ confirmDelete: e.id }, 'confirm-delete') }, t('cells.delete')) : null,
    ];
    return [
      h('div', { class: 'card-head' },
        h('h3', { id: 'cell-heading', tabindex: '-1', 'data-fk': 'heading' }, cellName(e) || e.id),
        h('span', { class: 'chips' }, sourceChips(item), verified ? chip(t('cells.all_verified'), 'avail-online') : null)),
      item.conflict ? h('div', { class: 'banner warn', role: 'status' }, t('cells.conflict.text', { id: e.id })) : null,
      h('div', { class: 'actions' }, actions),
      h('article', { class: 'card stack' },
        h('dl', { class: 'kvs' },
          detailRow(e, t('cells.f.id'), e.id),
          detailRow(e, t('cells.f.version'), `${e.entry_version} (${toDate(e)})`),
          detailRow(e, t('cells.f.chemistry'), e.chemistry),
          detailRow(e, t('cells.f.form_factor'), e.form_factor ? textOr('cells.form_factor', e.form_factor) : '', 'form_factor'),
          detailRow(e, t('cells.f.nominal_v'), num('voltage.nominal_v', 'V', 3), 'voltage.nominal_v'),
          detailRow(e, t('cells.f.nominal_ah'), num('capacity.nominal_ah', 'Ah', 3), 'capacity.nominal_ah'),
          detailRow(e, t('cells.f.nominal_wh'), num('capacity.nominal_energy_wh', 'Wh', 1), 'capacity.nominal_energy_wh'),
          detailRow(e, t('cells.f.charge_cutoff'), num('voltage.charge_cutoff_v', 'V', 3), 'voltage.charge_cutoff_v'),
          detailRow(e, t('cells.f.discharge_cutoff'), num('voltage.discharge_cutoff_v', 'V', 3), 'voltage.discharge_cutoff_v'),
          detailRow(e, t('cells.f.discharge_cutoff_low'), num('voltage.discharge_cutoff_low_temp_v', 'V', 3), 'voltage.discharge_cutoff_low_temp_v'),
          detailRow(e, t('cells.f.std_charge'), rate(getPath(e, 'current.standard_charge')), 'current.standard_charge'),
          detailRow(e, t('cells.f.std_discharge'), rate(getPath(e, 'current.standard_discharge')), 'current.standard_discharge'),
          detailRow(e, t('cells.f.max_charge'), num('current.max_continuous_charge_a', 'A', 3), 'current.max_continuous_charge_a'),
          detailRow(e, t('cells.f.max_discharge'), num('current.max_continuous_discharge_a', 'A', 3), 'current.max_continuous_discharge_a'),
          detailRow(e, t('cells.f.pulse_charge'), rate(getPath(e, 'current.pulse_charge')), 'current.pulse_charge'),
          detailRow(e, t('cells.f.pulse_discharge'), rate(getPath(e, 'current.pulse_discharge')), 'current.pulse_discharge'),
          detailRow(e, t('cells.f.charge_temp'), tempRange(e, 'charge'), 'temperature.charge_min_c'),
          detailRow(e, t('cells.f.discharge_temp'), tempRange(e, 'discharge'), 'temperature.discharge_min_c'),
          detailRow(e, t('cells.f.storage_temp'), tempRange(e, 'storage'), 'temperature.storage_min_c')),
        h('p', { class: 'help' }, t('cells.rate.help')),
        deratingBlock(e),
        datasheetBlock(e),
        e.notes ? h('p', {}, e.notes) : null),
    ];
  }

  function tempRange(entry, kind) {
    const lo = getPath(entry, `temperature.${kind}_min_c`);
    const hi = getPath(entry, `temperature.${kind}_max_c`);
    if (lo === undefined && hi === undefined) return '';
    return `${lo === undefined ? '–' : fmt(lo, '', 1)} … ${hi === undefined ? '–' : fmt(hi, '', 1)} °C`;
  }

  function backButton() {
    return h('button', { class: 'btn', type: 'button', 'data-fk': 'back', onClick: () => go('list', 'search') }, t('cells.back'));
  }

  // -- delete / rename -------------------------------------------------------------------------

  function renderConfirmDelete(id) {
    const item = byId(id);
    if (!item) return [h('p', { class: 'muted' }, t('cells.gone')), backButton()];
    return [
      h('div', { class: 'banner warn', role: 'alert' },
        h('p', {}, t('cells.delete.confirm', { name: cellName(item.entry) || id })),
        h('div', { class: 'actions' },
          h('button', { class: 'btn danger', type: 'button', 'data-fk': 'confirm-delete', onClick: () => removeCell(id) }, t('cells.delete')),
          h('button', { class: 'btn', type: 'button', onClick: () => go({ detail: id }, 'heading') }, t('cells.cancel')))),
    ];
  }

  function renderRename(id) {
    const item = byId(id);
    if (!item) return [h('p', { class: 'muted' }, t('cells.gone')), backButton()];
    const taken = new Set(cells.map((c) => c.entry.id));
    const input = h('input', { type: 'text', id: 'rename-to', autocomplete: 'off', spellcheck: 'false', 'data-fk': 'rename-to' });
    input.value = proposeId(id, taken);
    return [
      h('form', { class: 'card stack', novalidate: true, onSubmit: (event) => { event.preventDefault(); renameCell(id, input.value.trim()); } },
        h('h3', {}, t('cells.rename')),
        h('p', {}, t('cells.rename.help', { id })),
        h('div', { class: 'field' }, h('label', { for: 'rename-to' }, t('cells.rename.to')), input),
        h('div', { class: 'actions' },
          h('button', { class: 'btn primary', type: 'submit' }, t('cells.rename')),
          h('button', { class: 'btn', type: 'button', onClick: () => go({ detail: id }, 'heading') }, t('cells.cancel')))),
    ];
  }

  // -- form ------------------------------------------------------------------------------------

  function openForm(source, { template = false } = {}) {
    if (controlsDisabled()) return;
    const isNew = source === null || template;
    let base;
    if (source === null) {
      base = { schema_version: 1, id: '', entry_version: 1, updated: todayIso(), manufacturer: '', model: '', chemistry: 'LFP',
        datasheet: { title: '', source_type: 'manufacturer_pdf', url: '' } };
    } else if (template) {
      base = structuredClone(source);
      base.id = proposeId(source.id, new Set(cells.map((c) => c.entry.id)));
      base.entry_version = 1;
      base.updated = todayIso();
    } else {
      base = structuredClone(source);
    }
    const texts = { isNew };
    for (const field of FIELDS) texts[field.path] = fieldText(base, field);
    form = { base, isNew, texts, initial: JSON.stringify(texts), errors: {}, editedId: source && !template ? source.id : null };
    go({ form: true }, 'f-first');
  }

  function fieldRow(field) {
    const id = uid('f-cell');
    const error = form.errors[field.path];
    let input;
    if (field.kind === 'enum') {
      input = h('select', {},
        field.req ? null : h('option', { value: '' }, '–'),
        field.options.map((o) => h('option', { value: o }, field.path === 'datasheet.source_type' ? textOr('cells.source_type', o)
          : field.path === 'form_factor' ? textOr('cells.form_factor', o) : o)));
      input.value = form.texts[field.path];
    } else {
      input = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', inputmode: field.kind === 'number' ? 'decimal' : null });
      input.value = form.texts[field.path];
    }
    input.id = id;
    input.setAttribute('aria-describedby', `${id}-err`);
    if (error) input.setAttribute('aria-invalid', 'true');
    input.dataset.path = field.path;
    input.addEventListener(field.kind === 'enum' ? 'change' : 'input', () => {
      form.texts[field.path] = input.value;
      saveButtonState();
    });
    const label = t(`cells.form.${field.path}`);
    return h('div', { class: 'field' },
      h('label', { for: id }, label, field.unit ? h('span', { class: 'unit' }, ` (${field.unit})`) : null, field.req ? ' *' : null),
      input,
      field.path === 'id' ? h('p', { class: 'help' }, t('cells.form.id.help')) : null,
      h('p', { class: 'field-error', id: `${id}-err`, hidden: !error }, error ? invalidText(error) : ''));
  }

  let formSave = null;
  function saveButtonState() {
    if (formSave) formSave.disabled = controlsDisabled() || !formDirty();
  }

  function renderForm() {
    const group = (titleKey, paths) => h('fieldset', { class: 'card stack' },
      h('legend', {}, t(titleKey)),
      h('div', { class: 'form-grid' }, FIELDS.filter((f) => paths.includes(f.path) && !(f.newOnly && !form.isNew)).map(fieldRow)));
    formSave = h('button', { class: 'btn primary', type: 'submit' }, t('cells.save'));
    const heading = form.isNew ? t('cells.form.new') : t('cells.form.edit', { name: cellName(form.base) || form.base.id });
    const result = h('form', { class: 'stack', novalidate: true, onSubmit: saveForm },
      h('h3', { id: 'form-heading', tabindex: '-1' }, heading),
      h('p', { class: 'help' }, t('cells.form.help')),
      problemSummary(),
      group('cells.form.group.identity', ['id', 'manufacturer', 'model', 'variant', 'chemistry', 'form_factor']),
      group('cells.form.group.electrical', ['capacity.nominal_ah', 'capacity.nominal_energy_wh', 'voltage.nominal_v',
        'voltage.charge_cutoff_v', 'voltage.discharge_cutoff_v']),
      group('cells.form.group.current', ['current.max_continuous_charge_a', 'current.max_continuous_discharge_a']),
      group('cells.form.group.temperature', ['temperature.charge_min_c', 'temperature.charge_max_c',
        'temperature.discharge_min_c', 'temperature.discharge_max_c']),
      group('cells.form.group.datasheet', ['datasheet.title', 'datasheet.url', 'datasheet.source_type', 'notes']),
      h('p', { class: 'help' }, t('cells.form.preserved')),
      h('div', { class: 'actions' }, formSave,
        h('button', { class: 'btn', type: 'button', onClick: () => leaveForm() }, t('cells.cancel'))));
    const first = result.querySelector('input, select');
    if (first) first.setAttribute('data-fk', 'f-first');
    return [result];
  }

  function leaveForm() {
    const editedId = form ? form.editedId : null;
    form = null;
    if (editedId) go({ detail: editedId }, 'heading');
    else go('list', 'new');
  }

  async function saveForm(event) {
    event.preventDefault();
    if (controlsDisabled() || !formDirty()) return;
    const { entry, errors } = buildEntry(form.base, form.texts);
    form.errors = errors;
    problemList = [];
    if (Object.keys(errors).length > 0) {
      problemList = Object.entries(errors).map(([path, key]) => ({ id: entry.id || '', path, key }));
      render();
      focusError();
      return;
    }
    const own = ownEntries();
    const list = form.isNew ? [...own, entry] : own.map((o) => (o.id === form.editedId ? entry : o));
    const result = await write(() => api('PUT', 'api/cells', { cells: list }, { revision }), list.map((e) => e.id));
    if (result === null) return;
    const id = entry.id;
    form = null;
    go({ detail: id }, 'heading');
    showNotice(t('cells.saved'), 'ok');
  }

  function problemSummary() {
    if (problemList.length === 0) return null;
    return h('div', { class: 'banner error', role: 'alert', tabindex: '-1', 'data-fk': 'problems' },
      h('p', {}, t('cells.fix_errors')),
      h('ul', {}, problemList.map((p) => h('li', {},
        p.id ? `${p.id}: ` : '', h('code', {}, p.path), ' – ', invalidText(p.key)))));
  }

  function focusError() {
    const target = body.querySelector('[data-fk="problems"]') || body.querySelector('[aria-invalid="true"]');
    if (target) target.focus();
  }

  // -- writes ----------------------------------------------------------------------------------

  // One write: guards, busy state, error mapping. Returns the answer, or null when it failed
  // (the failure is already shown). `ids`: the ids of the sent list, to name problems by entry.
  async function write(send, ids = []) {
    if (controlsDisabled()) return null;
    notice.hidden = true;
    generation += 1;
    loading = false;
    saving = true;
    let focusAfterRender = false;
    render();
    try {
      const data = await send();
      adopt(data);
      return data;
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      saving = false;
      if (error.key === 'revision_stale') {
        form = null;
        screen = 'list';
        await load();
        showNotice(t('cells.stale'), 'warn');
      } else if (error.key === 'invalid_fields') {
        problemList = (error.params.fields || []).map((item) => {
          const { index, field } = splitProblemPath(item.path);
          return { id: index !== null && ids[index] ? ids[index] : '', path: field || item.path, key: item.key };
        });
        if (form) {
          form.errors = {};
          for (const p of problemList) if (FIELDS.some((f) => f.path === p.path)) form.errors[p.path] = p.key;
        }
        showNotice(errorText(error), 'error');
        focusAfterRender = true;          // the final render below would detach a focus set here
      } else if (error.key === 'in_use') {
        const items = error.params.items || [];
        showNotice(items.map((i) => t('cells.in_use', { id: i.id, packs: (i.addresses || []).map(packName).join(', ') })).join(' '), 'error');
        render();
      } else {
        showNotice(errorText(error), 'error');
        render();
      }
      return null;
    } finally {
      saving = false;
      render();
      if (focusAfterRender) focusError();
    }
  }

  async function removeCell(id) {
    const list = ownEntries().filter((e) => e.id !== id);
    const result = await write(() => api('PUT', 'api/cells', { cells: list }, { revision }), list.map((e) => e.id));
    if (result === null) return;
    go('list', 'new');
    showNotice(t('cells.deleted'), 'ok');
  }

  async function renameCell(from, to) {
    const result = await write(() => api('POST', 'api/cells/rename', { from, to }, { revision }), []);
    if (result === null) return;
    go({ detail: to }, 'heading');
    showNotice(t('cells.renamed', { id: to }), 'ok');
  }

  // -- import / export -------------------------------------------------------------------------

  function download(entries, filename) {
    const blob = new Blob([`${JSON.stringify(entries, null, 2)}\n`], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const link = h('a', { href: url, download: filename });
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function onFile() {
    const file = fileInput.files && fileInput.files[0];
    fileInput.value = '';
    if (!file || controlsDisabled()) return;
    let parsed;
    try {
      parsed = JSON.parse(await file.text());
    } catch {
      showNotice(t('cells.import.bad_json'), 'error');
      return;
    }
    if (!Array.isArray(parsed) || parsed.length === 0 || parsed.some((x) => x === null || typeof x !== 'object' || Array.isArray(x))) {
      showNotice(t('cells.import.not_array'), 'error');
      return;
    }
    const own = new Set(ownEntries().map((e) => e.id));
    importPlan = {
      entries: parsed,
      rows: parsed.map((e) => ({ id: typeof e.id === 'string' ? e.id : '', replaces: own.has(e.id) })),
    };
    notice.hidden = true;
    go({ importing: true }, 'import-go');
  }

  function renderImport() {
    return [
      h('div', { class: 'card stack' },
        h('h3', {}, t('cells.import')),
        h('p', {}, t('cells.import.preview', { n: importPlan.entries.length })),
        problemSummary(),
        h('ul', {}, importPlan.rows.map((row) => h('li', {}, row.id || t('cells.import.no_id'), ' ',
          chip(t(row.replaces ? 'cells.import.replace' : 'cells.import.add'), row.replaces ? 'avail-unknown' : 'avail-online')))),
        h('div', { class: 'actions' },
          h('button', { class: 'btn primary', type: 'button', 'data-fk': 'import-go', disabled: controlsDisabled(), onClick: runImport }, t('cells.import.go')),
          h('button', { class: 'btn', type: 'button', onClick: () => { importPlan = null; go('list', 'new'); } }, t('cells.cancel')))),
    ];
  }

  async function runImport() {
    const incoming = importPlan.entries;
    const merged = ownEntries();
    const ids = merged.map((e) => e.id);
    for (const entry of incoming) {
      const at = typeof entry.id === 'string' ? ids.indexOf(entry.id) : -1;
      if (at >= 0) merged[at] = entry;
      else {
        merged.push(entry);
        ids.push(typeof entry.id === 'string' ? entry.id : t('cells.import.no_id'));
      }
    }
    const result = await write(() => api('PUT', 'api/cells', { cells: merged }, { revision }), ids);
    if (result === null) return;
    importPlan = null;
    go('list', 'new');
    showNotice(t('cells.import.done', { n: incoming.length }), 'ok');
  }

  // -- render / load ---------------------------------------------------------------------------

  function render() {
    let children;
    if (loadFailed) children = [];
    else if (screen === 'list') children = renderList();
    else if (screen.detail) children = renderDetail(screen.detail);
    else if (screen.confirmDelete) children = renderConfirmDelete(screen.confirmDelete);
    else if (screen.rename) children = renderRename(screen.rename);
    else if (screen.form && form) children = renderForm();
    else if (screen.importing && importPlan) children = renderImport();
    else children = renderList();
    // the native replaceChildren turns null into the text "null": only real nodes go in
    body.replaceChildren(...children.filter((child) => child !== null && child !== undefined && child !== false));
    for (const control of body.querySelectorAll('input, select, button')) {
      if (saving || loading) control.disabled = true;
    }
    if (screen.form) saveButtonState();
    if (focusSelector) {
      const target = focusSelector === 'heading' ? body.querySelector('[data-fk="heading"]') : body.querySelector(`[data-fk="${focusSelector}"]`);
      focusSelector = null;
      if (target && !target.disabled) target.focus();
    }
  }

  function adopt(data) {
    cells = data.cells;
    manifest = data.manifest || null;
    revision = data.revision;
    loaded = true;
  }

  async function load() {
    if (saving) return;
    const mine = ++generation;
    loadFailed = false;
    if (!storeOk) {
      loaded = true;
      render();
      return;
    }
    loading = true;
    render();
    let data;
    try {
      data = await api('GET', 'api/cells');
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (mine !== generation) return;
      loading = false;
      loadFailed = true;
      showNotice(errorText(error), 'error', h('button', { class: 'btn small', type: 'button', onClick: () => load() }, t('sets.reload')));
      render();
      return;
    }
    if (mine !== generation) return;
    loading = false;
    adopt(data);
    render();
  }

  fileInput.addEventListener('change', onFile);
  render();
  load();

  return {
    el,
    dirty: () => !lockedReason() && formDirty(),
    discardDraft() { form = null; },
    destroy() {},
  };
}

