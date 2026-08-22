import fcntl
import json
import os
import stat
from dataclasses import replace
from pathlib import Path
from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS, RuntimeState

class ServiceError(RuntimeError):
    def __init__(self, code): self.code = code; super().__init__(code)

class _OperationLock:
    def __init__(self, path): self.path, self.handle = Path(path), None
    def __enter__(self):
        root=self.path.parent
        if not root.is_absolute() or root.is_symlink() or not root.is_dir() or stat.S_IMODE(root.stat().st_mode)!=0o700 or root.stat().st_uid not in {0,os.geteuid()}: raise ServiceError("operation_lock_invalid")
        ancestor=Path(root.anchor)
        for part in root.parts[1:]:
            ancestor/=part
            if ancestor.is_symlink(): raise ServiceError("operation_lock_invalid")
        flags=os.O_RDWR|os.O_CREAT|getattr(os,"O_NOFOLLOW",0)
        try:
            descriptor=os.open(self.path,flags,0o600); os.fchmod(descriptor,0o600); details=os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode)!=0o600 or details.st_nlink!=1 or details.st_uid not in {0,os.geteuid()}: raise OSError
            self.handle=os.fdopen(descriptor,"r+b",closefd=True)
        except OSError:
            try: os.close(descriptor)
            except (OSError,UnboundLocalError): pass
            raise ServiceError("operation_lock_invalid") from None
        try: fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close(); self.handle = None; raise ServiceError("operation_busy") from None
        return self
    def __exit__(self, *_):
        if self.handle: fcntl.flock(self.handle, fcntl.LOCK_UN); self.handle.close()

class ClashSubService:
    def __init__(self, config, *, read_snapshot, load_state, reconcile_state, rotate_user_token, fetch_xui_proxies, download_airport_proxies, load_proxy_snapshot, render_user_bundle, validate_clash, mihomo_validator, release_store, render_routes, activate_runtime, runner, snapshot_encoder=None, state_sink=None, lock_factory=None):
        self.config=config; self._read_snapshot=read_snapshot; self._load_state=load_state; self._reconcile=reconcile_state; self._rotate=rotate_user_token; self._fetch=fetch_xui_proxies; self._download=download_airport_proxies; self._load_proxy=load_proxy_snapshot; self._render=render_user_bundle; self._validate=validate_clash; self._mihomo=mihomo_validator; self._releases=release_store; self._render_routes=render_routes; self._activate_runtime=activate_runtime; self._runner=runner; self._encode=snapshot_encoder or _snapshot_bytes; self._sink=state_sink or (lambda _: None); self._lock_factory=lock_factory or _OperationLock
    def sync_all(self):
        with self._lock():
            snapshot,state=self._reconciled(); airport,home,bad_owner=self._owner_sources(); next_state=state; candidates=[]; updated=[]; errors=[]; tokens=tuple(u.token for u in state.users.values())
            for client in snapshot.clients:
                user=state.users.get(client.client_id); owner=client.client_id==state.owner_client_id
                if not user or not client.enabled: continue
                if owner and bad_owner: errors.append(_error(client,"owner_update_failed")); continue
                try:
                    release=self._prepare(client,owner,snapshot.source_url(client),airport if owner else (),home if owner else (),tokens,candidates=candidates)
                    if release: next_state=_with_release(next_state,client.client_id,release.release_id); updated.append(_result(client,release))
                except Exception:
                    owned=[item for item in candidates if item[0] == client.client_id]; self._discard(owned); candidates[:]=[item for item in candidates if item[0] != client.client_id]; errors.append(_error(client,"owner_update_failed" if owner else "member_update_failed"))
            try: self._activate(snapshot.clients,next_state,candidates)
            except ServiceError: self._discard(candidates); raise
            return self._finish(next_state,candidates,updated,errors)
    def update_airport(self,url):
        with self._lock():
            snapshot,state=self._reconciled(); candidates=[]
            try:
                owner=_client(snapshot.clients,state.owner_client_id); home=self._load_proxy(_home_path(self.config)); airport=self._download(url,self.config.max_source_bytes); release=self._prepare(owner,True,snapshot.source_url(owner),airport,home,tuple(u.token for u in state.users.values()),url,candidates); next_state=state if not release else _with_release(state,owner.client_id,release.release_id)
                self._activate(snapshot.clients,next_state,candidates,[( _airport_path(self.config),self._encode(airport),0o600)])
            except Exception: self._discard(candidates); raise ServiceError("airport_activation_failed") from None
            return self._finish(next_state,candidates,[_result(owner,release)] if release else [],[])
    def traffic_update(self):
        with self._lock():
            try:
                snapshot=self._read_snapshot(self.config.xui_database); state=self._load_state(_state_path(self.config))
                if state is None: raise ValueError
                self._activate(snapshot.clients,state,[])
            except Exception: raise ServiceError("traffic_activation_failed") from None
            return self._finish(state,[],[],[])
    def rollback(self,user,release):
        with self._lock():
            client_id=_client_id(user); snapshot,state=self._reconciled()
            try:
                verified=self._releases.verify_release(client_id,release); _shape(verified.public_paths,client_id==state.owner_client_id); next_state=_with_release(state,client_id,verified.release_id); self._activate(snapshot.clients,next_state,[(client_id,verified)])
            except Exception: raise ServiceError("rollback_release_invalid") from None
            self._observe(next_state); return _result(_client(snapshot.clients,client_id),verified)
    def rotate_link(self,user):
        with self._lock():
            client_id=_client_id(user); snapshot,state=self._reconciled(); identity=state.users.get(client_id); client=_find(snapshot.clients,client_id)
            if not identity or not identity.active or not identity.current_release or not client or not client.enabled: raise ServiceError("rotation_not_allowed")
            try: next_state=self._rotate(state,client_id); self._activate(snapshot.clients,next_state,[])
            except Exception: raise ServiceError("rotation_activation_failed") from None
            self._observe(next_state); user=next_state.users[client_id]; return {"client_id":client_id,"token":user.token,"urls":tuple(_urls(self.config,user.token,client_id==next_state.owner_client_id))}
    def links(self):
        snapshot,state=self._reconciled(); clients={c.client_id:c for c in snapshot.clients}; return tuple({"client_id":u.client_id,"email":u.email,"readable_code":u.readable_code,"urls":tuple(_urls(self.config,u.token,u.client_id==state.owner_client_id))} for u in sorted(state.users.values(),key=lambda u:u.client_id) if u.active and u.current_release and clients.get(u.client_id) and clients[u.client_id].enabled)
    def status(self):
        _,state=self._reconciled(); return {"owner_client_id":state.owner_client_id,"users":tuple({"client_id":u.client_id,"email":u.email,"active":u.active,"current_release":u.current_release} for u in sorted(state.users.values(),key=lambda u:u.client_id))}
    def history(self,user): return tuple({"release_id":r.release_id,"variants":tuple(r.public_paths)} for r in self._releases.history(_client_id(user)))
    def _prepare(self,client,owner,url,airport,home,tokens,transient=None,candidates=None):
        xui=self._fetch(url,self.config.max_source_bytes); bundle=self._render(owner,xui,airport,home,self.config.template_root); _shape(bundle,owner); forbidden=tokens+(url,client.sub_id)+((transient,) if transient else ())
        for text in bundle.values(): self._validate(text,forbidden)
        release=self._releases.prepare(client.client_id,bundle,{"inputs":_digest(xui,airport,home)})
        if release:
            if candidates is not None: candidates.append((client.client_id,release))
            for path in release.public_paths.values(): self._mihomo.validate(path)
        return release
    def _activate(self,clients,state,candidates,extra=()):
        try: self._activate_runtime(self.config,state,self._render_routes(self.config,_routable(state),clients),self._runner,tuple(extra)+tuple(self._releases.current_artifact(i,r.release_id) for i,r in candidates))
        except Exception: raise ServiceError("sync_activation_failed") from None
    def _finish(self,state,candidates,updated,errors):
        self._observe(state)
        for i,_ in candidates:
            try:self._releases.prune(i)
            except Exception:errors.append({"client_id":i,"code":"release_cleanup_failed"})
        return {"updated":tuple(updated),"errors":tuple(errors)}
    def _discard(self,candidates):
        failed=False
        for i,r in candidates:
            try:self._releases.discard_unreferenced(i,r.release_id)
            except Exception:failed=True
        if failed: raise ServiceError("release_cleanup_failed")
    def _reconciled(self):
        try:
            snapshot=self._read_snapshot(self.config.xui_database); return snapshot,self._reconcile(self._load_state(_state_path(self.config)),snapshot.clients,self.config.owner_email)
        except Exception:raise ServiceError("xui_snapshot_failed") from None
    def _owner_sources(self):
        try:return self._load_proxy(_airport_path(self.config)),self._load_proxy(_home_path(self.config)),False
        except Exception:return (),(),True
    def _observe(self,state):
        try:self._sink(state)
        except Exception:pass
    def _lock(self):return self._lock_factory(Path(self.config.private_root)/"operation.lock")

