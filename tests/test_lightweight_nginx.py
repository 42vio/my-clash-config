import base64
import grp
import http.client
import json
import os
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from clash_sub import metadata_server
from clash_sub.domain import RuntimeState, ServiceConfig, Traffic, UserState, XuiClient
from clash_sub.state import load_state, save_state
import clash_sub.nginx as nginx_module

try:
    from clash_sub.nginx import (
        NginxError,
        activate_nginx_files,
        activate_runtime,
        recover_runtime,
        render_routes,
        render_stream_config,
        render_sub_server,
    )
except ImportError:
    NginxError = RuntimeError
    activate_nginx_files = None
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


_ARG_CONDITION_LINE = '    if ($arg_u ~ "^[0-9]{1,19}$") {'

_ARG_USERINFO_LINE = (
    '        add_header Subscription-Userinfo '
    '"upload=$arg_u; download=$arg_d; total=$arg_t; expire=$arg_e";'
)


def _location_blocks(text):
    """Map every ``location = ...`` header line to its full block text.

    Only a location's own closing brace is unindented (nested if-blocks
    close with an indented ``"}"``), so a block is simply the lines from
    its header to the first unindented closing brace.
    """
    blocks = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("location = ") and line.endswith(" {"):
            block = [line]
            index += 1
            while index < len(lines) and lines[index] != "}":
                block.append(lines[index])
                index += 1
            if index < len(lines):
                block.append("}")
            blocks[line[: -len(" {")]] = "\n".join(block)
        index += 1
    return blocks


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
        for client_id, variants in ((7, ("compat", "balance")), (8, ("compat",))):
            directory = self.public_root / "releases" / str(client_id) / release
            directory.mkdir(parents=True)
            for variant in variants:
                path = directory / (
                    "Clash-Compat.yaml" if variant == "compat" else "Clash-Balance.yaml"
                )
                path.write_text("proxies: []\n", encoding="utf-8")
                os.chmod(path, 0o640)
        provider_path = self.public_root / "provider" / "AmyTelecom.yaml"
        provider_path.parent.mkdir(parents=True)
        provider_path.write_text("proxies:\n- name: Amy\n", encoding="utf-8")
        os.chmod(provider_path, 0o640)
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

        self.assertIn("location = /s/%s/Clash-Compat.yaml" % self.owner_token, text)
        self.assertIn("location = /s/%s/Clash-Balance.yaml" % self.owner_token, text)
        self.assertIn("location = /s/%s/Clash-Compat.yaml" % self.member_token, text)
        self.assertNotIn("location = /s/%s/Clash-Balance.yaml" % self.member_token, text)
        self.assertNotIn("location /s/", text)
        self.assertNotIn("/s/ABCDEF/", text)
        self.assertNotIn("deleted@example.invalid", text)
        self.assertNotIn(self.owner.email, text)
        self.assertNotIn(self.member.email, text)
        self.assertNotIn(self.disabled.email, text)
        self.assertIn("alias %s;" % (self.public_root / "releases" / "8" / self.state.users[8].current_release / "Clash-Compat.yaml"), text)
        self.assertIn('if ($request_method !~ ^(GET|HEAD)$) { return 405; }', text)
        self.assertIn('if ($args != "") { return 400; }', text)
        self.assertIn("limit_req zone=clash_subscription burst=5 nodelay;", text)
        self.assertIn("limit_req_status 429;", text)
        self.assertIn("client_max_body_size 1k;", text)
        self.assertNotIn("limit_except GET HEAD", text)
        self.assertIn("access_log off;", text)
        self.assertIn("log_not_found off;", text)
        self.assertIn('default_type "text/yaml; charset=utf-8";', text)
        self.assertIn("add_header X-Content-Type-Options nosniff always;", text)
        self.assertIn("add_header Cache-Control no-store always;", text)

    def test_routes_ignore_client_traffic_entirely(self):
        baseline = render_routes(
            self.config, self.state, (self.owner, self.member, self.disabled)
        )
        busier = (
            self.owner,
            replace(
                self.member,
                upload=987654321,
                download=876543210,
                total=999999999,
                expiry_ms=123456789,
            ),
            self.disabled,
        )

        self.assertEqual(render_routes(self.config, self.state, busier), baseline)
        # Only the per-request arg-template line exists; no client traffic
        # value is ever baked into the routes, and the upstream variable
        # that nginx empties across the redirect appears nowhere.
        self.assertNotRegex(baseline, r"upload=[0-9]")
        self.assertNotRegex(baseline, r"expire=[0-9]")
        self.assertNotIn("$upstream_http", baseline)
        self.assertEqual(
            baseline.count(_ARG_USERINFO_LINE),
            4,  # owner compat + owner balance + provider + member compat
        )

    def test_the_metadata_socket_is_a_patchable_module_constant(self):
        self.assertEqual(nginx_module._METADATA_SOCKET, "/run/clash-sub/metadata.sock")

    def test_public_blocks_use_the_standard_proxy_chain_with_internal_fallback(self):
        text = render_routes(self.config, self.state, (self.owner, self.member, self.disabled))
        blocks = _location_blocks(text)
        expected_public = {
            "location = /s/%s/Clash-Compat.yaml" % self.owner_token: (
                "/profile/7/Clash-Compat.yaml",
                "/accel/7/Clash-Compat.yaml",
            ),
            "location = /s/%s/Clash-Balance.yaml" % self.owner_token: (
                "/profile/7/Clash-Balance.yaml",
                "/accel/7/Clash-Balance.yaml",
            ),
            "location = /s/%s/AmyTelecom.yaml" % self.owner_token: (
                "/airport/AmyTelecom.yaml",
                "/accel/provider/AmyTelecom.yaml",
            ),
            "location = /s/%s/Clash-Compat.yaml" % self.member_token: (
                "/profile/8/Clash-Compat.yaml",
                "/accel/8/Clash-Compat.yaml",
            ),
        }
        self.assertEqual(
            sorted(blocks),
            sorted(
                list(expected_public)
                + [
                    "location = /accel/7/Clash-Compat.yaml",
                    "location = /accel/7/Clash-Balance.yaml",
                    "location = /accel/provider/AmyTelecom.yaml",
                    "location = /accel/8/Clash-Compat.yaml",
                ]
            ),
        )
        for header, (upstream, fallback) in expected_public.items():
            with self.subTest(location=header):
                block = blocks[header]
                self.assertIn(
                    "proxy_pass http://unix:%s:%s;" % (nginx_module._METADATA_SOCKET, upstream),
                    block,
                )
                self.assertIn("proxy_pass_request_headers off;", block)
                self.assertIn("proxy_connect_timeout 1s;", block)
                self.assertIn("proxy_read_timeout 1s;", block)
                self.assertIn("proxy_intercept_errors on;", block)
                self.assertIn(
                    "error_page 404 500 502 503 504 =200 %s;" % fallback, block
                )
                self.assertNotIn("auth_request", block)
                self.assertNotIn("proxy_set_header", block)
                self.assertNotIn("Subscription-Userinfo", block)
                self.assertNotIn("alias ", block)
                # The guards keep their rejection codes disjoint from the
                # error_page set, so locally rejected requests can never
                # degrade into the file.
                self.assertIn('if ($request_method !~ ^(GET|HEAD)$) { return 405; }', block)
                self.assertIn('if ($args != "") { return 400; }', block)
                self.assertIn("limit_req zone=clash_subscription burst=5 nodelay;", block)
                self.assertIn("limit_req_status 429;", block)
                self.assertIn("client_max_body_size 1k;", block)
                self.assertIn("access_log off;", block)
                self.assertIn("log_not_found off;", block)

    def test_accel_locations_are_internal_and_carry_the_full_display_headers(self):
        text = render_routes(self.config, self.state, (self.owner, self.member, self.disabled))
        blocks = _location_blocks(text)
        release = self.state.users[7].current_release
        cases = {
            "location = /accel/7/Clash-Compat.yaml": (
                self.public_root / "releases" / "7" / release / "Clash-Compat.yaml",
                'add_header Profile-Title "Clash-Compat";',
                "add_header Content-Disposition 'attachment; filename=Clash-Compat.yaml';",
                'add_header Profile-Update-Interval "24";',
            ),
            "location = /accel/7/Clash-Balance.yaml": (
                self.public_root / "releases" / "7" / release / "Clash-Balance.yaml",
                'add_header Profile-Title "Clash-Balance";',
                "add_header Content-Disposition 'attachment; filename=Clash-Balance.yaml';",
                'add_header Profile-Update-Interval "24";',
            ),
            "location = /accel/8/Clash-Compat.yaml": (
                self.public_root / "releases" / "8" / release / "Clash-Compat.yaml",
                'add_header Profile-Title "Clash-Compat";',
                "add_header Content-Disposition 'attachment; filename=Clash-Compat.yaml';",
                'add_header Profile-Update-Interval "24";',
            ),
        }
        for header, (alias, *header_lines) in cases.items():
            with self.subTest(location=header):
                block = blocks[header]
                self.assertIn("    internal;", block)
                self.assertIn("alias %s;" % alias, block)
                self.assertIn('default_type "text/yaml; charset=utf-8";', block)
                for line in header_lines:
                    self.assertIn(line, block)
                self.assertNotIn('add_header Profile-Title "Clash-Compat" always;', block)
                self.assertNotIn(
                    "add_header Content-Disposition 'attachment; filename=Clash-Compat.yaml' always;",
                    block,
                )
                self.assertIn("add_header X-Content-Type-Options nosniff always;", block)
                self.assertIn("add_header Cache-Control no-store always;", block)
                self.assertNotIn("proxy_pass", block)
                # Nginx releases the upstream during the X-Accel-Redirect
                # internal redirect ($upstream_http_* is empty there), so
                # the service encodes the four traffic numbers as query
                # args on the redirect URI and only the if-block re-emits
                # the header.  When the if matches its add_header set
                # fully replaces the location-level set, so the whole
                # header set repeats inside it; with no args (the
                # error_page degradation jump, the no-traffic response)
                # only the location-level set applies, without any
                # traffic header.
                self.assertIn(_ARG_CONDITION_LINE, block)
                self.assertEqual(
                    [line for line in block.splitlines() if "Subscription-Userinfo" in line],
                    [_ARG_USERINFO_LINE],
                )
                self.assertEqual(
                    block.count('add_header Profile-Update-Interval "24";'), 2
                )
                self.assertEqual(
                    block.count("add_header X-Content-Type-Options nosniff always;"), 2
                )
                self.assertEqual(
                    block.count("add_header Cache-Control no-store always;"), 2
                )
        for block in blocks.values():
            # No static traffic value exists anywhere: the only traffic
            # line anywhere is the arg template inside the accel if-block.
            self.assertNotRegex(block, r"upload=[0-9]")
            self.assertNotRegex(block, r"expire=[0-9]")

    def _provider_alias(self):
        return self.public_root / "provider" / "AmyTelecom.yaml"

    def test_owner_routes_use_exact_case_and_stable_provider(self):
        alias = self._provider_alias()

        text = render_routes(self.config, self.state, (self.owner, self.member, self.disabled))

        block = "location = /s/%s/AmyTelecom.yaml {" % self.owner_token
        self.assertIn(block, text)
        self.assertEqual(text.count("location = /s/%s/" % self.owner_token), 3)
        self.assertEqual(text.count("location = /s/%s/" % self.member_token), 1)
        self.assertIn("alias %s;" % alias, text)
        blocks = _location_blocks(text)
        self.assertEqual(text.count("location = /accel/provider/AmyTelecom.yaml"), 1)
        provider_block = blocks["location = /accel/provider/AmyTelecom.yaml"]
        self.assertIn("    internal;", provider_block)
        self.assertIn("alias %s;" % alias, provider_block)
        self.assertIn('default_type "text/yaml; charset=utf-8";', provider_block)
        self.assertIn('add_header Profile-Title "AmyTelecom";', provider_block)
        self.assertIn(
            "add_header Content-Disposition 'attachment; filename=AmyTelecom.yaml';",
            provider_block,
        )
        self.assertIn(_ARG_CONDITION_LINE, provider_block)
        self.assertEqual(
            [line for line in provider_block.splitlines() if "Subscription-Userinfo" in line],
            [_ARG_USERINFO_LINE],
        )
        # The provider header set (no Profile-Update-Interval) repeats
        # in full inside the if-block.
        self.assertEqual(provider_block.count('add_header Profile-Title "AmyTelecom";'), 2)
        self.assertNotIn("Profile-Update-Interval", provider_block)
        self.assertEqual(
            provider_block.count("add_header X-Content-Type-Options nosniff always;"), 2
        )
        self.assertEqual(
            provider_block.count("add_header Cache-Control no-store always;"), 2
        )
        self.assertNotIn(self.owner.email, provider_block)
        self.assertNotIn("airport.example", text)

    def test_member_has_no_balance_or_provider_route(self):
        text = render_routes(self.config, self.state, (self.owner, self.member, self.disabled))

        member_lines = [
            line
            for line in text.splitlines()
            if "location = /s/%s/" % self.member_token in line
        ]
        self.assertEqual(
            member_lines,
            ["location = /s/%s/Clash-Compat.yaml {" % self.member_token],
        )
        self.assertEqual(
            [
                line
                for line in text.splitlines()
                if "location = /accel/8/" in line
            ],
            ["location = /accel/8/Clash-Compat.yaml {"],
        )
        self.assertNotIn("/s/%s/Clash-Balance.yaml" % self.member_token, text)
        self.assertNotIn("/s/%s/AmyTelecom.yaml" % self.member_token, text)
        self.assertNotIn("/accel/8/Clash-Balance.yaml", text)

    def test_owner_routes_require_the_stable_provider(self):
        self._provider_alias().unlink()

        with self.assertRaisesRegex(NginxError, "release path") as caught:
            render_routes(self.config, self.state, (self.owner, self.member, self.disabled))

        self.assertNotIn(self.owner_token, str(caught.exception))

    def test_provider_route_rejects_insecure_files_without_leaking_the_token(self):
        for name in ("mode", "symlink", "hard link", "directory"):
            with self.subTest(name=name):
                alias = self._provider_alias()
                if name == "mode":
                    os.chmod(alias, 0o644)
                elif name == "symlink":
                    alias.unlink()
                    alias.symlink_to(
                        self.public_root
                        / "releases"
                        / "7"
                        / self.state.users[7].current_release
                        / "Clash-Compat.yaml"
                    )
                elif name == "directory":
                    alias.unlink()
                    alias.mkdir()
                else:
                    os.link(alias, alias.with_name("linked.yaml"))
                with self.assertRaisesRegex(NginxError, "release path") as caught:
                    render_routes(
                        self.config, self.state, (self.owner, self.member, self.disabled)
                    )
                self.assertNotIn(self.owner_token, str(caught.exception))
                linked = alias.with_name("linked.yaml")
                if linked.exists() or linked.is_symlink():
                    linked.unlink()

    def test_provider_route_rejects_a_symlinked_provider_directory(self):
        real = self.public_root / "real-provider"
        real.mkdir()
        os.chown(real, -1, grp.getgrnam("www-data").gr_gid if os.geteuid() == 0 else os.getegid())
        os.chmod(real, 0o2750)
        provider = self.public_root / "provider"
        shutil.rmtree(provider)
        provider.symlink_to(real, target_is_directory=True)

        with self.assertRaisesRegex(NginxError, "release path") as caught:
            render_routes(self.config, self.state, (self.owner, self.member, self.disabled))

        self.assertNotIn(self.owner_token, str(caught.exception))

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
            / "Clash-Compat.yaml"
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
        self.assertNotIn("proxy_protocol", rendered)
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
        self.assertIn('proxy_set_header X-Forwarded-For "";', rendered)
        self.assertIn("limit_req_zone $binary_remote_addr zone=clash_subscription", rendered)

    def test_rejects_domain_with_config_syntax_characters(self):
        for domain in ("example.com }\nserver { listen 12345;", "exa mple.com", "example.com;", "localhost", ""):
            with self.subTest(domain=domain):
                with self.assertRaisesRegex(NginxError, "invalid domain"):
                    render_stream_config(self._config(), domain)

    def test_rejects_sub_server_parameter_abuse(self):
        kwargs = dict(
            domain="example.com",
            panel_port=2053,
            panel_base_path="/p-1a2b3c4d",
            routes_include="/etc/nginx/clash-sub/routes.conf",
            fullchain="/etc/ssl/domain/fullchain.pem",
            privkey="/etc/ssl/domain/privkey.pem",
        )
        cases = (
            ("panel_port", True),
            ("panel_port", 443),
            ("panel_port", 30443),
            ("panel_base_path", "/p/x"),
            ("panel_base_path", "/p-x/"),
            ("routes_include", "etc/nginx/routes.conf"),
            ("fullchain", "/etc/ssl/x;\n}"),
            ("privkey", "/key'#"),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(NginxError, "invalid sub server parameters"):
                    render_sub_server(self._config(), **{**kwargs, key: value})


class ActivateNginxFilesTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name).resolve()
        self.target = self.root / "conf.d" / "clash-sub.conf"
        self.target.parent.mkdir()
        self.runner_calls = []
        self.fail_validation = False

    def tearDown(self):
        self.tempdir.cleanup()

    def _runner(self, arguments, **_):
        self.runner_calls.append(list(arguments))
        returncode = 1 if (self.fail_validation and arguments[0] == "/usr/sbin/nginx") else 0
        return subprocess.CompletedProcess(arguments, returncode)

    def _activate(self, files, *, reload=False, journal_path=None):
        return activate_nginx_files(
            files,
            self._runner,
            nginx_binary="/usr/sbin/nginx",
            systemctl_binary="/usr/bin/systemctl",
            reload=reload,
            journal_path=journal_path,
        )

    def test_installs_new_file_and_runs_nginx_t(self):
        contents = b"# new\n"

        self._activate(((self.target, contents, 0o640),))

        self.assertEqual(self.target.read_bytes(), contents)
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(len(self.runner_calls), 1)
        self.assertEqual(self.runner_calls[0][:2], ["/usr/sbin/nginx", "-t"])

    def test_restores_previous_contents_when_validation_fails(self):
        self.target.write_text("# old\n", encoding="utf-8")
        os.chmod(self.target, 0o640)
        self.fail_validation = True

        with self.assertRaisesRegex(NginxError, "Nginx validation failed"):
            self._activate(((self.target, b"# new\n", 0o640),))

        self.assertEqual(self.target.read_text(encoding="utf-8"), "# old\n")

    def test_removes_new_file_when_validation_fails(self):
        self.fail_validation = True

        with self.assertRaisesRegex(NginxError, "Nginx validation failed"):
            self._activate(((self.target, b"# new\n", 0o640),))

        self.assertFalse(self.target.exists())

    def test_reloads_when_requested(self):
        self._activate(((self.target, b"# new\n", 0o640),), reload=True)

        self.assertEqual(
            [call[:3] for call in self.runner_calls],
            [["/usr/sbin/nginx", "-t"], ["/usr/bin/systemctl", "reload", "nginx"]],
        )

    def test_rejects_relative_path(self):
        with self.assertRaisesRegex(NginxError, "invalid nginx file"):
            self._activate(((Path("relative.conf"), b"x", 0o640),))

    def test_multi_file_install_rolls_back_mixed_targets(self):
        existing = self.root / "conf.d" / "existing.conf"
        existing.write_text("# keep\n", encoding="utf-8")
        os.chmod(existing, 0o600)
        fresh = self.root / "conf.d" / "fresh.conf"
        self.fail_validation = True

        with self.assertRaisesRegex(NginxError, "Nginx validation failed"):
            self._activate(
                (
                    (existing, b"# replaced\n", 0o640),
                    (fresh, b"# new\n", 0o640),
                )
            )

        self.assertEqual(existing.read_text(encoding="utf-8"), "# keep\n")
        self.assertEqual(existing.stat().st_mode & 0o777, 0o600)
        self.assertFalse(fresh.exists())
        self.assertEqual(
            [name for name in os.listdir(self.target.parent) if name.startswith(".")],
            [],
        )

    def test_reload_failure_restores_previous_contents(self):
        self.target.write_text("# old\n", encoding="utf-8")
        os.chmod(self.target, 0o640)

        def runner(arguments, **_):
            self.runner_calls.append(list(arguments))
            returncode = 1 if arguments[:2] == ["/usr/bin/systemctl", "reload"] else 0
            return subprocess.CompletedProcess(arguments, returncode)

        with self.assertRaisesRegex(NginxError, "Nginx reload failed"):
            activate_nginx_files(
                ((self.target, b"# new\n", 0o640),),
                runner,
                nginx_binary="/usr/sbin/nginx",
                systemctl_binary="/usr/bin/systemctl",
                reload=True,
            )

        self.assertEqual(self.target.read_text(encoding="utf-8"), "# old\n")

    def test_recovers_prepared_rerender_journal_before_next_activation(self):
        self.target.write_text("# old\n", encoding="utf-8"); os.chmod(self.target, 0o640)
        journal = self.root / ".nginx-rerender-journal.json"
        nginx_module._write_nginx_file_journal(
            journal, [(self.target.resolve(), (True, b"# old\n", 0o640))]
        )
        self.target.write_text("# half-written\n", encoding="utf-8")

        activate_nginx_files(
            ((self.target, b"# converged\n", 0o640),), self._runner,
            nginx_binary="/usr/sbin/nginx", journal_path=journal,
        )

        self.assertEqual(self.target.read_text(encoding="utf-8"), "# converged\n")
        self.assertFalse(journal.exists())

    def test_prepared_rerender_recovery_validates_and_reloads_before_new_write(self):
        self.target.write_text("# old\n", encoding="utf-8"); os.chmod(self.target, 0o640)
        journal = self.root / ".nginx-rerender-journal.json"
        nginx_module._write_nginx_file_journal(journal, [(self.target.resolve(), (True, b"# old\n", 0o640))])
        self.target.write_text("# interrupted\n", encoding="utf-8")

        self._activate(((self.target, b"# converged\n", 0o640),), reload=True, journal_path=journal)

        self.assertEqual(
            [call[:3] for call in self.runner_calls],
            [["/usr/sbin/nginx", "-t"], ["/usr/bin/systemctl", "reload", "nginx"], ["/usr/sbin/nginx", "-t"], ["/usr/bin/systemctl", "reload", "nginx"]],
        )
        self.assertEqual(self.target.read_bytes(), b"# converged\n")

    def test_prepared_rerender_recovery_reload_failure_preserves_journal_and_old_file(self):
        self.target.write_text("# old\n", encoding="utf-8"); os.chmod(self.target, 0o640)
        journal = self.root / ".nginx-rerender-journal.json"
        nginx_module._write_nginx_file_journal(journal, [(self.target.resolve(), (True, b"# old\n", 0o640))])
        self.target.write_text("# interrupted\n", encoding="utf-8")

        def runner(arguments, **_):
            return subprocess.CompletedProcess(arguments, 1 if arguments[0] == "/usr/bin/systemctl" else 0)

        with self.assertRaisesRegex(NginxError, "activation journal failed"):
            activate_nginx_files(((self.target, b"# new\n", 0o640),), runner, nginx_binary="/usr/sbin/nginx", systemctl_binary="/usr/bin/systemctl", reload=True, journal_path=journal)
        self.assertEqual(self.target.read_bytes(), b"# old\n")
        self.assertTrue(journal.exists())

    def test_committed_rerender_journal_keeps_successful_live_file_and_only_cleans_up(self):
        self.target.write_text("# old\n", encoding="utf-8"); os.chmod(self.target, 0o640)
        journal = self.root / ".nginx-rerender-journal.json"
        nginx_module._write_nginx_file_journal(journal, [(self.target.resolve(), (True, b"# old\n", 0o640))], phase="committed")
        self.target.write_text("# success\n", encoding="utf-8")

        activate_nginx_files(((self.target, b"# success\n", 0o640),), self._runner, nginx_binary="/usr/sbin/nginx", journal_path=journal)

        self.assertEqual(self.target.read_bytes(), b"# success\n")
        self.assertFalse(journal.exists())

    def test_rerender_rejects_symlink_journal_without_touching_target(self):
        outside = self.root / "outside.json"; outside.write_text("outside", encoding="utf-8")
        journal = self.root / ".nginx-rerender-journal.json"; journal.symlink_to(outside)

        with self.assertRaisesRegex(NginxError, "activation journal failed"):
            activate_nginx_files(((self.target, b"# new\n", 0o640),), self._runner, nginx_binary="/usr/sbin/nginx", journal_path=journal)
        self.assertFalse(self.target.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_committed_cleanup_failure_keeps_new_configuration_for_next_cleanup(self):
        self.target.write_text("# old\n", encoding="utf-8"); os.chmod(self.target, 0o640)
        journal = self.root / ".nginx-rerender-journal.json"
        original_remove = nginx_module._remove_nginx_file_journal
        calls = []

        def fail_once(path):
            calls.append(path)
            if len(calls) == 1:
                raise NginxError("Nginx activation journal failed")
            return original_remove(path)

        with patch("clash_sub.nginx._remove_nginx_file_journal", side_effect=fail_once):
            self._activate(((self.target, b"# new\n", 0o640),), reload=True, journal_path=journal)

        self.assertEqual(self.target.read_bytes(), b"# new\n")
        self.assertTrue(journal.exists())
        self.assertEqual(json.loads(journal.read_text(encoding="ascii"))["phase"], "committed")
        self.assertEqual([call[:3] for call in self.runner_calls], [["/usr/sbin/nginx", "-t"], ["/usr/bin/systemctl", "reload", "nginx"]])

        self._activate(((self.target, b"# newer\n", 0o640),), reload=True, journal_path=journal)
        self.assertEqual(self.target.read_bytes(), b"# newer\n")
        self.assertFalse(journal.exists())

    def test_rerender_journal_rejects_boolean_schema_version(self):
        journal = self.root / ".nginx-rerender-journal.json"
        payload = {"schema_version": True, "phase": "prepared", "targets": []}
        journal.write_text(json.dumps(payload), encoding="ascii"); os.chmod(journal, 0o600)

        with self.assertRaisesRegex(NginxError, "activation journal failed"):
            activate_nginx_files(((self.target, b"# new\n", 0o640),), self._runner, nginx_binary="/usr/sbin/nginx", journal_path=journal)

    def test_rerender_journal_replace_failure_removes_candidate(self):
        journal = self.root / ".nginx-rerender-journal.json"
        with patch("clash_sub.nginx.os.replace", side_effect=OSError):
            with self.assertRaisesRegex(NginxError, "activation journal failed"):
                nginx_module._write_nginx_file_journal(journal, [(self.target.resolve(), (False, b"", 0))])
        self.assertFalse(any("nginx-rerender-journal" in path.name for path in self.root.iterdir()))


def _nginx_candidate():
    """Return the raw CLASH_TEST_NGINX value, or None when unset.

    Presence alone gates the real-nginx tests (machines without nginx
    skip).  When the variable IS set the binary is verified in setUp and
    a broken path fails the test loudly instead of silently skipping.
    """
    return os.environ.get("CLASH_TEST_NGINX")


class _StaticStore:
    """Metadata store stub with fixed traffic answers (real server, no DB).

    The answers live in underscore-prefixed attributes: a same-named
    instance attribute would shadow the methods the server calls, turn
    every call into a TypeError, and silently degrade the airport answer
    to ``None``.
    """

    def __init__(self, profile_traffic, airport_traffic):
        self._profile_traffic = profile_traffic
        self._airport_traffic = airport_traffic

    def traffic_for(self, client_id):
        return self._profile_traffic

    def airport_traffic(self):
        return self._airport_traffic


def _permit_nginx_worker(path):
    """Make a freshly bound unix socket connectable by the nginx worker.

    A bound socket defaults to 0755 owned by the test user, so the worker
    (www-data under a root-run master) can never connect and every proxied
    request silently degrades through the error_page fallback — the exact
    trap that hid the traffic-header behaviour from this suite until it ran
    on a real Linux nginx.  Production does not have this problem: systemd
    creates the socket as 0660 root:www-data.
    """
    public_gid = grp.getgrnam("www-data").gr_gid if os.geteuid() == 0 else os.getegid()
    os.chown(path, -1, public_gid)
    os.chmod(path, 0o660)


class _RawUnixResponder(threading.Thread):
    """Bind a unix socket and answer every connection with fixed bytes.

    Any previous socket file at ``path`` is unlinked first so a fresh
    responder can take over the path after an earlier listener closed.
    ``response=None`` keeps each accepted connection open without replying
    (used to drive the proxy read timeout).
    """

    def __init__(self, path, response):
        super().__init__(daemon=True)
        Path(path).unlink(missing_ok=True)
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(path))
        self.listener.listen(8)
        _permit_nginx_worker(path)
        self.response = response

    def run(self):
        while True:
            try:
                connection, _ = self.listener.accept()
            except OSError:
                return
            try:
                if self.response is None:
                    time.sleep(30)
                else:
                    connection.sendall(self.response)
            except OSError:
                pass
            finally:
                try:
                    connection.close()
                except OSError:
                    pass

    def close(self):
        try:
            self.listener.close()
        except OSError:
            pass


