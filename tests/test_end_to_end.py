"""End-to-end acceptance tests composing the real service pipeline.

The harness wires the real settings, release, manager, and publisher code
together with fake loopback converter/traffic clients and an injectable
fake Mihomo validator command, mirroring the ``host_cli`` refresh
lifecycle.  No HTTP socket is opened: ``PublicationService.handle()`` is
driven directly with ``Request`` objects and the manager JSON API is
driven in-process.  A separate container integration remains the proof
against the real pinned converter and Mihomo images.
"""

import io
import json
import copy
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import yaml

from clash_sub import manager as manager_module
from clash_sub.converter import SourceError
from clash_sub.host_cli import ValidatorError
from clash_sub.models import Request, SubscriptionUserinfo
from clash_sub.publisher import (
    TRAFFIC_CACHE_SECONDS,
    PublicationService,
    settings_file_revision,
)
from clash_sub.rendering import render_variant
from clash_sub.settings import hash_token, load_settings
from clash_sub.validation import ValidationError, validate_config


REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeConverterClient:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def convert(self, source_url: str):
        self.calls.append(source_url)
        response = self._responses[source_url]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class FakeTrafficClient:
    def __init__(self, responses):
        self._responses = responses
        self.fail = False
        self.calls = 0

    def fetch(self, source_url: str):
        self.calls += 1
        if self.fail:
            raise SourceError("synthetic live fetch failure")
        return self._responses.get(source_url)


class FakeMihomoCommand:
    """Injectable stand-in for ``docker compose run validator -t -f``."""

    def __init__(self):
        self.fail_variant = None
        self.calls = []

    def validate(self, candidate_path: Path) -> None:
        variant = candidate_path.name[: -len(".yaml")]
        self.calls.append(variant)
        if variant == self.fail_variant:
            raise ValidatorError("mihomo_validation_failed")


class HarnessRenderer:
    """Real renderer that records which variant is being rendered."""

    def __init__(self):
        self.current_variant = None
        self.calls = []

    def __call__(self, template_dir: Path, variant: str, proxies):
        self.current_variant = variant
        self.calls.append(variant)
        return render_variant(template_dir, variant, proxies)


class HarnessValidator:
    """Real structural validation with one injectable failing variant."""

    def __init__(self, renderer: HarnessRenderer):
        self.renderer = renderer
        self.fail_variant = None

    def __call__(self, text: str, source_urls, reality):
        if self.fail_variant is not None and self.renderer.current_variant == self.fail_variant:
            raise ValidationError("synthetic end-to-end validation failure")
        return validate_config(text, source_urls, reality)


class PublisherDriver:
    """Drives the real publication service with synthetic requests."""

    def __init__(self, service: PublicationService):
        self.service = service

    def get(self, token: str, variant: str, client_ip: str = "127.0.0.1"):
        request = Request(
            method="GET",
            path="/s/%s/%s.yaml" % (token, variant),
            client_ip=client_ip,
            peer_ip="127.0.0.1",
            headers={},
        )
        return self.service.handle(request)


class HarnessCli:
    """Refresh lifecycle mirroring ``clash_sub.host_cli`` on real code."""

    def __init__(self, harness):
        self.harness = harness

    def refresh(self, user_id: str):
        operation_id = self.harness.next_operation_id()
        build = self.harness.run_manager(
            ["build", "--operation-id", operation_id, "--user", user_id]
        )
        if build.returncode != 0:
            return SimpleNamespace(
                ok=False, release_id=None, error_code=_error_code(build), variant=None
            )
        payload = json.loads(build.stdout)
        for variant in payload["variants"]:
            try:
                self.harness.mihomo.validate(
                    Path(payload["candidate_path"]) / ("%s.yaml" % variant)
                )
            except ValidatorError as exc:
                return SimpleNamespace(
                    ok=False, release_id=None, error_code=exc.code, variant=variant
                )
        publish = self.harness.run_manager(
            ["publish", "--operation-id", operation_id, "--user", user_id]
        )
        if publish.returncode != 0:
            return SimpleNamespace(
                ok=False, release_id=None, error_code=_error_code(publish), variant=None
            )
        release_id = json.loads(publish.stdout)["release_id"]
        return SimpleNamespace(ok=True, release_id=release_id, error_code=None, variant=None)

    def airport(self, url: str):
        result = self.harness.run_manager(["import-airport"], stdin=url + "\n")
        if result.returncode != 0:
            return SimpleNamespace(ok=False, error_code=_error_code(result))
        owner_ids = [
            user_id
            for user_id, role in self._listed_users()
            if role == "owner"
        ]
        if not owner_ids:
            return SimpleNamespace(ok=False, error_code="operation_failed")
        refresh = self.refresh(sorted(owner_ids)[0])
        return SimpleNamespace(ok=refresh.ok, error_code=refresh.error_code)

    def _listed_users(self):
        result = self.harness.run_manager(["list-users"])
        payload = json.loads(result.stdout)
        return [
            (user["user_id"], user["role"])
            for user in payload["users"]
        ]


