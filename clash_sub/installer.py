"""One-shot integration installer for the unified 443 topology."""

import base64
import grp
import json
import os
import re
import socket
import subprocess
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from clash_sub.domain import ServiceConfig
from clash_sub.nginx import (
    NginxError,
    activate_nginx_files,
    render_stream_config,
    render_sub_server,
)
from clash_sub.xui import XuiCompatibilityError, read_panel_settings, read_xui_snapshot

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
    cli_symlink: Path = Path("/usr/local/bin/clash-sub")
    swap_file: Path = Path("/swapfile-clash-sub.img")
    fstab: Path = Path("/etc/fstab")
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
    """Durable install journal: phase progress plus render parameters.

    Schema stays at version 1 across additive changes: new fields always ship
    with defaults so journals written by older installers keep loading, and
    rollback proves file ownership via ``replaced_files``/``files_written``
    instead of touching whatever it finds on disk.
    """

    schema_version: int = 1
    domain: str = ""
    node_host: str = ""
    panel_port: int = 0
    panel_base_path: str = ""
    phases_done: list = field(default_factory=list)
    files_written: list = field(default_factory=list)
    backups: dict = field(default_factory=dict)
    default_site_removed: bool = False
    replaced_files: dict = field(default_factory=dict)
    # Write-ahead provenance, all additive with safe defaults:
    artifact_mutation_started: bool = False
    default_site_removal_intent: bool = False
    nginx_active: bool | None = None  # original service state, captured pre-apt
    nginx_enabled: bool | None = None
    systemd_actions_started: bool = False


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

    def _record_replacement(self, state, path):
        """Remember pre-existing file/symlink so rollback can restore it."""
        path = Path(path)
        key = str(path)
        if (
            key in state.replaced_files
            # Paths already journaled as written are our own files; a re-run
            # must not adopt them as foreign content to "restore".
            or key in state.files_written
            or not (path.exists() or path.is_symlink())
        ):
            return
        if path.is_symlink():
            try:
                target = os.readlink(path)
            except OSError:
                return
            state.replaced_files[key] = {"kind": "symlink", "target": target}
            return
        try:
            content = path.read_bytes()
            mode = path.stat().st_mode & 0o777
        except OSError:
            return
        state.replaced_files[key] = {
            "kind": "file",
            "content": base64.b64encode(content).decode("ascii"),
            "mode": mode,
        }

    # -- phase 0 ---------------------------------------------------------
    def preflight(self, domain, node_host=None):
        if os.geteuid() != 0:
            raise InstallerError("not_root")
        self._require_debian()
        self._require_disk()
        self._require_xui()
        self._require_panel_base_path()
        self._require_free_tcp_port(443)
        self._require_host_resolves_locally("sub." + domain)
        self._require_host_resolves_locally(node_host or ("node." + domain))
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
            read_panel_settings(self.paths.xui_database)
        except XuiCompatibilityError:
            raise InstallerError("xui_incompatible") from None

    def _require_panel_base_path(self):
        _, base_path, listen = self._panel_settings()
        if not re.fullmatch(r"/[A-Za-z0-9_-]+/", base_path):
            raise InstallerError("panel_base_path_required")
        if listen != "127.0.0.1":
            raise InstallerError("panel_listen_unsafe")

    def _require_free_tcp_port(self, port):
        _require_free_tcp_port(self, port)

    def _require_host_resolves_locally(self, host):
        resolved = _resolve_host(host)
        local = _local_ipv4(self.runner)
        if not any(address in local for address in resolved):
            raise InstallerError("dns_mismatch")

    # -- phase 1 ---------------------------------------------------------
    def optimize_low_memory(self, swap_mb):
        self._write_file(self.paths.sysctl_conf, "vm.swappiness=10\n", 0o644)
        self.paths.journald_conf_dir.mkdir(parents=True, exist_ok=True)
        self._write_file(
            self.paths.journald_conf_dir / "99-clash-sub.conf",
            "[Journal]\nSystemMaxUse=50M\n",
            0o644,
        )
        if (
            isinstance(swap_mb, int)
            and not isinstance(swap_mb, bool)
            and swap_mb > 0
        ):
            if not _swap_active(self.paths.swap_file):
                if self.paths.swap_file.exists() or self.paths.swap_file.is_symlink():
                    self.paths.swap_file.unlink(missing_ok=True)
                try:
                    self._run(
                        ["fallocate", "-l", "%sM" % swap_mb, str(self.paths.swap_file)]
                    )
                    self._run(["chmod", "600", str(self.paths.swap_file)])
                    self._run(["mkswap", str(self.paths.swap_file)])
                    self._run(["swapon", str(self.paths.swap_file)])
                    self._write_fstab_entry()
                except Exception:
                    self._run_best_effort(["swapoff", str(self.paths.swap_file)])
                    self.paths.swap_file.unlink(missing_ok=True)
                    raise
            else:
                self._write_fstab_entry()
        self._run(["sysctl", "-p", str(self.paths.sysctl_conf)])
        self._phase_done("low_memory")

    def _write_fstab_entry(self):
        marker = "# clash-sub swap"
        fstab_text = (
            self.paths.fstab.read_text(encoding="utf-8")
            if self.paths.fstab.is_file()
            else ""
        )
        if marker not in fstab_text:
            self._write_file(
                self.paths.fstab,
                fstab_text.rstrip("\n")
                + ("\n" if fstab_text.strip() else "")
                + "%s\n%s none swap sw 0 0\n" % (marker, self.paths.swap_file),
                0o644,
            )

    # -- phase 2 ---------------------------------------------------------
    def install_nginx_packages(self):
        # Write-ahead provenance: this transaction is about to mutate nginx
        # artifacts, and rollback may only stop/disable nginx against the
        # original service state captured here, before apt runs.
        state = self.state()
        state.artifact_mutation_started = True
        state.nginx_active, state.nginx_enabled = self._nginx_service_state()
        self._save_state(state)
        self._run(
            [
                "apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                "nginx",
                "libnginx-mod-stream",
            ]
        )
        if self._remove_default_site_will_proceed():
            # Journal the removal intent before the unlink: if the intent
            # save fails the link must stay untouched.
            state = self.state()
            state.default_site_removal_intent = True
            try:
                self._save_state(state)
            except OSError:
                raise InstallerError("install_state_invalid") from None
            self._remove_default_site()
            state = self.state()
            state.default_site_removed = True
            self._save_state(state)
        self._ensure_stream_include()
        state = self.state()
        if "nginx_packages" not in state.phases_done:
            state.phases_done.append("nginx_packages")
        self._save_state(state)

    def _nginx_service_state(self):
        def query(flag):
            try:
                result = self.runner(
                    ["systemctl", flag, "nginx"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
                return result.returncode == 0
            except Exception:
                return False

        return query("is-active"), query("is-enabled")

    def _remove_default_site(self):
        return self._remove_default_site_at(
            Path("/etc/nginx/sites-enabled/default"),
            Path("/etc/nginx/sites-available/default"),
        )

    def _remove_default_site_will_proceed(self):
        return self._default_site_is_stock_link(
            Path("/etc/nginx/sites-enabled/default"),
            Path("/etc/nginx/sites-available/default"),
        )

    def _default_site_is_stock_link(self, enabled, available):
        try:
            resolved = enabled.resolve(strict=True)
        except OSError:
            return False
        return resolved == available

    def _remove_default_site_at(self, enabled, available):
        if not self._default_site_is_stock_link(enabled, available):
            return False
        try:
            enabled.unlink()
        except OSError:
            raise InstallerError("default_site_removal_failed") from None
        return True

    def _restore_default_site(self):
        return self._restore_default_site_at(
            Path("/etc/nginx/sites-enabled/default"),
            Path("/etc/nginx/sites-available/default"),
        )

    def _restore_default_site_at(self, enabled, available):
        if enabled.exists() or enabled.is_symlink():
            return False
        if not available.is_file():
            return False
        try:
            enabled.symlink_to(available)
        except OSError:
            raise InstallerError("default_site_restore_failed") from None
        return True

    def _ensure_stream_include(self):
        marker = "# clash-sub stream include"
        text = self.paths.nginx_conf.read_text(encoding="utf-8")
        if marker in text:
            return False
        block = (
            "\n%s\nstream {\n    include %s/*.conf;\n}\n"
            % (marker, self.paths.stream_conf_dir)
        )
        self._write_file(
            self.paths.nginx_conf, text.rstrip("\n") + "\n" + block, 0o644
        )
        return True

    # -- phase 3 ---------------------------------------------------------
    def issue_certificate(self, domain, cf_token):
        if not isinstance(domain, str) or not domain.strip():
            raise InstallerError("invalid_domain")
        if not isinstance(cf_token, str) or not cf_token.strip():
            raise InstallerError("missing_cf_token")
        acme = self.paths.acme_home / "acme.sh"
        if not acme.is_file():
            bootstrap = self.repo_root / "private" / "acme-install.sh"
            bootstrap.parent.mkdir(parents=True, exist_ok=True)
            self._run(["curl", "-fsSL", "https://get.acme.sh", "-o", str(bootstrap)])
            self._run(["sh", str(bootstrap), "--home", str(self.paths.acme_home)])
        environment = {"CF_Token": cf_token}
        self._run(
            [
                str(acme),
                "--issue",
                "--dns",
                "dns_cf",
                "-d",
                domain,
                "-d",
                "*." + domain,
                "--keylength",
                "ec-256",
                "--server",
                "letsencrypt",
                "--home",
                str(self.paths.acme_home),
            ],
            env=environment,
        )
        self.paths.ssl_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.ssl_dir, 0o700)
        self._run(
            [
                str(acme),
                "--install-cert",
                "-d",
                domain,
                "--ecc",
                "--fullchain-file",
                str(self.paths.fullchain()),
                "--key-file",
                str(self.paths.privkey()),
                "--reloadcmd",
                "systemctl reload nginx || true",
                "--home",
                str(self.paths.acme_home),
            ],
            env=environment,
        )
        os.chmod(self.paths.privkey(), 0o600)
        self._phase_done("certificate")

    # -- phase 4 ---------------------------------------------------------
    def activate_nginx(self, *, domain, panel_port, panel_base_path):
        base_path = panel_base_path.rstrip("/")
        config = ServiceConfig(
            owner_email="pending",
            subscription_authority="sub.%s:443" % domain,
            xui_public_endpoint="%s:443" % domain,
            xui_database=self.paths.xui_database,
            private_root=self.paths.private_root,
            public_root=self.paths.public_root,
            nginx_routes=self.paths.routes_conf,
            mihomo_binary=Path("/usr/local/lib/clash-sub/mihomo"),
            nginx_binary=Path("/usr/sbin/nginx"),
            systemctl_binary=Path("/usr/bin/systemctl"),
            template_root=self.repo_root / "templates",
        )
        stream = render_stream_config(config, domain)
        sub_server = render_sub_server(
            config,
            domain=domain,
            panel_port=panel_port,
            panel_base_path=base_path,
            routes_include=str(self.paths.routes_conf),
            fullchain=str(self.paths.fullchain()),
            privkey=str(self.paths.privkey()),
        )
        self.paths.stream_conf_dir.mkdir(parents=True, exist_ok=True)
        self.paths.http_conf_dir.mkdir(parents=True, exist_ok=True)
        self.paths.routes_conf.parent.mkdir(parents=True, exist_ok=True)
        # Crash-safety metadata: persist what we are about to replace before
        # the writes run, so rollback can restore rather than delete it.
        state = self.state()
        for path in (
            self.paths.stream_conf(),
            self.paths.http_conf(),
            self.paths.routes_conf,
        ):
            self._record_replacement(state, path)
        self._save_state(state)
        try:
            activate_nginx_files(
                (
                    (self.paths.stream_conf(), stream.encode("utf-8"), 0o640),
                    (self.paths.http_conf(), sub_server.encode("utf-8"), 0o640),
                    (self.paths.routes_conf, b"# clash-sub routes placeholder\n", 0o640),
                ),
                self.runner,
                nginx_binary="/usr/sbin/nginx",
            )
        except NginxError:
            raise InstallerError("nginx_activation_failed") from None
        self._run(["systemctl", "enable", "--now", "nginx"])
        state = self.state()
        if "nginx_activation" not in state.phases_done:
            state.phases_done.append("nginx_activation")
        state.domain = domain
        state.panel_port = panel_port
        state.panel_base_path = base_path
        for path in (
            self.paths.stream_conf(),
            self.paths.http_conf(),
            self.paths.routes_conf,
        ):
            if str(path) not in state.files_written:
                state.files_written.append(str(path))
        self._save_state(state)

    # -- phase 5 ---------------------------------------------------------
    def harden_systemd(self):
        assets = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
        restart_drop_in = (
            self.paths.systemd_dir / "nginx.service.d" / "clash-sub-restart.conf"
        )
        recover_drop_in = (
            self.paths.systemd_dir / "nginx.service.d" / "clash-sub-recover.conf"
        )
        units = [
            self.paths.systemd_dir / unit
            for unit in (
                "clash-sub-traffic.service",
                "clash-sub-traffic.timer",
                "clash-sub-recover.service",
            )
        ]
        # Crash-safety metadata: journal every path this phase is about to
        # touch BEFORE any write runs.  Replacements let rollback restore
        # foreign content; files_written lets it remove our new units even
        # when the crash precedes both the writes and the phase save
        # (unlink of a never-written path is a no-op, replacements win).
        state = self.state()
        for path in (self.paths.cli_symlink, restart_drop_in, *units, recover_drop_in):
            self._record_replacement(state, path)
            if str(path) not in state.files_written:
                state.files_written.append(str(path))
        state.artifact_mutation_started = True
        self._save_state(state)
        self._install_cli_symlink()
        self._write_file(
            restart_drop_in,
            "[Service]\nRestart=on-failure\nRestartSec=2s\n",
            0o644,
        )
        for unit in units:
            self._write_file(
                unit,
                (assets / unit.name).read_text(encoding="utf-8"),
                0o644,
            )
        drop_in_source = assets / "nginx.service.d" / "clash-sub-recover.conf"
        self._write_file(
            recover_drop_in,
            drop_in_source.read_text(encoding="utf-8"),
            0o644,
        )
        # Persist the action intent before the first systemctl call: a crash
        # after the timer is enabled but before the phase save must still
        # disable it during rollback.
        state = self.state()
        state.systemd_actions_started = True
        self._save_state(state)
        self._run(["systemctl", "daemon-reload"])
        self._run(["systemctl", "enable", "--now", "clash-sub-traffic.timer"])
        self._phase_done("systemd_harden")

    def _install_cli_symlink(self):
        target = self.repo_root / "bin" / "clash-sub"
        if not target.is_file():
            raise InstallerError("cli_entry_missing")
        link = self.paths.cli_symlink
        if link.is_symlink() and link.resolve() == target.resolve():
            return False
        if link.exists() or link.is_symlink():
            # Never overwrite a foreign file or symlink; that is data loss
            # the rollback path cannot undo.
            raise InstallerError("cli_symlink_conflict")
        temporary = link.parent / (".%s.tmp" % link.name)
        temporary.unlink(missing_ok=True)
        try:
            temporary.symlink_to(target)
            os.replace(temporary, link)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise InstallerError("cli_symlink_failed") from None
        return True

    # -- phase 6 ---------------------------------------------------------
    def initialize_subscription(self, *, domain, owner_email, node_host=None):
        config_dir = self.repo_root / "private" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        contents = (
            "schema-version: 2\n"
            "owner-email: %s\n"
            "subscription-authority: sub.%s:443\n"
            "xui-public-endpoint: %s:443\n"
            "xui-database: %s\n"
            "private-root: %s\n"
            "public-root: %s\n"
            "nginx-routes: %s\n"
            "mihomo-binary: /usr/local/lib/clash-sub/mihomo\n"
            "nginx-binary: /usr/sbin/nginx\n"
            "systemctl-binary: /usr/bin/systemctl\n"
            "max-source-bytes: 5242880\n"
            % (
                owner_email,
                domain,
                node_host or ("node." + domain),
                self.paths.xui_database,
                self.paths.private_root,
                self.paths.public_root,
                self.paths.routes_conf,
            )
        )
        self._write_file(config_dir / "service.yaml", contents, 0o600)
        self._prepare_runtime_directories()
        self._phase_done("subscription_init")

    def _prepare_runtime_directories(self):
        self.paths.private_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.paths.private_root, 0o700)
        self.paths.public_root.mkdir(parents=True, exist_ok=True)
        try:
            public_gid = grp.getgrnam("www-data").gr_gid
        except KeyError:
            public_gid = -1
        if public_gid != -1:
            os.chown(self.paths.public_root, -1, public_gid)
        os.chmod(self.paths.public_root, 0o2750)
        self.paths.routes_conf.parent.mkdir(parents=True, exist_ok=True)

    # -- phase 7 ---------------------------------------------------------
    def finalize(self):
        self._phase_done("report")
        return self._report(self.state())

    def _report(self, state):
        return {
            "domain": state.domain,
            "panel_url": "https://sub.%s%s/" % (state.domain, state.panel_base_path),
            "subscription_note": "run `clash-sub sync` then `clash-sub links`",
            "gate_instruction": (
                "3x-ui 面板：把 Reality inbound 的 listen 从 0.0.0.0 改为 127.0.0.1"
                "（保持端口 10443），公网仅保留 443。"
            ),
        }

    # -- orchestration ---------------------------------------------------
    def install(
        self, *, domain, cf_token, swap_mb=0, owner_email="owner-example", node_host=None
    ):
        try:
            snapshot = read_xui_snapshot(self.paths.xui_database)
        except XuiCompatibilityError:
            raise InstallerError("xui_incompatible") from None
        matches = [
            client
            for client in snapshot.clients
            if client.enabled and client.email == owner_email
        ]
        if len(matches) != 1:
            raise InstallerError("owner_email_invalid")
        node_host = node_host or ("node." + domain)
        state = self.state()
        if state.domain and state.domain != domain and state.phases_done:
            raise InstallerError("domain_mismatch")
        state.domain = domain
        state.node_host = node_host
        self._save_state(state)
        done = set(state.phases_done)
        phases = (
            ("preflight", lambda: self.preflight(domain, node_host)),
            ("low_memory", lambda: self.optimize_low_memory(swap_mb)),
            ("nginx_packages", self.install_nginx_packages),
            ("certificate", lambda: self.issue_certificate(domain, cf_token)),
            (
                "nginx_activation",
                lambda: self._activate_with_panel(domain),
            ),
            ("systemd_harden", self.harden_systemd),
            (
                "subscription_init",
                lambda: self.initialize_subscription(
                    domain=domain, owner_email=owner_email, node_host=node_host
                ),
            ),
            ("report", self.finalize),
        )
        for name, action in phases:
            if name in done:
                continue
            action()
            self.print_fn("phase %s: done" % name)
        return self._report(self.state())

    def _panel_settings(self):
        try:
            return read_panel_settings(self.paths.xui_database)
        except XuiCompatibilityError:
            raise InstallerError("xui_incompatible") from None

    def _activate_with_panel(self, domain):
        port, base_path, _ = self._panel_settings()
        return self.activate_nginx(
            domain=domain, panel_port=port, panel_base_path=base_path
        )

    # -- rollback --------------------------------------------------------
    def rollback_install(self):
        if not self._state_path.exists():
            return
        state = self.state()
        # Restore-to-original model: every destructive action gates on
        # provenance journaled by THIS transaction.  nginx stop/disable runs
        # only against the pre-apt capture, so a pre-existing nginx keeps
        # running and a fresh install gets stopped and disabled.
        if state.nginx_active is not None:
            if not state.nginx_active:
                self._run_best_effort(["systemctl", "stop", "nginx"])
            if not state.nginx_enabled:
                self._run_best_effort(["systemctl", "disable", "nginx"])
        # Crash-window coverage: paths are journaled write-ahead, but a
        # replacement recorded by an even earlier crash window still wins
        # over deletion; restoring the recorded bytes is idempotent.
        for recorded in dict.fromkeys([*state.files_written, *state.replaced_files]):
            self._rollback_file(Path(recorded), state)
        # Content-evidence actions gate on the transaction's mutation flag:
        # an empty or preflight-only journal must not delete artifacts that
        # merely look like ours.  The sweep itself removes only
        # marker/resolution-verified files and the include removal strips
        # only our verbatim block.
        if state.artifact_mutation_started:
            self._sweep_unjournaled_artifacts(state)
            self._remove_stream_include()
            if state.default_site_removed or state.default_site_removal_intent:
                self._restore_default_site()
        if "systemd_harden" in state.phases_done or state.systemd_actions_started:
            self._run_best_effort(
                ["systemctl", "disable", "--now", "clash-sub-traffic.timer"]
            )
            self._run(["systemctl", "daemon-reload"])
        try:
            (self.repo_root / "private" / "install-state.json").unlink(missing_ok=True)
        except OSError:
            raise InstallerError("rollback_failed") from None

    def _rollback_file(self, path, state):
        recorded = state.replaced_files.get(str(path))
        if recorded is not None:
            if recorded.get("kind") == "symlink":
                # Entries without "kind" are legacy regular-file backups.
                # Dangling targets recreate fine as dangling symlinks.
                path.unlink(missing_ok=True)
                os.symlink(recorded["target"], path)
                return
            self._write_file(
                path,
                None,
                recorded["mode"],
                data=base64.b64decode(recorded["content"]),
            )
        elif path.is_file() or path.is_symlink():
            path.unlink(missing_ok=True)

    def _sweep_unjournaled_artifacts(self, state):
        """Clean crash-window artifacts that carry our content fingerprint.

        Bounded to well-known paths only; the filesystem is never globbed.
        """
        for conf in (self.paths.stream_conf(), self.paths.http_conf()):
            try:
                with conf.open("rb") as handle:
                    first = handle.readline()
            except OSError:
                continue
            if first.strip().startswith(b"# Managed by clash-sub install"):
                self._rollback_file(conf, state)
        link = self.paths.cli_symlink
        if link.is_symlink():
            try:
                link.resolve().relative_to(self.repo_root.resolve())
                link.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass

    def _remove_stream_include(self):
        marker = "# clash-sub stream include"
        path = self.paths.nginx_conf
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8")
        if marker not in text:
            return False
        block = (
            "\n%s\nstream {\n    include %s/*.conf;\n}\n"
            % (marker, self.paths.stream_conf_dir)
        )
        if block not in text:
            # The block was modified after installation; leave it for the operator.
            return False
        self._write_file(path, text.replace(block, ""), 0o644)
        return True

    def _run_best_effort(self, arguments):
        try:
            self._run(arguments)
        except InstallerError:
            pass

    def _write_file(self, path, contents, mode, data=None):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=".%s." % path.name, dir=str(path.parent)
        )
        try:
            os.fchmod(descriptor, mode)
            if data is None:
                with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                    output.write(contents)
                    output.flush()
                    os.fsync(output.fileno())
            else:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
            os.replace(temporary, path)
        finally:
            if Path(temporary).exists():
                Path(temporary).unlink(missing_ok=True)

    def _run(self, arguments, env=None):
        try:
            result = self.runner(
                list(arguments),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=600,
                check=False,
                env=dict(os.environ, **env) if env else None,
            )
        except Exception:
            raise InstallerError("command_failed") from None
        if result.returncode != 0:
            raise InstallerError("command_failed")
        return result


def _require_free_tcp_port(installer, port):
    probe = socket.socket()
    try:
        probe.bind(("0.0.0.0", port))
    except OSError:
        raise InstallerError("port_443_taken") from None
    finally:
        probe.close()


def _swap_active(swap_file):
    """True when the swap file is listed in /proc/swaps."""
    try:
        with open("/proc/swaps", encoding="ascii") as handle:
            return any(
                line.split() and line.split()[0] == str(swap_file)
                for line in handle.read().splitlines()[1:]
            )
    except OSError:
        return False


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
