// Sets tab: parallel sets (GET/PUT api/sets with If-Match). One draft list, saved as a whole.

import { api, ApiError } from './api.js';
import { t, errorText, packName } from './i18n.js';
import { h, uid, busy, chip } from './ui.js';
import { storeBanner } from './overview.js';

const WIRINGS = ['diagonal', 'common_busbar', 'unknown'];
const FIELDS = ['name', 'packs', 'wiring', 'plus', 'minus'];
const MAX_NAME = 40;
const DASH = '–';
// server problem paths: parallel_sets[2].plus, parallel_sets[2].packs[1], parallel_sets[2]
const SERVER_PATH = /^parallel_sets\[(\d+)\](?:\.(\w+)(?:\[\d+\])?)?$/;

// `open`: the form of a stored set is shown (new sets and sets with errors are always open);
// `saved`: the payload as loaded, to mark a collapsed set that has unsaved edits.
function toDraft(set) {
  const { active, reason, ...base } = set;        // what the server computed is not part of the set
  const d = {
    key: uid('set'), base, active, reason, open: false, saved: null,
    name: set.name, packs: [...set.packs], wiring: set.wiring, plus: set.plus, minus: set.minus,
  };
  d.saved = JSON.stringify(toPayload(d));
  return d;
}

function newDraft(name, packs) {
  return {
    key: uid('set'), base: null, active: null, reason: null, open: true, saved: null,
    name, packs, wiring: 'unknown', plus: null, minus: null,
  };
}

// Swap two members of the ordered list; false (and nothing changed) if either index is outside it.
export function moveMember(packs, from, to) {
  const inside = (n) => Number.isInteger(n) && n >= 0 && n < packs.length;
  if (!inside(from) || !inside(to)) return false;
  [packs[from], packs[to]] = [packs[to], packs[from]];
  return true;
}

// The set as the API takes it: unknown keys and the id of a stored set travel back unchanged.
function toPayload(d) {
  return { ...(d.base || {}), name: d.name.trim(), packs: d.packs, wiring: d.wiring, plus: d.plus, minus: d.minus };
}

// Client-side checks mirror the server model (which stays authoritative): one error key per field.
export function checkDraft(draft) {
  const errors = draft.map(() => ({}));
  const names = new Set();
  const used = new Set();
  draft.forEach((d, i) => {
    const e = errors[i];
    const name = d.name.trim();
    if (!name || [...name].length > MAX_NAME) e.name = 'range';
    else if (names.has(name.toLowerCase())) e.name = 'duplicate';
    names.add(name.toLowerCase());
    if (d.packs.length < 2) e.packs = 'range';
    else if (d.packs.some((p) => used.has(p))) e.packs = 'duplicate';
    d.packs.forEach((p) => used.add(p));
    for (const tap of ['plus', 'minus']) {
      if (d[tap] === null) {
        if (d.wiring === 'diagonal') e[tap] = 'required';
      } else if (!d.packs.includes(d[tap])) {
        e[tap] = 'value';
      }
    }
    if (d.wiring === 'diagonal' && d.plus !== null && d.plus === d.minus) {
      e.plus = 'value';
      e.minus = 'value';
    }
  });
  return errors;
}

