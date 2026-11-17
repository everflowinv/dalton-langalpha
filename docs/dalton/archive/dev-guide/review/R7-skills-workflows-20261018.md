# R7（其余部分）skill、workflow、覆盖名单与迁移脚本 审查结果

审查人：ZCode 审查智能体  日期：2026-10-18  代码版本：20232e5f（源码与 7670a03f 相同）

承接 R7-financial-lenses-20261018.md，本篇覆盖其余部分：`plugins/dalton/**`（除 dalton-financial-lenses 的内容审查；其注册仍在本文范围）、`agents/*.md`、6 个 `workflows/*/workflow.js`、`docs/dalton/dev-guide/automation-templates/*`、`config/dalton/coverage.yaml` + `scripts/dalton/build_coverage.py`、`scripts/dalton/{init_company_workspace.py, migrate_legacy_knowledge.py}`。只读审查；离线验证包括：workflow 引擎 `compile_check` 全量编译、`_Entry` 自动任务模型对 19 个模板逐一校验、`build_coverage.py --check`、init/migration 脚本两次运行的幂等对比（输出仅写 /tmp）。

## 结论

骨架层（workflow 与引擎接口、skill 注册、覆盖名单、初始化脚本的幂等与只读性）质量高：6 个 workflow 全部通过真实引擎编译，派发数、schema、agentType、失败处理全部合规；22 个 skill 描述逐字一致、无重名，注册挂钩带 DALTON-HOOK 标注；覆盖名单 Core 27 / Watch 78 与 `--check` 全对；初始化脚本实测幂等且保护分析师手改文件；迁移脚本对来源的只读保护（`mode=ro&immutable=1`、前后 SHA-256 校验）做得扎实。

skill 内容层整体**扎实而非模板化**：bullbear v4.2、deep dive 12 问四支线、财报前瞻三优先级、快讯 14 项质量门、异动六步、周会归因公式、conviction call 六门禁、方法纪律三要点全部落实，且反编造措辞（`[假设]` 标注、"本轮未取得"、严禁心算/估算）贯穿。

主要问题集中在三类：**① 迁移脚本把编造数字写进知识层**（四家公司一律 65/35 的"EPS vs 倍数"分解，严重）；**② 研究总监 skill 把设计已删除的"每日最多 3 家"次数门写了回来**（高，违反硬原则）；**③ 成片的 OpenClaw 工具名残留**（topic-research 等 6 个文件引用不存在的函数名，高）。另有若干文档-接口不一致（RunWorkflow `args=`/`params=`、dalton_sec 工具名、dalton-agenda 的自动任务示例 JSON 用了 4 个不存在的字段 + feishu 渠道）。

## 问题（按严重程度排序）

