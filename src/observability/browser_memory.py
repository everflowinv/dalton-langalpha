"""Three-way container memory attribution for in-backend browsers.

Samples `/proc` and cgroup memory on a fixed cadence (default 60s) to attribute
backend container memory across three buckets before deciding whether to split
browsers into a dedicated container:

1. ``crawler``: Scrapling / Playwright / Patchright / Camoufox browser process
   trees launched for web fetching.
2. ``pdf_render``: Playwright headless Chromium process tree launched by
   ``src/server/services/pdf_render.py``.
3. ``other``: the backend Python worker itself plus non-browser child processes
   (``tini``, ``uv``, ``heavy_compute`` workers, stdio MCP servers) and any
   unrecognized processes with neither a known root ancestor nor exclusive
   command-line markers.

Known multi-worker scope limitation (production runs a single worker):
统计范围为当前 worker 进程启动的浏览器 + 容器级总量（不做跨 worker 汇总）。

Per-process memory prefers ``Pss`` from ``/proc/<pid>/smaps_rollup`` so shared
Chromium mappings (binary text, ICU data, V8 snapshot, ``/dev/shm``) are not
multiplied across helper processes; when ``smaps_rollup`` is absent or lacks
``Pss``, it falls back to ``VmRSS`` in ``/proc/<pid>/status`` (then ``statm`` /
``stat``). On non-Linux hosts (macOS dev machines) where ``/proc`` is absent,
the sampler logs one ``INFO`` line at startup and degrades to a no-op.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import logging
import math
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Literal

from src.utils.concurrency import cancel_and_join

logger = logging.getLogger(__name__)

_MIB = 1024 * 1024
_DEFAULT_INTERVAL_S = 60.0
_DEFAULT_WARN_RATIO = 0.80
_DEFAULT_WINDOW_S = 24 * 3600.0
_BUCKET_SPAN_S = 60.0
_MAX_BUCKETS_24H = 1440
# 64-bit cgroup v1 uses a sentinel near 2^63-1 when no memory limit is set.
_UNLIMITED_CGROUP_BYTES = 1 << 60
_DEFAULT_PAGE_SIZE = 4096

_DIAGNOSTICS_SCOPE_NOTE = (
    "统计范围为当前 worker 进程启动的浏览器 + 容器级总量（不做跨 worker 汇总）"
)

_INTERVAL_ENV_KEYS = (
    "DALTON_BROWSER_MEMORY_SAMPLE_INTERVAL_S",
    "BROWSER_MEMORY_SAMPLE_INTERVAL_S",
)
_ENABLED_ENV_KEYS = (
    "DALTON_BROWSER_MEMORY_SAMPLER_ENABLED",
    "BROWSER_MEMORY_SAMPLER_ENABLED",
)

_BROWSER_BIN_PREFIXES = (
    "chrome",
    "chromium",
    "headless_shell",
    "chrome-headless-shell",
    "camoufox",
    "firefox",
)

# Strictly Scrapling-exclusive command-line markers.
# Standard Playwright flags set on every `chromium.launch()` (such as
# `--user-data-dir=`, `--no-service-autorun`, `--disable-infobars`,
# `--no-first-run`) MUST NOT appear here because `pdf_render.py` also launches
# Chromium via Playwright and receives those flags from `chromiumSwitches.js`.
_CRAWLER_EXCLUSIVE_CMDLINE_MARKERS = (
    "scrapling",
    "patchright",
    "camoufox",
    "--no-pings",
    "--homepage=about:blank",
    "--disable-session-crashed-bubble",
    "--disable-blink-features=automationcontrolled",
    "--fingerprinting-canvas-image-data-noise",
)

_PDF_RENDER_EXCLUSIVE_CMDLINE_MARKERS = (
    "pdf_render",
    "pdf-render",
    "/api/v1/wsfiles/",
)

_PSS_RE = re.compile(r"^Pss:\s+(\d+)\s*kB", re.MULTILINE)
_RSS_ROLLUP_RE = re.compile(r"^Rss:\s+(\d+)\s*kB", re.MULTILINE)
_VMRSS_RE = re.compile(r"^VmRSS:\s+(\d+)\s*kB", re.MULTILINE)
_PPID_STATUS_RE = re.compile(r"^PPid:\s+(\d+)", re.MULTILINE)
_NAME_STATUS_RE = re.compile(r"^Name:\s+(\S+)", re.MULTILINE)

BucketRole = Literal["crawler", "pdf_render", "other"]
MetricSource = Literal["pss", "rss", "pss+rss"]


# ---------------------------------------------------------------------------
# Explicit per-worker driver / browser root PID registry
# ---------------------------------------------------------------------------
# Populated at browser launch / session start by DALTON-HOOKs in `pdf_render.py`
# and `scrapling_crawler.py` so attribution does not depend on Playwright's
# private `_connection._transport._proc.pid` chain.
_registered_pdf_root_pids: set[int] = set()
_registered_crawler_root_pids: set[int] = set()
_active_launch_pids_var: ContextVar[set[int] | None] = ContextVar(
    "_active_launch_pids_var", default=None
)
_subprocess_exec_hook_installed = False
_orig_create_subprocess_exec: Any = None


def _is_valid_pid(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def register_pdf_render_pids(*pids: int) -> None:
    """Explicitly register PDF render driver/browser root PIDs for this worker."""
    for pid in pids:
        if _is_valid_pid(pid):
            _registered_pdf_root_pids.add(pid)
            _registered_crawler_root_pids.discard(pid)


def unregister_pdf_render_pids(*pids: int) -> None:
    """Remove specific PDF render root PIDs."""
    for pid in pids:
        if _is_valid_pid(pid):
            _registered_pdf_root_pids.discard(pid)


def clear_pdf_render_pids() -> None:
    """Clear all registered PDF render root PIDs (called on `_reset_browser`)."""
    _registered_pdf_root_pids.clear()


def get_registered_pdf_render_pids() -> set[int]:
    """Return a copy of currently registered PDF render root PIDs."""
    return set(_registered_pdf_root_pids)


def register_crawler_pids(*pids: int) -> None:
    """Explicitly register crawler driver/browser root PIDs for this worker."""
    for pid in pids:
        if _is_valid_pid(pid):
            if pid not in _registered_pdf_root_pids:
                _registered_crawler_root_pids.add(pid)


def unregister_crawler_pids(*pids: int) -> None:
    """Remove crawler driver/browser root PIDs when a crawl session closes."""
    for pid in pids:
        if _is_valid_pid(pid):
            _registered_crawler_root_pids.discard(pid)


def clear_crawler_pids() -> None:
    """Clear all registered crawler root PIDs."""
    _registered_crawler_root_pids.clear()


def get_registered_crawler_pids() -> set[int]:
    """Return a copy of currently registered crawler root PIDs."""
    return set(_registered_crawler_root_pids)


def _ensure_subprocess_exec_hook() -> None:
    """Install a ContextVar-gated listener on `asyncio.create_subprocess_exec`.

    When a browser launch runs inside `capture_browser_launch`, the spawned
    `asyncio.subprocess.Process` exposes its public `.pid` attribute here,
    recording the exact driver PID without touching Playwright internals.
    Outside `capture_browser_launch`, `_active_launch_pids_var.get()` is `None`
    and the wrapper delegates straight through.
    """
    global _subprocess_exec_hook_installed, _orig_create_subprocess_exec
    if _subprocess_exec_hook_installed:
        return
    _orig_create_subprocess_exec = asyncio.create_subprocess_exec

    async def _hooked_create_subprocess_exec(*args: Any, **kwargs: Any) -> Any:
        proc = await _orig_create_subprocess_exec(*args, **kwargs)
        sink = _active_launch_pids_var.get()
        if sink is not None:
            pid = getattr(proc, "pid", None)
            if _is_valid_pid(pid):
                sink.add(pid)
        return proc

    asyncio.create_subprocess_exec = _hooked_create_subprocess_exec  # type: ignore[assignment]
    _subprocess_exec_hook_installed = True


def _snapshot_direct_children(
    parent_pid: int,
    proc_root: Path = Path("/proc"),
) -> set[int]:
    """Return all current PIDs in ``proc_root`` whose ``ppid == parent_pid``."""
    children: set[int] = set()
    try:
        if not proc_root.is_dir():
            return children
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            ppid: int | None = None
            try:
                stat_raw = (entry / "stat").read_text(
                    encoding="utf-8", errors="replace"
                )
                _, right = stat_raw.rsplit(")", 1)
                ppid = int(right.split()[1])
            except Exception:
                try:
                    status_raw = (entry / "status").read_text(
                        encoding="utf-8", errors="replace"
                    )
                    m_ppid = _PPID_STATUS_RE.search(status_raw)
                    if m_ppid:
                        ppid = int(m_ppid.group(1))
                except Exception:
                    pass
            if ppid == parent_pid:
                children.add(pid)
    except Exception:
        pass
    return children


def _extract_public_pids(*handles: Any) -> set[int]:
    """Extract public PID attributes (`.pid`, `.driver_pid`, `.process.pid`) if present."""
    found: set[int] = set()
    for handle in handles:
        if handle is None:
            continue
        for attr in ("pid", "driver_pid"):
            try:
                val = getattr(handle, attr, None)
                if _is_valid_pid(val):
                    found.add(val)
            except Exception:
                pass
        for sub_attr in ("process", "proc", "browser", "playwright", "context"):
            try:
                sub = getattr(handle, sub_attr, None)
                if sub is not None:
                    for attr in ("pid", "driver_pid"):
                        val = getattr(sub, attr, None)
                        if _is_valid_pid(val):
                            found.add(val)
            except Exception:
                pass
    return found


def _extract_playwright_driver_pid(*handles: Any) -> int | None:
    """Fallback-only extraction of Playwright's private `_connection._transport._proc.pid`."""
    candidates: list[Any] = []
    for h in handles:
        if h is None:
            continue
        candidates.append(h)
        try:
            impl = getattr(h, "_impl_obj", None)
            if impl is not None:
                candidates.append(impl)
        except Exception:
            pass
    for candidate in candidates:
        try:
            conn = getattr(candidate, "_connection", None)
            transport = getattr(conn, "_transport", None)
            proc = getattr(transport, "_proc", None)
            pid = getattr(proc, "pid", None)
            if _is_valid_pid(pid):
                return pid
        except Exception:
            continue
    return None


