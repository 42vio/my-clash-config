"""Synthetic tests for the Compat/Balance template updater.

The fixtures in this module are deliberately fake: addresses are from RFC
5737, credentials repeat one digit, and the provider uses an example domain.
No test reads an iCloud or real private file.
"""

import os
import shutil
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from clash_sub.sources import load_home_overlay
from clash_sub.template_sync import (
    HOME_SCOPE_PATH,
    OUTPUT_MODES,
    PUBLIC_TEMPLATE_FILES,
    TEMPLATE_OUTPUT_PATHS,
    TemplateSyncError,
    TemplateSyncReport,
    default_source_paths,
    initialize_home_scope,
    run_template_sync,
)
from clash_sub import template_sync


ROOT = Path(__file__).resolve().parents[1]

PROVIDER_DIGEST = "6" * 64
SHARED_UUID = "11111111-1111-4111-8111-111111111111"
HOME_UUID = "22222222-2222-4222-8222-222222222222"
SHARED_KEY = "A" * 43
HOME_KEY = "B" * 43
HOME_PASSWORD = "synthetic-home-password-9999"
PROVIDER_URL = "https://airport.example.invalid/subscription/AmyTelecom.yaml"


COMPAT_OFFICE = """# shared comment
mixed-port: 7890
allow-lan: true
mode: rule
dns:
  enable: true
  nameserver:  # compat dns comment
  - https://9.9.9.9/dns-query  # inline DNS comment
  fake-ip-filter:
  - +.example.test
proxies:
- name: Shared 3x-ui
  type: vless
  server: 192.0.2.10
  port: 443
  uuid: %s
  network: tcp
  tls: true
  flow: xtls-rprx-vision
  servername: shared.example.test
  client-fingerprint: chrome
  reality-opts:
    public-key: %s
    short-id: 0123456789abcdef
- name: Home
  type: vless
  server: 192.0.2.20
  port: 443
  uuid: %s
  network: tcp
  tls: true
  flow: xtls-rprx-vision
  servername: home.example.test
  client-fingerprint: chrome
  password: %s
  reality-opts:
    public-key: %s
    short-id: fedcba9876543210
proxy-groups:
- name: Public
  type: select
  proxies: [DIRECT, Shared 3x-ui, HomeAll]
- name: HomeAll
  type: select
  proxies: [DIRECT, Shared 3x-ui, Home]
- name: HomeOnly
  type: select
  proxies: [DIRECT, Home]
rule-providers:
  Direct:
    type: http
    behavior: classical
    interval: 86400
    url: https://rules.example.invalid/direct.yaml
    path: ./rules/direct.yaml
proxy-providers:
  AmyTelecom:
    type: http
    url: %s
    interval: 0
    path: ./proxy_providers/AmyTelecom-%s.yaml
rules:
- DOMAIN-SUFFIX,public.example.test,Public
- DOMAIN-SUFFIX,home.example.test,HomeAll,no-resolve
""" % (
    SHARED_UUID,
    SHARED_KEY,
    HOME_UUID,
    HOME_PASSWORD,
    HOME_KEY,
    PROVIDER_URL,
    PROVIDER_DIGEST,
)


COMPAT_UNIVERSAL = COMPAT_OFFICE.replace(
    "- name: Home\n  type: vless\n  server: 192.0.2.20\n  port: 443\n  uuid: %s\n  network: tcp\n  tls: true\n  flow: xtls-rprx-vision\n  servername: home.example.test\n  client-fingerprint: chrome\n  password: %s\n  reality-opts:\n    public-key: %s\n    short-id: fedcba9876543210\n"
    % (HOME_UUID, HOME_PASSWORD, HOME_KEY),
    "",
).replace(
    "- name: Public\n  type: select\n  proxies: [DIRECT, Shared 3x-ui, HomeAll]\n",
    "- name: Public\n  type: select\n  proxies: [DIRECT, Shared 3x-ui]\n",
).replace(
    "- name: HomeAll\n  type: select\n  proxies: [DIRECT, Shared 3x-ui, Home]\n- name: HomeOnly\n  type: select\n  proxies: [DIRECT, Home]\n",
    "",
).replace("- DOMAIN-SUFFIX,home.example.test,HomeAll,no-resolve\n", "")


