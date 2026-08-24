# 部署指南（干净 Debian 12，轻量静态架构）

目标环境是重装后的干净 Debian 12 amd64 VPS，资源约束约 512 MiB RAM、256 MiB Swap、
10 GiB 磁盘。本仓库不提供一键安装脚本：每条会修改服务器的命令都是单独
人工步骤，前面都有只读检查与理由。域名、IP、端口、邮箱、远端、密码和哈希均为
占位符；真实值只存在于服务器 root-only 私有配置。

空闲时常驻进程只有 3x-ui 管理的 Xray 与宿主机 Nginx。不要安装 Docker、转换器
或发布进程；Python 与 Mihomo 只在手动同步和每日流量任务期间运行。

| 端口 | 归属 | 公网状态 |
| --- | --- | --- |
| TCP 443 | Xray：VLESS + RAW/TCP + REALITY | 开放，REALITY 独占 |
| TCP 80 | Nginx：ACME HTTP-01 与通用 404 | 开放 |
| TCP 8443 | Nginx：面板与 Clash 订阅 HTTPS | 开放 |
| SSH | sshd | 管理员指定 |
| 面板 / 原始订阅端口 | 3x-ui | 仅回环 127.0.0.1 |

不开放 UDP 443，不使用公网 1443。以下均以 root 或已验证的 sudo 执行；不要把
修改命令拼成脚本或一行。

## 1. 检查主机并安装轻量前置包

先只读确认 Debian 版本、SSH 端口、资源和监听状态。把实际 SSH 端口记为
<SSH-port>，不得猜测。

~~~bash
cat /etc/os-release
~~~

~~~bash
sshd -T | grep '^port '
~~~

~~~bash
free -m; df -h /; ss -H -lntp
~~~

先检查 APT 来源；随后刷新包索引。这是独立的服务器修改。

~~~bash
apt-config dump | grep -E '^(Dir::Etc::sourcelist|Dir::Etc::sourceparts)'
~~~

~~~bash
apt-get update
~~~

先只读核对候选版本；再一次安装所有必需的轻量包。--no-install-recommends 避免
512 MiB 主机安装推荐包，且 ca-certificates、curl、git、python3、python3-venv、
nginx、ufw、dnsutils、gzip 必须在首次使用前到位。

~~~bash
apt-cache policy ca-certificates curl git python3 python3-venv nginx ufw dnsutils gzip
~~~

~~~bash
apt-get -y install --no-install-recommends ca-certificates curl git python3 python3-venv nginx ufw dnsutils gzip
~~~

安装后只读确认工具与包均可用。

~~~bash
command -v curl git python3 nginx ufw dig gzip; dpkg-query -W ca-certificates python3-venv
~~~

## 2. UFW：先保住 SSH，再启用

先只读确认当前规则和 SSH 监听。未确认 <SSH-port> 前不要继续。

~~~bash
ufw status numbered; ss -H -lntp | grep sshd
~~~

先放行当前 SSH；这是后续默认拒绝策略的恢复路径。

~~~bash
ufw allow <SSH-port>/tcp
~~~

只读确认 SSH 规则出现后，才设置默认拒绝入站。

~~~bash
ufw status numbered
~~~

~~~bash
ufw default deny incoming
~~~

只读确认策略后，设置默认允许出站，保留软件更新、ACME 和管理员连接能力。

~~~bash
ufw status verbose
~~~

~~~bash
ufw default allow outgoing
~~~

每次只添加一个设计端口。先检查当前规则，再开放 80、443、8443 TCP；绝不添加
UDP 443。

~~~bash
ufw status numbered
~~~

~~~bash
ufw allow 80/tcp
~~~

~~~bash
ufw status numbered
~~~

~~~bash
ufw allow 443/tcp
~~~

~~~bash
ufw status numbered
~~~

~~~bash
ufw allow 8443/tcp
~~~

启用前最后一次只读核对必须同时显示 SSH、80、443、8443 与正确默认策略。

~~~bash
ufw status verbose
~~~

