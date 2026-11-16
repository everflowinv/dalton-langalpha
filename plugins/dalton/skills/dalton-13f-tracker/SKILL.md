---
name: dalton-13f-tracker
description: "查询美股机构 SEC（美国证监会）13F 季度持仓、对比持仓变化并按 China Book / AI/Semi/US SaaS / Other US Book 分类生成固定格式中文报告，适用于机构持仓追踪与组合拆解"
---

# 13F 机构持仓追踪（dalton-13f-tracker）

⚠️ **必须执行 `python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py` 获取并对比持仓数据，不要自行重写 Python 抓取脚本。**
⚠️ **固定格式报告由代码直接生成，不要改写模板措辞。**
⚠️ **分类 JSON 只接受 SEC 股票代码（ticker，如 `DASH`），不接受公司名（如 `DOORDASH`）。系统会自动将公司名解析为股票代码，无需手动转换。**
⚠️ **每次运行「全部机构」对比报告时，必须覆盖 §11 默认清单的全部 19 个 SEC 13F（表格代码 `13F-HR`，即美国证监会机构季度持仓报告）申报主体，合并 Hillhouse 旗下两个主体后输出 18 组报告，不要省略 First Beijing / Evergreen 等。**
⚠️ **Hillhouse 必须合并 HHLR + Evergreen Quality Fund GP（CIK 美国证监会机构编号 0001762304 + 0002016939），按 CUSIP（北美证券统一识别码）加总。**

---

## 0) 环境与工作区状态

- **SEC 身份**：脚本按 `SEC_USER_AGENT` → `EDGAR_IDENTITY` → 内置默认身份的顺序读取请求身份；沙箱环境通常已注入 `SEC_USER_AGENT`。如需临时覆盖，可在命令前设置 `SEC_USER_AGENT` 或 `EDGAR_IDENTITY`。
- **内置数据与工作区覆盖层**：
  - `scripts/` 下的 5 个映射 JSON（`classification.json`、`institution_map.json`、`merge_rules.json`、`name_to_ticker.json`、`ticker_aliases.json`）为只读内置初始数据，脚本不会写入该目录。
  - 传入 `--workspace-dir <workspace>` 时，脚本会合并读取内置初始数据与 `<workspace>/state/dalton-13f-tracker/` 下的同名覆盖层文件（工作区覆盖层优先于内置数据）；`auto-learn`（自动学习，即自动将新识别的映射写入工作区覆盖层）、`map-propose`、`map-apply` 的更新，以及审计日志 `auto_learn_log.jsonl`、补丁文件 `map_patches.json` 均写入 `<workspace>/state/dalton-13f-tracker/`。
  - 未传 `--workspace-dir` 时，脚本会打印一行提示，仅使用只读内置数据，关闭所有磁盘写入。

---

## 0) 依赖

脚本依赖 `edgartools`（SEC 数据客户端），沙箱镜像已预装。如果运行时报 `ModuleNotFoundError: No module named 'edgar'`，说明当前沙箱创建得较早、缺少这个依赖：不要自行安装或改写抓取脚本，直接告诉用户当前沙箱缺少 edgartools，需要管理员补装后再运行。

## 1) 健康检查（首次运行建议先执行）

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json self-test --institution 0001067983
```

---

## 2) 核心用法

### A) 固定格式中文报告（推荐）

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
  --workspace-dir <workspace> --json compare --institution HHLR --quarter 25Q4 --format cn
```

- `--quarter 25Q4` 等同于 `--offset 0`（如果 25Q4 是最新季度）
- 输出的 `formatted_report` 字段就是完整中文报告，**直接原样返回给用户，不做任何修改**

### B) 原始数据模式

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
  --workspace-dir <workspace> --json compare --institution HHLR --quarter 25Q4 --format raw
```

### C) 其他子命令

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json summary --institution HHLR --quarter 25Q4
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json top --institution HHLR --quarter 25Q4 --limit 10
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json search --query "aspex"
```

---

## 3) 输出模板说明（代码自动生成，不要改写）

### 基础模板

