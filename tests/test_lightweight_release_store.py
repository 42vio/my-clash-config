import hashlib
import json
import os
import re
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from clash_sub.domain import PreparedRelease
import clash_sub.release_store as release_store_module

try:
    from clash_sub.release_store import ReleaseStore, ReleaseStoreError
except ImportError:
    ReleaseStore = None
    ReleaseStoreError = RuntimeError


RELEASE_ID_RE = re.compile(r"^[0-9TZ-]+-[a-f0-9]{8}$")


class ReleaseStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.private_root = root / "private"
        self.public_root = root / "public"
        self.now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        self.suffixes = iter(
            (
                "00000001",
                "00000002",
                "00000003",
                "00000004",
                "00000005",
                "00000006",
                "00000007",
            )
        )
        self.store = self.make_store()
        self.member_bundle = {"standard": "proxies: []\n"}
        self.owner_bundle = {
            "balanced": "proxies: [balanced]\n",
            "standard": "proxies: [standard]\n",
            "privacy": "proxies: [privacy]\n",
        }

    def tearDown(self):
        self.tempdir.cleanup()

    def make_store(self):
        self.assertIsNotNone(ReleaseStore, "ReleaseStore is not implemented")
        return ReleaseStore(
            self.private_root,
            self.public_root,
            clock=lambda: self.now,
            suffix_factory=lambda: next(self.suffixes),
        )

    def prepare_member(self, text="proxies: []\n"):
        release = self.store.prepare(7, {"standard": text}, {"xui": "a" * 64})
        self.assertIsNotNone(release)
        return release

    def assert_prior_release_survives(self, release):
        self.assertEqual(self.store.current_release_id(7), release.release_id)
        self.assertEqual(
            tuple(item.release_id for item in self.store.history(7)),
            (release.release_id,),
        )
        self.assertEqual(self.store.verify_release(7, release.release_id), release)

    def assert_no_uncommitted_candidates(self):
        staging = self.private_root / "staging"
        if staging.exists():
            self.assertEqual(tuple(staging.iterdir()), ())
        public_client = self.public_root / "releases" / "7"
        if public_client.exists():
            self.assertFalse(any(path.name.startswith(".") for path in public_client.iterdir()))

    def test_creates_immutable_member_release_with_canonical_manifest(self):
        release = self.prepare_member()

        self.assertIsInstance(release, PreparedRelease)
        self.assertRegex(release.release_id, RELEASE_ID_RE)
        self.assertEqual(
            release.public_paths,
            {
                "standard": self.public_root
                / "releases"
                / "7"
                / release.release_id
                / "clash-standard.yaml"
            },
        )
        self.assertEqual(
            stat.S_IMODE((self.private_root / "staging").stat().st_mode),
            0o700,
        )
        self.assertEqual(
            stat.S_IMODE(release.manifest_path.stat().st_mode),
            0o600,
        )
        self.assertEqual(
            stat.S_IMODE(release.public_paths["standard"].stat().st_mode),
            0o640,
        )
        self.assertFalse((self.public_root / "current").exists())
        self.assertFalse((self.public_root / "current").is_symlink())

        manifest = json.loads(release.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(
            set(manifest),
            {
                "client_id",
                "created_at",
                "input_hashes",
                "output_hashes",
                "release_id",
                "schema_version",
                "variants",
            },
        )
        self.assertEqual(manifest["client_id"], 7)
        self.assertEqual(manifest["release_id"], release.release_id)
        self.assertEqual(manifest["variants"], ["standard"])
        self.assertEqual(manifest["input_hashes"], {"xui": "a" * 64})
        self.assertEqual(
            manifest["output_hashes"],
            {"standard": hashlib.sha256(b"proxies: []\n").hexdigest()},
        )
        self.assertNotIn("proxies", json.dumps(manifest))
        self.assertEqual(
            (release.manifest_path.parent / "manifest.sha256").read_text(encoding="utf-8"),
            hashlib.sha256(release.manifest_path.read_bytes()).hexdigest() + "\n",
        )

    def test_public_yaml_stays_private_through_staged_hash_verification_then_releases_group_readable(self):
        observed_modes = []
        real_verify = release_store_module._verify_staged_release

        def verify_with_mode_probe(private_stage, public_stage, manifest):
            observed_modes.extend(
                stat.S_IMODE((public_stage / ("clash-%s.yaml" % variant)).stat().st_mode)
                for variant in manifest["variants"]
            )
            return real_verify(private_stage, public_stage, manifest)

        with patch(
            "clash_sub.release_store._verify_staged_release",
            side_effect=verify_with_mode_probe,
        ):
            release = self.prepare_member()

        self.assertEqual(observed_modes, [0o600])
        self.assertEqual(stat.S_IMODE(release.public_paths["standard"].stat().st_mode), 0o640)

    def test_identical_bundle_returns_no_new_release_when_current_output_hashes_match(self):
        first = self.prepare_member()
        self.store.mark_current(7, first.release_id)

        second = self.store.prepare(7, self.member_bundle, {"xui": "b" * 64})

        self.assertIsNone(second)
        self.assertEqual(tuple(item.release_id for item in self.store.history(7)), (first.release_id,))

    def test_owner_release_requires_all_three_authorized_variants(self):
        with self.assertRaisesRegex(ReleaseStoreError, "variants"):
            self.store.prepare(
                7,
                {"balanced": "balanced\n", "standard": "standard\n"},
                {"xui": "a" * 64},
            )

        release = self.store.prepare(7, self.owner_bundle, {"xui": "a" * 64})

        self.assertEqual(tuple(release.public_paths), ("balanced", "standard", "privacy"))
        self.assertEqual(
            tuple(path.name for path in release.public_paths.values()),
            ("clash-balanced.yaml", "clash-standard.yaml", "clash-privacy.yaml"),
        )

    def test_rejects_unsafe_client_and_release_ids_before_path_construction(self):
        for unsafe_client_id in (0, -1, "7", True):
            with self.assertRaisesRegex(ReleaseStoreError, "client id"):
                self.store.prepare(unsafe_client_id, self.member_bundle, {"xui": "a" * 64})

        with self.assertRaisesRegex(ReleaseStoreError, "release id"):
            self.store.verify_release(7, "../escape")
        with self.assertRaisesRegex(ReleaseStoreError, "release id"):
            self.store.mark_current(7, "not-a-release")

    def test_rejects_symlinked_release_directory_and_manifest(self):
        release = self.prepare_member()
        outside = Path(self.tempdir.name) / "outside"
        outside.mkdir()
        private_release = release.manifest_path.parent
        renamed = private_release.with_name("saved")
        private_release.rename(renamed)
        private_release.symlink_to(outside, target_is_directory=True)

        with self.assertRaisesRegex(ReleaseStoreError, "release"):
            self.store.verify_release(7, release.release_id)

        private_release.unlink()
        renamed.rename(private_release)
        release.manifest_path.unlink()
        release.manifest_path.symlink_to(outside / "manifest.json")

        with self.assertRaisesRegex(ReleaseStoreError, "manifest"):
            self.store.verify_release(7, release.release_id)

    def test_rejects_symlinked_private_root_for_history_and_rollback_lookup(self):
        release = self.prepare_member()
        linked_private_root = Path(self.tempdir.name) / "linked-private"
        linked_private_root.symlink_to(self.private_root, target_is_directory=True)
        linked_store = ReleaseStore(linked_private_root, self.public_root)

        with self.assertRaisesRegex(ReleaseStoreError, "release path"):
            linked_store.verify_release(7, release.release_id)
        self.assertEqual(linked_store.history(7), ())

    def test_verify_release_requires_root_only_private_ancestor_modes(self):
        release = self.prepare_member()
        private_release = release.manifest_path.parent
        ancestors = (
            self.private_root,
            self.private_root / "releases",
            private_release.parent,
            private_release,
        )
        self.assertEqual(
            tuple(stat.S_IMODE(path.stat().st_mode) for path in ancestors),
            (0o700, 0o700, 0o700, 0o700),
        )
        os.chmod(private_release.parent, 0o750)

        with self.assertRaisesRegex(ReleaseStoreError, "permissions"):
            self.store.verify_release(7, release.release_id)

    def test_verify_release_rejects_symlinked_private_client_ancestor(self):
        release = self.prepare_member()
        client_root = release.manifest_path.parent.parent
        saved = client_root.with_name("saved-client")
        client_root.rename(saved)
        client_root.symlink_to(saved, target_is_directory=True)

        with self.assertRaisesRegex(ReleaseStoreError, "release path"):
            self.store.verify_release(7, release.release_id)

    def test_failed_prepare_leaves_prior_current_release_intact(self):
        first = self.prepare_member()
        self.store.mark_current(7, first.release_id)
        self.now += timedelta(seconds=1)

        with patch("clash_sub.release_store.os.replace", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(ReleaseStoreError, "prepare"):
                self.store.prepare(7, {"standard": "proxies: [new]\n"}, {"xui": "b" * 64})

        current = self.store.current_release_id(7)
        self.assertEqual(current, first.release_id)
        self.assertEqual(self.store.verify_release(7, current).public_paths, first.public_paths)

    def test_prepare_cleans_private_candidate_when_public_root_setup_fails(self):
        self.public_root.write_text("not a directory\n", encoding="utf-8")

        with self.assertRaises(ReleaseStoreError):
            self.prepare_member()

        self.assert_no_uncommitted_candidates()

    def test_prepare_cleans_private_candidate_when_public_stage_setup_fails(self):
        first = self.prepare_member()
        self.store.mark_current(7, first.release_id)
        upcoming_stage = (
            self.public_root
            / "releases"
            / "7"
            / ".2026-08-23T12-00-00Z-00000002.tmp"
        )
        upcoming_stage.mkdir()

        with self.assertRaises(ReleaseStoreError):
            self.store.prepare(7, {"standard": "proxies: [new]\n"}, {"xui": "b" * 64})

        self.assert_prior_release_survives(first)
        self.assertEqual(tuple((self.private_root / "staging").iterdir()), ())
        self.assertTrue(upcoming_stage.is_dir())

    def test_prepare_cleans_both_stages_when_staged_verification_fails(self):
        first = self.prepare_member()
        self.store.mark_current(7, first.release_id)

        with patch(
            "clash_sub.release_store._verify_staged_release",
            side_effect=ReleaseStoreError("verification failed"),
        ):
            with self.assertRaises(ReleaseStoreError):
                self.store.prepare(7, {"standard": "proxies: [new]\n"}, {"xui": "b" * 64})

        self.assert_prior_release_survives(first)
        self.assert_no_uncommitted_candidates()

    def test_prepare_cleans_published_public_candidate_when_private_publish_fails(self):
        first = self.prepare_member()
        self.store.mark_current(7, first.release_id)
        real_replace = os.replace

        def fail_private_publish(source, destination):
            if Path(source).parent == self.private_root / "staging":
                raise OSError("private publish failed")
            return real_replace(source, destination)

        with patch("clash_sub.release_store.os.replace", side_effect=fail_private_publish):
            with self.assertRaises(ReleaseStoreError):
                self.store.prepare(7, {"standard": "proxies: [new]\n"}, {"xui": "b" * 64})

        self.assert_prior_release_survives(first)
        self.assert_no_uncommitted_candidates()

    def test_prepare_cleans_published_candidates_when_final_verification_fails(self):
        first = self.prepare_member()
        self.store.mark_current(7, first.release_id)
        real_verify = self.store.verify_release

        def fail_new_release(client_id, release_id):
            if release_id != first.release_id:
                raise ReleaseStoreError("final verification failed")
            return real_verify(client_id, release_id)

        with patch.object(self.store, "verify_release", side_effect=fail_new_release):
            with self.assertRaises(ReleaseStoreError):
                self.store.prepare(7, {"standard": "proxies: [new]\n"}, {"xui": "b" * 64})

        self.assert_prior_release_survives(first)
        self.assert_no_uncommitted_candidates()

    def test_verify_release_and_history_fail_closed_on_digest_or_public_hash_tampering(self):
        release = self.prepare_member()
        digest = release.manifest_path.with_name("manifest.sha256")
        digest.write_text("0" * 64 + "\n", encoding="utf-8")

        with self.assertRaisesRegex(ReleaseStoreError, "digest"):
            self.store.verify_release(7, release.release_id)
        self.assertEqual(self.store.history(7), ())

        digest.write_text(
            hashlib.sha256(release.manifest_path.read_bytes()).hexdigest() + "\n",
            encoding="utf-8",
        )
        release.public_paths["standard"].write_text("tampered\n", encoding="utf-8")
        os.chmod(release.public_paths["standard"], 0o640)

        with self.assertRaisesRegex(ReleaseStoreError, "hash"):
            self.store.verify_release(7, release.release_id)
        self.assertEqual(self.store.history(7), ())

    def test_prunes_only_on_sixth_success_and_preserves_private_current_reference(self):
        releases = []
        for index in range(6):
            self.now += timedelta(seconds=1)
            releases.append(self.prepare_member("proxies: [%s]\n" % index))

        self.store.mark_current(7, releases[0].release_id)
        self.assertEqual(self.store.prune(7), (releases[1].release_id,))
        remaining = tuple(item.release_id for item in self.store.history(7))

        self.assertEqual(len(remaining), 5)
        self.assertIn(releases[0].release_id, remaining)
        self.assertNotIn(releases[1].release_id, remaining)
        self.assertEqual(self.store.current_release_id(7), releases[0].release_id)

    def test_prune_keeps_all_five_successful_releases(self):
        releases = []
        for index in range(5):
            self.now += timedelta(seconds=1)
            releases.append(self.prepare_member("proxies: [%s]\n" % index))

        self.assertEqual(self.store.prune(7), ())
        self.assertEqual(len(self.store.history(7)), 5)


if __name__ == "__main__":
    unittest.main()
