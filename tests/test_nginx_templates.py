"""Strict Nginx template tests.

The deploy templates must render a TLS default server on 8443 plus
only the approved panel and subscription routes, never TCP 443 or the
raw 3x-ui/subconverter listeners, never a product banner, and the /s/
location must disable access logs and pass X-Real-IP so publisher
rate limiting sees the real client address.
"""

import importlib.util
import re
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_NGINX = ROOT / "deploy" / "nginx"


def load_script(name):
    module_name = "scripts_%s" % name
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "scripts" / ("%s.py" % name)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


install_server = load_script("install_server")

render_template = install_server.render_template
nginx_template_context = install_server.nginx_template_context
NGINX_FILE_TARGETS = install_server.NGINX_FILE_TARGETS


def service_document(mode):
    authority_host = "198.51.100.10" if mode == "ip" else None
    fullchain = (
        "/etc/letsencrypt/live/198.51.100.10/fullchain.pem"
        if mode == "ip"
        else "/etc/letsencrypt/live/clash-sub-domain/fullchain.pem"
    )
    return {
        "schema-version": 1,
        "private-root": "/opt/clash-sub/private",
        "converter-base-url": "http://127.0.0.1:25500",
        "publication": {
            "mode": mode,
            "subscription-authority": (authority_host or "sub") + ".example.com:8443"
            if mode == "domain"
            else "198.51.100.10:8443",
            "panel-authority": (authority_host or "panel") + ".example.com:8443"
            if mode == "domain"
            else "198.51.100.10:8443",
            "publisher-listen": "127.0.0.1",
            "publisher-port": 25501,
        },
        "reality": {
            "public-address": "198.51.100.10",
            "public-port": 443,
            "required-flow": "xtls-rprx-vision",
        },
        "xui": {
            "panel-listen": "127.0.0.1",
            "panel-port": 2053,
            "panel-base-path": "/example-random-panel-path/",
            "subscription-listen": "127.0.0.1",
            "subscription-port": 2096,
            "xray-config-path": "/usr/local/x-ui/bin/config.json",
            "xray-binary-path": "/usr/local/x-ui/bin/xray-linux-amd64",
            "expected-panel-version": "3.6.0",
            "expected-xray-version": "26.6.27",
        },
        "certificate": {
            "fullchain-path": fullchain,
            "acme-email": "admin@example.com",
            "alert-before-seconds": 259200 if mode == "ip" else 1209600,
            "alert-command": ["notify-command", "--channel", "private"]
            if mode == "ip"
            else [],
        },
    }


def load_settings(mode):
    from clash_sub.settings import _parse_service_settings

    return _parse_service_settings(service_document(mode))


def render(mode):
    template_name = (
        "10-clash-ip.conf.tmpl" if mode == "ip" else "10-clash-domain.conf.tmpl"
    )
    text = (DEPLOY_NGINX / template_name).read_text(encoding="utf-8")
    return render_template(text, nginx_template_context(load_settings(mode)))


LISTEN_RE = re.compile(r"\blisten\s+(?:\[[^\]]*\]:)?(\d+)")


def location_block(text, pattern):
    match = re.search(pattern, text)
    if match is None:
        raise AssertionError("location %s not found" % pattern)
    start = match.start()
    opening = text.index("{", match.start())
    depth = 0
    for position in range(opening, len(text)):
        if text[position] == "{":
            depth += 1
        elif text[position] == "}":
            depth -= 1
            if depth == 0:
                return text[start : position + 1]
    raise AssertionError("unbalanced location block for %s" % pattern)


