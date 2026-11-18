# my-clash-config 使用手册

这是当前搬瓦工 VPS 的代理和订阅项目。3x-ui 管理入站、客户端和用量，Nginx 在公网 `443` 按 SNI 分流，`clash-sub` 为各用户生成订阅。

当前配置核对日期为 2026-11-18。日常从本页查阅，安装细节和程序机制见[技术参考](docs/technical-reference.md)。服务器命令以 root 在 `/opt/my-clash-config` 执行，Mac 命令在本地仓库执行。

- [当前部署](#当前部署)
- [配置速查](#配置速查)
- [订阅和用量](#订阅和用量)
- [日常维护](#日常维护)
- [故障排查](#故障排查)
- [备份与恢复](#备份与恢复)

## 当前部署

| 用途 | 外部地址与 Cloudflare DNS | VPS 内部 |
| --- | --- | --- |
| REALITY 直连「洛杉矶」 | `node.42io.cc:443`，灰云 A + AAAA | `127.0.0.1:10443`，VLESS + TCP + REALITY |
| CDN「洛杉矶-CDN」 | `cdn.42io.cc:443`，橙云 CNAME 到 `node.42io.cc` | Nginx 在 `127.0.0.1:31443` 终止回源 TLS，再将 `/vless-ws` 转到 `127.0.0.1:11443` |
| Trojan 直连「洛杉矶-Trojan」 | `site.42io.cc:443`，灰云 CNAME 到 `node.42io.cc` | Nginx 按 `site.42io.cc` 的 SNI 透传到 `127.0.0.1:20443` |
| 订阅和管理页面 | `sub.42io.cc:443` | Nginx TLS 入口 `127.0.0.1:30443` |

VPS 换地址时，更新 `node` 的 A 和 AAAA。CDN 在 Cloudflare 开启 WebSockets，确认边缘证书覆盖 `cdn.42io.cc`，回源 TLS 用 **Full (strict)**。当前双栈 CNAME 的源站地址由 Cloudflare 选择。Trojan 的灰云记录直接解析到 VPS。

三个节点共用公网 TCP 443。空 SNI 和未知 SNI 被关闭，网页检查 HTTP Host。内部端口仅监听回环地址，公网另保留 SSH 26019。

REALITY 未通过验证的连接转到本机 HTTPS `127.0.0.1:18443`，支持 TLS 1.3 和 h2。Trojan 网页回落到 HTTP `127.0.0.1:18080`，公网 TLS 当前使用 HTTP/1.1。浏览器访问 `https://node.42io.cc/` 和 `https://site.42io.cc/` 都显示同一份工具网站。

## 配置速查



- 三条入站和 3x-ui 原始 Clash 订阅都监听 `127.0.0.1`。在「客户端」把入站关联到已有账号，沿用原订阅链接。现有 7 个客户端已关联三条线路。
- **REALITY** 的 Target 为 `127.0.0.1:18443`，Server Names 为 `node.42io.cc`，xVer 为 `0`。客户端连接地址和 SNI 都为 `node.42io.cc`，使用 Vision，沿用已有密钥和 Short ID。3x-ui 客户端设置中的 serverName 也与此一致。
- **CDN** 用 VLESS + WebSocket，路径 `/vless-ws`，Host 可留空。3x-ui 的 Security 和 VLESS Encryption 均为 `none`，客户端 Flow 留空。Nginx 负责公网 TLS，订阅发布时补上 CDN 的 SNI 和 WebSocket Host。
- **Trojan** 用 TCP（RAW）+ TLS，SNI 为 `site.42io.cc`，TLS ALPN 只选 `http/1.1`，ECH 留空。证书与私钥使用 `/etc/ssl/domain/fullchain.pem` 和 `/etc/ssl/domain/privkey.pem`，保留热加载。在「协议 → 回落」加默认目标 `127.0.0.1:18080`，Xver 为 `0`，子入站、Name、ALPN、Path 留空。Xray 解密后把网页请求交给 Nginx 的 HTTP 首页。页面内容在 [assets/site-home.html](assets/site-home.html)。

回落首页是一个单文件 AI 开发工具箱，提供提示词模板、JSON 格式化、时间戳换算和本地收藏。文字和数据在浏览器中处理，收藏仅存于当前浏览器。

VPS 私有配置文件为 `/opt/my-clash-config/private/config/service.yaml`。当前部署的可选配置如下，原文件其他字段继续保留。

```yaml
xui-reality-hostname: node.42io.cc
xui-cdn:
  hostname: cdn.42io.cc
  websocket-path: /vless-ws
xui-trojan-hostname: site.42io.cc
```

`xui-public-endpoint` 为 `node.42io.cc:443`，与 REALITY hostname 一致。上述设置生成本机 HTTPS 目标和严格 SNI 分流。修改后执行 `clash-sub update`，再执行 `clash-sub sync`。

通配符证书覆盖 `*.42io.cc` 和 `42io.cc`，复用现有证书及续期流程，网页只维护 `assets/site-home.html` 一份文件。

### 代理出站规则

当前 Xray 路由先保留 API、BitTorrent 和私网屏蔽规则，再依次添加以下规则。

1. `full:www.gstatic.com` 走 `direct`，用于当前健康检查。
2. `geosite:cn` 走 `blocked`。
3. `geoip:cn` 走 `blocked`。

`domainStrategy` 为 `IPOnDemand`，域名请求按需要解析后参与 IP 匹配。这些规则作用于认证后的代理请求，网站回落使用本机目标。更换测速网址时核对精确域名例外。

## 订阅和用量

### 订阅选哪份

| 用途 | 选哪个 |
| --- | --- |
| 日常 Clash 配置 | `Clash-Compat.yaml`。owner 同时有 AmyTelecom，普通客户端只拿自己的自建节点 |
| 另一套 DNS | owner 的 `Clash-Balance.yaml`，只换 DNS 配方 |
| 只用机场 | owner 的 `Clash-Airport.yaml`，有单独的订阅令牌 |
| 只导入节点 | `Nodes.txt`，只含该用户的自建节点 |
| VPS 套餐总用量 | 管理页「搬瓦工套餐用量」。3x-ui 的入站计数和个人额度单独显示 |

忘记订阅链接用 `clash-sub links`，忘记网页入口用 `clash-sub web links`。管理员、个人和机场网页的链接各自独立。备用来源在「订阅来源 → 备用节点管理」维护，只影响符合条件用户的 Compat 配置。

### 节点排序与网络选择

「🏴‍☠️ 自建节点」是 `fallback` 组，从列表前面优先选可用节点。当前已发布顺序为 **Trojan → CDN → REALITY**。要调整顺序，按下面两步操作。

1. 在 **3x-ui → 入站 → 编辑**里设置「订阅排序（Subscription sort index）」。当前 Trojan 为 `1`，CDN 为 `2`，REALITY 为 `3`。数值相同时按入站 ID 排。
2. 在 VPS 执行 `clash-sub sync`，然后让客户端刷新原订阅。

排序只决定先试哪条节点。线上 Compat 和 Balance 的全局 `ipv6` 为 `true`，三个节点都沿用客户端默认的 `ip-version`。需要绕开源站直连故障时，可以手动选 CDN。 [3x-ui 排序说明](https://github.com/MHSanaei/3x-ui/issues/4835)

当前健康检查地址为 `https://www.gstatic.com/generate_204`，预期状态 `204`，间隔 `600` 秒，`lazy: true`。全局 `ipv6: true`、`dns.ipv6: false`，节点地址族沿用默认值，实际选择取决于客户端与解析结果。

### 用量和定时重置

- 搬瓦工套餐用量反映整台 VPS 的账单口径。三个入站共用同一台 VPS 的套餐，分别填写入站额度会形成各自限制。
- 3x-ui 客户端额度、入站累计和搬瓦工套餐各自统计，管理员页面分别展示。个人页只显示本人用量。
- 终端 `clash-sub` 菜单 **4 → 9** 查看搬瓦工用量、配置 API 或按账期设置重置。凭据隐藏输入。
- **4 → 9 → 2** 先显示预览，确认后每月调用 3x-ui 清零所有客户端和所有入站的上传、下载累计，包含停用入站。需要通过 **4 → 8** 设置有相应权限的 API 令牌。启用后将 3x-ui 原有自动重置设为 Never，避免重复清零。
- 定时任务清零 3x-ui 计数，搬瓦工账单由服务商按周期重置。查看任务用 `systemctl list-timers clash-sub-inbound-reset.timer`，记录用 `journalctl -u clash-sub-inbound-reset.service`。
- AmyTelecom 用量来自最近一次服务器刷新。页面显示的每月 19 日重置规则用于展示。

## 日常维护

### 更新代码和发布订阅

```bash
cd /opt/my-clash-config
clash-sub backup
clash-sub update
clash-sub sync
nginx -t
clash-sub status
```

`update` 创建快照、拉取代码并更新服务和 Nginx，`sync` 生成用户配置，完成后客户端刷新订阅。修改入站或用户后执行 sync。网站修改在 `assets/site-home.html`，提交推送后由服务器 update 拉取。

### 网页和链接管理

| 要做的事 | 命令 |
| --- | --- |
| 首次设置网页管理员账号密码 | `clash-sub web setup` |
| 查看网页入口 | `clash-sub web links` |
| 开关订阅网页 | `clash-sub web web-on` / `clash-sub web web-off` |
| 开关 3x-ui 公网面板 | `clash-sub web panel-on` / `clash-sub web panel-off` |
| 获取面板 SSH 隧道说明 | `clash-sub web tunnel` |
| 更换用户网页链接 | `clash-sub web rotate-page <用户ID>` |
| 修改网页管理员密码 | `clash-sub web reset-password` |

网页地址和订阅地址分别轮换。关闭网页保留客户端订阅下载和代理服务。管理员网页支持查看和轮换链接、刷新机场、更新代码、生成配置。

网页管理员账号密码独立设置。桌面实时速度功能当前使用网页登录时的账号密码建立本机 3x-ui 会话，需要两边账号密码一致；当前自动登录不支持 3x-ui 两步验证。会话和权限细节见[技术参考](docs/technical-reference.md#管理网页与服务)。

### 用户管理

普通用户与 owner 都来自 3x-ui client；在面板中增删或启停 client 后执行 `clash-sub sync` 即可生效。owner 由安装时输入的 email 确定；owner 身份变化才执行：

```bash
clash-sub reinitialize-owner <用户ID>
clash-sub sync
```

重新初始化只重建身份映射；稳定 provider 已在服务器上时无需重新导入机场，直接 `sync` 即可。

### 更新机场与备用节点

运行 `clash-sub`，进入 **1「订阅来源」**。

| 操作 | 使用时机 |
| --- | --- |
| 设置订阅开关页面 | 首次设置或更换机场 |
| 自动开启订阅并刷新 | 日常刷新 |
| 手动开启订阅后刷新 | 自动开关失效，先在浏览器开启 |
| 使用新订阅链接更新 | 人工输入新链接覆盖 |
| 查看机场状态 | 检查用量、来源和更新时间 |

服务器每天北京时间 06:00–08:00 自动尝试刷新机场。客户端 provider 自动刷新间隔为 24 小时，立即更新可在 Clash Verge 中手动刷新 `AmyTelecom` provider。机场刷新更新 provider，代码或自建节点变更后另执行 sync。

备用节点在「订阅来源 → 备用节点管理」维护，支持订阅和手动 URI。默认仅分配给 `42vio`，通过菜单 **7「选择适用用户并生成配置」** 自行调整。输入用户 ID，以逗号或空格分隔；`all` 选择当前所有用户，`none` 清空，`default` 恢复默认仅 `42vio`，回车取消。

同一选择范围用于全部备用订阅节点和手动节点，仅加入所选用户的 Compat。明确选择按 3x-ui 用户 ID 保存，改名保留关联；明确选择后，新增用户需要再次选择。停用用户可预先选择，启用并同步后生效。保存选择会重新生成订阅，客户端刷新标准订阅后生效；没有备用节点时可先保存选择，稍后导入。

Balance、纯机场和仅节点订阅沿用各自来源。管理网页分别展示当前选择范围和实际已发布备用的用户；更改范围使用上述终端菜单。支持协议和去重规则见[技术参考](docs/technical-reference.md#自建备用节点来源)。

### 同步 Mac 的 iCloud 模板

当前手动来源为 **iCloud Drive → Clash**，在本地仓库显式指定两个文件。

```bash
icloud_clash="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Clash"
.venv/bin/python3.14 bin/clash-sub template-sync   --compat "$icloud_clash/Clash-Compat.yaml"   --balance "$icloud_clash/Clash-Balance.yaml"
git diff --check
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
```

Compat 更新完整基础模板，Balance 只提取 DNS。检查差异后提交推送，服务器再 update、sync。不传参数时脚本仍使用旧 ClashX 容器默认路径，当前维护使用上述显式参数。

### Home、证书和组件

Home 节点、`HomeServer` 与 `ProxyServer` 分组只存在于本机 `private/clash-verge-home.js`。修改 Home 时直接编辑脚本，随后：

```bash
node --check private/clash-verge-home.js
```

语法通过后在 Clash Verge 中重新载入配置验证效果。脚本只对 `Clash-Compat.yaml` 和 `Clash-Balance.yaml` 两个标题生效；其他 profile 原样返回。不要把脚本内容或其中的节点值写入仓库、文档或终端记录。

先查看证书，再按需要强制续期；`--domain` 不是改域名入口，会被拒绝：

```bash
clash-sub cert
clash-sub cert --renew
clash-sub status
```

单独升级 Mihomo 校验器时：

```bash
clash-sub mihomo-update
clash-sub sync
clash-sub status
```

## 故障排查

先查看 `clash-sub status`，根据症状定位。`links` 输出包含访问凭据，故障记录保留用户 ID 与错误码。

| 症状 | 检查顺序 |
| --- | --- |
| 网站打不开 | DNS → 当前网络到公网 443 → Nginx 与证书 |
| 网站正常而 REALITY 失败 | 刷新订阅 → SNI 为 node → Server Names → Target 为 `127.0.0.1:18443` |
| Trojan 回落失败 | VPS 访问 `http://127.0.0.1:18080/` → 浏览器访问 site → 真实代理验证 |
| CDN 失败 | 橙云、WebSockets、Full (strict) → 路径和 Host → 31443、11443 监听 |
| 间歇性测速失败 | 分别测试当前网络、地址族和检测网址，观察真实代理请求失败率 |
| 订阅能下载但没有用量 | metadata socket、服务日志和数据库读取 |
| `airport_provider_required` | 刷新机场、检查 provider 权限后同步 |
| 发布中断 | `clash-sub recover` → Nginx 检查 → status → 重试同步 |

```bash
nginx -t
systemctl status nginx x-ui clash-sub-metadata.socket
journalctl -u nginx -u x-ui -u clash-sub-metadata.service -n 100 --no-pager
```

同一网络上的健康检查波动可能触发 fallback 切换。网页和代理分别验证，各错误码和缓存机制见[技术参考](docs/technical-reference.md#故障检查顺序)。

## 备份与恢复

### 保存当前服务器

执行 `clash-sub backup`，归档写入仓库 `backups/`，权限为 `0600`。包含 3x-ui 数据库、两份 Nginx 配置、`state.json` 和 `airport-source.json`，启用网页时额外包含 `web-settings.json`。

证书、provider 正文、发布历史和项目私有配置另行维护。当前自有 REALITY 网站依赖 `private/config/service.yaml` 中的可选字段，请把这份文件也保存到受保护的备份位置。备份含访问凭据，离线或加密保管。

### 历史与回退

先从 `status` 或 `links` 确认目标用户 ID，再查看该用户可用发布版本：

```bash
clash-sub history <用户ID>
clash-sub rollback <用户ID> <发布版本ID>
clash-sub status
clash-sub links
```

回滚边界：只回滚主配置和相应路由映射；机场 provider 文件、身份令牌与本机 Home 脚本不参与回滚。只有历史中存在的版本可以回退。泄露订阅链接时，轮换对应用户的令牌并重新取链接；旧链接立即失效：

```bash
clash-sub rotate-link <用户ID>
clash-sub links
```

owner 轮换会以新令牌 URL 重新生成三份主配置；provider 文件本身不移动，机场上游不会被再次联系。普通用户轮换只改路由，不触碰 provider。

旧配置若仍使用第三方 REALITY SNI，需要协调服务端和客户端参数。撤销整合安装使用 `clash-sub rollback --install`，完整边界见[技术参考](docs/technical-reference.md#升级与卸载)。

### 重装或迁移 VPS

1. 旧服务器备份，另保存私有服务配置，保留旧服务直到新环境验收。
2. 新服务器恢复 3x-ui 数据库、入站和用户，恢复运行时状态和机场来源记录。
3. 按[全新安装](docs/technical-reference.md#全新安装)安装项目并签发证书。
4. 恢复当前私有配置字段，生成支持 h2 的本机 HTTPS 目标和 SNI 分流，核对三个节点的关联设置。
5. 从恢复的机场来源重建 provider，执行 sync，检查用户权限与订阅。
6. 实测三个代理和网站回落后切换 DNS、客户端，稳定观察后处理旧主机。

沿用链接需要保持 3x-ui client ID、owner 映射和订阅令牌。域名变更按新环境迁移，`cert --domain` 无法用于原地改名。完整恢复细节及路径表见[技术参考](docs/technical-reference.md)。

历史设计和计划保存在 [docs/superpowers](docs/superpowers)，各文件注明历史用途。当前运行结果见[验证记录](docs/technical-reference.md#验证记录)。
