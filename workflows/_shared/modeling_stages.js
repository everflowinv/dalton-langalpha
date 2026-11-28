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
    planned_output_path: { type: ['string', 'null'] },
    baseline_health_warning: { type: ['string', 'null'] },
  },
  required: ['files', 'summary', 'mode'],
}

// Replayable planned output-path binding (written to an independent JSON artifact so resume
// keeps the exact same planned --out path and never increments the model version again).
const _MODEL_NEXT_PATH_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    next_path: { type: ['string', 'null'] },
  },
  required: ['files', 'summary', 'next_path'],
}

// Non-replayable live disk probe for a pre-bound candidate path (carries `path`, no `files`/`file`).
const _MODEL_DISK_PROBE_SCHEMA = {
  type: 'object',
  properties: {
    path: { type: ['string', 'null'] },
    exists: { type: 'boolean' },
    readable: { type: 'boolean' },
    model_sha256: { type: ['string', 'null'] },
    run_matched: { type: ['boolean', 'null'] },
    receipt_run_id: { type: ['string', 'null'] },
  },
  required: ['path', 'exists', 'readable'],
}

// Non-replayable live disk probe for full-coverage required worksheets (carries `candidate_path`, no `files`/`file`).
// Matches `model_check.py check-sheets` stdout JSON 1:1 so a child can return the script output verbatim.
const _MODEL_SHEET_GATE_SCHEMA = {
  type: 'object',
  properties: {
    candidate_path: { type: ['string', 'null'] },
    status: { type: 'string', enum: ['complete', 'missing', 'unverified'] },
    readable: { type: 'boolean' },
    workflow: { type: ['string', 'null'] },
    mode: { type: ['string', 'null'] },
    sheet_names: { type: 'array', items: { type: 'string' } },
    missing_required_sheets: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string', minLength: 1 },
  },
  required: ['candidate_path', 'status', 'sheet_names', 'missing_required_sheets', 'summary'],
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
  required: ['files', 'summary', 'readable', 'final_status', 'actionable_count', 'missing_required_sheets'],
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
    recalc_status: { type: ['string', 'null'] },
  },
  required: ['files', 'summary', 'changed', 'model_path'],
}

const _MODEL_REV_DIFF_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    consensus_diff_generated: { type: 'boolean' },
  },
  required: ['files', 'summary', 'consensus_diff_generated'],
}

const _MODEL_REV_REPORT_SYNC_SCHEMA = {
  type: 'object',
  properties: {
    files: { type: 'array', items: { type: 'string', minLength: 1 } },
    summary: { type: 'string', minLength: 1 },
    report_sha256: { type: ['string', 'null'] },
  },
  required: ['files', 'summary'],
}

// Non-replayable live disk probe for consensus-diff.{json,md} bound to a specific model_sha256.
const _MODEL_CONSENSUS_PROBE_SCHEMA = {
  type: 'object',
  properties: {
    path: { type: ['string', 'null'] },
    json_ok: { type: 'boolean' },
    md_ok: { type: 'boolean' },
    matched: { type: 'boolean' },
  },
  required: ['path', 'json_ok', 'md_ok', 'matched'],
}

// Non-replayable live disk probe for report sync & real report SHA-256 measurement.
const _MODEL_REPORT_SYNC_PROBE_SCHEMA = {
  type: 'object',
  properties: {
    path: { type: ['string', 'null'] },
    exists: { type: 'boolean' },
    synced: { type: 'boolean' },
    report_sha256: { type: ['string', 'null'] },
  },
  required: ['path', 'exists', 'synced'],
}

function _mValidModelOutPath(p, baselinePath, workspaceDir) {
  const raw = _mNormPath(p, workspaceDir)
  if (!raw) return null
  const norm = raw.replace(/\\/g, '/')
  if (norm.startsWith('/')) return null
  const parts = norm.split('/')
  if (parts.some((seg) => !seg || seg === '.' || seg === '..')) return null
  if (parts[0] !== 'model' || parts.length < 2) return null
  if (!/\.xlsx$/i.test(norm)) return null
  const normBase = _mNormPath(baselinePath, workspaceDir)
  if (normBase && norm === normBase.replace(/\\/g, '/')) return null
  return norm
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
  const readOpts = {
    agentType: 'general-purpose',
    label: '读取运行ID',
    phase: phaseTitle || '读取运行ID',
    schema: _MODEL_RUN_ID_SCHEMA,
    checkFiles: false,
  }
  let res = await _mSafeAgent(prompt, readOpts)
  if (!res || typeof res.run_id !== 'string' || !res.run_id.trim()) {
    log('[modeling] 读取运行ID首次失败或返回空，重试一次')
    res = await _mSafeAgent(prompt, readOpts)
  }
  if (res && typeof res.run_id === 'string' && res.run_id.trim()) {
    return res.run_id.trim()
  }
  return null
}

async function _mResolveNextModelPath(W, T, baselinePath, recordPath, phaseTitle, label) {
  const nextCmd = [
    `python3 .agents/skills/dalton-model/scripts/model_edit.py next-path`,
    `--workspace-dir "${W}"`,
    `--ticker "${T}"`,
  ]
  if (baselinePath) nextCmd.push(`--baseline "${baselinePath}"`)
  const prompt = [
    `计算并绑定本轮模型输出路径（不修改任何现有模型文件）。`,
    `1) 在 Bash 运行：\`${nextCmd.join(' ')}\`，读取输出 JSON 的 next_path 字段（转为工作区相对路径，如 model/${T}-model-v01.xlsx）。`,
    `2) 将该绑定结果写入独立记录文件 ${recordPath}（JSON：{"next_path": "<路径>", "baseline_path": ${baselinePath ? JSON.stringify(baselinePath) : 'null'}}）。`,
    `返回 JSON：files（含 ${recordPath}）、summary、next_path。`,
  ].join('\n')
  const res = await _mSafeAgent(prompt, {
    agentType: 'general-purpose',
    label: label || '绑定输出路径',
    phase: phaseTitle || '绑定输出路径',
    schema: _MODEL_NEXT_PATH_SCHEMA,
  })
  if (!res || typeof res.next_path !== 'string') return null
  return _mNormPath(res.next_path, W)
}