class _BrowserLaunchCapture:
    """Collects driver/browser root PIDs spawned during a browser launch block."""

    def __init__(
        self,
        role: Literal["pdf_render", "crawler"],
        *,
        parent_pid: int | None = None,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self.role = role
        self.parent_pid = parent_pid if parent_pid is not None else os.getpid()
        self.proc_root = proc_root
        self.pids: set[int] = set()
        self._before_children: set[int] = set()
        self._handles: list[Any] = []

    def register_handles(self, *handles: Any) -> set[int]:
        """Record launched handles and immediately register discovered root PIDs."""
        self._handles.extend(h for h in handles if h is not None)
        return self.finalize()

    def finalize(self) -> set[int]:
        """Discover spawned PIDs via subprocess hook, `/proc` child diff, public attrs, then fallback."""
        # The subprocess hook already put this launch's own PIDs in `self.pids`
        # (task-local). The worker-wide child diff would also pick up a PDF and
        # a crawler browser launched at the same moment, so it is only a
        # fallback for a launch the hook did not see.
        if not self.pids:
            try:
                after_children = _snapshot_direct_children(
                    self.parent_pid, self.proc_root
                )
                self.pids.update(after_children - self._before_children)
            except Exception:
                pass
        try:
            self.pids.update(_extract_public_pids(*self._handles))
        except Exception:
            pass
        if not self.pids and self._handles:
            fallback_pid = _extract_playwright_driver_pid(*self._handles)
            if fallback_pid is not None:
                self.pids.add(fallback_pid)

        if self.pids:
            if self.role == "pdf_render":
                register_pdf_render_pids(*self.pids)
            else:
                register_crawler_pids(*self.pids)
        return set(self.pids)


@asynccontextmanager
async def capture_browser_launch(
    role: Literal["pdf_render", "crawler"],
    *,
    parent_pid: int | None = None,
    proc_root: Path = Path("/proc"),
) -> AsyncIterator[_BrowserLaunchCapture]:
    """Capture and register driver/browser PIDs spawned inside the `async with` block."""
    capture = _BrowserLaunchCapture(
        role, parent_pid=parent_pid, proc_root=proc_root
    )
    token = None
    try:
        _ensure_subprocess_exec_hook()
        capture._before_children = _snapshot_direct_children(
            capture.parent_pid, capture.proc_root
        )
        token = _active_launch_pids_var.set(capture.pids)
    except Exception:
        pass
    try:
        yield capture
    finally:
        if token is not None:
            try:
                _active_launch_pids_var.reset(token)
            except Exception:
                pass
        try:
            capture.finalize()
        except Exception:
            pass


@asynccontextmanager
async def track_crawler_session(
    session: Any,
    *,
    parent_pid: int | None = None,
    proc_root: Path = Path("/proc"),
) -> AsyncIterator[set[int]]:
    """Register a crawler session's driver/browser PIDs on `start()` and unregister on exit."""
    session_pids: set[int] = set()
    orig_start = getattr(session, "start", None)
    eff_parent = parent_pid if parent_pid is not None else os.getpid()

    if callable(orig_start):

        async def _tracked_start(*args: Any, **kwargs: Any) -> Any:
            async with capture_browser_launch(
                "crawler", parent_pid=eff_parent, proc_root=proc_root
            ) as cap:
                try:
                    return await orig_start(*args, **kwargs)
                finally:
                    try:
                        session.start = orig_start
                    except Exception:
                        pass
                    cap.register_handles(
                        session,
                        getattr(session, "playwright", None),
                        getattr(session, "browser", None),
                        getattr(session, "context", None),
                    )
                    session_pids.update(cap.pids)

        try:
            session.start = _tracked_start
        except Exception:
            pass

    try:
        yield session_pids
    finally:
        if callable(orig_start):
            try:
                session.start = orig_start
            except Exception:
                pass
        if session_pids:
            unregister_crawler_pids(*session_pids)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BucketMetrics:
    """Aggregated memory and process count for one attribution bucket."""

    bytes: int
    mb: float
    procs: int
    pids: tuple[int, ...] = ()


@dataclass(frozen=True)
class CgroupMemory:
    """Container cgroup memory reading (v2 or v1)."""

    version: Literal["v2", "v1"] | None
    usage_bytes: int | None
    limit_bytes: int | None
    usage_ratio: float | None


@dataclass(frozen=True)
class ConcurrencySnapshot:
    """In-flight concurrency and known root PIDs for crawler and PDF render."""

    crawler_browser_active: int
    crawler_browser_limit: int
    crawler_queue_active: int
    pdf_render_active: int
    pdf_render_limit: int
    pdf_driver_pid: int | None = None
    pdf_root_pids: tuple[int, ...] = ()
    crawler_root_pids: tuple[int, ...] = ()


@dataclass(frozen=True)
class BrowserMemorySample:
    """One point-in-time three-way memory attribution sample."""

    ts: str
    monotonic_ts: float
    metric: MetricSource
    rss_fallback_procs: int
    crawler: BucketMetrics
    pdf_render: BucketMetrics
    other: BucketMetrics
    total_proc_bytes: int
    total_procs: int
    cgroup: CgroupMemory
    concurrency: ConcurrencySnapshot
    warn_triggered: bool

    def shares_of_total_pct(self) -> dict[str, float]:
        if self.total_proc_bytes <= 0:
            return {"crawler": 0.0, "pdf_render": 0.0, "other": 0.0}
        total = float(self.total_proc_bytes)
        return {
            "crawler": round(self.crawler.bytes * 100.0 / total, 2),
            "pdf_render": round(self.pdf_render.bytes * 100.0 / total, 2),
            "other": round(self.other.bytes * 100.0 / total, 2),
        }

    def shares_of_limit_pct(self) -> dict[str, float] | None:
        limit = self.cgroup.limit_bytes
        if limit is None or limit <= 0:
            return None
        lim = float(limit)
        return {
            "crawler": round(self.crawler.bytes * 100.0 / lim, 2),
            "pdf_render": round(self.pdf_render.bytes * 100.0 / lim, 2),
            "other": round(self.other.bytes * 100.0 / lim, 2),
        }

    def to_log_dict(self) -> dict[str, Any]:
        usage_b = self.cgroup.usage_bytes
        limit_b = self.cgroup.limit_bytes
        ratio = self.cgroup.usage_ratio
        return {
            "event": (
                "browser_memory_high"
                if self.warn_triggered
                else "browser_memory_sample"
            ),
            "ts": self.ts,
            "metric": self.metric,
            "rss_fallback_procs": self.rss_fallback_procs,
            "crawler_bytes": self.crawler.bytes,
            "crawler_mb": self.crawler.mb,
            "crawler_procs": self.crawler.procs,
            "pdf_render_bytes": self.pdf_render.bytes,
            "pdf_render_mb": self.pdf_render.mb,
            "pdf_render_procs": self.pdf_render.procs,
            # Crawl and PDF browsers together: exact whatever the split between
            # them (attribution of one browser tree to the wrong one of the two
            # still counts it here).
            "browsers_total_bytes": self.crawler.bytes + self.pdf_render.bytes,
            "browsers_total_mb": round((self.crawler.bytes + self.pdf_render.bytes) / _MIB, 2),
            "other_bytes": self.other.bytes,
            "other_mb": self.other.mb,
            "other_procs": self.other.procs,
            "total_proc_bytes": self.total_proc_bytes,
            "total_proc_mb": round(self.total_proc_bytes / _MIB, 2),
            "total_procs": self.total_procs,
            "cgroup_version": self.cgroup.version,
            "cgroup_usage_bytes": usage_b,
            "cgroup_usage_mb": round(usage_b / _MIB, 2) if usage_b is not None else None,
            "cgroup_limit_bytes": limit_b,
            "cgroup_limit_mb": round(limit_b / _MIB, 2) if limit_b is not None else None,
            "cgroup_usage_ratio": round(ratio, 4) if ratio is not None else None,
            "cgroup_usage_pct": round(ratio * 100.0, 2) if ratio is not None else None,
            "crawler_browser_active": self.concurrency.crawler_browser_active,
            "crawler_browser_limit": self.concurrency.crawler_browser_limit,
            "crawler_queue_active": self.concurrency.crawler_queue_active,
            "pdf_render_active": self.concurrency.pdf_render_active,
            "pdf_render_limit": self.concurrency.pdf_render_limit,
            "shares_of_total_pct": self.shares_of_total_pct(),
            "shares_of_limit_pct": self.shares_of_limit_pct(),
        }


@dataclass(frozen=True)
class _ProcRecord:
    pid: int
    ppid: int
    comm: str
    cmdline: str
    memory_bytes: int
    used_pss: bool


@dataclass
class _MinutePeakBucket:
    """Aggregates per-road peaks within a 1-minute time slice (1,440 buckets = 24h)."""

    bucket_id: int
    min_monotonic_ts: float
    max_monotonic_ts: float
    sample_count: int
    crawler_peak: BrowserMemorySample
    pdf_render_peak: BrowserMemorySample
    other_peak: BrowserMemorySample
    total_proc_peak: BrowserMemorySample
    cgroup_peak: BrowserMemorySample | None

    @classmethod
    def from_sample(
        cls, bucket_id: int, sample: BrowserMemorySample
    ) -> _MinutePeakBucket:
        return cls(
            bucket_id=bucket_id,
            min_monotonic_ts=sample.monotonic_ts,
            max_monotonic_ts=sample.monotonic_ts,
            sample_count=1,
            crawler_peak=sample,
            pdf_render_peak=sample,
            other_peak=sample,
            total_proc_peak=sample,
            cgroup_peak=sample if sample.cgroup.usage_bytes is not None else None,
        )

    def update(self, sample: BrowserMemorySample, *, cutoff: float) -> None:
        self.sample_count += 1
        if sample.monotonic_ts < self.min_monotonic_ts:
            self.min_monotonic_ts = sample.monotonic_ts
        if sample.monotonic_ts > self.max_monotonic_ts:
            self.max_monotonic_ts = sample.monotonic_ts

        if (
            self.crawler_peak.monotonic_ts < cutoff
            or (sample.crawler.bytes, sample.monotonic_ts)
            >= (self.crawler_peak.crawler.bytes, self.crawler_peak.monotonic_ts)
        ):
            self.crawler_peak = sample

        if (
            self.pdf_render_peak.monotonic_ts < cutoff
            or (sample.pdf_render.bytes, sample.monotonic_ts)
            >= (
                self.pdf_render_peak.pdf_render.bytes,
                self.pdf_render_peak.monotonic_ts,
            )
        ):
            self.pdf_render_peak = sample

        if (
            self.other_peak.monotonic_ts < cutoff
            or (sample.other.bytes, sample.monotonic_ts)
            >= (self.other_peak.other.bytes, self.other_peak.monotonic_ts)
        ):
            self.other_peak = sample

        if (
            self.total_proc_peak.monotonic_ts < cutoff
            or (sample.total_proc_bytes, sample.monotonic_ts)
            >= (
                self.total_proc_peak.total_proc_bytes,
                self.total_proc_peak.monotonic_ts,
            )
        ):
            self.total_proc_peak = sample

        if sample.cgroup.usage_bytes is not None:
            if (
                self.cgroup_peak is None
                or self.cgroup_peak.monotonic_ts < cutoff
                or self.cgroup_peak.cgroup.usage_bytes is None
                or (sample.cgroup.usage_bytes, sample.monotonic_ts)
                >= (
                    self.cgroup_peak.cgroup.usage_bytes,
                    self.cgroup_peak.monotonic_ts,
                )
            ):
                self.cgroup_peak = sample


def _resolve_interval_s(explicit: float | None) -> float:
    if explicit is not None:
        return float(explicit)
    for key in _INTERVAL_ENV_KEYS:
        raw = os.environ.get(key)
        if raw is not None and raw.strip():
            try:
                val = float(raw.strip())
                if val > 0:
                    return val
            except ValueError:
                pass
    return _DEFAULT_INTERVAL_S


def _is_sampler_enabled_env() -> bool:
    for key in _ENABLED_ENV_KEYS:
        raw = os.environ.get(key)
        if raw is not None and raw.strip():
            return raw.strip().lower() not in ("0", "false", "no", "off")
    return True


def read_live_concurrency() -> ConcurrencySnapshot:
    """Read current crawler/PDF browser concurrency and registered root PIDs in-process."""
    crawler_active = 0
    crawler_limit = 2
    crawler_queue = 0
    try:
        from src.config.tool_settings import get_crawler_browser_concurrency

        crawler_limit = int(get_crawler_browser_concurrency())
    except Exception:
        pass

    try:
        from src.tools.web.inhouse import safe_wrapper as sw_mod

        wrapper = getattr(sw_mod, "_safe_wrapper", None)
        if wrapper is not None:
            limit_attr = getattr(wrapper, "_browser_concurrency", None)
            if isinstance(limit_attr, int) and limit_attr > 0:
                crawler_limit = limit_attr
            queue_attr = getattr(wrapper, "_queue_count", 0)
            if isinstance(queue_attr, int) and queue_attr >= 0:
                crawler_queue = queue_attr
            crawler_obj = getattr(wrapper, "_crawler", None)
            sem = getattr(crawler_obj, "_browser_sem", None)
            if sem is None and crawler_obj is not None:
                fallback = getattr(crawler_obj, "_fallback", None)
                sem = getattr(fallback, "_browser_sem", None)
            if sem is not None:
                avail = getattr(sem, "_value", None)
                if isinstance(avail, int):
                    crawler_active = max(0, crawler_limit - avail)
    except Exception:
        pass

    pdf_active = 0
    pdf_limit = 2
    pdf_roots = get_registered_pdf_render_pids()
    crawler_roots = get_registered_crawler_pids()
    pdf_driver_pid: int | None = min(pdf_roots) if pdf_roots else None

    try:
        from src.server.services import pdf_render as pdf_mod

        render_sem = getattr(pdf_mod, "_RENDER_SEMAPHORE", None)
        if render_sem is not None:
            avail = getattr(render_sem, "_value", None)
            if isinstance(avail, int):
                pdf_active = max(0, pdf_limit - avail)
        cm = getattr(pdf_mod, "_playwright_cm", None)
        browser = getattr(pdf_mod, "_browser", None)
        # Prefer explicit registration; fall back to public attrs then private chain
        # only when no PID was registered (e.g. unit test setting `_playwright_cm` directly).
        if not pdf_roots and (cm is not None or browser is not None):
            pub_pids = _extract_public_pids(cm, browser)
            if pub_pids:
                pdf_roots.update(pub_pids)
            else:
                fallback_pid = _extract_playwright_driver_pid(cm, browser)
                if fallback_pid is not None:
                    pdf_roots.add(fallback_pid)
            if pdf_roots and pdf_driver_pid is None:
                pdf_driver_pid = min(pdf_roots)
    except Exception:
        pass

    return ConcurrencySnapshot(
        crawler_browser_active=crawler_active,
        crawler_browser_limit=crawler_limit,
        crawler_queue_active=crawler_queue,
        pdf_render_active=pdf_active,
        pdf_render_limit=pdf_limit,
        pdf_driver_pid=pdf_driver_pid,
        pdf_root_pids=tuple(sorted(pdf_roots)),
        crawler_root_pids=tuple(sorted(crawler_roots)),
    )


def _read_int_or_max(path: Path) -> tuple[bool, int | None]:
    """Return ``(file_exists, byte_value_or_none_if_unlimited)``."""
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return False, None
    if not raw:
        return True, None
    first = raw.splitlines()[0].strip().lower()
    if first == "max":
        return True, None
    try:
        val = int(first)
    except ValueError:
        return True, None
    if val <= 0 or val >= _UNLIMITED_CGROUP_BYTES:
        return True, None
    return True, val


def read_cgroup_memory(
    cgroup_root: Path,
    *,
    fallback_usage_bytes: int | None = None,
) -> CgroupMemory:
    """Read cgroup v2 (`memory.current`/`memory.max`) or v1 equivalents."""
    v2_cur_exists, v2_usage = _read_int_or_max(cgroup_root / "memory.current")
    v2_max_exists, v2_limit = _read_int_or_max(cgroup_root / "memory.max")
    if v2_cur_exists or v2_max_exists:
        eff_usage = v2_usage if v2_usage is not None else fallback_usage_bytes
        ratio = (
            (eff_usage / v2_limit)
            if (eff_usage is not None and v2_limit is not None and v2_limit > 0)
            else None
        )
        return CgroupMemory(
            version="v2",
            usage_bytes=v2_usage,
            limit_bytes=v2_limit,
            usage_ratio=ratio,
        )

    for sub in (cgroup_root / "memory", cgroup_root):
        v1_cur_exists, v1_usage = _read_int_or_max(sub / "memory.usage_in_bytes")
        v1_lim_exists, v1_limit = _read_int_or_max(sub / "memory.limit_in_bytes")
        if v1_cur_exists or v1_lim_exists:
            eff_usage = v1_usage if v1_usage is not None else fallback_usage_bytes
            ratio = (
                (eff_usage / v1_limit)
                if (eff_usage is not None and v1_limit is not None and v1_limit > 0)
                else None
            )
            return CgroupMemory(
                version="v1",
                usage_bytes=v1_usage,
                limit_bytes=v1_limit,
                usage_ratio=ratio,
            )

    return CgroupMemory(version=None, usage_bytes=None, limit_bytes=None, usage_ratio=None)


def _read_one_proc(pid_dir: Path, pid: int, page_size: int) -> _ProcRecord | None:
    """Read comm, ppid, cmdline, and PSS (or RSS fallback) for a single `/proc/<pid>`."""
    comm = ""
    ppid: int | None = None
    stat_rss_bytes: int | None = None

    try:
        stat_raw = (pid_dir / "stat").read_text(encoding="utf-8", errors="replace")
        left, right = stat_raw.rsplit(")", 1)
        comm = left.split("(", 1)[1].strip()
        rest = right.split()
        ppid = int(rest[1])
        if len(rest) > 21:
            stat_rss_pages = int(rest[21])
            if stat_rss_pages >= 0:
                stat_rss_bytes = stat_rss_pages * page_size
    except (FileNotFoundError, PermissionError, OSError, ValueError, IndexError):
        pass

    try:
        comm_file = (pid_dir / "comm").read_text(encoding="utf-8", errors="replace").strip()
        if comm_file:
            comm = comm_file
    except (FileNotFoundError, PermissionError, OSError):
        pass

    status_rss_bytes: int | None = None
    try:
        status_raw = (pid_dir / "status").read_text(encoding="utf-8", errors="replace")
        if not comm:
            m_name = _NAME_STATUS_RE.search(status_raw)
            if m_name:
                comm = m_name.group(1).strip()
        if ppid is None:
            m_ppid = _PPID_STATUS_RE.search(status_raw)
            if m_ppid:
                ppid = int(m_ppid.group(1))
        m_rss = _VMRSS_RE.search(status_raw)
        if m_rss:
            status_rss_bytes = int(m_rss.group(1)) * 1024
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass

    cmdline = ""
    try:
        raw_cmd = (pid_dir / "cmdline").read_bytes()
        if raw_cmd:
            cmdline = raw_cmd.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
    except (FileNotFoundError, PermissionError, OSError):
        pass

    # Prefer PSS from smaps_rollup; fall back to RSS (status VmRSS -> smaps_rollup Rss -> statm -> stat).
    memory_bytes: int | None = None
    used_pss = False
    rollup_rss_bytes: int | None = None
    try:
        rollup_raw = (pid_dir / "smaps_rollup").read_text(
            encoding="utf-8", errors="replace"
        )
        m_pss = _PSS_RE.search(rollup_raw)
        if m_pss:
            memory_bytes = int(m_pss.group(1)) * 1024
            used_pss = True
        else:
            m_rollup_rss = _RSS_ROLLUP_RE.search(rollup_raw)
            if m_rollup_rss:
                rollup_rss_bytes = int(m_rollup_rss.group(1)) * 1024
    except (FileNotFoundError, PermissionError, OSError, ValueError):
        pass

    if memory_bytes is None:
        if status_rss_bytes is not None:
            memory_bytes = status_rss_bytes
        elif rollup_rss_bytes is not None:
            memory_bytes = rollup_rss_bytes
        else:
            try:
                statm_parts = (
                    (pid_dir / "statm")
                    .read_text(encoding="utf-8", errors="replace")
                    .split()
                )
                if len(statm_parts) >= 2:
                    memory_bytes = int(statm_parts[1]) * page_size
            except (FileNotFoundError, PermissionError, OSError, ValueError):
                pass
        if memory_bytes is None and stat_rss_bytes is not None:
            memory_bytes = stat_rss_bytes

    if memory_bytes is None:
        return None

    return _ProcRecord(
        pid=pid,
        ppid=ppid if ppid is not None else 0,
        comm=comm,
        cmdline=cmdline,
        memory_bytes=max(0, memory_bytes),
        used_pss=used_pss,
    )


def _is_driver_proc(proc: _ProcRecord) -> bool:
    """True if ``proc`` is a Playwright/Patchright Node ``run-driver`` process."""
    comm_l = proc.comm.lower()
    cmd_l = proc.cmdline.lower()
    if "run-driver" in cmd_l and (
        "playwright" in cmd_l or "patchright" in cmd_l or comm_l == "node"
    ):
        return True
    if "playwright/driver" in cmd_l or "patchright/driver" in cmd_l:
        return True
    return False


def _is_browser_or_driver(proc: _ProcRecord) -> bool:
    """True if ``proc`` is a Chromium/Firefox/Camoufox binary or Playwright Node driver."""
    comm_l = proc.comm.lower()
    cmd_l = proc.cmdline.lower()
    first_tok = cmd_l.split()[0] if cmd_l else ""
    exe_base = os.path.basename(first_tok)

    if comm_l.startswith(_BROWSER_BIN_PREFIXES) or exe_base.startswith(
        _BROWSER_BIN_PREFIXES
    ):
        return True
    if "chrome-linux/chrome" in first_tok or "chrome-headless-shell" in first_tok:
        return True
    return _is_driver_proc(proc)


def _exclusive_cmdline_role(
    proc: _ProcRecord,
) -> Literal["crawler", "pdf_render"] | None:
    """Match ONLY genuinely exclusive command-line markers on unrooted processes.

    Used only when a process has NO known ancestor in ``pdf_root_pids`` or
    ``crawler_root_pids``. Common Playwright flags (``--user-data-dir=``,
    ``--no-service-autorun``, ``--disable-infobars``, ``--headless``) are never
    matched here; if no exclusive marker is present, returns ``None`` so the
    caller classifies the process as ``other`` rather than guessing.
    """
    if not _is_browser_or_driver(proc):
        return None

    comm_l = proc.comm.lower()
    cmd_l = proc.cmdline.lower()
    first_tok = cmd_l.split()[0] if cmd_l else ""
    exe_base = os.path.basename(first_tok)

    if any(marker in cmd_l for marker in _PDF_RENDER_EXCLUSIVE_CMDLINE_MARKERS):
        return "pdf_render"
    if comm_l.startswith(("camoufox", "firefox")) or exe_base.startswith(
        ("camoufox", "firefox")
    ):
        return "crawler"
    if any(marker in cmd_l for marker in _CRAWLER_EXCLUSIVE_CMDLINE_MARKERS):
        return "crawler"
    return None


def _classify_processes(
    procs: dict[int, _ProcRecord],
    *,
    backend_pid: int,
    pdf_root_pids: set[int],
    crawler_root_pids: set[int],
) -> dict[int, BucketRole]:
    """Classify each PID into ``crawler``, ``pdf_render``, or ``other``.

    Attribution strictly prioritizes the parent-PID chain from known roots:
    1. **Parent-PID chain first**: every process whose ``ppid`` ancestor chain
       reaches ``pdf_root_pids`` is attributed to ``pdf_render``; every process
       whose ancestor chain reaches ``crawler_root_pids`` is attributed to
       ``crawler``. Known root ancestry unconditionally overrides any command-line
       flags on child processes.
    2. **Exclusive command-line markers for unrooted processes only**: processes
       with no known root ancestor are checked for strictly exclusive Scrapling
       or ``pdf_render`` markers (never generic Playwright switches like
       ``--user-data-dir=``). When an unrooted browser main process matches an
       exclusive marker, its unrooted child helpers and immediate unrooted
       ``run-driver`` parent inherit that role.
    3. **Conservative fallback to ``other``**: any remaining process with neither
       a known root ancestor nor an exclusive command-line marker is classified
       as ``other`` (never guessed as ``crawler``).
    """
    stop_pids = {0, 1, backend_pid}
    roles: dict[int, BucketRole] = {}

    for pid in stop_pids:
        if pid in procs:
            roles[pid] = "other"

    def _known_root_ancestor_role(
        start_pid: int,
    ) -> Literal["crawler", "pdf_render"] | None:
        visited: set[int] = set()
        cur = start_pid
        while cur not in stop_pids and cur not in visited:
            visited.add(cur)
            if cur in pdf_root_pids:
                return "pdf_render"
            if cur in crawler_root_pids:
                return "crawler"
            rec = procs.get(cur)
            if rec is None:
                break
            cur = rec.ppid
        return None

    # Phase 1: Parent-PID chain from known roots (strictly highest priority).
    for pid in procs:
        if pid in roles:
            continue
        root_role = _known_root_ancestor_role(pid)
        if root_role is not None:
            roles[pid] = root_role

    # Phase 2: Exclusive command-line markers ONLY for processes with no known root ancestor.
    unrooted_seeds: dict[int, Literal["crawler", "pdf_render"]] = {}
    for pid, proc in procs.items():
        if pid in roles:
            continue
        cmd_role = _exclusive_cmdline_role(proc)
        if cmd_role is not None:
            unrooted_seeds[pid] = cmd_role
            roles[pid] = cmd_role

    def _unrooted_ancestor_role(
        start_pid: int,
    ) -> Literal["crawler", "pdf_render"] | None:
        visited: set[int] = set()
        cur = start_pid
        while cur not in stop_pids and cur not in visited:
            visited.add(cur)
            seed_role = unrooted_seeds.get(cur)
            if seed_role is not None:
                return seed_role
            if cur in roles:
                # Reached a node already claimed by Phase 1 or stop_pids.
                return None
            rec = procs.get(cur)
            if rec is None:
                break
            cur = rec.ppid
        return None

    # Propagate down from unrooted exclusive-cmdline seeds to their helper children.
    for pid in procs:
        if pid in roles:
            continue
        anc_role = _unrooted_ancestor_role(pid)
        if anc_role is not None:
            roles[pid] = anc_role

    # Propagate up from an unrooted exclusive-cmdline browser child to its immediate
    # unrooted Playwright `run-driver` parent, then down to any unrooted siblings.
    for pid, seed_role in list(unrooted_seeds.items()):
        proc = procs.get(pid)
        if proc is None:
            continue
        parent_pid = proc.ppid
        if parent_pid in stop_pids or parent_pid in roles:
            continue
        parent = procs.get(parent_pid)
        if parent is not None and _is_driver_proc(parent):
            unrooted_seeds[parent_pid] = seed_role
            roles[parent_pid] = seed_role

    for pid in procs:
        if pid in roles:
            continue
        anc_role = _unrooted_ancestor_role(pid)
        if anc_role is not None:
            roles[pid] = anc_role

    # Phase 3: Any process with neither a known root ancestor nor exclusive markers -> `other`.
    for pid in procs:
        if pid not in roles:
            roles[pid] = "other"

    return roles


def collect_browser_memory_sample(
    *,
    proc_root: Path = Path("/proc"),
    cgroup_root: Path = Path("/sys/fs/cgroup"),
    backend_pid: int | None = None,
    pdf_root_pids: set[int] | None = None,
    crawler_root_pids: set[int] | None = None,
    concurrency: ConcurrencySnapshot | None = None,
    warn_ratio: float = _DEFAULT_WARN_RATIO,
    now_monotonic: float | None = None,
    now_iso: str | None = None,
) -> BrowserMemorySample:
    """Synchronous `/proc` + cgroup scan returning a structured 3-way sample."""
    eff_backend_pid = backend_pid if backend_pid is not None else os.getpid()
    conc = concurrency if concurrency is not None else read_live_concurrency()
    eff_pdf_roots: set[int] = (
        set(pdf_root_pids or ())
        | get_registered_pdf_render_pids()
        | set(conc.pdf_root_pids)
    )
    if conc.pdf_driver_pid is not None:
        eff_pdf_roots.add(conc.pdf_driver_pid)
    eff_crawler_roots: set[int] = (
        (
            set(crawler_root_pids or ())
            | get_registered_crawler_pids()
            | set(conc.crawler_root_pids)
        )
        - eff_pdf_roots
    )

    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        if page_size <= 0:
            page_size = _DEFAULT_PAGE_SIZE
    except (AttributeError, ValueError, OSError):
        page_size = _DEFAULT_PAGE_SIZE

    procs: dict[int, _ProcRecord] = {}
    for entry in proc_root.iterdir():
        name = entry.name
        if not name.isdigit():
            continue
        pid = int(name)
        rec = _read_one_proc(entry, pid, page_size)
        if rec is not None:
            procs[pid] = rec

    roles = _classify_processes(
        procs,
        backend_pid=eff_backend_pid,
        pdf_root_pids=eff_pdf_roots,
        crawler_root_pids=eff_crawler_roots,
    )

    bucket_bytes: dict[BucketRole, int] = {"crawler": 0, "pdf_render": 0, "other": 0}
    bucket_pids: dict[BucketRole, list[int]] = {
        "crawler": [],
        "pdf_render": [],
        "other": [],
    }
    pss_count = 0
    rss_count = 0

    for pid in sorted(procs):
        rec = procs[pid]
        role = roles.get(pid, "other")
        bucket_bytes[role] += rec.memory_bytes
        bucket_pids[role].append(pid)
        if rec.used_pss:
            pss_count += 1
        else:
            rss_count += 1

    if pss_count > 0 and rss_count == 0:
        metric: MetricSource = "pss"
    elif rss_count > 0 and pss_count == 0:
        metric = "rss"
    elif pss_count > 0 and rss_count > 0:
        metric = "pss+rss"
    else:
        metric = "pss"

    total_proc_bytes = (
        bucket_bytes["crawler"] + bucket_bytes["pdf_render"] + bucket_bytes["other"]
    )
    total_procs = len(procs)

    cgroup = read_cgroup_memory(
        cgroup_root,
        fallback_usage_bytes=total_proc_bytes,
    )
    warn_triggered = (
        cgroup.usage_ratio is not None and cgroup.usage_ratio >= warn_ratio
    )

    def _build_bucket(role: BucketRole) -> BucketMetrics:
        b = bucket_bytes[role]
        pids = tuple(bucket_pids[role])
        return BucketMetrics(
            bytes=b,
            mb=round(b / _MIB, 2),
            procs=len(pids),
            pids=pids,
        )

    return BrowserMemorySample(
        ts=now_iso or datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        monotonic_ts=now_monotonic if now_monotonic is not None else time.monotonic(),
        metric=metric,
        rss_fallback_procs=rss_count,
        crawler=_build_bucket("crawler"),
        pdf_render=_build_bucket("pdf_render"),
        other=_build_bucket("other"),
        total_proc_bytes=total_proc_bytes,
        total_procs=total_procs,
        cgroup=cgroup,
        concurrency=conc,
        warn_triggered=warn_triggered,
    )


class BrowserMemorySampler:
    """Per-worker background sampler retaining 24-hour rolling per-road peaks.

    Scope note: 统计范围为当前 worker 进程启动的浏览器 + 容器级总量（不做跨 worker 汇总）。
    Peaks are retained in 1-minute time buckets (up to 1,440 buckets for 24h)
    so short sampling intervals (e.g. 10s) never evict spikes before 24 hours.
    """

    _instance: BrowserMemorySampler | None = None

    def __init__(
        self,
        *,
        interval_s: float | None = None,
        warn_ratio: float = _DEFAULT_WARN_RATIO,
        window_s: float = _DEFAULT_WINDOW_S,
        proc_root: Path | None = None,
        cgroup_root: Path | None = None,
        platform: str | None = None,
        backend_pid: int | None = None,
        pdf_root_pids: set[int] | None = None,
        crawler_root_pids: set[int] | None = None,
        concurrency_provider: Callable[[], ConcurrencySnapshot] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._interval_s = _resolve_interval_s(interval_s)
        self._warn_ratio = warn_ratio
        self._window_s = window_s
        # One bucket more than the window holds: the oldest bucket only goes
        # once every sample in it is older than the window, so a peak 86350 s
        # old is still reported.
        self._max_buckets = (
            max(_MAX_BUCKETS_24H, int(math.ceil(self._window_s / _BUCKET_SPAN_S))) + 1
        )
        self._custom_proc_root = proc_root is not None
        self._proc_root = proc_root if proc_root is not None else Path("/proc")
        self._cgroup_root = (
            cgroup_root if cgroup_root is not None else Path("/sys/fs/cgroup")
        )
        if platform is not None:
            self._effective_platform = platform
        elif self._custom_proc_root:
            self._effective_platform = "linux"
        else:
            self._effective_platform = sys.platform
        self._backend_pid = backend_pid
        self._pdf_root_pids = set(pdf_root_pids or ())
        self._crawler_root_pids = set(crawler_root_pids or ())
        self._concurrency_provider = concurrency_provider or read_live_concurrency
        self._clock = clock or time.monotonic

        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._buckets: deque[_MinutePeakBucket] = deque()
        self._latest: BrowserMemorySample | None = None
        self.degraded_non_linux = False
        self._logged_non_linux = False

    @classmethod
    def get_instance(cls) -> BrowserMemorySampler:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _is_proc_available(self) -> bool:
        try:
            return self._effective_platform.startswith("linux") and (
                self._proc_root.is_dir()
            )
        except Exception:
            return False

    def _degrade_if_non_linux(self) -> bool:
        """Return True and log one INFO line when `/proc` is unavailable."""
        if self._is_proc_available():
            self.degraded_non_linux = False
            return False
        self.degraded_non_linux = True
        if not self._logged_non_linux:
            self._logged_non_linux = True
            logger.info(
                "[browser-memory] /proc unavailable (platform=%s, proc_root=%s); "
                "browser memory sampler degraded to no-op",
                self._effective_platform,
                self._proc_root,
            )
        return True

    def start(self) -> None:
        if not _is_sampler_enabled_env():
            logger.info("[browser-memory] sampler disabled by environment variable")
            return
        if self._degrade_if_non_linux():
            return
        if self._task is not None and not self._task.done():
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="browser-memory-sampler")

    async def stop(self) -> None:
        self._stopping.set()
        task, self._task = self._task, None
        if task is not None:
            await cancel_and_join(task)

    def _record_into_bucket(
        self, sample: BrowserMemorySample, now_monotonic: float
    ) -> None:
        cutoff = now_monotonic - self._window_s
        bucket_id = int(math.floor(sample.monotonic_ts / _BUCKET_SPAN_S))
        if self._buckets and self._buckets[-1].bucket_id == bucket_id:
            self._buckets[-1].update(sample, cutoff=cutoff)
        else:
            self._buckets.append(_MinutePeakBucket.from_sample(bucket_id, sample))
        self._prune_expired(now_monotonic)

    def _prune_expired(self, now_monotonic: float) -> None:
        cutoff = now_monotonic - self._window_s
        while self._buckets and self._buckets[0].max_monotonic_ts < cutoff:
            self._buckets.popleft()
        while len(self._buckets) > self._max_buckets:
            self._buckets.popleft()

    def _collect_sync(self, now_monotonic: float) -> BrowserMemorySample:
        conc = self._concurrency_provider()
        return collect_browser_memory_sample(
            proc_root=self._proc_root,
            cgroup_root=self._cgroup_root,
            backend_pid=self._backend_pid,
            pdf_root_pids=self._pdf_root_pids,
            crawler_root_pids=self._crawler_root_pids,
            concurrency=conc,
            warn_ratio=self._warn_ratio,
            now_monotonic=now_monotonic,
        )

    async def sample_once(
        self,
        *,
        now_monotonic: float | None = None,
    ) -> BrowserMemorySample | None:
        """Run one sample, log it, and update 24h rolling minute buckets. Never raises."""
        try:
            if self._degrade_if_non_linux():
                return None
            mono = now_monotonic if now_monotonic is not None else self._clock()
            sample = await asyncio.to_thread(self._collect_sync, mono)
            self._latest = sample
            self._record_into_bucket(sample, mono)

            payload_json = json.dumps(
                sample.to_log_dict(),
                separators=(",", ":"),
                ensure_ascii=False,
            )
            if sample.warn_triggered:
                pct = (
                    round(sample.cgroup.usage_ratio * 100.0, 2)
                    if sample.cgroup.usage_ratio is not None
                    else 0.0
                )
                logger.warning(
                    "[browser-memory] container memory at %.2f%% of cgroup limit "
                    "(threshold %.0f%%): %s",
                    pct,
                    self._warn_ratio * 100.0,
                    payload_json,
                )
            else:
                logger.info("[browser-memory] %s", payload_json)
            return sample
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[browser-memory] sample failed (ignored): %s", exc)
            return None

    async def _run(self) -> None:
        while not self._stopping.is_set():
            await self.sample_once()
            try:
                await asyncio.sleep(self._interval_s)
            except asyncio.CancelledError:
                return

    def get_peaks_24h(self, *, now_monotonic: float | None = None) -> dict[str, Any]:
        """Return rolling 24h peak metrics across 1-minute buckets (up to 1,440 buckets)."""
        mono = now_monotonic if now_monotonic is not None else self._clock()
        self._prune_expired(mono)
        # Minute granularity: a bucket counts while any sample in it is inside
        # the window, so a bucket's peak is never dropped while a lower sample
        # of the same minute would still be reported.
        cutoff = mono - self._window_s

        crawler_candidates = [
            b.crawler_peak
            for b in self._buckets
            if b.max_monotonic_ts >= cutoff
        ]
        pdf_candidates = [
            b.pdf_render_peak
            for b in self._buckets
            if b.max_monotonic_ts >= cutoff
        ]
        other_candidates = [
            b.other_peak
            for b in self._buckets
            if b.max_monotonic_ts >= cutoff
        ]
        total_candidates = [
            b.total_proc_peak
            for b in self._buckets
            if b.max_monotonic_ts >= cutoff
        ]
        cgroup_candidates = [
            b.cgroup_peak
            for b in self._buckets
            if b.cgroup_peak is not None
            and b.max_monotonic_ts >= cutoff
            and b.cgroup_peak.cgroup.usage_bytes is not None
        ]

        if not crawler_candidates:
            return {
                "crawler": None,
                "pdf_render": None,
                "other": None,
                "total_proc": None,
                "cgroup_usage": None,
            }

        crawler_peak = max(
            crawler_candidates, key=lambda s: (s.crawler.bytes, s.monotonic_ts)
        )
        pdf_peak = max(
            pdf_candidates, key=lambda s: (s.pdf_render.bytes, s.monotonic_ts)
        )
        other_peak = max(
            other_candidates, key=lambda s: (s.other.bytes, s.monotonic_ts)
        )
        total_peak = max(
            total_candidates, key=lambda s: (s.total_proc_bytes, s.monotonic_ts)
        )
        cgroup_peak = (
            max(
                cgroup_candidates,
                key=lambda s: (s.cgroup.usage_bytes or 0, s.monotonic_ts),
            )
            if cgroup_candidates
            else None
        )

        return {
            "crawler": {
                "bytes": crawler_peak.crawler.bytes,
                "mb": crawler_peak.crawler.mb,
                "procs": crawler_peak.crawler.procs,
                "ts": crawler_peak.ts,
                "crawler_browser_active": crawler_peak.concurrency.crawler_browser_active,
                "cgroup_usage_bytes": crawler_peak.cgroup.usage_bytes,
            },
            "pdf_render": {
                "bytes": pdf_peak.pdf_render.bytes,
                "mb": pdf_peak.pdf_render.mb,
                "procs": pdf_peak.pdf_render.procs,
                "ts": pdf_peak.ts,
                "pdf_render_active": pdf_peak.concurrency.pdf_render_active,
                "cgroup_usage_bytes": pdf_peak.cgroup.usage_bytes,
            },
            "other": {
                "bytes": other_peak.other.bytes,
                "mb": other_peak.other.mb,
                "procs": other_peak.other.procs,
                "ts": other_peak.ts,
                "cgroup_usage_bytes": other_peak.cgroup.usage_bytes,
            },
            "total_proc": {
                "bytes": total_peak.total_proc_bytes,
                "mb": round(total_peak.total_proc_bytes / _MIB, 2),
                "procs": total_peak.total_procs,
                "ts": total_peak.ts,
            },
            "cgroup_usage": (
                {
                    "bytes": cgroup_peak.cgroup.usage_bytes,
                    "mb": (
                        round(cgroup_peak.cgroup.usage_bytes / _MIB, 2)
                        if cgroup_peak.cgroup.usage_bytes is not None
                        else None
                    ),
                    "limit_bytes": cgroup_peak.cgroup.limit_bytes,
                    "limit_mb": (
                        round(cgroup_peak.cgroup.limit_bytes / _MIB, 2)
                        if cgroup_peak.cgroup.limit_bytes is not None
                        else None
                    ),
                    "usage_ratio": (
                        round(cgroup_peak.cgroup.usage_ratio, 4)
                        if cgroup_peak.cgroup.usage_ratio is not None
                        else None
                    ),
                    "usage_pct": (
                        round(cgroup_peak.cgroup.usage_ratio * 100.0, 2)
                        if cgroup_peak.cgroup.usage_ratio is not None
                        else None
                    ),
                    "ts": cgroup_peak.ts,
                }
                if cgroup_peak is not None
                else None
            ),
        }

    def get_diagnostics(self, *, now_monotonic: float | None = None) -> dict[str, Any]:
        """Read-only diagnostic snapshot with worker scope note, latest sample, and 24h peaks."""
        mono = now_monotonic if now_monotonic is not None else self._clock()
        self._prune_expired(mono)
        if self.degraded_non_linux:
            status = "disabled_non_linux"
        elif self._latest is not None:
            status = "ok"
        else:
            status = "pending_first_sample"

        return {
            "status": status,
            "scope": _DIAGNOSTICS_SCOPE_NOTE,
            "worker_pid": (
                self._backend_pid if self._backend_pid is not None else os.getpid()
            ),
            "platform": self._effective_platform,
            "interval_s": self._interval_s,
            "warn_ratio": self._warn_ratio,
            "window_seconds": int(self._window_s),
            "bucket_span_seconds": int(_BUCKET_SPAN_S),
            "max_buckets_24h": self._max_buckets,
            "bucket_count_24h": len(self._buckets),
            "sample_count_24h": sum(b.sample_count for b in self._buckets),
            "latest": self._latest.to_log_dict() if self._latest is not None else None,
            "peaks_24h": self.get_peaks_24h(now_monotonic=mono),
        }


def get_browser_memory_diagnostics() -> dict[str, Any]:
    """Return the singleton sampler's read-only 24-hour peaks and latest sample."""
    return BrowserMemorySampler.get_instance().get_diagnostics()
