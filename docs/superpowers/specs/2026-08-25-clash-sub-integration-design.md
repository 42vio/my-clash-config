# clash-sub 整合部署方案设计（3x-ui + Nginx Stream 统一 443）

- 日期：2026-08-25
- 状态：已与用户逐节确认，待转入实现计划
- 前置仓库状态：main @ c0caa64（2026-08-24 合并 codex/clash-subscription）

## 1. 背景与目标

在一台低配 VPS（512MB RAM / 10GB Disk / 默认 swap 256MB）上重装部署个人/小团队代理服务。
服务器将**重置重装**，因此本方案是全新部署流程，不存在从旧拓扑在线迁移的问题。

目标：

- 3x-ui（Xray-core，VLESS + Reality + Vision）为唯一代理线路，独立 systemd 服务
- Nginx stream 统一公网 443 入口，按 SNI 分流（ssl_preread，不终止代理协议的 TLS）
- 订阅服务保持现有「无守护进程、静态文件」形态，故障不影响代理
- `bash install.sh` 一键完成 443 整合、证书、systemd、健康检查
- 支持 VPS 重装后快速恢复、配置变更回滚、域名变更

## 2. ADR 变更（推翻既有决策的记录）

本方案**有意推翻**仓库现行两项决策，理由如下：

| 原 ADR（README.md / docs） | 新决策 | 理由 |
|---|---|---|
| 「不引入 Nginx stream，Reality 直占 443」 | 引入 Nginx stream，ssl_preread 分流 443 | 订阅与 3x-ui 面板需要 HTTPS 共用 443（公网仅开放一个端口）；Reality 与 HTTPS 无法在同一 http server 共存，stream 分流是唯一解 |
| Reality 直占 443 | Reality 移至 `127.0.0.1:10443`，经 default 分流 | 配合上一条；客户端视角仍连 443，订阅层做端口改写 |

同时**确认放弃**的需求文档原始项（演进过程记录）：

| 原始需求 | 结论 | 原因 |
|---|---|---|
| 保留 Jrohy/trojan 独立服务作备用线路 | **第一期移除，仅预留接口** | 实测内存非瓶颈；「挂掉自动恢复」的真正保障是 systemd 自愈链而非第二协议；机器级故障时同机备用无效 |
| 订阅服务监听 127.0.0.1:3000（动态 HTTP） | 静态文件方案（现有管线不动） | 无守护进程天然满足「订阅故障不影响代理」；复用刚加固完的原子发布/崩溃恢复管线 |
| Trojan 节点进订阅（一对一映射读 SQLite） | 不实施 | 随 Trojan 移除而搁置；若后期启用 Xray 内置 trojan inbound，映射自动成立，无需代码 |
| Docker 部署 | 原生部署 | 管理面开销 ≈ 再造一套代理核心的内存；dockerd 单点与故障隔离原则冲突；重装恢复依赖镜像拉取网络 |
| swap「256MB」 | 按 256MB 理解（需求文档笔误） | — |

## 3. 最终架构

```
Internet ──► 443/tcp ──► Nginx stream（ssl_preread，不终止 TLS）
                          │
                          ├─ SNI = sub.<域名>    ──► 127.0.0.1:30443  Nginx http（终止 TLS）
                          ├─ SNI = trojan.<域名> ──► 127.0.0.1:20443  【预留】后期扩展接入点
                          └─ default（任意其他 SNI）──► 127.0.0.1:10443  Xray Reality

127.0.0.1:30443 Nginx http server（一张通配证书）:
  ├─ /s/<token>/clash-<variant>.yaml   静态订阅（routes.conf，现有机制原样保留）
  └─ /<随机panel-path>/                反代 3x-ui 面板（127.0.0.1:<面板端口>，端口从
                                          xui snapshot 的 settings 读取，2053 仅为示例）

后台服务全景（无常驻订阅进程、无数据库、无 docker）:
  systemd: x-ui（3x-ui 面板+xray） · nginx · clash-sub-traffic.timer · clash-sub-recover.service
公网端口: 仅 443
```

