export const meta = {
  name: 'initial-coverage',
  description: 'Dalton 初次覆盖：背景∥行业∥财务镜头∥估值历史 → bull/bear 快速版 → 12 问初筛 → 知识层草案 → 审稿',
}

const { ticker: T, company: C, workspace_dir: W, as_of: D } = args
const R = `${W}/research/${D}-ic`
// A1：SKILL 声明的 pm_question/sector（与可选的 revision_brief 文件路径）必须随
// base 进每个子任务，否则 PM 的问题与上一轮审稿的修正要求到不了任何支线。
const pmAsks = []
if (args.pm_question) pmAsks.push(String(args.pm_question))
if (args.revision_brief) pmAsks.push(`本轮必须修正的事项见 ${args.revision_brief}（先读该文件）`)
const pmSection = pmAsks.length ? ` PM 的问题 / 本轮必须修正的事项：${pmAsks.join('；')}。` : ''
const base = `先读 .agents/skills/dalton-conventions/SKILL.md；公司 ${C || T}（${T}），市场 ${args.market || 'US'}${
  args.sector ? `，所属行业 ${args.sector}` : ''
}，截至 ${D}。${pmSection}`
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
    { agentType: 'dalton-evidence', label: '支线核验', phase: '取证', schema: PROBE_OUT, checkFiles: false }
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
// entries: [[key, 中文名, 产物路径数组], ...]；路径可以是字符串或 [路径, 契约
// min_bytes]；resultOf 由调用方给出 key→子任务结果。
const missingBranchesOf = async (entries, resultOf) => {
  const missing = entries.filter(([k]) => branchArtifacts(resultOf(k)).length === 0).map(([k]) => k)
  const byPath = new Map(
    entries.flatMap(([k, , ps]) => ps.map((p) => [(Array.isArray(p) ? p[0] : p), k]))
  )
  const toProbe = entries.filter(([k]) => !missing.includes(k)).flatMap(([, , ps]) => ps)
  if (!toProbe.length) return { missing, probeFailed: false }
  const probe = await probeBranchFiles(toProbe)
  if (probe === null) return { missing, probeFailed: true }
  for (const entry of probe) {
    if (!entry.endsWith('|missing')) continue
    const key = byPath.get(entry.slice(0, -'|missing'.length))
    if (key && !missing.includes(key)) missing.push(key)
  }
  return { missing, probeFailed: false }
}

