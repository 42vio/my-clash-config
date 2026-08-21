"""Tests for the tracked-secret scanner.

Every test uses synthetic values only.  The scanner must prove that a
leak fails, that safe documentation examples pass, that binary files
are skipped safely, and that its output never echoes the secret.
"""

import importlib.util
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


ROOT = Path(__file__).resolve().parents[1]
SCANNER_PATH = ROOT / "scripts" / "scan_tracked_secrets.py"

# Random-looking synthetic canaries: none of these follow the repeated
# digit patterns that mark documentation UUIDs.  The UUID and the bare
# hex token are written as adjacent literals so this tracked test file
# never itself contains a contiguous random-looking value to flag.
RANDOM_UUID = "8f14e45f" "ceea167a5a36dedd4bea2543"
RANDOM_UUID_TEXT = "8f14e45f" "-ceea-167a-5a36-dedd" "4bea2543"
PRIVATE_PASSWORD = "synthetic-password-0123456789abcdef"
PRIVATE_TOKEN_HASH = "9e107669" "6b24aa1f9b7d3fd3b5a2c0dc"
PRIVATE_TOKEN_HASH_TEXT = (
    "9e1076696b24aa1f9b7d3fd3b5a2c0dc9e1076696b24aa1f9b7d3fd3b5a2c0dc"
)
SUBSCRIPTION_TOKEN = "e2eBearerToken0123456789abcdefghijklmnopqrstuv"


