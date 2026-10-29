"""Private backup source and cache; only sanitized status leaves this module."""
import json
import os
import stat
import tempfile
import time
from dataclasses import asdict
from pathlib import Path

import yaml

from clash_sub.operation import ServiceError
from clash_sub.sources import download_airport_document, _valid_airport_url
from clash_sub.checks import _validate_proxies

FILENAME = 'backup-source.json'
PROVIDER = 'Backup'
PRIMARY_GROUP = '🏴\u200d☠️ 自建节点'
BACKUP_GROUP = '🏴\u200d☠️ 备用线路'
EXCLUDED_USERS = frozenset({'42vio', 'openwrt'})
INTERVAL = 86400
LIMIT = 5 * 1024 * 1024


def eligible(email):
    return email not in EXCLUDED_USERS


def read(root):
    path = Path(root) / FILENAME
    if not path.exists() and not path.is_symlink():
        return None
    try:
        with path.open('rb') as handle:
            info = os.fstat(handle.fileno())
            if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise ValueError
            raw = handle.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError
        data = json.loads(raw)
        if not isinstance(data,dict) or data.get('version') != 1 or (data.get('url') is not None and not _valid_airport_url(data.get('url'))) or type(data.get('enabled')) is not bool:
            raise ValueError
        manual = data.get('manual_nodes', [])
        if manual:
            validate_nodes(manual)
        elif manual != []:
            raise ValueError
        if data.get('manual_updated') is not None and type(data['manual_updated']) is not int:
            raise ValueError
        if data.get('url') is None and (data.get('nodes') or not manual):
            raise ValueError
        if data.get('nodes'):
            validate_nodes(data['nodes'])
        elif data.get('nodes') != []:
            raise ValueError
        if data.get('last_error') not in (None,'backup_download_failed'):
            raise ValueError
        if data.get('last_success') is not None and type(data['last_success']) is not int:
            raise ValueError
        traffic=data.get('traffic')
        if traffic is not None and (not isinstance(traffic,dict) or set(traffic)!={'upload','download','total','expiry_ms'} or any(type(v) is not int or v<0 for v in traffic.values())):
            raise ValueError
        return data
    except Exception:
        raise ServiceError('backup_source_invalid') from None


def validate_nodes(nodes):
    if not isinstance(nodes,list) or not 0 < len(nodes) <= 1000:
        raise ValueError('backup_nodes_invalid')
    _validate_proxies(nodes)
    for node in nodes:
        if not isinstance(node.get('type'),str) or not isinstance(node.get('server'),str) or not node['server'] or type(node.get('port')) is not int or not 1<=node['port']<=65535:
            raise ValueError('backup_nodes_invalid')
        if node.get('dialer-proxy'):
            raise ValueError('backup_nodes_invalid')


def _write(root, data):
    root=Path(root)
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Verify an existing record before replacing it (including symlinks).
    read(root)
    payload=json.dumps(data,ensure_ascii=False).encode()
    if len(payload)>LIMIT:
        raise ServiceError('backup_source_invalid')
    fd,name=tempfile.mkstemp(prefix='.backup-source-',dir=root)
    try:
        with os.fdopen(fd,'wb') as handle:
            os.fchmod(handle.fileno(),0o600);handle.write(payload);handle.flush();os.fsync(handle.fileno())
        os.replace(name,root/FILENAME)
        directory=os.open(root,os.O_RDONLY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:
        if os.path.exists(name):os.unlink(name)


def refresh(root, url=None, *, download=download_airport_document, validate=None, now=None):
    previous=read(root)
    source=url if url is not None else previous['url'] if previous else None
    if source is None and previous and previous.get('manual_nodes'):
        return previous
    if not _valid_airport_url(source):
        raise ServiceError('backup_url_invalid')
    current=dict(previous or {'version':1,'url':source,'enabled':True,'nodes':[],'last_success':None,'traffic':None,'last_error':None})
    try:
        downloaded=download(source,LIMIT)
        document=yaml.safe_load(downloaded.document)
        nodes=document.get('proxies') if isinstance(document,dict) else None
        validate_nodes(nodes)
        if validate is not None:validate(nodes)
        # Store only nodes, never remote rules, groups or scripts.
        current.update(url=source,enabled=True,nodes=nodes,last_success=int(time.time() if now is None else now),last_error=None,traffic=asdict(downloaded.traffic) if downloaded.traffic else None)
        merged=combined_nodes(current)
        if validate is not None:validate(merged)
        _write(root,current)
    except Exception:
        current=dict(previous or current)
        current['last_error']='backup_download_failed'
        _write(root,current)
        raise ServiceError('backup_download_failed') from None
    return current


def remove(root):
    try:data=read(root)
    except ServiceError:data=None
    if data and data.get('manual_nodes'):
        data.update(url=None,nodes=[],last_success=None,last_error=None,traffic=None)
        _write(root,data)
        return
    _remove_file(root)


def _remove_file(root):
    path=Path(root)/FILENAME
    try:info=path.lstat()
    except FileNotFoundError:return
    # Permit deletion of a corrupt record, but never follow a link or
    # remove an object that does not satisfy the private-file contract.
    if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.geteuid() or stat.S_IMODE(info.st_mode)!=0o600:
        raise ServiceError('backup_source_invalid')
    path.unlink()
    directory=os.open(root,os.O_RDONLY)
    try:os.fsync(directory)
    finally:os.close(directory)


def status(root):
    try:
        data=read(root)
    except ServiceError:
        return {'configured':False,'enabled':False,'nodes':[],'last_success':None,'last_error':'backup_source_invalid','traffic':None}
    if not data:
        return {'configured':False,'enabled':False,'nodes':[],'last_success':None,'last_error':None,'traffic':None}
    subscription=[n['name'] for n in data['nodes']]
    manual=[n['name'] for n in data.get('manual_nodes',[])]
    return {k:data[k] for k in ('enabled','last_success','last_error','traffic')} | {'configured':True,'nodes':[n['name'] for n in combined_nodes(data)],'subscription_nodes':subscription,'manual_nodes':manual,'manual_updated':data.get('manual_updated'),'subscription_configured':bool(data.get('url'))}


def combined_nodes(data):
    """Deduplicate connection settings; deterministically disambiguate names."""
    result, seen, names = [], set(), set()
    for node in data['nodes'] + data.get('manual_nodes', []):
        identity=json.dumps({k:v for k,v in node.items() if k!='name'},sort_keys=True,ensure_ascii=False)
        if identity in seen:continue
        seen.add(identity)
        current=dict(node);base=node['name'];name=base;number=2
        while name in names:
            name='%s (%d)' % (base,number);number+=1
        current['name']=name;names.add(name);result.append(current)
    if result:validate_nodes(result)
    return result


def import_nodes(root, text, *, validate=None, now=None):
    from clash_sub.node_import import parse_links
    nodes=parse_links(text)
    # Assign unique names before handing a candidate to the core.
    candidate=combined_nodes({'nodes':[], 'manual_nodes':nodes})
    if validate is not None:validate(candidate)
    current=dict(read(root) or {'version':1,'url':None,'enabled':True,'nodes':[],'last_success':None,'traffic':None,'last_error':None})
    current.update(manual_nodes=candidate,manual_updated=int(time.time() if now is None else now),enabled=True)
    if validate is not None:validate(combined_nodes(current))
    _write(root,current)
    return current


def remove_manual(root):
    current=read(root)
    if current is None:return
    if not current.get('url'):
        _remove_file(root)
        return
    current.pop('manual_nodes',None);current.pop('manual_updated',None)
    _write(root,current)
