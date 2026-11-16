"""Registered data-job -> host directory -> monitor bindings and contained FileSource."""

from __future__ import annotations

import json
import os
import stat as stat_mod
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from html import escape as html_escape
from pathlib import Path

from src.dalton.monitors.models import MonitorCategory, MonitorVisibility
from src.server.services.served_paths import may_be_artifact

MAX_HOST_FILE_BYTES = 25 * 1024 * 1024
_DIR_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
_FILE_OPEN_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

# Built-in bilingual demo UI copy kept in Python so backend container images
# (which do not bundle web/src/locales) never degrade to fallback text.
_DEMO_COPY: dict[str, dict[str, str]] = {
    "zh-CN": {
        "title": "实时数据监控示例",
        "waiting": "等待数据刷新…",
        "unavailable": "数据暂不可用",
        "dataAsOfPrefix": "数据时间：",
        "requestFailed": "请求失败",
    },
    "en-US": {
        "title": "Realtime Monitor Demo",
        "waiting": "Waiting for data refresh…",
        "unavailable": "Data unavailable",
        "dataAsOfPrefix": "Data as of: ",
        "requestFailed": "Request failed",
    },
}


def _load_demo_copy(locale: str = "zh-CN") -> dict[str, str]:
    clean = "en-US" if locale.lower().startswith("en") else "zh-CN"
    return dict(_DEMO_COPY[clean])


@dataclass(frozen=True)
class MonitorDataJobBinding:
    """Pre-registered mapping from a deterministic data job to a host output folder."""

    job_key: str
    slug: str
    title: str
    category: MonitorCategory
    visibility: MonitorVisibility
    rel_dir: str
    entry_file: str = "index.html"
    data_keys: tuple[str, ...] = ()
    # Required when visibility == "public": the approving administrator whose current
    # admin tier is re-verified on every public data-job republish.
    approved_by_admin_user_id: str | None = None


# Only pre-registered job_keys and their fixed relative output folders under
# ``data_root`` may be published by data tasks. Arbitrary caller paths are rejected.
MONITOR_DATA_JOB_REGISTRY: dict[str, MonitorDataJobBinding] = {
    "monitor-live-demo": MonitorDataJobBinding(
        job_key="monitor-live-demo",
        slug="live-demo",
        title=_DEMO_COPY["zh-CN"]["title"],
        category="overview",
        visibility="company",
        rel_dir="research-outputs/monitor-demo",
        entry_file="index.html",
        data_keys=("metrics",),
    ),
}


def get_registered_monitor_job(job_key: str) -> MonitorDataJobBinding | None:
    return MONITOR_DATA_JOB_REGISTRY.get(str(job_key or "").strip())


def _split_clean_rel_parts(raw_rel: str, *, allow_leading_slash: bool = False) -> list[str] | None:
    text = str(raw_rel or "").strip()
    if allow_leading_slash:
        text = text.lstrip("/")
    elif text.startswith("/"):
        return None
    if not text or "\\" in text or "\x00" in text:
        return None
    parts = text.split("/")
    if any(seg in ("", ".", "..") for seg in parts):
        return None
    return parts