async function _mProbeCandidateOnDisk(
  candidatePath,
  W,
  phaseTitle,
  label,
  expectedRunId = null,
  requireReceipt = true,
  progressPath = null,
  dispatchLabel = null
) {
  const norm = _mNormPath(candidatePath, W)
  const wantRunId = typeof expectedRunId === 'string' && expectedRunId.trim() ? expectedRunId.trim() : ''
  const progNorm = _mNormPath(progressPath, W) || ''
  const dispLbl = typeof dispatchLabel === 'string' ? dispatchLabel.trim() : ''
  if (!norm) {
    return {
      verified: true,
      status: 'missing',
      exists: false,
      readable: false,
      model_sha256: null,
      run_matched: false,
      receipt_run_id: null,
    }
  }
  if (requireReceipt && !wantRunId) {
    log(`[modeling] ${label || '核验候选落盘'}缺少本轮 run_id，按未核验处理，不采纳候选`)
    return {
      verified: false,
      status: 'unverified',
      exists: false,
      readable: false,
      model_sha256: null,
      run_matched: false,
      receipt_run_id: null,
    }
  }
  const pyCode = [
    `import json, pathlib, re, sys`,
    `sys.path.insert(0, '.agents/skills/dalton-model/scripts')`,
    `import model_readable, model_fingerprint, model_edit`,
    `p = pathlib.Path(sys.argv[1])`,
    `want_rid = sys.argv[2].strip() if len(sys.argv) > 2 else ''`,
    `prog_arg = sys.argv[3].strip() if len(sys.argv) > 3 else ''`,
    `disp_lbl = sys.argv[4].strip() if len(sys.argv) > 4 else ''`,
    `ex = p.is_file()`,
    `ok, _msg = model_readable.check_workbook_readable(p) if ex else (False, 'missing')`,
    `sha = model_fingerprint.file_sha256(p) if ok else None`,
    `run_matched = None`,
    `receipt_rid = None`,
    `if want_rid:`,
    `    allowed_rids = {want_rid}`,
    `    if prog_arg and disp_lbl:`,
    `        try:`,
    `            pp = pathlib.Path(prog_arg)`,
    `            prog = json.loads(pp.read_text(encoding='utf-8')) if pp.is_file() else None`,
    `            rfrom = str(prog.get('resumed_from') or '').strip() if isinstance(prog, dict) else ''`,
    `            prog_rid = str(prog.get('run_id') or '').strip() if isinstance(prog, dict) else ''`,
    `            if rfrom and (not prog_rid or prog_rid == want_rid):`,
    `                ws_root = pathlib.Path('.').resolve()`,
    `                run_dir_raw = str(prog.get('run_dir') or '').strip()`,
    `                this_dir = pathlib.Path(run_dir_raw) if run_dir_raw else (pathlib.Path(rfrom).parent / want_rid)`,
    `                safe_lbl = re.sub(r'[^0-9A-Za-z_\\u4e00-\\u9fff.-]+', '_', disp_lbl)[:80] or 'dispatch'`,
    `                jf = this_dir / 'journal' / f'{safe_lbl}.json'`,
    `                was_replayed = False`,
    `                if jf.is_file() and jf.resolve().is_relative_to(ws_root):`,
    `                    jd = json.loads(jf.read_text(encoding='utf-8'))`,
    `                    ds = jd.get('dispatches') if isinstance(jd, dict) else None`,
    `                    if isinstance(ds, list) and ds and isinstance(ds[-1], dict):`,
    `                        last = ds[-1]`,
    `                        was_replayed = bool(last.get('status') == 'ok' and isinstance(last.get('resumed_from'), str) and last['resumed_from'].strip())`,
    `                if was_replayed:`,
    `                    cur_dir = rfrom`,
    `                    visited = set()`,
    `                    hops = 0`,
    `                    while cur_dir and hops < 32:`,
    `                        nd = cur_dir.strip().rstrip('/')`,
    `                        if not nd or nd in visited:`,
    `                            break`,
    `                        visited.add(nd)`,
    `                        hops += 1`,
    `                        dp = pathlib.Path(nd)`,
    `                        if not dp.is_dir() or not dp.resolve().is_relative_to(ws_root):`,
    `                            break`,
    `                        anc_rid = None`,
    `                        nxt = None`,
    `                        for meta_name, rid_k in (('resume.json', 'task_id'), ('status.json', 'run_id')):`,
    `                            mf = dp / meta_name`,
    `                            if mf.is_file():`,
    `                                try:`,
    `                                    md = json.loads(mf.read_text(encoding='utf-8'))`,
    `                                    if isinstance(md, dict):`,
    `                                        if not anc_rid and isinstance(md.get(rid_k), str) and md[rid_k].strip():`,
    `                                            anc_rid = md[rid_k].strip()`,
    `                                        if not nxt and isinstance(md.get('resumed_from'), str) and md['resumed_from'].strip():`,
    `                                            nxt = md['resumed_from'].strip()`,
    `                                except Exception:`,
    `                                    pass`,
    `                        if not anc_rid and dp.name:`,
    `                            anc_rid = dp.name`,
    `                        if anc_rid:`,
    `                            allowed_rids.add(anc_rid)`,
    `                        if not nxt and (dp / 'journal').is_dir():`,
    `                            for ajf in sorted((dp / 'journal').glob('*.json')):`,
    `                                try:`,
    `                                    ajd = json.loads(ajf.read_text(encoding='utf-8'))`,
    `                                    for ent in (ajd.get('dispatches') or []) if isinstance(ajd, dict) else []:`,
    `                                        if isinstance(ent, dict) and isinstance(ent.get('resumed_from'), str) and ent['resumed_from'].strip():`,
    `                                            nxt = ent['resumed_from'].strip()`,
    `                                            break`,
    `                                except Exception:`,
    `                                    continue`,
    `                                if nxt:`,
    `                                    break`,
    `                        cur_dir = nxt`,
    `        except Exception:`,
    `            pass`,
    `    run_matched = False`,
    `    if ok and sha:`,
    `        rp = model_edit.publish_receipt_path(p)`,
    `        if rp.is_file():`,
    `            try:`,
    `                rd = json.loads(rp.read_text(encoding='utf-8'))`,
    `                receipt_rid = rd.get('run_id') if isinstance(rd, dict) else None`,
    `                r_sha = str(rd.get('candidate_sha256') or '').strip().lower() if isinstance(rd, dict) else ''`,
    `                r_abs = rd.get('candidate_abs_path') if isinstance(rd, dict) else None`,
    `                r_rel = str(rd.get('candidate_path') or '').lstrip('./') if isinstance(rd, dict) else ''`,
    `                path_ok = (r_rel == sys.argv[1].lstrip('./')) or (isinstance(r_abs, str) and pathlib.Path(r_abs).resolve() == p.resolve())`,
    `                run_matched = bool(receipt_rid in allowed_rids and r_sha == sha.lower() and path_ok)`,
    `            except Exception:`,
    `                run_matched = False`,
    `print(json.dumps({'path': sys.argv[1], 'exists': ex, 'readable': bool(ok), 'model_sha256': sha, 'run_matched': run_matched, 'receipt_run_id': receipt_rid}))`,
  ].join('\n')
  const prompt = [
    `只读核验候选模型 ${norm} 是否已落盘、可读${wantRunId ? `且由本轮（run_id=${wantRunId}，或经日志复用的续跑链）发布` : ''}（不修改任何文件）。`,
    `在 Bash 执行：\n\`\`\`bash\npython3 - "${norm}" "${wantRunId}" "${progNorm}" "${dispLbl}" <<'PY'\n${pyCode}\nPY\n\`\`\`\n将输出 JSON 的 path、exists、readable、model_sha256、run_matched、receipt_run_id 字段原样回传。`,
  ].join('\n')
  const probeOpts = {
    agentType: 'dalton-evidence',
    label: label || '核验候选落盘',
    phase: phaseTitle || '核验候选落盘',
    schema: _MODEL_DISK_PROBE_SCHEMA,
    checkFiles: false,
  }
  let res = await _mSafeAgent(prompt, probeOpts)
  if (!res || typeof res !== 'object') {
    log(`[modeling] ${probeOpts.label}首次超时或无返回，重试一次`)
    res = await _mSafeAgent(prompt, probeOpts)
  }
  if (!res || typeof res !== 'object') {
    return {
      verified: false,
      status: 'unverified',
      exists: false,
      readable: false,
      model_sha256: null,
      run_matched: false,
      receipt_run_id: null,
    }
  }
  const ex = Boolean(res.exists)
  const rd = ex && Boolean(res.readable)
  const sha = rd ? _mValidSha(res.model_sha256) : null
  const runMatched = Boolean(wantRunId && res.run_matched === true)
  return {
    verified: true,
    status: !ex ? 'missing' : (rd && sha ? 'readable' : 'unreadable'),
    exists: ex,
    readable: rd && Boolean(sha),
    model_sha256: sha,
    run_matched: runMatched,
    receipt_run_id: typeof res.receipt_run_id === 'string' && res.receipt_run_id.trim() ? res.receipt_run_id.trim() : null,
  }
}

