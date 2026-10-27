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
  if (button.dataset.action === 'back') { history.back(); return; }
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

const adminBase = document.querySelector('main[data-admin-base]')?.dataset.adminBase || '';

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
    const response = await fetch(adminBase + '/onlines', {cache: 'no-store', credentials: 'same-origin', signal: controller.signal});
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

// Fixed owner actions. Confirmation occurs before any authenticated POST.
const manageDialog = document.querySelector('#manage-dialog');
const confirmDialog = document.querySelector('#confirm-dialog');
const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content;
let selectedUser = null;
let selectedName = "";
let confirmation = null;
let submitting = false;
let taskTimer = null;
let tasksPending = false;
let taskRequestPending = false;
let completedTask = null;

async function fetchLinks(userId) {
  const response = await fetch(adminBase + '/user/' + encodeURIComponent(userId) + '/links', {cache:'no-store', credentials:'same-origin'});
  if (!response.ok) throw new Error('unavailable');
  return response.json();
}
function drawUserLinks(data) {
  selectedName = data.email;
  document.querySelector('#manage-title').textContent = data.email + ' · 链接管理';
  const root = document.querySelector('#user-links');
  root.replaceChildren();
  for (const item of data.links) {
    const block = document.createElement('div'); block.className = 'modal-link';
    const label = document.createElement('div'); label.className = 'label'; label.textContent = item.title;
    if (item.rotate) {
      const button = document.createElement('button'); button.className = 'rotate-link';
      button.textContent = item.rotate === 'rotate-page' ? '更换网页链接' : '更换订阅链接';
      button.dataset.rotate = item.rotate; button.dataset.target = item.target;
      label.append(button);
    }
    block.append(label);
    if (item.url) {
      const box = document.createElement('div'); box.className = 'linkbox';
      const input = document.createElement('input'); input.readOnly = true; input.value = item.url; input.setAttribute('aria-label', item.title);
      const button = document.createElement('button'); button.textContent = '复制'; button.dataset.copy = item.url;
      box.append(input, button); block.append(box);
    }
    root.append(block);
  }
}
function askTask(action, target) {
  const descriptions = {
    'rotate-page':['更换网页链接','旧网页链接将立即失效，已导入客户端的订阅不受影响。请确认目标用户。'],
    'rotate-sub':['更换订阅链接','该用户的旧订阅地址将失效，所有客户端需要重新导入。其个人网页地址保留。owner 操作后可能需要重新登录。'],
    'refresh-airport':['刷新 Amy 订阅','获取最新机场节点与流量信息，保留现有订阅地址。'],
    'update-sync':['更新代码并生成配置','先更新代码与服务，成功后生成全部用户配置；更新失败则停止。订阅地址保留。网页可能短暂中断并要求重新登录。']
  };
  if (!(action in descriptions)) return;
  const [title, detail] = descriptions[action];
  confirmation = {action, target, request_id: crypto.randomUUID()};
  document.querySelector('#confirm-title').textContent = target && selectedName ? selectedName + (target === 'airport' ? ' · 机场网页' : '') + ' · ' + title : title;
  document.querySelector('#confirm-text').textContent = detail;
  document.querySelector('#confirm-action').textContent = '确认执行';
  confirmDialog.showModal();
}

async function refreshTasks() {
  if (!adminBase || !manageDialog || document.hidden || taskRequestPending) return;
  clearTimeout(taskTimer);
  taskRequestPending = true;
  try {
    const response = await fetch(adminBase + '/tasks', {cache:'no-store', credentials:'same-origin', signal:AbortSignal.timeout(5000)});
    if (response.status === 401) {
      location.href = adminBase + '/login';
      return;
    }
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json();
    const rows = Array.isArray(data.tasks) ? data.tasks : [];
    tasksPending = rows.some(row => row.status === 'queued' || row.status === 'running');
    const root = document.querySelector('#operation-list');
    root.replaceChildren();
    for (const row of rows.slice(0,10)) {
      const element = document.createElement('div'); element.className = 'history-row';
      const info = document.createElement('div');
      const title = document.createElement('strong'); title.textContent = row.title;
      const phase = document.createElement('span'); phase.className = 'subtle'; phase.textContent = (row.target_label ? row.target_label + ' · ' : '') + row.phase;
      info.append(title,phase);
      const badge = document.createElement('span'); badge.className = 'badge'; badge.textContent = row.label;
      const time = document.createElement('time'); time.textContent = new Date(row.updated_at*1000).toLocaleTimeString('zh-CN',{timeZone:'Asia/Shanghai',hour:'2-digit',minute:'2-digit'});
      element.append(info,badge,time); root.append(element);
    }
    if (!rows.length) root.textContent = '暂无执行记录';
    const current = rows.find(row => row.status === 'queued' || row.status === 'running');
    document.querySelector('#task-progress').textContent = current ? current.phase : '当前无执行中的任务';
    for (const button of document.querySelectorAll('button[data-task],button[data-rotate]')) button.disabled = tasksPending;
    if (rows[0] && rows[0].status === 'success' && completedTask !== rows[0].id) {
      completedTask = rows[0].id;
      if (manageDialog.open && selectedUser) drawUserLinks(await fetchLinks(selectedUser));
    }
  } catch {
    document.querySelector('#task-progress').textContent = '暂时无法取得结果，更新期间请稍后重试或重新登录。';
  } finally {
    taskRequestPending = false;
    if (tasksPending && !document.hidden) taskTimer = setTimeout(refreshTasks, 5000);
  }
}

document.addEventListener('click', async event => {
  if (!adminBase || !manageDialog) return;
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.close) { document.getElementById(button.dataset.close).close(); return; }
  if (button.dataset.user) {
    selectedUser = button.dataset.user;
    document.querySelector('#manage-title').textContent = '读取用户链接';
    document.querySelector('#user-links').textContent = '正在读取…';
    manageDialog.showModal();
    try { const data = await fetchLinks(selectedUser); drawUserLinks(data); }
    catch { document.querySelector('#user-links').textContent = '读取失败，请刷新或重新登录。'; }
    return;
  }
  if (button.dataset.copy) {
    try { await navigator.clipboard.writeText(button.dataset.copy); notify('已复制链接'); }
    catch { notify('请选中链接手动复制'); }
    return;
  }
  if (button.dataset.rotate) { askTask(button.dataset.rotate, button.dataset.target); return; }
  if (button.dataset.task) { askTask(button.dataset.task, null); return; }
  if (button.id !== 'confirm-action' || !confirmation || submitting) return;
  submitting = true; button.disabled = true;
  const operation = confirmation;
  try {
    const body = new URLSearchParams({...operation, target:operation.target || '', csrf:csrfToken, confirmed:'yes'});
    const response = await fetch(adminBase + '/tasks', {method:'POST',body,credentials:'same-origin',signal:AbortSignal.timeout(8000)});
    if (response.status !== 202) {
      notify(response.status === 409 ? '已有操作正在执行，请查看执行记录。' : '操作未能启动，请检查登录状态和执行记录。');
      return;
    }
    confirmation = null; confirmDialog.close(); tasksPending = true;
    notify('任务已提交到后台，可在执行记录中查看结果。');
    refreshTasks();
  } catch {
    notify('暂未收到确认，请先查看执行记录，不要重复提交。');
    tasksPending = true; refreshTasks();
  } finally { submitting = false; button.disabled = false; }
});
if (adminBase && manageDialog) {
  refreshTasks();
  document.addEventListener('visibilitychange', () => {
    clearTimeout(taskTimer);
    if (!document.hidden) refreshTasks();
  });
  window.addEventListener('pagehide', () => clearTimeout(taskTimer));
}
