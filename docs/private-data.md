# Private data

`private/` is Git 忽略. Do not add private files with force-add, copy them into
fixtures, or print their contents in diagnostics.

| Path | Purpose | Mode |
| --- | --- | --- |
| `private/home.yaml` | Home overlay for office profiles | `0600` |
| `private/config/service.yaml` | Server-only service settings | `0600` |

Keep private regular files owned by the intended user, non-symlinked, and mode
`0600`. Store encrypted backups outside Git and restore them only into a
private directory with the same mode.

This repository has no private transfer feature. Future server upload and
deployment remain a separately designed boundary; never treat a generated
release as a backup of its source data.