| 场景 | 输出格式 | 示例 |
|------|---------|------|
| 新建仓 | `建仓{name}，{金额}` | 建仓Nvidia，5.4亿美元 |
| 清仓 | `清仓{name}（之前持仓{金额}）` | 清仓Baidu（之前持仓4900万美元） |
| 大幅加仓（>50%） | `大幅加仓{name}至{金额}，之前持仓{金额}` | 大幅加仓Grab至3.0亿美元，之前持仓1.7亿美元 |
| 加仓（3-50%） | `加仓{name} {pct}%至{金额}` | 加仓FUTU 38%至5.5亿美元 |
| 减仓（>2%） | `减仓{name} {pct}%至{金额}` | 减仓Sea 21%至4.3亿美元 |
| 几乎不变（≤2%） | `{name}仓位几乎不变，{金额}` | PDD仓位几乎不变，3.5亿美元 |
| 完全不变（0%） | `{name}仓位不变，{金额}` | YMM仓位不变，2.5亿美元 |

### 连续趋势前缀（自动生成）

系统会自动获取上一期对比数据。如果满足以下条件，输出前自动加趋势前缀：

| 当期动作 | 上期同公司动作 | 前缀 |
|---------|--------------|------|
| 加仓/大幅加仓 | 建仓 | `继{上期Q}建仓后，继续` |
| 加仓/大幅加仓 | 加仓/大幅加仓 | `继{上期Q}加仓后，继续` |
| 减仓 | 减仓 | `继{上期Q}减仓后，继续` |

示例：`继25Q3建仓后，继续加仓Amer Sports 28%至2.8亿美元`

### 可转债（CB）识别

- 通过 CUSIP 第 7-8 位自动识别：含字母 = 债券/CB，纯数字 = 正股
- CB 持仓名称自动追加 ` CB` 后缀（如 `H World CB`）
- CB 与正股独立计算，不合并

### 金额格式

- ≥1亿美元 → `X.X亿美元`
- <1亿美元 → `XX00万美元`（四舍五入到百万）

---

## 4) 分类体系

三个类别：`China Book`（中概股）/ `AI/Semi/US SaaS`（AI、半导体与美股软件服务）/ `Other US Book`（其他美股）。

初始映射保存在只读内置文件 `scripts/classification.json`，工作区覆盖层写入 `<workspace>/state/dalton-13f-tracker/classification.json`。

### 分类写入规则

- **只接受 SEC 股票代码（ticker）格式**（1-6 个大写字母）
- 如果用户给的是公司名（如 "Doordash"），系统会**自动解析**为对应股票代码（如 "DASH"）
- 解析依赖 `name_to_ticker.json`（传入 `--workspace-dir` 运行 `compare` 时，脚本会从 SEC 数据自动更新公司名到股票代码的映射至工作区覆盖层）
- 如果公司名无法解析，系统会拒绝写入并提示先运行 `compare`
- **不要**手动修改内置 `scripts/classification.json`

### 股票代码别名（Ticker Alias）机制

某些公司的 SEC 申报文件（filing）中的股票代码与常用股票代码不同（如 TAL Education 在 SEC 申报中为 `XRS`，但用户习惯用 `TAL`）。

`ticker_aliases.json` 解决此问题：

```json
{
  "XRS": "TAL",
  "FB": "META"
}
```

- **键** = SEC 申报文件中使用的股票代码
- **值** = 分类映射中使用的股票代码
- 分类时先查别名表，再用转换后的股票代码查找对应类别
- **Auto-learn**：传入 `--workspace-dir` 运行 `compare` 时，如果 SEC 股票代码未分类，但发行人名称（issuer name）能通过 `name_to_ticker.json` 关联到已分类的股票代码，脚本会自动将别名写入工作区覆盖层
- 手动维护：`map-propose --workspace-dir <workspace> --instruction "alias XRS -> TAL"`

---

## 5) 维护映射表（需传 `--workspace-dir`）

### 自然语言维护（推荐）

