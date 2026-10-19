# 第四轮代码审查清单：P0I 修正与证据重做（`dalton/main` `8a85b106`）

版本 0.1，2026-10-19。写给负责审查的人。

**审查范围：** `925fd0f9..8a85b106`，即 P0I 指南提交之后的全部改动。依据：
- `P0I-round3-fixes.md`：本轮的开发指南，决定 L34–L38；
- `review/SUMMARY-R3-20261018.md` 与第三轮的 6 份审查报告；
- 开发方的 `P0I-round3-dev-report.md`；
- `PROGRESS.md`；
- `trial-runs/realrun-20261019/` 下的 9 份证据摘要；
- GitHub PR #1–#8。

## 0. 怎么用这份清单

### 0.1 规模和特点

- **8 个族，8 个 GitHub PR，都在 GitHub 上合并。** 流程形式上第一次符合 L32。

  | PR | 族 | 分支（以 GitHub 为准） | 合并提交 | 规模 |
  |---|---|---|---|---|
  | #1 | I4 前端回绿 | `fix/P0I.I4-web-green` | `46f42833` | 3 个文件，+57 / −18 |
  | #2 | I2 证据工具 | `fix/P0I.I2-evidence-tools` | `487497bc` | 5 个文件，+1,574 / −0 |
  | #3 | I3 供应商、启动、预算 | `fix/P0I.I3-providers-startup-budget` | `1a04cb71` | 7 个文件，+305 / −19 |
  | #4 | I5 自主议程低危项 | `fix/P0I.I5-autonomy-low-risk` | `dc1fd7a4` | 8 个文件，+320 / −18 |
  | #5 | I6 财务镜头锚点 | `fix/P0I.I6-lenses-unified-anchors` | `b2463c54` | 6 个文件，+465 / −28 |
  | #6 | I1 发布核验 | `fix/P0I.I1-publish-stop-false-verified` | `58e85d4e` | 23 个文件，+7,535 / −821（其中测试集和金标准约 5,100 行） |
  | #7 | I7 证据重做 | `fix/P0I.I7-realrun-evidence` | `7464c4a6` | 39 个文件，+5,874 / −144（**其中约 20 个是生产代码**，见第 2.1 节） |
  | #8 | I8 文档 | `fix/P0I.I8-docs-and-report` | `8a85b106` | 6 个文件，+262 / −56 |

- **8 个 PR 都没有任何审查记录**（`reviews=0`），全部由 `everflowinv` 自己合并。
- **lead 收到的开发总结和仓库对不上。** 交给 lead 的那份总结里，至少 14 个文件名、类名、脚本名在仓库里不存在：
  - `ResearchDocSource.test.tsx`、`evidence_recorder.py`、`EvidenceBundle`、`verify_dalton_budget_startup`、`oauth_flow.py`、`coverage_board.py`、`budget_config.py`、`pipeline.py`；
  - `g1_part_a_deepseek_budget.py`、`g2_knowledge_sync.py`、`g7_mcp_heartbeat.py`、`b_real_antigravity_chat.py`；
  - `en.json`、`zh.json`。

  另外，G2、G5、G6、G7 的验证内容也和 P0I 的定义不同，9 个证据的 sha256 里只有 G4 一个对得上。**审查一律以仓库、PR 的实际 diff 和 `var/` 下的原始证据为准，** 开发总结、开发报告、PR 描述都只作为待核对的说法。

### 0.2 审查规则（和前三轮相同）

- 每条检查给出 ✅ 通过 / ❌ 有问题 / ❓ 疑似，并写明依据：读了哪段代码、跑了什么命令、看了哪份证据。
- 严重程度分四级：严重、高、中、低，定义见 `REVIEW-CHECKLIST.md`。
- 只读审查：不改代码，不写生产数据，不登录供应商，不打印令牌或密钥。允许跑单元测试和离线脚本。
- **涉及 LLM 的检查允许少量真实调用，总花费控制在 $2 以内，在报告里写明花了多少。** 本轮要求审查人独立重跑 G1 和 G3（第 5.2 节），这部分花费也计入。
- 结果写进 `docs/dalton/dev-guide/review/R4-<模块>-<日期>.md`，汇总写进 `review/SUMMARY-R4-<日期>.md`。

### 0.3 本轮的审查重点

1. **生产代码里的测试后门。** 为了让真实运行跑通，PR #7 往生产代码里加了针对 `realrun-`、`test-user-` 用户的特殊分支（第 2.1 节）。这是本轮最严重的问题。
2. **证据和代码是不是同一个版本。** 9 份证据的 `git_sha` 都是 `58e85d4e`（I1 的合并提交）。可是 PR #7 之后又改了约 20 个生产文件，证据实际是在未提交的工作区上跑的。
3. **开发报告里的数字是不是来自证据。** G3 的召回率、调用次数、名义花费，G6 的 BABA 净现金占比，G2、G6 的 sha256，都和原始证据对不上（第 2.2 节）。
4. **I1 有没有用新规则替代被删掉的规则。** 新增的口语词正则、"非核验对象"的范围（第 2.3 节）。

