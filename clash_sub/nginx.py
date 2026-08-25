import base64
import grp
import json
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from clash_sub.domain import MEMBER_VARIANTS, OWNER_VARIANTS, RuntimeState, ServiceConfig, XuiClient
from clash_sub.state import TOKEN_RE, _state_to_payload


_RELEASE_ID_RE = re.compile(r"^[0-9TZ-]+-[a-f0-9]{8}$")
_DOMAIN_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$")
_ROUTE_MODE = 0o640
_PRIVATE_MODE = 0o600
_ACTIVATION_JOURNAL = ".activation-journal.json"
_JOURNAL_SCHEMA = 1
_UNSAFE_PATH_CHARACTERS = frozenset(" ;{}'\\\"#$" + "".join(chr(code) for code in range(0x20)) + chr(0x7F))
_TITLES = {
    "balanced": ("Clash Balanced", "Clash-Balanced.yaml"),
    "standard": ("Clash Standard", "Clash-Standard.yaml"),
    "privacy": ("Clash Privacy", "Clash-Privacy.yaml"),
}


class NginxError(RuntimeError):
    pass


def _nginx_template_environment(config):
    directory = Path(config.template_root) / "nginx"
    if not directory.is_dir():
        raise NginxError("invalid service configuration")
    return Environment(
        loader=FileSystemLoader(str(directory)),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )


def render_stream_config(config, domain):
    """Render the 443 stream SNI routing configuration."""
    if not isinstance(domain, str) or not _DOMAIN_RE.fullmatch(domain):
        raise NginxError("invalid domain")
    try:
        return _nginx_template_environment(config).get_template(
            "stream.conf.j2"
        ).render(domain=domain)
    except NginxError:
        raise
    except Exception:
        raise NginxError("stream rendering failed") from None


def render_sub_server(config, *, domain, panel_port, panel_base_path, routes_include, fullchain, privkey):
    """Render the loopback TLS server for subscriptions and the panel."""
    if (
        not isinstance(domain, str)
        or not _DOMAIN_RE.fullmatch(domain)
        or isinstance(panel_port, bool)
        or not isinstance(panel_port, int)
        or not 1 <= panel_port <= 65535
        or panel_port in (443, 10443, 20443, 30443)
        or not isinstance(panel_base_path, str)
        or not re.fullmatch(r"/[A-Za-z0-9_-]+", panel_base_path)
        or not isinstance(routes_include, str)
        or not routes_include.startswith("/")
        or any(c in _UNSAFE_PATH_CHARACTERS for c in routes_include)
        or not isinstance(fullchain, str)
        or not fullchain.startswith("/")
        or any(c in _UNSAFE_PATH_CHARACTERS for c in fullchain)
        or not isinstance(privkey, str)
        or not privkey.startswith("/")
        or any(c in _UNSAFE_PATH_CHARACTERS for c in privkey)
    ):
        raise NginxError("invalid sub server parameters")
    try:
        return _nginx_template_environment(config).get_template(
            "sub-server.conf.j2"
        ).render(
            domain=domain,
            panel_port=panel_port,
            panel_base_path=panel_base_path,
            routes_include=routes_include,
            fullchain=fullchain,
            privkey=privkey,
        )
    except NginxError:
        raise
    except Exception:
        raise NginxError("sub server rendering failed") from None


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

    # A prior process may have been terminated between replacements.  Recover
    # before reading or changing any live runtime artifact.
    recover_runtime(checked_config, runner, reload=True)

    state_path = private_root / "state.json"
    artifacts = [
        (state_path, _state_bytes(state), _PRIVATE_MODE),
        (routes_path, routes.encode("utf-8"), _ROUTE_MODE),
    ]
    artifacts.extend(_extra_artifacts(extra_replacements, private_root))
    _validate_artifacts(artifacts)
    snapshots = [(path, _snapshot(path)) for path, _, _ in artifacts]
    journal_path = private_root / _ACTIVATION_JOURNAL
    candidates = []
    changed = False
    journal_written = False
    try:
        for path, contents, mode in artifacts:
            candidates.append((path, _write_candidate(path, contents, mode)))
        _write_activation_journal(journal_path, checked_config, snapshots, "prepared")
        journal_written = True
        for path, candidate in candidates:
            os.replace(candidate, path)
            changed = True
            _require_activation_target(path, _artifact_mode(path, artifacts), private_root)
            _fsync_directory(path.parent)

        if not _command_ok(runner, (str(checked_config.nginx_binary), "-t")):
            if not _recover_after_failed_activation(checked_config, runner):
                raise NginxError("Nginx activation rollback failed")
            raise NginxError("Nginx validation failed")

        if not _command_ok(runner, (str(checked_config.systemctl_binary), "reload", "nginx")):
            if not _recover_after_failed_activation(checked_config, runner):
                raise NginxError("Nginx activation rollback failed")
            raise NginxError("Nginx reload failed")

        _commit_activation_journal(checked_config, runner, journal_path, snapshots)
        try:
            _remove_activation_journal(journal_path)
        except NginxError:
            # The new runtime is already durably committed.  Retain the
            # committed journal for the next recovery instead of reporting a
            # failed activation that might cause its release to be discarded.
            pass
    except NginxError:
        raise
    except Exception:
        if changed and journal_written and not _recover_after_failed_activation(checked_config, runner):
            raise NginxError("Nginx activation rollback failed") from None
        if not changed and journal_written:
            _remove_activation_journal(journal_path)
        raise NginxError("Nginx activation failed") from None
    finally:
        for _, candidate in candidates:
            _remove_candidate(candidate)


