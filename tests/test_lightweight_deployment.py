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


def _location_header(block):
    return block.split("{", 1)[0].strip()


def _selected_location(locations, uri):
    exact = "location = %s" % uri
    for location in locations:
        if _location_header(location) == exact:
            return location

    prefixes = []
    for location in locations:
        header = _location_header(location)
        if header.startswith("location ^~ "):
            prefix = header.removeprefix("location ^~ ")
        elif header.startswith("location "):
            prefix = header.removeprefix("location ")
        else:
            continue
        if uri.startswith(prefix):
            prefixes.append((len(prefix), location))
    return max(prefixes, default=(0, None))[1]


def _server_with_name(servers, name):
    matches = [server for server in servers if _directive(server, "server_name") == [name]]
    if len(matches) != 1:
        raise AssertionError("expected exactly one server_name %r, found %d" % (name, len(matches)))
    return matches[0]


DOCUMENTATION_PATHS = {
    "readme": ROOT / "README.md",
    "deployment": ROOT / "DEPLOYMENT.md",
    "xui-setup": ROOT / "docs" / "3x-ui-setup.md",
    "operations": ROOT / "docs" / "operations.md",
    "private-data": ROOT / "docs" / "private-data.md",
    "legacy-topology": ROOT / "docs" / "legacy-trojan-topology.md",
}


