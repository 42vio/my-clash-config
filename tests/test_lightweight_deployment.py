import configparser
import re
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
NGINX_STREAM_TEMPLATE = ROOT / "templates" / "nginx" / "stream.conf.j2"
NGINX_SUB_TEMPLATE = ROOT / "templates" / "nginx" / "sub-server.conf.j2"
INSTALL_SH = ROOT / "install.sh"
RECOVERY_SERVICE = ROOT / "deploy" / "systemd" / "clash-sub-recover.service"
RECOVERY_DROP_IN = ROOT / "deploy" / "systemd" / "nginx.service.d" / "clash-sub-recover.conf"
METADATA_SOCKET = ROOT / "deploy" / "systemd" / "clash-sub-metadata.socket"
METADATA_SERVICE = ROOT / "deploy" / "systemd" / "clash-sub-metadata.service"
METADATA_TMPFILES = ROOT / "deploy" / "systemd" / "tmpfiles.d" / "clash-sub-metadata.conf"
AIRPORT_REFRESH_SERVICE = ROOT / "deploy" / "systemd" / "clash-sub-airport-refresh.service"
AIRPORT_REFRESH_TIMER = ROOT / "deploy" / "systemd" / "clash-sub-airport-refresh.timer"
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
    "template-design": ROOT / "docs" / "template-design.md",
    "operations": ROOT / "docs" / "operations.md",
}


RETIRED_DOCUMENTATION_PATHS = (
    "docs/3x-ui-setup.md",
    "docs/dns-design.md",
    "docs/legacy-trojan-topology.md",
    "docs/private-data.md",
    "docs/recovery.md",
    "docs/superpowers/plans/2026-08-21-clash-subscription-publication.md",
    "docs/superpowers/plans/2026-08-23-clash-sub-lightweight.md",
    "docs/superpowers/plans/2026-08-25-clash-sub-integration.md",
    "docs/superpowers/plans/2026-08-28-private-home-overlay-upload.md",
    "docs/superpowers/specs/2026-08-21-clash-subscription-publication-design.md",
    "docs/superpowers/specs/2026-08-23-clash-sub-lightweight-redesign.md",
    "docs/superpowers/specs/2026-08-25-clash-sub-integration-design.md",
    "docs/superpowers/specs/2026-08-27-local-template-work" + "bench-design.md",
    "docs/superpowers/specs/2026-08-28-private-home-overlay-upload-design.md",
    "docs/superpowers/specs/2026-08-28-stable-amytelecom-provider-design.md",
    "docs/superpowers/plans/2026-08-29-clash-template-redesign.md",
    "docs/superpowers/specs/2026-08-29-clash-template-redesign.md",
)


