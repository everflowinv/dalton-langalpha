# Dalton on LangAlpha：新分析师蓝图与开发计划

版本 0.1，2026-10-18。基座：LangAlpha main `8e0dce94`（2026-10-18），分支 `dalton/main`。

## 0. 一页结论

Dalton 要成为基金的一名"新分析师"：先学基金方法论，再做初次覆盖、完整覆盖，之后长期跟踪，回答 PM，在周会上复盘并主动 call 高 conviction 机会。它的认知随时间沉淀、修正，自己安排工作。

旧 Dalton 的目标一直是对的，但两个月里实现层漂向了治理和管道正确性：测试一万六千多项，PM 签收的研究成果为 0。切换到 session 模式之后，正式版本为 0，日常跟踪在 10-16 后中断。

新项目的做法是：

1. **运行模式用 LangAlpha 的**：一个主智能体主导研究，子智能体并行取证，workflow 编排大任务，自动任务负责定时和事件，用户能实时看到过程并随时插话。
2. **从旧 Dalton 和 OpenClaw 带过来的是"内容"，不是"机制"**：基金方法论（Playbook v1、培训手册精粹、OpenClaw skill 里的方法纪律）、内部数据源、模型接口、底线原则。
3. **我们自己补四块 LangAlpha 没有的**：结构化的公司知识层、自主研究议程、发布治理、本地每日预算。
4. **核验只在发布时卡一次**。草稿阶段只给反馈和标注，不阻止研究推进。
5. **按"分析师成长"分阶段交付**，每一阶段的完成标准都是 PM 能读、能评的研究成果，不是测试数量。

## 1. 我们要做什么

### 1.1 目标角色

我们是 fundamental long-biased 对冲基金。Dalton 的角色是新入职分析师：金融背景扎实，但对具体股票和行业了解不多。他的成长路径是：

| 阶段 | 分析师要做的事 | 产出 |
|---|---|---|
| 入职 | 学习基金 wiki：初次覆盖、完整覆盖、跟踪各自关注什么，以及通用分析方法 | 内化的方法论、覆盖名单 |
| 初次覆盖 | 从卖方研报、公告、网络、专家访谈、sales note 归纳认知：主营业务、行业特性（是否周期）、长短期驱动、竞争位置与壁垒（形成还是被破坏）、历史股价与估值驱动、当前 bull/bear debate、股价位置、叙事演绎、高 conviction 的预期差（含宏观驱动） | 初次覆盖报告、初始 thesis |
| 完整覆盖 | PM 安排后，完整拆解业务，理解行业、竞争、管理层、市场情绪，深挖当前最关键的问题，建财务模型和预测，修正早期认知 | 深度研究 memo、Excel 财务模型 |
| 持续覆盖 | 每天跟踪：股价为什么动，debate 往哪里转，市场定价对还是错；及时更新预测；业绩前后校准；主动发起专项研究；积累越来越深的认知（例如管理层给 guidance 的风格、驱动的领先指标） | 事件笔记、模型更新、thesis 修订、专题研究 |
| 响应 PM | 基于最新观点回答 PM 的问题，必要时补充检索 | 问答 |
| 周会 | 复盘过去一周的股价表现，讨论观点变化，主动 call 高 conviction 机会 | 周会稿、conviction call |

### 1.2 自主性（Dalton 章程 v0.1 的原话）

> "Dalton 的目标不是成为一个等待人派任务、再把任务做完的聊天 agent，而是成为一名持续覆盖行业和公司的自主研究分析师。"

人只做四件事：定目标和边界、校正航向、基于积累的认知讨论、读成果并决定是否交易。Dalton 自己形成议程、排序、执行、验证、重新规划，没有高价值的事就休眠。

### 1.3 为什么开新项目

- 旧 Dalton 的界面骨架在大版本前后没有变化，研究大脑的过程几乎不可见，只能单向发指令。
- 旧流程（六阶段阶梯、约 35 条 mission 流水线、深度认知门）没有删，新旧两套并存，限制和核验层层叠加。
- LangAlpha 已经把"智能体主导研究"这件事做成了产品：子智能体、workflow、自动任务、带公式的 Excel 建模、来源记录、实时可见的工作过程。在它上面补基金需要的部分，比继续修旧 Dalton 快。

## 2. 新分析师现在欠缺什么

以旧 Dalton 的实际生产情况为基线（只读调研，2026-10-18）：

