# 运维手册（clash-sub）

日常管理只记一个命令：

```bash
clash-sub
```

无参数命令显示交互菜单（`1`–`4` 选择，`0` 退出）：

```text
1. 更新机场订阅
2. 同步所有配置
3. 查看订阅链接
4. 查看状态和历史版本
0. 退出
```

所有输出默认脱敏：不显示令牌、UUID、subId、节点凭据或源 URL。唯一例外是
「查看订阅链接」与 `rotate-link`——它们的目的就是展示完整地址。

## 非交互子命令（systemd 与排错用）

```text
clash-sub sync                    # 重新读取 3x-ui/模板/机场/家庭节点并发布（菜单 2 等价）
clash-sub traffic-update          # 仅更新流量响应头（每日 timer 调用）
clash-sub status                  # 最后成功时间、每用户当前版本、待同步来源、最近错误
clash-sub links                   # 全部有效订阅地址（按 email 分组 + 六位识别码）
clash-sub history <user-id>       # 该用户最近五个成功版本
clash-sub rollback <user-id> <release-id>
clash-sub rotate-link <user-id>   # 轮换令牌；随后用 root-only links 重新查看
clash-sub reinitialize-owner <numeric-client-id>  # 仅持久化 owner ID 消失后的人工迁移
clash-sub recover                 # root-only；启动恢复 unit 调用，不要求 Nginx 已运行
```

失败时输出只有稳定错误代码（如 `操作失败（错误代码：xui_snapshot_failed）`），
不含敏感值。没有 `refresh` 命令，也没有兼容别名。

## 机场更新（手机 SSH 完整流程）

机场入口只在本机手机浏览器里保持登录（Cookie 留在手机上，**从不导出到
VPS**，服务器不保存任何机场登录状态或机场 URL）：

1. 手机浏览器登录机场后台，生成一条**短时效临时订阅 URL**（约 5 分钟）。
2. 手机 SSH 连接服务器，执行 `clash-sub`，选 `1`（或直接用菜单），在
   隐藏输入提示「请输入机场订阅地址：」处粘贴 URL 回车。隐藏输入不回显，
   URL 不进入 argv、shell history、环境变量、日志或任何持久化文件。
3. 输入框仅接受 https:// 开头的地址；下载有固定超时与响应体上限。成功后
   服务器只保存规范化的**节点快照**（`<private-root>/airport.yaml`，不含
   URL），并立即重新生成、校验、发布 owner 的三个 variant，同时更新
   owner 流量头。
4. 失败（URL 过期、非 YAML、proxies 为空、任一 owner variant 校验不过）
   时保留旧快照与旧发布，`clash-sub status` 会显示错误代码。

### 机场要求「生成链接与下载同出口」时

部分机场要求生成订阅链接与下载链接使用同一公网出口。此时在手机
Quantumult X 中**只为机场门户/API 域名**添加分流规则，把这些域名指到
owner 的 3x-ui REALITY 节点即可——不必切换全局代理：

```text
# Quantumult X [filter] 分流（示例占位域名）
HOST-SUFFIX,portal.example.com,Owner-REALITY
HOST-SUFFIX,api.portal.example.com,Owner-REALITY
HOST,cdn.portal.example.com,Owner-REALITY
FINAL,DIRECT
```

机场域名换成真实值，其余流量保持原状。手机保持 QX 开启时，机场看到的
出口即 VPS，临时链接即可正常生成。

## 客户端刷新是用户自己的责任

下载订阅**不会**触发任何生成或实时查询——Nginx 只回静态 YAML 与最近一次
保存的流量头。Clash 客户端是否自动拉取由各用户客户端的更新设置决定；
未设置更新间隔的客户端需要手动点击更新。

## 流量头

- 每日 systemd timer 运行一次 `clash-sub traffic-update`：从**只读 SQLite**
  读取 3x-ui 客户端配额，只更新 Nginx 路由里的 `Subscription-Userinfo` 头，不生成 YAML、
  不创建版本。
- 回环 Clash endpoint 只提供各客户端的 proxy YAML；它不承担流量查询。
- 菜单「同步所有配置」与机场更新也会刷新流量头。
- 流量任务失败时保留上一份响应头，订阅仍可下载。
- 已知限制：两次更新之间突然耗尽的配额会短暂滞后显示；3x-ui 仍按自身
  规则禁用客户端。本项目不为此增加实时查询或提醒。

## 历史与回退

