// Overview tab: live status of the add-on, polled every 5 s while the page is visible.

import { api, ApiError } from './api.js';
import { t, errorText, fmt, fmtDuration, orDash } from './i18n.js';
import { h } from './ui.js';

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

function chip(text, kind) {
  return h('span', { class: `chip ${kind || ''}` }, text);
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

function maintText(value) {
  return t(`maint.${['present', 'absent', 'pending', 'disabled'].includes(value) ? value : 'pending'}`);
}

function firmware(pack) {
  if (!pack.firmware_version) return orDash(null);
  const patch = pack.firmware_patch && pack.firmware_patch !== pack.firmware_version ? ` (${pack.firmware_patch})` : '';
  return `${pack.firmware_version}${patch}`;
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

// The age in the snapshot was measured when it was built: add the time that passed since.
function lastSuccessAge(pack, snapshotSeconds) {
  if (pack.last_success_age_s === null || pack.last_success_age_s === undefined) return null;
  return pack.last_success_age_s + (snapshotSeconds ?? 0);
}

function packCard(pack, multi, snapshotSeconds) {
  const title = `${t('pack.title', { n: String(pack.address).padStart(2, '0') })}${multi && pack.address === 0 ? ` ${t('pack.master')}` : ''}`;
  const id = `pack-title-${pack.address}`;
  return h('article', { class: 'card pack', 'aria-labelledby': id },
    h('div', { class: 'card-head' }, h('h3', { id }, title), availabilityChip(pack.availability)),
    h('dl', { class: 'kvs' },
      kv(t('pack.last_success'), fmtDuration(lastSuccessAge(pack, snapshotSeconds))),
      kv(t('pack.device'), orDash(pack.device_name)),
      kv(t('pack.firmware'), firmware(pack)),
      kv(t('pack.cells'), orDash(pack.cell_count)),
      kv(t('pack.maint'), maintText(pack.maint)),
      kv(t('pack.soc'), fmt(pack.state_of_charge, '%', 1)),
      kv(t('pack.voltage'), fmt(pack.total_pack_voltage, 'V', 2)),
      kv(t('pack.current'), fmt(pack.current, 'A', 2)),
      kv(t('pack.cell_high'), fmt(pack.highest_cell_voltage, 'V', 3)),
      kv(t('pack.cell_low'), fmt(pack.lowest_cell_voltage, 'V', 3))));
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
    const children = [
      headerLine(status),
      multi ? systemCard(status.system) : null,
      h('div', { class: 'grid' }, status.packs.map((pack) => packCard(pack, multi, age))),
    ].filter((child) => child !== null);
    body.replaceChildren(...children);
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
