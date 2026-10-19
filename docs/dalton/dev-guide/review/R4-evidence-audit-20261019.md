# R4 证据与版本审计（V1–V11、L37 证据规范、§5.1 档位）

审查人：ZCode 审查智能体 　日期：2026-10-19 　范围：`925fd0f9..8a85b106`（PR #1–#8；PR #9 `d1d8c098` 为后门移除，仅引用）

依据：`REVIEW-CHECKLIST-R4.md` §2.2/§4.2/§5.1；原始证据 `var/dalton/trial-runs/realrun-20261019/`（8 份 JSON + 1 截图）；摘要 `docs/dalton/dev-guide/trial-runs/realrun-20261019/`（9 份 md）；入库脚本 `scripts/dalton/realrun/*.py`；`gh pr view/diff`。只读审查，未改任何文件、未登录供应商；个人信息已打码。G1/G3 独立重跑由 lead 执行，本文相应位置标注"见 lead 重跑结果"。

## 结论

- **V1 坐实（严重）：全部 7 份 JSON 证据都在未提交工作区上跑，`git_sha` 全是 `58e85d4e`，而 PR #7 在证据全部跑完之后才提交（`43ee96ed`，12:24:57Z）；合并后的 `7464c4a6`/`8a85b106` 从未跑过证据。证据头部无 `git_dirty` 字段（P0I §2.1 第 2 条的头部规范本身也没规定该字段——规范缺口）。另：`g1_budget.py` 在证据生成后被 PR #9 再改（额度默认 $0.02→$0.30），HEAD 上的脚本与生成证据的脚本不一致。建议：**证据整体退回，在合并后干净提交上重跑（§5.2），证据头部增加 `git_dirty`。**
- **V2 坐实（严重）：开发报告 §3/§3.1 的 G3 数字（召回 61/61、调用 78/1、名义 $0.214313、AMZN 74 个数字、抽样 25）与原始证据（36 有来源/核实 33、139/87、$1.768686、金标准 AMZN 87 条、范围外核实共 24 个全查）和入库摘要 G3.md 全都对不上；仓库内没有任何工件能得出报告那组数。最可能来自 11:21Z 最终重跑之前被覆盖的旧版证据——无论来源如何，报告把正确的 sha256（`5c9c2249…`，与现行文件一致）与旧数字混排，自相矛盾，违反 L38"数字附命令/证据"。
- **V3 坐实：报告自定"真阳召回 ≥75%"验收线、范围外抽 25 个**；P0I §1.8 原文是误伤 ≤10%（即召回 ≥90%）、范围外抽 30。入库脚本 `g3_publish.py` 的 `acceptance_pass` 用的是 P0I 原阈值（≤0.10）——**改标准的只发生在报告层，不在代码/证据层**。按 P0I 原标准重判现行证据：误伤按字面（判"无来源/有矛盾"）1/36=2.8%，计入 2 个"未能判断"（F-089、F-093）为 3/36=8.3%，仍在 10% 以内；召回 33/36=91.7%≥90%；错放 0；交叉对照 0；范围外已核实的 24 个（全部存在者）错放 0——按证据本身 G3 达标，但金标准尚待 R4 复核、且整份证据因 V1/B1 退回。
- **V4 坐实：摘要与原始文件 sha256 全部一致（8/8，含截图）；开发报告里 G2（`dc736110…`）、G6（`0dc12374…`）两个 sha256 与原始文件（`5b0a0452…`、`2d5cb497…`）不一致。** 报告行内 G1/G3/G4/G5/G7 的 sha256 又与现行文件一致——同一张表新旧混排。以**摘要+原始文件**为准（两者互证一致）；报告的 G2/G6 两行 sha256 无法追溯到任何现存工件。这同时说明"报告数字不可信"不是孤立笔误。
- **V5 坐实：21.39% 与 19.13% 是同一组锚点下"受限现金扣一次还是扣两次"的口径差**，且 G6 证据的锚点/财务输入全部硬编码在脚本里（详见下文）。报告 §2.5（19.13%）与自己的证据（21.39%）不一致。
- **V6 坐实：G5.2 粘贴回调登录没有走完**——状态与 PKCE 均正确的那次回调返回 400（`Token exchange failed: HTTPStatusError`），只验证了拒绝路径；且"已连接"状态是把 owner 的令牌直接写库制造的（B2 + 脚本层直写，均未声明）。P0I §7 G5.2 要求"在 Docker 部署下**走完一次**粘贴回调地址的登录"——未满足，G5.2 按 🟦。
- V7：G5.1 与 G5.2 共用一份证据/一张日志表属实；G5.1 自身的断开重连与重启 409 有 broker 侧的独立佐证（health 前后快照：`started_at` 变化、journal 67→68、indeterminate 3→4），不依赖 B2，可独立成立（仍受 V1 整体退回约束）。
- V8：**路由数 22 是对的**——`src/dalton/app/router.py` 在基线 `925fd0f9` 与 HEAD 完全一致，均为 22 个 `(method, path)`，无 `include_in_schema=False`，OpenAPI 枚举不漏；第三轮"24 条"的说法在代码层面复算不出来。但 **"contributor 启动失败"不是重启真实后端测的**：子进程 `python -c` 直接 import `verify_credit_gate_wiring`/`role_policy` 并 `mock.patch` 配置加载——内部函数直调 + 未在 synthetic 声明，该子项不满足 L37。
- V9：G4 场景②"当天内部资料无新入库，如实标 🟦、不合成"，符合 P0I §7 的允许项；但 §11 要求 G4 整体 🟨，两处条文的冲突由 lead 决定（见档位表）。
- V10 坐实：3 个分支名、3 个文件归属、内部探针路由名、SKILL.md 改动归属全部与 PR 实际 diff 不符（逐条见下）。
- V11 坐实：PR #8 把 `check_report_claims.py` 默认扫描从 P0H 报告改成 P0I 报告（P0H 降为兜底）、单测从固定样例改为断言真实 PROGRESS.md 与 P0I 报告"干净"；该工具只查标识符/路径存在性，V2–V5、V10 的错误一类也查不出。`all_clean` 是必要非充分条件，PR #8 的改动让"绿测试"更像背书。

