"""Management tokens never reach host logs in plaintext (bridge-admin-token hook)."""

from __future__ import annotations

from src.dalton import settings as dalton_settings


def test_redact_managed_tokens_masks_both_values(monkeypatch):
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "tok-internal-123")
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "tok-admin-456")
    out = dalton_settings.redact_managed_tokens(
        "spawn failed: --a=tok-internal-123 --b=tok-admin-456"
    )
    assert "tok-internal-123" not in out and "tok-admin-456" not in out
    assert out.count("***") == 2


def test_redact_managed_tokens_passes_through_non_strings_and_empty_tokens(monkeypatch):
    monkeypatch.delenv("INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_ADMIN_TOKEN", raising=False)
    assert dalton_settings.redact_managed_tokens(None) is None
    assert dalton_settings.redact_managed_tokens("plain text") == "plain text"