## 1. 先跑的命令

```bash
cd /Users/everflow/Projects/dalton-langalpha && git checkout 8a85b106

# 四条基线。后端单测按 P0I 跑四个目录（开发报告声称 1,698 项），不只跑 tests/unit/dalton
./scripts/dalton/check_hooks.sh --strict --audit
uv run pytest tests/unit/dalton tests/unit/llms tests/unit/middleware/skills tests/unit/mcp_servers -q
node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs
pnpm --dir web typecheck && pnpm --dir web test -- --run                  # 声称 677 个文件、7,228 项
uv run python scripts/dalton/check_report_claims.py --strict

# 测试后门：生产代码里不应有任何针对测试用户 ID 的分支
grep -rnE "realrun-|test-user-|-paste\"|startswith\(\(?\"(realrun|test)" src/ plugins/ | grep -v "/tests/"

# 证据和代码的版本：每份证据的 git_sha，以及它和合并提交之间改过的生产代码
for f in var/dalton/trial-runs/realrun-20261019/*.json; do
  python3 -I -c "import json,sys;d=json.load(open(sys.argv[1]));print(sys.argv[1].split('/')[-1],d['git_sha'],d['started_at'])" "$f"
done
git diff --stat 58e85d4e 7464c4a6 -- src/ plugins/ | tail -1

# 证据摘要里写的 sha256 和原始文件是否一致
shasum -a 256 var/dalton/trial-runs/realrun-20261019/*.json
grep -h "sha256" docs/dalton/dev-guide/trial-runs/realrun-20261019/*.md | grep -oE "[0-9a-f]{64}"

# I1 的规则增长
diff <(git show 925fd0f9:src/dalton/publish/numbers.py | grep -oE "^_[A-Z0-9_]+_RE\b" | sort) \
     <(grep -oE "^_[A-Z0-9_]+_RE\b" src/dalton/publish/numbers.py | sort)
git diff 925fd0f9 8a85b106 -- src/dalton/publish | grep -nE "^\+.*re\.(compile|search|match|findall|sub)\("
git diff --shortstat 925fd0f9 8a85b106 -- src/dalton/publish
```

**每条命令的实际输出都附在报告里。** lead 预跑的结果：
- 后门 grep 命中 `src/dalton/app/router.py:81` 和 `src/dalton/llm/providers/antigravity/oauth.py:476-480`；
- `role_policy.py` 里还有一处以 `-paste` 结尾的特判；
- 9 份证据的 `git_sha` 全是 `58e85d4e`；
- numbers.py 新增了 `_COLLOQUIAL_HINT_RE`；
- publish 的 diff 里新增了 11 处正则调用。

## 2. lead 预审发现的疑点（最先回答）

### 2.1 生产代码里的测试后门和夹带（严重，必须全部回答）

| # | 问题 | 位置 | 怎么核实 |
|---|---|---|---|
| B1 | **PM 权限检查被绕过：** `user_id` 以 `realrun-` 开头的任何用户，都被当作 PM | `src/dalton/app/router.py:81`：`if user_id not in settings.knowledge.pm_user_ids and not str(user_id).startswith("realrun-")` | 列出这个检查保护的所有接口（发布审批、知识层正式字段等）。在 OSS 和托管两种认证模式下，确认用户 ID 能不能由用户自己决定。**无论能不能利用，都按严重报告：** 测试需要的权限应该通过配置 `pm_user_ids` 给测试用户，而不是在代码里放行 |
| B2 | **测试用户借用 owner 的 OAuth 令牌：** `realrun-`、`test-user-` 开头的用户，`get_valid_token` 返回 `local-dev-user` 的令牌；`role_policy` 里登录状态也借用 `local-dev-user` 的 | `src/dalton/llm/providers/antigravity/oauth.py:476-480`；`src/dalton/llm/role_policy.py`（PR #7 新增的 `connected` 回退，含 `-paste` 后缀特判） | 多用户部署下，一个用户可以用另一个用户的订阅凭据。**这也让 G5.2 的证据失效：** 测试用户的"Antigravity 已连接"和 4 轮工具调用，用的是 owner 的令牌，证明不了测试用户自己的登录链路 |
| B3 | **预算准入的门槛被改了：** 额度不高于最低余额门槛时，门槛被调成 $0.000001 | `src/dalton/budget/gate.py`（PR #7）：`effective_floor` | 这条改动是为了让 G1 在 $0.02 的额度下跑通，还是一个独立的修复？读 `floor_usd` 原来的用途：开始一轮对话至少要留多少余额。确认改动之后，低额度用户会不会在余额只够一次调用的情况下进入整轮，然后在中途被拒。**再确认：没有这条改动，G1 能不能通过** |
| B4 | **PR #7 夹带了约 20 个生产文件的改动，** PR 描述称为"运行中发现的修复" | `git show 43ee96ed --stat`：`verdicts.py`、`knowledge/service.py`、`knowledge/validate.py`、两个 schema 文件、`probes/director.py`（+87）、`board.py`、`antigravity/client.py`（+188）、`complete.py`、`assist.py`、`gate.py`、`role_policy.py`、`router.py`、`publish/{numbers,audit,match,review}.py`、`researchdata_bridge.py` | 违反 L38 的"不夹带"。**每个文件都按常规审查读一遍，** 第 3.7 节逐个给出结论。其中 `publish/` 的四个文件并入 I1 的审查（第 3.6 节） |
| B5 | **PR 描述里说的"Antigravity 在额度用尽时回退到 CLI 网关"，** 在 `antigravity/client.py` 里找不到 | PR #7 描述第 3 条；`client.py` 的 +188 行里，lead 只看到 `_sanitize_gemini_schema` 和一处 `except Exception` | 如果回退存在，它违反 L26（不提供回退）以及审稿的家族隔离：调用会被悄悄换成另一个家族的模型。如果回退不存在，PR 描述不实。两种情况都要写明 |