| 能力 | 旧 Dalton 现状 | 欠缺 |
|---|---|---|
| 学基金方法论 | 研究大脑只读几句硬编码的短方法；Playbook v1 和培训手册没进入新研究流程 | 方法论没有内化 |
| 初次覆盖取材 | 旧流水线大量用 sales note、company wiki、Guidepoint；新会话几乎只用 SEC | 多源取证没有进入研究大脑 |
| 业务、行业、驱动 | 旧版初筛 6 份、dossier 25 版 | 新流程没产出 |
| 竞争格局与护城河 | 没有专门的分析对象 | 完全缺失 |
| 历史估值驱动、股价位置 | 有价格和估值快照数据 | 没有"历史上是 EPS 还是倍数驱动"的分析 |
| bull/bear debate | debate map 99 版，但 10-16 后停更 | 新流程没有接手 |
| 高 conviction 预期差 | conviction call 0 次 | 没有从市场隐含预期推出错误定价的环节 |
| 深度研究 memo | 旧版 100 份抽查 71 份被拒；新版正式版本 0 | 交付链不通 |
| 管理层与指引风格 | 只有一张孤立的"指引 vs 实际"表 | 没进入研究上下文 |
| 财务模型 | 旧版模型 19 版；新会话的模型修订几乎没跑 | 没被 PM 评审过 |
| 观点演化 | 正式 thesis 0 版；472 次事件判断全是"无变化" | 观点层是空的 |
| 日常跟踪 | 10-16 后中断 | 没人接手 |
| 业绩校准 | 前瞻 4 份，实际与预测对账 0 | 从未实战 |
| 专项研究 | 问题清单主要是自动生成的营收簿记问题 | 自主选题质量低 |
| 回答 PM | 生产记录 0 | 没有"最新观点"可依据 |
| 周会复盘与 call | 周会稿 0 | 缺失 |
| 长期记忆 | 只有版本化的数据记录 | 没有可读、可演化的公司认知 |

**结论：** 欠缺的不是模块，而是三样东西。

1. 一个真正主导研究、能看到问题并自己解决问题的大脑。
2. 一份随时间演化的公司认知，包括 thesis、debate、驱动指标、管理层风格和经验教训。
3. PM 能读、能评、能反馈的成果，以及让反馈回到认知里的闭环。

## 3. 设计原则

### 3.1 必须保留的底线（来自旧 Dalton）

1. 正式发布的成果里不能有编造的数字、一致预期或目标价。每个数字都能回指原件或工具结果，并标注期间。
2. 估计、假设、公司指引、一致预期分层标注；house case 和市场基准分开，一致预期不能自动覆盖研究判断。
3. 来源有身份和层级：filing > 公司公告 > 署名研报 > 官方或协会数据 > 专家（标注身份）> 新闻（只作线索）。同一份研报被多处转述只算一条来源链。
4. PM 对正式观点和交易拥有最终决定权。Dalton 只提研究观点和仓位建议。
5. 历史观点不被静默覆盖，每次修订留下旧值、新值和原因。
6. 已付费的结果复用，状态未知的调用不靠换编号重新购买。
7. 重要结论由不同模型家族独立审阅；审阅缺席时草稿仍可读，但不能升级为正式版本。
8. 长仓导向，但不强迫看多；没有足够预期差时直说"暂无高 conviction"。

### 3.2 明确去掉的

- 多层预算：单次、阶段、次数、资金池、按用途的金额上限。只保留共享日美元额度和可选的 task cap。
- 固定研究阶段和人工阶段关卡。研究顺序由大脑决定，方法论只规定"要回答什么"。
- 草稿阶段的硬性核验闸门。核验只在正式发布时做一次。
- 篇幅、章节数、图表数之类的机械门槛。
- 固定的重试次数和失败计数。必然失败的情况直接判定，临时失败按时间退避。

### 3.3 运行模式（来自 LangAlpha）

- 一个主智能体，按需调用 skill、派出子智能体、运行 workflow。
- 大量数据写进工作区文件，上下文只保留结论和索引。
- 用户能实时看到每一步和每个工具调用，可以随时插话改方向。
- 智能体可以自己写自动任务给自己排期。

## 4. 目标架构

### 4.1 总体分层

