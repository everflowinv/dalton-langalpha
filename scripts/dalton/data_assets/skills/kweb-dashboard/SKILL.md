---
name: "kweb-dashboard"
description: "生成相对强弱看板（中概 / 美股金融 / ETF）并更新虚拟组合，统一在 Everflow 监控面板（relative-strength）展示。"
---

# kweb-dashboard（相对强弱看板）

本技能生成自包含 HTML 看板 `research-outputs/kweb-dashboard/index.html` 与虚拟组合日结报告 `research-outputs/kweb-dashboard/portfolio/*.md`，统一在 Everflow 监控面板（slug: `relative-strength`）展示。

## 常用命令

- **构建并推进虚拟组合（推荐定时任务与手动发布使用）**：

```bash
bash skills/kweb-dashboard/scripts/run.sh deploy --no-upload --json
```

- **仅重算并生成 HTML（不推进虚拟组合）**：

```bash
bash skills/kweb-dashboard/scripts/run.sh build --json
```

- **离线自检**：

```bash
bash skills/kweb-dashboard/scripts/run.sh self-test --json
```

- **刷新成分或 ETF AUM 快照**：

```bash
bash skills/kweb-dashboard/scripts/run.sh refresh-constituents --json
bash skills/kweb-dashboard/scripts/run.sh refresh-etf-aum --file <path.xlsx> --as-of YYYY-MM-DD --json
```

## 注意事项

- 当页面其余部分完整、仅 ETF 看板取数失败时，`deploy --no-upload --json` 会照常生成页面并以 `ok: true`（退出码 `0`）完成以便监控面板更新，同时在 ETF 区块顶部显示“ETF 数据未更新（上次成功：日期，原因）”提示并在输出 JSON 中写入 `etf_error`（不依赖任务通知）。
- 当主体取数出现 `warnings` 或过长图注导致页面不完整时，仍会阻止发布并返回失败；确认无误后可人工追加 `--allow-warnings` 重试。
