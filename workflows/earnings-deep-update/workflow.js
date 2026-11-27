export const meta = {
  name: 'earnings-deep-update',
  description: 'Dalton 财报深度链：八节点评 ∥ 模型更新流水线 → thesis 校准 + 前瞻对账 → 审稿与共享改模 → 知识层修订草案',
}

/* DALTON_INCLUDE:_shared/modeling_stages.js */

const { ticker: T, company: C, quarter: Q, workspace_dir: W } = args
const D = args.release_date
const R = `${W}/research/${D}-deep-${Q}`
const progressPath = args.progress_path || `${R}/.progress.json`
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
const normPath = (p) => _mNormPath(p, W)
const hasArtifact = (list, target) => _mHasPath(list, target, W)

phase('深度点评与模型')
const [report, modelResult] = await parallel([
  () =>
    agent(
      `${base} 按 ${S}/templates/deep.md（首个二级标题为「## 摘要」，后接「业绩与指引」「投资逻辑的验证」「赔率与行动」「跟踪」、按需「附注」及文末「引用」，对账与校准的逐条表格留内部文件、不在正文展开；改判条件只在「跟踪」完整列一次，「赔率与行动」和「摘要」只写决定行动的那一个条件；成稿后通读全文，同一缺口、同一限定、同一改判条件只保留最合适的一处）与 ${S}/references/eight-sections.md${
        args.sector_overlay === 'hardware' ? ` 与 ${S}/references/hardware-overlay.md` : ''
      } 写深度点评到 ${W}/drafts/${D}-deep-${Q}.md；价格反应用 python ${S}/scripts/price_reaction.py；卖方窗口为发布到发布后 3 个交易日。返回成稿路径与 sha256。`,
      { agentType: 'general-purpose', label: '八节点评', phase: '深度点评与模型', schema: DRAFT_OUT }
    ),
  () =>
    runModelingPipeline({
      workspaceDir: W,
      ticker: T,
      company: C || T,
      researchDir: R,
      progressPath,
      workflow: 'earnings-deep-update',
      mode: 'update',
      modelPath: args.model_path || null,
      expectedBaselineSha256: args.expected_baseline_sha256 || null,
      consensusThresholds: args.consensus_thresholds || null,
      quarter: Q,
      asOf: D,
      phaseLabel: '深度点评与模型',
      emitPhases: false,
      skipIfNoModel: true,
      deepNotes: true,
      contextNote: base,
    }),
])

let modelStatus = modelResult ? modelResult.status : args.model_path ? 'failed_unusable' : 'skipped_no_model'
let modelPath = modelResult && modelResult.candidate_path ? normPath(modelResult.candidate_path) : null
let modelSha = (modelResult && modelResult.model_sha256) || null
let modelRunId = (modelResult && modelResult.run_id) || null
let modelConsensusPath = (modelResult && modelResult.consensus_path) || null
let modelFailedStages = modelResult && Array.isArray(modelResult.failed_stages) ? modelResult.failed_stages.slice() : []
let modelArtifacts = modelResult && Array.isArray(modelResult.artifacts) ? modelResult.artifacts.slice() : []
let hasMapping = hasArtifact(modelArtifacts, `${R}/mapping.md`)
let hasModelDelta = hasArtifact(modelArtifacts, `${R}/model-delta.md`)
let modelCheckPath = hasArtifact(modelArtifacts, `${R}/model_dump/model-check.md`) ? `${R}/model_dump/model-check.md` : null
let remainingIssuesPath =
  modelStatus === 'delivered_with_open_issues'
    ? (modelResult && modelResult.remaining_issues_path) || `${R}/model_dump/remaining-issues.md`
    : null
