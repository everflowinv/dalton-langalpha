export const meta = {
  name: 'initial-coverage',
  description: 'Dalton 初次覆盖：背景∥行业∥财务镜头∥估值历史 → bull/bear 快速版 → 12 问初筛 → 知识层草案 → 审稿',
}

const { ticker: T, company: C, workspace_dir: W, as_of: D } = args
const R = `${W}/research/${D}-ic`
const base = `先读 .agents/skills/dalton-conventions/SKILL.md；公司 ${C || T}（${T}），市场 ${args.market || 'US'}，截至 ${D}。`
const OUT = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
    sha256: { type: 'string' },
    gaps: { type: 'array', items: { type: 'string' } },
  },
  required: ['files', 'summary'],
}

phase('取证')
const [bg, ind, lens, val] = await parallel([
  () =>
    agent(
      `${base} 按 .agents/skills/dalton-company-background/SKILL.md 写 9 段背景稿到 ${W}/drafts/${D}-background.md。`,
      { agentType: 'dalton-evidence', label: '公司背景', phase: '取证', schema: OUT }
    ),
  () =>
    agent(
      `${base} 按 .agents/skills/dalton-initial-coverage/references/branch-industry.md 写行业支线到 ${R}/industry.md。${
        args.peers && args.peers.length ? '同业候选：' + args.peers.join(',') : ''
      }`,
      { agentType: 'dalton-evidence', label: '行业支线', phase: '取证', schema: OUT }
    ),
  () =>
    agent(
      `${base} 按 .agents/skills/dalton-financial-lenses/SKILL.md 做三把镜头，JSON 到 ${R}/lenses.json，草稿到 ${W}/drafts/${D}-lenses.md。`,
      { agentType: 'dalton-evidence', label: '财务镜头', phase: '取证', schema: OUT }
    ),
  () =>
    agent(
      `${base} 按 .agents/skills/dalton-initial-coverage/references/branch-valuation-history.md 写估值历史与可比（含 ln(P1/P0)=ln(EPS1/EPS0)+ln(PE1/PE0) 分解表）到 ${R}/valuation.md，可比表 ${R}/comps.xlsx。`,
      { agentType: 'dalton-evidence', label: '估值历史与可比', phase: '取证', schema: OUT }
    ),
])

const got = { bg, ind, lens, val }
const missing = Object.entries(got)
  .filter(([, v]) => v === null)
  .map(([k]) => k)
if (missing.length === 4) {
  return { ok: false, reason: '四条支线全部失败', missing }
}
log(`支线完成 ${4 - missing.length}/4，缺：${missing.join(',') || '无'}`)

phase('bull/bear 快速版')
const bb = await agent(
  `${base} 读 ${R}/ 下全部支线文件与 ${W}/drafts/${D}-background.md，按 .agents/skills/dalton-bullbear/SKILL.md 的快速版方法写成文字版到 ${R}/bullbear-quick.md（可补充检索内部库与 AlphaEngine）。缺失支线：${
    missing.join(',') || '无'
  }。`,
  { agentType: 'general-purpose', label: 'bull/bear 快速版', phase: 'bull/bear 快速版', schema: OUT }
)

phase('12 问初筛')
const draft = await agent(
  `${base} 按 .agents/skills/dalton-initial-coverage/references/screen-12q.md 和 .agents/skills/dalton-initial-coverage/templates/initial-coverage.md，综合 ${R}/ 全部文件，写 ${W}/drafts/${D}-initial-coverage.md，并返回成稿文件的 sha256。缺失支线对应的问题写“缺口：需要什么、已尝试路径”。`,
  { agentType: 'general-purpose', label: '初筛成稿', phase: '12 问初筛', schema: OUT }
)

phase('知识层草案')
const kp =
  draft === null
    ? null
    : await agent(
        `${base} 从 ${W}/drafts/${D}-initial-coverage.md 抽取知识层草案，按 PK.2 的 JSON Schema（.agents/skills/dalton-knowledge/SKILL.md 有字段说明）写 ${R}/knowledge-proposal.json，结构为 {doc: [ops]}，可直接交给 knowledge_update（company、thesis 支柱、debate、drivers、management 初记、agenda）。只抽取，不新增判断，也不直接改 ${W}/data/。`,
        { agentType: 'general-purpose', label: '知识层草案', phase: '知识层草案', schema: OUT }
      )

phase('审稿')
const review =
  draft === null
    ? null
    : await agent(
        `审阅 ${W}/drafts/${D}-initial-coverage.md（支线材料在 ${R}/，只读不改文件）。这是买方初次覆盖，重点看：Q1/Q2/Q3/Q4/Q5/Q7/Q8/Q12 是否有判断；“预期差”是否只是复述共识；壁垒“形成还是被破坏”是否有证据及可证伪指标；估值分解是否算对（两项之和等于股价对数变化）。`,
        { agentType: 'dalton-reviewer', label: '审稿', phase: '审稿' }
      )

const fix =
  review === null
    ? null
    : await agent(
        `先核对 ${W}/drafts/${D}-initial-coverage.md 的 sha256 是否与审稿前（${
          draft && draft.sha256 ? draft.sha256 : '成稿记录值'
        }）一致；按审稿意见修订 ${W}/drafts/${D}-initial-coverage.md，P0 全改，不新增未核实数字；同步修订 ${R}/knowledge-proposal.json。审稿意见：\n${review}`,
        { agentType: 'general-purpose', label: '修订', phase: '审稿', schema: OUT }
      )

phase('自动跟踪')
const tracking =
  draft === null
    ? { ok: false, skipped: true, reason: '初筛成稿未完成' }
    : {
        action: 'enable',
        source: 'coverage_finish',
        tool: 'dalton_coverage_tracking',
        call: 'dalton_coverage_tracking(action="enable", source="coverage_finish")',
        respect_user_pause: true,
      }
if (draft !== null) {
  log(
    `初筛完成，收尾需调用 dalton_coverage_tracking(action="enable", source="coverage_finish") 开启 ${T} 自动跟踪（若 paused_by_user 则保持关闭）`
  )
}

return {
  ok: draft !== null,
  draft: `${W}/drafts/${D}-initial-coverage.md`,
  knowledge_proposal: kp && `${R}/knowledge-proposal.json`,
  missing,
  review_done: review !== null,
  fix_summary: fix && fix.summary,
  tracking,
}
