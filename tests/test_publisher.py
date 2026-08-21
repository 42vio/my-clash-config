import copy
import hashlib
import http.client
import io
import json
import os
import re
import threading
import time
import unittest
from contextlib import redirect_stderr
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from clash_sub.models import Request, Response, SubscriptionUserinfo
from clash_sub.publisher import (
    CONNECTION_TIMEOUT_SECONDS,
    LRU_MAX_ENTRIES,
    MAX_TARGET_BYTES,
    MAX_YAML_BYTES,
    RATE_LIMIT_BURST,
    TRAFFIC_CACHE_SECONDS,
    PublicationService,
    PublisherRequestHandler,
    create_publication_server,
    resolve_client_ip,
    settings_file_revision,
)
from clash_sub.releases import ReleaseBuilder, publish_candidate
from clash_sub.settings import hash_token, load_settings
from clash_sub.traffic import TrafficError
from clash_sub.validation import sha256_bytes


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
    def __init__(self, live_value=None):
        self.live_value = live_value
        self.calls = 0
        self.fail = False
        self.missing_header = False
        self.fail_after_first_call = False

    def fetch(self, source_url: str):
        self.calls += 1
        if self.fail:
            raise TrafficError("synthetic live fetch failure")
        if self.missing_header:
            return None
        if self.fail_after_first_call and self.calls > 1:
            raise TrafficError("synthetic live fetch failure")
        return self.live_value


class FakeLocalLoader:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def __call__(self, path: Path):
        self.calls.append(path)
        response = self._responses[path]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class FakeRenderer:
    def __init__(self):
        self.calls = []

    def __call__(self, template_dir: Path, variant: str, private_proxy_snapshot):
        proxies = copy.deepcopy(list(private_proxy_snapshot))
        self.calls.append((template_dir, variant, [proxy["name"] for proxy in proxies]))
        document = {
            "dns": {"enable": True},
            "proxies": proxies,
            "proxy-groups": [
                {
                    "name": "Selector",
                    "type": "select",
                    "proxies": ["DIRECT"] + [proxy["name"] for proxy in proxies],
                }
            ],
            "rule-providers": {
                "Apple": {
                    "type": "http",
                    "behavior": "classical",
                    "url": "https://rules.example/apple.yaml",
                    "path": "./rules/apple.yaml",
                }
            },
            "rules": ["MATCH,Selector"],
            "variant": variant,
        }
        return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


class FakeValidator:
    def __init__(self):
        self.calls = []

    def __call__(self, text: str, source_urls, reality):
        self.calls.append((text, tuple(source_urls), reality))
        return yaml.safe_load(text)


