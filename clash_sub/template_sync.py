"""Build tracked Clash templates from synthetic-safe local ClashX inputs.

The updater is deliberately local and deterministic.  It reads the two
explicitly named ClashX profiles, removes dynamic and home-owned objects from
the tracked Compat candidate, stores the complete Balance ``dns`` mapping as
its own round-trip document, and atomically replaces only the selected output
files.  All failures use short stable error codes; source values are never
included in exceptions or reports.
"""

import copy
import importlib.util
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from clash_sub.checks import CheckError, validate_clash
from clash_sub.domain import HomeOverlay
from clash_sub.sources import (
    HomeSourceError,
    dump_home_overlay,
    load_home_overlay,
    parse_home_overlay,
)
from clash_sub.yaml_rt import (
    RoundTripYamlError,
    clone_round_trip,
    copy_key_comments,
    dump_round_trip,
    load_round_trip,
    plain_data,
)


ICLOUD_RELATIVE_ROOT = Path(
    "Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents"
)
COMPAT_SOURCE_NAME = "Compat-Office.yaml"
BALANCE_SOURCE_NAME = "Balance-Office.yaml"

PUBLIC_TEMPLATE_FILES = (
    "templates/base/compat-office.yaml",
    "templates/dns/balance-office.yaml",
    "templates/profiles.yaml",
)
HOME_SCOPE_PATH = "private/home.yaml"
TEMPLATE_OUTPUT_PATHS = PUBLIC_TEMPLATE_FILES + (HOME_SCOPE_PATH,)
OUTPUT_MODES = {
    "templates/base/compat-office.yaml": 0o644,
    "templates/dns/balance-office.yaml": 0o644,
    "templates/profiles.yaml": 0o644,
    "private/home.yaml": 0o600,
}
MAX_SOURCE_BYTES = 5 * 1024 * 1024

