"""Unit tests for Dalton data-task report preparation, artifact safety, authorization, and gateway transport.

Uses synthetic fixtures only and constructs all test doubles with ``unittest.mock.create_autospec``.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import io
import json
import os
from pathlib import Path
import sys
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from src.dalton.app.router import router as dalton_router
from src.dalton.data_jobs.container_runner import build_path_map
from src.dalton.data_jobs.notification_payloads import (
    DataTaskArtifactError,
    DataTaskArtifactNotFoundError,
    DataTaskNotificationError,
    DataTaskNotificationPrepError,
    T26_JOB_KEY,
    T27_JOB_KEY,
    _safe_read_contained_regular_file,
    build_data_task_delivery_plan,
    prepare_t27_run_outbox,
    read_validated_run_artifact,
)
from src.dalton.data_jobs.runner_wrapper import (
    SecretRedactor,
    persist_redacted_outbox,
    run_wrapped_command,
)
from src.dalton.data_jobs.scheduler import (
    dispatch_data_task_run_notification,
    dispatch_pending_data_task_notifications,
)
from src.dalton.database import channel_gateway as cg_db
from src.dalton.database import data_task_notifications as dtn_db
from src.dalton.notify import channel_transport as ct_mod
from src.dalton.notify import dispatch as notify_dispatch_mod
from src.dalton.settings import get_dalton_channel_gateway_url

SERVICE_TOKEN = "t" * 40


def _make_synthetic_png(tag: bytes) -> bytes:
    """Build deterministic valid PNG header + synthetic payload bytes."""
    return b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + tag + b"\x00" * 32


def _make_synthetic_t27_markdown(workspace_prefix: str = "/Users/everflow/.openclaw/workspace") -> str:
    return "\n".join(
        [
            "# 亚洲四地杠杆/拥挤度监控 — 2026-11-09",
            "> 合成测试数据 ｜ 四市场截面",
            "",
            "## 🇨🇳 中国A股 — 上证指数 3400 (+0.5%)",
            "- 融资余额：18,500 亿元",
            "| 指标 | 当前 | 分位 |",
            "| --- | --- | --- |",
            "| 融资买入占比 | 9.2% | 76% |",
            f"IMG:{workspace_prefix}/temp/asia-leverage-crossview/charts/cn_latest.png",
            "",
            "## 🇰🇷 韩国 — KOSPI 2600 (-0.2%)",
            "- 信用融资余额：19.1 万亿韩元",
            f"IMG:{workspace_prefix}/temp/asia-leverage-crossview/charts/kr_latest.png",
            "",
            "## 🇯🇵 日本 — 日经225 39000 (+0.8%)",
            "- 买方信用余额：4.2 万亿日元",
            f"IMG:{workspace_prefix}/temp/asia-leverage-crossview/charts/jp_latest.png",
            "",
            "## 🇹🇼 台湾 — 台湾加权指数 23000 (+0.4%)",
            "- 融资余额：3,120 亿新台币",
            f"IMG:{workspace_prefix}/temp/asia-leverage-crossview/charts/tw_latest.png",
            "",
            "## 口径说明",
            "- 合成口径说明第 1 条",
            "- 合成口径说明第 2 条",
        ]
    )


def _write_synthetic_t27_charts(ws_dir: Path) -> dict[str, bytes]:
    charts_dir = ws_dir / "temp" / "asia-leverage-crossview" / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    by_name: dict[str, bytes] = {}
    for code in ("cn", "kr", "jp", "tw"):
        fname = f"{code}_latest.png"
        data = _make_synthetic_png(f"T27-{code.upper()}-V1".encode("ascii"))
        (charts_dir / fname).write_bytes(data)
        by_name[fname] = data
    return by_name


def test_call_site_signatures_bind_cleanly() -> None:
    """Verify actual function signatures across notification payload, DB, router, scheduler, and retry modules."""
    inspect.signature(build_data_task_delivery_plan).bind(
        run_id=str(uuid4()),
        job_key="sentiment-dashboard-digest",
        raw_outbox={},
        run_artifacts_dir=Path("/tmp"),
    )
    inspect.signature(prepare_t27_run_outbox).bind(
        "# Title",
        workspace_dir=Path("/tmp"),
        run_artifacts_dir=Path("/tmp"),
        redactor=SecretRedactor([]),
    )
    inspect.signature(dtn_db.authorize_data_task_notification_plan).bind(
        run_id=str(uuid4()),
        target="feishu:@me",
    )
    inspect.signature(dtn_db.authorize_and_claim_data_task_delivery).bind(
        delivery_key="data_task:k",
        run_id=str(uuid4()),
        target="feishu:@me",
        has_non_empty_text=True,
        total_chunks=1,
    )
    inspect.signature(cg_db.authorize_and_claim_delivery).bind(
        delivery_key="data_task:k",
        kind="data_task",
        run_id=str(uuid4()),
        target="feishu:@me",
        platform="feishu",
        has_non_empty_text=True,
        total_chunks=1,
    )
    inspect.signature(ct_mod.dispatch_data_task_target_via_gateway).bind(
        run_id=str(uuid4()),
        target="feishu:@me",
    )
    inspect.signature(ct_mod.post_data_task_gateway_notify).bind(
        ["feishu:@me"],
        run_id=str(uuid4()),
    )
    inspect.signature(dispatch_data_task_run_notification).bind(
        object(),
        lock_conn=object(),
        run_id=str(uuid4()),
        job_key="softbank-nav-monitor",
    )
    inspect.signature(dispatch_pending_data_task_notifications).bind(
        object(),
        lock_conn=object(),
    )
    inspect.signature(notify_dispatch_mod.retry_failed_notify_deliveries).bind(
        data_task_notify_fn=ct_mod.post_data_task_gateway_notify,
    )


def test_t13_card_schema2_structure_nested_placeholders_and_chat_id_stripping(
    tmp_path: Path,
) -> None:
    """Requirement 2: T13/T52/T54 Card 2.0 JSON is unchanged except placeholder wiring; embedded chat_id is ignored."""
    run_id = str(uuid4())
    run_dir = tmp_path / ".dalton-runs" / run_id
    (run_dir / "images").mkdir(parents=True)

    img_main_bytes = _make_synthetic_png(b"T13-MAIN-CHART")
    img_nested_bytes = _make_synthetic_png(b"T13-PANEL-CHART")
    (run_dir / "images" / "card0_img0.png").write_bytes(img_main_bytes)
    (run_dir / "images" / "card0_img1.png").write_bytes(img_nested_bytes)

    original_card: dict[str, Any] = {
        "schema": "2.0",
        "config": {"wide_screen_mode": True, "update_multi": True, "custom_flag": "keep_me"},
        "header": {
            "title": {"tag": "plain_text", "content": "合成市场情绪日报"},
            "subtitle": {"tag": "plain_text", "content": "窗口：20D"},
            "template": "indigo",
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": "每日追踪"},
                    "color": "blue",
                }
            ],
        },
        "body": {
            "direction": "vertical",
            "vertical_spacing": "8px",
            "elements": [
                {"tag": "markdown", "content": "**一、核心指标**\n\n| 指标 | 数值 |\n|---|---|\n| VIX | 15.2 |"},
                {"tag": "__img__", "path": "/Users/everflow/.openclaw/workspace/temp/main.png"},
                {"tag": "hr"},
                {
                    "tag": "collapsible_panel",
                    "expanded": False,
                    "header": {"title": {"tag": "markdown", "content": "**附图明细**"}},
                    "border": {"color": "grey", "corner_radius": "6px"},
                    "elements": [
                        {"tag": "markdown", "content": "子面板说明"},
                        {"tag": "__img__", "path": "/Users/everflow/.openclaw/workspace/temp/nested.png"},
                    ],
                },
            ],
        },
    }

    raw_outbox = {
        "feishu_cards": [
            {
                "card": copy.deepcopy(original_card),
                "images": [
                    {
                        "placeholder": "/Users/everflow/.openclaw/workspace/temp/main.png",
                        "path": "images/card0_img0.png",
                    },
                    {
                        "placeholder": "/Users/everflow/.openclaw/workspace/temp/nested.png",
                        "path": "images/card0_img1.png",
                    },
                ],
                "chat_id": "oc_untrusted_embedded_chat",
            }
        ]
    }

    plan = build_data_task_delivery_plan(
        run_id=run_id,
        job_key="sentiment-dashboard-digest",
        raw_outbox=raw_outbox,
        run_artifacts_dir=run_dir,
    )
    assert len(plan.items) == 1
    wire_item = plan.items[0].to_wire_dict(delivery_key=f"data_task:{run_id}:feishu:oc_dest:item:0")

    # Embedded chat_id and local filesystem paths must not appear anywhere in wire item
    serialized_wire = json.dumps(wire_item, ensure_ascii=False)
    assert "oc_untrusted_embedded_chat" not in serialized_wire
    assert "/Users/everflow" not in serialized_wire
    assert "images/card0_img0.png" not in serialized_wire

    # Verify card JSON is identical to original_card except __img__.path normalized to artifact:<id>
    expected_card = copy.deepcopy(original_card)
    expected_card["body"]["elements"][1]["path"] = "artifact:card0-img0"
    expected_card["body"]["elements"][3]["elements"][1]["path"] = "artifact:card0-img1"
    assert wire_item["card"] == expected_card

    assert [img["artifact_id"] for img in wire_item["images"]] == ["card0-img0", "card0-img1"]
    assert wire_item["images"][0]["sha256"] == hashlib.sha256(img_main_bytes).hexdigest()
    assert wire_item["images"][1]["sha256"] == hashlib.sha256(img_nested_bytes).hexdigest()

    # Unresolved __img__ placeholder must be rejected before any send
    broken_outbox = copy.deepcopy(raw_outbox)
    broken_outbox["feishu_cards"][0]["images"].pop()
    with pytest.raises(DataTaskArtifactError, match="Unresolved __img__ placeholder"):
        build_data_task_delivery_plan(
            run_id=run_id,
            job_key="sentiment-dashboard-digest",
            raw_outbox=broken_outbox,
            run_artifacts_dir=run_dir,
        )


@pytest.mark.asyncio
async def test_t27_card2_rendering_four_chart_snapshot_immutability_and_missing_chart_failure(
    tmp_path: Path,
) -> None:
    """Requirement 3 & 5: T27 captures 4 CN/KR/JP/TW charts in section order, isolates snapshots from later overwrites, and fails explicitly if any chart is missing."""
    ws_dir = tmp_path / "openclaw-data"
    orig_charts = _write_synthetic_t27_charts(ws_dir)
    md_text = _make_synthetic_t27_markdown()

    run_id_prod = str(uuid4())
    run_id_preview = str(uuid4())
    prod_dir = ws_dir / ".dalton-runs" / run_id_prod
    preview_dir = ws_dir / ".dalton-runs" / run_id_preview

    secret_val = "SK-SYNTHETIC-SECRET-TOKEN-999"
    redactor = SecretRedactor([secret_val])
    md_with_secret = md_text + f"\n- 调试备注：{secret_val}"

    prod_outbox = persist_redacted_outbox(
        outbox_path=prod_dir / "outbox.json",
        redactor=redactor,
        job_key=T27_JOB_KEY,
        stdout_text=md_with_secret,
        workspace_dir=ws_dir,
    )
    preview_outbox = persist_redacted_outbox(
        outbox_path=preview_dir / "outbox.json",
        redactor=redactor,
        job_key=T27_JOB_KEY,
        stdout_text=md_with_secret,
        workspace_dir=ws_dir,
    )
    assert prod_outbox is not None and preview_outbox is not None
    # Preview and production preparation produce identical Card 2.0 & digest structures
    assert prod_outbox == preview_outbox
    assert secret_val not in json.dumps(prod_outbox, ensure_ascii=False)
    assert "[REDACTED]" in json.dumps(prod_outbox, ensure_ascii=False)

    card = prod_outbox["feishu_cards"][0]["card"]
    assert card["schema"] == "2.0"
    assert card["header"]["title"]["content"] == "亚洲四地杠杆/拥挤度监控 — 2026-11-09"
    assert card["header"]["subtitle"]["content"] == "合成测试数据 ｜ 四市场截面"
    assert card["header"]["text_tag_list"] == [
        {
            "tag": "text_tag",
            "text": {"tag": "plain_text", "content": "实时监控"},
            "color": "wathet",
        }
    ]

    # Verify element ordering: CN text -> CN img -> hr -> KR text -> KR img -> hr -> JP text -> JP img -> hr -> TW text -> TW img -> hr -> notation
    elems = card["body"]["elements"]
    img_indices = [i for i, el in enumerate(elems) if el.get("tag") == "__img__"]
    assert len(img_indices) == 4
    assert "中国A股" in elems[img_indices[0] - 1]["content"]
    assert "韩国" in elems[img_indices[1] - 1]["content"]
    assert "日本" in elems[img_indices[2] - 1]["content"]
    assert "台湾" in elems[img_indices[3] - 1]["content"]
    assert elems[-1]["tag"] == "markdown"
    assert elems[-1]["text_size"] == "notation"
    assert "口径说明" in elems[-1]["content"]

    # Overwrite source *_latest.png charts on the data volume (simulating a later run)
    charts_dir = ws_dir / "temp" / "asia-leverage-crossview" / "charts"
    for code in ("cn", "kr", "jp", "tw"):
        (charts_dir / f"{code}_latest.png").write_bytes(
            _make_synthetic_png(f"LATER-OVERWRITE-{code}".encode("ascii"))
        )

    # Completed run's delivery plan and artifact bytes must still match the original run's snapshot!
    plan = build_data_task_delivery_plan(
        run_id=run_id_prod,
        job_key=T27_JOB_KEY,
        raw_outbox=prod_outbox,
        run_artifacts_dir=prod_dir,
    )
    for idx, code in enumerate(("cn", "kr", "jp", "tw")):
        art = read_validated_run_artifact(
            run_id=run_id_prod,
            artifact_id=f"card0-img{idx}",
            plan=plan,
            run_artifacts_dir=prod_dir,
        )
        assert art.content == orig_charts[f"{code}_latest.png"]
        assert art.sha256 == hashlib.sha256(orig_charts[f"{code}_latest.png"]).hexdigest()

    # Missing required chart fails preparation explicitly (and wrapper exits non-zero)
    (charts_dir / "jp_latest.png").unlink()
    missing_run_id = str(uuid4())
    err_sink = io.StringIO()
    out_sink = io.StringIO()
    script_file = tmp_path / "emit_t27.py"
    script_file.write_text(
        f"import sys\nsys.stdout.write({md_text!r})\n",
        encoding="utf-8",
    )
    wrap_res = await run_wrapped_command(
        [sys.executable, str(script_file)],
        run_id=missing_run_id,
        job_key=T27_JOB_KEY,
        workdir=str(ws_dir),
        workspace_dir=str(ws_dir),
        secrets_dir=str(tmp_path / "empty_secrets"),
        outbox_tmp_dir=str(tmp_path / "tmp_outbox"),
        stdout_sink=out_sink,
        stderr_sink=err_sink,
    )
    assert wrap_res.exit_code == 1
    assert wrap_res.outbox_payload is None
    assert not (ws_dir / ".dalton-runs" / missing_run_id / "outbox.json").exists()
    assert "jp_latest.png" in wrap_res.redacted_stderr

    # Local Markdown image link syntax is explicitly rejected
    _write_synthetic_t27_charts(ws_dir)
    md_local_link = md_text + "\n![bad](/tmp/local.png)"
    with pytest.raises(DataTaskNotificationPrepError, match="Local Markdown image links"):
        prepare_t27_run_outbox(
            md_local_link,
            workspace_dir=ws_dir,
            run_artifacts_dir=ws_dir / ".dalton-runs" / str(uuid4()),
        )


def test_t26_two_ordered_items_size_and_sha256_verification(tmp_path: Path) -> None:
    """Requirement 4 & 5: T26 preserves 2 ordered text+chart items, checks recorded size & SHA256, and ignores embedded target/workspace_media."""
    run_id = str(uuid4())
    other_run_id = str(uuid4())
    run_dir = tmp_path / ".dalton-runs" / run_id
    (run_dir / "attachments").mkdir(parents=True)

    chart0 = _make_synthetic_png(b"T26-TOP20-CHART0")
    chart1 = _make_synthetic_png(b"T26-BOT20-CHART1")
    (run_dir / "attachments" / "0.png").write_bytes(chart0)
    (run_dir / "attachments" / "1.png").write_bytes(chart1)

    # Supply items reversed in JSON array to prove `index` ordering (`0` then `1`)
    raw_outbox = {
        "items": [
            {
                "index": 1,
                "text": "📊 US Bottom 20 DeMark Digest (Synthetic)",
                "media": "attachments/1.png",
                "chart_bytes": len(chart1),
                "chart_sha256": hashlib.sha256(chart1).hexdigest(),
                "workspace_media": f".dalton-runs/{other_run_id}/attachments/1.png",
                "channel": "discord",
                "target": "user:999999999999999999",
            },
            {
                "index": 0,
                "text": "📊 US Top 20 DeMark Digest (Synthetic)",
                "media": "attachments/0.png",
                "chart_bytes": len(chart0),
                "chart_sha256": hashlib.sha256(chart0).hexdigest(),
                "workspace_media": f".dalton-runs/{other_run_id}/attachments/0.png",
                "channel": "discord",
                "target": "user:999999999999999999",
            },
        ]
    }

    plan = build_data_task_delivery_plan(
        run_id=run_id,
        job_key=T26_JOB_KEY,
        raw_outbox=raw_outbox,
        run_artifacts_dir=run_dir,
    )
    assert len(plan.items) == 2
    assert [it.item_index for it in plan.items] == [0, 1]
    assert [it.kind for it in plan.items] == ["text_and_file", "text_and_file"]
    assert "Top 20" in plan.items[0].text
    assert "Bottom 20" in plan.items[1].text

    art0 = read_validated_run_artifact(
        run_id=run_id,
        artifact_id="item0-media0",
        plan=plan,
        run_artifacts_dir=run_dir,
    )
    art1 = read_validated_run_artifact(
        run_id=run_id,
        artifact_id="item1-media0",
        plan=plan,
        run_artifacts_dir=run_dir,
    )
    assert art0.content == chart0
    assert art1.content == chart1

    # Tampering with chart bytes on disk after plan creation is caught by SHA256/size verification
    (run_dir / "attachments" / "1.png").write_bytes(_make_synthetic_png(b"TAMPERED-BYTES!!"))
    with pytest.raises(DataTaskArtifactError) as exc_info:
        read_validated_run_artifact(
            run_id=run_id,
            artifact_id="item1-media0",
            plan=plan,
            run_artifacts_dir=run_dir,
        )
    assert exc_info.value.code in ("artifact_sha256_mismatch", "artifact_size_mismatch")

    # T26 missing second item or missing media is rejected (never text-only substitution)
    single_item_outbox = {"items": [raw_outbox["items"][1]]}
    with pytest.raises(DataTaskNotificationError, match="requires exactly 2"):
        build_data_task_delivery_plan(
            run_id=run_id,
            job_key=T26_JOB_KEY,
            raw_outbox=single_item_outbox,
            run_artifacts_dir=run_dir,
        )


def test_artifact_security_containment_symlinks_urls_oversized_and_router_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 1 & 5: Symlink traversal, URLs, '..' escape, cross-run access, oversized files, and extra caller fields are rejected."""
    run_id = str(uuid4())
    other_run_id = str(uuid4())
    run_dir = tmp_path / ".dalton-runs" / run_id
    (run_dir / "images").mkdir(parents=True)

    outside_file = tmp_path / "outside_secret.png"
    outside_file.write_bytes(_make_synthetic_png(b"OUTSIDE-SECRET"))

    # 1. Leaf symlink escaping run_dir -> rejected via O_NOFOLLOW
    leaf_link = run_dir / "images" / "escaped_leaf.png"
    leaf_link.symlink_to(outside_file)
    with pytest.raises(DataTaskArtifactError) as sym_exc:
        build_data_task_delivery_plan(
            run_id=run_id,
            job_key="softbank-nav-monitor",
            raw_outbox={
                "feishu_cards": [
                    {
                        "card": {"schema": "2.0", "body": {"elements": [{"tag": "__img__", "path": "p0"}]}},
                        "images": [{"placeholder": "p0", "path": "images/escaped_leaf.png"}],
                    }
                ]
            },
            run_artifacts_dir=run_dir,
        )
    assert sym_exc.value.code == "symlink_rejected"

    # 2. Intermediate directory symlink -> rejected via dir_fd O_NOFOLLOW walk
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "chart.png").write_bytes(_make_synthetic_png(b"OUTSIDE-DIR-IMG"))
    (run_dir / "symdir").symlink_to(outside_dir)
    with pytest.raises(DataTaskArtifactError) as dir_sym_exc:
        build_data_task_delivery_plan(
            run_id=run_id,
            job_key="softbank-nav-monitor",
            raw_outbox={
                "feishu_cards": [
                    {
                        "card": {"schema": "2.0", "body": {"elements": [{"tag": "__img__", "path": "p0"}]}},
                        "images": [{"placeholder": "p0", "path": "symdir/chart.png"}],
                    }
                ]
            },
            run_artifacts_dir=run_dir,
        )
    assert dir_sym_exc.value.code == "symlink_rejected"

    # 3. Path traversal `..`, absolute path, and URL input -> rejected
    for bad_path, expected_code in (
        ("../outside_secret.png", "path_traversal"),
        (f"../{other_run_id}/images/0.png", "path_traversal"),
        ("https://evil.example/chart.png", "url_rejected"),
        ("file:///etc/passwd", "url_rejected"),
    ):
        with pytest.raises(DataTaskArtifactError) as bad_exc:
            build_data_task_delivery_plan(
                run_id=run_id,
                job_key="softbank-nav-monitor",
                raw_outbox={
                    "feishu_cards": [
                        {
                            "card": {"schema": "2.0", "body": {"elements": [{"tag": "__img__", "path": "p0"}]}},
                            "images": [{"placeholder": "p0", "path": bad_path}],
                        }
                    ]
                },
                run_artifacts_dir=run_dir,
            )
        assert bad_exc.value.code == expected_code

    # 4. Unsupported format (HTML disguised as .png), directory target, missing file, oversized file, & cross-run read rejection
    disguised = run_dir / "images" / "fake.png"
    disguised.write_bytes(b"<!doctype html><html>not an image</html>")
    with pytest.raises(DataTaskArtifactError) as mime_exc:
        build_data_task_delivery_plan(
            run_id=run_id,
            job_key="softbank-nav-monitor",
            raw_outbox={
                "feishu_cards": [
                    {
                        "card": {"schema": "2.0", "body": {"elements": [{"tag": "__img__", "path": "p0"}]}},
                        "images": [{"placeholder": "p0", "path": "images/fake.png"}],
                    }
                ]
            },
            run_artifacts_dir=run_dir,
        )
    assert mime_exc.value.code == "unsupported_media_type"

    dir_target = run_dir / "images" / "subdir.png"
    dir_target.mkdir()
    with pytest.raises(DataTaskArtifactError) as dir_target_exc:
        _safe_read_contained_regular_file(run_dir, "images/subdir.png")
    assert dir_target_exc.value.code in ("unsupported_media_type", "symlink_rejected")

    with pytest.raises(DataTaskArtifactError) as missing_exc:
        _safe_read_contained_regular_file(run_dir, "images/does_not_exist.png")
    assert missing_exc.value.code == "artifact_missing"

    oversized_img = run_dir / "images" / "oversized.png"
    oversized_img.write_bytes(_make_synthetic_png(b"BIG" * 64))
    with pytest.raises(DataTaskArtifactError) as over_exc:
        _safe_read_contained_regular_file(run_dir, "images/oversized.png", max_bytes=32)
    assert over_exc.value.code == "artifact_too_large"

    valid_img = _make_synthetic_png(b"VALID-IMG")
    (run_dir / "images" / "ok.png").write_bytes(valid_img)
    ok_plan = build_data_task_delivery_plan(
        run_id=run_id,
        job_key="softbank-nav-monitor",
        raw_outbox={
            "feishu_cards": [
                {
                    "card": {"schema": "2.0", "body": {"elements": [{"tag": "__img__", "path": "p0"}]}},
                    "images": [{"placeholder": "p0", "path": "images/ok.png"}],
                }
            ]
        },
        run_artifacts_dir=run_dir,
    )
    with pytest.raises(DataTaskArtifactError) as cross_exc:
        read_validated_run_artifact(
            run_id=other_run_id,
            artifact_id="card0-img0",
            plan=ok_plan,
            run_artifacts_dir=run_dir,
        )
    assert cross_exc.value.code == "cross_run_denied"

    with pytest.raises(DataTaskArtifactNotFoundError):
        read_validated_run_artifact(
            run_id=run_id,
            artifact_id="card9-img9",
            plan=ok_plan,
            run_artifacts_dir=run_dir,
        )

    # 5. TOCTOU protection: replacing validated regular file with a symlink before read_validated_run_artifact
    (run_dir / "images" / "ok.png").unlink()
    (run_dir / "images" / "ok.png").symlink_to(outside_file)
    with pytest.raises(DataTaskArtifactError) as toctou_exc:
        read_validated_run_artifact(
            run_id=run_id,
            artifact_id="card0-img0",
            plan=ok_plan,
            run_artifacts_dir=run_dir,
        )
    assert toctou_exc.value.code == "symlink_rejected"

    # 6. Internal FastAPI router rejects missing/wrong X-Service-Token and arbitrary caller fields
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    app = FastAPI()
    app.include_router(dalton_router)
    client = TestClient(app)

    res_unauth = client.post(
        "/api/v1/dalton/channels/internal/data-task-notifications/plan",
        json={"run_id": run_id, "target": "feishu:@me"},
    )
    assert res_unauth.status_code in (401, 403)

    for forbidden_field, forbidden_val in (
        ("card", {"schema": "2.0"}),
        ("files", ["/etc/passwd"]),
        ("url", "https://evil.example"),
        ("user_id", "admin-override"),
        ("succeeded", True),
    ):
        res_extra = client.post(
            "/api/v1/dalton/channels/internal/data-task-notifications/plan",
            headers={"X-Service-Token": SERVICE_TOKEN},
            json={"run_id": run_id, "target": "feishu:@me", forbidden_field: forbidden_val},
        )
        assert res_extra.status_code == 422


