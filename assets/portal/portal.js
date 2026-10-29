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
  if (button.dataset.action === 'refresh-overview') { await refreshOverview(true); return; }
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

// Patch values in place so cards keep their identity, focus and animation state.
function patchOverviewNode(current, incoming) {
  if (current.nodeType !== incoming.nodeType || current.nodeName !== incoming.nodeName) {
    current.replaceWith(incoming.cloneNode(true));
    return;
  }
  if (current.nodeType === Node.TEXT_NODE) {
    if (current.nodeValue !== incoming.nodeValue) current.nodeValue = incoming.nodeValue;
    return;
  }
  if (current.nodeType !== Node.ELEMENT_NODE) return;
  for (const attr of [...current.attributes]) {
    if (!incoming.hasAttribute(attr.name) && attr.name !== 'open') current.removeAttribute(attr.name);
  }
  for (const attr of incoming.attributes) {
    if (current.getAttribute(attr.name) !== attr.value) current.setAttribute(attr.name, attr.value);
  }
  const oldChildren = [...current.childNodes], newChildren = [...incoming.childNodes];
  for (let i = 0; i < Math.max(oldChildren.length, newChildren.length); i++) {
    if (!newChildren[i]) oldChildren[i].remove();
    else if (!oldChildren[i]) current.append(newChildren[i].cloneNode(true));
    else patchOverviewNode(oldChildren[i], newChildren[i]);
  }
}
async function refreshPageData(manual) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 8000);
  try {
    const response = await fetch(adminBase + (manual ? '/overview/refresh' : '/overview'), {
      cache:'no-store', credentials:'same-origin', redirect:'error', signal:controller.signal
    });
    if (!response.ok) throw new Error('unavailable');
    const doc = new DOMParser().parseFromString(await response.text(), 'text/html');
    const selectors = ['.traffic-card', '#members', '#server-overview', '.backup-entry-state'];
    const regions = selectors.map(selector => [document.querySelector(selector), doc.querySelector(selector)]);
    if (regions.some(([current, next]) => !current || !next)) throw new Error('unavailable');
    if (document.hidden) return false;
    for (const [current, next] of regions) patchOverviewNode(current, next);
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timeout);
  }
}
let overviewPending = false;
async function refreshOverview(manual = false) {
  const button = document.querySelector('.floating-refresh');
  if (!adminBase || !button || document.hidden || overviewPending) return;
  overviewPending = true;
  button.disabled = true; button.setAttribute('aria-busy','true');
  button.querySelector('.refresh-label').textContent = '刷新中';
  try {
    const updated = await refreshPageData(manual);
    const historyUpdated = manual ? await refreshTasks() : true;
    if (manual && !document.hidden) notify(updated && historyUpdated !== false
      ? '页面数据已刷新'
      : '部分数据刷新失败，请稍后重试；登录过期时请重新登录。');
  } finally {
    overviewPending = false; button.disabled = false; button.removeAttribute('aria-busy');
    button.querySelector('.refresh-label').textContent = '刷新';
  }
}
let overviewInterval = null;
function stopOverviewPolling() {
  clearInterval(overviewInterval); overviewInterval = null;
}
function updateOverviewPolling(refreshNow = true) {
  stopOverviewPolling();
  if (document.hidden || !adminBase || !document.querySelector('.floating-refresh')) return;
  if (refreshNow) refreshOverview();
  overviewInterval = setInterval(() => refreshOverview(), 30000);
}
if (adminBase && document.querySelector('.floating-refresh')) {
  updateOverviewPolling(false);
  document.addEventListener('visibilitychange', () => updateOverviewPolling());
  window.addEventListener('pagehide', stopOverviewPolling);
  window.addEventListener('pageshow', event => updateOverviewPolling(event.persisted));
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
    if (item.composition) { const note = document.createElement('p'); note.className = 'subscription-backup-note'; note.textContent = item.composition; block.append(note); }
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
    'delete-history':['删除全部执行记录','删除全部已结束的执行记录，保留等待中和执行中的任务。不会撤销操作或更改订阅配置。删除后无法恢复。'],
    'rotate-page':['更换网页链接','旧网页链接将立即失效，已导入客户端的订阅不受影响。请确认目标用户。'],
    'rotate-sub':['更换订阅链接','该用户的旧订阅地址将失效，所有客户端需要重新导入。其个人网页地址保留。owner 操作后可能需要重新登录。'],
    'refresh-airport':['刷新 Amy 订阅','获取最新机场节点与流量信息，保留现有订阅地址。'],
    'update-sync':['更新代码并生成配置','先更新代码与服务，成功后生成全部用户配置；更新失败则停止。订阅地址保留。网页可能短暂中断并要求重新登录。']
  };
  if (!(action in descriptions)) return;
  const [title, detail] = descriptions[action];
  confirmation = {action, target, request_id: crypto.randomUUID()};
  document.querySelector('#confirm-title').textContent = target && action !== 'delete-history' && selectedName ? selectedName + (target === 'airport' ? ' · 机场网页' : '') + ' · ' + title : title;
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
      element.append(info,badge,time);
      root.append(element);
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
    return false;
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
  if (button.hasAttribute('data-backup-details')) { await showBackupStatus(); return; }
  if (button.dataset.history) { document.querySelector('#history-dialog').showModal(); refreshTasks(); return; }
  if (button.hasAttribute('data-clear-history')) { askTask('delete-history', null); return; }
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
    if (operation.action === 'delete-history') {
      const body = new URLSearchParams({csrf:csrfToken,confirmed:'yes'});
      const response = await fetch(adminBase + '/tasks/delete', {method:'POST',body,credentials:'same-origin',signal:AbortSignal.timeout(8000)});
      if (!response.ok) { notify(response.status === 409 ? '操作繁忙，请稍后重试。' : '删除失败，请检查登录状态后重试。'); return; }
      confirmation = null; confirmDialog.close(); notify('已清空已结束的执行记录');
      await refreshTasks(); return;
    }
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
    if (operation.action === 'delete-history') { notify('暂未确认删除结果，请重新打开执行记录查看。'); refreshTasks(); return; }
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

