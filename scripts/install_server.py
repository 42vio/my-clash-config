"""Deterministic dry-run/apply server installer.

Default mode is strictly read-only: strict settings load, the full
read-only preflight, in-memory rendering of every intended host file,
and a redacted action list.  ``--apply`` executes the same action
list: preflight gate, backups, packages, a pinned certbot venv, the
ACME Nginx file, certificate issuance, the final TLS Nginx file and
certificate timers, the private-root tree for uid/gid 10001, the
pinned Compose stack with loopback health checks, the host command
symlink, and UFW last (verified SSH port first, then default deny).

Every external command is an argv list with a timeout and bounded
output; failures surface as stable codes only.  Any failure after
backup restores every project-owned file (nginx -t gates any rollback
reload) and leaves user data, 3x-ui, Xray, DNS, and unrelated Nginx
files untouched.  Package installation is reported as
non-reversible: rollback restores configuration and service state
only.
"""

from __future__ import annotations

import argparse
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import uuid
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml

from clash_sub.host_cli import HELP_TEXT
from clash_sub.settings import SettingsError, _parse_service_settings

_preflight_spec = importlib.util.spec_from_file_location(
    "clash_sub_install_server_preflight",
    str(ROOT / "scripts" / "server_preflight.py"),
)
server_preflight = importlib.util.module_from_spec(_preflight_spec)
sys.modules[_preflight_spec.name] = server_preflight
_preflight_spec.loader.exec_module(server_preflight)

run_preflight = server_preflight.run_preflight
RunResult = server_preflight.RunResult
_evaluate_ufw = server_preflight._evaluate_ufw
_ssh_port_from_env = server_preflight._ssh_port

PINNED_REPO_PATH = Path("/opt/clash-sub")
DEFAULT_CONFIG_PATH = PINNED_REPO_PATH / "private" / "config" / "service.yaml"
COMMAND_TIMEOUT_SECONDS = 900.0
FAST_TIMEOUT_SECONDS = 60.0
MAX_OUTPUT_BYTES = 256 * 1024

CERTBOT_VERSION = "5.7.0"
CERTBOT_VENV = "/opt/certbot"
ACME_WEBROOT = "/var/lib/clash-sub/acme"
BACKUP_ROOT = "/var/backups/clash-sub"
HOST_COMMAND_PATH = "/usr/local/bin/clash-sub"
PRIVATE_UID = "10001"
PRIVATE_GID = "10001"
PRIVATE_DIR_MODE = "700"
PRIVATE_SUBDIRS = ("config", "staging", "releases", "current", "logs", "sources")
PACKAGES = (
    "docker.io",
    "docker-compose-v2",
    "nginx",
    "ufw",
    "python3-venv",
    "curl",
    "ca-certificates",
)
UFW_PUBLIC_TCP = ("80/tcp", "443/tcp", "8443/tcp")
COMPOSE_FILE = ROOT / "compose.yaml"
# Debian's nginx package ships a default site whose "listen 80
# default_server" collides with the ACME server; bootstrap must remove
# it (recorded in the backup inventory so rollback restores it).
DEFAULT_SITE_PATH = "/etc/nginx/sites-enabled/default"

# Installed Nginx files carry the literal project marker so the
# post-install preflight attributes their listeners to this project.
NGINX_FILE_TARGETS = {
    "acme": "/etc/nginx/conf.d/clash-sub-00-acme-http.conf",
    "tls": "/etc/nginx/conf.d/clash-sub-10-tls.conf",
}
SYSTEMD_UNITS = (
    "clash-sub-cert-renew.service",
    "clash-sub-cert-renew.timer",
    "clash-sub-cert-check.service",
    "clash-sub-cert-check.timer",
    "clash-sub-cert-renew-failed.service",
)
SYSTEMD_TARGET_DIR = "/etc/systemd/system"
DEPLOY_HOOK_PATH = "/etc/letsencrypt/renewal-hooks/deploy/clash-sub-nginx-reload.sh"
DEPLOY_HOOK_CONTENT = (
    "#!/bin/sh\n"
    "# Managed by the local installer. Do not edit.\n"
    "# Reload only when the configuration still validates.\n"
    "nginx -t && systemctl reload nginx\n"
)

_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z0-9_]+\}\}")
_SSHD_LISTEN_RE = r"\S+:%d\b"