@pytest.mark.asyncio
async def test_gateway_transport_and_wrapping_layers_preserve_uncertain_state_and_non_data_routing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 6, 7 & 9: Pre-send failure is retryable; post-send timeout stays uncertain (never converted to failed); non-data notifications retain bridge transport."""
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("CHANNEL_GATEWAY_URL", "http://127.0.0.1:8962")

    run_id = str(uuid4())
    target = "feishu:chat:oc_synth_group"

    # 1. ConnectError -> definite failed (retryable)
    def _connect_err_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_connect_err_handler)) as client_conn_err:
        res_conn_err = await ct_mod.dispatch_data_task_target_via_gateway(
            run_id=run_id,
            target=target,
            http_client=client_conn_err,
        )
    assert res_conn_err["status"] == "failed"
    assert res_conn_err["uncertain"] is False

    # 2. ReadTimeout after request sent -> uncertain ("unknown"), never "failed"
    sent_wire_bodies: list[dict[str, Any]] = []

    def _read_timeout_handler(request: httpx.Request) -> httpx.Response:
        sent_wire_bodies.append(json.loads(request.content.decode("utf-8")))
        raise httpx.ReadTimeout("Gateway timed out after request sent", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(_read_timeout_handler)) as client_timeout:
        res_timeout = await ct_mod.post_data_task_gateway_notify(
            [target],
            text="IGNORED_CALLER_TEXT",
            card={"schema": "2.0", "ignored": True},
            run_id=run_id,
            http_client=client_timeout,
        )
    # Wire request carries ONLY run_id and target
    assert sent_wire_bodies == [{"run_id": run_id, "target": target}]
    assert res_timeout["status"] == "unknown"
    assert res_timeout["uncertain"] is True

    # 3. Wrapping scheduler dispatch_data_task_run_notification keeps uncertain target as `pending` (`delivery_uncertain`), NEVER `failed`
    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    art_dir = path_map.backend_run_artifacts_dir(run_id)
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "outbox.json").write_text(
        json.dumps({"feishu_message": "Synthetic digest"}),
        encoding="utf-8",
    )

    import src.dalton.data_jobs.scheduler as sched_mod

    monkeypatch.setattr(
        sched_mod,
        "get_configured_pm_user_ids",
        create_autospec(sched_mod.get_configured_pm_user_ids, return_value=["pm-synth-1"]),
    )

    marked_calls: list[dict[str, Any]] = []

    async def _fake_mark(
        execution_id: str,
        channel: str,
        tgt: str,
        *,
        status: str,
        error_code: str | None = None,
        reason: str | None = None,
        expected_status: Any = None,
    ) -> bool:
        marked_calls.append(
            {
                "execution_id": execution_id,
                "channel": channel,
                "target": tgt,
                "status": status,
                "error_code": error_code,
                "reason": reason,
                "expected_status": expected_status,
            }
        )
        return True

    mark_double = create_autospec(notify_dispatch_mod.mark_target_delivery, side_effect=_fake_mark)
    claim_double = create_autospec(notify_dispatch_mod.claim_target_delivery, return_value=True)
    eval_double = create_autospec(
        notify_dispatch_mod.evaluate_user_notify_eligibility,
        return_value=(True, None, [target]),
    )
    fetch_double = create_autospec(notify_dispatch_mod.fetch_execution_deliveries, return_value=[])

    async def _fake_gateway_unknown(
        targets: Any,
        text: str = "",
        card: dict[str, Any] | None = None,
        *,
        images: list[dict[str, Any]] | None = None,
        run_id: str | None = None,
        gateway_url: str | None = None,
        service_token: str | None = None,
        timeout_s: float | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        _ = (text, card, images, run_id, gateway_url, service_token, timeout_s, http_client)
        return {
            "ok": False,
            "status": "unknown",
            "uncertain": True,
            "status_code": 200,
            "error_code": "in_flight",
            "results": [{"target": targets[0], "ok": False, "status": "unknown", "uncertain": True, "error_code": "in_flight"}],
        }

    monkeypatch.setattr(
        sched_mod,
        "post_data_task_gateway_notify",
        create_autospec(ct_mod.post_data_task_gateway_notify, side_effect=_fake_gateway_unknown),
    )

    from contextlib import asynccontextmanager
    import psycopg

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    cursor.fetchone.return_value = (1,)
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.closed = False

    @asynccontextmanager
    async def _cur_cm(*args: Any, **kwargs: Any):
        yield cursor

    @asynccontextmanager
    async def _tx_cm(*args: Any, **kwargs: Any):
        yield

    conn.cursor.side_effect = _cur_cm
    conn.transaction.side_effect = _tx_cm

    terminal = await sched_mod.dispatch_data_task_run_notification(
        conn,
        lock_conn=conn,
        run_id=run_id,
        job_key="megacap-relative-signal-monitor",
        path_map=path_map,
        evaluate_eligibility_fn=eval_double,
        claim_target_fn=claim_double,
        post_notify_fn=sched_mod.post_data_task_gateway_notify,
        mark_target_fn=mark_double,
        fetch_deliveries_fn=fetch_double,
    )
    assert terminal == "pending_dispatch"
    assert len(marked_calls) == 1
    assert marked_calls[0]["status"] == "pending"
    assert marked_calls[0]["error_code"] == "in_flight"

    # 4. Non-data notification (`on_automation_settled`) still uses `post_bridge_notify` and never uses data-task transport
    data_task_gw_spy = create_autospec(notify_dispatch_mod.post_data_task_gateway_notify)
    monkeypatch.setattr(notify_dispatch_mod, "post_data_task_gateway_notify", data_task_gw_spy)

    bridge_double = create_autospec(
        notify_dispatch_mod.post_bridge_notify,
        return_value={"ok": True, "status_code": 200, "results": [{"target": target, "ok": True}]},
    )
    settled_ok = await notify_dispatch_mod.on_automation_settled(
        automation={
            "automation_id": str(uuid4()),
            "user_id": "pm-synth-1",
            "name": "dalton-nvda-earn-2026q3-t0",
        },
        execution_id=str(uuid4()),
        final_text="## 结论\n合成财报点评",
        resolve_enabled_fn=create_autospec(notify_dispatch_mod.resolve_dalton_notify_enabled, return_value=True),
        resolve_overrides_fn=create_autospec(notify_dispatch_mod.resolve_dalton_notify_overrides, return_value={}),
        resolve_targets_fn=create_autospec(notify_dispatch_mod.resolve_dalton_notify_targets, return_value=[]),
        resolve_routes_fn=create_autospec(
            notify_dispatch_mod.resolve_dalton_notify_routes,
            return_value={"earnings_t0": [target]},
        ),
        is_admin_fn=create_autospec(notify_dispatch_mod._default_notify_admin, return_value=True),
        get_verdict_fn=create_autospec(notify_dispatch_mod.get_verdict_for_execution, return_value=None),
        claim_target_fn=create_autospec(notify_dispatch_mod.claim_target_delivery, return_value=True),
        mark_target_fn=create_autospec(notify_dispatch_mod.mark_target_delivery, return_value=True),
        post_notify_fn=bridge_double,
        frontend_url_fn=lambda: "",
    )
    assert settled_ok is True
    bridge_double.assert_awaited_once()
    assert bridge_double.await_args.args[0] == [target]
    data_task_gw_spy.assert_not_awaited()


def test_channel_gateway_url_container_to_host_loopback_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify backend container rewrites loopback CHANNEL_GATEWAY_URL to HOST_IP while host tests keep 127.0.0.1."""
    import src.dalton.settings as settings_mod

    orig_exists = os.path.exists
    monkeypatch.setenv("CHANNEL_GATEWAY_URL", "http://127.0.0.1:8962")
    monkeypatch.setattr(settings_mod, "HOST_IP", "host.docker.internal")

    monkeypatch.setattr(
        settings_mod.os.path,
        "exists",
        create_autospec(orig_exists, side_effect=lambda p: p == "/.dockerenv"),
    )
    assert get_dalton_channel_gateway_url() == "http://host.docker.internal:8962"

    monkeypatch.setattr(
        settings_mod.os.path,
        "exists",
        create_autospec(orig_exists, return_value=False),
    )
    assert get_dalton_channel_gateway_url() == "http://127.0.0.1:8962"


