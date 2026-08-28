"""Promote a private local balanced workbench into tracked and private outputs.

``clash-sub template-sync`` is a development-machine command: it reads the
ignored ``private/workbench/balanced.yaml`` and the existing
``private/home.yaml`` ownership scope, splits the composed workbench back
into the public template candidates and the private home overlay candidate,
re-validates every candidate with synthetic probes, and only then atomically
replaces all three outputs.  The command performs no network access, no
server actions, no git operations, and no external binary validation.

Every failure raises :class:`TemplateSyncError` with one stable code and
never echoes workbench content, node names, or credentials.
"""

import copy
import importlib.util
import os
import re
import stat
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml

from clash_sub.checks import CheckError, validate_clash
from clash_sub.domain import AirportProvider, HomeOverlay
from clash_sub.generator import render_user_bundle
from clash_sub.sources import (
    HomeSourceError,
    dump_home_overlay,
    load_home_overlay,
    parse_home_overlay,
)


WORKBENCH_RELATIVE_PATH = ("private", "workbench", "balanced.yaml")
HOME_SCOPE_RELATIVE_PATH = "private/home.yaml"
MAX_WORKBENCH_BYTES = 5 * 1024 * 1024

OUTPUT_MODES = {
    "templates/clash.yaml": 0o644,
    "templates/variants/manifest.yaml": 0o644,
    "private/home.yaml": 0o600,
}
TEMPLATE_OUTPUT_PATHS = tuple(OUTPUT_MODES)
# The tracked, world-readable template outputs; the private home overlay is
# the remaining output and is always written 0600.
_PUBLIC_TEMPLATE_OUTPUTS = TEMPLATE_OUTPUT_PATHS[:2]

# Proxy fields whose values are protocol structure, not secrets: they may
# legitimately appear in rendered output (e.g. the synthetic probe nodes)
# and are therefore exempt from the forbidden-value set.  Everything else
# under a proxy -- including every nested protocol option such as
# plugin-opts, ws-opts headers, sni, or reality-opts -- is treated as
# private, so no field-name allowlist has to be maintained.
_STRUCTURAL_PROXY_KEYS = {
    "name",
    "type",
    "network",
    "cipher",
    "flow",
    "plugin",
    "client-fingerprint",
    "skip-cert-verify",
    "alpn",
    "interval",
    "enabled",
    "port",
    "tls",
    "udp",
}

# Synthetic probes used only to exercise candidate composition; none of
# these values may ever collide with workbench content.
_PROBE_PREFIX = "template-sync-probe-"
_PROBE_PROVIDER_URL = "https://template-sync.invalid/s/probe/AmyTelecom.yaml"
_PROBE_PROVIDER_DIGEST = "5" * 64
_PROBE_REALITY = {
    "type": "vless",
    "port": 443,
    "network": "tcp",
    "tls": True,
    "flow": "xtls-rprx-vision",
    "servername": "probe.invalid",
    "client-fingerprint": "chrome",
    "reality-opts": {
        "public-key": "5555555555555555555555555555555555555555555",
        "short-id": "5555555555555555",
    },
}

_PRIVATE_FIELD_KEYS = {
    "server",
    "uuid",
    "password",
    "token",
    "private-key",
    "public-key",
    "short-id",
    "psk",
    "secret",
    "servername",
    "username",
    "authentication",
}
_CREDENTIAL_QUERY_KEYS = {
    "apikey",
    "auth",
    "authorization",
    "credential",
    "key",
    "password",
    "passwd",
    "secret",
    "sig",
    "signature",
    "token",
}
_MIN_FORBIDDEN_CHARS = 4

# The home extension is deliberately not exported for the PT group; any
# attempt to sync such an extension is rejected before candidates exist.
_DENIED_EXTENSION_TARGET = "PT站加速"

# Clash rule options that may trail the policy target of a rule line.
_RULE_OPTION_TOKENS = frozenset(("no-resolve", "src"))


class TemplateSyncError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _os_replace(source, target):
    os.replace(source, target)


