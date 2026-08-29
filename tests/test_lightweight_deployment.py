import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
NGINX_STREAM_TEMPLATE = ROOT / "templates" / "nginx" / "stream.conf.j2"
NGINX_SUB_TEMPLATE = ROOT / "templates" / "nginx" / "sub-server.conf.j2"
INSTALL_SH = ROOT / "install.sh"
TRAFFIC_SERVICE = ROOT / "deploy" / "systemd" / "clash-sub-traffic.service"
TRAFFIC_TIMER = ROOT / "deploy" / "systemd" / "clash-sub-traffic.timer"
RECOVERY_SERVICE = ROOT / "deploy" / "systemd" / "clash-sub-recover.service"
RECOVERY_DROP_IN = ROOT / "deploy" / "systemd" / "nginx.service.d" / "clash-sub-recover.conf"
REQUIREMENTS = ROOT / "requirements.txt"


def _unit_value(text, key):
    return re.findall(r"(?m)^%s=(.+)$" % re.escape(key), text)


def _fenced_blocks(text, languages):
    blocks = []
    delimiter = None
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if delimiter is None:
            opening = re.fullmatch(r"([~\x60]{3,})([A-Za-z0-9_-]*)", stripped)
            if opening and opening.group(2) in languages:
                delimiter = opening.group(1)
                lines = []
        elif stripped == delimiter:
            blocks.append("\n".join(lines))
            delimiter = None
        else:
            lines.append(line)
    if delimiter is not None:
        raise AssertionError("unterminated fenced block")
    return blocks


def _activation_candidate_inventory_block(text):
    blocks = [
        block
        for block in _fenced_blocks(text, {"bash"})
        if '[[ "$candidate" =~' in block and "activation-journal" in block
    ]
    if len(blocks) != 1:
        raise AssertionError("expected exactly one activation candidate inventory block")
    return blocks[0]