class AcmeTemplateTests(unittest.TestCase):
    def setUp(self):
        self.text = (DEPLOY_NGINX / "00-acme-http.conf.tmpl").read_text(
            encoding="utf-8"
        )

    def test_declares_only_tcp_80_default_server(self):
        ports = [int(p) for p in LISTEN_RE.findall(self.text)]
        self.assertEqual(sorted(set(ports)), [80])
        self.assertIn("listen 80 default_server;", self.text)

    def test_serves_only_the_acme_challenge_and_404_elsewhere(self):
        self.assertIn("location ^~ /.well-known/acme-challenge/", self.text)
        self.assertIn("root /var/lib/clash-sub/acme;", self.text)
        self.assertIn("try_files $uri =404;", self.text)
        self.assertIn("location / {", self.text)
        self.assertIn("return 404;", self.text)

    def test_no_tls_listeners_or_proxying_in_acme_file(self):
        self.assertNotIn("ssl", self.text)
        self.assertNotIn("proxy_pass", self.text)


class TlsTemplateTests(unittest.TestCase):
    modes = ("domain", "ip")

    def test_only_tcp_808443_listeners_are_ever_declared(self):
        for mode in self.modes:
            text = render(mode)
            ports = [int(p) for p in LISTEN_RE.findall(text)]
            self.assertTrue(ports, mode)
            self.assertEqual(set(ports), {8443}, mode)

    def test_generic_default_server_on_8443(self):
        for mode in self.modes:
            self.assertIn("listen 8443 ssl default_server;", render(mode))

    def test_tls_restricted_to_1_2_and_1_3(self):
        for mode in self.modes:
            self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", render(mode))
            self.assertNotIn("TLSv1.1", render(mode))
            self.assertNotIn("TLSv1.0", render(mode))

    def test_subscription_location_proxies_to_publisher_with_real_ip(self):
        for mode in self.modes:
            block = location_block(render(mode), r"location /s/ \{")
            self.assertIn("proxy_pass http://127.0.0.1:25501;", block)
            self.assertEqual(block.count("proxy_pass"), 1)
            self.assertIn("access_log off;", block)
            self.assertIn("proxy_set_header X-Real-IP $remote_addr;", block)
            self.assertIn("limit_req zone=", block)

    def test_panel_location_proxies_to_loopback_panel_port(self):
        for mode in self.modes:
            block = location_block(
                render(mode), r"location /example-random-panel-path/ \{"
            )
            self.assertIn("proxy_pass http://127.0.0.1:2053;", block)
            self.assertEqual(block.count("proxy_pass"), 1)
            self.assertIn("proxy_set_header Upgrade $http_upgrade;", block)
            self.assertIn('proxy_set_header Connection "upgrade";', block)

    def test_every_server_has_the_same_generic_fallback(self):
        for mode in self.modes:
            text = render(mode)
            servers = text.count("server {")
            fallbacks = len(
                re.findall(r"location / \{[^}]*return 404;[^}]*\}", text)
            )
            self.assertGreaterEqual(servers, 1)
            self.assertEqual(fallbacks, servers, mode)

    def test_body_size_response_and_time_limits_are_bounded(self):
        for mode in self.modes:
            text = render(mode)
            self.assertIn("client_max_body_size 1m;", text)
            self.assertIn("proxy_connect_timeout", text)
            self.assertIn("proxy_read_timeout", text)
            self.assertIn("proxy_send_timeout", text)
            self.assertIn("proxy_hide_header Server;", text)

    def test_rate_limit_zone_is_keyed_by_binary_remote_addr(self):
        for mode in self.modes:
            text = render(mode)
            self.assertRegex(
                text, r"limit_req_zone \$binary_remote_addr zone=\S+ "
            )

    def test_security_headers_are_scoped_to_generic_fallbacks_only(self):
        # Server-level headers would be inherited into the panel and
        # subscription locations; a restrictive CSP on the panel breaks
        # its SPA in CSP-enforcing browsers.
        for mode in self.modes:
            text = render(mode)
            fallbacks = re.findall(r"location / \{[^}]*\}", text)
            self.assertTrue(fallbacks, mode)
            for block in fallbacks:
                self.assertIn("X-Content-Type-Options nosniff", block, mode)
                self.assertIn("Referrer-Policy no-referrer", block, mode)
                self.assertIn("Content-Security-Policy", block, mode)
                self.assertIn("return 404;", block, mode)
            panel_block = location_block(
                text, r"location /example-random-panel-path/ \{"
            )
            sub_block = location_block(text, r"location /s/ \{")
            self.assertNotIn("add_header", panel_block, mode)
            self.assertNotIn("add_header", sub_block, mode)
            # Exactly three header lines per fallback proves none are
            # declared at server level or inside proxied locations.
            self.assertEqual(text.count("add_header"), 3 * len(fallbacks), mode)

    def test_never_exposes_forbidden_ports_blocks_or_products(self):
        forbidden = (
            "stream {",
            "listen 443",
            ":443",
            "1443",
            "25500",
            "2096",
            "autoindex",
            " alias ",
            "server_tokens on",
        )
        for mode in self.modes:
            text = render(mode)
            for token in forbidden:
                self.assertNotIn(token, text, "%s: %s" % (mode, token))

    def test_rendered_output_has_no_product_banner(self):
        for mode in self.modes:
            # Certificate file paths legitimately name the cert directory;
            # everything else must stay product-neutral.
            text = "\n".join(
                line
                for line in render(mode).splitlines()
                if not line.strip().startswith("ssl_certificate")
            )
            for token in ("clash", "mihomo", "subconverter", "x-ui"):
                self.assertNotIn(token, text, mode)

    def test_templates_carry_placeholders_not_real_domains(self):
        for name in ("10-clash-domain.conf.tmpl", "10-clash-ip.conf.tmpl"):
            text = (DEPLOY_NGINX / name).read_text(encoding="utf-8")
            self.assertNotIn("example.com", text)
            self.assertNotIn("198.51", text)

    def test_rendered_output_substitutes_every_placeholder(self):
        for mode in self.modes:
            self.assertNotIn("{{", render(mode))


