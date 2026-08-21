import copy
import hashlib
import json
import stat
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import yaml

from clash_sub.converter import SourceError
from clash_sub.models import (
    Candidate,
    CertificateSettings,
    PublicationSettings,
    RealitySettings,
    ServiceSettings,
    Settings,
    SourceSpec,
    SubscriptionUserinfo,
    UserSpec,
    VARIANTS,
    XuiSettings,
)
from clash_sub.releases import (
    BuildError,
    ReleaseBuilder,
    _cleanup_candidate,
    list_history,
    publish_candidate,
    rollback,
)
from clash_sub.validation import sha256_file


def private_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


class FakeConverterClient:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def convert(self, source_url: str):
        self.calls.append(source_url)
        response = self._responses[source_url]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class FakeTrafficClient:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def fetch(self, source_url: str):
        self.calls.append(source_url)
        response = self._responses.get(source_url)
        if isinstance(response, Exception):
            raise response
        return response


class FakeLocalLoader:
    def __init__(self, responses):
        self._responses = responses
        self.calls = []

    def __call__(self, path: Path):
        self.calls.append(path)
        response = self._responses[path]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)


class FakeRenderer:
    def __init__(self):
        self.calls = []
        self.fail_variant = None

    def __call__(self, template_dir: Path, variant: str, private_proxy_snapshot):
        if self.fail_variant == variant:
            raise ValueError("synthetic render failure")
        proxies = []
        if isinstance(private_proxy_snapshot, dict):
            proxies = copy.deepcopy(private_proxy_snapshot.get("proxies", []))
        else:
            proxies = copy.deepcopy(list(private_proxy_snapshot))
        self.calls.append((template_dir, variant, [proxy["name"] for proxy in proxies]))
        document = {
            "dns": {"enable": True},
            "proxies": proxies,
            "proxy-groups": [
                {
                    "name": "Selector",
                    "type": "select",
                    "proxies": ["DIRECT"] + [proxy["name"] for proxy in proxies],
                }
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
            "variant": variant,
        }
        return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)


