export const meta = {
  name: 'earnings-deep-update',
  description: 'Dalton 财报深度链：八节点评 ∥ 模型更新 → thesis 校准 + 前瞻对账 → 审稿 → 知识层修订草案',
}

const { ticker: T, company: C, quarter: Q, workspace_dir: W } = args
const D = args.release_date
const R = `${W}/research/${D}-deep-${Q}`
const S = '.agents/skills/dalton-earnings-deep'
const base = `先读 .agents/skills/dalton-conventions/SKILL.md 与 ${S}/SKILL.md；${C || T}（${T}）${Q}，发布日 ${D}。快讯稿在 ${W}/research/ 或 ${W}/drafts/ 下 flash-${Q}。`
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

phase('深度点评与模型')
const [report, model] = await parallel([
  () =>
    agent(
      `${base} 按 ${S}/references/eight-sections.md${
        args.sector_overlay === 'hardware' ? ` 与 ${S}/references/hardware-overlay.md` : ''
      } 写八节深度点评到 ${W}/drafts/${D}-deep-${Q}.md；价格反应用 python ${S}/scripts/price_reaction.py；卖方窗口为发布到发布后 3 个交易日。返回成稿路径与 sha256。`,
      { agentType: 'general-purpose', label: '八节点评', phase: '深度点评与模型', schema: DRAFT_OUT }
    ),
  () =>
    args.model_path
      ? agent(
          `${base} 按 .agents/skills/model-update/SKILL.md 更新模型 ${args.model_path}：触发类型 Results；先出映射表到 ${R}/mapping.md，再写入另存的新版本（不覆盖旧版），Change Log 完整；recalc 与 audit --strict 通过；输出 EPS walk 与估值变化到 ${R}/model-delta.md。`,
          { agentType: 'general-purpose', label: '模型更新', phase: '深度点评与模型', schema: OUT }
        )
      : Promise.resolve(null),
])

// 回报文件里的模型路径（取其版本号最大者）。
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
const reportedModelPath = model ? modelFileIn(model.files) : null

// 确定性子任务解析磁盘上的最终模型（ls … | sort -V | tail -1）：模型更新后、
// 每轮修订后（复审前）、循环结束后各一次；校准/审稿/知识草案/核验/返回值只认它。
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
const resolveFinalModel = async (label) => {
  const r = await agent(
    `在 workspace 里执行：ls model/${T}-model-v*.xlsx 2>/dev/null | sort -V | tail -1，对输出文件计算 sha256sum。只返回 JSON：path（ls 的原样输出）与 sha256（64 位十六进制）。不写任何文件。`,
    { agentType: 'dalton-evidence', label, phase: '解析最终模型', schema: FINAL_MODEL }
  )
  return r && typeof r.path === 'string' && r.path && shaOf(r) ? { path: normPath(r.path), sha256: r.sha256 } : null
}
phase('解析最终模型')
let modelFile = null
if (args.model_path && report !== null) {
  const updated = await resolveFinalModel('解析模型（模型更新后）')
  if (updated === null) {
    return {
      ok: false,
      report: report && `${W}/drafts/${D}-deep-${Q}.md`,
      model_file: null,
      incomplete: ['解析最终模型的子任务失败，流程终止'],
    }
  }
  if (reportedModelPath !== null && normPath(reportedModelPath) !== updated.path) {
    return {
      ok: false,
      report: report && `${W}/drafts/${D}-deep-${Q}.md`,
      model_file: updated.path,
      incomplete: [
        `模型更新回报的模型（${reportedModelPath}）与磁盘最大版本（${updated.path}）不一致，最终模型不确定，流程终止`,
      ],
    }
  }
  modelFile = updated.path
}
// 第 1 轮语言审查：八节点评写完之后、校准与第一次独立审稿之前（改稿会换哈希，必须先改再送审）。
const reportPath = `${W}/drafts/${D}-deep-${Q}.md`
phase('语言审查')
const reportSha0 = report === null ? null : shaOf(report)
const lang1 =
  reportSha0 === null
    ? null
    : await agent(
        `对 ${reportPath} 做第 1 轮语言审查：调用工具 check_language(draft_path="${reportPath}", round=1)（只标问题），读 ${reportPath}.language.md 清单，按清单逐条修改 ${reportPath}（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。改完返回 ${reportPath} 的 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 1', phase: '语言审查', schema: DRAFT_OUT }
      )
const lang1Sha = lang1 === null ? null : shaOf(lang1)
const lang1Failed = reportSha0 !== null && lang1Sha === null
const reportSha = lang1Sha || reportSha0
const reportOk = report !== null && reportSha !== null

phase('校准与对账')
const calib =
  report === null
    ? null
    : await agent(
        `${base} 按 ${S}/references/calibration.md：
1) ${
          args.preview_predictions
            ? `用 python ${S}/scripts/reconcile.py 对 ${args.preview_predictions} 逐条对账，输出 ${R}/reconcile.md，并把对账计数写成 ${R}/reconcile.json（{"wrong": <错数>, "partial": <部分数>}）；`
            : `本次财报前无 predictions.json，跳过前瞻对账：仍要生成 ${R}/reconcile.md（写明跳过原因）与 ${R}/reconcile.json（{"wrong": 0, "partial": 0}）；`
        }
