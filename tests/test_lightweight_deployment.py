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
    "legacy-topology": ROOT / "docs" / "legacy-trojan-topology.md",
}


class DocumentationCoverageTests(unittest.TestCase):
    """Coverage: the active documentation must stay complete."""

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

    def test_deployment_documents_unified_443_port_plan(self):
        deployment = self.texts["deployment"]
        for phrase in ("TCP 443", "10443", "30443", "20443", "不开放 UDP 443", "不使用公网 1443"):
            self.assertIn(phrase, deployment)

    def test_xui_setup_documents_loopback_listeners_and_readonly_sqlite(self):
        xui = self.texts["xui-setup"]
        for phrase in ("127.0.0.1", "Clash 输出", "x-ui.db", "只读"):
            self.assertIn(phrase, xui)

    def test_active_shell_examples_are_safe_to_copy_without_ripgrep(self):
        for name in ("deployment", "xui-setup", "operations"):
            with self.subTest(document=name):
                blocks = _fenced_blocks(self.texts[name], {"bash", "sh", "shell"})
                self.assertTrue(blocks)
                for block in blocks:
                    self.assertNotRegex(block, r"<[^>\n]+>")
                    self.assertNotRegex(block, r"\brg\b")

    def test_deployment_documents_installer_lifecycle_and_first_sync(self):
        deployment = self.texts["deployment"]
        for phrase in (
            "bash install.sh",
            "nginx -t",
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

    def test_operations_documents_pinned_acme_maintenance(self):
        operations = self.texts["operations"]
        for phrase in (
            "acme.sh 3.1.4",
            "每季度",
            "SHA-256",
            "--auto-upgrade",
            "证书自动续期",
        ):
            self.assertIn(phrase, operations)

    def test_activation_candidate_inventory_is_strict_and_read_only(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            private_root = root / "private"
            current_root = private_root / "current"
            nginx_root = root / "nginx"
            current_root.mkdir(parents=True)
            nginx_root.mkdir()
            candidates = (
                private_root / ".state.json.a1b2c3d4",
                private_root / "..activation-journal.json.i9j0k1l2",
                current_root / ".7.m3n4o5p6",
                nginx_root / ".routes.conf.q7r8s9t0",
            )
            decoys = (
                private_root / ".status.json.a1b2c3d4",
                private_root / ".state.json.bad-suffix!",
                current_root / ".0.m3n4o5p6",
                nginx_root / ".routes.conf.bad-suffix!",
            )
            for path in candidates + decoys:
                path.write_bytes(b"fixture\n")

            status, listed = _activation_candidate_inventory_paths(
                self.texts["operations"], private_root, current_root, nginx_root
            )

            self.assertEqual(status, 0)
            self.assertEqual(set(listed), set(candidates))
            self.assertTrue(all(path.exists() for path in candidates + decoys))

    def test_operations_documents_replacement_checklists(self):
        operations = self.texts["operations"]
        for phrase in ("更换域名", "更换 VPS", "recovery.md", "install-state.json"):
            self.assertIn(phrase, operations)

    def test_recovery_documents_backup_scope_and_reserved_trojan_caveat(self):
        recovery = self.texts["recovery"]
        for phrase in (
            "x-ui.db",
            "service.yaml",
            "20443",
            "fail-closed",
            "normalize_xui_endpoints",
        ):
            self.assertIn(phrase, recovery)

    def test_readme_documents_non_goals_and_menu_management(self):
        readme = self.texts["readme"]
        for phrase in (
            "更新机场订阅",
            "重新生成所有配置",
            "更新代码并同步配置",
            "template-sync",
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


PRIVATE_HOME_WORKFLOW_DOCS = ("readme", "operations", "private-data")
PRIVATE_HOME_SIX_FIELDS = (
    "proxies",
    "proxy-groups",
    "extend-proxy-groups",
    "inject-node-groups",
    "inject-home-node-groups",
    "rules",
)


def _unwrapped(text):
    """Collapse hard line wraps so phrase assertions survive markdown reflow."""
    return re.sub(r"\s+", "", text)


class PrivateHomeWorkflowDocumentationTests(unittest.TestCase):
    """Coverage: the private home overlay upload workflow stays documented."""

    @classmethod
    def setUpClass(cls):
        cls.texts = {
            name: DOCUMENTATION_PATHS[name].read_text(encoding="utf-8")
            for name in PRIVATE_HOME_WORKFLOW_DOCS
        }
        cls.flattened = {
            name: _unwrapped(text) for name, text in cls.texts.items()
        }

    def _assert_documented(self, name, phrase):
        self.assertIn(_unwrapped(phrase), self.flattened[name], "%s: %s" % (name, phrase))

    def _assert_not_documented(self, name, phrase):
        self.assertNotIn(
            _unwrapped(phrase), self.flattened[name], "%s: %s" % (name, phrase)
        )

    def test_workflow_documents_the_three_fixed_elements(self):
        for name in ("readme", "operations"):
            with self.subTest(document=name):
                for phrase in (
                    "./bin/clash-sub template-sync",
                    "private/home.yaml → /var/lib/clash-sub/private/home.yaml",
                    "clash-sub sync",
                ):
                    self._assert_documented(name, phrase)

    def test_private_data_documents_the_fixed_sftp_target(self):
        self._assert_documented(
            "private-data",
            "private/home.yaml → /var/lib/clash-sub/private/home.yaml",
        )

    def test_documented_remote_targets_are_only_the_fixed_home_path(self):
        for name in PRIVATE_HOME_WORKFLOW_DOCS:
            with self.subTest(document=name):
                for line in self.texts[name].splitlines():
                    if "→ /var/lib/clash-sub" in line:
                        self.assertIn(
                            "/var/lib/clash-sub/private/home.yaml",
                            line,
                            "%s: %s" % (name, line.strip()),
                        )

    def test_workflow_documents_the_rolling_workbench_origin(self):
        for name in PRIVATE_HOME_WORKFLOW_DOCS:
            with self.subTest(document=name):
                for phrase in ("clash-balanced.yaml", "滚动"):
                    self._assert_documented(name, phrase)

    def test_workflow_documents_the_six_private_home_fields(self):
        for name in ("operations", "private-data"):
            with self.subTest(document=name):
                self._assert_documented(name, "六个顶层字段")
                for field in PRIVATE_HOME_SIX_FIELDS:
                    self._assert_documented(name, field)

    def test_workflow_documents_owner_variant_isolation(self):
        for name in PRIVATE_HOME_WORKFLOW_DOCS:
            with self.subTest(document=name):
                for phrase in ("owner standard", "member standard"):
                    self._assert_documented(name, phrase)

    def test_workflow_documents_server_only_mihomo_validation(self):
        for name in PRIVATE_HOME_WORKFLOW_DOCS:
            with self.subTest(document=name):
                self._assert_documented(name, "本机不需要安装 Mihomo")

    def test_workflow_documents_asymmetric_failure_rule(self):
        for name in PRIVATE_HOME_WORKFLOW_DOCS:
            with self.subTest(document=name):
                for phrase in ("旧 owner release 继续服务", "不会恢复"):
                    self._assert_documented(name, phrase)
        self._assert_documented("private-data", "不是源文件备份")

    def test_workflow_documents_backup_boundaries(self):
        for phrase in ("`home.yaml`（家庭覆盖层）", "加密备份", "永不进入 Git"):
            self._assert_documented("private-data", phrase)

    def test_workflow_documents_sanitized_home_errors(self):
        self._assert_documented("operations", "home_yaml_invalid")

    def test_operational_docs_do_not_advertise_removed_upload_surfaces(self):
        banned = (
            "MIHOMO_BIN",
            "mihomo_binary_missing",
            "mihomo_validation_failed",
            "本地 Mihomo",
            "本机 Mihomo",
            "inbox",
            "home-import",
            "templates/features/home.yaml",
            "上传脚本",
        )
        for name in PRIVATE_HOME_WORKFLOW_DOCS:
            with self.subTest(document=name):
                text = self.texts[name]
                for phrase in banned:
                    self._assert_not_documented(name, phrase)
                # Word boundaries keep the SFTP recommendation itself legal.
                self.assertNotRegex(text, r"(?i)\bscp\b")
                self.assertNotRegex(text, r"(?i)\bftp\b")


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

    def test_requirements_are_the_two_approved_pins(self):
        self.assertEqual(
            REQUIREMENTS.read_text(encoding="utf-8").splitlines(),
            ["Jinja2==3.1.6", "PyYAML==6.0.3"],
        )


if __name__ == "__main__":
    unittest.main()
