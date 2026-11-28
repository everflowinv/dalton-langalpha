"""Unit tests for Batch 2 (§1): Monitor publish 3-stage verification, sibling JSON discovery,
dual-axis realtime feed health, per-panel data-job health, evaluators, and ops export.

Note: Migration ``d061_monitor_publish_verify.py`` uses placeholder ``revision="d061"`` /
``down_revision="d060"`` (main head after D4: d059–d060). The chain test validates the real
d060 -> d061 upgrade/downgrade SQL.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
import pytest

from scripts.dalton.monitor_feeds import common as feeds_common
from scripts.dalton.monitor_feeds.pm_cockpit import run as pm_run
from scripts.dalton.monitor_feeds.run_feeds import FeedDaemon
from scripts.dalton.ops.prod import export_ops_status
from src.dalton.company_identity import COMPANY_SYSTEM_USER_ID
from src.dalton.data_jobs.evaluators import (
    SENTIMENT_DASHBOARD_JOB_KEY,
    SENTIMENT_DASHBOARD_PANEL_SLUGS,
    TaskEvaluationContext,
    evaluate_task_run,
)
from src.dalton.data_jobs.freeze import freeze_monitor_publish_artifacts
from src.dalton.migrations.versions import d061_monitor_publish_verify as d06y_mig
from src.dalton.monitors import (
    data_job_registry,
    db as monitor_db,
    grants as monitor_grants,
    service as monitor_service,
    verify as monitor_verify,
)
from src.dalton.monitors.models import (
    MonitorRecord,
    PublishMonitorRequest,
)
from src.dalton.monitors.router import router as monitors_router
from src.dalton import settings as dalton_settings
from src.server.database import artifacts as artifacts_db
from src.server.database.artifacts import Artifact, VersionFile
from src.server.database.artifacts.versions import StoredVersionFile
from src.server.preview_proxy.page_bridge import bridge_for
from src.server.services.artifacts import snapshot as artifact_snapshot
from src.server.services.artifacts.manifest import ManifestEntry
from src.server.services.artifacts.reader import Reader
from src.server.services.artifacts.snapshot import Snapshot
from src.server.utils.api import get_current_user_id

NOW = datetime(2026, 11, 27, 10, 0, 0, tzinfo=UTC)
MONITOR_ID = "70000000-0000-4000-8000-000000000001"
ARTIFACT_ID = "71000000-0000-4000-8000-000000000001"
WS_ID = "72000000-0000-4000-8000-000000000001"
TEST_KEY = b"dalton-verify-test-signing-key32"


@pytest.fixture(autouse=True)
def _stub_signing_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(monitor_grants._MONITOR_CODEC, "_key", TEST_KEY)
    monkeypatch.setattr(monitor_verify._CANDIDATE_PROBE_CODEC, "_key", TEST_KEY)


def _make_monitor(**overrides: Any) -> MonitorRecord:
    defaults: dict[str, Any] = dict(
        monitor_id=MONITOR_ID,
        slug="macro-pulse",
        artifact_id=ARTIFACT_ID,
        owner_user_id="admin-1",
        title="Macro Pulse",
        category="overview",
        sort_order=0,
        visibility="public",
        published_seq=1,
        last_verified_seq=1,
        last_verified_at=NOW,
        keep_updated=True,
        source_kind="workspace",
        source_ref=f"{WS_ID}:index.html",
        acl_revision=1,
        created_at=NOW,
        updated_at=NOW,
        artifact_code="codeverify01",
        workspace_id=WS_ID,
        entry_path="index.html",
        owner_name="Admin",
        owner_email="admin@example.com",
        data_as_of=None,
        member_user_ids=(),
    )
    defaults.update(overrides)
    return MonitorRecord(**defaults)


def _make_artifact(**overrides: Any) -> Artifact:
    defaults: dict[str, Any] = dict(
        artifact_id=ARTIFACT_ID,
        code="codeverify01",
        workspace_id=WS_ID,
        path="index.html",
        created_thread_id=None,
        bound_thread_id=None,
        keep_updated=False,
        latest_seq=1,
        shared_at=None,
        files=None,
        revision=0,
        created_at=NOW,
        shared_seq=0,
    )
    defaults.update(overrides)
    return Artifact(**defaults)


def _build_app(user_id: str = "admin-1") -> FastAPI:
    app = FastAPI()
    app.include_router(monitors_router)
    app.dependency_overrides[get_current_user_id] = lambda: user_id
    return app


def _make_version_file(path: str, raw: bytes, content_type: str = "text/html; charset=utf-8", inline: bool = True) -> VersionFile:
    return VersionFile(
        path=path,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_len=len(raw),
        content_type=content_type,
        content=raw if inline else None,
    )


def _make_stored_file(
    vf: VersionFile, *, user_id: str = "admin-1"
) -> StoredVersionFile:
    return StoredVersionFile(
        user_id=user_id,
        sha256=vf.sha256,
        byte_len=vf.byte_len,
        content_type=vf.content_type,
        content=vf.content,
    )


# ---------------------------------------------------------------------------
# 1. Stage 2 verification: actual entry bytes SHA-256 vs snap.entry_sha256,
#    per-dependency checks, HMAC candidate probe, and post-promotion readback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_candidate_checks_entry_sha256_deps_and_public_hmac_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_html = b"<!doctype html><html><head><title>V2</title></head><body>Hello v2</body></html>"
    small_json = b'{"series":[1,2,3]}'
    large_png_sha = hashlib.sha256(b"big-image-bytes").hexdigest()

    entry_vf = _make_version_file("index.html", entry_html, "text/html; charset=utf-8", inline=True)
    json_vf = _make_version_file("data/metrics.json", small_json, "application/json", inline=False)
    large_vf = VersionFile(
        path="assets/chart.png",
        sha256=large_png_sha,
        byte_len=5 * 1024 * 1024,
        content_type="image/png",
        content=None,
    )

    stored_map: dict[tuple[str, int, str], StoredVersionFile] = {
        (ARTIFACT_ID, 2, "index.html"): _make_stored_file(entry_vf),
        (ARTIFACT_ID, 2, "data/metrics.json"): _make_stored_file(json_vf),
        (ARTIFACT_ID, 2, "assets/chart.png"): _make_stored_file(large_vf),
    }

    async def _get_version_file(aid: str, seq: int, path: str) -> StoredVersionFile | None:
        return stored_map.get((aid, seq, path))

    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(artifacts_db.get_version_file, side_effect=_get_version_file),
    )
    fetch_blob_spy = create_autospec(
        monitor_verify.fetch_blob,
        side_effect=lambda uid, sha: small_json if sha == json_vf.sha256 else b"",
    )
    head_blob_spy = create_autospec(monitor_verify.head_blob, return_value=True)
    monkeypatch.setattr(monitor_verify, "fetch_blob", fetch_blob_spy)
    monkeypatch.setattr(monitor_verify, "head_blob", head_blob_spy)

    # Existing monitor still points at v1 while candidate v2 is being probed!
    live_v1_monitor = _make_monitor(published_seq=1, last_verified_seq=1)
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=live_v1_monitor),
    )
    monkeypatch.setattr(
        monitor_db,
        "is_monitor_version_published",
        create_autospec(monitor_db.is_monitor_version_published, side_effect=lambda _mid, s: s == 1),
    )

    # Confirm anonymous visitor STILL cannot read candidate v2 before Stage 3 promote
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        anon_v2 = await client.get("/api/v1/dalton/monitors/public/macro-pulse/v/2/index.html")
        assert anon_v2.status_code == 404

    result = await monitor_verify.verify_candidate_monitor_version(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        entry_path="index.html",
        snap_files=[entry_vf, json_vf, large_vf],
        snap_entry_sha256=entry_vf.sha256,
        visibility="public",
        slug="macro-pulse",
    )
    assert result.entry_sha256 == entry_vf.sha256
    assert result.verified_dependencies == 2
    assert result.public_probed is True
    fetch_blob_spy.assert_awaited_once_with("admin-1", json_vf.sha256)
    head_blob_spy.assert_awaited_once_with("admin-1", large_png_sha)

    # Tampering entry bytes causes SHA-256 mismatch failure before promotion
    stored_map[(ARTIFACT_ID, 2, "index.html")] = StoredVersionFile(
        user_id="admin-1",
        sha256=entry_vf.sha256,
        byte_len=len(entry_html),
        content_type="text/html; charset=utf-8",
        content=b"<html>corrupted bytes</html>",
    )
    with pytest.raises(monitor_verify.MonitorVerificationError, match="SHA-256 mismatch"):
        await monitor_verify.verify_candidate_monitor_version(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            entry_path="index.html",
            snap_files=[entry_vf, json_vf, large_vf],
            snap_entry_sha256=entry_vf.sha256,
            visibility="public",
            slug="macro-pulse",
        )


@pytest.mark.asyncio
async def test_first_time_publish_failure_leaves_no_shell_row_and_success_promotes_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setenv("DALTON_MONITOR_VERIFY_READBACK_ENABLED", "1")

    entry_html = b"<!doctype html><html><head><title>First</title></head><body>v1</body></html>"
    entry_vf = _make_version_file("index.html", entry_html)
    snap = Snapshot(
        entries=[ManifestEntry("index.html", len(entry_html), "entry")],
        left_out=[],
        files=[entry_vf],
        blobs={},
        title="First",
    )
    artifact = _make_artifact(latest_seq=0)

    monkeypatch.setattr(
        monitor_service,
        "validate_publish_permissions",
        create_autospec(monitor_service.validate_publish_permissions, return_value=[]),
    )
    monkeypatch.setattr(
        monitor_service,
        "owned_workspace_on_computer",
        create_autospec(
            monitor_service.owned_workspace_on_computer,
            return_value={"workspace_id": WS_ID, "user_id": "admin-1", "status": "running"},
        ),
    )
    monkeypatch.setattr(
        monitor_service,
        "_normalize_workspace_entry_path",
        create_autospec(monitor_service._normalize_workspace_entry_path, return_value="index.html"),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(artifacts_db.ensure_artifact, return_value=artifact),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_artifact_id",
        create_autospec(monitor_db.get_monitor_by_artifact_id, return_value=None),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=None),
    )
    monkeypatch.setattr(
        artifact_snapshot,
        "capture_workspace_monitor_snapshot",
        create_autospec(artifact_snapshot.capture_workspace_monitor_snapshot, return_value=snap),
    )

    stage_spy = create_autospec(
        monitor_db.stage_monitor_candidate_version_tx,
        return_value=monitor_db.StagedMonitorCandidate(
            artifact_id=ARTIFACT_ID, candidate_seq=1, cut=True
        ),
    )
    promote_spy = create_autospec(monitor_db.promote_verified_monitor_version_tx)
    monkeypatch.setattr(monitor_db, "stage_monitor_candidate_version_tx", stage_spy)
    monkeypatch.setattr(monitor_db, "promote_verified_monitor_version_tx", promote_spy)

    # Case A: Stage 2 fails -> promote MUST NOT be called (zero shell dalton_monitors rows)
    verify_spy = create_autospec(
        monitor_verify.verify_candidate_monitor_version,
        side_effect=monitor_verify.MonitorVerificationError("blob unreadable"),
    )
    monkeypatch.setattr(monitor_verify, "verify_candidate_monitor_version", verify_spy)
    req = PublishMonitorRequest(
        workspace_id=WS_ID,
        path="index.html",
        slug="first-pub",
        title="First",
        visibility="public",
    )
    with pytest.raises(HTTPException) as exc_info:
        await monitor_service.publish_workspace_monitor(req, caller_id="admin-1")
    assert exc_info.value.status_code == 500
    assert stage_spy.await_count == 1
    assert promote_spy.await_count == 0

    # Case B: Stage 2 (including pre-promote public detail & file probes) passes ->
    # Stage 3 promotes atomically with ZERO post-commit fallible readback calls!
    promoted_mon = _make_monitor(slug="first-pub", published_seq=1, last_verified_seq=1)
    verify_spy.side_effect = None
    verify_spy.return_value = monitor_verify.VerifiedCandidateResult(
        artifact_id=ARTIFACT_ID,
        candidate_seq=1,
        entry_path="index.html",
        entry_sha256=entry_vf.sha256,
        verified_dependencies=0,
        public_probed=True,
    )
    promote_spy.return_value = monitor_db.PromotedMonitorResult(
        monitor=promoted_mon, promoted=True
    )
    readback_spy = create_autospec(
        monitor_verify.verify_public_monitor_readback, return_value=None
    )
    monkeypatch.setattr(monitor_verify, "verify_public_monitor_readback", readback_spy)
    monkeypatch.setattr(
        monitor_db,
        "list_monitor_versions",
        create_autospec(monitor_db.list_monitor_versions, return_value=[]),
    )
    monkeypatch.setattr(
        monitor_service,
        "get_user_tier",
        create_autospec(monitor_service.get_user_tier, return_value="admin"),
    )

    detail = await monitor_service.publish_workspace_monitor(req, caller_id="admin-1")
    assert detail["cut"] is True
    assert detail["monitor"]["last_verified_seq"] == 1
    assert detail["monitor"]["has_recent_publish_issue"] is False
    promote_spy.assert_awaited_once()
    assert readback_spy.await_count == 0


@pytest.mark.asyncio
async def test_public_detail_cache_control_and_post_promote_readback_etag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry_html = b"<!doctype html><html><body>Verified Public</body></html>"
    entry_vf = _make_version_file("index.html", entry_html)
    mon = _make_monitor(
        slug="pub-check",
        published_seq=2,
        last_verified_seq=2,
        last_publish_error="latest failed",
        last_publish_failed_at=NOW,
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=mon),
    )
    monkeypatch.setattr(
        monitor_db,
        "is_monitor_version_published",
        create_autospec(monitor_db.is_monitor_version_published, return_value=True),
    )
    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(artifacts_db.get_version_file, return_value=_make_stored_file(entry_vf)),
    )

    # Full Stage 3 public readback verification via real FastAPI router
    await monitor_verify.verify_public_monitor_readback(
        slug="pub-check",
        candidate_seq=2,
        entry_path="index.html",
        expected_entry_sha256=entry_vf.sha256,
    )

    # Confirm public detail strips raw error but keeps last_verified_seq & has_recent_publish_issue + Cache-Control
    app = _build_app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        resp = await client.get("/api/v1/dalton/monitors/public/pub-check")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-cache"
        body = resp.json()
        assert body["monitor"]["last_verified_seq"] == 2
        assert body["monitor"]["has_recent_publish_issue"] is True
        assert "last_publish_error" not in body["monitor"]

        file_resp = await client.get("/api/v1/dalton/monitors/public/pub-check/v/2/index.html")
        assert file_resp.status_code == 200
        bridge = bridge_for("view")
        assert file_resp.headers["etag"] == f'"{entry_vf.sha256}-{bridge.digest}"'

    # Public monitor with effective_seq <= 0 returns 404
    zero_mon = _make_monitor(slug="zero-pub", published_seq=0, last_verified_seq=None)
    monitor_db.get_monitor_by_slug.return_value = zero_mon  # type: ignore[attr-defined]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://127.0.0.1") as client:
        resp_404 = await client.get("/api/v1/dalton/monitors/public/zero-pub")
        assert resp_404.status_code == 404


# ---------------------------------------------------------------------------
# 2. Sibling .json discovery & _after_change auto-update monitor check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reader_snapshot_discovers_sibling_and_data_json_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    panel_dir = tmp_path / "research-outputs" / "kweb-dashboard"
    (panel_dir / "data").mkdir(parents=True)
    (panel_dir / "portfolio").mkdir(parents=True)
    # HTML dynamically loads metrics.json without static reference
    (panel_dir / "index.html").write_text(
        "<!doctype html><html><body><script>fetch(base + '.json')</script></body></html>",
        encoding="utf-8",
    )
    (panel_dir / "metrics.json").write_text('{"score": 42}', encoding="utf-8")
    (panel_dir / "data" / "series.json").write_text('{"dates": ["2026-11-27"]}', encoding="utf-8")
    (panel_dir / "portfolio" / "notes.md").write_text("unrelated", encoding="utf-8")

    monkeypatch.setattr(
        artifact_snapshot,
        "get_vault_secrets_for_redaction",
        create_autospec(artifact_snapshot.get_vault_secrets_for_redaction, return_value={}),
    )
    monkeypatch.setattr(
        artifact_snapshot,
        "_store_blobs",
        create_autospec(artifact_snapshot._store_blobs, return_value=None),
    )
    monkeypatch.setattr(artifact_snapshot, "is_storage_enabled", lambda: False)

    binding = data_job_registry.get_registered_monitor_job("richard-relative-dashboard-refresh")
    assert binding is not None
    assert binding.dependencies_only is True

    source, work_dir, extra_files = monitor_service._open_binding_reader_source(
        tmp_path, binding, is_multi=False
    )
    assert "relative-strength/metrics.json" in extra_files
    assert "relative-strength/data/series.json" in extra_files
    assert not any(p.endswith(".md") for p in extra_files)

    with source:
        reader = Reader(files=source, work_dir=work_dir, previous_dirs=())
        snap = await artifact_snapshot.capture_reader_monitor_snapshot(
            reader,
            owner_id=COMPANY_SYSTEM_USER_ID,
            entry_path="relative-strength/index.html",
            extra_paths=extra_files,
        )
    captured_paths = {f.path for f in snap.files}
    assert "relative-strength/index.html" in captured_paths
    assert "relative-strength/metrics.json" in captured_paths
    assert "relative-strength/data/series.json" in captured_paths
    assert not any(p.endswith(".md") for p in captured_paths)


@pytest.mark.asyncio
async def test_after_change_proceeds_when_workspace_has_auto_update_monitors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        artifacts_db,
        "list_kept_updated",
        create_autospec(artifacts_db.list_kept_updated, return_value=[]),
    )
    monkeypatch.setattr(
        monitor_db,
        "has_workspace_auto_update_monitors",
        create_autospec(monitor_db.has_workspace_auto_update_monitors, return_value=True),
    )
    monkeypatch.setattr(
        artifact_snapshot,
        "_claim_change_pass",
        create_autospec(artifact_snapshot._claim_change_pass, return_value=True),
    )
    keep_turn_spy = create_autospec(
        artifact_snapshot.keep_updated_after_turn, return_value=None
    )
    monkeypatch.setattr(artifact_snapshot, "keep_updated_after_turn", keep_turn_spy)
    monkeypatch.setattr(artifact_snapshot, "_CHANGE_SETTLE_S", 0.0)

    await artifact_snapshot._after_change(WS_ID, "admin-1")
    keep_turn_spy.assert_awaited_once_with(workspace_id=WS_ID, user_id="admin-1")


# ---------------------------------------------------------------------------
# 3. Dual-axis monitor_data (as_of + attempt_seq) four-step sequence,
#    first-boot null snapshot failure, legacy wall-clock self-heal, & readback
# ---------------------------------------------------------------------------


class _InMemoryMonitorDataDB:
    """Deterministic single-table fake backing ``upsert_monitor_data`` / ``get_monitor_data``."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    def make_conn_ctx(self):
        store = self.rows

        class _FakeCursor:
            def __init__(self) -> None:
                self._result: dict[str, Any] | None = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

            async def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
                q = " ".join(sql.split())
                assert params is not None
                if q.startswith("SELECT monitor_id::text"):
                    mid, key = str(params[0]), str(params[1])
                    row = store.get((mid, key))
                    self._result = dict(row) if row is not None else None
                elif q.startswith("INSERT INTO dalton_monitor_data"):
                    mid, key, payload_json, as_of, sha, seq, is_stale, last_att, last_err = params
                    k = (str(mid), str(key))
                    if k in store:
                        self._result = None
                    else:
                        row = {
                            "monitor_id": str(mid),
                            "data_key": str(key),
                            "payload": json.loads(payload_json) if payload_json is not None else None,
                            "as_of": as_of,
                            "sha256": sha,
                            "updated_at": datetime.now(UTC),
                            "attempt_seq": int(seq),
                            "is_stale": bool(is_stale),
                            "last_attempt_at": last_att,
                            "last_error": last_err,
                        }
                        store[k] = row
                        self._result = dict(row)
                elif q.startswith("UPDATE dalton_monitor_data SET payload"):
                    payload_json, as_of, sha, seq, is_stale, last_att, last_err, mid, key = params
                    k = (str(mid), str(key))
                    row = dict(store[k])
                    row.update(
                        {
                            "payload": json.loads(payload_json) if payload_json is not None else None,
                            "as_of": as_of,
                            "sha256": sha,
                            "attempt_seq": int(seq),
                            "is_stale": bool(is_stale),
                            "last_attempt_at": last_att,
                            "last_error": last_err,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    store[k] = row
                    self._result = dict(row)
                elif q.startswith("UPDATE dalton_monitor_data SET attempt_seq"):
                    seq, is_stale, last_att, last_err, mid, key = params
                    k = (str(mid), str(key))
                    row = dict(store[k])
                    row.update(
                        {
                            "attempt_seq": int(seq),
                            "is_stale": bool(is_stale),
                            "last_attempt_at": last_att,
                            "last_error": last_err,
                            "updated_at": datetime.now(UTC),
                        }
                    )
                    store[k] = row
                    self._result = dict(row)
                else:
                    raise AssertionError(f"Unexpected SQL: {q}")

            async def fetchone(self) -> dict[str, Any] | None:
                return self._result

        class _FakeTx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return False

        class _FakeConn:
            def transaction(self):
                return _FakeTx()

            def cursor(self, **_kw):
                return _FakeCursor()

        @asynccontextmanager
        async def _ctx():
            yield _FakeConn()

        return _ctx


@pytest.mark.asyncio
async def test_monitor_data_dual_axis_four_step_sequence_and_first_boot_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mem = _InMemoryMonitorDataDB()
    monkeypatch.setattr(monitor_db, "get_db_connection", mem.make_conn_ctx())

    t1 = datetime(2026, 11, 27, 7, 0, 0, tzinfo=UTC)
    snap_p1 = {"close": 3300.5, "stale": False}

    # Step 1: Success (seq=10, as_of=T1)
    r1 = await monitor_db.upsert_monitor_data(
        MONITOR_ID, "snapshot", snap_p1, as_of=t1, attempt_seq=10, is_stale=False
    )
    assert r1.payload == snap_p1
    assert r1.as_of == t1
    assert r1.attempt_seq == 10
    assert r1.is_stale is False
    assert r1.last_error is None

    # Step 2: New failure (seq=11, is_stale=True) -> retains T1 snapshot, flips is_stale=True
    r2 = await monitor_db.upsert_monitor_data(
        MONITOR_ID, "snapshot", None, as_of=None, attempt_seq=11, is_stale=True, error="upstream 502"
    )
    assert r2.payload == snap_p1
    assert r2.as_of == t1
    assert r2.attempt_seq == 11
    assert r2.is_stale is True
    assert r2.last_error == "upstream 502"

    # Step 3: Recovery at SAME market close time T1 (seq=12, as_of=T1, is_stale=False)
    r3 = await monitor_db.upsert_monitor_data(
        MONITOR_ID, "snapshot", snap_p1, as_of=t1, attempt_seq=12, is_stale=False
    )
    assert r3.payload == snap_p1
    assert r3.as_of == t1
    assert r3.attempt_seq == 12
    assert r3.is_stale is False
    assert r3.last_error is None

    # Step 4: Late out-of-order failure (seq=11, is_stale=True) -> ignored, remains healthy seq=12!
    r4 = await monitor_db.upsert_monitor_data(
        MONITOR_ID, "snapshot", None, as_of=None, attempt_seq=11, is_stale=True, error="stale timeout"
    )
    assert r4.attempt_seq == 12
    assert r4.is_stale is False
    assert r4.last_error is None

    # First-boot failure (no prior snapshot) stores payload=None, as_of=None, sha256=None, is_stale=True
    first_fail = await monitor_db.upsert_monitor_data(
        MONITOR_ID,
        "intraday",
        None,
        as_of=None,
        attempt_seq=1,
        is_stale=True,
        error="calendar unavailable",
        last_attempt_at=NOW,
    )
    assert first_fail.payload is None
    assert first_fail.as_of is None
    assert first_fail.sha256 is None
    assert first_fail.is_stale is True
    assert first_fail.last_error == "calendar unavailable"

    # _read_monitor_data_response exposes health + null snapshot fields (and hides last_error publicly)
    mon = _make_monitor()
    st_auth, body_auth, hdr_auth = await monitor_service._read_monitor_data_response(
        mon, "intraday", include_error_detail=True
    )
    assert st_auth == 200
    assert body_auth is not None
    assert body_auth["data"] is None
    assert body_auth["as_of"] is None
    assert body_auth["is_stale"] is True
    assert body_auth["has_fetch_error"] is True
    assert body_auth["attempt_seq"] == 1
    assert body_auth["last_error"] == "calendar unavailable"

    st_pub, body_pub, _hdr_pub = await monitor_service._read_monitor_data_response(
        mon, "intraday", include_error_detail=False
    )
    assert st_pub == 200
    assert body_pub is not None
    assert body_pub["is_stale"] is True
    assert body_pub["has_fetch_error"] is True
    assert "last_error" not in body_pub

    # Legacy wall-clock polluted row self-heals on fresh close with earlier as_of!
    wall_t = datetime(2026, 11, 27, 7, 5, 0, tzinfo=UTC)
    close_t = datetime(2026, 11, 27, 7, 0, 0, tzinfo=UTC)
    mem.rows[(MONITOR_ID, "legacy_key")] = {
        "monitor_id": MONITOR_ID,
        "data_key": "legacy_key",
        "payload": {"stale": True, "close": 100},
        "as_of": wall_t,
        "sha256": hashlib.sha256(b'{"close":100,"stale":true}').hexdigest(),
        "updated_at": wall_t,
        "attempt_seq": 5,
        "is_stale": True,
        "last_attempt_at": wall_t,
        "last_error": "old error",
    }
    healed = await monitor_db.upsert_monitor_data(
        MONITOR_ID, "legacy_key", {"close": 101, "stale": False}, as_of=close_t, attempt_seq=6, is_stale=False
    )
    assert healed.as_of == close_t
    assert healed.payload == {"close": 101, "stale": False}
    assert healed.is_stale is False

    # Backwards as_of on a fresh row is still rejected with 409
    with pytest.raises(HTTPException) as exc_409:
        await monitor_db.upsert_monitor_data(
            MONITOR_ID,
            "legacy_key",
            {"close": 99, "stale": False},
            as_of=close_t - timedelta(minutes=5),
            attempt_seq=7,
            is_stale=False,
        )
    assert exc_409.value.status_code == 409


# ---------------------------------------------------------------------------
# 4. FeedDaemon monotonic attempt_seq & no wall-clock as_of on failure;
#    PM per-panel health, multi_binding_publish_mode, sentiment evaluator,
#    kweb --no-upload exit, freeze isolation, ops export, and d061 metadata
# ---------------------------------------------------------------------------


def test_feed_daemon_sends_monotonic_attempt_seq_and_omits_as_of_on_failure(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, Any]] = []

    def _capture_push(slug: str, data_key: str, payload: Any = None, **kw: Any) -> dict[str, Any]:
        calls.append({"slug": slug, "data_key": data_key, "payload": payload, **kw})
        return {"ok": True}

    push_mock = create_autospec(feeds_common.push_monitor_data, side_effect=_capture_push)
    cn_fn = create_autospec(
        lambda: {},
        return_value={"as_of": "2026-11-27 14:30:00", "as_of_iso": "2026-11-27T14:30:00+08:00", "stale": False},
    )
    t1 = datetime(2026, 11, 27, 14, 30, 0, tzinfo=feeds_common.CN_TZ)
    daemon = FeedDaemon(
        state_dir=tmp_path / "feeds",
        feeds=("cn",),
        cn_collect_fn=cn_fn,
        push_fn=push_mock,
        now_fn=lambda: t1,
    )
    daemon.run_tick(now=t1, force=True)
    assert len(calls) == 1
    seq1 = calls[0]["attempt_seq"]
    assert isinstance(seq1, int) and seq1 > 0
    assert calls[0]["is_stale"] is False
    assert calls[0]["as_of"] == "2026-11-27T14:30:00+08:00"

    # Failure tick: attempt_seq strictly increases and as_of is None (no wall-clock pollution!)
    cn_fn.side_effect = RuntimeError("quote timeout")
    t2 = datetime(2026, 11, 27, 14, 31, 0, tzinfo=feeds_common.CN_TZ)
    daemon.run_tick(now=t2, force=True)
    assert len(calls) == 2
    seq2 = calls[1]["attempt_seq"]
    assert seq2 > seq1
    assert calls[1]["is_stale"] is True
    assert calls[1]["as_of"] is None
    assert "quote timeout" in (calls[1]["error"] or "")


def test_pm_panel_statuses_and_binding_source_health_split_us_and_cn(tmp_path: Path) -> None:
    sources = {
        "us": {"name": "us", "label": "美股", "ok": True, "stale": False, "last_success_at": "2026-11-27 08:00"},
        "cn": {"name": "cn", "label": "A股", "ok": False, "stale": True, "reason": "akshare 502", "last_success_at": "2026-11-26 16:00"},
        "cn_valuation": {"name": "cn_valuation", "label": "估值", "ok": True, "stale": False},
    }
    panel_st = pm_run._pm_panel_statuses(
        mode="all", sources=sources, compute_ok=True, render_ok=True
    )
    assert panel_st["pm-market-state"]["ok"] is True
    assert panel_st["cn-sector-rotation"]["ok"] is False
    assert "akshare 502" in (panel_st["cn-sector-rotation"]["reason"] or "")

    st_file = tmp_path / "state" / "monitor-feeds" / "pm_cockpit" / "status.json"
    st_file.parent.mkdir(parents=True)
    st_file.write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")

    us_binding, cn_binding = data_job_registry.get_registered_monitor_bindings("pm-market-state")
    assert data_job_registry.check_binding_source_health(tmp_path, us_binding) == (True, None)
    cn_ok, cn_err = data_job_registry.check_binding_source_health(tmp_path, cn_binding)
    assert cn_ok is False
    assert cn_err is not None and "akshare 502" in cn_err


def test_sentiment_dashboard_evaluator_and_kweb_no_upload_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "research-outputs" / "sentiment-dashboard"
    shared = base / "panels" / "_shared"
    shared.mkdir(parents=True)
    (shared / "panel.css").write_text("body{}", encoding="utf-8")
    (shared / "panel.js").write_text("console.log(1)", encoding="utf-8")
    for slug in SENTIMENT_DASHBOARD_PANEL_SLUGS:
        p = base / "panels" / slug / "index.html"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<html>ok</html>", encoding="utf-8")
    (base / "signals.json").write_text(json.dumps({"date": "2026-11-27"}), encoding="utf-8")

    res_ok = evaluate_task_run(
        TaskEvaluationContext(
            job_key=SENTIMENT_DASHBOARD_JOB_KEY,
            exit_code=0,
            stdout='{"ok": true}',
            data_root=tmp_path,
            reference_date=datetime(2026, 11, 27, tzinfo=UTC).date(),
        )
    )
    assert res_ok.ok is True

    # Stale signals.json fails evaluator
    (base / "signals.json").write_text(json.dumps({"date": "2026-11-10"}), encoding="utf-8")
    res_stale = evaluate_task_run(
        TaskEvaluationContext(
            job_key=SENTIMENT_DASHBOARD_JOB_KEY,
            exit_code=0,
            stdout='{"ok": true}',
            data_root=tmp_path,
            reference_date=datetime(2026, 11, 27, tzinfo=UTC).date(),
        )
    )
    assert res_stale.ok is False
    assert "sentiment_signals_stale" in (res_stale.failure_reason or "")

    # kweb_dashboard_cli.deploy(..., no_upload=True) MUST return ok=False when ETF board fails
    import importlib.util
    import sys
    import types

    ws_mod = types.ModuleType("openclaw_workspace")
    ws_mod.workspace_root = create_autospec(lambda _f: tmp_path, return_value=tmp_path)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openclaw_workspace", ws_mod)

    kweb_path = (
        Path(__file__).resolve().parents[3]
        / "scripts/dalton/data_assets/skills/kweb-dashboard/scripts/kweb_dashboard_cli.py"
    )
    spec = importlib.util.spec_from_file_location("kweb_dashboard_cli_test", kweb_path)
    assert spec is not None and spec.loader is not None
    kweb_mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = kweb_mod
    spec.loader.exec_module(kweb_mod)

    monkeypatch.setattr(kweb_mod, "load_config", lambda: {})
    monkeypatch.setattr(
        kweb_mod,
        "build",
        lambda _out, _cfg, update_portfolio=True: {
            "ok": True,
            "warnings": [],
            "overlong_notes": [],
            "etf_board": {"ok": False, "errors": ["yfinance 429"]},
        },
    )
    dep_res = kweb_mod.deploy(tmp_path / "kweb.html", no_upload=True)
    assert dep_res["ok"] is False
    assert "yfinance 429" in (dep_res.get("error") or "")


def test_freeze_monitor_artifacts_isolates_run_and_ops_export_excludes_superseded(
    tmp_path: Path,
) -> None:
    pdir = tmp_path / "research-outputs" / "cn-intraday"
    pdir.mkdir(parents=True)
    (pdir / "index.html").write_text("<html>run1</html>", encoding="utf-8")
    (pdir / "data.json").write_text('{"v":1}', encoding="utf-8")

    frozen = freeze_monitor_publish_artifacts(
        data_root=tmp_path, job_key="cn-intraday", run_id="run-001"
    )
    assert frozen is not None
    assert frozen == tmp_path / ".freeze" / "cn-intraday" / "runs" / "run-001"

    # Overwrite live data_root (simulating Run 2) -> Run 1 frozen_dir stays unchanged!
    (pdir / "index.html").write_text("<html>run2</html>", encoding="utf-8")
    assert (frozen / "research-outputs" / "cn-intraday" / "index.html").read_text(encoding="utf-8") == "<html>run1</html>"

    # Ops status exporter includes failed/partial_failed/disabled and excludes superseded
    job = export_ops_status.inspect_monitor_publish_job(
        issue_rows=[
            {"run_id": "r1", "job_key": "pm-market-state", "monitor_publish_status": "superseded", "completed_at": "2026-11-27T09:00:00Z"},
            {"run_id": "r2", "job_key": "cn-intraday", "monitor_publish_status": "partial_failed", "completed_at": "2026-11-27T09:30:00Z", "last_error": "cn failed"},
        ]
    )
    assert job["issue_count"] == 1
    assert job["status"] == "partial"
    assert job["issues"][0]["job_key"] == "cn-intraday"


def test_settings_accessors_and_d06y_migration_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DALTON_MONITOR_VERIFY_READBACK_ENABLED", raising=False)
    monkeypatch.delenv("DALTON_MONITOR_MULTI_BINDING_PUBLISH_MODE", raising=False)
    assert dalton_settings.is_monitor_verify_readback_enabled() is True
    assert dalton_settings.get_monitor_multi_binding_publish_mode() == "per_panel"

    monkeypatch.setenv("DALTON_MONITOR_VERIFY_READBACK_ENABLED", "0")
    monkeypatch.setenv("DALTON_MONITOR_MULTI_BINDING_PUBLISH_MODE", "all_or_nothing")
    assert dalton_settings.is_monitor_verify_readback_enabled() is False
    assert dalton_settings.get_monitor_multi_binding_publish_mode() == "all_or_nothing"

    # Migration metadata check (anchored on d060 main head)
    assert d06y_mig.revision == "d061"
    assert d06y_mig.down_revision == "d060"


# ---------------------------------------------------------------------------
# 5. Batch 2 R2 tests: required JS/CSS pre-promote verification, 3-fence promote,
#    mandatory frozen_dir & disabled alert, all_or_nothing single-tx rollback,
#    per_panel PM isolation, root & excluded sibling JSON, same as_of backfill,
#    backend DB ops status, rolling effective_seq, and Alembic d058->d061 chain
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_candidate_rejects_missing_referenced_js_css_outside_snap_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Entry HTML referencing JS/CSS outside snap.files MUST fail pre-promotion verification."""
    html_bytes = (
        b'<!doctype html><html><head>'
        b'<link rel="stylesheet" href="assets/theme.css">'
        b'<script src="assets/app.js"></script>'
        b"</head><body>Dashboard</body></html>"
    )
    css_bytes = b'@import "tokens.css"; body { color: black; }'
    tokens_bytes = b":root { --fg: #111; }"
    js_bytes = b"console.log('ready');"

    entry_vf = _make_version_file("index.html", html_bytes, "text/html; charset=utf-8")
    css_vf = _make_version_file("assets/theme.css", css_bytes, "text/css")
    tokens_vf = _make_version_file("assets/tokens.css", tokens_bytes, "text/css")
    js_vf = _make_version_file("assets/app.js", js_bytes, "application/javascript")

    # Initially assets/app.js is missing from DB even though snap_files only lists index.html!
    stored_map: dict[tuple[str, int, str], StoredVersionFile] = {
        (ARTIFACT_ID, 2, "index.html"): _make_stored_file(entry_vf),
        (ARTIFACT_ID, 2, "assets/theme.css"): _make_stored_file(css_vf),
        (ARTIFACT_ID, 2, "assets/tokens.css"): _make_stored_file(tokens_vf),
    }

    async def _get_vf(aid: str, seq: int, path: str) -> StoredVersionFile | None:
        return stored_map.get((aid, seq, path))

    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(artifacts_db.get_version_file, side_effect=_get_vf),
    )
    live_v1 = _make_monitor(published_seq=1, last_verified_seq=1)
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(monitor_db.get_monitor_by_slug, return_value=live_v1),
    )

    with pytest.raises(monitor_verify.MonitorVerificationError, match="assets/app.js"):
        await monitor_verify.verify_candidate_monitor_version(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            entry_path="index.html",
            snap_files=[entry_vf],
            snap_entry_sha256=entry_vf.sha256,
            visibility="public",
            slug="macro-pulse",
        )

    # Once assets/app.js is present, verify_candidate_monitor_version probes detail, entry, AND JS/CSS!
    stored_map[(ARTIFACT_ID, 2, "assets/app.js")] = _make_stored_file(js_vf)
    res = await monitor_verify.verify_candidate_monitor_version(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        entry_path="index.html",
        snap_files=[entry_vf],
        snap_entry_sha256=entry_vf.sha256,
        visibility="public",
        slug="macro-pulse",
    )
    assert res.public_probed is True
    assert set(res.probed_assets) == {"assets/theme.css", "assets/tokens.css", "assets/app.js"}

    # If pre-promote public readback fails (e.g. HTTP 500), promote is NEVER called!
    async def _failing_probe(_path: str, *, headers: dict[str, str] | None = None):
        import httpx

        return httpx.Response(500, text="boom")

    with pytest.raises(monitor_verify.MonitorVerificationError, match="public detail probe failed"):
        await monitor_verify.verify_candidate_monitor_version(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            entry_path="index.html",
            snap_files=[entry_vf],
            snap_entry_sha256=entry_vf.sha256,
            visibility="public",
            slug="macro-pulse",
            probe_get=_failing_probe,
        )


@pytest.mark.asyncio
async def test_promote_three_fences_claim_epoch_share_revision_and_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Promote transaction enforces all 3 fences in-tx: claim_epoch, share_revision/acl_revision, and superseded."""
    from src.server.database.artifacts.versions import SharingChanged

    # Fake DB cursor simulating dalton_data_task_runs + share_links + dalton_monitors locks
    state: dict[str, Any] = {
        "run_row": {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "job_key": "cn-intraday",
            "status": "succeeded",
            "scheduled_slot_utc": NOW,
            "completed_at": NOW,
            "monitor_publish_status": "publishing",
            "monitor_publish_claim_epoch": 2,
        },
        "has_newer_run": False,
        "artifact": _make_artifact(revision=5, audience="public", latest_seq=2),
        "monitor_row": {
            "monitor_id": MONITOR_ID,
            "visibility": "own",
            "owner_user_id": "admin-1",
            "source_kind": "workspace",
            "title": "Test",
            "published_seq": 1,
            "last_verified_seq": 1,
            "acl_revision": 3,
        },
        "writes": [],
    }

    class _FenceCursor:
        def __init__(self) -> None:
            self._one: Any = None
            self._many: list[Any] = []
            self.rowcount = 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(sql.split())
            if "FROM dalton_data_task_runs WHERE run_id =" in q:
                state.setdefault("events", []).append("run_fence_lock")
                self._one = dict(state["run_row"])
            elif "FROM dalton_data_task_runs WHERE job_key =" in q:
                assert "FOR SHARE" in q
                state.setdefault("events", []).append("superseded_check")
                self._many = (
                    [
                        {
                            "run_id": "22222222-2222-4222-8222-222222222222",
                            "status": "succeeded",
                            "monitor_publish_status": "published",
                        }
                    ]
                    if state["has_newer_run"]
                    else []
                )
                self._one = self._many[0] if self._many else None
            elif "FROM dalton_monitors WHERE" in q:
                self._one = dict(state["monitor_row"])
            elif q.startswith(("UPDATE", "INSERT", "DELETE")):
                state["writes"].append(q)
                self._one = None
            else:
                self._one = None

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._many)

    class _FenceTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _FenceConn:
        def transaction(self):
            return _FenceTx()

        def cursor(self, **_kw):
            return _FenceCursor()

    @asynccontextmanager
    async def _conn_ctx():
        yield _FenceConn()

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        monitor_db.artifacts_records_db,
        "lock_owned",
        create_autospec(
            monitor_db.artifacts_records_db.lock_owned,
            side_effect=lambda _cur, **_kw: state["artifact"],
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "_select_monitor_in_cur",
        create_autospec(monitor_db._select_monitor_in_cur, return_value=_make_monitor()),
    )

    # Fence 1: stale claim_epoch=1 when DB has epoch=2 -> fenced_out with zero writes!
    res_epoch = await monitor_db.promote_verified_monitor_version_tx(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        paths=["index.html"],
        monitor_id=MONITOR_ID,
        run_id="11111111-1111-4111-8111-111111111111",
        expected_claim_epoch=1,
        check_superseded=True,
    )
    assert res_epoch.fenced_out is True
    assert res_epoch.promoted is False
    assert state["writes"] == []

    # Fence 3: newer run already publishing in dalton_data_task_runs -> superseded in-tx with zero writes!
    state["has_newer_run"] = True
    res_sup = await monitor_db.promote_verified_monitor_version_tx(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        paths=["index.html"],
        monitor_id=MONITOR_ID,
        run_id="11111111-1111-4111-8111-111111111111",
        expected_claim_epoch=2,
        check_superseded=True,
    )
    assert res_sup.superseded_by_newer is True
    assert res_sup.promoted is False
    assert state["writes"] == []

    # Fence 2a: share_links.revision moved (e.g. unshare during verification) -> raises SharingChanged!
    state["has_newer_run"] = False
    with pytest.raises(SharingChanged):
        await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            workspace_owner_id="admin-1",
            paths=["index.html"],
            monitor_id=MONITOR_ID,
            visibility="public",
            update_metadata=True,
            expected_share_revision=4,  # DB now at 5
            expected_share_audience="public",
            expected_acl_revision=3,
        )
    assert state["writes"] == []

    # Fence 2b: share_links.audience changed -> raises SharingChanged!
    with pytest.raises(SharingChanged):
        await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            workspace_owner_id="admin-1",
            paths=["index.html"],
            monitor_id=MONITOR_ID,
            visibility="public",
            update_metadata=True,
            expected_share_revision=5,
            expected_share_audience="company",  # DB now 'public'
            expected_acl_revision=3,
        )
    assert state["writes"] == []

    # Fence 2c: dalton_monitors.acl_revision moved (e.g. visibility downgraded) -> raises SharingChanged!
    with pytest.raises(SharingChanged):
        await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            workspace_owner_id="admin-1",
            paths=["index.html"],
            monitor_id=MONITOR_ID,
            visibility="public",
            update_metadata=True,
            expected_share_revision=5,
            expected_share_audience="public",
            expected_acl_revision=2,  # DB now 3
        )
    assert state["writes"] == []