- 每位用户只保留**最近五个成功版本**；第 6 次成功发布清理最旧版本。
  参考原件（`private/reference-configs/`）永久保留，不参与清理。
- `clash-sub history <user-id>` 列出可用版本；`clash-sub rollback
  <user-id> <release-id>` 原子切回该版本（重新校验哈希，不重新抓取来源、
  不重新渲染）。
- 任一失败（来源获取、结构校验、Mihomo 校验、Nginx 激活）都不改变当前
  版本；owner 的三个 variant 是一个原子集合，要么全部成功要么全部保留
  旧版本。Nginx 配置检查（`nginx -t`）失败时不替换 include、不 reload。
- 普通用户彼此隔离：一个用户失败只保留该用户旧版本，不阻塞其他用户。

## 令牌轮换与凭据撤销（泄漏响应）

订阅链接等于密码——任何持有者都能下载展开后的全部节点凭据。怀疑泄漏时：

1. `clash-sub rotate-link <user-id>`：随机核心与六位识别码同时更换；旧路径
   立即撤销（404）。新链接可随时由 root-only `clash-sub links` 重新查看，
   旧令牌不进状态、错误或历史日志。
2. 在 3x-ui 面板撤销/重建该用户的客户端（重置 UUID），因为旧配置里已经
   包含旧 UUID——轮换链接不能撤销已导入的节点凭据。
3. 把新订阅链接通过既有安全渠道发给该用户一次。

禁用或删除 3x-ui 客户端后，下次手动同步自动撤销其公开路径，但 root-only
历史版本保留用于审计与人工恢复；重新启用同一客户端恢复原令牌，删除后
重建则获得新令牌。

## acme.sh 版本维护

安装器当前固定使用经过校验的 **acme.sh 3.1.4** release，并在执行前核对
固定的 SHA-256。不要启用 `acme.sh --upgrade --auto-upgrade`：程序自动升级与
证书自动续期是两件事，证书仍由 acme.sh 自带 cron 自动续期，固定程序版本
不会阻止续期。

把 acme.sh 版本检查纳入**每季度**运维清单；出现 acme.sh 安全公告、Cloudflare
DNS API 变化、Let's Encrypt 兼容问题或续期失败时，不等季度检查，立即评估升级。
没有上述触发条件且续期正常时，不需要为了追随最新版而升级。

升级必须人工完成以下闭环：

1. 从 acme.sh 官方 release 选择明确版本，不使用 `master`、`latest` 或管道执行的
   在线安装脚本。
2. 在受信任环境下载 release 压缩包，计算 SHA-256；同时更新
   `clash_sub/installer.py` 中的版本 URL、解压目录和 SHA-256，三者必须对应同一版本。
3. 运行 `CertificatePhaseTests`、全量测试和 secret scan；随后在 Debian 测试机完成
   一次签发/续期与 Nginx reload 验证。
4. 验证通过后再部署，并在私有运维记录中写下版本、校验值、升级日期和下次季度检查日期。

## 3x-ui 升级流程

1. **备份**：`cp -a /etc/x-ui/x-ui.db /root/x-ui.db.pre-upgrade`（示例
   路径，以私有配置为准），并确认最近五个 release 完整。
2. **停止**：`systemctl stop x-ui`，避免升级期间数据库被写。
3. **在副本上核对结构**：对备份副本只读检查表与字段（客户端表、subId、
   启用状态、配额/到期、订阅服务设置）与当前固定预期一致；结构变化时
   **不要升级**，先在本仓库测试环境验证新 schema。
4. **升级** 3x-ui / Xray（人工执行官方安装步骤），保持或重新固定版本。
5. `clash-sub status` 然后 `clash-sub sync`：数据库结构不匹配时全局
   失败关闭、非零退出；此时客户端**继续使用旧 YAML 和旧路由**，直到人工
   确认兼容并成功同步。

### owner 数据库 ID 消失后的显式恢复

如果迁移/重建 3x-ui 后 `clash-sub status` 返回
`owner_reinitialization_required`，不要修改 state.json，也不要猜新 ID：先在
3x-ui 确认新客户端的 numeric ID 与 `service.yaml` 的 `owner-email` **完全一致**，
再执行一次非交互命令：

```bash
clash-sub reinitialize-owner 123
```

该命令会原子撤销失效 owner 的公开路由/令牌，保留未变化数据库 ID 的映射；新
owner 没有 release，状态会显示 pending。随后按手机流程更新机场快照并执行
`clash-sub sync`，才会重新发布 owner 三份配置。验证、Nginx 或恢复 journal
失败时命令不会替换旧 state/routes。

