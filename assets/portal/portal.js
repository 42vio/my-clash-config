'use strict';
const copyLinkIcon='<svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><g class="copy-symbol"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></g><path class="copy-done" d="m5 12 4 4L19 6"/></svg>';

const copiedFeedbackTimers=new WeakMap();
function markLinkCopied(button){
  if(!button)return;
  clearTimeout(copiedFeedbackTimers.get(button));
  if(!button.dataset.copyLabel){button.dataset.copyLabel=button.getAttribute('aria-label')||button.title||'复制链接';button.dataset.copyTitle=button.title||'复制链接';}
  button.classList.add('is-copied');button.title='已复制';button.setAttribute('aria-label','已复制');
  copiedFeedbackTimers.set(button,setTimeout(()=>{button.classList.remove('is-copied');button.title=button.dataset.copyTitle;button.setAttribute('aria-label',button.dataset.copyLabel);},1600));
}

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
    button.title=visible?'隐藏链接':'显示链接';button.setAttribute('aria-label',button.title);
  } else if (button.dataset.action === 'copy') {
    try {
      await navigator.clipboard.writeText(code.dataset.url);
      markLinkCopied(button);
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
    const selectors = ['.traffic-card', '#members', '#server-overview', '.backup-entry-state', '.nav-shortcuts'];
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
async function refreshOverview(manual = false, fresh = manual) {
  const button = document.querySelector('.floating-refresh');
  if (!adminBase || !button || document.hidden || overviewPending) return;
  overviewPending = true;
  button.disabled = true; button.setAttribute('aria-busy','true');
  button.querySelector('.refresh-label').textContent = '刷新中';
  try {
    const resumeSpeed=speedEnabled;
    if(resumeSpeed)setSpeedMode(false);
    const updated = await refreshPageData(fresh);
    if(resumeSpeed)setSpeedMode(true);
    const historyUpdated = manual ? await refreshTasks() : true;
    if (manual && !document.hidden) notify(updated && historyUpdated !== false
      ? '页面数据已刷新'
      : '部分数据刷新失败，请稍后重试；登录过期时请重新登录。');
  } finally {
    overviewPending = false; button.disabled = false; button.removeAttribute('aria-busy');
    button.querySelector('.refresh-label').textContent = '刷新';
  }
}
// Refresh once per page entry; returning to a visible tab does not poll.
let overviewEntryRefreshed = false;
function refreshOverviewOnEntry() {
  if (document.hidden || overviewEntryRefreshed || !adminBase || !document.querySelector('.floating-refresh')) return;
  overviewEntryRefreshed = true;
  refreshOverview(false, true);
}
if (adminBase && document.querySelector('.floating-refresh')) {
  document.addEventListener('visibilitychange', refreshOverviewOnEntry);
  window.addEventListener('pageshow', event => {
    if (event.persisted) overviewEntryRefreshed = false;
    refreshOverviewOnEntry();
  });
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
  const response = await fetch(adminBase + (userId==='airport'?'/airport/links':'/user/'+encodeURIComponent(userId)+'/links'), {cache:'no-store', credentials:'same-origin'});
  if (!response.ok) throw new Error('unavailable');
  return response.json();
}
function drawUserLinks(data) {
  selectedName=data.email;
  const airport=data.scope==='airport';
  document.querySelector('#manage-title').textContent=airport?'机场链接':data.email+' · 链接管理';
  const root=document.querySelector('#user-links');root.replaceChildren();
  const make=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text)el.textContent=text;return el;};
  const page=data.links.find(item=>item.rotate==='rotate-page');
  const subscriptions=data.links.filter(item=>item.url&&item!==page);
  const rotateItem=subscriptions.find(item=>item.rotate==='rotate-sub');
  function rotate(item,text){const b=make('button','rotate-link',text);b.dataset.rotate=item.rotate;b.dataset.target=item.target;return b;}
  function address(item,isPage){
    const box=make('div','linkbox relation-linkbox');
    const input=make('input');input.readOnly=true;input.value=item.url;input.setAttribute('aria-label',item.title);box.append(input);
    if(isPage){const open=make('a','page-open','打开 ↗');open.href=item.url;open.target='_blank';open.rel='noopener noreferrer';box.append(open);}
    const copy=make('button','copy-icon relation-copy');copy.innerHTML=copyLinkIcon;copy.dataset.copy=item.url;copy.title='复制'+item.title+'链接';copy.setAttribute('aria-label',copy.title);box.append(copy);return box;
  }
  const section=make('section','link-relation');const heading=make('div','relation-heading');
  heading.append(make('h3','',page?.title||(airport?'机场订阅网页':'个人网页')));
  if(page?.rotate)heading.append(rotate(page,'更换网页链接'));
  section.append(heading);
  if(page?.url)section.append(address(page,true));else section.append(make('p','subtle','网页尚未生成'));
  const children=make('div','relation-children');
  const childHeading=make('div','relation-child-heading');childHeading.append(make('span','','订阅链接'));
  if(rotateItem)childHeading.append(rotate(rotateItem,'更换订阅链接'));
  children.append(childHeading);
  for(const item of subscriptions){const row=make('div','relation-subscription');const label=make('div','relation-subtitle');label.append(make('strong','',item.title==='仅节点'?'仅节点订阅':item.title));const composition=item.composition||(item.title==='仅节点'?'仅自建节点':'');if(composition)label.append(make('span','relation-composition',composition));row.append(label,address(item,false));children.append(row);}
  if(!subscriptions.length)children.append(make('p','subtle','暂无已发布订阅'));
  section.append(children);root.append(section);
  root.append(make('p','warning-note','更换网页链接不影响已导入的订阅。请勿公开分享这些链接。'));
}
function askTask(action, target) {
  const descriptions = {
    'delete-history':['删除全部执行记录','删除全部已结束的执行记录，保留等待中和执行中的任务。不会撤销操作或更改订阅配置。删除后无法恢复。'],
    'rotate-page':['更换网页链接','旧网页链接将立即失效，已导入客户端的订阅不受影响。请确认目标用户。'],
    'rotate-sub':['更换订阅链接','该用户的旧订阅地址将失效，所有客户端需要重新导入。其个人网页地址保留。owner 操作后可能需要重新登录。'],
    'refresh-airport':['刷新 AmyTelecom 订阅','获取最新机场节点与流量信息，保留现有订阅地址。'],
    'update':['更新代码','更新项目代码与服务配置，不生成用户订阅。网页可能短暂中断并要求重新登录。'],
    'sync':['生成配置','使用当前项目代码生成全部用户订阅配置，保留现有订阅地址。']
  };
  if (!(action in descriptions)) return;
  const [title, defaultDetail] = descriptions[action];
  const detail=action==='rotate-sub'&&target==='airport'?'旧机场专用订阅地址将失效，使用它的客户端需要重新导入。成员个人网页及个人订阅保持不变。':defaultDetail;
  confirmation = {action, target, request_id: crypto.randomUUID()};
  document.querySelector('#confirm-title').textContent = target && action !== 'delete-history' && selectedName ? selectedName + ' · ' + title : title;
  document.querySelector('#confirm-text').textContent = detail;
  document.querySelector('#confirm-action').textContent = '确认执行';
  confirmDialog.showModal();
}

