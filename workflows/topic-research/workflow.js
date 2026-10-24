export const meta = {
  name: 'topic-research',
  description: 'Dalton 专题研究：规划 → 一手 → 分簇取证∥内部记忆∥专家 → 交叉验证与盲区 → 写作 → 数字核验 → 独立审稿 → 修订',
}

const { topic, slug, horizon, workspace_dir: W, as_of: D } = args
const related = args.related_tickers || []
const dateTag = D || 'latest'
const topicSlug = slug || 'topic'
const R = `${W}/research/${dateTag}-topic-${topicSlug}`
const S = '.agents/skills/dalton-topic-research'
const base = `先读 .agents/skills/dalton-conventions/SKILL.md 与 ${S}/SKILL.md、${S}/references/principles.md；专题：「${topic}」，时间范围：${
  horizon || '优先近 30 天（6 个月以上标日期，12 个月以上仅作背景）'
}，相关公司：${related.join(', ') || '见规划'}。基金 wiki 只读，任何阶段不回写 wiki。`

// Keep schema nesting depth <= 4 (well within schema_max_depth=5)
const PLAN_SCHEMA = {
  type: 'object',
  properties: {
    file: { type: 'string' },
    clusters: { type: 'array', items: { type: 'object' } },
    questions: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string', minLength: 1 },
  },
  required: ['file', 'clusters', 'questions'],
}

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

phase('规划')
const plan = await agent(
  `${base} 阶段 0 规划：参考 ${S}/references/clusters-investment.md 设计 3–6 个检索簇（每簇含 id, name, queries（5–8 条中英文检索词）、预期信息类型、关键来源、盲区），锁定报告框架与 5–8 个核心问题（${
    (args.core_questions || []).join('；') || '按主题拆解'
  }）、预期矛盾与数字核验清单，写 ${R}/plan.md。返回 file、clusters（最多 6 个对象，每项含 id, name, queries）、questions、summary。`,
  { agentType: 'general-purpose', label: '规划', phase: '规划', schema: PLAN_SCHEMA }
)
if (plan === null) {
  return { ok: false, reason: '专题规划阶段失败', incomplete: ['专题规划阶段失败'] }
}

phase('一手材料')
const primary = await agent(
  `${base} 阶段 0.5 一手材料：规划见 ${R}/plan.md。涉及上市公司（${
    related.join(', ') || '按规划'
  }）时读最近定期报告与 8-K 附件、1–2 次电话会、近 6 个月卖方报告；行业/宏观题读 AlphaEngine 行业检索与内部库，写 ${R}/primary.md。`,
  { agentType: 'dalton-evidence', label: '一手材料', phase: '一手材料', schema: OUT }
)

phase('分簇取证')
const clusters = (plan.clusters || []).slice(0, 6)
const evTasks = [
  ...clusters.map((c, idx) => () => {
    const cid = c.id || `c${idx + 1}`
    const cname = c.name || `簇 ${idx + 1}`
    const qs = Array.isArray(c.queries) ? c.queries.join(' / ') : ''
    return agent(
      `${base} 阶段 2 分簇取证（${cid}：${cname}）：检索词 [${qs}]。跑完全部检索词，精读 3–5 篇全文；输出 Facts / Direct Quotes / Timeline / Contradictions / 全文摘录 / Sources 到 ${R}/cluster-${cid}.md。`,
      { agentType: 'dalton-evidence', label: `取证 ${cname}`, phase: '分簇取证', schema: OUT }
    )
  }),
  () =>
    agent(
      `${base} 阶段 2 内部记忆检索：检索 researchdata 的 sales-notes、tmtb、funda-ai、company-wiki（只读），英文与中文结合，写 ${R}/internal-memory.md。`,
      { agentType: 'dalton-evidence', label: '内部记忆', phase: '分簇取证', schema: OUT }
    ),
  () =>
    agent(
      `${base} 阶段 2 专家纪要检索：用英文检索 Guidepoint 1–2 轮，标专家身份不写姓名，原话引用 ≤20 个英文词并带出处，写 ${R}/guidepoint.md。`,
      { agentType: 'dalton-evidence', label: '专家纪要', phase: '分簇取证', schema: OUT }
    ),
]
const evResults = await parallel(evTasks)

