import io
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from clash_sub import template_sync
from clash_sub.cli import _call, _parser, main

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
    def test_airport_command_updates_the_provider_only(self):
        service = MagicMock()
        stdout = io.StringIO()

        code = _call("airport", "https://airport.example/sub", stdout, io.StringIO(), factory=lambda: service)

        self.assertEqual(code, 0)
        service.update_airport.assert_called_once_with("https://airport.example/sub")
        self.assertEqual(stdout.getvalue(), "机场订阅已更新。\n")

    def test_reinitialize_message_instructs_sync_without_a_new_import(self):
        service = MagicMock()
        stdout = io.StringIO()

        code = _call("reinitialize", 7, stdout, io.StringIO(), factory=lambda: service)

        self.assertEqual(code, 0)
        service.reinitialize_owner.assert_called_once_with(7)
        self.assertEqual(stdout.getvalue(), "所有者已重新初始化；请执行 sync。\n")
