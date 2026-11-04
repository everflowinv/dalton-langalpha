export const meta = {
  name: 'initial-coverage',
  description: 'Dalton 初次覆盖：背景∥行业∥财务镜头∥估值历史 → bull/bear 快速版 → 知识层草案 → 12 问初筛 → 审稿',
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
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    verdict: { type: 'string', enum: ['pass', 'revise', 'block'] },
    summary: { type: 'string', minLength: 1 },
    must_fix: { type: 'array', items: { type: 'string', minLength: 1 } },
    // items 元素的键（check/result/reason/lines）由 dalton-reviewer 的提示词契约约束，
    // 结构深度保持在平台 schema_max_depth 限制内（元素键不展开为 schema 节点）。
    items: { type: 'array', items: { type: 'object' } },
  },
  required: ['files', 'verdict', 'summary', 'items'],
}
const HASH = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
  },
  required: ['files'],
}
const verdictOf = (r) => {
  // pass 但 must_fix 非空按 revise 处理，与服务端 publish/review.py 的规则一致
  //（提示词第 1 批第 2 轮 #2）。
  if (r && typeof r === 'object' && r.verdict === 'pass' && Array.isArray(r.must_fix) && r.must_fix.length) {
    return 'revise'
  }
  return r && typeof r === 'object' ? r.verdict : null
}
// 审稿结果与 dalton-reviewer 提示词、服务端发布审稿（publish/review.py）同一契约：
// items 必须逐项覆盖 1–9 项检查，且 files 必须声明审稿子任务实际读取检查的全部文件。
const REVIEW_CHECKS = [1, 2, 3, 4, 5, 6, 7, 8, 9]
const completeReview = (r, expectedFiles = []) => {
  if (!r || typeof r !== 'object') return false
  if (!Array.isArray(r.files) || expectedFiles.some((f) => !r.files.includes(f))) return false
  const items = Array.isArray(r.items) ? r.items : []
  const done = new Set()
  for (const it of items) {
    if (
      it &&
      typeof it === 'object' &&
      Number.isInteger(it.check) &&
      it.check >= 1 &&
      it.check <= 9 &&
      (it.result === 'pass' || it.result === 'revise')
    ) {
      done.add(it.check)
    }
  }
  return REVIEW_CHECKS.every((c) => done.has(c))
}
const LANG_OUT = {
  ...OUT,
  required: ['files', 'summary', 'sha256'],
}
const DRAFT_OUT = {
  ...OUT,
  properties: {
    ...OUT.properties,
    underwriting_version: { type: 'integer', minimum: 1 },
  },
  required: ['files', 'summary', 'sha256', 'underwriting_version'],
}
const REVISION_OUT = {
  ...OUT,
  properties: {
    ...OUT.properties,
    needs_reunderwrite: { type: 'boolean' },
    underwriting_version: { type: 'integer', minimum: 1 },
  },
  required: ['files', 'summary', 'sha256', 'needs_reunderwrite', 'underwriting_version'],
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
const DRAFT_PROBE_OUT = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    draft_content: { type: 'string', minLength: 1 },
  },
  required: ['files', 'draft_content'],
}
const UW_PROBE_OUT = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    exists: { type: 'boolean' },
    first_line: { type: 'string', minLength: 1 },
  },
  required: ['files', 'exists', 'first_line'],
}
const LEGACY_HEADING_RE = /^#{1,3}\s*一、\s*12\s*问初筛/m
const SAVED_VERSION_RE = /^已保存\s*thesis\s*版本\s*(\d+)(?:$|[；;])/i
const checkUnderwritingSummary = (u, expectedFiles) => {
  if (
    !u ||
    typeof u !== 'object' ||
    !Array.isArray(u.files) ||
    expectedFiles.some((f) => !u.files.includes(f))
  ) {
    return { ok: false, reason: 'probe_failed', version: null }
  }
  if (!u.exists) return { ok: false, reason: 'missing', version: null }
  const fl = typeof u.first_line === 'string' ? u.first_line.trim() : ''
  if (
    fl.includes('没有保存成功') ||
    fl.includes('不能写进正文') ||
    fl.includes('尚未保存')
  ) {
    return { ok: false, reason: 'not_saved', version: null }
  }
  const m = fl.match(SAVED_VERSION_RE)
  if (!m) return { ok: false, reason: 'invalid_header', version: null }
  const version = Number(m[1])
  if (!Number.isInteger(version) || version < 1) {
    return { ok: false, reason: 'invalid_header', version: null }
  }
  return { ok: true, reason: 'ok', version }
}
const verifyUnderwriting = async (label, phaseName, extraFiles = []) => {
  const summaryFile = `${R}/underwriting-summary.md`
  const expectedFiles = [summaryFile, ...extraFiles]
  const filesListJson = JSON.stringify(expectedFiles)
  const extraReadHint = extraFiles.length
    ? `并同时读取核对 ${extraFiles.join('、')}，`
    : ''
  const probe = await agent(
    `只读核验赔率摘要 ${summaryFile}（${extraReadHint}不写任何文件）：先读取 ${expectedFiles.join('、')}（若文件不存在则跳过读取），按以下要求返回 JSON：1) \`files\`：固定返回本轮核验的目标文件列表 \`${filesListJson}\`（即使文件不存在也必须原样返回该数组）；2) \`exists\`：${summaryFile} 是否存在且至少 20 字节（\`true\` / \`false\`）；3) \`first_line\`：${summaryFile} 第一个非空行的原样文本（文件不存在或为空时填 \`"none"\`）。`,
    { agentType: 'dalton-evidence', label, phase: phaseName, schema: UW_PROBE_OUT, checkFiles: false }
  )
  return { probe, ...checkUnderwritingSummary(probe, expectedFiles) }
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

const draftPath = `${W}/drafts/${D}-initial-coverage.md`
const draftProbe = await agent(
  `只读探测草稿文件 ${draftPath}（不写任何文件）：1) \`files\` 数组固定返回 \`["${draftPath}"]\`（即使文件不存在也必须返回该路径）；2) 若 ${draftPath} 存在且非空，\`draft_content\` 原样返回该文件前 4000 字符文本（保留原始换行）；若文件不存在或为空，\`draft_content\` 填 \`"absent"\`。`,
  { agentType: 'dalton-evidence', label: '草稿探测', phase: 'bull/bear 快速版', schema: DRAFT_PROBE_OUT, checkFiles: false }
)
const isLegacyDraft = Boolean(
  draftProbe &&
    Array.isArray(draftProbe.files) &&
    draftProbe.files.includes(draftPath) &&
    typeof draftProbe.draft_content === 'string' &&
    draftProbe.draft_content !== 'absent' &&
    LEGACY_HEADING_RE.test(draftProbe.draft_content)
)

let kp = null
let uwInit = null
if (!isLegacyDraft) {
  phase('知识层草案')
  kp = await agent(
    `${base} 从 ${R}/ 下全部支线文件（industry.md、lenses.json、valuation.md、bullbear-quick.md）与 ${W}/drafts/${D}-background.md、${W}/drafts/${D}-lenses.md 抽取知识层初始化草案，按知识层 JSON Schema（见 src/dalton/knowledge/schemas/thesis.schema.json、.agents/skills/dalton-knowledge/SKILL.md 与 .agents/skills/dalton-knowledge/references/structured-write-example.md）写入 ${R}/knowledge-proposal.json，顶层结构为 \`{doc: [ops]}\`，可直接传给 \`knowledge_update\`（覆盖 \`company\`、\`thesis\`〔含支柱、\`underwriting\` 三情景赔率卡、\`evidence\`〕、\`debate\`、\`drivers\`、\`management\` 初记、\`agenda\`）。不直接修改 ${W}/data/。${RETRY_HINT}`,
    { agentType: 'general-purpose', label: '知识层草案', phase: '知识层草案', schema: OUT }
  )
  if (kp === null || branchArtifacts(kp).length === 0) {
    return {
      ok: false,
      blocked: `知识层草案失败（未产出 ${R}/knowledge-proposal.json）`,
      missing,
      incomplete: [
        `知识层草案失败（未产出 ${R}/knowledge-proposal.json），已停在成稿之前；请重试或补齐后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑`,
      ],
      resume_hint:
        '补齐知识层草案后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：已完成的四条支线与 bull/bear 快速版会直接复用',
    }
  }
  // 写手动笔前必须先正式写入并锁定赔率数字：主智能体先正式写入 agenda/debate 等跨文档引用，
  // 再对 thesis 先 dry_run=True 检查、通过后 dry_run=False 正式写入，按固定格式写第一行“已保存 thesis 版本 N”。
  uwInit = await verifyUnderwriting('赔率摘要核验', '知识层草案')
  if (uwInit.reason === 'probe_failed') {
    return {
      ok: false,
      blocked: `赔率摘要核验子任务失败，无法确认 ${R}/underwriting-summary.md 状态`,
      knowledge_proposal: `${R}/knowledge-proposal.json`,
      missing,
      incomplete: [
        `赔率摘要核验子任务失败（无法确认 ${R}/underwriting-summary.md 状态），已停在成稿之前；请用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑`,
      ],
      resume_hint:
        '用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：已完成的四条支线、bull/bear 快速版与知识层草案会直接复用',
    }
  }
  if (!uwInit.ok) {
    const reasonText =
      uwInit.reason === 'not_saved'
        ? `赔率尚未正式保存（${R}/underwriting-summary.md 含“尚未保存”“没有保存成功”或“不能写进正文”，严禁直接续跑）`
        : uwInit.reason === 'invalid_header'
          ? `${R}/underwriting-summary.md 第一行缺少固定格式“已保存 thesis 版本 N”`
          : `${R}/underwriting-summary.md 尚未生成`
    const pauseInstruction = `请按顺序执行：1) 先把 ${R}/knowledge-proposal.json 中不依赖报告数字的 \`agenda\`、\`debate\`、\`company\`、\`drivers\`、\`management\` 先调用 \`knowledge_update(..., dry_run=True)\` 校验，通过后再调用 \`knowledge_update(..., dry_run=False)\` 正式写入，确保 \`trigger_refs\` 等跨文档引用已落库；2) 再对 \`thesis\` 先调用 \`knowledge_update(doc="thesis", ..., dry_run=True)\` 检查（若返回含“没有保存成功”或“不能写进正文”，须先修正 ${R}/knowledge-proposal.json 重试），确认无误后调用 \`knowledge_update(doc="thesis", ..., dry_run=False)\` 正式写入；3) 按固定格式写入 ${R}/underwriting-summary.md：第一行固定写 \`已保存 thesis 版本 N\`（\`N\` 取自 \`knowledge_update\` 返回的版本号；若 \`house_view.stance\` 为 \`"no_view"\` 且 \`underwriting\` 为空，第一行写 \`已保存 thesis 版本 N；暂不表态，不计算赔率\`），摘要文件首行之后只贴正式保存后的赔率摘要，不要贴试写的返回，然后用启动时的同一组参数 + \`resume=<.progress.json 里的 run_id>\` 续跑`
    return {
      ok: false,
      blocked: `待主智能体锁定赔率摘要（${reasonText}）：${pauseInstruction}`,
      knowledge_proposal: `${R}/knowledge-proposal.json`,
      underwriting_summary: `${R}/underwriting-summary.md`,
      missing,
      incomplete: [
        `待主智能体锁定赔率摘要（${reasonText}），已停在成稿之前；${pauseInstruction}`,
      ],
      resume_hint: pauseInstruction,
    }
  }
}

phase('12 问初筛')
const draftPrompt = isLegacyDraft
  ? `${base} 盘上已有在途旧结构草稿 ${draftPath}：综合 ${R}/ 全部文件及 ${W}/drafts/${D}-background.md、${W}/drafts/${D}-lenses.md 保留旧结构继续完善 ${draftPath}（不要求补写 ${R}/questions.md 与 ${R}/underwriting-summary.md；\`underwriting_version\` 固定填 \`1\`），并返回 ${draftPath} 的 \`sha256\`。缺失支线对应的问题写“缺口：需要什么、已尝试路径”。`
  : `${base} 按 .agents/skills/dalton-initial-coverage/references/screen-12q.md、.agents/skills/dalton-initial-coverage/templates/questions.md 和 .agents/skills/dalton-initial-coverage/templates/initial-coverage.md，综合 ${R}/ 全部文件（含赔率摘要 ${R}/underwriting-summary.md 与 ${R}/knowledge-proposal.json）及 ${W}/drafts/${D}-background.md、${W}/drafts/${D}-lenses.md，由一位写手按以下步骤成稿：\n1) 先写内部 12 问底稿 ${R}/questions.md：逐题回答 Q1–Q12 并标明归属章节；未取得的数据在对应题目下写明“缺口：需要什么、已尝试路径”。\n2) 再写初次覆盖报告 ${draftPath}：直接采用 ${R}/underwriting-summary.md 中的赔率数字。正文第一个二级标题必须是「## 摘要」（或「## 一页摘要」「## Summary」）；摘要不用粗体标签列清单，表前用四到六段连续论证写清结论与行动分类、行情与流动性、生意怎么赚钱与壁垒在哪、利润率为什么在这个水平、长期趋势与短期噪音怎么区分（哪些变量能持续跟踪、哪些短期波动不比市场多预判）、观察价/买点区间为什么由业务质量上限和相对同业估值上限决定，再放紧凑三情景赔率表、赔率结论与双向改判条件。其余章节覆盖「## 公司与商业模式」「## 核心投资逻辑与主要分歧」（每个分歧用两三段连贯的业务机制论证写完，不用“多方 / 空方 / 我们的判断”三段对称清单；单列「### 交易角度」小节，覆盖卖方覆盖密度、季度噪音、期权隐含波动率与借券成本、动量与回撤、相对同业性价比，无则写“暂无”；「### 核心投资支柱与验证阈值」小节只写每个支柱的确认、预警、证伪阈值并指明对应哪条分歧，不重复展开论据）「## 估值与赔率」（写清观察价/买点区间的业务质量上限与相对同业估值上限依据，正文退出倍数判断与赔率表一致）「## 风险」「## 管理层与资本配置」「## 研究议程与跟踪」，按需设「## 附注」，文末为单一「## 引用」。\n3) 正文文风与数字纪律：严禁以标题、加粗或列表逐条列 Q1–Q12；每段先用短句给判断，单句事实数字一般不超过两个；同一组同业倍数比较全文只在一处展开；三情景的价格、倍数、回报列成紧凑小表；「[假设]」写在假设数字所在的那一句或那一格里（核验按句〔句末标点或换行断开，见 dalton-conventions 的 writing.md 第 4 节〕或按表格单元格豁免，标记不传到下一句或相邻格子，段前或表前写一次不够）；口径、来源分歧、未取得的数据放进「## 附注」；正文不出现英文字段名、枚举值和内部代号，常见买方英文术语直接用且不建术语替换表。\n4) 返回 JSON：\`files\` 必须声明 \`["${draftPath}", "${R}/questions.md", "${R}/underwriting-summary.md"]\`，\`underwriting_version\` 填入 ${R}/underwriting-summary.md 第一行的版本号 \`N\`，\`sha256\` 填入 ${draftPath} 的 sha256。`
const draft = await agent(draftPrompt, {
  agentType: 'general-purpose',
  label: '初筛成稿',
  phase: '12 问初筛',
  schema: DRAFT_OUT,
})

// The writer reads the payoff summary, so it must declare it: the journal
// only re-runs a dispatch when a declared file changes.
const draftDeclaresSummary =
  isLegacyDraft ||
  (draft !== null &&
    Array.isArray(draft.files) &&
    draft.files.some((f) => typeof f === 'string' && f.replace(/^\.\//, '') === `${R}/underwriting-summary.md`.replace(/^\.\//, '')))
const draftVersionOk =
  draft !== null &&
  draftDeclaresSummary &&
  Number.isInteger(draft.underwriting_version) &&
  draft.underwriting_version >= 1
const draftSha = draftVersionOk ? shaOf(draft) : null
const draftOk = draft !== null && draftSha !== null

// 第 1 轮语言审查：初稿写完之后、第一次独立审稿之前（改稿会换哈希，必须先改再送审）。
phase('语言审查')
const lang1 =
  !draftOk
    ? null
    : await agent(
        `对 ${draftPath} 做第 1 轮语言审查：调用工具 check_language(draft_path="${draftPath}", round=1)（只标问题），读 ${draftPath}.language.md 清单，按清单逐条修改 ${draftPath}（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。改完返回 ${draftPath} 的 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 1', phase: '语言审查', schema: LANG_OUT }
      )
const lang1Sha = lang1 === null ? null : shaOf(lang1)
const lang1Failed = draftOk && lang1Sha === null
const reviewedSha = lang1Sha || draftSha

if (isLegacyDraft) {
  phase('知识层草案')
  kp = await agent(
    `${base} 从 ${draftPath} 与 ${R}/ 下全部支线文件抽取知识层初始化草案，按知识层 JSON Schema（见 .agents/skills/dalton-knowledge/SKILL.md）写入 ${R}/knowledge-proposal.json，顶层结构为 \`{doc: [ops]}\`，可直接传给 \`knowledge_update\`（覆盖 \`company\`、\`thesis\`、\`debate\`、\`drivers\`、\`management\` 初记、\`agenda\`）。不直接修改 ${W}/data/。`,
    { agentType: 'general-purpose', label: '知识层草案', phase: '知识层草案', schema: OUT }
  )
}

phase('审稿')
const reviewExpectedFiles = isLegacyDraft
  ? [draftPath]
  : [draftPath, `${R}/questions.md`, `${R}/underwriting-summary.md`]
const reviewFilesHint = JSON.stringify(reviewExpectedFiles)
const reviewPrompt = isLegacyDraft
  ? `审阅 ${draftPath}（被审草稿 sha256：${reviewedSha || '未知'}；支线材料在 ${R}/，只读不改文件）。这是在途旧结构买方初次覆盖，按旧结构规则核对草稿内 Q1/Q2/Q3/Q4/Q5/Q7/Q8/Q12 是否有判断、预期差与壁垒判断是否有证据、估值分解是否算对（两项之和等于股价对数变化）、\`## 引用\` 表是否存在且每个 \`[引用N]\` 都能在表里找到，豁免独立 questions.md、新章节结构与赔率摘要要求。按 dalton-reviewer 规范输出 JSON（\`files\` 固定返回 \`${reviewFilesHint}\`；\`verdict\` 为 \`"pass"\` / \`"revise"\` / \`"block"\`；\`items\` 逐项覆盖 1–9 项检查）。`
  : `审阅 ${draftPath}（被审草稿 sha256：${reviewedSha || '未知'}；内部 12 问底稿 ${R}/questions.md、赔率摘要 ${R}/underwriting-summary.md 与支线材料在 ${R}/，只读不改文件）。这是买方初次覆盖，重点核对：1) ${R}/questions.md 的重点题（Q1–Q5、Q7、Q8、Q12）是否有明确判断与定量证据；2) 正文第一个二级标题是否为摘要（连贯论证而非粗体标签清单），是否完整覆盖公司与商业模式、核心投资逻辑与主要分歧（含「交易角度」小节，且「核心投资支柱与验证阈值」只写确认、预警、证伪阈值并指明对应分歧，不重复展开论据）、估值与赔率（写清观察价/买点区间的业务质量上限与相对同业估值上限依据，且正文与赔率表的退出倍数一致）、风险、管理层与资本配置、研究议程与跟踪，章节无遗漏也无重复展开，正文未逐条列 Q1–Q12、未混入英文字段名或内部代号；3) 预期差是否只是复述共识，壁垒“正在形成还是正在被破坏”是否有证据及可证伪指标，估值分解是否算对（两项之和等于股价对数变化）；4) 摘要与估值章节的赔率数字是否与 ${R}/underwriting-summary.md 完全一致，文末 \`## 引用\` 表是否存在且每个 \`[引用N]\` 都能在表里找到。按 dalton-reviewer 规范输出 JSON（\`files\` 必须声明实际检查的全部文件 \`${reviewFilesHint}\`；\`verdict\` 为 \`"pass"\` / \`"revise"\` / \`"block"\`；\`items\` 逐项覆盖 1–9 项检查）。`
let review =
  !draftOk
    ? null
    : await agent(reviewPrompt, {
        agentType: 'dalton-reviewer',
        label: '审稿',
        phase: '审稿',
        schema: VERDICT,
        reviewDraft: draftPath,
      })

const incomplete = []
// 审稿结果未覆盖 1–9 项检查或未声明实际检查的文件时按审稿失败处理（fail-closed）。
if (review !== null && !completeReview(review, reviewExpectedFiles)) {
  incomplete.push('审稿结果未覆盖 1–9 项检查或未完整声明检查文件，按审稿失败处理')
  review = null
}
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

// revise → 修订 →（需要改赔率时立刻停下重写版本并更新正文数字）→ 复审，仅 1 轮
let fix = null
let finalReview = review
let rounds = 0
let currentDraftSha = reviewedSha
let reviewPassed = review !== null && verdictOf(review) === 'pass'
while (finalReview && verdictOf(finalReview) === 'revise' && rounds < 1) {
  rounds += 1
  const fixPrompt = isLegacyDraft
    ? `先核对 ${W}/drafts/${D}-initial-coverage.md 的 sha256 是否与审稿前（${currentDraftSha || '当前记录值'}）一致；按审稿意见做第 ${rounds} 轮修订（\`must_fix\` 必改项全部修改，不新增未核实数字；同步修订 ${R}/knowledge-proposal.json，在途旧结构草稿豁免同步 questions.md；\`needs_reunderwrite\` 固定填 \`false\`，\`underwriting_version\` 固定填 \`1\`；改完在 \`sha256\` 返回成稿 sha256）。审稿意见：\n${JSON.stringify(finalReview)}`
    : `先核对 ${W}/drafts/${D}-initial-coverage.md 的 sha256 是否与审稿前（${currentDraftSha || '当前记录值'}）一致，并读取 ${R}/underwriting-summary.md 将其第一行版本号 \`N\` 填入必填字段 \`underwriting_version\`；按审稿意见做第 ${rounds} 轮修订：\n1) \`must_fix\` 必改项全部修改，不新增未核实数字，并同步修订 ${R}/questions.md 与 ${R}/knowledge-proposal.json。\n2) 若无需改动赔率输入（或已按最新 ${R}/underwriting-summary.md 改好正文赔率数字），将 \`needs_reunderwrite\` 设为 \`false\`；若需改动赔率输入（EPS、倍数、持有期、权重等），先在 ${R}/knowledge-proposal.json 的 \`thesis\` 操作列表里更新写 \`/underwriting\` 的那条操作（或 \`create\` 操作 \`value\` 里的 \`underwriting\`），本次暂不修改正文中的赔率数字，将 \`needs_reunderwrite\` 设为 \`true\`。\n3) \`files\` 必须声明 \`["${draftPath}", "${R}/questions.md", "${R}/underwriting-summary.md", "${R}/knowledge-proposal.json"]\`，并在 \`sha256\` 返回成稿 sha256。\n审稿意见：\n${JSON.stringify(finalReview)}`
  fix = await agent(fixPrompt, {
    agentType: 'general-purpose',
    label: `修订 ${rounds}`,
    phase: '审稿',
    schema: REVISION_OUT,
  })
  const summaryPath = `${R}/underwriting-summary.md`
  const normPath = (p) => (typeof p === 'string' ? p.replace(/^\.\//, '') : '')
  const fixHasSummary =
    isLegacyDraft ||
    (fix !== null &&
      Array.isArray(fix.files) &&
      fix.files.some((f) => normPath(f) === normPath(summaryPath)))
  const fixedSha =
    fix === null ||
    !fixHasSummary ||
    typeof fix.needs_reunderwrite !== 'boolean' ||
    !Number.isInteger(fix.underwriting_version) ||
    fix.underwriting_version < 1
      ? null
      : shaOf(fix)
  if (fixedSha === null) {
    incomplete.push(
      `第 ${rounds} 轮修订失败（修订结果、underwriting-summary.md 文件声明、needs_reunderwrite、underwriting_version 或其 sha256 缺失/非法），未复审`
    )
    break
  }
  currentDraftSha = fixedSha
  if (!isLegacyDraft && fix.needs_reunderwrite === true) {
    const reunderwriteInstruction = `修订子任务发现需要改赔率（\`needs_reunderwrite=true\`，当前修订记录的摘要版本：${fix.underwriting_version}）：请主智能体按 ${R}/knowledge-proposal.json 先将更新过的非 \`thesis\` 依赖文档正式写入（\`dry_run=False\`），再对更新后的 \`thesis\` 先调用 \`knowledge_update(doc="thesis", ..., dry_run=True)\` 检查、通过后调用 \`knowledge_update(doc="thesis", ..., dry_run=False)\` 正式写入新版本，按固定格式更新 ${R}/underwriting-summary.md（第一行固定写 \`已保存 thesis 版本 N\`，\`house_view.stance="no_view"\` 时写 \`已保存 thesis 版本 N；暂不表态，不计算赔率\`，首行之后只贴正式保存后的赔率摘要，不要贴试写的返回），然后用启动时的同一组参数 + \`resume=<.progress.json 里的 run_id>\` 续跑；续跑时声明了该摘要的子任务会自动按新摘要重跑，改好正文数字、将 \`needs_reunderwrite\` 设为 \`false\` 并记录新的 \`underwriting_version\` 后进入复审`
    return {
      ok: false,
      blocked: `待主智能体重写赔率摘要：${reunderwriteInstruction}`,
      draft: draftPath,
      knowledge_proposal: `${R}/knowledge-proposal.json`,
      underwriting_summary: `${R}/underwriting-summary.md`,
      missing,
      incomplete: [reunderwriteInstruction],
      resume_hint: reunderwriteInstruction,
    }
  }
  const reReviewPrompt = isLegacyDraft
    ? `复审（第 ${rounds} 轮修订后）：审阅 ${W}/drafts/${D}-initial-coverage.md（被审草稿 sha256：${currentDraftSha}；支线材料在 ${R}/，只读不改文件）。重点核对上一轮 \`must_fix\` 是否全部解决（在途旧结构草稿豁免独立 questions.md、新章节结构与赔率摘要一致性要求）。按 dalton-reviewer 规范输出 JSON（\`files\` 固定返回 \`${reviewFilesHint}\`；\`verdict\` 为 \`"pass"\` / \`"revise"\` / \`"block"\`；\`items\` 逐项覆盖 1–9 项检查）。上一轮审稿意见：\n${JSON.stringify(finalReview)}`
    : `复审（第 ${rounds} 轮修订后）：审阅 ${W}/drafts/${D}-initial-coverage.md（被审草稿 sha256：${currentDraftSha}；内部 12 问底稿 ${R}/questions.md、赔率摘要 ${R}/underwriting-summary.md 与支线材料在 ${R}/，只读不改文件）。重点核对上一轮 \`must_fix\` 是否全部解决，章节是否完整且无重复，正文是否未逐条列 Q1–Q12，摘要与估值章节赔率数字是否与 ${R}/underwriting-summary.md 完全一致。按 dalton-reviewer 规范输出 JSON（\`files\` 必须声明实际检查的全部文件 \`${reviewFilesHint}\`；\`verdict\` 为 \`"pass"\` / \`"revise"\` / \`"block"\`；\`items\` 逐项覆盖 1–9 项检查）。上一轮审稿意见：\n${JSON.stringify(finalReview)}`
  const reReview = await agent(reReviewPrompt, {
    agentType: 'dalton-reviewer',
    label: `复审 ${rounds}`,
    phase: '审稿',
    schema: VERDICT,
    reviewDraft: draftPath,
  })
  if (reReview === null) {
    incomplete.push(`第 ${rounds} 轮复审失败`)
    break
  }
  if (!completeReview(reReview, reviewExpectedFiles)) {
    incomplete.push(`第 ${rounds} 轮复审结果未覆盖 1–9 项检查或未完整声明检查文件，按复审失败处理`)
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
const noteListLines = [
  `- ${W}/drafts/${D}-background.md`,
  `- ${W}/drafts/${D}-lenses.md`,
  `- ${R}/industry.md`,
  `- ${R}/valuation.md`,
  `- ${R}/bullbear-quick.md`,
  ...(isLegacyDraft ? [] : [`- ${R}/questions.md`]),
].join('\n')
const notesReview =
  !draftOk || !reviewPassed
    ? null
    : await agent(
        `对本次运行产出、给人读的 .md 笔记逐一做两轮语言审查（规则见 .agents/skills/dalton-language/SKILL.md，只标问题）。每个文件依次：1) 调用工具 check_language(draft_path="<文件>", round=1)，读 <文件>.language.md 清单并按清单修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）；2) 调用 check_language(draft_path="<文件>", round=2) 复核；若第 1 轮后改过稿，改完再调用一次 round=2 确认（记录追加在同一 json）。\n文件清单：\n${noteListLines}\n清单之外本次运行写的其他给人读的 .md 一并同法处理；${R}/underwriting-summary.md、json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/ 下的文件、知识层渲染文件与 .audit/.annotated/.language 附属文件不审。返回处理过的文件列表与摘要。`,
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
        { agentType: 'general-purpose', label: '语言审查 2', phase: '审稿', schema: LANG_OUT }
      )
const lang2Sha = lang2 === null ? null : shaOf(lang2)
if (draftOk && reviewPassed && lang2Sha === null) {
  incomplete.push('语言审查第 2 轮失败（结果或其 sha256 缺失/非法），未做终稿语言修改')
  reviewPassed = false
} else if (draftOk && reviewPassed) {
  currentDraftSha = lang2Sha
  if (!isLegacyDraft) {
    const finalDraftVersion =
      fix !== null
        ? fix.underwriting_version
        : draft !== null
          ? draft.underwriting_version
          : null
    const uwFinal = await verifyUnderwriting('赔率摘要复核', '审稿', [draftPath])
    if (uwFinal.reason === 'probe_failed') {
      incomplete.push(`赔率摘要复核子任务失败（无法确认 ${R}/underwriting-summary.md 版本号）`)
      reviewPassed = false
    } else if (!uwFinal.ok || uwFinal.version !== finalDraftVersion) {
      const mismatchReason = !uwFinal.ok
        ? `赔率摘要状态异常（${uwFinal.reason}）`
        : `最终稿记录的 underwriting_version（版本 ${finalDraftVersion}）与 ${R}/underwriting-summary.md 首行版本号（版本 ${uwFinal.version}）不一致`
      const syncInstruction = `终稿审稿前赔率版本核对未通过（${mismatchReason}）：请主智能体确认 ${R}/underwriting-summary.md 第一行固定写 \`已保存 thesis 版本 N\`（与数据库 \`thesis\` 最新有效版本一致，首行之后只贴正式保存后的赔率摘要，不要贴试写的返回），然后**由主智能体自己**按摘要里的最新赔率改好 ${draftPath}（及 ${R}/questions.md）中的赔率数字——续跑只会重跑声明的文件有变化的子任务，摘要和正文都没变时，续跑会原样复用旧结果、再次停在这里——改完后再用启动时的同一组参数 + \`resume=<.progress.json 里的 run_id>\` 续跑，修订子任务会因正文变化而重跑并记录新的 \`underwriting_version\``
      return {
        ok: false,
        blocked: `待同步正文与赔率摘要版本：${syncInstruction}`,
        draft: draftPath,
        knowledge_proposal: `${R}/knowledge-proposal.json`,
        underwriting_summary: `${R}/underwriting-summary.md`,
        missing,
        incomplete: [syncInstruction],
        resume_hint: syncInstruction,
      }
    }
  }
  if (reviewPassed && (lang2Sha !== preLang2Sha || rounds > 0)) {
    const finalReviewPrompt = isLegacyDraft
      ? `终稿审稿（语言修改后）：审阅 ${draftPath}（被审草稿 sha256：${lang2Sha}；支线材料在 ${R}/，只读不改文件）。重点：语言修改未动事实与数字，上一轮 \`must_fix\` 仍全部解决（在途旧结构草稿豁免独立 questions.md、新章节结构与赔率摘要一致性要求）。按 dalton-reviewer 规范输出 JSON（\`files\` 固定返回 \`${reviewFilesHint}\`；\`verdict\` 为 \`"pass"\` / \`"revise"\` / \`"block"\`；\`items\` 逐项覆盖 1–9 项检查）。`
      : `终稿审稿（语言修改后）：审阅 ${draftPath}（被审草稿 sha256：${lang2Sha}；内部 12 问底稿 ${R}/questions.md、赔率摘要 ${R}/underwriting-summary.md 与支线材料在 ${R}/，只读不改文件）。重点：语言修改未动事实与数字，上一轮 \`must_fix\` 仍全部解决，摘要与估值章节赔率数字与 ${R}/underwriting-summary.md 完全一致。按 dalton-reviewer 规范输出 JSON（\`files\` 必须声明实际检查的全部文件 \`${reviewFilesHint}\`；\`verdict\` 为 \`"pass"\` / \`"revise"\` / \`"block"\`；\`items\` 逐项覆盖 1–9 项检查）。`
    const finalCheck = await agent(finalReviewPrompt, {
      agentType: 'dalton-reviewer',
      label: '终稿审稿',
      phase: '审稿',
      schema: VERDICT,
      reviewDraft: draftPath,
    })
    if (finalCheck === null) {
      incomplete.push('终稿审稿失败')
      reviewPassed = false
    } else if (!completeReview(finalCheck, reviewExpectedFiles)) {
      incomplete.push('终稿审稿结果未覆盖 1–9 项检查或未完整声明检查文件，按审稿失败处理')
      finalReview = null
      reviewPassed = false
    } else {
      finalReview = finalCheck
      reviewPassed = verdictOf(finalCheck) === 'pass'
    }
  }
}

if (missing.length) incomplete.push(`支线失败：${missing.join('、')}`)
if (lang1Failed) incomplete.push('语言审查第 1 轮失败（结果或其 sha256 缺失/非法），未按清单修改')
if (!draftOk) incomplete.push('初筛成稿失败（草稿、underwriting-summary.md 文件声明、underwriting_version 或其 sha256 缺失/非法）')
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
  ...(isLegacyDraft
    ? []
    : [
        `${R}/underwriting-summary.md`,
        `${R}/questions.md`,
        `${R}/questions.md.language.json`,
      ]),
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
