# 部署手册（Debian 12）

目标环境是重装后的干净 Debian 12 amd64 VPS，资源约束约 512 MiB RAM、256 MiB Swap、10 GiB 磁盘。除 3x-ui 本体外，全部部署由 `bash install.sh` 一条命令完成（Phase 2）。空闲时常驻进程只有 3x-ui 管理的 Xray 与宿主机 Nginx；Python 与 Mihomo 仅在同步和每日流量任务期间短暂运行。

公网仅开放 TCP 443（Nginx stream 按 SNI 分流）；10443 / 30443 / 20443 仅回环。不开放 UDP 443，不使用公网 1443。3x-ui 面板与原始订阅服务仅监听 127.0.0.1，永不直接暴露公网。

## 部署前准备清单（Cloudflare）

1. 域名 NS 托管在 Cloudflare。
2. 添加 DNS 记录：`sub.<你的域名>` → A 记录 → VPS 公网 IP（仅此一条必须；Reality 客户端连的 server 也用这个域名）。
3. A 记录须为「仅 DNS」（灰云，不开橙色云代理），否则解析到 Cloudflare 节点会导致 preflight 失败且 Reality 不可用。
4. 创建 API Token：权限 Zone → DNS → Edit，Zone Resources 限定该域名。安装时粘贴一次。

## Phase 1：基础代理（手动，约 10 分钟）

1. Debian 12 最小安装，`apt update && apt upgrade`。
2. 安装 3x-ui（官方脚本），记下面板端口/路径/凭据。版本 pin 与人工加固细节（2FA、随机
   Web Base Path、回环面板/订阅）见 [docs/3x-ui-setup.md](docs/3x-ui-setup.md)；
   该文的公网入站端口在本拓扑下为 10443（不再是 443）。
3. 安装 Mihomo 校验二进制（`clash-sub sync` 的配置校验依赖它）：

   从 https://github.com/MetaCubeX/mihomo/releases 下载 linux-amd64（与 CI 无关，
   固定一个 release 版本即可，下载后建议核对 release 的 sha256），解压后先
   `mkdir -p /usr/local/lib/clash-sub`，再把
   二进制放到 `/usr/local/lib/clash-sub/mihomo` 并 `chmod 755`。
4. 面板设置：启用 Clash 订阅（subListen=127.0.0.1 默认即可）。
5. 建入站：协议 VLESS、端口 10443、listen 0.0.0.0、传输 TCP、Security=Reality
   （serverName 填第三方伪装域），添加 client（email 记住，作为 owner-email）。
   —— 此时代理已可用（公网 10443 直连）。

## Phase 2：整合 443（一条命令）

以 root：

    git clone <repo> /opt/my-clash-config && cd /opt/my-clash-config
    bash install.sh

交互输入：主域名、Cloudflare API Token（swap 扩容由 CLASH_SUB_SWAP_MB 环境变量决定，交互模式下也不询问）。
非交互（CI/重装）：`CLASH_SUB_DOMAIN=example.com CLASH_SUB_OWNER_EMAIL=owner-1 CLASH_SUB_SWAP_MB=1024 bash install.sh`
（CF Token 无环境变量，非交互场景下通过 stdin 提供）。

installer 阶段：preflight（只读检查，含 DNS 前置与 443 空闲）→ 低配优化（swap/swappiness/journald）
→ 安装 nginx+stream 模块 → acme.sh 签发 wildcard → 激活 443 分流与订阅/面板 TLS → systemd 自愈补齐
→ 生成 service.yaml → 报告。任一阶段失败即停止；重跑自动跳过已完成阶段（幂等）。
模块自检说明：无需单独验证 stream 模块——安装阶段的 nginx 配置激活步骤会先跑
`nginx -t`，模块缺失时立即失败。

## Phase 3：收口（手动一步）

3x-ui 面板把 Reality 入站 listen 从 0.0.0.0 改为 127.0.0.1（端口保持 10443）。
之后公网仅剩 443。运行 `clash-sub sync` 生成订阅，`clash-sub links` 查看 URL。

## 部署后验证清单

~~~bash
ss -tlnp | grep -E '443|10443|30443'
~~~

- `ss` 输出：443=nginx；10443/30443 仅 127.0.0.1（收口后）
- `ss -tlnp | grep ':80 '`：应无输出（默认站点已移除；如有输出说明 default site 残留）
- `clash-sub sync && clash-sub links`：订阅 URL 以 https://sub.<域名>/s/<token>/ 开头（443 端口）
- `clash-sub status`：状态正常；`clash-sub cert`：证书到期时间
- 面板：install 报告输出的 https://sub.<域名>/<panel-path>/
- 中断恢复：installer 崩溃后重跑 `bash install.sh`（幂等）；错误激活自动还原（nginx -t 失败即回滚本次写入）

## 日常管理

- `clash-sub backup`：全量备份到 backups/（含 x-ui.db、运行时 private/、nginx 配置）
- `clash-sub update`：git pull + 依赖同步 + nginx 配置重渲染（变更前自动快照）
- `clash-sub cert` / `clash-sub cert --renew`：证书状态 / 强制续期
- `clash-sub rollback --install`：回滚整合安装（Reality 回到公网 10443 直连，代理不断）（注：收口后 Reality 监听 127.0.0.1，回滚需同时在 3x-ui 面板把入站 listen 改回 0.0.0.0 才能恢复公网直连）

日常运维（机场更新、流量、历史、版本回滚、轮换）见 [docs/operations.md](docs/operations.md)；
重装恢复与域名变更见 [docs/recovery.md](docs/recovery.md)。
