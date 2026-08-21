"""Certificate health checker and alerter.

Inspects the configured fullchain file through ``openssl x509``
without a shell, persists a sanitized state document atomically with
mode 0600, runs the configured alert argv directly when the
certificate is unreadable, invalid, expiring inside the configured
threshold, or its renewal is marked failed, and suppresses duplicate
identical alerts for twelve hours.

``renewal_ok`` is marked explicitly by the clash-sub-cert-renew
systemd units: ``ExecStartPost`` marks ``ok`` after a successful
``certbot renew`` and ``OnFailure`` marks ``failed``.  A missing mark
is treated as not failed so a fresh install does not alert before the
first timer run.

Output contract: ``--status-only`` prints exactly the seven sanitized
keys consumed by the host CLI (valid, renewal_ok, remaining_seconds,
checked_at, last_success_at, last_alert_at, error_code).  The script
never emits SANs, authorities, emails, filesystem paths, or
alert-command arguments.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    import yaml

    from clash_sub.settings import SettingsError, _parse_service_settings
except ImportError:  # pragma: no cover - in-tree runs always have clash_sub
    yaml = None
    _parse_service_settings = None
    SettingsError = ValueError


COMMAND_TIMEOUT_SECONDS = 15.0
MAX_OUTPUT_BYTES = 256 * 1024
ALERT_DEDUP_SECONDS = 12 * 3600
STATE_FILE_MODE = 0o600
DEFAULT_THRESHOLD_SECONDS = 14 * 24 * 3600
DEFAULT_CONFIG_PATH = ROOT / "private" / "config" / "service.yaml"

STATUS_KEYS = (
    "valid",
    "renewal_ok",
    "remaining_seconds",
    "checked_at",
    "last_success_at",
    "last_alert_at",
    "error_code",
)

_NOT_AFTER_RE = re.compile(r"^notAfter=(.+)$", re.MULTILINE)
_ERROR_CODE_RE = re.compile(r"^[a-z0-9_]{1,64}$")
_NOT_AFTER_FORMATS = (
    "%a %b %d %H:%M:%S %Y %Z",
    "%b %d %H:%M:%S %Y %Z",
    "%a %b %d %H:%M:%S %Y",
)


@dataclass(frozen=True)
class CertReport:
    """Sanitized inspection result; never carries subject material."""

    valid: bool
    remaining_seconds: int
    error_code: str

    def to_json(self) -> dict:
        return {
            "valid": self.valid,
            "remaining_seconds": self.remaining_seconds,
            "error_code": self.error_code,
        }


@dataclass(frozen=True)
class CertificateStatus:
    valid: bool
    renewal_ok: bool
    remaining_seconds: int
    alerted: bool
    error_code: str


class SubprocessRunner:
    """Production runner: argv lists only, never a shell."""

    def __init__(self):
        self.commands = []
        self.shell_used = False

    def run(self, argv, timeout: float = COMMAND_TIMEOUT_SECONDS):
        self.commands.append(list(argv))
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError:
            return SimpleNamespace(returncode=127, stdout="", stderr="")
        except subprocess.TimeoutExpired:
            return SimpleNamespace(returncode=124, stdout="", stderr="")
        return SimpleNamespace(
            returncode=completed.returncode,
            stdout=(completed.stdout or "")[:MAX_OUTPUT_BYTES],
            stderr=(completed.stderr or "")[:MAX_OUTPUT_BYTES],
        )


def inspect_certificate(path, runner=None, now=None) -> CertReport:
    """Reduce one openssl inspection to a sanitized report."""
    current_runner = runner if runner is not None else SubprocessRunner()
    checked_at = now if now is not None else datetime.now(timezone.utc)

    enddate = current_runner.run(
        ["openssl", "x509", "-in", str(path), "-noout", "-enddate"]
    )
    if enddate.returncode != 0:
        return CertReport(
            valid=False,
            remaining_seconds=0,
            error_code=_classify_openssl_failure(enddate.stderr),
        )
    not_after = _parse_not_after(enddate.stdout)
    if not_after is None:
        return CertReport(
            valid=False, remaining_seconds=0, error_code="certificate_invalid"
        )
    checkend = current_runner.run(
        ["openssl", "x509", "-in", str(path), "-noout", "-checkend", "0"]
    )
    remaining = int((not_after - checked_at).total_seconds())
    valid = checkend.returncode == 0 and remaining > 0
    return CertReport(
        valid=valid,
        remaining_seconds=remaining,
        error_code="" if valid else "certificate_expired",
    )


def check_and_alert(
    cert_path,
    alert_argv: Sequence[str],
    runner=None,
    now=None,
    threshold_seconds: int = DEFAULT_THRESHOLD_SECONDS,
    state_path=None,
    state=None,
) -> CertificateStatus:
    """Inspect, update state, and alert once per reason per 12 hours."""
    current_runner = runner if runner is not None else SubprocessRunner()
    checked_at = now if now is not None else datetime.now(timezone.utc)
    alert_tuple = tuple(alert_argv or ())

    report = inspect_certificate(cert_path, runner=current_runner, now=checked_at)
    document = dict(state) if state is not None else load_state(state_path)
    document.setdefault("renewal_ok", True)

    document["checked_at"] = _iso(checked_at)
    document["valid"] = report.valid
    document["remaining_seconds"] = report.remaining_seconds
    document["last_error_code"] = report.error_code
    if report.valid and not report.error_code:
        document["last_success_at"] = _iso(checked_at)

    reason = _alert_reason(report, document, threshold_seconds)
    alerted = False
    if reason and alert_tuple and not _duplicate_alert(document, checked_at, reason):
        current_runner.run(list(alert_tuple))
        document["last_alert_at"] = _iso(checked_at)
        document["last_alert_fingerprint"] = reason
        alerted = True

    if state_path is not None:
        write_state(Path(state_path), document)

    return CertificateStatus(
        valid=report.valid,
        renewal_ok=bool(document.get("renewal_ok", True)),
        remaining_seconds=report.remaining_seconds,
        alerted=alerted,
        error_code=report.error_code,
    )


def mark_renewal(ok: bool, state_path, now=None) -> None:
    """Record the systemd renew unit's outcome; nothing else."""
    checked_at = now if now is not None else datetime.now(timezone.utc)
    document = load_state(state_path)
    document["renewal_ok"] = bool(ok)
    if ok:
        document["last_renewal_success_at"] = _iso(checked_at)
    write_state(Path(state_path), document)


