# Operations

## Airport refresh

On the server, use the `clash-sub` interactive menu to update the airport.
Paste a temporary HTTPS subscription only into the hidden prompt. Do not place
the URL in shell history, repository files, or logs. Check `clash-sub status`
after completion.

## Template update on the development Mac

By default, `template-sync` reads these two iCloud files:

```text
~/Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/
├── Compat-Office.yaml
└── Balance-Office.yaml
```

```bash
./bin/clash-sub template-sync
```

To update exactly one source, pass exactly one option. It does not read the
other default file:

```bash
./bin/clash-sub template-sync --compat-office /path/Compat-Office.yaml
./bin/clash-sub template-sync --balance-office /path/Balance-Office.yaml
```

Compat 公共注释 are retained with the public base. Balance 的完整 `dns` is
replaced as one section, including its comments; it is not recursively merged.
Inputs are read only. A bad path, unavailable iCloud file, parse failure, or
failed validation leaves every destination unchanged.

## Safe report and review

The change report names changed public YAML paths, whether Balance DNS changed,
comment and collection counts, and written or unchanged files. It may show
public comments, but 不显示家庭内容或动态节点的名称、地址、URL 或凭据.

Review only tracked changes, then run the repository tests and both secret
scans. `private/home.yaml` stays local and ignored.

## Releases and rollback

Use `clash-sub sync` to generate a release, `clash-sub links` to view links,
`clash-sub history` to inspect releases, and `clash-sub rollback` to select a
previous release. Rotate a leaked link with `clash-sub rotate-link`.

## Deployment boundary

未来的服务器上传 is a separate, manual deployment boundary. This repository
update neither transfers private files nor connects to a server. When that
future process is defined, it must validate server inputs before publishing;
it is not part of `template-sync`.
