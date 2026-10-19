# 第三轮代码审查清单：P0H 修正与真实运行证据（`dalton/main` `18bd3d8e`）

版本 0.1，2026-10-18。写给负责审查的人。

**审查范围：** `dc16c3f0..18bd3d8e`，即 P0H 指南提交之后的全部代码改动。依据：`P0H-round2-fixes.md`（本轮的开发指南，决定 L28–L33）、`review/SUMMARY-R2-20261018.md` 与第二轮的 8 份审查报告、开发方的 `P0H-round2-dev-report.md`、`PROGRESS.md`、`trial-runs/G3-real-report-audit-20261018.md`、`trial-runs/G6-financial-lenses-comparison-20261018.md`。

## 0. 怎么用这份清单

### 0.1 规模和特点

- **6 个功能提交，每个都以 `--no-ff` 合入 `dalton/main`；** 59 个文件，新增约 3,800 行，删除约 1,200 行。

  | 族 | 分支 | 功能提交 | 合入提交 | 规模 |
  |---|---|---|---|---|
  | H7 | `fix/P0H.H7-progress-truth` | `425f7551` | `355e3a37` | 1 个文件，+14 / −14 |
  | H4 | `fix/P0H.H4-providers-roles` | `90a1d94e` | `588dfc95` | 12 个文件，+613 / −67 |
  | H5 | `fix/P0H.H5-budget-journal` | `4fe42f87` | `1e4f3851` | 6 个文件，+403 / −13 |
  | H1 | `fix/P0H.H1-publish-redesign` | `dabe0b7f` | `c1e76363` | 18 个文件，+932 / −752 |
  | H3 | `fix/P0H.H3-autonomy-chain` | `16006401` | `aa1d1922` | 18 个文件，+1,492 / −312 |
  | H2 + H6 | `fix/P0H.H2-H6-realrun-evidence` | `a725c47f` | `18bd3d8e` | 19 个文件，+373 / −80 |

- **按族拆分做到了，审查可以按族独立进行。** 但有两处和 L32 不一致，见第 4.3 节：H2 和 H6 合进了一个分支；GitHub 上没有 PR，只有本地合并后推送的分支。
- **本轮的重点从"代码对不对"转到了"证据是不是真的"。** 开发方声称 G1–G7 除 G5.2 外全部达到 🟨，PROGRESS.md 的 P0.9/P0.10 一行写着"证据已齐备"。**本轮审查的结论直接决定能否进入 P0.9 界面走查和 P0.10 验收。**

### 0.2 审查规则（和前两轮相同）

- 每条检查给出 ✅ 通过 / ❌ 有问题 / ❓ 疑似，并写明依据：读了哪段代码、跑了什么命令、看了哪份证据。
- 严重程度分四级：严重、高、中、低，定义见 `REVIEW-CHECKLIST.md`。
- 只读审查：不改代码，不写生产数据，不登录供应商，不打印令牌或密钥。允许跑单元测试和离线脚本。**涉及 LLM 的检查，允许用便宜的模型做少量真实调用，总花费控制在 $2 以内，在报告里写明花了多少。**
- **"单元测试通过"不能代替审查；"证据文件存在"也不能代替审查。** 前两轮已经出现过单元测试全绿、接口一调就 500；这一轮要防的是证据文件齐全、但证据本身不能证明它声称的东西。
- 结果写进 `docs/dalton/dev-guide/review/R3-<模块>-<日期>.md`，汇总写进 `review/SUMMARY-R3-<日期>.md`。

### 0.3 本轮的审查重点

1. **证据的真实性。** 每份证据都要回答三个问题：
   - 它是不是真的经过了声称的那条通路（真实后端、真实 LLM、真实数据库），而不是在脚本里直接调用内部函数、或者往数据库里预先写入数据；
   - 它的数字和调用日志 `dalton_llm_journal`、预算表、日志时间戳对得上吗；
   - 换一个人、换一个时间重跑一次，结果还是一样吗。
2. **开发报告和指南的出入。** 开发报告里有几处的编号、名称、做法和 P0H 的原文不一致（第 2.2 节）。不一致不一定是错，但每一处都要核对：是换了个名字做了同一件事，还是做了另一件事、原来要求的那件没做。
3. **L28–L31 有没有真正落地。** 发布核验的分工改了，要确认改的是生产路径，而不只是新增了一条测试路径。
4. **L33 的测试替身规则。** 抽查本轮新增和修改的测试，确认没有新的 `mock(**kwargs)`。

## 1. 先跑的命令

