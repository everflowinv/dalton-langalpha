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
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    sha256: { type: 'string', minLength: 64 },
    gaps: { type: 'array', items: { type: 'string' } },
  },
  required: ['files', 'summary'],
}
const VERDICT = {
  type: 'object',
  properties: {
    verdict: { type: 'string', enum: ['pass', 'revise', 'block'] },
    summary: { type: 'string', minLength: 1 },
    must_fix: { type: 'array', items: { type: 'string', minLength: 1 } },
    draft_sha256: { type: 'string', minLength: 64 },
  },
  required: ['verdict', 'summary', 'draft_sha256'],
}
const HASH = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
  },
  required: ['files'],
}
const verdictOf = (r) => (r && typeof r === 'object' ? r.verdict : null)
const DRAFT_OUT = { ...OUT, required: ['files', 'summary', 'sha256'] }
const shaOf = (r) =>
  r && typeof r === 'object' && typeof r.sha256 === 'string' && /^[0-9a-f]{64}$/.test(r.sha256)
    ? r.sha256
    : null

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
  return { ok: false, reason: '四条支线全部失败', missing, incomplete: ['四条支线全部失败，无产物可核验'] }
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
  { agentType: 'general-purpose', label: '初筛成稿', phase: '12 问初筛', schema: DRAFT_OUT }
)

const draftSha = draft === null ? null : shaOf(draft)
const draftOk = draft !== null && draftSha !== null

phase('知识层草案')
const kp =
  !draftOk
    ? null
    : await agent(
        `${base} 从 ${W}/drafts/${D}-initial-coverage.md 抽取知识层草案，按 PK.2 的 JSON Schema（.agents/skills/dalton-knowledge/SKILL.md 有字段说明）写 ${R}/knowledge-proposal.json，结构为 {doc: [ops]}，可直接交给 knowledge_update（company、thesis 支柱、debate、drivers、management 初记、agenda）。只抽取，不新增判断，也不直接改 ${W}/data/。`,
        { agentType: 'general-purpose', label: '知识层草案', phase: '知识层草案', schema: OUT }
      )

