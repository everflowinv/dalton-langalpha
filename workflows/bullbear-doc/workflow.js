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
// A1 同类：SKILL 声明的 market（与可选的 revision_brief 文件路径）随 conv 进
// 每个子任务。
const pmAsks = []
if (args.pm_question) pmAsks.push(String(args.pm_question))
if (args.revision_brief) pmAsks.push(`本轮必须修正的事项见 ${args.revision_brief}（先读该文件）`)
const pmSection = pmAsks.length ? ` PM 的问题 / 本轮必须修正的事项：${pmAsks.join('；')}。` : ''
const conv = `先读 .agents/skills/dalton-conventions/SKILL.md 与 ${S}/SKILL.md、${S}/references/v42-structure.md；市场 ${args.market || 'US'}。${pmSection}`

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
    { agentType: 'dalton-evidence', label: '支线核验', phase: '股东回报支线', schema: PROBE_OUT, checkFiles: false }
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
// B1 共用出口：缺失（含全部缺失）与核验失败都停在主稿之前，文案与审稿 block
// 一致，主线程补数后用原参数 resume 重跑（已完派发按 journal 复用）。
const RESUME_HINT = '用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，缺失支线重新派发（其提示词已写明“若目标文件已存在，先读并核对，缺什么补什么”）'

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
输出 ${runDir}/return.md、${runDir}/return_appendix.md、${runDir}/return_summary.md。若目标文件已存在，先读并核对，缺什么补什么，不必推倒重写。`,
        { agentType: 'dalton-evidence', label: `${T} 股东回报`, phase: '股东回报支线', schema: FILES_SCHEMA }
      )

// B1：contract 把准备四件与股东回报三件都列为必交（min_bytes 见各项）。任一缺失
// （null、files 为空、或核验发现在盘缺失/过小，含全部缺失）都停在主稿之前，
// 出口与审稿 block 同一段文案。
const prepPaths = [
  [`${runDir}/anchors.json`, 50],
  [`${runDir}/fincheck.md`, 100],
  [`${runDir}/consensus.md`, 100],
  [`${runDir}/price.md`, 50],
]
const retPaths = [
  [`${runDir}/return.md`, 100],
  [`${runDir}/return_appendix.md`, 50],
  [`${runDir}/return_summary.md`, 50],
]
const gateBranches = [
  ['准备', prep, prepPaths],
  ['股东回报', ret, retPaths],
]
const gateMissing = gateBranches
  .filter(([, v]) => branchArtifacts(v).length === 0)
  .map(([name]) => name)
const toProbe = gateBranches
  .filter(([name]) => !gateMissing.includes(name))
  .flatMap(([, , ps]) => ps)
if (toProbe.length) {
  const byPath = new Map(gateBranches.flatMap(([name, , ps]) => ps.map(([p]) => [p, name])))
  const probe = await probeBranchFiles(toProbe)
  if (probe === null) {
    return {
      ok: false,
      blocked: '支线核验子任务失败，无法确认支线产物在盘',
      incomplete: [
        '支线核验子任务失败（无法确认产物文件存在且非空），已停在主稿之前；请重试或补数后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑',
      ],
      resume_hint: '用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，失败支线重新派发',
    }
  }
  for (const entry of probe) {
    if (!entry.endsWith('|missing')) continue
    const name = byPath.get(entry.slice(0, -'|missing'.length))
    if (name && !gateMissing.includes(name)) gateMissing.push(name)
  }
}
if (gateMissing.length) {
  const absent = gateBranches
    .filter(([name]) => gateMissing.includes(name))
    .flatMap(([, , ps]) => ps.map(([p]) => p))
    .join('、')
  return {
    ok: false,
    blocked: `关键支线缺失：${gateMissing.join('、')}（${absent} 不存在、为空或过小）`,
    incomplete: [
      `关键支线缺失：${gateMissing.join('、')}，已停在主稿之前；补齐缺失产物后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用`,
    ],
    resume_hint: RESUME_HINT,
  }
}

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

// 第 1 轮语言审查：合并成稿之后、第一次独立审稿之前（改稿会换哈希，必须先改再送审）。
const draftPath = `${W}/drafts/${D}-bullbear.md`
phase('语言审查')
const lang1 =
  !mergedOk
    ? null
    : await agent(
        `对 ${draftPath} 做第 1 轮语言审查：调用工具 check_language(draft_path="${draftPath}", round=1)（只标问题），读 ${draftPath}.language.md 清单，按清单逐条修改 ${draftPath}（只改语言表达，不动事实、数字、术语、引用编号与判断强度，不改 <!-- INSERT:… --> 结构；工具报错就不改稿并在 summary 里原样汇报错误）。改完返回 ${draftPath} 的 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 1', phase: '语言审查', schema: FILES_DRAFT }
      )