async function _mProbeConsensusOnDisk(consensusJsonPath, consensusMdPath, expectedModelSha, W, phaseTitle, label) {
  const jNorm = _mNormPath(consensusJsonPath, W)
  const mNorm = _mNormPath(consensusMdPath, W)
  const wantSha = _mValidSha(expectedModelSha)
  if (!jNorm || !mNorm || !wantSha) {
    return { json_ok: false, md_ok: false, matched: false }
  }
  const pyCode = [
    `import json, pathlib, sys`,
    `jp, mp, sha = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), sys.argv[3].lower()`,
    `j_ok = False`,
    `if jp.is_file() and jp.stat().st_size > 0:`,
    `    try: j_ok = str(json.loads(jp.read_text(encoding='utf-8')).get('model_sha256') or '').lower() == sha`,
    `    except Exception: j_ok = False`,
    `m_ok = mp.is_file() and mp.stat().st_size > 0 and (sha[:12] in mp.read_text(encoding='utf-8', errors='replace').lower())`,
    `print(json.dumps({'path': sys.argv[1], 'json_ok': j_ok, 'md_ok': m_ok, 'matched': bool(j_ok and m_ok)}))`,
  ].join('\n')
  const prompt = [
    `只读核验一致预期差异产物 ${jNorm} 与 ${mNorm} 是否已按模型哈希 ${wantSha} 落盘（不修改任何文件）。`,
    `在 Bash 执行：\n\`\`\`bash\npython3 - "${jNorm}" "${mNorm}" "${wantSha}" <<'PY'\n${pyCode}\nPY\n\`\`\`\n将输出 JSON 的 path、json_ok、md_ok、matched 原样回传。`,
  ].join('\n')
  const res = await _mSafeAgent(prompt, {
    agentType: 'dalton-evidence',
    label: label || '核验差异表落盘',
    phase: phaseTitle || '核验差异表落盘',
    schema: _MODEL_CONSENSUS_PROBE_SCHEMA,
    checkFiles: false,
  })
  if (!res || typeof res !== 'object') {
    return { json_ok: false, md_ok: false, matched: false }
  }
  return {
    json_ok: Boolean(res.json_ok),
    md_ok: Boolean(res.md_ok),
    matched: Boolean(res.matched) && Boolean(res.json_ok) && Boolean(res.md_ok),
  }
}

async function _mProbeReportSyncOnDisk(reportPath, nextModelPath, nextModelSha, extraSyncPaths, W, phaseTitle, label) {
  const rNorm = _mNormPath(reportPath, W)
  const mNorm = _mNormPath(nextModelPath, W)
  const mSha = _mValidSha(nextModelSha) || ''
  if (!rNorm || !mNorm) {
    return { exists: false, synced: false, report_sha256: null }
  }
  const extras = (Array.isArray(extraSyncPaths) ? extraSyncPaths : [])
    .map((p) => _mNormPath(p, W))
    .filter(Boolean)
  const pyCode = [
    `import hashlib, json, pathlib, re, sys`,
    `rp, mp, msha = pathlib.Path(sys.argv[1]), sys.argv[2].lstrip('./'), sys.argv[3].lower()`,
    `extras = json.loads(sys.argv[4])`,
    `ex = rp.is_file() and rp.stat().st_size > 0`,
    `rsha = hashlib.sha256(rp.read_bytes()).hexdigest() if ex else None`,
    `path_re = re.compile(r'(?<![A-Za-z0-9_./\\\\-])(?:\\./)?' + re.escape(mp) + r'(?![A-Za-z0-9_./\\\\-])')`,
    `def _md_synced(raw):`,
    `    clean = re.sub(r'<!--.*?-->', '', raw, flags=re.S).split('<!--', 1)[0]`,
    `    refs = [re.sub(r'^(?:\\./)+', '', r) for r in re.findall(r'model:([^\\s!#()\\[\\]<>\"\\x27\\x60]+\\.xlsx)[!#]', clean, re.I)]`,
    `    return all(r == mp for r in refs) and (bool(refs) or bool(path_re.search(clean)))`,
    `synced = False`,
    `if ex:`,
    `    txt = rp.read_text(encoding='utf-8', errors='replace')`,
    `    report_ok = _md_synced(txt)`,
    `    extras_ok = True`,
    `    for ep_s in extras:`,
    `        ep = pathlib.Path(ep_s)`,
    `        if not ep.is_file() or ep.stat().st_size == 0:`,
    `            extras_ok = False; break`,
    `        if ep_s.lower().endswith('.json'):`,
    `            try:`,
    `                d = json.loads(ep.read_text(encoding='utf-8'))`,
    `                if str(d.get('path') or '').lstrip('./') != mp or (msha and str(d.get('sha256') or '').lower() != msha):`,
    `                    extras_ok = False; break`,
    `            except Exception:`,
    `                extras_ok = False; break`,
    `        else:`,
    `            etxt = ep.read_text(encoding='utf-8', errors='replace')`,
    `            if not _md_synced(etxt):`,
    `                extras_ok = False; break`,
    `    synced = bool(report_ok and extras_ok)`,
    `print(json.dumps({'path': sys.argv[1], 'exists': ex, 'synced': synced, 'report_sha256': rsha}))`,
  ].join('\n')
  const prompt = [
    `只读核验报告 ${rNorm}（及元数据 ${extras.length ? extras.join('、') : '无'}）是否已同步指向 ${mNorm} 并实测报告 SHA-256（不修改任何文件）。`,
    `在 Bash 执行：\n\`\`\`bash\npython3 - "${rNorm}" "${mNorm}" "${mSha}" '${JSON.stringify(extras)}' <<'PY'\n${pyCode}\nPY\n\`\`\`\n将输出 JSON 的 path、exists、synced、report_sha256 原样回传。`,
  ].join('\n')
  const res = await _mSafeAgent(prompt, {
    agentType: 'dalton-evidence',
    label: label || '核验报告同步落盘',
    phase: phaseTitle || '核验报告同步落盘',
    schema: _MODEL_REPORT_SYNC_PROBE_SCHEMA,
    checkFiles: false,
  })
  if (!res || typeof res !== 'object') {
    return { exists: false, synced: false, report_sha256: null }
  }
  return {
    exists: Boolean(res.exists),
    synced: Boolean(res.exists) && Boolean(res.synced),
    report_sha256: _mValidSha(res.report_sha256),
  }
}

