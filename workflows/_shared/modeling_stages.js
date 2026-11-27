// Shared Modeling v2 pipeline stages: runModelingPipeline, reviseModelForReport, writeFallbackLedger.
// Inlined into company-model, earnings-deep-update, and full-coverage by content assembly.

const _MODEL_SHA_RE = /^[0-9a-f]{64}$/i

const _MODEL_PREP_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    mode: { type: 'string', enum: ['build', 'update'] },
    baseline_path: { type: ['string', 'null'] },
    baseline_sha256: { type: ['string', 'null'] },
    fingerprint_path: { type: ['string', 'null'] },
    consensus_path: { type: ['string', 'null'] },
    baseline_health_warning: { type: ['string', 'null'] },
  },
  required: ['files', 'summary', 'mode'],
}

// Non-replayable live state query (no files/file field; journal.py never replays {run_id}-only queries).
const _MODEL_RUN_ID_SCHEMA = {
  type: 'object',
  properties: {
    run_id: { type: ['string', 'null'] },
  },
  required: ['run_id'],
}

const _MODEL_STAGE2A_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    candidate_path: { type: ['string', 'null'] },
    model_sha256: { type: ['string', 'null'] },
    recalc_status: { type: ['string', 'null'] },
  },
  required: ['files', 'summary', 'candidate_path'],
}

const _MODEL_STAGE2B_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    candidate_path: { type: ['string', 'null'] },
    model_sha256: { type: ['string', 'null'] },
    recalc_status: { type: ['string', 'null'] },
    consensus_status: { type: ['string', 'null'] },
    consensus_diff_generated: { type: 'boolean' },
  },
  required: ['files', 'summary', 'consensus_diff_generated'],
}

const _MODEL_CHECK_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    readable: { type: 'boolean' },
    final_status: { type: 'string', enum: ['pass', 'delivered_with_open_issues', 'failed_unusable'] },
    model_sha256: { type: ['string', 'null'] },
    actionable_count: { type: 'integer' },
    actionable_issues: { type: 'array', items: { type: 'string' } },
    baseline_legacy_issues: { type: 'array', items: { type: 'string' } },
    missing_evidence_issues: { type: 'array', items: { type: 'string' } },
    not_applicable_issues: { type: 'array', items: { type: 'string' } },
    missing_required_sheets: { type: 'array', items: { type: 'string' } },
    remaining_issues_path: { type: ['string', 'null'] },
    summary_10lines: { type: ['string', 'null'] },
  },
  required: ['files', 'summary', 'readable', 'final_status', 'actionable_count'],
}

const _MODEL_FIX_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    candidate_path: { type: ['string', 'null'] },
    model_sha256: { type: ['string', 'null'] },
    recalc_status: { type: ['string', 'null'] },
    consensus_diff_generated: { type: 'boolean' },
  },
  required: ['files', 'summary', 'candidate_path'],
}

const _MODEL_LEDGER_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    final_status: { type: 'string', enum: ['delivered_with_open_issues', 'failed_unusable'] },
    model_sha256: { type: ['string', 'null'] },
    run_id: { type: ['string', 'null'] },
  },
  required: ['files', 'summary', 'final_status'],
}

const _MODEL_NOTE_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
  },
  required: ['files', 'summary'],
}

const _MODEL_REV_EDIT_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    changed: { type: 'boolean' },
    model_path: { type: ['string', 'null'] },
    model_sha256: { type: ['string', 'null'] },
    report_sha256: { type: ['string', 'null'] },
    recalc_status: { type: ['string', 'null'] },
    consensus_diff_generated: { type: 'boolean' },
  },
  required: ['files', 'summary', 'changed', 'model_path'],
}