const modelVerificationPath = args.model_path ? `${R}/model_dump/model-verification.json` : null

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
let currentSha = reportSha

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
2) 按第二节的财报后校准规则，对 ${W}/thesis.md 每个支柱给信号与状态，写 ${R}/thesis-calibration.md；
3) 更新指引兑现记录：本季实际 vs 上季给的指引，判断管理层指引风格是否延续，写 ${R}/management-delta.md；
4) 把 ${R}/reconcile.md、${R}/thesis-calibration.md、${R}/management-delta.md 的结论各用一两句话写进 ${reportPath} 正文「投资逻辑的验证」节（逐条表格留在内部文件），改完返回 ${reportPath} 新的 sha256。
材料：${W}/drafts/${D}-deep-${Q}.md${hasModelDelta ? `，${R}/model-delta.md` : ''}${
          modelPath ? `；最终模型 ${modelPath}（只读）` : ''
        }。`,
        { agentType: 'general-purpose', label: '校准与对账', phase: '校准与对账', schema: DRAFT_OUT }
      )
const calibSha = shaOf(calib)
if (calibSha !== null) currentSha = calibSha

phase('审稿')
let review =
  calibSha === null
    ? null
    : await agent(
        `审阅 ${reportPath}（被审草稿 sha256：${
          currentSha || '未知'
        }）及 ${R}/ 下的对账与校准文件${modelPath ? `、最终模型 ${modelPath}` : ''}（只读不改任何文件）。重点：超预期/低于预期（beat/miss）口径；摘要与各节是否有独立判断；核心论点（thesis）信号是否被价格走势替代；对账是否回避了错判；核对正文「投资逻辑的验证」节是否已各用一两句话准确写入 reconcile.md、thesis-calibration.md、management-delta.md 的结论（逐条表格留在内部文件）。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；items 逐项覆盖 1–9 项检查）。`,
        { agentType: 'dalton-reviewer', label: '审稿', phase: '审稿', schema: VERDICT, reviewDraft: reportPath }
      )

const incomplete = []
// 审稿结果未覆盖 1–9 项检查时按审稿失败处理（与发布审稿同一契约，fail-closed）。
if (review !== null && !completeReview(review)) {
  incomplete.push('审稿结果未覆盖 1–9 项检查，按审稿失败处理')
  review = null
}
let finalReview = review
let rounds = 0

// 所有提前返回的出口都经过 finish：直接读取当前流水线与账本终态。
const finish = (extra = {}) => ({
  ok: false,
  report: report && reportPath,
  model_delta: hasModelDelta ? `${R}/model-delta.md` : null,
  model_file: modelPath,
  model_status: modelStatus,
  model_verification: modelVerificationPath,
  model_check: modelCheckPath,
  remaining_issues: remainingIssuesPath,
  reconcile: calib && `${R}/reconcile.md`,
  revise_rounds: rounds,
  incomplete,
  ...extra,
})

if (review && verdictOf(review) === 'block') {
  incomplete.push(`审稿 block：${review.summary || '存在不可修订级问题'}`)
  return finish({
    blocked: review.summary || '审稿 block',
    resume_hint:
      '修订报告后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，修订之后的阶段重新派发',
  })
}

// revise → 共享改模入口 + 报告修订 → 复审，最多 2 轮；每个失败出口都写进 incomplete 并停下
while (finalReview && verdictOf(finalReview) === 'revise' && rounds < 2) {
  rounds += 1
  if (modelPath !== null) {
    const revModel = await reviseModelForReport({
      workspaceDir: W,
      ticker: T,
      researchDir: R,
      progressPath,
      workflow: 'earnings-deep-update',
      currentModelPath: modelPath,
      currentModelSha256: modelSha,
      currentStatus: modelStatus,
      currentArtifacts: modelArtifacts,
      consensusPath: modelConsensusPath,
      consensusThresholds: args.consensus_thresholds || null,
      priorFailedStages: modelFailedStages,
      runId: modelRunId,
      reportPath,
      reviewFeedback: finalReview,
      extraSyncPaths: hasModelDelta ? [`${R}/model-delta.md`] : [],
      phaseLabel: '审稿',
    })
    modelPath = revModel.modelPath || modelPath
    modelSha = revModel.modelSha256 || modelSha
    modelStatus = revModel.status
    modelRunId = revModel.runId || modelRunId
    modelFailedStages = Array.isArray(revModel.failedStages) ? revModel.failedStages.slice() : modelFailedStages
    modelArtifacts = Array.isArray(revModel.artifacts) ? revModel.artifacts : modelArtifacts
    if (revModel.reportSha256) currentSha = revModel.reportSha256
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
  }
  const existingModelReports = []
  if (modelCheckPath) existingModelReports.push(modelCheckPath)
  if (hasArtifact(modelArtifacts, `${R}/model_dump/consensus-diff.md`)) {
    existingModelReports.push(`${R}/model_dump/consensus-diff.md`)
  }
  if (remainingIssuesPath) existingModelReports.push(remainingIssuesPath)
  const modelRefClause = modelPath
    ? `当前最终模型为 ${modelPath}${modelSha ? `（sha256：${modelSha}）` : ''}（已由共享改模入口更新，此处不再改写 .xlsx；若报告正文${hasModelDelta ? `或 ${R}/model-delta.md ` : ''}引用模型，请核对已指向 ${modelPath}${existingModelReports.length ? `，可参考 ${existingModelReports.join('、')}` : ''}）。`
    : ''
  const fixed = await agent(
    `先核对 ${reportPath} 的 sha256 是否与审稿前（${
      currentSha || '成稿记录值'
    }）一致；按审稿意见做第 ${rounds} 轮修订（必改项 must_fix 全改，不新增未核实数字；修订后通读全文，同一缺口、同一限定、同一改判条件只保留最合适的一处），并核对 ${R}/reconcile.md、${R}/thesis-calibration.md、${R}/management-delta.md 的结论是否已各用一两句话准确写进正文「投资逻辑的验证」节（如有缺漏或偏差一并修正，逐条表格留在内部文件），改完返回新 sha256。${modelRefClause}审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'general-purpose', label: `修订 ${rounds}`, phase: '审稿', schema: DRAFT_OUT }
  )
  const fixedSha = shaOf(fixed)
  if (fixedSha === null) {
    incomplete.push(`第 ${rounds} 轮修订失败（修订结果或其 sha256 缺失/非法），未复审，流程终止`)
    return finish()
  }
  currentSha = fixedSha
  const reReview = await agent(
    `复审（第 ${rounds} 轮修订后）：审阅 ${reportPath}（被审草稿 sha256：${
      currentSha
    }）及 ${R}/ 下的对账与校准文件${
      modelPath ? `、最终模型 ${modelPath}` : ''
    }（只读不改任何文件）。重点核对上一轮 must_fix 是否全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；items 逐项覆盖 1–9 项检查）。上一轮审稿意见：\n${JSON.stringify(
      finalReview
    )}`,
    { agentType: 'dalton-reviewer', label: `复审 ${rounds}`, phase: '审稿', schema: VERDICT, reviewDraft: reportPath }
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
      resume_hint:
        '修订报告后用启动时的同一组参数 + resume=<.progress.json 里的 run_id> 重跑：label、prompt 哈希与产物哈希一致的已完派发会直接复用，修订之后的阶段重新派发',
    })
  }
  finalReview = reReview
}
let reviewPassed = finalReview !== null && verdictOf(finalReview) === 'pass'

