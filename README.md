# my-clash-config 项目总览

这是搬瓦工 VPS 的代理和订阅项目。3x-ui 管理入站、客户端和用量，Nginx 在公网 `443` 按域名的 TLS SNI 分流，`clash-sub` 把每个客户端关联的节点发布到自己的订阅。

## 域名和端口

| 用途 | 外部地址与 Cloudflare DNS | VPS 内部 |
| --- | --- | --- |
| REALITY 直连「洛杉矶」 | `node.42io.cc:443`，灰云 A + AAAA | `127.0.0.1:10443`，VLESS + TCP + REALITY |
| CDN「洛杉矶-CDN」 | `cdn.42io.cc:443`，橙云 CNAME 到 `node.42io.cc` | Nginx 在 `127.0.0.1:31443` 终止回源 TLS，再将 `/vless-ws` 转到 `127.0.0.1:11443` |
| Trojan 直连「洛杉矶-Trojan」 | `site.42io.cc:443`，灰云 CNAME 到 `node.42io.cc` | Nginx 按 `site.42io.cc` 的 SNI 透传到 `127.0.0.1:20443` |
| 订阅和管理页面 | `sub.42io.cc:443` | Nginx TLS 入口 `127.0.0.1:30443` |

VPS 换地址时，更新 `node` 的 A 和 AAAA。CDN 在 Cloudflare 开启 WebSockets，确认边缘证书覆盖 `cdn.42io.cc`，回源 TLS 用 **Full (strict)**。当前双栈 CNAME 的源站地址由 Cloudflare 选择。Trojan 的灰云记录直接解析到 VPS。

## 3x-ui 设置速查

- 三条入站和 3x-ui 原始 Clash 订阅都监听 `127.0.0.1`。在「客户端」把入站关联到已有账号，沿用原订阅链接。现有 7 个客户端已关联三条线路。
- **REALITY** 保留原有 SNI、客户端 Vision 和 Short ID。客户端连接的是 `node.42io.cc`，握手使用配置里原有的 REALITY SNI。
- **CDN** 用 VLESS + WebSocket，路径 `/vless-ws`，Host 可留空。3x-ui 的 Security 和 VLESS Encryption 均为 `none`，客户端 Flow 留空。Nginx 负责公网 TLS，订阅发布时补上 CDN 的 SNI 和 WebSocket Host。
- **Trojan** 用 TCP（RAW）+ TLS，SNI 为 `site.42io.cc`，TLS ALPN 只选 `http/1.1`，ECH 留空。证书与私钥使用 `/etc/ssl/domain/fullchain.pem` 和 `/etc/ssl/domain/privkey.pem`，保留热加载。在「协议 → 回落」加默认目标 `127.0.0.1:18080`，Xver 为 `0`，子入站、Name、ALPN、Path 留空。Xray 解密后把网页请求交给 Nginx 的 HTTP 首页。页面内容在 [assets/site-home.html](assets/site-home.html)。

回落首页是一个单文件 AI 开发工具箱，提供提示词模板、JSON 格式化、时间戳换算和本地收藏。文字和数据在浏览器中处理，收藏仅存于当前浏览器。

VPS 私有配置文件为 `/opt/my-clash-config/private/config/service.yaml`。CDN 和 Trojan 在当前部署中的配置为：

```yaml
xui-cdn:
  hostname: cdn.42io.cc
  websocket-path: /vless-ws
xui-trojan-hostname: site.42io.cc
```

## 节点排序与网络选择

「🏴‍☠️ 自建节点」是 `fallback` 组，从列表前面优先选可用节点。当前已发布顺序为 **Trojan → CDN → REALITY**。要调整顺序，按下面两步操作。

1. 在 **3x-ui → 入站 → 编辑**里设置「订阅排序（Subscription sort index）」。当前 Trojan 为 `1`，CDN 为 `2`，REALITY 为 `3`。数值相同时按入站 ID 排。
2. 在 VPS 执行 `clash-sub sync`，然后让客户端刷新原订阅。

排序只决定先试哪条节点。线上 Compat 和 Balance 的全局 `ipv6` 为 `true`，三个节点都沿用客户端默认的 `ip-version`。需要绕开源站直连故障时，可以手动选 CDN。 [3x-ui 排序说明](https://github.com/MHSanaei/3x-ui/issues/4835)

2026-11-18 验收时，Cloudflare CDN 的 IPv4 和 IPv6 代理都已跑通。Trojan 的公网 IPv6 网页回落返回 `200`，VPS 本机通过 Nginx 测真实 Trojan 认证代理成功。Mac 所在网络直连 VPS 的 IPv4 `443` 仍超时。

## 订阅和用量

| 用途 | 选哪个 |
| --- | --- |
| 日常 Clash 配置 | `Clash-Compat.yaml`。owner 同时有 AmyTelecom，普通客户端只拿自己的自建节点 |
| 另一套 DNS | owner 的 `Clash-Balance.yaml`，只换 DNS 配方 |
| 只用机场 | owner 的 `Clash-Airport.yaml`，有单独的订阅令牌 |
| 只导入节点 | `Nodes.txt`，只含该用户的自建节点 |
| VPS 套餐总用量 | 管理页「搬瓦工套餐用量」。3x-ui 的入站计数和个人额度单独显示 |

忘记订阅链接用 `clash-sub links`，忘记网页入口用 `clash-sub web links`。管理员、个人和机场网页的链接各自独立。备用来源在「订阅来源 → 备用节点管理」维护，只影响符合条件用户的 Compat 配置。 [运维手册](docs/operations.md)

## 平时怎么维护

| 要做的事 | 入口 |
| --- | --- |
| 新增、停用或调整入站 | 先在 3x-ui 改，执行 `clash-sub sync`，在客户端刷新订阅 |
| 将 GitHub 的代码更新到 VPS | 在 `/opt/my-clash-config` 执行 `clash-sub update`，然后 `nginx -t` 和 `clash-sub sync` |
| 看状态或备份 | `clash-sub status` / `clash-sub backup` |
| 回到一份旧订阅 | 先用 `clash-sub history <用户ID>` 查版本，再执行用户级回滚 |
| 暂停 Trojan | 在 3x-ui 停用入站后同步订阅。若也要移除网页入口，再去掉私有配置中的 `xui-trojan-hostname` 并重生成 Nginx 配置 |

排查 Trojan 回落时，先在 VPS 访问 `http://127.0.0.1:18080/` 确认首页，再从浏览器用 HTTPS 访问 `site.42io.cc`，最后用真实 Trojan 客户端验证代理。浏览器首页和代理要分别通过。

## Mac 上的模板

原版模板在 **iCloud Drive → Clash**。在仓库根目录运行：

```bash
icloud_clash="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Clash"
.venv/bin/python3.14 bin/clash-sub template-sync \
  --compat "$icloud_clash/Clash-Compat.yaml" \
  --balance "$icloud_clash/Clash-Balance.yaml"
```

Compat 是完整基础模板，Balance 只导入 DNS 段。同步会净化模板，先核对生成差异再发布。 [模板说明](docs/template-design.md)

重装 VPS 从 [部署清单](DEPLOYMENT.md) 开始。恢复备份、面板开关、用量和订阅排错的完整操作见 [运维手册](docs/operations.md)。
