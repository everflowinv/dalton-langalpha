"""Unit tests for migrating OpenClaw weekly podcast tasks to Dalton (OC-1)."""

from __future__ import annotations

import importlib.util
import http.client
import json
import os
import re
import sys
from io import StringIO
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any
from unittest.mock import create_autospec

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from src.dalton.app.internal_llm_router import (
    get_allowed_host_oauth_models,
    internal_llm_router,
)
from src.dalton.app.router import router as dalton_api_router
from src.dalton.data_jobs.manifest import load_data_task_manifest
from src.dalton.data_jobs.runner_wrapper import resolve_task_payload_source
from src.dalton.database.automation_reports import REPORT_PROFILES
from src.dalton.llm.complete import DaltonCompleteResult, dalton_complete
from src.dalton.settings import get_dalton_config
from ptc_agent.agent.middleware.skills.registry import SKILL_REGISTRY
from ptc_agent.agent.middleware.skills.dalton_registry import (
    DALTON_SKILL_DEFINITIONS,
    _DALTON_SKILL_SPECS,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
OVERLAY_CLI_PATH = (
    REPO_ROOT
    / "scripts"
    / "dalton"
    / "openclaw_overlays"
    / "skills"
    / "podcast-weekly-digest"
    / "scripts"
    / "podcast_weekly_cli.py"
)
SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-podcast-weekly"
PUBLISH_CLI_PATH = SKILL_DIR / "scripts" / "podcast_publish.py"
JOB_424_PATH = REPO_ROOT / "tests" / "unit" / "dalton" / "fixtures" / "podcast_weekly_job_424e121f.json"

BANNED_PROSE = ("不代表", "并非", "而不是", "不能", "不构成", "不应")
BANNED_RECIPIENTS = (
    "discord",
    "feishu:",
    "feishu_send",
    ".openclaw",
    "chat_id",
    "1134818575211839498",
    "1333343845827739700",
)
_OC_ID_RE = re.compile(r"(?<![A-Za-z])oc_")


def _load_module(name: str, path: Path):
    cached = sys.modules.get(name)
    if cached is not None:
        return cached
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _run_cli(module, argv: list[str]) -> tuple[int, dict]:
    buf = StringIO()
    exit_code = 0
    with redirect_stdout(buf):
        try:
            rc = module.main(argv)
            if isinstance(rc, int):
                exit_code = rc
        except SystemExit as exc:
            exit_code = int(exc.code) if isinstance(exc.code, int) else 1
    return exit_code, json.loads(buf.getvalue())


def _fake_dalton_app() -> FastAPI:
    app = FastAPI()
    app.include_router(dalton_api_router)
    return app


def _make_endpoint_app() -> FastAPI:
    assert internal_llm_router in dalton_api_router.routes or any(
        getattr(route, "path", "").endswith("/internal/llm/complete")
        for route in dalton_api_router.routes
    )
    return _fake_dalton_app()


# ---------- 1. Overlay script tests ----------


def test_overlay_self_test_runs_without_openclaw_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_module("podcast_weekly_cli_under_test", OVERLAY_CLI_PATH)
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    ws = tmp_path / "data"
    ws.mkdir()
    monkeypatch.setenv("OPENCLAW_WORKSPACE", str(ws))
    cli.configure_workspace(ws)

    for key in (
        "GEMINI_API_KEY",
        "DALTON_BACKEND_URL",
        "INTERNAL_SERVICE_TOKEN",
        "SEED_ASR_APP_ID",
        "SEED_ASR_ACCESS_KEY",
        "VOLC_TOS_AK",
        "VOLC_TOS_SK",
        "VOLC_TOS_BUCKET",
    ):
        monkeypatch.setenv(key, f"test-{key.lower()}")
    monkeypatch.setenv("DALTON_BACKEND_URL", "http://127.0.0.1:8000")

    which_mock = create_autospec(
        cli.shutil.which, side_effect=lambda name: f"/usr/bin/{name}"
    )
    monkeypatch.setattr(cli.shutil, "which", which_mock)

    rc, payload = _run_cli(cli, ["--json", "self-test"])
    assert rc == 0
    assert payload["ok"] is True
    assert payload["workspace"] == str(ws.resolve())
    assert not (fake_home / ".openclaw").exists()
    check_names = {c["name"] for c in payload["checks"]}
    assert "antigravity-gateway" not in check_names
    assert {"dalton-backend-config", "workspace-path-guard", "merge-logic"} <= check_names


def test_overlay_reads_gemini_and_asr_keys_only_from_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_module("podcast_weekly_cli_under_test", OVERLAY_CLI_PATH)
    fake_home = tmp_path / "home"
    oc_dir = fake_home / ".openclaw"
    oc_dir.mkdir(parents=True)
    (oc_dir / ".env").write_text('GEMINI_API_KEY="stale-openclaw-key"\n', encoding="utf-8")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(cli.SkillError, match="GEMINI_API_KEY"):
        cli.gemini_key()

    monkeypatch.setenv("GEMINI_API_KEY", "env-gemini-secret")
    assert cli.gemini_key() == "env-gemini-secret"

    monkeypatch.delenv("SEED_ASR_APP_ID", raising=False)
    with pytest.raises(cli.SkillError, match="SEED_ASR_APP_ID"):
        cli._asr_env()


def test_overlay_workspace_path_prevents_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_module("podcast_weekly_cli_under_test", OVERLAY_CLI_PATH)
    ws = tmp_path / "data"
    ws.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = ws / "escape_link"
    os.symlink(outside, link)

    for bad in ("../outside.txt", "/etc/passwd", "escape_link", "a/../../outside.txt"):
        with pytest.raises(cli.SkillError):
            cli.workspace_path(ws, bad)
    with pytest.raises(cli.SkillError):
        cli.validate_video_id("../bad_id")


def test_overlay_translate_defaults_to_dalton_and_falls_back_per_chunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli = _load_module("podcast_weekly_cli_under_test", OVERLAY_CLI_PATH)
    ws = tmp_path / "data"
    ws.mkdir()
    cli.configure_workspace(ws)
    monkeypatch.setenv("DALTON_BACKEND_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "svc-tok")
    monkeypatch.setenv("GEMINI_API_KEY", "gem-key")

    # Verify CLI defaults match D1 specification.
    seen_args = {}

    def capture(args):
        seen_args["text_backend"] = args.text_backend
        seen_args["text_model"] = args.text_model
        seen_args["video_model"] = args.video_model

    monkeypatch.setattr(
        cli, "cmd_pipeline", create_autospec(cli.cmd_pipeline, side_effect=capture)
    )
    cli_old_sys_argv = sys.argv
    try:
        sys.argv = ["podcast_weekly_cli.py", "pipeline"]
        cli.main()
    finally:
        sys.argv = cli_old_sys_argv
    assert seen_args == {
        "text_backend": "dalton",
        "text_model": "gemini-4-hydrogen-high-oauth",
        "video_model": "gemini-4-argon",
    }

    vid = "vid12345"
    wd = cli.workspace_path(cli.WORK, vid)
    wd.mkdir(parents=True)
    # Build 2 chunks with chunk_words=5 so chunk 0 uses dalton and chunk 1 fails on dalton & falls back.
    long_text = "word " * 8
    cli.write_json(
        wd / "merged.json",
        {
            "video_id": vid,
            "blocks": [
                {"speaker": "Host", "start": 0.0, "text": long_text.strip(), "asr_speakers": ["1"]},
                {"speaker": "Guest", "start": 10.0, "text": long_text.strip(), "asr_speakers": ["2"]},
            ],
            "words_by_speaker": {"Host": 8, "Guest": 8},
        },
    )
    cli.write_json(wd / "turns.json", {"roster": ["Host", "Guest"], "windows": {}})
    meta = {
        "video_id": vid,
        "title": "Test Podcast Episode",
        "channel": "All-In",
        "url": "https://www.youtube.com/watch?v=vid12345",
        "upload_date": "20261108",
        "duration": 600,
        "description": "",
    }

    dalton_calls: list[dict] = []

    def fake_urlopen(req, timeout=0):
        url = req.full_url
        if url.endswith("/api/v1/dalton/internal/llm/complete"):
            body = json.loads(req.data.decode("utf-8"))
            dalton_calls.append(body)
            # Chunk 0 -> block #0 succeeds via dalton; chunk 1 -> block #1 gets budget_exhausted.
            if "#0 |" in body["user_prompt"]:
                resp_payload = {
                    "ok": True,
                    "model": body["model"],
                    "status": "done",
                    "text": json.dumps(
                        {
                            "blocks": [
                                {"n": 0, "speaker": "Host", "en": long_text.strip(), "zh": "测试段落零。"}
                            ],
                            "corrections": [],
                            "unclear": [],
                        },
                        ensure_ascii=False,
                    ),
                }
            else:
                resp_payload = {
                    "ok": False,
                    "model": body["model"],
                    "status": "budget_exhausted",
                    "error": "budget_exhausted: quota used up",
                    "text": None,
                }
            resp_mock = create_autospec(http.client.HTTPResponse, instance=True)
            resp_mock.status = 200
            resp_mock.read.return_value = json.dumps(resp_payload).encode("utf-8")
            resp_mock.__enter__.return_value = resp_mock
            resp_mock.__exit__.return_value = False
            return resp_mock
        raise AssertionError(f"Unexpected URL: {url}")

    gemini_stub = create_autospec(
        cli.gemini,
        return_value=(
            {
                "blocks": [{"n": 1, "speaker": "Guest", "en": long_text.strip(), "zh": "测试段落一。"}],
                "corrections": [],
                "unclear": [],
            },
            {"totalTokenCount": 42},
        ),
    )
    monkeypatch.setattr(
        cli.urllib.request,
        "urlopen",
        create_autospec(cli.urllib.request.urlopen, side_effect=fake_urlopen),
    )
    monkeypatch.setattr(cli, "gemini", gemini_stub)

    cli.stage_translate(
        vid,
        wd,
        meta,
        model="gemini-4-hydrogen-high-oauth",
        backend="dalton",
        chunk_words=5,
    )
    assert len(dalton_calls) == 2
    assert all(c["model"] == "gemini-4-hydrogen-high-oauth" for c in dalton_calls)
    assert gemini_stub.call_count == 1
    assert gemini_stub.call_args.args[0] == "gemini-4-argon"

    notes = cli.stage_assemble(vid, wd, meta)
    assert notes["translation_routes"] == {"dalton": 1, "gemini-api": 1}
    assert len(notes["translation_route_fallbacks"]) == 1
    assert "budget_exhausted" in notes["translation_route_fallbacks"][0]["reason"]


# ---------- 2. Internal LLM endpoint tests ----------


def test_internal_llm_complete_auth_whitelist_forward_and_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _make_endpoint_app()
    client = TestClient(app)

    valid_token = "top-secret-internal-service-token-123456"
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", valid_token)
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-user-01")
    get_dalton_config.cache_clear()

    valid_body = {
        "model": "gemini-4-hydrogen-high-oauth",
        "system_prompt": "Translate strictly.",
        "user_prompt": "Hello world",
        "response_schema": {
            "type": "OBJECT",
            "properties": {
                "blocks": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "properties": {"n": {"type": "INTEGER"}, "zh": {"type": "STRING"}},
                        "required": ["n", "zh"],
                    },
                }
            },
            "required": ["blocks"],
        },
        "purpose": "podcast_weekly_translate",
        "timeout_s": 120.0,
    }

    # Missing or wrong token -> 401
    assert (
        client.post("/api/v1/dalton/internal/llm/complete", json=valid_body).status_code
        in (401, 403)
    )
    assert (
        client.post(
            "/api/v1/dalton/internal/llm/complete",
            json=valid_body,
            headers={"X-Service-Token": "wrong"},
        ).status_code
        in (401, 403)
    )

    # Disallowed model -> 400
    bad_model_body = {**valid_body, "model": "gemini-4-argon"}
    resp_400 = client.post(
        "/api/v1/dalton/internal/llm/complete",
        json=bad_model_body,
        headers={"X-Service-Token": valid_token},
    )
    assert resp_400.status_code == 400

    assert get_allowed_host_oauth_models() == frozenset(
        {
            "gemini-4-hydrogen-high-oauth",
            "gemini-4-argon-medium-oauth",
            "gpt-6.1-sol-oauth",
        }
    )

    # Normal call forwards args to dalton_complete (create_autospec)
    mock_complete = create_autospec(
        dalton_complete,
        return_value=DaltonCompleteResult(
            ok=True,
            status="done",
            text='{"blocks":[{"n":0,"zh":"你好"}]}',
            model="gemini-4-hydrogen-high-oauth",
        ),
    )
    monkeypatch.setattr(
        "src.dalton.app.internal_llm_router.dalton_complete", mock_complete
    )
    resp_ok = client.post(
        "/api/v1/dalton/internal/llm/complete",
        json=valid_body,
        headers={"X-Service-Token": valid_token},
    )
    assert resp_ok.status_code == 200
    assert resp_ok.json() == {
        "ok": True,
        "text": '{"blocks":[{"n":0,"zh":"你好"}]}',
        "model": "gemini-4-hydrogen-high-oauth",
        "status": "done",
        "error": None,
    }
    kw = mock_complete.call_args.kwargs
    assert kw["user_id"] == "pm-user-01"
    assert kw["slot"] == "background"
    assert kw["model"] == "gemini-4-hydrogen-high-oauth"
    assert kw["system_prompt"] == "Translate strictly."
    assert kw["user_prompt"] == "Hello world"
    assert kw["purpose"] == "podcast_weekly_translate"
    assert kw["timeout_s"] == 120.0
    assert isinstance(kw["response_schema"], type) and issubclass(
        kw["response_schema"], BaseModel
    )

    # Failure in dalton_complete returns ok: false with error category
    mock_fail = create_autospec(
        dalton_complete,
        return_value=DaltonCompleteResult(
            ok=False,
            status="budget_exhausted",
            error_kind="budget_exhausted",
            error="monthly quota reached",
            model="gpt-6.1-sol-oauth",
        ),
    )
    monkeypatch.setattr("src.dalton.app.internal_llm_router.dalton_complete", mock_fail)
    resp_fail = client.post(
        "/api/v1/dalton/internal/llm/complete",
        json={**valid_body, "model": "gpt-6.1-sol-oauth", "response_schema": None},
        headers={"X-Service-Token": valid_token},
    )
    assert resp_fail.status_code == 200
    data_fail = resp_fail.json()
    assert data_fail["ok"] is False
    assert data_fail["status"] == "budget_exhausted"
    assert "budget_exhausted" in data_fail["error"]


# ---------- 3. Publish helper script tests ----------