### 2.2 证据和报告的疑点

| # | 疑点 | 依据 | 怎么核实 |
|---|---|---|---|
| V1 | **证据是在未提交的工作区上跑的。** 9 份证据的 `git_sha` 都是 `58e85d4e`；但 `58e85d4e..7464c4a6` 之间改了约 20 个生产文件 | 第 1 节的命令 | 哪些改动是在证据运行之前做的、哪些在之后？证据里没有记录工作区是否干净。**合并后的代码 `8a85b106` 从来没有跑过证据。** 建议的结论：证据整体退回，用入库的脚本在合并后的提交上重跑（第 5.2 节），证据头部增加 `git_dirty` |
| V2 | **G3 的数字和原始证据对不上** | 开发报告第 3.1 节对 `g3_publish.json` 的 `evaluation` 和 `journal.totals` | 逐项对照：① 金标准里有来源的数字，报告写 61 个、召回 61/61，证据是 36 个、核实 33 个、误判"无来源" 1 个、"未能判断" 2 个；② 调用次数，报告写新调用 78、回放 1，证据是 139 和 87；③ 名义花费，报告写 $0.214，证据是 $1.77；④ AMZN，报告写全文 74 个数字，但金标准里 AMZN 3 个节就有 87 个数字。**按 P0I §1.8，误伤是 3/36 = 8.3%，仍在 10% 以内；但报告里的数字是哪里来的？** |
| V3 | **报告自定了验收线。** 报告写"真阳召回标准要求 ≥75%"、金标准范围外抽 25 个 | P0I §1.8：误伤不超过有来源数字的 10%（即召回至少 90%），金标准范围外抽 30 个 | 按 P0I 的标准重新判定。审查人从金标准范围外再补抽，凑够 30 个 |
| V4 | **G2、G6 的 sha256，报告和原始文件不一致；** 摘要和原始文件是一致的 | 开发报告第 3 节：G2 写 `dc736110…`、G6 写 `0dc12374…`；原始文件是 `5b0a0452…`、`2d5cb497…` | 证据在报告写完之后又重新生成过？以哪一次为准？ |
| V5 | **G6 的 BABA 严格净现金占比：报告写 19.13%，证据写 21.39%** | 开发报告第 2.5 节；`G6.md` 第 3 节 | 第三轮审查人统一锚点后算出的是 19.13%。现在的 21.39% 用的是同一组锚点（市值 271,437.86、汇率 0.149772），为什么结果不同？是镜头脚本的计算口径变了，还是输入的财务数据变了？另外，证据的 `started_at` 和 `ended_at` 是同一秒，12 次脚本运行在 1 秒内完成：anchors 是当场联网取的，还是读了现成的文件？ |
| V6 | **G5.2 的 Docker 粘贴回调登录没有完成。** 证据里状态正确的那次回调返回 400 | `G5.2.md`：`matching_state_pkce_verified=400` | P0I 要求在 Docker 部署下**走完一次**粘贴回调地址的登录。现在只验证了拒绝路径。后端是在 Docker 里运行的吗？4 轮工具调用用的是不是 B2 借来的令牌？ |
| V7 | **G5.1 和 G5.2 用的是同一份证据、同一张调用记录表** | `G5.1.md` 和 `G5.2.md` 第 4 节完全相同 | G5.1 的"断开重放"和"重启后 409"在 broker 自己的日志里，不在 `dalton_llm_journal` 里。证据里有没有 broker 日志的对应记录？ |
| V8 | **G2 枚举到 22 条路由，第三轮审查数过是 24 条** | `G2.md`：通过 `GET /openapi.json` 枚举 | `include_in_schema=False` 的路由不会出现在 OpenAPI 里。用应用的路由表重新数一遍。另外，"assist 配成 contributor 时启动失败"是怎么测的：脚本重启了真实后端，还是直接调用了校验函数？ |
| V9 | **G4 的场景②标为 🟦** | `G4.md`；开发报告第 3 节 | P0I §7 允许这一场景如实标为 🟦，但 §11 要求 G4 整体达到 🟨。这一项由 lead 决定是否接受，审查报告里单独列出 |
| V10 | **开发报告的分支名和文件归属不对** | 开发报告第 1 节、第 2.3–2.4 节 | ① I4、I6、I1 三个分支名和 GitHub 上不一致（报告写 `frontend-green`、`lenses-anchors-flag`、`publish-audit`）；② I3 一节列了 `role_policy.py`、`assist.py`、`test_router_wiring.py`，但这三个文件都不在 PR #3 里（前两个在 PR #7）；③ I5 一节写内部库探针的三个路由是 `research`、`news`、`community`（原来是 sales-notes、alphaengine、guidepoint），又写改了 `dalton-agenda/SKILL.md`，但 PR #4 改的是 `make_price_triggers.py`。逐条核对 PR #3、#4 的实际 diff |
| V12 | **（审查中补充，本轮最大的新发现）证据里的"测试用户"实际上是 owner 本人。** `.env` 没有设 `INTERNAL_SERVICE_TOKEN`，OSS 模式下 `get_current_user_id` 对所有请求都返回 `local-dev-user`，请求头里的 `X-User-Id` 不起作用 | `src/server/utils/api.py:83-91`：服务令牌路径只有在设置了 `INTERNAL_SERVICE_TOKEN` 时才生效，否则 `HOST_MODE == "oss"` 直接返回 `LOCAL_DEV_USER_ID` | 带一个陌生的 `X-User-Id` 调用 PM 接口：如果放行，说明请求者其实就是 owner（PM）。**后果：** G1 的测试用户预算，以及 G2、G3、G4 按用户隔离的说法，全部没有被真正测过；B2 的借用令牌在这个部署里甚至是多余的。核对每份证据的 `test_user_id`，在 `dalton_llm_journal`、`conversation_usages` 里实际记的是谁 |
| V11 | **`check_report_claims.py` 的 `all_clean` 证明不了报告是真的** | PR #8 改了这个工具：默认扫描列表去掉了 P0H 报告；单测改为断言真实的 PROGRESS.md 和 P0I 报告"干净" | 这个工具只检查标识符和路径存不存在，查不出 V2–V5、V10 这类错误。① PR #8 对工具的改动是否合理（P0H 报告保留了原文、只追加勘误，原文里的错名字仍在）；② 单测绑定真实文档的内容，文档一改测试就会坏，建议改用固定的样例文件 |