async function showBackupStatus() {
  const dialog = document.querySelector('#backup-dialog');
  const root = document.querySelector('#backup-content');
  if (!adminBase || !dialog || !root) return;
  root.textContent = '正在读取…'; dialog.showModal();
  try {
    const response = await fetch(adminBase + '/backup', {cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error('unavailable');
    const data = await response.json(); root.replaceChildren();
    const badge=document.createElement('span');badge.className='badge';badge.textContent=data.label;root.append(badge);
    const entry=document.querySelector('.backup-entry-state');if(entry)entry.textContent=data.label;
    function row(title,value) {
      const line=document.createElement('p');line.className='backup-status-row';
      const label=document.createElement('span');label.textContent=title;
      const text=document.createElement('span');text.textContent=value;
      line.append(label,text);root.append(line);
    }
    row('缓存节点',data.nodes.length+' 个');
    row('节点名称',data.nodes.join('、')||'暂无');
    row('最近成功刷新',data.last_success ? new Date(data.last_success*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+' 北京时间' : '暂无');
    const errors={backup_download_failed:'下载失败；保留上次成功缓存',backup_source_invalid:'备用配置不可用，请到终端检查'};
    row('最近错误',errors[data.last_error]||'无');
    row('已发布备用的用户',data.users.join('、')||'无');
    row('固定排除',data.excluded.join('、'));
    if(data.traffic) {
      const size=bytes=>{let n=bytes,unit='B';for(const u of ['KB','MB','GB','TB']){if(n<1024)break;n/=1024;unit=u;}return n.toFixed(1)+' '+unit;};
      const t=data.traffic,used=t.upload+t.download;
      row('来源流量',size(used)+' / '+(t.total?size(t.total):'不限额'));
      if(t.total)row('剩余流量',size(Math.max(0,t.total-used)));
    } else row('来源流量','来源未提供');
  } catch { root.textContent='无法取得备用状态，请稍后重试或重新登录。'; }
}
