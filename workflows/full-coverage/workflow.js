export const meta = {
  name: 'full-coverage',
  description:
    'Dalton 完整覆盖：一手材料与数字底表 → frame → A∥B∥C∥D 取证 → 建模（串行，失败即停）→ 单一写手成稿 → 语言审查（两轮，第 2 轮在终稿上）→ 只读审模型∥独立审稿 → 一轮修订 → 知识层修订草案',
}

const { ticker: T, company: C, workspace_dir: W, as_of: D } = args
const R = `${W}/research/${D}-fc`
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
const FINAL_MODEL = {
  type: 'object',
  properties: {
    path: { type: 'string', minLength: 1 },
    sha256: { type: 'string', minLength: 64 },
  },
  required: ['path', 'sha256'],
}
// Compare model paths relative to the workspace: callers pass workspace_dir
// either as '.' or as an absolute folder, while the resolver reports model/….
const normPath = (p) => {
  if (typeof p !== 'string') return p
  let s = p.replace(/^\.\//, '')
  const w = typeof W === 'string' ? W.replace(/\/+$/, '').replace(/^\.\//, '') : ''
  if (w && w !== '.' && s.startsWith(w + '/')) s = s.slice(w.length + 1)
  return s.replace(/^\.\//, '')
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
// B1 共用出口：缺失（含全部缺失）与核验失败都停在当前阶段之前，文案与审稿
// block 一致，主线程补数后用原参数 resume 重跑（已完派发按 journal 复用）。
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
// B1 共用判定：底表 null/空回报、在盘缺失/不足契约下限（facts.md ≥500 字节）、
// 核验子任务失败三种情形分开报，都停在问题清单之前。核验只看本步询问的路径
// （回应里可能混有其他派发的条目）。
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
// B1 共用判定：问题清单 null/空回报、在盘缺失/过小（契约 questions.md ≥200
// 字节）、核验子任务失败三种情形分开报，都停在取证之前。核验只看本步询问的
// 路径（回应里可能混有其他派发的条目）。
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
// resume 提示：block 后主线程补数再用原参数 resume，失败支线的子任务会在已补
// 文件的基础上核对完善，所以每条支线都写明“已存在先核对、缺什么补什么”。
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
// B1：contract 把 e{a,b,c,d}.md 都列为必交（全为关键支线）。任一缺失（null、files
// 为空、或核验发现文件不存在/为空）就停在建模之前，出口与上面共用 gateBlock。
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

// 建模串行在最前、仅模型文件缺失或损坏时停止：估值、基准预测与 Street vs House 的数字来自模型，
// 模型文件本身不在或打不开时，后续写作与审稿无法展开。门禁只核对三项：
// 子任务是否产出结果、是否声明了本次的 model/<TICKER>-model-vNN.xlsx（规范化后完整路径相等；
// 别的目录或别的公司不算）、以及声明的文件在盘上存在且可由 openpyxl 正常打开读取（model_readable.py）。
// model_scan.py --recalc 提示性扫描的提示、超时或执行失败，以及 Checks 某行未显示 OK 的原因，
// 均写入 Checks 表与变更记录，工作流继续。
const MODEL_OUT = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    sha256: { type: 'string', minLength: 64 },
    checks_passed: { type: 'boolean' },
  },
  required: ['files', 'summary'],
}
// 模型路径与版本：解析与“另存为下一版”都以磁盘最新版本为准（第 3 轮 #1）。
const modelFileIn = (files) => {
  const found = (Array.isArray(files) ? files : [])
    .map((f) => {
      const m = typeof f === 'string' ? f.match(/-model-v(\d+)\.xlsx$/) : null
      return m ? { path: f, version: Number(m[1]) } : null
    })
    .filter(Boolean)
  return found.length ? found.reduce((a, b) => (b.version > a.version ? b : a)).path : null
}
const nextVersion = (p) =>
  p.replace(/-model-v(\d+)\.xlsx$/, (_, v) => `-model-v${String(Number(v) + 1).padStart(v.length, '0')}.xlsx`)
const resolveFinalModel = async (label, phaseName = '解析最终模型') => {
  const r = await agent(
    `在 workspace 里执行：ls model/${T}-model-v*.xlsx 2>/dev/null | sort -V | tail -1，对输出文件计算 sha256sum。只返回 JSON：path（ls 的原样输出）与 sha256（64 位十六进制）。不写任何文件。`,
    { agentType: 'dalton-evidence', label, phase: phaseName, schema: FINAL_MODEL }
  )
  return r && typeof r.path === 'string' && r.path && shaOf(r) ? { path: normPath(r.path), sha256: r.sha256 } : null
}
phase('建模')
// 只读探针对模型实跑秒级可读性核验（与收尾契约共用 model_readable.py，由 openpyxl 只读打开并核对各工作表）：
// 提示性扫描超时、执行失败或提示项均记入变更记录，工作流继续。门禁和之后实际使用的模型必须是同一个文件，
// 所以解析出磁盘最新版本后，若与建模回报的不同，对最新版本再核验一次。
const modelReadableProbe = async (targetModel, label) => {
  const probe = await agent(
    `只读核验（不写任何文件）。执行 bash：f='${targetModel}'；python3 .agents/skills/dalton-model/scripts/model_readable.py "$f" >/dev/null 2>&1 && echo "$f|readable_ok" || echo "$f|readable_failed"。返回 files 数组，只含这一行原样输出。`,
    { agentType: 'dalton-evidence', label, phase: '建模', schema: PROBE_OUT, checkFiles: false }
  )
  const entry = probe && Array.isArray(probe.files)
    ? probe.files.find((e) => typeof e === 'string' && e.startsWith(`${targetModel}|`))
    : undefined
  if (probe === null || entry === undefined) {
    return gateBlock(
      '模型可读性探针失败，无法确认模型文件可正常打开',
      '模型可读性探针未回报结果；请重试或修复模型后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑'
    )
  }
  if (!entry.endsWith('|readable_ok')) {
    return gateBlock(
      `模型文件不可读或已损坏（只读探针对 ${targetModel} 实跑工作簿可读性检查失败），已停在成稿之前`,
      '模型文件不可读或已损坏，已停在成稿之前；修复模型文件至可正常打开后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用'
    )
  }
  return null
}
let checkedModel = ''
const model = await agent(
  `${base} 先读 .agents/skills/dalton-model/SKILL.md 与 ${S}/references/model-spec.md，结合支线材料 ${R}/e*.md 在 ${W}/model/ 产出模型：按 .agents/skills/dalton-model/SKILL.md 第 1 步选择起点原模型（优先采用用户本次指定或上传至 sources/ 的模型文件，其次取 data/models.json 与 model/ 下的最新版本；均无模型时按 model-spec.md 的 15 页规格新建）。原件与历史版本保持只读，新文件名取 model/${T}-model-v*.xlsx 当前最大版本号加一（尚无该文件时存为 ${W}/model/${T}-model-v01.xlsx；已有版本时另存为下一版，如最大为 v01 则另存为 v02）。在原模型上滚动维护时，按 .agents/skills/dalton-model/references/rollforward.md 沿用其结构、预测方法与格式（先运行 python3 .agents/skills/dalton-model/scripts/model_style.py profile <原模型路径> --out ${R}/style_profile.json 提取格式画像），补齐预测期及 Facts（数字底表镜像）、Guidance vs Actual（12 季指引兑现）、Street vs House（本方与市场预期对比）、Checks（勾稽检查）四张工作表，不新建情景页；尚无模型时按 model-spec.md 的 15 张业务工作表（另在工作簿最前面加 README & Changelog 变更记录工作表）新建，保留 Scenarios（情景分析）页并通过 .agents/skills/dalton-model/scripts/model_style.py 的 apply_cell_style 统一写格式。完成后运行 python3 .agents/skills/xlsx/scripts/recalc.py <新版本路径> 重算缓存值，并运行 python3 .agents/skills/dalton-model/scripts/model_scan.py --recalc <新版本路径> 提示性扫描，将结果写入 Checks 表与变更记录（扫描超时、执行失败、保留不改的提示项或 Checks 未显示 OK 的条目均在变更记录写明原因后继续，不阻断交付）；维护原模型时加跑 python3 .agents/skills/dalton-model/scripts/model_style.py parts-diff <原模型路径> <新版本路径> 核对部件变动；最后运行 python3 .agents/skills/dalton-model/scripts/model_style.py lineage --file ${R}/model_lineage.json --output <新版本路径>（有原模型时加 --baseline <原模型路径>）记录输入输出路径与哈希。新建 15 页模型时返回 Scenarios 三情景的每股价值与关键驱动，在原模型上滚动维护时返回原模型估值区域（如有）的基准估值与关键驱动；若属中断续跑且盘上已有本轮未写完的新版本半成品，确认并非用户原件或已登记历史版本后可在该半成品上续写。`,
  { agentType: 'general-purpose', label: '建模', phase: '建模', schema: MODEL_OUT }
)
{
  if (model === null) {
    return gateBlock(
      '建模失败（建模子任务未产出结果），已停在成稿之前',
      '建模失败，已停在成稿之前；补齐模型后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用'
    )
  }
  if (model.checks_passed === false) {
    log(`建模提示性核对存在未通过项或扫描异常（已记入核对记录/变更记录，工作流继续）：${model.summary || '见模型变更记录'}`)
  }
  // 声明产物里必须有规范化后正好等于 model/<TICKER>-model-vNN.xlsx 的文件：
  // 别的目录（archive/…）、别的公司（AAPL-…）或脚本、报告都不算本次的建模产物（第 3 轮 #3）。
  const tickerLit = String(T).replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const modelPathRe = new RegExp(`^model/${tickerLit}-model-v\\d+\\.xlsx$`)
  const modelDeclared = branchArtifacts(model).filter((p) => modelPathRe.test(normPath(p)))
  if (modelDeclared.length === 0) {
    return gateBlock(
      `建模失败（未声明本次的模型文件 model/${T}-model-vNN.xlsx；其他目录、其他公司的 xlsx 与脚本、报告都不算建模产物），已停在成稿之前`,
      '建模失败，已停在成稿之前；在 model/ 目录产出本次公司的模型文件后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用'
    )
  }
  const modelProbe = await probeBranchFiles(
    modelDeclared.map((p) => [p, 1000]),
    '建模'
  )
  if (modelProbe === null) {
    return gateBlock(
      '支线核验子任务失败，无法确认模型在盘',
      '支线核验子任务失败（无法确认模型文件存在且足 1000 字节），已停在成稿之前；请重试或补数后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑'
    )
  }
  if (!modelProbe.every((e) => e.endsWith('|ok'))) {
    return gateBlock(
      `建模失败（声明的模型产物不存在、为空或不足 1000 字节：${modelDeclared.join('、')}）`,
      '建模失败，已停在成稿之前；补齐模型后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑，label、prompt 哈希与产物哈希一致的已完派发会直接复用'
    )
  }
  checkedModel = modelDeclared[0]
  const readableBlock = await modelReadableProbe(checkedModel, '模型可读性核验')
  if (readableBlock !== null) return readableBlock
}

// 写作、审模型、审稿与“另存为下一版”一律基于磁盘最新模型版本（第 3 轮 #1）：resume 时
// 盘上可能已有上一 run 修订另存的更高版本，仍按旧版本审稿或覆盖既有版本都是错的。
const initialModel = await resolveFinalModel('解析模型（建模后）', '建模')
if (initialModel === null) {
  return gateBlock(
    '解析最新模型版本的子任务失败，已停在成稿之前',
    '解析最新模型版本的子任务未回报结果；请重试后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑'
  )
}
let modelPath = initialModel.path
if (modelPath !== normPath(checkedModel)) {
  const latestBlock = await modelReadableProbe(modelPath, '模型可读性核验（最新版本）')
  if (latestBlock !== null) return latestBlock
  log(`磁盘最新模型版本为 ${modelPath}（高于建模子任务回报的 ${checkedModel}，可能来自上一 run 的修订另存），写作、审稿与修订都按该版本进行`)
}

// 一位写手一次成稿：三位写手分段接力会让各段复述上下文，是 DASH memo 重复的成因之一。
// resume 保护：语言审查、审稿修订或人工补稿都会原位改 drafts/<D>-memo.md，journal 的
// 产物哈希随之失配、“成稿”会重新派发——所以派发前先探测并校验（第 3 轮 #2）：
// 只有结构校验通过、引用的模型路径一致、且 .memo-model.json 记录的路径与哈希与本次最终模型一致的完整稿才直接沿用（缺少该记录按 stale_model 处理）；
// 若模型路径或哈希已变更（stale_model），派写手按新模型更新受影响的预测、估值与引用后再送审；
// 半成品/空文件派写手在已有内容基础上续写补全；没有文件才从头写成。
const memoPath = `${W}/drafts/${D}-memo.md`
const memoModelMeta = `${R}/.memo-model.json`
phase('成稿')
const memoProbe = await agent(
  `只读核验（不写任何文件）。执行 bash：f='${memoPath}'；m='${modelPath}'；msha='${initialModel.sha256}'；meta='${memoModelMeta}'；if ! test -e "$f"; then echo "$f|absent"; elif [ "$(wc -c < "$f")" -ge 500 ] && python ${S}/scripts/validate_memo.py --mode full "$f" >/dev/null 2>&1; then if python3 -c "import json, os, re, sys; f, m, msha, meta = sys.argv[1:5]; txt = open(f, encoding='utf-8').read(); refs = [r.lstrip('./') for r in re.findall(r'model:([^\\s!#]+\\.xlsx)[!#]', txt, re.I)]; ok = all(r == m for r in refs); ok = ok and os.path.exists(meta) and json.load(open(meta, encoding='utf-8')).get('path') == m and json.load(open(meta, encoding='utf-8')).get('sha256') == msha; sys.exit(0 if ok else 1)" "$f" "$m" "$msha" "$meta" >/dev/null 2>&1; then echo "$f|ok|$(sha256sum "$f" | cut -d' ' -f1)"; else echo "$f|stale_model"; fi; else echo "$f|incomplete"; fi。返回 files 数组，只含这一行原样输出。`,
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
      `${base} 通读 ${R}/questions.md（12 问研究提纲）、${R}/e*.md（含文末逐题证据小结）、${R}/facts.md 与本次最终模型 ${modelPath}（sha256：${initialModel.sha256}）的输出（新建 15 页模型时读取 Scenarios〔情景分析〕、Street vs House〔本方与市场预期对比〕、Valuation Summary〔估值汇总〕；在原模型上滚动维护时读取 Street vs House 与原模型估值区域，原模型未设情景页时备忘录三情景从原模型估值区域或分析师假设取数，并在正文用文字说明情景假设及模型里没有对应计算），同步按支线证据更新 ${R}/questions.md 每问的 A / B / C / D 自评档位（只写在底稿、不进备忘录正文），并按 ${S}/references/memo-writing.md（章节顺序：摘要 → 公司与商业模式 → 行业与竞争〔可并入上一节〕→ 业务质量 → 核心投资逻辑与争议 → 财务预测与估值 → 风险 → 管理层 → 跟踪 → 附录 → 引用）完成备忘录 ${memoPath}${
        memoStaleModel
          ? `。盘上已有 ${memoPath}（结构完整），但引用的模型路径或哈希与本次最终模型 ${modelPath}（sha256：${initialModel.sha256}）不一致：先读已有备忘录，保留未受影响的章节与论证，对照本次最终模型 ${modelPath} 更新受影响的预测、估值、表格与全部 model: 引用`
          : memoPartial
            ? `。盘上已有 ${memoPath} 的半成品（结构校验未通过）：先读已有内容，保留其中可用的部分，补齐缺失的章节与引用，修到结构完整；不要推倒重写已有内容`
            : ''
      }。写给基金经理看：每件事只写一遍、12 问不进正文、计算过程放附录；信息缺口写它对判断的影响，不罗列“未披露”；成稿后通读全文，同一缺口、同一限定、同一改判条件只保留最合适的一处。随后把本次最终模型信息写入 ${memoModelMeta}（JSON：{"path": "${modelPath}", "sha256": "${initialModel.sha256}"}），跑 python ${S}/scripts/validate_memo.py --mode full ${memoPath} --research-dir ${R} 到 0 FAIL，并返回 ${memoPath} 的 sha256。`,
      { agentType: 'general-purpose', label: '成稿', phase: '成稿', schema: DRAFT_OUT }
    )
const memoSha = shaOf(memo)
const memoOk = memo !== null && memoSha !== null

// 第 1 轮语言审查：成稿之后、第一次独立审稿之前（改稿会换哈希，必须先改再送审）；
// 第 2 轮在修订循环结束后的终稿上（见下方“语言审查 2”），两轮与 check_done 契约一致。
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

phase('审模型与审稿')
const [mcheck, reviewRaw] = await parallel([
  () =>
    agent(
      `按 .agents/skills/dalton-model/SKILL.md 与 .agents/skills/dalton-model/references/sanity-check.md 对 ${modelPath} 做只读核查（原件只读）：运行 python3 .agents/skills/dalton-model/scripts/model_scan.py --recalc ${modelPath} 做提示性扫描（若扫描超时或执行失败，如实记录状态并直接基于工作簿完成核查），结合公司财年、指标口径、三表勾稽与市场一致预期逐条复核，只输出核查报告至 ${R}/model-check.md。`,
      { agentType: 'general-purpose', label: '只读审模型', phase: '审模型与审稿', schema: OUT }
    ),
  () =>
    !memoOk
      ? Promise.resolve(null)
      : agent(
          `审阅 ${memoPath}（被审草稿 sha256：${
            reviewedMemoSha || '未知'
          }；问题提纲 ${R}/questions.md，支线 ${R}/e*.md，底表 ${R}/facts.md，模型 ${modelPath}，全部只读，不修改任何文件）。先只凭摘要复述投资论点（立场、赔率、改判条件），并对 ${R}/questions.md 中自评为 C、D 档的问题重点核对，再按章节顺序（摘要 → 公司与商业模式 → 行业与竞争〔可并入上一节〕→ 业务质量 → 核心投资逻辑与争议 → 财务预测与估值 → 风险 → 管理层 → 跟踪 → 附录 → 引用）逐节检查：摘要（含投委会四问）、公司与商业模式、行业与竞争、业务质量（Good Business〔生意质量〕/ Good People〔管理与组织质量〕/ Good Price〔价格与赔率〕三项判断是否各自独立且有证据支撑，综合判断评级表与方向和行动是否一致）、核心投资逻辑与争议、财务预测与估值（估值三角、赔率卡与三层保护）、风险（含一年后亏损 30% 的事前推演是否写出了空方最强版本）、管理层、跟踪、附录、引用。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；items 逐项覆盖 1–9 项检查）。`,
          { agentType: 'dalton-reviewer', label: '独立审稿', phase: '审模型与审稿', schema: VERDICT, reviewDraft: memoPath }
        ),
])

const incomplete = []
// 审稿结果未覆盖 1–9 项检查时按审稿失败处理（与发布审稿同一契约，fail-closed）。
let review = reviewRaw
if (review !== null && !completeReview(review)) {
  incomplete.push('审稿结果未覆盖 1–9 项检查，按审稿失败处理')
  review = null
}
let fix = null
let finalReview = review
let rounds = 0
let currentMemoSha = reviewedMemoSha
// 提前结束时解析一次磁盘上的模型，作为返回值里的 model（建模门禁保证 model 已在盘）。
const resolveOnExit = async () => {
  const r = await resolveFinalModel('解析模型（终止）')
  if (r === null) incomplete.push('解析最终模型的子任务失败')
  return r
}
// 所有提前返回的出口都经过 finish：返回值里的 model 只取传进来的解析结果。
const finish = (resolved, extra = {}) => ({
  ok: false,
  memo: memoPath,
  model: resolved ? resolved.path : null,
  model_check: mcheck && `${R}/model-check.md`,
  revise_rounds: rounds,
  incomplete,
  ...extra,
})

if (review && verdictOf(review) === 'block') {
  incomplete.push(`审稿 block：${review.summary || '存在不可修订级问题'}`)
  return finish(await resolveOnExit(), {
    blocked: review.summary || '审稿 block',
    // 人工处理后两条路：resume 继续跑后续阶段（盘上 memo 与模型不会被重写——
    // 成稿前有 memo 探测、模型按磁盘最新版本识别），或按 SKILL 手动路径收尾。
    resume_hint:
      '审稿 block：先人工处理（按 must_fix 修订 memo 与模型、补证据），再选择：① 需要重走审稿/修订/收尾的，用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑（支线、底表、建模等未变产物直接复用；盘上已有的 memo 与模型不会被重写，流程沿用最新稿）；② 只需人工收尾的，按 dalton-full-coverage SKILL 手动降级路径执行，并以 manual=true 调用 dalton_check_done',
  })
}

phase('修订')
// revise → 修订 → 解析模型 → 复审，仅 1 轮（写作流程第 2 批：一轮修订）；
// 复审仍是 revise 时按“审稿未最终通过”收场，不进第二轮；每个失败出口都写进 incomplete 并停下
let resolved = null
while (finalReview && verdictOf(finalReview) === 'revise' && rounds < 1) {
  rounds += 1
  const target = nextVersion(modelPath)
  const modelClause = `模型若有改动，按 .agents/skills/dalton-model/SKILL.md 与 .agents/skills/dalton-model/references/rollforward.md 保持原件只读，另存为 ${target}，运行 python3 .agents/skills/xlsx/scripts/recalc.py ${target} 重算与 python3 .agents/skills/dalton-model/scripts/model_scan.py --recalc ${target} 提示性扫描并逐条核对（属问题的修正并重算；保留项、扫描超时或执行失败随本次改动一并写入变更记录〔README & Changelog 或 Change Log 工作表〕后继续），再运行 python3 .agents/skills/dalton-model/scripts/model_style.py lineage --file ${R}/model_lineage.json --output ${target} --baseline ${modelPath} 记录输入输出路径与哈希；模型未改则原样沿用 ${modelPath}。无论是否修改，均须在 files 里回报当前模型路径（另存报 ${target}，未改报 ${modelPath}）`
  fix = await agent(
    `${base} 先核对 ${memoPath} 的 sha256 是否与审稿前（${
      currentMemoSha || '当前记录值'
    }）一致；按审稿意见与 ${R}/model-check.md 做第 ${rounds} 轮修订（memo 与模型；${modelClause}）。P0 全改，修订后通读全文，同一缺口、同一限定、同一改判条件只保留最合适的一处；跑 python ${S}/scripts/validate_memo.py --mode full ${memoPath} --research-dir ${R} 到 0 FAIL。审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'general-purpose', label: `修订 ${rounds}`, phase: '修订', schema: DRAFT_OUT }
  )
  const fixedMemoSha = shaOf(fix)
  if (fixedMemoSha === null) {
    incomplete.push(`第 ${rounds} 轮修订失败（修订结果或其 sha256 缺失/非法），未复审，流程终止`)
    return finish(await resolveOnExit())
  }
  {
    const reportedPath = modelFileIn(fix.files)
    if (!reportedPath) {
      incomplete.push(`第 ${rounds} 轮修订未回报模型路径（模型未改也须回报 ${modelPath}），未复审，流程终止`)
      return finish(await resolveOnExit())
    }
    const disk = await resolveFinalModel(`解析模型（修订 ${rounds}）`)
    if (disk === null) {
      incomplete.push(`第 ${rounds} 轮修订后解析模型的子任务失败，未复审，流程终止`)
      return finish(null)
    }
    if (normPath(reportedPath) !== disk.path) {
      incomplete.push(
        `第 ${rounds} 轮修订后模型版本不一致：回报 ${reportedPath}，磁盘最大版本 ${disk.path}，最终模型不确定，未复审，流程终止`
      )
      return finish(disk)
    }
    modelPath = disk.path
    resolved = disk
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
    return finish(resolved)
  }
  if (!completeReview(reReview)) {
    incomplete.push(`第 ${rounds} 轮复审结果未覆盖 1–9 项检查，按复审失败处理，流程终止`)
    return finish(resolved)
  }
  if (verdictOf(reReview) === 'block') {
    incomplete.push(`复审 block：${reReview.summary || '存在不可修订级问题'}，流程终止`)
    return finish(resolved, {
      blocked: reReview.summary || '复审 block',
      resume_hint: RESUME_HINT,
    })
  }
  finalReview = reReview
}
let reviewPassed = finalReview !== null && verdictOf(finalReview) === 'pass'

// 笔记两轮：一次写完、不再改的支线/底稿笔记同样审两轮——第 1 轮审完按清单改，
// 第 2 轮复核（改动过的文件再补一次 round=2 确认，绑定终态哈希）。model-check.md
// 与首次审稿并行产出，其两轮也在这里补齐。
const notesReview =
  !memoOk || !reviewPassed
    ? null
    : await agent(
        `对本次运行产出、给人读的 .md 笔记逐一做两轮语言审查（规则见 .agents/skills/dalton-language/SKILL.md，只标问题）。每个文件依次：1) 调用工具 check_language(draft_path="<文件>", round=1)，读 <文件>.language.md 清单并按清单修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）；2) 调用 check_language(draft_path="<文件>", round=2) 复核；若第 1 轮后改过稿，改完再调用一次 round=2 确认（记录追加在同一 json）。\n文件清单：\n- ${R}/facts.md\n- ${R}/questions.md\n- ${R}/ea.md\n- ${R}/eb.md\n- ${R}/ec.md\n- ${R}/ed.md\n- ${R}/model-check.md\n本次运行清单之外新写的其他给人读的 .md 一并同法处理；只审查以上文件，不审 json/xlsx 等机器文件与 .audit/.annotated/.language 侧车。返回处理过的文件列表与摘要。`,
        { agentType: 'general-purpose', label: '笔记语言审查', phase: '修订', schema: OUT }
      )
if (memoOk && reviewPassed && notesReview === null) {
  incomplete.push('笔记语言审查失败（支线/底稿笔记未完成两轮语言审查）')
}

// 第 2 轮语言审查：终稿内容定稿（审稿修订结束）之后。改了稿就再跑一次 round=2 复核，
// 使最后一条第 2 轮记录绑定终稿哈希；哈希有变则终稿审稿重新绑定——语言修改不能让已有
// pass 记录失效却无人重审。两轮语言审查与 check_done 的契约（rounds 1+2、末轮哈希=终稿）一致。
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

// 循环结束后再确认一次磁盘上的最终模型：本 run 内做过修订的（rounds > 0），磁盘最大
// 版本必须就是修订确认的 modelPath（回报与盘不符 = 版本不确定）；本 run 未修订的
// （rounds === 0，含 resume：上一 run 修订另存的 v02+ 就在盘上），按磁盘最新版本沿用。
phase('解析最终模型')
const finalModel = await resolveFinalModel('解析最终模型')
if (finalModel === null) {
  incomplete.push('解析最终模型的子任务失败，流程终止')
  return finish(null)
}
if (rounds > 0 && finalModel.path !== normPath(modelPath)) {
  incomplete.push(
    `最终模型与修订确认的版本（${modelPath}）不一致：磁盘最大版本 ${finalModel.path}，最终模型不确定，流程终止`
  )
  return finish(finalModel)
}
if (rounds === 0 && finalModel.path !== normPath(modelPath)) {
  log(`本 run 未修订模型，磁盘最新版本为 ${finalModel.path}（可能来自上一 run 的修订另存），按最新版本沿用`)
}
modelPath = finalModel.path

const kp =
  !memoOk
    ? null
    : await agent(
        `${base} 先核对 ${memoPath} 的 sha256 是否为修订后哈希（${currentMemoSha || '修订后记录值'}）；最终模型为 ${finalModel.path}；再对比 ${W} 根目录现有知识层，按 PK.2 的 JSON Schema 写知识层修订草案 ${R}/knowledge-revision.json（结构为 {doc: {ops, reason, evidence_refs}}）：每处改动给旧值、新值、原因、证据（引用编号）。管理层指引风格与 12 季指引兑现历史（ec.md 第 C4 节）写入 management 部分，models 部分按 .agents/skills/dalton-model/SKILL.md 第 7 步登记最终模型版本与关键输出单元格；不直接修改 ${W}/data/。`,
        { agentType: 'general-purpose', label: '知识层修订草案', phase: '修订', schema: OUT }
      )

// facts / frame / 支线 / 建模的缺失已在前置门禁返回，这里只收写作与审稿阶段的失败。
if (!memoOk) incomplete.push('memo 成稿失败（草稿或其 sha256 缺失/非法）')
if (lang1Failed) incomplete.push('语言审查第 1 轮失败（结果或其 sha256 缺失/非法），未按清单修改')
if (mcheck === null) incomplete.push('只读审模型失败')
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
  finalModel.path,
  memoPath,
  `${memoPath}.language.json`,
  `${R}/model-check.md`,
  `${R}/model-check.md.language.json`,
  `${R}/knowledge-revision.json`,
]
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
// The tracking hint follows the run's own completion verdict exactly: an
// unfinished run must not tell the analyst to switch tracking on.
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
  model: finalModel.path,
  review_done: finalReview !== null,
  review_verdict: finalReview && verdictOf(finalReview),
  language_done: memoOk && lang1Sha !== null && lang2Sha !== null,
  revise_rounds: rounds,
  fix_summary: fix && fix.summary,
  model_check: mcheck && `${R}/model-check.md`,
  knowledge_revision: kp && `${R}/knowledge-revision.json`,
  evidence_missing: evMissing,
  tracking,
  incomplete,
}