phase('取证')
// resume 提示：block 后主线程补数再用原参数 resume，失败支线的子任务会在已补
// 文件的基础上核对完善，所以每条支线都写明“已存在先核对、缺什么补什么”。
const RETRY_HINT = '若目标文件已存在，先读并核对，缺什么补什么，不必推倒重写。'
const [bg, ind, lens, val] = await parallel([
  () =>
    agent(
      `${base} 按 .agents/skills/dalton-company-background/SKILL.md 写 9 段背景稿到 ${W}/drafts/${D}-background.md。${RETRY_HINT}`,
      { agentType: 'dalton-evidence', label: '公司背景', phase: '取证', schema: OUT }
    ),
  () =>
    agent(
      `${base} 按 .agents/skills/dalton-initial-coverage/references/branch-industry.md 写行业支线到 ${R}/industry.md。${
        args.peers && args.peers.length ? '同业候选：' + args.peers.join(',') : ''
      }${RETRY_HINT}`,
      { agentType: 'dalton-evidence', label: '行业支线', phase: '取证', schema: OUT }
    ),
  () =>
    agent(
      `${base} 按 .agents/skills/dalton-financial-lenses/SKILL.md 做三把镜头，JSON 到 ${R}/lenses.json，草稿到 ${W}/drafts/${D}-lenses.md。${RETRY_HINT}`,
      { agentType: 'dalton-evidence', label: '财务镜头', phase: '取证', schema: OUT }
    ),
  // B1 补充：child_timeout 不能按派发覆盖（validate_dispatch 只认
  // agentType/label/phase/schema，driver 统一用 caps.child_timeout），估值支线
  // 拆成“取数”“写稿”两次派发，各得一个完整的 child_timeout 窗口；取数产物
  // 落盘后，resume 时写稿可单独重派。
  () =>
    (async () => {
      const data = await agent(
        `${base} 按 .agents/skills/dalton-initial-coverage/references/branch-valuation-history.md 只做取数阶段：拉取估值历史分解（股价、EPS、PE）与可比公司财务所需的全部原始数据，连同选定的可比清单与选取理由存 ${R}/valuation-data.json（不写正式稿、不建 xlsx）；已取到的写明来源，取不到的逐条记进 gaps。${RETRY_HINT}`,
        { agentType: 'dalton-evidence', label: '估值取数', phase: '取证', schema: OUT }
      )
      // B1 共用判定：取数返回 null 或没有声明任何产物（空 files）都不派写稿；
      // 声明了产物还要在盘核验 valuation-data.json 存在且非空（派发本身失败同
      // 路）。任一失败都返回 null，由统一的支线 block 出口停下。核验只看本步
      // 询问的那个路径（回应里可能混有其他派发的条目）。
      if (branchArtifacts(data).length === 0) return null
      const dataProbe = await probeBranchFiles([[`${R}/valuation-data.json`, 1]])
      const dataVerdict =
        dataProbe !== null
          ? dataProbe.find((e) => e.startsWith(`${R}/valuation-data.json|`))
          : null
      if (dataVerdict === null || !dataVerdict.endsWith('|ok')) return null
      return await agent(
        `${base} 按 .agents/skills/dalton-initial-coverage/references/branch-valuation-history.md 做写稿阶段：以 ${R}/valuation-data.json 为底（缺的自行补取），写估值历史与可比（含 ln(P1/P0)=ln(EPS1/EPS0)+ln(PE1/PE0) 分解表）到 ${R}/valuation.md，可比表 ${R}/comps.xlsx。${RETRY_HINT}`,
        { agentType: 'dalton-evidence', label: '估值历史与可比', phase: '取证', schema: OUT }
      )
    })(),
])

// 契约 min_bytes：核验下限按 contract.json 的取证阶段逐项取。
const BRANCHES = [
  ['bg', '公司背景', [[`${W}/drafts/${D}-background.md`, 200]]],
  ['ind', '行业支线', [[`${R}/industry.md`, 200]]],
  ['lens', '财务镜头', [[`${R}/lenses.json`, 50], [`${W}/drafts/${D}-lenses.md`, 200]]],
  ['val', '估值历史与可比', [[`${R}/valuation.md`, 200], [`${R}/comps.xlsx`, 1000]]],
]
const got = { bg, ind, lens, val }
const { missing, probeFailed } = await missingBranchesOf(BRANCHES, (k) => got[k])
const BRANCH_NAME = Object.fromEntries(BRANCHES.map(([k, name]) => [k, name]))
const BRANCH_PATHS = Object.fromEntries(
  BRANCHES.map(([k, , ps]) => [k, ps.map((p) => (Array.isArray(p) ? p[0] : p))])
)
log(`支线完成 ${BRANCHES.length - missing.length}/4，缺：${missing.join(',') || '无'}`)
// B1：contract 把四条支线产物都列为必交（全为关键支线，估值在内）。任一缺失
// （含全部缺失）都停在成稿之前，出口与审稿 block 同一段文案：主线程补数后用
// 原参数 resume 重跑——已完派发按 journal 复用（label、prompt 哈希与产物哈希
// 一致才复用，见 RunWorkflow 的 resume 机制）。
if (probeFailed) {
  return {
    ok: false,
    blocked: '支线核验子任务失败，无法确认支线产物在盘',
    missing,
    incomplete: [
      '支线核验子任务失败（无法确认产物文件存在且非空），已停在成稿之前；请重试或补数后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑',
    ],
    resume_hint:
      '用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，失败支线重新派发',
  }
}
if (missing.length) {
  const names = missing.map((k) => BRANCH_NAME[k]).join('、')
  const absent = missing.flatMap((k) => BRANCH_PATHS[k]).join('、')
  return {
    ok: false,
    blocked: `关键支线缺失：${names}（${absent} 不存在或为空）`,
    missing,
    incomplete: [
      `关键支线缺失：${names}，已停在成稿之前；补齐缺失产物后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用`,
    ],
    resume_hint:
      '补齐缺失支线产物后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，缺失支线重新派发（其提示词已写明“若目标文件已存在，先读并核对，缺什么补什么”）',
  }
}

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