2) 按 .agents/skills/thesis-tracker/SKILL.md 的 post-earnings 模式，对 ${W}/thesis.md 每个支柱给信号与状态，写 ${R}/thesis-calibration.md；
3) 更新指引兑现记录：本季实际 vs 上季给的指引，判断管理层指引风格是否延续，写 ${R}/management-delta.md。
材料：${W}/drafts/${D}-deep-${Q}.md${model ? `，${R}/model-delta.md` : ''}${
          modelFile ? `；最终模型 ${modelFile}（只读）` : ''
        }。`,
        { agentType: 'general-purpose', label: '校准与对账', phase: '校准与对账', schema: OUT }
      )

phase('审稿')
const review =
  calib === null
    ? null
    : await agent(
        `审阅 ${reportPath}（被审草稿 sha256：${
          reportSha || '未知'
        }）及 ${R}/ 下的对账与校准文件${modelFile ? `、最终模型 ${modelFile}` : ''}（只读不改任何文件）。重点：beat/miss 口径；“综合判断”是否有判断；thesis 信号是否被价格走势替代；对账是否回避了错判。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
        { agentType: 'dalton-reviewer', label: '审稿', phase: '审稿', schema: VERDICT }
      )

const incomplete = []
let finalReview = review
let rounds = 0
let currentSha = reportSha
let modelPath = modelFile
// 提前结束时解析一次磁盘上的模型，作为返回值里的 model_file；没有模型则为 null。
const resolveOnExit = async () => {
  if (modelPath === null) return null
  const r = await resolveFinalModel('解析模型（终止）')
  if (r === null) incomplete.push('解析最终模型的子任务失败')
  return r
}
// 所有提前返回的出口都经过 finish：返回值里的 model_file 只取传进来的解析结果。
const finish = (resolved, extra = {}) => ({
  ok: false,
  report: report && reportPath,
  model_delta: model && `${R}/model-delta.md`,
  model_file: resolved ? resolved.path : null,
  reconcile: calib && `${R}/reconcile.md`,
  revise_rounds: rounds,
  incomplete,
  ...extra,
})

if (review && verdictOf(review) === 'block') {
  incomplete.push(`审稿 block：${review.summary || '存在不可修订级问题'}`)
  return finish(await resolveOnExit(), {
    blocked: review.summary || '审稿 block',
    resume_hint:
      '修订报告后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，修订之后的阶段重新派发',
  })
}

