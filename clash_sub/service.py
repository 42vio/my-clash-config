"""Fail-closed orchestration for the on-demand subscription service."""

import hashlib
import json
from dataclasses import replace
from pathlib import Path

from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS, RuntimeState


class ServiceError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ClashSubService:
    def __init__(
        self, config, *, read_snapshot, load_state, reconcile_state, rotate_user_token,
        fetch_xui_proxies, download_airport_proxies, load_proxy_snapshot,
        render_user_bundle, validate_clash, mihomo_validator, release_store,
        render_routes, activate_runtime, runner, snapshot_encoder=None, state_sink=None,
    ):
        self.config = config
        self._read_snapshot = read_snapshot
        self._load_state = load_state
        self._reconcile_state = reconcile_state
        self._rotate_user_token = rotate_user_token
        self._fetch_xui_proxies = fetch_xui_proxies
        self._download_airport_proxies = download_airport_proxies
        self._load_proxy_snapshot = load_proxy_snapshot
        self._render_user_bundle = render_user_bundle
        self._validate_clash = validate_clash
        self._mihomo = mihomo_validator
        self._releases = release_store
        self._render_routes = render_routes
        self._activate_runtime = activate_runtime
        self._runner = runner
        self._snapshot_encoder = snapshot_encoder or _snapshot_bytes
        self._state_sink = state_sink or (lambda state: None)
        self._input_cache = {}

    def sync_all(self):
        snapshot, state = self._snapshot_and_state("xui_snapshot_failed")
        airport, home = self._private_sources()
        next_state = state
        prepared = []
        updates = []
        errors = []
        for client in snapshot.clients:
            user = state.users.get(client.client_id)
            if user is None or not client.enabled:
                continue
            owner = client.client_id == state.owner_client_id
            try:
                xui = self._fetch_xui_proxies(snapshot.source_url(client), self.config.max_source_bytes)
                fingerprint = _fingerprint(xui, airport if owner else (), home if owner else ())
                if self._input_cache.get(client.client_id) == fingerprint and user.current_release:
                    continue
                bundle = self._render_user_bundle(owner, xui, airport if owner else (), home if owner else (), self.config.template_root)
                _check_variant_shape(bundle, owner)
                for text in bundle.values():
                    self._validate_clash(text, ())
                release = self._releases.prepare(client.client_id, bundle, {"inputs": fingerprint})
                if release is None:
                    self._input_cache[client.client_id] = fingerprint
                    continue
                for path in release.public_paths.values():
                    self._mihomo.validate(path)
                next_state = _with_release(next_state, client.client_id, release.release_id)
                prepared.append((client.client_id, release))
                self._input_cache[client.client_id] = fingerprint
                updates.append(_result(client, release))
            except Exception:
                errors.append({"client_id": client.client_id, "email": client.email, "code": "owner_update_failed" if owner else "member_update_failed"})
        self._activate(snapshot.clients, next_state, "sync_activation_failed")
        self._commit(next_state, prepared)
        return {"updated": tuple(updates), "errors": tuple(errors)}

    def update_airport(self, url):
        snapshot, state = self._snapshot_and_state("xui_snapshot_failed")
        owner = _client(snapshot.clients, state.owner_client_id)
        try:
            candidate = self._download_airport_proxies(url, self.config.max_source_bytes)
            home = self._load_proxy_snapshot(_home_path(self.config))
            xui = self._fetch_xui_proxies(snapshot.source_url(owner), self.config.max_source_bytes)
            bundle = self._render_user_bundle(True, xui, candidate, home, self.config.template_root)
            _check_variant_shape(bundle, True)
            for text in bundle.values():
                self._validate_clash(text, ())
            fingerprint = _fingerprint(xui, candidate, home)
            release = self._releases.prepare(owner.client_id, bundle, {"inputs": fingerprint})
            next_state = state if release is None else _with_release(state, owner.client_id, release.release_id)
            if release is not None:
                for path in release.public_paths.values():
                    self._mihomo.validate(path)
            routes = self._render_routes(self.config, _routable_state(next_state), snapshot.clients)
            self._activate_runtime(self.config, next_state, routes, self._runner, (( _airport_path(self.config), self._snapshot_encoder(candidate), 0o600),))
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("airport_activation_failed") from None
        self._input_cache[owner.client_id] = fingerprint
        self._commit(next_state, [] if release is None else [(owner.client_id, release)])
        return {"updated": (_result(owner, release),) if release else (), "errors": ()}

    def traffic_update(self):
        snapshot, state = self._snapshot_and_state("traffic_snapshot_failed")
        try:
            self._activate(snapshot.clients, state, "traffic_activation_failed")
        except ServiceError:
            raise
        return {"updated": (), "errors": ()}

    def links(self):
        snapshot, state = self._snapshot_and_state("xui_snapshot_failed")
        clients = {item.client_id: item for item in snapshot.clients}
        return tuple(
            {"client_id": user.client_id, "email": user.email, "readable_code": user.readable_code, "urls": tuple(_urls(self.config, user.token, user.client_id == state.owner_client_id))}
            for user in sorted(state.users.values(), key=lambda item: item.client_id)
            if user.active and user.client_id in clients and clients[user.client_id].enabled
        )

    def status(self):
        _, state = self._snapshot_and_state("xui_snapshot_failed")
        return {"owner_client_id": state.owner_client_id, "users": tuple({"client_id": user.client_id, "email": user.email, "active": user.active, "current_release": user.current_release} for user in state.users.values())}

    def history(self, user):
        return tuple({"release_id": item.release_id, "variants": tuple(item.public_paths)} for item in self._releases.history(_client_id(user)))

    def rollback(self, user, release):
        client_id = _client_id(user)
        snapshot, state = self._snapshot_and_state("xui_snapshot_failed")
        try:
            verified = self._releases.verify_release(client_id, release)
            _check_variant_shape(verified.public_paths, client_id == state.owner_client_id)
            next_state = _with_release(state, client_id, verified.release_id)
            self._activate(snapshot.clients, next_state, "rollback_activation_failed")
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("rollback_release_invalid") from None
        self._commit(next_state, [(client_id, verified)])
        return _result(_client(snapshot.clients, client_id), verified)

    def rotate_link(self, user):
        client_id = _client_id(user)
        snapshot, state = self._snapshot_and_state("xui_snapshot_failed")
        try:
            next_state = self._rotate_user_token(state, client_id)
            self._activate(snapshot.clients, next_state, "rotation_activation_failed")
        except ServiceError:
            raise
        except Exception:
            raise ServiceError("rotation_activation_failed") from None
        self._state_sink(next_state)
        rotated = next_state.users[client_id]
        return {"client_id": client_id, "token": rotated.token, "urls": tuple(_urls(self.config, rotated.token, client_id == next_state.owner_client_id))}

    def _snapshot_and_state(self, code):
        try:
            snapshot = self._read_snapshot(self.config.xui_database)
            state = self._reconcile_state(self._load_state(_state_path(self.config)), snapshot.clients, self.config.owner_email)
            return snapshot, state
        except Exception:
            raise ServiceError(code) from None

    def _private_sources(self):
        try:
            return self._load_proxy_snapshot(_airport_path(self.config)), self._load_proxy_snapshot(_home_path(self.config))
        except Exception:
            return (), ()

    def _activate(self, clients, state, code):
        try:
            routes = self._render_routes(self.config, _routable_state(state), clients)
            self._activate_runtime(self.config, state, routes, self._runner)
        except Exception:
            raise ServiceError(code) from None

    def _commit(self, state, prepared):
        self._state_sink(state)
        for client_id, release in prepared:
            self._releases.mark_current(client_id, release.release_id)
            self._releases.prune(client_id)


