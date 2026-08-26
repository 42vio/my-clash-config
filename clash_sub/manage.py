"""Backup and lifecycle management commands."""

import datetime
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from clash_sub.config import ConfigError, load_config
from clash_sub.installer import InstallState, load_install_state
from clash_sub.runtime import config_path


def _backups_root(repo_root):
    root = Path(repo_root) / "backups"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
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


def _runtime_private_root(repo_root):
    """Resolve the configured private-root, or None when not yet configured."""
    service_yaml = config_path(repo_root)
    if not service_yaml.is_file():
        return None
    try:
        return load_config(service_yaml, repo_root).private_root
    except ConfigError:
        return None


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
    runtime_root = _runtime_private_root(repo_root)
    if runtime_root is not None and runtime_root.is_dir() and runtime_root != private_root:
        source_files.extend(
            path for path in sorted(runtime_root.rglob("*")) if path.is_file()
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


_ACME = Path("/root/.acme.sh/acme.sh")


def _load_install_state(repo_root):
    return load_install_state(Path(repo_root) / "private" / "install-state.json")


def _fullchain_path(repo_root=None):
    return Path("/etc/ssl/domain/fullchain.pem")


def _read_certificate_expiry(runner):
    try:
        result = runner(
            ["openssl", "x509", "-noout", "-enddate", "-in", str(_fullchain_path())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(
        r"notAfter=(\w{3})\s+(\d{1,2})\s+(\d{2}:\d{2}:\d{2})\s+(\d{4})",
        result.stdout.decode("ascii", "replace"),
    )
    return match.group(0) if match else None


def cert_status(repo_root, runner):
    fullchain = _fullchain_path(repo_root)
    not_after = _read_certificate_expiry(runner) if fullchain.is_file() else None
    return {"present": fullchain.is_file(), "not_after": not_after or "unknown"}


def cert_renew(repo_root, runner):
    state = _load_install_state(repo_root)
    result = runner(
        [
            str(_ACME),
            "--renew",
            "-d",
            state.domain,
            "--force",
            "--ecc",
            "--home",
            str(_ACME.parent),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("cert_renew_failed")
    return True


def _rerender_nginx(repo_root, runner, state):
    from clash_sub.domain import ServiceConfig
    from clash_sub.installer import InstallPaths
    from clash_sub.nginx import (
        NginxError,
        activate_nginx_files,
        render_stream_config,
        render_sub_server,
    )

    paths = InstallPaths()
    config = ServiceConfig(
        owner_email="pending",
        subscription_authority="sub.%s:443" % state.domain,
        xui_public_endpoint="%s:443" % state.domain,
        xui_database=paths.xui_database,
        private_root=paths.private_root,
        public_root=paths.public_root,
        nginx_routes=paths.routes_conf,
        mihomo_binary=Path("/usr/local/lib/clash-sub/mihomo"),
        nginx_binary=Path("/usr/sbin/nginx"),
        systemctl_binary=Path("/usr/bin/systemctl"),
        template_root=Path(repo_root) / "templates",
    )
    stream = render_stream_config(config, state.domain)
    sub_server = render_sub_server(
        config,
        domain=state.domain,
        panel_port=state.panel_port,
        panel_base_path=state.panel_base_path,
        routes_include=str(paths.routes_conf),
        fullchain=str(paths.fullchain()),
        privkey=str(paths.privkey()),
    )
    try:
        activate_nginx_files(
            (
                (paths.stream_conf(), stream.encode("utf-8"), 0o640),
                (paths.http_conf(), sub_server.encode("utf-8"), 0o640),
            ),
            runner,
            nginx_binary="/usr/sbin/nginx",
            systemctl_binary="/usr/bin/systemctl",
            reload=True,
        )
    except NginxError:
        raise RuntimeError("nginx_rerender_failed") from None
    return True


def run_update(repo_root, runner):
    auto_snapshot(repo_root, runner, label="pre-update")
    result = runner(
        ["git", "-C", str(repo_root), "pull", "--ff-only"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("git_pull_failed")
    runner(
        [
            str(Path(repo_root) / ".venv" / "bin" / "pip"),
            "install",
            "-r",
            str(Path(repo_root) / "requirements.txt"),
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=600,
        check=False,
    )
    state = _load_install_state(repo_root)
    _rerender_nginx(repo_root, runner, state)
    return True


def health_report(repo_root, runner):
    def unit_state(unit):
        try:
            result = runner(
                ["systemctl", "is-active", unit],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
            return result.stdout.decode("ascii", "replace").strip()
        except Exception:
            return "unknown"

    not_after = _read_certificate_expiry(runner)
    days_left = None
    if not_after:
        try:
            expiry = datetime.datetime.strptime(
                not_after.split("=", 1)[-1], "%b %d %H:%M:%S %Y GMT"
            )
            days_left = (expiry - datetime.datetime.utcnow()).days
        except ValueError:
            days_left = None
    return {
        "units": {"nginx": unit_state("nginx"), "x-ui": unit_state("x-ui")},
        "certificate": {"not_after": not_after or "unknown", "days_left": days_left},
    }
