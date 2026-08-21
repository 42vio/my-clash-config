import importlib.util
import re
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml
from jinja2 import UndefinedError

from clash_sub.rendering import load_variant, render_text, render_variant


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = ROOT / "templates"
VARIANTS = ("balanced", "balanced-win", "privacy")

SYNTHETIC_PROXIES = (
    {
        "name": "Synthetic Node",
        "type": "vless",
        "server": "vpn.example.com",
        "port": 443,
        "uuid": "11111111-1111-4111-8111-111111111111",
        "password": "pass:[]{}#,?&*",
    },
    {
        "name": "香港 专线",
        "type": "trojan",
        "server": "hk.example.com",
        "port": 8443,
        "password": "汉字:[]{}#,?&*",
    },
)


def write_template_fixture(path: Path, template_text: str, variant_name: str = "balanced", variant_document=None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "clash.yaml.j2").write_text(template_text, encoding="utf-8")
    variants_dir = path / "variants"
    variants_dir.mkdir(exist_ok=True)
    document = variant_document or {
        "_generator": {"inject-node-groups": ["Selector"]},
        "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["DIRECT"]}],
        "rules": ["MATCH,DIRECT"],
    }
    (variants_dir / f"{variant_name}.yaml").write_text(
        yaml.safe_dump(document, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def private_proxy_snapshot(*names: str) -> dict[str, object]:
    proxies = []
    for index, name in enumerate(names, start=1):
        proxies.append(
            {
                "name": name,
                "type": "vless",
                "server": f"node-{index}.example.com",
                "port": 443,
                "uuid": "11111111-1111-4111-8111-%012d" % index,
            }
        )
    return {"proxies": proxies}


def write_reference_fixture(path: Path, proxy_names: list[str]) -> None:
    document = {
        "mixed-port": 7890,
        "dns": {"enable": True},
        "proxies": private_proxy_snapshot(*proxy_names)["proxies"],
        "proxy-providers": {
            "Subscribe": {
                "type": "http",
                "url": "https://subscription.example/provider",
                "path": "./providers/subscribe.yaml",
            }
        },
        "proxy-groups": [
            {"name": "Selector", "type": "select", "proxies": ["DIRECT"] + list(proxy_names)},
            {"name": "Fallback", "type": "select", "use": ["Subscribe"], "proxies": ["Selector"]},
            {"name": "ByProvider", "type": "select", "use": ["Subscribe"]},
        ],
        "rule-providers": {
            "Apple": {
                "type": "http",
                "behavior": "classical",
                "url": "https://rules.example/apple.yaml",
                "path": "./rules/apple.yaml",
            }
        },
        "rules": ["MATCH,Selector"],
    }
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")


def write_reference_document(path: Path, document: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8")


def load_private_snapshot(path: Path) -> dict[str, object]:
    snapshot = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(snapshot, dict):
        raise AssertionError("private snapshot must be a mapping")
    return snapshot


def run_python_script(script_name: str, *args: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / script_name), *[str(argument) for argument in args]],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def load_script_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"cannot load module {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RenderingTests(unittest.TestCase):
    def test_all_variants_render_complete_expanded_documents(self):
        rendered = {
            variant: yaml.safe_load(render_variant(TEMPLATE_DIR, variant, SYNTHETIC_PROXIES))
            for variant in VARIANTS
        }
        for document in rendered.values():
            self.assertIn("dns", document)
            self.assertIn("proxies", document)
            self.assertIn("proxy-groups", document)
            self.assertIn("rule-providers", document)
            self.assertIn("rules", document)
            self.assertNotIn("proxy-providers", document)
        self.assertNotEqual(rendered["balanced"], rendered["balanced-win"])
        self.assertNotEqual(rendered["balanced"], rendered["privacy"])

    def test_nodes_are_injected_only_into_declared_groups(self):
        variant = load_variant(TEMPLATE_DIR, "balanced")
        document = yaml.safe_load(render_variant(TEMPLATE_DIR, "balanced", SYNTHETIC_PROXIES))
        groups = {item["name"]: item for item in document["proxy-groups"]}
        for group_name in variant.inject_node_groups:
            self.assertIn("Synthetic Node", groups[group_name]["proxies"])
        for group_name, group in groups.items():
            if group_name not in variant.inject_node_groups:
                self.assertNotIn("Synthetic Node", group.get("proxies", []))

    def test_strict_undefined_rejects_unknown_template_marker(self):
        with self.assertRaises(UndefinedError):
            render_text("{{ UNKNOWN_MARKER }}", {})

    def test_load_variant_removes_generator_metadata(self):
        variant = load_variant(TEMPLATE_DIR, "balanced")

        self.assertNotIn("_generator", variant.top_level)
        self.assertTrue(variant.inject_node_groups)

    def test_render_variant_preserves_unicode_special_characters_and_key_order(self):
        with TemporaryDirectory() as directory:
            template_dir = Path(directory)
            write_template_fixture(
                template_dir,
                "alpha: 1\n{{ PROXIES_ROOT_YAML }}\n{{ VARIANT_PROXY_GROUPS_ROOT_YAML }}\nomega: true\n{{ VARIANT_RULES_ROOT_YAML }}\n",
                variant_document={
                    "_generator": {"inject-node-groups": ["Selector"]},
                    "rules": ["MATCH,DIRECT"],
                    "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["DIRECT"]}],
                },
            )

            rendered_text = render_variant(template_dir, "balanced", SYNTHETIC_PROXIES)
            rendered = yaml.safe_load(rendered_text)

            self.assertEqual(list(rendered), ["alpha", "proxies", "proxy-groups", "omega", "rules"])
            self.assertEqual([proxy["name"] for proxy in rendered["proxies"]], ["Synthetic Node", "香港 专线"])
            self.assertEqual(rendered["proxies"][0]["password"], "pass:[]{}#,?&*")
            self.assertEqual(rendered["proxies"][1]["password"], "汉字:[]{}#,?&*")

    def test_render_variant_rejects_missing_injection_group(self):
        with TemporaryDirectory() as directory:
            template_dir = Path(directory)
            write_template_fixture(template_dir, "{{ PROXIES_ROOT_YAML }}\n{{ VARIANT_PROXY_GROUPS_ROOT_YAML }}\n")
            variant_path = template_dir / "variants" / "balanced.yaml"
            variant_path.write_text(
                yaml.safe_dump(
                    {
                        "_generator": {"inject-node-groups": ["Missing"]},
                        "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["DIRECT"]}],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Missing"):
                render_variant(template_dir, "balanced", SYNTHETIC_PROXIES)

    def test_render_variant_rejects_duplicate_injection_group_names(self):
        with TemporaryDirectory() as directory:
            template_dir = Path(directory)
            write_template_fixture(template_dir, "{{ PROXIES_ROOT_YAML }}\n{{ VARIANT_PROXY_GROUPS_ROOT_YAML }}\n")
            variant_path = template_dir / "variants" / "balanced.yaml"
            variant_path.write_text(
                yaml.safe_dump(
                    {
                        "_generator": {"inject-node-groups": ["Selector"]},
                        "proxy-groups": [
                            {"name": "Selector", "type": "select", "proxies": ["DIRECT"]},
                            {"name": "Selector", "type": "select", "proxies": ["REJECT"]},
                        ],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "Selector"):
                render_variant(template_dir, "balanced", SYNTHETIC_PROXIES)

    def test_render_variant_leaves_no_template_markers_or_source_url(self):
        for variant in VARIANTS:
            rendered_text = render_variant(TEMPLATE_DIR, variant, SYNTHETIC_PROXIES)
            self.assertIsNone(re.search(r"{{|}}", rendered_text))
            self.assertNotIn("panel.example", rendered_text)
            self.assertNotIn("convert.example", rendered_text)

    def test_render_variant_accepts_private_proxy_snapshot_mapping(self):
        with TemporaryDirectory() as directory:
            template_dir = Path(directory)
            write_template_fixture(
                template_dir,
                "{{ PROXIES_ROOT_YAML }}\n{{ VARIANT_PROXY_GROUPS_ROOT_YAML }}\n",
                variant_document={
                    "_generator": {"inject-node-groups": ["Selector"]},
                    "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["DIRECT"]}],
                },
            )

            rendered = yaml.safe_load(
                render_variant(template_dir, "balanced", private_proxy_snapshot("Owner XUI", "Owner Home"))
            )

            self.assertEqual(
                [proxy["name"] for proxy in rendered["proxies"]],
                ["Owner XUI", "Owner Home"],
            )
            self.assertEqual(
                rendered["proxy-groups"][0]["proxies"],
                ["DIRECT", "Owner XUI", "Owner Home"],
            )

    def test_variant_specific_private_snapshots_allow_balanced_win_to_omit_home_nodes(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        with TemporaryDirectory() as directory:
            temp_root = Path(directory)
            reference_dir = temp_root / "references"
            template_dir = temp_root / "templates"
            private_proxy_dir = temp_root / "private" / "sources" / "owner"
            reference_dir.mkdir(parents=True)

            write_reference_fixture(
                reference_dir / "My-Clash_Balanced.yaml",
                ["Owner XUI", "Owner Home"],
            )
            write_reference_fixture(
                reference_dir / "My-Clash_Balanced_Win.yaml",
                ["Owner XUI"],
            )
            write_reference_fixture(
                reference_dir / "My-Clash_Privacy.yaml",
                ["Owner XUI", "Owner Home"],
            )

            references = {
                variant: migration.require_mapping_root(reference_dir / filename)
                for variant, filename in migration.REFERENCE_FILENAMES.items()
            }
            for document in references.values():
                migration.require_no_jinja_scalars(document)
                migration.validate_reference_document(document)
            orders = [list(document.keys()) for document in references.values()]
            self.assertTrue(all(order == orders[0] for order in orders[1:]))
            migration.write_private_proxy_snapshots(private_proxy_dir, references)
            transformed = {}
            injections = {}
            for variant, document in references.items():
                variant_inline_names = migration.inline_proxy_names(document)
                variant_provider_names = migration.provider_names(document)
                provider_urls = migration.collect_provider_source_urls(document)
                candidate, _, injection_groups = migration.strip_private_provider_values(
                    document,
                    variant_inline_names,
                    variant_provider_names,
                )
                migration.ensure_no_provider_leaks(candidate, variant_provider_names, provider_urls)
                transformed[variant] = candidate
                injections[variant] = injection_groups
            migration.write_variants(template_dir, orders[0], transformed, injections)

            compare = run_python_script(
                "compare_reference_configs.py",
                "--reference-dir",
                reference_dir,
                "--template-dir",
                template_dir,
                "--private-proxy-dir",
                private_proxy_dir,
            )
            self.assertEqual(compare.returncode, 0, compare.stdout + compare.stderr)

            balanced = yaml.safe_load(
                render_variant(
                    template_dir,
                    "balanced",
                    load_private_snapshot(private_proxy_dir / "balanced.yaml"),
                )
            )
            balanced_win = yaml.safe_load(
                render_variant(
                    template_dir,
                    "balanced-win",
                    load_private_snapshot(private_proxy_dir / "balanced-win.yaml"),
                )
            )

            self.assertIn("Owner Home", [proxy["name"] for proxy in balanced["proxies"]])
            self.assertNotIn("Owner Home", [proxy["name"] for proxy in balanced_win["proxies"]])
            self.assertTrue((private_proxy_dir / "privacy.yaml").exists())

    def test_balanced_win_migration_drops_stale_unresolved_home_target_and_compare_stays_clean(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        with TemporaryDirectory() as directory:
            temp_root = Path(directory)
            reference_dir = temp_root / "references"
            template_dir = temp_root / "templates"
            private_proxy_dir = temp_root / "private" / "sources" / "owner"
            reference_dir.mkdir(parents=True)

            balanced_document = {
                "dns": {"enable": True},
                "proxies": private_proxy_snapshot("Owner XUI", "Owner Home")["proxies"],
                "proxy-providers": {
                    "Subscribe": {
                        "type": "http",
                        "url": "https://subscription.example/provider",
                        "path": "./providers/subscribe.yaml",
                    }
                },
                "proxy-groups": [
                    {"name": "Selector", "type": "select", "proxies": ["DIRECT", "Owner XUI", "Owner Home"]},
                    {"name": "HomeServer", "type": "fallback", "proxies": ["DIRECT", "Owner Home"]},
                ],
                "rules": ["MATCH,Selector"],
            }
            balanced_win_document = {
                "dns": {"enable": True},
                "proxies": private_proxy_snapshot("Owner XUI")["proxies"],
                "proxy-providers": {
                    "Subscribe": {
                        "type": "http",
                        "url": "https://subscription.example/provider",
                        "path": "./providers/subscribe.yaml",
                    }
                },
                "proxy-groups": [
                    {"name": "Selector", "type": "select", "proxies": ["DIRECT", "Owner XUI"]},
                    {"name": "Group1", "type": "select", "proxies": ["Selector"]},
                    {"name": "Group2", "type": "select", "proxies": ["Group1"]},
                    {"name": "Group3", "type": "select", "proxies": ["Group2"]},
                    {"name": "Group4", "type": "select", "proxies": ["Group3"]},
                    {"name": "HomeServer", "type": "select", "proxies": ["DIRECT", "Selector", "Unavailable Home"]},
                ],
                "rules": ["MATCH,Selector"],
            }
            privacy_document = {
                "dns": {"enable": True},
                "proxies": private_proxy_snapshot("Owner XUI", "Owner Home")["proxies"],
                "proxy-providers": {
                    "Subscribe": {
                        "type": "http",
                        "url": "https://subscription.example/provider",
                        "path": "./providers/subscribe.yaml",
                    }
                },
                "proxy-groups": [
                    {"name": "Selector", "type": "select", "proxies": ["DIRECT", "Owner XUI", "Owner Home"]},
                    {"name": "HomeServer", "type": "fallback", "proxies": ["DIRECT", "Owner Home"]},
                ],
                "rules": ["MATCH,Selector"],
            }

            write_reference_document(reference_dir / "My-Clash_Balanced.yaml", balanced_document)
            write_reference_document(reference_dir / "My-Clash_Balanced_Win.yaml", balanced_win_document)
            write_reference_document(reference_dir / "My-Clash_Privacy.yaml", privacy_document)

            references = {
                variant: migration.require_mapping_root(reference_dir / filename)
                for variant, filename in migration.REFERENCE_FILENAMES.items()
            }
            unresolved_paths = {}
            for variant, document in references.items():
                migration.require_no_jinja_scalars(document)
                unresolved_paths[variant] = migration.validate_reference_document(document, variant=variant)

            self.assertEqual(unresolved_paths["balanced"], [])
            self.assertEqual(unresolved_paths["balanced-win"], ["proxy-groups[5].proxies[2]"])
            self.assertEqual(unresolved_paths["privacy"], [])

            orders = [list(document.keys()) for document in references.values()]
            self.assertTrue(all(order == orders[0] for order in orders[1:]))
            migration.write_private_proxy_snapshots(private_proxy_dir, references)
            transformed = {}
            path_only_changes = {}
            injections = {}
            for variant, document in references.items():
                variant_inline_names = migration.inline_proxy_names(document)
                variant_provider_names = migration.provider_names(document)
                provider_urls = migration.collect_provider_source_urls(document)
                candidate, removed_paths, injection_groups = migration.strip_private_provider_values(
                    document,
                    variant_inline_names,
                    variant_provider_names,
                    allowed_unresolved_proxy_paths=unresolved_paths[variant],
                )
                migration.ensure_no_provider_leaks(candidate, variant_provider_names, provider_urls)
                transformed[variant] = candidate
                path_only_changes[variant] = removed_paths
                injections[variant] = injection_groups

            self.assertEqual(path_only_changes["balanced-win"], ["proxy-groups[0].proxies", "proxy-groups[5].proxies[2]"])
            self.assertNotIn(
                "Unavailable Home",
                transformed["balanced-win"]["proxy-groups"][5]["proxies"],
            )

            migration.write_variants(template_dir, orders[0], transformed, injections)

            compare = run_python_script(
                "compare_reference_configs.py",
                "--reference-dir",
                reference_dir,
                "--template-dir",
                template_dir,
                "--private-proxy-dir",
                private_proxy_dir,
            )
            self.assertEqual(compare.returncode, 0, compare.stdout + compare.stderr)

    def test_migration_private_snapshots_use_mode_600(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        with TemporaryDirectory() as directory:
            temp_root = Path(directory)
            private_proxy_dir = temp_root / "private" / "sources" / "owner"
            references = {
                "balanced": {"proxies": private_proxy_snapshot("Owner XUI", "Owner Home")["proxies"]},
                "balanced-win": {"proxies": private_proxy_snapshot("Owner XUI")["proxies"]},
                "privacy": {"proxies": private_proxy_snapshot("Owner XUI", "Owner Home")["proxies"]},
            }

            counts = migration.write_private_proxy_snapshots(private_proxy_dir, references)

            for variant in VARIANTS:
                snapshot_path = private_proxy_dir / f"{variant}.yaml"
                self.assertEqual(counts[variant], len(load_private_snapshot(snapshot_path)["proxies"]))
                self.assertEqual(stat.S_IMODE(snapshot_path.stat().st_mode), 0o600)

    def test_migration_rejects_private_proxy_dir_outside_primary_checkout_before_writes(self):
        with TemporaryDirectory() as directory:
            temp_root = Path(directory)
            reference_dir = temp_root / "references"
            template_dir = temp_root / "templates"
            outside_private_proxy_dir = temp_root / "outside" / "sources" / "owner"
            reference_dir.mkdir(parents=True)

            write_reference_fixture(reference_dir / "My-Clash_Balanced.yaml", ["Owner XUI", "Owner Home"])
            write_reference_fixture(reference_dir / "My-Clash_Balanced_Win.yaml", ["Owner XUI"])
            write_reference_fixture(reference_dir / "My-Clash_Privacy.yaml", ["Owner XUI", "Owner Home"])

            migration = run_python_script(
                "migrate_reference_templates.py",
                "--reference-dir",
                reference_dir,
                "--template-dir",
                template_dir,
                "--private-proxy-dir",
                outside_private_proxy_dir,
            )

            self.assertNotEqual(migration.returncode, 0)
            self.assertIn("private/sources/owner", migration.stderr)
            self.assertFalse(template_dir.exists())
            self.assertFalse(outside_private_proxy_dir.exists())

    def test_provider_only_groups_are_not_marked_for_inline_proxy_injection(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        document = {
            "proxy-groups": [
                {"name": "Selector", "type": "select", "proxies": ["DIRECT", "Owner XUI"]},
                {"name": "ByProvider", "type": "select", "use": ["Subscribe"]},
            ]
        }

        _, removed_paths, injection_groups = migration.strip_private_provider_values(
            document,
            {"Owner XUI"},
            {"Subscribe"},
        )

        self.assertEqual(
            removed_paths,
            ["proxy-groups[0].proxies", "proxy-groups[1].use"],
        )
        self.assertEqual(injection_groups, ["Selector"])

    def test_validate_reference_document_accepts_reject_drop_builtin_target(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        document = {
            "proxies": [{"name": "Owner XUI", "type": "vless", "server": "node.example.com", "port": 443}],
            "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["REJECT-DROP"]}],
        }

        migration.validate_reference_document(document)

    def test_validate_reference_document_rejects_unresolved_proxy_target_with_path_only_error(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        document = {
            "proxies": [{"name": "Owner XUI", "type": "vless", "server": "node.example.com", "port": 443}],
            "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["REJECT-DROP", "Missing Node"]}],
        }

        with self.assertRaisesRegex(ValueError, r"proxy-groups\[0\]\.proxies\[1\]") as context:
            migration.validate_reference_document(document)

        self.assertNotIn("Missing Node", str(context.exception))

    def test_balanced_variant_still_rejects_unknown_proxy_target(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        document = {
            "proxies": [{"name": "Owner XUI", "type": "vless", "server": "node.example.com", "port": 443}],
            "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["DIRECT", "Unknown Target"]}],
        }

        with self.assertRaisesRegex(ValueError, r"proxy-groups\[0\]\.proxies\[1\]") as context:
            migration.validate_reference_document(document, variant="balanced")

        self.assertNotIn("Unknown Target", str(context.exception))

    def test_balanced_win_rejects_distinct_unresolved_proxy_targets(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        document = {
            "proxies": [{"name": "Owner XUI", "type": "vless", "server": "node.example.com", "port": 443}],
            "proxy-groups": [
                {"name": "Selector", "type": "select", "proxies": ["DIRECT", "Unknown Target A", "Unknown Target B"]}
            ],
        }

        with self.assertRaisesRegex(ValueError, r"proxy-groups\[0\]\.proxies\[2\]") as context:
            migration.validate_reference_document(document, variant="balanced-win")

        self.assertNotIn("Unknown Target A", str(context.exception))
        self.assertNotIn("Unknown Target B", str(context.exception))

    def test_private_proxy_dir_guard_accepts_only_primary_checkout_owner_directory(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        with TemporaryDirectory() as directory:
            primary_root = Path(directory) / "repo"
            common_dir = primary_root / ".git"
            intended_dir = primary_root / "private" / "sources" / "owner"
            common_dir.mkdir(parents=True)

            self.assertEqual(
                migration.primary_checkout_root_from_git_common_dir(common_dir),
                primary_root.resolve(),
            )
            self.assertEqual(
                migration.require_private_proxy_dir(intended_dir, expected_dir=intended_dir),
                intended_dir.resolve(),
            )

            with self.assertRaisesRegex(ValueError, "private/sources/owner"):
                migration.require_private_proxy_dir(primary_root / "tmp" / "owner", expected_dir=intended_dir)

    def test_atomic_write_text_cleans_up_temporary_file_when_replace_fails(self):
        migration = load_script_module(
            "migrate_reference_templates",
            ROOT / "scripts" / "migrate_reference_templates.py",
        )
        with TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            output.mkdir()
            target = output / "balanced.yaml"
            target.mkdir()

            with self.assertRaises(OSError):
                migration.atomic_write_text(target, "proxies: []\n", 0o600)

            self.assertEqual(list(output.glob("tmp*")), [])


if __name__ == "__main__":
    unittest.main()