// 笔记两轮：只对本次实际已生成的给人读 .md 笔记做两轮语言审查；model_dump/ 下机器报告免审。
const noteFiles = [
  `${R}/reconcile.md`,
  `${R}/thesis-calibration.md`,
  `${R}/management-delta.md`,
]
if (hasMapping) noteFiles.push(`${R}/mapping.md`)
if (hasModelDelta) noteFiles.push(`${R}/model-delta.md`)

const notesReview =
  !reportOk || !reviewPassed
    ? null
    : await agent(
        `对本次运行产出、给人读的 .md 笔记逐一做两轮语言审查（规则见 .agents/skills/dalton-language/SKILL.md，只标问题）。每个文件依次：1) 调用工具 check_language(draft_path="<文件>", round=1)，读 <文件>.language.md 清单并按清单修改（只改语言表达，不动事实、数字、术语、引用编号与判断强度；工具报错就不改稿并在 summary 里原样汇报错误）；2) 调用 check_language(draft_path="<文件>", round=2) 复核；若第 1 轮后改过稿，改完再调用一次 round=2 确认（记录追加在同一 json）。\n文件清单：\n${noteFiles
          .map((p) => `- ${p}`)
          .join('\n')}\n清单之外本次运行写的其他给人读的 .md 一并同法处理；json/xlsx 等机器文件、bg_data/、data/、lenses_work/、model_dump/ 下的文件、知识层渲染文件与 .audit/.annotated/.language 侧车不审。返回处理过的文件列表与摘要。`,
        { agentType: 'general-purpose', label: '笔记语言审查', phase: '审稿', schema: OUT }
      )
