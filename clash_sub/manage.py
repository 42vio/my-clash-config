"""Backup and lifecycle management commands."""

import datetime
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from clash_sub.config import ConfigError, load_config
from clash_sub.installer import InstallPaths, Installer, load_install_state
from clash_sub.mihomo import MihomoUpdateError, install_latest_mihomo
from clash_sub.runtime import config_path
from clash_sub.service import _OperationLock


def _backups_root(repo_root):
    root = Path(repo_root) / "backups"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _xui_database_path(repo_root):
    candidate = Path("/etc/x-ui/x-ui.db")
    return candidate if candidate.is_file() else None


def _nginx_config_paths():
    """The two nginx files a rebuild restores verbatim."""
    return (
        Path("/etc/nginx/stream-conf.d/clash-sub.conf"),
        Path("/etc/nginx/conf.d/clash-sub.conf"),
    )


_ROUTES_CONF_PATH = Path("/etc/nginx/clash-sub/routes.conf")


def _runtime_private_root(repo_root):
    """Resolve the configured private-root, or None when not yet configured."""
    service_yaml = config_path(repo_root)
    if not service_yaml.is_file():
        return None
    try:
        return load_config(service_yaml, repo_root).private_root
    except ConfigError:
        return None


_BACKUP_ARCHIVE_NAMES = (
    "etc/x-ui/x-ui.db",
    "etc/nginx/stream-conf.d/clash-sub.conf",
    "etc/nginx/conf.d/clash-sub.conf",
    "var/lib/clash-sub/private/state.json",
    "var/lib/clash-sub/private/airport-source.json",
)


def create_backup(repo_root, runner):
    """Archive exactly the five rebuild-essential files; returns the path (0600)."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = _backups_root(repo_root) / ("clash-sub-backup-%s.tar.gz" % stamp)
    private_root = _runtime_private_root(repo_root)
    sources = []
    database = _xui_database_path(repo_root)
    if database is not None:
        sources.append(database)
    sources.extend(_nginx_config_paths())
    if private_root is not None:
        sources.append(private_root / "state.json")
        sources.append(private_root / "airport-source.json")
    if len(sources) != len(_BACKUP_ARCHIVE_NAMES) or any(
        not path.is_file() for path in sources
    ):
        raise RuntimeError("backup_incomplete")
    descriptor, temporary = tempfile.mkstemp(dir=str(_backups_root(repo_root)))
    os.close(descriptor)
    try:
        with tarfile.open(temporary, "w:gz") as archive:
            for path, archive_name in zip(sources, _BACKUP_ARCHIVE_NAMES):
                archive.add(str(path), arcname=archive_name, recursive=False)
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
    targets.extend(
        path for path in (*_nginx_config_paths(), _ROUTES_CONF_PATH) if path.is_file()
    )
    names = {path.name: sum(item.name == path.name for item in targets) for path in targets}
    for path in targets:
        if path.is_file():
            # Basenames collide for stream/http config files.  The stable
            # parent directory retains every input while keeping existing
            # single-file snapshot names compatible.
            target = directory / path.name
            if names[path.name] > 1:
                target = directory / path.parent.name / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(path), str(target))
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


def _rerender_nginx(repo_root, runner, state, *, paths, config):
    from clash_sub.nginx import (
        NginxError,
        activate_nginx_files,
        render_stream_config,
        render_sub_server,
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
            nginx_binary=str(config.nginx_binary),
            systemctl_binary=str(config.systemctl_binary),
            reload=True,
            journal_path=paths.private_root / ".nginx-rerender-journal.json",
        )
    except NginxError:
        raise RuntimeError("nginx_rerender_failed") from None
    return True


def run_update(repo_root, runner):
    """Pull new code, then delegate post-update work to a fresh process.

    This process still runs the pre-pull code objects, so any systemd/nginx
    work must happen in a child that loads the newly pulled code from disk.
    """
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
    result = runner(
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
    if result.returncode != 0:
        raise RuntimeError("pip_sync_failed")
    try:
        child = runner(
            [
                str(Path(repo_root) / ".venv" / "bin" / "python"),
                str(Path(repo_root) / "bin" / "clash-sub"),
                "update",
                "--post-update",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=900,
            check=False,
        )
    except Exception:
        raise RuntimeError("post_update_failed") from None
    if child.returncode != 0:
        raise RuntimeError("post_update_failed")
    return True


def run_post_update(repo_root, runner):
    """Apply systemd/nginx post-update steps with the newly pulled code.

    Runs only in the child spawned by run_update; never spawns another child.
    """
    repo_root = Path(repo_root)
    try:
        config = load_config(config_path(repo_root), repo_root)
    except Exception:
        raise RuntimeError("post_update_config_invalid") from None
    paths = InstallPaths(
        xui_database=config.xui_database,
        private_root=config.private_root,
        public_root=config.public_root,
        routes_conf=config.nginx_routes,
        mihomo_binary=config.mihomo_binary,
    )
    installer = Installer(repo_root, paths=paths, runner=runner)
    installer._prepare_runtime_directories()
    installer.harden_systemd()
    state = _load_install_state(repo_root)
    _rerender_nginx(repo_root, runner, state, paths=paths, config=config)
    return True


def update_mihomo(repo_root, runner):
    """Upgrade Mihomo independently from repository code updates."""
    repo_root = Path(repo_root)
    try:
        config = load_config(config_path(repo_root), repo_root)
        with _OperationLock(Path(config.private_root) / "operation.lock"):
            return install_latest_mihomo(
                repo_root,
                runner,
                binary=config.mihomo_binary,
                public_root=config.public_root,
            )
    except MihomoUpdateError as error:
        raise RuntimeError(str(error)) from None
    except ConfigError:
        raise RuntimeError("service_config_invalid") from None


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
                not_after.split("=", 1)[-1], "%b %d %H:%M:%S %Y"
            ).replace(tzinfo=datetime.timezone.utc)
            days_left = (
                expiry - datetime.datetime.now(datetime.timezone.utc)
            ).days
        except ValueError:
            days_left = None
    return {
        "units": {"nginx": unit_state("nginx"), "x-ui": unit_state("x-ui")},
        "certificate": {"not_after": not_after or "unknown", "days_left": days_left},
    }
