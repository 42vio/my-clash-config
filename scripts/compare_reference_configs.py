from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path
from typing import Mapping, Tuple

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from clash_sub.rendering import load_variant, render_variant


REFERENCE_FILENAMES = {
    "balanced": "My-Clash_Balanced.yaml",
    "balanced-win": "My-Clash_Balanced_Win.yaml",
    "privacy": "My-Clash_Privacy.yaml",
}
IGNORED_ROOT_KEYS = {"proxies", "proxy-providers"}
BUILTIN_PROXY_TARGETS = {"DIRECT", "REJECT", "REJECT-DROP", "PASS", "GLOBAL", "COMPATIBLE"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare rendered variants against the ignored reference configs.")
    parser.add_argument("--reference-dir", type=Path, required=True)
    parser.add_argument("--template-dir", type=Path, required=True)
    parser.add_argument("--private-proxy-dir", type=Path, required=True)
    return parser.parse_args()


def safe_difference(path: Tuple[object, ...], kind: str) -> str:
    rendered_path = ".".join(str(item) for item in path)
    return f"{kind}: {rendered_path}"


def load_reference(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("reference root must be a mapping")
    return copy.deepcopy(document)


def proxy_names(document: Mapping[str, object]) -> set[str]:
    proxies = document.get("proxies")
    if not isinstance(proxies, list):
        raise ValueError("reference proxies must be a list")
    names = []
    for proxy in proxies:
        if not isinstance(proxy, dict):
            raise ValueError("reference proxies must be mappings")
        name = proxy.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("reference proxies must be named")
        names.append(name)
    return set(names)


def provider_names(document: Mapping[str, object]) -> set[str]:
    providers = document.get("proxy-providers")
    if providers is None:
        return set()
    if not isinstance(providers, dict):
        raise ValueError("reference proxy-providers must be a mapping")
    return {name for name in providers if isinstance(name, str)}


def load_private_proxy_snapshot(path: Path) -> dict[str, object]:
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("private proxy snapshot must be a mapping")
    proxies = document.get("proxies")
    if not isinstance(proxies, list):
        raise ValueError("private proxy snapshot must contain a proxies list")
    return copy.deepcopy(document)


def normalize_reference(document: Mapping[str, object], inject_groups: tuple[str, ...], variant: str) -> dict[str, object]:
    normalized = copy.deepcopy(dict(document))
    inline_names = proxy_names(normalized)
    provider_name_set = provider_names(normalized)
    group_name_set = {
        group.get("name").strip()
        for group in normalized.get("proxy-groups", [])
        if isinstance(group, dict) and isinstance(group.get("name"), str)
    }
    for key in IGNORED_ROOT_KEYS:
        normalized.pop(key, None)
    groups = normalized.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("reference proxy-groups must be a list")
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("reference proxy-groups entries must be mappings")
        use = group.get("use")
        if isinstance(use, list):
            filtered = [item for item in use if item not in provider_name_set]
            if filtered:
                group["use"] = filtered
            else:
                group.pop("use", None)
        proxies = group.get("proxies")
        if isinstance(proxies, list) and variant == "balanced-win":
            proxies = [
                item
                for item in proxies
                if not (
                    isinstance(item, str)
                    and item.strip() not in BUILTIN_PROXY_TARGETS
                    and item.strip() not in inline_names
                    and item.strip() not in group_name_set
                    and item.strip() not in provider_name_set
                )
            ]
            group["proxies"] = proxies
        if group.get("name") not in inject_groups:
            continue
        if isinstance(proxies, list):
            group["proxies"] = [item for item in proxies if item not in inline_names]
    return normalized


def normalize_rendered(
    document: Mapping[str, object],
    inject_groups: tuple[str, ...],
    private_proxy_snapshot: Mapping[str, object],
) -> dict[str, object]:
    normalized = copy.deepcopy(dict(document))
    for key in IGNORED_ROOT_KEYS:
        normalized.pop(key, None)
    synthetic_names = proxy_names(private_proxy_snapshot)
    groups = normalized.get("proxy-groups")
    if not isinstance(groups, list):
        raise ValueError("rendered proxy-groups must be a list")
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError("rendered proxy-groups entries must be mappings")
        if group.get("name") not in inject_groups:
            continue
        proxies = group.get("proxies")
        if isinstance(proxies, list):
            group["proxies"] = [item for item in proxies if item not in synthetic_names]
    return normalized


def compare_structures(expected: object, actual: object, path: Tuple[object, ...] = ()) -> list[str]:
    differences: list[str] = []
    if isinstance(expected, dict) and isinstance(actual, dict):
        if list(expected.keys()) != list(actual.keys()):
            differences.append(safe_difference(path, "changed"))
        for key in expected:
            if key not in actual:
                differences.append(safe_difference(path + (key,), "missing"))
            else:
                differences.extend(compare_structures(expected[key], actual[key], path + (key,)))
        for key in actual:
            if key not in expected:
                differences.append(safe_difference(path + (key,), "extra"))
        return differences
    if isinstance(expected, list) and isinstance(actual, list):
        minimum = min(len(expected), len(actual))
        for index in range(minimum):
            differences.extend(compare_structures(expected[index], actual[index], path + (index,)))
        for index in range(minimum, len(expected)):
            differences.append(safe_difference(path + (index,), "missing"))
        for index in range(minimum, len(actual)):
            differences.append(safe_difference(path + (index,), "extra"))
        return differences
    if expected != actual or type(expected) is not type(actual):
        differences.append(safe_difference(path, "changed"))
    return differences


def main() -> int:
    args = parse_args()
    exit_code = 0
    for variant, filename in REFERENCE_FILENAMES.items():
        reference = load_reference(args.reference_dir / filename)
        private_proxy_snapshot = load_private_proxy_snapshot(args.private_proxy_dir / ("%s.yaml" % variant))
        variant_spec = load_variant(args.template_dir, variant)
        rendered = yaml.safe_load(render_variant(args.template_dir, variant, private_proxy_snapshot))
        if not isinstance(rendered, dict):
            raise ValueError("rendered root must be a mapping")
        normalized_reference = normalize_reference(reference, variant_spec.inject_node_groups, variant)
        normalized_rendered = normalize_rendered(rendered, variant_spec.inject_node_groups, private_proxy_snapshot)
        differences = compare_structures(normalized_reference, normalized_rendered)
        if differences:
            exit_code = 1
            print("%s:" % variant)
            for difference in differences:
                print(difference)
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ValueError as error:
        print("comparison failed: %s" % error, file=sys.stderr)
        raise SystemExit(1)
