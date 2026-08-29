# Recovery

For a clean rebuild, first create a new host and configure 3x-ui according to
[3x-ui-setup.md](3x-ui-setup.md). Install the repository as described in
[DEPLOYMENT.md](../DEPLOYMENT.md), then restore private service settings and
private data from encrypted backups with mode `0600`.

Restore in this order:

1. 3x-ui database and inbound/client configuration.
2. Private service configuration and `private/home.yaml`.
3. Repository code and Python environment.
4. Nginx configuration and certificates through the installer.
5. `clash-sub sync`, then `clash-sub links` and `clash-sub status`.

If a release is valid but undesired, use `clash-sub history` and
`clash-sub rollback`. Do not recover secrets from public releases or Git.
