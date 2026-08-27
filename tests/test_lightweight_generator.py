import shutil
import re
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from jinja2 import UndefinedError

from clash_sub.checks import validate_clash

try:
    from clash_sub.generator import render_user_bundle
except ImportError:
    render_user_bundle = None


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "templates"


def reality_proxy(name):
    return {
        "name": name,
        "type": "vless",
        "server": "203.0.113.10",
        "port": 443,
        "uuid": "11111111-1111-4111-8111-111111111111",
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


def proxy_names(text):
    return [proxy["name"] for proxy in yaml.safe_load(text)["proxies"]]


def proxy_groups(text):
    return {group["name"]: group for group in yaml.safe_load(text)["proxy-groups"]}


class LightweightGeneratorTests(unittest.TestCase):
    def test_member_standard_contains_only_its_xui_proxies(self):
        self.assertIsNotNone(render_user_bundle)

        bundle = render_user_bundle(
            False,
            [reality_proxy("Member 3x-ui")],
            [reality_proxy("Airport")],
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )

        self.assertEqual(tuple(bundle), ("standard",))
        self.assertEqual(proxy_names(bundle["standard"]), ["Member 3x-ui"])

    def test_owner_variants_use_their_exact_authorized_source_scopes(self):
        self.assertIsNotNone(render_user_bundle)

        bundle = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            [reality_proxy("Airport")],
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )

        self.assertEqual(tuple(bundle), ("balanced", "standard", "privacy"))
        self.assertEqual(set(proxy_names(bundle["balanced"])), {"Owner 3x-ui", "Airport", "Home"})
        self.assertEqual(set(proxy_names(bundle["standard"])), {"Owner 3x-ui", "Airport"})
        self.assertEqual(set(proxy_names(bundle["privacy"])), {"Owner 3x-ui", "Airport", "Home"})

    def test_rendered_rule_providers_use_immutable_revisions(self):
        bundle = render_user_bundle(True, [reality_proxy("Owner")], [reality_proxy("Airport")], [reality_proxy("Home")], TEMPLATE_ROOT)

        urls = [item["url"] for item in yaml.safe_load(bundle["standard"])["rule-providers"].values()]
        self.assertTrue(urls)
        jsdelivr = [url for url in urls if "jsdelivr.net/gh/" in url]
        gist = next(url for url in urls if "gist.githubusercontent.com" in url)
        self.assertTrue(all(re.search(r"@[0-9a-f]{40}/", url) for url in jsdelivr))
        self.assertRegex(gist, r"/raw/[0-9a-f]{40}/Ai\.yaml$")

    def test_owner_bundle_allows_missing_optional_home_source(self):
        bundle = render_user_bundle(True, [reality_proxy("Owner")], [reality_proxy("Airport")], (), TEMPLATE_ROOT)

        self.assertEqual(tuple(bundle), ("balanced", "standard", "privacy"))

    def test_source_collisions_are_renamed_without_changing_authorization(self):
        self.assertIsNotNone(render_user_bundle)

        bundle = render_user_bundle(
            True,
            [reality_proxy("Duplicate")],
            [reality_proxy("Duplicate")],
            [reality_proxy("Duplicate")],
            TEMPLATE_ROOT,
        )

        self.assertEqual(
            proxy_names(bundle["standard"]),
            ["Duplicate [3x-ui]", "Duplicate [airport]"],
        )
        self.assertEqual(
            proxy_names(bundle["balanced"]),
            ["Duplicate [3x-ui]", "Duplicate [airport]", "Duplicate [home]"],
        )

    def test_unknown_base_template_marker_is_rejected_by_strict_jinja(self):
        self.assertIsNotNone(render_user_bundle)

        with TemporaryDirectory() as directory:
            template_root = Path(directory)
            shutil.copytree(TEMPLATE_ROOT / "variants", template_root / "variants")
            template = (TEMPLATE_ROOT / "clash.yaml.j2").read_text(encoding="utf-8")
            (template_root / "clash.yaml.j2").write_text(
                template.replace("{{ VARIANT_DNS_ROOT_YAML }}", "{{ UNKNOWN_VALUE }}"),
                encoding="utf-8",
            )

            with self.assertRaises(UndefinedError):
                render_user_bundle(False, [reality_proxy("Member 3x-ui")], [], [], template_root)

    def test_every_rendered_profile_validates_and_owner_groups_have_their_authorized_members(self):
        self.assertIsNotNone(render_user_bundle)
        owner = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            [reality_proxy("Airport")],
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )
        member = render_user_bundle(False, [reality_proxy("Member 3x-ui")], [], [], TEMPLATE_ROOT)

        for text in (*owner.values(), *member.values()):
            validate_clash(text, ())

        for variant in ("balanced", "privacy"):
            groups = proxy_groups(owner[variant])
            self.assertEqual(groups["HomeServer"]["proxies"], ["🎯 Direct", "Home"])
            self.assertEqual(
                groups["ProxyServer"]["proxies"],
                ["🎯 Direct", "HomeServer", "Owner 3x-ui", "Airport", "Home"],
            )
        standard_groups = proxy_groups(owner["standard"])
        self.assertNotIn("HomeServer", standard_groups)
        self.assertNotIn("ProxyServer", standard_groups)
