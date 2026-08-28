import gzip
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from clash_sub.mihomo import MihomoUpdateError, install_latest_mihomo


class MihomoUpdateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.binary = self.root / "lib" / "mihomo"
        self.public = self.root / "public"
        self.public.mkdir()
        (self.public / "clash-standard.yaml").write_text("mixed-port: 7890\n", encoding="utf-8")
        self.payload = b"#!/bin/sh\necho Mihomo Meta v1.19.28\n"
        self.archive = gzip.compress(self.payload)
        self.digest = hashlib.sha256(self.archive).hexdigest()
        self.calls = []

    def tearDown(self):
        self.tempdir.cleanup()

    def _metadata(self, *, digest=None):
        return {
            "tag_name": "v1.19.28",
            "prerelease": False,
            "assets": [{
                "name": "mihomo-linux-amd64-v1.19.28.gz",
                "browser_download_url": "https://example.test/mihomo.gz",
                "digest": "sha256:" + (digest or self.digest),
            }],
        }

    def _runner(self, arguments, **kwargs):
        arguments = list(arguments)
        self.calls.append(arguments)
        if arguments[0] == "curl":
            destination = Path(arguments[arguments.index("-o") + 1])
            url = next(argument for argument in arguments if argument.startswith("https://"))
            if url.endswith("/releases/latest"):
                destination.write_text(json.dumps(self._metadata()), encoding="utf-8")
            else:
                destination.write_bytes(self.archive)
        return subprocess.CompletedProcess(arguments, 0, stdout=b"Mihomo Meta v1.19.28\n")

    def test_installs_latest_stable_release_and_validates_published_configs(self):
        nested = self.public / "owner" / "release"
        nested.mkdir(parents=True)
        nested_config = nested / "clash-privacy.yaml"
        nested_config.write_text("mixed-port: 7891\n", encoding="utf-8")
        result = install_latest_mihomo(
            self.root, self._runner, binary=self.binary, public_root=self.public
        )

        self.assertEqual(result, {"changed": True, "version": "v1.19.28"})
        self.assertEqual(self.binary.read_bytes(), self.payload)
        self.assertEqual(self.binary.stat().st_mode & 0o777, 0o755)
        self.assertTrue(any(call[1:] == ["-t", "-f", str(self.public / "clash-standard.yaml")] for call in self.calls))
        self.assertTrue(any(call[1:] == ["-t", "-f", str(nested_config)] for call in self.calls))
        self.assertTrue(all("-fsSL" in call for call in self.calls if call[0] == "curl"))

    def test_current_latest_version_skips_binary_download(self):
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(self.payload)
        self.binary.chmod(0o755)

        result = install_latest_mihomo(
            self.root, self._runner, binary=self.binary, public_root=self.public
        )

        self.assertEqual(result, {"changed": False, "version": "v1.19.28"})
        self.assertEqual(sum(call[0] == "curl" for call in self.calls), 1)

    def test_checksum_failure_keeps_existing_binary(self):
        old = b"old mihomo"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(old)
        self.binary.chmod(0o755)

        def runner(arguments, **kwargs):
            arguments = list(arguments)
            if arguments[0] == str(self.binary):
                return subprocess.CompletedProcess(arguments, 0, stdout=b"Mihomo Meta v1.19.27\n")
            if arguments[0] == "curl":
                destination = Path(arguments[arguments.index("-o") + 1])
                url = next(argument for argument in arguments if argument.startswith("https://"))
                if url.endswith("/releases/latest"):
                    destination.write_text(json.dumps(self._metadata(digest="0" * 64)), encoding="utf-8")
                else:
                    destination.write_bytes(self.archive)
            return subprocess.CompletedProcess(arguments, 0, stdout=b"")

        with self.assertRaisesRegex(MihomoUpdateError, "mihomo_checksum_invalid"):
            install_latest_mihomo(
                self.root, runner, binary=self.binary, public_root=self.public
            )

        self.assertEqual(self.binary.read_bytes(), old)

    def test_non_object_release_metadata_fails_with_stable_error(self):
        def runner(arguments, **kwargs):
            arguments = list(arguments)
            destination = Path(arguments[arguments.index("-o") + 1])
            destination.write_text("[]", encoding="utf-8")
            return subprocess.CompletedProcess(arguments, 0, stdout=b"")

        with self.assertRaisesRegex(MihomoUpdateError, "mihomo_release_invalid"):
            install_latest_mihomo(
                self.root, runner, binary=self.binary, public_root=self.public
            )

    def test_candidate_validation_failure_keeps_existing_binary(self):
        old = b"old mihomo"
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(old)
        self.binary.chmod(0o755)

        def runner(arguments, **kwargs):
            arguments = list(arguments)
            if arguments[0] == str(self.binary):
                return subprocess.CompletedProcess(arguments, 0, stdout=b"Mihomo Meta v1.19.27\n")
            result = self._runner(arguments, **kwargs)
            if arguments[1:3] == ["-t", "-f"]:
                result.returncode = 1
            return result

        with self.assertRaisesRegex(MihomoUpdateError, "mihomo_command_failed"):
            install_latest_mihomo(
                self.root, runner, binary=self.binary, public_root=self.public
            )

        self.assertEqual(self.binary.read_bytes(), old)


if __name__ == "__main__":
    unittest.main()
