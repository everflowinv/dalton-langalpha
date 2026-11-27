"""Public OpenGraph metadata fragment for shared thread and file links.

Read-only, unauthenticated endpoint consumed by Caddy's ``templates``
``httpInclude`` directive on ``/s/<token>`` and ``/a/<token>`` routes so link
unfurlers (Feishu, Slack, Discord, etc.) display the shared item's title
instead of the static site brand name.

Design invariants:
- Never reads request credentials or performs authentication.
- Never mutates state (never triggers legacy snapshots or mints grants).
- Invalid, revoked, organization-only, non-public, or erroring tokens all
  return HTTP 200 with a uniform brand-only ``<meta>`` fragment.
- All interpolated values are HTML-escaped with ``quote=True``.
- Brand name always resolves via ``src.dalton.settings.get_brand_name()``.
"""

from __future__ import annotations

import html
import inspect
import json
import logging
import os
import re
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from src.dalton.settings import get_brand_name
from src.server.database import artifacts as artifacts_db
from src.server.database.conversation import get_thread_by_share_token
from src.server.database.share_links import KIND_FILE
from src.server.services import share_redaction
from src.server.services.account import serves_public_links
from src.server.services.share_audience import ORGANIZATION, thread_audience

if TYPE_CHECKING:
    from src.server.app.share_access import LinkAccess

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["Public Sharing"])

# Both `/s/<token>` (shared conversations) and `/a/<token>` (published workspace
# files) live in a single share-code namespace (`SharePage` dispatches both).
_SHARE_PATH_RE = re.compile(r"^/(?:s|a)/([A-Za-z0-9_-]{1,128})$")

_MAX_OG_TITLE_CHARS = 160
_CACHE_CONTROL = "public, max-age=60"


def extract_share_token(raw_path: str | None) -> str | None:
    """Extract the share token from a public share path (``/s/<t>`` or ``/a/<t>``)."""
    if not raw_path or not isinstance(raw_path, str):
        return None
    normalized = urlsplit(raw_path.strip()).path
    if normalized.endswith("/") and len(normalized) > 1:
        normalized = normalized[:-1]
    match = _SHARE_PATH_RE.fullmatch(normalized)
    if not match:
        return None
    return match.group(1)


def _clean_title(value: Any) -> str | None:
    """Normalize a candidate title to a single bounded line, or None if empty."""
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.replace("\x00", "").split())
    if not collapsed:
        return None
    if len(collapsed) > _MAX_OG_TITLE_CHARS:
        return collapsed[: _MAX_OG_TITLE_CHARS - 1].rstrip() + "…"
    return collapsed


def _filename_stem(path: str | None) -> str | None:
    """Derive a display title from a file path by stripping its directory and extension."""
    if not path or not isinstance(path, str):
        return None
    name = PurePosixPath(path.replace("\\", "/")).name
    if not name or name in (".", ".."):
        return None
    stem, _ext = os.path.splitext(name)
    return _clean_title(stem or name)


def _call_safe_share_title_resolver(
    resolver: Any, thread: Mapping[str, Any]
) -> str | None:
    """Invoke ``share_redaction.resolve_safe_share_title`` across candidate signatures."""
    try:
        params = [
            p
            for p in inspect.signature(resolver).parameters.values()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            )
        ]
    except (TypeError, ValueError):
        params = []

    if params and params[0].name in {"title", "raw_title", "text"}:
        result = resolver(thread.get("title"))
    else:
        result = resolver(thread)
    return _clean_title(result)


def _resolve_thread_title(thread: Mapping[str, Any]) -> str | None:
    """Resolve the public OG title for a shared thread.

    Prefers ``share_redaction.resolve_safe_share_title`` when available on the
    active branch (and respects its None/empty suppression). Falls back to the
    existing public metadata endpoint's visible title (``title`` / ``workspace_name``)
    when that helper is absent.
    """
    resolver = getattr(share_redaction, "resolve_safe_share_title", None)
    if callable(resolver):
        return _call_safe_share_title_resolver(resolver, thread)
    return _clean_title(thread.get("title")) or _clean_title(
        thread.get("workspace_name")
    )


