export const meta = {
  name: 'full-coverage',
  description:
    'Dalton 完整覆盖：一手材料与数字底表 → frame → A∥B∥C∥D 取证 → 建模∥memo → check-model∥独立审稿 → 修订 → 知识层修订草案',
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
const base = `先读 .agents/skills/dalton-conventions/SKILL.md 与 ${S}/SKILL.md；公司 ${C || T}（${T}），市场 ${args.market || 'US'}，截至 ${D}；数字底表 ${R}/facts.md（只引用底表，缺了写“底表缺 X”）。${pmSection}`
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
const probeBranchFiles = async (paths, phaseName) => {
  const entries = paths.map((p) => (Array.isArray(p) ? p : [p, 1]))
  const r = await agent(
    `只读核验以下支线产物文件是否存在、非空白且达到字节数下限（不写任何文件）。逐个执行 bash：f='<路径>'；n=<字节数下限>；test -e "$f" && [ "$(wc -c < "$f")" -ge "$n" ] && [ -n "$(tr -d '[:space:]' < "$f")" ] && echo "$f|ok" || echo "$f|missing"。返回 files 数组：每项为上述一行的原样输出（<路径>|ok 或 <路径>|missing）：\n${entries
      .map(([p, min]) => `- ${p}（至少 ${min} 字节）`)
      .join('\n')}`,
    { agentType: 'dalton-evidence', label: '支线核验', phase: phaseName, schema: PROBE_OUT }
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
  `${base} 按 ${S}/references/frame.md 写 ${R}/questions.md。PM 要求：${args.pm_brief || '无'}；重点问题：${
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

phase('建模与 memo 第一段')
const [model, memo1] = await parallel([
  () =>
    agent(
      `${base} 按 ${S}/references/model-spec.md 建模型 ${W}/model/${T}-model-v01.xlsx（三表 + DCF + comps + Facts/Guidance vs Actual/Street vs House 三页），支线材料 ${R}/e*.md。recalc 与 audit --strict 必须通过，Checks 全 OK。返回 Scenarios 三情景的每股价值与关键驱动。`,
      { agentType: 'general-purpose', label: '建模', phase: '建模与 memo 第一段', schema: OUT }
    ),
  () =>
    agent(
      `${base} 按 ${S}/references/memo-writing.md 写 memo 第一段（基本信息、核心判断、零、一、二）到 ${W}/drafts/${D}-memo.md，材料 ${R}/。缺失支线：${
        evMissing.join(',') || '无'
      }。跑 python ${S}/scripts/validate_memo.py --part 1 ${W}/drafts/${D}-memo.md 到 0 FAIL。`,
      { agentType: 'general-purpose', label: 'memo 第一段', phase: '建模与 memo 第一段', schema: OUT }
    ),
])

const memo2 =
  memo1 === null
    ? null
    : await agent(
        `${base} 续写 ${W}/drafts/${D}-memo.md 第二段（三至六），第六章情景与敏感度引用模型 ${W}/model/${T}-model-v01.xlsx${
          model === null ? '（模型失败：第六章写缺口）' : ''
        }。跑 python ${S}/scripts/validate_memo.py --part 2 ${W}/drafts/${D}-memo.md 到 0 FAIL。`,
        { agentType: 'general-purpose', label: 'memo 第二段', phase: '建模与 memo 第一段', schema: OUT }
      )

const memo3 =
  memo2 === null
    ? null
    : await agent(
        `${base} 续写 ${W}/drafts/${D}-memo.md 第三段（七至十、附录、引用清单），先通读前两段保持文风与引用编号连贯；第七章用 C4 指引可信度表，第九章估值与模型一致。跑 python ${S}/scripts/validate_memo.py --mode full ${W}/drafts/${D}-memo.md 到 0 FAIL，并返回 ${W}/drafts/${D}-memo.md 的 sha256。`,
        { agentType: 'general-purpose', label: 'memo 第三段', phase: '建模与 memo 第一段', schema: DRAFT_OUT }
      )

const memoSha = memo3 === null ? null : shaOf(memo3)
const memoOk = memo3 !== null && memoSha !== null

// 第 1 轮语言审查：memo 三段写完之后、第一次独立审稿之前（改稿会换哈希，必须先改再送审）。
const memoPath = `${W}/drafts/${D}-memo.md`
phase('语言审查')
const lang1 =
  !memoOk
    ? null
    : await agent(
        `对 ${memoPath} 做第 1 轮语言审查：调用工具 check_language(draft_path="${memoPath}", round=1)（只标问题），读 ${memoPath}.language.md 清单，按清单逐条修改 ${memoPath}（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。改完跑 python ${S}/scripts/validate_memo.py --mode full ${memoPath} 确认 0 FAIL，并返回 ${memoPath} 的 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 1', phase: '语言审查', schema: DRAFT_OUT }
      )
const lang1Sha = lang1 === null ? null : shaOf(lang1)
const lang1Failed = memoOk && lang1Sha === null
const reviewedMemoSha = lang1Sha || memoSha

phase('审模型与审稿')
const [mcheck, review] = await parallel([
  () =>
    model === null
      ? Promise.resolve(null)
      : agent(
          `按 .agents/skills/check-model/SKILL.md 审 ${W}/model/${T}-model-v01.xlsx，只出报告不改模型，报告写 ${R}/model-check.md。`,
          { agentType: 'general-purpose', label: 'check-model', phase: '审模型与审稿', schema: OUT }
        ),
  () =>
    memo3 === null
      ? Promise.resolve(null)
      : agent(
          `审阅 ${memoPath}（被审草稿 sha256：${
            reviewedMemoSha || '未知'
          }；支线 ${R}/e*.md，底表 ${R}/facts.md，模型 ${W}/model/${T}-model-v01.xlsx，全部只读，不修改任何文件）。先只凭核心判断和零章复述投资论点，再逐题检查，重点 Q1–Q5、Q7、Q8、Q12。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
          { agentType: 'dalton-reviewer', label: '独立审稿', phase: '审模型与审稿', schema: VERDICT }
        ),
])

// 修订循环与收尾只认 resolveFinalModel()（确定性子任务：ls … | sort -V | tail -1）
// 解析出的磁盘路径；每轮修订后、复审前都解析一次，并与修订回报的路径比对。
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
const resolveFinalModel = async (label) => {
  const r = await agent(
    `在 workspace 里执行：ls model/${T}-model-v*.xlsx 2>/dev/null | sort -V | tail -1，对输出文件计算 sha256sum。只返回 JSON：path（ls 的原样输出）与 sha256（64 位十六进制）。不写任何文件。`,
    { agentType: 'dalton-evidence', label, phase: '解析最终模型', schema: FINAL_MODEL }
  )
  return r && typeof r.path === 'string' && r.path && shaOf(r) ? { path: normPath(r.path), sha256: r.sha256 } : null
}

let modelPath = `${W}/model/${T}-model-v01.xlsx`
const incomplete = []
let fix = null
let finalReview = review
let rounds = 0
let currentMemoSha = reviewedMemoSha
// 提前结束时解析一次磁盘上的模型，作为返回值里的 model；建模失败则没有模型。
const resolveOnExit = async () => {
  if (model === null) return null
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
    resume_hint:
      '修订 memo/模型后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发（支线、底表等）会直接复用，成稿及之后的阶段重新派发',
  })
}

phase('修订')
// revise → 修订 → 解析模型 → 复审，最多 2 轮；每个失败出口都写进 incomplete 并停下
let resolved = null
while (finalReview && verdictOf(finalReview) === 'revise' && rounds < 2) {
  rounds += 1
  const target = nextVersion(modelPath)
  const modelClause =
    model === null
      ? '建模失败，本轮只改 memo'
      : `模型若有改动走 .agents/skills/model-update/SKILL.md，另存为 ${target}，不覆盖旧版本；模型没改就原样沿用 ${modelPath}。不管改没改，都必须在 files 里回报当前模型的路径（另存则报 ${target}，未改则报 ${modelPath}）`
  fix = await agent(
    `${base} 先核对 ${memoPath} 的 sha256 是否与审稿前（${
      currentMemoSha || '当前记录值'
    }）一致；按审稿意见与 ${R}/model-check.md 做第 ${rounds} 轮修订（memo 与模型；${modelClause}）。P0 全改，跑 python ${S}/scripts/validate_memo.py --mode full ${memoPath} 到 0 FAIL。审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'general-purpose', label: `修订 ${rounds}`, phase: '修订', schema: DRAFT_OUT }
  )
  const fixedMemoSha = shaOf(fix)
  if (fixedMemoSha === null) {
    incomplete.push(`第 ${rounds} 轮修订失败（修订结果或其 sha256 缺失/非法），未复审，流程终止`)
    return finish(await resolveOnExit())
  }
  if (model !== null) {
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
    }；模型 ${model === null ? '建模失败，无模型' : modelPath}；${W}/model/*.xlsx 只读不改）。重点核对上一轮 must_fix 是否全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。上一轮审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'dalton-reviewer', label: `复审 ${rounds}`, phase: '修订', schema: VERDICT }
  )
  if (reReview === null) {
    incomplete.push(`第 ${rounds} 轮复审失败，流程终止`)
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
        `对本次运行产出、给人读的 .md 笔记逐一做两轮语言审查（规则见 .agents/skills/dalton-language/SKILL.md，只标问题）。每个文件依次：1) 调用工具 check_language(draft_path="<文件>", round=1)，读 <文件>.language.md 清单并按清单修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）；2) 调用 check_language(draft_path="<文件>", round=2) 复核；若第 1 轮后改过稿，改完再调用一次 round=2 确认（记录追加在同一 json）。\n文件清单：\n- ${R}/facts.md\n- ${R}/questions.md\n- ${R}/ea.md\n- ${R}/eb.md\n- ${R}/ec.md\n- ${R}/ed.md\n- ${R}/model-check.md\n清单之外本次运行写的其他给人读的 .md 一并同法处理；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/ 下的文件、知识层渲染文件与 .audit/.annotated/.language 侧车不审。返回处理过的文件列表与摘要。`,
        { agentType: 'general-purpose', label: '笔记语言审查', phase: '修订', schema: OUT }
      )
if (memoOk && reviewPassed && notesReview === null) {
  incomplete.push('笔记语言审查失败（支线/底稿笔记未完成两轮语言审查）')
}

// 第 2 轮语言审查：终稿内容定稿（审稿修订循环结束）之后、提交最后一次审稿之前。
// 改了稿就再跑一次 round=2 复核，使最后一条第 2 轮记录绑定终稿哈希；哈希有变
// 则终稿审稿重新绑定独立审稿哈希——语言修改不能让已有 pass 记录失效却无人重审。
const preLang2Sha = currentMemoSha
const lang2 =
  !memoOk || !reviewPassed
    ? null
    : await agent(
        `对 ${memoPath} 做第 2 轮语言审查：调用工具 check_language(draft_path="${memoPath}", round=2)，读 ${memoPath}.language.md 清单，按清单逐条完成终稿语言修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。若草稿有改动，再调用一次 check_language(draft_path="${memoPath}", round=2) 对终稿复核（记录追加在同一 json），并跑 python ${S}/scripts/validate_memo.py --mode full ${memoPath} 确认 0 FAIL。返回 ${memoPath} 最终 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 2', phase: '修订', schema: DRAFT_OUT }
      )
const lang2Sha = lang2 === null ? null : shaOf(lang2)
if (memoOk && reviewPassed && lang2Sha === null) {
  incomplete.push('语言审查第 2 轮失败（结果或其 sha256 缺失/非法），未做终稿语言修改')
  reviewPassed = false
} else if (memoOk && reviewPassed && lang2Sha !== preLang2Sha) {
  currentMemoSha = lang2Sha
  const finalCheck = await agent(
    `终稿审稿（语言修改后）：审阅 ${memoPath}（被审草稿 sha256：${lang2Sha}；模型 ${model === null ? '建模失败，无模型' : modelPath}；${W}/model/*.xlsx 只读不改）。重点：语言修改未动事实与数字，上一轮 must_fix 仍全部解决，validate_memo 仍 0 FAIL。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
    { agentType: 'dalton-reviewer', label: '终稿审稿', phase: '修订', schema: VERDICT }
  )
  if (finalCheck === null) {
    incomplete.push('终稿审稿失败')
    reviewPassed = false
  } else {
    finalReview = finalCheck
    reviewPassed = verdictOf(finalCheck) === 'pass'
  }
}

// 循环结束后再确认一次：磁盘最大版本必须就是循环里最后确认的 modelPath。
// 之后的知识草案、核验清单、返回值都只用 finalModel.path。
phase('解析最终模型')
let finalModel = null
if (model !== null) {
  finalModel = await resolveFinalModel('解析最终模型')
  if (finalModel === null) {
    incomplete.push('解析最终模型的子任务失败，流程终止')
    return finish(null)
  }
  if (finalModel.path !== normPath(modelPath)) {
    incomplete.push(
      `最终模型与修订确认的版本（${modelPath}）不一致：磁盘最大版本 ${finalModel.path}，最终模型不确定，流程终止`
    )
    return finish(finalModel)
  }
  modelPath = finalModel.path
}

const kp =
  memo3 === null
    ? null
    : await agent(
        `${base} 先核对 ${memoPath} 的 sha256 是否为修订后哈希（${currentMemoSha || '修订后记录值'}）；最终模型为 ${finalModel ? finalModel.path : '无（建模失败）'}；再对比 ${W} 根目录现有知识层，按 PK.2 的 JSON Schema 写知识层修订草案 ${R}/knowledge-revision.json（结构为 {doc: {ops, reason, evidence_refs}}）：每处改动给旧值、新值、原因、证据（引用编号）。管理层指引风格与 C4 历史写入 management 部分；不直接修改 ${W}/data/。`,
        { agentType: 'general-purpose', label: '知识层修订草案', phase: '修订', schema: OUT }
      )

if (frame === null) incomplete.push('问题清单 frame 失败')
if (evMissing.length) incomplete.push(`支线失败：${evMissing.join('、')}`)
if (model === null) incomplete.push('建模失败')
if (memo1 === null || memo2 === null || !memoOk) incomplete.push('memo 三段写作失败（末段草稿或其 sha256 缺失/非法）')
if (lang1Failed) incomplete.push('语言审查第 1 轮失败（结果或其 sha256 缺失/非法），未按清单修改')
if (mcheck === null && model !== null) incomplete.push('check-model 失败')
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
  // No model when the build failed: never list an assumed v01 (the run is
  // already incomplete for it).
  ...(finalModel ? [finalModel.path] : []),
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
  model: finalModel ? finalModel.path : null,
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