~~~bash
ufw --force enable
~~~

另开一个 SSH 会话后只读确认；失败时只用 VPS 控制台恢复。

~~~bash
ufw status numbered
~~~

## 3. 人工初始化 3x-ui / Xray

现在按 [docs/3x-ui-setup.md](docs/3x-ui-setup.md) 完成原生 3x-ui 3.6.0、
Xray-core 26.6.27、强密码、2FA、随机 Web Base Path、回环面板/订阅服务、唯一
VLESS + RAW/TCP + REALITY 443 入站和每人一个客户端。本仓库脚本不会下载、
安装或修改 3x-ui。

完成后只读检查：443 只属于 Xray，面板和订阅仅在 127.0.0.1，且 3x-ui 环境的
acme.sh 已存在。若 acme.sh 不可执行，停止并完成该人工初始化；不能提前签发。

~~~bash
ss -H -lntp | grep -E ':(443|<panel-port>|<subscription-port>)'
~~~

~~~bash
systemctl is-active x-ui; ~/.acme.sh/acme.sh --version
~~~

## 4. 检出仓库并建立 Python 环境

先只读确认目标路径为空且私有远端可读；随后才检出。

~~~bash
test ! -e /opt/clash-sub; git ls-remote <private-repository-url> HEAD
~~~

~~~bash
git clone <private-repository-url> /opt/clash-sub
~~~

先检查 Python 与固定依赖，再创建 venv。

~~~bash
cd /opt/clash-sub && python3 --version && sed -n '1,20p' requirements.txt
~~~

~~~bash
cd /opt/clash-sub && python3 -m venv .venv
~~~

先检查 venv pip 与依赖清单，再安装。requirements.txt 固定为 Jinja2==3.1.6 与
PyYAML==6.0.3。

~~~bash
cd /opt/clash-sub && .venv/bin/python -m pip --version && sed -n '1,20p' requirements.txt
~~~

~~~bash
cd /opt/clash-sub && .venv/bin/python -m pip install --no-input -r requirements.txt
~~~

~~~bash
cd /opt/clash-sub && .venv/bin/python -m pip freeze
~~~

## 5. 安装固定 Mihomo 校验器

Mihomo 只做发布前真实校验，版本固定 1.19.30。先检查下载工具和临时目标，再下载
官方归档；不要把网络响应管道给 shell。

~~~bash
curl --version; test ! -e /tmp/mihomo-v1.19.30.gz
~~~

~~~bash
curl --fail --show-error --location --output /tmp/mihomo-v1.19.30.gz <mihomo-v1.19.30-linux-amd64-url>
~~~

下载后先用 Debian 的 sha256sum 输出与官方 <mihomo-sha256> 逐字比较；不一致立即
停止。

~~~bash
sha256sum /tmp/mihomo-v1.19.30.gz
~~~

校验一致后，先检查安装目录，再创建目录。

~~~bash
stat -c '%a %U:%G %n' /usr/local/lib/clash-sub 2>/dev/null || true
~~~

~~~bash
install -d -o root -g root -m 0755 /usr/local/lib/clash-sub
~~~

先确认二进制目标不存在且归档可解压，再写入它。

~~~bash
test ! -e /usr/local/lib/clash-sub/mihomo; gzip -t /tmp/mihomo-v1.19.30.gz
~~~

~~~bash
gunzip -c /tmp/mihomo-v1.19.30.gz > /usr/local/lib/clash-sub/mihomo
~~~

先确认文件非空，再设可执行权限。

~~~bash
test -s /usr/local/lib/clash-sub/mihomo
~~~

~~~bash
chmod 0755 /usr/local/lib/clash-sub/mihomo
~~~

~~~bash
/usr/local/lib/clash-sub/mihomo -v
~~~

确认仅临时归档仍存在后再删除它。

~~~bash
test -f /tmp/mihomo-v1.19.30.gz
~~~

~~~bash
rm /tmp/mihomo-v1.19.30.gz
~~~

## 6. 运行目录、私有配置与命令入口