function showTaskProgress(message, failed=false) {
  const status=document.querySelector('#task-progress');
  if(!status)return;
  status.textContent=message;
  const footer=status.closest('.maintenance-footer');
  footer.hidden=!message;
  footer.classList.toggle('task-failed',failed);
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
    const failed=!current && rows[0] && ['failed','interrupted'].includes(rows[0].status);
    showTaskProgress(current ? current.phase : failed ? rows[0].phase || rows[0].label : '',Boolean(failed));
    for (const button of document.querySelectorAll('button[data-task],button[data-rotate]')) button.disabled = tasksPending;
    if (rows[0] && rows[0].status === 'success' && completedTask !== rows[0].id) {
      completedTask = rows[0].id;
      if (manageDialog.open && selectedUser) drawUserLinks(await fetchLinks(selectedUser));
    }
  } catch {
    showTaskProgress('暂时无法取得结果，更新期间请稍后重试或重新登录。',true);
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
  if(button.hasAttribute('data-panel-entry')){notify(button.dataset.panelState==='unknown'?'无法读取面板入口，请到终端检查设置。':'3x-ui 面板公网访问已关闭，请通过 SSH 隧道访问；终端菜单可查看使用说明。');return;}
  if (button.dataset.user || button.hasAttribute('data-airport-links')) {
    selectedUser = button.hasAttribute('data-airport-links')?'airport':button.dataset.user;
    document.querySelector('#manage-title').textContent = '读取用户链接';
    document.querySelector('#user-links').textContent = '正在读取…';
    manageDialog.showModal();
    try { const data = await fetchLinks(selectedUser); drawUserLinks(data); }
    catch { document.querySelector('#user-links').textContent = '读取失败，请刷新或重新登录。'; }
    return;
  }
  if (button.dataset.copy) {
    try { await navigator.clipboard.writeText(button.dataset.copy); markLinkCopied(button); notify('已复制链接'); }
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
    if (!document.hidden && tasksPending) refreshTasks();
  });
  window.addEventListener('pagehide', () => clearTimeout(taskTimer));
}

