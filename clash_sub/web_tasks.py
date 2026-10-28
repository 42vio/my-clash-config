"""Fixed owner operations, executed outside the web service by a systemd worker."""

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

from clash_sub.operation import _OperationLock, ServiceError
from clash_sub.state import load_state
from clash_sub.web_settings import admin_prefix, read_settings, write_settings

ACTIONS = {'rotate-page':'更换网页链接', 'rotate-sub':'更换订阅链接', 'refresh-airport':'刷新 Amy 订阅', 'update-sync':'更新代码并生成配置'}
STATUSES = {'queued':'等待执行', 'running':'执行中', 'success':'成功', 'failed':'失败', 'interrupted':'执行中断，请检查'}


def _read(root):
    path = Path(root) / 'web-tasks.json'
    if not path.exists() and not path.is_symlink():
        return []
    with path.open('rb') as handle:
        info = os.fstat(handle.fileno())
        if path.is_symlink() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.geteuid():
            raise ValueError('task_state_invalid')
        payload = handle.read(1024 * 1024 + 1)
    if len(payload) > 1024 * 1024:
        raise ValueError('task_state_invalid')
    rows = json.loads(payload)
    if not isinstance(rows, list) or len(rows) > 30:
        raise ValueError('task_state_invalid')
    for row in rows:
        if not isinstance(row, dict) or not re.fullmatch(r'[0-9a-f]{32}',row.get('id','')) or row.get('action') not in ACTIONS or row.get('status') not in STATUSES or type(row.get('created_at')) not in (int,float) or type(row.get('updated_at')) not in (int,float) or not re.fullmatch(r'[0-9a-f]{64}',row.get('authorization','')) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',row.get('request_id','')) or not (row.get('target') in {'airport','admin',None} or isinstance(row.get('target'),str) and re.fullmatch(r'[1-9][0-9]{0,18}',row['target'])):
            raise ValueError('task_state_invalid')
    return rows