BALANCE_OFFICE = COMPAT_OFFICE.replace(
    "# shared comment\n", "# shared comment\n# balance source comment\n", 1
).replace(
    "dns:\n  enable: true\n  nameserver:  # compat dns comment\n",
    "dns:\n  enable: true  # balance dns comment\n  nameserver:  # balance dns nameserver\n",
).replace("https://9.9.9.9/dns-query", "https://8.8.8.8/dns-query")


BASE_COMPAT = """# shared comment
mixed-port: 7890
allow-lan: true
mode: rule
dns:
  enable: true
  nameserver:  # compat dns comment
  - https://9.9.9.9/dns-query  # inline DNS comment
  fake-ip-filter:
  - +.example.test
proxies: []
proxy-groups:
- name: Public
  type: select
  proxies: [DIRECT]
rule-providers:
  Direct:
    type: http
    behavior: classical
    interval: 86400
    url: https://rules.example.invalid/direct.yaml
    path: ./rules/direct.yaml
rules:
- DOMAIN-SUFFIX,public.example.test,Public
"""


BASE_BALANCE = """# existing balance
dns:
  enable: true
  nameserver:
  - https://9.9.9.9/dns-query
  fake-ip-filter:
  - +.example.test
"""


PROFILES = """profiles:
  compat-office:
    dns: compat
    home: true
  compat-universal:
    dns: compat
    home: false
  balance-office:
    dns: balance-office
    home: true
inject-node-groups:
  []
"""


HOME_SCOPE = """# home scope comment
proxies:
- name: Home
  type: vless
  server: 192.0.2.20
  port: 443
  uuid: %s
  network: tcp
  tls: true
  flow: xtls-rprx-vision
  servername: home.example.test
  client-fingerprint: chrome
  password: %s
  reality-opts:
    public-key: %s
    short-id: fedcba9876543210
proxy-groups:
- name: HomeAll
  type: select
  proxies: [DIRECT]
- name: HomeOnly
  type: select
  proxies: [DIRECT]
extend-proxy-groups:
  Public: [HomeAll]
inject-node-groups: [HomeAll]
inject-home-node-groups: [HomeOnly]
rules:
- DOMAIN-SUFFIX,home.example.test,HomeAll,no-resolve
""" % (HOME_UUID, HOME_PASSWORD, HOME_KEY)


