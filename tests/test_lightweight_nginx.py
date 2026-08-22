import base64
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from clash_sub.domain import RuntimeState, ServiceConfig, UserState, XuiClient
from clash_sub.state import load_state, save_state

try:
    from clash_sub.nginx import NginxError, activate_runtime, render_routes
except ImportError:
    NginxError = RuntimeError
    activate_runtime = None
    render_routes = None


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
        root = Path(self.tempdir.name)
        self.private_root = root / "private"
        self.private_root.mkdir(mode=0o700)
        self.public_root = root / "public"
        self.routes = root / "nginx" / "routes.conf"
        self.routes.parent.mkdir()
        self.config = ServiceConfig(
            owner_email="owner@example.invalid",
            subscription_authority="sub.example.invalid:8443",
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

    def tearDown(self):
        self.tempdir.cleanup()

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
        self.assertIn('add_header Profile-Title "Clash Standard" always;', text)
        self.assertIn("add_header Content-Disposition 'attachment; filename=\"Clash-Standard.yaml\"' always;", text)
        self.assertIn('add_header Subscription-Userinfo "upload=5; download=6; total=7; expire=8" always;', text)
        self.assertIn('if ($args != "") { return 404; }', text)
        self.assertIn("limit_req zone=clash_subscription burst=5 nodelay;", text)
        self.assertIn("client_max_body_size 1k;", text)
        self.assertIn("limit_except GET HEAD { deny all; }", text)
        self.assertIn("access_log off;", text)
        self.assertIn("log_not_found off;", text)
        self.assertIn('default_type "text/yaml; charset=utf-8";', text)
        self.assertIn("add_header X-Content-Type-Options nosniff always;", text)
        self.assertIn("add_header Cache-Control no-store always;", text)

    def test_routes_reject_a_symlinked_release_ancestor_without_exposing_the_token(self):
        self.assertIsNotNone(render_routes, "Nginx routes are not implemented")
        releases = self.public_root / "releases"
        saved = self.public_root / "saved-releases"
        releases.rename(saved)
        releases.symlink_to(saved, target_is_directory=True)

        with self.assertRaisesRegex(NginxError, "release path") as error:
            render_routes(self.config, self.state, (self.owner, self.member, self.disabled))

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

    def test_failed_nginx_test_restores_prior_bytes_and_never_reloads(self):
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
        self.assertEqual([call[0] for call in runner.calls], [("/usr/sbin/nginx", "-t")])
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


if __name__ == "__main__":
    unittest.main()