def run_template_sync(repo_root):
    """Synchronize the workbench into the outputs; return changed paths."""
    root = Path(repo_root)
    workbench = _load_workbench(root)
    home_scope = _load_home_scope(root)
    scanner = _load_scanner(root)
    candidate_public, candidate_manifest, candidate_home = _split_workbench(
        root, workbench, home_scope
    )
    forbidden_names, forbidden_values = _forbidden_values(workbench)
    home_names, home_values = _forbidden_home_values(candidate_home)
    forbidden_names |= home_names
    forbidden_values |= home_values

    with tempfile.TemporaryDirectory(prefix="clash-sub-template-sync.") as scratch:
        candidate_root = Path(scratch)
        _materialize_candidates(
            candidate_root, candidate_public, candidate_manifest, candidate_home, root
        )
        _validate_candidates(
            candidate_root, forbidden_names, forbidden_values, scanner
        )

    payloads = _candidate_bytes(candidate_public, candidate_manifest, candidate_home)
    _atomic_replace_outputs(root, payloads)
    return {"changed": TEMPLATE_OUTPUT_PATHS}


def _load_scanner(root):
    path = root / "scripts" / "scan_tracked_secrets.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "clash_sub._template_sync_scanner", path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        raise TemplateSyncError("template_candidate_invalid") from None
    return module


def _load_workbench(root):
    path = root.joinpath(*WORKBENCH_RELATIVE_PATH)
    try:
        details = path.lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or stat.S_IMODE(details.st_mode) != 0o600
            or details.st_uid != (0 if os.geteuid() == 0 else os.geteuid())
            or details.st_nlink != 1
        ):
            raise OSError
        payload = path.read_bytes()
    except OSError:
        raise TemplateSyncError("template_source_invalid") from None
    if len(payload) > MAX_WORKBENCH_BYTES:
        raise TemplateSyncError("template_source_invalid")
    try:
        text = payload.decode("utf-8")
        document = yaml.safe_load(text)
    except (UnicodeError, yaml.YAMLError):
        raise TemplateSyncError("template_source_invalid") from None
    if not isinstance(document, dict):
        raise TemplateSyncError("template_source_invalid")
    if any(str(key).startswith("_") for key in document):
        raise TemplateSyncError("template_source_invalid")
    try:
        validate_clash(text, (), allowed_provider_url=_workbench_provider_url(document))
    except CheckError:
        raise TemplateSyncError("template_source_invalid") from None
    return document


def _load_home_scope(root):
    """Load the ownership scope; it is also the private output target."""
    try:
        return load_home_overlay(root / HOME_SCOPE_RELATIVE_PATH, MAX_WORKBENCH_BYTES)
    except HomeSourceError as error:
        raise TemplateSyncError(error.code) from None


def _workbench_provider_url(document):
    """Authorize the workbench's own airport provider mapping, if present.

    A workbench rendered from the current generator carries exactly the
    synthetic AmyTelecom provider; any other provider shape stays rejected.
    """
    providers = document.get("proxy-providers")
    if not isinstance(providers, dict) or set(providers) != {"AmyTelecom"}:
        return None
    url = providers["AmyTelecom"].get("url") if isinstance(providers["AmyTelecom"], dict) else None
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    return url


