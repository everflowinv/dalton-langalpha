---
name: dalton-bdc-analyst
description: "解析 BDC 10-K/10-Q 投资明细表（Schedule of Investments）并对比两期公允价值/面值比率变化，按恶化程度排序定位信用风险持仓；内置 24Q1–25Q4 种子库，支持增量抓取新季度。"
---

# BDC 投资组合与信用恶化分析（`dalton-bdc-analyst`）

解析 BDC（Business Development Company，业务发展公司）SEC `10-K`/`10-Q` 中的投资明细表（Schedule of Investments，SoI），将持仓数据存入工作区 JSON 库，并对比任意两期的公允价值 / 面值（Fair Value / Face Value）比率变化，按恶化程度排序定位信用风险持仓。

⚠️ **必须使用 `.agents/skills/dalton-bdc-analyst/scripts/` 下的 `harvest.py`、`compare.py` 与 `enrich_company_db.py`，不要自行重写抓取或解析脚本。**
⚠️ **当前内置种子库覆盖 44 家 BDC 的 `24Q1–25Q4` 数据。如需对比 `26Q1` 及之后的新季度，必须先运行 `harvest.py` 抓取目标季度入库，再运行 `compare.py`。**
⚠️ **已验证解析器保护名单见 [`references/VERIFIED_PARSERS.md`](references/VERIFIED_PARSERS.md)，未经明确批准不得改动对应 `scripts/bdc_profiles/{ticker}.py`。**

---

## 1) 工作区状态、内置种子与 SEC 身份

- **内置只读种子（`data/seed.tar.gz`）**：随技能发布 44 家 BDC `24Q1–25Q4` 持仓 JSON 与 `company_db.json`（只读归档，脚本不会修改技能目录）。
- **工作区状态目录（`<workspace>/state/dalton-bdc-analyst/`）**：
  - 所有写入（`data/{TICKER}.json`、`company_db.json`）统一落盘到 `<workspace>/state/dalton-bdc-analyst/`。
  - 首次在某工作区使用时，脚本会从 `data/seed.tar.gz` 解压缺失的种子文件到状态目录，**绝不覆盖已有文件**；读取时以工作区状态目录为准。
  - 脚本执行路径越界校验（若状态目录或目标文件经符号链接解析后超出 `<workspace>`，立即拒绝写入并报错退出）与文件锁（`fcntl.flock` + 原子写，避免并发 `harvest` 同一 BDC 时互相覆盖）。
- **`--workspace-dir` 规则**：
  - `harvest.py` **必须传入 `--workspace-dir <workspace>`**；未传时直接报错退出，不写入任何文件。
  - `compare.py` 对比分析：传入 `--workspace-dir <workspace>` 时会先将缺失的种子文件初始化到工作区状态目录 `<workspace>/state/dalton-bdc-analyst/`，再从工作区状态读取；未传 `--workspace-dir` 时仅在内存中只读读取内置种子 `data/seed.tar.gz`，不写磁盘。
- **SEC 请求身份**：按 `SEC_USER_AGENT` → `EDGAR_IDENTITY` → `DaltonResearch research@everflowinv.com` 的顺序解析，并保持 SEC EDGAR 限速与指数退避重试。
- **依赖**：依赖沙箱预装的 `requests`、`pandas`、`numpy`、`beautifulsoup4`、`lxml`、`tabulate`（如缺包，直接告知管理员，不要擅自修改系统环境）。

---

## 2) 核心用法

### A) Stage 1 — 抓取新季度入库（`harvest.py`，必须传 `--workspace-dir`）

```bash
# 抓取单只 BDC 单个季度
python3 .agents/skills/dalton-bdc-analyst/scripts/harvest.py \
  --workspace-dir <workspace> --ticker ARCC --period 25Q4

# 抓取全部支持 BDC 的指定季度（如新季度 26Q1）
python3 .agents/skills/dalton-bdc-analyst/scripts/harvest.py \
  --workspace-dir <workspace> --ticker all --period 26Q1

# 强制覆盖重抓已存在季度
python3 .agents/skills/dalton-bdc-analyst/scripts/harvest.py \
  --workspace-dir <workspace> --ticker ARCC --period 25Q4 --force

# 查看工作区数据库覆盖状态
python3 .agents/skills/dalton-bdc-analyst/scripts/harvest.py \
  --workspace-dir <workspace> --list
```

### B) Stage 2 — 两期对比分析（`compare.py`，纯本地计算不联网）

```bash
# 使用工作区状态对比单只 BDC 两期持仓（默认输出 Markdown 表格）
python3 .agents/skills/dalton-bdc-analyst/scripts/compare.py \
  --workspace-dir <workspace> --ticker ARCC --periodA 25Q4 --periodB 25Q3

# 未传 --workspace-dir 时只读读取内置 24Q1-25Q4 种子
python3 .agents/skills/dalton-bdc-analyst/scripts/compare.py \
  --ticker ARCC --periodA 25Q4 --periodB 25Q3

# 全市场 44 家对比并导出 CSV / JSON
python3 .agents/skills/dalton-bdc-analyst/scripts/compare.py \
  --workspace-dir <workspace> --ticker all --periodA 25Q4 --periodB 25Q3 --csv temp/bdc_comparison.csv
python3 .agents/skills/dalton-bdc-analyst/scripts/compare.py \
  --workspace-dir <workspace> --ticker ARCC --periodA 25Q4 --periodB 25Q3 --json
```