def _open_dir_chain_fd(base_dir: Path, rel_segments: list[str]) -> int:
    """Open ``base_dir`` once and descend ``rel_segments`` using ``dir_fd`` + ``O_NOFOLLOW``.

    Returns an open directory descriptor pinned to the leaf directory inode so later
    enumeration/stat/read operations never re-traverse parent paths.
    """
    if base_dir.is_symlink():
        raise ValueError(f"Symlink not allowed at base directory: {base_dir}")
    cur = base_dir
    for seg in rel_segments:
        cur = cur / seg
        if cur.is_symlink():
            raise ValueError(f"Symlink not allowed in registered path: {seg}")

    fd = os.open(base_dir, _DIR_OPEN_FLAGS)
    try:
        for seg in rel_segments:
            next_fd = os.open(seg, _DIR_OPEN_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _descend_dir_fd(base_fd: int, rel_segments: list[str]) -> int:
    """Duplicate ``base_fd`` and descend ``rel_segments`` using ``dir_fd`` + ``O_NOFOLLOW``."""
    fd = os.dup(base_fd)
    try:
        for seg in rel_segments:
            next_fd = os.open(seg, _DIR_OPEN_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def resolve_registered_job_dir(data_root: Path, binding: MonitorDataJobBinding) -> Path:
    """Validate each segment of ``binding.rel_dir`` inside ``data_root`` without following symlinks."""
    parts = _split_clean_rel_parts(binding.rel_dir, allow_leading_slash=False)
    if not parts:
        raise ValueError(f"Invalid registered monitor relative dir: {binding.rel_dir}")
    try:
        fd = _open_dir_chain_fd(data_root, parts)
        os.close(fd)
    except ValueError:
        raise
    except OSError as exc:
        raise ValueError(
            f"Registered monitor dir unavailable or contains symlink ({exc}): {binding.rel_dir}"
        ) from exc
    return data_root.joinpath(*parts)


class HostDirFileSource:
    """Containment-enforced ``FileSource`` holding an open ``dir_fd`` with ``O_NOFOLLOW`` traversal."""

    def __init__(self, root_dir: Path, *, _root_fd: int | None = None) -> None:
        if _root_fd is not None:
            self._root = root_dir
            self._root_fd: int | None = _root_fd
        else:
            if root_dir.is_symlink():
                raise ValueError("Monitor source root directory must not be a symlink")
            self._root = root_dir
            self._root_fd = os.open(root_dir, _DIR_OPEN_FLAGS)

    @classmethod
    def open_registered(
        cls, data_root: Path, binding: MonitorDataJobBinding
    ) -> HostDirFileSource:
        """Validate ``binding.rel_dir`` from trusted ``data_root`` and keep the opened leaf ``dir_fd``."""
        parts = _split_clean_rel_parts(binding.rel_dir, allow_leading_slash=False)
        if not parts:
            raise ValueError(f"Invalid registered monitor relative dir: {binding.rel_dir}")
        try:
            fd = _open_dir_chain_fd(data_root, parts)
        except ValueError:
            raise
        except OSError as exc:
            raise ValueError(
                f"Registered monitor dir unavailable or contains symlink ({exc}): {binding.rel_dir}"
            ) from exc
        return cls(data_root.joinpath(*parts), _root_fd=fd)

    def close(self) -> None:
        if self._root_fd is not None:
            try:
                os.close(self._root_fd)
            except OSError:
                pass
            self._root_fd = None

    def __enter__(self) -> HostDirFileSource:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    def _require_root_fd(self) -> int:
        if self._root_fd is None:
            raise ValueError("HostDirFileSource is closed")
        return self._root_fd

    @contextmanager
    def _open_parent_fd(self, rel_path: str) -> Iterator[tuple[int, str, str] | None]:
        parts = _split_clean_rel_parts(rel_path, allow_leading_slash=False)
        if not parts:
            yield None
            return
        canonical = "/".join(parts)
        dir_parts, leaf = parts[:-1], parts[-1]
        dir_fd: int | None = None
        try:
            root_fd = self._require_root_fd()
            dir_fd = _descend_dir_fd(root_fd, dir_parts)
        except (OSError, ValueError):
            yield None
            return
        try:
            yield dir_fd, leaf, canonical
        finally:
            try:
                os.close(dir_fd)
            except OSError:
                pass

    def list_artifact_files(self) -> tuple[str, ...]:
        """List relative paths of regular artifact-eligible files strictly via ``dir_fd``."""
        root_fd = self._require_root_fd()
        found: list[str] = []

        def _walk_fd(dir_fd: int, prefix: str) -> None:
            for name in sorted(os.listdir(dir_fd)):
                if name.startswith("."):
                    continue
                try:
                    st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    continue
                if stat_mod.S_ISLNK(st.st_mode):
                    continue
                rel = f"{prefix}/{name}" if prefix else name
                if stat_mod.S_ISDIR(st.st_mode):
                    try:
                        child_fd = os.open(name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
                    except OSError:
                        continue
                    try:
                        _walk_fd(child_fd, rel)
                    finally:
                        os.close(child_fd)
                elif stat_mod.S_ISREG(st.st_mode) and may_be_artifact(rel):
                    found.append(rel)

        _walk_fd(root_fd, "")
        return tuple(found)

    async def stat(self, paths: list[str]) -> dict[str, tuple[str, int]]:
        hits: dict[str, tuple[str, int]] = {}
        for req in paths:
            with self._open_parent_fd(req) as target:
                if target is None:
                    continue
                dir_fd, leaf, canonical = target
                if not may_be_artifact(canonical):
                    continue
                try:
                    st = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
                except OSError:
                    continue
                if not stat_mod.S_ISREG(st.st_mode):
                    continue
                hits[req] = (canonical, int(st.st_size))
        return hits

    async def read(self, path: str) -> bytes | None:
        with self._open_parent_fd(path) as target:
            if target is None:
                return None
            dir_fd, leaf, canonical = target
            if not may_be_artifact(canonical):
                return None
            try:
                file_fd = os.open(leaf, _FILE_OPEN_FLAGS, dir_fd=dir_fd)
            except OSError:
                return None
            try:
                st = os.fstat(file_fd)
                if not stat_mod.S_ISREG(st.st_mode) or st.st_size > MAX_HOST_FILE_BYTES:
                    os.close(file_fd)
                    return None
                with os.fdopen(file_fd, "rb") as fh:
                    data = fh.read(MAX_HOST_FILE_BYTES + 1)
                if len(data) > MAX_HOST_FILE_BYTES:
                    return None
                return data
            except Exception:
                try:
                    os.close(file_fd)
                except OSError:
                    pass
                return None


def build_live_demo_html(*, data_key: str = "metrics", locale: str = "zh-CN") -> str:
    """Return a self-contained HTML monitor page polling realtime data with ETag/304 & grant renewal."""
    copy = _load_demo_copy(locale)
    lang = "en" if locale.lower().startswith("en") else "zh-CN"
    title_html = html_escape(copy["title"])
    waiting_html = html_escape(copy["waiting"])
    unavailable_js = json_str(copy["unavailable"])
    as_of_prefix_js = json_str(copy["dataAsOfPrefix"])
    request_failed_js = json_str(copy["requestFailed"])
    return f"""<!doctype html>
<html lang="{lang}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{title_html}</title>
  <style>
    :root {{
      color-scheme: light dark;
    }}
    body {{
      font-family: var(--font-ui, system-ui, -apple-system, sans-serif);
      background: var(--color-bg-page);
      color: var(--color-text-primary);
      margin: 0;
      padding: 24px;
    }}
    .card {{
      border: 1px solid var(--color-border-muted);
      background: var(--color-bg-card);
      border-radius: 10px;
      padding: 16px 20px;
      max-width: 720px;
    }}
    .meta {{
      font-size: 12px;
      color: var(--color-text-tertiary);
      margin-top: 6px;
    }}
    pre {{
      font-family: var(--font-mono, monospace);
      font-size: 12px;
      overflow-x: auto;
      margin-top: 12px;
    }}
  </style>
</head>
<body>
  <div class="card">
    <h1 style="font-size:16px;margin:0">{title_html}</h1>
    <div id="as-of" class="meta">{waiting_html}</div>
    <pre id="payload">{{}}</pre>
  </div>
  <script>
    (function () {{
      var KEY = {json_str(data_key)};
      var MSG_UNAVAILABLE = {unavailable_js};
      var MSG_AS_OF_PREFIX = {as_of_prefix_js};
      var MSG_REQUEST_FAILED = {request_failed_js};
      var etag = null;
      var dataBase = null;

      function inferDataBaseFromPath() {{
        var m = /^(.*?\\/api\\/v1\\/dalton\\/monitors\\/(?:g|public)\\/[^\\/]+)\\/v\\//.exec(location.pathname);
        return m ? (m[1] + '/data') : null;
      }}

      function resolveBase() {{
        if (window.__DALTON_MONITOR__ && window.__DALTON_MONITOR__.dataBase) {{
          dataBase = window.__DALTON_MONITOR__.dataBase;
        }} else if (!dataBase) {{
          dataBase = inferDataBaseFromPath();
        }}
        return dataBase;
      }}

      async function poll() {{
        var base = resolveBase();
        if (!base) return;
        var headers = {{}};
        if (etag) headers['If-None-Match'] = etag;
        try {{
          var resp = await fetch(base + '/' + encodeURIComponent(KEY), {{ headers: headers }});
          if (resp.status === 304) return;
          if (!resp.ok) {{
            document.getElementById('as-of').textContent = MSG_UNAVAILABLE + ' (' + resp.status + ')';
            return;
          }}
          etag = resp.headers.get('ETag') || etag;
          var body = await resp.json();
          document.getElementById('as-of').textContent = MSG_AS_OF_PREFIX + (body.as_of || '--');
          document.getElementById('payload').textContent = JSON.stringify(body.data, null, 2);
        }} catch (err) {{
          document.getElementById('as-of').textContent = MSG_REQUEST_FAILED;
        }}
      }}

      window.addEventListener('dalton:monitor-grant', function (ev) {{
        if (ev && ev.detail && ev.detail.dataBase) {{
          dataBase = ev.detail.dataBase;
        }}
      }});

      poll();
      setInterval(poll, 60000);
    }})();
  </script>
</body>
</html>
"""


def json_str(value: str) -> str:
    return json.dumps(value)
