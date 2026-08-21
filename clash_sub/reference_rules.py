from __future__ import annotations

from typing import Iterable


APPROVED_BALANCED_WIN_UNRESOLVED_PROXY_PATHS: tuple[tuple[object, ...], ...] = (
    ("proxy-groups", 5, "proxies", 2),
    ("proxy-groups", 6, "proxies", 2),
    ("proxy-groups", 7, "proxies", 2),
)


def safe_path(path: tuple[object, ...]) -> str:
    if not path:
        return "<root>"
    rendered = ""
    for item in path:
        if isinstance(item, int):
            rendered += "[%d]" % item
        else:
            if rendered:
                rendered += "."
            rendered += str(item)
    return rendered


def approved_balanced_win_unresolved_proxy_paths() -> tuple[str, ...]:
    return tuple(safe_path(path) for path in APPROVED_BALANCED_WIN_UNRESOLVED_PROXY_PATHS)


def is_approved_balanced_win_unresolved_proxy_path(path: Iterable[object]) -> bool:
    return tuple(path) in APPROVED_BALANCED_WIN_UNRESOLVED_PROXY_PATHS
