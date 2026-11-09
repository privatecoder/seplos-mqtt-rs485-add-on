// Overview tab: live status of the add-on, polled every 5 s while the page is visible.

import { api, ApiError } from './api.js';
import { t, errorText, fmt, fmtDuration, orDash, packName } from './i18n.js';
import { h, chip } from './ui.js';
import { resultChip, balancerChip } from './cellui.js';

const POLL_MS = 5000;

export function storeBanner(session) {
  const store = session && session.store;
  if (!store || store.status === 'valid') return null;
  return h('div', { class: 'banner warn', role: 'status' },
    h('strong', {}, t(`store.${store.status}.title`)),
    ' ',
    t(`store.${store.status}.text`),
    store.problem ? h('span', { class: 'muted' }, ` (${store.problem})`) : null);
}

function fact(label, value) {
  return h('li', {}, h('span', { class: 'muted' }, label), ' ', value instanceof Node ? value : h('strong', {}, value));
}

function kv(label, value) {
  return h('div', { class: 'kv' }, h('dt', {}, label), h('dd', {}, value));
}

function availabilityChip(value) {
  const known = value === 'online' || value === 'offline';
  return chip(t(`avail.${known ? value : 'unknown'}`), known ? `avail-${value}` : 'avail-unknown');
}

// MAINT row: the version when present, "being determined" while pending; no row for OEM firmware
// or with MAINT reads switched off.
function maintRow(pack) {
  if (pack.maint === 'present') return kv(t('pack.maint'), orDash(pack.maint_version));
  if (pack.maint === 'pending') return kv(t('pack.maint'), t('maint.pending'));
  return null;
}

// The most precise known base firmware: the derived patch level, else major.minor.
function firmware(pack) {
  return orDash(pack.firmware_patch || pack.firmware_version || null);
}

function snapshotAge(status) {
  const generated = Date.parse(status.generated_at);
  return Number.isNaN(generated) ? null : Math.max(0, (Date.now() - generated) / 1000);
}

function headerLine(status) {
  const bridge = ['alive', 'dead', 'none'].includes(status.bridge) ? status.bridge : 'none';
  return h('ul', { class: 'facts' },
    fact(t('overview.mode'), t(`mode.${status.mode === 'multi' ? 'multi' : 'single'}`)),
    fact(t('overview.mqtt'), chip(t(status.mqtt_connected ? 'mqtt.connected' : 'mqtt.disconnected'), status.mqtt_connected ? 'avail-online' : 'avail-offline')),
    fact(t('overview.bridge'), chip(t(`bridge.${bridge}`), bridge === 'alive' ? 'avail-online' : bridge === 'dead' ? 'avail-offline' : '')),
    fact(t('overview.last_cycle'), fmtDuration(status.last_cycle_s)),
    fact(t('overview.snapshot_age'), fmtDuration(snapshotAge(status))));
}

// Setup mode: the add-on could not open its BMS connection and waits for a saved one.
function connectionBanner(reason) {
  return h('div', { class: 'banner error', role: 'alert' },
    h('strong', {}, t('overview.no_connection')),
    ' ', reason, ' ',
    h('a', { href: '#/settings' }, t('overview.check_settings')));
}

const SYSTEM_STATES = ['Charging', 'Discharging', 'Idle', 'Off'];

// Known states are translated; an unknown protocol value (for example "0x40") is shown as it is.
function systemState(value) {
  return SYSTEM_STATES.includes(value) ? t(`system.state_value.${value}`) : orDash(value);
}

function systemCard(system) {
  return h('article', { class: 'card', 'aria-labelledby': 'system-title' },
    h('div', { class: 'card-head' }, h('h3', { id: 'system-title' }, t('system.title'))),
    system === null
      ? h('p', { class: 'muted' }, t('system.waiting'))
      : h('dl', { class: 'kvs' },
        kv(t('system.soc'), fmt(system.system_soc, '%', 1)),
        kv(t('system.voltage'), fmt(system.system_voltage, 'V', 1)),
        kv(t('system.current'), fmt(system.system_current, 'A', 1)),
        kv(t('system.power'), fmt(system.system_power, 'W', 0)),
        kv(t('system.capacity'), fmt(system.total_capacity, 'Ah', 1)),
        kv(t('system.energy'), fmt(system.total_full_energy, 'kWh', 2)),
        kv(t('system.state'), systemState(system.system_state))));
}

// A set value with the pack it comes from: "3,315 V (Pack 01)"; unknown stays a plain dash.
function withPack(text, pack) {
  return pack === null || pack === undefined || text === '–' ? text : `${text} (${packName(pack)})`;
}