先确认入口未占用，再建立符号链接。复制会破坏脚本定位仓库与 venv 的逻辑。

~~~bash
test ! -e /usr/local/bin/clash-sub
~~~

~~~bash
ln -s /opt/clash-sub/bin/clash-sub /usr/local/bin/clash-sub
~~~

~~~bash
clash-sub status || true
~~~

依次创建运行目录：private 树是 0700 root-only；public 树归组 www-data 且 setgid
2750，发布 YAML 才能是 0640；ACME webroot 同样归组 www-data。

~~~bash
stat -c '%a %U:%G %n' /var/lib/clash-sub/private 2>/dev/null || true
~~~

~~~bash
install -d -o root -g root -m 0700 /var/lib/clash-sub/private
~~~

~~~bash
stat -c '%a %U:%G %n' /var/lib/clash-sub/private/config 2>/dev/null || true
~~~

~~~bash
install -d -o root -g root -m 0700 /var/lib/clash-sub/private/config
~~~

~~~bash
stat -c '%a %U:%G %n' /var/lib/clash-sub/public 2>/dev/null || true
~~~

~~~bash
install -d -o root -g www-data -m 2750 /var/lib/clash-sub/public
~~~

~~~bash
stat -c '%a %U:%G %n' /etc/nginx/clash-sub 2>/dev/null || true
~~~

~~~bash
install -d -o root -g root -m 0750 /etc/nginx/clash-sub
~~~

~~~bash
stat -c '%a %U:%G %n' /var/lib/clash-sub/acme 2>/dev/null || true
~~~

~~~bash
install -d -o root -g www-data -m 0750 /var/lib/clash-sub/acme
~~~

先确认服务配置目标不存在和示例可读，再创建唯一 root-only 副本。

~~~bash
test ! -e /opt/clash-sub/private/config/service.yaml; sed -n '1,120p' /opt/clash-sub/config/service.example.yaml
~~~

~~~bash
install -o root -g root -m 0600 /opt/clash-sub/config/service.example.yaml /opt/clash-sub/private/config/service.yaml
~~~

先检查权限和键；随后在私有副本填写真实 owner-email、subscription-authority、
xui-database、private-root、public-root、nginx-routes 与 mihomo-binary。

~~~bash
stat -c '%a %U:%G %n' /opt/clash-sub/private/config/service.yaml; sed -n '1,120p' /opt/clash-sub/private/config/service.yaml
~~~

~~~bash
<editor> /opt/clash-sub/private/config/service.yaml
~~~

~~~bash
stat -c '%a %U:%G %n' /opt/clash-sub/private/config/service.yaml
~~~

## 7. HTTP-01 webroot bootstrap

本节必须在任何 acme.sh --issue 前完成。先只读确认 DNS 指向 <VPS-public-IP>、
Nginx 已安装、webroot 权限正确；DNS-01 是显式替代方式，不是 HTTP-01 隐藏前置。

~~~bash
dig +short panel.<domain>; dig +short sub.<domain>; nginx -v; stat -c '%a %U:%G %n' /var/lib/clash-sub/acme
~~~

先确认 bootstrap 文件不存在，再创建 root-owned 空文件。

~~~bash
test ! -e /etc/nginx/sites-available/clash-sub-http-bootstrap.conf
~~~

~~~bash
install -o root -g root -m 0644 /dev/null /etc/nginx/sites-available/clash-sub-http-bootstrap.conf
~~~

先检查权限，再手工填入仅 port-80 webroot 和通用 404。此时不得加入 TLS、面板或
订阅代理。

~~~bash
stat -c '%a %U:%G %n' /etc/nginx/sites-available/clash-sub-http-bootstrap.conf
~~~

~~~bash
<editor> /etc/nginx/sites-available/clash-sub-http-bootstrap.conf
~~~

~~~nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;
    server_tokens off;
    location ^~ /.well-known/acme-challenge/ {
        root /var/lib/clash-sub/acme;
        try_files $uri =404;
    }
    location / {
        return 404;
    }
}
~~~

