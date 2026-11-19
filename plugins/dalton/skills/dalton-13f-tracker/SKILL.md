---
name: dalton-13f-tracker
description: "查询美股机构 SEC（美国证监会）13F 季度持仓、对比持仓变化并按 China Book / AI/Semi/US SaaS / Other US Book 分类生成固定格式中文报告，适用于机构持仓追踪与组合拆解"
---

# 13F 机构持仓追踪（dalton-13f-tracker）

⚠️ **必须执行 `python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py` 获取并对比持仓数据，不要自行重写 Python 抓取脚本。**
⚠️ **固定格式报告由代码直接生成，不要改写模板措辞。**
⚠️ **分类文件 `classification.json` 只存储 SEC（美国证监会）股票代码（ticker，如 `DASH`），不存储公司名（如 `DOORDASH`）。维护分类时，系统会尝试自动将公司名解析为股票代码，通常无需手动转换。**
⚠️ **每次生成全量季度报告时，必须覆盖 §11 默认清单的全部 19 个 SEC 13F（表格代码 `13F-HR`，即美国证监会机构季度持仓报告）申报主体，合并 Hillhouse（高瓴）旗下两个主体后输出 18 组报告，不要省略 First Beijing / Evergreen 等。**
⚠️ **Hillhouse（高瓴）必须合并 HHLR + Evergreen Quality Fund GP（CIK，即美国证监会机构编号：`0001762304` + `0002016939`），按 CUSIP（北美证券统一识别码）加总。**

---

## 0) 环境、依赖与工作区状态

- **SEC 请求身份**：脚本按环境变量 `SEC_USER_AGENT` → `EDGAR_IDENTITY`（SEC 电子数据系统 EDGAR 身份标识）→ 内置默认身份的顺序读取请求身份；沙箱环境通常已注入 `SEC_USER_AGENT`。如需临时覆盖，可在命令前设置 `SEC_USER_AGENT` 或 `EDGAR_IDENTITY`。
- **运行依赖**：脚本依赖 `edgartools`（SEC 数据客户端），沙箱镜像已预装。如果运行时报 `ModuleNotFoundError: No module named 'edgar'`，说明当前沙箱镜像较早、缺少该依赖：不要自行安装或改写抓取脚本，直接告知用户当前沙箱缺少 `edgartools`，需要管理员补装后再运行。
- **内置数据与工作区覆盖层**：
  - `scripts/` 下的 7 个内置 JSON（`classification.json`、`descriptors.json`、`institution_map.json`、`merge_rules.json`、`name_to_ticker.json`、`ticker_aliases.json`、`verified_tickers.json`）为只读内置初始数据，脚本不会写入该目录。
  - 传入 `--workspace-dir <workspace>` 时，脚本会合并读取内置初始数据与 `<workspace>/state/dalton-13f-tracker/` 下的同名工作区覆盖层文件（工作区覆盖层优先于内置数据）。自动学习（`auto-learn`，即自动将新识别的映射写入工作区覆盖层）、`map-propose`（生成映射补丁）、`map-apply`（应用映射补丁）、`descriptors-add`（写入业务定语，即 4–24 字公司主营业务简短说明）产生的更新，以及审计日志 `auto_learn_log.jsonl`、补丁记录文件 `map_patches.json`，均写入 `<workspace>/state/dalton-13f-tracker/`。
  - 未传 `--workspace-dir` 时，脚本会打印一行提示，仅使用只读内置数据，并关闭所有磁盘写入。

## 1) 健康检查（首次运行建议先执行）

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json self-test --institution 0001067983
```

---

## 2) 两种模式与核心用法

- **模式一（单机构快查）**：查询单家机构持仓或季度变动，使用下方 A–C 子命令并遵守 §10。
- **模式二（全量季度报告）**：覆盖 18 组机构并生成固定五段式完整报告，见 §14 流程及相关子命令（`aggregate`、`descriptors-add`、`render-batch`、`verify-numbers`）。

### A) 固定格式中文报告（单机构快查推荐）

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
  --workspace-dir <workspace> --json compare --institution HHLR --quarter 25Q4 --format cn
```

- `--quarter 25Q4` 等同于 `--offset 0`（如果 25Q4 是最新季度）
- 输出的 `formatted_report` 字段就是完整中文报告正文，**直接原样返回给用户，不做任何修改**（详见 §10）

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