### 2.3 发布核验的疑点

| # | 疑点 | 位置 | 怎么核实 |
|---|---|---|---|
| R1 | **新增了一条中文口语词正则，用来决定哪些节送给 LLM 扫描。** 开发报告却写"新增 0 个正则" | `numbers.py:1124` 的 `_COLLOQUIAL_HINT_RE`（`[一二…十]成|百分之|分之|翻倍|翻一番|腰斩|过半|减半`），在 `:1183` 用作条件 | 草稿里有数字的情况下，一个没有抽到数字、也不含这几个词的节，**根本不会送给 LLM 扫描口语数字**。L30 删掉的正是这类词表；L35 要求覆盖全文。用一个只含"近一半""大半""砍掉一半""倍增"的节试一次，看它会不会被扫描 |
| R2 | **"非核验对象"的范围超出了 P0I 的定义** | `g3_publish.json` 的 `evaluation.incidental_sample_30`；两份报告共排除 59 个 | P0I §1.4 列的是点赞数、浏览量、状态码、文件和文档编号、版本号、序号、页码。抽样里被排除的还有"AEP 702MW 燃料电池装机""实测 58 次购物""合同期 10 年"，**这些是研究报告里的事实陈述，应该进入核验。** `misclassified_financial_incidental_count=0` 只统计了带货币、百分比等单位的数字，统计不到这类错误。**审查人逐个看完全部 59 个**，统计有多少不该排除 |
| R3 | publish 的 diff 里新增 11 处正则调用 | 第 1 节的 grep | 逐条判断是掩码和格式识别，还是语义规则 |
| R4 | **金标准由开发者编写，和被验收的代码在同一个 PR 里；没有复核记录** | `tests/fixtures/dalton/g3/gold_standard.json` 只有 `schema_version`、`description`、`summary`、`items` | P0I §1.8 要求审查人或 PM 复核所有标为"有来源"的条目。**审查人复核全部 36 个有来源的条目，再抽 30 个无来源的条目**，确认金标准本身没有偏向代码 |
| R5 | **豁免标记的列表变了；S5 用的标记在代码和证据里都找不到** | `numbers.py:74` 的 `_EXEMPT_MARKER_RE` 现在是 示意、假设、估计、举例；开发报告写 S5 用的是 `[常识豁免]`，在 `src/` 和 `g3_publish.json` 里都搜不到 | ① 豁免标记的列表是谁定的，和 `dalton-publish` SKILL.md 里写给分析师的列表一致吗？P0H 列过的"测算""敏感性""情景"去哪了？② S5 实际用的是什么标记，结果是什么 |
| R6 | **PR #7 又改了 numbers.py（+145）、audit.py、match.py、review.py** | `git show 43ee96ed --stat` | I1 合并之后，I1 的代码又被改过一次，而且这次没有对应的 I1 测试更新。I1 的审查以 `8a85b106` 的代码为准，第 3.6 节的每一条都要在这个版本上验证 |