const _FC_BUILD_REQUIRED_SHEETS_JS = [
  'Cover',
  'Facts',
  'Revenue Model',
  'Income Statement',
  'Cash Flow Statement',
  'Balance Sheet',
  'Scenarios',
  'Guidance vs Actual',
  'Street vs House',
  'DCF Inputs',
  'DCF',
  'Sensitivity Analysis',
  'Comparable Companies',
  'Valuation Summary',
  'Checks',
]
const _FC_UPDATE_REQUIRED_SHEETS_JS = ['Facts', 'Guidance vs Actual', 'Street vs House', 'Checks']

function _mComputeMissingFullCoverageSheets(sheetNames, mode) {
  const lowered = new Set(
    (Array.isArray(sheetNames) ? sheetNames : [])
      .filter((s) => typeof s === 'string' && s.trim())
      .map((s) => s.trim().toLowerCase())
  )
  const missing = []
  const effMode = String(mode || 'update').trim().toLowerCase()
  if (effMode === 'build') {
    let hasChangelog = false
    for (const n of lowered) {
      if (n.includes('readme') || n.includes('change')) {
        hasChangelog = true
        break
      }
    }
    if (!hasChangelog) missing.push('README & Changelog')
    for (const req of _FC_BUILD_REQUIRED_SHEETS_JS) {
      if (!lowered.has(req.toLowerCase())) missing.push(req)
    }
  } else {
    for (const req of _FC_UPDATE_REQUIRED_SHEETS_JS) {
      if (!lowered.has(req.toLowerCase())) missing.push(req)
    }
  }
  return missing
}

