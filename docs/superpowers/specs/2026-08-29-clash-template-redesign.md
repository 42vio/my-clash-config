# Clash 模板体系重构设计

日期：2026-08-29

## 目标

以 ClashX iCloud 中生成的 `Compat-Office.yaml` 为公共基础，重新建立一个
可保留注释的三档订阅体系。项目按全新部署处理，不兼容旧模板、旧档位、旧
release 或旧订阅链接。

本次发布：

| 用户 | 订阅 | 机场 | 家庭 |
| --- | --- | --- | --- |
| owner | `compat-office` | 有 | 有 |
| owner | `compat-universal` | 有 | 无 |
| owner | `balance-office` | 有 | 有 |
| member | `compat-universal` | 无 | 无 |

`privacy` 本次不进入模板、代码、链接或测试，后续单独设计。

## 模板与私有数据

```text
templates/
├── base/
│   └── compat-office.yaml
├── dns/
│   └── balance-office.yaml
└── profiles.yaml

private/
└── home.yaml
```

- `templates/base/compat-office.yaml` 保存无凭据、无家庭内容的公共 Compat
  配置，并保留适用的注释、键顺序、锚点和别名。
- `templates/dns/balance-office.yaml` 保存 `Balance-Office.yaml` 的完整
  `dns` 段及其全部注释。生成 Balance 时整体替换 Compat 的 `dns`，不做
  递归差异合并。
- `templates/profiles.yaml` 只声明组合关系和公共节点注入组。角色授权仍由
  代码锁定，清单不能扩大机场或家庭数据权限。
- `private/home.yaml` 保存家庭节点、策略组、规则、注入关系和随对象保留的
  注释，不进入 Git。

旧 `templates/clash.yaml`、`templates/variants/manifest.yaml`、
`templates/variants/privacy-dns.yaml` 及清空后的 `templates/variants/`
全部删除。

## 模板更新命令

`clash-sub template-sync` 支持以下调用：

```bash
# 默认读取 ClashX iCloud 目录中的 Compat 与 Balance
./bin/clash-sub template-sync

# 只更新一个来源
./bin/clash-sub template-sync --compat-office /path/Compat-Office.yaml
./bin/clash-sub template-sync --balance-office /path/Balance-Office.yaml

# 同时显式指定两个来源
./bin/clash-sub template-sync \
  --compat-office /path/Compat-Office.yaml \
  --balance-office /path/Balance-Office.yaml
```

无参数时通过 `Path.home()` 读取：

```text
Library/Mobile Documents/iCloud~com~west2online~ClashX/Documents/
├── Compat-Office.yaml
└── Balance-Office.yaml
```

显式传入一个参数时只处理该来源，不隐式读取另一个默认文件：

- 只更新 Compat：更新公共基础与家庭覆盖层，保留现有完整 Balance DNS。
- 只更新 Balance：以仓库当前 Compat 基础为基准，只更新完整 Balance DNS。
- 同时更新：所有候选统一拆分、验证并原子写入。

输入文件只读，不复制进仓库。路径错误、iCloud 文件未下载、格式错误或任一
候选校验失败时，所有目标文件保持不变。

## 首次家庭范围初始化

当前仓库没有新的 `private/home.yaml`。本次实现使用现有 iCloud
`Compat-Office.yaml` 与 `Compat-Universal.yaml` 做一次性结构对照，提取
家庭节点、家庭策略组、家庭规则和注入关系，生成并验证首份
`private/home.yaml`。

该对照不是日常命令接口。以后 Universal 始终由 Compat Office 按
`private/home.yaml` 声明的范围删除家庭内容后派生。若新家庭结构超出现有
声明范围，更新失败，不允许家庭内容静默进入 Universal。

新 `private/home.yaml` 验证成功后，删除不再使用的本地旧私有文件：

```text
private/proxies.yaml
private/proxy-groups.yaml
private/rules.yaml
private/reference-configs/
```

## 注释与 YAML 往返

模板更新与运行时生成使用支持注释往返的 YAML 数据模型：