class DegradationScenarioLifecycleTests(unittest.TestCase):
    """Ungated checks for the outage states the real-nginx tests simulate.

    These run everywhere: they pin the bind/close/unlink lifecycle the
    gated degradation scenarios depend on, so a regression in the harness
    itself cannot hide behind the CLASH_TEST_NGINX gate.
    """

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.socket_path = Path(temporary.name).resolve() / "degraded.sock"

    def _connect(self):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(self.socket_path))
        return client

    def test_a_stopped_listener_leaves_a_refusing_socket_file(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(1)
        listener.close()

        # The file remains with nobody accepting: connects are refused.
        self.assertTrue(self.socket_path.exists())
        with self.assertRaises(ConnectionRefusedError):
            self._connect()

    def test_an_unlinked_socket_path_is_absent(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.socket_path))
        listener.listen(1)
        listener.close()
        self.socket_path.unlink()

        with self.assertRaises(FileNotFoundError):
            self._connect()

    def test_responders_rebind_the_same_path_and_hold_silent_connections(self):
        canned = b"HTTP/1.0 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n"
        first = _RawUnixResponder(self.socket_path, canned)
        first.start()
        try:
            client = self._connect()
            self.assertEqual(client.recv(4096), canned)
            client.close()
        finally:
            first.close()

        # A dead socket file (above) must not block the next responder:
        # __init__ unlinks before binding, so the same path is reusable.
        second = _RawUnixResponder(self.socket_path, None)
        second.start()
        try:
            client = self._connect()
            client.settimeout(0.3)
            # A silent responder accepts but never sends a byte.
            with self.assertRaises(socket.timeout):
                client.recv(4096)
            client.close()
        finally:
            second.close()