if (reportOk && reviewPassed && notesReview === null) {
  incomplete.push('笔记语言审查失败（校准/对账笔记未完成两轮语言审查）')
}

// 第 2 轮语言审查：终稿内容定稿（审稿修订循环结束）之后、提交最后一次审稿之前。
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
    `终稿审稿（语言修改后）：审阅 ${reportPath}（被审草稿 sha256：${lang2Sha}）及 ${R}/ 下的对账与校准文件${modelPath ? `、最终模型 ${modelPath}` : ''}（只读不改任何文件）。重点：语言修改未动事实与数字，上一轮 must_fix 仍全部解决。按 dalton-reviewer 规范输出 JSON（verdict ∈ pass/revise/block；items 逐项覆盖 1–9 项检查）。`,
    { agentType: 'dalton-reviewer', label: '终稿审稿', phase: '审稿', schema: VERDICT, reviewDraft: reportPath }
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

const kp = await agent(
  `${base} 先核对 ${reportPath} 的 sha256 是否与修订后（${
    currentSha || '修订后记录值'
  }）一致。${modelPath ? `最终模型为 ${modelPath}（终态：${modelStatus}，账本：${modelVerificationPath}）。` : ''}然后汇总 ${R}/ 全部文件与审稿意见（${
    finalReview ? '见下' : '审稿缺席：只写 NO_CHANGE 以外结论为待审说明，不改核心支柱状态'
  }），按知识层修订 JSON 规范写知识层修订草案 ${R}/knowledge-revision.json（{doc: {ops, reason, evidence_refs}}）：thesis 支柱信号与证据行、debate 变化、drivers 新读数、management 指引兑现行、lessons（前瞻错判的原因）、models（按 .agents/skills/dalton-model/SKILL.md 登记新模型版本与关键输出单元格；若模型未产出可读候选则写“模型未更新”并在 agenda_ops 排入人工跟进待办）、事件五词终判。\n${
    finalReview ? JSON.stringify(finalReview) : ''
  }`,
  { agentType: 'general-purpose', label: '知识层修订草案', phase: '审稿', schema: OUT }
)

if (!reportOk) incomplete.push('八节点评失败（草稿或其 sha256 缺失/非法）')
if (lang1Failed) incomplete.push('语言审查第 1 轮失败（结果或其 sha256 缺失/非法），未按清单修改')
if (args.model_path && modelStatus === 'failed_unusable') incomplete.push('模型更新失败（无可读候选模型，须人工跟进）')
if (calibSha === null && reportOk) incomplete.push('校准与对账失败（结果或其 sha256 缺失/非法）')
if (review === null && calibSha !== null) incomplete.push('审稿失败')
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
  if (modelVerificationPath) deliverables.push(modelVerificationPath)
  if (remainingIssuesPath) deliverables.push(remainingIssuesPath)
  if (hasMapping) deliverables.push(`${R}/mapping.md`, `${R}/mapping.md.language.json`)
  if (hasModelDelta) deliverables.push(`${R}/model-delta.md`, `${R}/model-delta.md.language.json`)
  if (modelPath) deliverables.push(modelPath)
}
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
  model_delta: hasModelDelta ? `${R}/model-delta.md` : null,
  model_file: modelPath,
  model_status: modelStatus,
  model_verification: modelVerificationPath,
  model_check: modelCheckPath,
  remaining_issues: remainingIssuesPath,
  reconcile: calib && `${R}/reconcile.md`,
  review_done: finalReview !== null,
  review_verdict: finalReview && verdictOf(finalReview),
  language_done: reportOk && lang1Sha !== null && lang2Sha !== null,
  revise_rounds: rounds,
  knowledge_revision: kp && `${R}/knowledge-revision.json`,
  incomplete,
}
