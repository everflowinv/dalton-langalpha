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
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    gaps: { type: 'array', items: { type: 'string' } },
  },
  required: ['files', 'summary'],
}

const FILES_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    sha256: { type: 'string', minLength: 64 },
    gaps: { type: 'array', items: { type: 'string' } },
  },
  required: ['files'],
}

const DRAFT_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    warns: { type: 'number' },
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
const FILES_DRAFT = { ...FILES_SCHEMA, required: ['files', 'sha256'] }
const FIXED_SCHEMA = {
  ...DRAFT_SCHEMA,
  properties: { ...DRAFT_SCHEMA.properties, sha256: { type: 'string', minLength: 64 } },
  required: ['files', 'summary', 'sha256'],
}
const shaOf = (r) =>
  r && typeof r === 'object' && typeof r.sha256 === 'string' && /^[0-9a-f]{64}$/.test(r.sha256)
    ? r.sha256
    : null

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
        { agentType: 'general-purpose', label: `${T} 合并`, phase: '合并与审稿', schema: FILES_DRAFT }
      )

const mergedSha = merged === null ? null : shaOf(merged)
const mergedOk = merged !== null && mergedSha !== null
const review =
  !mergedOk
    ? null
    : await agent(
        `审阅 ${W}/drafts/${D}-bullbear.md（被审草稿 sha256：${
          shaOf(merged) || '未知'
        }；支线材料在 ${runDir}/，只读不改任何文件）。重点：每个 Debate 的“我的判断”是否有明确立场、改判条件是否可观察；§2 是否把价格走势当共识；§4 修正分层是否能回指一致预期快照。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
        { agentType: 'dalton-reviewer', label: `${T} 审稿`, phase: '合并与审稿', schema: VERDICT }
      )

const incomplete = []
if (review && verdictOf(review) === 'block') {
  incomplete.push(`审稿 block：${review.summary || '存在不可修订级问题'}`)
  return {
    ok: false,
    blocked: review.summary || '审稿 block',
    draft: `${W}/drafts/${D}-bullbear.md`,
    failed: [prep, ret, main, merged, review].map((x) => x === null),
    incomplete,
  }
}

phase('修订')
// revise → 修订 → 复审，最多 2 轮；每个失败出口都写进 incomplete 并停下
let fixed = null
let finalReview = review
let rounds = 0
let currentSha = mergedSha
let reviewPassed = review !== null && verdictOf(review) === 'pass'
while (finalReview && verdictOf(finalReview) === 'revise' && rounds < 2) {
  rounds += 1
  fixed = await agent(
    `先核对 ${W}/drafts/${D}-bullbear.md 的 sha256 是否仍为 ${currentSha || '当前记录值'}（不一致说明审稿后被改动，先标注再修订）。然后按审稿意见做第 ${rounds} 轮修订 ${W}/drafts/${D}-bullbear.md：P0 全改，P1 能改就改，不新增未核实数字；修完再跑 python ${S}/scripts/bb_check.py ${W}/drafts/${D}-bullbear.md 确认 0 WARN。审稿意见如下：\n${JSON.stringify(
      finalReview
    )}\n返回改动清单与剩余 P1。`,
    { agentType: 'general-purpose', label: `${T} 修订 ${rounds}`, phase: '修订', schema: FIXED_SCHEMA }
  )
  const fixedSha = fixed === null ? null : shaOf(fixed)
  if (fixedSha === null) {
    incomplete.push(`第 ${rounds} 轮修订失败（修订结果或其 sha256 缺失/非法），未复审`)
    break
  }
  currentSha = fixedSha
  const reReview = await agent(
    `复审（第 ${rounds} 轮修订后）：审阅 ${W}/drafts/${D}-bullbear.md（被审草稿 sha256：${fixedSha}；支线材料在 ${runDir}/，只读不改任何文件）。重点核对上一轮 must_fix 是否全部解决、bb_check 是否仍 0 WARN。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。上一轮审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'dalton-reviewer', label: `${T} 复审 ${rounds}`, phase: '修订', schema: VERDICT }
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

if (prep === null) incomplete.push('准备阶段失败')
if (ret === null && prep !== null) incomplete.push('股东回报支线失败')
if (main === null) incomplete.push('主稿失败')
if (!mergedOk && main !== null) incomplete.push('合并失败（草稿或其 sha256 缺失/非法）')
if (review === null && mergedOk) incomplete.push('审稿失败')
if (!reviewPassed && !incomplete.length) incomplete.push('审稿未最终通过（无 pass 记录）')

phase('产物核验')
const deliverables = [
  `${runDir}/anchors.json`,
  `${runDir}/fincheck.md`,
  `${runDir}/consensus.md`,
  `${runDir}/price.md`,
  `${runDir}/return.md`,
  `${runDir}/return_appendix.md`,
  `${runDir}/return_summary.md`,
  `${runDir}/main.md`,
  `${W}/drafts/${D}-bullbear.md`,
]
const check =
  !mergedOk
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
if (check === null && mergedOk) incomplete.push('产物核验子任务失败')

return {
  ok: mergedOk && reviewPassed && incomplete.length === 0,
  draft: `${W}/drafts/${D}-bullbear.md`,
  review_verdict: finalReview ? verdictOf(finalReview) : null,
  review_summary: finalReview && finalReview.summary,
  revise_rounds: rounds,
  fixed: fixed && fixed.summary,
  failed: [prep, ret, main, merged, review].map((x) => x === null),
  incomplete,
}
