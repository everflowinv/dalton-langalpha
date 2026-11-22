"""Direct LangChain tools for reading and writing Feishu docs (`DALTON-HOOK(feishu-docs)`).

Every call resolves the caller's tier via `get_user_tier` (admin / internal /
external, roster-fixed). Admins and internal members read and write; external
members read only, and only docs whose wiki space is on the
`dalton_feishu_read_scopes` whitelist an admin maintains in Settings.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool, tool

from src.dalton.access_tiers import get_user_tier
from src.dalton.database.feishu import get_read_scope_space_ids, list_read_scopes
from src.dalton.feishu import (
    FeishuBridgeError,
    describe_feishu_error,
    is_uncertain_bridge_error,
    post_bridge_feishu,
)
from src.dalton.settings import get_dalton_invite_email_domain
from src.dalton.tools.conversation_tools import _extract_caller_identity

logger = logging.getLogger(__name__)

EXTERNAL_READ_ONLY_MESSAGE = "外部成员只能读取飞书文档（且仅限管理员开放的知识空间），无法执行写操作。"
EXTERNAL_SPACE_DENIED_MESSAGE = "该文档所属的知识空间未对外的成员开放读取，请联系管理员在设置中开放。"
EXTERNAL_NOT_IN_WIKI_MESSAGE = "外部成员只能读取知识空间（wiki）内的文档；该文档不属于任何知识空间。"
UNAUTHENTICATED_MESSAGE = "feishu 工具需要登录用户的会话（RunnableConfig 缺少 user_id）。"

MAX_READ_CHARS = 50000
DEFAULT_READ_CHARS = 20000

# C2: markdown image references in document order — each becomes a docx image
# block whose bytes the caller must supply for local paths (the bridge reads
# http(s) links itself).
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_FEISHU_IMAGE_MAX_BYTES = 10 * 1024 * 1024


def _dump(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _error_payload(message: str, *, error: str, **extra: Any) -> str:
    return _dump({"ok": False, "error": error, "message": message, **extra})


async def _require_authenticated_user(config: RunnableConfig | None) -> tuple[str | None, str | None]:
    user_id, workspace_id, thread_id, _run_id = _extract_caller_identity(config)
    if not user_id:
        logger.warning(
            "[dalton.feishu.audit] rejected unauthenticated feishu call (workspace_id=%s thread_id=%s)",
            workspace_id or "-",
            thread_id or "-",
        )
        return None, _error_payload(UNAUTHENTICATED_MESSAGE, error="unauthenticated")
    return user_id, None


async def _bridge(route: str, body: dict[str, Any]) -> dict[str, Any] | str:
    """Call a feishu bridge route; on failure return the error payload string.

    `content_insert_failed` keeps the bridge's structured fields (url,
    document_id, detail) and distinguishes `doc/create` (a new empty doc exists)
    from `doc/append`, whose wording follows the bridge's `outcome`: a rejected
    append left the original doc unchanged, while an unknown one (network error,
    unparseable response) may already be written and must be checked in the doc
    before any retry; every other error maps to a generic payload as before,
    keeping the bridge's own `message` when it sent one.
    """
    try:
        return await post_bridge_feishu(route, body)
    except FeishuBridgeError as exc:
        if exc.error_code == "content_insert_failed" and (
            exc.payload.get("url") or (route == "doc/create" and exc.payload.get("document_id"))
        ):
            url = str(exc.payload.get("url") or "")
            doc_id = str(exc.payload.get("document_id") or "")
            detail = str(exc.payload.get("detail") or "").strip()
            if route == "doc/append":
                lead = (
                    "追加请求结果未知，可能已经写入；请先打开文档检查，不要直接重试"
                    if str(exc.payload.get("outcome") or "") == "unknown"
                    else "追加内容没有写入，原文档未改变"
                )
                msg = (
                    f"{lead}（{detail}），文档地址：{url}"
                    if detail
                    else f"{lead}，文档地址：{url}"
                )
            else:
                msg = (
                    f"文档已创建但正文没写进去，请到 {url} 手动处理。"
                    if url
                    else f"文档已创建但正文没写进去（document_id={doc_id}）。"
                )
            err_payload: dict[str, Any] = {
                "ok": False,
                "error": "content_insert_failed",
                "message": msg,
                "document_id": doc_id,
                "url": url,
            }
            outcome = str(exc.payload.get("outcome") or "").strip()
            if outcome:
                err_payload["outcome"] = outcome
            if detail:
                err_payload["detail"] = detail
            return _dump(err_payload)
        if exc.error_code == "image_bind_failed":
            return _dump(
                {
                    "ok": False,
                    "error": "image_bind_failed",
                    "message": str(exc.payload.get("message") or "图片没有绑定成功，请查看文档或重试。"),
                    "document_id": str(exc.payload.get("document_id") or ""),
                    "images": exc.payload.get("images") or {},
                    **({"url": exc.payload["url"]} if exc.payload.get("url") else {}),
                    **({"node_token": exc.payload["node_token"]} if exc.payload.get("node_token") else {}),
                }
            )
        bridge_message = str(exc.payload.get("message") or "").strip()
        described = describe_feishu_error(exc.error_code)
        extra_fields: dict[str, Any] = {}
        if exc.payload.get("detail"):
            extra_fields["detail"] = str(exc.payload.get("detail") or "")
        outcome = str(exc.payload.get("outcome") or "").strip()
        if not outcome and is_uncertain_bridge_error(exc.error_code):
            outcome = "unknown"
        if outcome:
            extra_fields["outcome"] = outcome
        return _error_payload(
            bridge_message or described,
            error=exc.error_code,
            **extra_fields,
        )


async def _collect_markdown_image_files(
    markdown: str, filesystem_backend: Any
) -> tuple[dict[str, Any], str | None]:
    """Local markdown images as ``{ref: {filename, content_b64}}`` for the bridge (C2).

    http(s) references are skipped — the bridge fetches those itself under its
    SSRF check — while a workspace path can only be read here, where the
    sandbox filesystem lives. Any unreadable or oversized local image fails
    the whole call up front: a doc that silently drops its images is the bug
    this flow exists to close.
    """
    files: dict[str, Any] = {}
    for match in _MARKDOWN_IMAGE_RE.finditer(markdown or ""):
        ref = match.group(1).strip()
        if not ref or ref.startswith(("http://", "https://", "data:")):
            continue
        if ref in files:
            continue
        if filesystem_backend is None:
            return {}, f"本地图片 {ref} 无法读取：当前没有可用的文件系统后端。"
        from src.dalton.tools.wiki_tools import _read_workspace_file_safely

        fname, fbytes, ferr = await _read_workspace_file_safely(
            filesystem_backend,
            ref,
            allowed_extensions=frozenset({".png", ".jpg", ".jpeg", ".webp", ".gif"}),
        )
        if ferr is not None or fname is None or fbytes is None:
            return {}, f"本地图片 {ref} 读取失败：{ferr or '未知错误'}"
        if len(fbytes) > _FEISHU_IMAGE_MAX_BYTES:
            return {}, f"本地图片 {ref} 超过 10 MB 上限（{len(fbytes)} bytes）。"
        if Path(fname).suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            return {}, f"本地图片 {ref} 不是可上传的图片格式。"
        files[ref] = {
            "filename": fname,
            "content_b64": base64.b64encode(fbytes).decode("ascii"),
        }
    return files, None


def _bridge_failed(res: Any) -> bool:
    return isinstance(res, str) or not isinstance(res, dict) or res.get("ok") is not True


def _image_bind_failed_payload(res: dict[str, Any]) -> str:
    """Keep the created doc's id/url and every image's failure reason (C2).

    A 200 body with ``ok:false`` / ``image_bind_failed`` means the doc (or the
    append) landed and only the images failed — the model must know where the
    doc is and which images to fix, so nothing is collapsed into a generic
    error here.
    """
    images = res.get("images") if isinstance(res.get("images"), dict) else {}
    failed = [
        {
            "ref": f.get("ref"),
            "block_id": f.get("block_id"),
            "reason": f.get("reason"),
        }
        for f in (images.get("failed") or [])
        if isinstance(f, dict)
    ]
    payload: dict[str, Any] = {
        "ok": False,
        "error": "image_bind_failed",
        "message": str(res.get("message") or "图片没有绑定成功，请查看文档或重试。"),
        "document_id": str(res.get("document_id") or ""),
        "images": {
            "total": images.get("total"),
            "bound": images.get("bound"),
            "failed": failed,
        },
    }
    if res.get("url"):
        payload["url"] = str(res["url"])
    if res.get("node_token"):
        payload["node_token"] = str(res["node_token"])
    return _dump(payload)


async def _external_read_allowed(token: str) -> tuple[str | None, dict[str, Any]]:
    """External-member gate: return ``(None, node)`` when allowed, ``(denial, None)`` otherwise.

    The same resolved node powers the subsequent read (`doc/read` gets its
    ``document_id``), so the permission check and the read can never disagree.
    Shortcuts cross spaces, so both the node's own space and its origin space
    must be whitelisted.
    """
    node_res = await _bridge("wiki/node", {"token": token})
    if isinstance(node_res, str):
        return node_res, {}
    if not isinstance(node_res, dict) or node_res.get("ok") is not True:
        return _error_payload(describe_feishu_error("bridge_error"), error="bridge_error"), {}
    node = node_res.get("node")
    if not isinstance(node, dict) or not str(node.get("space_id") or "").strip():
        return _error_payload(EXTERNAL_NOT_IN_WIKI_MESSAGE, error="external_doc_not_in_wiki"), {}
    try:
        allowed_spaces = await get_read_scope_space_ids()
    except Exception as exc:
        logger.warning("feishu read-scope lookup failed (treated as empty): %s", type(exc).__name__)
        allowed_spaces = set()
    node_space = str(node["space_id"]).strip()
    if node_space not in allowed_spaces:
        return _error_payload(EXTERNAL_SPACE_DENIED_MESSAGE, error="external_space_not_allowed"), {}
    if str(node.get("node_type") or "") == "shortcut":
        origin_space = str(node.get("origin_space_id") or "").strip()
        if origin_space not in allowed_spaces:
            return _error_payload(EXTERNAL_SPACE_DENIED_MESSAGE, error="external_space_not_allowed"), {}
    return None, node


async def _caller_email(user_id: str) -> str:
    """Return the caller's lowercase email ('' when the user row or email is missing)."""
    try:
        from src.server.database.user import get_user

        user = await get_user(user_id)
    except Exception as exc:
        logger.warning("feishu caller email lookup failed for user_id=%s: %s", user_id, type(exc).__name__)
        return ""
    return str((user or {}).get("email") or "").strip().lower()


