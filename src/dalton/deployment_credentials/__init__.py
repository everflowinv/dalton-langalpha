"""Dalton deployment-level integration credentials package.

Exports the authoritative §2.3 read contract consumed across personal integrations,
the internal Feishu compatibility endpoint, and channel gateway sync.
"""

from __future__ import annotations

from src.dalton.deployment_credentials.snapshot import (
    CredentialGroupResult,
    CredentialSnapshot,
    CredentialSource,
    feishu_app_ready,
    get_credential_group,
    get_feishu_event_credentials,
    google_oauth_ready,
    resolve_credential_group,
    resolve_feishu_app_credentials,
)

__all__ = [
    "CredentialGroupResult",
    "CredentialSnapshot",
    "CredentialSource",
    "feishu_app_ready",
    "get_credential_group",
    "get_feishu_event_credentials",
    "google_oauth_ready",
    "resolve_credential_group",
    "resolve_feishu_app_credentials",
]