def _with_release(state, client_id, release_id):
    users = dict(state.users)
    users[client_id] = replace(users[client_id], current_release=release_id)
    return RuntimeState(state.schema_version, state.owner_client_id, users)


def _routable_state(state):
    users = {
        client_id: replace(user, active=False) if user.active and user.current_release is None else user
        for client_id, user in state.users.items()
    }
    return RuntimeState(state.schema_version, state.owner_client_id, users)


def _fingerprint(*sources):
    return hashlib.sha256(json.dumps(sources, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _check_variant_shape(bundle, owner):
    expected = OWNER_VARIANTS if owner else MEMBER_VARIANTS
    if tuple(bundle) != expected:
        raise ValueError("invalid bundle")


def _client(clients, client_id):
    for client in clients:
        if client.client_id == client_id:
            return client
    raise ValueError("missing client")


def _client_id(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ServiceError("invalid_client")
    return value


def _urls(config, token, owner):
    variants = OWNER_VARIANTS if owner else MEMBER_VARIANTS
    return ["https://%s/s/%s/clash-%s.yaml" % (config.subscription_authority, token, variant) for variant in variants]


def _result(client, release):
    return {
        "client_id": client.client_id,
        "email": client.email,
        "release_id": release.release_id,
        "variants": tuple(release.public_paths),
    }


def _state_path(config):
    return Path(config.private_root) / "state.json"


def _airport_path(config):
    return Path(config.private_root) / "airport.yaml"


def _home_path(config):
    return Path(config.private_root) / "home.yaml"


def _snapshot_bytes(proxies):
    return json.dumps({"proxies": proxies}, sort_keys=True).encode()
