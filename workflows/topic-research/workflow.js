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

// B1：支线产物缺失判定（各 workflow 同一实现；workflow 之间没有共用 js 模块的
// 机制，改动需同步全部 workflow.js）。子任务返回 null、files/file 为空、或（经
// 只读核验子任务确认）声明的产物文件不存在/为空（0 字节或只有空白），都算缺失；
// workflow 自己碰不到文件系统，最后一项由支线核验子任务在盘上确认。
const branchArtifacts = (v) => {
  if (v === null || typeof v !== 'object') return []
  const out = []
  if (typeof v.file === 'string' && v.file.trim()) out.push(v.file.split('|', 1)[0].trim())
  for (const f of Array.isArray(v.files) ? v.files : []) {
    if (typeof f === 'string' && f.trim()) out.push(f.split('|', 1)[0].trim())
  }
  return out.filter(Boolean)
}
const PROBE_OUT = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
  },
  required: ['files'],
}
// 返回每个路径的 "<路径>|ok" / "<路径>|missing"；结果不完整（漏路径、格式不对）
// 一律算核验失败（fail-closed），返回 null。paths 的每一项可以是路径或
// [路径, 最小字节数]（契约 min_bytes；缺省 1，即非空白即可）。
const probeBranchFiles = async (paths) => {
  const entries = paths.map((p) => (Array.isArray(p) ? p : [p, 1]))
  const r = await agent(
    `只读核验以下支线产物文件是否存在、非空白且达到字节数下限（不写任何文件）。逐个执行 bash：f='<路径>'；n=<字节数下限>；test -e "$f" && [ "$(wc -c < "$f")" -ge "$n" ] && [ -n "$(tr -d '[:space:]' < "$f")" ] && echo "$f|ok" || echo "$f|missing"。返回 files 数组：每项为上述一行的原样输出（<路径>|ok 或 <路径>|missing）：\n${entries
      .map(([p, min]) => `- ${p}（至少 ${min} 字节）`)
      .join('\n')}`,
    { agentType: 'dalton-evidence', label: '支线核验', phase: '分簇取证', schema: PROBE_OUT, checkFiles: false }
  )
  if (r === null || typeof r !== 'object' || !Array.isArray(r.files)) return null
  const answered = new Set()
  for (const entry of r.files) {
    if (typeof entry !== 'string') return null
    const bar = entry.lastIndexOf('|')
    const path = bar > 0 ? entry.slice(0, bar) : ''
    const verdict = bar > 0 ? entry.slice(bar + 1) : ''
    if (!path || (verdict !== 'ok' && verdict !== 'missing')) return null
    answered.add(path)
  }
  if (entries.some(([p]) => !answered.has(p))) return null
  return r.files
}
// B1 共用出口：缺口凑不满契约底线（含全部缺失）与核验失败都停在交叉验证之前，
// 文案与审稿 block 一致，主线程补数后用原参数 resume 重跑（已完派发按 journal
// 复用）。
const RESUME_HINT = '用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，缺失支线重新派发（其提示词已写明“若目标文件已存在，先读并核对，缺什么补什么”）'

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
  }）时读最近定期报告与 8-K 附件、1–2 次电话会、近 6 个月卖方报告；行业/宏观题读 AlphaEngine 行业检索与内部库，写 ${R}/primary.md。若目标文件已存在，先读并核对，缺什么补什么，不必推倒重写。`,
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

// B1：contract 要求 primary.md（≥100 字节）、cluster-* ≥3 个（各 ≥100 字节）、
// internal-memory.md 与 guidepoint.md（各 ≥50 字节）必交。取证子任务返回
// null/files 为空、或核验发现在盘缺失/过小，都算缺失；凑不满底线（含全部缺失）
// 停在交叉验证之前，出口与审稿 block 同一段文案。个别簇缺失但底线仍够时不
// block：缺失簇写进 incomplete、传给写作提示词并在返回值里列出。
const clusterFiles = clusters.map((c, idx) => `${R}/cluster-${c.id || `c${idx + 1}`}.md`)
const clusterNames = clusters.map((c, idx) => c.name || `簇 ${idx + 1}`)
const internalFile = `${R}/internal-memory.md`
const guidepointFile = `${R}/guidepoint.md`
const primaryFile = `${R}/primary.md`
const clusterOnDisk = clusters.map((_, i) => branchArtifacts(evResults[i]).length > 0)
let internalOnDisk = branchArtifacts(evResults[clusters.length]).length > 0
let guidepointOnDisk = branchArtifacts(evResults[clusters.length + 1]).length > 0
let primaryOnDisk = branchArtifacts(primary).length > 0
// 缺失簇在核验前先按子任务回报算一遍（核验失败的出口要用），核验后再按在盘
// 结果重算；此后所有出口（取证缺失 block、审稿 block、最终返回）都带上。
let missingClusters = clusters
  .map((_, i) => (clusterOnDisk[i] ? null : clusterNames[i]))
  .filter(Boolean)
let missingClusterIds = clusters
  .map((c, i) => (clusterOnDisk[i] ? null : c.id || `c${i + 1}`))
  .filter(Boolean)
const toProbe = [
  ...(primaryOnDisk ? [[primaryFile, 100]] : []),
  ...clusterFiles.filter((_, i) => clusterOnDisk[i]).map((p) => [p, 100]),
  ...(internalOnDisk ? [[internalFile, 50]] : []),
  ...(guidepointOnDisk ? [[guidepointFile, 50]] : []),
]
if (toProbe.length) {
  const probe = await probeBranchFiles(toProbe)
  if (probe === null) {
    return {
      ok: false,
      blocked: '支线核验子任务失败，无法确认取证产物在盘',
      incomplete: [
        `支线核验子任务失败（无法确认产物文件存在且非空；按子任务回报缺失的簇：${
          missingClusters.join('、') || '无'
        }），已停在交叉验证之前；请重试或补数后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑`,
      ],
      missing_clusters: missingClusterIds,
      resume_hint: '用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，失败支线重新派发',
    }
  }
  for (const entry of probe) {
    if (!entry.endsWith('|missing')) continue
    const path = entry.slice(0, -'|missing'.length)
    const idx = clusterFiles.indexOf(path)
    if (idx >= 0) clusterOnDisk[idx] = false
    else if (path === internalFile) internalOnDisk = false
    else if (path === guidepointFile) guidepointOnDisk = false
    else if (path === primaryFile) primaryOnDisk = false
  }
  missingClusters = clusters
    .map((_, i) => (clusterOnDisk[i] ? null : clusterNames[i]))
    .filter(Boolean)
  missingClusterIds = clusters
    .map((c, i) => (clusterOnDisk[i] ? null : c.id || `c${i + 1}`))
    .filter(Boolean)
}
// 底线之外的个别簇缺失：不 block，随报告与返回值交代。
const presentClusters = clusterOnDisk.filter(Boolean).length
const topicMissing = []
if (!primaryOnDisk) topicMissing.push('一手材料')
if (presentClusters < 3)
  topicMissing.push(`cluster-*.md 完整产物不足 3 个（现 ${presentClusters} 个）`)
if (!internalOnDisk) topicMissing.push('内部记忆检索')
if (!guidepointOnDisk) topicMissing.push('专家纪要检索')
if (topicMissing.length) {
  return {
    ok: false,
    blocked: `取证支线缺失：${topicMissing.join('、')}`,
    incomplete: [
      `取证支线缺失：${topicMissing.join('、')}，已停在交叉验证之前；补齐缺失产物后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用`,
    ],
    missing_clusters: missingClusterIds,
    resume_hint: RESUME_HINT,
  }
}

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
  `${base} 阶段 4 写作：按 ${S}/templates/topic.md 综合 ${R}/ 下全部材料写专题报告到 ${draftPath}（含逐条置信度标签、“对覆盖公司的含义”表、情景推演、信息缺口与局限、数字核验表占位、Sources）${
    missingClusters.length ? `。缺失簇：${missingClusters.join('、')}，对应内容在“信息缺口与局限”写明缺什么与已尝试路径` : ''
  }。跑 python ${S}/scripts/validate_topic.py --input ${draftPath} --json 到通过，返回成稿路径与 sha256。`,
  { agentType: 'general-purpose', label: '专题写作', phase: '写作与数字核验', schema: DRAFT_OUT }
)

// 第 1 轮语言审查：专题报告写完之后、数字核验与第一次独立审稿之前（改稿会换哈希，必须先改再送审）。
const draftSha0 = draft === null ? null : shaOf(draft)
phase('语言审查')
const lang1 =
  draftSha0 === null
    ? null
    : await agent(
        `对 ${draftPath} 做第 1 轮语言审查：调用工具 check_language(draft_path="${draftPath}", round=1)（只标问题），读 ${draftPath}.language.md 清单，按清单逐条修改 ${draftPath}（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。改完跑 python ${S}/scripts/validate_topic.py --input ${draftPath} --json 确认仍通过，返回 ${draftPath} 的 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 1', phase: '语言审查', schema: DRAFT_OUT }
      )
