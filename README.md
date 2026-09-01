# my-clash-config 个人维护手册

本仓库维护本地 Clash 模板，并生成服务器运行时所需的订阅发布物。部署、日常运维和恢复分别以对应手册为准。

## 最终输出

订阅 URL 与文件名严格区分大小写，Clash Verge 中的 profile 标题与文件名一致：

| 身份 | 输出文件 | Clash 标题 | 机场 provider |
| --- | --- | --- | --- |
| owner | `Clash-Compat.yaml` | `Clash-Compat` | 有（`AmyTelecom`） |
| owner | `Clash-Balance.yaml` | `Clash-Balance` | 有（`AmyTelecom`） |
| 普通用户 | `Clash-Compat.yaml` | `Clash-Compat` | 无 |

owner 与普通用户不通过文件名区分，由订阅令牌和服务端授权决定。普通用户不生成 Balance，也不包含或访问任何机场 provider。机场节点由独立文件 `AmyTelecom.yaml` 单独发布，只挂在 owner 的订阅路由下。

本次为不兼容升级：旧订阅文件名与旧 URL 已全部删除，没有重定向或兼容入口，旧客户端必须用 `clash-sub links` 重新获取新链接。

## 生成关系

`templates/base/Clash-Compat.yaml` 是完整基础模板；生成 Balance 时以 `templates/dns/Clash-Balance.yaml` 的整个 `dns:` 段（含注释）替换基础模板的 DNS。发布时注入各用户的 3x-ui 节点，owner 额外注入 `AmyTelecom` 机场 provider；普通用户只输出 Compat。服务器不保存任何 Home 配置，Home 只存在于本机 Clash Verge 的全局扩展脚本。

## 订阅流量元数据

订阅响应里的 `Subscription-Userinfo` 流量头不预写进任何配置文件：客户端请求订阅时，Nginx 把请求转发到 socket 激活的 `clash-sub-metadata.service`，服务按需读取 3x-ui 流量（五分钟缓存）与最近一次机场下载保存的流量，生成响应头；文件正文仍由 Nginx 直接发送。元数据服务不可用或超时的时候，同一份文件照常以 200 返回，只是缺少流量头，订阅本身不受影响。完整链路、缓存与降级行为见部署与运维手册。

## 本地文件

- `templates/base/Clash-Compat.yaml`：Compat 完整基础模板（已去除动态节点与机场痕迹）。
- `templates/dns/Clash-Balance.yaml`：只含 Balance 的完整 `dns:` 段及其注释。
- `templates/profiles.yaml`：两个 profile 的 DNS 配方与注入策略组列表。
- `private/clash-verge-home.js`：Git 忽略的本机 Home 扩展脚本，仅对 `Clash-Compat` 和 `Clash-Balance` 两个标题生效。

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

- [DEPLOYMENT.md](DEPLOYMENT.md)：安装、监听收口、目录权限、升级卸载、首次导入与备份恢复。
- [docs/template-design.md](docs/template-design.md)：模板组成、注释、iCloud 同步、导出矩阵、机场引用与 Home 脚本。
- [docs/operations.md](docs/operations.md)：模板更新、机场更新、用户管理、同步、回滚与故障处理。

## 修改后检查

先检查模板同步报告与受跟踪 diff，再运行测试和两种密钥扫描。模板机制、部署与恢复的完整操作分别留在对应手册，避免在本页重复维护。