```bash
cd /Users/everflow/Projects/dalton-langalpha && git checkout 18bd3d8e
./scripts/dalton/check_hooks.sh --strict --audit
uv run pytest tests/unit/dalton tests/unit/llms tests/unit/middleware/skills tests/unit/mcp_servers -q   # 开发报告声称 1,685 项，PROGRESS.md 第 5 节写的是 1,674 项
(cd integrations/openclaw/dalton-langalpha-broker && node --test test/*.test.mjs)                        # 声称 13 项
pnpm --dir web typecheck && pnpm --dir web test                                                          # 声称 7,224 项
uv run python scripts/dalton/check_skill_refs.py                                                         # 声称 ghost_hits=0, broken_refs=0

# L30：应删掉的规则。指南和开发报告用了两套名字，两套都要搜，结果应为空
grep -rnE "_CN_PROSE_FIG_RE|_CN_DIGIT_MAP|_parse_cn_prose_figure|_CN_COLLOQUIAL_MULTI_RE|_CN_NUMBER_SEQ_RE|_chinese_numeral_to_float" src/dalton
grep -rnE "label_hint|is_consensus_labeled_line|consensus_confusion" src/dalton
grep -rn "llm_service" src/dalton/publish

# L30：冻结的表，和 P0H 之前比较应该没有新增条目
git diff dc16c3f0 18bd3d8e -- src/dalton/publish | grep -nE "^\+.*(DEFAULT_METRIC_ALIASES|_METRIC_SYNONYM_GROUPS|_GROWTH_WORDS|_MARGIN_WORDS|_GENERIC_MODIFIER_WORDS)"

# 本轮新增的正则，逐条列出，用于第 4.1 节
git diff dc16c3f0 18bd3d8e -- src/dalton | grep -nE "^\+.*re\.compile"

# L33：不带 spec 的测试替身
git diff dc16c3f0 18bd3d8e --name-only -- tests | xargs grep -nE "(Magic|Async)?Mock\(\)|Mock\(\*\*|side_effect=lambda \*\*" 

# 行数统计（开发报告声称 publish + knowledge 净减 88 行）
git diff --stat dc16c3f0 c1e76363 -- src/dalton/publish src/dalton/knowledge | tail -1
git diff --stat dc16c3f0 18bd3d8e -- src/dalton/publish src/dalton/knowledge | tail -1

# 守门测试的破坏性验证
uv run pytest tests/unit/dalton/test_router_wiring.py -v
```

**每条命令的实际输出都附在报告里，** 和开发者声称的结果逐一对照。**lead 预跑的两条行数统计：** H1 一个提交内 `publish + knowledge` 为 +602 / −707（净 −105）；到 `18bd3d8e` 为止为 +871 / −741（净 +130，H3 在知识层又加了约 150 行）。和"净减 88 行"都对不上，需要说明口径。

## 2. lead 预审发现的疑点（最先回答）

lead 抽看了几份证据文件和开发报告，发现下面这些问题。**审查人先逐条核实。** 第 2.1 节的任何一条坐实，相应族的档位就退回 🟦。

### 2.1 证据本身的疑点

