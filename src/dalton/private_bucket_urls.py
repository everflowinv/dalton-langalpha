"""Private-bucket browser URL containment helpers (Batch 1 Step 4, §1.5 #2).

When ``workspace_files.public_bucket_urls`` is ``False`` (the default in both
Everflow and Standalone Core), browsers never receive direct object-storage URLs:

- Workspace file downloads (``download_link.py``) skip presigned bucket URLs so
  ``/files/download-url`` falls back to the 60-second HMAC ``dl_token`` (for
  ``> 8 MiB`` files) or the authenticated ``/files/download`` stream.
- Sandbox image capture (``ptc_agent.agent.middleware.image_capture`` and
  ``server.services.persistence.image_capture``) skips emitting direct bucket
  URLs so messages keep their sandbox paths and the frontend renders them via
  the existing authenticated backend file proxy.
- Attachment history metadata (``server.utils.multimodal_context``) omits direct
  bucket URLs (and skips uploading inline copies solely for a public link).

When ``workspace_files.public_bucket_urls`` is ``True`` (or
``DALTON_WSFILES_PUBLIC_BUCKET_URLS=1``), all four call sites preserve their
existing behavior (including AU1's ``private-media/`` prefixes and Step 2's
``STORAGE_BROWSER_ENDPOINT_URL`` check).
"""

from __future__ import annotations

from collections.abc import Mapping

from src.dalton.download_ticket import has_browser_endpoint_url
from src.dalton.storage_config import is_wsfiles_public_bucket_urls


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
