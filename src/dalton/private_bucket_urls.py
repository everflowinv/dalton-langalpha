"""Private-bucket browser URL containment & share-token log redaction helpers (Batch 1 Step 4).

When ``workspace_files.public_bucket_urls`` is ``False`` (the default in both
Everflow and Standalone Core), browsers never receive direct object-storage URLs:

- Workspace file downloads (``download_link.py``) skip presigned bucket URLs so
  ``/files/download-url`` falls back to the 60-second HMAC ``dl_token`` (for
  ``> 8 MiB`` files) or the authenticated ``/files/download`` stream.
- Sandbox image capture (``ptc_agent.agent.middleware.image_capture`` and
  ``server.services.persistence.image_capture``) still uploads private snapshots
  under ``private-media/response-images/...`` (preserving ``/tmp`` images and
  pre-overwrite snapshots) and records an internal ``private-media/...``
  reference (never a direct bucket URL), which AU1 controlled media routes
  rewrite to ``/api/v1/threads/{id}/media/{token}`` or
  ``/api/v1/public/shared/{share_token}/media/{ref}`` on replay and live
  ``run_end``.
- Attachment history metadata (``server.utils.multimodal_context``) still
  uploads inline attachments under ``private-media/attachments/...`` and records
  ``object_key`` (plus ``attachment_id`` for staged uploads, omitting direct
  bucket ``url``), which AU1 controlled media routes verify and rewrite to
  authenticated backend media URLs on replay.

When ``workspace_files.public_bucket_urls`` is ``True`` (or
``DALTON_WSFILES_PUBLIC_BUCKET_URLS=1``), all four call sites preserve their
existing behavior (including AU1's ``private-media/`` prefixes and Step 2's
``STORAGE_BROWSER_ENDPOINT_URL`` check).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, MutableMapping
from typing import Any
from urllib.parse import quote

from src.dalton.download_ticket import has_browser_endpoint_url
from src.dalton.storage_config import is_wsfiles_public_bucket_urls

# Matches share tokens carried in request paths and /og-meta?path=... queries:
# - /api/v1/public/shared/<token>(/...)?
# - /s/<token>(/...)? and /a/<token>(/...)? (standalone target, after whitespace/quote, or after scheme://host)
# - ?path=/s/<token>, &path=/a/<token>, and percent-encoded %2Fs%2F<token> / %2Fa%2F<token>
_PUBLIC_SHARED_API_TOKEN_RE = re.compile(
    r"(/+api/v1/public/shared/+)([^/?#\s\"'<>]+)",
    re.IGNORECASE,
)
_SHARE_PAGE_PATH_TOKEN_RE = re.compile(
    r"(^|[\s\"'<(]|https?://[^/?#\s\"'<>]+)(/+(?:s|a)/+)([^/?#\s\"'<>]+)",
    re.IGNORECASE,
)
_OG_META_PATH_TOKEN_RE = re.compile(
    r"([?&]path=(?:/+|%2[fF])(?:s|a)(?:/+|%2[fF]))([^/?&#\s\"'<>]+)",
    re.IGNORECASE,
)


def is_public_bucket_urls_enabled(
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return True iff direct browser object-storage URLs are enabled."""
    return is_wsfiles_public_bucket_urls(env=env)


def allow_direct_download_url(
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return True iff ``download_link`` may issue a presigned browser URL.

    Stacks ``workspace_files.public_bucket_urls`` on top of Step 2's
    ``has_browser_endpoint_url()`` gate: both must be true to presign a direct
    browser link; otherwise callers return ``None`` so ``/files/download-url``
    issues a 60-second backend ``dl_token``.
    """
    if not is_wsfiles_public_bucket_urls(env=env):
        return False
    return has_browser_endpoint_url()


def allow_public_bucket_image_capture(
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return True iff image capture may emit direct object-storage URLs."""
    return is_wsfiles_public_bucket_urls(env=env)


def allow_public_bucket_attachment_urls(
    *,
    env: Mapping[str, str] | None = None,
) -> bool:
    """Return True iff attachment history metadata may emit direct bucket URLs."""
    return is_wsfiles_public_bucket_urls(env=env)


def format_captured_image_ref(
    key: str,
    *,
    public_url_fn: Callable[[str], str],
    env: Mapping[str, str] | None = None,
) -> str:
    """Return direct storage URL when public URLs are enabled, else a private key ref.

    In private mode (default), stores the percent-encoded logical object key
    (e.g. ``private-media/response-images/<thread>/<sha16>/<file>``) without any
    bucket host or ``/storage/`` prefix. AU1 controlled media routes verify and
    rewrite this ref to ``/api/v1/threads/{id}/media/{token}`` or
    ``/api/v1/public/shared/{share_token}/media/{ref}``.
    """
    if is_wsfiles_public_bucket_urls(env=env):
        return public_url_fn(key)
    cleaned = str(key or "").strip().lstrip("/")
    return quote(cleaned, safe="/")


def attach_private_attachment_ref(
    meta: MutableMapping[str, Any],
    *,
    object_key: str,
    public_url_fn: Callable[[str], str],
    attachment_id: str | None = None,
    env: Mapping[str, str] | None = None,
) -> MutableMapping[str, Any]:
    """Attach either a direct bucket URL (public mode) or AU1-verifiable ``object_key`` (private mode)."""
    cleaned_key = str(object_key or "").strip().lstrip("/")
    if not cleaned_key:
        return meta
    if is_wsfiles_public_bucket_urls(env=env):
        meta["url"] = public_url_fn(cleaned_key)
        return meta
    meta["object_key"] = cleaned_key
    if attachment_id:
        meta["attachment_id"] = str(attachment_id).strip()
    return meta


def redact_share_token_in_text(value: Any) -> Any:
    """Replace public share tokens in request paths and ``/og-meta?path=`` with ``REDACTED``."""
    if not isinstance(value, str) or not value:
        return value
    cleaned = _PUBLIC_SHARED_API_TOKEN_RE.sub(r"\1REDACTED", value)
    cleaned = _OG_META_PATH_TOKEN_RE.sub(r"\1REDACTED", cleaned)
    cleaned = _SHARE_PAGE_PATH_TOKEN_RE.sub(r"\1\2REDACTED", cleaned)
    return cleaned
