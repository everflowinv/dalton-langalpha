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
import struct
import sys
from typing import Any
from unittest.mock import create_autospec
from uuid import uuid4
import zlib

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


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)


def _make_valid_png_bytes(
    *,
    width: int = 8,
    height: int = 8,
    rgb: tuple[int, int, int] = (20, 90, 180),
) -> bytes:
    """Build a spec-compliant PNG with uncompressed deflate (`level=0`) so equal dimensions have equal byte length."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    raw_scanlines = row * height
    idat = zlib.compress(raw_scanlines, level=0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


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

    from src.dalton.data_jobs.notification_payloads import (
        prepare_run_outbox_image_metadata,
    )

    prepared_outbox = prepare_run_outbox_image_metadata(
        raw_outbox,
        run_artifacts_dir=run_dir,
        job_key="sentiment-dashboard-digest",
    )
    assert prepared_outbox is not None
    assert prepared_outbox["feishu_cards"][0]["card"] == original_card
    assert prepared_outbox["feishu_cards"][0]["images"][0]["size_bytes"] == len(img_main_bytes)
    assert (
        prepared_outbox["feishu_cards"][0]["images"][0]["sha256"]
        == hashlib.sha256(img_main_bytes).hexdigest()
    )
    assert prepared_outbox["feishu_cards"][0]["images"][1]["size_bytes"] == len(img_nested_bytes)
    assert (
        prepared_outbox["feishu_cards"][0]["images"][1]["sha256"]
        == hashlib.sha256(img_nested_bytes).hexdigest()
    )

    plan = build_data_task_delivery_plan(
        run_id=run_id,
        job_key="sentiment-dashboard-digest",
        raw_outbox=prepared_outbox,
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
    broken_outbox = copy.deepcopy(prepared_outbox)
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

    from src.dalton.data_jobs.notification_payloads import (
        prepare_run_outbox_image_metadata,
    )

    valid_img = _make_synthetic_png(b"VALID-IMG")
    (run_dir / "images" / "ok.png").write_bytes(valid_img)
    ok_outbox = prepare_run_outbox_image_metadata(
        {
            "feishu_cards": [
                {
                    "card": {"schema": "2.0", "body": {"elements": [{"tag": "__img__", "path": "p0"}]}},
                    "images": [{"placeholder": "p0", "path": "images/ok.png"}],
                }
            ]
        },
        run_artifacts_dir=run_dir,
        job_key="softbank-nav-monitor",
    )
    ok_plan = build_data_task_delivery_plan(
        run_id=run_id,
        job_key="softbank-nav-monitor",
        raw_outbox=ok_outbox,
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
    """Administrator group report succeeds without `dalton_channel_identities`, omitting `owner_platform_user_id`, while personal DM without binding fails `not_linked`."""
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
    """Narrow Vault startup endpoint enforces service auth, rejects caller overrides, sets no-store, and never logs secret values."""
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
    """LaunchAgent plist template uses Vault credential source and owner-only env file, storing no plaintext credentials."""
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
    """Writerless FIFO artifact leaf is rejected immediately via nonblocking open + fstat without stalling."""
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


# ---------------------------------------------------------------------------
# Descriptor-Based Snapshot/Outbox Creation & Metadata Precedence
# ---------------------------------------------------------------------------


def test_snapshot_and_outbox_writer_reject_temp_and_destination_symlinks_without_touching_external_files(
    tmp_path: Path,
) -> None:
    """§1 & §2 (1, 2): Dangling or existing temp-file symlinks and destination symlinks are refused safely."""
    from src.dalton.data_jobs.notification_payloads import _atomic_write_snapshot_file
    from src.dalton.data_jobs.runner_wrapper import atomic_write_json, atomic_write_text
    from src.dalton.data_jobs.safe_fs import SafeFsError, safe_atomic_write_bytes

    path_map = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )
    run_id = str(uuid4())
    run_dir = path_map.backend_run_artifacts_dir(run_id)
    images_dir = run_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    outside_dir = tmp_path / "outside_sensitive"
    outside_dir.mkdir(parents=True, exist_ok=True)
    dangling_outside = outside_dir / "must_never_be_created.bin"
    existing_outside = outside_dir / "must_never_be_modified.bin"
    existing_outside.write_bytes(b"ORIGINAL-EXTERNAL-CONTENT")

    pid = os.getpid()

    # 1a. Legacy predictable temp symlink names AND forced exact random-token collision on dangling symlink
    os.symlink(dangling_outside, images_dir / f".chart.png.tmp.{pid}")
    os.symlink(dangling_outside, run_dir / f".tmp.outbox.json.{pid}")
    os.symlink(dangling_outside, images_dir / f".tmp.chart.png.{pid}.forceddangling")

    with pytest.raises(SafeFsError) as exc_dangling_collision:
        safe_atomic_write_bytes(
            path_map.backend_data_root,
            (".dalton-runs", run_id, "images", "chart.png"),
            b"snapshot-bytes",
            _token_supplier=lambda: "forceddangling",
        )
    assert exc_dangling_collision.value.code == "temp_create_collision"
    assert not dangling_outside.exists()
    assert not (images_dir / "chart.png").exists()

    # Normal random token bypasses legacy `.chart.png.tmp.<pid>` and `.tmp.outbox.json.<pid>` symlinks safely
    written_snap = _atomic_write_snapshot_file(
        run_dir,
        "images/chart.png",
        b"clean-snapshot",
        anchor_dir=path_map.backend_data_root,
    )
    assert written_snap.is_file() and not written_snap.is_symlink()
    assert written_snap.read_bytes() == b"clean-snapshot"
    assert not dangling_outside.exists()

    atomic_write_json(
        run_dir / "outbox.json",
        {"ok": True, "label": "中文测试"},
        anchor_dir=path_map.backend_data_root,
    )
    assert (run_dir / "outbox.json").is_file() and not (run_dir / "outbox.json").is_symlink()
    assert not dangling_outside.exists()

    # 1b. Forced temp collision against symlink pointing to an EXISTING external file
    os.symlink(existing_outside, images_dir / f".tmp.other.png.{pid}.forcedexisting")
    with pytest.raises(SafeFsError) as exc_existing_collision:
        safe_atomic_write_bytes(
            path_map.backend_data_root,
            (".dalton-runs", run_id, "images", "other.png"),
            b"overwrite-attempt",
            _token_supplier=lambda: "forcedexisting",
        )
    assert exc_existing_collision.value.code == "temp_create_collision"
    assert existing_outside.read_bytes() == b"ORIGINAL-EXTERNAL-CONTENT"
    assert not (images_dir / "other.png").exists()

    # 2a. Destination is a dangling symlink -> refused, external target never created, output stays untouched symlink
    dest_dangling_snap = images_dir / "dest_dangling.png"
    os.symlink(dangling_outside, dest_dangling_snap)
    with pytest.raises(DataTaskNotificationPrepError) as exc_dest_dangling:
        _atomic_write_snapshot_file(
            run_dir,
            "images/dest_dangling.png",
            b"blocked",
            anchor_dir=path_map.backend_data_root,
        )
    assert exc_dest_dangling.value.code == "symlink_rejected"
    assert not dangling_outside.exists()
    assert dest_dangling_snap.is_symlink()

    # 2b. Destination is a symlink to an existing external file -> refused on both snapshot & outbox writers
    dest_existing_snap = images_dir / "dest_existing.png"
    os.symlink(existing_outside, dest_existing_snap)
    with pytest.raises(DataTaskNotificationPrepError) as exc_dest_existing:
        _atomic_write_snapshot_file(
            run_dir,
            "images/dest_existing.png",
            b"blocked",
            anchor_dir=path_map.backend_data_root,
        )
    assert exc_dest_existing.value.code == "symlink_rejected"
    assert existing_outside.read_bytes() == b"ORIGINAL-EXTERNAL-CONTENT"

    dest_outbox_symlink = run_dir / "symlink_outbox.json"
    os.symlink(existing_outside, dest_outbox_symlink)
    with pytest.raises(SafeFsError) as exc_outbox_dest:
        atomic_write_json(
            dest_outbox_symlink,
            {"blocked": True},
            anchor_dir=path_map.backend_data_root,
        )
    assert exc_outbox_dest.value.code == "symlink_rejected"
    with pytest.raises(SafeFsError):
        atomic_write_text(
            dest_outbox_symlink,
            "blocked",
            anchor_dir=path_map.backend_data_root,
        )
    assert existing_outside.read_bytes() == b"ORIGINAL-EXTERNAL-CONTENT"


def test_snapshot_writer_rejects_run_root_symlinks_and_deterministic_directory_replacement(
    tmp_path: Path,
) -> None:
    """§1 & §2 (3): Symlink components and mid-operation directory swaps cannot redirect writes outside."""
    from src.dalton.data_jobs.notification_payloads import _atomic_write_snapshot_file
    from src.dalton.data_jobs.safe_fs import SafeFsError, safe_atomic_write_bytes

    outside_dir = tmp_path / "outside_redirect"
    outside_dir.mkdir(parents=True, exist_ok=True)

    # 1. Symlink at `.dalton-runs`, `<run_id>`, or `images` is rejected; configured storage-root symlink is allowed
    real_storage = tmp_path / "real_var_storage"
    real_storage.mkdir(parents=True, exist_ok=True)
    configured_root_symlink = tmp_path / "mac_like_var_link"
    os.symlink(real_storage, configured_root_symlink)

    ok_run_id = str(uuid4())
    ok_file = _atomic_write_snapshot_file(
        configured_root_symlink / ".dalton-runs" / ok_run_id,
        "images/0_cn_latest.png",
        _make_synthetic_png(b"MAC-ROOT"),
        anchor_dir=configured_root_symlink,
    )
    assert ok_file.is_file() and not ok_file.is_symlink()

    symlink_run_id = str(uuid4())
    os.symlink(outside_dir, real_storage / ".dalton-runs" / symlink_run_id)
    with pytest.raises(DataTaskNotificationPrepError) as exc_run_sym:
        _atomic_write_snapshot_file(
            configured_root_symlink / ".dalton-runs" / symlink_run_id,
            "images/0_cn_latest.png",
            b"must-not-write",
            anchor_dir=configured_root_symlink,
        )
    assert exc_run_sym.value.code == "symlink_rejected"
    assert list(outside_dir.iterdir()) == []

    # 2. Deterministic directory swap AFTER directory verification, BEFORE temp creation
    swap1_run = real_storage / ".dalton-runs" / str(uuid4())
    (swap1_run / "images").mkdir(parents=True, exist_ok=True)
    swap1_called = False

    def _swap_after_verify() -> None:
        nonlocal swap1_called
        swap1_called = True
        moved = swap1_run / "images_moved_1"
        os.rename(swap1_run / "images", moved)
        os.symlink(outside_dir, swap1_run / "images")

    with pytest.raises(SafeFsError) as exc_swap1:
        safe_atomic_write_bytes(
            configured_root_symlink,
            (".dalton-runs", swap1_run.name, "images", "chart.png"),
            b"swap-before-open",
            anchor_dir=configured_root_symlink,
            _on_dir_verified=_swap_after_verify,
        )
    assert swap1_called is True
    assert exc_swap1.value.code in ("directory_changed", "symlink_rejected")
    assert list(outside_dir.iterdir()) == []
    assert list((swap1_run / "images_moved_1").iterdir()) == []

    # 3. Deterministic directory swap AFTER temp write/fsync, BEFORE atomic replace
    swap2_run = real_storage / ".dalton-runs" / str(uuid4())
    (swap2_run / "images").mkdir(parents=True, exist_ok=True)
    swap2_called = False

    def _swap_before_publish() -> None:
        nonlocal swap2_called
        swap2_called = True
        moved = swap2_run / "images_moved_2"
        os.rename(swap2_run / "images", moved)
        os.symlink(outside_dir, swap2_run / "images")

    with pytest.raises(SafeFsError) as exc_swap2:
        safe_atomic_write_bytes(
            configured_root_symlink,
            (".dalton-runs", swap2_run.name, "images", "chart.png"),
            b"swap-before-replace",
            anchor_dir=configured_root_symlink,
            _on_before_publish=_swap_before_publish,
        )
    assert swap2_called is True
    assert exc_swap2.value.code in ("directory_changed", "symlink_rejected")
    assert list(outside_dir.iterdir()) == []
    # Temp file created inside the verified descriptor was cleaned up via `dir_fd`
    assert list((swap2_run / "images_moved_2").iterdir()) == []


def test_concurrent_snapshot_writes_and_failure_cleanup_close_descriptors_and_preserve_existing_files(
    tmp_path: Path,
) -> None:
    """§2 (4, 5, 6): Concurrent writers isolate temp files; failures close fds and preserve existing files."""
    from concurrent.futures import ThreadPoolExecutor
    import errno
    import threading

    from src.dalton.data_jobs.safe_fs import safe_atomic_write_bytes

    data_root = tmp_path / "data_root"
    run_id = str(uuid4())
    rel_parts = (".dalton-runs", run_id, "images", "concurrent.png")
    images_dir = data_root / ".dalton-runs" / run_id / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    # 1. Concurrent invocations targeting the same basename use distinct `.tmp.*` files
    payload_a = _make_synthetic_png(b"CONCURRENT-A-" * 64)
    payload_b = _make_synthetic_png(b"CONCURRENT-B-" * 64)
    barrier = threading.Barrier(2, timeout=3.0)
    observed_temp_snapshots: list[list[str]] = []
    lock = threading.Lock()

    def _sync_before_publish() -> None:
        with lock:
            observed_temp_snapshots.append(
                sorted(p.name for p in images_dir.iterdir() if p.name.startswith(".tmp."))
            )
        barrier.wait()

    with ThreadPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(
            safe_atomic_write_bytes,
            data_root,
            rel_parts,
            payload_a,
            _on_before_publish=_sync_before_publish,
        )
        fut_b = pool.submit(
            safe_atomic_write_bytes,
            data_root,
            rel_parts,
            payload_b,
            _on_before_publish=_sync_before_publish,
        )
        fut_a.result(timeout=5.0)
        fut_b.result(timeout=5.0)

    assert any(len(names) == 2 for names in observed_temp_snapshots)
    final_file = images_dir / "concurrent.png"
    assert final_file.is_file() and not final_file.is_symlink()
    assert final_file.read_bytes() in (payload_a, payload_b)
    assert [p.name for p in images_dir.iterdir() if p.name.startswith(".tmp.")] == []

    # 2. Partial-write chunk loop succeeds cleanly when `os.write` writes short chunks
    chunked_payload = b"0123456789abcdef" * 128

    def _short_writer(fd: int, view: memoryview) -> int:
        return os.write(fd, view[:17])

    safe_atomic_write_bytes(
        data_root,
        (".dalton-runs", run_id, "images", "chunked.png"),
        chunked_payload,
        _write_chunk_fn=_short_writer,
    )
    assert (images_dir / "chunked.png").read_bytes() == chunked_payload

    # 3. Failure during writing closes temp fd, removes only its own temp file, and preserves prior output + unrelated temp
    unrelated_entry = images_dir / ".tmp.chunked.png.other_worker"
    unrelated_entry.write_bytes(b"KEEP-OTHER-WORKER-TEMP")
    captured_write_fds: list[int] = []

    def _failing_writer(fd: int, view: memoryview) -> int:
        if not captured_write_fds:
            captured_write_fds.append(fd)
            return os.write(fd, view[:8])
        raise OSError(errno.ENOSPC, "synthetic disk full")

    with pytest.raises(OSError):
        safe_atomic_write_bytes(
            data_root,
            (".dalton-runs", run_id, "images", "chunked.png"),
            b"partial-should-never-publish" * 10,
            _write_chunk_fn=_failing_writer,
        )

    assert len(captured_write_fds) == 1
    with pytest.raises(OSError) as exc_closed_fd:
        os.fstat(captured_write_fds[0])
    assert exc_closed_fd.value.errno == errno.EBADF
    assert (images_dir / "chunked.png").read_bytes() == chunked_payload
    assert unrelated_entry.read_bytes() == b"KEEP-OTHER-WORKER-TEMP"
    assert [
        p.name
        for p in images_dir.iterdir()
        if p.name.startswith(".tmp.") and p.name != unrelated_entry.name
    ] == []

    # 4. Failure during publication leaves existing valid output and unrelated entry intact
    with pytest.raises(RuntimeError, match="abort-before-replace"):
        safe_atomic_write_bytes(
            data_root,
            (".dalton-runs", run_id, "images", "chunked.png"),
            b"aborted-publish",
            _on_before_publish=lambda: (_ for _ in ()).throw(RuntimeError("abort-before-replace")),
        )
    assert (images_dir / "chunked.png").read_bytes() == chunked_payload
    assert unrelated_entry.read_bytes() == b"KEEP-OTHER-WORKER-TEMP"
    assert [
        p.name
        for p in images_dir.iterdir()
        if p.name.startswith(".tmp.") and p.name != unrelated_entry.name
    ] == []


@pytest.mark.asyncio
async def test_t27_scheduler_execution_publishes_safe_outbox_and_preserves_four_market_digests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§1 & §2 (6): T27 preparation + scheduler `outbox.json` publication preserve CN/KR/JP/TW order and digests."""
    from datetime import datetime, timezone

    from src.dalton.data_jobs import scheduler as sched_mod
    from src.dalton.data_jobs.container_runner import (
        ContainerInspectionOutcome,
        DataTaskDockerClient,
    )

    path_map = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )
    charts_dir = path_map.backend_data_root / "temp" / "asia-leverage-crossview" / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    expected_market_bytes: list[tuple[str, bytes, str]] = []
    for idx, code in enumerate(("cn", "kr", "jp", "tw")):
        raw = _make_synthetic_png(f"MKT-{code.upper()}".encode("ascii"))
        (charts_dir / f"{code}_latest.png").write_bytes(raw)
        expected_market_bytes.append(
            (f"images/t27_{idx}_{code}_latest.png", raw, hashlib.sha256(raw).hexdigest())
        )

    run_id = str(uuid4())
    run_dir = path_map.backend_run_artifacts_dir(run_id)
    run_dir.mkdir(parents=True, exist_ok=True)
    outside_target = tmp_path / "outside_outbox_target.json"
    # Plant legacy `.tmp.outbox.json.<pid>` symlink to verify scheduler T27 write never follows it
    os.symlink(outside_target, run_dir / f".tmp.outbox.json.{os.getpid()}")

    markdown = _make_synthetic_t27_markdown()
    fake_outcome = ContainerInspectionOutcome(
        container_id="cid-synth-t27",
        exit_code=0,
        oom_killed=False,
        running=False,
        log_tail=markdown,
    )

    finalized_calls: list[dict[str, Any]] = []

    async def _fake_finalize(conn: Any, **kwargs: Any) -> tuple[bool, int | None]:
        finalized_calls.append(kwargs)
        return True, 0

    monkeypatch.setattr(
        sched_mod,
        "start_claimed_container_run",
        create_autospec(sched_mod.start_claimed_container_run, return_value="cid-synth-t27"),
    )
    monkeypatch.setattr(
        sched_mod,
        "monitor_container_until_exit",
        create_autospec(sched_mod.monitor_container_until_exit, return_value=(fake_outcome, False)),
    )
    monkeypatch.setattr(
        sched_mod,
        "finalize_task_run",
        create_autospec(sched_mod.finalize_task_run, side_effect=_fake_finalize),
    )

    slot = datetime(2026, 11, 9, 10, 0, tzinfo=timezone.utc)
    claimed_run = sched_mod.ClaimedTaskRun(
        run_id=run_id,
        job_key=T27_JOB_KEY,
        generation=1,
        runner="container",
        command_argv=["python3", "t27.py"],
        workdir="/workspace",
        timeout_seconds=60,
        no_output_timeout_seconds=60,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=True,
        scheduled_slot_utc=slot,
        deadline_utc=slot,
    )

    await sched_mod.execute_claimed_container_run(
        object(),
        lock_conn=object(),
        docker_client=create_autospec(DataTaskDockerClient, instance=True),
        claimed=claimed_run,
        path_map=path_map,
    )

    assert not outside_target.exists()
    outbox_file = run_dir / "outbox.json"
    assert outbox_file.is_file() and not outbox_file.is_symlink()
    persisted_outbox = finalized_calls[0]["outbox_payload"]
    assert json.loads(outbox_file.read_text(encoding="utf-8")) == persisted_outbox

    plan = build_data_task_delivery_plan(
        run_id=run_id,
        job_key=T27_JOB_KEY,
        raw_outbox=persisted_outbox,
        run_artifacts_dir=run_dir,
    )
    assert len(plan.items) == 1
    card_images = plan.items[0].images
    assert [plan.artifacts_by_id[img.artifact_id].rel_path for img in card_images] == [
        m[0] for m in expected_market_bytes
    ]
    assert [img.size_bytes for img in card_images] == [len(m[1]) for m in expected_market_bytes]
    assert [img.sha256 for img in card_images] == [m[2] for m in expected_market_bytes]


