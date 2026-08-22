import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
NGINX_TEMPLATE = ROOT / "deploy" / "nginx" / "clash-sub.conf.tmpl"
TRAFFIC_SERVICE = ROOT / "deploy" / "systemd" / "clash-sub-traffic.service"
TRAFFIC_TIMER = ROOT / "deploy" / "systemd" / "clash-sub-traffic.timer"
REQUIREMENTS = ROOT / "requirements.txt"


def _without_comments(text):
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _blocks(text, keyword):
    """Return balanced nginx blocks beginning with *keyword*."""
    blocks = []
    position = 0
    marker = re.compile(r"(?m)^\s*%s\b[^\n]*\{\s*$" % re.escape(keyword))
    while match := marker.search(text, position):
        depth = 0
        for end in range(match.end() - 1, len(text)):
            if text[end] == "{":
                depth += 1
            elif text[end] == "}":
                depth -= 1
                if depth == 0:
                    blocks.append(text[match.start() : end + 1])
                    position = end + 1
                    break
        else:
            raise AssertionError("unterminated nginx block")
    return blocks


def _directive(block, name):
    return re.findall(r"(?m)^\s*%s\s+([^;]+);" % re.escape(name), block)


def _unit_value(text, key):
    return re.findall(r"(?m)^%s=(.+)$" % re.escape(key), text)


def _server_with_name(servers, name):
    matches = [server for server in servers if _directive(server, "server_name") == [name]]
    if len(matches) != 1:
        raise AssertionError("expected exactly one server_name %r, found %d" % (name, len(matches)))
    return matches[0]


