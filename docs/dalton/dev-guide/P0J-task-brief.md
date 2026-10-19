# P0J 开发任务书（第四轮审查后的修正）

**项目绝对路径：`/Users/everflow/Projects/dalton-langalpha`**

- **仓库：** `github.com/everflowinv/dalton-langalpha`（私有），主分支 `dalton/main`。开工时先 `git pull`，以最新的 `dalton/main` 为起点。
- **完整指南：** `/Users/everflow/Projects/dalton-langalpha/docs/dalton/dev-guide/P0J-round4-fixes.md`。本任务书和完整指南冲突时，以完整指南为准。
- **已经完成、不用再做的：**
  - J2a 金标准修订：PR #13；
  - 证据环境的 `.env`：已设置 `INTERNAL_SERVICE_TOKEN` 和 `DALTON_PM_USER_IDS`，后端需要重启才生效。

## 一、开工前必读（按顺序）

1. `docs/dalton/dev-guide/P0J-round4-fixes.md`：本轮指南，全文读完。特别注意第 0 节（总原则）和第 8.1 节（已决定的问题）。
2. `docs/dalton/dev-guide/review/SUMMARY-R4-20261019.md`：第四轮审查结论。
3. 4 份第四轮审查报告，都在 `docs/dalton/dev-guide/review/` 下，修到哪一项就读到哪一项：
   - `R4-backdoors-and-smuggled-code-20261019.md`
   - `R4-evidence-audit-20261019.md`
   - `R4-publish-20261019.md`
   - `R4-families-20261019.md`
4. `docs/dalton/dev-guide/P0G-review-fixes.md` §0.2：七条开发原则。
5. `docs/dalton/dev-guide/README.md` §5（工程约定）、§6（决定 L1–L38）。
6. 项目根目录的 `AGENTS.md`：Working principles，以及 Multi-worker 一节。

## 二、本轮的结论（为什么要做这些）

第四轮审查判定：不进入 P0.9/P0.10。PR #9 和 PR #10 已经删掉了三处测试后门，剩下的问题按根源排序：

1. **证据环境的身份是假的。** `.env` 原来没有设 `INTERNAL_SERVICE_TOKEN`，OSS 模式下 `get_current_user_id` 对所有请求都返回 `local-dev-user`（`src/server/utils/api.py:83-91`），请求头里的 `X-User-Id` 不起作用。所有"测试用户"实际都是 owner，按用户的额度、隔离、PM 权限、登录，一项都没有被测过。
2. **证据和代码不是同一个版本。** 证据都是在未提交的工作区上跑的，而且确实踩着后门；合并后的提交从来没有跑过证据。
3. **证据的转述不可信。** 开发报告的证据表 8 行错 5 行；报告自降验收线；G6 的对比文档、证据、报告三方数字互不一致；PR #7 的描述有两处和 diff 不符。
4. **发布核验又长出了规则。** publish 净增约 849 行，其中 3 处是新的语义规则。59 个被排除的数字里，有 10 个是事实陈述。J2a 复核金标准时还发现，6.4 年这个数字在 10-Q 证据里有原文，却被当成非核验对象排除了。
5. **PR #7 夹带了 20 个生产文件，没有测试，** 还留下了按 `local-dev-user` 特判的分支。

## 三、硬性规则（违反任何一条，整个 PR 退回）