关键设计点：

- **Reality 用 default 兜底**而非按 serverName 精确匹配：Reality 客户端握手的 SNI 是伪装第三方域，
  永不等于自有域名；default 兜底永不失配，未匹配流量进 Reality 符合其伪装哲学。
- **SNI 三规则中 trojan 规则现在就预置**（注释标记预留）：后端不存在时仅该 SNI 连接失败，
  不影响 sub 与 default 分流。
- **面板并入 sub 域名**：3x-ui 面板经 nginx 反代挂在 `https://sub.<域名>/<随机路径>/`，
  公网零独立管理端口（复用现有模板 PANEL_UPSTREAM/PANEL_BASE_PATH 能力）。
- **公网不再开 80**：wildcard DNS-01 签证书不需要 HTTP-01。

## 4. 部署阶段与重装恢复

```
Phase 1（手动，约 10 分钟）
  Debian 重装 → 官方脚本装 3x-ui → 面板建 Reality inbound
  （listen 0.0.0.0:10443——此时代理已可用，公网 10443 直连）

Phase 2（一条命令）
  git clone 本仓库 → bash install.sh → 交互应答 → 完成
```

重装恢复 = 同样两步（回灌 x-ui.db 备份后代理恢复点即达成），详见 `docs/recovery.md`。

**443 整合零中断顺序**：installer 起 nginx 443 stream 时 Xray 仍监听 `0.0.0.0:10443`
（default 分流转发到 `127.0.0.1:10443` 可达）→ 验证 443 走通 → 提示用户在面板把 inbound
listen 改为 `127.0.0.1:10443`（installer gate 检测）→ 公网 10443 自然关闭。全程代理不断。

## 5. 代码结构变更

```
新增:
  install.sh                  # ~30 行 bootstrap：root 检查 → python3-venv → venv → exec clash-sub install
  clash_sub/installer.py      # install 全流程八阶段 + install journal + 回滚
  templates/nginx/stream.conf.j2      # 443 stream + ssl_preread map（含预留 trojan 规则）
  templates/nginx/sub-server.conf.j2  # 由现有 clash-sub.conf.tmpl 演化（去 8443/80，443 TLS）
  tests/test_installer.py、test_installer_rollback.py 等

修改:
  config.py     # schema-version 1→2（见 §6）
  sources.py    # 新增「出站端点规范化」步骤
  xui.py        # 快照校验增强：单 Reality inbound 约定
  checks.py     # 改写后一致性校验
  nginx.py      # activate_runtime 从单文件泛化为多文件原子替换（routes + stream + sub-server）；
                #   泛化后供 install/update/cert 复用，日常 sync 仍只激活 routes.conf（见 §6.1）
  cli.py        # 新子命令：install / backup / update / cert / rollback
  service.py    # 编排接线
  README.md / DEPLOYMENT.md / docs/*（见 §11）

明确不写:
  trojan 数据源 / 443 服务编排 cutover journal —— 新机 443 空闲，无需服务编排
```

CLI 命名保留 `clash-sub`（含包名、systemd unit、nginx 路径约定），README 补一句定位说明：
「clash-sub = 本 VPS clash 订阅栈的全生命周期管理 CLI」。

## 6. 数据模型

### 6.1 config schema v2

```
schema-version: 2

现有键全部保留（owner-email / xui-database / private-root / public-root / nginx-routes /
mihomo-binary / nginx-binary / systemctl-binary / max-source-bytes）

值变化:  subscription-authority: "sub.<域名>:8443" → "sub.<域名>:443"
         （校验逻辑从「强制 8443」改为「强制 443」）

新增:    xui-public-endpoint: "<域名>:443"    ← 必填，fail-closed
         作用：订阅节点公网真实入口；端口整合（10443→443）与域名变更共用此键
```

v1 配置在 v2 代码下报清晰的迁移错误。stream/sub-server 配置路径不进运行时 config
（仅 install/update/cert 写），运行时 sync 依然只碰 `routes.conf`。

