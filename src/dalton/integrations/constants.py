"""Authoritative OAuth callback and event route constants for personal integrations.

Shared between personal integration OAuth flows and the deployment credentials
management API so displayed callback URLs always match mounted routes.
"""

from __future__ import annotations

from src.dalton.settings import get_dalton_public_app_url

GOOGLE_OAUTH_CALLBACK_PATH = "/api/v1/dalton/integrations/google/callback"
FEISHU_OAUTH_CALLBACK_PATH = "/api/v1/dalton/integrations/feishu/callback"
FEISHU_INTEGRATION_EVENTS_PATH = "/api/v1/dalton/integrations/feishu/events"

_PROVIDER_CALLBACK_PATHS: dict[str, str] = {
    "google": GOOGLE_OAUTH_CALLBACK_PATH,
    "feishu": FEISHU_OAUTH_CALLBACK_PATH,
}


def build_integration_redirect_uri(provider: str, *, base_url: str | None = None) -> str:
    """Build the full OAuth callback URL for ``provider`` (``google`` or ``feishu``)."""
    normalized = str(provider or "").strip().lower()
    path = _PROVIDER_CALLBACK_PATHS.get(normalized)
    if path is None:
        raise ValueError(f"Unsupported integration provider: {provider!r}")
    origin = (
        get_dalton_public_app_url() if base_url is None else str(base_url)
    ).strip().rstrip("/")
    if not origin:
        return ""
    return f"{origin}{path}"


def build_feishu_integration_events_url(*, base_url: str | None = None) -> str:
    """Build the full Feishu personal-integration event subscription callback URL."""
    origin = (
        get_dalton_public_app_url() if base_url is None else str(base_url)
    ).strip().rstrip("/")
    if not origin:
        return ""
    return f"{origin}{FEISHU_INTEGRATION_EVENTS_PATH}"


__all__ = [
    "FEISHU_INTEGRATION_EVENTS_PATH",
    "FEISHU_OAUTH_CALLBACK_PATH",
    "GOOGLE_OAUTH_CALLBACK_PATH",
    "build_feishu_integration_events_url",
    "build_integration_redirect_uri",
]