def _write(root, rows):
    fd, name = tempfile.mkstemp(prefix='.web-tasks-',dir=root)
    try:
        with os.fdopen(fd,'w') as handle:
            os.fchmod(handle.fileno(),0o600)
            json.dump(rows,handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name,Path(root)/'web-tasks.json')
        directory = os.open(root,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def authorization(settings, state):
    if not settings.get('enabled') or not admin_prefix(settings,state):
        raise ValueError('owner_authorization_invalid')
    record = settings['admin']
    from clash_sub.web_settings import admin_username
    return hashlib.sha256((record['token']+record['binding']+settings['password_hash']+admin_username(settings)).encode()).hexdigest()


def _status(row, now):
    if row['status'] in {'queued','running'} and row.get('boot_id') is not None and row['boot_id'] != _boot_id():
        return 'interrupted'
    if row['status'] in {'queued','running'} and now-row['updated_at'] > 3700:
        return 'interrupted'
    return row['status']


def _boot_id():
    try:return Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:return None


def history(config, *, now=None):
    now = time.time() if now is None else now
    return [{'id':r['id'],'action':r['action'],'title':ACTIONS[r['action']],'target':r['target'],'status':_status(r,now),'label':STATUSES[_status(r,now)],'created_at':r['created_at'],'updated_at':r['updated_at'],'phase':r.get('phase',''),'target_label':r.get('target_label',r['target'] or '')} for r in _read(config.private_root)]


def enqueue(config, action, target, request_id, actor, *, runner=subprocess.run, now=None):
    now = time.time() if now is None else now
    if action not in ACTIONS or not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{16,64}',request_id):
        raise ValueError('invalid_task')
    state = load_state(Path(config.private_root)/'state.json')
    settings = read_settings(config.private_root)
    if not secrets.compare_digest(actor,authorization(settings,state)):
        raise ValueError('owner_authorization_invalid')
    if action in {'rotate-page','rotate-sub'}:
        if target not in {str(k) for k in state.users} | ({'airport','admin'} if action == 'rotate-page' else set()):
            raise ValueError('invalid_task_target')
    elif target is not None:
        raise ValueError('invalid_task_target')
    with _OperationLock(Path(config.private_root)/'web-task.lock'):
        rows = _read(config.private_root)
        existing = next((r for r in rows if r['request_id']==request_id),None)
        if existing:
            if existing['action'] != action or existing['target'] != target or existing['authorization'] != actor:
                raise ValueError('invalid_task')
            return existing['id']
        if any(_status(r,now) in {'queued','running'} for r in rows):
            raise ServiceError('operation_busy')
        row = {'id':secrets.token_hex(16),'request_id':request_id,'authorization':actor,'action':action,'target':target,'status':'queued','created_at':now,'updated_at':now,'phase':'等待后台任务启动','boot_id':_boot_id()}
        identity = state.users[state.owner_client_id if target in {'airport','admin'} else int(target)] if target is not None else None
        row['target_binding'] = hashlib.sha256(identity.token.encode()).hexdigest() if identity else None
        row['target_label'] = identity.email + ('（机场专用）' if target=='airport' else '（管理员入口）' if target=='admin' else '') if identity else 'AmyTelecom' if action=='refresh-airport' else '全部用户'
        _write(config.private_root,[row]+rows[:29])
    try:
        result = runner([str(config.systemctl_binary),'start','--no-block','clash-sub-task@%s.service'%row['id']],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=5,check=False)
        if result.returncode:raise ValueError('task_start_failed')
    except Exception:
        update(config,row['id'],'failed','后台任务未能启动',only_queued=True)
        raise ValueError('task_start_failed') from None
    return row['id']


def update(config, task_id, status, phase, *, only_queued=False):
    with _OperationLock(Path(config.private_root)/'web-task.lock'):
        rows = _read(config.private_root)
        row = next((r for r in rows if r['id']==task_id),None)
        if row and (not only_queued or row['status']=='queued'):
            row.update(status=status,phase=phase,updated_at=time.time())
            _write(config.private_root,rows)


def delete_history(config, actor):
    with _OperationLock(Path(config.private_root)/'web-task.lock'):
        state = load_state(Path(config.private_root)/'state.json')
        if not secrets.compare_digest(actor,authorization(read_settings(config.private_root),state)):
            raise ValueError('owner_authorization_invalid')
        rows = _read(config.private_root)
        # Keep unfinished jobs, including those whose status is uncertain.
        _write(config.private_root,[r for r in rows if r['status'] in {'queued','running'}])


def _rebind_rotated(config, before, after, client_id):
    """An authorized subscription rotation preserves independent page addresses."""
    from clash_sub.web_settings import provision_pages
    with _OperationLock(Path(config.private_root)/'operation.lock'):
        settings = read_settings(config.private_root)
        old, new = before.users[client_id], after.users[client_id]
        old_hash, new_hash = (hashlib.sha256(u.token.encode()).hexdigest() for u in (old,new))
        if old.email != new.email or before.owner_client_id != after.owner_client_id:
            raise ValueError('identity_changed')
        for key in (str(client_id),'airport' if client_id==after.owner_client_id else str(client_id)):
            record = settings['pages'].get(key)
            if record and record['binding']==old_hash:record['binding']=new_hash
        if client_id==after.owner_client_id and settings.get('admin',{}).get('binding')==old_hash:
            settings['admin']['binding']=new_hash
        provision_pages(settings,after)
        write_settings(config.private_root,settings)


def run(config, root, task_id, *, service_factory=None, runner=subprocess.run, sync_only=False):
    if not re.fullmatch(r'[0-9a-f]{32}',task_id):raise ValueError('invalid_task')
    from clash_sub.runtime import build_service
    from clash_sub.web_manage import execute
    from clash_sub.manage import run_update
    def check_authorization():
        current = load_state(Path(config.private_root)/'state.json')
        if not secrets.compare_digest(task['authorization'],authorization(read_settings(config.private_root),current)):
            raise ValueError('owner_authorization_invalid')
        if task['target'] is not None:
            identity = current.users.get(current.owner_client_id if task['target'] in {'airport','admin'} else int(task['target']))
            if identity is None or task.get('target_binding') != hashlib.sha256(identity.token.encode()).hexdigest():
                raise ValueError('target_identity_changed')
    @contextmanager
    def guarded_lock(path):
        with _OperationLock(path):
            check_authorization()
            yield
    service_factory = service_factory or (lambda:build_service(root,lock_factory=guarded_lock))
    with _OperationLock(Path(config.private_root)/'web-task.lock'):
        rows = _read(config.private_root)
        row = next((r for r in rows if r['id']==task_id),None)
        if sync_only:
            if not row or _status(row,time.time())!='running' or row['action']!='update-sync' or row.get('phase')!='代码更新成功，正在生成配置':
                raise ValueError('invalid_task')
            task = dict(row)
        elif not row or _status(row,time.time())!='queued':return
        # Copy while locked. Status is claimed before starting any side effects.
        if not sync_only:
            row.update(status='running',phase='校验 owner 权限',updated_at=time.time())
            _write(config.private_root,rows)
            task=dict(row)
    if sync_only:
        check_authorization()
        result = service_factory().sync_all()
        if result.get('errors'):
            raise ValueError('sync_failed')
        return
    try:
        before = load_state(Path(config.private_root)/'state.json')
        check_authorization()
        action,target = task['action'],task['target']
        if action=='rotate-page':
            update(config,task_id,'running','更换网页链接')
            execute(root,'rotate-page',target=target,runner=runner,actor=task['authorization'],authorize=check_authorization)
        elif action=='rotate-sub':
            update(config,task_id,'running','更换订阅链接')
            service_factory().rotate_link(int(target))
            after=load_state(Path(config.private_root)/'state.json')
            _rebind_rotated(config,before,after,int(target))
        elif action=='refresh-airport':
            update(config,task_id,'running','开启机场订阅并刷新 Amy')
            result = service_factory().auto_refresh_airport()
            if result.get('skipped'):
                raise ServiceError('airport_portal_unavailable')
        elif action=='update-sync':
            update(config,task_id,'running','更新代码与服务，网页可能短暂中断')
            run_update(root,runner,authorize=check_authorization)
            update(config,task_id,'running','代码更新成功，正在生成配置')
            result=runner([str(Path(root)/'.venv/bin/python'),str(Path(root)/'bin/clash-sub'),'web-task',task_id,'--sync-only'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=900,check=False)
            if result.returncode:raise ValueError('sync_failed')
        with _OperationLock(Path(config.private_root)/'operation.lock'):
            from clash_sub.web_settings import provision_pages
            settings = read_settings(config.private_root)
            provision_pages(settings,load_state(Path(config.private_root)/'state.json'))
            write_settings(config.private_root,settings)
        update(config,task_id,'success','已完成')
    except Exception as error:
        phase = {
            'airport_activation_missing':'未设置机场订阅开关页面，请先在终端设置',
            'airport_portal_unavailable':'机场开关页面和订阅暂不可用，保留原配置；未自动重试',
            'airport_download_failed':'机场订阅下载失败，保留原配置；未自动重试',
        }.get(error.code if isinstance(error,ServiceError) else None,
              '执行失败，请到终端检查；未自动重试')
        update(config,task_id,'failed',phase)