| # | 严重程度 | 文件:行号 | 问题 | 出错场景 / 复现 | 建议修改 |
|---|---|---|---|---|---|
| 1 | 严重 | `scripts/dalton/migrate_legacy_knowledge.py:980-986` | 四家公司的 `data/drivers.json` 一律写入编造的 `historical_valuation_driver.eps_share_pct=65.0 / multiple_share_pct=35.0`（"2025-2026 股价 EPS/倍数驱动分解"），且 `model_ref` 指向并不含该数据的 `sources/legacy/openclaw_thesis_2026-10-15.md` | 已离线跑迁移验证：META、MSFT 等产物中均为同一组 65/35，无任何来源计算。drivers.json 是 agent 读的公司认知底座；initial-coverage D 支线的估值分解、weekly attribution 若引用它，会把假分解当真数据带进正式成果，违反"无编造数字进正式成果" | `eps_share_pct`/`multiple_share_pct` 写 `null`，summary 写"迁移未计算，待首次 EPS vs 倍数归因"；或删掉这两个字段只留 summary |
| 2 | 高 | `plugins/dalton/skills/dalton-research-director/SKILL.md:42-46` | Step 3 写死次数门："每日最多委派 **3 家**（`max_delegations_per_day = 3`）""重型 workflow 每日最多触发 **1 个**"。PK v0.3 修订记录明确"去掉所有'次数'类限制（例如研究总监'一次最多派 3 个'改为按额度和 Redis 槽位自然排队）"，PK.19 并发节再次确认"并发不另设 Dalton 的个数上限……总量由当日美元额度约束"。自动任务模板 `dalton-director-daily.json` 的 instruction 也只写"在今日额度内排程"——skill 文案与设计、模板三方矛盾 | 财报季一天 4 家 Core 同时出事件：按本 skill 第 4 家起拒绝委派（即便日额度充足），研究停摆；违反硬原则"限制只有日美元额度+可选 task cap（不许次数门）" | 删除 3 家/1 个计数上限，改为"受当日美元额度约束，并发由 reserve() 槽位自然排队，拿不到槽位的列入当日计划稍后派"；`S_i≥45` 门槛可保留为优先级排序规则 |
| 3 | 高 | `dalton-topic-research/SKILL.md:53-65`；`…/references/clusters-investment.md:16,31`；`dalton-earnings-preview/SKILL.md:78`、`…/references/retrieval.md:12,150`；`dalton-stock-move/SKILL.md:98`；`dalton-full-coverage/references/branch-c-primary.md:22` | OpenClaw 运行时工具名残留（PS 2.5 明确要求剥掉）：`research_sec_filings`、`research_earnings_transcripts`、`research_alphaengine`、`research_internal`、`guidepoint__search_events`、`alphaengine__download_document`、`x_search`、`firecrawl__firecrawl_scrape`。真实接口是 `researchdata.research(route=…, operation=…)`；X 检索实际是 `x_api.search_posts`；仓库无 firecrawl MCP | 每次跑 `/topic-research` Phase 0.5/2、preview 的 X 轮、stock-move 第 6 步、full-coverage C 支线降级路径，agent 按文档调用的函数名不存在，首轮必报错后自行摸索（浪费轮次、可能漏检）；`retrieval.md:12` 还把 `x_search` 列为 researchdata 的合法 route | 全部替换为 `researchdata.research(route="sec"/"transcript"/"alphaengine"/"guidepoint"/"sales-notes"/"tmtb"/"funda-ai", …)`、`x_api.search_posts`、`web_fetch`；C 支线的 firecrawl 降级改为 `scrape.scrape_page` 或删除 |
| 4 | 中 | `plugins/dalton/skills/dalton-agenda/SKILL.md:59-72` | 收尾三步第三步的自动任务示例 JSON 用了 4 个不存在的字段（`schedule_type`、`enabled`、`agent_mode`、`workspace_name`），`delivery` 写成 `{methods:["feishu"]}`（合法形状是 `["slack"]` 式数组，且 feishu 是 OpenClaw 时代渠道残留） | 已用 `file.py` 的 `_Entry` 模型实测校验：6 项错误全命中，照抄必被拒（NOT SAVED）。收尾三步是每个研究会话的常见路径，首次写 once 任务大概率失败一轮 | 示例改为与 `automation-templates/` 一致的真实字段（`trigger_type`/`status`/`workspace_id`/`delivery: [...]`）；同文件 :39 的 `knowledge_read(target=…)` 也不存在（读取直接用文件），一并删改 |
| 5 | 中 | `src/ptc_agent/agent/middleware/skills/dalton_registry.py`（全文件） | 所有 22 条 `requires` 都只有 `("dalton-conventions", "research-conventions")`，未按 PS.0 表加附加依赖（如 bullbear→financial-lenses/knowledge/agenda；workflow 启动 skill→run-workflow；full-coverage→3-statements/dcf-model/comps-analysis/check-model/model-update/xlsx 等） | 用户在 Plugins 页单独关掉某个被依赖 skill 时，`resolve_disabled_skills` 不会保它，沙箱里 `.agents/skills/dalton-financial-lenses/scripts/anchors.py`、`run-workflow/SKILL.md` 等路径消失，对应 workflow/skill 运行中断且报错不直观 | 按 PS.0 表补齐附加 `requires`（依赖不传递，必须逐条声明） |
| 6 | 中 | `dalton-earnings-deep/SKILL.md:17,24`；`dalton-topic-research/SKILL.md:25`；`dalton-weekly-review/SKILL.md:39` | RunWorkflow 调用示例写 `args={...}`，工具真实参数名是 `params`（`tool.py:578`；`params` 才暴露为脚本 `args` 全局）。bullbear/initial-coverage/full-coverage 三处写的是对的 | 照抄示例的调用会被工具 schema 拒绝或 `params=None` → 脚本解构 null 直接 TypeError 终止运行；模型通常看 schema 自纠，但多一轮失败 | 三处 `args=` 改为 `params=` |
| 7 | 中 | `plugins/dalton/skills/dalton-conventions/SKILL.md:68`、`…/references/model-standards.md:16`、`dalton-company-background/SKILL.md:23` | dalton_sec 工具名不符：写成 `extract_facts`、`verify_numbers`、`get_filing_metadata`、`read_filing_section`；真实 MCP 只暴露 `get_company_facts` 和 `list_filings`（`plugins/dalton_data/dalton_sec_mcp_server.py:967,1029`） | agent 按总纲指引调用不存在的函数名报错；conventions 是所有 skill 的第一跳，错名会被反复模仿 | 统一为 `get_company_facts` / `list_filings`，正文读取指向 `researchdata.finance(route="sec")` |
| 8 | 中 | `scripts/dalton/migrate_legacy_knowledge.py:1012` | `guidance_history` 全部条目写死 `issued_at: "2026-07-29"`（实测 META 3 条、MSFT 5 条全部同一天），指引原文来自 legacy 底稿但日期是编造的 | 未来做 C4 指引兑现追踪（guide vs actual）时，错误的发布时间戳会污染"指引风格"判断；`outcome=pending` 有缓解但日期本身无来源 | 原文行无日期时写 `null` 或"date unknown"，不填具体日期 |
| 9 | 中 | `scripts/dalton/migrate_legacy_knowledge.py:1281` | `company.json` 写死 `autonomy: {enabled: true, max_usd_per_day: 15.0}`——一个迁移脚本发明的每日预算数字（owner 尚未定额度，P0.8 待决）。当前 `src/dalton` 无消费方（仅 schema 定义） | 现在无害，但字段在 schema 里是正式预算位：一旦后续 autonomy 消费方上线，四家公司会静默按 15 美元/天封顶 | 写 `null` 或去掉该键，等 owner 定额后由配置层填 |
| 10 | 低 | `dalton_registry.py:75-84` | `dalton-financial-lenses` 无 `command`（PS.0 表规定斜杠命令 `dalton-financial-lenses`，PS.2 验收也用 `/dalton-financial-lenses {TICKER}` 触发） | PM/分析师无法用斜杠命令直接触发财务镜头，只能靠别的 skill 带出 | 注册表补 `command="dalton-financial-lenses"`（frontmatter 不动，command 只在注册表） |
| 11 | 低 | `scripts/dalton/migrate_legacy_knowledge.py:1182` | 非财报催化剂的 watch event 日期 fallback 写死 `"2026-11-15"` | `date_status: "estimated"` 有标注、缓解了误导，但日期本身凭空 | fallback 日期写 `null`，`next_validations.due` 同 |
| 12 | 低 | `plugins/dalton/skills/dalton-company-background/SKILL.md:67` | `knowledge_update(target="company", …)`——真实参数名是 `doc`（`knowledge_tools.py:103`） | 照抄会被工具拒绝，模型看 schema 自纠 | 改为 `doc=` |
| 13 | 低 | `dalton-pm-qa/SKILL.md:41` | 提议 `/dalton-earnings-review`——该命令不存在（应为 `/dalton-earnings-deep`） | PM 问答路由到不存在的命令，agent 需要自行猜 | 改为 `/dalton-earnings-deep` |
| 14 | 低 | `workflows/earnings-deep-update/workflow.js:64-73` | `kp`（知识层修订草案）无条件派发：八节点评失败（`report===null`）时仍派一次，产出的 knowledge-revision.json 只能写"成稿缺失"（与 PS.9 骨架行为一致，但白花一次派发） | 点评支线失败的财报日：多一次无效子智能体 | `report === null ? null : await agent(...)` 与其他阶段对齐 |
| 15 | 低 | 迁移脚本幂等性的字节级缺口：`migrate_legacy_knowledge.py:349`（report `generated_at`）、`:438`（bundle `extracted_at`）、渲染 md 头时间戳 | 知识文档 `data/*.json` 与 `agent.md` 两次运行字节相同（已实测 diff），但 `raw/*.json`、`migration_report.json`、`lessons.md`/`verdicts.md`/`INDEX.md` 渲染头带墙钟时间 | 重复运行后 diff 不干净，审计对照费劲 | 渲染头与 report 时间戳沿用 `now_iso` 常量或从内容哈希派生 |
| 16 | 低 | `dalton-company-background/SKILL.md`、`dalton-call/SKILL.md`、`dalton-topic-research/SKILL.md`、`dalton-pm-qa/SKILL.md`（正文前 12 行） | 缺 PS 2.2 规定的第二句固定行（"公司认知按 dalton-knowledge 的读序读…"）；"Fund methodology"第一句 22 个 skill 都在 | 这 4 个 skill 单独触发时不强制先读知识层读序 | 补第二句（pm-qa 已有自己的读序段，可改成引用固定句式） |
| 17 | 低 | `dalton-earnings-preview/SKILL.md:117`（"第ス三节"混入日文假名）、`dalton-earnings-flash/SKILL.md:82`（"針對"繁体）、`dalton-director-daily.json` instruction 与 research-director SKILL 的 `coverage.html`/`coverage.md` 不一致 | 可读性/一致性小疵 | 顺手修正 |