@pytest.mark.asyncio
async def test_frozen_dir_required_no_enqueue_on_freeze_fail_and_disabled_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Freeze failure must not enqueue & must alert; missing frozen_dir must not fall back to data_root; disabled must alert."""
    from src.dalton.data_jobs import scheduler as sched_mod
    from src.dalton.data_jobs.container_runner import build_path_map

    paths = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    paths.backend_data_root.mkdir(parents=True, exist_ok=True)

    # 1. Missing frozen_dir during execute_claimed_monitor_publish NEVER falls back to backend_data_root
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    republish_spy = create_autospec(monitor_service.republish_data_job_monitor)
    monkeypatch.setattr(monitor_service, "republish_data_job_monitor", republish_spy)
    monkeypatch.setattr(
        monitor_service,
        "maybe_publish_data_job_monitor",
        create_autospec(monitor_service.maybe_publish_data_job_monitor, return_value=None),
    )
    settle_spy = create_autospec(sched_mod.settle_monitor_publish_run_tx, return_value=True)
    alert_spy = create_autospec(sched_mod._emit_monitor_publish_ops_alert, return_value=True)
    monkeypatch.setattr(sched_mod, "settle_monitor_publish_run_tx", settle_spy)
    monkeypatch.setattr(sched_mod, "_emit_monitor_publish_ops_alert", alert_spy)

    claimed = sched_mod.ClaimedMonitorPublishRun(
        run_id="22222222-2222-4222-8222-222222222222",
        job_key="cn-intraday",
        status="succeeded",
        failure_reason=None,
        scheduled_slot_utc=NOW,
        completed_at=NOW,
        attempts=1,
        claim_epoch=1,
        detail={
            "frozen_dir": str(tmp_path / "nonexistent-freeze-dir"),
            "panels": {"cn-intraday": {"status": "pending"}},
        },
    )
    final_st = await sched_mod.execute_claimed_monitor_publish(
        object(), claimed, path_map=paths
    )
    assert final_st == "failed"
    assert republish_spy.await_count == 0
    settle_spy.assert_awaited_once()
    assert settle_spy.await_args.kwargs["next_status"] == "failed"
    assert settle_spy.await_args.kwargs["next_retry_delay_s"] is None
    alert_spy.assert_awaited_once()
    assert alert_spy.await_args.kwargs["status"] == "failed"

    # 2. Publishing disabled during execute settles as 'disabled' AND emits ops alert!
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "0")
    settle_spy.reset_mock()
    alert_spy.reset_mock()
    st_disabled = await sched_mod.execute_claimed_monitor_publish(
        object(), claimed, path_map=paths
    )
    assert st_disabled == "disabled"
    settle_spy.assert_awaited_once()
    assert settle_spy.await_args.kwargs["next_status"] == "disabled"
    alert_spy.assert_awaited_once()
    assert alert_spy.await_args.kwargs["status"] == "disabled"

    # 3. Empty directory freeze returns None (cannot freeze empty output)
    empty_res = freeze_monitor_publish_artifacts(
        data_root=paths.backend_data_root,
        job_key="cn-intraday",
        run_id="run-empty-001",
    )
    assert empty_res is None

    # 4. finalize_task_run: freeze failure sets status='failed' (no enqueue) + emits alert;
    #    disabled publishing sets status='disabled' + emits alert!
    from src.dalton.data_jobs.evaluators import TaskEvaluationResult

    captured_updates: list[dict[str, Any]] = []

    class _FinCur:
        rowcount = 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            if "UPDATE dalton_data_task_runs" in sql and isinstance(params, dict):
                captured_updates.append(dict(params))

        async def fetchone(self):
            return {"consecutive_failures": 0}

    class _FinTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _FinConn:
        def transaction(self):
            return _FinTx()

        def cursor(self, **_kw):
            return _FinCur()

    monkeypatch.setattr(
        sched_mod,
        "check_scheduler_lock",
        create_autospec(sched_mod.check_scheduler_lock, return_value=None),
    )
    monkeypatch.setattr(
        sched_mod,
        "freeze_task_run_artifacts",
        create_autospec(sched_mod.freeze_task_run_artifacts, return_value=None),
    )
    process_spy = create_autospec(sched_mod.process_monitor_publish_for_run, return_value="published")
    monkeypatch.setattr(sched_mod, "process_monitor_publish_for_run", process_spy)
    monkeypatch.setattr(
        sched_mod,
        "freeze_monitor_publish_artifacts",
        create_autospec(sched_mod.freeze_monitor_publish_artifacts, side_effect=OSError("disk full")),
    )
    monkeypatch.setattr(
        "src.dalton.capabilities.has_capability",
        lambda _cap: True,
    )

    claimed_run = sched_mod.ClaimedTaskRun(
        run_id="55555555-5555-4555-8555-555555555555",
        job_key="cn-intraday",
        generation=1,
        runner="host",
        command_argv=["echo", "ok"],
        workdir="/tmp",
        timeout_seconds=300,
        no_output_timeout_seconds=120,
        required_secrets=[],
        cos_mode="none",
        notify_enabled=False,
        scheduled_slot_utc=NOW,
        deadline_utc=NOW + timedelta(minutes=30),
        trigger_source="manual",
        is_trial=False,
    )
    outcome = sched_mod.ContainerInspectionOutcome(
        container_id="host-run",
        running=False,
        exit_code=0,
        oom_killed=False,
        log_tail="",
    )
    evaluation = TaskEvaluationResult(ok=True, status="succeeded")

    # 4a: Freeze failure -> monitor_publish_status='failed', monitor_publish_schedule_now=False, alert emitted!
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    alert_spy.reset_mock()
    captured_updates.clear()
    ok_fin, _cf = await sched_mod.finalize_task_run(
        _FinConn(),
        lock_conn=object(),
        claimed=claimed_run,
        outcome=outcome,
        evaluation=evaluation,
        docker_client=None,
        container_id=None,
        path_map=paths,
    )
    assert ok_fin is True
    assert captured_updates[-1]["monitor_publish_status"] == "failed"
    assert captured_updates[-1]["monitor_publish_schedule_now"] is False
    assert process_spy.await_count == 0
    alert_spy.assert_awaited_once()
    assert alert_spy.await_args.kwargs["status"] == "failed"

    # 4b: Disabled publishing -> monitor_publish_status='disabled', alert emitted!
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "0")
    alert_spy.reset_mock()
    captured_updates.clear()
    ok_dis, _cf2 = await sched_mod.finalize_task_run(
        _FinConn(),
        lock_conn=object(),
        claimed=claimed_run,
        outcome=outcome,
        evaluation=evaluation,
        docker_client=None,
        container_id=None,
        path_map=paths,
    )
    assert ok_dis is True
    assert captured_updates[-1]["monitor_publish_status"] == "disabled"
    assert captured_updates[-1]["monitor_publish_schedule_now"] is False
    assert process_spy.await_count == 0
    alert_spy.assert_awaited_once()
    assert alert_spy.await_args.kwargs["status"] == "disabled"


@pytest.mark.asyncio
async def test_all_or_nothing_single_tx_batch_promote_and_per_panel_pm_isolation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """all_or_nothing promotes all panels in ONE tx (rolling back all on error); per_panel PM isolates US/CN."""
    # 1. PM _pm_publish_ok exits 0 when US fails but CN succeeds (per_panel), exits 1 when BOTH fail
    us_fail_cn_ok = {
        "us": {"ok": False, "stale": True, "reason": "yf 503"},
        "cn": {"ok": True, "stale": False},
        "cn_valuation": {"ok": True, "stale": False},
    }
    assert pm_run._pm_publish_ok(
        mode="all", sources=us_fail_cn_ok, compute_ok=True, render_ok=True
    ) is True
    both_fail = {
        "us": {"ok": False, "stale": True, "reason": "yf 503"},
        "cn": {"ok": False, "stale": True, "reason": "ak 502"},
        "cn_valuation": {"ok": True, "stale": False},
    }
    assert pm_run._pm_publish_ok(
        mode="all", sources=both_fail, compute_ok=True, render_ok=True
    ) is False

    # Missing status.json fails closed in check_binding_source_health
    us_binding, _cn_binding = data_job_registry.get_registered_monitor_bindings("pm-market-state")
    ok_missing, err_missing = data_job_registry.check_binding_source_health(tmp_path, us_binding)
    assert ok_missing is False
    assert err_missing is not None and "缺少" in err_missing

    # 2. all_or_nothing calls promote_verified_monitor_batch_tx ONCE for all panels (not per-panel!)
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setenv("DALTON_MONITOR_MULTI_BINDING_PUBLISH_MODE", "all_or_nothing")
    monkeypatch.setattr(monitor_service, "has_capability", lambda _cap: True)
    monkeypatch.setattr(
        monitor_db,
        "list_monitors_by_data_job",
        create_autospec(monitor_db.list_monitors_by_data_job, return_value=[]),
    )

    def _fake_prep(binding, **_kw):
        return {
            "kind": "verified_ready",
            "binding": binding,
            "monitor": None,
            "artifact": _make_artifact(artifact_id=f"art-{binding.slug}", path=f"{binding.slug}/index.html"),
            "entry_path": f"{binding.slug}/index.html",
            "snap": Snapshot(
                entries=[ManifestEntry(f"{binding.slug}/index.html", 10, "entry")],
                left_out=[],
                files=[_make_version_file(f"{binding.slug}/index.html", b"<html>ok</html>")],
                blobs={},
                title=binding.title,
            ),
            "candidate_seq": 2,
            "staged_cut": True,
            "verified": monitor_verify.VerifiedCandidateResult(
                artifact_id=f"art-{binding.slug}",
                candidate_seq=2,
                entry_path=f"{binding.slug}/index.html",
                entry_sha256="a" * 64,
                verified_dependencies=0,
                public_probed=False,
            ),
            "clean_run_id": "run-batch-1",
            "verify_fn": None,
            "share_revision": 0,
            "share_audience": None,
            "monitor_acl_revision": None,
        }

    monkeypatch.setattr(
        monitor_service,
        "_prepare_and_verify_single_binding",
        create_autospec(monitor_service._prepare_and_verify_single_binding, side_effect=_fake_prep),
    )
    single_promote_spy = create_autospec(monitor_db.promote_verified_monitor_version_tx)
    batch_promote_spy = create_autospec(
        monitor_db.promote_verified_monitor_batch_tx,
        return_value=monitor_db.BatchPromoteResult(
            results=[
                monitor_db.PromotedMonitorResult(monitor=_make_monitor(slug="pm-market-state", published_seq=2, last_verified_seq=2), promoted=True),
                monitor_db.PromotedMonitorResult(monitor=_make_monitor(slug="cn-sector-rotation", published_seq=2, last_verified_seq=2), promoted=True),
            ],
            promoted=True,
        ),
    )
    monkeypatch.setattr(monitor_db, "promote_verified_monitor_version_tx", single_promote_spy)
    monkeypatch.setattr(monitor_db, "promote_verified_monitor_batch_tx", batch_promote_spy)

    batch_out = await monitor_service.republish_data_job_monitor(
        job_key="pm-market-state",
        run_id="run-batch-1",
        data_root=tmp_path,
        claim_epoch=3,
        check_superseded=True,
    )
    assert batch_out["status"] == "published"
    assert single_promote_spy.await_count == 0
    batch_promote_spy.assert_awaited_once()
    assert len(batch_promote_spy.await_args.args[0]) == 2
    assert batch_promote_spy.await_args.kwargs["expected_claim_epoch"] == 3
    assert batch_promote_spy.await_args.kwargs["check_superseded"] is True


@pytest.mark.asyncio
async def test_sibling_json_respects_exclusions_root_index_html_and_reviewed_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sibling JSON discovery works for root index.html, reviewed manual publish, and never re-adds excluded files."""
    from types import SimpleNamespace
    from src.server.services.artifacts.manifest import Manifest

    # 1. Root index.html sandbox scan resolves work_dir via contained_listing_path (not failing on '.')
    seen_cmds: list[str] = []

    class _FakeRuntime:
        async def exec(self, cmd: str, timeout: int = 10):
            seen_cmds.append(cmd)
            return SimpleNamespace(
                exit_code=0,
                stdout="/workspace/metrics.json\n/workspace/data/series.json\n",
            )

    fake_sandbox = SimpleNamespace(
        runtime=_FakeRuntime(),
        validate_path=lambda p: p.startswith("/workspace"),
        working_dir="/workspace",
    )
    fake_sandbox_reader = SimpleNamespace(_sandbox=fake_sandbox, _work_dir="/workspace")
    found_root = await monitor_verify._list_sandbox_sibling_json(
        fake_sandbox_reader, "", "data"
    )
    assert seen_cmds and "for d in /workspace /workspace/data;" in seen_cmds[0]
    assert found_root == ["metrics.json", "data/series.json"]

    # 2. _MonitorWalkedSource includes non-excluded JSONs even when reviewed (`expected`) is set,
    #    excludes publisher left_out_files, and preserves them in manifest.left_out!
    class _StubFiles:
        def list_files(self) -> list[str]:
            return ["index.html", "metrics.json", "secret.json", "data/series.json"]

        async def stat(self, paths: list[str]) -> dict[str, tuple[str, int]]:
            return {p: (p, 64) for p in paths}

    class _StubBaseWalk:
        expected = ["index.html"]
        caps = None

        async def manifest(self, _reader: Any, _artifact: Any) -> Manifest:
            return Manifest(
                entries=[ManifestEntry("index.html", 100, "entry")],
                left_out=[],
                byte_total=100,
            )

    art = _make_artifact(path="index.html", left_out_files=("secret.json",))
    reader = Reader(files=_StubFiles(), work_dir="/workspace", previous_dirs=())  # type: ignore[arg-type]
    merged = await monitor_verify.monitor_walked_source(_StubBaseWalk()).manifest(reader, art)
    entry_paths = [e.path for e in merged.entries]
    left_out_paths = [e.path for e in merged.left_out]
    assert "metrics.json" in entry_paths
    assert "data/series.json" in entry_paths
    assert "secret.json" not in entry_paths
    assert "secret.json" in left_out_paths

    # 3. monitor_page_matches_reviewed matches reviewed_page computed without sibling JSONs
    entry_vf = _make_version_file("index.html", b"<html>hello</html>")
    json_vf = _make_version_file("metrics.json", b'{"a":1}', "application/json")
    walked_only_snap = Snapshot(
        entries=[ManifestEntry("index.html", entry_vf.byte_len, "entry")],
        left_out=[],
        files=[entry_vf],
        blobs={},
        title="Hello",
    )
    with_sibling_snap = Snapshot(
        entries=[
            ManifestEntry("index.html", entry_vf.byte_len, "entry"),
            ManifestEntry("metrics.json", json_vf.byte_len, monitor_verify.REASON_SIBLING_JSON, optional=True),
        ],
        left_out=[],
        files=[entry_vf, json_vf],
        blobs={},
        title="Hello",
    )
    assert with_sibling_snap.page_sha256 != walked_only_snap.page_sha256
    assert monitor_verify.monitor_page_matches_reviewed(
        with_sibling_snap, walked_only_snap.page_sha256
    ) is True
    assert monitor_verify.monitor_page_matches_reviewed(
        with_sibling_snap, "0" * 64
    ) is False


