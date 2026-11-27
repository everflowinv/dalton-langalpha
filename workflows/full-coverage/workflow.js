export const meta = {
  name: 'full-coverage',
  description:
    'Dalton 完整覆盖：一手材料与数字底表 → frame → A∥B∥C∥D 取证 → 建模流水线（串行，无可读候选即停）→ 单一写手成稿 → 语言审查（两轮，第 2 轮在终稿上）→ 独立审稿 → 共享改模与一轮修订 → 知识层修订草案',
}

/* DALTON_INCLUDE:_shared/modeling_stages.js */

const { ticker: T, company: C, workspace_dir: W, as_of: D } = args
const R = `${W}/research/${D}-fc`
const progressPath = args.progress_path || `${R}/.progress.json`
const S = '.agents/skills/dalton-full-coverage'
// A1 同类：SKILL 声明的 market/pm_brief/focus_questions（与可选的 revision_brief
// 文件路径）随 base 进每个子任务，不只给 frame——取证支线与写作同样要按 PM 的
// 要求做。
const pmAsks = []
if (args.pm_brief) pmAsks.push(String(args.pm_brief))
if (Array.isArray(args.focus_questions) && args.focus_questions.length)
  pmAsks.push(`重点问题：${args.focus_questions.join('；')}`)
if (args.revision_brief) pmAsks.push(`本轮必须修正的事项见 ${args.revision_brief}（先读该文件）`)
const pmSection = pmAsks.length ? ` PM 的问题 / 本轮必须修正的事项：${pmAsks.join('；')}。` : ''
const base = `先读 .agents/skills/dalton-conventions/SKILL.md；公司 ${C || T}（${T}），市场 ${args.market || 'US'}，截至 ${D}；数字底表 ${R}/facts.md（目标公司财务与市场数字只取自底表）。${pmSection}`
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
    // items 元素的键（check/result/reason/lines）由 dalton-reviewer 的提示词契约约束，
    // 结构深度保持在平台 schema_max_depth 限制内（元素键不展开为 schema 节点）。
    items: { type: 'array', items: { type: 'object' } },
  },
  required: ['verdict', 'summary', 'items'],
}
const HASH = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
  },
  required: ['files'],
}
const normPath = (p) => _mNormPath(p, W)
const hasArtifact = (list, target) => _mHasPath(list, target, W)
const verdictOf = (r) => {
  // pass 但 must_fix 非空按 revise 处理，与服务端 publish/review.py 的规则一致
  //（提示词第 1 批第 2 轮 #2）。
  if (r && typeof r === 'object' && r.verdict === 'pass' && Array.isArray(r.must_fix) && r.must_fix.length) {
    return 'revise'
  }
  return r && typeof r === 'object' ? r.verdict : null
}
// 审稿结果与 dalton-reviewer 提示词、服务端发布审稿（publish/review.py）同一契约：
// items 必须逐项覆盖 1–9 项检查；缺项的结果不可作为 verdict 使用（fail-closed）。
const REVIEW_CHECKS = [1, 2, 3, 4, 5, 6, 7, 8, 9]
const completeReview = (r) => {
  if (!r || typeof r !== 'object') return false
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
const DRAFT_OUT = { ...OUT, required: ['files', 'summary', 'sha256'] }
const shaOf = (r) =>
  r && typeof r === 'object' && typeof r.sha256 === 'string' && /^[0-9a-f]{64}$/.test(r.sha256)
    ? r.sha256
    : null

// B1：支线产物缺失判定
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
const probeBranchFiles = async (paths, phaseName) => {
  const entries = paths.map((p) => (Array.isArray(p) ? p : [p, 1]))
  const r = await agent(
    `只读核验以下支线产物文件是否存在、非空白且达到字节数下限（不写任何文件）。逐个执行 bash：f='<路径>'；n=<字节数下限>；test -e "$f" && [ "$(wc -c < "$f")" -ge "$n" ] && [ -n "$(tr -d '[:space:]' < "$f")" ] && echo "$f|ok" || echo "$f|missing"。返回 files 数组：每项为上述一行的原样输出（<路径>|ok 或 <路径>|missing）：\n${entries
      .map(([p, min]) => `- ${p}（至少 ${min} 字节）`)
      .join('\n')}`,
    { agentType: 'dalton-evidence', label: '支线核验', phase: phaseName, schema: PROBE_OUT, checkFiles: false }
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
const RESUME_HINT = '用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，缺失项重新派发'
const gateBlock = (blocked, incomplete) => ({
  ok: false,
  blocked,
  incomplete: [incomplete],
  resume_hint: RESUME_HINT,
})

phase('一手材料与数字底表')
const facts = await agent(
  `${base} 按 ${S}/references/facts-ledger.md 建 ${R}/facts.md：读最近 4 份定期报告（${args.form || '10-K'}/10-Q）、4 次电话会、招股书（如有）、内部人交易；按 .agents/skills/dalton-financial-lenses/SKILL.md 做三把镜头；用 .agents/skills/dalton-financial-lenses/scripts/earnings_quality.py 算盈利质量（Sloan 比率、CFO/NI、现金转换周期）；所有必填键用 python ${S}/scripts/facts.py set 写入。若目标文件已存在，先读并核对，缺什么补什么，不必推倒重写。`,
  { agentType: 'dalton-evidence', label: '数字底表', phase: '一手材料与数字底表', schema: OUT }
)
{
  const factsDeclared = branchArtifacts(facts).length > 0
  const factsProbe = factsDeclared
    ? await probeBranchFiles([[`${R}/facts.md`, 500]], '一手材料与数字底表')
    : null
  if (!factsDeclared) {
    return gateBlock(
      '关键支线缺失：数字底表（数字底表子任务未产出或未声明产物）',
      '关键支线缺失：数字底表，已停在问题清单之前；补齐后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用'
    )
  }
  if (factsProbe === null) {
    return gateBlock(
      '支线核验子任务失败，无法确认数字底表在盘',
      '支线核验子任务失败（无法确认 facts.md 存在且足 500 字节），已停在问题清单之前；请重试或补数后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑'
    )
  }
  const factsVerdict = factsProbe.find((e) => e.startsWith(`${R}/facts.md|`))
  if (factsVerdict === undefined || !factsVerdict.endsWith('|ok')) {
    return gateBlock(
      `关键支线缺失：数字底表（${R}/facts.md 不存在、为空或不足 500 字节）`,
      '关键支线缺失：数字底表，已停在问题清单之前；补齐后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用'
    )
  }
}

phase('问题清单')
const frame = await agent(
  `${base} 按 ${S}/references/frame.md 写 ${R}/questions.md（每问含 A / B / C / D 初始自评档位，只写在底稿、不进备忘录正文）。PM 要求：${args.pm_brief || '无'}；重点问题：${
    (args.focus_questions || []).join('；') || '默认'
  }。写完跑 python ${S}/scripts/research_structure.py validate-agenda ${R}/questions.md 到通过。初次覆盖稿与知识层在 ${W}/drafts/ 与 ${W} 根目录。若目标文件已存在，先读并核对，缺什么补什么，不必推倒重写。`,
  { agentType: 'general-purpose', label: 'frame', phase: '问题清单', schema: OUT }
)
{
  const frameDeclared = branchArtifacts(frame).length > 0
  const frameProbe = frameDeclared
    ? await probeBranchFiles([[`${R}/questions.md`, 200]], '问题清单')
    : null
  if (!frameDeclared) {
    return gateBlock(
      '关键支线缺失：问题清单 frame（frame 子任务未产出或未声明产物）',
      '关键支线缺失：问题清单 frame，已停在取证之前；补齐后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用'
    )
  }
  if (frameProbe === null) {
    return gateBlock(
      '支线核验子任务失败，无法确认问题清单在盘',
      '支线核验子任务失败（无法确认 questions.md 存在且足 200 字节），已停在取证之前；请重试或补数后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑'
    )
  }
  const frameVerdict = frameProbe.find((e) => e.startsWith(`${R}/questions.md|`))
  if (frameVerdict === undefined || !frameVerdict.endsWith('|ok')) {
    return gateBlock(
      `关键支线缺失：问题清单 frame（${R}/questions.md 不存在、为空或不足 200 字节）`,
      '关键支线缺失：问题清单 frame，已停在取证之前；补齐后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用'
    )
  }
}

phase('取证')
const RETRY_HINT = '若目标文件已存在，先读并核对，缺什么补什么，不必推倒重写。'
const branches = [
  ['a', 'branch-a-industry'],
  ['b', 'branch-b-sellside-expert'],
  ['c', 'branch-c-primary'],
  ['d', 'branch-d-offstreet'],
]
const ev = await parallel(
  branches.map(([k, f]) => () =>
    agent(
      `${base} 问题清单 ${R}/questions.md。按 ${S}/references/branch-common.md 与 ${S}/references/${f}.md 取证，写 ${R}/e${k}.md，文件末尾带 <!-- 完成 -->。profile=${
        args.profile || 'toc'
      }。${RETRY_HINT}`,
      { agentType: 'dalton-evidence', label: `支线 ${k.toUpperCase()}`, phase: '取证', schema: OUT }
    )
  )
)
const evMissing = branches.filter((_, i) => branchArtifacts(ev[i]).length === 0).map(([k]) => k.toUpperCase())
if (evMissing.length < branches.length) {
  const toProbe = branches
    .filter(([k]) => !evMissing.includes(k.toUpperCase()))
    .map(([k]) => [`${R}/e${k}.md`, 200])
  const byPath = new Map(branches.map(([k]) => [`${R}/e${k}.md`, k.toUpperCase()]))
  const probe = toProbe.length ? await probeBranchFiles(toProbe, '取证') : []
  if (probe === null) {
    return gateBlock(
      '支线核验子任务失败，无法确认支线产物在盘',
      '支线核验子任务失败（无法确认产物文件存在且非空），已停在建模之前；请重试或补数后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑'
    )
  }
  for (const entry of probe) {
    if (!entry.endsWith('|missing')) continue
    const key = byPath.get(entry.slice(0, -'|missing'.length))
    if (key && !evMissing.includes(key)) evMissing.push(key)
  }
}
if (evMissing.length) {
  return {
    ...gateBlock(
      `关键支线缺失：${evMissing.join('、')}（${evMissing.map((k) => `${R}/e${k.toLowerCase()}.md`).join('、')} 不存在或为空）`,
      `关键支线缺失：${evMissing.join('、')}，已停在建模之前；补齐缺失产物后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用`
    ),
    evidence_missing: evMissing,
  }
}

// ---------------------------------------------------------------------------
// 建模阶段：调用共享 runModelingPipeline，pass 与 delivered_with_open_issues 均进入成稿，
// 仅 failed_unusable（无可读候选）时 gateBlock 停在成稿之前。
// ---------------------------------------------------------------------------
phase('建模')
const modelResult = await runModelingPipeline({
  workspaceDir: W,
  ticker: T,
  company: C || T,
  researchDir: R,
  progressPath,
  workflow: 'full-coverage',
  mode: args.model_path ? 'update' : 'auto',
  modelPath: args.model_path || null,
  expectedBaselineSha256: args.expected_baseline_sha256 || null,
  consensusThresholds: args.consensus_thresholds || null,
  asOf: D,
  modelSpecPath: `${S}/references/model-spec.md`,
  phaseLabel: '建模',
  emitPhases: false,
  skipIfNoModel: false,
  contextNote: `${base} 结合支线材料 ${R}/e*.md，新建模型时按 ${S}/references/model-spec.md 的 15 页规格搭建；在原模型上滚动维护时补齐 Facts、Guidance vs Actual、Street vs House、Checks，不新建情景页。`,
})

let modelStatus = modelResult ? modelResult.status : 'failed_unusable'
let modelEffMode =
  modelResult && (modelResult.mode === 'build' || modelResult.mode === 'update')
    ? modelResult.mode
    : args.model_path
      ? 'update'
      : 'build'
let modelPath = modelResult && modelResult.candidate_path ? normPath(modelResult.candidate_path) : null
let modelSha = (modelResult && modelResult.model_sha256) || null
let modelRunId = (modelResult && modelResult.run_id) || null
let modelConsensusPath = (modelResult && modelResult.consensus_path) || null
let modelFailedStages = modelResult && Array.isArray(modelResult.failed_stages) ? modelResult.failed_stages.slice() : []
let modelArtifacts = modelResult && Array.isArray(modelResult.artifacts) ? modelResult.artifacts.slice() : []
const reportedMissingSheets =
  modelResult && Array.isArray(modelResult.missing_required_sheets) ? modelResult.missing_required_sheets : []
let modelCheckPath = hasArtifact(modelArtifacts, `${R}/model_dump/model-check.md`)
  ? `${R}/model_dump/model-check.md`
  : null
let remainingIssuesPath =
  modelStatus === 'delivered_with_open_issues'
    ? (modelResult && modelResult.remaining_issues_path) || `${R}/model_dump/remaining-issues.md`
    : null
const modelVerificationPath = `${R}/model_dump/model-verification.json`

if ((modelResult && modelResult.ok === false) || modelStatus === 'failed_unusable' || !modelPath) {
  return gateBlock(
    `建模失败（${(modelResult && modelResult.failure_reason) || '未产出可读候选模型'}），已停在成稿之前`,
    '建模未产出可读候选模型，已停在成稿之前；补齐可读模型后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用'
  )
}

// 完整覆盖成稿前独立读盘核验必需工作表：不依赖前序检查子任务是否回传 missing_required_sheets，
// 严格区分「齐全（complete）」「缺表（missing）」「未核验（unverified）」；缺表或未核验均停在成稿之前并写入未决清单。
const sheetGate = await verifyFullCoverageSheetsOnDisk({
  workspaceDir: W,
  candidatePath: modelPath,
  mode: modelEffMode,
  priorMissing: reportedMissingSheets,
  phaseLabel: '建模',
})
if (sheetGate.status === 'missing') {
  const missingRequiredSheets = sheetGate.missing_required_sheets
  const missList = missingRequiredSheets.join('、') || '必需工作表'
  if (modelFailedStages.includes('check') || !remainingIssuesPath) {
    const fb = await writeFallbackLedger({
      workspaceDir: W,
      ticker: T,
      researchDir: R,
      progressPath,
      workflow: 'full-coverage',
      mode: modelEffMode,
      candidatePath: modelPath,
      baselinePath: (modelResult && modelResult.baseline_path) || args.model_path || null,
      kind: 'modeling',
      failedStages: modelFailedStages.length ? modelFailedStages : ['sheet_gate'],
      runId: modelRunId,
      fixRounds: modelResult && typeof modelResult.fix_rounds === 'number' ? modelResult.fix_rounds : 0,
      reason: `完整覆盖模型缺少必需工作表（${missList}），已写入未决清单并停在成稿之前`,
      phaseLabel: '建模',
    })
    if (fb && fb.remaining_issues_path) remainingIssuesPath = fb.remaining_issues_path
    if (fb && fb.model_sha256) modelSha = fb.model_sha256
  }
  const remHint = remainingIssuesPath || `${R}/model_dump/remaining-issues.md`
  return {
    ...gateBlock(
      `完整覆盖模型缺少必需工作表（${missList}），已写入未决清单 ${remHint} 并停在成稿之前`,
      `完整覆盖模型缺少必需工作表（${missList}），详见 ${remHint}；补齐所需工作表后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑`
    ),
    model: modelPath,
    model_status: 'delivered_with_open_issues',
    model_verification: modelVerificationPath,
    remaining_issues: remHint,
    sheet_gate_status: 'missing',
    missing_required_sheets: missingRequiredSheets,
  }
}
if (sheetGate.status !== 'complete') {
  const unverifiedStages = modelFailedStages.slice()
  _mPushUnique(unverifiedStages, 'sheet_check')
  const fb = await writeFallbackLedger({
    workspaceDir: W,
    ticker: T,
    researchDir: R,
    progressPath,
    workflow: 'full-coverage',
    mode: modelEffMode,
    candidatePath: modelPath,
    baselinePath: (modelResult && modelResult.baseline_path) || args.model_path || null,
    kind: 'modeling',
    failedStages: unverifiedStages,
    runId: modelRunId,
    fixRounds: modelResult && typeof modelResult.fix_rounds === 'number' ? modelResult.fix_rounds : 0,
    reason: '完整覆盖成稿前独立页签核验失败或超时（未核验），已写入未决清单并停在成稿之前',
    phaseLabel: '建模',
  })
  const remHint = (fb && fb.remaining_issues_path) || remainingIssuesPath || `${R}/model_dump/remaining-issues.md`
  return {
    ...gateBlock(
      `完整覆盖模型必需工作表未核验（独立读盘核验失败或超时），已写入未决清单 ${remHint} 并停在成稿之前`,
      `完整覆盖模型必需工作表尚未核验（详见 ${remHint}）；确认模型工作表齐全后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑`
    ),
    model: modelPath,
    model_status: 'delivered_with_open_issues',
    model_verification: modelVerificationPath,
    remaining_issues: remHint,
    sheet_gate_status: 'unverified',
    missing_required_sheets: [],
  }
}
if (modelStatus === 'delivered_with_open_issues') {
  log(`建模已按「带问题交付」完成（${modelPath}），未决清单见 ${remainingIssuesPath}，工作流继续进入成稿`)
}

const memoPath = `${W}/drafts/${D}-memo.md`
const memoModelMeta = `${R}/.memo-model.json`
phase('成稿')
const memoProbe = await agent(
  `只读核验（不写任何文件）。执行 bash：f='${memoPath}'；m='${modelPath}'；msha='${modelSha || ''}'；meta='${memoModelMeta}'；if ! test -e "$f"; then echo "$f|absent"; elif [ "$(wc -c < "$f")" -ge 500 ] && python ${S}/scripts/validate_memo.py --mode full "$f" >/dev/null 2>&1; then if python3 -c "import json, os, re, sys; f, m, msha, meta = sys.argv[1:5]; txt = open(f, encoding='utf-8').read(); refs = [r.lstrip('./') for r in re.findall(r'model:([^\\s!#]+\\.xlsx)[!#]', txt, re.I)]; ok = all(r == m for r in refs); ok = ok and os.path.exists(meta) and json.load(open(meta, encoding='utf-8')).get('path') == m and (not msha or json.load(open(meta, encoding='utf-8')).get('sha256') == msha); sys.exit(0 if ok else 1)" "$f" "$m" "$msha" "$meta" >/dev/null 2>&1; then echo "$f|ok|$(sha256sum "$f" | cut -d' ' -f1)"; else echo "$f|stale_model"; fi; else echo "$f|incomplete"; fi。返回 files 数组，只含这一行原样输出。`,
  { agentType: 'dalton-evidence', label: 'memo 探测', phase: '成稿', schema: HASH, checkFiles: false }
)
const memoFiles = memoProbe && Array.isArray(memoProbe.files) ? memoProbe.files : null
if (memoFiles === null) {
  return gateBlock(
    'memo 探测子任务失败，无法判断盘上是否已有备忘录',
    'memo 探测子任务未回报结果；请重试或补数后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑'
  )
}
const memoEntry = memoFiles.find((e) => typeof e === 'string' && e.startsWith(`${memoPath}|`))
if (memoEntry === undefined) {
  return gateBlock(
    `memo 探测结果缺 ${memoPath} 的条目`,
    'memo 探测子任务的输出不含该路径的“<路径>|ok|<哈希> / stale_model / incomplete / absent”条目；请重试后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑'
  )
}
const memoTail = memoEntry.slice(memoPath.length + 1)
const memoCompleteSha = memoTail.startsWith('ok|') && /^[0-9a-f]{64}$/.test(memoTail.slice(3)) ? memoTail.slice(3) : null
const memoPartial = memoTail === 'incomplete'
const memoStaleModel = memoTail === 'stale_model'
if (memoCompleteSha === null && !memoPartial && !memoStaleModel && memoTail !== 'absent') {
  return gateBlock(
    `memo 探测结果无法解析（${memoEntry}）`,
    'memo 探测条目既不是 ok|<64 位哈希>也不是 stale_model / incomplete / absent；请重试后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑'
  )
}
const memo = memoCompleteSha
  ? {
      files: [memoPath],
      summary: `盘上已有 ${memoPath}，结构校验通过且引用的模型路径与哈希与本次最终模型 ${modelPath} 一致，沿用最新稿，不重写`,
      sha256: memoCompleteSha,
    }
  : await agent(
      `${base} 通读 ${R}/questions.md（12 问研究提纲）、${R}/e*.md（含文末逐题证据小结）、${R}/facts.md 与本次最终模型 ${modelPath}（sha256：${modelSha || '见账本'}）的输出（新建 15 页模型时读取 Scenarios〔情景分析〕、Street vs House〔本方与市场预期对比〕、Valuation Summary〔估值汇总〕；在原模型上滚动维护时读取 Street vs House 与原模型估值区域，原模型未设情景页时备忘录三情景从原模型估值区域或分析师假设取数，并在正文用文字说明情景假设及模型里没有对应计算），同步按支线证据更新 ${R}/questions.md 每问的 A / B / C / D 自评档位（只写在底稿、不进备忘录正文），并按 ${S}/references/memo-writing.md（章节顺序：摘要 → 公司与商业模式 → 行业与竞争〔可并入上一节〕→ 业务质量 → 核心投资逻辑与争议 → 财务预测与估值 → 风险 → 管理层 → 跟踪 → 附录 → 引用）完成备忘录 ${memoPath}${
        memoStaleModel
          ? `。盘上已有 ${memoPath}（结构完整），但引用的模型路径或哈希与本次最终模型 ${modelPath}（sha256：${modelSha || '见账本'}）不一致：先读已有备忘录，保留未受影响的章节与论证，对照本次最终模型 ${modelPath} 更新受影响的预测、估值、表格与全部 model: 引用`
          : memoPartial
            ? `。盘上已有 ${memoPath} 的半成品（结构校验未通过）：先读已有内容，保留其中可用的部分，补齐缺失的章节与引用，修到结构完整；不要推倒重写已有内容`
            : ''
      }。写给基金经理看：每件事只写一遍、12 问不进正文、计算过程放附录；信息缺口写它对判断的影响，不罗列“未披露”；成稿后通读全文，同一缺口、同一限定、同一改判条件只保留最合适的一处。随后把本次最终模型信息写入 ${memoModelMeta}（JSON：{"path": "${modelPath}", "sha256": "${modelSha || ''}"}），跑 python ${S}/scripts/validate_memo.py --mode full ${memoPath} --research-dir ${R} 到 0 FAIL，并返回 ${memoPath} 的 sha256。`,
      { agentType: 'general-purpose', label: '成稿', phase: '成稿', schema: DRAFT_OUT }
    )
const memoSha = shaOf(memo)
const memoOk = memo !== null && memoSha !== null

phase('语言审查')
const lang1 =
  !memoOk
    ? null
    : await agent(
        `对 ${memoPath} 做语言审查：调用工具 check_language(draft_path="${memoPath}", round=1)（只标问题），读 ${memoPath}.language.md 清单，按清单逐条修改 ${memoPath}（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。改完跑 python ${S}/scripts/validate_memo.py --mode full ${memoPath} --research-dir ${R} 确认 0 FAIL，并返回 ${memoPath} 的 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 1', phase: '语言审查', schema: DRAFT_OUT }
      )
const lang1Sha = lang1 === null ? null : shaOf(lang1)
const lang1Failed = memoOk && lang1Sha === null
const reviewedMemoSha = lang1Sha || memoSha

phase('独立审稿')
const reviewRaw = !memoOk
  ? null
  : await agent(
      `审阅 ${memoPath}（被审草稿 sha256：${
        reviewedMemoSha || '未知'
      }；问题提纲 ${R}/questions.md，支线 ${R}/e*.md，底表 ${R}/facts.md，模型 ${modelPath}${
        modelCheckPath ? `，模型检查报告 ${modelCheckPath}` : ''
      }，全部只读，不修改任何文件）。先只凭摘要复述投资论点（立场、赔率、改判条件），并对 ${R}/questions.md 中自评为 C、D 档的问题重点核对，再按章节顺序（摘要 → 公司与商业模式 → 行业与竞争〔可并入上一节〕→ 业务质量 → 核心投资逻辑与争议 → 财务预测与估值 → 风险 → 管理层 → 跟踪 → 附录 → 引用）逐节检查：摘要（含投委会四问）、公司与商业模式、行业与竞争、业务质量（Good Business〔生意质量〕/ Good People〔管理与组织质量〕/ Good Price〔价格与赔率〕三项判断是否各自独立且有证据支撑，综合判断评级表与方向和行动是否一致）、核心投资逻辑与争议、财务预测与估值（估值三角、赔率卡与三层保护）、风险（含一年后亏损 30% 的事前推演是否写出了空方最强版本）、管理层、跟踪、附录、引用。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；items 逐项覆盖 1–9 项检查）。`,
      { agentType: 'dalton-reviewer', label: '独立审稿', phase: '独立审稿', schema: VERDICT, reviewDraft: memoPath }
    )

const incomplete = []
let review = reviewRaw
if (review !== null && !completeReview(review)) {
  incomplete.push('审稿结果未覆盖 1–9 项检查，按审稿失败处理')
  review = null
}
let fix = null
let finalReview = review
let rounds = 0
let currentMemoSha = reviewedMemoSha

// 所有提前返回的出口都经过 finish：直接读取流水线与账本维护的终态。
const finish = (extra = {}) => ({
  ok: false,
  memo: memoPath,
  model: modelPath,
  model_status: modelStatus,
  model_verification: modelVerificationPath,
  model_check: modelCheckPath,
  remaining_issues: remainingIssuesPath,
  revise_rounds: rounds,
  incomplete,
  ...extra,
})

if (review && verdictOf(review) === 'block') {
  incomplete.push(`审稿 block：${review.summary || '存在不可修订级问题'}`)
  return finish({
    blocked: review.summary || '审稿 block',
    resume_hint:
      '审稿 block：先人工处理（按 must_fix 修订 memo 与模型、补证据），再选择：① 需要重走审稿/修订/收尾的，用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑（支线、底表、建模等未变产物直接复用；盘上已有的 memo 与模型不会被重写，流程沿用最新稿）；② 只需人工收尾的，按 dalton-full-coverage SKILL 手动降级路径执行，并以 manual=true 调用 dalton_check_done',
  })
}

phase('修订')
// revise → 共享改模入口 + 备忘录修订 → 复审，仅 1 轮（一轮修订）
while (finalReview && verdictOf(finalReview) === 'revise' && rounds < 1) {
  rounds += 1
  const revModel = await reviseModelForReport({
    workspaceDir: W,
    ticker: T,
    researchDir: R,
    progressPath,
    workflow: 'full-coverage',
    currentModelPath: modelPath,
    currentModelSha256: modelSha,
    currentStatus: modelStatus,
    currentArtifacts: modelArtifacts,
    consensusPath: modelConsensusPath,
    consensusThresholds: args.consensus_thresholds || null,
    priorFailedStages: modelFailedStages,
    runId: modelRunId,
    reportPath: memoPath,
    reviewFeedback: finalReview,
    extraSyncPaths: [memoModelMeta],
    phaseLabel: '修订',
  })
  modelPath = revModel.modelPath || modelPath
  modelSha = revModel.modelSha256 || modelSha
  modelStatus = revModel.status
  modelRunId = revModel.runId || modelRunId
  modelFailedStages = Array.isArray(revModel.failedStages) ? revModel.failedStages.slice() : modelFailedStages
  modelArtifacts = Array.isArray(revModel.artifacts) ? revModel.artifacts : modelArtifacts
  if (revModel.reportSha256) currentMemoSha = revModel.reportSha256
  modelCheckPath = hasArtifact(modelArtifacts, `${R}/model_dump/model-check.md`)
    ? `${R}/model_dump/model-check.md`
    : null
  remainingIssuesPath =
    modelStatus === 'delivered_with_open_issues'
      ? revModel.remainingIssuesPath || `${R}/model_dump/remaining-issues.md`
      : null
  if (revModel.ok === false || modelStatus === 'failed_unusable' || !modelPath) {
    incomplete.push(`第 ${rounds} 轮报告联动改模未能维持可读候选模型，未复审，流程终止`)
    return finish()
  }

  const existingModelReports = []
  if (modelCheckPath) existingModelReports.push(modelCheckPath)
  if (hasArtifact(modelArtifacts, `${R}/model_dump/consensus-diff.md`)) {
    existingModelReports.push(`${R}/model_dump/consensus-diff.md`)
  }
  if (remainingIssuesPath) existingModelReports.push(remainingIssuesPath)
  const reportsClause = existingModelReports.length
    ? `与模型核验报告（${existingModelReports.join('、')}）`
    : ''

  fix = await agent(
    `${base} 先核对 ${memoPath} 的 sha256 是否与审稿前（${
      currentMemoSha || '当前记录值'
    }）一致；按审稿意见${reportsClause}做第 ${rounds} 轮备忘录修订（当前最终模型为 ${modelPath}${
      modelSha ? `，sha256：${modelSha}` : ''
    }，已由共享改模入口完成检查与元数据同步，此处不再改写 .xlsx，仅核对备忘录全部 model: 引用与数字已与 ${modelPath} 一致并更新 ${memoModelMeta}）。P0 全改，修订后通读全文，同一缺口、同一限定、同一改判条件只保留最合适的一处；跑 python ${S}/scripts/validate_memo.py --mode full ${memoPath} --research-dir ${R} 到 0 FAIL。审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'general-purpose', label: `修订 ${rounds}`, phase: '修订', schema: DRAFT_OUT }
  )
  const fixedMemoSha = shaOf(fix)
  if (fixedMemoSha === null) {
    incomplete.push(`第 ${rounds} 轮修订失败（修订结果或其 sha256 缺失/非法），未复审，流程终止`)
    return finish()
  }
  currentMemoSha = fixedMemoSha
  const reReview = await agent(
    `复审（第 ${rounds} 轮修订后）：审阅 ${memoPath}（被审草稿 sha256：${
      currentMemoSha
    }；模型 ${modelPath}；${W}/model/*.xlsx 只读不改）。重点核对上一轮 must_fix 是否全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；items 逐项覆盖 1–9 项检查）。上一轮审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'dalton-reviewer', label: `复审 ${rounds}`, phase: '修订', schema: VERDICT, reviewDraft: memoPath }
  )
  if (reReview === null) {
    incomplete.push(`第 ${rounds} 轮复审失败，流程终止`)
    return finish()
  }
  if (!completeReview(reReview)) {
    incomplete.push(`第 ${rounds} 轮复审结果未覆盖 1–9 项检查，按复审失败处理，流程终止`)
    return finish()
  }
  if (verdictOf(reReview) === 'block') {
    incomplete.push(`复审 block：${reReview.summary || '存在不可修订级问题'}，流程终止`)
    return finish({
      blocked: reReview.summary || '复审 block',
      resume_hint: RESUME_HINT,
    })
  }
  finalReview = reReview
}
let reviewPassed = finalReview !== null && verdictOf(finalReview) === 'pass'

// 笔记两轮：支线与底稿笔记做两轮语言审查；model_dump/ 下机器报告免审。
const notesReview =
  !memoOk || !reviewPassed
    ? null
    : await agent(
        `对本次运行产出、给人读的 .md 笔记逐一做两轮语言审查（规则见 .agents/skills/dalton-language/SKILL.md，只标问题）。每个文件依次：1) 调用工具 check_language(draft_path="<文件>", round=1)，读 <文件>.language.md 清单并按清单修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）；2) 调用 check_language(draft_path="<文件>", round=2) 复核；若第 1 轮后改过稿，改完再调用一次 round=2 确认（记录追加在同一 json）。\n文件清单：\n- ${R}/facts.md\n- ${R}/questions.md\n- ${R}/ea.md\n- ${R}/eb.md\n- ${R}/ec.md\n- ${R}/ed.md\n本次运行清单之外新写的其他给人读的 .md 一并同法处理；跳过 model_dump/ 下的全部机器报告、json/xlsx 等机器文件与 .audit/.annotated/.language 侧车。返回处理过的文件列表与摘要。`,
        { agentType: 'general-purpose', label: '笔记语言审查', phase: '修订', schema: OUT }
      )
if (memoOk && reviewPassed && notesReview === null) {
  incomplete.push('笔记语言审查失败（支线/底稿笔记未完成两轮语言审查）')
}

const preLang2Sha = currentMemoSha
const lang2 =
  !memoOk || !reviewPassed
    ? null
    : await agent(
        `对 ${memoPath} 做第 2 轮语言审查：调用工具 check_language(draft_path="${memoPath}", round=2)，读 ${memoPath}.language.md 清单，按清单逐条完成终稿语言修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。若草稿有改动，再调用一次 check_language(draft_path="${memoPath}", round=2) 对终稿复核（记录追加在同一 json），并跑 python ${S}/scripts/validate_memo.py --mode full ${memoPath} --research-dir ${R} 确认 0 FAIL。返回 ${memoPath} 最终 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 2', phase: '修订', schema: DRAFT_OUT }
      )
