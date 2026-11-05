// Packs tab: per pack address the cell type (+ parallel), the balancer profile, identity guard, check summaries and
// update offers (GET/PUT api/packs); below them the balancer profile editor (GET/PUT api/balancer-profiles).
// Every save carries the store revision (If-Match); a 412 reloads everything and says so.

import { api, ApiError } from './api.js';
import { t, errorText, fmt, orDash, packName, parseNumber } from './i18n.js';
import { h, uid, busy, chip } from './ui.js';
import { storeBanner } from './overview.js';
import { resultChip, textOr, invalidText, cellName, nominalAh, suggestParallel } from './cellui.js';

const MAX_NAME = 40;
const MAX_PARALLEL = 10000;
const WHENS = ['always', 'charging', 'not-discharging'];
const MV_FIELDS = ['trigger_mv', 'stop_mv', 'start_mv', 'stop_cell_mv'];
const PROFILE_FIELDS = ['name', ...MV_FIELDS, 'when', 'max_a'];
const PROFILE_PATH = /^balancer_profiles\[(\d+)\](?:\.(?:values\.)?(\w+))?$/;
const PACK_PATH = /^packs\.(\d+)(?:\.(cell\.id|cell\.parallel|cell|balancer_profile))?/;

// -- pure helpers (tested with node) -------------------------------------------------------------

function mvProblem(text, required) {
  const raw = String(text).trim();
  if (raw === '') return required ? 'required' : null;
  const n = parseNumber(raw);
  if (Number.isNaN(n)) return 'type';
  return Number.isInteger(n) && n >= 1 && n <= 5000 ? null : 'range';
}

// Client-side mirror of the server rules (exchange format `balancers[]`); one error key per field.
export function checkProfiles(drafts) {
  const names = new Set();
  return drafts.map((d) => {
    const e = {};
    const name = d.name.trim();
    if (!name || [...name].length > MAX_NAME) e.name = 'range';
    else if (names.has(name.toLowerCase())) e.name = 'duplicate';
    names.add(name.toLowerCase());
    for (const key of MV_FIELDS) {
      const problem = mvProblem(d[key], key === 'trigger_mv');
      if (problem) e[key] = problem;
    }
    if (!WHENS.includes(d.when)) e.when = 'value';
    const maxA = String(d.max_a).trim();
    if (maxA !== '') {
      const n = parseNumber(maxA);
      if (Number.isNaN(n)) e.max_a = 'type';
      else if (n < 0 || n > 1000000) e.max_a = 'range';
    }
    return e;
  });
}

function numberOrNull(text) {
  const raw = String(text).trim();
  return raw === '' ? null : parseNumber(raw);
}

// The profile as the API takes it: unknown keys and the id of a stored profile travel back unchanged.
export function profilePayload(d) {
  const base = d.base || {};
  const values = { ...(base.values || {}) };
  for (const key of MV_FIELDS) values[key] = numberOrNull(d[key]);
  values.when = d.when;
  values.max_a = numberOrNull(d.max_a);
  return { ...base, name: d.name.trim(), values };
}

function profileDraft(profile) {
  const v = profile ? profile.values : {};
  const text = (x) => (x === null || x === undefined ? '' : String(x));
  return {
    key: uid('profile'), base: profile || null, name: profile ? profile.name : '',
    trigger_mv: text(v.trigger_mv), stop_mv: text(v.stop_mv), start_mv: text(v.start_mv), stop_cell_mv: text(v.stop_cell_mv),
    when: v.when || 'always', max_a: text(v.max_a),
  };
}

// The parallel count of a pack draft: an integer 1..10000, else null.
export function parseParallel(text) {
  const n = parseNumber(text);
  return Number.isInteger(n) && n >= 1 && n <= MAX_PARALLEL ? n : null;
}

// References for the changed packs only (a listed address is replaced, an unlisted one stays).
export function packsPayload(drafts, baseline) {
  const out = {};
  for (const [address, d] of Object.entries(drafts)) {
    if (JSON.stringify(d) === JSON.stringify(baseline[address])) continue;
    out[address] = {
      cell: d.cellId === '' ? null : { id: d.cellId, parallel: parseParallel(d.parallel) },
      balancer_profile: d.profile === '' ? null : Number(d.profile),
    };
  }
  return out;
}

const POLL_MS = 5000;       // the same pace as the overview

