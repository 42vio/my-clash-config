import inspect
import io
import subprocess
import unittest
from collections import namedtuple
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from clash_sub import manage
from clash_sub.domain import ServiceConfig
from clash_sub.service import ServiceError, _OperationLock
from clash_sub.cli import MENU, _suggest_owner_email, main
from clash_sub.runtime import build_service


TOKEN = "x" * 43 + "-ABC234"
ROTATED_TOKEN = "y" * 43 + "-XYZ789"
SOURCE_URL = "http://127.0.0.1:2096/clash/private-sub-id"
AIRPORT_URL = "https://airport.example/temporary-secret"


def recovery_config(private_root):
    root = private_root.parent
    return ServiceConfig(
        "owner",
        "sub.example.test:8443",
        "example.com:443",
        root / "xui",
        private_root,
        root / "public",
        root / "routes",
        root / "mihomo",
        root / "nginx",
        root / "systemctl",
        root / "templates",
    )


class FakeService:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.sync_result = {"updated": (), "errors": ()}
        self.status_value = {
            "owner_client_id": 7,
            "last_success": 1750000000.0,
            "last_errors": ("member_update_failed",),
            "pending": ({"client_id": 9, "email": "new@example.test"},),
            "users": (
                {"client_id": 7, "email": "Alice", "active": True, "current_release": "release-owner"},
                {"client_id": 8, "email": "Bob", "active": True, "current_release": "release-member"},
            ),
        }
        self.links_value = (
            {
                "client_id": 7,
                "email": "Alice",
                "readable_code": "ABC234",
                "urls": (
                    "https://sub.example.test:8443/s/%s/clash-balanced.yaml" % TOKEN,
                    "https://sub.example.test:8443/s/%s/clash-standard.yaml" % TOKEN,
                    "https://sub.example.test:8443/s/%s/clash-privacy.yaml" % TOKEN,
                ),
            },
            {
                "client_id": 8,
                "email": "Bob",
                "readable_code": "XYZ789",
                "urls": ("https://sub.example.test:8443/s/%s/clash-standard.yaml" % ROTATED_TOKEN,),
            },
        )

    def _call(self, name, *args):
        self.calls.append((name, args))
        if self.failure is not None:
            raise self.failure

    def update_airport(self, url):
        self._call("update_airport", url)
        return {"updated": (), "errors": ()}

    def sync_all(self):
        self._call("sync_all")
        return self.sync_result

    def traffic_update(self):
        self._call("traffic_update")
        return {"updated": (), "errors": ()}

    def links(self):
        self._call("links")
        return self.links_value

    def status(self):
        self._call("status")
        return self.status_value

    def history(self, user):
        self._call("history", user)
        return ({"release_id": "release-%s" % user, "variants": ("standard",)},)

    def rollback(self, user, release):
        self._call("rollback", user, release)
        return {"client_id": user, "release_id": release, "variants": ("standard",)}

    def rotate_link(self, user):
        self._call("rotate_link", user)
        return {
            "client_id": user,
            "token": ROTATED_TOKEN,
            "urls": ("https://sub.example.test:8443/s/%s/clash-standard.yaml" % ROTATED_TOKEN,),
        }

    def reinitialize_owner(self, user):
        self._call("reinitialize_owner", user)
        return {"owner_client_id": user}


def run_cli(argv, service, *, stdin_text="", getpass_value=AIRPORT_URL, health_value=None):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with patch("clash_sub.cli.getpass", return_value=getpass_value), patch.object(
        manage, "health_report", return_value=health_value
    ):
        code = main(
            argv,
            stdin=io.StringIO(stdin_text),
            stdout=stdout,
            stderr=stderr,
            service_factory=lambda: service,
        )
    return code, stdout.getvalue(), stderr.getvalue()


class InterruptingInput:
    def readline(self):
        raise KeyboardInterrupt


class LightweightCliTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()

    def test_main_has_the_documented_injection_signature(self):
        signature = inspect.signature(main)
        self.assertEqual(tuple(signature.parameters), ("argv", "stdin", "stdout", "stderr", "service_factory"))
        self.assertTrue(all(parameter.default is None for parameter in signature.parameters.values()))

    def test_no_arguments_shows_only_the_four_options_and_exit(self):
        code, stdout, stderr = run_cli(None, self.service)

        self.assertEqual(code, 0)
        self.assertEqual(stdout, MENU)
        self.assertEqual(
            stdout,
            "1. 更新机场订阅\n"
            "2. 同步所有配置\n"
            "3. 查看订阅链接\n"
            "4. 查看状态和历史版本\n"
            "0. 退出\n",
        )
        self.assertEqual(stderr, "")
        self.assertEqual(self.service.calls, [])

    def test_menu_eof_and_interrupt_exit_without_constructing_a_service(self):
        constructed = []
        factory = lambda: constructed.append(object())
        stdout = io.StringIO()

        self.assertEqual(main(None, stdin=io.StringIO(), stdout=stdout, stderr=io.StringIO(), service_factory=factory), 0)
        self.assertEqual(main(None, stdin=InterruptingInput(), stdout=io.StringIO(), stderr=io.StringIO(), service_factory=factory), 0)
        self.assertEqual(constructed, [])

    def test_menu_airport_input_uses_getpass_and_keeps_url_out_of_terminal_output(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n")

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("update_airport", (AIRPORT_URL,))])
        self.assertNotIn(AIRPORT_URL, stdout + stderr)

    def test_menu_rejects_empty_airport_input_before_constructing_service(self):
        constructed = []
        with patch("clash_sub.cli.getpass", return_value=""):
            code = main(
                None,
                stdin=io.StringIO("1\n"),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                service_factory=lambda: constructed.append(object()),
            )

        self.assertEqual(code, 2)
        self.assertEqual(constructed, [])

    def test_menu_options_two_through_four_call_sync_links_and_status_history(self):
        sync = FakeService()
        links = FakeService()
        status = FakeService()

        self.assertEqual(run_cli(None, sync, stdin_text="2\n")[0], 0)
        self.assertEqual(run_cli(None, links, stdin_text="3\n")[0], 0)
        code, output, _ = run_cli(None, status, stdin_text="4\n")

        self.assertEqual(sync.calls, [("sync_all", ())])
        self.assertEqual(links.calls, [("links", ())])
        self.assertEqual(code, 0)
        self.assertEqual(status.calls, [("status", ()), ("history", (7,)), ("history", (8,))])
        self.assertIn("状态", output)
        self.assertIn("历史版本", output)

    def test_menu_and_noninteractive_sync_report_sanitized_partial_completion_nonzero(self):
        partial_result = {
            "updated": (),
            "errors": (
                {
                    "client_id": 8,
                    "email": "member@example.test",
                    "code": "member_update_failed",
                },
            ),
        }
        for argv, stdin_text in ((None, "2\n"), (["sync"], "")):
            with self.subTest(argv=argv):
                service = FakeService()
                service.sync_result = partial_result

                code, stdout, stderr = run_cli(argv, service, stdin_text=stdin_text)

                self.assertEqual(code, 1)
                self.assertEqual(stdout, (MENU if argv is None else "") + "同步部分完成。\n")
                self.assertEqual(stderr, "客户端 ID 8（错误代码：member_update_failed）\n")
                self.assertNotIn("member@example.test", stdout + stderr)

        code, stdout, stderr = run_cli(["sync"], FakeService())

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "同步已完成。\n")
        self.assertEqual(stderr, "")

    def test_links_lists_every_user_in_returned_database_id_order_without_selection(self):
        code, stdout, stderr = run_cli(["links"], self.service)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertLess(stdout.index("Alice [ABC234]"), stdout.index("Bob [XYZ789]"))
        for item in self.service.links_value:
            for url in item["urls"]:
                self.assertIn(url, stdout)
        self.assertNotIn("请选择用户", stdout)

    def test_status_and_history_are_readable_deterministic_and_never_expose_secrets(self):
        code, stdout, stderr = run_cli(["status"], self.service)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("所有者客户端 ID：7", stdout)
        self.assertLess(stdout.index("ID 7"), stdout.index("ID 8"))
        for secret in (TOKEN, ROTATED_TOKEN, "ABC234", "private-sub-id", SOURCE_URL, "550e8400-e29b-41d4-" "a716-446655440000"):
            self.assertNotIn(secret, stdout)

    def test_status_output_renders_last_success_errors_and_pending_sources(self):
        code, stdout, stderr = run_cli(["status"], self.service)

        self.assertEqual(code, 0)
        self.assertIn("最后成功时间：2025-06-15 15:06:40Z", stdout)
        self.assertIn("最近错误：member_update_failed", stdout)
        self.assertIn("待同步：", stdout)
        self.assertIn("ID 9（new@example.test）", stdout)

    def test_status_output_renders_empty_state_without_placeholders(self):
        self.service.status_value = dict(
            self.service.status_value,
            last_success=None,
            last_errors=(),
            pending=(),
        )

        code, stdout, stderr = run_cli(["status"], self.service)

        self.assertEqual(code, 0)
        self.assertIn("最后成功时间：无", stdout)
        self.assertIn("最近错误：无", stdout)
        self.assertIn("待同步：无", stdout)

    def test_status_appends_health_report_when_available(self):
        health_value = {
            "units": {"nginx": "active", "x-ui": "active"},
            "certificate": {"not_after": "notAfter=Sep 25 12:00:00 2026 GMT", "days_left": 30},
        }

        code, stdout, stderr = run_cli(["status"], self.service, health_value=health_value)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("nginx：active；x-ui：active", stdout)
        self.assertIn("证书：notAfter=Sep 25 12:00:00 2026 GMT（剩余 30 天）", stdout)
        self.assertLess(stdout.index("用户："), stdout.index("nginx："))

    def test_status_health_failure_never_breaks_status_output(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch("clash_sub.cli.getpass", return_value=AIRPORT_URL), patch.object(
            manage, "health_report", side_effect=RuntimeError("health subsystem broken")
        ):
            code = main(
                ["status"],
                stdin=io.StringIO(),
                stdout=stdout,
                stderr=stderr,
                service_factory=lambda: self.service,
            )

        self.assertEqual(code, 0)
        self.assertIn("所有者客户端 ID：7", stdout.getvalue())
        self.assertNotIn("nginx：", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_noninteractive_commands_call_only_the_documented_service_operations(self):
        cases = (
            (["sync"], "sync_all", ()),
            (["traffic-update"], "traffic_update", ()),
            (["status"], "status", ()),
            (["links"], "links", ()),
            (["history", "7"], "history", (7,)),
            (["rollback", "7", "release-7"], "rollback", (7, "release-7")),
            (["rotate-link", "7"], "rotate_link", (7,)),
            (["reinitialize-owner", "9"], "reinitialize_owner", (9,)),
        )
        for argv, method, arguments in cases:
            with self.subTest(argv=argv):
                service = FakeService()
                code, _, stderr = run_cli(argv, service)
                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(service.calls[0], (method, arguments))

    def test_reinitialize_owner_is_noninteractive_and_never_appears_in_the_daily_menu(self):
        code, stdout, stderr = run_cli(["reinitialize-owner", "9"], self.service)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, "所有者已重新初始化；请更新机场订阅后执行 sync。\n")
        self.assertNotIn("重新初始化", MENU)

    def test_reinitialize_owner_requires_a_canonical_decimal_database_id(self):
        code, stdout, stderr = run_cli(["reinitialize-owner", "+9"], self.service)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "操作失败（错误代码：invalid_command）\n")
        self.assertEqual(self.service.calls, [])

    def test_root_only_recover_uses_disk_recovery_without_requiring_running_nginx(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory).resolve() / "private"
            private_root.mkdir(mode=0o700)
            private_root.chmod(0o700)
            config = recovery_config(private_root)
            with patch("clash_sub.cli.os", SimpleNamespace(geteuid=lambda: 0)), patch(
                "clash_sub.cli.load_config", return_value=config
            ), patch("clash_sub.cli.recover_runtime") as recover:
                code, stdout, stderr = run_cli(["recover"], self.service)

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "运行时恢复已完成。\n")
        self.assertEqual(stderr, "")
        self.assertEqual(recover.call_args.args, (config, subprocess.run))
        self.assertEqual(recover.call_args.kwargs, {"reload": False})
        self.assertEqual(self.service.calls, [])

    def test_root_only_recover_rejects_a_real_contended_operation_lock_before_recovery(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory).resolve() / "private"
            private_root.mkdir(mode=0o700)
            private_root.chmod(0o700)
            config = recovery_config(private_root)
            with _OperationLock(private_root / "operation.lock"):
                with patch("clash_sub.cli.os", SimpleNamespace(geteuid=lambda: 0)), patch(
                    "clash_sub.cli.load_config", return_value=config
                ), patch("clash_sub.cli.recover_runtime") as recover:
                    code, stdout, stderr = run_cli(["recover"], self.service)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "操作失败（错误代码：operation_busy）\n")
        recover.assert_not_called()
        self.assertEqual(self.service.calls, [])

    def test_root_only_recover_rejects_an_invalid_operation_lock_before_recovery(self):
        with TemporaryDirectory() as directory:
            private_root = Path(directory).resolve() / "private"
            private_root.mkdir(mode=0o700)
            private_root.chmod(0o755)
            config = recovery_config(private_root)
            with patch("clash_sub.cli.os", SimpleNamespace(geteuid=lambda: 0)), patch(
                "clash_sub.cli.load_config", return_value=config
            ), patch("clash_sub.cli.recover_runtime") as recover:
                code, stdout, stderr = run_cli(["recover"], self.service)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "操作失败（错误代码：operation_lock_invalid）\n")
        recover.assert_not_called()
        self.assertEqual(self.service.calls, [])

    def test_rejects_legacy_unknown_and_airport_url_arguments_without_echoing_them(self):
        rejected = (
            ["refresh"],
            ["refresh-all"],
            ["clashctl"],
            ["airport", AIRPORT_URL],
            [AIRPORT_URL],
            ["sync", AIRPORT_URL],
        )
        for argv in rejected:
            with self.subTest(argv=argv):
                service = FakeService()
                code, stdout, stderr = run_cli(argv, service)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "操作失败（错误代码：invalid_command）\n")
                self.assertNotIn(AIRPORT_URL, stderr)
                self.assertEqual(service.calls, [])

    def test_rejects_any_url_argument_before_it_can_reach_or_appear_from_a_command(self):
        url = "ftp://airport.example/temporary-secret"

        code, stdout, stderr = run_cli(["rollback", "7", url], self.service)

        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "操作失败（错误代码：invalid_command）\n")
        self.assertNotIn(url, stdout + stderr)
        self.assertEqual(self.service.calls, [])

    def test_service_errors_print_only_a_stable_chinese_summary_and_code(self):
        self.service.failure = ServiceError("sync_activation_failed")

        code, stdout, stderr = run_cli(["sync"], self.service)

        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "操作失败（错误代码：sync_activation_failed）\n")
        self.assertNotIn("traceback", stderr.lower())

    def test_rotate_link_prints_new_urls_without_a_separate_raw_token(self):
        code, stdout, stderr = run_cli(["rotate-link", "7"], self.service)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("订阅链接已轮换", stdout)
        self.assertIn(self.service.rotate_link(7)["urls"][0], stdout)
        self.assertNotIn("令牌", stdout)

    def test_default_factory_uses_private_repo_config_and_injects_subprocess_runner(self):
        root = Path(__file__).resolve().parents[1]
        config = ServiceConfig(
            "owner", "sub.example.test:8443", "example.com:443", Path("/xui"), Path("/private"), Path("/public"),
            Path("/routes"), Path("/mihomo"), Path("/nginx"), Path("/systemctl"), Path("/templates"),
        )
        built = []
        with patch("clash_sub.runtime.load_config", return_value=config) as load, patch("clash_sub.runtime.ClashSubService", side_effect=lambda *args, **kwargs: built.append((args, kwargs)) or object()):
            build_service()

        self.assertEqual(load.call_args.args, (root / "private" / "config" / "service.yaml", root))
        self.assertIs(built[0][1]["runner"], subprocess.run)
        self.assertIs(built[0][1]["mihomo_validator"].runner, subprocess.run)

    def test_entry_point_imports_only_the_new_cli_main_after_path_bootstrap(self):
        entry_point = Path(__file__).resolve().parents[1] / "bin" / "clash-sub"
        source = entry_point.read_text(encoding="utf-8")

        self.assertIn("from clash_sub.cli import main", source)
        self.assertNotIn("clash_sub.host_cli", source)

    def test_entry_point_reexecs_when_launched_by_the_venv_base_interpreter(self):
        import subprocess
        import sys

        repository = Path(__file__).resolve().parents[1]
        venv_python = repository / ".venv" / "bin" / "python"
        base_interpreter = venv_python.resolve()
        if not venv_python.is_symlink():
            self.skipTest("venv python is not a symlink to a base interpreter")

        result = subprocess.run(
            [str(base_interpreter), str(repository / "bin" / "clash-sub"), "invalid-cmd"],
            capture_output=True,
            text=True,
        )

        self.assertNotIn("ModuleNotFoundError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_installed_entry_point_reexecs_repo_venv_python_from_any_location(self):
        import shutil
        import sys
        from tempfile import TemporaryDirectory

        repository = Path(__file__).resolve().parents[1]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            copy = root / "repo"
            (copy / "bin").mkdir(parents=True)
            shutil.copy(repository / "bin" / "clash-sub", copy / "bin" / "clash-sub")
            marker = root / "argv.txt"
            fake_venv_python = copy / ".venv" / "bin" / "python"
            fake_venv_python.parent.mkdir(parents=True)
            fake_venv_python.write_text(
                '#!/bin/sh\nprintf "%%s\\n" "$@" > "%s"\n' % marker,
                encoding="utf-8",
            )
            fake_venv_python.chmod(0o755)
            installed = root / "installed" / "clash-sub"
            installed.parent.mkdir()
            installed.symlink_to(copy / "bin" / "clash-sub")

            result = subprocess.run(
                [sys.executable, str(installed), "sync"],
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            recorded = marker.read_text(encoding="utf-8").splitlines()
            self.assertEqual(recorded[-1], "sync")
            self.assertTrue(recorded[0].endswith("clash-sub"), recorded)


class InstallCommandTests(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()

    def test_install_requires_root(self):
        with patch("clash_sub.cli.Installer") as installer:
            installer.return_value.install.return_value = {}
            with patch("clash_sub.installer.os.geteuid", return_value=1000):
                code = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(code, 1)
        self.assertIn("not_root", self.stderr.getvalue())

    def test_rollback_install_flag_requires_root(self):
        with patch("clash_sub.cli.Installer"):
            with patch("clash_sub.installer.os.geteuid", return_value=1000):
                code = main(["rollback", "--install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(code, 1)
        self.assertIn("not_root", self.stderr.getvalue())

    def test_rollback_rejects_install_flag_with_positionals(self):
        code = main(["rollback", "--install", "1", "release-id"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(code, 2)
        self.assertIn("invalid_command", self.stderr.getvalue())

    def test_rollback_still_accepts_user_release(self):
        with patch("clash_sub.cli.build_service") as factory:
            service = factory.return_value
            service.rollback.return_value = None
            code = main(["rollback", "1", "release-id"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(code, 0)
        service.rollback.assert_called_once_with(1, "release-id")

    def test_install_rejects_empty_domain(self):
        with patch.dict("os.environ", {"CLASH_SUB_DOMAIN": ""}), patch(
            "builtins.input", return_value=""
        ), patch("clash_sub.cli.getpass", return_value="tok"), patch(
            "clash_sub.cli.os.geteuid", return_value=0
        ):
            status = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 2)
        self.assertIn("invalid_domain", self.stderr.getvalue())

    def test_install_prompts_owner_with_snapshot_default(self):
        captured = {}

        class FakeInstaller:
            def __init__(self, root, print_fn=None):
                captured["print_fn"] = print_fn

            def install(self, **kwargs):
                captured["kwargs"] = kwargs
                return {"panel_url": "https://sub.example.com/xui7k2m/", "gate_instruction": ""}

        with patch.dict(
            "os.environ", {"CLASH_SUB_DOMAIN": "example.com"}, clear=False
        ), patch(
            "clash_sub.cli.getpass", return_value="tok"
        ), patch(
            "builtins.input", return_value=""
        ), patch(
            "clash_sub.cli._suggest_owner_email", return_value="real-owner@x"
        ), patch(
            "clash_sub.cli.Installer", FakeInstaller
        ), patch(
            "clash_sub.cli.os.geteuid", return_value=0
        ):
            status = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 0)
        self.assertEqual(captured["kwargs"]["owner_email"], "real-owner@x")

    def test_install_uses_typed_owner_over_suggestion(self):
        captured = {}

        class FakeInstaller:
            def __init__(self, root, print_fn=None):
                pass

            def install(self, **kwargs):
                captured["kwargs"] = kwargs
                return {"panel_url": "https://sub.example.com/xui7k2m/", "gate_instruction": ""}

        with patch.dict(
            "os.environ", {"CLASH_SUB_DOMAIN": "example.com"}, clear=False
        ), patch(
            "clash_sub.cli.getpass", return_value="tok"
        ), patch(
            "builtins.input", return_value="typed-owner@x"
        ), patch(
            "clash_sub.cli._suggest_owner_email", return_value="real-owner@x"
        ), patch(
            "clash_sub.cli.Installer", FakeInstaller
        ), patch(
            "clash_sub.cli.os.geteuid", return_value=0
        ):
            status = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 0)
        self.assertEqual(captured["kwargs"]["owner_email"], "typed-owner@x")

    def test_install_rejects_missing_owner_without_suggestion(self):
        with patch.dict(
            "os.environ", {"CLASH_SUB_DOMAIN": "example.com"}, clear=False
        ), patch(
            "clash_sub.cli.getpass", return_value="tok"
        ), patch(
            "builtins.input", return_value=""
        ), patch(
            "clash_sub.cli._suggest_owner_email", return_value=""
        ), patch(
            "clash_sub.cli.Installer"
        ), patch(
            "clash_sub.cli.os.geteuid", return_value=0
        ):
            status = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 2)
        self.assertIn("owner_email_required", self.stderr.getvalue())

    def test_install_eof_without_owner_env_returns_error(self):
        with patch.dict(
            "os.environ",
            {"CLASH_SUB_DOMAIN": "example.com", "CLASH_SUB_OWNER_EMAIL": ""},
            clear=False,
        ), patch(
            "clash_sub.cli.getpass", return_value="tok"
        ), patch(
            "builtins.input", side_effect=EOFError
        ), patch(
            "clash_sub.cli._suggest_owner_email", return_value="someone@x"
        ) as suggest, patch(
            "clash_sub.cli.Installer"
        ) as installer, patch(
            "clash_sub.cli.os.geteuid", return_value=0
        ):
            status = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 2)
        self.assertIn("owner_email_required", self.stderr.getvalue())
        suggest.assert_called_once()
        installer.return_value.install.assert_not_called()

    def test_install_passes_typed_owner_when_multiple_clients(self):
        captured = {}

        class FakeInstaller:
            def __init__(self, root, print_fn=None):
                pass

            def install(self, **kwargs):
                captured["kwargs"] = kwargs
                return {"panel_url": "https://sub.example.com/xui7k2m/", "gate_instruction": ""}

        with patch.dict(
            "os.environ",
            {"CLASH_SUB_DOMAIN": "example.com", "CLASH_SUB_OWNER_EMAIL": ""},
            clear=False,
        ), patch(
            "clash_sub.cli.getpass", return_value="tok"
        ), patch(
            "builtins.input", return_value="owner@x"
        ), patch(
            "clash_sub.cli._suggest_owner_email", return_value=""
        ), patch(
            "clash_sub.cli.Installer", FakeInstaller
        ), patch(
            "clash_sub.cli.os.geteuid", return_value=0
        ):
            status = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 0)
        self.assertEqual(captured["kwargs"]["owner_email"], "owner@x")

    def test_install_uses_owner_email_from_environment(self):
        captured = {}

        class FakeInstaller:
            def __init__(self, root, print_fn=None):
                pass

            def install(self, **kwargs):
                captured["kwargs"] = kwargs
                return {"panel_url": "https://sub.example.com/xui7k2m/", "gate_instruction": ""}

        with patch.dict(
            "os.environ",
            {"CLASH_SUB_DOMAIN": "example.com", "CLASH_SUB_OWNER_EMAIL": "env-owner@x"},
            clear=False,
        ), patch(
            "clash_sub.cli.getpass", return_value="tok"
        ), patch(
            "clash_sub.cli._suggest_owner_email"
        ) as suggest, patch(
            "clash_sub.cli.Installer", FakeInstaller
        ), patch(
            "clash_sub.cli.os.geteuid", return_value=0
        ):
            status = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 0)
        self.assertEqual(captured["kwargs"]["owner_email"], "env-owner@x")
        suggest.assert_not_called()

    def test_rollback_with_user_only_is_invalid(self):
        status = main(["rollback", "1"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 2)
        self.assertIn("invalid_command", self.stderr.getvalue())


FakeSnapshotClient = namedtuple("FakeSnapshotClient", "email enabled")


class OwnerSuggestionTests(unittest.TestCase):
    def _suggest_with_clients(self, clients):
        snapshot = SimpleNamespace(clients=tuple(clients))
        with patch("clash_sub.xui.read_xui_snapshot", lambda path: snapshot):
            return _suggest_owner_email()

    def test_suggests_the_client_when_exactly_one_is_enabled(self):
        clients = (
            FakeSnapshotClient("owner@x", True),
            FakeSnapshotClient("member@x", False),
        )

        self.assertEqual(self._suggest_with_clients(clients), "owner@x")

    def test_no_suggestion_when_multiple_clients_are_enabled(self):
        clients = (
            FakeSnapshotClient("first@x", True),
            FakeSnapshotClient("second@x", True),
            FakeSnapshotClient("member@x", False),
        )

        self.assertEqual(self._suggest_with_clients(clients), "")

    def test_no_suggestion_when_no_client_is_enabled(self):
        clients = (FakeSnapshotClient("member@x", False),)

        self.assertEqual(self._suggest_with_clients(clients), "")

    def test_no_suggestion_when_there_are_no_clients(self):
        self.assertEqual(self._suggest_with_clients(()), "")

    def test_no_suggestion_when_snapshot_cannot_be_read(self):
        with patch("clash_sub.xui.read_xui_snapshot", side_effect=RuntimeError("boom")):
            self.assertEqual(_suggest_owner_email(), "")


class ManageCommandTests(unittest.TestCase):
    def setUp(self):
        self.stderr = io.StringIO()
        self.stdout = io.StringIO()

    def test_backup_requires_root(self):
        with patch("clash_sub.cli.os.geteuid", return_value=1000):
            status = main(["backup"], stdout=self.stdout, stderr=self.stderr)

        self.assertEqual(status, 1)
        self.assertIn("not_root", self.stderr.getvalue())

    def test_cert_domain_flag_is_unsupported(self):
        with patch("clash_sub.cli.os.geteuid", return_value=0), patch(
            "clash_sub.manage.cert_status"
        ) as status_fn:
            status = main(["cert", "--domain", "new.example.com"], stdout=self.stdout, stderr=self.stderr)

        self.assertEqual(status, 2)
        self.assertIn("domain_change_unsupported", self.stderr.getvalue())
        status_fn.assert_not_called()

    def test_cert_prints_status(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage,
            "cert_status",
            return_value={"present": True, "not_after": "Sep 25 12:00:00 2026 GMT"},
        ):
            status = main(["cert"], stdout=self.stdout, stderr=self.stderr)

        self.assertEqual(status, 0)
        self.assertIn("Sep 25", self.stdout.getvalue())

    def test_cert_renew_runs(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "cert_renew", return_value=True
        ) as renew:
            status = main(["cert", "--renew"], stdout=self.stdout, stderr=self.stderr)

        self.assertEqual(status, 0)
        renew.assert_called_once()


if __name__ == "__main__":
    unittest.main()
