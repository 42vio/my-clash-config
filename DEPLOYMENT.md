# Deployment

This repository change stops at local initialization and verification. Server
upload and deployment are a separate future boundary; do not run server
commands as part of a template update.

## Prerequisites

Use a clean apt/systemd Linux host with 3x-ui installed and configured as in
[docs/3x-ui-setup.md](docs/3x-ui-setup.md). The host needs Git, Python 3,
`python3-venv`, Nginx, and the required domain and DNS setup.

## Clean install

```bash
apt-get update
apt-get install -y git python3 python3-venv nginx
git clone https://github.com/42vio/my-clash-config /opt/my-clash-config
cd /opt/my-clash-config
bash install.sh
```

Keep service configuration and release state private. Follow the installer
prompts; do not put tokens, node values, or private YAML in the repository.

## Verify

```bash
nginx -t
clash-sub sync
clash-sub links
clash-sub status
```

Confirm Nginx validates, synchronization creates only the authorized release
files, links are available only to their owners, and status reports success.
Use [docs/recovery.md](docs/recovery.md) for a clean rebuild or restore.