def _grant_email_for(email: str) -> str:
    """Return `email` unless it is an invite-signup synthetic address (those cannot be granted)."""
    domain = get_dalton_invite_email_domain().lower().lstrip("@")
    if not email or not domain or email.endswith(f"@{domain}"):
        return ""
    return email


async def _caller_feishu_open_id(user_id: str) -> str:
    """Return the caller's bound Feishu open_id ('' when missing or lookup fails)."""
    try:
        from src.dalton.database.channel_gateway import get_feishu_open_id_for_user

        return str(await get_feishu_open_id_for_user(user_id) or "").strip()
    except Exception as exc:
        logger.warning(
            "feishu caller open_id lookup failed for user_id=%s: %s",
            user_id,
            type(exc).__name__,
        )
        return ""


def _normalize_bridge_write_failure(res: dict[str, Any] | str) -> dict[str, Any]:
    """Convert a failed `_bridge` result for `doc/create` or `doc/append` into a dict."""
    if isinstance(res, str):
        return json.loads(res)
    if not isinstance(res, dict):
        return {"ok": False, "error": "bridge_error", "message": describe_feishu_error("bridge_error")}
    if str(res.get("error") or "") == "image_bind_failed":
        return json.loads(_image_bind_failed_payload(res))
    err_code = str(res.get("error") or "bridge_error")
    payload: dict[str, Any] = {
        "ok": False,
        "error": err_code,
        "message": str(res.get("message") or "").strip() or describe_feishu_error(err_code),
    }
    for key in ("document_id", "url", "detail", "outcome"):
        if res.get(key) is not None:
            payload[key] = res[key]
    if not str(payload.get("outcome") or "").strip() and is_uncertain_bridge_error(err_code):
        payload["outcome"] = "unknown"
    return payload


