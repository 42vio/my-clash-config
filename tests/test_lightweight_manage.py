import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


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

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        completed = subprocess.CompletedProcess(list(arguments), 0)
        completed.stdout = (self.root.as_posix() + "\n").encode()
        return completed

    def test_creates_tarball_with_private_and_nginx_configs(self):
        from clash_sub.manage import create_backup

        nginx_conf = self.root / "etc-clash-sub.conf"
        with patch("clash_sub.manage._xui_database_path", return_value=None), patch(
            "clash_sub.manage._nginx_config_paths",
            return_value=(nginx_conf,),
        ), patch("clash_sub.manage._runtime_private_root", return_value=None):
            nginx_conf.write_text("# nginx\n", encoding="utf-8")
            path = create_backup(self.root, self._runner)

        self.assertTrue(path.exists())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(path.parent == (self.root / "backups"))
        with tarfile.open(path, "r:gz") as archive:
            names = archive.getnames()
        self.assertTrue(
            any(name.endswith("private/config/service.yaml") for name in names)
        )
        self.assertTrue(any(name.endswith("private/state.json") for name in names))
        self.assertTrue(any(name.endswith("etc-clash-sub.conf") for name in names))
        self.assertTrue(any(name == "clash-sub-versions.json" for name in names))
        self.assertFalse(any("install-state" in name for name in names))
        self.assertTrue(
            any("git" in " ".join(c) for c in self.runner_calls)
        )

    def test_creates_tarball_with_runtime_private_root(self):
        from clash_sub.manage import create_backup

        runtime_root = Path(self.tempdir.name) / "var-lib-private"
        (runtime_root / "releases").mkdir(parents=True)
        (runtime_root / "state.json").write_text("{}", encoding="utf-8")
        (runtime_root / "releases" / "r1.yaml").write_text("p\n", encoding="utf-8")

        with patch("clash_sub.manage._xui_database_path", return_value=None), patch(
            "clash_sub.manage._nginx_config_paths", return_value=()
        ), patch(
            "clash_sub.manage._runtime_private_root", return_value=runtime_root
        ):
            path = create_backup(self.root, self._runner)

        with tarfile.open(path, "r:gz") as archive:
            names = archive.getnames()
        self.assertTrue(any(name.endswith("state.json") for name in names))
        self.assertTrue(any(name.endswith("releases/r1.yaml") for name in names))
        self.assertTrue(any(name.endswith("private/config/service.yaml") for name in names))

    def test_backups_directory_is_private(self):
        from clash_sub.manage import create_backup

        with patch("clash_sub.manage._xui_database_path", return_value=None), patch(
            "clash_sub.manage._nginx_config_paths", return_value=()
        ), patch("clash_sub.manage._runtime_private_root", return_value=None):
            create_backup(self.root, self._runner)

        self.assertEqual((self.root / "backups").stat().st_mode & 0o777, 0o700)

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


if __name__ == "__main__":
    unittest.main()
