import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clash_sub.installer import InstallPaths, InstallState
from clash_sub.domain import ServiceConfig
from clash_sub.service import _OperationLock


class MihomoManagementTests(unittest.TestCase):
    def test_upgrade_refuses_to_race_a_configuration_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            private_root = root / "runtime-private"
            private_root.mkdir(mode=0o700)
            config = ServiceConfig(
                "owner@example.test",
                "sub.example.test:443",
                "example.test:443",
                root / "xui.db",
                private_root,
                root / "public",
                root / "routes.conf",
                root / "mihomo",
                root / "nginx",
                root / "systemctl",
                root / "templates",
            )
            from clash_sub.manage import update_mihomo

            with patch("clash_sub.manage.load_config", return_value=config), patch(
                "clash_sub.manage.install_latest_mihomo"
            ) as install, _OperationLock(private_root / "operation.lock"):
                with self.assertRaisesRegex(RuntimeError, "operation_busy"):
                    update_mihomo(root, subprocess.run)

            install.assert_not_called()


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private" / "config").mkdir(parents=True)
        (self.root / "private" / "config" / "service.yaml").write_text(
            "schema-version: 2\n", encoding="utf-8"
        )
        (self.root / "private" / "state.json").write_text("{}", encoding="utf-8")
        self.runner_calls = []
        self.database = self.root / "backup-sources" / "x-ui.db"
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.database.write_bytes(b"database")
        self.stream_conf = self.root / "backup-sources" / "stream-conf.d" / "clash-sub.conf"
        self.stream_conf.parent.mkdir(parents=True, exist_ok=True)
        self.stream_conf.write_text("# stream\n", encoding="utf-8")
        self.http_conf = self.root / "backup-sources" / "conf.d" / "clash-sub.conf"
        self.http_conf.parent.mkdir(parents=True, exist_ok=True)
        self.http_conf.write_text("# http\n", encoding="utf-8")
        self.runtime_root = self.root / "var-lib-private"
        self.runtime_root.mkdir()
        (self.runtime_root / "state.json").write_text("{}", encoding="utf-8")
        (self.runtime_root / "current").mkdir()
        (self.runtime_root / "current" / "7").write_text("release\n", encoding="utf-8")

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        completed = subprocess.CompletedProcess(list(arguments), 0)
        completed.stdout = (self.root.as_posix() + "\n").encode()
        return completed

    _MISSING = object()

    def _patched_sources(self, database=_MISSING, nginx=_MISSING, runtime=_MISSING):
        return patch("clash_sub.manage._xui_database_path", return_value=self.database if database is self._MISSING else database), patch(
            "clash_sub.manage._nginx_config_paths",
            return_value=(self.stream_conf, self.http_conf) if nginx is self._MISSING else nginx,
        ), patch(
            "clash_sub.manage._runtime_private_root",
            return_value=self.runtime_root if runtime is self._MISSING else runtime,
        )

    def test_backup_contains_only_four_rebuild_files(self):
        from clash_sub.manage import create_backup

        patches = self._patched_sources()
        with patches[0], patches[1], patches[2]:
            path = create_backup(self.root, self._runner)

        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(path.parent == (self.root / "backups"))
        self.assertEqual((self.root / "backups").stat().st_mode & 0o777, 0o700)
        with tarfile.open(path, "r:gz") as archive:
            self.assertEqual(
                set(archive.getnames()),
                {
                    "etc/x-ui/x-ui.db",
                    "etc/nginx/stream-conf.d/clash-sub.conf",
                    "etc/nginx/conf.d/clash-sub.conf",
                    "var/lib/clash-sub/private/state.json",
                },
            )

    def test_backup_fails_incomplete_when_a_rebuild_file_is_missing(self):
        from clash_sub.manage import create_backup

        cases = (
            ("database", self._patched_sources(database=None)),
            ("stream conf", self._patched_sources(nginx=(self.root / "absent-stream.conf", self.http_conf))),
            ("http conf", self._patched_sources(nginx=(self.stream_conf, self.root / "absent-http.conf"))),
            ("state", self._patched_sources(runtime=None)),
        )
        for name, patches in cases:
            with self.subTest(name=name):
                with patches[0], patches[1], patches[2]:
                    with self.assertRaisesRegex(RuntimeError, "backup_incomplete"):
                        create_backup(self.root, self._runner)
                self.assertEqual(
                    tuple(entry.name for entry in (self.root / "backups").iterdir()),
                    (),
                )

    def test_snapshot_copies_live_configs(self):
        from clash_sub.manage import auto_snapshot

        nginx_conf = self.root / "etc-clash-sub.conf"
        nginx_conf.write_text("# nginx\n", encoding="utf-8")
        with patch("clash_sub.manage._nginx_config_paths", return_value=(nginx_conf,)):
            snapshot_dir = auto_snapshot(self.root, self._runner, label="pre-update")

        self.assertTrue(snapshot_dir.is_dir())
        self.assertTrue(snapshot_dir.name.startswith("2"))
        self.assertTrue(snapshot_dir.name.endswith("pre-update"))
        self.assertEqual(
            (snapshot_dir / "service.yaml").read_text(encoding="utf-8"),
            "schema-version: 2\n",
        )
        self.assertEqual(
            (snapshot_dir / "etc-clash-sub.conf").read_text(encoding="utf-8"),
            "# nginx\n",
        )

    def test_snapshot_preserves_same_named_nginx_files(self):
        from clash_sub.manage import auto_snapshot

        stream = self.root / "stream-conf.d" / "clash-sub.conf"; stream.parent.mkdir()
        http = self.root / "conf.d" / "clash-sub.conf"; http.parent.mkdir()
        stream.write_text("stream", encoding="utf-8"); http.write_text("http", encoding="utf-8")
        with patch("clash_sub.manage._nginx_config_paths", return_value=(stream, http)):
            snapshot = auto_snapshot(self.root, self._runner, label="pre-update")

        copied = [path.read_text(encoding="utf-8") for path in snapshot.rglob("clash-sub.conf")]
        self.assertCountEqual(copied, ["stream", "http"])


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        (self.root / "private" / "config").mkdir(parents=True)
        (self.root / "private" / "config" / "service.yaml").write_text(
            "schema-version: 2\n", encoding="utf-8"
        )
        self.runner_calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        return subprocess.CompletedProcess(list(arguments), 0)

    def _post_update_spawn_argv(self):
        return [
            str(self.root / ".venv" / "bin" / "python"),
            str(self.root / "bin" / "clash-sub"),
            "update",
            "--post-update",
        ]

    def test_update_snapshots_pulls_pips_and_spawns_post_update(self):
        from clash_sub.manage import run_update

        with patch("clash_sub.manage.auto_snapshot") as snapshot:
            run_update(self.root, self._runner)

        snapshot.assert_called_once()
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertTrue(any("git" in item and "pull" in item for item in joined))
        self.assertTrue(any("pip" in item and "install" in item for item in joined))
        self.assertIn(self._post_update_spawn_argv(), self.runner_calls)

    def test_update_spawns_post_update_process_after_pull(self):
        from clash_sub.manage import run_update

        with patch("clash_sub.manage.auto_snapshot"), patch(
            "clash_sub.installer.Installer.harden_systemd"
        ) as harden:
            run_update(self.root, self._runner)

        expected = self._post_update_spawn_argv()
        self.assertIn(expected, self.runner_calls)
        pull_indexes = [
            index for index, call in enumerate(self.runner_calls) if "pull" in call
        ]
        self.assertTrue(pull_indexes)
        self.assertGreater(
            self.runner_calls.index(expected), pull_indexes[0],
            "post-update child must be spawned after the git pull",
        )
        harden.assert_not_called()

    def test_update_post_failure_raises_stable_error(self):
        from clash_sub.manage import run_update

        def failing_spawn(arguments, **_):
            self.runner_calls.append(list(arguments))
            returncode = 1 if "--post-update" in arguments else 0
            return subprocess.CompletedProcess(list(arguments), returncode)

        with patch("clash_sub.manage.auto_snapshot") as snapshot:
            with self.assertRaisesRegex(RuntimeError, "post_update_failed"):
                run_update(self.root, failing_spawn)

        snapshot.assert_called_once()
        self.assertTrue(any("pull" in call for call in self.runner_calls))

    def test_post_update_runs_harden_and_rerender_without_git_or_spawn(self):
        from clash_sub.manage import run_post_update

        state = InstallState(domain="example.com", panel_port=2053, panel_base_path="/p-x")
        config = ServiceConfig("owner@example.com", "sub.example.com:443", "node.example.com:443", self.root / "xui.db", self.root / "runtime" / "private", self.root / "runtime" / "public", self.root / "nginx" / "routes.conf", Path("/bin/mihomo"), Path("/bin/nginx"), Path("/bin/systemctl"), self.root / "templates")
        with patch("clash_sub.manage.Installer") as installer, patch(
            "clash_sub.manage._rerender_nginx"
        ) as rerender, patch(
            "clash_sub.manage._load_install_state", return_value=state
        ), patch("clash_sub.manage.load_config", return_value=config), patch("clash_sub.manage.auto_snapshot") as snapshot:
            run_post_update(self.root, self._runner)

        installer.return_value.harden_systemd.assert_called_once()
        paths = installer.call_args.kwargs["paths"]
        self.assertEqual((paths.private_root, paths.public_root, paths.routes_conf), (config.private_root, config.public_root, config.nginx_routes))
        self.assertEqual(rerender.call_args.kwargs["paths"], paths)
        self.assertEqual(rerender.call_args.kwargs["config"], config)
        snapshot.assert_not_called()
        joined = [" ".join(c) for c in self.runner_calls]
        self.assertFalse(any("git" in item or "pull" in item for item in joined))
        self.assertFalse(any("--post-update" in item for item in joined))

    def test_post_update_invalid_config_has_no_system_side_effect(self):
        from clash_sub.manage import run_post_update

        with patch("clash_sub.manage.Installer") as installer, patch(
            "clash_sub.manage.load_config", side_effect=Exception("bad config")
        ), self.assertRaisesRegex(RuntimeError, "post_update_config_invalid"):
            run_post_update(self.root, self._runner)
        installer.assert_not_called()
        self.assertEqual(self.runner_calls, [])

    def test_rerender_uses_configured_runtime_paths_and_journal(self):
        from clash_sub.manage import _rerender_nginx

        state = InstallState(domain="example.com", panel_port=2053, panel_base_path="/p-x")
        config = ServiceConfig("owner@example.com", "sub.example.com:443", "node.example.com:443", self.root / "xui.db", self.root / "custom" / "private", self.root / "custom" / "public", self.root / "custom-nginx" / "routes.conf", Path("/bin/mihomo"), Path("/bin/nginx"), Path("/bin/systemctl"), self.root / "templates")
        paths = InstallPaths(xui_database=config.xui_database, private_root=config.private_root, public_root=config.public_root, routes_conf=config.nginx_routes)
        with patch("clash_sub.nginx.render_stream_config", return_value="stream") as stream, patch(
            "clash_sub.nginx.render_sub_server", return_value="server"
        ) as server, patch("clash_sub.nginx.activate_nginx_files") as activate:
            _rerender_nginx(self.root, self._runner, state, paths=paths, config=config)

        self.assertIs(stream.call_args.args[0], config)
        self.assertIs(server.call_args.args[0], config)
        self.assertEqual(activate.call_args.kwargs["journal_path"], config.private_root / ".nginx-rerender-journal.json")

    def test_update_pull_failure_does_not_spawn(self):
        from clash_sub.manage import run_update

        def failing_pull(arguments, **_):
            self.runner_calls.append(list(arguments))
            returncode = 1 if "pull" in arguments else 0
            return subprocess.CompletedProcess(list(arguments), returncode)

        with patch("clash_sub.manage.auto_snapshot"), patch(
            "clash_sub.installer.Installer.harden_systemd"
        ) as harden:
            with self.assertRaisesRegex(RuntimeError, "git_pull_failed"):
                run_update(self.root, failing_pull)

        self.assertFalse(any("--post-update" in call for call in self.runner_calls))
        harden.assert_not_called()

    def test_update_fails_when_pull_fails(self):
        from clash_sub.manage import run_update

        def failing_pull(arguments, **_):
            self.runner_calls.append(list(arguments))
            returncode = 1 if "pull" in arguments else 0
            return subprocess.CompletedProcess(list(arguments), returncode)

        with patch("clash_sub.manage.auto_snapshot"):
            with self.assertRaisesRegex(RuntimeError, "git_pull_failed"):
                run_update(self.root, failing_pull)


class CertTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = (Path(self.tempdir.name) / "repo").resolve()
        self.root.mkdir(parents=True)

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        result = subprocess.CompletedProcess(list(arguments), 0)
        if arguments[:1] == ["openssl"]:
            result.stdout = b"notAfter=Sep 25 12:00:00 2026 GMT\n"
        return result

    def test_cert_status_reports_expiry(self):
        from clash_sub.manage import cert_status

        fullchain = self.root / "fullchain.pem"
        fullchain.write_text("CERT", encoding="ascii")
        with patch("clash_sub.manage._fullchain_path", return_value=fullchain):
            status = cert_status(self.root, self._runner)

        self.assertTrue(status["present"])
        self.assertIn("notAfter", status["not_after"])

    def test_cert_status_absent_certificate(self):
        from clash_sub.manage import cert_status

        with patch("clash_sub.manage._fullchain_path", return_value=self.root / "missing.pem"):
            status = cert_status(self.root, self._runner)

        self.assertFalse(status["present"])
        self.assertEqual(status["not_after"], "unknown")

    def test_cert_renew_invokes_acme(self):
        from clash_sub.manage import cert_renew

        state = InstallState(domain="example.com")
        calls = []

        def runner(arguments, **_):
            calls.append(list(arguments))
            return subprocess.CompletedProcess(list(arguments), 0)

        with patch("clash_sub.manage._load_install_state", return_value=state):
            cert_renew(self.root, runner)

        self.assertTrue(any("--renew" in item for item in calls))

    def test_cert_renew_failure_raises(self):
        from clash_sub.manage import cert_renew

        def runner(arguments, **_):
            return subprocess.CompletedProcess(list(arguments), 1)

        state = InstallState(domain="example.com")
        with patch("clash_sub.manage._load_install_state", return_value=state):
            with self.assertRaisesRegex(RuntimeError, "cert_renew_failed"):
                cert_renew(self.root, runner)


class HealthReportTests(unittest.TestCase):
    def test_reports_units_and_cert(self):
        from clash_sub.manage import health_report

        def runner(arguments, **_):
            result = subprocess.CompletedProcess(list(arguments), 0)
            if "is-active" in arguments:
                result.stdout = b"active\n"
            elif "openssl" in arguments:
                result.stdout = b"notAfter=Sep 25 12:00:00 2026 GMT\n"
            return result

        root = Path(tempfile.mkdtemp())
        try:
            report = health_report(root, runner)
        finally:
            import shutil

            shutil.rmtree(root)

        self.assertEqual(report["units"]["nginx"], "active")
        self.assertEqual(report["units"]["x-ui"], "active")
        self.assertIn("days_left", report["certificate"])
        self.assertIsNotNone(report["certificate"]["days_left"])


if __name__ == "__main__":
    unittest.main()
