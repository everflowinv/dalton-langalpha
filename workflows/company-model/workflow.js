export const meta = {
  name: 'company-model',
  description: 'Dalton 公司财务建模：准备与基线指纹 → 受控原地建模与一致预期对照 → 本轮范围检查与最多两轮修改 → 终态账本与交付',
}

/* DALTON_INCLUDE:_shared/modeling_stages.js */

const { ticker: T, company: C, workspace_dir: W, as_of: D } = args
const R = `${W}/research/${D}-model-${T}`
const progressPath = args.progress_path || `${R}/.progress.json`

const result = await runModelingPipeline({
  workspaceDir: W,
  ticker: T,
  company: C || T,
  researchDir: R,
  progressPath,
  workflow: 'company-model',
  mode: args.mode || 'auto',
  modelPath: args.model_path || null,
  expectedBaselineSha256: args.expected_baseline_sha256 || null,
  consensusThresholds: args.consensus_thresholds || null,
  asOf: D,
  emitPhases: true,
  skipIfNoModel: false,
})

return {
  ok: result.ok !== false && (result.status === 'pass' || result.status === 'delivered_with_open_issues'),
  ...result,
}
