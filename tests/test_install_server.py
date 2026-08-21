"""Installer dry-run, ordering, and rollback tests.

`scripts/install_server.py` is driven here against a temporary
filesystem root and a fake command runner that replays preflight
fixtures and simulates apply-side commands.  The default mode is
strictly read-only; ``--apply`` opens the verified SSH port before
enabling default-deny, rolls every project-owned file back on any
failure, and never prints domains, panel paths, tokens, or the
certificate email.
"""

import copy
import importlib.util
import json
import os
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def load_script(name):
    module_name = "scripts_%s" % name
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "scripts" / ("%s.py" % name)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


server_preflight = load_script("server_preflight")
install_server = load_script("install_server")

run_installer = install_server.run_installer
RunResult = server_preflight.RunResult

SSH_PORT = 26019

BASE_FIXTURE = json.loads(
    (FIXTURES / "preflight-clean.json").read_text(encoding="utf-8")
)
BASE_FIXTURE["environment"]["SSH_CONNECTION"] = (
    "198.51.100.99 51234 192.0.2.10 %d" % SSH_PORT
)
BASE_FIXTURE["listeners"] = [
    line.replace("0.0.0.0:22", "0.0.0.0:%d" % SSH_PORT)
    for line in BASE_FIXTURE["listeners"]
]
BASE_FIXTURE["ufw_status_output"] = "Status: inactive\n"

UFW_ACTIVE_APPROVED = (
    "Status: active\n\n"
    "To                         Action      From\n"
    "--                         ------      ----\n"
    "[ 1] %d/tcp                  ALLOW IN    Anywhere\n"
    "[ 2] 80/tcp                    ALLOW IN    Anywhere\n"
    "[ 3] 443/tcp                  ALLOW IN    Anywhere\n"
    "[ 4] 8443/tcp                 ALLOW IN    Anywhere\n" % SSH_PORT
)


def service_document(root, mode="domain"):
    cert_name = "192.0.2.10" if mode == "ip" else "clash-sub-domain"
    fullchain = str(
        Path(root) / "etc" / "letsencrypt" / "live" / cert_name / "fullchain.pem"
    )
    if mode == "ip":
        subscription_authority = panel_authority = "192.0.2.10:8443"
    else:
        subscription_authority = "sub.example.com:8443"
        panel_authority = "panel.example.com:8443"
    return {
        "schema-version": 1,
        "private-root": str(root / "private"),
        "converter-base-url": "http://127.0.0.1:25500",
        "publication": {
            "mode": mode,
            "subscription-authority": subscription_authority,
            "panel-authority": panel_authority,
            "publisher-listen": "127.0.0.1",
            "publisher-port": 25501,
        },
        "reality": {
            "public-address": "192.0.2.10",
            "public-port": 443,
            "required-flow": "xtls-rprx-vision",
        },
        "xui": {
            "panel-listen": "127.0.0.1",
            "panel-port": 2053,
            "panel-base-path": "/example-random-panel-path/",
            "subscription-listen": "127.0.0.1",
            "subscription-port": 2096,
            "xray-config-path": "/usr/local/x-ui/bin/config.json",
            "xray-binary-path": "/usr/local/x-ui/bin/xray-linux-amd64",
            "expected-panel-version": "3.6.0",
            "expected-xray-version": "26.6.27",
        },
        "certificate": {
            "fullchain-path": fullchain,
            "acme-email": "admin@example.com",
            "alert-before-seconds": 259200 if mode == "ip" else 1209600,
            "alert-command": ["notify-command", "--channel", "private"]
            if mode == "ip"
            else [],
        },
    }


