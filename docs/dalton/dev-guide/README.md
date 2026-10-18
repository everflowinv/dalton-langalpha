# Dalton on LangAlpha 开发指南：总览

版本 1.0，2026-10-18。写给接手开发的工程师。验收由 lead 负责。

## 0. 怎么读这份指南

| 文件 | 内容 | 谁先读 |
|---|---|---|
| `../blueprint.md` | 目标、设计原则、总体架构。**所有人先读** | 全体 |
| `README.md`（本文） | 阶段、依赖、工作包、约定、决定记录、验收方式 | 全体 |
| `P0-platform.md` | 本地环境、broker v2 模型接入、模型同步与同步按钮、本地日预算、原生界面验证 | 平台工程师、broker 工程师 |
| `P0D-P1-data-and-onboarding.md` | researchdata 桥接、SEC 数据、来源记录；基金方法论 skill、覆盖名单、公司工作区初始化、旧认知迁移 | 数据工程师、内容工程师 |
| `PK-knowledge-governance-autonomy.md` | 公司知识层、发布治理、自主研究议程、界面小改 | 后端工程师 |
| `PS-analyst-skills-and-workflows.md` | 分析师的 skill、workflow、自动任务，以及验收样例 | 内容工程师 |

**冲突时的优先级：** 本文第 6 节"决定记录" > 各分册 > blueprint。分册里写着【待验证】的地方，要先做验证，再按结果实现。验证结果写进 PR 描述。

每个步骤都有八项：目标、前置依赖、改动或新增的文件、具体做法、验证方式、验收标准、风险与注意、预估工时。**验收标准就是 lead 验收时检查的内容**，提交前请自己先逐条跑一遍。

## 1. 要做成什么

Dalton 是基金的一名 AI 分析师。它要像新入职的分析师一样工作：学会基金方法论，做初次覆盖、完整覆盖，之后持续跟踪、回答 PM、在周会上复盘并主动 call，自己安排研究工作。

**完成的定义：** 每个阶段都以 PM 能读、能评分的研究成果作为验收，不以测试数量或代码量作为完成标准。

**三条硬原则：**
1. **核验只在正式发布时卡一次。** 草稿阶段只给反馈、标注"未核实的数字"，不阻止保存、阅读和继续研究。
2. **限制只有共享日美元额度和可选的 task cap。** 不加调用次数、尝试次数、资金池、按用途的金额上限。退避按时间算，不按次数。供应商自己的配额只做提醒，不做门槛。
3. **尽量少改 LangAlpha 上游代码。** Dalton 的代码放在 `plugins/dalton*/`、`src/dalton/`、`workflows/`、`scripts/dalton/`、`config/dalton/`。对上游文件的改动只允许做"挂钩"，每处都要标注（见第 5 节），以便定期合并上游。

## 2. 架构一览

```
OpenClaw（宿主机）
  ├─ dalton-langalpha-broker（P0.2–P0.3，新建插件）：OpenAI 兼容的模型接口，不经过 OpenClaw agent
  └─ researchdata MCP（原样复用）── researchdata 桥接（P0-D.1）：stdio 转 HTTP，只读

LangAlpha（Docker / colima）
  ├─ 后端：主智能体、子智能体、workflow、自动任务、记忆
  │    ├─ 模型：openclaw-broker 供应商（ocb-*）+ 原生 Claude 订阅登录（主模型可切换）
  │    ├─ 本地日预算 LocalBudgetPort（P0.8）
  │    ├─ 知识层工具 knowledge_update / knowledge_append_verdict（PK.4）
  │    ├─ 发布工具 publish_report：数字核验 → 独立审稿 → PM 审批（PK.8–PK.13）
  │    └─ 零成本预检 + 研究总监（PK.16–PK.19）
  ├─ 沙箱（一个，所有公司工作区共用）：执行智能体写的代码、建模重算
  └─ 前端：LangAlpha 原生界面 + 三处小改（PK-UI）

plugins/dalton/            基金方法论与研究 skill、子智能体定义
plugins/dalton_data/       researchdata（HTTP）与 dalton_sec 数据服务的注册
workflows/<name>/          initial-coverage、full-coverage、bullbear-doc、earnings-deep-update、weekly-coverage-review、topic-research
```