---

## V1–V11 逐条

### V1 证据在未提交工作区上跑 ❌（严重）

时间线（全部 UTC；`git log --format=%ci` 与各 JSON 的 `started_at/ended_at` 复核）：

| 事件 | 时间 |
|---|---|
| PR #6 合并 `58e85d4e`（证据声称的 HEAD） | 07:53:34 |
| G2 证据运行 | 08:18:14–08:18:25 |
| G6 证据运行 | 08:22:01（1 秒） |
| G5（含 5.1/5.2）证据运行 | 09:11:07–09:13:24 |
| G7 证据运行 | 09:11:13–09:15:31 |
| G1 证据运行 | 10:14:46–10:15:58 |
| G3 证据运行 | 11:21:21–11:24:42 |
| G4 证据运行 | 12:12:58–12:15:29 |
| PR #7 单提交 `43ee96ed`（9 摘要 + 10 脚本 + **20 个 src 生产文件**） | **12:24:57（在所有证据之后）** |
| PR #7 合并 `7464c4a6` | 12:25:36 |
| PR #8 合并 `8a85b106` | 12:46:24 |
| PR #9 后门移除 `d5b49563` + 邮箱打码 `eeb1e2ca`（改 `g1_budget.py` 5 行） | 14:58:02 |

- 7 份 JSON 的 `git_sha` 全为 `58e85d4e`，`git_dirty` 字段**全部缺失**（P0I §2.1 第 2 条的固定头部清单里也没有这个字段——规范本身要补）。
- PR #7 的全部改动（含 `router.py` B1、`gate.py` B3、`oauth.py`/`role_policy.py` B2、`publish/` 四文件、`antigravity/client.py` 等）在证据运行时都处于未提交状态；每次运行时工作区到底含哪个子集**无法追溯**。G2 的路由扫描在 08:18Z 就已踩着 B1 放行（`realrun-` 前缀当 PM）调 PM 接口；G1 的 $0.02 额度靠 B3 的 `effective_floor=$0.000001` 才能通过 $0.25 门槛；G3/G4 的知识导入走 B1。也就是说**证据不是"疑似"踩未提交代码，而是确定踩了三个后门的未提交版本**。
- 例外说明：G6 被测的镜头脚本（`plugins/.../netcash.py` 等）最后一次改动在 PR #5（06:36:12 合并），PR #7/#9 未触碰 plugins——G6 被测代码与合并版一致，仅有 `g6_lenses.py` 本身当时未提交。
- PR #9 事后修改 `g1_budget.py`（`--cap-usd` 默认 $0.02→$0.30，并写明"须高于 `admission_floor_usd` 默认 $0.25"）——等于开发方自己确认：**G1 原证据在无 B3 的代码上不可复现**。
- 结论按清单建议：**8 份证据整体退回；用入库脚本在合并后的干净提交（工作区 clean，头部记录 `git_dirty`）重跑**；G1/G3 由 lead 按 §5.2 独立重跑（见 lead 重跑结果）。

### V2 G3 数字对不上 ❌（严重）

逐项对照（报告 §3 表 + §3.1 vs `g3_publish.json` 的 `evaluation` 与 `journal.totals` vs 入库摘要 G3.md）：

| 指标 | 开发报告 | 原始证据 | 入库摘要 G3.md |
|---|---|---|---|
| 金标准有来源数 | 61 | 36（`gold_sourced_total`；金标准 summary：159 条、GOOGL 72 + AMZN 87、sourced 36） | 36 |
| 真阳召回 | 61/61=100% | 33（`true_verified_in_gold_count`）=91.7% | 33 |
| 判"无来源"误伤 | —（未提） | 1（F-071 AMZN "2,200 亿"） | 1/36=2.78% |
| 有来源判"未能判断" | — | 2（F-089、F-093，"引文期间与正文不一致"） | 计入 uncertain 2/857 |
| 新调用/回放 | 78 / 1 | **139 / 87**（`fresh_calls/replayed_calls`，总 226） | 139 / 87 |
| 名义花费 | $0.214313 | **$1.768686** | $1.768686 |
| AMZN 数字总数 | 74（7 章节） | 金标准 AMZN 87 条；实跑 `amzn_labeling_coverage.total`=398 | 398 |
| 范围外抽样 | 25 | 范围外"已核实"共 24 个，**全部**核对（`out_of_gold_verified_sample_count=24`） | 0/24 |
| sha256 | `5c9c2249…`（与现行文件一致） | `5c9c2249…` | `5c9c2249…` |

判定：报告的 61/78/1/$0.214/74/25 在仓库任何工件（证据 JSON、摘要、金标准、脚本输出）中都得不出来；唯一自洽的解释是来自 11:21Z 最终重跑**之前**被覆盖的旧版证据（g3_publish.json 文件 mtime 07:24 EDT=11:24Z，此前当天有过运行痕迹的推断），而报告写于 12:45Z、引用了新文件的 sha256 却沿用旧数字。这是"编造或引用过期工件"二选一，按 L38 均不可接受。G3 的**证据本身**内部自洽（脚本汇总、摘要、对账一致），按 P0I §1.8 判定见 V3。

