# 覆盖池维护

### scan_tier 由 retier 算，不手工标

核心池的定义是「值不值得每周看公司级读数」，不是「会不会拆 AI 收入」。`retier` 按五条规则算，命中任一条进核心池：

1. `disclosed_ai_metric` —— 通道一有 A/B 级 AI 分项指标入库
2. `native_purity` —— `ai_purity = native`
3. `has_attribution` —— 有 A/B 级管理层 AI 归因原话
4. `embedded_accel` —— 公司级总收入同比 ≥ 20% 且同比增速较对比期未回落
5. `pinned` —— 行业读数设定者，显式钉在核心池（腾讯、阿里、Google、微软、Meta、亚马逊、苹果、Oracle、百度、Palantir）

命中的规则名写进每家的 `tier_rules` 字段，可审可撤。先跑 `retier` 试算看变动清单，确认后直接修改仓库里的 `references/universe.json`（`retier --apply` 会被拒绝）。第 5 条是唯一的人工例外，写成显式名单而不是散落的手工标注，就是为了让例外可见。

### 加减公司

- `references/universe.json` 每季度复核一次：退市、改名、并购、新上市的 AI 应用公司。
- 新加公司一律先进观察池，由 `retier` 用数据决定是否升核心池，不在加入时手工定档。
- 自动驾驶与具身智能标的归 `humanoid-robot-weekly-monitor`，算力与云基础设施不属 AI 应用层，两类都不入本池。
- 复核时对全池跑一次 yfinance 解析校验，解析失败的先查是否并购退市，再决定移除还是换代码。已移除的记进 `delisted_removed`，避免下次误加回。
- 新上市公司上市不足四个季度的，只进拐点观察，不进增速主表，因为没有可比基数。
- `private_watchlist` 是未上市公司，只做定性追踪，不进筛选表。新变现方式通常先在这里出现，再传导到上市公司。
