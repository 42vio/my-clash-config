# 运维、故障与恢复

## 快速入口

| 目标 | 从这里开始 |
| --- | --- |
| 开发 Mac 更新模板 | [模板更新](#模板更新) |
| 服务器只更新机场 | [机场更新](#机场更新) |
| 客户端手动刷新机场 | [手动刷新 provider](#手动刷新-provider) |
| 发布、状态与链接 | [同步、状态与链接](#同步状态与链接) |
| 订阅异常或服务不可用 | [故障检查顺序](#故障检查顺序) |
| 保存当前服务器 | [备份](#备份) |
| 重装机器或更换域名/VPS | [全新服务器恢复](#全新服务器恢复) 或 [域名或-vps-迁移](#域名或-vps-迁移) |
| 修改本机 Home 节点 | [Home 脚本维护](#home-脚本维护) |
| 不再由脚本继续维护 | [人工接管](#人工接管) |

以下服务器命令均以 root 在 `/opt/my-clash-config` 的已安装环境执行。首次安装、3x-ui 固定设置、目录权限和安装回滚见[部署清单](../DEPLOYMENT.md)；模板的拆分、注释规则与机场引用见[模板设计](template-design.md)。

## 模板更新

日常默认来源为开发 Mac iCloud 目录下的 `Clash-Compat.yaml` 和 `Clash-Balance.yaml`：

```text
~/Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/
├── Clash-Compat.yaml
└── Clash-Balance.yaml
```

```bash
./bin/clash-sub template-sync
```

只替换一个来源时，每次只传一个选项；未指定的默认来源不会被读取：

```bash
./bin/clash-sub template-sync --compat /path/Clash-Compat.yaml
./bin/clash-sub template-sync --balance /path/Clash-Balance.yaml
```

同步先净化并验证全部候选，随后原子写入；路径不可用、YAML 解析或验证失败时，目标不会留下半更新。报告只允许出现公开路径与被忽略的顶层路径名；不得把机场 URL、节点或凭据复制到终端记录、提交信息或故障报告。

完成后只审查受跟踪改动，并运行：

```bash
git diff --check
.venv/bin/python -m unittest discover -s tests -p 'test*.py'
.venv/bin/python scripts/scan_tracked_secrets.py
.venv/bin/python scripts/scan_tracked_secrets.py --private-root private
```

模板同步只更新仓库模板，不触碰服务器。模板变更生效需要提交、推送，并在服务器 `clash-sub update` 后 `sync`。

## 机场更新

机场更新与主配置发布完全解耦：只更新 provider 文件，不生成、不发布、不切换任何主配置。

在服务器执行 `clash-sub`，主菜单选择 `1`，在可见提示中粘贴临时 HTTPS 订阅地址；输入会自动清理首尾空白。地址会显示在当前终端，但不要把它作为 shell 参数、写入历史、仓库、重定向文件或工单。成功后：

```bash
clash-sub status
```

更新流程下载原始字节到同目录随机临时文件，验证（YAML 结构、非空代理列表、Mihomo 以本地文件 provider 装载校验）后原子替换 `/var/lib/clash-sub/public/provider/AmyTelecom-Provider.yaml`。失败时保留当前 provider，只输出隐藏了源 URL、令牌、UUID 和节点敏感字段的稳定错误码。

机场字节变化本身不会创建新发布：发布输入哈希只含 3x-ui 数据。需要把新机场节点刷进客户端时，在 Clash Verge 中手动刷新 provider（见下节），或在内容需要重排时执行 `clash-sub sync`。

## 手动刷新 provider

owner 客户端在 Clash Verge 的「订阅」页选中 `Clash-Compat` 或 `Clash-Balance`，对 `AmyTelecom` provider 执行手动更新即可拉取最新机场节点；主配置本身无需重新下载。provider 的自动刷新间隔为 7 天。

## 同步、状态与链接

模板或 3x-ui 数据有效变更后发布：

```bash
clash-sub sync
clash-sub status
clash-sub links
```

`sync` 要求当前机场 provider 存在且有效，然后恢复中断的运行时发布、准备并验证候选、最后激活；机场 provider 缺失或无效时整体拒绝（`airport_provider_required`）。若输出“同步部分完成”，按其中公开的客户端 ID 与错误代码处理，且以 `status` 的最近错误和 pending 项为准。`links` 输出可用订阅链接（owner 两条、普通用户一条），屏幕录制或终端转存前先避免泄露。

日常代码维护使用：

```bash
clash-sub update
clash-sub sync
```

`update` 先创建更新前快照，执行 fast-forward 拉取和依赖同步，再由新代码完成 systemd 加固与 Nginx 重渲染；更新后仍需重新发布。

## 用户管理

普通用户与 owner 都来自 3x-ui client；在面板中增删或启停 client 后执行 `clash-sub sync` 即可生效。owner 由安装时输入的 email 确定；owner 身份变化才执行：

```bash
clash-sub reinitialize-owner <用户ID>
clash-sub sync
```

重新初始化只重建身份映射；稳定 provider 已在服务器上时无需重新导入机场，直接 `sync` 即可。

## 历史与回退

先从 `status` 或 `links` 确认目标用户 ID，再查看该用户可用发布版本：

```bash
clash-sub history <用户ID>
clash-sub rollback <用户ID> <发布版本ID>
clash-sub status
clash-sub links
```

回滚边界：只回滚主配置和相应路由映射；机场 provider 文件、身份令牌与本机 Home 脚本不参与回滚。只有历史中存在的版本可以回退。泄露订阅链接时，轮换对应用户的令牌并重新取链接；旧链接立即失效：

```bash
clash-sub rotate-link <用户ID>
clash-sub links
```

owner 轮换会以新令牌 URL 重新生成两份主配置；provider 文件本身不移动，机场上游不会被再次联系。普通用户轮换只改路由，不触碰 provider。

## Home 脚本维护

Home 节点、`HomeServer` 与 `ProxyServer` 分组只存在于本机 `private/clash-verge-home.js`。修改 Home 时直接编辑脚本，随后：

```bash
node --check private/clash-verge-home.js
```

语法通过后在 Clash Verge 中重新载入配置验证效果。脚本只对 `Clash-Compat` 和 `Clash-Balance` 两个标题生效；其他 profile 原样返回。不要把脚本内容或其中的节点值写入仓库、文档或终端记录。

## 证书和组件更新

先查看证书，再按需要强制续期；`--domain` 不是改域名入口，会被拒绝：

```bash
clash-sub cert
clash-sub cert --renew
clash-sub status
```

单独升级 Mihomo 校验器时：

```bash
clash-sub mihomo-update
clash-sub sync
clash-sub status
```

每日流量任务由 `clash-sub-traffic.timer` 触发。检查定时器与服务状态：

```bash
systemctl status clash-sub-traffic.timer clash-sub-traffic.service
```

## 故障检查顺序

按以下顺序停止；前一步异常时先修复，不要直接重装或删除发布目录。

1. 查看发布与健康摘要：

   ```bash
   clash-sub status
   clash-sub links
   ```

   记录公开的错误代码、pending 项与受影响用户 ID，不记录订阅链接或私密值。

2. 检查 Nginx、x-ui 与定时器：

   ```bash
   nginx -t
   systemctl status nginx x-ui clash-sub-traffic.timer
   journalctl -u nginx -u x-ui -u clash-sub-traffic.service -n 100 --no-pager
   ```

3. 若发布过程中断、启动恢复未完成，先运行：

   ```bash
   clash-sub recover
   nginx -t
   clash-sub status
   ```

   `clash-sub-recover.service` 也会在启动时于 Nginx 之前处理这类激活日志；手动恢复成功后再决定是否重试 `sync`。

4. `airport_provider_required` 表示稳定 provider 缺失或无效：按[机场更新](#机场更新)重新导入订阅，再 `sync`。机场更新失败会保留原 provider，并给出不含订阅地址的分类错误码：

   - `airport_url_invalid`：不是允许的 HTTPS 地址；
   - `airport_redirect_invalid`：重定向超过三次或转向非 HTTPS 地址；
   - `airport_download_failed`：网络、TLS 证书或上游响应失败；
   - `airport_document_invalid`：返回内容不是顶层含非空 `proxies:` 的 Clash YAML；
   - `airport_document_too_large`：返回内容超过允许大小；
   - `airport_provider_invalid`：Mihomo 无法把候选内容作为文件 provider 装载，或现有 provider 权限/类型不合规；
   - `airport_provider_write_failed`：服务器无法原子写入 provider 文件。

   不要反复粘贴地址或把地址发送到日志；只记录上述错误码。

5. 本地模板变更后失败时，回到开发 Mac 运行模板同步与两种密钥扫描；不要从服务器或日志取回私密内容。

6. 当前发布有效但内容不合预期时，使用[历史与回退](#历史与回退)的用户级回退；仅整合安装本身需要撤销时，按[部署清单](../DEPLOYMENT.md#升级与卸载)执行 `clash-sub rollback --install`。

## 备份

变更前、更新前和迁移前都创建备份：

```bash
clash-sub backup
```

备份写入仓库 `backups/`，文件权限为 `0600`。归档只包含四个重建必需文件：3x-ui 数据库、两份 Nginx 配置和运行时 `state.json`；证书、机场 provider、发布历史、运行状态与 systemd 文件不进入备份。任何必需文件缺失时备份直接失败。归档含私密数据：立即复制到受保护的离线或加密位置，不上传到仓库、公开网盘、工单或聊天记录。

备份创建后验证文件存在且权限正确；不要解包到仓库或把内容贴进记录：

```bash
ls -l backups/clash-sub-backup-*.tar.gz
```

## 全新服务器恢复

恢复以新服务器为目标，保留旧服务器直到验收完成。顺序如下：

1. 从受保护备份恢复 3x-ui 数据库与入站/client 配置；按[部署清单](../DEPLOYMENT.md#3x-ui-关键配置)检查面板、Reality 入站和端口。
2. 恢复备份中的 `state.json` 到新服务器的私密运行时目录。
3. 克隆仓库到 `/opt/my-clash-config`，按[部署清单](../DEPLOYMENT.md#全新安装)执行 `bash install.sh`；安装时仅在隐藏提示输入新主机所需的域名、Cloudflare token 与 owner email。证书由安装流程重新签发。
4. 通过可见提示重新导入机场订阅，生成 `AmyTelecom-Provider.yaml`，然后发布并验收：

   ```bash
   clash-sub sync
   nginx -t
   systemctl status nginx x-ui clash-sub-traffic.timer
   clash-sub status
   clash-sub links
   ```

5. 核对并恢复两份 Nginx 配置（stream 与 HTTP 的 `clash-sub.conf`）。
6. 验证 owner 与普通用户的授权范围、`status` 最近错误为空、链接可访问后，才停止旧服务器。保留原订阅链接要求 x-ui client ID 与数据库一致，且不重新初始化 owner、不轮换令牌。

## 域名或 VPS 迁移

既有安装记录不允许用 `clash-sub cert --domain` 或再次安装直接改域名；新域名或新 VPS 一律按[全新服务器恢复](#全新服务器恢复)建立新环境。

1. 在旧服务器执行 `clash-sub backup`，并保留旧服务继续运行。
2. 在新主机完成恢复和验收；新域名的 DNS、证书和 Nginx 由安装流程处理。
3. 用新域名的 `clash-sub links` 验证每个预期链接，并确认 Nginx、x-ui 与流量定时器正常。
4. 仅在新发布物和客户端实际连通后切换 DNS/客户端；保留旧主机作为回退点。
5. 稳定观察后再人工撤销旧主机的公开入口和访问凭据。不要在切换前执行安装回滚或删除旧备份。

## 人工接管

需要永久停止脚本化维护时，先创建并离线保存完整备份，记录公开的版本号、服务状态和待办事项；不要记录订阅链接、token 或机场地址。将日常变更切换为受控的人工操作前，先冻结 `clash-sub update`、`sync`、`rotate-link` 等写入操作，避免两套流程同时发布。

若仅撤销本项目的整合安装，使用 `clash-sub rollback --install`，并按部署文档恢复 Reality 入站公网 listen。该回滚保留运行时目录、3x-ui 数据库和已签发证书；接管人必须决定这些保留数据的后续备份、权限与删除策略。完成交接前，不删除旧服务器、备份或安装记录。