def load_scanner():
    spec = importlib.util.spec_from_file_location("scan_tracked_secrets", SCANNER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("cannot load the secret scanner")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_git(repository: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=repository,
        check=True,
        capture_output=True,
    )


class ScannerTestCase(unittest.TestCase):
    def setUp(self):
        self.scanner = load_scanner()

    def make_repository(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        run_git(directory, "init", "-q")
        return directory

    def stage(self, repository: Path, relative: str, text: str) -> None:
        path = repository / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        run_git(repository, "add", "-f", relative)

    def scan(self, repository: Path, private_root=None):
        import io
        from contextlib import redirect_stdout

        arguments = []
        if private_root is not None:
            arguments += ["--private-root", str(private_root)]
        captured = io.StringIO()
        with redirect_stdout(captured):
            exit_code = self.scanner.main(arguments, root=repository)
        self.captured_report = captured.getvalue()
        return exit_code

    def write_private_files(self, private_root: Path) -> None:
        config = private_root / "config"
        sources = private_root / "sources" / "owner"
        config.mkdir(parents=True, exist_ok=True)
        sources.mkdir(parents=True, exist_ok=True)
        (config / "users.yaml").write_text(
            "users:\n  owner:\n    token-sha256: %s\n" % PRIVATE_TOKEN_HASH_TEXT,
            encoding="utf-8",
        )
        (sources / "airport.yaml").write_text(
            "proxies:\n  - name: synthetic\n    password: %s\n" % PRIVATE_PASSWORD,
            encoding="utf-8",
        )


class TrackedPathRuleTests(ScannerTestCase):
    def test_forbidden_tracked_paths_are_rejected_with_categories(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            forbidden = {
                "generated/out.yaml": "tracked-generated-yaml",
                "private/config/service.yaml": "tracked-private-data",
                "private/sources/owner/airport.yaml": "tracked-private-data",
                ".env": "tracked-env-file",
                "server.key": "tracked-private-key-file",
                "releases/user/rel/manifest.json": "tracked-runtime-manifest",
                "1/My-Clash_Balanced.yaml": "tracked-legacy-path",
            }
            for relative in forbidden:
                self.stage(repository, relative, "synthetic\n")
            self.stage(repository, "README.md", "safe\n")

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 1)
            report_lines = self.captured_report.splitlines()
            for relative, category in forbidden.items():
                self.assertIn("%s: %s" % (category, relative), report_lines)

    def test_example_and_documentation_paths_are_allowed(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            for relative in (
                "generated/.gitkeep",
                "private/proxies.yaml.example",
                ".env.example",
                "config/service.example.yaml",
                "templates/clash.yaml.j2",
            ):
                self.stage(repository, relative, "safe example\n")

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 0, self.captured_report)


class TrackedContentRuleTests(ScannerTestCase):
    def test_concrete_proxy_uri_subscription_path_and_uuid_are_flagged(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            self.stage(
                repository,
                "notes.md",
                "link: vless://%s@203.0.113.9:443?security=reality\n" % RANDOM_UUID_TEXT,
            )
            self.stage(
                repository,
                "links.txt",
                "https://sub.example.com:8443/s/%s/balanced.yaml\n" % SUBSCRIPTION_TOKEN,
            )
            self.stage(repository, "ids.txt", "client id %s\n" % RANDOM_UUID_TEXT)

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 1)
            report = self.captured_report
            self.assertIn("tracked-proxy-uri: notes.md", report)
            self.assertIn("tracked-subscription-token: links.txt", report)
            self.assertIn("tracked-uuid: ids.txt", report)
            self.assertNotIn(RANDOM_UUID_TEXT, report)
            self.assertNotIn(SUBSCRIPTION_TOKEN, report)

    def test_documentation_examples_are_not_flagged(self):
        fixture_uri = (
            "vless://00000000-0000-4000-8000-000000000001@192.0.2.10:443"
            "?security=reality&sni=www.example.com#Example\n"
        )
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            self.stage(repository, "fixture.txt", fixture_uri)
            self.stage(
                repository,
                "negative-case.txt",
                "https://user:pass@airport.example/private\n",
            )
            self.stage(
                repository,
                "synthetic-key.txt",
                "-----BEGIN PRIVATE KEY-----\nSYNTHETIC\n",
            )
            self.stage(
                repository,
                "uuids.txt",
                "11111111-1111-4111-8111-111111111111 and 00000000-0000-4000-8000-000000000999\n",
            )

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 0, self.captured_report)

    def test_concrete_pem_private_key_block_is_flagged(self):
        body = "\n".join("A" * 64 for _ in range(4))
        block = "-----BEGIN PRIVATE KEY-----\n%s\n-----END PRIVATE KEY-----\n" % body
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            self.stage(repository, "leaked.key.txt", block)

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 1)
            self.assertIn("tracked-private-key-pem: leaked.key.txt", self.captured_report)

    def test_url_userinfo_with_real_credentials_is_flagged(self):
        # The scheme is split from the credential part so this tracked
        # test file itself contains no complete userinfo URL.
        leak_template = "https" + "://%s:%s@portal.example.net/path\n"
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            self.stage(
                repository,
                "uri.txt",
                leak_template % ("realuser9", "realpassword12345"),
            )

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 1)
            self.assertIn("tracked-url-userinfo: uri.txt", self.captured_report)
            self.assertNotIn("realpassword12345", self.captured_report)

    def test_binary_files_are_skipped_safely(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            payload = (
                b"\x89PNG\r\n\x1a\n\x00\x00" + RANDOM_UUID_TEXT.encode("ascii") + b"\x00"
            )
            path = repository / "image.png"
            path.write_bytes(payload)
            run_git(repository, "add", "-f", "image.png")

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 0, self.captured_report)

    def test_content_beyond_the_ten_mebibyte_cap_is_not_scanned(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            path = repository / "big.txt"
            filler = "safe\n" * ((10 * 1024 * 1024) // 4)
            path.write_text(filler + RANDOM_UUID_TEXT + "\n", encoding="utf-8")
            run_git(repository, "add", "-f", "big.txt")

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 0, self.captured_report)

    def test_tracked_symlink_content_is_not_followed(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            outside = repository / "outside.txt"
            outside.write_text("uuid %s\n" % RANDOM_UUID_TEXT, encoding="utf-8")
            link = repository / "linked.txt"
            link.symlink_to(outside)
            run_git(repository, "add", "-f", "linked.txt")

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 0, self.captured_report)

    def test_bare_hex_token_is_flagged_and_url_embedded_hex_is_allowed(self):
        # Schemes and hex values are assembled so this tracked test file
        # contains no contiguous trigger itself.
        gist_url = "https" + "://gist.example.com/ddgksf2013/%s/raw/Ai.yaml" % RANDOM_UUID
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            self.stage(repository, "token.txt", "hash: %s\n" % RANDOM_UUID)
            self.stage(repository, "rules.txt", gist_url + "\n")
            self.stage(repository, "synthetic.txt", "00ff00ff00ff00ff00ff00ff00ff00ff\n")
            self.stage(repository, "repeated.txt", "A" * 32 + "\n")

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 1)
            self.assertIn("tracked-hex-token: token.txt", self.captured_report)
            self.assertNotIn("tracked-hex-token: rules.txt", self.captured_report)
            self.assertNotIn("tracked-hex-token: synthetic.txt", self.captured_report)
            self.assertNotIn("tracked-hex-token: repeated.txt", self.captured_report)
            self.assertNotIn(RANDOM_UUID, self.captured_report)

    def test_proxy_uri_with_punctuated_first_character_is_flagged(self):
        # The scheme is split from the credential so this tracked test
        # file contains no complete proxy URI itself.
        leaks = (
            "vless" + "://.password@203.0.113.9:443\n",
            "trojan" + "://-dashpass@203.0.113.9:443\n",
        )
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            for index, leak in enumerate(leaks):
                self.stage(repository, "leak-%d.txt" % index, leak)

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 1)
            report_lines = self.captured_report.splitlines()
            for index in range(len(leaks)):
                self.assertIn(
                    "tracked-proxy-uri: leak-%d.txt" % index, report_lines
                )

    def test_additional_proxy_uri_schemes_are_flagged(self):
        leaks = (
            "hysteria" + "2://secretpassword@203.0.113.9:443\n",
            "hy" + "2://secretpassword@203.0.113.9:443\n",
            "tuic" + "://secretpassword@203.0.113.9:443\n",
            "socks5" + "h://secretpassword@203.0.113.9:443\n",
        )
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            for index, leak in enumerate(leaks):
                self.stage(repository, "scheme-%d.txt" % index, leak)

            exit_code = self.scan(repository)

            self.assertEqual(exit_code, 1)
            report_lines = self.captured_report.splitlines()
            for index in range(len(leaks)):
                self.assertIn(
                    "tracked-proxy-uri: scheme-%d.txt" % index, report_lines
                )

    def test_undecodable_tracked_name_is_reported(self):
        # macOS refuses to create invalid-UTF-8 filenames, so simulate
        # the git ls-files bytes directly; the scanner must report the
        # undecodable name as a finding instead of dropping it.
        from types import SimpleNamespace
        from unittest.mock import patch

        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            self.stage(repository, "README.md", "safe\n")
            fake_output = SimpleNamespace(
                returncode=0,
                stdout=b"README.md\0undecodable-\xff-name.txt\0",
            )
            with patch.object(
                self.scanner.subprocess, "run", return_value=fake_output
            ):
                exit_code = self.scan(repository)

            self.assertEqual(exit_code, 1)
            self.assertIn(
                "tracked-undecodable-name: undecodable-\\xff-name.txt",
                self.captured_report.splitlines(),
            )