先生成补丁（patch）预览：

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
  --workspace-dir <workspace> --json map-propose --instruction "把 DASH 从 AI 改到 Other"
```

确认后应用：

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
  --workspace-dir <workspace> --json map-apply --patch-id p1741140000
```

支持的指令样式（股票代码或公司名均可，系统自动解析）：
- `把 Doordash 从 AI 改到 Other` → 自动识别为 DASH
- `NVDA 分类到 AI`
- `Coupang 归类到 Other` → 自动识别为 CPNG
- `hhlr -> 0001762304`（机构别名）
- `merge GOOG,GOOGL -> Alphabet`（合并同一公司的不同股份类别 share class）
- `alias XRS -> TAL`（SEC 股票代码别名）
- `XRS 实际是 TAL`（中文写法）

### 查看映射

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --workspace-dir <workspace> --json map-show --type all
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json map-show --type alias
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json map-show --type classification --key DASH
```

---

## 6) 自动学习（Auto-Learn）

传入 `--workspace-dir <workspace>` 时默认开启（`--auto-learn true`），每次运行 `compare` 后自动将新识别的映射增量写入 `<workspace>/state/dalton-13f-tracker/`：
- 未分类的新股票代码 → 按兜底规则（fallback）推断类别后写入 `classification.json` 覆盖层
- 新机构别名 → 写入 `institution_map.json` 覆盖层
- 股份类别（share class）合并关系 → 写入 `merge_rules.json` 覆盖层
- 公司名→股票代码映射 → 写入 `name_to_ticker.json` 覆盖层
- **SEC 股票代码别名** → 写入 `ticker_aliases.json` 覆盖层（当 SEC 股票代码与已分类股票代码不一致时）
- 审计日志 → `<workspace>/state/dalton-13f-tracker/auto_learn_log.jsonl`

关闭写入：`--auto-learn false`（或不传 `--workspace-dir`）。

---

## 7) 机构（Institution）查找

支持 CIK / 机构别名（或股票代码）/ 机构名称（不区分大小写）：

```bash
--institution 0001762304    # CIK
--institution HHLR          # 机构别名
--institution "Aspex"       # 机构名
```

映射在内置 `scripts/institution_map.json` 与工作区覆盖层中。未找到时用 `search` 命令查：

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json search --query "aspex"
```

---

## 8) JSON 输出格式约定

### `compare --format cn`

```json
{
  "ok": true,
  "institution": "...",
  "offset": 0,
  "manager": "Aspex Management (HK) Ltd",
  "base_period": "2025-12-31",
  "rows": [...],
  "formatted_report": "**China Book**\n- 加仓FUTU 38%至5.5亿美元\n...",
  "categories": {
    "China Book": ["..."],
    "AI/Semi/US SaaS": ["..."],
    "Other US Book": ["..."]
  },
  "total_positions": 25,
  "filtered_biotech": 4
}
```

### 错误返回（Error）

```json
{"ok": false, "error": "...", "hint": "..."}
```

---

## 9) 生物医药（Biotech）过滤

默认开启，自动排除名称或分类含 Bio / Therapeutics / Pharma / Genomics 等关键词的生物医药公司。
关闭：`--include-biotech`

---

## 10) 报告输出规则（⚠️ 必须遵守）

当用户要求固定格式报告时：
1. 运行 `compare --format cn --json`
2. **直接使用 `formatted_report` 字段**作为最终回复
3. 可在报告前加 1 行上下文（如机构名和季度），**不要修改报告内容**
4. **不要**重新计算百分比、改动措辞、重排顺序、重新分类
5. **不要**根据自己的判断把公司从一个类别移到另一个——分类完全由代码决定
6. 若 `ok` 为 `false`，返回 `error` 与 `hint`（错误原因和排查提示）

### 示例回复格式

```
Aspex Management 25Q4 vs 25Q3：

**China Book**
- 加仓FUTU 38%至5.5亿美元
- PDD仓位几乎不变，3.5亿美元
...
```

### 禁止的行为

