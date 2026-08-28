#!/usr/bin/env python3
"""Scan tracked repository content for leaked private values.

The scanner answers one question only: does anything Git tracks contain
private data?  It checks three things:

1. Tracked paths that must never exist (runtime private data, generated
   YAML, release manifests, environment files, private-key files, and
   the legacy ``1/`` tree).
2. Tracked text that contains concrete proxy URIs, bearer-token
   subscription paths, PEM private-key blocks, non-example UUIDs, URL
   userinfo, or random-looking bare 32-hex tokens.  Documentation
   placeholders built from RFC 5737 addresses, ``example.com``,
   repeated-digit UUIDs/hex, and hex embedded in rule-provider URLs
   stay allowed.
3. With ``--private-root``: credential-like scalar values extracted in
   memory from the ignored ``private/config``, ``private/sources``, and
   ``private/workbench`` trees -- and from the exact root
   ``private/home.yaml`` overlay, whose complete rules also join the
   comparison unless tracked documentation already publishes them --
   must not occur, byte for byte, in any tracked file.

Output is always a category and a tracked path.  A matched value is
never printed, logged, or embedded in an error message; malformed
private YAML is skipped silently and an unexpected internal failure
prints exactly one redacted line.

The scanner never follows symlinks, skips binary files, and reads at
most 10 MiB of any single file.  It exits 0 only when clean.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, List, Optional, Set, Tuple

import yaml


ROOT = Path(__file__).resolve().parents[1]

MAX_FILE_BYTES = 10 * 1024 * 1024
MIN_PRIVATE_VALUE_CHARS = 16

# Runtime directories under the ignored private root: tracked content
# anywhere below them is always a leak.  Plain top-level ``private/*.example``
# documentation files from the legacy layout stay allowed.
PRIVATE_RUNTIME_DIRECTORIES = (
    "private/config",
    "private/sources",
    "private/workbench",
    "private/releases",
    "private/current",
    "private/staging",
    "private/logs",
    "private/state",
    "private/reference-configs",
)
PRIVATE_KEY_EXTENSIONS = (".key", ".pem", ".p12", ".pfx", ".id_rsa", ".id_ed25519")
RUNTIME_MANIFEST_NAMES = ("manifest.json", "manifest.sha256")
RUNTIME_MANIFEST_SUFFIXES = (".meta.json",)

RESERVED_HOST_SUFFIXES = (
    "example.com",
    "example.net",
    "example.org",
    "localhost",
    "test",
)
RESERVED_HOST_PREFIXES = ("192.0.2.", "198.51.100.", "203.0.113.")
USERINFO_PLACEHOLDERS = frozenset(
    (
        "user",
        "user1",
        "username",
        "pass",
        "pass1",
        "password",
        "passwd",
        "example",
        "test",
        "fake",
        "dummy",
        "sample",
        "synthetic",
        "placeholder",
        "replace",
        "replace_me",
        "your_user",
        "your-user",
        "your_password",
        "your-password",
        "xxx",
        "xxxx",
        "xxxxx",
        "****",
        "changeme",
        "change-me",
    )
)
USERINFO_PLACEHOLDER_MARKERS = ("example", "placeholder", "replace")

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_PROXY_URI_RE = re.compile(
    r"(?i)\b(?:vless|vmess|trojan|ss|hysteria2|hy2|tuic|socks5h|socks5)://"
    r"[0-9A-Za-z_.~+-][^\s'\"`<>]*"
)
# The /s/ route prefix is optional: a bare <43-char-core>-<six-readable-code>
# leak reconstructs the subscription URL on its own, so flag both forms.  The
# left boundary keeps a token embedded in a longer identifier from matching.
_SUBSCRIPTION_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:/s/)?[A-Za-z0-9_-]{43}-"
    r"[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}(?![A-Za-z0-9_-])"
)
_PEM_KEY_RE = re.compile(r"-----BEGIN[A-Z0-9 ]*PRIVATE KEY-----[A-Za-z0-9+/=\r\n]{100,}")
_URL_USERINFO_RE = re.compile(
    r"[a-zA-Z][a-zA-Z0-9+.-]*://([^/@\s\"']+):([^/@\s\"']*)@"
)
# Bare 32-hex tokens (undashed UUID / token-hash form).  The boundary
# assertions keep 64-hex sha256 digests from matching.
_HEX_32_TOKEN_RE = re.compile(r"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
_URL_RE = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'`<>]+")
_HEX_32_RE = re.compile(r"^[0-9a-fA-F]{32,}$")
_RANDOM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_CREDENTIAL_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "uuid",
    "publickey",
    "privatekey",
    "shortid",
    "psk",
    "credential",
)
_PRIVATE_SCAN_DIRECTORIES = ("config", "sources", "workbench")
# The ignored root home overlay joins the private-value comparison as
# one exact filename, never a recursed directory.
_HOME_OVERLAY_FILENAME = "home.yaml"
# Tracked documentation legitimately publishes overlay references (the
# documented group names and example rules); suffixes listed here are
# that published baseline.
_DOCUMENTATION_SUFFIXES = (".md",)


class Finding:
    __slots__ = ("category", "path")

    def __init__(self, category: str, path: str):
        self.category = category
        self.path = path

    def __repr__(self) -> str:
        return "%s: %s" % (self.category, self.path)

    def __eq__(self, other) -> bool:
        if not isinstance(other, Finding):
            return NotImplemented
        return self.category == other.category and self.path == other.path

    def __hash__(self) -> int:
        return hash((self.category, self.path))


def load_tracked_paths(root: Path, undecodable: Optional[List[bytes]] = None) -> List[str]:
    """Return the paths Git tracks, without reading any file content."""
    completed = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(
            "scan failed: git ls-files exited %d" % completed.returncode
        )
    paths = []
    for raw in completed.stdout.split(b"\0"):
        if not raw:
            continue
        try:
            paths.append(raw.decode("utf-8"))
        except UnicodeDecodeError:
            if undecodable is not None:
                undecodable.append(raw)
    return paths


def forbidden_path_category(relative_path: str) -> Optional[str]:
    """Classify a tracked path that must not be tracked at all."""
    normalized = relative_path.replace("\\", "/")
    if normalized.startswith("1/") or normalized == "1":
        return "tracked-legacy-path"
    for directory in PRIVATE_RUNTIME_DIRECTORIES:
        if normalized == directory or normalized.startswith(directory + "/"):
            return "tracked-private-data"
    name = normalized.rsplit("/", 1)[-1]
    if normalized.startswith("generated/") and normalized.endswith((".yaml", ".yml")):
        return "tracked-generated-yaml"
    if name in RUNTIME_MANIFEST_NAMES:
        return "tracked-runtime-manifest"
    if name.endswith(RUNTIME_MANIFEST_SUFFIXES):
        return "tracked-runtime-manifest"
    if name == ".env" or (
        name.startswith(".env.")
        and not name.endswith((".example", ".example.yaml", ".example.yml"))
    ):
        return "tracked-env-file"
    if name.endswith(PRIVATE_KEY_EXTENSIONS):
        return "tracked-private-key-file"
    return None


def looks_like_example_uuid(value: str) -> bool:
    """Documentation UUIDs repeat one digit; random ones do not."""
    hex_digits = value.replace("-", "").lower()
    if len(hex_digits) != 32:
        return False
    counts = Counter(hex_digits)
    return counts.most_common(1)[0][1] >= 24


def looks_like_example_hex(value: str) -> bool:
    """Documentation hex repeats one digit or a short block; random does not."""
    lowered = value.lower()
    if len(lowered) != 32:
        return False
    counts = Counter(lowered)
    if counts.most_common(1)[0][1] >= 24:
        return True
    for block_size in range(1, 9):
        block = lowered[:block_size]
        if block * (32 // block_size) == lowered:
            return True
    return False


def _is_reserved_host(host: str) -> bool:
    lowered = host.strip().lower()
    if lowered in RESERVED_HOST_SUFFIXES:
        return True
    if any(lowered.startswith(prefix) for prefix in RESERVED_HOST_PREFIXES):
        return True
    return any(
        lowered.endswith("." + suffix) for suffix in RESERVED_HOST_SUFFIXES
    )


def _is_placeholder_credential(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered in USERINFO_PLACEHOLDERS:
        return True
    return any(marker in lowered for marker in USERINFO_PLACEHOLDER_MARKERS)


def _uri_host(uri: str) -> str:
    remainder = uri.split("://", 1)[1]
    if "@" in remainder:
        remainder = remainder.rsplit("@", 1)[1]
    for separator in (":", "/", "?", "#"):
        remainder = remainder.split(separator, 1)[0]
    return remainder


def _proxy_uri_is_documentation(uri: str) -> bool:
    host = _uri_host(uri)
    if not _is_reserved_host(host):
        return False
    uuids = _UUID_RE.findall(uri)
    if any(not looks_like_example_uuid(value) for value in uuids):
        return False
    authority = uri.split("://", 1)[1]
    if "@" in authority:
        userinfo = authority.rsplit("@", 1)[0]
        if ":" in userinfo:
            user, _, password = userinfo.partition(":")
            if not (
                _is_placeholder_credential(user)
                and _is_placeholder_credential(password)
            ):
                return False
        elif _UUID_RE.fullmatch(userinfo):
            if not looks_like_example_uuid(userinfo):
                return False
        elif not _is_placeholder_credential(userinfo):
            # Opaque single-token credentials (hysteria2/tuic auth,
            # ss base64 blobs, vless UUIDs without dashes) are real
            # secrets unless they are obvious placeholders.
            return False
    return True


def find_content_findings(text: str, relative_path: str) -> List[Finding]:
    """Return category findings for one tracked text file."""
    findings: Set[Finding] = set()
    for match in _PROXY_URI_RE.finditer(text):
        if not _proxy_uri_is_documentation(match.group(0)):
            findings.add(Finding("tracked-proxy-uri", relative_path))
    if _SUBSCRIPTION_TOKEN_RE.search(text):
        findings.add(Finding("tracked-subscription-token", relative_path))
    if _PEM_KEY_RE.search(text):
        findings.add(Finding("tracked-private-key-pem", relative_path))
    for match in _UUID_RE.finditer(text):
        if not looks_like_example_uuid(match.group(0)):
            findings.add(Finding("tracked-uuid", relative_path))
    for match in _URL_USERINFO_RE.finditer(text):
        if not (
            _is_placeholder_credential(match.group(1))
            and _is_placeholder_credential(match.group(2))
        ):
            findings.add(Finding("tracked-url-userinfo", relative_path))
    url_spans = [match.span() for match in _URL_RE.finditer(text)]
    for match in _HEX_32_TOKEN_RE.finditer(text):
        if any(start <= match.start() < end for start, end in url_spans):
            # Hex inside a URL (public rule-provider gist ids, and
            # credential-bearing URIs already covered above) is judged
            # by the URI rules, not the bare-token rule.
            continue
        if not looks_like_example_hex(match.group(0)):
            findings.add(Finding("tracked-hex-token", relative_path))
    return sorted(findings, key=repr)


def _credential_key_fragment(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return any(fragment in normalized for fragment in _CREDENTIAL_KEY_FRAGMENTS)


def _looks_like_random_token(value: str) -> bool:
    if not _RANDOM_TOKEN_RE.fullmatch(value):
        return False
    has_digit = any(character.isdigit() for character in value)
    has_letter = any(character.isalpha() for character in value)
    return has_digit and has_letter


def _url_segment_candidates(value: str) -> Iterable[str]:
    for segment in re.split(r"[/?#&]", value):
        if "://" in segment:
            continue
        if len(segment) >= MIN_PRIVATE_VALUE_CHARS and _looks_like_random_token(segment):
            yield segment


def _scalar_is_credential_like(key: str, value: str) -> bool:
    if len(value) < MIN_PRIVATE_VALUE_CHARS:
        return False
    if _credential_key_fragment(key):
        return True
    if _HEX_32_RE.fullmatch(value):
        return True
    if _UUID_RE.fullmatch(value):
        return True
    if _URL_USERINFO_RE.search(value):
        return True
    return _looks_like_random_token(value)


def _collect_scalars(node, key: str, out: List[Tuple[str, str]]) -> None:
    if isinstance(node, dict):
        for child_key, child_value in node.items():
            _collect_scalars(child_value, str(child_key), out)
    elif isinstance(node, list):
        for item in node:
            _collect_scalars(item, key, out)
    elif isinstance(node, str):
        out.append((key, node))


def _iter_private_yaml_files(private_root: Path) -> Iterable[Path]:
    home = private_root / _HOME_OVERLAY_FILENAME
    if not home.is_symlink() and home.is_file():
        yield home
    for directory in _PRIVATE_SCAN_DIRECTORIES:
        base = private_root / directory
        if not base.is_dir() or base.is_symlink():
            continue
        for path in sorted(base.rglob("*.yaml")):
            if path.is_symlink() or not path.is_file():
                continue
            yield path


def extract_private_values(
    private_root: Path, documentation_payloads: Optional[List[bytes]] = None
) -> Set[bytes]:
    """Extract private scalar bytes from ignored private YAML.

    The ``private/config``, ``private/sources``, and ``private/workbench``
    trees and the exact root ``private/home.yaml`` overlay file are read,
    entirely in memory; nothing is written and no value is ever printed.
    Malformed YAML is skipped silently: a parser error message would
    embed the offending source line, which may itself carry a private
    value.
    """
    values: Set[bytes] = set()
    home_path = private_root / _HOME_OVERLAY_FILENAME
    for path in _iter_private_yaml_files(private_root):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, yaml.YAMLError):
            continue
        scalars: List[Tuple[str, str]] = []
        _collect_scalars(document, "", scalars)
        for key, value in scalars:
            if _scalar_is_credential_like(key, value):
                values.add(value.encode("utf-8"))
            if "://" in value:
                for segment in _url_segment_candidates(value):
                    values.add(segment.encode("utf-8"))
        if path == home_path:
            _add_home_rule_values(document, values, documentation_payloads or [])
    return values


def _add_home_rule_values(
    document, values: Set[bytes], documentation_payloads: List[bytes]
) -> None:
    """Add complete home rules that tracked documentation has not published.

    A whole rule string is a leak needle only while it is unpublished:
    the tracked design notes legitimately quote the documented overlay
    rules, so a rule already occurring byte for byte in tracked
    documentation is a public reference rather than a private value.
    Rule text is never printed.
    """
    if not isinstance(document, dict):
        return
    rules = document.get("rules")
    if not isinstance(rules, list):
        return
    for rule in rules:
        if not isinstance(rule, str) or len(rule) < MIN_PRIVATE_VALUE_CHARS:
            continue
        encoded = rule.encode("utf-8")
        if any(encoded in payload for payload in documentation_payloads):
            continue
        values.add(encoded)


def _tracked_documentation_payloads(
    root: Path, tracked_paths: List[str]
) -> List[bytes]:
    """Return tracked documentation payloads, the published-value baseline."""
    payloads: List[bytes] = []
    for relative_path in tracked_paths:
        if not relative_path.endswith(_DOCUMENTATION_SUFFIXES):
            continue
        payload = _read_scannable_bytes(root / relative_path)
        if payload is not None:
            payloads.append(payload)
    return payloads


def _read_scannable_bytes(path: Path) -> Optional[bytes]:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        with path.open("rb") as handle:
            chunk = handle.read(MAX_FILE_BYTES)
    except OSError:
        return None
    if b"\0" in chunk:
        return None
    return chunk


def scan_repository(
    root: Path, private_root: Optional[Path] = None
) -> List[Finding]:
    """Scan every tracked path; return every category/path finding."""
    findings: Set[Finding] = set()
    undecodable: List[bytes] = []
    tracked_paths = load_tracked_paths(root, undecodable)
    for raw in undecodable:
        findings.add(
            Finding("tracked-undecodable-name", raw.decode("utf-8", "backslashreplace"))
        )
    private_values: Set[bytes] = set()
    if private_root is not None:
        private_values = extract_private_values(
            private_root, _tracked_documentation_payloads(root, tracked_paths)
        )
    for relative_path in tracked_paths:
        category = forbidden_path_category(relative_path)
        if category is not None:
            findings.add(Finding(category, relative_path))
            continue
        payload = _read_scannable_bytes(root / relative_path)
        if payload is None:
            continue
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError:
            text = payload.decode("latin-1")
        findings.update(find_content_findings(text, relative_path))
        if private_values and any(value in payload for value in private_values):
            findings.add(Finding("tracked-private-value", relative_path))
    return sorted(findings, key=lambda finding: (finding.path, finding.category))


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan tracked files for leaked private values without printing them."
    )
    parser.add_argument(
        "--private-root",
        type=Path,
        default=None,
        help="ignored private root whose config/sources/workbench and root home.yaml values must not appear tracked",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None, root: Optional[Path] = None) -> int:
    args = parse_args(argv)
    current_root = Path(root) if root is not None else ROOT
    try:
        findings = scan_repository(current_root, args.private_root)
    except Exception:
        # One redacted line only: an unexpected failure must never echo
        # private content through a traceback.
        print("scan failed: internal_error")
        return 2
    for finding in findings:
        print(repr(finding))
    if findings:
        print("scan failed: %d tracked finding(s)" % len(findings))
        return 1
    print("scan clean: %d tracked path(s) checked" % len(load_tracked_paths(current_root)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
