"""A5 review: an assembly-phase kill must settle the publish request into
``audit_failed`` with the Chinese wording, not leave the row at ``auditing``."""

from __future__ import annotations

from typing import Any
from unittest.mock import create_autospec

import pytest

from src.dalton.publish import service as service_mod
from src.dalton.publish.audit import AuditBuildTimeout, aaudit_draft


class _FakeBackend:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.writes: list[str] = []

    async def aread_text(self, path: str) -> str | None:
        return self.files.get(path)

    async def awrite_text(self, path: str, content: str) -> None:
        self.writes.append(path)


async def _noop(*args: Any, **kwargs: Any) -> Any:
    return None


async def _fingerprint(*args: Any, **kwargs: Any) -> Any:
    return ("m", [], "v1", "cfgsha")


def _patch_common(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service_mod, "verify_workspace_access", _noop)
    monkeypatch.setattr(service_mod, "_load_workspace_provenance", _noop)
    monkeypatch.setattr(service_mod, "_load_referenced_model_cells", _noop)
    monkeypatch.setattr(service_mod, "_load_evidence_ref_bodies", _noop)
    monkeypatch.setattr(service_mod, "_check_unrecalculated_models", _noop)
    monkeypatch.setattr(service_mod, "resolve_audit_config_fingerprint", _fingerprint)
    monkeypatch.setattr(
        service_mod.db_knowledge,
        "latest_valid_version",
        create_autospec(service_mod.db_knowledge.latest_valid_version, return_value=None),
    )


@pytest.mark.asyncio
async def test_run_publish_settles_audit_failed_on_build_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transitions: list[tuple[Any, ...]] = []

    async def _transition(req_id, from_statuses, to, **kwargs):
        transitions.append((req_id, from_statuses, to, kwargs))
        return {"publish_request_id": req_id, "status": to}

    async def _raise_timeout(*args: Any, **kwargs: Any) -> Any:
        raise AuditBuildTimeout(
            "审计结果汇总超时，请重试（publish.audit_total_timeout_s 或机器负载可能需要调整）"
        )

    backend = _FakeBackend({"drafts/d.md": "# 草稿\nRevenue $1,234 million。\n"})

    _patch_common(monkeypatch)
    monkeypatch.setattr(
        service_mod.db_publications,
        "resolve_thread_author_model",
        create_autospec(
            service_mod.db_publications.resolve_thread_author_model,
            return_value="m",
        ),
    )
    monkeypatch.setattr(
        service_mod.db_knowledge,
        "list_all_latest_for_workspace",
        create_autospec(
            service_mod.db_knowledge.list_all_latest_for_workspace,
            return_value={},
        ),
    )
    monkeypatch.setattr(
        service_mod.db_publications,
        "get_request",
        create_autospec(
            service_mod.db_publications.get_request,
            return_value={
                "publish_request_id": "req-1",
                "workspace_id": "ws1",
                "user_id": "u1",
                "status": "auditing",
            },
        ),
    )
    monkeypatch.setattr(service_mod.db_publications, "transition", _transition)
    monkeypatch.setattr(
        service_mod, "aaudit_draft", create_autospec(aaudit_draft, side_effect=_raise_timeout)
    )

    result = await service_mod.run_publish(
        user_id="u1",
        workspace_id="ws1",
        thread_id="t1",
        tool_call_id="c1",
        draft_path="drafts/d.md",
        title="T",
        kind="topic",
        slug="s",
        request_id="req-1",
        author_model="m",
        backend=backend,
    )

    assert result["ok"] is False
    assert result["status"] == "audit_failed"
    assert "审计结果汇总超时" in result["message"]
    # The row settles into audit_failed with the wording saved on it.
    assert len(transitions) == 1
    req_id, from_statuses, to, kwargs = transitions[0]
    assert (req_id, from_statuses, to) == ("req-1", ("auditing",), "audit_failed")
    assert "审计结果汇总超时" in kwargs["error"]
    # No sidecars written for a failed build.
    assert backend.writes == []


@pytest.mark.asyncio
async def test_run_check_draft_reports_build_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The tool-side entry answers ok=False with the wording instead of raising."""

    async def _raise_timeout(*args: Any, **kwargs: Any) -> Any:
        raise AuditBuildTimeout("审计结果汇总超时，请重试（负载或预算需调整）")

    backend = _FakeBackend({"drafts/d.md": "# 草稿\nRevenue $1,234 million。\n"})

    _patch_common(monkeypatch)
    monkeypatch.setattr(
        service_mod, "aaudit_draft", create_autospec(aaudit_draft, side_effect=_raise_timeout)
    )

    result = await service_mod.run_check_draft(
        workspace_id="ws1",
        draft_path="drafts/d.md",
        backend=backend,
        user_id="u1",
    )
    assert result["ok"] is False
    assert "审计结果汇总超时" in result["message"]
    assert backend.writes == []
