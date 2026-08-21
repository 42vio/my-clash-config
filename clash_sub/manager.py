import argparse
import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import yaml

from clash_sub.converter import SourceError, SubconverterClient, load_local_proxies, normalize_reality_proxy
from clash_sub.models import Settings
from clash_sub.releases import (
    BuildError,
    MANIFEST_NAME,
    ReleaseBuilder,
    _hash_proxies,
    _hash_template_tree,
    list_history as release_list_history,
    publish_candidate as release_publish_candidate,
    rollback as release_rollback,
)
from clash_sub.settings import SettingsError, load_settings, rotate_user_token
from clash_sub.traffic import TrafficClient, TrafficError
from clash_sub.validation import ValidationError


class ParserError(ValueError):
    """Raised when command-line arguments are invalid."""


class JsonArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        raise ParserError(message)


@dataclass
class ManagerRuntime:
    service_path: Path
    users_path: Path
    operation_log_path: Path
    template_dir: Path
    settings_loader: Callable[[Path, Path], Settings] = load_settings
    token_rotator: Callable[[Path, Settings, str], object] = rotate_user_token
    converter_factory: Callable[[str], object] = SubconverterClient
    traffic_client_factory: Callable[[], object] = TrafficClient
    local_loader: Callable[[Path], Sequence[Mapping[str, object]]] = load_local_proxies
    renderer: Callable[..., str] = None
    validator: Callable[..., Mapping[str, object]] = None
    publisher: Callable[..., object] = release_publish_candidate
    history_loader: Callable[[Path, str], Sequence[object]] = release_list_history
    rollback_loader: Callable[[Path, str, str], object] = release_rollback
    clock: Callable[[], datetime] = None


def _default_runtime() -> ManagerRuntime:
    repo_root = Path(__file__).resolve().parents[1]
    return ManagerRuntime(
        service_path=repo_root / "private" / "config" / "service.yaml",
        users_path=repo_root / "private" / "config" / "users.yaml",
        operation_log_path=repo_root / "private" / "logs" / "operations.jsonl",
        template_dir=repo_root / "templates",
    )


def main(argv=None, stdin=None, stdout=None, stderr=None, runtime=None) -> int:
    current_argv = list(sys.argv[1:] if argv is None else argv)
    in_stream = sys.stdin if stdin is None else stdin
    out_stream = sys.stdout if stdout is None else stdout
    err_stream = sys.stderr if stderr is None else stderr
    current_runtime = runtime or _default_runtime()
    if current_runtime.clock is None:
        current_runtime.clock = _utcnow
    if current_runtime.renderer is None or current_runtime.validator is None:
        from clash_sub.rendering import render_variant
        from clash_sub.validation import validate_config

        if current_runtime.renderer is None:
            current_runtime.renderer = render_variant
        if current_runtime.validator is None:
            current_runtime.validator = validate_config

    parser = _build_parser()
    try:
        args = parser.parse_args(current_argv)
        payload = _dispatch(args, in_stream, current_runtime)
        out_stream.write(_dump_json(payload))
        return 0
    except Exception as exc:
        payload = _error_payload(exc, current_argv)
        out_stream.write(_dump_json(payload))
        err_stream.write("")
        return 1


def _build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="python -m clash_sub.manager", add_help=False)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list-users", add_help=False)

    build = subparsers.add_parser("build", add_help=False)
    build.add_argument("--operation-id", required=True)
    build.add_argument("--user", required=True)

    publish = subparsers.add_parser("publish", add_help=False)
    publish.add_argument("--operation-id", required=True)
    publish.add_argument("--user", required=True)

    status = subparsers.add_parser("status", add_help=False)
    status.add_argument("user_id", nargs="?")

    history = subparsers.add_parser("history", add_help=False)
    history.add_argument("user_id")

    rollback = subparsers.add_parser("rollback", add_help=False)
    rollback.add_argument("user_id")
    rollback.add_argument("release_id")

    rotate = subparsers.add_parser("rotate-token", add_help=False)
    rotate.add_argument("user_id")

    subparsers.add_parser("import-airport", add_help=False)

    logs = subparsers.add_parser("logs", add_help=False)
    logs.add_argument("--limit", type=int, default=50)

    return parser


