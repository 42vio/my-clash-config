"""Read-only REALITY dest probe.

Judges one prospective REALITY dest from a single full-mode
``openssl s_client`` observation and reduces it to five booleans,
elapsed milliseconds, and a stable error code.  ``-brief`` is
deliberately NOT used: quiet mode routes the ALPN summary through a
discarded BIO, so the ALPN line would never appear.  Hostname
checking is delegated to openssl itself via ``-verify_hostname``.

The script never writes to the host and never echoes certificate
material, target bodies, or any other raw observation into its
output.  The parser accepts both the OpenSSL 3.0 ``Server Temp
Key:`` label and the OpenSSL >= 3.5 ``Peer Temp Key:`` rename, plus
``-brief``-style text piped in manually.
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
# The combined stdout+stderr capture is capped at this many bytes so a
# hostile or pathological peer cannot flood the process memory.
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

# -brief-style labels (kept for manually piped text).
_PROTOCOL_VERSION_RE = re.compile(r"^Protocol version:\s*(\S+)", re.MULTILINE)
_PEER_CERT_RE = re.compile(r"^Peer certificate:\s*(.+?)\s*$", re.MULTILINE)
_VERIFIED_RE = re.compile(r"^Verification:\s*OK\s*$", re.MULTILINE)
# Full-mode labels.
_CONNECTED_RE = re.compile(r"^CONNECTED\(", re.MULTILINE)
_PROTOCOL_RE = re.compile(r"^Protocol:\s*(TLSv\S+)", re.MULTILINE)
_NEW_SESSION_RE = re.compile(r"^New,\s*([A-Za-z0-9().]+)", re.MULTILINE)
_SESSION_PROTOCOL_RE = re.compile(r"^\s+Protocol\s*:\s*(TLSv[\w.]+)", re.MULTILINE)
_ALPN_RE = re.compile(r"^ALPN protocol:\s*(\S+)", re.MULTILINE)
# OpenSSL >= 3.5 renamed "Server Temp Key" to "Peer Temp Key".
_TEMP_KEY_RE = re.compile(r"^(?:Server|Peer) Temp Key:\s*(.+?)\s*$", re.MULTILINE)
_VERIFY_RETURN_RE = re.compile(r"^\s*Verify return code:\s*(\d+)", re.MULTILINE)
_VERIFIED_PEERNAME_RE = re.compile(r"^Verified peername:\s*(\S+)", re.MULTILINE)
_NO_PEER_CERT_RE = re.compile(r"no peer certificate available", re.IGNORECASE)
_SUBJECT_RE = re.compile(r"^subject=(.*)$", re.MULTILINE)
_CN_RE = re.compile(r"CN\s*=\s*([^,\s/]+)")
_SAN_DNS_RE = re.compile(r"DNS:([^,\s]+)")
_RECOGNIZED_LINE_RE = re.compile(
    r"^(?:Protocol version|Protocol:|Ciphersuite|Peer certificate|Verification|"
    r"Verify return code|Verified peername|Server Temp Key|Peer Temp Key|"
    r"ALPN protocol|No ALPN negotiated|CONNECTION ESTABLISHED|CONNECTED|New,|"
    r"depth|write:errno|read:errno|no peer certificate|handshake|openssl|"
    r"unknown option|usage|subject|issuer|Certificate chain|Server certificate|"
    r"SSL handshake|SSL-Session|Secure Renegotiation|Compression|Expansion|"
    r"Server public key|No client certificate|Early data|DONE)",
    re.IGNORECASE | re.MULTILINE,
)


class InvalidTargetError(ValueError):
    """Raised when probe arguments are not safe argv values."""


@dataclass(frozen=True)
class TargetObservation:
    """Facts extracted from one s_client run (never serialized)."""

    reachable: bool
    protocol_version: Optional[str]
    alpn: Optional[str]
    server_temp_key: Optional[str]
    subject: Optional[str]
    peer_certificate: Optional[str]
    verification_ok: bool
    verify_return_code: Optional[int]
    verified_peername: Optional[str]
    sans: Tuple[str, ...]
    certificate_present: bool
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
        # Let openssl itself check the certificate name; the parsed
        # "Verify return code" then reflects chain AND hostname.
        "-verify_hostname",
        server_name,
        "-tls1_3",
        "-alpn",
        "h2",
        "-groups",
        "X25519",
    ]


def parse_s_client_output(text: str) -> TargetObservation:
    """Extract the summary facts; never raise on malformed output."""
    reachable = "CONNECTION ESTABLISHED" in text or bool(
        _CONNECTED_RE.search(text)
    )
    verify_code = _VERIFY_RETURN_RE.search(text)
    subject = _capture(_SUBJECT_RE, text)
    peer_certificate = _capture(_PEER_CERT_RE, text)
    verified_peername = _capture(_VERIFIED_PEERNAME_RE, text)
    return TargetObservation(
        reachable=reachable,
        protocol_version=_protocol_version(text),
        alpn=_capture(_ALPN_RE, text),
        server_temp_key=_capture(_TEMP_KEY_RE, text),
        subject=subject,
        peer_certificate=peer_certificate,
        verification_ok=bool(_VERIFIED_RE.search(text)),
        verify_return_code=(
            int(verify_code.group(1)) if verify_code is not None else None
        ),
        verified_peername=verified_peername,
        sans=tuple(_SAN_DNS_RE.findall(text)),
        # A handshake that fails before the Certificate message still
        # prints vacuous verification success; only positive evidence
        # counts as a certificate being present.
        certificate_present=(
            not _NO_PEER_CERT_RE.search(text)
            and bool(subject or peer_certificate or verified_peername)
        ),
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
        return _failed_result("timeout", elapsed, family)
    elapsed = int((time.monotonic() - started) * 1000)
    if returncode == 127 and not output.strip():
        return _failed_result("openssl_unavailable", elapsed, family)
    parsed = evaluate_target(
        parse_s_client_output(output), expected_server_name=server_name
    )
    return TargetResult(
        checks=parsed.checks,
        elapsed_ms=elapsed,
        error_code=parsed.error_code,
        address_family=family,
    )


def _failed_result(error_code: str, elapsed_ms: int, family: str) -> TargetResult:
    return TargetResult(
        checks={name: False for name in CHECK_NAMES},
        elapsed_ms=elapsed_ms,
        error_code=error_code,
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
        )[:MAX_OUTPUT_BYTES]
    return returncode, combined.decode("utf-8", errors="replace")


def _protocol_version(text: str) -> Optional[str]:
    """Return the negotiated protocol, ignoring attempted-only versions.

    A handshake that fails without a session still prints a bare
    "Protocol: TLSv1.3" reflecting the attempt, so that form (and the
    SSL-Session block) counts only alongside other negotiated
    evidence such as a real "New, TLSv..." session line, an ALPN
    result, a key-exchange line, or a received certificate.
    """
    new_token = _capture(_NEW_SESSION_RE, text)
    if new_token is not None and new_token.startswith("TLSv"):
        return new_token
    brief = _capture(_PROTOCOL_VERSION_RE, text)
    if brief is not None and brief.startswith("TLSv"):
        return brief
    if _handshake_completed_evidence(text):
        for pattern in (_PROTOCOL_RE, _SESSION_PROTOCOL_RE):
            captured = _capture(pattern, text)
            if captured and captured.startswith("TLSv"):
                return captured
    return None


def _handshake_completed_evidence(text: str) -> bool:
    return bool(
        _ALPN_RE.search(text)
        or _TEMP_KEY_RE.search(text)
        or _SUBJECT_RE.search(text)
        or _PEER_CERT_RE.search(text)
        or _VERIFIED_PEERNAME_RE.search(text)
    )


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


def _name_matches(name: Optional[str], expected: str) -> bool:
    if not name:
        return False
    candidate = name.strip().rstrip(".").lower()
    wanted = expected.strip().rstrip(".").lower()
    if candidate == wanted:
        return True
    if candidate.startswith("*."):
        # A wildcard covers exactly one extra label: *.example.com
        # matches www.example.com but not a.b.example.com.
        suffix = candidate[1:]
        if not wanted.endswith(suffix) or len(wanted) <= len(suffix):
            return False
        return "." not in wanted[: len(wanted) - len(suffix)]
    return candidate.endswith("." + wanted)


def _certificate_name_matches(
    observation: TargetObservation, expected_server_name: str
) -> bool:
    """Hostname decision, strongest evidence first.

    The -verify_hostname result (chain plus hostname) is authoritative,
    but only when a certificate was actually received: without one,
    openssl reports vacuous success.  The remaining signals only cover
    text that lacks the verification result.
    """
    if not observation.certificate_present:
        return False
    if observation.verify_return_code is not None:
        return observation.verify_return_code == 0
    if _name_matches(observation.verified_peername, expected_server_name):
        return True
    if observation.verification_ok:
        return True
    for san in observation.sans:
        if _name_matches(san, expected_server_name):
            return True
    for line in (observation.subject, observation.peer_certificate):
        if not line:
            continue
        match = _CN_RE.search(line)
        if match and _name_matches(match.group(1), expected_server_name):
            return True
    return False


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only REALITY dest probe (full-mode openssl s_client)."
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