### V3 报告自定验收线 ❌（高）

- 报告 §3.1："真阳召回…（标准要求 ≥ 75%）"、"随机固定种子抽样 25 个"。P0I §1.8 原文：误伤"不超过有来源数字的 10%"（等价召回 ≥90%）；金标准范围外"随机抽 30 个'已核实'"。
- 入库脚本 `g3_publish.py:765-776` 的 `acceptance_pass` 断言的是 P0I 原阈值（`false_unverified_ratio<=0.10`、`uncertain_ratio<=0.10`、错放=0、交叉=0 等），G3.md 第 3 节也按"标准: <=10%"呈现——**降标准的动作只出现在开发报告里**，属于对 lead 的验收误导，单列为流程问题。
- 按 P0I 原标准重判现行证据：误伤（判"无来源/有矛盾"）1/36=2.8%；把两个"未能判断"计入为 3/36=**8.3%，仍在 10% 以内（写明）**；召回 91.7%；错放 0/159 内含范围（`false_verified_in_gold_count=0`）；交叉对照 verified=0；范围外已核实 24 个（不足 30 是因为只有 24 个存在，全查反而穷尽）；`incidental_sample_30` 恰为 30。**证据层面 G3 各项过线**；但金标准由开发方起草且尚无复核记录（R4，另行），且整份证据因 V1+B1 退回、待 lead 重跑（见 lead 重跑结果）。

### V4 G2/G6 sha256 报告与原始不一致；摘要与原始一致 ❌（高）

- `shasum -a 256 var/dalton/trial-runs/realrun-20261019/*.json` 实测与 9 份摘要中引用的 sha256 逐一比对：**8 份 JSON + 1 截图全部一致**（g1 `a70ea63b…`、g2 `5b0a0452…`、g3 `5c9c2249…`、g4 `62a2a334…`、g5 `a2e15558…`、g6 `2d5cb497…`、g7 `1266a7d8…`、png `2d9f8257…`）。摘要（`write_evidence_and_summary` 在写证据时自动渲染）与原始文件互证。
- 开发报告 §3 表：G2 写 `dc736110…`、G6 写 `0dc12374…`——与现行文件不符；同表 G1/G3/G4/G5.1/G5.2/G7 六行的 sha256 又都对。**不是整表过期，是逐行新旧混排**：g2/g6 的现行文件自 08:18/08:22Z 起未再重新生成，报告 12:45Z 才提交，故"报告写完后证据重新生成"的解释不成立；更可能这两行抄了更早轮次（或手写错）的值。
- 以哪次为准：**以摘要+原始文件为准**（两者一致且时间戳自洽）；报告 G2/G6 两行按勘误处理。附加发现：报告 G2 行的"3/0 调用、$0.000015"与证据（1/0、$0.000058）不符、G5 行的"8/0、$0.000531"与证据（1/0、$0.000000/名义 $0.000013）不符——报告 §3 表的数字只有 G1、G4、G7 三行与证据一致。

### V5 G6 BABA 净现金 19.13% vs 21.39% ❌（高，兼口径与证据方法问题）

同锚点（市值 $271,437.86M、汇率 0.149772）两个结果的来源：

- 现行 `netcash.py` 口径（证据 21.39%）：口径 A 毛现金**含**受限现金（docstring：Cash + Restricted + Time Deposits + ST Investments），basic=64,222.25（23.66%）；口径 B = basic − 受限现金一次（`restricted_entry` 自动插入）= 58,051.64 → **21.39%**。用 g6_lenses.py 的输入手算复核一致。
- 对比文档 `G6-financial-lenses-comparison-20261018.md`（报告 19.13% 的出处）：其"口径 A"$58,062.27 恰等于现行代码的**口径 B**（≈58,051.64，差 $10.63M 为分项舍入；0700.HK 与 META 两行**逐分不差**：10,409.15=10,409.15、49,109.00=49,109.00），其"口径 B"再扣一次受限现金 → 19.12/19.13%。即**文档口径相对现行代码把受限现金扣了两次**（或产自 Def A 语义不同的旧版/手算）。开发报告 §2.5（"由 19.80% 更新为 19.13%"）与它自己的 G6 证据（21.39%）互相矛盾。
- **anchors 是当场联网还是读现成文件：都不是——是脚本硬编码。** `g6_lenses.py` 第 75–187 行把市值/汇率/全部资产负债表科目写死在 `specs` 里（如 `--market-cap-usd-mn 271437.86 --fx-rate 0.149772 --cash 195400 …`），`anchors.py` 只是把这些 CLI 数字打包成 JSON（`--allow-network` 未传，无任何网络调用），镜头脚本再读该文件——这就是 `started_at==ended_at` 同一秒、12 个子进程 1 秒内完成的原因。`anchors_source:"anchors"` 证明的只是"三把镜头读了同一份文件"的管道一致性，不是锚点取自真实行情；且输入为预先写定却 `synthetic: []` 未声明。
- 判定：21.39% 是现行代码+该组硬编码输入的正确输出；19.13% 无法用现行代码和该输入复现。G6 满足 P0I §6 的"`--anchors` 结构统一/互斥报错"（exit 2 验证在案），但"按 SKILL.md 命令对真实标的跑通"打了折扣：标的存在、数字是脚本常量。需与 R7（镜头族）审查合并处理：要么在 P0I 里明确"确定性镜头允许固定输入"，要么要求 anchors 从行情源取数。

