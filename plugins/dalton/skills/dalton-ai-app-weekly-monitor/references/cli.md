# 命令与四表看板

下文每条命令前面省略的调用是 `python3 .agents/skills/dalton-ai-app-weekly-monitor/scripts/ai_application_weekly_monitor_cli.py --json --workspace-dir <workspace>`。`state/`、`research-outputs/` 和 `state/ai-app-weekly/temp/` 均相对该工作区。入库前读 [数据库合同](database-contract.md)，门槛复核读 [门槛细则](thresholds.md)。仅需导出或看板时读取对应命令，不启动周更或回填。


```bash
self-test
plan [--as-of YYYY-MM-DD[THH:MM:SS+08:00]] [--days 7]
universe [--track toc_internet|tob_software|ai_for_science] [--market AH|US] [--scan-tier 1|2] [--purity native|embedded|adjacent]
retier             # 只试算 scan_tier。传入 --apply 会拒绝；覆盖池调整改仓库里的 references/universe.json
add-metric --company 美图公司 --ticker 1357.HK --track toc_internet \
  --metric ai_revenue --period 2026Q2 --value 520 --unit CNY_mn --value-usd-mn 72 \
  --revenue-share-pct 28 --definition "影像与设计产品订阅收入，公司口径" \
  --ai-basis analyst_related \
  --evidence-grade A --source-type interim_report --source-url https://... --source-date 2026-08-20 \
  --quote "来源原文里含该数字的那句话，逐字" \
  [--series-id ai_applications] [--primary-series] [--value-is-bound] \
  [--proxy-only --proxy-only-reason "存量订阅重贴标签"] [--allow-duplicate]
  # --ai-basis 四选一：company_ai_line | company_ai_related | analyst_ai_product | analyst_related
  # 财务与比率指标必填。判据是公司口径原文有没有自称 AI，不是业务实质有多 AI。
  # 上例公司披露的是「影像与设计产品订阅收入」，公司没说它是 AI 收入，所以填 analyst_related。
add-fundamental --company Cloudflare --ticker NET --track tob_software \
  --period 2026Q2 --total-revenue-usd-mn 696.1 --revenue-yoy-pct 36 --crpo-yoy-pct 35 \
  [--currency CNY --total-revenue-orig-mn 4942.3] \
  # 非美元列报的公司必填这两个：--total-revenue-usd-mn 是排序口径，原币金额只用于显示与回溯。
  # 只给原币金额和 --currency 时，CLI 按固定汇率当场折出美元值，不让没有美元口径的记录进库。\
  [--includes-ma --ma-note "2026-05-11 完成 Udemy 全股票合并，本期并表约七周" --organic-yoy-pct -1] \
  # 本期同比含重大并表/剥离时必填前两个。--ma-note 是硬要求，下一期判档的人靠它决定基数何时回到同口径。
  # 公司给了 pro-forma / 剔除并表口径才填 --organic-yoy-pct，自己估的不填；不填就进「口径不可比」档。\
  --evidence-grade A --source-type press_release --source-url https://... --source-date 2026-08-06 \
  --quote "业绩新闻稿里含该数字的那句话，逐字" [--nrr-pct 111] [--large-customers 4200]
add-attribution --company Cloudflare --ticker NET --track tob_software \
  --period 2026Q2 --attribution-type demand_driver \
  --quote "管理层把增长归到 AI / agent 上的那句原话，逐字" \
  --evidence-grade A --source-type press_release --source-url https://... --source-date 2026-08-06 \
  [--venue call_qa] [--speaker "Matthew Prince"] [--speaker-role CEO] [--specificity quantified]
  # attribution-type: demand_driver | new_monetization | product_embed | erosion
  # 这条会同步镜像成一条管理层原话，venue / speaker / specificity 决定镜像出来那条长什么样
screen [--tier breakout] [--track tob_software] [--with-price] [--primary-only]
coverage [--track tob_software]
ai-monetization [--by yoy|qoq] [--track tob_software] [--limit 20] [--min-growth 100] [--with-price]
  # 表一 + 表二 AI Monetization as % of Headline。旧名 rank 仍可用
headline-monetization [--tier accelerating|native_accel|high_absolute|...] [--track ...] [--purity native] [--with-price]
  # 表三，输出分档视图 + 绝对增速榜。旧名 screen-embedded 仍可用
usage [--track tob_software]            # 表四，不排名次。旧名 usage-board 仍可用
validate --file state/ai-app-weekly/temp/ai-app-weekly/report-2026-08-17.md
```

底层数据库：