@unittest.skipUnless(_nginx_candidate(), "CLASH_TEST_NGINX is not set (point it at a real nginx binary to run the real-nginx tests)")
class RealNginxSubscriptionTests(unittest.TestCase):
    """Drive the rendered proxy/X-Accel chain through a real nginx.

    Set ``CLASH_TEST_NGINX=/usr/sbin/nginx`` (any working binary) to run
    these.  When the variable is set but the binary is broken, the tests
    FAIL loudly rather than skip — a typo on a deployment box must not
    turn the whole suite silently green.  Each test renders routes with
    ``render_routes`` (only the metadata socket path is patched to a
    tempdir path — /run does not exist on macOS), starts the real
    metadata server from Task 5 on that unix socket, launches nginx with
    a minimal standalone configuration, and asserts over real HTTP.
    """

    release_id = "2026-08-23T12-00-00Z-1234abcd"

    def setUp(self):
        candidate = _nginx_candidate()
        if not candidate:
            self.skipTest("CLASH_TEST_NGINX is not set")
        self.nginx = self._verify_nginx(candidate)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        # The tempdir itself defaults to 0700 root:root, which blocks the
        # nginx worker (www-data) from traversing to the fixtures and the
        # metadata socket on Linux; the fixture subtrees below already carry
        # the production 2750 root:www-data discipline.
        os.chmod(self.root, 0o755)
        self._make_runtime()
        self._render_routes()
        self._write_nginx_config()
        self._start_metadata_server()
        self._start_nginx()

    @staticmethod
    def _verify_nginx(candidate):
        try:
            completed = subprocess.run(
                [candidate, "-v"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AssertionError(
                "CLASH_TEST_NGINX=%r is not a working nginx binary: %r"
                % (candidate, error)
            )
        if completed.returncode != 0:
            raise AssertionError(
                "CLASH_TEST_NGINX=%r is not a working nginx binary: "
                "'nginx -v' exited with %d" % (candidate, completed.returncode)
            )
        return candidate

    # -- fixture -------------------------------------------------------

    def _make_runtime(self):
        self.private_root = self.root / "private"
        self.private_root.mkdir(mode=0o700)
        os.chmod(self.private_root, 0o700)
        self.public_root = self.root / "public"
        self.public_root.mkdir()
        self.routes_path = self.root / "nginx" / "routes.conf"
        self.routes_path.parent.mkdir()
        self.owner_token = token(b"r", "RWXYZA")
        self.member_token = token(b"n", "ABCDEF")
        self.compat_bytes = b"# owner clash-compat profile\nproxies: []\n"
        self.balance_bytes = b"# owner clash-balance profile\nproxies: []\n"
        self.member_bytes = b"# member clash-compat profile\nproxies: []\n"
        self.provider_bytes = b"# AmyTelecom provider snapshot\nproxies: []\n"
        fixtures = {
            self.public_root / "releases" / "7" / self.release_id / "Clash-Compat.yaml": self.compat_bytes,
            self.public_root / "releases" / "7" / self.release_id / "Clash-Balance.yaml": self.balance_bytes,
            self.public_root / "releases" / "8" / self.release_id / "Clash-Compat.yaml": self.member_bytes,
            self.public_root / "provider" / "AmyTelecom.yaml": self.provider_bytes,
        }
        for path, body in fixtures.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            os.chmod(path, 0o640)
        public_gid = grp.getgrnam("www-data").gr_gid if os.geteuid() == 0 else os.getegid()
        for directory in (self.public_root, *self.public_root.rglob("*")):
            if directory.is_dir():
                os.chown(directory, -1, public_gid)
                os.chmod(directory, 0o2750)
        for path in fixtures:
            os.chown(path, -1, public_gid)
        self.config = ServiceConfig(
            owner_email="owner@example.invalid",
            subscription_authority="sub.example.invalid:8443",
            xui_public_endpoint="example.com:443",
            xui_database=self.root / "x-ui.db",
            private_root=self.private_root,
            public_root=self.public_root,
            nginx_routes=self.routes_path,
            mihomo_binary=Path("/opt/mihomo/mihomo"),
            nginx_binary=Path("/usr/sbin/nginx"),
            systemctl_binary=Path("/usr/bin/systemctl"),
            template_root=self.root / "templates",
        )
        self.state = RuntimeState(
            1,
            7,
            {
                7: UserState(7, "owner@example.invalid", self.owner_token, "RWXYZA", True, self.release_id),
                8: UserState(8, "member@example.invalid", self.member_token, "ABCDEF", True, self.release_id),
            },
        )
        self.owner_client = XuiClient(7, "owner@example.invalid", "owner-sub", True, 1, 2, 3, 4000)
        self.member_client = XuiClient(8, "member@example.invalid", "member-sub", True, 5, 6, 7, 8000)

    def _render_routes(self):
        self.metadata_socket = self.root / "metadata.sock"
        with patch.object(nginx_module, "_METADATA_SOCKET", str(self.metadata_socket)):
            self.routes_text = render_routes(
                self.config, self.state, (self.owner_client, self.member_client)
            )

    def _write_nginx_config(self):
        self.prefix = self.root / "nginx-run"
        self.prefix.mkdir()
        self.port = self._free_port()
        self.conf_path = self.prefix / "nginx.conf"
        # Without a user directive the worker falls back to the compiled
        # default (nobody), which cannot traverse the production-style
        # 2750 root:www-data fixture tree; only a root master may set it.
        user_directive = ("user www-data;",) if os.geteuid() == 0 else ()
        self.conf_path.write_text(
            "\n".join(
                (
                    *user_directive,
                    "worker_processes 1;",
                    "pid %s;" % (self.prefix / "nginx.pid"),
                    "error_log %s warn;" % (self.prefix / "error.log"),
                    "events { worker_connections 64; }",
                    "http {",
                    "    default_type text/yaml;",
                    "    access_log off;",
                    "    limit_req_zone $binary_remote_addr zone=clash_subscription:10m rate=100r/s;",
                    "    server {",
                    "        listen 127.0.0.1:%d;" % self.port,
                    "        server_name subscription.invalid;",
                    self.routes_text,
                    "        location / { return 404; }",
                    "    }",
                    "}",
                )
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _free_port():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]

    def _start_metadata_server(self):
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(self.metadata_socket))
        listener.listen(16)
        _permit_nginx_worker(self.metadata_socket)
        self.store = _StaticStore(
            Traffic(upload=112233, download=99887766, total=123456789, expiry_ms=55),
            Traffic(upload=1, download=2, total=3, expiry_ms=4),
        )
        self.metadata = metadata_server.MetadataSocketServer(self.store, listener)
        thread = threading.Thread(
            target=self.metadata.serve_forever, kwargs={"poll_interval": 0.05}
        )
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(self.metadata.server_close)
        self.addCleanup(self.metadata.shutdown)

    def _start_nginx(self):
        completed = subprocess.run(
            [self.nginx, "-c", str(self.conf_path), "-p", str(self.prefix)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        self.addCleanup(self._stop_nginx)
        self.assertEqual(
            completed.returncode,
            0,
            "nginx failed to start: %s" % completed.stderr.decode("utf-8", "replace"),
        )
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.1)
        self.fail("nginx did not start listening on 127.0.0.1:%d" % self.port)

    def _stop_nginx(self):
        for signal in ("quit", "stop"):
            subprocess.run(
                [self.nginx, "-c", str(self.conf_path), "-p", str(self.prefix), "-s", signal],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
                check=False,
            )
            pid_path = self.prefix / "nginx.pid"
            deadline = time.time() + 10
            while time.time() < deadline and pid_path.exists():
                time.sleep(0.1)
            if not pid_path.exists():
                return

    # -- helpers -------------------------------------------------------

    def _request(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            connection.request(method, path, headers=dict(headers or {}))
            response = connection.getresponse()
            body = response.read()
            return (
                response.status,
                {name.lower(): value for name, value in response.getheaders()},
                body,
            )
        finally:
            connection.close()

    def _degrade_metadata(self, scenario):
        """Take the metadata service down in one of four distinct ways.

        Returns a callable restoring a clean socket state (dead socket
        files removed, responders closed) so scenarios can run in
        sequence within one test.
        """
        self.metadata.shutdown()
        self.metadata.server_close()
        if scenario == "socket-refusing":
            # The stopped server leaves the socket file behind with nobody
            # listening: connects are refused (ECONNREFUSED -> nginx 502).
            return lambda: Path(self.metadata_socket).unlink(missing_ok=True)
        if scenario == "socket-absent":
            # No socket file at all (ENOENT -> nginx 502).
            Path(self.metadata_socket).unlink(missing_ok=True)
            return lambda: None
        if scenario == "upstream-500":
            # A live upstream answering 5xx (intercepted -> error_page).
            responder = _RawUnixResponder(
                self.metadata_socket,
                b"HTTP/1.0 500 Internal Server Error\r\nContent-Length: 0\r\n\r\n",
            )
        elif scenario == "upstream-timeout":
            # A live upstream accepting but never answering
            # (proxy_read_timeout -> 504 -> error_page).
            responder = _RawUnixResponder(self.metadata_socket, None)
        else:
            raise AssertionError("unknown degradation scenario: %s" % scenario)
        responder.start()
        return responder.close

    def _assert_profile_response(self, status, headers, body, expected_body):
        self.assertEqual(status, 200)
        self.assertEqual(body, expected_body)
        self.assertEqual(headers["content-type"], "text/yaml; charset=utf-8")
        self.assertEqual(headers["profile-title"], "Clash-Compat")
        self.assertEqual(
            headers["content-disposition"], "attachment; filename=Clash-Compat.yaml"
        )
        self.assertEqual(headers["profile-update-interval"], "24")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["cache-control"], "no-store")

    # -- tests ---------------------------------------------------------

    def test_normal_requests_serve_yaml_with_the_dynamic_traffic_header(self):
        for token_value, expected_body in (
            (self.owner_token, self.compat_bytes),
            (self.member_token, self.member_bytes),
        ):
            with self.subTest(token=token_value[:8]):
                status, headers, body = self._request(
                    "GET", "/s/%s/Clash-Compat.yaml" % token_value
                )
                self._assert_profile_response(status, headers, body, expected_body)
                self.assertEqual(
                    headers["subscription-userinfo"],
                    "upload=112233; download=99887766; total=123456789; expire=55",
                )

    def test_head_requests_return_the_headers_without_a_body(self):
        status, headers, body = self._request(
            "HEAD", "/s/%s/Clash-Compat.yaml" % self.owner_token
        )

        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers["profile-title"], "Clash-Compat")
        self.assertEqual(headers["profile-update-interval"], "24")
        self.assertEqual(headers["cache-control"], "no-store")
        # The metadata service answers HEAD like GET, so the traffic
        # header survives the proxied HEAD request too.
        self.assertEqual(
            headers["subscription-userinfo"],
            "upload=112233; download=99887766; total=123456789; expire=55",
        )

    def test_the_airport_url_serves_the_provider_with_saved_airport_traffic(self):
        status, headers, body = self._request(
            "GET", "/s/%s/AmyTelecom.yaml" % self.owner_token
        )

        self.assertEqual(status, 200)
        self.assertEqual(body, self.provider_bytes)
        self.assertEqual(headers["profile-title"], "AmyTelecom")
        self.assertEqual(
            headers["content-disposition"], "attachment; filename=AmyTelecom.yaml"
        )
        self.assertNotIn("profile-update-interval", headers)
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(
            headers["subscription-userinfo"], "upload=1; download=2; total=3; expire=4"
        )

    def test_metadata_outage_degrades_to_identical_bytes_without_the_traffic_header(self):
        # socket-refusing first: it relies on the stopped server leaving a
        # dead socket file behind.  Every scenario cleans up its socket
        # state before the next one starts.
        for scenario in (
            "socket-refusing",
            "socket-absent",
            "upstream-500",
            "upstream-timeout",
        ):
            with self.subTest(scenario=scenario):
                cleanup = self._degrade_metadata(scenario)
                try:
                    status, headers, body = self._request(
                        "GET", "/s/%s/Clash-Compat.yaml" % self.owner_token
                    )
                    self._assert_profile_response(
                        status, headers, body, self.compat_bytes
                    )
                    self.assertNotIn("subscription-userinfo", headers)
                finally:
                    cleanup()

    def test_airport_outage_degrades_to_identical_bytes_without_the_traffic_header(self):
        self._degrade_metadata("socket-absent")

        status, headers, body = self._request(
            "GET", "/s/%s/AmyTelecom.yaml" % self.owner_token
        )

        self.assertEqual(status, 200)
        self.assertEqual(body, self.provider_bytes)
        self.assertEqual(headers["profile-title"], "AmyTelecom")
        self.assertNotIn("subscription-userinfo", headers)
        self.assertNotIn("profile-update-interval", headers)

    def test_local_guards_reject_without_ever_serving_the_file(self):
        # Query, method, and rate rejections are generated by the public
        # location itself; their codes must stay disjoint from the
        # error_page set, which degrades only PROXY failures to the file.
        status, _, body = self._request(
            "GET", "/s/%s/Clash-Compat.yaml?x=1" % self.owner_token
        )
        self.assertEqual(status, 400)
        self.assertNotEqual(body, self.compat_bytes)

        status, _, body = self._request(
            "POST", "/s/%s/Clash-Compat.yaml" % self.owner_token
        )
        self.assertEqual(status, 405)
        self.assertNotEqual(body, self.compat_bytes)

    def test_head_during_degradation_serves_headers_without_body_or_traffic(self):
        self._degrade_metadata("socket-absent")

        status, headers, body = self._request(
            "HEAD", "/s/%s/Clash-Compat.yaml" % self.owner_token
        )

        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers["profile-title"], "Clash-Compat")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertNotIn("subscription-userinfo", headers)

    def test_internal_locations_are_invisible_and_forged_headers_are_ignored(self):
        forged = {
            "X-Accel-Redirect": "/accel/7/Clash-Balance.yaml",
            "Subscription-Userinfo": "upload=9; download=9; total=9; expire=9",
        }

        status, _, _ = self._request("GET", "/accel/7/Clash-Compat.yaml")
        self.assertEqual(status, 404)
        status, headers, _ = self._request(
            "GET", "/accel/7/Clash-Compat.yaml", forged
        )
        self.assertEqual(status, 404)

        # With the service down, forged client-side traffic or redirect
        # headers must not inject a Subscription-Userinfo into the response.
        self._degrade_metadata("socket-absent")
        status, headers, body = self._request(
            "GET", "/s/%s/Clash-Compat.yaml" % self.owner_token, forged
        )
        self._assert_profile_response(status, headers, body, self.compat_bytes)
        self.assertNotIn("subscription-userinfo", headers)


if __name__ == "__main__":
    unittest.main()
