import unittest

from scripts.generate_configs import build_provider_url, render_template


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
