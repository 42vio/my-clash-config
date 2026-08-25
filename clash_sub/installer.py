"""One-shot integration installer for the unified 443 topology."""

import json
import os
import socket
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clash_sub.xui import XuiCompatibilityError, read_panel_port, read_xui_snapshot

_MINIMUM_FREE_BYTES = 1024 ** 3
_DEBIAN_MAJOR = "12"
_OS_RELEASE_PATH = Path("/etc/os-release")


class InstallerError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class InstallPaths:
    """Filesystem layout touched by the installer.  Overridable for tests."""

    nginx_conf: Path = Path("/etc/nginx/nginx.conf")
    stream_conf_dir: Path = Path("/etc/nginx/stream-conf.d")
    http_conf_dir: Path = Path("/etc/nginx/conf.d")
    routes_conf: Path = Path("/etc/nginx/clash-sub/routes.conf")
    ssl_dir: Path = Path("/etc/ssl/domain")
    acme_home: Path = Path("/root/.acme.sh")
    sysctl_conf: Path = Path("/etc/sysctl.d/99-clash-sub.conf")
    journald_conf_dir: Path = Path("/etc/systemd/journald.conf.d")
    systemd_dir: Path = Path("/etc/systemd/system")
    swap_file: Path = Path("/swapfile-clash-sub.img")
    xui_database: Path = Path("/etc/x-ui/x-ui.db")
    private_root: Path = Path("/var/lib/clash-sub/private")
    public_root: Path = Path("/var/lib/clash-sub/public")

    def stream_conf(self):
        return self.stream_conf_dir / "clash-sub.conf"

    def http_conf(self):
        return self.http_conf_dir / "clash-sub.conf"

    def fullchain(self):
        return self.ssl_dir / "fullchain.pem"

    def privkey(self):
        return self.ssl_dir / "privkey.pem"


@dataclass
class InstallState:
    """Durable install journal: phase progress plus render parameters."""

    schema_version: int = 1
    domain: str = ""
    panel_port: int = 0
    panel_base_path: str = ""
    phases_done: list = field(default_factory=list)
    files_written: list = field(default_factory=list)
    backups: dict = field(default_factory=dict)


def load_install_state(path):
    path = Path(path)
    if not path.exists():
        return InstallState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        state = InstallState(**payload)
    except (OSError, ValueError, TypeError):
        raise InstallerError("install_state_invalid") from None
    if state.schema_version != 1:
        raise InstallerError("install_state_invalid")
    return state


def save_install_state(path, state):
    path = Path(path)
    if not isinstance(state, InstallState):
        raise InstallerError("install_state_invalid")
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % path.name, dir=str(path.parent)
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(json.dumps(asdict(state), sort_keys=True) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if Path(temporary).exists():
            Path(temporary).unlink(missing_ok=True)


class Installer:
    """Phase-driven installer; every external effect goes through ``runner``."""

    def __init__(self, repo_root, *, paths=None, runner=None, print_fn=None):
        self.repo_root = Path(repo_root)
        self.paths = paths or InstallPaths()
        self.runner = runner or subprocess.run
        self.print_fn = print_fn or (lambda message: None)
        self._state_path = self.repo_root / "private" / "install-state.json"

    # -- journal ---------------------------------------------------------
    def state(self):
        return load_install_state(self._state_path)

    def _save_state(self, state):
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        save_install_state(self._state_path, state)

    def _phase_done(self, name, **updates):
        state = self.state()
        if name not in state.phases_done:
            state.phases_done.append(name)
        for key, value in updates.items():
            setattr(state, key, value)
        self._save_state(state)

    # -- phase 0 ---------------------------------------------------------
    def preflight(self, domain):
        if os.geteuid() != 0:
            raise InstallerError("not_root")
        self._require_debian()
        self._require_disk()
        self._require_xui()
        self._require_free_tcp_port(443)
        self._require_dns(domain)
        self._phase_done("preflight")
        return True

    def _require_debian(self):
        try:
            with open(_OS_RELEASE_PATH, encoding="ascii") as handle:
                fields = dict(
                    line.split("=", 1)
                    for line in handle.read().splitlines()
                    if "=" in line
                )
        except OSError:
            raise InstallerError("unsupported_distribution") from None
        if fields.get("ID", "").strip('"') != "debian" or not fields.get(
            "VERSION_ID", ""
        ).strip('"').startswith(_DEBIAN_MAJOR):
            raise InstallerError("unsupported_distribution")

    def _require_disk(self):
        try:
            usage = os.statvfs(self.repo_root)
        except OSError:
            raise InstallerError("disk_space_insufficient") from None
        if usage.f_bavail * usage.f_frsize < _MINIMUM_FREE_BYTES:
            raise InstallerError("disk_space_insufficient")

    def _require_xui(self):
        try:
            read_xui_snapshot(self.paths.xui_database)
            read_panel_port(self.paths.xui_database)
        except XuiCompatibilityError:
            raise InstallerError("xui_incompatible") from None

    def _require_free_tcp_port(self, port):
        _require_free_tcp_port(self, port)

    def _require_dns(self, domain):
        resolved = _resolve_host("sub." + domain)
        local = _local_ipv4(self.runner)
        if not any(address in local for address in resolved):
            raise InstallerError("dns_mismatch")


def _require_free_tcp_port(installer, port):
    probe = socket.socket()
    try:
        probe.bind(("0.0.0.0", port))
    except OSError:
        raise InstallerError("port_443_taken") from None
    finally:
        probe.close()


def _resolve_host(hostname):
    try:
        return sorted(
            {info[4][0] for info in socket.getaddrinfo(hostname, None, socket.AF_INET)}
        )
    except OSError:
        raise InstallerError("dns_mismatch") from None


def _local_ipv4(runner):
    try:
        result = runner(
            ["hostname", "-I"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return result.stdout.decode("ascii", "replace").split()
    except Exception:
        raise InstallerError("dns_mismatch") from None
