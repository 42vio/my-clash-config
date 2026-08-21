"""Read-only preflight for a clean 3x-ui REALITY host.

`run_preflight()` inspects an already-configured server through a
`CommandRunner` that can only answer bounded, timeout-limited read
commands, capped file reads, stat calls, DNS resolutions, and one
environment variable.  It never mutates the host, and the report is
strictly allowlisted: booleans, versions, counts, ports, and stable
codes.  Xray client IDs, REALITY keys, server names, raw paths,
source URLs, process command lines, and configuration fragments are
never serialized.

The Xray JSON is read from the path configured in the service
settings and parsed entirely in memory.  The private-root ownership
checks use the host-side ``private`` tree that compose.yaml
bind-mounts into the container private root (uid/gid 10001, 0700
directories, 0600 config files).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Sequence, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    from clash_sub.settings import SettingsError, _parse_service_settings
except ImportError:  # pragma: no cover - in-tree runs always have clash_sub
    _parse_service_settings = None
    SettingsError = ValueError


MAX_OUTPUT_BYTES = 256 * 1024
COMMAND_TIMEOUT_SECONDS = 15.0

# xray itself runs as a child of the x-ui service in this design; both
# unit names are still probed so a stray standalone xray unit is visible.
REQUIRED_XUI_UNITS = ("x-ui", "3x-ui")
OBSERVED_UNITS = ("xray", "nginx", "docker")
LEGACY_UNITS = ("trojan", "trojan-web", "mariadb", "portainer")
ALL_UNITS = REQUIRED_XUI_UNITS + OBSERVED_UNITS + LEGACY_UNITS

EXPECTED_COMPOSE_SERVICES = ("manager", "publisher", "subconverter", "validator")
ALLOWED_PUBLIC_PORTS = (22, 80, 443, 8443)

PRIVATE_UID = 10001
PRIVATE_GID = 10001
PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600
PRIVATE_SUBDIRS = ("config", "staging", "releases", "current", "logs", "sources")
PRIVATE_CONFIG_FILES = ("config/service.yaml", "config/users.yaml")

# Task 12 installs the project-owned Nginx files under a path that
# carries this marker; anything else listening on 80/8443 is unmanaged.
NGINX_PROJECT_MARKER = "clash-sub"

# Ordered (check name, blocking code) pairs; the report exposes every
# check as a boolean and every failed check as its stable code.
CHECK_SPEC = (
    ("os_supported", "os_unsupported"),
    ("xui_service_active", "xui_service_inactive"),
    ("panel_version_matches", "panel_version_mismatch"),
    ("xray_version_matches", "xray_version_mismatch"),
    ("tcp_443_xray_owned", "tcp_443_not_xray"),
    ("udp_443_closed", "udp_443_open"),
    ("panel_loopback", "panel_not_loopback"),
    ("subscription_loopback", "subscription_not_loopback"),
    ("reality_inbound_present", "reality_inbound_missing"),
    ("clients_present", "clients_missing"),
    ("client_flow_consistent", "client_flow_inconsistent"),
    ("server_names_nonempty", "server_names_empty"),
    ("short_ids_nonempty", "short_ids_empty"),
    ("no_legacy_services", "legacy_service_present"),
    ("no_unexpected_public_listeners", "unexpected_public_listener"),
    ("docker_available", "docker_unavailable"),
    ("compose_available", "compose_unavailable"),
    ("compose_config_valid", "compose_config_invalid"),
    ("ufw_safe", "ufw_unsafe"),
    ("dns_matches", "dns_mismatch"),
    ("private_root_owned", "private_root_ownership"),
    ("nginx_ok", "nginx_conflict"),
)


class PreflightError(RuntimeError):
    """Raised for unusable settings or refused commands."""


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str


@dataclass(frozen=True)
class StatInfo:
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class Listener:
    proto: str
    address: str
    port: int
    process: Optional[str]

    @property
    def loopback(self) -> bool:
        return (
            self.address == "127.0.0.1"
            or self.address == "::1"
            or self.address.startswith("127.")
        )


@dataclass(frozen=True)
class XraySummary:
    reality_inbound_count: int
    client_count: int
    flow_consistent: bool
    server_names_nonempty: bool
    short_ids_nonempty: bool


@dataclass(frozen=True)
class PreflightReport:
    checks: Mapping[str, bool]
    facts: Mapping[str, object]
    blocking_codes: List[str]
    notes: List[str]

    @property
    def ok(self) -> bool:
        return not self.blocking_codes

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "checks": dict(self.checks),
            "facts": dict(self.facts),
            "blocking_codes": list(self.blocking_codes),
            "notes": list(self.notes),
        }


_NEVER_RUN_PROGRAMS = frozenset(
    {
        "apt", "apt-get", "dpkg", "snap", "yum", "dnf", "pip", "pip3",
        "npm", "yarn", "brew",
        "rm", "mv", "cp", "ln", "touch", "mkdir", "rmdir", "unlink",
        "truncate", "tee", "dd", "shred", "split",
        "chmod", "chown", "chgrp", "chattr", "setfacl", "install",
        "mount", "umount", "swapoff", "swapon", "mkfs", "mkswap",
        "fdisk", "sfdisk", "parted",
        "reboot", "shutdown", "halt", "poweroff", "suspend", "hibernate",
        "kill", "killall", "pkill", "passwd", "useradd", "usermod",
        "userdel", "groupadd", "chpasswd",
        "iptables", "ip6tables", "nft", "firewall-cmd", "sysctl",
        "modprobe", "insmod", "rmmod",
        "bash", "sh", "dash", "zsh", "ksh", "csh", "tcsh", "env",
        "nohup", "xargs", "timeout", "sudo", "su", "doas",
        "curl", "wget", "nc", "ncat", "socat", "telnet",
        "ssh", "scp", "sftp", "rsync", "git", "make", "systemd-run",
        "crontab", "echo", "printf", "yes", "sed", "awk", "perl",
        "python", "python3",
    }
)

_SAFE_SYSTEMCTL_VERBS = frozenset(
    {
        "is-active",
        "is-enabled",
        "is-failed",
        "show",
        "status",
        "cat",
        "help",
        "list-units",
        "list-unit-files",
        "list-sockets",
    }
)


def is_mutating_command(argv) -> bool:
    """True unless argv is one of the bounded read-only probes issued here.

    The classifier is deliberately conservative: unknown programs,
    shells, downloaders, and package managers all count as mutating.
    """
    if not isinstance(argv, (list, tuple)) or not argv:
        return True
    program = argv[0]
    if not isinstance(program, str) or not program:
        return True
    name = Path(program).name
    verb = argv[1] if len(argv) > 1 and isinstance(argv[1], str) else ""
    if name in ("uname", "ss"):
        return False
    if name == "systemctl":
        return verb not in _SAFE_SYSTEMCTL_VERBS
    if name == "ufw":
        return not verb.startswith("status")
    if name == "docker":
        if verb in ("version", "--version"):
            return False
        if verb == "compose" and len(argv) > 2 and argv[2] in ("version", "config"):
            return False
        return True
    if name == "nginx":
        return verb not in ("-T", "-t", "-v", "-V")
    if name == "x-ui":
        return verb not in ("--version", "version", "-v")
    if name in _NEVER_RUN_PROGRAMS:
        return True
    if len(argv) == 2 and verb in ("version", "--version", "-v"):
        # Configured binary version probes (e.g. the bundled xray).
        return False
    return True


class CommandRunner:
    """Base class recording every issued command."""

    def __init__(self):
        self.commands: List[List[str]] = []

    @property
    def mutating_command_seen(self) -> bool:
        return any(is_mutating_command(command) for command in self.commands)

    def run(self, argv, timeout: float = COMMAND_TIMEOUT_SECONDS) -> RunResult:
        raise NotImplementedError

    def read_text(self, path) -> Optional[str]:
        raise NotImplementedError

    def stat(self, path) -> Optional[StatInfo]:
        raise NotImplementedError

    def resolve_host(self, host: str) -> Tuple[str, ...]:
        raise NotImplementedError

    def getenv(self, name: str) -> Optional[str]:
        raise NotImplementedError

    def euid(self) -> int:
        raise NotImplementedError


class SubprocessRunner(CommandRunner):
    """Live read-only runner: bounded timeout-limited commands without
    a shell; command stdout and file reads are capped at 256 KiB each
    (stderr is spooled to a temp file and never read)."""

    def __init__(self, root=ROOT):
        super().__init__()
        self.root = Path(root)

    def run(self, argv, timeout: float = COMMAND_TIMEOUT_SECONDS) -> RunResult:
        self.commands.append(list(argv))
        if is_mutating_command(argv):
            raise PreflightError("refused non-read command: %s" % Path(argv[0]).name)
        with tempfile.TemporaryFile(mode="w+b") as out, tempfile.TemporaryFile(
            mode="w+b"
        ) as err:
            try:
                completed = subprocess.run(
                    list(argv),
                    cwd=str(self.root),
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=err,
                    timeout=timeout,
                    check=False,
                )
                returncode = completed.returncode
            except FileNotFoundError:
                return RunResult(127, "")
            except subprocess.TimeoutExpired:
                return RunResult(124, "")
            out.seek(0)
            stdout = out.read(MAX_OUTPUT_BYTES).decode("utf-8", errors="replace")
        return RunResult(returncode, stdout)

    def read_text(self, path) -> Optional[str]:
        try:
            with open(path, "rb") as handle:
                data = handle.read(MAX_OUTPUT_BYTES)
        except OSError:
            return None
        return data.decode("utf-8", errors="replace")

    def stat(self, path) -> Optional[StatInfo]:
        try:
            info = os.stat(path)
        except OSError:
            return None
        return StatInfo(
            uid=info.st_uid, gid=info.st_gid, mode=info.st_mode & 0o777
        )

    def resolve_host(self, host: str) -> Tuple[str, ...]:
        try:
            infos = socket.getaddrinfo(host, None)
        except OSError:
            return ()
        addresses: List[str] = []
        for info in infos:
            address = info[4][0]
            if address not in addresses:
                addresses.append(address)
        return tuple(addresses)

    def getenv(self, name: str) -> Optional[str]:
        return os.environ.get(name)

    def euid(self) -> int:
        return os.geteuid()


class FixtureRunner(CommandRunner):
    """Replays a saved JSON snapshot of runner outputs (fixture mode)."""

    def __init__(self, fixture, root=ROOT, xray_config_path=None):
        super().__init__()
        self.fixture = fixture
        self.root = Path(root)
        self.xray_config_path = (
            str(xray_config_path) if xray_config_path is not None else None
        )

    def run(self, argv, timeout: float = COMMAND_TIMEOUT_SECONDS) -> RunResult:
        self.commands.append(list(argv))
        return self._replay(list(argv))

    def _replay(self, argv) -> RunResult:
        program = Path(argv[0]).name
        if program == "uname":
            return RunResult(0, str(self.fixture.get("arch", "")))
        if program == "ss":
            return RunResult(0, "\n".join(self.fixture.get("listeners", [])))
        if program == "systemctl":
            return self._replay_systemctl(argv)
        if program == "x-ui":
            return self._optional_output("panel_version_output")
        if program == "nginx":
            dump = self.fixture.get("nginx_dump")
            if isinstance(dump, str) and dump:
                return RunResult(0, dump)
            return RunResult(1, "")
        if program == "ufw":
            status = self.fixture.get("ufw_status_output")
            if isinstance(status, str):
                return RunResult(0, status)
            return RunResult(127, "")
        if program == "docker":
            return self._replay_docker(argv)
        if len(argv) == 2 and argv[1] in ("version", "--version"):
            return self._optional_output("xray_version_output")
        return RunResult(1, "")

    def _replay_systemctl(self, argv) -> RunResult:
        if len(argv) >= 3 and argv[1] == "is-active":
            state = self.fixture.get("services", {}).get(argv[2], "unknown")
            return RunResult(0 if state == "active" else 3, state)
        if len(argv) >= 3 and argv[1] == "show":
            properties = self.fixture.get("unit_properties", {}).get(argv[2], {})
            lines = []
            for argument in argv[3:]:
                if argument.startswith("--property="):
                    for name in argument.split("=", 1)[1].split(","):
                        lines.append("%s=%s" % (name, properties.get(name, "")))
            return RunResult(0, "\n".join(lines) + ("\n" if lines else ""))
        return RunResult(1, "")

    def _replay_docker(self, argv) -> RunResult:
        if len(argv) >= 3 and argv[1] == "compose":
            if argv[2] == "version":
                return self._optional_output("compose_version_output")
            if argv[2] == "config":
                return self._optional_output("compose_config_output")
            return RunResult(1, "")
        return self._optional_output("docker_version_output")

    def _optional_output(self, key: str) -> RunResult:
        value = self.fixture.get(key)
        if isinstance(value, str) and value:
            return RunResult(0, value)
        return RunResult(1, "")

    def read_text(self, path) -> Optional[str]:
        key = str(path)
        files = self.fixture.get("files") or {}
        if key in files:
            return files[key]
        if self.xray_config_path is not None and key == self.xray_config_path:
            config = self.fixture.get("xray_config")
            if config is None:
                return None
            return json.dumps(config)
        if key == "/etc/os-release":
            release = self.fixture.get("os_release")
            return release if isinstance(release, str) else None
        return None

    def stat(self, path) -> Optional[StatInfo]:
        paths = self.fixture.get("paths") or {}
        candidate = Path(path)
        relative = None
        try:
            relative = candidate.relative_to(self.root)
        except ValueError:
            try:
                relative = candidate.resolve().relative_to(self.root.resolve())
            except ValueError:
                relative = None
        key = relative.as_posix() if relative is not None else str(candidate)
        entry = paths.get(key)
        if not isinstance(entry, dict):
            return None
        return StatInfo(
            uid=int(entry.get("uid", -1)),
            gid=int(entry.get("gid", -1)),
            mode=int(entry.get("mode", 0)),
        )

    def resolve_host(self, host: str) -> Tuple[str, ...]:
        answers = self.fixture.get("dns", {}).get(host)
        if isinstance(answers, list):
            return tuple(str(answer) for answer in answers)
        return ()

    def getenv(self, name: str) -> Optional[str]:
        value = self.fixture.get("environment", {}).get(name)
        return str(value) if value is not None else None

    def euid(self) -> int:
        return int(self.fixture.get("euid", 0))


_LISTEN_PROCESS_RE = re.compile(r'users:\(\("([^"]+)"')
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
_OS_RELEASE_QUOTED_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_NGINX_FILE_HEADER_RE = re.compile(r"^# configuration file (\S+):\s*$")
_NGINX_LISTEN_RE = re.compile(
    r"\blisten\s+(?:\[[^\]]*\]:)?(?:\d{1,3}(?:\.\d{1,3}){3}:)?(\d+)"
)
_UFW_STATUS_RE = re.compile(r"^Status:\s*(\S+)", re.MULTILINE)
_UFW_RULE_RE = re.compile(r"^\[\s*\d+\]\s+(\S+)", re.MULTILINE)


def run_preflight(runner: CommandRunner, settings, root=None) -> PreflightReport:
    """Evaluate every blocking prerequisite without touching the host."""
    repo_root = Path(root) if root is not None else ROOT
    checks: dict = {}
    facts: dict = {}
    notes: List[str] = []

    # Operating system and architecture.
    release_text = runner.read_text(Path("/etc/os-release")) or ""
    release = _parse_os_release(release_text)
    arch = runner.run(["uname", "-m"]).stdout.strip()
    facts["os_id"] = release.get("ID")
    facts["os_version"] = release.get("VERSION_ID")
    facts["arch"] = arch or None
    checks["os_supported"] = (
        release.get("ID") == "debian"
        and release.get("VERSION_ID") == "12"
        and arch == "x86_64"
    )

    # Privilege level: reported, never blocking on its own.
    running_as_root = runner.euid() == 0
    facts["running_as_root"] = running_as_root
    if not running_as_root:
        notes.append("non_root_execution")

    # Service states.
    states = {
        unit: runner.run(["systemctl", "is-active", unit]).stdout.strip()
        for unit in ALL_UNITS
    }
    checks["xui_service_active"] = any(
        states.get(unit) == "active" for unit in REQUIRED_XUI_UNITS
    )
    checks["no_legacy_services"] = not any(
        states.get(unit) == "active" for unit in LEGACY_UNITS
    )

    # Panel and Xray versions.
    panel_output = _panel_version_output(runner)
    xray_output = runner.run([str(settings.xui.xray_binary_path), "version"]).stdout
    panel_version = _first_version(panel_output)
    xray_version = _first_version(xray_output)
    facts["panel_version"] = panel_version
    facts["xray_version"] = xray_version
    facts["expected_panel_version"] = settings.xui.expected_panel_version
    facts["expected_xray_version"] = settings.xui.expected_xray_version
    checks["panel_version_matches"] = (
        panel_version is not None
        and panel_version == settings.xui.expected_panel_version
    )
    checks["xray_version_matches"] = (
        xray_version is not None
        and xray_version == settings.xui.expected_xray_version
    )

    # Current SSH connection port (number only) and the public ports it
    # legitimizes on top of the fixed allowlist.
    ssh_port = _ssh_port(runner.getenv("SSH_CONNECTION"))
    facts["ssh_port"] = ssh_port
    allowed_public = set(ALLOWED_PUBLIC_PORTS)
    if ssh_port is not None:
        allowed_public.add(ssh_port)

    # Listening sockets.
    listeners = _parse_listeners(runner.run(["ss", "-H", "-lntup"]).stdout)
    public_listeners = [item for item in listeners if not item.loopback]
    facts["public_listener_ports"] = sorted({item.port for item in public_listeners})
    public_port = settings.reality.public_port
    tcp_public = [
        item for item in listeners if item.proto == "tcp" and item.port == public_port
    ]
    checks["tcp_443_xray_owned"] = (
        len(tcp_public) == 1
        and not tcp_public[0].loopback
        and _is_xray_process(tcp_public[0].process)
    )
    checks["udp_443_closed"] = not any(
        item.proto == "udp" and item.port == public_port for item in listeners
    )
    panel_listeners = [
        item
        for item in listeners
        if item.proto == "tcp" and item.port == settings.xui.panel_port
    ]
    subscription_listeners = [
        item
        for item in listeners
        if item.proto == "tcp" and item.port == settings.xui.subscription_port
    ]
    checks["panel_loopback"] = bool(panel_listeners) and all(
        item.loopback for item in panel_listeners
    )
    checks["subscription_loopback"] = bool(subscription_listeners) and all(
        item.loopback for item in subscription_listeners
    )
    checks["no_unexpected_public_listeners"] = all(
        item.port in allowed_public for item in public_listeners
    )

    # Selected Xray configuration, parsed in memory.
    config_text = runner.read_text(settings.xui.xray_config_path)
    summary: Optional[XraySummary] = None
    if config_text is not None:
        try:
            document = json.loads(config_text)
        except ValueError:
            document = None
        if isinstance(document, dict):
            summary = summarize_xray_config(
                document,
                required_flow=settings.reality.required_flow,
                public_port=public_port,
            )
    if summary is None:
        summary = XraySummary(
            reality_inbound_count=0,
            client_count=0,
            flow_consistent=False,
            server_names_nonempty=False,
            short_ids_nonempty=False,
        )
    facts["reality_inbound_count"] = summary.reality_inbound_count
    facts["client_count"] = summary.client_count
    checks["reality_inbound_present"] = summary.reality_inbound_count == 1
    checks["clients_present"] = summary.client_count >= 1
    checks["client_flow_consistent"] = summary.flow_consistent
    checks["server_names_nonempty"] = summary.server_names_nonempty
    checks["short_ids_nonempty"] = summary.short_ids_nonempty

    # Docker / Compose availability and the pinned service set.
    checks["docker_available"] = runner.run(["docker", "--version"]).returncode == 0
    checks["compose_available"] = (
        runner.run(["docker", "compose", "version"]).returncode == 0
    )
    compose_config = runner.run(["docker", "compose", "config", "--format", "json"])
    compose_services = None
    if compose_config.returncode == 0:
        try:
            parsed = json.loads(compose_config.stdout)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and isinstance(parsed.get("services"), dict):
            compose_services = set(parsed["services"])
    checks["compose_config_valid"] = compose_services == set(
        EXPECTED_COMPOSE_SERVICES
    )

    # Firewall state.
    ufw_state, ufw_ports, ufw_unscoped = _evaluate_ufw(
        runner.run(["ufw", "status", "numbered"])
    )
    facts["ufw_state"] = ufw_state
    ufw_safe = True
    if ufw_state == "absent":
        notes.append("ufw_absent")
    elif ufw_state == "inactive":
        notes.append("ufw_inactive")
    elif ufw_state == "active":
        # An "Anywhere" To column allows every port for one source and
        # any rule outside the expected public ports is unattributed.
        if ufw_unscoped or any(port not in allowed_public for port in ufw_ports):
            ufw_safe = False
        else:
            notes.append("ufw_active_clean")
    else:
        ufw_safe = False
    checks["ufw_safe"] = ufw_safe

    # Domain DNS vs the configured public address.
    if settings.publication.mode == "domain":
        authorities = (
            _authority_host(settings.publication.subscription_authority),
            _authority_host(settings.publication.panel_authority),
        )
        checks["dns_matches"] = all(
            settings.reality.public_address in runner.resolve_host(host)
            for host in authorities
        )
    else:
        checks["dns_matches"] = True
        notes.append("ip_mode_no_dns_check")

    # Private-root bind-mount ownership (compose uid/gid 10001 tree).
    checks["private_root_owned"] = _private_root_owned(runner, repo_root / "private")

    # Nginx: absent is fine before installation; any 443 listener or an
    # unmanaged 80/8443 listener conflicts with the project layout.
    nginx_state = _evaluate_nginx(runner.run(["nginx", "-T"]))
    facts["nginx_state"] = nginx_state
    if nginx_state == "absent":
        notes.append("nginx_absent")
    checks["nginx_ok"] = nginx_state != "conflict"

    blocking = [code for name, code in CHECK_SPEC if not checks.get(name, False)]
    return PreflightReport(
        checks=checks, facts=facts, blocking_codes=blocking, notes=notes
    )


def summarize_xray_config(document, required_flow: str, public_port: int) -> XraySummary:
    """Reduce a parsed Xray config to counts and booleans, nothing else."""
    inbounds = document.get("inbounds") if isinstance(document, dict) else None
    if not isinstance(inbounds, list):
        return XraySummary(0, 0, False, False, False)
    reality_inbounds = []
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            continue
        stream = inbound.get("streamSettings")
        if not isinstance(stream, dict):
            continue
        if inbound.get("protocol") != "vless":
            continue
        if stream.get("network") != "tcp":
            continue
        if stream.get("security") != "reality":
            continue
        if inbound.get("port") != public_port:
            continue
        reality_inbounds.append(inbound)
    client_count = 0
    flow_consistent = True
    server_names_nonempty = False
    short_ids_nonempty = False
    for inbound in reality_inbounds:
        settings_block = inbound.get("settings")
        clients = (
            settings_block.get("clients")
            if isinstance(settings_block, dict)
            else None
        )
        if isinstance(clients, list):
            for client in clients:
                if isinstance(client, dict):
                    client_count += 1
                    if client.get("flow") != required_flow:
                        flow_consistent = False
        stream = inbound.get("streamSettings") or {}
        reality = stream.get("realitySettings")
        if not isinstance(reality, dict):
            continue
        names = reality.get("serverNames")
        if isinstance(names, list) and any(
            isinstance(name, str) and name.strip() for name in names
        ):
            server_names_nonempty = True
        short_ids = reality.get("shortIds")
        if isinstance(short_ids, list) and any(
            isinstance(identifier, str) and identifier.strip()
            for identifier in short_ids
        ):
            short_ids_nonempty = True
    return XraySummary(
        reality_inbound_count=len(reality_inbounds),
        client_count=client_count,
        flow_consistent=flow_consistent,
        server_names_nonempty=server_names_nonempty,
        short_ids_nonempty=short_ids_nonempty,
    )


def load_service_settings(path):
    """Strictly parse the service settings; minimal fallback in-tree."""
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as error:
        raise PreflightError("settings_unreadable: %s" % error)
    if not isinstance(document, dict):
        raise PreflightError("settings_invalid: root must be a mapping")
    if _parse_service_settings is not None:
        try:
            return _parse_service_settings(document)
        except SettingsError as error:
            raise PreflightError("settings_invalid: %s" % error)
    return _minimal_service_settings(document)


def _minimal_service_settings(document):
    """Same keys as clash_sub.settings, duck-typed for standalone runs."""
    from types import SimpleNamespace

    try:
        publication = document["publication"]
        reality = document["reality"]
        xui = document["xui"]
        return SimpleNamespace(
            publication=SimpleNamespace(
                mode=str(publication["mode"]),
                subscription_authority=str(publication["subscription-authority"]),
                panel_authority=str(publication["panel-authority"]),
            ),
            reality=SimpleNamespace(
                public_address=str(reality["public-address"]),
                public_port=int(reality["public-port"]),
                required_flow=str(reality["required-flow"]),
            ),
            xui=SimpleNamespace(
                panel_port=int(xui["panel-port"]),
                subscription_port=int(xui["subscription-port"]),
                xray_config_path=Path(xui["xray-config-path"]),
                xray_binary_path=Path(xui["xray-binary-path"]),
                expected_panel_version=str(xui["expected-panel-version"]),
                expected_xray_version=str(xui["expected-xray-version"]),
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise PreflightError("settings_invalid: %s" % error)


def _parse_os_release(text: str) -> dict:
    fields = {}
    for line in text.splitlines():
        match = _OS_RELEASE_QUOTED_RE.match(line.strip())
        if not match:
            continue
        fields[match.group(1)] = match.group(2).strip().strip('"')
    return fields


def _parse_listeners(ss_text: str) -> List[Listener]:
    listeners = []
    for line in ss_text.splitlines():
        fields = line.split()
        if len(fields) < 6:
            continue
        # ss reports IPv6 sockets as tcp6/udp6; a dual-stack Go listener
        # (Xray with an empty listen address) shows up only as tcp6.
        proto = fields[0].lower().rstrip("6")
        if proto not in ("tcp", "udp"):
            continue
        address, separator, port_text = fields[4].rpartition(":")
        if not separator or not port_text.isdigit():
            continue
        address = address.strip("[]") or "*"
        process_match = _LISTEN_PROCESS_RE.search(line)
        listeners.append(
            Listener(
                proto=proto,
                address=address,
                port=int(port_text),
                process=process_match.group(1) if process_match else None,
            )
        )
    return listeners


def _is_xray_process(process: Optional[str]) -> bool:
    """Exact-token match: "notxray" or "xrayevil" must not pass."""
    if not process:
        return False
    name = process.lower()
    return name == "xray" or name.startswith("xray ") or name.startswith("xray-")


def _ssh_port(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    fields = value.split()
    if len(fields) != 4 or not fields[3].isdigit():
        return None
    return int(fields[3])


def _authority_host(authority: str) -> str:
    return authority.rsplit(":", 1)[0]


def _first_version(text: Optional[str]) -> Optional[str]:
    match = _VERSION_RE.search(text or "")
    return match.group(0) if match else None


def _panel_version_output(runner: CommandRunner) -> str:
    direct = runner.run(["x-ui", "--version"])
    if direct.returncode == 0 and direct.stdout.strip():
        return direct.stdout
    # Fallback only: the packaged unit exposes its Version property.
    shown = runner.run(["systemctl", "show", "x-ui", "--property=Version"])
    return shown.stdout


def _evaluate_ufw(result: RunResult) -> Tuple[str, Sequence[int], bool]:
    """Return (state, rule ports, whether any rule allows all ports)."""
    if result.returncode == 127 or not result.stdout.strip():
        return "absent", (), False
    match = _UFW_STATUS_RE.search(result.stdout)
    if match is None:
        return "unknown", (), False
    state = match.group(1).lower()
    if state != "active":
        return ("inactive" if state == "inactive" else "unknown"), (), False
    ports = []
    unscoped = False
    for rule in _UFW_RULE_RE.finditer(result.stdout):
        token = rule.group(1).split("/")[0]
        if token.isdigit():
            port = int(token)
            if port not in ports:
                ports.append(port)
        else:
            # e.g. "Anywhere" in the To column: every port is allowed.
            unscoped = True
    return "active", ports, unscoped


def _evaluate_nginx(dump: RunResult) -> str:
    if dump.returncode != 0 or not dump.stdout.strip():
        return "absent"
    conflict = False
    current_file = ""
    for line in dump.stdout.splitlines():
        header = _NGINX_FILE_HEADER_RE.match(line)
        if header:
            current_file = header.group(1)
            continue
        # Scan the directive text only: a trailing comment such as
        # "# used to be listen 443" must not create a phantom listener.
        code = line.split("#", 1)[0]
        match = _NGINX_LISTEN_RE.search(code)
        if not match:
            continue
        port = int(match.group(1))
        managed = NGINX_PROJECT_MARKER in current_file
        if port == 443:
            conflict = True
        elif port in (80, 8443) and not managed:
            conflict = True
    return "conflict" if conflict else "present"


def _private_root_owned(runner: CommandRunner, private_root: Path) -> bool:
    directories = [private_root] + [private_root / name for name in PRIVATE_SUBDIRS]
    for directory in directories:
        info = runner.stat(directory)
        if (
            info is None
            or info.uid != PRIVATE_UID
            or info.gid != PRIVATE_GID
            or info.mode != PRIVATE_DIR_MODE
        ):
            return False
    for name in PRIVATE_CONFIG_FILES:
        info = runner.stat(private_root / name)
        if (
            info is None
            or info.uid != PRIVATE_UID
            or info.gid != PRIVATE_GID
            or info.mode != PRIVATE_FILE_MODE
        ):
            return False
    return True


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only clean-host preflight for the pinned 3x-ui REALITY stack."
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=ROOT / "config" / "service.example.yaml",
        help="service settings YAML (default: the repository example)",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=None,
        help="replay a saved JSON snapshot of runner outputs instead of the live host",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _print_human(report: PreflightReport) -> None:
    print("server preflight")
    for name, code in CHECK_SPEC:
        state = "yes" if report.checks.get(name) else "no"
        print("  %-32s %s" % (name + ":", state))
    print("  facts:")
    for key in sorted(report.facts):
        print("    %-30s %s" % (key + ":", report.facts[key]))
    if report.blocking_codes:
        print("  blocking codes: %s" % ", ".join(report.blocking_codes))
    else:
        print("  blocking codes: none")
    if report.notes:
        print("  notes: %s" % ", ".join(report.notes))
    print("  result: %s" % ("OK" if report.ok else "BLOCKED"))


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        settings = load_service_settings(args.config)
    except PreflightError as error:
        print(str(error), file=sys.stderr)
        return 2
    if args.fixture is not None:
        try:
            fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print("fixture_unreadable: %s" % error, file=sys.stderr)
            return 2
        if not isinstance(fixture, dict):
            print("fixture_unreadable: root must be an object", file=sys.stderr)
            return 2
        runner = FixtureRunner(
            fixture, root=ROOT, xray_config_path=str(settings.xui.xray_config_path)
        )
    else:
        runner = SubprocessRunner(root=ROOT)
    report = run_preflight(runner, settings)
    if args.json:
        print(json.dumps(report.to_json(), sort_keys=True))
    else:
        _print_human(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