async def _resolve_file_title(access: LinkAccess) -> str | None:
    """Resolve the public OG title for an admitted public file link (read-only)."""
    link = access.link
    if link.kind != KIND_FILE:
        return None
    artifact = link.artifact
    if artifact is None or not artifact.shared:
        return None

    if artifact.shared_seq > 0:
        version = await artifacts_db.get_version(
            artifact.artifact_id, artifact.shared_seq
        )
        if version is None:
            return None
        return _clean_title(version.title) or _filename_stem(
            version.entry_path or artifact.path
        )

    # Legacy link shared before artifact versions: strictly read-only here
    # (never trigger `legacy_snapshot`). Refused legacy links return 404 on
    # the real share route, so fall back to brand name.
    if artifact.legacy_refused:
        return None
    return _filename_stem(artifact.path or link.path)


async def _resolve_link(token: str, *, user_id: str | None = None) -> LinkAccess | None:
    """Lazy wrapper around ``share_access.resolve_link`` to avoid circular app import."""
    from src.server.app.share_access import resolve_link

    return await resolve_link(token, user_id=user_id)


def _get_permissions(thread: Mapping[str, Any]) -> dict[str, Any]:
    """Extract share permissions dict from a thread row."""
    perms = thread.get("share_permissions") or {}
    if isinstance(perms, str):
        perms = json.loads(perms)
    return perms if isinstance(perms, dict) else {}


async def _resolve_public_thread_title(share_token: str) -> str | None:
    """Resolve the public OG title for a shared thread token, or None if unavailable."""
    thread = await get_thread_by_share_token(share_token)
    if not thread:
        return None
    if thread_audience(_get_permissions(thread)) == ORGANIZATION:
        return None
    if not await serves_public_links(thread.get("owner_id")):
        return None
    return _resolve_thread_title(thread)


async def resolve_og_title_for_path(raw_path: str | None) -> str | None:
    """Resolve the publicly visible OG title for a share page path, or None on fallback."""
    token = extract_share_token(raw_path)
    if token is None:
        return None
    try:
        access = await _resolve_link(token, user_id=None)
    except HTTPException:
        # Private, revoked, or members-only file/app link: never fall through to thread lookup.
        return None
    if access is not None:
        return await _resolve_file_title(access)
    return await _resolve_public_thread_title(token)


def render_og_meta_fragment(*, brand: str, title: str | None = None) -> str:
    """Render an HTML-escaped ``<meta>`` fragment for OpenGraph unfurlers."""
    safe_brand = html.escape(_clean_title(brand) or "Dalton", quote=True)
    resolved_title = _clean_title(title)
    safe_title = html.escape(resolved_title, quote=True) if resolved_title else safe_brand
    safe_description = html.escape(
        f"来自 {_clean_title(brand) or 'Dalton'} 的分享", quote=True
    )
    return "\n".join(
        (
            f'<meta property="og:site_name" content="{safe_brand}" />',
            f'<meta property="og:title" content="{safe_title}" />',
            f'<meta property="og:description" content="{safe_description}" />',
        )
    )


@router.get("/og-meta")
async def get_public_og_meta(
    path: str | None = Query(default=None),
) -> Response:
    """Return a short OpenGraph ``<meta>`` HTML fragment for a public share path.

    Always responds with HTTP 200 and never inspects request credentials. Any
    invalid, revoked, members-only, or failing lookup falls back to a generic
    brand-only fragment.
    """
    brand = get_brand_name()
    title: str | None = None
    try:
        title = await resolve_og_title_for_path(path)
    except Exception:
        logger.warning(
            "Failed to resolve public OG metadata; falling back to brand fragment",
            exc_info=True,
        )
        title = None

    body = render_og_meta_fragment(brand=brand, title=title)
    return Response(
        content=body,
        status_code=200,
        media_type="text/html; charset=utf-8",
        headers={
            "Cache-Control": _CACHE_CONTROL,
            "X-Robots-Tag": "noindex",
        },
    )
