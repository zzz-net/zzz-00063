# patchgate - 本地补丁发布闸门 CLI

一个全功能的本地补丁发布闸门工具。不只是"读清单 → 打印结果"的简单校验器，而是覆盖从**清单导入 → 规则校验 → 人工审批 → 标记发布 → 导出摘要**完整发布链路的状态机系统，并支持失败修复、按批次续跑、发布撤销回退、全流程历史追踪、结果持久化落盘。

所有状态、检查结果、审批记录、发布历史均保存在本地 SQLite 数据库中，重新执行工具时与上次退出时完全一致。

---

## 目录

- [安装与环境](#安装与环境)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [命令手册](#命令手册)
  - [import - 导入清单创建批次](#import---导入清单创建批次)
  - [check - 执行规则校验](#check---执行规则校验)
  - [approve - 审批通过](#approve---审批通过)
  - [reject - 驳回审批](#reject---驳回审批)
  - [publish - 标记发布](#publish---标记发布)
  - [revoke - 撤销发布 / 回退](#revoke---撤销发布--回退)
  - [export - 导出发布摘要](#export---导出发布摘要)
  - [resume - 按批次续跑](#resume---按批次续跑)
  - [history - 查看历史](#history---查看历史)
  - [status - 查看当前状态](#status---查看当前状态)
  - [list - 列出所有批次](#list---列出所有批次)
- [handover-export - 导出接手包](#handover-export---导出接手包)
- [handover-import - 导入接手包](#handover-import---导入接手包)
- [handover-list - 查看导入记录](#handover-list---查看导入记录)
- [状态机与流转规则](#状态机与流转规则)
- [清单格式](#清单格式)
- [规则配置](#规则配置)
- [完整流程演示](#完整流程演示)
  - [演示一：正常发布全链路](#演示一正常发布全链路)
  - [演示二：失败链路（预检拒绝 + check 阶段错误 + 驳回）](#演示二失败链路预检拒绝--check-阶段错误--审批阻塞--驳回)
  - [演示三：发布 → 撤销回退 → 重新发布](#演示三发布--撤销回退--重新发布)
  - [演示四：断点续跑（一键到发布）](#演示四断点续跑一键到发布)
  - [演示五：接手包导出导入全链路](#演示五接手包导出导入全链路)
- [数据持久化说明](#数据持久化说明)

---

## 安装与环境

```bash
# 1. 安装依赖
pip install -r requirements.txt

# 2. 运行（两种方式等价）
python patchgate_cli.py --help
# 或
python -m patchgate.cli --help
```

依赖：
- `PyYAML` - 解析 YAML 清单与规则配置
- `tabulate` - 终端表格输出

---

## 项目结构

```
.
├── patchgate/
│   ├── __init__.py
│   ├── cli.py          # CLI 主入口与所有命令实现
│   ├── models.py       # 状态枚举与状态机
│   ├── rules.py        # 规则引擎 + 6 条内置规则
│   ├── manifest.py     # 清单解析（JSON/YAML/CSV）
│   └── storage.py      # SQLite 持久化层
├── config/
│   └── rules.yaml      # 规则配置文件（可自定义）
├── examples/
│   ├── manifest_good.json                 # 正常清单示例
│   ├── manifest_with_errors.json          # import 预检样本：含重复包名，import 当场拒绝 (rc=2, 零写入)
│   ├── manifest_check_stage_errors.json   # check 阶段失败样本：过预检但缺 checksum，check 阶段阻塞审批
│   └── manifest_example.yaml              # YAML 格式示例
├── patchgate_cli.py    # 快捷入口脚本
├── requirements.txt
└── README.md
```

首次运行会自动在当前工作目录创建 `.patchgate/patchgate.db` 数据库文件。

---

## 快速开始

三步完成一次发布闸门：

```bash
# 1. 导入清单
python patchgate_cli.py import examples/manifest_good.json --name "Q2补丁批次" --id "demo-001"

# 2. 规则校验
python patchgate_cli.py check demo-001

# 3. 审批 + 发布 + 导出
python patchgate_cli.py approve demo-001 --approver zhangsan --comment "代码评审通过"
python patchgate_cli.py publish demo-001 --operator lisi --comment "窗口发布完成"
python patchgate_cli.py export  demo-001 -o release_summary.json -f json
```

---

## 命令手册

所有命令共享以下全局参数：
- `--db <path>` - 指定数据库文件路径（默认 `.patchgate/patchgate.db`）
- `--no-color` - 禁用彩色输出

---

### import - 导入清单创建批次

**功能**：读取补丁清单，**先在内存中做预检**（包名必填、包名唯一性），只有通过预检才会在数据库中创建新批次（状态 `created`）。

**预检机制（关键行为）**：
- `package_name` 为空 → 当场报错并拒绝
- 包名重复 → 逐条指出重复位置并**拒绝导入**
- 预检失败时 **不创建批次、不写入任何数据库记录**、返回**退出码 2**

**用法**：
```bash
patchgate import <MANIFEST_PATH> [--name NAME] [--desc DESC] [--id BATCH_ID]
```

**参数**：
| 参数 | 必填 | 说明 |
|---|---|---|
| `MANIFEST_PATH` | ✅ | 清单文件路径，支持 `.json` / `.yaml` / `.yml` / `.csv` |
| `--name` / `-n` | ❌ | 批次名称（默认使用清单文件名） |
| `--desc` / `-d` | ❌ | 批次描述 |
| `--id` | ❌ | 自定义批次 ID（默认自动生成 `batch-YYYYMMDD-XXXXXX`） |

**示例**：
```bash
# 自动生成 ID
patchgate import examples/manifest_good.json --name "Q2安全补丁"

# 自定义 ID
patchgate import examples/manifest_good.json --id "release-2026-06-20-01"
```

**退出码**：
| 退出码 | 含义 |
|---|---|
| `0` | 导入成功，批次已创建 |
| `1` | 其他错误（文件不存在、解析失败、ID 重复等） |
| `2` | **预检失败**（包名重复或空包名，数据库零写入） |

---

### check - 执行规则校验

**功能**：对批次执行所有启用的规则检查。通过 → `check_passed`，存在 error 级失败 → `check_failed`（默认阻塞后续审批）。

**用法**：
```bash
patchgate check <BATCH_ID> [--rules RULES_YAML] [--continue-on-error]
```

**参数**：
| 参数 | 说明 |
|---|---|
| `--rules` / `-r` | 使用自定义规则配置文件（替代默认 `config/rules.yaml`） |
| `--continue-on-error` | 即使有 error 级失败，也不将状态置为阻塞（仍写入失败记录） |

**输出内容**：
- 启用规则列表
- 每项检查的通过/失败统计
- 所有失败项的表格（序号、包名、规则名、级别、详细信息）
- *注意：包名重复/空包名在 import 预检阶段就会被拒绝，不会进入 check 阶段*

---

### approve - 审批通过

**功能**：人工审批通过批次，状态从 `check_passed` → `approved`。

**用法**：
```bash
patchgate approve <BATCH_ID> --approver NAME [--comment COMMENT] [--force]
```

**参数**：
| 参数 | 必填 | 说明 |
|---|---|---|
| `--approver` / `-a` | ✅ | 审批人姓名/账号（会落盘记录） |
| `--comment` / `-c` | ❌ | 审批备注 |
| `--force` | ❌ | **不推荐**：即使批次在 `check_failed` 状态或仍有未解决失败项，也强制通过 |

**拦截逻辑**：
1. 当前状态必须是 `check_passed` / `check_failed` / `rejected`
2. **仍有未解决 error 级失败项时，默认拒绝审批**，必须 `--force` 才能绕过
3. 审批人、决定、备注、时间戳均落盘

---

### reject - 驳回审批

**功能**：驳回审批，批次状态 → `rejected`。**必须提供驳回原因**。

**用法**：
```bash
patchgate reject <BATCH_ID> --approver NAME --comment REASON
```

**注意**：
- `--comment` 必填（驳回必须有原因）
- 驳回后可修改清单文件重新执行 `check`，自动从 `rejected` → `checking`

---

### publish - 标记发布

**功能**：将审批通过的批次标记为已发布，状态 → `published`。

**用法**：
```bash
patchgate publish <BATCH_ID> --operator NAME [--comment COMMENT]
```

**参数**：
| 参数 | 必填 | 说明 |
|---|---|---|
| `--operator` / `-o` | ✅ | 发布操作人姓名（落盘记录） |
| `--comment` / `-c` | ❌ | 发布备注（如"窗口发布 02:00-02:30 完成"） |

**限制**：只能从 `approved` 状态流转，其他状态一律拒绝。

---

### revoke - 撤销发布 / 回退

**功能**：撤销已发布的批次。状态从 `published` → `revoked` → **自动回退到最近一次审批通过状态 `approved`**。**必须提供撤销原因**。

**用法**：
```bash
patchgate revoke <BATCH_ID> --operator NAME --comment REASON
```

**典型场景**：发布后发现问题，需要回滚批次闸门状态，待修复后再次发布。回退会保留：
- 原始审批人、审批备注
- 撤销原因、撤销操作人、时间
- 所有历史校验结果

---

### export - 导出发布摘要

**功能**：将批次的完整信息导出为结构化报告文件。

**用法**：
```bash
patchgate export <BATCH_ID> [-o OUTPUT] [-f json|yaml|markdown]
```

**参数**：
| 参数 | 说明 |
|---|---|
| `--output` / `-o` | 输出文件路径（省略则打印到 stdout） |
| `--format` / `-f` | 格式：`json`(默认) / `yaml` / `markdown` |

**导出内容**：
- 批次基本信息（ID、名称、清单哈希、状态…）
- 完整清单内容表
- 校验摘要 + 所有失败项明细
- 审批记录（审批人、决定、备注、时间）
- 发布与撤销历史
- 完整状态流转轨迹

---

### resume - 按批次续跑

**功能**：从批次当前状态自动推进到指定阶段。适合在修复问题后一键推进到目标状态。

**用法**：
```bash
patchgate resume <BATCH_ID> [--to check|approve|publish] [--rules ...] [--approver ...] [--operator ...]
```

**参数**：
| 参数 | 说明 |
|---|---|
| `--to` | 目标阶段：`check`(默认) / `approve` / `publish` |
| `--approver` / `-a` | `--to approve/publish` 时必填，用于自动审批 |
| `--operator` / `-o` | `--to publish` 时必填，用于自动发布 |
| `--rules` / `-r` | 自定义规则配置 |

**自动流转逻辑**：
| 当前状态 | `--to check` | `--to approve` | `--to publish` |
|---|---|---|---|
| `created` | check → 完成 | check → approve → 完成 | check → approve → publish → 完成 |
| `check_failed` | check → 完成（仍失败则中断） | check 仍失败 → 中断 | check 仍失败 → 中断 |
| `rejected` | check → 完成 | check → approve → 完成 | check → approve → publish → 完成 |
| `check_passed` | 直接结束 | approve → 完成 | approve → publish → 完成 |
| `approved` | 直接结束 | 直接结束 | publish → 完成 |
| `published` | 直接结束 | 直接结束 | 直接结束 |
| `revoked` | check → 完成 | check → approve → 完成 | check → approve → publish → 完成 |

任一阶段失败则立刻中断并给出明确原因。

---

### history - 查看历史

**功能**：查看批次的完整历史记录（状态流转、审批、发布/回退）或全局批次列表。

**用法**：
```bash
patchgate history [BATCH_ID] [-t all|status|approval|publish]
```

**示例**：
```bash
# 查看所有批次（BATCH_ID 省略）
patchgate history

# 查看某个批次的全部历史
patchgate history demo-001

# 只看审批记录
patchgate history demo-001 --type approval

# 只看发布/回退记录
patchgate history demo-001 -t publish
```

---

### status - 查看当前状态

**功能**：查看单个批次的当前状态摘要（基本信息 + 校验结果摘要 + 审批记录 + 发布记录）。

**用法**：
```bash
patchgate status <BATCH_ID>
```

---

### list - 列出所有批次

**功能**：以表格形式列出所有创建过的批次及其状态。

**用法**：
```bash
patchgate list
```

---

## 状态机与流转规则

```
                    ┌────────────┐
                    │  created   │
                    └─────┬──────┘
                          ▼
                    ┌────────────┐
             ┌─────▶│  checking  │◀────┐
             │      └─────┬──────┘     │
             │            │            │
        check_failed  check_passed  rejected
             │      ┌─────┴──────┐     │
             │      ▼            ▼     │
             │  ┌────────┐   ┌────────┐│
             └──│rejected│   │approved│┘
                └────────┘   └────┬───┘
                                 ▼
                           ┌──────────┐
                           │published │
                           └────┬─────┘
                                ▼
                           ┌──────────┐
                           │ revoked  │ ──▶ 自动回退到 approved
                           └──────────┘
```

| 当前状态 | 允许流转到 |
|---|---|
| `created` | `checking` |
| `checking` | `check_passed` / `check_failed` |
| `check_failed` | `checking` / `rejected` |
| `check_passed` | `approved` / `rejected` |
| `rejected` | `checking` |
| `approved` | `published` |
| `published` | `revoked` |
| `revoked` | `checking` / `approved` |

非法流转会被明确拒绝并返回错误信息。

---

## 清单格式

支持三种格式，自动按扩展名识别。

### JSON 格式（`.json`）
```json
{
  "batch_description": "可选的批次说明",
  "items": [
    {
      "package_name": "auth-service",
      "version": "2.4.1",
      "source_path": "./artifacts/auth-service-2.4.1.tar.gz",
      "checksum": "a1b2c3d4...",
      "custom_field": "任意自定义字段"
    }
  ]
}
```

### YAML 格式（`.yaml` / `.yml`）
```yaml
items:
  - package_name: "api-gateway"
    version: "7.8.2"
    source_path: "./dist/api-gateway-7.8.2.zip"
    checksum: "f6a7b8..."
```

### CSV 格式（`.csv`）
首行必须包含表头，支持 BOM：
```csv
package_name,version,source_path,checksum
auth-service,2.4.1,./artifacts/auth-2.4.1.tar.gz,a1b2c3d4
order-service,3.1.0,./artifacts/order-3.1.0.tar.gz,b2c3d4e5
```

### 标准字段
| 字段 | 说明 | 别名 |
|---|---|---|
| `package_name` | 包名（必填检查） | `name`, `pkg` |
| `version` | 版本号 | `ver` |
| `source_path` | 源包路径 | `path`, `src` |
| `checksum` | 校验和 | `sha256`, `md5` |

其余字段自动归入 `metadata`，原样保留在数据库中。

---

## 规则配置

默认规则文件：`config/rules.yaml`，可复制修改后用 `check --rules` / `resume --rules` 指定。

### 内置规则
| 规则 ID | 名称 | 范围 | 默认级别 | 说明 |
|---|---|---|---|---|
| `duplicate_package_name` | 包名唯一性检查 | batch | error | 同一批次包名不得重复（项级错误） |
| `package_name_required` | 包名必填检查 | item | error | 每个条目必须有非空包名 |
| `version_format` | 版本号格式检查 | item | warning | 应符合语义化 x.y.z |
| `checksum_required` | 校验和必填检查 | item | warning | 默认禁用 |
| `source_path_exists` | 源路径存在性检查 | item | warning | 检查本地路径 |
| `batch_size_limit` | 批次规模限制 | batch | error | 默认禁用，上限 100 |

### 规则配置示例
```yaml
rules:
  - id: duplicate_package_name
    name: "包名唯一性检查"
    enabled: true
    severity: error   # error / warning
    scope: batch      # item / batch

  - id: version_format
    name: "版本号格式检查"
    enabled: true
    severity: warning
    scope: item
    params:
      pattern: '^\d+\.\d+\.\d+$'   # 自定义正则
```

`severity: error` 的规则失败会**阻塞审批**，`warning` 级别仅告警不阻塞。

---

## 完整流程演示

以下演示全部使用真实命令和输入，你可以逐行复制运行。

### 演示一：正常发布全链路

**Step 1 - 导入清单**
```bash
$ python patchgate_cli.py import examples/manifest_good.json --name "Q2生产补丁" --id "demo-normal-01"
✓ 批次创建成功
  批次 ID   : demo-normal-01
  名称      : Q2生产补丁
  清单文件  : .../examples/manifest_good.json
  清单摘要  : 3e8c4f1a...
  条目数量  : 5
  当前状态  : created
下一步建议:
  patchgate check demo-normal-01
```

**Step 2 - 规则校验**
```bash
$ python patchgate_cli.py check demo-normal-01
▶ 开始校验批次 demo-normal-01 ...
  启用规则数: 4
    - duplicate_package_name: 包名唯一性检查 [ERROR]
    - package_name_required: 包名必填检查 [ERROR]
    - version_format: 版本号格式检查 [WARNING]
    - source_path_exists: 源路径存在性检查 [WARNING]

═════════════════════════════════════════════════
  校验摘要: 共 20 项检查
    通过 : 15   失败 : 5   警告 : 5   错误 : 0   跳过 : 0
═════════════════════════════════════════════════

✗ 失败项详情:
+-------+--------------+----------------+--------+----------------------------------+
|   序号 | 包名         | 规则           | 级别   | 详细信息                         |
+=======+==============+================+========+==================================+
|     1 | auth-service | 源路径存在性检查| WARNING| 源路径不存在: .../auth-2.4.1.tar.gz |
+-------+--------------+----------------+--------+----------------------------------+
| ... （其余4条警告：示例包源路径不存在） |

✓ 校验通过，状态已更新为: CHECK_PASSED
  下一步: patchgate approve demo-normal-01 --approver <姓名>
```

（因为示例中源路径是占位的，只有 warning 没有 error，所以通过）

**Step 3 - 审批通过**
```bash
$ python patchgate_cli.py approve demo-normal-01 --approver zhangsan --comment "所有变更均已代码评审，版本核对无误"
✓ 审批通过
  批次 ID   : demo-normal-01
  审批人     : zhangsan
  审批备注   : 所有变更均已代码评审，版本核对无误
  当前状态   : APPROVED
  下一步: patchgate publish demo-normal-01 --operator <姓名>
```

**Step 4 - 标记发布**
```bash
$ python patchgate_cli.py publish demo-normal-01 --operator lisi --comment "6/20 凌晨2:00-2:25 灰度+全量发布完成，监控正常"
✓ 批次已标记为发布
  批次 ID   : demo-normal-01
  发布人     : lisi
  发布时间   : 2026-06-18T10:30:00
  发布备注   : 6/20 凌晨2:00-2:25 灰度+全量发布完成，监控正常
  当前状态   : PUBLISHED
  导出发布摘要: patchgate export demo-normal-01 -o summary.json
```

**Step 5 - 导出 Markdown 摘要报告**
```bash
$ python patchgate_cli.py export demo-normal-01 -o release_report.md -f markdown
✓ 发布摘要已导出到: .../release_report.md
```

生成的报告包含所有信息（清单表格、校验失败明细、审批人、发布时间、完整状态轨迹）。

---

### 演示二：失败链路（预检拒绝 + check 阶段错误 + 审批阻塞 + 驳回）

失败链路分两层：**第一层是 import 当场拒绝（包名重复/空包名）**，根本不会进闸门；
**第二层是 import 通过但 check 阶段触发 error 级规则**，此时批次被锁，审批被阻塞。

---

#### Part 1：import 预检拒绝（当场、零写入、退出码 2）

`examples/manifest_with_errors.json` 中有 2 组重复包名 + 1 条空包名，**import 阶段当场拒绝，不创建批次，数据库零写入**。

```bash
$ python patchgate_cli.py --no-color import examples/manifest_with_errors.json \
    --id "demo-precheck-fail" --name "预检被拒批次"
[FAIL] 清单预检失败，拒绝导入
  共发现 5 项错误:

+------+---------------+--------+-------------------------------------------------+
| 行号   | 包名            | 错误类型   | 详细信息                                            |
+======+===============+========+=================================================+
| #6   | (空)           | 包名必填   | package_name 字段缺失或为空                            |
+------+---------------+--------+-------------------------------------------------+
| #1   | auth-service  | 包名重复   | 同时出现在第 [1, 4] 行 (共 2 处, 版本: ['2.4.1', '2.4.0']) |
+------+---------------+--------+-------------------------------------------------+
| #4   | auth-service  | 包名重复   | 同时出现在第 [1, 4] 行 (共 2 处, 版本: ['2.4.1', '2.4.0']) |
+------+---------------+--------+-------------------------------------------------+
| #2   | order-service | 包名重复   | 同时出现在第 [2, 5] 行 (共 2 处, 版本: ['3.1.0', '3.0.9']) |
+------+---------------+--------+-------------------------------------------------+
| #5   | order-service | 包名重复   | 同时出现在第 [2, 5] 行 (共 2 处, 版本: ['3.1.0', '3.0.9']) |
+------+---------------+--------+-------------------------------------------------+

修复清单后请重新执行 import 命令
# 退出码: 2
# 数据库: batches=0, manifest_items=0, check_results=0,
#          approvals=0, publish_records=0, status_history=0
```

> **验收口径**：预检失败的批次 ID 无论怎样也不会出现在 `list` / `status` 里，数据库里查不到任何与此 ID 相关的记录。

---

#### Part 2：import 通过 + check 阶段触发 error 级规则

改用 `examples/manifest_check_stage_errors.json` —— 包名不重复、无空包名（能通过 import 预检），但 3 个包缺 `checksum`，配了 `checksum_required: error` 规则就会在 check 阶段阻塞。

**Step 1 - import 通过（预检放行）**
```bash
$ python patchgate_cli.py --no-color import examples/manifest_check_stage_errors.json \
    --id "demo-check-fail-01" --name "check阶段错误测试"
[OK] 批次创建成功
  批次 ID   : demo-check-fail-01
  名称      : check阶段错误测试
  清单文件  : .../examples/manifest_check_stage_errors.json
  清单摘要  : ca3f249ecf4a9a84
  条目数量  : 4
  当前状态  : created

下一步建议:
  patchgate check demo-check-fail-01
# 退出码: 0
```

**Step 2 - check：触发 checksum error，状态置为 CHECK_FAILED**
```bash
$ python patchgate_cli.py --no-color check demo-check-fail-01 \
    --rules config/rules_test_checksum_error.yaml
[>] 开始校验批次 demo-check-fail-01 ...
  启用规则数: 5
    - duplicate_package_name: 包名唯一性检查 [ERROR]
    - package_name_required: 包名必填检查 [ERROR]
    - version_format: 版本号格式检查 [WARNING]
    - checksum_required: 校验和必填检查 [ERROR]
    - source_path_exists: 源路径存在性检查 [WARNING]

═════════════════════════════════════════════════
  校验摘要: 共 17 项检查
    通过 : 9   失败 : 8   警告 : 5   错误 : 3   跳过 : 0
═════════════════════════════════════════════════

[FAIL] 失败项详情:
+------+---------------+----------+--------+----------------------------------------+
| 序号   | 包名            | 规则       | 级别     | 详细信息                                   |
+======+===============+==========+========+========================================+
| #1   | service-alpha | 校验和必填检查  | ERROR  | 包 'service-alpha' 缺少 checksum 校验和      |
| #2   | service-beta  | 校验和必填检查  | ERROR  | 包 'service-beta' 缺少 checksum 校验和       |
| #3   | service-gamma | 校验和必填检查  | ERROR  | 包 'service-gamma' 缺少 checksum 校验和      |
| #4   | service-delta | 版本号格式检查  | WARNING| 版本号 'not-semver' 不符合语义化版本格式 (x.y.z) |
| ...  | （4 条源路径不存在 WARNING，仅告警不阻塞）                              |
+------+---------------+----------+--------+----------------------------------------+

[FAIL] 存在未解决的错误项，后续审批被阻塞。
  如需忽略请使用 --continue-on-error，或修复清单后重新执行 check。
# 退出码: 2，当前状态: CHECK_FAILED
```

**Step 3 - 审批被阻塞（默认拦截）**
```bash
$ python patchgate_cli.py --no-color approve demo-check-fail-01 --approver zhangsan
错误: 批次状态为 CHECK_FAILED，存在未解决的失败项。
  请先修复问题并重新 check，或使用 --force 强制审批。
  当前未解决失败项共 8 个：
    - [error] 校验和必填检查: 包 'service-alpha' 缺少 checksum 校验和
    - [error] 校验和必填检查: 包 'service-beta' 缺少 checksum 校验和
    - [error] 校验和必填检查: 包 'service-gamma' 缺少 checksum 校验和
    ... 等共 8 项
# 退出码: 1，批次仍停留在 CHECK_FAILED
```

**Step 4 - 选择驳回（CHECK_FAILED → REJECTED，新状态流转已支持）**

此时除了修复问题重新 check，也可以直接驳回：

```bash
$ python patchgate_cli.py --no-color reject demo-check-fail-01 \
    --approver zhangsan --comment "checksum 必须补齐才能放行"
[FAIL] 审批已驳回
  批次 ID   : demo-check-fail-01
  审批人     : zhangsan
  驳回原因   : checksum 必须补齐才能放行
  当前状态   : REJECTED
  修复后请重新执行: patchgate check demo-check-fail-01
# 退出码: 0
```

**Step 5 - 修复清单 + 重新 check → 审批 → 发布**

修复 checksum 后，`check` 会自动从 `rejected` → `checking` → `check_passed`，后续审批发布同正常链路。

---

### 演示三：发布 → 撤销回退 → 重新发布

**Step 1 - 已发布后发现问题**
```bash
$ python patchgate_cli.py revoke demo-normal-01 --operator zhangsan --comment "监控发现 auth-service 报错率升高，紧急撤销，版本回滚到 2.4.0"
✓ 已撤销发布
  批次 ID   : demo-normal-01
  操作人     : zhangsan
  回退备注   : 监控发现 auth-service 报错率升高，紧急撤销，版本回滚到 2.4.0
  当前状态   : APPROVED
  (批次已恢复到审批通过状态，可再次发布或修改)
  重新发布: patchgate publish demo-normal-01 --operator <姓名>
```

**Step 2 - 查看历史确认**
```bash
$ python patchgate_cli.py history demo-normal-01 --type publish
── 发布/回退记录 ──
┌────┬──────────┬──────┬──────────────────────────────────────────────┬──────────────────┐
│  # │ 操作人    │ 动作  │ 备注                                          │ 时间              │
├────┼──────────┼──────┼──────────────────────────────────────────────┼──────────────────┤
│  1 │ lisi     │ 发布  │ 6/20 凌晨2:00-2:25 灰度+全量发布完成，监控正常 │ 2026-06-18T10:30 │
│  2 │ zhangsan │ 撤销  │ 监控发现 auth-service 报错率升高...            │ 2026-06-18T10:35 │
└────┴──────────┴──────┴──────────────────────────────────────────────┴──────────────────┘
```

**Step 3 - 修复 auth-service 后重新发布**
```bash
# 修改清单后重新校验（从 APPROVED 状态返回 check）
$ python patchgate_cli.py check  demo-normal-01
$ python patchgate_cli.py approve demo-normal-01 --approver zhangsan --comment "替换为 2.4.2 修复版，重新审批"
$ python patchgate_cli.py publish demo-normal-01 --operator lisi --comment "6/20 12:00 修复版发布"
```

---

### 演示四：断点续跑（一键到发布）

场景：导入了清单但只执行了 check 就被打断，下次无需逐命令重跑。

```bash
# 只执行了前两步
$ python patchgate_cli.py import examples/manifest_good.json --id "demo-resume-01"
$ python patchgate_cli.py check  demo-resume-01
# 此时状态: CHECK_PASSED

# 直接一键推进到发布（无需 approve/publish 分开敲）
$ python patchgate_cli.py resume demo-resume-01 --to publish --approver zhangsan --operator lisi
▶ 续跑批次 demo-resume-01
  当前状态: CHECK_PASSED
  目标阶段: publish

→ 自动审批 (审批人: zhangsan) ...
✓ 审批阶段完成
→ 自动发布 (操作人: lisi) ...
✓ 发布阶段完成

✓ 续跑完成，当前状态: PUBLISHED
```

如果此时去查看 history，会看到完整的审批 + 发布记录依然落盘保留，与手动执行无差别。

---

### 演示五：接手包导出导入全链路

**场景**：张三（zhangsan）负责的 Q2 补丁批次已完成校验和审批，现在要交接给李四（lisi）。张三导出接手包发给李四，李四导入后核对历史结论并继续发布。

**Step 1 - 张三：完成校验和审批**
```bash
$ python patchgate_cli.py import examples/manifest_good.json \
    --id "release-2026-q2" --name "2026年Q2安全补丁"
[OK] 批次创建成功
  批次 ID   : release-2026-q2
  名称      : 2026年Q2安全补丁
  当前状态  : created

$ python patchgate_cli.py check release-2026-q2
[>] 开始校验批次 release-2026-q2 ...
  校验摘要: 共 16 项检查
    通过 : 11   失败 : 5   警告 : 5   错误 : 0   跳过 : 0
[OK] 校验通过（仅 warning 不阻塞）
  当前状态: check_passed

$ python patchgate_cli.py approve release-2026-q2 \
    --approver zhangsan --comment "所有补丁已代码评审，版本核对无误"
[OK] 审批通过
  批次 ID   : release-2026-q2
  审批人     : zhangsan
  审批意见   : 所有补丁已代码评审，版本核对无误
  当前状态   : approved
```

**Step 2 - 张三：导出接手包**
```bash
$ python patchgate_cli.py handover-export release-2026-q2 \
    -o release-2026-q2-handover.json \
    -e zhangsan \
    -n "Q2补丁批次已完成校验和审批，李四接手后可直接发布"
[OK] 接手包导出成功
  输出文件   : release-2026-q2-handover.json
  包哈希     : 6a7b8c9d...
  包含内容   : 批次信息 + 校验结果 + 审批结论 + 规则快照 + 待办 + 日志索引
```

张三把 `release-2026-q2-handover.json` 发给李四。

**Step 3 - 李四：先 dry-run 预览**
```bash
$ python patchgate_cli.py handover-import release-2026-q2-handover.json \
    --by lisi --dry-run
[>] 导入接手包: release-2026-q2-handover.json
  导入人: lisi

[OK] 接手包格式验证通过
  包哈希     : 6a7b8c9d...
  生成时间   : 2026-06-18T10:30:00
  导出人     : zhangsan
  批次 ID    : release-2026-q2
  批次名称   : 2026年Q2安全补丁
  批次状态   : approved

[OK] 无冲突

[DRY-RUN] 模拟导入成功（未实际写入）
  新批次 ID : release-2026-q2
```

**Step 4 - 李四：正式导入**
```bash
$ python patchgate_cli.py handover-import release-2026-q2-handover.json \
    --by lisi -n "从张三处接手Q2补丁批次"
[>] 导入接手包: release-2026-q2-handover.json
  导入人: lisi
  导入备注: 从张三处接手Q2补丁批次

[OK] 接手包格式验证通过
  ...

[OK] 无冲突

[OK] 接手包导入成功
  新批次 ID : release-2026-q2
  导入记录 ID: #1

  当前状态  : approved

📌 接手人待办 📌
  [!] 已通过审批（zhangsan），可标记发布
     $ patchgate publish release-2026-q2 --operator <姓名>
  [-] 如需修改可重新校验
     $ patchgate check release-2026-q2
```

**Step 5 - 李四：核对历史结论**
```bash
$ python patchgate_cli.py status release-2026-q2
批次 release-2026-q2
  名称       : 2026年Q2安全补丁
  当前状态   : approved
  ...

📌 接手来源 📌
  导入记录 #1:
    原批次 ID : release-2026-q2
    包哈希    : 6a7b8c9d...
    导出人    : zhangsan
    导出时间  : 2026-06-18T10:30:00
    导出备注  : Q2补丁批次已完成校验和审批...
    导入人    : lisi
    导入时间  : 2026-06-18T10:35:00
    导入备注  : 从张三处接手Q2补丁批次

审批记录:
  [通过] zhangsan @ 2026-06-18T10:30:00: 所有补丁已代码评审，版本核对无误
```

李四确认了张三的审批结论和校验结果，与交接说明一致。

**Step 6 - 李四：继续发布**
```bash
$ python patchgate_cli.py publish release-2026-q2 \
    --operator lisi --comment "凌晨2:00-2:30灰度+全量发布完成，监控正常"
[OK] 已发布
  批次 ID   : release-2026-q2
  操作人     : lisi
  发布备注   : 凌晨2:00-2:30灰度+全量发布完成，监控正常
  当前状态   : published
```

**Step 7 - 查看全部导入记录（审计）**
```bash
$ python patchgate_cli.py handover-list
+-----+-----------------+-----------------+--------+---------------------+----------+
|   # | 批次 ID         | 原 ID           | 导入人  | 导入时间             | 导出人    |
+=====+=================+==========+=====================+==========+
|   1 | release-2026-q2 | release-2026-q2 | lisi   | 2026-06-18T10:35:00 | zhangsan |
+-----+-----------------+-----------------+--------+---------------------+----------+
```

---

#### 冲突处理示例：同名批次 + 规则变更

如果李四本地已经有一个同名批次，且规则文件与包内快照不一致，导入时会检测到冲突：

```bash
# 先检测冲突
$ python patchgate_cli.py handover-import release-2026-q2-handover.json \
    --by lisi --dry-run
[!] 检测到 2 个冲突

  1. [严重] 批次 ID 已存在 (type: duplicate_id)
     可用选项: keep_local, overwrite_with_package, rename_package
     本地批次: release-2026-q2 (李四本地的Q2批次)

  2. [中等] 本地规则文件与包内快照不一致 (type: rules_changed)
     可用选项: keep_package_snapshot, switch_to_local_rules
     差异: 修改 1 条规则 (风险: HIGH)

# 选择解决方案：重命名批次 + 保留包内规则快照
$ python patchgate_cli.py handover-import release-2026-q2-handover.json \
    --by lisi \
    --resolve duplicate_id=rename_package \
    --resolve rules_changed=keep_package_snapshot
[OK] 接手包导入成功
  新批次 ID : release-2026-q2-imported-202606181035
  导入记录 ID: #2

📌 冲突处理记录 📌
  - duplicate_id: rename_package
  - rules_changed: keep_package_snapshot
```

导入后原批次不受影响，新批次包含接手包的所有数据，且规则快照保留了包内版本。

---

## 数据持久化说明

**所有数据保存在本地 SQLite 数据库**（默认路径 `.patchgate/patchgate.db`）。

包含 8 张表：

| 表名 | 用途 |
|---|---|
| `batches` | 批次主表（ID/名称/状态/清单哈希/时间戳） |
| `manifest_items` | 清单条目（包名/版本/路径/校验和/自定义元数据） |
| `check_results` | 每次 check 的逐条检查结果（含 severity、详细信息 JSON） |
| `approvals` | 审批记录（审批人/决定/备注/时间戳） |
| `publish_records` | 发布与撤销记录（操作人/动作类型/备注/时间戳） |
| `status_history` | 每一次状态流转的完整轨迹 |
| `rule_snapshots` | 规则快照（校验时的规则配置，保证可追溯） |
| `handover_imports` | 接手包导入记录（审计追溯用，包含导出人、导入人、冲突处理决策） |

**重新执行工具时**：
- 所有之前的批次、检查结果、审批人、回退备注、导出摘要**保持不变**
- 接手包导入记录、来源说明、冲突处理决策**持久化保留**，重启后仍可通过 `status` / `history` / `handover-list` 查看
- 可以在任何时候对任一历史批次执行 `status` / `history` / `export` 查看
- 未完成的批次（`check_failed` / `rejected` / `approved`）可以随时 `resume` 续跑
- 已发布的可以随时 `revoke` 回退

需要完全重置时，删除 `.patchgate/` 目录即可。