def _dispatch(args, stdin, runtime: ManagerRuntime):
    if args.command == "list-users":
        return _list_users(runtime)
    if args.command == "build":
        return _build_release(runtime, args.user, args.operation_id)
    if args.command == "publish":
        return _publish_release(runtime, args.user, args.operation_id)
    if args.command == "status":
        return _status(runtime, args.user_id)
    if args.command == "history":
        return _history(runtime, args.user_id)
    if args.command == "rollback":
        return _rollback_release(runtime, args.user_id, args.release_id)
    if args.command == "rotate-token":
        return _rotate_token(runtime, args.user_id)
    if args.command == "import-airport":
        return _import_airport(runtime, stdin)
    if args.command == "logs":
        return _logs(runtime, args.limit)
    raise ParserError("unknown command")


def _list_users(runtime: ManagerRuntime):
    settings = _load_settings(runtime)
    users = []
    for user_id in sorted(settings.users):
        user = settings.users[user_id]
        users.append(
            {
                "user_id": user.user_id,
                "role": user.role,
                "variants": list(user.variants),
            }
        )
    return {"users": users}


def _build_release(runtime: ManagerRuntime, user_id: str, operation_id: str):
    settings = _load_settings(runtime)
    builder = _make_builder(runtime, settings)
    try:
        candidate = builder.build_candidate(user_id, operation_id)
    except Exception as exc:
        _append_operation_log(
            runtime,
            operation="build",
            user_id=user_id,
            release_id=operation_id,
            status="error",
            error_code=_error_code(exc),
        )
        raise
    payload = {
        "user_id": candidate.user_id,
        "operation_id": candidate.operation_id,
        "variants": list(candidate.files),
        "candidate_path": str(candidate.path),
    }
    _append_operation_log(
        runtime,
        operation="build",
        user_id=user_id,
        release_id=operation_id,
        status="success",
    )
    return payload


def _publish_release(runtime: ManagerRuntime, user_id: str, operation_id: str):
    settings = _load_settings(runtime)
    user = settings.users.get(user_id)
    if user is None:
        raise BuildError("unknown user")
    candidate_path = settings.service.private_root / "staging" / operation_id / user_id
    candidate = _candidate_from_path(candidate_path, user_id, operation_id, user.variants)
    try:
        release = runtime.publisher(candidate, settings.service.private_root)
    except Exception as exc:
        _append_operation_log(
            runtime,
            operation="publish",
            user_id=user_id,
            release_id=operation_id,
            status="error",
            error_code=_error_code(exc),
        )
        raise
    payload = {
        "user_id": release.user_id,
        "release_id": release.release_id,
        "variants": list(release.files),
    }
    _append_operation_log(
        runtime,
        operation="publish",
        user_id=user_id,
        release_id=release.release_id,
        status="success",
    )
    return payload


def _status(runtime: ManagerRuntime, selected_user_id: Optional[str]):
    settings = _load_settings(runtime)
    if selected_user_id is None:
        user_ids = sorted(settings.users)
    else:
        user_ids = [selected_user_id]
    users_payload = {}
    for user_id in user_ids:
        user = settings.users.get(user_id)
        if user is None:
            raise BuildError("unknown user")
        try:
            release_info = _current_release_info(settings.service.private_root, user_id)
            input_hashes = _current_input_hashes(runtime, settings, user)
            needs_refresh = release_info is None or release_info["input_hashes"] != input_hashes
            traffic = _traffic_payload(runtime, user.xui_source.url)
            users_payload[user_id] = {
                "release_id": None if release_info is None else release_info["release_id"],
                "variants": list(user.variants if release_info is None else release_info["variants"]),
                "created_at": None if release_info is None else release_info["created_at"],
                "needs_refresh": needs_refresh,
                "traffic": traffic,
            }
        except Exception as exc:
            users_payload[user_id] = {
                "release_id": None,
                "variants": list(user.variants),
                "created_at": None,
                "needs_refresh": True,
                "traffic": None,
                "error_code": _error_code(exc),
            }
    return {"users": users_payload}


def _history(runtime: ManagerRuntime, user_id: str):
    settings = _load_settings(runtime)
    if user_id not in settings.users:
        raise BuildError("unknown user")
    releases = []
    for release in runtime.history_loader(settings.service.private_root, user_id):
        metadata = _read_manifest_metadata(release.path / MANIFEST_NAME)
        releases.append(
            {
                "release_id": release.release_id,
                "variants": list(release.files),
                "created_at": metadata["created_at"],
            }
        )
    return {"user_id": user_id, "releases": releases}