// `snapshotSeconds`: time since the snapshot was built; the sample age was measured then.
function setCard(set, snapshotSeconds) {
  const id = `set-title-${set.id}`;
  const v = set.values;
  let body;
  if (!set.active) {
    body = h('p', { class: 'muted' }, t(`sets.reason.${set.reason}`));
  } else if (v === null || v === undefined) {
    body = h('p', { class: 'muted' }, t('sets.waiting'));
  } else {
    body = h('dl', { class: 'kvs' },
      kv(t('sets.val.current'), fmt(v.current, 'A', 2)),
      kv(t('sets.val.power'), fmt(v.power, 'W', 1)),
      kv(t('sets.val.soc'), fmt(v.soc, '%', 1)),
      kv(t('sets.val.voltage_median'), fmt(v.voltage_median, 'V', 2)),
      kv(t('sets.val.voltage_range'), fmt(v.voltage_range, 'V', 3)),
      kv(t('sets.val.cell_high'), withPack(fmt(v.highest_cell_voltage, 'V', 3), v.highest_cell_voltage_pack)),
      kv(t('sets.val.cell_low'), withPack(fmt(v.lowest_cell_voltage, 'V', 3), v.lowest_cell_voltage_pack)),
      kv(t('sets.val.temp_high'), withPack(fmt(v.highest_cell_temperature, '°C', 1), v.highest_cell_temperature_pack)),
      kv(t('sets.val.temp_low'), withPack(fmt(v.lowest_cell_temperature, '°C', 1), v.lowest_cell_temperature_pack)),
      kv(t('sets.val.members'), `${orDash(v.members_reporting)} / ${set.packs.length}`),
      kv(t('sets.val.age'), fmtDuration(typeof v.sample_age === 'number' ? v.sample_age + (snapshotSeconds ?? 0) : null)));
  }
  return h('article', { class: 'card', 'aria-labelledby': id },
    h('div', { class: 'card-head' },
      h('h3', { id }, set.name),
      chip(t(set.active ? 'sets.state.active' : 'sets.state.inactive'), set.active ? 'avail-online' : 'avail-unknown')),
    body);
}

// The age in the snapshot was measured when it was built: add the time that passed since.
function lastSuccessAge(pack, snapshotSeconds) {
  if (pack.last_success_age_s === null || pack.last_success_age_s === undefined) return null;
  return pack.last_success_age_s + (snapshotSeconds ?? 0);
}

function packCard(pack, multi, snapshotSeconds) {
  const title = `${packName(pack.address)}${multi && pack.address === 0 ? ` ${t('pack.master')}` : ''}`;
  const id = `pack-title-${pack.address}`;
  return h('article', { class: 'card pack', 'aria-labelledby': id },
    h('div', { class: 'card-head' }, h('h3', { id }, title), availabilityChip(pack.availability)),
    h('dl', { class: 'kvs' },
      kv(t('pack.last_success'), fmtDuration(lastSuccessAge(pack, snapshotSeconds))),
      kv(t('pack.device'), orDash(pack.device_name)),
      kv(t('pack.firmware'), firmware(pack)),
      kv(t('pack.cells'), orDash(pack.cell_count)),
      maintRow(pack),
      kv(t('pack.soc'), fmt(pack.state_of_charge, '%', 1)),
      kv(t('pack.voltage'), fmt(pack.total_pack_voltage, 'V', 2)),
      kv(t('pack.current'), fmt(pack.current, 'A', 2)),
      kv(t('pack.cell_high'), fmt(pack.highest_cell_voltage, 'V', 3)),
      kv(t('pack.cell_low'), fmt(pack.lowest_cell_voltage, 'V', 3)),
      ...cellRows(pack)));
}

// Cell type and live check summary (only with an assignment); balancer state only with a profile.
function cellRows(pack) {
  const cells = pack.cells;
  const assignment = cells && cells.assignment;
  const rows = [];
  if (assignment) {
    rows.push(kv(t('pack.cell_type'), `${assignment.name || assignment.id} × ${assignment.parallel}`));
    rows.push(kv(t('pack.datasheet_check'), cells.live ? resultChip(cells.live.summary) : resultChip('not_evaluated')));
  } else {
    rows.push(kv(t('pack.cell_type'), t('pack.cell_type.none')));
  }
  if (pack.balancer) rows.push(kv(t('pack.balancer'), balancerChip(pack.balancer.state)));
  return rows;
}

// OpenBMS Monitor feed: the switch, the optional automatic switch-off and what it changes while on.
const AUTO_OFF_HOURS = ['', '1', '4', '24'];