def _activation_candidate_inventory_paths(text, private_root, current_root, nginx_root):
    block = _activation_candidate_inventory_block(text)
    block = block.replace("/var/lib/clash-sub/private/current", str(current_root))
    block = block.replace("/var/lib/clash-sub/private", str(private_root))
    block = block.replace("/etc/nginx/clash-sub", str(nginx_root))
    result = subprocess.run(
        ["bash", "-c", block],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode, tuple(Path(line) for line in result.stdout.splitlines() if line)


DOCUMENTATION_PATHS = {
    "readme": ROOT / "README.md",
    "deployment": ROOT / "DEPLOYMENT.md",
    "xui-setup": ROOT / "docs" / "3x-ui-setup.md",
    "operations": ROOT / "docs" / "operations.md",
    "private-data": ROOT / "docs" / "private-data.md",
    "recovery": ROOT / "docs" / "recovery.md",
}


RETIRED_DOCUMENTATION_PATHS = (
    "docs/dns-design.md",
    "docs/legacy-trojan-topology.md",
    "docs/superpowers/plans/2026-08-21-clash-subscription-publication.md",
    "docs/superpowers/plans/2026-08-23-clash-sub-lightweight.md",
    "docs/superpowers/plans/2026-08-25-clash-sub-integration.md",
    "docs/superpowers/plans/2026-08-28-private-home-overlay-upload.md",
    "docs/superpowers/specs/2026-08-21-clash-subscription-publication-design.md",
    "docs/superpowers/specs/2026-08-23-clash-sub-lightweight-redesign.md",
    "docs/superpowers/specs/2026-08-25-clash-sub-integration-design.md",
    "docs/superpowers/specs/2026-08-27-local-template-workbench-design.md",
    "docs/superpowers/specs/2026-08-28-private-home-overlay-upload-design.md",
    "docs/superpowers/specs/2026-08-28-stable-amytelecom-provider-design.md",
)


class DocumentationContractTests(unittest.TestCase):
    """The six concise user documents describe only the current workflow."""

    @classmethod
    def setUpClass(cls):
        cls.texts = {
            name: path.read_text(encoding="utf-8")
            for name, path in DOCUMENTATION_PATHS.items()
        }

    def test_all_documented_files_exist(self):
        for name, path in DOCUMENTATION_PATHS.items():
            self.assertTrue(path.is_file(), name)

    def test_readme_documents_authorization_and_release_filenames(self):
        readme = self.texts["readme"]
        for phrase in (
            "owner | `compat-office`",
            "owner | `compat-universal`",
            "owner | `balance-office`",
            "member | `compat-universal`",
            "clash-compat-office.yaml",
            "clash-compat-universal.yaml",
            "clash-balance-office.yaml",
            "./bin/clash-sub template-sync",
        ):
            self.assertIn(phrase, readme)

    def test_operations_documents_iCloud_sources_single_source_updates_and_report(self):
        operations = self.texts["operations"]
        for phrase in (
            "Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents",
            "Compat-Office.yaml",
            "Balance-Office.yaml",
            "--compat-office",
            "--balance-office",
            "change report",
            "不显示家庭内容或动态节点",
            "未来的服务器上传",
        ):
            self.assertIn(phrase, operations)

    def test_documents_explain_comments_private_modes_and_deferred_privacy(self):
        self.assertIn("Compat 公共注释", self.texts["operations"])
        self.assertIn("Balance 的完整 `dns`", self.texts["operations"])
        private_data = self.texts["private-data"]
        for phrase in ("Git 忽略", "0600", "private/home.yaml"):
            self.assertIn(phrase, private_data)
        self.assertIn("privacy", self.texts["readme"])
        self.assertIn("not included", self.texts["readme"])

    def test_retired_documents_and_old_release_aliases_are_absent(self):
        for relative in RETIRED_DOCUMENTATION_PATHS:
            self.assertFalse((ROOT / relative).exists(), relative)
        banned = (
            "clash-" + "balanced.yaml",
            "clash-" + "standard.yaml",
            "clash-" + "privacy.yaml",
            "private/workbench/" + "balanced.yaml",
        )
        for name, text in self.texts.items():
            for phrase in banned:
                self.assertNotIn(phrase, text, "%s: %s" % (name, phrase))


class LightweightDeploymentTests(unittest.TestCase):
    def test_stream_template_routes_default_to_reality(self):
        text = NGINX_STREAM_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("ssl_preread on;", text)
        self.assertIn("127.0.0.1:10443", text)
        self.assertIn("127.0.0.1:30443", text)
        self.assertIn("127.0.0.1:20443", text)
        self.assertIn("{{ domain }}", text)
        self.assertNotIn("proxy_protocol", text.split("server {")[1].split("}")[0])

    def test_sub_server_template_binds_loopback_and_includes_routes(self):
        text = NGINX_SUB_TEMPLATE.read_text(encoding="utf-8")

        self.assertIn("listen 127.0.0.1:30443 ssl;", text)
        self.assertIn("{{ routes_include }}", text)
        self.assertIn("{{ panel_base_path }}", text)
        self.assertIn("{{ panel_port }}", text)
        self.assertIn("limit_req_zone $binary_remote_addr zone=clash_subscription", text)

    def test_install_sh_bootstraps_venv_and_executes_install(self):
        text = INSTALL_SH.read_text(encoding="utf-8")

        self.assertIn("python3 -m venv", text)
        self.assertIn("clash-sub install", text)
        self.assertIn("python3-venv", text)
        self.assertIn("git", text)

    def test_deployment_prerequisites_install_git_before_clone(self):
        deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
        self.assertIn("apt-get install -y git", deployment)

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

    def test_recovery_oneshot_runs_before_nginx_without_adding_a_resident_service(self):
        service = RECOVERY_SERVICE.read_text(encoding="utf-8")
        drop_in = RECOVERY_DROP_IN.read_text(encoding="utf-8")
        self.assertEqual(_unit_value(service, "Type"), ["oneshot"])
        self.assertEqual(_unit_value(service, "ExecStart"), ["/usr/local/bin/clash-sub recover"])
        self.assertEqual(_unit_value(service, "User"), ["root"])
        self.assertIn("Before=nginx.service", service)
        self.assertIn("Requires=clash-sub-recover.service", drop_in)
        self.assertIn("After=clash-sub-recover.service", drop_in)
        self.assertNotIn("[Install]", service)

    def test_requirements_are_the_approved_pins(self):
        self.assertEqual(
            REQUIREMENTS.read_text(encoding="utf-8").splitlines(),
            ["Jinja2==3.1.6", "PyYAML==6.0.3", "ruamel.yaml==0.19.1"],
        )


if __name__ == "__main__":
    unittest.main()