### 季度格式约定
- 格式：`YYQn`（如 `25Q4`、`25Q3`、`26Q1`），**始终使用日历季度**（Q1=3/31、Q2=6/30、Q3=9/30、Q4=12/31），非财年季度。
- 脚本按 EDGAR `reportDate` 自动精确匹配对应 `10-K` 或 `10-Q`（例如非 12 月财年结束的 CSWC 3 月财年、OCSL 9 月财年会自动匹配对应期间报告）；目标季度尚未申报时（如 9 月财年 BDC 的 9 月季度年报提交较晚），脚本直接跳过并输出 `⏳ 该期 10-Q/10-K 尚未申报，跳过`，不会回退到最近的旧申报写入该季度，也不会因此算作失败。

---

## 3) 支持的 BDC 名单（44 家）

`ARCC, BBDC, BCIC, BCSF, BXSL, CCAP, CGBD, CION, CSWC, FDUS, FSK, GAIN, GBDC, GECC, GLAD, GSBD, HRZN, HTGC, KBDC, MAIN, MFIC, MRCC, MSDL, MSIF, NCDL, NMFC, OBDC, OCSL, OTF, OXSQ, PFLT, PNNT, PSBD, PSEC, RWAY, SAR, SCM, SLRC, SSSS, TCPC, TPVG, TRIN, TSLX, WHF`

不支持：`REFI`（cannabis REIT，无标准 Schedule of Investments）。

SEC 代码表已查不到：`MRCC`、`SSSS`（`harvest.py --ticker all` 默认跳过并在汇总里提示，不视为失败；历史数据保留，`compare.py` 仍可正常读取对比）。

---

## 4) 筛选逻辑与输出规范

### 对比与排序规则
1. **跨期对齐**：通过 `normalize_names.canonical_key` 清洗脚注、DBA/FKA 与融资工具后缀后按规范名内连接（同一公司多笔贷款合并加总面值与公允价值）。
2. **重要性过滤**：剔除期末面值 `< 0.1% × 股东权益（equity_usd）` 的微小持仓。
3. **信用恶化筛选**：计算 `ratio = fair_m / face_m` 与 `ratio_change = ratio_A - ratio_B`；仅保留当期 `ratio_A <= 1.0` 且满足「比率恶化（`ratio_change < 0`）或深度折价（`ratio_A < 0.9`）」的持仓。
4. **排序**：按 `ratio_change` 升序排列（跌幅最剧烈排在最前），每只 BDC 默认取前 20 条（`--top` 可调）。

### 交付格式（Dalton 惯例）
- 回复中直接使用 Markdown 表格呈现对比结果（列顺序：`公司名`、`periodA face value`、`periodA fair value`、`periodA face/fair`、`periodB face`、`periodB fair`、`periodB face/fair`、`期间face/fair变化`、`行业`、`细分行业`、`公司主要业务简介`）。
- 如用户需要导出文件，可用 `--csv` 保存至工作区。
- **仅当用户在本次对话明确要求写入飞书文档时**，调用 `feishu_create_doc(title="BDC 持仓信用恶化对比（{periodA} vs {periodB}）", markdown=...)` 生成文档并返回链接。

---

## 5) 组合公司数据库补全（`company_db.json`）

`company_db.json` 保存组合借款公司的行业、细分行业与一句话中文简介。发现新季度持仓中有未收录公司时：

```bash
# 查看统计与缺失条目
python3 .agents/skills/dalton-bdc-analyst/scripts/enrich_company_db.py \
  --workspace-dir <workspace> --stats
python3 .agents/skills/dalton-bdc-analyst/scripts/enrich_company_db.py \
  --workspace-dir <workspace> --list-missing
python3 .agents/skills/dalton-bdc-analyst/scripts/enrich_company_db.py \
  --workspace-dir <workspace> --batch 20
```

然后由分析师使用 `web_search(query="{COMPANY_NAME} company business industry")` 查询公司主营业务，将补全条目整理成 JSON（`{"COMPANY NAME": {"industry": "...", "sub_industry": "...", "description": "..."}}`），并通过 `enrich_company_db.py --apply`（内部调用带工作区越界检查与文件锁的 `save_db`）安全合并写回 `<workspace>/state/dalton-bdc-analyst/company_db.json`，**不要直接手写该 JSON 文件**，也不得修改技能目录：

```bash
python3 .agents/skills/dalton-bdc-analyst/scripts/enrich_company_db.py \
  --workspace-dir <workspace> --apply temp/company_updates.json
```

---

## 6) 已知覆盖缺口与并购事件（24Q1–25Q4 种子基线）

- **缺失季度（解析格式不兼容）**：`BCIC 24Q1`、`MRCC 24Q1`（0 持仓）；`SLRC 24Q1`（35 列 XBRL 格式仅解析到权益部分）。
- **已知并购（环比规模跳升属正常事件，非解析错误）**：`OTF 25Q1 (+90%)`、`CGBD 25Q1 (+60%)`、`MFIC 25Q1 (+44%)`、`OBDC 25Q1 (+36%)`。
- **待核查异常**：`PFLT 24Q3→24Q4 (+65%)`、`SLRC 24Q4→25Q1 (+53%)`、`GBDC 24Q1→24Q2 (+31%)`。详见 [`references/VERIFIED_PARSERS.md`](references/VERIFIED_PARSERS.md)。