def _split_workbench(root, workbench, home_scope):
    """Split one composed workbench into public, manifest, and home candidates.

    The scope's declared group names own the workbench's home groups; every
    undeclared group stays public.  The split is deterministic: home proxies
    are collected only from ``inject-home-node-groups`` members, copied home
    groups lose their runtime-injected members and provider ``use``, public
    groups lose home group members (recorded as extensions) and dynamic
    inline members, and rules follow their parsed policy target.
    """
    candidate = copy.deepcopy(workbench)
    inline_names = [proxy["name"] for proxy in candidate["proxies"]]
    inline = set(inline_names)
    groups = candidate["proxy-groups"]

    declared_order = [
        group.get("name")
        for group in home_scope.proxy_groups
        if isinstance(group, dict)
    ]
    declared = set(declared_order)
    counts = {}
    for group in groups:
        if isinstance(group, dict):
            name = group.get("name")
            counts[name] = counts.get(name, 0) + 1
    for name in declared_order:
        if counts.get(name, 0) != 1:
            # A declared home group that is missing (or was duplicated
            # before validation tightened) may never be silently dropped.
            raise TemplateSyncError("template_source_invalid")

    home_injected = set(home_scope.inject_home_node_groups)
    home_member_names = set()
    for group in groups:
        if (
            isinstance(group, dict)
            and group.get("name") in home_injected
            and isinstance(group.get("proxies"), list)
        ):
            for member in group["proxies"]:
                if isinstance(member, str) and member in inline:
                    home_member_names.add(member)
    home_proxies = [
        copy.deepcopy(proxy)
        for proxy in candidate["proxies"]
        if proxy["name"] in home_member_names
    ]
    home_inline = {proxy["name"] for proxy in home_proxies}

    all_injected = set(home_scope.inject_node_groups)
    private_groups = []
    public_groups = []
    node_injected_names = []
    extensions = {}
    for group in groups:
        if not isinstance(group, dict):
            raise TemplateSyncError("template_source_invalid")
        name = group.get("name")
        if name in declared:
            private_groups.append(
                _stripped_home_group(
                    group, name, all_injected, home_injected, inline, home_inline
                )
            )
            continue
        public_groups.append(
            _split_public_group(group, name, inline, declared, node_injected_names, extensions)
        )
    if _DENIED_EXTENSION_TARGET in extensions:
        raise TemplateSyncError("template_candidate_invalid")

    private_rules = []
    public_rules = []
    for rule in candidate["rules"]:
        if _rule_targets_home_group(rule, declared):
            private_rules.append(rule)
        else:
            public_rules.append(rule)
    candidate["rules"] = public_rules
    candidate["proxies"] = []
    # The provider mapping and the injected provider use lists are composed
    # at render time; the shared templates must stay free of both.
    candidate.pop("proxy-providers", None)
    candidate["proxy-groups"] = public_groups

    manifest_path = root / "templates" / "variants" / "manifest.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError):
        raise TemplateSyncError("template_candidate_invalid") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("variants"), dict):
        raise TemplateSyncError("template_candidate_invalid")
    new_manifest = {
        "variants": manifest["variants"],
        "inject-node-groups": node_injected_names,
    }
    home = HomeOverlay(
        proxies=tuple(home_proxies),
        proxy_groups=tuple(private_groups),
        extend_proxy_groups=extensions,
        inject_node_groups=tuple(home_scope.inject_node_groups),
        inject_home_node_groups=tuple(home_scope.inject_home_node_groups),
        rules=tuple(private_rules),
    )
    return candidate, new_manifest, home


def _stripped_home_group(group, name, all_injected, home_injected, inline, home_inline):
    """Copy one home group without its runtime-injected members and use."""
    copied = _without_provider_use(group)
    members = copied.get("proxies")
    if not isinstance(members, list):
        return copied
    if name in all_injected:
        injected = inline
    elif name in home_injected:
        injected = home_inline
    else:
        # A declared home group outside both injection lists receives no
        # runtime members at all, so none of its members are stripped.
        injected = set()
    stripped = [
        member
        for member in members
        if not (isinstance(member, str) and member in injected)
    ]
    return dict(copied, proxies=stripped)


def _split_public_group(group, name, inline, declared, node_injected_names, extensions):
    """Copy one public group, recording home extensions and node injection."""
    copied = _without_provider_use(group)
    proxies = copied.get("proxies")
    if not isinstance(proxies, list):
        return copied
    if any(isinstance(member, str) and member in inline for member in proxies):
        node_injected_names.append(name)
    kept = []
    removed_home = []
    for member in proxies:
        if isinstance(member, str) and member in declared:
            removed_home.append(member)
        else:
            kept.append(member)
    if removed_home:
        extensions[name] = removed_home
    stripped = [member for member in kept if not (isinstance(member, str) and member in inline)]
    if stripped != proxies:
        copied = dict(copied, proxies=stripped)
    return copied


def _without_provider_use(group):
    """Drop the render-time provider use list from one split group copy."""
    if "use" not in group:
        return group
    stripped = dict(group)
    stripped.pop("use")
    return stripped


def _rule_targets_home_group(rule, home_group_names):
    if not isinstance(rule, str):
        return False
    parts = rule.strip().split(",")
    if not parts:
        return False
    target = parts[-1]
    if target in _RULE_OPTION_TOKENS and len(parts) >= 3:
        target = parts[-2]
    return target in home_group_names


def _materialize_candidates(candidate_root, candidate_public, candidate_manifest, candidate_home, root):
    templates = candidate_root / "templates"
    (templates / "variants").mkdir(parents=True)
    _write_yaml(templates / "clash.yaml", candidate_public)
    _write_yaml(templates / "variants" / "manifest.yaml", candidate_manifest)
    (candidate_root / "private").mkdir()
    (candidate_root / "private" / "home.yaml").write_bytes(dump_home_overlay(candidate_home))
    try:
        source = root / "templates" / "variants" / "privacy-dns.yaml"
        (templates / "variants" / "privacy-dns.yaml").write_bytes(source.read_bytes())
    except OSError:
        raise TemplateSyncError("template_candidate_invalid") from None