@pytest.mark.asyncio
async def test_monitor_data_same_as_of_larger_attempt_seq_backfill_clears_stale_without_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-close backfill at the SAME as_of with richer payload & larger attempt_seq succeeds and clears is_stale."""
    mem = _InMemoryMonitorDataDB()
    monkeypatch.setattr(monitor_db, "get_db_connection", mem.make_conn_ctx())

    t_close = datetime(2026, 11, 27, 7, 0, 0, tzinfo=UTC)
    r1 = await monitor_db.upsert_monitor_data(
        MONITOR_ID, "snapshot", {"close": 3300.0}, as_of=t_close, attempt_seq=10, is_stale=False
    )
    assert r1.is_stale is False

    r2 = await monitor_db.upsert_monitor_data(
        MONITOR_ID, "snapshot", None, as_of=None, attempt_seq=11, is_stale=True, error="net timeout"
    )
    assert r2.is_stale is True and r2.as_of == t_close

    # Same as_of, DIFFERENT payload (backfilled fields), larger attempt_seq=12 -> MUST succeed (no 409) & clear is_stale!
    backfilled = {"close": 3300.0, "volume": 123456, "breadth": 0.62}
    r3 = await monitor_db.upsert_monitor_data(
        MONITOR_ID, "snapshot", backfilled, as_of=t_close, attempt_seq=12, is_stale=False
    )
    assert r3.payload == backfilled
    assert r3.as_of == t_close
    assert r3.attempt_seq == 12
    assert r3.is_stale is False
    assert r3.last_error is None

    # Out-of-order late packet with smaller attempt_seq=11 and different payload at same as_of is still 409!
    with pytest.raises(HTTPException) as exc_409:
        await monitor_db.upsert_monitor_data(
            MONITOR_ID, "snapshot", {"close": 3299.0}, as_of=t_close, attempt_seq=11, is_stale=False
        )
    assert exc_409.value.status_code == 409


@pytest.mark.asyncio
async def test_backend_ops_jobs_reads_monitor_publish_from_existing_db_without_extra_dsn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend _fetch_ops_jobs reads ops-monitor-publish directly from get_db_connection with no extra DSN env."""
    from src.dalton.autonomy import system_jobs as sj_mod

    monkeypatch.setenv("APP_ENV", "everflow")
    monkeypatch.delenv("DALTON_OPS_MONITOR_PG_DSN", raising=False)
    monkeypatch.delenv("DALTON_OPS_JOBS_VIA_BRIDGE", raising=False)
    status_file = tmp_path / "ops_jobs_status.json"
    status_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": datetime.now(UTC).isoformat(),
                "ttl_seconds": 300,
                "jobs": [
                    {"key": "ops-db-backup", "kind": "ops_db_backup", "status": "on", "last_result": "成功"},
                    {"key": "ops-backend-watchdog", "kind": "ops_backend_watchdog", "status": "on", "last_result": "健康"},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("DALTON_OPS_STATUS_FILE", str(status_file))

    fake_rows = [
        {
            "run_id": "33333333-3333-4333-8333-333333333333",
            "job_key": "pm-market-state",
            "monitor_publish_status": "disabled",
            "completed_at": NOW,
            "last_error": "DALTON_ARTIFACT_PUBLISHING_ENABLED 未开启",
        },
        {
            "run_id": "44444444-4444-4444-8444-444444444444",
            "job_key": "cn-intraday",
            "monitor_publish_status": "superseded",
            "completed_at": NOW,
            "last_error": None,
        },
    ]

    class _OpsCur:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _sql: str, _params: Any = None) -> None:
            return None

        async def fetchall(self):
            return list(fake_rows)

    class _OpsConn:
        def cursor(self, **_kw):
            return _OpsCur()

    @asynccontextmanager
    async def _ops_db():
        yield _OpsConn()

    monkeypatch.setattr(sj_mod, "get_db_connection", _ops_db)

    jobs = await sj_mod._fetch_ops_jobs()
    by_key = {j["key"]: j for j in jobs}
    assert "ops-monitor-publish" in by_key
    mon_job = by_key["ops-monitor-publish"]
    assert mon_job["status"] == "partial"
    assert mon_job["issue_count"] == 1
    assert mon_job["issues"][0]["job_key"] == "pm-market-state"
    assert mon_job["issues"][0]["monitor_publish_status"] == "disabled"


