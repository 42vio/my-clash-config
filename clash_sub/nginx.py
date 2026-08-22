import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS, RuntimeState, ServiceConfig, XuiClient
from clash_sub.state import TOKEN_RE, _state_to_payload


_RELEASE_ID_RE = re.compile(r"^[0-9TZ-]+-[a-f0-9]{8}$")
_ROUTE_MODE = 0o640
_PRIVATE_MODE = 0o600
_UNSAFE_PATH_CHARACTERS = frozenset(" ;{}'\\\"#$" + "".join(chr(code) for code in range(0x20)) + chr(0x7F))
_TITLES = {
    "balanced": ("Clash Balanced", "Clash-Balanced.yaml"),
    "standard": ("Clash Standard", "Clash-Standard.yaml"),
    "privacy": ("Clash Privacy", "Clash-Privacy.yaml"),
}


class NginxError(RuntimeError):
    pass


def render_routes(config, state, clients):
    checked_config, _, public_root, _ = _validate_config(config)
    _validate_state(state)
    clients_by_id = _validated_clients(clients)
    blocks = []
    for client_id in sorted(state.users):
        user = state.users[client_id]
        client = clients_by_id.get(client_id)
        if client is None or not user.active or not client.enabled:
            continue
        release_id = _release_id(user.current_release)
        variants = OWNER_VARIANTS if client_id == state.owner_client_id else MEMBER_VARIANTS
        traffic = _userinfo(client)
        for variant in variants:
            alias = _release_path(public_root, client_id, release_id, variant)
            blocks.append(_route_block(user.token, variant, alias, traffic))
    return "\n".join(blocks) + ("\n" if blocks else "")


def activate_runtime(config, state, routes, runner, extra_replacements=()):
    checked_config, private_root, _, routes_path = _validate_config(config)
    _validate_state(state)
    if not isinstance(routes, str) or "\x00" in routes:
        raise NginxError("invalid routes")
    if not callable(runner):
        raise NginxError("invalid command runner")

    state_path = private_root / "state.json"
    artifacts = [
        (state_path, _state_bytes(state), _PRIVATE_MODE),
        (routes_path, routes.encode("utf-8"), _ROUTE_MODE),
    ]
    artifacts.extend(_extra_artifacts(extra_replacements, private_root))
    _validate_artifacts(artifacts)
    snapshots = [(path, _snapshot(path)) for path, _, _ in artifacts]
    candidates = []
    changed = False
    try:
        for path, contents, mode in artifacts:
            candidates.append((path, _write_candidate(path, contents, mode)))
        for path, candidate in candidates:
            os.replace(candidate, path)
            changed = True
            _fsync_directory(path.parent)

        if not _command_ok(runner, (str(checked_config.nginx_binary), "-t")):
            if not _restore(snapshots):
                raise NginxError("Nginx activation rollback failed")
            raise NginxError("Nginx validation failed")

        if not _command_ok(runner, (str(checked_config.systemctl_binary), "reload", "nginx")):
            if not _restore(snapshots):
                raise NginxError("Nginx activation rollback failed")
            if not _command_ok(runner, (str(checked_config.nginx_binary), "-t")):
                raise NginxError("Nginx activation rollback failed")
            if not _command_ok(runner, (str(checked_config.systemctl_binary), "reload", "nginx")):
                raise NginxError("Nginx activation rollback failed")
            raise NginxError("Nginx reload failed")
    except NginxError:
        raise
    except Exception:
        if changed and not _restore(snapshots):
            raise NginxError("Nginx activation rollback failed") from None
        raise NginxError("Nginx activation failed") from None
    finally:
        for _, candidate in candidates:
            _remove_candidate(candidate)


def _validate_config(config):
    if not isinstance(config, ServiceConfig):
        raise NginxError("invalid service configuration")
    try:
        private_root = _directory(config.private_root, private=True)
        public_root = _directory(config.public_root, private=False)
        routes = _target(config.nginx_routes)
        _directory(routes.parent, private=False)
        for binary in (config.nginx_binary, config.systemctl_binary):
            _safe_path(binary, require_exists=False)
    except NginxError:
        raise NginxError("invalid service configuration")
    return config, private_root, public_root, routes


def _validate_state(state):
    if not isinstance(state, RuntimeState):
        raise NginxError("invalid runtime state")
    try:
        _state_to_payload(state)
    except Exception:
        raise NginxError("invalid runtime state") from None


def _validated_clients(clients):
    if isinstance(clients, (str, bytes)):
        raise NginxError("invalid clients")
    try:
        values = tuple(clients)
    except TypeError:
        raise NginxError("invalid clients") from None
    result = {}
    for client in values:
        if not isinstance(client, XuiClient) or client.client_id in result:
            raise NginxError("invalid clients")
        if (
            isinstance(client.client_id, bool)
            or not isinstance(client.client_id, int)
            or client.client_id < 1
            or not isinstance(client.enabled, bool)
        ):
            raise NginxError("invalid clients")
        for value in (client.upload, client.download, client.total, client.expiry_ms):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise NginxError("invalid client traffic")
        if client.expiry_ms % 1000:
            raise NginxError("invalid client traffic")
        result[client.client_id] = client
    return result


def _release_id(value):
    if not isinstance(value, str) or not _RELEASE_ID_RE.fullmatch(value):
        raise NginxError("invalid current release")
    return value


