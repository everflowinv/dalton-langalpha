# 第五轮代码审查清单：P0J 修正与证据重做（`dalton/main` `db11613b`）

版本 0.1，2026-10-21。写给负责审查的人。

**审查范围：** `96804a0e..db11613b`，即 P0J 指南（PR #14）合并之后的全部改动。依据：
- `P0J-round4-fixes.md`：本轮的开发指南，决定 L39–L43，§8.1 第 1–20 条；
- `review/SUMMARY-R4-20261019.md` 与第四轮的审查报告；
- 开发方的 `P0J-round4-dev-report.md`；
- `PROGRESS.md`；
- `trial-runs/realrun-2026-10-21/` 下的 8 份证据摘要；
- GitHub PR #13–#45。

## 0. 怎么用这份清单

### 0.1 这一轮要回答的只有两件事

1. **P0 能不能进入 P0.9/P0.10。** 第四轮的结论是"不进入"，理由是证据不可信（V1）、按用户的说法全是 owner 账号在跑、测试后门。本轮要判断这些是否真的解决了。
2. **本轮新改的生产代码有没有引入问题。** 重点是 PR #35（预算结算）和 PR #39（口语数字），其余大多是证据脚本。

**不要把这一轮做成第五次全面审查。** 项目"完成"的定义是 PM 能读、能评分的研究成果（`README.md` 第 1 节），不是证据工具越来越完善。审查发现的问题，分成"挡住 P0 验收的"和"可以带着往下走的"两类，后者登记即可。

### 0.2 规模

| PR | 分支 | 合并提交 | 规模 |
|---|---|---|---|
| #13 | `fix/P0J.J2a-gold-standard` | `1155a4e3` | 5 个文件，+1055 / −646 |
| #15 | `fix/P0J.J4-process` | `69c14504` | 1 个文件，+30 / −10 |
| #17 | `fix/P0J.J3-smuggled-cleanup` | `8bbd1557` | 29 个文件，+1054 / −127 |
| #18 | `fix/P0J.J2-publish-delete-rules` | `6d8843b5` | 13 个文件，+922 / −263 |
| #23 | `fix/P0J.J3b-workspace-access-fail-closed` | `ca822e62` | 2 个文件，+71 / −11 |
| #25 | `fix/P0J.J1-evidence-base-r3` | `cd27b9b9` | 26 个文件，+6163 / −1197 |
| #26 | `fix/P0J.J2b-remove-disclaimer-skip` | `53bfb990` | 2 个文件，+93 / −23 |
| #32 | `fix/P0J.J5a-realrun-script-fixes-r2` | `4dfef0c6` | 11 个文件，+4559 / −1712 |
| #33 | `fix/P0J.J5b-g5-record-exchange` | `1bf645ee` | 2 个文件，+181 / −2 |
| #34 | `fix/P0J.J5c-g1-admission-floor` | `65d32f76` | 2 个文件，+72 / −19 |
| #35 | `fix/P0J.J5d-budget-settle-race` | `c4d10b94` | 5 个文件，+739 / −3 |
| #36 | `fix/P0J.J5e-g1-screenshot-wait-and-publications-logger` | `304b6924` | 2 个文件，+6 / −1 |
| #37 | `fix/P0J.J5f-g1-dismiss-onboarding` | `50ecb12d` | 1 个文件，+9 / −0 |
| #38 | `fix/P0J.J5g-g1-hide-getting-started` | `15ef5fc3` | 1 个文件，+5 / −0 |
| #39 | `fix/P0J.J5h-g2-byok-and-colloquial-qualifiers` | `89b9a7e5` | 7 个文件，+578 / −79 |
| #40 | `fix/P0J.J5i-g4-fresh-form4` | `0781c0e8` | 1 个文件，+35 / −1 |
| #41 | `fix/P0J.J5j-wait-for-idle-workspace-delete` | `9ba4bedf` | 7 个文件，+61 / −8 |
| #42 | `fix/P0J.J5k-journal-replay-time-and-g3-unique-edit` | `ad1b9544` | 4 个文件，+334 / −34 |
| #43 | `fix/P0J.J5l-render-g5-subitems` | `a432909c` | 2 个文件，+31 / −1 |
| #44 | `docs/P0J.J5-realrun-evidence-20261021` | `cd880f52` | 9 个文件，+762 / −0 |
| #45 | `docs/P0J.J6-docs-corrections` | `db11613b` | 7 个文件，+236 / −23 |

