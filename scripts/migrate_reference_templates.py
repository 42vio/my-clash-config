from __future__ import annotations

import argparse
import copy
import os
import stat
import subprocess
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Iterable, Mapping

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clash_sub.rendering import dump_root_yaml, variant_root_marker
from clash_sub.reference_rules import (
    is_approved_balanced_win_unresolved_proxy_path,
    safe_path,
)


REFERENCE_FILENAMES = {
    "balanced": "My-Clash_Balanced.yaml",
    "balanced-win": "My-Clash_Balanced_Win.yaml",
    "privacy": "My-Clash_Privacy.yaml",
}
BUILTIN_PROXY_TARGETS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL", "COMPATIBLE"}
JINJA_TOKENS = ("{{", "}}", "{%", "%}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Derive the shared Clash template and variants from ignored references.")
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--template-dir", type=Path, required=True)
    parser.add_argument("--private-proxy-dir", type=Path, required=True)
    return parser.parse_args()


def iter_scalars(value: object, path: tuple[object, ...] = ()) -> Iterable[tuple[tuple[object, ...], object]]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield from iter_scalars(child, path + (key,))
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            yield from iter_scalars(child, path + (index,))
        return
    yield path, value


def require_mapping_root(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("reference root must be a mapping")
    return copy.deepcopy(document)


def primary_checkout_root_from_git_common_dir(common_dir: Path) -> Path:
    resolved = common_dir.resolve()
    if resolved.name != ".git":
        raise ValueError("git common-dir must resolve to a .git directory")
    return resolved.parent


def expected_private_proxy_dir(repo_root: Path = ROOT) -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    common_dir = Path(result.stdout.strip())
    return primary_checkout_root_from_git_common_dir(common_dir) / "private" / "sources" / "owner"


def require_private_proxy_dir(
    private_proxy_dir: Path,
    *,
    expected_dir: Path | None = None,
    repo_root: Path = ROOT,
) -> Path:
    resolved = private_proxy_dir.resolve()
    expected = (expected_dir if expected_dir is not None else expected_private_proxy_dir(repo_root)).resolve()
    if resolved != expected:
        raise ValueError("private proxy snapshots must resolve to private/sources/owner in the primary checkout")
    return expected


def atomic_write_text(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fchmod(temporary.fileno(), mode)
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
        path.chmod(mode)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def require_no_jinja_scalars(document: Mapping[str, object]) -> None:
    for _path, scalar in iter_scalars(document):
        if isinstance(scalar, str) and any(token in scalar for token in JINJA_TOKENS):
            raise ValueError("reference contains a Jinja-like scalar")


def duplicate_names(items: object) -> set[str]:
    if not isinstance(items, list):
        return set()
    counts: dict[str, int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str):
            counts[name] = counts.get(name, 0) + 1
    return {name for name, count in counts.items() if count > 1}


def provider_names(document: Mapping[str, object]) -> set[str]:
    providers = document.get("proxy-providers", {})
    if not isinstance(providers, dict):
        return set()
    return {name for name in providers if isinstance(name, str)}


def group_names(document: Mapping[str, object]) -> set[str]:
    return {
        item.get("name").strip()
        for item in document.get("proxy-groups", [])
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }


def inline_proxy_names(document: Mapping[str, object]) -> set[str]:
    proxies = document.get("proxies")
    if not isinstance(proxies, list):
        raise ValueError("reference proxies must be a list")
    names = []
    for proxy in proxies:
        if not isinstance(proxy, dict):
            raise ValueError("reference proxies must contain mappings")
        name = proxy.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("reference proxies must have unique names")
        names.append(name)
    if len(set(names)) != len(names):
        raise ValueError("reference proxies must have unique names")
    return set(names)


def private_proxy_snapshot(document: Mapping[str, object]) -> dict[str, object]:
    proxies = document.get("proxies")
    if not isinstance(proxies, list):
        raise ValueError("reference proxies must be a list")
    return {"proxies": copy.deepcopy(proxies)}


def collect_provider_source_urls(document: Mapping[str, object]) -> set[str]:
    urls = set()
    providers = document.get("proxy-providers")
    if not isinstance(providers, dict):
        return urls
    for provider in providers.values():
        if not isinstance(provider, dict):
            raise ValueError("proxy-providers entries must be mappings")
        url = provider.get("url")
        if isinstance(url, str):
            urls.add(url)
    return urls


def validate_reference_document(document: Mapping[str, object], *, variant: str | None = None) -> list[str]:
    duplicates = duplicate_names(document.get("proxy-groups"))
    if duplicates:
        raise ValueError("reference contains duplicate proxy-group names")
    duplicates = duplicate_names(document.get("proxies"))
    if duplicates:
        raise ValueError("reference contains duplicate proxy names")
    providers = document.get("proxy-providers")
    if providers is not None and not isinstance(providers, dict):
        raise ValueError("reference proxy-providers must be a mapping")
    if providers:
        duplicate_provider_names = [name for name in providers if not isinstance(name, str)]
        if duplicate_provider_names:
            raise ValueError("reference contains invalid provider names")
    proxy_names = {name.strip() for name in inline_proxy_names(document)}
    group_names_set = group_names(document)
    providers_names = provider_names(document)
    unresolved_proxy_paths: list[str] = []
    allow_unresolved_proxy_targets = variant == "balanced-win"
    for group_index, group in enumerate(document.get("proxy-groups", [])):
        if not isinstance(group, dict):
            raise ValueError("proxy-groups entries must be mappings")
        group_proxies = group.get("proxies", [])
        if group_proxies is not None and not isinstance(group_proxies, list):
            raise ValueError("proxy-groups proxies must be lists")
        for proxy_index, target in enumerate(group_proxies or []):
            if not isinstance(target, str):
                raise ValueError("proxy-groups proxies must be string lists")
            normalized_target = target.strip()
            if (
                normalized_target in BUILTIN_PROXY_TARGETS
                or normalized_target in proxy_names
                or normalized_target in group_names_set
            ):
                continue
            if normalized_target not in providers_names:
                path_tuple = ("proxy-groups", group_index, "proxies", proxy_index)
                path = safe_path(path_tuple)
                if allow_unresolved_proxy_targets:
                    if is_approved_balanced_win_unresolved_proxy_path(path_tuple):
                        unresolved_proxy_paths.append(path)
                        continue
                raise ValueError(
                    "reference contains an unresolved proxy target at %s"
                    % path
                )
        use_targets = group.get("use", [])
        if use_targets is not None and not isinstance(use_targets, list):
            raise ValueError("proxy-groups use must be lists")
        for use_index, target in enumerate(use_targets or []):
            if target not in providers_names:
                raise ValueError(
                    "reference contains an unresolved provider target at %s"
                    % safe_path(("proxy-groups", group_index, "use", use_index))
                )
    return unresolved_proxy_paths


def strip_private_provider_values(
    document: Mapping[str, object],
    inline_names: set[str],
    provider_names_set: set[str],
    *,
    allowed_unresolved_proxy_paths: list[str] | None = None,
) -> tuple[dict[str, object], list[str], list[str]]:
    candidate = copy.deepcopy(dict(document))
    candidate.pop("proxies", None)
    candidate.pop("proxy-providers", None)
    removed_paths: list[str] = []
    injection_groups: list[str] = []
    remaining_unresolved_paths = set(allowed_unresolved_proxy_paths or [])
    group_names_set = group_names(document)
    groups = candidate.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("proxy-groups must remain a list")
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            raise ValueError("proxy-groups entries must be mappings")
        group_name = group.get("name")
        if not isinstance(group_name, str) or not group_name:
            raise ValueError("proxy-groups entries must have names")
        removed_inline_proxies = False
        proxies = group.get("proxies")
        if proxies is not None:
            if not isinstance(proxies, list):
                raise ValueError("proxy-groups proxies must be lists")
            filtered = []
            for proxy_index, item in enumerate(proxies):
                if item in inline_names:
                    removed_inline_proxies = True
                    continue
                item_path = safe_path(("proxy-groups", index, "proxies", proxy_index))
                normalized_item = item.strip() if isinstance(item, str) else item
                if item_path in remaining_unresolved_paths:
                    if (
                        not isinstance(normalized_item, str)
                        or normalized_item in BUILTIN_PROXY_TARGETS
                        or normalized_item in inline_names
                        or normalized_item in group_names_set
                        or normalized_item in provider_names_set
                    ):
                        raise ValueError("allowed unresolved proxy target did not remain unresolved at %s" % item_path)
                    removed_paths.append(item_path)
                    remaining_unresolved_paths.remove(item_path)
                    continue
                filtered.append(item)
            if len(filtered) != len(proxies):
                group["proxies"] = filtered
                if removed_inline_proxies:
                    removed_paths.append(safe_path(("proxy-groups", index, "proxies")))
        use = group.get("use")
        if use is not None:
            if not isinstance(use, list):
                raise ValueError("proxy-groups use must be lists")
            filtered_use = [item for item in use if item not in provider_names_set]
            if len(filtered_use) != len(use):
                removed_paths.append(safe_path(("proxy-groups", index, "use")))
            if filtered_use:
                group["use"] = filtered_use
            else:
                group.pop("use", None)
        if removed_inline_proxies:
            injection_groups.append(group_name)
    if remaining_unresolved_paths:
        unresolved_path = sorted(remaining_unresolved_paths)[0]
        raise ValueError("allowed unresolved proxy target was not removed at %s" % unresolved_path)
    return candidate, removed_paths, injection_groups


def ensure_no_provider_leaks(document: Mapping[str, object], provider_names_set: set[str], provider_urls: set[str]) -> None:
    for path, scalar in iter_scalars(document):
        if path and path[-1] == "use" and isinstance(scalar, str) and scalar in provider_names_set:
            raise ValueError("private provider left after transformation")
        if isinstance(scalar, str) and scalar in provider_urls and (not path or path[0] != "rule-providers"):
            raise ValueError("reference contains a source URL outside rule-providers")


def identical_values(values: list[object]) -> bool:
    return all(value == values[0] for value in values[1:])


def collect_structure_paths(expected: object, actual: object, path: tuple[object, ...] = ()) -> list[str]:
    differences: list[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        if list(expected.keys()) != list(actual.keys()):
            differences.append(safe_path(path))
        for key in expected:
            if key not in actual:
                differences.append(safe_path(path + (key,)))
            else:
                differences.extend(collect_structure_paths(expected[key], actual[key], path + (key,)))
        for key in actual:
            if key not in expected:
                differences.append(safe_path(path + (key,)))
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        minimum = min(len(expected), len(actual))
        for index in range(minimum):
            differences.extend(collect_structure_paths(expected[index], actual[index], path + (index,)))
        for index in range(minimum, len(expected)):
            differences.append(safe_path(path + (index,)))
        for index in range(minimum, len(actual)):
            differences.append(safe_path(path + (index,)))
        return differences
    if expected != actual or type(expected) is not type(actual):
        differences.append(safe_path(path))
    return differences


def write_variants(
    template_dir: Path,
    order: list[str],
    transformed: Mapping[str, Mapping[str, object]],
    injections: Mapping[str, list[str]],
) -> tuple[list[str], Path]:
    markers: dict[str, str] = {}
    template_parts: list[str] = []
    differing_keys: list[str] = []
    for root_key in order:
        if root_key == "proxy-providers":
            continue
        if root_key == "proxies":
            template_parts.append("{{ PROXIES_ROOT_YAML }}")
            continue
        values = [transformed[variant][root_key] for variant in REFERENCE_FILENAMES]
        if identical_values(values):
            template_parts.append(dump_root_yaml(root_key, values[0]))
            continue
        marker_name = variant_root_marker(root_key)
        if marker_name in markers.values():
            raise ValueError("variant markers collide")
        markers[root_key] = marker_name
        differing_keys.append(root_key)
        template_parts.append("{{ %s }}" % marker_name)
    template_dir.mkdir(parents=True, exist_ok=True)
    variants_dir = template_dir / "variants"
    variants_dir.mkdir(parents=True, exist_ok=True)
    template_path = template_dir / "clash.yaml.j2"
    atomic_write_text(template_path, "\n\n".join(template_parts) + "\n", 0o644)
    expected_keys = tuple(differing_keys)
    for variant in REFERENCE_FILENAMES:
        variant_document: dict[str, object] = {
            "_generator": {"inject-node-groups": list(injections[variant])}
        }
        for root_key in differing_keys:
            variant_document[root_key] = copy.deepcopy(transformed[variant][root_key])
        if injections[variant] and "proxy-groups" not in variant_document:
            variant_document["proxy-groups"] = copy.deepcopy(transformed[variant]["proxy-groups"])
        if tuple(key for key in variant_document if key != "_generator") != expected_keys:
            actual_keys = tuple(key for key in variant_document if key != "_generator")
            allowed_keys = expected_keys
            if not (
                injections[variant]
                and actual_keys == allowed_keys + ("proxy-groups",)
                and "proxy-groups" not in allowed_keys
            ):
                raise ValueError("variants do not contain the same differing-key set")
        atomic_write_text(
            variants_dir / ("%s.yaml" % variant),
            yaml.safe_dump(variant_document, allow_unicode=True, sort_keys=False),
            0o644,
        )
    return differing_keys, template_path


def write_private_proxy_snapshots(
    private_proxy_dir: Path,
    references: Mapping[str, Mapping[str, object]],
) -> dict[str, int]:
    private_proxy_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for variant, document in references.items():
        snapshot = private_proxy_snapshot(document)
        counts[variant] = len(snapshot["proxies"])
        atomic_write_text(
            private_proxy_dir / ("%s.yaml" % variant),
            yaml.safe_dump(snapshot, allow_unicode=True, sort_keys=False),
            stat.S_IRUSR | stat.S_IWUSR,
        )
    return counts


def main() -> int:
    args = parse_args()
    args.private_proxy_dir = require_private_proxy_dir(args.private_proxy_dir)
    references = {
        variant: require_mapping_root(args.reference_dir / filename)
        for variant, filename in REFERENCE_FILENAMES.items()
    }
    unresolved_proxy_paths: dict[str, list[str]] = {}
    for variant, document in references.items():
        require_no_jinja_scalars(document)
        unresolved_proxy_paths[variant] = validate_reference_document(document, variant=variant)
    orders = [list(document.keys()) for document in references.values()]
    if not all(order == orders[0] for order in orders[1:]):
        raise ValueError("reference top-level order must match")
    private_proxy_counts = write_private_proxy_snapshots(args.private_proxy_dir, references)
    transformed: dict[str, dict[str, object]] = {}
    path_only_changes: dict[str, list[str]] = {}
    injections: dict[str, list[str]] = {}
    for variant, document in references.items():
        variant_inline_names = inline_proxy_names(document)
        variant_provider_names = provider_names(document)
        provider_urls = collect_provider_source_urls(document)
        candidate, removed_paths, injection_groups = strip_private_provider_values(
            document,
            variant_inline_names,
            variant_provider_names,
            allowed_unresolved_proxy_paths=unresolved_proxy_paths[variant],
        )
        ensure_no_provider_leaks(candidate, variant_provider_names, provider_urls)
        transformed[variant] = candidate
        path_only_changes[variant] = removed_paths
        injections[variant] = injection_groups
    differing_keys, template_path = write_variants(args.template_dir, orders[0], transformed, injections)
    print("private-proxy-dir: %s" % args.private_proxy_dir)
    print("template: %s" % template_path)
    print("differing-root-keys: %d" % len(differing_keys))
    for variant in REFERENCE_FILENAMES:
        print("%s: private-proxies=%d" % (variant, private_proxy_counts[variant]))
        print("%s: inject-node-groups=%d" % (variant, len(injections[variant])))
        if unresolved_proxy_paths[variant]:
            print("%s: dropped-unresolved-proxy-targets=%d" % (variant, len(unresolved_proxy_paths[variant])))
        for change in path_only_changes[variant]:
            print("%s: %s" % (variant, change))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print("migration failed: %s" % error, file=sys.stderr)
        raise SystemExit(1)