以下条目模板用于单机构快查及全量季度报告后三段各家明细；全量季度报告整体采用 §14 固定五段式结构（后三段由 `render-batch` 按固定模板直接生成）。

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

系统会自动获取上期对比数据。如果满足以下条件，输出前自动加趋势前缀：

| 当期动作 | 上期同公司动作 | 前缀 |
|---------|--------------|------|
| 加仓/大幅加仓 | 建仓 | `继{上期Q}建仓后，继续` |
| 加仓/大幅加仓 | 加仓/大幅加仓 | `继{上期Q}加仓后，继续` |
| 减仓 | 减仓 | `继{上期Q}减仓后，继续` |

示例：`继25Q3建仓后，继续加仓Amer Sports 28%至2.8亿美元`

### 可转债（CB，Convertible Bond）识别

- 通过 CUSIP 第 7-8 位自动识别：含字母 = 债券/CB，纯数字 = 正股
- CB 持仓名称自动追加 ` CB` 后缀（如 `H World CB`）
- CB 与正股独立计算，不合并

### 金额格式

- 先按原值决定单位，再单次舍入（不跨单位二次进位）：
  - 原值 ≥ 9950 万美元 → `X.X亿美元`（保留 1 位小数；≥ 9950 万统一用亿，如 9960 万进位为 `1.0亿美元`，避免 `10000万美元`）
  - 原值 < 9950 万美元 → `XX00万美元`（按原值单次四舍五入到百万位，如 9949.7 万 → `9900万美元`）

---

## 4) 分类体系

三个类别：`China Book`（中概股）/ `AI/Semi/US SaaS`（AI 人工智能、半导体与美股软件服务 SaaS）/ `Other US Book`（其他美股）。

初始映射保存在只读内置文件 `scripts/classification.json`，工作区覆盖层写入 `<workspace>/state/dalton-13f-tracker/classification.json`。

### 分类写入规则

- **只接受 SEC 股票代码格式**（1-6 个大写字母，可带 `.` 或 `-` 加 1-2 个字母的股份类别后缀，如 `BRK.B`）
- **公司名自动解析**：在维护指令中传入公司名时，系统会尝试通过 `name_to_ticker.json` 自动解析为股票代码（如 DoorDash → `DASH`）；传入 `--workspace-dir` 运行 `compare` 时，脚本会从 SEC 数据自动更新该映射至工作区覆盖层
- 如果公司名无法解析，系统会拒绝写入并提示先运行 `compare`（也可先通过 `name_to_ticker.json` 或 `compare` 结果查到代码后，直接在指令中写股票代码）
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
- **自动学习（`auto-learn`）**：传入 `--workspace-dir` 运行 `compare` 时，如果 SEC 股票代码未分类，但发行人名称（issuer name）能通过 `name_to_ticker.json` 关联到已分类的股票代码，脚本会自动将别名写入工作区覆盖层
- 手动维护：`map-propose --workspace-dir <workspace> --instruction "alias XRS -> TAL"`

---

## 5) 维护映射表（需传 `--workspace-dir`）

### 自然语言维护（推荐）

先生成补丁（patch）预览：

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
  --workspace-dir <workspace> --json map-propose --instruction "把 DASH 从 AI 改到 Other"
```

确认后应用（`--patch-id` 使用 `map-propose` 返回的 `patch_id` 字段值）：

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
  --workspace-dir <workspace> --json map-apply --patch-id p1741140000_1a2b3c4d
```

支持的指令样式：
- `把 DASH 从 AI 改到 Other`
- `NVDA 分类到 AI`
- `CPNG 归类到 Other`
- `hhlr -> 0001762304`（机构别名）
- `merge GOOG,GOOGL -> Alphabet`（合并同一公司的不同股份类别（share class））
- `alias XRS -> TAL`（SEC 股票代码别名）
- `XRS 实际是 TAL`（中文写法）