function feedCard(feed, lastCycle, ctx, state, onSwitch) {
  const readOnly = !ctx.session.user;
  const on = Boolean(feed && feed.on);
  const toggle = h('input', { id: 'feed-on', type: 'checkbox' });
  toggle.checked = on;
  const hours = h('select', { id: 'feed-auto-off' },
    AUTO_OFF_HOURS.map((value) => h('option', { value }, t(`feed.auto_off.${value || 'none'}`))));
  hours.value = state.hours;
  hours.addEventListener('change', () => { state.hours = hours.value; });
  toggle.disabled = readOnly || state.busy || Boolean(feed && feed.pending);
  hours.disabled = toggle.disabled;
  toggle.addEventListener('change', () => onSwitch(toggle.checked, state.hours === '' ? null : Number(state.hours)));
  let hint;
  if (on) {
    const until = feed.until_ms ? new Date(feed.until_ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null;
    hint = t('feed.hint_on', { s: lastCycle === null || lastCycle === undefined ? '–' : fmt(lastCycle, '', 1).trim() })
      + (until ? ` ${t('feed.hint_until', { time: until })}` : '');
  } else {
    hint = t('feed.hint_off');
  }
  return h('article', { class: 'card', 'aria-labelledby': 'feed-title' },
    h('div', { class: 'card-head' }, h('h3', { id: 'feed-title' }, t('feed.title')),
      chip(t(on ? 'feed.state.on' : 'feed.state.off'), on ? 'avail-online' : 'avail-unknown')),
    h('div', { class: 'field check' }, h('div', { class: 'check-row' }, toggle, h('label', { for: 'feed-on' }, t('feed.switch')))),
    h('div', { class: 'field' }, h('label', { for: 'feed-auto-off' }, t('feed.auto_off')), hours),
    h('p', { class: 'help', role: 'status' }, hint),
    state.error ? h('p', { class: 'field-error' }, state.error) : null);
}

export function overviewView(ctx) {
  const banner = h('div', { class: 'banner error', role: 'alert', hidden: true });
  const body = h('div', { class: 'stack' });
  const el = h('section', { 'aria-labelledby': 'view-title' },
    h('h2', { id: 'view-title', tabindex: '-1' }, t('tab.overview')),
    storeBanner(ctx.session),
    banner,
    body);
  let timer = null;
  const feedState = { hours: '', busy: false, error: null };
  let status = null;
  let loaded = false;
  let destroyed = false;

  function render() {
    if (!loaded) {
      body.replaceChildren(h('p', { class: 'muted', role: 'status' }, t('boot.loading')));
      return;
    }
    if (status === null) {
      body.replaceChildren(h('p', { class: 'muted', role: 'status' }, t('overview.waiting')));
      return;
    }
    const multi = status.mode === 'multi';
    const age = snapshotAge(status);
    // replaceChildren turns null into the text "null": only real nodes go in
    const sets = Array.isArray(status.sets) ? status.sets : [];
    const children = [
      typeof status.connection_error === 'string' ? connectionBanner(status.connection_error) : null,
      headerLine(status),
      feedCard(status.feed, status.last_cycle_s, ctx, feedState, switchFeed),
      multi || status.system ? systemCard(status.system) : null,
      sets.length > 0 ? h('div', { class: 'grid' }, sets.map((set) => setCard(set, age))) : null,
      h('div', { class: 'grid' }, status.packs.map((pack) => packCard(pack, multi, age))),
    ].filter((child) => child !== null);
    body.replaceChildren(...children);
  }

  async function switchFeed(on, hours) {
    feedState.busy = true;
    feedState.error = null;
    render();
    try {
      await api('PUT', 'api/feed', { on, auto_off_hours: hours });
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      feedState.error = errorText(error);
    }
    feedState.busy = false;
    await refresh();
  }

  async function refresh() {
    if (destroyed || document.visibilityState !== 'visible') return;
    try {
      const data = await api('GET', 'api/status');
      status = data.status;
      loaded = true;
      banner.hidden = true;
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      banner.textContent = error.status === 0 ? t('conn.lost') : errorText(error);
      banner.hidden = false;
    }
    if (!destroyed) render();
  }

  function start() {
    stop();
    refresh();
    timer = setInterval(refresh, POLL_MS);
  }

  function stop() {
    if (timer !== null) clearInterval(timer);
    timer = null;
  }

  function onVisibility() {
    if (document.visibilityState === 'visible') start();
    else stop();
  }

  render();
  document.addEventListener('visibilitychange', onVisibility);
  start();

  return {
    el,
    destroy() {
      destroyed = true;
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    },
  };
}
