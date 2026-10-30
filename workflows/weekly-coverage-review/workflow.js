export const meta = {
  name: 'weekly-coverage-review',
  description: 'Dalton 周会：全名单筛选 → Core 逐家并行五源 → Watch 命中定向检索 → EPS vs 倍数归因 → 观点变化汇总 → call',
}

const { home_dir: K, end: E, coverage_path: L } = args
const R = `${K}/research/weekly-${E}`
const S = '.agents/skills/dalton-weekly-review'
const base = '先读 .agents/skills/dalton-conventions/SKILL.md 与 ' + S + '/SKILL.md。'

// Note: schema_max_depth is 5 (root=1, properties=2, field=3, items=4).
// Element object keys ({ticker, workspace_dir, held} for core; {ticker, event_hint, workspace_dir} for flagged)
// are enforced via the prompt contract while keeping schema nesting depth <= 4.
const SCREEN = {
  type: 'object',
  properties: {
    ok: { type: 'boolean' },
    base: { type: 'string' },
    core: { type: 'array', items: { type: 'object' } },
    flagged: { type: 'array', items: { type: 'object' } },
    warnings: { type: 'array', items: { type: 'string' } },
  },
  required: ['ok', 'core', 'flagged'],
}

const CO = {
  type: 'object',
  properties: {
    ticker: { type: 'string' },
    file: { type: 'string' },
    move_pp: { type: 'number' },
    excess_pp: { type: 'number' },
    driver: { type: 'string' },
    thesis_impact: { type: 'string' },
    verdict: { type: 'string' },
    call_candidate: { type: 'boolean' },
  },
  required: ['ticker', 'file'],
}

const HASH = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
  },
  required: ['files'],
}

// B1：支线产物缺失判定（各 workflow 同一实现；workflow 之间没有共用 js 模块的
// 机制，改动需同步全部 workflow.js）。子任务返回 null、或 files/file 为空，都算
// 该家失败。周会是全名单扫描，单家缺失不 block 整个 run：按既有口径写进 incomplete
// 与周会稿的“口径与缺口”，产物核验阶段仍会点名缺失文件。
const branchArtifacts = (v) => {
  if (v === null || typeof v !== 'object') return []
  const out = []
  if (typeof v.file === 'string' && v.file.trim()) out.push(v.file.split('|', 1)[0].trim())
  for (const f of Array.isArray(v.files) ? v.files : []) {
    if (typeof f === 'string' && f.trim()) out.push(f.split('|', 1)[0].trim())
  }
  return out.filter(Boolean)
}

phase('筛选与归因')
const scr = await agent(
  `${base} 用覆盖名单 ${L}，窗口终点 ${E}：跑 python ${S}/scripts/screen_movers.py 与 python ${S}/scripts/attribution.py，结果写 ${R}/screen.json、${R}/attribution.json。返回 JSON：ok（boolean）、base（窗口起点日期）、core（数组，每项含 ticker, workspace_dir, held）、flagged（Watch 命中数组，每项含 ticker, event_hint, workspace_dir）、warnings（字符串数组）。`,
  { agentType: 'general-purpose', label: '筛选', phase: '筛选与归因', schema: SCREEN }
)
if (scr === null || !scr.ok) {
  return {
    ok: false,
    reason: '筛选失败或数据未更新，不下结论',
    warnings: scr && scr.warnings,
    incomplete: ['筛选失败或数据未更新（screen.json 未就绪），本周无法核验产物'],
  }
}
const core = [...scr.core].sort((a, b) => (b.held ? 1 : 0) - (a.held ? 1 : 0))
// 平台上限：每次运行最多派 64 个子智能体（config.yaml 的 workflow 段）；扣掉筛选 1、
// 支线核验 1、call 3、周会稿 1、产物核验 1
const room = Math.max(0, 64 - 7 - core.length)
const watch = scr.flagged.slice(0, room)
if (scr.flagged.length > room) {
  log(
    `Watch 命中 ${scr.flagged.length} 家，超出平台派发上限，余下 ${
      scr.flagged.length - room
    } 家写进“口径与缺口”，下次优先`
  )
}