export function setsView(ctx) {
  const session = ctx.session;
  const readOnly = !session.user;
  const storeOk = session.store.status === 'valid';
  let draft = [];
  let errors = [];
  let packCount = 1;
  let revision = null;
  let initial = null;
  let loaded = false;
  // same request discipline as the settings tab: a load or save in flight freezes the controls and
  // every request bumps the generation, so an outdated answer is ignored entirely
  let generation = 0;
  let loading = false;
  let saving = false;
  let loadFailed = false;

  const notice = h('div', { class: 'banner', role: 'status', hidden: true });
  const list = h('div', { class: 'stack' });
  const addButton = h('button', { class: 'btn', type: 'button', onClick: () => edit(() => draft.push(newDraft('', []))) }, t('sets.add'));
  const proposeButton = h('button', { class: 'btn', type: 'button', onClick: propose }, t('sets.propose'));
  const saveButton = h('button', { class: 'btn primary', type: 'submit' }, t('sets.save'));
  const resetButton = h('button', { class: 'btn', type: 'button', onClick: () => load() }, t('sets.discard'));
  const form = h('form', { class: 'stack', novalidate: true }, list,
    h('div', { class: 'actions' }, addButton, proposeButton, saveButton, resetButton));
  const singleHint = h('div', { class: 'banner info', role: 'status', hidden: true }, t('sets.single_pack_hint'));
  const el = h('section', { 'aria-labelledby': 'view-title' },
    h('h2', { id: 'view-title', tabindex: '-1' }, t('tab.sets')),
    storeBanner(session),
    readOnly ? h('div', { class: 'banner info', role: 'status' }, t('readonly.hint')) : null,
    h('p', {}, t('sets.intro')),
    singleHint,
    notice,
    form);

  const lockedReason = () => readOnly || !storeOk || ctx.restarting;
  const controlsDisabled = () => lockedReason() || loading || saving || loadFailed;
  const serialise = () => JSON.stringify(draft.map(toPayload));

  // `data-off` marks a control that is disabled on its own (first member up, last member down):
  // unlocking the form must not enable it. Save and discard also wait for an edited draft.
  function setDisabled(disabled) {
    for (const control of form.querySelectorAll('input, select, button')) {
      control.disabled = disabled || control.hasAttribute('data-off');
    }
    const clean = !loaded || serialise() === initial;
    saveButton.disabled = disabled || clean;
    resetButton.disabled = disabled || clean;
  }

  function showNotice(text, kind = 'info', extra = null) {
    notice.className = `banner ${kind}`;
    notice.replaceChildren(text, extra ? ' ' : '', extra || '');
    notice.hidden = false;
  }

  function reloadHint() {
    return h('button', { class: 'btn small', type: 'button', onClick: () => load() }, t('sets.reload'));
  }

  // every structural edit: change the draft, forget old error marks, draw again, keep the focus
  function edit(change, focusKey) {
    if (controlsDisabled()) return;
    change();
    errors = [];
    render(focusKey);
  }

  function freePacks() {
    const used = new Set(draft.flatMap((d) => d.packs));
    return Array.from({ length: packCount }, (_unused, n) => n).filter((n) => !used.has(n));
  }

  function propose() {
    if (controlsDisabled()) return;
    const free = new Set(freePacks());
    const names = new Set(draft.map((d) => d.name.trim().toLowerCase()));
    let proposed = 0;
    let number = 0;
    edit(() => {
      for (let a = 0; a + 1 < packCount; a += 2) {
        if (!free.has(a) || !free.has(a + 1)) continue;
        do number += 1; while (names.has(t('sets.pair_name', { n: number }).toLowerCase()));
        draft.push(newDraft(t('sets.pair_name', { n: number }), [a, a + 1]));
        proposed += 1;
      }
    });
    if (proposed === 0) showNotice(t('sets.propose_none'), 'info');
    else showNotice(t('sets.proposed', { n: proposed }), 'info');
  }

  function textField(label, help, errorKey, input, fieldErrors) {
    const id = uid('f-sets');
    input.id = id;
    input.setAttribute('aria-describedby', `${id}-help ${id}-err`);
    if (fieldErrors[errorKey]) input.setAttribute('aria-invalid', 'true');
    return h('div', { class: 'field' },
      h('label', { for: id }, label), input,
      help ? h('p', { class: 'help', id: `${id}-help` }, help) : null,
      h('p', { class: 'field-error', id: `${id}-err`, hidden: !fieldErrors[errorKey] },
        fieldErrors[errorKey] ? t(`invalid.${fieldErrors[errorKey]}`) : ''));
  }

  function tapSelect(d, tap) {
    const select = h('select', { 'data-fk': `${tap}-${d.key}` },
      h('option', { value: '' }, DASH),
      d.packs.map((p) => h('option', { value: String(p) }, packName(p))));
    select.value = d[tap] === null ? '' : String(d[tap]);
    select.addEventListener('change', () => edit(() => { d[tap] = select.value === '' ? null : Number(select.value); }, select.dataset.fk));
    return select;
  }

  function memberRow(d, p, j) {
    const swap = (to) => edit(() => { moveMember(d.packs, j, to); }, `move-${d.key}-${p}`);
    return h('li', { class: 'member' },
      chip(`${j + 1}. ${packName(p)}`),
      h('button', {
        class: 'btn small', type: 'button', 'data-off': j === 0, 'aria-label': t('sets.member.up', { pack: packName(p) }),
        'data-fk': `move-${d.key}-${p}`, onClick: () => swap(j - 1),
      }, '↑'),
      h('button', {
        class: 'btn small', type: 'button', 'data-off': j === d.packs.length - 1,
        'aria-label': t('sets.member.down', { pack: packName(p) }), onClick: () => swap(j + 1),
      }, '↓'),
      h('button', {
        class: 'btn small', type: 'button', 'aria-label': t('sets.member.remove', { pack: packName(p) }),
        onClick: () => edit(() => {
          d.packs.splice(j, 1);
          if (d.plus === p) d.plus = null;
          if (d.minus === p) d.minus = null;
        }, `add-${d.key}`),
      }, '✕'));
  }

  function summaryLine(d) {
    const taps = d.wiring === 'diagonal'
      ? t('sets.summary.taps', { plus: d.plus === null ? DASH : packName(d.plus), minus: d.minus === null ? DASH : packName(d.minus) })
      : null;
    return [d.packs.map(packName).join(' → ') || DASH, t(`sets.wiring.${d.wiring}`), taps].filter((x) => x).join(' · ');
  }

  function stateChip(d) {
    return d.active === null ? null : chip(t(d.active ? 'sets.state.active' : 'sets.state.inactive'), d.active ? 'avail-online' : 'avail-unknown');
  }

  // a stored set without errors is shown as its summary; unsaved edits stay in the draft
  function collapsedCard(d) {
    const edited = JSON.stringify(toPayload(d)) !== d.saved;
    return h('article', { class: 'card set' },
      h('div', { class: 'card-head' },
        h('h3', {}, d.name.trim() || t('sets.new_name')),
        h('span', { class: 'chips' }, stateChip(d), edited ? chip(t('sets.unsaved'), 'avail-unknown') : null)),
      h('p', { class: 'muted' }, summaryLine(d)),
      d.active === false ? h('p', { class: 'help' }, t(`sets.reason.${d.reason}`)) : null,
      h('div', { class: 'actions' },
        h('button', {
          class: 'btn small', type: 'button', 'data-fk': `edit-${d.key}`,
          onClick: () => edit(() => { d.open = true; }, `name-${d.key}`),
        }, t('sets.edit'))));
  }

  function setCard(d, i) {
    const e = errors[i] || {};
    const hasErrors = Object.keys(e).length > 0;
    if (!d.open && !hasErrors) return collapsedCard(d);
    const nameInput = h('input', { type: 'text', autocomplete: 'off', spellcheck: 'false', 'data-fk': `name-${d.key}` });
    nameInput.value = d.name;
    nameInput.addEventListener('input', () => { d.name = nameInput.value; setDisabled(controlsDisabled()); });
    const free = freePacks();
    const addId = uid('f-add');
    const addSelect = h('select', { id: addId, 'data-fk': `add-${d.key}` },
      h('option', { value: '' }, free.length > 0 ? t('sets.add_pack') : t('sets.add_pack_none')),
      free.map((p) => h('option', { value: String(p) }, packName(p))));
    addSelect.addEventListener('change', () => {
      if (addSelect.value !== '') edit(() => d.packs.push(Number(addSelect.value)), addSelect.dataset.fk);
    });
    const wiringSelect = h('select', { 'data-fk': `wiring-${d.key}` },
      WIRINGS.map((w) => h('option', { value: w }, t(`sets.wiring.${w}`))));
    wiringSelect.value = d.wiring;
    wiringSelect.addEventListener('change', () => edit(() => { d.wiring = wiringSelect.value; }, wiringSelect.dataset.fk));
    const membersId = uid('members');
    return h('fieldset', { class: 'card set', 'data-set': String(i) },
      h('legend', {}, d.name.trim() || t('sets.new_name')),
      h('div', { class: 'card-head' },
        h('p', { class: 'muted' }, summaryLine(d)),
        stateChip(d)),
      d.active === false ? h('p', { class: 'help' }, t(`sets.reason.${d.reason}`)) : null,
      e.set ? h('p', { class: 'field-error' }, t(`invalid.${e.set}`)) : null,
      textField(t('sets.name.label'), t('sets.name.help'), 'name', nameInput, e),
      h('div', { class: 'field', role: 'group', 'aria-labelledby': membersId },
        h('span', { id: membersId, class: 'label' }, t('sets.members.label')),
        d.packs.length > 0 ? h('ol', { class: 'members' }, d.packs.map((p, j) => memberRow(d, p, j))) : h('p', { class: 'muted' }, t('sets.members.none')),
        h('label', { for: addId, class: 'label' }, t('sets.add_pack_label')),
        addSelect,
        h('p', { class: 'help' }, t('sets.members.help')),
        h('p', { class: 'field-error', hidden: !e.packs }, e.packs ? t(`invalid.${e.packs}`) : '')),
      textField(t('sets.wiring.label'), t(`sets.wiring.${d.wiring}.help`), 'wiring', wiringSelect, e),
      h('div', { class: 'taps' },
        textField(t('sets.plus.label'), null, 'plus', tapSelect(d, 'plus'), e),
        textField(t('sets.minus.label'), null, 'minus', tapSelect(d, 'minus'), e)),
      h('p', { class: 'help' }, t('sets.taps.help')),
      h('div', { class: 'actions' },
        d.base === null ? null : h('button', {
          class: 'btn small', type: 'button', 'data-off': hasErrors, onClick: () => edit(() => { d.open = false; }, `edit-${d.key}`),
        }, t('sets.collapse')),
        h('button', { class: 'btn small danger', type: 'button', onClick: () => edit(() => draft.splice(i, 1), 'add-set') }, t('sets.delete'))));
  }

  function render(focusKey) {
    singleHint.hidden = packCount > 1;
    const children = draft.map(setCard);
    if (children.length === 0 && loading) children.push(h('p', { class: 'muted', role: 'status' }, t('boot.loading')));
    else if (children.length === 0 && loaded) children.push(h('p', { class: 'muted' }, t('sets.empty')));
    list.replaceChildren(...children);
    setDisabled(controlsDisabled());
    const target = focusKey ? list.querySelector(`[data-fk="${focusKey}"]`) : null;
    if (target && !target.disabled) target.focus();
    else if (focusKey === 'add-set') addButton.focus();
  }

  async function load() {
    if (saving) return;
    const mine = ++generation;
    notice.hidden = true;
    errors = [];
    loadFailed = false;
    if (!storeOk) {
      render();
      return;
    }
    loading = true;
    render();
    let data;
    try {
      data = await api('GET', 'api/sets');
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (mine !== generation) return;
      loading = false;
      loadFailed = true;
      showNotice(errorText(error), 'error', reloadHint());
      setDisabled(true);
      return;
    }
    if (mine !== generation) return;
    loading = false;
    adopt(data);
  }

  // the draft is exactly what the server answered (after a load and after a successful save)
  function adopt(data) {
    packCount = data.pack_count;
    revision = data.revision;
    draft = data.sets.map(toDraft);
    initial = serialise();
    loaded = true;
    render();
  }

  // a set with a problem stays open (also after the marks are cleared by the next edit)
  function openFailed() {
    draft.forEach((d, i) => { if (errors[i] && Object.keys(errors[i]).length > 0) d.open = true; });
  }

  function showServerErrors(fields) {
    errors = draft.map(() => ({}));
    for (const item of fields) {
      const match = SERVER_PATH.exec(String(item.path));
      const row = match ? errors[Number(match[1])] : undefined;
      if (!row) continue;
      const field = FIELDS.includes(match[2]) ? match[2] : 'set';
      if (!row[field]) row[field] = item.key;
    }
    openFailed();
    render();
    const first = list.querySelector('[aria-invalid="true"]');
    if (first) first.focus();
  }

  async function save(event) {
    event.preventDefault();
    if (controlsDisabled()) return;
    notice.hidden = true;
    errors = checkDraft(draft);
    if (errors.some((e) => Object.keys(e).length > 0)) {
      openFailed();
      render();
      const first = list.querySelector('[aria-invalid="true"]');
      if (first) first.focus();
      showNotice(t('sets.fix_errors'), 'error');
      return;
    }
    generation += 1;
    loading = false;
    saving = true;
    setDisabled(true);
    busy(saveButton, true);
    try {
      const result = await api('PUT', 'api/sets', { sets: draft.map(toPayload) }, { revision });
      adopt(result);
      showNotice(t('sets.saved'), 'ok');
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (error.key === 'revision_stale') {
        showNotice(t('err.revision_stale'), 'warn', reloadHint());
      } else if (error.key === 'invalid_fields') {
        showServerErrors(error.params.fields || []);
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

  form.addEventListener('submit', save);
  render();
  setDisabled(true);
  load();

  return {
    el,
    dirty: () => loaded && !lockedReason() && serialise() !== initial,
    discardDraft() { initial = serialise(); },
    destroy() {},
  };
}