```
LangAlpha 基座（不改或少改）
  主智能体 / 子智能体 / workflow / 自动任务 / 沙箱 / 记忆 / Web UI / 来源记录

plugins/dalton/（新建，基金自己的插件包）
  skills/        基金方法论与研究 skill
  agents/        基金子智能体定义（取证支线、审稿人等）
  MCP servers    内部数据源、基金 wiki、中港 A 数据、公司知识层

Dalton 服务层（新建，在 LangAlpha 后端内）
  公司知识层     结构化、带版本的 thesis / debate / 驱动指标 / 管理层风格 / 经验
  研究议程       每家公司的议程 + 零成本唤醒检查 + 研究总监调度
  发布治理       publish_report 工具 + 发布前核验 + 独立审稿 + PM 审批
  本地预算       共享日美元额度 + task cap
  模型接入       OpenClaw 网关 + 生成的模型清单
```

### 4.2 分析师职责与机制的对应

| 职责 | 机制 | 触发 |
|---|---|---|
| 入职学习 | 方法论写入 `dalton-conventions` skill 和用户层记忆；覆盖名单和已有 thesis 迁入各公司工作区 | 一次性 |
| 初次覆盖 | workflow `initial-coverage` | PM 指派，或研究总监按议程安排 |
| 完整覆盖 | workflow `full-coverage`，含 Excel 模型和审稿 | PM 指派 |
| bull/bear | skill `/bullbear`（对话快速版）+ workflow `bullbear-doc`（完整成稿） | 按需，PM 问答常用 |
| 日常跟踪 | 价格触发跑 `stock-move`；每晚跑 thesis 增量检查，无重要变化不推送 | 自动任务 |
| 财报前瞻与点评 | 每周刷新财报日期；T-3 自动跑 preview；T0 跑快讯；之后跑深度点评、模型更新、thesis 校准 | 自动任务链 |
| 公告监控 | 定时轮询公告数据接口，有新公告就唤醒对应公司的分析师 | 自动任务 |
| 专项研究 | workflow `topic-research`；由分析师自己根据议程发起 | 议程 |
| 回答 PM | 主页的研究总监或公司工作区的分析师，先读公司知识层，再按需检索 | PM 提问 |
| 周会 | workflow `weekly-coverage-review`：全名单筛选异动，Core 逐家并行分析，汇总观点变化和 call | 每周定时 |
| 高 conviction call | skill `dalton-call`：预期差三问、三情景 IRR、仓位级别、对冲 | 按需或周会 |

### 4.3 模型接入：复用 OpenClaw 的模型列表

**现状：**
- 旧 Dalton 通过一个专属 broker（Unix socket，HMAC 认证）调用 OpenClaw 模型。它带调用日志、只读重放和"不重复付费"，但只支持单条 prompt 进、整段文本出，不支持流式，也不支持工具调用。
- LangAlpha 的智能体必须用原生工具调用和流式输出，所以 broker 不能作为主路径。
- OpenClaw 网关在 `127.0.0.1:18789` 已经开放了 OpenAI 兼容的 `/v1/chat/completions`，支持工具调用、SSE 流式和用量统计。

**方案（先 A，再 C）：**

- **A. 主路径：把 OpenClaw 网关注册为 LangAlpha 的一个模型供应商。**
  - `providers.json` 新增 `openclaw` 供应商，`base_url` 指向 `http://127.0.0.1:18789/v1`。
  - 写脚本 `scripts/sync_openclaw_models.py`，从 `openclaw.json` 只读模型的公开信息（模型名、上下文、最大输出），再合并 Dalton 的价格卡和实测上限，生成 `models.json` 条目。每个条目用请求头 `x-openclaw-model` 指定 OpenClaw 里的具体模型。
  - 在 OpenClaw 里新建一个专用的透传 agent：不带工具、空工作区、不配回退。目的是不让 OpenClaw 自己的提示词和工具混进来，也不让它悄悄换模型。这一步要改 `openclaw.json`，需要 owner 操作。
  - **先做冒烟测试**，确认三件事：网关是否按我们发送的完整对话历史处理；是否会注入 OpenClaw 自己的提示词；推理强度和缓存 token 能否透传。
- **C. 第二步：一次性调用走 broker。** 标题、摘要、结构化输出这类不需要工具和流式的调用，改走 Dalton broker，拿回调用日志和"不重复付费"。
- **备选：** 如果冒烟测试发现网关会改写对话历史或注入提示词，就改用 LangAlpha 原生支持的 Claude 和 Codex 订阅登录，不再经过 OpenClaw。这一步需要 owner 决定。