## 已确认没问题的关键点

**workflow 与引擎接口（检查项 1）**
- 6 个 workflow 全部通过引擎真实 `compile_check`（`engine.py`，QuickJS）：`meta.name` 均等于目录名、描述长度合规、语法可编译。`node --check` 直接跑会因顶层 `return`/`export` 报错——这是引擎把脚本包进 async IIFE 的预期形态，非缺陷；等价包裹后语法全部有效。
- 脚本 API 用法与 `prelude.js`/run-workflow SKILL 一致：只用了 `agent(prompt, {agentType, label, phase, schema})`、`parallel`（槽位全部 thunk 形式或已启动 promise，符合吸收规则）、`phase`/`log`/`args`；schema 均 ≤4096B、深度 ≤5（weekly 的 SCREEN/CO 特意压扁并注释了深度账）。
- 派发数：bullbear-doc 6、initial-coverage 9、full-coverage 14、earnings-deep-update 5–6、topic-research ≤17，全部远低于 64；weekly-coverage-review 的 `room = 64 - 5 - core.length` 公式数学正确，Core(27)+Watch+call(3)+周会稿 合计 ≤64，超额 Watch 有 log 与"口径与缺口"出口。并发 8、单子智能体 1800s 由平台 config 兜底，脚本自身无并行超限写法。
- `agentType` 只用 `dalton-evidence`、`dalton-reviewer`、`general-purpose`：两个插件子智能体文件都在 `plugins/dalton/agents/`，且经 `bundled_agent_plans → plugin_bases → merge` 进入子智能体注册表（`subagent_graphs`），是 `known_subagent_types` 的合法成员；没有用无 `execute_code` 的内置 `research` 型。两个 agent 定义与 PS.1/PK.10 一致（取证员只取证不下结论、不调 `knowledge_*`、`tools: [web, market_data, code, shell]` 合法、无 model 键跟随线程模型）。
- 失败处理：失败槽 `null` 判定齐全（initial 的四支线全失败短路、full 的底表失败中止、weekly 的 `!scr.ok` 不下结论、各审稿/修订阶段前置 null 守卫）；审稿 sha256 前后比对已写进修订提示词（PS.17 规则 1）；无写死模型名（唯一 model 键在 `dalton-reviewer.md:3`，为 PS.2.5/PK.10 明确允许的一处 manifest 键）。

