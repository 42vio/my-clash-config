"""User-facing ``clash-sub`` host command.

The command orchestrates the machine-facing manager JSON API, the pinned
Mihomo validator container, and the certificate status helper.  It only
prints sanitized fields: never source URLs, tokens, hashes, panel paths,
node names, or credentials.  The single deliberate exception is
``rotate-link``, whose purpose is to show each new tokenized URL once.
"""

import getpass
import json
import re
import secrets
import subprocess
import sys
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import List, Mapping, Optional, Sequence, TextIO


EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2

MANAGER_SERVICE = "manager"
VALIDATOR_SERVICE = "validator"
CERTIFICATE_SCRIPT_PATH = Path("scripts") / "check_certificate.py"

AIRPORT_PROMPT = "Temporary airport subscription URL: "
DEFAULT_LOG_LIMIT = 50
MAX_LOG_LIMIT = 1000
MAX_MANAGER_OUTPUT_BYTES = 1024 * 1024
MAX_CERTIFICATE_VALUE_CHARS = 64
OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SENSITIVE_CERTIFICATE_KEY_RE = re.compile(
    r"path|email|command|argv|san|domain|host|url|token|secret|key",
    re.IGNORECASE,
)

HELP_TEXT = """clash-sub: manage the private Clash subscription service

usage: clash-sub <command> [arguments]

commands:
  status
      Show service reachability, per-user release state, traffic, and certificate health.
  refresh [user-id]
      Rebuild, validate, and publish configurations for one user or every user.
  airport
      Import a temporary airport subscription over a hidden prompt, then refresh the owner.
  history <user-id>
      List published releases for one user.
  rollback <user-id> <release-id>
      Repoint a user at a previously published release.
  rotate-link <user-id>
      Rotate a user's subscription token and print the new links once.
  logs [--limit N]
      Show the most recent redacted operation log entries (default 50, at most 1000).
  help
      Show this help.

refresh builds one operation-scoped candidate per user, validates every
variant with the pinned Mihomo validator, and publishes only after every
variant passes.  A failed user never blocks the remaining users.
"""


class UsageError(ValueError):
    """Raised when the user supplies invalid command-line arguments."""