def recover_runtime(config, runner, *, reload=False):
    """Recover an interrupted activation without exposing runtime contents.

    ``reload=False`` is deliberately suitable for the boot-time oneshot: it
    validates restored files but does not require an already running Nginx.
    """
    checked_config, private_root, _, _ = _validate_config(config)
    if not callable(runner) or not isinstance(reload, bool):
        raise NginxError("invalid command runner")
    journal_path = private_root / _ACTIVATION_JOURNAL
    if not journal_path.exists() and not journal_path.is_symlink():
        return False
    try:
        phase, snapshots = _load_activation_journal(journal_path, checked_config)
        if phase == "prepared":
            if not _restore(snapshots):
                raise NginxError("Nginx recovery failed")
            if not _command_ok(runner, (str(checked_config.nginx_binary), "-t")):
                raise NginxError("Nginx recovery failed")
            if reload and not _command_ok(
                runner, (str(checked_config.systemctl_binary), "reload", "nginx")
            ):
                raise NginxError("Nginx recovery failed")
        _remove_activation_journal(journal_path)
        return True
    except NginxError:
        raise
    except Exception:
        raise NginxError("Nginx recovery failed") from None


def _recover_after_failed_activation(config, runner):
    try:
        recover_runtime(config, runner, reload=True)
        return True
    except NginxError:
        return False


def _write_activation_journal(journal_path, config, snapshots, phase):
    payload = _activation_journal_payload(config, snapshots, phase)
    candidate = None
    try:
        candidate = _write_candidate(journal_path, payload, _PRIVATE_MODE)
        os.replace(candidate, journal_path)
        candidate = None
        _require_private_regular_file(journal_path, _PRIVATE_MODE)
        _fsync_directory(journal_path.parent)
    except Exception:
        raise NginxError("Nginx activation journal failed") from None
    finally:
        if candidate is not None:
            _remove_candidate(candidate)


def _commit_activation_journal(config, runner, journal_path, snapshots):
    try:
        _write_activation_journal(journal_path, config, snapshots, "committed")
        return
    except NginxError:
        try:
            phase, _ = _load_activation_journal(journal_path, config)
        except NginxError:
            phase = None
        if phase == "committed":
            try:
                _fsync_directory(journal_path.parent)
            except OSError:
                raise NginxError("Nginx activation journal failed") from None
            return
        if phase == "prepared" and _recover_after_failed_activation(config, runner):
            raise NginxError("Nginx activation failed")
        raise NginxError("Nginx activation rollback failed")