// revise → 修订 → 解析模型 → 复审，最多 2 轮；每个失败出口都写进 incomplete 并停下
let resolved = null
while (finalReview && verdictOf(finalReview) === 'revise' && rounds < 2) {
  rounds += 1
  const target = modelPath === null ? null : nextVersion(modelPath)
  const modelClause =
    modelPath === null
      ? ''
      : `模型若有改动走 .agents/skills/model-update/SKILL.md，另存为 ${target}，不覆盖旧版本；模型没改就原样沿用 ${modelPath}。不管改没改，都必须在 files 里回报当前模型的路径（另存则报 ${target}，未改则报 ${modelPath}）。`
  const fixed = await agent(
    `先核对 ${reportPath} 的 sha256 是否与审稿前（${
      currentSha || '成稿记录值'
    }）一致；按审稿意见做第 ${rounds} 轮修订（P0 全改，不新增未核实数字），改完返回新 sha256。${modelClause}审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'general-purpose', label: `修订 ${rounds}`, phase: '审稿', schema: DRAFT_OUT }
  )
  const fixedSha = shaOf(fixed)
  if (fixedSha === null) {
    incomplete.push(`第 ${rounds} 轮修订失败（修订结果或其 sha256 缺失/非法），未复审，流程终止`)
    return finish(await resolveOnExit())
  }
  if (modelPath !== null) {
    const reportedPath = modelFileIn(fixed.files)
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
  currentSha = fixedSha
  const reReview = await agent(
    `复审（第 ${rounds} 轮修订后）：审阅 ${reportPath}（被审草稿 sha256：${
      currentSha
    }）及 ${R}/ 下的对账与校准文件${
      modelPath ? `、最终模型 ${modelPath}` : ''
    }（只读不改任何文件）。重点核对上一轮 must_fix 是否全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。上一轮审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'dalton-reviewer', label: `复审 ${rounds}`, phase: '审稿', schema: VERDICT }
  )
  if (reReview === null) {
    incomplete.push(`第 ${rounds} 轮复审失败，流程终止`)
    return finish(resolved)
  }
  if (verdictOf(reReview) === 'block') {
    incomplete.push(`复审 block：${reReview.summary || '存在不可修订级问题'}，流程终止`)
    return finish(resolved, {
      blocked: reReview.summary || '复审 block',
      resume_hint:
        '修订报告后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，修订之后的阶段重新派发',
    })
  }
  finalReview = reReview
}
let reviewPassed = finalReview !== null && verdictOf(finalReview) === 'pass'

// 笔记两轮：一次写完、不再改的校准/对账笔记同样审两轮——第 1 轮审完按清单改，
// 第 2 轮复核（改动过的文件再补一次 round=2 确认，绑定终态哈希）。
const notesReview =
  !reportOk || !reviewPassed
    ? null
    : await agent(
        `对本次运行产出、给人读的 .md 笔记逐一做两轮语言审查（规则见 .agents/skills/dalton-language/SKILL.md，只标问题）。每个文件依次：1) 调用工具 check_language(draft_path="<文件>", round=1)，读 <文件>.language.md 清单并按清单修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）；2) 调用 check_language(draft_path="<文件>", round=2) 复核；若第 1 轮后改过稿，改完再调用一次 round=2 确认（记录追加在同一 json）。\n文件清单：\n- ${R}/reconcile.md\n- ${R}/thesis-calibration.md\n- ${R}/management-delta.md${
          args.model_path ? `\n- ${R}/mapping.md\n- ${R}/model-delta.md` : ''
        }\n清单之外本次运行写的其他给人读的 .md 一并同法处理；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/ 下的文件、知识层渲染文件与 .audit/.annotated/.language 侧车不审。返回处理过的文件列表与摘要。`,
        { agentType: 'general-purpose', label: '笔记语言审查', phase: '审稿', schema: OUT }
      )
if (reportOk && reviewPassed && notesReview === null) {
  incomplete.push('笔记语言审查失败（校准/对账笔记未完成两轮语言审查）')
}

// 第 2 轮语言审查：终稿内容定稿（审稿修订循环结束）之后、提交最后一次审稿之前。
// 改了稿就再跑一次 round=2 复核，使最后一条第 2 轮记录绑定终稿哈希；哈希有变
// 则终稿审稿重新绑定独立审稿哈希——语言修改不能让已有 pass 记录失效却无人重审。
const preLang2Sha = currentSha
const lang2 =
  !reportOk || !reviewPassed
    ? null
    : await agent(
        `对 ${reportPath} 做第 2 轮语言审查：调用工具 check_language(draft_path="${reportPath}", round=2)，读 ${reportPath}.language.md 清单，按清单逐条完成终稿语言修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）。若草稿有改动，再调用一次 check_language(draft_path="${reportPath}", round=2) 对终稿复核（记录追加在同一 json）。返回 ${reportPath} 最终 sha256。`,
        { agentType: 'general-purpose', label: '语言审查 2', phase: '审稿', schema: DRAFT_OUT }
      )
