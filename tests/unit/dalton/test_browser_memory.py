"""Unit tests for the Backlog P6a 3-way browser memory attribution sampler."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from unittest.mock import create_autospec

from fastapi import HTTPException
import pytest

from src.dalton.app.router import get_browser_memory_diagnostics_endpoint
from src.dalton.settings import get_dalton_config
from src.observability.browser_memory import (
    BrowserMemorySampler,
    ConcurrencySnapshot,
    capture_browser_launch,
    clear_crawler_pids,
    clear_pdf_render_pids,
    collect_browser_memory_sample,
    get_registered_crawler_pids,
    get_registered_pdf_render_pids,
    read_cgroup_memory,
    read_live_concurrency,
    register_crawler_pids,
    register_pdf_render_pids,
    track_crawler_session,
)
from src.server.services import pdf_render as pdf_render_mod
from src.tools.web.inhouse import safe_wrapper as safe_wrapper_mod
from src.tools.web.inhouse.scrapling_crawler import ScraplingCrawler

_MIB = 1024 * 1024


def _write_proc(
    proc_root: Path,
    *,
    pid: int,
    ppid: int,
    comm: str,
    cmdline: list[str],
    pss_kb: int | None = None,
    vmrss_kb: int | None = None,
    statm_resident_pages: int | None = None,
) -> Path:
    """Create a synthetic `/proc/<pid>` directory."""
    pid_dir = proc_root / str(pid)
    pid_dir.mkdir(parents=True, exist_ok=True)
    (pid_dir / "comm").write_text(f"{comm}\n", encoding="utf-8")
    # /proc/<pid>/stat: pid (comm) state ppid pgrp session tty_nr tpgid ...
    filler = " ".join(["0"] * 30)
    (pid_dir / "stat").write_text(
        f"{pid} ({comm}) S {ppid} {filler}\n",
        encoding="utf-8",
    )
    raw_cmd = b"\x00".join(part.encode("utf-8") for part in cmdline) + b"\x00"
    (pid_dir / "cmdline").write_bytes(raw_cmd)

    status_lines = [
        f"Name:\t{comm}",
        f"PPid:\t{ppid}",
    ]
    if vmrss_kb is not None:
        status_lines.append(f"VmRSS:\t{vmrss_kb} kB")
    (pid_dir / "status").write_text("\n".join(status_lines) + "\n", encoding="utf-8")

    if pss_kb is not None:
        rss_val = vmrss_kb if vmrss_kb is not None else pss_kb
        (pid_dir / "smaps_rollup").write_text(
            "55b4a2c00000-7fffd9b9a000 ---p 00000000 00:00 0 [rollup]\n"
            f"Rss:               {rss_val} kB\n"
            f"Pss:               {pss_kb} kB\n",
            encoding="utf-8",
        )

    if statm_resident_pages is not None:
        (pid_dir / "statm").write_text(
            f"{statm_resident_pages * 2} {statm_resident_pages} 0 0 0 0 0\n",
            encoding="utf-8",
        )
    return pid_dir


def test_three_way_attribution_by_ppid_and_cmdline(tmp_path: Path) -> None:
    """Crawler tree, PDF render tree, and backend/other processes are attributed accurately."""
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()

    # cgroup v2: 1.8 GiB / 4.0 GiB
    (cgroup_root / "memory.current").write_text(str(1800 * _MIB), encoding="utf-8")
    (cgroup_root / "memory.max").write_text(str(4096 * _MIB), encoding="utf-8")

    # 1. Other: tini (PID 1), backend Python (PID 100), heavy_compute worker (PID 150)
    _write_proc(
        proc_root,
        pid=1,
        ppid=0,
        comm="tini",
        cmdline=["/usr/bin/tini", "-s", "--", "python", "server.py"],
        pss_kb=2 * 1024,
    )
    _write_proc(
        proc_root,
        pid=100,
        ppid=1,
        comm="python",
        cmdline=["python", "server.py", "--port", "8000"],
        pss_kb=700 * 1024,
    )
    _write_proc(
        proc_root,
        pid=150,
        ppid=100,
        comm="python",
        cmdline=["python", "-c", "from src.dalton.heavy_compute import _worker"],
        pss_kb=48 * 1024,
    )

    # 2. Crawler browser tree: Playwright driver (200) -> Chromium (201) -> zygote (202) -> renderer (203),
    # plus Camoufox (210) and an orphaned Chromium renderer reparented to PID 1 (211).
    _write_proc(
        proc_root,
        pid=200,
        ppid=100,
        comm="node",
        cmdline=["node", "/opt/playwright/driver/package/cli.js", "run-driver"],
        pss_kb=40 * 1024,
    )
    _write_proc(
        proc_root,
        pid=201,
        ppid=200,
        comm="chrome",
        cmdline=[
            "/ms-playwright/chromium/chrome-linux/chrome",
            "--headless",
            "--no-pings",
            "--homepage=about:blank",
            "--user-data-dir=/tmp/scrapling-ctx",
        ],
        pss_kb=160 * 1024,
    )
    _write_proc(
        proc_root,
        pid=202,
        ppid=201,
        comm="chrome",
        cmdline=["/ms-playwright/chromium/chrome-linux/chrome", "--type=zygote"],
        pss_kb=30 * 1024,
    )
    _write_proc(
        proc_root,
        pid=203,
        ppid=202,
        comm="chrome",
        cmdline=["/ms-playwright/chromium/chrome-linux/chrome", "--type=renderer"],
        pss_kb=220 * 1024,
    )
    _write_proc(
        proc_root,
        pid=210,
        ppid=100,
        comm="camoufox",
        cmdline=["/opt/camoufox/camoufox", "--headless"],
        pss_kb=150 * 1024,
    )
    _write_proc(
        proc_root,
        pid=211,
        ppid=1,
        comm="chrome",
        cmdline=["/opt/patchright/chromium/chrome-linux/chrome", "--type=renderer"],
        pss_kb=50 * 1024,
    )

    # 3. PDF render browser tree: Playwright driver (300, seeded via pdf_root_pids) ->
    # Chromium main (301) -> crashpad (302) + renderer (303).
    _write_proc(
        proc_root,
        pid=300,
        ppid=100,
        comm="node",
        cmdline=["node", "/opt/playwright/driver/package/cli.js", "run-driver"],
        pss_kb=35 * 1024,
    )
    _write_proc(
        proc_root,
        pid=301,
        ppid=300,
        comm="chrome",
        cmdline=["/ms-playwright/chromium/chrome-linux/chrome", "--headless"],
        pss_kb=120 * 1024,
    )
    _write_proc(
        proc_root,
        pid=302,
        ppid=301,
        comm="chrome_crashpad",
        cmdline=["/ms-playwright/chromium/chrome-linux/chrome_crashpad_handler"],
        pss_kb=5 * 1024,
    )
    _write_proc(
        proc_root,
        pid=303,
        ppid=301,
        comm="chrome",
        cmdline=["/ms-playwright/chromium/chrome-linux/chrome", "--type=renderer"],
        pss_kb=140 * 1024,
    )

    sample = collect_browser_memory_sample(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        backend_pid=100,
        pdf_root_pids={300},
        concurrency=ConcurrencySnapshot(
            crawler_browser_active=2,
            crawler_browser_limit=6,
            crawler_queue_active=3,
            pdf_render_active=1,
            pdf_render_limit=2,
            pdf_driver_pid=300,
        ),
    )

    assert sample.metric == "pss"
    assert sample.rss_fallback_procs == 0

    assert sample.crawler.procs == 6
    assert sample.crawler.pids == (200, 201, 202, 203, 210, 211)
    assert sample.crawler.bytes == (40 + 160 + 30 + 220 + 150 + 50) * _MIB
    assert sample.crawler.mb == 650.0

    assert sample.pdf_render.procs == 4
    assert sample.pdf_render.pids == (300, 301, 302, 303)
    assert sample.pdf_render.bytes == (35 + 120 + 5 + 140) * _MIB
    assert sample.pdf_render.mb == 300.0

    assert sample.other.procs == 3
    assert sample.other.pids == (1, 100, 150)
    assert sample.other.bytes == (2 + 700 + 48) * _MIB
    assert sample.other.mb == 750.0

    assert sample.total_procs == 13
    assert sample.total_proc_bytes == (650 + 300 + 750) * _MIB


def test_pss_missing_falls_back_to_rss(tmp_path: Path) -> None:
    """When smaps_rollup is absent, the sampler falls back to VmRSS in status (or statm)."""
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()

    # No smaps_rollup; VmRSS in /proc/<pid>/status
    _write_proc(
        proc_root,
        pid=100,
        ppid=1,
        comm="python",
        cmdline=["python", "server.py"],
        pss_kb=None,
        vmrss_kb=512 * 1024,
    )
    _write_proc(
        proc_root,
        pid=201,
        ppid=100,
        comm="chrome",
        cmdline=["chrome", "--headless", "--no-pings"],
        pss_kb=None,
        vmrss_kb=256 * 1024,
    )
    # Process with neither smaps_rollup nor VmRSS, falling back to statm resident pages
    _write_proc(
        proc_root,
        pid=301,
        ppid=100,
        comm="chrome",
        cmdline=["chrome", "--headless", "--pdf_render"],
        pss_kb=None,
        vmrss_kb=None,
        statm_resident_pages=1000,
    )

    sample = collect_browser_memory_sample(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        backend_pid=100,
    )

    assert sample.metric == "rss"
    assert sample.rss_fallback_procs == 3
    assert sample.other.bytes == 512 * _MIB
    assert sample.crawler.bytes == 256 * _MIB
    assert sample.pdf_render.bytes > 0


def test_cgroup_v2_and_v1_parsing(tmp_path: Path) -> None:
    """Reads cgroup v2 (`memory.current`/`memory.max`) and falls back to cgroup v1."""
    # 1. cgroup v2 with concrete limit
    cg_v2 = tmp_path / "cgroup_v2"
    cg_v2.mkdir()
    (cg_v2 / "memory.current").write_text(str(3 * 1024 * _MIB) + "\n", encoding="utf-8")
    (cg_v2 / "memory.max").write_text(str(4 * 1024 * _MIB) + "\n", encoding="utf-8")

    m_v2 = read_cgroup_memory(cg_v2)
    assert m_v2.version == "v2"
    assert m_v2.usage_bytes == 3 * 1024 * _MIB
    assert m_v2.limit_bytes == 4 * 1024 * _MIB
    assert m_v2.usage_ratio == pytest.approx(0.75)

    # 2. cgroup v2 with "max" (unlimited)
    cg_v2_max = tmp_path / "cgroup_v2_max"
    cg_v2_max.mkdir()
    (cg_v2_max / "memory.current").write_text(str(500 * _MIB) + "\n", encoding="utf-8")
    (cg_v2_max / "memory.max").write_text("max\n", encoding="utf-8")

    m_v2_max = read_cgroup_memory(cg_v2_max)
    assert m_v2_max.version == "v2"
    assert m_v2_max.usage_bytes == 500 * _MIB
    assert m_v2_max.limit_bytes is None
    assert m_v2_max.usage_ratio is None

    # 3. cgroup v1 under memory/ subdirectory
    cg_v1 = tmp_path / "cgroup_v1"
    (cg_v1 / "memory").mkdir(parents=True)
    (cg_v1 / "memory" / "memory.usage_in_bytes").write_text(
        str(2048 * _MIB) + "\n", encoding="utf-8"
    )
    (cg_v1 / "memory" / "memory.limit_in_bytes").write_text(
        str(4096 * _MIB) + "\n", encoding="utf-8"
    )

    m_v1 = read_cgroup_memory(cg_v1)
    assert m_v1.version == "v1"
    assert m_v1.usage_bytes == 2048 * _MIB
    assert m_v1.limit_bytes == 4096 * _MIB
    assert m_v1.usage_ratio == pytest.approx(0.5)

    # 4. cgroup v1 unlimited sentinel (9223372036854771712)
    cg_v1_unlim = tmp_path / "cgroup_v1_unlim"
    cg_v1_unlim.mkdir()
    (cg_v1_unlim / "memory.usage_in_bytes").write_text(
        str(1024 * _MIB) + "\n", encoding="utf-8"
    )
    (cg_v1_unlim / "memory.limit_in_bytes").write_text(
        "9223372036854771712\n", encoding="utf-8"
    )

    m_v1_unlim = read_cgroup_memory(cg_v1_unlim)
    assert m_v1_unlim.version == "v1"
    assert m_v1_unlim.usage_bytes == 1024 * _MIB
    assert m_v1_unlim.limit_bytes is None
    assert m_v1_unlim.usage_ratio is None


@pytest.mark.asyncio
async def test_non_linux_graceful_degradation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """On non-Linux (macOS dev machine), sampler logs one INFO line and does not sample."""
    sampler = BrowserMemorySampler(
        proc_root=tmp_path / "missing_proc",
        cgroup_root=tmp_path / "missing_cgroup",
        platform="darwin",
    )

    with caplog.at_level(logging.INFO, logger="src.observability.browser_memory"):
        sampler.start()
        result = await sampler.sample_once()

    assert result is None
    assert sampler.degraded_non_linux is True
    assert sampler._task is None
    info_records = [
        r for r in caplog.records if r.levelno == logging.INFO and "degraded" in r.message
    ]
    assert len(info_records) == 1
    diag = sampler.get_diagnostics()
    assert diag["status"] == "disabled_non_linux"


@pytest.mark.asyncio
async def test_sampling_exception_does_not_propagate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Unexpected errors inside concurrency/proc collection are caught and never raised."""
    proc_root = tmp_path / "proc"
    proc_root.mkdir()

    failing_provider = create_autospec(read_live_concurrency, side_effect=RuntimeError("boom"))
    sampler = BrowserMemorySampler(
        proc_root=proc_root,
        cgroup_root=tmp_path / "cgroup",
        concurrency_provider=failing_provider,
    )

    with caplog.at_level(logging.WARNING, logger="src.observability.browser_memory"):
        sample = await sampler.sample_once()

    assert sample is None
    failing_provider.assert_called_once()
    assert any("sample failed (ignored): boom" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_80_percent_warning_and_three_way_shares(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Usage >= 80% of cgroup limit emits a WARNING with 3-way shares; < 80% emits INFO."""
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()

    _write_proc(
        proc_root,
        pid=100,
        ppid=1,
        comm="python",
        cmdline=["python", "server.py"],
        pss_kb=800 * 1024,
    )
    _write_proc(
        proc_root,
        pid=201,
        ppid=100,
        comm="chrome",
        cmdline=["chrome", "--headless", "--no-pings"],
        pss_kb=2000 * 1024,
    )
    _write_proc(
        proc_root,
        pid=301,
        ppid=300,
        comm="chrome",
        cmdline=["chrome", "--headless"],
        pss_kb=600 * 1024,
    )

    (cgroup_root / "memory.max").write_text(str(4000 * _MIB), encoding="utf-8")
    # First sample: 50% (2000 / 4000 MiB) -> INFO
    (cgroup_root / "memory.current").write_text(str(2000 * _MIB), encoding="utf-8")

    sampler = BrowserMemorySampler(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        backend_pid=100,
        pdf_root_pids={300},
    )

    with caplog.at_level(logging.INFO, logger="src.observability.browser_memory"):
        s_low = await sampler.sample_once(now_monotonic=10.0)

    assert s_low is not None
    assert s_low.warn_triggered is False
    assert any(r.levelno == logging.INFO for r in caplog.records)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)

    caplog.clear()
    # Second sample: 85% (3400 / 4000 MiB) -> WARNING with 3-way shares
    (cgroup_root / "memory.current").write_text(str(3400 * _MIB), encoding="utf-8")

    with caplog.at_level(logging.INFO, logger="src.observability.browser_memory"):
        s_high = await sampler.sample_once(now_monotonic=70.0)

    assert s_high is not None
    assert s_high.warn_triggered is True
    warn_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warn_records) == 1
    msg = warn_records[0].message
    assert "85.00%" in msg
    payload_str = msg.split(": ", 1)[1]
    payload = json.loads(payload_str)
    assert payload["event"] == "browser_memory_high"
    assert payload["shares_of_limit_pct"] == {
        "crawler": 50.0,
        "pdf_render": 15.0,
        "other": 20.0,
    }
    assert payload["shares_of_total_pct"] == {
        "crawler": round(2000 * 100.0 / 3400, 2),
        "pdf_render": round(600 * 100.0 / 3400, 2),
        "other": round(800 * 100.0 / 3400, 2),
    }


@pytest.mark.asyncio
async def test_24_hour_rolling_peaks_and_admin_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Peaks over the last 24h roll over when older spikes age past 86,400 seconds."""
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()

    (cgroup_root / "memory.max").write_text(str(4096 * _MIB), encoding="utf-8")

    def _set_sample_state(crawler_mb: int, pdf_mb: int, other_mb: int, cg_mb: int) -> None:
        _write_proc(
            proc_root,
            pid=100,
            ppid=1,
            comm="python",
            cmdline=["python", "server.py"],
            pss_kb=other_mb * 1024,
        )
        _write_proc(
            proc_root,
            pid=201,
            ppid=100,
            comm="chrome",
            cmdline=["chrome", "--headless", "--no-pings"],
            pss_kb=crawler_mb * 1024,
        )
        _write_proc(
            proc_root,
            pid=301,
            ppid=300,
            comm="chrome",
            cmdline=["chrome", "--headless"],
            pss_kb=pdf_mb * 1024,
        )
        (cgroup_root / "memory.current").write_text(str(cg_mb * _MIB), encoding="utf-8")

    current_mono = 100.0
    sampler = BrowserMemorySampler(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        backend_pid=100,
        pdf_root_pids={300},
        clock=lambda: current_mono,
    )

    # t = 100s: early spike in crawler (1800 MiB) and pdf_render (450 MiB)
    _set_sample_state(crawler_mb=1800, pdf_mb=450, other_mb=700, cg_mb=2950)
    await sampler.sample_once()

    # t = 3600s: lower crawler (600 MiB) and pdf_render (200 MiB), higher other (850 MiB)
    current_mono = 3600.0
    _set_sample_state(crawler_mb=600, pdf_mb=200, other_mb=850, cg_mb=1650)
    await sampler.sample_once()

    peaks_within_24h = sampler.get_peaks_24h()
    assert peaks_within_24h["crawler"]["mb"] == 1800.0
    assert peaks_within_24h["pdf_render"]["mb"] == 450.0
    assert peaks_within_24h["other"]["mb"] == 850.0
    assert peaks_within_24h["cgroup_usage"]["mb"] == 2950.0

    # t = 86550s (> 100 + 86400): t=100s sample expires; t=3600s sample remains in window
    current_mono = 86550.0
    _set_sample_state(crawler_mb=300, pdf_mb=100, other_mb=720, cg_mb=1120)
    await sampler.sample_once()

    peaks_rolled = sampler.get_peaks_24h()
    assert peaks_rolled["crawler"]["mb"] == 600.0
    assert peaks_rolled["pdf_render"]["mb"] == 200.0
    assert peaks_rolled["other"]["mb"] == 850.0
    assert peaks_rolled["cgroup_usage"]["mb"] == 1650.0

    # Verify admin-only diagnostic endpoint
    monkeypatch.setattr(BrowserMemorySampler, "_instance", sampler)
    monkeypatch.setenv("DALTON_PM_USER_IDS", "pm-admin")
    get_dalton_config.cache_clear()
    try:
        diag = await get_browser_memory_diagnostics_endpoint(user_id="pm-admin")
        assert diag["status"] == "ok"
        assert diag["sample_count_24h"] == 2
        assert diag["peaks_24h"]["crawler"]["mb"] == 600.0

        with pytest.raises(HTTPException) as exc_info:
            await get_browser_memory_diagnostics_endpoint(user_id="regular-analyst")
        assert exc_info.value.status_code == 403
    finally:
        get_dalton_config.cache_clear()


def test_read_live_concurrency_from_semaphores(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_live_concurrency reads active counts from ScraplingCrawler and pdf_render semaphores."""
    crawler_spec = create_autospec(ScraplingCrawler, instance=True)
    crawler_spec._browser_sem = asyncio.Semaphore(4)  # 2 of 6 in flight

    wrapper_spec = create_autospec(safe_wrapper_mod.SafeCrawlerWrapper, instance=True)
    wrapper_spec._browser_concurrency = 6
    wrapper_spec._queue_count = 5
    wrapper_spec._crawler = crawler_spec

    monkeypatch.setattr(safe_wrapper_mod, "_safe_wrapper", wrapper_spec)
    monkeypatch.setattr(pdf_render_mod, "_RENDER_SEMAPHORE", asyncio.Semaphore(1))  # 1 of 2 in flight

    snap = read_live_concurrency()
    assert snap.crawler_browser_limit == 6
    assert snap.crawler_browser_active == 2
    assert snap.crawler_queue_active == 5
    assert snap.pdf_render_limit == 2
    assert snap.pdf_render_active == 1


def test_pdf_browser_with_user_data_dir_attributed_to_pdf_render(tmp_path: Path) -> None:
    """Playwright's standard `--user-data-dir`, `--no-service-autorun`, and `--disable-infobars`
    flags on a PDF render Chromium tree must remain in `pdf_render`, never `crawler`."""
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()

    _write_proc(
        proc_root,
        pid=100,
        ppid=1,
        comm="python",
        cmdline=["python", "server.py"],
        pss_kb=500 * 1024,
    )
    _write_proc(
        proc_root,
        pid=300,
        ppid=100,
        comm="node",
        cmdline=["node", "/opt/playwright/driver/package/cli.js", "run-driver"],
        pss_kb=30 * 1024,
    )
    _write_proc(
        proc_root,
        pid=301,
        ppid=300,
        comm="chrome",
        cmdline=[
            "/ms-playwright/chromium/chrome-linux/chrome",
            "--headless",
            "--user-data-dir=/tmp/playwright_chromiumdev_profile-abc123",
            "--no-service-autorun",
            "--disable-infobars",
            "--no-first-run",
        ],
        pss_kb=150 * 1024,
    )
    _write_proc(
        proc_root,
        pid=302,
        ppid=301,
        comm="chrome",
        cmdline=[
            "/ms-playwright/chromium/chrome-linux/chrome",
            "--type=renderer",
            "--user-data-dir=/tmp/playwright_chromiumdev_profile-abc123",
        ],
        pss_kb=120 * 1024,
    )

    sample = collect_browser_memory_sample(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        backend_pid=100,
        pdf_root_pids={300},
    )

    assert sample.pdf_render.procs == 3
    assert sample.pdf_render.pids == (300, 301, 302)
    assert sample.pdf_render.mb == 300.0
    assert sample.crawler.procs == 0
    assert sample.crawler.bytes == 0
    assert sample.other.pids == (100,)


def test_parent_pid_chain_overrides_conflicting_cmdline_markers(tmp_path: Path) -> None:
    """Known parent-PID roots take strict precedence over conflicting child cmdline flags."""
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()

    _write_proc(
        proc_root,
        pid=100,
        ppid=1,
        comm="python",
        cmdline=["python", "server.py"],
        pss_kb=400 * 1024,
    )
    # Known PDF root (300) whose child (301) has Scrapling-like flags (`--no-pings`, `--homepage=about:blank`)
    _write_proc(
        proc_root,
        pid=300,
        ppid=100,
        comm="node",
        cmdline=["node", "/opt/playwright/driver/package/cli.js", "run-driver"],
        pss_kb=25 * 1024,
    )
    _write_proc(
        proc_root,
        pid=301,
        ppid=300,
        comm="chrome",
        cmdline=[
            "/ms-playwright/chromium/chrome-linux/chrome",
            "--headless",
            "--no-pings",
            "--homepage=about:blank",
        ],
        pss_kb=175 * 1024,
    )
    # Known crawler root (200) whose child (201) has `/api/v1/wsfiles/` in cmdline
    _write_proc(
        proc_root,
        pid=200,
        ppid=100,
        comm="node",
        cmdline=["node", "/opt/playwright/driver/package/cli.js", "run-driver"],
        pss_kb=20 * 1024,
    )
    _write_proc(
        proc_root,
        pid=201,
        ppid=200,
        comm="chrome",
        cmdline=[
            "/ms-playwright/chromium/chrome-linux/chrome",
            "--headless",
            "http://127.0.0.1:8000/api/v1/wsfiles/ws-1/index.html",
        ],
        pss_kb=180 * 1024,
    )

    sample = collect_browser_memory_sample(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        backend_pid=100,
        pdf_root_pids={300},
        crawler_root_pids={200},
    )

    assert sample.pdf_render.pids == (300, 301)
    assert sample.pdf_render.mb == 200.0
    assert sample.crawler.pids == (200, 201)
    assert sample.crawler.mb == 200.0
    assert sample.other.pids == (100,)


def test_unknown_ancestor_and_no_exclusive_markers_falls_back_to_other(
    tmp_path: Path,
) -> None:
    """Browser/driver processes with neither a known root ancestor nor exclusive markers
    are attributed to `other` rather than guessed as `crawler`."""
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()

    _write_proc(
        proc_root,
        pid=1,
        ppid=0,
        comm="tini",
        cmdline=["/usr/bin/tini", "--", "python", "server.py"],
        pss_kb=2 * 1024,
    )
    _write_proc(
        proc_root,
        pid=100,
        ppid=1,
        comm="python",
        cmdline=["python", "server.py"],
        pss_kb=500 * 1024,
    )
    # Generic Playwright driver and Chromium without known root or Scrapling-exclusive flags
    _write_proc(
        proc_root,
        pid=400,
        ppid=100,
        comm="node",
        cmdline=["node", "/opt/playwright/driver/package/cli.js", "run-driver"],
        pss_kb=30 * 1024,
    )
    _write_proc(
        proc_root,
        pid=401,
        ppid=400,
        comm="chrome",
        cmdline=[
            "/ms-playwright/chromium/chrome-linux/chrome",
            "--headless",
            "--user-data-dir=/tmp/playwright_chromiumdev_profile-generic",
            "--no-service-autorun",
            "--disable-infobars",
        ],
        pss_kb=140 * 1024,
    )
    # Orphaned generic renderer reparented to PID 1 with no exclusive markers
    _write_proc(
        proc_root,
        pid=402,
        ppid=1,
        comm="chrome",
        cmdline=["/ms-playwright/chromium/chrome-linux/chrome", "--type=renderer"],
        pss_kb=60 * 1024,
    )

    sample = collect_browser_memory_sample(
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        backend_pid=100,
        pdf_root_pids=set(),
        crawler_root_pids=set(),
    )

    assert sample.crawler.procs == 0
    assert sample.crawler.bytes == 0
    assert sample.pdf_render.procs == 0
    assert sample.pdf_render.bytes == 0
    assert sample.other.pids == (1, 100, 400, 401, 402)
    assert sample.other.mb == 732.0


@pytest.mark.asyncio
async def test_explicit_pid_registration_without_private_playwright_attrs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When Playwright private `_connection._transport._proc` attributes are absent,
    explicit DALTON-HOOK registration still attributes both PDF and crawler trees."""
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()

    clear_pdf_render_pids()
    clear_crawler_pids()
    try:
        _write_proc(
            proc_root,
            pid=100,
            ppid=1,
            comm="python",
            cmdline=["python", "server.py"],
            pss_kb=500 * 1024,
        )

        # Simulate `_get_browser` launching PDF driver PID 300 in `/proc` with NO `_connection` attr
        async with capture_browser_launch(
            "pdf_render", parent_pid=100, proc_root=proc_root
        ) as launch_cap:
            _write_proc(
                proc_root,
                pid=300,
                ppid=100,
                comm="node",
                cmdline=["node", "/opt/playwright/driver/package/cli.js", "run-driver"],
                pss_kb=30 * 1024,
            )
            _write_proc(
                proc_root,
                pid=301,
                ppid=300,
                comm="chrome",
                cmdline=[
                    "/ms-playwright/chromium/chrome-linux/chrome",
                    "--headless",
                    "--user-data-dir=/tmp/playwright_chromiumdev_profile-pdf",
                ],
                pss_kb=170 * 1024,
            )
            # Handle has NO `_connection` attribute at all
            opaque_browser = object()
            launch_cap.register_handles(opaque_browser)

        assert get_registered_pdf_render_pids() == {300}

        # Simulate crawler session explicitly registering root PID 200 (also without private attrs)
        register_crawler_pids(200)
        _write_proc(
            proc_root,
            pid=200,
            ppid=100,
            comm="node",
            cmdline=["node", "/opt/playwright/driver/package/cli.js", "run-driver"],
            pss_kb=25 * 1024,
        )
        _write_proc(
            proc_root,
            pid=201,
            ppid=200,
            comm="chrome",
            cmdline=[
                "/ms-playwright/chromium/chrome-linux/chrome",
                "--headless",
                "--user-data-dir=/tmp/playwright_chromiumdev_profile-crawl",
            ],
            pss_kb=225 * 1024,
        )

        # Ensure `pdf_render._playwright_cm` and `_browser` have no `_connection`
        monkeypatch.setattr(pdf_render_mod, "_playwright_cm", object())
        monkeypatch.setattr(pdf_render_mod, "_browser", object())

        snap = read_live_concurrency()
        assert snap.pdf_driver_pid == 300
        assert snap.pdf_root_pids == (300,)
        assert snap.crawler_root_pids == (200,)

        sample = collect_browser_memory_sample(
            proc_root=proc_root,
            cgroup_root=cgroup_root,
            backend_pid=100,
            concurrency=snap,
        )
        assert sample.pdf_render.pids == (300, 301)
        assert sample.pdf_render.mb == 200.0
        assert sample.crawler.pids == (200, 201)
        assert sample.crawler.mb == 250.0
        assert sample.other.pids == (100,)

        # Resetting the PDF browser clears registered PDF root PIDs
        await pdf_render_mod._reset_browser()
        assert get_registered_pdf_render_pids() == set()

        # Direct registration + track_crawler_session lifecycle cleanup
        register_pdf_render_pids(350)
        assert get_registered_pdf_render_pids() == {350}

        class _FakeSession:
            playwright = None
            browser = None
            context = None
            driver_pid = 250

            async def start(self) -> None:
                return None

        fake_sess = _FakeSession()
        async with track_crawler_session(
            fake_sess, parent_pid=100, proc_root=proc_root
        ):
            await fake_sess.start()
            assert 250 in get_registered_crawler_pids()
        assert 250 not in get_registered_crawler_pids()
    finally:
        clear_pdf_render_pids()
        clear_crawler_pids()


@pytest.mark.asyncio
async def test_24h_peaks_retained_at_10s_interval_via_minute_buckets(
    tmp_path: Path,
) -> None:
    """At a 10-second sampling cadence (8,640 samples in 24h), minute-bucketed
    retention preserves an early spike across the entire 24-hour window."""
    proc_root = tmp_path / "proc"
    cgroup_root = tmp_path / "cgroup"
    proc_root.mkdir()
    cgroup_root.mkdir()

    (cgroup_root / "memory.max").write_text(str(4096 * _MIB), encoding="utf-8")
    (cgroup_root / "memory.current").write_text(str(1500 * _MIB), encoding="utf-8")

    _write_proc(
        proc_root,
        pid=100,
        ppid=1,
        comm="python",
        cmdline=["python", "server.py"],
        pss_kb=600 * 1024,
    )
    _write_proc(
        proc_root,
        pid=201,
        ppid=200,
        comm="chrome",
        cmdline=["chrome", "--headless", "--no-pings"],
        pss_kb=1900 * 1024,
    )
    _write_proc(
        proc_root,
        pid=301,
        ppid=300,
        comm="chrome",
        cmdline=["chrome", "--headless"],
        pss_kb=500 * 1024,
    )

    current_mono = 0.0
    sampler = BrowserMemorySampler(
        interval_s=10.0,
        proc_root=proc_root,
        cgroup_root=cgroup_root,
        backend_pid=100,
        pdf_root_pids={300},
        crawler_root_pids={200},
        clock=lambda: current_mono,
    )

    # First sample at t = 0s with spike (crawler = 1900 MiB, pdf_render = 500 MiB)
    first_sample = await sampler.sample_once(now_monotonic=0.0)
    assert first_sample is not None

    # Steady-state sample template (crawler = 250 MiB, pdf_render = 120 MiB)
    _write_proc(
        proc_root,
        pid=201,
        ppid=200,
        comm="chrome",
        cmdline=["chrome", "--headless", "--no-pings"],
        pss_kb=250 * 1024,
    )
    _write_proc(
        proc_root,
        pid=301,
        ppid=300,
        comm="chrome",
        cmdline=["chrome", "--headless"],
        pss_kb=120 * 1024,
    )
    # Container usage drops with the browsers after the first minute.
    (cgroup_root / "memory.current").write_text(str(250 * _MIB), encoding="utf-8")
    steady_template = sampler._collect_sync(10.0)

    # Feed the remaining 8,639 samples at 10-second intervals across [0, 86400)
    from dataclasses import replace

    for step in range(1, 8640):
        t = float(step * 10)
        current_mono = t
        s = replace(steady_template, monotonic_ts=t, ts=f"t-{step}")
        sampler._latest = s
        sampler._record_into_bucket(s, t)

    diag = sampler.get_diagnostics(now_monotonic=86390.0)
    assert diag["sample_count_24h"] == 8640
    assert diag["bucket_count_24h"] == 1440
    assert diag["peaks_24h"]["crawler"]["mb"] == 1900.0
    assert diag["peaks_24h"]["pdf_render"]["mb"] == 500.0
    assert "统计范围为当前 worker 进程启动的浏览器 + 容器级总量" in diag["scope"]

    # Minute granularity (lead, P6a round 3): at t = 86,410s the window starts at
    # 10s; bucket 0 still holds the 50s sample, so its peak is still reported
    # rather than dropping to the steady value.
    boundary_peaks = sampler.get_peaks_24h(now_monotonic=86410.0)
    assert boundary_peaks["crawler"]["mb"] == 1900.0
    # The container-level peak follows the same minute rule.
    assert boundary_peaks["cgroup_usage"]["mb"] == 1500.0

    # Advance past 24h after bucket 0 (t = 86,460s -> cutoff = 60s > bucket 0 max_ts 50s)
    current_mono = 86460.0
    s_after = replace(steady_template, monotonic_ts=86460.0, ts="t-after")
    sampler._latest = s_after
    sampler._record_into_bucket(s_after, 86460.0)

    peaks_after = sampler.get_peaks_24h(now_monotonic=86460.0)
    assert peaks_after["crawler"]["mb"] == 250.0
    assert peaks_after["pdf_render"]["mb"] == 120.0



# Lead fix (P6a round 2): a launch the subprocess hook saw keeps to its own
# PIDs; the worker-wide child diff would also take a browser launched by a
# concurrent task at the same moment (a crawler next to a first PDF render).
def test_hooked_launch_does_not_take_a_concurrent_launchs_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.observability.browser_memory as bm

    registered: list[tuple[str, tuple[int, ...]]] = []
    monkeypatch.setattr(
        bm, "register_pdf_render_pids", lambda *p: registered.append(("pdf", p))
    )
    monkeypatch.setattr(
        bm, "register_crawler_pids", lambda *p: registered.append(("crawler", p))
    )
    # PDF driver 101 (hooked) and a concurrent crawler driver 202 both appear
    # as new children of the worker between the two snapshots.
    monkeypatch.setattr(bm, "_snapshot_direct_children", lambda *_a, **_k: {101, 202})

    capture = bm._BrowserLaunchCapture("pdf_render", parent_pid=1)
    capture._before_children = set()
    capture.pids.add(101)  # what the subprocess hook recorded for this task
    assert capture.finalize() == {101}
    assert registered == [("pdf", (101,))]