_PROBE_PROVIDER_URL = "https://template-sync.invalid/s/probe/AmyTelecom.yaml"
_PROBE_PROVIDER_DIGEST = "5" * 64
_PROBE_NAME = "template-sync-probe-3xui"
_PROBE_PROXY = {
    "name": _PROBE_NAME,
    "type": "vless",
    "server": "192.0.2.10",
    "port": 443,
    "uuid": "55555555-5555-4555-8555-555555555555",
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
_PROBE_PROVIDER = {
    "type": "http",
    "url": _PROBE_PROVIDER_URL,
    "interval": 0,
    "path": "./proxy_providers/AmyTelecom-%s.yaml" % _PROBE_PROVIDER_DIGEST,
}

_PROVIDER_NAME = "AmyTelecom"
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
    "authorization",
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
_RULE_OPTION_TOKENS = frozenset(("no-resolve", "src"))
_HOME_RULE_POLICIES = frozenset(
    ("DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL")
)
_PROXY_STRUCTURAL_KEYS = {
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
_HOME_KEYS = (
    "proxies",
    "proxy-groups",
    "extend-proxy-groups",
    "inject-node-groups",
    "inject-home-node-groups",
    "rules",
)
_DROP_PRIVATE_REFERENCE = object()


class TemplateSyncError(RuntimeError):
    """A redacted, stable template-sync failure."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TemplateSyncReport:
    changed: tuple[str, ...]
    lines: tuple[str, ...]


@dataclass(frozen=True)
class _SplitResult:
    public: Mapping
    home: HomeOverlay | None
    inject_node_groups: tuple[str, ...]
    dynamic_names: frozenset[str]


def _os_replace(source, target):
    os.replace(source, target)


def default_source_paths(home: Path | None = None) -> tuple[Path, Path]:
    """Return the fixed ClashX Compat and Balance source paths."""
    home_root = Path.home() if home is None else Path(home)
    source_root = home_root / ICLOUD_RELATIVE_ROOT
    return source_root / COMPAT_SOURCE_NAME, source_root / BALANCE_SOURCE_NAME


def run_template_sync(
    repo_root: Path,
    compat_office: Path | None = None,
    balance_office: Path | None = None,
) -> TemplateSyncReport:
    """Synchronize the explicitly selected Compat and/or Balance source.

    With no source arguments both default ClashX files are read.  Supplying
    one argument selects exactly that source and leaves the other outputs
    untouched.
    """
    root = Path(repo_root)
    if compat_office is None and balance_office is None:
        compat_office, balance_office = default_source_paths()

    compat_selected = compat_office is not None
    balance_selected = balance_office is not None
    scope = _load_home_scope(root)

    compat_source = None
    balance_source = None
    if compat_selected:
        compat_source = _load_source(Path(compat_office))
        split = _split_source(compat_source, scope)
        compat_candidate = split.public
        home_candidate = split.home
        profiles_candidate = _build_profiles(split.inject_node_groups)
    else:
        compat_candidate = _load_current_candidate(root, PUBLIC_TEMPLATE_FILES[0])
        home_candidate = scope
        profiles_candidate = _load_current_candidate(root, PUBLIC_TEMPLATE_FILES[2])
        _validate_profiles_document(profiles_candidate)

    balance_candidate = None
    if balance_selected:
        balance_source = _load_source(Path(balance_office))
        balance_candidate = _extract_balance_dns(
            balance_source, compat_candidate, scope
        )

    candidates = {}
    if compat_selected:
        candidates[PUBLIC_TEMPLATE_FILES[0]] = compat_candidate
        candidates[PUBLIC_TEMPLATE_FILES[2]] = profiles_candidate
        candidates[HOME_SCOPE_PATH] = dump_home_overlay(home_candidate)
    if balance_selected:
        candidates[PUBLIC_TEMPLATE_FILES[1]] = balance_candidate

    forbidden_names, forbidden_values = _forbidden_values(
        (source for source in (compat_source, balance_source) if source is not None),
        scope,
    )
    _validate_candidates(
        root,
        compat_candidate,
        profiles_candidate,
        balance_candidate,
        home_candidate,
        forbidden_names,
        forbidden_values,
    )

    payloads = _serialize_candidates(candidates)
    selected = tuple(relative for relative in TEMPLATE_OUTPUT_PATHS if relative in payloads)
    previous = _snapshot_targets(root, selected)
    report = _build_report(
        root,
        payloads,
        previous,
        compat_selected,
        balance_selected,
        compat_candidate,
        home_candidate,
        scope,
    )
    _atomic_replace_outputs(root, payloads)
    return report


def initialize_home_scope(
    repo_root: Path, compat_office: Path, compat_universal: Path
) -> Path:
    """Derive and atomically write the initial private home scope."""
    office = _load_source(Path(compat_office))
    universal = _load_source(Path(compat_universal))
    home = _derive_home_from_pair(office, universal)
    try:
        payload = dump_home_overlay(home)
    except HomeSourceError:
        raise TemplateSyncError("template_candidate_invalid") from None
    root = Path(repo_root)
    _atomic_replace_outputs(root, {HOME_SCOPE_PATH: payload})
    return root / HOME_SCOPE_PATH


def _load_home_scope(root):
    try:
        return load_home_overlay(root / HOME_SCOPE_PATH, MAX_SOURCE_BYTES)
    except HomeSourceError as error:
        raise TemplateSyncError(error.code) from None


def _read_regular(path, *, require_mode=None, error_code="template_source_invalid"):
    try:
        details = Path(path).lstat()
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_ISLNK(details.st_mode)
            or details.st_nlink != 1
            or (require_mode is not None and stat.S_IMODE(details.st_mode) != require_mode)
        ):
            raise OSError
        with Path(path).open("rb") as handle:
            payload = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        raise TemplateSyncError(error_code) from None
    if len(payload) > MAX_SOURCE_BYTES:
        raise TemplateSyncError(error_code)
    return payload


def _load_source(path):
    payload = _read_regular(path)
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        raise TemplateSyncError("template_source_invalid") from None
    if "{{" in text or "{%" in text:
        raise TemplateSyncError("template_source_invalid")
    try:
        document = load_round_trip(payload)
    except RoundTripYamlError:
        raise TemplateSyncError("template_source_invalid") from None
    if any(str(key).startswith("_") for key in document):
        raise TemplateSyncError("template_source_invalid")
    _validate_source_document(document, text)
    return document


def _validate_source_document(document, text):
    source_proxy_names = set(_source_names(document, "proxies"))
    source_group_names = set(_source_names(document, "proxy-groups"))
    providers = document.get("proxy-providers")
    if providers is None:
        validation_document = document
        provider_url = None
    else:
        if not isinstance(providers, Mapping) or len(providers) != 1:
            raise TemplateSyncError("template_source_invalid")
        provider_name, provider = next(iter(providers.items()))
        if not isinstance(provider_name, str) or not provider_name.strip():
            raise TemplateSyncError("template_source_invalid")
        if provider_name == _PROVIDER_NAME:
            provider_url = provider.get("url") if isinstance(provider, Mapping) else None
            if not isinstance(provider_url, str) or not provider_url.startswith("https://"):
                raise TemplateSyncError("template_source_invalid")
            validation_document = document
        elif (
            isinstance(provider, Mapping)
            and set(provider) == {"type", "path"}
            and provider.get("type") == "file"
            and isinstance(provider.get("path"), str)
            and bool(provider.get("path").strip())
        ):
            validation_document = clone_round_trip(document)
            validation_document["proxy-providers"] = CommentedMap(
                {_PROVIDER_NAME: clone_round_trip(_PROBE_PROVIDER)}
            )
            groups = validation_document.get("proxy-groups")
            if not isinstance(groups, list):
                raise TemplateSyncError("template_source_invalid")
            for group in groups:
                if not isinstance(group, Mapping):
                    raise TemplateSyncError("template_source_invalid")
                uses = _group_uses(group)
                if uses is None:
                    continue
                if not isinstance(uses, list) or any(
                    use != provider_name for use in uses
                ):
                    raise TemplateSyncError("template_source_invalid")
                _drop_merged_provider_use(group)
                group["use"] = CommentedSeq([_PROVIDER_NAME] * len(uses))
                if "proxies" not in group and group.get("include-all") is not True:
                    group["proxies"] = CommentedSeq()
            external_names = _external_provider_members(
                document, source_proxy_names, source_group_names
            )
            if external_names:
                for group in groups:
                    members = group.get("proxies")
                    if isinstance(members, list):
                        group["proxies"] = CommentedSeq(
                            member for member in members if member not in external_names
                        )
            provider_url = _PROBE_PROVIDER_URL
        else:
            raise TemplateSyncError("template_source_invalid")
    try:
        if validation_document is not document:
            text = dump_round_trip(validation_document)
        validate_clash(text, (), allowed_provider_url=provider_url)
    except CheckError:
        raise TemplateSyncError("template_source_invalid") from None


def _load_current_candidate(root, relative):
    payload = _read_regular(
        root / relative, error_code="template_candidate_invalid"
    )
    try:
        text = payload.decode("utf-8")
        if "{{" in text or "{%" in text:
            raise ValueError
        document = load_round_trip(payload)
    except (UnicodeError, RoundTripYamlError, ValueError):
        raise TemplateSyncError("template_candidate_invalid") from None
    if any(str(key).startswith("_") for key in document):
        raise TemplateSyncError("template_candidate_invalid")
    return document


def _source_names(document, key):
    entries = document.get(key)
    if not isinstance(entries, list):
        raise TemplateSyncError("template_source_invalid")
    names = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, Mapping) else None
        if not isinstance(name, str) or not name.strip():
            raise TemplateSyncError("template_source_invalid")
        names.append(name)
    if len(set(names)) != len(names):
        raise TemplateSyncError("template_source_invalid")
    return tuple(names)


def _source_file_provider_name(document):
    providers = document.get("proxy-providers")
    if not isinstance(providers, Mapping) or len(providers) != 1:
        return None
    provider_name, provider = next(iter(providers.items()))
    if (
        not isinstance(provider_name, str)
        or not provider_name.strip()
        or not isinstance(provider, Mapping)
        or set(provider) != {"type", "path"}
        or provider.get("type") != "file"
        or not isinstance(provider.get("path"), str)
        or not provider.get("path").strip()
    ):
        return None
    return provider_name


def _external_provider_members(document, proxy_names, group_names):
    if _source_file_provider_name(document) is None:
        return frozenset()
    builtins = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "COMPATIBLE", "GLOBAL"}
    external = set()
    for group in document.get("proxy-groups", []) or []:
        if not isinstance(group, Mapping):
            continue
        members = group.get("proxies")
        if not isinstance(members, list):
            continue
        external.update(
            member
            for member in members
            if isinstance(member, str)
            and member not in proxy_names | group_names | builtins
        )
    return frozenset(external)


def _prune_private_references(node, names):
    if isinstance(node, Mapping):
        if node.get("name") in names:
            return _DROP_PRIVATE_REFERENCE
        for key in tuple(node):
            child = _prune_private_references(node[key], names)
            if child is _DROP_PRIVATE_REFERENCE:
                del node[key]
        return node
    if isinstance(node, list):
        for index in range(len(node) - 1, -1, -1):
            if _prune_private_references(node[index], names) is _DROP_PRIVATE_REFERENCE:
                del node[index]
        return node
    if isinstance(node, str) and node in names:
        return _DROP_PRIVATE_REFERENCE
    return node


def _split_source(
    source,
    scope,
    *,
    allow_missing_home=False,
    build_home=True,
):
    """Split one full profile into a sanitized public candidate and home."""
    source_proxy_names = _source_names(source, "proxies")
    source_group_names = _source_names(source, "proxy-groups")
    home_proxy_names = {
        item["name"] for item in scope.proxies if isinstance(item, Mapping)
    }
    home_group_names = {
        item["name"] for item in scope.proxy_groups if isinstance(item, Mapping)
    }
    if not allow_missing_home:
        if not home_proxy_names.issubset(source_proxy_names):
            raise TemplateSyncError("template_source_invalid")
        if not home_group_names.issubset(source_group_names):
            raise TemplateSyncError("template_source_invalid")
    dynamic_names = frozenset(set(source_proxy_names) - home_proxy_names)
    external_provider_names = _external_provider_members(
        source, set(source_proxy_names), set(source_group_names)
    )
    all_inline = set(source_proxy_names)
    home_inline = set(source_proxy_names) & home_proxy_names
    all_injected = set(scope.inject_node_groups)
    home_injected = set(scope.inject_home_node_groups)

    groups = source["proxy-groups"]
    private_group_values = []
    private_group_indices = []
    public_group_values = []
    public_group_indices = []
    inject_node_groups = []
    extensions = CommentedMap()
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping):
            raise TemplateSyncError("template_source_invalid")
        name = group.get("name")
        if name in home_group_names:
            private = _strip_private_group(group, name, all_injected, home_injected, all_inline, home_inline)
            private_group_indices.append(index)
            private_group_values.append(private)
            continue
        public = clone_round_trip(group)
        _drop_provider_use(public)
        members = group.get("proxies")
        if isinstance(members, list):
            kept_indices = []
            extension_indices = []
            if any(isinstance(member, str) and member in dynamic_names for member in members):
                inject_node_groups.append(name)
            for member_index, member in enumerate(members):
                if isinstance(member, str) and member in home_group_names:
                    extension_indices.append(member_index)
                elif isinstance(member, str) and member in home_proxy_names:
                    # HomeOverlay can restore private groups through an
                    # extension, but it cannot restore a home node embedded
                    # directly in a public group.  Reject rather than lose it.
                    raise TemplateSyncError("template_source_invalid")
                elif isinstance(member, str) and member in external_provider_names:
                    continue
                elif isinstance(member, str) and member in all_inline:
                    continue
                else:
                    kept_indices.append(member_index)
            public["proxies"] = _select_sequence(members, kept_indices)
            if extension_indices:
                extensions[name] = _select_sequence(members, extension_indices)
        elif _group_uses(group) is not None and "include-all" not in group:
            public["proxies"] = CommentedSeq()
        public_group_indices.append(index)
        public_group_values.append(public)

    public = clone_round_trip(source)
    public["proxies"] = CommentedSeq()
    public["proxy-groups"] = _sequence_with_values(
        groups, public_group_indices, public_group_values
    )
    public_rules = source.get("rules")
    if not isinstance(public_rules, list):
        raise TemplateSyncError("template_source_invalid")
    private_rule_indices = [
        index
        for index, rule in enumerate(public_rules)
        if _rule_targets_private(rule, home_group_names | home_proxy_names)
    ]
    private_rule_index_set = set(private_rule_indices)
    public_rule_indices = [
        index for index in range(len(public_rules)) if index not in private_rule_index_set
    ]
    public["rules"] = _select_sequence(public_rules, public_rule_indices)
    if "proxy-providers" in public:
        del public["proxy-providers"]
    private_names = (
        set(source_proxy_names)
        | home_group_names
        | set(external_provider_names)
        | {_PROVIDER_NAME}
    )
    for key in tuple(public):
        if key in {
            "dns",
            "proxies",
            "proxy-groups",
            "rule-providers",
            "rules",
        }:
            continue
        if _prune_private_references(public[key], private_names) is _DROP_PRIVATE_REFERENCE:
            del public[key]

    home = None
    if build_home:
        home_proxy_indices = [
            index
            for index, proxy in enumerate(source["proxies"])
            if isinstance(proxy, Mapping) and proxy.get("name") in home_proxy_names
        ]
        home_document = _build_home_document(
            source,
            scope,
            home_proxy_indices,
            private_group_indices,
            private_group_values,
            extensions,
            private_rule_indices,
        )
        try:
            home = parse_home_overlay(
                dump_round_trip(home_document).encode("utf-8"), MAX_SOURCE_BYTES
            )
        except (HomeSourceError, RoundTripYamlError):
            raise TemplateSyncError("template_candidate_invalid") from None

    return _SplitResult(
        public=public,
        home=home,
        inject_node_groups=tuple(inject_node_groups),
        dynamic_names=dynamic_names,
    )


def _strip_private_group(group, name, all_injected, home_injected, all_inline, home_inline):
    copied = clone_round_trip(group)
    _drop_provider_use(copied)
    members = group.get("proxies")
    if not isinstance(members, list):
        return copied
    if name in all_injected:
        injected = all_inline
    elif name in home_injected:
        injected = home_inline
    else:
        injected = set()
    copied["proxies"] = _select_sequence(
        members,
        [
            index
            for index, member in enumerate(members)
            if not (isinstance(member, str) and member in injected)
        ],
    )
    return copied


def _drop_provider_use(group):
    if "use" in group:
        del group["use"]
    _drop_merged_provider_use(group)


def _group_uses(group):
    """Return a group's direct or YAML-merge-inherited provider uses."""
    uses = group.get("use")
    if uses is not None:
        return uses
    merged = getattr(group, "merge", None)
    if merged:
        for item in merged:
            if isinstance(item, Mapping) and "use" in item:
                return item.get("use")
    return None


def _drop_merged_provider_use(group):
    merged = getattr(group, "merge", None)
    if not merged:
        return
    for item in merged:
        if isinstance(item, Mapping) and "use" in item:
            del item["use"]


def _build_home_document(
    source,
    scope,
    home_proxy_indices,
    private_group_indices,
    private_group_values,
    extensions,
    private_rule_indices,
):
    document = CommentedMap()
    if getattr(source, "ca", None) is not None:
        document.ca.comment = copy.deepcopy(source.ca.comment)
    proxies = _select_sequence(source["proxies"], home_proxy_indices)
    groups = _sequence_with_values(
        source["proxy-groups"], private_group_indices, private_group_values
    )
    rules = _select_sequence(source["rules"], private_rule_indices)
    document["proxies"] = proxies
    document["proxy-groups"] = groups
    document["extend-proxy-groups"] = extensions
    document["inject-node-groups"] = _copy_scope_sequence(
        scope, "inject-node-groups"
    )
    document["inject-home-node-groups"] = _copy_scope_sequence(
        scope, "inject-home-node-groups"
    )
    document["rules"] = rules
    for key in ("proxies", "proxy-groups", "rules"):
        if key in source:
            copy_key_comments(source, key, document, key)
    for key in (
        "extend-proxy-groups",
        "inject-node-groups",
        "inject-home-node-groups",
    ):
        scope_document = getattr(scope, "document", None)
        if isinstance(scope_document, Mapping) and key in scope_document:
            copy_key_comments(scope_document, key, document, key)
    return document


def _copy_scope_sequence(scope, key):
    scope_document = getattr(scope, "document", None)
    if isinstance(scope_document, Mapping) and isinstance(scope_document.get(key), list):
        source = scope_document[key]
        result = CommentedSeq(clone_round_trip(list(source)))
        if getattr(source, "fa", None) is not None and source.fa.flow_style():
            result.fa.set_flow_style()
        for index, comments in getattr(getattr(source, "ca", None), "items", {}).items():
            if _has_visible_comment(comments):
                result.ca.items[index] = copy.deepcopy(comments)
        if _has_visible_comment(getattr(getattr(source, "ca", None), "comment", None)):
            result.ca.comment = copy.deepcopy(source.ca.comment)
        if _has_visible_comment(getattr(getattr(source, "ca", None), "end", None)):
            result.ca.end = copy.deepcopy(source.ca.end)
        return result
    return CommentedSeq(list(getattr(scope, "inject_node_groups" if key == "inject-node-groups" else "inject_home_node_groups")))


def _has_visible_comment(value):
    if isinstance(value, (list, tuple)):
        return any(_has_visible_comment(item) for item in value)
    text = getattr(value, "value", None)
    return isinstance(text, str) and bool(text.strip())


def _select_sequence(source, indices):
    values = [source[index] for index in indices]
    return _sequence_with_values(source, indices, values)


def _sequence_with_values(source, indices, values):
    result = CommentedSeq()
    if getattr(source, "ca", None) is not None and indices and indices[0] == 0:
        result.ca.comment = copy.deepcopy(source.ca.comment)
        result.ca.end = copy.deepcopy(source.ca.end)
    for new_index, (old_index, value) in enumerate(zip(indices, values)):
        result.append(clone_round_trip(value))
        source_comments = getattr(getattr(source, "ca", None), "items", {})
        comments = source_comments.get(old_index)
        if comments is not None:
            result.ca.items[new_index] = copy.deepcopy(comments)
    return result


def _rule_targets_private(rule, private_names):
    if not isinstance(rule, str):
        return False
    parts = [part.strip() for part in rule.strip().split(",")]
    if len(parts) < 2:
        return False
    target = parts[-1]
    if target in _RULE_OPTION_TOKENS and len(parts) >= 3:
        target = parts[-2]
    return target in private_names


def _build_profiles(inject_node_groups):
    profiles = CommentedMap()
    profiles["compat-office"] = CommentedMap({"dns": "compat", "home": True})
    profiles["compat-universal"] = CommentedMap({"dns": "compat", "home": False})
    profiles["balance-office"] = CommentedMap(
        {"dns": "balance-office", "home": True}
    )
    document = CommentedMap()
    document["profiles"] = profiles
    document["inject-node-groups"] = CommentedSeq(list(inject_node_groups))
    return document


def _extract_balance_dns(balance_source, compat_candidate, scope):
    if "dns" not in balance_source or not isinstance(balance_source["dns"], Mapping):
        raise TemplateSyncError("template_candidate_invalid")
    balance_split = _split_source(balance_source, scope)
    if _without_key(plain_data(balance_split.public), "dns") != _without_key(
        plain_data(compat_candidate), "dns"
    ):
        raise TemplateSyncError("balance_profile_mismatch")
    result = CommentedMap()
    if getattr(balance_source, "ca", None) is not None:
        result.ca.comment = copy.deepcopy(balance_source.ca.comment)
    result["dns"] = clone_round_trip(balance_source["dns"])
    copy_key_comments(balance_source, "dns", result, "dns")
    return result


def _without_key(document, key):
    copied = dict(document)
    copied.pop(key, None)
    return copied


def _derive_home_from_pair(office, universal):
    office_proxy_names = set(_source_names(office, "proxies"))
    universal_proxy_names = set(_source_names(universal, "proxies"))
    office_group_names = set(_source_names(office, "proxy-groups"))
    universal_group_names = set(_source_names(universal, "proxy-groups"))
    if not universal_proxy_names.issubset(office_proxy_names):
        raise TemplateSyncError("template_source_invalid")
    if not universal_group_names.issubset(office_group_names):
        raise TemplateSyncError("template_source_invalid")
    home_proxy_names = office_proxy_names - universal_proxy_names
    home_group_names = office_group_names - universal_group_names
    if not home_proxy_names or not home_group_names:
        raise TemplateSyncError("template_source_invalid")

    dynamic_names = office_proxy_names - home_proxy_names
    private_group_entries = [
        group
        for group in office["proxy-groups"]
        if isinstance(group, Mapping) and group.get("name") in home_group_names
    ]
    inject_node = []
    inject_home = []
    for group in private_group_entries:
        members = group.get("proxies")
        if not isinstance(members, list):
            continue
        member_names = {member for member in members if isinstance(member, str)}
        if member_names & dynamic_names:
            inject_node.append(group["name"])
        elif member_names & home_proxy_names:
            inject_home.append(group["name"])

    extensions = {}
    for group in office["proxy-groups"]:
        if not isinstance(group, Mapping) or group.get("name") in home_group_names:
            continue
        members = group.get("proxies")
        if isinstance(members, list):
            removed = [member for member in members if member in home_group_names]
            if removed:
                extensions[group["name"]] = removed

    private_rule_indices, _universal_rule_indices = _pair_rule_indices(
        office.get("rules"), universal.get("rules")
    )
    scope = HomeOverlay(
        proxies=tuple(
            clone_round_trip(proxy)
            for proxy in office["proxies"]
            if isinstance(proxy, Mapping) and proxy.get("name") in home_proxy_names
        ),
        proxy_groups=tuple(clone_round_trip(group) for group in private_group_entries),
        extend_proxy_groups=extensions,
        inject_node_groups=tuple(inject_node),
        inject_home_node_groups=tuple(inject_home),
        rules=tuple(
            office["rules"][index]
            for index in private_rule_indices
            if isinstance(office.get("rules"), list)
        ),
    )
    _validate_derived_universal(office, universal, scope)
    split = _split_source(office, scope)
    if split.home is None:
        raise TemplateSyncError("template_candidate_invalid")
    return split.home


def _pair_rule_indices(office_rules, universal_rules):
    if not isinstance(office_rules, list) or not isinstance(universal_rules, list):
        raise TemplateSyncError("template_source_invalid")
    used = set()
    universal_indices = []
    cursor = 0
    for universal_rule in universal_rules:
        found = None
        for index in range(cursor, len(office_rules)):
            if office_rules[index] == universal_rule:
                found = index
                break
        if found is None:
            raise TemplateSyncError("template_source_invalid")
        universal_indices.append(found)
        used.add(found)
        cursor = found + 1
    private_indices = [index for index in range(len(office_rules)) if index not in used]
    return private_indices, universal_indices


def _validate_derived_universal(office, universal, scope):
    office_proxy_names = set(_source_names(office, "proxies"))
    universal_proxy_names = set(_source_names(universal, "proxies"))
    home_proxy_names = {
        proxy["name"] for proxy in scope.proxies if isinstance(proxy, Mapping)
    }
    home_group_names = {
        group["name"] for group in scope.proxy_groups if isinstance(group, Mapping)
    }
    if office_proxy_names & home_proxy_names != home_proxy_names:
        raise TemplateSyncError("template_source_invalid")
    if universal_proxy_names & home_proxy_names:
        raise TemplateSyncError("template_source_invalid")
    universal_groups = universal.get("proxy-groups")
    if not isinstance(universal_groups, list):
        raise TemplateSyncError("template_source_invalid")
    for group in universal_groups:
        if not isinstance(group, Mapping):
            raise TemplateSyncError("template_source_invalid")
        if group.get("name") in home_group_names:
            raise TemplateSyncError("template_source_invalid")
        members = group.get("proxies")
        if isinstance(members, list) and any(
            isinstance(member, str) and member in (home_proxy_names | home_group_names)
            for member in members
        ):
            raise TemplateSyncError("template_source_invalid")
    universal_rules = universal.get("rules")
    if isinstance(universal_rules, list) and any(
        _rule_targets_private(rule, home_proxy_names | home_group_names)
        for rule in universal_rules
    ):
        raise TemplateSyncError("template_source_invalid")

    office_public = _split_source(office, scope).public
    universal_public = _split_source(
        universal, scope, allow_missing_home=True, build_home=False
    ).public
    if plain_data(office_public) != plain_data(universal_public):
        raise TemplateSyncError("template_source_invalid")


def _validate_profiles_document(document):
    if not isinstance(document, Mapping) or set(document) != {
        "profiles",
        "inject-node-groups",
    }:
        raise TemplateSyncError("template_candidate_invalid")
    profiles = document.get("profiles")
    if not isinstance(profiles, Mapping) or set(profiles) != {
        "compat-office",
        "compat-universal",
        "balance-office",
    }:
        raise TemplateSyncError("template_candidate_invalid")
    expected = {
        "compat-office": ("compat", True),
        "compat-universal": ("compat", False),
        "balance-office": ("balance-office", True),
    }
    for name, (dns, home) in expected.items():
        recipe = profiles.get(name)
        if (
            not isinstance(recipe, Mapping)
            or set(recipe) != {"dns", "home"}
            or recipe.get("dns") != dns
            or type(recipe.get("home")) is not bool
            or recipe.get("home") is not home
        ):
            raise TemplateSyncError("template_candidate_invalid")
    inject = document.get("inject-node-groups")
    if not isinstance(inject, list) or any(
        not isinstance(name, str) or not name.strip() for name in inject
    ):
        raise TemplateSyncError("template_candidate_invalid")
    if len(set(inject)) != len(inject):
        raise TemplateSyncError("template_candidate_invalid")


def _validate_candidates(
    root,
    compat_candidate,
    profiles_candidate,
    balance_candidate,
    home_candidate,
    forbidden_names,
    forbidden_values,
):
    _validate_profiles_document(profiles_candidate)
    if not isinstance(compat_candidate, Mapping):
        raise TemplateSyncError("template_candidate_invalid")
    # A public candidate must never retain source nodes.
    if not isinstance(compat_candidate.get("proxies"), list) or compat_candidate.get(
        "proxies"
    ):
        raise TemplateSyncError("template_candidate_invalid")
    if "proxy-providers" in compat_candidate:
        raise TemplateSyncError("template_candidate_invalid")
    if not isinstance(home_candidate, HomeOverlay):
        raise TemplateSyncError("template_candidate_invalid")
    profile_injections = profiles_candidate["inject-node-groups"]
    public_group_names = {
        group.get("name")
        for group in compat_candidate.get("proxy-groups", [])
        if isinstance(group, Mapping)
    }
    home_group_names = {
        group.get("name")
        for group in home_candidate.proxy_groups
        if isinstance(group, Mapping)
    }
    if any(name not in public_group_names for name in profile_injections):
        raise TemplateSyncError("template_candidate_invalid")
    if set(profile_injections) & home_group_names:
        raise TemplateSyncError("template_candidate_invalid")

    scanner = _load_scanner(root)
    texts = [(PUBLIC_TEMPLATE_FILES[0], _dump_candidate(compat_candidate))]
    if balance_candidate is not None:
        if set(balance_candidate) != {"dns"} or not isinstance(
            balance_candidate.get("dns"), Mapping
        ):
            raise TemplateSyncError("template_candidate_invalid")
        texts.append((PUBLIC_TEMPLATE_FILES[1], _dump_candidate(balance_candidate)))
    texts.append((PUBLIC_TEMPLATE_FILES[2], _dump_candidate(profiles_candidate)))
    _scan_for_secrets(texts, forbidden_names, forbidden_values, scanner)

    try:
        compat_owner = _compose_for_validation(
            compat_candidate, profiles_candidate, home_candidate, use_home=True, owner=True
        )
        compat_universal_owner = _compose_for_validation(
            compat_candidate, profiles_candidate, home_candidate, use_home=False, owner=True
        )
        compat_member = _compose_for_validation(
            compat_candidate, profiles_candidate, home_candidate, use_home=False, owner=False
        )
        _validate_rendered(compat_owner, _PROBE_PROVIDER_URL)
        _validate_rendered(compat_universal_owner, _PROBE_PROVIDER_URL)
        _validate_rendered(compat_member, None)
        if balance_candidate is not None:
            balance_full = clone_round_trip(compat_candidate)
            balance_full["dns"] = clone_round_trip(balance_candidate["dns"])
            balance_owner = _compose_for_validation(
                balance_full,
                profiles_candidate,
                home_candidate,
                use_home=True,
                owner=True,
            )
            _validate_rendered(balance_owner, _PROBE_PROVIDER_URL)
    except Exception:
        raise TemplateSyncError("template_candidate_invalid") from None


def _compose_for_validation(base, profiles, home, *, use_home, owner):
    document = clone_round_trip(base)
    document["proxies"] = CommentedSeq([clone_round_trip(_PROBE_PROXY)])
    if use_home:
        document["proxies"].extend(
            clone_round_trip(proxy) for proxy in home.proxies
        )
        groups = document.get("proxy-groups")
        if not isinstance(groups, list):
            raise ValueError
        groups.extend(clone_round_trip(group) for group in home.proxy_groups)
        by_name = {
            group.get("name"): group for group in groups if isinstance(group, Mapping)
        }
        for group_name, members in home.extend_proxy_groups.items():
            target = by_name.get(group_name)
            if not isinstance(target, Mapping) or not isinstance(target.get("proxies"), list):
                raise ValueError
            target["proxies"].extend(clone_round_trip(member) for member in members)
        public_rules = document.get("rules")
        if not isinstance(public_rules, list):
            raise ValueError
        document["rules"] = CommentedSeq(
            list(clone_round_trip(rule) for rule in home.rules)
            + list(clone_round_trip(rule) for rule in public_rules)
        )
    injections = profiles["inject-node-groups"]
    for name in injections:
        target = next(
            (
                group
                for group in document.get("proxy-groups", [])
                if isinstance(group, Mapping) and group.get("name") == name
            ),
            None,
        )
        if not isinstance(target, Mapping):
            raise ValueError
        members = target.get("proxies")
        if isinstance(members, list) and _PROBE_NAME not in members:
            members.append(_PROBE_NAME)
    if use_home:
        all_groups = set(home.inject_node_groups)
        home_groups = set(home.inject_home_node_groups)
        home_names = [proxy.get("name") for proxy in home.proxies]
        for group in document.get("proxy-groups", []):
            name = group.get("name") if isinstance(group, Mapping) else None
            members = group.get("proxies") if isinstance(group, Mapping) else None
            if not isinstance(members, list):
                continue
            if name in all_groups:
                for member in [_PROBE_NAME] + home_names:
                    if member not in members:
                        members.append(member)
            elif name in home_groups:
                for member in home_names:
                    if member not in members:
                        members.append(member)
    if owner:
        document["proxy-providers"] = CommentedMap({_PROVIDER_NAME: _PROBE_PROVIDER})
    elif "proxy-providers" in document:
        del document["proxy-providers"]
    return _dump_candidate(document)


def _validate_rendered(text, provider_url):
    validate_clash(text, (), allowed_provider_url=provider_url)


def _load_scanner(root):
    path = Path(root) / "scripts" / "scan_tracked_secrets.py"
    try:
        spec = importlib.util.spec_from_file_location(
            "clash_sub._template_sync_scanner", path
        )
        if spec is None or spec.loader is None:
            raise ImportError
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, "find_content_findings", None)):
            raise ImportError
        return module
    except Exception:
        raise TemplateSyncError("template_candidate_invalid") from None