先只读检查默认站点与目标链接。下面两条只改变磁盘上的链接；Nginx 继续运行旧配置，
直到 nginx -t 成功后的 reload。

~~~bash
ls -l /etc/nginx/sites-enabled/default /etc/nginx/sites-enabled/clash-sub.conf 2>/dev/null || true
~~~

~~~bash
rm /etc/nginx/sites-enabled/default
~~~

~~~bash
test ! -e /etc/nginx/sites-enabled/default; test -f /etc/nginx/sites-available/clash-sub-http-bootstrap.conf; test ! -e /etc/nginx/sites-enabled/clash-sub.conf
~~~

~~~bash
ln -s /etc/nginx/sites-available/clash-sub-http-bootstrap.conf /etc/nginx/sites-enabled/clash-sub.conf
~~~

先验证整个 Nginx 配置；失败时不得 reload。

~~~bash
nginx -t
~~~

~~~bash
systemctl reload nginx
~~~

先确认 Nginx active、80 监听和 ACME challenge 目录尚不存在，再建立 root-owned、
group-readable challenge 目录。

~~~bash
systemctl is-active nginx; ss -H -lntp | grep ':80'; test ! -e /var/lib/clash-sub/acme/.well-known
~~~

~~~bash
install -d -o root -g www-data -m 0750 /var/lib/clash-sub/acme/.well-known/acme-challenge
~~~

challenge 目录建立后，先只读确认 probe 目标不存在，再写入无秘密 probe。

~~~bash
test ! -e /var/lib/clash-sub/acme/.well-known/acme-challenge/clash-sub-probe
~~~

~~~bash
echo clash-sub-acme-probe > /var/lib/clash-sub/acme/.well-known/acme-challenge/clash-sub-probe
~~~

用真实域名与公网 IP 做只读请求，预期返回 probe 内容。失败先修 DNS、UFW 或 Nginx，
不能申请证书。

~~~bash
curl --fail --resolve panel.<domain>:80:<VPS-public-IP> http://panel.<domain>/.well-known/acme-challenge/clash-sub-probe
~~~

验证后先确认唯一 probe 文件仍存在，再删除它。

~~~bash
test -f /var/lib/clash-sub/acme/.well-known/acme-challenge/clash-sub-probe
~~~

~~~bash
rm /var/lib/clash-sub/acme/.well-known/acme-challenge/clash-sub-probe
~~~

## 8. SAN 证书（HTTP-01；DNS-01 可选）

先只读重复确认两条 A 记录、正在运行的 bootstrap Nginx 和 acme.sh。此顺序保证
签发不依赖尚未存在的 Nginx。

~~~bash
dig +short panel.<domain>; dig +short sub.<domain>; systemctl is-active nginx; ~/.acme.sh/acme.sh --version
~~~

HTTP-01 已可达，现在申请 panel.<domain> 与 sub.<domain> 共用 SAN 证书。

~~~bash
~/.acme.sh/acme.sh --issue --webroot /var/lib/clash-sub/acme -d panel.<domain> -d sub.<domain>
~~~

若选择 DNS-01，改用 acme.sh 的显式 DNS API 参数并保护 API 凭据；它是 HTTP-01 的
替代流程，不是 HTTP-01 的隐藏要求。

签发成功后先检查稳定证书目录不存在，再创建 root-only 目录。

~~~bash
test ! -e /var/lib/clash-sub/certs
~~~

~~~bash
install -d -o root -g root -m 0700 /var/lib/clash-sub/certs
~~~

先查看签发记录和目标路径；Nginx 已存在，所以续期的 reload hook 现在才安全。

~~~bash
~/.acme.sh/acme.sh --info -d panel.<domain>; test ! -e /var/lib/clash-sub/certs/fullchain.pem; test ! -e /var/lib/clash-sub/certs/privkey.pem
~~~