def _write_yaml(path, document):
    try:
        path.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )
    except (OSError, yaml.YAMLError):
        raise TemplateSyncError("template_candidate_invalid") from None


def _probe_proxy():
    probe = dict(_PROBE_REALITY)
    probe.update(
        {
            "name": _PROBE_PREFIX + "3xui",
            "server": "192.0.2.10",
            "uuid": "55555555-5555-4555-8555-555555555555",
        }
    )
    return probe


def _validate_candidates(candidate_root, forbidden_names, forbidden_values, scanner):
    xui = _probe_proxy()
    provider = AirportProvider(_PROBE_PROVIDER_URL, _PROBE_PROVIDER_DIGEST)
    try:
        home = parse_home_overlay(
            (candidate_root / "private" / "home.yaml").read_bytes(), MAX_WORKBENCH_BYTES
        )
        # All four authorization cases are composed from the candidates with
        # synthetic node sources; the private overlay candidate proves the
        # new public templates still accept the real home composition.
        owner = render_user_bundle(
            True, [copy.deepcopy(xui)], provider, home, candidate_root / "templates"
        )
        member = render_user_bundle(
            False, [copy.deepcopy(xui)], None, None, candidate_root / "templates"
        )
    except (ValueError, HomeSourceError, OSError, yaml.YAMLError, UnicodeError):
        raise TemplateSyncError("template_candidate_invalid") from None

    outputs = [owner["balanced"], owner["standard"], owner["privacy"], member["standard"]]
    for index, text in enumerate(outputs):
        try:
            if index < 3:
                validate_clash(text, (), allowed_provider_url=_PROBE_PROVIDER_URL)
            else:
                validate_clash(text, ())
        except CheckError:
            raise TemplateSyncError("template_candidate_invalid") from None

    public_candidates = [
        (relative, (candidate_root / relative).read_text(encoding="utf-8"))
        for relative in _PUBLIC_TEMPLATE_OUTPUTS
    ]
    _scan_for_secrets(public_candidates, forbidden_names, forbidden_values, scanner)


def _scan_for_secrets(public_candidates, forbidden_names, forbidden_values, scanner):
    for relative, text in public_candidates:
        if scanner.find_content_findings(text, relative):
            raise TemplateSyncError("template_secret_leak")

    # Private field values (server/uuid/password/credential-like) are so
    # distinctive that any substring occurrence is a leak.  Node names are
    # deliberately excluded here: a similarly-named static group is legal,
    # and the real leak channel for names is exact list membership, which
    # the member check below covers.
    for _relative, text in public_candidates:
        for value in forbidden_values:
            if len(value) >= _MIN_FORBIDDEN_CHARS and value in text:
                raise TemplateSyncError("template_secret_leak")

    # Scalar-exact layer: a forbidden value appearing as a COMPLETE scalar
    # anywhere in a candidate document is a leak at any length, so short
    # credentials (secret: abc) cannot slip under the substring threshold.
    for _relative, text in public_candidates:
        document = yaml.safe_load(text)
        if not isinstance(document, dict):
            continue
        scalars = set()
        _collect_string_scalars(document, scalars)
        if scalars & forbidden_values:
            raise TemplateSyncError("template_secret_leak")

        # Exact member check: no dynamic proxy name may survive as a group
        # member anywhere in the tracked candidates.
        for scalar in scalars:
            if scalar in forbidden_names or (
                "://" in scalar
                and any(
                    len(name) >= _MIN_FORBIDDEN_CHARS and name in scalar
                    for name in forbidden_names
                )
            ):
                raise TemplateSyncError("template_secret_leak")
        for group in document.get("proxy-groups", []) or []:
            if isinstance(group, dict):
                for member in group.get("proxies", []) or []:
                    if member in forbidden_names:
                        raise TemplateSyncError("template_secret_leak")