def test_rolling_upgrade_effective_seq_and_d06y_alembic_chain_from_d060(
    tmp_path: Path,
) -> None:
    """effective_seq never regresses during rolling upgrade; d061 forms a single unbroken head from d060."""
    import subprocess
    import sys
    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory

    # Rolling upgrade: old worker advanced published_seq=6 while d061 backfill left last_verified_seq=5
    rolling_mon = _make_monitor(published_seq=6, last_verified_seq=5)
    assert rolling_mon.effective_seq == 6

    # Normal verified state
    normal_mon = _make_monitor(published_seq=2, last_verified_seq=2)
    assert normal_mon.effective_seq == 2

    # Zero shell row stays 0
    shell_mon = _make_monitor(published_seq=0, last_verified_seq=None)
    assert shell_mon.effective_seq == 0

    assert d06y_mig.revision == "d061"
    assert d06y_mig.down_revision == "d060"

    repo_root = Path(__file__).resolve().parents[3]
    real_migrations = repo_root / "src/dalton/migrations"
    # The real d059/d060 files are on this branch (D4 merged); validate the actual chain.
    assert any((real_migrations / "versions").glob("d060*.py"))
    alembic_ini = repo_root / "src/dalton/alembic.ini"
    script_loc = real_migrations

    cfg = AlembicConfig(str(alembic_ini))
    cfg.set_main_option("script_location", str(script_loc))
    script_dir = ScriptDirectory.from_config(cfg)
    assert script_dir.get_heads() == ["d062"]
    rev = script_dir.get_revision("d061")
    assert rev is not None and rev.down_revision == "d060"

    up_sql = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "upgrade", "d060:d061", "--sql"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "last_verified_seq" in up_sql.stdout
    assert "monitor_publish_claim_epoch" in up_sql.stdout
    assert "attempt_seq" in up_sql.stdout

    down_sql = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(alembic_ini), "downgrade", "d061:d060", "--sql"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert "DROP COLUMN IF EXISTS last_verified_seq" in down_sql.stdout
    assert "DROP COLUMN IF EXISTS monitor_publish_status" in down_sql.stdout


# ---------------------------------------------------------------------------
# Round 3 fixes: JS static import recursion, visitor /v/{seq}/ paths, empty entry,
# lock-ordered supersede & left_out_files, legacy_migrate verification, stale banner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_candidate_recursively_checks_js_static_imports_with_depth_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JS module static relative import/export chains are recursively verified within candidate_seq up to depth cap."""
    html_bytes = (
        b'<!doctype html><html><head>'
        b'<script type="module" src="js/main.js"></script>'
        b"</head><body>Module App</body></html>"
    )
    main_js = (
        b"// import './commented-out-missing.js';\n"
        b"/* export * from './block-comment-missing.js'; */\n"
        b"import React from 'react';\n"
        b"const lazy = () => import('./lazy-dynamic-not-static.js');\n"
        b"import { init } from './sub/a.js';\n"
        b"import './side.mjs';\n"
    )
    a_js = b"export * from '../shared/b.js';\n"
    b_js = b"import './c.js';\nimport '../sub/a.js';\n"  # cycle back to a.js + next hop c.js
    side_mjs = b"console.log('side');\n"
    c_js = b"export const C = 1;\n"

    entry_vf = _make_version_file("index.html", html_bytes, "text/html; charset=utf-8")
    main_vf = _make_version_file("js/main.js", main_js, "application/javascript")
    a_vf = _make_version_file("js/sub/a.js", a_js, "application/javascript")
    b_vf = _make_version_file("js/shared/b.js", b_js, "application/javascript")
    side_vf = _make_version_file("js/side.mjs", side_mjs, "application/javascript")
    c_vf = _make_version_file("js/shared/c.js", c_js, "application/javascript")

    # Missing js/shared/c.js (depth 4: index -> main(1) -> a(2) -> b(3) -> c(4))
    stored_map: dict[tuple[str, int, str], StoredVersionFile] = {
        (ARTIFACT_ID, 2, "index.html"): _make_stored_file(entry_vf),
        (ARTIFACT_ID, 2, "js/main.js"): _make_stored_file(main_vf),
        (ARTIFACT_ID, 2, "js/sub/a.js"): _make_stored_file(a_vf),
        (ARTIFACT_ID, 2, "js/shared/b.js"): _make_stored_file(b_vf),
        (ARTIFACT_ID, 2, "js/side.mjs"): _make_stored_file(side_vf),
    }

    async def _get_vf(aid: str, seq: int, path: str) -> StoredVersionFile | None:
        return stored_map.get((aid, seq, path))

    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(artifacts_db.get_version_file, side_effect=_get_vf),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(
            monitor_db.get_monitor_by_slug,
            return_value=_make_monitor(published_seq=1, last_verified_seq=1),
        ),
    )

    # Default depth cap (8) catches missing transitive static import js/shared/c.js
    with pytest.raises(monitor_verify.MonitorVerificationError, match="js/shared/c.js"):
        await monitor_verify.verify_candidate_monitor_version(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            entry_path="index.html",
            snap_files=[entry_vf],
            snap_entry_sha256=entry_vf.sha256,
            visibility="public",
            slug="macro-pulse",
        )

    # Depth cap (max_dep_depth=3) stops at depth 3 (b.js) without recursing to depth 4 (c.js)
    capped = await monitor_verify.verify_candidate_monitor_version(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        entry_path="index.html",
        snap_files=[entry_vf],
        snap_entry_sha256=entry_vf.sha256,
        visibility="public",
        slug="macro-pulse",
        max_dep_depth=3,
    )
    assert "js/shared/b.js" in capped.probed_assets
    assert "js/shared/c.js" not in capped.probed_assets

    # Adding c.js makes full depth verification + cycle handling pass & probe all modules
    stored_map[(ARTIFACT_ID, 2, "js/shared/c.js")] = _make_stored_file(c_vf)
    ok = await monitor_verify.verify_candidate_monitor_version(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        entry_path="index.html",
        snap_files=[entry_vf],
        snap_entry_sha256=entry_vf.sha256,
        visibility="public",
        slug="macro-pulse",
    )
    assert ok.public_probed is True
    assert {"js/main.js", "js/sub/a.js", "js/shared/b.js", "js/side.mjs", "js/shared/c.js"} <= set(
        ok.probed_assets
    )