async function showBackupStatus() {
  const dialog=document.querySelector('#backup-dialog'),root=document.querySelector('#backup-content');
  root.textContent='正在读取…';dialog.showModal();
  try{
    const response=await fetch(adminBase+'/backup',{cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(5000)});
    if(!response.ok)throw new Error('unavailable');
    const data=await response.json();root.replaceChildren();
    const make=(tag,cls,text)=>{const el=document.createElement(tag);if(cls)el.className=cls;if(text)el.textContent=text;return el;};
    const stamp=value=>value?new Date(value*1000).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false})+' 北京时间':'暂无';
    const badge=make('span','badge',data.label);if(data.last_error)badge.classList.add('backup-error-badge');else if(!data.enabled)badge.classList.add('backup-neutral-badge');root.append(badge);
    const entry=document.querySelector('.backup-entry-state');if(entry)entry.textContent=data.label;
    if(!data.configured){root.append(make('p','backup-empty',data.last_error?'备用配置不可用，请到终端检查。':'尚未设置备用订阅链接或手动节点。自建策略组仍保留，只包含实际主节点。'));return;}
    const summary=make('div','backup-summary');
    const size=value=>{let n=value,unit='B';for(const next of ['KB','MB','GB','TB']){if(n<1024)break;n/=1024;unit=next;}return n.toFixed(1)+' '+unit;};
    const quota=data.traffic?size(data.traffic.upload+data.traffic.download)+' / '+(data.traffic.total?size(data.traffic.total)+' · 剩余 '+size(Math.max(0,data.traffic.total-data.traffic.upload-data.traffic.download)):'不限额'):'来源未提供';
    for(const [label,value]of [['合并节点',data.nodes.length+' 个节点'],['订阅最近刷新',stamp(data.last_success)],['订阅来源额度',quota]]){const item=make('div');item.append(make('label','',label),make('strong','',value));summary.append(item);}root.append(summary);
    if(data.last_error)root.append(make('p','backup-notice','订阅刷新失败，保留上次成功缓存；手动节点不受影响。'));
    if(!data.enabled)root.append(make('p','backup-notice','备用分发已停用，当前仅保留缓存供查看。'));
    const details=make('details','backup-details');details.open=true;details.append(make('summary','','节点与适用用户'));
    const grid=make('div','backup-detail-grid'),sources=make('div');
    for(const [label,nodes]of [['订阅来源',data.subscription_nodes||[]],['手动导入',data.manual_nodes||[]]]){sources.append(make('h3','',label+' · '+nodes.length+' 个'));const list=make('ul','backup-node-list');for(const name of nodes)list.append(make('li','',name));if(!nodes.length)list.append(make('li','subtle','暂无节点'));sources.append(list);}
    sources.append(make('p','backup-exclusions','手动节点最近修改：'+stamp(data.manual_updated)));
    const users=make('div');users.append(make('h3','','已发布备用的用户'));const names=make('div','backup-users');for(const name of data.users)names.append(make('span','backup-user',name));if(!data.users.length)names.append(make('span','subtle','无'));users.append(names,make('p','backup-exclusions','固定排除：'+data.excluded.join('、')));
    grid.append(sources,users);details.append(grid);root.append(details);
  }catch{root.textContent='无法取得备用状态，请稍后重试或重新登录。';}
}


document.addEventListener('click',event=>{document.querySelectorAll('.reset-info[open]').forEach(item=>{if(!item.contains(event.target))item.open=false;});});
document.addEventListener('keydown',event=>{if(event.key==='Escape')document.querySelectorAll('.reset-info[open]').forEach(item=>item.open=false);});