@pytest.mark.asyncio
async def test_group_authorization_succeeds_without_personal_identity_while_dm_stays_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R2 §1: Administrator group report succeeds without `dalton_channel_identities`, omitting `owner_platform_user_id`, while personal DM without binding fails `not_linked`."""
    from contextlib import asynccontextmanager
    import psycopg

    run_id = str(uuid4())
    pm_uid = "pm-unbound-synth"
    group_target = "feishu:chat:oc_synth_admin_group"

    path_map = build_path_map(host_var_dir=tmp_path / "h", backend_var_dir=tmp_path / "b")
    rdir = path_map.backend_run_artifacts_dir(run_id)
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "outbox.json").write_text(
        json.dumps({"feishu_message": "Synthetic group report"}),
        encoding="utf-8",
    )

    executed_sql: list[str] = []

    async def _sql_handler(sql: str, params: Any = None) -> None:
        _ = params
        norm = " ".join(sql.split())
        executed_sql.append(norm)
        if "FROM dalton_data_task_runs r" in norm:
            cursor.fetchone.return_value = {
                "run_id": run_id,
                "job_key": "megacap-relative-signal-monitor",
                "status": "succeeded",
                "notify_status": "pending_dispatch",
                "outbox_payload": None,
                "log_tail": None,
                "def_job_key": "megacap-relative-signal-monitor",
                "task_enabled": True,
                "task_notify_enabled": True,
            }
        elif "FROM dalton_channel_identities" in norm:
            cursor.fetchone.return_value = None
        elif "FROM dalton_channel_deliveries WHERE delivery_key = ANY" in norm:
            cursor.fetchall.return_value = []
        else:
            cursor.fetchone.return_value = None

    cursor = create_autospec(psycopg.AsyncCursor, instance=True)
    cursor.execute.side_effect = _sql_handler
    conn = create_autospec(psycopg.AsyncConnection, instance=True)
    conn.closed = False

    @asynccontextmanager
    async def _cur_cm(*args: Any, **kwargs: Any):
        yield cursor

    conn.cursor.side_effect = _cur_cm

    monkeypatch.setenv("DALTON_PM_USER_IDS", pm_uid)
    monkeypatch.setattr(
        dtn_db,
        "evaluate_user_notify_eligibility",
        create_autospec(
            dtn_db.evaluate_user_notify_eligibility,
            return_value=(True, None, [group_target, "feishu:user:ou_unlinked_legacy"]),
        ),
    )
    monkeypatch.setattr(
        dtn_db,
        "_verify_group_target_deliverable",
        create_autospec(
            dtn_db._verify_group_target_deliverable,
            return_value=(True, None, "Synth Admin Group"),
        ),
    )

    # 1. Group target succeeds with ZERO `dalton_channel_identities` queries and omits `owner_platform_user_id`
    grp_plan = await dtn_db.authorize_data_task_notification_plan(
        run_id=run_id,
        target=group_target,
        path_map=path_map,
        conn=conn,
    )
    assert grp_plan["authorized"] is True
    assert grp_plan["owner_user_id"] == pm_uid
    assert "owner_platform_user_id" not in grp_plan
    assert grp_plan["target_kind"] == "group"
    assert grp_plan["canonical_target_address"] == "feishu:oc_synth_admin_group"
    assert grp_plan["recipient_id"] == "oc_synth_admin_group"
    assert grp_plan["items"][0]["delivery_key"] == (
        f"data_task:{run_id}:feishu:oc_synth_admin_group:item:0"
    )
    assert not any("dalton_channel_identities" in q for q in executed_sql)

    # 2. Personal DM target for the same unbound administrator is still denied with `not_linked`
    dm_plan = await dtn_db.authorize_data_task_notification_plan(
        run_id=run_id,
        target="feishu:user:ou_unlinked_legacy",
        path_map=path_map,
        conn=conn,
    )
    assert dm_plan["authorized"] is False
    assert dm_plan["code"] == "not_linked"
    assert any("dalton_channel_identities" in q for q in executed_sql)


def test_dedicated_feishu_vault_credentials_endpoint_and_log_redaction(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R2 §2: Narrow Vault startup endpoint enforces service auth, rejects caller overrides, sets no-store, and never logs secret values."""
    import logging

    import src.dalton.services.channel_vault_credentials as vault_creds_mod

    pm_uid = "pm-vault-admin-synth"
    synth_app_id = "cli_synth_vault_app_9001"
    synth_app_secret = "sec_synth_vault_secret_never_logged_9001"

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DALTON_PM_USER_IDS", pm_uid)

    orig_get_secrets = vault_creds_mod.get_user_secrets_decrypted
    secrets_calls: list[tuple[str, tuple[str, ...]]] = []

    async def _fake_get_secrets_ok(
        user_id: str,
        names: Any = None,
    ) -> dict[str, str]:
        secrets_calls.append((user_id, tuple(names or ())))
        return {
            "DALTON_FEISHU_APP_ID": synth_app_id,
            "DALTON_FEISHU_APP_SECRET": synth_app_secret,
            "UNREQUESTED_EXTRA_KEY": "should_never_be_returned",
        }

    monkeypatch.setattr(
        vault_creds_mod,
        "get_user_secrets_decrypted",
        create_autospec(orig_get_secrets, side_effect=_fake_get_secrets_ok),
    )

    app = FastAPI()
    app.include_router(dalton_router)
    client = TestClient(app)
    endpoint = "/api/v1/dalton/channels/internal/data-task-notifications/feishu-credentials"

    # 1. Invalid or missing service token -> 401/403
    assert client.post(endpoint, json={}).status_code in (401, 403)
    assert (
        client.post(endpoint, headers={"X-Service-Token": "wrong"}, json={}).status_code
        in (401, 403)
    )

    # 2. Caller-supplied query params, X-User-Id header, or body fields (`user_id`, `names`) -> rejected without reading Vault
    assert (
        client.post(
            f"{endpoint}?user_id=other_user",
            headers={"X-Service-Token": SERVICE_TOKEN},
            json={},
        ).status_code
        == 400
    )
    assert (
        client.post(
            endpoint,
            headers={"X-Service-Token": SERVICE_TOKEN, "X-User-Id": "other_user"},
            json={},
        ).status_code
        == 400
    )
    for bad_body in (
        {"user_id": "other_user"},
        {"names": ["DALTON_FEISHU_APP_ID", "OTHER_SECRET"]},
    ):
        assert (
            client.post(
                endpoint,
                headers={"X-Service-Token": SERVICE_TOKEN},
                json=bad_body,
            ).status_code
            == 422
        )
    assert secrets_calls == []

    # 3. Valid empty-body request loads strictly the 2 keys for configured primary admin with `Cache-Control: no-store`
    with caplog.at_level(logging.DEBUG):
        ok_res = client.post(
            endpoint,
            headers={"X-Service-Token": SERVICE_TOKEN},
            json={},
        )
    assert ok_res.status_code == 200
    assert ok_res.headers.get("cache-control") == "no-store"
    assert ok_res.json() == {
        "credentials": {
            "DALTON_FEISHU_APP_ID": synth_app_id,
            "DALTON_FEISHU_APP_SECRET": synth_app_secret,
        }
    }
    assert secrets_calls == [
        (pm_uid, ("DALTON_FEISHU_APP_ID", "DALTON_FEISHU_APP_SECRET"))
    ]
    assert synth_app_secret not in caplog.text

    # 4. Missing credential returns 503 naming only the missing key, never returning partial values
    monkeypatch.setattr(
        vault_creds_mod,
        "get_user_secrets_decrypted",
        create_autospec(
            orig_get_secrets,
            return_value={"DALTON_FEISHU_APP_ID": synth_app_id},
        ),
    )
    with caplog.at_level(logging.DEBUG):
        miss_res = client.post(
            endpoint,
            headers={"X-Service-Token": SERVICE_TOKEN},
            json={},
        )
    assert miss_res.status_code == 503
    assert miss_res.headers.get("cache-control") == "no-store"
    assert miss_res.json()["code"] == "vault_credentials_missing"
    assert miss_res.json()["missing"] == ["DALTON_FEISHU_APP_SECRET"]
    assert synth_app_id not in miss_res.text
    assert synth_app_id not in caplog.text

    # 5. Vault decryption error whose exception text contains a secret never leaks into HTTP body or logs
    async def _failing_decrypt(user_id: str, names: Any = None) -> dict[str, str]:
        _ = (user_id, names)
        raise RuntimeError(f"pgcrypto failure near {synth_app_secret}")

    monkeypatch.setattr(
        vault_creds_mod,
        "get_user_secrets_decrypted",
        create_autospec(orig_get_secrets, side_effect=_failing_decrypt),
    )
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        err_res = client.post(
            endpoint,
            headers={"X-Service-Token": SERVICE_TOKEN},
            json={},
        )
    assert err_res.status_code == 503
    assert err_res.json()["code"] == "vault_unavailable"
    assert synth_app_secret not in err_res.text
    assert synth_app_secret not in caplog.text