### 4.4 数据源

做成插件包 `plugins/dalton/` 下的数据接口（MCP server），按 LangAlpha 的统一返回格式输出：

| 数据 | 来源 | 做法 |
|---|---|---|
| 内部研究库：AlphaEngine、sales notes、TMTB、FUNDA AI、Guidepoint、电话会纪要、一致预期 | OpenClaw 现有的 researchdata 接口 | 复用 `~/.openclaw/workspace/scripts/researchdata_mcp.py`、`data_gateway.py`、`research_library.py` 的逻辑，包成一个多路由的接口：`search`、`read`、`list` |
| 基金 wiki：174 家公司、28 个行业、1,291 篇文章 | `~/.openclaw/workspace/wiki/`，带 `wiki.db` 和向量索引 | 只读检索接口；深度研究写完后回写关键发现 |
| 美股公告与财务 | LangAlpha 自带 SEC 工具 + 旧 Dalton 的 XBRL 经验 | 先用自带的，缺什么补什么 |
| 港股、A 股公告 | 旧 Dalton 的 HKEX、巨潮接入经验 | 第二期新建接口 |
| 价格与估值 | LangAlpha 自带行情 + yfinance | 直接用 |
| 社交与另类数据：X、雪球 | LangAlpha 的另类数据插件 + 新建雪球接口 | 第二期 |

**要先验证：** LangAlpha 的沙箱在 Docker 里，能否访问宿主机 localhost 上的 AlphaEngine（8950）和 Guidepoint（8943）端口。如果不行，就把这些数据接口放在后端进程侧，而不是沙箱里。

### 4.5 方法论与 skill

**基金方法论（`dalton-conventions` skill，所有 Dalton skill 都先读它）**，合并三处来源：
- **Playbook v1：** 初筛四问、深度认知 12 问、完整覆盖 12 个关键问题、反方论证、来源层级、模型期间标准、风险回报标准、事件五词结论（NO_CHANGE、STRENGTHENED、WEAKENED、BROKEN、NEW）。
- **培训手册精粹 4 篇：** 研究流程、memo 模板、跟踪清单、分析师等级。
- **OpenClaw skill 里的方法纪律：**
  - 判断先行；
  - 来源身份分层，按来源链去重；
  - 分析师在电话会提问里说的数字，若管理层未确认，标"提问口径，管理层未确认"；
  - "本轮未取得"不等于"公司未披露"；
  - 数字底表、Good Business / Good People / Good Price、Variant Perception 三问；
  - 净现金双口径、隐含资产、股东回报 R1/R2/R3 评分；
  - 中文写作规范。

**skill 家族的处理（借原理、按新机制重新设计，不照搬 OpenClaw 的 `SKILL.md`）：**

