"""Direct LangChain tools for the Dalton company knowledge layer (PK.4)."""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.auth import verify_workspace_access
from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import verdicts as db_verdicts
from src.dalton.knowledge.paths import ALL_DOC_KEYS
from src.dalton.knowledge.reconcile import apply_pending
from src.dalton.knowledge.scenario_math import evaluate_underwriting
from src.dalton.knowledge.service import append_verdict, apply_update
from src.server.database.pool import get_db_connection
from src.tools.secretary._commands import InjectedToolCallId

logger = logging.getLogger(__name__)

# DALTON-HOOK(payoff-assessment): the two skills whose every verdict must
# carry the odds assessment, matched by their standard automation names
# (dalton-<ticker>-move-up/down = stock-move, dalton-<ticker>-delta =
# thesis-delta). Runs the pattern cannot see (manual skill runs, custom task
# names) fall through to the dalton_check_done contract, which knows the
# workflow by name.
_PAYOFF_SKILL_AUTO_RE = re.compile(
    r"^dalton-[a-z0-9.]+-(?:move-(?:up|down)|delta)$", re.IGNORECASE
)
DocLiteral = Literal[
    "company",
    "sector",
    "thesis",
    "debate",
    "drivers",
    "management",
    "lessons",
    "agenda",
    "models",
]


def _extract_run_identity(
    config: RunnableConfig | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract ``(user_id, workspace_id, thread_id, run_id)`` from ``RunnableConfig``."""
    cfg = (config or {}).get("configurable") or {}
    user_id = cfg.get("user_id")
    workspace_id = cfg.get("workspace_id")
    thread_id = cfg.get("thread_id")
    run_id = cfg.get("run_id") or cfg.get("response_id")
    return (
        str(user_id) if user_id else None,
        str(workspace_id) if workspace_id else None,
        str(thread_id) if thread_id else None,
        str(run_id) if run_id else None,
    )


async def _resolve_thread_origin(
    thread_id: str | None,
) -> tuple[str | None, str | None]:
    """Resolve ``(origin_type, automation_execution_id)`` for ``thread_id``."""
    if not thread_id:
        return None, None
    origin_type: str | None = None
    automation_execution_id: str | None = None
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT metadata->'origin'->>'type'
                    FROM conversation_threads
                    WHERE conversation_thread_id = %s
                    """,
                    (thread_id,),
                )
                row = await cur.fetchone()
                if row and row[0]:
                    origin_type = str(row[0])
                if origin_type == "automation":
                    await cur.execute(
                        """
                        SELECT automation_execution_id
                        FROM automation_executions
                        WHERE conversation_thread_id = %s
                        ORDER BY started_at DESC NULLS LAST
                        LIMIT 1
                        """,
                        (thread_id,),
                    )
                    erow = await cur.fetchone()
                    if erow and erow[0]:
                        automation_execution_id = str(erow[0])
    except Exception as e:
        logger.debug("Could not resolve thread origin for %s: %s", thread_id, e)
    return origin_type, automation_execution_id


async def _automation_name_by_id(automation_id: str) -> str | None:
    """The automation's name, or None when the id resolves to nothing."""
    if not automation_id:
        return None
    try:
        async with get_db_connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT name FROM automations WHERE automation_id = %s",
                    (automation_id,),
                )
                row = await cur.fetchone()
                return str(row[0]) if row and row[0] else None
    except Exception as e:
        logger.debug("Could not resolve automation %s: %s", automation_id, e)
        return None


async def _payoff_required_because(
    verdict: dict[str, Any],
    config: RunnableConfig | None,
) -> str | None:
    """Why THIS verdict must carry ``payoff_assessment`` (save-time gate), or None.

    Two reliable signals: a ``price_move`` trigger (stock-move always writes
    one; thesis-delta's price-wake runs do too) and a turn the server stamped
    as sent by a stock-move / thesis-delta automation — per TURN, from
    ``configurable.turn_origin_automation_id``: a pinned automation thread
    also takes manual follow-ups, whose verdicts (other skills, no origin
    stamp) must pass untouched. Nothing else is rejected here — the contract
    layer (``dalton_check_done``) enforces the field for those runs by
    workflow name."""
    trigger = verdict.get("trigger")
    kind = str(trigger.get("kind") or "") if isinstance(trigger, dict) else ""
    if kind == "price_move":
        return "`trigger.kind` 为 `price_move`（股价异动归因结论）"
    cfg = (config or {}).get("configurable") or {}
    automation_id = str(cfg.get("turn_origin_automation_id") or "").strip()
    if not automation_id:
        return None  # a manual turn (or an unstamped surface): check_done 兜底
    origin_name = await _automation_name_by_id(automation_id)
    if origin_name and _PAYOFF_SKILL_AUTO_RE.match(origin_name.strip()):
        return (
            f"本轮由自动任务 `{origin_name.strip()}` 触发"
            "（`dalton-stock-move` / `dalton-thesis-delta` 的结论）"
        )
    return None