class FakeValidator:
    def __init__(self):
        self.calls = []

    def __call__(self, text: str, source_urls, reality: RealitySettings):
        self.calls.append((text, tuple(source_urls), reality))
        return yaml.safe_load(text)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.private_root = Path(self.directory.name) / "private"
        (self.private_root / "sources" / "owner").mkdir(parents=True)
        (self.private_root / "reference-configs" / "2026-08-21").mkdir(parents=True)
        self.airport_path = self.private_root / "sources" / "owner" / "airport.yaml"
        self.home_path = self.private_root / "sources" / "owner" / "home.yaml"
        self.airport_path.write_text("synthetic-airport", encoding="utf-8")
        self.home_path.write_text("synthetic-home", encoding="utf-8")

        self.template_dir = Path(self.directory.name) / "templates"
        (self.template_dir / "variants").mkdir(parents=True)
        (self.template_dir / "clash.yaml.j2").write_text(
            "{{ PROXIES_ROOT_YAML }}\n{{ VARIANT_PROXY_GROUPS_ROOT_YAML }}\n{{ VARIANT_RULES_ROOT_YAML }}\n",
            encoding="utf-8",
        )
        for variant in ("balanced", "balanced-win", "privacy"):
            (self.template_dir / "variants" / ("%s.yaml" % variant)).write_text(
                yaml.safe_dump(
                    {
                        "_generator": {"inject-node-groups": ["Selector"]},
                        "proxy-groups": [{"name": "Selector", "type": "select", "proxies": ["DIRECT"]}],
                        "rules": ["MATCH,DIRECT"],
                    },
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

        self.owner_url = "http://127.0.0.1:2096/sub/owner-secret"
        self.friend_url = "http://127.0.0.1:2096/sub/friend-secret"
        self.converter = FakeConverterClient(
            {
                self.owner_url: (
                    self.reality_proxy("owner-xui-node", "11111111-1111-4111-8111-111111111111"),
                ),
                self.friend_url: (
                    self.reality_proxy("friend-node", "22222222-2222-4222-8222-222222222222"),
                ),
            }
        )
        self.traffic = FakeTrafficClient(
            {
                self.owner_url: SubscriptionUserinfo(10, 20, 100, 1893456000),
                self.friend_url: SubscriptionUserinfo(1, 2, 10, 1893456001),
            }
        )
        self.local_loader = FakeLocalLoader(
            {
                self.airport_path: (
                    {
                        "name": "owner-airport-node",
                        "type": "trojan",
                        "server": "airport.example.com",
                        "port": 443,
                        "password": "airport-password",
                    },
                ),
                self.home_path: (
                    {
                        "name": "owner-home-node",
                        "type": "trojan",
                        "server": "home.example.com",
                        "port": 443,
                        "password": "home-password",
                    },
                ),
            }
        )
        self.renderer = FakeRenderer()
        self.validator = FakeValidator()
        self.clock_value = datetime(2026, 8, 21, 12, 0, 0, tzinfo=timezone.utc)
        self.builder = self.make_builder()

    def make_builder(self, settings=None):
        current_settings = settings or self.make_settings()
        return ReleaseBuilder(
            current_settings,
            self.converter,
            self.traffic,
            local_loader=self.local_loader,
            renderer=self.renderer,
            validator=self.validator,
            template_dir=self.template_dir,
            clock=lambda: self.clock_value,
        )

    def make_settings(self, friend_local_sources=()):
        service = ServiceSettings(
            private_root=self.private_root,
            converter_base_url="http://127.0.0.1:25500",
            publication=PublicationSettings(
                mode="domain",
                subscription_authority="sub.example.com:8443",
                panel_authority="panel.example.com:8443",
                publisher_listen="127.0.0.1",
                publisher_port=25501,
            ),
            reality=RealitySettings(
                public_address="198.51.100.25",
                public_port=443,
                required_flow="xtls-rprx-vision",
            ),
            xui=XuiSettings(
                panel_listen="127.0.0.1",
                panel_port=2053,
                panel_base_path="/panel/",
                subscription_listen="127.0.0.1",
                subscription_port=2096,
                xray_config_path=self.private_root / "xray-config.json",
                xray_binary_path=self.private_root / "xray",
                expected_panel_version="3.6.0",
                expected_xray_version="26.6.27",
            ),
            certificate=CertificateSettings(
                fullchain_path=self.private_root / "fullchain.pem",
                alert_before_seconds=1209600,
                alert_command=(),
            ),
        )
        owner = UserSpec(
            user_id="owner",
            role="owner",
            token_sha256="a" * 64,
            variants=("balanced", "balanced-win", "privacy"),
            xui_source=SourceSpec(kind="xui", label="owner", url=self.owner_url),
            local_sources=(
                SourceSpec(kind="airport", label="airport", path=self.airport_path),
                SourceSpec(kind="home", label="home", path=self.home_path),
            ),
        )
        friend = UserSpec(
            user_id="friend",
            role="member",
            token_sha256="b" * 64,
            variants=("balanced",),
            xui_source=SourceSpec(kind="xui", label="friend", url=self.friend_url),
            local_sources=tuple(friend_local_sources),
        )
        return Settings(service=service, users={"owner": owner, "friend": friend})

    def reality_proxy(self, name: str, uuid: str):
        return {
            "name": name,
            "type": "vless",
            "server": "127.0.0.1",
            "port": 2096,
            "uuid": uuid,
            "network": "tcp",
            "tls": True,
            "flow": "xtls-rprx-vision",
            "servername": "www.example.com",
            "client-fingerprint": "chrome",
            "reality-opts": {
                "public-key": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
                "short-id": "0123456789abcdef",
            },
        }

    def publish_valid_owner_release(self, operation_id="op-owner"):
        candidate = self.builder.build_candidate("owner", operation_id)
        return publish_candidate(candidate, self.private_root, keep=5)

    def tamper_manifest(self, directory: Path, **changes):
        manifest_path = directory / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(changes)
        manifest_text = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        manifest_path.write_text(manifest_text, encoding="utf-8")

    def write_manifest_digest(self, directory: Path):
        manifest_path = directory / "manifest.json"
        digest_path = directory / "manifest.sha256"
        manifest_bytes = manifest_path.read_bytes()
        digest_path.write_text(hashlib.sha256(manifest_bytes).hexdigest() + "\n", encoding="utf-8")

    def test_member_candidate_contains_only_its_own_xui_nodes(self):
        candidate = self.builder.build_candidate("friend", "op-friend")

        text = candidate.files["balanced"].read_text(encoding="utf-8")

        self.assertIn("friend-node", text)
        self.assertNotIn("owner-xui-node", text)
        self.assertNotIn("owner-airport-node", text)
        self.assertNotIn("owner-home-node", text)
        self.assertEqual(self.local_loader.calls, [])

    def test_build_candidate_rejects_unsafe_operation_id_before_creating_paths(self):
        escaped_root = Path(self.directory.name) / "escaped-build"

        with self.assertRaises(BuildError):
            self.builder.build_candidate("owner", "../../escaped-build")

        self.assertFalse(escaped_root.exists())

    def test_build_candidate_rejects_unsafe_user_id_before_path_join(self):
        unsafe_user = UserSpec(
            user_id="../owner",
            role="owner",
            token_sha256="c" * 64,
            variants=tuple(VARIANTS),
            xui_source=SourceSpec(kind="xui", label="owner", url=self.owner_url),
            local_sources=(),
        )
        settings = Settings(service=self.make_settings().service, users={"../owner": unsafe_user})
        builder = self.make_builder(settings=settings)

        with self.assertRaises(BuildError):
            builder.build_candidate("../owner", "op-unsafe-user")

        self.assertFalse((self.private_root / "staging" / "op-unsafe-user").exists())

    def test_owner_switches_all_three_variants_together(self):
        candidate = self.builder.build_candidate("owner", "op-owner")

        release = publish_candidate(candidate, self.private_root, keep=5)

        self.assertEqual(
            (self.private_root / "current" / "owner").resolve(),
            release.path.resolve(),
        )
        self.assertEqual(
            set(release.files),
            {"balanced", "balanced-win", "privacy"},
        )
        self.assertTrue((self.private_root / "current" / "owner").is_symlink())

    def test_member_publish_uses_declared_variant_subset(self):
        candidate = self.builder.build_candidate("friend", "op-friend")

        release = publish_candidate(candidate, self.private_root, keep=5)

        self.assertEqual(
            (self.private_root / "current" / "friend").resolve(),
            release.path.resolve(),
        )
        self.assertEqual(set(release.files), {"balanced"})
        self.assertTrue((release.path / "balanced.yaml").exists())
        self.assertTrue((release.path / "balanced.meta.json").exists())
        self.assertFalse((release.path / "balanced-win.yaml").exists())
        self.assertFalse((release.path / "privacy.yaml").exists())
        manifest = json.loads((release.path / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["variants"], ["balanced"])
        self.assertEqual(set(manifest["output_hashes"]), {"balanced"})

    def test_failed_build_does_not_change_current(self):
        previous = self.publish_valid_owner_release()
        self.renderer.fail_variant = "privacy"

        with self.assertRaises(BuildError):
            self.builder.build_candidate("owner", "op-failing")

        self.assertEqual(
            (self.private_root / "current" / "owner").resolve(),
            previous.path.resolve(),
        )

    def test_cleanup_candidate_refuses_to_delete_outside_staging_root(self):
        staging_root = self.private_root / "staging"
        staging_root.mkdir(parents=True)
        escaped_root = Path(self.directory.name) / "escaped-cleanup"
        escaped_root.mkdir()
        (escaped_root / "sentinel.txt").write_text("keep", encoding="utf-8")

        with self.assertRaises(BuildError):
            _cleanup_candidate(staging_root, escaped_root)

        self.assertTrue(escaped_root.exists())
        self.assertTrue((escaped_root / "sentinel.txt").exists())

    def test_candidate_uses_private_modes_and_writes_sidecars(self):
        candidate = self.builder.build_candidate("owner", "op-owner")

        self.assertEqual(private_mode(candidate.path), 0o700)
        self.assertEqual(private_mode(candidate.manifest_path), 0o600)
        self.assertEqual(private_mode(candidate.path / "manifest.sha256"), 0o600)
        for variant, path in candidate.files.items():
            self.assertEqual(private_mode(path), 0o600)
            self.assertEqual(private_mode(path.with_suffix(".meta.json")), 0o600)
            self.assertTrue(path.with_suffix(".meta.json").exists(), variant)

    def test_owner_build_requires_local_snapshots(self):
        self.local_loader = FakeLocalLoader(
            {
                self.airport_path: (
                    {
                        "name": "owner-airport-node",
                        "type": "trojan",
                        "server": "airport.example.com",
                        "port": 443,
                        "password": "airport-password",
                    },
                ),
                self.home_path: SourceError("missing local snapshot"),
            }
        )
        self.builder = self.make_builder()

        with self.assertRaises(BuildError) as context:
            self.builder.build_candidate("owner", "op-missing-home")

        self.assertNotIn(str(self.home_path), str(context.exception))

    def test_member_build_rejects_local_sources_even_with_synthetic_settings(self):
        settings = self.make_settings(
            friend_local_sources=(SourceSpec(kind="home", label="home", path=self.home_path),)
        )
        builder = self.make_builder(settings=settings)

        with self.assertRaisesRegex(BuildError, "member"):
            builder.build_candidate("friend", "op-member-local")

    def test_manifest_is_sanitized_and_omits_secret_values(self):
        candidate = self.builder.build_candidate("owner", "op-owner")

        manifest_text = candidate.manifest_path.read_text(encoding="utf-8")
        manifest = json.loads(manifest_text)
        manifest_digest_text = (candidate.path / "manifest.sha256").read_text(encoding="utf-8")

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["variants"], ["balanced", "balanced-win", "privacy"])
        self.assertEqual(set(manifest["input_hashes"]), {"template", "xui", "airport", "home"})
        self.assertEqual(set(manifest["source_counts"]), {"xui", "airport", "home"})
        self.assertRegex(manifest_digest_text, "^[0-9a-f]{64}\n$")
        self.assertNotIn(self.owner_url, manifest_text)
        self.assertNotIn("owner-airport-node", manifest_text)
        self.assertNotIn("owner-home-node", manifest_text)
        self.assertNotIn("11111111-1111-4111-8111-111111111111", manifest_text)
        self.assertNotIn("airport-password", manifest_text)
        self.assertNotIn("a" * 64, manifest_text)

    def test_sidecar_contains_traffic_but_not_proxy_secrets(self):
        candidate = self.builder.build_candidate("friend", "op-friend")

        sidecar_text = candidate.files["balanced"].with_suffix(".meta.json").read_text(encoding="utf-8")
        sidecar = json.loads(sidecar_text)

        self.assertEqual(sidecar["traffic"]["upload"], 1)
        self.assertEqual(sidecar["traffic"]["download"], 2)
        self.assertEqual(sidecar["traffic"]["total"], 10)
        self.assertEqual(sidecar["traffic"]["expire"], 1893456001)
        self.assertEqual(sidecar["yaml_sha256"], sha256_file(candidate.files["balanced"]))
        self.assertNotIn(self.friend_url, sidecar_text)
        self.assertNotIn("friend-node", sidecar_text)
        self.assertNotIn("22222222-2222-4222-8222-222222222222", sidecar_text)

    def test_publish_candidate_rejects_unsafe_candidate_user_id_before_move(self):
        candidate = self.builder.build_candidate("owner", "op-owner")
        forged = Candidate(
            operation_id=candidate.operation_id,
            user_id="../../escaped-release-owner",
            path=candidate.path,
            files=candidate.files,
            manifest_path=candidate.manifest_path,
        )
        escaped_root = Path(self.directory.name) / "escaped-release-owner"

        with self.assertRaises(BuildError):
            publish_candidate(forged, self.private_root, keep=5)

        self.assertFalse(escaped_root.exists())
        self.assertTrue(candidate.path.exists())

    def test_publish_candidate_rejects_nested_staging_child_forgery(self):
        candidate = self.builder.build_candidate("owner", "op-owner")
        forged_root = self.private_root / "staging" / "op-owner" / "forged"
        forged_root.mkdir()
        moved_path = forged_root / "owner"
        candidate.path.rename(moved_path)
        forged = Candidate(
            operation_id=candidate.operation_id,
            user_id=candidate.user_id,
            path=moved_path,
            files={variant: moved_path / path.name for variant, path in candidate.files.items()},
            manifest_path=moved_path / "manifest.json",
        )

        with self.assertRaises(BuildError):
            publish_candidate(forged, self.private_root, keep=5)

        self.assertTrue(moved_path.exists())
        self.assertFalse((self.private_root / "staging" / "op-owner" / "owner").exists())

    def test_publish_candidate_rejects_symlinked_expected_staging_user_path(self):
        candidate = self.builder.build_candidate("owner", "op-owner")
        external_root = Path(self.directory.name) / "outside-candidate"
        candidate.path.rename(external_root)
        candidate.path.symlink_to(external_root, target_is_directory=True)

        with self.assertRaises(BuildError):
            publish_candidate(candidate, self.private_root, keep=5)

        self.assertTrue(candidate.path.is_symlink())
        self.assertTrue(external_root.exists())
        self.assertFalse((self.private_root / "releases" / "owner" / "op-owner").exists())

    def test_publish_rejects_candidate_if_manifest_changes_after_write(self):
        candidate = self.builder.build_candidate("owner", "op-owner")
        self.tamper_manifest(candidate.path, created_at="2099-12-31T23:59:59Z")

        with self.assertRaises(BuildError):
            publish_candidate(candidate, self.private_root, keep=5)

    def test_source_name_collisions_are_disambiguated_by_source_label(self):
        duplicate_proxy = self.reality_proxy("duplicate-node", "33333333-3333-4333-8333-333333333333")
        self.converter = FakeConverterClient({self.owner_url: (duplicate_proxy,), self.friend_url: (duplicate_proxy,)})
        self.local_loader = FakeLocalLoader(
            {
                self.airport_path: (
                    {
                        "name": "duplicate-node",
                        "type": "trojan",
                        "server": "airport.example.com",
                        "port": 443,
                        "password": "airport-password",
                    },
                ),
                self.home_path: (
                    {
                        "name": "duplicate-node",
                        "type": "trojan",
                        "server": "home.example.com",
                        "port": 443,
                        "password": "home-password",
                    },
                ),
            }
        )
        self.builder = self.make_builder()

        candidate = self.builder.build_candidate("owner", "op-collisions")
        text = candidate.files["balanced"].read_text(encoding="utf-8")

        self.assertIn("duplicate-node [3x-ui]", text)
        self.assertIn("duplicate-node [机场]", text)
        self.assertIn("duplicate-node [家庭]", text)

    def test_publish_prunes_to_five_successful_releases_and_preserves_references(self):
        sentinel = self.private_root / "reference-configs" / "2026-08-21" / "reference.yaml"
        sentinel.write_text("reference", encoding="utf-8")
        published = []
        for index in range(6):
            self.clock_value = self.clock_value + timedelta(minutes=1)
            published.append(self.publish_valid_owner_release("op-owner-%d" % index))

        history = list_history(self.private_root, "owner")

        self.assertEqual(len(history), 5)
        self.assertEqual([item.release_id for item in history], [item.release_id for item in reversed(published[1:])])
        self.assertFalse((self.private_root / "releases" / "owner" / published[0].release_id).exists())
        self.assertTrue(sentinel.exists())

    def test_list_history_ignores_tampered_releases(self):
        older = self.publish_valid_owner_release("op-owner-older")
        self.clock_value = self.clock_value + timedelta(minutes=1)
        newer = self.publish_valid_owner_release("op-owner-newer")
        older.files["balanced"].write_text("tampered", encoding="utf-8")

        history = list_history(self.private_root, "owner")

        self.assertEqual([item.release_id for item in history], [newer.release_id])

    def test_list_history_rejects_manifest_metadata_tampering(self):
        older = self.publish_valid_owner_release("op-owner-older")
        self.clock_value = self.clock_value + timedelta(minutes=1)
        newer = self.publish_valid_owner_release("op-owner-newer")
        self.tamper_manifest(older.path, created_at="2099-12-31T23:59:59Z")

        history = list_history(self.private_root, "owner")

        self.assertEqual([item.release_id for item in history], [newer.release_id])

    def test_list_history_rejects_unsafe_user_id(self):
        with self.assertRaises(BuildError):
            list_history(self.private_root, "../owner")

    def test_rollback_rejects_traversal_and_requires_matching_hashes(self):
        first = self.publish_valid_owner_release("op-owner-first")
        self.clock_value = self.clock_value + timedelta(minutes=1)
        second = self.publish_valid_owner_release("op-owner-second")
        second.files["balanced"].write_text("tampered", encoding="utf-8")

        with self.assertRaises(BuildError):
            rollback(self.private_root, "owner", "../escape")
        with self.assertRaises(BuildError):
            rollback(self.private_root, "owner", "op-owner-second")

        restored = rollback(self.private_root, "owner", "op-owner-first")

        self.assertEqual(restored.release_id, first.release_id)
        self.assertEqual(
            (self.private_root / "current" / "owner").resolve(),
            first.path.resolve(),
        )

    def test_rollback_rejects_manifest_metadata_tampering(self):
        first = self.publish_valid_owner_release("op-owner-first")
        self.clock_value = self.clock_value + timedelta(minutes=1)
        self.publish_valid_owner_release("op-owner-second")
        self.tamper_manifest(first.path, created_at="2099-12-31T23:59:59Z")

        with self.assertRaises(BuildError):
            rollback(self.private_root, "owner", "op-owner-first")

    def test_validator_receives_exact_private_source_locations(self):
        self.builder.build_candidate("owner", "op-owner")

        _text, source_urls, _reality = self.validator.calls[0]

        self.assertEqual(
            source_urls,
            (
                self.owner_url,
                str(self.airport_path),
                str(self.home_path),
            ),
        )