| # | 疑点 | 依据 | 怎么核实 |
|---|---|---|---|
| E1 | **META 真实报告 445 个数字全部判为"已核实"，耗时 7.56 秒。** 第二轮同一份底稿抽出 835 个数字、误判 331 个；这一轮数字少了将近一半，结论变成 100% 通过 | `var/dalton/reports/g3_real_llm_audit_20261018.json` 的 `meta_real_report_audit`：`verified_count=445`，其余全为 0，`elapsed_s=7.56` | ① 证据来源是 `var/dalton/migration/.../meta/data/thesis.json`，**它是不是由同一份 `raw/META.json` 迁移生成的？** 如果是，这就是拿报告自己核验自己，结果必然全对，不能作为 G3 的验收依据；② 445 个数字按 40 个一批要调 12 次标注，加上判断，7.56 秒做不完。查 `dalton_llm_journal` 里这次运行实际有几次 `audit.label_figures` 和 `audit.judge` 调用；③ 835 → 445：是掩码排除得更多了，还是抽取漏了？抽 20 个第二轮抽到、这一轮没抽到的数字，逐个说明 |
| E2 | **G3 全部 5 个场景只有 15 次 LLM 调用，** 包括口语数字、注入、META 全文和独立审稿 | 同上文件的 `journal_cost_summary.journal_calls=15` | 按场景拆分这 15 次调用。META 全文如果只有极少数调用，说明 B 环节的"每份报告必经标注"在这条路径上没有发生 |
| E3 | **口语数字场景耗时 0.07 秒，** 真实 LLM 调用不可能这么快 | 同上文件的 `colloquial_cn_extraction.elapsed_s=0.07` | 确认这一步调的是真实 LLM 还是缓存命中、还是测试替身。如果是缓存，找到第一次真实调用的记录 |
| E4 | **注入场景里，数字的 `metric` 字段是"我们在   看到公司广告收入达"，** 看起来是把句子去掉期间后的剩余部分，不像 LLM 标注的结果 | 同上文件 `prompt_injection_real_llm.contradicted_items[0].metric` | 确认标注环节是否真的调用了 LLM；如果标注失败，`labeling_incomplete` 应该为 true，但这里是 false。**这一项如果坐实，说明 L31 的失败退路也有问题** |
| E5 | **注入场景的结论是"有矛盾"，** 不是 P0H §1.4 要求的锚定失败时的"无来源"或"未能判断" | 同上：`contradicted_count=1`，`reason=source_conflict` | 注入的指令要求判 supported，LLM 没照做、判了 contradicted，引文 512 和草稿 9999 确实不同，这个结论本身是对的。但要确认：**这个"有矛盾"是 D 环节 LLM 判断 + 锚定得出的，还是 C 环节脚注匹配得出的？** 如果是后者，注入实测根本没有走到 LLM 判断，不能证明锚定兜住了注入。用测试替身补一个"LLM 照做、返回 supported"的场景，确认代码锚定把它否决 |
| E6 | **G1 里一次 xAI 调用的现金花费是 $1.05，** 而开发报告写 G1 的真实花费是 $0.00148 | `var/dalton/smoke/g1_budget_realrun_20261018.json`：`by_provider.xai = {cash_usd: 1.05, calls: 1}`，`open_reservations_usd=1.0` | 这 $1.05 是真实调用产生的，还是往调用日志里预先写入的数据？**P0H §6 要求的是"派出现金的取证子智能体，花到上限后被拒"。** 如果是写入数据再调准入函数，证明的是准入逻辑，不是端到端的整轮拒绝。查这条日志记录的来源和 `grok-4.7` 子智能体的实际运行记录 |
| E7 | **G1 的拒绝是在准入函数层面验证的，** 字段名是 `turn2_granted_after_cap`、`enforce_local_admission_byok_rejected` | 同上文件 | 确认有没有一次**通过真实后端接口**发起的对话被拒，前端显示的是证据里那段中文提示 |
| E8 | **独立审稿的结论是 `revise`（需修改），** 证据里写的是"通过" | `g3_real_llm_audit_20261018.json` 的 `independent_reviewer.verdict="revise"`；开发报告第 5 节写"跨家族审稿通过" | "通过"指的是跨家族隔离生效，还是审稿通过？报告措辞要改准确。另外确认 `retry_attempts=1` 的那次重试有没有重复计费 |

### 2.2 开发报告和 P0H 指南的出入