phase('审稿')
const review =
  !draftOk
    ? null
    : await agent(
        `审阅 ${W}/drafts/${D}-initial-coverage.md（被审草稿 sha256：${
          draftSha || '未知'
        }；支线材料在 ${R}/，只读不改文件）。这是买方初次覆盖，重点看：Q1/Q2/Q3/Q4/Q5/Q7/Q8/Q12 是否有判断；“预期差”是否只是复述共识；壁垒“形成还是被破坏”是否有证据及可证伪指标；估值分解是否算对（两项之和等于股价对数变化）。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
        { agentType: 'dalton-reviewer', label: '审稿', phase: '审稿', schema: VERDICT }
      )

const incomplete = []
if (review && verdictOf(review) === 'block') {
  incomplete.push(`审稿 block：${review.summary || '存在不可修订级问题'}`)
  return {
    ok: false,
    blocked: review.summary || '审稿 block',
    draft: `${W}/drafts/${D}-initial-coverage.md`,
    missing,
    incomplete,
  }
}

// revise → 修订 → 复审，最多 2 轮；每个失败出口都写进 incomplete 并停下
let fix = null
let finalReview = review
let rounds = 0
let currentDraftSha = draftSha
let reviewPassed = review !== null && verdictOf(review) === 'pass'
while (finalReview && verdictOf(finalReview) === 'revise' && rounds < 2) {
  rounds += 1
  fix = await agent(
    `先核对 ${W}/drafts/${D}-initial-coverage.md 的 sha256 是否与审稿前（${
      currentDraftSha || '当前记录值'
    }）一致；按审稿意见做第 ${rounds} 轮修订（P0 全改，不新增未核实数字；同步修订 ${R}/knowledge-proposal.json；改完返回成稿 sha256）。审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'general-purpose', label: `修订 ${rounds}`, phase: '审稿', schema: DRAFT_OUT }
  )
  const fixedSha = fix === null ? null : shaOf(fix)
  if (fixedSha === null) {
    incomplete.push(`第 ${rounds} 轮修订失败（修订结果或其 sha256 缺失/非法），未复审`)
    break
  }
  currentDraftSha = fixedSha
  const reReview = await agent(
    `复审（第 ${rounds} 轮修订后）：审阅 ${W}/drafts/${D}-initial-coverage.md（被审草稿 sha256：${fixedSha}；支线材料在 ${R}/，只读不改文件）。重点核对上一轮 must_fix 是否全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。上一轮审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'dalton-reviewer', label: `复审 ${rounds}`, phase: '审稿', schema: VERDICT }
  )
  if (reReview === null) {
    incomplete.push(`第 ${rounds} 轮复审失败`)
    break
  }
  if (verdictOf(reReview) === 'block') {
    incomplete.push(`复审 block：${reReview.summary || '存在不可修订级问题'}`)
    break
  }
  finalReview = reReview
}
reviewPassed = finalReview !== null && verdictOf(finalReview) === 'pass'

if (missing.length) incomplete.push(`支线失败：${missing.join('、')}`)
if (!draftOk) incomplete.push('初筛成稿失败（草稿或其 sha256 缺失/非法）')
if (bb === null) incomplete.push('bull/bear 快速版失败')
if (kp === null) incomplete.push('知识层草案失败')
if (review === null && draftOk) incomplete.push('审稿失败')
if (!reviewPassed && !incomplete.length) incomplete.push('审稿未最终通过（无 pass 记录）')

phase('产物核验')
const deliverables = [
  `${W}/drafts/${D}-background.md`,
  `${R}/industry.md`,
  `${R}/lenses.json`,
  `${W}/drafts/${D}-lenses.md`,
  `${R}/valuation.md`,
  `${R}/comps.xlsx`,
  `${R}/bullbear-quick.md`,
  `${W}/drafts/${D}-initial-coverage.md`,
  `${R}/knowledge-proposal.json`,
]
const check =
  !draftOk
    ? null
    : await agent(
        `只读核对以下文件是否存在并计算 sha256（用 bash 的 test -e 与 sha256sum，不写任何文件）：\n${deliverables
          .map((p) => '- ' + p)
          .join('\n')}\n返回 files 字符串数组：存在的文件写 “<路径>|<sha256>”，不存在的只写 “<路径>”（不带竖线）。`,
        { agentType: 'dalton-evidence', label: '产物核验', phase: '产物核验', schema: HASH }
      )
const SHA_RE = /^[0-9a-f]{64}$/
const expected = new Set(deliverables)
const reported = new Set()
for (const entry of check && Array.isArray(check.files) ? check.files : []) {
  const bar = entry.indexOf('|')
  const path = bar > 0 ? entry.slice(0, bar) : ''
  const sha = bar > 0 ? entry.slice(bar + 1) : ''
  if (!path || !SHA_RE.test(sha) || !expected.has(path)) {
    incomplete.push(`产物核验返回无效条目：${entry}`)
    continue
  }
  reported.add(path)
}
for (const path of deliverables) {
  if (!reported.has(path)) incomplete.push(`产物缺失或未报哈希：${path}`)
}
if (check === null && draftOk) incomplete.push('产物核验子任务失败')

phase('自动跟踪')
// The tracking hint follows the run's own completion verdict exactly: an
// unfinished run must not tell the analyst to switch tracking on.
const runOk = draftOk && reviewPassed && incomplete.length === 0
const tracking = runOk
  ? {
      action: 'enable',
      source: 'coverage_finish',
      tool: 'dalton_coverage_tracking',
      call: 'dalton_coverage_tracking(action="enable", source="coverage_finish")',
      respect_user_pause: true,
    }
  : {
      ok: false,
      skipped: true,
      reason: !draftOk ? '初筛成稿未完成' : !reviewPassed ? '审稿未最终通过' : `流程未完成：${incomplete[0]}`,
    }
if (runOk) {
  log(
    `初筛完成，收尾需调用 dalton_coverage_tracking(action="enable", source="coverage_finish") 开启 ${T} 自动跟踪（若 paused_by_user 则保持关闭）`
  )
} else {
  log(`不开启自动跟踪：${tracking.reason}`)
}

return {
  ok: runOk,
  draft: `${W}/drafts/${D}-initial-coverage.md`,
  knowledge_proposal: kp && `${R}/knowledge-proposal.json`,
  missing,
  review_done: finalReview !== null,
  review_verdict: finalReview && verdictOf(finalReview),
  revise_rounds: rounds,
  fix_summary: fix && fix.summary,
  tracking,
  incomplete,
}
