"""Unified research-material ingestion tools (D1): `ingest_sales_note` and the
`ingest_research` router.

Routing follows "who is speaking" (`.agy/ref/openclaw_AGENTS_ingest.md` rule 2,
user decision 2026-10-19): sell-side voices go to sales notes (OpenClaw
market-digest `add-note`, run host-side by the researchdata bridge); expert
calls, company meetings and everything else go to the company wiki. A
workspace whose `dalton_auto_ingest` config flag is on ingests confirmed
material without the two-step preview and answers with a one-line receipt;
suspected duplicates or an undecided classification still ask.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import uuid
from typing import Any, Literal

import httpx
from fastapi import HTTPException
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.access_tiers import get_user_tier
from src.dalton.auth import resolve_caller_home_workspace_id, verify_workspace_access
from src.dalton.settings import get_dalton_bridge_admin_token, get_dalton_bridge_base_url
from src.dalton.tools.knowledge_tools import _extract_run_identity
from src.dalton.tools.wiki_tools import (
    WIKI_INGEST_RATE_LIMIT_PER_HOUR,
    _read_workspace_file_safely,
    _turn_admitted_workspace,
    check_and_increment_wiki_ingest_rate_limit,
)

logger = logging.getLogger(__name__)

SALES_NOTES_HTTP_TIMEOUT_S: float = 190.0
SALES_NOTES_RATE_LIMIT_PER_HOUR: int = 30
EXTERNAL_INGEST_FORBIDDEN_MESSAGE = (
    "外部成员不能写入共享研究库（ingest_sales_note / ingest_research 仅管理员与内部成员可用）；"
    "请如实告知用户这一身份限制，不要尝试绕过。"
)

SpeakerLiteral = Literal["sell_side", "expert_or_company"]

#: `workspaces.config` key for the workspace-level auto-ingest switch.
AUTO_INGEST_CONFIG_KEY = "dalton_auto_ingest"


async def get_workspace_auto_ingest(workspace_id: str) -> bool:
    """Whether *workspace_id* has auto-ingest on (`workspaces.config`, D1)."""
    try:
        from src.server.database.workspace import get_workspace

        workspace = await get_workspace(workspace_id)
    except Exception as exc:
        logger.warning(
            "auto-ingest flag lookup failed for workspace %s: %s",
            workspace_id,
            type(exc).__name__,
        )
        return False
    config = (workspace or {}).get("config")
    if isinstance(config, dict):
        return config.get(AUTO_INGEST_CONFIG_KEY) is True
    return False


async def call_bridge_sales_notes_ingest(payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    """POST `payload` to `{bridge_url}/sales-notes/ingest` with the admin token."""
    bridge_url = get_dalton_bridge_base_url().rstrip("/")
    admin_token = get_dalton_bridge_admin_token()
    if not admin_token:
        return 500, {
            "ok": False,
            "status": "config_error",
            "error": "DALTON_BRIDGE_ADMIN_TOKEN is not configured on backend.",
        }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {admin_token}",
        "X-Dalton-Bridge-Token": admin_token,
    }
    async with httpx.AsyncClient(timeout=SALES_NOTES_HTTP_TIMEOUT_S, trust_env=False) as client:
        resp = await client.post(
            f"{bridge_url}/sales-notes/ingest", json=payload, headers=headers
        )
        try:
            data = resp.json()
            if not isinstance(data, dict):
                data = {"ok": False, "status": "invalid_response", "error": str(data)}
        except Exception:
            data = {
                "ok": False,
                "status": "http_error",
                "error": f"Bridge returned HTTP {resp.status_code}",
            }
        return resp.status_code, data


async def _resolve_ingest_identity(
    config: RunnableConfig,
    filesystem_backend: Any,
    *,
    allow_home_workspace: bool,
) -> tuple[str, str, str | None, str | None]:
    """`(user_id, workspace_id, resolved_home_error, file_read_error_marker)`.

    Returns the caller identity after the same gates `ingest_company_wiki`
    applies; an error string in place three aborts the call.
    """
    user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
    if not user_id:
        return "", "", "ERROR: user_id and workspace_id are required in RunnableConfig.", None
    if allow_home_workspace and (not workspace_id or workspace_id.strip().lower() == "home"):
        try:
            workspace_id = await resolve_caller_home_workspace_id(user_id)
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            return user_id, "", f"ERROR: {detail}", None
    if not workspace_id or workspace_id.strip().lower() == "home":
        return user_id, "", "ERROR: user_id and workspace_id are required in RunnableConfig.", None
    try:
        await verify_workspace_access(workspace_id, user_id)
    except HTTPException as exc:
        return user_id, workspace_id, f"ERROR: {exc.detail}", None
    return user_id, workspace_id, thread_id, None


async def _ingest_file_payload(
    filesystem_backend: Any, file_path: str
) -> tuple[dict[str, Any] | None, str, str | None]:
    """Read `file_path` into the bridge's `file` payload; `(payload, sha256, error)`.

    The error is `None` on success and a non-empty string on failure — the
    same convention `_read_workspace_file_safely` uses, so callers can test
    `err is not None` without an empty string reading as a failure.
    """
    fname, fbytes, ferr = await _read_workspace_file_safely(filesystem_backend, file_path)
    if ferr is not None or fname is None or fbytes is None:
        return None, "", ferr or "Failed reading file_path."
    return (
        {"filename": fname, "content_b64": base64.b64encode(fbytes).decode("ascii")},
        hashlib.sha256(fbytes).hexdigest(),
        None,
    )


async def _ingest_text_payload(content: str) -> tuple[str, str]:
    norm = " ".join(content.replace("\r\n", "\n").replace("\r", "\n").split())
    return content, hashlib.sha256(norm.encode("utf-8")).hexdigest()


def build_ingest_sales_note_tool(
    filesystem_backend: Any = None,
    *,
    allow_home_workspace: bool = False,
) -> BaseTool:
    """Build `ingest_sales_note`: sell-side material into sales notes, two-step."""

    @tool("ingest_sales_note")
    async def _ingest_sales_note(
        institution: str,
        title: str,
        kind: str | None = None,
        analyst: str | None = None,
        doc_date: str | None = None,
        tickers: list[str] | None = None,
        source_url: str | None = None,
        date: str | None = None,
        content: str | None = None,
        file_path: str | None = None,
        confirm: bool = False,
        *,
        config: RunnableConfig,
    ) -> str:
        """把卖方观点（券商研报、点评、卖方电话会纪要、sales color）入库到 sales notes；专家会、公司会和其他一手资料请改用 ingest_company_wiki。

        仅管理员与内部成员可入库（外部成员调用会被直接拒绝，preview 同样拒绝）。
        必须分两步：先传 confirm=False 获取预览并向用户确认；用户同意后再传 confirm=True 写入。同一正文重复入库由库端按内容指纹判重（返回 duplicate，不写第二份）。
        Args:
            institution: 卖方机构名（必填，如 'Morgan Stanley'）。
            title: 标题（必填）。
            kind: '研报' | '点评' | '卖方电话会' | '销售观点'（可选）。
            analyst: 分析师姓名（可选）。
            doc_date: 报告或会议本身的日期 YYYY-MM-DD（可选）。
            tickers: 相关标的代码列表（可选，最多 10 个）。
            source_url: 原始链接 http/https（可选）。
            date: 收到日期 YYYY-MM-DD（可选，默认今天）。
            content: 转写稿/整理稿正文（与 file_path 二选一，<=200k 字符）。
            file_path: 当前工作区内的原件路径（与 content 二选一，支持 .md/.txt/.pdf/.docx/.png/.jpg/.webp，<=20MB）。
            confirm: False 仅校验并返回预览；True 正式写入。
        Returns: JSON 字符串，含预览（confirm=False）或入库结果与 doc_key/document_id（confirm=True）。
        """
        user_id, err = await _require_internal_ingest_caller(config)
        if err is not None:
            return err
        workspace_id, thread_id, id_err = await _ingest_workspace_identity(
            config, allow_home_workspace
        )
        if id_err is not None:
            return id_err

        file_payload, payload_sha256, payload_err = await _load_ingest_payload(
            filesystem_backend, content, file_path
        )
        if payload_err is not None:
            return payload_err

        if confirm:
            rate_err = await _check_ingest_rate_limit(
                user_id, scope="sales_notes", limit=SALES_NOTES_RATE_LIMIT_PER_HOUR
            )
            if rate_err is not None:
                return rate_err

        request_id = (
            str(uuid.uuid5(
                uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
                f"sales-note:{user_id}:{workspace_id}:{thread_id or ''}:"
                f"{institution}:{title}:{payload_sha256}",
            ))
            if confirm
            else None
        )
        payload: dict[str, Any] = {
            "institution": institution,
            "title": title,
            "kind": kind,
            "analyst": analyst,
            "doc_date": doc_date,
            "tickers": tickers,
            "source_url": source_url,
            "date": date,
            "content": content if file_payload is None else None,
            "file": file_payload,
            "ingested_by": {"user_id": user_id},
            "ingested_via": {"workspace_id": workspace_id, "thread_id": thread_id or ""},
            "dry_run": not bool(confirm),
        }
        if request_id is not None:
            payload["request_id"] = request_id
        try:
            _code, resp = await call_bridge_sales_notes_ingest(payload)
        except Exception as exc:
            logger.warning("Bridge sales-notes ingest call failed: %s", type(exc).__name__)
            return json.dumps(
                {
                    "ok": False,
                    "status": "bridge_unreachable",
                    "request_id": request_id,
                    "error": f"Failed calling researchdata bridge /sales-notes/ingest: {type(exc).__name__}",
                },
                ensure_ascii=False,
            )
        if request_id is not None:
            resp.setdefault("request_id", request_id)
        return json.dumps(resp, ensure_ascii=False)

    return _ingest_sales_note


async def _require_internal_ingest_caller(config: RunnableConfig) -> tuple[str, str | None]:
    user_id, workspace_id, _thread_id, _ = _extract_run_identity(config)
    if not user_id:
        return "", "ERROR: user_id and workspace_id are required in RunnableConfig."
    if await get_user_tier(user_id) == "external":
        return user_id, json.dumps(
            {
                "ok": False,
                "status": "forbidden",
                "error_code": "external_member_forbidden",
                "error": EXTERNAL_INGEST_FORBIDDEN_MESSAGE,
            },
            ensure_ascii=False,
        )
    return user_id, None


async def _check_ingest_rate_limit(
    user_id: str, *, scope: str, limit: int
) -> str | None:
    """The per-user hourly write counter every ingest write shares (manual and auto).

    Returns the JSON error answer when the write must be refused, `None` when
    it may go on. Redis unavailable fails closed exactly like the manual
    `confirm=True` path in `ingest_company_wiki` — the wording is the same.
    """
    allowed, current_count = await check_and_increment_wiki_ingest_rate_limit(
        user_id, scope=scope, limit=limit
    )
    if allowed:
        return None
    if current_count < 0:
        return json.dumps(
            {
                "ok": False,
                "status": "rate_limit_unavailable",
                "error_code": "rate_limit_unavailable",
                "error": "限流服务（Redis）不可用，出于安全策略拒绝正式写入（预览仍可使用）。",
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "ok": False,
            "status": "rate_limited",
            "error_code": "rate_limited",
            "limit_per_hour": limit,
            "current_count": current_count,
            "error": (
                f"入库频率超限：每用户每小时最多写入 {limit} 次"
                f"（当前第 {current_count} 次），请稍后再试。"
            ),
        },
        ensure_ascii=False,
    )


def _suspected_duplicate(resp: dict[str, Any]) -> bool:
    """Whether a bridge answer says the library already holds this content.

    One canonical spelling for both libraries (r3): the bridge normalizes the
    CLI's own `status`/`result`/`duplicate` verdicts into a top-level
    `duplicate` field on every preview and write answer, so this check reads
    exactly that field (plus the `status` spelling the wiki endpoints answer
    with) instead of replaying the normalization here.
    """
    return resp.get("duplicate") is True or str(resp.get("status") or "").strip().lower() == "duplicate"


def _duplicate_question(library: str, resp: dict[str, Any]) -> str:
    """The ask-first answer for a suspected duplicate on an auto-ingest write.

    Auto-ingest skips the two-step preview exactly as long as the library is
    clean; a duplicate is the one case where writing on would be a mistake the
    user cannot see, so the flow falls back to asking — the same confirm
    question the switch-off path shows, with the existing record named.
    """
    existing = (
        resp.get("existing_filepath")
        or resp.get("existing_doc_id")
        or resp.get("doc_key")
        or resp.get("document_id")
        or ""
    )
    reason = str(resp.get("duplicate_reason") or "").strip()
    detail = f"（原因：{reason}）" if reason else ""
    where = f"，已有记录：{existing}" if existing else ""
    return json.dumps(
        {
            "ok": False,
            "status": "duplicate_suspected",
            "duplicate": True,
            "error": "疑似重复，请确认是否仍要入库。",
            "question": (
                f"请先问用户：这份材料与 {library} 已有记录疑似重复{detail}{where}。"
                "用户确认仍要入库时用 confirm=True 且 allow_similar=True 重新调用"
                "（仅跳过相似度判重；完全相同的内容仍会按指纹拒绝）；"
                "用户不需要就放弃本次入库。"
            ),
            "preview": resp,
        },
        ensure_ascii=False,
    )


async def _ingest_workspace_identity(
    config: RunnableConfig, allow_home_workspace: bool
) -> tuple[str, str | None, str | None]:
    """`(workspace_id, thread_id, error)` after the home-resolution and access gates."""
    user_id, workspace_id, thread_id, _ = _extract_run_identity(config)
    if allow_home_workspace and (not workspace_id or workspace_id.strip().lower() == "home"):
        try:
            workspace_id = await resolve_caller_home_workspace_id(user_id)
        except Exception as exc:
            detail = getattr(exc, "detail", None) or str(exc)
            return "", thread_id, f"ERROR: {detail}"
    if not workspace_id or workspace_id.strip().lower() == "home":
        return "", thread_id, "ERROR: user_id and workspace_id are required in RunnableConfig."
    try:
        # DALTON-HOOK(colleague-dispatch): the turn's admitted workspace passed
        # START admission already; the ingest stays attributed to the caller.
        if not _turn_admitted_workspace(config, workspace_id):
            await verify_workspace_access(workspace_id, user_id)
    except HTTPException as exc:
        return "", thread_id, f"ERROR: {exc.detail}"
    except Exception as exc:
        return "", thread_id, f"ERROR: {exc}"
    return workspace_id, thread_id, None


async def _load_ingest_payload(
    filesystem_backend: Any, content: str | None, file_path: str | None
) -> tuple[dict[str, Any] | None, str, str | None]:
    """The bridge payload half (`content` xor `file`) plus its sha256."""
    has_content = content is not None and str(content).strip() != ""
    has_file = file_path is not None and str(file_path).strip() != ""
    if has_content == has_file:
        return None, "", "ERROR: Provide exactly one of content or file_path (not both, not neither)."
    if has_file:
        file_payload, sha256, err = await _ingest_file_payload(filesystem_backend, str(file_path))
        if err is not None:
            return None, "", f"ERROR: {err}"
        return file_payload, sha256, None
    _content, sha256 = await _ingest_text_payload(str(content))
    return None, sha256, None


def build_ingest_research_tool(
    filesystem_backend: Any = None,
    *,
    allow_home_workspace: bool = False,
) -> BaseTool:
    """Build `ingest_research`: route material to sales notes or the wiki by who speaks."""

    @tool("ingest_research")
    async def _ingest_research(
        speaker: SpeakerLiteral | None = None,
        # sales-notes side
        institution: str | None = None,
        title: str | None = None,
        kind: str | None = None,
        analyst: str | None = None,
        doc_date: str | None = None,
        tickers: list[str] | None = None,
        source_url: str | None = None,
        date: str | None = None,
        # wiki side
        target_type: Literal["company", "sector"] | None = None,
        target: str | None = None,
        content_type: str | None = None,
        source: str | None = None,
        # shared
        content: str | None = None,
        file_path: str | None = None,
        confirm: bool = False,
        allow_similar: bool = False,
        *,
        config: RunnableConfig,
    ) -> str:
        """统一入库入口：按"谁在说"把研究资料分流进 sales notes（卖方观点）或 company-wiki（专家会、公司会及其他一手资料）。

        分流规则：正文主体是券商分析师/销售的判断 → sales notes（需给 institution、title，可选 kind/analyst/doc_date/tickers）；专家会、公司会、公告、自己整理的纪要 → wiki（需给 target_type、target、content_type、date）。
        拿不准是谁在说时不要猜：不传 speaker 调用会返回一个该问用户的问题。一份材料两种都有时按篇幅主体归类。
        工作区打开"自动入库"后：分类明确时直接写入并只回一条回执（库、编号、要点），不再逐步确认；疑似重复或分类拿不准时仍会问一句。开关关闭时保持两步确认（confirm=False 预览 → confirm=True 写入）。
        Args:
            speaker: 'sell_side'（卖方观点）或 'expert_or_company'（专家会/公司会/其他）；不确定就省略，工具会给出要问用户的问题。
            institution: 卖方机构名（speaker='sell_side' 必填）。
            title: 标题（speaker='sell_side' 必填）。
            kind: '研报'|'点评'|'卖方电话会'|'销售观点'（sell_side 可选）。
            analyst: 分析师姓名（可选）。
            doc_date: 报告/会议日期 YYYY-MM-DD（可选）。
            tickers: 相关标的（可选）。
            source_url: 原始链接（可选）。
            date: 收到日期（sell_side 可选）/ 文档日期（wiki 必填）。
            target_type: 'company' 或 'sector'（speaker='expert_or_company' 必填）。
            target: 公司代码/名称或行业 slug（wiki 必填）。
            content_type: wiki 文档类型（wiki 必填）。
            source: wiki 资料来源名（可选）。
            content: 正文文本（与 file_path 二选一）。
            file_path: 工作区内文件路径（与 content 二选一）。
            confirm: False 预览；True 写入（自动入库开启且分类明确时可一步写入）。
            allow_similar: 仅在工具上一条回复是 duplicate_suspected 且用户明确确认仍要入库时传 true（wiki 侧跳过相似度判重；完全相同内容仍拒绝）。其他情况保持 false。
        Returns: JSON 字符串：路由与预览/回执/要问用户的问题。
        """
        user_id, err = await _require_internal_ingest_caller(config)
        if err is not None:
            return err

        # Ask before anything else: the classification question needs no
        # workspace, and guessing an归属 is the one mistake this router exists
        # to prevent.
        undecided = json.dumps(
            {
                "ok": False,
                "status": "speaker_unknown",
                "error": "这份材料算卖方观点还是公司会/专家会？",
                "question": (
                    "请先问用户：这份材料按'谁在说'归类——正文主体是券商分析师或销售的判断"
                    "（研报、点评、卖方电话会、sales color）选 sell_side 并提供 institution；"
                    "专家会、公司会、公告或自整理纪要选 expert_or_company 并提供 target/target_type。"
                    "不要猜着入库，错误归属比不入库更难清理。"
                ),
            },
            ensure_ascii=False,
        )
        if speaker not in ("sell_side", "expert_or_company"):
            return undecided

        workspace_id, _thread_id, id_err = await _ingest_workspace_identity(
            config, allow_home_workspace
        )
        if id_err is not None:
            return id_err

        file_payload, payload_sha256, payload_err = await _load_ingest_payload(
            filesystem_backend, content, file_path
        )
        if payload_err is not None:
            return payload_err

        auto_ingest = await get_workspace_auto_ingest(workspace_id)
        # An auto write still previews first: a suspected duplicate must come
        # back as a question, never as a silent write or a bare refusal.
        auto_write = auto_ingest and not bool(confirm)
        effective_confirm = bool(confirm) or auto_write

        if speaker == "sell_side":
            if not (institution and str(institution).strip() and title and str(title).strip()):
                return json.dumps(
                    {
                        "ok": False,
                        "status": "bad_request",
                        "error": "speaker='sell_side' 需要 institution 与 title。",
                    },
                    ensure_ascii=False,
                )
            if effective_confirm:
                rate_err = await _check_ingest_rate_limit(
                    user_id, scope="sales_notes", limit=SALES_NOTES_RATE_LIMIT_PER_HOUR
                )
                if rate_err is not None:
                    return rate_err
            request_id = (
                str(uuid.uuid5(
                    uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8"),
                    f"sales-note:{user_id}:{workspace_id}:{institution}:{title}:{payload_sha256}",
                ))
                if effective_confirm
                else None
            )
            payload: dict[str, Any] = {
                "institution": institution,
                "title": title,
                "kind": kind,
                "analyst": analyst,
                "doc_date": doc_date,
                "tickers": tickers,
                "source_url": source_url,
                "date": date,
                "content": content if file_payload is None else None,
                "file": file_payload,
                "ingested_by": {"user_id": user_id},
                "ingested_via": {"workspace_id": workspace_id},
                "dry_run": not effective_confirm,
            }
            if request_id is not None:
                payload["request_id"] = request_id
            if auto_write:
                preview = dict(payload)
                preview["dry_run"] = True
                preview.pop("request_id", None)
                try:
                    _code, preview_resp = await call_bridge_sales_notes_ingest(preview)
                except Exception as exc:
                    logger.warning("Bridge sales-notes preview call failed: %s", type(exc).__name__)
                    return json.dumps(
                        {"ok": False, "status": "bridge_unreachable",
                         "error": f"Failed calling researchdata bridge: {type(exc).__name__}"},
                        ensure_ascii=False,
                    )
                if _suspected_duplicate(preview_resp):
                    return _duplicate_question("sales notes", preview_resp)
            try:
                _code, resp = await call_bridge_sales_notes_ingest(payload)
            except Exception as exc:
                logger.warning("Bridge sales-notes ingest call failed: %s", type(exc).__name__)
                return json.dumps(
                    {"ok": False, "status": "bridge_unreachable",
                     "error": f"Failed calling researchdata bridge: {type(exc).__name__}"},
                    ensure_ascii=False,
                )
            return _render_ingest_receipt("sales-notes", resp, auto_ingest, effective_confirm)

        # speaker == "expert_or_company" → company-wiki
        if not (target_type and target and content_type and date):
            return json.dumps(
                {
                    "ok": False,
                    "status": "bad_request",
                    "error": "speaker='expert_or_company' 需要 target_type、target、content_type、date。",
                },
                ensure_ascii=False,
            )
        if effective_confirm:
            rate_err = await _check_ingest_rate_limit(
                user_id, scope="wiki_ingest", limit=WIKI_INGEST_RATE_LIMIT_PER_HOUR
            )
            if rate_err is not None:
                return rate_err
        from src.dalton.tools.wiki_tools import call_bridge_wiki_ingest, derive_wiki_ingest_request_id

        wiki_request_id = (
            derive_wiki_ingest_request_id(
                user_id=user_id,
                workspace_id=workspace_id,
                thread_id=None,
                target_type=str(target_type),
                target=str(target),
                content_type=str(content_type),
                date=str(date),
                payload_sha256=payload_sha256,
            )
            if effective_confirm
            else None
        )
        wiki_payload = {
            "target_type": target_type,
            "target": target,
            "content_type": content_type,
            "date": date,
            "source": source,
            "source_url": source_url,
            "content": content if file_payload is None else None,
            "file": file_payload,
            "ingested_by": {"user_id": user_id},
            "ingested_via": {"workspace_id": workspace_id},
            "dry_run": not effective_confirm,
        }
        if wiki_request_id is not None:
            wiki_payload["request_id"] = wiki_request_id
        # r3 (#2): the confirmed "ingest anyway" answer rides the write (and
        # only the write) so a similar-but-not-identical revision can land;
        # the exact fingerprint dedup still applies bridge-side.
        # Only an explicit confirm counts: auto-ingest turns effective_confirm
        # on by itself, and that alone must never skip the similarity check.
        if allow_similar and confirm:
            wiki_payload["allow_similar"] = True
        if auto_write:
            preview = dict(wiki_payload)
            preview["dry_run"] = True
            preview.pop("request_id", None)
            preview.pop("allow_similar", None)
            try:
                _code, preview_resp = await call_bridge_wiki_ingest(preview)
            except Exception as exc:
                logger.warning("Bridge wiki preview call failed: %s", type(exc).__name__)
                return json.dumps(
                    {"ok": False, "status": "bridge_unreachable",
                     "error": f"Failed calling researchdata bridge: {type(exc).__name__}"},
                    ensure_ascii=False,
                )
            if _suspected_duplicate(preview_resp):
                return _duplicate_question("company-wiki", preview_resp)
        try:
            _code, resp = await call_bridge_wiki_ingest(wiki_payload)
        except Exception as exc:
            logger.warning("Bridge wiki ingest call failed: %s", type(exc).__name__)
            return json.dumps(
                {"ok": False, "status": "bridge_unreachable",
                 "error": f"Failed calling researchdata bridge: {type(exc).__name__}"},
                ensure_ascii=False,
            )
        # A manual preview that only *resembles* an existing record asks the
        # same question as auto-ingest, so the confirmed retry knows to carry
        # allow_similar; an exact duplicate stays the bridge's plain refusal.
        if (
            not effective_confirm
            and str(resp.get("duplicate_reason") or "").strip() == "similar_content"
        ):
            return _duplicate_question("company-wiki", resp)
        return _render_ingest_receipt("company-wiki", resp, auto_ingest, effective_confirm)

    return _ingest_research


def _render_ingest_receipt(
    library: str, resp: dict[str, Any], auto_ingest: bool, effective_confirm: bool
) -> str:
    """One-line receipt for an auto-ingest write; the plain bridge answer otherwise.

    The receipt keeps to three points — which library, the record id, and the
    title/institution it filed under — so the agent relays a single line
    instead of the full preview payload.
    """
    if not (auto_ingest and effective_confirm and resp.get("ok") is True):
        return json.dumps(resp, ensure_ascii=False)
    identifier = (
        resp.get("document_id")
        or resp.get("doc_key")
        or resp.get("doc_id")
        or resp.get("filepath")
        or ""
    )
    name = str(resp.get("title") or resp.get("institution") or resp.get("target") or "")
    receipt = {
        "ok": True,
        "auto_ingest": True,
        "receipt": (
            f"已入库 {library}；编号 {identifier or '（见 cli_result/audit_path）'}；{name}"
        ),
        "library": library,
        "document_id": identifier or None,
        "duplicate": bool(resp.get("duplicate")),
    }
    if resp.get("status") == "duplicate":
        receipt["receipt"] = f"检测到重复，未写第二份（{library} 已有该内容：{identifier or '同内容记录'}）"
    return json.dumps(receipt, ensure_ascii=False)


__all__ = [
    "AUTO_INGEST_CONFIG_KEY",
    "build_ingest_research_tool",
    "build_ingest_sales_note_tool",
    "call_bridge_sales_notes_ingest",
    "get_workspace_auto_ingest",
]
