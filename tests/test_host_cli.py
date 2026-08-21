import json
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from clash_sub.host_cli import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_USAGE,
    MAX_MANAGER_OUTPUT_BYTES,
    CommandRunner,
    ManagerError,
    ValidatorError,
    parse_manager_result,
    require_success_without_echoing_config,
    run_cli,
)


OWNER_VARIANTS = ["balanced", "balanced-win", "privacy"]
FRIEND_VARIANTS = ["balanced"]
AIRPORT_URL = "https://airport.example/temp/private-value"
AIRPORT_PROMPT = "Temporary airport subscription URL: "
OPERATION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"


class FakeRunner:
    """Injectable command runner: no Docker, no shell, records everything."""

    def __init__(
        self,
        users=None,
        failing_variant=None,
        failing_build_users=(),
        failing_import=False,
        failing_status=False,
        status_payload=None,
        history_payload=None,
        rollback_payload=None,
        rotation_payload=None,
        log_payload=None,
        certificate_payload=None,
    ):
        self.users = users if users is not None else {
            "owner": {"role": "owner", "variants": list(OWNER_VARIANTS)},
            "friend": {"role": "member", "variants": list(FRIEND_VARIANTS)},
        }
        self.failing_variant = failing_variant
        self.failing_build_users = set(failing_build_users)
        self.failing_import = failing_import
        self.failing_status = failing_status
        self.status_payload = status_payload
        self.history_payload = history_payload
        self.rollback_payload = rollback_payload
        self.rotation_payload = rotation_payload
        self.log_payload = log_payload
        self.certificate_payload = certificate_payload
        self.manager_actions = []
        self.manager_arguments = []
        self.stdin_texts = []
        self.validated_variants = []
        self.validated_paths = []
        self.operation_ids = []

    def manager(self, arguments, stdin_text=None):
        arguments = list(arguments)
        self.manager_arguments.append(arguments)
        if stdin_text is not None:
            self.stdin_texts.append(stdin_text)
        command = arguments[0]
        user = self._user_from(arguments)
        if command == "list-users":
            self.manager_actions.append("list-users")
            return {
                "users": [
                    {
                        "user_id": user_id,
                        "role": self.users[user_id]["role"],
                        "variants": list(self.users[user_id]["variants"]),
                    }
                    for user_id in sorted(self.users)
                ]
            }
        if command == "build":
            self.manager_actions.append("build:%s" % user)
            if user not in self.users:
                raise ManagerError("not_authorized")
            if user in self.failing_build_users:
                raise ManagerError("source_failed")
            operation_id = self._value_after(arguments, "--operation-id")
            self.operation_ids.append(operation_id)
            return {
                "user_id": user,
                "operation_id": operation_id,
                "variants": list(self.users[user]["variants"]),
                "candidate_path": "/staging/%s/%s" % (operation_id, user),
            }
        if command == "publish":
            self.manager_actions.append("publish:%s" % user)
            operation_id = self._value_after(arguments, "--operation-id")
            return {
                "user_id": user,
                "release_id": operation_id,
                "variants": list(self.users[user]["variants"]),
            }
        if command == "status":
            self.manager_actions.append("status")
            if self.failing_status:
                raise ManagerError("manager_unavailable")
            if self.status_payload is not None:
                return self.status_payload
            return {"users": {user_id: self._default_status(user_id) for user_id in sorted(self.users)}}
        if command == "history":
            self.manager_actions.append("history:%s" % user)
            if self.history_payload is not None:
                return self.history_payload
            return {
                "user_id": user,
                "releases": [
                    {
                        "release_id": "rel-b",
                        "variants": list(self.users[user]["variants"]),
                        "created_at": "2026-08-21T13:00:00Z",
                    },
                    {
                        "release_id": "rel-a",
                        "variants": list(self.users[user]["variants"]),
                        "created_at": "2026-08-21T12:00:00Z",
                    },
                ],
            }
        if command == "rollback":
            self.manager_actions.append("rollback:%s:%s" % (arguments[1], arguments[2]))
            if self.rollback_payload is not None:
                return self.rollback_payload
            return {
                "user_id": arguments[1],
                "release_id": arguments[2],
                "variants": list(self.users[arguments[1]]["variants"]),
            }
        if command == "rotate-token":
            self.manager_actions.append("rotate-token:%s" % user)
            if self.rotation_payload is not None:
                return self.rotation_payload
            return {
                "user_id": user,
                "token": "plain-token-value",
                "urls": {
                    variant: "https://sub.example.com:8443/s/%s/url-token-value.yaml" % variant
                    for variant in self.users[user]["variants"]
                },
            }
        if command == "import-airport":
            self.manager_actions.append("import-airport")
            if self.failing_import:
                raise ManagerError("source_failed")
            return {"imported": True, "owner_refresh_required": True}
        if command == "logs":
            self.manager_actions.append("logs:%s" % self._value_after(arguments, "--limit"))
            if self.log_payload is not None:
                return self.log_payload
            return {
                "entries": [
                    {
                        "timestamp": "2026-08-21T12:00:00Z",
                        "operation": "build",
                        "user_id": "friend",
                        "release_id": "op1",
                        "status": "success",
                    }
                ]
            }
        raise AssertionError("unexpected manager command %r" % command)

    def validate(self, candidate_path):
        variant = candidate_path.name[: -len(".yaml")]
        self.validated_variants.append(variant)
        self.validated_paths.append(str(candidate_path))
        if variant == self.failing_variant:
            raise ValidatorError("mihomo_validation_failed")

    def certificate_status(self):
        return self.certificate_payload

    def _default_status(self, user_id):
        return {
            "release_id": "op-%s-current" % user_id,
            "variants": list(self.users[user_id]["variants"]),
            "created_at": "2026-08-21T12:00:00Z",
            "needs_refresh": False,
            "traffic": {
                "upload": 10,
                "download": 20,
                "total": 100,
                "expire": 1893456000,
                "remaining": 70,
            },
        }

    def _user_from(self, arguments):
        if "--user" in arguments:
            return self._value_after(arguments, "--user")
        if arguments[0] in ("status", "history", "rollback", "rotate-token") and len(arguments) > 1:
            return arguments[1]
        return None

    def _value_after(self, arguments, flag):
        index = arguments.index(flag)
        return arguments[index + 1]