@pytest.mark.asyncio
async def test_verify_candidate_resolves_visitor_v_seq_paths_and_rejects_escape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public verification resolves relative refs against visitor /v/{seq}/ and rejects parent escapes."""
    shared_vf = _make_version_file("shared.js", b"export const S = 1;", "application/javascript")
    escape_html = b'<!doctype html><html><head><script src="../shared.js"></script></head><body>Esc</body></html>'
    escape_entry = _make_version_file("panel/index.html", escape_html, "text/html; charset=utf-8")

    stored_escape: dict[tuple[str, int, str], StoredVersionFile] = {
        (ARTIFACT_ID, 2, "panel/index.html"): _make_stored_file(escape_entry),
        (ARTIFACT_ID, 2, "shared.js"): _make_stored_file(shared_vf),
    }

    async def _get_escape(aid: str, seq: int, path: str) -> StoredVersionFile | None:
        return stored_escape.get((aid, seq, path))

    orig_get_version_file = artifacts_db.get_version_file
    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(orig_get_version_file, side_effect=_get_escape),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(
            monitor_db.get_monitor_by_slug,
            return_value=_make_monitor(entry_path="panel/index.html", published_seq=1, last_verified_seq=1),
        ),
    )

    # Public iframe loads /v/2/, so ../shared.js escapes version root -> MUST FAIL even though shared.js is stored!
    with pytest.raises(monitor_verify.MonitorVerificationError, match="outside version root"):
        await monitor_verify.verify_candidate_monitor_version(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            entry_path="panel/index.html",
            snap_files=[escape_entry, shared_vf],
            snap_entry_sha256=escape_entry.sha256,
            visibility="public",
            slug="macro-pulse",
        )

    # Authenticated (company) iframe loads /v/2/panel/index.html, so ../shared.js resolves to shared.js inside /v/2/ -> OK!
    company_ok = await monitor_verify.verify_candidate_monitor_version(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        entry_path="panel/index.html",
        snap_files=[escape_entry, shared_vf],
        snap_entry_sha256=escape_entry.sha256,
        visibility="company",
        slug="macro-pulse",
    )
    assert company_ok.public_probed is False

    # Valid subdirectory public monitor: entry at panel/index.html loads js/main.js -> imports ../util.js (stays in /v/2/)
    good_html = b'<!doctype html><html><head><script type="module" src="js/main.js"></script></head><body>Sub OK</body></html>'
    good_entry = _make_version_file("panel/index.html", good_html, "text/html; charset=utf-8")
    good_main = _make_version_file("panel/js/main.js", b"import '../util.js';\n", "application/javascript")
    good_util = _make_version_file("panel/util.js", b"export const U = 1;\n", "application/javascript")
    stored_good = {
        (ARTIFACT_ID, 2, "panel/index.html"): _make_stored_file(good_entry),
        (ARTIFACT_ID, 2, "panel/js/main.js"): _make_stored_file(good_main),
        (ARTIFACT_ID, 2, "panel/util.js"): _make_stored_file(good_util),
    }

    async def _get_good(aid: str, seq: int, path: str) -> StoredVersionFile | None:
        return stored_good.get((aid, seq, path))

    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(orig_get_version_file, side_effect=_get_good),
    )

    probed_urls: list[str] = []
    real_get = monitor_verify._default_asgi_get

    async def _recording_get(path: str, *, headers: dict[str, str] | None = None):
        probed_urls.append(path)
        return await real_get(path, headers=headers)

    good_res = await monitor_verify.verify_candidate_monitor_version(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        entry_path="panel/index.html",
        snap_files=[good_entry, good_main, good_util],
        snap_entry_sha256=good_entry.sha256,
        visibility="public",
        slug="macro-pulse",
        probe_get=_recording_get,
    )
    assert good_res.public_probed is True
    assert "/api/v1/dalton/monitors/public/macro-pulse/v/2/" in probed_urls
    assert "/api/v1/dalton/monitors/public/macro-pulse/v/2/js/main.js" in probed_urls
    assert "/api/v1/dalton/monitors/public/macro-pulse/v/2/util.js" in probed_urls
    assert set(good_res.probed_assets) == {"js/main.js", "util.js"}

    # JS module escaping version root (import '../../outside.js' from /v/2/js/main.js) MUST FAIL
    bad_main = _make_version_file("panel/js/main.js", b"import '../../outside.js';\n", "application/javascript")
    stored_good[(ARTIFACT_ID, 2, "panel/js/main.js")] = _make_stored_file(bad_main)
    with pytest.raises(monitor_verify.MonitorVerificationError, match="outside version root"):
        await monitor_verify.verify_candidate_monitor_version(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            entry_path="panel/index.html",
            snap_files=[good_entry, bad_main, good_util],
            snap_entry_sha256=good_entry.sha256,
            visibility="public",
            slug="macro-pulse",
        )


@pytest.mark.asyncio
async def test_verify_candidate_rejects_empty_and_whitespace_entry_before_bridge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Zero-byte and whitespace-only HTML candidates MUST fail BEFORE with_bridge runs."""
    bridge_spy = create_autospec(monitor_verify.with_bridge, wraps=monitor_verify.with_bridge)
    monkeypatch.setattr(monitor_verify, "with_bridge", bridge_spy)
    orig_get_version_file = artifacts_db.get_version_file

    for raw in (b"", b"   \n\t  "):
        empty_vf = _make_version_file("index.html", raw, "text/html; charset=utf-8")
        monkeypatch.setattr(
            artifacts_db,
            "get_version_file",
            create_autospec(orig_get_version_file, return_value=_make_stored_file(empty_vf)),
        )
        with pytest.raises(monitor_verify.MonitorVerificationError, match="empty"):
            await monitor_verify.verify_candidate_monitor_version(
                artifact_id=ARTIFACT_ID,
                candidate_seq=2,
                entry_path="index.html",
                snap_files=[empty_vf],
                snap_entry_sha256=empty_vf.sha256,
                visibility="public",
                slug="macro-pulse",
            )
    assert bridge_spy.call_count == 0


@pytest.mark.asyncio
async def test_promote_checks_left_out_and_superseded_after_artifact_lock_without_bumping_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Artifact lock precedes cross-run supersede & left_out checks; set_left_out does NOT bump revision."""
    from src.server.database.artifacts import records as records_mod
    from src.server.database.artifacts.versions import LeftOutChanged

    # 1. Verify set_left_out SQL does NOT touch share_links.revision (preserving open /a/{code} grants)
    captured_assignments: list[str] = []

    async def _fake_set_record(*, user_id: str, artifact_id: str, assignments: str, params: dict[str, Any]):
        captured_assignments.append(assignments)
        return _make_artifact(revision=2, left_out_files=("data/secret.json",))

    monkeypatch.setattr(
        records_mod,
        "set_record",
        create_autospec(records_mod.set_record, side_effect=_fake_set_record),
    )
    await records_mod.set_left_out(
        user_id="admin-1",
        artifact_id=ARTIFACT_ID,
        path="data/secret.json",
        left_out=True,
        cap=50,
    )
    assert len(captured_assignments) == 1
    assert "revision" not in captured_assignments[0]

    # 2. Verify promote_verified_monitor_version_tx acquires lock_owned BEFORE checking run fences,
    #    and rejects concurrent left_out_files drift / excluded file overlap with LeftOutChanged!
    events: list[str] = []
    state: dict[str, Any] = {
        "artifact": _make_artifact(revision=5, left_out_files=("data/secret.json",)),
        "run_row": {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "job_key": "cn-intraday",
            "status": "succeeded",
            "scheduled_slot_utc": NOW,
            "completed_at": NOW,
            "monitor_publish_status": "publishing",
            "monitor_publish_claim_epoch": 1,
        },
        "newer_rows": [],
    }

    class _OrderCur:
        def __init__(self) -> None:
            self._one: Any = None
            self._many: list[Any] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql: str, _params: Any = None) -> None:
            q = " ".join(sql.split())
            if "FROM dalton_data_task_runs WHERE run_id =" in q:
                events.append("run_lock")
                self._one = dict(state["run_row"])
            elif "FROM dalton_data_task_runs WHERE job_key =" in q:
                assert "FOR SHARE" in q
                events.append("superseded_for_share")
                self._many = list(state["newer_rows"])
            else:
                self._one = None

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._many)

    class _OrderTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _OrderConn:
        def transaction(self):
            return _OrderTx()

        def cursor(self, **_kw):
            return _OrderCur()

    @asynccontextmanager
    async def _conn_ctx():
        yield _OrderConn()

    def _lock_owned(_cur, **_kw):
        events.append("artifact_lock")
        return state["artifact"]

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        monitor_db.artifacts_records_db,
        "lock_owned",
        create_autospec(monitor_db.artifacts_records_db.lock_owned, side_effect=_lock_owned),
    )

    with pytest.raises(LeftOutChanged):
        await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            workspace_owner_id="admin-1",
            paths=["index.html", "data/secret.json"],
            monitor_id=MONITOR_ID,
            run_id="11111111-1111-4111-8111-111111111111",
            expected_claim_epoch=1,
            check_superseded=True,
            expected_share_revision=5,
            expected_left_out_files=(),  # DB now has data/secret.json excluded!
        )
    assert events == ["artifact_lock", "run_lock", "superseded_for_share"]

    # Even if expected_left_out_files wasn't passed, overlap between candidate paths and locked.left_out_files fails!
    events.clear()
    with pytest.raises(LeftOutChanged, match="data/secret.json"):
        await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            workspace_owner_id="admin-1",
            paths=["index.html", "data/secret.json"],
            monitor_id=MONITOR_ID,
            expected_share_revision=5,
        )
    assert events == ["artifact_lock"]


@pytest.mark.asyncio
async def test_legacy_index_html_migration_runs_stage_verify_and_fenced_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy bare index.html data-job auto-migration MUST go through Stage 1 -> Stage 2 verify -> Stage 3 fenced promote."""
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setenv("DALTON_MONITOR_VERIFY_READBACK_ENABLED", "1")
    monkeypatch.setattr(monitor_service, "has_capability", lambda _cap: True)

    panel_dir = tmp_path / "research-outputs" / "cn-intraday"
    panel_dir.mkdir(parents=True)
    (panel_dir / "index.html").write_text("<html><body>Migrated V2</body></html>", encoding="utf-8")

    legacy_mon = _make_monitor(
        slug="cn-intraday",
        owner_user_id=COMPANY_SYSTEM_USER_ID,
        source_kind="data_job",
        source_ref="cn-intraday",
        entry_path="index.html",
        visibility="company",
        published_seq=1,
        last_verified_seq=1,
    )
    legacy_art = _make_artifact(path="index.html", latest_seq=1, revision=2)
    snap = Snapshot(
        entries=[ManifestEntry("cn-intraday/index.html", 36, "entry")],
        left_out=[],
        files=[_make_version_file("cn-intraday/index.html", b"<html><body>Migrated V2</body></html>")],
        blobs={},
        title="Migrated",
    )

    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_data_job",
        create_autospec(monitor_db.get_monitor_by_data_job, return_value=legacy_mon),
    )
    monkeypatch.setattr(
        monitor_db,
        "list_monitors_by_data_job",
        create_autospec(monitor_db.list_monitors_by_data_job, return_value=[legacy_mon]),
    )
    monkeypatch.setattr(
        monitor_db,
        "has_processed_monitor_run",
        create_autospec(monitor_db.has_processed_monitor_run, return_value=False),
    )
    monkeypatch.setattr(
        monitor_db,
        "ensure_company_monitor_workspace",
        create_autospec(monitor_db.ensure_company_monitor_workspace, return_value=WS_ID),
    )
    monkeypatch.setattr(
        artifacts_db,
        "get_artifact",
        create_autospec(artifacts_db.get_artifact, return_value=legacy_art),
    )
    monkeypatch.setattr(
        artifact_snapshot,
        "capture_reader_monitor_snapshot",
        create_autospec(artifact_snapshot.capture_reader_monitor_snapshot, return_value=snap),
    )
    monkeypatch.setattr(
        monitor_service,
        "_abort_if_absent_dependencies",
        create_autospec(monitor_service._abort_if_absent_dependencies, return_value=None),
    )
    monkeypatch.setattr(
        monitor_db,
        "record_monitor_publish_failure",
        create_autospec(monitor_db.record_monitor_publish_failure, return_value=legacy_mon),
    )

    stage_spy = create_autospec(
        monitor_db.stage_monitor_candidate_version_tx,
        return_value=monitor_db.StagedMonitorCandidate(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            cut=True,
            existing_monitor=legacy_mon,
            share_revision=2,
            share_audience=None,
            monitor_acl_revision=1,
            left_out_files=(),
        ),
    )
    verify_spy = create_autospec(
        monitor_verify.verify_candidate_monitor_version,
        return_value=monitor_verify.VerifiedCandidateResult(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            entry_path="cn-intraday/index.html",
            entry_sha256=snap.entry_sha256,
            verified_dependencies=0,
            public_probed=False,
        ),
    )
    migrated_mon = _make_monitor(
        slug="cn-intraday",
        owner_user_id=COMPANY_SYSTEM_USER_ID,
        source_kind="data_job",
        source_ref="cn-intraday",
        entry_path="cn-intraday/index.html",
        visibility="company",
        published_seq=2,
        last_verified_seq=2,
    )
    promote_spy = create_autospec(
        monitor_db.promote_verified_monitor_version_tx,
        return_value=monitor_db.PromotedMonitorResult(monitor=migrated_mon, promoted=True),
    )
    monkeypatch.setattr(monitor_db, "stage_monitor_candidate_version_tx", stage_spy)
    monkeypatch.setattr(monitor_verify, "verify_candidate_monitor_version", verify_spy)
    monkeypatch.setattr(monitor_db, "promote_verified_monitor_version_tx", promote_spy)

    # Case A: Happy path passes candidate_entry_path to Stage 1, verifies Stage 2, and passes fences + migrate_entry_path to Stage 3
    res = await monitor_service.republish_data_job_monitor(
        job_key="cn-intraday",
        run_id="run-legacy-1",
        data_root=tmp_path,
        claim_epoch=7,
        check_superseded=True,
    )
    assert res["status"] == "published"
    stage_spy.assert_awaited_once()
    assert stage_spy.await_args.kwargs["candidate_entry_path"] == "cn-intraday/index.html"
    assert stage_spy.await_args.kwargs["expected_legacy_entry_path"] == "index.html"
    verify_spy.assert_awaited_once()
    assert verify_spy.await_args.kwargs["entry_path"] == "cn-intraday/index.html"
    promote_spy.assert_awaited_once()
    assert promote_spy.await_args.kwargs["expected_claim_epoch"] == 7
    assert promote_spy.await_args.kwargs["check_superseded"] is True
    assert promote_spy.await_args.kwargs["migrate_entry_path"] == "cn-intraday/index.html"
    assert promote_spy.await_args.kwargs["migrate_legacy_entry_path"] == "index.html"

    # Case B: Stage 2 verification failure aborts legacy migration BEFORE promote (share_links.path never renames)
    promote_spy.reset_mock()
    verify_spy.side_effect = monitor_verify.MonitorVerificationError("missing dep")
    with pytest.raises(HTTPException):
        await monitor_service.republish_data_job_monitor(
            job_key="cn-intraday",
            run_id="run-legacy-2",
            data_root=tmp_path,
            claim_epoch=8,
            check_superseded=True,
        )
    assert promote_spy.await_count == 0


