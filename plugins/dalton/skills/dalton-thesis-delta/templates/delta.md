# {TICKER} Thesis 增量笔记｜{YYYY-MM-DD}（`{run_id}`）

**事件结论**：{五词：无变化 / 强化 / 削弱 / 证伪 / 新论点（`NO_CHANGE` / `THESIS_STRENGTHENED` / `THESIS_WEAKENED` / `THESIS_BROKEN` / `NEW_THESIS`）}——{一句话核心判断：本次新证据如何影响哪个支柱或哪条 Debate，置信度如何变化}
**唤醒信号**：`{signal_id}`（`{trigger_kind}`：{命中的预检阈值或事件描述}）

## 一、核心变化与新证据
- **{变化条目 1}**：{事实或观点内容，每个数字带期间、单位与口径}（{来源身份，如“Morgan Stanley 2026-10-18 [引用1]”或“8-K Item 2.02 [引用2]”}）
- **{变化条目 2}**：……

## 二、一致预期与盈利预测修正
- **修正方向**：{收入 / 费用与 Capex / 一次性项目 哪一层发生变化}
- **快照对比**（`data/consensus/{snapshot_file}` [引用3]）：
  - 本财年（`0y`）EPS：{旧值} → {新值}（{vs_last_pct}%，7 天前 {7d_ago}）[引用3]
  - 下一财年（`+1y`）EPS：{旧值} → {新值}（{vs_last_pct}%，7 天前 {7d_ago}；近 7 天上调 {up} 家 / 下调 {down} 家）[引用3]
  - 目标价均值：{旧值} → {新值} 美元（{pt_delta_pct}%）[引用3]

## 三、对 Thesis 支柱与 Debate 的影响
- **受影响的支柱 / Debate**：{如 P2 核心广告变现 / D2（2027 年 Capex 与折旧对 EPS 的拖累）}
- **判断变化**：{旧倾向/状态} → {新倾向/状态}（理由：{具体机制与证据} [引用1]）
- **知识层更新记录**：已调用 `knowledge_update(doc="{thesis|debate|drivers|management}")` 与 `knowledge_append_verdict`

## 四、下一项验证与改判条件
- **下一项验证（`next_validation`）**：{具体观察指标或事件}（预期时间：`{YYYY-MM-DD}`）
- **改判条件**：{什么可观察的数据出现会推翻本轮判断}

## 引用

- **数据备注**：{如有 AlphaEngine 配额用尽、未登录或单一来源待核实线索，在此注明；无则写“全部来源正常读取”}

[引用1]: prov:3f6c9a12-0000-0000-0000-000000000001 （{来源类型｜机构/媒体/Filing｜标题｜日期｜链接或 document_id}）
[引用2]: evidence:E-20261023-01 （{来源类型｜机构/媒体/Filing｜标题｜日期｜accession 或链接}）
[引用3]: evidence:E-20261023-02 （市场数据｜一致预期快照｜`data/consensus/{snapshot_file}`｜{YYYY-MM-DD}）
