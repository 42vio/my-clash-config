import json
import shutil
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import ANY, patch

from clash_sub.installer import (
    InstallPaths,
    InstallState,
    Installer,
    InstallerError,
    load_install_state,
    save_install_state,
)
from clash_sub.xui import XuiCompatibilityError


class InstallStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name).resolve()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_round_trips_state_with_0600_mode(self):
        state = InstallState(domain="example.com", panel_port=2053, panel_base_path="/p-1a")
        path = self.root / "install-state.json"

        save_install_state(path, state)
        loaded = load_install_state(path)

        self.assertEqual(loaded.domain, "example.com")
        self.assertEqual(loaded.panel_port, 2053)
        self.assertEqual(loaded.panel_base_path, "/p-1a")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_load_rejects_unknown_schema(self):
        path = self.root / "install-state.json"
        path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

        with self.assertRaisesRegex(InstallerError, "install_state_invalid"):
            load_install_state(path)

    def test_load_rejects_corrupted_payload(self):
        path = self.root / "install-state.json"
        path.write_text("{not json", encoding="utf-8")

        with self.assertRaisesRegex(InstallerError, "install_state_invalid"):
            load_install_state(path)

    def test_load_returns_default_when_absent(self):
        self.assertEqual(load_install_state(self.root / "missing.json"), InstallState())

    def test_save_rejects_foreign_object(self):
        with self.assertRaisesRegex(InstallerError, "install_state_invalid"):
            save_install_state(self.root / "state.json", {"domain": "example.com"})

    def test_default_paths_target_etc_layout(self):
        paths = InstallPaths()

        self.assertEqual(paths.stream_conf(), Path("/etc/nginx/stream-conf.d/clash-sub.conf"))
        self.assertEqual(paths.http_conf(), Path("/etc/nginx/conf.d/clash-sub.conf"))
        self.assertEqual(paths.routes_conf, Path("/etc/nginx/clash-sub/routes.conf"))
        self.assertEqual(paths.fullchain(), Path("/etc/ssl/domain/fullchain.pem"))
        self.assertEqual(paths.privkey(), Path("/etc/ssl/domain/privkey.pem"))
        self.assertEqual(paths.xui_database, Path("/etc/x-ui/x-ui.db"))
        self.assertEqual(paths.acme_home, Path("/root/.acme.sh"))


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        self.root.mkdir()
        self.paths = InstallPaths(
            nginx_conf=self.root / "nginx.conf",
            stream_conf_dir=self.root / "stream-conf.d",
            http_conf_dir=self.root / "conf.d",
            routes_conf=self.root / "clash-sub" / "routes.conf",
            ssl_dir=self.root / "ssl",
            acme_home=self.root / "acme",
            sysctl_conf=self.root / "sysctl.conf",
            journald_conf_dir=self.root / "journald",
            systemd_dir=self.root / "systemd",
            swap_file=self.root / "swap.img",
            xui_database=self.root / "x-ui.db",
            private_root=self.root / "private",
            public_root=self.root / "public",
        )
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self, runner=None):
        return Installer(
            self.root,
            paths=self.paths,
            runner=runner or self._runner,
        )

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    def test_rejects_non_root(self):
        with patch("clash_sub.installer.os.geteuid", return_value=1000):
            with self.assertRaisesRegex(InstallerError, "not_root"):
                self._installer().preflight("example.com")

    def test_rejects_unsupported_distribution(self):
        os_release = self.root / "os-release"
        os_release.write_text('ID="ubuntu"\nVERSION_ID="24.04"\n', encoding="utf-8")

        with patch("clash_sub.installer.os.geteuid", return_value=0), patch(
            "clash_sub.installer._OS_RELEASE_PATH", os_release
        ):
            with self.assertRaisesRegex(InstallerError, "unsupported_distribution"):
                self._installer().preflight("example.com")

    def test_accepts_debian_12(self):
        os_release = self.root / "os-release"
        os_release.write_text('ID="debian"\nVERSION_ID="12"\n', encoding="utf-8")

        with patch("clash_sub.installer.os.geteuid", return_value=0), patch(
            "clash_sub.installer._OS_RELEASE_PATH", os_release
        ), patch(
            "clash_sub.installer.read_xui_snapshot", lambda path: object()
        ), patch(
            "clash_sub.installer.read_panel_port", lambda path: 2053
        ), patch(
            "clash_sub.installer._require_free_tcp_port", lambda installer_self, port: None
        ), patch(
            "clash_sub.installer._resolve_host", lambda host: ["192.0.2.1"]
        ), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1"]
        ):
            self._installer().preflight("example.com")

        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertIn("preflight", state.phases_done)

    def test_rejects_xui_database_problems(self):
        os_release = self.root / "os-release"
        os_release.write_text('ID="debian"\nVERSION_ID="12"\n', encoding="utf-8")

        def broken(path):
            raise XuiCompatibilityError("boom")

        with patch("clash_sub.installer.os.geteuid", return_value=0), patch(
            "clash_sub.installer._OS_RELEASE_PATH", os_release
        ), patch("clash_sub.installer.read_xui_snapshot", broken):
            with self.assertRaisesRegex(InstallerError, "xui_incompatible"):
                self._installer().preflight("example.com")

    def test_requires_free_tcp_port(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.listen(1)
        installer = self._installer()
        try:
            with self.assertRaisesRegex(InstallerError, "port_443_taken"):
                installer._require_free_tcp_port(port)
        finally:
            server.close()

    def test_accepts_free_tcp_port(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        port = server.getsockname()[1]
        server.close()
        installer = self._installer()

        installer._require_free_tcp_port(port)

    def test_rejects_dns_mismatch(self):
        installer = self._installer()

        with patch("clash_sub.installer._resolve_host", lambda host: ["203.0.113.99"]), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1"]
        ):
            with self.assertRaisesRegex(InstallerError, "dns_mismatch"):
                installer._require_dns("example.com")

    def test_accepts_matching_dns(self):
        installer = self._installer()

        with patch("clash_sub.installer._resolve_host", lambda host: ["192.0.2.1"]), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1", "10.0.0.5"]
        ):
            installer._require_dns("example.com")


class LowMemoryPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths(
            sysctl_conf=self.root / "99-clash-sub.conf",
            journald_conf_dir=self.root / "journald",
            swap_file=self.root / "swap.img",
        )
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        return Installer(self.root, paths=self.paths, runner=self._runner)

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    def test_writes_sysctl_and_journald_without_swap(self):
        self._installer().optimize_low_memory(swap_mb=0)

        self.assertEqual(
            self.paths.sysctl_conf.read_text(encoding="utf-8"),
            "vm.swappiness=10\n",
        )
        self.assertEqual(
            (self.paths.journald_conf_dir / "99-clash-sub.conf").read_text(encoding="utf-8"),
            "[Journal]\nSystemMaxUse=50M\n",
        )
        swap_commands = [
            c for c in self.runner_calls if "swapon" in c or "mkswap" in c or "fallocate" in c
        ]
        self.assertEqual(swap_commands, [])
        self.assertIn("low_memory", load_install_state(self.root / "private" / "install-state.json").phases_done)

    def test_creates_swap_when_requested(self):
        self._installer().optimize_low_memory(swap_mb=1024)

        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("fallocate" in item for item in joined))
        self.assertTrue(
            any("mkswap" in item and str(self.paths.swap_file) in item for item in joined)
        )
        self.assertTrue(
            any("swapon" in item and str(self.paths.swap_file) in item for item in joined)
        )

    def test_skips_swap_when_file_exists(self):
        self.paths.swap_file.write_bytes(b"")
        self._installer().optimize_low_memory(swap_mb=1024)

        joined = [" ".join(c) for c in self.runner_calls]
        self.assertFalse(any("fallocate" in item for item in joined))

    def test_raises_when_command_fails(self):
        def failing_runner(arguments, **_):
            self.runner_calls.append(list(arguments))
            return subprocess.CompletedProcess(arguments, 1)

        installer = Installer(self.root, paths=self.paths, runner=failing_runner)

        with self.assertRaisesRegex(InstallerError, "command_failed"):
            installer.optimize_low_memory(swap_mb=0)


class NginxPackagePhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        self.nginx_conf = self.root / "nginx.conf"
        self.nginx_conf.write_text(
            "user www-data;\nhttp {\n    include /etc/nginx/conf.d/*.conf;\n}\n",
            encoding="utf-8",
        )
        self.paths = InstallPaths(
            nginx_conf=self.nginx_conf,
            stream_conf_dir=self.root / "stream-conf.d",
        )
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        return Installer(self.root, paths=self.paths, runner=self._runner)

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    def test_installs_packages_and_appends_stream_include_once(self):
        installer = self._installer()

        installer.install_nginx_packages()
        text_one = self.nginx_conf.read_text(encoding="utf-8")

        installer.install_nginx_packages()
        text_two = self.nginx_conf.read_text(encoding="utf-8")

        self.assertIn("stream {", text_one)
        self.assertIn(str(self.paths.stream_conf_dir), text_one)
        self.assertEqual(text_one, text_two)
        self.assertIn("user www-data;", text_one)
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(
            any("apt-get" in item and "nginx" in item for item in joined)
        )
        self.assertTrue(any("libnginx-mod-stream" in item for item in joined))
        self.assertIn("nginx_packages", load_install_state(self.root / "private" / "install-state.json").phases_done)

    def test_appends_stream_include_to_empty_conf(self):
        self.nginx_conf.write_text("", encoding="utf-8")

        self._installer().install_nginx_packages()

        text = self.nginx_conf.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# clash-sub stream include") or text.startswith("\n# clash-sub stream include") or "stream {" in text)
        self.assertIn("stream {", text)

    def test_skips_append_when_marker_present(self):
        marked = self.nginx_conf.read_text(encoding="utf-8") + "\n# clash-sub stream include\nstream {\n    include %s/*.conf;\n}\n" % self.paths.stream_conf_dir
        self.nginx_conf.write_text(marked, encoding="utf-8")

        self._installer().install_nginx_packages()

        self.assertEqual(self.nginx_conf.read_text(encoding="utf-8"), marked)


class CertificatePhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths(
            ssl_dir=self.root / "ssl",
            acme_home=self.root / "acme",
        )
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append({"argv": list(arguments), "env": None})
        acme = self.paths.acme_home / "acme.sh"
        if (
            arguments
            and arguments[0] == str(acme)
            and "--install-cert" in arguments
        ):
            self.paths.fullchain().parent.mkdir(parents=True, exist_ok=True)
            self.paths.fullchain().write_text("CERT", encoding="ascii")
            self.paths.privkey().write_text("KEY", encoding="ascii")
        return subprocess.CompletedProcess(arguments, 0)

    def _installer(self):
        return Installer(self.root, paths=self.paths, runner=self._runner)

    def test_issues_wildcard_and_installs_cert(self):
        installer = self._installer()
        captured = self.runner_calls

        def env_runner(arguments, **kwargs):
            captured.append({"argv": list(arguments), "env": kwargs.get("env")})
            if (
                arguments
                and arguments[0] == str(installer.paths.acme_home / "acme.sh")
                and "--install-cert" in arguments
            ):
                installer.paths.ssl_dir.mkdir(parents=True, exist_ok=True)
                installer.paths.fullchain().write_text("CERT", encoding="ascii")
                installer.paths.privkey().write_text("KEY", encoding="ascii")
            return subprocess.CompletedProcess(list(arguments), 0)

        installer.runner = env_runner
        installer.issue_certificate("example.com", "cf-token-value")

        issue = next(call for call in captured if "--issue" in call["argv"])
        self.assertIn("-d", issue["argv"])
        self.assertIn("example.com", issue["argv"])
        self.assertIn("*.example.com", issue["argv"])
        self.assertIn("dns_cf", issue["argv"])
        self.assertEqual(issue["env"]["CF_Token"], "cf-token-value")
        install = next(call for call in captured if "--install-cert" in call["argv"])
        self.assertIn(str(self.paths.fullchain()), install["argv"])
        self.assertIn(str(self.paths.privkey()), install["argv"])
        self.assertTrue(
            any("get.acme.sh" in " ".join(call["argv"]) for call in captured),
            "acme.sh bootstrap must be downloaded",
        )
        self.assertEqual(self.paths.privkey().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.paths.ssl_dir.stat().st_mode & 0o777, 0o700)
        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertIn("certificate", state.phases_done)

    def test_rejects_empty_inputs(self):
        installer = self._installer()

        for domain, token in (("", "t"), ("example.com", ""), (None, "t")):
            with self.subTest(domain=domain):
                with self.assertRaisesRegex(InstallerError, "invalid_domain|missing_cf_token"):
                    installer.issue_certificate(domain, token)


class NginxActivationPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        (self.root / "templates" / "nginx").mkdir(parents=True)
        source = Path(__file__).resolve().parents[1] / "templates" / "nginx"
        for template in source.iterdir():
            shutil.copy(template, self.root / "templates" / "nginx" / template.name)
        self.paths = InstallPaths(
            stream_conf_dir=self.root / "stream-conf.d",
            http_conf_dir=self.root / "conf.d",
            routes_conf=self.root / "clash-sub" / "routes.conf",
            ssl_dir=self.root / "ssl",
            systemd_dir=self.root / "systemd",
            nginx_conf=self.root / "nginx.conf",
        )
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        return Installer(self.root, paths=self.paths, runner=self._runner)

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    def test_activates_stream_and_sub_server_and_records_state(self):
        installer = self._installer()

        installer.activate_nginx(domain="example.com", panel_port=2053)

        stream_text = self.paths.stream_conf().read_text(encoding="utf-8")
        http_text = self.paths.http_conf().read_text(encoding="utf-8")
        self.assertIn("sub.example.com", stream_text)
        self.assertIn("sub.example.com", http_text)
        self.assertIn("/p-", http_text)
        self.assertIn("127.0.0.1:10443", stream_text)
        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertIn("nginx_activation", state.phases_done)
        self.assertEqual(state.domain, "example.com")
        self.assertEqual(state.panel_port, 2053)
        self.assertTrue(state.panel_base_path.startswith("/p-"))
        self.assertEqual(
            state.files_written,
            [str(self.paths.stream_conf()), str(self.paths.http_conf()), str(self.paths.routes_conf)],
        )
        self.assertTrue(self.paths.routes_conf.exists())
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("nginx" in item and "-t" in item for item in joined))
        self.assertTrue(any("enable" in item and "nginx" in item for item in joined))

    def test_reuses_recorded_panel_base_path(self):
        installer = self._installer()
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(panel_base_path="/p-fixedpath"),
        )

        installer.activate_nginx(domain="example.com", panel_port=2053)

        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertEqual(state.panel_base_path, "/p-fixedpath")

    def test_enable_failure_does_not_journal_phase(self):
        def failing_enable_runner(arguments, **_):
            self.runner_calls.append(list(arguments))
            if "enable" in arguments and "nginx" in arguments:
                return subprocess.CompletedProcess(arguments, 1)
            return subprocess.CompletedProcess(arguments, 0)

        installer = Installer(self.root, paths=self.paths, runner=failing_enable_runner)

        with self.assertRaisesRegex(InstallerError, "command_failed"):
            installer.activate_nginx(domain="example.com", panel_port=2053)

        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertNotIn("nginx_activation", state.phases_done)

    def test_hardens_systemd_units(self):
        installer = self._installer()

        installer.harden_systemd()

        restart = self.paths.systemd_dir / "nginx.service.d" / "clash-sub-restart.conf"
        self.assertEqual(
            restart.read_text(encoding="utf-8"),
            "[Service]\nRestart=on-failure\nRestartSec=2s\n",
        )
        traffic = self.paths.systemd_dir / "clash-sub-traffic.service"
        self.assertTrue(traffic.exists())
        timer = self.paths.systemd_dir / "clash-sub-traffic.timer"
        self.assertTrue(timer.exists())
        recover = self.paths.systemd_dir / "clash-sub-recover.service"
        self.assertTrue(recover.exists())
        recover_drop_in = (
            self.paths.systemd_dir / "nginx.service.d" / "clash-sub-recover.conf"
        )
        self.assertTrue(recover_drop_in.exists())
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("daemon-reload" in item for item in joined))
        self.assertTrue(any("enable" in item and "clash-sub-traffic.timer" in item for item in joined))
        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertIn("systemd_harden", state.phases_done)


class SubscriptionInitPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths(
            xui_database=self.root / "x-ui.db",
            private_root=self.root / "var" / "private",
            public_root=self.root / "var" / "public",
            routes_conf=self.root / "clash-sub" / "routes.conf",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _noop_runner(self, arguments, **_):
        return subprocess.CompletedProcess(list(arguments), 0)

    def test_writes_service_yaml_with_expected_values(self):
        installer = Installer(self.root, paths=self.paths, runner=self._noop_runner)

        installer.initialize_subscription(domain="example.com", owner_email="owner-example")

        content = (self.root / "private" / "config" / "service.yaml").read_text(encoding="utf-8")
        self.assertIn("schema-version: 2", content)
        self.assertIn("owner-email: owner-example", content)
        self.assertIn("subscription-authority: sub.example.com:443", content)
        self.assertIn("xui-public-endpoint: example.com:443", content)
        self.assertIn(str(self.paths.xui_database), content)
        mode = (self.root / "private" / "config" / "service.yaml").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertIn("subscription_init", state.phases_done)

    def test_prepares_runtime_directories(self):
        installer = Installer(self.root, paths=self.paths, runner=self._noop_runner)

        installer.initialize_subscription(domain="example.com", owner_email="owner-example")

        self.assertTrue(self.paths.private_root.is_dir())
        self.assertEqual(self.paths.private_root.stat().st_mode & 0o777, 0o700)
        self.assertTrue(self.paths.public_root.is_dir())
        self.assertEqual(self.paths.public_root.stat().st_mode & 0o7777, 0o2750)
        self.assertTrue(self.paths.routes_conf.parent.is_dir())


class InstallOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths()
        self.printed = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _noop_runner(self, arguments, **_):
        return subprocess.CompletedProcess(list(arguments), 0)

    def test_install_skips_completed_phases_and_persists_domain(self):
        installer = Installer(
            self.root,
            paths=self.paths,
            runner=self._noop_runner,
            print_fn=self.printed.append,
        )
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(domain="example.com", phases_done=["preflight", "low_memory"]),
        )

        with patch.object(Installer, "install_nginx_packages") as pkg, patch.object(
            Installer, "issue_certificate"
        ) as cert, patch.object(Installer, "activate_nginx") as activate, patch.object(
            Installer, "harden_systemd"
        ) as harden, patch.object(
            Installer, "initialize_subscription"
        ) as init, patch.object(
            Installer, "preflight"
        ) as preflight, patch.object(
            Installer, "optimize_low_memory"
        ) as low_memory, patch(
            "clash_sub.installer.read_panel_port", lambda path: 2053
        ):
            installer.install(
                domain="example.com", cf_token="tok", swap_mb=0, owner_email="owner-example"
            )

        preflight.assert_not_called()
        low_memory.assert_not_called()
        self.assertFalse(any("preflight" in message for message in self.printed))
        pkg.assert_called_once()
        cert.assert_called_once_with("example.com", "tok")
        activate.assert_called_once_with(domain="example.com", panel_port=ANY)
        harden.assert_called_once()
        init.assert_called_once_with(domain="example.com", owner_email="owner-example")
        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertEqual(state.domain, "example.com")
        self.assertIn("report", state.phases_done)

    def test_finalize_reports_panel_url_and_gate(self):
        installer = Installer(self.root, paths=self.paths, runner=self._noop_runner)
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(domain="example.com", panel_port=2053, panel_base_path="/p-abc"),
        )

        report = installer.finalize()

        self.assertEqual(report["panel_url"], "https://sub.example.com/p-abc/")
        self.assertIn("gate_instruction", report)


class RollbackInstallTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths(
            nginx_conf=self.root / "nginx.conf",
            stream_conf_dir=self.root / "stream-conf.d",
            http_conf_dir=self.root / "conf.d",
            systemd_dir=self.root / "systemd",
        )
        self.paths.nginx_conf.write_text(
            "http {\n}\n# clash-sub stream include\nstream {\n    include %s/*.conf;\n}\n"
            % self.paths.stream_conf_dir,
            encoding="utf-8",
        )
        self.paths.stream_conf().parent.mkdir(parents=True)
        self.paths.stream_conf().write_text("# stream\n", encoding="utf-8")
        self.paths.http_conf().parent.mkdir(parents=True)
        self.paths.http_conf().write_text("# http\n", encoding="utf-8")
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(arguments, 0)

    def _installer(self):
        return Installer(self.root, paths=self.paths, runner=self._runner)

    def test_rolls_back_install_artifacts(self):
        installer = self._installer()
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(
                domain="example.com",
                panel_port=2053,
                panel_base_path="/p-x",
                phases_done=["nginx_activation"],
                files_written=[
                    str(self.paths.stream_conf()),
                    str(self.paths.http_conf()),
                ],
                backups={},
            ),
        )

        installer.rollback_install()

        self.assertFalse(self.paths.stream_conf().exists())
        self.assertFalse(self.paths.http_conf().exists())
        text = self.paths.nginx_conf.read_text(encoding="utf-8")
        self.assertNotIn("clash-sub stream include", text)
        self.assertIn("http {", text)
        self.assertFalse((self.root / "private" / "install-state.json").exists())
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("stop" in item and "nginx" in item for item in joined))
        self.assertTrue(any("disable" in item and "nginx" in item for item in joined))
        self.assertTrue(any("daemon-reload" in item for item in joined))

    def test_rollback_without_journal_leaves_files(self):
        installer = self._installer()

        installer.rollback_install()

        self.assertTrue(self.paths.stream_conf().exists())
        text = self.paths.nginx_conf.read_text(encoding="utf-8")
        self.assertIn("clash-sub stream include", text)

    def test_rollback_tolerates_missing_files(self):
        self.paths.stream_conf().unlink()
        installer = self._installer()
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(files_written=[str(self.paths.stream_conf())]),
        )

        installer.rollback_install()

        self.assertFalse((self.root / "private" / "install-state.json").exists())

    def test_rollback_survives_stop_failure(self):
        def failing_stop_runner(arguments, **_):
            self.runner_calls.append(list(arguments))
            if "stop" in arguments:
                return subprocess.CompletedProcess(arguments, 1)
            return subprocess.CompletedProcess(arguments, 0)

        installer = Installer(self.root, paths=self.paths, runner=failing_stop_runner)
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(files_written=[str(self.paths.http_conf())]),
        )

        installer.rollback_install()

        self.assertFalse(self.paths.http_conf().exists())
        self.assertFalse((self.root / "private" / "install-state.json").exists())


class InstallResumeGuardTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)

    def tearDown(self):
        self.tempdir.cleanup()

    def _noop_runner(self, arguments, **_):
        return subprocess.CompletedProcess(list(arguments), 0)

    def test_rejects_conflicting_domain_resume(self):
        installer = Installer(self.root, runner=self._noop_runner)
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(domain="old.com", phases_done=["preflight"]),
        )

        with patch.object(Installer, "preflight"), patch.object(
            Installer, "install_nginx_packages"
        ):
            with self.assertRaisesRegex(InstallerError, "domain_mismatch"):
                installer.install(domain="new.com", cf_token="t")

    def test_allows_same_domain_resume(self):
        installer = Installer(self.root, runner=self._noop_runner)
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(domain="same.com", phases_done=["preflight"]),
        )

        with patch.object(Installer, "preflight") as preflight, patch.object(
            Installer, "install_nginx_packages"
        ) as pkg, patch.object(
            Installer, "optimize_low_memory"
        ), patch.object(
            Installer, "issue_certificate"
        ), patch.object(
            Installer, "activate_nginx"
        ), patch.object(
            Installer, "harden_systemd"
        ), patch.object(
            Installer, "initialize_subscription"
        ), patch(
            "clash_sub.installer.read_panel_port", lambda path: 2053
        ):
            installer.install(domain="same.com", cf_token="t")

        preflight.assert_not_called()
        pkg.assert_called_once()

    def test_panel_port_maps_to_installer_error(self):
        installer = Installer(self.root, runner=self._noop_runner)

        def broken(path):
            raise XuiCompatibilityError("boom")

        with patch("clash_sub.installer.read_panel_port", broken):
            with self.assertRaisesRegex(InstallerError, "xui_incompatible"):
                installer._panel_port()


if __name__ == "__main__":
    unittest.main()