// Desktop-only shared 3x-ui stream leases; never renew while hidden.
const speedDesktop=matchMedia('(min-width:761px)');
const speedViewer=crypto.randomUUID();
let speedEnabled=false,speedTimer=null,speedController=null,speedGeneration=0,speedOriginals=[];
function speedRows(){return [...document.querySelectorAll('#members tbody tr')].map(row=>({key:row.querySelector('[data-online-client]')?.dataset.onlineClient,cell:row.querySelector('td:nth-child(4)')}));}
function rateText(value){
  if(typeof value!=='number'||!Number.isFinite(value)||value<0)return '—';
  if(value<1024)return Math.round(value)+' B/s';
  if(value<1024*1024)return (value/1024).toFixed(1)+' KB/s';
  return (value/1024/1024).toFixed(1)+' MB/s';
}
function paintSpeedValues(values){
  for(const {key,cell} of speedRows()){
    if(!cell)continue;
    const block=document.createElement('div');block.className='member-transfer';
    for(const [field,arrow] of [['upload','↑'],['download','↓']]){
      const span=document.createElement('span');span.textContent=arrow+' '+rateText(values[key]?.[field]);
      span.title=field==='upload'?'3x-ui 实时上传速度':'3x-ui 实时下载速度';block.append(span);
    }
    cell.replaceChildren(block);
  }
}
function releaseSpeedViewer(){
  if(!adminBase||!csrfToken)return;
  const body=new URLSearchParams({csrf:csrfToken,viewer:speedViewer,active:'no'});
  fetch(adminBase+'/speeds',{method:'POST',body,credentials:'same-origin',keepalive:true,redirect:'error'}).catch(()=>{});
}
function pauseSpeedRequests(release=false){
  if(release&&speedEnabled)releaseSpeedViewer();
  clearTimeout(speedTimer);speedTimer=null;speedGeneration++;
  speedController?.abort();speedController=null;
}
async function querySpeeds(){
  if(!speedEnabled||document.hidden||!speedDesktop.matches||!adminBase)return;
  const generation=speedGeneration,controller=new AbortController();speedController=controller;
  const timeout=setTimeout(()=>controller.abort(),5000);
  try{
    const body=new URLSearchParams({csrf:csrfToken,viewer:speedViewer,active:'yes'});
    const response=await fetch(adminBase+'/speeds',{method:'POST',body,cache:'no-store',credentials:'same-origin',redirect:'error',signal:controller.signal});
    if(response.status===401){setSpeedMode(false);notify('登录已过期，请重新登录。');return;}
    if(!response.ok)throw new Error('unavailable');
    const data=await response.json();
    if(generation!==speedGeneration||!speedEnabled||document.hidden)return;
    if(data.status==='panel_login_required'){setSpeedMode(false);notify('请重新登录订阅网页，以相同账号密码建立 3x-ui 会话；若启用两步验证需另行配置。');return;}
    paintSpeedValues(data.speeds||{});
  }catch{
    if(generation===speedGeneration&&speedEnabled&&!document.hidden)paintSpeedValues({});
  }finally{
    clearTimeout(timeout);
    if(generation===speedGeneration){speedController=null;if(speedEnabled&&!document.hidden&&speedDesktop.matches)speedTimer=setTimeout(querySpeeds,3000);}
  }
}
function setSpeedMode(enabled){
  pauseSpeedRequests(true);
  const button=document.querySelector('[data-speed-toggle]');
  speedEnabled=Boolean(enabled&&speedDesktop.matches&&button&&adminBase);
  if(!button)return;
  button.setAttribute('aria-pressed',String(speedEnabled));
  button.textContent=speedEnabled?'实时速度 ⇄':'上传 / 下载 ⇄';
  button.title=speedEnabled?'点击恢复累计流量；直接读取 3x-ui 实时推送，与面板相同口径；后台断开':'切换累计流量与 3x-ui 实时速度';
  document.querySelector('#members .user-table')?.classList.toggle('live-speeds',speedEnabled);
  if(speedEnabled){speedOriginals=speedRows().map(({key,cell})=>({key,html:cell.innerHTML}));paintSpeedValues({});querySpeeds();}
  else{for(const {key,cell} of speedRows()){const original=speedOriginals.find(row=>row.key===key);if(original)cell.innerHTML=original.html;}speedOriginals=[];}
}
document.addEventListener('click',event=>{if(event.target.closest('[data-speed-toggle]'))setSpeedMode(!speedEnabled);});
document.addEventListener('visibilitychange',()=>{
  pauseSpeedRequests(true);
  if(speedEnabled){paintSpeedValues({});if(!document.hidden)querySpeeds();}
});
speedDesktop.addEventListener('change',()=>{if(!speedDesktop.matches)setSpeedMode(false);});
window.addEventListener('pagehide',()=>pauseSpeedRequests(true));
window.addEventListener('pageshow',event=>{if(event.persisted&&speedEnabled&&!document.hidden)querySpeeds();});

refreshOverviewOnEntry();