### 6.2 出站端点规范化（sources.py）

- 插入点：`fetch_xui_proxies` 之后、`merge_proxy_sources` 之前，仅作用于 3x-ui 源
  （airport/home 外部源不动）
- 每节点只改写 `server`、`port` 两个地址字段；**不动** `servername`（Reality 伪装 SNI 是
  协议字段）及 uuid/public-key/short-id 等凭据字段
- `checks.py` 同步校验：改写后所有 xui 节点 port 一致为 443，不一致 fail；缺配置 fail-closed

### 6.3 单 Reality inbound 约定（xui.py）

stream default 分流写死 `127.0.0.1:10443`。为防多 inbound 导致节点与后端错乱：
**Reality inbound 必须恰好一个且端口 10443**，违反 → `XuiCompatibilityError` 整体 fail。
加节点 = 在同一 inbound 里加 client（3x-ui 常规用法）。

### 6.4 用户/token/流量模型

全部不变：`state.json` token 机制、`clients` 表 per-user 流量头、`release_store` 双目录发布、
variant 策略。老 `state.json` 可直接沿用。

## 7. Installer 设计（clash-sub install）

### 7.1 八个阶段

```
Phase 0  preflight —— 只读检测，任一失败即停（不碰任何东西）
         硬性: root · Debian 12 · Disk 空闲≥1GB · 3x-ui 存在且 db 可读（复用 xui.py 校验）
              · Reality inbound 恰 1 个且 port=10443 · 443 空闲（ss）· nginx 未装或可接管
              · DNS 前置：用户输入的 sub.<域名> A 记录指向本机公网 IP
         信息性（不 gate）: RAM / swap 状态显示
         交互收集: 主域名 / CF API Token /（swap 扩容、证书等确认项）

Phase 1  低配优化 —— swap<1GB 则询问扩容至 1GB · vm.swappiness=10（sysctl.d）
         · journald SystemMaxUse=50M

Phase 2  apt install nginx libnginx-mod-stream
         · 模块自检: nginx -V 含 --with-stream_ssl_preread_module

Phase 3  证书 —— 安装 acme.sh（官方）→ CF Token 入 ~/.acme.sh/account.conf（0600）
         → --issue --dns dns_cf -d <域名> -d '*.<域名>' --keylength ec-256
         → --install-cert 到 /etc/ssl/domain/{fullchain,privkey}.pem（目录 0700 / key 0600）
         → reloadcmd="systemctl reload nginx"；续期由 acme.sh 自带 cron
         （注：首次 install-cert 时 nginx 尚未启动，reloadcmd 失败属预期、不影响证书落盘；
          后续自动续期时 nginx 已在运行）

Phase 4  nginx 配置激活 —— 渲染 stream.conf（443 三规则）+ sub-server.conf（30443 TLS
         + 订阅 routes include + 面板反代 + 限速）
         · nginx.conf 幂等追加 stream include（标记注释，已存在则跳过）
         · 移除 Debian default site · nginx -t → systemctl enable --now nginx
         · 验证: SNI=sub 返回证书 ✓ · default 后端 10443 可达 ✓

Phase 5  systemd 自愈补齐 —— nginx drop-in Restart=on-failure · 确认 x-ui.service enabled
         · 安装仓库 deploy/systemd/ 资产（traffic timer + recover.service + nginx drop-in）

Phase 6  订阅初始化 —— 生成 private/config/service.yaml（值来自 Phase 0 收集）
         · 跑通首次 clash-sub sync（release + routes + activate）

Phase 7  收尾 gate + 报告 —— 打印面板操作指引：把 Reality inbound listen 改为 127.0.0.1
         （此时代理已在 443 可用，改完公网 10443 关闭，零中断）
         · 输出订阅 URL · 面板 URL · 健康摘要 · 备份提示
```

### 7.2 install journal

每阶段完成写 `private/install-journal.json`（阶段/时间/关键产物路径）。中途失败重跑时
跳过已完成阶段（幂等），也是 `rollback --install` 的依据。