### 查看映射

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --workspace-dir <workspace> --json map-show --type all
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json map-show --type alias
python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py --json map-show --type classification --key DASH
```

---

## 6) 自动学习（auto-learn）

传入 `--workspace-dir <workspace>` 时默认开启（`--auto-learn true`），每次运行 `compare` 后自动将新识别的映射增量写入 `<workspace>/state/dalton-13f-tracker/`：
- 未分类的新股票代码 → 按默认分类规则（fallback）推断类别后写入 `classification.json` 覆盖层
- 新机构别名 → 写入 `institution_map.json` 覆盖层
- 股份类别合并关系 → 写入 `merge_rules.json` 覆盖层
- 公司名→股票代码映射 → 写入 `name_to_ticker.json` 覆盖层
- **SEC 股票代码别名** → 写入 `ticker_aliases.json` 覆盖层（当 SEC 股票代码与已分类股票代码不一致时）
- 审计日志 → `<workspace>/state/dalton-13f-tracker/auto_learn_log.jsonl`

关闭写入：`--auto-learn false`（或不传 `--workspace-dir`）。

---

## 7) 机构（Institution）查找

支持 CIK、机构别名（或股票代码）或机构名称（不区分大小写）：

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
（全量季度报告计算单家机构 13F 仓位权重时，分母仍使用含生物医药在内的 SEC 申报总市值。）

---

## 10) 单机构报告输出规则（⚠️ 单机构快查必须遵守；全量季度报告见 §14）

当用户要求单机构固定格式报告时：
1. 运行 `compare --format cn --json`
2. 若 `ok` 为 `false`，返回 `error`（错误原因）与 `hint`（排查提示）
3. 若 `ok` 为 `true`，**直接使用 `formatted_report` 字段**作为最终回复正文，最多在开头加 1 行上下文（如机构名和季度），**不要修改报告内容**

### 示例回复格式

```
Aspex Management 25Q4 vs 25Q3：