| 家族 | 权威参考 | 在新项目里的形态 | 和 LangAlpha 自带 skill 的关系 |
|---|---|---|---|
| bull/bear | `bullbear-discussion` 第 20 版（v4.2） | skill `/bullbear` 对话快速版 + workflow 完整成稿；计算脚本放进 skill 的 `scripts/` | 新建；结尾借用 trade-pitch 的情景和表达方式 |
| 公司背景 | `company-background`（9 段） | skill | 自带的 company-profile 只做一页幻灯片，保留作附件 |
| 初次覆盖 | company-background + 行业支线 + 财务镜头 + 估值历史 + bull/bear 快速版 | workflow `initial-coverage`，输出 12 问初筛，并初始化 thesis | — |
| 完整覆盖 | `company-deepdive-mx-0911`（12 问、四条取证支线、数字底表、干净上下文审稿） | workflow `full-coverage` | 基于 initiating-coverage 另起一版：保留它的三表、DCF、comps 建模，研究正文换成 12 问 |
| 建模 | LangAlpha 自带：3-statements、dcf-model、comps、model-update、check-model | 直接用；新增 skill `dalton-financial-lenses`（净现金、隐含资产、股东回报） | 扩展 |
| 财报前瞻 | `company-preview`（真实门槛五分、高频数据、debate 按机制聚类） | 自动任务 T-3 触发 | 扩展自带的 earnings-preview |
| 财报点评 | `earnings-update` 简版 + `company-earnings-update` 八节 + 硬件叠加层 | 自动任务 T0 快讯 + 按需深度点评 → 模型更新 → thesis 校准 | 扩展自带的 earnings-analysis |
| 财报日期 | `richard-earnings-tracker`（只认官方日期，三态滚动，对账） | 每周自动任务 | 扩展自带的 catalyst-calendar |
| 股价异动 | `stock-move-analyzer`（先量化：用同组和板块 ETF 分清是板块 beta 还是个股 alpha；再按 filing、新闻、卖方、社媒的顺序找原因） | skill + 价格触发 | 新建 |
| 每日 thesis 增量 | `stock_tracker.py`（有重要变化才推送） | 每晚自动任务 | 状态格式用自带的 thesis-tracker |
| 周度复盘 | `coverage-update-richard`（全名单筛选、Core 逐家五源、Watch 只看异动）+ 已归档 weekly-review 的"EPS 修正 vs 估值倍数"归因 | workflow `weekly-coverage-review` + 每周自动任务 | 新建 |
| 专题研究 | `topic-deep-dive` v8 | workflow `topic-research` | 借用自带的 sector-overview、impact-analysis 模板 |
| 高 conviction call | bull/bear 的预期差 + deep dive 的仓位建议格式 | skill `dalton-call` | 基于 trade-pitch 另起一版 |
| 选股 | 第二曲线七维、做空四维、尾险 12 项、新股周筛 | 写成 idea-generation 的新原型 | 扩展；第二期 |
| 监控看板 | 情绪、杠杆、行业周报 | 定时自动任务 | 第二期 |

**迁移时要剥掉的：** 飞书分块写入、25 分钟上限、Discord 排版限制、写死在 skill 里的模型名、人名和账号。人名和账号改成工作区配置。

### 4.6 公司知识层（新建，最核心）

LangAlpha 的工作区文件可以自由改写，但没有结构和版本。分析师的认知需要可读、可比、可追溯。每家公司一个工作区，包含：

| 文件 | 内容 | 谁写 |
|---|---|---|
| `agent.md` | 这家公司的目标、当前判断一句话、各文件索引 | 分析师 |
| `thesis.md` + `thesis.json` | thesis 支柱、证据台账（只追加）、确认、警告、证伪阈值；当前 house view | 分析师；正式版本需 PM 批准 |
| `debate.md` | 当前 top debates：分歧变量、多方、空方、最强反驳、我的判断、改判条件 | 分析师 |
| `drivers.json` | 关键驱动、领先指标、最新读数、敏感度 | 分析师 |
| `management.md` | 管理层风格：给指引的习惯（保守还是激进）、历史兑现记录、资本配置 | 分析师 |
| `lessons.md` | 经验教训：适用条件、反例；可撤回 | 分析师 |
| `agenda.md` | 研究议程，见 4.7 | 分析师 |
| `model/*.xlsx` | 财务模型及版本 | 分析师 |
| `published/` | 正式发布的成果，不可改写 | 发布治理 |

**版本管理：** 结构化文件每次修改都存一个快照，记录旧值、新值、原因、证据。UI 能对比任意两个版本。

**要先确认：** LangAlpha 每轮结束会给工作区文件做快照，这能否直接满足版本对比；不够的话，补一个轻量的版本表。

### 4.7 自主研究议程（新建）

目标是让分析师"自己安排工作、持续研究、空闲时不花钱"。

1. **议程（`agenda.md`）：** 未解决的关键问题、下一项最值得做的验证、待观察的事件和指标、上次结论和触发条件。分析师每次工作结束时更新议程，并通过写自动任务文件，给自己定下一次唤醒的时间和理由。
2. **零成本唤醒检查：** 由一层不调用模型的检查判断有没有值得做的事，有才唤醒分析师。检查的内容：
   - 新公告（轮询公告接口）；
   - 财报日历；
   - 价格异动（LangAlpha 自带价格触发）；
   - 议程里到期的验证；
   - PM 的新问题。
3. **研究总监：** 由 LangAlpha 主页的跨工作区角色扮演，每天看所有覆盖公司的议程，按价值和紧迫度排序，在当天额度内决定研究谁、做什么，派给各公司的分析师；周会前汇总复盘。PM 可以随时和它对话，调整优先级。
4. **退避：** 同样的证据、同样的方法反复没有进展时，自动拉长下一次唤醒的间隔。交给 PM 的事项不在后台空转。

