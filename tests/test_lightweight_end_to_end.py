"""End-to-end acceptance tests for the lightweight static subscription service."""

import hashlib
import io
import os
import shutil
import sqlite3
import stat
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from clash_sub.checks import MihomoValidator, validate_clash
from clash_sub.cli import main as cli_main
from clash_sub.config import load_config
from clash_sub.generator import render_user_bundle
from clash_sub.nginx import activate_runtime, render_routes
from clash_sub.release_store import ReleaseStore
from clash_sub.service import ClashSubService, ServiceError
from clash_sub.sources import (
    download_airport_document,
    fetch_xui_proxies,
    normalize_server_home,
)
from clash_sub.state import load_state, reconcile_state, rotate_user_token, save_state
from clash_sub.xui import read_xui_snapshot


ROOT = Path(__file__).resolve().parents[1]
XUI_SCHEMA = ROOT / "tests" / "fixtures" / "xui-3.6.0.sql"


def home_document_bytes(node_name="Home"):
    """Synthetic six-field overlay bytes exactly as SFTP would upload them."""
    return yaml.safe_dump(
        {
            "proxies": [{"name": node_name, "type": "ss"}],
            "proxy-groups": [
                {"name": "HomeServer", "type": "select", "proxies": [node_name]}
            ],
            "extend-proxy-groups": {},
            "inject-node-groups": [],
            "inject-home-node-groups": ["HomeServer"],
            "rules": [],
        },
        sort_keys=False,
    ).encode("utf-8")


class FakeResponse:
    """In-memory HTTP response used at the no-socket source boundary."""

    def __init__(self, url, body):
        self._url = url
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def geturl(self):
        return self._url

    def read(self, size):
        return self._body[:size]


class FakeRunner:
    """Captures validator/Nginx commands without executing a process."""

    def __init__(self, harness):
        self.harness = harness
        self.calls = []
        self.fail_mihomo = False
        self.fail_nginx_test = False

    def __call__(self, arguments, **_):
        command = tuple(str(value) for value in arguments)
        self.calls.append(command)
        if command[0] == str(self.harness.config.mihomo_binary) and self.fail_mihomo:
            return subprocess.CompletedProcess(arguments, 1)
        if command == (str(self.harness.config.nginx_binary), "-t") and self.fail_nginx_test:
            return subprocess.CompletedProcess(arguments, 1)
        return subprocess.CompletedProcess(arguments, 0)

    def clear(self):
        self.calls.clear()

    def mihomo_calls(self):
        return [
            command
            for command in self.calls
            if command[0] == str(self.harness.config.mihomo_binary)
        ]