const lang2Sha = lang2 === null ? null : shaOf(lang2)
if (reportOk && reviewPassed && lang2Sha === null) {
  incomplete.push('语言审查第 2 轮失败（结果或其 sha256 缺失/非法），未做终稿语言修改')
  reviewPassed = false
} else if (reportOk && reviewPassed && lang2Sha !== preLang2Sha) {
  currentSha = lang2Sha
  const finalCheck = await agent(
    `终稿审稿（语言修改后）：审阅 ${reportPath}（被审草稿 sha256：${lang2Sha}）及 ${R}/ 下的对账与校准文件${modelFile ? `、最终模型 ${modelFile}` : ''}（只读不改任何文件）。重点：语言修改未动事实与数字，上一轮 must_fix 仍全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；draft_sha256 填提示词里的被审草稿 sha256 原值）。`,
    { agentType: 'dalton-reviewer', label: '终稿审稿', phase: '审稿', schema: VERDICT }
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
let finalModel = null
if (modelPath !== null) {
  finalModel = await resolveFinalModel('解析最终模型')
  if (finalModel === null) {
    incomplete.push('解析最终模型的子任务失败，流程终止')
    return finish(null)
  }
  if (finalModel.path !== modelPath) {
    incomplete.push(
      `最终模型与修订确认的版本（${modelPath}）不一致：磁盘最大版本 ${finalModel.path}，最终模型不确定，流程终止`
    )
    return finish(finalModel)
  }
}

const kp = await agent(
  `${base} 先核对 ${reportPath} 的 sha256 是否与修订后（${
    currentSha || '修订后记录值'
  }）一致。${finalModel ? `最终模型为 ${finalModel.path}。` : ''}然后汇总 ${R}/ 全部文件与审稿意见（${
    finalReview ? '见下' : '审稿缺席：只写 NO_CHANGE 以外结论为待审说明，不改核心支柱状态'
  }），按 PK.2 的 JSON Schema 写知识层修订草案 ${R}/knowledge-revision.json（{doc: {ops, reason, evidence_refs}}）：thesis 支柱信号与证据行、debate 变化、drivers 新读数、management 指引兑现行、lessons（前瞻错判的原因）、models（新模型版本登记，若模型更新失败则写“模型未更新”并排人工跟进）、事件五词终判。\n${
    finalReview ? JSON.stringify(finalReview) : ''
  }`,
  { agentType: 'general-purpose', label: '知识层修订草案', phase: '审稿', schema: OUT }
)

if (!reportOk) incomplete.push('八节点评失败（草稿或其 sha256 缺失/非法）')
if (lang1Failed) incomplete.push('语言审查第 1 轮失败（结果或其 sha256 缺失/非法），未按清单修改')
if (args.model_path && model === null) incomplete.push('模型更新失败（须人工跟进）')
if (calib === null && reportOk) incomplete.push('校准与对账失败')
if (review === null && calib !== null) incomplete.push('审稿失败')
if (!reviewPassed && !incomplete.length) incomplete.push('审稿未最终通过（无 pass 记录）')
if (kp === null) incomplete.push('知识层修订草案失败')

phase('产物核验')
const deliverables = [
  `${W}/drafts/${D}-deep-${Q}.md`,
  `${W}/drafts/${D}-deep-${Q}.md.language.json`,
  `${R}/reconcile.md`,
  `${R}/reconcile.md.language.json`,
  `${R}/reconcile.json`,
  `${R}/thesis-calibration.md`,
  `${R}/thesis-calibration.md.language.json`,
  `${R}/management-delta.md`,
  `${R}/management-delta.md.language.json`,
  `${R}/knowledge-revision.json`,
]
if (args.model_path) {
  deliverables.push(`${R}/mapping.md`, `${R}/mapping.md.language.json`, `${R}/model-delta.md`, `${R}/model-delta.md.language.json`)
}
if (finalModel) deliverables.push(finalModel.path)
const check =
  !reportOk
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
return {
  ok: reportOk && reviewPassed && incomplete.length === 0,
  report: report && `${W}/drafts/${D}-deep-${Q}.md`,
  model_delta: model && `${R}/model-delta.md`,
  model_file: finalModel ? finalModel.path : null,
  reconcile: calib && `${R}/reconcile.md`,
  review_done: finalReview !== null,
  review_verdict: finalReview && verdictOf(finalReview),
  language_done: reportOk && lang1Sha !== null && lang2Sha !== null,
  revise_rounds: rounds,
  knowledge_revision: kp && `${R}/knowledge-revision.json`,
  incomplete,
}
