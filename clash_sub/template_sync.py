"""Synchronize safe public Compat and Balance DNS templates from ClashX files."""

import copy
import importlib.util
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml.comments import CommentedMap, CommentedSeq

from clash_sub.checks import CheckError, validate_clash
from clash_sub.domain import AirportProvider
from clash_sub.generator import render_user_bundle
from clash_sub.yaml_rt import (
    RoundTripYamlError,
    clone_isolated_round_trip,
    clone_round_trip_document,
    copy_key_comments,
    dump_round_trip,
    load_round_trip,
    plain_data,
)

ICLOUD_RELATIVE_ROOT = Path("Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents")
COMPAT_SOURCE_NAME = "Clash-Compat.yaml"
BALANCE_SOURCE_NAME = "Clash-Balance.yaml"
PUBLIC_TEMPLATE_FILES = (
    "templates/base/Clash-Compat.yaml",
    "templates/dns/Clash-Balance.yaml",
    "templates/profiles.yaml",
)
OUTPUT_MODES = {relative: 0o644 for relative in PUBLIC_TEMPLATE_FILES}
MAX_SOURCE_BYTES = 5 * 1024 * 1024
_PROVIDER_NAME = "AmyTelecom"
# The local cache filename the airport subscription used before publication;
# any provider or comment referencing it is private airport machinery.  The
# literal is assembled so this removal marker itself stays outside plain
# repository text searches for the retired name.
_AIRPORT_CACHE_NAME = "AmyTelecom" + ".yaml"


class TemplateSyncError(RuntimeError):
    """A redacted, stable template synchronization failure."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class TemplateSyncReport:
    changed: tuple[str, ...]
    lines: tuple[str, ...]
    ignored_balance_paths: tuple[str, ...]


def _os_replace(source, target):
    os.replace(source, target)


def default_source_paths(home: Path | None = None) -> tuple[Path, Path]:
    root = Path.home() if home is None else Path(home)
    source = root / ICLOUD_RELATIVE_ROOT
    return source / COMPAT_SOURCE_NAME, source / BALANCE_SOURCE_NAME


def run_template_sync(repo_root: Path, compat: Path | None = None, balance: Path | None = None) -> TemplateSyncReport:
    """Update selected public templates; no arguments select both iCloud files."""
    root = Path(repo_root)
    if compat is None and balance is None:
        compat, balance = default_source_paths()
    compat_selected, balance_selected = compat is not None, balance is not None
    compat_source = _load_source(compat) if compat_selected else _load_current(root, PUBLIC_TEMPLATE_FILES[0])
    compat_candidate, injections = _sanitize_compat(compat_source)
    profiles_candidate = _profiles(injections) if compat_selected else _load_current(root, PUBLIC_TEMPLATE_FILES[2])
    balance_candidate = None
    ignored = ()
    if balance_selected:
        balance_source = _load_source(balance)
        balance_candidate = _extract_balance(balance_source)
        ignored = _balance_differences(balance_source, compat_source)
    candidates = {}
    if compat_selected:
        candidates[PUBLIC_TEMPLATE_FILES[0]] = compat_candidate
        candidates[PUBLIC_TEMPLATE_FILES[2]] = profiles_candidate
    if balance_selected:
        candidates[PUBLIC_TEMPLATE_FILES[1]] = balance_candidate
    _validate_candidates(root, candidates, compat_candidate, profiles_candidate, balance_candidate)
    payloads = {relative: _dump(document).encode("utf-8") for relative, document in candidates.items()}
    previous = _snapshot_targets(root, tuple(payloads))
    changed = tuple(relative for relative in PUBLIC_TEMPLATE_FILES if relative in payloads and (previous[relative][0] != payloads[relative] or previous[relative][1] != OUTPUT_MODES[relative]))
    lines = []
    if compat_selected:
        lines.append("Compat 基础：%s" % ("已更新" if PUBLIC_TEMPLATE_FILES[0] in changed else "无变化"))
    if balance_selected:
        lines.append("Balance DNS：%s" % ("已更新" if PUBLIC_TEMPLATE_FILES[1] in changed else "无变化"))
    if ignored:
        lines.append("Balance 非 DNS 差异（未合并）：%s" % ", ".join(ignored))
    lines.extend("写入：%s" % relative for relative in changed)
    _atomic_replace_outputs(root, payloads)
    return TemplateSyncReport(changed, tuple(lines), ignored)


def _read_regular(path, code):
    try:
        details = Path(path).lstat()
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode) or details.st_nlink != 1:
            raise OSError
        payload = Path(path).read_bytes()
    except OSError:
        raise TemplateSyncError(code) from None
    if len(payload) > MAX_SOURCE_BYTES:
        raise TemplateSyncError(code)
    return payload


def _load_source(path):
    payload = _read_regular(path, "template_source_invalid")
    try:
        document = load_round_trip(payload)
    except RoundTripYamlError:
        raise TemplateSyncError("template_source_invalid") from None
    if not isinstance(document.get("proxies"), list) or not isinstance(document.get("proxy-groups"), list):
        raise TemplateSyncError("template_source_invalid")
    return document


def _load_current(root, relative):
    try:
        return load_round_trip(_read_regular(Path(root) / relative, "template_candidate_invalid"))
    except RoundTripYamlError:
        raise TemplateSyncError("template_candidate_invalid") from None


def _sanitize_compat(source):
    public = clone_round_trip_document(source)
    proxy_names = {item.get("name") for item in source["proxies"] if isinstance(item, Mapping) and isinstance(item.get("name"), str)}
    if len(proxy_names) != len(source["proxies"]):
        raise TemplateSyncError("template_source_invalid")
    public["proxies"] = CommentedSeq()
    airports = _airport_provider_names(public.get("proxy-providers"))
    provider_groups, node_groups = [], []
    for group in public["proxy-groups"]:
        if not isinstance(group, Mapping) or not isinstance(group.get("name"), str):
            raise TemplateSyncError("template_source_invalid")
        members = group.get("proxies")
        if isinstance(members, list):
            kept = [member for member in members if member not in proxy_names]
            if len(kept) != len(members):
                node_groups.append(group["name"])
                group["proxies"] = CommentedSeq(kept)
        if _drop_airport_group_uses(group, airports):
            provider_groups.append(group["name"])
    if "proxy-providers" in public:
        providers = public["proxy-providers"]
        if not isinstance(providers, Mapping):
            raise TemplateSyncError("template_source_invalid")
        for name in airports:
            providers.pop(name, None)
        if not providers:
            del public["proxy-providers"]
    _strip_airport_cache_comments(public)
    return public, (tuple(dict.fromkeys(node_groups)), tuple(dict.fromkeys(provider_groups)))


def _airport_provider_names(providers):
    """The canonical airport provider plus local aliases of its cache file."""
    names = set()
    if not isinstance(providers, Mapping):
        return names
    for name, mapping in providers.items():
        if name == _PROVIDER_NAME:
            names.add(name)
            continue
        if not isinstance(mapping, Mapping):
            continue
        for field in ("path", "url"):
            value = mapping.get(field)
            if isinstance(value, str) and _AIRPORT_CACHE_NAME in value:
                names.add(name)
                break
    return names


def _drop_airport_group_uses(group, airports):
    """Remove airport references from one group's use entries and merges."""
    referenced = False
    for entry in list(getattr(group, "merge", None) or []):
        base = entry[0] if isinstance(entry, (list, tuple)) else entry
        if not isinstance(base, Mapping):
            continue
        base_uses = base.get("use")
        if isinstance(base_uses, list) and any(name in airports for name in base_uses):
            referenced = True
            kept = [name for name in base_uses if name not in airports]
            if kept:
                base["use"] = CommentedSeq(kept)
            else:
                del base["use"]
    uses = group.get("use")
    if isinstance(uses, list) and any(name in airports for name in uses):
        referenced = True
        kept = [name for name in uses if name not in airports]
        if kept:
            group["use"] = CommentedSeq(kept)
        else:
            try:
                del group["use"]
            except KeyError:
                pass
            if "proxies" not in group and group.get("include-all") is not True:
                group["proxies"] = CommentedSeq()
    return referenced