class InstallerError(RuntimeError):
    """Stable-code failure; never carries secrets or paths."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


# ----------------------------------------------------------------------
# Nginx template rendering


def render_template(text: str, mapping: Dict[str, str]) -> str:
    """Substitute {{PLACEHOLDER}} tokens strictly, leaving none behind."""

    def replace(match):
        key = match.group(0)
        if key not in mapping:
            raise InstallerError("template_placeholder_missing")
        return str(mapping[key])

    rendered = _PLACEHOLDER_RE.sub(replace, text)
    if _PLACEHOLDER_RE.search(rendered):
        raise InstallerError("template_placeholder_unresolved")
    return rendered


def authority_host(authority: str) -> str:
    return authority.rsplit(":", 1)[0]


def nginx_template_context(service) -> Dict[str, str]:
    fullchain = service.certificate.fullchain_path
    if service.publication.mode == "ip":
        panel_name = authority_host(service.publication.panel_authority)
        sub_name = panel_name
    else:
        panel_name = authority_host(service.publication.panel_authority)
        sub_name = authority_host(service.publication.subscription_authority)
    return {
        "{{FULLCHAIN_PATH}}": str(fullchain),
        "{{PRIVKEY_PATH}}": str(fullchain.parent / "privkey.pem"),
        "{{PANEL_SERVER_NAME}}": panel_name,
        "{{SUB_SERVER_NAME}}": sub_name,
        "{{PANEL_BASE_PATH}}": service.xui.panel_base_path,
        "{{PANEL_UPSTREAM}}": "%s:%d"
        % (service.xui.panel_listen, service.xui.panel_port),
        "{{PUBLISHER_UPSTREAM}}": "%s:%d"
        % (
            service.publication.publisher_listen,
            service.publication.publisher_port,
        ),
    }


def tls_template_name(mode: str) -> str:
    return "10-clash-ip.conf.tmpl" if mode == "ip" else "10-clash-domain.conf.tmpl"


def render_nginx_files(service) -> Dict[str, str]:
    """Return installed-path -> rendered content for both Nginx files."""
    acme_text = (ROOT / "deploy" / "nginx" / "00-acme-http.conf.tmpl").read_text(
        encoding="utf-8"
    )
    tls_text = (
        ROOT / "deploy" / "nginx" / tls_template_name(service.publication.mode)
    ).read_text(encoding="utf-8")
    return {
        NGINX_FILE_TARGETS["acme"]: render_template(acme_text, {}),
        NGINX_FILE_TARGETS["tls"]: render_template(
            tls_text, nginx_template_context(service)
        ),
    }


def certbot_argv(certbot_bin: str, service, webroot: str) -> List[str]:
    """Validated settings-derived argv; never shell interpolation."""
    cert_name = service.certificate.fullchain_path.parent.name
    argv = [
        certbot_bin,
        "certonly",
        "--webroot",
        "--webroot-path",
        webroot,
        "--non-interactive",
        "--agree-tos",
        "--email",
        service.certificate.acme_email,
    ]
    if service.publication.mode == "ip":
        argv += [
            "--preferred-profile",
            "shortlived",
            "--ip-address",
            authority_host(service.publication.panel_authority),
            "--cert-name",
            cert_name,
        ]
    else:
        argv += [
            "--cert-name",
            cert_name,
            "-d",
            authority_host(service.publication.panel_authority),
            "-d",
            authority_host(service.publication.subscription_authority),
        ]
    return argv


# ----------------------------------------------------------------------
# Runners


class SystemRunner:
    """Production runner: argv lists, timeouts, bounded output, no shell.

    Commands always run with the repository root as the working
    directory so a bare ``docker compose config`` preflight probe does
    not depend on the caller's cwd.  Failed commands record their
    (bounded) stderr for the private failure log; it is never printed.
    """

    def __init__(self):
        self.commands: List[Tuple[str, ...]] = []
        self.failures: List[Tuple[str, str]] = []

    @property
    def mutating_command_seen(self) -> bool:
        return any(
            server_preflight.is_mutating_command(command) for command in self.commands
        )

    def run(self, argv, timeout: float = COMMAND_TIMEOUT_SECONDS, env=None) -> RunResult:
        argv = [str(item) for item in argv]
        self.commands.append(tuple(argv))
        environment = dict(os.environ)
        if env:
            environment.update(env)
        try:
            completed = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=environment,
                cwd=str(ROOT),
            )
        except FileNotFoundError:
            return RunResult(127, "")
        except subprocess.TimeoutExpired:
            return RunResult(124, "")
        if completed.returncode != 0:
            self.failures.append(
                (
                    os.path.basename(argv[0]),
                    (completed.stderr or "")[:4000],
                )
            )
        return RunResult(
            completed.returncode, (completed.stdout or "")[:MAX_OUTPUT_BYTES]
        )

    def read_text(self, path) -> Optional[str]:
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_OUTPUT_BYTES)
        except OSError:
            return None
        return data.decode("utf-8", errors="replace")

    def stat(self, path):
        try:
            info = os.stat(path)
        except OSError:
            return None
        return server_preflight.StatInfo(
            uid=info.st_uid, gid=info.st_gid, mode=info.st_mode & 0o777
        )

    def resolve_host(self, host: str) -> Tuple[str, ...]:
        return server_preflight.SubprocessRunner.resolve_host(self, host)

    def getenv(self, name: str) -> Optional[str]:
        return os.environ.get(name)

    def euid(self) -> int:
        return os.geteuid()


# ----------------------------------------------------------------------
# Action model shared by dry-run printing and apply execution


@dataclass(frozen=True)
class Action:
    code: str
    label: str
    argv: Tuple[Tuple[str, ...], ...] = ()
    writes: Tuple[Tuple[str, int, str], ...] = ()
    dirs: Tuple[str, ...] = ()
    removes: Tuple[str, ...] = ()
    symlink: Optional[Tuple[str, str]] = None
    env: Optional[Dict[str, str]] = None


class Layout:
    """Maps absolute host paths onto an injected filesystem root."""

    def __init__(self, root: Optional[Path]):
        self.root = Path(root).resolve() if root is not None else None

    def p(self, path) -> Path:
        target = Path(path)
        if self.root is None:
            return target
        try:
            target.relative_to(self.root)
            return target
        except ValueError:
            return self.root / str(target).lstrip("/")


def _operation_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return "%s-%s" % (stamp, uuid.uuid4().hex[:6])


def _load_settings(path: Path):
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise InstallerError("settings_unreadable")
    if not isinstance(document, dict):
        raise InstallerError("settings_invalid")
    try:
        return _parse_service_settings(document)
    except SettingsError:
        raise InstallerError("settings_invalid")


def build_actions(context) -> List[Action]:
    """Resolve every intended mutation into a static action list."""
    service = context["service"]
    layout: Layout = context["layout"]
    runner = context["runner"]
    ssh_port = context["ssh_port"]
    mode = service.publication.mode

    nginx_files = render_nginx_files(service)
    certbot_bin = str(layout.p("%s/bin/certbot" % CERTBOT_VENV))
    venv_python = str(layout.p("%s/bin/python" % CERTBOT_VENV))
    fullchain = layout.p(service.certificate.fullchain_path)

    is_active_nginx = (
        runner.run(["systemctl", "is-active", "nginx"], timeout=FAST_TIMEOUT_SECONDS)
        .stdout.strip()
        == "active"
    )
    nginx_start = (
        ("systemctl", "start", "nginx")
        if not is_active_nginx
        else ("systemctl", "reload", "nginx")
    )

    ufw_state, ufw_ports, _unscoped = _evaluate_ufw(
        runner.run(["ufw", "status", "numbered"], timeout=FAST_TIMEOUT_SECONDS)
    )
    if ufw_state == "active":
        approved = {ssh_port, 80, 443, 8443}
        if set(ufw_ports) != approved:
            raise InstallerError("ufw_conflict")

    actions: List[Action] = [
        Action(
            code="package_install",
            label="install required debian packages",
            argv=(
                (
                    "apt-get",
                    "install",
                    "-y",
                    "--no-install-recommends",
                    *PACKAGES,
                ),
            ),
            env={"DEBIAN_FRONTEND": "noninteractive"},
        ),
        Action(
            code="certbot_venv",
            label="create the pinned certbot virtualenv",
            argv=(
                ("python3", "-m", "venv", str(layout.p(CERTBOT_VENV))),
                (
                    venv_python,
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "certbot==%s" % CERTBOT_VERSION,
                ),
            ),
        ),
        Action(
            code="acme_nginx",
            label="install the acme http server and validate nginx",
            dirs=(ACME_WEBROOT,),
            writes=((NGINX_FILE_TARGETS["acme"], 0o644, nginx_files[NGINX_FILE_TARGETS["acme"]]),),
            removes=(DEFAULT_SITE_PATH,),
            argv=(("nginx", "-t"), nginx_start),
        ),
    ]

    if not fullchain.exists():
        actions.append(
            Action(
                code="certificate_issuance",
                label="issue the certificate (mode=%s)" % mode,
                argv=(tuple(certbot_argv(certbot_bin, service, ACME_WEBROOT)),),
            )
        )

    unit_writes = tuple(
        (
            "%s/%s" % (SYSTEMD_TARGET_DIR, name),
            0o644,
            (ROOT / "deploy" / "systemd" / name).read_text(encoding="utf-8"),
        )
        for name in SYSTEMD_UNITS
    )
    actions.append(
        Action(
            code="tls_nginx",
            label="install the tls configuration and certificate timers",
            writes=(
                (NGINX_FILE_TARGETS["tls"], 0o644, nginx_files[NGINX_FILE_TARGETS["tls"]]),
                (DEPLOY_HOOK_PATH, 0o755, DEPLOY_HOOK_CONTENT),
                *unit_writes,
            ),
            argv=(
                ("nginx", "-t"),
                ("systemctl", "reload", "nginx"),
                ("systemctl", "daemon-reload"),
                (
                    "systemctl",
                    "enable",
                    "--now",
                    "clash-sub-cert-renew.timer",
                ),
                (
                    "systemctl",
                    "enable",
                    "--now",
                    "clash-sub-cert-check.timer",
                ),
            ),
        )
    )

    if mode == "ip":
        actions.append(
            Action(
                code="ip_mode_requirements",
                label="verify short-lived certificate renewal prerequisites",
                argv=(
                    (
                        "openssl",
                        "x509",
                        "-in",
                        str(fullchain),
                        "-noout",
                        "-checkend",
                        "86400",
                    ),
                    (certbot_bin, "renew", "--dry-run", "--quiet"),
                ),
            )
        )

    private_root = Path(service.private_root)
    provision = [
        "install",
        "-d",
        "-o",
        PRIVATE_UID,
        "-g",
        PRIVATE_GID,
        "-m",
        PRIVATE_DIR_MODE,
        str(private_root),
    ] + [str(private_root / name) for name in PRIVATE_SUBDIRS]
    chmod_argv: Tuple[Tuple[str, ...], ...] = ()
    config_files = [
        str(private_root / "config" / name)
        for name in ("service.yaml", "users.yaml")
    ]
    existing = [path for path in config_files if Path(path).exists()]
    if existing:
        chmod_argv = (("chmod", "600", *existing),)
    actions.append(
        Action(
            code="private_root",
            label="provision the private tree for the application user",
            argv=(tuple(provision),) + chmod_argv,
        )
    )

    compose = str(COMPOSE_FILE)
    actions.append(
        Action(
            code="compose",
            label="validate, build, and start the pinned loopback stack",
            argv=(
                ("docker", "compose", "-f", compose, "config", "--quiet"),
                ("docker", "compose", "-f", compose, "build"),
                (
                    "docker",
                    "compose",
                    "-f",
                    compose,
                    "up",
                    "-d",
                    "subconverter",
                    "publisher",
                ),
                ("curl", "-fsS", "--max-time", "5", "http://127.0.0.1:25500/version"),
                ("curl", "-fsS", "--max-time", "5", "http://127.0.0.1:25501/healthz"),
            ),
        )
    )

    actions.append(
        Action(
            code="host_command",
            label="install the host command symlink",
            symlink=(HOST_COMMAND_PATH, str(ROOT / "bin" / "clash-sub")),
        )
    )

    if ufw_state != "active":
        actions.append(
            Action(
                code="ufw_reset",
                label="reset the inactive firewall before adding rules",
                argv=(("ufw", "--force", "reset"),),
            )
        )
    actions.extend(
        [
            Action(
                code="ufw_ssh",
                label="allow the verified ssh port first",
                argv=(("ufw", "allow", "%d/tcp" % ssh_port),),
            ),
            Action(
                code="ufw_public",
                label="allow the public tcp ports",
                argv=tuple(
                    ("ufw", "allow", port) for port in UFW_PUBLIC_TCP
                ),
            ),
            Action(
                code="ufw_defaults",
                label="deny incoming and allow outgoing",
                argv=(
                    ("ufw", "default", "deny", "incoming"),
                    ("ufw", "default", "allow", "outgoing"),
                ),
            ),
            Action(
                code="ufw_enable",
                label="enable the firewall",
                argv=(("ufw", "--force", "enable"),),
            ),
        ]
    )
    return actions


# ----------------------------------------------------------------------
# Apply machinery: backups, atomic writes, rollback


@dataclass
class ApplyJournal:
    layout: Layout
    runner: object = None
    backup_dir: Optional[Path] = None
    entries: List[dict] = field(default_factory=list)
    created_dirs: List[Path] = field(default_factory=list)
    created_paths: List[Path] = field(default_factory=list)
    nginx_was_active: bool = False
    nginx_started: bool = False
    timers_enabled: bool = False

    def note_dir(self, path: Path) -> None:
        if not path.exists():
            self.created_dirs.append(path)

    def ensure_dir(self, path: Path) -> None:
        """Create a directory tree, recording every new ancestor."""
        missing = []
        probe = Path(path)
        while not probe.exists():
            missing.append(probe)
            if probe.parent == probe:
                break
            probe = probe.parent
        for directory in reversed(missing):
            try:
                directory.mkdir(parents=False, exist_ok=True)
                self.created_dirs.append(directory)
            except OSError:
                pass

    def backup(self, targets: List[str]) -> None:
        inventory = []
        for host_path in targets:
            target = self.layout.p(host_path)
            entry = {"path": host_path, "kind": "absent", "mode": 0}
            if target.is_symlink():
                entry = {
                    "path": host_path,
                    "kind": "link",
                    "target": os.readlink(target),
                    "mode": 0,
                }
            elif target.exists():
                entry = {
                    "path": host_path,
                    "kind": "file",
                    "mode": target.stat().st_mode & 0o777,
                }
                copy = self.backup_dir / ("%d.copy" % len(inventory))
                copy.write_bytes(target.read_bytes())
                entry["copy"] = copy.name
            inventory.append(entry)
        self.entries = inventory
        manifest = self.backup_dir / "inventory.json"
        _atomic_write(
            manifest,
            0o600,
            json.dumps(
                {"operation": "install", "entries": inventory}, sort_keys=True
            ),
        )

    def rollback(self) -> None:
        for entry in reversed(self.entries):
            target = self.layout.p(entry["path"])
            try:
                if entry["kind"] == "absent":
                    if target.is_symlink() or target.exists():
                        if target.is_dir() and not target.is_symlink():
                            continue
                        target.unlink()
                elif entry["kind"] == "link":
                    if target.exists() or target.is_symlink():
                        if not target.is_dir() or target.is_symlink():
                            target.unlink()
                    os.symlink(entry["target"], target)
                else:
                    copy = self.backup_dir / entry["copy"]
                    if copy.exists():
                        _atomic_write(target, entry["mode"], copy.read_text())
            except OSError:
                pass
        for path in self.created_paths:
            try:
                if path.exists() and not path.is_dir():
                    path.unlink()
            except OSError:
                pass
        # Timers we enabled must not keep firing against rolled-back
        # files (the checker would alert certificate_unreadable).
        if self.timers_enabled:
            for timer in ("clash-sub-cert-renew.timer", "clash-sub-cert-check.timer"):
                self.runner.run(
                    ["systemctl", "disable", "--now", timer],
                    timeout=FAST_TIMEOUT_SECONDS,
                )
            self.runner.run(
                ["systemctl", "daemon-reload"], timeout=FAST_TIMEOUT_SECONDS
            )
        # Any reload during rollback happens only when the restored
        # configuration still validates.
        check = self.runner.run(["nginx", "-t"], timeout=FAST_TIMEOUT_SECONDS)
        if check.returncode == 0:
            if self.nginx_was_active:
                self.runner.run(
                    ["systemctl", "reload", "nginx"], timeout=FAST_TIMEOUT_SECONDS
                )
            elif self.nginx_started:
                self.runner.run(
                    ["systemctl", "stop", "nginx"], timeout=FAST_TIMEOUT_SECONDS
                )
        # The backup directory deliberately survives a failed apply:
        # it holds the inventory plus the private failure log an
        # operator needs to diagnose what happened, so its ancestors
        # are never removed either.
        for directory in reversed(self.created_dirs):
            if self.backup_dir is not None and (
                directory == self.backup_dir
                or self.backup_dir.is_relative_to(directory)
            ):
                continue
            try:
                directory.rmdir()
            except OSError:
                pass


def _atomic_write(path: Path, mode: int, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile

    temp_fd, temp_name = tempfile.mkstemp(
        prefix="%s." % path.name, suffix=".tmp", dir=str(path.parent), text=True
    )
    try:
        os.fchmod(temp_fd, mode)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.close(temp_fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _execute_action(action: Action, journal: ApplyJournal) -> None:
    for directory in action.dirs:
        journal.ensure_dir(journal.layout.p(directory))
    for host_path, mode, content in action.writes:
        target = journal.layout.p(host_path)
        journal.ensure_dir(target.parent)
        if not target.exists():
            journal.created_paths.append(target)
        _atomic_write(target, mode, content)
    for host_path in action.removes:
        # Only project-recognized conflicts (the stock Debian default
        # site); the backup inventory restores whatever was here.
        target = journal.layout.p(host_path)
        try:
            if target.is_symlink() or (target.exists() and not target.is_dir()):
                target.unlink()
        except OSError:
            pass
    if action.symlink is not None:
        host_path, link_target = action.symlink
        target = journal.layout.p(host_path)
        journal.ensure_dir(target.parent)
        if not target.exists() and not target.is_symlink():
            journal.created_paths.append(target)
        if target.exists() or target.is_symlink():
            if not target.is_dir() or target.is_symlink():
                target.unlink()
        os.symlink(link_target, target)
    for argv in action.argv:
        result = journal.runner.run(
            list(argv),
            timeout=COMMAND_TIMEOUT_SECONDS,
            env=action.env,
        )
        if len(argv) > 2 and argv[:2] == ("systemctl", "start") and argv[2] == "nginx":
            journal.nginx_started = True
        if (
            len(argv) > 2
            and argv[:2] == ("systemctl", "enable")
            and "clash-sub-cert-renew.timer" in argv
        ):
            journal.timers_enabled = True
        if result.returncode != 0:
            raise InstallerError("%s_failed" % action.code)


def _verify_ssh_port(runner, ssh_port: int, environment_port) -> None:
    if environment_port is None or environment_port != ssh_port:
        raise InstallerError("ssh_port_mismatch")
    listeners = runner.run(["ss", "-H", "-ltnp"], timeout=FAST_TIMEOUT_SECONDS).stdout
    sshd_seen = any(
        re.search(_SSHD_LISTEN_RE % ssh_port, line) and "sshd" in line
        for line in listeners.splitlines()
    )
    if not sshd_seen:
        raise InstallerError("ssh_port_mismatch")


def _print_dry_run(actions: List[Action], out) -> None:
    out.write("install dry-run (no changes will be made)\n")
    out.write("  backup: existing target files to the backup root\n")
    for action in actions:
        out.write("  %s: %s\n" % (action.code, action.label))
    out.write("  verify: re-run preflight and health checks\n")
    out.write("result: DRY-RUN OK (no changes were made)\n")


def _apply(context, actions: List[Action], out, err) -> int:
    layout: Layout = context["layout"]
    runner = context["runner"]
    service = context["service"]

    journal = ApplyJournal(layout=layout, runner=runner)
    journal.nginx_was_active = (
        runner.run(["systemctl", "is-active", "nginx"], timeout=FAST_TIMEOUT_SECONDS)
        .stdout.strip()
        == "active"
    )

    targets = [
        NGINX_FILE_TARGETS["acme"],
        NGINX_FILE_TARGETS["tls"],
        DEPLOY_HOOK_PATH,
        HOST_COMMAND_PATH,
        DEFAULT_SITE_PATH,
    ] + ["%s/%s" % (SYSTEMD_TARGET_DIR, name) for name in SYSTEMD_UNITS]

    operation_id = _operation_id()
    backup_root = layout.p(BACKUP_ROOT)
    journal.backup_dir = backup_root / operation_id
    journal.ensure_dir(journal.backup_dir)
    journal.backup(targets)

    # An issued certificate is deliberately NOT tracked for removal:
    # deleting only the live symlinks would leave a half-deleted
    # certbot lineage that blocks re-issuance on a retry apply, and a
    # retained certificate is both harmless and idempotent.

    try:
        for action in actions:
            _execute_action(action, journal)
        verification = run_preflight(
            runner, service, root=ROOT if layout.root is None else layout.root
        )
        if not verification.ok:
            raise InstallerError("verification_failed")
        if service.publication.mode == "ip":
            timer = runner.run(
                ["systemctl", "is-active", "clash-sub-cert-renew.timer"],
                timeout=FAST_TIMEOUT_SECONDS,
            )
            if timer.returncode != 0:
                raise InstallerError("ip_mode_requirements_failed")
    except (InstallerError, OSError) as error:
        code = error.code if isinstance(error, InstallerError) else "filesystem_error"
        if not isinstance(error, InstallerError):
            failures = getattr(runner, "failures", None)
            if isinstance(failures, list):
                failures.append(("python", "exception: %r" % (error,)))
        journal.rollback()
        _write_failure_log(journal, code)
        err.write("install: error=%s rolled_back=yes\n" % code)
        err.write(
            "install: packages already installed are not removed by rollback\n"
        )
        err.write(
            "install: diagnostics written to the private backup root\n"
        )
        return 1

    out.write(HELP_TEXT)
    out.write("verification: preflight-ok firewall-enabled nginx-reloaded\n")
    out.write("verification: compose-healthy backup-id=%s\n" % operation_id)
    return 0


def _write_failure_log(journal: ApplyJournal, code: str) -> None:
    """Record bounded command diagnostics at mode 0600, never stdout."""
    if journal.backup_dir is None:
        return
    try:
        lines = ["error=%s" % code]
        failures = getattr(journal.runner, "failures", None) or []
        for program, detail in list(failures)[:16]:
            lines.append("%s: %s" % (program, str(detail)[:2000]))
        _atomic_write(journal.backup_dir / "failure.log", 0o600, "\n".join(lines))
    except OSError:
        pass


def main(argv=None, root=None, runner=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic dry-run/apply installer for the publication stack."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--ssh-port", type=int, default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))

    try:
        service = _load_settings(args.config)
    except InstallerError as error:
        sys.stderr.write("install: error=%s\n" % error.code)
        return 2

    if args.apply and root is None and ROOT.resolve() != PINNED_REPO_PATH:
        sys.stderr.write("install: error=repo_path_mismatch\n")
        return 1

    current_runner = runner if runner is not None else SystemRunner()
    environment_port = _ssh_port_from_env(current_runner.getenv("SSH_CONNECTION"))

    if args.apply:
        if args.ssh_port is None:
            sys.stderr.write("install: error=ssh_port_required\n")
            return 2
        try:
            _verify_ssh_port(current_runner, args.ssh_port, environment_port)
        except InstallerError as error:
            sys.stderr.write("install: error=%s\n" % error.code)
            return 1

    context = {
        "service": service,
        "layout": Layout(root),
        "runner": current_runner,
        "ssh_port": args.ssh_port or environment_port or 22,
    }

    # The read-only preflight gates both modes: a dry-run preview must
    # show the same blocking codes an apply would stop on.
    report = run_preflight(
        current_runner,
        service,
        root=ROOT if context["layout"].root is None else context["layout"].root,
    )
    if not report.ok:
        codes = ",".join(report.blocking_codes)
        if args.apply:
            sys.stderr.write("install: error=preflight_blocked codes=%s\n" % codes)
        else:
            sys.stdout.write("install dry-run (no changes will be made)\n")
            sys.stdout.write("  preflight: blocked codes=%s\n" % codes)
            sys.stdout.write("result: DRY-RUN BLOCKED (no changes were made)\n")
        return 1

    try:
        actions = build_actions(context)
    except InstallerError as error:
        sys.stderr.write("install: error=%s\n" % error.code)
        return 1

    if not args.apply:
        _print_dry_run(actions, sys.stdout)
        return 0
    return _apply(context, actions, sys.stdout, sys.stderr)


def run_installer(argv, root=None, runner=None) -> SimpleNamespace:
    """Test-friendly wrapper with captured streams."""
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        returncode = main(list(argv), root=root, runner=runner)
    return SimpleNamespace(
        returncode=returncode, stdout=stdout.getvalue(), stderr=stderr.getvalue()
    )


if __name__ == "__main__":
    sys.exit(main())