def _rollback_release(runtime: ManagerRuntime, user_id: str, release_id: str):
    settings = _load_settings(runtime)
    try:
        release = runtime.rollback_loader(settings.service.private_root, user_id, release_id)
    except Exception as exc:
        _append_operation_log(
            runtime,
            operation="rollback",
            user_id=user_id,
            release_id=release_id,
            status="error",
            error_code=_error_code(exc),
        )
        raise
    _append_operation_log(
        runtime,
        operation="rollback",
        user_id=user_id,
        release_id=release.release_id,
        status="success",
    )
    return {
        "user_id": release.user_id,
        "release_id": release.release_id,
        "variants": list(release.files),
    }


def _rotate_token(runtime: ManagerRuntime, user_id: str):
    settings = _load_settings(runtime)
    try:
        rotation = runtime.token_rotator(runtime.users_path, settings, user_id)
    except Exception as exc:
        _append_operation_log(
            runtime,
            operation="rotate-token",
            user_id=user_id,
            release_id=None,
            status="error",
            error_code=_error_code(exc),
        )
        raise
    _append_operation_log(
        runtime,
        operation="rotate-token",
        user_id=user_id,
        release_id=None,
        status="success",
    )
    return {
        "user_id": rotation.user_id,
        "token": rotation.token,
        "urls": dict(rotation.urls),
    }


def _import_airport(runtime: ManagerRuntime, stdin):
    settings = _load_settings(runtime)
    owner = _owner_user(settings)
    airport_source = _airport_source(owner)
    try:
        source_url = _read_single_input_line(stdin)
        _validate_airport_url(source_url)
        converter = runtime.converter_factory(settings.service.converter_base_url)
        proxies = tuple(converter.convert(source_url))
        if not proxies:
            raise SourceError("airport snapshot is empty")
        contents = yaml.safe_dump(
            {"proxies": list(proxies)},
            allow_unicode=True,
            sort_keys=False,
        )
        _atomic_write_private_file(airport_source.path, contents.encode("utf-8"))
    except OSError as exc:
        _append_operation_log(
            runtime,
            operation="import-airport",
            user_id=owner.user_id,
            release_id=None,
            status="error",
            error_code="snapshot_write_failed",
        )
        raise exc
    except Exception as exc:
        _append_operation_log(
            runtime,
            operation="import-airport",
            user_id=owner.user_id,
            release_id=None,
            status="error",
            error_code=_error_code(exc),
        )
        raise
    _append_operation_log(
        runtime,
        operation="import-airport",
        user_id=owner.user_id,
        release_id=None,
        status="success",
    )
    return {"imported": True, "owner_refresh_required": True}


def _logs(runtime: ManagerRuntime, limit: int):
    if limit < 1:
        raise ParserError("limit must be positive")
    entries = []
    if runtime.operation_log_path.exists():
        for line in runtime.operation_log_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            entries.append(payload)
    return {"entries": entries[-limit:]}


def _load_settings(runtime: ManagerRuntime) -> Settings:
    return runtime.settings_loader(runtime.service_path, runtime.users_path)


def _make_builder(runtime: ManagerRuntime, settings: Settings) -> ReleaseBuilder:
    return ReleaseBuilder(
        settings,
        runtime.converter_factory(settings.service.converter_base_url),
        runtime.traffic_client_factory(),
        local_loader=runtime.local_loader,
        renderer=runtime.renderer,
        validator=runtime.validator,
        template_dir=runtime.template_dir,
        clock=runtime.clock,
    )


def _candidate_from_path(candidate_path: Path, user_id: str, operation_id: str, variants: Sequence[str]):
    files = {}
    for variant in variants:
        files[variant] = candidate_path / ("%s.yaml" % variant)
    return type("CandidateProxy", (), {
        "operation_id": operation_id,
        "user_id": user_id,
        "path": candidate_path,
        "files": files,
        "manifest_path": candidate_path / MANIFEST_NAME,
    })()


def _current_release_info(private_root: Path, user_id: str):
    current_link = private_root / "current" / user_id
    if not current_link.exists():
        return None
    if not current_link.is_symlink():
        raise BuildError("release path is invalid")
    release_path = current_link.resolve(strict=True)
    releases_root = (private_root / "releases" / user_id).resolve(strict=True)
    if release_path.parent != releases_root:
        raise BuildError("release path is invalid")
    metadata = _read_manifest_metadata(release_path / MANIFEST_NAME)
    return {
        "release_id": release_path.name,
        "variants": tuple(metadata["variants"]),
        "created_at": metadata["created_at"],
        "input_hashes": dict(metadata["input_hashes"]),
    }


