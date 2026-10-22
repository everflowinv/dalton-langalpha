---
name: dalton-earnings-calendar
description: "逐家核实覆盖名单下一次财报的公司官方披露日期，维护三态台账并为已确认标的自动排程财报前瞻、快讯与次日深度链任务。定期巡检或需要确认覆盖公司财报日期并排定跟踪计划时使用；几分钟内可完成。"
---

# Dalton 财报日历维护（`dalton-earnings-calendar`）

Fund methodology: .agents/skills/dalton-conventions/SKILL.md — read before the first deliverable.
公司认知按 `.agents/skills/dalton-knowledge/SKILL.md` 的读序读；本 skill 在 Home 运行（研究总监），通过 `dalton_agenda_note` 将财报事件同步至各公司 `agenda.watch.events`。

---

## 目标与硬约束

每周六 09:00（Asia/Shanghai，即美东周五收盘后）或按需对 `Home/dalton/coverage.yaml` 覆盖名单逐家核查下一次财报的**公司官方**披露日期，维护 `pending / confirmed / reported` 三态台账（`Home/dalton/earnings-calendar.json`）。

- **只认公司官方来源**：仅公司 IR 新闻稿、IR Events 页面、美股 SEC 8-K/6-K 或港股/A 股交易所法定排期公告（通过 `researchdata.finance(route="cn-hk", operation="filings", ...)` 或披露易 HKEXnews 原文核实的董事会会议召期通告、定期报告预约披露时间表）明确披露未来发布日期时，才标为 `confirmed`（`date_status: confirmed`）并创建财报链自动任务。
- **聚合日历一律标“待确认”**：FMP、yfinance、Nasdaq、Zacks、Earnings Whispers、WSJ、MarketWatch、`get_earnings_calendar` 等给出的日期只记为 `pending`（附 `tentative_date`、`tentative_sources`），`date_status: estimated`，界面与总结显示“待确认”，**在官方公布前绝不建 `tm3/t0/tp1` 自动任务**（若 `tentative_date` 在 10 天内，只建一条 T-5 的 `once` 日期复查任务，仅复查日期不做研究）。详见 [`references/classification.md`](references/classification.md)。
- **跨工作区建任务走 REST helper**：Home 的研究总监不跨工作区直接写自动任务文件，统一调用后端 REST helper `PUT /api/v1/dalton/automations/{name}` 创建或原地改期；分析师在本公司工作区补建时可直接写文件（常设授权）。
- **无工作区不建链**：只为 `workspace.status=active` 的公司创建自动任务链；没有工作区的 Core 公司列入总结“缺工作区”，严禁在 Home 工作区跑出无知识层支撑的前瞻。

---

## 台账格式（`Home/dalton/earnings-calendar.json`）

```json
{
  "updated": "2026-10-18",
  "entries": {
    "META": {
      "name": "Meta Platforms",
      "tier": "Core",
      "workspace_id": "<uuid|null>",
      "fiscal_note": "FY=CY",
      "status": "confirmed",
      "date_status": "confirmed",
      "quarter": "FY26Q3",
      "date": "2026-10-28",
      "timing": "AMC",
      "release_time_et": "16:05",
      "call_time_et": "16:30",
      "confidence": "high",
      "source_url": "<IR 新闻稿链接>",
      "source_type": "company_ir",
      "automations": {
        "tm3": "dalton-meta-earn-fy26q3-tm3.json",
        "t0": "dalton-meta-earn-fy26q3-t0.json",
        "tp1": "dalton-meta-earn-fy26q3-tp1.json"
      },
      "checked_at": "2026-10-18T07:12:00-04:00",
      "history": [
        {
          "at": "2026-10-18T07:12:00-04:00",
          "from": { "date": "2026-10-29" },
          "to": { "date": "2026-10-28" },
          "reason": "公司改期公告"
        }
      ]
    }
  },
  "reported": [
    {
      "ticker": "META",
      "quarter": "FY26Q2",
      "date": "2026-07-29",
      "timing": "AMC",
      "automations": {
        "tm3": "dalton-meta-earn-fy26q2-tm3.json",
        "t0": "dalton-meta-earn-fy26q2-t0.json",
        "tp1": "dalton-meta-earn-fy26q2-tp1.json"
      }
    }
  ]
}
```