const lang2Sha = lang2 === null ? null : shaOf(lang2)
if (memoOk && reviewPassed && lang2Sha === null) {
  incomplete.push('语言审查第 2 轮失败（结果或其 sha256 缺失/非法），未做终稿语言修改')
  reviewPassed = false
} else if (memoOk && reviewPassed && lang2Sha !== preLang2Sha) {
  currentMemoSha = lang2Sha
  const finalCheck = await agent(
    `终稿审稿（语言修改后）：审阅 ${memoPath}（被审草稿 sha256：${lang2Sha}；模型 ${modelPath}；${W}/model/*.xlsx 只读不改）。重点：语言修改未动事实与数字，上一轮 must_fix 仍全部解决，validate_memo 仍 0 FAIL。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；items 逐项覆盖 1–9 项检查）。`,
    { agentType: 'dalton-reviewer', label: '终稿审稿', phase: '修订', schema: VERDICT, reviewDraft: memoPath }
  )
  if (finalCheck === null) {
    incomplete.push('终稿审稿失败')
    reviewPassed = false
  } else if (!completeReview(finalCheck)) {
    incomplete.push('终稿审稿结果未覆盖 1–9 项检查，按审稿失败处理')
    reviewPassed = false
  } else {
    finalReview = finalCheck
    reviewPassed = verdictOf(finalCheck) === 'pass'
  }
}

