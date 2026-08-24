# 部署指南（干净 Debian 12，轻量静态架构）

目标环境是**重装后的干净 Debian 12 amd64 VPS**，资源约束约 **512 MiB RAM、
256 MiB Swap、10 GiB 磁盘**。本仓库**不提供一键安装脚本**：下面每一条会修改
服务器的命令都是单独的人工步骤，且执行前先给出只读检查与预期输出。域名、
路径、端口、邮箱、哈希在本文中一律是示例占位，真实值只存在于服务器上的
root-only 私有配置中。

本架构空闲时常驻进程只有两个：3x-ui 管理的 Xray 与宿主机 Nginx。不安装
Docker，不部署转换器或发布进程；Python 与 Mihomo 只在手动同步和每日流量
任务期间短暂运行。

端口规划（部署完成后必须逐项核对）：

| 端口 | 归属 | 公网状态 |
| --- | --- | --- |
| TCP 443 | Xray：VLESS + RAW/TCP + REALITY | 开放，REALITY 独占 |
| TCP 80 | Nginx：ACME HTTP-01 与通用 404 | 开放 |
| TCP 8443 | Nginx：面板与 Clash 订阅（HTTPS） | 开放 |
| SSH 端口 | sshd | 由管理员指定 |
| 3x-ui 面板 / 原始订阅端口 | 3x-ui | 仅回环 `127.0.0.1` |

不开放 UDP 443，不使用公网 1443。

## 1. 人工初始化 3x-ui / Xray（本仓库不代办）

按 [docs/3x-ui-setup.md](docs/3x-ui-setup.md) 完成：原生 3x-ui `3.6.0`、
Xray-core `26.6.27`、强密码 + 2FA + 随机 Web Base Path、面板与订阅服务仅绑
回环、一个 VLESS + RAW/TCP + REALITY 公网 443 入站、每人一个独立客户端。
该清单中的安装命令是人工步骤，本仓库的任何脚本都不会下载或执行它们。

完成后做只读检查（预期：443 只属于 Xray，面板与订阅只出现在 127.0.0.1）：

```bash
ss -H -lntp | grep -E ':(443|2053|2096)\b'
systemctl is-active x-ui nginx
```

## 2. 检出仓库与 Python 运行环境

只读检查：确认目标目录空闲、Python 3 可用。

```bash
ls -ld /opt/clash-sub 2>/dev/null; python3 --version
```

逐条执行（每条是独立的修改步骤）：

```bash
git clone <你的私有远端> /opt/clash-sub
cd /opt/clash-sub
python3 -m venv .venv
.venv/bin/python -m pip install --no-input -r requirements.txt
```

`requirements.txt` 只有两个固定依赖：`Jinja2==3.1.6` 与 `PyYAML==6.0.3`。
只读验证（预期输出与两个固定版本号一致）：

```bash
.venv/bin/python -m pip freeze
```

## 3. 安装固定版本 Mihomo 校验器

Mihomo 只用于发布前的真实配置校验，固定为 `1.19.30`。先在本机核对官方
Release 公布的 `sha256`（示例占位哈希，务必替换为官方值）：

```bash
curl --fail --show-error --location \
  --output /tmp/mihomo-v1.19.30.gz \
  https://github.com/MetaCubeX/mihomo/releases/download/v1.19.30/mihomo-linux-amd64-v1.19.30.gz
shasum -a 256 /tmp/mihomo-v1.19.30.gz   # 与官方公布值逐字比对
```

校验一致后再安装：

```bash
install -d -o root -g root -m 0755 /usr/local/lib/clash-sub
install -o root -g root -m 0755 /dev/null /usr/local/lib/clash-sub/mihomo
gunzip -c /tmp/mihomo-v1.19.30.gz > /usr/local/lib/clash-sub/mihomo
chmod 0755 /usr/local/lib/clash-sub/mihomo
rm /tmp/mihomo-v1.19.30.gz
```

只读验证（预期显示 v1.19.30）：

```bash
/usr/local/lib/clash-sub/mihomo -v
```

## 4. 安装 clash-sub 命令与运行目录

用**符号链接**安装入口（复制会导致脚本无法定位仓库与 venv；symlink +
脚本内的 `.resolve()` 与 venv 重执行逻辑保证从任何位置、任何解释器启动
都正确）：