**skill 注册（检查项 2）**
- 22 个 skill 目录与 `DALTON_SKILL_DEFINITIONS` 一一对应；`description` 与 SKILL.md frontmatter 逐字一致（脚本全量比对 0 差异）；frontmatter 只有 `name`+`description`；与 LangAlpha 自带 skill 无名字冲突、无命令冲突（内置 `earnings-preview`/`initiating-coverage` 等均未被占用，Dalton 用 `dalton-` 前缀或 PS.0 指定的短名）；`registry.py:566` 挂钩带 `# DALTON-HOOK(skills-registry)` 标注，合并方式（`SKILL_REGISTRY.update`）与上游兼容；`tests/unit/middleware/skills/test_shared_skill_layer.py` 48 项通过。

**skill 内容对照 PS 分册（检查项 3，逐个结论见下节）**：bullbear v4.2 七要素齐全（共识三分类 2–4/2–4/1–3、Debate 五块、`→ Debate N` 修正图、3/6/12 催化剂、体检 ≤12 行、Variant perception 单点、一句话句式）；deep dive 12 问 + A/B/C/D 四支线 references 实文齐备（QUESTIONS 映射在 `research_structure.py`，13 项回归测试过）；前瞻三优先级 + 真实门槛五分 + 冻结时钟 + 零泄漏自检；快讯"上一季同比（禁环比冒充）/指引对上季官方指引/同口径 beat-miss"+14 项质量门；异动先量化（同组中位数+板块 ETF+z 值+量比，`data_ok=false` 不下结论）后 filing→新闻→卖方→社媒；周会 Core/Watch 分层 + `ln(P)=ln(EPS)+ln(PE)` 归因公式（`attribution.py`）；call 六门禁 + 预期差三问 + `scenario_irr.py` 禁心算 + Core/Tactical/Small + "暂无高 conviction"出口；conventions 八条底线覆盖来源身份分层、"本轮未取得≠未披露"（`evidence-and-sources.md:72-82`）、"分析师提问口径，管理层未确认"（§6）。

