"""B-batch contract tests for the researchdata bridge: guidepoint's 30 cap
(B3), Chinese permission/quota signals (B4), the AlphaEngine usage ledger
(B5), and the markdown-image binding helpers (C2)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402


# ---------------------------------------------------------------------- B3

def test_guidepoint_catalog_documents_the_30_cap():
    spec = rb.ROUTE_CONTRACT_SPECS["guidepoint"]
    assert "1-30" in spec["param_formats"]["limit"]
    assert "40" not in spec["param_formats"]["limit"]


def _bare_bridge() -> rb.ResearchDataBridge:
    bridge = rb.ResearchDataBridge.__new__(rb.ResearchDataBridge)
    bridge.cfg = create_autospec(rb.BridgeConfig, instance=True)
    bridge.cfg.caller_budget_s = 625
    return bridge


@pytest.mark.asyncio
async def test_guidepoint_fanout_clamps_limit_to_30():
    bridge = _bare_bridge()
    gp_args, win_err = bridge._build_guidepoint_fanout_args(
        {"query": "Meta Reels", "limit": 100}
    )
    assert win_err is None
    assert gp_args["limit"] == 30


@pytest.mark.asyncio
async def test_guidepoint_fanout_keeps_small_limits():
    bridge = _bare_bridge()
    gp_args, _ = bridge._build_guidepoint_fanout_args(
        {"query": "Meta Reels", "limit": 10}
    )
    assert gp_args["limit"] == 10


def test_mcp_field_validation_is_a_parameter_error_naming_the_field():
    payload = {
        "ok": False,
        "status": "error",
        "error": "MCP_VALIDATION_INVALID_FIELD: size must be <= 30 (got 100)",
    }
    category, hint = rb._classify_error_category_and_hint(
        "research", "guidepoint", "search_library", payload
    )
    assert category == "参数错误"
    assert "size must be <= 30" in hint


# ---------------------------------------------------------------------- B4

@pytest.mark.parametrize(
    "message",
    [
        "无查看权限或额度已达上限",
        "该账号额度已达上限，请明天再试",
        "无查看权限，请联系管理员开通",
        "登录已过期，请重新授权 AlphaEngine",
    ],
)
def test_alphaengine_chinese_permission_refusals_are_permission_or_quota(message):
    payload = {"ok": False, "status": "error", "error": message}
    category, hint = rb._classify_error_category_and_hint(
        "research", "alphaengine", "search_library", payload
    )
    assert category == "权限或额度"
    assert "不要" not in hint or "重试" in hint or "切换" in hint
    # The category's standing advice: switch sources and say so in the report.
    assert "切换" in hint or "替代" in hint


def test_english_quota_failure_stays_permission_or_quota():
    payload = {"ok": False, "status": "quota_exceeded", "error": "quota gone"}
    category, _ = rb._classify_error_category_and_hint(
        "research", "alphaengine", "search_library", payload
    )
    assert category == "权限或额度"


# ---------------------------------------------------------------------- B5

def _bridge_with_usage_file(tmp_path: Path) -> rb.ResearchDataBridge:
    cfg = create_autospec(rb.BridgeConfig, instance=True)
    cfg.alphaengine_usage_file = tmp_path / "alphaengine_usage.jsonl"
    bridge = rb.ResearchDataBridge.__new__(rb.ResearchDataBridge)
    bridge.cfg = cfg
    bridge._usage_lock = asyncio.Lock()
    return bridge


@pytest.mark.asyncio
async def test_usage_ledger_line_fields_and_hash_prefix(tmp_path):
    bridge = _bridge_with_usage_file(tmp_path)
    await bridge._record_alphaengine_usage(
        operation="search_library",
        arguments={"query": "META AI capex"},
        ok=True,
        error_category="",
        count=7,
        caller={"user": "u1", "workspace": "ws1", "thread": "t1"},
    )
    lines = (tmp_path / "alphaengine_usage.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    import hashlib

    assert entry["op"] == "search"
    assert entry["query_sha256_12"] == hashlib.sha256(b"META AI capex").hexdigest()[:12]
    assert entry["doc_id"] == ""
    assert entry["count"] == 7
    assert entry["ok"] is True
    assert entry["error_category"] == ""
    assert entry["user"] == "u1"
    assert entry["workspace"] == "ws1"
    assert entry["thread"] == "t1"
    assert entry["ts"]


@pytest.mark.asyncio
async def test_usage_ledger_maps_read_op_and_missing_identity(tmp_path):
    bridge = _bridge_with_usage_file(tmp_path)
    await bridge._record_alphaengine_usage(
        operation="get_document",
        arguments={"params": {"doc_id": "99001122"}},
        ok=False,
        error_category="权限或额度",
        count=0,
        caller=None,
    )
    entry = json.loads(
        (tmp_path / "alphaengine_usage.jsonl").read_text().strip().splitlines()[0]
    )
    assert entry["op"] == "read"
    assert entry["doc_id"] == "99001122"
    assert entry["ok"] is False
    assert entry["error_category"] == "权限或额度"
    # Missing identity headers record as empty strings, never an error.
    assert entry["user"] == "" and entry["workspace"] == "" and entry["thread"] == ""


@pytest.mark.asyncio
async def test_usage_ledger_rotates_on_size(tmp_path):
    bridge = _bridge_with_usage_file(tmp_path)
    bridge._ALPHAENGINE_USAGE_MAX_BYTES = 1
    await bridge._record_alphaengine_usage(
        operation="search_library", arguments={"query": "a"}, ok=True,
        error_category="", count=1, caller=None,
    )
    await bridge._record_alphaengine_usage(
        operation="search_library", arguments={"query": "b"}, ok=True,
        error_category="", count=1, caller=None,
    )
    assert (tmp_path / "alphaengine_usage.jsonl.1").is_file()
    assert (tmp_path / "alphaengine_usage.jsonl").is_file()


def _write_ledger(path: Path, entries: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e, ensure_ascii=False) + "\n")


def test_usage_report_summary_and_duplicates(tmp_path):
    sys.path.insert(0, str(SCRIPTS_DALTON))
    import alphaengine_usage_report as report

    ledger = tmp_path / "alphaengine_usage.jsonl"
    _write_ledger(
        ledger,
        [
            {"ts": "2026-10-26T01:00:00+00:00", "op": "search", "ok": True,
             "query_sha256_12": "aaa", "doc_id": "", "user": "u1", "thread": "t1", "count": 5},
            {"ts": "2026-10-26T02:00:00+00:00", "op": "search", "ok": True,
             "query_sha256_12": "aaa", "doc_id": "", "user": "u1", "thread": "t1", "count": 5},
            {"ts": "2026-10-26T03:00:00+00:00", "op": "read", "ok": True,
             "query_sha256_12": "", "doc_id": "99001122", "user": "u2", "thread": "t2", "count": 1},
            {"ts": "2026-10-27T01:00:00+00:00", "op": "read", "ok": True,
             "query_sha256_12": "", "doc_id": "99001122", "user": "u2", "thread": "t2", "count": 1},
            {"ts": "2026-10-27T02:00:00+00:00", "op": "read", "ok": False,
             "query_sha256_12": "", "doc_id": "", "user": "", "thread": "t1", "count": 0},
        ],
    )
    summary = report.summarize(report.load_entries([ledger]))
    assert summary["total_calls"] == 5
    assert summary["by_day"]["2026-10-26"]["total"] == 3
    assert summary["by_day"]["2026-10-26"]["search.ok"] == 2
    assert summary["by_day"]["2026-10-27"]["read.fail"] == 1
    assert summary["by_user"]["u1"]["search"] == 2
    assert summary["repeated_query_searches"] == {"aaa": 2}
    assert summary["repeated_doc_reads"] == {"99001122": 2}
    assert list(summary["top_threads"].items())[0] == ("t1", 3)


# ---------------------------------------------------------------------- C2

def test_markdown_image_refs_in_order():
    refs = rb._markdown_image_refs(
        "before ![a](https://x.test/1.png) text ![b](charts/local.png) "
        'after ![c](https://x.test/2.jpg "title")'
    )
    assert refs == ["https://x.test/1.png", "charts/local.png", "https://x.test/2.jpg"]


def test_image_url_block_reason_rejects_private_and_localhost():
    assert rb._feishu_image_url_block_reason("http://localhost/img.png") is not None
    assert rb._feishu_image_url_block_reason("http://127.0.0.1/img.png") is not None
    assert rb._feishu_image_url_block_reason("http://192.168.1.4/img.png") is not None
    assert rb._feishu_image_url_block_reason("ftp://x.test/1.png") is not None
    assert rb._feishu_image_url_block_reason("https://upload.wikimedia.org/a.png") is None


def test_bind_docx_images_reports_count_mismatch():
    client = create_autospec(rb.FeishuClient, instance=True)
    client.list_blocks.return_value = [
        {"block_id": "b1", "block_type": rb.FEISHU_DOC_IMAGE_BLOCK_TYPE, "image": {}},
    ]
    outcome = rb.bind_docx_images(client, "doc1", "![x](a.png) ![y](b.png)", {})
    assert outcome["total"] == 2
    assert outcome["bound"] == 0
    assert len(outcome["failed"]) == 2
    assert "不一致" in outcome["failed"][0]["reason"]


def test_bind_docx_images_success_and_verify(tmp_path):
    import base64

    client = create_autospec(rb.FeishuClient, instance=True)
    client.list_blocks.return_value = [
        {"block_id": "b1", "block_type": rb.FEISHU_DOC_IMAGE_BLOCK_TYPE, "image": {}},
        {"block_id": "b2", "block_type": 2, "text": {}},
    ]
    # First verify read: unbound; after the retry upload: bound.
    client.get_block.side_effect = [
        {"block_id": "b1", "image": {}},
        {"block_id": "b1", "image": {"token": "tok1"}},
    ]
    client.upload_docx_image.return_value = "tok_uploaded"
    png = base64.b64encode(b"\x89PNG fake").decode("ascii")
    outcome = rb.bind_docx_images(
        client,
        "doc1",
        "![x](local.png)",
        {"local.png": {"filename": "local.png", "content_b64": png}},
    )
    assert outcome["total"] == 1
    assert outcome["bound"] == 1
    assert outcome["failed"] == []
    # The binding retried with the document id as the upload point once the
    # block-level upload left the block unbound, and bound the token via PATCH.
    assert client.upload_docx_image.call_count == 2
    second_kwargs = client.upload_docx_image.call_args_list[1].kwargs
    assert second_kwargs["image_block_id"] == "doc1"
    client.replace_block_image.assert_called_with("doc1", "b1", "tok_uploaded")


def test_bind_docx_images_uses_insert_response_block_id_relations():
    import base64

    client = create_autospec(rb.FeishuClient, instance=True)
    client.upload_docx_image.return_value = "tok_rel"
    client.get_block.return_value = {"block_id": "real_img_1", "image": {"token": "tok_rel"}}
    png = base64.b64encode(b"\x89PNG fake").decode("ascii")
    outcome = rb.bind_docx_images(
        client,
        "doc1",
        "![x](local.png)",
        {"local.png": {"filename": "local.png", "content_b64": png}},
        converted_blocks=[
            {"block_id": "tmp_img_1", "block_type": rb.FEISHU_DOC_IMAGE_BLOCK_TYPE, "image": {}}
        ],
        insert_response={
            "block_id_relations": [
                {"temporary_block_id": "tmp_img_1", "block_id": "real_img_1"}
            ]
        },
    )
    assert outcome == {"total": 1, "bound": 1, "failed": []}
    client.list_blocks.assert_not_called()
    client.replace_block_image.assert_called_once_with("doc1", "real_img_1", "tok_rel")

