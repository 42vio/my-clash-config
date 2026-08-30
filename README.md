# my-clash-config 个人维护手册

本仓库维护本地 Clash 模板，并生成服务器运行时所需的配置发布物。部署、日常运维和恢复分别以对应手册为准。

## 当前配置

| 角色 | profile | 运行时来源 |
| --- | --- | --- |
| owner | `compat-office` | 自有 3x-ui、`AmyTelecom.yaml`、Home |
| owner | `compat-universal` | 自有 3x-ui、`AmyTelecom.yaml` |
| owner | `balance-office` | 自有 3x-ui、`AmyTelecom.yaml`、Home |
| member | `compat-universal` | 自有 3x-ui |

owner 生成三份 profile，并有独立的 `AmyTelecom.yaml`；member 只生成 `compat-universal`，不包含机场 provider、机场节点或 Home。Privacy 当前未纳入本版本，因此没有操作入口。

## 生成关系

`Compat-Office.yaml` 生成 Compat 公共基础；`compat-universal` 从该基础移除 `private/home.yaml` 声明的 Home 范围；`Balance-Office.yaml` 以完整 `dns:` 段替换 DNS。生成发布物时再注入各用户的 3x-ui 数据，owner 额外注入机场与适用的 Home。

## 本地文件

- `templates/profiles.yaml`：profile 与 DNS/Home 组合。
- `templates/base/compat-office.yaml`：Compat 公共基础。
- `templates/dns/balance-office.yaml`：Balance 完整 DNS 段。
- `private/home.yaml`：Git 忽略的 `0600` 私密 Home 文件；维护边界见模板设计和部署手册。

日常模板同步默认读取 iCloud 中的 `Compat-Office.yaml` 与 `Balance-Office.yaml`；`Compat-Universal.yaml` 仅用于首次初始化 Home 范围。

## 常用命令

开发 Mac 上同步模板：

```bash
./bin/clash-sub template-sync
```

服务器上进入管理菜单：

```bash
clash-sub
```

运行仓库测试：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test*.py'
```

扫描受跟踪文件中的敏感值：

```bash
.venv/bin/python scripts/scan_tracked_secrets.py
```

在服务器上额外比对私密目录中的值：

```bash
sudo .venv/bin/python scripts/scan_tracked_secrets.py --private-root /var/lib/clash-sub/private
```

## 文档入口

- [DEPLOYMENT.md](DEPLOYMENT.md)：首次安装、初始化与部署验收。
- [docs/template-design.md](docs/template-design.md)：模板职责、DNS、Home 与私密边界。
- [docs/operations.md](docs/operations.md)：日常更新、服务器运维、排错、备份、恢复、迁移与人工接管。

## 修改后检查

先检查模板同步报告与受跟踪 diff，再运行测试和两种密钥扫描。模板机制、部署与恢复的完整操作分别留在对应手册，避免在本页重复维护。
