# my-clash-config

This repository prepares comment-preserving Clash templates locally. It does
not connect to, upload to, or deploy a server.

## Authorization and releases

| Role | Profile | Sources |
| --- | --- | --- |
| owner | `compat-office` | own 3x-ui, AmyTelecom, home overlay |
| owner | `compat-universal` | own 3x-ui, AmyTelecom |
| owner | `balance-office` | own 3x-ui, AmyTelecom, home overlay |
| member | `compat-universal` | own 3x-ui only |

Owner releases contain `clash-compat-office.yaml`,
`clash-compat-universal.yaml`, `clash-balance-office.yaml`, and the owner-only
`AmyTelecom.yaml`. A member release contains only
`clash-compat-universal.yaml`.

`privacy` is deferred and not included in this release, its links, or its
templates.

## Local data flow

`Compat-Office.yaml` becomes the public base. `Balance-Office.yaml` contributes
its complete `dns` section. `private/home.yaml` is added only to office
profiles. Per-user 3x-ui data and the owner provider are injected at release
generation time.

## Daily template update

```bash
./bin/clash-sub template-sync
```

This local command validates candidates and writes only after all checks pass.
Read the concise procedure in [docs/operations.md](docs/operations.md).

## Documents

- [DEPLOYMENT.md](DEPLOYMENT.md): clean host installation and verification.
- [docs/3x-ui-setup.md](docs/3x-ui-setup.md): required panel and inbound fields.
- [docs/operations.md](docs/operations.md): local updates, release commands, and rollback.
- [docs/private-data.md](docs/private-data.md): ignored private files and backup boundary.
- [docs/recovery.md](docs/recovery.md): rebuild and restore order.
