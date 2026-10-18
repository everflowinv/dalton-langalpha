"""Dalton built-in skill registry definitions.

Merged into ``SKILL_REGISTRY`` at the end of ``registry.py`` via
``DALTON-HOOK(skills-registry)``. Each entry's ``description`` must match its
``SKILL.md`` YAML frontmatter ``description`` verbatim.
"""

from __future__ import annotations

from .registry import SkillDefinition

DALTON_SKILL_DEFINITIONS: dict[str, SkillDefinition] = {
    "dalton-conventions": SkillDefinition(
        name="dalton-conventions",
        description=(
            "Fund methodology for Dalton (fundamental long-biased hedge fund): research questions "
            "by coverage stage, source hierarchy and identity, expert-call compliance, model period "
            "standards, risk/reward standards, five-word event verdicts, Chinese delivery. Read "
            "before any Dalton deliverable or knowledge-file write."
        ),
        skill_md_path="skills/dalton-conventions/SKILL.md",
        requires=("research-conventions",),
    ),
    "dalton-init-company": SkillDefinition(
        name="dalton-init-company",
        description=(
            "Initialize or upgrade a Dalton company coverage workspace skeleton (agent.md with "
            "managed summary block, directory structure, READMEs, and idempotent .dalton/init.json "
            "manifest) without overwriting analyst-modified files."
        ),
        skill_md_path="skills/dalton-init-company/SKILL.md",
        command="dalton-init-company",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-knowledge": SkillDefinition(
        name="dalton-knowledge",
        description=(
            "Maintain a company workspace's structured knowledge layer (thesis, debate, drivers, "
            "management, lessons, agenda, models, verdicts) through knowledge_update and "
            "knowledge_append_verdict."
        ),
        skill_md_path="skills/dalton-knowledge/SKILL.md",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-publish": SkillDefinition(
        name="dalton-publish",
        description=(
            "Draft verification (check_draft) and formal publication governance (publish_report): "
            "footnote conventions, facts ledger, independent cross-family review, and PM approval."
        ),
        skill_md_path="skills/dalton-publish/SKILL.md",
        command="dalton-publish",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-agenda": SkillDefinition(
        name="dalton-agenda",
        description=(
            "Closes every research session with verdict recording, agenda updates, and optional "
            "verification scheduling."
        ),
        skill_md_path="skills/dalton-agenda/SKILL.md",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-research-director": SkillDefinition(
        name="dalton-research-director",
        description=(
            "Autonomous Research Director running in Dalton Home — scores coverage board, "
            "allocates daily/weekly budget, delegates to company analysts, and coordinates "
            "cross-company research."
        ),
        skill_md_path="skills/dalton-research-director/SKILL.md",
        command="dalton-director",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-financial-lenses": SkillDefinition(
        name="dalton-financial-lenses",
        description=(
            "Dalton 三把财务镜头与盈利质量计算：在同一报告期、汇率与市值锚点下，计算基础/严格净现金（Definition A/B）、"
            "隐含资产（折前/折后及隐含经营资产）、股东回报 R1/R2/R3 二十分制评分与盈利质量（Sloan 比率、CFO/NI、CCC）。"
            "触发词：财务镜头、净现金、net cash、隐含资产、hidden assets、股东回报、R1/R2/R3、盈利质量。"
        ),
        skill_md_path="skills/dalton-financial-lenses/SKILL.md",
        command="dalton-financial-lenses",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-company-background": SkillDefinition(
        name="dalton-company-background",
        description=(
            "Rapid 9-section company background sheet (business, segments, 3Y annual, 4Q quarterly, "
            "balance sheet, market data, moat, catalysts, risks) with strict fact-vs-view separation."
        ),
        skill_md_path="skills/dalton-company-background/SKILL.md",
        command="dalton-company-background",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-bullbear": SkillDefinition(
        name="dalton-bullbear",
        description=(
            "个股多空争论（Bull/Bear Discussion）：默认在对话内快速交付共识、2–4个核心Debate、盈利预测修正三层与隐含费用空间、"
            "3–6个月催化剂与改判条件；需要完整文档时启动 bullbear-doc workflow 产出 v4.2 结构长稿（含独立股东回报支线与财务体检）。"
            "触发词：/bullbear、多空观点、多空在争什么、bull bear、earnings revision、variant perception、近期催化剂。"
        ),
        skill_md_path="skills/dalton-bullbear/SKILL.md",
        command="bullbear",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-initial-coverage": SkillDefinition(
        name="dalton-initial-coverage",
        description=(
            "买方初次覆盖（Initial Coverage / 12问初筛）：启动 initial-coverage workflow 并行执行公司背景、行业与竞争、"
            "三把财务镜头、估值历史与可比四条支线，汇总多空快速版与12问初筛报告，并初始化公司知识层（company/thesis/debate/"
            "drivers/management/agenda 与 NEW_THESIS 结论）。触发词：/initial-coverage、初次覆盖、首次覆盖、初筛报告、initial coverage。"
        ),
        skill_md_path="skills/dalton-initial-coverage/SKILL.md",
        command="initial-coverage",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-full-coverage": SkillDefinition(
        name="dalton-full-coverage",
        description=(
            "买方完整覆盖（Full Coverage / 12问深度备忘录与三表DCF模型）：启动 full-coverage workflow 完成一手材料与数字底表"
            "（FACTS-LEDGER）、12问问题清单（frame）、A/B/C/D 四支线并行取证、15页Excel模型（三表+DCF+Comps+Facts+Guidance vs "
            "Actual+Street vs House）、三段式12问投资备忘录、模型与成稿双审稿、知识层修订与 publish_report 正式发布。"
            "触发词：/full-coverage、完整覆盖、深度覆盖、深度报告、投资备忘录、full coverage、investment memo。"
        ),
        skill_md_path="skills/dalton-full-coverage/SKILL.md",
        command="full-coverage",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-earnings-calendar": SkillDefinition(
        name="dalton-earnings-calendar",
        description=(
            "每周一次确认覆盖名单下一次财报的官方披露日期，维护 pending/confirmed/reported 三态台账，经 REST helper 生成或改期 "
            "tm3/t0/tp1 自动任务并同步 agenda.watch.events，执行读回对账与固定顺序总结。触发词：财报日历、更新财报日期、earnings calendar。"
        ),
        skill_md_path="skills/dalton-earnings-calendar/SKILL.md",
        command="dalton-earnings-calendar",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-earnings-preview": SkillDefinition(
        name="dalton-earnings-preview",
        description=(
            "财报前三个交易日（T-3）产出一份财报前瞻：区分五类预期门槛（buyside bogey）与高频数据、业绩关注点与 EPS 质量及指引隐含门槛、"
            "按机制聚类的核心 debate 与一轮 X、三情景与期权隐含波动，并生成结构化 predictions.json 供财报后对账。"
            "触发词：财报前瞻、earnings preview、T-3 前瞻。"
        ),
        skill_md_path="skills/dalton-earnings-preview/SKILL.md",
        command="dalton-earnings-preview",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-earnings-flash": SkillDefinition(
        name="dalton-earnings-flash",
        description=(
            "财报发布后数小时内（T0+N 小时）产出一份财报快讯：头条数字与上一季同比、关键经营 KPI、指引变化、电话会 Q&A 要点、"
            "卖方初评与 sales 反馈、前瞻要点初步对照及事件五词初判，并确认次日深度链自动任务。"
            "触发词：财报快讯、earnings flash、业绩快讯、T0 快讯。"
        ),
        skill_md_path="skills/dalton-earnings-flash/SKILL.md",
        command="dalton-earnings-flash",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-earnings-deep": SkillDefinition(
        name="dalton-earnings-deep",
        description=(
            "财报次日（T+1 交易日 20:00）启动 earnings-deep-update 深度链（也可独立产出八节深度点评）：并行完成八节深度报告与 Excel "
            "模型更新，随后执行前瞻 predictions.json 逐条对账、thesis 支柱校准与管理层指引兑现记录更新，审稿后写入知识层工作版本。"
            "触发词：财报深度点评、earnings deep、八节财报报告、T+1 深度链。"
        ),
        skill_md_path="skills/dalton-earnings-deep/SKILL.md",
        command="dalton-earnings-deep",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-stock-move": SkillDefinition(
        name="dalton-stock-move",
        description=(
            "分析个股股价异动（涨跌归因）：先跑量化脚本对比同组中位数与板块 ETF（区分板块 beta 与个股 alpha、计算 60 日超额 z 值与 "
            "20 日量比），再按 filing → 新闻 → 卖方 → 社媒顺序找原因，给出对 thesis/debate 的含义与事件五词结论；支持价格触发自动运行"
            "（beta 无个股事件不推送）。触发词：股价异动、今天为什么跌、今天为什么涨、stock move、异动归因、/dalton-stock-move。"
        ),
        skill_md_path="skills/dalton-stock-move/SKILL.md",
        command="dalton-stock-move",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-thesis-delta": SkillDefinition(
        name="dalton-thesis-delta",
        description=(
            "每晚覆盖公司 thesis 增量跟踪：由后端预检（consensus/sec/internal/price 探针）通过 <dalton_wake> 唤醒后，运行 collect.py "
            "生成一致预期快照与增量包，按 materiality.md 判断是否存在重要变化；有重要变化才写增量笔记、更新知识层并推送 PM，"
            "无重要变化只记 NO_CHANGE 与 seen.json 不打扰 PM。触发词：thesis delta、每晚增量、日常跟踪、/dalton-thesis-delta。"
        ),
        skill_md_path="skills/dalton-thesis-delta/SKILL.md",
        command="dalton-thesis-delta",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-filing-review": SkillDefinition(
        name="dalton-filing-review",
        description=(
            "覆盖公司 SEC 公告审阅（公告监控响应）：被 watch 预检的 sec 探针通过 <dalton_wake> 唤醒（或手动指定 accession）后，"
            "按 form-guide.md 审阅 10-Q/10-K、8-K、融资文件（S-3/424B）、DEF 14A、13D/13G 与 Form 4，对照 thesis/debate "
            "给出事件五词结论，写事件笔记并更新知识层。触发词：公告审阅、filing review、10-Q 审阅、10-K 审阅、8-K 审阅、/dalton-filing-review。"
        ),
        skill_md_path="skills/dalton-filing-review/SKILL.md",
        command="dalton-filing-review",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-weekly-review": SkillDefinition(
        name="dalton-weekly-review",
        description=(
            "启动每周覆盖周会（weekly-coverage-review workflow）：在 Home 对全覆盖名单做同组异动筛选（screen_movers.py）与 EPS 修正 vs "
            "估值倍数归因（attribution.py），Core 逐家并行五源深挖，Watch 仅对命中公司定向检索，汇总一周 verdicts.md 观点变化并给出"
            "至少一条 conviction call 或“暂无高 conviction”，知识层修改以提案经 delegate_to_analyst 交办。"
            "触发词：周会、覆盖周报、weekly review、/weekly-review。"
        ),
        skill_md_path="skills/dalton-weekly-review/SKILL.md",
        command="weekly-review",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-topic-research": SkillDefinition(
        name="dalton-topic-research",
        description=(
            "启动专题研究（topic-research workflow）：围绕产业、竞争、监管或技术专题按 Phase 0–7 执行多集群并行证据收集（精读全文 + "
            "内部纪要/Guidepoint 20 词限制）、盲区检查（写下“盲区检查完成”）、置信度标注校验（validate_topic.py）、数字核验与 "
            "dalton-reviewer 独立审稿，回写相关公司知识层与复利教训，不写回基金 wiki。"
            "触发词：专题研究、专题深度、行业研究、竞争格局研究、topic research、/topic-research。"
        ),
        skill_md_path="skills/dalton-topic-research/SKILL.md",
        command="topic-research",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-call": SkillDefinition(
        name="dalton-call",
        description=(
            "生成高确信度投资建议（Conviction Call）：先做 6 条证据门禁硬检（不满足则诚实输出“暂无高 conviction”而绝不硬凑），"
            "通过门禁后完成预期差三问、2–5 条可证伪主张、三档情景与 scenario_irr.py 概率加权 IRR 计算、仓位级别（Core/Tactical/Small）、"
            "对冲方案及退出与监控条件，经 PM 审批后更新知识层。触发词：conviction call、推荐报告、高确信度建议、买入建议、做空建议、/call、/dalton-call。"
        ),
        skill_md_path="skills/dalton-call/SKILL.md",
        command="dalton-call",
        requires=("dalton-conventions", "research-conventions"),
    ),
    "dalton-pm-qa": SkillDefinition(
        name="dalton-pm-qa",
        description=(
            "处理基金经理（PM）日常互动问答：先读知识层（thesis/debate/drivers/verdicts/published/agenda），按问题类型分流"
            "（直接回答、补查实时信息、或提议新研究），以固定五段结构（直接回答、依据、和现有观点的关系、不确定性与改判条件、下一步）输出，"
            "并在引入新证据或改变判断时执行收盘三步更新 verdicts 与 agenda（origin='pm'）。"
            "触发词：PM 提问、回答 PM、观点问答、怎么看、为什么我们认为、/pm-qa、/dalton-pm-qa。"
        ),
        skill_md_path="skills/dalton-pm-qa/SKILL.md",
        command="pm-qa",
        requires=("dalton-conventions", "research-conventions"),
    ),
}