### V6 G5.2 粘贴回调未走完 + 借用令牌 ❌（严重，档位 🟦）

- 证据 `docker_callback_paste_flow`：`initiate=200`、poll pending；三种回调：缺 state 400、错 state 400、**state 与 PKCE 均正确的回调也 400**（`Token exchange failed: HTTPStatusError`）。P0I §7 G5.2 要求 Docker 部署下"走完一次"粘贴回调登录——只演示了拒绝路径，正确路径在令牌交换处失败。G5.2.md §3 原样列出 400 却在 §5 写成"验证了…PKCE/state 校验"，把失败路径包装成验证通过。
- "已连接"与 4 轮工具调用的令牌来源：`_seed_owner_antigravity_refresh_token`（g5 脚本 75–125 行）直接 `INSERT INTO user_oauth_tokens`，把 `local-dev-user`（owner）的 refresh_token 连同邮箱/account_id 复制给测试用户并把 `expires_at` 置于过去强制刷新；证据 `provisioned.seeded_from_user="local-dev-user"` 自证。B2 的生产代码回退（`oauth.py get_valid_token`/`role_policy connected` 借 owner）是叠加的第二层。**脚本直写凭据表既不在允许的四类注入内，也未在 `synthetic` 声明**（g5 的 synthetic 只有 settings_api_budget）。PR #9 移除了 B2 但**没有**改这个脚本直写——G5.2 在 HEAD 上依旧不合规。
- "后端在 Docker 里运行吗"：所有证据 backend 均为 `http://127.0.0.1:8000`，G2 的启动检查 traceback 路径为宿主机 `/Users/everflow/...`，无任何证据表明该后端跑在 Docker 容器里（G5.2.md 标题写"实跑 Docker"但证据无 Docker 标识）。❓ 存疑，倾向未满足"Docker 部署下"的前提。
- 档位：🟦。要回 🟨 必须：测试用户自己完成一次真实（Docker 下）粘贴回调登录，工具调用用其自有令牌，且 owner 邮箱不进入任何入库文件（PR #9 `eeb1e2ca` 已把摘要里的邮箱打码，但原始 JSON 仍在 `var/`——按 §2.1 第 6 条"摘要不含凭据"尚可接受，摘要曾经入库含邮箱是事实）。

### V7 G5.1/G5.2 同一份证据同一张调用表 ❓（G5.1 可独立成立）

- 属实：两份摘要出自同一脚本同一 JSON，§4 调用日志表逐字相同（1 条 provider_probe + 5 条 conversation_usages，全部是 G5.2 的 Gemini OAuth 调用）。G5.1 的 broker 调用（deepseek 计费 $3.06e-05）走 OpenClaw broker 路径，不落 `dalton_llm_journal`，所以它的对账表里根本没有自己的调用——这是设计如此，不是造假，但两族共用一张表确实让 G5.1 的"花费按调用日志重新汇总"只能依赖 broker 侧字段（`dalton_meta.cost_usd`、`x_dalton_replayed=1`）。
- broker 自身日志/状态的对应记录：`var/` 下没有独立 broker 日志文件；但证据内嵌了 broker `/health` 前后快照：重启前 `started_at 09:09:47`、`journal_entries 67`、`indeterminate 3` → 重启后 `started_at 09:12:58`、`journal_entries 68`、`indeterminate 4`——与 `gateway_process_restart_indeterminate`（409 `idempotency_indeterminate`）和断开重连（200，`X-Dalton-Replayed=1`）相互印证。**G5.1 的断开/重启证据独立成立**，不依赖 B1/B2/B3（deepseek 走 api-key）。
- 保留意见：受 V1 整体退回约束；且"重启的是承载 broker 的网关进程"（P0I G5.1 措辞修正后的要求）与 health 显示的 broker 自身 `started_at` 变化吻合。档位：条件 🟨（重跑后）。

### V8 G2 枚举 22 条 vs 第三轮 24 条 ✅（22 正确）/ 启动检查方式 ❌

- 离线复算：`src/dalton/app/router.py` 的 `@router.*` 装饰器在 `925fd0f9` 与 HEAD（`d1d8c098`）均为 **22** 个（method,path）且两版逐条相同（`diff` 为空）；`grep -rn include_in_schema src/dalton src/server` 无命中，`setup.py` 仅挂载这一个 dalton router。证据 `routes` 数组 23 条记录 = 22 个唯一路由 + `providers/{provider}/probe` 调了两次（deepseek、antigravity 各一）。**"22 条、全部调用、uncalled=[]"成立；第三轮的"24 条"复算不出来**（疑为把别的计数混入，建议 lead 在汇总里更正第三轮记录）。
- "assist 配成 contributor 启动失败"怎么测的（`g2_providers.py:50-77`）：**子进程 `python -c` 直接 `from src.dalton.llm import role_policy` / `from src.server.services.credit_gate_port import verify_credit_gate_wiring`，用 `unittest.mock.patch` 改配置后直调 `verify_credit_gate_wiring()`**——没有重启真实后端、没有走启动生命周期，且未在 synthetic 声明内部调用。RolePolicyError 确实会抛（对 PR #3 的 `enforce_role_policy_on_startup` 是有效信号），但按 P0I §2.1 第 1/4 条，这一子项是内部函数验证伪装成真实运行，G2 的该子项不满足 L37（G2.md 未披露此细节）。
- 另：G2 路由扫描对 PM 门禁接口（`POST /openclaw/models/sync`、`POST /knowledge/{ws}/{doc}/import`）的调用能成功，靠的正是 B1 对 `realrun-` 前缀的放行——G2 的"22 条全部调用"这一结论本身依赖后门（见 §4.2）。