def _error_code(result) -> str:
    payload = json.loads(result.stdout)
    return payload["error"]["code"]


class EndToEndHarness:
    """Temp world wiring the real pipeline to synthetic private data."""

    owner_token = "owner-e2e-token-0123456789abcdef"
    friend_token = "friend-e2e-token-0123456789abcd"
    owner_url = "http://127.0.0.1:2096/sub/owner-e2e-secret-sub-id"
    friend_url = "http://127.0.0.1:2096/sub/friend-e2e-secret-sub-id"
    airport_import_url = "https://airport.example/temporary-e2e"
    airport_malformed_url = "https://airport.example/malformed-e2e"

    def __init__(self, root: Path):
        self.root = root
        self.private_root = root / "private"
        (self.private_root / "config").mkdir(parents=True, mode=0o700)
        (self.private_root / "logs").mkdir(mode=0o700)
        (self.private_root / "sources" / "owner").mkdir(parents=True, mode=0o700)
        (self.private_root / "reference-configs" / "2026-08-21").mkdir(parents=True)
        self.service_path = self.private_root / "config" / "service.yaml"
        self.users_path = self.private_root / "config" / "users.yaml"
        self.operation_log_path = self.private_root / "logs" / "operations.jsonl"
        self.airport_path = self.private_root / "sources" / "owner" / "airport.yaml"
        self.home_path = self.private_root / "sources" / "owner" / "home.yaml"
        self.reference_sentinel = (
            self.private_root / "reference-configs" / "2026-08-21" / "reference.yaml"
        )
        self.reference_sentinel.write_text("reference", encoding="utf-8")

        self.template_dir = root / "templates"
        self._write_templates()
        self.write_airport_snapshot(
            {
                "name": "owner-airport",
                "type": "ss",
                "server": "airport.example.com",
                "port": 443,
                "cipher": "aes-128-gcm",
                "password": "synthetic-airport-e2e-password",
            }
        )
        self.write_home_snapshot(
            {
                "name": "owner-home",
                "type": "trojan",
                "server": "home.example.com",
                "port": 443,
                "password": "synthetic-home-e2e-password",
            }
        )
        self._write_settings_files()

        self.converter = FakeConverterClient(
            {
                self.owner_url: (self.reality_proxy("owner-xui", "11111111-1111-4111-8111-111111111111"),),
                self.friend_url: (self.reality_proxy("friend-xui", "22222222-2222-4222-8222-222222222222"),),
                self.airport_import_url: (
                    {
                        "name": "Imported Airport E2E",
                        "type": "ss",
                        "server": "198.51.100.20",
                        "port": 443,
                        "cipher": "aes-128-gcm",
                        "password": "synthetic-imported-e2e-password",
                    },
                ),
                self.airport_malformed_url: SourceError("airport snapshot is malformed"),
            }
        )
        self.traffic = FakeTrafficClient(
            {
                self.owner_url: SubscriptionUserinfo(10, 20, 100, 1893456000),
                self.friend_url: SubscriptionUserinfo(1, 2, 10, 1893456001),
            }
        )
        self.renderer = HarnessRenderer()
        self.validator = HarnessValidator(self.renderer)
        self.mihomo = FakeMihomoCommand()
        self.clock_value = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.now = 1000.0
        self._operation_counter = 0
        self._mtime_tick = int(os.stat(self.users_path).st_mtime)
        self.log_lines = []
        self.runtime = manager_module.ManagerRuntime(
            service_path=self.service_path,
            users_path=self.users_path,
            operation_log_path=self.operation_log_path,
            template_dir=self.template_dir,
            converter_factory=lambda _base_url: self.converter,
            traffic_client_factory=lambda: self.traffic,
            renderer=self.renderer,
            validator=self.validator,
            clock=lambda: self.clock_value,
        )
        self.cli = HarnessCli(self)
        self.publisher = PublisherDriver(self.make_publisher_service())

    # -- wiring helpers -------------------------------------------------

    def make_publisher_service(self, traffic_client=None) -> PublicationService:
        return PublicationService(
            settings_loader=lambda: load_settings(self.service_path, self.users_path),
            traffic_client=traffic_client or self.traffic,
            clock=lambda: self.now,
            settings_revision=lambda: settings_file_revision(
                (self.service_path, self.users_path)
            ),
            log_sink=self.log_lines.append,
        )

    def next_operation_id(self) -> str:
        self.clock_value = self.clock_value + timedelta(minutes=1)
        self._operation_counter += 1
        return "op-e2e-%02d" % self._operation_counter

    def run_manager(self, arguments, stdin=""):
        stdout = io.StringIO()
        stderr = io.StringIO()
        returncode = manager_module.main(
            list(arguments),
            stdin=io.StringIO(stdin),
            stdout=stdout,
            stderr=stderr,
            runtime=self.runtime,
        )
        return SimpleNamespace(
            returncode=returncode, stdout=stdout.getvalue(), stderr=stderr.getvalue()
        )

    def current_release(self, user_id: str):
        link = self.private_root / "current" / user_id
        if not link.exists():
            return None
        return link.resolve().name

    def history(self, user_id: str):
        result = self.run_manager(["history", user_id])
        payload = json.loads(result.stdout)
        return [entry["release_id"] for entry in payload["releases"]]

    def rollback(self, user_id: str, release_id: str):
        result = self.run_manager(["rollback", user_id, release_id])
        return SimpleNamespace(ok=result.returncode == 0, result=result)

    def rotate_link(self, user_id: str):
        result = self.run_manager(["rotate-token", user_id])
        payload = json.loads(result.stdout)
        self.bump_users_mtime()
        return SimpleNamespace(token=payload["token"], urls=payload["urls"])

    def bump_users_mtime(self) -> None:
        self._mtime_tick += 1
        os.utime(self.users_path, (self._mtime_tick, self._mtime_tick))

    def write_airport_snapshot(self, proxy) -> None:
        self._write_snapshot(self.airport_path, proxy)

    def write_home_snapshot(self, proxy) -> None:
        self._write_snapshot(self.home_path, proxy)

    def _write_snapshot(self, path: Path, proxy) -> None:
        path.write_text(
            yaml.safe_dump({"proxies": [proxy]}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def _write_templates(self) -> None:
        (self.template_dir / "variants").mkdir(parents=True)
        (self.template_dir / "clash.yaml.j2").write_text(
            "{{ PROXIES_ROOT_YAML }}\n"
            "{{ VARIANT_DNS_ROOT_YAML }}\n"
            "{{ VARIANT_PROXY_GROUPS_ROOT_YAML }}\n"
            "{{ VARIANT_RULE_PROVIDERS_ROOT_YAML }}\n"
            "{{ VARIANT_RULES_ROOT_YAML }}\n",
            encoding="utf-8",
        )
        variant_differences = {
            "balanced": (
                {"enable": True, "ipv6": False},
                ["GEOIP,LAN,DIRECT,no-resolve", "MATCH,Selector"],
            ),
            "balanced-win": (
                {"enable": True, "ipv6": False, "respect-rules": True},
                ["GEOIP,LAN,DIRECT,no-resolve", "MATCH,Selector"],
            ),
            "privacy": (
                {"enable": True, "enhanced-mode": "fake-ip"},
                ["MATCH,Selector"],
            ),
        }
        for variant, (dns, rules) in variant_differences.items():
            document = {
                "_generator": {"inject-node-groups": ["Selector"]},
                "dns": dns,
                "proxy-groups": [
                    {"name": "Selector", "type": "select", "proxies": ["DIRECT"]}
                ],
                "rule-providers": {
                    "Example": {
                        "type": "http",
                        "behavior": "classical",
                        "url": "https://rules.example/example.yaml",
                        "path": "./rules/example.yaml",
                    }
                },
                "rules": rules,
            }
            (self.template_dir / "variants" / ("%s.yaml" % variant)).write_text(
                yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )

    def _write_settings_files(self) -> None:
        service = {
            "schema-version": 1,
            "private-root": str(self.private_root),
            "converter-base-url": "http://127.0.0.1:25500",
            "publication": {
                "mode": "domain",
                "subscription-authority": "sub.example.com:8443",
                "panel-authority": "panel.example.com:8443",
                "publisher-listen": "127.0.0.1",
                "publisher-port": 25501,
            },
            "reality": {
                "public-address": "198.51.100.25",
                "public-port": 443,
                "required-flow": "xtls-rprx-vision",
            },
            "xui": {
                "panel-listen": "127.0.0.1",
                "panel-port": 2053,
                "panel-base-path": "/panel/",
                "subscription-listen": "127.0.0.1",
                "subscription-port": 2096,
                "xray-config-path": str(self.private_root / "xray-config.json"),
                "xray-binary-path": str(self.private_root / "xray"),
                "expected-panel-version": "3.6.0",
                "expected-xray-version": "26.6.27",
            },
            "certificate": {
                "fullchain-path": str(self.private_root / "fullchain.pem"),
                "acme-email": "admin@example.com",
                "alert-before-seconds": 1209600,
                "alert-command": [],
            },
        }
        users = {
            "schema-version": 1,
            "users": {
                "owner": {
                    "role": "owner",
                    "token-sha256": hash_token(self.owner_token),
                    "variants": ["balanced", "balanced-win", "privacy"],
                    "xui-subscription-url": self.owner_url,
                    "local-sources": {
                        "airport": "sources/owner/airport.yaml",
                        "home": "sources/owner/home.yaml",
                    },
                },
                "friend": {
                    "role": "member",
                    "token-sha256": hash_token(self.friend_token),
                    "variants": ["balanced"],
                    "xui-subscription-url": self.friend_url,
                    "local-sources": {},
                },
            },
        }
        for path, document in ((self.service_path, service), (self.users_path, users)):
            path.write_text(
                yaml.safe_dump(document, sort_keys=False),
                encoding="utf-8",
            )
            path.chmod(0o600)

    @staticmethod
    def reality_proxy(name: str, uuid: str):
        return {
            "name": name,
            "type": "vless",
            "server": "127.0.0.1",
            "port": 2096,
            "uuid": uuid,
            "network": "tcp",
            "tls": True,
            "flow": "xtls-rprx-vision",
            "servername": "www.example.com",
            "client-fingerprint": "chrome",
            "reality-opts": {
                "public-key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "short-id": "0123456789abcdef",
            },
        }


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.harness = EndToEndHarness(Path(directory.name))
        self.cli = self.harness.cli
        self.manager = self.harness
        self.publisher = self.harness.publisher
        self.validator = self.harness.validator
        self.mihomo = self.harness.mihomo
        self.traffic = self.harness.traffic
        self.owner_token = self.harness.owner_token
        self.friend_token = self.harness.friend_token

    # -- generation and isolation ---------------------------------------

    def test_member_and_owner_generate_only_authorized_sources(self):
        friend_refresh = self.cli.refresh("friend")
        owner_refresh = self.cli.refresh("owner")

        self.assertTrue(friend_refresh.ok, friend_refresh.error_code)
        self.assertTrue(owner_refresh.ok, owner_refresh.error_code)

        friend = self.publisher.get(self.friend_token, "balanced")
        owner = self.publisher.get(self.owner_token, "balanced")

        self.assertEqual(friend.status, 200)
        self.assertEqual(owner.status, 200)
        self.assertIn(b"friend-xui", friend.body)
        self.assertNotIn(b"owner-xui", friend.body)
        self.assertNotIn(b"owner-airport", friend.body)
        self.assertNotIn(b"owner-home", friend.body)
        self.assertIn(b"owner-xui", owner.body)
        self.assertIn(b"owner-airport", owner.body)
        self.assertIn(b"owner-home", owner.body)

    def test_three_variant_failure_preserves_previous_owner_release(self):
        previous = self.cli.refresh("owner").release_id
        mihomo_calls_before = len(self.mihomo.calls)

        self.validator.fail_variant = "privacy"
        failed = self.cli.refresh("owner")

        self.assertFalse(failed.ok)
        self.assertEqual(failed.error_code, "validation_failed")
        self.assertEqual(self.manager.current_release("owner"), previous)
        self.assertEqual(len(self.mihomo.calls), mihomo_calls_before)

    def test_mihomo_variant_failure_publishes_nothing(self):
        previous = self.cli.refresh("owner").release_id

        self.mihomo.fail_variant = "balanced-win"
        failed = self.cli.refresh("owner")

        self.assertFalse(failed.ok)
        self.assertEqual(failed.error_code, "mihomo_validation_failed")
        self.assertEqual(failed.variant, "balanced-win")
        self.assertEqual(self.manager.current_release("owner"), previous)
        self.assertEqual(
            self.harness.history("owner"),
            [self.manager.current_release("owner")],
        )

    def test_owner_serves_all_three_variants(self):
        self.assertTrue(self.cli.refresh("owner").ok)

        responses = {
            variant: self.publisher.get(self.owner_token, variant)
            for variant in ("balanced", "balanced-win", "privacy")
        }

        for variant, response in responses.items():
            self.assertEqual(response.status, 200, variant)
            self.assertEqual(
                response.headers["Content-Type"], "text/yaml; charset=utf-8"
            )
        bodies = [response.body for response in responses.values()]
        self.assertEqual(len(set(bodies)), 3)

    def test_member_token_cannot_fetch_forbidden_variant_and_404s_are_identical(self):
        self.assertTrue(self.cli.refresh("friend").ok)

        unknown = self.publisher.get("totally-unknown-token-value", "balanced")
        forbidden = self.publisher.get(self.friend_token, "privacy")

        self.assertEqual(unknown.status, 404)
        self.assertEqual(forbidden.status, 404)
        self.assertEqual(unknown.body, forbidden.body)
        self.assertEqual(unknown.headers, forbidden.headers)

    def test_airport_import_refreshes_owner_without_changing_friend(self):
        friend_before = self.cli.refresh("friend").release_id
        owner_before = self.cli.refresh("owner").release_id

        result = self.cli.airport(self.harness.airport_import_url)

        self.assertTrue(result.ok, result.error_code)
        self.assertEqual(self.manager.current_release("friend"), friend_before)
        self.assertNotEqual(self.manager.current_release("owner"), owner_before)
        owner = self.publisher.get(self.owner_token, "balanced")
        self.assertIn(b"Imported Airport E2E", owner.body)
        self.assertNotIn(b"owner-airport\n", owner.body)

    def test_malformed_airport_import_preserves_old_snapshot_and_release(self):
        owner_before = self.cli.refresh("owner").release_id
        snapshot_before = self.harness.airport_path.read_bytes()

        result = self.cli.airport(self.harness.airport_malformed_url)

        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "source_failed")
        self.assertEqual(self.manager.current_release("owner"), owner_before)
        self.assertEqual(self.harness.airport_path.read_bytes(), snapshot_before)

    # -- traffic metadata ------------------------------------------------

    def test_traffic_header_uses_cache_then_sidecar_when_live_fails(self):
        self.assertTrue(self.cli.refresh("friend").ok)

        first = self.publisher.get(self.friend_token, "balanced")
        self.assertEqual(first.status, 200)
        self.assertEqual(
            first.headers["Subscription-Userinfo"],
            "upload=1; download=2; total=10; expire=1893456001",
        )

        self.traffic.fail = True
        self.harness.now += TRAFFIC_CACHE_SECONDS + 1
        cached = self.publisher.get(self.friend_token, "balanced")
        self.assertEqual(cached.status, 200)
        self.assertEqual(
            cached.headers["Subscription-Userinfo"],
            first.headers["Subscription-Userinfo"],
        )

        failing_only = PublisherDriver(
            self.harness.make_publisher_service(
                traffic_client=FakeTrafficClient({})
            )
        )
        sidecar = failing_only.get(self.friend_token, "balanced")
        self.assertEqual(sidecar.status, 200)
        self.assertEqual(
            sidecar.headers["Subscription-Userinfo"],
            first.headers["Subscription-Userinfo"],
        )

    # -- links and retention ----------------------------------------------

    def test_token_rotation_invalidates_the_old_link(self):
        self.assertTrue(self.cli.refresh("friend").ok)

        self.assertEqual(
            self.publisher.get(self.friend_token, "balanced").status, 200
        )

        rotation = self.manager.rotate_link("friend")

        self.assertNotEqual(rotation.token, self.friend_token)
        self.assertEqual(
            self.publisher.get(self.friend_token, "balanced").status, 404
        )
        fresh = self.publisher.get(rotation.token, "balanced")
        self.assertEqual(fresh.status, 200)
        self.assertIn(b"friend-xui", fresh.body)

    def test_sixth_release_prunes_oldest_and_keeps_references(self):
        for _ in range(6):
            self.assertTrue(self.cli.refresh("owner").ok)

        history = self.harness.history("owner")

        self.assertEqual(len(history), 5)
        self.assertEqual(history[0], self.manager.current_release("owner"))
        releases_root = self.harness.private_root / "releases" / "owner"
        self.assertEqual(
            sorted(child.name for child in releases_root.iterdir()),
            sorted(history),
        )
        self.assertTrue(self.harness.reference_sentinel.exists())
        self.assertTrue(
            (self.harness.private_root / "reference-configs" / "2026-08-21").is_dir()
        )

    def test_rollback_serves_exact_previous_bytes_and_tampering_fails_closed(self):
        first = self.cli.refresh("owner")
        first_bytes = self.publisher.get(self.owner_token, "balanced").body
        self.harness.write_home_snapshot(
            {
                "name": "owner-home-2",
                "type": "trojan",
                "server": "home2.example.com",
                "port": 443,
                "password": "synthetic-home-e2e-password-2",
            }
        )
        second = self.cli.refresh("owner")

        self.assertNotEqual(first.release_id, second.release_id)
        self.assertNotEqual(
            self.publisher.get(self.owner_token, "balanced").body, first_bytes
        )

        rolled_back = self.manager.rollback("owner", first.release_id)

        self.assertTrue(rolled_back.ok)
        self.assertEqual(
            self.publisher.get(self.owner_token, "balanced").body, first_bytes
        )

        release_path = (
            self.harness.private_root / "releases" / "owner" / first.release_id
        )
        (release_path / "balanced.yaml").write_text("tampered", encoding="utf-8")
        self.assertEqual(
            self.publisher.get(self.owner_token, "balanced").status, 404
        )

    def test_downloads_never_trigger_generation(self):
        self.assertTrue(self.cli.refresh("owner").ok)
        converter_calls_before = len(self.harness.converter.calls)
        releases_before = sorted(
            child.name
            for child in (self.harness.private_root / "releases" / "owner").iterdir()
        )
        staging = self.harness.private_root / "staging"
        staging_before = sorted(
            child.name for child in staging.iterdir()
        ) if staging.exists() else []

        for _ in range(3):
            self.assertEqual(
                self.publisher.get(self.owner_token, "balanced").status, 200
            )
            self.assertEqual(
                self.publisher.get(self.owner_token, "privacy").status, 200
            )

        self.assertEqual(len(self.harness.converter.calls), converter_calls_before)
        self.assertEqual(
            sorted(
                child.name
                for child in (self.harness.private_root / "releases" / "owner").iterdir()
            ),
            releases_before,
        )
        self.assertEqual(
            sorted(child.name for child in staging.iterdir()) if staging.exists() else [],
            staging_before,
        )

    # -- no scheduled generation -------------------------------------------

    def test_certificate_units_and_compose_never_generate_configurations(self):
        deploy_dir = REPO_ROOT / "deploy" / "systemd"
        unit_paths = sorted(deploy_dir.iterdir())
        self.assertTrue(unit_paths)
        for unit_path in unit_paths:
            for line in unit_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped.startswith(("ExecStart", "ExecStartPost", "-ExecStartPost")):
                    continue
                command = stripped.split("=", 1)[1].strip()
                self.assertTrue(
                    command.startswith("/opt/certbot/bin/certbot")
                    or "/opt/clash-sub/scripts/check_certificate.py" in command,
                    "%s schedules an unexpected command" % unit_path.name,
                )

        compose_text = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")
        compose = yaml.safe_load(compose_text)
        for service_name in ("manager", "validator"):
            self.assertIn(
                "manual",
                compose["services"][service_name].get("profiles", []),
                service_name,
            )
            self.assertNotIn(
                "restart", compose["services"][service_name], service_name
            )


if __name__ == "__main__":
    unittest.main()