另有 PR #19、#21、#22、#24、#27、#28、#30、#31，都是 lead 写的决定记录（§8.1）。

**流程变化：** PR #32 起，代码由 Antigravity 写，grok 和 Muse 辅助审查，lead 审查、写 PR 描述、合并；少数小修由 lead 直接写。每个 PR 都用 `pr_baseline.sh` 发布基线结果。审查时照常对待这些 PR，不因为经过了辅助审查就放松。

### 0.3 审查规则（和前几轮相同）

- 每条结论附可复现的命令或 `文件:行号`。
- 不信 PR 描述和开发报告，以代码和原始证据为准。
- 发现的问题按"严重 / 高 / 中 / 低"分级，并标明是否挡住 P0 验收。

## 1. 先跑的命令

```bash
git checkout db11613b
uv run pytest tests/unit/ -q                     # 单测
./scripts/dalton/check_hooks.sh --strict --audit # 上游改动只允许挂钩
uv run python scripts/dalton/check_report_claims.py --strict \
  docs/dalton/dev-guide/P0J-round4-dev-report.md docs/dalton/dev-guide/PROGRESS.md \
  docs/dalton/dev-guide/trial-runs/realrun-2026-10-21/*.md
uv run python scripts/dalton/realrun/check_env.py # 在审查人自己的环境里必须全部通过
```

`check_report_claims` 会对证据摘要报 3 个误报（LLM 生成的两个指标名、broker 内部文件名），开发报告第 5 节和 PR #44 已说明。其余发现都要追查。

## 2. 挡住 P0 验收的核心问题（必须全部回答）

### 2.1 第四轮的结论是否真的解决了

| 第四轮问题 | 本轮声称的解决 | 要核实的事 |
|---|---|---|
| V1 证据在未提交工作区上跑 | 8 份证据都在已合并干净提交 `ad1b9544` 上，后端版本一致（L41） | 证据头部的 `git_sha`、`git_dirty`、`backend_git_sha` 是否真由脚本读取而不是写死；`--record` 在工作区不干净或未合并时是否真的拒绝运行（自己改一个文件试） |
| OSS 模式下全是 owner 账号 | 服务令牌加 `X-User-Id`（L40） | 证据的调用日志里 `user_id` 是否都是声明的测试用户；`local-dev-user` 的数据有没有被测试改动 |
| B1–B3 测试后门 | PR #9、#10、#17 删除，守门测试锁定 | `grep -rn "realrun-\|local-dev-user" src/` 有没有残留的按用户 ID 特判（L39） |
| V6 Antigravity 登录没走完 | G5.2 用真实回调完成令牌交换 | G5.2 证据里的回调交换状态；令牌行是否属于测试用户 |

### 2.2 审查人独立重跑 G1 和 G3（P0J §5.2）

在 `ad1b9544` 上、在审查人自己的环境里跑，**不用开发方的数据库。** 这一步同时检验"证据换个环境还能不能复现"。
- 先过 `check_env.py`。
- G1：429 拒绝、用户 B 同时正常、截图里能读出拒绝横幅、花费不超过 $0.50。
- G3：验收全部通过；你的环境里没有缓存，所以第 1–5 步应该全是新调用。对照开发方的结果，差异超过 LLM 正常波动的要追查。

### 2.3 PR #35：预算结算竞态（真实的资金漏洞）

- 读 `src/dalton/database/budget.py` 的 `reconcile_reservations_sql`。判断：在任何写入顺序下，挂在某个预约下的现金花费会不会漏计或重复计。
- 30 分钟兜底照抄上游 `settle_abandoned` 的双时钟（子运行和父运行都满 30 分钟），核对 `src/server/database/runs/credit_ledger.py` 的对应段落。
- 集成测试 `tests/integration/dalton/test_budget_settlement_db.py`：**只能在专用的测试库上跑**（`TEST_DB_*` 指向名字以 `_test` 结尾的库；上游的 fixture 会 `DROP SCHEMA public`）。确认把 `budget.py` 换回 PR 之前的版本时，用例会失败。

## 3. 本轮的其他生产代码改动