def _forbidden_values(sources, scope):
    sources = tuple(sources)
    names = set()
    values = set()
    source_names = set()
    for source in sources:
        source_names.update(_source_names(source, "proxies"))
        source_names.update(_source_names(source, "proxy-groups"))
    home_names = {
        item.get("name")
        for item in scope.proxies
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    home_group_names = {
        item.get("name")
        for item in scope.proxy_groups
        if isinstance(item, Mapping) and isinstance(item.get("name"), str)
    }
    names.update(home_names)
    names.update(home_group_names)
    for proxy in scope.proxies:
        _collect_proxy_sensitive_values(proxy, values)
    for group in scope.proxy_groups:
        if isinstance(group, Mapping):
            for member in group.get("proxies", []) or []:
                if (
                    isinstance(member, str)
                    and member not in {"DIRECT", "REJECT", "GLOBAL"}
                    and member not in source_names
                ):
                    names.add(member)
    values.update(scope.rules)

    for source in sources:
        source_proxy_names = set(_source_names(source, "proxies"))
        names.update(source_proxy_names)
        for proxy in source.get("proxies", []) or []:
            _collect_proxy_sensitive_values(proxy, values)
        for group in source.get("proxy-groups", []) or []:
            if not isinstance(group, Mapping):
                continue
            name = group.get("name")
            if name in home_group_names:
                names.add(name)
                for member in group.get("proxies", []) or []:
                    if (
                        isinstance(member, str)
                        and member not in {"DIRECT", "REJECT", "GLOBAL"}
                        and member not in source_names
                    ):
                        names.add(member)
        providers = source.get("proxy-providers")
        if isinstance(providers, Mapping):
            names.add(_PROVIDER_NAME)
            provider = providers.get(_PROVIDER_NAME)
            _collect_provider_sensitive_values(provider, values)
        rules = source.get("rules")
        if isinstance(rules, list):
            for rule in rules:
                if _rule_targets_private(rule, home_names | home_group_names):
                    if isinstance(rule, str):
                        values.add(rule)
        _collect_credential_like_values(source, values)
    return names, values


def _collect_proxy_sensitive_values(proxy, values):
    if not isinstance(proxy, Mapping):
        return
    for key, value in proxy.items():
        if key in _PROXY_STRUCTURAL_KEYS:
            continue
        _collect_all_strings(value, values)


def _collect_provider_sensitive_values(provider, values):
    if not isinstance(provider, Mapping):
        return
    for key in ("url", "username", "password", "token", "secret"):
        value = provider.get(key)
        if isinstance(value, str) and value:
            values.add(value)


def _collect_all_strings(node, values):
    if isinstance(node, Mapping):
        for value in node.values():
            _collect_all_strings(value, values)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _collect_all_strings(value, values)
    elif isinstance(node, str) and node:
        values.add(node)


def _collect_credential_like_values(node, values, key=""):
    if isinstance(node, Mapping):
        for child_key, child in node.items():
            _collect_credential_like_values(child, values, str(child_key))
    elif isinstance(node, (list, tuple)):
        for child in node:
            _collect_credential_like_values(child, values, key)
    elif isinstance(node, str) and node:
        if (
            str(key).lower() in _PRIVATE_FIELD_KEYS
            or _looks_credential_like(key, node)
            or _url_has_credentials(node)
        ):
            values.add(node)


def _scan_for_secrets(public_candidates, forbidden_names, forbidden_values, scanner):
    for relative, text in public_candidates:
        try:
            findings = scanner.find_content_findings(text, relative)
        except Exception:
            raise TemplateSyncError("template_candidate_invalid") from None
        if findings:
            raise TemplateSyncError("template_secret_leak")
        for value in forbidden_values:
            if value and len(value) >= 16 and value in text:
                raise TemplateSyncError("template_secret_leak")
        try:
            document = load_round_trip(text)
        except RoundTripYamlError:
            raise TemplateSyncError("template_candidate_invalid") from None
        scalars = set()
        _collect_string_scalars(document, scalars)
        if scalars & forbidden_values:
            raise TemplateSyncError("template_secret_leak")
        for scalar in scalars:
            if any(name and name in scalar for name in forbidden_names):
                raise TemplateSyncError("template_secret_leak")
        for group in document.get("proxy-groups", []) or []:
            if isinstance(group, Mapping):
                for member in group.get("proxies", []) or []:
                    if member in forbidden_names:
                        raise TemplateSyncError("template_secret_leak")


def _collect_string_scalars(node, out):
    if isinstance(node, Mapping):
        for key, value in node.items():
            if isinstance(key, str):
                out.add(key)
            _collect_string_scalars(value, out)
    elif isinstance(node, (list, tuple)):
        for item in node:
            _collect_string_scalars(item, out)
    elif isinstance(node, str):
        out.add(node)


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
    if not isinstance(value, str) or len(value) < 16:
        return False
    if re.fullmatch(r"[0-9a-fA-F]{32,}", value):
        return True
    if re.fullmatch(
        r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
        value,
    ):
        return True
    return False


def _url_has_credentials(value):
    if not isinstance(value, str) or "://" not in value:
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


def _serialize_candidates(candidates):
    payloads = {}
    for relative, candidate in candidates.items():
        if isinstance(candidate, bytes):
            payloads[relative] = candidate
            continue
        payloads[relative] = _dump_candidate(candidate).encode("utf-8")
    return payloads


def _dump_candidate(document):
    try:
        return dump_round_trip(clone_round_trip(document))
    except RoundTripYamlError:
        raise TemplateSyncError("template_candidate_invalid") from None


def _snapshot_one(root, relative):
    target = Path(root) / relative
    try:
        details = target.lstat()
    except FileNotFoundError:
        return None, None
    except OSError:
        raise TemplateSyncError("template_write_failed") from None
    if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode):
        raise TemplateSyncError("template_write_failed")
    try:
        return target.read_bytes(), stat.S_IMODE(details.st_mode)
    except OSError:
        raise TemplateSyncError("template_write_failed") from None


