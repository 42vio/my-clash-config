import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.generate_configs import build_provider_url, generate_configs, render_template


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