def _activation_journal_payload(config, snapshots, phase):
    if phase not in {"prepared", "committed"}:
        raise NginxError("invalid activation journal")
    entries = []
    for path, (exists, contents, mode) in snapshots:
        _journal_target(Path(path), config)
        if not isinstance(exists, bool) or not isinstance(contents, bytes):
            raise NginxError("invalid activation journal")
        if not exists:
            contents, mode = b"", 0
        if isinstance(mode, bool) or not isinstance(mode, int) or mode < 0 or mode > 0o777:
            raise NginxError("invalid activation journal")
        entries.append(
            {
                "contents": base64.b64encode(contents).decode("ascii"),
                "exists": exists,
                "mode": mode,
                "path": str(path),
            }
        )
    return (
        json.dumps(
            {"phase": phase, "schema_version": _JOURNAL_SCHEMA, "targets": entries},
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("ascii")


def _load_activation_journal(journal_path, config):
    _require_private_regular_file(journal_path, _PRIVATE_MODE)
    try:
        payload = json.loads(journal_path.read_text(encoding="ascii"))
    except (OSError, UnicodeError, ValueError):
        raise NginxError("Nginx recovery failed") from None
    if not isinstance(payload, dict) or set(payload) != {"phase", "schema_version", "targets"}:
        raise NginxError("Nginx recovery failed")
    if payload["schema_version"] != _JOURNAL_SCHEMA or payload["phase"] not in {"prepared", "committed"}:
        raise NginxError("Nginx recovery failed")
    targets = payload["targets"]
    if not isinstance(targets, list) or not targets:
        raise NginxError("Nginx recovery failed")
    snapshots = []
    seen = set()
    for entry in targets:
        if not isinstance(entry, dict) or set(entry) != {"contents", "exists", "mode", "path"}:
            raise NginxError("Nginx recovery failed")
        try:
            path = _journal_target(Path(entry["path"]), config)
            contents = base64.b64decode(entry["contents"], validate=True)
        except (NginxError, TypeError, ValueError, UnicodeError):
            raise NginxError("Nginx recovery failed") from None
        exists = entry["exists"]
        mode = entry["mode"]
        if (
            path in seen
            or not isinstance(exists, bool)
            or isinstance(mode, bool)
            or not isinstance(mode, int)
            or mode < 0
            or mode > 0o777
            or not isinstance(entry["contents"], str)
            or (not exists and (contents or mode))
        ):
            raise NginxError("Nginx recovery failed")
        seen.add(path)
        snapshots.append((path, (exists, contents, mode)))
    state_path = Path(config.private_root).resolve() / "state.json"
    routes_path = Path(config.nginx_routes).resolve()
    if state_path not in seen or routes_path not in seen:
        raise NginxError("Nginx recovery failed")
    return payload["phase"], tuple(snapshots)


def _journal_target(path, config):
    target = _target(path)
    state_path = Path(config.private_root).resolve() / "state.json"
    routes_path = Path(config.nginx_routes).resolve()
    if target in {state_path, routes_path}:
        return target
    try:
        target.relative_to(Path(config.private_root).resolve())
    except ValueError:
        raise NginxError("invalid activation journal") from None
    return target


def _remove_activation_journal(journal_path):
    try:
        if journal_path.exists() or journal_path.is_symlink():
            _require_private_regular_file(journal_path, _PRIVATE_MODE)
        if journal_path.is_symlink() or (journal_path.exists() and not journal_path.is_file()):
            raise OSError
        journal_path.unlink(missing_ok=True)
        _fsync_directory(journal_path.parent)
    except OSError:
        raise NginxError("Nginx activation journal failed") from None


def _validate_config(config):
    if not isinstance(config, ServiceConfig):
        raise NginxError("invalid service configuration")
    try:
        private_root = _directory(config.private_root, private=True)
        public_root = _directory(config.public_root, private=False, public_release=True)
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
    public_gid = _public_gid(root)
    for path_part in (root, release_root, client_root, release_root_path):
        _require_public_release_directory(path_part, public_gid)
    if (
        path.is_symlink()
        or not path.is_file()
        or _mode(path) != _ROUTE_MODE
        or path.stat().st_uid != _expected_uid()
        or path.stat().st_gid != public_gid
        or path.stat().st_nlink != 1
    ):
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


def _artifact_mode(path, artifacts):
    for candidate_path, _, mode in artifacts:
        if candidate_path == path:
            return mode
    raise NginxError("invalid activation artifacts")


def _require_activation_target(path, mode, private_root):
    try:
        details = Path(path).stat()
        Path(path).relative_to(private_root)
        private = True
    except ValueError:
        private = False
        try:
            details = Path(path).stat()
        except OSError:
            raise NginxError("invalid activation artifacts") from None
    except OSError:
        raise NginxError("invalid activation artifacts") from None
    if (
        Path(path).is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or _mode(path) != mode
        or details.st_uid != _expected_uid()
        or details.st_nlink != 1
        or (private and mode != _PRIVATE_MODE)
        or (not private and mode != _ROUTE_MODE)
    ):
        raise NginxError("invalid activation artifacts")


def _directory(value, private, public_release=False):
    path = _safe_path(value, require_exists=True)
    if not path.is_dir():
        raise NginxError("invalid service path")
    details = path.stat()
    if details.st_uid != _expected_uid():
        raise NginxError("invalid service path")
    if private:
        if _mode(path) != 0o700:
            raise NginxError("invalid service path")
    elif public_release:
        _require_public_release_directory(path, _public_gid(path))
    elif _mode(path) & 0o022:
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


def _expected_uid():
    return 0 if os.geteuid() == 0 else os.geteuid()


def _public_gid(public_root):
    if os.geteuid() != 0:
        try:
            return Path(public_root).stat().st_gid
        except OSError:
            raise NginxError("invalid service path") from None
    try:
        return grp.getgrnam("www-data").gr_gid
    except KeyError:
        raise NginxError("invalid service path") from None


def _require_public_release_directory(path, public_gid):
    try:
        details = Path(path).stat()
    except OSError:
        raise NginxError("invalid release path") from None
    if (
        Path(path).is_symlink()
        or not stat.S_ISDIR(details.st_mode)
        or (details.st_mode & 0o7777) != 0o2750
        or details.st_uid != _expected_uid()
        or details.st_gid != public_gid
    ):
        raise NginxError("invalid release path")


def _snapshot(path):
    if not path.exists():
        return False, b"", 0
    details = path.stat()
    if (
        path.is_symlink()
        or not path.is_file()
        or details.st_uid != _expected_uid()
        or details.st_nlink != 1
    ):
        raise NginxError("invalid activation artifacts")
    return True, path.read_bytes(), _mode(path)


def _require_private_regular_file(path, mode):
    try:
        details = path.stat()
    except OSError:
        raise NginxError("Nginx recovery failed") from None
    if (
        path.is_symlink()
        or not stat.S_ISREG(details.st_mode)
        or _mode(path) != mode
        or details.st_nlink != 1
        or details.st_uid != _expected_uid()
    ):
        raise NginxError("Nginx recovery failed")


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