```bash
ln -s /opt/clash-sub/bin/clash-sub /usr/local/bin/clash-sub
```

只读验证（预期打印帮助性质的菜单错误码或菜单，而非
`ModuleNotFoundError`）：

```bash
clash-sub status || true
```

目录与权限（private 树 `0700` root-only；public 树归组 www-data，setgid
`2750`，发布出的 YAML 为 `0640`，Nginx worker 恰好可读、其他用户不可进入；
acme webroot 同样归组 www-data，Nginx worker 需要穿越它读取 challenge
文件）：

```bash
install -d -o root -g root -m 0700 /var/lib/clash-sub/private /var/lib/clash-sub/private/config
install -d -o root -g www-data -m 2750 /var/lib/clash-sub/public
install -d -o root -g root -m 0750 /etc/nginx/clash-sub
install -d -o root -g www-data -m 0750 /var/lib/clash-sub/acme
```

只读验证：

```bash
stat -c '%a %U:%G %n' /var/lib/clash-sub/private /var/lib/clash-sub/public
# 预期：700 root:root .../private
#       2750 root:www-data .../public
```

## 5. 私有配置

```bash
install -o root -g root -m 0600 /opt/clash-sub/config/service.example.yaml \
  /opt/clash-sub/private/config/service.yaml
```

编辑 `/opt/clash-sub/private/config/service.yaml`（保持属主 root:root、权限
`0600`），逐项填写真实值：

| 键 | 含义 |
| --- | --- |
| `owner-email` | owner 客户端在 3x-ui 中的 email 标识（示例 `owner-example`） |
| `subscription-authority` | 订阅对外主机名，如 `sub.<域名>:8443` |
| `xui-database` | 3x-ui SQLite 路径（示例 `/etc/x-ui/x-ui.db`），只读打开 |
| `private-root` / `public-root` | 运行数据目录（第 4 步创建的两组目录） |
| `nginx-routes` | 生成的精确路由 include（本文示例 `/etc/nginx/clash-sub/routes.conf`） |
| `mihomo-binary` | 第 3 步安装的 Mihomo 路径 |

owner 家庭节点手工维护在 `<private-root>/home.yaml`（同样 `0600`）。
私有数据边界见 [docs/private-data.md](docs/private-data.md)。

## 6. 证书（acme.sh，一张 SAN 证书）

复用服务器上已有的 acme.sh（3x-ui 环境通常已安装；没有则先人工安装
acme.sh，本文不代装）。`panel.<域名>` 与 `sub.<域名>` 共用一张 SAN 证书。

先做只读检查：确认 DNS A 记录已指向本机公网 IP，且 80 端口尚未被占用
（第 7 步的 Nginx 才会监听 80）：

```bash
dig +short panel.<域名>; dig +short sub.<域名>
ss -H -lntp | grep ':80\b' || echo '80 free'
```

签发（以 root 身份；HTTP-01，webroot 即第 4 步的 acme 目录；也可改用
DNS API 方式）：

```bash
~/.acme.sh/acme.sh --issue \
  --webroot /var/lib/clash-sub/acme \
  -d panel.<域名> \
  -d sub.<域名>
```

然后把证书安装到稳定路径（acme.sh 续期后会自动执行 `--reloadcmd`；Nginx
不直接引用 `~/.acme.sh` 内部文件）：

```bash
install -d -o root -g root -m 0700 /var/lib/clash-sub/certs
~/.acme.sh/acme.sh --install-cert -d panel.<域名> \
  --fullchain-file /var/lib/clash-sub/certs/fullchain.pem \
  --key-file /var/lib/clash-sub/certs/privkey.pem \
  --reloadcmd "systemctl reload nginx"
```

只读验证（预期两张文件存在、权限 0600）：

```bash
stat -c '%a %n' /var/lib/clash-sub/certs/*.pem
```

## 7. Nginx 配置（手工编辑模板）

先装 Nginx 并做只读检查：

```bash
apt install nginx
nginx -v
ls -l /etc/nginx/sites-enabled/
```

把模板复制后**手工编辑六个占位符**（`{{DOMAIN}}`、`{{FULLCHAIN_PATH}}`、
`{{PRIVKEY_PATH}}`、`{{PANEL_BASE_PATH}}`、`{{PANEL_UPSTREAM}}`、
`{{ROUTES_INCLUDE}}`）：

