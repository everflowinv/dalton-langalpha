"""Admin output routes and the bounded embedding adapter.

Runners and host calls are substitutes. No test starts a service, embeds
with a provider, or reads a production credential.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import create_autospec

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DALTON = REPO_ROOT / "scripts" / "dalton"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402

BRIDGE_TOKEN = "bridge-mcp-token-secret"
ADMIN_TOKEN = "bridge-admin-token-secret"
HOST = "abcdef66-6666-4666-8666-666666666666"
BODY_HASH = "ab" * 32
MARKER = "MARKDOWN-BODY-勿截断"
SECRET_PATH = "/tmp/digest-output-secret-path"
SKILL = REPO_ROOT / "plugins" / "dalton" / "skills" / "dalton-market-digest" / "SKILL.md"
RULES = SKILL.parent / "references" / "report-rules.md"


class _Host:
    """Records host calls and returns a dict. It does not raise."""

    def __init__(self, **payload: Any) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.payload = payload or {"ok": True, "code": "ok", "markdown": MARKER}

    def get(self, config: Any, execution_id: str, period: str) -> dict[str, Any]:
        self.calls.append(("get", execution_id, period, config))
        body = dict(self.payload)
        body.setdefault("period", period)
        return body

    def save(self, config: Any, execution_id: str, period: str, markdown: str) -> dict[str, Any]:
        self.calls.append(("save", execution_id, period, markdown, config))
        body = dict(self.payload)
        if body.get("ok") is True:
            body.setdefault("markdown", markdown)
            body.setdefault("period", period)
        return body

    def finalize(
        self,
        config: Any,
        execution_id: str,
        period: str,
        body_sha256: str,
        embedder: Any,
    ) -> dict[str, Any]:
        self.calls.append(("finalize", execution_id, period, body_sha256, embedder, config))
        body = dict(self.payload)
        body.setdefault("period", period)
        return body


def _bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    state_root: Path | None = None,
    prepare_config: Path | None = None,
    output_config: Path | None = None,
    env_text: str = "",
) -> rb.ResearchDataBridge:
    env_file = tmp_path / "bridge.env"
    env_file.write_text(env_text, encoding="utf-8")
    monkeypatch.setenv("DALTON_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("DALTON_BRIDGE_USAGE_FILE", str(tmp_path / "usage.json"))
    monkeypatch.delenv("FEISHU_APP_ID", raising=False)
    monkeypatch.delenv("FEISHU_APP_SECRET", raising=False)
    monkeypatch.delenv("DALTON_BRIDGE_SUBPROCESS_PROXY", raising=False)
    if state_root is None:
        monkeypatch.delenv("DALTON_MARKET_DIGEST_STATE_ROOT", raising=False)
    else:
        monkeypatch.setenv("DALTON_MARKET_DIGEST_STATE_ROOT", str(state_root))
    if prepare_config is None:
        monkeypatch.delenv("DALTON_MARKET_DIGEST_PREPARE_CONFIG", raising=False)
    else:
        monkeypatch.setenv("DALTON_MARKET_DIGEST_PREPARE_CONFIG", str(prepare_config))
    if output_config is None:
        monkeypatch.delenv("DALTON_MARKET_DIGEST_OUTPUT_CONFIG", raising=False)
    else:
        monkeypatch.setenv("DALTON_MARKET_DIGEST_OUTPUT_CONFIG", str(output_config))
    return rb.ResearchDataBridge(rb.BridgeConfig())


def _install_configs(
    monkeypatch: pytest.MonkeyPatch,
    prepare_cfg: Any,
    output_cfg: Any,
    calls: list[Any],
) -> None:
    def load_prepare(path: Path) -> Any:
        calls.append(("prepare-config", path))
        return prepare_cfg

    def load_output(path: Path) -> Any:
        calls.append(("output-config", path))
        return output_cfg

    monkeypatch.setattr(rb, "_load_market_digest_prepare_config", load_prepare)
    monkeypatch.setattr(rb, "_load_market_digest_output_config", load_output)


def _ready_bridge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    host: _Host,
    *,
    prepare_root: Path | None = None,
    output_root: Path | None = None,
    env_root: Path | None = None,
    env_text: str = "",
) -> tuple[rb.ResearchDataBridge, list[Any]]:
    root = tmp_path / "state"
    root.mkdir(exist_ok=True)
    selected = env_root if env_root is not None else root
    prepare_cfg = SimpleNamespace(
        state_root=root if prepare_root is None else prepare_root,
        python_executable=Path(sys.executable),
        repo_root=REPO_ROOT,
    )
    output_cfg = SimpleNamespace(input_state_root=root if output_root is None else output_root)
    calls: list[Any] = []
    bridge = _bridge(
        monkeypatch,
        tmp_path,
        state_root=selected,
        prepare_config=tmp_path / "prepare.json",
        output_config=tmp_path / "output.json",
        env_text=env_text,
    )
    _install_configs(monkeypatch, prepare_cfg, output_cfg, calls)
    monkeypatch.setattr(rb, "_call_get_output", host.get)
    monkeypatch.setattr(rb, "_call_save_output", host.save)
    monkeypatch.setattr(rb, "_call_finalize_output", host.finalize)
    return bridge, calls


def _get_body(**extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"period": "AM", "automation_execution_id": HOST}
    body.update(extra)
    return body


def _save_body(markdown: str = MARKER) -> dict[str, Any]:
    return {**_get_body(), "markdown": markdown}


def _finalize_body() -> dict[str, Any]:
    return {**_get_body(), "body_sha256": BODY_HASH}


async def _drive(
    bridge: rb.ResearchDataBridge,
    *,
    path: str,
    body: dict[str, Any] | bytes = b"",
    token: str | None = ADMIN_TOKEN,
    method: str = "POST",
    content_length: int | None = None,
) -> tuple[int, dict[str, Any]]:
    raw_body = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
    length = len(raw_body) if content_length is None else content_length
    header_lines = ["Host: 127.0.0.1"]
    if token:
        header_lines.append(f"Authorization: Bearer {token}")
    head = (
        f"{method} {path} HTTP/1.1\r\n"
        + "\r\n".join(header_lines)
        + f"\r\nContent-Length: {length}\r\n\r\n"
    ).encode("utf-8")
    payload = b"" if content_length is not None and content_length > len(raw_body) else raw_body
    raw = head + payload
    reader = asyncio.StreamReader(limit=max(len(raw), 1024))
    reader.feed_data(raw)
    reader.feed_eof()
    writer = create_autospec(asyncio.StreamWriter, instance=True)
    await rb._handle_client(bridge, reader, writer)
    written = b"".join(call.args[0] for call in writer.write.call_args_list)
    status = int(written.split(b"\r\n", 1)[0].decode("utf-8").split(" ")[1])
    parsed = json.loads(written.split(b"\r\n\r\n", 1)[1].decode("utf-8"))
    return status, parsed


def _child(stdout: str, **overrides: Any) -> Any:
    from scripts.dalton.market_digest_inputs import ChildResult

    fields: dict[str, Any] = {
        "returncode": 0,
        "stdout": stdout,
        "stdout_truncated": False,
        "stderr_byte_count": 0,
        "stderr_truncated": False,
        "timed_out": False,
        "pid": 1,
        "still_running": False,
    }
    fields.update(overrides)
    return ChildResult(**fields)


def test_output_budgets_stay_narrow() -> None:
    assert rb.MAX_REQUEST_BYTES == 256 * 1024
    assert rb._MARKET_DIGEST_OUTPUT_SAVE_REQUEST_BYTES == 1024 * 1024
    assert rb._OUTPUT_RESPONSE_BYTES == 1024 * 1024
    assert rb._OUTPUT_FINALIZE_RESPONSE_BYTES == 128 * 1024
    assert rb._OUTPUT_BODY_BYTES == 128 * 1024
    assert rb._EMBED_CHILD_TIMEOUT_S == 75.0
    assert rb._EMBED_DEADLINE_S == 300.0
    assert rb._EMBED_BATCH_SIZE == 100
    doc = rb.ResearchDataBridge.handle_market_digest_output.__doc__ or ""
    assert "30" in doc and "360" in doc
    signature = inspect.signature(rb._embed_texts)
    assert signature.parameters["runner"].default is None
    assert signature.parameters["child_timeout_seconds"].default == 75.0


def test_skill_states_the_final_procedure() -> None:
    text = SKILL.read_text(encoding="utf-8")
    rules = RULES.read_text(encoding="utf-8")
    match = re.search(r'^description:\s*"(.*)"\s*$', text, re.M)
    assert match is not None
    assert len(match.group(1)) <= 120
    assert "name: dalton-market-digest" in text
    assert "SKIP: no new emails for <authoritative_date> <period>" in text
    assert "market_digest_output(action=\"submit\"" in text
    assert "report_delivery_confirmed" in text
    assert "run.sh" not in text and "feishu_send_card.py" not in text
    assert "# 📊 市场晨报 — <authoritative_date>" in rules
    assert "## 📝 来源" in rules
    assert "本轮此栏无新材料" in rules


def test_wrappers_call_the_integrated_host(monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.dalton.market_digest_outputs as outputs

    config = object()
    embedder = lambda texts, model: [[1.0]]  # noqa: E731
    get_output = create_autospec(outputs.get_output, return_value={"ok": True, "code": "ok"})
    save_output = create_autospec(outputs.save_output, return_value={"ok": True, "code": "saved"})
    finalize_output = create_autospec(
        outputs.finalize_output,
        return_value={"ok": True, "code": "finalized"},
    )
    monkeypatch.setattr(outputs, "get_output", get_output)
    monkeypatch.setattr(outputs, "save_output", save_output)
    monkeypatch.setattr(outputs, "finalize_output", finalize_output)
    rb._call_get_output(config, HOST, "AM")
    rb._call_save_output(config, HOST, "PM", "正文 /tmp/keep")
    rb._call_finalize_output(config, HOST, "AM", BODY_HASH, embedder)
    get_output.assert_called_once_with(config, HOST, "AM", include_body=True)
    save_output.assert_called_once_with(config, HOST, "PM", "正文 /tmp/keep")
    finalize_output.assert_called_once_with(config, HOST, "AM", BODY_HASH, embedder=embedder)


@pytest.mark.asyncio
async def test_output_routes_require_admin_and_exact_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = _Host()
    bridge, _loads = _ready_bridge(monkeypatch, tmp_path, host)
    status, missing = await _drive(
        bridge,
        path="/internal/market-digest/output/get",
        body=_get_body(),
        token=None,
    )
    assert status == 401 and missing == {"ok": False, "error": "Unauthorized"}
    status, forbidden = await _drive(
        bridge,
        path="/internal/market-digest/output/save",
        body=_save_body(),
        token=BRIDGE_TOKEN,
    )
    assert status == 403 and forbidden == {"ok": False, "error": "Forbidden"}
    status, wrong = await _drive(
        bridge,
        path="/internal/market-digest/output/finalize",
        body=_finalize_body(),
        token="other-token",
    )
    assert status == 401 and wrong["error"] == "Unauthorized"
    status, method = await _drive(
        bridge,
        path="/internal/market-digest/output/get",
        body=_get_body(),
        method="GET",
    )
    assert status == 405 and method["error"] == "Method Not Allowed"
    status, parsed = await _drive(
        bridge,
        path="/internal/market-digest/output/get",
        body=b"[1]",
    )
    assert status == 400 and parsed == {"ok": False, "error": "Parse error"}
    cases = [
        ("/internal/market-digest/output/get", _get_body(include_body=True), "unknown_field"),
        ("/internal/market-digest/output/save", _save_body() | {"confirmed": True}, "unknown_field"),
        ("/internal/market-digest/output/get", {"period": "AM"}, "invalid_request"),
        ("/internal/market-digest/output/get", _get_body(period="am"), "invalid_period"),
        ("/internal/market-digest/output/get", _get_body(period="AM "), "invalid_period"),
        (
            "/internal/market-digest/output/get",
            _get_body(automation_execution_id=HOST.upper()),
            "invalid_automation_execution",
        ),
        (
            "/internal/market-digest/output/finalize",
            {**_get_body(), "body_sha256": "AB" * 32},
            "hash_mismatch",
        ),
    ]
    for path, body, code in cases:
        status, payload = await _drive(bridge, path=path, body=body)
        assert status == 400 and payload["code"] == code, payload
        assert "_bridge_http" not in payload
    assert host.calls == []


@pytest.mark.asyncio
async def test_request_caps_differ_by_route(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = _Host()
    bridge, _loads = _ready_bridge(monkeypatch, tmp_path, host)
    save_limit = rb._MARKET_DIGEST_OUTPUT_SAVE_REQUEST_BYTES
    default_limit = rb.MAX_REQUEST_BYTES
    status, over = await _drive(
        bridge,
        path="/internal/market-digest/output/save",
        body=b"",
        content_length=save_limit + 1,
    )
    assert status == 413
    assert over == {"error": f"Request body exceeds {save_limit} bytes"}
    status, get_over = await _drive(
        bridge,
        path="/internal/market-digest/output/get",
        body=b"",
        content_length=default_limit + 1,
    )
    assert status == 413
    assert get_over == {"error": f"Request body exceeds {default_limit} bytes"}
    status, fin_over = await _drive(
        bridge,
        path="/internal/market-digest/output/finalize",
        body=b"",
        content_length=default_limit + 1,
    )
    assert status == 413
    assert fin_over == {"error": f"Request body exceeds {default_limit} bytes"}
    status, at_save = await _drive(
        bridge,
        path="/internal/market-digest/output/save",
        body=b" " * save_limit,
    )
    assert status == 400 and at_save["error"] == "Parse error"
    status, at_get = await _drive(
        bridge,
        path="/internal/market-digest/output/get",
        body=b" " * default_limit,
    )
    assert status == 400 and at_get["error"] == "Parse error"
    assert host.calls == []


@pytest.mark.asyncio
async def test_missing_or_bad_config_does_not_call_the_host(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    host = _Host()
    calls: list[Any] = []
    bridge = _bridge(monkeypatch, tmp_path)
    _install_configs(monkeypatch, SimpleNamespace(), SimpleNamespace(), calls)
    monkeypatch.setattr(rb, "_call_get_output", host.get)
    monkeypatch.setattr(rb, "_call_save_output", host.save)
    monkeypatch.setattr(rb, "_call_finalize_output", host.finalize)
    caplog.set_level(logging.INFO, logger="researchdata_bridge")
    caplog.clear()
    status, missing = await _drive(
        bridge,
        path="/internal/market-digest/output/get",
        body=_get_body(),
    )
    assert status == 503 and missing["code"] == "not_configured"
    assert missing["error"] == "Market-digest output is not configured."
    assert host.calls == [] and calls == []

    root = tmp_path / "state"
    root.mkdir()

    def explode(path: Path) -> Any:
        calls.append(path)
        raise RuntimeError("leak-/tmp/output-secret")

    monkeypatch.setattr(rb, "_load_market_digest_output_config", explode)
    monkeypatch.setattr(
        rb,
        "_load_market_digest_prepare_config",
        lambda path: SimpleNamespace(state_root=root, repo_root=REPO_ROOT, python_executable=Path(sys.executable)),
    )
    bridge = _bridge(
        monkeypatch,
        tmp_path,
        state_root=root,
        prepare_config=tmp_path / "prepare.json",
        output_config=tmp_path / "output.json",
    )
    caplog.clear()
    status, invalid = await _drive(bridge, path="/internal/market-digest/output/save", body=_save_body())
    assert status == 503 and invalid["code"] == "config_invalid"
    assert invalid["error"] == "Market-digest output config is invalid."
    assert "leak-/tmp/output-secret" not in json.dumps(invalid)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "RuntimeError" in logged
    assert "leak-/tmp/output-secret" not in logged
    assert host.calls == []

    monkeypatch.setattr(
        rb,
        "_load_market_digest_output_config",
        lambda path: SimpleNamespace(input_state_root=tmp_path / "other"),
    )
    status, mismatch = await _drive(bridge, path="/internal/market-digest/output/get", body=_get_body())
    assert status == 503 and mismatch["code"] == "config_invalid"
    assert mismatch["error"] == "Market-digest output config does not match the read state root."

    monkeypatch.setattr(
        rb,
        "_load_market_digest_prepare_config",
        lambda path: SimpleNamespace(state_root=1, repo_root=REPO_ROOT, python_executable=Path(sys.executable)),
    )
    monkeypatch.setattr(
        rb,
        "_load_market_digest_output_config",
        lambda path: SimpleNamespace(input_state_root=root),
    )
    status, unresolved = await _drive(
        bridge,
        path="/internal/market-digest/output/get",
        body=_get_body(),
    )
    assert status == 503 and unresolved["code"] == "config_invalid"
    assert unresolved["error"] == "Market-digest output config is invalid."
    assert host.calls == []


@pytest.mark.asyncio
async def test_get_and_save_keep_markdown_and_host_codes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    root = tmp_path / "state"
    linked = root / "child" / ".."
    markdown = f"见 {SECRET_PATH} {MARKER} {ADMIN_TOKEN}"
    host = _Host(
        ok=True,
        code="ok",
        markdown=markdown,
        report_run_id="r" * 201,
        date="2026-11-10",
        archive={"status": "saved", "code": "ok", "path": SECRET_PATH},
    )
    bridge, _loads = _ready_bridge(
        monkeypatch,
        tmp_path,
        host,
        prepare_root=linked,
        output_root=linked,
        env_root=root,
    )
    caplog.set_level(logging.INFO, logger="researchdata_bridge")
    caplog.clear()
    status, got = await _drive(bridge, path="/internal/market-digest/output/get", body=_get_body())
    assert status == 200
    assert got["ok"] is True and got["code"] == "ok"
    assert got["markdown"] == markdown
    assert "report_run_id" not in got
    assert "path" not in got["archive"]
    assert host.calls[0][0:3] == ("get", HOST, "AM")
    status, saved = await _drive(
        bridge,
        path="/internal/market-digest/output/save",
        body=_save_body(markdown),
    )
    assert status == 200 and saved["markdown"] == markdown
    assert host.calls[1][0:4] == ("save", HOST, "AM", markdown)
    conflict = _Host(ok=False, code="body_conflict")
    monkeypatch.setattr(rb, "_call_save_output", conflict.save)
    status, rejected = await _drive(
        bridge,
        path="/internal/market-digest/output/save",
        body=_save_body("other"),
    )
    assert status == 200 and rejected == {"ok": False, "code": "body_conflict"}
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert MARKER not in logged
    assert SECRET_PATH not in logged
    assert ADMIN_TOKEN not in logged
    assert str(root) not in logged


@pytest.mark.asyncio
async def test_unusable_markdown_and_oversize_metadata_stay_small(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    host = _Host(ok=True, code="ok", markdown=f"{MARKER}\x00tail")
    bridge, _loads = _ready_bridge(monkeypatch, tmp_path, host)
    status, bad = await _drive(bridge, path="/internal/market-digest/output/get", body=_get_body())
    assert status == 409 and bad["code"] == "body_invalid"
    assert MARKER not in json.dumps(bad)
    assert "\x00" not in json.dumps(bad)
    huge = _Host(ok=True, code="ok", markdown="x", archive=["y" * 200] * 6000)
    monkeypatch.setattr(rb, "_call_get_output", huge.get)
    status, bounded = await _drive(bridge, path="/internal/market-digest/output/get", body=_get_body())
    assert status == 409 and bounded["code"] == "response_exceeds_bound"
    assert "markdown" not in bounded
    assert len(json.dumps(bounded).encode("utf-8")) < 1000
    wide = _Host(
        ok=True,
        code="finalized",
        markdown="SHOULD-NOT-ECHO",
        archive=["z" * 200] * 800,
    )
    monkeypatch.setattr(rb, "_call_finalize_output", wide.finalize)
    monkeypatch.setattr(rb, "_make_market_digest_embedder", lambda cfg, prepare: (lambda texts, model: []))
    status, fin = await _drive(
        bridge,
        path="/internal/market-digest/output/finalize",
        body=_finalize_body(),
    )
    assert status == 409 and fin["code"] == "response_exceeds_bound"
    assert "SHOULD-NOT-ECHO" not in json.dumps(fin)


@pytest.mark.asyncio
async def test_finalize_receives_a_callable_and_hides_markdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seen: dict[str, Any] = {}

    def embedder(texts: list[str], model: str) -> list[list[float]]:
        seen["called"] = True
        return [[1.0]]

    host = _Host(
        ok=True,
        code="finalized",
        markdown="SHOULD-NOT-ECHO",
        embedding_status="done",
        archive={"status": "done", "code": "archived"},
        embedding={"status": "done", "code": "embedded"},
        index={"status": "done", "code": "indexed"},
    )
    bridge, _loads = _ready_bridge(
        monkeypatch,
        tmp_path,
        host,
        env_text="GEMINI_API_KEY=from-env-file\n",
    )
    monkeypatch.setattr(rb, "_make_market_digest_embedder", lambda cfg, prepare: embedder)
    status, payload = await _drive(
        bridge,
        path="/internal/market-digest/output/finalize",
        body=_finalize_body(),
    )
    assert status == 200
    assert payload["code"] == "finalized"
    assert payload["embedding_status"] == "done"
    assert "markdown" not in payload
    assert "SHOULD-NOT-ECHO" not in json.dumps(payload)
    assert "_bridge_http" not in payload
    kind, execution_id, period, digest, passed, _config = host.calls[0]
    assert (kind, execution_id, period, digest) == ("finalize", HOST, "AM", BODY_HASH)
    assert passed is embedder
    assert "called" not in seen

    missing = _Host(ok=True, code="finalized")
    monkeypatch.setattr(rb, "_call_finalize_output", missing.finalize)
    monkeypatch.setattr(
        rb,
        "_make_market_digest_embedder",
        lambda cfg, prepare: {
            "ok": False,
            "code": "embedding_not_configured",
            "error": "Market-digest embedding is not configured.",
            "_bridge_http": 503,
        },
    )
    status, absent = await _drive(
        bridge,
        path="/internal/market-digest/output/finalize",
        body=_finalize_body(),
    )
    assert status == 503 and absent["code"] == "embedding_not_configured"
    assert absent["error"] == "Market-digest embedding is not configured."
    assert "_bridge_http" not in absent
    assert missing.calls == []


@pytest.mark.asyncio
async def test_cancel_leaves_the_one_worker_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    calls: list[str] = []

    def get(config: Any, execution_id: str, period: str) -> dict[str, Any]:
        del config, period
        calls.append(execution_id)
        started.set()
        release.wait(10)
        try:
            return {"ok": True, "code": "ok", "markdown": MARKER}
        finally:
            finished.set()

    host = _Host()
    bridge, _loads = _ready_bridge(monkeypatch, tmp_path, host)
    monkeypatch.setattr(rb, "_call_get_output", get)
    task = asyncio.create_task(
        bridge.handle_market_digest_output("get", _get_body()),
    )
    try:
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await asyncio.to_thread(finished.wait, 5)
    finally:
        release.set()
    assert calls == [HOST]
    finished.clear()
    status, again = await bridge.handle_market_digest_output("get", _get_body())
    assert status == 200 and again["markdown"] == MARKER
    assert calls == [HOST, HOST]


def test_embedder_uses_the_env_file_key_and_one_script(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    env_file = tmp_path / "bridge.env"
    env_file.write_text('GEMINI_API_KEY="file-key-value"\nOPENAI_API_KEY=openai-secret\n', encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", "process-key-value")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    monkeypatch.setenv("DALTON_BRIDGE_TOKEN", BRIDGE_TOKEN)
    monkeypatch.setenv("HTTP_PROXY", "http://process-proxy.example")
    monkeypatch.setenv("MARKET_DIGEST_SENTINEL", "process-only")
    cfg = SimpleNamespace(env_path=env_file, subprocess_proxy="")
    built = rb._market_digest_embed_env(cfg, REPO_ROOT)
    assert built is not None
    assert built["GEMINI_API_KEY"] == "file-key-value"
    assert "process-key-value" not in built.values()
    assert "OPENAI_API_KEY" not in built
    assert "DALTON_BRIDGE_TOKEN" not in built
    assert "HTTP_PROXY" not in built
    assert "MARKET_DIGEST_SENTINEL" not in built
    cfg.subprocess_proxy = "http://127.0.0.1:7897"
    proxied = rb._market_digest_embed_env(cfg, REPO_ROOT)
    assert proxied is not None
    assert proxied["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert proxied["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert proxied["GEMINI_API_KEY"] == "file-key-value"
    blank = tmp_path / "blank.env"
    blank.write_text("GEMINI_API_KEY=\n", encoding="utf-8")
    blank_cfg = SimpleNamespace(env_path=blank, subprocess_proxy="")
    assert rb._market_digest_embed_env(blank_cfg, REPO_ROOT) is None
    prepare = SimpleNamespace(python_executable=Path(sys.executable), repo_root=REPO_ROOT)
    missing_key = rb._make_market_digest_embedder(blank_cfg, prepare)
    assert missing_key["code"] == "embedding_not_configured"
    assert missing_key["_bridge_http"] == 503

    seen: dict[str, Any] = {}

    def capture(texts: list[str], model: str, **kwargs: Any) -> list[list[float]]:
        seen["texts"] = texts
        seen["model"] = model
        seen.update(kwargs)
        return [[1.0]]

    monkeypatch.setattr(rb, "_embed_texts", capture)
    embedder = rb._make_market_digest_embedder(cfg, prepare)
    assert callable(embedder)
    assert embedder(["中文"], "gemini-embedding-001") == [[1.0]]
    assert seen["script_path"] == rb._market_digest_embed_script()
    assert seen["script_path"].name == "market_digest_embed.py"
    assert seen["python_executable"] == Path(sys.executable)
    assert seen["env"]["GEMINI_API_KEY"] == "file-key-value"
    assert "process-key-value" not in seen["env"].values()

    monkeypatch.setattr(rb, "_market_digest_embed_script", lambda: tmp_path / "missing-embed.py")
    missing_script = rb._make_market_digest_embedder(cfg, prepare)
    assert missing_script["code"] == "output_failed"
    assert missing_script["_bridge_http"] == 503

    def explode(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        raise RuntimeError("env-secret-value")

    monkeypatch.setattr(rb, "_market_digest_embed_script", lambda: Path(__file__).resolve())
    monkeypatch.setattr(rb.bridge_subprocess_env, "build_nonarchive_subprocess_env", explode)
    failed = rb._make_market_digest_embedder(cfg, prepare)
    assert failed["code"] == "output_failed"
    assert "env-secret-value" not in json.dumps(failed)


def test_embed_texts_batches_and_raises_without_a_partial() -> None:
    calls: list[dict[str, Any]] = []

    def runner(argv: list[str], **kwargs: Any) -> Any:
        calls.append({"argv": argv, **kwargs})
        batch = json.loads(kwargs["input_bytes"].decode("utf-8"))["texts"]
        previous = sum(
            len(json.loads(item["input_bytes"].decode("utf-8"))["texts"])
            for item in calls[:-1]
        )
        rows = [[float(previous + index)] for index, _text in enumerate(batch)]
        return _child(json.dumps({"ok": True, "embeddings": rows}))

    texts = [f"段{index}" for index in range(101)]
    rows = rb._embed_texts(
        texts,
        "gemini-embedding-001",
        python_executable=Path("/opt/dalton/python3"),
        script_path=Path("/opt/dalton/market_digest_embed.py"),
        env={"GEMINI_API_KEY": "file-key-value", "PATH": "/usr/bin"},
        runner=runner,
    )
    assert rows[0] == [0.0]
    assert rows[99] == [99.0]
    assert rows[100] == [100.0]
    assert len(rows) == 101
    assert [len(json.loads(item["input_bytes"].decode("utf-8"))["texts"]) for item in calls] == [100, 1]
    assert calls[0]["argv"] == ["/opt/dalton/python3", "/opt/dalton/market_digest_embed.py"]
    assert "段0" in calls[0]["input_bytes"].decode("utf-8")
    assert calls[0]["stdout_limit_bytes"] == 8 * 1024 * 1024
    assert calls[0]["stderr_limit_bytes"] == 0
    assert calls[0]["env"] == {"GEMINI_API_KEY": "file-key-value", "PATH": "/usr/bin"}
    assert "段" not in "".join(calls[0]["argv"])

    ticks = iter([10.0, 10.0, 270.0])
    timeouts: list[float] = []

    def timed(argv: list[str], **kwargs: Any) -> Any:
        del argv
        timeouts.append(kwargs["timeout_seconds"])
        batch = json.loads(kwargs["input_bytes"].decode("utf-8"))["texts"]
        return _child(json.dumps({"ok": True, "embeddings": [[1.0] for _item in batch]}))

    rb._embed_texts(
        ["a"] * 101,
        "gemini-embedding-001",
        python_executable=Path("/opt/dalton/python3"),
        script_path=Path("/opt/dalton/market_digest_embed.py"),
        env={"PATH": "/usr/bin"},
        runner=timed,
        now=lambda: next(ticks),
    )
    assert timeouts == [75.0, 40.0]

    stopped: list[int] = []

    def once(argv: list[str], **kwargs: Any) -> Any:
        del argv
        stopped.append(1)
        batch = json.loads(kwargs["input_bytes"].decode("utf-8"))["texts"]
        return _child(json.dumps({"ok": True, "embeddings": [[1.0] for _item in batch]}))

    clock = iter([0.0, 0.0, 300.0])
    with pytest.raises(rb._EmbedError, match="embed_failed"):
        rb._embed_texts(
            ["a"] * 101,
            "gemini-embedding-001",
            python_executable=Path("/opt/dalton/python3"),
            script_path=Path("/opt/dalton/market_digest_embed.py"),
            env={"PATH": "/usr/bin"},
            runner=once,
            now=lambda: next(clock),
        )
    assert stopped == [1]


@pytest.mark.parametrize(
    "stdout, overrides",
    [
        ('{"ok": true, "embeddings": [[1.0]]}', {"returncode": 1}),
        ('{"ok": true, "embeddings": [[1.0]]}', {"timed_out": True}),
        ('{"ok": true, "embeddings": [[1.0]]}', {"still_running": True}),
        ('{"ok": true, "embeddings": [[1.0]]}', {"stdout_truncated": True}),
        ('{"ok": true, "embeddings": [[NaN]]}', {}),
        ('{"ok": true, "embeddings": [[1.0], [2.0]]}', {}),
        ('{"ok": false, "code": "embed_failed"}', {}),
        ('{"ok": true, "embeddings": ["nope"]}', {}),
    ],
)
def test_embed_texts_rejects_a_bad_child(stdout: str, overrides: dict[str, Any]) -> None:
    def runner(argv: list[str], **kwargs: Any) -> Any:
        del argv, kwargs
        return _child(stdout, **overrides)

    with pytest.raises(rb._EmbedError, match="^embed_failed$"):
        rb._embed_texts(
            ["only"],
            "gemini-embedding-001",
            python_executable=Path("/opt/dalton/python3"),
            script_path=Path("/opt/dalton/market_digest_embed.py"),
            env={"PATH": "/usr/bin"},
            runner=runner,
        )


def test_embed_texts_ignores_discarded_stderr_and_checks_the_request() -> None:
    def runner(argv: list[str], **kwargs: Any) -> Any:
        del argv, kwargs
        return _child(
            '{"ok": true, "embeddings": [[1.0, 2.0]]}',
            stderr_truncated=True,
            stderr_byte_count=40,
        )

    assert rb._embed_texts(
        ["only"],
        "gemini-embedding-001",
        python_executable=Path("/opt/dalton/python3"),
        script_path=Path("/opt/dalton/market_digest_embed.py"),
        env={"PATH": "/usr/bin"},
        runner=runner,
    ) == [[1.0, 2.0]]
    calls: list[int] = []

    def unused(argv: list[str], **kwargs: Any) -> Any:
        del argv, kwargs
        calls.append(1)
        return _child("{}")

    for texts, model in (([], "gemini-embedding-001"), (["ok"], "bad..model"), (["a\x00b"], "gemini-embedding-001")):
        with pytest.raises(rb._EmbedError):
            rb._embed_texts(
                texts,
                model,
                python_executable=Path("/opt/dalton/python3"),
                script_path=Path("/opt/dalton/market_digest_embed.py"),
                env={"PATH": "/usr/bin"},
                runner=unused,
            )
    assert calls == []