def test_launchd_template_contains_no_plaintext_feishu_or_service_secrets() -> None:
    """R2 §2: LaunchAgent plist template uses Vault credential source and owner-only env file, storing no plaintext credentials."""
    import plistlib

    project_root = Path(__file__).resolve().parents[3]
    plist_path = (
        project_root
        / "scripts"
        / "dalton"
        / "launchd"
        / "com.dalton.channel-gateway.plist.template"
    )
    raw_bytes = plist_path.read_bytes()
    parsed = plistlib.loads(raw_bytes)
    env_vars = parsed["EnvironmentVariables"]

    assert env_vars["GATEWAY_HOST"] == "127.0.0.1"
    assert env_vars["DALTON_FEISHU_CREDENTIAL_SOURCE"] == "vault"
    assert env_vars["DALTON_CHANNEL_GATEWAY_ENV_FILE"] == (
        "__HOME__/.config/dalton/channel-gateway.env"
    )
    for forbidden_key in (
        "DALTON_FEISHU_APP_ID",
        "DALTON_FEISHU_APP_SECRET",
        "DALTON_DISCORD_BOT_TOKEN",
        "INTERNAL_SERVICE_TOKEN",
    ):
        assert forbidden_key not in env_vars


def test_artifact_fifo_rejected_promptly_without_blocking_in_isolated_subprocess() -> None:
    """R3 §1: Writerless FIFO artifact leaf is rejected immediately via nonblocking open + fstat without stalling."""
    import shutil
    import subprocess
    import tempfile

    project_root = Path(__file__).resolve().parents[3]
    tmp_dir = Path(tempfile.mkdtemp(prefix="dalton-fifo-reg-")).resolve()
    proc: subprocess.Popen[str] | None = None
    try:
        images_dir = tmp_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)
        fifo_path = images_dir / "chart.png"
        os.mkfifo(fifo_path)

        child_script = "\n".join(
            [
                "import json",
                "from pathlib import Path",
                "from uuid import uuid4",
                "from src.dalton.data_jobs.notification_payloads import (",
                "    DataTaskArtifactError,",
                "    _safe_read_contained_regular_file,",
                "    build_data_task_delivery_plan,",
                ")",
                f"root = Path({str(tmp_dir)!r})",
                "direct_code = None",
                "try:",
                "    _safe_read_contained_regular_file(root, 'images/chart.png')",
                "except DataTaskArtifactError as exc:",
                "    direct_code = exc.code",
                "plan_code = None",
                "try:",
                "    build_data_task_delivery_plan(",
                "        run_id=str(uuid4()),",
                "        job_key='softbank-nav-monitor',",
                "        raw_outbox={",
                "            'feishu_cards': [{",
                "                'card': {'schema': '2.0', 'body': {'elements': [{'tag': '__img__', 'path': 'p0'}]}},",
                "                'images': [{'placeholder': 'p0', 'path': 'images/chart.png'}],",
                "            }]",
                "        },",
                "        run_artifacts_dir=root,",
                "    )",
                "except DataTaskArtifactError as exc:",
                "    plan_code = exc.code",
                "print(json.dumps({'direct_code': direct_code, 'plan_code': plan_code}), flush=True)",
            ]
        )

        proc = subprocess.Popen(
            [sys.executable, "-c", child_script],
            cwd=project_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout_text, stderr_text = proc.communicate(timeout=3.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5.0)
            pytest.fail(
                "FIFO artifact open blocked in isolated child subprocess beyond 3.0s hard timeout"
            )

        assert proc.returncode == 0, f"Child subprocess failed: {stderr_text}"
        result = json.loads(stdout_text.strip().splitlines()[-1])
        assert result == {
            "direct_code": "unsupported_media_type",
            "plan_code": "unsupported_media_type",
        }
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    assert not tmp_dir.exists()
