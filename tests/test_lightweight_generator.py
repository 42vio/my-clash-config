import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from clash_sub.checks import validate_clash
from clash_sub.domain import AirportProvider
from clash_sub.generator import render_user_bundle


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "templates"
PROVIDER_URL = "https://sub.example.test:443/s/owner-token/AmyTelecom.yaml"


def provider(digest="1" * 64):
    return AirportProvider(PROVIDER_URL, digest)


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


def provider_mapping(text):
    return yaml.safe_load(text).get("proxy-providers", {})


def rules(text):
    return yaml.safe_load(text)["rules"]


class LightweightGeneratorTests(unittest.TestCase):
    def test_member_standard_contains_only_its_xui_proxies(self):
        bundle = render_user_bundle(
            False,
            [reality_proxy("Member 3x-ui")],
            None,
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )

        self.assertEqual(tuple(bundle), ("standard",))
        self.assertEqual(proxy_names(bundle["standard"]), ["Member 3x-ui"])
        self.assertEqual(provider_mapping(bundle["standard"]), {})
        for group in proxy_groups(bundle["standard"]).values():
            self.assertNotIn("use", group)

    def test_owner_variants_use_their_exact_authorized_source_scopes(self):
        bundle = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )

        self.assertEqual(tuple(bundle), ("balanced", "standard", "privacy"))
        self.assertEqual(set(proxy_names(bundle["balanced"])), {"Owner 3x-ui", "Home"})
        self.assertEqual(proxy_names(bundle["standard"]), ["Owner 3x-ui"])
        self.assertEqual(set(proxy_names(bundle["privacy"])), {"Owner 3x-ui", "Home"})
        for variant in bundle:
            self.assertEqual(tuple(provider_mapping(bundle[variant])), ("AmyTelecom",))

    def test_owner_emits_exactly_one_stable_amytelecom_provider(self):
        bundle = render_user_bundle(
            True, [reality_proxy("Owner")], provider(), [reality_proxy("Home")], TEMPLATE_ROOT
        )

        for variant, text in bundle.items():
            mapping = provider_mapping(text)
            self.assertEqual(
                mapping,
                {
                    "AmyTelecom": {
                        "type": "http",
                        "url": PROVIDER_URL,
                        "path": "./proxy_providers/AmyTelecom-%s.yaml" % ("1" * 64),
                        "interval": 0,
                    }
                },
                variant,
            )

    def test_changed_airport_digest_changes_the_cache_path_only(self):
        first = render_user_bundle(
            True, [reality_proxy("Owner")], provider("1" * 64), (), TEMPLATE_ROOT
        )
        second = render_user_bundle(
            True, [reality_proxy("Owner")], provider("2" * 64), (), TEMPLATE_ROOT
        )
        unchanged = render_user_bundle(
            True, [reality_proxy("Owner")], provider("1" * 64), (), TEMPLATE_ROOT
        )

        self.assertIn("AmyTelecom-%s.yaml" % ("1" * 64), first["standard"])
        self.assertIn("AmyTelecom-%s.yaml" % ("2" * 64), second["standard"])
        self.assertEqual(first, unchanged)

    def test_provider_use_reaches_declared_and_include_all_owner_groups(self):
        bundle = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )

        expected = {
            "加速线路",
            "AI服务",
            "ProxyServer",
            "自动选择",
            "🇭🇰 香港节点",
            "🇯🇵 日本节点",
            "🇺🇲 美国节点",
            "🇨🇳 台湾节点",
            "🇸🇬 新加坡节点",
        }
        for variant in ("balanced", "standard", "privacy"):
            groups = proxy_groups(bundle[variant])
            with_use = {name for name, group in groups.items() if "use" in group}
            if variant == "standard":
                self.assertEqual(with_use, expected - {"ProxyServer"})
            else:
                self.assertEqual(with_use, expected)
            for name in with_use:
                self.assertEqual(groups[name]["use"], ["AmyTelecom"], name)
        # Groups without airport reach keep their plain shape.
        self.assertNotIn("use", proxy_groups(bundle["balanced"])["HomeServer"])
        self.assertNotIn("use", proxy_groups(bundle["standard"])["游戏下载"])

    def test_owner_without_a_provider_and_member_with_one_fail_closed(self):
        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], None, (), TEMPLATE_ROOT)
        with self.assertRaises(ValueError):
            render_user_bundle(False, [reality_proxy("Member")], provider(), (), TEMPLATE_ROOT)

    def test_rendered_rule_providers_use_immutable_revisions(self):
        import re

        bundle = render_user_bundle(True, [reality_proxy("Owner")], provider(), [reality_proxy("Home")], TEMPLATE_ROOT)

        urls = [item["url"] for item in yaml.safe_load(bundle["standard"])["rule-providers"].values()]
        self.assertTrue(urls)
        jsdelivr = [url for url in urls if "jsdelivr.net/gh/" in url]
        gist = next(url for url in urls if "gist.githubusercontent.com" in url)
        self.assertTrue(all(re.search(r"@[0-9a-f]{40}/", url) for url in jsdelivr))
        self.assertRegex(gist, r"/raw/[0-9a-f]{40}/Ai\.yaml$")

    def test_owner_bundle_allows_missing_optional_home_source(self):
        bundle = render_user_bundle(True, [reality_proxy("Owner")], provider(), (), TEMPLATE_ROOT)

        self.assertEqual(tuple(bundle), ("balanced", "standard", "privacy"))
        groups = proxy_groups(bundle["balanced"])
        self.assertEqual(groups["HomeServer"]["proxies"], ["🎯 Direct"])

    def test_source_collisions_are_renamed_without_changing_authorization(self):
        bundle = render_user_bundle(
            True,
            [reality_proxy("Duplicate")],
            provider(),
            [reality_proxy("Duplicate")],
            TEMPLATE_ROOT,
        )

        self.assertEqual(
            proxy_names(bundle["standard"]),
            ["Duplicate"],
        )
        self.assertEqual(
            proxy_names(bundle["balanced"]),
            ["Duplicate [3x-ui]", "Duplicate [home]"],
        )

    def test_home_feature_only_reaches_balanced_and_privacy(self):
        bundle = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )

        for variant in ("balanced", "privacy"):
            groups = proxy_groups(bundle[variant])
            self.assertIn("HomeServer", groups)
            self.assertIn("ProxyServer", groups)
            self.assertIn("IP-CIDR,192.168.2.0/24,HomeServer,no-resolve", rules(bundle[variant]))
            self.assertEqual(groups["BiliBili"]["proxies"], ["🎯 Direct", "加速线路", "ProxyServer"])
        standard_groups = proxy_groups(bundle["standard"])
        self.assertNotIn("HomeServer", standard_groups)
        self.assertNotIn("ProxyServer", standard_groups)
        self.assertNotIn("HomeServer", standard_groups["BiliBili"]["proxies"])
        for rule in rules(bundle["standard"]):
            self.assertNotIn("HomeServer", rule)
            self.assertNotIn("ProxyServer", rule)

    def test_home_node_injection_matches_the_declared_groups(self):
        bundle = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )

        groups = proxy_groups(bundle["balanced"])
        self.assertEqual(groups["ProxyServer"]["proxies"], ["🎯 Direct", "HomeServer", "Owner 3x-ui", "Home"])
        self.assertEqual(groups["ProxyServer"]["use"], ["AmyTelecom"])
        self.assertEqual(groups["HomeServer"]["proxies"], ["🎯 Direct", "Home"])
        self.assertNotIn("use", groups["HomeServer"])
        self.assertIn("Owner 3x-ui", groups["加速线路"]["proxies"])
        self.assertEqual(groups["加速线路"]["use"], ["AmyTelecom"])

        standard_groups = proxy_groups(bundle["standard"])
        self.assertNotIn("Home", standard_groups["加速线路"]["proxies"])

    def test_standard_receives_only_global_node_injections(self):
        bundle = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )

        groups = proxy_groups(bundle["standard"])
        self.assertEqual(
            groups["加速线路"]["proxies"],
            ["自动选择", "🇭🇰 香港节点", "🇯🇵 日本节点", "🇺🇲 美国节点", "🇨🇳 台湾节点", "🇸🇬 新加坡节点", "Owner 3x-ui"],
        )
        self.assertEqual(groups["加速线路"]["use"], ["AmyTelecom"])

    def test_privacy_keeps_its_dns_override_and_inherits_the_rest(self):
        bundle = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )

        privacy_dns = yaml.safe_load(bundle["privacy"])["dns"]
        self.assertEqual(
            privacy_dns["nameserver"],
            ["https://223.5.5.5/dns-query", "https://doh.pub/dns-query"],
        )
        self.assertFalse(privacy_dns["respect-rules"])
        # Inherited public values stay present after the override.
        self.assertEqual(privacy_dns["fake-ip-range"], "198.18.0.1/16")
        self.assertEqual(
            privacy_dns["nameserver-policy"]["+.hitrontech.com"],
            ["172.28.30.211", "172.28.30.210"],
        )
        balanced_dns = yaml.safe_load(bundle["balanced"])["dns"]
        self.assertEqual(
            balanced_dns["nameserver"],
            ["https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query"],
        )
        self.assertTrue(balanced_dns["respect-rules"])

    def test_public_template_carries_empty_proxies_and_no_home_content(self):
        document = yaml.safe_load((TEMPLATE_ROOT / "clash.yaml").read_text(encoding="utf-8"))
        feature = yaml.safe_load((TEMPLATE_ROOT / "features" / "home.yaml").read_text(encoding="utf-8"))

        self.assertEqual(document["proxies"], [])
        group_names = {group["name"] for group in document["proxy-groups"]}
        self.assertNotIn("HomeServer", group_names)
        self.assertNotIn("ProxyServer", group_names)
        for rule in document["rules"]:
            self.assertNotIn("HomeServer", rule)
            self.assertNotIn("ProxyServer", rule)
        self.assertEqual(feature["inject-node-groups"], ["ProxyServer"])
        self.assertEqual(feature["inject-home-node-groups"], ["HomeServer"])

    def test_manifest_declares_the_fixed_variant_composition(self):
        manifest = yaml.safe_load((TEMPLATE_ROOT / "variants" / "manifest.yaml").read_text(encoding="utf-8"))

        self.assertEqual(
            manifest["variants"],
            {
                "balanced": {"features": ["home"], "overrides": []},
                "standard": {"features": [], "overrides": []},
                "privacy": {"features": ["home"], "overrides": ["privacy-dns"]},
            },
        )
        self.assertEqual(manifest["inject-node-groups"], ["加速线路", "AI服务"])

    def test_every_rendered_profile_validates(self):
        owner = render_user_bundle(
            True,
            [reality_proxy("Owner 3x-ui")],
            provider(),
            [reality_proxy("Home")],
            TEMPLATE_ROOT,
        )
        member = render_user_bundle(False, [reality_proxy("Member 3x-ui")], None, [], TEMPLATE_ROOT)

        for text in owner.values():
            validate_clash(text, (), allowed_provider_url=PROVIDER_URL)
        for text in member.values():
            validate_clash(text, ())

    def test_identical_inputs_produce_byte_stable_output(self):
        first = render_user_bundle(True, [reality_proxy("Owner")], provider(), [reality_proxy("Home")], TEMPLATE_ROOT)
        second = render_user_bundle(True, [reality_proxy("Owner")], provider(), [reality_proxy("Home")], TEMPLATE_ROOT)

        self.assertEqual(first, second)

    def test_rule_order_is_preserved_end_to_end(self):
        bundle = render_user_bundle(True, [reality_proxy("Owner")], provider(), [reality_proxy("Home")], TEMPLATE_ROOT)

        self.assertEqual(rules(bundle["balanced"])[0], "IP-CIDR,192.168.2.0/24,HomeServer,no-resolve")
        self.assertEqual(rules(bundle["standard"])[0], "IP-CIDR,199.19.110.145/32,🎯 Direct,no-resolve")
        self.assertEqual(rules(bundle["balanced"])[-1], "MATCH,🐟 Final")
        self.assertEqual(rules(bundle["standard"])[-1], "MATCH,🐟 Final")


