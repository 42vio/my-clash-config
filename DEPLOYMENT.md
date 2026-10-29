# 部署清单

本页只记录从已准备好的个人服务器到首次服务可用的步骤。日常更新、故障处理和完整恢复见[运维手册](docs/operations.md)。

## 部署前检查

- 使用干净的 apt/systemd Linux 主机，以 root 执行安装；磁盘至少保留 1 GiB 可用空间。
- 已准备主域名及其 Cloudflare API Token；安装过程会申请证书。
- 443/TCP 未被占用，3x-ui 已安装，且目标 owner 的 3x-ui client 已启用并可用其 email 识别。
- 先按下节完成 3x-ui 设置；不要提交订阅 URL、token 或任何私密 YAML。

防火墙

```
# 按顺序放行端口——这一行最重要，先保住 SSH
26019/tcp
80/tcp
443/tcp
```

BBR

https://vpsoso.com/blog/vps/tcp_better

https://github.com/666shen/tcp-dashboard



## 3x-ui 关键配置

| 项目 | 固定值 |
| --- | --- |
| 数据库 | `/etc/x-ui/x-ui.db` |
| 面板路径 | 随机且非 `/`，格式为 `/名称/`，只含字母、数字、`_`、`-` |
| 面板监听 | `127.0.0.1` |
| 面板 TLS | 证书与私钥字段留空 |
| 原始订阅服务 | 启用，且同时启用 Clash 订阅 |
| 原始订阅监听 | `127.0.0.1` |
| 原始订阅端口 | 保留有效且未冲突的本地端口，例如 `2096` |
| Reality 入站 | 启用的 `VLESS` + `TCP` + `REALITY`，端口 `10443` |

原始订阅只供本机 `clash-sub` 读取，不直接对公网开放。公网订阅入口为 `443`，由 Nginx 按 SNI 转到内部 `127.0.0.1:30443`；脚本读取 3x-ui 原始订阅时使用 `127.0.0.1:<原始订阅端口>`，这三个端口用途不同。

安装前保持 Reality 入站的公网 listen；安装成功并看到收口提示后，将该入站 listen 改为 `127.0.0.1`，端口仍为 `10443`。回滚安装后需改回 `0.0.0.0`。

## Reality Short ID 约定

本项目直接读取 3x-ui 的本地 Clash 订阅，不经过 VLESS 链接转换。以测试夹具对应的 3x-ui v3.6.0 为准，Clash 生成器将服务端 `realitySettings.shortIds[0]` 写入客户端 `reality-opts.short-id`；本项目只重写公网地址和端口，保留该值。多个服务端 Short ID 输出成一个客户端 Short ID 是正常行为，无需复制成多个节点。

- 新建单节点入站时，使用一个固定、非空的 Short ID，建议为 16 位十六进制字符串（8 字节）；不要在同步或刷新订阅时重新生成。协议也允许更短的偶数位十六进制值，本项目当前完整性校验要求非空。
- 已有入站保留当前首项及其顺序，确保已发布订阅的 `short-id` 仍包含在服务端 `shortIds` 中。不要为简化数组立即删除其他项：旧 VLESS 链接或 JSON 订阅可能已经使用这些值。待相关客户端迁移并验证后，再考虑收敛为单项。
- v3.6.0 的原始 VLESS 链接 `sid` 与 JSON 订阅 `shortId` 使用随机选项。未来若增加 QX 或其他转换入口，需要核对转换器是否保留该值；单项服务端数组可消除这些入口的随机选值，不能仅凭当前 Clash 链路推断它们稳定。本仓库目前没有 QX/VLESS 转换器。
- 本项目尚未将输出 `short-id` 与数据库数组独立交叉校验；当前选值由 3x-ui 自己从该入站数组产生。修改数组、升级 3x-ui 或切换订阅源后，重新生成配置并核对所选值、成员关系与连续两次生成结果，再执行客户端连接验证。配置校验通过不代表远端握手成功。
- 多设备需要独立停用、流量统计或额度时，在 3x-ui 为每台设备创建独立 client/UUID，并使用各自订阅。Short ID 是入站的可接受值列表，不是 UUID 的设备权限绑定；不同 UUID 可以共用同一个 Short ID。当前无需新增 UUID 哈希选值逻辑。

