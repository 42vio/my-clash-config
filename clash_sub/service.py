import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
from dataclasses import replace
from pathlib import Path

import yaml

from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS, PROFILE_FILENAMES, AirportProvider, RuntimeState
from clash_sub.sources import normalize_xui_endpoints
from clash_sub.state import StateError

class ServiceError(RuntimeError):
    def __init__(self, code): self.code = code; super().__init__(code)

class _OperationLock:
    def __init__(self, path): self.path, self.descriptor = Path(path), None
    def __enter__(self):
        root = self.path.parent
        if not root.is_absolute() or any(part in {".", ".."} for part in root.parts): raise ServiceError("operation_lock_invalid")
        opened=[]; parent=None
        try:
            parent=os.open(root.anchor,os.O_RDONLY|os.O_DIRECTORY)
            for part in root.parts[1:]:
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|getattr(os,"O_NOFOLLOW",0),dir_fd=parent)
                opened.append(parent); parent=child
                details=os.fstat(parent)
                if not stat.S_ISDIR(details.st_mode): raise OSError
            details=os.fstat(parent)
            if stat.S_IMODE(details.st_mode)!=0o700 or details.st_uid not in {0,os.geteuid()}: raise OSError
            flags=os.O_RDWR|os.O_CREAT|getattr(os,"O_NOFOLLOW",0)|getattr(os,"O_CLOEXEC",0)
            self.descriptor=os.open("operation.lock",flags,0o600,dir_fd=parent); os.fchmod(self.descriptor,0o600); details=os.fstat(self.descriptor)
            if not stat.S_ISREG(details.st_mode) or stat.S_IMODE(details.st_mode)!=0o600 or details.st_nlink!=1 or details.st_uid not in {0,os.geteuid()}: raise OSError
        except OSError:
            if isinstance(self.descriptor,int):
                try:os.close(self.descriptor)
                except OSError:pass
            self.descriptor=None
            raise ServiceError("operation_lock_invalid") from None
        finally:
            for descriptor in opened:
                try:os.close(descriptor)
                except OSError:pass
            if parent is not None:
                try:os.close(parent)
                except OSError:pass
        try: fcntl.flock(self.descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try: os.close(self.descriptor)
            except OSError: pass
            finally: self.descriptor = None
            raise ServiceError("operation_busy") from None
        return self
    def __exit__(self, exc_type, *_):
        if self.descriptor is None:return
        error=False
        try: fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        except OSError:error=True
        finally:
            try:os.close(self.descriptor)
            except OSError:error=True
            self.descriptor=None
        if error and exc_type is None: raise ServiceError("operation_lock_invalid")

class ClashSubService:
    def __init__(self, config, *, read_snapshot, load_state, reconcile_state, rotate_user_token, fetch_xui_proxies, download_airport_document, airport_store, render_user_bundle, validate_clash, mihomo_validator, release_store, render_routes, activate_runtime, runner, state_sink=None, lock_factory=None, clock=None, reinitialize_owner=None, recover_runtime=None):
        self.config=config; self._read_snapshot=read_snapshot; self._load_state=load_state; self._reconcile=reconcile_state; self._rotate=rotate_user_token; self._fetch=fetch_xui_proxies; self._download=download_airport_document; self._airport=airport_store; self._render=render_user_bundle; self._validate=validate_clash; self._mihomo=mihomo_validator; self._releases=release_store; self._render_routes=render_routes; self._activate_runtime=activate_runtime; self._runner=runner; self._sink=state_sink or (lambda _: None); self._lock_factory=lock_factory or _OperationLock; self._clock=clock or time.time; self._reinitialize=reinitialize_owner; self._recover_runtime=recover_runtime or (lambda *_, **__: None)
    def sync_all(self):
        with self._lock():
            self._recover()
            snapshot,state=self._reconciled(); self._require_airport(); next_state=state; candidates=[]; updated=[]; errors=[]; tokens=tuple(u.token for u in state.users.values())
            for client in snapshot.clients:
                user=state.users.get(client.client_id); owner=client.client_id==state.owner_client_id
                if not user or not client.enabled: continue
                try:
                    release=self._prepare(client,owner,snapshot.source_url(client),user,tokens,candidates=candidates)
                    if release: next_state=_with_release(next_state,client.client_id,release.release_id); updated.append(_result(client,release))
                except Exception:
                    owned=[item for item in candidates if item[0] == client.client_id]; self._discard(owned); candidates[:]=[item for item in candidates if item[0] != client.client_id]; errors.append(_error(client,"owner_update_failed" if owner else "member_update_failed"))
            try: self._activate(snapshot.clients,next_state,candidates)
            except ServiceError as error: self._journal(errors=(error.code,)); self._discard(candidates); raise
            return self._finish(next_state,candidates,updated,errors)
    def update_airport(self,url):
        # The airport update only replaces the stable provider file; it never
        # reconciles state, fetches x-ui, renders profiles, or activates Nginx.
        with self._lock():
            try:
                document=self._download(url,self.config.max_source_bytes)
                self._airport.replace(document,self._validate_airport_candidate)
            except Exception:
                self._journal(errors=("airport_update_failed",)); raise ServiceError("airport_update_failed") from None
            self._journal(self._clock(),())
            return {"updated": True}
    def traffic_update(self):
        with self._lock():
            self._recover()
            try:
                snapshot=self._read_snapshot(self.config.xui_database); state=self._load_state(_state_path(self.config))
                if state is None or not _traffic_matches_state(snapshot.clients,state): raise ValueError
                self._activate(snapshot.clients,state,[])
            except Exception: self._journal(errors=("traffic_activation_failed",)); raise ServiceError("traffic_activation_failed") from None
            return self._finish(state,[],[],[])
    def rollback(self,user,release):
        with self._lock():
            self._recover()
            client_id=_client_id(user); snapshot,state=self._reconciled()
            identity=state.users.get(client_id); client=_find(snapshot.clients,client_id)
            if not identity or not identity.active or not identity.current_release or not client or not client.enabled: raise ServiceError("rollback_release_invalid")
            try:
                verified=self._releases.verify_release(client_id,release); _shape(verified.public_paths,client_id==state.owner_client_id); next_state=_with_release(state,client_id,verified.release_id); self._activate(snapshot.clients,next_state,[(client_id,verified)])
            except Exception: raise ServiceError("rollback_release_invalid") from None
            self._observe(next_state); return _result(client,verified)
    def rotate_link(self,user):
        with self._lock():
            self._recover()
            client_id=_client_id(user); snapshot,state=self._reconciled(); identity=state.users.get(client_id); client=_find(snapshot.clients,client_id)
            if not identity or not identity.active or not identity.current_release or not client or not client.enabled: raise ServiceError("rotation_not_allowed")
            candidates=[]
            try:
                next_state=self._rotate(state,client_id)
                if client_id==state.owner_client_id:
                    release=self._prepare(client,True,snapshot.source_url(client),next_state.users[client_id],tuple(u.token for u in next_state.users.values()),candidates=candidates)
                    if release: next_state=_with_release(next_state,client_id,release.release_id)
                self._activate(snapshot.clients,next_state,candidates)
            except Exception:
                # Any failure inside the rotation activation transaction keeps
                # the dedicated code; the prior link and release stay live.
                self._discard(candidates); raise ServiceError("rotation_activation_failed") from None
            self._observe(next_state); rotated=next_state.users[client_id]; return {"client_id":client_id,"token":rotated.token,"urls":tuple(_urls(self.config,rotated.token,client_id==next_state.owner_client_id))}
    def links(self):
        with self._lock():
            self._recover()
            snapshot,state=self._reconciled(); clients={c.client_id:c for c in snapshot.clients}; return tuple({"client_id":u.client_id,"email":u.email,"readable_code":u.readable_code,"urls":tuple(_urls(self.config,u.token,u.client_id==state.owner_client_id))} for u in sorted(state.users.values(),key=lambda u:u.client_id) if u.active and u.current_release and clients.get(u.client_id) and clients[u.client_id].enabled)
    def status(self):
        with self._lock():
            self._recover()
            snapshot,state=self._reconciled(); journal=self._read_journal(); pending=tuple({"client_id":c.client_id,"email":c.email} for c in sorted(snapshot.clients,key=lambda c:c.client_id) if _pending_source(state.users.get(c.client_id),c))
            return {"owner_client_id":state.owner_client_id,"last_success":journal["last_success"],"last_errors":journal["last_errors"],"pending":pending,"users":tuple({"client_id":u.client_id,"email":u.email,"active":u.active,"current_release":u.current_release} for u in sorted(state.users.values(),key=lambda u:u.client_id))}
    def history(self,user):
        with self._lock():
            self._recover()
            return tuple({"release_id":r.release_id,"variants":tuple(r.public_paths)} for r in self._releases.history(_client_id(user)))
    def reinitialize_owner(self,user):
        with self._lock():
            self._recover()
            try:
                client_id=_client_id(user); snapshot=self._read_snapshot(self.config.xui_database); previous=self._load_state(_state_path(self.config))
                if previous is None or self._reinitialize is None: raise ValueError
                next_state=self._reinitialize(previous,snapshot.clients,self.config.owner_email,client_id)
                self._activate(snapshot.clients,next_state,[])
            except Exception:
                raise ServiceError("owner_reinitialization_failed") from None
            self._observe(next_state); self._journal(self._clock(),())
            return {"owner_client_id":client_id}
    def _prepare(self,client,owner,url,identity,tokens,transient=None,candidates=None):
        xui=normalize_xui_endpoints(self._fetch(url,self.config.max_source_bytes),self.config.xui_public_endpoint)
        provider=AirportProvider(_provider_url(self.config,identity.token)) if owner else None
        bundle=self._render(owner,xui,provider,self.config.template_root); _shape(bundle,owner); forbidden=tokens+(url,client.sub_id)+((transient,) if transient else ())
        for text in bundle.values(): self._validate(text,forbidden,provider.url if provider else None)
        release=self._releases.prepare(client.client_id,bundle,{"xui":_digest(xui)})
        if release:
            if candidates is not None: candidates.append((client.client_id,release))
            if owner: self._validate_owner_with_mihomo(bundle)
            else:
                for path in release.public_paths.values(): self._mihomo.validate(path)
        return release
    def _validate_airport_candidate(self,path):
        # The staged candidate must be a provider document with real proxy
        # entries, then survive Mihomo mounted as a local file provider.
        document=yaml.safe_load(Path(path).read_bytes())
        if not isinstance(document,dict) or not isinstance(document.get("proxies"),list) or not document["proxies"]: raise ValueError("airport provider document is invalid")
        probe={"proxy-providers":{"AmyTelecom":{"type":"file","path":str(path)}},"proxy-groups":[{"name":"Provider Check","type":"select","use":["AmyTelecom"]}],"rules":["MATCH,Provider Check"]}
        with tempfile.TemporaryDirectory(prefix=".clash-sub-provider-validate.") as directory:
            candidate=Path(directory)/"provider-check.yaml"
            candidate.write_text(yaml.safe_dump(probe,allow_unicode=True,sort_keys=False),encoding="utf-8")
            self._mihomo.validate(candidate)
    def _validate_owner_with_mihomo(self,bundle):
        # The published profile keeps the HTTP provider; Mihomo checks a
        # non-published equivalent that points at the stable local file.
        provider_file=Path(self._airport.path)
        with tempfile.TemporaryDirectory(prefix=".clash-sub-owner-validate.") as directory:
            for variant,text in bundle.items():
                document=yaml.safe_load(text)
                document.setdefault("proxy-providers",{})["AmyTelecom"]={"type":"file","path":str(provider_file)}
                candidate=Path(directory)/("verify-%s.yaml"%variant)
                candidate.write_text(yaml.safe_dump(document,allow_unicode=True,sort_keys=False),encoding="utf-8")
                self._mihomo.validate(candidate)
    def _require_airport(self):
        # The stable provider is a hard precondition for preparing any user.
        try:
            document=self._airport.read()
            parsed=yaml.safe_load(document)
            if not isinstance(parsed,dict) or not isinstance(parsed.get("proxies"),list) or not parsed["proxies"]: raise ValueError
        except Exception:
            raise ServiceError("airport_provider_required") from None
    def _activate(self,clients,state,candidates,extra=()):
        try: self._activate_runtime(self.config,state,self._render_routes(self.config,_routable(state),clients),self._runner,tuple(extra)+tuple(self._releases.current_artifact(i,r.release_id) for i,r in candidates))
        except Exception: raise ServiceError("sync_activation_failed") from None
    def _finish(self,state,candidates,updated,errors):
        self._observe(state)
        for i,_ in candidates:
            try:self._releases.prune(i)
            except Exception:errors.append({"client_id":i,"code":"release_cleanup_failed"})
        self._journal(self._clock(),tuple(item["code"] for item in errors))
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
        except StateError as error:
            if str(error)=="owner_reinitialization_required": raise ServiceError("owner_reinitialization_required") from None
            raise ServiceError("xui_snapshot_failed") from None
        except Exception:
            raise ServiceError("xui_snapshot_failed") from None
    def _recover(self):
        try:self._recover_runtime(self.config,self._runner,reload=True)
        except Exception:raise ServiceError("runtime_recovery_failed") from None
    def _observe(self,state):
        try:self._sink(state)
        except Exception:pass
    def _journal(self,success=None,errors=()):
        # Best-effort sanitized operation journal: timestamps and stable
        # error codes only, never tokens, URLs, or client secrets.
        path=Path(self.config.private_root)/"status.json"
        descriptor=None; temporary=None
        try:
            current=self._read_journal(); payload={"last_success":current["last_success"] if success is None else success,"last_errors":tuple(str(item) for item in errors)}
            descriptor,temporary=tempfile.mkstemp(prefix=".%s."%path.name,dir=str(path.parent))
            try:
                os.fchmod(descriptor,0o600); content=json.dumps(payload,sort_keys=True).encode("utf-8"); offset=0
                while offset<len(content):
                    written=os.write(descriptor,content[offset:])
                    if written<=0:raise OSError
                    offset+=written
                os.fsync(descriptor)
            finally:
                os.close(descriptor); descriptor=None
            os.replace(temporary,path); temporary=None
            directory=os.open(path.parent,os.O_RDONLY|getattr(os,"O_DIRECTORY",0))
            try:os.fsync(directory)
            finally:os.close(directory)
        except Exception:pass
        finally:
            if descriptor is not None:
                try:os.close(descriptor)
                except OSError:pass
            if temporary is not None:
                try:os.unlink(temporary)
                except OSError:pass
    def _read_journal(self):
        try:
            loaded=json.loads((Path(self.config.private_root)/"status.json").read_text(encoding="utf-8"))
            success=loaded.get("last_success"); errors=loaded.get("last_errors")
            return {"last_success":success if isinstance(success,(int,float)) else None,"last_errors":tuple(str(item) for item in errors) if isinstance(errors,list) else ()}
        except Exception:return {"last_success":None,"last_errors":()}
    def _lock(self):return self._lock_factory(Path(self.config.private_root)/"operation.lock")

def _with_release(s,i,r):
    users=dict(s.users); users[i]=replace(users[i],current_release=r); return RuntimeState(s.schema_version,s.owner_client_id,users)
def _routable(s):return RuntimeState(s.schema_version,s.owner_client_id,{i:replace(u,active=False) if u.active and not u.current_release else u for i,u in s.users.items()})
def _traffic_matches_state(clients,state):
    clients_by_id={client.client_id:client for client in clients}
    return all(user and user.email==client.email and user.active==client.enabled for client in clients for user in (state.users.get(client.client_id),)) and all(user.client_id in clients_by_id for user in state.users.values() if user.active)
def _shape(b,owner):
    if tuple(b)!=(OWNER_VARIANTS if owner else MEMBER_VARIANTS):raise ValueError
def _digest(v):return hashlib.sha256(json.dumps(v,sort_keys=True,default=str).encode()).hexdigest()
def _provider_url(c,t):return "https://%s/s/%s/AmyTelecom-Provider.yaml"%(c.subscription_authority,t)
def _find(cs,i):return next((c for c in cs if c.client_id==i),None)
def _client_id(i):
    if isinstance(i,bool) or not isinstance(i,int) or i<1:raise ServiceError("invalid_client")
    return i
def _urls(c,t,o):return ["https://%s/s/%s/%s"%(c.subscription_authority,t,PROFILE_FILENAMES[v]) for v in (OWNER_VARIANTS if o else MEMBER_VARIANTS)]
def _result(c,r):return {"client_id":c.client_id,"email":c.email,"release_id":r.release_id,"variants":tuple(r.public_paths)}
def _error(c,code):return {"client_id":c.client_id,"code":code}
def _pending_source(user,client):return bool(user and client.enabled and user.active and not user.current_release)
def _state_path(c):return Path(c.private_root)/"state.json"
