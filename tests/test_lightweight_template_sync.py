"""Regression tests for the local ``clash-sub template-sync`` command.

Every workbench document below is synthetic: nodes use RFC 5737 addresses,
example domains, and repeated-digit UUIDs so this tracked test file never
depends on or reproduces real credentials.
"""

import os
import shutil
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from clash_sub.domain import AirportProvider
from clash_sub.generator import render_user_bundle
from clash_sub.template_sync import TemplateSyncError, run_template_sync


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
AIRPORT_PROXY = {
    "name": "Synthetic Airport",
    "type": "ss",
    "server": "203.0.113.20",
    "port": 8388,
    "cipher": "aes-256-gcm",
    "password": "synthetic-airport-password-0123456789",
}
HOME_PROXY = {
    "name": "Synthetic Home",
    "type": "vless",
    "server": "203.0.113.30",
    "port": 443,
    "uuid": "33333333-3333-4333-8333-333333333333",
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

PROBE_PROVIDER = AirportProvider(
    "https://sub.example.invalid/s/probe-token/AmyTelecom.yaml", "6" * 64
)


PUBLIC_TEMPLATE_FILES = (
    "templates/clash.yaml",
    "templates/features/home.yaml",
    "templates/variants/manifest.yaml",
)


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


def make_workbench_document(transform=None):
    """Build a synthetic full balanced workbench from the shipped templates."""
    bundle = render_user_bundle(
        True, [dict(XUI_PROXY)], PROBE_PROVIDER, [dict(HOME_PROXY)], TEMPLATE_ROOT
    )
    document = yaml.safe_load(bundle["balanced"])
    if transform is not None:
        transform(document)
    return document


def write_workbench(root, document, *, mode=0o600, name="balanced.yaml"):
    path = root / "private" / "workbench" / name
    path.write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    path.chmod(mode)
    return path


def ok_runner(arguments, **kwargs):
    return SimpleNamespace(returncode=0)


def snapshot_files(root, relatives):
    return {
        relative: (root / relative).read_bytes()
        for relative in relatives
        if (root / relative).exists()
    }


class TemplateSyncSafetyMatrixTests(unittest.TestCase):
    """Every rejection happens before any template byte is touched."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = make_repo(self.directory.name)
        self.before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)
        self.workbench_path = self.root / "private" / "workbench" / "balanced.yaml"

    def _assert_rejected(self, code="template_source_invalid"):
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self._mihomo(), runner=ok_runner)
        self.assertEqual(str(caught.exception), code)
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), self.before)

    def _mihomo(self):
        binary = self.root / "mihomo"
        binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binary.chmod(0o755)
        return binary

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

    def test_error_never_echoes_private_values(self):
        document = make_workbench_document()
        document["dns"]["nameserver"] = ["https://203.0.113.10/dns-query"]
        write_workbench(self.root, document)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self._mihomo(), runner=ok_runner)

        message = str(caught.exception)
        for secret in ("203.0.113.10", "11111111", "Synthetic", "password"):
            self.assertNotIn(secret, message)


class TemplateSyncRoundTripTests(unittest.TestCase):
    """A workbench rendered from the shipped templates re-syncs to itself."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = make_repo(self.directory.name)
        self.mihomo = self.root / "mihomo"
        self.mihomo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.mihomo.chmod(0o755)

    def test_round_trip_reproduces_the_shipped_templates(self):
        write_workbench(self.root, make_workbench_document())

        changed = run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(
            changed,
            {
                "changed": PUBLIC_TEMPLATE_FILES,
            },
        )
        for relative in PUBLIC_TEMPLATE_FILES:
            produced = yaml.safe_load((self.root / relative).read_text(encoding="utf-8"))
            shipped = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
            self.assertEqual(produced, shipped, relative)

    def test_round_trip_output_is_byte_stable_across_runs(self):
        write_workbench(self.root, make_workbench_document())
        run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)
        first = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)
        second = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        self.assertEqual(first, second)

    def test_synced_templates_still_render_all_authorized_bundles(self):
        write_workbench(self.root, make_workbench_document())
        run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        owner = render_user_bundle(
            True, [dict(XUI_PROXY)], PROBE_PROVIDER, [dict(HOME_PROXY)], self.root / "templates"
        )
        member = render_user_bundle(False, [dict(XUI_PROXY)], None, [], self.root / "templates")

        self.assertEqual(tuple(owner), ("balanced", "standard", "privacy"))
        self.assertEqual(tuple(member), ("standard",))
        member_groups = {
            group["name"] for group in yaml.safe_load(member["standard"])["proxy-groups"]
        }
        self.assertNotIn("HomeServer", member_groups)
        self.assertNotIn("ProxyServer", member_groups)