### V9 G4 场景② 🟦（列出，lead 决定）

- 证据 `scenario_2_form4_triage_and_internal_lit.internal_literature_subitem`：`status:"🟦"`，注明"当天内部资料库无新入库文档（meta_new_docs=0, googl_new_docs=0）；按 P0I §7 G4 规范如实标为 🟦，不合成假内部资料"。这是 P0I §7 明确允许的诚实标注。
- 冲突点：P0I §11 第 2 条要求 G4 整体 🟨 才能进 P0.9/P0.10。场景②主体（Form 4 → preflight.triage 真实 LLM 分诊 → skip）已真实跑通（`triage_fresh_calls_count=1`，`replayed=false`），只有"相关/不相关内部资料"子项 🟦。
- 附带：该子项的核查方式是脚本顶层 `from src.dalton.autonomy.probes.internal import probe_internal` 直调（g4_autonomy.py:74,330-337，传入空 `watch_state`）——内部函数直调未声明；恰好落在已标 🟦 的子项内，按 §2.1 第 4 条"场景里有不允许的成分按 🟦 算"自洽，但应在重跑时改走 HTTP 或声明。
- 由 lead 决定是否接受"主体 🟨 + 子项 🟦"的组合。

### V10 报告分支名/文件归属错误 ❌（高，全部坐实）

`gh pr view/diff --name-only` 逐条核对：

| # | 报告说法 | 实际 | 判定 |
|---|---|---|---|
| 1 | I4 分支 `fix/P0I.I4-frontend-green` | `fix/P0I.I4-web-green` | ❌ |
| 2 | I6 分支 `fix/P0I.I6-lenses-anchors-flag` | `fix/P0I.I6-lenses-unified-anchors` | ❌ |
| 3 | I1 分支 `fix/P0I.I1-publish-audit` | `fix/P0I.I1-publish-stop-false-verified` | ❌ |
| 4 | I3 列 `role_policy.py`、`assist.py`、`test_router_wiring.py` | PR #3 实际 7 文件：`probes/director.py`、`budget/gate.py`、`llm/complete.py`、`antigravity/oauth.py`、`services/provider_status.py`、`server/app/setup.py`、`test_budget_port.py`——上述三文件均不在（role_policy/assist 在 PR #7，test_router_wiring 未在本轮任何 PR 出现） | ❌ |
| 5 | I5 写内部探针三个路由为 `research`/`news`/`community` | `internal.py:18` 实为 `("sales-notes","alphaengine","guidepoint")`；`research` 是 MCP tool 的 `name` 字段，不是路由名。并行化（`asyncio.gather`）与 10 秒时限为真 | ❌（路由名错，改动本身真） |
| 6 | I5 写改了 `dalton-agenda/SKILL.md`（cooldown 86400） | PR #4 改的是 `plugins/dalton/skills/dalton-stock-move/scripts/make_price_triggers.py`；`automation-templates/` 与 agenda SKILL.md 均未改 | ❌ |

附带核对：8 个 PR `reviews=0` 全部自合并（流程项归 L38 审查，此处仅记录）。模板与代码 cooldown 是否一致归 I5 族审查（R4-families）。

### V11 check_report_claims.py ❌（中）

- PR #8 对工具的改动（`bf79d385`）：默认扫描列表从 `[PROGRESS.md, P0H 报告, P0I 报告]` 改为 `[PROGRESS.md, P0I 报告(存在则用，否则 P0H)]`——**P0H 报告默认不再被扫**。理由可以理解（P0H 按勘误规范保留原文、只追加勘误，原文的错误名字永远清不掉），但净效果是默认路径下少查一份已知含错文档；P0I §2.2 原本用"扫 P0H 能报出第三轮的错名字"作为该工具的验证手段，该验证自此不再默认执行。可接受，但应在工具的 README/注释里写明"P0H 需显式传入"。
- 单测改动：`test_check_report_claims_catches_p0h_and_progress_violations` 把原来的固定 `clean-report.md` 样例换成**断言真实 `PROGRESS.md` 与 `P0I-round3-dev-report.md` 扫出 `all_clean=True`**。文档一改测试就坏（脆弱），且把"工具绿"变成了对活文档的背书；保留 P0H（必脏）作反例的同时用真文档当正例，动机明显是让 PR #8 自证清白。建议改回固定样例文件（一份必脏、一份必净）。
- 它查不出什么：工具只做"反引号标识符/仓库路径/var 路径存在性"检查。V2（数字与证据不符）、V3（自定验收线）、V4（sha256 不符——只查路径存在不查哈希）、V5（口径/百分比）、V10（分支名/文件归属——分支名不是仓库路径）**全部查不出**。本轮 `all_clean=True` 与报告六处数字错误并存即是证明；P0I §11.6 只把它当必要条件是对的，PR #8 的测试改动把它往充分条件的观感上推。

---

## §4.2 证据规范表（P0I §2.1 六条 × 8 份证据）

判定符号：✅ 满足 / ❌ 不满足 / ❓ 存疑或部分。头部字段按 P0I 清单核对（注：P0I 头部规范本身无 `git_dirty`，本轮全部缺失，单列为规范修正项，不计入各份证据的单项 ❌，但在 V1 统一追究）。

