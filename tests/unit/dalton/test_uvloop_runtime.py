"""Tests for direct uvloop enablement across main server.py and Dalton uvicorn services."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path
from unittest.mock import create_autospec

import pytest
import uvicorn

from src.dalton import settings as dalton_settings
from src.dalton.uvicorn_runtime import configure_uv_threadpool_size, default_uvicorn_loop

REPO_ROOT = Path(__file__).resolve().parents[3]


def _expected_threadpool_size() -> str:
    return str(min(32, (os.process_cpu_count() or 1) + 4))


def _assert_uvicorn_creates_uvloop(call_args: tuple, call_kwargs: dict) -> None:
    """Instantiate uvicorn.Config with the captured uvicorn.run arguments and assert uvloop."""
    uvloop = pytest.importorskip("uvloop")
    app = call_args[0] if call_args else call_kwargs["app"]
    loop_arg = call_kwargs.get("loop", "auto")
    cfg = uvicorn.Config(app, loop=loop_arg)
    factory = cfg.get_loop_factory()
    assert factory is not None
    loop = factory()
    try:
        assert isinstance(loop, uvloop.Loop)
    finally:
        loop.close()


def test_uvloop_off_switch_removed_from_settings() -> None:
    assert not hasattr(dalton_settings, "is_uvloop_enabled")
    assert not hasattr(dalton_settings, "DaltonUvloopConfig")
    assert "uvloop" not in dalton_settings.DaltonConfig.model_fields


@pytest.mark.skipif(sys.platform == "win32", reason="uvloop is POSIX-only")
def test_main_server_and_dalton_services_start_on_uvloop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    expected_pool = _expected_threadpool_size()
    real_uvicorn_run = uvicorn.run

    # 1. Main backend (server.py)
    monkeypatch.delenv("UV_THREADPOOL_SIZE", raising=False)
    monkeypatch.setattr(sys, "argv", ["server.py", "--host", "127.0.0.1", "--port", "8000"])
    mock_run = create_autospec(real_uvicorn_run)
    monkeypatch.setattr(uvicorn, "run", mock_run)

    runpy.run_path(str(REPO_ROOT / "server.py"), run_name="__main__")

    assert os.environ.get("UV_THREADPOOL_SIZE") == expected_pool
    mock_run.assert_called_once()
    assert "loop" not in mock_run.call_args.kwargs
    _assert_uvicorn_creates_uvloop(mock_run.call_args.args, mock_run.call_args.kwargs)

    # 2. crawl_service
    from src.dalton.crawl_service import __main__ as crawl_main

    monkeypatch.delenv("UV_THREADPOOL_SIZE", raising=False)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "test-token")
    mock_crawl_run = create_autospec(real_uvicorn_run)
    monkeypatch.setattr(crawl_main.uvicorn, "run", mock_crawl_run)

    crawl_main.main()

    assert os.environ.get("UV_THREADPOOL_SIZE") == expected_pool
    mock_crawl_run.assert_called_once()
    assert mock_crawl_run.call_args.kwargs["loop"] == "uvloop"
    _assert_uvicorn_creates_uvloop(mock_crawl_run.call_args.args, mock_crawl_run.call_args.kwargs)

    # 3. research_host
    from src.dalton.research_host import __main__ as rh_main

    monkeypatch.delenv("UV_THREADPOOL_SIZE", raising=False)
    monkeypatch.setenv("DALTON_RESEARCH_HOME", str(tmp_path / "rh-home"))
    monkeypatch.setenv("DALTON_WIKI_DATA_DIR", str(tmp_path / "rh-wiki"))
    mock_rh_run = create_autospec(real_uvicorn_run)
    monkeypatch.setattr(rh_main.uvicorn, "run", mock_rh_run)

    rc = rh_main.main(["--host", "127.0.0.1", "--port", "8951"])
    assert rc == 0
    assert os.environ.get("UV_THREADPOOL_SIZE") == expected_pool
    mock_rh_run.assert_called_once()
    assert mock_rh_run.call_args.kwargs["loop"] == "uvloop"
    _assert_uvicorn_creates_uvloop(mock_rh_run.call_args.args, mock_rh_run.call_args.kwargs)

    # 4. transcription_service
    from src.dalton.transcription_service import __main__ as tr_main

    monkeypatch.delenv("UV_THREADPOOL_SIZE", raising=False)
    monkeypatch.setenv("DALTON_TRANSCRIPTION_TOKEN", "test-tr-token")
    monkeypatch.setenv("DALTON_ASR_JOBS_DIR", str(tmp_path / "asr-jobs"))
    mock_tr_run = create_autospec(real_uvicorn_run)
    monkeypatch.setattr(tr_main.uvicorn, "run", mock_tr_run)

    tr_main.main()

    assert os.environ.get("UV_THREADPOOL_SIZE") == expected_pool
    mock_tr_run.assert_called_once()
    assert mock_tr_run.call_args.kwargs["loop"] == "uvloop"
    _assert_uvicorn_creates_uvloop(mock_tr_run.call_args.args, mock_tr_run.call_args.kwargs)


def test_configure_uv_threadpool_size_preserves_existing_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("UV_THREADPOOL_SIZE", "16")
    assert configure_uv_threadpool_size() == "16"
    assert os.environ["UV_THREADPOOL_SIZE"] == "16"
    assert default_uvicorn_loop() == ("uvloop" if sys.platform != "win32" else "asyncio")


@pytest.mark.asyncio
async def test_dalton_service_lifespans_call_keep_descriptors_from_children(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from src.dalton.crawl_service.app import create_app as create_crawl_app
    from src.dalton.research_host.app import ResearchHostConfig, create_app as create_rh_app
    from src.dalton.transcription_service.app import (
        TranscriptionServiceConfig,
        create_app as create_tr_app,
    )
    from src.server.utils import child_fds

    spy = create_autospec(child_fds.keep_descriptors_from_children)
    monkeypatch.setattr(child_fds, "keep_descriptors_from_children", spy)

    crawl_app = create_crawl_app(browser_concurrency=1)
    async with crawl_app.router.lifespan_context(crawl_app):
        pass

    rh_cfg = ResearchHostConfig(
        data_root=tmp_path / "wiki",
        mcp_token="tok",
        admin_token="adm",
    )
    rh_app = create_rh_app(rh_cfg)
    async with rh_app.router.lifespan_context(rh_app):
        pass

    tr_cfg = TranscriptionServiceConfig(
        jobs_dir=tmp_path / "jobs",
        token="tok",
        disabled=True,
    )
    tr_app = create_tr_app(config=tr_cfg)
    async with tr_app.router.lifespan_context(tr_app):
        pass

    assert spy.call_count == 3
