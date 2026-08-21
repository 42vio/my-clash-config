# Manual 3x-ui initialization checklist

This checklist configures the panel side of the server exactly once, by
hand.  Everything the project automates afterwards (Nginx, certificates,
firewall, the pinned Compose stack) assumes the end state described
here, and `scripts/server_preflight.py` refuses to pass a host that
drifts away from it.

## Pinned versions

| Component | Pinned value |
| --- | --- |
| Operating system | Debian 12 (bookworm), amd64 |
| 3x-ui panel | 3.6.0 (native install, systemd `x-ui` unit) |
| Xray-core | 26.6.27 (selected inside 3x-ui) |
| Public inbound | VLESS + RAW/TCP + REALITY on TCP 443, flow `xtls-rprx-vision` |

All names, addresses, and ports below that are not version numbers are
examples; the real values live only in the private `service.yaml` and in
3x-ui itself.

## Steps (in order)

1. **Reinstall a clean Debian 12 amd64 server** and update the OS
   (`apt update && apt full-upgrade`).  Do not reuse a host that ever ran
   another proxy stack; the preflight blocks leftover Trojan/Jrohy,
   MariaDB, or Portainer services and unknown owners of TCP 443.
2. **Download the official 3.6.0 installation script from the matching
   Git tag to a local file and inspect it before running it.**  Never
   pipe the network response to a shell:

   ```bash
   curl --fail --show-error --location \
     --output /tmp/3x-ui-install-v3.6.0.sh \
     https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.6.0/install.sh
   less /tmp/3x-ui-install-v3.6.0.sh
   bash /tmp/3x-ui-install-v3.6.0.sh v3.6.0
   ```

   The final `bash` command is an intentional human step; it is never
   called by `scripts/install_server.py` or any other script in this
   repository.
3. **Install native 3x-ui 3.6.0 manually.**  Record the package or
   source URL and its checksum in the administrator's private
   deployment log (kept outside this repository, like every other
   private artifact).
4. **Panel settings:** strong unique username and password, 2FA
   enabled, a random Web Base Path, and the panel bound to
   `127.0.0.1:<configured-panel-port>` (the `xui.panel-port` from the
   private `service.yaml`, e.g. `2053`).
5. **Raw subscription service** bound to
   `127.0.0.1:<configured-subscription-port>` (the
   `xui.subscription-port`, e.g. `2096`) with a random subscription
   path.  The panel and the subscription service are only reachable
   through the project's Nginx on 8443 or over an SSH tunnel, never
   directly.
6. **Select Xray-core 26.6.27 in 3x-ui**, verify the bundled binary
   version (`<xray-binary-path> version`, path from the private
   settings), and **disable automatic core upgrades** so the pin holds.
7. **Create exactly one public inbound:** protocol VLESS, transport
   RAW/TCP, security REALITY, port TCP 443, flow `xtls-rprx-vision`, a
   REALITY dest/SNI that passed `scripts/check_reality_target.py`, a
   generated REALITY keypair, and at least one non-empty short ID.
   Nothing else may listen publicly on 443 (TCP or UDP).
8. **Create one independent 3x-ui client per person.**  Never share
   UUIDs or raw subscription IDs: ordinary-user clients are referenced
   only by their matching `users.yaml` entry, and the owner has a
   separate client of their own.
9. **Verify before use:** run `scripts/check_reality_target.py` before
   accepting the REALITY target, then
   `scripts/server_preflight.py --config private/config/service.yaml`
   (as root, exit 0 required) before running the project installer —
   a bare invocation defaults to the repository example settings and
   will always block on the DNS comparison.

## Redacted verification table

Copy this table into the private deployment log and fill it in; it is
the human-readable twin of the machine report.

| Check | Expected |
| --- | --- |
| Panel version | 3.6.0 (`xui --version`) |
| Xray version | 26.6.27 (bundled binary) |
| Panel listener | `127.0.0.1:<panel-port>` (loopback only) |
| Subscription listener | `127.0.0.1:<subscription-port>` (loopback only) |
| Public listener | exactly one TCP 443, owned by the Xray process |
| Inbound protocol / network / security | VLESS / tcp / reality |
| Inbound flow (every client) | `xtls-rprx-vision` |
| Client count | one per person, no shared UUIDs |
| Short IDs | at least one non-empty value |
| Server names | non-empty, matches the tested target |
| Target test | `reachable`, `tls13`, `alpn_h2`, `x25519`, `certificate_name` all `true` |
| Preflight | exit 0, no blocking codes |
| Private tree | `private/` subdirs uid/gid 10001, dirs 0700, `config/*.yaml` 0600 |

## Secret boundaries

- The **REALITY private key stays only in Xray/3x-ui** (the generated
  keypair's private half).  It must never enter this repository, the
  private `service.yaml`, the deployment log in plaintext, or any
  generated Clash file; published Clash configs carry only the public
  key and a short ID.
- Panel credentials, 2FA secrets, Web Base Path, subscription paths,
  client UUIDs, and the private deployment log are equally excluded
  from the repository and from every report the scripts print.
- `scripts/server_preflight.py` is read-only: it issues bounded
  `systemctl is-active`/`show`, `ss`, version, `ufw status`, `nginx -T`,
  and `docker compose config` probes only, and its reports are limited
  to booleans, versions, counts, ports, and stable codes.