async def create_feishu_doc_on_server(
    *,
    user_id: str,
    title: str,
    markdown: str,
    space_id: Optional[str] = None,
    parent_node_token: Optional[str] = None,
    folder_token: Optional[str] = None,
    filesystem_backend: Any = None,
    tier: Optional[str] = None,
) -> dict[str, Any]:
    """Create a Feishu doc via the host bridge, returning the structured response dict."""
    image_files, image_err = await _collect_markdown_image_files(markdown, filesystem_backend)
    if image_err is not None:
        return {"ok": False, "error": "image_read_failed", "message": image_err}
    body: dict[str, Any] = {"title": title, "markdown": markdown}
    if image_files:
        body["image_files"] = image_files
    if space_id:
        body["space_id"] = space_id
    if parent_node_token:
        body["parent_node_token"] = parent_node_token
    if folder_token:
        body["folder_token"] = folder_token
    grant_email = _grant_email_for(await _caller_email(user_id))
    if grant_email:
        body["grant_email"] = grant_email
    grant_open_id = await _caller_feishu_open_id(user_id)
    if grant_open_id:
        body["grant_open_id"] = grant_open_id
    res = await _bridge("doc/create", body)
    if _bridge_failed(res):
        return _normalize_bridge_write_failure(res)
    assert isinstance(res, dict)
    payload: dict[str, Any] = {"ok": True}
    if tier is not None:
        payload["tier"] = tier
    payload.update({k: v for k, v in res.items() if k != "ok"})
    if payload.get("warning"):
        payload["message"] = "文档已创建，但部分自动分享失败（详见 warning），请手动分享。"
    return payload


