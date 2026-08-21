"""Read-only clean-host preflight tests.

`scripts/server_preflight.py` inspects an already-configured 3x-ui
REALITY host through an injected `CommandRunner` that can only answer
bounded read commands, file reads, stats, DNS lookups, and one
environment variable.  The fixtures under tests/fixtures/ replay
synthetic runner outputs for a clean host and for a host still
carrying a legacy Trojan stack; every test asserts that reports stay
limited to booleans, versions, counts, ports, and stable codes.
"""

import copy
import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

from clash_sub.models import (
    CertificateSettings,
    PublicationSettings,
    RealitySettings,
    ServiceSettings,
    XuiSettings,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def load_script(name):
    module_name = "scripts_%s" % name
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "scripts" / ("%s.py" % name)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


server_preflight = load_script("server_preflight")

run_preflight = server_preflight.run_preflight
FixtureRunner = server_preflight.FixtureRunner


def load_fixture(name):
    document = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("fixture %s must be an object" % name)
    return document


def build_settings(mode="domain"):
    authority = "192.0.2.10:8443" if mode == "ip" else None
    return ServiceSettings(
        private_root=Path("/app/private"),
        converter_base_url="http://127.0.0.1:25500",
        publication=PublicationSettings(
            mode=mode,
            subscription_authority=authority or "sub.example.com:8443",
            panel_authority=authority or "panel.example.com:8443",
            publisher_listen="127.0.0.1",
            publisher_port=25501,
        ),
        reality=RealitySettings(
            public_address="192.0.2.10",
            public_port=443,
            required_flow="xtls-rprx-vision",
        ),
        xui=XuiSettings(
            panel_listen="127.0.0.1",
            panel_port=2053,
            panel_base_path="/secret-panel-path/",
            subscription_listen="127.0.0.1",
            subscription_port=2096,
            xray_config_path=Path("/usr/local/x-ui/bin/config.json"),
            xray_binary_path=Path("/usr/local/x-ui/bin/xray-linux-amd64"),
            expected_panel_version="3.6.0",
            expected_xray_version="26.6.27",
        ),
        certificate=CertificateSettings(
            fullchain_path=Path("/etc/letsencrypt/live/panel.example.com/fullchain.pem"),
            acme_email="admin@example.com",
            alert_before_seconds=1209600,
            alert_command=(),
        ),
    )


UFW_ACTIVE_UNKNOWN_RULES = (
    "Status: active\n"
    "\n"
    "To                         Action      From\n"
    "--                         ------      ----\n"
    "[ 1] 8080/tcp                  ALLOW IN    Anywhere\n"
)

UFW_ACTIVE_EXPECTED_RULES = (
    "Status: active\n"
    "\n"
    "To                         Action      From\n"
    "--                         ------      ----\n"
    "[ 1] 22/tcp                    ALLOW IN    Anywhere\n"
    "[ 2] 80/tcp                    ALLOW IN    Anywhere\n"
    "[ 3] 443                       ALLOW IN    Anywhere\n"
    "[ 4] 8443/tcp                  ALLOW IN    Anywhere\n"
)

# An "Anywhere" To column allows every port for one source; skipping
# that row would let an allow-all rule pass as clean.
UFW_ANYWHERE_TO_RULES = (
    "Status: active\n"
    "\n"
    "To                         Action      From\n"
    "--                         ------      ----\n"
    "[ 1] Anywhere                 ALLOW IN    10.0.0.2\n"
)

# Inline comments must not create or hide listeners.
NGINX_PROJECT_COMMENT_DUMP = (
    "# configuration file /etc/nginx/conf.d/clash-sub-http.conf:\n"
    "server {\n"
    "    listen 80; # used to be listen 443 ssl\n"
    "}\n"
)

NGINX_443_DUMP = (
    "# configuration file /etc/nginx/sites-enabled/default:\n"
    "server {\n"
    "    listen 443 ssl;\n"
    "}\n"
)

NGINX_UNMANAGED_80_DUMP = (
    "# configuration file /etc/nginx/sites-enabled/default:\n"
    "server {\n"
    "    listen 80;\n"
    "}\n"
)

NGINX_PROJECT_DUMP = (
    "# configuration file /etc/nginx/conf.d/clash-sub-http.conf:\n"
    "server {\n"
    "    listen 80;\n"
    "}\n"
)


class ServerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.settings = build_settings()
        self.clean = load_fixture("preflight-clean.json")
        self.legacy = load_fixture("preflight-legacy-trojan.json")

    def make_runner(self, fixture):
        return FixtureRunner(
            copy.deepcopy(fixture),
            ROOT,
            xray_config_path=str(self.settings.xui.xray_config_path),
        )

    def clean_runner(self):
        return self.make_runner(self.clean)

    def legacy_trojan_runner(self):
        return self.make_runner(self.legacy)

    def report_for(self, **updates):
        fixture = copy.deepcopy(self.clean)
        fixture.update(updates)
        return run_preflight(self.make_runner(fixture), self.settings)

    def mutate_xray_inbound(self, stream_changes=None, client_changes=None):
        fixture = copy.deepcopy(self.clean)
        inbound = fixture["xray_config"]["inbounds"][0]
        for key, value in (stream_changes or {}).items():
            inbound["streamSettings"][key] = value
        for index, changes in enumerate(client_changes or []):
            for key, value in changes.items():
                inbound["settings"]["clients"][index][key] = value
        return run_preflight(self.make_runner(fixture), self.settings)

    def test_clean_expected_host_passes_with_redacted_summary(self):
        report = run_preflight(self.clean_runner(), self.settings)
        self.assertTrue(report.ok, report.blocking_codes)
        serialized = json.dumps(report.to_json())
        self.assertNotIn("00000000-0000-4000-8000-000000000001", serialized)
        self.assertNotIn("private-key", serialized)
        self.assertNotIn("/secret-panel-path/", serialized)

    def test_clean_summary_contains_only_allowlisted_fields(self):
        report = run_preflight(self.clean_runner(), self.settings)
        payload = report.to_json()
        self.assertEqual(
            set(payload), {"ok", "checks", "facts", "blocking_codes", "notes"}
        )
        self.assertEqual(set(payload["checks"]), {name for name, _ in server_preflight.CHECK_SPEC})
        self.assertEqual(payload["blocking_codes"], [])
        for value in payload["facts"].values():
            # Booleans, versions, counts, ports (int or list of int),
            # and nothing else.
            self.assertTrue(
                isinstance(value, (bool, int, str, type(None)))
                or (
                    isinstance(value, list)
                    and value
                    and all(isinstance(item, int) for item in value)
                ),
                value,
            )

    def test_reports_exclude_addresses_names_urls_keys_and_node_names(self):
        for runner in (self.clean_runner(), self.legacy_trojan_runner()):
            serialized = json.dumps(run_preflight(runner, self.settings).to_json())
            for forbidden in (
                "192.0.2.10",
                "198.51.100.99",
                "www.example.com",
                "sub.example.com",
                "panel.example.com",
                "00000000-0000-4000-8000-000000000001",
                "00000000-0000-4000-8000-000000000002",
                "private-key",
                "privateKey",
                "SYNTHETIC",
                "/secret-panel-path/",
                "/usr/local/x-ui/bin/config.json",
                "xray-linux-amd64",
                "client-owner",
                "client-member",
            ):
                self.assertNotIn(forbidden, serialized)

    def test_legacy_trojan_or_unknown_443_owner_blocks_apply(self):
        report = run_preflight(self.legacy_trojan_runner(), self.settings)
        self.assertFalse(report.ok)
        self.assertIn("legacy_service_present", report.blocking_codes)

    def test_preflight_never_executes_mutating_commands(self):
        runner = self.clean_runner()
        run_preflight(runner, self.settings)
        self.assertFalse(runner.mutating_command_seen)

    def test_command_classifier_rejects_mutations_and_allows_reads(self):
        for argv in (
            ["systemctl", "restart", "x-ui"],
            ["systemctl", "stop", "docker"],
            ["systemctl", "enable", "nginx"],
            ["ufw", "allow", "443/tcp"],
            ["ufw", "enable"],
            ["apt", "install", "nginx"],
            ["rm", "-rf", "/"],
            ["mv", "/a", "/b"],
            ["chmod", "600", "/etc/passwd"],
            ["chown", "0:0", "/x"],
            ["install", "-d", "/x"],
            ["bash", "/tmp/install.sh"],
            ["curl", "http://example.invalid/"],
            ["/bin/dd", "if=/dev/zero"],
            ["echo", "x"],
        ):
            self.assertTrue(server_preflight.is_mutating_command(argv), argv)
        for argv in (
            ["uname", "-m"],
            ["ss", "-H", "-lntup"],
            ["systemctl", "is-active", "x-ui"],
            ["systemctl", "show", "x-ui", "--property=Version"],
            ["ufw", "status", "numbered"],
            ["docker", "--version"],
            ["docker", "compose", "version"],
            ["docker", "compose", "config", "--format", "json"],
            ["nginx", "-T"],
            ["x-ui", "--version"],
            ["/usr/local/x-ui/bin/xray-linux-amd64", "version"],
        ):
            self.assertFalse(server_preflight.is_mutating_command(argv), argv)

    def test_unsupported_os_or_architecture_blocks_apply(self):
        report = self.report_for(os_release='ID=ubuntu\nVERSION_ID="24.04"\n')
        self.assertFalse(report.ok)
        self.assertIn("os_unsupported", report.blocking_codes)
        report = self.report_for(arch="aarch64\n")
        self.assertIn("os_unsupported", report.blocking_codes)

    def test_non_root_execution_is_reported_without_blocking(self):
        report = self.report_for(euid=1000)
        self.assertTrue(report.ok, report.blocking_codes)
        self.assertIn("non_root_execution", report.notes)
        self.assertFalse(report.facts["running_as_root"])

    def test_xui_service_inactive_blocks_apply(self):
        services = dict(self.clean["services"], **{"x-ui": "inactive"})
        report = self.report_for(services=services)
        self.assertIn("xui_service_inactive", report.blocking_codes)

    def test_panel_version_mismatch_blocks_apply(self):
        report = self.report_for(panel_version_output="x-ui version 3.5.0\n")
        self.assertFalse(report.ok)
        self.assertIn("panel_version_mismatch", report.blocking_codes)
        self.assertEqual(report.facts["panel_version"], "3.5.0")

    def test_xray_version_mismatch_blocks_apply(self):
        report = self.report_for(xray_version_output="Xray 25.1.30 (Xray, Penetrates Everything.)\n")
        self.assertIn("xray_version_mismatch", report.blocking_codes)

    def test_tcp_443_with_unknown_owner_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"] = [
            line.replace('users:(("xray",pid=1234,fd=7))', 'users:(("unknown",pid=1234,fd=7))')
            for line in fixture["listeners"]
        ]
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("tcp_443_not_xray", report.blocking_codes)

    def test_second_tcp_443_listener_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"].append(
            'tcp   LISTEN 0      511          192.0.2.10:443     0.0.0.0:*    '
            'users:(("xray",pid=1234,fd=8))'
        )
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("tcp_443_not_xray", report.blocking_codes)

    def test_udp_443_listener_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"].append(
            "udp   UNCONN 0      0            0.0.0.0:443        0.0.0.0:*"
        )
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("udp_443_open", report.blocking_codes)

    def test_udp6_443_listener_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"].append(
            "udp6  UNCONN 0      0            [::]:443           [::]:*      "
            'users:(("hysteria",pid=2100,fd=9))'
        )
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("udp_443_open", report.blocking_codes)

    def test_tcp6_wildcard_443_owned_by_xray_passes(self):
        # A dual-stack Go listener shows up as a single tcp6 [::]:443 line.
        report = run_preflight(self.clean_runner(), self.settings)
        self.assertTrue(report.ok, report.blocking_codes)
        self.assertTrue(report.checks["tcp_443_xray_owned"])
        self.assertIn(443, report.facts["public_listener_ports"])

    def test_tcp6_unexpected_public_listener_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"].append(
            "tcp6  LISTEN 0      511          [::]:8080          [::]:*      "
            'users:(("legacy6",pid=2101,fd=4))'
        )
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("unexpected_public_listener", report.blocking_codes)

    def test_tcp_443_process_name_lookalike_is_not_xray(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"] = [
            line.replace('users:(("xray",pid=1234,fd=7))', 'users:(("notxray",pid=1234,fd=7))')
            for line in fixture["listeners"]
        ]
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("tcp_443_not_xray", report.blocking_codes)

    def test_panel_listener_not_loopback_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"] = [
            line.replace("127.0.0.1:2053", "0.0.0.0:2053") for line in fixture["listeners"]
        ]
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("panel_not_loopback", report.blocking_codes)

    def test_subscription_listener_not_loopback_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"] = [
            line.replace("127.0.0.1:2096", "0.0.0.0:2096") for line in fixture["listeners"]
        ]
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("subscription_not_loopback", report.blocking_codes)

    def test_missing_listener_on_a_configured_port_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"] = [
            line for line in fixture["listeners"] if ":2096" not in line
        ]
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("subscription_not_loopback", report.blocking_codes)

    def test_inbound_without_reality_security_blocks_apply(self):
        report = self.mutate_xray_inbound(stream_changes={"security": "tls"})
        self.assertIn("reality_inbound_missing", report.blocking_codes)

    def test_inbound_with_wrong_network_or_port_blocks_apply(self):
        report = self.mutate_xray_inbound(stream_changes={"network": "ws"})
        self.assertIn("reality_inbound_missing", report.blocking_codes)
        fixture = copy.deepcopy(self.clean)
        fixture["xray_config"]["inbounds"][0]["port"] = 8443
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("reality_inbound_missing", report.blocking_codes)

    def test_inbound_with_wrong_protocol_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["xray_config"]["inbounds"][0]["protocol"] = "trojan"
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("reality_inbound_missing", report.blocking_codes)

    def test_unreadable_xray_config_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["xray_config"] = None
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("reality_inbound_missing", report.blocking_codes)
        self.assertEqual(report.facts["reality_inbound_count"], 0)

    def test_no_clients_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["xray_config"]["inbounds"][0]["settings"]["clients"] = []
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("clients_missing", report.blocking_codes)

    def test_wrong_or_mixed_client_flow_blocks_apply(self):
        report = self.mutate_xray_inbound(client_changes=[{"flow": "xtls-rprx-direct"}])
        self.assertIn("client_flow_inconsistent", report.blocking_codes)
        report = self.mutate_xray_inbound(client_changes=[{}, {"flow": ""}])
        self.assertIn("client_flow_inconsistent", report.blocking_codes)

    def test_empty_short_ids_block_apply(self):
        fixture = copy.deepcopy(self.clean)
        reality = fixture["xray_config"]["inbounds"][0]["streamSettings"]["realitySettings"]
        reality["shortIds"] = ["", ""]
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("short_ids_empty", report.blocking_codes)

    def test_empty_server_names_block_apply(self):
        fixture = copy.deepcopy(self.clean)
        reality = fixture["xray_config"]["inbounds"][0]["streamSettings"]["realitySettings"]
        reality["serverNames"] = []
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("server_names_empty", report.blocking_codes)

    def test_unexpected_public_listener_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["listeners"].append(
            'tcp   LISTEN 0      511          0.0.0.0:8080        0.0.0.0:*    '
            'users:(("legacy",pid=2000,fd=9))'
        )
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("unexpected_public_listener", report.blocking_codes)

    def test_absent_nginx_is_acceptable_before_installation(self):
        report = run_preflight(self.clean_runner(), self.settings)
        self.assertTrue(report.ok, report.blocking_codes)
        self.assertIn("nginx_absent", report.notes)
        self.assertEqual(report.facts["nginx_state"], "absent")

    def test_nginx_with_443_listener_conflicts(self):
        report = self.report_for(nginx_dump=NGINX_443_DUMP)
        self.assertIn("nginx_conflict", report.blocking_codes)
        self.assertEqual(report.facts["nginx_state"], "conflict")

    def test_nginx_with_unmanaged_80_listener_conflicts(self):
        report = self.report_for(nginx_dump=NGINX_UNMANAGED_80_DUMP)
        self.assertIn("nginx_conflict", report.blocking_codes)

    def test_nginx_with_project_owned_80_listener_is_accepted(self):
        report = self.report_for(nginx_dump=NGINX_PROJECT_DUMP)
        self.assertTrue(report.ok, report.blocking_codes)
        self.assertEqual(report.facts["nginx_state"], "present")

    def test_nginx_inline_comments_are_ignored_when_scanning(self):
        report = self.report_for(nginx_dump=NGINX_PROJECT_COMMENT_DUMP)
        self.assertTrue(report.ok, report.blocking_codes)
        self.assertEqual(report.facts["nginx_state"], "present")

    def test_stale_trojan_web_service_blocks_apply(self):
        services = dict(self.clean["services"], **{"trojan-web": "active"})
        report = self.report_for(services=services)
        self.assertIn("legacy_service_present", report.blocking_codes)

    def test_docker_unavailability_blocks_apply(self):
        report = self.report_for(docker_version_output=None)
        self.assertIn("docker_unavailable", report.blocking_codes)

    def test_compose_unavailability_blocks_apply(self):
        report = self.report_for(compose_version_output=None)
        self.assertIn("compose_unavailable", report.blocking_codes)

    def test_compose_config_with_wrong_services_blocks_apply(self):
        report = self.report_for(compose_config_output='{"services": {"legacy": {}}}')
        self.assertIn("compose_config_invalid", report.blocking_codes)

    def test_ssh_port_is_reported_as_a_number_only(self):
        report = run_preflight(self.clean_runner(), self.settings)
        self.assertEqual(report.facts["ssh_port"], 22)
        report = self.report_for(environment={})
        self.assertIsNone(report.facts["ssh_port"])

    def test_ufw_inactive_is_noted_without_blocking(self):
        report = run_preflight(self.clean_runner(), self.settings)
        self.assertTrue(report.ok, report.blocking_codes)
        self.assertIn("ufw_inactive", report.notes)

    def test_ufw_active_with_unknown_rule_blocks_apply(self):
        report = self.report_for(ufw_status_output=UFW_ACTIVE_UNKNOWN_RULES)
        self.assertIn("ufw_unsafe", report.blocking_codes)

    def test_ufw_anywhere_to_column_rule_blocks_apply(self):
        report = self.report_for(ufw_status_output=UFW_ANYWHERE_TO_RULES)
        self.assertIn("ufw_unsafe", report.blocking_codes)

    def test_ufw_active_with_expected_ports_is_clean(self):
        report = self.report_for(ufw_status_output=UFW_ACTIVE_EXPECTED_RULES)
        self.assertTrue(report.ok, report.blocking_codes)
        self.assertIn("ufw_active_clean", report.notes)

    def test_ufw_absent_is_noted_without_blocking(self):
        report = self.report_for(ufw_status_output=None)
        self.assertTrue(report.ok, report.blocking_codes)
        self.assertIn("ufw_absent", report.notes)

    def test_domain_dns_mismatch_blocks_apply(self):
        dns = dict(
            self.clean["dns"],
            **{"sub.example.com": ["192.0.2.99"]},
        )
        report = self.report_for(dns=dns)
        self.assertIn("dns_mismatch", report.blocking_codes)

    def test_ip_mode_skips_the_dns_check_with_a_note(self):
        settings = build_settings(mode="ip")
        report = run_preflight(self.clean_runner(), settings)
        self.assertTrue(report.ok, report.blocking_codes)
        self.assertIn("ip_mode_no_dns_check", report.notes)
        self.assertTrue(report.checks["dns_matches"])

    def test_private_root_wrong_owner_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["paths"]["private/config"]["uid"] = 0
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("private_root_ownership", report.blocking_codes)

    def test_private_root_wrong_directory_mode_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["paths"]["private/staging"]["mode"] = 0o755
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("private_root_ownership", report.blocking_codes)

    def test_private_root_missing_directory_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        del fixture["paths"]["private/sources"]
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("private_root_ownership", report.blocking_codes)

    def test_private_config_file_mode_blocks_apply(self):
        fixture = copy.deepcopy(self.clean)
        fixture["paths"]["private/config/service.yaml"]["mode"] = 0o644
        report = run_preflight(self.make_runner(fixture), self.settings)
        self.assertIn("private_root_ownership", report.blocking_codes)