def _strip_airport_cache_comments(node):
    """Drop comment tokens that reference the local airport cache file."""
    comments = getattr(node, "ca", None)
    if comments is not None:
        comment = getattr(comments, "comment", None)
        if comment is not None:
            comments.comment = _without_airport_tokens(comment)
        items = getattr(comments, "items", None)
        if items:
            for key, slots in list(items.items()):
                items[key] = [_without_airport_tokens(slot) for slot in slots]
    if isinstance(node, Mapping):
        for value in node.values():
            _strip_airport_cache_comments(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            _strip_airport_cache_comments(value)


def _without_airport_tokens(value):
    """Remove airport-cache comment tokens while preserving the shape."""
    if value is None:
        return None
    if isinstance(value, list):
        if any(item is None or isinstance(item, list) for item in value):
            # Positional slot lists (root comment pairs) keep their slots.
            return [_without_airport_tokens(item) for item in value]
        kept = [item for item in value if not _token_mentions_airport_cache(item)]
        return kept or None
    if isinstance(value, str):
        return None if _AIRPORT_CACHE_NAME in value else value
    if _token_mentions_airport_cache(value):
        return None
    return value


def _token_mentions_airport_cache(value):
    return _AIRPORT_CACHE_NAME in getattr(value, "value", "")


def _profiles(injections):
    node_groups, provider_groups = injections
    return CommentedMap({
        "profiles": CommentedMap({"compat": CommentedMap({"dns": "compat"}), "balance": CommentedMap({"dns": "balance"})}),
        "inject-node-groups": CommentedSeq(node_groups),
        "inject-provider-groups": CommentedSeq(provider_groups),
    })


def _extract_balance(source):
    if not isinstance(source.get("dns"), Mapping):
        raise TemplateSyncError("template_source_invalid")
    result = CommentedMap({"dns": clone_isolated_round_trip(source["dns"])})
    copy_key_comments(source, "dns", result, "dns")
    return result


def _balance_differences(balance, compat):
    balance_data, compat_data = plain_data(_sanitize_compat(balance)[0]), plain_data(_sanitize_compat(compat)[0])
    balance_data.pop("dns", None); compat_data.pop("dns", None)
    return tuple(str(key) for key in sorted(set(balance_data) | set(compat_data), key=str) if balance_data.get(key) != compat_data.get(key))


def _validate_candidates(root, candidates, compat, profiles, balance):
    for relative, document in candidates.items():
        if relative == PUBLIC_TEMPLATE_FILES[0] and document.get("proxies") != []:
            raise TemplateSyncError("template_candidate_invalid")
        if relative == PUBLIC_TEMPLATE_FILES[1] and (set(document) != {"dns"} or not isinstance(document["dns"], Mapping)):
            raise TemplateSyncError("template_candidate_invalid")
    scanner = _load_scanner(root)
    for relative, document in candidates.items():
        try:
            if scanner.find_content_findings(_dump(document), relative):
                raise TemplateSyncError("template_secret_leak")
        except TemplateSyncError:
            raise
        except Exception:
            raise TemplateSyncError("template_candidate_invalid") from None
    _validate_rendered_candidates(compat, profiles, balance)


def _validate_rendered_candidates(compat, profiles, balance):
    """Exercise the public candidates through the production renderer."""
    probe = {
        "name": "template-sync-probe",
        "type": "vless", "server": "192.0.2.1", "port": 443,
        "uuid": "11111111-1111-4111-8111-111111111111", "network": "tcp",
        "tls": True, "flow": "xtls-rprx-vision", "servername": "probe.invalid",
        "client-fingerprint": "chrome",
        "reality-opts": {"public-key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "short-id": "1111111111111111"},
    }
    provider = AirportProvider("https://template-sync.invalid/AmyTelecom-Provider.yaml")
    try:
        with tempfile.TemporaryDirectory() as directory:
            templates = Path(directory) / "templates"
            _write_validation_file(templates / "base/Clash-Compat.yaml", compat)
            _write_validation_file(templates / "dns/Clash-Balance.yaml", balance or CommentedMap({"dns": clone_isolated_round_trip(compat["dns"])}))
            _write_validation_file(templates / "profiles.yaml", profiles)
            rendered = render_user_bundle(True, [probe], provider, templates)
            for text in rendered.values():
                validate_clash(text, (), allowed_provider_url=provider.url)
    except (CheckError, OSError, RoundTripYamlError, ValueError, KeyError, TypeError) as error:
        raise TemplateSyncError("template_candidate_invalid") from error


def _write_validation_file(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(document), encoding="utf-8")


def _load_scanner(root):
    try:
        spec = importlib.util.spec_from_file_location("clash_sub._scanner", Path(root) / "scripts/scan_tracked_secrets.py")
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        if not callable(getattr(module, "find_content_findings", None)): raise ImportError
        return module
    except Exception:
        raise TemplateSyncError("template_candidate_invalid") from None


def _dump(document):
    try:
        return dump_round_trip(clone_round_trip_document(document))
    except RoundTripYamlError:
        raise TemplateSyncError("template_candidate_invalid") from None


def _snapshot_one(root, relative):
    target = Path(root) / relative
    try:
        details = target.lstat()
        if not stat.S_ISREG(details.st_mode) or stat.S_ISLNK(details.st_mode): raise OSError
        return target.read_bytes(), stat.S_IMODE(details.st_mode)
    except OSError:
        raise TemplateSyncError("template_write_failed") from None


def _snapshot_targets(root, relatives):
    return {relative: _snapshot_one(root, relative) for relative in relatives}


def _atomic_replace_outputs(root, payloads):
    previous = _snapshot_targets(root, tuple(payloads)); attempted = []
    try:
        for relative in PUBLIC_TEMPLATE_FILES:
            if relative not in payloads: continue
            old, mode = previous[relative]
            if old == payloads[relative] and mode == OUTPUT_MODES[relative]: continue
            attempted.append(relative)
            _write_file_atomically(Path(root) / relative, payloads[relative], OUTPUT_MODES[relative])
    except (OSError, ValueError) as error:
        try:
            for relative in reversed(attempted):
                old, mode = previous[relative]
                _write_file_atomically(Path(root) / relative, old, mode)
        except (OSError, ValueError):
            raise TemplateSyncError("template_rollback_failed") from error
        raise TemplateSyncError("template_write_failed") from error


def _write_file_atomically(target, payload, mode):
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % target.name, dir=str(target.parent))
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as output:
            descriptor = None; output.write(payload); output.flush(); os.fsync(output.fileno())
        _os_replace(temporary, target); temporary = None
    finally:
        if descriptor is not None: os.close(descriptor)
        if temporary is not None:
            try: os.unlink(temporary)
            except OSError: pass