| 证据 | 脚本入库 | 通过真实接口 | 头部字段齐全 | 调用日志对账 | 非真实成分已声明且在四类内 | 关键调用 replayed=false | 摘要入库 sha256 一致 | B1–B3 依赖 |
|---|---|---|---|---|---|---|---|---|
| G1 | ✅ `g1_budget.py`（注：PR #9 又改 5 行，HEAD 版 ≠ 生成证据版） | ✅ threads/messages、budget、api-keys、providers/probe 全 HTTP | ✅（缺 git_dirty，见 V1） | ✅ 摘要与 journal 一致（2/1，$0.022249） | ✅ settings_api_budget（额度注入，允许类①） | ✅ 子智能体 deepseek 17 次调用、BYOK 现金 $0.0222>0，conversation_usages 逐条可查 | ✅ `a70ea63b…` + 截图 `2d9f8257…` | 🟦 **依赖 B3**：$0.02 额度 < $0.25 门槛，靠 `effective_floor` 钳制才放行（PR #9 改默认 $0.30 自证） |
| G2 | ✅ `g2_providers.py` | ❌ 路由扫描/探测 ✅ HTTP；**启动检查为子进程 import 内部函数 + mock 配置**（g2:50-77），未重启真实后端、未声明 | ✅（缺 git_dirty） | ✅ 摘要一致（1/0，$0.000058）；**开发报告写 3/0、$0.000015 与证据不符**（V4 附加） | ❌ startup_fail_fast 的 mock/内部调用未声明，且不在四类内 | ✅ deepseek 探测 fresh=1、$0.000058>0 | ❌→摘要 ✅ `5b0a0452…`；**报告写 `dc736110…` 不符** | 🟦 **依赖 B1**：调用 PM 门禁接口（models/sync、knowledge import）靠 `realrun-` 前缀放行 |
| G3 | ✅ `g3_publish.py` | ✅ 全部经 HTTP（threads/messages、knowledge import、workspaces 文件写入、publish-requests 查询） | ✅（缺 git_dirty） | ✅ 摘要一致（139/87，$1.768686）；**报告 78/1、$0.214313 不符**（V2） | ❓ **类别误标**：证据导入声明为 `knowledge_api_agenda`，而 P0I 第②类定义是"修改议程"（g4 的 agenda/import 用法才是对的）；证据导入本身走官方 import 入口、但严格说不在四类字面内。工具只校验 kind 字符串、不校验语义（`_common.py:74-100`） | ✅ 139 fresh（judge 101 fresh），注入场景 judge 调用在日志内 | ✅ `5c9c2249…`（报告此行一致） | 🟦 **依赖 B1**：`POST /knowledge/{ws}/thesis/import` 是 PM 门禁接口 |
| G4 | ✅ `g4_autonomy.py` | ❓ 主体 ✅（automations 立即执行、preflight/verdicts/board 全 HTTP）；场景②内部资料子项 `probe_internal` 内部直调未声明（g4:74,330） | ✅（缺 git_dirty） | ✅ 摘要一致（2/1，$0.010144） | ✅ 四条声明均在允许类（①额度、③unsee×2、②agenda import）；❌ probe_internal 未声明 | ✅ `triage_fresh_calls_count=1, replayed=false` | ✅ `62a2a334…` | 🟦 **依赖 B1**：`POST /knowledge/{ws}/agenda/import` 同为 PM 门禁接口 |
| G5.1 | ✅ `g5_broker_and_antigravity.py` | ✅ broker HTTP（disconnect/replay、gateway restart 后 409、health 快照） | ✅（缺 git_dirty） | ❓ 两族共用一份 journal，G5.1 自身调用不落 `dalton_llm_journal`（broker 路径设计如此），靠 broker 侧字段对账 | ✅ settings_api_budget | n/a（无 LLM 门槛要求）；断开重连 `X-Dalton-Replayed=1` 只计费一次在案 | ✅ `a2e15558…` | 无后门依赖（deepseek api-key）；受 V1 整体退回 |
| G5.2 | ✅ 同上 | ❓ 4 轮工具调用 ✅ 真实 SSE；**"已连接"由直写 owner 令牌制造**（`INSERT INTO user_oauth_tokens`，g5:75-125），粘贴回调正确路径 400 未走完 | ✅（缺 git_dirty） | ✅ 摘要一致（1/0，$0.000013 名义）；**报告 8/0、$0.000531 不符** | ❌ **owner 令牌直写库未声明、不在四类内**（synthetic 只有额度注入）；B2 生产代码回退叠加 | ✅（4 轮为真实 OAuth 调用，但用的是 owner 令牌） | ✅ `a2e15558…`（与 G5.1 同文件） | 🟦 **依赖 B2 + 脚本直写**；PR #9 移除了 B2 但脚本直写仍在 |
| G6 | ✅ `g6_lenses.py` | ❓ 镜头脚本 CLI 子进程即其真实接口；但**锚点与全部财务输入硬编码于脚本**（`--fx-rate 0.149772` 等为脚本常量，无网络），`anchors_source:"anchors"` 仅证明管道一致 | ✅（缺 git_dirty） | ✅ 无 LLM 调用（0/0），声明"纯确定性脚本"属实 | ❓ `synthetic: []`，但市值/汇率/科目均为预先写定未声明（V5） | n/a（无 LLM） | ❌→摘要 ✅ `2d5cb497…`；**报告写 `0dc12374…` 不符** | 无后门依赖（不经过后端业务接口） |
| G7 | ✅ `g7_bridge.py` | ✅ 桥接 MCP HTTP 实测（18960 测试实例 + 8960 真实实例）；钩子 `DALTON_BRIDGE_TEST_MODE=1` 且入库代码默认关闭（`researchdata_bridge.py:168-190`） | ✅（缺 git_dirty） | ✅ 无 LLM 调用（0/0） | ✅ `bridge_delay_hook` 声明，允许类④；第二次为真实内容读取（wiki:1657 等 3 个来源，非占位文本） | n/a（无 LLM） | ✅ `1266a7d8…` | 无后门依赖；被测桥接代码当时未提交（V1） |

