"""Unit tests for `market-alert-ta-screen` profile, `md_to_report_cards.py`, and shared `render_card.py`."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import psycopg
import pytest

from ptc_agent.agent.middleware.skills import dalton_registry as dr_mod
from ptc_agent.agent.middleware.skills import registry as skill_reg_mod
from src.config.core import clear_config_cache
from src.dalton.capabilities import CapabilityId, has_capability
from src.dalton.data_jobs.notification_payloads import (
    FEISHU_CARD_MAX_BYTES,
    FEISHU_CARD_MAX_ELEMENTS,
    FEISHU_CARD_TABLE_LIMIT,
    feishu_card_serialized_utf8_bytes,
    is_feishu_card_within_envelope,
)
from src.dalton.database.automation_reports import (
    REPORT_PROFILES,
    parse_report_profile,
    prepare_automation_report_content,
)
from src.dalton.services import report_delivery as rd_svc
from src.dalton.settings import get_dalton_config
from src.dalton.tools.report_delivery_tools import create_report_delivery_tools

REPO_ROOT = Path(__file__).resolve().parents[3]
BULLBEAR_SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-bullbear-tracker"
CONVENTIONS_SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-conventions"
SCRIPT_PATH = BULLBEAR_SKILL_DIR / "scripts" / "md_to_report_cards.py"
RENDERER_PATH = BULLBEAR_SKILL_DIR / "scripts" / "render_card.py"

_AUTO_UUID = "11111111-1111-1111-1111-111111111111"
_OWNER = "user-ta-owner"
_FEISHU_CHAT = "feishu:chat:oc_market_alert_group"
_DISCORD_DM = "discord:user:424242424242424242"

# Exact verbatim text of `.agy/ta_screen_{am,pm}_instruction.new.txt`
_TA_AM_INSTRUCTION = (
    "执行美股盘后技术面每日多空筛选（早班）：\n"
    "1. 运行 `python3 screener/daily_job.py --session am`（自动取刚收盘的美股交易日 asof，"
    "增量更新美股日线与基准 ETF，计算 9 个多空信号、市场状态广度、多空各前 15 候选及前 3 精看结论卡片，"
    "输出 `push/<asof>_market_alert_am.md` 与图表附件）。若当日美股休市（最新 K 线日期早于上一交易日且与上期重复），"
    '调用 `automation_report(action="submit", report_run_id="ta-screen-am-<YYYY-MM-DD>", material="no")`'
    "（日期取当日 `YYYY-MM-DD`，不传 `cards`），简短说明“美股休市，无新增日线信号”后结束。\n"
    "2. 读取 `screener/output/<asof>/regime.json`、`screener/output/<asof>/cards.json` 和生成的 "
    "`push/<asof>_market_alert_am.md`，核对前 3 名多空候选的事件形态（财报跳空、死猫跳、旗形突破等）、止损位与盈亏比。\n"
    "3. 在 `screener/output/<asof>/notes.json` 中更新 `headline_am` 与 `us` 一句话核心判断"
    "（直接给结论、广度边际变化与前 3 候选特征，不写套话），重跑 "
    "`python3 screener/make_push.py --asof <asof> --session am --out push/<asof>_market_alert_am.md`。\n"
    "4. 运行 `python3 .agents/skills/dalton-bullbear-tracker/scripts/md_to_report_cards.py "
    "push/<asof>_market_alert_am.md --title-from-first-line --out push/<asof>_market_alert_am.cards.json` "
    "生成卡片文件；读取该 JSON 文件并把解析出的卡片数组直接传给 "
    '`automation_report(action="submit", report_run_id="ta-screen-am-<asof>", material="yes", cards=<解析后的数组>)`'
    "（`<asof>` 为 `YYYY-MM-DD`，卡片内仅写报告正文，由任务投递配置决定接收方），再调用 "
    '`automation_report(action="status", report_run_id="ta-screen-am-<asof>")` 核对 '
    '`ok=true`、`status="sent"`、`delivery_confirmed=true`。若提交被拒或状态为 `pending`、`unknown`、`failed`，'
    "保留原卡片文件与同一 `report_run_id`：可重试错误用同一 `report_run_id` 与同一数组重试提交一次并复查 `status`；"
    "若为 `content_conflict`、`not_automation_run` 或重试后仍未确认，按工具返回在最终回复中如实说明当前状态与原因。"
)

_TA_PM_INSTRUCTION = (
    "执行港股与A股盘后技术面每日多空筛选（晚班）：\n"
    "1. 运行 `python3 screener/daily_job.py --session pm`（自动取当日 asof，每周一刷新股票池；"
    "增量更新港股与A股日线及基准 ETF，计算 9 个多空信号、市场状态广度、多空各前 15 候选及前 3 精看结论卡片，"
    "核对 HKEX 可卖空名单，A股空头标“回避/减仓”，输出 `push/<asof>_market_alert_pm.md` 与图表附件）。"
    "若港股与A股同日休市，调用 "
    '`automation_report(action="submit", report_run_id="ta-screen-pm-<YYYY-MM-DD>", material="no")`'
    "（日期取当日 `YYYY-MM-DD`，不传 `cards`），简短说明“港股与A股今日休市，无新增日线信号”后结束。\n"
    "2. 读取 `screener/output/<asof>/regime.json`、`screener/output/<asof>/cards.json` 和生成的 "
    "`push/<asof>_market_alert_pm.md`，核对港股可卖空标记、A股回测负超额降级提示（观察不执行）、止损位与盈亏比。\n"
    "3. 在 `screener/output/<asof>/notes.json` 中更新 `headline_pm`、`hk`、`cn` 一句话核心判断"
    "（直接给结论、广度边际变化与前 3 候选特征，不写套话），重跑 "
    "`python3 screener/make_push.py --asof <asof> --session pm --out push/<asof>_market_alert_pm.md`。\n"
    "4. 运行 `python3 .agents/skills/dalton-bullbear-tracker/scripts/md_to_report_cards.py "
    "push/<asof>_market_alert_pm.md --title-from-first-line --out push/<asof>_market_alert_pm.cards.json` "
    "生成卡片文件；读取该 JSON 文件并把解析出的卡片数组直接传给 "
    '`automation_report(action="submit", report_run_id="ta-screen-pm-<asof>", material="yes", cards=<解析后的数组>)`'
    "（`<asof>` 为 `YYYY-MM-DD`，卡片内仅写报告正文，由任务投递配置决定接收方），再调用 "
    '`automation_report(action="status", report_run_id="ta-screen-pm-<asof>")` 核对 '
    '`ok=true`、`status="sent"`、`delivery_confirmed=true`。若提交被拒或状态为 `pending`、`unknown`、`failed`，'
    "保留原卡片文件与同一 `report_run_id`：可重试错误用同一 `report_run_id` 与同一数组重试提交一次并复查 `status`；"
    "若为 `content_conflict`、`not_automation_run` 或重试后仍未确认，按工具返回在最终回复中如实说明当前状态与原因。"
)


def _load_script_module() -> Any:
    spec = importlib.util.spec_from_file_location("test_md_to_report_cards_mod", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_renderer_module() -> Any:
    spec = importlib.util.spec_from_file_location("test_shared_render_card_mod", RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_table(idx: int, rows: int = 3) -> str:
    lines = [
        f"| 标的_{idx} | 信号 | 止损位 | 盈亏比 |",
        "| --- | --- | --- | --- |",
    ]
    for r in range(1, rows + 1):
        lines.append(f"| T{idx}{r} | 旗形突破 | {100 + r}.0 | 2.{r}x |")
    return "\n".join(lines)


def _all_card_markdown_text(cards: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for card in cards:
        for elem in card.get("body", {}).get("elements", []):
            if elem.get("tag") == "markdown" and isinstance(elem.get("content"), str):
                parts.append(elem["content"])
    return "\n".join(parts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_name", "instruction", "draft_rel"),
    [
        ("dalton-ta-screen-am", _TA_AM_INSTRUCTION, ".agy/ta_screen_am_instruction.new.txt"),
        ("dalton-ta-screen-pm", _TA_PM_INSTRUCTION, ".agy/ta_screen_pm_instruction.new.txt"),
    ],
)
async def test_market_alert_ta_screen_profile_passes_report_validation_and_delivery_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    task_name: str,
    instruction: str,
    draft_rel: str,
) -> None:
    draft_file = REPO_ROOT / draft_rel
    if draft_file.is_file():
        assert draft_file.read_text(encoding="utf-8").strip() == instruction.strip()

    assert "market-alert-ta-screen" in REPORT_PROFILES

    metadata = {
        "report_profile": {
            "profile": "market-alert-ta-screen",
            "report_targets": [_FEISHU_CHAT],
            "failure_targets": [_DISCORD_DM],
        }
    }
    parsed, err = parse_report_profile(metadata)
    assert err is None
    assert parsed is not None
    assert parsed["profile"] == "market-alert-ta-screen"

    # Draft instruction references the shared md_to_report_cards.py inside
    # dalton-bullbear-tracker/scripts/, which must NOT bind the task to bullbear tracker.
    assert rd_svc.extract_report_skills(instruction) == []
    assert rd_svc.instruction_uses_report_skill(instruction) is True
    assert "market-alert-ta-screen" in rd_svc.allowed_profiles_for_instruction(instruction)
    assert (
        rd_svc.resolve_report_profile_id(
            instruction,
            requested_profile="market-alert-ta-screen",
        )
        == "market-alert-ta-screen"
    )

    row = {
        "automation_id": _AUTO_UUID,
        "user_id": _OWNER,
        "name": task_name,
        "instruction": instruction,
        "metadata": {},
        "config_version": 1,
    }
    monkeypatch.setattr(rd_svc, "get_user_tier", create_autospec(rd_svc.get_user_tier, return_value="internal"))

    async def _fake_get_auto(aid: str, uid: str, *, conn: Any = None) -> dict[str, Any] | None:
        return dict(row) if aid == _AUTO_UUID and uid == _OWNER else None

    monkeypatch.setattr(
        rd_svc.auto_db,
        "get_automation",
        create_autospec(rd_svc.auto_db.get_automation, side_effect=_fake_get_auto),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_caller_dm_identities",
        create_autospec(
            rd_svc.list_caller_dm_identities,
            return_value=[
                {
                    "platform": "discord",
                    "platform_user_id": "424242424242424242",
                    "target": _DISCORD_DM,
                    "supports_report_cards": False,
                    "supports_text_targets": True,
                    "supports_failure_targets": True,
                }
            ],
        ),
    )
    monkeypatch.setattr(
        rd_svc,
        "list_authorized_group_bindings",
        create_autospec(
            rd_svc.list_authorized_group_bindings,
            return_value=[
                {
                    "platform": "feishu",
                    "chat_address": "feishu:oc_market_alert_group",
                    "chat_name": "Market Alert",
                    "workspace_id": None,
                    "bound_by_user_id": "user-admin",
                    "target": _FEISHU_CHAT,
                }
            ],
        ),
    )
    monkeypatch.setattr(
        rd_svc,
        "get_feishu_open_id_for_user",
        create_autospec(rd_svc.get_feishu_open_id_for_user, return_value="ou_ta_owner"),
    )
    monkeypatch.setattr(
        rd_svc,
        "verify_feishu_chat_member",
        create_autospec(rd_svc.verify_feishu_chat_member, return_value=(True, None)),
    )
    monkeypatch.setattr(
        rd_svc,
        "sync_owner_group_chat_settings",
        create_autospec(rd_svc.sync_owner_group_chat_settings, return_value=None),
    )
    monkeypatch.setattr(
        rd_svc,
        "resolve_dalton_notify_targets",
        create_autospec(rd_svc.resolve_dalton_notify_targets, return_value=[_DISCORD_DM]),
    )
    monkeypatch.setattr(
        rd_svc,
        "apply_dalton_notify_preference_patch",
        create_autospec(rd_svc.apply_dalton_notify_preference_patch, return_value={}),
    )
    monkeypatch.setattr(
        rd_svc,
        "invalidate_user_prefs_cache",
        create_autospec(rd_svc.invalidate_user_prefs_cache, return_value=None),
    )
    monkeypatch.setattr(
        rd_svc,
        "invalidate_user_profile_cache",
        create_autospec(rd_svc.invalidate_user_profile_cache, return_value=None),
    )
    monkeypatch.setattr(
        rd_svc,
        "notify_admins_group_target_change",
        create_autospec(rd_svc.notify_admins_group_target_change, return_value={"notified": True}),
    )
    monkeypatch.setattr(
        rd_svc,
        "resolve_dalton_notify_enabled",
        create_autospec(rd_svc.resolve_dalton_notify_enabled, return_value=True),
    )
    monkeypatch.setattr(
        rd_svc,
        "evaluate_user_notify_eligibility",
        create_autospec(rd_svc.evaluate_user_notify_eligibility, return_value=(True, None, [])),
    )

    fake_tx = create_autospec(psycopg.AsyncTransaction, instance=True)
    fake_tx.__aenter__.return_value = fake_tx
    fake_tx.__aexit__.return_value = False

    fake_cur = create_autospec(psycopg.AsyncCursor, instance=True)
    fake_cur.__aenter__.return_value = fake_cur
    fake_cur.__aexit__.return_value = False
    fake_cur.fetchone.return_value = None

    fake_conn = create_autospec(psycopg.AsyncConnection, instance=True)
    fake_conn.transaction.return_value = fake_tx
    fake_conn.cursor.return_value = fake_cur

    @asynccontextmanager
    async def _fake_get_conn(conn: Any = None):
        yield fake_conn if conn is None else conn

    monkeypatch.setattr(
        rd_svc,
        "get_db_connection",
        create_autospec(rd_svc.get_db_connection, side_effect=_fake_get_conn),
    )

    async def _fake_update_auto(
        aid: str, uid: str, *, conn: Any = None, **kwargs: Any
    ) -> dict[str, Any]:
        updated = dict(row)
        updated["metadata"] = kwargs.get("metadata", row["metadata"])
        return updated

    monkeypatch.setattr(
        rd_svc.auto_db,
        "update_automation",
        create_autospec(rd_svc.auto_db.update_automation, side_effect=_fake_update_auto),
    )
    monkeypatch.setenv("DALTON_BACKEND_VAR_DIR", str(tmp_path / "var_dalton"))

    from src.dalton.tools import report_delivery_tools as rd_tools

    captured_cards: list[tuple[str, dict[str, Any]]] = []

    def _approve_hitl(action_type: str, payload: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
        captured_cards.append((action_type, dict(payload)))
        return True, {
            "decisions": [{"type": "approve", "confirm_token": payload.get("confirm_token")}]
        }

    monkeypatch.setattr(
        rd_tools,
        "hitl_confirm",
        create_autospec(rd_tools.hitl_confirm, side_effect=_approve_hitl),
    )

    tool = create_report_delivery_tools()[0]
    cmd = await tool.ainvoke(
        {
            "name": tool.name,
            "args": {
                "action": "set",
                "automation_id": _AUTO_UUID,
                "profile": "market-alert-ta-screen",
                "report_targets": [_FEISHU_CHAT],
            },
            "id": f"tc-ta-screen-{task_name}",
            "type": "tool_call",
        },
        config={"configurable": {"user_id": _OWNER, "thread_id": "t-ta"}},
    )
    assert len(captured_cards) == 1
    assert captured_cards[0][0] == "dalton_report_delivery_set"
    assert captured_cards[0][1]["report_targets"] == [_FEISHU_CHAT]
    assert captured_cards[0][1]["failure_targets"] == [_DISCORD_DM]
    update = getattr(cmd, "update", None)
    assert isinstance(update, dict) and update.get("messages")
    res = json.loads(update["messages"][0].content)
    assert res["ok"] is True
    assert res["report_profile"] == {
        "profile": "market-alert-ta-screen",
        "report_targets": [_FEISHU_CHAT],
        "failure_targets": [_DISCORD_DM],
    }


def test_bullbear_skill_references_still_enforce_tracker_profiles_except_shared_helper() -> None:
    # Genuine bullbear skill references must still bind to dalton-bullbear-tracker
    for bullbear_inst in (
        "按 .agents/skills/dalton-bullbear-tracker/SKILL.md 运行 MSFT 日跟踪。",
        "运行 .agents/skills/dalton-bullbear-tracker/scripts/render_card.py 生成卡片。",
        "运行 .agents/skills/dalton-bullbear-tracker/scripts/collect.py 采集并按 dalton-bullbear-tracker 提交。",
    ):
        assert rd_svc.extract_report_skills(bullbear_inst) == ["dalton-bullbear-tracker"]
        with pytest.raises(rd_svc.ReportDeliveryError) as exc_info:
            rd_svc.resolve_report_profile_id(
                bullbear_inst,
                requested_profile="market-alert-ta-screen",
            )
        assert exc_info.value.code == "profile_skill_mismatch"

    # Adjacent HTTPS reference definitions stay accepted by server validation (e.g. x-accounts-daily)
    https_refs = (
        "![图1][r1]\n![图2][r2]\n\n"
        "[r1]: https://example.com/a.png\n"
        "[r2]: https://example.com/b.png\n"
    )
    card_ok, card_err = prepare_automation_report_content(
        profile="x-accounts-daily",
        cards=[
            {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": "X 账号日报"}},
                "body": {"elements": [{"tag": "markdown", "content": https_refs}]},
            }
        ],
        failure_text=None,
    )
    assert card_err is None and card_ok is not None
    text_ok, text_err = prepare_automation_report_content(
        profile="x-accounts-daily",
        cards=None,
        failure_text=None,
        text=https_refs,
    )
    assert text_err is None and text_ok is not None


def test_core_profile_and_release_bundle_keep_script_co_located_under_fund_automations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Tool lives alongside render_card.py in dalton-bullbear-tracker, never in dalton-conventions
    assert SCRIPT_PATH.is_file()
    assert RENDERER_PATH.is_file()
    assert SCRIPT_PATH.parent == RENDERER_PATH.parent
    assert not (CONVENTIONS_SKILL_DIR / "scripts" / "md_to_report_cards.py").exists()
    assert "md_to_report_cards.py" not in (CONVENTIONS_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "md_to_report_cards.py" in (BULLBEAR_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    # Under Core profile default, FUND_AUTOMATIONS is off and dalton-bullbear-tracker stays out of sandbox
    monkeypatch.setenv("APP_ENV", "core")
    monkeypatch.delenv("DALTON_PROFILE", raising=False)
    monkeypatch.delenv("DALTON_CAP_FUND_AUTOMATIONS", raising=False)
    clear_config_cache()
    get_dalton_config.cache_clear()

    assert has_capability(CapabilityId.FUND_AUTOMATIONS) is False
    assert dr_mod.DALTON_SKILL_CAPABILITY_MAP["dalton-bullbear-tracker"] is CapabilityId.FUND_AUTOMATIONS
    assert "dalton-bullbear-tracker" in dr_mod.disabled_dalton_skills()
    assert "dalton-bullbear-tracker" in skill_reg_mod.skills_outside_sandbox()

    # Enabled Core skills must not ship md_to_report_cards.py or any script depending on render_card.py
    enabled_core_skills = [
        name for name in dr_mod.DALTON_SKILL_DEFINITIONS if dr_mod.is_dalton_skill_enabled(name)
    ]
    assert "dalton-conventions" in enabled_core_skills
    assert "dalton-bullbear-tracker" not in enabled_core_skills
    for skill_name in enabled_core_skills:
        scripts_dir = dr_mod.DALTON_SKILLS_DIR / skill_name / "scripts"
        if not scripts_dir.is_dir():
            continue
        for script_file in scripts_dir.rglob("*.py"):
            assert script_file.name != "md_to_report_cards.py"
            content = script_file.read_text(encoding="utf-8")
            assert "render_card.py" not in content, (
                f"Core skill script {script_file} must not depend on disabled skill renderer render_card.py"
            )

    # Confirm scripts/standalone/package_release.sh does not split md_to_report_cards.py from render_card.py
    pkg_text = (REPO_ROOT / "scripts" / "standalone" / "package_release.sh").read_text(encoding="utf-8")
    assert '"plugins/dalton"' in pkg_text
    assert "md_to_report_cards.py" not in pkg_text


def test_render_card_preserves_preamble_and_splits_oversized_table_row_with_headers(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer_module()

    # 1. Preamble before first ## heading must be preserved in both single-card and multi-card outputs
    preamble_head = "PREAMBLE_START_引言首句：本日美股广度转强。"
    preamble_tail = "PREAMBLE_END_引言尾句：重点跟踪突破确认。"
    single_md = f"# 日报标题\n\n{preamble_head}\n{preamble_tail}\n\n## 第一节\n\n正文一。\n"
    single_cards = renderer.markdown_to_card_chunks(single_md, title="日报标题")
    assert len(single_cards) == 1
    single_text = _all_card_markdown_text(single_cards)
    assert preamble_head in single_text
    assert preamble_tail in single_text
    assert "正文一。" in single_text

    # Multi-card split must also retain preamble
    multi_md = (
        f"# 多卡日报\n\n{preamble_head}\n{preamble_tail}\n\n"
        + "\n\n".join(f"## 章节 {i}\n\n" + ("长段分析内容。" * 250) for i in range(1, 8))
    )
    multi_cards = renderer.markdown_to_card_chunks(multi_md, title="多卡日报")
    assert len(multi_cards) >= 2
    multi_text = _all_card_markdown_text(multi_cards)
    assert preamble_head in multi_text
    assert preamble_tail in multi_text

    # 2. Single oversized table row (>30 KB) must split continuously with repeated header on EVERY card
    row_head = "ROW_HEAD_START_ALPHA"
    row_mid = "中段持续数据验证记录_" * 1400
    row_tail = "ROW_TAIL_END_OMEGA"
    end_col = "STOP_123.45"
    table_hdr = "| 代码 | 详细分析 | 止损位 |"
    table_sep = "| --- | --- | --- |"
    oversized_table_md = (
        "# 超长单行表\n\n"
        "## 候选明细\n\n"
        f"{table_hdr}\n"
        f"{table_sep}\n"
        f"| NVDA | {row_head}{row_mid}{row_tail} | {end_col} |\n"
    )
    assert len(oversized_table_md.encode("utf-8")) > FEISHU_CARD_MAX_BYTES

    row_cards = renderer.markdown_to_card_chunks(oversized_table_md, title="超长单行表")
    assert len(row_cards) >= 2
    combined = _all_card_markdown_text(row_cards)
    assert "（本行过长已截断）" not in combined
    assert "NVDA" in combined
    assert row_head in combined
    assert row_tail in combined
    assert end_col in combined
    for card in row_cards:
        card_text = _all_card_markdown_text([card])
        assert table_hdr in card_text and table_sep in card_text
        assert feishu_card_serialized_utf8_bytes(card) <= FEISHU_CARD_MAX_BYTES
        assert len(card["body"]["elements"]) <= FEISHU_CARD_MAX_ELEMENTS
        assert renderer._count_card_tables(card) <= FEISHU_CARD_TABLE_LIMIT
        assert is_feishu_card_within_envelope(card)

    # 3. When max_bytes cannot even fit header + one character of data, fail explicitly instead of dropping header
    tiny_table_md = (
        "# 极小上限测试\n\n"
        "## 明细\n\n"
        "| 标的代码与所属板块行业分类全称说明（字段一） | 详细技术形态突破信号与量价风控核验说明（字段二） | 止损价位目标价位与盈亏比区间测算说明（字段三） |\n"
        "| --- | --- | --- |\n"
        "| NVDA | 突破确认与放量验证 | 120.0 |\n"
    )
    with pytest.raises(renderer.RenderError) as exc_info:
        renderer.markdown_to_card_chunks(tiny_table_md, title="极小上限测试", max_bytes=500)
    assert exc_info.value.status == "table_unfit"

    md_path = tmp_path / "tiny_limit.md"
    md_path.write_text(tiny_table_md, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(md_path), "--title-from-first-line", "--max-bytes", "500"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "表头" in proc.stderr


def test_reference_images_and_backtick_paths_including_spaces_cleaned_by_cli(
    tmp_path: Path,
) -> None:
    md_text = "\n".join(
        [
            "# 晚班筛选 2026-11-26",
            "",
            "首段摘要保留，验证引号外正文不丢。",
            "",
            "## 信号摘要",
            "港股多头 3 只，A 股观望。保留链接 https://example.com/ta-screen/doc 与行内代码 `P/E`。",
            "![市场广度][breadth]",
            "[breadth]: push/2026-11-26_breadth.png",
            "图表输出位置：`/Users/everflow/workspace/push/2026-11-26.png`",
            "共享目录文件：`/mnt/shared reports/chart notes.csv`",
            "自定义目录文件：`/custom dir/sub folder/breadth chart.png`",
            "临时路径：`/tmp/ta_plot.svg`",
            "保留末句：所有止损按收盘价确认。",
        ]
    )
    md_path = tmp_path / "ref_and_backtick.md"
    md_path.write_text(md_text, encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(md_path), "--title-from-first-line"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    cards = json.loads(proc.stdout)
    assert isinstance(cards, list) and len(cards) == 1
    assert cards[0]["header"]["title"]["content"] == "晚班筛选 2026-11-26"

    serialized = json.dumps(cards, ensure_ascii=False)
    for forbidden in (
        "![市场广度][breadth]",
        "push/2026-11-26_breadth.png",
        "/Users/everflow/",
        "/mnt/shared reports/chart notes.csv",
        "/custom dir/sub folder/breadth chart.png",
        "/tmp/ta_plot.svg",
    ):
        assert forbidden not in serialized
    assert "首段摘要保留，验证引号外正文不丢。" in serialized
    assert "https://example.com/ta-screen/doc" in serialized
    assert "`P/E`" in serialized
    assert "保留末句：所有止损按收盘价确认。" in serialized
    assert "图表见 Dalton" in cards[-1]["body"]["elements"][-1]["content"]

    prepared, prep_err = prepare_automation_report_content(
        profile="market-alert-ta-screen",
        cards=cards,
        failure_text=None,
    )
    assert prep_err is None and prepared is not None
    assert prepared["content_kind"] == "cards"

    # Standalone line with ONLY a space-containing arbitrary-directory backtick path still appends 「图表见 Dalton」
    spaced_only_md = "# 测试\n\n## 正文\n\n主要结论保留。\n参考：`/mnt/shared reports/chart notes.csv`\n"
    spaced_path = tmp_path / "spaced_only.md"
    spaced_path.write_text(spaced_only_md, encoding="utf-8")
    proc_spaced = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(spaced_path), "--title-from-first-line"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_spaced.returncode == 0, proc_spaced.stderr
    spaced_cards = json.loads(proc_spaced.stdout)
    spaced_text = json.dumps(spaced_cards, ensure_ascii=False)
    assert "/mnt/shared reports/chart notes.csv" not in spaced_text
    assert "主要结论保留。" in spaced_text
    assert "图表见 Dalton" in spaced_cards[-1]["body"]["elements"][-1]["content"]


def test_md_to_report_cards_splits_six_tables_long_body_and_strips_local_images(
    tmp_path: Path,
) -> None:
    renderer = _load_renderer_module()
    long_para = "技术面广度与量价结构持续跟踪，关注突破有效性、止损保护与盈亏比分布。" * 65
    sections = [
        "# 美股盘后技术面多空筛选 2026-11-26",
        "> US Daily Technical Screen · 2026-11-26",
        "",
        "首段引言：美股整体处于震荡偏强区间，以下为分板块多空候选。",
        "",
        "## 一句话结论",
        "市场广度边际回升，多头前 3 集中于财报跳空与旗形突破。参考方法：https://example.com/ta-screen/method",
        "![市场广度图](push/2026-11-26_breadth.png)",
        "IMG:/tmp/ta_screen_breadth.png",
        "",
    ]
    for idx in range(1, 7):
        sections.append(f"## 候选清单第 {idx} 组")
        sections.append(_make_table(idx, rows=4))
        sections.append("")
        sections.append(long_para)
        sections.append("")
    sections.extend(
        [
            "## 附图说明",
            "![多头前3](./charts/us_long_top3.png)",
            "图表文件：/Users/everflow/workspace/screener/output/2026-11-26/regime.png",
            "补充路径：file:///tmp/extra_chart.jpg",
            "正文保留说明：所有候选止损位均按收盘确认。",
        ]
    )
    markdown = "\n".join(sections)
    assert len(markdown.encode("utf-8")) > FEISHU_CARD_MAX_BYTES

    md_path = tmp_path / "2026-11-26_market_alert_am.md"
    md_path.write_text(markdown, encoding="utf-8")

    # 1. Stdout JSON array mode with --title-from-first-line
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(md_path), "--title-from-first-line"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    cards = json.loads(proc.stdout)
    assert isinstance(cards, list)
    assert len(cards) >= 2

    all_serialized = json.dumps(cards, ensure_ascii=False)
    for forbidden in (
        "push/2026-11-26_breadth.png",
        "./charts/us_long_top3.png",
        "/tmp/ta_screen_breadth.png",
        "/Users/everflow/",
        "file:///tmp/",
        "IMG:",
        "__img__",
    ):
        assert forbidden not in all_serialized
    assert "首段引言：美股整体处于震荡偏强区间，以下为分板块多空候选。" in all_serialized
    assert "https://example.com/ta-screen/method" in all_serialized
    assert "正文保留说明：所有候选止损位均按收盘确认。" in all_serialized

    total_tables = 0
    for card in cards:
        assert card["schema"] == "2.0"
        assert isinstance(card["body"]["elements"], list) and card["body"]["elements"]
        assert feishu_card_serialized_utf8_bytes(card) <= FEISHU_CARD_MAX_BYTES
        assert len(card["body"]["elements"]) <= FEISHU_CARD_MAX_ELEMENTS
        card_tables = renderer._count_card_tables(card)
        assert card_tables <= FEISHU_CARD_TABLE_LIMIT
        total_tables += card_tables
        assert is_feishu_card_within_envelope(card)
    assert total_tables >= 6

    last_element = cards[-1]["body"]["elements"][-1]
    assert last_element["tag"] == "markdown"
    assert "图表见 Dalton" in last_element["content"]

    prepared, prep_err = prepare_automation_report_content(
        profile="market-alert-ta-screen",
        cards=cards,
        failure_text=None,
    )
    assert prep_err is None
    assert prepared is not None
    assert prepared["content_kind"] == "cards"
    assert prepared["expected_item_count"] == len(cards)

    # 2. --out file mode
    out_path = tmp_path / "cards.json"
    proc_out = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            str(md_path),
            "--title-from-first-line",
            "--out",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_out.returncode == 0, proc_out.stderr
    summary = json.loads(proc_out.stdout)
    assert summary["ok"] is True
    assert summary["card_count"] == len(cards)
    assert summary["had_local_images"] is True
    assert json.loads(out_path.read_text(encoding="utf-8")) == cards


def test_md_to_report_cards_six_tables_compact_body_splits_by_table_limit(tmp_path: Path) -> None:
    renderer = _load_renderer_module()
    parts = ["# 港股与A股盘后筛选 2026-11-26", ""]
    for idx in range(1, 7):
        parts.append(f"## 表格 {idx}")
        parts.append(_make_table(idx, rows=2))
        parts.append("")
    md_path = tmp_path / "six_tables.md"
    md_path.write_text("\n".join(parts), encoding="utf-8")

    mod = _load_script_module()
    cards, had_images = mod.convert_markdown_file(md_path)
    assert had_images is False
    assert len(cards) >= 2
    assert cards[0]["header"]["title"]["content"].startswith("港股与A股盘后筛选 2026-11-26")
    for card in cards:
        assert renderer._count_card_tables(card) <= FEISHU_CARD_TABLE_LIMIT
        assert feishu_card_serialized_utf8_bytes(card) <= FEISHU_CARD_MAX_BYTES
        assert "图表见 Dalton" not in json.dumps(card, ensure_ascii=False)
    assert sum(renderer._count_card_tables(c) for c in cards) == 6


@pytest.mark.parametrize(
    "bad_args",
    [
        ["--max-bytes", str(FEISHU_CARD_MAX_BYTES + 1)],
        ["--max-bytes", "0"],
        ["--max-elements", str(FEISHU_CARD_MAX_ELEMENTS + 1)],
        ["--max-elements", "0"],
        ["--max-tables", str(FEISHU_CARD_TABLE_LIMIT + 1)],
        ["--max-tables", "0"],
    ],
)
def test_max_limit_flags_cannot_exceed_feishu_hard_limits(
    tmp_path: Path,
    bad_args: list[str],
) -> None:
    md_path = tmp_path / "sample.md"
    md_path.write_text("# 标题\n\n## 结论\n\n简短正文。\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), str(md_path), "--title-from-first-line", *bad_args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 2
    assert "error:" in proc.stderr


def test_md_to_report_cards_reuses_shared_renderer_and_rechunks_for_charts_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = _load_script_module()
    renderer = mod.load_renderer_module()
    assert Path(renderer.__file__).resolve() == RENDERER_PATH.resolve()

    clean_body = "# 边界测试\n\n## 第一段\n\n" + ("数据验证段落。" * 45) + "\n\n## 第二段\n\n" + ("跟踪结论补充。" * 45)
    base_cards = renderer.markdown_to_card_chunks(clean_body, title="边界测试")
    assert len(base_cards) == 1
    base_bytes = feishu_card_serialized_utf8_bytes(base_cards[0])
    tight_limit = base_bytes + 10
    assert tight_limit < FEISHU_CARD_MAX_BYTES

    spy_chunks = create_autospec(
        renderer.markdown_to_card_chunks,
        side_effect=renderer.markdown_to_card_chunks,
    )
    monkeypatch.setattr(renderer, "markdown_to_card_chunks", spy_chunks)

    cards, had_images = mod.markdown_to_report_cards(
        clean_body + "\n\nIMG:push/boundary.png\n",
        title="边界测试",
        max_bytes=tight_limit,
    )
    assert had_images is True
    assert spy_chunks.call_count == 2
    assert len(cards) >= 2
    assert "图表见 Dalton" in cards[-1]["body"]["elements"][-1]["content"]
    for card in cards:
        assert feishu_card_serialized_utf8_bytes(card) <= tight_limit
        assert is_feishu_card_within_envelope(card, max_bytes=tight_limit)


def test_atomic_write_cleans_up_temp_file_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    mod = _load_script_module()
    target = tmp_path / "cards.json"

    failing_replace = create_autospec(os.replace, side_effect=KeyboardInterrupt)
    monkeypatch.setattr(mod.os, "replace", failing_replace)

    with pytest.raises(KeyboardInterrupt):
        mod._atomic_write_text(target, "[]\n")

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
