// Entry point: boot, session, hash router (works below the Ingress base path), restart handling.

import { api, setCsrf, ApiError } from './api.js';
import { loadLanguage, t, errorText, localiseDocument } from './i18n.js';
import { h, toast } from './ui.js';
import { overviewView } from './overview.js';
import { settingsView } from './settings.js';
import { setsView } from './sets.js';
import { detectView } from './detect.js';
import { cellsView } from './cells.js';
import { packsView } from './packs.js';

const VIEWS = { overview: overviewView, settings: settingsView, sets: setsView, cells: cellsView, packs: packsView, detect: detectView };
const RESTART_POLL_MS = 2000;
const RESTART_GIVE_UP_MS = 180000;

const ctx = {
  session: null,
  restarting: false,
  pendingPackCount: null,
  navigate(tab) { location.hash = `#/${tab}`; },
  usePackCount(n) { ctx.pendingPackCount = n; ctx.navigate('settings'); },
  awaitRestart,
};

let current = null;       // { el, dirty?, discardDraft?, destroy }
let currentTab = null;
const view = document.getElementById('view');
const guard = document.getElementById('guard');
const restartBanner = document.getElementById('restart');

function tabFromHash() {
  const name = location.hash.replace(/^#\/?/, '');
  return Object.hasOwn(VIEWS, name) ? name : 'overview';
}

function mount(tab) {
  if (current && current.destroy) current.destroy();
  currentTab = tab;
  current = VIEWS[tab](ctx);
  view.replaceChildren(current.el);
  for (const link of document.querySelectorAll('nav a[data-tab]')) {
    if (link.dataset.tab === tab) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  document.title = `${t(`tab.${tab}`)} · ${t('app.title')}`;
  const heading = view.querySelector('h2');
  if (heading) heading.focus({ preventScroll: true });
}

function hideGuard() {
  guard.hidden = true;
  guard.replaceChildren();
}

// Leaving a tab with unsaved changes: stay by default, the user decides in the page itself
// (no browser dialogs, they are blocked inside the Home Assistant frame).
function onHashChange() {
  const target = tabFromHash();
  if (target === currentTab) return;
  if (current && current.dirty && current.dirty()) {
    history.replaceState(null, '', `#/${currentTab}`);
    const lines = current.changes ? current.changes() : [];
    // replaceChildren turns null into the text "null": only real nodes go in
    guard.replaceChildren(...[
      h('p', {}, t('guard.text')),
      lines.length ? h('ul', { class: 'guard-changes' }, lines.map((line) => h('li', {}, line))) : null,
      h('div', { class: 'actions' },
        h('button', { class: 'btn', type: 'button', onClick: hideGuard }, t('guard.stay')),
        h('button', {
          class: 'btn danger',
          type: 'button',
          onClick: () => {
            if (current.discardDraft) current.discardDraft();
            hideGuard();
            ctx.navigate(target);
          },
        }, t('guard.discard')))].filter((child) => child !== null));
    guard.hidden = false;
    guard.querySelector('button').focus();
    return;
  }
  hideGuard();
  mount(target);
}

async function loadSession() {
  const data = await api('GET', 'api/session');
  ctx.session = data;
  setCsrf(data.csrf);
  return data;
}

// Resolves at once while the page is visible, otherwise when it becomes visible again.
function waitVisible() {
  return new Promise((resolve) => {
    if (!document.hidden) {
      resolve();
      return;
    }
    const onChange = () => {
      if (document.hidden) return;
      document.removeEventListener('visibilitychange', onChange);
      resolve();
    };
    document.addEventListener('visibilitychange', onChange);
  });
}

// After a saved connection change the add-on restarts its process. Its CSRF secret is new per
// process, so "answers again with a revision at least the acknowledged one (another browser may
// have saved a newer one) and a different token" means the new process is up. Nothing is polled
// while the page is hidden; the give-up time only counts polling time.
async function awaitRestart(revision) {
  const oldCsrf = ctx.session ? ctx.session.csrf : null;
  ctx.restarting = true;
  let polledMs = 0;
  const show = (text, extra) => {
    restartBanner.replaceChildren(h('span', {}, text), extra ? ' ' : '', extra || '');
    restartBanner.hidden = false;
  };
  show(t('restart.waiting'));
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, RESTART_POLL_MS));
    await waitVisible();
    polledMs += RESTART_POLL_MS;
    try {
      const data = await api('GET', 'api/session');
      if (data.revision >= revision && (oldCsrf === null || data.csrf !== oldCsrf)) {
        ctx.session = data;
        setCsrf(data.csrf);
        break;
      }
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;     // errors / 502 are expected while it restarts
    }
    if (polledMs > RESTART_GIVE_UP_MS) {
      show(t('restart.slow'), h('button', { class: 'btn small', type: 'button', onClick: () => location.reload() }, t('boot.retry')));
      return;
    }
  }
  ctx.restarting = false;
  restartBanner.hidden = true;
  toast(t('restart.done'), 'ok');
  if (currentTab === 'settings') ctx.navigate('overview');
  else mount(currentTab);
}

function showFailure(error) {
  const forbidden = error instanceof ApiError && error.key === 'forbidden_peer';
  const text = forbidden ? t('boot.forbidden') : error instanceof ApiError ? errorText(error) : t('boot.render_failed');
  view.replaceChildren(h('section', { class: 'card stack', role: 'alert' },
    h('h2', {}, t('boot.failed_title')),
    h('p', {}, text),
    h('button', { class: 'btn primary', type: 'button', onClick: () => location.reload() }, t('boot.retry'))));
}

async function boot() {
  try {
    await loadLanguage();
  } catch {
    view.textContent = 'Seplos BMS: could not load the texts. / Texte konnten nicht geladen werden.';
    return;
  }
  localiseDocument();
  try {
    await loadSession();
    mount(tabFromHash());
  } catch (error) {
    showFailure(error);
    return;
  }
  window.addEventListener('hashchange', onHashChange);
  window.addEventListener('beforeunload', (event) => {
    if (current && current.dirty && current.dirty()) event.preventDefault();
  });
}

boot();
