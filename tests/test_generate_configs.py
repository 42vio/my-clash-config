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
        template = """url: '{{ SUBSCRIPTION_PROVIDER_URL }}'\n{{ PRIVATE_PROXIES }}
{{ PRIVATE_PROXY_GROUPS }}
{{ PRIVATE_RULES }}
"""

        result = render_template(template, "https://convert.example.com/sub?target=clash", {})

        self.assertNotIn("{{", result)
        self.assertIn("url: 'https://convert.example.com/sub?target=clash'", result)

    def test_render_template_error_names_the_offending_marker_snippet(self):
        template = "rules:\n{{ PRIVATE_RULE }}\n"

        with self.assertRaises(ValueError) as context:
            render_template(template, "https://convert.example.com/sub?target=clash", {})

        self.assertIn("{{ PRIVATE_RULE }}", str(context.exception))

    def test_render_template_allows_fragments_containing_closing_braces(self):
        template = "proxies:\n{{ PRIVATE_PROXIES }}\nrules:\n{{ PRIVATE_RULES }}\n"
        fragments = {"proxies": "  - {name: node, password: pass}}word}\n", "rules": "  - MATCH,DIRECT"}

        result = render_template(template, "https://convert.example.com/sub?target=clash", fragments)

        self.assertIn("pass}}word}", result)

        with self.assertRaises(ValueError):
            render_template("value: {{ UNKNOWN_MARKER }}\n", "https://convert.example.com/sub?target=clash", fragments)


def write_fixture_template(path: Path) -> None:
    path.write_text(
        "proxy-providers:\n"
        "  Subscribe:\n"
        "    type: http\n"
        "    url: '{{ SUBSCRIPTION_PROVIDER_URL }}'\n"
        "proxies:\n{{ PRIVATE_PROXIES }}\n"
        "proxy-groups:\n{{ PRIVATE_PROXY_GROUPS }}\n"
        "rules:\n{{ PRIVATE_RULES }}\n",
        encoding="utf-8",
    )


class GenerationTests(unittest.TestCase):
    def test_generate_configs_injects_private_fragments_only_into_outputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            private = root / "private"
            output = root / "output"
            templates.mkdir()
            private.mkdir()
            for name in ("My-Clash_Balanced", "My-Clash_Balanced_Win", "My-Clash_Privacy"):
                write_fixture_template(templates / f"{name}.yaml.tmpl")
            (private / "proxies.yaml").write_text("  - name: private-node\n", encoding="utf-8")
            (private / "proxy-groups.yaml").write_text("  - name: Private\n", encoding="utf-8")
            (private / "rules.yaml").write_text("  - MATCH,Private\n", encoding="utf-8")

            outputs = generate_configs(
                templates, output, "https://convert.example.com", "https://panel.example/sub", private, True
            )

            self.assertEqual([path.name for path in outputs], [
                "My-Clash_Balanced.yaml",
                "My-Clash_Balanced_Win.yaml",
                "My-Clash_Privacy.yaml",
            ])
            self.assertIn("private-node", outputs[0].read_text(encoding="utf-8"))
            self.assertNotIn("private-node", (templates / "My-Clash_Balanced.yaml.tmpl").read_text(encoding="utf-8"))

    def test_generate_configs_requires_every_private_fragment_before_writing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            templates = root / "templates"
            private = root / "private"
            templates.mkdir()
            private.mkdir()
            for name in ("My-Clash_Balanced", "My-Clash_Balanced_Win", "My-Clash_Privacy"):
                write_fixture_template(templates / f"{name}.yaml.tmpl")
            (private / "proxies.yaml").write_text("  - name: private-node\n", encoding="utf-8")

            with self.assertRaisesRegex(FileNotFoundError, "proxy-groups.yaml"):
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
PRIVATE_EXAMPLE_DIR = ROOT / "private"

TEMPLATE_NAMES = tuple(spec.template_name for spec in TEMPLATES)

PRIVATE_EXAMPLE_NAMES = (
    "proxies.yaml.example",
    "proxy-groups.yaml.example",
    "rules.yaml.example",
)

FORBIDDEN_SUBSTRINGS = (
    "kfcv50",
    "420615",
    "hitrontech",
    "BWG",
    "182616",
    "161.129",
    "106.15.121",
    "199.19.110",
    "172.28.30",
    "192.168.2",
    "AmyTelecom",
    "99d8bd45",
    "ce8346e6",
    "aaq_AIa3r",
    "48b3db40",
    "password:",
    "viokeo",
    "HomeServer",
    "ProxyServer",
    "AliyunSS",
    "HomeVLESS",
    "HomeSS@Debian",
    "自建节点",
)


class TemplateStructureTests(unittest.TestCase):
    def test_all_templates_have_required_provider_and_private_markers(self):
        for name in TEMPLATE_NAMES:
            content = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
            self.assertIn("url: '{{ SUBSCRIPTION_PROVIDER_URL }}'", content)
            self.assertIn("{{ PRIVATE_PROXIES }}", content)
            self.assertIn("{{ PRIVATE_PROXY_GROUPS }}", content)
            self.assertIn("{{ PRIVATE_RULES }}", content)

    def test_private_rules_marker_precedes_lan_ruleset(self):
        for name in TEMPLATE_NAMES:
            content = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
            self.assertLess(
                content.index("{{ PRIVATE_RULES }}"),
                content.index("- RULE-SET,Lan,🎯 Direct,no-resolve"),
                f"{name} must place private rules before the Lan ruleset",
            )

    def test_real_templates_render_without_leftover_markers(self):
        fragments = {
            "proxies": "- name: dummy",
            "proxy_groups": "- name: DummyGroup",
            "rules": "- MATCH,DummyGroup",
        }

        for name in TEMPLATE_NAMES:
            template = (TEMPLATE_DIR / name).read_text(encoding="utf-8")

            result = render_template(
                template, "https://convert.example.com/sub?target=clash", fragments
            )

            self.assertNotIn("{{", result, f"{name} still contains an unreplaced marker")
            self.assertIn("- MATCH,DummyGroup", result)

    def test_dns_variant_rules_remain_distinct(self):
        balanced = (TEMPLATE_DIR / "My-Clash_Balanced.yaml.tmpl").read_text(encoding="utf-8")
        windows = (TEMPLATE_DIR / "My-Clash_Balanced_Win.yaml.tmpl").read_text(encoding="utf-8")
        privacy = (TEMPLATE_DIR / "My-Clash_Privacy.yaml.tmpl").read_text(encoding="utf-8")
        self.assertIn("respect-rules: true", balanced)
        self.assertIn("respect-rules: true", windows)
        self.assertNotIn("respect-rules:", privacy)
        self.assertIn("- GEOIP,CN,🎯 Direct", balanced)
        self.assertIn("- GEOIP,CN,🎯 Direct", windows)
        self.assertIn("- GEOIP,CN,🎯 Direct,no-resolve", privacy)

    def test_public_templates_and_examples_contain_no_private_data(self):
        for directory, names in ((TEMPLATE_DIR, TEMPLATE_NAMES), (PRIVATE_EXAMPLE_DIR, PRIVATE_EXAMPLE_NAMES)):
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