def load_state(path) -> dict:
    """Read the sanitized state; missing or corrupt files start fresh."""
    if path is None:
        return {}
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        document = json.loads(raw)
    except ValueError:
        return {}
    return document if isinstance(document, dict) else {}


def write_state(path, document: dict) -> None:
    """Atomic sibling-temp replace with mode 0600."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(document, sort_keys=True)
    temp_fd, temp_name = tempfile.mkstemp(
        prefix="%s." % target.name, suffix=".tmp", dir=str(target.parent), text=True
    )
    try:
        os.fchmod(temp_fd, STATE_FILE_MODE)
        with os.fdopen(temp_fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp_name, target)
    except Exception:
        try:
            os.close(temp_fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def status_document(state: dict) -> dict:
    """Project state onto exactly the seven sanitized status keys."""
    document = {}
    document["valid"] = bool(state.get("valid", False))
    document["renewal_ok"] = bool(state.get("renewal_ok", False))
    remaining = state.get("remaining_seconds", 0)
    document["remaining_seconds"] = (
        remaining
        if isinstance(remaining, int) and not isinstance(remaining, bool)
        else 0
    )
    for key in ("checked_at", "last_success_at", "last_alert_at"):
        value = state.get(key, "")
        document[key] = value if isinstance(value, str) else ""
    error_code = state.get("last_error_code", "")
    document["error_code"] = (
        error_code
        if isinstance(error_code, str) and _ERROR_CODE_RE.fullmatch(error_code)
        else ""
    )
    return document


def _alert_reason(report: CertReport, state: dict, threshold_seconds: int) -> str:
    if report.error_code:
        return report.error_code
    if report.remaining_seconds <= threshold_seconds:
        return "certificate_expiring"
    if not state.get("renewal_ok", True):
        return "renewal_failed"
    return ""


def _duplicate_alert(state: dict, now: datetime, reason: str) -> bool:
    last_text = state.get("last_alert_at")
    fingerprint = state.get("last_alert_fingerprint")
    if not isinstance(last_text, str) or fingerprint != reason:
        return False
    last = _parse_iso(last_text)
    if last is None:
        return False
    return (now - last).total_seconds() < ALERT_DEDUP_SECONDS


def _classify_openssl_failure(stderr: str) -> str:
    lowered = (stderr or "").lower()
    if "no such file" in lowered or "permission denied" in lowered:
        return "certificate_unreadable"
    return "certificate_invalid"


def _parse_not_after(text: str):
    match = _NOT_AFTER_RE.search(text or "")
    if match is None:
        return None
    raw = match.group(1).strip()
    for candidate in _NOT_AFTER_FORMATS:
        try:
            parsed = datetime.strptime(raw, candidate)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(text: str):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _load_service_settings(path: Path):
    if _parse_service_settings is None:
        raise RuntimeError("settings_unavailable")
    try:
        document = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        raise RuntimeError("settings_unreadable")
    if not isinstance(document, dict):
        raise RuntimeError("settings_unreadable")
    try:
        return _parse_service_settings(document)
    except SettingsError:
        raise RuntimeError("settings_invalid")


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Certificate health check with sanitized state and alerting."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--mark-renewal", choices=("ok", "failed"))
    return parser.parse_args(argv)


def _state_path_for(args, service) -> Path:
    if args.state is not None:
        return Path(args.state)
    if service is not None:
        return Path(service.private_root) / "state" / "certificate.json"
    return DEFAULT_CONFIG_PATH.parent / "state" / "certificate.json"


def main(argv=None, runner=None) -> int:
    args = _parse_args(argv)
    service = None
    try:
        if args.config.is_file():
            service = _load_service_settings(args.config)
    except RuntimeError:
        service = None

    state_path = _state_path_for(args, service)

    if args.status_only:
        state = load_state(state_path) if (service is not None or args.state is not None) else {}
        if not state:
            state = {"last_error_code": "state_missing"}
        print(json.dumps(status_document(state), sort_keys=True))
        return 0

    if args.mark_renewal is not None:
        mark_renewal(args.mark_renewal == "ok", state_path)
        return 0

    if service is None:
        print("certificate check: error=settings_unavailable", file=sys.stderr)
        return 2

    status = check_and_alert(
        service.certificate.fullchain_path,
        service.certificate.alert_command,
        runner=runner,
        threshold_seconds=service.certificate.alert_before_seconds,
        state_path=state_path,
    )
    return 0 if status.valid and not status.error_code else 1


if __name__ == "__main__":
    sys.exit(main())