def _release_path(public_root, client_id, release_id, variant):
    if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id < 1:
        raise NginxError("invalid release path")
    if variant not in _TITLES:
        raise NginxError("invalid release path")
    root = Path(public_root)
    release_root = root / "releases"
    client_root = release_root / str(client_id)
    release_root_path = client_root / release_id
    path = release_root_path / ("clash-%s.yaml" % variant)
    if any(path_part.is_symlink() or not path_part.is_dir() for path_part in (root, release_root, client_root, release_root_path)):
        raise NginxError("invalid release path")
    if path.is_symlink() or not path.is_file() or _mode(path) != _ROUTE_MODE:
        raise NginxError("invalid release path")
    return path


def _userinfo(client):
    return "upload=%s; download=%s; total=%s; expire=%s" % (
        client.upload,
        client.download,
        client.total,
        client.expiry_ms // 1000,
    )


def _route_block(token, variant, alias, userinfo):
    if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise NginxError("invalid subscription token")
    title, filename = _TITLES[variant]
    return "\n".join(
        (
            "location = /s/%s/clash-%s.yaml {" % (token, variant),
            '    if ($request_method !~ ^(GET|HEAD)$) { return 404; }',
            '    if ($args != "") { return 404; }',
            "    limit_req zone=clash_subscription burst=5 nodelay;",
            "    client_max_body_size 1k;",
            "    access_log off;",
            "    log_not_found off;",
            '    default_type "text/yaml; charset=utf-8";',
            "    alias %s;" % alias,
            '    add_header Profile-Title "%s";' % title,
            "    add_header Content-Disposition 'attachment; filename=\"%s\"';" % filename,
            '    add_header Subscription-Userinfo "%s";' % userinfo,
            "    add_header X-Content-Type-Options nosniff always;",
            "    add_header Cache-Control no-store always;",
            "}",
        )
    )


def _state_bytes(state):
    return (json.dumps(_state_to_payload(state), sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _extra_artifacts(replacements, private_root):
    if isinstance(replacements, (str, bytes)):
        raise NginxError("invalid extra replacements")
    try:
        entries = tuple(replacements)
    except TypeError:
        raise NginxError("invalid extra replacements") from None
    artifacts = []
    for entry in entries:
        if not isinstance(entry, tuple) or len(entry) not in (2, 3):
            raise NginxError("invalid extra replacements")
        path, contents = entry[:2]
        mode = entry[2] if len(entry) == 3 else _PRIVATE_MODE
        try:
            path = _target(path)
        except NginxError:
            raise NginxError("invalid extra replacements") from None
        try:
            canonical_path = path.resolve(strict=False)
            canonical_path.relative_to(private_root)
        except (OSError, ValueError):
            raise NginxError("invalid extra replacements") from None
        if not isinstance(contents, bytes) or not contents or isinstance(mode, bool) or mode != _PRIVATE_MODE:
            raise NginxError("invalid extra replacements")
        artifacts.append((canonical_path, contents, mode))
    return artifacts


def _validate_artifacts(artifacts):
    paths = set()
    for path, contents, mode in artifacts:
        path = _target(path)
        _directory(path.parent, private=False)
        if path in paths or not isinstance(contents, bytes) or not contents:
            raise NginxError("invalid activation artifacts")
        if mode not in (_PRIVATE_MODE, _ROUTE_MODE):
            raise NginxError("invalid activation artifacts")
        paths.add(path)


def _directory(value, private):
    path = _safe_path(value, require_exists=True)
    if not path.is_dir():
        raise NginxError("invalid service path")
    if private and _mode(path) != 0o700:
        raise NginxError("invalid service path")
    return path


def _target(value):
    path = _safe_path(value, require_exists=False)
    if path.exists() and not path.is_file():
        raise NginxError("invalid service path")
    return path


def _safe_path(value, *, require_exists):
    try:
        raw = os.fspath(value)
    except TypeError:
        raise NginxError("invalid service path") from None
    if not isinstance(raw, str) or not raw or any(character in _UNSAFE_PATH_CHARACTERS for character in raw):
        raise NginxError("invalid service path")
    path = Path(raw)
    if not path.is_absolute() or any(part == ".." for part in path.parts):
        raise NginxError("invalid service path")
    ancestor = Path(path.anchor)
    for part in path.parts[1:]:
        ancestor = ancestor / part
        if ancestor.is_symlink():
            raise NginxError("invalid service path")
    try:
        return path.resolve(strict=require_exists)
    except (OSError, ValueError):
        raise NginxError("invalid service path") from None


def _snapshot(path):
    if not path.exists():
        return False, b"", 0
    return True, path.read_bytes(), _mode(path)


def _write_candidate(path, contents, mode):
    descriptor = None
    candidate = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
        candidate = Path(name)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(contents)
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
        return candidate
    except Exception:
        if descriptor is not None:
            os.close(descriptor)
        if candidate is not None:
            _remove_candidate(candidate)
        raise


def _restore(snapshots):
    candidates = []
    try:
        for path, (exists, contents, mode) in snapshots:
            if exists:
                candidates.append((path, _write_candidate(path, contents, mode)))
        for path, candidate in candidates:
            os.replace(candidate, path)
            _fsync_directory(path.parent)
        for path, (exists, _, _) in snapshots:
            if not exists and path.exists():
                path.unlink()
                _fsync_directory(path.parent)
        return True
    except Exception:
        return False
    finally:
        for _, candidate in candidates:
            _remove_candidate(candidate)


def _command_ok(runner, arguments):
    try:
        result = runner(
            list(arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        return result.returncode == 0
    except (AttributeError, OSError, subprocess.TimeoutExpired):
        return False


def _fsync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _mode(path):
    return path.stat().st_mode & 0o777


def _remove_candidate(path):
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