def test_monitor_feeds_stale_banner_persists_across_local_timer_and_304_until_server_recovers() -> None:
    """Server is_stale banner stays visible across 15s local freshness timer & 304 until server reports is_stale=false."""
    import shutil
    import subprocess

    js_snippet = feeds_common.build_grant_poller_js(
        data_key="snapshot",
        on_data_fn="onData",
        on_error_fn="onError",
        on_stale_fn="onStale",
        market="cn",
    )
    assert "serverStaleMsg" in js_snippet

    node_bin = shutil.which("node")
    if node_bin is None:
        pytest.skip("node binary unavailable in environment")

    harness = f"""
const staleCalls = [];
const dataCalls = [];
const intervals = [];
global.window = {{
  __DALTON_MONITOR__: {{ dataBase: '/api/v1/dalton/monitors/public/cn-intraday/data' }},
  addEventListener: () => {{}},
  onStale: (msg) => staleCalls.push(msg),
}};
global.onData = (b) => dataCalls.push(b);
global.onError = (e) => {{ throw new Error('unexpected error: ' + e); }};
global.location = {{ pathname: '/api/v1/dalton/monitors/public/cn-intraday/v/1/' }};
global.setInterval = (fn, ms) => {{ intervals.push({{ fn, ms }}); return intervals.length; }};
global.clearTimeout = () => {{}};

let nextResp = null;
global.fetch = async () => nextResp;

// Step 1: initial response is server is_stale=true with a fresh timestamp (would clear under local timer alone!)
const nowIso = new Date().toISOString();
nextResp = {{
  status: 200,
  ok: true,
  headers: {{ get: (k) => (k === 'ETag' ? '"e1"' : null) }},
  json: async () => ({{ is_stale: true, last_error: 'upstream timeout', updated_at: nowIso, as_of: nowIso, data: {{ as_of: nowIso, is_trading_day: false }} }}),
}};

{js_snippet}

(async () => {{
  // Wait for initial pollOnce() microtasks
  await new Promise((r) => setTimeout(r, 20));
  const afterStale = staleCalls[staleCalls.length - 1];
  if (!afterStale || !afterStale.includes('upstream timeout')) {{
    throw new Error('Expected stale banner after is_stale=true, got: ' + JSON.stringify(staleCalls));
  }}

  // Step 2: 15s local freshness timer runs (is_trading_day=false + fresh timestamp) -> MUST NOT clear banner!
  const localRet = window.__DALTON_CHECK_FRESHNESS__();
  if (localRet !== true || staleCalls[staleCalls.length - 1] !== afterStale) {{
    throw new Error('Local timer cleared server stale banner: ' + JSON.stringify({{ localRet, staleCalls }}));
  }}

  // Step 3: Next poll returns 304 Not Modified -> MUST NOT clear banner!
  nextResp = {{ status: 304, ok: false, headers: {{ get: () => null }} }};
  await window.__DALTON_POLL_NOW__();
  if (staleCalls[staleCalls.length - 1] !== afterStale) {{
    throw new Error('304 response cleared server stale banner: ' + JSON.stringify(staleCalls));
  }}

  // Step 4: Server recovers with is_stale=false -> banner clears (onStale(null))!
  nextResp = {{
    status: 200,
    ok: true,
    headers: {{ get: (k) => (k === 'ETag' ? '"e2"' : null) }},
    json: async () => ({{ is_stale: false, updated_at: nowIso, as_of: nowIso, data: {{ as_of: nowIso, is_trading_day: false }} }}),
  }};
  await window.__DALTON_POLL_NOW__();
  if (staleCalls[staleCalls.length - 1] !== null) {{
    throw new Error('Expected banner cleared after is_stale=false, got: ' + JSON.stringify(staleCalls));
  }}
  console.log('STALE_BANNER_OK');
}})().catch((err) => {{
  console.error(err);
  process.exit(1);
}});
"""
    proc = subprocess.run(
        [node_bin, "-e", harness],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert proc.returncode == 0, f"JS stale banner test failed:\nSTDOUT: {proc.stdout}\nSTDERR: {proc.stderr}"
    assert "STALE_BANNER_OK" in proc.stdout


# ---------------------------------------------------------------------------
# Round 4 fixes: minified JS imports, percent-encoded paths, running newer run
# cross-round fence, claim_epoch-fenced last_publish_error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "minified_js",
    [
        b'import{render}from"./x.js";render();',
        b'import"./x.js";',
        b'export*from"./x.js";',
        b"export{a}from'./x.js';",
    ],
)
async def test_verify_candidate_extracts_minified_js_imports_and_fails_on_missing_modules(
    minified_js: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Minified static import/export forms without spaces must be extracted and fail when missing."""
    assert monitor_verify._extract_js_static_imports(minified_js) == ["./x.js"]

    html_bytes = (
        b'<!doctype html><html><head>'
        b'<script type="module" src="js/main.js"></script>'
        b"</head><body>Minified</body></html>"
    )
    entry_vf = _make_version_file("index.html", html_bytes, "text/html; charset=utf-8")
    main_vf = _make_version_file("js/main.js", minified_js, "application/javascript")
    x_vf = _make_version_file("js/x.js", b"export const a=1;export function render(){}", "application/javascript")

    stored_map: dict[tuple[str, int, str], StoredVersionFile] = {
        (ARTIFACT_ID, 2, "index.html"): _make_stored_file(entry_vf),
        (ARTIFACT_ID, 2, "js/main.js"): _make_stored_file(main_vf),
    }

    async def _get_vf(aid: str, seq: int, path: str) -> StoredVersionFile | None:
        return stored_map.get((aid, seq, path))

    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(artifacts_db.get_version_file, side_effect=_get_vf),
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_slug",
        create_autospec(
            monitor_db.get_monitor_by_slug,
            return_value=_make_monitor(published_seq=1, last_verified_seq=1),
        ),
    )

    with pytest.raises(monitor_verify.MonitorVerificationError, match="js/x.js"):
        await monitor_verify.verify_candidate_monitor_version(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            entry_path="index.html",
            snap_files=[entry_vf, main_vf],
            snap_entry_sha256=entry_vf.sha256,
            visibility="public",
            slug="macro-pulse",
        )

    stored_map[(ARTIFACT_ID, 2, "js/x.js")] = _make_stored_file(x_vf)
    ok = await monitor_verify.verify_candidate_monitor_version(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        entry_path="index.html",
        snap_files=[entry_vf, main_vf, x_vf],
        snap_entry_sha256=entry_vf.sha256,
        visibility="public",
        slug="macro-pulse",
    )
    assert ok.public_probed is True
    assert {"js/main.js", "js/x.js"} <= set(ok.probed_assets)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_ref",
    [
        "./assets/..%2Fapp.js",
        "./assets/..%2fapp.js",
        "./assets/..%5Capp.js",
        "./assets/..%5capp.js",
        "./assets/%2E%2E/app.js",
        "./assets/%2e%2e/app.js",
    ],
)
async def test_verify_candidate_rejects_percent_encoded_path_separators_without_folding(
    bad_ref: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Percent-encoded %2F/%5C/%2E in asset references MUST fail verification directly without folding."""
    html_bytes = (
        f'<!doctype html><html><head><script src="{bad_ref}"></script></head>'
        f"<body>Encoded</body></html>"
    ).encode("utf-8")
    entry_vf = _make_version_file("index.html", html_bytes, "text/html; charset=utf-8")
    app_vf = _make_version_file("app.js", b"console.log('ok');", "application/javascript")

    stored_map: dict[tuple[str, int, str], StoredVersionFile] = {
        (ARTIFACT_ID, 2, "index.html"): _make_stored_file(entry_vf),
        (ARTIFACT_ID, 2, "app.js"): _make_stored_file(app_vf),
    }

    async def _get_vf(aid: str, seq: int, path: str) -> StoredVersionFile | None:
        return stored_map.get((aid, seq, path))

    monkeypatch.setattr(
        artifacts_db,
        "get_version_file",
        create_autospec(artifacts_db.get_version_file, side_effect=_get_vf),
    )

    with pytest.raises(monitor_verify.MonitorVerificationError, match="outside version root"):
        await monitor_verify.verify_candidate_monitor_version(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            entry_path="index.html",
            snap_files=[entry_vf, app_vf],
            snap_entry_sha256=entry_vf.sha256,
            visibility="public",
            slug="macro-pulse",
        )


@pytest.mark.asyncio
async def test_promote_aborts_as_superseded_when_newer_run_is_still_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """During promotion, any newer run for the same job_key not failed/cancelled (including running) supersedes older replay."""
    state: dict[str, Any] = {
        "run_row": {
            "run_id": "11111111-1111-4111-8111-111111111111",
            "job_key": "cn-intraday",
            "status": "succeeded",
            "scheduled_slot_utc": NOW,
            "completed_at": NOW,
            "monitor_publish_status": "publishing",
            "monitor_publish_claim_epoch": 1,
        },
        "newer_rows": [
            {
                "run_id": "22222222-2222-4222-8222-222222222222",
                "status": "running",
                "monitor_publish_status": "not_applicable",
            }
        ],
        "captured_sql": [],
        "writes": [],
    }

    class _RaceCur:
        def __init__(self) -> None:
            self._one: Any = None
            self._many: list[Any] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql: str, _params: Any = None) -> None:
            q = " ".join(sql.split())
            state["captured_sql"].append(q)
            if "FROM dalton_data_task_runs WHERE run_id =" in q:
                self._one = dict(state["run_row"])
            elif "FROM dalton_data_task_runs WHERE job_key =" in q:
                assert "FOR SHARE" in q
                assert "status NOT IN (" in q
                assert "'failed'" in q and "'cancelled'" in q
                self._many = list(state["newer_rows"])
            elif q.startswith(("UPDATE", "INSERT", "DELETE")):
                state["writes"].append(q)
                self._one = None
            else:
                self._one = None

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._many)

    class _RaceTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _RaceConn:
        def transaction(self):
            return _RaceTx()

        def cursor(self, **_kw):
            return _RaceCur()

    @asynccontextmanager
    async def _conn_ctx():
        yield _RaceConn()

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        monitor_db.artifacts_records_db,
        "lock_owned",
        create_autospec(
            monitor_db.artifacts_records_db.lock_owned,
            return_value=_make_artifact(revision=5, latest_seq=2),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "_select_monitor_in_cur",
        create_autospec(monitor_db._select_monitor_in_cur, return_value=_make_monitor()),
    )

    # 1. Newer run still running -> old replay MUST NOT advance pointer and MUST NOT terminally supersede!
    res_running = await monitor_db.promote_verified_monitor_version_tx(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        paths=["index.html"],
        monitor_id=MONITOR_ID,
        run_id="11111111-1111-4111-8111-111111111111",
        expected_claim_epoch=1,
        check_superseded=True,
    )
    assert res_running.deferred_to_newer is True
    assert res_running.superseded_by_newer is False
    assert res_running.promoted is False
    assert state["writes"] == []

    # 1b. Newer run already terminally published -> old replay IS terminally superseded!
    state["newer_rows"] = [
        {
            "run_id": "22222222-2222-4222-8222-222222222222",
            "status": "succeeded",
            "monitor_publish_status": "published",
        }
    ]
    res_published = await monitor_db.promote_verified_monitor_version_tx(
        artifact_id=ARTIFACT_ID,
        candidate_seq=2,
        workspace_owner_id=COMPANY_SYSTEM_USER_ID,
        paths=["index.html"],
        monitor_id=MONITOR_ID,
        run_id="11111111-1111-4111-8111-111111111111",
        expected_claim_epoch=1,
        check_superseded=True,
    )
    assert res_published.superseded_by_newer is True
    assert res_published.deferred_to_newer is False
    assert res_published.promoted is False
    assert state["writes"] == []

    # 2. Classifier distinguishes deferred (in-flight) vs superseded (terminal) vs none (failed/cancelled)
    assert monitor_db._classify_newer_data_job_run(
        {"status": "queued", "monitor_publish_status": "not_applicable"}
    ) == "deferred"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "starting", "monitor_publish_status": "not_applicable"}
    ) == "deferred"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "running", "monitor_publish_status": "not_applicable"}
    ) == "deferred"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "pending"}
    ) == "deferred"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "retrying"}
    ) == "deferred"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "publishing"}
    ) == "deferred"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "published"}
    ) == "superseded"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "unchanged"}
    ) == "superseded"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "partial_failed"}
    ) == "superseded"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "failed"}
    ) == "none"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "disabled"}
    ) == "none"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "failed", "monitor_publish_status": "not_applicable"}
    ) == "none"
    assert monitor_db._classify_newer_data_job_run(
        {"status": "cancelled", "monitor_publish_status": "not_applicable"}
    ) == "none"
    assert monitor_db._is_blocking_newer_data_job_run(
        {"status": "running", "monitor_publish_status": "not_applicable"}
    ) is True
    assert monitor_db._is_blocking_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "publishing"}
    ) is True
    assert monitor_db._is_blocking_newer_data_job_run(
        {"status": "succeeded", "monitor_publish_status": "pending"}
    ) is True
    assert monitor_db._is_blocking_newer_data_job_run(
        {"status": "failed", "monitor_publish_status": "not_applicable"}
    ) is False
    assert monitor_db._is_blocking_newer_data_job_run(
        {"status": "cancelled", "monitor_publish_status": "not_applicable"}
    ) is False


@pytest.mark.asyncio
async def test_record_monitor_publish_failure_fenced_by_claim_epoch_blocks_late_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Late worker error write is fenced by claim_epoch so taken-over/settled runs are not overwritten."""
    run_id = "33333333-3333-4333-8333-333333333333"
    db_state: dict[str, Any] = {
        "run_row": {
            "monitor_publish_status": "publishing",
            "monitor_publish_claim_epoch": 2,  # Worker 2 took over!
        },
        "last_publish_error": None,
        "updates": [],
    }

    class _ErrCur:
        def __init__(self) -> None:
            self._one: Any = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(sql.split())
            if "FROM dalton_data_task_runs" in q:
                assert "FOR SHARE" in q
                self._one = dict(db_state["run_row"])
            elif "UPDATE dalton_monitors" in q:
                db_state["updates"].append((q, params))
                db_state["last_publish_error"] = params[0]

        async def fetchone(self):
            return self._one

    class _ErrTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _ErrConn:
        def transaction(self):
            return _ErrTx()

        def cursor(self, **_kw):
            return _ErrCur()

    @asynccontextmanager
    async def _conn_ctx():
        yield _ErrConn()

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_id",
        create_autospec(
            monitor_db.get_monitor_by_id,
            side_effect=lambda _mid: _make_monitor(
                last_publish_error=db_state["last_publish_error"]
            ),
        ),
    )

    # 1a. Stale worker (claim_epoch=1) after takeover (DB epoch=2) -> MUST NOT overwrite last_publish_error!
    mon_stale = await monitor_db.record_monitor_publish_failure(
        MONITOR_ID,
        error_message="stale worker verification error",
        run_id=run_id,
        expected_claim_epoch=1,
    )
    assert mon_stale is not None and mon_stale.last_publish_error is None
    assert db_state["updates"] == []

    # 1b. Settled run (status='published', epoch=2) -> late worker with epoch=2 also MUST NOT overwrite!
    db_state["run_row"] = {
        "monitor_publish_status": "published",
        "monitor_publish_claim_epoch": 2,
    }
    mon_settled = await monitor_db.record_monitor_publish_failure(
        MONITOR_ID,
        error_message="late error after publish succeeded",
        run_id=run_id,
        expected_claim_epoch=2,
    )
    assert mon_settled is not None and mon_settled.last_publish_error is None
    assert db_state["updates"] == []

    # 1c. Active owner (status='publishing', epoch=2) -> writes last_publish_error normally!
    db_state["run_row"] = {
        "monitor_publish_status": "publishing",
        "monitor_publish_claim_epoch": 2,
    }
    mon_active = await monitor_db.record_monitor_publish_failure(
        MONITOR_ID,
        error_message="real verification error",
        run_id=run_id,
        expected_claim_epoch=2,
    )
    assert mon_active is not None and mon_active.last_publish_error == "real verification error"
    assert len(db_state["updates"]) == 1

    # 2. service.republish_data_job_monitor forwards run_id + expected_claim_epoch on Stage 2 failure
    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setenv("DALTON_MONITOR_VERIFY_READBACK_ENABLED", "1")
    monkeypatch.setattr(monitor_service, "has_capability", lambda _cap: True)

    panel_dir = tmp_path / "research-outputs" / "cn-intraday"
    panel_dir.mkdir(parents=True)
    (panel_dir / "index.html").write_text("<html><body>V2</body></html>", encoding="utf-8")

    existing_mon = _make_monitor(
        slug="cn-intraday",
        owner_user_id=COMPANY_SYSTEM_USER_ID,
        source_kind="data_job",
        source_ref="cn-intraday",
        entry_path="cn-intraday/index.html",
        visibility="company",
        published_seq=1,
        last_verified_seq=1,
    )
    snap = Snapshot(
        entries=[ManifestEntry("cn-intraday/index.html", 26, "entry")],
        left_out=[],
        files=[_make_version_file("cn-intraday/index.html", b"<html><body>V2</body></html>")],
        blobs={},
        title="V2",
    )
    monkeypatch.setattr(
        monitor_db,
        "get_monitor_by_data_job",
        create_autospec(monitor_db.get_monitor_by_data_job, return_value=existing_mon),
    )
    monkeypatch.setattr(
        monitor_db,
        "has_processed_monitor_run",
        create_autospec(monitor_db.has_processed_monitor_run, return_value=False),
    )
    monkeypatch.setattr(
        monitor_db,
        "ensure_company_monitor_workspace",
        create_autospec(monitor_db.ensure_company_monitor_workspace, return_value=WS_ID),
    )
    monkeypatch.setattr(
        artifacts_db,
        "ensure_artifact",
        create_autospec(
            artifacts_db.ensure_artifact,
            return_value=_make_artifact(path="cn-intraday/index.html", latest_seq=1, revision=2),
        ),
    )
    monkeypatch.setattr(
        artifact_snapshot,
        "capture_reader_monitor_snapshot",
        create_autospec(artifact_snapshot.capture_reader_monitor_snapshot, return_value=snap),
    )
    monkeypatch.setattr(
        monitor_service,
        "_abort_if_absent_dependencies",
        create_autospec(monitor_service._abort_if_absent_dependencies, return_value=None),
    )
    monkeypatch.setattr(
        monitor_db,
        "stage_monitor_candidate_version_tx",
        create_autospec(
            monitor_db.stage_monitor_candidate_version_tx,
            return_value=monitor_db.StagedMonitorCandidate(
                artifact_id=ARTIFACT_ID,
                candidate_seq=2,
                cut=True,
                existing_monitor=existing_mon,
                share_revision=2,
                share_audience=None,
                monitor_acl_revision=1,
                left_out_files=(),
            ),
        ),
    )
    monkeypatch.setattr(
        monitor_verify,
        "verify_candidate_monitor_version",
        create_autospec(
            monitor_verify.verify_candidate_monitor_version,
            side_effect=monitor_verify.MonitorVerificationError("boom"),
        ),
    )
    rec_fail_spy = create_autospec(
        monitor_db.record_monitor_publish_failure, return_value=existing_mon
    )
    monkeypatch.setattr(monitor_db, "record_monitor_publish_failure", rec_fail_spy)

    with pytest.raises(HTTPException):
        await monitor_service.republish_data_job_monitor(
            job_key="cn-intraday",
            run_id=run_id,
            data_root=tmp_path,
            claim_epoch=1,
            check_superseded=True,
        )
    rec_fail_spy.assert_awaited_once()
    assert rec_fail_spy.await_args.kwargs["run_id"] == run_id
    assert rec_fail_spy.await_args.kwargs["expected_claim_epoch"] == 1


@pytest.mark.asyncio
async def test_set_left_out_keeps_revision_so_existing_share_grant_reads_remaining_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toggling left_out on an ordinary /a/{code} share does not bump share_links.revision,
    so already-open pages holding the existing visitor grant still fetch remaining files."""
    from src.server.database.artifacts import records as records_db
    from src.server.preview_proxy import artifacts as artifact_proxy
    from src.server.services.artifacts import grants as artifact_grants

    monkeypatch.setattr(artifact_grants._VISITOR, "_key", b"s" * 32)
    share_code = "k3Vq9ZtR2mXa"
    index_html = b"<!doctype html><html><head><title>t</title></head><body>v11</body></html>"
    chart_js = b"console.log('ok');\n"
    stored_files = {
        "index.html": StoredVersionFile(
            user_id="owner-1",
            sha256="a" * 64,
            byte_len=len(index_html),
            content_type="text/html",
            content=index_html,
            owner_id="owner-1",
        ),
        "chart.js": StoredVersionFile(
            user_id="owner-1",
            sha256="b" * 64,
            byte_len=len(chart_js),
            content_type="application/javascript",
            content=chart_js,
            owner_id="owner-1",
        ),
    }
    row_state: dict[str, Any] = {
        "revision": 3,
        "shared_seq": 11,
        "left_out_files": (),
    }
    captured_assignments: list[str] = []

    async def _fake_set_record(*, user_id: str, artifact_id: str, assignments: str, params: dict[str, Any]):
        captured_assignments.append(assignments)
        assert "revision" not in assignments
        if params["left_out"]:
            row_state["left_out_files"] = tuple(
                sorted({*row_state["left_out_files"], params["path"]})
            )
        else:
            row_state["left_out_files"] = tuple(
                p for p in row_state["left_out_files"] if p != params["path"]
            )
        return _make_artifact(
            artifact_id=artifact_id,
            code=share_code,
            path="index.html",
            latest_seq=11,
            shared_at=NOW,
            files=("index.html", "chart.js"),
            revision=row_state["revision"],
            shared_seq=row_state["shared_seq"],
            left_out_files=row_state["left_out_files"],
        )

    async def _fake_get_shared_version_file(
        *, code: str, artifact_id: str, revision: int, seq: int, path: str, audience: str | None = None
    ):
        if (
            code != share_code
            or artifact_id != ARTIFACT_ID
            or revision != row_state["revision"]
            or seq != row_state["shared_seq"]
        ):
            return None
        return stored_files.get(path)

    monkeypatch.setattr(
        records_db,
        "set_record",
        create_autospec(records_db.set_record, side_effect=_fake_set_record),
    )
    monkeypatch.setattr(
        artifact_proxy,
        "get_shared_version_file",
        create_autospec(
            artifact_proxy.get_shared_version_file,
            side_effect=_fake_get_shared_version_file,
        ),
    )

    # Visitor opened /a/{code} before owner excluded secret.json and holds revision=3 grant.
    grant = await artifact_grants.mint_artifact_share_grant(
        share_code, ARTIFACT_ID, revision=3, seq=11
    )
    before = await artifact_proxy.load_artifact_file(
        grant.token, "11", "index.html", host_artifact_id=ARTIFACT_ID
    )
    assert before is not None and before.status == 200

    # Owner checks "don't publish" on secret.json -> share_links.revision stays 3.
    updated = await records_db.set_left_out(
        user_id="owner-1",
        artifact_id=ARTIFACT_ID,
        path="secret.json",
        left_out=True,
        cap=50,
    )
    assert updated.revision == 3
    assert "secret.json" in updated.left_out_files
    assert len(captured_assignments) == 1
    assert "revision" not in captured_assignments[0]

    # Existing visitor grant still fetches remaining files without 404.
    after_js = await artifact_proxy.load_artifact_file(
        grant.token, "11", "chart.js", host_artifact_id=ARTIFACT_ID
    )
    after_html = await artifact_proxy.load_artifact_file(
        grant.token, "11", "index.html", host_artifact_id=ARTIFACT_ID
    )
    assert after_js is not None and after_js.status == 200
    assert after_js.body == chart_js
    assert after_html is not None and after_html.status == 200


