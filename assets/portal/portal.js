'use strict';
let timer;
function notify(message) {
  const toast = document.querySelector('#toast');
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(timer);
  timer = setTimeout(() => { toast.hidden = true; }, 3000);
}
document.addEventListener('click', async (event) => {
  const button = event.target.closest('button[data-action]');
  if (!button) return;
  const block = button.closest('.subscription-block');
  const code = block.querySelector('code');
  if (button.dataset.action === 'reveal') {
    const visible = button.getAttribute('aria-pressed') !== 'true';
    code.textContent = visible ? code.dataset.url : code.dataset.hidden;
    button.setAttribute('aria-pressed', String(visible));
  } else if (button.dataset.action === 'copy') {
    try {
      await navigator.clipboard.writeText(code.dataset.url);
      notify('已复制订阅链接');
    } catch {
      notify('无法访问剪贴板，请显示链接后手动复制');
    }
  } else if (button.dataset.action === 'import') {
    location.href = 'clash://install-config?url=' + encodeURIComponent(code.dataset.url);
  }
});

// Presence is admin-only. Hidden tabs stop polling; failures never imply offline.
const presenceCells = [...document.querySelectorAll('[data-online-client]')];
function renderPresence(cell, state) {
  const labels = {online: '在线', offline: '离线', unknown: '未知'};
  if (!(state in labels)) state = 'unknown';
  const badge = document.createElement('span');
  badge.className = 'presence ' + state;
  badge.title = '来源：3x-ui 在线查询';
  const marker = document.createElement(state === 'unknown' ? 'span' : 'i');
  marker.setAttribute('aria-hidden', 'true');
  if (state === 'unknown') marker.textContent = '—';
  badge.append(marker, document.createTextNode(labels[state]));
  cell.replaceChildren(badge);
}
let presencePending = false;
async function refreshPresence() {
  if (!presenceCells.length || document.hidden || presencePending) return;
  presencePending = true;
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 5000);
  try {
    const response = await fetch('/admin/onlines', {cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    if (document.hidden) return;
    for (const cell of presenceCells) renderPresence(cell, data.states?.[cell.dataset.onlineClient] ?? 'unknown');
  } catch {
    if (!document.hidden) {
      for (const cell of presenceCells) renderPresence(cell, 'unknown');
    }
  } finally {
    clearTimeout(timeout);
    presencePending = false;
  }
}
let presenceInterval = null;
function stopPresencePolling() {
  clearInterval(presenceInterval);
  presenceInterval = null;
}
function updatePresencePolling(refreshNow = true) {
  stopPresencePolling();
  if (document.hidden || !presenceCells.length) return;
  if (refreshNow) refreshPresence();
  presenceInterval = setInterval(refreshPresence, 30000);
}
if (presenceCells.length) {
  updatePresencePolling(false);
  document.addEventListener('visibilitychange', () => updatePresencePolling());
  window.addEventListener('pagehide', stopPresencePolling);
  window.addEventListener('pageshow', () => updatePresencePolling());
}
