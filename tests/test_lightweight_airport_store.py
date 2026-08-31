import os
import stat
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from clash_sub import airport_store
from clash_sub.airport_store import AirportStore, AirportStoreError

PROVIDER_BYTES = (
    b"proxies:\n"
    b"- {name: Node, type: trojan, server: airport.example, port: 443, password: secret}\n"
)
OLD_BYTES = PROVIDER_BYTES.replace(b"airport.example", b"old-airport.example")
NEW_BYTES = PROVIDER_BYTES.replace(b"airport.example", b"new-airport.example")


class AirportStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.public_root = Path(self.tmp.name) / "public"
        self.provider_directory = self.public_root / "provider"
        self.provider_directory.mkdir(parents=True)
        self.store = AirportStore(self.public_root)

    def remaining_names(self):
        return sorted(entry.name for entry in self.provider_directory.iterdir())

    def test_path_is_always_the_stable_provider_file(self):
        self.assertEqual(
            self.store.path,
            self.public_root / "provider" / "AmyTelecom-Provider.yaml",
        )

    def test_replace_validates_temporary_file_then_atomically_publishes(self):
        seen = []
        modes = []

        def validator(candidate):
            seen.append(candidate.read_bytes())
            modes.append(stat.S_IMODE(candidate.stat().st_mode))
            self.assertEqual(candidate.parent, self.provider_directory)
            self.assertTrue(candidate.name.startswith(".AmyTelecom-Provider."))

        path = self.store.replace(PROVIDER_BYTES, validator)
        self.assertEqual(seen, [PROVIDER_BYTES])
        self.assertEqual(modes, [0o640])
        self.assertEqual(path, self.store.path)
        self.assertEqual(path.name, "AmyTelecom-Provider.yaml")
        self.assertEqual(path.read_bytes(), PROVIDER_BYTES)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
        self.assertEqual(self.remaining_names(), ["AmyTelecom-Provider.yaml"])

    def test_read_returns_exact_bytes_of_the_current_provider(self):
        self.store.replace(OLD_BYTES, lambda candidate: None)
        self.assertEqual(self.store.read(), OLD_BYTES)

    def test_read_requires_an_existing_provider(self):
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()

    def test_validator_failure_keeps_previous_provider(self):
        self.store.replace(OLD_BYTES, lambda candidate: None)

        def reject(candidate):
            raise ValueError("candidate rejected")

        with self.assertRaises(AirportStoreError) as caught:
            self.store.replace(NEW_BYTES, reject)
        self.assertEqual(caught.exception.code, "airport_provider_invalid")
        self.assertEqual(self.store.read(), OLD_BYTES)
        self.assertEqual(self.remaining_names(), ["AmyTelecom-Provider.yaml"])

    def test_failed_replacement_keeps_previous_provider_and_cleans_up(self):
        self.store.replace(OLD_BYTES, lambda candidate: None)
        with patch.object(airport_store, "_os_replace", side_effect=OSError("injected")):
            with self.assertRaises(AirportStoreError) as caught:
                self.store.replace(NEW_BYTES, lambda candidate: None)
        self.assertEqual(caught.exception.code, "airport_provider_write_failed")
        self.assertEqual(self.store.read(), OLD_BYTES)
        self.assertEqual(self.remaining_names(), ["AmyTelecom-Provider.yaml"])

    def test_empty_input_is_rejected(self):
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(b"", lambda candidate: None)
        self.assertFalse(self.store.path.exists())

    def test_non_bytes_input_is_rejected(self):
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(PROVIDER_BYTES.decode("utf-8"), lambda candidate: None)
        self.assertFalse(self.store.path.exists())

    def test_oversized_input_is_rejected(self):
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(
                b"x" * (airport_store.MAX_PROVIDER_BYTES + 1), lambda candidate: None
            )
        self.assertFalse(self.store.path.exists())

    def test_oversized_stored_provider_is_rejected_on_read(self):
        target = self.store.path
        target.write_bytes(b"x" * (airport_store.MAX_PROVIDER_BYTES + 1))
        os.chmod(target, 0o640)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()

    def test_symlink_target_is_rejected_for_read_and_replace(self):
        outside = Path(self.tmp.name) / "outside.yaml"
        outside.write_bytes(PROVIDER_BYTES)
        self.store.path.symlink_to(outside)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, lambda candidate: None)
        self.assertTrue(self.store.path.is_symlink())
        self.assertEqual(outside.read_bytes(), PROVIDER_BYTES)

    def test_directory_target_is_rejected(self):
        self.store.path.mkdir()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, lambda candidate: None)
        self.assertTrue(self.store.path.is_dir())

    def test_hard_linked_provider_is_rejected_for_read_and_replace(self):
        self.store.replace(OLD_BYTES, lambda candidate: None)
        link = Path(self.tmp.name) / "hardlink.yaml"
        os.link(self.store.path, link)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, lambda candidate: None)
        self.assertEqual(self.store.path.read_bytes(), OLD_BYTES)

    def test_wrong_mode_provider_is_rejected_for_read_and_replace(self):
        self.store.replace(OLD_BYTES, lambda candidate: None)
        self.store.path.chmod(0o644)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, lambda candidate: None)
        self.assertEqual(self.store.path.read_bytes(), OLD_BYTES)

    def test_missing_provider_directory_is_rejected(self):
        store = AirportStore(Path(self.tmp.name) / "absent")
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            store.replace(PROVIDER_BYTES, lambda candidate: None)

    def test_symlinked_provider_directory_is_rejected(self):
        real = Path(self.tmp.name) / "real-provider"
        real.mkdir()
        (real / "ignored.yaml").write_bytes(PROVIDER_BYTES)
        self.public_root.mkdir(parents=True, exist_ok=True)
        (self.public_root / "provider").rmdir()
        (self.public_root / "provider").symlink_to(real)
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.read()
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(NEW_BYTES, lambda candidate: None)
        self.assertEqual(sorted(entry.name for entry in real.iterdir()), ["ignored.yaml"])

    def test_non_callable_validator_is_rejected(self):
        with self.assertRaisesRegex(AirportStoreError, "airport_provider_invalid"):
            self.store.replace(PROVIDER_BYTES, None)

    def test_errors_never_expose_paths_or_document_values(self):
        self.store.replace(OLD_BYTES, lambda candidate: None)

        def reject(candidate):
            raise ValueError(NEW_BYTES)

        messages = []
        with patch.object(
            airport_store, "_os_replace", side_effect=OSError(str(self.tmp.name))
        ):
            with self.assertRaises(AirportStoreError) as caught:
                self.store.replace(NEW_BYTES, reject)
            self.assertEqual(caught.exception.code, "airport_provider_invalid")
            messages.append(str(caught.exception))
            with self.assertRaises(AirportStoreError) as caught:
                self.store.replace(NEW_BYTES, lambda candidate: None)
            self.assertEqual(caught.exception.code, "airport_provider_write_failed")
            messages.append(str(caught.exception))
        self.store.path.unlink()
        with self.assertRaises(AirportStoreError) as caught:
            self.store.read()
        self.assertEqual(caught.exception.code, "airport_provider_invalid")
        messages.append(str(caught.exception))
        for message in messages:
            self.assertNotIn(str(self.tmp.name), message)
            self.assertNotIn("new-airport.example", message)