**China Book**
- 加仓FUTU 38%至5.5亿美元
- PDD仓位几乎不变，3.5亿美元
...
```

### 禁止的行为

- 重新分类或跨类别移动公司（如把 `formatted_report` 中 China Book 下的公司移到 AI/Semi/US SaaS——分类完全由代码决定）
- 重新计算百分比、改动模板措辞或修改金额数字格式（如把「4.5亿」改成「4.50亿」）
- 重新排序（代码已按持仓市值降序排列）
- 在报告条目中添加个人分析评论
- 省略任何条目

---

## 11) 默认机构清单（标准清单，不可省略）

每次生成全量季度报告时，必须覆盖以下 **19 个 13F 申报主体**，按三个机构组别输出。Hillhouse 的 HHLR 与 Evergreen 两个主体合并成一组报告，所以最终是 **18 组报告**：`China Book`（中国及中概背景机构）8 组（9 个主体）、`US Growth`（美股成长型机构）9 组、`Value`（价值投资型机构）1 组。18 组固定顺序与标准显示名见 `scripts/institution_map.json` 的 `_groups`；核对完整性时请清点报告组数，总数应为 18 组。

### China Book（8 组 / 9 申报主体）
1. **Hillhouse 高瓴（HHLR + Evergreen 合并）**（⚠️ 必须合并）
   - HHLR Advisors: `0001762304`
   - Evergreen Quality Fund GP Ltd: `0002016939`
2. **First Beijing Investment**: `0001701717`
3. **Aspex Management**: `0001768375`
4. **Greenwoods 景林（香港）**: `0001848138`（别名 `Greenwoods` / `景林` / `Jinglin`，⚠️ 不是 First Beijing `0001701717`）
5. **Sequoia China 红杉中国（HCEP）**: `0001822015`（别名 `Sequoia China` / `红杉中国` / `HCEP`）
6. **Capital Today 今日资本**: `0001625502`
7. **Brilliance Asset Management**: `0001665751`
8. **Yiheng Capital**: `0001447228`

### US Growth（9 组）
9. **Tiger Global Management**: `0001167483`
10. **Coatue Management**: `0001135730`
11. **D1 Capital Partners**: `0001747057`
12. **Altimeter Capital Management**: `0001541617`
13. **Whale Rock Capital Management**: `0001387322`
14. **Dragoneer Investment Group**: `0001602189`
15. **Lone Pine Capital**: `0001061165`
16. **Sequoia US 红杉美国（SCGE）**: `0001607841`
17. **Durable Capital Partners**: `0001798849`

### Value（1 组）
18. **Berkshire Hathaway 伯克希尔·哈撒韦**: `0001067983`

### Hillhouse 合并规则（关键）

HHLR (`0001762304`) 与 Evergreen Quality Fund GP (`0002016939`) 是 Hillhouse 同体系两个 13F 申报主体，**必须按 CUSIP 层面合并**后再生成报告。`compare` 已内置此逻辑：传入 `--institution HHLR` 会自动获取 Evergreen 同一报告期的对比持仓行（JSON 中的 `rows` 字段），按 CUSIP 加总 `Shares`（持股数）/`Value`（持仓市值）/`PrevShares`（上期持股数）/`PrevValue`（上期持仓市值），重算变动、百分比和持仓状态（`Status`，包括 `NEW` 新建仓 / `CLOSED` 清仓 / `INCREASED` 加仓 / `DECREASED` 减仓 / `UNCHANGED` 不变），上期趋势同样合并；输出的 `merged_with` 记录合并了哪个主体及多少行。其他同体系主体用 `--merge-with <CIK或名称>[,...]`；若仅查看 HHLR 单个主体，请加 `--no-merge`。不要自己临时编写合并脚本。

---

## 12) 数据文件与质检脚本清单

| 文件 / 脚本 | 用途 | 维护或运行方式 |
|------|------|---------|
| `scripts/classification.json` | 股票代码 → 三大类别（内置初始只读） | 工作区覆盖层 + `map-propose` |
| `scripts/descriptors.json` | 两级豁免名单（`major_bluechips` 头部蓝筹 / `familiar_names` 常见知名公司）与业务定语库（内置初始只读） | 工作区覆盖层 + `descriptors-add` |
| `scripts/institution_map.json` | 机构别名 → CIK 与 18 组固定顺序（内置初始只读） | 工作区覆盖层 + `map-propose` |
| `scripts/merge_rules.json` | 股份类别（share class）合并（内置初始只读） | 工作区覆盖层 + `map-propose` |
| `scripts/name_to_ticker.json` | 公司名 → SEC 股票代码（内置初始只读） | 工作区覆盖层（运行 `compare` 时更新） |
| `scripts/ticker_aliases.json` | SEC 股票代码 → 分类用股票代码（内置初始只读） | 工作区覆盖层 + `map-propose` |
| `scripts/verified_tickers.json` | 核验台账（已核验标的的类别与规范简称，内置初始只读） | 工作区覆盖层（`<workspace>/state/dalton-13f-tracker/verified_tickers.json`） |
| `scripts/audit_13f_run.py` | 质检脚本（18 组全量季度报告输出前的强制校验脚本） | 只读取本地内置数据与工作区覆盖层（默认不联网；仅显式加 `--lookup-new` 时联网查询 Yahoo Finance） |
| `<workspace>/state/dalton-13f-tracker/auto_learn_log.jsonl` | 自动学习（`auto-learn`）、映射补丁与新增业务定语审计日志 | 自动追加（需传 `--workspace-dir`） |
| `<workspace>/state/dalton-13f-tracker/map_patches.json` | `map-propose` / `map-apply` 补丁记录 | 自动读写（需传 `--workspace-dir`） |

---

## 13) 三类归属边界、简称规范与输出前强制质检（⚠️ 必须执行）

### A) 三类归属标准
- **`China Book`**：全部中概股及中国大陆/香港背景美股 ADR（美国存托凭证），涵盖电商、互联网、教育、消费、金融科技、出行及医药流通等行业（如 `PDD`、`BABA`、`JD`、`NTES`、`TCOM`、`FUTU`、`BEKE`、`YMM`、`HTHT`、`ATAT`、`VIPS`、`EDU`、`TAL`、`NOAH`、`RLX`、`ZTO` 等）。
- **`AI/Semi/US SaaS`**：
  1. AI 芯片、CPU（中央处理器）/GPU（图形处理器）/ASIC（专用集成电路）、晶圆代工、EDA（电子设计自动化）、封测、半导体设备与材料、半导体 ETF（交易所交易基金，如 `NVDA`、`TSM`、`AVGO`、`AMD`、`ASML`、`LRCX`、`AMAT`、`KLAC`、`CBRS`、`ALAB`、`MPWR`、`Q`、`MKSI`、`KLIC`、`SMH` 等）；
  2. HBM（高带宽内存）/DRAM（动态随机存取存储器）/NAND（闪存）与机械硬盘存储（如 `MU`、`SKHY`、`SNDK`、`STX`、`WDC`）；
  3. 光通信、高速连接、AI 服务器电源模块与代工制造（如 `COHR`、`LITE`、`FN`、`AAOI`、`VIAV`、`VICR`、`CLS`、`FLEX`、`TTMI`、`VRT`、`GLW`）；
  4. AI 云/Neocloud（新兴 AI 算力云厂商）、高密度算力数据中心与量子计算（如 `CRWV`、`NBIS`、`EQIX`、`APLD`、`CORZ`、`CIFR`、`HUT`、`WULF`、`BTDR`、`SHAZ`、`QNT`、`XNDU`）；
  5. 美股科技巨头云平台及企业级/基础设施软件与 SaaS（如 `MSFT`、`GOOGL`、`AMZN`、`META`、`AAPL`、`CRM`、`NOW`、`SNOW`、`MDB`、`NET`、`OKTA`、`TWLO`、`SHOP`、`APP`、`U`、`TTD`、`FIG`、`FROG`、`VRSN`、`BILL`、`CCC`、`RNG`、`AMPL`、`EPAM`，以及对应的可转债如 `Unity Software CB`）；
  6. AI 电力基础设施：燃气轮机/现场燃气发电、电网与输配电设备/工程、燃料电池与数据中心备用电源（如 `GEV`、`PWR`、`BE`、`GNRC`、`INIO`）。
- **`Other US Book`**：传统航空发动机与军工制造（如 `GE Aerospace`、`Honeywell`）、通用泵阀与一般工业制造（如 `Flowserve`）、传统受监管公用事业（如 `AEP`、`National Grid`）、小型核电开发商（如 `Oklo`、`NuScale`）、数据中心机电与土建施工（如 `Comfort Systems USA`、`EMCOR`、`Sterling Infrastructure`）、消费互联网/交易平台/出行/支付与金融科技（如 `SpaceX`、`DoorDash`、`Instacart`、`Uber`、`Sea`、`Coupang`、`MercadoLibre`、`Nu Holdings`、`Visa`、`Mastercard`），以及传统消费、医疗器械与金融价值股。

### B) 公司简称完整性与大小写标准
- 规范简称由脚本按 `merge_rules.json` 与内置简称规则自动生成，并由质检脚本校验（仍须遵守 §10，不得手工改写单机构 `formatted_report`）。
- 禁止出现未展开的 SEC 申报缩写（如 `Matls`、`Instrs`、`Pwr`、`Finl`、`Pmts`、`Svcs`、`Mkts`）或被截断的残缺名称（如 `Bank Of`、`D R`、`Kulicke &`、`Trip Com`、`Netease Com`）。
- 品牌大小写须与官方一致（如 `NVIDIA`、`ASML`、`KLA`、`TSMC`、`SK hynix`、`CoreWeave`、`ServiceNow`、`MongoDB`、`UiPath`、`JFrog`、`SiTime`、`SanDisk`、`AppLovin`、`DoorDash`、`MercadoLibre`、`Moody's`）；ETF 须标明具体名称与代码（如 `VanEck Semiconductor ETF (SMH)`）。