// 第 1 轮语言审查：初稿写完之后、第一次独立审稿之前（改稿会换哈希，必须先改再送审）。
const draftPath = `${W}/drafts/${D}-initial-coverage.md`
phase('语言审查')
const lang1 =
  !draftOk
    ? null
    : await agent(
        `对 ${draftPath} 做第 1 轮语言审查：调用工具 check_language(draft_path="${draftPath}", round=1)（只标问题），读 ${draftPath}.language.md 清单，按清单逐条修改 ${draftPath}（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。改完返回 ${draftPath} 的 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 1', phase: '语言审查', schema: DRAFT_OUT }
      )
const lang1Sha = lang1 === null ? null : shaOf(lang1)
const lang1Failed = draftOk && lang1Sha === null
const reviewedSha = lang1Sha || draftSha

phase('知识层草案')
const kp =
  !draftOk
    ? null
    : await agent(
        `${base} 从 ${draftPath} 抽取知识层草案，按 PK.2 的 JSON Schema（.agents/skills/dalton-knowledge/SKILL.md 有字段说明）写 ${R}/knowledge-proposal.json，结构为 {doc: [ops]}，可直接交给 knowledge_update（company、thesis 支柱、debate、drivers、management 初记、agenda）。只抽取，不新增判断，也不直接改 ${W}/data/。`,
        { agentType: 'general-purpose', label: '知识层草案', phase: '知识层草案', schema: OUT }
      )

phase('审稿')
const review =
  !draftOk
    ? null
    : await agent(
        `审阅 ${draftPath}（被审草稿 sha256：${
          reviewedSha || '未知'
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
    resume_hint:
      '修订草稿后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发（四条支线等只读产物）会直接复用，成稿及之后的阶段重新派发',
  }
}

// revise → 修订 → 复审，最多 2 轮；每个失败出口都写进 incomplete 并停下
let fix = null
let finalReview = review
let rounds = 0
let currentDraftSha = reviewedSha
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

// 笔记两轮：一次写完、不再改的支线/背景笔记同样审两轮——第 1 轮审完按清单改，
// 第 2 轮复核（改动过的文件再补一次 round=2 确认，绑定终态哈希）。
const notesReview =
  !draftOk || !reviewPassed
    ? null
    : await agent(
        `对本次运行产出、给人读的 .md 笔记逐一做两轮语言审查（规则见 .agents/skills/dalton-language/SKILL.md，只标问题）。每个文件依次：1) 调用工具 check_language(draft_path="<文件>", round=1)，读 <文件>.language.md 清单并按清单修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）；2) 调用 check_language(draft_path="<文件>", round=2) 复核；若第 1 轮后改过稿，改完再调用一次 round=2 确认（记录追加在同一 json）。\n文件清单：\n- ${W}/drafts/${D}-background.md\n- ${W}/drafts/${D}-lenses.md\n- ${R}/industry.md\n- ${R}/valuation.md\n- ${R}/bullbear-quick.md\n清单之外本次运行写的其他给人读的 .md 一并同法处理；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/ 下的文件、知识层渲染文件与 .audit/.annotated/.language 侧车不审。返回处理过的文件列表与摘要。`,
        { agentType: 'general-purpose', label: '笔记语言审查', phase: '审稿', schema: OUT }
      )
