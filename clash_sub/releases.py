import hashlib
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, Mapping, Optional, Sequence, Tuple

from clash_sub.converter import (
    SourceError,
    load_local_proxies,
    merge_proxy_sources,
    normalize_reality_proxy,
)
from clash_sub.models import Candidate, LOCAL_SOURCE_KINDS, Release, Settings, SourceSpec, VARIANTS
from clash_sub.rendering import render_variant
from clash_sub.traffic import TrafficClient, TrafficError
from clash_sub.validation import ValidationError, sha256_bytes, sha256_file, validate_config


VARIANT_SUFFIX = ".yaml"
MANIFEST_NAME = "manifest.json"
MANIFEST_DIGEST_NAME = "manifest.sha256"
SIDECAR_SUFFIX = ".meta.json"
SOURCE_LABELS = {
    "xui": "3x-ui",
    "airport": "机场",
    "home": "家庭",
}
SAFE_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MANIFEST_FIELDS = frozenset(
    (
        "created_at",
        "input_hashes",
        "operation_id",
        "output_hashes",
        "schema_version",
        "source_counts",
        "user_id",
        "variants",
    )
)


class BuildError(RuntimeError):
    """Raised when a candidate or release cannot be safely built or published."""


class ReleaseBuilder:
    def __init__(
        self,
        settings: Settings,
        converter,
        traffic_client: TrafficClient,
        local_loader: Callable[[Path], Sequence[Mapping[str, object]]] = load_local_proxies,
        renderer: Callable[[Path, str, Sequence[Mapping[str, object]]], str] = render_variant,
        validator: Callable[[str, Sequence[str], object], Mapping[str, object]] = validate_config,
        template_dir: Optional[Path] = None,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self._settings = settings
        self._converter = converter
        self._traffic_client = traffic_client
        self._local_loader = local_loader
        self._renderer = renderer
        self._validator = validator
        self._template_dir = template_dir or Path(__file__).resolve().parents[1] / "templates"
        self._clock = clock or _utcnow

    def build_candidate(self, user_id: str, operation_id: str) -> Candidate:
        _validate_slug(user_id, "user id")
        _validate_slug(operation_id, "operation id")
        user = self._settings.users.get(user_id)
        if user is None:
            raise BuildError("unknown user")
        if user.role != "owner" and user.local_sources:
            raise BuildError("member candidates may not access local sources")

        private_root = self._settings.service.private_root
        staging_root = private_root / "staging"
        operation_root = staging_root / operation_id
        candidate_root = operation_root / user_id
        manifest_path = candidate_root / MANIFEST_NAME
        _ensure_private_dir(private_root)
        _ensure_private_dir(staging_root)
        _ensure_private_dir(operation_root)
        if candidate_root.exists():
            raise BuildError("candidate already exists")
        candidate_root.mkdir(mode=0o700)

        try:
            xui_url = user.xui_source.url
            if not xui_url:
                raise BuildError("xui source is missing")
            converted = self._converter.convert(xui_url)
            xui_proxies = tuple(
                normalize_reality_proxy(proxy, self._settings.service.reality)
                for proxy in converted
            )
            if not xui_proxies:
                raise BuildError("xui source is empty")

            source_urls = [xui_url]
            input_hashes = {
                "template": _hash_template_tree(self._template_dir),
                "xui": _hash_proxies(xui_proxies),
            }
            source_counts = {"xui": len(xui_proxies)}
            merge_inputs = [(SOURCE_LABELS["xui"], xui_proxies)]

            if user.is_owner:
                for source in user.local_sources:
                    proxies = _load_local_source(self._local_loader, source)
                    source_urls.append(str(source.path))
                    input_hashes[source.kind] = _hash_proxies(proxies)
                    source_counts[source.kind] = len(proxies)
                    merge_inputs.append((SOURCE_LABELS[source.kind], proxies))

            merged_proxies = merge_proxy_sources(merge_inputs)
            traffic = _fetch_traffic(self._traffic_client, xui_url)
            files = {}
            output_hashes = {}
            created_at = _format_timestamp(self._clock())

            for variant in user.variants:
                rendered = self._renderer(self._template_dir, variant, merged_proxies)
                self._validator(rendered, tuple(source_urls), self._settings.service.reality)
                yaml_path = candidate_root / ("%s%s" % (variant, VARIANT_SUFFIX))
                _write_private_text(yaml_path, rendered)
                yaml_hash = sha256_file(yaml_path)
                output_hashes[variant] = yaml_hash
                sidecar = {
                    "schema_version": 1,
                    "variant": variant,
                    "created_at": created_at,
                    "yaml_sha256": yaml_hash,
                }
                if traffic is not None:
                    sidecar["traffic"] = {
                        "upload": traffic.upload,
                        "download": traffic.download,
                        "total": traffic.total,
                        "expire": traffic.expire,
                    }
                _write_private_json(yaml_path.with_suffix(SIDECAR_SUFFIX), sidecar)
                files[variant] = yaml_path

            manifest = {
                "schema_version": 1,
                "operation_id": operation_id,
                "user_id": user_id,
                "created_at": created_at,
                "variants": list(user.variants),
                "input_hashes": input_hashes,
                "output_hashes": output_hashes,
                "source_counts": source_counts,
            }
            _write_private_json(manifest_path, manifest)
            _write_manifest_digest(manifest_path)
            return Candidate(
                operation_id=operation_id,
                user_id=user_id,
                path=candidate_root,
                files=dict(files),
                manifest_path=manifest_path,
            )
        except BuildError:
            _cleanup_candidate(staging_root, operation_root)
            raise
        except (OSError, SourceError, TrafficError, ValidationError, ValueError, KeyError):
            _cleanup_candidate(staging_root, operation_root)
            raise BuildError("failed to build release candidate")


def publish_candidate(candidate: Candidate, private_root: Path, keep: int = 5) -> Release:
    if keep < 1:
        raise BuildError("keep must be positive")
    _validate_slug(candidate.user_id, "user id")
    _validate_slug(candidate.operation_id, "operation id")
    if candidate.manifest_path != candidate.path / MANIFEST_NAME:
        raise BuildError("candidate manifest path is invalid")
    staging_root = private_root / "staging"
    operation_root = staging_root / candidate.operation_id
    expected_candidate_path = staging_root / candidate.operation_id / candidate.user_id
    _require_real_directory(staging_root, "staging root")
    _require_real_directory(operation_root, "operation root")
    _require_real_directory(expected_candidate_path, "candidate path")
    _require_real_directory(candidate.path, "candidate path")
    _require_exact_resolved_path(expected_candidate_path, candidate.path, "candidate path")
    _require_exact_resolved_path(
        expected_candidate_path / MANIFEST_NAME,
        candidate.manifest_path,
        "candidate manifest path",
    )
    manifest = _load_manifest(candidate.manifest_path)
    manifest_variants = _validate_manifest(manifest, candidate.operation_id, candidate.user_id)
    _verify_release_hashes(candidate.path, manifest_variants, manifest, candidate.user_id)

    releases_root = private_root / "releases" / candidate.user_id
    current_root = private_root / "current"
    _ensure_private_dir(private_root)
    _ensure_private_dir(private_root / "releases")
    _ensure_private_dir(releases_root)
    _ensure_private_dir(current_root)

    release_id = candidate.operation_id
    release_path = releases_root / release_id
    if release_path.exists():
        raise BuildError("release already exists")
    try:
        candidate.path.rename(release_path)
        release = _release_from_manifest(release_path, manifest_variants, candidate.user_id)
        _switch_current_link(private_root, candidate.user_id, release_path)
        _prune_old_releases(releases_root, keep)
        return release
    except OSError:
        raise BuildError("failed to publish release")


def list_history(private_root: Path, user_id: str) -> Tuple[Release, ...]:
    _validate_slug(user_id, "user id")
    releases_root = private_root / "releases" / user_id
    if not releases_root.exists():
        return ()
    releases = []
    for child in releases_root.iterdir():
        release = _try_load_release(child, user_id)
        if release is not None:
            releases.append(release)
    releases.sort(reverse=True)
    return tuple(item[2] for item in releases)


def rollback(private_root: Path, user_id: str, release_id: str) -> Release:
    _validate_slug(user_id, "user id")
    _validate_release_id(release_id)
    releases_root = private_root / "releases" / user_id
    release_path = releases_root / release_id
    release = _load_release(release_path, user_id)
    _switch_current_link(private_root, user_id, release.path)
    return release


def _load_local_source(loader, source: SourceSpec):
    if source.path is None:
        raise BuildError("local source path is missing")
    proxies = tuple(loader(source.path))
    if not proxies:
        raise BuildError("local source is empty")
    return proxies


def _fetch_traffic(traffic_client: TrafficClient, source_url: str):
    try:
        return traffic_client.fetch(source_url)
    except TrafficError:
        return None


def _cleanup_candidate(staging_root: Path, operation_root: Path) -> None:
    _remove_private_path(staging_root, operation_root)


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_private_text(path: Path, contents: str) -> None:
    _write_private_bytes(path, contents.encode("utf-8"))


def _write_private_json(path: Path, value: Mapping[str, object]) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _write_private_text(path, text)


def _write_private_bytes(path: Path, contents: bytes) -> None:
    handle = path.open("wb")
    try:
        os.chmod(path, 0o600)
        handle.write(contents)
    finally:
        handle.close()
    os.chmod(path, 0o600)


def _write_manifest_digest(manifest_path: Path) -> None:
    digest = sha256_bytes(manifest_path.read_bytes())
    _write_private_text(manifest_path.with_name(MANIFEST_DIGEST_NAME), digest + "\n")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    current = value
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    return current.strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash_template_tree(template_dir: Path) -> str:
    hasher = hashlib.sha256()
    for path in sorted(template_dir.rglob("*")):
        if path.is_dir():
            continue
        relative = path.relative_to(template_dir).as_posix().encode("utf-8")
        hasher.update(relative)
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _hash_proxies(proxies: Sequence[Mapping[str, object]]) -> str:
    payload = json.dumps(proxies, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return sha256_bytes(payload)


def _load_manifest(path: Path) -> Mapping[str, object]:
    _verify_manifest_digest(path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BuildError("release manifest is invalid")
    if not isinstance(manifest, dict):
        raise BuildError("release manifest is invalid")
    return manifest


def _verify_release_hashes(
    release_path: Path,
    manifest_variants: Sequence[str],
    manifest: Mapping[str, object],
    user_id: str,
) -> None:
    output_hashes = manifest.get("output_hashes")
    if not isinstance(output_hashes, dict):
        raise BuildError("release manifest is invalid")
    if manifest.get("user_id") != user_id:
        raise BuildError("release manifest is invalid")
    expected_variants = set(manifest_variants)
    if _discover_release_variants(release_path, VARIANT_SUFFIX) != expected_variants:
        raise BuildError("release is incomplete")
    if _discover_release_variants(release_path, SIDECAR_SUFFIX) != expected_variants:
        raise BuildError("release is incomplete")

    for variant in manifest_variants:
        expected_hash = output_hashes.get(variant)
        if not isinstance(expected_hash, str):
            raise BuildError("release manifest is invalid")
        yaml_path = release_path / ("%s%s" % (variant, VARIANT_SUFFIX))
        sidecar_path = yaml_path.with_suffix(SIDECAR_SUFFIX)
        if not yaml_path.exists() or not sidecar_path.exists():
            raise BuildError("release is incomplete")
        if sha256_file(yaml_path) != expected_hash:
            raise BuildError("release hash mismatch")
        sidecar = _load_sidecar(sidecar_path)
        if sidecar.get("yaml_sha256") != expected_hash:
            raise BuildError("release hash mismatch")


def _load_sidecar(path: Path) -> Mapping[str, object]:
    try:
        sidecar = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise BuildError("release sidecar is invalid")
    if not isinstance(sidecar, dict):
        raise BuildError("release sidecar is invalid")
    return sidecar


def _release_from_manifest(
    release_path: Path,
    manifest_variants: Sequence[str],
    user_id: str,
) -> Release:
    files = {}
    for variant in manifest_variants:
        files[variant] = release_path / ("%s%s" % (variant, VARIANT_SUFFIX))
    return Release(
        release_id=release_path.name,
        user_id=user_id,
        path=release_path,
        files=files,
    )


def _validate_release_id(release_id: str) -> None:
    _validate_slug(release_id, "release id")


def _load_release(release_path: Path, user_id: str) -> Release:
    _validate_slug(release_path.name, "release id")
    releases_root = release_path.parent
    resolved_root = releases_root.resolve()
    try:
        resolved_release = release_path.resolve()
        resolved_release.relative_to(resolved_root)
    except (OSError, ValueError):
        raise BuildError("release path escapes release root")
    manifest = _load_manifest(release_path / MANIFEST_NAME)
    manifest_variants = _validate_manifest(manifest, release_path.name, user_id)
    _verify_release_hashes(release_path, manifest_variants, manifest, user_id)
    return _release_from_manifest(release_path, manifest_variants, user_id)


def _switch_current_link(private_root: Path, user_id: str, release_path: Path) -> None:
    _validate_slug(user_id, "user id")
    current_root = private_root / "current"
    _ensure_private_dir(current_root)
    current_link = current_root / user_id
    temp_link = current_root / ("%s.tmp" % user_id)
    if temp_link.exists() or temp_link.is_symlink():
        temp_link.unlink()
    relative_target = os.path.relpath(str(release_path), str(current_root))
    os.symlink(relative_target, temp_link)
    os.replace(temp_link, current_link)


def _prune_old_releases(releases_root: Path, keep: int) -> None:
    history = list_history(releases_root.parent.parent, releases_root.name)
    for release in history[keep:]:
        _remove_private_path(releases_root, release.path)


def _try_load_release(path: Path, user_id: str):
    if not path.is_dir():
        return None
    try:
        release = _load_release(path, user_id)
    except BuildError:
        return None
    stat_result = path.stat()
    return stat_result.st_mtime_ns, release.release_id, release


def _validate_slug(value: str, label: str) -> None:
    if not isinstance(value, str) or not SAFE_SLUG_RE.match(value):
        raise BuildError("invalid %s" % label)


def _verify_manifest_digest(manifest_path: Path) -> None:
    digest_path = manifest_path.with_name(MANIFEST_DIGEST_NAME)
    try:
        digest = digest_path.read_text(encoding="utf-8")
    except OSError:
        raise BuildError("release manifest integrity is invalid")
    if not SHA256_RE.match(digest.rstrip("\n")) or digest != digest.rstrip("\n") + "\n":
        raise BuildError("release manifest integrity is invalid")
    if sha256_bytes(manifest_path.read_bytes()) != digest.rstrip("\n"):
        raise BuildError("release manifest integrity is invalid")


def _validate_manifest(manifest: Mapping[str, object], release_id: str, user_id: str) -> Tuple[str, ...]:
    if set(manifest) != MANIFEST_FIELDS:
        raise BuildError("release manifest is invalid")
    if manifest.get("schema_version") != 1:
        raise BuildError("release manifest is invalid")
    if manifest.get("operation_id") != release_id:
        raise BuildError("release manifest is invalid")
    if manifest.get("user_id") != user_id:
        raise BuildError("release manifest is invalid")
    manifest_variants = _validate_manifest_variants(manifest.get("variants"))
    _validate_timestamp(manifest.get("created_at"))
    _validate_hash_mapping(manifest.get("input_hashes"), ("template", "xui"), LOCAL_SOURCE_KINDS)
    _validate_hash_mapping(manifest.get("output_hashes"), manifest_variants, exact=True)
    _validate_count_mapping(manifest.get("source_counts"))
    return manifest_variants


def _validate_manifest_variants(value: object) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise BuildError("release manifest is invalid")
    variants = []
    seen = set()
    for item in value:
        if not isinstance(item, str) or item not in VARIANTS or item in seen:
            raise BuildError("release manifest is invalid")
        seen.add(item)
        variants.append(item)
    return tuple(variants)


def _validate_timestamp(value: object) -> None:
    if not isinstance(value, str):
        raise BuildError("release manifest is invalid")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise BuildError("release manifest is invalid")


def _validate_hash_mapping(
    value: object,
    required_keys: Sequence[str],
    optional_keys: Sequence[str] = (),
    exact: bool = False,
) -> None:
    if not isinstance(value, dict):
        raise BuildError("release manifest is invalid")
    allowed_keys = set(required_keys) | set(optional_keys)
    actual_keys = set(value)
    if exact:
        if actual_keys != set(required_keys):
            raise BuildError("release manifest is invalid")
    elif not actual_keys.issubset(allowed_keys):
        raise BuildError("release manifest is invalid")
    if not set(required_keys).issubset(actual_keys):
        raise BuildError("release manifest is invalid")
    for key in sorted(actual_keys):
        item = value.get(key)
        if not isinstance(item, str) or not SHA256_RE.match(item):
            raise BuildError("release manifest is invalid")


def _validate_count_mapping(value: object) -> None:
    if not isinstance(value, dict):
        raise BuildError("release manifest is invalid")
    actual_keys = tuple(sorted(value))
    allowed_keys = ("xui",) + tuple(LOCAL_SOURCE_KINDS)
    if not set(actual_keys).issubset(set(allowed_keys)):
        raise BuildError("release manifest is invalid")
    if "xui" not in value:
        raise BuildError("release manifest is invalid")
    for key in actual_keys:
        item = value.get(key)
        if not isinstance(item, int) or item < 0:
            raise BuildError("release manifest is invalid")


def _remove_private_path(root: Path, target: Path) -> None:
    if not target.exists() and not target.is_symlink():
        return
    resolved_target = _require_canonical_child(root, target, "cleanup path")
    if target.is_symlink() or resolved_target.is_file():
        target.unlink()
        return
    shutil.rmtree(str(resolved_target))


def _require_canonical_child(root: Path, target: Path, label: str) -> Path:
    try:
        resolved_root = root.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
        resolved_target.relative_to(resolved_root)
    except (FileNotFoundError, OSError, ValueError):
        raise BuildError("%s escapes root" % label)
    return resolved_target


def _require_exact_resolved_path(expected: Path, actual: Path, label: str) -> Path:
    try:
        resolved_expected = expected.resolve(strict=True)
        resolved_actual = actual.resolve(strict=True)
    except (FileNotFoundError, OSError):
        raise BuildError("%s is invalid" % label)
    if resolved_actual != resolved_expected:
        raise BuildError("%s is invalid" % label)
    return resolved_actual


def _require_real_directory(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_dir():
        raise BuildError("%s is invalid" % label)


def _discover_release_variants(release_path: Path, suffix: str) -> set:
    variants = set()
    try:
        children = tuple(release_path.iterdir())
    except OSError:
        raise BuildError("release is incomplete")
    for child in children:
        if child.is_dir():
            continue
        name = child.name
        if name.endswith(suffix):
            variants.add(name[: -len(suffix)])
    return variants