def _write(path, text, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mode is not None:
        path.chmod(mode)
    return path


def _write_source(path, text):
    return _write(path, text, 0o600)


def _write_scope(root, text=HOME_SCOPE):
    return _write(root / HOME_SCOPE_PATH, text, 0o600)


def _make_repo(directory, *, with_scope=True):
    root = Path(directory)
    _write(root / "templates/base/compat-office.yaml", BASE_COMPAT, 0o644)
    _write(root / "templates/dns/balance-office.yaml", BASE_BALANCE, 0o644)
    _write(root / "templates/profiles.yaml", PROFILES, 0o644)
    _write(root / "scripts/scan_tracked_secrets.py", (ROOT / "scripts/scan_tracked_secrets.py").read_text())
    if with_scope:
        _write_scope(root)
    return root


def _source_dir(directory):
    source_root = Path(directory) / "Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents"
    source_root.mkdir(parents=True)
    compat = _write_source(source_root / "Compat-Office.yaml", COMPAT_OFFICE)
    universal = _write_source(source_root / "Compat-Universal.yaml", COMPAT_UNIVERSAL)
    balance = _write_source(source_root / "Balance-Office.yaml", BALANCE_OFFICE)
    return source_root, compat, universal, balance


def _outputs(root):
    return {
        relative: (
            (root / relative).read_bytes(),
            stat.S_IMODE((root / relative).stat().st_mode),
        )
        for relative in TEMPLATE_OUTPUT_PATHS
    }


class TemplateSyncInputTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = _make_repo(self.directory.name)
        self.source_root, self.compat_path, self.universal_path, self.balance_path = _source_dir(
            self.directory.name
        )

    def test_no_paths_read_both_default_icloud_sources(self):
        compat, balance = default_source_paths(Path("/Users/tester"))
        self.assertEqual(compat.name, "Compat-Office.yaml")
        self.assertEqual(balance.name, "Balance-Office.yaml")
        self.assertIn("iCloud~com~west2online~ClashX", str(compat))

        with patch("clash_sub.template_sync.Path.home", return_value=Path(self.directory.name)):
            report = run_template_sync(self.root)
        self.assertIsInstance(report, TemplateSyncReport)
        self.assertIn("templates/base/compat-office.yaml", report.changed)
        self.assertIn("templates/dns/balance-office.yaml", report.changed)

    def test_explicit_compat_updates_only_compat_targets(self):
        before_balance = (self.root / "templates/dns/balance-office.yaml").read_bytes()
        report = run_template_sync(self.root, compat_office=self.compat_path)
        self.assertNotIn("templates/dns/balance-office.yaml", report.changed)
        self.assertEqual(
            (self.root / "templates/dns/balance-office.yaml").read_bytes(),
            before_balance,
        )
        self.assertIn("templates/base/compat-office.yaml", report.changed)

    def test_explicit_balance_updates_only_balance_target(self):
        report = run_template_sync(self.root, balance_office=self.balance_path)
        self.assertEqual(report.changed, ("templates/dns/balance-office.yaml",))

    def test_accepts_valid_yaml_flow_mapping_with_closing_braces(self):
        flow_provider = (
            "proxy-providers: {AmyTelecom: {type: http, url: %s, interval: 0, "
            "path: ./proxy_providers/AmyTelecom-%s.yaml}}\n"
        ) % (PROVIDER_URL, PROVIDER_DIGEST)
        source = COMPAT_OFFICE.replace(
            "proxy-providers:\n"
            "  AmyTelecom:\n"
            "    type: http\n"
            "    url: %s\n"
            "    interval: 0\n"
            "    path: ./proxy_providers/AmyTelecom-%s.yaml\n"
            % (PROVIDER_URL, PROVIDER_DIGEST),
            flow_provider,
        )
        path = _write_source(self.source_root / "Compat-Office-flow.yaml", source)
        report = run_template_sync(self.root, compat_office=path)
        self.assertIn(PUBLIC_TEMPLATE_FILES[0], report.changed)

    def test_accepts_source_only_local_file_provider(self):
        source_provider = (
            "proxy-providers: {SourceNodes: {type: file, "
            "path: ./proxy_providers/source.yaml}}\n"
        )
        source = COMPAT_OFFICE.replace(
            "proxy-providers:\n"
            "  AmyTelecom:\n"
            "    type: http\n"
            "    url: %s\n"
            "    interval: 0\n"
            "    path: ./proxy_providers/AmyTelecom-%s.yaml\n"
            % (PROVIDER_URL, PROVIDER_DIGEST),
            source_provider,
        )
        source = source.replace(
            "- name: Public\n  type: select\n",
            "- name: Public\n  type: select\n  use: [SourceNodes]\n",
        )
        path = _write_source(self.source_root / "Compat-Office-local.yaml", source)
        report = run_template_sync(self.root, compat_office=path)
        self.assertIn(PUBLIC_TEMPLATE_FILES[0], report.changed)

    def test_strips_provider_nodes_and_keeps_provider_only_groups_valid(self):
        source_provider = (
            "proxy-providers: {SourceNodes: {type: file, "
            "path: ./proxy_providers/source.yaml}}\n"
        )
        source = COMPAT_OFFICE.replace(
            "proxy-providers:\n"
            "  AmyTelecom:\n"
            "    type: http\n"
            "    url: %s\n"
            "    interval: 0\n"
            "    path: ./proxy_providers/AmyTelecom-%s.yaml\n"
            % (PROVIDER_URL, PROVIDER_DIGEST),
            source_provider,
        ).replace(
            "- name: Public\n  type: select\n  proxies: [DIRECT, Shared 3x-ui, HomeAll]\n",
            "- name: Public\n  type: select\n  use: [SourceNodes]\n  proxies: [DIRECT, Shared 3x-ui, HomeAll, SourceNode]\n",
        )
        path = _write_source(self.source_root / "Compat-Office-provider-node.yaml", source)
        run_template_sync(self.root, compat_office=path)
        document = yaml.safe_load((self.root / PUBLIC_TEMPLATE_FILES[0]).read_text())
        self.assertNotIn("SourceNode", (self.root / PUBLIC_TEMPLATE_FILES[0]).read_text())
        self.assertTrue(
            all(
                isinstance(group.get("proxies"), list) or group.get("include-all") is True
                for group in document["proxy-groups"]
            )
        )

    def test_rejects_unknown_member_in_group_unrelated_to_local_provider(self):
        source_provider = (
            "proxy-providers: {SourceNodes: {type: file, "
            "path: ./proxy_providers/source.yaml}}\n"
        )
        source = COMPAT_OFFICE.replace(
            "proxy-providers:\n"
            "  AmyTelecom:\n"
            "    type: http\n"
            "    url: %s\n"
            "    interval: 0\n"
            "    path: ./proxy_providers/AmyTelecom-%s.yaml\n"
            % (PROVIDER_URL, PROVIDER_DIGEST),
            source_provider,
        ).replace(
            "- name: Public\n  type: select\n  proxies: [DIRECT, Shared 3x-ui, HomeAll]\n",
            "- name: Public\n  type: select\n  proxies: [DIRECT, Shared 3x-ui, HomeAll, Synthetic Unknown]\n",
        )
        path = _write_source(self.source_root / "Compat-Office-unrelated.yaml", source)

        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, compat_office=path)

        self.assertEqual(str(caught.exception), "template_source_invalid")

    def test_candidate_validation_does_not_mutate_yaml_merge_state(self):
        source_provider = (
            "proxy-providers: {SourceNodes: {type: file, "
            "path: ./proxy_providers/source.yaml}}\n"
        )
        source = COMPAT_OFFICE.replace(
            "proxy-providers:\n"
            "  AmyTelecom:\n"
            "    type: http\n"
            "    url: %s\n"
            "    interval: 0\n"
            "    path: ./proxy_providers/AmyTelecom-%s.yaml\n"
            % (PROVIDER_URL, PROVIDER_DIGEST),
            source_provider,
        ).replace(
            "proxy-groups:\n",
            "provider-group: &provider-group\n"
            "  use: [SourceNodes]\n"
            "proxy-groups:\n",
        ).replace(
            "- name: Public\n  type: select\n",
            "- name: Public\n  type: select\n  <<: *provider-group\n",
        ).replace(
            "  proxies: [DIRECT, Shared 3x-ui, HomeAll]\n",
            "  proxies: [DIRECT, Shared 3x-ui, HomeAll, Synthetic Provider Member]\n",
            1,
        ).replace(
            "- name: HomeAll\n  type: select\n",
            "- name: HomeAll\n  type: select\n  <<: *provider-group\n",
        ).replace(
            "- name: HomeOnly\n  type: select\n",
            "- name: HomeOnly\n  type: select\n  <<: *provider-group\n",
        )
        path = _write_source(self.source_root / "Compat-Office-merge.yaml", source)

        report = run_template_sync(self.root, compat_office=path)

        self.assertIn(PUBLIC_TEMPLATE_FILES[0], report.changed)

    def test_accepts_source_rule_with_empty_policy_target(self):
        source = COMPAT_OFFICE.replace(
            "- DOMAIN-SUFFIX,home.example.test,HomeAll,no-resolve\n",
            "- DOMAIN-SUFFIX,home.example.test,HomeAll,no-resolve\n- GEOIP,ZZ,\n",
        )
        path = _write_source(self.source_root / "Compat-Office-empty-target.yaml", source)
        report = run_template_sync(self.root, compat_office=path)
        self.assertIn(PUBLIC_TEMPLATE_FILES[0], report.changed)

    def test_allows_short_proxy_address_as_part_of_public_rule_value(self):
        source = COMPAT_OFFICE.replace(
            "- DOMAIN-SUFFIX,public.example.test,Public\n",
            "- DOMAIN-SUFFIX,192.0.2.10.example.test,Public\n",
        )
        path = _write_source(self.source_root / "Compat-Office-public-address.yaml", source)
        report = run_template_sync(self.root, compat_office=path)
        self.assertIn(PUBLIC_TEMPLATE_FILES[0], report.changed)


class TemplateSyncSplitTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = _make_repo(self.directory.name)
        self.source_root, self.compat_path, self.universal_path, self.balance_path = _source_dir(
            self.directory.name
        )

    def test_split_preserves_comments_and_balance_is_dns_only(self):
        report = run_template_sync(
            self.root,
            compat_office=self.compat_path,
            balance_office=self.balance_path,
        )
        self.assertIn("templates/base/compat-office.yaml", report.changed)
        self.assertIn("# shared comment", (self.root / PUBLIC_TEMPLATE_FILES[0]).read_text())
        balance_text = (self.root / PUBLIC_TEMPLATE_FILES[1]).read_text()
        self.assertIn("# balance dns comment", balance_text)
        self.assertEqual(set(yaml.safe_load(balance_text)), {"dns"})
        self.assertEqual(
            yaml.safe_load(balance_text)["dns"],
            yaml.safe_load(self.balance_path.read_text())["dns"],
        )

    def test_balance_profile_mismatch_is_rejected_before_writes(self):
        mismatched = COMPAT_OFFICE.replace(
            "DOMAIN-SUFFIX,public.example.test,Public",
            "DOMAIN-SUFFIX,changed.example.test,Public",
        )
        mismatch_path = _write_source(self.source_root / "Balance-Office.yaml", mismatched)
        before = _outputs(self.root)
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, balance_office=mismatch_path)
        self.assertEqual(str(caught.exception), "balance_profile_mismatch")
        self.assertEqual(_outputs(self.root), before)

    def test_missing_or_insecure_home_scope_uses_existing_home_error(self):
        scope = self.root / HOME_SCOPE_PATH
        scope.unlink()
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, compat_office=self.compat_path)
        self.assertEqual(str(caught.exception), "home_source_invalid")

        _write_scope(self.root)
        scope.chmod(0o644)
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, compat_office=self.compat_path)
        self.assertEqual(str(caught.exception), "home_source_invalid")

    def test_credential_in_retained_comment_is_rejected(self):
        commented = COMPAT_OFFICE.replace(
            "# shared comment\n", "# shared comment\n# retained note: %s\n" % HOME_PASSWORD, 1
        )
        path = _write_source(self.source_root / "Compat-Office.yaml", commented)
        before = _outputs(self.root)
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, compat_office=path)
        self.assertEqual(str(caught.exception), "template_secret_leak")
        self.assertEqual(_outputs(self.root), before)

    def test_short_private_name_in_yaml_scalar_is_rejected(self):
        short_name = "Q"
        leaked = COMPAT_OFFICE.replace("Shared 3x-ui", short_name).replace(
            "  - +.example.test\n",
            "  - +.example.test\n  - +.%s.example.test\n" % short_name,
            1,
        )
        path = _write_source(self.source_root / "Compat-Office.yaml", leaked)
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, compat_office=path)
        self.assertEqual(str(caught.exception), "template_secret_leak")

    def test_private_provider_name_in_yaml_scalar_is_rejected(self):
        provider_name = "AmyTelecom"
        leaked = COMPAT_OFFICE.replace(
            "  - +.example.test\n",
            "  - +.example.test\n  - +.%s.example.test\n" % provider_name,
            1,
        )
        path = _write_source(self.source_root / "Compat-Office.yaml", leaked)
        with self.assertRaises(TemplateSyncError) as caught:
            run_template_sync(self.root, compat_office=path)
        self.assertEqual(str(caught.exception), "template_secret_leak")

    def test_short_private_name_in_comment_is_preserved(self):
        short_name = "Q"
        commented = COMPAT_OFFICE.replace("Shared 3x-ui", short_name).replace(
            "# shared comment\n",
            "# shared comment\n# %s\n" % short_name,
            1,
        )
        path = _write_source(self.source_root / "Compat-Office.yaml", commented)
        run_template_sync(self.root, compat_office=path)
        self.assertIn("# %s\n" % short_name, (self.root / PUBLIC_TEMPLATE_FILES[0]).read_text())

    def test_private_name_in_comment_is_preserved(self):
        private_name = "Shared 3x-ui"
        commented = COMPAT_OFFICE.replace(
            "# shared comment\n",
            "# shared comment\n# %s\n" % private_name,
            1,
        )
        path = _write_source(self.source_root / "Compat-Office.yaml", commented)
        run_template_sync(self.root, compat_office=path)
        self.assertIn("# %s\n" % private_name, (self.root / PUBLIC_TEMPLATE_FILES[0]).read_text())


class HomeScopeBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = _make_repo(self.directory.name, with_scope=False)
        self.source_root, self.compat_path, self.universal_path, self.balance_path = _source_dir(
            self.directory.name
        )

    def test_initialize_home_scope_derives_private_objects_and_mode(self):
        path = initialize_home_scope(self.root, self.compat_path, self.universal_path)
        self.assertEqual(path, self.root / HOME_SCOPE_PATH)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        home = load_home_overlay(path, 5 * 1024 * 1024)
        self.assertEqual([item["name"] for item in home.proxies], ["Home"])
        self.assertEqual(
            [group["name"] for group in home.proxy_groups], ["HomeAll", "HomeOnly"]
        )
        self.assertEqual(dict(home.extend_proxy_groups), {"Public": ("HomeAll",)})
        self.assertEqual(tuple(home.inject_node_groups), ("HomeAll",))
        self.assertEqual(tuple(home.inject_home_node_groups), ("HomeOnly",))
        self.assertEqual(tuple(home.rules), ("DOMAIN-SUFFIX,home.example.test,HomeAll,no-resolve",))
        self.assertIn("# shared comment", path.read_text())

    def test_initialize_rejects_universal_mismatch_without_creating_scope(self):
        bad_universal = _write_source(
            self.source_root / "Compat-Universal.yaml",
            COMPAT_UNIVERSAL.replace("public.example.test", "changed.example.test"),
        )
        with self.assertRaises(TemplateSyncError) as caught:
            initialize_home_scope(self.root, self.compat_path, bad_universal)
        self.assertEqual(str(caught.exception), "template_source_invalid")
        self.assertFalse((self.root / HOME_SCOPE_PATH).exists())

    def test_initialize_prunes_home_names_from_round_trip_helper_sections(self):
        office = COMPAT_OFFICE.replace(
            "# shared comment\n",
            "g2: {name: helper, proxies: [HomeAll, DIRECT, Shared 3x-ui]}\n"
            "# shared comment\n",
        )
        universal = COMPAT_UNIVERSAL.replace(
            "# shared comment\n",
            "g2: {name: helper, proxies: [DIRECT, Shared 3x-ui]}\n"
            "# shared comment\n",
        )
        office_path = _write_source(self.source_root / "Compat-Office-helper.yaml", office)
        universal_path = _write_source(
            self.source_root / "Compat-Universal-helper.yaml", universal
        )
        initialize_home_scope(self.root, office_path, universal_path)
        report = run_template_sync(self.root, compat_office=office_path)
        self.assertIn(PUBLIC_TEMPLATE_FILES[0], report.changed)
        self.assertNotIn("HomeAll", (self.root / PUBLIC_TEMPLATE_FILES[0]).read_text())

    def test_scope_injection_sequences_do_not_carry_previous_line_positions(self):
        scope_text = HOME_SCOPE.replace(
            "inject-home-node-groups: [HomeOnly]\nrules:",
            "inject-home-node-groups:\n- HomeOnly\n\n\nrules:",
        )
        _write_scope(self.root, scope_text)
        scope = load_home_overlay(self.root / HOME_SCOPE_PATH, 5 * 1024 * 1024)

        copied = template_sync._copy_scope_sequence(
            scope, "inject-home-node-groups"
        )

        self.assertEqual(list(copied), list(scope.inject_home_node_groups))
        self.assertIsNone(copied.lc.line)
        self.assertEqual(copied.ca.items, {})


class TemplateSyncAtomicityTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = _make_repo(self.directory.name)
        self.source_root, self.compat_path, self.universal_path, self.balance_path = _source_dir(
            self.directory.name
        )

    def test_candidate_dump_isolated_from_round_trip_serializer_mutation(self):
        candidate = {"proxies": []}

        def mutating_dump(document):
            document["proxies"].append("serializer-side-effect")
            return "proxies: []\n"

        with patch.object(template_sync, "dump_round_trip", side_effect=mutating_dump):
            self.assertEqual(template_sync._dump_candidate(candidate), "proxies: []\n")

        self.assertEqual(candidate, {"proxies": []})

    def test_replacement_failure_at_each_target_restores_bytes_and_modes(self):
        from clash_sub import template_sync

        original_replace = template_sync._os_replace
        for fail_at in range(1, len(TEMPLATE_OUTPUT_PATHS) + 1):
            with self.subTest(fail_at=fail_at):
                with TemporaryDirectory() as directory:
                    root = _make_repo(directory)
                    _source_dir(directory)
                    compat = Path(directory) / "Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/Compat-Office.yaml"
                    balance = Path(directory) / "Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/Balance-Office.yaml"
                    before = _outputs(root)
                    calls = []

                    def fail_once(source, target):
                        calls.append(target)
                        original_replace(source, target)
                        if len(calls) == fail_at:
                            raise OSError("injected failure")

                    with patch.object(template_sync, "_os_replace", side_effect=fail_once):
                        with self.assertRaises(TemplateSyncError) as caught:
                            run_template_sync(root, compat_office=compat, balance_office=balance)
                    self.assertEqual(str(caught.exception), "template_write_failed")
                    self.assertEqual(_outputs(root), before)

    def test_restore_failure_is_explicit_after_partial_replacement(self):
        from clash_sub import template_sync

        original_replace = template_sync._os_replace
        with TemporaryDirectory() as directory:
            root = _make_repo(directory)
            _source_dir(directory)
            compat = Path(directory) / "Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/Compat-Office.yaml"
            balance = Path(directory) / "Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/Balance-Office.yaml"
            before = _outputs(root)
            state = {"write_failed": False, "restore_failed": False}

            def fail_write_then_restore(source, target):
                if not state["write_failed"] and target == root / TEMPLATE_OUTPUT_PATHS[1]:
                    original_replace(source, target)
                    state["write_failed"] = True
                    raise OSError("injected replacement failure")
                if state["write_failed"] and not state["restore_failed"]:
                    state["restore_failed"] = True
                    raise OSError("injected rollback failure")
                original_replace(source, target)

            with patch.object(
                template_sync, "_os_replace", side_effect=fail_write_then_restore
            ):
                with self.assertRaises(TemplateSyncError) as caught:
                    run_template_sync(
                        root, compat_office=compat, balance_office=balance
                    )
            self.assertEqual(str(caught.exception), "template_rollback_failed")
            self.assertIsInstance(caught.exception.__cause__, OSError)
            self.assertEqual(str(caught.exception.__cause__), "injected replacement failure")
            self.assertNotEqual(_outputs(root), before)

    def test_identical_runs_are_byte_stable(self):
        first = run_template_sync(
            self.root,
            compat_office=self.compat_path,
            balance_office=self.balance_path,
        )
        first_bytes = {relative: (self.root / relative).read_bytes() for relative in TEMPLATE_OUTPUT_PATHS}
        second = run_template_sync(
            self.root,
            compat_office=self.compat_path,
            balance_office=self.balance_path,
        )
        second_bytes = {relative: (self.root / relative).read_bytes() for relative in TEMPLATE_OUTPUT_PATHS}
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(second.changed, ())
        self.assertTrue(first.lines)


class TemplateSyncContractTests(unittest.TestCase):
    def test_fixed_paths_modes_and_report_shape(self):
        self.assertEqual(
            PUBLIC_TEMPLATE_FILES,
            (
                "templates/base/compat-office.yaml",
                "templates/dns/balance-office.yaml",
                "templates/profiles.yaml",
            ),
        )
        self.assertEqual(TEMPLATE_OUTPUT_PATHS, PUBLIC_TEMPLATE_FILES + (HOME_SCOPE_PATH,))
        self.assertEqual(OUTPUT_MODES[HOME_SCOPE_PATH], 0o600)
        self.assertEqual(OUTPUT_MODES[PUBLIC_TEMPLATE_FILES[0]], 0o644)
        self.assertEqual(TemplateSyncReport((), ()).changed, ())


if __name__ == "__main__":
    unittest.main()