def _snapshot_targets(root, relatives):
    return {relative: _snapshot_one(root, relative) for relative in relatives}


def _build_report(
    root,
    payloads,
    previous,
    compat_selected,
    balance_selected,
    compat_candidate,
    home_candidate,
    old_home,
):
    changed = tuple(
        relative
        for relative in TEMPLATE_OUTPUT_PATHS
        if relative in payloads
        and (
            previous[relative][0] != payloads[relative]
            or previous[relative][1] != OUTPUT_MODES[relative]
        )
    )
    lines = []
    if compat_selected:
        compat_changed = PUBLIC_TEMPLATE_FILES[0] in changed
        lines.append("Compat 基础：%s" % ("已更新" if compat_changed else "无变化"))
        old_compat = _previous_document(root, PUBLIC_TEMPLATE_FILES[0], previous)
        added, deleted, modified = _sequence_diff_counts(
            old_compat.get("rules", []) if isinstance(old_compat, Mapping) else [],
            compat_candidate.get("rules", []) if isinstance(compat_candidate, Mapping) else [],
        )
        lines.append("  - rules：新增 %d，删除 %d，修改 %d" % (added, deleted, modified))
        home_changed = HOME_SCOPE_PATH in changed
        lines.append("家庭覆盖层：%s" % ("已更新" if home_changed else "无变化"))
        lines.append(
            "  - 节点数量：%d → %d"
            % (len(old_home.proxies), len(home_candidate.proxies))
        )
        lines.append(
            "  - 策略组数量：%d → %d"
            % (len(old_home.proxy_groups), len(home_candidate.proxy_groups))
        )
        lines.append(
            "  - 规则数量：%d → %d" % (len(old_home.rules), len(home_candidate.rules))
        )
    if balance_selected:
        balance_changed = PUBLIC_TEMPLATE_FILES[1] in changed
        lines.append("Balance DNS：%s" % ("已更新" if balance_changed else "无变化"))
    for relative in changed:
        if relative == HOME_SCOPE_PATH:
            continue
        lines.append("写入：%s" % relative)
    for relative in payloads:
        if relative == HOME_SCOPE_PATH:
            continue
        if relative not in changed:
            lines.append("保持：%s" % relative)
    return TemplateSyncReport(changed=changed, lines=tuple(lines))