class AcceptanceHarness:
    """A real temporary runtime with only HTTP and process boundaries faked."""

    owner_id = 7
    member_id = 8

    def __init__(self, testcase):
        self._temporary = TemporaryDirectory()
        testcase.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name).resolve(strict=True)
        self.repo_root = self.root / "repo"
        self.private_root = self.root / "private"
        self.public_root = self.root / "public"
        self.routes_path = self.root / "nginx" / "routes.conf"
        self.database = self.root / "x-ui.db"
        self.render_calls = 0
        self.fail_render = False
        self.fail_xui_source = False
        self.airport_body = (
            b"# AmyTelecom upstream snapshot\n"
            b"proxies:\n"
            b"- {name: 'Amy HK 01', type: ss, server: amy-1.example.test, port: 443,"
            b" cipher: aes-128-gcm, password: airport-old}\n"
            b"- name: Amy HK 02\n"
            b"  type: trojan\n"
            b"  server: amy-2.example.test\n"
            b"  port: 443\n"
            b"  password: airport-old-2\n"
            b"# trailing comment preserved verbatim\n"
        )
        self._make_runtime()

    def _make_runtime(self):
        shutil.copytree(ROOT / "templates", self.repo_root / "templates")
        self.private_root.mkdir(mode=0o700)
        os.chmod(self.private_root, 0o700)
        self.public_root.mkdir()
        os.chown(self.public_root, -1, os.getegid())
        os.chmod(self.public_root, 0o2750)
        if stat.S_IMODE(self.public_root.stat().st_mode) != 0o2750:
            raise RuntimeError("setgid-capable filesystem is required for acceptance")
        self.routes_path.parent.mkdir()

        config_path = self.repo_root / "private" / "config" / "service.yaml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            "\n".join(
                (
                    "schema-version: 2",
                    "owner-email: owner@example.test",
                    "subscription-authority: sub.example.test:443",
                    "xui-public-endpoint: example.com:443",
                    "xui-database: %s" % self.database,
                    "private-root: %s" % self.private_root,
                    "public-root: %s" % self.public_root,
                    "nginx-routes: %s" % self.routes_path,
                    "mihomo-binary: %s" % (self.root / "bin" / "mihomo"),
                    "nginx-binary: %s" % (self.root / "bin" / "nginx"),
                    "systemctl-binary: %s" % (self.root / "bin" / "systemctl"),
                    "max-source-bytes: 1048576",
                    "",
                )
            ),
            encoding="utf-8",
        )
        os.chmod(config_path, 0o600)
        self.config = load_config(config_path, self.repo_root)

        connection = sqlite3.connect(self.database)
        try:
            connection.executescript(XUI_SCHEMA.read_text(encoding="utf-8"))
            connection.executemany(
                """
                INSERT INTO clients (id, email, sub_id, enable, total_gb, expiry_time)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (self.owner_id, "owner@example.test", "owner-sub-id", 1, 10000, 0),
                    (self.member_id, "member@example.test", "member-sub-id", 1, 20000, 0),
                ),
            )
            connection.executemany(
                "INSERT INTO client_traffics (id, email, up, down) VALUES (?, ?, ?, ?)",
                (
                    (self.owner_id, "owner@example.test", 101, 202),
                    (self.member_id, "member@example.test", 303, 404),
                ),
            )
            connection.commit()
        finally:
            connection.close()

        snapshot = read_xui_snapshot(self.database)
        self.source_urls = {
            client.client_id: snapshot.source_url(client) for client in snapshot.clients
        }
        initial_state = reconcile_state(None, snapshot.clients, self.config.owner_email)
        save_state(self.private_root / "state.json", initial_state)
        if load_state(self.private_root / "state.json") != initial_state:
            raise AssertionError("state save/load must preserve reconciled identities")
        self.xui_bodies = {
            self.source_urls[self.owner_id]: self._document(
                [self._proxy("Owner 3x-ui", "owner-current", port=10443)]
            ),
            self.source_urls[self.member_id]: self._document(
                [self._proxy("Member 3x-ui", "member-current", port=10443)]
            ),
        }
        home_path = self.private_root / "home.yaml"
        home_path.write_text(
            yaml.safe_dump(
                {
                    "proxies": [self._proxy("Home", "home-snapshot")],
                    "proxy-groups": [
                        {
                            "name": "HomeServer",
                            "type": "select",
                            "proxies": ["🎯 Direct", "Home"],
                        }
                    ],
                    "extend-proxy-groups": {},
                    "inject-node-groups": [],
                    "inject-home-node-groups": ["HomeServer"],
                    "rules": [],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        os.chmod(home_path, 0o600)
        # The maintainer's SFTP overwrite target: private/home.yaml.
        self.home_path = home_path

        self.runner = FakeRunner(self)
        self.release_store = ReleaseStore(self.private_root, self.public_root)
        self.service = ClashSubService(
            self.config,
            read_snapshot=read_xui_snapshot,
            load_state=load_state,
            reconcile_state=reconcile_state,
            rotate_user_token=rotate_user_token,
            fetch_xui_proxies=self._fetch_xui,
            download_airport_document=self._download_airport,
            # The same server boundary the real runtime injects; the local
            # test user stands in for the root service account.
            load_home_overlay=lambda path, max_bytes: normalize_server_home(
                path, max_bytes, expected_uid=os.geteuid()
            ),
            render_user_bundle=self._render,
            validate_clash=validate_clash,
            mihomo_validator=MihomoValidator(self.config.mihomo_binary, runner=self.runner),
            release_store=self.release_store,
            render_routes=render_routes,
            activate_runtime=activate_runtime,
            runner=self.runner,
        )

    @staticmethod
    def _proxy(name, password, port=443):
        return {
            "name": name,
            "type": "trojan",
            "server": "node.example.test",
            "port": port,
            "password": password,
        }

    @staticmethod
    def _document(proxies):
        return yaml.safe_dump({"proxies": proxies}, sort_keys=False).encode("utf-8")

    def _xui_opener(self, request, _timeout):
        if self.fail_xui_source:
            raise OSError("synthetic source failure")
        return FakeResponse(request.full_url, self.xui_bodies[request.full_url])

    def _airport_opener(self, request, _timeout):
        return FakeResponse(request.full_url, self.airport_body)

    def _fetch_xui(self, url, maximum):
        return fetch_xui_proxies(url, maximum, opener=self._xui_opener)

    def _download_airport(self, url, maximum):
        return download_airport_document(url, maximum, opener=self._airport_opener)

    def _render(self, owner, xui, airport, home, template_root):
        self.render_calls += 1
        if self.fail_render:
            raise RuntimeError("synthetic render failure")
        return render_user_bundle(owner, xui, airport, home, template_root)

    def set_xui_proxy(self, client_id, name):
        self.xui_bodies[self.source_urls[client_id]] = self._document(
            [self._proxy(name, "%s-password" % name.lower().replace(" ", "-"), port=10443)]
        )

    def import_airport(self, url="https://airport.example.test/import/live"):
        """Import the airport once; the short-lived URL is never persisted."""
        return self.service.update_airport(url)

    def set_traffic(self, client_id, upload, download):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE client_traffics SET up = ?, down = ? WHERE id = ?",
                (upload, download, client_id),
            )
            connection.commit()
        finally:
            connection.close()

    def corrupt_schema(self):
        connection = sqlite3.connect(self.database)
        try:
            connection.execute("DROP TABLE client_traffics")
            connection.commit()
        finally:
            connection.close()

    def state(self):
        state = load_state(self.private_root / "state.json")
        if state is None:
            raise AssertionError("accepted runtime must persist state")
        return state

    def release(self, client_id):
        release_id = self.state().users[client_id].current_release
        if release_id is None:
            raise AssertionError("accepted runtime must select a release")
        return self.release_store.verify_release(client_id, release_id)

    def route_text(self):
        return self.routes_path.read_text(encoding="utf-8")

    def active_view(self):
        state = self.state()
        releases = []
        for client_id in sorted(state.users):
            release_id = state.users[client_id].current_release
            if release_id is not None:
                release = self.release_store.verify_release(client_id, release_id)
                airport = (
                    hashlib.sha256(release.airport_path.read_bytes()).hexdigest()
                    if release.airport_path is not None
                    else None
                )
                releases.append(
                    (
                        client_id,
                        release_id,
                        tuple(
                            sorted(
                                (variant, hashlib.sha256(path.read_bytes()).hexdigest())
                                for variant, path in release.public_paths.items()
                            )
                        ),
                        airport,
                    )
                )
        return {
            "state": hashlib.sha256((self.private_root / "state.json").read_bytes()).hexdigest(),
            "routes": hashlib.sha256(self.routes_path.read_bytes()).hexdigest(),
            "releases": tuple(releases),
        }

    def active_owner_view(self):
        """The complete live owner surface: identity, marker, release, bytes."""
        user = self.state().users[self.owner_id]
        release = (
            self.release_store.verify_release(self.owner_id, user.current_release)
            if user.current_release is not None
            else None
        )
        return {
            "user": user,
            "marker": (self.private_root / "current" / str(self.owner_id)).read_text(
                encoding="ascii"
            ),
            "release_id": user.current_release,
            "public": tuple(
                sorted(
                    (variant, hashlib.sha256(path.read_bytes()).hexdigest())
                    for variant, path in release.public_paths.items()
                )
            )
            if release is not None
            else None,
            "airport": hashlib.sha256(release.airport_path.read_bytes()).hexdigest()
            if release is not None and release.airport_path is not None
            else None,
        }

    def assert_candidate_cleanup(self, testcase):
        staging = self.private_root / "staging"
        if staging.exists():
            testcase.assertEqual(tuple(staging.iterdir()), ())
        releases = self.public_root / "releases"
        if releases.exists():
            for client_root in releases.iterdir():
                testcase.assertFalse(any(path.name.startswith(".") for path in client_root.iterdir()))

    def assert_lock_and_markers(self, testcase):
        lock = self.private_root / "operation.lock"
        details = lock.lstat()
        testcase.assertTrue(stat.S_ISREG(details.st_mode))
        testcase.assertEqual(stat.S_IMODE(details.st_mode), 0o600)
        testcase.assertEqual(details.st_nlink, 1)
        for client_id, user in self.state().users.items():
            if user.current_release is None:
                continue
            marker = self.private_root / "current" / str(client_id)
            testcase.assertEqual(marker.read_text(encoding="ascii"), user.current_release + "\n")


class LightweightEndToEndAcceptanceTests(unittest.TestCase):
    def make_harness(self):
        return AcceptanceHarness(self)

    def test_owner_and_member_sources_are_isolated(self):
        """A wrong role/source mapping must not reach a rendered release."""
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()

        owner = harness.release(harness.owner_id)
        member = harness.release(harness.member_id)
        owner_documents = {
            variant: yaml.safe_load(path.read_text())
            for variant, path in owner.public_paths.items()
        }
        owner_names = {
            variant: [proxy["name"] for proxy in document["proxies"]]
            for variant, document in owner_documents.items()
        }
        member_universal = yaml.safe_load(member.public_paths["compat-universal"].read_text())
        member_names = [proxy["name"] for proxy in member_universal["proxies"]]

        # Airport nodes stay out of every inline proxies list.
        self.assertEqual(owner_names["compat-office"], ["Owner 3x-ui", "Home"])
        self.assertEqual(owner_names["compat-universal"], ["Owner 3x-ui"])
        self.assertEqual(owner_names["balance-office"], ["Owner 3x-ui", "Home"])
        self.assertEqual(member_names, ["Member 3x-ui"])

        owner_token = harness.state().users[harness.owner_id].token
        expected_url = "https://sub.example.test:443/s/%s/AmyTelecom.yaml" % owner_token
        for variant, document in owner_documents.items():
            provider = document["proxy-providers"]["AmyTelecom"]
            self.assertEqual(provider["type"], "http", variant)
            self.assertEqual(provider["url"], expected_url, variant)
            self.assertEqual(provider["interval"], 0, variant)
            self.assertEqual(
                provider["path"],
                "./proxy_providers/AmyTelecom-%s.yaml"
                % hashlib.sha256(harness.airport_body).hexdigest(),
                variant,
            )
            groups = {group["name"]: group for group in document["proxy-groups"]}
            self.assertEqual(groups["加速线路"]["use"], ["AmyTelecom"], variant)
            self.assertEqual(groups["自动选择"]["use"], ["AmyTelecom"], variant)

        self.assertNotIn("proxy-providers", member_universal)
        member_groups = {
            group["name"]: group for group in member_universal["proxy-groups"]
        }
        self.assertNotIn("use", member_groups["加速线路"])
        member_text = member.public_paths["compat-universal"].read_text()
        self.assertNotIn("AmyTelecom", member_text)
        self.assertNotIn("amy-1.example.test", member_text)
        self.assertNotIn("airport.example.test", member_text)

        owner_xui = next(
            proxy
            for proxy in owner_documents["compat-universal"]["proxies"]
            if proxy["name"] == "Owner 3x-ui"
        )
        self.assertEqual((owner_xui["server"], owner_xui["port"]), ("example.com", 443))
        self.assertEqual(
            [
                (proxy["server"], proxy["port"])
                for proxy in member_universal["proxies"]
            ],
            [("example.com", 443)],
        )
        harness.assert_lock_and_markers(self)

    def test_published_airport_yaml_is_byte_identical_and_owner_only(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()

        release = harness.release(harness.owner_id)
        routes = harness.route_text()
        member_token = harness.state().users[harness.member_id].token

        self.assertEqual(release.airport_path.read_bytes(), harness.airport_body)
        self.assertEqual(
            (release.manifest_path.parent / "AmyTelecom.yaml").read_bytes(),
            harness.airport_body,
        )
        self.assertIn("alias %s;" % release.airport_path, routes)
        owner_token = harness.state().users[harness.owner_id].token
        self.assertIn("location = /s/%s/AmyTelecom.yaml" % owner_token, routes)
        self.assertNotIn("location = /s/%s/AmyTelecom.yaml" % member_token, routes)

    def test_first_sync_without_an_airport_leaves_the_owner_pending(self):
        harness = self.make_harness()

        result = harness.service.sync_all()

        self.assertEqual(
            {item["code"] for item in result["errors"]},
            {"owner_update_failed"},
        )
        status = harness.service.status()
        self.assertEqual([item["client_id"] for item in status["pending"]], [harness.owner_id])
        self.assertIsNone(harness.state().users[harness.owner_id].current_release)
        self.assertIsNotNone(harness.state().users[harness.member_id].current_release)

    def test_sync_all_reuses_the_release_artifact_without_the_upstream(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()

        def exploding(request, _timeout):
            raise OSError("upstream must never be contacted")

        harness._airport_opener = exploding
        harness.set_xui_proxy(harness.owner_id, "Owner changed")

        result = harness.service.sync_all()

        self.assertFalse(result["errors"])
        release = harness.release(harness.owner_id)
        self.assertEqual(release.airport_path.read_bytes(), harness.airport_body)

    def test_changed_airport_body_publishes_a_new_digest_cache_path(self):
        harness = self.make_harness()
        harness.import_airport()

        first = yaml.safe_load(harness.release(harness.owner_id).public_paths["compat-universal"].read_text())
        harness.airport_body = harness.airport_body.replace(b"airport-old", b"airport-new")
        harness.import_airport()
        second_release = harness.release(harness.owner_id)

        second = yaml.safe_load(second_release.public_paths["compat-universal"].read_text())
        self.assertNotEqual(
            first["proxy-providers"]["AmyTelecom"]["path"],
            second["proxy-providers"]["AmyTelecom"]["path"],
        )
        self.assertEqual(second_release.airport_path.read_bytes(), harness.airport_body)
        self.assertIn("alias %s;" % second_release.airport_path, harness.route_text())

    def test_links_are_anonymous_full_urls_with_unique_readable_codes(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()

        links = harness.service.links()

        self.assertEqual([item["client_id"] for item in links], [7, 8])
        self.assertEqual(len({item["readable_code"] for item in links}), 2)
        self.assertEqual([len(item["urls"]) for item in links], [3, 1])
        for item in links:
            self.assertNotIn("token", item)
            for url in item["urls"]:
                self.assertNotIn(item["email"], url)
                self.assertIn("-%s/" % item["readable_code"], url)
                self.assertRegex(
                    url,
                    r"^https://sub\.example\.test:443/s/[A-Za-z0-9_-]{43}-[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}/clash-(?:compat-office|compat-universal|balance-office)\.yaml$",
                )

    def test_routes_authorize_only_exact_token_user_and_variant_combinations(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        state = harness.state()
        owner = state.users[harness.owner_id]
        member = state.users[harness.member_id]
        routes = harness.route_text()

        self.assertIn("location = /s/%s/clash-compat-universal.yaml" % member.token, routes)
        self.assertNotIn("location = /s/%s/clash-compat-office.yaml" % member.token, routes)
        self.assertNotIn("location = /s/%s/clash-balance-office.yaml" % member.token, routes)
        self.assertIn("location = /s/%s/clash-compat-office.yaml" % owner.token, routes)
        self.assertIn("location = /s/%s/AmyTelecom.yaml" % owner.token, routes)
        self.assertNotIn("location = /s/%s/AmyTelecom.yaml" % member.token, routes)
        self.assertEqual(routes.count("location = /s/%s/" % member.token), 1)
        self.assertEqual(routes.count("location = /s/%s/" % owner.token), 4)
        block = routes[routes.index("location = /s/%s/AmyTelecom.yaml" % owner.token):]
        self.assertNotIn("Subscription-Userinfo", block[: block.index("\n}")])

    def test_database_failure_preserves_active_bytes_and_metadata(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        harness.corrupt_schema()

        with self.assertRaisesRegex(ServiceError, "xui_snapshot_failed"):
            harness.service.sync_all()

        self.assertEqual(harness.active_view(), before)
        harness.assert_candidate_cleanup(self)

    def test_source_failure_preserves_active_bytes_and_metadata(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        harness.fail_xui_source = True

        result = harness.service.sync_all()

        self.assertEqual({item["code"] for item in result["errors"]}, {"owner_update_failed", "member_update_failed"})
        self.assertEqual(harness.active_view(), before)
        harness.assert_candidate_cleanup(self)

    def test_render_failure_preserves_active_bytes_and_metadata(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        harness.fail_render = True

        result = harness.service.sync_all()

        self.assertEqual({item["code"] for item in result["errors"]}, {"owner_update_failed", "member_update_failed"})
        self.assertEqual(harness.active_view(), before)
        harness.assert_candidate_cleanup(self)

    def test_mihomo_failure_preserves_active_bytes_and_metadata(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        # A home-less owner candidate keeps the generic owner failure code;
        # the home-bearing rejection is pinned by the overwrite tests below.
        harness.home_path.unlink()
        harness.set_xui_proxy(harness.owner_id, "Owner changed")
        harness.runner.fail_mihomo = True

        result = harness.service.sync_all()

        self.assertEqual({item["code"] for item in result["errors"]}, {"owner_update_failed"})
        self.assertEqual(harness.active_view(), before)
        harness.assert_candidate_cleanup(self)

    def test_nginx_failure_preserves_active_bytes_metadata_and_candidates(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        harness.set_xui_proxy(harness.owner_id, "Owner changed")
        harness.runner.fail_nginx_test = True

        with self.assertRaisesRegex(ServiceError, "sync_activation_failed"):
            harness.service.sync_all()

        self.assertEqual(harness.active_view(), before)
        harness.assert_candidate_cleanup(self)
        harness.assert_lock_and_markers(self)

    def test_failed_airport_activation_restores_the_previous_airport_release(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        old_body = harness.airport_body
        old_airport_path = harness.release(harness.owner_id).airport_path

        harness.airport_body = old_body.replace(b"airport-old", b"airport-new")
        harness.runner.fail_nginx_test = True
        with self.assertRaisesRegex(ServiceError, "airport_activation_failed"):
            harness.import_airport("https://airport.example.test/import/second")

        self.assertEqual(harness.active_view(), before)
        self.assertEqual(old_airport_path.read_bytes(), old_body)
        harness.assert_candidate_cleanup(self)
        harness.assert_lock_and_markers(self)

    def test_transient_airport_url_and_credentials_do_not_leak(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        airport_url = "https://airport.example.test/import/temporary-credential-4893"
        airport_credential = "airport-credential-4893"
        harness.airport_body = harness._document([harness._proxy("Airport", airport_credential)])
        stdout = io.StringIO()
        stderr = io.StringIO()

        with patch("clash_sub.cli.getpass", return_value=airport_url):
            code = cli_main(
                [],
                stdin=io.StringIO("1\n" + airport_url + "\n"),
                stdout=stdout,
                stderr=stderr,
                service_factory=lambda: harness.service,
            )

        self.assertEqual(code, 0)
        observed = "\n".join(
            (
                (harness.private_root / "state.json").read_text(encoding="utf-8"),
                harness.route_text(),
                "\n".join(
                    release.manifest_path.read_text(encoding="utf-8")
                    for release in harness.release_store.history(harness.owner_id)
                ),
                stdout.getvalue(),
                stderr.getvalue(),
                repr(harness.runner.calls),
            )
        )
        self.assertNotIn(airport_url, observed)
        self.assertNotIn(airport_credential, observed)

        harness.runner.fail_nginx_test = True
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("clash_sub.cli.getpass", return_value=airport_url):
            failed = cli_main(
                [],
                stdin=io.StringIO("1\n" + airport_url + "\n"),
                stdout=stdout,
                stderr=stderr,
                service_factory=lambda: harness.service,
            )
        self.assertEqual(failed, 1)
        self.assertNotIn(airport_url, stdout.getvalue() + stderr.getvalue())
        self.assertNotIn(airport_credential, stdout.getvalue() + stderr.getvalue())

    def test_traffic_update_changes_only_route_headers_without_rendering_or_mihomo(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        previous_routes = harness.route_text()
        harness.render_calls = 0
        harness.runner.clear()
        harness.set_traffic(harness.owner_id, 999, 888)

        harness.service.traffic_update()

        self.assertEqual(harness.render_calls, 0)
        self.assertEqual(harness.runner.mihomo_calls(), [])
        self.assertEqual(harness.active_view()["state"], before["state"])
        self.assertEqual(harness.active_view()["releases"], before["releases"])
        self.assertNotEqual(harness.route_text(), previous_routes)
        self.assertIn('Subscription-Userinfo "upload=999; download=888; total=10000; expire=0"', harness.route_text())

    def test_failed_owner_rotation_keeps_the_old_link_routes_and_release(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        old_token = harness.state().users[harness.owner_id].token
        old_routes = harness.route_text()

        harness.runner.fail_nginx_test = True
        with self.assertRaisesRegex(ServiceError, "rotation_activation_failed"):
            harness.service.rotate_link(harness.owner_id)

        self.assertEqual(harness.state().users[harness.owner_id].token, old_token)
        self.assertEqual(harness.active_view(), before)
        self.assertEqual(harness.route_text(), old_routes)
        self.assertIn("location = /s/%s/AmyTelecom.yaml" % old_token, harness.route_text())
        harness.assert_candidate_cleanup(self)
        harness.assert_lock_and_markers(self)

    def test_rollback_restores_the_matching_airport_and_rotation_revokes_it(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        first_release = harness.state().users[harness.owner_id].current_release
        old_token = harness.state().users[harness.owner_id].token
        old_body = harness.airport_body

        harness.airport_body = old_body.replace(b"airport-old", b"airport-new")
        harness.import_airport()
        harness.service.rollback(harness.owner_id, first_release)

        # Rollback must pair the old profiles with the old raw airport file.
        rolled = harness.release(harness.owner_id)
        self.assertEqual(rolled.release_id, first_release)
        self.assertEqual(rolled.airport_path.read_bytes(), old_body)
        routes = harness.route_text()
        self.assertIn("location = /s/%s/AmyTelecom.yaml" % old_token, routes)
        self.assertIn("alias %s;" % rolled.airport_path, routes)
        universal = yaml.safe_load(rolled.public_paths["compat-universal"].read_text())
        self.assertEqual(
            universal["proxy-providers"]["AmyTelecom"]["path"],
            "./proxy_providers/AmyTelecom-%s.yaml" % hashlib.sha256(old_body).hexdigest(),
        )

        rotated = harness.service.rotate_link(harness.owner_id)

        self.assertNotEqual(rotated["token"], old_token)
        routes = harness.route_text()
        self.assertNotIn("location = /s/%s/" % old_token, routes)
        self.assertIn("location = /s/%s/clash-compat-office.yaml" % rotated["token"], routes)
        self.assertIn("location = /s/%s/AmyTelecom.yaml" % rotated["token"], routes)
        rotated_release = harness.release(harness.owner_id)
        self.assertEqual(rotated_release.airport_path.read_bytes(), old_body)
        rotated_universal = yaml.safe_load(
            rotated_release.public_paths["compat-universal"].read_text()
        )
        self.assertEqual(
            rotated_universal["proxy-providers"]["AmyTelecom"]["url"],
            "https://sub.example.test:443/s/%s/AmyTelecom.yaml" % rotated["token"],
        )
        harness.assert_lock_and_markers(self)

    def test_six_successful_content_changes_retain_five_verified_releases(self):
        harness = self.make_harness()
        harness.import_airport()

        for version in range(6):
            harness.set_xui_proxy(harness.member_id, "Member generation %s" % version)
            result = harness.service.sync_all()
            self.assertFalse(result["errors"])

        history = harness.release_store.history(harness.member_id)
        self.assertEqual(len(history), 5)
        self.assertIn(
            harness.state().users[harness.member_id].current_release,
            {release.release_id for release in history},
        )
        for release in history:
            self.assertEqual(
                harness.release_store.verify_release(harness.member_id, release.release_id),
                release,
            )
        harness.assert_candidate_cleanup(self)

    def test_optional_real_mihomo_validates_all_owner_variants(self):
        binary = os.environ.get("MIHOMO_BIN")
        if not binary:
            self.skipTest("MIHOMO_BIN is not set")
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()

        with TemporaryDirectory(prefix="mihomo-acceptance.") as scratch:
            airport_file = Path(scratch) / "AmyTelecom.yaml"
            airport_file.write_bytes(harness.airport_body)
            for path in harness.release(harness.owner_id).public_paths.values():
                # Published profiles carry the HTTP provider; validate the
                # local-file equivalent exactly like the service does.
                document = yaml.safe_load(path.read_text(encoding="utf-8"))
                document["proxy-providers"]["AmyTelecom"] = {
                    "type": "file",
                    "path": str(airport_file),
                }
                candidate = Path(scratch) / path.name
                candidate.write_text(
                    yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                    encoding="utf-8",
                )
                completed = subprocess.run(
                    [binary, "-t", "-f", str(candidate)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0)


class DirectHomeOverwriteAcceptanceTests(unittest.TestCase):
    """The direct-SFTP failure model: the upload stays, the runtime freezes.

    Every scenario writes bytes straight to ``private/home.yaml`` — exactly
    what an external SFTP client does outside any program transaction — and
    then runs the real service.  A rejected upload is never repaired,
    moved, or rolled back; it stays on disk for debugging while the
    previously activated runtime survives untouched.
    """

    def make_harness(self):
        return AcceptanceHarness(self)

    def test_uploaded_home_is_published_by_the_next_sync(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        previous = harness.active_owner_view()
        uploaded = home_document_bytes(node_name="Home Updated")
        harness.home_path.write_bytes(uploaded)
        os.chmod(harness.home_path, 0o600)

        result = harness.service.sync_all()

        self.assertFalse(result["errors"])
        self.assertEqual(harness.home_path.read_bytes(), uploaded)
        current = harness.active_owner_view()
        self.assertNotEqual(current["release_id"], previous["release_id"])
        self.assertEqual(current["marker"], current["release_id"] + "\n")
        names = {
            variant: [
                proxy["name"]
                for proxy in yaml.safe_load(path.read_text(encoding="utf-8"))["proxies"]
            ]
            for variant, path in harness.release(harness.owner_id).public_paths.items()
        }
        self.assertEqual(names["compat-office"], ["Owner 3x-ui", "Home Updated"])
        self.assertEqual(names["balance-office"], ["Owner 3x-ui", "Home Updated"])
        self.assertEqual(names["compat-universal"], ["Owner 3x-ui"])
        self.assertEqual(stat.S_IMODE(harness.home_path.stat().st_mode), 0o600)
        harness.assert_lock_and_markers(self)

    def test_uploaded_home_with_mode_0644_is_normalized_and_published(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        uploaded = home_document_bytes(node_name="Home Widened")
        harness.home_path.write_bytes(uploaded)
        os.chmod(harness.home_path, 0o644)

        result = harness.service.sync_all()

        self.assertFalse(result["errors"])
        self.assertEqual(harness.home_path.read_bytes(), uploaded)
        self.assertEqual(stat.S_IMODE(harness.home_path.stat().st_mode), 0o600)
        compat_office = yaml.safe_load(
            harness.release(harness.owner_id).public_paths["compat-office"].read_text(
                encoding="utf-8"
            )
        )
        self.assertIn(
            "Home Widened", [proxy["name"] for proxy in compat_office["proxies"]]
        )

    def test_malformed_uploaded_home_is_kept_and_reported_while_members_sync(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_owner_view()
        uploaded = b"proxies: [unclosed\n"
        harness.home_path.write_bytes(uploaded)
        harness.set_xui_proxy(harness.member_id, "Member changed")

        result = harness.service.sync_all()

        self.assertEqual(
            result["errors"],
            ({"client_id": harness.owner_id, "code": "home_yaml_invalid"},),
        )
        self.assertEqual(harness.home_path.read_bytes(), uploaded)
        self.assertEqual(harness.active_owner_view(), before)
        self.assertIn(
            harness.member_id, [item["client_id"] for item in result["updated"]]
        )
        harness.assert_candidate_cleanup(self)

    def test_uploaded_home_with_an_invalid_reference_is_kept_and_reported(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        document = yaml.safe_load(home_document_bytes())
        document["inject-home-node-groups"] = ["MissingGroup"]
        uploaded = yaml.safe_dump(document, sort_keys=False).encode("utf-8")
        harness.home_path.write_bytes(uploaded)

        result = harness.service.sync_all()

        self.assertEqual(
            result["errors"],
            ({"client_id": harness.owner_id, "code": "home_group_reference_invalid"},),
        )
        self.assertEqual(harness.home_path.read_bytes(), uploaded)
        self.assertEqual(harness.active_view(), before)
        harness.assert_candidate_cleanup(self)

    def test_mihomo_rejects_uploaded_home_but_source_is_not_rolled_back(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        uploaded = home_document_bytes(node_name="Mihomo Reject")
        harness.home_path.write_bytes(uploaded)
        harness.runner.fail_mihomo = True

        result = harness.service.sync_all()

        self.assertEqual(harness.home_path.read_bytes(), uploaded)
        self.assertEqual(harness.active_view(), before)
        self.assertEqual(
            result["errors"],
            ({"client_id": harness.owner_id, "code": "home_mihomo_validation_failed"},),
        )
        harness.assert_candidate_cleanup(self)

    def test_nginx_rejection_freezes_the_runtime_and_keeps_the_upload(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        before = harness.active_view()
        uploaded = home_document_bytes(node_name="Nginx Reject")
        harness.home_path.write_bytes(uploaded)
        harness.runner.fail_nginx_test = True

        with self.assertRaisesRegex(ServiceError, "sync_activation_failed"):
            harness.service.sync_all()

        self.assertEqual(harness.home_path.read_bytes(), uploaded)
        self.assertEqual(harness.active_view(), before)
        harness.assert_candidate_cleanup(self)
        harness.assert_lock_and_markers(self)

    def test_empty_or_truncated_upload_is_rejected_and_kept_verbatim(self):
        harness = self.make_harness()
        harness.import_airport()
        harness.service.sync_all()
        for uploaded in (
            b"",
            b"proxies:\n- {name: Truncated Upload, type: ss\n",
        ):
            with self.subTest(uploaded=uploaded):
                before = harness.active_view()
                harness.home_path.write_bytes(uploaded)

                result = harness.service.sync_all()

                self.assertEqual(
                    result["errors"],
                    ({"client_id": harness.owner_id, "code": "home_yaml_invalid"},),
                )
                self.assertEqual(harness.home_path.read_bytes(), uploaded)
                self.assertEqual(harness.active_view(), before)
        harness.assert_candidate_cleanup(self)


if __name__ == "__main__":
    unittest.main()