def _seed_ready_episode(ws: Path, vid: str = "ep001", date: str = "20261110") -> None:
    stem = f"{date}-{vid}"
    out_dir = ws / "research-outputs" / "podcast-weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    zh_path = out_dir / f"{stem}.zh.md"
    en_path = out_dir / f"{stem}.en.md"
    summary_path = out_dir / f"{stem}.summary.md"
    notes_path = out_dir / f"{stem}.notes.json"

    zh_path.write_text("**Chamath** [0:10]\n人工智能资本开支达到 100 亿美元。\n", encoding="utf-8")
    en_path.write_text("**Chamath** [0:10]\nAI capex reached $10B.\n", encoding="utf-8")
    summary_path.write_text(
        "# 算力投资进入新阶段\n\n本期核心判断：资本开支加速。\n\n**投资看点**\n- 算力基建扩张 [0:10]\n",
        encoding="utf-8",
    )
    notes_path.write_text(
        json.dumps(
            {
                "video_id": vid,
                "title": "AI Capex Cycle",
                "channel": "All-In",
                "url": f"https://www.youtube.com/watch?v={vid}",
                "upload_date": date,
                "duration_min": 65,
                "zh_path": f"/Users/host/var/dalton/openclaw-data/research-outputs/podcast-weekly/{stem}.zh.md",
                "en_path": f"/Users/host/var/dalton/openclaw-data/research-outputs/podcast-weekly/{stem}.en.md",
                "corrections": ["100 亿美元：已核对"],
                "speaker_flags": [],
                "unclear": [],
                "amount_warnings": [],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    ledger_path = ws / "state" / "podcast-weekly" / "episodes.json"
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(
        json.dumps(
            {
                vid: {
                    "title": "AI Capex Cycle",
                    "channel": "All-In",
                    "upload_date": date,
                    "stage": "ready",
                    "notes_path": str(notes_path),
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_publish_helper_assembles_4_sections_and_guards_state(tmp_path: Path) -> None:
    pub = _load_module("podcast_publish_under_test", PUBLISH_CLI_PATH)
    ws = tmp_path / "ws"
    ws.mkdir()

    # Missing episodes.json MUST fail cleanly (never report empty_week)
    rc_missing, status_missing = _run_cli(
        pub, ["status", "--workspace-dir", str(ws), "--date", "2026-11-15", "--json"]
    )
    assert rc_missing == 1
    assert status_missing["ok"] is False
    assert status_missing["reason"] == "ledger_missing"
    assert "empty_week" not in status_missing

    # Empty {} ledger -> emits exact empty_week line
    (ws / "state" / "podcast-weekly").mkdir(parents=True, exist_ok=True)
    (ws / "state" / "podcast-weekly" / "episodes.json").write_text("{}", encoding="utf-8")
    rc_empty, status_empty = _run_cli(
        pub, ["status", "--workspace-dir", str(ws), "--date", "2026-11-15", "--json"]
    )
    assert rc_empty == 0
    assert status_empty["empty_week"] is True
    assert status_empty["empty_message"] == "播客周报｜本周 All-In 和 a16z 没有新节目"
    assert status_empty["report_run_id"] == "podcast-weekly-2026-11-15-empty"

    # Ready episode -> assembles all 4 sections
    _seed_ready_episode(ws)
    rc_prep, prep = _run_cli(
        pub, ["prepare-doc", "--workspace-dir", str(ws), "--video", "ep001", "--json"]
    )
    assert rc_prep == 0
    assert prep["create"] is True
    assert prep["title"] == "【All-In】算力投资进入新阶段（2026-11-10）"
    assert prep["folder_token"] == "TIwFfn5UAlfMrpdby0bcVDb5nwg"
    for section in ("## 内容提要", "## 中文全文", "## 校正说明", "## 英文校订稿"):
        assert section in prep["markdown"]

    # Record success -> duplicate prepare-doc does not recreate
    ret_file = ws / "temp" / "podcast-weekly" / "ep001.ret.json"
    ret_file.parent.mkdir(parents=True, exist_ok=True)
    ret_file.write_text(
        json.dumps({"ok": True, "document_id": "dox123", "url": "https://feishu.cn/docx/dox123"}),
        encoding="utf-8",
    )
    rc_save, saved = _run_cli(
        pub,
        [
            "save-doc",
            "--workspace-dir",
            str(ws),
            "--video",
            "ep001",
            "--feishu-return",
            str(ret_file.relative_to(ws)),
            "--zh-title",
            "算力投资进入新阶段",
            "--json",
        ],
    )
    assert rc_save == 0
    assert saved["doc_url"] == "https://feishu.cn/docx/dox123"

    rc_dup, dup = _run_cli(
        pub, ["prepare-doc", "--workspace-dir", str(ws), "--video", "ep001", "--json"]
    )
    assert rc_dup == 0
    assert dup["create"] is False
    assert dup["reason"] == "already_exists"
    assert dup["doc_url"] == "https://feishu.cn/docx/dox123"


def test_publish_helper_stops_after_two_failures_and_never_overwrites_corrupt(
    tmp_path: Path,
) -> None:
    pub = _load_module("podcast_publish_under_test", PUBLISH_CLI_PATH)
    ws = tmp_path / "ws"
    ws.mkdir()
    _seed_ready_episode(ws, vid="ep002")

    # Fail twice -> halted
    rc1, fail1 = _run_cli(
        pub, ["record-failure", "--workspace-dir", str(ws), "--video", "ep002", "--error", "err1"]
    )
    assert rc1 == 0 and fail1["attempts"] == 1 and fail1["stopped"] is False
    rc2, fail2 = _run_cli(
        pub, ["record-failure", "--workspace-dir", str(ws), "--video", "ep002", "--error", "err2"]
    )
    assert rc2 == 0 and fail2["attempts"] == 2 and fail2["stopped"] is True

    rc_blocked, blocked = _run_cli(
        pub, ["prepare-doc", "--workspace-dir", str(ws), "--video", "ep002", "--json"]
    )
    assert rc_blocked == 1
    assert blocked["create"] is False
    assert blocked["reason"] == "doc_create_failed_twice"

    # Corrupt doc.json must never be overwritten
    ws2 = tmp_path / "ws_corrupt"
    ws2.mkdir()
    _seed_ready_episode(ws2, vid="ep003")
    doc_rec = ws2 / "state" / "podcast-weekly" / "docs" / "ep003" / "doc.json"
    doc_rec.parent.mkdir(parents=True, exist_ok=True)
    doc_rec.write_text("{corrupt json", encoding="utf-8")

    rc_c, out_c = _run_cli(
        pub, ["prepare-doc", "--workspace-dir", str(ws2), "--video", "ep003", "--json"]
    )
    assert rc_c == 1
    assert out_c["create"] is False
    assert out_c["reason"] == "doc_record_unreadable"
    assert doc_rec.read_text(encoding="utf-8") == "{corrupt json"


# ---------- 4. Skill contract & registry tests ----------


def test_dalton_podcast_weekly_skill_contract_and_safety() -> None:
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", skill_md, re.DOTALL)
    assert m is not None
    fm = yaml.safe_load(m.group(1))
    assert fm["name"] == "dalton-podcast-weekly"
    assert len(fm["description"]) <= 120
    assert "触发词" not in fm["description"]

    assert ("dalton-podcast-weekly", None, ()) in _DALTON_SKILL_SPECS
    assert len(_DALTON_SKILL_SPECS) == 47
    assert "dalton-podcast-weekly" in DALTON_SKILL_DEFINITIONS
    assert "dalton-podcast-weekly" in SKILL_REGISTRY
    assert "podcast-weekly" in REPORT_PROFILES

    # Zero recipient IDs anywhere in skill tree
    text_suffixes = {".py", ".md", ".yaml", ".yml", ".json", ".txt"}
    for file_path in SKILL_DIR.rglob("*"):
        if not file_path.is_file() or file_path.suffix not in text_suffixes:
            continue
        text = file_path.read_text(encoding="utf-8")
        low = text.lower()
        for banned in BANNED_RECIPIENTS:
            assert banned not in low, f"Found {banned!r} in {file_path}"
        assert not _OC_ID_RE.search(text), f"Found oc_ chat ID in {file_path}"

    # Verbatim 撰写硬规则 preserved from job_424e121f.json, and banned prose absent outside it
    job_payload = json.loads(JOB_424_PATH.read_text(encoding="utf-8"))["payload"]["message"]
    expected_rules = job_payload.split("【撰写硬规则：严禁防御性否定表达】", 1)[1].strip()
    body, sep, rules_sec = skill_md.partition("## 撰写硬规则")
    assert sep == "## 撰写硬规则"
    assert expected_rules in rules_sec
    for phrase in BANNED_PROSE:
        assert phrase not in body, f"Found banned phrase {phrase!r} outside 撰写硬规则"

    assert "general-purpose" in body
    assert "同时最多运行 2 个" in body
    assert 'action="stage"' in body
    assert 'action="collect"' in body
    assert 'action="publish_doc"' in body
    assert "feishu_read" in body
    for legacy in (
        'action="read_artifact"',
        'action="write_artifact"',
        'action="prepare_doc"',
        "feishu_create_doc",
        "feishu_append",
        'action="record_chunk"',
        'action="record_failure"',
        'action="save_doc"',
    ):
        assert legacy not in body
    assert "automation_report" in body
    assert "播客周报｜本周 All-In 和 a16z 没有新节目" in body
    for token in (
        'next_action == "resubmit_pending"',
        'next_action == "empty_week"',
        'next_action == "nothing_to_report"',
        'next_action == "publish"',
        'next_action == "submit"',
        "report_success",
        "report_failed",
        "save_pending_report",
        "abandon_pending_report",
        "delivery_confirmed",
        "mark_reported",
        "content_conflict",
        "execution_batch_conflict",
        "pending_report_unreadable",
        "chunks_record_unreadable",
        "结束本次运行",
        "已补发上次的周报，本周新内容在下次运行时提交",
    ):
        assert token in body
    assert "失败段：本段只写入刷新后 `status` 返回的 `report_failed` 期次" in body
    assert "失败或跳过段" not in body
    assert "批次" not in body
    assert "pending_summary_batch" not in body
    # Abandoning is only safe once the old delivery is finished: a resubmit that first saved the
    # report (created == true) is still being delivered and must be kept, or two reports go out.
    resubmit_line = next(line for line in body.splitlines() if 'next_action == "resubmit_pending"' in line)
    assert "created == true" in resubmit_line
    assert resubmit_line.index("created == false") < resubmit_line.index("abandon_pending_report")


# ---------- 5. Manifest & payload source tests ----------


def test_manifest_podcast_weekly_pipeline_entry() -> None:
    manifest = load_data_task_manifest()
    entry = manifest.by_job_key().get("podcast-weekly-pipeline")
    assert entry is not None
    assert entry.task_code == "T60"
    assert entry.runner == "host"
    assert entry.host_mode == "run"
    assert entry.cwd == "var/dalton/openclaw-data"
    assert entry.cron == "0 6 * * 0"
    assert entry.timezone == "Asia/Hong_Kong"
    assert entry.timeout_seconds == 10800
    assert entry.enabled is False
    assert entry.notify_enabled is False
    assert entry.cos_mode == "skip"
    assert entry.command_argv == [
        "bash",
        "skills/podcast-weekly-digest/scripts/run.sh",
        "--json",
        "pipeline",
        "--since-days",
        "7",
        "--video-model",
        "gemini-4-argon",
        "--text-backend",
        "dalton",
        "--text-model",
        "gemini-4-hydrogen-high-oauth",
    ]
    assert set(entry.secret_names) == {
        "GEMINI_API_KEY",
        "INTERNAL_SERVICE_TOKEN",
        "SEED_ASR_APP_ID",
        "SEED_ASR_ACCESS_KEY",
        "VOLC_TOS_AK",
        "VOLC_TOS_SK",
        "VOLC_TOS_BUCKET",
    }
    assert "DALTON_BACKEND_URL" not in entry.secret_names
    assert entry.env == {"DALTON_BACKEND_URL": "http://127.0.0.1:8000"}
    assert resolve_task_payload_source("podcast-weekly-pipeline") == "none"
    assert resolve_task_payload_source("T60") == "none"


# ---------- 6. Round 4 review hardening tests ----------


def test_asr_call_uses_python_http_without_credentials_in_argv_and_sanitizes_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import io
    import subprocess
    import urllib.request

    cli = _load_module("podcast_weekly_cli_r4", OVERLAY_CLI_PATH)
    cli.configure_workspace(tmp_path)

    captured_headers: dict[str, str] = {}

    class _FakeResp(io.BytesIO):
        def __init__(self, body: bytes, headers: dict[str, str]) -> None:
            super().__init__(body)
            self.headers = headers

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def _fake_urlopen(url, data=None, timeout=None):
        _ = (data, timeout)
        captured_headers.update(dict(url.header_items()))
        return _FakeResp(
            b'{"result":{"utterances":[]}}',
            {"X-Api-Status-Code": "20000000", "X-Api-Message": "ok"},
        )

    def _forbid_subprocess(*_args, **_kwargs):
        raise AssertionError("_asr_call must not spawn curl subprocess")

    spy_urlopen = create_autospec(urllib.request.urlopen, spec_set=True, side_effect=_fake_urlopen)
    spy_subproc = create_autospec(subprocess.run, spec_set=True, side_effect=_forbid_subprocess)
    monkeypatch.setattr(urllib.request, "urlopen", spy_urlopen)
    monkeypatch.setattr(subprocess, "run", spy_subproc)

    code, msg, text = cli._asr_call(
        "https://openspeech.bytedance.com/api/v3/auc/bigmodel/submit",
        {"SEED_ASR_APP_ID": "asr-app-secret-01", "SEED_ASR_ACCESS_KEY": "asr-acc-secret-02"},
        "task-123",
        {"audio": {"url": "https://tos.example/a.mp3"}},
    )
    assert code == "20000000" and msg == "ok" and "utterances" in text
    assert captured_headers.get("X-api-app-key") == "asr-app-secret-01"
    assert captured_headers.get("X-api-access-key") == "asr-acc-secret-02"

    # Exception sanitization: TimeoutExpired.cmd & env secrets MUST NEVER appear in episodes.json
    monkeypatch.setenv("SEED_ASR_ACCESS_KEY", "super-secret-asr-999")

    def _timeout_stage(_vid, _wd, _meta):
        raise subprocess.TimeoutExpired(
            cmd=["curl", "-H", "X-Api-Access-Key: super-secret-asr-999", "https://api.example/x?token=tok123"],
            timeout=120,
        )

    def _fetch_ok(_vid, wd):
        wd.mkdir(parents=True, exist_ok=True)
        return {"video_id": "ep_sec_1", "title": "Test", "channel": "All-In", "upload_date": "20261110"}

    spy_fetch = create_autospec(cli.stage_fetch, spec_set=True, side_effect=_fetch_ok)
    spy_asr = create_autospec(cli.stage_asr_submit, spec_set=True, side_effect=_timeout_stage)
    monkeypatch.setattr(cli, "stage_fetch", spy_fetch)
    monkeypatch.setattr(cli, "stage_asr_submit", spy_asr)
    args = type("Args", (), {"video_model": "m1", "text_model": "m2", "text_backend": "dalton"})()
    res = cli.process_episode({"video_id": "ep_sec_1", "hosts": []}, args)
    assert res["ok"] is False
    ledger = json.loads(cli.LEDGER.read_text(encoding="utf-8"))
    err_saved = ledger["ep_sec_1"]["stage_error"]
    assert "super-secret-asr-999" not in err_saved
    assert "curl" not in err_saved
    assert "TimeoutExpired" in err_saved


def test_stage_assemble_emits_merged_md_in_research_outputs(tmp_path: Path) -> None:
    cli = _load_module("podcast_weekly_cli_r4", OVERLAY_CLI_PATH)
    cli.configure_workspace(tmp_path)

    wd = cli.WORK / "epm01"
    wd.mkdir(parents=True, exist_ok=True)
    meta = {
        "video_id": "epm01",
        "title": "Episode Merged Test",
        "channel": "All-In",
        "url": "https://youtu.be/epm01",
        "upload_date": "20261110",
        "duration": 600,
    }
    (wd / "merged.json").write_text(
        json.dumps({"video_id": "epm01", "blocks": [{"speaker": "Jason", "start": 0, "text": "hello world"}]}),
        encoding="utf-8",
    )
    (wd / "merged.md").write_text("# Episode Merged Test\n\n**Jason** [00:00]\nhello world\n", encoding="utf-8")
    (wd / "zh.json").write_text(
        json.dumps(
            {
                "blocks": [{"n": 0, "speaker": "Jason", "en": "hello world", "zh": "你好世界"}],
                "corrections": [],
                "unclear": [],
            }
        ),
        encoding="utf-8",
    )
    notes = cli.stage_assemble("epm01", wd, meta)
    merged_out = cli.OUT / "20261110-epm01.merged.md"
    assert merged_out.is_file()
    assert notes["merged_path"] == str(merged_out)
    assert "hello world" in merged_out.read_text(encoding="utf-8")


def test_run_sh_uses_skill_venv_even_when_dalton_python_bin_set(tmp_path: Path) -> None:
    import shutil
    import subprocess

    src_run_sh = OVERLAY_CLI_PATH.parent / "run.sh"
    skill_dir = tmp_path / "skills" / "podcast-weekly-digest"
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(parents=True)
    shutil.copy2(src_run_sh, scripts_dir / "run.sh")
    (scripts_dir / "podcast_weekly_cli.py").write_text(
        "import json, os\nprint(json.dumps({'used_skill_venv': os.environ.get('USED_SKILL_VENV')}))\n",
        encoding="utf-8",
    )
    venv_py = skill_dir / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_text(
        f'#!/bin/sh\nexport USED_SKILL_VENV=1\nexec "{sys.executable}" "$@"\n',
        encoding="utf-8",
    )
    venv_py.chmod(0o755)

    fake_bin = tmp_path / "fake_dalton_py"
    fake_bin.write_text("#!/bin/sh\necho MUST_NOT_BYPASS_VENV >&2\nexit 99\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    env = os.environ.copy()
    env["DALTON_PYTHON_BIN"] = str(fake_bin)
    res = subprocess.run(
        ["bash", str(scripts_dir / "run.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "MUST_NOT_BYPASS_VENV" not in res.stderr
    assert json.loads(res.stdout)["used_skill_venv"] == "1"


@pytest.mark.asyncio
async def test_backend_podcast_weekly_tools_auth_path_guard_and_resumable_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton import podcast_weekly_store as store
    from src.dalton.tools import build_dalton_tools
    from src.dalton.tools import podcast_weekly_tools as pwt

    assert "podcast_weekly_read" not in {t.name for t in build_dalton_tools("analyst", user_tier="external")}
    assert "podcast_weekly_write" not in {t.name for t in build_dalton_tools("chief_of_staff", user_tier="external")}
    for role in ("analyst", "chief_of_staff"):
        names = {t.name for t in build_dalton_tools(role, user_tier="internal")}
        assert {"podcast_weekly_read", "podcast_weekly_write"}.issubset(names)

    var_dir = tmp_path / "var_dalton"
    data_root = var_dir / "openclaw-data"
    data_root.mkdir(parents=True)
    monkeypatch.setenv("DALTON_BACKEND_VAR_DIR", str(var_dir))

    async def _is_internal(uid: str) -> bool:
        return uid == "usr_internal"

    fake_auth = create_autospec(_is_internal, instance=False, spec_set=True)
    fake_auth.side_effect = _is_internal
    monkeypatch.setattr(pwt, "is_internal_or_admin", fake_auth)

    read_tool, write_tool = pwt.create_podcast_weekly_tools()
    ext_cfg = {"configurable": {"user_id": "usr_external"}}
    int_cfg = {"configurable": {"user_id": "usr_internal"}}

    denied = json.loads(await read_tool.ainvoke({"action": "status"}, config=ext_cfg))
    assert denied["ok"] is False and denied["error"] == "not_allowed"

    # Missing ledger -> ok: False (never empty_week)
    missing = json.loads(await read_tool.ainvoke({"action": "status"}, config=int_cfg))
    assert missing["ok"] is False and missing["reason"] == "ledger_missing"

    # Seed ready episode on host (give ep_r4_1 >25k total with head<=25k and tail<=25k so prepare_doc splits cleanly into 2 persisted parts)
    _seed_ready_episode(data_root, vid="ep_r4_1")
    (data_root / "research-outputs" / "podcast-weekly" / "20261110-ep_r4_1.zh.md").write_text(
        "\n\n".join(
            f"**Chamath** [0:{i:02d}]\n" + ("人工智能资本开支深入分析段落内容，用于验证双段切片。" * 18)
            for i in range(1, 36)
        ),
        encoding="utf-8",
    )
    (data_root / "research-outputs" / "podcast-weekly" / "20261110-ep_r4_1.en.md").write_text(
        "\n\n".join(
            f"**Chamath** [0:{i:02d}]\nAI capex expanded steadily across hyperscalers with durable compute demand."
            for i in range(1, 105)
        ),
        encoding="utf-8",
    )
    (data_root / "research-outputs" / "podcast-weekly" / "20261110-ep_r4_1.merged.md").write_text(
        "**Jason** [00:00]\nMerged body.\n", encoding="utf-8"
    )
    (data_root / "secret.txt").write_text("do-not-leak", encoding="utf-8")

    # Path / video_id traversal blocked
    bad_vid = json.loads(
        await read_tool.ainvoke(
            {"action": "read_artifact", "video_id": "../secret", "artifact": "zh"},
            config=int_cfg,
        )
    )
    assert bad_vid["ok"] is False
    bad_art = json.loads(
        await read_tool.ainvoke(
            {"action": "read_artifact", "video_id": "ep_r4_1", "artifact": "../secret.txt"},
            config=int_cfg,
        )
    )
    assert bad_art["ok"] is False

    # Symlink rejection inside research-outputs
    sym_path = data_root / "research-outputs" / "podcast-weekly" / "20261110-epsym1.zh.md"
    sym_path.symlink_to(data_root / "secret.txt")
    bad_sym = json.loads(
        await read_tool.ainvoke(
            {"action": "read_artifact", "video_id": "epsym1", "artifact": "zh"},
            config=int_cfg,
        )
    )
    assert bad_sym["ok"] is False

    # Read merged & write updated summary back via backend tools
    got_merged = json.loads(
        await read_tool.ainvoke(
            {"action": "read_artifact", "video_id": "ep_r4_1", "artifact": "merged", "max_chars": 100},
            config=int_cfg,
        )
    )
    assert got_merged["ok"] is True and "Merged body." in got_merged["content"]

    wrote_sum = json.loads(
        await write_tool.ainvoke(
            {
                "action": "write_artifact",
                "video_id": "ep_r4_1",
                "artifact": "summary",
                "content": "# 算力拐点\n\n核心结论。\n\n**投资看点**\n- 资本开支维持高增 [01:00]",
            },
            config=int_cfg,
        )
    )
    assert wrote_sum["ok"] is True

    # Status classifies unpublished ready episode into to_publish with next_action="publish"
    st1 = json.loads(
        await read_tool.ainvoke({"action": "status", "date": "2026-11-15"}, config=int_cfg)
    )
    assert st1["ok"] is True and st1["next_action"] == "publish" and st1["empty_week"] is False
    assert [ep["video_id"] for ep in st1["ready"]] == ["ep_r4_1"]
    assert [ep["video_id"] for ep in st1["to_publish"]] == ["ep_r4_1"]

    # Simulate feishu_create_doc partial failure (content_insert_failed returning document_id + url)
    saved_partial = json.loads(
        await write_tool.ainvoke(
            {
                "action": "save_doc",
                "video_id": "ep_r4_1",
                "document_id": "dox_partial_1",
                "doc_url": "https://feishu.cn/docx/dox_partial_1",
                "zh_title": "算力拐点",
                "part": 0,
                "total_parts": 2,
                "body_complete": False,
            },
            config=int_cfg,
        )
    )
    assert saved_partial["ok"] is True and saved_partial["body_status"] == "incomplete"

    # Rerun prepare_doc MUST resume existing document_id (never re-create)
    resumed = json.loads(
        await read_tool.ainvoke({"action": "prepare_doc", "video_id": "ep_r4_1"}, config=int_cfg)
    )
    assert resumed["create"] is False
    assert resumed["resume_append"] is True
    assert resumed["document_id"] == "dox_partial_1"

    # Complete chunk 1 & 2 -> promotes body_status to complete & writes published_at
    c1 = json.loads(
        await write_tool.ainvoke(
            {
                "action": "record_chunk",
                "video_id": "ep_r4_1",
                "document_id": "dox_partial_1",
                "doc_url": "https://feishu.cn/docx/dox_partial_1",
                "part": 1,
                "total_parts": 2,
            },
            config=int_cfg,
        )
    )
    assert c1["body_status"] == "incomplete"
    c2 = json.loads(
        await write_tool.ainvoke(
            {
                "action": "record_chunk",
                "video_id": "ep_r4_1",
                "document_id": "dox_partial_1",
                "doc_url": "https://feishu.cn/docx/dox_partial_1",
                "part": 2,
                "total_parts": 2,
            },
            config=int_cfg,
        )
    )
    assert c2["body_status"] == "complete"

    # Before mark_reported is called, status returns next_action="submit" with report_success and hash run_id
    st_pending_sum = json.loads(
        await read_tool.ainvoke({"action": "status", "date": "2026-11-15"}, config=int_cfg)
    )
    assert st_pending_sum["next_action"] == "submit"
    assert st_pending_sum["empty_week"] is False
    assert re.fullmatch(r"podcast-weekly-2026-11-15-[0-9a-f]{10}", st_pending_sum["report_run_id"])
    assert [ep["video_id"] for ep in st_pending_sum["report_success"]] == ["ep_r4_1"]
    assert st_pending_sum["report_success"][0]["doc_url"] == "https://feishu.cn/docx/dox_partial_1"
    assert st_pending_sum["report_success"][0]["recovered"] is False

    # After mark_reported -> same-day rerun returns nothing_to_report (already reported today)
    marked = json.loads(
        await write_tool.ainvoke(
            {
                "action": "mark_reported",
                "report_run_id": st_pending_sum["report_run_id"],
                "success_video_ids": ["ep_r4_1"],
                "failed_video_ids": [],
            },
            config=int_cfg,
        )
    )
    assert marked["ok"] is True and marked["recorded_success"] == ["ep_r4_1"]
    st_done = store.get_status(data_root, date_str="2026-11-15")
    assert st_done["next_action"] == "nothing_to_report" and st_done["empty_week"] is False

    # Next week with zero new episodes -> empty_week, and marking empty_date turns rerun into nothing_to_report
    st_next = store.get_status(data_root, date_str="2026-11-22")
    assert st_next["next_action"] == "empty_week" and st_next["empty_week"] is True
    assert st_next["report_run_id"] == "podcast-weekly-2026-11-22-empty"
    marked_empty = json.loads(
        await write_tool.ainvoke(
            {
                "action": "mark_reported",
                "report_run_id": st_next["report_run_id"],
                "empty_date": "2026-11-22",
            },
            config=int_cfg,
        )
    )
    assert marked_empty["ok"] is True and marked_empty["empty_date"] == "2026-11-22"
    st_next_after = store.get_status(data_root, date_str="2026-11-22")
    assert st_next_after["next_action"] == "nothing_to_report" and st_next_after["empty_week"] is False


def test_skill_md_podcast_publish_commands_parse_with_build_parser() -> None:
    import shlex

    pub = _load_module("podcast_publish_under_test", PUBLISH_CLI_PATH)
    parser = pub.build_parser()
    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    # Join line continuations and extract every command invocation of podcast_publish.py
    normalized = skill_md.replace("\\\n", " ")
    cmds = re.findall(r"podcast_publish\.py\s+([^\n`]+)", normalized)
    assert cmds, "Expected at least one podcast_publish.py CLI command in SKILL.md"
    for cmd_tail in cmds:
        tokens = shlex.split(cmd_tail.strip())
        parsed = parser.parse_args(tokens)
        assert parsed.command in {
            "status",
            "prepare-doc",
            "save-doc",
            "save-chunk",
            "record-failure",
            "save-pending-report",
            "abandon-pending-report",
            "mark-reported",
        }


def test_cross_process_flock_preserves_interleaved_state_updates(tmp_path: Path) -> None:
    """Two concurrent OS processes updating ledger/doc under shared episodes.lock lose zero updates."""
    import subprocess
    import sys

    data_root = tmp_path / "openclaw-data"
    state_dir = data_root / "state" / "podcast-weekly"
    state_dir.mkdir(parents=True)
    (state_dir / "episodes.json").write_text(
        json.dumps(
            {
                "ep_lock_a": {"stage": "ready", "title": "A"},
                "ep_lock_b": {"stage": "ready", "title": "B"},
            }
        ),
        encoding="utf-8",
    )

    worker = tmp_path / "concurrent_worker.py"
    worker.write_text(
        (
            "import importlib.util, pathlib, sys, time\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "from src.dalton import podcast_weekly_store as store\n"
            f"spec = importlib.util.spec_from_file_location('pwc', {str(OVERLAY_CLI_PATH)!r})\n"
            "pwc = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(pwc)\n"
            "root = pathlib.Path(sys.argv[1])\n"
            "mode = sys.argv[2]\n"
            "vid = sys.argv[3]\n"
            "pwc.LEDGER = store.ledger_path(root)\n"
            "for i in range(10):\n"
            "    if mode == 'cli':\n"
            "        pwc.ledger_update(vid, worker_tag=f'{vid}-{i}')\n"
            "    else:\n"
            "        store.save_doc(\n"
            "            root,\n"
            "            vid,\n"
            "            doc_url=f'https://feishu.cn/docx/{vid}',\n"
            "            document_id=f'doc_{vid}',\n"
            "            zh_title=f'{vid}-{i}',\n"
            "            total_parts=1,\n"
            "            body_complete=True,\n"
            "        )\n"
            "    time.sleep(0.01)\n"
        ),
        encoding="utf-8",
    )

    p1 = subprocess.Popen([sys.executable, str(worker), str(data_root), "cli", "ep_lock_a"])
    p2 = subprocess.Popen([sys.executable, str(worker), str(data_root), "store", "ep_lock_b"])
    assert p1.wait(timeout=15) == 0
    assert p2.wait(timeout=15) == 0

    final_ledger = json.loads((state_dir / "episodes.json").read_text(encoding="utf-8"))
    assert final_ledger["ep_lock_a"]["worker_tag"] == "ep_lock_a-9"
    assert final_ledger["ep_lock_b"]["doc_url"] == "https://feishu.cn/docx/ep_lock_b"
    assert final_ledger["ep_lock_b"]["published_at"]


def test_persisted_chunks_freeze_total_parts_and_content_on_resume(tmp_path: Path) -> None:
    """First prepare_doc freezes chunks + SHA-256 + total_parts; resume ignores source edits/max_chars and >2-part docs cannot complete at part 2."""
    from src.dalton import podcast_weekly_store as store

    data_root = tmp_path / "openclaw-data"
    out_dir = data_root / "research-outputs" / "podcast-weekly"
    st_dir = data_root / "state" / "podcast-weekly"
    out_dir.mkdir(parents=True)
    st_dir.mkdir(parents=True)

    zh_para = "\n\n".join(f"第 {i} 段中文实录内容，包含长文本以便稳定切分为三段以上。" * 8 for i in range(1, 20))
    (out_dir / "2026-11-15-ep_chunks.zh.md").write_text(zh_para, encoding="utf-8")
    (out_dir / "2026-11-15-ep_chunks.en.md").write_text("English transcript body. " * 60, encoding="utf-8")
    (out_dir / "2026-11-15-ep_chunks.notes.json").write_text(
        json.dumps(
            {
                "channel": "All-In",
                "title": "Chunks Title",
                "upload_date": "20261114",
                "url": "https://www.youtube.com/watch?v=ep_chunks",
                "duration_min": 45,
                "corrections": [],
            }
        ),
        encoding="utf-8",
    )
    (out_dir / "2026-11-15-ep_chunks.summary.md").write_text("# 分段锁定标题\n\n核心提要判断。\n", encoding="utf-8")
    (st_dir / "episodes.json").write_text(
        json.dumps(
            {
                "ep_chunks": {
                    "stage": "ready",
                    "channel": "All-In",
                    "title": "Chunks Title",
                    "upload_date": "20261114",
                    "notes_path": "research-outputs/podcast-weekly/2026-11-15-ep_chunks.notes.json",
                }
            }
        ),
        encoding="utf-8",
    )

    prep1 = store.prepare_doc(data_root, "ep_chunks", max_chars=1200)
    assert prep1["ok"] is True and prep1["create"] is True
    n_parts = prep1["total_parts"]
    assert n_parts >= 3
    orig_chunks = prep1["chunks"]
    manifest_file = st_dir / "docs" / "ep_chunks" / "chunks" / "manifest.json"
    assert manifest_file.is_file()
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    assert manifest["total_parts"] == n_parts
    assert len(manifest["parts"]) == n_parts

    # Save partial doc after part 1 (even if caller passes lower total_parts=2)
    saved = store.save_doc(
        data_root,
        "ep_chunks",
        doc_url="https://feishu.cn/docx/dox_chunks",
        document_id="dox_chunks",
        zh_title="分段锁定标题",
        total_parts=2,
        completed_parts=[1],
        body_complete=False,
    )
    assert saved["total_parts"] == n_parts
    assert saved["body_status"] == "incomplete"

    # Mutate workspace .zh.md drastically AND pass much larger max_chars on resume
    (out_dir / "2026-11-15-ep_chunks.zh.md").write_text("已被改写后的极短新正文。", encoding="utf-8")
    prep2 = store.prepare_doc(data_root, "ep_chunks", max_chars=30000)
    assert prep2["resume_append"] is True
    assert prep2["total_parts"] == n_parts
    assert prep2["chunks"] == orig_chunks
    assert [c["part"] for c in prep2["remaining_chunks"]] == list(range(2, n_parts + 1))

    # Passing hardcoded part=2, total_parts=2 MUST NOT mark >2-part doc complete prematurely
    c2 = store.record_chunk(
        data_root,
        "ep_chunks",
        part=2,
        total_parts=2,
        document_id="dox_chunks",
        doc_url="https://feishu.cn/docx/dox_chunks",
    )
    assert c2["body_status"] == "incomplete"
    assert c2["total_parts"] == n_parts
    assert c2["next_part"] == 3

    for p in range(3, n_parts + 1):
        res_p = store.record_chunk(data_root, "ep_chunks", part=p, total_parts=n_parts)
    assert res_p["body_status"] == "complete"

    # Verify SKILL.md delegates doc chunking/appending to publish_doc rather than hardcoded part=1/2 calls
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert "part=2, total_parts=2" not in skill_text
    assert "part=1, total_parts=1" not in skill_text
    assert 'action="publish_doc"' in skill_text


def test_stateless_status_hash_determinism_and_cross_day_unreported_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """get_status writes no files, builds deterministic report_run_id hashes, and recovers unreported Day 1 episodes on Day 2."""
    from src.dalton import podcast_weekly_store as store

    # mark_reported stamps reported_at with the real clock, and "already reported today"
    # compares that stamp's Hong Kong date with the queried day. Pin the clock to Day 1 so
    # the Day 2 assertions do not depend on the date the suite happens to run.
    class _Day1Clock(store.datetime):
        @classmethod
        def now(cls, tz=None):
            return store.datetime(2026, 11, 15, 12, 0, tzinfo=store.HK_TZ).astimezone(tz) if tz else store.datetime(2026, 11, 15, 12, 0)

    monkeypatch.setattr(store, "datetime", _Day1Clock)

    data_root = tmp_path / "openclaw-data"
    st_dir = data_root / "state" / "podcast-weekly"
    st_dir.mkdir(parents=True)
    (st_dir / "episodes.json").write_text(
        json.dumps(
            {
                "ep_day1": {
                    "stage": "ready",
                    "channel": "a16z",
                    "title": "Day 1 Episode",
                    "upload_date": "20261114",
                }
            }
        ),
        encoding="utf-8",
    )
    store.save_doc(
        data_root,
        "ep_day1",
        doc_url="https://feishu.cn/docx/dox_day1",
        document_id="dox_day1",
        zh_title="第一天节目",
        total_parts=1,
        completed_parts=[1],
        body_complete=True,
    )

    # First & second status calls are pure reads (no reported.json written) and produce identical deterministic run_id
    st_day1_a = store.get_status(data_root, date_str="2026-11-15")
    st_day1_b = store.get_status(data_root, date_str="2026-11-15")
    assert st_day1_a["next_action"] == "submit"
    assert re.fullmatch(r"podcast-weekly-2026-11-15-[0-9a-f]{10}", st_day1_a["report_run_id"])
    assert st_day1_a["report_run_id"] == st_day1_b["report_run_id"]
    assert not store.reported_path(data_root).exists()
    assert not (st_dir / "batches").exists()

    # Adding a failed episode changes the 10-hex hash
    ledger = store.read_json_dict(store.ledger_path(data_root))
    ledger["ep_day1_fail"] = {
        "stage": "asr_failed",
        "stage_error": "asr error",
        "title": "Day 1 Fail",
        "upload_date": "20261114",
    }
    store.atomic_write_json(store.ledger_path(data_root), ledger)
    st_day1_changed = store.get_status(data_root, date_str="2026-11-15")
    assert st_day1_changed["report_run_id"] != st_day1_a["report_run_id"]
    assert [it["video_id"] for it in st_day1_changed["report_failed"]] == ["ep_day1_fail"]

    # Save pending report on Day 1 -> crash before confirmation -> Day 2 gets full identical report_run_id and exact text
    pending_text = "播客周报｜2026-11-15 测试汇总原文\nhttps://feishu.cn/docx/dox_day1"
    saved_pending = store.save_pending_report(
        data_root,
        st_day1_changed["report_run_id"],
        pending_text,
        success_ids=["ep_day1"],
        failed_ids=["ep_day1_fail"],
        date_str="2026-11-15",
    )
    assert saved_pending["ok"] is True

    st_day2_a = store.get_status(data_root, date_str="2026-11-16")
    st_day2_b = store.get_status(data_root, date_str="2026-11-16")
    assert st_day2_a["next_action"] == "resubmit_pending"
    assert st_day2_a["report_run_id"] == st_day1_changed["report_run_id"]
    assert st_day2_a["report_run_id"] == st_day2_b["report_run_id"]
    assert st_day2_a["pending_report"]["report_run_id"] == st_day1_changed["report_run_id"]
    assert st_day2_a["pending_report"]["text"] == pending_text

    # Mark reported with Day 1 run_id on Day 2 -> cleans pending_report.json and never re-triggers
    res = store.mark_reported(
        data_root,
        st_day2_a["report_run_id"],
        success_ids=st_day2_a["pending_report"]["success_video_ids"],
        failed_ids=st_day2_a["pending_report"]["failed_video_ids"],
    )
    assert res["ok"] is True
    assert res["deleted_pending_report"] is True
    assert not store.pending_report_path(data_root).exists()
    assert set(res["recorded_success"]) == {"ep_day1"}
    assert set(res["recorded_failed"]) == {"ep_day1_fail"}

    st_day2_after = store.get_status(data_root, date_str="2026-11-16")
    assert st_day2_after["next_action"] == "empty_week"
    assert st_day2_after["report_success"] == []
    assert st_day2_after["report_failed"] == []


@pytest.mark.asyncio
async def test_symlinked_podcast_root_dir_rejected_before_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Symlinked research-outputs/podcast-weekly or state/podcast-weekly inside data_root is rejected pre-resolve."""
    from src.dalton import podcast_weekly_store as store
    from src.dalton.tools import podcast_weekly_tools as pwt

    data_root = tmp_path / "openclaw-data"
    alt_dir = data_root / "research-outputs" / "redirected"
    alt_dir.mkdir(parents=True)
    (alt_dir / "2026-11-15-ep_sym.zh.md").write_text("redirected secret", encoding="utf-8")
    (data_root / "research-outputs" / "podcast-weekly").symlink_to(alt_dir)

    with pytest.raises(store.PodcastStoreSecurityError):
        store.safe_podcast_path(data_root, "research-outputs/podcast-weekly/2026-11-15-ep_sym.zh.md")

    # Test state/podcast-weekly symlink via podcast_weekly_read tool with real auth & valid var dirs
    target_var_dir = tmp_path / "var"
    oc_dir = target_var_dir / "openclaw-data"
    (oc_dir / "state" / "redirected_state").mkdir(parents=True)
    (oc_dir / "state" / "redirected_state" / "episodes.json").write_text("{}", encoding="utf-8")
    (oc_dir / "state" / "podcast-weekly").symlink_to(oc_dir / "state" / "redirected_state")

    monkeypatch.delenv("DALTON_BACKEND_VAR_DIR", raising=False)
    monkeypatch.setattr(
        store,
        "get_dalton_host_var_dir",
        create_autospec(store.get_dalton_host_var_dir, return_value=target_var_dir),
    )
    monkeypatch.setattr(
        store,
        "get_dalton_backend_var_dir",
        create_autospec(store.get_dalton_backend_var_dir, return_value=tmp_path / "missing_backend_var"),
    )

    async def _is_internal(uid: str) -> bool:
        return uid == "usr_internal"

    fake_auth = create_autospec(_is_internal, instance=False, spec_set=True)
    fake_auth.side_effect = _is_internal
    monkeypatch.setattr(pwt, "is_internal_or_admin", fake_auth)

    tools = {t.name: t for t in pwt.create_podcast_weekly_tools()}
    int_cfg = {"configurable": {"user_id": "usr_internal"}}
    res = json.loads(await tools["podcast_weekly_read"].ainvoke({"action": "status"}, config=int_cfg))
    assert res["ok"] is False
    assert res["error"] == "PodcastStoreSecurityError"
    assert "软链接" in res["message"]

    prep_res = json.loads(
        await tools["podcast_weekly_read"].ainvoke(
            {"action": "prepare_doc", "video_id": "ep_sym"}, config=int_cfg
        )
    )
    assert prep_res["ok"] is False
    assert prep_res["error"] == "PodcastStoreSecurityError"


def test_podcast_publish_cli_runs_standalone_without_pythonpath(tmp_path: Path) -> None:
    """Running podcast_publish.py in a clean process with PYTHONPATH stripped resolves repo root and runs status."""
    import os
    import subprocess
    import sys

    ws = tmp_path / "ws"
    st_dir = ws / "state" / "podcast-weekly"
    st_dir.mkdir(parents=True)
    (st_dir / "episodes.json").write_text(
        json.dumps({"ep_cli": {"stage": "ready", "title": "CLI Test", "upload_date": "20261114"}}),
        encoding="utf-8",
    )

    clean_env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    help_proc = subprocess.run(
        [sys.executable, str(PUBLISH_CLI_PATH), "--help"],
        env=clean_env,
        cwd=str(ws),
        capture_output=True,
        text=True,
        check=False,
    )
    assert help_proc.returncode == 0, help_proc.stderr
    assert "prepare-doc" in help_proc.stdout

    st_proc = subprocess.run(
        [
            sys.executable,
            str(PUBLISH_CLI_PATH),
            "status",
            "--workspace-dir",
            str(ws),
            "--date",
            "2026-11-15",
            "--json",
        ],
        env=clean_env,
        cwd=str(ws),
        capture_output=True,
        text=True,
        check=False,
    )
    assert st_proc.returncode == 0, st_proc.stderr
    payload = json.loads(st_proc.stdout)
    assert payload["ok"] is True
    assert payload["ready"][0]["video_id"] == "ep_cli"


def test_persisted_manifest_overrides_stale_doc_total_parts_and_drops_extra_parts(
    tmp_path: Path,
) -> None:
    """When persisted chunks manifest has 1 part, stale doc total_parts=2 + part=2 are overridden and completing part 1 finishes doc."""
    from src.dalton import podcast_weekly_store as store

    data_root = tmp_path / "openclaw-data"
    _seed_ready_episode(data_root, vid="ep_shrink")
    (data_root / "research-outputs" / "podcast-weekly" / "20261110-ep_shrink.summary.md").write_text(
        "# 单段标题\n\n短提要。\n", encoding="utf-8"
    )

    prep = store.prepare_doc(data_root, "ep_shrink", max_chars=15000)
    assert prep["ok"] is True and prep["total_parts"] == 1

    # Simulate legacy/buggy doc.json with stale total_parts=2 and out-of-range completed_parts=[2]
    dpath = store.doc_record_path(data_root, "ep_shrink")
    store.atomic_write_json(
        dpath,
        {
            "video_id": "ep_shrink",
            "document_id": "dox_shrink",
            "doc_url": "https://feishu.cn/docx/dox_shrink",
            "zh_title": "单段标题",
            "body_status": "incomplete",
            "total_parts": 2,
            "completed_parts": [2],
            "next_part": 1,
        },
    )

    res = store.record_chunk(
        data_root,
        "ep_shrink",
        part=1,
        total_parts=2,
        document_id="dox_shrink",
        doc_url="https://feishu.cn/docx/dox_shrink",
    )
    assert res["total_parts"] == 1
    assert res["completed_parts"] == [1]
    assert res["body_status"] == "complete"
    assert res["next_part"] is None
    assert store.read_json_dict(store.ledger_path(data_root))["ep_shrink"]["published_at"]


def test_recovery_from_prior_failure_sets_recovered_flag_and_never_overwrites_timestamps(
    tmp_path: Path,
) -> None:
    """Failed episodes recorded via mark_reported trigger recovered=True once published and preserve prior timestamps."""
    from src.dalton import podcast_weekly_store as store

    data_root = tmp_path / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)
    lpath = store.ledger_path(data_root)
    store.atomic_write_json(
        lpath,
        {
            "ep_ok": {"stage": "ready", "title": "OK Ep", "upload_date": "20261114"},
            "ep_fail_a": {
                "stage": "asr_failed",
                "stage_error": "asr timeout",
                "title": "Fail A",
                "upload_date": "20261114",
            },
        },
    )
    store.save_doc(
        data_root,
        "ep_ok",
        doc_url="https://feishu.cn/docx/ok",
        document_id="dox_ok",
        zh_title="成功期",
        total_parts=1,
        completed_parts=[1],
        body_complete=True,
    )

    st1 = store.get_status(data_root, date_str="2026-11-15")
    assert st1["next_action"] == "submit"
    assert [it["video_id"] for it in st1["report_success"]] == ["ep_ok"]
    assert st1["report_success"][0]["recovered"] is False
    assert [it["video_id"] for it in st1["report_failed"]] == ["ep_fail_a"]

    marked1 = store.mark_reported(
        data_root,
        st1["report_run_id"],
        success_ids=["ep_ok"],
        failed_ids=["ep_fail_a"],
    )
    assert marked1["ok"] is True
    assert marked1["recorded_success"] == ["ep_ok"]
    assert marked1["recorded_failed"] == ["ep_fail_a"]
    orig_ok_ts = marked1["reported"]["success"]["ep_ok"]["reported_at"]
    orig_fail_ts = marked1["reported"]["failed"]["ep_fail_a"]["reported_at"]

    # Same-day rerun with no recovery or new items -> nothing_to_report
    st_same = store.get_status(data_root, date_str="2026-11-15")
    assert st_same["next_action"] == "nothing_to_report"

    # Next week while ep_fail_a stays unresolved -> not re-disclosed -> empty_week; mark empty preserves prior timestamps
    st_next_week = store.get_status(data_root, date_str="2026-11-22")
    assert st_next_week["next_action"] == "empty_week"
    assert st_next_week["report_run_id"] == "podcast-weekly-2026-11-22-empty"
    assert st_next_week["report_failed"] == []
    assert [it["video_id"] for it in st_next_week["stage_incomplete"]] == ["ep_fail_a"]

    marked_empty = store.mark_reported(
        data_root,
        st_next_week["report_run_id"],
        empty_date="2026-11-22",
    )
    assert marked_empty["ok"] is True and marked_empty["empty_date"] == "2026-11-22"
    assert marked_empty["reported"]["success"]["ep_ok"]["reported_at"] == orig_ok_ts
    assert marked_empty["reported"]["failed"]["ep_fail_a"]["reported_at"] == orig_fail_ts
    assert store.get_status(data_root, date_str="2026-11-22")["next_action"] == "nothing_to_report"

    # Once ep_fail_a recovers to ready -> next_action="publish" (ep_fail_a in to_publish)
    ledger = store.read_json_dict(lpath)
    ledger["ep_fail_a"]["stage"] = "ready"
    ledger["ep_fail_a"]["stage_error"] = ""
    store.atomic_write_json(lpath, ledger)
    st_ready_rec = store.get_status(data_root, date_str="2026-11-22")
    assert st_ready_rec["next_action"] == "publish"
    assert [it["video_id"] for it in st_ready_rec["to_publish"]] == ["ep_fail_a"]
    assert st_ready_rec["report_success"] == []

    # After ep_fail_a completes Feishu doc -> report_success marks recovered=True and omits from report_failed
    store.save_doc(
        data_root,
        "ep_fail_a",
        doc_url="https://feishu.cn/docx/fail_a_recovered",
        document_id="dox_fail_a",
        zh_title="补发成功",
        total_parts=1,
        completed_parts=[1],
        body_complete=True,
    )
    st_submit_rec = store.get_status(data_root, date_str="2026-11-22")
    assert st_submit_rec["next_action"] == "submit"
    assert [it["video_id"] for it in st_submit_rec["report_success"]] == ["ep_fail_a"]
    assert st_submit_rec["report_success"][0]["recovered"] is True
    assert st_submit_rec["report_failed"] == []

    # Marking recovered success preserves prior ep_ok and ep_fail_a failed entry timestamps
    marked2 = store.mark_reported(
        data_root,
        st_submit_rec["report_run_id"],
        success_ids=["ep_fail_a"],
    )
    assert marked2["recorded_success"] == ["ep_fail_a"]
    assert marked2["reported"]["success"]["ep_ok"]["reported_at"] == orig_ok_ts
    assert marked2["reported"]["failed"]["ep_fail_a"]["reported_at"] == orig_fail_ts
    assert store.get_status(data_root, date_str="2026-11-22")["next_action"] == "nothing_to_report"


@pytest.mark.asyncio
async def test_mark_reported_ignores_unready_unpublished_or_unknown_ids_and_records_doc_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """mark_reported ignores ready/unknown/already-reported IDs in `ignored` and accepts doc_create_failed_twice."""
    from src.dalton import podcast_weekly_store as store
    from src.dalton.tools import podcast_weekly_tools as pwt

    var_dir = tmp_path / "var_dalton"
    data_root = var_dir / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)
    monkeypatch.setenv("DALTON_BACKEND_VAR_DIR", str(var_dir))

    async def _is_internal(uid: str) -> bool:
        return uid == "usr_internal"

    fake_auth = create_autospec(_is_internal, instance=False, spec_set=True)
    fake_auth.side_effect = _is_internal
    monkeypatch.setattr(pwt, "is_internal_or_admin", fake_auth)
    _, write_tool = pwt.create_podcast_weekly_tools()
    int_cfg = {"configurable": {"user_id": "usr_internal"}}

    store.atomic_write_json(
        store.ledger_path(data_root),
        {
            "ep_still_ready": {"stage": "ready", "title": "Ready", "upload_date": "20261114"},
            "ep_doc_fail": {"stage": "ready", "title": "Doc Fail", "upload_date": "20261114"},
        },
    )
    store.record_failure(data_root, "ep_doc_fail", "feishu 500 try 1")
    fail2 = store.record_failure(data_root, "ep_doc_fail", "feishu 500 try 2")
    assert fail2["reason"] == "doc_create_failed_twice"

    st = store.get_status(data_root, date_str="2026-11-15")
    assert st["next_action"] == "publish"
    assert [it["video_id"] for it in st["to_publish"]] == ["ep_still_ready"]
    assert [it["video_id"] for it in st["report_failed"]] == ["ep_doc_fail"]

    # Attempt to mark an unpublished ready ep and unknown ID as success -> both ignored, nothing written for them
    marked = json.loads(
        await write_tool.ainvoke(
            {
                "action": "mark_reported",
                "report_run_id": st["report_run_id"],
                "success_video_ids": ["ep_still_ready", "ep_unknown"],
                "failed_video_ids": ["ep_doc_fail", "ep_still_ready"],
            },
            config=int_cfg,
        )
    )
    assert marked["ok"] is True
    assert marked["recorded_success"] == []
    assert marked["recorded_failed"] == ["ep_doc_fail"]
    assert set(marked["ignored"]) == {"ep_still_ready", "ep_unknown"}
    assert "ep_still_ready" not in marked["reported"]["success"]
    assert "ep_still_ready" not in marked["reported"]["failed"]
    assert "ep_unknown" not in marked["reported"]["success"]

    # Ready ep still stays in to_publish next call
    st_after = store.get_status(data_root, date_str="2026-11-15")
    assert st_after["next_action"] == "publish"
    assert [it["video_id"] for it in st_after["to_publish"]] == ["ep_still_ready"]
    assert st_after["report_failed"] == []


def test_prepare_doc_missing_ledger_returns_ledger_missing(tmp_path: Path) -> None:
    """prepare_doc distinguishes missing episodes.json (ledger_missing) from absent episode entry (episode_not_in_ledger)."""
    from src.dalton import podcast_weekly_store as store

    data_root = tmp_path / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)

    missing_res = store.prepare_doc(data_root, "ep_missing")
    assert missing_res["ok"] is False
    assert missing_res["reason"] == "ledger_missing"
    assert missing_res["error"] == "ledger_missing"

    # Present ledger without that vid -> episode_not_in_ledger
    store.atomic_write_json(store.ledger_path(data_root), {"other_ep": {"stage": "ready"}})
    no_ep = store.prepare_doc(data_root, "ep_missing")
    assert no_ep["ok"] is False
    assert no_ep["reason"] == "episode_not_in_ledger"


def test_skipped_episode_in_step3_allows_submitting_published_and_retries_next_run(
    tmp_path: Path,
) -> None:
    """When one episode completes save_doc and a sibling stays skipped/ready, refreshed status puts the done episode in
    report_success (without the skipped one) so Step 4 submits it and marks it reported without looping back to Step 2."""
    from src.dalton import podcast_weekly_store as store

    data_root = tmp_path / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)
    store.atomic_write_json(
        store.ledger_path(data_root),
        {
            "ep_done": {"stage": "ready", "title": "Done", "upload_date": "20261114"},
            "ep_skipped": {"stage": "ready", "title": "Skipped", "upload_date": "20261114"},
        },
    )
    store.save_doc(
        data_root,
        "ep_done",
        doc_url="https://feishu.cn/docx/ep_done",
        document_id="dox_ep_done",
        zh_title="已完成期",
        total_parts=1,
        completed_parts=[1],
        body_complete=True,
    )

    st = store.get_status(data_root, date_str="2026-11-15")
    assert st["next_action"] == "publish"
    assert [it["video_id"] for it in st["to_publish"]] == ["ep_skipped"]
    assert [it["video_id"] for it in st["report_success"]] == ["ep_done"]
    assert re.fullmatch(r"podcast-weekly-2026-11-15-[0-9a-f]{10}", st["report_run_id"])

    # Step 4 submits report_success (ep_done only) and marks it reported; ep_skipped is ignored if passed
    marked = store.mark_reported(
        data_root,
        st["report_run_id"],
        success_ids=["ep_done", "ep_skipped"],
    )
    assert marked["recorded_success"] == ["ep_done"]
    assert marked["ignored"] == ["ep_skipped"]

    # Next run: ep_done is never re-reported; ep_skipped stays in to_publish for retry
    st_next = store.get_status(data_root, date_str="2026-11-15")
    assert st_next["next_action"] == "publish"
    assert [it["video_id"] for it in st_next["to_publish"]] == ["ep_skipped"]
    assert st_next["report_success"] == []


def test_podcast_publish_cli_mark_reported_and_empty_week_subcommands(tmp_path: Path) -> None:
    """podcast_publish.py mark-reported handles both episode IDs and --empty-date cleanly end-to-end."""
    from src.dalton import podcast_weekly_store as store

    pub = _load_module("podcast_publish_cli_r9", PUBLISH_CLI_PATH)
    ws_root = tmp_path / "ws"
    (ws_root / "state" / "podcast-weekly").mkdir(parents=True)
    store.atomic_write_json(
        store.ledger_path(ws_root),
        {
            "ep_cli_ok": {"stage": "ready", "title": "CLI OK", "upload_date": "20261114"},
            "ep_cli_fail": {
                "stage": "asr_failed",
                "stage_error": "asr error",
                "title": "CLI Fail",
                "upload_date": "20261114",
            },
        },
    )
    store.save_doc(
        ws_root,
        "ep_cli_ok",
        doc_url="https://feishu.cn/docx/cli_ok",
        document_id="dox_cli_ok",
        zh_title="CLI 成功",
        total_parts=1,
        completed_parts=[1],
        body_complete=True,
    )

    rc1, st1 = _run_cli(pub, ["status", "--workspace-dir", str(ws_root), "--date", "2026-11-15", "--json"])
    assert rc1 == 0
    assert st1["next_action"] == "submit"
    run_id = st1["report_run_id"]

    rc_mark, marked = _run_cli(
        pub,
        [
            "mark-reported",
            "--workspace-dir",
            str(ws_root),
            "--report-run-id",
            run_id,
            "--success-video-ids",
            "ep_cli_ok",
            "--failed-video-ids",
            "ep_cli_fail",
            "--json",
        ],
    )
    assert rc_mark == 0
    assert marked["ok"] is True
    assert marked["recorded_success"] == ["ep_cli_ok"]
    assert marked["recorded_failed"] == ["ep_cli_fail"]

    rc_same, st_same = _run_cli(
        pub, ["status", "--workspace-dir", str(ws_root), "--date", "2026-11-15", "--json"]
    )
    assert rc_same == 0
    assert st_same["next_action"] == "nothing_to_report"

    rc_next, st_next = _run_cli(
        pub, ["status", "--workspace-dir", str(ws_root), "--date", "2026-11-22", "--json"]
    )
    assert rc_next == 0
    assert st_next["next_action"] == "empty_week"
    assert st_next["report_run_id"] == "podcast-weekly-2026-11-22-empty"

    rc_empty, marked_empty = _run_cli(
        pub,
        [
            "mark-reported",
            "--workspace-dir",
            str(ws_root),
            "--report-run-id",
            st_next["report_run_id"],
            "--empty-date",
            "2026-11-22",
            "--json",
        ],
    )
    assert rc_empty == 0
    assert marked_empty["ok"] is True
    assert marked_empty["empty_date"] == "2026-11-22"

    rc_after, st_next_after = _run_cli(
        pub, ["status", "--workspace-dir", str(ws_root), "--date", "2026-11-22", "--json"]
    )
    assert rc_after == 0
    assert st_next_after["next_action"] == "nothing_to_report"


def test_save_pending_report_validation_rejects_mismatch_non_subset_and_duplicate(
    tmp_path: Path,
) -> None:
    """save_pending_report rejects non-matching run_id, non-subset video IDs, bad empty_week args, and duplicates."""
    from src.dalton import podcast_weekly_store as store

    data_root = tmp_path / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)
    store.atomic_write_json(
        store.ledger_path(data_root),
        {
            "ep_s1": {"stage": "ready", "title": "S1", "upload_date": "20261114"},
            "ep_f1": {
                "stage": "asr_failed",
                "stage_error": "asr fail",
                "title": "F1",
                "upload_date": "20261114",
            },
        },
    )
    store.save_doc(
        data_root,
        "ep_s1",
        doc_url="https://feishu.cn/docx/s1",
        document_id="dox_s1",
        zh_title="S1 标题",
        total_parts=1,
        completed_parts=[1],
        body_complete=True,
    )
    st = store.get_status(data_root, date_str="2026-11-15")
    run_id = st["report_run_id"]

    # Reject wrong report_run_id (hash mismatch or wrong date)
    bad_hash = store.save_pending_report(
        data_root,
        "podcast-weekly-2026-11-15-0000000000",
        "汇总",
        success_ids=["ep_s1"],
        failed_ids=["ep_f1"],
        date_str="2026-11-15",
    )
    assert bad_hash["ok"] is False and bad_hash["reason"] == "report_run_id_mismatch"

    bad_date = store.save_pending_report(
        data_root,
        run_id.replace("2026-11-15", "2026-11-16"),
        "汇总",
        success_ids=["ep_s1"],
        failed_ids=["ep_f1"],
        date_str="2026-11-15",
    )
    assert bad_date["ok"] is False and bad_date["reason"] == "report_run_id_mismatch"

    # Reject non-subset / swapped IDs and empty_date on non-empty report
    bad_sub = store.save_pending_report(
        data_root,
        run_id,
        "汇总",
        success_ids=["ep_s1", "ep_not_in_status"],
        failed_ids=["ep_f1"],
        date_str="2026-11-15",
    )
    assert bad_sub["ok"] is False and bad_sub["reason"] == "invalid_video_ids"

    bad_swap = store.save_pending_report(
        data_root,
        run_id,
        "汇总",
        success_ids=["ep_f1"],
        failed_ids=["ep_s1"],
        date_str="2026-11-15",
    )
    assert bad_swap["ok"] is False and bad_swap["reason"] == "invalid_video_ids"

    # Valid save_pending_report succeeds (even with subset failed_ids=[] if desired)
    ok_save = store.save_pending_report(
        data_root,
        run_id,
        "播客周报｜S1",
        success_ids=["ep_s1"],
        failed_ids=["ep_f1"],
        date_str="2026-11-15",
    )
    assert ok_save["ok"] is True and store.pending_report_path(data_root).exists()

    # Duplicate save_pending_report while one exists -> pending_report_exists
    dup = store.save_pending_report(
        data_root,
        run_id,
        "另一份正文",
        success_ids=["ep_s1"],
        date_str="2026-11-15",
    )
    assert dup["ok"] is False and dup["reason"] == "pending_report_exists"


def test_pending_report_resubmit_deletion_guard_and_same_day_new_episode_isolation(
    tmp_path: Path,
) -> None:
    """resubmit_pending stays immutable across days, mark_reported deletes pending only on matching run_id,
    allows already-reported IDs without overwriting, and isolates newly added same-day episodes into a fresh report."""
    from src.dalton import podcast_weekly_store as store

    data_root = tmp_path / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)
    lpath = store.ledger_path(data_root)
    store.atomic_write_json(
        lpath,
        {"ep_one": {"stage": "ready", "title": "One", "upload_date": "20261114"}},
    )
    store.save_doc(
        data_root,
        "ep_one",
        doc_url="https://feishu.cn/docx/one",
        document_id="dox_one",
        zh_title="一期",
        total_parts=1,
        completed_parts=[1],
        body_complete=True,
    )

    st1 = store.get_status(data_root, date_str="2026-11-15")
    run1 = st1["report_run_id"]
    orig_text = "播客周报｜第一期 原文"
    assert store.save_pending_report(
        data_root, run1, orig_text, success_ids=["ep_one"], date_str="2026-11-15"
    )["ok"] is True

    # Same-day second episode ep_two publishes + arrives while pending_report for ep_one is waiting confirmation
    ledger = store.read_json_dict(lpath)
    ledger["ep_two"] = {"stage": "ready", "title": "Two", "upload_date": "20261115"}
    store.atomic_write_json(lpath, ledger)
    store.save_doc(
        data_root,
        "ep_two",
        doc_url="https://feishu.cn/docx/two",
        document_id="dox_two",
        zh_title="二期",
        total_parts=1,
        completed_parts=[1],
        body_complete=True,
    )

    # Status remains resubmit_pending with exact original run_id and text (even next day)
    st_cross = store.get_status(data_root, date_str="2026-11-16")
    assert st_cross["next_action"] == "resubmit_pending"
    assert st_cross["report_run_id"] == run1
    assert st_cross["pending_report"]["report_run_id"] == run1
    assert st_cross["pending_report"]["text"] == orig_text
    assert st_cross["pending_report"]["success_video_ids"] == ["ep_one"]

    # Pre-mark ep_one directly in reported.json (simulating external settlement before resubmit finishes)
    rep = store.read_json_dict(store.reported_path(data_root))
    rep.setdefault("success", {})["ep_one"] = {
        "reported_at": "2026-11-15T10:00:00+08:00",
        "report_run_id": "podcast-weekly-2026-11-15-external",
    }
    rep.setdefault("failed", {})
    rep.setdefault("empty_weeks", {})
    store.atomic_write_json(store.reported_path(data_root), rep)

    # Calling mark_reported with a mismatching report_run_id MUST NOT delete pending_report.json
    mismatch = store.mark_reported(
        data_root,
        "podcast-weekly-2026-11-15-ffffffff00",
        success_ids=["ep_one"],
    )
    assert mismatch["ok"] is True
    assert mismatch["deleted_pending_report"] is False
    assert store.pending_report_path(data_root).exists()

    # Calling mark_reported with matching pending run1 records nothing new for ep_one, preserves its timestamp, NOT in ignored, AND deletes pending_report.json
    confirmed = store.mark_reported(
        data_root,
        run1,
        success_ids=st_cross["pending_report"]["success_video_ids"],
        failed_ids=st_cross["pending_report"]["failed_video_ids"],
    )
    assert confirmed["ok"] is True
    assert confirmed["deleted_pending_report"] is True
    assert confirmed["ignored"] == []
    assert confirmed["reported"]["success"]["ep_one"]["reported_at"] == "2026-11-15T10:00:00+08:00"
    assert not store.pending_report_path(data_root).exists()

    # Next status after resubmit settlement: ep_one absent from report_success; ONLY ep_two forms the new report!
    st_after = store.get_status(data_root, date_str="2026-11-15")
    assert st_after["next_action"] == "submit"
    assert st_after["pending_report"] is None
    assert [it["video_id"] for it in st_after["report_success"]] == ["ep_two"]
    assert st_after["report_run_id"] != run1


@pytest.mark.asyncio
async def test_backend_tools_save_pending_report_and_mark_reported_with_autospec(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Backend tools and CLI cleanly support save_pending_report -> mark_reported with create_autospec spies."""
    from src.dalton import podcast_weekly_store as store
    from src.dalton.tools import podcast_weekly_tools as pwt

    var_dir = tmp_path / "var_dalton"
    data_root = var_dir / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)
    monkeypatch.setenv("DALTON_BACKEND_VAR_DIR", str(var_dir))

    async def _is_internal(uid: str) -> bool:
        return uid == "usr_internal"

    fake_auth = create_autospec(_is_internal, instance=False, spec_set=True)
    fake_auth.side_effect = _is_internal
    monkeypatch.setattr(pwt, "is_internal_or_admin", fake_auth)

    spy_save_pending = create_autospec(store.save_pending_report, spec_set=True, side_effect=store.save_pending_report)
    spy_mark_reported = create_autospec(store.mark_reported, spec_set=True, side_effect=store.mark_reported)
    monkeypatch.setattr(pwt, "save_pending_report", spy_save_pending)
    monkeypatch.setattr(pwt, "mark_reported", spy_mark_reported)

    read_tool, write_tool = pwt.create_podcast_weekly_tools()
    int_cfg = {"configurable": {"user_id": "usr_internal"}}

    store.atomic_write_json(store.ledger_path(data_root), {})
    st_empty = json.loads(
        await read_tool.ainvoke({"action": "status", "date": "2026-11-29"}, config=int_cfg)
    )
    assert st_empty["next_action"] == "empty_week"
    empty_run_id = st_empty["report_run_id"]

    # Tool saves empty-week pending_report
    pend_res = json.loads(
        await write_tool.ainvoke(
            {
                "action": "save_pending_report",
                "report_run_id": empty_run_id,
                "text": "播客周报｜本周 All-In 和 a16z 没有新节目",
                "success_video_ids": [],
                "failed_video_ids": [],
                "empty_date": "2026-11-29",
                "date": "2026-11-29",
            },
            config=int_cfg,
        )
    )
    assert pend_res["ok"] is True
    assert spy_save_pending.call_count == 1

    # Status now returns resubmit_pending with preserved empty_date
    st_resub = json.loads(
        await read_tool.ainvoke({"action": "status", "date": "2026-11-30"}, config=int_cfg)
    )
    assert st_resub["next_action"] == "resubmit_pending"
    assert st_resub["report_run_id"] == empty_run_id
    assert st_resub["pending_report"]["empty_date"] == "2026-11-29"

    # Only after delivery_confirmed=true -> tool calls mark_reported (spy verifies args & deletion)
    marked_res = json.loads(
        await write_tool.ainvoke(
            {
                "action": "mark_reported",
                "report_run_id": empty_run_id,
                "success_video_ids": [],
                "failed_video_ids": [],
                "empty_date": "2026-11-29",
            },
            config=int_cfg,
        )
    )
    assert marked_res["ok"] is True
    assert marked_res["deleted_pending_report"] is True
    assert spy_mark_reported.call_count == 1
    assert spy_mark_reported.call_args.kwargs["empty_date"] == "2026-11-29"
    assert not store.pending_report_path(data_root).exists()

    # Also exercise CLI `save-pending-report` subcommand for completeness
    pub = _load_module("podcast_publish_cli_r10", PUBLISH_CLI_PATH)
    rc_pend_cli, cli_out = _run_cli(
        pub,
        [
            "save-pending-report",
            "--workspace-dir",
            str(data_root),
            "--report-run-id",
            "podcast-weekly-2026-12-06-empty",
            "--text",
            "播客周报｜本周 All-In 和 a16z 没有新节目",
            "--empty-date",
            "2026-12-06",
            "--date",
            "2026-12-06",
            "--json",
        ],
    )
    assert rc_pend_cli == 0
    assert cli_out["ok"] is True and cli_out["pending_report"]["empty_date"] == "2026-12-06"


@pytest.mark.asyncio
async def test_abandon_pending_report_guards_and_r2_r3_revision_progression(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """abandon_pending_report rejects same-day and mismatching run_id, moves expired pending_report to
    reported.json['abandoned'] without marking episodes, advances hash run_id to -r2/-r3, and works via tool & CLI."""
    from src.dalton import podcast_weekly_store as store
    from src.dalton.database import automation_reports
    from src.dalton.tools import podcast_weekly_tools as pwt

    var_dir = tmp_path / "var_dalton"
    data_root = var_dir / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)
    monkeypatch.setenv("DALTON_BACKEND_VAR_DIR", str(var_dir))

    async def _is_internal(uid: str) -> bool:
        return uid == "usr_internal"

    fake_auth = create_autospec(_is_internal, instance=False, spec_set=True)
    fake_auth.side_effect = _is_internal
    monkeypatch.setattr(pwt, "is_internal_or_admin", fake_auth)
    spy_abandon = create_autospec(
        store.abandon_pending_report, spec_set=True, side_effect=store.abandon_pending_report
    )
    monkeypatch.setattr(pwt, "abandon_pending_report", spy_abandon)
    _, write_tool = pwt.create_podcast_weekly_tools()
    int_cfg = {"configurable": {"user_id": "usr_internal"}}

    store.atomic_write_json(
        store.ledger_path(data_root),
        {
            "ep_abn_ok": {"stage": "ready", "title": "Abn OK", "upload_date": "20261114"},
            "ep_abn_fail": {
                "stage": "asr_failed",
                "stage_error": "asr fail",
                "title": "Abn Fail",
                "upload_date": "20261114",
            },
        },
    )
    store.save_doc(
        data_root,
        "ep_abn_ok",
        doc_url="https://feishu.cn/docx/abn_ok",
        document_id="dox_abn_ok",
        zh_title="放弃恢复测试",
        total_parts=1,
        completed_parts=[1],
        body_complete=True,
    )

    st1 = store.get_status(data_root, date_str="2026-11-15")
    run1 = st1["report_run_id"]
    assert store.save_pending_report(
        data_root,
        run1,
        "原文 1",
        success_ids=["ep_abn_ok"],
        failed_ids=["ep_abn_fail"],
        date_str="2026-11-15",
    )["ok"] is True

    # Force pending_report created_at to HK date 2026-11-15 for deterministic date comparisons
    pend_rec = store.read_json_dict(store.pending_report_path(data_root), required=True)
    pend_rec["created_at"] = "2026-11-15T09:00:00+08:00"
    store.atomic_write_json(store.pending_report_path(data_root), pend_rec)

    # Same-day abandon -> rejected (pending_report_not_expired); mismatch run_id -> rejected
    same_day = store.abandon_pending_report(data_root, run1, date_str="2026-11-15")
    assert same_day["ok"] is False and same_day["reason"] == "pending_report_not_expired"
    mismatch = store.abandon_pending_report(
        data_root, "podcast-weekly-2026-11-15-0000000000", date_str="2026-11-16"
    )
    assert mismatch["ok"] is False and mismatch["reason"] == "report_run_id_mismatch"
    assert store.pending_report_path(data_root).exists()

    # Expired (created 2026-11-15, run date 2026-11-16) -> abandon succeeds via backend tool
    abn1 = json.loads(
        await write_tool.ainvoke(
            {"action": "abandon_pending_report", "report_run_id": run1, "date": "2026-11-16"},
            config=int_cfg,
        )
    )
    assert abn1["ok"] is True
    assert abn1["deleted_pending_report"] is True
    assert spy_abandon.call_count == 1
    assert not store.pending_report_path(data_root).exists()
    assert run1 in abn1["reported"]["abandoned"]
    assert abn1["reported"]["abandoned"][run1]["created_at"] == "2026-11-15T09:00:00+08:00"
    assert abn1["reported"]["abandoned"][run1]["success_video_ids"] == ["ep_abn_ok"]
    assert abn1["reported"]["abandoned"][run1]["failed_video_ids"] == ["ep_abn_fail"]
    # Episodes remain unreported so they re-enter status
    assert abn1["reported"]["success"] == {} and abn1["reported"]["failed"] == {}

    # Re-computing status on same date 2026-11-15 yields <run1>-r2 and satisfies both regexes
    st_r2 = store.get_status(data_root, date_str="2026-11-15")
    run_r2 = st_r2["report_run_id"]
    assert run_r2 == f"{run1}-r2"
    assert store.REPORT_RUN_ID_RE.fullmatch(run_r2)
    assert automation_reports._REPORT_RUN_ID_RE.fullmatch(run_r2)
    assert [it["video_id"] for it in st_r2["report_success"]] == ["ep_abn_ok"]
    assert [it["video_id"] for it in st_r2["report_failed"]] == ["ep_abn_fail"]

    # Save pending -r2, seed prior created_at, abandon via CLI -> next run_id advances to -r3
    assert store.save_pending_report(
        data_root,
        run_r2,
        "原文 2",
        success_ids=["ep_abn_ok"],
        failed_ids=["ep_abn_fail"],
        date_str="2026-11-15",
    )["ok"] is True
    pend_r2 = store.read_json_dict(store.pending_report_path(data_root), required=True)
    pend_r2["created_at"] = "2026-11-15T12:00:00+08:00"
    store.atomic_write_json(store.pending_report_path(data_root), pend_r2)

    pub = _load_module("podcast_publish_cli_r11", PUBLISH_CLI_PATH)
    rc_abn, abn2 = _run_cli(
        pub,
        [
            "abandon-pending-report",
            "--workspace-dir",
            str(data_root),
            "--report-run-id",
            run_r2,
            "--date",
            "2026-11-16",
            "--json",
        ],
    )
    assert rc_abn == 0 and abn2["ok"] is True
    assert run_r2 in abn2["reported"]["abandoned"] and run1 in abn2["reported"]["abandoned"]

    st_r3 = store.get_status(data_root, date_str="2026-11-15")
    run_r3 = st_r3["report_run_id"]
    assert run_r3 == f"{run1}-r3"
    assert store.REPORT_RUN_ID_RE.fullmatch(run_r3)
    assert automation_reports._REPORT_RUN_ID_RE.fullmatch(run_r3)

    # Confirming -r3 via mark_reported preserves abandoned audit trail
    marked3 = store.mark_reported(
        data_root, run_r3, success_ids=["ep_abn_ok"], failed_ids=["ep_abn_fail"]
    )
    assert set(marked3["reported"]["abandoned"].keys()) == {run1, run_r2}


def test_empty_week_cross_day_reported_at_prevents_same_day_duplicate_empty_week(
    tmp_path: Path,
) -> None:
    """When yesterday's empty_week confirms delivery today, _has_report_on_date matches reported_at HK date
    so same-day status returns nothing_to_report instead of firing a duplicate empty_week message."""
    from src.dalton import podcast_weekly_store as store

    data_root = tmp_path / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)
    store.atomic_write_json(store.ledger_path(data_root), {})

    # Empty week from 2026-11-15 delivered/marked on 2026-11-16 HK morning (and test UTC -> HK conversion)
    store.atomic_write_json(
        store.reported_path(data_root),
        {
            "success": {},
            "failed": {},
            "empty_weeks": {
                "2026-11-15": {
                    "reported_at": "2026-11-15T18:30:00+00:00",  # == 2026-11-16T02:30:00+08:00 HK
                    "report_run_id": "podcast-weekly-2026-11-15-empty",
                }
            },
            "abandoned": {},
        },
    )

    st_day2 = store.get_status(data_root, date_str="2026-11-16")
    assert st_day2["next_action"] == "nothing_to_report"
    assert st_day2["empty_week"] is False

    # Next week (2026-11-22) is still empty_week
    st_next_week = store.get_status(data_root, date_str="2026-11-22")
    assert st_next_week["next_action"] == "empty_week"


def _setup_podcast_tool_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    internal_user: str = "usr_internal",
):
    from src.dalton import podcast_weekly_store as store
    from src.dalton.tools import podcast_weekly_tools as pwt

    var_dir = tmp_path / "var_dalton"
    data_root = var_dir / "openclaw-data"
    (data_root / "state" / "podcast-weekly").mkdir(parents=True)
    (data_root / "research-outputs" / "podcast-weekly").mkdir(parents=True)
    monkeypatch.setenv("DALTON_BACKEND_VAR_DIR", str(var_dir))

    async def _is_internal(uid: str) -> bool:
        return uid == internal_user

    fake_auth = create_autospec(_is_internal, instance=False, spec_set=True)
    fake_auth.side_effect = _is_internal
    monkeypatch.setattr(pwt, "is_internal_or_admin", fake_auth)
    return store, pwt, data_root


def _tool_config(user_id: str = "usr_internal") -> dict[str, Any]:
    return {"configurable": {"user_id": user_id, "thread_id": "thr_podcast_1", "workspace_id": "ws_1"}}


def _write_host_episode(
    store: Any,
    data_root: Path,
    video_id: str,
    *,
    date_prefix: str = "20261115",
    include_summary: bool = False,
    zh_body: str = "中文全文测试内容 [01:23]\n\n说话人 A 讨论要点。",
    en_body: str = "English transcript body [01:23]\n\nSpeaker A discussed key points.",
    merged_body: str = "[01:23] Merged raw transcript content.",
    notes_obj: dict[str, Any] | None = None,
) -> dict[str, Path]:
    out_dir = data_root / "research-outputs" / "podcast-weekly"
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    notes_data = notes_obj if notes_obj is not None else {
        "video_id": video_id,
        "title": f"Original Show {video_id}",
        "channel": "a16z",
        "upload_date": date_prefix.replace("-", ""),
        "amount_warnings": [],
        "speaker_flags": [],
        "short_blocks": [],
        "corrections": ["无须修改"],
        "unclear": [],
    }
    p_notes = out_dir / f"{date_prefix}-{video_id}.notes.json"
    p_zh = out_dir / f"{date_prefix}-{video_id}.zh.md"
    p_en = out_dir / f"{date_prefix}-{video_id}.en.md"
    p_merged = out_dir / f"{date_prefix}-{video_id}.merged.md"
    p_notes.write_text(json.dumps(notes_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    p_zh.write_text(zh_body, encoding="utf-8")
    p_en.write_text(en_body, encoding="utf-8")
    p_merged.write_text(merged_body, encoding="utf-8")
    paths.update({"notes": p_notes, "zh": p_zh, "en": p_en, "merged": p_merged})
    if include_summary:
        p_sum = out_dir / f"{date_prefix}-{video_id}.summary.md"
        p_sum.write_text("# 中文节目标题\n\n核心判断摘要内容。\n", encoding="utf-8")
        paths["summary"] = p_sum

    ledger = store.read_json_dict(store.ledger_path(data_root), required=False)
    ledger[video_id] = {
        "stage": "ready",
        "title": f"Original Show {video_id}",
        "channel": "a16z",
        "upload_date": date_prefix.replace("-", ""),
    }
    store.atomic_write_json(store.ledger_path(data_root), ledger)
    return paths


def _make_local_sandbox_backend(tmp_path: Path):
    """Build a SandboxBackend autospec backed by real temp dirs with separate root_dir vs workspace_dir."""
    import subprocess
    from types import SimpleNamespace

    from deepagents.backends.protocol import FileDownloadResponse, FileUploadResponse

    from src.ptc_agent.agent.backends.sandbox import SandboxBackend

    computer_root = tmp_path / "computer_root"
    workspace_dir = computer_root / "workspaces" / "ws_1"
    computer_root.mkdir(parents=True, exist_ok=True)
    workspace_dir.mkdir(parents=True, exist_ok=True)

    backend = create_autospec(SandboxBackend, instance=True)
    backend.root_dir = str(computer_root)
    backend.workspace_dir = str(workspace_dir)

    def _norm(p: str) -> str:
        rel = p.lstrip("/")
        if p.startswith(str(workspace_dir)):
            return p
        return str((workspace_dir / rel).as_posix())

    backend.normalize_path.side_effect = _norm
    executed_cmds: list[str] = []

    async def _exec(cmd: str, *, timeout: int | None = None):
        executed_cmds.append(cmd)
        proc = subprocess.run(
            ["bash", "-lc", cmd],
            cwd=str(computer_root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return SimpleNamespace(
            output=(proc.stdout or "") + (proc.stderr or ""),
            exit_code=proc.returncode,
            truncated=False,
        )

    async def _upload(files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        out: list[FileUploadResponse] = []
        for rel_p, data in files:
            target = workspace_dir / rel_p
            if not target.parent.is_dir():
                out.append(FileUploadResponse(path=rel_p, error="parent_missing"))
            else:
                target.write_bytes(data)
                out.append(FileUploadResponse(path=rel_p, error=None))
        return out

    async def _download(paths: list[str]) -> list[FileDownloadResponse]:
        out: list[FileDownloadResponse] = []
        for rel_p in paths:
            target = workspace_dir / rel_p
            if not target.exists():
                out.append(FileDownloadResponse(path=rel_p, content=None, error="file_not_found"))
            else:
                out.append(FileDownloadResponse(path=rel_p, content=target.read_bytes(), error=None))
        return out

    backend.aexecute.side_effect = _exec
    backend.aupload_files.side_effect = _upload
    backend.adownload_files.side_effect = _download
    return backend, computer_root, workspace_dir, executed_cmds


@pytest.mark.asyncio
async def test_stage_writes_four_files_to_sandbox_without_body_and_reports_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    from deepagents.backends.protocol import FileUploadResponse

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    host_files = _write_host_episode(store, data_root, "vid_stage_01")

    backend, computer_root, workspace_dir, executed_cmds = _make_local_sandbox_backend(tmp_path)

    # M1: Seed stale summary and leftover file from a previous run; stage must delete both.
    stale_dir = workspace_dir / ".work" / "podcast-weekly" / "vid_stage_01"
    stale_dir.mkdir(parents=True, exist_ok=True)
    stale_summary = stale_dir / "20261115-vid_stage_01.summary.md"
    stale_extra = stale_dir / "stale-old-run.txt"
    stale_summary.write_text("# stale summary from prior run\n", encoding="utf-8")
    stale_extra.write_text("leftover\n", encoding="utf-8")

    # M3: Fail the first aupload_files call to force mkdir -p retry at resolved workspace_dir (not computer_root).
    orig_upload = backend.aupload_files.side_effect
    upload_calls = 0

    async def _flaky_first_upload(files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        nonlocal upload_calls
        upload_calls += 1
        if upload_calls == 1:
            return [FileUploadResponse(path=p, error="parent_dir_missing") for p, _ in files]
        return await orig_upload(files)

    backend.aupload_files.side_effect = _flaky_first_upload
    read_tool, _ = pwt.create_podcast_weekly_tools(filesystem_backend=backend)

    raw_res = await read_tool.ainvoke(
        {"action": "stage", "video_id": "vid_stage_01"},
        config=_tool_config("usr_internal"),
    )
    res = json.loads(raw_res)
    assert res["ok"] is True
    assert res["sandbox_dir"] == ".work/podcast-weekly/vid_stage_01"
    assert sorted(res["cleaned_files"]) == ["20261115-vid_stage_01.summary.md", "stale-old-run.txt"]
    assert not stale_summary.exists()
    assert not stale_extra.exists()

    # Verify M3 mkdir retry targeted workspace_dir (never relative path inside computer_root)
    mkdir_cmds = [c for c in executed_cmds if "mkdir -p " in c]
    assert mkdir_cmds and str(workspace_dir) in mkdir_cmds[0]
    assert not (computer_root / ".work").exists()

    for art_key in ("notes", "zh", "en", "merged"):
        raw_bytes = host_files[art_key].read_bytes()
        rel_path = f".work/podcast-weekly/vid_stage_01/{host_files[art_key].name}"
        assert (workspace_dir / rel_path).read_bytes() == raw_bytes
        meta = res["files"][art_key]
        assert meta["path"] == rel_path
        assert meta["filename"] == host_files[art_key].name
        assert meta["bytes"] == len(raw_bytes)
        assert meta["sha256"] == hashlib.sha256(raw_bytes).hexdigest()

    # Body text must never leak into model conversation
    assert "中文全文测试内容" not in raw_res
    assert "English transcript body" not in raw_res
    assert "Merged raw transcript" not in raw_res

    # M2: Sandbox symlink on target file or episode directory must fail closed before upload
    backend.aupload_files.reset_mock()
    outside_secret = tmp_path / "outside_secret.txt"
    outside_secret.write_text("secret", encoding="utf-8")
    zh_target = stale_dir / host_files["zh"].name
    zh_target.unlink()
    os.symlink(outside_secret, zh_target)
    sym_res = json.loads(
        await read_tool.ainvoke(
            {"action": "stage", "video_id": "vid_stage_01"},
            config=_tool_config("usr_internal"),
        )
    )
    assert sym_res["ok"] is False
    assert sym_res["error"] == "symlink_rejected"
    assert backend.aupload_files.await_count == 0
    zh_target.unlink()

    # External user remains denied
    denied = json.loads(
        await read_tool.ainvoke(
            {"action": "stage", "video_id": "vid_stage_01"},
            config=_tool_config("usr_external"),
        )
    )
    assert denied["ok"] is False and denied["error"] == "not_allowed"

    # Missing host artifact + L2: symlinked host artifact goes to rejected_artifacts, missing goes to missing_artifacts
    host_files["en"].unlink()
    host_files["merged"].unlink()
    os.symlink(outside_secret, host_files["merged"])
    backend.aupload_files.reset_mock()
    missing_res = json.loads(
        await read_tool.ainvoke(
            {"action": "stage", "video_id": "vid_stage_01"},
            config=_tool_config("usr_internal"),
        )
    )
    assert missing_res["ok"] is False
    assert missing_res["missing_artifacts"] == ["en"]
    assert missing_res["rejected_artifacts"] == ["merged"]
    assert "20261115-vid_stage_01.en.md" in missing_res["message"]
    assert backend.aupload_files.await_count == 0


@pytest.mark.asyncio
async def test_collect_reads_from_sandbox_validates_writes_host_and_handles_partial_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hashlib

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    host_files = _write_host_episode(store, data_root, "vid_collect_01")

    new_zh = "校对后中文全文 [01:23]\n\n补充完整对话。\n"
    new_en = "Proofread English transcript [01:23]\n\nUpdated paragraph.\n"
    new_notes = (
        json.dumps(
            {
                "video_id": "vid_collect_01",
                "title": "Original Show vid_collect_01",
                "amount_warnings": [],
                "speaker_flags": [],
                "short_blocks": [],
                "corrections": ["核对金额无误"],
                "unclear": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    new_summary = "# 中文测试节目标题\n\n核心判断一句总结。\n\n**投资看点**\n- 看点条一，嘉宾自述，未独立核实 [01:23]\n"

    backend, _computer_root, workspace_dir, _cmds = _make_local_sandbox_backend(tmp_path)
    sb_ep_dir = workspace_dir / ".work" / "podcast-weekly" / "vid_collect_01"
    sb_ep_dir.mkdir(parents=True, exist_ok=True)
    sandbox_contents = {
        "20261115-vid_collect_01.zh.md": new_zh.encode("utf-8"),
        "20261115-vid_collect_01.en.md": new_en.encode("utf-8"),
        "20261115-vid_collect_01.notes.json": new_notes.encode("utf-8"),
        "20261115-vid_collect_01.summary.md": new_summary.encode("utf-8"),
    }
    for fname, raw_bytes in sandbox_contents.items():
        (sb_ep_dir / fname).write_bytes(raw_bytes)

    _, write_tool = pwt.create_podcast_weekly_tools(filesystem_backend=backend)

    raw_res = await write_tool.ainvoke(
        {
            "action": "collect",
            "video_id": "vid_collect_01",
            "artifacts": ["zh", "en", "notes", "summary"],
        },
        config=_tool_config("usr_internal"),
    )
    res = json.loads(raw_res)
    assert res["ok"] is True
    assert res["partial"] is False
    assert res["failed"] == {}
    for art_key, expected_text in (
        ("zh", new_zh),
        ("en", new_en),
        ("notes", new_notes),
        ("summary", new_summary),
    ):
        expected_bytes = expected_text.encode("utf-8")
        info = res["collected"][art_key]
        assert info["bytes"] == len(expected_bytes)
        assert info["sha256"] == hashlib.sha256(expected_bytes).hexdigest()
        host_written = (
            data_root / "research-outputs" / "podcast-weekly" / f"20261115-vid_collect_01{store.ARTIFACT_EXTENSION_MAP[art_key]}"
        )
        assert host_written.read_text(encoding="utf-8") == expected_text

    # No file body text returned
    assert "校对后中文全文" not in raw_res
    assert "Proofread English transcript" not in raw_res
    assert "核心判断一句总结" not in raw_res

    # M2 + Partial failure: summary becomes a symlink in sandbox + notes is invalid JSON; zh & en still save
    updated_zh_2 = "第二次校对中文 [02:00]\n"
    (sb_ep_dir / "20261115-vid_collect_01.zh.md").write_bytes(updated_zh_2.encode("utf-8"))
    (sb_ep_dir / "20261115-vid_collect_01.notes.json").write_bytes(b"{not valid json")
    outside_target = tmp_path / "outside.md"
    outside_target.write_text("# hijacked", encoding="utf-8")
    (sb_ep_dir / "20261115-vid_collect_01.summary.md").unlink()
    os.symlink(outside_target, sb_ep_dir / "20261115-vid_collect_01.summary.md")

    partial_res = json.loads(
        await write_tool.ainvoke(
            {"action": "collect", "video_id": "vid_collect_01"},
            config=_tool_config("usr_internal"),
        )
    )
    assert partial_res["ok"] is False
    assert partial_res["partial"] is True
    assert "zh" in partial_res["collected"] and "en" in partial_res["collected"]
    assert partial_res["failed"]["summary"]["error"] == "symlink_rejected"
    assert "notes" in partial_res["failed"]
    assert host_files["zh"].read_text(encoding="utf-8") == updated_zh_2
    assert json.loads(host_files["notes"].read_text(encoding="utf-8"))["corrections"] == ["核对金额无误"]

    # M4: Hyphenated host filenames (2026-11-15-) vs 8-digit row.upload_date (20261115), plus per-file target isolation
    hyph_files = _write_host_episode(store, data_root, "vid_hyph_01", date_prefix="2026-11-15")
    ledger_h = store.read_json_dict(store.ledger_path(data_root))
    ledger_h["vid_hyph_01"]["upload_date"] = "20261115"
    store.atomic_write_json(store.ledger_path(data_root), ledger_h)
    hyph_sb_dir = workspace_dir / ".work" / "podcast-weekly" / "vid_hyph_01"
    hyph_sb_dir.mkdir(parents=True, exist_ok=True)
    (hyph_sb_dir / "2026-11-15-vid_hyph_01.zh.md").write_text("连字符日期中文\n", encoding="utf-8")
    (hyph_sb_dir / "2026-11-15-vid_hyph_01.en.md").write_text("Hyphenated EN\n", encoding="utf-8")
    (hyph_sb_dir / "2026-11-15-vid_hyph_01.notes.json").write_text(hyph_files["notes"].read_text(encoding="utf-8"), encoding="utf-8")
    (hyph_sb_dir / "2026-11-15-vid_hyph_01.summary.md").write_text("# 标题\n\n摘要正文\n", encoding="utf-8")
    hyph_collect = json.loads(
        await write_tool.ainvoke(
            {"action": "collect", "video_id": "vid_hyph_01"},
            config=_tool_config("usr_internal"),
        )
    )
    assert hyph_collect["ok"] is True
    assert hyph_collect["collected"]["summary"]["filename"] == "2026-11-15-vid_hyph_01.summary.md"
    assert (data_root / "research-outputs" / "podcast-weekly" / "2026-11-15-vid_hyph_01.summary.md").exists()

    # M4 per-item exception tolerance in resolve_collect_targets: symlinked host summary fails alone while zh resolves
    (data_root / "research-outputs" / "podcast-weekly" / "2026-11-15-vid_hyph_01.summary.md").unlink()
    os.symlink(outside_target, data_root / "research-outputs" / "podcast-weekly" / "2026-11-15-vid_hyph_01.summary.md")
    targets_m4, invalid_m4 = store.resolve_collect_targets(data_root, "vid_hyph_01", ["zh", "summary"])
    assert targets_m4 == {"zh": "2026-11-15-vid_hyph_01.zh.md"}
    assert "summary" in invalid_m4


@pytest.mark.asyncio
async def test_publish_doc_multichunk_already_exists_resume_and_failure_states(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.dalton.tools import feishu_tools as ft

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)

    # 1) H1 + M5: Realistic bridge returns document_id WITHOUT `url`, plus sharing `warning`.
    # Derived `https://feishu.cn/docx/<id>` must promote the episode into `report_success` (NOT incomplete_docs).
    long_zh = "\n\n".join(
        f"第 {i} 段：" + ("这是中文测试段落内容，用于触发多段切分。" * 80)
        for i in range(1, 22)
    ) + "\n"
    _write_host_episode(store, data_root, "vid_pub_multi", include_summary=True, zh_body=long_zh)

    feishu_calls: list[tuple[str, dict[str, Any]]] = []

    async def _bridge_ok_no_url(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            return {
                "ok": True,
                "document_id": "dox_multi_1",
                "warning": "部分自动分享失败，请手动加权",
            }
        if route == "doc/append":
            return {"ok": True}
        raise AssertionError(f"Unexpected route: {route}")

    fake_bridge = create_autospec(ft.post_bridge_feishu, spec_set=True, side_effect=_bridge_ok_no_url)
    monkeypatch.setattr(ft, "post_bridge_feishu", fake_bridge)
    read_tool, write_tool = pwt.create_podcast_weekly_tools()

    raw_pub = await write_tool.ainvoke(
        {"action": "publish_doc", "video_id": "vid_pub_multi"},
        config=_tool_config("usr_internal"),
    )
    pub = json.loads(raw_pub)
    assert pub["ok"] is True
    assert pub["already_exists"] is False
    assert pub["document_id"] == "dox_multi_1"
    assert pub["doc_url"] == "https://feishu.cn/docx/dox_multi_1"
    assert pub["warning"] == "部分自动分享失败，请手动加权"
    assert pub["total_parts"] >= 2
    assert pub["appended_parts"] == list(range(2, pub["total_parts"] + 1))
    assert "这是中文测试段落内容" not in raw_pub
    assert feishu_calls[0][0] == "doc/create"
    assert feishu_calls[0][1]["folder_token"] == store.channels_folder_token()
    assert len(feishu_calls) == pub["total_parts"]
    doc_rec = store.read_json_dict(store.doc_record_path(data_root, "vid_pub_multi"))
    assert doc_rec["body_status"] == "complete"
    assert doc_rec["doc_url"] == "https://feishu.cn/docx/dox_multi_1"

    st_after_pub = json.loads(
        await read_tool.ainvoke(
            {"action": "status", "date": "2026-11-15"},
            config=_tool_config("usr_internal"),
        )
    )
    assert st_after_pub["incomplete_docs"] == []
    assert [it["video_id"] for it in st_after_pub["report_success"]] == ["vid_pub_multi"]

    # H1 wiki node_token variant (without explicit url) derives https://feishu.cn/wiki/<node_token>
    _write_host_episode(store, data_root, "vid_pub_wiki", include_summary=True)

    async def _bridge_wiki_node(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        return {"ok": True, "document_id": "dox_w1", "node_token": "wikcnNode99"}

    fake_bridge.side_effect = _bridge_wiki_node
    feishu_calls.clear()
    wiki_pub = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_wiki"},
            config=_tool_config("usr_internal"),
        )
    )
    assert wiki_pub["ok"] is True
    assert wiki_pub["doc_url"] == "https://feishu.cn/wiki/wikcnNode99"

    # Re-calling publish_doc hits already_exists with zero Feishu calls
    feishu_calls.clear()
    idem = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_multi"},
            config=_tool_config("usr_internal"),
        )
    )
    assert idem["ok"] is True
    assert idem["already_exists"] is True
    assert idem["document_id"] == "dox_multi_1"
    assert idem["total_parts"] == pub["total_parts"]
    assert idem["appended_parts"] == []
    assert feishu_calls == []

    # 2) resume_append appends ONLY remaining chunks (never doc/create)
    _write_host_episode(store, data_root, "vid_pub_resume", include_summary=True, zh_body=long_zh)
    prep_resume = store.prepare_doc(data_root, "vid_pub_resume", max_chars=25000)
    res_total = prep_resume["total_parts"]
    assert res_total >= 2
    store.save_doc(
        data_root,
        "vid_pub_resume",
        doc_url="https://feishu.cn/docx/dox_res_1",
        document_id="dox_res_1",
        zh_title="中文节目标题",
        total_parts=res_total,
        completed_parts=[1],
        body_complete=False,
    )
    fake_bridge.side_effect = _bridge_ok_no_url
    feishu_calls.clear()
    resumed = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_resume"},
            config=_tool_config("usr_internal"),
        )
    )
    assert resumed["ok"] is True
    assert resumed["already_exists"] is False
    assert resumed["appended_parts"] == list(range(2, res_total + 1))
    assert [r for r, _ in feishu_calls] == ["doc/append"] * (res_total - 1)
    assert store.read_json_dict(store.doc_record_path(data_root, "vid_pub_resume"))["body_status"] == "complete"

    # 3) Create fails without document_id -> 2 failures stop permanently
    _write_host_episode(store, data_root, "vid_pub_fail2", include_summary=True)

    async def _bridge_fail(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        return {"ok": False, "error": "bridge_502", "message": "upstream 502"}

    fake_bridge.side_effect = _bridge_fail
    feishu_calls.clear()
    f1 = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_fail2"},
            config=_tool_config("usr_internal"),
        )
    )
    assert f1["ok"] is False and f1["attempts"] == 1 and f1["stopped"] is False
    f2 = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_fail2"},
            config=_tool_config("usr_internal"),
        )
    )
    assert f2["ok"] is False and f2["attempts"] == 2 and f2["stopped"] is True
    assert f2["reason"] == "doc_create_failed_twice"
    assert len(feishu_calls) == 2

    feishu_calls.clear()
    f3 = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_fail2"},
            config=_tool_config("usr_internal"),
        )
    )
    assert f3["ok"] is False and f3["stopped"] is True and f3["reason"] == "doc_create_failed_twice"
    assert feishu_calls == []

    # 4) W1: content_insert_failed with document_id abandons doc, clears active doc.json, and recreates next run
    _write_host_episode(store, data_root, "vid_pub_cif", include_summary=True)

    async def _bridge_cif(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            return {
                "ok": False,
                "error": "content_insert_failed",
                "document_id": "dox_cif_1",
                "message": "first block insert timed out",
            }
        raise AssertionError(f"Should not immediately append after create content_insert_failed: {route}")

    fake_bridge.side_effect = _bridge_cif
    feishu_calls.clear()
    cif_res = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_cif"},
            config=_tool_config("usr_internal"),
        )
    )
    assert cif_res["ok"] is False
    assert cif_res["code"] == "doc_abandoned_uncertain"
    assert cif_res["error"] == "doc_abandoned_uncertain"
    assert cif_res["stopped"] is False
    assert cif_res["document_id"] == ""
    assert cif_res["doc_url"] == ""
    assert cif_res["appended_parts"] == []
    assert [d["document_id"] for d in cif_res["abandoned_documents"]] == ["dox_cif_1"]
    assert cif_res["abandoned_documents"][0]["doc_url"] == "https://feishu.cn/docx/dox_cif_1"
    assert [r for r, _ in feishu_calls] == ["doc/create"]
    cif_doc = store.read_json_dict(store.doc_record_path(data_root, "vid_pub_cif"))
    assert cif_doc["document_id"] == ""
    assert cif_doc["doc_url"] == ""
    assert cif_doc["completed_parts"] == []
    assert cif_doc["body_status"] == ""
    assert len(cif_doc["abandoned_documents"]) == 1
    assert not store.doc_failed_path(data_root, "vid_pub_cif").exists()

    # Status does NOT leave 1st-abandoned episode in incomplete_docs or report_failed; still in to_publish
    cif_status = store.get_status(data_root)
    assert all(it["video_id"] != "vid_pub_cif" for it in cif_status["incomplete_docs"])
    assert all(it["video_id"] != "vid_pub_cif" for it in cif_status["report_failed"])
    assert any(it["video_id"] == "vid_pub_cif" for it in cif_status["to_publish"])

    # Next run recreates brand-new document from scratch (NO doc/read)
    async def _bridge_cif_rebuild_ok(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            return {"ok": True, "document_id": "dox_cif_2"}
        if route == "doc/append":
            assert payload["token"] == "dox_cif_2"
            return {"ok": True, "document_id": "dox_cif_2"}
        raise AssertionError(f"Unexpected route: {route}")

    fake_bridge.side_effect = _bridge_cif_rebuild_ok
    feishu_calls.clear()
    cif_done = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_cif"},
            config=_tool_config("usr_internal"),
        )
    )
    assert cif_done["ok"] is True
    assert cif_done["document_id"] == "dox_cif_2"
    assert cif_done["doc_url"] == "https://feishu.cn/docx/dox_cif_2"
    assert feishu_calls[0][0] == "doc/create"
    assert [r for r, _ in feishu_calls[1:]] == ["doc/append"] * (cif_done["total_parts"] - 1)
    assert cif_done["appended_parts"] == list(range(2, cif_done["total_parts"] + 1))
    rebuilt_doc = store.read_json_dict(store.doc_record_path(data_root, "vid_pub_cif"))
    assert rebuilt_doc["body_status"] == "complete"
    assert len(rebuilt_doc["abandoned_documents"]) == 1

    # L1 + W4: deterministic tail chunk append failure preserves doc_id/progress and resumes remaining parts
    long_zh = "\n\n".join(
        f"第 {i} 段：用于检验尾部追加错误透传的长段落内容。" * 80 for i in range(1, 22)
    ) + "\n"
    _write_host_episode(store, data_root, "vid_pub_l1", include_summary=True, zh_body=long_zh)

    async def _bridge_tail_busy(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            return {"ok": True, "document_id": "dox_l1_1"}
        return {"ok": False, "error": "append_busy", "message": "still busy"}

    fake_bridge.side_effect = _bridge_tail_busy
    l1_res = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_l1"},
            config=_tool_config("usr_internal"),
        )
    )
    assert l1_res["ok"] is False and l1_res["error"] == "append_busy"
    assert l1_res["document_id"] == "dox_l1_1"
    l1_doc = store.read_json_dict(store.doc_record_path(data_root, "vid_pub_l1"))
    assert l1_doc["document_id"] == "dox_l1_1" and l1_doc["completed_parts"] == [1]

    feishu_calls.clear()

    async def _bridge_tail_resume_ok(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/append":
            return {"ok": True}
        raise AssertionError(f"Should only append remaining parts on deterministic resume: {route}")

    fake_bridge.side_effect = _bridge_tail_resume_ok
    l1_resumed = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_pub_l1"},
            config=_tool_config("usr_internal"),
        )
    )
    assert l1_resumed["ok"] is True
    assert l1_resumed["document_id"] == "dox_l1_1"
    assert l1_resumed["appended_parts"] == list(range(2, l1_resumed["total_parts"] + 1))
    assert all(r == "doc/append" for r, _ in feishu_calls)


@pytest.mark.asyncio
async def test_publish_doc_append_unknown_outcome_abandons_and_recreates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W1/W3/W4: outcome='unknown' during create append or resume append abandons doc, clears progress, and next publish_doc recreates from scratch without doc/read."""
    from src.dalton.feishu import FeishuBridgeError
    from src.dalton.tools import feishu_tools as ft

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    long_zh = "\n\n".join(
        f"第 {i} 段：标记片段 ALPHA-{i:02d} " + ("用于分段写入不确定时作废重建测试。" * 100)
        for i in range(1, 22)
    ) + "\n"
    _write_host_episode(store, data_root, "vid_unk_recreate", include_summary=True, zh_body=long_zh)
    _write_host_episode(store, data_root, "vid_resume_unk", include_summary=True, zh_body=long_zh)

    feishu_calls: list[tuple[str, dict[str, Any]]] = []
    fake_bridge = create_autospec(ft.post_bridge_feishu, spec_set=True)
    monkeypatch.setattr(ft, "post_bridge_feishu", fake_bridge)
    _, write_tool = pwt.create_podcast_weekly_tools()

    # Case A: create succeeds on dox_unk_1, part 2 append raises outcome="unknown" -> abandon & clear
    async def _bridge_unk_first(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            return {"ok": True, "document_id": "dox_unk_1"}
        if route == "doc/append":
            raise FeishuBridgeError(
                "content_insert_failed",
                "追加请求结果未知",
                payload={
                    "ok": False,
                    "error": "content_insert_failed",
                    "outcome": "unknown",
                    "detail": "timeout",
                    "document_id": "dox_unk_1",
                    "url": "https://feishu.cn/docx/dox_unk_1",
                },
            )
        raise AssertionError(f"Unexpected route (no doc/read allowed): {route}")

    fake_bridge.side_effect = _bridge_unk_first
    res_a1 = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_unk_recreate"},
            config=_tool_config("usr_internal"),
        )
    )
    assert res_a1["ok"] is False
    assert res_a1["code"] == "doc_abandoned_uncertain"
    assert res_a1["error"] == "doc_abandoned_uncertain"
    assert res_a1["stopped"] is False
    assert res_a1["document_id"] == ""
    assert res_a1["doc_url"] == ""
    assert [d["document_id"] for d in res_a1["abandoned_documents"]] == ["dox_unk_1"]
    doc_a1 = store.read_json_dict(store.doc_record_path(data_root, "vid_unk_recreate"))
    assert doc_a1["document_id"] == ""
    assert doc_a1["doc_url"] == ""
    assert doc_a1["completed_parts"] == []
    assert doc_a1["body_status"] == ""
    assert len(doc_a1["abandoned_documents"]) == 1
    assert [r for r, _ in feishu_calls] == ["doc/create", "doc/append"]

    # Next publish_doc creates fresh dox_unk_2 from scratch and completes all parts (NO doc/read)
    feishu_calls.clear()

    async def _bridge_rebuild_ok(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            return {"ok": True, "document_id": "dox_unk_2"}
        if route == "doc/append":
            assert payload["token"] == "dox_unk_2"
            return {"ok": True}
        raise AssertionError(f"Unexpected route: {route}")

    fake_bridge.side_effect = _bridge_rebuild_ok
    res_a2 = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_unk_recreate"},
            config=_tool_config("usr_internal"),
        )
    )
    assert res_a2["ok"] is True
    assert res_a2["document_id"] == "dox_unk_2"
    assert res_a2["doc_url"] == "https://feishu.cn/docx/dox_unk_2"
    total_parts = res_a2["total_parts"]
    assert res_a2["appended_parts"] == list(range(2, total_parts + 1))
    assert feishu_calls[0][0] == "doc/create"
    assert [r for r, _ in feishu_calls[1:]] == ["doc/append"] * (total_parts - 1)
    doc_a2 = store.read_json_dict(store.doc_record_path(data_root, "vid_unk_recreate"))
    assert doc_a2["body_status"] == "complete"
    assert [d["document_id"] for d in doc_a2["abandoned_documents"]] == ["dox_unk_1"]
    status_after = store.get_status(data_root)
    assert any(it["video_id"] == "vid_unk_recreate" for it in status_after["report_success"])

    # Case B: deterministic append failure keeps incomplete doc, then resume hits outcome="unknown" -> abandon -> 3rd run creates fresh doc
    async def _bridge_det_fail(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            return {"ok": True, "document_id": "dox_res_1"}
        return {"ok": False, "error": "rate_limited", "message": "try later"}

    fake_bridge.side_effect = _bridge_det_fail
    feishu_calls.clear()
    r_b1 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_resume_unk"}, config=_tool_config("usr_internal"))
    )
    assert r_b1["ok"] is False and r_b1["error"] == "rate_limited" and r_b1["document_id"] == "dox_res_1"

    async def _bridge_resume_unk(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/append":
            return {"ok": False, "error": "timeout", "outcome": "unknown", "message": "write unknown on resume"}
        raise AssertionError(f"Expected doc/append on deterministic resume, got {route}")

    fake_bridge.side_effect = _bridge_resume_unk
    feishu_calls.clear()
    r_b2 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_resume_unk"}, config=_tool_config("usr_internal"))
    )
    assert r_b2["ok"] is False and r_b2["code"] == "doc_abandoned_uncertain"
    assert [d["document_id"] for d in r_b2["abandoned_documents"]] == ["dox_res_1"]

    async def _bridge_rebuild_b_ok(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            return {"ok": True, "document_id": "dox_res_2"}
        if route == "doc/append":
            assert payload["token"] == "dox_res_2"
            return {"ok": True}
        raise AssertionError(f"Unexpected route: {route}")

    fake_bridge.side_effect = _bridge_rebuild_b_ok
    feishu_calls.clear()
    r_b3 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_resume_unk"}, config=_tool_config("usr_internal"))
    )
    assert r_b3["ok"] is True and r_b3["document_id"] == "dox_res_2"
    assert feishu_calls[0][0] == "doc/create"


@pytest.mark.asyncio
async def test_publish_doc_uncertain_twice_stops_and_enters_report_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W2: At most 1 rebuild per episode; second uncertain failure stops with doc_uncertain_twice and moves episode to report_failed."""
    from src.dalton.tools import feishu_tools as ft

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    long_zh = "\n\n".join(
        f"第 {i} 段：标记片段 TWICE-{i:02d} " + ("用于验证同一期连续两次不确定停止自动重建。" * 100)
        for i in range(1, 22)
    ) + "\n"
    _write_host_episode(store, data_root, "vid_twice_unk", include_summary=True, zh_body=long_zh)

    feishu_calls: list[tuple[str, dict[str, Any]]] = []
    fake_bridge = create_autospec(ft.post_bridge_feishu, spec_set=True)
    monkeypatch.setattr(ft, "post_bridge_feishu", fake_bridge)
    _, write_tool = pwt.create_podcast_weekly_tools()

    create_seq = 0

    async def _bridge_always_uncertain_append(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        nonlocal create_seq
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            create_seq += 1
            return {"ok": True, "document_id": f"dox_tw_{create_seq}"}
        if route == "doc/append":
            return {"ok": False, "error": "content_insert_failed", "outcome": "unknown", "message": "uncertain append"}
        raise AssertionError(f"Unexpected route: {route}")

    fake_bridge.side_effect = _bridge_always_uncertain_append

    res1 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_twice_unk"}, config=_tool_config("usr_internal"))
    )
    assert res1["ok"] is False
    assert res1["code"] == "doc_abandoned_uncertain"
    assert res1["stopped"] is False
    assert [d["document_id"] for d in res1["abandoned_documents"]] == ["dox_tw_1"]

    res2 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_twice_unk"}, config=_tool_config("usr_internal"))
    )
    assert res2["ok"] is False
    assert res2["code"] == "doc_uncertain_twice"
    assert res2["error"] == "doc_uncertain_twice"
    assert res2["stopped"] is True
    assert res2["document_id"] == ""
    assert [d["document_id"] for d in res2["abandoned_documents"]] == ["dox_tw_1", "dox_tw_2"]
    assert [d["doc_url"] for d in res2["abandoned_documents"]] == [
        "https://feishu.cn/docx/dox_tw_1",
        "https://feishu.cn/docx/dox_tw_2",
    ]
    assert create_seq == 2

    # 3rd publish_doc must NOT call Feishu at all
    feishu_calls.clear()
    res3 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_twice_unk"}, config=_tool_config("usr_internal"))
    )
    assert feishu_calls == []
    assert res3["ok"] is False
    assert res3["code"] == "doc_uncertain_twice"
    assert res3["error"] == "doc_uncertain_twice"
    assert res3["stopped"] is True
    assert [d["document_id"] for d in res3["abandoned_documents"]] == ["dox_tw_1", "dox_tw_2"]

    # Status removes episode from to_publish and classifies it under report_failed with abandoned_documents
    st = store.get_status(data_root)
    assert all(it["video_id"] != "vid_twice_unk" for it in st["to_publish"])
    assert all(it["video_id"] != "vid_twice_unk" for it in st["incomplete_docs"])
    failed_entry = next(it for it in st["report_failed"] if it["video_id"] == "vid_twice_unk")
    assert failed_entry["stage_error"] == "doc_uncertain_twice"
    assert [d["document_id"] for d in failed_entry["abandoned_documents"]] == ["dox_tw_1", "dox_tw_2"]


@pytest.mark.asyncio
async def test_feishu_post_send_errors_mark_outcome_unknown_vs_connect_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W1/W4: ReadTimeout/ReadError/RemoteProtocolError/invalid_json yield outcome='unknown' (abandons doc); ConnectError/ConnectTimeout keep deterministic failure/resume semantics."""
    import httpx

    from src.dalton import feishu as feishu_client_mod
    from src.dalton.feishu import FeishuBridgeError, post_bridge_feishu

    monkeypatch.setenv("DALTON_BRIDGE_BASE_URL", "http://bridge.invalid:9111")
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", "tok-test")
    real_client_cls = httpx.AsyncClient

    def _mock_transport(handler):
        def _factory(**kwargs: Any):
            return real_client_cls(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))

        monkeypatch.setattr(
            feishu_client_mod.httpx,
            "AsyncClient",
            create_autospec(real_client_cls, side_effect=_factory),
        )

    for exc_factory in (
        lambda req: httpx.ReadTimeout("read timeout", request=req),
        lambda req: httpx.ReadError("reset after write", request=req),
        lambda req: httpx.RemoteProtocolError("truncated response", request=req),
    ):
        _mock_transport(lambda req, fn=exc_factory: (_ for _ in ()).throw(fn(req)))
        with pytest.raises(FeishuBridgeError) as excinfo:
            await post_bridge_feishu("doc/append", {"token": "dox1", "markdown": "hi"})
        assert excinfo.value.error_code == "bridge_unreachable"
        assert excinfo.value.payload.get("outcome") == "unknown"

    _mock_transport(lambda req: httpx.Response(200, content=b"{not-json"))
    with pytest.raises(FeishuBridgeError) as exc_json:
        await post_bridge_feishu("doc/append", {"token": "dox1", "markdown": "hi"})
    assert exc_json.value.error_code == "invalid_json"
    assert exc_json.value.payload.get("outcome") == "unknown"

    for exc_factory in (
        lambda req: httpx.ConnectError("connection refused", request=req),
        lambda req: httpx.ConnectTimeout("connect timeout", request=req),
    ):
        _mock_transport(lambda req, fn=exc_factory: (_ for _ in ()).throw(fn(req)))
        with pytest.raises(FeishuBridgeError) as excinfo_conn:
            await post_bridge_feishu("doc/append", {"token": "dox1", "markdown": "hi"})
        assert excinfo_conn.value.error_code == "bridge_unreachable"
        assert "outcome" not in excinfo_conn.value.payload

    # End-to-end through publish_doc (Real _bridge + post_bridge_feishu -> httpx MockTransport)
    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    long_zh = "\n\n".join(
        f"第 {i} 段：用于检验 HTTP 超时与连接失败分支区分的文本内容。" * 45 for i in range(1, 20)
    ) + "\n"
    _write_host_episode(store, data_root, "vid_v3_timeout", include_summary=True, zh_body=long_zh)
    _write_host_episode(store, data_root, "vid_v3_conn", include_summary=True, zh_body=long_zh)
    _, write_tool = pwt.create_podcast_weekly_tools()

    def _handler_read_timeout(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/doc/create"):
            return httpx.Response(200, json={"ok": True, "document_id": "dox_v3_t"})
        raise httpx.ReadTimeout("bridge read timed out after request sent", request=req)

    _mock_transport(_handler_read_timeout)
    r_timeout = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_v3_timeout"}, config=_tool_config("usr_internal"))
    )
    assert r_timeout["ok"] is False and r_timeout["code"] == "doc_abandoned_uncertain"
    doc_timeout = store.read_json_dict(store.doc_record_path(data_root, "vid_v3_timeout"))
    assert doc_timeout["document_id"] == ""
    assert [d["document_id"] for d in doc_timeout["abandoned_documents"]] == ["dox_v3_t"]

    def _handler_connect_err(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/doc/create"):
            return httpx.Response(200, json={"ok": True, "document_id": "dox_v3_c"})
        raise httpx.ConnectError("connection refused before request sent", request=req)

    _mock_transport(_handler_connect_err)
    r_conn = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_v3_conn"}, config=_tool_config("usr_internal"))
    )
    assert r_conn["ok"] is False and r_conn.get("code") is None
    doc_conn = store.read_json_dict(store.doc_record_path(data_root, "vid_v3_conn"))
    assert doc_conn["document_id"] == "dox_v3_c" and doc_conn["completed_parts"] == [1]
    assert "abandoned_documents" not in doc_conn


@pytest.mark.asyncio
async def test_publish_doc_create_timeout_abandons_vs_connect_error_records_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """W1/W4: doc/create ReadTimeout (outcome='unknown', no ID) abandons & clears; ConnectError uses deterministic record_failure (2 attempts stop)."""
    from src.dalton.feishu import FeishuBridgeError
    from src.dalton.tools import feishu_tools as ft

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    _write_host_episode(store, data_root, "vid_create_rt", include_summary=True)
    _write_host_episode(store, data_root, "vid_create_ce", include_summary=True)

    fake_bridge = create_autospec(ft.post_bridge_feishu, spec_set=True)
    monkeypatch.setattr(ft, "post_bridge_feishu", fake_bridge)
    _, write_tool = pwt.create_podcast_weekly_tools()

    async def _bridge_create_timeout(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        raise FeishuBridgeError("bridge_unreachable", "ReadTimeout", payload={"outcome": "unknown"})

    fake_bridge.side_effect = _bridge_create_timeout
    r1 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_create_rt"}, config=_tool_config("usr_internal"))
    )
    assert r1["ok"] is False and r1["code"] == "doc_abandoned_uncertain"
    assert not store.doc_failed_path(data_root, "vid_create_rt").exists()

    async def _bridge_create_ok(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "document_id": "dox_rt_ok"}

    fake_bridge.side_effect = _bridge_create_ok
    r2 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_create_rt"}, config=_tool_config("usr_internal"))
    )
    assert r2["ok"] is True and r2["document_id"] == "dox_rt_ok"

    # ConnectError on doc/create without document_id or outcome='unknown' uses deterministic 2-attempt record_failure
    async def _bridge_create_conn(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        raise FeishuBridgeError("bridge_unreachable", "ConnectError")

    fake_bridge.side_effect = _bridge_create_conn
    ce1 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_create_ce"}, config=_tool_config("usr_internal"))
    )
    assert ce1["ok"] is False and ce1["stopped"] is False and ce1.get("code") is None
    ce2 = json.loads(
        await write_tool.ainvoke({"action": "publish_doc", "video_id": "vid_create_ce"}, config=_tool_config("usr_internal"))
    )
    assert ce2["ok"] is False and ce2["stopped"] is True and ce2["reason"] == "doc_create_failed_twice"


@pytest.mark.asyncio
async def test_publish_doc_node_token_only_multichunk_append(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V5: when doc/create returns only `node_token` (no `document_id` or `url`), multi-chunk appends send `node_token` rather than empty string."""
    from src.dalton.tools import feishu_tools as ft

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    long_zh = "\n\n".join(
        f"第 {i} 段：仅返回 node_token 时的多分块追加测试内容段落。" * 45 for i in range(1, 20)
    ) + "\n"
    _write_host_episode(store, data_root, "vid_v5_node_only", include_summary=True, zh_body=long_zh)

    feishu_calls: list[tuple[str, dict[str, Any]]] = []
    fake_bridge = create_autospec(ft.post_bridge_feishu, spec_set=True)
    monkeypatch.setattr(ft, "post_bridge_feishu", fake_bridge)
    _, write_tool = pwt.create_podcast_weekly_tools()

    async def _bridge_node_only(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        feishu_calls.append((route, dict(payload)))
        if route == "doc/create":
            return {"ok": True, "node_token": "wikicnOnly99"}
        if route == "doc/append":
            assert payload["token"] == "wikicnOnly99"
            return {"ok": True}
        raise AssertionError(f"Unexpected route: {route}")

    fake_bridge.side_effect = _bridge_node_only
    res = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_v5_node_only"},
            config=_tool_config("usr_internal"),
        )
    )
    assert res["ok"] is True
    assert res["doc_url"] == "https://feishu.cn/wiki/wikicnOnly99"
    assert res["total_parts"] >= 2
    assert res["appended_parts"] == list(range(2, res["total_parts"] + 1))
    append_calls = [p for r, p in feishu_calls if r == "doc/append"]
    assert len(append_calls) == res["total_parts"] - 1
    assert all(call["token"] == "wikicnOnly99" for call in append_calls)
    doc_rec = store.read_json_dict(store.doc_record_path(data_root, "vid_v5_node_only"))
    assert doc_rec["doc_url"] == "https://feishu.cn/wiki/wikicnOnly99"
    assert doc_rec["body_status"] == "complete"


@pytest.mark.asyncio
async def test_publish_doc_x1_feishu_network_error_is_uncertain_not_deterministic_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X1: HTTP 502 `feishu_network_error` without document_id or outcome is classified uncertain and triggers abandon+rebuild instead of `record_failure`."""
    # Verify raw bridge layer converts HTTP 502 `feishu_network_error` (without outcome) into `outcome="unknown"`.
    from typing import Self

    import httpx

    from src.dalton import feishu as feishu_mod
    from src.dalton.feishu import FeishuBridgeError
    from src.dalton.tools import feishu_tools as ft

    monkeypatch.setattr(feishu_mod, "get_dalton_bridge_base_url", lambda: "http://bridge.invalid")
    monkeypatch.setattr(feishu_mod, "get_dalton_bridge_admin_token", lambda: "tok_test")

    class _FakeAsyncClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                502,
                content=b'{"ok": false, "error": "feishu_network_error", "detail": "read timed out"}',
            )

    monkeypatch.setattr(feishu_mod.httpx, "AsyncClient", _FakeAsyncClient)
    with pytest.raises(FeishuBridgeError) as exc_info:
        await feishu_mod.post_bridge_feishu("doc/create", {"title": "T", "markdown": "M"})
    assert exc_info.value.error_code == "feishu_network_error"
    assert (exc_info.value.payload or {}).get("outcome") == "unknown"

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    _write_host_episode(store, data_root, "vid_x1_net", include_summary=True)
    _write_host_episode(store, data_root, "vid_x1_rejected", include_summary=True)

    fake_bridge = create_autospec(ft.post_bridge_feishu, spec_set=True)
    monkeypatch.setattr(ft, "post_bridge_feishu", fake_bridge)
    _, write_tool = pwt.create_podcast_weekly_tools()

    async def _bridge_502_net(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        raise FeishuBridgeError(
            "feishu_network_error",
            "HTTP 502 bad gateway",
            payload={"ok": False, "error": "feishu_network_error", "detail": "upstream read timeout"},
        )

    fake_bridge.side_effect = _bridge_502_net
    r1 = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x1_net"},
            config=_tool_config("usr_internal"),
        )
    )
    assert r1["ok"] is False
    assert r1["code"] == "doc_abandoned_uncertain"
    assert r1["stopped"] is False
    assert not store.doc_failed_path(data_root, "vid_x1_net").exists()
    doc_rec = store.read_json_dict(store.doc_record_path(data_root, "vid_x1_net"))
    assert len(doc_rec["abandoned_documents"]) == 1
    assert "in_flight_write" not in doc_rec

    # Next publish_doc rebuilds cleanly from scratch
    async def _bridge_ok(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "document_id": "dox_x1_rebuilt", "url": "https://feishu.cn/docx/dox_x1_rebuilt"}

    fake_bridge.side_effect = _bridge_ok
    r2 = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x1_net"},
            config=_tool_config("usr_internal"),
        )
    )
    assert r2["ok"] is True
    assert r2["document_id"] == "dox_x1_rebuilt"

    # Explicit rejection (`ConnectError` / `feishu_http_400`) remains a deterministic failure via `record_failure`
    async def _bridge_rejected(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        raise FeishuBridgeError(
            "feishu_http_400",
            "invalid request",
            payload={"ok": False, "error": "feishu_http_400", "outcome": "rejected"},
        )

    fake_bridge.side_effect = _bridge_rejected
    det1 = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x1_rejected"},
            config=_tool_config("usr_internal"),
        )
    )
    assert det1["ok"] is False
    assert det1.get("code") is None
    assert det1["stopped"] is False
    assert store.doc_failed_path(data_root, "vid_x1_rejected").exists()


@pytest.mark.asyncio
async def test_publish_doc_x2_image_bind_failed_records_chunk_and_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X2: `image_bind_failed` on create or append marks the body chunk completed, never resends it, and emits a manual image-fix warning."""
    from src.dalton.feishu import FeishuBridgeError
    from src.dalton.tools import feishu_tools as ft

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    long_zh = "\n\n".join(
        f"第 {i} 段：图片绑定失败时正文已落库不再重发验证。" * 45 for i in range(1, 35)
    ) + "\n"
    _write_host_episode(store, data_root, "vid_x2_append_img", include_summary=True, zh_body=long_zh)
    _write_host_episode(store, data_root, "vid_x2_create_img", include_summary=True, zh_body=long_zh)

    fake_bridge = create_autospec(ft.post_bridge_feishu, spec_set=True)
    monkeypatch.setattr(ft, "post_bridge_feishu", fake_bridge)
    _, write_tool = pwt.create_podcast_weekly_tools()

    append_calls: list[dict[str, Any]] = []

    async def _bridge_append_img_fail(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if route == "doc/create":
            return {"ok": True, "document_id": "dox_x2_app", "url": "https://feishu.cn/docx/dox_x2_app"}
        if route == "doc/append":
            append_calls.append(dict(payload))
            if len(append_calls) == 1:
                raise FeishuBridgeError(
                    "image_bind_failed",
                    "body appended, image failed",
                    payload={
                        "ok": False,
                        "error": "image_bind_failed",
                        "document_id": "dox_x2_app",
                        "url": "https://feishu.cn/docx/dox_x2_app",
                        "images": {"total": 1, "bound": 0, "failed": [{"ref": "chart_p2.png", "reason": "upload_failed"}]},
                    },
                )
            return {"ok": True, "document_id": "dox_x2_app"}
        raise AssertionError(f"Unexpected route: {route}")

    fake_bridge.side_effect = _bridge_append_img_fail
    res_app = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x2_append_img"},
            config=_tool_config("usr_internal"),
        )
    )
    assert res_app["ok"] is True
    assert res_app["document_id"] == "dox_x2_app"
    assert res_app["total_parts"] >= 2
    assert len(append_calls) == res_app["total_parts"] - 1
    assert res_app["appended_parts"] == list(range(2, res_app["total_parts"] + 1))
    assert "chart_p2.png" in str(res_app.get("warning") or "")
    assert "人工补图" in str(res_app.get("warning") or "")
    doc_app = store.read_json_dict(store.doc_record_path(data_root, "vid_x2_append_img"))
    assert doc_app["body_status"] == "complete"
    assert doc_app["completed_parts"] == list(range(1, res_app["total_parts"] + 1))

    # Also verify doc/create (part 1) `image_bind_failed` records part 1 and continues tail appends without re-creating
    create_calls = 0

    async def _bridge_create_img_fail(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        nonlocal create_calls
        if route == "doc/create":
            create_calls += 1
            raise FeishuBridgeError(
                "image_bind_failed",
                "doc created with body, cover image failed",
                payload={
                    "ok": False,
                    "error": "image_bind_failed",
                    "document_id": "dox_x2_cre",
                    "url": "https://feishu.cn/docx/dox_x2_cre",
                    "images": {"total": 1, "bound": 0, "failed": [{"ref": "cover_p1.png", "reason": "bind_timeout"}]},
                },
            )
        if route == "doc/append":
            return {"ok": True, "document_id": "dox_x2_cre"}
        raise AssertionError(f"Unexpected route: {route}")

    fake_bridge.side_effect = _bridge_create_img_fail
    res_cre = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x2_create_img"},
            config=_tool_config("usr_internal"),
        )
    )
    assert res_cre["ok"] is True
    assert create_calls == 1
    assert res_cre["document_id"] == "dox_x2_cre"
    assert "cover_p1.png" in str(res_cre.get("warning") or "")
    assert "人工补图" in str(res_cre.get("warning") or "")

    skill_text = (Path(__file__).resolve().parents[3] / "plugins/dalton/skills/dalton-podcast-weekly/SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "image_bind_failed" in skill_text
    assert "人工补图" in skill_text


@pytest.mark.asyncio
async def test_publish_doc_x3_interrupted_in_flight_write_abandons_and_rebuilds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X3: crashed write leaves `in_flight_write`; next `publish_doc` abandons it, counts toward rebuild cap, and rebuilds (or stops on 2nd)."""
    from src.dalton.tools import feishu_tools as ft

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    long_zh = "\n\n".join(
        f"第 {i} 段：请求发出后进程中断恢复测试段落。" * 45 for i in range(1, 35)
    ) + "\n"
    _write_host_episode(store, data_root, "vid_x3_crash", include_summary=True, zh_body=long_zh)
    _write_host_episode(store, data_root, "vid_x3_twice", include_summary=True, zh_body=long_zh)

    fake_bridge = create_autospec(ft.post_bridge_feishu, spec_set=True)
    monkeypatch.setattr(ft, "post_bridge_feishu", fake_bridge)
    _, write_tool = pwt.create_podcast_weekly_tools()

    async def _bridge_crash_append(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if route == "doc/create":
            return {"ok": True, "document_id": "dox_x3_old", "url": "https://feishu.cn/docx/dox_x3_old"}
        if route == "doc/append":
            raise RuntimeError("simulated worker SIGKILL")
        raise AssertionError(f"Unexpected route: {route}")

    fake_bridge.side_effect = _bridge_crash_append
    with pytest.raises(RuntimeError, match="simulated worker SIGKILL"):
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x3_crash"},
            config=_tool_config("usr_internal"),
        )

    crashed_rec = store.read_json_dict(store.doc_record_path(data_root, "vid_x3_crash"))
    assert crashed_rec["document_id"] == "dox_x3_old"
    assert crashed_rec["in_flight_write"]["operation"] == "append"
    assert crashed_rec["in_flight_write"]["part"] == 2
    assert crashed_rec["in_flight_write"]["document_id"] == "dox_x3_old"
    assert crashed_rec["in_flight_write"]["started_at"]

    # Recovery run abandons old doc, counts toward rebuild cap, and creates a new doc cleanly
    seen_routes: list[str] = []

    async def _bridge_rebuild_ok(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        seen_routes.append(route)
        return {"ok": True, "document_id": "dox_x3_new", "url": "https://feishu.cn/docx/dox_x3_new"}

    fake_bridge.side_effect = _bridge_rebuild_ok
    rebuilt = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x3_crash"},
            config=_tool_config("usr_internal"),
        )
    )
    assert rebuilt["ok"] is True
    assert rebuilt["document_id"] == "dox_x3_new"
    assert seen_routes[0] == "doc/create"
    final_rec = store.read_json_dict(store.doc_record_path(data_root, "vid_x3_crash"))
    assert "in_flight_write" not in final_rec
    assert len(final_rec["abandoned_documents"]) == 1
    assert final_rec["abandoned_documents"][0]["document_id"] == "dox_x3_old"
    assert "interrupted_in_flight:append:part_2" in final_rec["abandoned_documents"][0]["reason"]

    # If 1 abandoned doc already exists and a lingering in_flight_write is found again -> doc_uncertain_twice
    store.abandon_uncertain_doc(data_root, "vid_x3_twice", document_id="dox_prior", reason="prior_unknown")
    store.mark_in_flight_write(data_root, "vid_x3_twice", operation="create", part=1, document_id="", doc_url="")
    fake_bridge.reset_mock()
    stopped_res = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x3_twice"},
            config=_tool_config("usr_internal"),
        )
    )
    assert stopped_res["ok"] is False
    assert stopped_res["code"] == "doc_uncertain_twice"
    assert stopped_res["stopped"] is True
    assert fake_bridge.call_count == 0


@pytest.mark.asyncio
async def test_publish_doc_x4_per_episode_lock_and_doc_identity_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """X4: concurrent `publish_doc` for the same video_id returns `publish_in_progress` immediately, and identity mismatch aborts state writes."""
    import asyncio
    import contextlib
    import fcntl

    from src.dalton.tools import feishu_tools as ft

    store, pwt, data_root = _setup_podcast_tool_env(tmp_path, monkeypatch)
    _write_host_episode(store, data_root, "vid_x4_lock", include_summary=True)
    _write_host_episode(store, data_root, "vid_x4_other", include_summary=True)
    _write_host_episode(store, data_root, "vid_x4_mismatch", include_summary=True)

    entered_create = asyncio.Event()
    release_create = asyncio.Event()

    async def _slow_bridge(route: str, payload: dict[str, Any], user_id: str | None = None, **kwargs: Any) -> dict[str, Any]:
        if payload.get("title") and "vid_x4_other" not in str(payload.get("title")) and not entered_create.is_set():
            entered_create.set()
            await release_create.wait()
            return {"ok": True, "document_id": "dox_x4_1", "url": "https://feishu.cn/docx/dox_x4_1"}
        return {"ok": True, "document_id": "dox_x4_other", "url": "https://feishu.cn/docx/dox_x4_other"}

    fake_bridge = create_autospec(ft.post_bridge_feishu, spec_set=True, side_effect=_slow_bridge)
    monkeypatch.setattr(ft, "post_bridge_feishu", fake_bridge)
    _, write_tool = pwt.create_podcast_weekly_tools()

    first_task = asyncio.create_task(
        write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x4_lock"},
            config=_tool_config("usr_internal"),
        )
    )
    await entered_create.wait()

    # Contending same-episode publish returns immediately with publish_in_progress
    contend = json.loads(
        await asyncio.wait_for(
            write_tool.ainvoke(
                {"action": "publish_doc", "video_id": "vid_x4_lock"},
                config=_tool_config("usr_internal"),
            ),
            timeout=1.0,
        )
    )
    assert contend["ok"] is False
    assert contend["code"] == "publish_in_progress"

    # Unrelated episode is NOT blocked
    other_res = json.loads(
        await write_tool.ainvoke(
            {"action": "publish_doc", "video_id": "vid_x4_other"},
            config=_tool_config("usr_internal"),
        )
    )
    assert other_res["ok"] is True

    release_create.set()
    first_res = json.loads(await first_task)
    assert first_res["ok"] is True

    # Cross-process flock simulation via an externally held fd on publish.lock
    @contextlib.contextmanager
    def _hold_external_flock(path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a+", encoding="utf-8") as ext_fh:
            fcntl.flock(ext_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(ext_fh.fileno(), fcntl.LOCK_UN)

    lock_path = store.doc_publish_lock_path(data_root, "vid_x4_lock")
    with _hold_external_flock(lock_path):
        ext_contend = json.loads(
            await asyncio.wait_for(
                write_tool.ainvoke(
                    {"action": "publish_doc", "video_id": "vid_x4_lock"},
                    config=_tool_config("usr_internal"),
                ),
                timeout=1.0,
            )
        )
        assert ext_contend["ok"] is False
        assert ext_contend["code"] == "publish_in_progress"

    # Doc identity verification rejects mismatched save/record_chunk/abandon without overwriting disk
    store.save_doc(
        data_root,
        "vid_x4_mismatch",
        doc_url="https://feishu.cn/docx/dox_real",
        document_id="dox_real",
        total_parts=2,
        completed_parts=[1],
        body_complete=False,
    )
    bad_save = store.save_doc(
        data_root,
        "vid_x4_mismatch",
        doc_url="https://feishu.cn/docx/dox_other",
        document_id="dox_other",
        total_parts=2,
        completed_parts=[1],
        body_complete=False,
    )
    assert bad_save["ok"] is False and bad_save["code"] == "doc_identity_mismatch"

    bad_chunk = store.record_chunk(
        data_root,
        "vid_x4_mismatch",
        part=2,
        total_parts=2,
        document_id="dox_real",
        expected_document_id="dox_stale",
    )
    assert bad_chunk["ok"] is False and bad_chunk["code"] == "doc_identity_mismatch"

    bad_abandon = store.abandon_uncertain_doc(
        data_root,
        "vid_x4_mismatch",
        document_id="dox_other",
        reason="wrong_doc",
    )
    assert bad_abandon["ok"] is False and bad_abandon["code"] == "doc_identity_mismatch"

    preserved = store.read_json_dict(store.doc_record_path(data_root, "vid_x4_mismatch"))
    assert preserved["document_id"] == "dox_real"
    assert preserved["completed_parts"] == [1]
    assert preserved.get("abandoned_documents", []) == []





@pytest.mark.asyncio
async def test_stage_guard_tolerates_parallel_stage_creating_shared_dir(tmp_path: Path) -> None:
    """Two episodes staged in parallel both see `.work` missing; the loser of the mkdir race continues."""
    import shlex
    import subprocess
    from types import SimpleNamespace

    from src.dalton.tools.podcast_weekly_tools import _run_sandbox_guard
    from src.ptc_agent.agent.backends.sandbox import SandboxBackend

    ws = tmp_path / "ws"
    ws.mkdir()
    hook_dir = tmp_path / "hook"
    hook_dir.mkdir()
    # The other stage call wins the race: the directory appears just before our mkdir.
    (hook_dir / "sitecustomize.py").write_text(
        "import os\n"
        "_real = os.mkdir\n"
        "def _racing_mkdir(path, mode=0o777, *a, **k):\n"
        "    if str(path).endswith('.work'):\n"
        "        _real(path, mode)\n"
        "        raise FileExistsError(17, 'File exists', str(path))\n"
        "    return _real(path, mode, *a, **k)\n"
        "os.mkdir = _racing_mkdir\n",
        encoding="utf-8",
    )
    backend = create_autospec(SandboxBackend, instance=True)

    async def _exec(cmd: str, *, timeout: int | None = None):
        argv = shlex.split(cmd)
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=30,
            env={**os.environ, "PYTHONPATH": str(hook_dir)},
        )
        return SimpleNamespace(output=proc.stdout + proc.stderr, exit_code=proc.returncode, truncated=False)

    backend.aexecute.side_effect = _exec
    res = await _run_sandbox_guard(
        backend, mode="prepare_stage", ws_root=str(ws),
        rel_parts=[".work", "podcast-weekly", "vid1"], files=["a.zh.md"],
    )
    assert res.get("ok") is True, res
    assert (ws / ".work" / "podcast-weekly" / "vid1").is_dir()

    # A non-directory left at the path is still rejected after the race.
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    (hook_dir / "sitecustomize.py").write_text(
        "import os\n"
        "_real = os.mkdir\n"
        "def _racing_mkdir(path, mode=0o777, *a, **k):\n"
        "    if str(path).endswith('.work'):\n"
        "        open(path, 'w').close()\n"
        "        raise FileExistsError(17, 'File exists', str(path))\n"
        "    return _real(path, mode, *a, **k)\n"
        "os.mkdir = _racing_mkdir\n",
        encoding="utf-8",
    )
    res2 = await _run_sandbox_guard(
        backend, mode="prepare_stage", ws_root=str(ws2),
        rel_parts=[".work", "podcast-weekly", "vid1"], files=["a.zh.md"],
    )
    assert res2.get("ok") is False and res2.get("error") == "not_a_directory", res2