1. **确定性代码只管三件事：** 钱；凭据和权限；数值和单位的换算、算术、编号。**语义判断交给 LLM，** 代码只核对能核对的部分。
2. **不新增正则、词表、按词或按 ID 的判断来做语义决定。** 期间、日期、编号这类格式识别除外，但每一条都要在 PR 里列出。
3. **不新增次数限制**："N 次后停止""每天最多 N 次"一律不允许。
4. **删除优先于补丁。** J2 这一族在 `src/dalton/publish` 下的净行数必须为负。
5. **测试只锁修好的回归，以及钱和凭据的契约。** 先真实运行，再写最少量的测试；测试替身必须带 `spec`（L33）。
6. **生产代码不按任何具体的用户 ID 特判，** 包括测试用户和 `local-dev-user`。测试需要的权限、凭据、额度，一律通过配置或测试数据提供。
7. **一族一个分支、一个 PR，不夹带。** 分支命名为 `fix/P0J.<族>-<短名>`。证据族的 PR 只包含文档。
8. **开发方不合并自己的 PR。** lead 在 PR 里留下"审查：通过"的评论后，由 lead 合并。
9. **凭据**（包括 `INTERNAL_SERVICE_TOKEN`）只放 `.env`，不进代码、日志、证据、PR。不要打印或复制 `.env` 的内容。摘要里不能出现邮箱。
10. **状态以 Postgres 为准，** 不引入模块级的可变状态。改上游文件要加 `# DALTON-HOOK: <步骤> <说明>`，并通过 `check_hooks.sh`。
11. **不要重构第四轮确认没问题的部分**：
    - I1 的锚定主链路；
    - B 环节和 D 环节的分批、缓存、候选、时限；
    - I5 的七项、I4、I3 的 P2–P6；
    - PR #9 和 PR #10 的修复。
12. **不要修改金标准文件：** `tests/fixtures/dalton/g3/gold_standard.json`、`colloquial_only_*`。

## 四、执行顺序

**J4 → J1 和 J3（可以并行）→ J2 → J5 → J6**（J2a 已完成）

每一族都按下面的流程走：
1. 从最新的 `dalton/main` 开分支；
2. 实现；
3. 真实运行验证；
4. 补最少量的测试；
5. 跑基线命令；
6. 提 PR，附原则自查表；
7. 等 lead 审查、合并；
8. 下一族从合并后的 `dalton/main` 开分支。

## 五、各族的具体任务

### J4 流程（`fix/P0J.J4-process`，0.5 天）

1. 新建 `.github/pull_request_template.md`，内容就是第六节的模板。
2. 模板里写明：PR 描述里的每一条说法都要写明对应的文件和行号；把 PR 描述保存成文件，用 `scripts/dalton/check_report_claims.py` 扫描。
3. 不需要改代码。

### J1 证据底座（`fix/P0J.J1-evidence-base`，2 天）

**1. 身份**
- `src/dalton/settings.py` 里有两份 `pm_user_ids`（第 58 行和第 123 行），合并为一份。路由的 PM 检查（`src/dalton/app/router.py`）改为读这一份。
- 默认值改为 `[LOCAL_DEV_USER_ID]`（从 `src/config/env.py` 导入），删掉 `"local_dev_user"` 和 `"default"`。
- 新增环境变量 `DALTON_PM_USER_IDS`（逗号分隔），覆盖默认值。`.env` 里已经设为 `local-dev-user,realrun-pm-a`。
- `scripts/dalton/realrun/_common.py` 封装请求时，一律带 `X-Service-Token`（从环境变量 `INTERNAL_SERVICE_TOKEN` 读）和 `X-User-Id`。没有服务令牌时直接报错退出。
- **固定的测试用户：**

  | 用户 | ID | 用途 |
  |---|---|---|
  | PM 测试用户 | `realrun-pm-a` | G2、G3、G4 里需要 PM 权限的操作 |
  | 非 PM 测试用户 | `realrun-user-b` | G2 的 403、G3 和 G4 的隔离 |
  | G1 的预算用户 | `realrun-g1-<运行时间>-a`、`-b` | 每次运行新建，因为额度按天计算 |

  每次运行的隔离靠新建的工作区和对话。这些 ID 只出现在 `.env` 和 `scripts/`，**不能出现在 `src/` 和 `plugins/`**（守门测试会拦）。
- **身份自检：** 每个脚本结束时，用 `journal_dump.py` 查这个时间窗口内的全部记录，断言每一条的用户都是本次声明的测试用户，没有一条记在 `local-dev-user` 名下。不满足时，证据 `valid=false`。