class LightweightDeploymentTests(unittest.TestCase):
    def setUp(self):
        self.template = NGINX_TEMPLATE.read_text(encoding="utf-8")
        self.nginx = _without_comments(self.template)
        self.servers = _blocks(self.nginx, "server")

    def test_nginx_template_uses_only_the_six_approved_placeholders(self):
        self.assertEqual(
            set(re.findall(r"\{\{[A-Z_]+\}\}", self.template)),
            {
                "{{DOMAIN}}",
                "{{FULLCHAIN_PATH}}",
                "{{PRIVKEY_PATH}}",
                "{{PANEL_BASE_PATH}}",
                "{{PANEL_UPSTREAM}}",
                "{{ROUTES_INCLUDE}}",
            },
        )

    def test_http_only_serves_acme_challenges_and_generic_404(self):
        http_servers = [server for server in self.servers if "listen 80 default_server;" in server]
        self.assertEqual(len(http_servers), 1)
        server = http_servers[0]
        self.assertIn("listen [::]:80 default_server;", server)
        self.assertEqual(_directive(server, "server_name"), ["_"])
        locations = _blocks(server, "location")
        self.assertEqual(len(locations), 2)
        self.assertIn("location ^~ /.well-known/acme-challenge/ {", locations[0])
        self.assertIn("root /var/lib/clash-sub/acme;", locations[0])
        self.assertIn("try_files $uri =404;", locations[0])
        self.assertIn("location / {", locations[1])
        self.assertIn("return 404;", locations[1])
        self.assertNotRegex(server, r"\breturn\s+30[12378]\b")
        self.assertNotIn("proxy_pass", server)
        self.assertNotIn("alias ", server)

    def test_tls_servers_share_one_san_pair_and_default_is_generic_404(self):
        tls_servers = [server for server in self.servers if "listen 8443 ssl" in server]
        self.assertEqual(len(tls_servers), 3)
        default = _server_with_name(tls_servers, "_")
        panel = _server_with_name(tls_servers, "panel.{{DOMAIN}}")
        subscription = _server_with_name(tls_servers, "sub.{{DOMAIN}}")
        for server in (default, panel, subscription):
            self.assertRegex(server, r"listen 8443 ssl(?: default_server)?;")
            self.assertRegex(server, r"listen \[::\]:8443 ssl(?: default_server)?;")
            self.assertEqual(_directive(server, "ssl_certificate"), ["{{FULLCHAIN_PATH}}"])
            self.assertEqual(_directive(server, "ssl_certificate_key"), ["{{PRIVKEY_PATH}}"])
            self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", server)
        self.assertIn("listen 8443 ssl default_server;", default)
        self.assertIn("return 404;", default)
        self.assertNotIn("proxy_pass", default)

    def test_panel_proxies_only_the_random_base_path_to_validated_loopback(self):
        panel = _server_with_name(self.servers, "panel.{{DOMAIN}}")
        locations = _blocks(panel, "location")
        proxied = [location for location in locations if _directive(location, "proxy_pass")]
        self.assertEqual(len(proxied), 1)
        self.assertIn("location ^~ {{PANEL_BASE_PATH}} {", proxied[0])
        self.assertEqual(_directive(proxied[0], "proxy_pass"), ["http://{{PANEL_UPSTREAM}}"])
        self.assertIn("validated loopback", self.template)
        unmatched = [location for location in locations if location not in proxied]
        self.assertEqual(len(unmatched), 1)
        self.assertIn("location / {", unmatched[0])
        self.assertIn("return 404;", unmatched[0])
        self.assertNotIn("proxy_pass", unmatched[0])

    def test_subscription_uses_only_generated_exact_routes_and_silent_404_fallbacks(self):
        subscription = _server_with_name(self.servers, "sub.{{DOMAIN}}")
        self.assertIn(
            "limit_req_zone $binary_remote_addr zone=clash_subscription:10m rate=2r/s;",
            self.nginx,
        )
        self.assertIn("include {{ROUTES_INCLUDE}};", subscription)
        self.assertNotIn("proxy_pass", subscription)
        self.assertNotIn("alias ", subscription)
        locations = _blocks(subscription, "location")
        self.assertEqual(len(locations), 2)
        unmatched_subscription = next(
            location for location in locations if "location ^~ /s/ {" in location
        )
        generic = next(location for location in locations if "location / {" in location)
        for location in (unmatched_subscription, generic):
            self.assertIn("access_log off;", location)
            self.assertIn("log_not_found off;", location)
            self.assertIn("return 404;", location)

    def test_nginx_template_has_no_legacy_or_broad_public_exposure(self):
        forbidden = (
            r"\blisten\s+(?:\[::\]:)?443\b",
            r"\blisten\s+(?:\[::\]:)?1443\b",
            r"\blisten\s+(?:\[::\]:)?8080\b",
            r"\bstream\s*\{",
            r"\budp\b",
            r"\bdocker\b",
            r"\bpublisher\b",
            r"\bsubconverter\b",
            r"\bcertbot\b",
            r"\bautoindex\b",
            r"location\s+(?:=\s*)?/sub/",
            r"location\s+(?:=\s*)?/json/",
            r"location\s+(?:=\s*)?/clash/",
        )
        for expression in forbidden:
            with self.subTest(expression=expression):
                self.assertNotRegex(self.nginx, expression)

    def test_traffic_unit_is_root_only_hardened_traffic_update_without_generation(self):
        service = TRAFFIC_SERVICE.read_text(encoding="utf-8")
        self.assertIn("[Service]", service)
        required = {
            "Type": "oneshot",
            "ExecStart": "/usr/local/bin/clash-sub traffic-update",
            "User": "root",
            "NoNewPrivileges": "true",
            "PrivateTmp": "true",
            "ProtectSystem": "strict",
            "ProtectHome": "true",
            "PrivateDevices": "true",
            "ProtectKernelTunables": "true",
            "ProtectKernelModules": "true",
            "ProtectControlGroups": "true",
            "RestrictSUIDSGID": "true",
            "LockPersonality": "true",
            "ReadWritePaths": "/var/lib/clash-sub/private /var/lib/clash-sub/public /etc/nginx/clash-sub",
        }
        for key, value in required.items():
            with self.subTest(key=key):
                self.assertEqual(_unit_value(service, key), [value])
        self.assertNotIn("ExecStartPre", service)
        self.assertNotRegex(service, r"\b(?:sync|airport|generate|render)\b")

    def test_traffic_timer_runs_once_daily_and_recovers_missed_runs(self):
        timer = TRAFFIC_TIMER.read_text(encoding="utf-8")
        self.assertIn("[Timer]", timer)
        self.assertEqual(_unit_value(timer, "OnCalendar"), ["daily"])
        self.assertEqual(_unit_value(timer, "RandomizedDelaySec"), ["5m"])
        self.assertEqual(_unit_value(timer, "Persistent"), ["true"])
        self.assertIn("WantedBy=timers.target", timer)
        self.assertNotRegex(timer, r"\b(?:sync|airport|generate|render)\b")

    def test_task7_group_contract_is_documented_without_an_installer_mutation(self):
        self.assertIn(
            "before first sync deployment must install -d -o root -g www-data -m 2750 "
            "/var/lib/clash-sub/public",
            self.template,
        )
        self.assertIn("Task13 will provide the executable manual command.", self.template)
        self.assertIn("setgid/group contract", self.template)
        self.assertIn("0640 release YAML", self.template)
        self.assertNotIn("ExecStartPre", self.template)
        self.assertNotIn("install-server", self.template)

    def test_requirements_are_the_two_approved_pins(self):
        self.assertEqual(
            REQUIREMENTS.read_text(encoding="utf-8").splitlines(),
            ["Jinja2==3.1.6", "PyYAML==6.0.3"],
        )


if __name__ == "__main__":
    unittest.main()
