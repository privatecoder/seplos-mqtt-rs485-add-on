// OpenBMS Monitor feed: its switch at the bottom of the Settings tab. It is no part of the settings document: it acts
// at once (PUT api/feed, adopted by the add-on at the end of the running poll round) and never restarts anything. Its
// state comes from api/status, read every 5 s while the tab is visible.

import { api, ApiError } from './api.js';
import { t, errorText, fmt } from './i18n.js';
import { h, chip } from './ui.js';

const POLL_MS = 5000;
const AUTO_OFF_HOURS = ['', '1', '4', '24'];

// The auto-off choice as shown: while on, the running deadline ("keep") or "never"; while off, the choice for switching on.
export function autoOffValue(feed, chosen) {
  if (feed && feed.on) return feed.until_ms ? 'keep' : '';
  return chosen;
}

// What a new auto-off choice does: while on it applies at once (a new deadline from now), while off it is kept for the
// next switch-on. Returns the PUT body, or null when nothing is sent.
export function autoOffRequest(feed, value) {
  if (!feed || !feed.on || value === 'keep') return null;
  return { on: true, auto_off_hours: value === '' ? null : Number(value) };
}

export function feedSection(ctx) {
  const readOnly = !ctx.session.user;
  // loadError: the last status read failed (cleared by the next good one); switchError: the last switch failed
  const state = { feed: null, round: null, hours: '', busy: false, loadError: null, switchError: null, loaded: false };
  const el = h('section', { class: 'card feed-card', 'aria-labelledby': 'feed-title' });
  let timer = null;
  let destroyed = false;
  let generation = 0;
  // the running status read (one at a time: a slow answer is taken, never overtaken by the next poll); only its owner
  // clears it
  let inflight = null;

  function render() {
    const feed = state.feed;
    const on = Boolean(feed && feed.on);
    const pending = Boolean(feed && feed.pending);
    const toggle = h('input', { id: 'feed-on', type: 'checkbox' });
    toggle.checked = on;
    const until = on && feed.until_ms ? new Date(feed.until_ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null;
    const hours = h('select', { id: 'feed-auto-off' },
      until ? h('option', { value: 'keep' }, t('feed.auto_off.until', { time: until })) : null,
      AUTO_OFF_HOURS.map((value) => h('option', { value }, t(`feed.auto_off.${value || 'none'}`))));
    hours.value = autoOffValue(feed, state.hours);
    hours.addEventListener('change', () => {
      const body = autoOffRequest(feed, hours.value);
      if (body) send(body);
      else if (hours.value !== 'keep') state.hours = hours.value;
    });
    toggle.disabled = readOnly || !state.loaded || state.busy || pending;
    hours.disabled = toggle.disabled;
    toggle.addEventListener('change', () => send({ on: toggle.checked, auto_off_hours: toggle.checked && state.hours !== '' ? Number(state.hours) : null }));
    let hint = t('feed.hint_off');
    if (pending) hint = t('feed.pending');
    else if (on) {
      hint = t('feed.hint_on', { s: state.round === null || state.round === undefined ? '–' : fmt(state.round, '', 1).trim() })
        + (until ? ` ${t('feed.hint_until', { time: until })}` : '');
    }
    const children = [
      h('div', { class: 'card-head' }, h('h3', { id: 'feed-title' }, t('feed.title')),
        state.loaded ? chip(t(on ? 'feed.state.on' : 'feed.state.off'), on ? 'avail-online' : 'avail-unknown') : null),
      h('p', { class: 'help' }, t('feed.independent')),
      h('div', { class: 'field check' }, h('div', { class: 'check-row' }, toggle, h('label', { for: 'feed-on' }, t('feed.switch')))),
      h('div', { class: 'field' }, h('label', { for: 'feed-auto-off' }, t('feed.auto_off')), hours),
      h('p', { class: 'help', role: 'status' }, hint),
      state.switchError ? h('p', { class: 'field-error' }, state.switchError) : null,
      state.loadError ? h('p', { class: 'field-error' }, state.loadError) : null,
    ].filter((child) => child !== null);
    el.replaceChildren(...children);
  }

  // `force`: the read after a switch, which replaces a read still under way (that one's answer is then ignored)
  async function refresh(force = false) {
    // no poll while a switch is sent: its read follows the switch
    if (destroyed || (!force && (inflight || state.busy)) || document.visibilityState !== 'visible') return;
    const token = {};
    inflight = token;
    const mine = generation;
    try {
      const data = await api('GET', 'api/status');
      if (destroyed || mine !== generation) return;          // an answer from before a switch changes nothing
      const status = data.status || {};
      state.feed = status.feed || null;
      state.round = status.last_cycle_s;
      state.loaded = state.feed !== null;
      state.loadError = null;
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (destroyed || mine !== generation) return;
      // the state stays as last read (unknown before the first read), never shown as "off"
      state.loadError = error.status === 0 ? t('conn.lost') : errorText(error);
    } finally {
      if (inflight === token) inflight = null;
    }
    if (mine === generation) render();
  }

  async function send(body) {
    state.busy = true;
    state.switchError = null;
    generation += 1;          // a status read still under way describes the state before this switch
    render();
    try {
      await api('PUT', 'api/feed', body);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      state.switchError = errorText(error);
    }
    state.busy = false;
    await refresh(true);
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