## 3. 阶段与里程碑

| 阶段 | 包含的步骤 | 里程碑（lead 验收） |
|---|---|---|
| **P0 接通** | P0.1–P0.10，P0-D.0–P0-D.8 | 在 LangAlpha 界面里用 OpenClaw 的模型问一个 META 的问题：能看到流式输出、工具调用卡片、对 researchdata 的调用和来源；费用计入日额度，额度用完时运行会暂停；同步按钮能更新模型列表 |
| **P1 入职** | P1.1–P1.5，PK.1–PK.7 | 问"你对 META 的看法和主要 debate"，回答引用的是迁入的认知，结构符合基金方法论；四家公司的知识层文件在文件面板里清楚可读 |
| **P2 初次覆盖** | PS.0–PS.5 | 用验收公司（第 7 节待定）产出初次覆盖报告和初始 thesis，PM 盲评达到"可用"；分层模型配置和全强模型配置做一次对照 |
| **P3 完整覆盖** | PS.6，PK.8–PK.13，PK-UI.3 | 一家公司的 12 问 memo 加带公式的 Excel 模型，经数字核验、独立审稿、PM 审批后正式发布，写入 `published/` |
| **P4 持续覆盖** | PS.7–PS.12 | 连续一周，四家公司都有事件驱动的更新和事件结论；赶上财报的话，完成一次前瞻、快讯、深度点评、模型更新、thesis 校准的完整链条 |
| **P5 自主议程** | PK.14–PK.20，PS.13–PS.17，PK-UI.5 | 一周不派任务，研究照常推进；有周会稿，并且至少有一条明确的 call 或"暂无高 conviction"；日花费不超额度，空闲日接近 $0 |
| **P6 扩展** | P0-D.7 的清单 | 港股和 A 股数据、雪球和 X、选股原型、多用户。按需启动 |

**关键路径：** P0.2 broker 技术验证 → P0.3 broker 正式开发 → P0.4 LangAlpha 接入 → P0.10 验收 → P2 → P3 → P5。broker 技术验证不通过时，按 P0.2 的结论切换到原生接入，后续阶段不受影响。

## 4. 工作包（按包派活）

"现在可开工"指不需要整套服务跑起来、可以单独验证的工作。整套服务要等 Mac 重启、colima 重新配置之后才能运行（见 P0.1）。

| 工作包 | 步骤 | 技能 | 依赖 | 现在可开工 |
|---|---|---|---|---|
| **WP-A broker v2** | P0.2 → P0.3 | Node，熟悉 OpenClaw 插件 | OpenClaw 网关在运行（已具备） | ✅ |
| **WP-B 平台接入** | P0.1、P0.4–P0.9 | Python、React/TS | P0.1 要等 Mac 重启；P0.4 依赖 WP-A 的接口定义 | ⚠️ P0.8 本地预算的代码和 P0.7 同步按钮的后端可以先写；其余等环境 |
| **WP-C 数据** | P0-D.0–P0-D.8 | Python、MCP | P0-D.0 要 colima 运行 | ✅ P0-D.1 桥接（只在宿主机上）、P0-D.5 `dalton_sec`、P0-D.6 价格与一致预期 |
| **WP-D 方法论与入职** | P1.1–P1.5 | 研究内容 + Python | P1.3、P1.4 依赖 PK.1–PK.2 的结构定义 | ✅ P1.1 方法论 skill、P1.2 覆盖名单；P1.3 模板和 P1.4 迁移脚本在 PK.2 定稿后开工 |
| **WP-E 知识层与发布治理** | PK.1–PK.13 | Python、Postgres | PK.4 之后的接入要等 WP-B | ✅ PK.1–PK.2 结构定义、PK.4 校验库、PK.9 数字核验引擎的核心逻辑 |
| **WP-F 分析师 skill** | PS.0–PS.18 | 研究内容 + Python + JS | workflow 的试跑要等 P0 和 PK.4 | ✅ PS.2 财务镜头脚本，PS.3、PS.4、PS.8、PS.9、PS.10、PS.15、PS.16 的 `SKILL.md` 和中文模板；workflow 脚本可以先写，P0 后试跑 |
| **WP-G 自主议程** | PK.14–PK.20 | Python | WP-B、PK.4 | ❌ 等 P0 |
| **WP-H 界面小改** | PK-UI.1–PK-UI.6 | React/TS | WP-B | ❌ 等 P0 |