~~~bash
~/.acme.sh/acme.sh --install-cert -d panel.<domain> --fullchain-file /var/lib/clash-sub/certs/fullchain.pem --key-file /var/lib/clash-sub/certs/privkey.pem --reloadcmd "systemctl reload nginx"
~~~

~~~bash
stat -c '%a %U:%G %n' /var/lib/clash-sub/certs/fullchain.pem /var/lib/clash-sub/certs/privkey.pem; systemctl is-active nginx
~~~

## 9. final 8443 TLS template

现在才安装最终模板。它保留 80 ACME location，并在 8443 提供默认 404、面板反向
代理与静态订阅。先确认 PEM、模板、空路由和 bootstrap 链接；缺少证书不得继续。

~~~bash
stat -c '%a %U:%G %n' /var/lib/clash-sub/certs/fullchain.pem /var/lib/clash-sub/certs/privkey.pem; test -f /opt/clash-sub/deploy/nginx/clash-sub.conf.tmpl; test -f /opt/clash-sub/deploy/nginx/routes.empty.conf; readlink /etc/nginx/sites-enabled/clash-sub.conf
~~~

先确认路由 include 不存在，再安装。

~~~bash
test ! -e /etc/nginx/clash-sub/routes.conf
~~~

~~~bash
install -o root -g root -m 0644 /opt/clash-sub/deploy/nginx/routes.empty.conf /etc/nginx/clash-sub/routes.conf
~~~

先确认最终站点副本不存在，再从模板创建它。

~~~bash
test ! -e /etc/nginx/sites-available/clash-sub.conf
~~~

~~~bash
install -o root -g root -m 0644 /opt/clash-sub/deploy/nginx/clash-sub.conf.tmpl /etc/nginx/sites-available/clash-sub.conf
~~~

先检查六个占位符都存在；随后只在服务器副本填入真实值。PANEL_UPSTREAM 必须是已验证
的 127.0.0.1:<panel-port>，PANEL_BASE_PATH 必须是 3x-ui 随机路径。

~~~bash
rg -n '{{' /etc/nginx/sites-available/clash-sub.conf
~~~

~~~bash
<editor> /etc/nginx/sites-available/clash-sub.conf
~~~

编辑后先确认无占位符，再原子切换同名链接到最终文件；运行中的 Nginx 仍保留 bootstrap
直到下一次成功 reload。

~~~bash
! rg -n '{{' /etc/nginx/sites-available/clash-sub.conf; readlink /etc/nginx/sites-enabled/clash-sub.conf
~~~

~~~bash
ln -sfn /etc/nginx/sites-available/clash-sub.conf /etc/nginx/sites-enabled/clash-sub.conf
~~~

~~~bash
nginx -t
~~~

~~~bash
systemctl reload nginx
~~~

只读确认 TLS 面板、HTTP challenge 路径和回环限制。面板限速覆盖整个随机 base path，
不依赖某个 3x-ui 版本的 login URI。

~~~bash
curl --fail --resolve panel.<domain>:8443:<VPS-public-IP> https://panel.<domain>:8443<PANEL_BASE_PATH>/
~~~

~~~bash
ss -H -lntp | grep -E ':(443|8443|<panel-port>|<subscription-port>)'
~~~

## 10. 每日流量 timer

先确认 unit 源与目标状态，再分别安装 service 和 timer。

~~~bash
test -f /opt/clash-sub/deploy/systemd/clash-sub-traffic.service; test ! -e /etc/systemd/system/clash-sub-traffic.service
~~~

~~~bash
install -o root -g root -m 0644 /opt/clash-sub/deploy/systemd/clash-sub-traffic.service /etc/systemd/system/clash-sub-traffic.service
~~~

~~~bash
test -f /opt/clash-sub/deploy/systemd/clash-sub-traffic.timer; test ! -e /etc/systemd/system/clash-sub-traffic.timer
~~~

~~~bash
install -o root -g root -m 0644 /opt/clash-sub/deploy/systemd/clash-sub-traffic.timer /etc/systemd/system/clash-sub-traffic.timer
~~~

先做只读 unit 验证，再让 systemd 重新加载。

