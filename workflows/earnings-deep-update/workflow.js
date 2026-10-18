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
    files: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
    sha256: { type: 'string' },
    gaps: { type: 'array', items: { type: 'string' } },
  },
  required: ['files', 'summary'],
}

phase('深度点评与模型')
const [report, model] = await parallel([
  () =>
    agent(
      `${base} 按 ${S}/references/eight-sections.md${
        args.sector_overlay === 'hardware' ? ` 与 ${S}/references/hardware-overlay.md` : ''
      } 写八节深度点评到 ${W}/drafts/${D}-deep-${Q}.md；价格反应用 python ${S}/scripts/price_reaction.py；卖方窗口为发布到发布后 3 个交易日。返回成稿路径与 sha256。`,
      { agentType: 'general-purpose', label: '八节点评', phase: '深度点评与模型', schema: OUT }
    ),
  () =>
    args.model_path
      ? agent(
          `${base} 按 .agents/skills/model-update/SKILL.md 更新模型 ${args.model_path}：触发类型 Results；先出映射表到 ${R}/mapping.md，再写入另存的新版本（不覆盖旧版），Change Log 完整；recalc 与 audit --strict 通过；输出 EPS walk 与估值变化到 ${R}/model-delta.md。`,
          { agentType: 'general-purpose', label: '模型更新', phase: '深度点评与模型', schema: OUT }
        )
      : Promise.resolve(null),
])

phase('校准与对账')
const calib =
  report === null
    ? null
    : await agent(
        `${base} 按 ${S}/references/calibration.md：
1) 用 python ${S}/scripts/reconcile.py 对 ${
          args.preview_predictions || '（无前瞻预测文件，跳过对账并说明）'
        } 逐条对账，输出 ${R}/reconcile.md；
2) 按 .agents/skills/thesis-tracker/SKILL.md 的 post-earnings 模式，对 ${W}/thesis.md 每个支柱给信号与状态，写 ${R}/thesis-calibration.md；
3) 更新指引兑现记录：本季实际 vs 上季给的指引，判断管理层指引风格是否延续，写 ${R}/management-delta.md。
材料：${W}/drafts/${D}-deep-${Q}.md${model ? `，${R}/model-delta.md` : ''}。`,
        { agentType: 'general-purpose', label: '校准与对账', phase: '校准与对账', schema: OUT }
      )

phase('审稿')
const review =
  calib === null
    ? null
    : await agent(
        `审阅 ${W}/drafts/${D}-deep-${Q}.md 及 ${R}/ 下的对账与校准文件（只读不改任何文件）。重点：beat/miss 口径；“综合判断”是否有判断；thesis 信号是否被价格走势替代；对账是否回避了错判。`,
        { agentType: 'dalton-reviewer', label: '审稿', phase: '审稿' }
      )

const kp = await agent(
  `${base} 先核对 ${W}/drafts/${D}-deep-${Q}.md 的 sha256 是否与审稿前（${
    report && report.sha256 ? report.sha256 : '成稿记录值'
  }）一致，若审稿有 P0 意见先修订 ${W}/drafts/${D}-deep-${Q}.md。然后汇总 ${R}/ 全部文件与审稿意见（${
    review ? '见下' : '审稿缺席：只写 NO_CHANGE 以外结论为待审说明，不改核心支柱状态'
  }），按 PK.2 的 JSON Schema 写知识层修订草案 ${R}/knowledge-revision.json（{doc: {ops, reason, evidence_refs}}）：thesis 支柱信号与证据行、debate 变化、drivers 新读数、management 指引兑现行、lessons（前瞻错判的原因）、models（新模型版本登记，若模型更新失败则写“模型未更新”并排人工跟进）、事件五词终判。\n${
    review || ''
  }`,
  { agentType: 'general-purpose', label: '知识层修订草案', phase: '审稿', schema: OUT }
)

return {
  ok: report !== null,
  report: report && `${W}/drafts/${D}-deep-${Q}.md`,
  model_delta: model && `${R}/model-delta.md`,
  reconcile: calib && `${R}/reconcile.md`,
  review_done: review !== null,
  knowledge_revision: kp && `${R}/knowledge-revision.json`,
}