class PrivateValueComparisonTests(ScannerTestCase):
    def test_private_value_leak_fails_without_echoing_the_secret(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            private_root = repository / "private"
            self.write_private_files(private_root)
            self.stage(
                repository,
                "leaked.md",
                "somebody pasted %s into a note\n" % PRIVATE_PASSWORD,
            )
            self.stage(repository, "hash.txt", PRIVATE_TOKEN_HASH_TEXT + "\n")

            exit_code = self.scan(repository, private_root=private_root)

            self.assertEqual(exit_code, 1)
            report = self.captured_report
            self.assertIn("tracked-private-value: leaked.md", report)
            self.assertIn("tracked-private-value: hash.txt", report)
            self.assertNotIn(PRIVATE_PASSWORD, report)
            self.assertNotIn(PRIVATE_TOKEN_HASH_TEXT, report)

    def test_clean_repository_with_private_root_passes(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            private_root = repository / "private"
            self.write_private_files(private_root)
            self.stage(repository, "README.md", "documentation only\n")

            exit_code = self.scan(repository, private_root=private_root)

            self.assertEqual(exit_code, 0, self.captured_report)

    def test_structural_private_values_do_not_cause_false_positives(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            private_root = repository / "private"
            config = private_root / "config"
            config.mkdir(parents=True)
            (config / "service.yaml").write_text(
                "converter-base-url: http://127.0.0.1:25500\n"
                "private-root: /opt/clash-sub/private\n"
                "subscription-authority: sub.example.com:8443\n"
                "required-flow: xtls-rprx-vision\n",
                encoding="utf-8",
            )
            self.stage(
                repository,
                "config/service.example.yaml",
                "converter-base-url: http://127.0.0.1:25500\n",
            )

            exit_code = self.scan(repository, private_root=private_root)

            self.assertEqual(exit_code, 0, self.captured_report)

    def test_private_root_without_config_or_sources_passes(self):
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            private_root = repository / "private"
            private_root.mkdir()
            self.stage(repository, "README.md", "safe\n")

            exit_code = self.scan(repository, private_root=private_root)

            self.assertEqual(exit_code, 0, self.captured_report)

    def test_ignored_private_symlinks_are_not_followed(self):
        # If the scanner followed link.yaml it would extract the password
        # and flag the tracked leak below; staying clean proves symlinks
        # inside the ignored private tree are never read.
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            private_root = repository / "private"
            sources = private_root / "sources" / "owner"
            sources.mkdir(parents=True)
            outside = repository / "outside-secret.yaml"
            outside.write_text(
                "proxies:\n  - name: x\n    password: %s\n" % PRIVATE_PASSWORD,
                encoding="utf-8",
            )
            (sources / "link.yaml").symlink_to(outside)
            self.stage(repository, "leaked.md", "contains %s\n" % PRIVATE_PASSWORD)

            exit_code = self.scan(repository, private_root=private_root)

            self.assertEqual(exit_code, 0, self.captured_report)

    def test_malformed_private_yaml_is_skipped_without_echoing_content(self):
        # A yaml parser error message embeds the offending source line,
        # which could carry a private value; the scanner must skip the
        # file silently instead of crashing with a traceback.
        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            private_root = repository / "private"
            config = private_root / "config"
            config.mkdir(parents=True)
            (config / "users.yaml").write_text(
                "users: [unclosed\n  password: %s\n" % PRIVATE_PASSWORD,
                encoding="utf-8",
            )
            self.stage(repository, "README.md", "safe\n")

            exit_code = self.scan(repository, private_root=private_root)

            self.assertEqual(exit_code, 0, self.captured_report)
            self.assertNotIn("Traceback", self.captured_report)
            self.assertNotIn(PRIVATE_PASSWORD, self.captured_report)

    def test_internal_scanner_failure_prints_one_redacted_line(self):
        from unittest.mock import patch

        with TemporaryDirectory() as directory:
            repository = self.make_repository(Path(directory))
            self.stage(repository, "README.md", "safe\n")
            failure = RuntimeError("boom %s" % PRIVATE_PASSWORD)
            with patch.object(
                self.scanner, "scan_repository", side_effect=failure
            ):
                exit_code = self.scan(repository)

            self.assertEqual(exit_code, 2)
            self.assertIn("internal_error", self.captured_report)
            self.assertNotIn(PRIVATE_PASSWORD, self.captured_report)
            self.assertNotIn("Traceback", self.captured_report)


class RepositoryScanTests(ScannerTestCase):
    def test_real_repository_scans_clean(self):
        exit_code = self.scan(ROOT)

        self.assertEqual(exit_code, 0, self.captured_report)


if __name__ == "__main__":
    unittest.main()
