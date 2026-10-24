# 财报日历对账与时区核验规则（`reconciliation.md`）

在 `dalton-earnings-calendar` 的 **Step 6** 或排查“扫描成功但自动任务未建/改期未生效”时必须执行本规程。**对账不通过不得报告任务完成。**

---

## 一、两层对账流程

### 第一层：扫描记录 vs 本地台账比对

运行 `calendar_state.py diff` 比对当期扫描 JSONL 文件与 `Home/dalton/earnings-calendar.json`：

```bash
python3 .agents/skills/dalton-earnings-calendar/scripts/calendar_state.py --json diff \
  --scan Home/dalton/research/earnings-scan-<YYYY-MM-DD>.jsonl
```

检查输出的四项核心指标：
1. `missing`：扫描结果为 `confirmed`（且发布日 `>= today`），但台账 `entries` 中未记录或状态不是 `confirmed`，或 `workspace.status=active` 却缺少对应的 `automations` 文件名。
2. `date_mismatch`：扫描确认的 `earnings_date` 与台账 `entries[ticker].date` 不一致（说明改期未落库）。
3. `bad_lines`：JSONL 文件中解析失败的损坏行（必须全部进入总结第 3 项“**解析失败行**”）。
4. `scan_failed`：扫描阶段工具报错或超时的公司（必须全部进入总结第 6 项“**扫描失败的公司**”）。

### 第二层：自动任务文件逐个读回核验

本地台账 `check` 返回 `recorded: true` 只证明台账 JSON 已写，**不能证明 LangAlpha 自动任务真实存在且时间正确**。必须逐个读回各公司工作区的自动任务文件（挂载路径或经接口读回），对每条 `confirmed` 且 `workspace.status=active` 的公司核验：

1. **应建任务集合**：
   - `Core` 公司：`tm3`、`t0`、`tp1` 三条文件均存在；
   - `Watch` 公司：`t0` 文件存在。
2. **状态与归属**：
   - 文件顶层 `status == "active"`（对于尚未触发的任务）；
   - `workspace_id` 等于该公司工作区 ID（严禁误写为 `"home"`）；
   - `trigger_type == "once"`。
3. **触发时间（`state.next_run_at` 或 `next_run_at`）**：
   - `tm3` 转换到市场时区后，日期等于 `trading_days(date, minus=3)`，时间为 `08:00`；
   - `t0` 转换到市场时区后，日期等于 `date`，时间符合 `release_time + N`（AMC 默认 +2.5h，BMO 默认 +2.0h，unknown 默认 20:00）；
   - `tp1` 转换到市场时区后，日期等于 `trading_days(date, plus=1)`，时间为 `20:00`。

> **硬性披露要求**：若因环境或权限无法读回自动任务文件，总结中只能写“**本地台账已记录，自动任务文件未能读回核验**”，严禁报告“对账通过”。

---

## 二、时区解析与夏令时（DST）硬规则

1. **禁止截取字符串前 10 位比日期**：
   - 后端或 `state.next_run_at` 可能以 UTC（`Z` 或 `+00:00`）或其他时区返回同一时刻。例如美股 `2026-10-29T20:00:00-04:00`（`tp1`）若以 UTC 表示则为 `2026-10-30T00:00:00Z`，直接截取前 10 位会误判为 `2026-10-30` 日期不符。
   - **正确做法**：先用 `datetime.fromisoformat()` 解析带偏移的时间戳，再用 `ZoneInfo("America/New_York")`（或公司所在市场时区）转换后提取 `.date()` 与 `.time()` 进行比对。
2. **无偏移时间的处理**：
   - 若读回的 `next_run_at` 不含 UTC offset（naive datetime），仅当文件内有明确的 `timezone` 字段（如 `"America/New_York"`）时，才按该时区解释；
   - 若既无 offset 又无 `timezone` 字段，一律标为“**待人工核对（缺时区信息）**”，严禁猜测本机时区。
3. **美东夏令时切换（11 月第一个周日 / 3 月第二个周日）**：
   - 美股 Q3 财报季（10 月下旬至 11 月上旬）恰逢美东夏令时切换（EDT `-04:00` → EST `-05:00`）。
   - 同一家公司的 `tm3`（切换前，`-04:00`）与 `t0` / `tp1`（切换后，`-05:00`）可能携带不同的 UTC 偏移。生成与对账时必须按各自目标日期在 `America/New_York` 下的真实偏移计算，禁止全年硬编码 `-04:00` 或 `-05:00`。

---

## 三、差异定性与失败恢复

发现 `missing`、`date_mismatch` 或自动任务回读不符时：

1. **先定性原因**：区分是（a）自动任务已建但台账漏 `mark`、（b）UTC 与市场时区的显示差异、（c）改期时未原地更新 `next_run_at`、还是（d）REST helper 调用失败未落盘。
2. **最小范围修复**：仅对确认存在缺口的公司重新执行 Step 5（`PUT /api/v1/dalton/automations/{name}` + `calendar_state.py mark` + `dalton_watch_event`）。严禁因时区显示差异盲目删除已有自动任务。
3. **如实汇总未落库项**：修复后再次运行 `diff` 与读回核验；仍未通过的公司逐一写入 Step 7 总结的第 2 项“**未落库**”，附上具体失败原因。