class XrayConfigParsingTests(unittest.TestCase):
    def load_xray_fixture(self):
        document = json.loads(
            (FIXTURES / "xray-reality-config.json").read_text(encoding="utf-8")
        )
        self.assertIsInstance(document, dict)
        return document

    def test_synthetic_config_summarizes_one_reality_inbound(self):
        summary = server_preflight.summarize_xray_config(
            self.load_xray_fixture(),
            required_flow="xtls-rprx-vision",
            public_port=443,
        )
        self.assertEqual(summary.reality_inbound_count, 1)
        self.assertEqual(summary.client_count, 2)
        self.assertTrue(summary.flow_consistent)
        self.assertTrue(summary.server_names_nonempty)
        self.assertTrue(summary.short_ids_nonempty)

    def test_summary_never_exposes_raw_config_values(self):
        summary = server_preflight.summarize_xray_config(
            self.load_xray_fixture(),
            required_flow="xtls-rprx-vision",
            public_port=443,
        )
        serialized = json.dumps(summary.__dict__)
        for forbidden in (
            "00000000-0000-4000-8000-000000000001",
            "private-key",
            "SYNTHETIC",
            "www.example.com",
        ):
            self.assertNotIn(forbidden, serialized)


class PreflightCliTests(unittest.TestCase):
    def run_script(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "server_preflight.py"),
            ]
            + list(arguments),
            capture_output=True,
            text=True,
            cwd=ROOT,
            check=False,
        )

    def test_fixture_mode_reports_json_with_clean_exit_code(self):
        result = self.run_script(
            "--fixture", str(FIXTURES / "preflight-clean.json"), "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["blocking_codes"], [])
        self.assertEqual(payload["facts"]["panel_version"], "3.6.0")

    def test_fixture_mode_blocks_legacy_host_with_exit_code_one(self):
        result = self.run_script(
            "--fixture", str(FIXTURES / "preflight-legacy-trojan.json"), "--json"
        )
        self.assertEqual(result.returncode, 1)
        payload = json.loads(result.stdout)
        self.assertFalse(payload["ok"])
        self.assertIn("legacy_service_present", payload["blocking_codes"])


if __name__ == "__main__":
    unittest.main()