上游实现：[Clash 生成器](https://github.com/MHSanaei/3x-ui/blob/v3.6.0/internal/sub/clash_service.go)、[VLESS 链接生成器](https://github.com/MHSanaei/3x-ui/blob/v3.6.0/internal/sub/service.go)、[JSON 生成器](https://github.com/MHSanaei/3x-ui/blob/v3.6.0/internal/sub/json_service.go)。服务器实际版本及数据库内容需另行核实，不由测试夹具版本推定。

## 全新安装

先安装 Git，再克隆仓库；其余依赖由 `install.sh` 补齐。

```bash
apt-get update
apt-get install -y git
git clone https://github.com/42vio/my-clash-config /opt/my-clash-config
cd /opt/my-clash-config
bash install.sh
```

按提示输入主域名、Cloudflare API Token 和 owner 的 3x-ui client email；仅在需要 swap 时预先设置 `CLASH_SUB_SWAP_MB`。安装会创建本地虚拟环境、生成 `/usr/local/bin/clash-sub`、初始化运行时目录（含机场 provider 目录）、Nginx 配置、`/etc/tmpfiles.d/clash-sub-metadata.conf`（开机建立 `/run/clash-sub` 目录），并启用 `clash-sub-metadata.socket` 与 `clash-sub-airport-refresh.timer`（北京时间每天 06:00 开始自动刷新机场配置，随机延迟 0–2 小时）。订阅流量头由该 socket 按请求按需提供，不存在任何定时流量刷新任务。

安装过程按 12 个步骤显示当前操作和已完成进度；百分比表示完成的步骤比例，不是剩余时间估算。若 Python 安装阶段失败，修正错误后重新执行 `bash install.sh`，安装器会读取安装记录并沿用已经完成的阶段。

若已有证书尚未到续期时间，安装器只会复用它；但会先确认 acme.sh 已安全保存 Cloudflare 续期凭据。凭据缺失时，当前安装输入的 Token 会触发一次受控强制签发以让 acme.sh 自行保存凭据；该操作失败或仍未保存时以 `acme_cf_token_not_persisted` 失败，不会把证书步骤记为完成。不要手工编辑或复制 acme.sh 的凭据文件，也不要反复强制签发。

## 目录与权限

安装建立并校验以下布局；不要手工放宽权限：

```text
/var/lib/clash-sub/
├── private/          0700，root 所有：state.json、status.json、airport-source.json、traffic-cache.json、operation.lock、releases、current、staging
└── public/           02750，root:www-data：releases/
    └── provider/     02750，root:www-data：AmyTelecom.yaml (0640)
```

机场 provider 文件是 public 下唯一的非发布目录内容；它由机场更新流程原子写入，不随主配置发布。`airport-source.json`（机场来源记录：订阅开关页面地址、订阅链接与最近保存的流量）与 `traffic-cache.json`（3x-ui 流量缓存）都是 0600、root:root 的私密文件：来源记录与 provider 文件经日志式事务同时切换，崩溃中断的事务在下一次机场操作时恢复到完整的旧状态或完整的新状态；流量缓存则由元数据服务在五分钟刷新时独立原子更新。两次正式文件替换之间存在毫秒级窗口，恰好并发的订阅请求可能短暂取得新正文与旧流量头，随后恢复一致。

流量元数据 Socket 在运行时目录之外：`/run/clash-sub/metadata.sock`（0660 root:www-data），父目录 `/run/clash-sub`（0750 root:www-data）由 tmpfiles 规则在启动时建立，socket 由 `clash-sub-metadata.socket` 监听并按需激活 `clash-sub-metadata.service`。

## 首次初始化

1. 执行 `clash-sub`，主菜单选择 `1` 进入机场订阅子菜单，再选择 `1`（设置订阅开关页面），按可见提示粘贴带 `sid/token` 的订阅开关页面地址；输入会自动清理首尾空白。该操作会访问页面开启订阅、生成一条新订阅链接，下载成功后才同时保存开关页面地址、订阅链接与流量，并生成 `AmyTelecom.yaml`；任一步失败都保留旧状态。
2. 依次执行首次生成与检查：

   ```bash
   clash-sub sync
   clash-sub links
   clash-sub status
   ```

3. 将 Reality 入站 listen 改为 `127.0.0.1`，保持 `10443`，使公网仅保留 443。

`sync` 要求 Compat 模板、Balance DNS 模板与当前机场 provider 同时有效；provider 缺失或无效时整体拒绝执行，不会生成半套发布。

## 部署验证

```bash
nginx -t
systemctl status nginx clash-sub-metadata.socket
clash-sub status
clash-sub links
```

确认 `nginx -t` 通过、`clash-sub-metadata.socket` 已启用且监听、`status` 的最近错误为空、`links` 显示 owner 三条（`Clash-Compat.yaml`、`Clash-Balance.yaml`、仅机场的 `Clash-Airport.yaml`）与普通用户一条（`Clash-Compat.yaml`）链接。用其中一条链接请求一次订阅后，`clash-sub-metadata.service` 应被 socket 激活（`systemctl status clash-sub-metadata.service` 显示运行中），响应携带 `Subscription-Userinfo` 流量头。面板入口为安装输出的 `https://sub.<主域名><面板路径>/`；不要把链接或面板路径写入仓库。

浏览器标签图标由 Nginx 提供兜底：`/favicon.ico` 先请求 3x-ui，只有其返回 404 时才返回仓库中的 `assets/favicon.ico`。如果日后 3x-ui 在页面里声明自己的图标，面板路径仍由原代理直接转发，浏览器会使用其声明的图标。不要在 3x-ui 安装目录里手工替换文件。

## 升级与卸载

更新代码后在现有目录重新执行安装：

```bash
cd /opt/my-clash-config
git pull --ff-only
bash install.sh
```

已运行的服务器只需更新项目代码并重新生成 Nginx 配置，无需重装 3x-ui。项目发布到远端仓库后，以 root 在服务器执行：

```bash
cd /opt/my-clash-config
clash-sub update
nginx -t
curl -I https://sub.<主域名>/favicon.ico
```

`clash-sub update` 会拉取代码并用新模板重载 Nginx；`curl -I` 应返回 `200` 和图标的 Content-Type。浏览器可能缓存旧图标，必要时重新打开标签页或强制刷新。

若需要撤销整合安装，执行：

```bash
clash-sub rollback --install
```

该操作移除或还原本项目写入的 Nginx 配置与 systemd 文件，保留运行时目录、3x-ui 数据库及已签发证书；之后将 Reality 入站 listen 改回 `0.0.0.0` 以恢复 `10443` 直连。再次安装前按“3x-ui 关键配置”和“全新安装”重新执行。

## 备份与恢复

`clash-sub backup` 归档以下五个重建必需文件；已初始化网页时，额外包含 `private-root/web-settings.json`，用于保留页面链接、管理员密码哈希与面板开关：

```text
/etc/x-ui/x-ui.db
/etc/nginx/stream-conf.d/clash-sub.conf
/etc/nginx/conf.d/clash-sub.conf
/var/lib/clash-sub/private/state.json
/var/lib/clash-sub/private/airport-source.json
```

任何必需文件缺失时备份以稳定错误码失败，而不是产出不完整的归档。证书、机场 provider 文件（`AmyTelecom.yaml`）、流量缓存、发布历史、运行状态与 systemd 文件不进入备份；机场 provider 可在恢复后用备份里的来源记录一键重建。

重建恢复顺序：

1. 恢复 3x-ui 数据库（`/etc/x-ui/x-ui.db`）。
2. 恢复 `state.json` 与 `airport-source.json` 到私密运行时目录。
3. 重新安装项目并重新签发证书。
4. 执行 `clash-sub`，主菜单 `1` 进入机场订阅后选择 `2`（自动开启订阅并刷新）：用恢复的开关页面与已保存链接重新下载并重建 `AmyTelecom.yaml`，无需重新输入地址；页面自动化失效时先用浏览器手动开启订阅再选 `3` 刷新；真实链接也已失效时用 `4` 输入新链接。
5. 执行 `clash-sub sync`。
6. 核对并恢复两份 Nginx 配置。

保留原订阅链接要求恢复后的 x-ui client ID 与数据库一致，且不重新初始化 owner、不轮换订阅令牌。

## 路径与命令速查

| 用途 | 路径或命令 |
| --- | --- |
| 仓库 | `/opt/my-clash-config` |
| 管理命令 | `/usr/local/bin/clash-sub` |
| 3x-ui 数据库 | `/etc/x-ui/x-ui.db` |
| 私密运行时目录 | `/var/lib/clash-sub/private` |
| 公开发布目录 | `/var/lib/clash-sub/public` |
| 机场 provider | `/var/lib/clash-sub/public/provider/AmyTelecom.yaml` |
| 备用订阅来源与缓存（可选，0600） | `/var/lib/clash-sub/private/backup-source.json` |
| 机场来源记录 / 流量缓存 | `/var/lib/clash-sub/private/airport-source.json`、`/var/lib/clash-sub/private/traffic-cache.json` |
| 服务配置 | `/opt/my-clash-config/private/config/service.yaml` |
| Nginx 路由 | `/etc/nginx/clash-sub/routes.conf` |
| Nginx stream / HTTP 配置 | `/etc/nginx/stream-conf.d/clash-sub.conf` / `/etc/nginx/conf.d/clash-sub.conf` |
| 证书 | `/etc/ssl/domain/fullchain.pem`、`/etc/ssl/domain/privkey.pem` |
| 流量元数据 | `/run/clash-sub/metadata.sock`、`clash-sub-metadata.socket`、`clash-sub-metadata.service` |
| 启动恢复 | `clash-sub-recover.service`、`nginx.service.d/clash-sub-recover.conf` |
| 安装记录 | `/opt/my-clash-config/private/install-state.json` |