**2. 版本**
- 在 `GET /api/v1/dalton/openclaw/health` 里增加 `git_sha`、`git_dirty`（进程启动时计算一次）和当前请求者的 `user_id`。如果新加 `/api/v1/dalton/health` 改动更小，也可以，二选一。
- 证据头部升级为 `evidence_version: 2`，新增字段：
  - `git_dirty`
  - `backend_git_sha`、`backend_git_dirty`
  - `script_sha256`
  - `identity_mode`、`pm_user_ids_source`
  - `deployment`（`docker` 或 `host`）
  - `valid`、`invalid_reasons`
- **脚本拒绝运行的三种情况**（退出码非 0，不生成证据）：
  - 工作区不干净；
  - 后端的版本和 HEAD 不一致，或者后端的工作区不干净；
  - HEAD 不在 `origin/dalton/main` 上。

  不提供跳过这些检查的参数。调试时不加 `--record`，不写证据文件。

**3. 环境检查 `scripts/dalton/realrun/check_env.py`（新建）**
- **身份**：带 `realrun-pm-a` 和 `realrun-user-b` 两个 `X-User-Id` 调用健康检查接口，返回的 `user_id` 和请求头一致。
- **版本**：后端版本等于 HEAD，并且干净。
- **对话传输层**：用订阅模型发一条最短的 `POST /api/v1/threads/messages`，拿到完整回复。
- **桥接**：8960 一次成功的读取。
- **broker**：`/health` 正常。
- **部署形态**：记录后端运行在 Docker 还是宿主机上。
- **静态扫描** `scripts/dalton/realrun/g*.py`：出现 `from src` 导入或直接的 `INSERT INTO` 时报错。
- **Redis 唤醒监听池不带密码连不上**（第四轮审查人重跑时遇到 503）：找到根因。
  - 如果是配置问题，改 `.env` 的说明，并写进 `docs/dalton/dev-guide/P0-platform.md` 的 P0.1；
  - 如果是上游代码的问题，用最小的 `DALTON-HOOK` 修复并登记。

  不允许绕过。

**4. 注入声明**（`_common.py:31-39`，`validate_synthetic_declarations`）
- 允许的类别改为 6 类，**同时校验类别和 `entry_point` 是否匹配**：

  | 类别 | 允许的入口 |
  |---|---|
  | `settings_api_budget` | `PUT /api/v1/dalton/budget` |
  | `knowledge_api_agenda` | 知识层的议程接口或工具 |
  | `knowledge_api_import_fixture` | 知识层的导入接口，文件必须在 `tests/fixtures/` 里 |
  | `watch_state_unsee_filing` | 只改 `dalton_watch_state` 的"已看到"集合 |
  | `committed_test_hook` | 必须在 `_common.py` 的钩子登记表里，写明代码位置；钩子默认关闭 |
  | `committed_fixture_input` | 已入库的输入文件 |

- 删掉 `registered_test_hook`。
- 正常的用户操作不算注入：建用户、建工作区、写草稿、发起对话、触发自动任务。

**5. 数字由脚本生成**
- `G<N>.md` 全部由 `_common.py` 从证据 JSON 渲染生成，人工说明写进 JSON 的 `notes` 字段。
- 新建 `scripts/dalton/realrun/render_report_tables.py`，生成开发报告的证据表，放在报告里 `<!-- generated:evidence-table -->` 和 `<!-- /generated -->` 两个标记之间。
- `check_report_claims.py` 新增两项检查：生成区块和重新生成的结果逐字相同；报告和摘要里引用的每个 `var/` 文件，sha256 和实际文件一致。
- `tests/unit/dalton/test_realrun_evidence_tools.py:198-201` 的"干净"断言改回固定样例文件，不绑定真实文档。
- `journal_dump.py:387-388`：查询 `conversation_usages` 失败时打 warning，`totals.conversation_usages_unavailable=true`，证据 `valid=false`。
- `assert_no_credentials_in_obj` 增加邮箱地址的扫描。

