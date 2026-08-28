# my-clash-config：轻量私有 Clash 订阅服务

对外统一使用 **Clash** 命名；服务器上唯一的管理命令是 `clash-sub`。本仓库从每人的 3x-ui 客户端、owner 的
机场快照与家庭节点出发，渲染 `balanced` / `standard` / `privacy` 三种完整
配置，经固定版本 Mihomo 真实校验后原子发布，由宿主机 Nginx 通过高强度
随机令牌路径**静态只读**发布。平时没有任何 Python、Mihomo 或转换进程常驻。

> **链接即密码：** 任何拿到订阅链接的人都能下载该链接展开后的全部节点凭据。
> 订阅链接只通过受信任渠道分发；一旦泄漏，立即 `clash-sub rotate-link <user-id>`
> 并在 3x-ui 面板撤销该用户的客户端凭据。

## 信任模型

| 角色 | 配置中允许的节点来源 | 可获得的 variant |
| --- | --- | --- |
| owner | 自己的 3x-ui 客户端 + 最新机场快照 + 自维护家庭节点 | `balanced`、`standard`、`privacy` |
| 普通用户（member） | 仅自己的 3x-ui 客户端 | 仅 `standard` |

普通用户之间完全隔离：输出中不含 owner 或其他任何用户的节点、名称或凭据。
每位用户持有独立令牌与独立 3x-ui 客户端（独立 UUID、配额、到期时间），
泄漏时可单独轮换、单独撤销。

## 架构决策记录（2026-08 更新）

本版使用 Nginx stream 统一 443 入口：

- 公网仅开放 443：`ssl_preread` 按 SNI 分流——`sub.<域名>` → 127.0.0.1:30443（订阅+面板，终止 TLS），
  其余任意 SNI → 127.0.0.1:10443（Xray Reality，不终止 TLS）；
  `trojan.<域名>` → 127.0.0.1:20443 为预留规则（后期扩展见 docs/recovery.md 预留说明）。
- Reality inbound 监听 127.0.0.1:10443；节点地址默认 `node.<域名>`（灰云，可用 `CLASH_SUB_NODE_HOST` 覆盖），客户端统一连 443（订阅层把节点端口改写为 443）。
- 证书：acme.sh DNS-01（Cloudflare）wildcard，统一 /etc/ssl/domain/；公网不再开放 80。
- 部署：`bash install.sh` 一键完成 443 整合（详见 DEPLOYMENT.md）；3x-ui 仍手动安装。
- clash-sub 定位：本 VPS clash 订阅栈的全生命周期管理 CLI（install/backup/update/cert/rollback）。
- 注意：stream 层未启用 PROXY protocol（会破坏 Reality 默认路由），30443 上的限流 bucket 实际为全局。

## 端口与监听

| 端口 | 归属 | 状态 |
| --- | --- | --- |
| TCP 443 | Nginx stream（唯一公网端口） | 公网开放，按 SNI 分流：`sub.<域名>` → 30443、`trojan.<域名>` → 20443、其余 → Reality 10443 |
| TCP 10443 | Xray（VLESS + RAW/TCP + REALITY） | 仅 127.0.0.1（部署收口后） |
| TCP 30443 | Nginx：订阅 + 面板 TLS | 仅 127.0.0.1 |
| SSH | sshd | 由管理员指定，本项目不更改 |
| 3x-ui 面板与原始订阅服务 | 回环 `127.0.0.1` | 永不直接暴露公网 |

不开放 UDP 443，不使用公网 1443。

## 数据流

```text
3x-ui SQLite（只读发现客户端）──┐
3x-ui 回环 Clash 订阅（每人 subId）─┤
机场 Clash 快照（仅 owner）────────┤
家庭节点（仅 owner，私有文件）──────┤
基础模板 + variant 差异────────────┤
                                  ▼
              clash-sub 手动同步（渲染 + 结构/泄漏校验）
                                  ▼
              固定版本 Mihomo 真实配置校验（按需运行）
                                  ▼
              原子发布（每用户仅保留最近 5 个成功版本）
                                  ▼
              宿主机 Nginx 443（stream 分流 → 30443）静态发布 ──> 各自的 Clash 客户端
```

空闲时常驻进程只有 3x-ui 管理的 Xray 和宿主机 Nginx；同步与每日流量任务
结束后，Python 与 Mihomo 进程全部退出。

## 三种 variant

公共策略只有一个事实来源：`templates/clash.yaml`（公共 DNS、策略组、
rule-provider 与 rules，`proxies` 恒为空）。差异按层组合：

- `templates/features/home.yaml`——家庭功能差异（`HomeServer`/`ProxyServer`
  组、家庭网段规则、向公共组追加的成员与节点注入目标），只进入
  `balanced` 与 `privacy`；
- `templates/variants/privacy-dns.yaml`——privacy 独有的最小 DNS 覆盖
  （递归合并，列表整体替换）；
- `templates/variants/manifest.yaml`——声明每个 variant 组合哪些
  feature/override，以及全局节点注入组（`加速线路`、`AI服务`）。
  组合授权由代码锁定，manifest 不能扩大任何数据源权限。

| 输出 | owner 节点范围 | 普通用户 | 用途 |
| --- | --- | --- | --- |
| `balanced` | 3x-ui + 机场 + 家庭 | 不发布 | 通用完整配置 |
| `standard` | 3x-ui + 机场（不含家庭） | 仅本人 3x-ui 节点 | 标准跨平台配置，默认发给其他用户 |
| `privacy` | 3x-ui + 机场 + 家庭 | 不发布 | 隐私优先配置（Fake-IP DNS） |

