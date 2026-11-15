# Richard 覆盖周报 — 定时自动化指令参考

- **建议触发时间**：每周日 `09:00`（`Asia/Shanghai` 北京时间，cron（定时表达式）：`0 9 * * 0`）
- **report_profile（简报配置名）**：`richard-coverage-weekly`
- **工作流模式**：定时自动化无人值守模式（按 `dalton-richard-coverage-weekly` 技能执行，全流程连续执行至完成或明确失败为止，跳过向用户反问确认）

---

## 自动化任务 Prompt（提示词）模板

按 `dalton-richard-coverage-weekly` 技能执行本周「Richard 覆盖周报（Core 核心覆盖逐家深查 + Watch 观察名单异动筛选）」：

1. **确定时间窗口（`window` 子命令）**
   - 执行 `window` 子命令，由美股基准指数 `SPY`（标普 500 指数 ETF）实际日线（收盘 K 线）与美东 `16:00 America/New_York` 收盘时间确定最近已完成交易日，自动排除周末与节假日。
   - **若返回 `ok: false`（停止分支）**：禁止猜测日期，立刻停止后续步骤；使用 `report_run_id="richard-coverage-weekly-failed-window-<当前 UTC（协调世界时）时间，格式 YYYYMMDDTHHMM>"`、`material="yes"` 与 `failure_text` 提交**失败说明**后结束。
   - **若返回 `ok: true`**：记下**周报正文**的标识 `report_run_id="richard-coverage-weekly-<end>"`、**失败说明**的标识 `failure_report_run_id`，以及运行目录 `state/richard-coverage-weekly/runs/<end>/`。

2. **全量行情筛选（`screen_movers.py`）**
   - 对全部 105 家公司（Core 核心覆盖 27 家 + Watch 观察名单 78 家）运行 `screen_movers.py`。
   - **若返回 `ok: false`（停止分支）**：可重跑 `screen_movers.py` 确认 1 次；若仍未补全美股完整收盘 K 线（返回 `ok: false`），立刻停止后续检索与建档，改用 `window` 返回的 `failure_report_run_id`，传 `material="yes"` 与 `failure_text` 提交**失败说明**后结束。

3. **名单筛选与多源检索**
   - 运行 `targets` 与 `sales_notes_search.py`，完成 27 家 Core、触发异动阈值的 Watch（命中 Watch）及持仓 Watch（Live Position 当前实盘持仓中的 EchoStar、D.R. Horton、唯品会）的内部库检索。
   - 按技能要求顺序依次调用 AlphaEngine（外部研报与新闻数据库）、`web_search`（网页搜索）、`community`（社交平台接口：`x-timeline` X 平台检索与 `xueqiu` 雪球）获取数据。
   - 若 AlphaEngine 达到日内额度上限（退出码 `3`），立即停止后续公司的 AlphaEngine 检索（不再重试 AlphaEngine），保留已完成公司的缓存，继续完成其余来源与成稿，并将未查公司完整记入飞书文档「口径与缺口」（雪球须写明近期帖子抽样范围，调用失败或无返回严禁写成「无讨论」）。

4. **飞书文档生成、防重校验与断点续跑**
   - 生成完整周报并写入 `draft.md`，先运行 `doc-status` 检查单期防重状态（防止同周重复建档）：
     - 若返回 `reason == "create_failed_twice"` 或 `"doc_record_unreadable"`：**立即停止建档**，不得再调用 `feishu_create_doc`，改按第 5 步提交**失败说明**。
     - 仅当 `create: true` 时：记录 `attempt_id`，按 `---` 切分为 `total_parts` 段并保存为 `part_<N>.md`，调用 `feishu_create_doc` 写入飞书 Eve 文件夹（`folder_token="SwkOf7AlFlCuQ5dODCGcY8Wyn1c"`）。无论成功、部分失败（已带 `document_id`/`url`）还是完全失败，都必须将原始返回写入 `feishu-return.json`，并立即传 `--total-parts <N>` 与 `--attempt-id <attempt_id>` 给 `save-doc` 写入本地记录。若首次无链接失败且返回 `create: true`，可再调用 `doc-status` 获取新 `attempt_id` 重试 1 次；累计两次失败则停止建档并按第 5 步提交**失败说明**。
     - 若 `body_incomplete: true` 且 `unverified_parts`（待核查段落）非空（含第 1 段部分失败）或 `progress_missing: true`：严禁重复建档或直接补写，先用 `feishu_read(..., max_chars=50000)` 读回完整 JSON（结构化数据格式）保存至 `feishu-read.json`，再运行 `verify-append` 对规范化整段内容校验：
       - `present: true`：脚本已标记完成，跳过 `feishu_append`，继续下一段。
       - `present: false` 且 `should_append: true`（未截断且确认未写入）：调用 `feishu_append` 写入该段，并立即运行 `record-append` 写入本地记录。
       - `ok: false` 且 `indeterminate: true`（状态为「无法确定」，例如读回被截断且找不到或仅部分匹配）：**严禁再调用 `feishu_append` 或整段重新追加，立即停止后续建档与追加**；保留已有文档链接，并在第 5 步**周报正文**或**失败说明**的「未覆盖与待核查清单」中写明需人工核对该文档。
     - 若 `body_incomplete: true` 且仅 `pending_parts` 非空（无待核查段落且 `progress_missing: false`）：严禁重复建档，直接按顺序对 `pending_parts` 调用 `feishu_append` 与 `record-append` 完成其余段落追加。
   - 仅当确认 `body_incomplete == false` 并经 `feishu_read(..., max_chars=50000)` 回读核对通过后，才算完整交付。

5. **提交周报正文或失败说明（`automation_report`）**
   - **正常交付周报正文**：调用 `automation_report`（`action="submit"`、`report_run_id="richard-coverage-weekly-<end>"`、`material="yes"`、`text`），正文依次包含飞书文档标题与链接、3 句以内本周核心要点、**未覆盖与待核查清单**。接收方由自动化配置自动路由，正文与参数中均省略任何接收方标识。
   - **阻塞失败上报失败说明**：若任一步骤阻塞导致周报正文无法交付，改用 `window` 返回的 `failure_report_run_id`（`window` 本身失败时用第 1 步的 `richard-coverage-weekly-failed-window-...`），**严禁使用周报正文的标识**；必须同时传 `material="yes"` 与 `failure_text` 写明真实报错与阻塞步骤（未传 `material="yes"` 会触发 `invalid_material` 报错，也禁止改写成「无更新」）。
   - **送达确认与重提**：提交后核实返回值同时满足 `ok == true`、`delivery_confirmed == true`、`status == "sent"`、`content_kind == "text"`（提交失败说明时按实际返回字段核对送达确认）且 `report_run_id` 与本次提交一致。若未确认送达，先用同一个 `report_run_id` 调用 `action="status"` 查询；仍未确认送达时，用**同一个 `report_run_id` 和同一份正文（`text` 或 `failure_text`）**重提 1 次。

6. **撰写硬规则**
   - 全文撰写、摘要、简报与回复严格遵守 `dalton-richard-coverage-weekly` 文末的「撰写硬规则」。