def _collect_string_scalars(node, out):
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str):
                out.add(key)
            _collect_string_scalars(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_string_scalars(item, out)
    elif isinstance(node, str):
        out.add(node)


def _forbidden_values(workbench):
    names = []
    values = set()
    for proxy in workbench["proxies"]:
        for path, value in _iter_scalar_paths(proxy):
            key = path[-1] if path else ""
            if path == ("name",):
                if isinstance(value, str) and value:
                    names.append(value)
                continue
            if len(path) == 1 and key in _STRUCTURAL_PROXY_KEYS:
                continue
            if isinstance(value, str) and value:
                values.add(value)
    # Credential-like scalars anywhere OUTSIDE proxies (top-level runtime
    # secrets such as `secret`/`authentication`, or nested auth fields) are
    # copied verbatim into the public template.  Every one of them is
    # therefore a forbidden value: the candidate scan rejects the whole sync
    # instead of promoting them into tracked content.
    without_proxies = {key: value for key, value in workbench.items() if key != "proxies"}
    for key, value in _iter_scalars(without_proxies):
        if isinstance(value, str) and value and (
            str(key).lower() in _PRIVATE_FIELD_KEYS
            or _looks_credential_like(key, value)
            or _url_has_credentials(value)
        ):
            values.add(value)
    return set(names), values


def _forbidden_home_values(home):
    """The candidate's own home names and rules may never surface publicly."""
    names = {
        entry["name"]
        for entries in (home.proxies, home.proxy_groups)
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    return names, set(home.rules)


def _iter_scalar_paths(node, path=()):
    if isinstance(node, dict):
        for child_key, value in node.items():
            yield from _iter_scalar_paths(value, path + (str(child_key),))
    elif isinstance(node, list):
        for item in node:
            yield from _iter_scalar_paths(item, path)
    else:
        yield path, node


def _iter_scalars(node, key=""):
    if isinstance(node, dict):
        for child_key, value in node.items():
            yield from _iter_scalars(value, str(child_key))
    elif isinstance(node, list):
        for item in node:
            yield from _iter_scalars(item, key)
    else:
        yield key, node


def _looks_credential_like(key, value):
    lowered = str(key).lower()
    normalized = re.sub(r"[^a-z0-9]", "", lowered)
    fragments = (
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
        "authentication",
        "authorization",
    )
    if normalized == "auth" or any(fragment in normalized for fragment in fragments):
        return True
    if len(value) < 16:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return True
    if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", value):
        return True
    return False


def _url_has_credentials(value):
    if "://" not in value:
        return False
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.netloc:
            return False
        if parsed.username is not None or parsed.password is not None:
            return True
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            normalized = re.sub(r"[^a-z0-9]", "", key.lower())
            if normalized in _CREDENTIAL_QUERY_KEYS or any(
                fragment in normalized
                for fragment in ("password", "secret", "token", "credential")
            ):
                return True
    except (TypeError, ValueError, UnicodeError):
        return False
    return False


def _candidate_bytes(candidate_public, candidate_manifest, candidate_home):
    return {
        "templates/clash.yaml": _dump_yaml(candidate_public),
        "templates/variants/manifest.yaml": _dump_yaml(candidate_manifest),
        "private/home.yaml": dump_home_overlay(candidate_home),
    }


def _dump_yaml(document):
    return (
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False, default_flow_style=False)
    ).encode("utf-8")


def _atomic_replace_outputs(root, payloads):
    previous = []
    attempted = []
    try:
        for relative in TEMPLATE_OUTPUT_PATHS:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                previous.append((relative, target.read_bytes(), stat.S_IMODE(target.stat().st_mode)))
            else:
                previous.append((relative, None, None))
        for relative in TEMPLATE_OUTPUT_PATHS:
            # Record intent BEFORE writing: a target whose os.replace already
            # took effect when the failure lands must be restored as well.
            attempted.append(relative)
            _write_file_atomically(root / relative, payloads[relative], OUTPUT_MODES[relative])
    except OSError:
        # Snapshot failures leave attempted empty (nothing written, nothing
        # to restore); write failures restore every attempted target.
        _restore_files(root, previous, attempted)
        raise TemplateSyncError("template_write_failed") from None


def _write_file_atomically(target, payload, mode):
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % target.name, dir=str(target.parent)
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        _os_replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)


def _restore_files(root, previous, attempted):
    attempted_set = set(attempted)
    for relative, payload, mode in previous:
        if relative not in attempted_set:
            continue
        target = root / relative
        try:
            if payload is None:
                target.unlink(missing_ok=True)
            else:
                _write_file_atomically(
                    target, payload, mode if mode is not None else OUTPUT_MODES[relative]
                )
        except OSError:
            pass
