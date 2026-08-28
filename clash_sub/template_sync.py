"""Promote a private local balanced workbench into the shared templates.

``clash-sub template-sync`` is a development-machine command: it reads the
ignored ``private/workbench/balanced.yaml``, strips every dynamic node,
splits the remainder into the public template and the declared home
feature, re-validates the composed candidates with synthetic probes, and
only then atomically replaces the tracked template files.  It performs no
network access, no server actions, and no git operations.

Every failure raises :class:`TemplateSyncError` with one stable code and
never echoes workbench content, node names, or credentials.
"""

import copy
import importlib.util
import os
import re
import stat
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import yaml

from clash_sub.checks import CheckError, MihomoValidator, validate_clash
from clash_sub.domain import AirportProvider
from clash_sub.generator import render_user_bundle


WORKBENCH_RELATIVE_PATH = ("private", "workbench", "balanced.yaml")
MAX_WORKBENCH_BYTES = 5 * 1024 * 1024

TEMPLATE_RELATIVE_PATHS = (
    "templates/clash.yaml",
    "templates/features/home.yaml",
    "templates/variants/manifest.yaml",
)
FEATURE_NAME = "home"
TEMPLATE_FILE_MODE = 0o644
_FEATURE_OPERATION_KEYS = {
    "add-proxy-groups",
    "extend-proxy-groups",
    "prepend-rules",
    "inject-node-groups",
    "inject-home-node-groups",
}

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


class TemplateSyncError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _os_replace(source, target):
    os.replace(source, target)


def run_template_sync(repo_root, mihomo_binary=None, runner=subprocess.run):
    """Synchronize the workbench into the templates; return changed paths."""
    root = Path(repo_root)
    workbench = _load_workbench(root)
    mihomo = _resolve_mihomo(mihomo_binary)
    scanner = _load_scanner(root)
    candidate_public, candidate_feature, candidate_manifest = _split_workbench(root, workbench)
    forbidden_names, forbidden_values = _forbidden_values(workbench)

    with tempfile.TemporaryDirectory(prefix="clash-sub-template-sync.") as scratch:
        candidate_root = Path(scratch)
        _materialize_candidates(
            candidate_root, candidate_public, candidate_feature, candidate_manifest, root
        )
        _validate_candidates(
            candidate_root, mihomo, runner, forbidden_names, forbidden_values, scanner
        )

    payloads = _candidate_bytes(candidate_public, candidate_feature, candidate_manifest)
    _atomic_replace_templates(root, payloads)
    return {"changed": TEMPLATE_RELATIVE_PATHS}


def _resolve_mihomo(mihomo_binary):
    configured = mihomo_binary if mihomo_binary is not None else os.environ.get("MIHOMO_BIN", "")
    if isinstance(configured, Path):
        binary = configured
    elif isinstance(configured, str) and configured.strip():
        binary = Path(configured)
    else:
        raise TemplateSyncError("mihomo_binary_missing")
    if not binary.is_file():
        raise TemplateSyncError("mihomo_binary_missing")
    return binary


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


