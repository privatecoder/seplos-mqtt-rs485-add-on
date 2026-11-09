// OpenBMS Monitor feed: its switch at the bottom of the Settings tab. It is no part of the settings document: it acts
// at once (PUT api/feed, adopted by the add-on at the end of the running poll round) and never restarts anything. Its
// state comes from api/status, read every 5 s while the tab is visible.

import { api, ApiError } from './api.js';
import { t, errorText, fmt } from './i18n.js';
import { h, chip } from './ui.js';

const POLL_MS = 5000;
const AUTO_OFF_HOURS = ['', '1', '4', '24'];

export function feedSection(ctx) {
  const readOnly = !ctx.session.user;
  const state = { feed: null, round: null, hours: '', busy: false, error: null, loaded: false };
  const el = h('section', { class: 'card feed-card', 'aria-labelledby': 'feed-title' });
  let timer = null;
  let destroyed = false;
  let generation = 0;

  function render() {
    const feed = state.feed;
    const on = Boolean(feed && feed.on);
    const pending = Boolean(feed && feed.pending);
    const toggle = h('input', { id: 'feed-on', type: 'checkbox' });
    toggle.checked = on;
    const hours = h('select', { id: 'feed-auto-off' },
      AUTO_OFF_HOURS.map((value) => h('option', { value }, t(`feed.auto_off.${value || 'none'}`))));
    hours.value = state.hours;
    hours.addEventListener('change', () => { state.hours = hours.value; });
    toggle.disabled = readOnly || !state.loaded || state.busy || pending;
    hours.disabled = toggle.disabled;
    toggle.addEventListener('change', () => switchFeed(toggle.checked, state.hours === '' ? null : Number(state.hours)));
    let hint = t('feed.hint_off');
    if (pending) hint = t('feed.pending');
    else if (on) {
      const until = feed.until_ms ? new Date(feed.until_ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : null;
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
      state.error ? h('p', { class: 'field-error' }, state.error) : null,
    ].filter((child) => child !== null);
    el.replaceChildren(...children);
  }

  async function refresh() {
    if (destroyed || document.visibilityState !== 'visible') return;
    const mine = ++generation;
    try {
      const data = await api('GET', 'api/status');
      if (destroyed || mine !== generation) return;          // a late answer changes nothing
      const status = data.status || {};
      state.feed = status.feed || null;
      state.round = status.last_cycle_s;
      state.loaded = state.feed !== null;
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (destroyed || mine !== generation) return;
      // the state stays unknown, never shown as "off"
      state.error = error.status === 0 ? t('conn.lost') : errorText(error);
    }
    render();
  }

  async function switchFeed(on, hours) {
    state.busy = true;
    state.error = null;
    render();
    try {
      await api('PUT', 'api/feed', { on, auto_off_hours: hours });
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      state.error = errorText(error);
    }
    state.busy = false;
    await refresh();
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