---

## 7 步执行流程

### Step 1：预检（Pre-check）

读取覆盖名单 `Home/dalton/coverage.yaml` 与台账 `Home/dalton/earnings-calendar.json`，运行 CLI 核对唯一代码数、分层（Core / Watch）与工作区激活状态：

```bash
python3 .agents/skills/dalton-earnings-calendar/scripts/calendar_state.py --json list
```

只读台账或确认无过期条目不等于完成扫描。

### Step 2：滚动（Roll）

将**业绩发布日**（`date`，不是电话会日）已过的 `confirmed` 条目整条移入 `reported` 数组，并以该公司下一财季重新登记为 `pending`：

```bash
python3 .agents/skills/dalton-earnings-calendar/scripts/calendar_state.py --json roll --today $(date +%F)
```

### Step 3：扫描与落盘（Scan）

按“先 `pending` 后 `confirmed`、Core 优先于 Watch”的顺序逐家核查：

1. **检索词与时间窗**：先算 `SEARCH_START_DATE = 今天 - 180 天`。每家公司执行两条检索：
   - `"{公司}" "to report" "{季度}" results`（限 180 天内）
   - `"{公司}" earnings date announcement`（限 180 天内）
2. **官方源确认**：美股阅读公司 IR 页面（`coverage.yaml` 中的 `earnings.ir_url`）或 SEC 8-K/6-K 原文确认；港股与 A 股调用 `researchdata.finance(route="cn-hk", operation="filings", ticker=CODE_5_OR_6, start=SEARCH_START_DATE, end=TODAY, limit=20)`（港股传 5 位零填充纯数字如 `"00700"`，A 股传 6 位纯数字如 `"600519"`）检索巨潮资讯披露的董事会会议召期通告（Date of Board Meeting）或定期报告预约披露公告原文（港股若 `cn-hk filings` 未覆盖则用 `web_search` 查披露易 `site:hkexnews.hk` 原文链接确认）。判定标准严格执行 [`references/classification.md`](references/classification.md)。
3. **逐家追加 JSONL**：每查完一家，立即用 `json.dumps` 追加一行到 `Home/dalton/research/earnings-scan-<YYYY-MM-DD>.jsonl`（禁止手写字符串拼接）。字段固定为：
   `ticker, name, status, earnings_date, timing, quarter_label, confidence, source_url, evidence, notes`
   - 工具报错或超时写 `"status": "scan_failed"`，**严禁写成 `"not_announced"`**。
   - 仅有聚合站/FMP/yfinance 日期时，写 `"status": "pending"`，在 `earnings_date` 记录候选日期，`confidence: "estimated"`，并在 `notes` 注明候选来源。
4. **大名单并行分片**：当待扫名单 > 30 家时，可用 workflow 内联脚本按 tier 分 4 组 `parallel` 扫描（每组派一个 `dalton-evidence` 子智能体，各写分片 jsonl），主智能体收齐后进入合并。

### Step 4：合并与容错解析（Merge）

逐行调用 `json.loads`（每行独立 `try/except`，单行损坏不得影响其余行）：

- **坏行补救**：若能从损坏行原文辨认出 `ticker` 与日期，当场重新检索核实该公司后进入 Step 5；无法辨认的按 `not_announced` 处理。
- **强制上报**：所有坏行无论是否补救成功，都必须原样列入 Step 7 总结的“**解析失败行**”，禁止静默丢弃。

### Step 5：逐家处理（不批量：Check → 建/改自动任务 → Mark → 同步议程）

对每家公司逐一闭环处理，做完一家再做下一家：

1. **状态检查**：
   ```bash
   python3 .agents/skills/dalton-earnings-calendar/scripts/calendar_state.py --json check --ticker META --date 2026-10-28
   ```
   - `recorded: true` 且日期未变 → 跳过建任务，仅确认议程已同步。
   - `date_changed: true`（已确认日期发生变更）：
     - 若三条 `once` 自动任务尚未执行，调 `PUT /api/v1/dalton/automations/{name}` **原地修改**它们的 `next_run_at`（严禁先删后建导致空窗），并在台账 `history` 记一条改期原因；
     - 若某条 `once` 已经执行完毕（`file.py` 禁止修改已完成的 `once` 任务），只为尚未执行的剩余窗口写入新自动任务文件。
