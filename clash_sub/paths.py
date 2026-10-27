"""Repository paths without importing the service construction stack."""

from pathlib import Path


def repo_root():
    return Path(__file__).resolve().parents[1]


def config_path(root=None):
    root = Path(root) if root else repo_root()
    return root / "private" / "config" / "service.yaml"