class TemplateSyncEvolutionTests(unittest.TestCase):
    """Public changes in the workbench propagate to every variant."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = make_repo(self.directory.name)
        self.mihomo = self.root / "mihomo"
        self.mihomo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.mihomo.chmod(0o755)

    def _sync(self, document):
        write_workbench(self.root, document)
        return run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

    def _bundles(self):
        return {
            "owner": render_user_bundle(
                True,
                [dict(XUI_PROXY)],
                PROBE_PROVIDER,
                [dict(HOME_PROXY)],
                self.root / "templates",
            ),
            "member": render_user_bundle(False, [dict(XUI_PROXY)], None, [], self.root / "templates"),
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
        names = {"Synthetic 3x-ui", "Synthetic Airport", "Synthetic Home"}
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
                    "proxies": ["🎯 Direct", "Synthetic 3x-ui", "Synthetic Home"],
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

    def test_home_feature_ownership_is_not_extended_by_new_groups(self):
        def transform(document):
            document["proxy-groups"].append(
                {
                    "name": "家庭专属新组",
                    "type": "select",
                    "proxies": ["HomeServer", "Synthetic Home"],
                }
            )

        self._sync(make_workbench_document(transform))
        feature = yaml.safe_load(
            (self.root / "templates" / "features" / "home.yaml").read_text(encoding="utf-8")
        )

        feature_groups = {group["name"] for group in feature["add-proxy-groups"]}
        self.assertNotIn("家庭专属新组", feature_groups)

        # It is public, so every variant (including member standard) sees it.
        member = yaml.safe_load(self._bundles()["member"]["standard"])
        self.assertIn("家庭专属新组", {group["name"] for group in member["proxy-groups"]})

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

    def test_removing_a_home_owned_group_fails_closed(self):
        def transform(document):
            # Removing the group and every reference keeps the workbench
            # self-consistent; the mismatch is then between the declared
            # home ownership and the workbench.
            document["proxy-groups"] = [
                group for group in document["proxy-groups"] if group["name"] != "ProxyServer"
            ]
            for group in document["proxy-groups"]:
                group["proxies"] = [
                    member for member in group.get("proxies", []) if member != "ProxyServer"
                ]

        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)
        write_workbench(self.root, make_workbench_document(transform))

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)
        self.assertEqual(str(caught.exception), "template_feature_invalid")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_public_rule_inserted_before_home_rules_fails_closed(self):
        def transform(document):
            document["rules"].insert(0, "DOMAIN,priority.example,🎯 Direct")

        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)
        write_workbench(self.root, make_workbench_document(transform))

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)
        self.assertEqual(str(caught.exception), "template_rule_order_invalid")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_interleaved_home_rules_fail_closed(self):
        def transform(document):
            rules = document["rules"]
            rules.insert(5, rules.pop(0))

        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)
        write_workbench(self.root, make_workbench_document(transform))

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)
        self.assertEqual(str(caught.exception), "template_rule_order_invalid")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_leading_home_rules_still_sync_and_stay_first(self):
        self._sync(make_workbench_document())

        manifest = yaml.safe_load(
            (self.root / "templates" / "variants" / "manifest.yaml").read_text(encoding="utf-8")
        )
        self.assertIn("加速线路", manifest["inject-node-groups"])

    def test_broken_home_feature_schema_fails_closed(self):
        broken_features = (
            {"add-proxy-groups": None},
            {"add-proxy-groups": [{"name": "HomeServer"}], "extend-proxy-groups": None},
            {"add-proxy-groups": [], "inject-node-groups": [7]},
            {"add-proxy-groups": [{"name": "HomeServer"}, {"name": "HomeServer"}]},
            {"add-proxy-groups": [{"type": "select"}]},
            {"prepend-rules": "IP-CIDR,192.168.2.0/24,HomeServer,no-resolve"},
            "not-a-mapping",
        )
        for broken in broken_features:
            with self.subTest(broken=broken):
                feature_path = self.root / "templates" / "features" / "home.yaml"
                original = feature_path.read_text(encoding="utf-8")
                feature_path.write_text(
                    yaml.safe_dump(broken, allow_unicode=True) if not isinstance(broken, str) else broken,
                    encoding="utf-8",
                )
                # The broken feature itself is part of the tree; "zero
                # changes" means the failing sync writes nothing on top of
                # the state it was handed.
                before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)
                write_workbench(self.root, make_workbench_document())

                with self.assertRaises(TemplateSyncError) as caught:
                    run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

                self.assertEqual(str(caught.exception), "template_feature_invalid")
                self.assertNotIn("HomeServer", str(caught.exception))
                self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)
                feature_path.write_text(original, encoding="utf-8")

    def test_unknown_home_feature_operation_fails_closed(self):
        feature_path = self.root / "templates" / "features" / "home.yaml"
        feature = yaml.safe_load(feature_path.read_text(encoding="utf-8"))
        feature["append-rules"] = ["DOMAIN,private.example,HomeServer"]
        feature_path.write_text(
            yaml.safe_dump(feature, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)
        write_workbench(self.root, make_workbench_document())

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_feature_invalid")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)


class TemplateSyncFailureTests(unittest.TestCase):
    """Candidate failures leave the tracked tree untouched."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = make_repo(self.directory.name)
        self.mihomo = self.root / "mihomo"
        self.mihomo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.mihomo.chmod(0o755)

    def test_missing_mihomo_binary_env_fails_without_touching_templates(self):
        write_workbench(self.root, make_workbench_document())
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(TemplateSyncError) as caught:
                run_template_sync(self.root, runner=ok_runner)

        self.assertEqual(str(caught.exception), "mihomo_binary_missing")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_nonexistent_mihomo_binary_path_fails_closed(self):
        write_workbench(self.root, make_workbench_document())
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(
                self.root, mihomo_binary=self.root / "missing-mihomo", runner=ok_runner
            )

        self.assertEqual(str(caught.exception), "mihomo_binary_missing")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_mihomo_rejection_keeps_templates_untouched(self):
        write_workbench(self.root, make_workbench_document())
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        def rejecting_runner(arguments, **kwargs):
            return SimpleNamespace(returncode=1)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=rejecting_runner)

        self.assertEqual(str(caught.exception), "mihomo_validation_failed")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_private_value_in_public_content_is_rejected_as_a_leak(self):
        def transform(document):
            document["dns"]["nameserver"] = ["https://203.0.113.10/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

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
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_structurally_invalid_candidate_keeps_templates_untouched(self):
        def transform(document):
            # The workbench itself stays resolvable (the rule targets a real
            # proxy), but stripping the dynamic node leaves the public rules
            # pointing at a name that no longer exists.  Appending keeps the
            # home rules a contiguous prefix.
            document["rules"].append("DOMAIN,node-direct.example,Synthetic 3x-ui")

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_candidate_invalid")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_top_level_controller_secret_is_rejected_as_a_leak(self):
        def transform(document):
            document["secret"] = "synthetic-controller-password-0123456789"

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertNotIn("synthetic-controller-password", str(caught.exception))
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_top_level_authentication_entries_are_rejected_as_a_leak(self):
        def transform(document):
            document["authentication"] = ["synthetic-user:synthetic-pass-0123456789"]

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_nested_credential_outside_proxies_is_rejected_as_a_leak(self):
        def transform(document):
            document["experimental"] = {"token": "synthetic-experimental-token-0123456789"}

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_proxy_servername_participates_in_the_forbidden_values(self):
        def transform(document):
            # The XUI probe's servername is www.example.com; reusing it in
            # public DNS content must trip the leak check.
            document["dns"]["nameserver"] = ["https://www.example.com/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_short_top_level_secret_value_is_rejected(self):
        def transform(document):
            document["secret"] = "abc"

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertNotIn("abc", str(caught.exception))
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_short_authentication_entry_is_rejected(self):
        def transform(document):
            document["authentication"] = ["u:p"]

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_short_proxy_password_reused_in_public_content_is_rejected(self):
        def transform(document):
            for proxy in document["proxies"]:
                proxy["password"] = "ab"
            # A two-character node password reappears as a complete public
            # scalar (DNS nameserver entry) -- still a leak.
            document["dns"]["nameserver"] = ["ab"]

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_nested_plugin_opts_host_reuse_is_rejected(self):
        def transform(document):
            document["proxies"][0]["plugin-opts"] = {"mode": "websocket", "host": "plugin-host-secret.example"}
            document["dns"]["nameserver"] = ["https://plugin-host-secret.example/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_nested_ws_opts_header_host_reuse_is_rejected(self):
        def transform(document):
            document["proxies"][0]["ws-opts"] = {"headers": {"Host": "ws-host-secret.example"}}
            document["dns"]["nameserver"] = ["https://ws-host-secret.example/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_sni_field_reuse_is_rejected(self):
        def transform(document):
            document["proxies"][0]["sni"] = "sni-secret.example"
            document["dns"]["nameserver"] = ["https://sni-secret.example/dns-query"]

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

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
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertNotIn("private-provider-token", str(caught.exception))
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_rule_provider_authorization_header_is_rejected(self):
        def transform(document):
            provider = next(iter(document["rule-providers"].values()))
            provider["header"] = {"Authorization": "Bearer ab"}

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertNotIn("Bearer", str(caught.exception))
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_nested_proxy_value_under_structural_key_is_rejected(self):
        def transform(document):
            document["proxies"][0]["ws-opts"] = {
                "headers": {"type": "nested-secret-value"}
            }
            document["dns"]["nameserver"].append(
                "https://nested-secret-value/dns-query"
            )

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_short_private_value_used_as_mapping_key_is_rejected(self):
        def transform(document):
            for proxy in document["proxies"]:
                proxy["password"] = "ab"
            document["dns"]["nameserver-policy"]["ab"] = [
                "https://1.1.1.1/dns-query"
            ]

        write_workbench(self.root, make_workbench_document(transform))
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

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
        before = snapshot_files(self.root, PUBLIC_TEMPLATE_FILES)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertNotIn("private-node.example", str(caught.exception))
        self.assertEqual(snapshot_files(self.root, PUBLIC_TEMPLATE_FILES), before)

    def test_snapshot_failure_returns_stable_error_without_touching_templates(self):
        target = self.root / "templates" / "clash.yaml"
        before = {
            relative: (
                (self.root / relative).read_bytes(),
                stat.S_IMODE((self.root / relative).stat().st_mode),
            )
            for relative in PUBLIC_TEMPLATE_FILES
        }
        write_workbench(self.root, make_workbench_document())
        original_read_bytes = Path.read_bytes

        def failing_snapshot(path):
            if path == target:
                raise PermissionError("injected snapshot failure")
            return original_read_bytes(path)

        with patch.object(Path, "read_bytes", new=failing_snapshot):
            with self.assertRaises(TemplateSyncError) as caught:
                run_template_sync(self.root, mihomo_binary=self.mihomo, runner=ok_runner)

        self.assertEqual(str(caught.exception), "template_write_failed")
        for relative, (payload, mode) in before.items():
            current = self.root / relative
            self.assertEqual(stat.S_IMODE(current.stat().st_mode), mode)
            self.assertEqual((self.root / relative).read_bytes(), payload)

    def test_replace_failure_at_each_position_restores_all_divergent_targets(self):
        def diverge(document):
            # Every tracked target must genuinely change so a missing
            # restore cannot hide behind identical bytes.
            document["dns"]["listen"] = "0.0.0.0:5353"
            for group in document["proxy-groups"]:
                if group["name"] == "HomeServer":
                    group["proxies"] = ["🎯 Direct", "REJECT"]
            document["proxy-groups"].append(
                {
                    "name": "注入组",
                    "type": "select",
                    "proxies": ["🎯 Direct", "Synthetic 3x-ui"],
                }
            )

        from clash_sub import template_sync

        original_replace = template_sync._os_replace
        for fail_at in (1, 2, 3):
            with self.subTest(fail_at=fail_at):
                with TemporaryDirectory() as directory:
                    root = make_repo(directory)
                    mihomo = root / "mihomo"
                    mihomo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
                    mihomo.chmod(0o755)
                    write_workbench(root, make_workbench_document(diverge))
                    before = snapshot_files(root, PUBLIC_TEMPLATE_FILES)

                    calls = []

                    def failing_replace(source, target):
                        calls.append(target)
                        original_replace(source, target)
                        if len(calls) == fail_at:
                            raise OSError("injected failure")

                    with patch.object(template_sync, "_os_replace", side_effect=failing_replace):
                        with self.assertRaises(TemplateSyncError) as caught:
                            run_template_sync(root, mihomo_binary=mihomo, runner=ok_runner)

                    self.assertEqual(str(caught.exception), "template_write_failed")
                    # Restore writes go through _os_replace as well, so more
                    # calls than fail_at are expected; what matters is that
                    # every target ended up byte-identical to the start.
                    self.assertGreaterEqual(len(calls), fail_at)
                    self.assertEqual(snapshot_files(root, PUBLIC_TEMPLATE_FILES), before)

    def test_restore_preserves_custom_file_modes(self):
        def diverge(document):
            document["dns"]["listen"] = "0.0.0.0:5353"

        from clash_sub import template_sync

        original_replace = template_sync._os_replace
        with TemporaryDirectory() as directory:
            root = make_repo(directory)
            mihomo = root / "mihomo"
            mihomo.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            mihomo.chmod(0o755)
            target = root / "templates" / "variants" / "manifest.yaml"
            target.chmod(0o400)
            before = snapshot_files(root, PUBLIC_TEMPLATE_FILES)
            write_workbench(root, make_workbench_document(diverge))

            calls = []

            def failing_replace(source, destination):
                calls.append(destination)
                original_replace(source, destination)
                if len(calls) == 3:
                    raise OSError("injected failure")

            with patch.object(template_sync, "_os_replace", side_effect=failing_replace):
                with self.assertRaises(TemplateSyncError):
                    run_template_sync(root, mihomo_binary=mihomo, runner=ok_runner)

            self.assertEqual(snapshot_files(root, PUBLIC_TEMPLATE_FILES), before)
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

    def test_workbench_path_is_fixed_and_never_a_parameter_of_the_public_api(self):
        import inspect

        from clash_sub import template_sync

        parameters = tuple(inspect.signature(run_template_sync).parameters)
        self.assertEqual(parameters, ("repo_root", "mihomo_binary", "runner"))
        self.assertEqual(
            template_sync.WORKBENCH_RELATIVE_PATH,
            ("private", "workbench", "balanced.yaml"),
        )


if __name__ == "__main__":
    unittest.main()
