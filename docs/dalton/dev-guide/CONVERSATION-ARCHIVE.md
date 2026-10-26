# OpenClaw 对话归档与历史回填指南

本文说明 `scripts/dalton/conversation_archive.py` 的日常增量运行、历史大批量回填参数、进度监控、运行锁以及中断后续跑方式。

## 1. 日常增量任务（默认参数）

每日定时任务使用 `src/dalton/dalton.yaml` 下 `conversations` 段的默认配置：
- `extract_concurrency: 1`（单并发，保持每日任务行为不变）
- `hourly_token_budget: 5000000`（10/26 起；此前 120000）

运行命令：

```bash
.venv/bin/python scripts/dalton/conversation_archive.py run
```

## 2. 回填

在 Antigravity（agy）额度充足、需要回填历史对话（例如回填到 `2026-04-30` 约 1 万个片段）时，建议使用以下参数启动：

```bash
.venv/bin/python scripts/dalton/conversation_archive.py backfill \
  --since 2026-04-30 \
  --concurrency 4 \
  --hourly-token-budget 1500000
```

- **建议参数**：`--concurrency 4 --hourly-token-budget 1500000`。
- **每日任务不受影响**：`dalton.yaml` 中默认仍为 `extract_concurrency: 1` 和 `hourly_token_budget: 5000000`，仅在回填命令行显式传入 `--concurrency 4 --hourly-token-budget 1500000` 时提速。
- **并发与限流保护**：
  - 使用 `asyncio.Semaphore` 控制并发抽取数；
  - `HourlyTokenBudgetLimiter` 在并发下先按估算 token 预占预算、完成后按实际消耗校正，避免多个协程同时看到剩余预算而超额；
  - 任一协程遇到瞬时 `429` 退避时，其余并发协程共享暂停窗口，暂停新发请求；遇到 `PermanentQuotaError` 时取消其余在途任务，将已完成连续前缀落盘后退出。
- **逐段有序落盘与进度监控**：
  - 并发抽取的结果按片段原始顺序缓冲，只落盘**同一会话（agent, session_id）内已连续完成的前缀**（10/26 起按会话阻断：某会话的段失败只挡住该会话的后续段，其他会话照常落盘）（最多攒 `N = concurrency` 段一起调用 embedding 并在同一事务内写入 `dalton_conversation_cards` 与 `dalton_conversation_processed`），严格保证 `supersedes` 的时间顺序语义；
  - 每落盘一次即同步更新 `dalton_conversation_runs` 行中的 `segments_processed`、`cards_extracted`、`cards_inserted`、`total_tokens` 等进度列；
  - 每个片段完成时向 `stderr`（无缓冲）输出一行 `INFO` 日志（含 `Segment i/total`、token 数、耗时）。

## 3. 运行锁与被杀后续跑

- **运行锁（Advisory Lock）**：每次运行（非 `--dry-run`）开始时获取 Postgres 会话级锁 `pg_try_advisory_lock(hashtext('dalton_conversation_archive'))`。若拿不到锁，打印 `"已有归档在运行"` 并以退出码 `2` 退出；拿到锁后，会将 `dalton_conversation_runs` 中因前次崩溃残留的 `status='running'` 行自动标记为 `status='abandoned'` 并写入 `error_text`。
- **被杀后如何续跑**：
  1. 进程被 `SIGTERM` / `SIGKILL` 或容器重建中断时，此前已连续完成的片段均已写入 `dalton_conversation_cards` 和 `dalton_conversation_processed`，未完全处理完的 inbox `.jsonl` 文件会保留在 `var/dalton/conversations/inbox/`。
  2. 直接重新执行原回填命令即可续跑：
     ```bash
     .venv/bin/python scripts/dalton/conversation_archive.py backfill \
       --since 2026-04-30 \
       --concurrency 4 \
       --hourly-token-budget 1500000
     ```
     若宿主机导出文件已在 `var/dalton/conversations/inbox/` 中无需重新导出，也可附加 `--skip-host-export` 直接继续消费剩余未处理片段。
  3. 重跑时管线会查询 `dalton_conversation_processed` 自动跳过已落盘片段，仅重做中断时尚未落盘的在途及后续片段，同时依靠 `content_hash` 保证卡片幂等不重复。