**验收（附在 PR 里）：**
- 在弄脏的工作区运行脚本，被拒绝；
- 去掉服务令牌的环境变量运行 `check_env.py`，报错；
- 用 G3 的旧证据跑 `render_report_tables.py`，能复现第四轮查出的 5 行差异；
- 重启后端之后，`check_env.py` 全部通过，输出附上。

### J3 夹带清理（`fix/P0J.J3-smuggled-cleanup`，1.5 天）

**1. 按 ID 特判的分支**

位置：
- `src/dalton/autonomy/board.py:147, 214, 241`
- `src/dalton/autonomy/probes/director.py:121, 143, 175, 204`
- `src/dalton/llm/assist.py:174`

改法：
- `user_id is None` 表示系统上下文，查全库；其他任何 `user_id`，**包括 `local-dev-user`，一律按所有者过滤。** OSS 单用户部署里，owner 本来就拥有全部工作区，过滤之后结果相同。
- `expire_stale_publications` 和 30 天清理，放到研究总监的每日任务里，以 `user_id=None` 调用。
- `assist.py` 的缓存范围一律包含 `user_id`。
- 字面量 `"local-dev-user"` 统一改为引用 `LOCAL_DEV_USER_ID`。
- 扩展 `tests/unit/dalton/test_no_test_user_backdoors.py`：禁止在 `src/dalton` 里出现 `== "local-dev-user"` 和 `!= "local-dev-user"`。

**2. PR #7 夹带的文件**

| 文件 | 处理 |
|---|---|
| `src/dalton/database/verdicts.py` | 保留。补测试：两个工作区同一天各写一条结论，编号不冲突 |
| `src/dalton/knowledge/validate.py` | 保留。补一个畸形阈值输入的测试 |
| `src/dalton/database/llm_journal.py` 的 `record_journal_replay` | 保留，补测试。`complete.py` 里调用它的两处 `except Exception: pass` 改为 debug 日志 |
| `src/dalton/knowledge/schemas/common.schema.json`、`thesis.schema.json` | 保留 `LTM`、`filing` 引用、`EV-` 编号。从 `label` 枚举里删掉 `guidance`，只保留 `company_guidance`：先查活库里有没有用 `guidance` 写入的文档，有的话追加修正版本，**不扩充别名表。** 补"旧文档仍能通过校验"的测试 |
| `src/dalton/knowledge/service.py` 的 `append_verdict` | 丢掉的字段写进返回的 `warnings[]` |
| `src/dalton/llm/providers/antigravity/client.py` | 保留。补测试锁住 `_sanitize_gemini_schema` 的扁平化行为；`dereference_refs` 失败的 `except: pass` 改为 debug 日志 |
| `src/dalton/publish/review.py` | 保留。补测试：没有可用的跨家族审稿人时拒绝，不降级到同家族 |
| `scripts/dalton/researchdata_bridge.py` | 读取 `DALTON_BRIDGE_TEST_DELAY_S` 时加 try；运行时统一以 `self.test_mode` 为准；登记进 J1 的钩子登记表 |

**3. 其他**
- **`src/server/app/setup.py:660-668`**：定义 `DaltonStartupConfigError`，把 `RolePolicyError` 和"迁移缺失"都归入这一类。只有这一类让启动失败，其他异常（比如数据库暂时连不上）维持 warning，保留 `DALTON-HOOK`。
- **缺键清单**：把 Dalton 组件的缺键清单写进 `docs/dalton/dev-guide/P0.9-ui-walkthrough-keys.md`，用真实的文件名：`ProviderStatusCard.tsx`、`OpenClawCard.tsx`、`DaltonBudgetCard.tsx`、`ResearchDocSource.tsx`，其余的逐个核实文件存在。

**验收：**
- `grep -rnE '[!=]= *"local-dev-user"' src/dalton` 输出为空；
- 守门测试通过；
- 两用户隔离的真实验证放进 J5 的 G4。