const kp =
  !memoOk
    ? null
    : await agent(
        `${base} 先核对 ${memoPath} 的 sha256 是否为修订后哈希（${currentMemoSha || '修订后记录值'}）；最终模型为 ${modelPath}（终态：${modelStatus}，账本：${modelVerificationPath}）；再对比 ${W} 根目录现有知识层，按知识层修订 JSON 规范写知识层修订草案 ${R}/knowledge-revision.json（结构为 {doc: {ops, reason, evidence_refs}}）：每处改动给旧值、新值、原因、证据（引用编号）。管理层指引风格与 12 季指引兑现历史（ec.md 第 C4 节）写入 management 部分，models 部分按 .agents/skills/dalton-model/SKILL.md 登记最终模型版本与关键输出单元格；不直接修改 ${W}/data/。`,
        { agentType: 'general-purpose', label: '知识层修订草案', phase: '修订', schema: OUT }
      )

if (!memoOk) incomplete.push('memo 成稿失败（草稿或其 sha256 缺失/非法）')
if (lang1Failed) incomplete.push('语言审查第 1 轮失败（结果或其 sha256 缺失/非法），未按清单修改')
if (modelStatus !== 'pass' && modelStatus !== 'delivered_with_open_issues') {
  incomplete.push('建模或报告联动改模未维持可读候选模型')
}
if (review === null && memoOk) incomplete.push('独立审稿失败')
if (!reviewPassed && !incomplete.length) incomplete.push('审稿未最终通过（无 pass 记录）')
if (kp === null && memoOk) incomplete.push('知识层修订草案失败')