| # | P0H 的要求 | 开发报告写的 | 要核对什么 |
|---|---|---|---|
| D1 | A1：传 `user_id`，键名用 `cap_usd`、`spent_usd` | 改读 `spent_usd` 与 `open_reserved_usd`，没有提 `user_id` 和 `cap_usd` | `preflight.py`、`board.py` 是否传了 `user_id`；看板显示的额度是不是从 `cap_usd` 读的，而不是又写死了 |
| D2 | A3：用 `asyncio.wait_for` 包住整个预检，45 秒超时时**唤醒，不记为失败** | "显式捕获 `CancelledError` 并立即向上传播" | 总超时到底有没有实施？超时后的结果是唤醒还是失败？写一个让预检卡住 46 秒的测试替身验证 |
| D3 | A8：时区一律用 `ZoneInfo(settings.autonomy.timezone)`；没有时区的 `next_run_at` 按自动任务的时区补上 | A12：统一使用 `ZoneInfo("Asia/Shanghai")` | 是不是写死了时区，而不是读配置？`schedule.py:379-384` 的无时区时间补全有没有做 |
| D4 | A10：**只有认领成功的那个才唤醒**；`upsert_signal` 改为 `INSERT … ON CONFLICT`；空工作区的信号用哨兵值去重 | A10：`upsert_signal` 返回 `(row, is_unwoken)`，只要未唤醒、未处理、未过期就纳入候选 | **这可能和要求的方向相反。** 指南要防的是并发时唤醒两次；"只要未唤醒就纳入候选"会让两个并发的预检都把同一个信号当作候选。读 `watch.py` 和 `preflight.py:659` 附近，确认原子认领的结果是否被使用。**用两个并发的预检实际跑一次** |
| D5 | A11：预检读 `next_attempt_after`，跳过还在退避期的问题 | A11 变成了"`probes/calendar.py` 读错 `agenda` 顶层字段" | 退避期的检查做了没有？`grep -rn next_attempt_after src/dalton/autonomy` |
| D6 | A12：看板和记录工具对"是否超出额度"的口径统一为严格不超出 | 报告里没有这一项 | 对比 `board.py:413` 和 `director_tools.py` 的比较运算符 |
| D7 | A13：自动任务时间表全部对齐 `automation-templates/`，包括每晚增量、监测频率、研究总监的时间 | A13：只写了财报日历周任务统一为周六 09:00 | 逐个对照 `schedule.py` 和 `automation-templates/` 的每一个模板 |
| D8 | A14：议程探针、内部库探针、信号处理的异常一律改为 warning | A14 变成了"未消费信号查询加上 `expired_at IS NULL`" | `grep -rnE "logger\.debug|except.*:\s*pass" src/dalton/autonomy`，逐个看异常分支的日志级别 |
| D9 | — | A8：`automation_executions.py` 对 `dalton-*` 的失败设 `status='active'` | 这是指南之外新增的改动，而且改的是上游文件。确认有 `DALTON-HOOK` 标注，非 Dalton 的自动任务行为不变，凭据失效仍然立即停用（第二轮 H9 的要求） |
| D10 | §1.4：supported 锚定失败 → "未能判断"；contradicted 锚定失败 → "无来源（附候选）"；③ 如果 LLM 报了 `source_value`，必须等于引文里找到的数 | 从结构里删掉了 `source_value`；contradicted 锚定失败 → `unverified` | 删掉 `source_value` 比指南更彻底，可以接受。**确认 supported 锚定失败时得到的是"未能判断"，不是"无来源"**：两者在审批卡上的含义不同 |
| D11 | §1.5：掩码补上斜杠日期（"10/12"）、时间、12 位以上长整数、批次号（"20261015-2200"）；修复"# 2Q26 复盘"的节号问题 | 正则清单里有 `_TIME_RE`、`_HEADING_NUM_RE`、`_HYPHEN_CODE_RE`，没有单独列出斜杠日期和长整数 | 用这五个例子各跑一次 `numbers.py` 的抽取，确认都不再被当作数字，并且"2Q26"的期间约束生效 |
| D12 | §1.6：`expire_stale_publications` 接到研究总监的每日任务；三处删掉 `.pdf`；审批卡组件测试（英文键和中文键两版）；作者模型取不到时打 warning；LLM 判断并发 4 加整步总时限 | 报告里只提到了审批卡的 `labeling_incomplete` 横幅 | 五项逐一核对。`grep -rn expire_stale_publications src/dalton`；`grep -rn "\.pdf" plugins/dalton/skills/dalton-publish src/dalton/publish plugins/dalton/skills/dalton-init-company` |
| D13 | §1.3：删掉的是 `_CN_PROSE_FIG_RE`、`_CN_DIGIT_MAP`、`_parse_cn_prose_figure`；别名表叫 `DEFAULT_METRIC_ALIASES` | 删掉的是 `_CN_COLLOQUIAL_MULTI_RE`、`_CN_NUMBER_SEQ_RE`、`_chinese_numeral_to_float`；冻结的是 `_METRIC_SYNONYM_GROUPS` | 第 1 节的 grep 两套名字都搜。如果指南里的名字在 P0H 之前就不存在，说明指南写错了名字，记一笔即可 |
| D14 | §2.2：PROGRESS.md 里记下环境启动的时间、各项检查的结果和截图 | PROGRESS.md 里没有单独的 H2 一行 | 找 `var/dalton/smoke/backend_startup_20261018.log`，确认三件事：容器能访问 8960 和 18789；`.env` 里有 `DALTON_BUDGET_GATE=1` 和 `APP_ENV=dalton`；启动日志里没有"预算未启用"的 warning |
| D15 | §6：G5 的四项都要达到 🟨，才能进入 P0.9/P0.10 | G5.2（Antigravity 4 轮工具调用、Docker 下粘贴回调登录）保持 🟦；但 PROGRESS.md 的 P0.9/P0.10 一行写着"证据已齐备" | **按 P0H §6 的原文，门槛没有达到。** PROGRESS.md 这一行属于措辞偏高，要改。是否允许 G5.2 带着 🟦 进入 P0.9，由 lead 决定，审查报告里单独列出 |
| D16 | PROGRESS.md 的每一行都要能对应到证据 | 测试数量：开发报告写 1,685，PROGRESS.md 第 5 节写 1,674 | 以第 1 节的实测为准，PROGRESS.md 改成一致 |