phase('交叉验证与盲区')
const cross = await agent(
  `${base} 阶段 3 交叉验证 + 阶段 3.5 盲区检查：读 ${R}/ 下全部取证文件（primary.md、cluster-*.md、internal-memory.md、guidepoint.md），跨簇互证升级置信度（【高】≥3 独立源、【中】2 源、【低】单源）；矛盾保留各方原话（【冲突】），不折中合并；检查盲区并写 ${R}/cross-check.md，结尾写“盲区检查完成”。若仍有必须补查的盲区，放入返回 JSON 的 gaps 数组（最多 2 项）。`,
  { agentType: 'general-purpose', label: '交叉验证与盲区', phase: '交叉验证与盲区', schema: OUT }
)

if (cross && Array.isArray(cross.gaps) && cross.gaps.length > 0) {
  const suppGaps = cross.gaps.slice(0, 2)
  await parallel(
    suppGaps.map((g, idx) => () =>
      agent(
        `${base} 阶段 3.5 盲区补查 #${idx + 1}：针对盲区「${g}」补充检索一手材料、内部库或 AlphaEngine，写 ${R}/supplement-${
          idx + 1
        }.md。`,
        { agentType: 'dalton-evidence', label: `盲区补查 ${idx + 1}`, phase: '交叉验证与盲区', schema: OUT }
      )
    )
  )
}

phase('写作与数字核验')
const draftPath = `${W}/drafts/${dateTag}-topic-${topicSlug}.md`
const draft = await agent(
  `${base} 阶段 4 写作：按 ${S}/templates/topic.md 综合 ${R}/ 下全部材料写专题报告到 ${draftPath}（含逐条置信度标签、“对覆盖公司的含义”表、情景推演、信息缺口与局限、数字核验表占位、Sources），跑 python ${S}/scripts/validate_topic.py --input ${draftPath} --json 到通过，返回成稿路径与 sha256。`,
  { agentType: 'general-purpose', label: '专题写作', phase: '写作与数字核验', schema: DRAFT_OUT }
)

const audit =
  draft === null
    ? null
    : await agent(
        `${base} 阶段 5 数字核验：对 ${draftPath} 里每个关键数字核对 SEC filing（dalton_sec）与行情/一致预期接口（价格、市值、PE、净现金、营收、净利、分红回购、capex），输出 PASS/FAIL 核验表到 ${R}/number-audit.md。`,
        { agentType: 'dalton-evidence', label: '数字核验', phase: '写作与数字核验', schema: OUT }
      )