@pytest.mark.asyncio
async def test_bounded_run_metadata_and_database_precedence_across_scheduler_and_api_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§3: Valid DB outbox takes strict precedence with zero disk metadata reads; fallback is bounded & safe."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone
    import shutil
    import subprocess
    import tempfile

    from src.dalton.data_jobs import scheduler as sched_mod
    from src.dalton.data_jobs.container_runner import (
        ContainerInspectionOutcome,
        DataTaskDockerClient,
        read_run_artifacts,
    )
    from src.dalton.data_jobs.safe_fs import MAX_RUN_METADATA_BYTES

    path_map = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )
    outside_secret = tmp_path / "outside_secret_outbox.json"
    outside_secret.write_text('{"feishu_cards": [{"card": {"schema": "2.0"}}]}', encoding="utf-8")

    # -------------------------------------------------------------------------
    # 1. DB outbox precedence despite hostile symlink / oversized disk `outbox.json` & `frozen_manifest.json`
    # -------------------------------------------------------------------------
    db_run_id = str(uuid4())
    db_run_dir = path_map.backend_run_artifacts_dir(db_run_id)
    (db_run_dir / "images").mkdir(parents=True, exist_ok=True)
    chart_bytes = _make_synthetic_png(b"DB-PRECEDENCE")
    (db_run_dir / "images" / "c0.png").write_bytes(chart_bytes)
    # Plant hostile symlink at `outbox.json` AND oversized file at `frozen_manifest.json`
    os.symlink(outside_secret, db_run_dir / "outbox.json")
    (db_run_dir / "frozen_manifest.json").write_bytes(b"{" + b"x" * (MAX_RUN_METADATA_BYTES + 128))

    valid_db_outbox = {
        "feishu_cards": [
            {
                "card": {
                    "schema": "2.0",
                    "body": {"elements": [{"tag": "__img__", "path": "p0"}]},
                },
                "images": [{"placeholder": "p0", "path": "images/c0.png"}],
            }
        ]
    }

    loaded_from_db = dtn_db._load_persisted_run_raw_outbox(
        db_run_id,
        "softbank-nav-monitor",
        db_outbox_payload=valid_db_outbox,
        log_tail=None,
        path_map=path_map,
    )
    assert loaded_from_db == valid_db_outbox

    _, _, resolved_sched_payload = sched_mod._resolve_run_notify_payload(
        db_run_id,
        "softbank-nav-monitor",
        db_outbox_payload=valid_db_outbox,
        log_tail=None,
        path_map=path_map,
    )
    assert resolved_sched_payload["expected_item_count"] == 1
    assert "_metadata_error_code" not in resolved_sched_payload

    # -------------------------------------------------------------------------
    # 2. Absent DB outbox fallback: valid disk `outbox.json` works WITHOUT opening `frozen_manifest.json`;
    #    missing `outbox.json` preserves normal `None` / stdout fallback; hostile files fail with classified codes
    # -------------------------------------------------------------------------
    fallback_run_id = str(uuid4())
    fb_dir = path_map.backend_run_artifacts_dir(fallback_run_id)
    fb_dir.mkdir(parents=True, exist_ok=True)
    (fb_dir / "outbox.json").write_text(
        json.dumps({"items": [{"text": "Fallback item 0"}]}),
        encoding="utf-8",
    )
    # Poison `frozen_manifest.json` as a symlink to outside file -> notification path must NEVER open it!
    os.symlink(outside_secret, fb_dir / "frozen_manifest.json")

    assert dtn_db._load_persisted_run_raw_outbox(
        fallback_run_id,
        T26_JOB_KEY,
        db_outbox_payload=None,
        log_tail=None,
        path_map=path_map,
    ) == {"items": [{"text": "Fallback item 0"}]}

    _, _, fb_sched = sched_mod._resolve_run_notify_payload(
        fallback_run_id,
        T26_JOB_KEY,
        db_outbox_payload=None,
        log_tail=None,
        path_map=path_map,
    )
    assert fb_sched["expected_item_count"] == 1
    assert "_metadata_error_code" not in fb_sched

    # Execution completion DOES read `frozen_manifest.json` with strict=True and rejects the symlink!
    with pytest.raises(Exception) as exc_manifest_sym:
        read_run_artifacts(fallback_run_id, path_map=path_map, strict=True)
    assert getattr(exc_manifest_sym.value, "code", None) == "symlink_rejected"

    # Missing files return normal `(None, None)` and preserve stdout fallback
    missing_run_id = str(uuid4())
    assert read_run_artifacts(missing_run_id, path_map=path_map, strict=True) == (None, None)
    _, _, t32_stdout_payload = sched_mod._resolve_run_notify_payload(
        missing_run_id,
        "quantum-ew-monitor",
        db_outbox_payload=None,
        log_tail="ALERT_FOUND: synthetic quantum signal",
        path_map=path_map,
    )
    assert t32_stdout_payload["expected_item_count"] == 1
    assert "_outbox_missing" not in t32_stdout_payload

    # Symlink `outbox.json` leaf (reproducing `.agy/outbox_symlink_diagnostic.json`) & oversized metadata (> 1 MiB)
    sym_run_id = str(uuid4())
    sym_dir = path_map.backend_run_artifacts_dir(sym_run_id)
    sym_dir.mkdir(parents=True, exist_ok=True)
    os.symlink(outside_secret, sym_dir / "outbox.json")

    with pytest.raises(DataTaskArtifactError) as exc_api_sym:
        dtn_db._load_persisted_run_raw_outbox(
            sym_run_id,
            "softbank-nav-monitor",
            db_outbox_payload=None,
            log_tail=None,
            path_map=path_map,
        )
    assert exc_api_sym.value.code == "symlink_rejected"
    _, _, sym_sched = sched_mod._resolve_run_notify_payload(
        sym_run_id,
        "softbank-nav-monitor",
        db_outbox_payload=None,
        log_tail=None,
        path_map=path_map,
    )
    assert sym_sched.get("_metadata_error_code") == "symlink_rejected"

    oversized_run_id = str(uuid4())
    ov_dir = path_map.backend_run_artifacts_dir(oversized_run_id)
    ov_dir.mkdir(parents=True, exist_ok=True)
    (ov_dir / "outbox.json").write_bytes(
        b'{"pad":"' + (b"a" * MAX_RUN_METADATA_BYTES) + b'"}'
    )
    with pytest.raises(DataTaskArtifactError) as exc_ov:
        dtn_db._load_persisted_run_raw_outbox(
            oversized_run_id,
            "softbank-nav-monitor",
            db_outbox_payload=None,
            log_tail=None,
            path_map=path_map,
        )
    assert exc_ov.value.code == "metadata_too_large"
    _, _, ov_sched = sched_mod._resolve_run_notify_payload(
        oversized_run_id,
        "softbank-nav-monitor",
        db_outbox_payload=None,
        log_tail=None,
        path_map=path_map,
    )
    assert ov_sched.get("_metadata_error_code") == "metadata_too_large"

    # Verify `execute_claimed_container_run` fails the run cleanly on hostile disk metadata
    recorded_finalizations: list[dict[str, Any]] = []

    async def _capture_finalize(conn: Any, **kwargs: Any) -> tuple[bool, int | None]:
        recorded_finalizations.append(kwargs)
        return True, 0

    monkeypatch.setattr(
        sched_mod,
        "start_claimed_container_run",
        create_autospec(sched_mod.start_claimed_container_run, return_value="cid-synth-hostile"),
    )
    monkeypatch.setattr(
        sched_mod,
        "monitor_container_until_exit",
        create_autospec(
            sched_mod.monitor_container_until_exit,
            return_value=(
                ContainerInspectionOutcome(
                    container_id="cid-synth-hostile",
                    exit_code=0,
                    oom_killed=False,
                    running=False,
                    log_tail="",
                ),
                False,
            ),
        ),
    )
    monkeypatch.setattr(
        sched_mod,
        "finalize_task_run",
        create_autospec(sched_mod.finalize_task_run, side_effect=_capture_finalize),
    )
    slot = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
    await sched_mod.execute_claimed_container_run(
        object(),
        lock_conn=object(),
        docker_client=create_autospec(DataTaskDockerClient, instance=True),
        claimed=sched_mod.ClaimedTaskRun(
            run_id=sym_run_id,
            job_key="softbank-nav-monitor",
            generation=1,
            runner="container",
            command_argv=["python3", "sb.py"],
            workdir="/workspace",
            timeout_seconds=60,
            no_output_timeout_seconds=60,
            required_secrets=[],
            cos_mode="skip",
            notify_enabled=True,
            scheduled_slot_utc=slot,
            deadline_utc=slot,
        ),
        path_map=path_map,
    )
    eval_res = recorded_finalizations[0]["evaluation"]
    assert eval_res.status == "failed"
    assert eval_res.failure_reason == "invalid_run_metadata:symlink_rejected"

    # Verify FastAPI `/plan` endpoint converts hostile metadata when DB outbox is NULL into clean `authorized: False` (not 500!)
    @asynccontextmanager
    async def _fake_conn(_c: Any = None):
        class _Cur:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return None

            async def execute(self, sql: str, _params: Any = None) -> None:
                self._sql = sql

            async def fetchone(self) -> dict[str, Any] | None:
                if "FROM dalton_data_task_runs" in self._sql:
                    return {
                        "run_id": sym_run_id,
                        "job_key": "softbank-nav-monitor",
                        "status": "succeeded",
                        "notify_status": "pending_dispatch",
                        "outbox_payload": None,
                        "log_tail": None,
                        "def_job_key": "softbank-nav-monitor",
                        "task_enabled": True,
                        "task_notify_enabled": True,
                    }
                if "FROM dalton_channel_identities" in self._sql:
                    return {"platform_user_id": "ou_synth_pm"}
                return None

        class _Conn:
            @asynccontextmanager
            async def transaction(self):
                yield

            def cursor(self, **_kw):
                return _Cur()

        yield _Conn()

    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", SERVICE_TOKEN)
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm_meta_owner")
    monkeypatch.setattr(
        dtn_db,
        "get_db_connection",
        create_autospec(dtn_db.get_db_connection, side_effect=_fake_conn),
    )
    monkeypatch.setattr(
        dtn_db,
        "build_path_map",
        create_autospec(dtn_db.build_path_map, return_value=path_map),
    )
    monkeypatch.setattr(
        dtn_db,
        "evaluate_user_notify_eligibility",
        create_autospec(
            dtn_db.evaluate_user_notify_eligibility,
            return_value=(True, None, ["feishu:@me"]),
        ),
    )
    app = FastAPI()
    app.include_router(dalton_router)
    with TestClient(app) as tc:
        r_plan = tc.post(
            "/api/v1/dalton/channels/internal/data-task-notifications/plan",
            headers={"X-Service-Token": SERVICE_TOKEN},
            json={"run_id": sym_run_id, "target": "feishu:@me"},
        )
        assert r_plan.status_code == 200
        assert r_plan.json()["authorized"] is False
        assert r_plan.json()["code"] == "symlink_rejected"

    # -------------------------------------------------------------------------
    # 3. Isolated bounded subprocess check for writerless FIFO `outbox.json` & `frozen_manifest.json`
    # -------------------------------------------------------------------------
    fifo_tmp = Path(tempfile.mkdtemp(prefix="dalton-meta-fifo-"))
    proc: subprocess.Popen[str] | None = None
    try:
        project_root = Path(__file__).resolve().parents[3]
        child_code = "\n".join(
            [
                "import json, os, uuid",
                "from pathlib import Path",
                "from src.dalton.data_jobs.container_runner import build_path_map, read_run_artifacts",
                "from src.dalton.data_jobs.notification_payloads import DataTaskArtifactError",
                "from src.dalton.data_jobs import scheduler as sched_mod",
                "from src.dalton.database import data_task_notifications as dtn_db",
                f"base = Path({str(fifo_tmp)!r})",
                "pm = build_path_map(host_var_dir=base / 'h', backend_var_dir=base / 'b')",
                # Run A: valid `outbox.json`, writerless FIFO at `frozen_manifest.json` -> notification must NOT open manifest; strict completion rejects FIFO promptly
                "rid_a = str(uuid.uuid4())",
                "dir_a = pm.backend_run_artifacts_dir(rid_a)",
                "dir_a.mkdir(parents=True, exist_ok=True)",
                "(dir_a / 'outbox.json').write_text(json.dumps({'items': [{'text': 'ok'}]}), encoding='utf-8')",
                "os.mkfifo(dir_a / 'frozen_manifest.json')",
                "a_api = dtn_db._load_persisted_run_raw_outbox(rid_a, 'us-topbottom-dm-v23', db_outbox_payload=None, log_tail=None, path_map=pm)",
                "_, _, a_sched = sched_mod._resolve_run_notify_payload(rid_a, 'us-topbottom-dm-v23', db_outbox_payload=None, log_tail=None, path_map=pm)",
                "manifest_fifo_code = None",
                "try:",
                "    read_run_artifacts(rid_a, path_map=pm, strict=True)",
                "except Exception as exc:",
                "    manifest_fifo_code = getattr(exc, 'code', None)",
                # Run B: writerless FIFO at `outbox.json` -> DB precedence ignores it; absent-DB rejects promptly without blocking
                "rid_b = str(uuid.uuid4())",
                "dir_b = pm.backend_run_artifacts_dir(rid_b)",
                "dir_b.mkdir(parents=True, exist_ok=True)",
                "os.mkfifo(dir_b / 'outbox.json')",
                "b_db_prec = dtn_db._load_persisted_run_raw_outbox(rid_b, 'us-topbottom-dm-v23', db_outbox_payload={'items': [{'text': 'from-db'}]}, log_tail=None, path_map=pm)",
                "outbox_fifo_api = None",
                "try:",
                "    dtn_db._load_persisted_run_raw_outbox(rid_b, 'us-topbottom-dm-v23', db_outbox_payload=None, log_tail=None, path_map=pm)",
                "except DataTaskArtifactError as exc:",
                "    outbox_fifo_api = exc.code",
                "_, _, outbox_fifo_sched = sched_mod._resolve_run_notify_payload(rid_b, 'us-topbottom-dm-v23', db_outbox_payload=None, log_tail=None, path_map=pm)",
                "print(json.dumps({",
                "    'a_api_ok': a_api == {'items': [{'text': 'ok'}]},",
                "    'a_sched_count': a_sched.get('expected_item_count'),",
                "    'manifest_fifo_code': manifest_fifo_code,",
                "    'b_db_prec_ok': b_db_prec == {'items': [{'text': 'from-db'}]},",
                "    'outbox_fifo_api': outbox_fifo_api,",
                "    'outbox_fifo_sched_err': outbox_fifo_sched.get('_metadata_error_code'),",
                "}), flush=True)",
            ]
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
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
                "Metadata FIFO open blocked in isolated child subprocess beyond 3.0s hard timeout"
            )
        assert proc.returncode == 0, f"Child subprocess failed: {stderr_text}"
        parsed_res = json.loads(stdout_text.strip().splitlines()[-1])
        assert parsed_res == {
            "a_api_ok": True,
            "a_sched_count": 1,
            "manifest_fifo_code": "non_regular_file",
            "b_db_prec_ok": True,
            "outbox_fifo_api": "non_regular_file",
            "outbox_fifo_sched_err": "non_regular_file",
        }
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5.0)
        shutil.rmtree(fifo_tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_reconcile_uncertain_data_task_deliveries_expected_count_and_db_read_failure_safety(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§4 (unit): Frozen payload item-count derivation is strict and DB read errors leave uncertain state intact."""
    from contextlib import asynccontextmanager

    assert dtn_db._derive_expected_item_count_from_persisted_payload(
        {"expected_item_count": 2, "text": "summary"}
    ) == 2
    assert dtn_db._derive_expected_item_count_from_persisted_payload(
        {
            "feishu_cards": [
                {"card": {"schema": "2.0"}},
                {"card": {"schema": "2.0"}},
            ]
        }
    ) == 2
    assert dtn_db._derive_expected_item_count_from_persisted_payload(
        {"items": [{"index": 0, "text": "a"}, {"index": 1, "text": "b"}]}
    ) == 2
    # Non-contiguous item indices or empty payload -> refused (`None`)
    assert dtn_db._derive_expected_item_count_from_persisted_payload(
        {"items": [{"index": 0, "text": "a"}, {"index": 2, "text": "b"}]}
    ) is None
    assert dtn_db._derive_expected_item_count_from_persisted_payload({}) is None

    # Database read error inside `reconcile_uncertain_data_task_deliveries` leaves state intact and returns 0
    executed_statements: list[str] = []

    @asynccontextmanager
    async def _broken_read_conn(_c: Any = None):
        class _Cur:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_a):
                return None

            async def execute(self, sql: str, _params: Any = None) -> None:
                executed_statements.append(sql)
                raise RuntimeError("simulated transient PostgreSQL read error")

        class _Conn:
            def cursor(self, **_kw):
                return _Cur()

        yield _Conn()

    monkeypatch.setattr(
        dtn_db,
        "get_db_connection",
        create_autospec(dtn_db.get_db_connection, side_effect=_broken_read_conn),
    )

    reconciled_count = await dtn_db.reconcile_uncertain_data_task_deliveries(limit=5)
    assert reconciled_count == 0
    assert len(executed_statements) == 1
    assert "UPDATE " not in executed_statements[0]


def test_safe_atomic_write_bytes_rejects_temp_replacement_symlink_and_regular_file_preserving_destination(
    tmp_path: Path,
) -> None:
    """Temp-file identity verification rejects symlink or regular-file replacement before publish without unlinking replacement."""
    from src.dalton.data_jobs.safe_fs import SafeFsError, safe_atomic_write_bytes

    anchor = tmp_path / "anchor"
    anchor.mkdir(parents=True, exist_ok=True)
    rel_parts = ("runs", "r-temp-verify", "outbox.json")
    dest = anchor.joinpath(*rel_parts)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(b"valid previous")

    outside_file = tmp_path / "outside_target.txt"
    outside_file.write_bytes(b"outside untouched")

    # 1. Replace temp file with an outside symlink before publish
    replaced_tmp_symlink: list[Path] = []

    def _replace_with_symlink(tmp_name: str) -> None:
        tmp_entry = dest.parent / tmp_name
        tmp_entry.unlink()
        os.symlink(outside_file, tmp_entry)
        replaced_tmp_symlink.append(tmp_entry)

    with pytest.raises(SafeFsError) as exc_sym:
        safe_atomic_write_bytes(
            anchor,
            rel_parts,
            b"new payload 1",
            anchor_dir=anchor,
            _on_before_publish=_replace_with_symlink,
        )
    assert exc_sym.value.code == "symlink_rejected"
    assert len(replaced_tmp_symlink) == 1
    assert dest.is_symlink() is False
    assert dest.read_bytes() == b"valid previous"
    assert outside_file.read_bytes() == b"outside untouched"
    # Cleanup must NOT unlink the replacement symlink whose inode differs from tmp_fd
    assert replaced_tmp_symlink[0].is_symlink() is True
    replaced_tmp_symlink[0].unlink()

    # 2. Replace temp file with a different regular file (different inode) before publish
    replaced_tmp_regular: list[Path] = []

    def _replace_with_other_regular_file(tmp_name: str) -> None:
        tmp_entry = dest.parent / tmp_name
        tmp_entry.unlink()
        tmp_entry.write_bytes(b"attacker swapped regular file")
        replaced_tmp_regular.append(tmp_entry)

    with pytest.raises(SafeFsError) as exc_reg:
        safe_atomic_write_bytes(
            anchor,
            rel_parts,
            b"new payload 2",
            anchor_dir=anchor,
            _on_before_publish=_replace_with_other_regular_file,
        )
    assert exc_reg.value.code == "symlink_rejected"
    assert len(replaced_tmp_regular) == 1
    assert dest.is_symlink() is False
    assert dest.read_bytes() == b"valid previous"
    # Cleanup must NOT unlink another writer's regular file whose inode differs from tmp_fd
    assert replaced_tmp_regular[0].is_file() is True
    assert replaced_tmp_regular[0].read_bytes() == b"attacker swapped regular file"
    replaced_tmp_regular[0].unlink()

    # 3. Normal write still succeeds and atomically updates destination
    safe_atomic_write_bytes(anchor, rel_parts, b"valid updated", anchor_dir=anchor)
    assert dest.is_symlink() is False
    assert dest.read_bytes() == b"valid updated"


@pytest.mark.asyncio
async def test_persisted_image_metadata_preparation_t52_t54_and_delivery_verification_rejects_modified_or_unprepared_images(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Image size_bytes + sha256 are persisted at run finalization and strictly verified on plan and artifact download."""
    from contextlib import asynccontextmanager
    from datetime import datetime, timezone

    from src.dalton.data_jobs import scheduler as sched
    from src.dalton.data_jobs.container_runner import ContainerInspectionOutcome
    from src.dalton.data_jobs.evaluators import TaskEvaluationResult
    from src.dalton.data_jobs.notification_payloads import (
        DataTaskNotificationError,
        DataTaskNotificationPrepError,
        build_data_task_delivery_plan,
        prepare_run_outbox_image_metadata,
        read_validated_run_artifact,
    )

    path_map = build_path_map(
        host_var_dir=tmp_path / "host_var",
        backend_var_dir=tmp_path / "backend_var",
    )
    png_v1 = _make_valid_png_bytes(width=18, height=18, rgb=(20, 90, 180))
    png_same_len_diff_sha = _make_valid_png_bytes(width=18, height=18, rgb=(200, 40, 60))
    png_v2_diff_len = _make_valid_png_bytes(width=32, height=24, rgb=(40, 160, 80))
    assert len(png_v1) == len(png_same_len_diff_sha)
    assert len(png_v1) != len(png_v2_diff_len)
    assert hashlib.sha256(png_same_len_diff_sha).hexdigest() != hashlib.sha256(png_v1).hexdigest()

    # --- 1. Representative T52 and T54 finalization via `finalize_task_run` persists `size_bytes` and `sha256` ---
    executed_updates: list[dict[str, Any]] = []

    class _FakeCur:
        rowcount = 1

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

        async def execute(self, sql: str, params: Any = None) -> None:
            _ = sql
            if isinstance(params, dict) and "terminal_status" in params:
                executed_updates.append(dict(params))

        async def fetchone(self):
            return {"consecutive_failures": 0}

    class _FakeConn:
        @asynccontextmanager
        async def transaction(self):
            yield

        def cursor(self, **_kw):
            return _FakeCur()

    monkeypatch.setattr(
        sched,
        "check_scheduler_lock",
        create_autospec(sched.check_scheduler_lock, return_value=None),
    )
    monkeypatch.setattr(
        sched,
        "freeze_task_run_artifacts",
        create_autospec(sched.freeze_task_run_artifacts, return_value=None),
    )
    monkeypatch.setattr(
        sched,
        "cleanup_run_secrets_dir",
        create_autospec(sched.cleanup_run_secrets_dir, return_value=None),
    )

    slot_utc = datetime(2026, 4, 15, 12, 0, tzinfo=timezone.utc)
    for idx, (job_key, title) in enumerate(
        [
            ("softbank-nav-monitor", "T52 SoftBank NAV 监控"),
            ("tsmc-adr-premium", "T54 TSMC ADR 溢价监控"),
        ]
    ):
        run_id = f"00000000-0000-4000-8000-00000000050{idx}"
        run_dir = path_map.backend_run_artifacts_dir(run_id)
        (run_dir / "images").mkdir(parents=True, exist_ok=True)
        img_file = run_dir / "images" / "card_0_img_0.png"
        img_file.write_bytes(png_v1)

        card_schema = {
            "schema": "2.0",
            "header": {"title": {"tag": "plain_text", "content": title}},
            "body": {
                "elements": [
                    {"tag": "markdown", "content": f"Section for {job_key}"},
                    {"tag": "__img__", "path": "__FEISHU_IMG_0_0__"},
                ]
            },
        }
        raw_outbox = {
            "text": f"{title} summary",
            "feishu_cards": [
                {
                    "card": copy.deepcopy(card_schema),
                    "images": [
                        {
                            "placeholder": "__FEISHU_IMG_0_0__",
                            "path": "images/card_0_img_0.png",
                        }
                    ],
                }
            ],
        }
        (run_dir / "outbox.json").write_text(
            json.dumps(raw_outbox, ensure_ascii=False),
            encoding="utf-8",
        )

        claimed = sched.ClaimedTaskRun(
            run_id=run_id,
            job_key=job_key,
            generation=1,
            runner="container",
            command_argv=["python", "-m", "task"],
            workdir="/workspace",
            timeout_seconds=120,
            no_output_timeout_seconds=60,
            required_secrets=[],
            cos_mode="skip",
            notify_enabled=True,
            scheduled_slot_utc=slot_utc,
            deadline_utc=slot_utc,
            trigger_source="cron",
            is_trial=False,
        )
        committed, _ = await sched.finalize_task_run(
            _FakeConn(),
            lock_conn=object(),
            docker_client=None,
            claimed=claimed,
            container_id=None,
            outcome=ContainerInspectionOutcome(
                container_id="cid-prep",
                running=False,
                exit_code=0,
                oom_killed=False,
                log_tail="ok",
            ),
            evaluation=TaskEvaluationResult(ok=True, status="succeeded"),
            outbox_payload=raw_outbox,
            frozen_manifest={"job_key": job_key, "status": "ok"},
            path_map=path_map,
        )
        assert committed is True
        last_update = executed_updates[-1]
        assert last_update["terminal_status"] == "succeeded"
        assert last_update["notify_status"] == "pending_dispatch"
        persisted_outbox = json.loads(last_update["outbox_payload"])
        assert persisted_outbox["feishu_cards"][0]["card"] == card_schema
        img_meta = persisted_outbox["feishu_cards"][0]["images"][0]
        assert img_meta["size_bytes"] == len(png_v1)
        assert img_meta["sha256"] == hashlib.sha256(png_v1).hexdigest()

        # Plan and artifact download succeed with intact image bytes
        plan = build_data_task_delivery_plan(
            run_id=run_id,
            job_key=job_key,
            raw_outbox=persisted_outbox,
            run_artifacts_dir=run_dir,
        )
        assert len(plan.items) == 1
        expected_plan_card = copy.deepcopy(card_schema)
        expected_plan_card["body"]["elements"][1]["path"] = "artifact:card0-img0"
        assert plan.items[0].card == expected_plan_card
        assert len(plan.items[0].images) == 1
        art_desc = plan.items[0].images[0]
        assert art_desc.artifact_id == "card0-img0"
        assert art_desc.size_bytes == len(png_v1)
        assert art_desc.sha256 == hashlib.sha256(png_v1).hexdigest()

        art = read_validated_run_artifact(
            run_id=run_id,
            artifact_id=art_desc.artifact_id,
            plan=plan,
            run_artifacts_dir=run_dir,
        )
        assert art.content == png_v1
        assert art.size_bytes == len(png_v1)
        assert art.sha256 == hashlib.sha256(png_v1).hexdigest()

        # --- 2. Same-byte-length valid replacement PNG is rejected by both plan rebuild and artifact download ---
        img_file.write_bytes(png_same_len_diff_sha)
        with pytest.raises(DataTaskNotificationError) as exc_same_len_plan:
            build_data_task_delivery_plan(
                run_id=run_id,
                job_key=job_key,
                raw_outbox=persisted_outbox,
                run_artifacts_dir=run_dir,
            )
        assert exc_same_len_plan.value.code == "artifact_sha256_mismatch"

        with pytest.raises(DataTaskNotificationError) as exc_same_len_dl:
            read_validated_run_artifact(
                run_id=run_id,
                artifact_id=art_desc.artifact_id,
                plan=plan,
                run_artifacts_dir=run_dir,
            )
        assert exc_same_len_dl.value.code == "artifact_sha256_mismatch"

        # Different-byte-length valid replacement PNG is rejected with `artifact_size_mismatch`
        img_file.write_bytes(png_v2_diff_len)
        with pytest.raises(DataTaskNotificationError) as exc_diff_len_plan:
            build_data_task_delivery_plan(
                run_id=run_id,
                job_key=job_key,
                raw_outbox=persisted_outbox,
                run_artifacts_dir=run_dir,
            )
        assert exc_diff_len_plan.value.code == "artifact_size_mismatch"

        with pytest.raises(DataTaskNotificationError) as exc_diff_len_dl:
            read_validated_run_artifact(
                run_id=run_id,
                artifact_id=art_desc.artifact_id,
                plan=plan,
                run_artifacts_dir=run_dir,
            )
        assert exc_diff_len_dl.value.code == "artifact_size_mismatch"

        # Corrupt non-PNG bytes of identical length are rejected by MIME/header validation (`unsupported_media_type`)
        img_file.write_bytes(b"NOT-A-VALID-PNG!" + b"x" * (len(png_v1) - 16))
        with pytest.raises(DataTaskNotificationError) as exc_corrupt:
            build_data_task_delivery_plan(
                run_id=run_id,
                job_key=job_key,
                raw_outbox=persisted_outbox,
                run_artifacts_dir=run_dir,
            )
        assert exc_corrupt.value.code == "unsupported_media_type"

        # Restore original PNG for subsequent checks
        img_file.write_bytes(png_v1)

    # --- 3. Missing or malformed persisted metadata is rejected on plan/download without recomputing from disk ---
    t52_dir = path_map.backend_run_artifacts_dir("00000000-0000-4000-8000-000000000500")
    for bad_img_entry, expected_err_code in [
        ({"placeholder": "__FEISHU_IMG_0_0__", "path": "images/card_0_img_0.png"}, "artifact_metadata_missing"),
        (
            {
                "placeholder": "__FEISHU_IMG_0_0__",
                "path": "images/card_0_img_0.png",
                "size_bytes": 0,
                "sha256": hashlib.sha256(png_v1).hexdigest(),
            },
            "artifact_size_invalid",
        ),
        (
            {
                "placeholder": "__FEISHU_IMG_0_0__",
                "path": "images/card_0_img_0.png",
                "size_bytes": True,
                "sha256": hashlib.sha256(png_v1).hexdigest(),
            },
            "artifact_size_invalid",
        ),
        (
            {
                "placeholder": "__FEISHU_IMG_0_0__",
                "path": "images/card_0_img_0.png",
                "size_bytes": len(png_v1),
                "sha256": "not-a-valid-sha256",
            },
            "artifact_sha256_invalid",
        ),
    ]:
        unprepared_outbox = {
            "text": "unprepared",
            "feishu_cards": [
                {
                    "card": {
                        "schema": "2.0",
                        "body": {"elements": [{"tag": "__img__", "path": "__FEISHU_IMG_0_0__"}]},
                    },
                    "images": [bad_img_entry],
                }
            ],
        }
        with pytest.raises(DataTaskNotificationError) as exc_bad_meta:
            build_data_task_delivery_plan(
                run_id="00000000-0000-4000-8000-000000000500",
                job_key="softbank-nav-monitor",
                raw_outbox=unprepared_outbox,
                run_artifacts_dir=t52_dir,
            )
        assert exc_bad_meta.value.code == expected_err_code

    # --- 4. Already-supplied mismatched or malformed metadata is rejected during preparation and fails the run ---
    for supplied_bad_entry, expected_prep_code in [
        (
            {
                "placeholder": "__FEISHU_IMG_0_0__",
                "path": "images/card_0_img_0.png",
                "size_bytes": len(png_v1) + 5,
                "sha256": hashlib.sha256(png_v1).hexdigest(),
            },
            "artifact_size_mismatch",
        ),
        (
            {
                "placeholder": "__FEISHU_IMG_0_0__",
                "path": "images/card_0_img_0.png",
                "size_bytes": len(png_v1),
                "sha256": "0" * 64,
            },
            "artifact_sha256_mismatch",
        ),
        (
            {
                "placeholder": "__FEISHU_IMG_0_0__",
                "path": "images/card_0_img_0.png",
                "size_bytes": -10,
            },
            "artifact_size_invalid",
        ),
        (
            {
                "placeholder": "__FEISHU_IMG_0_0__",
                "path": "images/card_0_img_0.png",
                "sha256": "bad-digest",
            },
            "artifact_sha256_invalid",
        ),
    ]:
        bad_supplied_outbox = {
            "text": "supplied mismatch",
            "feishu_cards": [
                {
                    "card": {
                        "schema": "2.0",
                        "body": {"elements": [{"tag": "__img__", "path": "__FEISHU_IMG_0_0__"}]},
                    },
                    "images": [supplied_bad_entry],
                }
            ],
        }
        with pytest.raises(DataTaskNotificationPrepError) as exc_prep:
            prepare_run_outbox_image_metadata(
                bad_supplied_outbox,
                run_artifacts_dir=t52_dir,
                job_key="softbank-nav-monitor",
            )
        assert exc_prep.value.code == expected_prep_code

    # Verify `finalize_task_run` marks run `failed` with `outbox_payload=None` and `notify_status='skipped'` on prep error
    bad_run_id = "00000000-0000-4000-8000-000000000599"
    bad_run_dir = path_map.backend_run_artifacts_dir(bad_run_id)
    (bad_run_dir / "images").mkdir(parents=True, exist_ok=True)
    (bad_run_dir / "images" / "card_0_img_0.png").write_bytes(png_v1)
    mismatched_outbox = {
        "text": "mismatched digest",
        "feishu_cards": [
            {
                "card": {
                    "schema": "2.0",
                    "body": {"elements": [{"tag": "__img__", "path": "__FEISHU_IMG_0_0__"}]},
                },
                "images": [
                    {
                        "placeholder": "__FEISHU_IMG_0_0__",
                        "path": "images/card_0_img_0.png",
                        "size_bytes": len(png_v1),
                        "sha256": "f" * 64,
                    }
                ],
            }
        ],
    }
    claimed_bad = sched.ClaimedTaskRun(
        run_id=bad_run_id,
        job_key="softbank-nav-monitor",
        generation=1,
        runner="container",
        command_argv=["python", "-m", "task"],
        workdir="/workspace",
        timeout_seconds=120,
        no_output_timeout_seconds=60,
        required_secrets=[],
        cos_mode="skip",
        notify_enabled=True,
        scheduled_slot_utc=slot_utc,
        deadline_utc=slot_utc,
        trigger_source="cron",
        is_trial=False,
    )
    committed_bad, _ = await sched.finalize_task_run(
        _FakeConn(),
        lock_conn=object(),
        docker_client=None,
        claimed=claimed_bad,
        container_id=None,
        outcome=ContainerInspectionOutcome(
            container_id="cid-bad",
            running=False,
            exit_code=0,
            oom_killed=False,
            log_tail="ok",
        ),
        evaluation=TaskEvaluationResult(ok=True, status="succeeded"),
        outbox_payload=mismatched_outbox,
        frozen_manifest={"job_key": "softbank-nav-monitor", "status": "ok"},
        path_map=path_map,
    )
    assert committed_bad is True
    failed_update = executed_updates[-1]
    assert failed_update["terminal_status"] == "failed"
    assert failed_update["notify_status"] == "skipped"
    assert failed_update["outbox_payload"] is None
    assert "report_preparation_failed" in str(failed_update["failure_reason"])

    # --- 5. Text-only records without images continue to build plans without image metadata ---
    text_only_plan = build_data_task_delivery_plan(
        run_id="00000000-0000-4000-8000-000000000500",
        job_key="softbank-nav-monitor",
        raw_outbox={"text": "text-only summary", "items": [{"index": 0, "text": "item 0"}]},
        run_artifacts_dir=t52_dir,
    )
    assert len(text_only_plan.items) == 1
    assert len(text_only_plan.artifacts_by_id) == 0


@pytest.mark.asyncio
async def test_outer_reconcile_identity_inspection_and_enforcement_rejects_conflicts_and_partial_metadata() -> None:
    """Persisted outer notification identity inspection rejects recipient/key conflicts, partial metadata, and disagreeing alias rows."""
    import psycopg

    run_id = "00000000-0000-4000-8000-000000000601"
    keys_a = [
        dtn_db.build_data_task_item_delivery_key(
            run_id=run_id,
            platform="feishu",
            recipient_id="ou_recipient_a",
            item_index=0,
        ),
        dtn_db.build_data_task_item_delivery_key(
            run_id=run_id,
            platform="feishu",
            recipient_id="ou_recipient_a",
            item_index=1,
        ),
    ]
    expected_a = dtn_db.build_reconcile_identity_metadata(
        platform="feishu",
        recipient_id="ou_recipient_a",
        canonical_target_address="feishu:@me",
        delivery_keys=keys_a,
    )

    # 1. Unstamped payload without any reconcile_* keys
    assert dtn_db._inspect_stored_reconcile_identity({"route_key": "data:sb"}, expected_meta=expected_a) == "unstamped"

    # 2. Exact match on all 5 fields
    assert (
        dtn_db._inspect_stored_reconcile_identity(
            {"route_key": "data:sb", **expected_a},
            expected_meta=expected_a,
        )
        == "matches"
    )

    # 3. Partial or malformed stored metadata -> "malformed"
    for malformed_payload in (
        {"reconcile_recipient_id": "ou_recipient_a"},
        {**expected_a, "expected_item_count": True},
        {**expected_a, "expected_item_count": 1},
        {**expected_a, "reconcile_delivery_keys": ["only_one"]},
        {**expected_a, "reconcile_delivery_keys": ["", keys_a[1]]},
    ):
        assert (
            dtn_db._inspect_stored_reconcile_identity(
                malformed_payload,
                expected_meta=expected_a,
            )
            == "malformed"
        )

    # 4. Recipient, canonical target, or ordered key mismatch -> "mismatch"
    keys_b = [
        dtn_db.build_data_task_item_delivery_key(
            run_id=run_id,
            platform="feishu",
            recipient_id="ou_recipient_b",
            item_index=0,
        ),
        dtn_db.build_data_task_item_delivery_key(
            run_id=run_id,
            platform="feishu",
            recipient_id="ou_recipient_b",
            item_index=1,
        ),
    ]
    expected_b = dtn_db.build_reconcile_identity_metadata(
        platform="feishu",
        recipient_id="ou_recipient_b",
        canonical_target_address="feishu:@me",
        delivery_keys=keys_b,
    )
    assert dtn_db._inspect_stored_reconcile_identity(expected_a, expected_meta=expected_b) == "mismatch"
    assert (
        dtn_db._inspect_stored_reconcile_identity(
            {**expected_a, "reconcile_delivery_keys": list(reversed(keys_a))},
            expected_meta=expected_a,
        )
        == "mismatch"
    )

    # 5. Candidate outer rows where one is unstamped and an alias row has conflicting recipient B:
    #    `_persist_reconcile_identity_on_outer_delivery` must reject BEFORE executing any UPDATE.
    executed_queries: list[str] = []

    async def _disagreeing_sql(sql: str, params: Any = None) -> None:
        _ = params
        norm = " ".join(sql.split())
        executed_queries.append(norm)
        if "FROM dalton_notify_deliveries" in norm:
            cur.fetchall.return_value = [
                {
                    "delivery_id": str(uuid4()),
                    "user_id": "pm_owner",
                    "channel": "feishu",
                    "target": "feishu:@me",
                    "status": "pending",
                    "payload_json": {"route_key": "data:sb"},
                },
                {
                    "delivery_id": str(uuid4()),
                    "user_id": "pm_owner",
                    "channel": "feishu",
                    "target": "feishu:user:ou_recipient_a",
                    "status": "pending",
                    "payload_json": {"route_key": "data:sb", **expected_b},
                },
            ]
        elif "FROM dalton_channel_deliveries" in norm:
            cur.fetchall.return_value = []

    cur = create_autospec(psycopg.AsyncCursor, instance=True)
    cur.execute.side_effect = _disagreeing_sql
    outcome = await dtn_db._persist_reconcile_identity_on_outer_delivery(
        cur,
        run_id=run_id,
        job_key="softbank-nav-monitor",
        owner_user_id="pm_owner",
        platform="feishu",
        target_kind="dm",
        requested_target="feishu:user:ou_recipient_a",
        canonical_target_address="feishu:@me",
        recipient_id="ou_recipient_a",
        delivery_keys=keys_a,
    )
    assert outcome.ok is False
    assert outcome.code == "recipient_identity_conflict"
    assert not any(q.startswith("UPDATE dalton_notify_deliveries") for q in executed_queries)
