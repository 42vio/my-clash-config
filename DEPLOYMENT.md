# 部署清单

本页只记录从已准备好的个人服务器到首次服务可用的步骤。日常更新、故障处理和完整恢复见[运维手册](docs/operations.md)。

## 部署前检查

- 使用干净的 apt/systemd Linux 主机，以 root 执行安装；磁盘至少保留 1 GiB 可用空间。
- 已准备主域名及其 Cloudflare API Token；安装过程会申请证书。
- 443/TCP 未被占用，3x-ui 已安装，且目标 owner 的 3x-ui client 已启用并可用其 email 识别。
- 先按下节完成 3x-ui 设置；不要提交订阅 URL、token 或任何私密 YAML。

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

## 全新安装

先安装 Git，再克隆仓库；其余依赖由 `install.sh` 补齐。

```bash
apt-get update
apt-get install -y git
git clone https://github.com/42vio/my-clash-config /opt/my-clash-config
cd /opt/my-clash-config
bash install.sh
```

按提示输入主域名、Cloudflare API Token 和 owner 的 3x-ui client email；仅在需要 swap 时预先设置 `CLASH_SUB_SWAP_MB`。安装会创建本地虚拟环境、生成 `/usr/local/bin/clash-sub`、初始化运行时目录（含机场 provider 目录）、Nginx 配置和 `clash-sub-traffic.timer`。

安装过程按 12 个步骤显示当前操作和已完成进度；百分比表示完成的步骤比例，不是剩余时间估算。若 Python 安装阶段失败，修正错误后重新执行 `bash install.sh`，安装器会读取安装记录并沿用已经完成的阶段。

## 目录与权限

安装建立并校验以下布局；不要手工放宽权限：

```text
/var/lib/clash-sub/
├── private/          0700，root 所有：state.json、releases、current、staging、journals
└── public/           02750，root:www-data：releases/
    └── provider/     02750，root:www-data：AmyTelecom.yaml (0640)
```

机场 provider 文件是 public 下唯一的非发布目录内容；它由机场更新流程原子写入，不随主配置发布。

## 首次初始化

1. 执行 `clash-sub`，在主菜单选择 `1`，按可见提示粘贴机场订阅地址；输入会自动清理首尾空白，成功后生成 `AmyTelecom.yaml`。
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
systemctl status nginx clash-sub-traffic.timer
clash-sub status
clash-sub links
```

确认 `nginx -t` 通过、`clash-sub-traffic.timer` 已启用且运行、`status` 的最近错误为空、`links` 显示 owner 两条（`Clash-Compat.yaml`、`Clash-Balance.yaml`）与普通用户一条（`Clash-Compat.yaml`）链接。面板入口为安装输出的 `https://sub.<主域名><面板路径>/`；不要把链接或面板路径写入仓库。

## 升级与卸载

更新代码后在现有目录重新执行安装：

```bash
cd /opt/my-clash-config
git pull --ff-only
bash install.sh
```

若需要撤销整合安装，执行：

```bash
clash-sub rollback --install
```

该操作移除或还原本项目写入的 Nginx 配置与 systemd 文件，保留运行时目录、3x-ui 数据库及已签发证书；之后将 Reality 入站 listen 改回 `0.0.0.0` 以恢复 `10443` 直连。再次安装前按“3x-ui 关键配置”和“全新安装”重新执行。

## 备份与恢复

`clash-sub backup` 归档且仅归档四个重建必需文件：

```text
/etc/x-ui/x-ui.db
/etc/nginx/stream-conf.d/clash-sub.conf
/etc/nginx/conf.d/clash-sub.conf
/var/lib/clash-sub/private/state.json
```

任何必需文件缺失时备份以稳定错误码失败，而不是产出不完整的归档。证书、机场 provider、发布历史、运行状态与 systemd 文件不进入备份。

重建恢复顺序：

1. 恢复 3x-ui 数据库（`/etc/x-ui/x-ui.db`）。
2. 恢复 `state.json`。
3. 重新安装项目并重新签发证书。
4. 重新导入机场订阅，生成 `AmyTelecom.yaml`。
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
| 服务配置 | `/var/lib/clash-sub/private/config/service.yaml` |
| Nginx 路由 | `/etc/nginx/clash-sub/routes.conf` |
| Nginx stream / HTTP 配置 | `/etc/nginx/stream-conf.d/clash-sub.conf` / `/etc/nginx/conf.d/clash-sub.conf` |
| 证书 | `/etc/ssl/domain/fullchain.pem`、`/etc/ssl/domain/privkey.pem` |
| 定时更新 | `clash-sub-traffic.service`、`clash-sub-traffic.timer` |
| 启动恢复 | `clash-sub-recover.service`、`nginx.service.d/clash-sub-recover.conf` |
| 安装记录 | `/opt/my-clash-config/private/install-state.json` |