2. **计算触发时间（官方 `confirmed` 且 `workspace.status=active`）**：
   - **前瞻（`tm3`）**：用 `trading_days.py` 计算发布日前 3 个交易日，设为当天市场时区 `08:00`（带 UTC 偏移）：
     ```bash
     python3 .agents/skills/dalton-earnings-calendar/scripts/trading_days.py --date 2026-10-28 --minus 3 --exchange XNYS --at 08:00 --timezone America/New_York --json
     ```
   - **快讯（`t0`）**：
     - `AMC`：`release_time_et + N`（N 默认 `2.5` 小时，例如 `16:05` → `18:35` ET）；
     - `BMO`：`release_time_et + N`（N 默认 `2.0` 小时）；
     - `unknown`：发布日当地市场时区 `20:00`。
     - N 可按 `coverage.yaml` 公司级配置覆盖。
   - **深度链（`tp1`）**：用 `trading_days.py` 计算发布日后 1 个交易日，设为当天市场时区 `20:00`（带 UTC 偏移）：
     ```bash
     python3 .agents/skills/dalton-earnings-calendar/scripts/trading_days.py --date 2026-10-28 --plus 1 --exchange XNYS --at 20:00 --timezone America/New_York --json
     ```
   - **按 Tier 建链**：
     - `Core`（`workspace.status=active`）：建 `["tm3", "t0", "tp1"]` 三条；若无工作区，不建任务，记入“缺工作区”。
     - `Watch`（`workspace.status=active`）：默认只建 `["t0"]` 快讯一条（`chain_by_tier: {"core": ["tm3","t0","tp1"], "watch": ["t0"]}`）。
3. **调用 REST Helper 写入自动任务**：
   向 `PUT /api/v1/dalton/automations/{name}` 发送 JSON 请求体（模板见下节；`<t>` 为小写 ticker、`.` 换 `-`，`<period>` 为小写财季如 `fy26q3`）。
4. **候选日期（`pending` + `tentative_date`）处理**：
   - 不建 `tm3/t0/tp1`；
   - 若 `tentative_date` 距今天 ≤ 10 天，经 REST helper 建一条 T-5 的 `once` 复查任务 `dalton-<t>-earn-<period>-recheck.json`（仅复查官方是否公布日期，不做前瞻研究）。
5. **更新台账（Mark）**：
   ```bash
   python3 .agents/skills/dalton-earnings-calendar/scripts/calendar_state.py --json mark \
     --ticker META --status confirmed --quarter FY26Q3 --date 2026-10-28 \
     --timing AMC --release-time-et 16:05 --call-time-et 16:30 \
     --source-url "https://investor.atmeta.com/..." --source-type company_ir \
     --tm3 dalton-meta-earn-fy26q3-tm3.json \
     --t0 dalton-meta-earn-fy26q3-t0.json \
     --tp1 dalton-meta-earn-fy26q3-tp1.json
   ```
6. **同步公司议程（`dalton_agenda_note`）**：
   调用 `dalton_agenda_note` 将事件写入该公司 `agenda.watch.events`：
   - 官方确认：写 `date`、`timing`、`date_status="confirmed"`、`source_url`、三条自动任务文件名；
   - 候选待确认：写 `tentative_date`、`date_status="estimated"`（显示“待确认”），不带自动任务文件名；
   - 改期：更新同一事件并保留改期原因。

### Step 6：强制对账（Reconciliation，不过不算完成）

严格按 [`references/reconciliation.md`](references/reconciliation.md) 执行两层核验：

1. **扫描文件 vs 台账对账**：
   ```bash
   python3 .agents/skills/dalton-earnings-calendar/scripts/calendar_state.py --json diff \
     --scan Home/dalton/research/earnings-scan-$(date +%F).jsonl
   ```
   检查 `missing` 与 `date_mismatch`。
