import base64
import grp
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from clash_sub.domain import RuntimeState, ServiceConfig, UserState, XuiClient
from clash_sub.state import load_state, save_state
import clash_sub.nginx as nginx_module

try:
    from clash_sub.nginx import (
        NginxError,
        activate_runtime,
        recover_runtime,
        render_routes,
        render_stream_config,
        render_sub_server,
    )
except ImportError:
    NginxError = RuntimeError
    activate_runtime = None
    recover_runtime = None
    render_routes = None
    render_stream_config = None
    render_sub_server = None


def token(byte, code):
    return base64.urlsafe_b64encode(byte * 32).decode("ascii").rstrip("=") + "-" + code


class FakeRunner:
    def __init__(self, return_codes=()):
        self.return_codes = iter(return_codes)
        self.calls = []

    def __call__(self, arguments, **kwargs):
        self.calls.append((tuple(arguments), kwargs))
        return subprocess.CompletedProcess(arguments, next(self.return_codes, 0))


class LightweightNginxTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name).resolve()
        self.private_root = root / "private"
        self.private_root.mkdir(mode=0o700)
        self.public_root = root / "public"
        self.routes = root / "nginx" / "routes.conf"
        self.routes.parent.mkdir()
        self.config = ServiceConfig(
            owner_email="owner@example.invalid",
            subscription_authority="sub.example.invalid:8443",
            xui_public_endpoint="example.com:443",
            xui_database=root / "x-ui.db",
            private_root=self.private_root,
            public_root=self.public_root,
            nginx_routes=self.routes,
            mihomo_binary=Path("/opt/mihomo/mihomo"),
            nginx_binary=Path("/usr/sbin/nginx"),
            systemctl_binary=Path("/usr/bin/systemctl"),
            template_root=root / "templates",
        )
        self.owner_token = token(b"o", "ABCDEF")
        self.member_token = token(b"m", "GHJKMN")
        self.owner = XuiClient(7, "owner@example.invalid", "owner-sub", True, 1, 2, 3, 4000)
        self.member = XuiClient(8, "member@example.invalid", "member-sub", True, 5, 6, 7, 8000)
        self.disabled = XuiClient(9, "disabled@example.invalid", "disabled-sub", False, 0, 0, 0, 0)
        release = "2026-08-23T12-00-00Z-1234abcd"
        self.state = RuntimeState(
            1,
            7,
            {
                7: UserState(7, self.owner.email, self.owner_token, "ABCDEF", True, release),
                8: UserState(8, self.member.email, self.member_token, "GHJKMN", True, release),
                9: UserState(9, self.disabled.email, token(b"d", "PQRSTU"), "PQRSTU", False, release),
                10: UserState(10, "deleted@example.invalid", token(b"x", "VWXYZA"), "VWXYZA", True, release),
            },
        )
        for client_id, variants in ((7, ("balanced", "standard", "privacy")), (8, ("standard",))):
            directory = self.public_root / "releases" / str(client_id) / release
            directory.mkdir(parents=True)
            for variant in variants:
                path = directory / ("clash-%s.yaml" % variant)
                path.write_text("proxies: []\n", encoding="utf-8")
                os.chmod(path, 0o640)
        public_gid = grp.getgrnam("www-data").gr_gid if os.geteuid() == 0 else os.getegid()
        for directory in (self.public_root, *self.public_root.rglob("*")):
            if directory.is_dir():
                os.chown(directory, -1, public_gid)
                os.chmod(directory, 0o2750)
        for path in self.public_root.rglob("*.yaml"):
            os.chown(path, -1, public_gid)

    def tearDown(self):
        self.tempdir.cleanup()

    def _activation_artifacts(self):
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        old_state = state_path.read_bytes()
        self.routes.write_bytes(b"old routes\n")
        current_path = self.private_root / "current" / "7"
        current_path.parent.mkdir()
        os.chmod(current_path.parent, 0o700)
        current_path.write_bytes(b"old release\n")
        os.chmod(current_path, 0o600)
        return state_path, old_state, current_path

    def test_routes_are_exact_anonymous_and_limited_to_authorized_variants(self):
        self.assertIsNotNone(render_routes, "Nginx routes are not implemented")

        text = render_routes(self.config, self.state, (self.owner, self.member, self.disabled))

        self.assertIn("location = /s/%s/clash-balanced.yaml" % self.owner_token, text)
        self.assertIn("location = /s/%s/clash-standard.yaml" % self.owner_token, text)
        self.assertIn("location = /s/%s/clash-privacy.yaml" % self.owner_token, text)
        self.assertIn("location = /s/%s/clash-standard.yaml" % self.member_token, text)
        self.assertNotIn("location = /s/%s/clash-balanced.yaml" % self.member_token, text)
        self.assertNotIn("location = /s/%s/clash-privacy.yaml" % self.member_token, text)
        self.assertNotIn("location /s/", text)
        self.assertNotIn("/s/ABCDEF/", text)
        self.assertNotIn("deleted@example.invalid", text)
        self.assertNotIn(self.owner.email, text)
        self.assertNotIn(self.member.email, text)
        self.assertNotIn(self.disabled.email, text)
        self.assertIn("alias %s;" % (self.public_root / "releases" / "8" / self.state.users[8].current_release / "clash-standard.yaml"), text)
        self.assertIn('if ($request_method !~ ^(GET|HEAD)$) { return 404; }', text)
        self.assertIn('add_header Profile-Title "Clash Standard";', text)
        self.assertIn("add_header Content-Disposition 'attachment; filename=\"Clash-Standard.yaml\"';", text)
        self.assertIn('add_header Subscription-Userinfo "upload=5; download=6; total=7; expire=8";', text)
        self.assertNotIn('add_header Profile-Title "Clash Standard" always;', text)
        self.assertNotIn("add_header Content-Disposition 'attachment; filename=\"Clash-Standard.yaml\"' always;", text)
        self.assertNotIn('add_header Subscription-Userinfo "upload=5; download=6; total=7; expire=8" always;', text)
        self.assertIn('if ($args != "") { return 404; }', text)
        self.assertIn("limit_req zone=clash_subscription burst=5 nodelay;", text)
        self.assertIn("client_max_body_size 1k;", text)
        self.assertNotIn("limit_except GET HEAD", text)
        self.assertIn("access_log off;", text)
        self.assertIn("log_not_found off;", text)
        self.assertIn('default_type "text/yaml; charset=utf-8";', text)
        self.assertIn("add_header X-Content-Type-Options nosniff always;", text)
        self.assertIn("add_header Cache-Control no-store always;", text)

    def test_routes_floor_non_second_expiry_milliseconds_in_the_userinfo_header(self):
        client_with_fractional_expiry = replace(self.member, expiry_ms=8123)

        text = render_routes(
            self.config,
            self.state,
            (self.owner, client_with_fractional_expiry, self.disabled),
        )

        self.assertIn(
            'add_header Subscription-Userinfo "upload=5; download=6; total=7; expire=8";',
            text,
        )

    def test_routes_reject_a_symlinked_release_ancestor_without_exposing_the_token(self):
        self.assertIsNotNone(render_routes, "Nginx routes are not implemented")
        releases = self.public_root / "releases"
        saved = self.public_root / "saved-releases"
        releases.rename(saved)
        releases.symlink_to(saved, target_is_directory=True)

        with self.assertRaisesRegex(NginxError, "release path") as error:
            render_routes(self.config, self.state, (self.owner, self.member, self.disabled))

        self.assertNotIn(self.member_token, str(error.exception))

    def test_routes_reject_a_hard_linked_release_alias_without_exposing_the_token(self):
        path = (
            self.public_root
            / "releases"
            / "8"
            / self.state.users[8].current_release
            / "clash-standard.yaml"
        )
        os.link(path, path.with_name("linked.yaml"))

        with self.assertRaisesRegex(NginxError, "release path") as error:
            render_routes(self.config, self.state, (self.owner, self.member, self.disabled))

        self.assertNotIn(self.member_token, str(error.exception))

    def test_routes_reject_public_alias_metacharacters_or_symlinked_ancestors(self):
        self.assertIsNotNone(render_routes, "Nginx routes are not implemented")
        invalid_names = (
            "with space",
            "bad;alias",
            "bad{alias",
            "bad}alias",
            'bad"alias',
            "bad'alias",
            "bad\\alias",
            "bad#alias",
            "bad$alias",
            *("bad%salias" % chr(code) for code in range(0x20)),
            "bad%salias" % chr(0x7F),
        )
        for name in invalid_names:
            with self.subTest(name=name):
                bad_config = replace(self.config, public_root=self.public_root.parent / name)
                with self.assertRaisesRegex(NginxError, "service configuration") as error:
                    render_routes(bad_config, self.state, (self.owner, self.member, self.disabled))
                self.assertNotIn(name, str(error.exception))
                self.assertNotIn(self.member_token, str(error.exception))

        dotted = replace(self.config, public_root=self.public_root / ".." / "public")
        with self.assertRaisesRegex(NginxError, "service configuration"):
            render_routes(dotted, self.state, (self.owner, self.member, self.disabled))

        linked_parent = Path(self.tempdir.name) / "linked-parent"
        linked_parent.symlink_to(Path(self.tempdir.name), target_is_directory=True)
        symlinked = replace(self.config, public_root=linked_parent / "public")
        with self.assertRaisesRegex(NginxError, "service configuration") as error:
            render_routes(symlinked, self.state, (self.owner, self.member, self.disabled))
        self.assertNotIn(self.member_token, str(error.exception))

    def test_activate_installs_state_routes_and_extra_only_after_a_silent_nginx_test(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        state_path = self.private_root / "state.json"
        old_state = RuntimeState(1, 7, {7: self.state.users[7]})
        save_state(state_path, old_state)
        self.routes.write_bytes(b"old routes\n")
        extra_path = self.private_root / "airport.json"
        extra_path.write_bytes(b"old airport\n")
        runner = FakeRunner((0, 0))

        activate_runtime(
            self.config,
            self.state,
            "new routes\n",
            runner,
            extra_replacements=((extra_path, b"new airport\n", 0o600),),
        )

        self.assertEqual(load_state(state_path), self.state)
        self.assertEqual(self.routes.read_bytes(), b"new routes\n")
        self.assertEqual(extra_path.read_bytes(), b"new airport\n")
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.routes.stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(extra_path.stat().st_mode), 0o600)
        self.assertEqual([call[0] for call in runner.calls], [("/usr/sbin/nginx", "-t"), ("/usr/bin/systemctl", "reload", "nginx")])
        for _, kwargs in runner.calls:
            self.assertIs(kwargs["stdin"], subprocess.DEVNULL)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(kwargs["timeout"], 30)
            self.assertFalse(kwargs["check"])

    def test_failed_nginx_test_restores_prior_bytes_then_revalidates_and_reloads_old_runtime(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        old_state = state_path.read_bytes()
        self.routes.write_bytes(b"old routes\n")
        extra_path = self.private_root / "airport.json"
        runner = FakeRunner((1,))

        with self.assertRaisesRegex(NginxError, "validation failed") as error:
            activate_runtime(
                self.config,
                self.state,
                "new routes\n",
                runner,
                extra_replacements=((extra_path, b"new airport\n", 0o600),),
            )

        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(self.routes.read_bytes(), b"old routes\n")
        self.assertFalse(extra_path.exists())
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/sbin/nginx", "-t"),
                ("/usr/sbin/nginx", "-t"),
                ("/usr/bin/systemctl", "reload", "nginx"),
            ],
        )
        self.assertNotIn(self.member_token, str(error.exception))
        self.assertNotIn("new routes", str(error.exception))

    def test_failed_reload_restores_all_artifacts_then_revalidates_and_reloads_old_config(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        old_state = state_path.read_bytes()
        self.routes.write_bytes(b"old routes\n")
        extra_path = self.private_root / "airport.json"
        extra_path.write_bytes(b"old airport\n")
        runner = FakeRunner((0, 1, 0, 0))

        with self.assertRaisesRegex(NginxError, "reload failed") as error:
            activate_runtime(
                self.config,
                self.state,
                "new routes\n",
                runner,
                extra_replacements=((extra_path, b"new airport\n", 0o600),),
            )

        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(self.routes.read_bytes(), b"old routes\n")
        self.assertEqual(extra_path.read_bytes(), b"old airport\n")
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/sbin/nginx", "-t"),
                ("/usr/bin/systemctl", "reload", "nginx"),
                ("/usr/sbin/nginx", "-t"),
                ("/usr/bin/systemctl", "reload", "nginx"),
            ],
        )
        self.assertNotIn(self.member_token, str(error.exception))
        self.assertNotIn("new routes", str(error.exception))

    def test_failed_reload_never_reloads_an_old_configuration_that_fails_validation(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        self.routes.write_bytes(b"old routes\n")
        runner = FakeRunner((0, 1, 1))

        with self.assertRaisesRegex(NginxError, "rollback failed"):
            activate_runtime(self.config, self.state, "new routes\n", runner)

        self.assertTrue((self.private_root / ".activation-journal.json").exists())
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/sbin/nginx", "-t"),
                ("/usr/bin/systemctl", "reload", "nginx"),
                ("/usr/sbin/nginx", "-t"),
            ],
        )

    def test_extra_replacements_reject_escape_and_symlinked_ancestors_without_writing_outside(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        outside = Path(self.tempdir.name) / "outside.json"
        outside.write_bytes(b"outside bytes\n")
        escaped = self.private_root / ".." / outside.name

        with self.assertRaisesRegex(NginxError, "extra replacements"):
            activate_runtime(
                self.config,
                self.state,
                "new routes\n",
                FakeRunner(),
                extra_replacements=((escaped, b"new outside\n"),),
            )
        self.assertEqual(outside.read_bytes(), b"outside bytes\n")

        outside_directory = Path(self.tempdir.name) / "outside-directory"
        outside_directory.mkdir()
        target = outside_directory / "airport.json"
        target.write_bytes(b"outside airport\n")
        linked = self.private_root / "linked"
        linked.symlink_to(outside_directory, target_is_directory=True)

        with self.assertRaisesRegex(NginxError, "extra replacements"):
            activate_runtime(
                self.config,
                self.state,
                "new routes\n",
                FakeRunner(),
                extra_replacements=((linked / "airport.json", b"new airport\n"),),
            )
        self.assertEqual(target.read_bytes(), b"outside airport\n")

    def test_failed_validation_restores_prior_modes(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        self.routes.write_bytes(b"old routes\n")
        os.chmod(self.routes, 0o644)
        extra_path = self.private_root / "airport.json"
        extra_path.write_bytes(b"old airport\n")
        os.chmod(extra_path, 0o640)

        with self.assertRaisesRegex(NginxError, "validation failed"):
            activate_runtime(
                self.config,
                self.state,
                "new routes\n",
                FakeRunner((1,)),
                extra_replacements=((extra_path, b"new airport\n"),),
            )

        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.routes.stat().st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(extra_path.stat().st_mode), 0o640)

    def test_partial_install_failure_restores_all_artifacts_and_removes_candidates(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        old_state = state_path.read_bytes()
        self.routes.write_bytes(b"old routes\n")
        old_routes = self.routes.read_bytes()
        real_replace = nginx_module.os.replace
        failed = False

        def fail_second_install(source, target):
            nonlocal failed
            if Path(target) == self.routes and not failed:
                failed = True
                raise OSError("replace failed")
            return real_replace(source, target)

        with patch("clash_sub.nginx.os.replace", side_effect=fail_second_install):
            with self.assertRaisesRegex(NginxError, "activation failed"):
                activate_runtime(self.config, self.state, "new routes\n", FakeRunner())

        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(self.routes.read_bytes(), old_routes)
        self.assertEqual(tuple(self.private_root.glob(".*")), ())
        self.assertEqual(tuple(self.routes.parent.glob(".*")), ())

    def test_candidate_write_failure_preserves_artifacts_and_removes_prior_candidates(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        old_state = state_path.read_bytes()
        self.routes.write_bytes(b"old routes\n")
        old_routes = self.routes.read_bytes()
        real_write_candidate = nginx_module._write_candidate
        calls = 0

        def fail_second_candidate(path, contents, mode):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("candidate write failed")
            return real_write_candidate(path, contents, mode)

        with patch("clash_sub.nginx._write_candidate", side_effect=fail_second_candidate):
            with self.assertRaisesRegex(NginxError, "activation failed"):
                activate_runtime(self.config, self.state, "new routes\n", FakeRunner())

        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(self.routes.read_bytes(), old_routes)
        self.assertEqual(tuple(self.private_root.glob(".*")), ())
        self.assertEqual(tuple(self.routes.parent.glob(".*")), ())

    def test_first_install_fsync_failure_restores_all_prior_artifacts_before_raising(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        old_state = state_path.read_bytes()
        self.routes.write_bytes(b"old routes\n")
        old_routes = self.routes.read_bytes()
        os.chmod(self.routes, 0o644)
        real_fsync_directory = nginx_module._fsync_directory
        calls = 0

        def fail_first_install_fsync(directory):
            nonlocal calls
            calls += 1
            if calls == 5:
                raise OSError("fsync failed")
            return real_fsync_directory(directory)

        with patch("clash_sub.nginx._fsync_directory", side_effect=fail_first_install_fsync):
            with self.assertRaisesRegex(NginxError, "activation failed"):
                activate_runtime(self.config, self.state, "new routes\n", FakeRunner())

        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(self.routes.read_bytes(), old_routes)
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.routes.stat().st_mode), 0o644)
        self.assertEqual(tuple(self.private_root.glob(".*")), ())
        self.assertEqual(tuple(self.routes.parent.glob(".*")), ())

    def test_termination_at_each_install_boundary_leaves_a_prepared_journal_that_recovers_old_runtime(self):
        self.assertIsNotNone(recover_runtime, "Nginx recovery is not implemented")
        state_path = self.private_root / "state.json"
        old_state = RuntimeState(1, 7, {7: self.state.users[7]})
        extra_path = self.private_root / "airport.json"
        journal = self.private_root / ".activation-journal.json"
        targets = (state_path, self.routes, extra_path)

        for target in targets:
            with self.subTest(target=target.name):
                save_state(state_path, old_state)
                old_state_bytes = state_path.read_bytes()
                self.routes.write_bytes(b"old routes\n")
                extra_path.write_bytes(b"old airport\n")
                real_replace = nginx_module.os.replace

                def terminate_after_target(source, destination):
                    result = real_replace(source, destination)
                    if Path(destination) == target:
                        raise KeyboardInterrupt
                    return result

                with patch("clash_sub.nginx.os.replace", side_effect=terminate_after_target):
                    with self.assertRaises(KeyboardInterrupt):
                        activate_runtime(
                            self.config,
                            self.state,
                            "new routes\n",
                            FakeRunner(),
                            extra_replacements=((extra_path, b"new airport\n", 0o600),),
                        )

                self.assertTrue(journal.is_file())
                self.assertEqual(stat.S_IMODE(journal.stat().st_mode), 0o600)
                recover_runtime(self.config, FakeRunner((0, 0)), reload=True)
                self.assertEqual(state_path.read_bytes(), old_state_bytes)
                self.assertEqual(self.routes.read_bytes(), b"old routes\n")
                self.assertEqual(extra_path.read_bytes(), b"old airport\n")
                self.assertFalse(journal.exists())

    def test_failed_prepared_recovery_keeps_its_private_journal_without_secret_output(self):
        self.assertIsNotNone(recover_runtime, "Nginx recovery is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        self.routes.write_bytes(b"old routes\n")
        journal = self.private_root / ".activation-journal.json"
        real_replace = nginx_module.os.replace

        def terminate_after_routes(source, destination):
            result = real_replace(source, destination)
            if Path(destination) == self.routes:
                raise KeyboardInterrupt
            return result

        with patch("clash_sub.nginx.os.replace", side_effect=terminate_after_routes):
            with self.assertRaises(KeyboardInterrupt):
                activate_runtime(self.config, self.state, "new routes\n", FakeRunner())

        with self.assertRaisesRegex(NginxError, "recovery failed") as caught:
            recover_runtime(self.config, FakeRunner((1,)), reload=True)

        self.assertTrue(journal.exists())
        self.assertNotIn(self.owner_token, str(caught.exception))
        self.assertNotIn("new routes", str(caught.exception))

    def test_failed_prepared_recovery_retains_journal_when_restore_cannot_replace(self):
        self.assertIsNotNone(recover_runtime, "Nginx recovery is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        self.routes.write_bytes(b"old routes\n")
        journal = self.private_root / ".activation-journal.json"
        real_replace = nginx_module.os.replace

        def terminate_after_routes(source, destination):
            result = real_replace(source, destination)
            if Path(destination) == self.routes:
                raise KeyboardInterrupt
            return result

        with patch("clash_sub.nginx.os.replace", side_effect=terminate_after_routes):
            with self.assertRaises(KeyboardInterrupt):
                activate_runtime(self.config, self.state, "new routes\n", FakeRunner())

        with patch("clash_sub.nginx.os.replace", side_effect=OSError("restore failed")):
            with self.assertRaisesRegex(NginxError, "recovery failed") as caught:
                recover_runtime(self.config, FakeRunner(), reload=True)

        self.assertTrue(journal.exists())
        self.assertNotIn(self.owner_token, str(caught.exception))
        self.assertNotIn("new routes", str(caught.exception))

    def test_committed_journal_recovery_keeps_new_runtime_without_an_nginx_command(self):
        self.assertIsNotNone(recover_runtime, "Nginx recovery is not implemented")
        state_path = self.private_root / "state.json"
        old_state = RuntimeState(1, 7, {7: self.state.users[7]})
        save_state(state_path, old_state)
        self.routes.write_bytes(b"old routes\n")
        snapshots = (
            (state_path, (True, state_path.read_bytes(), 0o600)),
            (self.routes, (True, self.routes.read_bytes(), 0o644)),
        )
        nginx_module._write_activation_journal(
            self.private_root / ".activation-journal.json",
            self.config,
            snapshots,
            "committed",
        )
        save_state(state_path, self.state)
        self.routes.write_bytes(b"new routes\n")
        runner = FakeRunner()

        recovered = recover_runtime(self.config, runner, reload=True)

        self.assertTrue(recovered)
        self.assertEqual(load_state(state_path), self.state)
        self.assertEqual(self.routes.read_bytes(), b"new routes\n")
        self.assertEqual(runner.calls, [])
        self.assertFalse((self.private_root / ".activation-journal.json").exists())

    def test_committed_journal_cleanup_failure_keeps_the_new_runtime_and_journal(self):
        self.assertIsNotNone(activate_runtime, "Nginx activation is not implemented")
        state_path = self.private_root / "state.json"
        save_state(state_path, RuntimeState(1, 7, {7: self.state.users[7]}))
        self.routes.write_bytes(b"old routes\n")
        journal = self.private_root / ".activation-journal.json"

        with patch(
            "clash_sub.nginx._remove_activation_journal",
            side_effect=nginx_module.NginxError("Nginx activation journal failed"),
        ):
            activate_runtime(self.config, self.state, "new routes\n", FakeRunner((0, 0)))

        self.assertEqual(load_state(state_path), self.state)
        self.assertEqual(self.routes.read_bytes(), b"new routes\n")
        self.assertTrue(journal.exists())

    def test_committed_journal_candidate_failure_restores_old_runtime_and_removes_prepared_journal(self):
        state_path, old_state, current_path = self._activation_artifacts()
        journal = self.private_root / ".activation-journal.json"
        runner = FakeRunner((0, 0, 0, 0))
        real_candidate = nginx_module._write_candidate
        real_load = nginx_module._load_activation_journal
        journal_writes = 0
        observed_phases = []

        def fail_committed_candidate(path, contents, mode):
            nonlocal journal_writes
            if Path(path) == journal:
                journal_writes += 1
                if journal_writes == 2:
                    raise OSError("committed candidate write failed")
            return real_candidate(path, contents, mode)

        def record_phase(journal_path, config):
            result = real_load(journal_path, config)
            observed_phases.append(result[0])
            return result

        with patch("clash_sub.nginx._write_candidate", side_effect=fail_committed_candidate), patch(
            "clash_sub.nginx._load_activation_journal", side_effect=record_phase
        ):
            with self.assertRaisesRegex(NginxError, "activation failed") as caught:
                activate_runtime(
                    self.config,
                    self.state,
                    "new routes\n",
                    runner,
                    extra_replacements=((current_path, b"new release\n", 0o600),),
                )

        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(self.routes.read_bytes(), b"old routes\n")
        self.assertEqual(current_path.read_bytes(), b"old release\n")
        self.assertEqual(set(observed_phases), {"prepared"})
        self.assertFalse(journal.exists())
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/sbin/nginx", "-t"),
                ("/usr/bin/systemctl", "reload", "nginx"),
                ("/usr/sbin/nginx", "-t"),
                ("/usr/bin/systemctl", "reload", "nginx"),
            ],
        )
        self.assertNotIn(self.member_token, str(caught.exception))
        self.assertNotIn("new routes", str(caught.exception))

    def test_committed_journal_rename_failure_restores_old_runtime_and_removes_prepared_journal(self):
        state_path, old_state, current_path = self._activation_artifacts()
        journal = self.private_root / ".activation-journal.json"
        runner = FakeRunner((0, 0, 0, 0))
        real_replace = nginx_module.os.replace
        real_load = nginx_module._load_activation_journal
        journal_replacements = 0
        observed_phases = []

        def fail_committed_rename(source, destination):
            nonlocal journal_replacements
            if Path(destination) == journal:
                journal_replacements += 1
                if journal_replacements == 2:
                    raise OSError("committed journal rename failed")
            return real_replace(source, destination)

        def record_phase(journal_path, config):
            result = real_load(journal_path, config)
            observed_phases.append(result[0])
            return result

        with patch("clash_sub.nginx.os.replace", side_effect=fail_committed_rename), patch(
            "clash_sub.nginx._load_activation_journal", side_effect=record_phase
        ):
            with self.assertRaisesRegex(NginxError, "activation failed") as caught:
                activate_runtime(
                    self.config,
                    self.state,
                    "new routes\n",
                    runner,
                    extra_replacements=((current_path, b"new release\n", 0o600),),
                )

        self.assertEqual(state_path.read_bytes(), old_state)
        self.assertEqual(self.routes.read_bytes(), b"old routes\n")
        self.assertEqual(current_path.read_bytes(), b"old release\n")
        self.assertEqual(set(observed_phases), {"prepared"})
        self.assertFalse(journal.exists())
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/sbin/nginx", "-t"),
                ("/usr/bin/systemctl", "reload", "nginx"),
                ("/usr/sbin/nginx", "-t"),
                ("/usr/bin/systemctl", "reload", "nginx"),
            ],
        )
        self.assertNotIn(self.member_token, str(caught.exception))
        self.assertNotIn("new routes", str(caught.exception))

    def test_committed_journal_fsync_failure_retries_before_keeping_the_new_runtime(self):
        state_path, _, current_path = self._activation_artifacts()
        journal = self.private_root / ".activation-journal.json"
        runner = FakeRunner((0, 0))
        real_fsync = nginx_module._fsync_directory
        observed_phases = []
        failed = False

        def fail_committed_fsync(directory):
            nonlocal failed
            if Path(directory) == self.private_root and not failed:
                try:
                    phase, _ = nginx_module._load_activation_journal(journal, self.config)
                except NginxError:
                    phase = None
                if phase == "committed":
                    failed = True
                    observed_phases.append(phase)
                    raise OSError("committed journal fsync failed")
            return real_fsync(directory)

        with patch("clash_sub.nginx._fsync_directory", side_effect=fail_committed_fsync):
            activate_runtime(
                self.config,
                self.state,
                "new routes\n",
                runner,
                extra_replacements=((current_path, b"new release\n", 0o600),),
            )

        self.assertTrue(failed)
        self.assertEqual(load_state(state_path), self.state)
        self.assertEqual(self.routes.read_bytes(), b"new routes\n")
        self.assertEqual(current_path.read_bytes(), b"new release\n")
        self.assertEqual(observed_phases, ["committed"])
        self.assertFalse(journal.exists())
        self.assertEqual(
            [call[0] for call in runner.calls],
            [
                ("/usr/sbin/nginx", "-t"),
                ("/usr/bin/systemctl", "reload", "nginx"),
            ],
        )


class NginxTemplateRenderTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.template_root = Path(self.tempdir.name) / "templates" / "nginx"
        self.template_root.mkdir(parents=True)
        source_root = Path(__file__).resolve().parents[1] / "templates" / "nginx"
        for template in source_root.iterdir():
            shutil.copy(template, self.template_root / template.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _config(self):
        return ServiceConfig(
            owner_email="owner-example",
            subscription_authority="sub.example.com:443",
            xui_public_endpoint="example.com:443",
            xui_database=Path("/etc/x-ui/x-ui.db"),
            private_root=Path("/var/lib/clash-sub/private"),
            public_root=Path("/var/lib/clash-sub/public"),
            nginx_routes=Path("/etc/nginx/clash-sub/routes.conf"),
            mihomo_binary=Path("/usr/local/lib/clash-sub/mihomo"),
            nginx_binary=Path("/usr/sbin/nginx"),
            systemctl_binary=Path("/usr/bin/systemctl"),
            template_root=self.template_root.parent,
        )

    def test_renders_stream_map_with_default_reality(self):
        rendered = render_stream_config(self._config(), "example.com")

        self.assertIn("map $ssl_preread_server_name", rendered)
        self.assertIn("sub.example.com", rendered)
        self.assertIn("127.0.0.1:30443", rendered)
        self.assertIn("trojan.example.com", rendered)
        self.assertIn("127.0.0.1:20443", rendered)
        self.assertIn("default", rendered)
        self.assertIn("127.0.0.1:10443", rendered)
        self.assertIn("ssl_preread on;", rendered)
        self.assertIn("listen 443;", rendered)

    def test_renders_sub_server_with_panel_and_routes(self):
        rendered = render_sub_server(
            self._config(),
            domain="example.com",
            panel_port=2053,
            panel_base_path="/p-1a2b3c4d",
            routes_include="/etc/nginx/clash-sub/routes.conf",
            fullchain="/etc/ssl/domain/fullchain.pem",
            privkey="/etc/ssl/domain/privkey.pem",
        )

        self.assertIn("listen 127.0.0.1:30443 ssl;", rendered)
        self.assertIn("server_name sub.example.com;", rendered)
        self.assertIn("ssl_certificate /etc/ssl/domain/fullchain.pem;", rendered)
        self.assertIn("include /etc/nginx/clash-sub/routes.conf;", rendered)
        self.assertIn("location = /p-1a2b3c4d {", rendered)
        self.assertIn("proxy_pass http://127.0.0.1:2053/p-1a2b3c4d/;", rendered)
        self.assertIn("limit_req_zone $binary_remote_addr zone=clash_subscription", rendered)


if __name__ == "__main__":
    unittest.main()