### J2 发布核验删规则（`fix/P0J.J2-publish-delete-rules`，2.5 天）

**1. 删掉**
- `src/dalton/publish/numbers.py:1124` 的 `_COLLOQUIAL_HINT_RE`，以及 `:1183` 的门控条件。**每一节都送 B 环节。**
- `numbers.py:925`"数字后面紧跟 年 / 个月 / 周 / 天 / 小时 → incidental"的正则。
- `_can_be_incidental` 里的指标黑名单（6 个词）和版本白名单（9 个词）。
- 守卫只保留按单位和格式的检查：货币、百分比、bp、倍数、每股单位不能排除；带小数的不能排除；表格里的数字不能排除。**去掉 `label_hint != "incidental"` 这个绕过通道**（`numbers.py:1017`）。
- `numbers.py` 和 `audit.py` 里写死的 deepseek-flash 重试。备选模型改为 `assist` 角色的配置，由 `assist_json` 统一处理：改用备选模型时打 info 日志，调用日志里记录实际使用的模型；`role_policy` 校验备选模型不能和审稿人同一个家族。

**2. 失败批次**

`numbers.py:1325-1330`：标注失败的批次，数字一律回落为普通数字，不能被排除。补一个用替身模拟整批失败的单元测试。

**3. 非核验对象只有九类**
1. 点赞数、转发数等社交互动计数；
2. 浏览量；
3. HTTP 状态码；
4. 文件、文档、邮件和 URL 里的编号；
5. 软件和模型的版本号、硬件型号（如 Sonnet 5.5、B200）；
6. 列表序号、页码；
7. 用户名里的数字（如 @kashyap286）；
8. 机构、产品、节目名称里的数字（如 a16z、Law360、财富 100 强）；
9. 期间、日期和时间窗口表达里的数字（如"过去 12 个月"、"近 30 天"、1Q27 里的 27）。

B 环节的指令里写明这九类；同时写明反例：装机容量、合同期限、样本量、覆盖半径、调查家数、量子比特数这类事实陈述，**必须核验。**

**4. 审批卡**（`web/src/pages/ChatAgent/components/dalton/PublishApprovalCard.tsx`）
- 被排除的数字逐条显示原句和类别（九类中的哪一类）；
- 渲染 `failed_batch_details`："第 N 批（第 X–Y 个数字）标注失败"；
- 新增的文案键同时加进 `en-US.json` 和 `zh-CN.json`。

**5. 期间**

`normalize_period`：把"过去 12 个月""近 12 个月""TTM""LTM"识别为 `LTM`。

**6. 豁免标记**
- 在 `numbers.py` 里定义唯一的一份常量：示意、假设、估计、举例，加三种括号；
- judge 指令从这个常量生成；
- `plugins/dalton/skills/dalton-publish/SKILL.md` 完整列出同一份列表；
- 加测试锁住 SKILL.md 和常量一致；
- 不新增标记词。

**7. 总时限**

`audit.py:869-872`：无条件取 `min(900, max(120, 待判数字数 × 2))`。

**8. 测试交接**

`tests/unit/dalton/test_publish_numbers.py` 里的 `test_p0i_g3_gold_standard_and_cross_control_zero_false_verified` 目前是 strict xfail，原因是时长正则把金标准里的"20 年 PPA"排除了。删掉时长正则后，它会转为通过，**必须移除这个 xfail 标记**，否则 strict 模式下会报 XPASS。

**验收：**
- `git diff --shortstat <起点> HEAD -- src/dalton/publish` 净行数为负；
- 用真实 LLM 跑 `tests/fixtures/dalton/g3/colloquial_only_draft.md`，4 个口语数字全部被抽出；
- `uv run pytest tests/unit/dalton/test_publish_numbers.py` 全部通过，没有 xfail。

### J5 重做全部证据（`fix/P0J.J5-realrun-evidence`，3 天，PR 只包含文档）