def _previous_document(root, relative, previous):
    payload, _mode = previous.get(relative, (None, None))
    if payload is None:
        return None
    try:
        return load_round_trip(payload)
    except RoundTripYamlError:
        return None


def _sequence_diff_counts(old, new):
    old_list = list(old) if isinstance(old, (list, tuple)) else []
    new_list = list(new) if isinstance(new, (list, tuple)) else []
    common = sum((Counter(old_list) & Counter(new_list)).values())
    deleted = len(old_list) - common
    added = len(new_list) - common
    modified = min(added, deleted)
    return added - modified, deleted - modified, modified


def _atomic_replace_outputs(root, payloads):
    selected = tuple(relative for relative in TEMPLATE_OUTPUT_PATHS if relative in payloads)
    previous = []
    attempted = []
    try:
        for relative in selected:
            payload, mode = _snapshot_one(root, relative)
            previous.append((relative, payload, mode))
        for relative, old_payload, old_mode in previous:
            if (
                old_payload == payloads[relative]
                and old_mode == OUTPUT_MODES[relative]
            ):
                continue
            target = Path(root) / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            attempted.append(relative)
            _write_file_atomically(
                target, payloads[relative], OUTPUT_MODES[relative]
            )
    except (OSError, ValueError) as write_error:
        try:
            _restore_files(root, previous, attempted)
        except (OSError, ValueError):
            raise TemplateSyncError("template_rollback_failed") from write_error
        raise TemplateSyncError("template_write_failed") from write_error


def _write_file_atomically(target, payload, mode):
    descriptor, temporary = tempfile.mkstemp(
        prefix=".%s." % target.name, dir=str(target.parent)
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        _os_replace(temporary, target)
        temporary = None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _restore_files(root, previous, attempted):
    attempted_set = set(attempted)
    restore_failed = False
    for relative, payload, mode in previous:
        if relative not in attempted_set:
            continue
        target = Path(root) / relative
        try:
            if payload is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                _write_file_atomically(
                    target,
                    payload,
                    mode if mode is not None else OUTPUT_MODES[relative],
                )
        except (OSError, ValueError):
            restore_failed = True
    if restore_failed:
        raise OSError("template rollback failed")