## 3. P0I 的逐条核对

不看提交说明，看代码和运行结果。第 2 节已经列出的项不用重复，在这里引用编号即可。

### 3.1 I4 前端（PR #1）

- [ ] 第 1 节的前端命令在 `8a85b106` 上通过。
- [ ] 测试数据补了必填字段，**组件的类型没有被放宽。**
- [ ] 至少一个组件测试用真实的 i18n 实例加载 `en-US`，断言横幅显示的是英文。
- [ ] P0I §4 第 3 条要求列出 Dalton 组件其余缺键的清单，留给 P0.9 界面走查。这张清单在哪里？

### 3.2 I2 证据工具（PR #2，以及 PR #7、#8 的后续改动）

- [ ] `journal_dump.py`：确认是按用户和时间窗口直接查询数据库，不经过可能被缓存的接口；输出不含请求和响应正文。
- [ ] `_common.py` 的 `validate_synthetic_declarations`：
  - 只允许 P0I §2.1 第 4 条的四类注入；
  - **G3 用 `knowledge_api_agenda` 这一类声明了"导入证据"，** 而 P0I 原文这一类指的是修改议程。是声明错了类别，还是工具对类别的检查太宽？
- [ ] 证据头部有没有记录工作区是否干净（见 V1）。没有的话，作为本轮的修正项。
- [ ] `check_report_claims.py`：见 V11。

### 3.3 I3 供应商、启动与预算（PR #3）

| P0I 编号 | 要求 | 核对 |
|---|---|---|
| P1 | `RolePolicyError` 让启动失败；`DALTON_BUDGET_GATE=1` 而迁移缺失时也失败；其他异常维持 warning；加 `DALTON-HOOK` | 读 `setup.py` 的改动（+6 行）。**在真实后端上验证一次：** assist 配成 contributor 变体，进程退出，退出码非 0 |
| P2 | 30 天清理接进研究总监的每日预检 | 开发报告写的是"在启动时调用"。PR #3 也改了 `probes/director.py`（+12 行），看清楚到底接在哪里。**只在启动时调用不满足要求：** 长时间运行的服务永远不会再清理；多 worker 启动时每个进程都会跑一遍 |
| P3 | 删掉 `_LAST_INITIATED_AT`，发起登录的时间写进数据库 | 开发报告写的是"60 秒内的 `oauth_initiated` 记录作为冷却"。P0I 要防的是**把登录之前的旧令牌误判为登录完成**，不是限制登录频率。确认守卫的语义没有变 |
| P4 | 读取自带 key 时打 debug 日志 | 开发报告写成了"支持扁平 `api_keys` 结构"，这是新的读取逻辑，P0I 没有要求。确认没有破坏第三轮已经验证过的嵌套结构读取 |
| P5 | `test_budget_port.py:202, 818` 的客户端替身改用 `create_autospec` | 看 +215 行的改动 |
| P6 | `complete.py` 里 `complete_journal_failed` 加保护 | 读改动 |

### 3.4 I5 自主议程低危项（PR #4）

逐条对照 P0I §5 的 7 项：
- [ ] **#3 内部库探针：** 三个路由仍然是 sales-notes、alphaengine、guidepoint 吗（见 V10）？并行之后整步时限是 10 秒吗？
- [ ] **#4 认领失败返回跳过：** 跳过原因是 `claim_failed`，信号保留到下一次预检。
- [ ] **#6 冷却时间：** P0I 的决定是**改模板**，写成 86400。PR #4 没有改动 `automation-templates/`，改的是 `make_price_triggers.py`。确认模板和代码现在一致。财报节点按交易日计算，时刻是 08:00 和 20:00。
- [ ] **#7 预检读预算出错：** 维持放行，日志级别改为 error，有注释写明理由。
- [ ] **H3 的主体逻辑没有被改动**（P0I 第 0 节）。看 `preflight.py` 的 diff，只应该有 #4 和 #7 两处。

