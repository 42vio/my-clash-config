# 部署指南（干净 Debian 12 服务器）

目标环境是**重装后的干净 Debian 12 amd64 VPS**。旧的 Jrohy/Trojan、`trojan-web`、
旧 MariaDB、Portainer 与旧 Nginx 配置一律不迁移；安装器在检测到这些残留或
无法识别的 443 服务时会直接停止，不做任何自动清理。

本仓库的部署物是：固定版本的回环 Compose 服务栈（subconverter / publisher /
一次性 manager、validator）、宿主机 Nginx（TCP 80/8443）、证书与定时检查、
UFW 规则以及 `/usr/local/bin/clash-sub`。原生 3x-ui / Xray、DNS 与操作系统
磁盘属于仓库之外。

## 1. 前置条件

- 重装后的干净 Debian 12 amd64，`apt update && apt full-upgrade` 完成。
- 管理员已确认重装与磁盘清除（仓库外操作，需单独批准）。

## 2. 固定版本的 3x-ui / Xray 人工初始化

按 [docs/3x-ui-setup.md](docs/3x-ui-setup.md) 完成：原生 3x-ui `3.6.0`、
Xray-core `26.6.27`、强密码 + 2FA + 随机 Web Base Path、面板与原始订阅只绑
回环、一个 VLESS + RAW/TCP + REALITY 公网 443 入站、每人一个独立客户端。
安装器不下载、不执行任何 3x-ui 安装脚本。

## 3. DNS 与证书

域名模式（主入口）：

- 为 `panel.<domain>` 与 `sub.<domain>` 各建一条 A（和/或 AAAA）记录，指向
  本 VPS 公网地址。
- 一张 SAN 证书同时覆盖两个主机名；Nginx 在 8443 终止 TLS，后端全为回环。

IP 模式（域名不可续费时的退路）：

- 不使用 `panel` / `sub` 子域名，Nginx 在同一张 IP 地址证书下按路径分流
  （`/s/` 交给 publisher，随机后台路径交给 3x-ui）。
- IP 证书有效期短，**必须**同时满足：配置非空 `alert-command`、
  `alert-before-seconds` 不低于 172800、续期定时器启用且续期失败会告警；
  否则 `service.yaml` 解析或 `--apply` 会拒绝。

## 4. `/opt/clash-sub` 检出与私有配置

```bash
git clone <你的私有远端> /opt/clash-sub
cd /opt/clash-sub

# 私有目录属主必须是容器内的应用用户 10001（Docker 代建目录属于 root，
# manager/publisher/validator 将无法读写）：
install -d -o 10001 -g 10001 -m 700 \
  /opt/clash-sub/private /opt/clash-sub/private/config \
  /opt/clash-sub/private/staging /opt/clash-sub/private/releases \
  /opt/clash-sub/private/current /opt/clash-sub/private/logs \
  /opt/clash-sub/private/sources

# 参照 config/service.example.yaml 与 config/users.example.yaml 填写真实值：
#   /opt/clash-sub/private/config/service.yaml
#   /opt/clash-sub/private/config/users.yaml
install -o 10001 -g 10001 -m 600 config/service.example.yaml \
  /opt/clash-sub/private/config/service.yaml
# 编辑后保持：属主 10001:10001，权限 0600。
```

`service.yaml` 的 `publication.mode` 决定域名模式（`domain`，两个 8443 主机名）
或 IP 模式（`ip`，同一公网 IP authority + IP 证书 + 强制告警）。真实域名、IP、
路径与令牌哈希只存在于此目录，绝不提交 Git（详见
[docs/private-data.md](docs/private-data.md)）。

本开发机没有 Docker，无法本地预检 Compose；请在部署主机上先执行
`docker compose config` 校验后再继续。

## 5. 默认只读预检（不改动系统）

```bash
sudo -E /opt/clash-sub/scripts/install-server.sh \
  --config /opt/clash-sub/private/config/service.yaml \
  --ssh-port <管理员确认的活动 SSH 端口>
```

- 必须使用 `sudo -E`：安装器依赖 `SSH_CONNECTION` 校验当前 SSH 端口，而
  `sudo` 的 `env_reset` 会丢弃它。
- 只读预检在安装任何软件包**之前**执行，且包含 Docker / Compose 探测：全新
  主机需先自行安装 Docker 与 Compose 插件（或接受预检阻塞提示后手动安装再
  重试）；其余软件包（nginx、ufw 等）由 `--apply` 的软件包阶段安装。
- 预检确认：3x-ui `3.6.0` / Xray `26.6.27`、公网 TCP 443 仅属 Xray 且无
  UDP 443、面板与原始订阅仅回环、80/8443 空闲或仅归本项目、无 Trojan /
  Jrohy / 旧数据库 / Portainer、域名 DNS（或 IP 前置条件）满足。