def _split_workbench(root, workbench):
    candidate = copy.deepcopy(workbench)
    dynamic_names = [proxy["name"] for proxy in candidate["proxies"]]
    dynamic = set(dynamic_names)
    candidate["proxies"] = []
    # The provider mapping and the injected provider use lists are composed
    # at render time; the shared templates must stay free of both.
    candidate.pop("proxy-providers", None)

    feature_path = root / "templates" / "features" / ("%s.yaml" % FEATURE_NAME)
    try:
        feature = yaml.safe_load(feature_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError):
        raise TemplateSyncError("template_feature_invalid") from None
    feature = _validated_feature(feature)
    owned_order = [group["name"] for group in feature["add-proxy-groups"]]
    owned = set(owned_order)
    declared = list(feature["inject-node-groups"]) + list(feature["inject-home-node-groups"])
    if set(declared) - owned:
        raise TemplateSyncError("template_feature_invalid")

    public_groups = []
    feature_groups = []
    extend_members = {}
    node_injected = []
    for group in candidate["proxy-groups"]:
        name = group.get("name")
        proxies = group.get("proxies")
        had_dynamic = isinstance(proxies, list) and any(
            member in dynamic for member in proxies if isinstance(member, str)
        )
        if name in owned:
            feature_groups.append(_without_provider_use(_strip_dynamic_members(group, dynamic)))
            continue
        if had_dynamic:
            node_injected.append(name)
        if not isinstance(proxies, list):
            public_groups.append(_without_provider_use(group))
            continue
        stripped = [
            member
            for member in proxies
            if member not in dynamic and member not in owned
        ]
        kept_owned = [member for member in proxies if member in owned]
        if kept_owned:
            extend_members[name] = kept_owned
        if stripped != proxies or kept_owned:
            group = dict(group, proxies=stripped)
        public_groups.append(_without_provider_use(group))

    if len(feature_groups) != len(owned_order):
        raise TemplateSyncError("template_feature_invalid")

    public_rules = []
    feature_rules = []
    home_positions = []
    for index, rule in enumerate(candidate["rules"]):
        if _rule_target(rule) in owned:
            feature_rules.append(rule)
            home_positions.append(index)
        else:
            public_rules.append(rule)
    # Feature rules can only be expressed as a prepended block; anything
    # other than a contiguous leading block in the workbench would silently
    # reorder public rules, so fail closed instead.
    if home_positions and home_positions != list(range(len(home_positions))):
        raise TemplateSyncError("template_rule_order_invalid")

    candidate["proxy-groups"] = public_groups
    candidate["rules"] = public_rules
    new_feature = {
        "add-proxy-groups": feature_groups,
        "extend-proxy-groups": extend_members,
        "prepend-rules": feature_rules,
        "inject-node-groups": list(feature.get("inject-node-groups", [])),
        "inject-home-node-groups": list(feature.get("inject-home-node-groups", [])),
    }

    manifest_path = root / "templates" / "variants" / "manifest.yaml"
    try:
        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, UnicodeError):
        raise TemplateSyncError("template_candidate_invalid") from None
    if not isinstance(manifest, dict) or not isinstance(manifest.get("variants"), dict):
        raise TemplateSyncError("template_candidate_invalid")
    new_manifest = {
        "variants": manifest["variants"],
        "inject-node-groups": node_injected,
    }
    return candidate, new_feature, new_manifest


def _strip_dynamic_members(group, dynamic):
    proxies = group.get("proxies")
    if not isinstance(proxies, list):
        return group
    stripped = [member for member in proxies if member not in dynamic]
    if stripped == proxies:
        return group
    return dict(group, proxies=stripped)


def _without_provider_use(group):
    """Drop the render-time provider use list from one split group copy."""
    if "use" not in group:
        return group
    stripped = dict(group)
    stripped.pop("use")
    return stripped


def _validated_feature(feature):
    """Reject any feature shape mismatch before list()/iteration runs."""
    def fail():
        raise TemplateSyncError("template_feature_invalid")

    if not isinstance(feature, dict):
        fail()
    if set(feature) - _FEATURE_OPERATION_KEYS:
        fail()
    add_groups = feature.get("add-proxy-groups", [])
    if not isinstance(add_groups, list):
        fail()
    names = []
    for group in add_groups:
        if not isinstance(group, dict):
            fail()
        name = group.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            fail()
        names.append(name)
    extend_groups = feature.get("extend-proxy-groups", {})
    if not isinstance(extend_groups, dict):
        fail()
    for group_name, members in extend_groups.items():
        if not isinstance(group_name, str) or not group_name.strip():
            fail()
        if not isinstance(members, list) or not all(
            isinstance(member, str) and member.strip() for member in members
        ):
            fail()
    for key in ("prepend-rules", "inject-node-groups", "inject-home-node-groups"):
        value = feature.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            fail()
    return feature


def _rule_target(rule):
    parts = [part.strip() for part in rule.split(",")]
    if len(parts) < 2:
        return None
    index = len(parts) - 1
    while index > 1 and parts[index] == "no-resolve":
        index -= 1
    return parts[index]


def _materialize_candidates(candidate_root, candidate_public, candidate_feature, candidate_manifest, root):
    templates = candidate_root / "templates"
    (templates / "features").mkdir(parents=True)
    (templates / "variants").mkdir(parents=True)
    _write_yaml(templates / "clash.yaml", candidate_public)
    _write_yaml(templates / "features" / ("%s.yaml" % FEATURE_NAME), candidate_feature)
    _write_yaml(templates / "variants" / "manifest.yaml", candidate_manifest)
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


def _probe_proxies():
    probes = []
    for label in ("3xui", "airport", "home"):
        probe = dict(_PROBE_REALITY)
        probe.update(
            {
                "name": _PROBE_PREFIX + label,
                "server": "192.0.2.%d" % (10 + len(probes)),
                "uuid": "55555555-5555-4555-8555-55555555555%d" % len(probes),
            }
        )
        probes.append(probe)
    return probes[0], probes[1], probes[2]