class DocumentationCoverageTests(unittest.TestCase):
    """Task 13 coverage: the active manual documentation must stay complete."""

    @classmethod
    def setUpClass(cls):
        cls.texts = {
            name: path.read_text(encoding="utf-8")
            for name, path in DOCUMENTATION_PATHS.items()
        }

    def test_all_documented_files_exist(self):
        for name, path in DOCUMENTATION_PATHS.items():
            self.assertTrue(path.is_file(), name)

    def test_deployment_documents_host_constraints_and_idle_processes(self):
        deployment = self.texts["deployment"]
        for phrase in ("512 MiB", "256 MiB Swap", "10 GiB", "常驻进程只有"):
            self.assertIn(phrase, deployment)

    def test_deployment_documents_port_plan_and_reality_boundaries(self):
        deployment = self.texts["deployment"]
        for phrase in ("TCP 443", "REALITY", "8443", "不开放 UDP 443", "不使用公网 1443"):
            self.assertIn(phrase, deployment)

    def test_xui_setup_documents_manual_pinned_installation(self):
        xui = self.texts["xui-setup"]
        for phrase in ("3.6.0", "26.6.27", "人工", "bash /tmp/3x-ui-install"):
            self.assertIn(phrase, xui)

    def test_xui_setup_documents_loopback_listeners_and_readonly_sqlite(self):
        xui = self.texts["xui-setup"]
        for phrase in ("127.0.0.1", "Clash 输出", "x-ui.db", "只读"):
            self.assertIn(phrase, xui)

    def test_deployment_documents_python_runtime_and_mihomo_checksum(self):
        deployment = self.texts["deployment"]
        for phrase in (
            "python3 -m venv",
            "Jinja2==3.1.6",
            "PyYAML==6.0.3",
            "1.19.30",
            "sha256",
            "install -d -o root -g www-data -m 2750 /var/lib/clash-sub/public",
            "0700",
            "0600",
            "0640",
        ):
            self.assertIn(phrase, deployment)

    def test_deployment_documents_acme_sh_certificate_lifecycle(self):
        deployment = self.texts["deployment"]
        for phrase in ("acme.sh", "--install-cert", "SAN", "systemctl reload nginx"):
            self.assertIn(phrase, deployment)

    def test_deployment_documents_nginx_timer_and_first_sync_lifecycle(self):
        deployment = self.texts["deployment"]
        for phrase in (
            "clash-sub.conf.tmpl",
            "nginx -t",
            "clash-sub-traffic.timer",
            "systemctl enable --now clash-sub-traffic.timer",
            "首次同步",
            "clash-sub sync",
            "clash-sub links",
        ):
            self.assertIn(phrase, deployment)

    def test_operations_documents_mobile_airport_update_and_commands(self):
        operations = self.texts["operations"]
        for phrase in (
            "手机",
            "隐藏输入",
            "仅接受 https://",
            "clash-sub status",
            "clash-sub history",
            "clash-sub rollback",
            "clash-sub rotate-link",
            "clash-sub traffic-update",
        ):
            self.assertIn(phrase, operations)

    def test_operations_documents_xui_upgrade_procedure(self):
        operations = self.texts["operations"]
        for phrase in (
            "备份",
            "停止",
            "副本",
            "clash-sub sync",
            "旧 YAML",
        ):
            self.assertIn(phrase, operations)

    def test_operations_documents_replacement_checklists(self):
        operations = self.texts["operations"]
        for phrase in ("更换域名", "更换 VPS", "不依赖 Nginx 证书"):
            self.assertIn(phrase, operations)

    def test_readme_documents_non_goals_and_menu_management(self):
        readme = self.texts["readme"]
        for phrase in (
            "更新机场订阅",
            "同步所有配置",
            "不提供短链",
            "实时查询",
            "Telegram",
            "不需要记住 refresh",
        ):
            self.assertIn(phrase, readme)

    def test_active_docs_never_reference_removed_tooling(self):
        removed = ("server_preflight", "install-server", "install_server", "certbot")
        for name in DOCUMENTATION_PATHS:
            if name == "legacy-topology":
                continue
            for phrase in removed:
                self.assertNotIn(phrase, self.texts[name], "%s: %s" % (name, phrase))


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

    def test_panel_base_path_has_an_exact_redirect_and_boundary_safe_child_proxy(self):
        panel = _server_with_name(self.servers, "panel.{{DOMAIN}}")
        rendered = panel.replace("{{PANEL_BASE_PATH}}", "/secret").replace(
            "{{PANEL_UPSTREAM}}", "127.0.0.1:2053"
        )
        locations = _blocks(rendered, "location")
        self.assertEqual(
            [_location_header(location) for location in locations],
            ["location = /secret", "location ^~ /secret/", "location /"],
        )
        exact = _selected_location(locations, "/secret")
        child = _selected_location(locations, "/secret/assets/app.js")
        collision = _selected_location(locations, "/secret-extra")
        self.assertIn("return 308 /secret/;", exact)
        self.assertNotIn("proxy_pass", exact)
        self.assertEqual(_directive(child, "proxy_pass"), ["http://127.0.0.1:2053/secret/"])
        self.assertIn("return 404;", collision)
        self.assertNotIn("proxy_pass", collision)
        self.assertIn("validated loopback", self.template)
        self.assertIn(
            "PANEL_BASE_PATH contract: leading slash, one safe random component ([A-Za-z0-9_-]+), no trailing slash.",
            self.template,
        )

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

    def test_included_task7_exact_routes_precede_the_subscription_prefix_fallback(self):
        subscription = _server_with_name(self.servers, "sub.{{DOMAIN}}")
        self.assertIn("include {{ROUTES_INCLUDE}};", subscription)
        self.assertIn(
            "Task 7 generated routes use exact-match locations, so they take precedence over the /s/ fallback.",
            self.template,
        )
        with_generated_route = subscription.replace(
            "include {{ROUTES_INCLUDE}};",
            "location = /s/example-token/clash-standard.yaml {\n    return 200;\n}",
        )
        selected = _selected_location(
            _blocks(with_generated_route, "location"),
            "/s/example-token/clash-standard.yaml",
        )
        self.assertEqual(
            _location_header(selected),
            "location = /s/example-token/clash-standard.yaml",
        )

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
            "ReadWritePaths": (
                "/var/lib/clash-sub/private /var/lib/clash-sub/public /etc/nginx/clash-sub "
                "/var/log/nginx /var/lib/nginx"
            ),
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