### 3.5 I6 财务镜头（PR #5）

- [ ] 三个镜头脚本都支持 `--anchors`；同时给出手填的市值或汇率时报错退出，退出码为 2。
- [ ] SKILL.md 的命令示例先跑 anchors、再传给其他镜头。
- [ ] **审查人自己按 SKILL.md 的新命令对 BABA 跑一遍，** 结果和 G6 的证据对比，回答 V5。
- [ ] 对比文档里的数字和 G6 的证据一致。

### 3.6 I1 发布核验（PR #6，加上 PR #7 对 publish 的改动）

**以 `8a85b106` 的代码为准。**

**L34：无脚注数字的"已核实"只能来自 D 环节**
- [ ] 读 `match.py`：Order 0b、Order 4、无脚注的目标价，都不再设置 `matched=True`，只挂候选。枚举 `match.py` 里所有能让 `matched=True` 的位置，确认每一个都对应 Order 0–3 的脚注路径，或者 Order 5 的推算。
- [ ] Order 5 只用 Order 0–3 或 D 环节核实过的数字做推算。
- [ ] SF-CN 特例已删掉。
- [ ] **`legacy_thesis`：**
  - schema 里加了新类型，迁移脚本也改了。
  - **P0I §1.3 第 3 条要求对四家已迁移公司的活库追加修正版本，** 开发报告没有提到。通过知识层接口查 META、AMZN、GOOGL、MSFT 的 `thesis` 版本历史，确认修正版本存在、原因写明。
  - 用一个只在 `legacy_thesis` 里出现的数字验证：结论不是"已核实"，候选里有"旧认知中的说法"的提示。

**L35：B 环节全量标注**
- [ ] 标注覆盖全部数字，每批不超过 40 个，按节切分。
- [ ] **口语数字的扫描覆盖全文：** 见 R1。
- [ ] LLM 的指标、期间、类型优先于启发式。用一个启发式和 LLM 期间不一致的数字验证。
- [ ] `incidental` 的守卫：带货币、百分比、bp、倍数、每股单位的数字不能被排除。**范围问题见 R2。**
- [ ] 审批卡上能看到"已标注 N/N"和被排除数字的折叠列表。
- [ ] 任意一批失败时 `labeling_incomplete=True`，审批卡写明是哪一批；失败批次的数字不能被排除为 `incidental`。

**L36：锚定的确定性检查**
- [ ] **量纲：** "19.11 美元/股"对"19.1%"不再核实。
- [ ] **期间：** 引文里有明确期间、而且和草稿不一致时，判"未能判断"。G3 证据里有两个 AMZN 数字（F-089、F-093）因为"引文期间与正文不一致"被判为"未能判断"，金标准却认为它们有来源。**读这两个数字的原句和引文，判断是检查本身有问题，还是金标准标错了。**
- [ ] `quote_metric_raw` 是必填字段，代码核对它逐字出现在 `quote` 里。
- [ ] 第三轮查出的四个越权分支都已修正。`JUDGE_SCHEMA` 里没有 `source_value`、`source_unit`。
- [ ] 有没有一个测试枚举所有能产生 `contradicted` 的代码位置（P0I §1.5 最后一段）？

**§1.6 D 环节**
- [ ] 每批 10 个，按节分批；修改一节只让这一节的缓存失效。G3 证据里有"单节改写"的场景，核对调用日志：只有改动的那一节产生了新调用。
- [ ] 候选数量规则：证据不超过 30 条时全部作为候选。第三轮的截断问题已修复（GOOGL 的 2,425 亿现金、5,140 亿积压能找到正确证据）。
- [ ] 总时限是 `max(120, 待判数字数 × 2)` 秒，上限 900 秒。
- [ ] "无来源"的条目附了候选。

**§1.7 其他**
- [ ] `check_draft` 和 `publish_report` 走同一个 `aaudit_draft`；同步的 `audit_draft` 生产入口已删除。
- [ ] 死代码已删除；`llm_service` 参数的去留有说明。
- [ ] 审批卡新增的五块内容都有组件测试。

**G3 的验收：** 见第 5.2 节，审查人独立重跑。

### 3.7 PR #7 夹带的生产代码

对第 2.1 节 B4 列出的每个文件，回答三个问题：
1. 改了什么，为什么要改；
2. 是修复了一个真实的 bug，还是为了让证据通过而改的（B1–B3 属于后者）；
3. 有没有对应的测试。