## 6. 应用变更（需管理员逐次确认）

只有在人工审阅预检报告之后，才执行：

```bash
sudo -E /opt/clash-sub/scripts/install-server.sh \
  --config /opt/clash-sub/private/config/service.yaml \
  --ssh-port <管理员确认的活动 SSH 端口> --apply
```

`--apply` 会：备份它将替换的目标文件到 `/var/backups/clash-sub/<操作 id>/`、
安装缺失软件包、创建 `/opt/certbot`（certbot `5.7.0`）、签发证书、写入
Nginx 80/8443 配置与 systemd 证书单元（先 `nginx -t` 再 reload）、构建并只
启动 `subconverter` 与 `publisher`、安装 `/usr/local/bin/clash-sub`，最后
配置 UFW（先放行已验证的 SSH 端口，再放行 TCP 80/443/8443，默认拒绝入站，
不开 UDP 443）。任何一步失败都会恢复本项目拥有的主机文件与服务状态
（先 `nginx -t` 再回滚 reload），不触碰 3x-ui、Xray 数据、DNS 与无关文件。

- 软件包安装不可逆；已启动的 compose 容器、`/opt/certbot` 虚拟环境与 ACME
  webroot 均只监听回环或无公网暴露，回滚后留置无害，可直接重试 `--apply`。
- 每次失败的 `--apply` 会在备份目录保留 `inventory.json` 与 0600 的
  `failure.log`（仅 root 可读，用于诊断 nginx/certbot/compose 失败输出；
  终端输出始终脱敏）。

## 7. 安装后检查

```bash
ss -H -lntup                          # 443 仅 Xray；80/8443 仅 Nginx；25500/25501/面板/原始订阅仅回环
ufw status numbered                   # 仅 SSH、TCP 80/443/8443；无 UDP 443
sudo nginx -t
docker compose ps                     # subconverter 与 publisher 健康
sudo /opt/certbot/bin/certbot certificates   # 证书覆盖预期主机名（或 IP）
```

- 面板：`https://panel.<domain>:8443/<随机后台路径>/` 可登录且要求 2FA；
  错误 Host / 路径只返回通用响应。
- 订阅：`clash-sub rotate-link <user-id>` 显示一次令牌链接后，从外部客户端
  下载 `https://sub.<domain>:8443/s/<令牌>/<variant>.yaml`，各令牌只能下载
  自己的当前配置。
- Mihomo：`clash-sub refresh`（或 `clash-sub refresh <user-id>`）已把三种
  variant 全部通过固定版本 Mihomo 校验后才发布；外部客户端导入后能被其
  Mihomo 内核正常解析。

## 8. 域名证书迁移到 IP 证书

域名到期或不再续费时切换为 IP 入口：

1. 修改 `service.yaml`：`publication.mode: ip`，两个 authority 都改为
   `<vps-ip>:8443`；补齐 IP 模式强制的 `alert-command` 与
   `alert-before-seconds`（≥ 172800）。
2. 重新执行第 5 步预检与第 6 步 `--apply`：安装器签发 IP 地址证书并切换
   Nginx 路径路由。迁移前必须满足：初始 IP 证书有效、续期定时器活动、
   续期 dry-run 成功、告警命令可用——否则 `--apply` 拒绝。
3. 每位用户只需更换一次订阅 URL（authority 变化）；3x-ui UUID、REALITY
   密钥与已生成配置不重建。REALITY 节点本身使用 VPS 公网 IP，不受域名
   到期影响。

## 9. 回滚与边界

- 项目内回滚：`--apply` 失败时自动恢复；如需手工回滚，使用
  `/var/backups/clash-sub/<操作 id>/inventory.json` 中记录的项目自有文件
  （Nginx 配置、systemd 证书单元、`/usr/local/bin/clash-sub`），恢复后先
  `nginx -t` 再 reload。
- 重装操作系统、清除磁盘、修改 DNS 记录、重命名远端仓库都是仓库外操作，
  必须由管理员单独决策与执行；安装器不代做。
- 配置内容的回退不用文件备份：`clash-sub history` / `clash-sub rollback`
  基于每用户保留的最近五个成功版本，详见 [docs/operations.md](docs/operations.md)。

## 日常操作

`clash-sub`（即 `/usr/local/bin/clash-sub`）是唯一管理命令；manager 与
validator 是一次性容器（compose profile `manual`），不随
`docker compose up` 启动：

```bash
clash-sub status            # 服务可达性、各用户发布状态、证书健康
clash-sub refresh           # 重建、校验并发布全部用户（按用户 ID 排序逐个执行）
clash-sub logs --limit 50   # 最近的脱敏操作日志
```

完整的机场更新、令牌轮换、回退与故障恢复流程见
[docs/operations.md](docs/operations.md)。