2. **自动任务文件读回核验**：
   逐个读回三条自动任务文件的 `state.next_run_at`（或顶层 `next_run_at`），确认：
   - `status == "active"`；
   - 带时区偏移的时间先转换到公司所在市场时区（如 `America/New_York`）再比对日期与时点，**严禁直接截取字符串前 10 位**；
   - 若读回不可用，只能报告“本地已记录”，严禁报告“对账通过”；
   - 对账未通过的公司及原因逐条进入总结第 2 项“**未落库**”。

### Step 7：按固定顺序输出总结

总结必须严格按以下 6 项顺序输出（第 2、3 项永远出现；仅当第 2、3 项均为“无”且其余无异常时可简短收尾）：

1. **新确认**：公司 / 财季 / 发布日期 / BMO·AMC（含发布与电话会时间）/ 官方来源链接
2. **未落库**：对账未通过的公司 + 具体原因（没有写“无”）
3. **解析失败行**：Step 4 捕获的 JSONL 坏行清单（没有写“无”）
4. **改期**：公司 / 旧日期 → 新日期 / 改期原因 / 自动任务原地更新状态
5. **本周已发布、仍待官宣与缺工作区**：本轮 `roll` 移入 `reported` 的公司、仍处于 `pending` 的数量（附带 `date_status: estimated`“待确认”候选日期的公司列表）、缺工作区的 Core 公司
6. **扫描失败的公司**：`status: "scan_failed"` 的公司及失败原因（没有写“无”）

---

## 自动任务 JSON 模板（遵守 PK.15）

### 1. 每周日历维护（`dalton-earnings-calendar.json`，Home 工作区）

```json
{
  "name": "财报日历每周维护",
  "status": "active",
  "trigger_type": "cron",
  "cron_expression": "0 9 * * 6",
  "timezone": "Asia/Shanghai",
  "instruction": "/dalton-earnings-calendar 全名单 —— 按 skill 流程滚动、扫描（只认官方来源，候选日期标待确认）、经 PUT /api/v1/dalton/automations/{name} 建/改 tm3/t0/tp1 自动任务、用 dalton_agenda_note 同步各公司 watch.events、对账，最后按固定顺序总结。",
  "workspace_id": "home",
  "thread": "new"
}
```

### 2. T-3 财报前瞻（`dalton-<t>-earn-<period>-tm3.json`）

```json
{
  "name": "META FY26Q3 财报前瞻（T-3）",
  "status": "active",
  "trigger_type": "once",
  "next_run_at": "2026-10-23T08:00:00-04:00",
  "timezone": "America/New_York",
  "instruction": "/dalton-earnings-preview META FY26Q3 发布日 2026-10-28 AMC（来源：<IR 链接>）。先读 .agents/skills/dalton-earnings-preview/SKILL.md；收尾按 dalton-agenda 三步。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new"
}
```

### 3. T0 财报快讯（`dalton-<t>-earn-<period>-t0.json`）

```json
{
  "name": "META FY26Q3 财报快讯（T0）",
  "status": "active",
  "trigger_type": "once",
  "next_run_at": "2026-10-28T18:35:00-04:00",
  "timezone": "America/New_York",
  "instruction": "/dalton-earnings-flash META FY26Q3 发布时间 2026-10-28 16:05 ET。先读 .agents/skills/dalton-earnings-flash/SKILL.md。前瞻稿与 predictions.json 在 drafts/、research/ 下最新的 preview-FY26Q3。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new",
  "delivery": ["<PM 的通知渠道，按工作区配置>"]
}
```

### 4. T+1 财报深度链（`dalton-<t>-earn-<period>-tp1.json`）

```json
{
  "name": "META FY26Q3 财报深度链（T+1）",
  "status": "active",
  "trigger_type": "once",
  "next_run_at": "2026-10-29T20:00:00-04:00",
  "timezone": "America/New_York",
  "instruction": "/dalton-earnings-deep META FY26Q3 发布日 2026-10-28。跑 earnings-deep-update workflow（模型：data/models.json 登记的当前版本；前瞻预测：research/ 下 preview-FY26Q3/predictions.json），结束后按 knowledge-revision.json 写知识层。",
  "workspace_id": "<META 工作区 id>",
  "thread": "new",
  "delivery": ["<PM 的通知渠道>"]
}
```