## 3. P0H 的逐条核对

不看提交说明，看代码和运行结果。第 2 节已经列出的项不用重复，在这里引用编号即可。

### 3.1 H1 发布核验（优先级：严重）

范围：`src/dalton/publish/{numbers.py, match.py, audit.py, review.py, service.py}`、`src/dalton/database/publications.py`、`web/.../PublishApprovalCard.tsx` 及其测试。

**第二轮的严重问题（锚定）：**
- [ ] 读 `match.py`：画出 LLM 返回 `{verdict, matched_source_id, quote, quote_number_raw}` 之后，代码复核的每一步，和 P0H §1.4 的表逐行对照。
- [ ] **用测试替身固定 LLM 的返回，跑下面五种情况，结论必须和右边一致：**

  | LLM 返回 | 应得结论 |
  |---|---|
  | supported，`quote` 不在来源原文里 | 未能判断 |
  | supported，`quote` 真实存在，但 `quote_number_raw` 不在 `quote` 里 | 未能判断 |
  | supported，`quote` 和 `quote_number_raw` 都真实存在，但这个数和草稿换算后不等 | 未能判断 |
  | contradicted，`quote` 真实存在，引文里的数和草稿换算后相等 | 无来源（附候选），不是"有矛盾" |
  | exempt，草稿原句里没有 `[估计]` 等标记 | 无来源 |

- [ ] **"引文里抽数"用的是哪个解析器？** 应该和抽取草稿数字的是同一个确定性解析器，单位换算一致。`quote_number_raw` 是"万亿"、"bn"、"%"、"bps" 时分别试一次。

**L28 的分工：**
- [ ] **除脚注数值确实不符之外，代码不能独立判出"有矛盾"。** 搜索所有能产生 `contradicted` 或 `source_conflict` 的代码分支，逐个列出，确认只有 C 环节的脚注路径和 D 环节的 LLM + 锚定路径。
- [ ] **数值碰巧相同、但指标或期间对不上的，** 判为"无来源"并附候选。用第二轮的反例（"DAP 512 百万"对"广告收入 512 亿美元"）跑一次。
- [ ] **一行里同时有一致预期和我们自己的估计，两个都带正确的脚注，** 两个都应判为"已核实"（P0H §1.7 的反例）。
- [ ] **B 环节必经：** `check_draft` 和 `publish_report` 两条生产路径都调用了 `aaudit_draft` → `maybe_label_prose_figures_with_llm`。从接口入口追踪到 `assist_json(task="audit.label_figures")`，不能只看函数存在。另外搜索有没有绕过 `aaudit_draft` 的旧同步入口还被调用。

**L31 的失败退路：**
- [ ] 标注的任一批次失败时，`labeling_incomplete=True`，审批卡顶部有琥珀色横幅。**确认失败的那一批数字没有退回到规则判断，** 也没有被直接丢掉。
- [ ] 额度用完时标注失败，结果同上，而且不会阻止草稿保存。

**§1.6 其他项：** 见 D12。

**G3 的验收（真实 LLM，审查人自己重跑）：**
- [ ] **换一份证据独立的真实报告重跑一次。** 不能用由同一份底稿迁移生成的 `thesis.json` 当证据（见 E1）。可以用 PS 试跑的产出加它实际引用的 SEC 和电话会原文，或者第二轮审查用过的底稿加独立整理的证据。记录误判数和"未能判断"的比例。
- [ ] **验收标准只量了一个方向。** "误判为有矛盾或无来源不超过 2 条"防的是误伤；**更危险的是"错判为已核实"。** 从结果里随机抽 30 个"已核实"的数字，人工逐个打开来源核对，记录有几个其实不该通过。只要有 1 个，按高危报告。
- [ ] 注入实测重跑一次，并按 E5 补"LLM 照做"的场景。

### 3.2 H3 自主议程（优先级：高）

范围：`src/dalton/autonomy/**`、`src/dalton/database/watch.py`、`src/dalton/tools/director_tools.py`、`src/server/database/automation_executions.py`、`src/server/services/automation_settlement.py`、`src/dalton/knowledge/{ops.py, service.py}`。