**判断"在自主研究"的标准：** 一周内不派任何任务，每家覆盖公司都至少有一次由事件或议程触发的研究，并留下事件五词结论和下一项验证；日花费不超额度。

### 4.8 发布治理（新建）

- **草稿：** 分析师写在工作区里，随时可读。核验只给反馈，并在报告里标出"未核实的数字"，不阻止保存和继续研究。
- **正式发布：** 新增工具 `publish_report`，依次做三件事：
  1. **数字核验：** 抽出报告里的每个数字，和来源记录、模型单元格对账，找不到来源的列出来，退回给分析师修改；
  2. **独立审稿：** 由不同模型家族的审稿子智能体判断论证和含义；
  3. **PM 审批：** 用 LangAlpha 自带的审批卡片，只有 PM 能批。
- 批准后写入 `published/`，不可改写；thesis 正式版本同步更新。
- **可借用的现成机制：** LangAlpha 的来源记录中间件已经记录了每次数据访问；下单审批"批一次、执行一次"的模式可以照搬。

### 4.9 本地预算（新建）

- **现状：** LangAlpha 开源模式下预算控制完全不生效。
- **做法：** 新写一个本地版的额度检查，接口和 LangAlpha 现有的一致，语义按旧 Dalton：
  - 共享日美元额度和可选 task cap 是唯一的硬约束；
  - 发送前预留，结算按实际用量；
  - 未结算的调用按全额占用；
  - 进程崩溃时按保守额度入账，不释放。
- **订阅类模型**（Claude、antigravity、ChatGPT 登录）单独记账，不计入现金额度。
- **待决：** 和旧 Dalton 共用一个日额度，还是新项目独立一个额度。

### 4.10 界面

直接用 LangAlpha 的界面，不再自己仿。在它上面加：

- 公司覆盖视图：thesis、debate、驱动指标、管理层、议程、模型、已发布成果，支持版本对比。
- 发布审批卡片、conviction call 卡片。
- 中文界面文案；交付默认中文，保留英文术语。
- 关掉用不上的部分：券商、下单、行情看板。

### 4.11 多用户

LangAlpha 目前所有数据按用户隔离，没有机构、团队、PM 和研究员的角色区分。第一期先单用户运行，由 owner 一人同时扮演 PM。第二期再做机构级的共享知识层和角色权限。

## 5. 开发计划

每个阶段对应分析师成长的一步。**完成标准都是 PM 能读、能评的成果。**

| 阶段 | 内容 | 完成标准 | 估计 |
|---|---|---|---|
| **P0 接通** | 本地跑起 LangAlpha（Docker）；OpenClaw 模型接入方案 A 和冒烟测试；数据接口：内部研究库、基金 wiki、SEC；本地预算；沙箱访问本机端口的验证 | 在 LangAlpha 界面里，用 OpenClaw 的模型，问一个关于 META 的问题，能看到它调用内部研究库和 wiki 作答，费用正确计入日额度 | 1 周 |
| **P1 入职** | `dalton-conventions`（方法论合并）；覆盖名单（Core 27 / Watch 78）；为 META、AMZN、GOOGL、MSFT 建工作区，把已有的 thesis 和 OpenClaw 跟踪状态迁成公司知识层文件 | 问"你对 META 的看法和主要 debate"，回答引用的是迁入的认知，而且符合方法论的结构 | 1 周 |
| **P2 初次覆盖** | `initial-coverage` workflow；skill：bull/bear 快速版、公司背景、财务镜头 | 选一家未覆盖的公司（建议你指定），产出初次覆盖报告和初始 thesis；PM 盲评达到"可用" | 1–2 周 |
| **P3 完整覆盖** | `full-coverage` workflow；Excel 三表模型和 DCF；独立审稿；发布治理 | 一家公司完整覆盖：12 问 memo 加带公式的 Excel 模型，经审稿和 PM 审批后正式发布 | 2 周 |
| **P4 持续覆盖** | 股价异动、每晚 thesis 增量、财报前瞻和点评链、公告监控；公司知识层随事件更新 | 连续一周，四家公司都有事件驱动的更新和事件五词结论；赶上财报季的话，完成一次完整的前瞻、点评、模型更新、thesis 校准 | 2 周 |
| **P5 自主议程** | 议程文件、零成本唤醒检查、研究总监、自主排期；周会复盘；PM 问答；conviction call | 一周不派任务，研究照常推进；周会稿包含股价归因、观点变化和至少一条明确的"call"或"暂无高 conviction" | 2 周 |
| **P6 扩展** | 港股和 A 股数据、雪球和 X、选股原型、监控看板、多用户与角色权限 | 按需 | — |