### C) 输出前强制质检
完成全部 18 组机构对比后、输出全量季度报告前，**必须运行质检脚本**（默认只读本地文件、不联网）：

```bash
python3 .agents/skills/dalton-13f-tracker/scripts/audit_13f_run.py \
  --data-dir <run_data_dir> --quarter <YYQN> --workspace-dir <workspace> [--report-file <report.md>]
```

- `--data-dir` 目录下须包含按 `<01..18>_<slug>_<yyqn>.json`（`<slug>` 为机构英文短标识，季度后缀小写，如 `26q3`）命名的全部 18 组 `compare --format cn --json` 输出：`01_hillhouse`、`02_first_beijing`、`03_aspex`、`04_greenwoods`、`05_sequoia_china`、`06_capital_today`、`07_brilliance`、`08_yiheng`、`09_tiger_global`、`10_coatue`、`11_d1_capital`、`12_altimeter`、`13_whale_rock`、`14_dragoneer`、`15_lone_pine`、`16_sequoia_us`、`17_durable`、`18_berkshire`。
- 仅当出现 `[UNVERIFIED NEW TICKERS]`（存在未录入核验台账的新增股票代码）且需要查询新标的背景时，再显式追加 `--lookup-new`（该选项会调用 `yfinance` 行情库联网查询 Yahoo Finance 公司信息）。
- **非 0 退出码处理**：若质检脚本返回非 0 退出码，必须先核实公司所在国家与主营业务，通过 `map-propose` / `map-apply` 及 `<workspace>/state/dalton-13f-tracker/verified_tickers.json` 更新工作区覆盖层（不要改动只读的 `scripts/` 目录），然后重新运行 `compare`（必要时重新运行 `render-batch`）与质检脚本，直至输出 `RESULT: PASS`（`Errors: 0`）后方可输出最终报告。常见错误标签包括：
  - 数据与合并：`[PERIOD MISMATCH]`（报告期不一致）、`[INCOMPLETE MERGE]`（合并主体缺少同期数据）、`[MISSING REPORT]`（缺少报告字段）、`[DUPLICATE/UNMERGED ENTITY IN SAME FUND]`（同机构内存在重复或未合并实体）
  - 简称与代码：`[UNVERIFIED NEW TICKERS]`（存在未录入核验台账的新增股票代码）、`[BAD NAME SUFFIX]`（简称残留未剥离后缀）、`[ABBREVIATED NAME]`（简称含未展开缩写）、`[STUB NAME]`（简称过短残缺）
  - 分类与完整报告：`[CROSS-FUND CATEGORY CONFLICT]`（跨机构分类冲突）、`[CATEGORY DRIFT VS VERIFIED LEDGER]`（报告类别偏离核验台账）、`[CATEGORY MISMATCH BY TICKER]`（按代码核对类别不一致），以及传入 `--report-file` 后的报告结构、转义或单位错误