| PR | 要看的 | 关注点 |
|---|---|---|
| #17 | 去掉 `local-dev-user` 分支、启动门窄化 | 系统上下文是否统一用 `user_id is None`；启动失败是否仍对角色配置错误生效 |
| #18、#26 | 发布核验删规则 | L43：代码里不再有按词或按时长决定"不核验"的规则；被排除的数字在审批卡上逐条列出 |
| #23 | `_verify_workspace_access` | 查询失败时拒绝而不是放行 |
| #36 | `publications.py` 的 `logger` | 之前是未定义名字，正常路径上会抛 `NameError` |
| #39 | 口语数字带上约数限定词 | 否定形式不被截成正向（"不超过三成"）；复合词不误扩（"最近一半"）；提示词没有写 G3 夹具的答案（原有守门测试未放宽） |
| #42 | 调用日志记录回放时间 | `record_journal_replay` 的写法；证据里本次运行的回放不再被漏掉，旧行的历史花费不计入本次运行 |

## 4. 证据脚本的改动有没有让证据变弱

J5 期间证据脚本改了很多次（#32–#43）。每一处都要判断：**是修正了脚本对产品行为的错误假设，还是放宽了验收？**

- #34 G1 改为在准入门槛处停止花费，而不是等到名义上限。
- #39 G2 给测试用户写临时 DeepSeek key，结束后恢复，不覆盖已有 key。
- #40 G4 选一份该用户没分诊过的 Form 4。
- #42 G3 第 6 步的验收从"有回放"改为"窗口内同时有新调用和回放"。
- #37、#38 G1 截图前关闭引导弹窗和清单。

## 5. 文档

- `P0I-round3-dev-report.md` 末尾的 10 条勘误：抽查不少于 5 条，核对引用的 R4 文件和行号。
- `PROGRESS.md`：G4 是否如实写成"场景②内部文献子场景待跑"。
- `README.md` 的 L39–L43 与 P0J 第 7 节逐字一致。
- 开发报告的证据表是否为生成区块（L42）。

## 6. 已知、无需重新发现的问题

- 证据目录名 `realrun-2026-10-21` 与 10-19 的格式不同，只是标签。
- G4 场景②的内部文献子场景未完成，要等有真实新入库资料的交易日（P0J §5 G4）。
- G3 第 1–5 步回放了 `89b9a7e5` 上的结果，`src/` 与 `ad1b9544` 逐字节相同，摘要分列了新调用和回放。
- 上游 Market Insight 服务在后端启动时补跑任务，其进程内浏览器崩溃曾让后端无响应一次；未改上游。

## 7. lead 请审查人顺带判断的一点

开发环境的 `docker-compose.yml` 把后端（8000）和前端（5173）绑在所有网卡上，而 OSS 模式没有登录：同一网络里的任何人都能以 owner 身份使用、花 owner 的额度。这是上游的默认配置。请判断是否需要在 Dalton 的配置层改成只绑本机，作为单独的小 PR，不挡 P0 验收。

## 8. 交付物

- 审查报告放在 `docs/dalton/dev-guide/review/`，文件名带 `R5` 和日期；另写 `SUMMARY-R5-<日期>.md`。
- 问题表的每一行注明：严重程度、是否挡住 P0 验收、`文件:行号`、复现命令。

## 9. 进入 P0.9/P0.10 的判定标准

全部满足才进入：
1. 第 2.1 节四项全部坐实已解决；
2. 审查人独立重跑 G1 和 G3 通过；
3. PR #35 没有发现会漏计现金的路径；
4. 没有新的"严重"问题。

**进入之后马上做的事（防止项目继续停在证据上）：**
- **P0.9 走查和 P0.10 验收由 PM 本人在界面里看：** 用 OpenClaw 的模型问一个 META 的问题，看到流式输出、工具调用卡片、对 researchdata 的调用和来源；费用计入日额度，额度用完时运行暂停；同步按钮能更新模型列表（`README.md` 第 3 节 P0 里程碑）。这一步以 PM 的观感为准，不再另做证据工具。
- 然后转入 P1 入职（问"你对 META 的看法和主要 debate"）和 P2 初次覆盖（PM 盲评），这两个才是 Dalton 作为分析师的第一批真正产出。
