export const meta = {
  name: 'full-coverage',
  description:
    'Dalton 完整覆盖：一手材料与数字底表 → frame → A∥B∥C∥D 取证 → 建模∥memo → check-model∥独立审稿 → 修订 → 知识层修订草案',
}

const { ticker: T, company: C, workspace_dir: W, as_of: D } = args
const R = `${W}/research/${D}-fc`
const S = '.agents/skills/dalton-full-coverage'
const base = `先读 .agents/skills/dalton-conventions/SKILL.md 与 ${S}/SKILL.md；公司 ${C || T}（${T}），截至 ${D}；数字底表 ${R}/facts.md（只引用底表，缺了写“底表缺 X”）。`
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

phase('一手材料与数字底表')
const facts = await agent(
  `${base} 按 ${S}/references/facts-ledger.md 建 ${R}/facts.md：读最近 4 份定期报告（${args.form || '10-K'}/10-Q）、4 次电话会、招股书（如有）、内部人交易；按 .agents/skills/dalton-financial-lenses/SKILL.md 做三把镜头；用 .agents/skills/dalton-financial-lenses/scripts/earnings_quality.py 算盈利质量（Sloan 比率、CFO/NI、现金转换周期）；所有必填键用 python ${S}/scripts/facts.py set 写入。`,
  { agentType: 'dalton-evidence', label: '数字底表', phase: '一手材料与数字底表', schema: OUT }
)
if (facts === null) {
  return { ok: false, reason: '数字底表失败，后续阶段不启动' }
}

phase('问题清单')
const frame = await agent(
  `${base} 按 ${S}/references/frame.md 写 ${R}/questions.md。PM 要求：${args.pm_brief || '无'}；重点问题：${
    (args.focus_questions || []).join('；') || '默认'
  }。写完跑 python ${S}/scripts/research_structure.py validate-agenda ${R}/questions.md 到通过。初次覆盖稿与知识层在 ${W}/drafts/ 与 ${W}/ 根目录。`,
  { agentType: 'general-purpose', label: 'frame', phase: '问题清单', schema: OUT }
)

phase('取证')
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
      }。`,
      { agentType: 'dalton-evidence', label: `支线 ${k.toUpperCase()}`, phase: '取证', schema: OUT }
    )
  )
)
const evMissing = branches.filter((_, i) => ev[i] === null).map(([k]) => k.toUpperCase())

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
        { agentType: 'general-purpose', label: 'memo 第三段', phase: '建模与 memo 第一段', schema: OUT }
      )

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
          `审阅 ${W}/drafts/${D}-memo.md，支线 ${R}/e*.md，底表 ${R}/facts.md，模型 ${W}/model/${T}-model-v01.xlsx（全部只读，不修改任何文件）。先只凭核心判断和零章复述投资论点，再逐题检查，重点 Q1–Q5、Q7、Q8、Q12。`,
          { agentType: 'dalton-reviewer', label: '独立审稿', phase: '审模型与审稿' }
        ),
])

phase('修订')
const fix =
  review === null
    ? null
    : await agent(
        `${base} 先核对 ${W}/drafts/${D}-memo.md 的 sha256 是否与审稿前（${
          memo3 && memo3.sha256 ? memo3.sha256 : '第三段记录值'
        }）一致；按审稿意见与 ${R}/model-check.md 修订 memo 和模型（模型若有改动走 .agents/skills/model-update/SKILL.md，另存 ${W}/model/${T}-model-v02.xlsx，不覆盖 v01）。P0 全改，跑 python ${S}/scripts/validate_memo.py --mode full ${W}/drafts/${D}-memo.md 到 0 FAIL。审稿意见：\n${review}`,
        { agentType: 'general-purpose', label: '修订', phase: '修订', schema: OUT }
      )

const kp =
  memo3 === null
    ? null
    : await agent(
        `${base} 对比 ${W}/ 根目录现有知识层与修订后的 ${W}/drafts/${D}-memo.md，按 PK.2 的 JSON Schema 写知识层修订草案 ${R}/knowledge-revision.json（结构为 {doc: {ops, reason, evidence_refs}}）：每处改动给旧值、新值、原因、证据（引用编号）。管理层指引风格与 C4 历史写入 management 部分；不直接修改 ${W}/data/。`,
        { agentType: 'general-purpose', label: '知识层修订草案', phase: '修订', schema: OUT }
      )

phase('自动跟踪')
const tracking =
  memo3 === null
    ? { ok: false, skipped: true, reason: 'memo 未完成' }
    : {
        action: 'enable',
        source: 'coverage_finish',
        tool: 'dalton_coverage_tracking',
        call: 'dalton_coverage_tracking(action="enable", source="coverage_finish")',
        respect_user_pause: true,
      }
if (memo3 !== null) {
  log(
    `完整覆盖完成，收尾需调用 dalton_coverage_tracking(action="enable", source="coverage_finish") 开启 ${T} 自动跟踪（若 paused_by_user 则保持关闭）`
  )
}

return {
  ok: memo3 !== null,
  memo: `${W}/drafts/${D}-memo.md`,
  model: model && `${W}/model/${T}-model-v01.xlsx`,
  review_done: review !== null,
  model_check: mcheck && `${R}/model-check.md`,
  knowledge_revision: kp && `${R}/knowledge-revision.json`,
  evidence_missing: evMissing,
  tracking,
}