**无诱导编造（检查项 5）**：未发现"缺数据时合理估计"类指令；反向措辞密集（`[假设] 数字（推导依据）`、"本轮未取得"、preview"绝不取简单平均充当市场共识"、flash"零脑补推断"、topic"目前缺乏数据，严禁估算编造"、call"严禁心算"）。

**次数限制（检查项 6）**：19 个自动任务模板零 `max_failures`、零 `llm_model` 写死，全部通过 `_Entry` 真实校验（占位符替换后 19/19 OK）；`dalton-agenda` 明文"严禁写入未知字段如 max_failures"。唯一次数门是问题 2。

**覆盖名单（检查项 7）**：`coverage.yaml` 实测 105 条 = core 27 + watch 78，与 `tiers` 块一致；SPCX 在 core 且带 owner 决定的 note；美团 `3690.HK`（`aliases.adr=MPNGY`、market HK）；`build_coverage.py --check` 离线 PASS（5 项已知差异全部登记，含 MPNGY/601100.SH/BRK.B 三组代码归一与 watchlist 去重）；脚本用 `ast.literal_eval` 只读解析源文件、`write_coverage_yaml` 内容不变时保留 `generated_at` 幂等写、`--sync` 按内容哈希不重写。

**迁移与初始化（检查项 8）**：`migrate_legacy_knowledge.py` 对 SQLite 用 `file:…?mode=ro&immutable=1`；拒读 `writer-tokens.json`/`credential-grants`/`deliveries.json`；状态 JSON 剥离 feishu 键；执行前后对全部源文件做 SHA-256+mtime 快照比对（实测 `zero_source_mutations=true`，27/27 OpenClaw debate 迁移，32 个 PK.2 文档 schema 全过）；产物结构对齐 PK.1/2（`data/{8 docs}.json` + 渲染 md + `agent.md` 托管块 + `verdicts.md` + `published/INDEX.md`），原始 thesis.md/seen.json/updates.log/consensus/provenance（含逐行行号溯源与 SHA）全部保留。`init_company_workspace.py`：本地两次运行 0 写入全 unchanged；手工修改过的文件进 `skipped_modified` 不覆盖（实测）；只建骨架不写知识文档、4 家存量走 P1.4 的边界与 PK.7 说明一致。