```bash
install -o root -g root -m 0644 /opt/clash-sub/deploy/nginx/clash-sub.conf.tmpl \
  /etc/nginx/sites-available/clash-sub.conf
install -o root -g root -m 0644 /opt/clash-sub/deploy/nginx/routes.empty.conf \
  /etc/nginx/clash-sub/routes.conf
ln -s /etc/nginx/sites-available/clash-sub.conf /etc/nginx/sites-enabled/clash-sub.conf
rm /etc/nginx/sites-enabled/default   # Debian 默认站点占用 80，会与模板冲突
```

占位符取值（全部替换为真实值）：

| 占位符 | 示例值 |
| --- | --- |
| `{{DOMAIN}}` | `example.com` |
| `{{FULLCHAIN_PATH}}` / `{{PRIVKEY_PATH}}` | 第 6 步的 `/var/lib/clash-sub/certs/*.pem` |
| `{{PANEL_BASE_PATH}}` | 3x-ui 随机 Web Base Path，如 `/x7Hq2mVt` |
| `{{PANEL_UPSTREAM}}` | 已验证的回环面板地址，如 `127.0.0.1:2053` |
| `{{ROUTES_INCLUDE}}` | `/etc/nginx/clash-sub/routes.conf` |

先检查再重载（`nginx -t` 失败就绝不能 reload）：

```bash
nginx -t && systemctl reload nginx
```

只读验证：`https://panel.<域名>:8443<面板路径>/` 可登录且要求 2FA；错误
Host / 路径只返回通用 404。

## 8. 每日流量 systemd timer

安装两个 unit 文件并启用（timer 每日运行一次 `clash-sub traffic-update`，
只更新流量响应头，绝不生成 YAML）：

```bash
install -o root -g root -m 0644 /opt/clash-sub/deploy/systemd/clash-sub-traffic.service \
  /etc/systemd/system/clash-sub-traffic.service
install -o root -g root -m 0644 /opt/clash-sub/deploy/systemd/clash-sub-traffic.timer \
  /etc/systemd/system/clash-sub-traffic.timer
systemctl daemon-reload
systemctl enable --now clash-sub-traffic.timer
```

只读验证（预期列出下一次触发时间）：

```bash
systemctl list-timers clash-sub-traffic.timer
```

## 9. 防火墙（UFW）

先只读确认当前规则与自己的 SSH 端口，避免把自己锁在门外：

```bash
ufw status numbered; ss -H -lntp | grep sshd
```

逐条放行（先 SSH，再开放端口；不开 UDP 443）：

```bash
ufw allow <SSH端口>/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 8443/tcp
ufw --force enable
```

只读验证（预期仅上述端口；无 UDP 443、无 1443）：

```bash
ufw status verbose
```

## 10. 首次同步与链接分发

```bash
clash-sub sync
clash-sub links
```

`sync` 只读打开 3x-ui SQLite 发现全部客户端，为每位用户生成
`standard`（owner 另有 `balanced` / `privacy`），经结构检查、泄漏扫描与
Mihomo 真实校验后原子发布；`links` 一次性显示全部有效订阅地址（按 3x-ui
内部 email 分组、附六位识别码），通过既有安全渠道分发一次。

## 11. 安装后验证清单

```bash
ss -H -lntup                          # 443 仅 Xray；80/8443 仅 Nginx；面板/订阅仅回环
ufw status numbered                   # 仅 SSH、TCP 80/443/8443
nginx -t
systemctl list-timers clash-sub-traffic.timer
clash-sub status
```

- 外部客户端下载 `https://sub.<域名>:8443/s/<token>/clash-<variant>.yaml`
  成功，错误 token / 越权 variant 返回相同 404。
- 手机更新机场、回滚、令牌轮换与故障恢复见
  [docs/operations.md](docs/operations.md)。

## 边界

- 重装操作系统、清除磁盘、修改 DNS 记录、更换 VPS / IP / 域名属于仓库外
  操作，由管理员单独决策执行；更换清单见 [docs/operations.md](docs/operations.md)。
- 配置内容的回退不靠文件备份：`clash-sub history` / `clash-sub rollback`
  基于每用户保留的最近五个成功版本。
- 3x-ui 的安装、升级、凭据管理全部人工完成，本仓库脚本只做只读检查。