phase('产物核验')
const deliverables = [
  `${R}/facts.md`,
  `${R}/facts.md.language.json`,
  `${R}/questions.md`,
  `${R}/questions.md.language.json`,
  `${R}/ea.md`,
  `${R}/ea.md.language.json`,
  `${R}/eb.md`,
  `${R}/eb.md.language.json`,
  `${R}/ec.md`,
  `${R}/ec.md.language.json`,
  `${R}/ed.md`,
  `${R}/ed.md.language.json`,
  modelPath,
  modelVerificationPath,
  memoPath,
  `${memoPath}.language.json`,
  `${R}/knowledge-revision.json`,
]
if (remainingIssuesPath) deliverables.push(remainingIssuesPath)
const check =
  !memoOk
    ? null
    : await agent(
        `只读核对以下文件是否存在并计算 sha256（用 bash 的 test -e 与 sha256sum，不写任何文件）。只返回下面列出的文件，一个不能少、不能多、不能夹带其他条目：\n${deliverables
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

phase('自动跟踪')
const runOk = memoOk && reviewPassed && incomplete.length === 0
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
      reason: !memoOk ? 'memo 未完成' : !reviewPassed ? '审稿未最终通过' : `流程未完成：${incomplete[0]}`,
    }
if (runOk) {
  log(
    `完整覆盖完成，收尾需调用 dalton_coverage_tracking(action="enable", source="coverage_finish") 开启 ${T} 自动跟踪（若 paused_by_user 则保持关闭）`
  )
} else {
  log(`不开启自动跟踪：${tracking.reason}`)
}

return {
  ok: runOk,
  memo: `${W}/drafts/${D}-memo.md`,
  model: modelPath,
  model_status: modelStatus,
  model_verification: modelVerificationPath,
  model_check: modelCheckPath,
  remaining_issues: remainingIssuesPath,
  review_done: finalReview !== null,
  review_verdict: finalReview && verdictOf(finalReview),
  language_done: memoOk && lang1Sha !== null && lang2Sha !== null,
  revise_rounds: rounds,
  fix_summary: fix && fix.summary,
  knowledge_revision: kp && `${R}/knowledge-revision.json`,
  evidence_missing: evMissing,
  tracking,
  incomplete,
}
