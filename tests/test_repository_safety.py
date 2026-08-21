import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


# Synthetic canary values: if any of these ever appear in a tracked
# template, example, or fixture, real private data has leaked into the
# repository.
FORBIDDEN_SUBSTRINGS = (
    "198.51.100.77",
    "203.0.113.88",
    "canary-panel.example.com",
    "canary-subscription.example.com",
    "relay-placeholder.example.com",
    "22222222-2222-4222-8222-222222222222",
    "33333333-3333-4333-8333-333333333333",
    "fedcba9876543210",
    "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
    "synthetic-password-fixture",
    "synthetic-owner-subscription-id",
    "synthetic-friend-subscription-id",
    "safe-panel-base-path-fixture",
    "operator-encrypted-storage-fixture",
)

TRACKED_DOCUMENT_PATHS = (
    "templates/clash.yaml.j2",
    "templates/variants/balanced.yaml",
    "templates/variants/balanced-win.yaml",
    "templates/variants/privacy.yaml",
    "tests/fixtures/synthetic-users.yaml",
)


class RepositorySafetyTests(unittest.TestCase):
    def test_every_runtime_private_path_is_ignored(self):
        paths = (
            "private/config/service.yaml",
            "private/config/users.yaml",
            "private/reference-configs/2026-08-21/My-Clash_Balanced.yaml",
            "private/reference-configs/2026-08-21/My-Clash_Balanced_Win.yaml",
            "private/reference-configs/2026-08-21/My-Clash_Privacy.yaml",
            "private/sources/owner/airport.yaml",
            "private/sources/owner/home.yaml",
            "private/staging/op/user/config.yaml",
            "private/releases/user/release/config.yaml",
            "private/current/user",
            "private/state/certificate.json",
            "private/logs/operations.jsonl",
        )
        for path in paths:
            result = subprocess.run(
                ["git", "check-ignore", "-q", path],
                cwd=ROOT,
                check=False,
            )
            self.assertEqual(result.returncode, 0, path)

    def test_reference_sources_are_not_tracked(self):
        for name in (
            "My-Clash_Balanced.yaml",
            "My-Clash_Balanced_Win.yaml",
            "My-Clash_Privacy.yaml",
        ):
            result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", f"1/{name}"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(result.returncode, 0, name)

    def test_tracked_templates_examples_and_fixtures_contain_no_private_data(self):
        for relative in TRACKED_DOCUMENT_PATHS:
            lowered = (ROOT / relative).read_text(encoding="utf-8").lower()
            for forbidden in FORBIDDEN_SUBSTRINGS:
                self.assertNotIn(
                    forbidden.lower(), lowered, f"{relative} leaks {forbidden!r}"
                )