const lang1Sha = lang1 === null ? null : shaOf(lang1)
const lang1Failed = mergedOk && lang1Sha === null
const reviewedSha = lang1Sha || mergedSha
const review =
  !mergedOk
    ? null
    : await agent(
        `审阅 ${draftPath}（被审草稿 sha256：${
          reviewedSha || '未知'
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
    resume_hint:
      '修订成稿后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发（准备、股东回报支线等）会直接复用，主稿及之后的阶段重新派发',
  }
}

phase('修订')
// revise → 修订 → 复审，最多 2 轮；每个失败出口都写进 incomplete 并停下
let fixed = null
let finalReview = review
let rounds = 0
let currentSha = reviewedSha
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

// 笔记两轮：一次写完、不再改的准备/支线/主稿笔记同样审两轮——第 1 轮审完按清单改，
// 第 2 轮复核（改动过的文件再补一次 round=2 确认，绑定终态哈希）。main.md 与
// return*.md 的内容已并入正式稿，档案副本仍按规则各自两轮。
const notesReview =
  !mergedOk || !reviewPassed
    ? null
    : await agent(
        `对本次运行产出、给人读的 .md 笔记逐一做两轮语言审查（规则见 .agents/skills/dalton-language/SKILL.md，只标问题）。每个文件依次：1) 调用工具 check_language(draft_path="<文件>", round=1)，读 <文件>.language.md 清单并按清单修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度，不改 <!-- INSERT:… --> 结构；工具报错就不改稿并在 summary 里原样汇报错误）；2) 调用 check_language(draft_path="<文件>", round=2) 复核；若第 1 轮后改过稿，改完再调用一次 round=2 确认（记录追加在同一 json）。\n文件清单：\n- ${runDir}/fincheck.md\n- ${runDir}/consensus.md\n- ${runDir}/price.md\n- ${runDir}/return.md\n- ${runDir}/return_appendix.md\n- ${runDir}/return_summary.md\n- ${runDir}/main.md\n清单之外本次运行写的其他给人读的 .md 一并同法处理；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/ 下的文件、知识层渲染文件与 .audit/.annotated/.language 侧车不审。返回处理过的文件列表与摘要。`,
        { agentType: 'general-purpose', label: '笔记语言审查', phase: '修订', schema: PREP_SCHEMA }
      )
if (mergedOk && reviewPassed && notesReview === null) {
  incomplete.push('笔记语言审查失败（准备/支线/主稿笔记未完成两轮语言审查）')
}

// 第 2 轮语言审查：终稿内容定稿（审稿修订循环结束）之后、提交最后一次审稿之前。
// 改了稿就再跑一次 round=2 复核，使最后一条第 2 轮记录绑定终稿哈希；哈希有变
// 则终稿审稿重新绑定独立审稿哈希——语言修改不能让已有 pass 记录失效却无人重审。
const preLang2Sha = currentSha
const lang2 =
  !mergedOk || !reviewPassed
    ? null
    : await agent(
        `对 ${draftPath} 做第 2 轮语言审查：调用工具 check_language(draft_path="${draftPath}", round=2)，读 ${draftPath}.language.md 清单，按清单逐条完成终稿语言修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度，不改 <!-- INSERT:… --> 结构；工具报错就不改稿并在 summary 里原样汇报错误）。若草稿有改动，再调用一次 check_language(draft_path="${draftPath}", round=2) 对终稿复核（记录追加在同一 json），并重跑 python ${S}/scripts/bb_check.py ${draftPath} 确认 0 WARN。返回 ${draftPath} 最终 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 2', phase: '修订', schema: FIXED_SCHEMA }
      )
const lang2Sha = lang2 === null ? null : shaOf(lang2)
if (mergedOk && reviewPassed && lang2Sha === null) {
  incomplete.push('语言审查第 2 轮失败（结果或其 sha256 缺失/非法），未做终稿语言修改')
} else if (mergedOk && reviewPassed && lang2Sha !== preLang2Sha) {
  currentSha = lang2Sha
  const finalCheck = await agent(
    `终稿审稿（语言修改后）：审阅 ${draftPath}（被审草稿 sha256：${lang2Sha}；支线材料在 ${runDir}/，只读不改任何文件）。重点：语言修改未动事实与数字，bb_check 仍 0 WARN，上一轮 must_fix 仍全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
    { agentType: 'dalton-reviewer', label: '终稿审稿', phase: '修订', schema: VERDICT }
  )
  if (finalCheck === null) {
    incomplete.push('终稿审稿失败')
  } else {
    finalReview = finalCheck
  }
}
reviewPassed = finalReview !== null && verdictOf(finalReview) === 'pass'

if (prep === null) incomplete.push('准备阶段失败')
if (ret === null && prep !== null) incomplete.push('股东回报支线失败')
if (main === null) incomplete.push('主稿失败')
if (!mergedOk && main !== null) incomplete.push('合并失败（草稿或其 sha256 缺失/非法）')
if (review === null && mergedOk) incomplete.push('审稿失败')
if (lang1Failed) incomplete.push('语言审查第 1 轮失败（结果或其 sha256 缺失/非法），未按清单修改')
if (!reviewPassed && !incomplete.length) incomplete.push('审稿未最终通过（无 pass 记录）')

phase('产物核验')
const deliverables = [
  `${runDir}/anchors.json`,
  `${runDir}/fincheck.md`,
  `${runDir}/fincheck.md.language.json`,
  `${runDir}/consensus.md`,
  `${runDir}/consensus.md.language.json`,
  `${runDir}/price.md`,
  `${runDir}/price.md.language.json`,
  `${runDir}/return.md`,
  `${runDir}/return.md.language.json`,
  `${runDir}/return_appendix.md`,
  `${runDir}/return_appendix.md.language.json`,
  `${runDir}/return_summary.md`,
  `${runDir}/return_summary.md.language.json`,
  `${runDir}/main.md`,
  `${runDir}/main.md.language.json`,
  `${W}/drafts/${D}-bullbear.md`,
  `${W}/drafts/${D}-bullbear.md.language.json`,
]
const check =
  !mergedOk
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
if (check === null && mergedOk) incomplete.push('产物核验子任务失败')

return {
  ok: mergedOk && reviewPassed && incomplete.length === 0,
  draft: `${W}/drafts/${D}-bullbear.md`,
  review_verdict: finalReview ? verdictOf(finalReview) : null,
  review_summary: finalReview && finalReview.summary,
  language_done: mergedOk && lang1Sha !== null && lang2Sha !== null,
  revise_rounds: rounds,
  fixed: fixed && fixed.summary,
  failed: [prep, ret, main, merged, review].map((x) => x === null),
  incomplete,
}
