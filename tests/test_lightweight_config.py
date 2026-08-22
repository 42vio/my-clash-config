import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path

from clash_sub.config import ConfigError, load_config
from clash_sub.domain import (
    MEMBER_VARIANTS,
    OWNER_VARIANTS,
    VARIANTS,
    ServiceConfig,
    UserState,
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
        self.assertTrue(is_dataclass(ServiceConfig))
        user = UserState(1, "owner-example", "token", "code", True, None)

        with self.assertRaises(FrozenInstanceError):
            user.email = "other"

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


if __name__ == "__main__":
    unittest.main()
