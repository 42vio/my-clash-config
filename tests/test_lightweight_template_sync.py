"""Regression tests for the local ``clash-sub template-sync`` command.

Every workbench document below is synthetic: nodes use RFC 5737 addresses,
example domains, and repeated-digit UUIDs so this tracked test file never
depends on or reproduces real credentials.  The private scope fixture is a
synthetic six-field home overlay and the workbench is its genuine composed
owner balanced profile, so the split logic runs against real generator
output.
"""

import copy
import os
import shutil
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from clash_sub.domain import AirportProvider, HomeOverlay
from clash_sub.generator import render_user_bundle
from clash_sub.sources import dump_home_overlay, load_home_overlay
from clash_sub.template_sync import (
    TEMPLATE_OUTPUT_PATHS,
    TemplateSyncError,
    run_template_sync,
)


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "templates"

XUI_PROXY = {
    "name": "Synthetic 3x-ui",
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
HOME_PROXY = {
    "name": "Home",
    "type": "vless",
    "server": "203.0.113.30",
    "port": 443,
    "uuid": "22222222-2222-4222-8222-222222222222",
    "network": "tcp",
    "tls": True,
    "flow": "xtls-rprx-vision",
    "servername": "home.example.com",
    "client-fingerprint": "chrome",
    "reality-opts": {
        "public-key": "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
        "short-id": "fedcba9876543210",
    },
}
AIRPORT_PROXY = {
    "name": "Synthetic Airport",
    "type": "ss",
    "server": "203.0.113.20",
    "port": 8388,
    "cipher": "aes-256-gcm",
    "password": "synthetic-airport-password-0123456789",
}

PROBE_PROVIDER = AirportProvider(
    "https://sub.example.invalid/s/probe-token/AmyTelecom.yaml", "6" * 64
)

HOME_RULE = "IP-CIDR,192.168.2.0/24,HomeServer,no-resolve"


PUBLIC_TEMPLATE_FILES = (
    "templates/clash.yaml",
    "templates/variants/manifest.yaml",
)
HOME_SCOPE_PATH = "private/home.yaml"


def make_repo(directory):
    """Copy the tracked template layout and the scanner into a scratch repo."""
    root = Path(directory)
    shutil.copytree(TEMPLATE_ROOT, root / "templates")
    (root / "scripts").mkdir(parents=True)
    shutil.copy(
        ROOT / "scripts" / "scan_tracked_secrets.py",
        root / "scripts" / "scan_tracked_secrets.py",
    )
    (root / "private" / "workbench").mkdir(parents=True)
    return root


def home_scope_document():
    """A synthetic six-field private home overlay holding only fake values."""
    return {
        "proxies": [dict(HOME_PROXY)],
        "proxy-groups": [
            {
                "type": "select",
                "name": "ProxyServer",
                "proxies": ["🎯 Direct", "HomeServer"],
            },
            {"type": "select", "name": "HomeServer", "proxies": ["🎯 Direct"]},
        ],
        "extend-proxy-groups": {
            "BiliBili": ["ProxyServer"],
            "国内流媒体": ["ProxyServer"],
        },
        "inject-node-groups": ["ProxyServer"],
        "inject-home-node-groups": ["HomeServer"],
        "rules": [HOME_RULE],
    }


def home_overlay(document=None):
    """Build one HomeOverlay value from a (possibly mutated) scope document."""
    base = document or home_scope_document()
    return HomeOverlay(
        proxies=tuple(copy.deepcopy(base["proxies"])),
        proxy_groups=tuple(copy.deepcopy(base["proxy-groups"])),
        extend_proxy_groups={
            name: list(members) for name, members in base["extend-proxy-groups"].items()
        },
        inject_node_groups=list(base["inject-node-groups"]),
        inject_home_node_groups=list(base["inject-home-node-groups"]),
        rules=list(base["rules"]),
    )


def make_workbench_document(transform=None, overlay=None):
    """Build a synthetic composed owner balanced workbench from the templates."""
    if overlay is None:
        overlay = home_overlay()
    bundle = render_user_bundle(
        True, [dict(XUI_PROXY)], PROBE_PROVIDER, overlay, TEMPLATE_ROOT
    )
    document = yaml.safe_load(bundle["balanced"])
    if transform is not None:
        transform(document)
    return document


def write_scope(root, overlay=None, *, mode=0o600):
    path = root / "private" / "home.yaml"
    path.write_bytes(dump_home_overlay(overlay if overlay is not None else home_overlay()))
    path.chmod(mode)
    return path


def write_workbench(root, document, *, mode=0o600, name="balanced.yaml"):
    path = root / "private" / "workbench" / name
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    path.chmod(mode)
    return path


def snapshot_files(root, relatives):
    return {
        relative: (root / relative).read_bytes()
        for relative in relatives
        if (root / relative).exists()
    }


def snapshot_outputs(root, relatives=TEMPLATE_OUTPUT_PATHS):
    """Snapshot both the bytes and the mode of every output target."""
    return {
        relative: (
            (root / relative).read_bytes(),
            stat.S_IMODE((root / relative).stat().st_mode),
        )
        for relative in relatives
        if (root / relative).exists()
    }


class TemplateSyncSafetyMatrixTests(unittest.TestCase):
    """Every rejection happens before any output byte is touched."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = make_repo(self.directory.name)
        write_scope(self.root)
        self.before = snapshot_files(self.root, TEMPLATE_OUTPUT_PATHS)
        self.workbench_path = self.root / "private" / "workbench" / "balanced.yaml"

    def _assert_rejected(self, code="template_source_invalid"):
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(snapshot_files(self.root, TEMPLATE_OUTPUT_PATHS), self.before)

    def _valid_workbench(self):
        write_workbench(self.root, make_workbench_document())

    def test_missing_workbench_is_rejected(self):
        self._assert_rejected()

    def test_symlink_workbench_is_rejected(self):
        self._valid_workbench()
        target = self.root / "private" / "workbench" / "balanced.yaml"
        hidden = self.root / "private" / "workbench" / "real.yaml"
        hidden.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        target.unlink()
        target.symlink_to(hidden)
        self._assert_rejected()

    def test_dangling_symlink_workbench_is_rejected(self):
        self.workbench_path.symlink_to(self.root / "private" / "workbench" / "gone.yaml")
        self._assert_rejected()

    def test_hardlinked_workbench_is_rejected(self):
        self._valid_workbench()
        target = self.root / "private" / "workbench" / "balanced.yaml"
        os.link(target, self.root / "private" / "workbench" / "hard-copy.yaml")
        self._assert_rejected()

    def test_wrong_mode_workbench_is_rejected(self):
        write_workbench(self.root, make_workbench_document(), mode=0o644)
        self._assert_rejected()

    def test_oversized_workbench_is_rejected(self):
        document = make_workbench_document()
        document["rules"] = list(document["rules"]) + [
            "DOMAIN-SUFFIX,pad-%06d.example,🎯 Direct" % index for index in range(200000)
        ]
        write_workbench(self.root, document)
        self._assert_rejected()

    def test_invalid_yaml_workbench_is_rejected(self):
        path = write_workbench(self.root, make_workbench_document())
        path.write_text("dns: [unclosed\n", encoding="utf-8")
        self._assert_rejected()

    def test_non_utf8_workbench_is_rejected(self):
        path = write_workbench(self.root, make_workbench_document())
        path.write_bytes(b"rules: [\xff\xfe\x11]\n")
        self._assert_rejected()

    def test_non_mapping_root_workbench_is_rejected(self):
        path = write_workbench(self.root, make_workbench_document())
        path.write_text("- just\n- a\n- list\n", encoding="utf-8")
        self._assert_rejected()

    def test_workbench_with_jinja_markers_is_rejected(self):
        document = make_workbench_document()
        document["rules"] = list(document["rules"]) + ["DOMAIN,{{ SECRET }},DIRECT"]
        write_workbench(self.root, document)
        self._assert_rejected()

    def test_workbench_with_generator_control_fields_is_rejected(self):
        document = make_workbench_document()
        document["_generator"] = {"inject-node-groups": ["加速线路"]}
        write_workbench(self.root, document)
        self._assert_rejected()

    def test_workbench_with_duplicate_proxy_names_is_rejected(self):
        document = make_workbench_document()
        document["proxies"] = [dict(XUI_PROXY), dict(XUI_PROXY)]
        write_workbench(self.root, document)
        self._assert_rejected()

    def test_workbench_with_unresolvable_group_target_is_rejected(self):
        document = make_workbench_document()
        document["proxy-groups"][0]["proxies"] = ["不存在的节点或组"]
        write_workbench(self.root, document)
        self._assert_rejected()

    def test_missing_scope_is_rejected_without_touching_outputs(self):
        write_workbench(self.root, make_workbench_document())
        before = snapshot_outputs(self.root, PUBLIC_TEMPLATE_FILES)
        (self.root / "private" / "home.yaml").unlink()

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)

        self.assertEqual(str(caught.exception), "home_source_invalid")
        self.assertEqual(snapshot_outputs(self.root, PUBLIC_TEMPLATE_FILES), before)
        self.assertFalse((self.root / "private" / "home.yaml").exists())

    def test_insecure_scope_mode_is_rejected_without_touching_outputs(self):
        write_workbench(self.root, make_workbench_document())
        write_scope(self.root, mode=0o644)
        before = snapshot_outputs(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)

        self.assertEqual(str(caught.exception), "home_source_invalid")
        self.assertEqual(snapshot_outputs(self.root, PUBLIC_TEMPLATE_FILES), before)
        self.assertEqual(
            stat.S_IMODE((self.root / "private" / "home.yaml").stat().st_mode), 0o644
        )

    def test_symlink_scope_is_rejected_without_touching_outputs(self):
        write_workbench(self.root, make_workbench_document())
        scope = write_scope(self.root)
        hidden = self.root / "private" / "home-real.yaml"
        hidden.write_bytes(scope.read_bytes())
        scope.unlink()
        scope.symlink_to(hidden)
        before = snapshot_outputs(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)

        self.assertEqual(str(caught.exception), "home_source_invalid")
        self.assertEqual(snapshot_outputs(self.root, PUBLIC_TEMPLATE_FILES), before)
        self.assertTrue(scope.is_symlink())

    def test_scope_declaring_a_missing_workbench_group_is_rejected(self):
        document = home_scope_document()
        document["proxy-groups"].append(
            {"type": "select", "name": "幽灵组", "proxies": ["🎯 Direct"]}
        )
        write_scope(self.root, home_overlay(document))
        write_workbench(self.root, make_workbench_document())
        before = snapshot_outputs(self.root)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)

        self.assertEqual(str(caught.exception), "template_source_invalid")
        self.assertEqual(snapshot_outputs(self.root), before)

    def test_error_never_echoes_private_values(self):
        document = make_workbench_document()
        document["dns"]["nameserver"] = ["https://203.0.113.10/dns-query"]
        write_workbench(self.root, document)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)

        message = str(caught.exception)
        for secret in ("203.0.113.10", "11111111", "Synthetic", "password"):
            self.assertNotIn(secret, message)


class TemplateSyncRoundTripTests(unittest.TestCase):
    """A composed workbench re-syncs to the shipped templates and the scope."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = make_repo(self.directory.name)
        write_scope(self.root)

    def test_round_trip_reproduces_the_shipped_templates_and_scope(self):
        write_workbench(self.root, make_workbench_document())

        changed = run_template_sync(self.root)

        self.assertEqual(
            changed,
            {
                "changed": TEMPLATE_OUTPUT_PATHS,
            },
        )
        for relative in PUBLIC_TEMPLATE_FILES:
            produced = yaml.safe_load((self.root / relative).read_text(encoding="utf-8"))
            shipped = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(produced, shipped, relative)
        self.assertEqual(
            (self.root / "private" / "home.yaml").read_bytes(),
            dump_home_overlay(home_overlay()),
        )

    def test_round_trip_output_is_byte_stable_across_runs(self):
        write_workbench(self.root, make_workbench_document())
        run_template_sync(self.root)
        first = snapshot_files(self.root, TEMPLATE_OUTPUT_PATHS)

        run_template_sync(self.root)
        second = snapshot_files(self.root, TEMPLATE_OUTPUT_PATHS)

        self.assertEqual(first, second)

    def test_sync_succeeds_with_an_empty_environment(self):
        write_workbench(self.root, make_workbench_document())

        with patch.dict(os.environ, {}, clear=True):
            result = run_template_sync(self.root)

        self.assertEqual(result, {"changed": TEMPLATE_OUTPUT_PATHS})

    def test_success_reports_three_paths_with_split_file_modes(self):
        write_workbench(self.root, make_workbench_document())

        result = run_template_sync(self.root)

        self.assertEqual(
            result["changed"],
            (
                "templates/clash.yaml",
                "templates/variants/manifest.yaml",
                "private/home.yaml",
            ),
        )
        for relative in PUBLIC_TEMPLATE_FILES:
            self.assertEqual(
                stat.S_IMODE((self.root / relative).stat().st_mode), 0o644, relative
            )
        self.assertEqual(
            stat.S_IMODE((self.root / "private" / "home.yaml").stat().st_mode), 0o600
        )

    def test_synced_outputs_still_render_all_authorized_bundles(self):
        write_workbench(self.root, make_workbench_document())
        run_template_sync(self.root)

        owner = render_user_bundle(
            True,
            [dict(XUI_PROXY)],
            PROBE_PROVIDER,
            home_overlay(),
            self.root / "templates",
        )
        member = render_user_bundle(False, [dict(XUI_PROXY)], None, None, self.root / "templates")

        self.assertEqual(tuple(owner), ("balanced", "standard", "privacy"))
        self.assertEqual(tuple(member), ("standard",))
        balanced_groups = {
            group["name"] for group in yaml.safe_load(owner["balanced"])["proxy-groups"]
        }
        self.assertIn("HomeServer", balanced_groups)
        self.assertIn("ProxyServer", balanced_groups)
        member_groups = {
            group["name"] for group in yaml.safe_load(member["standard"])["proxy-groups"]
        }
        self.assertNotIn("HomeServer", member_groups)
        self.assertNotIn("ProxyServer", member_groups)


class TemplateSyncHomeExtractionTests(unittest.TestCase):
    """The private scope decides what is exported as the private overlay."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = make_repo(self.directory.name)
        write_scope(self.root)
        write_workbench(self.root, make_workbench_document())

    def _home(self):
        return load_home_overlay(self.root / "private" / "home.yaml", 5 * 1024 * 1024)

    def test_template_sync_exports_home_without_xui_or_provider_members(self):
        result = run_template_sync(self.root)
        home = load_home_overlay(self.root / "private" / "home.yaml", 5 * 1024 * 1024)

        self.assertEqual(result["changed"], TEMPLATE_OUTPUT_PATHS)
        self.assertEqual([item["name"] for item in home.proxies], ["Home"])
        proxy_server = next(item for item in home.proxy_groups if item["name"] == "ProxyServer")
        self.assertEqual(proxy_server["proxies"], ["🎯 Direct", "HomeServer"])
        self.assertNotIn("use", proxy_server)

    def test_home_group_names_come_only_from_the_scope_declaration(self):
        run_template_sync(self.root)

        public_text = (self.root / "templates" / "clash.yaml").read_text(encoding="utf-8")
        for name in ("ProxyServer", "HomeServer"):
            self.assertNotIn(name, public_text)
        home = self._home()
        self.assertEqual(
            [group["name"] for group in home.proxy_groups],
            ["ProxyServer", "HomeServer"],
        )

    def test_copied_home_groups_have_runtime_members_and_use_removed(self):
        run_template_sync(self.root)

        home_server = next(
            item for item in self._home().proxy_groups if item["name"] == "HomeServer"
        )
        self.assertEqual(home_server["proxies"], ["🎯 Direct"])
        self.assertNotIn("use", home_server)

    def test_exactly_two_extensions_and_both_injection_lists_are_exported(self):
        run_template_sync(self.root)

        home = self._home()
        self.assertEqual(
            dict(home.extend_proxy_groups),
            {"BiliBili": ("ProxyServer",), "国内流媒体": ("ProxyServer",)},
        )
        self.assertEqual(tuple(home.inject_node_groups), ("ProxyServer",))
        self.assertEqual(tuple(home.inject_home_node_groups), ("HomeServer",))

    def test_home_rule_moves_to_private_and_precedes_public_rules(self):
        run_template_sync(self.root)

        home = self._home()
        self.assertEqual(tuple(home.rules), (HOME_RULE,))
        public = yaml.safe_load(
            (self.root / "templates" / "clash.yaml").read_text(encoding="utf-8")
        )
        self.assertNotIn(HOME_RULE, public["rules"])
        bundle = render_user_bundle(
            True, [dict(XUI_PROXY)], PROBE_PROVIDER, home, self.root / "templates"
        )
        rendered_rules = yaml.safe_load(bundle["balanced"])["rules"]
        self.assertEqual(rendered_rules[0], HOME_RULE)
        self.assertEqual(rendered_rules[1:], public["rules"])

    def test_undeclared_new_group_is_public_and_never_receives_home_nodes(self):
        def transform(document):
            document["proxy-groups"].append(
                {
                    "name": "全新公共组",
                    "type": "select",
                    "proxies": ["🎯 Direct", "Home"],
                }
            )

        write_workbench(self.root, make_workbench_document(transform))
        run_template_sync(self.root)

        document = yaml.safe_load(
            (self.root / "templates" / "clash.yaml").read_text(encoding="utf-8")
        )
        group = next(
            item for item in document["proxy-groups"] if item["name"] == "全新公共组"
        )
        self.assertEqual(group["proxies"], ["🎯 Direct"])
        home = self._home()
        self.assertEqual(
            [group["name"] for group in home.proxy_groups],
            ["ProxyServer", "HomeServer"],
        )
        self.assertEqual([item["name"] for item in home.proxies], ["Home"])

    def test_pt_extension_is_never_exported(self):
        def transform(document):
            group = next(
                item for item in document["proxy-groups"] if item["name"] == "PT站加速"
            )
            group["proxies"] = list(group["proxies"]) + ["ProxyServer"]

        before = snapshot_outputs(self.root)
        write_workbench(self.root, make_workbench_document(transform))

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)

        self.assertEqual(str(caught.exception), "template_candidate_invalid")
        self.assertEqual(snapshot_outputs(self.root), before)


class TemplateSyncEvolutionTests(unittest.TestCase):
    """Public changes in the workbench propagate to every variant."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = make_repo(self.directory.name)

    def _sync(self, document):
        write_scope(self.root)
        write_workbench(self.root, document)
        return run_template_sync(self.root)

    def _bundles(self):
        return {
            "owner": render_user_bundle(
                True,
                [dict(XUI_PROXY)],
                PROBE_PROVIDER,
                home_overlay(),
                self.root / "templates",
            ),
            "member": render_user_bundle(False, [dict(XUI_PROXY)], None, None, self.root / "templates"),
        }

    def test_new_public_rule_group_provider_and_dns_reach_every_output(self):
        def transform(document):
            document["dns"]["listen"] = "0.0.0.0:5353"
            document["rule-providers"]["NewProvider"] = {
                "type": "http",
                "behavior": "classical",
                "interval": 86400,
                "url": "https://cdn.jsdelivr.net/gh/example@7777777777777777777777777777777777654321/rule/New.yaml",
                "path": "./rule/New.yaml",
            }
            document["proxy-groups"].append(
                {
                    "name": "新公共组",
                    "type": "select",
                    "proxies": ["🎯 Direct", "加速线路"],
                }
            )
            document["rules"].insert(
                5, "DOMAIN-SUFFIX,newsite.example,新公共组"
            )

        self._sync(make_workbench_document(transform))
        bundles = self._bundles()

        outputs = {
            "balanced": bundles["owner"]["balanced"],
            "standard": bundles["owner"]["standard"],
            "privacy": bundles["owner"]["privacy"],
            "member": bundles["member"]["standard"],
        }
        for variant, text in outputs.items():
            document = yaml.safe_load(text)
            self.assertEqual(document["dns"]["listen"], "0.0.0.0:5353", variant)
            self.assertIn("NewProvider", document["rule-providers"], variant)
            self.assertIn("新公共组", {group["name"] for group in document["proxy-groups"]}, variant)
            self.assertIn("DOMAIN-SUFFIX,newsite.example,新公共组", document["rules"], variant)

        # The privacy DNS override still wins over the public change.
        privacy_dns = yaml.safe_load(outputs["privacy"])["dns"]
        self.assertEqual(
            privacy_dns["nameserver"],
            ["https://223.5.5.5/dns-query", "https://doh.pub/dns-query"],
        )

    def test_dynamic_node_names_are_stripped_from_the_public_template(self):
        self._sync(make_workbench_document())
        document = yaml.safe_load((self.root / "templates" / "clash.yaml").read_text(encoding="utf-8"))

        self.assertEqual(document["proxies"], [])
        names = {"Synthetic 3x-ui", "Synthetic Airport", "Home"}
        for group in document["proxy-groups"]:
            for member in group.get("proxies", []):
                self.assertNotIn(member, names)

    def test_similarly_named_static_groups_are_not_deleted(self):
        def transform(document):
            document["proxy-groups"].append(
                {
                    "name": "Synthetic 3x-ui 观测组",
                    "type": "select",
                    "proxies": ["🎯 Direct"],
                }
            )

        self._sync(make_workbench_document(transform))
        document = yaml.safe_load((self.root / "templates" / "clash.yaml").read_text(encoding="utf-8"))

        group_names = {group["name"] for group in document["proxy-groups"]}
        self.assertIn("Synthetic 3x-ui 观测组", group_names)

    def test_new_node_bearing_public_group_joins_the_global_injections(self):
        def transform(document):
            document["proxy-groups"].insert(
                3,
                {
                    "name": "新聚合组",
                    "type": "select",
                    "proxies": ["🎯 Direct", "Synthetic 3x-ui"],
                },
            )

        self._sync(make_workbench_document(transform))

        manifest = yaml.safe_load(
            (self.root / "templates" / "variants" / "manifest.yaml").read_text(encoding="utf-8")
        )
        self.assertIn("新聚合组", manifest["inject-node-groups"])
        self.assertNotIn("ProxyServer", manifest["inject-node-groups"])
        self.assertNotIn("HomeServer", manifest["inject-node-groups"])

        for text in self._bundles()["owner"].values():
            groups = {group["name"]: group for group in yaml.safe_load(text)["proxy-groups"]}
            self.assertIn("Synthetic 3x-ui", groups["新聚合组"]["proxies"])

    def test_home_groups_stay_out_of_standard_and_member_outputs(self):
        self._sync(make_workbench_document())
        bundles = self._bundles()

        for text in (bundles["owner"]["standard"], bundles["member"]["standard"]):
            document = yaml.safe_load(text)
            group_names = {group["name"] for group in document["proxy-groups"]}
            self.assertNotIn("HomeServer", group_names)
            self.assertNotIn("ProxyServer", group_names)
            for rule in document["rules"]:
                self.assertNotIn("HomeServer", rule)
            for group in document["proxy-groups"]:
                self.assertNotIn("ProxyServer", group.get("proxies", []))

    def test_synced_manifest_keeps_the_global_injection_declarations(self):
        self._sync(make_workbench_document())

        manifest = yaml.safe_load(
            (self.root / "templates" / "variants" / "manifest.yaml").read_text(encoding="utf-8")
        )
        self.assertIn("加速线路", manifest["inject-node-groups"])
        self.assertIn("AI服务", manifest["inject-node-groups"])
        self.assertNotIn("ProxyServer", manifest["inject-node-groups"])
        self.assertNotIn("HomeServer", manifest["inject-node-groups"])


class TemplateSyncFailureTests(unittest.TestCase):
    """Candidate failures leave every output untouched."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = make_repo(self.directory.name)
        write_scope(self.root)
        write_workbench(self.root, make_workbench_document())
        self.before = snapshot_outputs(self.root)

    def _assert_rejected(self, code="template_secret_leak"):
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(snapshot_outputs(self.root), self.before)

    def test_private_value_in_public_content_is_rejected_as_a_leak(self):
        def transform(document):
            document["dns"]["nameserver"] = ["https://203.0.113.10/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_credential_like_value_in_public_rules_is_rejected_as_a_leak(self):
        random_uuid = "8f14e45f" "-ceea-167a-5a36-dedd" "4bea2543"

        def transform(document):
            document["proxies"].append(
                {
                    "name": "Random Node",
                    "type": "ss",
                    "server": "203.0.113.40",
                    "port": 8388,
                    "cipher": "aes-256-gcm",
                    "password": "synthetic-random-0123456789abcdef",
                    "uuid": random_uuid,
                }
            )
            document["rules"].append("DOMAIN,%s.example,🎯 Direct" % random_uuid)

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_structurally_invalid_candidate_keeps_templates_untouched(self):
        def transform(document):
            # The workbench itself stays resolvable (the rule targets a real
            # proxy), but stripping the dynamic node leaves the public rules
            # pointing at a name that no longer exists.  Appending keeps the
            # home rules a contiguous prefix.
            document["rules"].append("DOMAIN,node-direct.example,Synthetic 3x-ui")

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected("template_candidate_invalid")

    def test_top_level_controller_secret_is_rejected_as_a_leak(self):
        def transform(document):
            document["secret"] = "synthetic-controller-password-0123456789"

        write_workbench(self.root, make_workbench_document(transform))
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)
        self.assertNotIn("synthetic-controller-password", str(caught.exception))
        self.assertEqual(snapshot_outputs(self.root), self.before)

    def test_top_level_authentication_entries_are_rejected_as_a_leak(self):
        def transform(document):
            document["authentication"] = ["synthetic-user:synthetic-pass-0123456789"]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_nested_credential_outside_proxies_is_rejected_as_a_leak(self):
        def transform(document):
            document["experimental"] = {"token": "synthetic-experimental-token-0123456789"}

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_proxy_servername_participates_in_the_forbidden_values(self):
        def transform(document):
            # The XUI probe's servername is www.example.com; reusing it in
            # public DNS content must trip the leak check.
            document["dns"]["nameserver"] = ["https://www.example.com/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_short_top_level_secret_value_is_rejected(self):
        def transform(document):
            document["secret"] = "abc"

        write_workbench(self.root, make_workbench_document(transform))
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)
        self.assertNotIn("abc", str(caught.exception))
        self.assertEqual(snapshot_outputs(self.root), self.before)

    def test_short_authentication_entry_is_rejected(self):
        def transform(document):
            document["authentication"] = ["u:p"]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_short_proxy_password_reused_in_public_content_is_rejected(self):
        def transform(document):
            for proxy in document["proxies"]:
                proxy["password"] = "ab"
            # A two-character node password reappears as a complete public
            # scalar (DNS nameserver entry) -- still a leak.
            document["dns"]["nameserver"] = ["ab"]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_nested_plugin_opts_host_reuse_is_rejected(self):
        def transform(document):
            document["proxies"][0]["plugin-opts"] = {"mode": "websocket", "host": "plugin-host-secret.example"}
            document["dns"]["nameserver"] = ["https://plugin-host-secret.example/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_nested_ws_opts_header_host_reuse_is_rejected(self):
        def transform(document):
            document["proxies"][0]["ws-opts"] = {"headers": {"Host": "ws-host-secret.example"}}
            document["dns"]["nameserver"] = ["https://ws-host-secret.example/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_sni_field_reuse_is_rejected(self):
        def transform(document):
            document["proxies"][0]["sni"] = "sni-secret.example"
            document["dns"]["nameserver"] = ["https://sni-secret.example/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_rule_provider_query_credential_is_rejected(self):
        def transform(document):
            document["rule-providers"]["PrivateProvider"] = {
                "type": "http",
                "behavior": "classical",
                "format": "yaml",
                "url": (
                    "https://rules.example.com/list.yaml"
                    "?token=private-provider-token-abcdef"
                ),
                "path": "./ruleset/private-provider.yaml",
                "interval": 86400,
            }

        write_workbench(self.root, make_workbench_document(transform))
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)
        self.assertNotIn("private-provider-token", str(caught.exception))
        self.assertEqual(snapshot_outputs(self.root), self.before)

    def test_rule_provider_authorization_header_is_rejected(self):
        def transform(document):
            provider = next(iter(document["rule-providers"].values()))
            provider["header"] = {"Authorization": "Bearer ab"}

        write_workbench(self.root, make_workbench_document(transform))
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)
        self.assertNotIn("Bearer", str(caught.exception))
        self.assertEqual(snapshot_outputs(self.root), self.before)

    def test_nested_proxy_value_under_structural_key_is_rejected(self):
        def transform(document):
            document["proxies"][0]["ws-opts"] = {
                "headers": {"type": "nested-secret-value"}
            }
            document["dns"]["nameserver"].append(
                "https://nested-secret-value/dns-query"
            )

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_short_private_value_used_as_mapping_key_is_rejected(self):
        def transform(document):
            for proxy in document["proxies"]:
                proxy["password"] = "ab"
            document["dns"]["nameserver-policy"]["ab"] = [
                "https://1.1.1.1/dns-query"
            ]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_proxy_name_embedded_in_public_url_is_rejected(self):
        def transform(document):
            old_name = document["proxies"][0]["name"]
            private_name = "private-node.example"
            document["proxies"][0]["name"] = private_name
            for group in document["proxy-groups"]:
                if isinstance(group.get("proxies"), list):
                    group["proxies"] = [
                        private_name if member == old_name else member
                        for member in group["proxies"]
                    ]
            document["dns"]["nameserver"].append(
                "https://private-node.example/dns-query"
            )

        write_workbench(self.root, make_workbench_document(transform))
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root)
        self.assertNotIn("private-node.example", str(caught.exception))
        self.assertEqual(snapshot_outputs(self.root), self.before)

    def test_home_group_name_leaked_into_public_content_is_rejected(self):
        def transform(document):
            # A home group name smuggled into public DNS policy content is a
            # private-name leak even though the value carries no credential.
            document["dns"]["nameserver-policy"]["HomeServer"] = [
                "https://1.1.1.1/dns-query"
            ]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_private_rule_text_leaked_into_public_content_is_rejected(self):
        def transform(document):
            # The complete private rule line smuggled into public DNS policy
            # content is a leak even though it carries no credential; the
            # split itself would always route a home-target rule privately.
            document["dns"]["nameserver-policy"][HOME_RULE] = [
                "https://1.1.1.1/dns-query"
            ]

        write_workbench(self.root, make_workbench_document(transform))
        self._assert_rejected()

    def test_snapshot_failure_returns_stable_error_without_touching_outputs(self):
        target = self.root / "templates" / "clash.yaml"
        before = snapshot_outputs(self.root)
        original_read_bytes = Path.read_bytes

        def failing_snapshot(path):
            if path == target:
                raise PermissionError("injected snapshot failure")
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", new=failing_snapshot):
            with self.assertRaises(TemplateSyncError) as caught:
                run_template_sync(self.root)

        self.assertEqual(str(caught.exception), "template_write_failed")
        self.assertEqual(snapshot_outputs(self.root), before)

    def test_replace_failure_at_each_position_restores_all_three_targets(self):
        def diverge(document):
            # Every output target must genuinely change so a missing restore
            # cannot hide behind identical bytes.
            document["dns"]["listen"] = "0.0.0.0:5353"
            document["proxy-groups"].append(
                {
                    "name": "注入组",
                    "type": "select",
                    "proxies": ["🎯 Direct", "Synthetic 3x-ui"],
                }
            )
            document["rules"].append("DOMAIN,extra-home.example,HomeServer")

        from clash_sub import template_sync

        original_replace = template_sync._os_replace
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at):
                with TemporaryDirectory() as directory:
                    root = make_repo(directory)
                    write_scope(root)
                    write_workbench(root, make_workbench_document(diverge))
                    before = snapshot_outputs(root)

                    calls = []

                    def failing_replace(source, target):
                        calls.append(target)
                        original_replace(source, target)
                        if len(calls) == fail_at:
                            raise OSError("injected failure")

                    with patch.object(template_sync, "_os_replace", side_effect=failing_replace):
                        with self.assertRaises(TemplateSyncError) as caught:
                            run_template_sync(root)

                    self.assertEqual(str(caught.exception), "template_write_failed")
                    # Restore writes go through _os_replace as well, so more
                    # calls than fail_at are expected; what matters is that
                    # every target ended up byte-identical to the start.
                    self.assertGreaterEqual(len(calls), fail_at)
                    self.assertEqual(snapshot_outputs(root), before)

    def test_restore_preserves_custom_file_modes(self):
        def diverge(document):
            document["dns"]["listen"] = "0.0.0.0:5353"

        from clash_sub import template_sync

        original_replace = template_sync._os_replace
        target = self.root / "templates" / "variants" / "manifest.yaml"
        target.chmod(0o400)
        before = snapshot_outputs(self.root)
        write_workbench(self.root, make_workbench_document(diverge))

        calls = []

        def failing_replace(source, destination):
            calls.append(destination)
            original_replace(source, destination)
            if len(calls) == 2:
                raise OSError("injected failure")

        with patch.object(template_sync, "_os_replace", side_effect=failing_replace):
            with self.assertRaises(TemplateSyncError):
                run_template_sync(self.root)

        self.assertEqual(snapshot_outputs(self.root), before)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o400)

    def test_restore_writes_back_a_zero_mode_verbatim(self):
        from clash_sub import template_sync

        with TemporaryDirectory() as directory:
            root = make_repo(directory)
            target = root / "templates" / "variants" / "manifest.yaml"
            target.parent.mkdir(parents=True, exist_ok=True)
            payload = b"mode: zero\n"
            target.write_bytes(payload)

            template_sync._restore_files(
                root, [("templates/variants/manifest.yaml", b"restored: true\n", 0)], ["templates/variants/manifest.yaml"]
            )

            # A mode-0 file cannot be read back; assert the mode first, then
            # lift it and verify the payload.
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0)
            target.chmod(0o600)
            self.assertEqual(target.read_bytes(), b"restored: true\n")

    def test_workbench_path_is_fixed_and_the_api_is_purely_local(self):
        import inspect

        from clash_sub import template_sync

        parameters = tuple(inspect.signature(run_template_sync).parameters)
        self.assertEqual(parameters, ("repo_root",))
        self.assertEqual(
            template_sync.WORKBENCH_RELATIVE_PATH,
            ("private", "workbench", "balanced.yaml"),
        )
        self.assertEqual(
            template_sync.TEMPLATE_OUTPUT_PATHS,
            ("templates/clash.yaml", "templates/variants/manifest.yaml", "private/home.yaml"),
        )
        self.assertEqual(template_sync.OUTPUT_MODES["private/home.yaml"], 0o600)
        self.assertEqual(template_sync.OUTPUT_MODES["templates/clash.yaml"], 0o644)
        source = inspect.getsource(template_sync)
        for retired in (
            "_resolve_mihomo",
            "MihomoValidator",
            "MIHOMO_BIN",
            "mihomo_validation_failed",
        ):
            self.assertNotIn(retired, source)


if __name__ == "__main__":
    unittest.main()