**OpenClaw 残留（检查项 4，除问题 3/4 外）**：全库 grep 无飞书分块写入、无 25 分钟硬杀、无 `sessions_spawn`/`NO_REPLY`/`data_gateway.py`；`SOURCES.md`、`coverage.yaml.sources` 里的 `~/.openclaw/...` 路径是规则溯源与来源哈希记录（设计要求保留），非运行时调用；无人名账号（`earnings_quality.py` 的 "Richardson" 是 Sloan 比率文献作者）。

**各 skill 一词评价（22/22，financial-lenses 内容见前篇）**：dalton-conventions 扎实；dalton-knowledge 扎实；dalton-publish 扎实；dalton-agenda 扎实（示例 JSON 有错，问题 4）；dalton-init-company 扎实；dalton-research-director 扎实（次数门违规，问题 2）；dalton-bullbear 扎实；dalton-company-background 扎实；dalton-initial-coverage 扎实；dalton-full-coverage 扎实；dalton-earnings-calendar 扎实；dalton-earnings-preview 扎实；dalton-earnings-flash 扎实；dalton-earnings-deep 扎实；dalton-stock-move 扎实；dalton-thesis-delta 扎实；dalton-filing-review 扎实；dalton-weekly-review 扎实；dalton-topic-research 扎实（接口名残留多，问题 3）；dalton-call 扎实；dalton-pm-qa 扎实。无空洞模板化交付。

## 疑似问题（未能确认）

- `plugins/dalton/agents/dalton-reviewer.md:3` 的 `model: ocb-openai-gpt-6.1-sol`（及 `src/dalton/settings.py:51` reviewer_models 前两项 ocb-* 键）不在已提交的 `src/llms/manifest/models.json`（其中只有 `gpt-6.1-sol`/`-oauth` 等键）。若部署侧没有 gitignored overlay 补充 ocb-* 模型，agent frontmatter 的 model 会被忽略、草稿期审稿回落到线程模型——与作者同家族，失去跨家族独立性。需在真实栈上确认 ocb-* 键存在与否。
- `dalton-thesis-delta/SKILL.md:51`"AlphaEngine 每轮最多 3 篇读全文"：判断为检索深度指引（防额度滥用的范围约定）而非次数门，且"遇配额或未登录即停"是状态纪律；但措辞与"不许次数门"的边界值得 owner 过目。
- weekly-coverage-review 在 schema 校验失败触发引擎重试时，重试也计入 64 派发上限（`driver.py:430`）；Core+Watch 贴满上限的极端周会因此出现 null 槽。脚本已按 null=缺口处理，属设计内行为，提示而非缺陷。
- 迁移脚本 CLI 的 `extract/render/apply` 子命令已解析但未实现（`main()` 恒跑全量 dry-run 管线）；P1.4 设计说"经 PK.7 的导入 API 写知识文档"，仓库内未见消费 `var/dalton/migration` 产物的导入脚本——真实落库路径可能是另一条已部署链（d003/d005），只读审查无法确认。
