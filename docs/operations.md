# 运维、故障与恢复

## 快速入口

| 目标 | 从这里开始 |
| --- | --- |
| 开发 Mac 更新模板 | [模板更新](#模板更新) |
| 服务器更换/刷新机场、查看机场状态 | [机场更新](#机场更新) |
| 客户端手动刷新机场 | [手动刷新 provider](#手动刷新-provider) |
| 订阅流量头的工作方式与降级 | [订阅流量元数据](#订阅流量元数据) |
| 发布、状态与链接 | [同步、状态与链接](#同步状态与链接) |
| 订阅异常或服务不可用 | [故障检查顺序](#故障检查顺序) |
| 保存当前服务器 | [备份](#备份) |
| 重装机器或更换域名/VPS | [全新服务器恢复](#全新服务器恢复) 或 [域名或-vps-迁移](#域名或-vps-迁移) |
| 修改本机 Home 节点 | [Home 脚本维护](#home-脚本维护) |
| 不再由脚本继续维护 | [人工接管](#人工接管) |

以下服务器命令均以 root 在 `/opt/my-clash-config` 的已安装环境执行。首次安装、3x-ui 固定设置、目录权限和安装回滚见[部署清单](../DEPLOYMENT.md)；模板的拆分、注释规则与机场引用见[模板设计](template-design.md)。

交互菜单在首次进入或返回上一级时显示完整选项。操作结束后保留结果，只显示一行继续操作提示：输入编号直接执行下一项，按回车展开当前菜单，输入 `0` 返回上一级（主菜单中为退出）。

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

服务器执行 `clash-sub update` 时，post-update 会幂等补齐并校正运行时目录（包括机场 provider 目录），再更新 systemd 与 Nginx；旧安装无需手工追补新目录。

## 机场更新

机场更新与主配置发布完全解耦：只更新 provider 文件与来源记录，不读取 x-ui、不渲染模板、不运行 Mihomo、不生成发布、不执行 sync、不激活 Nginx。

在服务器执行 `clash-sub`，主菜单选择 `1` 进入机场订阅子菜单：

```text
1. 设置订阅开关页面
2. 自动开启订阅并刷新（推荐）
3. 手动开启订阅后刷新
4. 使用新订阅链接更新
5. 查看机场状态
0. 返回
```

- **设置订阅开关页面（1）**：首次使用或更换机场时执行。在可见提示中粘贴带 `sid/token` 的订阅开关页面地址，输入自动清理首尾空白。该操作每次都强制完成全链路验证——访问页面开启订阅、生成一条新订阅链接、下载成功后才把开关页面地址、订阅链接、新正文与新流量一起保存；任一步失败时旧的开关页面、链接、正文与流量全部保留。
- **自动开启订阅并刷新（2）**：日常推荐操作，也是定时任务复用的同一条链路。每次执行都会先访问一次开关页面开启订阅（不记录持续开启时间），然后优先用已保存的旧链接下载；旧链接失败且页面可用时才生成新链接重试。页面临时不可用时仍会尝试旧链接；页面和旧链接都不可用时本次跳过并完整保留旧数据。尚未设置开关页面时提示先用菜单 1。
- **手动开启订阅后刷新（3）**：机场页面自动化失效时的过渡手段。先在浏览器里手动打开订阅开关页面确认订阅已开启，回车后程序只用已保存的旧链接下载——不访问、不解析、也不显示开关页面地址，不生成新链接。
- **使用新订阅链接更新（4）**：永久可用的人工兜底，页面改版或自动化长期失效时使用。完全不访问订阅开关页面，在可见提示中粘贴新的订阅链接；下载成功后保存新链接并更新 `AmyTelecom.yaml`，已有的开关页面地址保持不变；尚无来源记录时以未设置开关页面新建记录。
- **查看机场状态（5）**：只显示公开摘要——开关页面与订阅链接是否已配置、两个来源主机名、总量/已用/剩余、到期时间、最近成功时间与 `AmyTelecom.yaml` 是否存在；URL 路径、查询串、令牌与任务编号永不显示。

地址会显示在当前终端，但不要把它作为 shell 参数、写入历史、仓库、重定向文件或工单。成功后可执行 `clash-sub status` 查看整体状态。

服务器还会通过 `clash-sub-airport-refresh.timer` 北京时间每天 06:00（随机延迟 0–2 小时，正常运行时约 06:00–08:00 执行；错过的周期补跑）自动执行一次与菜单 2 相同的刷新：共用同一把操作锁，锁被占用时按本次跳过处理；未设置开关页面时直接跳过；不做密集重试，也不执行 sync、不读取 3x-ui、不生成主配置、不调用 Mihomo、不重载 Nginx。

三层刷新周期各管一段：服务器每天北京时间 06:00 开始更新 `AmyTelecom.yaml`（随机延迟 0–2 小时）；owner 配置里 `AmyTelecom` provider 的 `interval: 86400` 让客户端每 24 小时从自有订阅服务器拉一次——服务器更新后最多约 24 小时内客户端取得新内容；主配置响应头 `Profile-Update-Interval: 24` 保持 24 小时不变。日常刷新只访问自有订阅服务器，不增加机场上游的访问频率。

下载与发布规则：按 HTTPS 下载原始字节（保留 HTTPS、最多三次 HTTPS 重定向、超时与 5 MB 大小限制），拒绝空响应与明显 HTML 错误页（`text/html`/`application/xhtml+xml` 响应类型，或去除 BOM 与空白后以 `<!DOCTYPE html`、`<html`、`<head`、`<body` 开头的正文），其余非空响应原字节、原注释写入随机临时文件，经日志式事务与来源记录同时原子替换 `/var/lib/clash-sub/public/provider/AmyTelecom.yaml`（权限 `0640 root:www-data` 不变）。来源记录 `/var/lib/clash-sub/private/airport-source.json` 同时保存订阅开关页面地址、订阅链接与最近一次下载的流量，固定 0600、root:root、非软链且硬链数为 1；崩溃中断的事务在下一次机场操作时恢复到完整的旧状态或完整的新状态。服务器不做 YAML 解析、不校验机场内容结构——上游来的原始字节、注释与顺序原样保留；坏的上游内容需要通过菜单 2 或菜单 4 覆盖。失败时保留当前 provider 与来源记录，只输出隐藏了源 URL、令牌、UUID 和节点敏感字段的稳定错误码。

上游响应里唯一被读取的响应头是 `Subscription-Userinfo`：机场带此头时，流量数字随本次下载保存；无此头时本次保存的流量明确置空，并提示「机场订阅已更新，未获取到流量信息」，正文照常发布。客户端之后请求 `AmyTelecom.yaml` 时看到的就是这份最近保存的流量与到期，服务器不会为此联系机场上游。

机场字节变化本身不会创建新发布：发布输入哈希只含 3x-ui 数据。需要把新机场节点刷进客户端时，在 Clash Verge 中手动刷新 provider（见下节），或在内容需要重排时执行 `clash-sub sync`。

## 手动刷新 provider

owner 客户端在 Clash Verge 的「订阅」页选中 `Clash-Compat.yaml` 或 `Clash-Balance.yaml`，对 `AmyTelecom` provider 执行手动更新即可拉取最新机场节点；主配置本身无需重新下载。provider 的自动刷新间隔为 24 小时。Clash Verge 里显示的机场流量与到期是服务器最近一次机场下载保存的数字，刷新 provider 不会触发服务器访问机场上游。

## 订阅流量元数据

订阅流量头（`Subscription-Userinfo`）按请求按需生成，不预写进任何配置文件，也没有定时刷新任务。链路如下：

1. 客户端请求订阅 URL，命中 Nginx 公网 location；守卫不变：非 GET/HEAD 返回 405、带查询串返回 400、超频返回 429。
2. Nginx 把请求转发到 `/run/clash-sub/metadata.sock`：不透传任何请求头，连接与读取超时各 1 秒。该 socket（0660 root:www-data）由 `clash-sub-metadata.socket` 监听，父目录 `/run/clash-sub`（0750 root:www-data）由 `/etc/tmpfiles.d/clash-sub-metadata.conf` 开机建立；首个请求到达时激活 `clash-sub-metadata.service`（已按 systemd 加固清单限制文件系统与网络访问，只读 3x-ui 数据库、只写私密运行时目录）。
3. 元数据服务只认识两类目标——某用户的 profile 文件与机场 `AmyTelecom.yaml`——其余一律返回同一固定 404。命中时返回 `Subscription-Userinfo` 流量头与 `X-Accel-Redirect` 内部跳转；跳转 URI 上用查询参数 `u/d/t/e` 携带四个纯数字流量值（Nginx 在内部跳转后读取不到上游头变量，数字只能随跳转一起传递），无正文。取不到流量时只返回不带参数的跳转，不报错。
4. Nginx 在内部位置（`/accel/…`，公网直访 404）发送文件正文，并统一附加 Profile-Title、Content-Disposition、`Profile-Update-Interval: 24`（仅用户 profile）、nosniff 与 no-store；正常路径与降级路径的正文和这些头完全一致。流量头也在这里按需拼出：跳转带 `u` 参数（纯数字校验）时按四个查询参数重组 `Subscription-Userinfo`，降级跳转与无流量跳转不带参数，因此不输出该头。

3x-ui 流量来自五分钟缓存：缓存过期后的第一个请求读取一次数据库快照（一次覆盖全部用户），随后 300 秒内所有用户、Compat 与 Balance 共用；并发请求只有一个执行刷新，其余等待后共享同一结果。数据库读取失败时回退旧缓存，没有旧缓存则该次响应不带流量头。缓存文件 `/var/lib/clash-sub/private/traffic-cache.json`（0600、原子更新、只含数字）持久化最近一次快照，重启后未过期可直接复用。机场流量不走这个缓存：直接读来源记录里最近一次机场下载保存的数字。流量刷新不生成发布、不改主 YAML、不重载 Nginx。

故障降级：元数据服务未启动、超时或返回 5xx 时，Nginx 用 `error_page` 在同一内部位置发送同一文件，状态码 200、正文一致，仅缺少流量头。订阅在降级下依然可用，Clash Verge 只是暂时看不到用量。

健康检查：

```bash
systemctl status clash-sub-metadata.socket
journalctl -u clash-sub-metadata.service -n 100 --no-pager
```

`clash-sub metadata-serve` 是服务单元的内部入口，不出现在交互菜单，日常无需手工调用。

## 同步、状态与链接

模板或 3x-ui 数据有效变更后发布：

```bash
clash-sub sync
clash-sub status
clash-sub links
```

`sync` 要求当前机场 provider 文件存在、权限安全且非空（内容本身不解析、不校验），然后恢复中断的运行时发布、准备并验证候选、最后激活；机场 provider 缺失或不合规时整体拒绝（`airport_provider_required`）。若输出“同步部分完成”，按其中公开的客户端 ID 与错误代码处理，且以 `status` 的最近错误和 pending 项为准。`links` 输出可用订阅链接：owner 三条完整配置与“机场镜像链接”（即 `Clash-Compat.yaml` 中 `AmyTelecom` provider 的 URL），普通用户一条；屏幕录制或终端转存前先避免泄露。

升级后先执行一次 `sync` 生成 owner 的第三份配置。升级前只有两份 owner 配置的历史发布仍可读取，但不符合新的完整发布矩阵，不能作为 owner 的回滚目标；需要恢复时重新执行 `sync` 生成三份配置。

日常代码维护使用：

```bash
clash-sub update
clash-sub sync
```

`update` 先创建更新前快照，执行 fast-forward 拉取和依赖同步，再由新代码完成 systemd 加固与 Nginx 重渲染；更新后仍需重新发布。

### 邮箱后缀订阅链接

公开链接统一为 `https://订阅域名/s/<22字符随机部分>-<client-email>/<文件名>`。邮箱（也可以是用户名）保留大小写，允许英文字母、数字及 `. _ @ + -`，长度 1–254；空格、中文、斜杠、百分号与控制字符会被拒绝。完整链接是访问凭据，邮箱后缀用于核对收件人，不能代替随机部分；分享时也会暴露该邮箱。

升级代码后执行 `clash-sub sync`：旧版状态在第一次成功同步时整体切换为新版，所有旧链接随即失效，需要重新分发。迁移中任一用户准备失败或发布失败，旧状态与路由保留，修复后重新同步即可；迁移前仍可查看已发布链接。

在 3x-ui 修改 email 后，下次成功同步会保留随机部分、替换邮箱后缀；该用户旧链接失效。同步前 `links` 标注“待同步”，只输出已发布的旧地址。日常同步某个用户失败时，该用户继续使用旧地址。`rotate-link` 会更换随机部分并保留已发布邮箱；旧版状态须先同步迁移。

owner 的机场专用配置使用另一枚 22 字符派生令牌，后缀也是邮箱；机场镜像 `AmyTelecom.yaml` 使用 owner 主令牌。owner 改名、迁移或轮换后，包含旧 provider URL 的历史配置不能直接回滚，需重新同步生成。3x-ui 的本地 Clash 订阅仍是自建节点来源，不能因此关闭。

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

owner 轮换会以新令牌 URL 重新生成三份主配置；provider 文件本身不移动，机场上游不会被再次联系。普通用户轮换只改路由，不触碰 provider。

## Home 脚本维护

Home 节点、`HomeServer` 与 `ProxyServer` 分组只存在于本机 `private/clash-verge-home.js`。修改 Home 时直接编辑脚本，随后：

```bash
node --check private/clash-verge-home.js
```

语法通过后在 Clash Verge 中重新载入配置验证效果。脚本只对 `Clash-Compat.yaml` 和 `Clash-Balance.yaml` 两个标题生效；其他 profile 原样返回。不要把脚本内容或其中的节点值写入仓库、文档或终端记录。

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

订阅流量元数据由 `clash-sub-metadata.socket` 按需激活提供；链路、缓存与降级行为见[订阅流量元数据](#订阅流量元数据)。

## 故障检查顺序

按以下顺序停止；前一步异常时先修复，不要直接重装或删除发布目录。

1. 查看发布与健康摘要：

   ```bash
   clash-sub status
   clash-sub links
   ```

   记录公开的错误代码、pending 项与受影响用户 ID，不记录订阅链接或私密值。

2. 检查 Nginx、x-ui 与流量元数据 Socket：

   ```bash
   nginx -t
   systemctl status nginx x-ui clash-sub-metadata.socket
   journalctl -u nginx -u x-ui -u clash-sub-metadata.service -n 100 --no-pager
   ```

   订阅能下载但没有流量头属于元数据链路降级（服务失败、超时或 5xx）：文件服务不受影响，按上述输出定位 socket 与服务，必要时检查 x-ui 数据库可读性；数据库读取失败时服务会自动回退旧缓存。

3. 若发布过程中断、启动恢复未完成，先运行：

   ```bash
   clash-sub recover
   nginx -t
   clash-sub status
   ```

   `clash-sub-recover.service` 也会在启动时于 Nginx 之前处理这类激活日志；手动恢复成功后再决定是否重试 `sync`。

4. `airport_provider_required` 表示稳定 provider 缺失或无效：按[机场更新](#机场更新)刷新或重新导入订阅，再 `sync`。机场更新失败会保留原 provider 与来源记录，并给出不含订阅地址的分类错误码：

   - `airport_url_invalid` / `airport_activation_url_invalid`：订阅链接或开关页面不是允许的 HTTPS 地址；
   - `airport_activation_missing`：尚未设置订阅开关页面（自动刷新先跳过，交互操作提示使用菜单 1）；
   - `airport_portal_unavailable`：开关页面临时不可用（网络、超时、重定向越界）；
   - `airport_portal_unsupported`：开关页面结构不兼容（页面改版；自动路径同时旧链接失败时报此码，改用菜单 3 或 4）；
   - `airport_link_generation_failed`：订阅链接生成接口失败或返回非法结果；
   - `airport_redirect_invalid`：下载重定向超过三次或转向非 HTTPS 地址；
   - `airport_download_failed`：网络、TLS 证书、上游响应失败，或响应为空；
   - `airport_document_too_large`：返回内容超过传输大小限制；
   - `airport_response_invalid`：响应为明显 HTML 错误页（HTML 响应类型或正文以 HTML 标签开头）；
   - `airport_provider_invalid`：provider 目录或文件的安全属性不合规（属主、组、类型、权限、硬链数，或目录模式不是 `02750`），或待发布字节为空/超限；
   - `airport_provider_write_failed`：服务器无法原子写入 provider 文件。

   不要反复粘贴地址或把地址发送到日志；只记录上述错误码。

5. 本地模板变更后失败时，回到开发 Mac 运行模板同步与两种密钥扫描；不要从服务器或日志取回私密内容。

6. 当前发布有效但内容不合预期时，使用[历史与回退](#历史与回退)的用户级回退；仅整合安装本身需要撤销时，按[部署清单](../DEPLOYMENT.md#升级与卸载)执行 `clash-sub rollback --install`。

## 备份

变更前、更新前和迁移前都创建备份：

```bash
clash-sub backup
```

备份写入仓库 `backups/`，文件权限为 `0600`。归档只包含五个重建必需文件：3x-ui 数据库、两份 Nginx 配置、运行时 `state.json` 与机场来源记录 `airport-source.json`；证书、机场 provider 文件、流量缓存、发布历史、运行状态与 systemd 文件不进入备份。任何必需文件缺失时备份直接失败。归档含私密数据：立即复制到受保护的离线或加密位置，不上传到仓库、公开网盘、工单或聊天记录。

备份创建后验证文件存在且权限正确；不要解包到仓库或把内容贴进记录：

```bash
ls -l backups/clash-sub-backup-*.tar.gz
```

## 全新服务器恢复

恢复以新服务器为目标，保留旧服务器直到验收完成。顺序如下：

1. 从受保护备份恢复 3x-ui 数据库与入站/client 配置；按[部署清单](../DEPLOYMENT.md#3x-ui-关键配置)检查面板、Reality 入站和端口。
2. 恢复备份中的 `state.json` 与 `airport-source.json` 到新服务器的私密运行时目录。
3. 克隆仓库到 `/opt/my-clash-config`，按[部署清单](../DEPLOYMENT.md#全新安装)执行 `bash install.sh`；安装时仅在隐藏提示输入新主机所需的域名、Cloudflare token 与 owner email。证书由安装流程重新签发。
4. 执行 `clash-sub`，主菜单 `1` 进入机场订阅后选择 `2`（自动开启订阅并刷新）：用恢复的开关页面与已保存链接重建 `AmyTelecom.yaml`，无需重新输入地址；页面自动化失效时先用浏览器手动开启订阅再选 `3` 刷新；真实链接也已失效时用 `4` 输入新链接。然后发布并验收：

   ```bash
   clash-sub sync
   nginx -t
   systemctl status nginx x-ui clash-sub-metadata.socket
   clash-sub status
   clash-sub links
   ```

5. 核对并恢复两份 Nginx 配置（stream 与 HTTP 的 `clash-sub.conf`）。
6. 验证 owner 与普通用户的授权范围、`status` 最近错误为空、链接可访问后，才停止旧服务器。保留原订阅链接要求 x-ui client ID 与数据库一致，且不重新初始化 owner、不轮换令牌。

## 域名或 VPS 迁移

既有安装记录不允许用 `clash-sub cert --domain` 或再次安装直接改域名；新域名或新 VPS 一律按[全新服务器恢复](#全新服务器恢复)建立新环境。

1. 在旧服务器执行 `clash-sub backup`，并保留旧服务继续运行。
2. 在新主机完成恢复和验收；新域名的 DNS、证书和 Nginx 由安装流程处理。
3. 用新域名的 `clash-sub links` 验证每个预期链接，并确认 Nginx、x-ui 与流量元数据 Socket 正常。
4. 仅在新发布物和客户端实际连通后切换 DNS/客户端；保留旧主机作为回退点。
5. 稳定观察后再人工撤销旧主机的公开入口和访问凭据。不要在切换前执行安装回滚或删除旧备份。

## 人工接管

需要永久停止脚本化维护时，先创建并离线保存完整备份，记录公开的版本号、服务状态和待办事项；不要记录订阅链接、token 或机场地址。将日常变更切换为受控的人工操作前，先冻结 `clash-sub update`、`sync`、`rotate-link` 等写入操作，避免两套流程同时发布。

若仅撤销本项目的整合安装，使用 `clash-sub rollback --install`，并按部署文档恢复 Reality 入站公网 listen。该回滚保留运行时目录、3x-ui 数据库和已签发证书；接管人必须决定这些保留数据的后续备份、权限与删除策略。完成交接前，不删除旧服务器、备份或安装记录。


## 订阅网页与面板开关

本功能与已有订阅下载分开运行。网页使用 `/run/clash-sub/web.sock`，订阅元数据继续使用原有 socket，网页异常不会改写订阅发布物。

### 初始化

服务器更新后，运行 `clash-sub update` 安装新增 systemd 单元。已获准部署后再运行：

```sh
clash-sub web setup
```

交互设置 12—256 字符的管理员密码，不通过命令行参数传密码。初始化会启用网页 socket，并关闭 3x-ui 的公网转发；保持 x-ui 服务与原始订阅运行。已有公网面板在普通代码更新时保持原状态。

初始化后终端输出管理员入口和各人的独立页面链接。owner 是唯一管理员，入口为 `https://<订阅域名>/admin/<独立入口令牌>-<owner的email>/`；固定 `/admin/` 和 `/admin/login` 返回 404。只有密码哈希被保存在私有运行时文件中，不保存明文密码。

### 页面与权限

- 管理员：以已部署 owner 身份，通过独立随机入口和密码登录；可查看成员列表、详情、服务器概况及机场专用页面，并执行下述固定管理操作。owner 的个人网页或订阅令牌不授予管理员权限。
- 个人：`/p/<独立随机页面令牌>-<email>/`，只能看到本人订阅及用量。email 为 3x-ui 用户标识，后端同时核对该标识与令牌归属。
- 机场专用：`/p/<独立机场页面令牌>-<owner的email>/`，只返回 `Clash-Airport.yaml` 的派生订阅令牌，不返回 owner 原令牌、自建节点或服务器概况。
- 旧的 `/p/<页面令牌>/` 继续兼容；新增可读后缀不更换令牌，也不改变现有订阅地址。更换页面令牌后，新旧两种路径都不能再用旧令牌访问。
- 页面令牌不能下载订阅，订阅令牌不能访问网页；保留现有 `/s/` 下载地址。

页面链接持有者可以获取对应订阅，仍需保密。URL 遮挡只避免旁人直接看到，不属于加密。网页关闭访问日志、禁用缓存，Referrer-Policy 使用 strict-origin，仅发送站点来源，不发送带令牌的路径；这样也能保留原生登录表单的 Origin 校验。脚本与样式使用同源资源。

管理员会话有效期八小时；Cookie 为 Secure、HttpOnly、SameSite=Strict。修改密码或重启网页服务会使原会话失效。登录采用全局五分钟最多五次失败的限制，因为现有 stream 层不保留真实客户端 IP。该限制可能导致所有人暂时无法登录；必要时走 SSH 管理。

### 终端命令

```sh
clash-sub web status
clash-sub web links
clash-sub web rotate-page 2
clash-sub web rotate-page airport
clash-sub web rotate-page admin
clash-sub web reset-password
clash-sub web panel-off
clash-sub web panel-on
clash-sub web tunnel
```

`rotate-page` 的数字为真实 3x-ui client ID；只更换页面链接，不改变已导入客户端的订阅地址。`links` 会为新用户补发页面令牌。订阅令牌轮换会保留独立网页地址，并重新绑定到新订阅；若保存绑定失败则旧页面拒绝访问，需要从终端重新获取。owner 重建会使旧管理入口与会话失效，之后运行 `web links` 获取新入口。

面板开关只控制公网 Nginx 代理及其 favicon 转发，内网面板保留供 SSH 隧道使用。开关状态保存在 `private-root/web-settings.json`，配置生成时读取；Nginx 验证或加载失败会恢复之前的文件与保存状态。不要手工编辑生成的 Nginx 文件。

### 数据来源与限制

- 用户用量：现有 3x-ui 统计与五分钟缓存；显示数据更新时间，过期缓存明确标注。
- Amy：最近一次成功更新机场订阅时保存的流量信息；不会因访问网页额外刷新机场。网页不显示到期时间；流量头不包含重置日期，按 owner 确认的规则显示「每月 19 日」；这是展示规则，不会触发或修改机场重置。
- 自建流量：只读 3x-ui 入站的 `up` / `down` 当前累计，包含停用入站，五分钟缓存；每月重置由 3x-ui 负责，网页不另建累计或重置任务。所有入站均配置有效总额度时才汇总额度，否则显示总额度未设置。该数据不是搬瓦工账单统计；读取失败显示暂无数据。
- 流量重置时间：只读 3x-ui 入站与客户端各自的 `traffic_reset` / `traffic_reset_day`，五分钟缓存；字段缺失显示「待确认」，未启用显示「未设置自动重置」。月末日期按 3x-ui 规则处理，时间以 3x-ui 时区为准；不把账号自动续期间隔当作月重置日。
- CPU、内存、磁盘、运行时间：仅管理员总览显示，个人及机场页面均不展示。沿用预览的百分比、进度条、负载、容量明细与运行时间布局，数据来自 Linux 主机统计。CPU 与网速按两次访问间的差值采样，首次访问可能无数据；不是服务商计费流量，也不是外部连通性结果。
- 网速统计排除常见容器和隧道接口以减少重复计数；复杂自定义网络需另行核实统计口径。

管理员不显示用户用量汇总。各成员配额与服务商额度各自独立。网页只提供链接轮换、机场刷新、更新代码并生成配置；不提供修改用户、重置额度、开放公网面板或任意命令执行。

### 保存和恢复

`web-settings.json` 包含页面能力令牌、管理员密码哈希、面板公网开关及网页启用状态，权限必须为 0600、由服务运行用户所有。启用网页后，项目完整备份会额外包含该文件；恢复时放回配置的 private-root，保持权限。备份本身含访问凭据，须保密。

管理员服务单元 `clash-sub-web.service` 使用只读系统、独立网络命名空间、仅 Unix socket；不允许网页调用外部网络。更新流程会重启已运行的网页服务，使新代码生效。

### 按用户管理链接与网页操作

终端主菜单 `3` 先列出用户（owner 有标识），选择用户 ID 后显示该用户的网页与全部已发布订阅。选择 owner 时额外显示管理入口和机场专用网页。命令行 `clash-sub links` 仍列出全部订阅，`clash-sub web links` 列出网页。

主菜单 `9` 中「更换用户网页链接」与「轮换用户订阅链接」分开；owner 网页可选择个人、机场或管理员入口。主菜单 `4` 提供初始化、访问状态、开关公网面板、SSH 隧道说明、重置网页管理员密码和设置 3x-ui API 令牌，操作后只显示简短提示，回车展开完整菜单。

管理员网页支持：

- 查看与复制指定用户的链接，更换个人网页或订阅链接。订阅轮换会使旧订阅地址失效，需要用户重新导入；个人网页保留。owner 订阅轮换后管理会话失效，需要用原管理入口重新登录。
- 刷新 Amy：复用已保存来源的刷新流程，不更换现有项目订阅地址。
- 更新代码并生成配置：先执行现有代码更新流程，成功后由新代码进程生成全部用户配置。更新失败时停止，不继续生成。生成失败会保留失败记录，请到终端检查；不会自动重试或伪报成功。

修改操作必须经过确认、会话和同源 CSRF 校验。网页请求只写入固定操作队列并启动 `clash-sub-task@<系统生成ID>.service`，不接受命令、脚本、文件路径或自定义参数。后台任务在已有操作锁内再次校验 owner，防止排队后权限已撤销；同时只允许一个网页任务。请求 ID 防止双击或重发造成重复轮换，记录最多保留三十条，不含订阅链接、入口令牌、密码或命令输出。

后台任务独立于网页服务，因此网页重启或标签页关闭不会取消已确认任务。执行记录保存在 private-root 的 `web-tasks.json`（0600）。任务遇到错误不自动重试；服务器重启或执行超时后显示中断，先检查结果，再决定是否重新执行。记录不属于重建必需备份。

新安装、首次启用旧版本或迁移 owner 时，在终端运行 `clash-sub web links` 获取随机管理入口；升级时会自动补齐已启用网页的入口信息。随机入口保存在现有 `web-settings.json`，更新和重启保持不变；可用 `clash-sub web rotate-page admin` 单独更换，更换后旧入口和会话立即失效。

个人页面、机场页面仍为只读。手机端使用紧凑成员列表，保留姓名、在线、用量和操作，重置规则与账号细节通过详情查看；其他模块同步压缩留白。电脑端保持完整表格。

个人及机场用量卡统一采用长条进度展示；管理员自建和 Amy 使用相同布局，剩余流量放在进度条下方右侧。没有额度时不显示百分比。成员列表在手机端保留额度进度条；「查看详情」直接打开该用户的独立个人网页，页面未启用则不生成链接。客户端下载页面为公开的固定内容，仅提供官方客户端下载地址，不包含用户令牌或管理操作；公开路径仍可能被扫描或大量请求，路径保密不能替代访问控制与请求限制。

### 管理员在线状态

成员列表的顺序为：用户、在线状态、用量、流量重置时间、账号状态。绿色圆点表示在线，灰色圆点表示离线，短横线表示未知；个人及机场页面不提供其他用户在线信息。

数据直接来自当前 3x-ui v3.11.0 的 `POST <面板路径>/panel/api/clients/onlines`，不使用流量差值或数据库 `last_online` 推测。该接口读取 3x-ui 的运行时在线列表；数据库中的 API 令牌为哈希，不能直接取回用于请求的明文。

在服务器执行 `clash-sub`，进入主菜单 **4 → 7「设置 3x-ui API 令牌」**，粘贴已有令牌（隐藏输入，直接回车取消）。也可执行：

```sh
clash-sub web set-api-token
```

不接受令牌命令行参数，无法隐藏终端输入时拒绝输入。菜单将令牌原子保存到 `/etc/clash-sub/credentials.json`，目录权限 0700、文件权限 0600、root 所有，随后重新加载在线查询服务并验证本机查询。失败时明确提示已保存但查询未成功；重新选择此项可替换令牌。不回显令牌或接口响应内容。

令牌需要允许 `POST <面板路径>/panel/api/clients/onlines`；本版本仅 `monitor` 范围不允许此接口。菜单不创建或扩大 3x-ui 令牌权限。令牌不写入项目配置、网页或 Git，不需要创建 `.claude` 目录。此前手动写入旧路径的安装请重新通过菜单设置；程序不会删除原凭据文件。

`clash-sub-online.service` 通过只读绑定取得该项目凭据文件，只连接本机回环地址，使用 root:root 0600 的 Unix socket。网页服务保持网络隔离，不读取 API 凭据。升级先执行 `clash-sub update`，以安装使用新路径的 systemd 单元，再设置令牌。

本机查询服务使用十秒缓存合并请求。只有管理员总览打开时发起查询，页面每三十秒刷新在线状态；隐藏标签页停止定时器，返回前台立即刷新；无 API 凭据、权限不足、超时和非法响应都显示未知，不能当作离线。只有查询成功返回的在线列表中不存在该用户时，才显示离线。状态与 3x-ui 同源，仍可能有短暂缓存延迟。

新安装在网页初始化时启用查询 socket；已启用网页的升级由 `clash-sub update` 补装并启用查询 socket，不需重新设置网页管理员密码。状态接口 `<随机管理员入口>/onlines` 受管理员会话保护，返回用户 ID 与三态标识，不返回 API 令牌或原始在线邮箱列表。

### 本地样式预览

```sh
.venv/bin/python -m http.server 8765 --bind 127.0.0.1 --directory previews/account
```

- 管理员：`http://127.0.0.1:8765/admin.html`
- 个人：`http://127.0.0.1:8765/index.html`
- 机场：`http://127.0.0.1:8765/airport.html`

预览页中的 query 参数仅切换虚构数据，不是正式授权机制。不要把真实令牌写入预览文件。