### 崩溃工件的只读盘点

正常成功发布只保留最近五个 release。断电时配对不完整或损坏的 staged/release
目录**不会自动删除**：自动清理需要同时证明它不被 state、current marker、routes
或 prepared activation journal 引用，当前版本刻意不做这个高风险判断。排障时 root
只读盘点，先备份再人工处理：

```bash
find /var/lib/clash-sub/private/staging /var/lib/clash-sub/private/releases /var/lib/clash-sub/public/releases -xdev -printf '%m %u:%g %p\n' 2>/dev/null | sort
```

运行时激活候选文件也只读盘点；下列匹配严格限定为 state、机场快照、journal、
current marker 与 routes 候选名，不把其他隐藏文件当成可处理对象。命令只输出路径，
不删除任何文件：

```bash
while IFS= read -r -d '' candidate; do
  if [[ "$candidate" =~ ^/var/lib/clash-sub/private/\.state\.json\.[[:alnum:]_]+$ ||
        "$candidate" =~ ^/var/lib/clash-sub/private/\.airport\.yaml\.[[:alnum:]_]+$ ||
        "$candidate" =~ ^/var/lib/clash-sub/private/\.\.activation-journal\.json\.[[:alnum:]_]+$ ||
        "$candidate" =~ ^/var/lib/clash-sub/private/current/\.[1-9][0-9]*\.[[:alnum:]_]+$ ||
        "$candidate" =~ ^/etc/nginx/clash-sub/\.routes\.conf\.[[:alnum:]_]+$ ]]; then
    printf '%s\n' "$candidate"
  fi
done < <(
  {
    find /var/lib/clash-sub/private -path /var/lib/clash-sub/private/current -prune -o -type f -print0
    find /var/lib/clash-sub/private/current -type f -print0
    find /etc/nginx/clash-sub -type f -print0
  } 2>/dev/null
) | sort
```

同时检查 `/var/lib/clash-sub/private/.activation-journal.json`；若存在，先执行
`clash-sub recover`，不得手动删除 journal 或 release。

## 更换域名 / 更换 VPS 或 IP

- **更换域名**：域名变更流程见 [recovery.md](recovery.md) 的「域名变更」一节
  （service.yaml 的 `subscription-authority` 与 `install-state.json` 的 domain →
  重签证书 → `clash-sub update` → `clash-sub sync`）；旧订阅 URL 随之失效，
  完成后把新链接发给每位用户一次。
- **更换 VPS / IP**：在新 VPS 上按 [DEPLOYMENT.md](../DEPLOYMENT.md) 重新
  部署；迁移 `<private-root>`（含 state.json 与家庭/机场快照）后重新签发
  证书、重建 3x-ui 客户端凭据；DNS A 记录指向新 IP；订阅地址不变（域名
  未换时）。旧 VPS 上的令牌与节点凭据全部作废。
- IP 被封时订阅与 REALITY 同时失效（同机部署）；按预先保留的离线恢复
  清单（记录在管理员私有部署日志中）恢复，换 IP 后通过既有安全渠道向
  每位用户发送一次新订阅地址。

## 3x-ui 的 Limit IP

3x-ui 面板中每客户端的 `Limit IP` 限制的是**同时观察到的公网源 IP 数**，
不是可靠设备数或连接数：

- 同一 NAT 后多台设备可能只算一个 IP；单设备在 Wi-Fi 与移动网络间切换
  可能算两个；CDN / IP Tunnel 场景可能不准。
- 该限制只约束 3x-ui 节点，不能约束机场或家庭节点。
- 建议其他用户从 `Limit IP = 2` 起步；该值在 3x-ui 面板配置，本服务不
  保存面板凭据、不代改面板设置。

## 私有数据备份

`/opt/my-clash-config/private/config/service.yaml` 与配置的 `<private-root>` 是必须
一起备份的两项私有数据：后者包含 state.json、机场/家庭快照、releases 与参考
原件，前者包含服务设置且不位于 private root 内。备份与恢复只在管理员控制的
加密存储之间进行，两个副本均保持 root-only；恢复前保留当前副本，恢复后核对
service.yaml 为 `0600 root:root`、private root 为 `0700 root:root` 和内部私有
文件为 `0600 root:root`。Git 与普通备份介质不得携带它们（见
[private-data.md](private-data.md)）。