def extract_verdict_signal_refs(verdict: dict[str, Any]) -> list[str]:
    """Extract trigger signal keys/IDs from a verdict payload (G4.2)."""
    if not isinstance(verdict, dict):
        return []
    refs: list[str] = []
    trig = verdict.get("trigger")
    if isinstance(trig, dict):
        t_ref = trig.get("ref")
        if isinstance(t_ref, str) and t_ref.strip():
            clean_ref = t_ref.strip()
            refs.append(clean_ref)
            if trig.get("kind") == "filing" and not clean_ref.startswith("filing:"):
                refs.append(f"filing:{clean_ref}")
    for key in ("trigger_signal", "signal_id", "signal_key"):
        val = verdict.get(key)
        if isinstance(val, str) and val.strip():
            refs.append(val.strip())
    for list_key in ("trigger_signals", "signal_ids", "signals"):
        items = verdict.get(list_key)
        if isinstance(items, (list, tuple)):
            for item in items:
                if isinstance(item, str) and item.strip():
                    refs.append(item.strip())
                elif isinstance(item, dict):
                    s_id = item.get("signal_id") or item.get("signal_key") or item.get("id")
                    if isinstance(s_id, str) and s_id.strip():
                        refs.append(s_id.strip())
    seen: set[str] = set()
    deduped: list[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            deduped.append(r)
    return deduped


async def resolve_verdict_signals(
    *,
    workspace_id: str | None,
    verdict: dict[str, Any],
    verdict_id: str | None = None,
    execution_id: str | None = None,
    ticker: str | None = None,
) -> int:
    """Resolve matching signals in ``dalton_signals`` after a verdict is recorded (G4.2)."""
    refs = extract_verdict_signal_refs(verdict)
    trig = verdict.get("trigger") if isinstance(verdict, dict) else None
    eff_exec_id = execution_id or (
        str(trig.get("automation_execution_id")).strip()
        if isinstance(trig, dict) and trig.get("automation_execution_id")
        else None
    )
    if not refs and not eff_exec_id:
        return 0
    try:
        from src.dalton.database import watch as watch_db

        return await watch_db.resolve_signals(
            refs,
            workspace_id=workspace_id,
            ticker=ticker or (str(verdict.get("ticker")).upper() if verdict.get("ticker") else None),
            resolved_by=str(verdict_id) if verdict_id else None,
            execution_id=eff_exec_id,
        )
    except (HTTPException, PermissionError, ValueError):
        raise
    except Exception as e:
        logger.warning("[DALTON_KNOWLEDGE_TOOLS] Could not resolve signals for verdict %s: %s", verdict_id, e)
        return 0


def _translate_staleness_reason(
    reason: str,
    *,
    days_elapsed: int | None = None,
    price_move: float | None = None,
) -> str:
    """Translate internal staleness reason codes into human-readable Chinese."""
    r = str(reason or "").strip()
    if r.startswith(("anchor_age_exceeded", "anchor_age_gt_")):
        if days_elapsed is not None:
            return f"距今已 {days_elapsed} 天，超过有效期限"
        return "距今超过有效期限"
    if r.startswith(("anchor_price_move_exceeded", "spot_drift_gt_")):
        if price_move is not None:
            return f"现价偏离基准股价 {float(price_move):.1%}，超过阈值"
        return "现价偏离基准股价超过阈值"
    return r


_MULT_STATUS_ZH: dict[str, str] = {
    "skipped_missing_reference": "未提供基准 EPS",
    "skipped_non_positive_or_reference": "非正数或引用法估值",
    "skipped_non_positive_price_return": "目标价未高于基准股价",
    "skipped_non_negative_price_return": "目标价未低于基准股价",
}


def _format_underwriting_summary(
    thesis_doc: dict[str, Any],
    *,
    ok: bool = True,
    dry_run: bool = False,
    version_no: int | None = None,
    validation_errors: list[dict[str, Any]] | None = None,
) -> str:
    """Format a human-readable summary of ``evaluate_underwriting(thesis_doc)`` for the agent."""
    if not isinstance(thesis_doc, dict) or not isinstance(thesis_doc.get("underwriting"), dict):
        return ""
    try:
        ev = evaluate_underwriting(thesis_doc)
    except Exception as exc:
        logger.debug("evaluate_underwriting failed in knowledge_update summary: %s", exc)
        return ""

    from src.dalton.knowledge.render import zh_label

    stance = str(ev.get("stance") or "")
    stance_zh = zh_label("stance", stance) if stance else "未填"
    horizon = ev.get("horizon_months")
    p0 = float(ev.get("anchor_price") or 0.0)
    hurdles = ev.get("hurdles") or {}
    h_l = float(hurdles.get("H_L") or 0.0)
    h_s = float(hurdles.get("H_S") or 0.0)
    cost_note = f"（{hurdles['short_cost_note']}）" if hurdles.get("short_cost_note") else ""

    stale = ev.get("staleness") or {}
    days = stale.get("days_elapsed")
    p_move = stale.get("price_move")
    if stale.get("is_stale"):
        reasons_zh = "、".join(
            _translate_staleness_reason(str(r), days_elapsed=days, price_move=p_move)
            for r in (stale.get("reasons") or [])
        )
        stale_str = f"是（{reasons_zh}）" if reasons_zh else "是"
    else:
        stale_str = f"否（{days} 天）" if days is not None else "否"

    states = ev.get("states") or {}
    state_parts: list[str] = []
    for name in ("bear", "base", "bull"):
        st = states.get(name)
        if isinstance(st, dict) and isinstance(st.get("price"), dict):
            pr = st["price"]
            vt = st.get("v_tr") or pr
            state_parts.append(
                f"{name} [{pr.get('lo', 0.0):.2f}, {pr.get('hi', 0.0):.2f}] "
                f"(中点 {pr.get('mid', 0.0):.2f}, 含现金终值 {vt.get('mid', 0.0):.2f})"
            )
    states_line = "｜".join(state_parts) if state_parts else "未算得有效三情景价格区间"

    be = ev.get("breakeven") or {}

    def _fmt_be_rows(rows: list[dict[str, Any]], prob_label: str) -> str:
        items: list[str] = []
        for r in rows:
            b_pct = int(round(float(r.get("b") or 0.0) * 100))
            mid_info = r.get("mid") or {}
            clamped = mid_info.get("clamped")
            status = str(mid_info.get("status") or "")
            if status in ("无需 bull 即达标", "无需 bear 即达标", "该组合不可达", "无法判断"):
                mid_s = status
            elif clamped is not None:
                mid_s = f"{float(clamped):.1%}"
            else:
                mid_s = status or "—"
            disp = str(r.get("display") or "")
            items.append(f"base 权重占 {b_pct}% 时需 {prob_label} {mid_s}（区间：{disp}）")
        return "、".join(items) if items else "—"

    long_be_str = _fmt_be_rows(be.get("long") or [], "bull")
    short_be_str = _fmt_be_rows(be.get("short") or [], "bear")
    no_bull_lo = bool(ev.get("no_bull_needed_all_lo"))
    no_bear_hi = bool(ev.get("no_bear_needed_all_hi"))

    wb_eval = ev.get("weight_bounds_eval")
    if isinstance(wb_eval, dict):
        if not wb_eval.get("feasible"):
            wb_str = "上下限本身无法凑出总和为 100% 的权重组合"
        else:
            v_cnt = len(wb_eval.get("vertices") or [])
            l_pass = bool((wb_eval.get("long") or {}).get("all_pass"))
            s_pass = bool((wb_eval.get("short") or {}).get("all_pass"))
            wb_str = (
                f"看多通过：{'是' if l_pass else '否'} / 看空通过：{'是' if s_pass else '否'}"
                f"（共检查 {v_cnt} 组极端权重组合）"
            )
    else:
        wb_str = "未填主观权重上下限（weight_bounds）"

    adv = ev.get("adverse_move") or {}
    adv_val = float(adv.get("adverse_move") or 0.0)
    adv_lim = float(adv.get("max_adverse_move_act_now") or 0.25)
    adv_ok = bool(adv.get("ok_for_act_now"))
    if stance == "long":
        adv_str = (
            f"最不利情景（bear 区间下沿，含现金回报）相对基准股价的跌幅：{adv_val:.1%}"
            f"（上限 {adv_lim:.0%}，满足立即行动要求：{'是' if adv_ok else '否'}）"
        )
    elif stance == "short":
        adv_str = (
            f"最不利情景（bull 区间上沿）相对基准股价的涨幅：{adv_val:.1%}"
            f"（上限 {adv_lim:.0%}，满足立即行动要求：{'是' if adv_ok else '否'}）"
        )
    else:
        adv_str = "最不利情景不利变动：中性或暂不表态时不计算"

    m_dec = ev.get("multiple_decomposition") or {}
    m_share = m_dec.get("multiple_share")
    m_tag = m_dec.get("tag") or "正常（未超 50%）"
    raw_m_status = str(m_dec.get("status") or "")
    m_status_zh = _MULT_STATUS_ZH.get(raw_m_status, raw_m_status or "未提供基准 EPS")
    m_str = f"{float(m_share):.1%}（{m_tag}）" if m_share is not None else f"未计算（{m_status_zh}）"

    calc_errors = [str(e).strip() for e in (ev.get("errors") or []) if str(e).strip()]
    val_err_msgs: list[str] = []
    for err in validation_errors or []:
        if isinstance(err, dict):
            p = str(err.get("path") or "/")
            m = str(err.get("message") or err.get("code") or "").strip()
            item_s = f"{p}: {m}" if p and p != "/" else m
            if item_s and item_s not in calc_errors and m not in calc_errors:
                val_err_msgs.append(item_s)
    all_errors = calc_errors + val_err_msgs
    has_error = (not ok) or bool(calc_errors)

    if has_error:
        status_label = "这一版没有保存成功（或计算有错误），下面的数字不能写进正文"
    elif dry_run:
        status_label = "试写通过，尚未保存"
    else:
        status_label = f"已保存，版本 {version_no}" if version_no is not None else "已保存"

    lines = [
        f"赔率计算摘要（{status_label}｜立场：{stance_zh}，期限：{horizon} 个月，"
        f"基准股价：{p0:.2f}，基准股价是否过期：{stale_str}｜看多达标终值：{h_l:.2f}，看空达标终值：{h_s:.2f}{cost_note}）："
    ]
    if has_error:
        err_text = "；".join(all_errors[:5]) if all_errors else "校验或计算未通过"
        lines.append(f"- 错误：{err_text}")
    lines.extend(
        [
            f"- 三情景价格区间：{states_line}",
            f"- 盈亏平衡所需概率（测试权重，不是主观预测）：看多：{long_be_str}（价格区间下端也无需 bull 即可达标：{'是' if no_bull_lo else '否'}）｜"
            f"看空：{short_be_str}（价格区间上端也无需 bear 即可达标：{'是' if no_bear_hi else '否'}）",
            f"- 全部权重组合是否都过最低回报要求：{wb_str}｜{adv_str}｜估值倍数变动占目标回报比例：{m_str}",
        ]
    )
    return "\n".join(lines)


def create_knowledge_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Build ``[knowledge_update, knowledge_append_verdict, knowledge_read]`` bound to ``filesystem_backend``."""
    reconciled_workspaces: set[str] = set()

    async def _maybe_reconcile(
        workspace_id: str,
        user_id: str,
        backend: Any,
        *,
        skip_access_check: bool = False,
    ) -> None:
        if backend is None or workspace_id in reconciled_workspaces:
            return
        reconciled_workspaces.add(workspace_id)
        try:
            await apply_pending(
                workspace_id,
                backend,
                user_id=user_id,
                skip_access_check=skip_access_check,
            )
        except Exception as e:
            logger.debug("apply_pending skipped for %s: %s", workspace_id, e)

    @tool("knowledge_update")
    async def _knowledge_update(
        doc: DocLiteral,
        ops: list[dict[str, Any]],
        reason: str,
        base_version: int | None = None,
        evidence_refs: list[dict[str, Any]] | None = None,
        dry_run: bool = False,
        workspace_id: str | None = None,
        *,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Change one knowledge document through validated operations; prefer this over editing data/*.json by hand.

        Which docs the workspace accepts follows its coverage profile: a company workspace takes company/thesis/debate/drivers/management/lessons/agenda/models; a sector workspace (bound to a sector coverage unit) takes sector/thesis/debate/drivers/lessons/agenda/models — management and company are company-only and are refused there, and sector is refused in a company workspace. Shared docs (thesis 等) validate identically in both; 行业 thesis 不强制公司目标价等公司专属字段。
        Writing requires the caller to own the workspace or hold rw on it; external members are fully isolated — colleagues' workspaces are not visible to them at all (a 404 is an identity limit — report it, don't retry or hand-edit around it).
        Call knowledge_read(doc=...) first and pass its returned latest_version_no as base_version (pass null when it returns null — data/<doc>.json carries no version number).
        Ops address list items by id (or name/side in underwriting), e.g. {"op":"append","path":"/evidence","value":{...}},
        {"op":"set","path":"/pillars/P2/status","value":"watch"}. reason says why, in one or two sentences.
        When writing thesis v2 (underwriting or actionability), stance in long/short/neutral requires market_view, house_view.{horizon_months,actionability,binding_constraint,flip_conditions}, and underwriting.{anchor,states,protection,breakeven_verdict} (weight_bounds optional), and source.type="market_color" may only back market_view or expectations (never pillars, underwriting, or reported/guidance figures).
        写 `thesis`（尤其包含 `underwriting` 赔率卡）时，请务必先传 `dry_run=True` 查看赔率计算摘要（含三情景价格区间、达标终值、`base` 权重占 25%/50% 时达标所需概率、全部权重组合是否都过最低回报要求、最不利情景相对基准股价的不利变动幅度、估值倍数变动占目标回报比例、基准股价是否过期），校验通过且数字无误后再传 `dry_run=False` 正式保存。
        On validation failure, the returned message includes the expected schema structure (失败时返回的信息里带有期望结构，照着补齐再提交).
        Returns: the new version and the rendered file (or dry-run preview/diff), plus the underwriting odds summary when doc="thesis" carries underwriting, or the list of problems to fix.
        """
        user_id, cfg_workspace_id, thread_id, run_id = _extract_run_identity(config)
        cfg = (config or {}).get("configurable") or {}
        admitted_ws_id = str(cfg.get("turn_admitted_workspace_id") or "")
        target_ws_id = (str(workspace_id).strip() if workspace_id else "") or (
            cfg_workspace_id or ""
        )
        if not user_id or not target_ws_id or target_ws_id.lower() == "home":
            return "ERROR: user_id and workspace_id are required in RunnableConfig."

        # When writing to the turn's already-admitted workspace, skip redundant
        # inner access checks so a mid-turn revocation does not break an in-flight
        # turn; cross-workspace writes (e.g. from Home) still check channel="cos".
        already_admitted = bool(admitted_ws_id and target_ws_id == admitted_ws_id)
        if not already_admitted:
            await verify_workspace_access(
                target_ws_id, user_id, need="write", channel="cos"
            )
        eff_backend = (
            filesystem_backend if target_ws_id == cfg_workspace_id else None
        )
        if not dry_run:
            await _maybe_reconcile(
                target_ws_id, user_id, eff_backend, skip_access_check=True
            )
        res = await apply_update(
            user_id=user_id,
            workspace_id=target_ws_id,
            doc_key=doc,
            ops=ops,
            reason=reason,
            base_version=base_version,
            evidence_refs=evidence_refs,
            source="tool",
            backend=eff_backend,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=tool_call_id or None,
            dry_run=dry_run,
            skip_access_check=True,
        )
        msg = str(res["message"])
        uw_summary = ""
        if doc == "thesis":
            candidate_doc = res.get("preview_doc") or res.get("content")
            if isinstance(candidate_doc, dict) and isinstance(
                candidate_doc.get("underwriting"), dict
            ):
                uw_summary = _format_underwriting_summary(
                    candidate_doc,
                    ok=bool(res.get("ok")),
                    dry_run=dry_run,
                    version_no=res.get("version_no"),
                    validation_errors=res.get("problems"),
                )
        if dry_run:
            parts = [msg]
            if uw_summary:
                parts.append(uw_summary)
            diff_val = res.get("diff")
            if diff_val is not None:
                diff_json = json.dumps(diff_val, ensure_ascii=False, indent=2)
                parts.append(f"Diff (before/after):\n```json\n{diff_json}\n```")
            preview_doc = res.get("preview_doc")
            if preview_doc is not None:
                preview_json = json.dumps(preview_doc, ensure_ascii=False, indent=2)
                total_len = len(preview_json)
                if total_len > 8000:
                    preview_json = (
                        f"{preview_json[:8000]}\n... (truncated at 8000 chars, total {total_len} chars)"
                    )
                parts.append(f"Preview document (preview_doc):\n```json\n{preview_json}\n```")
            return "\n\n".join(parts)
        if uw_summary:
            return f"{msg}\n\n{uw_summary}"
        return msg

    @tool("knowledge_append_verdict")
    async def _knowledge_append_verdict(
        verdict: dict[str, Any],
        agenda_ops: list[dict[str, Any]] | None = None,
        workspace_id: str | None = None,
        *,
        config: RunnableConfig,
        tool_call_id: Annotated[str, InjectedToolCallId] = "",
    ) -> str:
        """Record an event verdict (NO_CHANGE/THESIS_STRENGTHENED/THESIS_WEAKENED/THESIS_BROKEN/NEW_THESIS) and its next validation; every research session that looked at new evidence ends with one.

        verdict 的字段（additionalProperties=false，多余字段整条被拒；at/id 由服务端生成，不要传）：
        - 必填：verdict（上述五词之一）；event（一句话事件，含期间与数字）；rationale（定量依据）；progress（"none"|"some"|"resolved"）；
          trigger：{"kind": filing|earnings|price_move|consensus_change|indicator|due_validation|pm_question|internal_material|agenda|director|manual, "ref": 唤醒信号 id / accession / V- 编号, "automation_execution_id": 仅自动任务}；
          next_validation：{"what": 下一项看什么, "due": "YYYY-MM-DD"（不能是 null）, "validation_id": 可选 V-xxx}——不是 text/when。
        - 可选：affected（{pillars:[], debates:[], drivers:[]}）；evidence_ids（["E-…"]——不是 evidence/evidence_refs/basis/summary）；question_ids（["Q-…"]）；house_view_changed；
          `payoff_assessment`（`dalton-stock-move` 与 `dalton-thesis-delta` 每条 `verdict` **必填**，缺了在能识别调用来源时直接拒存；其他技能可选）：评估当前价格下的赔率是否发生实质变化。赔率没变填 `{"changed": false, "reason": "none"}`；赔率变了填 `{"changed": true, "reason": "<枚举值>"}`（`changed=false` 时 `reason` 只能填 `"none"`，`changed=true` 时不得填 `"none"`），`reason` 五选一：`"price_crossed_band"`（三情景假设未改，单纯股价越过三情景或买点价带）| `"scenario_revised"`（重算并调整了三情景 EPS 或估值倍数假设）| `"expectations_migrated"`（三情景假设未改，但 Street consensus 或买方 bogey 明显移动）| `"catalyst_expired"`（支撑催化剂已兑现、落空或过期）| `"protection_failed"`（生存、价值或路径任一下行保护条件失效）。
        最小示例：{"verdict":"NO_CHANGE","event":"…","rationale":"…","progress":"none","trigger":{"kind":"internal_material","ref":"<信号id>"},"next_validation":{"what":"…","due":"2026-11-05"}}；被拒时返回只列未知字段与缺少的字段，照改后原样重试。

        Writing requires the caller to own the workspace or hold rw on it; external members are fully isolated — colleagues' workspaces are not visible to them at all (a 404 is an identity limit — report it, don't retry).
        验证完 agenda 里的某一项，要在 agenda_ops 里显式关掉它，或者把 trigger.ref 设成该 validation id；next_validation 写的是下一项要看什么，不会关闭任何项。
        Returns: the verdict id and the agenda version it updated; agenda 更新失败时返回 verdict_saved/agenda_saved 与补救调用。
        """
        user_id, cfg_workspace_id, thread_id, run_id = _extract_run_identity(config)
        cfg = (config or {}).get("configurable") or {}
        admitted_ws_id = str(cfg.get("turn_admitted_workspace_id") or "")
        target_ws_id = (str(workspace_id).strip() if workspace_id else "") or (
            cfg_workspace_id or ""
        )
        if not user_id or not target_ws_id or target_ws_id.lower() == "home":
            return "ERROR: user_id and workspace_id are required in RunnableConfig."

        already_admitted = bool(admitted_ws_id and target_ws_id == admitted_ws_id)
        if not already_admitted:
            await verify_workspace_access(
                target_ws_id, user_id, need="write", channel="cos"
            )
        refs = extract_verdict_signal_refs(verdict)
        if refs:
            from src.dalton.database import watch as watch_db

            await watch_db.verify_signal_refs_in_workspace(refs, target_ws_id)

        eff_backend = (
            filesystem_backend if target_ws_id == cfg_workspace_id else None
        )
        await _maybe_reconcile(
            target_ws_id, user_id, eff_backend, skip_access_check=True
        )
        origin_type, auto_exec_id = await _resolve_thread_origin(thread_id)
        # DALTON-HOOK(payoff-assessment): reject at save time where the caller
        # is reliably identifiable (price_move trigger, or THIS TURN stamped as
        # sent by a stock-move / thesis-delta automation) — a fieldless verdict
        # that lands in the append-only table can only be healed by appending
        # another row, and unattended automation runs may never call
        # dalton_check_done to be told so. Manual turns in an automation's
        # thread carry no stamp and pass untouched.
        if not isinstance(verdict.get("payoff_assessment"), dict):
            why = await _payoff_required_because(verdict, config)
            if why:
                return (
                    "ERROR: `verdict` 缺少 `payoff_assessment`（"
                    + why
                    + "），本条未保存（NOT SAVED）。"
                    "股价异动归因与每日增量跟踪的每条 `verdict` 都要评估当前价格下的赔率；"
                    "补上该字段后原样重试：赔率没变传 "
                    '`{"changed": false, "reason": "none"}`；'
                    "赔率变了传 "
                    '`{"changed": true, "reason": "<枚举值>"}`'
                    "（`price_crossed_band` 单纯股价越过三情景或买点价带 | "
                    "`scenario_revised` 重算了三情景 EPS 或估值倍数假设 | "
                    "`expectations_migrated` Street consensus 或买方 bogey 明显移动 | "
                    "`catalyst_expired` 催化剂已兑现或过期 | "
                    "`protection_failed` 下行保护失效）。"
                )
        res = await append_verdict(
            user_id=user_id,
            workspace_id=target_ws_id,
            verdict=verdict,
            agenda_ops=agenda_ops,
            backend=eff_backend,
            thread_id=thread_id,
            run_id=run_id,
            tool_call_id=tool_call_id or None,
            origin_type=origin_type,
            automation_execution_id=auto_exec_id,
            skip_access_check=True,
        )
        if res.get("ok"):
            await resolve_verdict_signals(
                workspace_id=target_ws_id,
                verdict=verdict,
                verdict_id=str(res.get("verdict_id") or "") or None,
                execution_id=auto_exec_id,
            )
            if res.get("agenda_update") == "failed":
                # Partial success the model must not read as done: the verdict
                # row exists, the agenda does not. The recovery is a READ of
                # the agenda's current version, then one versioned update — an
                # unversioned retry on an existing agenda only hits
                # version_conflict and fails again.
                verdict_id = res.get("verdict_id")
                ws_arg = f'workspace_id="{target_ws_id}", ' if workspace_id else ""
                recovery = (
                    "verdict 已入库、agenda 未更新，按以下两步补救"
                    "（不要重复调用 knowledge_append_verdict，会写第二条 verdict）：\n"
                    f'1. 读当前版本：knowledge_read({ws_arg}doc="agenda")，'
                    "取返回 JSON 里的 latest_version_no（最新版本，含校验失败的行——"
                    "不要用 version，那是最新有效版本，用它必然 version_conflict）；\n"
                    f'2. 带 base_version 重试：knowledge_update({ws_arg}doc="agenda", '
                    "ops=[…按 detail 里的期望结构…], "
                    "base_version=<第 1 步读到的 latest_version_no>, "
                    f'reason="补写 verdict {verdict_id} 的 agenda 更新")。'
                    "\n返回的 latest_version_no 为 null（库里没有任何 agenda 行）时："
                    'ops 用 [{"op":"create","value":{…}}]，base_version=null。'
                )
                return json.dumps(
                    {
                        "verdict_saved": True,
                        "agenda_saved": False,
                        "verdict_id": verdict_id,
                        "recovery": recovery,
                        "detail": str(res["message"]),
                    },
                    ensure_ascii=False,
                )
        return str(res["message"])

    @tool("knowledge_read")
    async def _knowledge_read(
        workspace_id: str | None = None,
        doc: str = "all",
        limit: int = 20,
        *,
        config: RunnableConfig,
    ) -> str:
        """Read knowledge documents or recent event verdicts for a workspace (own or colleague's visible workspace).

        Args:
            workspace_id: Target workspace UUID (defaults to current workspace in RunnableConfig).
            doc: Document key ("company", "sector", "thesis", "debate", "drivers", "management", "lessons", "agenda", "models"), "verdicts", or "all" (default). A sector workspace holds sector instead of company/management; asking for a doc the workspace's profile refuses returns content null — switch doc or ask in the right workspace.
            limit: Maximum number of verdicts to return when doc is "verdicts" or "all" (default 20).

        Returns:
            JSON string with the requested knowledge document(s) and/or verdicts. 单文档时带 `latest_version_no`（最新版本号，含校验失败的行；无任何行为 `null`）——做 `knowledge_update` 时用它作 `base_version`（`version` 是最新有效版本号，最新行无效时用它会 `version_conflict`）。当读取的 `thesis` 含 `underwriting` 时，返回 JSON 中一并附带 `underwriting_summary`（含三情景价格区间、达标终值与盈亏平衡所需概率）。
        """
        user_id, cfg_workspace_id, _, _ = _extract_run_identity(config)
        target_ws_id = (str(workspace_id).strip() if workspace_id else "") or (
            cfg_workspace_id or ""
        )
        if not user_id or not target_ws_id or target_ws_id.lower() == "home":
            return "ERROR: user_id and workspace_id are required."

        await verify_workspace_access(
            target_ws_id, user_id, need="read", channel="cos"
        )
        cleaned_doc = str(doc or "all").strip().lower()
        safe_limit = max(1, min(100, int(limit or 20)))

        if cleaned_doc == "verdicts":
            items = await db_verdicts.list_verdicts(target_ws_id, limit=safe_limit)
            return json.dumps(
                {"ok": True, "workspace_id": target_ws_id, "verdicts": items},
                ensure_ascii=False,
                default=str,
            )

        if cleaned_doc in ALL_DOC_KEYS:
            row = await db_knowledge.latest_valid_version(target_ws_id, cleaned_doc)
            # latest_version_no is the LATEST row's number, valid or not — the
            # exact value apply_update validates base_version against. It is
            # the number to retry knowledge_update with when the newest write
            # was rejected (invalid), where `version` (latest VALID) would
            # collide.
            latest_row = await db_knowledge.latest_version(target_ws_id, cleaned_doc)
            content_val = row.get("content") if row else None
            ver_no = row.get("version_no") if row else None
            payload: dict[str, Any] = {
                "ok": True,
                "workspace_id": target_ws_id,
                "doc": cleaned_doc,
                "version": ver_no,
                "latest_version_no": (
                    latest_row.get("version_no") if latest_row else None
                ),
                "content": content_val,
            }
            if (
                cleaned_doc == "thesis"
                and isinstance(content_val, dict)
                and isinstance(content_val.get("underwriting"), dict)
            ):
                uw_sum = _format_underwriting_summary(
                    content_val,
                    ok=True,
                    dry_run=False,
                    version_no=ver_no,
                )
                if uw_sum:
                    payload["underwriting_summary"] = uw_sum
            return json.dumps(
                payload,
                ensure_ascii=False,
                default=str,
            )

        latest_map = await db_knowledge.list_all_latest_for_workspace(
            target_ws_id, valid_only=True
        )
        docs_out: dict[str, dict[str, Any]] = {}
        for k, v in latest_map.items():
            item_content = v.get("content")
            item_ver = v.get("version_no")
            entry: dict[str, Any] = {
                "version_no": item_ver,
                "content": item_content,
            }
            if (
                k == "thesis"
                and isinstance(item_content, dict)
                and isinstance(item_content.get("underwriting"), dict)
            ):
                uw_sum = _format_underwriting_summary(
                    item_content,
                    ok=True,
                    dry_run=False,
                    version_no=item_ver,
                )
                if uw_sum:
                    entry["underwriting_summary"] = uw_sum
            docs_out[k] = entry
        verdicts_out = await db_verdicts.list_verdicts(target_ws_id, limit=safe_limit)
        return json.dumps(
            {
                "ok": True,
                "workspace_id": target_ws_id,
                "documents": docs_out,
                "verdicts": verdicts_out,
            },
            ensure_ascii=False,
            default=str,
        )

    return [_knowledge_update, _knowledge_append_verdict, _knowledge_read]


def knowledge_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Return the company-knowledge tools bound to ``filesystem_backend``."""
    return create_knowledge_tools(filesystem_backend)


_DEFAULT_TOOLS = create_knowledge_tools(None)
knowledge_update = _DEFAULT_TOOLS[0]
knowledge_append_verdict = _DEFAULT_TOOLS[1]
knowledge_read = _DEFAULT_TOOLS[2]