**先做编号对照。** 开发报告的 A8–A14 和 P0H 的 A8–A14 不是同一组问题（见 D3–D9）。**审查报告按 P0H 的编号逐条给结论，** 开发报告多出来的项另列。

| P0H 编号 | 内容 | 对应开发报告 | 结论 |
|---|---|---|---|
| A1 | 预算键名、传 `user_id` | A1 | 见 D1 |
| A2 | 分诊调用 `assist_json` 的签名 | A2 | |
| A3 | 45 秒总超时 | A3 | 见 D2 |
| A4 | 议程探针读正确字段，用真实 GOOGL 议程写测试 | A4 | |
| A5 | `dalton_agenda_note` 补 `owner`，**用真实的结构定义校验写入内容** | A5 | |
| A6 | 内部库探针改走桥接，上次检查时间存 `dalton_watch_state`，异常改 warning | A6 | |
| A7 | 研究总监的五个"有事可做"条件 | A7 | |
| A8 | 时区读配置 | A12 | 见 D3 |
| A9 | 额度用完时一次性任务推迟，不关闭 | A9 | |
| A10 | 原子认领，只唤醒一次 | A10？ | 见 D4 |
| A11 | 跳过退避期的问题 | — | 见 D5 |
| A12 | 额度比较口径统一 | — | 见 D6 |
| A13 | 时间表全部对齐模板 | A13（部分） | 见 D7 |
| A14 | 异常改 warning | — | 见 D8 |

- [ ] **A2：** 读 `test_autonomy_preflight.py`，确认替身是 `create_autospec(assist_json, spec_set=True)`；**把调用处的一个参数名改错（只在本地，不提交），确认测试失败。**
- [ ] **A5：** 测试里校验写入内容用的是 `knowledge/schemas/agenda.schema.json` 本身，还是一份复制出来的结构。
- [ ] **A7：** 五个条件逐个对照 P0G G4.3 第 3 条。
- [ ] **A9：** `defer_once_1d` 只用于"因额度跳过"，其他原因的跳过保持原来的行为。
- [ ] **§3.3 字段修复：** `difflib` 的结果只作为建议交给 LLM，**没有一条路径会不经 LLM 确认就直接改写。** LLM 确认后的写入，仍然执行三条硬规则的检查（不能写进 `/official` 类字段）。构造一个会诱导 LLM 往 `thesis.official` 写的错误字段名，确认被拒。
- [ ] **G4 的真实运行（E 类核实）：** 读 `var/dalton/smoke/g4_autonomy_realrun_20261018.json` 的七个场景。P0H §3.4 允许"人为制造信号"，但**要用真实的数据通路。** 逐个场景判断：是通过触发真实的监测任务、真实的预检进入的，还是直接调用内部函数、直接往 `dalton_watch_state` 或信号表写数据？后者不算 🟨。场景 ② 的"不唤醒"要有 LLM 给出的理由，并且在 `dalton_llm_journal` 里能找到这次 `preflight.triage` 调用。

### 3.3 H4 供应商与模型角色（优先级：高）

范围：`src/dalton/services/provider_status.py`、`src/dalton/llm/role_policy.py`、`tests/unit/dalton/test_router_wiring.py`。

- [ ] P1–P4 逐一核对。
- [ ] **守门测试（L33）：** 把 `provider_status.py` 里的 `resolve_model_client` 改回 `resolve_llm_client`（只在本地，不提交），确认 `test_router_wiring.py` 失败。再任选两个其他接口背后的服务函数做同样的破坏，确认都会失败。
- [ ] **探测的花费：** 证据写"测试连接"花费 $0.00051，并计入了预算。到 `dalton_llm_journal` 里找到这几次探测调用。确认探测走的是 `dalton_complete`，而不是自己记了一笔账。
- [ ] **`check_roles`：** 10 个角色全部校验；`assist` 配成 contributor 变体时，**后端启动失败**，而不是只打一行 warning 继续运行。
- [ ] **改动了 `src/dalton/llm/providers/{antigravity,muse}`：** 开发报告没有提到这两处，看一下改了什么，有没有影响第二轮已经验证过的行为。

### 3.4 H5 预算与调用日志（优先级：中）

范围：`src/dalton/database/{llm_journal.py, budget.py}`、`src/dalton/llm/complete.py`。