class HostCliTests(unittest.TestCase):
    def run_cli(self, arguments, runner=None, **kwargs):
        return run_cli(list(arguments), runner=runner or FakeRunner(), **kwargs)

    def test_no_arguments_prints_complete_help(self):
        result = self.run_cli([])
        self.assertEqual(result.returncode, 0)
        for command in (
            "status",
            "refresh",
            "airport",
            "history",
            "rollback",
            "rotate-link",
            "logs",
        ):
            self.assertIn(command, result.stdout)
        self.assertNotIn("refresh-all", result.stdout)

    def test_help_command_prints_the_same_help(self):
        result = self.run_cli(["help"])
        self.assertEqual(result.returncode, EXIT_OK)
        for command in ("status", "refresh", "airport", "history", "rollback", "rotate-link", "logs"):
            self.assertIn(command, result.stdout)

    def test_help_with_extra_arguments_still_prints_help(self):
        result = self.run_cli(["help", "extra"])
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("commands:", result.stdout)
        self.assertNotIn("unknown command", result.stderr)

    def test_unknown_command_exits_with_usage_code(self):
        result = self.run_cli(["frobnicate"])
        self.assertEqual(result.returncode, EXIT_USAGE)
        self.assertIn("unknown command", result.stderr)
        self.assertNotIn("refresh-all", result.stdout + result.stderr)

    def test_refresh_publishes_only_after_every_variant_validates(self):
        runner = FakeRunner()
        result = run_cli(["refresh", "owner"], runner=runner)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            runner.manager_actions,
            ["build:owner", "publish:owner"],
        )
        self.assertEqual(
            runner.validated_variants,
            ["balanced", "balanced-win", "privacy"],
        )

    def test_failed_variant_validation_never_publishes(self):
        runner = FakeRunner(failing_variant="privacy")
        result = run_cli(["refresh", "owner"], runner=runner)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(runner.manager_actions, ["build:owner"])

    def test_refresh_names_the_failing_variant_without_config_echo(self):
        runner = FakeRunner(failing_variant="balanced-win")
        result = run_cli(["refresh", "owner"], runner=runner)
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("mihomo_validation_failed", result.stderr)
        self.assertIn("balanced-win", result.stderr)
        self.assertNotIn("proxies", result.stderr.lower())

    def test_refresh_for_one_user_prints_only_safe_result_fields(self):
        result = self.run_cli(["refresh", "owner"], operation_id_factory=lambda: "op-1")
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(
            result.stdout.splitlines(),
            ["refresh owner: release=op-1 variants=balanced,balanced-win,privacy status=published"],
        )

    def test_refresh_rejects_more_than_one_user_id(self):
        result = self.run_cli(["refresh", "owner", "friend"])
        self.assertEqual(result.returncode, EXIT_USAGE)

    def test_refresh_without_user_refreshes_every_user_in_sorted_order(self):
        runner = FakeRunner()
        result = run_cli(["refresh"], runner=runner)
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(
            runner.manager_actions,
            [
                "list-users",
                "build:friend",
                "publish:friend",
                "build:owner",
                "publish:owner",
            ],
        )

    def test_refresh_all_users_isolates_per_user_failure(self):
        runner = FakeRunner(failing_build_users=("friend",))
        result = run_cli(["refresh"], runner=runner)
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("build:owner", runner.manager_actions)
        self.assertIn("publish:owner", runner.manager_actions)
        self.assertNotIn("publish:friend", runner.manager_actions)
        self.assertIn("refresh friend: error=source_failed", result.stderr)
        self.assertIn("status=published", result.stdout)

    def test_refresh_unknown_user_reports_manager_error_without_publish(self):
        runner = FakeRunner()
        result = run_cli(["refresh", "nobody"], runner=runner)
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertEqual(runner.manager_actions, ["build:nobody"])
        self.assertIn("not_authorized", result.stderr)

    def test_generated_operation_ids_are_unique_and_slug_safe(self):
        runner = FakeRunner()
        first = run_cli(["refresh", "owner"], runner=runner)
        second = run_cli(["refresh", "owner"], runner=runner)
        self.assertEqual(first.returncode, EXIT_OK)
        self.assertEqual(second.returncode, EXIT_OK)
        self.assertEqual(len(runner.operation_ids), 2)
        first_id, second_id = runner.operation_ids
        self.assertNotEqual(first_id, second_id)
        for operation_id in (first_id, second_id):
            self.assertRegex(operation_id, OPERATION_ID_PATTERN)

    def test_airport_reads_url_only_through_hidden_prompt_and_stdin(self):
        prompts = []

        def prompt(message):
            prompts.append(message)
            return AIRPORT_URL

        runner = FakeRunner()
        result = run_cli(["airport"], runner=runner, prompt=prompt)

        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(prompts, [AIRPORT_PROMPT])
        self.assertEqual(runner.stdin_texts, [AIRPORT_URL + "\n"])
        self.assertNotIn(AIRPORT_URL, json.dumps(runner.manager_arguments))
        self.assertNotIn(AIRPORT_URL, result.stdout)
        self.assertNotIn(AIRPORT_URL, result.stderr)
        self.assertEqual(
            runner.manager_actions,
            ["import-airport", "list-users", "build:owner", "publish:owner"],
        )

    def test_airport_rejects_extra_arguments(self):
        result = self.run_cli(["airport", "owner"], prompt=lambda _message: AIRPORT_URL)
        self.assertEqual(result.returncode, EXIT_USAGE)

    def test_failed_airport_import_preserves_state_without_refresh(self):
        runner = FakeRunner(failing_import=True)
        result = run_cli(["airport"], runner=runner, prompt=lambda _message: AIRPORT_URL)

        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertEqual(runner.manager_actions, ["import-airport"])
        self.assertIn("source_failed", result.stderr)
        self.assertNotIn(AIRPORT_URL, result.stderr)

    def test_cancelled_airport_prompt_touches_nothing(self):
        runner = FakeRunner()

        def prompt(_message):
            raise EOFError()

        result = run_cli(["airport"], runner=runner, prompt=prompt)

        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertEqual(runner.manager_actions, [])
        self.assertEqual(runner.stdin_texts, [])
        self.assertIn("cancelled", result.stderr)

    def test_status_shows_only_sanitized_manager_and_certificate_state(self):
        status_payload = {
            "users": {
                "owner": {
                    "release_id": "op-owner-current",
                    "variants": list(OWNER_VARIANTS),
                    "created_at": "2026-08-21T12:00:00Z",
                    "needs_refresh": True,
                    "traffic": {
                        "upload": 10,
                        "download": 20,
                        "total": 100,
                        "expire": 1893456000,
                        "remaining": 70,
                    },
                    "token_sha256": "deadbeefdeadbeef",
                    "source_url": "https://secret.example/sub",
                }
            }
        }
        certificate_payload = {
            "valid": True,
            "remaining_seconds": 3600,
            "checked_at": "2026-08-21T00:00:00Z",
            "fullchain_path": "/private/fullchain.pem",
        }
        runner = FakeRunner(
            status_payload=status_payload,
            certificate_payload=certificate_payload,
        )

        result = run_cli(["status"], runner=runner)

        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("manager: reachable", result.stdout)
        self.assertIn("release=op-owner-current", result.stdout)
        self.assertIn("variants=balanced,balanced-win,privacy", result.stdout)
        self.assertIn("generated=2026-08-21T12:00:00Z", result.stdout)
        self.assertIn("needs_refresh=yes", result.stdout)
        self.assertIn("remaining=70", result.stdout)
        self.assertIn("valid=true", result.stdout)
        self.assertIn("remaining_seconds=3600", result.stdout)
        self.assertNotIn("deadbeef", result.stdout)
        self.assertNotIn("secret.example", result.stdout)
        self.assertNotIn("fullchain", result.stdout.lower())
        self.assertNotIn("/private", result.stdout)

    def test_status_degrades_gracefully_without_certificate_state(self):
        result = self.run_cli(["status"])
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("manager: reachable", result.stdout)
        self.assertIn("certificate: state unavailable", result.stdout)
        self.assertIn("needs_refresh=", result.stdout)

    def test_status_rejects_malformed_payload_without_claiming_reachable(self):
        runner = FakeRunner(status_payload={"unexpected": ["shape"]})
        result = run_cli(["status"], runner=runner)
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertNotIn("reachable", result.stdout)
        self.assertIn("operation_failed", result.stderr)

    def test_status_never_prints_unknown_certificate_fields(self):
        certificate_payload = {
            "issuer": "CN=acme,emailAddress=admin@secret.example",
            "chain": "/etc/letsencrypt/live/subscription",
            "email": "admin@secret.example",
            "subject": "CN=sub.example.com",
            "serial": "00ff00ff00ff00ff00ff00ff00ff00ff",
            "valid": True,
            "remaining_seconds": 42,
        }
        runner = FakeRunner(certificate_payload=certificate_payload)

        result = run_cli(["status"], runner=runner)

        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("valid=true", result.stdout)
        self.assertIn("remaining_seconds=42", result.stdout)
        self.assertNotIn("secret.example", result.stdout)
        self.assertNotIn("letsencrypt", result.stdout)
        self.assertNotIn("issuer", result.stdout)
        self.assertNotIn("chain", result.stdout)
        self.assertNotIn("subject", result.stdout)
        self.assertNotIn("serial", result.stdout)

    def test_status_reports_unreachable_manager_without_leaking(self):
        runner = FakeRunner(failing_status=True, certificate_payload={"valid": True})
        result = run_cli(["status"], runner=runner)
        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertIn("manager: unreachable (error=manager_unavailable)", result.stdout)
        self.assertIn("certificate:", result.stdout)
        self.assertEqual(runner.manager_actions, ["status"])

    def test_history_prints_sanitized_release_fields_only(self):
        history_payload = {
            "user_id": "owner",
            "releases": [
                {
                    "release_id": "rel-b",
                    "variants": ["balanced", "privacy"],
                    "created_at": "2026-08-21T13:00:00Z",
                    "node_names": "secret-node",
                },
                {
                    "release_id": "rel-a",
                    "variants": ["balanced"],
                    "created_at": "2026-08-21T12:00:00Z",
                },
            ],
        }
        runner = FakeRunner(history_payload=history_payload)

        result = run_cli(["history", "owner"], runner=runner)

        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(runner.manager_arguments, [["history", "owner"]])
        self.assertIn("release=rel-b", result.stdout)
        self.assertIn("release=rel-a", result.stdout)
        self.assertIn("variants=balanced,privacy", result.stdout)
        self.assertIn("created=2026-08-21T13:00:00Z", result.stdout)
        self.assertNotIn("secret-node", result.stdout)

    def test_history_requires_exactly_one_user_id(self):
        self.assertEqual(self.run_cli(["history"]).returncode, EXIT_USAGE)
        self.assertEqual(self.run_cli(["history", "owner", "friend"]).returncode, EXIT_USAGE)

    def test_rollback_routes_ids_to_hash_checking_manager_and_reports_result(self):
        runner = FakeRunner()

        result = run_cli(["rollback", "owner", "rel-a"], runner=runner)

        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(runner.manager_arguments, [["rollback", "owner", "rel-a"]])
        self.assertEqual(runner.manager_actions, ["rollback:owner:rel-a"])
        self.assertIn("release=rel-a", result.stdout)
        self.assertIn("status=rolled-back", result.stdout)

    def test_rollback_requires_a_user_id_and_a_release_id(self):
        self.assertEqual(self.run_cli(["rollback", "owner"]).returncode, EXIT_USAGE)
        self.assertEqual(
            self.run_cli(["rollback", "owner", "rel-a", "extra"]).returncode,
            EXIT_USAGE,
        )

    def test_rotate_link_prints_tokenized_urls_exactly_once_with_warning(self):
        result = self.run_cli(["rotate-link", "owner"])

        self.assertEqual(result.returncode, EXIT_OK)
        for variant in OWNER_VARIANTS:
            url = "https://sub.example.com:8443/s/%s/url-token-value.yaml" % variant
            self.assertEqual(result.stdout.count(url), 1)
        self.assertIn("stop working", result.stdout)
        self.assertIn("only once", result.stdout)

    def test_rotate_link_never_prints_the_bare_token_field(self):
        result = self.run_cli(["rotate-link", "owner"])
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertNotIn("plain-token-value", result.stdout)
        self.assertNotIn("plain-token-value", result.stderr)

    def test_rotate_link_requires_exactly_one_user_id(self):
        self.assertEqual(self.run_cli(["rotate-link"]).returncode, EXIT_USAGE)
        self.assertEqual(self.run_cli(["rotate-link", "a", "b"]).returncode, EXIT_USAGE)

    def test_logs_forwards_bounded_limit_to_manager(self):
        runner = FakeRunner()
        result = run_cli(["logs", "--limit", "5"], runner=runner)
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(runner.manager_arguments, [["logs", "--limit", "5"]])

    def test_logs_defaults_to_fifty_entries(self):
        runner = FakeRunner()
        result = run_cli(["logs"], runner=runner)
        self.assertEqual(result.returncode, EXIT_OK)
        self.assertEqual(runner.manager_arguments, [["logs", "--limit", "50"]])

    def test_logs_rejects_out_of_bounds_limits(self):
        for raw_limit in ("0", "-1", "abc", "1001"):
            result = self.run_cli(["logs", "--limit", raw_limit])
            self.assertEqual(result.returncode, EXIT_USAGE, raw_limit)
        self.assertEqual(self.run_cli(["logs", "--limit=7", "extra"]).returncode, EXIT_USAGE)

    def test_logs_prints_only_sanitized_entry_fields(self):
        log_payload = {
            "entries": [
                {
                    "timestamp": "2026-08-21T12:00:00Z",
                    "operation": "rotate-token",
                    "user_id": "owner",
                    "release_id": None,
                    "status": "success",
                    "source_url": "https://secret.example/sub",
                }
            ]
        }
        runner = FakeRunner(log_payload=log_payload)

        result = run_cli(["logs"], runner=runner)

        self.assertEqual(result.returncode, EXIT_OK)
        self.assertIn("operation=rotate-token", result.stdout)
        self.assertIn("user_id=owner", result.stdout)
        self.assertIn("status=success", result.stdout)
        self.assertNotIn("secret.example", result.stdout)
        self.assertNotIn("source_url", result.stdout)

    def test_interrupt_exits_130_without_traceback(self):
        class InterruptingRunner(FakeRunner):
            def manager(self, arguments, stdin_text=None):
                raise KeyboardInterrupt()

        result = run_cli(["status"], runner=InterruptingRunner())

        self.assertEqual(result.returncode, EXIT_INTERRUPTED)
        self.assertIn("interrupted", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_unexpected_os_error_exits_with_redacted_failure(self):
        class BrokenRunner(FakeRunner):
            def manager(self, arguments, stdin_text=None):
                raise OSError("synthetic disk failure")

        result = run_cli(["status"], runner=BrokenRunner())

        self.assertEqual(result.returncode, EXIT_FAILURE)
        self.assertNotIn("synthetic disk failure", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("error:", result.stderr)


class CommandRunnerTests(unittest.TestCase):
    """Cover the production parse/redaction layer that FakeRunner bypasses."""

    def completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(
            args=[],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    def test_manager_returns_parsed_payload_with_fixed_compose_argv(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return self.completed(stdout='{"users": {}}\n')

        with patch("clash_sub.host_cli.subprocess.run", side_effect=fake_run):
            payload = CommandRunner().manager(["status"])

        self.assertEqual(payload, {"users": {}})
        self.assertEqual(
            captured["command"],
            ["docker", "compose", "run", "--rm", "-T", "manager", "status"],
        )
        self.assertTrue(captured["kwargs"]["capture_output"])
        self.assertFalse(captured["kwargs"]["check"])

    def test_manager_passes_stdin_text_only(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["kwargs"] = kwargs
            return self.completed(stdout='{"imported": true}\n')

        with patch("clash_sub.host_cli.subprocess.run", side_effect=fake_run):
            CommandRunner().manager(["import-airport"], stdin_text="https://airport.example/x\n")

        self.assertEqual(captured["kwargs"]["input"], "https://airport.example/x\n")
        self.assertTrue(captured["kwargs"]["text"])

    def test_parse_manager_result_rejects_malformed_json(self):
        with self.assertRaises(ManagerError) as context:
            parse_manager_result(self.completed(stdout="not json at all"))
        self.assertEqual(context.exception.code, "manager_unavailable")

    def test_parse_manager_result_rejects_non_dict_payloads(self):
        for stdout in ('["users"]\n', '"ok"\n', "null\n", "17\n"):
            with self.assertRaises(ManagerError) as context:
                parse_manager_result(self.completed(stdout=stdout))
            self.assertEqual(context.exception.code, "manager_unavailable")

    def test_parse_manager_result_raises_payload_error_despite_zero_exit(self):
        with self.assertRaises(ManagerError) as context:
            parse_manager_result(
                self.completed(stdout='{"error": {"code": "source_failed"}}\n')
            )
        self.assertEqual(context.exception.code, "source_failed")

    def test_parse_manager_result_prefers_manager_error_code_on_failure(self):
        with self.assertRaises(ManagerError) as context:
            parse_manager_result(
                self.completed(
                    returncode=1,
                    stdout='{"error": {"code": "release_missing"}}\n',
                )
            )
        self.assertEqual(context.exception.code, "release_missing")

    def test_parse_manager_result_maps_docker_failure_without_code(self):
        for stdout in ("", "connection refused by peer\n"):
            with self.assertRaises(ManagerError) as context:
                parse_manager_result(self.completed(returncode=1, stdout=stdout))
            self.assertEqual(context.exception.code, "manager_unavailable")
            self.assertNotIn("connection refused", str(context.exception))

    def test_parse_manager_result_bounds_oversized_stdout(self):
        oversized = '{"a":"' + "b" * MAX_MANAGER_OUTPUT_BYTES + '"}'
        self.assertGreater(len(oversized), MAX_MANAGER_OUTPUT_BYTES)
        with self.assertRaises(ManagerError) as context:
            parse_manager_result(self.completed(stdout=oversized))
        self.assertEqual(context.exception.code, "manager_unavailable")

    def test_validate_uses_pinned_compose_prefix_without_tty(self):
        captured = {}

        def fake_run(command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs
            return self.completed(returncode=0, stdout="cfg file test success")

        with patch("clash_sub.host_cli.subprocess.run", side_effect=fake_run):
            CommandRunner().validate(Path("/staging/op/owner/balanced.yaml"))

        self.assertEqual(
            captured["command"],
            [
                "docker",
                "compose",
                "run",
                "--rm",
                "-T",
                "validator",
                "-t",
                "-f",
                "/staging/op/owner/balanced.yaml",
            ],
        )
        self.assertTrue(captured["kwargs"]["capture_output"])

    def test_validate_failure_raises_redacted_error_without_echoing_output(self):
        completed = self.completed(
            returncode=2,
            stdout="proxies: secret-node password private-server-443",
            stderr="errno 1",
        )

        with patch("clash_sub.host_cli.subprocess.run", return_value=completed):
            with self.assertRaises(ValidatorError) as context:
                CommandRunner().validate(Path("/staging/op/owner/privacy.yaml"))

        self.assertEqual(context.exception.code, "mihomo_validation_failed")
        self.assertNotIn("secret-node", str(context.exception))
        self.assertNotIn("private-server", str(context.exception))

    def test_require_success_without_echoing_config_ignores_success_output(self):
        self.assertIsNone(
            require_success_without_echoing_config(
                self.completed(returncode=0, stdout="anything")
            )
        )
        with self.assertRaises(ValidatorError):
            require_success_without_echoing_config(self.completed(returncode=1))

    def test_missing_docker_binary_raises_redacted_manager_error(self):
        with patch(
            "clash_sub.host_cli.subprocess.run",
            side_effect=FileNotFoundError("docker"),
        ):
            with self.assertRaises(ManagerError) as context:
                CommandRunner().manager(["status"])
        self.assertEqual(context.exception.code, "manager_unavailable")

    def test_missing_docker_binary_raises_redacted_validator_error(self):
        with patch(
            "clash_sub.host_cli.subprocess.run",
            side_effect=FileNotFoundError("docker"),
        ):
            with self.assertRaises(ValidatorError) as context:
                CommandRunner().validate(Path("/staging/op/owner/balanced.yaml"))
        self.assertEqual(context.exception.code, "validator_unavailable")

    def test_certificate_status_returns_none_when_script_is_absent(self):
        with TemporaryDirectory() as directory:
            missing = Path(directory) / "check_certificate.py"
            with patch("clash_sub.host_cli.CERTIFICATE_SCRIPT_PATH", missing):
                self.assertIsNone(CommandRunner().certificate_status())

    def test_certificate_status_returns_none_for_failure_or_bad_payload(self):
        with TemporaryDirectory() as directory:
            script = Path(directory) / "check_certificate.py"
            script.write_text("# synthetic placeholder\n", encoding="utf-8")
            with patch("clash_sub.host_cli.CERTIFICATE_SCRIPT_PATH", script):
                for completed in (
                    self.completed(returncode=1, stdout='{"valid": true}\n'),
                    self.completed(returncode=0, stdout="not json\n"),
                    self.completed(returncode=0, stdout='["valid"]\n'),
                ):
                    with patch("clash_sub.host_cli.subprocess.run", return_value=completed):
                        self.assertIsNone(CommandRunner().certificate_status())
                with patch(
                    "clash_sub.host_cli.subprocess.run",
                    side_effect=OSError("spawn failed"),
                ):
                    self.assertIsNone(CommandRunner().certificate_status())

    def test_certificate_status_returns_parsed_state(self):
        with TemporaryDirectory() as directory:
            script = Path(directory) / "check_certificate.py"
            script.write_text("# synthetic placeholder\n", encoding="utf-8")
            captured = {}

            def fake_run(command, **kwargs):
                captured["command"] = command
                return self.completed(stdout='{"valid": true}\n')

            with patch("clash_sub.host_cli.CERTIFICATE_SCRIPT_PATH", script), patch(
                "clash_sub.host_cli.subprocess.run", side_effect=fake_run
            ):
                status = CommandRunner().certificate_status()

            self.assertEqual(status, {"valid": True})
            self.assertEqual(
                captured["command"],
                [sys.executable, str(script), "--status-only"],
            )


if __name__ == "__main__":
    unittest.main()