phase('Core 逐家')
const coreRes = await parallel(
  core.map((c) => () =>
    agent(
      `${base} 公司 ${c.ticker}${c.held ? '（持仓）' : ''}，知识层在 ${c.workspace_dir || `../${c.ticker}`}。按 ${S}/references/core-five-sources.md 做本周（${scr.base} 至 ${E}）五源检索，归因数据见 ${R}/screen.json 与 ${R}/attribution.json。写 ${R}/core-${c.ticker}.md；若有知识层修改建议，写提案 ${R}/proposals/${c.ticker}.json（不直接改公司工作区知识层）。`,
      { agentType: 'dalton-evidence', label: String(c.ticker), phase: 'Core 逐家', schema: CO }
    )
  )
)

phase('Watch 命中')
const watchRes = await parallel(
  watch.map((w) => () =>
    agent(
      `${base} Watch 公司 ${w.ticker}（提示：${w.event_hint || '异动命中'}）。按 ${S}/references/watch-rules.md 定向检索，写 ${R}/watch-${w.ticker}.md。`,
      { agentType: 'dalton-evidence', label: String(w.ticker), phase: 'Watch 命中', schema: CO }
    )
  )
)

// B1 共用判定（在盘核验）：单家文件 core-<T>.md / watch-<T>.md 在盘缺失或不足
// 契约下限（100 字节）计入失败；核验子任务本身失败不 block 整个名单，只记一条
// incomplete 并按子任务回报判断。单家失败不派 call。
phase('汇总与 call')
const PROBE_OUT = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
  },
  required: ['files'],
}
const probeBranchFiles = async (paths) => {
  const entries = paths.map((p) => (Array.isArray(p) ? p : [p, 1]))
  const r = await agent(
    `只读核验以下支线产物文件是否存在、非空白且达到字节数下限（不写任何文件）。逐个执行 bash：f='<路径>'；n=<字节数下限>；test -e "$f" && [ "$(wc -c < "$f")" -ge "$n" ] && [ -n "$(tr -d '[:space:]' < "$f")" ] && echo "$f|ok" || echo "$f|missing"。返回 files 数组：每项为上述一行的原样输出（<路径>|ok 或 <路径>|missing）：\n${entries
      .map(([p, min]) => `- ${p}（至少 ${min} 字节）`)
      .join('\n')}`,
    { agentType: 'dalton-evidence', label: '支线核验', phase: '汇总与 call', schema: PROBE_OUT, checkFiles: false }
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
const corePath = (t) => `${R}/core-${t}.md`
const watchPath = (t) => `${R}/watch-${t}.md`
const incomplete = []
const probeTargets = [
  ...core.filter((_, i) => branchArtifacts(coreRes[i]).length > 0).map((c) => [corePath(c.ticker), 100]),
  ...watch.filter((_, i) => branchArtifacts(watchRes[i]).length > 0).map((w) => [watchPath(w.ticker), 100]),
]
const offDisk = new Set()
let probeFailed = false
if (probeTargets.length) {
  // 派发本身抛错（如到达平台上限）与核验失败同路处理，不让 run 变 script_error。
  let probe = null
  try {
    probe = await probeBranchFiles(probeTargets)
  } catch (e) {
    log(`支线核验派发失败：${e}`)
    probe = null
  }
  if (probe === null) {
    // 核验失败：公司仍按子任务回报计入名单汇总，但未核实的公司本轮不派
    // call，并在返回值里标出（unverified_by_probe）。
    probeFailed = true
    incomplete.push('支线核验子任务失败（单家文件在盘状态未核实，未核实的公司本轮不派 call）')
  } else {
    for (const entry of probe) {
      if (entry.endsWith('|missing')) offDisk.add(entry.slice(0, -'|missing'.length))
    }
  }
}
const coreOk = (i) => branchArtifacts(coreRes[i]).length > 0 && !offDisk.has(corePath(core[i].ticker))
const watchOk = (i) => branchArtifacts(watchRes[i]).length > 0 && !offDisk.has(watchPath(watch[i].ticker))
const failedCore = core.filter((_, i) => !coreOk(i)).map((c) => c.ticker)
const failedWatch = watch.filter((_, i) => !watchOk(i)).map((w) => w.ticker)

const okCore = coreRes.filter((_, i) => coreOk(i))
// call 只派给在盘核验通过的公司：核验失败（probeFailed）时一家都不派。
const cands = probeFailed
  ? []
  : okCore.filter((x) => x.call_candidate).map((x) => x.ticker)
const calls =
  cands.length === 0
    ? []
    : await parallel(
        cands.slice(0, 3).map((t) => () => {
          const c = core.find((y) => y.ticker === t) || { ticker: t, workspace_dir: `../${t}` }
          return agent(
            `按 .agents/skills/dalton-call/SKILL.md 对 ${t} 做 conviction 检查（知识层 ${
              c.workspace_dir || `../${t}`
            }，本周材料 ${R}/core-${t}.md）。你是子智能体，没有 Dalton 直连工具：知识层与材料用文件读取，只写 ${R}/call-${t}.md，不调用 knowledge_append_verdict、check_draft、publish_report，跳过该 SKILL 的 Step 2.2 和 Step 9。证据门槛不过就输出“暂无高 conviction”及缺什么。`,
            { agentType: 'general-purpose', label: `call ${t}`, phase: '汇总与 call', schema: CO }
          )
        })
      )

const doc = await agent(
  `${base} 按 ${S}/templates/weekly.md 汇总 ${R}/ 下全部文件为周会稿 ${K}/drafts/${E}-weekly.md。观点变化部分读各 Core 工作区本周 verdicts.md 与 ${R}/proposals/。Core 失败的公司：${
    failedCore.join(',') || '无'
  }，在“口径与缺口”写明。`,
  { agentType: 'general-purpose', label: '周会稿', phase: '汇总与 call', schema: CO }
)

if (doc === null) incomplete.push('周会稿失败')
if (failedCore.length) incomplete.push(`Core 逐家失败：${failedCore.join('、')}`)
if (failedWatch.length) incomplete.push(`Watch 命中检索失败：${failedWatch.join('、')}`)
const failedCalls = cands
  .slice(0, 3)
  .filter((t) => !calls.some((x) => x && x.ticker === t))
if (failedCalls.length) incomplete.push(`conviction call 失败：${failedCalls.join('、')}`)

phase('产物核验')
const deliverables = [
  `${R}/screen.json`,
  `${R}/attribution.json`,
  `${K}/drafts/${E}-weekly.md`,
  ...core.map((c) => `${R}/core-${c.ticker}.md`),
  ...watch.map((w) => `${R}/watch-${w.ticker}.md`),
]
// 与支线同路处理：派发抛错（如到达平台上限）按 null 计缺失，不让 run 变 script_error。
let check = null
try {
  check = await agent(
    `只读核对以下文件是否存在并计算 sha256（用 bash 的 test -e 与 sha256sum，不写任何文件）：\n${deliverables
      .map((p) => '- ' + p)
      .join('\n')}\n返回 files 字符串数组：存在的文件写 “<路径>|<sha256>”，不存在的只写 “<路径>”（不带竖线）。`,
    { agentType: 'dalton-evidence', label: '产物核验', phase: '产物核验', schema: HASH, checkFiles: false }
  )
} catch (e) {
  log(`产物核验派发失败：${e}`)
  check = null
}
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
if (check === null) incomplete.push('产物核验子任务失败')

return {
  ok: doc !== null && incomplete.length === 0,
  draft: `${K}/drafts/${E}-weekly.md`,
  core_done: okCore.length,
  core_total: core.length,
  watch_done: watch.filter((_, i) => watchOk(i)).length,
  calls: calls.filter((x) => x !== null).map((x) => x.ticker),
  proposals_dir: `${R}/proposals`,
  unverified_by_probe: probeFailed
    ? [...core.map((c) => c.ticker), ...watch.map((w) => w.ticker)]
    : [],
  incomplete,
}
