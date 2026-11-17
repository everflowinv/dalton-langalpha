# Dalton 自动任务 JSON 模板说明（PK.15 & PS.7 / PS.10 / PS.11 / PS.13）

所有 Dalton 自动任务文件遵循 `src/server/services/automations/file.py` 的可编辑字段契约（`name, description, status, trigger_type, cron_expression, next_run_at, trigger_config, timezone, instruction, workspace_id, thread, llm_model, delivery`），且**不设置 `max_failures`**（Dalton 不加次数上限，只由美元预算、按时间退避 PK.18 和供应商配额告警治理）。

## 命名与预检识别规则（`<t>` = ticker 小写、`.` 换 `-`）

| 模板文件 | 实际落盘文件名 | 触发类型 | 工作区 | 负责模块 | 预检探针（PK.16） |
|---|---|---|---|---|---|
| `dalton-ticker-watch.json` / `dalton-<t>-watch.json` | `dalton-meta-watch.json` | `cron` | 公司工作区 | PK.15 / PS.12 | `sec`, `earnings`, `price`, `indicator`, `pm_question`, `internal` |
| `dalton-ticker-delta.json` / `dalton-<t>-delta.json` | `dalton-meta-delta.json` | `cron` | 公司工作区 | PS.11 | `consensus`, `sec`, `internal`, `price` |
| `dalton-ticker-move-up.json` / `dalton-<t>-move-up.json` | `dalton-meta-move-up.json` | `price` (`pct_change_above`) | 公司工作区 | PS.10 | `price_move` (`ref='<交易日>:price'`) |
| `dalton-ticker-move-down.json` / `dalton-<t>-move-down.json` | `dalton-meta-move-down.json` | `price` (`pct_change_below`) | 公司工作区 | PS.10 | `price_move` (`ref='<交易日>:price'`) |
| `dalton-ticker-earn-period-tm3.json` / `dalton-<t>-earn-<period>-tm3.json` | `dalton-meta-earn-fy26q3-tm3.json` | `once` | 公司工作区 | PS.7 / PS.8 | `earnings` (`ref='FY26Q3:tm3'`, 仅限官方 `confirmed` 日期) |
| `dalton-ticker-earn-period-t0.json` / `dalton-<t>-earn-<period>-t0.json` / `t0.json` | `dalton-meta-earn-fy26q3-t0.json` | `once` | 公司工作区 | PS.7 / PS.9 | `earnings` (`ref='FY26Q3:t0'`) |
| `dalton-ticker-earn-period-tp1.json` / `dalton-<t>-earn-<period>-tp1.json` / `tp1.json` | `dalton-meta-earn-fy26q3-tp1.json` | `once` | 公司工作区 | PS.7 / PS.9 | `earnings` (`ref='FY26Q3:tp1'`) |
| `dalton-earnings-calendar.json` | `dalton-earnings-calendar.json` | `cron` (每周六 09:00 Asia/Shanghai) | `home` | PS.7 | `calendar` |
| `dalton-director-daily.json` | `dalton-director-daily.json` | `cron` (工作日 07:30 Asia/Shanghai) | `home` | PK.19 | `director` |
| `dalton-director-weekly.json` | `dalton-director-weekly.json` | `cron` (每周六 10:00 Asia/Shanghai) | `home` | PS.13 / PK.19 | `director` |

## 写入权限与方式
1. **公司分析师写本工作区**：直接写 `.agents/user/automations/dalton-<t>-*.json`（owner 已授予常设权限，无需弹窗确认）。
2. **Home 研究总监跨工作区建/改自动任务**：一律调用 `dalton_schedule_automation` 工具（对应后端 REST helper `PUT /api/v1/dalton/automations/{name}`），绝不跨工作区直接写文件。
3. **过期清理**：研究总监每日将 14 天前已完成的 `dalton-*-v*` 和 `dalton-*-earn-*` 文件设为 `"status": "deleted"`。
