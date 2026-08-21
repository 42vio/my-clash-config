import io
import json
import stat
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from clash_sub.converter import SourceError
from clash_sub.models import SubscriptionUserinfo
from clash_sub.releases import ReleaseBuilder, list_history, publish_candidate, rollback
from clash_sub.settings import SettingsError, hash_token, load_settings
from clash_sub.validation import ValidationError
from tests.test_releases import FakeConverterClient, FakeRenderer, FakeTrafficClient, FakeValidator

from clash_sub import manager as manager_module


def private_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.private_root = self.root / "private"
        (self.private_root / "config").mkdir(parents=True, mode=0o700)
        (self.private_root / "logs").mkdir(mode=0o700)
        (self.private_root / "sources" / "owner").mkdir(parents=True, mode=0o700)
        self.service_path = self.private_root / "config" / "service.yaml"
        self.users_path = self.private_root / "config" / "users.yaml"
        self.operation_log_path = self.private_root / "logs" / "operations.jsonl"
        self.airport_path = self.private_root / "sources" / "owner" / "airport.yaml"
        self.home_path = self.private_root / "sources" / "owner" / "home.yaml"
        self.clock_value = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.owner_url = "http://127.0.0.1:2096/sub/owner-private"
        self.friend_url = "http://127.0.0.1:2096/sub/friend-private"
        self.temp_airport_url = "https://airport.example/temp/private-value"
        self.template_dir = self.root / "templates"
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

        self.write_private_snapshot(
            self.airport_path,
            {
                "proxies": [
                    {
                        "name": "Airport Old",
                        "type": "ss",
                        "server": "198.51.100.10",
                        "port": 443,
                        "cipher": "aes-128-gcm",
                        "password": "airport-old-password",
                    }
                ]
            },
        )
        self.write_private_snapshot(
            self.home_path,
            {
                "proxies": [
                    {
                        "name": "Home Node",
                        "type": "trojan",
                        "server": "home.example.com",
                        "port": 443,
                        "password": "home-password",
                    }
                ]
            },
        )
        self.write_settings_files()

        self.converter = FakeConverterClient(
            {
                self.owner_url: (
                    self.reality_proxy(
                        "owner-node",
                        "11111111-1111-4111-8111-111111111111",
                    ),
                ),
                self.friend_url: (
                    self.reality_proxy(
                        "friend-node",
                        "22222222-2222-4222-8222-222222222222",
                    ),
                ),
                self.temp_airport_url: (
                    {
                        "name": "Imported Airport",
                        "type": "ss",
                        "server": "198.51.100.20",
                        "port": 443,
                        "cipher": "aes-128-gcm",
                        "password": "synthetic-password",
                    },
                ),
            }
        )
        self.traffic = FakeTrafficClient(
            {
                self.owner_url: SubscriptionUserinfo(10, 20, 100, 1893456000),
                self.friend_url: SubscriptionUserinfo(1, 2, 10, 1893456001),
            }
        )
        self.renderer = FakeRenderer()
        self.validator = FakeValidator()

    def write_private_snapshot(self, path: Path, document) -> None:
        path.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        path.chmod(0o600)

    def write_settings_files(self) -> None:
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
                "alert-before-seconds": 1209600,
                "alert-command": [],
            },
        }
        users = {
            "schema-version": 1,
            "users": {
                "owner": {
                    "role": "owner",
                    "token-sha256": "a" * 64,
                    "variants": ["balanced", "balanced-win", "privacy"],
                    "xui-subscription-url": self.owner_url,
                    "local-sources": {
                        "airport": "sources/owner/airport.yaml",
                        "home": "sources/owner/home.yaml",
                    },
                },
                "friend": {
                    "role": "member",
                    "token-sha256": "b" * 64,
                    "variants": ["balanced"],
                    "xui-subscription-url": self.friend_url,
                    "local-sources": {},
                },
            },
        }
        self.service_path.write_text(
            yaml.safe_dump(service, sort_keys=False),
            encoding="utf-8",
        )
        self.users_path.write_text(
            yaml.safe_dump(users, sort_keys=False),
            encoding="utf-8",
        )
        self.service_path.chmod(0o600)
        self.users_path.chmod(0o600)

    def runtime(self):
        return manager_module.ManagerRuntime(
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

    def run_manager(self, arguments, stdin="", runtime=None):
        stdout = io.StringIO()
        stderr = io.StringIO()
        returncode = manager_module.main(
            list(arguments),
            stdin=io.StringIO(stdin),
            stdout=stdout,
            stderr=stderr,
            runtime=runtime or self.runtime(),
        )
        return SimpleNamespace(
            returncode=returncode,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
        )

    def load_current_settings(self):
        return load_settings(self.service_path, self.users_path)

    def make_release_builder(self):
        return ReleaseBuilder(
            self.load_current_settings(),
            self.converter,
            self.traffic,
            renderer=self.renderer,
            validator=self.validator,
            template_dir=self.template_dir,
            clock=lambda: self.clock_value,
        )

    def publish_release_direct(self, user_id="owner", operation_id="op-owner"):
        candidate = self.make_release_builder().build_candidate(user_id, operation_id)
        return publish_candidate(candidate, self.private_root)

    def change_home_snapshot(self):
        self.write_private_snapshot(
            self.home_path,
            {
                "proxies": [
                    {
                        "name": "Home Node 2",
                        "type": "trojan",
                        "server": "home2.example.com",
                        "port": 443,
                        "password": "changed-password",
                    }
                ]
            },
        )

    def operation_log_text(self):
        if not self.operation_log_path.exists():
            return ""
        return self.operation_log_path.read_text(encoding="utf-8")

    def parsed_log_entries(self):
        text = self.operation_log_text().strip()
        if not text:
            return []
        return [json.loads(line) for line in text.splitlines()]

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

    def assert_error_code(self, result, code):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["error"]["code"], code)
        return payload

    def test_airport_import_reads_url_only_from_stdin(self):
        secret = self.temp_airport_url

        result = self.run_manager(["import-airport"], stdin=secret + "\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload,
            {"imported": True, "owner_refresh_required": True},
        )
        self.assertNotIn("private-value", result.stdout)
        self.assertNotIn("private-value", result.stderr)
        self.assertNotIn("private-value", self.operation_log_text())
        imported = yaml.safe_load(self.airport_path.read_text(encoding="utf-8"))
        self.assertEqual(imported["proxies"][0]["name"], "Imported Airport")
        self.assertEqual(private_mode(self.airport_path), 0o600)

    def test_import_airport_rejects_empty_stdin(self):
        original = self.airport_path.read_text(encoding="utf-8")

        result = self.run_manager(["import-airport"], stdin="\n")

        self.assert_error_code(result, "source_failed")
        self.assertEqual(self.airport_path.read_text(encoding="utf-8"), original)

    def test_import_airport_rejects_multiple_lines(self):
        result = self.run_manager(
            ["import-airport"],
            stdin=self.temp_airport_url + "\nhttps://airport.example/extra\n",
        )

        self.assert_error_code(result, "source_failed")

    def test_import_airport_rejects_non_https_url(self):
        result = self.run_manager(
            ["import-airport"],
            stdin="http://airport.example/not-https\n",
        )

        self.assert_error_code(result, "source_failed")

    def test_import_airport_rejects_embedded_credentials(self):
        result = self.run_manager(
            ["import-airport"],
            stdin="https://user:pass@airport.example/private\n",
        )

        payload = self.assert_error_code(result, "source_failed")
        self.assertNotIn("user:pass", json.dumps(payload))

    def test_import_airport_rejects_empty_conversion_without_replacing_snapshot(self):
        original = self.airport_path.read_text(encoding="utf-8")
        self.converter._responses[self.temp_airport_url] = ()

        result = self.run_manager(["import-airport"], stdin=self.temp_airport_url + "\n")

        self.assert_error_code(result, "source_failed")
        self.assertEqual(self.airport_path.read_text(encoding="utf-8"), original)

    def test_import_airport_preserves_previous_snapshot_on_atomic_replace_failure(self):
        original = self.airport_path.read_text(encoding="utf-8")

        with patch("clash_sub.manager.os.replace", side_effect=OSError("synthetic failure")):
            result = self.run_manager(["import-airport"], stdin=self.temp_airport_url + "\n")

        self.assert_error_code(result, "snapshot_write_failed")
        self.assertEqual(self.airport_path.read_text(encoding="utf-8"), original)

    def test_list_users_returns_roles_and_variant_subsets_only(self):
        result = self.run_manager(["list-users"])

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(
            payload["users"],
            [
                {
                    "user_id": "friend",
                    "role": "member",
                    "variants": ["balanced"],
                },
                {
                    "user_id": "owner",
                    "role": "owner",
                    "variants": ["balanced", "balanced-win", "privacy"],
                },
            ],
        )
        self.assertNotIn("token", result.stdout.lower())

    def test_build_and_publish_return_json_and_write_redacted_logs(self):
        build = self.run_manager(["build", "--operation-id", "op123", "--user", "friend"])

        self.assertEqual(build.returncode, 0, build.stderr)
        build_payload = json.loads(build.stdout)
        self.assertEqual(build_payload["user_id"], "friend")
        self.assertEqual(build_payload["operation_id"], "op123")
        self.assertEqual(build_payload["variants"], ["balanced"])
        self.assertTrue(Path(build_payload["candidate_path"]).is_dir())

        publish = self.run_manager(["publish", "--operation-id", "op123", "--user", "friend"])

        self.assertEqual(publish.returncode, 0, publish.stderr)
        publish_payload = json.loads(publish.stdout)
        self.assertEqual(publish_payload["user_id"], "friend")
        self.assertEqual(publish_payload["release_id"], "op123")
        self.assertEqual(publish_payload["variants"], ["balanced"])

        entries = self.parsed_log_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(
            set(entries[0]),
            {"timestamp", "operation", "user_id", "release_id", "status"},
        )
        self.assertEqual(entries[0]["operation"], "build")
        self.assertEqual(entries[1]["operation"], "publish")
        self.assertNotIn("friend-private", self.operation_log_text())

    def test_build_validation_error_uses_stable_redacted_code(self):
        runtime = self.runtime()
        runtime.validator = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            ValidationError("synthetic validation failure")
        )

        result = self.run_manager(
            ["build", "--operation-id", "op-bad", "--user", "friend"],
            runtime=runtime,
        )

        payload = self.assert_error_code(result, "validation_failed")
        self.assertNotIn(self.friend_url, json.dumps(payload))

    def test_publish_missing_candidate_returns_release_missing(self):
        result = self.run_manager(["publish", "--operation-id", "missing", "--user", "friend"])

        self.assert_error_code(result, "release_missing")

    def test_status_detects_changed_inputs_without_credentials(self):
        self.publish_release_direct()
        self.change_home_snapshot()

        result = self.run_manager(["status", "owner"])

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["users"]["owner"]["needs_refresh"])
        self.assertNotIn("password", result.stdout.lower())
        self.assertEqual(
            payload["users"]["owner"]["release_id"],
            "op-owner",
        )

    def test_status_for_all_users_reports_release_and_variant_metadata(self):
        self.publish_release_direct(user_id="owner", operation_id="owner-a")
        self.publish_release_direct(user_id="friend", operation_id="friend-a")

        result = self.run_manager(["status"])

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload["users"]), {"friend", "owner"})
        self.assertEqual(payload["users"]["friend"]["variants"], ["balanced"])
        self.assertEqual(payload["users"]["friend"]["release_id"], "friend-a")
        self.assertFalse(payload["users"]["friend"]["needs_refresh"])
        self.assertIn("traffic", payload["users"]["friend"])

    def test_history_and_rollback_use_sanitized_release_metadata(self):
        self.publish_release_direct(user_id="owner", operation_id="owner-a")
        self.clock_value = self.clock_value.replace(hour=13)
        self.publish_release_direct(user_id="owner", operation_id="owner-b")

        history_result = self.run_manager(["history", "owner"])

        self.assertEqual(history_result.returncode, 0, history_result.stderr)
        history_payload = json.loads(history_result.stdout)
        self.assertEqual(
            [entry["release_id"] for entry in history_payload["releases"]],
            ["owner-b", "owner-a"],
        )
        self.assertNotIn("password", history_result.stdout.lower())

        rollback_result = self.run_manager(["rollback", "owner", "owner-a"])

        self.assertEqual(rollback_result.returncode, 0, rollback_result.stderr)
        rollback_payload = json.loads(rollback_result.stdout)
        self.assertEqual(rollback_payload["release_id"], "owner-a")
        current_link = (self.private_root / "current" / "owner").resolve()
        self.assertEqual(current_link.name, "owner-a")

    def test_logs_limit_returns_safe_fields_only(self):
        self.run_manager(["build", "--operation-id", "op1", "--user", "friend"])
        self.run_manager(["publish", "--operation-id", "op1", "--user", "friend"])
        self.run_manager(["rotate-token", "friend"])

        result = self.run_manager(["logs", "--limit", "1"])

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(len(payload["entries"]), 1)
        self.assertEqual(payload["entries"][0]["operation"], "rotate-token")
        self.assertEqual(
            set(payload["entries"][0]),
            {"timestamp", "operation", "user_id", "release_id", "status"},
        )

    def test_rotate_token_persists_only_hash_and_returns_urls_once(self):
        result = self.run_manager(["rotate-token", "friend"])

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(set(payload["urls"]), {"balanced"})
        users_text = self.users_path.read_text(encoding="utf-8")
        self.assertNotIn(payload["token"], users_text)
        self.assertEqual(
            load_settings(self.service_path, self.users_path)
            .users["friend"]
            .token_sha256,
            hash_token(payload["token"]),
        )

    def test_settings_error_uses_stable_code(self):
        self.users_path.chmod(0o644)

        result = self.run_manager(["list-users"])

        self.assert_error_code(result, "settings_invalid")


if __name__ == "__main__":
    unittest.main()