```bash
migrate-db [--apply]          # 旧平铺库导入分片库，可重复跑；不传 --apply 只试算
add-profile --ticker NET --company Cloudflare --track tob_software \
  --purity embedded --route channel_two --route-reason "不单列 AI 分项，靠 agent 流量推高主业用量" \
  --business "全球边缘网络，卖 CDN、安全和 Workers 开发者平台" \
  --ai-role "AI 织进主产品：Workers AI 推理、AI 爬虫与 agent 流量推高主业用量与定价" \
  --ai-products "Workers AI,AI Gateway" [--ai-line-item] [--usage-metric] [--transcript-available] \
  --evidence-url https://... --evidence-date 2026-08-06
  # route: channel_one（走表一/表二）| channel_two（走表三）| usage_only（走表四）| insufficient（数据不足）
profiles [--ticker NET]       # 画像覆盖情况；缺一家，就有一家进不了任何一张表
add-quote --company Cloudflare --ticker NET --track tob_software --period 2026Q2 \
  --venue call_qa --speaker "Matthew Prince" --speaker-role CEO \
  --analyst-question "分析师原问题" --quote "<完整段落，逐字>" \
  --anchor result --anchor workflow --specificity quantified --polarity positive \
  --evidence-grade B --source-url https://... --source-date 2026-08-06 \
  [--attribution-type demand_driver] [--transcript-file transcripts/NET/2026Q2.txt] [--paragraph-index 42]
  # venue: call_qa | call_prepared | press_release | investor_day | filing | interview | other
  # anchor: product | workflow | result | customer | commitment（至少一条，否则拒收）
  # specificity: vague | directional | quantified　polarity: positive | neutral | negative
quotes --ticker NET [--since 2025Q1] [--until 2026Q2] [--venue call_qa] [--polarity negative]
add-transcript --ticker NET --period 2026Q2 --file state/ai-app-weekly/temp/ai-app-weekly/net-q2.txt --source-url https://...
mark-gap --ticker 002153.SZ --period 2025Q1 --data-type quotes --reason no_transcript \
  --checked-source "查过 AlphaEngine 与巨潮，均无该季业绩说明会记录"
  # data-type: fundamentals | metrics | quotes | experts | profile
  # reason: no_transcript | no_expert_coverage | no_chinese_minutes | not_disclosed | no_earnings_call | not_listed | not_reported_yet | source_unavailable
add-expert --company Cloudflare --ticker NET --track tob_software --period 2026Q2 \
  --expert-role channel_partner --expert-background "北美 CDN 分销商技术负责人（不写姓名）" \
  --topic "AI 网关采购节奏" --claim "用自己的话写清主张：谁、哪个业务环节、结论是什么" \
  --verbatim "不超过 20 词的逐字引用" --alignment contradicts --management-ref "2026Q2 CEO quantified positive" \
  --polarity negative --evidence-grade A --source-id GP-xxxxx --source-date 2026-08-20 \
  --source-attribution "提供方给的 source_attribution.markdown 原文"
  # expert-role: former_employee | channel_partner | customer | competitor | supplier | industry_consultant | other
  # alignment: confirms | contradicts | extends | unrelated | no_management_claim
  # 判 contradicts 必须带 --management-ref，否则拒收：无法复核的矛盾没有价值
experts --ticker NET [--since 2025Q1] [--alignment contradicts]
contradictions [--tickers NET,PLTR | --track ... | --all] [--since 2025Q1]
coverage-ledger [--since 2025Q1] [--until 2026Q2] [--track ...] [--write]
backfill-plan [--layer profile|company|expert] [--batch-size 12]
reconcile --expected NET,PLTR | --expected-file state/ai-app-weekly/temp/ai-app-weekly/due-2026-08-21.json [--against 2026-08-14]
snapshot [--write] [--overwrite] [--no-doc] [--as-of YYYY-MM-DD[THH:MM:SS+08:00]]
  # --write 前按 --as-of 的日期检查该期文档。doc.json 里 ok 为 true 且能拼出链接时写入。
  # --no-doc 只给「无变化、不建文档」的短消息路径：该期既没有 doc.json 也没有 doc.failed.json（reason 为 doc_missing）时才写入。正文未完成、记录损坏或已有失败记录时仍拒绝。
diff [--against 2026-08-08] [--min-delta 5] [--limit 20]
export --tickers NET,PLTR [--since 2025Q1] [--until 2026Q2] [--format json|md] [--out state/ai-app-weekly/temp/ai-app-weekly/x.md]
export --track tob_software --format md --out state/ai-app-weekly/temp/ai-app-weekly/tob.md
board [--out research-outputs/ai-application-weekly/board-YYYY-MM-DD.html] [--track ...] [--title ...]
```

所有命令支持 `--json`。失败输出必须包含 `ok: false`、`error` 和 `hint`。

### board：四表可视化

`board` 把表一、表二、表三、表四渲染成**一张自包含 HTML**，不引 CDN、不联网、不新增取数——数据全部来自 `ai-monetization` / `headline-monetization` / `usage` 三个命令的返回值加 `state/ai_app_db/coverage.json`。文件可直接归档、可离线打开、可整份发给别人。

页面五屏，`#p1`–`#p5` 可深链：

- **表一**：主榜 + 四个分流小榜（算不出增速 / 小基数 / 口径存疑 / 代理口径）。增速条按开方压缩，2203% 不会把 20% 压到看不见。
- **表二**：按占比水平排，四档。占比条是线性满槽，柱长等于数字本身，不做开方压缩。
- **表三**：先按分档看增速变化（每家带 `← 上期 ← 去年同期` 增速链和同比序列迷你折线），再单独给绝对增速榜。`暂不入档` 默认折叠。
- **表四**：只分组不排名，四组并列。
- **数据完整度**：序列深度、表一算不出增速的原因、表三只差归因原话的公司名单、表三最新期分布、台账 6960 格热力图。**这一屏跟榜单同等显眼是刻意的**——覆盖率不会到 100%，缺口摆在明面上才不会被当成"没有"。

交互：全局搜公司/代码/口径、按赛道与市场与证据等级筛（四张表同时生效）、点表头排序、点任意一行展开口径与一手件链接、每张表可导出当前视图 CSV。

**金额列分两栏**：「规模 mn USD」「总收入 mn USD」是唯一能比大小的口径，排序按这一列的数值走（表格排序读单元格的 `data-v`，不读文本，避免把 1127.9 CNY 排到 800 USD 前面）；紧跟着的「原币金额」列印原币数字、币种和折算依据（披露原值 / 入库已折算 / 固定汇率折算 @7.1 / 按备注汇率回推），只看不排。「数据完整度」屏的**金额口径覆盖**卡片统计各类折算来源，并点名折不出美元的条目。

写周报时用它自查：**表三「只差一条归因原话」那张名单是每周最高性价比的补数据清单**——这些公司规模和增速都已过闸门，缺的只是 A/B 级举证，补一条就能入档。

