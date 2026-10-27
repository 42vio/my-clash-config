# my-clash-config 个人维护手册

本仓库维护本地 Clash 模板，并生成服务器运行时所需的订阅发布物。部署、日常运维和恢复分别以对应手册为准。

## 最终输出

订阅 URL 与文件名严格区分大小写，Clash Verge 中的 profile 标题与文件名一致：

| 身份 | 输出文件 | Clash 标题 | 机场 provider |
| --- | --- | --- | --- |
| owner | `Clash-Compat.yaml` | `Clash-Compat.yaml` | 有（`AmyTelecom`） |
| owner | `Clash-Balance.yaml` | `Clash-Balance.yaml` | 有（`AmyTelecom`） |
| owner | `Clash-Airport.yaml` | `Clash-Airport.yaml` | 有（`AmyTelecom`），无自建节点 |
| 普通用户 | `Clash-Compat.yaml` | `Clash-Compat.yaml` | 无 |

owner 与普通用户不通过文件名区分，由订阅令牌和服务端授权决定。普通用户不生成 Balance，也不包含或访问任何机场 provider。`Clash-Airport.yaml` 使用与 owner 原链接不同的派生令牌；该令牌只能访问 Airport 配置及其 `AmyTelecom.yaml` provider，不能换文件名取得 owner 的自建配置。

本次为不兼容升级：旧订阅文件名与旧 URL 已全部删除，没有重定向或兼容入口，旧客户端必须用 `clash-sub links` 重新获取新链接。

### 通用纯节点订阅

owner 和普通用户各自额外提供 `Nodes.txt`，地址为 `https://<订阅域名>/s/<该用户令牌>/Nodes.txt`。正文是 Base64 编码的节点分享 URI 列表，只包含该用户的自建节点，不包含机场节点、DNS、策略组或分流规则。Shadowrocket 添加为订阅；Quantumult X 通过资源解析器导入，客户端仍需自己的规则配置。

更新服务器代码后运行 `clash-sub sync`，再用 `clash-sub links` 获取实际链接。旧发布物在同步前继续可用，同步前不会列出尚未生成的纯节点链接。纯节点与 Clash 配置一起发布、回滚，沿用用户令牌、令牌轮换和自建流量头；机场专用令牌无权访问它。

导出支持当前自建 VLESS TCP（含 REALITY / Vision）、Trojan TCP TLS 和无插件 Shadowsocks。遇到无法完整转换的协议或选项会使该用户本次同步失败并保留旧发布物，避免生成缺参数的节点。

## 生成关系

`templates/base/Clash-Compat.yaml` 是完整基础模板；生成 Balance 时以 `templates/dns/Clash-Balance.yaml` 的整个 `dns:` 段（含注释）替换基础模板的 DNS。owner 的 Airport 使用 Compat 模板和机场 provider，不注入 3x-ui 节点；原有两份 owner 配置继续注入自建节点与机场 provider，普通用户只输出 Compat。服务器不保存任何 Home 配置，Home 只存在于本机 Clash Verge 的全局扩展脚本。

## 订阅网页与 3x-ui 面板开关

终端主菜单 `3` 按用户查看网页与订阅链接；`9` 管理独立链接轮换；`4「网页与 3x-ui 面板」` 提供网页初始化、3x-ui 公网访问开关、SSH 隧道说明、API 令牌设置（子菜单 7）和订阅网页开关（8 开启、9 关闭；保留客户端订阅下载）。详细步骤见 [网页运维说明](docs/operations.md#订阅网页与-3x-ui-面板开关)。

网页包含管理员总览、个人用量和机场专用页面。页面令牌独立于订阅令牌，可单独轮换；owner 是唯一管理员，通过独立随机入口及网页管理员账号、密码登录（密码至少 8 位，与 3x-ui 认证独立）。更新代码不会自动开放网页，也不会自动关闭原有公网面板；运行 `clash-sub web setup` 才初始化网页并关闭公网面板。代理和订阅下载不随面板开关停止。

`clash-sub web links` 输出 `/p/<页面令牌>-<email>/` 格式的可读网页地址；旧的无后缀网页地址继续有效。用户标识与令牌归属同时校验，原有 `/s/` 订阅链接保持不变。

owner 网页可管理用户链接、刷新 Amy，以及更新代码后生成配置。固定白名单操作由独立 systemd 后台任务执行，带会话与 CSRF 校验、幂等请求和脱敏执行记录；更新会重启网页，可能需要重新登录。

管理员成员列表可显示来自 3x-ui 在线接口的在线／离线／未知状态，每三十秒按需刷新；没有配置 `XUI_API_TOKEN` 时显示未知，不影响其他网页功能。凭据与运行方式见运维说明的“管理员在线状态”。

本地示例保留在 `previews/account/`，不含真实凭据。自建流量读取 3x-ui 所有入站当前的上传与下载累计（含已停用入站），随入站每月重置；不读取历史累计、不汇总用户用量，也不作为搬瓦工账单统计。

## 订阅流量元数据

订阅响应里的 `Subscription-Userinfo` 流量头不预写进任何配置文件：客户端请求订阅时，Nginx 把请求转发到 socket 激活的 `clash-sub-metadata.service`，服务按需读取 3x-ui 流量（五分钟缓存）与最近一次机场下载保存的流量，生成响应头；文件正文仍由 Nginx 直接发送。元数据服务不可用或超时的时候，同一份文件照常以 200 返回，只是缺少流量头，订阅本身不受影响。完整链路、缓存与降级行为见部署与运维手册。

## 本地文件

- `templates/base/Clash-Compat.yaml`：Compat 完整基础模板（已去除动态节点与机场痕迹）。
- `templates/dns/Clash-Balance.yaml`：只含 Balance 的完整 `dns:` 段及其注释。
- `templates/profiles.yaml`：两个 profile 的 DNS 配方与注入策略组列表。
- `private/clash-verge-home.js`：Git 忽略的本机 Home 扩展脚本，仅对 `Clash-Compat.yaml` 和 `Clash-Balance.yaml` 两个标题生效。

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