### 7.3 核心安全性质

Phase 4 之前 Reality 一直公网 10443 直连可用；Phase 4 之后 443 分流可达而 10443 仍开着；
Phase 7 用户手动收口。**任何阶段失败，代理从未中断**（新机 443 无存量服务）。

### 7.4 不做的事

- 不安装/修改 3x-ui 与 Xray（只读访问 x-ui.db；listen 修改是用户面板手动操作 + gate 检测）
- 不预置任何写死的域名（全部交互收集后派生）
- 首次 install 不做「变更前备份」（全新服务器无可备份的现状；备份语义见 §9）

## 8. 证书管理

```
*.<域名> + <裸域名>（双 SAN wildcard，acme.sh DNS-01 dns_cf，ec-256，/etc/ssl/domain/）
  └─ 唯一消费者: nginx sub-server（127.0.0.1:30443 TLS 终止）
     Reality: 不需要证书    3x-ui 面板: TLS 由 nginx 终止，本体 http
```

- 不用 3x-ui 证书管理、不用任何独立 ACME
- CF API Token 由 acme.sh 自管（account.conf，0600，root）
- 续期自动（acme.sh cron），reloadcmd 触发 nginx reload

## 9. 备份与回滚

| 时机 | 行为 |
|---|---|
| 首次 `install` | 不备份（无意义） |
| `update` / `cert --domain` 等变更命令 | 执行前自动快照到 `/opt/my-clash-config/backups/<UTC时间戳>/` |
| `clash-sub backup`（手动） | 全量打包：x-ui.db 副本 + private/ 全部 + nginx 配置 + 版本清单 → 单 tar.gz（0600，含 token/uuid 等敏感数据，提示 scp 异地保存；**不含**证书私钥，可重签）。打印 sha256 |

`clash-sub rollback`：

- `--install`：按 install journal 逆序：停 nginx → 移除 stream/sub-server 配置 → 还原
  nginx.conf include → 恢复 default site。**不动 x-ui 与证书**。结果回到 install 前状态
  （Reality 公网 10443 直连），代理始终可用
- `--activation`：现有 routes/state 快照恢复机制（上轮已加固，不改）

## 10. 其余管理命令

- `clash-sub update`：git pull → requirements 同步 → systemd 资产刷新 → nginx 模板有变则
  走现有 activation 原子管线；stream 模板变化时提示需手动重跑 install
- `clash-sub cert`：无参 = 证书状态/有效期；`--renew` = 强制续期；`--domain <new>` = 域名变更
  全流程（重签 → 更新 service.yaml 两处域名键 → 重渲染 nginx → sync 重建订阅 → 提示旧 URL 失效）
- `clash-sub status`（增强）：443 探活 · 证书剩余天数 · 各 unit 状态 · 上次 sync 结果 ·
  install journal 摘要。只报告，不做自动修复动作

## 11. 自愈保障清单（「挂掉自己恢复」的完整答案）

```
xray 崩溃       → x-ui.service Restart 自动拉起（3x-ui 官方 unit 已带）        [已有]
x-ui 面板崩溃   → 同上 systemd 拉起                                       [已有]
nginx 崩溃      → Debian 默认 unit 无 Restart → installer 补 drop-in
                  Restart=on-failure                                      [本方案补齐]
服务器重启      → 全部 enabled 自启 + clash-sub-recover.service 先于 nginx
                  恢复激活状态                                            [已有]
订阅层故障      → 静态文件 + 无常驻进程，sync 崩不影响已下发配置             [架构天然]
机器级灾难      → 重装手册：装 3x-ui → 回灌备份 → install（约 20 分钟）      [docs/recovery.md]
```

机器级高可用的正解是第二台 VPS（见 §13），不是本机第二协议。

## 12. 测试策略（延续 unittest 风格，无 CI，本地 `unittest discover`）