function _mNormPath(p, workspaceDir) {
  if (typeof p !== 'string') return null
  let s = p.trim().replace(/^\.\//, '')
  const w = typeof workspaceDir === 'string' ? workspaceDir.trim().replace(/\/+$/, '').replace(/^\.\//, '') : ''
  if (w && w !== '.' && s.startsWith(w + '/')) s = s.slice(w.length + 1)
  return s.replace(/^\.\//, '') || null
}

function _mValidSha(v) {
  return typeof v === 'string' && _MODEL_SHA_RE.test(v.trim()) ? v.trim().toLowerCase() : null
}

function _mPushUnique(arr, item) {
  if (item && !arr.includes(item)) arr.push(item)
}

function _mHasPath(files, target, workspaceDir) {
  const want = _mNormPath(target, workspaceDir)
  if (!want || !Array.isArray(files)) return false
  return files.some((f) => _mNormPath(typeof f === 'string' ? f.split('|', 1)[0] : null, workspaceDir) === want)
}

async function _mSafeAgent(prompt, opts) {
  try {
    return await agent(prompt, opts)
  } catch (err) {
    if (err instanceof TypeError || err instanceof ReferenceError) throw err
    log(`[modeling] 子任务异常（${opts && opts.label ? opts.label : 'unknown'}）：${String(err)}`)
    return null
  }
}

function _mProgressReadCmd(progressPath) {
  return `python3 -c "import json,os; p='${progressPath}'; print(json.load(open(p)).get('run_id','') if os.path.exists(p) else '')"`
}

async function _mReadLiveRunId(progressPath, phaseTitle) {
  const prompt = [
    `只读读取本轮工作流运行 ID（不修改任何文件）。`,
    `在 Bash 执行：\`${_mProgressReadCmd(progressPath)}\`，将输出的非空字符串作为 JSON 的 run_id 字段原样回传（输出为空则回传 null）。`,
  ].join('\n')
  const res = await _mSafeAgent(prompt, {
    agentType: 'general-purpose',
    label: '读取运行ID',
    phase: phaseTitle || '读取运行ID',
    schema: _MODEL_RUN_ID_SCHEMA,
    checkFiles: false,
  })
  if (res && typeof res.run_id === 'string' && res.run_id.trim()) {
    return res.run_id.trim()
  }
  return null
}

/* DALTON_CAP:researchdata_bridge */
function _mConsensusSourceHint() {
  return '一致预期按能力调用 researchdata.finance(route="estimates", ticker=...) 获取，并按需检索卖方报告；原始数据存 data/consensus/<YYYY-MM-DD_HHMM>.json。'
}
/* DALTON_CAP_ELSE */
function _mConsensusSourceHint() {
  return '一致预期调用公开行情工具 yf_analysis.get_revenue_estimates / yf_analysis.get_earnings_estimates（可用时补充 fundamentals.get_analyst_estimates）获取；原始数据存 data/consensus/<YYYY-MM-DD_HHMM>.json。'
}
/* /DALTON_CAP */

async function writeFallbackLedger(ctx) {
  const W = ctx.workspaceDir || '.'
  const T = String(ctx.ticker || '').trim().toUpperCase()
  const R = ctx.researchDir
  const progressPath = ctx.progressPath || `${R}/.progress.json`
  const wfName = ctx.workflow || 'company-model'
  const kind = ctx.kind || 'modeling'
  const candidatePath = _mNormPath(ctx.candidatePath, W)
  const baselinePath = _mNormPath(ctx.baselinePath, W)
  const failedStages = Array.isArray(ctx.failedStages) && ctx.failedStages.length ? ctx.failedStages.slice() : ['check']
  const runId = ctx.runId && String(ctx.runId).trim() ? String(ctx.runId).trim() : null
  const fixRounds = typeof ctx.fixRounds === 'number' ? ctx.fixRounds : null
  const reason = ctx.reason || '检查、复算或笔记子任务超时或返回空'
  const phaseTitle = ctx.phaseLabel || '写入降级账本'

  // Never invoke write-ledger when there is no readable candidate on disk (prevents --candidate "null").
  if (!candidatePath) {
    return {
      ok: false,
      final_status: 'failed_unusable',
      ledger_path: null,
      remaining_issues_path: null,
      model_sha256: null,
      run_id: runId,
      failed_stages: failedStages,
      artifacts: [],
    }
  }

  const ledgerPath = `${R}/model_dump/model-verification.json`
  const remPath = `${R}/model_dump/remaining-issues.md`
  const failedArg = failedStages.join(',')

  const runIdStep = runId
    ? `本次工作流 run_id 为 "${runId}"，命令参数传入 --run-id "${runId}"。`
    : `先在 Bash 中读取进度文件获取 run_id：\`${_mProgressReadCmd(progressPath)}\`；若输出非空，将结果传入 --run-id，并在返回 JSON 的 run_id 字段原样回传（为空则回传 null）。`

  const cmdParts = [
    `python3 .agents/skills/dalton-model/scripts/model_check.py write-ledger`,
    `--workspace "${W}"`,
    `--ticker "${T}"`,
    `--research-dir "${R}"`,
    `--candidate "${candidatePath}"`,
    `--kind "${kind}"`,
    `--failed-stages "${failedArg}"`,
    `--workflow "${wfName}"`,
  ]
  if (baselinePath) cmdParts.push(`--baseline "${baselinePath}"`)
  if (fixRounds !== null) cmdParts.push(`--fix-rounds ${fixRounds}`)
  if (runId) cmdParts.push(`--run-id "${runId}"`)
  cmdParts.push(`--reason "${String(reason).replace(/"/g, "'")}"`)

  const prompt = [
    `为 ${T} 补写降级终态账本（不修改模型文件）。`,
    runIdStep,
    `在 Bash 执行：\`${cmdParts.join(' ')}\`（若上面刚读出非空 run_id 请补上 --run-id <runId>）。`,
    `确认以下两个文件已生成且非空：${ledgerPath} 与 ${remPath}。`,
    `返回的 files 数组只列这两个文件：["${ledgerPath}", "${remPath}"]（不要把未生成的阶段报告放进 files），并回传 final_status、model_sha256 与 run_id。`,
  ].join('\n')

  const res = await _mSafeAgent(prompt, {
    agentType: 'general-purpose',
    label: '写入降级账本',
    phase: phaseTitle,
    schema: _MODEL_LEDGER_SCHEMA,
  })

  if (!res || typeof res !== 'object') {
    return {
      ok: false,
      final_status: 'failed_unusable',
      ledger_path: null,
      remaining_issues_path: null,
      model_sha256: null,
      run_id: runId,
      failed_stages: failedStages,
      artifacts: [],
    }
  }

  const ledgerOk = _mHasPath(res.files, ledgerPath, W)
  const remOk = _mHasPath(res.files, remPath, W)
  const confirmed = []
  if (ledgerOk) confirmed.push(ledgerPath)
  if (remOk) confirmed.push(remPath)
  const effectiveRunId = (res.run_id && String(res.run_id).trim()) || runId

  if (!ledgerOk || !remOk || res.final_status === 'failed_unusable') {
    return {
      ok: false,
      final_status: 'failed_unusable',
      ledger_path: ledgerOk ? ledgerPath : null,
      remaining_issues_path: remOk ? remPath : null,
      model_sha256: _mValidSha(res.model_sha256),
      run_id: effectiveRunId,
      failed_stages: failedStages,
      artifacts: confirmed,
    }
  }

  return {
    ok: true,
    final_status: res.final_status || 'delivered_with_open_issues',
    ledger_path: ledgerPath,
    remaining_issues_path: remPath,
    model_sha256: _mValidSha(res.model_sha256),
    run_id: effectiveRunId,
    failed_stages: failedStages,
    artifacts: confirmed,
  }
}

async function runModelingPipeline(ctx) {
  const W = ctx.workspaceDir || '.'
  const T = String(ctx.ticker || '').trim().toUpperCase()
  const C = ctx.company || T
  const R = ctx.researchDir
  const progressPath = ctx.progressPath || `${R}/.progress.json`
  const wfName = ctx.workflow || 'company-model'
  const emitPhases = ctx.emitPhases !== false
  const modeReq = ctx.mode || 'auto'
  const startModelPath = ctx.modelPath || null
  const expectedBaseSha = ctx.expectedBaselineSha256 || null
  const consensusThresholds = ctx.consensusThresholds || null
  const modelSpecPath = ctx.modelSpecPath || null
  const deepNotes = Boolean(ctx.deepNotes)
  const extraCtx = ctx.contextNote ? `${ctx.contextNote}\n` : ''

  const emptyIssues = () => ({
    actionable: [],
    baseline_legacy: [],
    missing_evidence: [],
    not_applicable: [],
  })

  if (ctx.skipIfNoModel && !startModelPath) {
    return {
      ok: true,
      status: 'skipped_no_model',
      candidate_path: null,
      model_sha256: null,
      baseline_path: null,
      baseline_sha256: null,
      consensus_path: null,
      fix_rounds: 0,
      failed_stages: [],
      artifacts: [],
      actionable_issues: [],
      open_issues_by_class: emptyIssues(),
      missing_required_sheets: [],
      remaining_issues_path: null,
      summary_10lines: '本次未传入起点模型，跳过模型更新。',
      run_id: null,
      failure_reason: null,
    }
  }

  const setPhase = (title) => {
    if (emitPhases) phase(title)
    return emitPhases ? title : (ctx.phaseLabel || title)
  }

  const dumpDir = `${R}/model_dump`
  const fpPath = `${dumpDir}/${T}-fingerprint.json`
  const coordMapPath = `${dumpDir}/coord_map.json`
  const ledgerPath = `${dumpDir}/model-verification.json`
  const checkMdPath = `${dumpDir}/model-check.md`
  const reviewPackPath = `${dumpDir}/model-review-pack.md`
  const consensusMdPath = `${dumpDir}/consensus-diff.md`
  const consensusJsonPath = `${dumpDir}/consensus-diff.json`
  const remPath = `${dumpDir}/remaining-issues.md`
  const mappingPath = `${R}/mapping.md`
  const modelDeltaPath = `${R}/model-delta.md`

  const failUnusable = (reason, failedStages, extra = {}) => ({
    ok: false,
    status: 'failed_unusable',
    candidate_path: extra.candidate_path || null,
    model_sha256: extra.model_sha256 || null,
    baseline_path: extra.baseline_path || startModelPath,
    baseline_sha256: extra.baseline_sha256 || null,
    consensus_path: extra.consensus_path || null,
    fix_rounds: extra.fix_rounds || 0,
    failed_stages: failedStages,
    artifacts: extra.artifacts || [],
    actionable_issues: extra.actionable_issues || [],
    open_issues_by_class: extra.open_issues_by_class || emptyIssues(),
    missing_required_sheets: extra.missing_required_sheets || [],
    remaining_issues_path: extra.remaining_issues_path || null,
    summary_10lines: reason,
    run_id: extra.run_id || null,
    failure_reason: reason,
  })

  // -------------------------------------------------------------------------
  // 阶段 1：建模准备（核对起点模型与哈希、提取指纹、拉取财报材料与一致预期快照）
  // -------------------------------------------------------------------------
  const p1 = setPhase('建模准备')
  const prepPrompt = [
    `${extraCtx}先读 .agents/skills/dalton-conventions/SKILL.md 与 .agents/skills/dalton-model/SKILL.md${modelSpecPath ? ` 及 ${modelSpecPath}` : ''}。`,
    `为 ${C}（${T}）执行建模准备（模式：${modeReq}${ctx.asOf ? `，基准日：${ctx.asOf}` : ''}${ctx.quarter ? `，财季：${ctx.quarter}` : ''}）：`,
    `1) 确定起点模型：${startModelPath ? `起点路径为 ${startModelPath}` : '按 dalton-model 第 1 步查找用户指定/上传模型或 data/models.json 已登记模型；均无模型时走新建（mode="build"），默认格式模板在 .agents/skills/dalton-model/assets/default_model_template.xlsx'}。${expectedBaseSha ? `若存在起点模型，先核对其 SHA-256 严格等于 ${expectedBaseSha}，不符时在 summary 说明并中止。` : ''}`,
    `2) 有起点模型（mode="update"）时，只读运行 \`python3 .agents/skills/dalton-model/scripts/model_fingerprint.py <起点模型> --ticker "${T}" --out "${fpPath}"\` 提取基线指纹，并检查起点健康度（若预测期已全为硬编码常数或冻结窗格严重错位，在 baseline_health_warning 写明）。新建模型（mode="build"）时对默认模板提取指纹至 "${fpPath}"。`,
    `3) 收集一手财报材料与指引记录，并获取市场一致预期快照：${_mConsensusSourceHint()}`,
    deepNotes
      ? `4) 将财报披露行项与原模型行项的对应关系写入 ${mappingPath}（至少 50 字节），并在 files 中返回 ${mappingPath}。`
      : `4) 机器产物写入 ${dumpDir}/（免语言审查目录）。`,
    `返回 JSON：files、summary、mode（build 或 update）、baseline_path、baseline_sha256、fingerprint_path、consensus_path、baseline_health_warning。`,
  ].join('\n')

  const prep = await _mSafeAgent(prepPrompt, {
    agentType: 'general-purpose',
    label: '建模准备',
    phase: p1,
    schema: _MODEL_PREP_SCHEMA,
  })
  if (!prep) {
    return failUnusable('建模准备阶段失败，未产出基线指纹与准备材料', ['prepare'])
  }

  const effMode = prep.mode === 'build' ? 'build' : 'update'
  const baselinePath = _mNormPath(prep.baseline_path || startModelPath, W)
  const baselineSha = _mValidSha(prep.baseline_sha256)
  const fingerprintPath = _mNormPath(prep.fingerprint_path || fpPath, W) || fpPath
  const consensusPath = _mNormPath(prep.consensus_path, W)
  const artifacts = []
  const failedStages = []
  if (deepNotes) {
    if (_mHasPath(prep.files, mappingPath, W)) {
      _mPushUnique(artifacts, mappingPath)
    } else {
      _mPushUnique(failedStages, 'mapping')
    }
  }

  // 独立非重放读取本轮 run_id：避免续跑时复用旧 run_id（§3.2 & journal.py）
  let runId = await _mReadLiveRunId(progressPath, p1)

  // -------------------------------------------------------------------------
  // 阶段 2a：受控原地建模并先确认候选模型落盘（与一致预期对照拆开，避免后续超时丢候选）
  // -------------------------------------------------------------------------
  const p2 = setPhase('建模与一致预期对照')
  const thresholdArg = consensusThresholds
    ? ` --thresholds '${typeof consensusThresholds === 'string' ? consensusThresholds : JSON.stringify(consensusThresholds)}'`
    : ''
  const editSourcePath = baselinePath || '.agents/skills/dalton-model/assets/default_model_template.xlsx'
  const stage2aPrompt = [
    `${extraCtx}先读 .agents/skills/dalton-model/SKILL.md、.agents/skills/dalton-model/references/forecast-rules.md、.agents/skills/dalton-model/references/rollforward.md 与 .agents/skills/dalton-model/references/format-spec.md${modelSpecPath ? `，以及 ${modelSpecPath}` : ''}。`,
    `目标公司：${C}（${T}）；模式：${effMode}；起点模型：${editSourcePath}；基线指纹：${fingerprintPath}。`,
    `执行独立建模与受控原地编辑（先落盘并确认可读候选模型，暂不写一致预期快照）：`,
    `- 完整遵守既有建模约定：预测期金额全部用公式算出、由季度汇总的年度列按行项类型写公式、季度预测覆盖至下一财年 Q4、年度预测覆盖至少 3 个完整财年（下一财年各季度加之后两个完整财年），原模型保持只读；多公司行业整本只改 ${T} 所属工作表与变更记录，保留其他公司工作表原状。`,
    `- 准备编辑计划 JSON，统一经由 \`python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline "${editSourcePath}" --ticker "${T}" --plan "<edit_plan.json>" --workspace-dir "${W}" --fingerprint "${fingerprintPath}" --coord-map-out "${coordMapPath}" --mode "${effMode}"${expectedBaseSha ? ` --expected-baseline-sha256 "${expectedBaseSha}"` : ''}\` 执行受控原地编辑、落盘前母本缓存恢复与 LibreOffice 临时副本复算写回。`,
    `返回 JSON：files（含落盘后的候选模型路径与 ${coordMapPath}）、summary、candidate_path、model_sha256（64 位）、recalc_status。`,
  ].join('\n')

  const stage2a = await _mSafeAgent(stage2aPrompt, {
    agentType: 'general-purpose',
    label: '模型构建与落盘',
    phase: p2,
    schema: _MODEL_STAGE2A_SCHEMA,
  })
  const stage2aCand = stage2a ? _mNormPath(stage2a.candidate_path, W) : null
  if (!stage2a || !stage2aCand || !_mHasPath(stage2a.files, stage2aCand, W)) {
    return failUnusable('模型构建与落盘阶段未产出可读候选模型', [...failedStages, 'model'], {
      baseline_path: baselinePath,
      baseline_sha256: baselineSha,
      consensus_path: consensusPath,
      artifacts,
      run_id: runId,
    })
  }

  let candidatePath = stage2aCand
  let modelSha = _mValidSha(stage2a.model_sha256)
  if (stage2a.recalc_status === 'recalc_failed') _mPushUnique(failedStages, 'recalc')

  // -------------------------------------------------------------------------
  // 阶段 2b：一致预期快照隔离与差异对照（超时或返空时候选已落盘，记 consensus 降级并继续交付）
  // -------------------------------------------------------------------------
  const stage2bPrompt = [
    `${extraCtx}先读 .agents/skills/dalton-model/references/sanity-check.md。`,
    `对已落盘的 ${C}（${T}）候选模型 ${candidatePath}${modelSha ? `（sha256：${modelSha}）` : ''} 执行一致预期快照写入与差异对照：`,
    `1) 用 \`python3 .agents/skills/dalton-model/scripts/model_consensus.py write-snapshot --model "${candidatePath}" --consensus "${consensusPath || 'data/consensus/<最新文件>.json'}" --coord-map "${coordMapPath}"\` 把外部参考值写入独立快照区（不把一致预期硬编码为本方预测或结果公式依赖）。`,
    `2) 运行 \`python3 .agents/skills/dalton-model/scripts/model_consensus.py diff --model "${candidatePath}" --ticker "${T}" --research-dir "${R}"${consensusPath ? ` --consensus "${consensusPath}"` : ''}${thresholdArg}\` 生成 ${consensusMdPath} 与 ${consensusJsonPath}。`,
    `3) 若核心指标超阈值（营业总收入 ±5%、利润与稀释 EPS ±10%、近盈亏平衡 ±150bp），拆到分部与驱动假设做一次返工复核：自身假设遗漏或口径有误时，先对当前候选重提指纹，再用 model_edit.py apply --baseline "${candidatePath}" --out "${candidatePath}" --in-place --prior-coord-map "${coordMapPath}" --coord-map-out "${coordMapPath}" 只改驱动假设并刷新差异表；有可核实独立依据则保留并在对照表与变更记录写明一句具体理由。`,
    `返回 JSON：files（含 ${candidatePath}、${consensusMdPath} 与 ${consensusJsonPath}）、summary、candidate_path、model_sha256（64 位）、recalc_status、consensus_status、consensus_diff_generated。`,
  ].join('\n')

  const stage2b = await _mSafeAgent(stage2bPrompt, {
    agentType: 'general-purpose',
    label: '一致预期对照',
    phase: p2,
    schema: _MODEL_STAGE2B_SCHEMA,
  })
  if (!stage2b) {
    _mPushUnique(failedStages, 'consensus')
  } else {
    const s2bCand = _mNormPath(stage2b.candidate_path, W)
    if (s2bCand && _mHasPath(stage2b.files, s2bCand, W)) {
      candidatePath = s2bCand
    }
    modelSha = _mValidSha(stage2b.model_sha256) || modelSha
    if (stage2b.recalc_status === 'recalc_failed') _mPushUnique(failedStages, 'recalc')
    const consMdOk = _mHasPath(stage2b.files, consensusMdPath, W)
    const consJsonOk = _mHasPath(stage2b.files, consensusJsonPath, W)
    if (stage2b.consensus_diff_generated === false || !consMdOk || !consJsonOk) {
      _mPushUnique(failedStages, 'consensus')
    }
    if (consMdOk) _mPushUnique(artifacts, consensusMdPath)
    if (consJsonOk) _mPushUnique(artifacts, consensusJsonPath)
  }

  // -------------------------------------------------------------------------
  // 阶段 3 & 阶段 6：本轮范围检查 + 仅「本轮可修」交回修改（最多两轮）
  // -------------------------------------------------------------------------
  let fixRounds = 0
  let finalStatus = 'delivered_with_open_issues'
  let actionableIssues = []
  let openByClass = emptyIssues()
  let missingRequiredSheets = []
  let remainingIssuesPath = null
  let summary10 = (stage2b && stage2b.summary) || stage2a.summary || ''

  while (true) {
    const chkPhase = setPhase(fixRounds === 0 ? '模型本轮范围检查' : `模型本轮范围复查 ${fixRounds}`)
    const chkLabel = fixRounds === 0 ? '模型本轮范围检查' : `模型本轮范围复查 ${fixRounds}`

    const chkCmdParts = [
      `python3 .agents/skills/dalton-model/scripts/model_check.py check`,
      `--workspace "${W}"`,
      `--ticker "${T}"`,
      `--research-dir "${R}"`,
      `--candidate "${candidatePath}"`,
      `--workflow "${wfName}"`,
      `--mode "${effMode}"`,
      `--fix-round ${fixRounds}`,
      `--coord-map "${coordMapPath}"`,
    ]
    if (baselinePath) chkCmdParts.push(`--baseline "${baselinePath}"`)
    if (fingerprintPath) chkCmdParts.push(`--fingerprint "${fingerprintPath}"`)
    if (runId) chkCmdParts.push(`--run-id "${runId}"`)
    if (failedStages.length) chkCmdParts.push(`--failed-stages "${failedStages.join(',')}"`)

    const chkPrompt = [
      `对 ${T} 候选模型 ${candidatePath} 执行第 ${fixRounds} 轮本轮范围格式与结构检查（不修改模型文件）。`,
      `在 Bash 运行：\`${chkCmdParts.join(' ')}\`。`,
      `核对生成的 ${checkMdPath}、${reviewPackPath}、${ledgerPath}（以及存在剩余问题时的 ${remPath}）。`,
      `按脚本 JSON 输出返回：files、summary、readable、final_status、model_sha256、actionable_count、actionable_issues、baseline_legacy_issues、missing_evidence_issues、not_applicable_issues、missing_required_sheets、remaining_issues_path、summary_10lines。`,
    ].join('\n')

    const chk = await _mSafeAgent(chkPrompt, {
      agentType: 'general-purpose',
      label: chkLabel,
      phase: chkPhase,
      schema: _MODEL_CHECK_SCHEMA,
    })

    const chkLedgerOk = Boolean(chk && _mHasPath(chk.files, ledgerPath, W))
    const chkRemOk = Boolean(chk && _mHasPath(chk.files, remPath, W))
    const chkDeliverableConfirmed =
      Boolean(chk) &&
      chk.readable &&
      chk.final_status !== 'failed_unusable' &&
      chkLedgerOk &&
      (chk.final_status === 'pass' || chkRemOk)

    if (!chk || (chk.readable && chk.final_status !== 'failed_unusable' && !chkDeliverableConfirmed)) {
      _mPushUnique(failedStages, 'check')
      const fb = await writeFallbackLedger({
        workspaceDir: W,
        ticker: T,
        researchDir: R,
        progressPath,
        workflow: wfName,
        candidatePath,
        baselinePath,
        kind: 'modeling',
        failedStages,
        runId,
        fixRounds,
        reason: `${chkLabel}超时或未确认账本落盘，已保留可读候选并降级写账本`,
        phaseLabel: chkPhase,
      })
      runId = fb.run_id || runId
      modelSha = fb.model_sha256 || modelSha
      for (const a of fb.artifacts) _mPushUnique(artifacts, a)
      if (!fb.ok || fb.final_status === 'failed_unusable') {
        return failUnusable('降级账本与未决清单未确认落盘，无法判定可交付', failedStages, {
          candidate_path: candidatePath,
          model_sha256: modelSha,
          baseline_path: baselinePath,
          baseline_sha256: baselineSha,
          consensus_path: consensusPath,
          fix_rounds: fixRounds,
          artifacts,
          run_id: runId,
        })
      }
      finalStatus = fb.final_status
      remainingIssuesPath = fb.remaining_issues_path
      break
    }

    if (!chk.readable || chk.final_status === 'failed_unusable') {
      return failUnusable(chk.summary || '候选模型不可读，无法交付', [...failedStages, 'check'], {
        baseline_path: baselinePath,
        baseline_sha256: baselineSha,
        consensus_path: consensusPath,
        fix_rounds: fixRounds,
        artifacts,
        run_id: runId,
      })
    }

    modelSha = _mValidSha(chk.model_sha256) || modelSha
    actionableIssues = Array.isArray(chk.actionable_issues) ? chk.actionable_issues : []
    openByClass = {
      actionable: actionableIssues,
      baseline_legacy: Array.isArray(chk.baseline_legacy_issues) ? chk.baseline_legacy_issues : [],
      missing_evidence: Array.isArray(chk.missing_evidence_issues) ? chk.missing_evidence_issues : [],
      not_applicable: Array.isArray(chk.not_applicable_issues) ? chk.not_applicable_issues : [],
    }
    missingRequiredSheets = Array.isArray(chk.missing_required_sheets) ? chk.missing_required_sheets : []
    summary10 = chk.summary_10lines || chk.summary || summary10
    if (chkLedgerOk) _mPushUnique(artifacts, ledgerPath)
    if (_mHasPath(chk.files, checkMdPath, W)) _mPushUnique(artifacts, checkMdPath)
    if (_mHasPath(chk.files, reviewPackPath, W)) _mPushUnique(artifacts, reviewPackPath)

    const actionableCount = typeof chk.actionable_count === 'number' ? chk.actionable_count : actionableIssues.length
    if (failedStages.length === 0 && actionableCount === 0 && chk.final_status === 'pass') {
      finalStatus = 'pass'
      remainingIssuesPath = null
      break
    }

    if (actionableCount > 0 && fixRounds < 2 && failedStages.length === 0) {
      fixRounds += 1
      const fixPhase = setPhase(`模型修改 ${fixRounds}`)
      const fixFpPath = `${dumpDir}/${T}-fix${fixRounds}-fingerprint.json`
      const sameAsStart = Boolean(baselinePath && _mNormPath(candidatePath, W) === _mNormPath(baselinePath, W))
      const editInPlaceArgs = sameAsStart
        ? `--coord-map-out "${coordMapPath}"`
        : `--out "${candidatePath}" --in-place --prior-coord-map "${coordMapPath}" --coord-map-out "${coordMapPath}"`
      const fixShaArg = modelSha ? ` --expected-baseline-sha256 "${modelSha}"` : ''
      const fixPrompt = [
        `根据本轮范围检查报告 ${checkMdPath}，仅修复以下「本轮可修（actionable）」问题（第 ${fixRounds}/2 轮；不改动基线遗留、缺证据与不适用项）：`,
        actionableIssues.map((it) => `- ${it}`).join('\n'),
        `操作要求（必须以当前候选 ${candidatePath} 为基础应用增量，保留前序写入的实际值、扩列与预测）：`,
        `1) 先对当前候选重新提取指纹：\`python3 .agents/skills/dalton-model/scripts/model_fingerprint.py "${candidatePath}" --ticker "${T}" --out "${fixFpPath}"\`；`,
        `2) 通过 \`python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline "${candidatePath}" --ticker "${T}" --plan "<fix_plan_${fixRounds}.json>" ${editInPlaceArgs} --workspace-dir "${W}" --fingerprint "${fixFpPath}"${fixShaArg} --mode "${effMode}"\` 在当前候选模型上定点应用增量并合并坐标映射，完成母本缓存恢复与复算写回；`,
        `3) 按最新模型哈希重跑 \`python3 .agents/skills/dalton-model/scripts/model_consensus.py diff --model "<最新候选路径>" --ticker "${T}" --research-dir "${R}"${consensusPath ? ` --consensus "${consensusPath}"` : ''}${thresholdArg}\` 刷新 ${consensusMdPath} 与 ${consensusJsonPath}；`,
        `4) 返回更新后的 files、candidate_path、model_sha256、recalc_status 与 consensus_diff_generated。`,
      ].join('\n')

      const fixRes = await _mSafeAgent(fixPrompt, {
        agentType: 'general-purpose',
        label: `模型修改 ${fixRounds}`,
        phase: fixPhase,
        schema: _MODEL_FIX_SCHEMA,
      })

      const fixCand = fixRes ? _mNormPath(fixRes.candidate_path, W) : null
      if (!fixRes || !fixCand || !_mHasPath(fixRes.files, fixCand, W)) {
        _mPushUnique(failedStages, 'fix')
        const fb = await writeFallbackLedger({
          workspaceDir: W,
          ticker: T,
          researchDir: R,
          progressPath,
          workflow: wfName,
          candidatePath,
          baselinePath,
          kind: 'modeling',
          failedStages,
          runId,
          fixRounds,
          reason: `第 ${fixRounds} 轮模型修改超时或返回空，已保留上一可读候选并降级交付`,
          phaseLabel: fixPhase,
        })
        runId = fb.run_id || runId
        modelSha = fb.model_sha256 || modelSha
        for (const a of fb.artifacts) _mPushUnique(artifacts, a)
        if (!fb.ok || fb.final_status === 'failed_unusable') {
          return failUnusable('修改阶段超时且降级账本未确认落盘', failedStages, {
            candidate_path: candidatePath,
            model_sha256: modelSha,
            baseline_path: baselinePath,
            baseline_sha256: baselineSha,
            consensus_path: consensusPath,
            fix_rounds: fixRounds,
            artifacts,
            run_id: runId,
          })
        }
        finalStatus = fb.final_status
        remainingIssuesPath = fb.remaining_issues_path
        break
      }

      candidatePath = fixCand
      modelSha = _mValidSha(fixRes.model_sha256) || modelSha
      if (fixRes.recalc_status === 'recalc_failed') _mPushUnique(failedStages, 'recalc')
      if (fixRes.consensus_diff_generated === false) _mPushUnique(failedStages, 'consensus')
      if (_mHasPath(fixRes.files, consensusMdPath, W)) _mPushUnique(artifacts, consensusMdPath)
      if (_mHasPath(fixRes.files, consensusJsonPath, W)) _mPushUnique(artifacts, consensusJsonPath)
      continue
    }

    finalStatus = 'delivered_with_open_issues'
    remainingIssuesPath = _mNormPath(chk.remaining_issues_path, W) || (chkRemOk ? remPath : null)
    if (remainingIssuesPath && _mHasPath(chk.files, remainingIssuesPath, W)) {
      _mPushUnique(artifacts, remainingIssuesPath)
    }
    break
  }

  // -------------------------------------------------------------------------
  // 深度链笔记：model-delta.md（在可读候选确立后生成；超时则写降级账本记 model_delta）
  // -------------------------------------------------------------------------
  if (deepNotes && candidatePath && finalStatus !== 'failed_unusable') {
    const deltaPhase = setPhase('模型变动拆解')
    const deltaPrompt = [
      `${extraCtx}基于最终候选模型 ${candidatePath}（sha256：${modelSha || '见账本'}）与起点模型 ${baselinePath || startModelPath}，结合 ${checkMdPath} 与 ${consensusMdPath}，撰写新旧版本每股收益（EPS）变动拆解与估值变化说明至 ${modelDeltaPath}（至少 50 字节；不修改模型文件）。`,
      `返回 JSON：files（含 ${modelDeltaPath}）与 summary。`,
    ].join('\n')

    const deltaRes = await _mSafeAgent(deltaPrompt, {
      agentType: 'general-purpose',
      label: '模型变动拆解',
      phase: deltaPhase,
      schema: _MODEL_NOTE_SCHEMA,
    })

    if (deltaRes && _mHasPath(deltaRes.files, modelDeltaPath, W)) {
      _mPushUnique(artifacts, modelDeltaPath)
    } else {
      _mPushUnique(failedStages, 'model_delta')
      const fb = await writeFallbackLedger({
        workspaceDir: W,
        ticker: T,
        researchDir: R,
        progressPath,
        workflow: wfName,
        candidatePath,
        baselinePath,
        kind: 'modeling',
        failedStages,
        runId,
        fixRounds,
        reason: '模型变动拆解（model-delta.md）生成超时或未产出，已保留可读候选并按带问题交付',
        phaseLabel: deltaPhase,
      })
      runId = fb.run_id || runId
      modelSha = fb.model_sha256 || modelSha
      for (const a of fb.artifacts) _mPushUnique(artifacts, a)
      if (!fb.ok || fb.final_status === 'failed_unusable') {
        return failUnusable('model-delta.md 缺失且降级账本未确认落盘', failedStages, {
          candidate_path: candidatePath,
          model_sha256: modelSha,
          baseline_path: baselinePath,
          baseline_sha256: baselineSha,
          consensus_path: consensusPath,
          fix_rounds: fixRounds,
          artifacts,
          run_id: runId,
        })
      }
      finalStatus = fb.final_status
      remainingIssuesPath = fb.remaining_issues_path
    }
  }

  return {
    ok: finalStatus === 'pass' || finalStatus === 'delivered_with_open_issues',
    status: finalStatus,
    candidate_path: candidatePath,
    model_sha256: modelSha,
    baseline_path: baselinePath,
    baseline_sha256: baselineSha,
    consensus_path: consensusPath,
    fix_rounds: fixRounds,
    failed_stages: failedStages,
    artifacts,
    actionable_issues: actionableIssues,
    open_issues_by_class: openByClass,
    missing_required_sheets: missingRequiredSheets,
    remaining_issues_path: remainingIssuesPath,
    summary_10lines: summary10,
    run_id: runId,
    failure_reason: null,
  }
}

async function reviseModelForReport(ctx) {
  const W = ctx.workspaceDir || '.'
  const T = String(ctx.ticker || '').trim().toUpperCase()
  const R = ctx.researchDir
  const progressPath = ctx.progressPath || `${R}/.progress.json`
  const wfName = ctx.workflow || 'full-coverage'
  const currentModelPath = _mNormPath(ctx.currentModelPath, W)
  const currentModelSha = _mValidSha(ctx.currentModelSha256)
  const currentStatus = ctx.currentStatus || 'pass'
  const currentArtifacts = Array.isArray(ctx.currentArtifacts) ? ctx.currentArtifacts.slice() : []
  const consensusPath = _mNormPath(ctx.consensusPath, W)
  const consensusThresholds = ctx.consensusThresholds || null
  const priorFailedStages = Array.isArray(ctx.priorFailedStages) ? ctx.priorFailedStages : []
  const reportPath = ctx.reportPath
  const reviewFeedback = ctx.reviewFeedback
  const extraSyncPaths = Array.isArray(ctx.extraSyncPaths) ? ctx.extraSyncPaths.filter(Boolean) : []
  const phaseLabel = ctx.phaseLabel || '修订'
  let runId = ctx.runId && String(ctx.runId).trim() ? String(ctx.runId).trim() : null
  if (!runId) {
    runId = await _mReadLiveRunId(progressPath, phaseLabel)
  }

  // Carry forward still-unrecovered deep-update note failures (mapping / model_delta)
  const carriedNoteFailures = []
  for (const st of ['mapping', 'model_delta']) {
    if (priorFailedStages.includes(st)) _mPushUnique(carriedNoteFailures, st)
  }

  const dumpDir = `${R}/model_dump`
  const ledgerPath = `${dumpDir}/model-verification.json`
  const checkMdPath = `${dumpDir}/model-check.md`
  const reviewPackPath = `${dumpDir}/model-review-pack.md`
  const consensusMdPath = `${dumpDir}/consensus-diff.md`
  const consensusJsonPath = `${dumpDir}/consensus-diff.json`
  const remPath = `${dumpDir}/remaining-issues.md`
  const coordMapPath = `${dumpDir}/coord_map.json`
  const revFpPath = `${dumpDir}/${T}-rev-fingerprint.json`

  const thresholdArg = consensusThresholds
    ? ` --thresholds '${typeof consensusThresholds === 'string' ? consensusThresholds : JSON.stringify(consensusThresholds)}'`
    : ''
  const syncClause = extraSyncPaths.length
    ? `以及 ${extraSyncPaths.join('、')}`
    : ''
  const baseShaArg = currentModelSha ? ` --expected-baseline-sha256 "${currentModelSha}"` : ''

  // -------------------------------------------------------------------------
  // 派发 1：改模落盘 + 刷新差异表 + 同步报告引用并回传实测报告哈希
  // 子任务 label 固定使用「模型联动修订」，避免命中 check_done 的审稿收集正则（审稿|复审）
  // -------------------------------------------------------------------------
  const editPrompt = [
    `评估报告审稿意见是否需要联动修改模型（目标公司 ${T}，当前模型 ${currentModelPath}${currentModelSha ? `，sha256：${currentModelSha}` : ''}，报告 ${reportPath}）。`,
    `1) 若审稿意见仅涉及文字表达、无需改动模型数字或驱动假设：核对 ${currentModelPath} 哈希未变，不修改模型与报告文件，直接返回 changed=false、model_path="${currentModelPath}"、model_sha256、report_sha256=null。`,
    `2) 若审稿意见要求调整模型数字、驱动假设或勾稽（先改模落盘并刷新差异表，检查在下一步单独执行）：`,
    `   a) 先对当前候选模型重新提取指纹：\`python3 .agents/skills/dalton-model/scripts/model_fingerprint.py "${currentModelPath}" --ticker "${T}" --out "${revFpPath}"\`；`,
    `   b) 按 .agents/skills/dalton-model/SKILL.md 与 .agents/skills/dalton-model/references/rollforward.md，保持原件只读，调用 \`python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline "${currentModelPath}" --ticker "${T}" --plan "<report_rev_plan.json>" --workspace-dir "${W}" --fingerprint "${revFpPath}"${baseShaArg} --coord-map-out "${coordMapPath}" --mode update\` 按原版本链规则另存下一版本并完成落盘前母本缓存恢复与复算写回；`,
    `   c) 运行 \`python3 .agents/skills/dalton-model/scripts/model_consensus.py diff --model "<新模型路径>" --ticker "${T}" --research-dir "${R}"${consensusPath ? ` --consensus "${consensusPath}"` : ''}${thresholdArg}\` 刷新一致预期差异表 ${consensusMdPath} 与 ${consensusJsonPath}；`,
    `   d) 同步更新 ${reportPath} 正文中的 model: 引用${syncClause}，使其指向新模型路径与新哈希（JSON 元数据文件写入 {"path": "<新模型路径>", "sha256": "<新哈希>"}），随后在 Bash 执行 \`sha256sum "${reportPath}"\` 并在返回 JSON 的 report_sha256 字段回传该 64 位实测哈希（未改写报告则回传 null）。`,
    `审稿意见：\n${typeof reviewFeedback === 'string' ? reviewFeedback : JSON.stringify(reviewFeedback)}`,
    `返回 JSON：files（含新模型路径、${consensusMdPath}、${consensusJsonPath} 及已改写报告路径）、summary、changed、model_path、model_sha256、report_sha256、recalc_status、consensus_diff_generated。`,
  ].join('\n')

  const editRes = await _mSafeAgent(editPrompt, {
    agentType: 'general-purpose',
    label: '模型联动修订',
    phase: phaseLabel,
    schema: _MODEL_REV_EDIT_SCHEMA,
  })

  if (!editRes) {
    const failedStages = [...carriedNoteFailures]
    _mPushUnique(failedStages, 'check')
    if (!currentModelPath) {
      return {
        ok: false,
        changed: false,
        modelPath: null,
        modelSha256: null,
        reportSha256: null,
        status: 'failed_unusable',
        openIssues: ['报告联动改模子任务超时且无可读候选模型'],
        artifacts: currentArtifacts,
        failedStages,
        remainingIssuesPath: null,
        runId,
      }
    }
    const fb = await writeFallbackLedger({
      workspaceDir: W,
      ticker: T,
      researchDir: R,
      progressPath,
      workflow: wfName,
      candidatePath: currentModelPath,
      baselinePath: currentModelPath,
      kind: 'report_revision',
      failedStages,
      runId,
      reason: '报告联动改模落盘子任务超时或返回空，已保留当前可读模型并补写降级账本',
      phaseLabel,
    })
    const arts = currentArtifacts.slice()
    for (const a of fb.artifacts) _mPushUnique(arts, a)
    if (!fb.ok || fb.final_status === 'failed_unusable') {
      return {
        ok: false,
        changed: false,
        modelPath: currentModelPath,
        modelSha256: fb.model_sha256 || currentModelSha,
        reportSha256: null,
        status: 'failed_unusable',
        openIssues: ['报告联动改模超时且降级账本未确认落盘'],
        artifacts: arts,
        failedStages,
        remainingIssuesPath: fb.remaining_issues_path,
        runId: fb.run_id || runId,
      }
    }
    return {
      ok: true,
      changed: false,
      modelPath: currentModelPath,
      modelSha256: fb.model_sha256 || currentModelSha,
      reportSha256: null,
      status: fb.final_status,
      openIssues: ['报告联动改模落盘子任务超时或返回空'],
      artifacts: arts,
      failedStages,
      remainingIssuesPath: fb.remaining_issues_path,
      runId: fb.run_id || runId,
    }
  }

  const reportSha256 = _mValidSha(editRes.report_sha256)

  if (!editRes.changed) {
    return {
      ok: true,
      changed: false,
      modelPath: currentModelPath,
      modelSha256: _mValidSha(editRes.model_sha256) || currentModelSha,
      reportSha256,
      status: currentStatus,
      openIssues: [],
      artifacts: currentArtifacts,
      failedStages: carriedNoteFailures,
      remainingIssuesPath: currentStatus === 'delivered_with_open_issues' ? remPath : null,
      runId,
    }
  }

  const explicitNextPath = _mNormPath(editRes.model_path, W)
  const nextPathConfirmed = Boolean(explicitNextPath && _mHasPath(editRes.files, explicitNextPath, W))
  if (!explicitNextPath || !nextPathConfirmed) {
    // No readable new candidate confirmed: never call write-ledger with --candidate "null".
    return {
      ok: false,
      changed: true,
      modelPath: null,
      modelSha256: null,
      reportSha256,
      status: 'failed_unusable',
      openIssues: ['报告联动改模未能产出并确认可读新候选模型'],
      artifacts: currentArtifacts,
      failedStages: [...carriedNoteFailures, 'check'],
      remainingIssuesPath: null,
      runId,
    }
  }

  const nextPath = explicitNextPath
  let nextSha = _mValidSha(editRes.model_sha256)
  const failedStages = [...carriedNoteFailures]
  if (editRes.recalc_status === 'recalc_failed') _mPushUnique(failedStages, 'recalc')
  const consMdOk = _mHasPath(editRes.files, consensusMdPath, W)
  const consJsonOk = _mHasPath(editRes.files, consensusJsonPath, W)
  if (editRes.consensus_diff_generated === false || !consMdOk || !consJsonOk) {
    _mPushUnique(failedStages, 'consensus')
  }

  // -------------------------------------------------------------------------
  // 派发 2：单遍本轮范围检查（append kind="report_revision"）；超时时按新候选路径写降级账本
  // -------------------------------------------------------------------------
  const chkCmdParts = [
    `python3 .agents/skills/dalton-model/scripts/model_check.py check`,
    `--workspace "${W}"`,
    `--ticker "${T}"`,
    `--research-dir "${R}"`,
    `--candidate "${nextPath}"`,
    `--baseline "${currentModelPath}"`,
    `--fingerprint "${revFpPath}"`,
    `--coord-map "${coordMapPath}"`,
    `--workflow "${wfName}"`,
    `--kind report_revision`,
    `--append-revision`,
  ]
  if (runId) chkCmdParts.push(`--run-id "${runId}"`)
  if (failedStages.length) chkCmdParts.push(`--failed-stages "${failedStages.join(',')}"`)

  const chkPrompt = [
    `对报告联动改模已落盘的新候选 ${nextPath}${nextSha ? `（sha256：${nextSha}）` : ''} 执行单遍本轮范围检查（不修改模型文件）。`,
    `在 Bash 运行：\`${chkCmdParts.join(' ')}\`（追加 kind="report_revision" 轮次记录，不计入 fix_rounds 两轮上限）。`,
    `核对生成的 ${checkMdPath}、${reviewPackPath}、${ledgerPath}（以及存在剩余问题时的 ${remPath}）。`,
    `按脚本 JSON 输出返回：files、summary、readable、final_status、model_sha256、actionable_count、actionable_issues、baseline_legacy_issues、missing_evidence_issues、not_applicable_issues、missing_required_sheets、remaining_issues_path、summary_10lines。`,
  ].join('\n')

  const chk = await _mSafeAgent(chkPrompt, {
    agentType: 'general-purpose',
    label: '模型联动修订检查',
    phase: phaseLabel,
    schema: _MODEL_CHECK_SCHEMA,
  })

  const chkLedgerOk = Boolean(chk && _mHasPath(chk.files, ledgerPath, W))
  const chkRemOk = Boolean(chk && _mHasPath(chk.files, remPath, W))
  const chkDeliverableConfirmed =
    Boolean(chk) &&
    chk.readable &&
    chk.final_status !== 'failed_unusable' &&
    chkLedgerOk &&
    (chk.final_status === 'pass' || chkRemOk)

  if (!chk || (chk.readable && chk.final_status !== 'failed_unusable' && !chkDeliverableConfirmed)) {
    _mPushUnique(failedStages, 'check')
    const fb = await writeFallbackLedger({
      workspaceDir: W,
      ticker: T,
      researchDir: R,
      progressPath,
      workflow: wfName,
      candidatePath: nextPath,
      baselinePath: currentModelPath,
      kind: 'report_revision',
      failedStages,
      runId,
      reason: '报告联动改模检查超时或未确认账本落盘，已保留已落盘新候选并补写降级账本',
      phaseLabel,
    })
    runId = fb.run_id || runId
    nextSha = fb.model_sha256 || nextSha
    const arts = currentArtifacts.filter((p) => {
      const n = _mNormPath(p, W)
      if (n === _mNormPath(checkMdPath, W)) return false
      if (!consMdOk && n === _mNormPath(consensusMdPath, W)) return false
      if (!consJsonOk && n === _mNormPath(consensusJsonPath, W)) return false
      return true
    })
    if (consMdOk) _mPushUnique(arts, consensusMdPath)
    if (consJsonOk) _mPushUnique(arts, consensusJsonPath)
    for (const a of fb.artifacts) _mPushUnique(arts, a)
    if (!fb.ok || fb.final_status === 'failed_unusable') {
      return {
        ok: false,
        changed: true,
        modelPath: nextPath,
        modelSha256: nextSha,
        reportSha256,
        status: 'failed_unusable',
        openIssues: ['报告联动改模检查超时且降级账本未确认落盘'],
        artifacts: arts,
        failedStages,
        remainingIssuesPath: fb.remaining_issues_path,
        runId,
      }
    }
    return {
      ok: true,
      changed: true,
      modelPath: nextPath,
      modelSha256: nextSha,
      reportSha256,
      status: fb.final_status,
      openIssues: ['报告联动改模检查超时，已保留新候选并写入降级账本'],
      artifacts: arts,
      failedStages,
      remainingIssuesPath: fb.remaining_issues_path,
      runId,
    }
  }

  if (!chk.readable || chk.final_status === 'failed_unusable') {
    return {
      ok: false,
      changed: true,
      modelPath: null,
      modelSha256: null,
      reportSha256,
      status: 'failed_unusable',
      openIssues: [chk.summary || '报告联动改模产出的新候选模型不可读'],
      artifacts: currentArtifacts,
      failedStages: [...failedStages, 'check'],
      remainingIssuesPath: null,
      runId,
    }
  }

  nextSha = _mValidSha(chk.model_sha256) || nextSha
  const arts = currentArtifacts.filter((p) => {
    const n = _mNormPath(p, W)
    if (!consMdOk && n === _mNormPath(consensusMdPath, W)) return false
    if (!consJsonOk && n === _mNormPath(consensusJsonPath, W)) return false
    return true
  })
  if (chkLedgerOk) _mPushUnique(arts, ledgerPath)
  if (_mHasPath(chk.files, checkMdPath, W)) _mPushUnique(arts, checkMdPath)
  if (_mHasPath(chk.files, reviewPackPath, W)) _mPushUnique(arts, reviewPackPath)
  if (consMdOk) _mPushUnique(arts, consensusMdPath)
  if (consJsonOk) _mPushUnique(arts, consensusJsonPath)

  const st = failedStages.length > 0 && chk.final_status === 'pass' ? 'delivered_with_open_issues' : chk.final_status
  const rem = st === 'delivered_with_open_issues' ? (_mNormPath(chk.remaining_issues_path, W) || (chkRemOk ? remPath : null)) : null
  if (rem && _mHasPath(chk.files, rem, W)) _mPushUnique(arts, rem)

  return {
    ok: st === 'pass' || st === 'delivered_with_open_issues',
    changed: true,
    modelPath: nextPath,
    modelSha256: nextSha,
    reportSha256,
    status: st,
    openIssues: Array.isArray(chk.actionable_issues) ? chk.actionable_issues : [],
    artifacts: arts,
    failedStages,
    remainingIssuesPath: rem,
    runId,
  }
}