订阅地址形状（Token 是唯一授权凭据，识别码不能单独下载）：

```text
普通用户：https://sub.<域名>/s/<token>/clash-standard.yaml
owner：  https://sub.<域名>/s/<token>/clash-<balanced|standard|privacy>.yaml
```

## 日常管理：只记一个命令

```bash
clash-sub
```

无参数命令显示循环式交互菜单（操作结束回到菜单，`0`/EOF/Ctrl-C 退出；
轮换链接、强制续期、用户回退、owner 重新初始化与安装回滚均需二次确认）。
主菜单保留日常操作，其余功能在四个二级菜单：

```text
╔──────────────────────────────────────────────╗
│  clash-sub 管理脚本                          │
│  0. 退出                                     │
│──────────────────────────────────────────────│
│  1. 更新机场订阅                             │
│  2. 重新生成所有配置                         │
│  3. 查看订阅链接                             │
│  4. 查看运行状态                             │
│──────────────────────────────────────────────│
│  5. 程序维护                                 │
│  6. 证书管理                                 │
│  7. 备份与恢复                               │
│  8. 用户与版本                               │
╚──────────────────────────────────────────────╝
```

```text
程序维护（5）               证书管理（6）
1. 更新代码并同步配置（推荐）  1. 查看证书状态
2. 仅更新仓库代码            2. 强制续期证书
3. 升级 Mihomo 校验器        0. 返回主菜单
0. 返回主菜单

备份与恢复（7）             用户与版本（8）
1. 创建完整备份              1. 查看用户历史版本
2. 恢复中断的配置发布         2. 回退用户版本
3. 回滚整合安装              3. 轮换用户订阅链接
0. 返回主菜单                4. 重新初始化 owner
                            0. 返回主菜单
```

ANSI 颜色只在真实交互终端启用；管道与重定向输出、订阅 URL 等需要复制的
值均不带颜色码。

`clash-sub update` 保持原语义（快照 → git pull → pip → 新进程 post-update），
不自动同步；成功后明确提示继续执行 `clash-sub sync`（或以后直接
`clash-sub update && clash-sub sync`）。「程序维护 → 更新代码并同步配置」
等价于这条组合，但 sync 由 pull 后磁盘入口启动的新进程执行；update 之后
菜单退出，不继续使用旧模块对象。

不需要记住 refresh 之类的命令——它不存在；systemd 与排错用的非交互子命令
（`sync` / `traffic-update` / `status` / `links` / `history` / `rollback` /
`rotate-link` / `mihomo-update`）见 [docs/operations.md](docs/operations.md)。

## 明确不做

- **不提供短链**：短链接会成为第二套 bearer 凭据和轮换状态。
- **没有在线转换页面**，不部署 Docker / subconverter / Subweb。
- **没有定时生成**：配置只在机场导入成功或显式同步时重建；订阅请求到达时
  不生成配置、不做实时查询，Clash 客户端只读到最近一次发布的静态 YAML。
- **不启用 Telegram 提醒**，不提供流量状态网页。
- **不自动安装或修改 3x-ui**：部署文档只提供人工步骤与只读检查命令。
- **不把任何真实节点、订阅地址、UUID、密码、REALITY 密钥或公开 Token 提交
  到 Git**；仓库私有不改变此规则。

## 文档

| 文档 | 内容 |
| --- | --- |
| [DEPLOYMENT.md](DEPLOYMENT.md) | apt/systemd Linux 服务器部署：3x-ui 手动 + `install.sh` 一键整合 443 |
| [docs/3x-ui-setup.md](docs/3x-ui-setup.md) | 3x-ui 官方安装与接入本项目所需的面板配置 |
| [docs/operations.md](docs/operations.md) | 日常运维：机场更新、流量、历史、回滚、轮换、故障恢复 |
| [docs/recovery.md](docs/recovery.md) | 重装恢复、域名变更与预留扩展（Trojan / 第二台 VPS） |
| [docs/private-data.md](docs/private-data.md) | 私有数据布局、权限、备份与恢复边界 |

`docs/` 目录中另有一份旧服务器拓扑的历史记录文档，仅作历史说明，不是
本项目的部署或运维步骤。

## 开发

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/scan_tracked_secrets.py            # 跟踪文件敏感信息扫描
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
```

模板数据流：在 Mac 上维护一份**完整私密工作稿**
`private/workbench/balanced.yaml`（`0600`，被 Git 忽略），在本机 Clash
导入实测通过后，运行本地命令 `clash-sub template-sync` 把公共策略安全提升
进 `templates/`（剥离全部动态节点、拆出家庭差异、合成节点重渲染 +
Mihomo + 泄漏校验全部通过后才原子替换）。之后查看 `git diff`、跑测试、
提交并 push；服务器照常只执行 `clash-sub update && clash-sub sync`，
工作稿永远不上传服务器。详见 [docs/operations.md](docs/operations.md)
的「本地模板工作流」一节。

安全约定：真实订阅 URL、令牌、UUID、节点密码、REALITY 密钥、机场临时 URL、
生成结果与含凭据的 release 元数据一律不进入 Git（`private/`、`generated/`
均被忽略）；扫描器只输出类别与路径，绝不回显命中的值。若凭据曾被推送到远程
仓库，仅删除文件无法撤回历史，必须轮换凭据。
