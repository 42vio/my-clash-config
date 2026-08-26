import base64
import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import unittest
from collections import namedtuple
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, patch

from clash_sub.installer import (
    InstallPaths,
    InstallState,
    Installer,
    InstallerError,
    _swap_active,
    load_install_state,
    save_install_state,
)
from clash_sub.xui import XuiCompatibilityError


FakeSnapshotClient = namedtuple("FakeSnapshotClient", "email enabled")


def fake_snapshot(*clients):
    return SimpleNamespace(clients=tuple(clients))


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

    def test_old_journal_without_new_fields_loads(self):
        path = self.root / "install-state.json"
        legacy = {
            "schema_version": 1,
            "domain": "example.com",
            "node_host": "node.example.com",
            "panel_port": 2053,
            "panel_base_path": "/p-1a",
            "phases_done": ["preflight"],
            "files_written": [],
            "backups": {},
        }
        path.write_text(json.dumps(legacy), encoding="utf-8")

        loaded = load_install_state(path)

        self.assertEqual(loaded.domain, "example.com")
        self.assertEqual(loaded.phases_done, ["preflight"])
        self.assertFalse(loaded.default_site_removed)
        self.assertEqual(loaded.replaced_files, {})

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
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/xui7k2m/", "127.0.0.1"),
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

    def test_rejects_default_panel_base_path(self):
        os_release = self.root / "os-release"
        os_release.write_text('ID="debian"\nVERSION_ID="12"\n', encoding="utf-8")

        with patch("clash_sub.installer.os.geteuid", return_value=0), patch(
            "clash_sub.installer._OS_RELEASE_PATH", os_release
        ), patch(
            "clash_sub.installer.read_xui_snapshot", lambda path: object()
        ), patch(
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/", "127.0.0.1"),
        ), patch(
            "clash_sub.installer._require_free_tcp_port", lambda installer_self, port: None
        ), patch(
            "clash_sub.installer._resolve_host", lambda host: ["192.0.2.1"]
        ), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1"]
        ):
            with self.assertRaisesRegex(InstallerError, "panel_base_path_required"):
                self._installer().preflight("example.com")

    def test_rejects_malformed_panel_base_path(self):
        os_release = self.root / "os-release"
        os_release.write_text('ID="debian"\nVERSION_ID="12"\n', encoding="utf-8")

        with patch("clash_sub.installer.os.geteuid", return_value=0), patch(
            "clash_sub.installer._OS_RELEASE_PATH", os_release
        ), patch(
            "clash_sub.installer.read_xui_snapshot", lambda path: object()
        ), patch(
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/bad path/", "127.0.0.1"),
        ), patch(
            "clash_sub.installer._require_free_tcp_port", lambda installer_self, port: None
        ), patch(
            "clash_sub.installer._resolve_host", lambda host: ["192.0.2.1"]
        ), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1"]
        ):
            with self.assertRaisesRegex(InstallerError, "panel_base_path_required"):
                self._installer().preflight("example.com")

    def test_rejects_panel_listen_on_all_interfaces(self):
        installer = self._installer()

        with patch("clash_sub.installer.os.geteuid", return_value=0), patch(
            "clash_sub.installer._OS_RELEASE_PATH", self._os_release_debian()
        ), patch(
            "clash_sub.installer.read_xui_snapshot", lambda path: object()
        ), patch(
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/xui7k2m/", "0.0.0.0"),
        ), patch(
            "clash_sub.installer._require_free_tcp_port", lambda installer_self, port: None
        ), patch(
            "clash_sub.installer._resolve_host", lambda host: ["192.0.2.1"]
        ), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1"]
        ):
            with self.assertRaisesRegex(InstallerError, "panel_listen_unsafe"):
                installer.preflight("example.com")

    def test_rejects_empty_panel_listen_default(self):
        installer = self._installer()

        with patch("clash_sub.installer.os.geteuid", return_value=0), patch(
            "clash_sub.installer._OS_RELEASE_PATH", self._os_release_debian()
        ), patch(
            "clash_sub.installer.read_xui_snapshot", lambda path: object()
        ), patch(
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/xui7k2m/", ""),
        ), patch(
            "clash_sub.installer._require_free_tcp_port", lambda installer_self, port: None
        ), patch(
            "clash_sub.installer._resolve_host", lambda host: ["192.0.2.1"]
        ), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1"]
        ):
            with self.assertRaisesRegex(InstallerError, "panel_listen_unsafe"):
                installer.preflight("example.com")

    def _os_release_debian(self):
        path = self.root / "os-release"
        path.write_text('ID="debian"\nVERSION_ID="12"\n', encoding="utf-8")
        return path

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
                installer._require_host_resolves_locally("sub.example.com")

    def test_accepts_matching_dns(self):
        installer = self._installer()

        with patch("clash_sub.installer._resolve_host", lambda host: ["192.0.2.1"]), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1", "10.0.0.5"]
        ):
            installer._require_host_resolves_locally("sub.example.com")


class LowMemoryPhaseTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        self.paths = InstallPaths(
            sysctl_conf=self.root / "99-clash-sub.conf",
            journald_conf_dir=self.root / "journald",
            swap_file=self.root / "swap.img",
            fstab=self.root / "fstab",
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
        fstab = self.paths.fstab.read_text(encoding="utf-8")
        self.assertIn("# clash-sub swap", fstab)
        self.assertIn(str(self.paths.swap_file), fstab)
        self.assertIn("%s none swap sw 0 0" % self.paths.swap_file, fstab)

    def test_does_not_duplicate_fstab_entry(self):
        entry = "# clash-sub swap\n%s none swap sw 0 0\n" % self.paths.swap_file
        self.paths.fstab.write_text(entry, encoding="utf-8")

        self._installer().optimize_low_memory(swap_mb=1024)

        fstab = self.paths.fstab.read_text(encoding="utf-8")
        self.assertEqual(fstab, entry)
        self.assertEqual(fstab.count("# clash-sub swap"), 1)

    def test_skips_creation_when_swap_already_active(self):
        self.paths.swap_file.write_bytes(b"")
        with patch("clash_sub.installer._swap_active", return_value=True):
            self._installer().optimize_low_memory(swap_mb=1024)

        joined = [" ".join(c) for c in self.runner_calls]
        self.assertFalse(any("fallocate" in item for item in joined))
        self.assertFalse(any("mkswap" in item for item in joined))
        self.assertFalse(any("swapon" in item for item in joined))
        fstab = self.paths.fstab.read_text(encoding="utf-8")
        self.assertIn("# clash-sub swap", fstab)
        self.assertIn("%s none swap sw 0 0" % self.paths.swap_file, fstab)

    def test_inactive_orphan_swap_file_is_recreated(self):
        self.paths.swap_file.write_bytes(b"")
        with patch("clash_sub.installer._swap_active", return_value=False):
            self._installer().optimize_low_memory(swap_mb=1024)

        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("fallocate" in item for item in joined))
        self.assertTrue(
            any("swapon" in item and str(self.paths.swap_file) in item for item in joined)
        )
        self.assertFalse(self.paths.swap_file.exists())

    def _failing_runner(self, keyword):
        def runner(arguments, **_):
            self.runner_calls.append(list(arguments))
            if keyword in arguments:
                return subprocess.CompletedProcess(arguments, 1)
            if arguments[0] == "fallocate":
                self.paths.swap_file.write_bytes(b"\0" * 1024)
            return subprocess.CompletedProcess(arguments, 0)

        return runner

    def test_failure_after_fallocate_cleans_up(self):
        installer = Installer(
            self.root, paths=self.paths, runner=self._failing_runner("mkswap")
        )

        with self.assertRaisesRegex(InstallerError, "command_failed"):
            installer.optimize_low_memory(swap_mb=1024)

        self.assertFalse(self.paths.swap_file.exists())
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("swapoff" in item for item in joined))

    def test_swap_failure_cleans_partial_file_and_rerun_completes(self):
        for keyword in ["chmod", "mkswap", "swapon"]:
            with self.subTest(keyword=keyword):
                self._assert_swap_failure_then_rerun(keyword)

    def _assert_swap_failure_then_rerun(self, keyword):
        with tempfile.TemporaryDirectory() as tempdir:
            root = (Path(tempdir) / "repo").resolve()
            (root / "private").mkdir(parents=True)
            paths = InstallPaths(
                sysctl_conf=root / "99-clash-sub.conf",
                journald_conf_dir=root / "journald",
                swap_file=root / "swap.img",
                fstab=root / "fstab",
            )
            calls = []
            armed = {"fail": True}

            def runner(arguments, **_):
                calls.append(list(arguments))
                if armed["fail"] and keyword in arguments:
                    armed["fail"] = False
                    return subprocess.CompletedProcess(arguments, 1)
                if arguments[0] == "fallocate":
                    paths.swap_file.write_bytes(b"\0" * 1024)
                return subprocess.CompletedProcess(arguments, 0)

            installer = Installer(root, paths=paths, runner=runner)
            with self.assertRaisesRegex(InstallerError, "command_failed"):
                installer.optimize_low_memory(swap_mb=1024)
            self.assertFalse(paths.swap_file.exists())

            first_run_calls = len(calls)
            installer.optimize_low_memory(swap_mb=1024)

            second_run = [" ".join(c) for c in calls[first_run_calls:]]
            self.assertTrue(any("fallocate" in item for item in second_run))
            self.assertTrue(any("swapon" in item for item in second_run))
            self.assertIn(
                "low_memory",
                load_install_state(root / "private" / "install-state.json").phases_done,
            )
            fstab = paths.fstab.read_text(encoding="utf-8")
            self.assertEqual(fstab.count("# clash-sub swap"), 1)

    def test_fstab_failure_cleans_up_and_rerun(self):
        real_write = Installer._write_fstab_entry
        flaky = {"armed": True}

        def flaky_write(installer_self):
            if flaky["armed"]:
                flaky["armed"] = False
                raise InstallerError("command_failed")
            real_write(installer_self)

        def runner(arguments, **_):
            self.runner_calls.append(list(arguments))
            if arguments[0] == "fallocate":
                self.paths.swap_file.write_bytes(b"\0" * 1024)
            return subprocess.CompletedProcess(arguments, 0)

        installer = Installer(self.root, paths=self.paths, runner=runner)
        with patch.object(Installer, "_write_fstab_entry", flaky_write):
            with self.assertRaisesRegex(InstallerError, "command_failed"):
                installer.optimize_low_memory(swap_mb=1024)
        self.assertFalse(self.paths.swap_file.exists())
        self.assertFalse(self.paths.fstab.exists())

        installer.optimize_low_memory(swap_mb=1024)

        self.assertIn(
            "low_memory",
            load_install_state(self.root / "private" / "install-state.json").phases_done,
        )
        fstab = self.paths.fstab.read_text(encoding="utf-8")
        self.assertEqual(fstab.count("# clash-sub swap"), 1)
        self.assertIn("%s none swap sw 0 0" % self.paths.swap_file, fstab)

    def test_swap_active_matches_proc_swaps_listing(self):
        content = (
            "Filename                                Type        Size    Used    Priority\n"
            "/dev/sda2                               partition   2048    0       -2\n"
            "%s                              file        1024    0       -3\n"
        )
        for expected, text in [
            (True, content % self.paths.swap_file),
            (False, content % "/other/swap.img"),
        ]:
            with self.subTest(expected=expected):
                handle = StringIO(text)
                with patch("builtins.open", return_value=handle):
                    self.assertEqual(_swap_active(self.paths.swap_file), expected)

    def test_swap_active_false_when_proc_swaps_unreadable(self):
        with patch("builtins.open", side_effect=OSError):
            self.assertFalse(_swap_active(self.paths.swap_file))

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


class DefaultSiteRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        base = Path(self.tempdir.name).resolve()
        self.available = base / "sites-available" / "default"
        self.available.parent.mkdir(parents=True)
        self.available.write_text("server { listen 80; }\n", encoding="utf-8")
        self.enabled_dir = base / "sites-enabled"
        self.enabled_dir.mkdir()
        self.paths = InstallPaths(nginx_conf=self.root / "nginx.conf")
        self.paths.nginx_conf.write_text("http {\n}\n", encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        return Installer(
            self.root,
            paths=self.paths,
            runner=lambda arguments, **_: subprocess.CompletedProcess(list(arguments), 0),
        )

    def test_removes_stock_default_site_symlink(self):
        installer = self._installer()
        enabled = self.enabled_dir / "default"
        enabled.symlink_to(self.available)

        self.assertTrue(installer._remove_default_site_at(enabled, self.available))
        self.assertFalse(enabled.exists() or enabled.is_symlink())
        self.assertTrue(self.available.exists())

    def test_keeps_non_default_link(self):
        installer = self._installer()
        other_target = self.enabled_dir.parent / "sites-available" / "other"
        other_target.write_text("# other\n", encoding="utf-8")
        enabled = self.enabled_dir / "default"
        enabled.symlink_to(other_target)

        self.assertFalse(installer._remove_default_site_at(enabled, self.available))
        self.assertTrue(enabled.is_symlink())

    def test_missing_link_is_noop(self):
        installer = self._installer()

        self.assertFalse(
            installer._remove_default_site_at(
                self.enabled_dir / "default", self.available
            )
        )

    def test_install_nginx_packages_removes_default_site(self):
        installer = self._installer()
        with patch.object(
            Installer, "_remove_default_site", return_value=True
        ) as remover:
            installer.install_nginx_packages()

        remover.assert_called_once()

    def test_install_records_default_site_removal_in_state(self):
        installer = self._installer()
        with patch.object(Installer, "_remove_default_site", return_value=True):
            installer.install_nginx_packages()

        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertTrue(state.default_site_removed)
        self.assertIn("nginx_packages", state.phases_done)


class StreamIncludeRemovalTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        self.stream_dir = self.root / "stream-conf.d"
        self.nginx_conf = self.root / "nginx.conf"
        self.paths = InstallPaths(nginx_conf=self.nginx_conf, stream_conf_dir=self.stream_dir)

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        return Installer(
            self.root,
            paths=self.paths,
            runner=lambda a, **_: subprocess.CompletedProcess(list(a), 0),
        )

    def _block(self):
        return "\n# clash-sub stream include\nstream {\n    include %s/*.conf;\n}\n" % self.stream_dir

    def test_removes_only_our_block_and_preserves_later_content(self):
        base = "user www-data;\nhttp {\n}\n"
        self.nginx_conf.write_text(base + self._block() + "# admin custom content\n", encoding="utf-8")
        installer = self._installer()

        self.assertTrue(installer._remove_stream_include())

        text = self.nginx_conf.read_text(encoding="utf-8")
        self.assertNotIn("clash-sub stream include", text)
        self.assertIn("# admin custom content", text)
        self.assertIn("user www-data;", text)

    def test_leaves_modified_block_untouched(self):
        base = "http {\n}\n"
        modified = "\n# clash-sub stream include\nstream {\n    include %s/*.conf;\n    # extra\n}\n" % self.stream_dir
        self.nginx_conf.write_text(base + modified, encoding="utf-8")
        installer = self._installer()

        self.assertFalse(installer._remove_stream_include())

        text = self.nginx_conf.read_text(encoding="utf-8")
        self.assertIn("# clash-sub stream include", text)
        self.assertIn("# extra", text)


class DefaultSiteRestoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        self.available = (Path(self.tempdir.name) / "sites-available" / "default")
        self.available.parent.mkdir(parents=True)
        self.available.write_text("server { listen 80; }\n", encoding="utf-8")
        self.enabled_dir = (Path(self.tempdir.name) / "sites-enabled")
        self.enabled_dir.mkdir()
        self.paths = InstallPaths(nginx_conf=self.root / "nginx.conf")

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        return Installer(
            self.root,
            paths=self.paths,
            runner=lambda a, **_: subprocess.CompletedProcess(list(a), 0),
        )

    def test_restores_removed_default_site(self):
        installer = self._installer()

        self.assertTrue(
            installer._restore_default_site_at(self.enabled_dir / "default", self.available)
        )
        self.assertTrue((self.enabled_dir / "default").is_symlink())

    def test_noop_when_link_exists_or_source_missing(self):
        installer = self._installer()
        self.assertFalse(
            installer._restore_default_site_at(
                self.enabled_dir / "default", Path("/nonexistent")
            )
        )


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
        (self.root / "bin").mkdir()
        (self.root / "bin" / "clash-sub").write_text("#!/bin/sh\n", encoding="utf-8")
        (self.root / "usr-local-bin").mkdir()
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
            cli_symlink=self.root / "usr-local-bin" / "clash-sub",
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

        installer.activate_nginx(
            domain="example.com", panel_port=2053, panel_base_path="/xui7k2m"
        )

        stream_text = self.paths.stream_conf().read_text(encoding="utf-8")
        http_text = self.paths.http_conf().read_text(encoding="utf-8")
        self.assertIn("sub.example.com", stream_text)
        self.assertIn("sub.example.com", http_text)
        self.assertIn("/xui7k2m", http_text)
        self.assertIn("127.0.0.1:10443", stream_text)
        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertIn("nginx_activation", state.phases_done)
        self.assertEqual(state.domain, "example.com")
        self.assertEqual(state.panel_port, 2053)
        self.assertEqual(state.panel_base_path, "/xui7k2m")
        self.assertEqual(
            state.files_written,
            [str(self.paths.stream_conf()), str(self.paths.http_conf()), str(self.paths.routes_conf)],
        )
        self.assertTrue(self.paths.routes_conf.exists())
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("nginx" in item and "-t" in item for item in joined))
        self.assertTrue(any("enable" in item and "nginx" in item for item in joined))

    def test_activate_nginx_strips_trailing_slash(self):
        installer = self._installer()

        installer.activate_nginx(
            domain="example.com", panel_port=2053, panel_base_path="/xui7k2m/"
        )

        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertEqual(state.panel_base_path, "/xui7k2m")
        self.assertIn(
            "location = /xui7k2m {",
            self.paths.http_conf().read_text(encoding="utf-8"),
        )

    def test_enable_failure_does_not_journal_phase(self):
        def failing_enable_runner(arguments, **_):
            self.runner_calls.append(list(arguments))
            if "enable" in arguments and "nginx" in arguments:
                return subprocess.CompletedProcess(arguments, 1)
            return subprocess.CompletedProcess(arguments, 0)

        installer = Installer(self.root, paths=self.paths, runner=failing_enable_runner)

        with self.assertRaisesRegex(InstallerError, "command_failed"):
            installer.activate_nginx(
                domain="example.com", panel_port=2053, panel_base_path="/xui7k2m"
            )

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


class CliSymlinkTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)
        (self.root / "bin").mkdir()
        (self.root / "bin" / "clash-sub").write_text("#!/bin/sh\n", encoding="utf-8")
        self.symlink_dir = Path(self.tempdir.name) / "usr-local-bin"
        self.symlink_dir.mkdir()
        self.paths = InstallPaths(
            systemd_dir=self.root / "systemd",
            cli_symlink=self.symlink_dir / "clash-sub",
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _installer(self):
        return Installer(
            self.root,
            paths=self.paths,
            runner=lambda a, **_: subprocess.CompletedProcess(list(a), 0),
        )

    def test_harden_systemd_creates_symlink_and_journals_it(self):
        installer = self._installer()

        installer.harden_systemd()

        link = self.symlink_dir / "clash-sub"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(), (self.root / "bin" / "clash-sub").resolve())
        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertIn(str(link), state.files_written)

    def test_harden_systemd_is_idempotent_for_existing_link(self):
        installer = self._installer()
        installer.harden_systemd()
        installer.harden_systemd()

        self.assertTrue((self.symlink_dir / "clash-sub").is_symlink())

    def test_missing_cli_entry_fails(self):
        (self.root / "bin" / "clash-sub").unlink()

        with self.assertRaisesRegex(InstallerError, "cli_entry_missing"):
            self._installer().harden_systemd()

    def test_install_cli_symlink_refuses_foreign_file(self):
        installer = self._installer()
        foreign = self.symlink_dir / "clash-sub"
        original = "#!/bin/sh\necho foreign\n"

        for kind in ("file", "symlink"):
            with self.subTest(kind=kind):
                if foreign.exists() or foreign.is_symlink():
                    foreign.unlink()
                if kind == "file":
                    foreign.write_text(original, encoding="utf-8")
                else:
                    other = self.symlink_dir / "other-target"
                    other.write_text("# other\n", encoding="utf-8")
                    foreign.symlink_to(other)

                with self.assertRaisesRegex(InstallerError, "cli_symlink_conflict"):
                    installer._install_cli_symlink()

                if kind == "file":
                    self.assertEqual(foreign.read_text(encoding="utf-8"), original)
                else:
                    self.assertTrue(foreign.is_symlink())

        with self.assertRaisesRegex(InstallerError, "cli_symlink_conflict"):
            installer.harden_systemd()


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
        self.assertIn("xui-public-endpoint: node.example.com:443", content)
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


