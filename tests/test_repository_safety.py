import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
