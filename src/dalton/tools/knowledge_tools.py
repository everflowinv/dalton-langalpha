"""Direct LangChain tools for the Dalton company knowledge layer (PK.4)."""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.auth import verify_workspace_access
from src.dalton.database import knowledge as db_knowledge
from src.dalton.database import verdicts as db_verdicts
from src.dalton.knowledge.paths import DOC_KEYS
from src.dalton.knowledge.reconcile import apply_pending
from src.dalton.knowledge.service import append_verdict, apply_update
from src.server.database.pool import get_db_connection
from src.tools.secretary._commands import InjectedToolCallId

logger = logging.getLogger(__name__)

DocLiteral = Literal[
    "company",
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


def create_knowledge_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Build ``[knowledge_update, knowledge_append_verdict, knowledge_read]`` bound to ``filesystem_backend``."""
    reconciled_workspaces: set[str] = set()

    async def _maybe_reconcile(
        workspace_id: str, user_id: str, backend: Any
    ) -> None:
        if backend is None or workspace_id in reconciled_workspaces:
            return
        reconciled_workspaces.add(workspace_id)
        try:
            await apply_pending(workspace_id, backend, user_id=user_id)
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
        """Change one company-knowledge document through validated operations; prefer this over editing data/*.json by hand.

        Writing requires the caller to own the workspace or hold rw on it; external members are fully isolated — colleagues' workspaces are not visible to them at all (a 404 is an identity limit — report it, don't retry or hand-edit around it).
        Read data/<doc>.json first and pass its version as base_version (null only to create the document).
        Ops address list items by id, e.g. {"op":"append","path":"/evidence","value":{...}},
        {"op":"set","path":"/pillars/P2/status","value":"watch"}. reason says why, in one or two sentences.
        Pass dry_run=True to validate a draft and preview the diff and expected schema structure without saving a DB version or rendering files; once dry_run passes, submit with dry_run=False.
        On validation failure, the returned message includes the expected schema structure (失败时返回的信息里带有期望结构，照着补齐再提交).
        Returns: the new version and the rendered file (or dry-run preview/diff), or the list of problems to fix.
        """
        user_id, cfg_workspace_id, thread_id, run_id = _extract_run_identity(config)
        target_ws_id = (str(workspace_id).strip() if workspace_id else "") or (
            cfg_workspace_id or ""
        )
        if not user_id or not target_ws_id or target_ws_id.lower() == "home":
            return "ERROR: user_id and workspace_id are required in RunnableConfig."

        await verify_workspace_access(
            target_ws_id, user_id, need="write", channel="cos"
        )
        eff_backend = (
            filesystem_backend if target_ws_id == cfg_workspace_id else None
        )
        if not dry_run:
            await _maybe_reconcile(target_ws_id, user_id, eff_backend)
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
        )
        msg = str(res["message"])
        if dry_run:
            parts = [msg]
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
        - 可选：affected（{pillars:[], debates:[], drivers:[]}）；evidence_ids（["E-…"]——不是 evidence/evidence_refs/basis/summary）；question_ids（["Q-…"]）；house_view_changed。
        最小示例：{"verdict":"NO_CHANGE","event":"…","rationale":"…","progress":"none","trigger":{"kind":"internal_material","ref":"<信号id>"},"next_validation":{"what":"…","due":"2026-11-05"}}；被拒时返回只列未知字段与缺少的字段，照改后原样重试。

        Writing requires the caller to own the workspace or hold rw on it; external members are fully isolated — colleagues' workspaces are not visible to them at all (a 404 is an identity limit — report it, don't retry).
        验证完 agenda 里的某一项，要在 agenda_ops 里显式关掉它，或者把 trigger.ref 设成该 validation id；next_validation 写的是下一项要看什么，不会关闭任何项。
        Returns: the verdict id and the agenda version it updated; agenda 更新失败时返回 verdict_saved/agenda_saved 与补救调用。
        """
        user_id, cfg_workspace_id, thread_id, run_id = _extract_run_identity(config)
        target_ws_id = (str(workspace_id).strip() if workspace_id else "") or (
            cfg_workspace_id or ""
        )
        if not user_id or not target_ws_id or target_ws_id.lower() == "home":
            return "ERROR: user_id and workspace_id are required in RunnableConfig."

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
        await _maybe_reconcile(target_ws_id, user_id, eff_backend)
        origin_type, auto_exec_id = await _resolve_thread_origin(thread_id)
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
        """Read company-knowledge documents or recent event verdicts for a workspace (own or colleague's visible workspace).

        Args:
            workspace_id: Target workspace UUID (defaults to current workspace in RunnableConfig).
            doc: Document key ("company", "thesis", "debate", "drivers", "management", "lessons", "agenda", "models"), "verdicts", or "all" (default).
            limit: Maximum number of verdicts to return when doc is "verdicts" or "all" (default 20).

        Returns:
            JSON string with the requested knowledge document(s) and/or verdicts. 单文档时带 latest_version_no（最新版本号，含校验失败的行；无任何行为 null）——做 knowledge_update 时用它作 base_version（version 是最新有效版本号，最新行无效时用它会 version_conflict）。
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

        if cleaned_doc in DOC_KEYS:
            row = await db_knowledge.latest_valid_version(target_ws_id, cleaned_doc)
            # latest_version_no is the LATEST row's number, valid or not — the
            # exact value apply_update validates base_version against. It is
            # the number to retry knowledge_update with when the newest write
            # was rejected (invalid), where `version` (latest VALID) would
            # collide.
            latest_row = await db_knowledge.latest_version(target_ws_id, cleaned_doc)
            return json.dumps(
                {
                    "ok": True,
                    "workspace_id": target_ws_id,
                    "doc": cleaned_doc,
                    "version": row.get("version_no") if row else None,
                    "latest_version_no": (
                        latest_row.get("version_no") if latest_row else None
                    ),
                    "content": row.get("content") if row else None,
                },
                ensure_ascii=False,
                default=str,
            )

        latest_map = await db_knowledge.list_all_latest_for_workspace(
            target_ws_id, valid_only=True
        )
        docs_out = {
            k: {
                "version_no": v.get("version_no"),
                "content": v.get("content"),
            }
            for k, v in latest_map.items()
        }
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