- **告警核查**：若出现 `[CUSIP CASE SPLIT]`（同一发行人出现大小写不同的 CUSIP）等告警，须同步核查是否已正确合并。

---

## 14) 全量季度报告流程与五段式完整报告

全量季度报告输出纯 Markdown（不做 `\_`、`\.`、`\+` 等反斜杠转义），固定五段式结构：
1. `# 13F Tracker_{YYYYQ#}` + `## 核心结论与跨机构动向`（四小节：`AI/Semi/US SaaS`、`China Book`、`Other US Book`、`实盘持仓与重点覆盖标的`）
2. `## 持仓变化中的新线索`
3. `## China Book 机构（8 组 / 9 申报主体）`
4. `## US Growth 机构（9 组）`
5. `## Value 机构（1 组）`

### 执行步骤（按顺序）

1. **生成 18 组对比数据并完成基础质检**：按 §11 与 §13 C 保存全部 18 组 `compare` 输出的 JSON 文件，先运行 `audit_13f_run.py`（不带 `--report-file`）确认分类、合并与简称无误。
2. **生成聚合汇总数据（`aggregate`）**：
   ```bash
   python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
     --workspace-dir <workspace> --json aggregate \
     --data-dir <run_data_dir> --quarter 26Q3 --out <work_dir>/aggregate_26q3.json
   ```
   - **板块共识矩阵**：每只股票的持有机构数（`held_count`）与清仓机构数（`closed_count`）分开统计，附买入/加仓/减仓/不变机构数、期末合计市值与分歧度。
   - **实盘与重点覆盖对账**：系统会将用户的覆盖名单生成并同步到用户 Home 工作区（主工作区）的 `dalton/coverage.yaml`（同一计算机的其他工作区对应路径为 `../Home/dalton/coverage.yaml`）。
     - 查找顺序：`aggregate` 依次查找 `--coverage` → `<workspace>/dalton/coverage.yaml` → `<workspace>/state/dalton-13f-tracker/coverage.yaml` → `<workspace>/state/dalton-13f-tracker/portfolio_coverage.json` → `../Home/dalton/coverage.yaml` → `dalton/coverage.yaml`（**不使用仓库内置静态文件回退**）。
     - 统计规则：名单中实盘持仓（`held: true` 或 `sheet_section: Live Position`）、重点覆盖（`tier: core`）与观察标的（`tier: watch`）均参与对账，按「净增持」「净减持或持平」输出，剔除在 18 组中既无持仓也无清仓记录的 0 持仓标的，金额不设门槛。合法空名单（`companies: []`）或仅包含观察标的时均不算缺失（`coverage_missing: false`）；读取或解析异常返回 `coverage_missing: true`。
     - 缺失处理：若返回 `coverage_missing: true`（例如当前运行在未同步 `Home/dalton/coverage.yaml` 的机器上），须先通过已有工具获取覆盖名单（在研究总监会话调用 `dalton_coverage(action="list")`，或跨机读取 Home 下的 `dalton/coverage.yaml`），将 JSON/YAML 写入工作区文件后，通过 `--coverage <path>` 重新运行 `aggregate`。
   - **新线索候选池**：筛选条件为不属于头部蓝筹名单（`major_bluechips`），新建仓或持股数加仓 ≥20%，且（单家权重 ≥2% 或净增持 ≥1.5 亿美元），且期末持仓 ≥2000 万美元（以上门槛均按原始未舍入精度比较）。其中**净增持 = 新增股数 × 期末单价**（不用市值差），权重分母采用含生物医药在内的 SEC 申报总市值；每条附股数净增倍数、该股票在全部 18 组中的持仓以及同机构大额卖出（清仓前持仓市值 ≥5000 万美元或减持金额 ≥1 亿美元）。**候选池仅供筛选，不是必须全部写入报告的清单**。