- 把 `formatted_report` 中 China Book 下的公司移到 AI/Semi/US SaaS（分类已由代码确定）
- 修改金额数字（如把"4.5亿"改成"4.50亿"）
- 重新排序（代码按持仓市值降序排列）
- 添加自己的分析评论到报告条目中
- 省略任何条目

---

## 11) 默认机构清单（标准清单，不可省略）

每次运行「全部机构」对比报告时，必须覆盖以下 **19 个 13F 申报主体**，按三个组别输出。Hillhouse 的 HHLR 与 Evergreen 两个主体合并成一组报告，所以最终是 **18 组报告**：China Book 8 组（9 个主体）、AI/Semi/US SaaS-heavy 9 组、Value 1 组。核对完整性时请清点报告组数，总数应为 18 组。

### China Book
- **Hillhouse**（HHLR + Evergreen，⚠️ 必须合并）
  - HHLR Advisors: `0001762304`
  - Evergreen Quality Fund GP Ltd: `0002016939`
- **First Beijing Investment Ltd**: `0001701717`
- **Aspex Management**: `0001768375`
- **Greenwoods 景林（香港）**: `0001848138`（别名 `Greenwoods` / `景林` / `Jinglin`，⚠️ 不是 First Beijing `0001701717`）
- **Sequoia China**: `Sequoia China` (auto-resolved)
- **Capital Today**: via institution_map
- **Brilliance**: via institution_map
- **Yiheng Capital**: via institution_map

### AI/Semi/US SaaS-heavy
- **Tiger Global Management**: `0001167483`
- **Coatue Management**: `Coatue` (auto-resolved)
- **D1 Capital**: via institution_map
- **Altimeter Capital**: via institution_map
- **Whale Rock Capital**: via institution_map
- **Dragoneer Investment**: via institution_map
- **Lone Pine Capital**: via institution_map
- **Sequoia US (Sequoia Capital)**: via institution_map
- **Durable Capital Partners**: via institution_map

### Value
- **Berkshire Hathaway**: `0001067983`

### Hillhouse 合并规则（关键）

HHLR (`0001762304`) 与 Evergreen Quality Fund GP (`0002016939`) 是 Hillhouse 同体系两个 13F 申报主体，**必须按 CUSIP 层面合并**后再生成报告。`compare` 已内置此逻辑：传入 `--institution HHLR` 会自动获取 Evergreen 同一报告期的对比持仓行（`compare rows`），按 CUSIP 加总 `Shares`/`Value`/`PrevShares`/`PrevValue`，重算变动、百分比和 `Status`（`NEW`/`CLOSED`/`INCREASED`/`DECREASED`/`UNCHANGED`），上一季趋势同样合并；输出的 `merged_with` 记录合并了哪个主体及多少行。其他同体系主体用 `--merge-with <CIK或名称>[,...]`；若仅查看 HHLR 单个主体，请加 `--no-merge`。不要自己临时编写合并脚本。

---

## 12) 数据文件清单

| 文件 | 用途 | 维护方式 |
|------|------|---------|
| `scripts/classification.json` | 股票代码 → 三大类别（内置初始只读） | 工作区覆盖层 + `map-propose` |
| `scripts/institution_map.json` | 机构别名 → CIK（内置初始只读） | 工作区覆盖层 + `map-propose` |
| `scripts/merge_rules.json` | 股份类别（share class）合并（内置初始只读） | 工作区覆盖层 + `map-propose` |
| `scripts/name_to_ticker.json` | 公司名 → SEC 股票代码（内置初始只读） | 工作区覆盖层（运行 `compare` 时更新） |
| `scripts/ticker_aliases.json` | SEC 股票代码 → 分类用股票代码（内置初始只读） | 工作区覆盖层 + `map-propose` |
| `<workspace>/state/dalton-13f-tracker/auto_learn_log.jsonl` | 自动学习（auto-learn）与补丁（patch）审计日志 | 自动追加（需传 `--workspace-dir`） |
| `<workspace>/state/dalton-13f-tracker/map_patches.json` | `map-propose` / `map-apply` 补丁记录 | 自动读写（需传 `--workspace-dir`） |
