"""Regression coverage for anchor-preserving template sync and rendering."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from clash_sub.generator import render_user_bundle
from clash_sub.template_sync import _dump, _profiles, _sanitize_compat
from clash_sub.yaml_rt import clone_round_trip_document, dump_round_trip, load_round_trip, plain_data
from tests.test_lightweight_generator import provider, reality_proxy


SOURCE = """anchors:
  select: &select {type: select}
  auto: &auto {type: url-test, use: [AmyTelecom]} # automatic policy
  direct: &direct {<<: *select, proxies: [DIRECT, Main]}
  rule: &rule {type: http, behavior: classical, interval: 86400} # rule base
icons:
  main: &main-icon https://example.invalid/main.png
  feature: &feature-icon https://example.invalid/feature.png
dns:
  enable: true
proxies:
- {name: Private Node, type: ss, server: 192.0.2.1, port: 443, cipher: aes-128-gcm, password: test-only}
proxy-providers:
  AmyTelecom: {type: http, url: https://example.invalid/private, path: ./private.yaml}
proxy-groups:
- {name: Main, <<: *select, proxies: [Auto, Private Node, Region], icon: *main-icon}
- {name: Feature, <<: *direct, icon: *feature-icon}
- {name: Auto, <<: *auto, icon: *main-icon}
- {name: Region, <<: *auto, filter: HK, icon: *main-icon}
rule-providers:
  Example: {<<: *rule, url: https://example.invalid/rules.yaml, path: ./rule/Example.yaml}
rules:
- RULE-SET,Example,Feature
- MATCH,Main
"""


class YamlFormatTests(unittest.TestCase):
    def assert_compact(self, text):
        self.assertFalse(any(line.startswith(',') for line in text.splitlines()))
        self.assertEqual(text.count('# rule base'), 1)
        self.assertIn('Example: {<<: *rule, url:', text)
        self.assertIn('{name: Feature, <<: *direct, icon: *feature-icon}', text)
        self.assertNotIn('id001', text)

    def test_document_clone_preserves_anchor_graph_without_mutating_source(self):
        source = load_round_trip(SOURCE)
        before = dump_round_trip(source)
        cloned = clone_round_trip_document(source)
        self.assert_compact(dump_round_trip(cloned))
        self.assertEqual(plain_data(source), plain_data(cloned))
        self.assertIs(cloned['rule-providers']['Example'].merge[0], cloned['anchors']['rule'])
        cloned['anchors']['rule']['interval'] = 123
        self.assertEqual(dump_round_trip(source), before)

    def test_inherited_private_nodes_stay_before_icon_and_source_is_unchanged(self):
        text = SOURCE.replace(
            'select: &select {type: select}',
            'select: &select {type: select, proxies: [Private Node, DIRECT]}',
        ).replace('proxies: [Auto, Private Node, Region], ', '')
        source = load_round_trip(text)
        before = dump_round_trip(source)
        sanitized, injections = _sanitize_compat(source)
        rendered = _dump(sanitized)
        self.assertNotIn('Private Node', rendered)
        self.assertIn('{name: Main, <<: *select, proxies: [DIRECT], icon: *main-icon}', rendered)
        self.assertEqual(_profiles(injections)['inject-node-positions'], {'Main': 0})
        self.assertEqual(dump_round_trip(source), before)

    def test_sync_and_both_profiles_keep_compact_anchors_and_group_order(self):
        source = load_round_trip(SOURCE)
        before = dump_round_trip(source)
        sanitized, injections = _sanitize_compat(source)
        text = _dump(sanitized)
        self.assert_compact(text)
        self.assertEqual(dump_round_trip(source), before)
        self.assertNotIn('Private Node', text)
        self.assertNotIn('AmyTelecom', text)
        manifest = _profiles(injections)
        self.assertEqual(manifest['inject-provider-groups'], ['Auto', 'Region'])
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'base').mkdir(); (root / 'dns').mkdir()
            (root / 'base/Clash-Compat.yaml').write_text(text)
            (root / 'dns/Clash-Balance.yaml').write_text('dns: {enable: true, enhanced-mode: fake-ip}\n')
            (root / 'profiles.yaml').write_text(_dump(manifest))
            for owner in (True, False):
                bundle = render_user_bundle(owner, [reality_proxy('Generated')], provider() if owner else None, root)
                for variant, rendered in bundle.items():
                    with self.subTest(owner=owner, variant=variant):
                        self.assert_compact(rendered)
                        document = load_round_trip(rendered)
                        groups = {g['name']: g for g in document['proxy-groups']}
                        self.assertEqual(list(groups), ['Main', 'Feature', 'Auto', 'Region'] if owner else ['Main', 'Feature'])
                        self.assertEqual(groups['Main']['proxies'], ['Auto', 'Generated', 'Region'] if owner else ['Generated'])
                        line = next(line for line in rendered.splitlines() if line.startswith('- {name: Main,'))
                        self.assertLess(line.index('<<:'), line.index('proxies:'))
                        self.assertLess(line.index('proxies:'), line.index('icon:'))
                        if owner:
                            for name in ('Auto', 'Region'):
                                self.assertEqual(groups[name]['use'], ['AmyTelecom'])
                                self.assertNotIn('proxies', groups[name])
                        self.assertEqual(plain_data(document['rules']), plain_data(source['rules']))
                        self.assertEqual(plain_data(document['rule-providers']), plain_data(source['rule-providers']))


if __name__ == '__main__':
    unittest.main()