phase('独立审稿与修订')
const draftSha = draft === null ? null : shaOf(draft)
const draftOk = draft !== null && draftSha !== null
const review =
  !draftOk
    ? null
    : await agent(
        `审阅 ${draftPath}（被审草稿 sha256：${
          draftSha || '未知'
        }）及 ${R}/number-audit.md、${R}/cross-check.md（只读不改文件）。检查：无据断言、逻辑漏洞、盲点、来源偏差、矛盾是否保留原话、低置信度占比是否异常（<15% 提示注水）、对覆盖公司的驱动变量传导是否具体。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block，pass=可发布、block=不得发布；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
        { agentType: 'dalton-reviewer', label: '独立审稿', phase: '独立审稿与修订', schema: VERDICT }
      )

const incomplete = []
if (review && verdictOf(review) === 'block') {
  incomplete.push(`审稿 block：${review.summary || '存在不可修订级问题'}`)
  return {
    ok: false,
    blocked: review.summary || '审稿 block',
    draft: draftPath,
    number_audit: audit && `${R}/number-audit.md`,
    incomplete,
  }
}

// revise → 修订 → 复审，最多 2 轮；每个失败出口都写进 incomplete 并停下。
// 知识层草案与是否修订无关：pass 路径也要生成。
let fix = null
let finalReview = review
let rounds = 0
let currentDraftSha = draftSha
let reviewPassed = review !== null && verdictOf(review) === 'pass'
while (finalReview && verdictOf(finalReview) === 'revise' && rounds < 2) {
  rounds += 1
  fix = await agent(
    `${base} 阶段 7 修订（第 ${rounds} 轮）：先核对 ${draftPath} 的 sha256 是否与审稿前（${
      currentDraftSha || '当前记录值'
    }）一致；把 ${R}/number-audit.md 的核验表并入报告（FAIL 项修成正值并标【更正】），按审稿意见修复全部 P0，跑 python ${S}/scripts/validate_topic.py --input ${draftPath} --json 到通过。审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'general-purpose', label: `修订 ${rounds}`, phase: '独立审稿与修订', schema: DRAFT_OUT }
  )
  const fixedSha = fix === null ? null : shaOf(fix)
  if (fixedSha === null) {
    incomplete.push(`第 ${rounds} 轮修订失败（修订结果或其 sha256 缺失/非法），未复审`)
    break
  }
  currentDraftSha = fixedSha
  const reReview = await agent(
    `复审（第 ${rounds} 轮修订后）：审阅 ${draftPath}（被审草稿 sha256：${
      fixedSha
    }）及 ${R}/number-audit.md、${R}/cross-check.md（只读不改文件）。重点核对上一轮 must_fix 是否全部解决、validate_topic 是否仍通过。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。上一轮审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'dalton-reviewer', label: `复审 ${rounds}`, phase: '独立审稿与修订', schema: VERDICT }
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

// 知识提案只看最终审稿结论，与中间有没有修订完全无关；修订 prompt 不写这个文件。
let kp = null
if (reviewPassed) {
  kp = await agent(
    `${base} 阶段 7 知识层草案：按 PK.2 的 JSON Schema 将对相关覆盖公司（${
      related.join(', ') || '报告涉及的覆盖公司'
    }）的知识层修改建议（drivers、debate、lessons、agenda）写到 ${R}/knowledge-revision.json（不回写基金 wiki，不直接改 data/）。`,
    { agentType: 'general-purpose', label: '知识层草案', phase: '独立审稿与修订', schema: OUT }
  )
}

if (primary === null) incomplete.push('一手材料失败')
const failedClusters = clusters.filter((_, i) => evResults[i] === null).length
if (failedClusters) incomplete.push(`分簇取证失败 ${failedClusters} 个`)
if (evResults[clusters.length] === null) incomplete.push('内部记忆检索失败')
if (evResults[clusters.length + 1] === null) incomplete.push('专家纪要检索失败')
if (cross === null) incomplete.push('交叉验证与盲区失败')
if (!draftOk) incomplete.push('专题写作失败（草稿或其 sha256 缺失/非法）')
if (audit === null && draftOk) incomplete.push('数字核验失败')
if (review === null && draftOk) incomplete.push('独立审稿失败')
if (!reviewPassed && !incomplete.length) incomplete.push('审稿未最终通过（无 pass 记录）')
if (reviewPassed && kp === null) incomplete.push('知识层草案失败')

phase('产物核验')
const deliverables = [
  `${R}/plan.md`,
  `${R}/primary.md`,
  `${R}/internal-memory.md`,
  `${R}/guidepoint.md`,
  `${R}/cross-check.md`,
  draftPath,
  `${R}/number-audit.md`,
  `${R}/knowledge-revision.json`,
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

return {
  ok: draftOk && reviewPassed && incomplete.length === 0,
  draft: draftPath,
  number_audit: audit && `${R}/number-audit.md`,
  review_done: finalReview !== null,
  review_verdict: finalReview && verdictOf(finalReview),
  revise_rounds: rounds,
  knowledge_revision: kp && `${R}/knowledge-revision.json`,
  incomplete,
}