def _with_release(s,i,r):
    users=dict(s.users); users[i]=replace(users[i],current_release=r); return RuntimeState(s.schema_version,s.owner_client_id,users)
def _routable(s):return RuntimeState(s.schema_version,s.owner_client_id,{i:replace(u,active=False) if u.active and not u.current_release else u for i,u in s.users.items()})
def _shape(b,owner):
    if tuple(b)!=(OWNER_VARIANTS if owner else MEMBER_VARIANTS):raise ValueError
def _digest(*v):return __import__('hashlib').sha256(json.dumps(v,sort_keys=True,default=str).encode()).hexdigest()
def _find(cs,i):return next((c for c in cs if c.client_id==i),None)
def _client(cs,i):
    c=_find(cs,i)
    if not c:raise ValueError
    return c
def _client_id(i):
    if isinstance(i,bool) or not isinstance(i,int) or i<1:raise ServiceError("invalid_client")
    return i
def _urls(c,t,o):return ["https://%s/s/%s/clash-%s.yaml"%(c.subscription_authority,t,v) for v in (OWNER_VARIANTS if o else MEMBER_VARIANTS)]
def _result(c,r):return {"client_id":c.client_id,"email":c.email,"release_id":r.release_id,"variants":tuple(r.public_paths)}
def _error(c,code):return {"client_id":c.client_id,"email":c.email,"code":code}
def _state_path(c):return Path(c.private_root)/"state.json"
def _airport_path(c):return Path(c.private_root)/"airport.yaml"
def _home_path(c):return Path(c.private_root)/"home.yaml"
def _snapshot_bytes(p):return json.dumps({"proxies":p},sort_keys=True).encode()