**"哪些脚本调了需要 PM 的接口"（B1 坐实后的 🟦 清单）：** PM 检查 `_require_pm_user`（router.py:78）只保护两个端点——`POST /api/v1/dalton/openclaw/models/sync`（router.py:116）与 `POST /api/v1/dalton/knowledge/{workspace_id}/{doc}/import`（router.py:327）。调用方：**G2**（路由全扫描命中两处）、**G3**（thesis/import ×多工作区）、**G4**（agenda/import）。G1/G5/G6/G7 未触及。B2 依赖：G5.2。B3 依赖：G1。**B1–B3 全部已由 PR #9 移除（d5b49563 diff 复核：router 前缀放行、oauth/role_policy 借用、gate 钳制三处均删，并新增 `test_no_test_user_backdoors.py` 守卫测试）——但依赖它们的证据不因此变有效，须按 🟦 重跑。**

---

## §5.1 档位表（静态部分）

| 族 | P0I 要求 | 证据 | 本轮核实 | 档位 |
|---|---|---|---|---|
| G1 | 对话接口发起、现金子智能体真实触顶、下轮 429、截图、总花费 ≤$0.50 | `G1.md`、`g1_budget.json`、截图 | 机制全真：429 返回体、截图 sha、deepseek $0.0222>0、BYOK 计入、审稿二次缓存命中均在案。但 **$0.02 额度依赖 B3 门槛钳制**（PR #9 自认并改 $0.30）+ V1 未提交工作区 | 🟦（退回；独立重跑**见 lead 重跑结果**；PR #9 后以 $0.30 额度在干净提交上重跑即可能回 🟨——注意 P0I 原文"$0.02 左右"与 $0.25 门槛本身矛盾，需 lead 定额度） |
| G2 | 路由表枚举全部路由；≥1 现金供应商探测成功计入；contributor 配置启动失败 | `G2.md` | 路由 22/22 调用 ✅（第三轮"24"复算不出）；deepseek 探测 $0.000058 计入 ✅；**启动失败系内部函数+mock 直调，非真实后端**；路由扫描依赖 B1 调 PM 接口 | 🟦（退回；重跑需：真实重启后端验证 RolePolicyError、PM 权限经 `pm_user_ids` 配置给测试用户） |
| G3 | P0I §1.8 全项 | `G3.md`、`g3_publish.json` | 证据内指标按 P0I 原线全过（错放 0、误伤 1/36=2.8%〔含 uncertain 3/36=8.3%〕、交叉 0、注入防住、范围外 24/24 错放 0、覆盖 857/857）；但 **B1 依赖 + V1 + 金标准无复核（R4）+ 开发报告数字全面失真（V2/V3）** | 🟦（退回；独立重跑**见 lead 重跑结果**） |
| G4 | 真实自动任务触发；只用允许注入；`preflight.triage` replayed=false | `G4.md` | 场景①真实 8-K（0001628280-26-050596, Item 2.02）唤醒、verdict 落库；triage fresh=1 ✅；注入四条均允许类且 unsee 只动 `seen_accessions` 不写信号表（g4:254-272 复核）✅；**agenda/import 走 B1**；场景②子项诚实 🟦（V9，lead 决定）+ probe_internal 直调未声明 | 🟦（退回；去 B1 重跑后主体可 🟨，子项 🟦 待 lead 裁决） |
| G5.1 | broker 重启后同 ID 重试 409；断开后重试拿完整结果、只计费一次 | `G5.1.md` | 409 `idempotency_indeterminate`、`X-Dalton-Replayed=1`、cost 单次 $3.06e-05；broker health 前后快照（journal 67→68、indeterminate 3→4、started_at 变化）独立佐证；不依赖后门 | 🟨（条件：V1 整体重跑后在干净提交上复现；无 V1 则本轮即可 🟨） |
| G5.2 | 测试用户自己登录；≥4 轮工具调用；Docker 下走完粘贴回调登录 | `G5.2.md` | 4 轮工具调用真实但用 **owner 令牌**（B2+脚本直写，未声明）；正确回调 400 未走完；"Docker"无证据标识；owner 邮箱曾入摘要（PR #9 打码） | 🟦（P0I §7 明言"不再豁免"；需 owner 配合重新登录链路） |
| G6 | BABA/0700.HK/META 按新命令跑通，三组锚点一致 | `G6.md` | `--anchors` 管道一致 ✅、互斥 exit 2 ✅；但输入全部硬编码、无行情来源；21.39% vs 报告/对比文档 19.13% 口径矛盾未解释（V5） | 🟨-（结构验证过、数据真实性存疑；与 R7 镜头族审查合并定档，倾向"🟨 但须在 SKILL.md/文档写明输入来源并把对比表勘误"） |
| G7 | >240s 读取成功且已声明（钩子入库默认关闭）；另一次真实内容读取 | `G7.md` | 245s 延迟钩子声明规范、默认关闭入库 ✅；真实读取 wiki:1657 等 3 源非占位 ✅；被测桥接代码当时未提交（V1） | 🟨（条件：干净提交上重跑确认） |

（G1、G3 的"独立重跑"结果以 lead 汇总为准，本表只给静态判定。）

---

## 问题汇总