class PublicationFixture:
    """Builds real settings files and published releases for publisher tests."""

    owner_token = "owner-example-token-0123456789abcdef"
    friend_token = "friend-example-token-0123456789abcdef"
    rotated_owner_token = "owner-rotated-token-abcdef0123456789"
    owner_url = "http://127.0.0.1:2096/sub/example-owner-sub-id"
    friend_url = "http://127.0.0.1:2096/sub/example-friend-sub-id"
    friend_live = SubscriptionUserinfo(1, 2, 10, 1893456001)
    build_clock_value = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)

    def __init__(self, root: Path):
        self.root = root
        self.private_root = root / "private"
        (self.private_root / "sources" / "owner").mkdir(parents=True, mode=0o700)
        (self.private_root / "config").mkdir(mode=0o700)
        self.airport_path = self.private_root / "sources" / "owner" / "airport.yaml"
        self.home_path = self.private_root / "sources" / "owner" / "home.yaml"
        self.airport_path.write_text("synthetic-airport", encoding="utf-8")
        self.home_path.write_text("synthetic-home", encoding="utf-8")
        self.airport_path.chmod(0o600)
        self.home_path.chmod(0o600)

        self.template_dir = root / "templates"
        (self.template_dir / "variants").mkdir(parents=True)
        (self.template_dir / "clash.yaml.j2").write_text(
            "{{ PROXIES_ROOT_YAML }}\n{{ VARIANT_PROXY_GROUPS_ROOT_YAML }}\n{{ VARIANT_RULES_ROOT_YAML }}\n",
            encoding="utf-8",
        )
        for variant in ("balanced", "balanced-win", "privacy"):
            (self.template_dir / "variants" / ("%s.yaml" % variant)).write_text(
                yaml.safe_dump(
                    {
                        "_generator": {"inject-node-groups": ["Selector"]},
                        "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["DIRECT"]}],
                        "rules": ["MATCH,DIRECT"],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

        self.service_path = self.private_root / "config" / "service.yaml"
        self.users_path = self.private_root / "config" / "users.yaml"
        self._mtime_tick = int(time.time())
        self._write_config_files()

        self.settings = self.load_settings()
        self.traffic_client = FakeTrafficClient(self.friend_live)
        self.releases = {}
        self.friend_balanced_bytes = self._publish_release("friend", "op-publisher-friend", "balanced")
        self.owner_balanced_bytes = self._publish_release("owner", "op-publisher-owner", "balanced")
        self.owner_privacy_bytes = self.releases["owner"].files["privacy"].read_bytes()
        self.traffic_client.calls = 0
        self.now = 1000.0
        self.log_lines = []

    def load_settings(self):
        return load_settings(self.service_path, self.users_path)

    def settings_revision(self):
        return settings_file_revision((self.service_path, self.users_path))

    def make_service(self, **overrides):
        parameters = dict(
            settings_loader=self.load_settings,
            traffic_client=self.traffic_client,
            clock=lambda: self.now,
            settings_revision=self.settings_revision,
            log_sink=self.log_lines.append,
        )
        parameters.update(overrides)
        return PublicationService(**parameters)

    def valid_users(self):
        return {
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
        }

    def rewrite_users(self, users):
        self.users_path.write_text(
            yaml.safe_dump(
                {"schema-version": 1, "users": users},
                sort_keys=False,
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        self.users_path.chmod(0o600)
        self._bump_mtime(self.users_path)

    def corrupt_service_file(self):
        self.service_path.write_text("::: not valid yaml {{{", encoding="utf-8")
        self.service_path.chmod(0o600)
        self._bump_mtime(self.service_path)

    def loosen_service_permissions(self):
        self.service_path.chmod(0o644)
        self._bump_mtime(self.service_path)

    def restore_service_permissions(self):
        self.service_path.chmod(0o600)
        self._bump_mtime(self.service_path)

    def _bump_mtime(self, path: Path):
        self._mtime_tick += 1
        os.utime(path, (self._mtime_tick, self._mtime_tick))

    def _write_config_files(self):
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
                "xray-config-path": str(self.private_root / "xray" / "config.json"),
                "xray-binary-path": str(self.private_root / "xray" / "xray"),
                "expected-panel-version": "3.6.0",
                "expected-xray-version": "26.6.27",
            },
            "certificate": {
                "fullchain-path": str(self.private_root / "certs" / "fullchain.pem"),
                "alert-before-seconds": 1209600,
                "alert-command": [],
            },
        }
        documents = (
            (self.service_path, service),
            (self.users_path, {"schema-version": 1, "users": self.valid_users()}),
        )
        for path, document in documents:
            path.write_text(
                yaml.safe_dump(document, sort_keys=False, default_flow_style=False),
                encoding="utf-8",
            )
            path.chmod(0o600)
            self._bump_mtime(path)

    def reality_proxy(self, name: str, uuid: str):
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

    def _publish_release(self, user_id: str, operation_id: str, variant: str) -> bytes:
        converter = FakeConverterClient(
            {
                self.owner_url: (
                    self.reality_proxy("owner-xui-node", "11111111-1111-4111-8111-111111111111"),
                ),
                self.friend_url: (
                    self.reality_proxy("friend-node", "22222222-2222-4222-8222-222222222222"),
                ),
            }
        )
        local_loader = FakeLocalLoader(
            {
                self.airport_path.resolve(): (
                    {
                        "name": "owner-airport-node",
                        "type": "trojan",
                        "server": "airport.example.com",
                        "port": 443,
                        "password": "airport-password",
                    },
                ),
                self.home_path.resolve(): (
                    {
                        "name": "owner-home-node",
                        "type": "trojan",
                        "server": "home.example.com",
                        "port": 443,
                        "password": "home-password",
                    },
                ),
            }
        )
        builder = ReleaseBuilder(
            self.load_settings(),
            converter,
            self.traffic_client,
            local_loader=local_loader,
            renderer=FakeRenderer(),
            validator=FakeValidator(),
            template_dir=self.template_dir,
            clock=lambda: self.build_clock_value,
        )
        candidate = builder.build_candidate(user_id, operation_id)
        release = publish_candidate(candidate, self.private_root, keep=5)
        self.releases[user_id] = release
        return release.files[variant].read_bytes()


class PublisherTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fx = PublicationFixture(Path(directory.name))
        self.service = self.fx.make_service()

    @property
    def friend_token(self):
        return self.fx.friend_token

    @property
    def friend_balanced_bytes(self):
        return self.fx.friend_balanced_bytes

    @property
    def traffic_client(self):
        return self.fx.traffic_client

    def request(self, method, path, client_ip="127.0.0.1", peer_ip=None, headers=None, service=None):
        active = service if service is not None else self.service
        return active.handle(
            Request(
                method=method,
                path=path,
                client_ip=client_ip,
                peer_ip=peer_ip if peer_ip is not None else client_ip,
                headers=headers or {},
            )
        )

    def subscription_path(self, token, variant):
        return "/s/%s/%s.yaml" % (token, variant)

    def friend_path(self, variant="balanced"):
        return self.subscription_path(self.fx.friend_token, variant)

    def unknown_reference(self, client_ip="203.0.113.99"):
        return self.request("GET", "/s/unknown-token/balanced.yaml", client_ip=client_ip)

    def assert_constant_not_found(self, response, reference):
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body, reference.body)
        self.assertEqual(response.headers, reference.headers)

    def test_valid_token_serves_only_current_allowed_variant(self):
        response = self.request(
            "GET",
            f"/s/{self.friend_token}/balanced.yaml",
            client_ip="127.0.0.1",
        )
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, self.friend_balanced_bytes)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.headers["Content-Type"], "text/yaml; charset=utf-8")
        self.assertEqual(
            response.headers["Content-Disposition"],
            'attachment; filename="balanced.yaml"',
        )
        self.assertEqual(response.headers["Pragma"], "no-cache")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(
            response.headers["Content-Length"], str(len(self.friend_balanced_bytes))
        )
        self.assertEqual(
            response.headers["Subscription-Userinfo"],
            self.fx.friend_live.header_value,
        )
        self.assertNotIn("profile-update-interval", response.headers)
        self.assertNotIn("update-interval", " ".join(response.headers))

    def test_unknown_token_and_forbidden_variant_are_indistinguishable(self):
        unknown = self.request("GET", "/s/unknown/balanced.yaml")
        forbidden = self.request(
            "GET",
            f"/s/{self.friend_token}/privacy.yaml",
        )
        self.assertEqual(unknown.status, 404)
        self.assertEqual(forbidden.status, 404)
        self.assertEqual(unknown.body, forbidden.body)
        self.assertEqual(unknown.headers, forbidden.headers)

    def test_live_traffic_failure_falls_back_without_blocking_download(self):
        self.traffic_client.fail_after_first_call = True
        first = self.request("GET", f"/s/{self.friend_token}/balanced.yaml")
        second = self.request("GET", f"/s/{self.friend_token}/balanced.yaml")
        self.assertEqual(second.status, 200)
        self.assertEqual(
            second.headers["Subscription-Userinfo"],
            first.headers["Subscription-Userinfo"],
        )
        self.assertEqual(self.traffic_client.calls, 1)
        self.assertEqual(second.body, self.friend_balanced_bytes)

    def test_head_returns_same_headers_and_empty_body(self):
        get = self.request("GET", self.friend_path())
        head = self.request("HEAD", self.friend_path())
        self.assertEqual(head.status, get.status)
        self.assertEqual(head.headers, get.headers)
        self.assertEqual(head.body, b"")
        self.assertEqual(
            head.headers["Content-Length"], str(len(self.friend_balanced_bytes))
        )

    def test_only_get_and_head_accepted(self):
        for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS"):
            response = self.request(method, self.friend_path())
            self.assertEqual(response.status, 405, method)
            self.assertEqual(response.headers["Allow"], "GET, HEAD")
            self.assertNotIn(self.fx.friend_token, response.body.decode("utf-8", "replace"))

    def test_mutation_methods_on_healthz_rejected(self):
        for method in ("POST", "PUT", "DELETE", "PATCH"):
            response = self.request(method, "/healthz", client_ip="127.0.0.1")
            self.assertEqual(response.status, 405, method)
            self.assertEqual(response.headers["Allow"], "GET, HEAD")

    def test_healthz_for_loopback_only_and_leaks_nothing(self):
        response = self.request("GET", "/healthz", client_ip="127.0.0.1")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, b'{"status": "ok"}\n')
        body_text = response.body.decode("utf-8")
        for secret in (
            self.fx.friend_token,
            self.fx.owner_token,
            "friend",
            "owner",
            "releases",
            "op-publisher",
            "balanced",
            "manifest",
        ):
            self.assertNotIn(secret, body_text)
        ipv6 = self.request("GET", "/healthz", client_ip="::1")
        self.assertEqual(ipv6.status, 200)
        remote = self.request("GET", "/healthz", client_ip="203.0.113.9")
        self.assertEqual(remote.status, 404)
        self.assert_constant_not_found(remote, self.unknown_reference())

    def test_malformed_paths_rejected_like_unknown_tokens(self):
        token = self.fx.friend_token
        paths = (
            "/s/%2F%2E%2E/balanced.yaml",
            f"/s/{token}/%2e%2e/balanced.yaml",
            "/s/../owner/balanced.yaml",
            f"/s/{token}/../owner/balanced.yaml",
            "/s/./balanced.yaml",
            "/s//balanced.yaml",
            f"/s/{token}/",
            f"/s/{token}/balanced.yaml/extra",
            f"/s/{token}/balanced.yaml?refresh=1",
            f"/s/{token}/balanced.yaml#section",
            f"/s/{token}/balanced.txt",
            f"/s/{token}/BALANCED.yaml",
            f"/s/{token}\\escaped/balanced.yaml",
            f"/s/{token}/bal.anced.yaml",
            f"s/{token}/balanced.yaml",
            f"/S/{token}/balanced.yaml",
        )
        reference = self.unknown_reference(client_ip="203.0.113.50")
        for index, path in enumerate(paths):
            response = self.request("GET", path, client_ip="203.0.113.%d" % (index + 1))
            self.assertEqual(response.status, 404, path)
            self.assert_constant_not_found(response, reference)

    def test_settings_mtime_change_rotates_tokens_and_allowlists_atomically(self):
        self.assertEqual(
            self.request("GET", self.subscription_path(self.fx.owner_token, "privacy")).status,
            200,
        )
        users = self.fx.valid_users()
        users["owner"]["token-sha256"] = hash_token(self.fx.rotated_owner_token)
        users["owner"]["variants"] = ["balanced"]
        self.fx.rewrite_users(users)

        stale = self.request("GET", self.subscription_path(self.fx.owner_token, "balanced"))
        self.assert_constant_not_found(stale, self.unknown_reference())
        rotated = self.request(
            "GET", self.subscription_path(self.fx.rotated_owner_token, "balanced")
        )
        self.assertEqual(rotated.status, 200)
        self.assertEqual(rotated.body, self.fx.owner_balanced_bytes)
        rotated_forbidden = self.request(
            "GET", self.subscription_path(self.fx.rotated_owner_token, "privacy")
        )
        self.assert_constant_not_found(rotated_forbidden, self.unknown_reference())
        friend = self.request("GET", self.friend_path())
        self.assertEqual(friend.status, 200)

    def test_invalid_settings_preserve_last_good_in_memory_settings(self):
        self.fx.corrupt_service_file()
        corrupted = self.request("GET", self.friend_path())
        self.assertEqual(corrupted.status, 200)
        self.assertEqual(corrupted.body, self.fx.friend_balanced_bytes)

        self.fx.restore_service_permissions()
        self.fx.loosen_service_permissions()
        loosened = self.request("GET", self.friend_path())
        self.assertEqual(loosened.status, 200)
        self.assertEqual(loosened.body, self.fx.friend_balanced_bytes)

        self.fx.restore_service_permissions()
        recovered = self.request("GET", self.friend_path())
        self.assertEqual(recovered.status, 200)
        self.assertEqual(recovered.body, self.fx.friend_balanced_bytes)

    def test_missing_current_link_fails_closed(self):
        (self.fx.private_root / "current" / "friend").unlink()
        response = self.request("GET", self.friend_path())
        self.assert_constant_not_found(response, self.unknown_reference())

    def test_current_link_outside_user_release_root_fails_closed(self):
        link = self.fx.private_root / "current" / "friend"
        link.unlink()
        os.symlink(
            os.path.relpath(self.fx.releases["owner"].path, str(link.parent)), link
        )
        owner_release = self.request("GET", self.friend_path())
        self.assert_constant_not_found(owner_release, self.unknown_reference())

        link.unlink()
        outside = self.fx.root / "outside-release"
        outside.mkdir()
        os.symlink(os.path.relpath(outside, str(link.parent)), link)
        escaped = self.request("GET", self.friend_path())
        self.assert_constant_not_found(escaped, self.unknown_reference())

    def test_bad_manifest_fails_closed(self):
        manifest_path = self.fx.releases["friend"].path / "manifest.json"
        manifest_path.write_text(":::not-json", encoding="utf-8")
        response = self.request("GET", self.friend_path())
        self.assert_constant_not_found(response, self.unknown_reference())

    def test_manifest_tampering_without_digest_fails_closed(self):
        manifest_path = self.fx.releases["friend"].path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["created_at"] = "2099-12-31T23:59:59Z"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        response = self.request("GET", self.friend_path())
        self.assert_constant_not_found(response, self.unknown_reference())

    def test_hash_mismatch_fails_closed(self):
        yaml_path = self.fx.releases["friend"].path / "balanced.yaml"
        yaml_path.write_bytes(yaml_path.read_bytes() + b"tampered\n")
        response = self.request("GET", self.friend_path())
        self.assert_constant_not_found(response, self.unknown_reference())

    def test_oversized_release_file_fails_closed(self):
        release_dir = self.fx.releases["friend"].path
        yaml_path = release_dir / "balanced.yaml"
        payload = b"# padding\n" + b"x" * (MAX_YAML_BYTES + 1)
        yaml_path.write_bytes(payload)
        manifest_path = release_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["output_hashes"]["balanced"] = sha256_bytes(payload)
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        manifest_path.write_text(manifest_text, encoding="utf-8")
        (release_dir / "manifest.sha256").write_text(
            sha256_bytes(manifest_text.encode("utf-8")) + "\n", encoding="utf-8"
        )
        response = self.request("GET", self.friend_path())
        self.assert_constant_not_found(response, self.unknown_reference())

    def test_incomplete_release_fails_closed(self):
        (self.fx.releases["friend"].path / "balanced.yaml").unlink()
        response = self.request("GET", self.friend_path())
        self.assert_constant_not_found(response, self.unknown_reference())

    def test_variant_absent_from_manifest_fails_closed(self):
        users = self.fx.valid_users()
        users["friend"]["variants"] = ["balanced", "privacy"]
        self.fx.rewrite_users(users)
        response = self.request("GET", self.subscription_path(self.fx.friend_token, "privacy"))
        self.assert_constant_not_found(response, self.unknown_reference())

    def test_live_traffic_cached_for_600_seconds(self):
        first = self.request("GET", self.friend_path())
        self.assertEqual(
            first.headers["Subscription-Userinfo"], self.fx.friend_live.header_value
        )
        self.assertEqual(self.traffic_client.calls, 1)
        self.traffic_client.live_value = SubscriptionUserinfo(7, 9, 100, 1893456999)

        self.fx.now += TRAFFIC_CACHE_SECONDS - 0.1
        cached = self.request("GET", self.friend_path())
        self.assertEqual(
            cached.headers["Subscription-Userinfo"], self.fx.friend_live.header_value
        )
        self.assertEqual(self.traffic_client.calls, 1)

        self.fx.now += 0.2
        refreshed = self.request("GET", self.friend_path())
        self.assertEqual(
            refreshed.headers["Subscription-Userinfo"],
            "upload=7; download=9; total=100; expire=1893456999",
        )
        self.assertEqual(self.traffic_client.calls, 2)

    def test_traffic_failure_falls_back_to_last_process_local_good_value(self):
        self.request("GET", self.friend_path())
        self.traffic_client.fail = True
        self.fx.now += TRAFFIC_CACHE_SECONDS + 1
        response = self.request("GET", self.friend_path())
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, self.fx.friend_balanced_bytes)
        self.assertEqual(
            response.headers["Subscription-Userinfo"],
            self.fx.friend_live.header_value,
        )
        self.assertEqual(self.traffic_client.calls, 2)

    def test_traffic_failure_without_history_uses_release_sidecar(self):
        failing = self.fx.make_service()
        self.traffic_client.fail = True
        response = self.request("GET", self.friend_path(), service=failing)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.headers["Subscription-Userinfo"],
            self.fx.friend_live.header_value,
        )
        self.assertEqual(self.traffic_client.calls, 1)

        missing_header = self.fx.make_service()
        self.traffic_client.fail = False
        self.traffic_client.missing_header = True
        response = self.request("GET", self.friend_path(), service=missing_header)
        self.assertEqual(response.status, 200)
        self.assertEqual(
            response.headers["Subscription-Userinfo"],
            self.fx.friend_live.header_value,
        )

    def test_sidecar_traffic_is_bound_to_verified_yaml_digest(self):
        sidecar_path = self.fx.releases["friend"].path / "balanced.meta.json"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        sidecar["yaml_sha256"] = "0" * 64
        sidecar["traffic"] = {
            "upload": 777,
            "download": 777,
            "total": 777,
            "expire": 777,
        }
        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        service = self.fx.make_service()
        self.traffic_client.fail = True
        response = self.request("GET", self.friend_path(), service=service)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, self.fx.friend_balanced_bytes)
        self.assertNotIn("Subscription-Userinfo", response.headers)

    def test_traffic_omitted_when_no_safe_value_exists(self):
        sidecar_path = self.fx.releases["friend"].path / "balanced.meta.json"
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        del sidecar["traffic"]
        sidecar_path.write_text(
            json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        service = self.fx.make_service()
        self.traffic_client.fail = True
        response = self.request("GET", self.friend_path(), service=service)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, self.fx.friend_balanced_bytes)
        self.assertNotIn("Subscription-Userinfo", response.headers)

    def test_total_zero_served_unchanged_as_unlimited_metadata(self):
        self.traffic_client.live_value = SubscriptionUserinfo(5, 7, 0, 1893456000)
        response = self.request("GET", self.friend_path())
        self.assertEqual(
            response.headers["Subscription-Userinfo"],
            "upload=5; download=7; total=0; expire=1893456000",
        )

    def test_rate_limit_allows_burst_then_sustained_rate(self):
        for _ in range(RATE_LIMIT_BURST):
            self.assertEqual(self.request("GET", self.friend_path()).status, 200)
        rejected = self.request("GET", self.friend_path())
        self.assertEqual(rejected.status, 429)

        self.fx.now += 2
        self.assertEqual(self.request("GET", self.friend_path()).status, 200)

        self.fx.now += 120
        for _ in range(RATE_LIMIT_BURST):
            self.assertEqual(self.request("GET", self.friend_path()).status, 200)
        self.assertEqual(self.request("GET", self.friend_path()).status, 429)

    def test_unknown_requests_keyed_only_by_client_address(self):
        for _ in range(RATE_LIMIT_BURST):
            response = self.request(
                "GET", "/s/unknown/balanced.yaml", client_ip="203.0.113.10"
            )
            self.assertEqual(response.status, 404)
        saturated = self.request(
            "GET", "/s/unknown/balanced.yaml", client_ip="203.0.113.10"
        )
        self.assertEqual(saturated.status, 429)
        other_client = self.request(
            "GET", "/s/unknown/balanced.yaml", client_ip="203.0.113.11"
        )
        self.assertEqual(other_client.status, 404)
        authorized_same_ip = self.request(
            "GET", self.friend_path(), client_ip="203.0.113.10"
        )
        self.assertEqual(authorized_same_ip.status, 200)

    def test_authorized_requests_add_token_hash_to_rate_key(self):
        for _ in range(RATE_LIMIT_BURST):
            self.assertEqual(
                self.request("GET", self.friend_path(), client_ip="203.0.113.20").status,
                200,
            )
        for _ in range(RATE_LIMIT_BURST):
            self.assertEqual(
                self.request(
                    "GET",
                    self.subscription_path(self.fx.owner_token, "balanced"),
                    client_ip="203.0.113.20",
                ).status,
                200,
            )
        friend = self.request("GET", self.friend_path(), client_ip="203.0.113.20")
        self.assertEqual(friend.status, 429)
        owner = self.request(
            "GET",
            self.subscription_path(self.fx.owner_token, "balanced"),
            client_ip="203.0.113.20",
        )
        self.assertEqual(owner.status, 429)

    def test_rate_limit_stores_are_bounded_lru(self):
        def address(index):
            return "10.%d.%d.%d" % (
                (index // 65536) % 256,
                (index // 256) % 256,
                index % 256 + 1,
            )

        statuses = set()
        for index in range(LRU_MAX_ENTRIES + 300):
            statuses.add(
                self.request(
                    "GET", "/s/unknown/balanced.yaml", client_ip=address(index)
                ).status
            )
        self.assertEqual(statuses, {404})
        self.assertEqual(len(self.service._anonymous_buckets), LRU_MAX_ENTRIES)
        self.assertLessEqual(len(self.service._authorized_buckets), LRU_MAX_ENTRIES)

    def test_rate_limit_rejection_does_not_log_keys(self):
        for _ in range(RATE_LIMIT_BURST + 1):
            self.request(
                "GET", "/s/unknown-secret-token/balanced.yaml", client_ip="203.0.113.77"
            )
        joined = "\n".join(self.fx.log_lines)
        self.assertNotIn("203.0.113.77", joined)
        self.assertNotIn("unknown-secret-token", joined)
        self.assertNotIn(hash_token("unknown-secret-token"), joined)
        self.assertTrue(any("error=rate_limited" in line for line in self.fx.log_lines))

    def test_sanitized_log_lines_contain_no_secrets(self):
        self.request("GET", self.friend_path())
        self.request("GET", "/s/unknown/balanced.yaml", client_ip="203.0.113.9")
        self.request("GET", "/healthz")
        pattern = re.compile(
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z [A-Z]+ \d+ "
            r"route=[0-9a-f]{16} bytes=\d+ error=[a-z_]+$"
        )
        subscription_route = hashlib.sha256(b"subscription").hexdigest()[:16]
        health_route = hashlib.sha256(b"health").hexdigest()[:16]
        self.assertEqual(len(self.fx.log_lines), 3)
        for line in self.fx.log_lines:
            self.assertRegex(line, pattern)
            self.assertNotIn(self.fx.friend_token, line)
            self.assertNotIn(self.fx.owner_token, line)
            self.assertNotIn("127.0.0.1", line)
            self.assertNotIn("203.0.113.9", line)
            self.assertNotIn("balanced", line)
        self.assertIn("route=%s" % subscription_route, self.fx.log_lines[0])
        self.assertIn("error=ok", self.fx.log_lines[0])
        self.assertIn("bytes=%d" % len(self.fx.friend_balanced_bytes), self.fx.log_lines[0])
        self.assertIn("error=not_found", self.fx.log_lines[1])
        self.assertIn("route=%s" % health_route, self.fx.log_lines[2])

    def test_default_log_sink_writes_sanitized_lines_to_stderr(self):
        service = self.fx.make_service(log_sink=None)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            response = self.request("GET", self.friend_path(), service=service)
        self.assertEqual(response.status, 200)
        output = stderr.getvalue()
        self.assertIn("GET 200", output)
        self.assertIn("route=", output)
        self.assertIn("error=ok", output)
        self.assertNotIn(self.fx.friend_token, output)
        self.assertNotIn("balanced", output)

    def test_model_representations_do_not_expose_tokens_or_bodies(self):
        request = Request(
            method="GET",
            path=self.friend_path(),
            client_ip="127.0.0.1",
            peer_ip="127.0.0.1",
            headers={"X-Real-Ip": "203.0.113.1"},
        )
        response = Response(
            status=200,
            headers={"Subscription-Userinfo": "upload=1; download=2; total=10; expire=3"},
            body=self.fx.friend_balanced_bytes,
        )
        self.assertNotIn(self.fx.friend_token, repr(request))
        self.assertNotIn("203.0.113.1", repr(request))
        self.assertNotIn("friend-node", repr(response))
        self.assertNotIn(self.fx.friend_token, repr(vars(self.service)))
        self.assertNotIn(self.fx.owner_token, repr(vars(self.service)))

    def test_x_real_ip_trusted_only_for_loopback_peers(self):
        self.assertEqual(
            resolve_client_ip("127.0.0.1", {"X-Real-Ip": "203.0.113.9"}), "203.0.113.9"
        )
        self.assertEqual(
            resolve_client_ip("::1", {"x-real-ip": "203.0.113.9"}), "203.0.113.9"
        )
        self.assertEqual(
            resolve_client_ip("203.0.113.9", {"X-Real-Ip": "198.51.100.7"}),
            "203.0.113.9",
        )
        self.assertEqual(
            resolve_client_ip("127.0.0.1", {"X-Real-Ip": "not-an-address"}), "127.0.0.1"
        )
        self.assertEqual(resolve_client_ip("127.0.0.1", {}), "127.0.0.1")
        forwarded = self.request(
            "GET",
            "/healthz",
            client_ip="203.0.113.9",
            peer_ip="127.0.0.1",
            headers={"X-Real-Ip": "203.0.113.9"},
        )
        self.assertEqual(forwarded.status, 404)


class PublisherServerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fx = PublicationFixture(Path(directory.name))
        self.service = self.fx.make_service()
        self.server = create_publication_server("127.0.0.1", 0, self.service)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def fetch(self, method, path, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, headers=headers or {})
            response = connection.getresponse()
            body = response.read()
            response_headers = {
                name.lower(): value for name, value in response.getheaders()
            }
            return response.status, response_headers, body
        finally:
            connection.close()

    def test_create_publication_server_rejects_non_loopback_listen(self):
        for listen in ("0.0.0.0", "localhost", "::1", "192.0.2.10"):
            with self.assertRaises(ValueError):
                create_publication_server(listen, 0, self.service)

    def test_server_serves_yaml_over_loopback_socket(self):
        status, headers, body = self.fetch(
            "GET", "/s/%s/balanced.yaml" % self.fx.friend_token
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, self.fx.friend_balanced_bytes)
        self.assertEqual(headers["content-type"], "text/yaml; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(
            headers["subscription-userinfo"], self.fx.friend_live.header_value
        )

        head_status, head_headers, head_body = self.fetch(
            "HEAD", "/s/%s/balanced.yaml" % self.fx.friend_token
        )
        self.assertEqual(head_status, 200)
        self.assertEqual(
            head_headers["content-length"], str(len(self.fx.friend_balanced_bytes))
        )
        self.assertEqual(head_body, b"")

    def test_server_rejects_mutation_methods(self):
        status, headers, _body = self.fetch(
            "POST", "/s/%s/balanced.yaml" % self.fx.friend_token
        )
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")

    def test_server_rejects_oversized_targets(self):
        target = "/s/%s/balanced.yaml" % ("a" * (MAX_TARGET_BYTES + 100))
        status, _headers, _body = self.fetch("GET", target)
        self.assertEqual(status, 414)

    def test_server_rejects_query_strings(self):
        status, _headers, _body = self.fetch(
            "GET", "/s/%s/balanced.yaml?x=1" % self.fx.friend_token
        )
        self.assertEqual(status, 404)

    def test_server_health_requires_effective_loopback_client(self):
        status, _headers, body = self.fetch("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body, b'{"status": "ok"}\n')
        forwarded_status, _headers, _body = self.fetch(
            "GET", "/healthz", headers={"X-Real-IP": "203.0.113.9"}
        )
        self.assertEqual(forwarded_status, 404)

    def test_stdlib_never_logs_tokenized_paths(self):
        handler_shell = type(
            "HandlerShell",
            (),
            {"log_message": PublisherRequestHandler.log_message},
        )()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            PublisherRequestHandler.log_message(
                None, "GET /s/%s/balanced.yaml 200 -", self.fx.friend_token
            )
            PublisherRequestHandler.log_error(
                handler_shell, "GET /s/%s/balanced.yaml", self.fx.friend_token
            )
        self.assertEqual(stderr.getvalue(), "")

    def test_handler_uses_fifteen_second_connection_timeout(self):
        self.assertEqual(PublisherRequestHandler.timeout, CONNECTION_TIMEOUT_SECONDS)
        self.assertEqual(CONNECTION_TIMEOUT_SECONDS, 15)


if __name__ == "__main__":
    unittest.main()
