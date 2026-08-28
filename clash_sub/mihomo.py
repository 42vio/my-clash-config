"""Install and upgrade the non-resident Mihomo configuration validator."""

import gzip
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


_LATEST_RELEASE_URL = "https://api.github.com/repos/MetaCubeX/mihomo/releases/latest"
_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_VERSION_RE = re.compile(r"\bv\d+\.\d+\.\d+\b")


class MihomoUpdateError(RuntimeError):
    pass


def _run(runner, arguments, *, stdout=subprocess.DEVNULL, timeout=120):
    try:
        result = runner(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise MihomoUpdateError("mihomo_command_failed") from None
    if result.returncode != 0:
        raise MihomoUpdateError("mihomo_command_failed")
    return result


def _installed_version(binary, runner):
    if not binary.is_file() or not os.access(binary, os.X_OK):
        return None
    try:
        result = _run(runner, [str(binary), "-v"], stdout=subprocess.PIPE, timeout=30)
    except MihomoUpdateError:
        return None
    output = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else str(result.stdout)
    match = _VERSION_RE.search(output)
    return match.group(0) if match else None


def _release(metadata):
    if not isinstance(metadata, dict):
        raise MihomoUpdateError("mihomo_release_invalid")
    tag = metadata.get("tag_name")
    assets = metadata.get("assets")
    if metadata.get("prerelease") is not False or not isinstance(tag, str) or not _TAG_RE.fullmatch(tag) or not isinstance(assets, list):
        raise MihomoUpdateError("mihomo_release_invalid")
    expected_name = "mihomo-linux-amd64-%s.gz" % tag
    matches = [asset for asset in assets if isinstance(asset, dict) and asset.get("name") == expected_name]
    if len(matches) != 1:
        raise MihomoUpdateError("mihomo_release_invalid")
    asset = matches[0]
    url = asset.get("browser_download_url")
    digest = asset.get("digest")
    if not isinstance(url, str) or not url.startswith("https://") or not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise MihomoUpdateError("mihomo_release_invalid")
    return tag, url, digest.removeprefix("sha256:")


def install_latest_mihomo(repo_root, runner, *, binary=Path("/usr/local/lib/clash-sub/mihomo"), public_root=Path("/var/lib/clash-sub/public")):
    """Install the latest stable amd64 release after checksum/config checks."""
    repo_root = Path(repo_root)
    binary = Path(binary)
    public_root = Path(public_root)
    work_root = repo_root / "private"
    work_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mihomo-update-", dir=str(work_root)) as temporary:
        temporary = Path(temporary)
        metadata_path = temporary / "release.json"
        _run(runner, ["curl", "-fsSL", _LATEST_RELEASE_URL, "-o", str(metadata_path)])
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise MihomoUpdateError("mihomo_release_invalid") from None
        tag, download_url, expected_digest = _release(metadata)
        if _installed_version(binary, runner) == tag:
            return {"changed": False, "version": tag}

        archive = temporary / "mihomo.gz"
        _run(runner, ["curl", "-fsSL", download_url, "-o", str(archive)], timeout=600)
        try:
            archive_bytes = archive.read_bytes()
        except OSError:
            raise MihomoUpdateError("mihomo_download_invalid") from None
        if hashlib.sha256(archive_bytes).hexdigest() != expected_digest:
            raise MihomoUpdateError("mihomo_checksum_invalid")
        try:
            payload = gzip.decompress(archive_bytes)
        except (OSError, EOFError):
            raise MihomoUpdateError("mihomo_download_invalid") from None

        binary.parent.mkdir(parents=True, exist_ok=True)
        descriptor, candidate_name = tempfile.mkstemp(prefix=".mihomo.", dir=str(binary.parent))
        candidate = Path(candidate_name)
        try:
            os.fchmod(descriptor, 0o755)
            with os.fdopen(descriptor, "wb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            if _installed_version(candidate, runner) != tag:
                raise MihomoUpdateError("mihomo_binary_invalid")
            for config in sorted(public_root.rglob("*.yaml")) if public_root.is_dir() else ():
                _run(runner, [str(candidate), "-t", "-f", str(config)], timeout=30)
            os.replace(candidate, binary)
            directory = os.open(binary.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            candidate.unlink(missing_ok=True)
    return {"changed": True, "version": tag}