class GeneratorFailClosedTests(unittest.TestCase):
    def _copy_templates(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        shutil.copytree(TEMPLATE_ROOT, root / "templates")
        return root / "templates"

    def _write_yaml(self, path, document):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")

    def test_missing_public_template_fails_closed(self):
        root = self._copy_templates()
        (root / "clash.yaml").unlink()

        with self.assertRaises(ValueError):
            render_user_bundle(False, [reality_proxy("Member")], None, [], root)

    def test_manifest_cannot_grant_home_to_standard(self):
        root = self._copy_templates()
        manifest = yaml.safe_load((root / "variants" / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["variants"]["standard"]["features"] = ["home"]
        self._write_yaml(root / "variants" / "manifest.yaml", manifest)

        with self.assertRaises(ValueError):
            render_user_bundle(False, [reality_proxy("Member")], None, [], root)

    def test_manifest_cannot_introduce_unknown_variants_or_features(self):
        root = self._copy_templates()
        manifest = yaml.safe_load((root / "variants" / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["variants"]["experimental"] = {"features": [], "overrides": []}
        self._write_yaml(root / "variants" / "manifest.yaml", manifest)

        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

        manifest = yaml.safe_load((root / "variants" / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["variants"]["balanced"]["features"] = ["nonexistent"]
        self._write_yaml(root / "variants" / "manifest.yaml", manifest)

        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

    def test_feature_cannot_duplicate_or_extend_a_missing_group(self):
        root = self._copy_templates()
        feature = yaml.safe_load((root / "features" / "home.yaml").read_text(encoding="utf-8"))
        feature["add-proxy-groups"].append({"name": "加速线路", "type": "select", "proxies": ["DIRECT"]})
        self._write_yaml(root / "features" / "home.yaml", feature)

        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

        feature = yaml.safe_load((root / "features" / "home.yaml").read_text(encoding="utf-8"))
        feature["extend-proxy-groups"]["不存在的组"] = ["ProxyServer"]
        self._write_yaml(root / "features" / "home.yaml", feature)

        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

    def test_feature_extension_rejects_duplicate_members(self):
        root = self._copy_templates()
        feature = yaml.safe_load((root / "features" / "home.yaml").read_text(encoding="utf-8"))
        feature["extend-proxy-groups"]["BiliBili"] = ["加速线路"]
        self._write_yaml(root / "features" / "home.yaml", feature)

        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

    def test_override_cannot_touch_proxies_or_injection_controls(self):
        root = self._copy_templates()
        self._write_yaml(root / "variants" / "privacy-dns.yaml", {"proxies": [reality_proxy("Evil")]})

        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

        self._write_yaml(root / "variants" / "privacy-dns.yaml", {"inject-node-groups": ["隐私广告"]})

        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

    def test_injection_declared_for_an_unknown_group_fails_closed(self):
        root = self._copy_templates()
        manifest = yaml.safe_load((root / "variants" / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["inject-node-groups"] = ["加速线路", "AI服务", "不存在的组"]
        self._write_yaml(root / "variants" / "manifest.yaml", manifest)

        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

    def test_conflicting_injection_declarations_fail_closed(self):
        root = self._copy_templates()
        feature = yaml.safe_load((root / "features" / "home.yaml").read_text(encoding="utf-8"))
        feature["inject-node-groups"] = ["加速线路"]
        self._write_yaml(root / "features" / "home.yaml", feature)

        with self.assertRaises(ValueError):
            render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

    def test_override_list_replaces_wholesale_instead_of_concatenating(self):
        root = self._copy_templates()
        self._write_yaml(
            root / "variants" / "privacy-dns.yaml",
            {
                "dns": {
                    "nameserver": ["https://223.5.5.5/dns-query"],
                }
            },
        )

        bundle = render_user_bundle(True, [reality_proxy("Owner")], provider(), (), root)

        self.assertEqual(
            yaml.safe_load(bundle["privacy"])["dns"]["nameserver"],
            ["https://223.5.5.5/dns-query"],
        )


if __name__ == "__main__":
    unittest.main()
