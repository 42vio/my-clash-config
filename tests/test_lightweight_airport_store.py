"""Dual-file airport transaction: provider bytes plus private source record."""

import json
import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from clash_sub import airport_store
from clash_sub.airport_source import (
    AIRPORT_SOURCE_FILENAME,
    AirportSource,
    parse_source,
    serialize_source,
)
from clash_sub.airport_store import AirportStore, AirportStoreError
from clash_sub.domain import Traffic

PROVIDER_BYTES = (
    b"proxies:\n"
    b"- {name: Node, type: trojan, server: airport.example, port: 443, password: secret}\n"
)
OLD_BYTES = PROVIDER_BYTES.replace(b"airport.example", b"old-airport.example")
NEW_BYTES = PROVIDER_BYTES.replace(b"airport.example", b"new-airport.example")

OLD_SOURCE = AirportSource(
    "https://airport.example/old", Traffic(1, 2, 3, 4), 1788192000
)
NEW_SOURCE = AirportSource("https://airport.example/new", None, 1788192100)

JOURNAL_NAME = "airport-transaction.json"


class AirportStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private_root = Path(temporary.name) / "private"
        self.private_root.mkdir(mode=0o700)
        os.chmod(self.private_root, 0o700)
        self.public_root = Path(temporary.name) / "public"
        self.provider_directory = self.public_root / "provider"
        self.provider_directory.mkdir(parents=True)
        self.store = AirportStore(self.private_root, self.public_root)

    def source_path(self):
        return self.private_root / AIRPORT_SOURCE_FILENAME

    def journal_path(self):
        return self.private_root / JOURNAL_NAME

    def provider_names(self):
        return sorted(entry.name for entry in self.provider_directory.iterdir())

    def private_names(self):
        return sorted(entry.name for entry in self.private_root.iterdir())

    def raw_source(self):
        return parse_source(self.source_path().read_bytes())

    def write_journal(self, payload):
        self.journal_path().write_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        os.chmod(self.journal_path(), 0o600)

    def test_path_is_always_the_stable_provider_file(self):
        self.assertEqual(
            self.store.path,
            self.public_root / "provider" / "AmyTelecom.yaml",
        )

    def test_replace_atomically_publishes_the_exact_bytes_and_record(self):
        path = self.store.replace(PROVIDER_BYTES, OLD_SOURCE)
        self.assertEqual(path, self.store.path)
        self.assertEqual(path.name, "AmyTelecom.yaml")
        self.assertEqual(path.read_bytes(), PROVIDER_BYTES)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        record = self.source_path()
        details = record.lstat()
        expected_uid = 0 if os.geteuid() == 0 else os.geteuid()
        self.assertFalse(record.is_symlink())
        self.assertTrue(stat.S_ISREG(details.st_mode))
        self.assertEqual(stat.S_IMODE(details.st_mode), 0o600)
        self.assertEqual(details.st_nlink, 1)
        self.assertEqual(details.st_uid, expected_uid)
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), {
            "schema_version": 1,
            "source_url": "https://airport.example/old",
            "traffic": {"upload": 1, "download": 2, "total": 3, "expire": 4},
            "last_success": 1788192000,
        })
        self.assertEqual(self.store.read_source(), OLD_SOURCE)

    def test_second_replacement_switches_both_files(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertEqual(self.store.read(), NEW_BYTES)
        self.assertEqual(self.store.read_source(), NEW_SOURCE)
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])

    def test_read_returns_exact_bytes_of_the_current_provider(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        self.assertEqual(self.store.read(), OLD_BYTES)

    def test_read_requires_an_existing_provider(self):
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()

    def test_read_source_reports_missing_record(self):
        with self.assertRaises(AirportStoreError) as caught:
            self.store.read_source()
        self.assertEqual(caught.exception.code, "airport_source_missing")

    def test_read_source_rejects_invalid_record_content(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        self.source_path().write_bytes(b"{not json")
        os.chmod(self.source_path(), 0o600)
        with self.assertRaises(AirportStoreError) as caught:
            self.store.read_source()
        self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_private_root_must_satisfy_the_root_only_contract(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        os.chmod(self.private_root, 0o755)
        with self.assertRaises(AirportStoreError) as read_caught:
            self.store.read_source()
        self.assertEqual(read_caught.exception.code, "airport_source_invalid")
        with self.assertRaises(AirportStoreError) as replace_caught:
            self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertEqual(replace_caught.exception.code, "airport_source_invalid")
        os.chmod(self.private_root, 0o700)
        self.assertEqual(self.store.read(), OLD_BYTES)

    def test_symlinked_private_root_is_rejected(self):
        real = self.private_root.parent / "real-private"
        real.mkdir(mode=0o700)
        os.chmod(real, 0o700)
        moved = self.private_root.parent / "moved-private"
        self.private_root.rename(moved)
        self.private_root.symlink_to(real)
        store = AirportStore(self.private_root, self.public_root)
        with self.assertRaises(AirportStoreError) as caught:
            store.replace(PROVIDER_BYTES, OLD_SOURCE)
        self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_missing_private_root_is_rejected_for_source_operations(self):
        absent = self.private_root.parent / "absent-private"
        store = AirportStore(absent, self.public_root)
        with self.assertRaises(AirportStoreError) as caught:
            store.read_source()
        self.assertEqual(caught.exception.code, "airport_source_invalid")

    def test_failed_replacement_keeps_previous_provider_and_cleans_up(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        with patch.object(airport_store, "_os_replace", side_effect=OSError("injected")):
            with self.assertRaises(AirportStoreError) as caught:
                self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assertEqual(self.store.read(), OLD_BYTES)
        self.assertEqual(self.store.read_source(), OLD_SOURCE)
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])

    def test_empty_input_is_rejected(self):
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(b"", OLD_SOURCE)
        self.assertFalse(self.store.path.exists())
        self.assertFalse(self.source_path().exists())

    def test_non_bytes_input_is_rejected(self):
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(PROVIDER_BYTES.decode("utf-8"), OLD_SOURCE)
        self.assertFalse(self.store.path.exists())

    def test_oversized_input_is_rejected(self):
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(b"x" * (airport_store.MAX_PROVIDER_BYTES + 1), OLD_SOURCE)
        self.assertFalse(self.store.path.exists())

    def test_non_source_record_is_rejected(self):
        for invalid in ("not a source", None, 123):
            with self.assertRaises(AirportStoreError) as caught:
                self.store.replace(PROVIDER_BYTES, invalid)
            self.assertEqual(caught.exception.code, "airport_source_invalid")
        self.assertEqual(self.provider_names(), [])
        self.assertFalse(self.source_path().exists())

    def test_invalid_source_record_is_rejected_without_disk_changes(self):
        invalid = AirportSource("", Traffic(1, 2, 3, 4), 5)
        with self.assertRaises(AirportStoreError) as caught:
            self.store.replace(PROVIDER_BYTES, invalid)
        self.assertEqual(caught.exception.code, "airport_source_invalid")
        self.assertEqual(self.provider_names(), [])
        self.assertFalse(self.source_path().exists())

    def test_oversized_stored_provider_is_rejected_on_read(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        target = self.store.path
        target.unlink()
        target.write_bytes(b"x" * (airport_store.MAX_PROVIDER_BYTES + 1))
        os.chmod(target, 0o640)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()

    def test_symlink_target_is_rejected_for_read_and_replace(self):
        outside = self.private_root.parent / "outside.yaml"
        outside.write_bytes(PROVIDER_BYTES)
        self.store.path.symlink_to(outside)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertTrue(self.store.path.is_symlink())
        self.assertEqual(outside.read_bytes(), PROVIDER_BYTES)

    def test_directory_target_is_rejected(self):
        self.store.path.mkdir()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertTrue(self.store.path.is_dir())

    def test_hard_linked_provider_is_rejected_for_read_and_replace(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        link = self.private_root.parent / "hardlink.yaml"
        os.link(self.store.path, link)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertEqual(self.store.path.read_bytes(), OLD_BYTES)

    def test_wrong_mode_provider_is_rejected_for_read_and_replace(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        self.store.path.chmod(0o644)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertEqual(self.store.path.read_bytes(), OLD_BYTES)

    def test_unsafe_existing_source_file_blocks_replacement(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        os.chmod(self.source_path(), 0o644)
        with self.assertRaises(AirportStoreError) as caught:
            self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertEqual(caught.exception.code, "airport_source_invalid")
        os.chmod(self.source_path(), 0o600)
        self.assertEqual(self.store.read(), OLD_BYTES)
        self.assertEqual(self.store.read_source(), OLD_SOURCE)
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])

    def test_symlinked_existing_source_file_blocks_replacement(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)
        record = self.source_path().read_bytes()
        self.source_path().unlink()
        outside = self.private_root.parent / "outside-source.json"
        outside.write_bytes(record)
        self.source_path().symlink_to(outside)
        with self.assertRaises(AirportStoreError) as caught:
            self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertEqual(caught.exception.code, "airport_source_invalid")
        self.assertEqual(self.store.read(), OLD_BYTES)

    def test_missing_provider_directory_is_rejected(self):
        store = AirportStore(self.private_root, self.private_root.parent / "absent")
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            store.replace(PROVIDER_BYTES, OLD_SOURCE)

    def test_symlinked_provider_directory_is_rejected(self):
        real = self.private_root.parent / "real-provider"
        real.mkdir()
        (real / "ignored.yaml").write_bytes(PROVIDER_BYTES)
        self.public_root.mkdir(parents=True, exist_ok=True)
        (self.public_root / "provider").rmdir()
        (self.public_root / "provider").symlink_to(real)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, NEW_SOURCE)
        self.assertEqual(sorted(entry.name for entry in real.iterdir()), ["ignored.yaml"])

    def test_errors_never_expose_paths_or_document_values(self):
        self.store.replace(OLD_BYTES, OLD_SOURCE)

        messages = []
        with patch.object(
            airport_store, "_os_replace", side_effect=OSError(str(self.private_root.parent))
        ):
            with self.assertRaises(AirportStoreError) as caught:
                self.store.replace(NEW_BYTES, NEW_SOURCE)
            self.assertEqual(caught.exception.code, "airport_provider_write_failed")
            messages.append(str(caught.exception))
        self.store.path.unlink()
        with self.assertRaises(AirportStoreError) as caught:
            self.store.read()
        self.assertEqual(caught.exception.code, "airport_provider_invalid")
        messages.append(str(caught.exception))
        self.source_path().write_bytes(b"{not json")
        with self.assertRaises(AirportStoreError) as caught:
            self.store.read_source()
        self.assertEqual(caught.exception.code, "airport_source_invalid")
        messages.append(str(caught.exception))
        for message in messages:
            self.assertNotIn(str(self.private_root.parent), message)
            self.assertNotIn("new-airport.example", message)
            self.assertNotIn("airport.example/old", message)
            self.assertNotIn("1788192000", message)


class AirportTransactionRecoveryTests(unittest.TestCase):
    """Crash at any stage; recovery must yield the old or the new pair."""

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.private_root = Path(temporary.name) / "private"
        self.private_root.mkdir(mode=0o700)
        os.chmod(self.private_root, 0o700)
        self.public_root = Path(temporary.name) / "public"
        self.provider_directory = self.public_root / "provider"
        self.provider_directory.mkdir(parents=True)
        self.store = AirportStore(self.private_root, self.public_root)
        self.store.replace(OLD_BYTES, OLD_SOURCE)

    def source_path(self):
        return self.private_root / AIRPORT_SOURCE_FILENAME

    def journal_path(self):
        return self.private_root / JOURNAL_NAME

    def provider_names(self):
        return sorted(entry.name for entry in self.provider_directory.iterdir())

    def private_names(self):
        return sorted(entry.name for entry in self.private_root.iterdir())

    def raw_pair(self):
        return (self.store.path.read_bytes(), parse_source(self.source_path().read_bytes()))

    def write_journal(self, payload):
        self.journal_path().write_bytes(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        os.chmod(self.journal_path(), 0o600)

    def assert_old_pair(self):
        self.assertEqual(self.raw_pair(), (OLD_BYTES, OLD_SOURCE))

    def assert_new_pair(self):
        self.assertEqual(self.raw_pair(), (NEW_BYTES, NEW_SOURCE))

    def replace_with_new(self):
        return self.store.replace(NEW_BYTES, NEW_SOURCE)

    def failing_replaces(self, fail_on):
        real = airport_store._os_replace
        calls = {"count": 0}

        def flaky(source, target):
            calls["count"] += 1
            if calls["count"] == fail_on:
                raise OSError("injected")
            return real(source, target)

        return patch.object(airport_store, "_os_replace", side_effect=flaky)

    def failing_fsyncs(self, fail_on):
        real = airport_store.os.fsync
        calls = {"count": 0}

        def flaky(descriptor):
            calls["count"] += 1
            if calls["count"] == fail_on:
                raise OSError("injected")
            return real(descriptor)

        return patch.object(airport_store, "_os_fsync", side_effect=flaky)

    def test_candidate_write_failure_keeps_the_old_pair(self):
        with patch.object(airport_store, "_os_write", side_effect=OSError("injected")):
            with self.assertRaises(AirportStoreError) as caught:
                self.replace_with_new()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assertFalse(self.journal_path().exists())
        self.assert_old_pair()
        self.store.recover()
        self.assert_old_pair()
        self.store.recover()
        self.assert_old_pair()

    def test_candidate_fsync_failure_keeps_the_old_pair(self):
        # fsync call 2 is the source candidate's durability barrier.
        with self.failing_fsyncs(fail_on=2):
            with self.assertRaises(AirportStoreError) as caught:
                self.replace_with_new()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])
        self.assert_old_pair()
        self.store.recover()
        self.assert_old_pair()

    def test_journal_write_failure_cleans_up_all_candidates(self):
        # replace call 1 is the journal's own rename into place.
        with self.failing_replaces(fail_on=1):
            with self.assertRaises(AirportStoreError) as caught:
                self.replace_with_new()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])
        self.assert_old_pair()
        self.store.recover()
        self.assert_old_pair()

    def test_first_target_replace_failure_recovers_to_the_new_pair(self):
        # replace call 2 is the provider switch; the journal is already durable.
        with self.failing_replaces(fail_on=2):
            with self.assertRaises(AirportStoreError) as caught:
                self.replace_with_new()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assertTrue(self.journal_path().exists())
        self.assert_old_pair()
        self.store.recover()
        self.assert_new_pair()
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])
        self.store.recover()
        self.assert_new_pair()
        self.assertEqual(self.store.read(), NEW_BYTES)
        self.assertEqual(self.store.read_source(), NEW_SOURCE)

    def test_second_target_replace_failure_recovers_to_the_new_pair(self):
        # replace call 3 is the source switch; the provider already switched,
        # so the crash leaves a mixed pair that recovery rolls forward.
        with self.failing_replaces(fail_on=3):
            with self.assertRaises(AirportStoreError) as caught:
                self.replace_with_new()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assertTrue(self.journal_path().exists())
        self.assertEqual(self.store.path.read_bytes(), NEW_BYTES)
        self.assertEqual(self.raw_pair()[1], OLD_SOURCE)
        self.store.recover()
        self.assert_new_pair()
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])
        self.store.recover()
        self.assert_new_pair()

    def test_directory_fsync_failure_is_recoverable_and_idempotent(self):
        # fsync call 5 is the first directory sync after both switches.
        with self.failing_fsyncs(fail_on=5):
            with self.assertRaises(AirportStoreError) as caught:
                self.replace_with_new()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assert_new_pair()
        self.assertTrue(self.journal_path().exists())
        self.store.recover()
        self.assert_new_pair()
        self.assertFalse(self.journal_path().exists())
        self.store.recover()
        self.assert_new_pair()

    def test_journal_cleanup_failure_is_recoverable_and_idempotent(self):
        with patch.object(airport_store, "_os_unlink", side_effect=OSError("injected")):
            with self.assertRaises(AirportStoreError) as caught:
                self.replace_with_new()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assert_new_pair()
        self.assertTrue(self.journal_path().exists())
        self.store.recover()
        self.assert_new_pair()
        self.assertFalse(self.journal_path().exists())
        self.store.recover()
        self.assert_new_pair()

    def test_read_operations_complete_a_pending_transaction_first(self):
        with self.failing_replaces(fail_on=3):
            with self.assertRaises(AirportStoreError):
                self.replace_with_new()
        self.assertTrue(self.journal_path().exists())
        self.assertEqual(self.store.read(), NEW_BYTES)
        self.assertEqual(self.store.read_source(), NEW_SOURCE)
        self.assertFalse(self.journal_path().exists())

    def test_corrupt_journal_with_leftover_candidates_is_rejected(self):
        self.journal_path().write_bytes(b"{corrupt")
        os.chmod(self.journal_path(), 0o600)
        leftover = self.provider_directory / ".AmyTelecom.yaml.leftover"
        leftover.write_bytes(NEW_BYTES)
        with self.assertRaises(AirportStoreError) as caught:
            self.store.recover()
        self.assertEqual(caught.exception.code, "airport_source_invalid")
        self.assertTrue(self.journal_path().exists())

    def test_corrupt_journal_without_leftovers_is_discarded(self):
        self.journal_path().write_bytes(b"{corrupt")
        os.chmod(self.journal_path(), 0o600)
        self.store.recover()
        self.assertFalse(self.journal_path().exists())
        self.assert_old_pair()
        self.store.recover()
        self.assert_old_pair()

    def test_symlinked_journal_is_discarded(self):
        self.journal_path().unlink(missing_ok=True)
        outside = self.private_root.parent / "outside-journal.json"
        outside.write_text("{}", encoding="utf-8")
        self.journal_path().symlink_to(outside)
        self.store.recover()
        self.assertFalse(self.journal_path().exists())
        self.assert_old_pair()

    def test_journal_with_escaping_names_is_discarded(self):
        self.write_journal(
            {"schema_version": 1, "provider": "../outside.yaml", "source": "x"}
        )
        self.store.recover()
        self.assertFalse(self.journal_path().exists())
        self.assert_old_pair()

    def test_consumed_candidates_with_missing_source_target_is_reported(self):
        self.write_journal(
            {
                "schema_version": 1,
                "provider": ".AmyTelecom.yaml.consumed",
                "source": ".airport-source.json.consumed",
            }
        )
        self.source_path().unlink()
        with self.assertRaises(AirportStoreError) as caught:
            self.store.recover()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")

    def test_recovery_without_journal_is_a_noop(self):
        self.store.recover()
        self.assert_old_pair()
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])

    def test_manual_journal_rolls_a_complete_pair_forward(self):
        # A durable journal plus both candidates is the pre-switch crash state.
        provider_candidate = self.provider_directory / ".AmyTelecom.yaml.pending"
        provider_candidate.write_bytes(NEW_BYTES)
        os.chmod(provider_candidate, 0o640)
        source_candidate = self.private_root / ".airport-source.json.pending"
        source_candidate.write_bytes(serialize_source(NEW_SOURCE))
        os.chmod(source_candidate, 0o600)
        self.write_journal(
            {
                "schema_version": 1,
                "provider": provider_candidate.name,
                "source": source_candidate.name,
            }
        )
        self.store.recover()
        self.assert_new_pair()
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])

    def write_pending_journal(self, *, provider_mode, source_mode):
        provider_candidate = self.provider_directory / ".AmyTelecom.yaml.pending"
        provider_candidate.write_bytes(NEW_BYTES)
        os.chmod(provider_candidate, provider_mode)
        source_candidate = self.private_root / ".airport-source.json.pending"
        source_candidate.write_bytes(serialize_source(NEW_SOURCE))
        os.chmod(source_candidate, source_mode)
        self.write_journal(
            {
                "schema_version": 1,
                "provider": provider_candidate.name,
                "source": source_candidate.name,
            }
        )

    def test_provider_leg_anomaly_is_reported_as_provider_invalid(self):
        self.write_pending_journal(provider_mode=0o644, source_mode=0o600)
        with self.assertRaises(AirportStoreError) as caught:
            self.store.recover()
        self.assertEqual(caught.exception.code, "airport_provider_invalid")
        self.assert_old_pair()
        self.assertTrue(self.journal_path().exists())

    def test_source_leg_anomaly_is_reported_as_source_invalid(self):
        self.write_pending_journal(provider_mode=0o640, source_mode=0o644)
        with self.assertRaises(AirportStoreError) as caught:
            self.store.recover()
        self.assertEqual(caught.exception.code, "airport_source_invalid")
        # The provider leg rolls forward first; the source leg stays old with
        # the journal left in place, never a silently mixed final state.
        self.assertEqual(self.store.path.read_bytes(), NEW_BYTES)
        self.assertEqual(self.raw_pair()[1], OLD_SOURCE)
        self.assertTrue(self.journal_path().exists())

    def test_provider_final_anomaly_is_reported_as_provider_invalid(self):
        # Both candidates already consumed, but the provider file was tampered
        # with after the switch; recovery must not misreport the leg.
        self.write_pending_journal(provider_mode=0o640, source_mode=0o600)
        self.store.recover()
        self.assert_new_pair()
        self.write_journal(
            {
                "schema_version": 1,
                "provider": ".AmyTelecom.yaml.pending",
                "source": ".airport-source.json.pending",
            }
        )
        os.chmod(self.store.path, 0o644)
        with self.assertRaises(AirportStoreError) as caught:
            self.store.recover()
        self.assertEqual(caught.exception.code, "airport_provider_invalid")

    def test_journal_candidate_fsync_failure_keeps_the_old_pair(self):
        # fsync call 3 is the journal candidate's own durability barrier.
        with self.failing_fsyncs(fail_on=3):
            with self.assertRaises(AirportStoreError) as caught:
                self.replace_with_new()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])
        self.assert_old_pair()
        self.store.recover()
        self.assert_old_pair()

    def test_journal_directory_fsync_failure_drops_the_journal(self):
        # fsync call 4 is the private-root sync after the journal rename; a
        # journal that never became durable is quietly removed again.
        with self.failing_fsyncs(fail_on=4):
            with self.assertRaises(AirportStoreError) as caught:
                self.replace_with_new()
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assertFalse(self.journal_path().exists())
        self.assertEqual(self.provider_names(), ["AmyTelecom.yaml"])
        self.assertEqual(self.private_names(), [AIRPORT_SOURCE_FILENAME])
        self.assert_old_pair()
        self.store.recover()
        self.assert_old_pair()


if __name__ == "__main__":
    unittest.main()