class FakeRunner(server_preflight.FixtureRunner):
    """Fixture-backed preflight runner plus canned apply answers."""

    def __init__(self, fixture=None, fail_on=(), root=ROOT, ufw_status=None):
        prepared = copy.deepcopy(fixture or BASE_FIXTURE)
        if ufw_status is not None:
            prepared["ufw_status_output"] = ufw_status
        super().__init__(
            prepared,
            root=root,
            xray_config_path="/usr/local/x-ui/bin/config.json",
        )
        self.fail_on = tuple(fail_on)
        self.apply_root = Path(root)
        self.commands = []
        self.failures = []

    def run(self, argv, timeout=None, env=None):
        argv = [str(item) for item in argv]
        self.commands.append(tuple(argv))
        if self.fail_on and _is_ordered_subsequence(
            self.fail_on, [os.path.basename(argv[0])] + argv[1:]
        ):
            program = os.path.basename(argv[0])
            self.failures.append(
                (program, "simulated failure output for %s" % program)
            )
            return RunResult(1, "")
        name = os.path.basename(argv[0])
        if name == "openssl":
            return RunResult(0, "notAfter=Mon Jan  1 00:00:00 2030 GMT\n")
        if name == "certbot":
            if "certonly" in argv:
                self._issue_certificate(argv)
                return RunResult(0, "")
            if "renew" in argv:
                return RunResult(0, "")
        if name in ("apt-get", "curl", "install", "chmod"):
            return RunResult(0, "")
        if name in ("python3", "python", "pip", "pip3"):
            return RunResult(0, "")
        if name == "systemctl":
            if argv[1] == "is-active" and argv[2].endswith(".timer"):
                return RunResult(0, "active")
            if argv[1] == "is-active":
                return self._replay_systemctl(argv)
            return RunResult(0, "")
        if name == "ufw":
            if argv[1] == "status":
                return RunResult(0, self.fixture.get("ufw_status_output") or "")
            return RunResult(0, "")
        if name == "nginx":
            return RunResult(0, "")
        if name == "docker":
            if len(argv) > 2 and argv[1] == "compose" and argv[2] in (
                "version",
                "config",
            ):
                return self._replay(argv)
            return RunResult(0, "")
        return self._replay(argv)

    def _issue_certificate(self, argv):
        cert_name = argv[argv.index("--cert-name") + 1]
        directory = self.apply_root / "etc" / "letsencrypt" / "live" / cert_name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "fullchain.pem").write_text(
            "-----BEGIN CERTIFICATE-----\nSYNTHETIC\n", encoding="utf-8"
        )
        (directory / "privkey.pem").write_text(
            "-----BEGIN PRIVATE KEY-----\nSYNTHETIC\n", encoding="utf-8"
        )

    def index(self, command):
        return self.commands.index(command)

    def has(self, command):
        return command in self.commands


def snapshot(root):
    root = Path(root)
    state = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            state[relative] = ("link", os.readlink(path))
        elif path.is_dir():
            state[relative] = ("dir", "")
        else:
            state[relative] = ("file", path.read_text(errors="replace"))
    return state


def snapshot_without_backups(root):
    """Snapshot minus the backup root area, which failed applies keep
    (inventory plus the private failure log) on purpose."""
    return {
        key: value
        for key, value in snapshot(root).items()
        if key != "var" and not key.startswith("var/backups")
    }


def _is_ordered_subsequence(needle, haystack):
    position = 0
    for item in haystack:
        if position < len(needle) and item == needle[position]:
            position += 1
    return position == len(needle)


class InstallerTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.base = Path(self._tempdir.name)
        self.config_dir = self.base / "config-source"
        self.config_dir.mkdir()
        import yaml

        self.yaml = yaml

    def write_config(self, mode="domain"):
        document = service_document(self.base / "root", mode)
        path = self.config_dir / ("service-%s.yaml" % mode)
        path.write_text(
            self.yaml.safe_dump(document, sort_keys=False), encoding="utf-8"
        )
        return path

    def arguments(self, *extra, mode="domain"):
        return [
            "--config",
            str(self.write_config(mode)),
            "--ssh-port",
            str(SSH_PORT),
            *extra,
        ]

    def empty_root(self):
        root = self.base / "root"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def installed_root(self):
        root = self.empty_root()
        conf_d = root / "etc" / "nginx" / "conf.d"
        conf_d.mkdir(parents=True)
        (conf_d / "unrelated.conf").write_text("unrelated\n", encoding="utf-8")
        (conf_d / "clash-sub-00-acme-http.conf").write_text("old-acme\n", "utf-8")
        return root

    # ------------------------------------------------------------------
    # dry run

    def test_default_mode_is_read_only_and_writes_nothing(self):
        root = self.empty_root()
        runner = FakeRunner(root=root)
        result = run_installer(self.arguments(), root=root, runner=runner)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(list(root.rglob("*")), [])
        self.assertFalse(runner.mutating_command_seen)

    def test_dry_run_prints_redacted_action_list(self):
        root = self.empty_root()
        result = run_installer(
            self.arguments(), root=root, runner=FakeRunner(root=root)
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("dry-run", result.stdout)
        self.assertIn("nginx", result.stdout)
        for forbidden in (
            "example.com",
            "admin@example",
            "example-random-panel-path",
            "notify-command",
            "fullchain.pem",
        ):
            self.assertNotIn(forbidden, result.stdout + result.stderr)

    def test_dry_run_runs_the_read_only_preflight_and_reports_blocking_codes(self):
        root = self.empty_root()
        fixture = copy.deepcopy(BASE_FIXTURE)
        fixture["services"] = dict(BASE_FIXTURE["services"], **{"x-ui": "inactive"})
        runner = FakeRunner(fixture=fixture, root=root)
        result = run_installer(self.arguments(), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("blocked", result.stdout)
        self.assertIn("xui_service_inactive", result.stdout)
        self.assertEqual(list(root.rglob("*")), [])
        self.assertFalse(runner.mutating_command_seen)

    # ------------------------------------------------------------------
    # ordering and rollback

    def test_apply_opens_ssh_before_enabling_default_deny(self):
        root = self.empty_root()
        runner = FakeRunner(root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(
            runner.index(("ufw", "allow", "%d/tcp" % SSH_PORT)),
            runner.index(("ufw", "default", "deny", "incoming")),
        )

    def test_apply_never_allows_udp_443(self):
        root = self.empty_root()
        runner = FakeRunner(root=root)
        run_installer(self.arguments("--apply"), root=root, runner=runner)

        allows = [
            command for command in runner.commands
            if command[0] == "ufw" and command[1] == "allow"
        ]
        self.assertTrue(allows)
        for command in allows:
            self.assertTrue(command[2].endswith("/tcp"), command)

    def test_failed_nginx_validation_restores_files_and_never_reloads(self):
        root = self.installed_root()
        before = snapshot_without_backups(root)
        runner = FakeRunner(fail_on=("nginx", "-t"), root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(snapshot_without_backups(root), before)
        self.assertNotIn(("systemctl", "reload", "nginx"), runner.commands)
        self.assertNotIn(("systemctl", "start", "nginx"), runner.commands)

    def test_failed_preflight_makes_no_changes(self):
        root = self.installed_root()
        before = snapshot(root)
        fixture = copy.deepcopy(BASE_FIXTURE)
        fixture["services"] = dict(BASE_FIXTURE["services"], **{"x-ui": "inactive"})
        runner = FakeRunner(fixture=fixture, root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(snapshot(root), before)
        self.assertFalse(runner.mutating_command_seen)
        self.assertIn("preflight_blocked", result.stderr)
        self.assertIn("xui_service_inactive", result.stderr)

    def test_ssh_argument_mismatch_blocks_apply(self):
        root = self.empty_root()
        runner = FakeRunner(root=root)
        result = run_installer(
            [
                "--config",
                str(self.write_config("domain")),
                "--ssh-port",
                "22",
                "--apply",
            ],
            root=root,
            runner=runner,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ssh_port_mismatch", result.stderr)
        self.assertFalse(runner.mutating_command_seen)
        self.assertEqual(list(root.rglob("*")), [])

    def test_unknown_active_ufw_rules_block_apply(self):
        root = self.empty_root()
        runner = FakeRunner(
            root=root,
            ufw_status="Status: active\n\n[ 1] 22/tcp ALLOW IN Anywhere\n",
        )
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ufw_conflict", result.stderr)
        ufw_mutations = [
            command
            for command in runner.commands
            if command[0] == "ufw" and command[1] != "status"
        ]
        self.assertEqual(ufw_mutations, [])

    def test_package_failure_rolls_back_and_reports_code(self):
        root = self.installed_root()
        before = snapshot_without_backups(root)
        runner = FakeRunner(fail_on=("apt-get",), root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("package_install_failed", result.stderr)
        self.assertEqual(snapshot_without_backups(root), before)

    def test_certificate_failure_rolls_back(self):
        root = self.installed_root()
        before = snapshot_without_backups(root)
        runner = FakeRunner(fail_on=("certbot",), root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("certificate_issuance_failed", result.stderr)
        self.assertEqual(snapshot_without_backups(root), before)

    def test_compose_failure_rolls_back(self):
        root = self.installed_root()
        before = snapshot_without_backups(root)
        runner = FakeRunner(fail_on=("docker", "compose", "up"), root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("compose_failed", result.stderr)
        # The issued certificate is deliberately retained (deleting
        # only the live files would corrupt the certbot lineage), so
        # the comparison ignores the letsencrypt tree.
        after = {
            key: value
            for key, value in snapshot_without_backups(root).items()
            if not key.startswith("etc/letsencrypt")
        }
        self.assertEqual(after, before)
        # The only reload is the successful pre-failure one; rollback
        # stops the nginx we started instead of reloading over it.
        up_command = next(
            command
            for command in runner.commands
            if command[:1] == ("docker",) and "up" in command
        )
        reload_command = ("systemctl", "reload", "nginx")
        self.assertLess(
            runner.commands.index(reload_command),
            runner.commands.index(up_command),
        )
        self.assertIn(("systemctl", "stop", "nginx"), runner.commands)
        self.assertEqual(
            runner.commands.index(("systemctl", "stop", "nginx")),
            len(runner.commands) - 1,
        )

    def test_idempotent_second_apply(self):
        root = self.empty_root()
        first = run_installer(
            self.arguments("--apply"), root=root, runner=FakeRunner(root=root)
        )
        after_first = snapshot_without_backups(root)
        second_runner = FakeRunner(root=root)
        second = run_installer(
            self.arguments("--apply"), root=root, runner=second_runner
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(snapshot_without_backups(root), after_first)
        # The existing certificate must not be re-issued on a repeat run.
        reissued = [
            command
            for command in second_runner.commands
            if "certonly" in command
        ]
        self.assertEqual(reissued, [])

    # ------------------------------------------------------------------
    # content and placement

    def test_mode_specific_certbot_argv(self):
        domain_root = self.empty_root()
        domain_runner = FakeRunner(root=domain_root)
        run_installer(
            self.arguments("--apply"), root=domain_root, runner=domain_runner
        )
        domain_argv = [
            command
            for command in domain_runner.commands
            if command[0].endswith("/certbot") and "certonly" in command
        ]
        self.assertEqual(len(domain_argv), 1)
        self.assertIn("--cert-name", domain_argv[0])
        self.assertEqual(
            domain_argv[0][domain_argv[0].index("--cert-name") + 1],
            "clash-sub-domain",
        )
        self.assertEqual(domain_argv[0].count("-d"), 2)
        self.assertIn("--email", domain_argv[0])

        ip_root = self.empty_root()
        ip_runner = FakeRunner(root=ip_root)
        run_installer(
            self.arguments("--apply", mode="ip"), root=ip_root, runner=ip_runner
        )
        ip_argv = [
            command
            for command in ip_runner.commands
            if command[0].endswith("/certbot") and "certonly" in command
        ]
        self.assertEqual(len(ip_argv), 1)
        self.assertIn("--preferred-profile", ip_argv[0])
        self.assertIn("--ip-address", ip_argv[0])
        self.assertNotIn("-d", ip_argv[0])
        self.assertEqual(
            ip_argv[0][ip_argv[0].index("--ip-address") + 1], "192.0.2.10"
        )

    def test_atomic_replacement_and_public_file_modes(self):
        root = self.installed_root()
        runner = FakeRunner(root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertEqual(result.returncode, 0, result.stderr)
        import stat as stat_module

        acme = root / "etc" / "nginx" / "conf.d" / "clash-sub-00-acme-http.conf"
        tls = root / "etc" / "nginx" / "conf.d" / "clash-sub-10-tls.conf"
        self.assertNotEqual(acme.read_text(encoding="utf-8"), "old-acme\n")
        self.assertEqual(stat_module.S_IMODE(acme.stat().st_mode), 0o644)
        self.assertEqual(stat_module.S_IMODE(tls.stat().st_mode), 0o644)
        for path in root.rglob("*.tmp"):
            self.fail("temporary file left behind: %s" % path)
        units = list((root / "etc" / "systemd" / "system").glob("clash-sub-cert-*"))
        self.assertGreaterEqual(len(units), 4)

    def test_private_root_is_provisioned_for_uid_10001(self):
        root = self.empty_root()
        runner = FakeRunner(root=root)
        run_installer(self.arguments("--apply"), root=root, runner=runner)

        install_commands = [
            command for command in runner.commands if command[0] == "install"
        ]
        self.assertEqual(len(install_commands), 1)
        argv = install_commands[0]
        self.assertEqual(
            argv[1:7], ("-d", "-o", "10001", "-g", "10001", "-m", "700")[:6]
        )
        provisioned = argv[7:]
        self.assertIn(str((root / "private").resolve()), provisioned)
        for name in ("config", "staging", "releases", "current", "logs", "sources"):
            self.assertIn(str((root / "private" / name).resolve()), provisioned)

    def test_host_command_symlink_points_at_the_repository(self):
        root = self.empty_root()
        runner = FakeRunner(root=root)
        run_installer(self.arguments("--apply"), root=root, runner=runner)

        link = root / "usr" / "local" / "bin" / "clash-sub"
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), str(ROOT / "bin" / "clash-sub"))

    def test_unrelated_nginx_files_are_never_deleted(self):
        root = self.installed_root()
        runner = FakeRunner(root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((root / "etc" / "nginx" / "conf.d" / "unrelated.conf").exists())

    def test_backups_carry_an_inventory_with_modes(self):
        root = self.installed_root()
        runner = FakeRunner(root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertEqual(result.returncode, 0, result.stderr)
        backups = root / "var" / "backups" / "clash-sub"
        inventories = list(backups.rglob("inventory.json"))
        self.assertEqual(len(inventories), 1)
        document = json.loads(inventories[0].read_text(encoding="utf-8"))
        self.assertIsInstance(document, dict)
        self.assertTrue(document)

    def test_output_is_sanitized(self):
        root = self.empty_root()
        runner = FakeRunner(root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        combined = result.stdout + result.stderr
        for forbidden in (
            "example.com",
            "admin@example",
            "example-random-panel-path",
            "notify-command",
            "fullchain.pem",
            "privkey",
        ):
            self.assertNotIn(forbidden, combined)

    def test_apply_requires_the_pinned_repository_path_in_production(self):
        result = run_installer(self.arguments("--apply"))

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("repo_path_mismatch", result.stderr)

    # ------------------------------------------------------------------
    # fresh-host bootstrap and rollback recoverability

    def stock_nginx_root(self):
        root = self.installed_root()
        sites_enabled = root / "etc" / "nginx" / "sites-enabled"
        sites_enabled.mkdir(parents=True, exist_ok=True)
        (sites_enabled / "default").symlink_to("../sites-available/default")
        return root, sites_enabled / "default"

    def test_stock_debian_default_site_is_removed_for_acme_bootstrap(self):
        # Debian's nginx package ships a default site whose
        # "listen 80 default_server" collides with the ACME server; a
        # fresh-host apply must remove it to ever pass nginx -t.
        root, default_site = self.stock_nginx_root()
        runner = FakeRunner(root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(default_site.exists())
        self.assertFalse(default_site.is_symlink())

    def test_default_site_removal_is_rolled_back_on_failure(self):
        root, default_site = self.stock_nginx_root()
        runner = FakeRunner(fail_on=("docker", "compose", "up"), root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(default_site.is_symlink())
        self.assertEqual(os.readlink(default_site), "../sites-available/default")

    def test_rollback_disables_certificate_timers(self):
        root = self.empty_root()
        runner = FakeRunner(fail_on=("docker", "compose", "up"), root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            ("systemctl", "disable", "--now", "clash-sub-cert-renew.timer"),
            runner.commands,
        )
        self.assertIn(
            ("systemctl", "disable", "--now", "clash-sub-cert-check.timer"),
            runner.commands,
        )
        disable_index = runner.commands.index(
            ("systemctl", "disable", "--now", "clash-sub-cert-renew.timer")
        )
        enable_index = runner.commands.index(
            ("systemctl", "enable", "--now", "clash-sub-cert-renew.timer")
        )
        self.assertLess(enable_index, disable_index)

    def test_issued_certificate_survives_rollback_so_reapply_succeeds(self):
        # Deleting only the live symlinks would leave a half-deleted
        # certbot lineage that blocks re-issuance; the certificate must
        # be retained so a retry apply succeeds.
        root = self.empty_root()
        failed = run_installer(
            self.arguments("--apply"),
            root=root,
            runner=FakeRunner(fail_on=("docker", "compose", "up"), root=root),
        )
        self.assertNotEqual(failed.returncode, 0)
        self.assertTrue(list(root.rglob("fullchain.pem")))

        retry = run_installer(
            self.arguments("--apply"), root=root, runner=FakeRunner(root=root)
        )
        self.assertEqual(retry.returncode, 0, retry.stderr)

    def test_filesystem_error_triggers_rollback_without_traceback(self):
        root = self.empty_root()
        (root / "etc").mkdir()
        (root / "etc" / "nginx").write_text("not a directory\n", encoding="utf-8")
        runner = FakeRunner(root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("filesystem_error", result.stderr)
        self.assertNotIn("Traceback", result.stderr + result.stdout)

    def test_failed_apply_records_diagnostics_in_a_private_failure_log(self):
        root = self.installed_root()
        runner = FakeRunner(fail_on=("nginx", "-t"), root=root)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertNotEqual(result.returncode, 0)
        import stat as stat_module

        failure_logs = list((root / "var" / "backups" / "clash-sub").rglob("failure.log"))
        self.assertEqual(len(failure_logs), 1)
        self.assertEqual(
            stat_module.S_IMODE(failure_logs[0].stat().st_mode), 0o600
        )
        content = failure_logs[0].read_text(encoding="utf-8")
        self.assertIn("nginx", content)
        self.assertNotIn("failure.log", result.stdout + result.stderr)

    def test_active_ufw_with_exact_approved_rules_proceeds_without_reset(self):
        root = self.empty_root()
        runner = FakeRunner(root=root, ufw_status=UFW_ACTIVE_APPROVED)
        result = run_installer(self.arguments("--apply"), root=root, runner=runner)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(("ufw", "--force", "reset"), runner.commands)
        self.assertIn(("ufw", "allow", "%d/tcp" % SSH_PORT), runner.commands)
        self.assertIn(("ufw", "default", "deny", "incoming"), runner.commands)
        self.assertIn(("ufw", "--force", "enable"), runner.commands)


class SystemdUnitTests(unittest.TestCase):
    def test_service_units_exec_only_interpreter_or_certbot_binaries(self):
        # A 644 project script without a shebang would fail with
        # systemd 203/EXEC; worse, a failed ExecStartPost marks the
        # whole renew oneshot failed, so OnFailure would record a
        # failed renewal even when certbot renew succeeded.
        allowed = ("/usr/bin/python3", "/opt/certbot/bin/certbot")
        checker_seen = False
        services = sorted((ROOT / "deploy" / "systemd").glob("*.service"))
        self.assertGreaterEqual(len(services), 3)
        for path in services:
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith(("ExecStart=", "ExecStartPost=")):
                    continue
                # A leading "-" is systemd's ignore-failure prefix.
                command = stripped.split("=", 1)[1]
                command = command[1:] if command.startswith("-") else command
                executable = command.split()[0]
                self.assertTrue(
                    executable.startswith(allowed), (path.name, executable)
                )
                if executable == "/usr/bin/python3":
                    self.assertIn("check_certificate.py", stripped, path.name)
                    checker_seen = True
                else:
                    self.assertEqual(
                        executable, "/opt/certbot/bin/certbot", path.name
                    )
        self.assertTrue(checker_seen)

    def test_units_never_refresh_subscriptions_or_run_containers(self):
        exec_lines = []
        for path in sorted((ROOT / "deploy" / "systemd").iterdir()):
            for line in path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith(("ExecStart=", "ExecStartPost=")):
                    exec_lines.append(stripped)
        self.assertTrue(exec_lines)
        combined = "\n".join(exec_lines)
        self.assertNotIn("docker", combined)
        self.assertNotIn("bin/clash-sub", combined)
        self.assertNotIn("refresh", combined)


class WrapperTests(unittest.TestCase):
    def test_wrapper_delegates_without_a_second_implementation(self):
        text = (ROOT / "scripts" / "install-server.sh").read_text(encoding="utf-8")

        self.assertIn("install_server.py", text)
        self.assertNotIn("curl", text)
        self.assertNotIn("apt-get", text)
        self.assertNotIn("ufw", text)
        self.assertIn("python3", text)

    def test_wrapper_changes_to_the_repository_root(self):
        # Keeps preflight commands such as a bare `docker compose
        # config` independent of the caller's working directory.
        text = (ROOT / "scripts" / "install-server.sh").read_text(encoding="utf-8")

        self.assertIn("cd", text)


class SystemRunnerTests(unittest.TestCase):
    def test_commands_run_from_the_repository_root_regardless_of_cwd(self):
        import tempfile

        runner = install_server.SystemRunner()
        with tempfile.TemporaryDirectory() as elsewhere:
            previous = os.getcwd()
            os.chdir(elsewhere)
            try:
                result = runner.run(["pwd"], timeout=10)
            finally:
                os.chdir(previous)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.strip(), str(install_server.ROOT))


if __name__ == "__main__":
    unittest.main()