**建议的人手：** 至少三人同时开工：一人做 WP-A，一人做 WP-C 加 WP-B 的可先写部分，一人做 WP-D 加 WP-F 的内容部分。WP-E 的结构定义要尽早出，因为 WP-D 和 WP-F 都依赖它。

**工时（v0.3 估计，单位：人天）：**

| 分册 | 工时 |
|---|---|
| P0 平台（含 broker v2） | 16–18.5 |
| P0 数据 | 约 8 |
| P1 入职 | 约 8.5 |
| P3 补充：逐文档来源记录（P0-D.3b） | 约 1.25 |
| 知识层、发布治理、自主议程、界面 | 约 31，另加一周真实自主运行作为验收 |
| 分析师 skill 和 workflow | 约 48 |
| **合计** | **约 113–115** |

三人并行时，大约 8–10 周走完 P0 到 P5，不含等待 PM 评分的时间。

## 5. 工程约定

**仓库与分支**
- 仓库：`github.com/everflowinv/dalton-langalpha`（私有）。主分支 `dalton/main`。
- 上游：remote `upstream` 指向 `ginlix-ai/LangAlpha`，推送地址已禁用。每两周由 lead 合并一次上游 main，合并后跑一遍 P0.10 里标为【回归】的验收项。
- 每个步骤一个功能分支，命名为 `feat/<步骤编号>-<短名>`，例如 `feat/P0.3-broker`、`feat/PS.4-bullbear`。完成后提 PR 合入 `dalton/main`。
- 提交信息用英文，一行讲清改了什么。

**代码位置**
- Dalton 自己的代码只放在：`plugins/dalton/`、`plugins/dalton_data/`、`src/dalton/`、`workflows/`、`scripts/dalton/`、`config/dalton/`，以及 Dalton 独立的 Alembic 迁移链。
- 唯一例外：skill 注册文件放在 `src/ptc_agent/agent/middleware/skills/dalton_registry.py`，因为上游的注册表只从这个目录加载。它是一个新文件，加上对 `registry.py` 两三行的挂钩。
- 改动上游文件时，在改动处加注释 `# DALTON-HOOK: <步骤编号> <一句话说明>`（前端用 `// DALTON-HOOK:`），每处尽量控制在几行以内。所有挂钩都要登记在 P0 附录 A 的清单里。
- 配置：Dalton 的行为配置放在 `agent_config.dalton.yaml`，通过 `APP_ENV=dalton` 生效；compose 的改动放在 `docker-compose.dalton.yml`。不改上游提交的默认配置文件。

**数据库**
- 所有 Dalton 表走独立的 Alembic 迁移链（`alembic_version_dalton`），不占用上游的迁移编号。
- 用原生 SQL，不用 ORM，和上游一致。
- 状态以 Postgres 为准，不在进程内存里保存请求要依赖的状态（上游的多 worker 约定）。

**测试与验证**（上游 `AGENTS.md` 的原则）
- 先用真实调用验证：起后端、接真实的模型和数据，跑通了再说。
- 单元测试只用来锁定已经稳定的契约，或者已经修复的回归问题。不要为了"测试通过"写测试。
- 不要改动被快照锁定的工具说明文字（`tests/unit/mcp_servers/agent_docstring_lock.json`），否则上游的默认测试会失败。