def _current_input_hashes(runtime: ManagerRuntime, settings: Settings, user) -> Mapping[str, str]:
    converter = runtime.converter_factory(settings.service.converter_base_url)
    xui_proxies = tuple(
        normalize_reality_proxy(proxy, settings.service.reality)
        for proxy in converter.convert(user.xui_source.url)
    )
    hashes = {
        "template": _hash_template_tree(runtime.template_dir),
        "xui": _hash_proxies(xui_proxies),
    }
    for source in user.local_sources:
        hashes[source.kind] = _hash_proxies(tuple(runtime.local_loader(source.path)))
    return hashes


def _traffic_payload(runtime: ManagerRuntime, source_url: str):
    try:
        info = runtime.traffic_client_factory().fetch(source_url)
    except TrafficError:
        return None
    if info is None:
        return None
    return {
        "upload": info.upload,
        "download": info.download,
        "total": info.total,
        "expire": info.expire,
        "remaining": info.remaining,
    }


def _owner_user(settings: Settings):
    for user in settings.users.values():
        if user.is_owner:
            return user
    raise BuildError("owner is missing")


def _airport_source(owner):
    for source in owner.local_sources:
        if source.kind == "airport":
            return source
    raise BuildError("airport snapshot is missing")


def _read_single_input_line(stdin) -> str:
    payload = stdin.read()
    lines = payload.splitlines()
    if len(lines) != 1:
        raise SourceError("airport source must be provided on a single line")
    value = lines[0].strip()
    if not value:
        raise SourceError("airport source must not be empty")
    return value


def _validate_airport_url(source_url: str) -> None:
    parsed = urlsplit(source_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise SourceError("airport source must be https")
    if parsed.username or parsed.password:
        raise SourceError("airport source must not include credentials")


def _atomic_write_private_file(path: Path, contents: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix="%s." % path.name,
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        os.fchmod(temp_fd, 0o600)
        with os.fdopen(temp_fd, "wb") as handle:
            handle.write(contents)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise
    os.chmod(path, 0o600)


def _append_operation_log(
    runtime: ManagerRuntime,
    *,
    operation: str,
    user_id: Optional[str],
    release_id: Optional[str],
    status: str,
    error_code: Optional[str] = None,
) -> None:
    runtime.operation_log_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    entry = {
        "timestamp": _format_timestamp(runtime.clock()),
        "operation": operation,
        "user_id": user_id,
        "release_id": release_id,
        "status": status,
    }
    if error_code is not None:
        entry["error_code"] = error_code
    handle = os.open(
        str(runtime.operation_log_path),
        os.O_APPEND | os.O_CREAT | os.O_WRONLY,
        0o600,
    )
    with os.fdopen(handle, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, sort_keys=True) + "\n")
    os.chmod(runtime.operation_log_path, 0o600)


def _read_manifest_metadata(path: Path):
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return {
        "created_at": loaded["created_at"],
        "variants": tuple(loaded["variants"]),
        "input_hashes": dict(loaded["input_hashes"]),
    }


def _error_payload(exc: Exception, argv: Sequence[str]):
    payload = {"error": {"code": _error_code(exc)}}
    user_id = _user_id_from_argv(argv)
    if user_id is not None:
        payload["user_id"] = user_id
    return payload


def _error_code(exc: Exception) -> str:
    if isinstance(exc, ParserError):
        return "settings_invalid"
    if isinstance(exc, SettingsError):
        return "settings_invalid"
    if isinstance(exc, ValidationError):
        return "validation_failed"
    if isinstance(exc, SourceError):
        return "source_failed"
    if isinstance(exc, OSError):
        return "snapshot_write_failed"
    if isinstance(exc, BuildError):
        cause = exc.__cause__
        if isinstance(cause, ValidationError):
            return "validation_failed"
        if isinstance(cause, SourceError):
            return "source_failed"
        message = str(exc)
        if "unknown user" in message:
            return "not_authorized"
        if (
            "candidate" in message
            or "release" in message
            or "manifest" in message
            or "staging root" in message
            or "operation root" in message
        ):
            return "release_missing"
    return "operation_failed"


def _user_id_from_argv(argv: Sequence[str]) -> Optional[str]:
    args = list(argv)
    if "--user" in args:
        index = args.index("--user")
        if index + 1 < len(args):
            return args[index + 1]
    for command in ("status", "history", "rollback", "rotate-token"):
        if args[:1] == [command] and len(args) > 1:
            return args[1]
    return None


def _dump_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