**前提：** J1–J4 全部合并；按 Docker 方式起服务（`make up`，`APP_ENV=dalton`），使 `.env` 里的新配置生效。先跑 `check_env.py`，通过之后，**在同一个提交上一次跑完所有族。** 中途发现代码问题：停下来，在对应族的分支上修，审查合并后，在新的提交上重跑所有受影响的证据。

| 族 | 要求 |
|---|---|
| G1 | 两个新建的预算用户。用户 A 的额度设为 **$0.30**，主模型用 Claude 订阅，派出现金子智能体，花到上限后，下一轮返回 429，并有前端截图；同一时间用户 B 的对话正常。设置页填的 key 的花费计入；审稿第二次命中缓存。总花费不超过 $0.50 |
| G2 | 从路由表枚举全部 22 条路由，逐条调用；`realrun-pm-a` 调用 PM 接口成功，`realrun-user-b` 得到 403；至少一个现金供应商的探测成功并计入调用日志。**用错误的 assist 配置真实启动一个后端进程**（另一个端口），记录退出码和日志的最后几行。删掉 `g2_providers.py:50-77` 的子进程加 mock 写法 |
| G3 | 按 `P0J-round4-fixes.md` §2.6 的验收表：错放 0；交叉对照 0；误伤不超过 10%；**误排除 0（看完全部被排除的数字，包括金标准覆盖范围外的）**；口语反例的 4 个数字被抽出；范围外抽 30 个，错放 0；"未能判断"不超过 10%；标注覆盖 100%；`〔假设〕` 标记的数字判为豁免。另外：通过知识层接口查 META、AMZN、GOOGL、MSFT 的 `thesis` 版本历史，证明 `legacy_thesis` 修正版本存在。`g3_publish.py:515-529` 的声明改为 `knowledge_api_import_fixture`，并补上 `settings_api_budget` |
| G4 | 经接口立即运行 Dalton 的自动任务，由执行器跑预检；只用允许的注入。**场景②用当天真实新入库的内部资料，** 脚本先检查有没有；没有就不跑这一场景，第二个交易日再跑。删掉 `g4_autonomy.py:74, 330-337` 对 `probe_internal` 的直接调用。加上两用户隔离：`realrun-pm-a` 和 `realrun-user-b` 的看板、信号、待审批列表都只显示自己的工作区 |
| G5.1 | 重启承载 broker 的网关进程后，同 ID 重试返回 409；断开后重试拿到完整结果，只计费一次。**单独一份摘要，** 对账来源写明是 broker 侧的日志和 `/health` 快照 |
| G5.2 | 后端运行在 Docker 里。测试用户发起 Antigravity 登录，owner 在浏览器里授权，把回调地址粘贴回去，**完成令牌交换。** 然后用测试用户自己的令牌跑至少 4 轮工具调用。删掉 `g5_broker_and_antigravity.py:75-125` 直接写 `user_oauth_tokens` 的代码。真实授权码仍返回 400 时，这是真实的 bug，先在单独的分支修好。**需要 owner 配合，提前约时间** |
| G6 | anchors 按 SKILL.md 的命令联网取数（`--allow-network`）；财务科目来自已入库的输入文件，每个科目标注来源的 accession 号，按 `committed_fixture_input` 声明。BABA、0700.HK、META 三组锚点一致。对比文档 `trial-runs/G6-financial-lenses-comparison-20261018.md` 由脚本根据证据重新生成 |
| G7 | 用登记过的延迟钩子，超过 240 秒的读取成功返回；另做一次真实内容的读取 |

摘要放在 `docs/dalton/dev-guide/trial-runs/realrun-<日期>/`，全部由脚本生成。

### J6 文档改正（`fix/P0J.J6-docs`，0.5 天）

**1. `PROGRESS.md`**
- P0I 中重升为 🟨 的行，先降为 🟦，J5 完成后再按新证据更新；
- P0.8：30 天清理的位置改为研究总监的每日任务（系统上下文）；
- P0N、P0.7：删掉"60 秒冷却"的说法，如实描述 `oauth_login_start` 时间比较；
- P0.7：删掉"扁平 `api_keys`"的说法；
- G2：路由数改为 22。