class DomainModeTests(unittest.TestCase):
    def setUp(self):
        self.text = render("domain")

    def test_separate_exact_server_names_share_one_certificate(self):
        self.assertIn("server_name panel.example.com;", self.text)
        self.assertIn("server_name sub.example.com;", self.text)
        certificates = re.findall(r"ssl_certificate [^;]+;", self.text)
        key_certificates = [c for c in certificates if "privkey" not in c]
        self.assertGreaterEqual(len(key_certificates), 3)
        self.assertEqual(len(set(key_certificates)), 1)
        expected = str(Path("/etc/letsencrypt/live/clash-sub-domain/fullchain.pem").resolve())
        self.assertIn("ssl_certificate %s;" % expected, self.text)

    def test_no_wildcard_or_ip_server_names(self):
        self.assertNotIn("server_name *", self.text)
        self.assertNotIn("server_name 198.", self.text)


class IpModeTests(unittest.TestCase):
    def setUp(self):
        self.text = render("ip")

    def test_single_literal_ip_server_carries_both_routes(self):
        self.assertIn("server_name 198.51.100.10;", self.text)
        self.assertEqual(self.text.count("server {"), 2)  # default + ip server
        self.assertIn("location /s/ {", self.text)
        self.assertIn("location /example-random-panel-path/ {", self.text)

    def test_uses_the_ip_certificate(self):
        expected = str(Path("/etc/letsencrypt/live/198.51.100.10/fullchain.pem").resolve())
        self.assertIn("ssl_certificate %s;" % expected, self.text)


class InstallTargetTests(unittest.TestCase):
    def test_nginx_install_targets_carry_the_project_marker(self):
        for name, target in NGINX_FILE_TARGETS.items():
            self.assertIn("clash-sub", target, name)
            self.assertTrue(target.startswith("/etc/nginx/"), name)

    def test_privkey_is_derived_as_fullchain_sibling(self):
        context = nginx_template_context(load_settings("domain"))
        self.assertEqual(
            context["{{PRIVKEY_PATH}}"],
            str(Path("/etc/letsencrypt/live/clash-sub-domain/privkey.pem").resolve()),
        )


if __name__ == "__main__":
    unittest.main()