| # | 严重程度 | 位置 | 问题 | 建议 |
|---|---|---|---|---|
| E1 | 严重 | 8 份证据全体 | V1：证据全部跑在未提交工作区（git_sha=58e85d4e，PR #7 于证据跑完后才提交），无 git_dirty；合并提交从未跑过证据；g1 脚本事后又被 PR #9 修改 | 证据整体退回，在干净提交上重跑；P0I §2.1 头部规范补 `git_dirty` 字段；摘要记录脚本自身 sha |
| E2 | 严重 | `P0I-round3-dev-report.md` §3/§3.1 | V2：G3 报告数字（61/61、78/1、$0.214313、AMZN 74、抽样 25）与证据/摘要全面不符，仓库内无法复现 | 报告按证据逐项重写；下一轮起报告数字必须由脚本从证据 JSON 汇总生成，禁止手写 |
| E3 | 严重 | 报告 §3 表 | V4 扩展：除 G2/G6 sha256 外，G2（3/0、$0.000015）与 G5（8/0、$0.000531）两行的调用/花费也与证据不符——报告 §3 表 8 行中 5 行数字或 sha 有误 | 同 E2 |
| E4 | 严重 | G5.2（g5 脚本 75-125 行） | V6/B2：owner 令牌直写 `user_oauth_tokens` 制造"已连接"，未声明、不在四类注入内；粘贴回调正确路径 400 未走完；PR #9 后脚本直写仍在 | 测试用户自行完成 Docker 粘贴回调登录；凭据准备只能经真实登录或配置化测试数据；G5.2 保持 🟦 |
| E5 | 高 | G1 / `budget/gate.py`（PR #7） | B3 依赖：$0.02 额度靠 `effective_floor` 钳制过 $0.25 门槛；P0I"$0.02 左右"与门槛常量本身冲突 | lead 定额度（如 $0.30）并修订 P0I 文字与门槛的关系；G1 重跑（见 lead 结果） |
| E6 | 高 | G2/G3/G4 | B1 依赖：PM 门禁接口（models/sync、knowledge import）靠 `realrun-` 前缀放行被调用 | 测试用户经 `pm_user_ids` 配置授权后重跑；三族按 🟦 退回 |
| E7 | 高 | 报告 §3.1 | V3：自定"召回 ≥75%"验收线、抽样 25，低于 P0I §1.8（≤10% 误伤/召回 ≥90%、抽 30）；入库脚本本身用的是 P0I 原线 | 报告勘误；汇总里明确"改验收线"这一动作本身记入 L38 违规 |
| E8 | 高 | `G6-financial-lenses-comparison-20261018.md` + 报告 §2.5 | V5：19.13% 相对现行代码双重扣除受限现金，与证据 21.39% 矛盾；G6 输入全为脚本硬编码却 synthetic 空、12 次运行 1 秒完成 | 对比表与报告勘误；G6 重跑注明输入来源，或经 anchors 真实取数 |
| E9 | 高 | 报告 §1/§2.3/§2.4 | V10：3 个分支名、I3 三个文件归属、I5 路由名（research/news/community）与 SKILL.md 改动归属共 6 处与 PR diff 不符 | 报告勘误并入下一轮 errata |
| E10 | 中 | G2（g2:50-77） | V8：contributor 启动失败用子进程 import 内部函数 + mock 配置验证，未重启真实后端、未声明 | 重跑时以真实进程启动验证退出码；G2.md 补披露 |
| E11 | 中 | `check_report_claims.py` + 其单测（PR #8） | V11：P0H 默认不再扫描；单测绑定真实文档内容；工具查不出数字/sha/归属类错误，`all_clean` 被当成背书 | 单测改固定样例；工具可选增加"报告引用的 var 证据 sha256 与实际文件一致"检查 |
| E12 | 中 | G3 synthetic 声明 | 证据导入误用 `knowledge_api_agenda` 类别；工具只校验 kind 字符串不校验语义 | P0I 四类中增设"经知识层导入接口写入测试证据"或改声明；`validate_synthetic_declarations` 校验 entry_point 与类别的一致性 |
| E13 | 低 | G5.1/G5.2 摘要 | V7：两族共用一份证据与日志表，G5.1 自身调用不在 `dalton_llm_journal` | 重跑时 G5.1 单独出证据或在摘要写明对账来源为 broker 侧字段 |
| E14 | 低 | 第三轮记录（非本轮代码） | V8 附带：第三轮"路由 24 条"在基线与 HEAD 均复算不出（22 条） | lead 在汇总中更正第三轮数字，避免以讹传讹 |

## 疑似

- G5.2 摘要标题与 §5 称"Docker 部署…实跑"，但全部证据无 Docker 标识（backend 为宿主机 127.0.0.1:8000，G2 traceback 为宿主机路径）——后端是否真在 Docker 中运行过无证据支撑（❓，倾向否）。
- V2 报告数字的最可能来源是 11:22Z 最终重跑之前被覆盖的旧版证据（当天多次运行的文件 mtime 与 PR #7 提交时间的空档支持该推断），但无直接工件可证；不排除部分数字为手写拼凑。
- 报告 §3 G6 行名义花费/调用数为 0/0 与证据一致，但其 sha256 行不符——疑为该行整体抄自旧版输出而 sha 又手改过半（G1/G3/G4/G5/G7 五行 sha 对、G2/G6 两行错，无一致解释）。
- G1 场景③审稿模型为 `gemini-4-argon-high-oauth`（journal by_model），而场景①lead 是 claude OAuth——审稿第二次命中缓存为真（replayed=1），但"审稿家族隔离"是否覆盖该组合归发布族审查（R4-publish），此处仅记录。