**重点看下面几处：**
- **`database/verdicts.py`：** 结论编号改为全局唯一，用按日期的 advisory lock。确认多 worker 下没有死锁或编号跳号，是否需要迁移。
- **`knowledge/service.py`、`knowledge/validate.py`、两个 schema 文件：** 改了结构定义，就要确认已有的知识文件仍然能通过校验；改动不能绕过三条硬规则。
- **`probes/director.py`（+87）、`board.py`：** 按工作区所有者过滤查询。这改的是第三轮已经审过的 H3 代码，确认五个"有事可做"的条件没有变。
- **`antigravity/client.py`（+188）：** 嵌套 JSON Schema 的清洗、`thoughtSignature` 跨轮透传，以及 B5。
- **`researchdata_bridge.py`：** 延迟钩子默认关闭，只有在 `DALTON_BRIDGE_TEST_MODE=1` 时才生效。

### 3.8 I8 文档（PR #8）

- [ ] README 第 6 节加入了 L34–L38，内容和 P0I 第 9 节一致。
- [ ] P0H 报告的勘误：逐条对照 P0I §8.2 的 10 项。
- [ ] **PROGRESS.md：** 逐行对照 P0I §8.1 的表；每一个 🟨 的证据都指向 `trial-runs/realrun-20261019/` 下的摘要；**按本轮审查的结论（第 5 节），重新判定每一行的档位。**
- [ ] 开发报告：第 2.2 节查出的数字、分支名、文件归属错误，在汇总里列出，交给下一轮作为勘误。

## 4. L34–L38 专项审计

### 4.1 规则还在长吗（L30、L34、L35）

1. 用第 1 节的命令统计 `src/dalton/publish` 在 `925fd0f9..8a85b106` 之间的净变化，扣掉测试集和金标准，分 PR #6、PR #7 两部分列出。
2. 新增的正则和正则调用逐条判断（R1、R3）。
3. 冻结的词表有没有新增条目。
4. 新增的确定性判断有没有在做语义判断，比如 `_can_be_incidental` 里"量级大于 1"的条件。
5. **结论：** 规则还在长吗？

### 4.2 证据规范（L37）

对照 P0I §2.1 的 6 条，逐份证据检查：

| 证据 | 脚本入库 | 通过真实接口 | 头部字段齐全 | 调用日志对账 | 非真实成分已声明，且在允许的四类内 | 关键调用 `replayed=false` | 摘要入库，sha256 一致 |
|---|---|---|---|---|---|---|---|
| G1 | | | | | | | |
| G2 | | | | | | | |
| G3 | | | | | | | |
| G4 | | | | | | | |
| G5.1 | | | | | | | |
| G5.2 | | | | | | | |
| G6 | | | | | | | |
| G7 | | | | | | | |

**"通过真实接口"一列特别注意：** 脚本有没有导入 `src/dalton` 的内部函数？`grep -n "from src" scripts/dalton/realrun/*.py`。G4 的 `watch_state_unsee_filing` 写的入口是数据库表本身，这是 P0I 允许的，但要确认脚本只改了"已看到"集合，没有写信号表。

### 4.3 报告纪律与流程（L38）

1. **数字附命令：** 抽开发报告里 10 个数字，按它附的命令或证据重新得出。第 2.2 节已经发现的不算在内。
2. **测试后门**（B1–B3）写进 L38 的执行情况：测试需要的权限、凭据和额度，应该通过配置或测试数据提供，不能在生产代码里按用户 ID 特判。建议把这一条补进 L38。
3. **PR 没有审查记录：** L32 要求一族一个 PR，目的是让每一族在合并之前被审查。8 个 PR 都是自己合并、零审查。由 lead 决定以后的合并门槛（比如要求至少一个审查通过），汇总里写明。
4. **夹带**（B4）。

## 5. 真实运行证据的复核

### 5.1 档位

按 P0F.5 的三档判断。**第 2.1 节的 B1–B3 坐实时，依赖这些后门的证据一律按 🟦 算。**

| 族 | P0I 要求 | 证据 | 本轮重点核实 | 档位 |
|---|---|---|---|---|
| G1 | 通过对话接口发起，现金子智能体真实花费触顶，下一轮被拒（429），前端截图，总花费不超过 $0.50 | `G1.md`、`g1_budget.json`、截图 | B3、V1；子智能体那一轮的调用日志记录，用量大于 0 | |
| G2 | 从路由表枚举全部路由；至少一个现金供应商的探测成功并计入；contributor 配置下启动失败 | `G2.md` | V8 | |
| G3 | P0I §1.8 | `G3.md`、`g3_publish.json` | V2、V3、R1–R6；第 5.2 节的独立重跑 | |
| G4 | 经真实的自动任务触发；只用允许的注入；`preflight.triage` 有真实调用 | `G4.md` | V9；场景①的公告真实存在；看板前后对比 | |
| G5.1 | broker 重启后同 ID 重试返回 409；断开后重试拿到完整结果，只计费一次 | `G5.1.md` | V7 | |
| G5.2 | 测试用户自己登录 Antigravity，至少 4 轮工具调用；Docker 下走完粘贴回调地址的登录 | `G5.2.md` | B2、V6 | |
| G6 | BABA、0700.HK、META 按新命令跑通，三组输出锚点一致 | `G6.md` | V5 | |
| G7 | 用入库的延迟钩子，超过 240 秒的读取成功返回，并已声明；另有一次真实内容的读取 | `G7.md` | 钩子默认关闭；真实读取的内容不是占位文本 | |

