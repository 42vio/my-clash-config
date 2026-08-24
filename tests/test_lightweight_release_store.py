import hashlib
import json
import os
import re
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
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
        self.public_root.mkdir()
        os.chown(self.public_root, -1, os.getegid())
        os.chmod(self.public_root, 0o2750)
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

    def make_store(self, **kwargs):
        self.assertIsNotNone(ReleaseStore, "ReleaseStore is not implemented")
        options = {
            "expected_uid": os.geteuid(),
            "expected_public_gid": os.getegid(),
        }
        options.update(kwargs)
        return ReleaseStore(
            self.private_root,
            self.public_root,
            clock=lambda: self.now,
            suffix_factory=lambda: next(self.suffixes),
            **options,
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

    def emulate_public_directory_mode(self):
        real_directory_mode = release_store_module.directory_mode

        def directory_mode(path):
            path = Path(path)
            if path == self.public_root or self.public_root in path.parents:
                return 0o2750
            return real_directory_mode(path)

        return patch("clash_sub.release_store.directory_mode", side_effect=directory_mode)

    def test_write_file_syncs_completed_file_bytes_and_mode(self):
        path = Path(self.tempdir.name) / "release.yaml"
        opened_paths = {}
        fsynced_paths = []
        real_open = os.open
        real_fsync = os.fsync

        def record_open(target, flags, *args, **kwargs):
            descriptor = real_open(target, flags, *args, **kwargs)
            opened_paths[descriptor] = Path(target)
            return descriptor

        def record_fsync(descriptor):
            fsynced_paths.append(opened_paths.get(descriptor))
            return real_fsync(descriptor)

        with patch("clash_sub.release_store.os.open", side_effect=record_open), patch(
            "clash_sub.release_store.os.fsync", side_effect=record_fsync
        ):
            release_store_module._write_file(path, b"proxies: []\n", 0o600)

        self.assertEqual(path.read_bytes(), b"proxies: []\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(fsynced_paths, [path])

    def test_finalize_public_stage_syncs_final_yaml_metadata_before_stage_directory(self):
        stage = Path(self.tempdir.name) / "public-stage"
        stage.mkdir()
        public_yaml = stage / "clash-standard.yaml"
        public_yaml.write_bytes(b"proxies: []\n")
        os.chmod(public_yaml, 0o600)
        opened_paths = {}
        fsynced_paths = []
        real_open = os.open
        real_fsync = os.fsync

        def record_open(target, flags, *args, **kwargs):
            descriptor = real_open(target, flags, *args, **kwargs)
            opened_paths[descriptor] = Path(target)
            return descriptor

        def record_fsync(descriptor):
            fsynced_paths.append(opened_paths.get(descriptor))
            return real_fsync(descriptor)

        with patch("clash_sub.release_store.os.open", side_effect=record_open), patch(
            "clash_sub.release_store.os.fsync", side_effect=record_fsync
        ):
            release_store_module._finalize_public_stage(stage, ("standard",))

        self.assertEqual(stat.S_IMODE(public_yaml.stat().st_mode), 0o640)
        self.assertEqual(fsynced_paths, [public_yaml, stage])

    def test_publish_directory_syncs_each_rename_parent_after_replace(self):
        root = Path(self.tempdir.name)
        same_parent = root / "same-parent"
        source_parent = root / "source-parent"
        target_parent = root / "target-parent"
        for parent in (same_parent, source_parent, target_parent):
            parent.mkdir()
        cases = (
            (same_parent / ".candidate", same_parent / "release", (same_parent,)),
            (source_parent / "candidate", target_parent / "release", (source_parent, target_parent)),
        )

        for source, target, expected_parents in cases:
            with self.subTest(source=source, target=target):
                source.mkdir()
                opened_paths = {}
                events = []
                real_open = os.open
                real_fsync = os.fsync
                real_replace = os.replace

                def record_open(path, flags, *args, **kwargs):
                    descriptor = real_open(path, flags, *args, **kwargs)
                    opened_paths[descriptor] = Path(path)
                    return descriptor

                def record_fsync(descriptor):
                    events.append(("fsync", opened_paths.get(descriptor)))
                    return real_fsync(descriptor)

                def record_replace(source_path, target_path):
                    events.append(("replace", Path(source_path), Path(target_path)))
                    return real_replace(source_path, target_path)

                with patch("clash_sub.release_store.os.open", side_effect=record_open), patch(
                    "clash_sub.release_store.os.fsync", side_effect=record_fsync
                ), patch("clash_sub.release_store.os.replace", side_effect=record_replace):
                    release_store_module._publish_directory(source, target)

                self.assertTrue(target.is_dir())
                self.assertEqual(
                    events,
                    [("replace", source, target)]
                    + [("fsync", parent) for parent in expected_parents],
                )

    def test_new_nested_private_root_syncs_every_created_parent_entry(self):
        root = Path(self.tempdir.name)
        private_root = root / "new-parent" / "private"
        opened_paths = {}
        fsynced_paths = []
        real_open = os.open
        real_fsync = os.fsync

        def record_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            opened_paths[descriptor] = Path(path)
            return descriptor

        def record_fsync(descriptor):
            fsynced_paths.append(opened_paths.get(descriptor))
            return real_fsync(descriptor)

        with patch("clash_sub.release_store.os.open", side_effect=record_open), patch(
            "clash_sub.release_store.os.fsync", side_effect=record_fsync
        ):
            created = release_store_module._new_or_existing_directory(private_root, None, 0o700)

        self.assertEqual(created, private_root)
        self.assertTrue(private_root.is_dir())
        self.assertEqual(stat.S_IMODE(private_root.stat().st_mode), 0o700)
        self.assertEqual(fsynced_paths, [root, root / "new-parent"])

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
        observed_groups = []
        real_verify = release_store_module._verify_staged_release

        def verify_with_mode_probe(private_stage, public_stage, manifest, *_):
            observed_modes.extend(
                stat.S_IMODE((public_stage / ("clash-%s.yaml" % variant)).stat().st_mode)
                for variant in manifest["variants"]
            )
            observed_groups.extend(
                (public_stage / ("clash-%s.yaml" % variant)).stat().st_gid
                for variant in manifest["variants"]
            )
            return real_verify(private_stage, public_stage, manifest)

        with patch(
            "clash_sub.release_store._verify_staged_release",
            side_effect=verify_with_mode_probe,
        ):
            release = self.prepare_member()

        self.assertEqual(observed_modes, [0o600])
        self.assertEqual(observed_groups, [self.public_root.stat().st_gid])
        self.assertEqual(stat.S_IMODE(release.public_paths["standard"].stat().st_mode), 0o640)

    def test_public_release_descendants_preserve_nginx_group_and_setgid_access(self):
        release = self.prepare_member()
        release_root = release.public_paths["standard"].parent
        directories = (
            self.public_root,
            self.public_root / "releases",
            release_root.parent,
            release_root,
        )
        public_gid = self.public_root.stat().st_gid

        self.assertEqual(
            tuple(stat.S_IMODE(path.stat().st_mode) for path in directories),
            (0o2750, 0o2750, 0o2750, 0o2750),
        )
        self.assertEqual(tuple(path.stat().st_gid for path in directories), (public_gid,) * 4)
        self.assertEqual(release.public_paths["standard"].stat().st_gid, public_gid)
        self.assertEqual(stat.S_IMODE(release.public_paths["standard"].stat().st_mode), 0o640)

    def test_verify_release_rejects_public_ancestors_without_setgid_group_access(self):
        release = self.prepare_member()
        release_root = release.public_paths["standard"].parent
        directories = (
            self.public_root,
            self.public_root / "releases",
            release_root.parent,
            release_root,
        )

        for path in directories:
            with self.subTest(path=path.name):
                os.chmod(path, 0o750)
                with self.assertRaises(ReleaseStoreError):
                    self.store.verify_release(7, release.release_id)
                os.chmod(path, 0o2750)

    def test_verify_release_rejects_public_ancestor_with_different_group(self):
        release = self.prepare_member()
        client_root = release.public_paths["standard"].parent.parent
        public_gid = self.public_root.stat().st_gid
        alternate_gid = next((gid for gid in os.getgroups() if gid != public_gid), None)
        if alternate_gid is None:
            self.skipTest("no alternate supplementary group available")
        try:
            os.chown(client_root, -1, alternate_gid)
            os.chmod(client_root, 0o2750)
        except PermissionError:
            self.skipTest("cannot change test directory group")

        with self.assertRaises(ReleaseStoreError):
            self.store.verify_release(7, release.release_id)

    def test_explicit_production_ownership_expectations_reject_wrong_root_owner_and_www_data_group(self):
        impossible_owner = os.geteuid() + 1
        wrong_owner_store = self.make_store(
            expected_uid=impossible_owner,
            expected_public_gid=os.getegid(),
        )
        with self.assertRaises(ReleaseStoreError):
            wrong_owner_store.prepare(7, self.member_bundle, {"xui": "a" * 64})

        wrong_group_store = self.make_store(
            expected_uid=os.geteuid(),
            expected_public_gid=os.getegid() + 1,
        )
        with self.assertRaises(ReleaseStoreError):
            wrong_group_store.prepare(7, self.member_bundle, {"xui": "a" * 64})

    def test_verify_release_rejects_a_hard_linked_public_yaml(self):
        release = self.prepare_member()
        linked = release.public_paths["standard"].with_name("linked.yaml")
        os.link(release.public_paths["standard"], linked)

        with self.assertRaisesRegex(ReleaseStoreError, "release"):
            self.store.verify_release(7, release.release_id)

    def test_space_preflight_rejects_before_creating_private_staging_or_public_release_entries(self):
        before_public = tuple(self.public_root.iterdir())
        no_space = SimpleNamespace(f_frsize=1, f_bavail=0)

        with patch("clash_sub.release_store.os.statvfs", return_value=no_space):
            with self.assertRaisesRegex(ReleaseStoreError, "space"):
                self.prepare_member()

        self.assertFalse(self.private_root.exists())
        self.assertEqual(tuple(self.public_root.iterdir()), before_public)

    def test_space_preflight_counts_headroom_once_on_a_shared_filesystem_at_the_exact_boundary(self):
        private = Path("/private")
        public = Path("/public")
        exact_required_bytes = 1376258
        space = SimpleNamespace(f_frsize=1, f_bavail=exact_required_bytes)

        with patch(
            "clash_sub.release_store._existing_filesystem_ancestor",
            return_value=(private, 1),
        ), patch("clash_sub.release_store.os.statvfs", return_value=space) as statvfs:
            release_store_module._preflight_space(
                private,
                public,
                {"standard": "x"},
                (private / "state.json", private / "current"),
            )

        self.assertEqual(statvfs.call_count, 1)

    def test_space_preflight_checks_each_distinct_filesystem_without_double_counting_headroom(self):
        private = Path("/private")
        public = Path("/public")
        space = SimpleNamespace(f_frsize=1, f_bavail=2_000_000)

        def filesystem(path):
            return (public, 2) if Path(path) == public else (private, 1)

        with patch(
            "clash_sub.release_store._existing_filesystem_ancestor", side_effect=filesystem
        ), patch("clash_sub.release_store.os.statvfs", return_value=space) as statvfs:
            release_store_module._preflight_space(
                private,
                public,
                {"standard": "x"},
                (private / "state.json", private / "current"),
            )

        self.assertEqual(statvfs.call_count, 2)

    def test_identical_bundle_returns_no_new_release_when_current_output_hashes_match(self):
        first = self.prepare_member()
        self.store.mark_current(7, first.release_id)

        second = self.store.prepare(7, self.member_bundle, {"xui": "b" * 64})

        self.assertIsNone(second)
        self.assertEqual(tuple(item.release_id for item in self.store.history(7)), (first.release_id,))

    def test_current_artifact_is_root_only_marker_installed_equivalently_by_mark_current(self):
        release = self.prepare_member("token: secret-value\n")

        marker_path, marker_bytes, marker_mode = self.store.current_artifact(
            7, release.release_id
        )

        self.assertTrue(marker_path.is_absolute())
        self.assertEqual(marker_path, (self.private_root / "current" / "7").absolute())
        self.assertEqual(marker_bytes, (release.release_id + "\n").encode("ascii"))
        self.assertEqual(marker_mode, 0o600)
        self.assertNotIn(b"secret-value", marker_bytes)
        self.assertNotIn(str(self.private_root).encode("utf-8"), marker_bytes)
        self.assertNotIn(b"/", marker_bytes)
        self.assertFalse(marker_path.exists())

        self.store.mark_current(7, release.release_id)

        self.assertFalse(marker_path.is_symlink())
        self.assertTrue(marker_path.is_file())
        self.assertEqual(marker_path.read_bytes(), marker_bytes)
        self.assertEqual(stat.S_IMODE(marker_path.stat().st_mode), marker_mode)
        self.assertEqual(self.store.current_release_id(7), release.release_id)
        self.assertFalse((self.public_root / "current").exists())
        self.assertFalse((self.public_root / "current").is_symlink())

    def test_current_release_id_rejects_marker_and_private_ancestor_drift(self):
        release = self.prepare_member()
        self.store.mark_current(7, release.release_id)
        marker = self.private_root / "current" / "7"
        current_root = marker.parent

        marker.unlink()
        marker.symlink_to(release.manifest_path.parent, target_is_directory=True)
        with self.assertRaisesRegex(ReleaseStoreError, "current release reference"):
            self.store.current_release_id(7)

        marker.unlink()
        marker.write_bytes((release.release_id + "\n").encode("ascii"))
        os.chmod(marker, 0o640)
        with self.assertRaisesRegex(ReleaseStoreError, "current release reference"):
            self.store.current_release_id(7)

        for contents in (
            release.release_id.encode("ascii"),
            (release.release_id + "\n\n").encode("ascii"),
            b"../escape\n",
        ):
            with self.subTest(contents=contents):
                marker.write_bytes(contents)
                os.chmod(marker, 0o600)
                with self.assertRaisesRegex(ReleaseStoreError, "current release reference"):
                    self.store.current_release_id(7)

        marker.write_bytes((release.release_id + "\n").encode("ascii"))
        os.chmod(marker, 0o600)
        os.chmod(current_root, 0o750)
        with self.assertRaisesRegex(ReleaseStoreError, "permissions"):
            self.store.current_release_id(7)

    def test_current_artifact_verifies_release_integrity(self):
        release = self.prepare_member()
        release.public_paths["standard"].write_text("tampered\n", encoding="utf-8")
        os.chmod(release.public_paths["standard"], 0o640)

        with self.assertRaisesRegex(ReleaseStoreError, "hash"):
            self.store.current_artifact(7, release.release_id)

    def test_mark_current_failure_preserves_prior_regular_marker(self):
        current = self.prepare_member()
        self.store.mark_current(7, current.release_id)
        self.now += timedelta(seconds=1)
        candidate = self.prepare_member("proxies: [candidate]\n")
        marker = self.private_root / "current" / "7"
        original = marker.read_bytes()

        with patch("clash_sub.release_store.os.replace", side_effect=OSError("disk error")):
            with self.assertRaisesRegex(ReleaseStoreError, "mark current"):
                self.store.mark_current(7, candidate.release_id)

        self.assertEqual(marker.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(marker.stat().st_mode), 0o600)
        self.assertEqual(self.store.current_release_id(7), current.release_id)
        self.assertFalse((marker.parent / ".7.tmp").exists())

    def test_discard_unreferenced_removes_only_candidate_pair_and_history_entry(self):
        current = self.prepare_member()
        self.store.mark_current(7, current.release_id)
        self.now += timedelta(seconds=1)
        candidate = self.prepare_member("proxies: [candidate]\n")
        candidate_private = candidate.manifest_path.parent
        candidate_public = candidate.public_paths["standard"].parent

        self.store.discard_unreferenced(7, candidate.release_id)

        self.assertFalse(candidate_private.exists())
        self.assertFalse(candidate_private.is_symlink())
        self.assertFalse(candidate_public.exists())
        self.assertFalse(candidate_public.is_symlink())
        self.assertEqual(
            tuple(item.release_id for item in self.store.history(7)),
            (current.release_id,),
        )
        self.assert_prior_release_survives(current)

    def test_discard_unreferenced_refuses_current_and_unsafe_ids(self):
        current = self.prepare_member()
        self.store.mark_current(7, current.release_id)

        with self.assertRaisesRegex(ReleaseStoreError, "failed to discard release"):
            self.store.discard_unreferenced(7, current.release_id)
        with self.assertRaisesRegex(ReleaseStoreError, "client id"):
            self.store.discard_unreferenced(0, current.release_id)
        with self.assertRaisesRegex(ReleaseStoreError, "release id"):
            self.store.discard_unreferenced(7, "../escape")

        self.assert_prior_release_survives(current)

    def test_discard_unreferenced_rejects_tampered_target_without_partial_removal(self):
        current = self.prepare_member()
        self.store.mark_current(7, current.release_id)
        self.now += timedelta(seconds=1)
        candidate = self.prepare_member("proxies: [candidate]\n")
        candidate_private = candidate.manifest_path.parent
        candidate_public = candidate.public_paths["standard"].parent
        public_yaml = candidate.public_paths["standard"]
        original_yaml = public_yaml.read_bytes()

        public_yaml.write_bytes(b"tampered\n")
        os.chmod(public_yaml, 0o640)
        with self.assertRaisesRegex(ReleaseStoreError, "failed to discard release"):
            self.store.discard_unreferenced(7, candidate.release_id)

        self.assertTrue(candidate_private.is_dir())
        self.assertTrue(candidate_public.is_dir())
        self.assert_prior_release_survives(current)

        public_yaml.write_bytes(original_yaml)
        os.chmod(public_yaml, 0o640)
        saved_private = candidate_private.with_name("saved-candidate")
        candidate_private.rename(saved_private)
        candidate_private.symlink_to(saved_private, target_is_directory=True)

        with self.assertRaisesRegex(ReleaseStoreError, "failed to discard release"):
            self.store.discard_unreferenced(7, candidate.release_id)

        self.assertTrue(candidate_private.is_symlink())
        self.assertTrue(saved_private.is_dir())
        self.assertTrue(candidate_public.is_dir())
        self.assert_prior_release_survives(current)

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
        self.public_root.rmdir()
        self.public_root.write_text("not a directory\n", encoding="utf-8")

        with self.assertRaises(ReleaseStoreError):
            self.prepare_member()

        self.assert_no_uncommitted_candidates()

    def test_prepare_requires_deployment_created_public_root(self):
        self.public_root.rmdir()

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

    def test_prepare_cleans_candidates_after_public_rename_parent_fsync_failure(self):
        release_id = "2026-08-23T12-00-00Z-00000002"
        public_candidate = self.public_root / "releases" / "7" / release_id
        private_candidate = self.private_root / "releases" / "7" / release_id
        opened_paths = {}
        real_open = os.open
        real_fsync = os.fsync

        def record_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            opened_paths[descriptor] = Path(path)
            return descriptor

        def fail_public_parent_fsync(descriptor):
            if opened_paths.get(descriptor) == public_candidate.parent and public_candidate.is_dir():
                raise OSError("public publish fsync failed")
            return real_fsync(descriptor)

        with self.emulate_public_directory_mode(), patch(
            "clash_sub.release_store.os.open", side_effect=record_open
        ), patch("clash_sub.release_store.os.fsync", side_effect=fail_public_parent_fsync):
            first = self.prepare_member()
            self.store.mark_current(7, first.release_id)

            with self.assertRaisesRegex(ReleaseStoreError, "prepare"):
                self.store.prepare(7, {"standard": "proxies: [new]\n"}, {"xui": "b" * 64})

            self.assertFalse(public_candidate.exists())
            self.assertFalse(private_candidate.exists())
            self.assert_no_uncommitted_candidates()
            self.assert_prior_release_survives(first)

    def test_prepare_cleans_candidates_after_private_rename_parent_fsync_failure(self):
        release_id = "2026-08-23T12-00-00Z-00000002"
        public_candidate = self.public_root / "releases" / "7" / release_id
        private_candidate = self.private_root / "releases" / "7" / release_id
        opened_paths = {}
        real_open = os.open
        real_fsync = os.fsync

        def record_open(path, flags, *args, **kwargs):
            descriptor = real_open(path, flags, *args, **kwargs)
            opened_paths[descriptor] = Path(path)
            return descriptor

        def fail_private_parent_fsync(descriptor):
            if opened_paths.get(descriptor) == private_candidate.parent and private_candidate.is_dir():
                raise OSError("private publish fsync failed")
            return real_fsync(descriptor)

        with self.emulate_public_directory_mode(), patch(
            "clash_sub.release_store.os.open", side_effect=record_open
        ), patch("clash_sub.release_store.os.fsync", side_effect=fail_private_parent_fsync):
            first = self.prepare_member()
            self.store.mark_current(7, first.release_id)

            with self.assertRaisesRegex(ReleaseStoreError, "prepare"):
                self.store.prepare(7, {"standard": "proxies: [new]\n"}, {"xui": "b" * 64})

            self.assertFalse(public_candidate.exists())
            self.assertFalse(private_candidate.exists())
            self.assert_no_uncommitted_candidates()
            self.assert_prior_release_survives(first)

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