- Compat 公共注释随公共对象进入基础模板。
- Balance 的完整 `dns` 及其注释整体保存。
- 家庭对象附属注释随对象进入 `private/home.yaml`，只出现在 Office 输出。
- 不按内容主动删除或改写注释；若注释真的包含明确凭据，安全扫描使更新失败。
- 配置项被明确删除且没有对应输出对象时，其绑定注释随对象消失，不转挂到
  无关字段。
- 最终订阅保留适用注释、键顺序、锚点和别名。

## 生成链路

`clash_sub/template_sync.py` 读取 iCloud 或显式来源，拆分并验证
`compat-office.yaml`、完整 Balance DNS 与 `private/home.yaml`。

`clash_sub/generator.py` 按以下顺序组合：

1. 读取 Compat 公共基础。
2. Balance 输出整体替换为完整 Balance DNS。
3. 注入当前用户自己的 3x-ui 节点。
4. 仅给 owner 注入固定 `AmyTelecom` Provider。
5. 仅给 `compat-office` 和 `balance-office` 注入家庭覆盖层。
6. 用注释往返序列化器输出最终 YAML。

`clash_sub/service.py` 负责用户级生成与完整校验；
`clash_sub/release_store.py` 原子发布不可变 release；`clash_sub/nginx.py`
只为 release 中实际存在的文件生成订阅路由。

最终文件：

```text
owner release/
├── clash-compat-office.yaml
├── clash-compat-universal.yaml
├── clash-balance-office.yaml
└── AmyTelecom.yaml

member release/
└── clash-compat-universal.yaml
```

## 变更摘要

每次模板更新明确报告：

- Compat 公共 YAML 路径的增删改；
- Balance DNS 是否变化及变化路径；
- rules、proxy-groups 与注释的增删改数量；
- 家庭节点、策略组和规则的数量变化；
- 实际写入或保持不变的目标文件。

公共注释可显示文本。家庭与动态节点只显示结构和数量，不输出节点名、地址、
UUID、密码、机场 URL 或其他凭据。

## 校验与原子性

更新流程固定为读取、拆分候选、完整验证、原子替换四阶段：

- Balance 除 `dns` 外的公共结构必须与 Compat 一致。
- tracked 模板不得包含真实节点、Provider URL、家庭内容或凭据。
- owner 三个输出与 member 唯一输出都必须符合固定授权矩阵。
- Universal 不得包含任何家庭节点、策略组或规则。
- 本地 `template-sync` 必须通过结构、隔离、泄漏与合成校验，不要求开发机
  安装 Mihomo；服务器 `clash-sub sync` 发布前仍必须通过固定版本 Mihomo
  校验。
- 任一失败不会修改 tracked 模板或 `private/home.yaml`。

## 测试

自动测试覆盖：

- 注释、键顺序、锚点和别名往返；
- 完整 Balance DNS 替换；
- owner/member 输出矩阵与机场、家庭隔离；
- 默认 iCloud 路径、单来源更新和双来源原子更新；
- 首次家庭范围初始化与后续范围拒绝；
- 失败零写入及安全的变更摘要；
- 新 release 文件名、链接和 Nginx 路由；
- 最终 YAML 结构、敏感信息扫描和 Mihomo 校验；
- 旧档位名与旧模板路径从运行时代码和用户文档中消失。

## 文档与交付边界

精简并保留：

```text
README.md
DEPLOYMENT.md
docs/3x-ui-setup.md
docs/operations.md
docs/private-data.md
docs/recovery.md
```

删除 `docs/dns-design.md`、`docs/legacy-trojan-topology.md` 及旧的
`docs/superpowers/specs/`、`docs/superpowers/plans/` 文档，只保留本次设计与
实施计划。用户文档避免重复说明，同一概念只在一处解释，其他文档使用链接。

本次交付只完成仓库重构、本地初始化和验证，不连接、修改或重装服务器。

实现由 `gpt-5.6-luna`、`max` 推理强度的子代理执行；当前 Sol 主代理负责
计划审阅、过程把关、完整测试与最终代码审查，不把最终验收委托出去。
