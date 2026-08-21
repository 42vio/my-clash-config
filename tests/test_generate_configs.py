import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.generate_configs import (
    TEMPLATES,
    atomic_write,
    build_provider_url,
    generate_configs,
    load_private_fragments,
    render_template,
)


class RenderingTests(unittest.TestCase):
    def test_build_provider_url_encodes_source_and_requests_clash_provider(self):
        result = build_provider_url(
            "https://convert.example.com/",
            "https://panel.example/sub?token=a&name=测试",
        )

        self.assertTrue(result.startswith("https://convert.example.com/sub?"))
        self.assertIn("target=clash", result)
        self.assertIn("list=true", result)
        self.assertIn("url=https%3A%2F%2Fpanel.example%2Fsub%3Ftoken%3Da%26name%3D", result)
        self.assertNotIn("测试", result)

    def test_render_template_replaces_all_markers_without_private_fragments(self):
        template = """url: '{{ SUBSCRIPTION_PROVIDER_URL }}'
{{ PRIVATE_PROXIES }}
{{ PRIVATE_PROXY_GROUPS }}
{{ PRIVATE_RULES }}
{{ DNS_VARIANT }}
{{ GEOIP_VARIANT }}
"""

        result = render_template(
            template,
            "https://convert.example.com/sub?target=clash",
            {},
            {"dns": "  respect-rules: true", "geoip": "- GEOIP,CN,🎯 Direct"},
        )

        self.assertNotIn("{{", result)
        self.assertIn("url: 'https://convert.example.com/sub?target=clash'", result)
        self.assertIn("  respect-rules: true", result)
        self.assertIn("- GEOIP,CN,🎯 Direct", result)

    def test_render_template_error_names_the_offending_marker_snippet(self):
        template = "rules:\n{{ PRIVATE_RULE }}\n"

        with self.assertRaises(ValueError) as context:
            render_template(template, "https://convert.example.com/sub?target=clash", {}, {})

        self.assertIn("{{ PRIVATE_RULE }}", str(context.exception))

    def test_render_template_allows_fragments_containing_closing_braces(self):
        template = "proxies:\n{{ PRIVATE_PROXIES }}\nrules:\n{{ PRIVATE_RULES }}\n"
        fragments = {"proxies": "  - {name: node, password: pass}}word}\n", "rules": "  - MATCH,DIRECT"}

        result = render_template(template, "https://convert.example.com/sub?target=clash", fragments, {})

        self.assertIn("pass}}word}", result)

        with self.assertRaises(ValueError):
            render_template("value: {{ UNKNOWN_MARKER }}\n", "https://convert.example.com/sub?target=clash", fragments, {})


def write_fixture_base_template(path: Path) -> None:
    path.write_text(
        "proxy-providers:\n"
        "  Subscribe:\n"
        "    type: http\n"
        "    url: '{{ SUBSCRIPTION_PROVIDER_URL }}'\n"
        "proxies:\n{{ PRIVATE_PROXIES }}\n"
        "proxy-groups:\n{{ PRIVATE_PROXY_GROUPS }}\n"
        "rules:\n{{ PRIVATE_RULES }}\n"
        "dns:\n{{ DNS_VARIANT }}\n"
        "final:\n{{ GEOIP_VARIANT }}\n",
        encoding="utf-8",
    )


def write_fixture_parts(directory: Path) -> None:
    directory.mkdir()
    (directory / "dns-balanced.part").write_text("  respect-rules: true\n", encoding="utf-8")
    (directory / "dns-privacy.part").write_text("  nameserver:\n    - https://223.5.5.5/dns-query\n", encoding="utf-8")
    (directory / "geoip-resolve.part").write_text("- GEOIP,CN,🎯 Direct\n", encoding="utf-8")
    (directory / "geoip-no-resolve.part").write_text("- GEOIP,CN,🎯 Direct,no-resolve\n", encoding="utf-8")


def write_private_fragments(private: Path) -> None:
    (private / "proxies.yaml").write_text("  - name: private-node\n", encoding="utf-8")
    (private / "proxy-groups.yaml").write_text("  - name: Private\n", encoding="utf-8")
    (private / "rules.yaml").write_text("  - MATCH,Private\n", encoding="utf-8")


