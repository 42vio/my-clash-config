# 部署手册（apt/systemd Linux）

目标环境是重装后的干净 Linux amd64 VPS，资源约束约 512 MiB RAM、256 MiB Swap、10 GiB 磁盘。安装器不再按发行版或版本号拦截；当前安装步骤仍要求 `apt-get`、systemd 和 Debian 风格的 Nginx 目录布局，主要按 Debian 12+ 验证。除 3x-ui 本体外，全部部署由 `bash install.sh` 一条命令完成（Phase 2）。空闲时常驻进程只有 3x-ui 管理的 Xray 与宿主机 Nginx；Python 与 Mihomo 仅在同步和每日流量任务期间短暂运行。

公网仅开放 TCP 443（Nginx stream 按 SNI 分流）；10443 / 30443 / 20443 仅回环。不开放 UDP 443，不使用公网 1443。3x-ui 面板与原始订阅服务仅监听 127.0.0.1，永不直接暴露公网。

## 部署前准备清单（Cloudflare）

1. 域名 NS 托管在 Cloudflare。
2. 添加 DNS 记录（以 42io.cc 为例）：
   - `sub.<域名>` → A → VPS IP，**仅 DNS（灰云）**——订阅与面板入口，橙云会导致回源失败
   - `node.<域名>` → A → VPS IP，**仅 DNS（灰云）**——Reality 节点地址（默认 node. 子域，
     可用 `CLASH_SUB_NODE_HOST` 覆盖）；橙云代理不支持该 Reality 节点入口
   - 裸域 `@`/`www` 可自由使用橙云（网站等），与本方案无关
   - 不需要 panel 记录：面板经 `sub.<域名>/<随机路径>/` 访问
   - 安装器不再把本机 DNS 解析作为前置条件；仍需先确认 `sub`/`node` 的公网记录正确，否则部署后入口不可用
3. 创建 API Token：权限 Zone → DNS → Edit，Zone Resources 限定该域名。安装时粘贴一次。

## Phase 1：基础代理（手动，约 10 分钟）

1. 准备 amd64 Linux 系统，并确保可用 `apt-get` 与 systemd（推荐 Debian 12+）。
2. 安装 Git（首次拉取私有仓库必需）：

       apt-get install -y git

3. 使用 3x-ui 官方 Quick Start 命令安装当前默认版本，不传版本参数：

       bash <(curl -Ls https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh)

   安装时使用默认 SQLite 数据库，记下面板端口、访问路径和凭据。随后按
   [docs/3x-ui-setup.md](docs/3x-ui-setup.md) 创建 10443 入站与 owner 客户端；
   本仓库不固定 3x-ui 或 Xray 版本。
4. 接入本项目之前确认面板设置：
   - **Web 根路径（webBasePath）**：设为随机串（如 `/xui7k2m/`）。preflight 会校验它非 `/`，
     installer 直接读取该值作为面板反代路径——不再单独生成。
   - **面板监听**：设为 127.0.0.1（上游默认监听 0.0.0.0，会裸奔公网端口；本方案经
     nginx 反代访问，无需公网直连）。
   - **面板证书**：清空 `webCertFile` 与 `webKeyFile`，或在终端 `x-ui` 菜单执行
     **Revoke & Remove Certificate**。Nginx 以 HTTP 回源并统一终止公网 TLS；证书仍启用时
     preflight 会以 `panel_tls_unsupported` 停止。
   - 启用 Clash 订阅（subListen=127.0.0.1 默认即可）。
5. 建入站：协议 VLESS、端口 10443、listen 0.0.0.0、传输 TCP、Security=Reality
   （serverName 填第三方伪装域），添加 client（email 记住，作为 owner-email）。
   —— 此时代理已可用（公网 10443 直连）。

## Phase 2：整合 443（一条命令）

以 root：

    git clone https://github.com/42vio/my-clash-config /opt/my-clash-config && cd /opt/my-clash-config
    bash install.sh

交互输入：主域名、Cloudflare API Token、owner 的 client email（仅当 3x-ui 中恰好只有一个
启用的 client 时自动建议，回车即用；多个或零个启用 client 时必须明确输入；swap 扩容由
CLASH_SUB_SWAP_MB 环境变量决定，交互模式下也不询问）。
非交互（CI/重装）：`CLASH_SUB_DOMAIN=example.com CLASH_SUB_OWNER_EMAIL=owner-1 CLASH_SUB_SWAP_MB=1024 bash install.sh`
（CF Token 无环境变量，非交互场景下通过 stdin 提供；非交互必须设置 CLASH_SUB_OWNER_EMAIL，
否则安装以 owner_email_required 终止）。

installer 阶段：preflight（只读检查，含 3x-ui 设置与 443 空闲）→ 低配优化（swap/swappiness/journald）
→ 安装 nginx+stream 模块 → 自动安装 Mihomo 最新稳定版（官方 SHA-256 校验）→ acme.sh 签发 wildcard → 激活 443 分流与订阅/面板 TLS → systemd 自愈补齐
→ 生成 service.yaml → 报告。任一阶段失败即停止；重跑自动跳过已完成阶段（幂等）。
Mihomo 安装到 `/usr/local/lib/clash-sub/mihomo`，无需手工下载；后续只在明确执行菜单
“升级 Mihomo 校验器”或 `clash-sub mihomo-update` 时检查并升级，不随代码更新静默追新。
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
- `clash-sub update`：git pull + 依赖同步，随后以新代码进程执行 systemd/nginx 刷新（--post-update 自动接力；变更前自动快照）。update 不自动同步配置；涉及模板或生成逻辑的变更按成功提示继续执行 `clash-sub sync`（日常直接记 `clash-sub update && clash-sub sync`，等价于菜单选项 6）
- `clash-sub cert` / `clash-sub cert --renew`：证书状态 / 强制续期
- `clash-sub rollback --install`：回滚整合安装——移除 nginx 配置与 stream include（仅精确移除 installer 写入的块）、停用 systemd 资产、恢复默认站点（注：收口后 Reality 监听 127.0.0.1，回滚需同时在 3x-ui 面板把入站 listen 改回 0.0.0.0 才能恢复公网直连）
- 模板维护（`clash-sub template-sync`）是**开发机本地命令**，在维护者的 Mac 上把私密工作稿提升为公共模板；服务器不运行它，也永远不上传 `private/workbench/` 工作稿——服务器只通过 Git 与 `clash-sub update && clash-sub sync` 获得模板变更。

一次性升级说明：已在运行的旧版本（≤ 2026-08-26 之前部署）的 update 不会自动拉起新进程；首次升级请手动 `git pull` 后再运行 `clash-sub update`（或连续运行两次 update）。此后升级均为单命令。

日常运维（机场更新、流量、历史、版本回滚、轮换）见 [docs/operations.md](docs/operations.md)；
重装恢复与域名变更见 [docs/recovery.md](docs/recovery.md)。
