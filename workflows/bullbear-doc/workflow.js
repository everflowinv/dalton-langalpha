export const meta = {
  name: 'bullbear-doc',
  description: 'Dalton bull/bear v4.2 完整成稿：回报支线 + 主稿 + 格式检查 + 独立审稿',
}

const W = args.workspace_dir || '.'
const T = args.ticker
const C = args.company || T
const D = args.as_of
const days = args.research_days || 92
const runDir = `${W}/research/${D}-bullbear`
const S = '.agents/skills/dalton-bullbear'
const conv = `先读 .agents/skills/dalton-conventions/SKILL.md 与 ${S}/SKILL.md、${S}/references/v42-structure.md。`

const PREP_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
    gaps: { type: 'array', items: { type: 'string' } },
  },
  required: ['files', 'summary'],
}

const FILES_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
    sha256: { type: 'string' },
    gaps: { type: 'array', items: { type: 'string' } },
  },
  required: ['files'],
}

const DRAFT_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
    warns: { type: 'number' },
    gaps: { type: 'array', items: { type: 'string' } },
  },
  required: ['files', 'summary'],
}

phase('准备')
const prep = await agent(
  `${conv}
为 ${C}（${T}）准备 bull/bear 成稿的底稿，写到 ${runDir}/：
1) 跑 .agents/skills/dalton-financial-lenses/scripts/anchors.py 锁定市值与汇率 → ${runDir}/anchors.json；
2) 跑 ${S}/scripts/fincheck.py → ${runDir}/fincheck.md；
3) 取一致预期快照（存 ${W}/data/consensus/）+ 跑 ${S}/scripts/implied_model.py → ${runDir}/consensus.md；
4) 价格锚（收盘价、日期、来源）→ ${runDir}/price.md。
返回各文件路径与一句话摘要。`,
  { agentType: 'dalton-evidence', label: `${T} 准备`, phase: '准备', schema: PREP_SCHEMA }
)

phase('股东回报支线')
const ret =
  prep === null
    ? null
    : await agent(
        `${conv}
按 ${S}/references/return-branch.md 与 .agents/skills/dalton-financial-lenses/references/shareholder-return.md 写股东回报第一节（≤40 行）、附录（≤30 行）和 5 条摘要，
分母统一用 ${runDir}/anchors.json，评分必须用 .agents/skills/dalton-financial-lenses/scripts/return_score.py 计算。
输出 ${runDir}/return.md、${runDir}/return_appendix.md、${runDir}/return_summary.md。`,
        { agentType: 'dalton-evidence', label: `${T} 股东回报`, phase: '股东回报支线', schema: FILES_SCHEMA }
      )

phase('主稿')
const main = await agent(
  `${conv}
写 ${C}（${T}）Bull/Bear Discussion 主稿到 ${runDir}/main.md，严格按 v4.2 结构（${S}/references/v42-structure.md 与 ${S}/references/ai-revenue-rules.md）。
研究窗口 ${days} 天（截至 ${D}）。股东回报只引用 ${runDir}/return_summary.md，预留 <!-- INSERT:return --> 与 <!-- INSERT:return_appendix --> 占位，不自行重算。
${args.prior_draft ? `上一版：${args.prior_draft}，“我的判断”有变化要写明原因。` : ''}
必须检索：内部库（sales-notes、tmtb、funda-ai）、company-wiki（只读）、AlphaEngine（foreign_report、sell_side_comment、meeting_minutes 分开搜）、Guidepoint（英文检索，原话 ≤20 英文词）、最新 filing 与电话会。
写完跑 python ${S}/scripts/bb_check.py ${runDir}/main.md，直到 0 WARN。`,
  { agentType: 'general-purpose', label: `${T} 主稿`, phase: '主稿', schema: DRAFT_SCHEMA }
)

phase('合并与审稿')
const merged =
  main === null
    ? null
    : await agent(
        `合并 ${runDir}/main.md 与回报支线文件（把 ${runDir}/return.md 插到 <!-- INSERT:return -->，把 ${runDir}/return_appendix.md 插到 <!-- INSERT:return_appendix -->）→ ${W}/drafts/${D}-bullbear.md，跑 python ${S}/scripts/bb_check.py ${W}/drafts/${D}-bullbear.md 到 0 WARN，计算并返回 ${W}/drafts/${D}-bullbear.md 的 sha256。`,
        { agentType: 'general-purpose', label: `${T} 合并`, phase: '合并与审稿', schema: FILES_SCHEMA }
      )

const review =
  merged === null
    ? null
    : await agent(
        `审阅 ${W}/drafts/${D}-bullbear.md（支线材料在 ${runDir}/，只读不改任何文件）。按审稿清单输出。重点：每个 Debate 的“我的判断”是否有明确立场、改判条件是否可观察；§2 是否把价格走势当共识；§4 修正分层是否能回指一致预期快照。`,
        { agentType: 'dalton-reviewer', label: `${T} 审稿`, phase: '合并与审稿' }
      )

phase('修订')
const fixed =
  review === null
    ? null
    : await agent(
        `先核对 ${W}/drafts/${D}-bullbear.md 的 sha256 是否仍为 ${merged && merged.sha256 ? merged.sha256 : '合并时记录值'}（若被审稿子智能体改动则先还原并标注审稿违规）。然后按审稿意见修订 ${W}/drafts/${D}-bullbear.md：P0 全改，P1 能改就改，不新增未核实数字；修完再跑 python ${S}/scripts/bb_check.py ${W}/drafts/${D}-bullbear.md 确认 0 WARN。审稿意见如下：\n${review}\n返回改动清单与剩余 P1。`,
        { agentType: 'general-purpose', label: `${T} 修订`, phase: '修订', schema: DRAFT_SCHEMA }
      )

const reviewVerdict = review
  ? review
      .split('\n')
      .find((l) => l.includes('可发布') || l.includes('需修订') || l.includes('不得发布')) || '已审阅'
  : null

return {
  ok: merged !== null,
  draft: `${W}/drafts/${D}-bullbear.md`,
  review_verdict: reviewVerdict,
  fixed: fixed && fixed.summary,
  failed: [prep, ret, main, merged, review].map((x) => x === null),
}