const lang1Sha = lang1 === null ? null : shaOf(lang1)
const lang1Failed = draftSha0 !== null && lang1Sha === null

const audit =
  draft === null
    ? null
    : await agent(
        `${base} 阶段 5 数字核验：对 ${draftPath} 里每个关键数字核对 SEC filing（dalton_sec）与行情/一致预期接口（价格、市值、PE、净现金、营收、净利、分红回购、capex），输出 PASS/FAIL 核验表到 ${R}/number-audit.md。`,
        { agentType: 'dalton-evidence', label: '数字核验', phase: '写作与数字核验', schema: OUT }
      )

phase('独立审稿与修订')
const draftSha = lang1Sha || draftSha0
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
// 缺失簇信息在取证门就初始化（见 missingClusters/missingClusterIds），所有出口都带上。
if (missingClusters.length)
  incomplete.push(`分簇取证失败（${missingClusters.join('、')}），缺口已传给写作并在报告中交代`)
if (review && verdictOf(review) === 'block') {
  incomplete.push(`审稿 block：${review.summary || '存在不可修订级问题'}`)
  return {
    ok: false,
    blocked: review.summary || '审稿 block',
    draft: draftPath,
    number_audit: audit && `${R}/number-audit.md`,
    missing_clusters: missingClusterIds,
    incomplete,
    resume_hint:
      '修订报告后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发（取证支线等）会直接复用，成稿及之后的阶段重新派发',
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

// 笔记两轮：一次写完、不再改的取证/核验笔记同样审两轮——第 1 轮审完按清单改，
// 第 2 轮复核（改动过的文件再补一次 round=2 确认，绑定终态哈希）。
const notesReview =
  !draftOk || !reviewPassed
    ? null
    : await agent(
        `对本次运行产出、给人读的 .md 笔记逐一做两轮语言审查（规则见 .agents/skills/dalton-language/SKILL.md，只标问题）。每个文件依次：1) 调用工具 check_language(draft_path="<文件>", round=1)，读 <文件>.language.md 清单并按清单修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）；2) 调用 check_language(draft_path="<文件>", round=2) 复核；若第 1 轮后改过稿，改完再调用一次 round=2 确认（记录追加在同一 json）。\n文件清单：\n- ${R}/plan.md\n- ${R}/primary.md\n- ${R}/internal-memory.md\n- ${R}/guidepoint.md\n- ${R}/cross-check.md\n- ${R}/number-audit.md\n清单之外本次运行写的其他给人读的 .md 一并同法处理；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/ 下的文件、知识层渲染文件与 .audit/.annotated/.language 侧车不审。返回处理过的文件列表与摘要。`,
        { agentType: 'general-purpose', label: '笔记语言审查', phase: '独立审稿与修订', schema: OUT }
      )
if (draftOk && reviewPassed && notesReview === null) {
  incomplete.push('笔记语言审查失败（取证/核验笔记未完成两轮语言审查）')
}

// 第 2 轮语言审查：终稿内容定稿（审稿修订循环结束）之后、提交最后一次审稿之前。
// 改了稿就再跑一次 round=2 复核，使最后一条第 2 轮记录绑定终稿哈希；哈希有变
// 则终稿审稿重新绑定独立审稿哈希——语言修改不能让已有 pass 记录失效却无人重审。
const preLang2Sha = currentDraftSha
const lang2 =
  !draftOk || !reviewPassed
    ? null
    : await agent(
        `对 ${draftPath} 做第 2 轮语言审查：调用工具 check_language(draft_path="${draftPath}", round=2)，读 ${draftPath}.language.md 清单，按清单逐条完成终稿语言修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。若草稿有改动，再调用一次 check_language(draft_path="${draftPath}", round=2) 对终稿复核（记录追加在同一 json），并跑 python ${S}/scripts/validate_topic.py --input ${draftPath} --json 确认仍通过。返回 ${draftPath} 最终 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 2', phase: '独立审稿与修订', schema: DRAFT_OUT }
      )
const lang2Sha = lang2 === null ? null : shaOf(lang2)
if (draftOk && reviewPassed && lang2Sha === null) {
  incomplete.push('语言审查第 2 轮失败（结果或其 sha256 缺失/非法），未做终稿语言修改')
  reviewPassed = false
} else if (draftOk && reviewPassed && lang2Sha !== preLang2Sha) {
  currentDraftSha = lang2Sha
  const finalCheck = await agent(
    `终稿审稿（语言修改后）：审阅 ${draftPath}（被审草稿 sha256：${lang2Sha}）及 ${R}/number-audit.md、${R}/cross-check.md（只读不改文件）。重点：语言修改未动事实与数字，上一轮 must_fix 仍全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
    { agentType: 'dalton-reviewer', label: '终稿审稿', phase: '独立审稿与修订', schema: VERDICT }
  )
  if (finalCheck === null) {
    incomplete.push('终稿审稿失败')
    reviewPassed = false
  } else {
    finalReview = finalCheck
    reviewPassed = verdictOf(finalCheck) === 'pass'
  }
}

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

if (!primaryOnDisk) incomplete.push('一手材料失败')
if (!internalOnDisk) incomplete.push('内部记忆检索失败')
if (!guidepointOnDisk) incomplete.push('专家纪要检索失败')
if (cross === null) incomplete.push('交叉验证与盲区失败')
if (!draftOk) incomplete.push('专题写作失败（草稿或其 sha256 缺失/非法）')
if (lang1Failed) incomplete.push('语言审查第 1 轮失败（结果或其 sha256 缺失/非法），未按清单修改')
if (audit === null && draftOk) incomplete.push('数字核验失败')
if (review === null && draftOk) incomplete.push('独立审稿失败')
if (!reviewPassed && !incomplete.length) incomplete.push('审稿未最终通过（无 pass 记录）')
if (reviewPassed && kp === null) incomplete.push('知识层草案失败')

phase('产物核验')
const deliverables = [
  `${R}/plan.md`,
  `${R}/plan.md.language.json`,
  `${R}/primary.md`,
  `${R}/primary.md.language.json`,
  `${R}/internal-memory.md`,
  `${R}/internal-memory.md.language.json`,
  `${R}/guidepoint.md`,
  `${R}/guidepoint.md.language.json`,
  `${R}/cross-check.md`,
  `${R}/cross-check.md.language.json`,
  draftPath,
  `${draftPath}.language.json`,
  `${R}/number-audit.md`,
  `${R}/number-audit.md.language.json`,
  `${R}/knowledge-revision.json`,
]
const check =
  !draftOk
    ? null
    : await agent(
        `只读核对以下文件是否存在并计算 sha256（用 bash 的 test -e 与 sha256sum，不写任何文件）：\n${deliverables
          .map((p) => '- ' + p)
          .join('\n')}\n返回 files 字符串数组：存在的文件写 “<路径>|<sha256>”，不存在的只写 “<路径>”（不带竖线）。`,
        { agentType: 'dalton-evidence', label: '产物核验', phase: '产物核验', schema: HASH, checkFiles: false }
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
  language_done: draftOk && lang1Sha !== null && lang2Sha !== null,
  revise_rounds: rounds,
  knowledge_revision: kp && `${R}/knowledge-revision.json`,
  missing_clusters: missingClusterIds,
  incomplete,
}