**密钥**
- 所有凭据只放在 `.env` 或 LangAlpha 的密钥库里，不进代码、不进日志、不进 PR。
- AlphaEngine、Guidepoint 等上游凭据始终只留在宿主机进程里，不进沙箱。
- `openclaw.json` 不挂载进容器。

**超时**
- 超时只用来兜底卡死的调用，不用来限制正常的慢请求。具体数值见各分册的 v0.3 修订。

## 6. 决定记录

### 6.1 owner 已经做出的决定

| 编号 | 决定 |
|---|---|
| O1 | 新开项目，基于 LangAlpha 最新版改造；旧 Dalton 已全部停止，代码已推送存档 |
| O2 | 沙箱用本地 Docker（colima），不用 Daytona |
| O3 | 模型列表以 OpenClaw 为准；模型调用走类似 Dalton broker 的方案，也就是 broker v2 |
| O4 | 研究成果直接复用 LangAlpha 原生界面，只做三处小改；以后需要时再加专门页面 |
| O5 | 模型设置页的"重新同步"按钮在第一阶段就要做 |
| O6 | 数据直接复用现有的 researchdata MCP |
| O7 | 合规没有问题：专家访谈、高管交流、Guidepoint 检索结果都可以用，引用时标注专家身份 |
| O8 | 基金 wiki 对新项目只读，不回写 |
| O9 | SPCX 留在 Core；美团用 `3690.HK` |
| O10 | 45 秒超时太短，要放宽 |
| O11 | 同意两项自主权限：研究总监可以自动把任务交给分析师；分析师可以自己创建和修改 `dalton-*` 自动任务 |
| O12 | 代码交给其他工程师写，lead 负责验收 |

### 6.2 lead 做出的技术决定

| 编号 | 决定 |
|---|---|
| L1 | 主模型可切换。默认用原生 Claude 订阅登录的 Opus 5.5；备选是经 broker 的 grok-4.7，或者 API key 按量付费。Claude 订阅登录存在订阅条款上的风险，owner 知情 |
| L2 | CLI 网关类模型（claude-cli-gateway、antigravity-cli-gateway、muse-cli-gateway）只用于纯文字的环节：上下文压缩、后台摘要、审稿。broker 收到带工具的请求时直接拒绝 |
| L3 | 自动回退默认关闭。打开时也只在供应商故障、且尚未输出任何内容时才回退，并且要告知调用方实际用了哪个模型 |
| L4 | 新项目的日预算和旧 Dalton 分开；时区 Asia/Shanghai |
| L5 | researchdata 桥接的凭据，第一期用"token 放在地址里、只读"；更彻底的做法以后再评估 |
| L6 | 来源面板第一期用不改数据库的过渡做法；逐文档的完整来源记录放到 P3，和数字核验一起做 |
| L7 | 知识层：人读的 Markdown 放在工作区根目录，程序用的 JSON 放在 `data/` 下；另有版本表和 `history/` 文件夹。直接修改知识文件不拦截、不自动恢复，只记录为一个新版本，不合规范时提醒分析师 |
| L8 | 知识层只保留三条硬规则：证据台账只能追加；正式观点只能经 `publish_report` 写入；目标价必须引用模型单元格 |
| L9 | 事件结论在数据里用 Playbook 的完整写法（`THESIS_STRENGTHENED` 等），界面上显示中文简称 |
| L10 | 财报日期只认公司官方披露，每周六刷新；其他日历只作候选，标为"待确认" |
| L11 | 事件触发用"定时轮询加零成本预检"，不新增触发类型 |
| L12 | 只做美股，港股和 A 股放到 P6 |
| L13 | 有正式发布成果的工作区不允许删除，只能归档；Dalton 模式下隐藏行情看板 |
| L14 | gridcraft 不作为建模主路径。建模用 LangAlpha 的 xlsx 流程；gridcraft 的 `check` 留作 P3 之后可选的第二引擎复算 |
| L15 | 模型分层：主分析师用强模型，取数子智能体和后台用便宜模型，审稿用另一家族的模型。上线前在 P2 做一次分层配置与全强模型配置的对照，PM 盲评没有明显差别才采用 |
| L16 | 所有审稿人（包括草稿阶段的审稿子智能体）都通过 `role_policy.family_of()` 选择与主模型不同家族的模型，不固定写死某一个模型 |