@pytest.mark.asyncio
async def test_older_run_deferred_while_newer_running_recovers_on_failure_and_supersedes_on_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newer run in-flight defers without settling; if newer fails, older reclaims and publishes;
    only terminal newer publish ('published'/'unchanged'/'partial_failed') settles older as 'superseded'."""
    from src.dalton.data_jobs import scheduler as sched_mod
    from src.dalton.data_jobs.container_runner import build_path_map

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setenv("DALTON_CAP_DATA_JOBS", "1")

    old_run_id = "11111111-1111-4111-8111-111111111111"
    new_run_id = "22222222-2222-4222-8222-222222222222"
    frozen_dir = tmp_path / "frozen-old-run"
    frozen_dir.mkdir(parents=True)
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")

    runs: dict[str, dict[str, Any]] = {
        old_run_id: {
            "run_id": old_run_id,
            "job_key": "cn-intraday",
            "status": "succeeded",
            "failure_reason": None,
            "scheduled_slot_utc": NOW,
            "completed_at": NOW,
            "monitor_publish_status": "pending",
            "monitor_publish_attempts": 0,
            "monitor_publish_claim_epoch": 0,
            "monitor_publish_detail": {
                "frozen_dir": str(frozen_dir),
                "panels": {"cn-intraday": {"status": "pending"}},
            },
            "lease_expired": True,
        },
        new_run_id: {
            "run_id": new_run_id,
            "job_key": "cn-intraday",
            "status": "running",
            "failure_reason": None,
            "scheduled_slot_utc": NOW + timedelta(hours=1),
            "completed_at": None,
            "monitor_publish_status": "not_applicable",
            "monitor_publish_attempts": 0,
            "monitor_publish_claim_epoch": 0,
            "monitor_publish_detail": {},
            "lease_expired": False,
        },
    }
    monitor_state: dict[str, Any] = {"published_seq": 1}
    settle_calls: list[dict[str, Any]] = []

    class _SimCur:
        def __init__(self) -> None:
            self._one: Any = None
            self._many: list[Any] = []
            self.rowcount = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(sql.split())
            self._one = None
            self._many = []
            self.rowcount = 0

            if q.startswith("SELECT 1 FROM dalton_data_task_runs WHERE job_key ="):
                # _is_run_superseded_in_cur MUST check only terminal publish statuses
                assert "'publishing'" not in q
                assert "'published'" in q and "'unchanged'" in q and "'partial_failed'" in q
                nr = runs[new_run_id]
                if (
                    nr["status"] == "succeeded"
                    and nr["monitor_publish_status"] in ("published", "unchanged", "partial_failed")
                ):
                    self._one = {"?column?": 1}
                return

            if "FROM dalton_data_task_runs WHERE (" in q and "FOR UPDATE SKIP LOCKED" in q:
                old = runs[old_run_id]
                if old["monitor_publish_status"] in ("pending", "retrying") or (
                    old["monitor_publish_status"] == "publishing" and old["lease_expired"]
                ):
                    self._many = [dict(old)]
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_status = 'superseded'" in q:
                old = runs[old_run_id]
                old["monitor_publish_status"] = "superseded"
                self.rowcount = 1
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_status = %(status)s" in q:
                old = runs[old_run_id]
                old["monitor_publish_status"] = str(params["status"])
                old["lease_expired"] = False
                self.rowcount = 1
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_status = 'publishing'" in q:
                old = runs[old_run_id]
                old["monitor_publish_status"] = "publishing"
                old["monitor_publish_attempts"] += 1
                old["monitor_publish_claim_epoch"] += 1
                old["lease_expired"] = False
                self._one = {
                    "monitor_publish_attempts": old["monitor_publish_attempts"],
                    "monitor_publish_claim_epoch": old["monitor_publish_claim_epoch"],
                }
                self.rowcount = 1
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_status = %(next_status)s" in q:
                old = runs[old_run_id]
                if (
                    old["monitor_publish_status"] == "publishing"
                    and old["monitor_publish_claim_epoch"] == int(params["claim_epoch"])
                ):
                    old["monitor_publish_status"] = str(params["next_status"])
                    if params.get("refund_attempt"):
                        assert "GREATEST(monitor_publish_attempts - 1, 0)" in q
                        old["monitor_publish_attempts"] = max(
                            old["monitor_publish_attempts"] - 1, 0
                        )
                    old["lease_expired"] = False
                    settle_calls.append(dict(params))
                    self.rowcount = 1
                return

            if "FROM dalton_data_task_runs WHERE run_id =" in q:
                self._one = dict(runs[old_run_id])
                return

            if "FROM dalton_data_task_runs WHERE job_key =" in q and "FOR SHARE" in q:
                nr = runs[new_run_id]
                if nr["status"] not in monitor_db._FAILED_OR_CANCELLED_RUN_STATUSES:
                    self._many = [
                        {
                            "run_id": nr["run_id"],
                            "status": nr["status"],
                            "monitor_publish_status": nr["monitor_publish_status"],
                        }
                    ]
                return

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._many)

    class _SimTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _SimConn:
        def transaction(self):
            return _SimTx()

        def cursor(self, **_kw):
            return _SimCur()

    sim_conn = _SimConn()

    @asynccontextmanager
    async def _conn_ctx():
        yield sim_conn

    async def _fake_promote_in_cur(_cur: Any, **kwargs: Any) -> monitor_db.PromotedMonitorResult:
        cand = int(kwargs["candidate_seq"])
        monitor_state["published_seq"] = cand
        return monitor_db.PromotedMonitorResult(
            monitor=_make_monitor(published_seq=cand, last_verified_seq=cand),
            promoted=True,
        )

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        monitor_db.artifacts_records_db,
        "lock_owned",
        create_autospec(
            monitor_db.artifacts_records_db.lock_owned,
            return_value=_make_artifact(revision=1, latest_seq=2),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "_select_monitor_in_cur",
        create_autospec(
            monitor_db._select_monitor_in_cur,
            side_effect=lambda _cur, _mid: _make_monitor(
                published_seq=monitor_state["published_seq"],
                last_verified_seq=monitor_state["published_seq"],
            ),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "_promote_verified_monitor_in_cur",
        create_autospec(
            monitor_db._promote_verified_monitor_in_cur,
            side_effect=_fake_promote_in_cur,
        ),
    )

    async def _fake_republish(**kwargs: Any) -> dict[str, Any]:
        before_promote = kwargs.get("before_promote")
        if before_promote is not None and not await before_promote():
            return {"status": "superseded", "superseded": True, "cut": False}
        promoted = await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            paths=["index.html"],
            monitor_id=MONITOR_ID,
            run_id=kwargs["run_id"],
            expected_claim_epoch=kwargs["claim_epoch"],
            check_superseded=kwargs["check_superseded"],
        )
        if promoted.fenced_out:
            return {"status": "fenced_out", "fenced_out": True, "cut": False}
        if promoted.deferred_to_newer:
            return {"status": "deferred", "deferred": True, "cut": False}
        if promoted.superseded_by_newer:
            return {"status": "superseded", "superseded": True, "cut": False}
        return {
            "status": "published",
            "cut": True,
            "panel_results": {"cn-intraday": {"status": "published", "published_seq": 2}},
        }

    monkeypatch.setattr(
        monitor_service,
        "republish_data_job_monitor",
        create_autospec(
            monitor_service.republish_data_job_monitor,
            side_effect=_fake_republish,
        ),
    )

    # Case 1: Newer run is 'running' -> older run does NOT advance pointer, refunds attempt,
    # and releases lease to 'retrying' (immediately reclaimable on next scan)!
    claimed1 = await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
    assert len(claimed1) == 1 and claimed1[0].claim_epoch == 1 and claimed1[0].attempts == 1
    st1 = await sched_mod.execute_claimed_monitor_publish(sim_conn, claimed1[0], path_map=path_map)
    assert st1 == "deferred"
    assert monitor_state["published_seq"] == 1
    assert runs[old_run_id]["monitor_publish_status"] == "retrying"
    assert runs[old_run_id]["monitor_publish_attempts"] == 0
    assert len(settle_calls) == 1
    assert settle_calls[0]["next_status"] == "retrying"
    assert settle_calls[0]["refund_attempt"] is True
    assert settle_calls[0]["delay_s"] is None

    # Case 2: Newer run subsequently fails -> next scan reclaims older run directly and publishes!
    runs[new_run_id]["status"] = "failed"
    claimed2 = await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
    assert len(claimed2) == 1 and claimed2[0].claim_epoch == 2 and claimed2[0].attempts == 1
    st2 = await sched_mod.execute_claimed_monitor_publish(sim_conn, claimed2[0], path_map=path_map)
    assert st2 == "published"
    assert monitor_state["published_seq"] == 2
    assert runs[old_run_id]["monitor_publish_status"] == "published"
    assert runs[old_run_id]["monitor_publish_attempts"] == 1
    assert len(settle_calls) == 2 and settle_calls[1]["next_status"] == "published"
    assert settle_calls[1]["refund_attempt"] is False

    # Case 3: Newer run is already 'published' -> older run settles cleanly to terminal 'superseded'
    runs[old_run_id]["monitor_publish_status"] = "publishing"
    runs[old_run_id]["monitor_publish_claim_epoch"] = 3
    runs[new_run_id]["status"] = "succeeded"
    runs[new_run_id]["monitor_publish_status"] = "published"
    claimed3 = sched_mod.ClaimedMonitorPublishRun(
        run_id=old_run_id,
        job_key="cn-intraday",
        status="succeeded",
        failure_reason=None,
        scheduled_slot_utc=NOW,
        completed_at=NOW,
        attempts=1,
        claim_epoch=3,
        detail={"frozen_dir": str(frozen_dir), "panels": {"cn-intraday": {"status": "pending"}}},
    )
    st3 = await sched_mod.execute_claimed_monitor_publish(sim_conn, claimed3, path_map=path_map)
    assert st3 == "superseded"
    assert runs[old_run_id]["monitor_publish_status"] == "superseded"

    # Also verify claim_monitor_publish_run_tx converges pending older run to 'superseded' ONLY on terminal publish
    runs[old_run_id]["monitor_publish_status"] = "pending"
    runs[new_run_id]["monitor_publish_status"] = "publishing"
    claimed_while_pub = await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
    assert len(claimed_while_pub) == 1
    assert runs[old_run_id]["monitor_publish_status"] == "publishing"

    runs[old_run_id]["monitor_publish_status"] = "pending"
    runs[new_run_id]["monitor_publish_status"] = "published"
    claimed_after_pub = await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
    assert claimed_after_pub == []
    assert runs[old_run_id]["monitor_publish_status"] == "superseded"


@pytest.mark.asyncio
async def test_deferred_publish_does_not_exhaust_attempts_and_preserves_fenced_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated deferrals (> MAX_MONITOR_PUBLISH_ATTEMPTS) must refund the attempt and release
    the lease under the same claim_epoch fence so the older run can still be claimed and published
    if the newer run fails; stale-epoch deferral is fenced out; fenced_out path stays untouched."""
    from src.dalton.data_jobs import scheduler as sched_mod
    from src.dalton.data_jobs.container_runner import build_path_map

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setenv("DALTON_CAP_DATA_JOBS", "1")

    old_run_id = "33333333-3333-4333-8333-333333333333"
    new_run_id = "44444444-4444-4444-8444-444444444444"
    frozen_dir = tmp_path / "frozen-old-run-defer"
    frozen_dir.mkdir(parents=True)
    path_map = build_path_map(host_var_dir=tmp_path / "h2", backend_var_dir=tmp_path / "b2")

    runs: dict[str, dict[str, Any]] = {
        old_run_id: {
            "run_id": old_run_id,
            "job_key": "cn-intraday",
            "status": "succeeded",
            "failure_reason": None,
            "scheduled_slot_utc": NOW,
            "completed_at": NOW,
            "monitor_publish_status": "pending",
            "monitor_publish_attempts": 0,
            "monitor_publish_claim_epoch": 0,
            "monitor_publish_lease_expires_at": None,
            "monitor_publish_next_retry_at": None,
            "monitor_publish_detail": {
                "frozen_dir": str(frozen_dir),
                "panels": {"cn-intraday": {"status": "pending"}},
            },
        },
        new_run_id: {
            "run_id": new_run_id,
            "job_key": "cn-intraday",
            "status": "running",
            "failure_reason": None,
            "scheduled_slot_utc": NOW + timedelta(hours=1),
            "completed_at": None,
            "monitor_publish_status": "not_applicable",
            "monitor_publish_attempts": 0,
            "monitor_publish_claim_epoch": 0,
            "monitor_publish_lease_expires_at": None,
            "monitor_publish_next_retry_at": None,
            "monitor_publish_detail": {},
        },
    }
    monitor_state: dict[str, Any] = {"published_seq": 1}
    settle_sql_calls: list[dict[str, Any]] = []

    class _SimCur:
        def __init__(self) -> None:
            self._one: Any = None
            self._many: list[Any] = []
            self.rowcount = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(sql.split())
            self._one = None
            self._many = []
            self.rowcount = 0

            if q.startswith("SELECT 1 FROM dalton_data_task_runs WHERE job_key ="):
                nr = runs[new_run_id]
                if (
                    nr["status"] == "succeeded"
                    and nr["monitor_publish_status"] in ("published", "unchanged", "partial_failed")
                ):
                    self._one = {"?column?": 1}
                return

            if "FROM dalton_data_task_runs WHERE (" in q and "FOR UPDATE SKIP LOCKED" in q:
                old = runs[old_run_id]
                if (
                    old["monitor_publish_status"] in ("pending", "retrying")
                    and old["monitor_publish_next_retry_at"] is None
                ) or (
                    old["monitor_publish_status"] == "publishing"
                    and old["monitor_publish_lease_expires_at"] == "expired"
                ):
                    self._many = [dict(old)]
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_status = %(status)s" in q:
                old = runs[old_run_id]
                old["monitor_publish_status"] = str(params["status"])
                old["monitor_publish_lease_expires_at"] = None
                old["monitor_publish_next_retry_at"] = None
                self.rowcount = 1
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_status = 'publishing'" in q:
                old = runs[old_run_id]
                old["monitor_publish_status"] = "publishing"
                old["monitor_publish_attempts"] += 1
                old["monitor_publish_claim_epoch"] += 1
                old["monitor_publish_lease_expires_at"] = "active"
                self._one = {
                    "monitor_publish_attempts": old["monitor_publish_attempts"],
                    "monitor_publish_claim_epoch": old["monitor_publish_claim_epoch"],
                }
                self.rowcount = 1
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_status = %(next_status)s" in q:
                old = runs[old_run_id]
                settle_sql_calls.append(dict(params))
                if (
                    old["monitor_publish_status"] == "publishing"
                    and old["monitor_publish_claim_epoch"] == int(params["claim_epoch"])
                ):
                    old["monitor_publish_status"] = str(params["next_status"])
                    if params.get("refund_attempt"):
                        assert "GREATEST(monitor_publish_attempts - 1, 0)" in q
                        old["monitor_publish_attempts"] = max(
                            old["monitor_publish_attempts"] - 1, 0
                        )
                    old["monitor_publish_lease_expires_at"] = None
                    old["monitor_publish_next_retry_at"] = (
                        f"delay:{params['delay_s']}" if params.get("delay_s") is not None else None
                    )
                    self.rowcount = 1
                return

            if "FROM dalton_data_task_runs WHERE run_id =" in q:
                self._one = dict(runs[old_run_id])
                return

            if "FROM dalton_data_task_runs WHERE job_key =" in q and "FOR SHARE" in q:
                nr = runs[new_run_id]
                if nr["status"] not in monitor_db._FAILED_OR_CANCELLED_RUN_STATUSES:
                    self._many = [
                        {
                            "run_id": nr["run_id"],
                            "status": nr["status"],
                            "monitor_publish_status": nr["monitor_publish_status"],
                        }
                    ]
                return

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._many)

    class _SimTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _SimConn:
        def transaction(self):
            return _SimTx()

        def cursor(self, **_kw):
            return _SimCur()

    sim_conn = _SimConn()

    @asynccontextmanager
    async def _conn_ctx():
        yield sim_conn

    async def _fake_promote_in_cur(_cur: Any, **kwargs: Any) -> monitor_db.PromotedMonitorResult:
        cand = int(kwargs["candidate_seq"])
        monitor_state["published_seq"] = cand
        return monitor_db.PromotedMonitorResult(
            monitor=_make_monitor(published_seq=cand, last_verified_seq=cand),
            promoted=True,
        )

    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        monitor_db.artifacts_records_db,
        "lock_owned",
        create_autospec(
            monitor_db.artifacts_records_db.lock_owned,
            return_value=_make_artifact(revision=1, latest_seq=2),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "_select_monitor_in_cur",
        create_autospec(
            monitor_db._select_monitor_in_cur,
            side_effect=lambda _cur, _mid: _make_monitor(
                published_seq=monitor_state["published_seq"],
                last_verified_seq=monitor_state["published_seq"],
            ),
        ),
    )
    monkeypatch.setattr(
        monitor_db,
        "_promote_verified_monitor_in_cur",
        create_autospec(
            monitor_db._promote_verified_monitor_in_cur,
            side_effect=_fake_promote_in_cur,
        ),
    )

    async def _fake_republish(**kwargs: Any) -> dict[str, Any]:
        before_promote = kwargs.get("before_promote")
        if before_promote is not None and not await before_promote():
            return {"status": "superseded", "superseded": True, "cut": False}
        promoted = await monitor_db.promote_verified_monitor_version_tx(
            artifact_id=ARTIFACT_ID,
            candidate_seq=2,
            workspace_owner_id=COMPANY_SYSTEM_USER_ID,
            paths=["index.html"],
            monitor_id=MONITOR_ID,
            run_id=kwargs["run_id"],
            expected_claim_epoch=kwargs["claim_epoch"],
            check_superseded=kwargs["check_superseded"],
        )
        if promoted.fenced_out:
            return {"status": "fenced_out", "fenced_out": True, "cut": False}
        if promoted.deferred_to_newer:
            return {"status": "deferred", "deferred": True, "cut": False}
        if promoted.superseded_by_newer:
            return {"status": "superseded", "superseded": True, "cut": False}
        return {
            "status": "published",
            "cut": True,
            "panel_results": {"cn-intraday": {"status": "published", "published_seq": 2}},
        }

    orig_republish = monitor_service.republish_data_job_monitor
    republish_mock = create_autospec(
        orig_republish,
        side_effect=_fake_republish,
    )
    monkeypatch.setattr(monitor_service, "republish_data_job_monitor", republish_mock)

    # 1. Older run is deferred 4 consecutive times (> MAX_MONITOR_PUBLISH_ATTEMPTS = 3)
    for round_idx in range(1, sched_mod.MAX_MONITOR_PUBLISH_ATTEMPTS + 2):
        claimed = await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
        assert len(claimed) == 1
        assert claimed[0].claim_epoch == round_idx
        assert claimed[0].attempts == 1
        st = await sched_mod.execute_claimed_monitor_publish(sim_conn, claimed[0], path_map=path_map)
        assert st == "deferred"
        assert monitor_state["published_seq"] == 1
        assert runs[old_run_id]["monitor_publish_status"] == "retrying"
        assert runs[old_run_id]["monitor_publish_attempts"] == 0
        assert runs[old_run_id]["monitor_publish_lease_expires_at"] is None
        assert runs[old_run_id]["monitor_publish_next_retry_at"] is None

    # 2. Newer run subsequently fails -> older run is still claimable and publishes cleanly!
    runs[new_run_id]["status"] = "failed"
    claimed_after_fail = await sched_mod.claim_monitor_publish_run_tx(
        sim_conn, run_id=old_run_id, limit=1
    )
    assert len(claimed_after_fail) == 1
    assert claimed_after_fail[0].claim_epoch == sched_mod.MAX_MONITOR_PUBLISH_ATTEMPTS + 2
    assert claimed_after_fail[0].attempts == 1
    st_pub = await sched_mod.execute_claimed_monitor_publish(
        sim_conn, claimed_after_fail[0], path_map=path_map
    )
    assert st_pub == "published"
    assert monitor_state["published_seq"] == 2
    assert runs[old_run_id]["monitor_publish_status"] == "published"
    assert runs[old_run_id]["monitor_publish_attempts"] == 1

    # 3. Stale-epoch deferred settlement is fenced out and NEVER refunds active worker's attempt
    runs[old_run_id]["monitor_publish_status"] = "publishing"
    runs[old_run_id]["monitor_publish_attempts"] = 2
    runs[old_run_id]["monitor_publish_claim_epoch"] = 10
    runs[old_run_id]["monitor_publish_lease_expires_at"] = "active"
    monkeypatch.setattr(
        monitor_service,
        "republish_data_job_monitor",
        create_autospec(
            orig_republish,
            return_value={"status": "deferred", "deferred": True, "cut": False},
        ),
    )
    stale_claimed = sched_mod.ClaimedMonitorPublishRun(
        run_id=old_run_id,
        job_key="cn-intraday",
        status="succeeded",
        failure_reason=None,
        scheduled_slot_utc=NOW,
        completed_at=NOW,
        attempts=1,
        claim_epoch=9,
        detail={"frozen_dir": str(frozen_dir), "panels": {"cn-intraday": {"status": "pending"}}},
    )
    st_stale_defer = await sched_mod.execute_claimed_monitor_publish(
        sim_conn, stale_claimed, path_map=path_map
    )
    assert st_stale_defer == "fenced_out"
    assert runs[old_run_id]["monitor_publish_status"] == "publishing"
    assert runs[old_run_id]["monitor_publish_attempts"] == 2
    assert runs[old_run_id]["monitor_publish_lease_expires_at"] == "active"

    # 4. fenced_out path from republish_data_job_monitor stays untouched (no settle call, no refund)
    settle_count_before_fence = len(settle_sql_calls)
    monkeypatch.setattr(
        monitor_service,
        "republish_data_job_monitor",
        create_autospec(
            orig_republish,
            return_value={"status": "fenced_out", "fenced_out": True, "cut": False},
        ),
    )
    st_fenced = await sched_mod.execute_claimed_monitor_publish(
        sim_conn, stale_claimed, path_map=path_map
    )
    assert st_fenced == "fenced_out"
    assert len(settle_sql_calls) == settle_count_before_fence
    assert runs[old_run_id]["monitor_publish_status"] == "publishing"
    assert runs[old_run_id]["monitor_publish_attempts"] == 2