| 测试 | 覆盖 |
|---|---|
| `test_installer.py` | 每 Phase 函数级：preflight 各分支（mock ss//proc/os-release/dig）、nginx.conf include 幂等、journal 记录与重入、任何阶段失败时 Reality 可用性断言 |
| `test_installer_rollback.py` | 失败注入（Phase 2/3/4 任意点中断）→ rollback 恢复 install 前状态 |
| `test_sources.py` 扩展 | 端点规范化：改写正确、servername 不动、外部源不改写、缺配置 fail |
| `test_xui.py` 扩展 | 单 inbound 约定：0/2/端口≠10443 → 异常；恰 1 个通过 |
| `test_config.py` 扩展 | schema v2 校验、443 校验、v1 报迁移错误 |
| `test_lightweight_deployment.py` 扩展 | 新模板与 systemd 资产、DEPLOYMENT.md 一致性 |
| `test_cli.py` 扩展 | 新子命令注册与参数 |

不做真实 apt/网络/acme 集成测试（shell 交互由 DEPLOYMENT.md 人工验证清单兜底，延续现状）。

## 13. 后期扩展预留（只写入文档，不写代码）

```
预留一：Trojan 备用协议（若未来需要）
  路径 A（零代码，约 5 分钟）：3x-ui 面板加 trojan inbound（listen 127.0.0.1:20443、
    TLS 证书引用 /etc/ssl/domain/），stream 预置规则立即生效，订阅管线自动输出节点
  路径 B（进程级隔离）：Docker 跑 trojan-go 映射 127.0.0.1:20443 + 本仓库补一个
    可插拔订阅源模块（读其 config 注入节点）

预留二：第二台 VPS（真正的高可用）
  订阅天然支持多节点 url-test；两台各自跑本方案，订阅合并即可
```

## 14. 文档变更

| 文档 | 变更 |
|---|---|
| `README.md` | ADR 变更记录置顶（§2 内容）；端口表更新（公网仅 443）；「明确不做」清单修订；clash-sub 定位说明 |
| `docs/legacy-trojan-topology.md` | 保持历史定位，头部加指向本设计的链接 |
| `DEPLOYMENT.md` | 重写：**部署前准备清单**（CF NS / `sub.<域名>` A 记录 → VPS IP / CF API Token 三步，操作级说明）+ 两阶段部署 + Phase 7 面板收口 + 部署后验证清单 |
| `docs/recovery.md`（新） | 重装恢复手册、backup 产物说明与异地保存建议 |
| 证书文档 | acme.sh / CF Token / 域名变更流程（并入 recovery.md 或独立） |

## 15. 实现切分建议（供 writing-plans 参考）

每步独立可验证、测试全绿后进入下一步：

1. config schema v2 + 端点规范化 + 单 inbound 校验（纯订阅层）
2. nginx.py 多文件原子激活泛化 + stream/sub-server 模板
3. installer.py 八阶段 + journal + rollback --install
4. backup / update / cert / status 增强 + 全部文档与 ADR

## 16. 需求文档映射（验收对照）

| 原需求（章节） | 本方案落点 |
|---|---|
| 统一 443 入口（二/十） | §3 架构；零中断顺序 §4 |
| 不修改上游（三） | §7.4：3x-ui/Xray 只读；installer 只装 nginx/acme.sh |
| 服务独立（三） | 各自 systemd；订阅静态文件（§2 放弃记录） |
| 一键 Installer（五） | §5 薄 Bash + Python；八阶段 §7.1 |
| 环境检测/swap（五.1/3） | §7.1 Phase 0/1（RAM 放宽为信息性） |
| 端口迁移（五.5/6） | Trojan 无；Reality §4 零中断顺序 |
| 证书统一（六） | §8 |
| 订阅输出（七/八） | 现有管线不动 + §6.2 端点规范化 |
| 迁移与回滚（十） | §7.3 核心安全性质 + §9 |
| 重装恢复（十一） | §4 + docs/recovery.md |
| 管理命令（十三） | §9/§10 |
| 域名变更（十三） | §6.1 单键 + §10 cert --domain |