**并行的工作：** 每个阶段结束都做一次 LangAlpha 界面走查，用真实数据截图，由你签收。

## 6. 度量

- **研究质量：** PM 盲评分数；每周正式发布的成果数量。
- **数字可靠性：** 正式发布成果里没有来源的数字为 0。
- **自主性：** 不派任务的一周里，由事件或议程触发的研究次数。
- **成本：** 每日花费不超额度；空闲日的模型花费接近 0。
- **响应：** PM 提问到得到回答的时间。

不以测试数量作为完成标准。测试只用来保证底线。

## 7. 风险与待决事项

| 事项 | 说明 | 谁决定 |
|---|---|---|
| OpenClaw 专用透传 agent | 需要修改 `openclaw.json` | owner |
| 网关行为 | 是否改写对话历史、注入提示词；不行就改用原生订阅登录 | 冒烟测试后 owner 决定 |
| 预算池 | 和旧 Dalton 共用，还是新项目独立 | owner |
| 第一家初次覆盖的公司 | P2 用 | owner |
| 交付渠道 | 飞书消息即可，还是要直接建飞书文档（LangAlpha 是否支持未验证） | owner |
| 沙箱访问本机端口 | 决定数据接口放沙箱还是后端 | P0 验证 |
| "独立 Dalton"部署 | 有记录提到本机有一个独立 Dalton 部署在访问 AlphaEngine 和 Guidepoint 端口，和新项目的关系不明 | owner 确认 |
| 合规口径 | wiki 里的专家访谈和高管交流已过合规流程，要写进方法论，避免模型自行排除 | 写入 P1 |
| 上游同步 | LangAlpha 更新很快（一周 176 个提交）。我们的改动尽量放在插件包和独立模块里，定期合并上游 | 持续 |

## 8. 从旧 Dalton 和 OpenClaw 带过来什么

**带过来：**
- 方法论：Playbook v1（`dalton-research-agent-os/deploy/phase9/p9a-research-playbook-v1.json`）、培训手册精粹（`~/.openclaw/workspace-chem/references/manual/`）、OpenClaw skill 的方法纪律。
- 数据接入逻辑：researchdata 多路由、wiki 检索、SEC XBRL、HKEX、巨潮的经验。
- 模型接口：profile 的上限和价格卡、回退只针对供应商故障、"不重复付费"的语义。
- 底线原则：见 3.1。
- 已有认知：四家公司的 thesis、debate map、模型。只读导出，转成公司知识层文件。

**不带：**
- 六阶段阶梯、深度认知门、mission 流水线、规划器。
- 候选身份、回执、lease、调度器这一整套治理机制。新项目里由 LangAlpha 的运行账本和发布治理替代。
- 旧 Cockpit 界面。

## 附录：主要路径

- 新项目：`/Users/everflow/Projects/dalton-langalpha`（分支 `dalton/main`）
- LangAlpha 关键文件：`AGENTS.md`、`agent_config.yaml`、`plugins/README.md`、`mcp_servers/AGENT_CONTRACT.md`、`src/ptc_agent/agent/middleware/skills/registry.py`、`src/server/models/automation.py`、`src/server/services/credit_gate_port.py`、`src/llms/manifest/`
- LangAlpha 自带研究 skill：`plugins/langalpha_research/skills/`
- OpenClaw skill：`~/.openclaw/workspace/skills/`、`~/.openclaw/agents/main/agent/workshop-skills/`
- OpenClaw 数据入口：`~/.openclaw/workspace/scripts/{data_gateway.py,research_library.py,researchdata_mcp.py,consensus_estimates.py,stock_tracker.py}`
- 基金 wiki：`~/.openclaw/workspace/wiki/`
- 旧 Dalton：`/Users/everflow/Projects/dalton-research-agent-os`（main）、`/Users/everflow/Projects/dalton-batch-cnhk`（最新开发线）
- 旧 Dalton 模型 broker：`dalton-batch-cnhk/integrations/openclaw-model-broker/`、`src/dalton_core/openclaw_model_adapter.py`、`model_router.py`