// -- view ----------------------------------------------------------------------------------------

export function packsView(ctx) {
  const session = ctx.session;
  const readOnly = !session.user;
  const storeOk = session.store.status === 'valid';
  let packs = [];
  let cells = [];
  let profiles = [];
  let status = null;
  let revision = null;
  let drafts = {};            // address -> { cellId, parallel, profile }
  let baseline = {};
  let packErrors = {};        // address -> { cell, parallel, profile }
  let profileDrafts = [];
  let profileInitial = null;
  let profileErrors = [];
  let confirmAll = null;      // { source, addresses }
  const open = new Set();     // addresses whose full editor is expanded (drafts live in `drafts`, not in the DOM)
  // The live status comes in by polling and only fills these slots (chip in the head, check lists in the editor),
  // so a refresh never rebuilds a form: drafts, dirty state, focus and the caret stay as they are.
  const statusSlots = new Map();     // address -> { chip, checks }
  const openChecks = new Set();      // "<address>-<live|parameters>" check lists the user expanded
  let pollTimer = null;
  let destroyed = false;
  // Every status request takes the next number; only the newest answer is applied. A reload and a save
  // take a number too, so an answer that was already on its way before them is dropped as outdated.
  let statusSeq = 0;
  let loaded = false;
  let loading = false;
  let saving = false;
  let loadFailed = false;
  let generation = 0;

  // `notice`: loading and conflicts (top of the page); the two save bars carry the feedback of their own saves,
  // so the answer appears next to the buttons the user just used
  const notice = h('div', { class: 'banner', role: 'status', hidden: true });
  const packNotice = h('div', { class: 'banner', role: 'status', tabindex: '-1', hidden: true });
  const profileNotice = h('div', { class: 'banner', role: 'status', tabindex: '-1', hidden: true });
  const packList = h('div', { class: 'stack' });
  const profileList = h('div', { class: 'stack' });
  const savePacks = h('button', { class: 'btn primary', type: 'submit' }, t('packs.save'));
  const discardPacks = h('button', { class: 'btn', type: 'button', onClick: () => resetPackDrafts() }, t('sets.discard'));
  const addProfile = h('button', { class: 'btn', type: 'button', onClick: () => editProfiles(() => profileDrafts.push(profileDraft(null)), 'last') }, t('profiles.add'));
  const saveProfiles = h('button', { class: 'btn primary', type: 'submit' }, t('profiles.save'));
  const discardProfiles = h('button', { class: 'btn', type: 'button', onClick: () => { resetProfileDrafts(); renderProfiles(); } }, t('sets.discard'));
  const packForm = h('form', { class: 'stack', novalidate: true }, packList,
    h('div', { class: 'savebar' }, packNotice, h('div', { class: 'actions' }, savePacks, discardPacks)));
  const profileForm = h('form', { class: 'stack', novalidate: true }, profileList,
    h('div', { class: 'savebar' }, profileNotice, h('div', { class: 'actions' }, addProfile, saveProfiles, discardProfiles)));
  const profilesTitle = h('h3', { id: 'profiles-title', tabindex: '-1' }, t('profiles.title'));
  const toProfiles = h('button', {
    class: 'btn small', type: 'button',
    onClick: () => { profilesTitle.scrollIntoView(); profilesTitle.focus({ preventScroll: true }); },
  }, t('profiles.jump'));
  const el = h('section', { 'aria-labelledby': 'view-title' },
    h('h2', { id: 'view-title', tabindex: '-1' }, t('tab.packs')),
    storeBanner(session),
    readOnly ? h('div', { class: 'banner info', role: 'status' }, t('readonly.hint')) : null,
    h('p', {}, t('packs.intro')),
    h('p', {}, toProfiles),
    notice,
    packForm,
    profilesTitle,
    h('p', {}, t('profiles.intro')),
    profileForm);

  const lockedReason = () => readOnly || !storeOk || ctx.restarting;
  const controlsDisabled = () => lockedReason() || loading || saving || loadFailed;
  const packsDirty = () => loaded && JSON.stringify(drafts) !== JSON.stringify(baseline);
  const profilesDirty = () => loaded && JSON.stringify(profileDrafts.map(profilePayload)) !== profileInitial;

  function showNotice(text, kind = 'info', extra = null, target = notice) {
    target.className = `banner ${kind}`;
    target.replaceChildren(text, extra ? ' ' : '', extra || '');
    target.hidden = false;
  }

  // Feedback beside the save buttons; focus moves to it so it is announced and in view
  // (the save button itself is disabled once the form is clean).
  function feedback(target, text, kind) {
    for (const other of [packNotice, profileNotice]) if (other !== target) other.hidden = true;
    showNotice(text, kind, null, target);
    target.focus({ preventScroll: true });
  }

  function setDisabled() {
    const off = controlsDisabled();
    for (const control of el.querySelectorAll('input, select, button')) control.disabled = off || control.hasAttribute('data-off');
    // actions that act on the stored state need the packs form to be saved first
    for (const control of packList.querySelectorAll('[data-needs-clean]')) control.disabled = off || packsDirty();
    savePacks.disabled = off || !packsDirty();
    discardPacks.disabled = off || !packsDirty();
    saveProfiles.disabled = off || !profilesDirty();
    discardProfiles.disabled = off || !profilesDirty();
  }

  // -- pack cards ----------------------------------------------------------------------------

  function cellOptions() {
    return cells.map((c) => ({ id: c.entry.id, label: `${cellName(c.entry) || c.entry.id} · ${fmt(nominalAh(c.entry), 'Ah', 1)}`, entry: c.entry }));
  }

  function entryOf(address, cellId) {
    const chosen = cells.find((c) => c.entry.id === cellId);
    if (chosen) return chosen.entry;
    const pack = packs.find((p) => p.address === address);
    return pack && pack.cell && pack.cell.id === cellId ? pack.cell.entry : null;
  }

  function statusOf(address) {
    const list = status && Array.isArray(status.packs) ? status.packs : [];
    return list.find((p) => p.address === address) || null;
  }

  // from the latest 0x42 sample, as GET api/packs reports it (null without a sample)
  function ratedCapacity(address) {
    const pack = packs.find((p) => p.address === address);
    return pack && typeof pack.rated_capacity_ah === 'number' ? pack.rated_capacity_ah : null;
  }

  function kvRow(label, value) {
    return h('div', { class: 'kv' }, h('dt', {}, label), h('dd', {}, value));
  }

  function identityBlock(pack) {
    if (pack.identity === null) return kvRow(t('packs.identity'), t('packs.identity.none'));
    const kind = { verified: 'avail-online', not_verifiable: '', mismatch: 'avail-offline' }[pack.identity];
    return kvRow(t('packs.identity'), h('span', { class: 'chips' }, chip(textOr('packs.identity', pack.identity), kind)));
  }

  // One check list. The <details>/<summary> nodes are created once and kept (focus, open state); a status
  // update only replaces the chip in the summary and the content of the body.
  function checkList(address, key) {
    const memo = `${address}-${key}`;
    const chipSlot = h('span', {});
    const body = h('div', {});
    const details = h('details', { open: openChecks.has(memo), hidden: true },
      h('summary', {}, t(`packs.check.${key}`), ' ', chipSlot), body);
    details.addEventListener('toggle', () => { if (details.open) openChecks.add(memo); else openChecks.delete(memo); });
    return {
      el: details,
      update(group) {
        details.hidden = !group;
        if (!group) return;
        const items = Array.isArray(group.checks) ? group.checks : [];
        chipSlot.replaceChildren(resultChip(group.summary));
        body.replaceChildren(...[
          group.reason ? h('p', { class: 'help' }, textOr('check.reason', group.reason)) : null,
          items.length === 0 ? null : h('ul', { class: 'checks' }, items.map((c) => h('li', {},
            resultChip(c.result), ' ', textOr('check.key', c.key),
            c.value !== null && c.value !== undefined ? ` ${fmt(c.value, c.unit || '', 3)}` : '',
            c.limit !== null && c.limit !== undefined ? ` (${t('packs.check.limit')} ${fmt(c.limit, c.unit || '', 3)})` : '',
            c.reason ? ` – ${textOr('check.reason', c.reason)}` : ''))),
        ].filter((child) => child !== null));
      },
    };
  }

  function checksBlock(address) {
    const pending = h('p', { class: 'help', hidden: true }, t('check.reason.pending'));
    const lists = { live: checkList(address, 'live'), parameters: checkList(address, 'parameters') };
    return { el: h('div', { class: 'stack' }, pending, lists.live.el, lists.parameters.el), pending, lists };
  }

  // the status-driven parts of one pack: the live summary chip and the check lists
  function fillStatus(address) {
    const slot = statusSlots.get(address);
    if (!slot) return;
    const live = statusOf(address);
    const cells = live && live.cells ? live.cells : null;
    slot.chip.replaceChildren(...(cells && cells.assignment && cells.live ? [resultChip(cells.live.summary)] : []));
    if (!slot.checks) return;
    // a saved assignment the main loop has not adopted yet: the old results (if any) are not the saved ones
    slot.checks.pending.hidden = !(cells && cells.pending && !(cells.live && cells.live.reason === 'pending'));
    for (const key of ['live', 'parameters']) slot.checks.lists[key].update(cells ? cells[key] : null);
  }

  function fillAllStatus() {
    for (const address of statusSlots.keys()) fillStatus(address);
  }

  function selectField(label, select, errorKey, help) {
    const id = uid('f-pack');
    select.id = id;
    select.setAttribute('aria-describedby', `${id}-err`);
    if (errorKey) select.setAttribute('aria-invalid', 'true');
    return h('div', { class: 'field' }, h('label', { for: id }, label), select,
      help ? h('p', { class: 'help' }, help) : null,
      h('p', { class: 'field-error', id: `${id}-err`, hidden: !errorKey }, errorKey ? invalidText(errorKey) : ''));
  }

  function packCard(pack) {
    const a = pack.address;
    const d = drafts[a];
    const e = packErrors[a] || {};
    const options = cellOptions();
    const stored = pack.cell;
    const cellSelect = h('select', { 'data-fk': `cell-${a}` },
      h('option', { value: '' }, t('packs.cell.none')),
      options.map((o) => h('option', { value: o.id }, o.label)),
      // a stored snapshot whose type is no longer listed stays selectable (it keeps working)
      d.cellId !== '' && !options.some((o) => o.id === d.cellId) && stored
        ? h('option', { value: d.cellId }, `${cellName(stored.entry) || d.cellId} (${t('packs.cell.snapshot')})`) : null);
    cellSelect.value = d.cellId;
    cellSelect.addEventListener('change', () => {
      d.cellId = cellSelect.value;
      if (d.cellId !== '' && d.parallel === '') d.parallel = '1';
      delete packErrors[a];
      renderPacks(`cell-${a}`);
    });
    const parallelInput = h('input', { type: 'text', inputmode: 'numeric', autocomplete: 'off', 'data-fk': `parallel-${a}` });
    parallelInput.value = d.parallel;
    parallelInput.disabled = d.cellId === '';
    parallelInput.addEventListener('input', () => { d.parallel = parallelInput.value; setDisabled(); });
    const entry = d.cellId === '' ? null : entryOf(a, d.cellId);
    const suggestion = entry ? suggestParallel(ratedCapacity(a), nominalAh(entry)) : null;
    const suggestBlock = suggestion !== null && String(suggestion) !== d.parallel.trim()
      ? h('p', { class: 'help' }, t('packs.parallel.suggest', { n: suggestion, rated: fmt(ratedCapacity(a), 'Ah', 1), nominal: fmt(nominalAh(entry), 'Ah', 1) }), ' ',
        h('button', { class: 'btn small', type: 'button', onClick: () => { d.parallel = String(suggestion); renderPacks(`parallel-${a}`); } }, t('packs.parallel.use')))
      : null;
    const profileSelect = h('select', { 'data-fk': `profile-${a}` },
      h('option', { value: '' }, t('packs.profile.none')),
      profiles.map((p) => h('option', { value: String(p.id) }, p.name)));
    profileSelect.value = d.profile;
    profileSelect.addEventListener('change', () => { d.profile = profileSelect.value; delete packErrors[a]; renderPacks(`profile-${a}`); });

    const offer = stored && stored.update_available
      ? h('div', { class: 'banner warn', role: 'status' },
        h('p', {}, t('packs.update.text', { fields: (stored.changed || []).join(', ') || '–' })),
        h('button', { class: 'btn small', type: 'button', 'data-needs-clean': true, onClick: () => acceptUpdate(a) }, t('packs.update.accept')))
      : null;
    const mismatch = pack.identity === 'mismatch'
      ? h('div', { class: 'banner error', role: 'alert' },
        h('p', {}, t('packs.mismatch.text')),
        pack.live_bms_serial ? h('p', { class: 'muted' }, t('packs.mismatch.live', { serial: pack.live_bms_serial })) : null,
        h('button', { class: 'btn small', type: 'button', 'data-needs-clean': true, onClick: () => confirmIdentity(a) }, t('packs.mismatch.confirm')))
      : null;
    const invalid = stored && !stored.valid ? h('div', { class: 'banner error', role: 'alert' }, t('packs.invalid')) : null;
    const title = `${packName(a)}${pack.configured ? '' : ` (${t('packs.not_configured')})`}`;
    const dirty = JSON.stringify(d) !== JSON.stringify(baseline[a]);
    const id = `pack-card-${a}`;
    const expanded = open.has(a) || Object.keys(e).length > 0;
    const draftEntry = d.cellId === '' ? null : entryOf(a, d.cellId);
    const profileName = d.profile === '' ? null : (profiles.find((p) => String(p.id) === d.profile) || {}).name;
    const summaryText = [
      d.cellId === '' ? t('packs.cell.none') : `${cellName(draftEntry) || d.cellId} × ${d.parallel.trim() || '?'}`,
      `${t('packs.profile')}: ${profileName || t('packs.profile.none')}`,
    ].join(' · ');
    const chipSlot = h('span', { class: 'chips' });
    const checks = expanded ? checksBlock(a) : null;
    statusSlots.set(a, { chip: chipSlot, checks });
    const toggle = h('button', {
      class: 'btn small', type: 'button', 'data-fk': `toggle-${a}`, 'aria-expanded': expanded ? 'true' : 'false',
      'aria-controls': expanded ? `${id}-editor` : null,
      onClick: () => {
        if (open.has(a)) open.delete(a);
        else open.add(a);
        renderPacks(`toggle-${a}`);
      },
    }, t(expanded ? 'sets.collapse' : 'sets.edit'));
    const head = [
      h('div', { class: 'card-head' },
        h('h3', { id }, title),
        h('span', { class: 'chips' },
          dirty ? chip(t('sets.unsaved'), 'avail-unknown') : null,
          pack.identity === 'mismatch' ? chip(t('packs.identity.mismatch'), 'avail-offline') : null,
          stored && stored.update_available ? chip(t('packs.update.chip'), 'avail-unknown') : null,
          stored && !stored.valid ? chip(t('packs.invalid.chip'), 'avail-offline') : null,
          chipSlot)),
      h('p', { class: 'muted' }, summaryText),
    ];
    fillStatus(a);
    if (!expanded) return h('article', { class: 'card pack-row', 'aria-labelledby': id }, head, h('div', { class: 'actions' }, toggle));
    return h('article', { class: 'card stack', 'aria-labelledby': id },
      head,
      h('div', { class: 'actions' }, toggle),
      offer, mismatch, invalid,
      h('dl', { class: 'kvs', id: `${id}-editor` },
        kvRow(t('packs.serial'), orDash(pack.bms_serial || pack.live_bms_serial)),
        identityBlock(pack),
        kvRow(t('packs.series'), orDash(pack.series_count)),
        kvRow(t('packs.rated'), fmt(ratedCapacity(a), 'Ah', 1))),
      selectField(t('packs.cell'), cellSelect, e.cell),
      selectField(t('packs.parallel'), parallelInput, e.parallel, t('packs.parallel.help')),
      suggestBlock,
      selectField(t('packs.profile'), profileSelect, e.profile),
      checks ? checks.el : null,
      h('div', { class: 'actions' }, h('button', {
        class: 'btn small', type: 'button', 'data-fk': `all-${a}`,
        onClick: () => askApplyAll(a),
      }, t('packs.apply_all'))));
  }

  function askApplyAll(source) {
    const targets = packs.filter((p) => p.configured && p.address !== source).map((p) => p.address);
    confirmAll = { source, addresses: targets };
    renderPacks('apply-yes');
  }

  function applyAll() {
    const from = drafts[confirmAll.source];
    for (const a of confirmAll.addresses) drafts[a] = { ...from };
    const n = confirmAll.addresses.length;
    confirmAll = null;
    packErrors = {};
    renderPacks();
    feedback(packNotice, t('packs.apply_all.done', { n }), 'info');
  }

  function applyAllBanner() {
    if (!confirmAll) return null;
    const names = confirmAll.addresses.map(packName).join(', ') || '–';
    return h('div', { class: 'banner warn', role: 'alert' },
      h('p', {}, t('packs.apply_all.confirm', { source: packName(confirmAll.source), packs: names })),
      h('div', { class: 'actions' },
        h('button', { class: 'btn primary', type: 'button', 'data-fk': 'apply-yes', onClick: applyAll }, t('packs.apply_all.go')),
        h('button', { class: 'btn', type: 'button', onClick: () => { const s = confirmAll.source; confirmAll = null; renderPacks(`all-${s}`); } }, t('cells.cancel'))));
  }

  function renderPacks(focusKey) {
    statusSlots.clear();
    const children = [applyAllBanner(), ...packs.map(packCard)].filter((x) => x !== null);
    if (packs.length === 0) children.push(h('p', { class: 'muted', role: 'status' }, loading ? t('boot.loading') : t('packs.empty')));
    packList.replaceChildren(...children);
    setDisabled();
    const target = focusKey ? el.querySelector(`[data-fk="${focusKey}"]`) : null;
    if (target && !target.disabled) target.focus();
  }

  // -- profile editor ------------------------------------------------------------------------

  function editProfiles(change, focus) {
    if (controlsDisabled()) return;
    change();
    profileErrors = [];
    renderProfiles(focus);
  }

  function profileField(d, i, key, kind) {
    const id = uid('f-prof');
    const err = (profileErrors[i] || {})[key];
    let input;
    if (kind === 'when') {
      input = h('select', {}, WHENS.map((w) => h('option', { value: w }, t(`profiles.when.${w}`))));
    } else {
      input = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', inputmode: key === 'name' ? null : 'decimal' });
    }
    input.value = d[key];
    input.id = id;
    input.setAttribute('aria-describedby', `${id}-help ${id}-err`);
    if (err) input.setAttribute('aria-invalid', 'true');
    input.dataset.fk = `${key}-${d.key}`;
    input.addEventListener(kind === 'when' ? 'change' : 'input', () => { d[key] = input.value; setDisabled(); });
    return h('div', { class: 'field' },
      h('label', { for: id }, t(`profiles.f.${key}`)), input,
      h('p', { class: 'help', id: `${id}-help` }, t(`profiles.f.${key}.help`)),
      h('p', { class: 'field-error', id: `${id}-err`, hidden: !err }, err ? invalidText(err) : ''));
  }

  function profileCard(d, i) {
    const users = d.base ? packs.filter((p) => p.balancer_profile === d.base.id).map((p) => p.address) : [];
    return h('fieldset', { class: 'card stack' },
      h('legend', {}, d.name.trim() || t('profiles.new_name')),
      users.length > 0 ? h('p', { class: 'help' }, t('profiles.used_by', { packs: users.map(packName).join(', ') })) : null,
      profileField(d, i, 'name', 'text'),
      h('div', { class: 'form-grid' },
        profileField(d, i, 'trigger_mv', 'mv'), profileField(d, i, 'stop_mv', 'mv'),
        profileField(d, i, 'start_mv', 'mv'), profileField(d, i, 'stop_cell_mv', 'mv')),
      profileField(d, i, 'when', 'when'),
      profileField(d, i, 'max_a', 'a'),
      h('div', { class: 'actions' },
        h('button', { class: 'btn small danger', type: 'button', onClick: () => editProfiles(() => profileDrafts.splice(i, 1), 'add') }, t('profiles.delete'))));
  }

  function renderProfiles(focus) {
    const cards = profileDrafts.map(profileCard);
    profileList.replaceChildren(...(cards.length > 0 ? cards : [h('p', { class: 'muted' }, loaded ? t('profiles.empty') : t('boot.loading'))]));
    setDisabled();
    if (focus === 'last') {
      const inputs = profileList.querySelectorAll('input');
      const first = profileDrafts.length > 0 ? profileList.querySelector(`[data-fk="name-${profileDrafts[profileDrafts.length - 1].key}"]`) : null;
      if (first) first.focus();
      else if (inputs.length > 0) inputs[0].focus();
    } else if (focus === 'add') {
      addProfile.focus();
    }
  }

  // -- load / save ---------------------------------------------------------------------------

  function resetPackDrafts() {
    drafts = {};
    for (const p of packs) {
      drafts[p.address] = {
        cellId: p.cell ? p.cell.id : '',
        parallel: p.cell ? String(p.cell.parallel) : '',
        profile: p.balancer_profile === null || p.balancer_profile === undefined ? '' : String(p.balancer_profile),
      };
    }
    baseline = JSON.parse(JSON.stringify(drafts));
    packErrors = {};
    confirmAll = null;
    renderPacks();
  }

  function resetProfileDrafts() {
    profileDrafts = profiles.map(profileDraft);
    profileInitial = JSON.stringify(profileDrafts.map(profilePayload));
    profileErrors = [];
  }

  function adoptAll(packsData, profilesData, cellsData, statusData) {
    packs = packsData.packs;
    profiles = profilesData.profiles;
    cells = cellsData.cells;
    if (statusData !== null) status = statusData.status ? statusData.status : null;      // null: a newer answer is in
    revision = Math.min(packsData.revision, profilesData.revision, cellsData.revision);    // the oldest: a stale save gets a 412
    loaded = true;
    resetPackDrafts();
    resetProfileDrafts();
    renderProfiles();
  }

  async function load(notes = null) {
    if (saving) return;
    const mine = ++generation;
    const statusMine = ++statusSeq;
    notice.hidden = notes === null;
    if (notes !== null) showNotice(notes, 'warn');
    loadFailed = false;
    if (!storeOk) {
      loaded = true;
      renderPacks();
      renderProfiles();
      return;
    }
    loading = true;
    renderPacks();
    try {
      const [p, pr, c, s] = await Promise.all([
        api('GET', 'api/packs'), api('GET', 'api/balancer-profiles'), api('GET', 'api/cells'),
        api('GET', 'api/status').catch(() => null),
      ]);
      if (mine !== generation) return;
      loading = false;
      adoptAll(p, pr, c, statusMine === statusSeq || status === null ? s : null);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (mine !== generation) return;
      loading = false;
      loadFailed = true;
      showNotice(errorText(error), 'error', h('button', { class: 'btn small', type: 'button', onClick: () => load() }, t('sets.reload')));
      setDisabled();
    }
  }

  // One write. `onOk(data)` runs with the answer; every failure is shown here and returns false.
  async function write(button, send, onFail, target = packNotice) {
    if (controlsDisabled()) return false;
    notice.hidden = true;
    packNotice.hidden = true;
    profileNotice.hidden = true;
    generation += 1;
    statusSeq += 1;
    loading = false;
    saving = true;
    setDisabled();
    busy(button, true);
    try {
      const data = await send();
      revision = data.revision;
      return data;
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      // not busy any more before anything is drawn or focused: a disabled control would lose the focus
      saving = false;
      if (error.key === 'revision_stale') {
        await load(t('packs.stale'));
      } else if (onFail && onFail(error)) {
        // shown by the caller
      } else {
        feedback(target, errorText(error), 'error');
      }
      return false;
    } finally {
      saving = false;
      busy(button, false);
      setDisabled();
    }
  }

  async function savePackAssignments(event) {
    event.preventDefault();
    if (controlsDisabled() || !packsDirty()) return;
    packErrors = {};
    for (const [address, d] of Object.entries(drafts)) {
      if (JSON.stringify(d) === JSON.stringify(baseline[address])) continue;
      if (d.cellId !== '' && parseParallel(d.parallel) === null) packErrors[address] = { parallel: 'range' };
    }
    if (Object.keys(packErrors).length > 0) {
      renderPacks();
      const first = packList.querySelector('[aria-invalid="true"]');
      showNotice(t('sets.fix_errors'), 'error', null, packNotice);
      if (first) first.focus();
      return;
    }
    const sent = packsPayload(drafts, baseline);
    const data = await write(savePacks, () => api('PUT', 'api/packs', { packs: sent }, { revision }), (error) => {
      if (error.key !== 'invalid_fields') return false;
      packErrors = {};
      for (const item of error.params.fields || []) {
        const match = PACK_PATH.exec(String(item.path));
        if (!match) continue;
        const field = match[2] === 'cell.parallel' ? 'parallel' : match[2] === 'balancer_profile' ? 'profile' : 'cell';
        packErrors[match[1]] = { ...(packErrors[match[1]] || {}), [field]: item.key };
      }
      renderPacks();
      showNotice(errorText(error), 'error', null, packNotice);
      const first = packList.querySelector('[aria-invalid="true"]');
      if (first) first.focus();
      return true;
    });
    if (data === false) return;
    // the answer is the new stored state; the drafts of this form equal it, nothing is dropped
    packs = data.packs;
    resetPackDrafts();
    feedback(packNotice, t('packs.saved'), 'ok');
    refreshStatus();
  }

  async function refreshStatus() {
    if (destroyed) return;
    const mine = ++statusSeq;
    try {
      const data = await api('GET', 'api/status');
      if (destroyed || mine !== statusSeq) return;      // a newer request, a reload or a save has overtaken this answer
      status = data.status;
      fillAllStatus();               // only the status slots: no form is rebuilt
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => { if (document.visibilityState === 'visible') refreshStatus(); }, POLL_MS);
  }

  function stopPolling() {
    if (pollTimer !== null) clearInterval(pollTimer);
    pollTimer = null;
  }

  function onVisibility() {
    if (document.visibilityState === 'visible') { refreshStatus(); startPolling(); } else stopPolling();
  }

  async function acceptUpdate(address) {
    const data = await write(null, () => api('POST', 'api/packs/accept-update', { addresses: [address] }, { revision }));
    if (data === false) return;
    packs = data.packs;
    resetPackDrafts();
    feedback(packNotice, t('packs.update.done', { pack: packName(address) }), 'ok');
  }

  async function confirmIdentity(address) {
    const data = await write(null, () => api('POST', 'api/packs/confirm-identity', { address }, { revision }));
    if (data === false) return;
    packs = data.packs;
    resetPackDrafts();
    feedback(packNotice, t('packs.mismatch.done', { pack: packName(address) }), 'ok');
  }

  function showProfileErrors(fields) {
    profileErrors = profileDrafts.map(() => ({}));
    for (const item of fields) {
      const match = PROFILE_PATH.exec(String(item.path));
      const row = match ? profileErrors[Number(match[1])] : undefined;
      if (!row) continue;
      const field = PROFILE_FIELDS.includes(match[2]) ? match[2] : 'name';
      if (!row[field]) row[field] = item.key;
    }
    renderProfiles();
  }

  function focusFirstProfileError() {
    const first = profileList.querySelector('[aria-invalid="true"]');
    if (first) first.focus();
  }

  async function saveProfileList(event) {
    event.preventDefault();
    if (controlsDisabled() || !profilesDirty()) return;
    profileErrors = checkProfiles(profileDrafts);
    if (profileErrors.some((e) => Object.keys(e).length > 0)) {
      renderProfiles();
      showNotice(t('sets.fix_errors'), 'error', null, profileNotice);
      focusFirstProfileError();
      return;
    }
    const data = await write(saveProfiles, () => api('PUT', 'api/balancer-profiles', { profiles: profileDrafts.map(profilePayload) }, { revision }), (error) => {
      if (error.key === 'invalid_fields') {
        showProfileErrors(error.params.fields || []);
        showNotice(errorText(error), 'error', null, profileNotice);
        focusFirstProfileError();
        return true;
      }
      if (error.key === 'in_use') {
        const items = error.params.items || [];
        feedback(profileNotice, items.map((i) => t('profiles.in_use', { packs: (i.addresses || []).map(packName).join(', ') })).join(' '), 'error');
        return true;
      }
      return false;
    }, profileNotice);
    if (data === false) return;
    profiles = data.profiles;
    // a pack draft that pointed at a profile which no longer exists must not survive silently
    const known = new Set(profiles.map((p) => String(p.id)));
    for (const d of Object.values(drafts)) if (d.profile !== '' && !known.has(d.profile)) d.profile = '';
    resetProfileDrafts();
    renderPacks();
    renderProfiles();
    feedback(profileNotice, t('profiles.saved'), 'ok');
  }

  packForm.addEventListener('submit', savePackAssignments);
  profileForm.addEventListener('submit', saveProfileList);
  renderPacks();
  renderProfiles();
  load();
  if (storeOk) {
    document.addEventListener('visibilitychange', onVisibility);
    startPolling();
  }

  return {
    el,
    dirty: () => !lockedReason() && (packsDirty() || profilesDirty()),
    discardDraft() { baseline = JSON.parse(JSON.stringify(drafts)); profileInitial = JSON.stringify(profileDrafts.map(profilePayload)); },
    destroy() {
      destroyed = true;
      stopPolling();
      document.removeEventListener('visibilitychange', onVisibility);
    },
  };
}