async def append_feishu_doc_on_server(
    *,
    token: str,
    markdown: str,
    filesystem_backend: Any = None,
    tier: Optional[str] = None,
) -> dict[str, Any]:
    """Append markdown to an existing Feishu doc via the host bridge, returning a dict."""
    image_files, image_err = await _collect_markdown_image_files(markdown, filesystem_backend)
    if image_err is not None:
        return {"ok": False, "error": "image_read_failed", "message": image_err}
    append_body: dict[str, Any] = {"token": token, "markdown": markdown}
    if image_files:
        append_body["image_files"] = image_files
    res = await _bridge("doc/append", append_body)
    if _bridge_failed(res):
        return _normalize_bridge_write_failure(res)
    assert isinstance(res, dict)
    payload: dict[str, Any] = {"ok": True}
    if tier is not None:
        payload["tier"] = tier
    payload.update({k: v for k, v in res.items() if k != "ok"})
    return payload


async def read_feishu_doc_on_server(
    *,
    token: str,
    max_chars: int = DEFAULT_READ_CHARS,
    offset: int = 0,
    resolved_document_id: Optional[str] = None,
    tier: Optional[str] = None,
    extra_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Read a Feishu doc (optionally paginated by `offset`) via the host bridge, returning a structured dict."""
    budget = max(1000, min(int(max_chars or DEFAULT_READ_CHARS), MAX_READ_CHARS))
    start_offset = max(0, int(offset or 0))
    body: dict[str, Any] = {"token": token, "max_chars": budget}
    if start_offset > 0:
        body["offset"] = start_offset
    if resolved_document_id:
        body["document_id"] = resolved_document_id
    res = await _bridge("doc/read", body)
    if _bridge_failed(res):
        if isinstance(res, str):
            return json.loads(res)
        if isinstance(res, dict):
            err_code = str(res.get("error") or "bridge_error")
            return {"ok": False, "error": err_code, "message": describe_feishu_error(err_code)}
        return {"ok": False, "error": "bridge_error", "message": describe_feishu_error("bridge_error")}
    assert isinstance(res, dict)
    payload: dict[str, Any] = {"ok": True}
    if tier is not None:
        payload["tier"] = tier
    payload.update({k: v for k, v in res.items() if k != "ok"})
    if extra_meta:
        for key, value in extra_meta.items():
            if key not in payload:
                payload[key] = value
    if payload.get("truncated"):
        payload["note"] = f"内容超过 {budget} 字被截断（全文 {payload.get('total_chars')} 字），需要更多时提高 max_chars 重读。"
    return payload


def create_feishu_tools(filesystem_backend: Any = None) -> list[BaseTool]:
    """Build `[feishu_read, feishu_list_wiki, feishu_create_doc, feishu_append,
    feishu_grant_access]` for both roles.

    ``filesystem_backend`` (optional) reads a workspace's local image files so
    a created doc's markdown images carry real bytes (C2); without it, docs
    with local-path images are refused rather than silently emptied.
    """

    @tool("feishu_read")
    async def _feishu_read(
        token: str,
        max_chars: int = DEFAULT_READ_CHARS,
        *,
        config: RunnableConfig,
    ) -> str:
        """Read one Feishu doc by docx link, wiki link, or raw token; use it whenever the user references a Feishu/Lark document.
        External team members can read only docs inside whitelisted wiki spaces; a doc outside any wiki space is refused for them.

        Args:
            token: Feishu document URL (`.../docx/<id>` or `.../wiki/<id>`) or the raw document/wiki token.
            max_chars: Plain-text budget, 1000-50000 (default 20000); longer docs are cut off and flagged `truncated`.

        Returns:
            JSON with `title`, plain-text `content`, `truncated`/`total_chars` (hard cap 50,000 chars, no pagination). Proceed with full-document actions only when `truncated=false`; if truncated, retry once with `max_chars=50000`, and if still truncated ask the user to provide the full file.
        """
        user_id, err = await _require_authenticated_user(config)
        if err is not None:
            return err
        tier = await get_user_tier(user_id)
        resolved_id: Optional[str] = None
        extra_meta: dict[str, Any] = {}
        if tier == "external":
            denial, check_node = await _external_read_allowed(token)
            if denial is not None:
                return denial
            # Reuse the permission check's resolution: the read must act on
            # the same document the whitelist verdict was made for.
            resolved_id = str(check_node.get("obj_token") or "").strip() or None
            if check_node:
                extra_meta = {
                    "space_id": str(check_node.get("space_id") or ""),
                    "node_token": str(check_node.get("node_token") or ""),
                }
        return _dump(
            await read_feishu_doc_on_server(
                token=token,
                max_chars=max_chars,
                resolved_document_id=resolved_id,
                tier=tier,
                extra_meta=extra_meta or None,
            )
        )

    @tool("feishu_list_wiki")
    async def _feishu_list_wiki(
        space_id: Optional[str] = None,
        parent_node_token: Optional[str] = None,
        *,
        config: RunnableConfig,
    ) -> str:
        """List Feishu wiki spaces, or the nodes inside one space, to find where a doc lives before reading or creating it.
        Without `space_id` it returns spaces; with it, the space's nodes (pass `parent_node_token` to descend a node).

        Args:
            space_id: Wiki space id to list nodes for; omit to list spaces.
            parent_node_token: Node token whose children to list (defaults to the space root).

        Returns:
            JSON with `spaces` (id, name) or `nodes` (title, node_token, obj_token, has_child); empty lists mean nothing visible.
        """
        user_id, err = await _require_authenticated_user(config)
        if err is not None:
            return err
        tier = await get_user_tier(user_id)
        if tier == "external":
            try:
                scopes = await list_read_scopes()
            except Exception as exc:
                logger.warning("feishu read-scope list failed (treated as empty): %s", type(exc).__name__)
                scopes = []
            allowed_ids = {str(s["space_id"]) for s in scopes}
            if not space_id:
                return _dump(
                    {
                        "ok": True,
                        "tier": tier,
                        "spaces": [
                            {"space_id": s["space_id"], "name": s["name"] or s["space_id"]}
                            for s in scopes
                        ],
                        "note": "外部成员仅能看到对其开放的知识空间。",
                    }
                )
            if space_id not in allowed_ids:
                return _error_payload(EXTERNAL_SPACE_DENIED_MESSAGE, error="external_space_not_allowed")
        res = await _bridge(
            "wiki/nodes",
            {"space_id": space_id or "", "parent_node_token": parent_node_token or ""},
        )
        if _bridge_failed(res):
            if isinstance(res, dict):
                return _error_payload(describe_feishu_error(str(res.get("error") or "bridge_error")), error=str(res.get("error") or "bridge_error"))
            return res
        payload = {"ok": True, "tier": tier, **{k: v for k, v in res.items() if k != "ok"}}
        if tier == "external":
            # A shortcut node points into another space; expose it only when
            # that origin space is whitelisted too (same rule as reads).
            payload["nodes"] = [
                n
                for n in (payload.get("nodes") or [])
                if str(n.get("node_type") or "") != "shortcut"
                or str(n.get("origin_space_id") or "").strip() in allowed_ids
            ]
            payload["note"] = "外部成员仅能看到对其开放的知识空间（含指向已开放空间的快捷方式）。"
        return _dump(payload)

    @tool("feishu_create_doc")
    async def _feishu_create_doc(
        title: str,
        markdown: str,
        space_id: Optional[str] = None,
        parent_node_token: Optional[str] = None,
        folder_token: Optional[str] = None,
        *,
        config: RunnableConfig,
    ) -> str:
        """Create a new Feishu docx from markdown, only when the user explicitly asks to put this content in Feishu; admin and internal members only.
        Deliverables stay in the workspace by default — never write to Feishu unasked.
        Creating inside a wiki space requires `space_id` (find one with `feishu_list_wiki`); without any target the doc lands in the app's drive root.

        Args:
            title: New document title.
            markdown: Full document body in markdown (headings, lists, tables, code fences).
            space_id: Wiki space id to create the doc in (as a wiki node).
            parent_node_token: Parent wiki node to nest under (defaults to space root).
            folder_token: Drive folder token, used only when not creating in a wiki space.

        Returns:
            JSON with the new `document_id` (plus `space_id`/`node_token` when in a wiki) and a `warning` when sharing with the team group or the caller failed (the doc still exists; relay the warning).
        """
        user_id, err = await _require_authenticated_user(config)
        if err is not None:
            return err
        tier = await get_user_tier(user_id)
        if tier == "external":
            return _error_payload(EXTERNAL_READ_ONLY_MESSAGE, error="external_write_forbidden")
        payload = await create_feishu_doc_on_server(
            user_id=user_id,
            title=title,
            markdown=markdown,
            space_id=space_id,
            parent_node_token=parent_node_token,
            folder_token=folder_token,
            filesystem_backend=filesystem_backend,
            tier=tier,
        )
        return _dump(payload)

    @tool("feishu_append")
    async def _feishu_append(
        token: str,
        markdown: str,
        *,
        config: RunnableConfig,
    ) -> str:
        """Append markdown to the end of an existing Feishu doc (docx or wiki link, or raw token), only when the user explicitly asks; admin and internal members only.
        It only appends at the end — it never edits, rewrites, or deletes existing content.

        Args:
            token: Feishu document URL (`.../docx/<id>` or `.../wiki/<id>`) or raw token.
            markdown: Content to append, in markdown.

        Returns:
            JSON with `document_id` and `appended_blocks`; confirm to the user what was appended and provide the document link if available (omit internal machine fields unless the user asks for diagnostics), and read the doc back with `feishu_read` when the user needs to verify.
        """
        user_id, err = await _require_authenticated_user(config)
        if err is not None:
            return err
        tier = await get_user_tier(user_id)
        if tier == "external":
            return _error_payload(EXTERNAL_READ_ONLY_MESSAGE, error="external_write_forbidden")
        payload = await append_feishu_doc_on_server(
            token=token,
            markdown=markdown,
            filesystem_backend=filesystem_backend,
            tier=tier,
        )
        return _dump(payload)

    @tool("feishu_grant_access")
    async def _feishu_grant_access(
        token: str,
        *,
        config: RunnableConfig,
    ) -> str:
        """Re-grant the caller full access to a doc Dalton created, only when the user asks to fix sharing on a doc they cannot edit (admin and internal members only).
        Use it when a doc was created but the automatic share failed (its warning said so), or the user reports they cannot edit a doc.
        It grants only the caller themself — using their bound Feishu account or Dalton account email, and only docs the bridge itself created can be re-granted.

        Args:
            token: Feishu document URL (`.../docx/<id>` or `.../wiki/<id>`) or the raw document/wiki token of the doc that was created through Dalton.

        Returns:
            JSON with `granted` (the email or `bound_openid`), the doc `url`, and the `grant_member` spelling used; a failure carries Feishu's own code/msg.
        """
        user_id, err = await _require_authenticated_user(config)
        if err is not None:
            return err
        tier = await get_user_tier(user_id)
        if tier == "external":
            return _error_payload(EXTERNAL_READ_ONLY_MESSAGE, error="external_write_forbidden")
        # Self-service only: the grant targets the caller's own bound Feishu
        # open_id and/or account email (the same sources doc/create's automatic
        # share uses), never an arbitrary identity the model was handed.
        grant_email = _grant_email_for(await _caller_email(user_id))
        grant_open_id = await _caller_feishu_open_id(user_id)
        if not grant_email and not grant_open_id:
            return _error_payload(
                "当前用户既没有绑定的飞书 open_id，也没有可授权的邮箱（账户记录缺少邮箱，或为邀请注册的合成地址）。",
                error="no_grant_email",
            )
        grant_body: dict[str, Any] = {"token": token}
        if grant_email:
            grant_body["email"] = grant_email
        if grant_open_id:
            grant_body["open_id"] = grant_open_id
        res = await _bridge("doc/grant", grant_body)
        if _bridge_failed(res):
            if isinstance(res, dict):
                message = str(res.get("message") or describe_feishu_error(str(res.get("error") or "bridge_error")))
                return _error_payload(message, error=str(res.get("error") or "bridge_error"))
            return res
        return _dump({"ok": True, "tier": tier, **{k: v for k, v in res.items() if k != "ok"}})

    return [_feishu_read, _feishu_list_wiki, _feishu_create_doc, _feishu_append, _feishu_grant_access]


_DEFAULT_TOOLS = create_feishu_tools()
feishu_read = _DEFAULT_TOOLS[0]
feishu_list_wiki = _DEFAULT_TOOLS[1]
feishu_create_doc = _DEFAULT_TOOLS[2]
feishu_append = _DEFAULT_TOOLS[3]
feishu_grant_access = _DEFAULT_TOOLS[4]