## 7. 待 owner 回答的问题

不阻塞开工，在对应阶段开始前回答即可。

| 问题 | 需要在何时之前 |
|---|---|
| 日预算额度定多少 | P0.8 联调前 |
| 基金对研究质量的正式评分标准（STRL）。暂用六个维度：商业机制、debate、预期差、驱动链、证伪条件、来源 | P2 验收前 |
| P2 初次覆盖用哪家公司验收。建议 DASH，备选 AFRM | P2 开始前 |
| conviction call 的仓位比例（Core、Tactical、Small）和门槛收益率（默认 15%） | PS.15 开发前 |
| 周度复盘在哪天跑：周五收盘后，还是周日 | PS.13 开发前 |
| 净现金口径：基金 skill 说明和脚本的定义不一致，暂采用说明里的定义，请确认 | PS.2 开发前 |

## 8. 验收方式

**每个 PR**
- 对应一个步骤，PR 描述里写清：
  - 做了什么；
  - 【待验证】项的验证结果；
  - 按"验收标准"自测的过程和结果，包括真实运行的命令、界面截图或对话片段；
  - 新增的上游挂钩。
- lead 按该步骤的验收标准复核，通过后合入。

**每个阶段**
- 阶段内的步骤都合入后，lead 按第 3 节的里程碑跑一遍完整验收。
- 研究成果类的验收（P2 及以后）由 PM 用评分表打分，评分表见 PS.18。

**不接受的提交**
- 没有真实运行证据，只有单元测试通过；
- 新增了调用次数、尝试次数或资金池之类的限制；
- 草稿阶段的核验会阻止保存或继续研究；
- 凭据出现在代码、日志或 PR 里；
- 改了上游文件却没有标注 `DALTON-HOOK`。

## 9. 开工前的环境准备

1. **重启 Mac。** 释放 fseventsd 约 8 GB 内存和 swap。Dalton 的服务已经禁用，重启后不会自动启动。
2. **重启 OpenClaw 网关，并定期检查它的内存。** 新项目通过它调用模型，它必须一直运行。
3. **重新配置 colima：** 4 核、5 GB 内存，虚拟机磁盘和数据放到 `/Volumes/EveSSD`（见 P0.1）。
4. **安装 broker v2 时修改 `openclaw.json`，并重启网关。** 已获授权，按 P0.3 的步骤执行。
5. **已经加好的透传 agent `langalpha`** 只保留作对照，验证命令见 P0.5。

## 10. 主要风险

| 风险 | 应对 |
|---|---|
| broker v2 用到的 OpenClaw SDK 底层接口没有公开文档，可能拿不到指定模型的凭据，或者流式输出走不通 | P0.2 技术验证先行；不通过就切换到原生接入，后续阶段不受影响 |
| 本机只有 16 GB 内存 | 只开一个沙箱，空闲时自动停止；长期不够用时再评估 Daytona，或者换一台机器 |
| LangAlpha 上游更新很快 | 代码尽量放在 Dalton 自己的目录里，挂钩都有登记，每两周合并一次上游并跑回归验收 |
| Claude 订阅登录可能被限制 | 主模型可切换（L1） |
| 研究质量达不到 PM 要求 | 每个阶段都以 PM 评分作为验收；不达标就先改 skill 和方法论，不加核验规则 |