**2. 勘误一律追加，不改原文**
- `P0I-round3-dev-report.md` 末尾追加勘误，列出第四轮查出的全部不实之处：V2、V3、V4、V10 的各项；"新增 0 个正则"；S5 的 `[常识豁免]`；`anchors_source` 的取值；setup.py 的位置；60 秒冷却；扁平 `api_keys`；
- `review/SUMMARY-R3-20261018.md` 末尾追加一行更正："24 条路由"实际是 22 条；
- `P0I-round3-fixes.md` 末尾追加一行更正：G1 额度改为 $0.30，原因是 $0.25 的准入门槛；
- PR #7 里追加一条评论，更正"回退到 CLI 网关""open_questions 计数"两处描述。

**3. `README.md` 第 6 节**

加入 L39–L43，内容照抄 `P0J-round4-fixes.md` 第 7 节（L43 是"九类"）。

**4. 本轮开发报告**

`docs/dalton/dev-guide/P0J-round4-dev-report.md`：证据数字只出现在 `render_report_tables.py` 生成的区块里，正文不复述。

## 六、每个 PR 的描述模板

```
## 本族范围
（对应 P0J-round4-fixes.md 第几节）

## 改动清单
- 文件:行号 —— 改了什么、为什么（每一条都要能在 diff 里找到）

## 真实运行验证
（命令、输出、证据路径；J5 之前的族写真实后端上的手工验证）

## 基线命令输出（贴实际输出）
./scripts/dalton/check_hooks.sh --strict --audit
uv run pytest tests/unit/dalton tests/unit/llms tests/unit/middleware/skills tests/unit/mcp_servers -q
node --test integrations/openclaw/dalton-langalpha-broker/test/*.test.mjs
pnpm --dir web typecheck && pnpm --dir web test -- --run
uv run python scripts/dalton/check_report_claims.py --strict <本 PR 描述保存的文件> <改动的文档>

## 原则自查
1. 新增了正则、词表、按词或按 ID 的判断吗？            是/否：
2. 新增了次数限制吗？                                    是/否：
3. 新增的测试锁的是修好的回归或钱、凭据的契约吗？        逐个列出：
4. src/ 下的净行数变化，以及删掉了什么：                  命令与输出：
5. 改了上游文件吗？DALTON-HOOK 是否齐全：                是/否：
6. 描述里每一条说法都对应到 diff 的文件和行号了吗：      是/否：
7. 包含本族范围以外的改动吗：                            是/否：
```

## 七、遇到这些情况，停下来问 lead

- 修复需要改动第三节第 11 条列出的"不要重构"的代码。
- 想新增任何正则、词表或次数限制。
- 跑证据时必须用到允许的 6 类之外的注入。
- 认为金标准有错。**不要自己改，** 在 PR 里写明，由 lead 判断。
- 真实运行的结果和本任务书的预期不一致，又找不到根因。
- 需要改动上游文件超过几行。

**不要为了让证据或测试通过而修改生产代码的行为。** 第四轮的三处后门就是这样产生的。

## 八、完成标准（第五轮审查时核对）

1. 没有未修复的严重和高危问题；生产代码里没有任何按具体用户 ID 特判的分支。
2. G1、G2、G3、G4、G5.1、G5.2、G6、G7 全部达到 🟨：
   - 在同一个合并后的干净提交上生成；
   - 使用服务令牌身份；
   - 身份自检通过。
3. G3 按 §2.6 验收通过，误排除为 0。
4. 审查人能用入库的脚本，在自己的环境里独立重跑 G1 和 G3。
5. HEAD 上的四条基线命令全部通过。
6. 报告和摘要里的证据数字全部来自生成区块，sha256 核对通过。
7. 每个 PR 都有合并前的审查记录和原则自查表。