~~~bash
systemd-analyze verify /etc/systemd/system/clash-sub-traffic.service /etc/systemd/system/clash-sub-traffic.timer
~~~

~~~bash
systemctl daemon-reload
~~~

先检查 timer 尚未启用，再启用；它只运行 clash-sub traffic-update，不生成 YAML。

~~~bash
systemctl is-enabled clash-sub-traffic.timer 2>/dev/null || true
~~~

~~~bash
systemctl enable --now clash-sub-traffic.timer
~~~

~~~bash
systemctl list-timers clash-sub-traffic.timer
~~~

## 11. 首次同步：先发布 owner，再同步成员

首次 owner 数据不能是空配置：<private-root>/home.yaml 必须有至少一个可解析代理。
先只读核对 service.yaml、配置的 private root 和当前状态；不要创建 proxies: []，
也不要跳过 home。

~~~bash
stat -c '%a %U:%G %n' /opt/clash-sub/private/config/service.yaml; sed -n '1,120p' /opt/clash-sub/private/config/service.yaml; clash-sub status
~~~

先确认 home snapshot 不存在，再以 root-only 模式创建。

~~~bash
test ! -e <private-root>/home.yaml
~~~

~~~bash
install -o root -g root -m 0600 /dev/null <private-root>/home.yaml
~~~

先核对模式，再写入下列合成示例并替换全部占位符。至少保留一个完整 proxy，所有真实
密码、地址、UUID 只留在这个文件。

~~~bash
stat -c '%a %U:%G %n' <private-root>/home.yaml
~~~

~~~bash
<editor> <private-root>/home.yaml
~~~

~~~yaml
proxies:
  - name: home-ss-<node-label>
    type: ss
    server: <home-proxy-host>
    port: <home-proxy-port>
    cipher: <home-proxy-cipher>
    password: <home-proxy-password>
~~~

保存后做 mode 与 parse 两项只读检查：必须是 600 root:root，且用实际服务的
load_proxy_snapshot 解析器确认至少一个 proxy。

~~~bash
stat -c '%a %U:%G %n' <private-root>/home.yaml
~~~

~~~bash
cd /opt/clash-sub && .venv/bin/python -c "from pathlib import Path; from clash_sub.sources import load_proxy_snapshot; print(len(load_proxy_snapshot(Path('<private-root>/home.yaml'))))"
~~~

先只读查看状态，再运行菜单并选择 `1`「更新机场订阅」，仅在隐藏输入中粘贴短时
HTTPS 机场 URL。成功时菜单保存 airport snapshot 并发布 owner 的 balanced、
standard、privacy；它不是空 home 的替代行为。

~~~bash
clash-sub status
~~~

~~~bash
clash-sub
~~~

owner 已发布后，先只读确认 owner 成功版本，再同步所有成员；sync 为成员发布各自
唯一的 standard。

~~~bash
clash-sub status
~~~

~~~bash
clash-sub sync
~~~

最后只读显示链接；完整 Token 仅通过既有安全渠道分发。

~~~bash
clash-sub links
~~~

## 12. 安装后验证与边界

以下均为只读检查：443 仅 Xray；80/8443 仅 Nginx；面板/订阅仅回环；UFW 仅 SSH、
TCP 80/443/8443；Nginx、timer 与发布状态均正常。

~~~bash
ss -H -lntup; ufw status numbered; nginx -t; systemctl list-timers clash-sub-traffic.timer; clash-sub status
~~~

- 客户端下载 https://sub.<domain>:8443/s/<token>/clash-<variant>.yaml 成功，错误
  token / 越权 variant 返回相同 404。
- 手机更新机场、回滚、令牌轮换、备份和故障恢复见
  [docs/operations.md](docs/operations.md) 与 [docs/private-data.md](docs/private-data.md)。
- 重装系统、清除磁盘、修改 DNS、更换 VPS / IP / 域名由管理员单独决策；内容回退
  使用 clash-sub history / clash-sub rollback 的五个成功版本，不覆盖私有备份。
