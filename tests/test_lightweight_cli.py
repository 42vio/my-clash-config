import inspect
import io
import os
import subprocess
import unittest
from collections import namedtuple
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from clash_sub import manage
from clash_sub import template_sync
from clash_sub.domain import ServiceConfig
from clash_sub.installer import InstallerError
from clash_sub.service import ServiceError, _OperationLock
from clash_sub.cli import (
    AIRPORT_MENU,
    BACKUP_MENU,
    CERT_MENU,
    MAINTENANCE_MENU,
    MENU,
    USER_MENU,
    _call,
    _parser,
    _prompt,
    _suggest_owner_email,
    main,
)
from clash_sub.runtime import build_service


TOKEN = "x" * 43 + "-ABC234"
ROTATED_TOKEN = "y" * 43 + "-XYZ789"
SOURCE_URL = "http://127.0.0.1:2096/clash/private-sub-id"
AIRPORT_URL = "https://airport.example/temporary-secret"
ACTIVATION_URL = "https://portal.example/Subscription/index?sid=placeholder&token=placeholder"
GREEN = "\033[0;32m"
YELLOW = "\033[0;33m"
RED = "\033[0;31m"
RESET = "\033[0m"


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
        self.airport_traffic_captured = True
        self.airport_auto_result = {"updated": True, "traffic_captured": True}
        self.auto_refresh_error = None
        self.airport_status_value = {
            "saved": True,
            "activation_configured": True,
            "activation_host": "portal.example",
            "source_configured": True,
            "source_host": "airport.example",
            "traffic_total": 107374182400,
            "traffic_used": 21474836480,
            "traffic_remaining": 85899345920,
            # The unified contract carries the Subscription-Userinfo expire
            # value in SECONDS under the legacy-named key.
            "traffic_expiry_ms": 1893456000,
            "last_success": 1750000000,
            "provider_present": True,
        }
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
                    "https://sub.example.test:8443/s/%s/Clash-Compat.yaml" % TOKEN,
                    "https://sub.example.test:8443/s/%s/Clash-Balance.yaml" % TOKEN,
                ),
            },
            {
                "client_id": 8,
                "email": "Bob",
                "readable_code": "XYZ789",
                "urls": ("https://sub.example.test:8443/s/%s/Clash-Compat.yaml" % ROTATED_TOKEN,),
            },
        )

    def _call(self, name, *args):
        self.calls.append((name, args))
        if self.failure is not None:
            raise self.failure

    def replace_airport_source(self, url):
        self._call("replace_airport_source", url)
        return {"updated": True, "traffic_captured": self.airport_traffic_captured}

    def refresh_airport(self):
        self._call("refresh_airport")
        return {"updated": True, "traffic_captured": self.airport_traffic_captured}

    def configure_airport_portal(self, url):
        self._call("configure_airport_portal", url)
        return {"updated": True, "traffic_captured": self.airport_traffic_captured}

    def auto_refresh_airport(self):
        self._call("auto_refresh_airport")
        if self.auto_refresh_error is not None:
            raise self.auto_refresh_error
        return self.airport_auto_result

    def airport_status(self):
        self._call("airport_status")
        return self.airport_status_value

    def sync_all(self):
        self._call("sync_all")
        return self.sync_result

    def links(self):
        self._call("links")
        return self.links_value

    def status(self):
        self._call("status")
        return self.status_value

    def history(self, user):
        self._call("history", user)
        return ({"release_id": "release-%s" % user, "variants": ("compat", "balance")},)

    def rollback(self, user, release):
        self._call("rollback", user, release)
        return {"client_id": user, "release_id": release, "variants": ("compat", "balance")}

    def rotate_link(self, user):
        self._call("rotate_link", user)
        return {
            "client_id": user,
            "token": ROTATED_TOKEN,
            "urls": ("https://sub.example.test:8443/s/%s/Clash-Compat.yaml" % ROTATED_TOKEN,),
        }

    def reinitialize_owner(self, user):
        self._call("reinitialize_owner", user)
        return {"owner_client_id": user}


class TtyStringIO(io.StringIO):
    def isatty(self):
        return True


def run_cli(argv, service, *, stdin_text="", getpass_value=AIRPORT_URL, health_value=None, tty=False):
    stdout = TtyStringIO() if tty else io.StringIO()
    stderr = TtyStringIO() if tty else io.StringIO()
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


class InterruptingAfterFirstLine:
    def __init__(self, first_line):
        self.lines = [first_line]

    def readline(self):
        if self.lines:
            return self.lines.pop(0)
        raise KeyboardInterrupt


class LightweightCliTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()

    def test_main_has_the_documented_injection_signature(self):
        signature = inspect.signature(main)
        self.assertEqual(tuple(signature.parameters), ("argv", "stdin", "stdout", "stderr", "service_factory"))
        self.assertTrue(all(parameter.default is None for parameter in signature.parameters.values()))

    def test_prompt_is_flushed_before_waiting_for_input(self):
        stdout = MagicMock()

        self.assertEqual(_prompt(io.StringIO("value\n"), stdout, "Prompt: "), "value")

        self.assertEqual(stdout.method_calls[:2], [call.write("Prompt: "), call.flush()])

    def test_no_arguments_shows_the_full_menu_and_exits_on_eof(self):
        code, stdout, stderr = run_cli(None, self.service)

        self.assertEqual(code, 0)
        self.assertEqual(stdout, MENU)
        for line in (
            "╔──────────────────────────────────────────────╗",
            "│  clash-sub 管理脚本",
            "│  0. 退出",
            "│  1. 机场订阅",
            "│  2. 重新生成所有配置",
            "│  3. 查看订阅链接",
            "│  4. 查看运行状态",
            "│  5. 程序维护",
            "│  6. 证书管理",
            "│  7. 备份与恢复",
            "│  8. 用户与版本",
            "╚──────────────────────────────────────────────╝",
            "请输入选项 [0-8]：",
        ):
            self.assertIn(line, stdout)
        self.assertNotIn("历史版本", MENU)
        self.assertEqual(stderr, "")
        self.assertEqual(self.service.calls, [])

    def test_menu_eof_and_interrupt_exit_without_constructing_a_service(self):
        constructed = []
        factory = lambda: constructed.append(object())
        stdout = io.StringIO()

        self.assertEqual(main(None, stdin=io.StringIO(), stdout=stdout, stderr=io.StringIO(), service_factory=factory), 0)
        self.assertEqual(main(None, stdin=InterruptingInput(), stdout=io.StringIO(), stderr=io.StringIO(), service_factory=factory), 0)
        self.assertEqual(constructed, [])

    def test_airport_menu_opens_the_fixed_submenu(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count(AIRPORT_MENU), 1)
        self.assertEqual(stdout.count(MENU), 2)

    def test_airport_submenu_text_is_fixed(self):
        self.assertEqual(
            AIRPORT_MENU,
            "机场订阅\n\n"
            "1. 设置订阅开关页面\n"
            "2. 自动开启订阅并刷新（推荐）\n"
            "3. 手动开启订阅后刷新\n"
            "4. 使用新订阅链接更新\n"
            "5. 查看机场状态\n"
            "0. 返回\n"
            "\n"
            "请输入选项 [0-5]：",
        )

    def test_airport_submenu_invalid_selection_stays_in_the_submenu(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n9\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(stderr.count("操作失败（错误代码：invalid_menu_selection）\n"), 1)
        self.assertEqual(stdout.count(AIRPORT_MENU), 2)
        self.assertEqual(stdout.count(MENU), 2)

    def test_airport_portal_explains_the_chain_and_reads_the_page_visibly(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "clash_sub.cli.getpass",
            side_effect=AssertionError("airport URL must use visible input"),
        ):
            code = main(
                None,
                stdin=io.StringIO("1\n1\n  " + ACTIVATION_URL + "  \n\n0\n0\n"),
                stdout=stdout,
                stderr=stderr,
                service_factory=lambda: self.service,
            )

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("configure_airport_portal", (ACTIVATION_URL,))])
        self.assertIn("此操作会验证订阅开关页面、生成新的订阅链接，", stdout.getvalue())
        self.assertIn("任一步失败都会保留当前配置。", stdout.getvalue())
        self.assertIn("请输入订阅开关页面：", stdout.getvalue())
        self.assertIn("机场订阅已更新。", stdout.getvalue())
        self.assertNotIn(ACTIVATION_URL, stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_airport_portal_empty_input_is_rejected_before_constructing_service(self):
        constructed = []
        code = main(
            None,
            stdin=io.StringIO("1\n1\n   \n0\n0\n"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            service_factory=lambda: constructed.append(object()),
        )

        self.assertEqual(code, 2)
        self.assertEqual(constructed, [])

    def test_airport_auto_refresh_calls_the_service_without_prompting(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n2\n\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("auto_refresh_airport", ())])
        self.assertNotIn("请输入订阅开关页面", stdout)
        self.assertNotIn("请输入新的机场订阅链接", stdout)
        self.assertIn("机场订阅已更新。", stdout)
        self.assertNotIn(("sync_all", ()), self.service.calls)
        self.assertEqual(stderr, "")

    def test_airport_auto_refresh_missing_activation_stays_in_the_submenu(self):
        self.service.auto_refresh_error = ServiceError("airport_activation_missing")

        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n2\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertIn("尚未设置订阅开关页面，请先使用菜单 1。", stderr)
        self.assertNotIn("airport_activation_missing", stderr)
        self.assertEqual(stdout.count(AIRPORT_MENU), 2)

    def test_airport_auto_refresh_reports_a_normal_skip(self):
        self.service.airport_auto_result = {"updated": False, "skipped": True}

        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n2\n\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertIn("本次已跳过，当前机场配置保持不变", stdout)
        self.assertEqual(stderr, "")

    def test_airport_manual_refresh_prompts_and_requires_enter(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n3\n\n\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertIn("请在浏览器中手动打开机场订阅开关页面。", stdout)
        self.assertIn("确认订阅已开启后，按回车继续；输入 0 取消：", stdout)
        self.assertEqual(self.service.calls, [("refresh_airport", ())])
        self.assertIn("机场订阅已更新。", stdout)
        self.assertNotIn("请输入新的机场订阅链接", stdout)
        self.assertEqual(stderr, "")

    def test_airport_manual_refresh_can_be_cancelled_without_touching_the_service(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n3\n0\n\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [])

    def test_airport_manual_refresh_reports_missing_traffic_information(self):
        self.service.airport_traffic_captured = False

        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n3\n\n\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("refresh_airport", ())])
        self.assertIn("机场订阅已更新，未获取到流量信息。", stdout)
        self.assertEqual(stderr, "")

    def test_airport_manual_refresh_without_a_saved_link_reports_it(self):
        self.service.failure = ServiceError("airport_source_missing")

        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n3\n\n")

        self.assertEqual(code, 1)
        self.assertIn("未保存机场订阅链接", stderr)
        self.assertIn("airport_source_missing", stderr)
        self.assertNotIn(AIRPORT_URL, stdout + stderr)

    def test_airport_source_update_explains_and_reads_the_link_visibly(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with patch(
            "clash_sub.cli.getpass",
            side_effect=AssertionError("airport URL must use visible input"),
        ):
            code = main(
                None,
                stdin=io.StringIO("1\n4\n  " + AIRPORT_URL + "  \n\n0\n0\n"),
                stdout=stdout,
                stderr=stderr,
                service_factory=lambda: self.service,
            )

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("replace_airport_source", (AIRPORT_URL,))])
        self.assertIn("此操作不会访问订阅开关页面。", stdout.getvalue())
        self.assertIn("请输入新的机场订阅链接：", stdout.getvalue())
        self.assertIn("机场订阅已更新。", stdout.getvalue())
        self.assertEqual(stderr.getvalue(), "")

    def test_airport_menu_rejects_empty_input_before_constructing_service(self):
        constructed = []
        code = main(
            None,
            stdin=io.StringIO("1\n4\n   \n0\n0\n"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            service_factory=lambda: constructed.append(object()),
        )

        self.assertEqual(code, 2)
        self.assertEqual(constructed, [])

    def test_airport_status_shows_only_the_public_summary(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n5\n\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("airport_status", ())])
        for line in (
            "开关页面已配置：是",
            "开关页面域名：portal.example",
            "订阅链接已配置：是",
            "来源域名：airport.example",
            "总量：107374182400",
            "已用：21474836480",
            "剩余：85899345920",
            "到期时间：2030-01-01 00:00:00Z",
            "最近成功时间：2025-06-15 15:06:40Z",
            "AmyTelecom.yaml：存在",
        ):
            self.assertIn(line, stdout)
        # A seconds value misread as milliseconds would render ~1970-01.
        self.assertNotIn("1970", stdout)
        self.assertNotIn("https://", stdout)
        self.assertNotIn("token", stdout)
        self.assertNotIn("sid", stdout)
        self.assertEqual(stderr, "")

    def test_airport_status_renders_unsaved_and_missing_fields(self):
        self.service.airport_status_value = {
            "saved": False,
            "activation_configured": False,
            "activation_host": None,
            "source_configured": False,
            "source_host": None,
            "traffic_total": None,
            "traffic_used": None,
            "traffic_remaining": None,
            "traffic_expiry_ms": None,
            "last_success": None,
            "provider_present": False,
        }

        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n5\n\n0\n0\n")

        self.assertEqual(code, 0)
        for line in (
            "开关页面已配置：否",
            "开关页面域名：无",
            "订阅链接已配置：否",
            "来源域名：无",
            "总量：未获取",
            "已用：未获取",
            "剩余：未获取",
            "到期时间：未获取",
            "最近成功时间：无",
            "AmyTelecom.yaml：不存在",
        ):
            self.assertIn(line, stdout)

    def test_airport_status_renders_a_zero_expiry_as_never_expiring(self):
        self.service.airport_status_value = dict(
            self.service.airport_status_value, traffic_expiry_ms=0
        )

        code, stdout, stderr = run_cli(None, self.service, stdin_text="1\n5\n\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertIn("到期时间：未设置", stdout)
        self.assertNotIn("1970", stdout)

    def test_scheduled_refresh_command_exits_zero_on_success_and_normal_skips(self):
        for result in (
            {"updated": True, "traffic_captured": True},
            {"updated": False, "skipped": True},
        ):
            with self.subTest(result=result):
                service = FakeService()
                service.airport_auto_result = result
                code, stdout, stderr = run_cli(["airport-scheduled-refresh"], service)

                self.assertEqual(code, 0)
                self.assertEqual(service.calls, [("auto_refresh_airport", ())])
                self.assertNotIn(("sync_all", ()), service.calls)
                self.assertNotIn("https://", stdout + stderr)

    def test_scheduled_refresh_treats_busy_or_missing_activation_as_success(self):
        for error_code in (
            "operation_busy",
            "airport_activation_missing",
            "airport_refresh_skipped",
        ):
            with self.subTest(error_code=error_code):
                service = FakeService()
                service.auto_refresh_error = ServiceError(error_code)
                code, stdout, stderr = run_cli(["airport-scheduled-refresh"], service)

                self.assertEqual(code, 0)

    def test_scheduled_refresh_fails_on_real_errors(self):
        service = FakeService()
        service.auto_refresh_error = ServiceError("airport_portal_unsupported")

        code, stdout, stderr = run_cli(["airport-scheduled-refresh"], service)

        self.assertEqual(code, 1)
        self.assertIn("airport_portal_unsupported", stderr)

    def test_scheduled_refresh_rejects_arguments_without_echoing_them(self):
        for arguments in (
            ["airport-scheduled-refresh", "anything"],
            ["airport-scheduled-refresh", "https://portal.example/secret"],
        ):
            with self.subTest(arguments=arguments[1:]):
                code, stdout, stderr = run_cli(arguments, self.service)

                self.assertEqual(code, 2)
                self.assertIn("invalid_command", stderr)
                self.assertNotIn("portal.example", stdout + stderr)
                self.assertNotIn("anything", stdout + stderr)
                self.assertEqual(self.service.calls, [])

    def test_menu_options_two_through_four_call_sync_links_and_status(self):
        sync = FakeService()
        links = FakeService()
        status = FakeService()

        self.assertEqual(run_cli(None, sync, stdin_text="2\n")[0], 0)
        self.assertEqual(run_cli(None, links, stdin_text="3\n")[0], 0)
        code, output, _ = run_cli(None, status, stdin_text="4\n")

        self.assertEqual(sync.calls, [("sync_all", ())])
        self.assertEqual(links.calls, [("links", ())])
        self.assertEqual(code, 0)
        self.assertEqual(status.calls, [("status", ())])
        self.assertIn("状态", output)
        self.assertNotIn("历史版本", output)

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
                self.assertTrue(stdout.startswith(MENU if argv is None else ""))
                self.assertIn("同步部分完成。\n", stdout)
                self.assertEqual(stderr, "客户端 ID 8（错误代码：member_update_failed）\n")
                self.assertNotIn("member@example.test", stdout + stderr)

        code, stdout, stderr = run_cli(["sync"], FakeService())

        self.assertEqual(code, 0)
        self.assertEqual(stdout, "同步已完成。\n")
        self.assertEqual(stderr, "")

    def test_home_import_stays_invalid_and_no_menu_offers_a_home_upload(self):
        menus = MENU + MAINTENANCE_MENU + CERT_MENU + BACKUP_MENU + USER_MENU
        for item in ("home", "上传", "导入", "sftp", "SFTP"):
            self.assertNotIn(item, menus)
        for argv in (["home-import"], ["home-import", "7"], ["upload-home"]):
            with self.subTest(argv=argv):
                service = FakeService()

                code, stdout, stderr = run_cli(argv, service)

                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "操作失败（错误代码：invalid_command）\n")
                self.assertEqual(service.calls, [])

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

    def test_reinitialize_owner_is_noninteractive_and_also_reachable_from_the_menu(self):
        code, stdout, stderr = run_cli(["reinitialize-owner", "9"], self.service)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout, "所有者已重新初始化；请执行 sync。\n")
        self.assertIn("重新初始化 owner", USER_MENU)
        self.assertNotIn("template-sync", MENU + MAINTENANCE_MENU + CERT_MENU + BACKUP_MENU + USER_MENU)
        self.assertNotIn("post-update", MENU + MAINTENANCE_MENU + CERT_MENU + BACKUP_MENU + USER_MENU)

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