class GenerationTests(unittest.TestCase):
    def test_generate_configs_injects_private_fragments_only_into_outputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            private = root / "private"
            output = root / "output"
            templates.mkdir()
            private.mkdir()
            write_fixture_base_template(templates / "_base.yaml.tmpl")
            write_fixture_parts(templates / "parts")
            write_private_fragments(private)

            outputs = generate_configs(
                templates, output, "https://convert.example.com", "https://panel.example/sub", private, True
            )

            self.assertEqual([path.name for path in outputs], [
                "My-Clash_Balanced.yaml",
                "My-Clash_Balanced_Win.yaml",
                "My-Clash_Privacy.yaml",
            ])
            self.assertIn("private-node", outputs[0].read_text(encoding="utf-8"))
            self.assertNotIn("private-node", (templates / "_base.yaml.tmpl").read_text(encoding="utf-8"))

    def test_generate_configs_requires_every_private_fragment_before_writing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            private = root / "private"
            templates.mkdir()
            private.mkdir()
            write_fixture_base_template(templates / "_base.yaml.tmpl")
            write_fixture_parts(templates / "parts")
            (private / "proxies.yaml").write_text("  - name: private-node\n", encoding="utf-8")

            with self.assertRaisesRegex(FileNotFoundError, "proxy-groups.yaml"):
                generate_configs(templates, root / "output", "https://convert.example.com", "https://panel.example/sub", private, True)

            self.assertFalse((root / "output").exists())

    def test_generate_configs_missing_part_file_fails_without_output(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            private = root / "private"
            templates.mkdir()
            private.mkdir()
            write_fixture_base_template(templates / "_base.yaml.tmpl")
            write_fixture_parts(templates / "parts")
            write_private_fragments(private)
            (templates / "parts" / "dns-privacy.part").unlink()

            with self.assertRaisesRegex(FileNotFoundError, "dns-privacy.part"):
                generate_configs(templates, root / "output", "https://convert.example.com", "https://panel.example/sub", private, True)

            self.assertFalse((root / "output").exists())


class AtomicWriteTests(unittest.TestCase):
    def test_atomic_write_cleans_up_temporary_file_when_replace_fails(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            output.mkdir()
            target = output / "My-Clash_Balanced.yaml"
            target.mkdir()

            with self.assertRaises(OSError):
                atomic_write(target, "proxies: []\n")

            self.assertEqual(list(output.glob("tmp*")), [])


class LoadPrivateFragmentsTests(unittest.TestCase):
    def test_partial_fragments_with_optional_private_returns_empty_dict(self):
        with TemporaryDirectory() as directory:
            private = Path(directory) / "private"
            private.mkdir()
            (private / "proxies.yaml").write_text("  - name: private-node\n", encoding="utf-8")

            self.assertEqual(load_private_fragments(private, False), {})

    def test_missing_private_dir_with_optional_private_returns_empty_dict(self):
        with TemporaryDirectory() as directory:
            self.assertEqual(load_private_fragments(Path(directory) / "private", False), {})


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates"
PART_DIR = TEMPLATE_DIR / "parts"
PRIVATE_EXAMPLE_DIR = ROOT / "private"

BASE_TEMPLATE_NAME = "_base.yaml.tmpl"

PART_NAMES = (
    "dns-balanced.part",
    "dns-privacy.part",
    "geoip-resolve.part",
    "geoip-no-resolve.part",
)

PRIVATE_EXAMPLE_NAMES = (
    "proxies.yaml.example",
    "proxy-groups.yaml.example",
    "rules.yaml.example",
)

FORBIDDEN_SUBSTRINGS = (
    "198.51.100.77",
    "203.0.113.88",
    "canary-panel.example.com",
    "canary-subscription.example.com",
    "relay-placeholder.example.com",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "fedcba9876543210",
    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "synthetic-password-fixture",
    "synthetic-owner-subscription-id",
    "synthetic-friend-subscription-id",
    "safe-panel-base-path-fixture",
    "operator-encrypted-storage-fixture",
)


def load_real_variants(spec) -> dict:
    return {
        "dns": (PART_DIR / spec.dns_part).read_text(encoding="utf-8").rstrip("\n"),
        "geoip": (PART_DIR / spec.geoip_part).read_text(encoding="utf-8").rstrip("\n"),
    }


class TemplateStructureTests(unittest.TestCase):
    def test_base_template_has_required_provider_private_and_variant_markers(self):
        content = (TEMPLATE_DIR / BASE_TEMPLATE_NAME).read_text(encoding="utf-8")
        self.assertIn("url: '{{ SUBSCRIPTION_PROVIDER_URL }}'", content)
        self.assertIn("{{ PRIVATE_PROXIES }}", content)
        self.assertIn("{{ PRIVATE_PROXY_GROUPS }}", content)
        self.assertIn("{{ PRIVATE_RULES }}", content)
        self.assertIn("{{ DNS_VARIANT }}", content)
        self.assertIn("{{ GEOIP_VARIANT }}", content)

    def test_private_rules_marker_precedes_lan_ruleset(self):
        content = (TEMPLATE_DIR / BASE_TEMPLATE_NAME).read_text(encoding="utf-8")
        self.assertLess(
            content.index("{{ PRIVATE_RULES }}"),
            content.index("- RULE-SET,Lan,🎯 Direct,no-resolve"),
            "base template must place private rules before the Lan ruleset",
        )

    def test_real_templates_render_without_leftover_markers(self):
        fragments = {
            "proxies": "- name: dummy",
            "proxy_groups": "- name: DummyGroup",
            "rules": "- MATCH,DummyGroup",
        }
        base = (TEMPLATE_DIR / BASE_TEMPLATE_NAME).read_text(encoding="utf-8")

        for spec in TEMPLATES:
            variants = load_real_variants(spec)

            result = render_template(
                base, "https://convert.example.com/sub?target=clash", fragments, variants
            )

            self.assertNotIn("{{", result, f"{spec.output_name} still contains an unreplaced marker")
            self.assertIn("- MATCH,DummyGroup", result)
            self.assertIn(
                f"{variants['geoip']}\n", result, f"{spec.output_name} does not inject its geoip variant"
            )

    def test_variant_parts_declare_expected_differences(self):
        balanced_dns = (PART_DIR / "dns-balanced.part").read_text(encoding="utf-8")
        privacy_dns = (PART_DIR / "dns-privacy.part").read_text(encoding="utf-8")
        self.assertIn("respect-rules: true", balanced_dns)
        self.assertNotIn("respect-rules:", privacy_dns)
        self.assertIn("https://223.5.5.5/dns-query", privacy_dns)
        self.assertEqual((PART_DIR / "geoip-resolve.part").read_text(encoding="utf-8").strip(), "- GEOIP,CN,🎯 Direct")
        self.assertEqual(
            (PART_DIR / "geoip-no-resolve.part").read_text(encoding="utf-8").strip(), "- GEOIP,CN,🎯 Direct,no-resolve"
        )

    def test_balanced_and_windows_outputs_are_identical_privacy_differs(self):
        fragments = {
            "proxies": "- name: dummy",
            "proxy_groups": "- name: DummyGroup",
            "rules": "- MATCH,DummyGroup",
        }
        base = (TEMPLATE_DIR / BASE_TEMPLATE_NAME).read_text(encoding="utf-8")
        rendered = {}
        for spec in TEMPLATES:
            rendered[spec.output_name] = render_template(
                base, "https://convert.example.com/sub?target=clash", fragments, load_real_variants(spec)
            )

        self.assertEqual(
            rendered["My-Clash_Balanced.yaml"],
            rendered["My-Clash_Balanced_Win.yaml"],
            "Balanced and Win must render identical outputs",
        )
        self.assertNotEqual(
            rendered["My-Clash_Balanced.yaml"],
            rendered["My-Clash_Privacy.yaml"],
            "Privacy must differ from Balanced",
        )

    def test_public_templates_and_examples_contain_no_private_data(self):
        for directory, names in (
            (TEMPLATE_DIR, (BASE_TEMPLATE_NAME,)),
            (PART_DIR, PART_NAMES),
            (PRIVATE_EXAMPLE_DIR, PRIVATE_EXAMPLE_NAMES),
        ):
            for name in names:
                lowered = (directory / name).read_text(encoding="utf-8").lower()
                for forbidden in FORBIDDEN_SUBSTRINGS:
                    self.assertNotIn(forbidden.lower(), lowered, f"{name} leaks {forbidden!r}")


class ComposeSecurityTests(unittest.TestCase):
    def test_compose_binds_both_services_only_to_loopback(self):
        content = (ROOT / "compose.yaml").read_text(encoding="utf-8")
        self.assertIn('"127.0.0.1:${SUBCONVERTER_PORT:-25500}:25500"', content)
        self.assertIn('"127.0.0.1:${SUBWEB_PORT:-58080}:80"', content)
        self.assertNotIn("0.0.0.0:", content)

    def test_pref_ini_listens_on_all_interfaces_inside_the_container(self):
        content = (ROOT / "docker" / "subconverter" / "pref.ini").read_text(encoding="utf-8")
        self.assertIn("listen=0.0.0.0", content)
        self.assertIn("api_mode=true", content)
        self.assertEqual(content.count("default_url"), 1)
        self.assertIn("default_url=\n", content)


class CliTests(unittest.TestCase):
    def test_cli_requires_source_url_and_converter_base_url(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "generate_configs.py")],
            cwd=ROOT,
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--source-url", result.stderr)

    def test_cli_generates_public_configs_without_printing_source_url(self):
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "generate_configs.py"),
                    "--source-url", "https://panel.example/sub?token=private-value",
                    "--converter-base-url", "https://convert.example.com",
                    "--output-dir", directory,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("private-value", result.stdout)
            self.assertNotIn("private-value", result.stderr)
            self.assertTrue((Path(directory) / "My-Clash_Balanced.yaml").is_file())

    def test_cli_private_mode_missing_fragments_fails_without_output(self):
        with TemporaryDirectory() as private_dir, TemporaryDirectory() as output_dir:
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "generate_configs.py"),
                    "--source-url", "https://panel.example/sub?token=private-value",
                    "--converter-base-url", "https://convert.example.com",
                    "--output-dir", output_dir,
                    "--private-dir", private_dir,
                    "--private",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("generation failed", result.stderr)
            self.assertNotIn("private-value", result.stderr)
            self.assertEqual(list(Path(output_dir).glob("*.yaml")), [])

    def test_cli_public_mode_prints_private_notice(self):
        with TemporaryDirectory() as directory:
            result = subprocess.run(
                [
                    sys.executable, str(ROOT / "scripts" / "generate_configs.py"),
                    "--source-url", "https://panel.example/sub?token=private-value",
                    "--converter-base-url", "https://convert.example.com",
                    "--output-dir", directory,
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("未注入私有节点", result.stdout)