- [ ] **B1：** 清理的是 `response` 正文，元数据保留；清理由每日任务触发。**确认请求原文也有同样的保留期限，** 或者本来就不存请求原文。
- [ ] **B2：** 现金汇总失败时拒绝准入，返回 503，打 error 日志。**在真实后端上验证一次：** 让数据库查询失败（比如临时改一个错误的表名，只在本地），发起一次对话，确认被拒，而不是按 $0 放行。
- [ ] **B3：** 有线程或工作区的一次性调用检查 task cap，没有的只检查日额度；P0.8 的说明里写了这一条。
- [ ] **`R2-N1 #6`：** 响应序列化异常落为 `indeterminate`，同一调用 ID 重试返回"状态不确定"，不会再调一次模型。

### 3.5 H7 PROGRESS.md

- [ ] P0H §7 的三行都已改正。
- [ ] **按 L32，PROGRESS.md 的每一个 🟨 都能对应到一份证据。** 逐行抽查，证据文件存在、日期对得上、内容支持这一行的说法。D15、D16 两处要改。
- [ ] **证据都在 `var/` 下，而 `var/` 被 `.gitignore` 忽略，** 不在仓库里。换一台机器、或者本机清理之后，证据就没了。建议：把每族证据的摘要（不含凭据）复制到 `docs/dalton/dev-guide/trial-runs/`，或者在 PR 描述里贴出关键字段。审查报告给出结论。

## 4. L28–L33 专项审计（必须回答）

### 4.1 代码量与规则数量（L30）

1. **发布核验和知识层的净变化：** 按第 1 节的两条统计给出 H1 单族和本轮整体的数字，和开发报告的"−88 行"对照，说明差在哪里。
2. **第 1 节 grep 出来的每一条新增 `re.compile`，逐条判断：** 是 P0H 允许的掩码和格式识别，还是又在用规则做语义判断。开发报告列了 7 类、二十多条正则，逐条在代码里找到它们的用途。
3. **冻结的表没有新增条目。** `_METRIC_SYNONYM_GROUPS` 等只用于候选的排序和粗筛：找到它们的每一个使用点，确认没有一个能直接决定结论。
4. **结论：** 规则还在长吗？

### 4.2 测试替身（L33）

1. 第 1 节的 grep 结果逐条看：不带 `spec` 的替身用在什么地方？如果是数据库连接池这类不在乎签名的地方可以接受，**用在 `assist_json`、`dalton_complete`、服务函数上的，一律按中危报告。**
2. 抽查本轮新增的测试，和第二轮一样回答：锁的是修好的回归和钱、凭据相关的契约，还是凑覆盖率？有没有锁定 LLM 的具体输出？

### 4.3 开发流程（L32）

1. **一族一个分支：** H2 和 H6 合进了一个分支。H2 是环境启动，几乎没有代码，可以接受，但要在汇总里写明。
2. **一族一个 PR，PR 描述附证据：** GitHub 上没有任何 PR（`gh pr list --state all` 为空），合并是在本地完成后推送的。**证据只出现在提交说明和开发报告里。** 由 lead 决定是否要求补 PR；审查报告里写明这一点不符合 L32。
3. **提交说明和代码一致吗：** 每个功能提交的说明，抽两句具体的说法（比如"删除约 95 行中文数字正则"），到 diff 里确认。

## 5. 真实运行证据的复核

按 P0F.5 的三档判断。**只有单元测试、或者只在内部函数层面验证的，一律按 🟦 算。**

| 族 | P0H 要求的真实运行验证 | 证据 | 本轮重点核实 | 档位 |
|---|---|---|---|---|
| G1 | 日额度 $1，Claude 订阅主模型派出现金子智能体，花到上限后被拒；xAI key 的对话花费计入；审稿第二次命中缓存 | `smoke/g1_budget_realrun_20261018.json` | E6、E7 | |
| G2 | 全部 Dalton 接口各调用一次，包括"测试连接"；`assist` 配成 contributor 变体时启动报错 | `smoke/g2_api_and_probes_20261018.json` | 接口清单和 `router.py` 对照，一个都不少；见 3.3 | |
| G3 | P0H §1.7：反例集、注入实测、真实报告误判 ≤2、"未能判断" ≤10% | `reports/g3_real_llm_audit_20261018.json`、`trial-runs/G3-…md` | E1–E5、E8；第 3.1 节的独立重跑和 30 个抽样 | |
| G4 | P0H §3.4 的六点，加 `dalton_agenda_note` 实际写入 | `smoke/g4_autonomy_realrun_20261018.json` | 第 3.2 节的"真实数据通路" | |
| G5.1 | 重启后同 ID 重试返回 409、没有二次计费；断开后同 ID 重试拿到完整结果 | `smoke/g5_broker_disconnect_and_restart_20261018.json` | 重启的是 broker 还是网关？P0H 要求重启 broker。到调用日志里确认只计费一次 | |
| G5.2 | Antigravity 4 轮工具调用；Docker 下粘贴回调地址登录 | 无，保持 🟦 | 见 D15 | 🟦 |
| G6 | BABA、0700.HK、META 按 SKILL.md 的命令跑通 | `lenses/*_20261018.json`、`trial-runs/G6-…md` | **审查人自己按 SKILL.md 的命令跑一遍 BABA，** 结果和证据对比；对比表里和基金原脚本的差异，每一处都有解释 | |
| G7 | 一次超过 240 秒的长文档读取成功返回；桥接日志里没有 stderr | `smoke/g7_bridge_realrun_20261018.json` | 242.05 秒是一次真实的长文档读取，还是人为加了延迟？两种都可以接受，但要写明 | |

