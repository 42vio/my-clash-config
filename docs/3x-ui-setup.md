# 3x-ui setup

Install 3x-ui using its official instructions. This project only requires the
following fields; it does not manage the panel.

| Area | Required value |
| --- | --- |
| Panel path | A random path, not `/` |
| Panel listener | `127.0.0.1` |
| Panel TLS fields | Empty; Nginx terminates public TLS |
| Clash output | Enabled on `127.0.0.1` |
| Database | `/etc/x-ui/x-ui.db`, read only to this project |
| Inbound | Enabled VLESS + TCP + REALITY on `10443` |

Create one independent client for every user and record the owner email for
installation. After the host is installed, keep the Reality inbound on
`127.0.0.1` and verify with `clash-sub sync`.

Never copy panel credentials, UUIDs, or node material into this repository.