@pytest.mark.asyncio
async def test_multi_panel_mixed_failed_and_deferred_does_not_refund_and_reaches_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In per-panel multi-binding publish (e.g. pm-market-state), if one panel fails
    verification while another is deferred by an in-flight newer run:
    - per-panel failure progress (status/error) is preserved;
    - the attempt is COUNTED (no refund) with normal retry backoff;
    - repeated failures reach MAX_MONITOR_PUBLISH_ATTEMPTS and settle terminally;
    - when all unfinished panels are purely deferred (no panel failed), the attempt is refunded."""
    from src.dalton.data_jobs import scheduler as sched_mod
    from src.dalton.data_jobs.container_runner import build_path_map

    monkeypatch.setenv("DALTON_ARTIFACT_PUBLISHING_ENABLED", "1")
    monkeypatch.setenv("DALTON_CAP_DATA_JOBS", "1")
    monkeypatch.delenv("DALTON_MONITOR_MULTI_BINDING_PUBLISH_MODE", raising=False)

    us_binding, cn_binding = data_job_registry.get_registered_monitor_bindings("pm-market-state")
    old_run_id = "55555555-5555-4555-8555-555555555555"
    new_run_id = "66666666-6666-4666-8666-666666666666"
    frozen_dir = tmp_path / "frozen-pm-mixed"
    frozen_dir.mkdir(parents=True)
    path_map = build_path_map(host_var_dir=tmp_path / "hm", backend_var_dir=tmp_path / "bm")

    def _new_run_state() -> dict[str, dict[str, Any]]:
        return {
            old_run_id: {
                "run_id": old_run_id,
                "job_key": "pm-market-state",
                "status": "succeeded",
                "failure_reason": None,
                "scheduled_slot_utc": NOW,
                "completed_at": NOW,
                "monitor_publish_status": "pending",
                "monitor_publish_attempts": 0,
                "monitor_publish_claim_epoch": 0,
                "monitor_publish_lease_expires_at": None,
                "monitor_publish_next_retry_at": None,
                "monitor_publish_detail": {
                    "frozen_dir": str(frozen_dir),
                    "panels": {
                        us_binding.slug: {"status": "pending"},
                        cn_binding.slug: {"status": "pending"},
                    },
                },
            },
            new_run_id: {
                "run_id": new_run_id,
                "job_key": "pm-market-state",
                "status": "running",
                "failure_reason": None,
                "scheduled_slot_utc": NOW + timedelta(hours=1),
                "completed_at": None,
                "monitor_publish_status": "not_applicable",
                "monitor_publish_attempts": 0,
                "monitor_publish_claim_epoch": 0,
                "monitor_publish_lease_expires_at": None,
                "monitor_publish_next_retry_at": None,
                "monitor_publish_detail": {},
            },
        }

    runs = _new_run_state()

    class _SimCur:
        def __init__(self) -> None:
            self._one: Any = None
            self._many: list[Any] = []
            self.rowcount = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, sql: str, params: Any = None) -> None:
            q = " ".join(sql.split())
            self._one = None
            self._many = []
            self.rowcount = 0

            if q.startswith("SELECT 1 FROM dalton_data_task_runs WHERE job_key ="):
                nr = runs[new_run_id]
                if (
                    nr["status"] == "succeeded"
                    and nr["monitor_publish_status"] in ("published", "unchanged", "partial_failed")
                ):
                    self._one = {"?column?": 1}
                return

            if "FROM dalton_data_task_runs WHERE (" in q and "FOR UPDATE SKIP LOCKED" in q:
                old = runs[old_run_id]
                if (
                    old["monitor_publish_status"] in ("pending", "retrying")
                    and old["monitor_publish_next_retry_at"] is None
                ) or (
                    old["monitor_publish_status"] == "publishing"
                    and old["monitor_publish_lease_expires_at"] == "expired"
                ):
                    self._many = [dict(old)]
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_status = 'publishing'" in q:
                old = runs[old_run_id]
                old["monitor_publish_status"] = "publishing"
                old["monitor_publish_attempts"] += 1
                old["monitor_publish_claim_epoch"] += 1
                old["monitor_publish_lease_expires_at"] = "active"
                self._one = {
                    "monitor_publish_attempts": old["monitor_publish_attempts"],
                    "monitor_publish_claim_epoch": old["monitor_publish_claim_epoch"],
                }
                self.rowcount = 1
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_detail = %(detail)s::jsonb" in q:
                old = runs[old_run_id]
                if (
                    old["monitor_publish_status"] == "publishing"
                    and old["monitor_publish_claim_epoch"] == int(params["claim_epoch"])
                ):
                    old["monitor_publish_detail"] = json.loads(params["detail"])
                    self.rowcount = 1
                return

            if "UPDATE dalton_data_task_runs SET monitor_publish_status = %(next_status)s" in q:
                old = runs[old_run_id]
                if (
                    old["monitor_publish_status"] == "publishing"
                    and old["monitor_publish_claim_epoch"] == int(params["claim_epoch"])
                ):
                    old["monitor_publish_status"] = str(params["next_status"])
                    if params.get("refund_attempt"):
                        assert "GREATEST(monitor_publish_attempts - 1, 0)" in q
                        old["monitor_publish_attempts"] = max(
                            old["monitor_publish_attempts"] - 1, 0
                        )
                    old["monitor_publish_detail"] = json.loads(params["detail"])
                    old["monitor_publish_lease_expires_at"] = None
                    old["monitor_publish_next_retry_at"] = (
                        f"delay:{params['delay_s']}" if params.get("delay_s") is not None else None
                    )
                    self.rowcount = 1
                return

            if "FROM dalton_data_task_runs WHERE run_id =" in q:
                self._one = dict(runs[old_run_id])
                return

            if "FROM dalton_data_task_runs WHERE job_key =" in q and "FOR SHARE" in q:
                nr = runs[new_run_id]
                if nr["status"] not in monitor_db._FAILED_OR_CANCELLED_RUN_STATUSES:
                    self._many = [
                        {
                            "run_id": nr["run_id"],
                            "status": nr["status"],
                            "monitor_publish_status": nr["monitor_publish_status"],
                        }
                    ]
                return

        async def fetchone(self):
            return self._one

        async def fetchall(self):
            return list(self._many)

    class _SimTx:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

    class _SimConn:
        def transaction(self):
            return _SimTx()

        def cursor(self, **_kw):
            return _SimCur()

    sim_conn = _SimConn()

    @asynccontextmanager
    async def _conn_ctx():
        yield sim_conn

    alert_spy = create_autospec(sched_mod._emit_monitor_publish_ops_alert, return_value=True)
    monkeypatch.setattr(sched_mod, "_emit_monitor_publish_ops_alert", alert_spy)
    monkeypatch.setattr(monitor_db, "get_db_connection", _conn_ctx)
    monkeypatch.setattr(
        monitor_db,
        "list_monitors_by_data_job",
        create_autospec(monitor_db.list_monitors_by_data_job, return_value=[]),
    )

    panel_modes: dict[str, str] = {
        us_binding.slug: "fail",
        cn_binding.slug: "defer",
    }

    async def _fake_single_binding(binding: Any, **_kwargs: Any) -> dict[str, Any]:
        mode = panel_modes[binding.slug]
        if mode == "fail":
            raise HTTPException(status_code=500, detail=f"{binding.slug} verify failed")
        if mode == "defer":
            return {
                "monitor_id": f"mon-{binding.slug}",
                "slug": binding.slug,
                "published_seq": 1,
                "last_verified_seq": 1,
                "cut": False,
                "deduplicated": False,
                "deferred": True,
                "status": "deferred",
            }
        return {
            "monitor_id": f"mon-{binding.slug}",
            "slug": binding.slug,
            "published_seq": 2,
            "last_verified_seq": 2,
            "cut": True,
            "deduplicated": False,
            "status": "published",
        }

    monkeypatch.setattr(
        monitor_service,
        "_republish_single_binding",
        create_autospec(monitor_service._republish_single_binding, side_effect=_fake_single_binding),
    )

    # 1. Mixed result across 3 attempts: us_binding fails while cn_binding is deferred.
    #    Each attempt MUST be counted (no refund), keep per-panel error/status, apply retry backoff,
    #    and enter terminal 'failed' on attempt 3 with an ops alert.
    for expected_attempt in range(1, sched_mod.MAX_MONITOR_PUBLISH_ATTEMPTS + 1):
        claimed = await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
        assert len(claimed) == 1
        assert claimed[0].attempts == expected_attempt
        assert claimed[0].claim_epoch == expected_attempt
        st = await sched_mod.execute_claimed_monitor_publish(sim_conn, claimed[0], path_map=path_map)

        old = runs[old_run_id]
        detail_panels = old["monitor_publish_detail"]["panels"]
        assert detail_panels[us_binding.slug]["status"] == "failed"
        assert f"{us_binding.slug} verify failed" in detail_panels[us_binding.slug]["error"]
        assert detail_panels[cn_binding.slug]["status"] == "deferred"
        assert f"{us_binding.slug} verify failed" in old["monitor_publish_detail"]["last_error"]
        assert old["monitor_publish_attempts"] == expected_attempt
        assert old["monitor_publish_lease_expires_at"] is None

        if expected_attempt < sched_mod.MAX_MONITOR_PUBLISH_ATTEMPTS:
            expected_delay = sched_mod.MONITOR_PUBLISH_RETRY_BACKOFFS_S[expected_attempt - 1]
            assert st == "retrying"
            assert old["monitor_publish_status"] == "retrying"
            assert old["monitor_publish_next_retry_at"] == f"delay:{expected_delay}"
            # Immediate scan while backoff is active must not re-claim
            assert (
                await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
                == []
            )
            # Advance clock past backoff window for next round
            old["monitor_publish_next_retry_at"] = None
        else:
            assert st == "failed"
            assert old["monitor_publish_status"] == "failed"
            assert old["monitor_publish_next_retry_at"] is None

    alert_spy.assert_awaited_once()
    assert alert_spy.await_args.kwargs["status"] == "failed"
    assert await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1) == []

    # 2. Mixed failure followed by pure deferral:
    #    Attempt 1 fails (counted -> attempts=1), Attempt 2 has BOTH panels deferred (refunded -> attempts=1).
    runs.clear()
    runs.update(_new_run_state())
    panel_modes[us_binding.slug] = "fail"
    panel_modes[cn_binding.slug] = "defer"
    c1 = await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
    assert len(c1) == 1
    assert await sched_mod.execute_claimed_monitor_publish(sim_conn, c1[0], path_map=path_map) == "retrying"
    assert runs[old_run_id]["monitor_publish_attempts"] == 1
    runs[old_run_id]["monitor_publish_next_retry_at"] = None

    panel_modes[us_binding.slug] = "defer"
    panel_modes[cn_binding.slug] = "defer"
    c2 = await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
    assert len(c2) == 1
    assert c2[0].attempts == 2
    assert await sched_mod.execute_claimed_monitor_publish(sim_conn, c2[0], path_map=path_map) == "deferred"
    assert runs[old_run_id]["monitor_publish_status"] == "retrying"
    assert runs[old_run_id]["monitor_publish_attempts"] == 1
    assert runs[old_run_id]["monitor_publish_next_retry_at"] is None
    assert runs[old_run_id]["monitor_publish_detail"]["panels"][us_binding.slug]["status"] == "deferred"
    assert "error" not in runs[old_run_id]["monitor_publish_detail"]["panels"][us_binding.slug]

    # 3. Pure deferral with one panel already published and the other deferred:
    #    Zero failed panels -> preserves published panel progress AND refunds attempt.
    runs.clear()
    runs.update(_new_run_state())
    panel_modes[us_binding.slug] = "publish"
    panel_modes[cn_binding.slug] = "defer"
    c3 = await sched_mod.claim_monitor_publish_run_tx(sim_conn, run_id=old_run_id, limit=1)
    assert len(c3) == 1
    assert c3[0].attempts == 1
    assert await sched_mod.execute_claimed_monitor_publish(sim_conn, c3[0], path_map=path_map) == "deferred"
    assert runs[old_run_id]["monitor_publish_status"] == "retrying"
    assert runs[old_run_id]["monitor_publish_attempts"] == 0
    assert runs[old_run_id]["monitor_publish_lease_expires_at"] is None
    assert runs[old_run_id]["monitor_publish_next_retry_at"] is None
    assert runs[old_run_id]["monitor_publish_detail"]["panels"][us_binding.slug]["status"] == "published"
    assert runs[old_run_id]["monitor_publish_detail"]["panels"][cn_binding.slug]["status"] == "deferred"