class ManagerError(RuntimeError):
    """Raised when a manager command fails; carries a redacted code only."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ValidatorError(RuntimeError):
    """Raised when the pinned Mihomo validator rejects a candidate variant."""

    def __init__(self, code: str = "mihomo_validation_failed", variant: Optional[str] = None):
        super().__init__(code)
        self.code = code
        self.variant = variant


def parse_manager_result(completed) -> Mapping[str, object]:
    """Turn a completed manager process into a payload or a redacted error."""
    stdout_text = completed.stdout or ""
    try:
        payload = json.loads(stdout_text[:MAX_MANAGER_OUTPUT_BYTES])
    except ValueError:
        raise ManagerError("manager_unavailable")
    if not isinstance(payload, dict):
        raise ManagerError("manager_unavailable")
    error = payload.get("error")
    if completed.returncode != 0:
        if isinstance(error, dict) and isinstance(error.get("code"), str) and error["code"]:
            raise ManagerError(error["code"])
        raise ManagerError("manager_unavailable")
    if error is not None:
        code = error.get("code") if isinstance(error, dict) else None
        raise ManagerError(code if isinstance(code, str) and code else "operation_failed")
    return payload


def require_success_without_echoing_config(completed) -> None:
    """Reduce a validator run to pass/fail without echoing its output."""
    if completed.returncode != 0:
        raise ValidatorError("mihomo_validation_failed")


class CommandRunner:
    """Production runner: fixed compose prefixes, no shell, no echo of configs."""

    def manager(
        self,
        arguments: Sequence[str],
        stdin_text: Optional[str] = None,
    ) -> Mapping[str, object]:
        completed = subprocess.run(
            ["docker", "compose", "run", "--rm", "-T", MANAGER_SERVICE, *arguments],
            input=stdin_text,
            text=True,
            capture_output=True,
            check=False,
        )
        return parse_manager_result(completed)

    def validate(self, candidate_path: Path) -> None:
        completed = subprocess.run(
            [
                "docker",
                "compose",
                "run",
                "--rm",
                VALIDATOR_SERVICE,
                "-t",
                "-f",
                str(candidate_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        require_success_without_echoing_config(completed)

    def certificate_status(self) -> Optional[Mapping[str, object]]:
        """Read the sanitized certificate state defensively.

        ``scripts/check_certificate.py`` may not exist yet or may fail;
        status must degrade gracefully instead of failing the command.
        """
        script_path = Path(__file__).resolve().parents[1] / CERTIFICATE_SCRIPT_PATH
        if not script_path.is_file():
            return None
        try:
            completed = subprocess.run(
                [sys.executable, str(script_path), "--status-only"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None
        try:
            payload = json.loads(completed.stdout)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None


def main(
    argv: Optional[Sequence[str]] = None,
    runner: Optional[CommandRunner] = None,
    stdout: Optional[TextIO] = None,
    stderr: Optional[TextIO] = None,
    prompt=None,
    operation_id_factory=None,
) -> int:
    """Run the user-facing command and return its integer exit code."""
    arguments = list(sys.argv[1:] if argv is None else argv)
    current_runner = runner if runner is not None else CommandRunner()
    out_stream = sys.stdout if stdout is None else stdout
    err_stream = sys.stderr if stderr is None else stderr
    prompt_function = getpass.getpass if prompt is None else prompt
    make_operation_id = operation_id_factory or _default_operation_id

    if not arguments or arguments == ["help"]:
        out_stream.write(HELP_TEXT)
        return EXIT_OK

    command, rest = arguments[0], arguments[1:]
    try:
        if command == "status":
            return _command_status(rest, current_runner, out_stream)
        if command == "refresh":
            return _command_refresh(rest, current_runner, out_stream, err_stream, make_operation_id)
        if command == "airport":
            return _command_airport(
                rest, current_runner, out_stream, err_stream, prompt_function, make_operation_id
            )
        if command == "history":
            return _command_history(rest, current_runner, out_stream)
        if command == "rollback":
            return _command_rollback(rest, current_runner, out_stream)
        if command == "rotate-link":
            return _command_rotate_link(rest, current_runner, out_stream)
        if command == "logs":
            return _command_logs(rest, current_runner, out_stream)
    except UsageError as exc:
        err_stream.write("clash-sub: %s\n" % (exc,))
        return EXIT_USAGE
    except ManagerError as exc:
        err_stream.write("clash-sub: error: %s\n" % exc.code)
        return EXIT_FAILURE
    except ValidatorError as exc:
        err_stream.write("clash-sub: error: %s\n" % exc.code)
        return EXIT_FAILURE

    err_stream.write("clash-sub: unknown command: %s\n" % _safe_command_name(command))
    err_stream.write("clash-sub: run 'clash-sub help' to list commands\n")
    return EXIT_USAGE


def run_cli(
    arguments: Sequence[str],
    runner: Optional[CommandRunner] = None,
    prompt=None,
    operation_id_factory=None,
) -> SimpleNamespace:
    """Test-friendly wrapper around :func:`main` with captured streams."""
    stdout = StringIO()
    stderr = StringIO()
    returncode = main(
        list(arguments),
        runner=runner,
        stdout=stdout,
        stderr=stderr,
        prompt=prompt,
        operation_id_factory=operation_id_factory,
    )
    return SimpleNamespace(returncode=returncode, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def _command_refresh(rest, runner, out_stream, err_stream, make_operation_id) -> int:
    if len(rest) > 1:
        raise UsageError("refresh takes at most one user id")
    if rest:
        user_ids = [rest[0]]
    else:
        user_ids = [
            user_id
            for user_id, role in _listed_users(runner)
        ]
    return _refresh_users(user_ids, runner, out_stream, err_stream, make_operation_id)


def _command_airport(rest, runner, out_stream, err_stream, prompt_function, make_operation_id) -> int:
    if rest:
        raise UsageError("airport takes no arguments")
    try:
        airport_url = prompt_function(AIRPORT_PROMPT)
    except (EOFError, KeyboardInterrupt):
        err_stream.write("clash-sub: airport import cancelled\n")
        return EXIT_FAILURE
    runner.manager(["import-airport"], stdin_text=airport_url + "\n")
    del airport_url
    owner_ids = [user_id for user_id, role in _listed_users(runner) if role == "owner"]
    if not owner_ids:
        err_stream.write("clash-sub: error: operation_failed\n")
        return EXIT_FAILURE
    return _refresh_users(owner_ids, runner, out_stream, err_stream, make_operation_id)


def _command_status(rest, runner, out_stream) -> int:
    if rest:
        raise UsageError("status takes no arguments")
    out_stream.write("services:\n")
    exit_code = EXIT_OK
    try:
        payload = runner.manager(["status"])
    except ManagerError as exc:
        out_stream.write("  manager: unreachable (error=%s)\n" % exc.code)
        exit_code = EXIT_FAILURE
        payload = {}
    else:
        out_stream.write("  manager: reachable\n")
    _write_certificate_state(runner, out_stream)
    users = payload.get("users")
    out_stream.write("users:\n")
    if not isinstance(users, dict):
        if exit_code == EXIT_OK:
            raise ManagerError("operation_failed")
        users = {}
    for user_id in sorted(users):
        info = users[user_id]
        if not isinstance(info, dict):
            continue
        _write_user_status(out_stream, user_id, info)
    return exit_code


def _command_history(rest, runner, out_stream) -> int:
    if len(rest) != 1:
        raise UsageError("history requires exactly one user id")
    payload = runner.manager(["history", rest[0]])
    out_stream.write("history %s:\n" % rest[0])
    releases = payload.get("releases")
    if isinstance(releases, list):
        for entry in releases:
            if not isinstance(entry, dict):
                continue
            out_stream.write(
                "  release=%s created=%s variants=%s\n"
                % (
                    _text(entry.get("release_id")) or "unknown",
                    _text(entry.get("created_at")) or "unknown",
                    ",".join(_string_sequence(entry.get("variants"))) or "none",
                )
            )
    return EXIT_OK


def _command_rollback(rest, runner, out_stream) -> int:
    if len(rest) != 2:
        raise UsageError("rollback requires a user id and a release id")
    user_id, release_id = rest
    payload = runner.manager(["rollback", user_id, release_id])
    out_stream.write(
        "rollback %s: release=%s variants=%s status=rolled-back\n"
        % (
            _text(payload.get("user_id")) or user_id,
            _text(payload.get("release_id")) or release_id,
            ",".join(_string_sequence(payload.get("variants"))) or "none",
        )
    )
    return EXIT_OK


def _command_rotate_link(rest, runner, out_stream) -> int:
    if len(rest) != 1:
        raise UsageError("rotate-link requires exactly one user id")
    payload = runner.manager(["rotate-token", rest[0]])
    urls = payload.get("urls")
    out_stream.write("rotate-link %s: new subscription links:\n" % rest[0])
    if isinstance(urls, dict):
        for variant in sorted(urls):
            url = urls[variant]
            if isinstance(url, str) and url:
                out_stream.write("  %s: %s\n" % (variant, url))
    out_stream.write(
        "warning: previous links stop working after the publisher reloads; "
        "these links are shown only once\n"
    )
    return EXIT_OK


def _command_logs(rest, runner, out_stream) -> int:
    limit = DEFAULT_LOG_LIMIT
    index = 0
    while index < len(rest):
        argument = rest[index]
        if argument == "--limit":
            index += 1
            if index >= len(rest):
                raise UsageError("logs --limit requires a value")
            raw_limit = rest[index]
        elif argument.startswith("--limit="):
            raw_limit = argument.split("=", 1)[1]
        else:
            raise UsageError("logs takes no positional arguments")
        try:
            limit = int(raw_limit)
        except ValueError:
            raise UsageError("logs --limit must be an integer")
        if not 1 <= limit <= MAX_LOG_LIMIT:
            raise UsageError("logs --limit must be between 1 and %d" % MAX_LOG_LIMIT)
        index += 1
    payload = runner.manager(["logs", "--limit", str(limit)])
    out_stream.write("logs:\n")
    entries = payload.get("entries")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            out_stream.write("  %s\n" % " ".join(_log_fields(entry)))
    return EXIT_OK


def _refresh_users(user_ids, runner, out_stream, err_stream, make_operation_id) -> int:
    """Apply the full refresh lifecycle per user, isolating failures."""
    failures = 0
    for user_id in sorted(user_ids):
        try:
            _refresh_one_user(user_id, runner, out_stream, make_operation_id)
        except ValidatorError as exc:
            failures += 1
            if exc.variant:
                err_stream.write(
                    "refresh %s: error=%s variant=%s\n" % (user_id, exc.code, exc.variant)
                )
            else:
                err_stream.write("refresh %s: error=%s\n" % (user_id, exc.code))
        except ManagerError as exc:
            failures += 1
            err_stream.write("refresh %s: error=%s\n" % (user_id, exc.code))
    return EXIT_FAILURE if failures else EXIT_OK


def _refresh_one_user(user_id, runner, out_stream, make_operation_id) -> None:
    """Build one candidate, validate every variant, publish, and report."""
    operation_id = make_operation_id()
    build_payload = runner.manager(
        ["build", "--operation-id", operation_id, "--user", user_id]
    )
    candidate_path = build_payload.get("candidate_path")
    variants = _string_sequence(build_payload.get("variants"))
    if not isinstance(candidate_path, str) or not candidate_path or not variants:
        raise ManagerError("operation_failed")
    for variant in variants:
        try:
            runner.validate(Path(candidate_path) / ("%s.yaml" % variant))
        except ValidatorError as exc:
            raise ValidatorError(exc.code, variant) from exc
    publish_payload = runner.manager(
        ["publish", "--operation-id", operation_id, "--user", user_id]
    )
    release_id = _text(publish_payload.get("release_id"))
    out_stream.write(
        "refresh %s: release=%s variants=%s status=published\n"
        % (user_id, release_id or operation_id, ",".join(variants))
    )


def _listed_users(runner) -> List[Sequence[str]]:
    payload = runner.manager(["list-users"])
    users = payload.get("users")
    if not isinstance(users, list):
        raise ManagerError("operation_failed")
    listed = []
    for user in users:
        if not isinstance(user, dict):
            continue
        user_id = user.get("user_id")
        role = user.get("role")
        if isinstance(user_id, str) and user_id and isinstance(role, str):
            listed.append((user_id, role))
    return sorted(listed)


def _write_user_status(out_stream, user_id, info) -> None:
    parts = [
        "release=%s" % (_text(info.get("release_id")) or "none"),
        "variants=%s" % (",".join(_string_sequence(info.get("variants"))) or "none"),
        "generated=%s" % (_text(info.get("created_at")) or "unknown"),
        "needs_refresh=%s" % ("yes" if info.get("needs_refresh") else "no"),
        "traffic=%s" % _format_traffic(info.get("traffic")),
    ]
    error_code = info.get("error_code")
    if isinstance(error_code, str) and error_code:
        parts.append("error=%s" % error_code)
    out_stream.write("  %s: %s\n" % (user_id, " ".join(parts)))


def _write_certificate_state(runner, out_stream) -> None:
    try:
        status = runner.certificate_status()
    except Exception:
        status = None
    if not isinstance(status, dict):
        out_stream.write("  certificate: state unavailable\n")
        return
    fields = _certificate_fields(status)
    out_stream.write("  certificate: %s\n" % (" ".join(fields) or "state unavailable"))


def _certificate_fields(status) -> List[str]:
    fields = []
    for key in sorted(status):
        if SENSITIVE_CERTIFICATE_KEY_RE.search(key):
            continue
        value = status[key]
        if isinstance(value, bool):
            fields.append("%s=%s" % (key, "true" if value else "false"))
        elif isinstance(value, int):
            fields.append("%s=%d" % (key, value))
        elif isinstance(value, str):
            fields.append("%s=%s" % (key, value.strip()[:MAX_CERTIFICATE_VALUE_CHARS] or "-"))
    return fields


def _format_traffic(traffic) -> str:
    if not isinstance(traffic, dict):
        return "unavailable"
    parts = []
    for key in ("upload", "download", "total", "remaining", "expire"):
        value = traffic.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            parts.append("%s=%d" % (key, value))
    return " ".join(parts) if parts else "unavailable"


def _log_fields(entry) -> List[str]:
    fields = []
    for key in ("timestamp", "operation", "user_id", "release_id", "status"):
        fields.append("%s=%s" % (key, _text(entry.get(key)) or "-"))
    error_code = entry.get("error_code")
    if isinstance(error_code, str) and error_code:
        fields.append("error=%s" % error_code)
    return fields


def _default_operation_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return "op-%s-%s" % (stamp, secrets.token_hex(6))


def _safe_command_name(value) -> str:
    return re.sub(r"\s+", " ", value)[:64]


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _string_sequence(value) -> List[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


if __name__ == "__main__":
    raise SystemExit(main())
