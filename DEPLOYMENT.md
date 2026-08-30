# 部署清单

本页只记录从已准备好的个人服务器到首次服务可用的步骤。日常更新、故障处理和完整恢复见[运维手册](docs/operations.md)。

## 部署前检查

- 使用干净的 apt/systemd Linux 主机，以 root 执行安装；磁盘至少保留 1 GiB 可用空间。
- 已准备主域名及其 Cloudflare API Token；安装过程会申请证书。
- 443/TCP 未被占用，3x-ui 已安装，且目标 owner 的 3x-ui client 已启用并可用其 email 识别。
- 先按下节完成 3x-ui 设置；不要提交 Home、订阅 URL、token 或任何私密 YAML。

## 3x-ui 关键配置

| 项目 | 固定值 |
| --- | --- |
| 数据库 | `/etc/x-ui/x-ui.db` |
| 面板路径 | 随机且非 `/`，格式为 `/名称/`，只含字母、数字、`_`、`-` |
| 面板监听 | `127.0.0.1` |
| 面板 TLS | 证书与私钥字段留空 |
| Reality 入站 | 启用的 `VLESS` + `TCP` + `REALITY`，端口 `10443` |

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

按提示输入主域名、Cloudflare API Token 和 owner 的 3x-ui client email；仅在需要 swap 时预先设置 `CLASH_SUB_SWAP_MB`。安装会创建本地虚拟环境、生成 `/usr/local/bin/clash-sub`、初始化运行时目录、Nginx 配置和 `clash-sub-traffic.timer`。

## 首次初始化

1. 用 SFTP 人工覆盖服务器 `/var/lib/clash-sub/private/home.yaml`；该文件是唯一的 Home 落位方式，随后确认权限为 `0600`。不使用上传命令。
2. 执行 `clash-sub`，在主菜单选择 `1`，按提示导入机场订阅。
3. 依次执行首次生成与检查：

   ```bash
   clash-sub sync
   clash-sub links
   clash-sub status
   ```

4. 将 Reality 入站 listen 改为 `127.0.0.1`，保持 `10443`，使公网仅保留 443。

## 部署验证

```bash
nginx -t
systemctl status nginx clash-sub-traffic.timer
clash-sub status
clash-sub links
```

确认 `nginx -t` 通过、`clash-sub-traffic.timer` 已启用且运行、`status` 的最近错误为空、`links` 只显示对应所有者的链接。面板入口为安装输出的 `https://sub.<主域名><面板路径>/`；不要把链接或面板路径写入仓库。

## 重新部署与安装回滚

更新代码后需要重新执行安装流程时，在现有目录执行：

```bash
cd /opt/my-clash-config
git pull --ff-only
bash install.sh
```

若需要撤销整合安装，执行：

```bash
clash-sub rollback --install
```

该操作移除本项目写入的 Nginx 配置、systemd unit 和发布目录，保留 3x-ui 数据库及已签发证书；之后将 Reality 入站 listen 改回 `0.0.0.0` 以恢复 `10443` 直连。再次安装前按“3x-ui 关键配置”和“全新安装”重新执行。

## 路径与命令速查

| 用途 | 路径或命令 |
| --- | --- |
| 仓库 | `/opt/my-clash-config` |
| 管理命令 | `/usr/local/bin/clash-sub` |
| 3x-ui 数据库 | `/etc/x-ui/x-ui.db` |
| 私密运行时目录 / Home | `/var/lib/clash-sub/private` / `/var/lib/clash-sub/private/home.yaml` |
| 公开发布目录 | `/var/lib/clash-sub/public` |
| 服务配置 | `/var/lib/clash-sub/private/config/service.yaml` |
| Nginx 路由 | `/etc/nginx/clash-sub/routes.conf` |
| Nginx stream / HTTP 配置 | `/etc/nginx/stream-conf.d/clash-sub.conf` / `/etc/nginx/conf.d/clash-sub.conf` |
| 证书 | `/etc/ssl/domain/fullchain.pem`、`/etc/ssl/domain/privkey.pem` |
| 定时更新 | `clash-sub-traffic.service`、`clash-sub-traffic.timer` |
| 启动恢复 | `clash-sub-recover.service`、`nginx.service.d/clash-sub-recover.conf` |
| 安装记录 | `/opt/my-clash-config/private/install-state.json` |
