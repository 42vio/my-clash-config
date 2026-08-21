# my-clash-config：私有 Clash 订阅服务

对外统一使用 **Clash** 命名；服务器上唯一的管理命令是 `clash-sub`（无 `refresh-all`
等别名）。本仓库把每个人独立的 3x-ui 订阅转换、校验并渲染为 `balanced` /
`balanced-win` / `privacy` 三种完整配置，原子发布后由宿主机 Nginx 通过高强度
随机令牌路径对外只读发布。

> **链接即密码：** 任何拿到订阅链接的人都能下载该链接展开后的全部节点凭据。
> 订阅链接只通过受信任渠道分发；一旦泄漏，立即 `clash-sub rotate-link <user-id>`
> 并在 3x-ui 面板撤销该用户的客户端凭据。

## 信任模型

| 角色 | 配置中允许的节点来源 | 说明 |
| --- | --- | --- |
| owner | 自己的 3x-ui 客户端 + 最新机场快照 + 自维护家庭节点 | 唯一可声明 `local-sources` 的用户 |
| 普通用户（member） | 仅自己的 3x-ui 客户端 | 相互隔离；输出中不含 owner 或其他任何用户的节点、名称或凭据 |

每位用户持有独立的订阅令牌（服务器只保存哈希）与独立的 3x-ui 客户端
（独立 UUID、配额、到期时间），泄漏时可以单独轮换、单独撤销。

## 端口与监听

| 端口 | 归属 | 状态 |
| --- | --- | --- |
| TCP 443 | 原生 Xray（VLESS + RAW/TCP + REALITY） | 公网开放，REALITY 独占，不经 Nginx |
| TCP 80 | 宿主机 Nginx | 仅 ACME HTTP-01 验证与通用响应 |
| TCP 8443 | 宿主机 Nginx HTTPS | 唯一 HTTPS 入口（域名模式按 `panel.<domain>` / `sub.<domain>` 分流；IP 模式按路径分流） |
| SSH | sshd | 由管理员指定，本项目不更改 |
| 3x-ui 面板与原始订阅、subconverter（25500）、publisher（25501） | 回环 `127.0.0.1` | 永不直接暴露公网；manager / validator 为一次性服务，无监听 |

不开放 UDP 443，不使用公网 1443，不引入 Nginx stream。

## 数据流

```text
每人独立的 3x-ui 订阅（回环原始订阅）──┐
机场快照（仅 owner，私有文件）─────────┤
家庭节点（仅 owner，私有文件）─────────┤
                                        ▼
                     MetaCubeX/subconverter（仅回环，容器内）
                                        ▼
                     渲染三种 variant + 结构/泄漏校验
                                        ▼
                     固定版本 Mihomo 真实配置校验（一次性容器）
                                        ▼
                     原子发布（每用户仅保留最近 5 个成功版本）
                                        ▼
                     publisher（仅回环，令牌只读，校验哈希后服务）
                                        ▼
                     宿主机 Nginx :8443 HTTPS ──> 各自的 Clash 客户端
```

## 三种 variant 与 DNS 策略

三种输出的差异由 `templates/variants/*.yaml` 描述（DNS、策略组、规则与 GEOIP
解析策略），公共结构在 `templates/clash.yaml.j2`：

| 输出 | DNS 架构 | Resolve 策略 | 适用设备 |
| --- | --- | --- | --- |
| `balanced` | 策略分流 DNS（`respect-rules` + 海外 DoH 默认 + 国内域名分流） | 混合：前置 IP 规则 `no-resolve`，仅最后 `GEOIP,CN` 允许解析 | 通用 / 游戏 Windows |
| `balanced-win` | 同 `balanced`（Windows 平台差异） | 同上 | Windows 桌面 |
| `privacy` | Fake-IP 隐私 DNS（配置最简，不开启 `respect-rules`） | 全 `no-resolve`：未知域名不为 IP 判断而解析，直接代理 | 工作 Mac，隐私优先 |

保留的设计语义（原独立 DNS 设计文档的要点）：

- **DNS 隐私目标**是尽量不让国内公网 DNS 获知敏感或未知国外域名的查询；
  本机发起、交给海外 DoH 的查询不等于 DNS 泄漏。
- **`no-resolve`** 只表示"该 IP/GEOIP 类规则不为匹配而主动解析域名"，
  不是禁止 Mihomo 在任何情况下解析。
- **`respect-rules: true`** 表示 Mihomo 自发的 DNS 查询连接也遵循分流规则
  （查询可能经代理发给海外 DoH），不等于"代理服务器远端解析"，两者别混淆。
- **机场 DoH** 可替换 Cloudflare/Google DNS，但前提是实测延迟、稳定性、
  CDN 结果与 DNS Leak 测试符合预期；性能差距明显时只保留 1～2 个。

## 明确不做

- **没有 sub-web**，也没有任何在线转换页面。
- **没有公开 converter**：subconverter 仅监听回环，Nginx 不代理任何转换路由。
- **没有定时生成**：配置只在前四种事件时重建——首次部署、显式
  `clash-sub refresh`、机场导入成功、来源或模板修改后的手动 refresh。
  证书续期与到期检查定时任务只维护 HTTPS，绝不触发配置生成。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/3x-ui-setup.md](docs/3x-ui-setup.md) | 固定版本 3x-ui / Xray 人工初始化清单 |
| [DEPLOYMENT.md](DEPLOYMENT.md) | 干净 Debian 12 服务器部署、预检、`--apply` 与回滚 |
| [docs/private-data.md](docs/private-data.md) | 私有数据布局、权限、备份与恢复边界 |
| [docs/operations.md](docs/operations.md) | 日常运维：机场更新、刷新、轮换、回退与故障恢复 |

## 开发

```bash
python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/scan_tracked_secrets.py            # 追踪文件敏感信息扫描
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
```

安全约定：真实订阅 URL、令牌、UUID、节点密码、REALITY 密钥、机场临时 URL、
生成结果与含凭据的 release 元数据一律不进入 Git（`private/`、`generated/`
均被忽略）；扫描器只输出类别与路径，绝不回显命中的值。若凭据曾被推送到远程
仓库，仅删除文件无法撤回历史，必须轮换凭据。
