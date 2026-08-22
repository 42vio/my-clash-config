import hashlib
import json
import os
import re
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

from clash_sub.domain import OWNER_VARIANTS, PreparedRelease


_RELEASE_ID_RE = re.compile(r"^[0-9TZ-]+-[a-f0-9]{8}$")
_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
_INPUT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MANIFEST_FIELDS = frozenset(
    (
        "client_id",
        "created_at",
        "input_hashes",
        "output_hashes",
        "release_id",
        "schema_version",
        "variants",
    )
)
_MEMBER_VARIANTS = ("standard",)
_PUBLIC_DIRECTORY_MODE = 0o2750


class ReleaseStoreError(RuntimeError):
    """Raised when immutable release storage cannot be used safely."""


class ReleaseStore:
    def __init__(
        self,
        private_root: Path,
        public_root: Path,
        *,
        clock: Callable[[], datetime] | None = None,
        suffix_factory: Callable[[], str] | None = None,
    ) -> None:
        self._private_root = Path(private_root)
        self._public_root = Path(public_root)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._suffix_factory = suffix_factory or (lambda: secrets.token_hex(4))

    def prepare(
        self,
        client_id: int,
        bundle: Mapping[str, str],
        input_hashes: Mapping[str, str],
    ) -> PreparedRelease | None:
        client_name = _client_name(client_id)
        variants, files = _validate_bundle(bundle)
        clean_input_hashes = _validate_input_hashes(input_hashes)
        output_hashes = {variant: _sha256(files[variant].encode("utf-8")) for variant in variants}

        current_release = self.current_release_id(client_id)
        if current_release is not None:
            current = self.verify_release(client_id, current_release)
            manifest = _load_verified_manifest(current.manifest_path, client_id, current_release)
            if manifest["output_hashes"] == output_hashes:
                return None

        release_id = _release_id(self._clock(), self._suffix_factory())
        private_stage = None
        public_stage = None
        private_release = None
        public_release = None
        try:
            private_staging_root = _private_directory(self._private_root, "staging")
            private_stage = _new_directory(private_staging_root, release_id, 0o700)
            public_gid = _require_public_root(self._public_root)
            public_releases = _new_or_existing_public_directory(
                self._public_root, "releases", public_gid
            )
            public_client_root = _new_or_existing_public_directory(
                public_releases, client_name, public_gid
            )
            public_stage = _new_public_directory(
                public_client_root, ".%s.tmp" % release_id, public_gid
            )
            manifest = {
                "schema_version": 1,
                "client_id": client_id,
                "release_id": release_id,
                "created_at": _timestamp(self._clock()),
                "variants": list(variants),
                "input_hashes": clean_input_hashes,
                "output_hashes": output_hashes,
            }
            for variant in variants:
                _write_file(private_stage / _filename(variant), files[variant].encode("utf-8"), 0o600)
                _write_file(public_stage / _filename(variant), files[variant].encode("utf-8"), 0o600)
            manifest_path = private_stage / "manifest.json"
            _write_file(
                manifest_path,
                (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
                0o600,
            )
            _write_file(
                private_stage / "manifest.sha256",
                (_sha256(manifest_path.read_bytes()) + "\n").encode("ascii"),
                0o600,
            )

            _verify_public_staging(public_stage, variants, public_gid)
            _verify_staged_release(private_stage, public_stage, manifest)
            public_target = public_client_root / release_id
            if public_target.exists() or public_target.is_symlink():
                raise ReleaseStoreError("release path is invalid")
            for variant in variants:
                os.chmod(public_stage / _filename(variant), 0o640)
            os.replace(public_stage, public_target)
            public_release = public_target
            private_releases = _private_directory(self._private_root, "releases")
            private_client_root = _new_or_existing_directory(private_releases, client_name, 0o700)
            private_target = private_client_root / release_id
            if private_target.exists() or private_target.is_symlink():
                raise ReleaseStoreError("release path is invalid")
            os.replace(private_stage, private_target)
            private_release = private_target
            return self.verify_release(client_id, release_id)
        except (ReleaseStoreError, OSError, ValueError, TypeError) as exc:
            for candidate in (public_stage, public_release, private_stage, private_release):
                if candidate is not None:
                    _remove_owned_directory(candidate)
            raise ReleaseStoreError("failed to prepare release") from exc

    def verify_release(self, client_id: int, release_id: str) -> PreparedRelease:
        client_name = _client_name(client_id)
        _validate_release_id(release_id)
        private_release = _existing_private_release_directory(
            self._private_root, "releases", client_name, release_id
        )
        public_release, public_gid = _existing_public_release_directory(
            self._public_root, "releases", client_name, release_id
        )
        manifest_path = private_release / "manifest.json"
        manifest = _load_verified_manifest(manifest_path, client_id, release_id)
        variants = tuple(manifest["variants"])
        output_hashes = manifest["output_hashes"]
        public_paths = {}
        expected_private_names = {"manifest.json", "manifest.sha256"}
        expected_public_names = set()
        for variant in variants:
            filename = _filename(variant)
            private_file = private_release / filename
            public_file = public_release / filename
            _require_regular_file(private_file, "release")
            _require_regular_file(public_file, "release")
            if stat_mode(private_file) != 0o600 or stat_mode(public_file) != 0o640:
                raise ReleaseStoreError("release permissions are invalid")
            if public_file.stat().st_gid != public_gid:
                raise ReleaseStoreError("release permissions are invalid")
            expected_hash = output_hashes[variant]
            if _sha256(private_file.read_bytes()) != expected_hash:
                raise ReleaseStoreError("release hash is invalid")
            if _sha256(public_file.read_bytes()) != expected_hash:
                raise ReleaseStoreError("release hash is invalid")
            expected_private_names.add(filename)
            expected_public_names.add(filename)
            public_paths[variant] = public_file
        if _directory_names(private_release) != expected_private_names:
            raise ReleaseStoreError("release is incomplete")
        if _directory_names(public_release) != expected_public_names:
            raise ReleaseStoreError("release is incomplete")
        return PreparedRelease(release_id, public_paths, manifest_path)

    def history(self, client_id: int) -> tuple[PreparedRelease, ...]:
        client_name = _client_name(client_id)
        if not _is_private_directory(self._private_root):
            return ()
        root = self._private_root / "releases" / client_name
        if not _is_private_directory(root):
            return ()
        releases = []
        try:
            children = tuple(root.iterdir())
        except OSError:
            return ()
        for path in children:
            if path.is_symlink() or not path.is_dir() or not _RELEASE_ID_RE.fullmatch(path.name):
                continue
            try:
                releases.append(self.verify_release(client_id, path.name))
            except ReleaseStoreError:
                continue
        return tuple(sorted(releases, key=lambda item: item.release_id, reverse=True))

    def mark_current(self, client_id: int, release_id: str) -> None:
        client_name = _client_name(client_id)
        verified = self.verify_release(client_id, release_id)
        current_root = _private_directory(self._private_root, "current")
        current = current_root / client_name
        temporary = current_root / (".%s.tmp" % client_name)
        if temporary.exists() or temporary.is_symlink():
            _remove_owned_path(temporary)
        target = "../releases/%s/%s" % (client_name, verified.release_id)
        os.symlink(target, temporary)
        try:
            os.replace(temporary, current)
        except OSError as exc:
            _remove_owned_path(temporary)
            raise ReleaseStoreError("failed to mark current release") from exc

    def current_release_id(self, client_id: int) -> str | None:
        client_name = _client_name(client_id)
        current = self._private_root / "current" / client_name
        if not current.exists() and not current.is_symlink():
            return None
        if not current.is_symlink():
            raise ReleaseStoreError("current release reference is invalid")
        try:
            target = os.readlink(current)
        except OSError as exc:
            raise ReleaseStoreError("current release reference is invalid") from exc
        parts = Path(target).parts
        if parts[:3] != ("..", "releases", client_name) or len(parts) != 4:
            raise ReleaseStoreError("current release reference is invalid")
        release_id = parts[3]
        _validate_release_id(release_id)
        try:
            if current.resolve(strict=True) != (
                self._private_root / "releases" / client_name / release_id
            ).resolve(strict=True):
                raise ReleaseStoreError("current release reference is invalid")
        except (OSError, ValueError) as exc:
            raise ReleaseStoreError("current release reference is invalid") from exc
        return release_id

    def prune(self, client_id: int, keep: int = 5) -> tuple[str, ...]:
        _client_name(client_id)
        if isinstance(keep, bool) or not isinstance(keep, int) or keep < 1:
            raise ReleaseStoreError("keep must be positive")
        history = list(self.history(client_id))
        if len(history) <= keep:
            return ()
        protected = self.current_release_id(client_id)
        removed = []
        for release in reversed(history):
            if len(history) - len(removed) <= keep:
                break
            if release.release_id == protected:
                continue
            _remove_owned_directory(release.manifest_path.parent)
            public_path = next(iter(release.public_paths.values())).parent
            _remove_owned_directory(public_path)
            removed.append(release.release_id)
        return tuple(removed)


def _validate_bundle(bundle: Mapping[str, str]) -> tuple[tuple[str, ...], dict[str, str]]:
    if not isinstance(bundle, Mapping):
        raise ReleaseStoreError("release variants are invalid")
    files = dict(bundle)
    keys = tuple(files)
    if set(keys) == set(_MEMBER_VARIANTS):
        variants = _MEMBER_VARIANTS
    elif set(keys) == set(OWNER_VARIANTS):
        variants = OWNER_VARIANTS
    else:
        raise ReleaseStoreError("release variants are invalid")
    if any(not isinstance(files[variant], str) or not files[variant] for variant in variants):
        raise ReleaseStoreError("release bundle is invalid")
    return variants, files


def _validate_input_hashes(input_hashes: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(input_hashes, Mapping) or not input_hashes:
        raise ReleaseStoreError("input hashes are invalid")
    clean = dict(input_hashes)
    for name, value in clean.items():
        if not isinstance(name, str) or not _INPUT_NAME_RE.fullmatch(name):
            raise ReleaseStoreError("input hashes are invalid")
        if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
            raise ReleaseStoreError("input hashes are invalid")
    return {name: clean[name] for name in sorted(clean)}


def _release_id(clock_value: datetime, suffix: str) -> str:
    if not isinstance(suffix, str) or not re.fullmatch(r"[a-f0-9]{8}", suffix):
        raise ReleaseStoreError("release id is invalid")
    return "%s-%s" % (_timestamp(clock_value).replace(":", "-"), suffix)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _client_name(client_id: int) -> str:
    if isinstance(client_id, bool) or not isinstance(client_id, int) or client_id < 1:
        raise ReleaseStoreError("client id is invalid")
    return str(client_id)


def _validate_release_id(release_id: str) -> None:
    if not isinstance(release_id, str) or not _RELEASE_ID_RE.fullmatch(release_id):
        raise ReleaseStoreError("release id is invalid")


def _filename(variant: str) -> str:
    return "clash-%s.yaml" % variant


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _private_directory(root: Path, name: str) -> Path:
    base = _new_or_existing_directory(root, None, 0o700)
    return _new_or_existing_directory(base, name, 0o700)


def _new_or_existing_directory(root: Path, name: str | None, mode: int) -> Path:
    path = root if name is None else root / name
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise ReleaseStoreError("release path is invalid")
    else:
        try:
            path.mkdir(mode=mode, parents=name is None)
        except OSError as exc:
            raise ReleaseStoreError("release path is invalid") from exc
    try:
        os.chmod(path, mode)
    except OSError as exc:
        raise ReleaseStoreError("release path is invalid") from exc
    return path


def _new_directory(root: Path, name: str, mode: int) -> Path:
    path = root / name
    if path.exists() or path.is_symlink():
        raise ReleaseStoreError("release path is invalid")
    try:
        path.mkdir(mode=mode)
        os.chmod(path, mode)
    except OSError as exc:
        raise ReleaseStoreError("release path is invalid") from exc
    return path


def _require_public_root(root: Path) -> int:
    if root.is_symlink() or not root.is_dir():
        raise ReleaseStoreError("public release path is invalid")
    if directory_mode(root) != _PUBLIC_DIRECTORY_MODE:
        raise ReleaseStoreError("public release permissions are invalid")
    return root.stat().st_gid


def _new_or_existing_public_directory(root: Path, name: str, public_gid: int) -> Path:
    path = root / name
    if path.exists() or path.is_symlink():
        _require_public_directory(path, public_gid)
        return path
    try:
        path.mkdir(mode=_PUBLIC_DIRECTORY_MODE)
        os.chmod(path, _PUBLIC_DIRECTORY_MODE)
    except OSError as exc:
        raise ReleaseStoreError("public release path is invalid") from exc
    _require_public_directory(path, public_gid)
    return path


def _new_public_directory(root: Path, name: str, public_gid: int) -> Path:
    path = root / name
    if path.exists() or path.is_symlink():
        raise ReleaseStoreError("public release path is invalid")
    try:
        path.mkdir(mode=_PUBLIC_DIRECTORY_MODE)
        os.chmod(path, _PUBLIC_DIRECTORY_MODE)
    except OSError as exc:
        raise ReleaseStoreError("public release path is invalid") from exc
    _require_public_directory(path, public_gid)
    return path


def _existing_public_release_directory(root: Path, *parts: str) -> tuple[Path, int]:
    public_gid = _require_public_root(root)
    path = root
    for part in parts:
        path = path / part
        _require_public_directory(path, public_gid)
    return path, public_gid


def _require_public_directory(path: Path, public_gid: int) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ReleaseStoreError("public release path is invalid")
    path_stat = path.stat()
    if directory_mode(path) != _PUBLIC_DIRECTORY_MODE:
        raise ReleaseStoreError("public release permissions are invalid")
    if path_stat.st_gid != public_gid:
        raise ReleaseStoreError("public release group is invalid")


def _existing_private_release_directory(root: Path, *parts: str) -> Path:
    if not _is_private_directory(root):
        _raise_private_directory_error(root)
    path = root
    for part in parts:
        path = path / part
        if not _is_private_directory(path):
            _raise_private_directory_error(path)
    return path


def _is_private_directory(path: Path) -> bool:
    return not path.is_symlink() and path.is_dir() and stat_mode(path) == 0o700


def _raise_private_directory_error(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise ReleaseStoreError("release path is invalid")
    raise ReleaseStoreError("release permissions are invalid")


def _write_file(path: Path, contents: bytes, mode: int) -> None:
    with path.open("xb") as handle:
        os.chmod(path, mode)
        handle.write(contents)
    os.chmod(path, mode)


def _verify_public_staging(public_stage: Path, variants: tuple[str, ...], public_gid: int) -> None:
    _require_public_directory(public_stage, public_gid)
    for variant in variants:
        public_file = public_stage / _filename(variant)
        _require_regular_file(public_file, "release")
        if stat_mode(public_file) != 0o600 or public_file.stat().st_gid != public_gid:
            raise ReleaseStoreError("release permissions are invalid")


def _verify_staged_release(private_stage: Path, public_stage: Path, manifest: Mapping[str, object]) -> None:
    for variant, expected_hash in manifest["output_hashes"].items():
        private_file = private_stage / _filename(variant)
        public_file = public_stage / _filename(variant)
        if _sha256(private_file.read_bytes()) != expected_hash:
            raise ReleaseStoreError("release hash is invalid")
        if _sha256(public_file.read_bytes()) != expected_hash:
            raise ReleaseStoreError("release hash is invalid")
    _load_verified_manifest(private_stage / "manifest.json", manifest["client_id"], manifest["release_id"])


def _load_verified_manifest(path: Path, client_id: int, release_id: str) -> dict[str, object]:
    _require_regular_file(path, "manifest")
    digest_path = path.with_name("manifest.sha256")
    _require_regular_file(digest_path, "digest")
    if stat_mode(path) != 0o600 or stat_mode(digest_path) != 0o600:
        raise ReleaseStoreError("manifest permissions are invalid")
    try:
        digest = digest_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ReleaseStoreError("manifest digest is invalid") from exc
    if not _HASH_RE.fullmatch(digest.rstrip("\n")) or digest != digest.rstrip("\n") + "\n":
        raise ReleaseStoreError("manifest digest is invalid")
    if _sha256(path.read_bytes()) != digest.rstrip("\n"):
        raise ReleaseStoreError("manifest digest is invalid")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReleaseStoreError("manifest is invalid") from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_FIELDS:
        raise ReleaseStoreError("manifest is invalid")
    if manifest.get("schema_version") != 1:
        raise ReleaseStoreError("manifest is invalid")
    if manifest.get("client_id") != client_id or manifest.get("release_id") != release_id:
        raise ReleaseStoreError("manifest is invalid")
    created_at = manifest.get("created_at")
    if not isinstance(created_at, str):
        raise ReleaseStoreError("manifest is invalid")
    try:
        datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise ReleaseStoreError("manifest is invalid") from exc
    variants, _ = _validate_bundle({variant: "x" for variant in manifest.get("variants", ())})
    if list(variants) != manifest.get("variants"):
        raise ReleaseStoreError("manifest is invalid")
    manifest["input_hashes"] = _validate_input_hashes(manifest.get("input_hashes"))
    output_hashes = manifest.get("output_hashes")
    if not isinstance(output_hashes, dict) or set(output_hashes) != set(variants):
        raise ReleaseStoreError("manifest is invalid")
    if any(not isinstance(value, str) or not _HASH_RE.fullmatch(value) for value in output_hashes.values()):
        raise ReleaseStoreError("manifest is invalid")
    return manifest


def _require_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReleaseStoreError("%s is invalid" % label)


def _directory_names(path: Path) -> set[str]:
    try:
        children = tuple(path.iterdir())
    except OSError as exc:
        raise ReleaseStoreError("release is incomplete") from exc
    if any(child.is_symlink() or child.is_dir() for child in children):
        raise ReleaseStoreError("release is incomplete")
    return {child.name for child in children}


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def directory_mode(path: Path) -> int:
    return path.stat().st_mode & 0o7777


def _remove_owned_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        raise ReleaseStoreError("release path is invalid")


def _remove_owned_directory(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or not path.is_dir():
        return
    shutil.rmtree(path)
