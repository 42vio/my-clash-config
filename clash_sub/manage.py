"""Backup and lifecycle management commands."""

import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


def _backups_root(repo_root):
    root = Path(repo_root) / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _xui_database_path(repo_root):
    candidate = Path("/etc/x-ui/x-ui.db")
    return candidate if candidate.is_file() else None


def _nginx_config_paths():
    return (
        Path("/etc/nginx/stream-conf.d/clash-sub.conf"),
        Path("/etc/nginx/conf.d/clash-sub.conf"),
        Path("/etc/nginx/clash-sub/routes.conf"),
    )


def _versions_manifest(repo_root, runner):
    def output(arguments):
        try:
            result = runner(
                arguments,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            return result.stdout.decode("utf-8", "replace").strip()
        except Exception:
            return ""

    return {
        "repository": output(["git", "-C", str(repo_root), "rev-parse", "HEAD"]),
        "nginx": output(["nginx", "-v"]),
    }


def create_backup(repo_root, runner):
    """Create one full tarball backup; returns its path (0600)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = _backups_root(repo_root) / ("clash-sub-backup-%s.tar.gz" % stamp)
    source_files = []
    database = _xui_database_path(repo_root)
    if database:
        source_files.append(database)
    source_files.extend(path for path in _nginx_config_paths() if path.is_file())
    private_root = Path(repo_root) / "private"
    if private_root.is_dir():
        source_files.extend(
            path
            for path in sorted(private_root.rglob("*"))
            if path.is_file() and "install-state" not in path.name
        )
    descriptor, temporary = tempfile.mkstemp(dir=str(_backups_root(repo_root)))
    os.close(descriptor)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for path in source_files:
                archive.add(str(path), arcname=str(path), recursive=False)
            manifest = json.dumps(
                _versions_manifest(repo_root, runner), sort_keys=True
            ).encode("utf-8")
            info = tarfile.TarInfo("clash-sub-versions.json")
            info.size = len(manifest)
            archive.addfile(info, io.BytesIO(manifest))
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink(missing_ok=True)
    hashlib.sha256(destination.read_bytes()).hexdigest()
    return destination


def auto_snapshot(repo_root, runner, *, label):
    """Snapshot live configurations before a mutating command."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    directory = _backups_root(repo_root) / ("%s-%s" % (stamp, label))
    directory.mkdir(parents=True, exist_ok=False)
    targets = [Path(repo_root) / "private" / "config" / "service.yaml"]
    targets.extend(path for path in _nginx_config_paths() if path.is_file())
    for path in targets:
        if path.is_file():
            shutil.copy2(str(path), str(directory / path.name))
    return directory