async function verifyFullCoverageSheetsOnDisk(ctx) {
  const W = ctx.workspaceDir || '.'
  const candidatePath = _mNormPath(ctx.candidatePath, W)
  const effMode = String(ctx.mode || 'update').trim().toLowerCase() === 'build' ? 'build' : 'update'
  const priorMissing = Array.isArray(ctx.priorMissing)
    ? ctx.priorMissing.filter((s) => typeof s === 'string' && s.trim())
    : []
  const phaseTitle = ctx.phaseLabel || '建模'

  if (!candidatePath) {
    return { status: 'unverified', missing_required_sheets: priorMissing, sheet_names: [] }
  }

  const prompt = [
    `只读独立核验完整覆盖候选模型 ${candidatePath} 的必需工作表（不修改任何文件，不依赖前序检查子任务回传）。`,
    `在 Bash 执行：\`python3 .agents/skills/dalton-model/scripts/model_check.py check-sheets --candidate "${candidatePath}" --workflow full-coverage --mode "${effMode}"\`。`,
    `将脚本输出的 JSON 原样回传（含 candidate_path、status、sheet_names、missing_required_sheets、summary）。`,
  ].join('\n')

  const probe = await _mSafeAgent(prompt, {
    agentType: 'dalton-evidence',
    label: '完整覆盖页签核验',
    phase: phaseTitle,
    schema: _MODEL_SHEET_GATE_SCHEMA,
    checkFiles: false,
  })

  if (!probe || typeof probe !== 'object') {
    return {
      status: priorMissing.length > 0 ? 'missing' : 'unverified',
      missing_required_sheets: priorMissing,
      sheet_names: [],
    }
  }

  const sheetNames = Array.isArray(probe.sheet_names)
    ? probe.sheet_names.filter((s) => typeof s === 'string' && s.trim())
    : []
  const probeMissing = Array.isArray(probe.missing_required_sheets)
    ? probe.missing_required_sheets.filter((s) => typeof s === 'string' && s.trim())
    : []
  const jsMissing = sheetNames.length > 0 ? _mComputeMissingFullCoverageSheets(sheetNames, effMode) : []

  const diskMissing = []
  for (const s of [...probeMissing, ...jsMissing]) {
    _mPushUnique(diskMissing, s)
  }

  if (diskMissing.length > 0 || probe.status === 'missing') {
    return {
      status: 'missing',
      missing_required_sheets: diskMissing.length > 0 ? diskMissing : priorMissing,
      sheet_names: sheetNames,
    }
  }

  if (probe.status === 'complete' && sheetNames.length > 0) {
    // 读盘结果为 complete 且表名核验无缺表时，不并入前序子任务回传的 missing_required_sheets
    return {
      status: 'complete',
      missing_required_sheets: [],
      sheet_names: sheetNames,
    }
  }

  // 探针返回 unverified 或无表名时，仅用 priorMissing 区分缺表与未核验
  if (priorMissing.length > 0) {
    return {
      status: 'missing',
      missing_required_sheets: priorMissing,
      sheet_names: sheetNames,
    }
  }

  return {
    status: 'unverified',
    missing_required_sheets: [],
    sheet_names: sheetNames,
  }
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
  if (ctx.mode === 'build' || ctx.mode === 'update') cmdParts.push(`--mode "${ctx.mode}"`)
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
    `2) 有起点模型（mode="update"）时，只读运行 \`python3 .agents/skills/dalton-model/scripts/model_fingerprint.py <起点模型> --ticker "${T}" --out "${fpPath}"\` 提取基线指纹，并检查起点健康度（若预测期已全为硬编码常数或冻结窗格严重错位，在 baseline_health_warning 写明）。新建模型（mode="build"）时对默认模板提取指纹至 "${fpPath}"。随后运行 \`python3 .agents/skills/dalton-model/scripts/model_edit.py next-path --workspace-dir "${W}" --ticker "${T}"\`（有起点模型时补 \`--baseline "<起点模型>"\`）计算本轮输出路径填入 planned_output_path。`,
    `3) 收集一手财报材料与指引记录，并获取市场一致预期快照：${_mConsensusSourceHint()}`,
    deepNotes
      ? `4) 将财报披露行项与原模型行项的对应关系写入 ${mappingPath}（至少 50 字节），并在 files 中返回 ${mappingPath}。`
      : `4) 机器产物写入 ${dumpDir}/（免语言审查目录）。`,
    `返回 JSON：files、summary、mode（build 或 update）、baseline_path、baseline_sha256、fingerprint_path、consensus_path、planned_output_path、baseline_health_warning。`,
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
  let plannedCandPath = _mValidModelOutPath(prep.planned_output_path, baselinePath, W)
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
  // 阶段 2a：先绑定本轮输出路径，再受控原地建模并确认可读候选模型落盘
  // （没有拿到合法 next_path 绝不派发编辑；避免省略 --out 后超时丢失已发布候选）
  // -------------------------------------------------------------------------
  const p2 = setPhase('建模与一致预期对照')
  if (!plannedCandPath) {
    const resolvedCand = await _mResolveNextModelPath(
      W,
      T,
      baselinePath,
      `${dumpDir}/planned-output.json`,
      p2,
      '绑定输出路径'
    )
    plannedCandPath = _mValidModelOutPath(resolvedCand, baselinePath, W)
  }
  if (!plannedCandPath) {
    return failUnusable('本轮模型输出路径绑定失败（未获得合法 next_path），未派发模型构建与落盘', [...failedStages, 'model'], {
      baseline_path: baselinePath,
      baseline_sha256: baselineSha,
      consensus_path: consensusPath,
      artifacts,
      run_id: runId,
    })
  }
  const thresholdArg = consensusThresholds
    ? ` --thresholds '${typeof consensusThresholds === 'string' ? consensusThresholds : JSON.stringify(consensusThresholds)}'`
    : ''
  const editSourcePath = baselinePath || '.agents/skills/dalton-model/assets/default_model_template.xlsx'
  const stage2aOutArg = ` --out "${plannedCandPath}"`
  const editRunIdArg = progressPath ? ` --progress-path "${progressPath}"` : ''
  const stage2aPrompt = [
    `${extraCtx}先读 .agents/skills/dalton-model/SKILL.md、.agents/skills/dalton-model/references/forecast-rules.md、.agents/skills/dalton-model/references/rollforward.md 与 .agents/skills/dalton-model/references/format-spec.md${modelSpecPath ? `，以及 ${modelSpecPath}` : ''}。`,
    `目标公司：${C}（${T}）；模式：${effMode}；起点模型：${editSourcePath}；基线指纹：${fingerprintPath}；本轮绑定输出路径：${plannedCandPath}。`,
    `执行独立建模与受控原地编辑（先落盘并确认可读候选模型，暂不写一致预期快照）：`,
    `- 完整遵守既有建模约定：预测期金额全部用公式算出、由季度汇总的年度列按行项类型写公式、季度预测覆盖至下一财年 Q4、年度预测覆盖至少 3 个完整财年（下一财年各季度加之后两个完整财年），原模型保持只读；多公司行业整本只改 ${T} 所属工作表与变更记录，保留其他公司工作表原状。`,
    `- 准备编辑计划 JSON，统一经由 \`python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline "${editSourcePath}" --ticker "${T}" --plan "<edit_plan.json>"${stage2aOutArg} --workspace-dir "${W}" --fingerprint "${fingerprintPath}" --coord-map-out "${coordMapPath}" --mode "${effMode}"${editRunIdArg}${expectedBaseSha ? ` --expected-baseline-sha256 "${expectedBaseSha}"` : ''}\` 执行受控原地编辑、落盘前母本缓存恢复、落盘后可读性确认与 LibreOffice 临时副本复算写回。`,
    `返回 JSON：files（含落盘后的候选模型路径与 ${coordMapPath}）、summary、candidate_path、model_sha256（64 位）、recalc_status。`,
  ].join('\n')

  const stage2a = await _mSafeAgent(stage2aPrompt, {
    agentType: 'general-purpose',
    label: '模型构建与落盘',
    phase: p2,
    schema: _MODEL_STAGE2A_SCHEMA,
  })
  let candidatePath = null
  let modelSha = null
  // 无论子任务是否回传成功，更新 candidatePath 之前一律按预绑路径与本轮 run_id（或经日志复用的续跑链）独立核验盘上可读性、哈希与发布回执
  const diskProbe = await _mProbeCandidateOnDisk(
    plannedCandPath,
    W,
    p2,
    '核验候选落盘',
    runId,
    true,
    progressPath,
    stage2a ? '模型构建与落盘' : null
  )
  if (diskProbe.verified && diskProbe.readable && diskProbe.model_sha256 && diskProbe.run_matched) {
    candidatePath = plannedCandPath
    modelSha = diskProbe.model_sha256
    if (!stage2a || stage2a.recalc_status === 'recalc_failed') {
      _mPushUnique(failedStages, 'recalc')
    }
    if (!stage2a) {
      log(`[modeling] 模型构建子任务未回传，但已绑定候选 ${candidatePath} 已由本轮落盘且可读，按 recalc 降级继续后续阶段`)
    }
  }
  if (!candidatePath) {
    const failReason = !diskProbe.verified
      ? (runId
          ? '模型构建与落盘阶段候选核验超时（未核验），不采纳候选模型'
          : '缺少本轮 run_id，模型构建与落盘阶段候选按未核验处理，不采纳候选模型')
      : (diskProbe.exists && !diskProbe.run_matched
          ? '模型构建与落盘阶段预绑路径文件发布回执不匹配本轮 run_id，不采纳候选模型'
          : '模型构建与落盘阶段未产出可读候选模型')
    return failUnusable(failReason, [...failedStages, 'model'], {
      baseline_path: baselinePath,
      baseline_sha256: baselineSha,
      consensus_path: consensusPath,
      artifacts,
      run_id: runId,
    })
  }

  // -------------------------------------------------------------------------
  // 阶段 2b：一致预期快照隔离与差异对照（超时或返空时候选已落盘，记 consensus 降级并继续交付）
  // -------------------------------------------------------------------------
  const consReworkFpPath = `${dumpDir}/${T}-consensus-rework-fingerprint.json`
  const stage2bPrompt = [
    `${extraCtx}先读 .agents/skills/dalton-model/references/sanity-check.md。`,
    `对已落盘的 ${C}（${T}）候选模型 ${candidatePath}${modelSha ? `（sha256：${modelSha}）` : ''} 执行一致预期快照写入与差异对照：`,
    `1) 用 \`python3 .agents/skills/dalton-model/scripts/model_consensus.py write-snapshot --model "${candidatePath}" --consensus "${consensusPath || 'data/consensus/<最新文件>.json'}" --coord-map "${coordMapPath}"\` 把外部参考值写入独立快照区（不把一致预期硬编码为本方预测或结果公式依赖）。`,
    `2) 运行 \`python3 .agents/skills/dalton-model/scripts/model_consensus.py diff --model "${candidatePath}" --ticker "${T}" --research-dir "${R}"${consensusPath ? ` --consensus "${consensusPath}"` : ''}${thresholdArg}\` 生成 ${consensusMdPath} 与 ${consensusJsonPath}。`,
    `3) 若核心指标超阈值（营业总收入 ±5%、利润与稀释 EPS ±10%、近盈亏平衡 ±150bp），拆到分部与驱动假设做一次返工复核：自身假设遗漏或口径有误时，先对当前候选重提指纹（\`python3 .agents/skills/dalton-model/scripts/model_fingerprint.py "${candidatePath}" --ticker "${T}" --out "${consReworkFpPath}"\`）并取当前候选 64 位 SHA-256，再用 \`python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline "${candidatePath}" --ticker "${T}" --plan "<consensus_rework_plan.json>" --out "${candidatePath}" --in-place --workspace-dir "${W}" --fingerprint "${consReworkFpPath}" --prior-coord-map "${coordMapPath}" --coord-map-out "${coordMapPath}" --expected-baseline-sha256 "<当前候选64位SHA-256>" --mode "${effMode}"${editRunIdArg}\` 只改驱动假设并合并坐标映射，随后按最新模型哈希重跑 \`python3 .agents/skills/dalton-model/scripts/model_consensus.py diff --model "${candidatePath}" --ticker "${T}" --research-dir "${R}"${consensusPath ? ` --consensus "${consensusPath}"` : ''}${thresholdArg}\` 刷新 ${consensusMdPath} 与 ${consensusJsonPath}；有可核实独立依据则保留并在对照表与变更记录写明一句具体理由。`,
    `返回 JSON：files（含 ${candidatePath}、${coordMapPath}、${consensusMdPath} 与 ${consensusJsonPath}）、summary、candidate_path、model_sha256（64 位）、recalc_status、consensus_status、consensus_diff_generated。`,
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
  let summary10 = (stage2b && stage2b.summary) || (stage2a && stage2a.summary) || ''

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
        mode: effMode,
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
        `2) 通过 \`python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline "${candidatePath}" --ticker "${T}" --plan "<fix_plan_${fixRounds}.json>" ${editInPlaceArgs} --workspace-dir "${W}" --fingerprint "${fixFpPath}"${fixShaArg} --mode "${effMode}"${editRunIdArg}\` 在当前候选模型上定点应用增量并合并坐标映射，完成母本缓存恢复与复算写回；`,
        `3) 按最新模型哈希重跑 \`python3 .agents/skills/dalton-model/scripts/model_consensus.py diff --model "<最新候选路径>" --ticker "${T}" --research-dir "${R}"${consensusPath ? ` --consensus "${consensusPath}"` : ''}${thresholdArg}\` 刷新 ${consensusMdPath} 与 ${consensusJsonPath}；`,
        `4) 返回更新后的 files、candidate_path、model_sha256、recalc_status 与 consensus_diff_generated。`,
      ].join('\n')

      const fixRes = await _mSafeAgent(fixPrompt, {
        agentType: 'general-purpose',
        label: `模型修改 ${fixRounds}`,
        phase: fixPhase,
        schema: _MODEL_FIX_SCHEMA,
      })

      const rawFixCand = fixRes ? _mNormPath(fixRes.candidate_path, W) : null
      const fixCand = rawFixCand ? _mValidModelOutPath(rawFixCand, baselinePath, W) : null
      const fixProbe =
        fixRes && fixCand && _mHasPath(fixRes.files, fixCand, W)
          ? await _mProbeCandidateOnDisk(
              fixCand,
              W,
              fixPhase,
              '核验候选落盘',
              runId,
              true,
              progressPath,
              `模型修改 ${fixRounds}`
            )
          : null
      const fixVerified = Boolean(
        fixProbe && fixProbe.verified && fixProbe.readable && fixProbe.model_sha256 && fixProbe.run_matched
      )
      if (!fixVerified) {
        _mPushUnique(failedStages, 'fix')
        const fixFailReason = !fixProbe
          ? `第 ${fixRounds} 轮模型修改超时或返回空，已保留上一可读候选并降级交付`
          : (!fixProbe.verified
              ? `第 ${fixRounds} 轮模型修改候选未核验（${runId ? '核验超时' : '缺少本轮 run_id'}），不采纳新候选，已保留上一可读候选并降级交付`
              : (fixProbe.exists && !fixProbe.run_matched
                  ? `第 ${fixRounds} 轮模型修改候选发布回执不匹配本轮 run_id，不采纳新候选，已保留上一可读候选并降级交付`
                  : `第 ${fixRounds} 轮模型修改未产出可读候选，已保留上一可读候选并降级交付`))
        const fb = await writeFallbackLedger({
          workspaceDir: W,
          ticker: T,
          researchDir: R,
          progressPath,
          workflow: wfName,
          mode: effMode,
          candidatePath,
          baselinePath,
          kind: 'modeling',
          failedStages,
          runId,
          fixRounds,
          reason: fixFailReason,
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
      modelSha = fixProbe.model_sha256
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
        mode: effMode,
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
    mode: effMode,
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
  const effMode = ctx.mode === 'build' ? 'build' : 'update'
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

  const fallbackOnCurrent = async (failedStagesList, reasonText, openIssueMsg) => {
    if (!currentModelPath) {
      return {
        ok: false,
        changed: false,
        modelPath: null,
        modelSha256: null,
        reportSha256: null,
        status: 'failed_unusable',
        openIssues: [openIssueMsg],
        artifacts: currentArtifacts,
        failedStages: failedStagesList,
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
      mode: effMode,
      candidatePath: currentModelPath,
      baselinePath: currentModelPath,
      kind: 'report_revision',
      failedStages: failedStagesList,
      runId,
      reason: reasonText,
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
        openIssues: [`${openIssueMsg}且降级账本未确认落盘`],
        artifacts: arts,
        failedStages: failedStagesList,
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
      openIssues: [openIssueMsg],
      artifacts: arts,
      failedStages: failedStagesList,
      remainingIssuesPath: fb.remaining_issues_path,
      runId: fb.run_id || runId,
    }
  }

  // -------------------------------------------------------------------------
  // 派发 0：父流程先绑定合法下一版本路径并写入 --out（独立记录，续跑不重复递增版本）
  // 没有拿到合法 next_path（非空 .xlsx 且不等于 currentModelPath）绝不派发改模！
  // -------------------------------------------------------------------------
  const failedStages = [...carriedNoteFailures]
  const revKey = String(currentModelPath || 'base').replace(/[^a-zA-Z0-9._-]/g, '_')
  const revPlannedRecord = `${dumpDir}/rev-planned-${revKey}.json`
  const rawPlannedNext = currentModelPath
    ? await _mResolveNextModelPath(W, T, currentModelPath, revPlannedRecord, phaseLabel, '计算联动新路径')
    : null
  const plannedNextPath = _mValidModelOutPath(rawPlannedNext, currentModelPath, W)
  if (!plannedNextPath) {
    _mPushUnique(failedStages, 'report_revision')
    return await fallbackOnCurrent(
      failedStages,
      '报告联动改模下一版本输出路径绑定失败（未获得合法 next_path），未派发改模，保留当前可读模型并补写降级账本',
      '报告联动改模下一版本输出路径绑定失败'
    )
  }
  const revOutArg = ` --out "${plannedNextPath}"`
  const revRunIdArg = progressPath ? ` --progress-path "${progressPath}"` : ''

  // -------------------------------------------------------------------------
  // 派发 1：仅重提指纹 + 改模落盘（按预绑路径独立实测可读性与哈希后，再分别派发差异刷新与报告同步）
  // 子任务 label 固定使用「模型联动修订」，避免命中 check_done 的审稿收集正则（审稿|复审）
  // -------------------------------------------------------------------------
  const editPrompt = [
    `评估报告审稿意见是否需要联动修改模型（目标公司 ${T}，当前模型 ${currentModelPath}${currentModelSha ? `，sha256：${currentModelSha}` : ''}，预绑定下一版本路径 ${plannedNextPath}，报告 ${reportPath}）。`,
    `1) 若审稿意见仅涉及文字表达、无需改动模型数字或驱动假设：核对 ${currentModelPath} 哈希未变，不修改模型与报告文件，直接返回 changed=false、model_path="${currentModelPath}"、model_sha256。`,
    `2) 若审稿意见要求调整模型数字、驱动假设或勾稽（本步只做重提指纹与改模落盘，暂不刷新差异表也不改写报告，确认新候选后再分别执行）：`,
    `   a) 先对当前候选模型重新提取指纹：\`python3 .agents/skills/dalton-model/scripts/model_fingerprint.py "${currentModelPath}" --ticker "${T}" --out "${revFpPath}"\`；`,
    `   b) 按 .agents/skills/dalton-model/SKILL.md 与 .agents/skills/dalton-model/references/rollforward.md，保持原件只读，调用 \`python3 .agents/skills/dalton-model/scripts/model_edit.py apply --baseline "${currentModelPath}" --ticker "${T}" --plan "<report_rev_plan.json>"${revOutArg} --workspace-dir "${W}" --fingerprint "${revFpPath}"${baseShaArg} --coord-map-out "${coordMapPath}" --mode update${revRunIdArg}\` 按原版本链规则另存下一版本并完成落盘前母本缓存恢复、落盘后可读性确认与复算写回。`,
    `审稿意见：\n${typeof reviewFeedback === 'string' ? reviewFeedback : JSON.stringify(reviewFeedback)}`,
    `返回 JSON：files（含新模型路径 ${plannedNextPath} 与 ${coordMapPath}）、summary、changed、model_path、model_sha256（64 位）、recalc_status。`,
  ].join('\n')

  const editRes = await _mSafeAgent(editPrompt, {
    agentType: 'general-purpose',
    label: '模型联动修订',
    phase: phaseLabel,
    schema: _MODEL_REV_EDIT_SCHEMA,
  })

  // 父流程始终先按预绑路径 plannedNextPath 与本轮 run_id 独立实测盘上可读性、发布记录与 SHA-256，再判断是否发生改动；
  // 绝不信任子任务回传的旧路径或自报哈希，也绝不把其他运行发布的文件认作本轮新候选。
  let nextPath = null
  let nextSha = null
  const diskProbe = await _mProbeCandidateOnDisk(
    plannedNextPath,
    W,
    phaseLabel,
    '核验联动候选落盘',
    runId,
    true,
    progressPath,
    editRes ? '模型联动修订' : null
  )
  if (diskProbe.verified && diskProbe.readable && diskProbe.model_sha256 && diskProbe.run_matched) {
    nextPath = plannedNextPath
    nextSha = diskProbe.model_sha256
    const hasRecalcStatus = Boolean(
      editRes && typeof editRes.recalc_status === 'string' && editRes.recalc_status.trim()
    )
    if (!editRes || !hasRecalcStatus || editRes.recalc_status === 'recalc_failed') {
      _mPushUnique(failedStages, 'recalc')
    }
    if (!editRes) {
      log(`[modeling] 报告联动改模派发未回传，但预绑新候选 ${nextPath} 已由本轮落盘且可读，按已确认新候选继续`)
    } else if (!editRes.changed) {
      log(`[modeling] 报告联动改模回报 changed=false，但预绑新候选 ${nextPath} 已由本轮落盘且可读，按已确认新候选继续`)
    }
  } else if (!diskProbe.verified) {
    // 核验子任务重试后仍无返回或缺少本轮 run_id：按「未核验」处理，交由降级账本命令自己读盘，不提前终止报告修订
    _mPushUnique(failedStages, 'report_revision')
    return await fallbackOnCurrent(
      failedStages,
      runId
        ? '报告联动改模新候选核验子任务超时未核验，保留当前模型并由降级账本命令直接读盘确认'
        : '缺少本轮 run_id，报告联动改模新候选按未核验处理且不采纳，保留当前模型并由降级账本命令直接读盘确认',
      runId
        ? '报告联动改模新候选核验子任务超时（未核验）'
        : '缺少本轮 run_id，报告联动改模新候选未核验'
    )
  } else if (editRes && !editRes.changed && (!diskProbe.exists || (diskProbe.readable && !diskProbe.run_matched))) {
    // 本轮未在预绑路径发布新候选且子任务声称未改：读盘实测当前模型可读性与哈希，绝不采用自报哈希
    const curProbe = await _mProbeCandidateOnDisk(currentModelPath, W, phaseLabel, '核验当前模型落盘', null, false)
    if (!curProbe.verified) {
      // 核验当前模型的子任务重试后仍超时：按「未核验」处理，由降级账本命令直接读盘确认当前模型，不提前终止报告修订
      _mPushUnique(failedStages, 'report_revision')
      return await fallbackOnCurrent(
        failedStages,
        '报告联动改模声称未改但当前模型核验子任务超时（未核验），已交由降级账本命令直接读盘确认当前模型',
        '当前模型核验子任务超时（未核验）'
      )
    }
    if (!curProbe.readable || !curProbe.model_sha256) {
      return {
        ok: false,
        changed: false,
        modelPath: null,
        modelSha256: null,
        reportSha256: null,
        status: 'failed_unusable',
        openIssues: [
          curProbe.status === 'missing'
            ? '报告联动改模未产出新版本，且当前模型在盘上不存在'
            : '报告联动改模未产出新版本，且当前模型在盘上不可读',
        ],
        artifacts: currentArtifacts,
        failedStages: [...carriedNoteFailures, 'report_revision'],
        remainingIssuesPath: null,
        runId,
      }
    }
    if (currentModelSha && curProbe.model_sha256 !== currentModelSha) {
      _mPushUnique(failedStages, 'report_revision')
      return await fallbackOnCurrent(
        failedStages,
        '报告联动改模声称未改，但当前模型盘上哈希与改前不一致，已对当前可读模型补写降级账本',
        '当前模型盘上哈希与改前不一致'
      )
    }
    return {
      ok: true,
      changed: false,
      modelPath: currentModelPath,
      modelSha256: curProbe.model_sha256,
      reportSha256: null,
      status: currentStatus,
      openIssues: [],
      artifacts: currentArtifacts,
      failedStages: carriedNoteFailures,
      remainingIssuesPath: currentStatus === 'delivered_with_open_issues' ? remPath : null,
      runId,
    }
  } else if (!editRes) {
    _mPushUnique(failedStages, 'report_revision')
    return await fallbackOnCurrent(
      failedStages,
      diskProbe.exists && !diskProbe.run_matched
        ? '报告联动改模落盘子任务超时或返回空，且预绑路径文件非本轮发布，已保留当前可读模型并补写降级账本'
        : '报告联动改模落盘子任务超时或返回空且预绑路径未见可读新候选，已保留当前可读模型并补写降级账本',
      '报告联动改模落盘子任务超时或返回空'
    )
  } else if (!editRes.changed && diskProbe.exists) {
    _mPushUnique(failedStages, 'report_revision')
    return await fallbackOnCurrent(
      failedStages,
      '报告联动改模回报未改但预绑路径存在不可读文件，已保留当前可读模型并补写降级账本',
      '报告联动改模在预绑输出路径留下不可读文件'
    )
  } else {
    // editRes.changed === true，但预绑新路径盘上未见本轮发布的可读候选（不存在、不可读或属于其他运行）：
    // 绝不把旧路径或其他运行的候选认作本轮新候选，也不终止流程；对当前可读模型补写降级账本，终态为带问题交付，报告修订继续。
    _mPushUnique(failedStages, 'report_revision')
    return await fallbackOnCurrent(
      failedStages,
      diskProbe.exists && !diskProbe.run_matched
        ? '报告联动改模声称已改，但预绑输出路径文件属于其他运行而非本轮发布，已保留当前可读模型并补写降级账本'
        : '报告联动改模声称已改但预绑输出路径未产出可读新候选，已保留当前可读模型并补写降级账本',
      diskProbe.exists && !diskProbe.run_matched
        ? '报告联动改模预绑输出路径文件非本轮发布'
        : '报告联动改模未在预绑输出路径产出可读新候选模型'
    )
  }

  // -------------------------------------------------------------------------
  // 派发 2a：单独派发一致预期差异刷新，并按盘上实测证据落账
  // -------------------------------------------------------------------------
  const diffPrompt = [
    `已确认报告联动改模产出的新候选模型 ${nextPath}（sha256：${nextSha}），请仅执行一致预期差异刷新（不修改 .xlsx 模型文件与报告正文）：`,
    `运行 \`python3 .agents/skills/dalton-model/scripts/model_consensus.py diff --model "${nextPath}" --ticker "${T}" --research-dir "${R}"${consensusPath ? ` --consensus "${consensusPath}"` : ''}${thresholdArg}\` 刷新一致预期差异表 ${consensusMdPath} 与 ${consensusJsonPath}。`,
    `返回 JSON：files（含 ${consensusMdPath} 与 ${consensusJsonPath}）、summary、consensus_diff_generated。`,
  ].join('\n')

  await _mSafeAgent(diffPrompt, {
    agentType: 'general-purpose',
    label: '模型联动差异刷新',
    phase: phaseLabel,
    schema: _MODEL_REV_DIFF_SCHEMA,
  })
  const consProbe = await _mProbeConsensusOnDisk(
    consensusJsonPath,
    consensusMdPath,
    nextSha,
    W,
    phaseLabel,
    '核验差异表落盘'
  )
  const consMdOk = consProbe.md_ok
  const consJsonOk = consProbe.json_ok
  if (!consProbe.matched) {
    _mPushUnique(failedStages, 'consensus')
  }

  // -------------------------------------------------------------------------
  // 派发 2b：单独派发报告引用同步，并按盘上实测证据与实测报告哈希落账
  // -------------------------------------------------------------------------
  const reportSyncPrompt = [
    `已确认报告联动改模产出的新候选模型 ${nextPath}（sha256：${nextSha}），请仅同步报告引用与元数据（不修改 .xlsx 模型文件）：`,
    `同步更新 ${reportPath} 正文中的 model: 引用${syncClause}，使其指向新模型路径 ${nextPath} 与新哈希 ${nextSha}（JSON 元数据文件写入 {"path": "${nextPath}", "sha256": "${nextSha}"}），随后在 Bash 执行 \`sha256sum "${reportPath}"\` 并在返回 JSON 的 report_sha256 字段回传该 64 位实测哈希。`,
    `返回 JSON：files（含已改写报告路径${extraSyncPaths.length ? `与 ${extraSyncPaths.join('、')}` : ''}）、summary、report_sha256。`,
  ].join('\n')

  await _mSafeAgent(reportSyncPrompt, {
    agentType: 'general-purpose',
    label: '模型联动报告同步',
    phase: phaseLabel,
    schema: _MODEL_REV_REPORT_SYNC_SCHEMA,
  })
  const reportProbe = await _mProbeReportSyncOnDisk(
    reportPath,
    nextPath,
    nextSha,
    extraSyncPaths,
    W,
    phaseLabel,
    '核验报告同步落盘'
  )
  const reportSha256 = reportProbe.report_sha256
  if (!reportProbe.synced || !reportSha256) {
    _mPushUnique(failedStages, 'report_sync')
  }

  // -------------------------------------------------------------------------
  // 派发 3：单遍本轮范围检查（append kind="report_revision"，透传本轮有效 mode）；
  // 超时时按已确认新候选路径与本轮有效 mode 写降级账本
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
    `--mode "${effMode}"`,
    `--kind report_revision`,
    `--append-revision`,
  ]
  if (runId) chkCmdParts.push(`--run-id "${runId}"`)
  if (failedStages.length) chkCmdParts.push(`--failed-stages "${failedStages.join(',')}"`)

  const chkPrompt = [
    `对报告联动改模已落盘的新候选 ${nextPath}（sha256：${nextSha}）执行单遍本轮范围检查（不修改模型文件）。`,
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
      mode: effMode,
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