**证据核实的通用方法：**
1. **对账。** 每族证据里写的调用次数和花费，到 `dalton_llm_journal` 里按时间窗口和测试用户查出对应的记录，逐条对上。对不上的，按"证据不实"报告。
2. **查来源。** 证据里每一个关键数字，找到产生它的脚本（应该在 `scripts/dalton/` 或测试目录里），读一遍：它调的是真实接口，还是内部函数？有没有预先写入数据？
3. **抽一族重跑。** 至少挑 G3 或 G4 中的一族，审查人自己完整重跑一遍。

## 6. 新代码的常规审查

和前两轮相同的通用检查项：多 worker 下状态以数据库为准、日志里没有凭据、没有静默吞掉错误、改动上游的地方都有 `DALTON-HOOK` 标注。本轮改动的上游文件只有 `src/server/database/automation_executions.py`、`src/server/services/automation_settlement.py` 和前端两处，逐个确认挂钩标注。

- [ ] **H3 的 +1,492 行是本轮最大的一块。** 除了第 3.2 节的逐条核对，按常规审查读一遍 `preflight.py` 和 `watch.py` 的新代码，特别是并发和事务边界。
- [ ] **`probes/internal.py` 改走桥接之后：** 桥接不可用时的行为（超时多久、打什么日志、会不会拖住整个预检）。
- [ ] **前端：** `PublishApprovalCard.tsx` 的新横幅和"未能判断"列表，在中英文两套文案下都显示正确；Settings 面板的改动是什么。

## 7. 交付物

| 报告 | 内容 |
|---|---|
| `review/R3-evidence-audit-<日期>.md` | 第 2.1 节 E1–E8、第 5 节：每一族证据的真实性结论和最终档位 |
| `review/R3-guide-conformance-<日期>.md` | 第 2.2 节 D1–D16、第 3 节：P0H 每一条要求的逐条结论 |
| `review/R3-anti-pattern-and-process-<日期>.md` | 第 4 节：代码量、正则、测试替身、流程 |
| `review/R3-code-<日期>.md` | 第 6 节：常规审查，可以按族分成几份 |
| `review/SUMMARY-R3-<日期>.md` | 汇总：问题统计、证据档位表、能否进入 P0.9/P0.10 的结论、建议的修正顺序 |

## 8. 建议的审查顺序

1. **第 2.1 节 E1–E5（G3 的证据）。** G3 是 P0H 的核心；如果 META 全文核验是"自己核验自己"，H1 的验收需要重做，后面的判断都受影响。
2. **第 3.1 节：锚定的五种替身场景和独立重跑。**
3. **第 2.2 节 D4（并发唤醒）和 D2（总超时）。** 这两项可能是没修好、甚至改反了。
4. **第 2.1 节 E6–E7（G1）和第 3.2 节的 G4 数据通路。**
5. **第 2.2 节其余各项、第 3.3–3.5 节。**
6. **第 4 节、第 6 节。**
7. **第 5 节汇总档位。**

## 9. 进入 P0.9/P0.10 的判定标准

同时满足以下条件，才建议进入：

1. 没有未修复的严重和高危问题。
2. G1、G2、G3、G4、G5.1、G6、G7 经本轮复核都是 🟨，**而且至少一族由审查人独立重跑通过。**
3. G3 用证据独立的真实报告验收通过，30 个"已核实"抽样没有错放。
4. 第 2.2 节的出入，每一条都有结论：做了、没做但有合理理由、或者列入下一轮修正。
5. G5.2 是否允许带着 🟦 进入，由 lead 在汇总里明确写出决定。