class MenuLoopTests(unittest.TestCase):
    """The menu tree: looping, submenus, confirmations, and update exits."""

    def setUp(self):
        self.service = FakeService()

    def test_main_menu_loops_until_zero_and_dispatches_each_action_once(self):
        code, stdout, stderr = run_cli(
            None, self.service, stdin_text="3\n\n2\n\n4\n\n0\n"
        )

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count(MENU), 4)
        self.assertEqual(
            self.service.calls,
            [("links", ()), ("sync_all", ()), ("status", ())],
        )
        self.assertEqual(stdout.count("按回车键返回当前菜单："), 3)

    def test_main_menu_invalid_or_empty_selection_stays_and_redisplays(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="99\n\nabc\n9\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(stderr.count("操作失败（错误代码：invalid_menu_selection）\n"), 4)
        self.assertEqual(stdout.count(MENU), 5)
        self.assertEqual(self.service.calls, [])

    def test_each_menu_prompt_lists_its_own_range(self):
        cases = (
            ("0\n", MENU, "请输入选项 [0-8]："),
            ("1\n0\n0\n", AIRPORT_MENU, "请输入选项 [0-5]："),
            ("5\n0\n0\n", MAINTENANCE_MENU, "请输入选项 [0-3]："),
            ("6\n0\n0\n", CERT_MENU, "请输入选项 [0-2]："),
            ("7\n0\n0\n", BACKUP_MENU, "请输入选项 [0-3]："),
            ("8\n0\n0\n", USER_MENU, "请输入选项 [0-4]："),
        )
        for stdin_text, menu, prompt in cases:
            with self.subTest(prompt=prompt):
                code, stdout, stderr = run_cli(None, self.service, stdin_text=stdin_text)

                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertIn(prompt, stdout)
                self.assertEqual(stdout.count(menu), 1)

    def test_submenus_return_to_the_main_menu_on_zero(self):
        for entry, menu in (
            ("5", MAINTENANCE_MENU),
            ("6", CERT_MENU),
            ("7", BACKUP_MENU),
            ("8", USER_MENU),
        ):
            with self.subTest(entry=entry):
                code, stdout, stderr = run_cli(
                    None, self.service, stdin_text="%s\n0\n0\n" % entry
                )

                self.assertEqual(code, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(stdout.count(menu), 1)
                self.assertEqual(stdout.count(MENU), 2)

    def test_submenu_eof_exits_quietly(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n")

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(stdout.count(MENU), 1)
        self.assertEqual(self.service.calls, [])

    def test_submenu_ctrl_c_exits_quietly(self):
        code = main(
            None,
            stdin=InterruptingAfterFirstLine("5\n"),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
            service_factory=lambda: self.service,
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [])

    def test_submenu_invalid_selection_stays_in_the_submenu(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="5\nx\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(stderr.count("操作失败（错误代码：invalid_menu_selection）\n"), 1)
        self.assertEqual(stdout.count(MAINTENANCE_MENU), 2)
        self.assertEqual(stdout.count(MENU), 2)

    def test_cert_menu_status_and_backup_menu_dispatch_to_manage_actions(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "cert_status", return_value={"present": True, "not_after": "Sep 25 12:00:00 2026 GMT"}
        ) as cert_status, patch.object(
            manage, "create_backup", return_value=Path("/backups/x.tar.gz")
        ) as backup:
            code, stdout, stderr = run_cli(
                None, self.service, stdin_text="6\n1\n\n0\n7\n1\n\n0\n0\n"
            )

        self.assertEqual(code, 0)
        self.assertEqual(cert_status.call_count, 1)
        self.assertEqual(backup.call_count, 1)
        self.assertIn("证书存在：是", stdout)

    def test_menu_mihomo_upgrade_requires_confirmation(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage,
            "update_mihomo",
            return_value={"changed": True, "version": "v1.19.28"},
            create=True,
        ) as update:
            cancelled, _, _ = run_cli(None, self.service, stdin_text="5\n3\nn\n\n0\n0\n")
            confirmed, stdout, _ = run_cli(None, self.service, stdin_text="5\n3\ny\n\n0\n0\n")

        self.assertEqual(cancelled, 0)
        self.assertEqual(confirmed, 0)
        self.assertEqual(update.call_count, 1)
        self.assertIn("v1.19.28", stdout)

    def test_mihomo_update_command_dispatches_noninteractively(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage,
            "update_mihomo",
            return_value={"changed": False, "version": "v1.19.28"},
            create=True,
        ) as update:
            code, stdout, stderr = run_cli(["mihomo-update"], self.service)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertEqual(update.call_count, 1)
        self.assertIn("v1.19.28", stdout)

    def test_menu_cert_renew_requires_confirmation_and_cancel_has_no_side_effect(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "cert_renew", return_value=True
        ) as renew:
            cancelled, _, _ = run_cli(None, self.service, stdin_text="6\n2\nn\n\n0\n0\n")
            confirmed, _, _ = run_cli(None, self.service, stdin_text="6\n2\ny\n\n0\n0\n")

        self.assertEqual(cancelled, 0)
        self.assertEqual(confirmed, 0)
        self.assertEqual(renew.call_count, 1)

    def test_history_view_is_a_standalone_entry_without_rollback(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="8\n1\n7\n\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("history", (7,))])
        self.assertIn("用户 7 的历史版本", stdout)

    def test_rollback_entry_prompts_for_each_value(self):
        code, stdout, stderr = run_cli(
            None, self.service, stdin_text="8\n2\n7\nrelease-7\ny\n\n0\n0\n"
        )

        self.assertEqual(code, 0)
        self.assertEqual(
            self.service.calls,
            [("history", (7,)), ("rollback", (7, "release-7"))],
        )
        self.assertIn("用户 7 的历史版本", stdout)

    def test_rollback_without_release_returns_without_side_effect(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="8\n2\n7\n\n\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("history", (7,))])

    def test_rollback_confirmation_cancel_keeps_zero_side_effects(self):
        code, stdout, stderr = run_cli(
            None, self.service, stdin_text="8\n2\n7\nrelease-7\nn\n\n0\n0\n"
        )

        self.assertEqual(code, 0)
        self.assertEqual(self.service.calls, [("history", (7,))])

    def test_menu_rotate_link_requires_confirmation_and_cancel_keeps_tokens(self):
        confirmed_code, _, _ = run_cli(None, self.service, stdin_text="8\n3\n7\ny\n\n0\n0\n")
        cancelled_code, _, _ = run_cli(None, self.service, stdin_text="8\n3\n7\nn\n\n0\n0\n")

        self.assertEqual(confirmed_code, 0)
        self.assertEqual(cancelled_code, 0)
        self.assertEqual(self.service.calls, [("rotate_link", (7,))])

    def test_menu_reinitialize_owner_requires_confirmation(self):
        confirmed_code, _, _ = run_cli(None, self.service, stdin_text="8\n4\n9\ny\n\n0\n0\n")
        cancelled_code, _, _ = run_cli(None, self.service, stdin_text="8\n4\n9\nn\n\n0\n0\n")

        self.assertEqual(confirmed_code, 0)
        self.assertEqual(cancelled_code, 0)
        self.assertEqual(self.service.calls, [("reinitialize_owner", (9,))])

    def test_menu_install_rollback_requires_the_exact_confirmation_text(self):
        class FakeInstaller:
            def __init__(self, root):
                pass

            def rollback_install(self):
                self.service_calls.append("rollback_install")

        FakeInstaller.service_calls = []
        with patch("clash_sub.cli.os.geteuid", return_value=0), patch(
            "clash_sub.cli.Installer", FakeInstaller
        ):
            cancelled_code, _, _ = run_cli(None, self.service, stdin_text="7\n3\nrollback\n\n0\n0\n")
            confirmed_code, _, _ = run_cli(None, self.service, stdin_text="7\n3\nROLLBACK\n\n0\n0\n")

        self.assertEqual(cancelled_code, 0)
        self.assertEqual(confirmed_code, 0)
        self.assertEqual(FakeInstaller.service_calls, ["rollback_install"])

    def test_menu_update_prints_the_sync_reminder_and_exits_the_menu(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_update", return_value=True
        ):
            code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n2\n")

        self.assertEqual(code, 0)
        self.assertEqual(stdout.count(MENU), 1)
        self.assertEqual(stdout.count(MAINTENANCE_MENU), 1)
        self.assertIn("代码更新完成。", stdout)
        self.assertIn("clash-sub update && clash-sub sync", stdout)
        self.assertEqual(self.service.calls, [])

    def test_menu_update_failure_exits_the_menu_without_sync(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_update", side_effect=RuntimeError("git_pull_failed")
        ):
            code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n2\n0\n")

        self.assertEqual(code, 1)
        self.assertEqual(stdout.count(MENU), 1)
        self.assertIn("git_pull_failed", stderr)
        self.assertEqual(self.service.calls, [])

    def test_menu_update_and_sync_spawns_a_fresh_process_for_sync(self):
        from clash_sub import manage

        spawned = []

        def fake_run(arguments, **kwargs):
            spawned.append(list(arguments))
            return SimpleNamespace(returncode=0)

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_update", return_value=True
        ), patch("clash_sub.cli.subprocess.run", side_effect=fake_run):
            code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n1\n")

        self.assertEqual(code, 0)
        self.assertEqual(stdout.count(MENU), 1)
        self.assertEqual(len(spawned), 1)
        self.assertEqual(spawned[0][-1], "sync")
        self.assertTrue(spawned[0][1].endswith("bin/clash-sub"))
        self.assertTrue(spawned[0][0].endswith(".venv/bin/python"))
        self.assertIn("clash-sub update && clash-sub sync", stdout)

    def test_menu_update_and_sync_never_spawns_sync_when_update_fails(self):
        from clash_sub import manage

        spawned = []

        def fake_run(arguments, **kwargs):
            spawned.append(list(arguments))
            return SimpleNamespace(returncode=0)

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_update", side_effect=RuntimeError("git_pull_failed")
        ), patch("clash_sub.cli.subprocess.run", side_effect=fake_run):
            code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n1\n0\n")

        self.assertEqual(spawned, [])
        self.assertEqual(code, 1)
        self.assertIn("git_pull_failed", stderr)

    def test_menu_update_and_sync_reports_a_failing_sync_process(self):
        from clash_sub import manage

        def fake_run(arguments, **kwargs):
            return SimpleNamespace(returncode=1)

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_update", return_value=True
        ), patch("clash_sub.cli.subprocess.run", side_effect=fake_run):
            code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n1\n")

        self.assertEqual(code, 1)
        self.assertIn("menu_sync_failed", stderr)

    def test_menu_update_and_sync_timeout_returns_stable_error_and_exits(self):
        from clash_sub import manage

        def fake_run(arguments, **kwargs):
            raise subprocess.TimeoutExpired(arguments, 900)

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_update", return_value=True
        ), patch("clash_sub.cli.subprocess.run", side_effect=fake_run):
            code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n1\n0\n")

        self.assertEqual(code, 1)
        self.assertEqual(stdout.count(MENU), 1)
        self.assertIn("menu_sync_failed", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_menu_update_and_sync_missing_entry_returns_stable_error_and_exits(self):
        from clash_sub import manage

        def fake_run(arguments, **kwargs):
            raise FileNotFoundError("venv python disappeared")

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_update", return_value=True
        ), patch("clash_sub.cli.subprocess.run", side_effect=fake_run):
            code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n1\n0\n")

        self.assertEqual(code, 1)
        self.assertEqual(stdout.count(MENU), 1)
        self.assertIn("menu_sync_failed", stderr)
        self.assertNotIn("disappeared", stderr)

    def test_menu_update_and_sync_does_not_duplicate_sync_output(self):
        from clash_sub import manage

        def fake_run(arguments, **kwargs):
            return SimpleNamespace(returncode=0)

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_update", return_value=True
        ), patch("clash_sub.cli.subprocess.run", side_effect=fake_run):
            code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n1\n")

        self.assertEqual(code, 0)
        self.assertEqual(stdout.count("同步已完成"), 0)

    def test_menu_recover_requires_root(self):
        with patch("clash_sub.cli.os.geteuid", return_value=1000):
            code, stdout, stderr = run_cli(None, self.service, stdin_text="7\n2\n0\n")

        self.assertEqual(code, 1)
        self.assertIn("recovery_not_authorized", stderr)


class MenuColorTests(unittest.TestCase):
    """ANSI colors appear only on a TTY and never around copyable values."""

    def setUp(self):
        self.service = FakeService()

    def test_plain_streams_never_emit_ansi(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="99\n3\n\n5\n0\n0\n")

        self.assertEqual(code, 0)
        self.assertNotIn("\033[", stdout)
        self.assertNotIn("\033[", stderr)

    def test_noninteractive_command_output_stays_plain_even_on_a_tty(self):
        code, stdout, stderr = run_cli(["links"], self.service, tty=True)

        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertNotIn("\033[", stdout)

    def test_tty_menu_colors_the_title_numbers_and_recommendation_green(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="5\n0\n0\n", tty=True)

        self.assertEqual(code, 0)
        self.assertIn(GREEN + "clash-sub 管理脚本" + RESET, stdout)
        self.assertIn(GREEN + "5." + RESET, stdout)
        self.assertIn(GREEN + "1." + RESET, stdout)
        self.assertIn(GREEN + "（推荐）" + RESET, stdout)

    def test_tty_pause_prompt_is_yellow(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="3\n\n0\n", tty=True)

        self.assertEqual(code, 0)
        self.assertIn(YELLOW + "按回车键返回当前菜单：" + RESET, stdout)

    def test_tty_invalid_selection_is_red(self):
        code, stdout, stderr = run_cli(None, self.service, stdin_text="99\n0\n", tty=True)

        self.assertEqual(code, 0)
        self.assertIn(RED + "操作失败（错误代码：invalid_menu_selection）", stderr)
        self.assertIn("invalid_menu_selection）\n" + RESET, stderr)

    def test_tty_marks_danger_keywords_red(self):
        code, stdout, stderr = run_cli(
            None, self.service, stdin_text="6\n0\n7\n0\n8\n0\n0\n", tty=True
        )

        self.assertEqual(code, 0)
        for keyword in ("回滚", "回退", "轮换", "重新初始化", "强制续期"):
            with self.subTest(keyword=keyword):
                self.assertIn(RED + keyword + RESET, stdout)

    def test_rotated_urls_stay_uncolored_on_a_tty(self):
        rotated_url = "https://sub.example.test:8443/s/%s/Clash-Compat.yaml" % ROTATED_TOKEN

        code, stdout, stderr = run_cli(
            None, self.service, stdin_text="8\n3\n7\ny\n\n0\n0\n", tty=True
        )

        self.assertEqual(code, 0)
        self.assertIn(rotated_url, stdout)
        self.assertNotIn("\033[" + ROTATED_TOKEN, stdout)
        self.assertNotIn(ROTATED_TOKEN + "\033", stdout)


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
        ), patch(
            "clash_sub.cli.os.geteuid", return_value=0
        ):
            status = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 2)
        self.assertIn("invalid_domain", self.stderr.getvalue())

    def test_install_reads_the_cf_token_visibly(self):
        # The Cloudflare token is typed as normal visible input (pasted
        # whitespace stripped); hiding it made paste failures invisible.
        captured = {}

        class FakeInstaller:
            def __init__(self, root, print_fn=None, progress_offset=0):
                pass

            def install(self, **kwargs):
                captured["kwargs"] = kwargs
                return {"panel_url": "https://sub.example.com/x/", "gate_instruction": ""}

        stdout = io.StringIO()
        with patch.dict(
            "os.environ", {"CLASH_SUB_DOMAIN": "example.com"}, clear=False
        ), patch(
            "builtins.input", side_effect=["  tok-visible  ", ""]
        ), patch(
            "clash_sub.cli.getpass", side_effect=AssertionError("token input must be visible")
        ), patch(
            "clash_sub.cli._suggest_owner_email", return_value="owner@x"
        ), patch(
            "clash_sub.cli.Installer", FakeInstaller
        ), patch(
            "clash_sub.cli.os.geteuid", return_value=0
        ):
            status = main(["install"], stdout=stdout, stderr=self.stderr)

        self.assertEqual(status, 0)
        self.assertEqual(captured["kwargs"]["cf_token"], "tok-visible")
        self.assertIn("请输入 Cloudflare API Token：", stdout.getvalue())

    def test_install_prompts_owner_with_snapshot_default(self):
        captured = {}

        class FakeInstaller:
            def __init__(self, root, print_fn=None, progress_offset=0):
                captured["print_fn"] = print_fn
                captured["progress_offset"] = progress_offset

            def install(self, **kwargs):
                captured["kwargs"] = kwargs
                return {"panel_url": "https://sub.example.com/xui7k2m/", "gate_instruction": ""}

        with patch.dict(
            "os.environ", {"CLASH_SUB_DOMAIN": "example.com"}, clear=False
        ), patch(
            "builtins.input", side_effect=["tok", ""]
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
            def __init__(self, root, print_fn=None, progress_offset=0):
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
            "builtins.input", side_effect=["tok", ""]
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
            "builtins.input", side_effect=["tok", EOFError]
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
            def __init__(self, root, print_fn=None, progress_offset=0):
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
            def __init__(self, root, print_fn=None, progress_offset=0):
                pass

            def install(self, **kwargs):
                captured["kwargs"] = kwargs
                return {"panel_url": "https://sub.example.com/xui7k2m/", "gate_instruction": ""}

        with patch.dict(
            "os.environ",
            {"CLASH_SUB_DOMAIN": "example.com", "CLASH_SUB_OWNER_EMAIL": "env-owner@x"},
            clear=False,
        ), patch(
            "builtins.input", return_value="tok"
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

    def test_install_passes_safe_progress_offset_without_echoing_secrets(self):
        captured = {}

        class FakeInstaller:
            def __init__(self, root, print_fn=None, progress_offset=0):
                captured["progress_offset"] = progress_offset

            def install(self, **kwargs):
                return {"panel_url": "", "gate_instruction": ""}

        stdout = io.StringIO()
        with patch.dict(
            "os.environ",
            {
                "CLASH_SUB_DOMAIN": "example.com",
                "CLASH_SUB_OWNER_EMAIL": "owner@x",
                "CLASH_SUB_PROGRESS_OFFSET": "3",
            },
            clear=False,
        ), patch("builtins.input", return_value="tok"), patch(
            "clash_sub.cli.Installer", FakeInstaller
        ), patch("clash_sub.cli.os.geteuid", return_value=0):
            status = main(["install"], stdout=stdout, stderr=self.stderr)

        self.assertEqual(status, 0)
        self.assertEqual(captured["progress_offset"], 3)
        self.assertNotIn("owner@x", stdout.getvalue())
        self.assertNotIn("tok", stdout.getvalue())

    def test_install_ignores_malformed_progress_offset_and_prints_heading(self):
        captured = {}

        class FakeInstaller:
            def __init__(self, root, print_fn=None, progress_offset=0):
                captured["progress_offset"] = progress_offset

            def install(self, **kwargs):
                return {"panel_url": "", "gate_instruction": ""}

        stdout = io.StringIO()
        with patch.dict(
            "os.environ",
            {
                "CLASH_SUB_DOMAIN": "example.com",
                "CLASH_SUB_OWNER_EMAIL": "owner@x",
                "CLASH_SUB_PROGRESS_OFFSET": "not-a-number",
            },
            clear=False,
        ), patch("builtins.input", return_value="tok"), patch(
            "clash_sub.cli.Installer", FakeInstaller
        ), patch("clash_sub.cli.os.geteuid", return_value=0):
            status = main(["install"], stdout=stdout, stderr=self.stderr)

        self.assertEqual(status, 0)
        self.assertEqual(captured["progress_offset"], 0)
        self.assertIn("clash-sub 安装程序", stdout.getvalue())

    def test_install_failure_includes_retry_guidance(self):
        class FakeInstaller:
            def __init__(self, root, print_fn=None, progress_offset=0):
                pass

            def install(self, **kwargs):
                raise InstallerError("certificate_issue_failed")

        with patch.dict(
            "os.environ",
            {"CLASH_SUB_DOMAIN": "example.com", "CLASH_SUB_OWNER_EMAIL": "owner@x"},
            clear=False,
        ), patch("builtins.input", return_value="tok"), patch(
            "clash_sub.cli.Installer", FakeInstaller
        ), patch("clash_sub.cli.os.geteuid", return_value=0):
            status = main(["install"], stdout=io.StringIO(), stderr=self.stderr)

        self.assertEqual(status, 1)
        self.assertIn("certificate_issue_failed", self.stderr.getvalue())
        self.assertIn("修正问题后重新执行：bash install.sh", self.stderr.getvalue())

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

    def test_update_post_update_flag_dispatches_to_post(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_post_update", return_value=True
        ) as post, patch.object(
            manage, "run_update", return_value=True
        ) as update:
            status = main(["update", "--post-update"], stdout=self.stdout, stderr=self.stderr)

        self.assertEqual(status, 0)
        post.assert_called_once()
        update.assert_not_called()

    def test_update_dispatches_run_update(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_post_update", return_value=True
        ) as post, patch.object(
            manage, "run_update", return_value=True
        ) as update:
            status = main(["update"], stdout=self.stdout, stderr=self.stderr)

        self.assertEqual(status, 0)
        update.assert_called_once()
        post.assert_not_called()

    def test_update_success_output_names_the_followup_sync_command_exactly(self):
        from clash_sub import manage

        with patch("clash_sub.cli.os.geteuid", return_value=0), patch.object(
            manage, "run_update", return_value=True
        ):
            status = main(["update"], stdout=self.stdout, stderr=self.stderr)

        self.assertEqual(status, 0)
        self.assertIn("代码更新完成。", self.stdout.getvalue())
        self.assertIn("如果本次修改涉及模板或生成逻辑，请继续执行：", self.stdout.getvalue())
        self.assertIn("clash-sub sync", self.stdout.getvalue())
        self.assertIn("clash-sub update && clash-sub sync", self.stdout.getvalue())


if __name__ == "__main__":
    unittest.main()


class TemplateSyncCommandTests(unittest.TestCase):
    def test_parser_accepts_compat_without_balance(self):
        parsed = _parser().parse_args(["template-sync", "--compat", "/tmp/Clash-Compat.yaml"])
        self.assertEqual(parsed.compat, Path("/tmp/Clash-Compat.yaml")); self.assertIsNone(parsed.balance)

    def test_no_argument_command_selects_both_default_sources(self):
        report = template_sync.TemplateSyncReport((), ("Compat 基础：无变化",), ())
        with patch.object(template_sync, "run_template_sync", return_value=report) as sync:
            code = main(["template-sync"], stdout=io.StringIO(), stderr=io.StringIO())
        sync.assert_called_once_with(Path(__file__).resolve().parents[1], None, None); self.assertEqual(code, 0)

    def test_compat_flag_selects_only_compat(self):
        report = template_sync.TemplateSyncReport((), (), ())
        with patch.object(template_sync, "run_template_sync", return_value=report) as sync:
            code = main(["template-sync", "--compat", "/tmp/Clash-Compat.yaml"], stdout=io.StringIO(), stderr=io.StringIO())
        sync.assert_called_once_with(Path(__file__).resolve().parents[1], Path("/tmp/Clash-Compat.yaml"), None); self.assertEqual(code, 0)

class ServiceCommandMessageTests(unittest.TestCase):
    def test_airport_replace_and_refresh_distinguish_captured_traffic(self):
        for operation, method in (
            ("airport-replace", "replace_airport_source"),
            ("airport-refresh", "refresh_airport"),
        ):
            for captured, message in (
                (True, "机场订阅已更新。\n"),
                (False, "机场订阅已更新，未获取到流量信息。\n"),
            ):
                with self.subTest(operation=operation, captured=captured):
                    service = MagicMock()
                    getattr(service, method).return_value = {
                        "updated": True,
                        "traffic_captured": captured,
                    }
                    stdout = io.StringIO()

                    value = "https://airport.example/sub" if operation == "airport-replace" else None
                    code = _call(operation, value, stdout, io.StringIO(), factory=lambda: service)

                    self.assertEqual(code, 0)
                    if operation == "airport-replace":
                        service.replace_airport_source.assert_called_once_with("https://airport.example/sub")
                    else:
                        service.refresh_airport.assert_called_once_with()
                    self.assertEqual(stdout.getvalue(), message)

    def test_reinitialize_message_instructs_sync_without_a_new_import(self):
        service = MagicMock()
        stdout = io.StringIO()

        code = _call("reinitialize", 7, stdout, io.StringIO(), factory=lambda: service)

        self.assertEqual(code, 0)
        service.reinitialize_owner.assert_called_once_with(7)
        self.assertEqual(stdout.getvalue(), "所有者已重新初始化；请执行 sync。\n")


class MetadataServeCommandTests(unittest.TestCase):
    """The internal systemd entry: wired, hidden from the menus, injectable."""

    def test_metadata_serve_is_a_parser_command_absent_from_every_menu(self):
        parsed = _parser().parse_args(["metadata-serve"])

        self.assertEqual(parsed.command, "metadata-serve")
        menus = MENU + AIRPORT_MENU + MAINTENANCE_MENU + CERT_MENU + BACKUP_MENU + USER_MENU
        self.assertNotIn("metadata-serve", menus)

    def test_metadata_serve_builds_and_serves_through_the_injected_seam(self):
        constructed = []
        listener = object()
        store = object()
        served = []

        with patch(
            "clash_sub.cli.metadata_server.listener_from_environment",
            return_value=listener,
        ) as listener_factory, patch(
            "clash_sub.cli._default_metadata_store", return_value=store
        ) as build_store, patch(
            "clash_sub.cli.metadata_server.serve",
            side_effect=lambda store, listener: served.append((store, listener)),
        ):
            code = main(
                ["metadata-serve"],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
                service_factory=lambda: constructed.append(object()),
            )

        self.assertEqual(code, 0)
        listener_factory.assert_called_once_with()
        build_store.assert_called_once_with()
        self.assertEqual(served, [(store, listener)])
        self.assertEqual(constructed, [])

    def test_metadata_serve_failure_reports_one_stable_code_without_details(self):
        with patch(
            "clash_sub.cli.metadata_server.listener_from_environment",
            side_effect=RuntimeError("boom http://secret"),
        ):
            stderr = io.StringIO()
            code = main(
                ["metadata-serve"],
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=stderr,
            )

        self.assertEqual(code, 1)
        self.assertEqual(stderr.getvalue(), "操作失败（错误代码：metadata_serve_failed）\n")