def _validate_candidates(candidate_root, mihomo, runner, forbidden_names, forbidden_values, scanner):
    xui, airport, home = _probe_proxies()
    provider = AirportProvider(_PROBE_PROVIDER_URL, _PROBE_PROVIDER_DIGEST)
    try:
        owner = render_user_bundle(
            True, [copy.deepcopy(xui)], provider, [copy.deepcopy(home)],
            candidate_root / "templates",
        )
        member = render_user_bundle(
            False, [copy.deepcopy(xui)], None, [], candidate_root / "templates"
        )
    except ValueError:
        raise TemplateSyncError("template_candidate_invalid") from None

    outputs = [owner["balanced"], owner["standard"], owner["privacy"], member["standard"]]
    texts = [output for output in outputs]
    texts.extend(
        (candidate_root / relative).read_text(encoding="utf-8")
        for relative in TEMPLATE_RELATIVE_PATHS
    )
    for text in outputs[:3]:
        try:
            validate_clash(text, (), allowed_provider_url=_PROBE_PROVIDER_URL)
        except CheckError:
            raise TemplateSyncError("template_candidate_invalid") from None
    try:
        validate_clash(outputs[3], ())
    except CheckError:
        raise TemplateSyncError("template_candidate_invalid") from None

    validator = MihomoValidator(mihomo, runner=runner)
    with tempfile.TemporaryDirectory(prefix="clash-sub-template-sync-validate.") as scratch:
        # Mihomo checks a local-file provider pointing at the synthetic
        # airport probe; the published shape keeps the HTTP provider mapping.
        airport_file = Path(scratch) / "AmyTelecom.yaml"
        airport_file.write_text(
            yaml.safe_dump({"proxies": [airport]}, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        for index, text in enumerate(outputs):
            document = yaml.safe_load(text)
            if index < 3:
                document["proxy-providers"]["AmyTelecom"] = {
                    "type": "file",
                    "path": str(airport_file),
                }
            candidate = Path(scratch) / ("probe-%d.yaml" % index)
            candidate.write_text(
                yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            try:
                validator.validate(candidate)
            except CheckError:
                raise TemplateSyncError("mihomo_validation_failed") from None

    _scan_for_secrets(texts, forbidden_names, forbidden_values, scanner)
    return outputs


def _scan_for_secrets(texts, forbidden_names, forbidden_values, scanner):
    tracked_texts = texts[len(texts) - len(TEMPLATE_RELATIVE_PATHS):]
    for index, text in enumerate(tracked_texts):
        if scanner.find_content_findings(text, TEMPLATE_RELATIVE_PATHS[index]):
            raise TemplateSyncError("template_secret_leak")

    # Private field values (server/uuid/password/credential-like) are so
    # distinctive that any substring occurrence is a leak.  Node names are
    # deliberately excluded here: a similarly-named static group is legal,
    # and the real leak channel for names is exact list membership, which
    # the member check below covers.
    for text in texts:
        for value in forbidden_values:
            if len(value) >= _MIN_FORBIDDEN_CHARS and value in text:
                raise TemplateSyncError("template_secret_leak")

    # Scalar-exact layer: a forbidden value appearing as a COMPLETE scalar
    # anywhere in a candidate document is a leak at any length, so short
    # credentials (secret: abc) cannot slip under the substring threshold.
    for text in texts:
        document = yaml.safe_load(text)
        scalars = set()
        _collect_string_scalars(document, scalars)
        if scalars & forbidden_values:
            raise TemplateSyncError("template_secret_leak")

    # Exact member check: no dynamic proxy name may survive as a group
    # member anywhere in the tracked candidates.
    for text in tracked_texts:
        document = yaml.safe_load(text)
        if not isinstance(document, dict):
            continue
        scalars = set()
        _collect_string_scalars(document, scalars)
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


def _candidate_bytes(candidate_public, candidate_feature, candidate_manifest):
    return {
        "templates/clash.yaml": _dump_yaml(candidate_public),
        "templates/features/home.yaml": _dump_yaml(candidate_feature),
        "templates/variants/manifest.yaml": _dump_yaml(candidate_manifest),
    }


def _dump_yaml(document):
    return (
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False, default_flow_style=False)
    ).encode("utf-8")


def _atomic_replace_templates(root, payloads):
    previous = []
    attempted = []
    try:
        for relative in TEMPLATE_RELATIVE_PATHS:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                previous.append((relative, target.read_bytes(), stat.S_IMODE(target.stat().st_mode)))
            else:
                previous.append((relative, None, None))
        for relative in TEMPLATE_RELATIVE_PATHS:
            # Record intent BEFORE writing: a target whose os.replace already
            # took effect when the failure lands must be restored as well.
            attempted.append(relative)
            _write_file_atomically(root / relative, payloads[relative], TEMPLATE_FILE_MODE)
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
                    target, payload, mode if mode is not None else TEMPLATE_FILE_MODE
                )
        except OSError:
            pass