3. **撰写前两章**：根据 `aggregate` 输出的聚合数据，自主归纳跨机构主题分组与「减持/清仓 A、买入 B」换仓配对，撰写 `# 13F Tracker_{YYYYQ#}`、`## 核心结论与跨机构动向` 与 `## 持仓变化中的新线索`（存为 `<work_dir>/chapters_1_2.md`），并记录前两章已带业务定语出现的股票代码列表。
4. **补充业务定语并生成后三段（`render-batch` / `descriptors-add`）**：
   ```bash
   python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
     --workspace-dir <workspace> --json render-batch \
     --data-dir <run_data_dir> --quarter 26Q3 \
     --annotated-tickers "BSP,SITM,SMTC,VIAV" \
     --chapters-file <work_dir>/chapters_1_2.md --out <work_dir>/report_26q3.md
   ```
   - **两级豁免名单与业务定语规则**：`descriptors.json` 维护头部蓝筹（`major_bluechips`，约 20 只，既不进新线索候选池，明细也不加业务定语）与常见知名公司（`familiar_names`，100+ 只，明细不加业务定语）；工作区 `<workspace>/state/dalton-13f-tracker/descriptors.json` 支持新增条目，也支持通过 `"remove": [...]`（或 `"remove_major_bluechips"` / `"remove_familiar_names"`）撤销内置项，工作区配置优先。按 1→18 组顺序生成明细时，**业务定语只加在全篇第一个满足「建仓或大幅加仓且期末 ≥500 万美元」的正股条目上**（此前因「仓位不变」或金额 <500 万美元而未触发的条目不加业务定语），之后再出现及前两章 `--annotated-tickers` 已标注的不再重复。
   - 若 `render-batch` 返回 `missing_descriptors`，直接起草 4–24 字中文业务定语（**无需人工确认**），通过 `descriptors-add` 写入工作区覆盖层后重新运行 `render-batch`：
     ```bash
     python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
       --workspace-dir <workspace> --json descriptors-add \
       --quarter 26Q3 --items-json '{"TICKER":"4至24字中文主营定位"}'
     ```
5. **数值核对提醒（仅提醒，不阻断输出）**：
   ```bash
   python3 .agents/skills/dalton-13f-tracker/scripts/13f_skill.py \
     --workspace-dir <workspace> --json verify-numbers \
     --aggregate <work_dir>/aggregate_26q3.json --report-file <work_dir>/report_26q3.md
   ```
   核对前两章带单位数值（亿/万美元、%、倍、家、约数 ±6% 及合计），若 `reminders` 字段非空，按 `aggregate` 输出的原始数值逐项核实修正。
6. **完整报告质检**：运行 `audit_13f_run.py --data-dir <run_data_dir> --quarter 26Q3 --workspace-dir <workspace> --report-file <work_dir>/report_26q3.md`，确认 `RESULT: PASS`。
7. **写入飞书与结尾输出**：若要求写入飞书，使用纯 Markdown 原文写入，写完后必须调用 `feishu_read` 回读全文核对无截断。运行结束输出一行：`本季新增定语 N 条`。