### 5.2 审查人独立重跑（P0I §7.2、§11 第 4 条）

在 `8a85b106` 上，确认工作区干净，用入库的脚本**独立重跑 G1 和 G3**：
1. 先把 B1–B3 的后门在本地临时去掉（不提交），再跑。**能跑通，说明后门不是必需的；跑不通，说明证据依赖后门，** 在报告里写明卡在哪一步。
2. G3 重跑之后，按 P0I §1.8 的标准逐项判定，不用开发报告的验收线。另外完成 R2（59 个被排除的数字）、R4（金标准复核）。
3. 结果和开发方的证据逐项对比，差异逐条说明。
4. 花费计入第 0.2 节的 $2 预算；G1 按 P0I 的要求不超过 $0.50。

## 6. 常规审查

和前几轮相同的通用检查项：
- 多 worker 下状态以数据库为准；
- 日志里没有凭据；
- 没有静默吞掉错误；
- 改动上游的地方都有 `DALTON-HOOK` 标注。

本轮额外检查：
- [ ] **按用户 ID 特判的代码：** 除了 B1–B3，全仓库搜一遍 `startswith(` 加用户 ID 的写法，以及 `local-dev-user` 的新增引用（`git diff 925fd0f9 8a85b106 | grep "^+.*local-dev-user"`）。
- [ ] **`antigravity/oauth.py` 的令牌借用路径：** 有没有把 owner 的令牌信息写进日志或证据文件。`grep -rn "richard\|@gmail" var/dalton/trial-runs docs/dalton/dev-guide/trial-runs`：G5.2 的摘要里写了 owner 的邮箱，入库的摘要不应包含个人信息。
- [ ] **`setup.py` 的改动是上游文件，** 有 `DALTON-HOOK` 标注；`check_hooks.sh --audit` 通过。

## 7. 交付物

| 报告 | 内容 |
|---|---|
| `review/R4-backdoors-and-smuggled-code-<日期>.md` | 第 2.1 节 B1–B5、第 3.7 节 |
| `review/R4-evidence-audit-<日期>.md` | 第 2.2 节 V1–V11、第 4.2 节、第 5 节，包括独立重跑的结果 |
| `review/R4-publish-<日期>.md` | 第 2.3 节 R1–R6、第 3.6 节、第 4.1 节 |
| `review/R4-families-<日期>.md` | 第 3.1–3.5 节、第 3.8 节 |
| `review/SUMMARY-R4-<日期>.md` | 汇总：问题统计、档位表、能否进入 P0.9/P0.10 的结论、建议的修正顺序，以及 L38 的修订建议 |

## 8. 建议的审查顺序

1. **第 2.1 节 B1–B3（测试后门）。** 一旦坐实，相关证据的档位直接受影响，而且这是需要马上修的安全问题，不必等审查结束。**建议 lead 在审查一开始就通知开发方。**
2. **第 2.2 节 V1（证据和代码的版本）和 V12（身份模型）。** 两者共同决定现有的证据是否还能用，还是整体重跑。
3. **第 5.2 节独立重跑 G1 和 G3，** 同时完成 R1、R2、R4。
4. **第 3.6 节 I1，第 3.7 节夹带的代码。**
5. **第 2.2 节其余各项，第 3.1–3.5 节、第 3.8 节。**
6. **第 4 节、第 6 节。**
7. **第 5.1 节汇总档位。**

## 9. 进入 P0.9/P0.10 的判定标准

和 P0I §11 相同，并补充两条。同时满足以下条件，才建议进入：

1. 没有未修复的严重和高危问题。**生产代码里没有任何按测试用户 ID 特判的分支。**
2. G1、G2、G3、G4、G5.1、G5.2、G6、G7 全部达到 🟨，证据符合 L37，**而且是在合并后的、干净的提交上生成的。** G4 场景②的 🟦 是否接受，由 lead 决定。
3. G3 按 P0I §1.8 的原始标准验收通过；被排除的"非核验对象"里没有事实陈述类的数字。
4. 审查人用入库的脚本独立重跑 G1 和 G3，**不依赖任何后门，** 结果和开发方的证据一致。
5. HEAD 上的四条基线命令全部通过。
6. 开发报告里的数字能按它附的命令重新得出。`check_report_claims.py` 通过只是必要条件，不是充分条件。
