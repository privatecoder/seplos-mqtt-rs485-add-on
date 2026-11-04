// Detection tab: a read-only scan of the DIP addresses 0-15 (one address per poll cycle).

import { api, ApiError } from './api.js';
import { t, errorText, orDash } from './i18n.js';
import { h, busy } from './ui.js';

const POLL_MS = 2000;
const ACTIVE = ['queued', 'running'];

// survives switching tabs: coming back shows the running (or finished) job again
let lastJobId = null;

function addressList(list) {
  return list.length === 0 ? orDash(null) : list.map((a) => String(a).padStart(2, '0')).join(', ');
}

function resultView(result, savedCount, ctx) {
  const n = result.contiguous_count;
  const rows = result.responding.map((pack) => h('tr', {},
    h('td', {}, String(pack.address).padStart(2, '0')),
    h('td', {}, orDash(pack.device_name)),
    h('td', {}, orDash(pack.firmware_version))));
  const differs = n >= 1 && savedCount !== null && n !== savedCount;
  return h('div', { class: 'stack' },
    result.responding.length === 0
      ? h('p', {}, t('detect.none'))
      : h('table', { class: 'table' },
        h('thead', {}, h('tr', {}, h('th', { scope: 'col' }, t('detect.col.address')), h('th', { scope: 'col' }, t('detect.col.device')), h('th', { scope: 'col' }, t('detect.col.firmware')))),
        h('tbody', {}, rows)),
    h('p', {}, t('detect.busy'), ' ', h('strong', {}, addressList(result.busy))),
    h('p', {}, t('detect.silent'), ' ', h('strong', {}, addressList(result.silent))),
    h('p', {}, t('detect.contiguous', { n }), n === 0 ? ` ${t('detect.no_master')}` : ''),
    differs
      ? h('div', { class: 'banner info' },
        h('p', {}, t('detect.differs', { n, saved: savedCount })),
        h('button', {
          class: 'btn primary',
          type: 'button',
          disabled: !ctx.session.user,
          onClick: () => ctx.usePackCount(n),
        }, t('detect.use', { n })),
        ctx.session.user ? null : h('p', { class: 'help' }, t('readonly.hint')))
      : null);
}

export function detectView(ctx) {
  const readOnly = !ctx.session.user;
  let job = null;
  let savedCount = null;
  let timer = null;
  let destroyed = false;
  // A remembered job is picked up again; until the first answer (or "unknown job") it is "resuming",
  // independent of whether its body has been loaded.
  let resuming = lastJobId !== null;

  const notice = h('div', { class: 'banner', role: 'status', hidden: true });
  const startButton = h('button', { class: 'btn primary', type: 'button', onClick: () => start() }, t('detect.start'));
  const cancelButton = h('button', { class: 'btn', type: 'button', onClick: () => cancel() }, t('detect.cancel'));
  const jobBox = h('div', { class: 'stack', 'aria-live': 'polite' });
  const el = h('section', { 'aria-labelledby': 'view-title' },
    h('h2', { id: 'view-title', tabindex: '-1' }, t('tab.detect')),
    h('div', { class: 'card stack' },
      h('p', {}, t('detect.explain')),
      readOnly ? h('p', { class: 'help' }, t('readonly.hint')) : null,
      h('div', { class: 'actions' }, startButton, cancelButton)),
    notice,
    jobBox);

  function showNotice(text, kind) {
    notice.className = `banner ${kind}`;
    notice.textContent = text;
    notice.hidden = false;
  }

  function render() {
    const active = job !== null && ACTIVE.includes(job.state);
    startButton.disabled = readOnly || active || resuming;
    cancelButton.hidden = !active;
    cancelButton.disabled = readOnly;
    if (job === null) {
      if (resuming) jobBox.replaceChildren(h('p', { class: 'muted', role: 'status' }, t('detect.resuming')));
      else jobBox.replaceChildren();
      return;
    }
    const { done, total } = job.progress;
    const children = [
      h('div', { class: 'card stack' },
        h('p', {}, h('strong', {}, t(`job.state.${job.state}`)), job.error ? ` (${t(`job.error.${job.error}`)})` : ''),
        ACTIVE.includes(job.state)
          ? h('div', {}, h('progress', { max: Math.max(total, 1), value: done, 'aria-label': t('detect.progress') }), ' ', h('span', {}, t('detect.progress_text', { done, total })))
          : null),
    ];
    if (job.result && !ACTIVE.includes(job.state)) {
      children.push(h('div', { class: 'card' }, h('h3', {}, t('detect.result')), resultView(job.result, savedCount, ctx)));
    } else if (job.result && job.result.responding.length > 0) {
      children.push(h('p', { class: 'muted' }, t('detect.partial', { n: job.result.responding.length })));
    }
    jobBox.replaceChildren(...children);
  }

  async function loadSavedCount() {
    try {
      savedCount = (await api('GET', 'api/connection')).connection.pack_count;
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      savedCount = null;             // no saved settings to compare with: no "use N" suggestion
    }
  }

  function needsPolling() {
    return resuming || (job !== null && ACTIVE.includes(job.state));
  }

  function schedule() {
    clearTimeout(timer);
    if (destroyed || !needsPolling()) return;
    timer = setTimeout(poll, POLL_MS);
  }

  async function adopt(next) {
    resuming = false;
    job = next;
    if (!ACTIVE.includes(job.state)) await loadSavedCount();
    if (destroyed) return;
    render();
    schedule();
  }

  async function poll() {
    const id = job !== null ? job.id : lastJobId;
    if (destroyed || id === null) return;
    if (document.hidden) return;         // picked up again by onVisibility
    try {
      const data = await api('GET', `api/jobs/${id}`);
      notice.hidden = true;
      await adopt(data.job);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      if (error.key === 'unknown_job') {
        lastJobId = null;
        job = null;
        resuming = false;
        notice.hidden = true;
        render();
        return;
      }
      showNotice(error.status === 0 ? t('conn.lost') : errorText(error), 'warn');   // the process may be restarting
      render();
      schedule();                        // retried until an answer or "unknown job"
    }
  }

  function onVisibility() {
    if (!document.hidden && needsPolling()) {
      clearTimeout(timer);
      poll();
    }
  }

  async function start() {
    notice.hidden = true;
    busy(startButton, true);
    try {
      const data = await api('POST', 'api/jobs/detect', {});
      lastJobId = data.job.id;
      await adopt(data.job);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      showNotice(errorText(error), error.key === 'job_busy' ? 'warn' : 'error');
    } finally {
      busy(startButton, false);
      if (!destroyed) render();
    }
  }

  async function cancel() {
    if (job === null) return;
    busy(cancelButton, true);
    try {
      const data = await api('POST', `api/jobs/${job.id}/cancel`, {});
      await adopt(data.job);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      showNotice(errorText(error), 'error');
    } finally {
      busy(cancelButton, false);
      if (!destroyed) render();
    }
  }

  document.addEventListener('visibilitychange', onVisibility);
  render();
  if (resuming) poll();

  return {
    el,
    destroy() {
      destroyed = true;
      clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    },
  };
}
