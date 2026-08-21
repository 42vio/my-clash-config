"""Read-only REALITY dest probe.

Judges one prospective REALITY dest from a single
``openssl s_client -brief`` observation and reduces it to five
booleans, elapsed milliseconds, and a stable error code.  The script
never writes to the host and never echoes certificate material,
target bodies, or any other raw observation into its output.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Tuple


DEFAULT_TIMEOUT_SECONDS = 10.0
# openssl -brief output is a handful of summary lines; the cap exists
# so a hostile or pathological peer cannot flood the process memory.
MAX_OUTPUT_BYTES = 256 * 1024

CHECK_NAMES = (
    "reachable",
    "tls13",
    "alpn_h2",
    "x25519",
    "certificate_name",
)

_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)
_FORBIDDEN_ADDRESS_CHARS = set(';|&`$<>"\'\\()\n\r\t ')

_PROTOCOL_RE = re.compile(r"^Protocol version:\s*(\S+)", re.MULTILINE)
_ALPN_RE = re.compile(r"^ALPN protocol:\s*(\S+)", re.MULTILINE)
_TEMP_KEY_RE = re.compile(r"^Server Temp Key:\s*(.+?)\s*$", re.MULTILINE)
_PEER_CERT_RE = re.compile(r"^Peer certificate:\s*(.+?)\s*$", re.MULTILINE)
_VERIFIED_RE = re.compile(r"^Verification:\s*OK\s*$", re.MULTILINE)
_CN_RE = re.compile(r"\bCN=([^,\s]+)")
_RECOGNIZED_LINE_RE = re.compile(
    r"^(?:Protocol version|Ciphers|Peer certificate|Verification|"
    r"Server Temp Key|ALPN protocol|CONNECTION ESTABLISHED|CONNECTED|"
    r"New,|depth|write:errno|read:errno|no peer certificate|handshake|"
    r"openssl|unknown option|usage)\b",
    re.IGNORECASE | re.MULTILINE,
)


class InvalidTargetError(ValueError):
    """Raised when probe arguments are not safe argv values."""


@dataclass(frozen=True)
class TargetObservation:
    """Facts extracted from one ``s_client -brief`` run (never serialized)."""

    reachable: bool
    protocol_version: Optional[str]
    alpn: Optional[str]
    server_temp_key: Optional[str]
    peer_certificate: Optional[str]
    verification_ok: bool
    error_code: Optional[str]


@dataclass(frozen=True)
class TargetResult:
    checks: Mapping[str, bool]
    elapsed_ms: int
    error_code: Optional[str]
    address_family: Optional[str] = None

    @property
    def ok(self) -> bool:
        return all(bool(value) for value in self.checks.values())

    def to_json(self) -> dict:
        return {
            "ok": self.ok,
            "checks": dict(self.checks),
            "elapsed_ms": self.elapsed_ms,
            "error_code": self.error_code,
            "connect_address_family": self.address_family,
        }


def validate_connect_address(value: str) -> Tuple[str, str]:
    """Return (family, address) for an IPv4, IPv6, or hostname literal."""
    if not isinstance(value, str) or not value:
        raise InvalidTargetError("connect address must be a non-empty string")
    if any(character in _FORBIDDEN_ADDRESS_CHARS for character in value) or any(
        character.isspace() for character in value
    ):
        raise InvalidTargetError("connect address contains forbidden characters")
    if len(value) > 253:
        raise InvalidTargetError("connect address is too long")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        if not _HOSTNAME_RE.fullmatch(value):
            raise InvalidTargetError(
                "connect address must be an IPv4, IPv6, or hostname literal"
            )
        return "hostname", value
    return ("ipv4" if address.version == 4 else "ipv6"), value


def validate_port(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidTargetError("port must be an integer")
    if value < 1 or value > 65535:
        raise InvalidTargetError("port must be between 1 and 65535")
    return value


def validate_server_name(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise InvalidTargetError("server name must be a non-empty string")
    if any(character in _FORBIDDEN_ADDRESS_CHARS for character in value) or any(
        character.isspace() for character in value
    ):
        raise InvalidTargetError("server name contains forbidden characters")
    if not _HOSTNAME_RE.fullmatch(value):
        raise InvalidTargetError("server name must be a hostname")
    return value


def build_command(connect_address: str, port: int, server_name: str) -> list:
    family, address = validate_connect_address(connect_address)
    validate_port(port)
    validate_server_name(server_name)
    if family == "ipv6":
        endpoint = "[%s]:%d" % (address, port)
    else:
        endpoint = "%s:%d" % (address, port)
    return [
        "openssl",
        "s_client",
        "-connect",
        endpoint,
        "-servername",
        server_name,
        "-tls1_3",
        "-alpn",
        "h2",
        "-groups",
        "X25519",
        "-brief",
    ]


def parse_s_client_output(text: str) -> TargetObservation:
    """Extract the -brief facts; never raise on malformed output."""
    reachable = "CONNECTION ESTABLISHED" in text
    return TargetObservation(
        reachable=reachable,
        protocol_version=_capture(_PROTOCOL_RE, text),
        alpn=_capture(_ALPN_RE, text),
        server_temp_key=_capture(_TEMP_KEY_RE, text),
        peer_certificate=_capture(_PEER_CERT_RE, text),
        verification_ok=bool(_VERIFIED_RE.search(text)),
        error_code=None if reachable else _infer_error_code(text),
    )


def evaluate_target(
    observation: TargetObservation, expected_server_name: str
) -> TargetResult:
    validate_server_name(expected_server_name)
    checks = {
        "reachable": bool(observation.reachable),
        "tls13": observation.protocol_version == "TLSv1.3",
        "alpn_h2": observation.alpn == "h2",
        "x25519": bool(observation.server_temp_key)
        and "X25519" in observation.server_temp_key,
        "certificate_name": _certificate_name_matches(
            observation, expected_server_name
        ),
    }
    return TargetResult(checks=checks, elapsed_ms=0, error_code=observation.error_code)


def probe_target(
    connect_address: str,
    port: int,
    server_name: str,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    executor: Optional[Callable[[list, float], Tuple[int, str]]] = None,
) -> TargetResult:
    """Run one read-only probe and reduce it to a safe report."""
    family, _ = validate_connect_address(connect_address)
    validate_port(port)
    validate_server_name(server_name)
    argv = build_command(connect_address, port, server_name)
    started = time.monotonic()
    run = executor if executor is not None else _run_openssl
    try:
        returncode, output = run(argv, timeout)
    except subprocess.TimeoutExpired:
        elapsed = int((time.monotonic() - started) * 1000)
        return TargetResult(
            checks={name: False for name in CHECK_NAMES},
            elapsed_ms=elapsed,
            error_code="timeout",
            address_family=family,
        )
    elapsed = int((time.monotonic() - started) * 1000)
    parsed = evaluate_target(
        parse_s_client_output(output), expected_server_name=server_name
    )
    return TargetResult(
        checks=parsed.checks,
        elapsed_ms=elapsed,
        error_code=parsed.error_code,
        address_family=family,
    )


def _run_openssl(argv: list, timeout: float) -> Tuple[int, str]:
    """Execute openssl without a shell, DEVNULL stdin, capped output."""
    with tempfile.TemporaryFile(mode="w+b") as out, tempfile.TemporaryFile(
        mode="w+b"
    ) as err:
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=timeout,
                check=False,
            )
            returncode = completed.returncode
        except FileNotFoundError:
            return 127, ""
        out.seek(0)
        err.seek(0)
        combined = (
            out.read(MAX_OUTPUT_BYTES)
            + b"\n"
            + err.read(MAX_OUTPUT_BYTES)
        )
    return returncode, combined.decode("utf-8", errors="replace")


def _capture(pattern, text):
    match = pattern.search(text)
    return match.group(1) if match else None


def _infer_error_code(text: str) -> Optional[str]:
    lowered = text.lower()
    if "connection refused" in lowered:
        return "connection_refused"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if not text.strip():
        # An empty capture is indistinguishable from a timeout here;
        # probe_target reports the authoritative "timeout" when the
        # subprocess itself timed out.
        return "timeout"
    if (
        "connection reset" in lowered
        or "no route to host" in lowered
        or "unreachable" in lowered
        or "handshake failure" in lowered
    ):
        return "connection_failed"
    if _RECOGNIZED_LINE_RE.search(text):
        return "connection_failed"
    return "malformed_output"


def _certificate_name_matches(
    observation: TargetObservation, expected_server_name: str
) -> bool:
    if observation.verification_ok:
        return True
    if not observation.peer_certificate:
        return False
    match = _CN_RE.search(observation.peer_certificate)
    if not match:
        return False
    common_name = match.group(1).rstrip(".")
    expected = expected_server_name.rstrip(".")
    return common_name == expected or common_name.endswith("." + expected)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only REALITY dest probe (openssl s_client -brief)."
    )
    parser.add_argument("--connect-address", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--server-name", required=True)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def _print_human(result: TargetResult) -> None:
    labels = (
        ("reachable", "reachable"),
        ("tls13", "tls1.3"),
        ("alpn_h2", "alpn h2"),
        ("x25519", "x25519"),
        ("certificate_name", "certificate name"),
    )
    print("REALITY target check")
    for key, label in labels:
        print("  %-18s %s" % (label + ":", "yes" if result.checks.get(key) else "no"))
    print("  %-18s %d ms" % ("elapsed:", result.elapsed_ms))
    print("  %-18s %s" % ("error:", result.error_code or "none"))
    print("  %-18s %s" % ("result:", "OK" if result.ok else "REJECTED"))


def main(argv=None) -> int:
    args = _parse_args(argv)
    try:
        result = probe_target(
            args.connect_address, args.port, args.server_name, timeout=args.timeout
        )
    except InvalidTargetError:
        # Never echo the rejected value back; report the field only.
        if args.json:
            print(
                json.dumps(
                    {"ok": False, "error_code": "invalid_argument"}, sort_keys=True
                )
            )
        else:
            print("argument rejected: connect address, port, or server name is invalid")
        return 2
    if args.json:
        print(json.dumps(result.to_json(), sort_keys=True))
    else:
        _print_human(result)
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
