"""Certificate inspection and alerting tests.

`scripts/check_certificate.py` inspects the configured fullchain file
through openssl without a shell, persists a sanitized state document,
runs the configured alert argv directly when the certificate is
expiring, unreadable, invalid, or its renewal is marked failed, and
suppresses duplicate identical alerts for twelve hours.  Nothing it
emits may contain subject names, SANs, authorities, emails, paths, or
alert-command arguments.
"""

import importlib.util
import json
import os
import stat
import subprocess
import sys
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]

FIXED_NOW_TEXT = "2026-08-21T12:00:00+00:00"


def load_script(name):
    module_name = "scripts_%s" % name
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "scripts" / ("%s.py" % name)
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


check_certificate = load_script("check_certificate")

inspect_certificate = check_certificate.inspect_certificate
check_and_alert = check_certificate.check_and_alert
mark_renewal = check_certificate.mark_renewal
load_state = check_certificate.load_state

from clash_sub.host_cli import CERTIFICATE_STATUS_FIELDS


def _fixed_now():
    from datetime import datetime, timezone

    return datetime.fromisoformat(FIXED_NOW_TEXT)


class FakeRunner:
    """Canned openssl answers plus direct alert-command recording."""

    def __init__(self, scenarios=None):
        self.commands = []
        self.shell_used = False
        self.scenarios = dict(scenarios or {})

    def run(self, argv, timeout=None):
        self.commands.append(list(argv))
        if os.path.basename(argv[0]) == "openssl":
            key = str(argv[argv.index("-in") + 1])
            scenario = self.scenarios.get(key)
            if scenario is None:
                scenario = {
                    "expiring": "expiring",
                    "expired": "expired",
                    "invalid": "invalid",
                    "bad": "invalid",
                    "missing": "missing",
                }.get(Path(key).stem, "valid")
            return self._openssl_result(scenario, argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    @staticmethod
    def _openssl_result(scenario, argv):
        if scenario == "expiring":
            if "-checkend" in argv:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(
                returncode=0,
                stdout="notAfter=Mon Aug 24 12:00:00 2026 GMT\n",
                stderr="",
            )
        if scenario == "expired":
            if "-checkend" in argv:
                return SimpleNamespace(returncode=1, stdout="", stderr="")
            return SimpleNamespace(
                returncode=0,
                stdout="notAfter=Mon Aug 10 12:00:00 2026 GMT\n",
                stderr="",
            )
        if scenario == "invalid":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="unable to load certificate\n",
            )
        if scenario == "missing":
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr=(
                    "error:02001002:system library:fopen:No such file "
                    "or directory\n"
                ),
            )
        if "-checkend" in argv:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout="notAfter=Mon Jan  1 00:00:00 2030 GMT\n",
            stderr="",
        )


ALERT_ARGV = ("notify-command", "--channel", "private")


class CertificateInspectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile

        cls._tempdir = tempfile.TemporaryDirectory()
        cls.directory = Path(cls._tempdir.name)
        cls.valid_cert = cls.directory / "valid.pem"
        cls.expiring_cert = cls.directory / "expiring.pem"
        cls._generate(cls.valid_cert, 3650)
        cls._generate(cls.expiring_cert, 3)
        cls.runner = check_certificate.SubprocessRunner()

    @classmethod
    def tearDownClass(cls):
        cls._tempdir.cleanup()

    @staticmethod
    def _generate(path, days):
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-keyout",
                str(path.with_suffix(".key")),
                "-out",
                str(path),
                "-days",
                str(days),
                "-subj",
                "/CN=example.com",
                "-addext",
                "subjectAltName=DNS:example.com,DNS:sub.example.com",
            ],
            check=True,
            capture_output=True,
        )

    def test_valid_certificate_reports_seconds_without_subject_names(self):
        report = inspect_certificate(self.valid_cert, runner=self.runner, now=_fixed_now())

        self.assertTrue(report.valid)
        self.assertGreater(report.remaining_seconds, 0)
        self.assertNotIn("example.com", json.dumps(report.to_json()))
        self.assertEqual(
            set(report.to_json()), {"valid", "remaining_seconds", "error_code"}
        )

    def test_expiring_certificate_reports_smaller_remaining_seconds(self):
        soon = inspect_certificate(
            self.expiring_cert, runner=self.runner, now=_fixed_now()
        )

        self.assertTrue(soon.valid)
        self.assertLess(soon.remaining_seconds, 4 * 24 * 3600)
        self.assertGreater(soon.remaining_seconds, 0)

    def test_unreadable_certificate_reports_stable_error_code(self):
        report = inspect_certificate(
            self.directory / "missing.pem", runner=self.runner, now=_fixed_now()
        )

        self.assertFalse(report.valid)
        self.assertEqual(report.error_code, "certificate_unreadable")
        self.assertEqual(report.remaining_seconds, 0)

    def test_wrong_format_file_reports_invalid_certificate(self):
        bad = self.directory / "garbage.pem"
        bad.write_text("this is not a certificate\n", encoding="utf-8")

        report = inspect_certificate(bad, runner=self.runner, now=_fixed_now())

        self.assertFalse(report.valid)
        self.assertEqual(report.error_code, "certificate_invalid")

    def test_no_shell_is_ever_used_for_openssl(self):
        runner = self.runner
        inspect_certificate(self.valid_cert, runner=runner, now=_fixed_now())

        self.assertFalse(runner.shell_used)
        for command in runner.commands:
            self.assertEqual(command[0], "openssl")
            self.assertNotIn(";", " ".join(command))


class CertificateAlertTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name)
        self.state_path = self.root / "state" / "certificate.json"
        self.state_path.parent.mkdir(parents=True)

    def test_expiring_or_failed_renewal_runs_alert_argv_without_shell(self):
        runner = FakeRunner()
        status = check_and_alert(
            "/synthetic/expiring.pem",
            ALERT_ARGV,
            runner=runner,
            now=_fixed_now(),
            threshold_seconds=14 * 24 * 3600,
        )

        self.assertTrue(status.alerted)
        self.assertIn(
            ["notify-command", "--channel", "private"],
            runner.commands,
        )
        self.assertFalse(runner.shell_used)

    def test_valid_certificate_with_healthy_renewal_never_alerts(self):
        runner = FakeRunner()

        status = check_and_alert(
            "/synthetic/valid.pem",
            ALERT_ARGV,
            runner=runner,
            now=_fixed_now(),
            threshold_seconds=14 * 24 * 3600,
            state_path=self.state_path,
        )

        self.assertFalse(status.alerted)
        self.assertTrue(status.valid)
        self.assertTrue(status.renewal_ok)
        alert_commands = [c for c in runner.commands if c[0] == "notify-command"]
        self.assertEqual(alert_commands, [])

    def test_state_file_is_written_atomically_with_mode_0600(self):
        check_and_alert(
            "/synthetic/valid.pem",
            ALERT_ARGV,
            runner=FakeRunner(),
            now=_fixed_now(),
            threshold_seconds=14 * 24 * 3600,
            state_path=self.state_path,
        )

        self.assertTrue(self.state_path.is_file())
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o600)
        self.assertEqual(list(self.state_path.parent.glob("*.tmp")), [])
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertIn("checked_at", state)
        self.assertIn("remaining_seconds", state)

    def test_duplicate_identical_alerts_are_suppressed_for_twelve_hours(self):
        runner = FakeRunner()
        first = check_and_alert(
            "/synthetic/expiring.pem",
            ALERT_ARGV,
            runner=runner,
            now=_fixed_now(),
            threshold_seconds=14 * 24 * 3600,
            state_path=self.state_path,
        )
        second = check_and_alert(
            "/synthetic/expiring.pem",
            ALERT_ARGV,
            runner=runner,
            now=_fixed_now() + timedelta(minutes=1),
            threshold_seconds=14 * 24 * 3600,
            state_path=self.state_path,
        )
        third = check_and_alert(
            "/synthetic/expiring.pem",
            ALERT_ARGV,
            runner=runner,
            now=_fixed_now() + timedelta(hours=12, minutes=1),
            threshold_seconds=14 * 24 * 3600,
            state_path=self.state_path,
        )

        self.assertTrue(first.alerted)
        self.assertFalse(second.alerted)
        self.assertTrue(third.alerted)
        alert_count = len([c for c in runner.commands if c[0] == "notify-command"])
        self.assertEqual(alert_count, 2)

    def test_renewal_failed_mark_triggers_alert_for_valid_certificate(self):
        mark_renewal(False, state_path=self.state_path, now=_fixed_now())
        runner = FakeRunner()

        status = check_and_alert(
            "/synthetic/valid.pem",
            ALERT_ARGV,
            runner=runner,
            now=_fixed_now(),
            threshold_seconds=14 * 24 * 3600,
            state_path=self.state_path,
        )

        self.assertFalse(status.renewal_ok)
        self.assertTrue(status.alerted)
        self.assertIn(
            ["notify-command", "--channel", "private"],
            runner.commands,
        )

    def test_unreadable_certificate_alerts_and_records_error_code(self):
        runner = FakeRunner(scenarios={"/synthetic/missing.pem": "missing"})

        status = check_and_alert(
            "/synthetic/missing.pem",
            ALERT_ARGV,
            runner=runner,
            now=_fixed_now(),
            threshold_seconds=14 * 24 * 3600,
            state_path=self.state_path,
        )

        self.assertFalse(status.valid)
        self.assertTrue(status.alerted)
        self.assertEqual(status.error_code, "certificate_unreadable")
        state = load_state(self.state_path)
        self.assertEqual(state["last_error_code"], "certificate_unreadable")

    def test_invalid_certificate_alerts_with_stable_error_code(self):
        runner = FakeRunner(scenarios={"/synthetic/bad.pem": "invalid"})

        status = check_and_alert(
            "/synthetic/bad.pem",
            ALERT_ARGV,
            runner=runner,
            now=_fixed_now(),
            threshold_seconds=14 * 24 * 3600,
            state_path=self.state_path,
        )

        self.assertEqual(status.error_code, "certificate_invalid")
        self.assertTrue(status.alerted)


class CertificateCliTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(self._tempdir.name)
        self.state_path = self.root / "state" / "certificate.json"
        self.state_path.parent.mkdir(parents=True)
        check_and_alert(
            "/synthetic/valid.pem",
            ALERT_ARGV,
            runner=FakeRunner(),
            now=_fixed_now(),
            threshold_seconds=14 * 24 * 3600,
            state_path=self.state_path,
        )

    def test_status_only_prints_exactly_the_seven_allowed_keys(self):
        import io

        stdout = io.StringIO()
        from contextlib import redirect_stdout

        with redirect_stdout(stdout):
            code = check_certificate.main(
                ["--status-only", "--state", str(self.state_path)]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(
            set(payload), {key for key, _ in CERTIFICATE_STATUS_FIELDS}
        )
        types = dict(CERTIFICATE_STATUS_FIELDS)
        self.assertIsInstance(payload["valid"], bool)
        self.assertIsInstance(payload["renewal_ok"], bool)
        self.assertIsInstance(payload["remaining_seconds"], int)
        for key in ("checked_at", "last_success_at", "last_alert_at", "error_code"):
            self.assertIsInstance(payload[key], str, key)

    def test_status_only_performs_no_mutation_and_runs_no_commands(self):
        import io

        before = self.state_path.read_bytes()
        stamp = self.state_path.stat().st_mtime_ns
        runner = FakeRunner()
        stdout = io.StringIO()
        from contextlib import redirect_stdout

        with redirect_stdout(stdout):
            check_certificate.main(
                ["--status-only", "--state", str(self.state_path)],
                runner=runner,
            )

        self.assertEqual(self.state_path.read_bytes(), before)
        self.assertEqual(self.state_path.stat().st_mtime_ns, stamp)
        self.assertEqual(runner.commands, [])

    def test_status_only_output_carries_no_private_values(self):
        import io

        stdout = io.StringIO()
        from contextlib import redirect_stdout

        with redirect_stdout(stdout):
            check_certificate.main(
                ["--status-only", "--state", str(self.state_path)]
            )

        text = stdout.getvalue()
        for forbidden in (
            "example.com",
            "admin@example",
            "notify-command",
            "fullchain",
            str(self.state_path),
            "/etc/letsencrypt",
        ):
            self.assertNotIn(forbidden, text)

    def test_status_only_without_state_reports_stable_error_code(self):
        import io

        stdout = io.StringIO()
        from contextlib import redirect_stdout

        with redirect_stdout(stdout):
            code = check_certificate.main(
                ["--status-only", "--state", str(self.root / "state" / "none.json")]
            )

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["error_code"], "state_missing")
        self.assertFalse(payload["valid"])

    def test_mark_renewal_updates_state_without_openssl(self):
        runner = FakeRunner()

        code = check_certificate.main(
            ["--mark-renewal", "failed", "--state", str(self.state_path)],
            runner=runner,
        )

        self.assertEqual(code, 0)
        state = load_state(self.state_path)
        self.assertFalse(state["renewal_ok"])
        self.assertEqual(runner.commands, [])


if __name__ == "__main__":
    unittest.main()