if (draftOk && reviewPassed && notesReview === null) {
  incomplete.push('笔记语言审查失败（支线/背景笔记未完成两轮语言审查）')
}

// 第 2 轮语言审查：终稿内容定稿（审稿修订循环结束）之后、提交最后一次审稿之前。
// 改了稿就再跑一次 round=2 复核，使最后一条第 2 轮记录绑定终稿哈希；哈希有变
// 则终稿审稿重新绑定独立审稿哈希——语言修改不能让已有 pass 记录失效却无人重审。
const preLang2Sha = currentDraftSha
const lang2 =
  !draftOk || !reviewPassed
    ? null
    : await agent(
        `对 ${draftPath} 做第 2 轮语言审查：调用工具 check_language(draft_path="${draftPath}", round=2)，读 ${draftPath}.language.md 清单，按清单逐条完成终稿语言修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。若草稿有改动，再调用一次 check_language(draft_path="${draftPath}", round=2) 对终稿复核（记录追加在同一 json）。返回 ${draftPath} 最终 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 2', phase: '审稿', schema: DRAFT_OUT }
      )
const lang2Sha = lang2 === null ? null : shaOf(lang2)
if (draftOk && reviewPassed && lang2Sha === null) {
  incomplete.push('语言审查第 2 轮失败（结果或其 sha256 缺失/非法），未做终稿语言修改')
} else if (draftOk && reviewPassed && lang2Sha !== preLang2Sha) {
  currentDraftSha = lang2Sha
  const finalCheck = await agent(
    `终稿审稿（语言修改后）：审阅 ${draftPath}（被审草稿 sha256：${lang2Sha}；支线材料在 ${R}/，只读不改文件）。重点：语言修改未动事实与数字，上一轮 must_fix 仍全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
    { agentType: 'dalton-reviewer', label: '终稿审稿', phase: '审稿', schema: VERDICT }
  )
  if (finalCheck === null) {
    incomplete.push('终稿审稿失败')
  } else {
    finalReview = finalCheck
  }
}
reviewPassed = finalReview !== null && verdictOf(finalReview) === 'pass'

if (missing.length) incomplete.push(`支线失败：${missing.join('、')}`)
if (lang1Failed) incomplete.push('语言审查第 1 轮失败（结果或其 sha256 缺失/非法），未按清单修改')
if (!draftOk) incomplete.push('初筛成稿失败（草稿或其 sha256 缺失/非法）')
if (bb === null) incomplete.push('bull/bear 快速版失败')
if (kp === null) incomplete.push('知识层草案失败')
if (review === null && draftOk) incomplete.push('审稿失败')
if (!reviewPassed && !incomplete.length) incomplete.push('审稿未最终通过（无 pass 记录）')

phase('产物核验')
const deliverables = [
  `${W}/drafts/${D}-background.md`,
  `${W}/drafts/${D}-background.md.language.json`,
  `${R}/industry.md`,
  `${R}/industry.md.language.json`,
  `${R}/lenses.json`,
  `${W}/drafts/${D}-lenses.md`,
  `${W}/drafts/${D}-lenses.md.language.json`,
  `${R}/valuation.md`,
  `${R}/valuation.md.language.json`,
  `${R}/comps.xlsx`,
  `${R}/bullbear-quick.md`,
  `${R}/bullbear-quick.md.language.json`,
  `${W}/drafts/${D}-initial-coverage.md`,
  `${W}/drafts/${D}-initial-coverage.md.language.json`,
  `${R}/knowledge-proposal.json`,
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
  language_done: draftOk && lang1Sha !== null && lang2Sha !== null,
  revise_rounds: rounds,
  fix_summary: fix && fix.summary,
  tracking,
  incomplete,
}