class NodeHostTests(unittest.TestCase):
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

    def test_service_yaml_uses_node_subdomain_by_default(self):
        installer = Installer(self.root, paths=self.paths, runner=self._noop_runner)

        installer.initialize_subscription(domain="42io.cc", owner_email="owner-1")

        content = (self.root / "private" / "config" / "service.yaml").read_text(encoding="utf-8")
        self.assertIn("xui-public-endpoint: node.42io.cc:443", content)
        self.assertIn("subscription-authority: sub.42io.cc:443", content)

    def test_service_yaml_honors_explicit_node_host(self):
        installer = Installer(self.root, paths=self.paths, runner=self._noop_runner)

        installer.initialize_subscription(
            domain="42io.cc", owner_email="owner-1", node_host="proxy.42io.cc"
        )

        content = (self.root / "private" / "config" / "service.yaml").read_text(encoding="utf-8")
        self.assertIn("xui-public-endpoint: proxy.42io.cc:443", content)

    def test_preflight_checks_both_hosts_resolve_locally(self):
        installer = Installer(self.root, paths=self.paths, runner=self._noop_runner)
        resolved = {"sub.42io.cc": ["192.0.2.1"], "node.42io.cc": ["192.0.2.1"]}

        with patch("clash_sub.installer.os.geteuid", return_value=0), patch(
            "clash_sub.installer._OS_RELEASE_PATH", self._os_release()
        ), patch(
            "clash_sub.installer.read_xui_snapshot", lambda path: object()
        ), patch(
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/xui7k2m/", "127.0.0.1"),
        ), patch(
            "clash_sub.installer._require_free_tcp_port", lambda installer_self, port: None
        ), patch(
            "clash_sub.installer._resolve_host",
            lambda host: resolved.get(host, ["203.0.113.9"]),
        ), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1"]
        ):
            installer.preflight("42io.cc")

    def test_preflight_rejects_node_host_not_pointing_here(self):
        installer = Installer(self.root, paths=self.paths, runner=self._noop_runner)
        resolved = {"sub.42io.cc": ["192.0.2.1"], "node.42io.cc": ["203.0.113.9"]}

        with patch("clash_sub.installer.os.geteuid", return_value=0), patch(
            "clash_sub.installer._OS_RELEASE_PATH", self._os_release()
        ), patch(
            "clash_sub.installer.read_xui_snapshot", lambda path: object()
        ), patch(
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/xui7k2m/", "127.0.0.1"),
        ), patch(
            "clash_sub.installer._require_free_tcp_port", lambda installer_self, port: None
        ), patch(
            "clash_sub.installer._resolve_host",
            lambda host: resolved.get(host, ["203.0.113.9"]),
        ), patch(
            "clash_sub.installer._local_ipv4", lambda runner: ["192.0.2.1"]
        ):
            with self.assertRaisesRegex(InstallerError, "dns_mismatch"):
                installer.preflight("42io.cc")

    def _os_release(self):
        path = self.root / "os-release"
        path.write_text('ID="debian"\nVERSION_ID="12"\n', encoding="utf-8")
        return path


class OwnerValidationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private").mkdir(parents=True)

    def tearDown(self):
        self.tempdir.cleanup()

    def _noop_runner(self, arguments, **_):
        return subprocess.CompletedProcess(list(arguments), 0)

    def _install(self, owner_email, snapshot):
        installer = Installer(self.root, runner=self._noop_runner)
        with contextlib.ExitStack() as stack:
            for phase in (
                "preflight",
                "optimize_low_memory",
                "install_nginx_packages",
                "issue_certificate",
                "activate_nginx",
                "harden_systemd",
                "initialize_subscription",
            ):
                stack.enter_context(patch.object(Installer, phase))
            stack.enter_context(
                patch("clash_sub.installer.read_xui_snapshot", lambda path: snapshot)
            )
            stack.enter_context(
                patch(
                    "clash_sub.installer.read_panel_settings",
                    lambda path: (2053, "/xui7k2m/", "127.0.0.1"),
                )
            )
            return installer.install(
                domain="example.com", cf_token="t", owner_email=owner_email
            )

    def test_install_rejects_owner_not_matching_enabled_client(self):
        snapshot = fake_snapshot(FakeSnapshotClient("member@x", True))

        with self.assertRaisesRegex(InstallerError, "owner_email_invalid"):
            self._install("owner@x", snapshot)

        self.assertFalse((self.root / "private" / "install-state.json").exists())

    def test_install_rejects_owner_when_disabled(self):
        snapshot = fake_snapshot(
            FakeSnapshotClient("owner@x", False), FakeSnapshotClient("member@x", True)
        )

        with self.assertRaisesRegex(InstallerError, "owner_email_invalid"):
            self._install("owner@x", snapshot)

        self.assertFalse((self.root / "private" / "install-state.json").exists())

    def test_install_accepts_owner_among_multiple_enabled_clients(self):
        snapshot = fake_snapshot(
            FakeSnapshotClient("member-1@x", True),
            FakeSnapshotClient("owner@x", True),
            FakeSnapshotClient("member-2@x", True),
        )

        self._install("owner@x", snapshot)

        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertIn("report", state.phases_done)

    def test_install_maps_snapshot_failure_to_stable_error(self):
        def broken(path):
            raise XuiCompatibilityError("boom")

        installer = Installer(self.root, runner=self._noop_runner)
        with patch("clash_sub.installer.read_xui_snapshot", broken):
            with self.assertRaisesRegex(InstallerError, "xui_incompatible"):
                installer.install(
                    domain="example.com", cf_token="t", owner_email="owner@x"
                )

        self.assertFalse((self.root / "private" / "install-state.json").exists())


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
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/xui7k2m/", "127.0.0.1"),
        ), patch(
            "clash_sub.installer.read_xui_snapshot",
            lambda path: fake_snapshot(FakeSnapshotClient("owner-example", True)),
        ):
            installer.install(
                domain="example.com", cf_token="tok", swap_mb=0, owner_email="owner-example"
            )

        preflight.assert_not_called()
        low_memory.assert_not_called()
        self.assertFalse(any("preflight" in message for message in self.printed))
        pkg.assert_called_once()
        cert.assert_called_once_with("example.com", "tok")
        activate.assert_called_once_with(
            domain="example.com", panel_port=ANY, panel_base_path=ANY
        )
        harden.assert_called_once()
        init.assert_called_once_with(
            domain="example.com", owner_email="owner-example", node_host="node.example.com"
        )
        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertEqual(state.domain, "example.com")
        self.assertEqual(state.node_host, "node.example.com")
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
            cli_symlink=self.root / "usr-local-bin" / "clash-sub",
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
        systemd_units = (
            self.paths.systemd_dir / "clash-sub-traffic.service",
            self.paths.systemd_dir / "clash-sub-traffic.timer",
            self.paths.systemd_dir / "clash-sub-recover.service",
            self.paths.systemd_dir / "nginx.service.d" / "clash-sub-restart.conf",
            self.paths.systemd_dir / "nginx.service.d" / "clash-sub-recover.conf",
        )
        for unit in systemd_units:
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text("# unit\n", encoding="utf-8")
        self.paths.cli_symlink.parent.mkdir(parents=True, exist_ok=True)
        self.paths.cli_symlink.symlink_to(self.root / "bin" / "clash-sub")
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(
                domain="example.com",
                panel_port=2053,
                panel_base_path="/p-x",
                phases_done=["nginx_activation", "systemd_harden"],
                files_written=[
                    str(self.paths.stream_conf()),
                    str(self.paths.http_conf()),
                    str(self.paths.cli_symlink),
                ]
                + [str(unit) for unit in systemd_units],
                backups={},
                default_site_removed=True,
            ),
        )

        with patch.object(
            Installer, "_restore_default_site", return_value=True
        ) as restore:
            installer.rollback_install()

        restore.assert_called_once()
        self.assertFalse(self.paths.stream_conf().exists())
        self.assertFalse(self.paths.http_conf().exists())
        self.assertFalse(self.paths.cli_symlink.exists() or self.paths.cli_symlink.is_symlink())
        text = self.paths.nginx_conf.read_text(encoding="utf-8")
        self.assertNotIn("clash-sub stream include", text)
        self.assertIn("http {", text)
        for unit in systemd_units:
            self.assertFalse(unit.exists())
        self.assertFalse((self.root / "private" / "install-state.json").exists())
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("stop" in item and "nginx" in item for item in joined))
        self.assertTrue(any("disable" in item and "nginx" in item for item in joined))
        self.assertTrue(any("clash-sub-traffic.timer" in item for item in joined))
        self.assertTrue(any("daemon-reload" in item for item in joined))

    def test_rollback_without_journal_leaves_files(self):
        installer = self._installer()

        installer.rollback_install()

        self.assertEqual(self.runner_calls, [])
        self.assertTrue(self.paths.stream_conf().exists())
        text = self.paths.nginx_conf.read_text(encoding="utf-8")
        self.assertIn("clash-sub stream include", text)

    def test_rollback_with_empty_journal_touches_nothing(self):
        decoy_conf = "user www-data;\nhttp {\n    include /etc/nginx/conf.d/*.conf;\n}\n"
        self.paths.nginx_conf.write_text(decoy_conf, encoding="utf-8")
        decoy_unit = self.paths.systemd_dir / "clash-sub-traffic.service"
        decoy_unit.parent.mkdir(parents=True, exist_ok=True)
        decoy_unit.write_text("# operator managed unit\n", encoding="utf-8")
        installer = self._installer()
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(),
        )

        installer.rollback_install()

        self.assertEqual(self.runner_calls, [])
        self.assertEqual(self.paths.nginx_conf.read_text(encoding="utf-8"), decoy_conf)
        self.assertEqual(
            decoy_unit.read_text(encoding="utf-8"), "# operator managed unit\n"
        )
        self.assertEqual(self.paths.stream_conf().read_text(encoding="utf-8"), "# stream\n")
        self.assertEqual(self.paths.http_conf().read_text(encoding="utf-8"), "# http\n")
        self.assertFalse((self.root / "private" / "install-state.json").exists())

    def test_rollback_with_preflight_only_journal_touches_nothing(self):
        decoy_conf = "user www-data;\nhttp {\n    include /etc/nginx/conf.d/*.conf;\n}\n"
        self.paths.nginx_conf.write_text(decoy_conf, encoding="utf-8")
        decoy_unit = self.paths.systemd_dir / "clash-sub-traffic.service"
        decoy_unit.parent.mkdir(parents=True, exist_ok=True)
        decoy_unit.write_text("# operator managed unit\n", encoding="utf-8")
        installer = self._installer()
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(
                domain="example.com",
                phases_done=["preflight"],
                files_written=[],
            ),
        )

        installer.rollback_install()

        self.assertEqual(self.runner_calls, [])
        self.assertEqual(self.paths.nginx_conf.read_text(encoding="utf-8"), decoy_conf)
        self.assertEqual(
            decoy_unit.read_text(encoding="utf-8"), "# operator managed unit\n"
        )
        self.assertTrue(self.paths.stream_conf().exists())
        self.assertTrue(self.paths.http_conf().exists())
        self.assertFalse((self.root / "private" / "install-state.json").exists())

    def test_rollback_restores_replaced_unit_content(self):
        (self.root / "bin").mkdir()
        (self.root / "bin" / "clash-sub").write_text("#!/bin/sh\n", encoding="utf-8")
        self.paths.cli_symlink.parent.mkdir(parents=True, exist_ok=True)
        unit = self.paths.systemd_dir / "clash-sub-traffic.service"
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text("# original unit\n", encoding="utf-8")
        os.chmod(unit, 0o600)
        installer = self._installer()

        installer.harden_systemd()

        state = load_install_state(self.root / "private" / "install-state.json")
        self.assertIn(str(unit), state.replaced_files)
        self.assertIn("systemd_harden", state.phases_done)
        self.assertIn(str(unit), state.files_written)
        self.assertEqual(unit.stat().st_mode & 0o777, 0o644)

        installer.rollback_install()

        self.assertEqual(unit.read_text(encoding="utf-8"), "# original unit\n")
        self.assertEqual(unit.stat().st_mode & 0o777, 0o600)
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("stop" in item and "nginx" in item for item in joined))
        self.assertTrue(any("disable" in item and "nginx" in item for item in joined))

    def test_rollback_without_default_site_removal_does_not_restore(self):
        installer = self._installer()
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(
                domain="example.com",
                phases_done=["nginx_packages", "nginx_activation"],
                files_written=[],
            ),
        )

        with patch.object(Installer, "_restore_default_site") as restore:
            installer.rollback_install()

        restore.assert_not_called()
        self.assertFalse((self.root / "private" / "install-state.json").exists())

    def test_sweep_removes_unjournaled_marker_confs(self):
        marker = "# Managed by clash-sub install. SNI routing.\n"
        self.paths.stream_conf().write_text(marker + "stream {\n}\n", encoding="utf-8")
        self.paths.http_conf().write_text(marker + "server {\n}\n", encoding="utf-8")
        (self.root / "bin").mkdir()
        (self.root / "bin" / "clash-sub").write_text("#!/bin/sh\n", encoding="utf-8")
        self.paths.cli_symlink.parent.mkdir(parents=True, exist_ok=True)
        self.paths.cli_symlink.symlink_to(self.root / "bin" / "clash-sub")
        installer = self._installer()
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(
                domain="example.com",
                phases_done=["nginx_activation"],
                files_written=[],
            ),
        )

        installer.rollback_install()

        self.assertFalse(self.paths.stream_conf().exists())
        self.assertFalse(self.paths.http_conf().exists())
        self.assertFalse(self.paths.cli_symlink.exists() or self.paths.cli_symlink.is_symlink())

        foreign_target = Path(self.tempdir.name) / "outside-repo-target"
        foreign_target.write_text("# foreign\n", encoding="utf-8")
        self.paths.cli_symlink.symlink_to(foreign_target)
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(
                domain="example.com",
                phases_done=["nginx_activation"],
                files_written=[],
            ),
        )

        installer.rollback_install()

        self.assertTrue(self.paths.cli_symlink.is_symlink())

    def test_sweep_restores_replaced_conf_content_when_unjournaled(self):
        foreign = "# operator stream config\n"
        self.paths.stream_conf().write_text(foreign, encoding="utf-8")
        os.chmod(self.paths.stream_conf(), 0o644)
        installer = self._installer()
        save_install_state(
            self.root / "private" / "install-state.json",
            InstallState(
                domain="example.com",
                phases_done=["nginx_activation"],
                files_written=[],
                replaced_files={
                    str(self.paths.stream_conf()): {
                        "content": base64.b64encode(foreign.encode("utf-8")).decode(
                            "ascii"
                        ),
                        "mode": 0o644,
                    }
                },
            ),
        )
        # Crash window: the installer overwrote the conf after recording the
        # replacement but before journaling the file in files_written.
        self.paths.stream_conf().write_text(
            "# Managed by clash-sub install. SNI routing.\nstream {\n}\n",
            encoding="utf-8",
        )

        installer.rollback_install()

        self.assertEqual(self.paths.stream_conf().read_text(encoding="utf-8"), foreign)

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
            InstallState(
                phases_done=["nginx_activation"],
                files_written=[str(self.paths.http_conf())],
            ),
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
        ), patch(
            "clash_sub.installer.read_xui_snapshot",
            lambda path: fake_snapshot(FakeSnapshotClient("owner-example", True)),
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
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/xui7k2m/", "127.0.0.1"),
        ), patch(
            "clash_sub.installer.read_xui_snapshot",
            lambda path: fake_snapshot(FakeSnapshotClient("owner-example", True)),
        ):
            installer.install(domain="same.com", cf_token="t")

        preflight.assert_not_called()
        pkg.assert_called_once()

    def test_panel_settings_maps_to_installer_error(self):
        installer = Installer(self.root, runner=self._noop_runner)

        def broken(path):
            raise XuiCompatibilityError("boom")

        with patch("clash_sub.installer.read_panel_settings", broken):
            with self.assertRaisesRegex(InstallerError, "xui_incompatible"):
                installer._panel_settings()

    def test_panel_settings_returns_port_base_path_and_listen(self):
        installer = Installer(self.root, runner=self._noop_runner)

        with patch(
            "clash_sub.installer.read_panel_settings",
            lambda path: (2053, "/xui7k2m/", "127.0.0.1"),
        ):
            self.assertEqual(
                installer._panel_settings(), (2053, "/xui7k2m/", "127.0.0.1")
            )


if __name__ == "__main__":
    unittest.main()