class DocumentationContractTests(unittest.TestCase):
    """The personal maintenance documentation has one stable topology."""

    def test_all_documented_files_exist(self):
        for name, path in DOCUMENTATION_PATHS.items():
            self.assertTrue(path.is_file(), name)

    def test_only_four_personal_maintenance_documents_remain(self):
        actual = {
            path.relative_to(ROOT).as_posix()
            for path in (*ROOT.glob("*.md"), *(ROOT / "docs").rglob("*.md"))
            if not path.relative_to(ROOT).as_posix().startswith("docs/superpowers/")
        }
        self.assertEqual(
            actual,
            {
                "README.md",
                "DEPLOYMENT.md",
                "docs/template-design.md",
                "docs/operations.md",
            },
        )

    def test_retired_documents_and_old_release_aliases_are_absent(self):
        for relative in RETIRED_DOCUMENTATION_PATHS:
            self.assertFalse((ROOT / relative).exists(), relative)


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
        self.assertIn("▶ [1/12] 检查基础工具", text)
        self.assertIn("▶ [2/12] 创建 Python 环境", text)
        self.assertIn("▶ [3/12] 安装项目依赖", text)
        self.assertIn("CLASH_SUB_PROGRESS_OFFSET=3", text)

    def test_install_sh_has_valid_posix_shell_syntax(self):
        result = subprocess.run(
            ["sh", "-n", str(INSTALL_SH)], capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deployment_prerequisites_install_git_before_clone(self):
        deployment = (ROOT / "DEPLOYMENT.md").read_text(encoding="utf-8")
        self.assertIn("apt-get install -y git", deployment)

    def test_scheduled_traffic_units_are_gone_and_metadata_units_are_the_installed_set(self):
        # The retired scheduled-traffic units must not ship at all; the
        # top-level unit set is exactly the socket-activated metadata pair,
        # the boot recovery oneshot, and the weekly airport refresh pair
        # (drop-ins live in nginx.service.d, the socket's rule in tmpfiles.d/).
        top_level_units = sorted(
            path.name
            for path in (ROOT / "deploy" / "systemd").iterdir()
            if path.is_file()
        )
        self.assertEqual(
            top_level_units,
            [
                "clash-sub-airport-refresh.service",
                "clash-sub-airport-refresh.timer",
                "clash-sub-metadata.service",
                "clash-sub-metadata.socket",
                "clash-sub-recover.service",
            ],
        )
        self.assertTrue(METADATA_TMPFILES.is_file())

    def test_metadata_socket_unit_fixes_one_group_readable_unix_socket(self):
        text = METADATA_SOCKET.read_text(encoding="utf-8")
        self.assertIn("[Socket]", text)
        self.assertEqual(
            _unit_value(text, "ListenStream"), ["/run/clash-sub/metadata.sock"]
        )
        # The parent directory belongs to the tmpfiles rule (see below):
        # the systemd runtime-directory mechanism cannot own /run/clash-sub
        # as root:www-data, and the two mechanisms must not fight over it.
        self.assertEqual(_unit_value(text, "RuntimeDirectory"), [])
        self.assertEqual(_unit_value(text, "RuntimeDirectoryMode"), [])
        self.assertEqual(_unit_value(text, "SocketUser"), ["root"])
        self.assertEqual(_unit_value(text, "SocketGroup"), ["www-data"])
        self.assertEqual(_unit_value(text, "SocketMode"), ["0660"])
        self.assertEqual(_unit_value(text, "Accept"), ["no"])
        self.assertIn("WantedBy=sockets.target", text)
        # No datagram or TCP-style listener may exist anywhere in the unit.
        self.assertNotIn("ListenDatagram", text)
        self.assertNotIn("ListenSequentialPacket", text)
        self.assertNotRegex(text, r"Listen\w+=\s*(?:[0-9]{1,3}\.){3}[0-9]{1,3}:\d+")
        parser = configparser.ConfigParser()
        parser.read_string(text)
        self.assertEqual(set(parser.sections()), {"Unit", "Socket", "Install"})

    def test_metadata_runtime_directory_contract_comes_from_one_tmpfiles_rule(self):
        text = METADATA_TMPFILES.read_text(encoding="utf-8")
        rules = [
            line
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        self.assertEqual(rules, ["d /run/clash-sub 0750 root www-data -"])
        fields = rules[0].split()
        self.assertEqual(
            fields,
            ["d", "/run/clash-sub", "0750", "root", "www-data", "-"],
        )
        # The rule is the single source of the parent-directory contract
        # and must cover exactly the fixed socket path's parent.
        socket_text = METADATA_SOCKET.read_text(encoding="utf-8")
        self.assertEqual(
            str(Path(_unit_value(socket_text, "ListenStream")[0]).parent),
            fields[1],
        )
        # The installer (Task 7) owns shipping this file to /etc/tmpfiles.d/.
        self.assertIn("/etc/tmpfiles.d/", text)

    def test_metadata_service_unit_is_socket_activated_and_tightly_hardened(self):
        text = METADATA_SERVICE.read_text(encoding="utf-8")
        self.assertIn("[Service]", text)
        required = {
            "Type": "simple",
            "ExecStart": "/usr/local/bin/clash-sub metadata-serve",
            "Sockets": "clash-sub-metadata.socket",
            "User": "root",
            "Group": "root",
            "UMask": "0077",
            "NoNewPrivileges": "true",
            "PrivateNetwork": "true",
            "PrivateTmp": "true",
            "ProtectSystem": "strict",
            "ProtectHome": "true",
            "PrivateDevices": "true",
            "ProtectKernelTunables": "true",
            "ProtectKernelModules": "true",
            "ProtectControlGroups": "true",
            "RestrictSUIDSGID": "true",
            "LockPersonality": "true",
            "RestrictAddressFamilies": "AF_UNIX",
            "ReadOnlyPaths": "/etc/x-ui",
            "ReadWritePaths": "/var/lib/clash-sub/private",
        }
        for key, value in required.items():
            with self.subTest(key=key):
                self.assertEqual(_unit_value(text, key), [value])
        # Socket-activated only: the service itself never listens and is
        # never started directly at boot.
        self.assertNotIn("ListenStream", text)
        self.assertNotIn("ListenDatagram", text)
        self.assertNotIn("[Install]", text)
        parser = configparser.ConfigParser()
        parser.read_string(text)
        self.assertEqual(set(parser.sections()), {"Unit", "Service"})

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

    def test_airport_refresh_service_is_a_hardened_weekly_oneshot(self):
        text = AIRPORT_REFRESH_SERVICE.read_text(encoding="utf-8")
        self.assertIn("Wants=network-online.target", text)
        self.assertIn("After=network-online.target", text)
        required = {
            "Type": "oneshot",
            "ExecStart": "/usr/local/bin/clash-sub airport-scheduled-refresh",
            "TimeoutStartSec": "180",
            "User": "root",
            "Group": "root",
            "UMask": "0077",
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
            "RestrictAddressFamilies": "AF_UNIX AF_INET AF_INET6",
            "ReadWritePaths": "/var/lib/clash-sub/private /var/lib/clash-sub/public/provider",
        }
        for key, value in required.items():
            with self.subTest(key=key):
                self.assertEqual(_unit_value(text, key), [value])
        # Only the timer carries [Install]; the oneshot itself never starts
        # at boot and keeps network access for the weekly refresh.
        self.assertNotIn("[Install]", text)
        self.assertNotIn("PrivateNetwork=true", text)
        self.assertNotIn("http", text)

    def test_airport_refresh_timer_runs_weekly_with_random_delay_and_persistence(self):
        text = AIRPORT_REFRESH_TIMER.read_text(encoding="utf-8")
        required = {
            "OnCalendar": "weekly",
            "RandomizedDelaySec": "6h",
            "Persistent": "true",
            "Unit": "clash-sub-airport-refresh.service",
        }
        for key, value in required.items():
            with self.subTest(key=key):
                self.assertEqual(_unit_value(text, key), [value])
        self.assertIn("WantedBy=timers.target", text)
        parser = configparser.ConfigParser()
        parser.read_string(text)
        self.assertEqual(set(parser.sections()), {"Unit", "Timer", "Install"})
        # The units carry no URLs or credentials of any kind.
        self.assertNotIn("http", text)
        self.assertNotIn(AIRPORT_REFRESH_SERVICE.read_text(encoding="utf-8"), "://")

    def test_requirements_are_the_approved_pins(self):
        self.assertEqual(
            REQUIREMENTS.read_text(encoding="utf-8").splitlines(),
            ["Jinja2==3.1.6", "PyYAML==6.0.3", "ruamel.yaml==0.19.1"],
        )


if __name__ == "__main__":
    unittest.main()
