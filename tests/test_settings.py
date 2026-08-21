import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from clash_sub.models import SourceSpec
from clash_sub.settings import SettingsError, hash_token, load_settings, rotate_user_token


def dump_yaml(value, indent=0):
    prefix = " " * indent
    if isinstance(value, dict):
        lines = []
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                if not item:
                    lines.append(f"{prefix}{key}: {'{}' if isinstance(item, dict) else '[]'}")
                elif isinstance(item, list) and all(not isinstance(entry, (dict, list)) for entry in item):
                    lines.append(
                        f"{prefix}{key}: [{', '.join(dump_scalar(entry) for entry in item)}]"
                    )
                else:
                    lines.append(f"{prefix}{key}:")
                    lines.append(dump_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}{key}: {dump_scalar(item)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if all(not isinstance(item, (dict, list)) for item in value):
            return f"{prefix}[{', '.join(dump_scalar(item) for item in value)}]"
        lines = []
        for item in value:
            if isinstance(item, (dict, list)):
                lines.append(f"{prefix}-")
                lines.append(dump_yaml(item, indent + 2))
            else:
                lines.append(f"{prefix}- {dump_scalar(item)}")
        return "\n".join(lines)
    return f"{prefix}{dump_scalar(value)}"


def dump_scalar(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return str(value)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self._tempdir = TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name)
        self.private_root = self.root / "private"
        self.private_root.mkdir(mode=0o700)
        (self.private_root / "config").mkdir(mode=0o700)
        (self.private_root / "sources" / "owner").mkdir(parents=True, mode=0o700)
        (self.private_root / "sources" / "owner" / "airport.yaml").write_text(
            "proxies: []\n",
            encoding="utf-8",
        )
        (self.private_root / "sources" / "owner" / "home.yaml").write_text(
            "proxies: []\n",
            encoding="utf-8",
        )
        (self.private_root / "sources" / "owner" / "airport.yaml").chmod(0o600)
        (self.private_root / "sources" / "owner" / "home.yaml").chmod(0o600)

    def write_settings(
        self,
        *,
        service=None,
        users=None,
        service_mode=0o600,
        users_mode=0o600,
    ):
        service_data = self.valid_service()
        if service is not None:
            service_data = service
        users_data = {"schema-version": 1, "users": self.valid_users()}
        if users is not None:
            users_data["users"] = users

        service_path = self.private_root / "config" / "service.yaml"
        users_path = self.private_root / "config" / "users.yaml"
        service_path.write_text(dump_yaml(service_data) + "\n", encoding="utf-8")
        users_path.write_text(dump_yaml(users_data) + "\n", encoding="utf-8")
        service_path.chmod(service_mode)
        users_path.chmod(users_mode)
        return service_path, users_path

    def valid_service(self):
        return {
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
                "public-address": "198.51.100.10",
                "public-port": 443,
                "required-flow": "xtls-rprx-vision",
            },
            "xui": {
                "panel-listen": "127.0.0.1",
                "panel-port": 2053,
                "panel-base-path": "/example-random-panel-path/",
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

    def valid_users(self):
        return {
            "owner": {
                "role": "owner",
                "token-sha256": "a" * 64,
                "variants": ["balanced", "balanced-win", "privacy"],
                "xui-subscription-url": "http://127.0.0.1:2096/sub/example-owner-sub-id",
                "local-sources": {
                    "airport": "sources/owner/airport.yaml",
                    "home": "sources/owner/home.yaml",
                },
            },
            "friend": {
                "role": "member",
                "token-sha256": "b" * 64,
                "variants": ["balanced"],
                "xui-subscription-url": "http://127.0.0.1:2096/sub/example-friend-sub-id",
                "local-sources": {},
            },
        }

    def test_load_settings_returns_immutable_models_for_domain_publication(self):
        service_path, users_path = self.write_settings()

        settings = load_settings(service_path, users_path)

        self.assertEqual(settings.service.publication.mode, "domain")
        self.assertTrue(settings.users["owner"].is_owner)
        self.assertIsInstance(settings.users["owner"].xui_source, SourceSpec)
        self.assertEqual(
            settings.users["owner"].local_sources[0].path,
            (self.private_root / "sources" / "owner" / "airport.yaml").resolve(),
        )
        self.assertEqual(settings.users["friend"].variants, ("balanced",))

    def test_load_settings_accepts_ip_publication_mode(self):
        service = self.valid_service()
        service["publication"] = {
            "mode": "ip",
            "subscription-authority": "198.51.100.10:8443",
            "panel-authority": "198.51.100.10:8443",
            "publisher-listen": "127.0.0.1",
            "publisher-port": 25501,
        }
        service_path, users_path = self.write_settings(service=service)

        settings = load_settings(service_path, users_path)

        self.assertEqual(settings.service.publication.subscription_authority, "198.51.100.10:8443")

    def test_member_cannot_declare_owner_local_sources(self):
        service_path, users_path = self.write_settings(
            users={
                "friend": {
                    "role": "member",
                    "token-sha256": "a" * 64,
                    "variants": ["balanced"],
                    "xui-subscription-url": "http://127.0.0.1:2096/sub/friend",
                    "local-sources": {"home": "sources/owner/home.yaml"},
                }
            }
        )

        with self.assertRaisesRegex(SettingsError, "friend.*local-sources"):
            load_settings(service_path, users_path)

    def test_remote_xui_url_must_be_loopback_http(self):
        service_path, users_path = self.write_settings(
            users={
                "friend": {
                    "role": "member",
                    "token-sha256": "a" * 64,
                    "variants": ["balanced"],
                    "xui-subscription-url": "http://192.0.2.20:2096/sub/friend",
                    "local-sources": {},
                }
            }
        )

        with self.assertRaisesRegex(SettingsError, "loopback"):
            load_settings(service_path, users_path)

    def test_converter_base_url_must_be_loopback_http(self):
        service = self.valid_service()
        service["converter-base-url"] = "https://converter.example.com/sub"
        service_path, users_path = self.write_settings(service=service)

        with self.assertRaisesRegex(SettingsError, "converter-base-url.*loopback"):
            load_settings(service_path, users_path)

    def test_service_unknown_key_is_rejected(self):
        service = self.valid_service()
        service["unexpected"] = True
        service_path, users_path = self.write_settings(service=service)

        with self.assertRaisesRegex(SettingsError, "unexpected"):
            load_settings(service_path, users_path)

    def test_user_unknown_key_is_rejected(self):
        users = self.valid_users()
        users["owner"]["unexpected"] = True
        service_path, users_path = self.write_settings(users=users)

        with self.assertRaisesRegex(SettingsError, "unexpected"):
            load_settings(service_path, users_path)

    def test_malformed_yaml_raises_settings_error(self):
        service_path, users_path = self.write_settings()
        users_path.write_text("users: [\n", encoding="utf-8")

        with self.assertRaisesRegex(SettingsError, "users"):
            load_settings(service_path, users_path)

    def test_public_authorities_require_no_scheme_and_port_8443(self):
        service = self.valid_service()
        service["publication"]["subscription-authority"] = "https://sub.example.com:8443"
        service_path, users_path = self.write_settings(service=service)

        with self.assertRaisesRegex(SettingsError, "subscription-authority"):
            load_settings(service_path, users_path)

    def test_invalid_public_addresses_are_rejected(self):
        service = self.valid_service()
        service["reality"]["public-address"] = "not-an-ip"
        service_path, users_path = self.write_settings(service=service)

        with self.assertRaisesRegex(SettingsError, "public-address"):
            load_settings(service_path, users_path)

    def test_non_loopback_xui_listener_is_rejected(self):
        service = self.valid_service()
        service["xui"]["panel-listen"] = "192.0.2.30"
        service_path, users_path = self.write_settings(service=service)

        with self.assertRaisesRegex(SettingsError, "panel-listen.*loopback"):
            load_settings(service_path, users_path)

    def test_publisher_listen_must_be_ipv4_loopback(self):
        service = self.valid_service()
        service["publication"]["publisher-listen"] = "::1"
        service_path, users_path = self.write_settings(service=service)

        with self.assertRaisesRegex(SettingsError, "publisher-listen.*127.0.0.1"):
            load_settings(service_path, users_path)

    def test_port_conflicts_are_rejected(self):
        service = self.valid_service()
        service["publication"]["publisher-port"] = 2053
        service_path, users_path = self.write_settings(service=service)

        with self.assertRaisesRegex(SettingsError, "port conflict"):
            load_settings(service_path, users_path)

    def test_unknown_and_duplicate_variants_are_rejected(self):
        users = self.valid_users()
        users["friend"]["variants"] = ["balanced", "balanced", "ghost"]
        service_path, users_path = self.write_settings(users=users)

        with self.assertRaisesRegex(SettingsError, "friend.*variant"):
            load_settings(service_path, users_path)

    def test_duplicate_owners_are_rejected(self):
        users = self.valid_users()
        users["friend"]["role"] = "owner"
        service_path, users_path = self.write_settings(users=users)

        with self.assertRaisesRegex(SettingsError, "owner"):
            load_settings(service_path, users_path)

    def test_token_hash_must_be_lowercase_hex(self):
        users = self.valid_users()
        users["friend"]["token-sha256"] = "ABC" * 21 + "D"
        service_path, users_path = self.write_settings(users=users)

        with self.assertRaisesRegex(SettingsError, "token-sha256"):
            load_settings(service_path, users_path)

    def test_local_source_path_must_stay_under_private_root(self):
        users = self.valid_users()
        users["owner"]["local-sources"]["home"] = "../escape.yaml"
        service_path, users_path = self.write_settings(users=users)

        with self.assertRaisesRegex(SettingsError, "outside.*private-root"):
            load_settings(service_path, users_path)

    def test_world_readable_private_settings_file_is_rejected(self):
        service_path, users_path = self.write_settings(users_mode=0o644)

        with self.assertRaisesRegex(SettingsError, "permissions"):
            load_settings(service_path, users_path)

    def test_hash_token_is_stable_without_storing_plaintext(self):
        self.assertEqual(
            hash_token("sample-token"),
            "0f35d0ae14518b96bd6d3fec3ca15801fd58c9e048b1ccdea11a71378f2acdc9",
        )

    def test_rotate_user_token_updates_hash_only_and_returns_urls(self):
        service_path, users_path = self.write_settings()
        settings = load_settings(service_path, users_path)

        with patch("clash_sub.settings.secrets.token_urlsafe", return_value="rotated-token"):
            rotation = rotate_user_token(users_path, settings, "friend")

        self.assertEqual(rotation.user_id, "friend")
        self.assertEqual(rotation.token, "rotated-token")
        self.assertEqual(
            rotation.urls,
            {"balanced": "https://sub.example.com:8443/s/rotated-token/balanced.yaml"},
        )

        stored = users_path.read_text(encoding="utf-8")
        self.assertNotIn("rotated-token", stored)
        self.assertIn(hash_token("rotated-token"), stored)
        self.assertEqual(stat.S_IMODE(users_path.stat().st_mode), 0o600)
        self.assertEqual(list(users_path.parent.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
