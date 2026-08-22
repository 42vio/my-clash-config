import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
from unittest.mock import patch

from clash_sub.config import ConfigError, load_config
from clash_sub.domain import (
    MEMBER_VARIANTS,
    OWNER_VARIANTS,
    VARIANTS,
    PreparedRelease,
    RuntimeState,
    ServiceConfig,
    Traffic,
    UserState,
    XuiClient,
    XuiSnapshot,
)


CONFIG = """\
schema-version: 1
owner-email: owner-example
subscription-authority: sub.example.com:8443
xui-database: /etc/x-ui/x-ui.db
private-root: /var/lib/clash-sub/private
public-root: /var/lib/clash-sub/public
nginx-routes: /etc/nginx/clash-sub/routes.conf
mihomo-binary: /usr/local/lib/clash-sub/mihomo
nginx-binary: /usr/sbin/nginx
systemctl-binary: /usr/bin/systemctl
max-source-bytes: 5242880
"""


class LightweightConfigTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "repo"
        self.root.mkdir()
        (self.root / "templates").mkdir()
        self.path = self.root / "service.yaml"
        self.write_config()

    def tearDown(self):
        self.tempdir.cleanup()

    def write_config(self, *, replacement=None, extra="", mode=0o600):
        content = replacement if replacement is not None else CONFIG + extra
        self.path.write_text(content, encoding="utf-8")
        os.chmod(self.path, mode)

    def test_loads_minimal_service_config(self):
        config = load_config(self.path, self.root)

        self.assertEqual(config.owner_email, "owner-example")
        self.assertEqual(config.subscription_authority, "sub.example.com:8443")
        self.assertEqual(VARIANTS, ("balanced", "standard", "privacy"))
        self.assertEqual(OWNER_VARIANTS, VARIANTS)
        self.assertEqual(MEMBER_VARIANTS, ("standard",))
        self.assertEqual(config.template_root, self.root / "templates")

    def test_domain_records_are_immutable_dataclasses(self):
        records = (
            ServiceConfig,
            XuiClient,
            XuiSnapshot,
            UserState,
            RuntimeState,
            Traffic,
            PreparedRelease,
        )
        for record in records:
            self.assertTrue(is_dataclass(record))
            self.assertTrue(record.__dataclass_params__.frozen)
        user = UserState(1, "owner-example", "token", "code", True, None)

        with self.assertRaises(FrozenInstanceError):
            user.email = "other"

    def test_runtime_state_users_are_defensively_immutable(self):
        users = {1: UserState(1, "owner-example", "token", "code", True, None)}
        state = RuntimeState(1, 1, users)
        users[2] = UserState(2, "member-example", "token-2", "code-2", True, None)

        self.assertEqual(tuple(state.users), (1,))
        with self.assertRaises(TypeError):
            state.users[2] = users[2]

    def test_prepared_release_paths_are_defensively_immutable(self):
        public_paths = {"standard": Path("/public/clash-standard.yaml")}
        release = PreparedRelease(
            "2026-08-23T00-00-00Z-deadbeef",
            public_paths,
            Path("/private/manifest.json"),
        )
        public_paths["privacy"] = Path("/public/clash-privacy.yaml")

        self.assertEqual(tuple(release.public_paths), ("standard",))
        with self.assertRaises(TypeError):
            release.public_paths["privacy"] = public_paths["privacy"]

    def test_rejects_unknown_key(self):
        self.write_config(extra="publisher-port: 25501\n")

        with self.assertRaisesRegex(ConfigError, "unsupported configuration"):
            load_config(self.path, self.root)

    def test_rejects_relative_config_path(self):
        with self.assertRaisesRegex(ConfigError, "absolute"):
            load_config(Path("service.yaml"), self.root)

    def test_rejects_symlink_escaped_config_path(self):
        outside = Path(self.tempdir.name) / "outside.yaml"
        outside.write_text(CONFIG, encoding="utf-8")
        os.chmod(outside, 0o600)
        escaped = self.root / "escaped.yaml"
        escaped.symlink_to(outside)

        with self.assertRaisesRegex(ConfigError, "within repository root"):
            load_config(escaped, self.root)

    def test_rejects_relative_configured_path(self):
        self.write_config(replacement=CONFIG.replace("/etc/x-ui/x-ui.db", "x-ui.db"))

        with self.assertRaisesRegex(ConfigError, "absolute path"):
            load_config(self.path, self.root)

    def test_rejects_authority_with_url_scheme(self):
        self.write_config(
            replacement=CONFIG.replace(
                "sub.example.com:8443", "https://sub.example.com:8443"
            )
        )

        with self.assertRaisesRegex(ConfigError, "subscription authority"):
            load_config(self.path, self.root)

    def test_rejects_authority_without_port_8443(self):
        self.write_config(replacement=CONFIG.replace("sub.example.com:8443", "sub.example.com"))

        with self.assertRaisesRegex(ConfigError, "8443"):
            load_config(self.path, self.root)

    def test_rejects_empty_owner_email(self):
        self.write_config(replacement=CONFIG.replace("owner-example", ""))

        with self.assertRaisesRegex(ConfigError, "owner email"):
            load_config(self.path, self.root)

    def test_rejects_non_private_config_mode(self):
        self.write_config(mode=0o640)

        with self.assertRaisesRegex(ConfigError, "0600"):
            load_config(self.path, self.root)

    @patch("os.geteuid", return_value=0)
    def test_root_service_rejects_unprivileged_owned_config(self, _geteuid):
        with self.assertRaisesRegex(ConfigError, "root-owned"):
            load_config(self.path, self.root)


if __name__ == "__main__":
    unittest.main()
