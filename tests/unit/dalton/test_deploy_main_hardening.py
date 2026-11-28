"""Unit tests for deploy_main.sh hardening r2:
- versioned frontend releases (web/releases/<build-id> + atomic current symlink)
- first-time migration from web/dist, carry-over of previous hashed assets, pruning to 3
- real concurrent reads during atomic switch & real HTTP check_public_edge.sh build-id verification
- strict --out-dir validation & shared pnpm build entry
- failure paths (build, backend, gateway, public edge on auto-revert/migrating/git-reset-fail) & SIGTERM
- re-exec protocol handshake (--resume-protocol, --validate-resume), paired env guard, and flock mutex.
"""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DEPLOY_SCRIPT = _REPO_ROOT / "scripts/dalton/ops/prod/deploy_main.sh"
_BUILD_FRONTEND_SCRIPT = _REPO_ROOT / "scripts/dalton/build_frontend_prod.sh"
_CHECK_PUBLIC_EDGE_SCRIPT = _REPO_ROOT / "scripts/dalton/check_public_edge.sh"


def _write_release_bundle(
    release_dir: Path,
    *,
    tag: str,
    entry_chunk: str,
    extra_assets: dict[str, str] | None = None,
    help_astro_assets: dict[str, str] | None = None,
    include_version_json: bool = True,
) -> None:
    (release_dir / "assets").mkdir(parents=True, exist_ok=True)
    (release_dir / "help").mkdir(parents=True, exist_ok=True)
    (release_dir / "index.html").write_text(
        f'<!doctype html><div id="root">{tag}</div><script type="module" src="/assets/{entry_chunk}"></script>\n',
        encoding="utf-8",
    )
    if include_version_json:
        (release_dir / "version.json").write_text(
            json.dumps({"build": entry_chunk}) + "\n",
            encoding="utf-8",
        )
    (release_dir / "assets" / entry_chunk).write_text(f'console.log("{tag}");\n', encoding="utf-8")
    for fname, content in (extra_assets or {}).items():
        (release_dir / "assets" / fname).write_text(content, encoding="utf-8")
    (release_dir / "help/index.html").write_text(f"<h1>Help {tag}</h1>\n", encoding="utf-8")
    if help_astro_assets:
        astro_dir = release_dir / "help/_astro"
        astro_dir.mkdir(parents=True, exist_ok=True)
        for fname, content in help_astro_assets.items():
            (astro_dir / fname).write_text(content, encoding="utf-8")


def _make_fake_build_script(
    *,
    tag: str,
    entry_chunk: str,
    trace_file: Path,
    fail: bool = False,
    extra_chunk: str = "",
    help_astro_chunk: str = "",
    block_on_barrier: tuple[Path, Path] | None = None,
    spawn_grandchild_pid_file: Path | None = None,
) -> str:
    barrier_snippet = ""
    if block_on_barrier is not None:
        ready_file, go_file = block_on_barrier
        grandchild_snippet = ""
        if spawn_grandchild_pid_file is not None:
            grandchild_snippet = f"""
"{sys.executable}" -c 'import os, time; open("{spawn_grandchild_pid_file}", "w").write(str(os.getpid())); time.sleep(60)' &
while [ ! -s "{spawn_grandchild_pid_file}" ]; do
  "{sys.executable}" -c 'import time; time.sleep(0.01)'
done
"""
        barrier_snippet = f"""
{grandchild_snippet}
touch "{ready_file}"
while [ ! -f "{go_file}" ]; do
  "{sys.executable}" -c 'import time; time.sleep(0.02)'
done
"""
    extra_snippet = ""
    if extra_chunk:
        extra_snippet = f'echo \'console.log("{tag}-lazy");\' > "${{out_dir}}/assets/{extra_chunk}"\n'
    astro_snippet = ""
    if help_astro_chunk:
        astro_snippet = (
            'mkdir -p "${out_dir}/help/_astro"\n'
            f'echo \'console.log("{tag}-astro");\' > "${{out_dir}}/help/_astro/{help_astro_chunk}"\n'
        )
    if fail:
        return f"""#!/bin/bash
set -euo pipefail
echo "build_frontend:$*" >> "{trace_file}"
out_dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --require-supabase) ;;
    --out-dir) out_dir="$2"; shift ;;
    --out-dir=*) out_dir="${{1#--out-dir=}}" ;;
    *) echo "UNEXPECTED_BUILD_ARG:$1" >&2; exit 99 ;;
  esac
  shift
done
[ -n "$out_dir" ] || out_dir="web/dist"
{barrier_snippet}
mkdir -p "${{out_dir}}/assets"
echo "broken-partial-{tag}" > "${{out_dir}}/index.html"
echo "ERROR: simulated frontend build failure" >&2
exit 1
"""
    return f"""#!/bin/bash
set -euo pipefail
echo "build_frontend:$*" >> "{trace_file}"
out_dir=""
while [ $# -gt 0 ]; do
  case "$1" in
    --require-supabase) ;;
    --out-dir) out_dir="$2"; shift ;;
    --out-dir=*) out_dir="${{1#--out-dir=}}" ;;
    *) echo "UNEXPECTED_BUILD_ARG:$1" >&2; exit 99 ;;
  esac
  shift
done
[ -n "$out_dir" ] || out_dir="web/dist"
{barrier_snippet}
mkdir -p "${{out_dir}}/assets" "${{out_dir}}/help"
echo '<!doctype html><div id="root">{tag}</div><script type="module" src="/assets/{entry_chunk}"></script>' > "${{out_dir}}/index.html"
echo '{{"build":"{entry_chunk}"}}' > "${{out_dir}}/version.json"
echo 'console.log("{tag}");' > "${{out_dir}}/assets/{entry_chunk}"
{extra_snippet}{astro_snippet}echo '<h1>Help {tag}</h1>' > "${{out_dir}}/help/index.html"
echo 'OK: Production bundle'
exit 0
"""


_INPROC_EDGE_SITECUSTOMIZE = r'''
import base64
import hashlib
import http.client
import json
import os
import socket
from pathlib import Path

_SERVE_ROOT = Path(os.environ["TEST_EDGE_SERVE_ROOT"])
_OVERRIDE_BUILD = os.environ.get("TEST_EDGE_OVERRIDE_BUILD", "")


class _FakeHTTPResponse:
    def __init__(self, status: int, headers: dict[str, str], body: bytes) -> None:
        self.status = status
        self._headers = list(headers.items())
        self._body = body

    def getheaders(self):
        return self._headers

    def read(self) -> bytes:
        return self._body


class _FakeHTTPConnection:
    def __init__(self, host: str, port: int = 80, timeout: float = 10.0) -> None:
        self.host = host
        self.port = port
        self._resp = None

    def request(self, method: str, path: str, headers=None) -> None:
        base_hdrs = {
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "strict-origin-when-cross-origin",
            "X-Frame-Options": "SAMEORIGIN",
            "Cache-Control": "no-cache, no-store, must-revalidate",
        }
        path_only = path.split("?", 1)[0]
        if method != "GET":
            self._resp = _FakeHTTPResponse(404, base_hdrs, b"not found")
            return
        if path_only in ("/health", "/openapi.json", "/docs"):
            self._resp = _FakeHTTPResponse(404, base_hdrs, b"blocked")
            return
        if path_only == "/api/v1/users/me":
            self._resp = _FakeHTTPResponse(
                401,
                {**base_hdrs, "Content-Type": "application/json"},
                json.dumps({"detail": "Missing authentication"}).encode(),
            )
            return
        if path_only == "/ws/v1/market-data/status":
            self._resp = _FakeHTTPResponse(200, {**base_hdrs, "Content-Type": "application/json"}, b'{"ok":true}')
            return
        if path_only == "/ws/v1/market-data/aggregates/stock":
            self._resp = _FakeHTTPResponse(404, base_hdrs, b"ws only")
            return
        if path_only == "/version.json":
            if _OVERRIDE_BUILD:
                self._resp = _FakeHTTPResponse(
                    200,
                    {**base_hdrs, "Content-Type": "application/json"},
                    json.dumps({"build": _OVERRIDE_BUILD}).encode(),
                )
                return
            vf = _SERVE_ROOT / "version.json"
            if vf.is_file():
                self._resp = _FakeHTTPResponse(200, {**base_hdrs, "Content-Type": "application/json"}, vf.read_bytes())
            else:
                self._resp = _FakeHTTPResponse(404, base_hdrs, b"missing")
            return
        if path_only.startswith("/help/pagefind/"):
            rel = path_only[len("/help/pagefind/"):]
            af = _SERVE_ROOT / "help/pagefind" / rel
            if af.is_file():
                self._resp = _FakeHTTPResponse(
                    200,
                    {**base_hdrs, "Content-Type": "application/octet-stream"},
                    af.read_bytes(),
                )
            else:
                self._resp = _FakeHTTPResponse(404, {**base_hdrs, "Cache-Control": "no-store"}, b"missing")
            return
        if path_only.startswith("/help/_astro/"):
            rel = path_only[len("/help/_astro/"):]
            af = _SERVE_ROOT / "help/_astro" / rel
            if af.is_file():
                self._resp = _FakeHTTPResponse(
                    200,
                    {**base_hdrs, "Content-Type": "application/javascript", "Cache-Control": "public, max-age=31536000, immutable"},
                    af.read_bytes(),
                )
            else:
                self._resp = _FakeHTTPResponse(404, {**base_hdrs, "Cache-Control": "no-store"}, b"missing")
            return
        if path_only.startswith("/assets/"):
            rel = path_only[len("/assets/"):]
            af = _SERVE_ROOT / "assets" / rel
            if af.is_file():
                self._resp = _FakeHTTPResponse(
                    200,
                    {**base_hdrs, "Content-Type": "application/javascript", "Cache-Control": "public, max-age=31536000, immutable"},
                    af.read_bytes(),
                )
            else:
                self._resp = _FakeHTTPResponse(404, {**base_hdrs, "Cache-Control": "no-store"}, b"missing")
            return
        idx = _SERVE_ROOT / "index.html"
        if idx.is_file():
            self._resp = _FakeHTTPResponse(200, {**base_hdrs, "Content-Type": "text/html"}, idx.read_bytes())
        else:
            self._resp = _FakeHTTPResponse(404, base_hdrs, b"no index")

    def getresponse(self):
        return self._resp

    def close(self) -> None:
        return None


class _FakeWSSocket:
    def __init__(self) -> None:
        self._resp = b""
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def sendall(self, data: bytes) -> None:
        key = ""
        for line in data.decode("iso-8859-1").splitlines():
            if line.lower().startswith("sec-websocket-key:"):
                key = line.split(":", 1)[1].strip()
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        self._resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        ).encode("ascii")

    def recv(self, n: int) -> bytes:
        chunk = self._resp[self._pos : self._pos + n]
        self._pos += len(chunk)
        return chunk


http.client.HTTPConnection = _FakeHTTPConnection
socket.create_connection = lambda addr, timeout=10.0: _FakeWSSocket()
'''


def _install_edge_http_stub(tmp_path: Path, name: str = "edge_py_stub") -> Path:
    stub_dir = tmp_path / name
    stub_dir.mkdir(exist_ok=True)
    (stub_dir / "sitecustomize.py").write_text(_INPROC_EDGE_SITECUSTOMIZE, encoding="utf-8")
    return stub_dir


def _setup_strict_deploy_env(
    tmp_path: Path,
    *,
    seed_legacy_dist: bool = False,
    seed_v1_release: bool = True,
    with_migration_change: bool = False,
    build_fail: bool = False,
    backend_health_fail: bool = False,
    public_edge_fail: bool = False,
    use_real_public_edge_check: bool = False,
    edge_port: int = 8088,
    gateway_changed: bool = False,
    gateway_health_fail: bool = False,
    busy_once_post_ff: bool = False,
    deterministic_clock: bool = True,
    build_barrier: tuple[Path, Path] | None = None,
    edge_check_barrier: tuple[Path, Path] | None = None,
    wait_idle_barrier: tuple[Path, Path] | None = None,
    git_reset_fail: bool = False,
) -> dict[str, Path | str]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", str(origin)], check=True)

    seed = tmp_path / "seed"
    subprocess.run(["git", "clone", "-q", str(origin), str(seed)], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(seed), "config", "user.name", "Test"], check=True)

    (seed / "src/dalton/migrations/versions").mkdir(parents=True)
    (seed / "migrations/versions").mkdir(parents=True)
    (seed / "src/dalton/migrations/versions/d040_base.py").write_text(
        "revision = 'd040'\ndown_revision = None\n"
    )
    (seed / "migrations/versions/146_base.py").write_text(
        "revision = '146'\ndown_revision = None\n"
    )
    (seed / "scripts/dalton/ops/prod").mkdir(parents=True)
    (seed / "web").mkdir(parents=True)
    (seed / "web/.keep").write_text("")
    (seed / "app.py").write_text("v1\n")

    events_trace = tmp_path / "events.trace"
    build_trace = tmp_path / "build.trace"
    wait_idle_trace = tmp_path / "wait_idle.trace"

    shutil.copy2(_DEPLOY_SCRIPT, seed / "scripts/dalton/ops/prod/deploy_main.sh")
    wait_barrier_code = ""
    if wait_idle_barrier is not None:
        w_ready, w_go = wait_idle_barrier
        wait_barrier_code = f"""
touch "{w_ready}"
while [ ! -f "{w_go}" ]; do
  "{sys.executable}" -c 'import time; time.sleep(0.02)'
done
"""
    (seed / "scripts/dalton/ops/prod/wait_backend_idle.sh").write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "wait_idle_v1" >> "{wait_idle_trace}"
echo "wait_idle_v1" >> "{events_trace}"
{wait_barrier_code}
exit 0
"""
    )
    (seed / "scripts/dalton/ops/prod/wait_backend_idle.sh").chmod(0o755)
    (seed / "scripts/dalton/build_frontend_prod.sh").write_text(
        _make_fake_build_script(
            tag="v1",
            entry_chunk="index-v1aaaa11.js",
            extra_chunk="lazy-v1bbbb22.js",
            trace_file=build_trace,
        )
    )

    if use_real_public_edge_check:
        shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, seed / "scripts/dalton/check_public_edge.sh")
    else:
        ec_barrier = ""
        if edge_check_barrier is not None:
            e_ready, e_go = edge_check_barrier
            ec_barrier = f"""
touch "{e_ready}"
while [ ! -f "{e_go}" ]; do
  "{sys.executable}" -c 'import time; time.sleep(0.02)'
done
"""
        (seed / "scripts/dalton/check_public_edge.sh").write_text(
            f"""#!/bin/bash
set -euo pipefail
cur_target=$(readlink web/releases/current 2>/dev/null || echo none)
echo "check_public_edge:args=$*:current=${{cur_target}}" >> "{events_trace}"
{ec_barrier}
if [ "{1 if public_edge_fail else 0}" = "1" ]; then
  echo "edge check failed" >&2
  exit 1
fi
echo "edge ok"
exit 0
"""
        )

    if gateway_changed:
        gw_seed = seed / "integrations/dalton-channel-gateway"
        (gw_seed / "src").mkdir(parents=True)
        (gw_seed / "package.json").write_text('{"name":"@dalton/channel-gateway","type":"module"}\n')
        (gw_seed / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n")
        (gw_seed / "pnpm-workspace.yaml").write_text("allowBuilds:\n  esbuild: true\n")
        (gw_seed / "build.mjs").write_text("// build\n")
        (gw_seed / "tsconfig.json").write_text('{"compilerOptions":{}}\n')
        (gw_seed / "src/main.ts").write_text("export const v = 1;\n")

    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", "v1"], check=True)
    subprocess.run(["git", "-C", str(seed), "branch", "-M", "dalton/main"], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "-u", "origin", "dalton/main"], check=True)
    v1 = subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()

    repo = tmp_path / "repo"
    subprocess.run(["git", "clone", "-q", "-b", "dalton/main", str(origin), str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)

    if seed_legacy_dist:
        _write_release_bundle(
            repo / "web/dist",
            tag="v1-legacy",
            entry_chunk="index-v1aaaa11.js",
            extra_assets={"lazy-v1bbbb22.js": 'console.log("v1-lazy");\n'},
        )
    elif seed_v1_release:
        rel_v1 = repo / "web/releases/v1-initial"
        _write_release_bundle(
            rel_v1,
            tag="v1",
            entry_chunk="index-v1aaaa11.js",
            extra_assets={"lazy-v1bbbb22.js": 'console.log("v1-lazy");\n'},
        )
        (rel_v1 / ".deploy-own-assets").write_text("index-v1aaaa11.js\nlazy-v1bbbb22.js\n")
        os.symlink("v1-initial", repo / "web/releases/current")

    if gateway_changed:
        gw_dir = repo / "integrations/dalton-channel-gateway"
        (gw_dir / "dist").mkdir(parents=True, exist_ok=True)
        (gw_dir / "dist/index.mjs").write_text("v1-gw\n")
        (gw_dir / "node_modules").mkdir(parents=True, exist_ok=True)
        (gw_dir / "node_modules/pkg.js").write_text("v1-pkg\n")
        env_dir = tmp_path / ".config/dalton"
        env_dir.mkdir(parents=True, exist_ok=True)
        (env_dir / "channel-gateway.env").write_text('INTERNAL_SERVICE_TOKEN="test-token-123"\n')

    # Stage v2 build script and optional gateway/migration changes in seed
    (seed / "scripts/dalton/build_frontend_prod.sh").write_text(
        _make_fake_build_script(
            tag="v2",
            entry_chunk="index-v2cccc33.js",
            extra_chunk="lazy-v2dddd44.js",
            trace_file=build_trace,
            fail=build_fail,
            block_on_barrier=build_barrier,
        )
    )
    if gateway_changed:
        (seed / "integrations/dalton-channel-gateway/src/main.ts").write_text("export const v = 2;\n")
    if with_migration_change:
        (seed / "src/dalton/migrations/versions/d041_next.py").write_text(
            "revision = 'd041'\ndown_revision = 'd040'\n"
        )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_git = shutil.which("git")
    real_curl = shutil.which("curl")
    assert real_git and real_curl

    # Strict fail-closed git wrapper
    git_trace = tmp_path / "git.trace"
    (fake_bin / "git").write_text(
        f"""#!/bin/bash
set -euo pipefail
case "${{1:-}}" in
  fetch|merge)
    echo "git:$*" >> "{git_trace}"
    echo "git:$*" >> "{events_trace}"
    ;;
  reset)
    if [ "{1 if git_reset_fail else 0}" = "1" ]; then
      echo "simulated git reset failure" >&2
      exit 1
    fi
    ;;
esac
exec "{real_git}" "$@"
"""
    )

    # Deterministic fake date (eliminates wall-clock 1-second timeout flakiness)
    clock_file = tmp_path / "clock.epoch"
    clock_file.write_text("1764150000")
    ts_seq_file = tmp_path / "ts.seq"
    ts_seq_file.write_text("0")
    if deterministic_clock:
        (fake_bin / "date").write_text(
            f"""#!/bin/bash
set -euo pipefail
if [ "${{1:-}}" = "+%s" ]; then
  cur=$(cat "{clock_file}")
  next=$((cur + 10))
  echo "$next" > "{clock_file}"
  echo "$cur"
  exit 0
elif [ "${{1:-}}" = "-u" ] && [ "${{2:-}}" = "+%Y%m%dT%H%M%SZ" ]; then
  seq=$(( $(cat "{ts_seq_file}") + 1 ))
  echo "$seq" > "{ts_seq_file}"
  printf '20261126T1000%02dZ\\n' "$seq"
  exit 0
elif [ "${{1:-}}" = "+%T" ]; then
  echo "10:00:00"
  exit 0
elif [ "${{1:-}}" = "-j" ] || [ "${{1:-}}" = "-d" ]; then
  exec /bin/date "$@"
fi
echo "UNEXPECTED_DATE_ARGS:$*" >&2
exit 99
"""
        )

    backup_log = tmp_path / "pg-backup.log"
    backup_seq = tmp_path / "pg-backup.seq"
    backup_seq.write_text("0")
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    backup_log.write_text(f"{now_str} ok  16G /backups/prev.dump\n")
    launchctl_trace = tmp_path / "launchctl.trace"
    (fake_bin / "launchctl").write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "launchctl:$*" >> "{launchctl_trace}"
cur_target=$(readlink "{repo}/web/releases/current" 2>/dev/null || echo none)
echo "launchctl:$*:current=${{cur_target}}" >> "{events_trace}"
case "${{1:-}}" in
  kickstart)
    if [[ "$*" == *"com.dalton.pg-backup"* ]]; then
      n=$(( $(cat "{backup_seq}") + 1 ))
      echo "$n" > "{backup_seq}"
      _nid_file="${{DALTON_PG_BACKUP_NEXT_ID_FILE:-$HOME/Library/Logs/.dalton-pg-backup.next-set-id}}"
      _sid="$(tr -d '[:space:]' < "$_nid_file" 2>/dev/null || true)"
      if [ -n "$_sid" ]; then
        _broot="{tmp_path}/mock-backups"
        mkdir -p "$_broot/pg" "$_broot/sets"
        _dump="$_broot/pg/dalton-pg-${{_sid}}.dump"
        printf 'PGDMP-%s-%s' "$_sid" "$n" > "$_dump"
        _man="$_broot/sets/minio-manifest-${{_sid}}.json"
        printf '{{"version":2,"backup_set_id":"%s","objects":[]}}\\n' "$_sid" > "$_man"
        _dsha="$(shasum -a 256 "$_dump" | awk '{{print $1}}')"
        _dsz="$(wc -c < "$_dump" | tr -d '[:space:]')"
        _msha="$(shasum -a 256 "$_man" | awk '{{print $1}}')"
        printf '{{"version":1,"backup_set_id":"%s","dump_path":"%s","dump_size_bytes":%s,"dump_sha256":"%s","manifest_path":"%s","manifest_sha256":"%s","object_count":0,"reconcile_ok":true}}\\n' \\
          "$_sid" "$_dump" "$_dsz" "$_dsha" "$_man" "$_msha" > "$_broot/sets/backup-set-${{_sid}}.json"
        echo "{now_str} ok  16G ${{_dump}} set=${{_sid}}" >> "{backup_log}"
      else
        echo "{now_str} ok  16G /backups/fresh-${{n}}.dump" >> "{backup_log}"
      fi
      exit 0
    elif [[ "$*" == *"com.dalton.researchdata-bridge"* ]] || [[ "$*" == *"com.dalton.channel-gateway"* ]]; then
      exit 0
    fi
    ;;
  list)
    echo "123 0 com.dalton.researchdata-bridge"
    exit 0
    ;;
esac
echo "UNEXPECTED_LAUNCHCTL:$*" >&2
exit 99
"""
    )

    busy_counter = tmp_path / "busy.count"
    busy_counter.write_text("0")
    docker_trace = tmp_path / "docker.trace"
    (fake_bin / "docker").write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "docker:$*" >> "{docker_trace}"
case "${{1:-}}" in
  restart)
    cur_target=$(readlink "{repo}/web/releases/current" 2>/dev/null || echo none)
    echo "docker_restart:$*:current=${{cur_target}}" >> "{events_trace}"
    exit 0
    ;;
  inspect)
    echo "backend mem_limit=6442450944"
    exit 0
    ;;
  ps)
    echo "cid-123"
    exit 0
    ;;
  logs)
    echo "log line"
    exit 0
    ;;
  exec)
    if [[ "$*" == *"alembic_version_dalton"* ]]; then
      echo "d040"
      exit 0
    elif [[ "$*" == *"alembic_version"* ]]; then
      echo "146"
      exit 0
    elif [[ "$*" == *"conversation_threads"* ]]; then
      if [ "{1 if busy_once_post_ff else 0}" = "1" ]; then
        c=$(cat "{busy_counter}")
        if [ "$c" = "0" ]; then
          echo "1" > "{busy_counter}"
          echo "1"
          exit 0
        fi
      fi
      echo "0"
      exit 0
    elif [[ "$*" == *"alembic.ini current"* ]]; then
      echo "d040 (head)"
      exit 0
    elif [[ "$*" == *"crawler:8100/health"* ]]; then
      echo "crawler health 200"
      exit 0
    fi
    ;;
  compose)
    if [ "${{2:-}}" = "config" ] && [ "${{3:-}}" = "--services" ]; then
      echo "backend"
      echo "crawler"
      exit 0
    elif [ "${{2:-}}" = "config" ] || [[ "$*" == *" config"* ]]; then
      echo "services:"
      echo "  edge:"
      echo "    volumes:"
      echo "      - ./web/releases:/srv-releases:ro"
      exit 0
    elif [ "${{2:-}}" = "up" ]; then
      cur_target=$(readlink "{repo}/web/releases/current" 2>/dev/null || echo none)
      echo "docker_compose_up:$*:current=${{cur_target}}" >> "{events_trace}"
      exit 0
    fi
    ;;
esac
echo "UNEXPECTED_DOCKER:$*" >&2
exit 99
"""
    )

    # Strict fail-closed curl wrapper (forwards real loopback edge requests when real edge server is used)
    (fake_bin / "curl").write_text(
        f"""#!/bin/bash
set -euo pipefail
if [[ "$*" == *":8000/health"* ]]; then
  if [ "{1 if backend_health_fail else 0}" = "1" ]; then
    echo "503"
    exit 22
  fi
  echo "200"
  exit 0
fi
if [[ "$*" == *":8962/health"* ]]; then
  if [ "{1 if gateway_health_fail else 0}" = "1" ]; then
    echo '{{"ready":false}}'
    exit 0
  fi
  echo '{{"ready":true}}'
  exit 0
fi
if [[ "$*" == *":{edge_port}/__edge_health"* ]]; then
  echo "edge_ready_probe:$*" >> "{events_trace}"
  printf 'dalton-edge-ok releases-v1\n200'
  exit 0
fi
echo "UNEXPECTED_CURL:$*" >&2
exit 99
"""
    )

    # Strict fail-closed uv, pnpm, node, sleep
    (fake_bin / "uv").write_text(
        """#!/bin/bash
set -euo pipefail
if [ "${1:-}" = "sync" ]; then
  exit 0
fi
echo "UNEXPECTED_UV:$*" >&2
exit 99
"""
    )
    (fake_bin / "pnpm").write_text(
        """#!/bin/bash
set -euo pipefail
if [ "${1:-}" = "install" ] && [ "${2:-}" = "--frozen-lockfile" ]; then
  mkdir -p node_modules
  echo "installed" > node_modules/pkg.js
  exit 0
fi
echo "UNEXPECTED_PNPM:$*" >&2
exit 99
"""
    )
    (fake_bin / "node").write_text(
        """#!/bin/bash
set -euo pipefail
if [ "${1:-}" = "build.mjs" ]; then
  mkdir -p dist
  echo "v2-gw" > dist/index.mjs
  exit 0
fi
if [ "${1:-}" = "--input-type=module" ] && [ "${2:-}" = "-e" ]; then
  exit 0
fi
echo "UNEXPECTED_NODE:$*" >&2
exit 99
"""
    )
    (fake_bin / "sleep").write_text("#!/bin/bash\nexit 0\n")

    bin_names = ["git", "launchctl", "docker", "curl", "uv", "pnpm", "node", "sleep"]
    if deterministic_clock:
        bin_names.append("date")
    for name in bin_names:
        (fake_bin / name).chmod(0o755)

    # Use sys.executable (never hardcode .venv/bin/python)
    (fake_bin / "python3").symlink_to(sys.executable)

    lock_file = tmp_path / "locks/deploy_main.lock"
    return {
        "origin": origin,
        "seed": seed,
        "repo": repo,
        "v1": v1,
        "fake_bin": fake_bin,
        "backup_log": backup_log,
        "lock_file": lock_file,
        "events_trace": events_trace,
        "build_trace": build_trace,
        "wait_idle_trace": wait_idle_trace,
        "git_trace": git_trace,
        "launchctl_trace": launchctl_trace,
        "docker_trace": docker_trace,
        "edge_port": str(edge_port),
    }


def _commit_and_push_seed(seed: Path, message: str = "v2") -> str:
    subprocess.run(["git", "-C", str(seed), "add", "."], check=True)
    subprocess.run(["git", "-C", str(seed), "commit", "-q", "-m", message], check=True)
    subprocess.run(["git", "-C", str(seed), "push", "-q", "origin", "dalton/main"], check=True)
    return subprocess.check_output(["git", "-C", str(seed), "rev-parse", "HEAD"], text=True).strip()


def _wrapper_env_and_cmd(
    ctx: dict[str, Path | str],
    tmp_path: Path,
    *,
    args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
    wrapper_name: str = "wrapper-tmp",
) -> tuple[list[str], dict[str, str]]:
    repo = Path(ctx["repo"])
    wrapper_tmp = tmp_path / wrapper_name
    wrapper_tmp.mkdir(exist_ok=True)
    shutil.copy2(repo / "scripts/dalton/ops/prod/deploy_main.sh", wrapper_tmp / "deploy_main.sh")
    shutil.copy2(
        repo / "scripts/dalton/ops/prod/wait_backend_idle.sh",
        wrapper_tmp / "wait_backend_idle.sh",
    )
    reexec_tmpdir = tmp_path / "reexec-tmpdir"
    reexec_tmpdir.mkdir(exist_ok=True)

    env = {
        "PATH": f"{ctx['fake_bin']}:/bin:/usr/bin",
        "HOME": str(tmp_path),
        "TMPDIR": str(reexec_tmpdir),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_PG_BACKUP_LOG": str(ctx["backup_log"]),
        "DALTON_DEPLOY_LOCK_FILE": str(ctx["lock_file"]),
        "DALTON_EDGE_PORT": str(ctx["edge_port"]),
        "DALTON_ALEMBIC_QUERY_RETRY_DELAY_S": "0",
        "DALTON_GATEWAY_WAIT_TIMEOUT": "20",
        "DALTON_GATEWAY_POLL_INTERVAL": "0",
    }
    if extra_env:
        env.update(extra_env)

    cmd = ["bash", str(wrapper_tmp / "deploy_main.sh"), *(args or [])]
    return cmd, env


def _run_via_wrapper(
    ctx: dict[str, Path | str],
    tmp_path: Path,
    *,
    args: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd, env = _wrapper_env_and_cmd(ctx, tmp_path, args=args, extra_env=extra_env)
    return subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)


def _is_lock_held(lock_file: Path) -> bool:
    if not lock_file.exists():
        return False
    fd = os.open(lock_file, os.O_RDWR)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# 1. Atomic symlink switch concurrency & carry-over of previous hashed assets
# ---------------------------------------------------------------------------


def test_concurrent_reads_during_atomic_symlink_switch_always_see_complete_version_and_prev_hashed_assets(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rel_dir = repo / "web/releases"
    rel_dir.mkdir(parents=True)

    r1 = rel_dir / "rel-v1"
    r2 = rel_dir / "rel-v2"
    r3 = rel_dir / "rel-v3"
    _write_release_bundle(
        r1,
        tag="v1",
        entry_chunk="index-v1aaaa11.js",
        extra_assets={"lazy-v1bbbb22.js": 'console.log("v1-lazy");\n'},
    )
    _write_release_bundle(
        r2,
        tag="v2",
        entry_chunk="index-v2cccc33.js",
        extra_assets={"lazy-v2dddd44.js": 'console.log("v2-lazy");\n'},
    )
    _write_release_bundle(
        r3,
        tag="v3",
        entry_chunk="index-v3eeee55.js",
        extra_assets={"lazy-v3ffff66.js": 'console.log("v3-lazy");\n'},
    )
    os.symlink("rel-v1", rel_dir / "current")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    env = {"PATH": f"{fake_bin}:/bin:/usr/bin", "HOME": str(tmp_path), "DALTON_REPO_ROOT": str(repo)}

    # Record own assets for r1, r2, carry r1 -> r2, then r2 -> r3
    prep_script = f"""
set -euo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
_record_own_frontend_assets "{r1}"
_record_own_frontend_assets "{r2}"
_carry_over_prev_hashed_assets "{r1}" "{r2}"
_record_own_frontend_assets "{r3}"
_carry_over_prev_hashed_assets "{r2}" "{r3}"
"""
    prep = subprocess.run(["bash", "-c", prep_script], env=env, capture_output=True, text=True, check=False)
    assert prep.returncode == 0, prep.stdout + prep.stderr

    # r2 carries r1's hashed assets; r3 carries ONLY r2's own hashed assets (no unbounded accumulation of r1!)
    assert (r2 / "assets/lazy-v1bbbb22.js").is_file()
    assert (r2 / "assets/index-v1aaaa11.js").is_file()
    assert (r3 / "assets/lazy-v2dddd44.js").is_file()
    assert not (r3 / "assets/lazy-v1bbbb22.js").exists()

    # Concurrent reader thread continuously resolves web/releases/current while switching r1 <-> r2
    stop_event = threading.Event()
    errors: list[str] = []
    reads_ok = 0

    def reader() -> None:
        nonlocal reads_ok
        cur = rel_dir / "current"
        while not stop_event.is_set():
            try:
                # Open current dir via realpath so one logical request inspects the resolved release
                resolved = Path(os.path.realpath(cur))
                html = (resolved / "index.html").read_text(encoding="utf-8")
                ver = json.loads((resolved / "version.json").read_text(encoding="utf-8"))
                entry = ver.get("entry") or ver["build"]
                if f"/assets/{entry}" not in html:
                    errors.append(f"mismatched html/version: entry={entry} html={html!r}")
                if not (resolved / "assets" / entry).is_file():
                    errors.append(f"missing active entry asset {entry} in {resolved}")
                # Old hashed asset remains available in both r1 and r2
                if not (resolved / "assets/lazy-v1bbbb22.js").is_file():
                    errors.append(f"missing carried-over v1 hashed asset in {resolved}")
                reads_ok += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"read exception: {exc}")

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        swap_loop = f"""
set -euo pipefail
DALTON_DEPLOY_LIB_ONLY=1
source "{_DEPLOY_SCRIPT}"
PREV_FRONTEND_RELEASE_ID="rel-v1"
for i in $(seq 1 15); do
  NEW_FRONTEND_RELEASE_ID="rel-v2"
  ACTIVE_FRONTEND_RELEASE_DIR="{r2}"
  switch_frontend_from_staging
  restore_frontend_from_prev
done
NEW_FRONTEND_RELEASE_ID="rel-v2"
ACTIVE_FRONTEND_RELEASE_DIR="{r2}"
switch_frontend_from_staging
"""
        res = subprocess.run(["bash", "-c", swap_loop], env=env, capture_output=True, text=True, check=False)
        assert res.returncode == 0, res.stdout + res.stderr
    finally:
        stop_event.set()
        t.join(timeout=5)

    assert not errors, f"concurrent read errors: {errors[:5]}"
    assert reads_ok > 20
    assert os.readlink(rel_dir / "current") == "rel-v2"


# ---------------------------------------------------------------------------
# 2. First-time migration from web/dist, real HTTP check_public_edge, & pruning to 3
# ---------------------------------------------------------------------------


def test_first_time_migration_from_web_dist_real_http_check_and_pruning_keeps_latest_3(
    tmp_path: Path,
) -> None:
    stub_dir = _install_edge_http_stub(tmp_path)
    ctx = _setup_strict_deploy_env(
        tmp_path,
        seed_legacy_dist=True,
        seed_v1_release=False,
        use_real_public_edge_check=True,
        edge_port=8088,
    )
    seed = Path(ctx["seed"])
    repo = Path(ctx["repo"])
    rel_dir = repo / "web/releases"
    edge_env = {
        "PYTHONPATH": str(stub_dir),
        "TEST_EDGE_SERVE_ROOT": str(rel_dir / "current"),
    }

    # Deploy 1 (v2): migrates legacy web/dist -> web/releases/legacy-*, switches current -> v2
    (seed / "app.py").write_text("v2\n")
    _commit_and_push_seed(seed, "v2 first migration")

    res1 = _run_via_wrapper(ctx, tmp_path, extra_env=edge_env)
    assert res1.returncode == 0, res1.stdout + res1.stderr
    assert "migrated legacy web/dist into web/releases/legacy-" in res1.stdout
    assert "ALL PUBLIC EDGE CHECKS PASSED." in res1.stdout

    # Legacy web/dist preserved untouched; web/releases/current points to relative v2 release
    assert (repo / "web/dist/index.html").is_file()
    cur1 = os.readlink(rel_dir / "current")
    assert "/" not in cur1 and not cur1.startswith("legacy-")
    # Previous v1 hashed asset carried over into v2 release
    assert (rel_dir / cur1 / "assets/lazy-v1bbbb22.js").is_file()
    assert (rel_dir / cur1 / "assets/index-v2cccc33.js").is_file()

    # Deploy 2 (v3), Deploy 3 (v4), Deploy 4 (v5) -> total 5 releases created, pruned to latest 3
    for idx, (tag, chunk) in enumerate(
        [("v3", "index-v3eeee55.js"), ("v4", "index-v4gggg77.js"), ("v5", "index-v5iiii99.js")],
        start=3,
    ):
        (seed / "scripts/dalton/build_frontend_prod.sh").write_text(
            _make_fake_build_script(
                tag=tag,
                entry_chunk=chunk,
                trace_file=Path(ctx["build_trace"]),
            )
        )
        (seed / "app.py").write_text(f"{tag}\n")
        _commit_and_push_seed(seed, f"{tag} release")
        res_n = _run_via_wrapper(ctx, tmp_path, extra_env=edge_env)
        assert res_n.returncode == 0, f"deploy {idx} failed:\n{res_n.stdout}\n{res_n.stderr}"

    kept_dirs = sorted(
        p.name for p in rel_dir.iterdir() if p.is_dir() and not p.is_symlink() and not p.name.startswith(".")
    )
    assert len(kept_dirs) == 3, f"expected 3 releases kept, got {kept_dirs}"
    assert os.readlink(rel_dir / "current") in kept_dirs
    assert not any(name.startswith("legacy-") for name in kept_dirs)


def test_real_check_public_edge_rejects_stale_http_build_id_and_edge_ready_uses_edge_health(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts/dalton").mkdir(parents=True)
    shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")

    rel_dir = repo / "web/releases"
    _write_release_bundle(rel_dir / "rel-new", tag="new", entry_chunk="index-new99999.js")
    os.symlink("rel-new", rel_dir / "current")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    stub_dir = _install_edge_http_stub(tmp_path)

    # 1. Server serves stale build id ("index-old11111.js") while disk & --expected-build expect "index-new99999.js"
    res_stale = subprocess.run(
        [
            "bash",
            str(repo / "scripts/dalton/check_public_edge.sh"),
            "--expected-build",
            "index-new99999.js",
            "--dist-dir",
            str(rel_dir / "current"),
        ],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "DALTON_EDGE_PORT": "8088",
            "PYTHONPATH": str(stub_dir),
            "TEST_EDGE_SERVE_ROOT": str(rel_dir / "current"),
            "TEST_EDGE_OVERRIDE_BUILD": "index-old11111.js",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_stale.returncode != 0
    assert "HTTP /version.json build mismatch" in res_stale.stderr

    # 2. Server serving matching build id passes all 6 checks
    res_ok = subprocess.run(
        [
            "bash",
            str(repo / "scripts/dalton/check_public_edge.sh"),
            "--expected-build",
            "index-new99999.js",
            "--dist-dir",
            str(rel_dir / "current"),
        ],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "DALTON_EDGE_PORT": "8088",
            "PYTHONPATH": str(stub_dir),
            "TEST_EDGE_SERVE_ROOT": str(rel_dir / "current"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_ok.returncode == 0, res_ok.stdout + res_ok.stderr
    assert "ALL PUBLIC EDGE CHECKS PASSED." in res_ok.stdout

    # 3. edge_ready probes /__edge_health (NOT /) and validates the dedicated response body
    curl_trace = tmp_path / "edge_curl.trace"
    (fake_bin / "curl").write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "$*" >> "{curl_trace}"
if [[ "$*" == *"http://127.0.0.1:8088/__edge_health"* ]]; then
  printf 'dalton-edge-ok releases-v1\\n200'
  exit 0
fi
echo "UNEXPECTED_EDGE_READY_CURL:$*" >&2
exit 99
"""
    )
    (fake_bin / "curl").chmod(0o755)
    res_ready = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1; DALTON_EDGE_PORT="8088"; '
            f'source "{_DEPLOY_SCRIPT}"; edge_ready 2',
        ],
        env={"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_ready.returncode == 0, res_ready.stdout + res_ready.stderr
    assert "/__edge_health" in curl_trace.read_text()


# ---------------------------------------------------------------------------
# 3. Strict --out-dir validation & shared `pnpm build` entry
# ---------------------------------------------------------------------------


def test_build_frontend_prod_out_dir_validation_and_shared_pnpm_build(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts/dalton").mkdir(parents=True)
    (repo / "web/src").mkdir(parents=True)
    (repo / "docs-site").mkdir(parents=True)
    (repo / "scripts/other").mkdir(parents=True)
    shutil.copy2(_BUILD_FRONTEND_SCRIPT, repo / "scripts/dalton/build_frontend_prod.sh")
    (repo / "web/.env.dalton").write_text("VITE_BRAND_NAME=Everflow\nVITE_BRAND_MARK=everflow\n")
    (repo / ".env").write_text(
        "APP_ENV=dalton\n"
        "DALTON_AUTH_MODE=supabase\n"
        "SUPABASE_URL=https://example.supabase.co\n"
        "SUPABASE_PUBLISHABLE_KEY=pk_test_123456\n"
    )
    _write_release_bundle(repo / "web/dist", tag="v1", entry_chunk="index-v1aaaa11.js")

    cmd_trace = tmp_path / "cmds.trace"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Strict fail-closed fake pnpm: ONLY accepts `pnpm build` (web) and `pnpm install --frozen-lockfile`/`pnpm build` (docs-site)
    (fake_bin / "pnpm").write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "pnpm:$(pwd):out=${{DALTON_VITE_OUT_DIR:-<unset>}}:$*" >> "{cmd_trace}"
if [ "$(basename "$(pwd)")" = "docs-site" ]; then
  if [ "${{1:-}}" = "install" ] && [ "${{2:-}}" = "--frozen-lockfile" ]; then
    exit 0
  elif [ "${{1:-}}" = "build" ]; then
    mkdir -p dist
    echo "<h1>docs</h1>" > dist/index.html
    exit 0
  fi
  echo "UNEXPECTED_DOCS_PNPM:$*" >&2
  exit 99
fi
if [ "${{1:-}}" = "build" ] && [ $# -eq 1 ]; then
  out="${{DALTON_VITE_OUT_DIR:-dist}}"
  mkdir -p "${{out}}/assets"
  echo "built-html" > "${{out}}/index.html"
  echo '{{"build":"index-built11.js"}}' > "${{out}}/version.json"
  echo "built-js" > "${{out}}/assets/index-built11.js"
  exit 0
fi
echo "UNEXPECTED_WEB_PNPM:$*" >&2
exit 99
"""
    )
    (fake_bin / "pnpm").chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}

    # 1. Valid new dir under web/releases/ -> runs shared `pnpm build` with DALTON_VITE_OUT_DIR, web/dist untouched
    res_rel = subprocess.run(
        [
            "bash",
            str(repo / "scripts/dalton/build_frontend_prod.sh"),
            "--require-supabase",
            "--out-dir",
            "web/releases/abc123-20261126T100001Z",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_rel.returncode == 0, res_rel.stdout + res_rel.stderr
    rel_out = (repo / "web/releases/abc123-20261126T100001Z").resolve()
    assert (rel_out / "index.html").read_text().strip() == "built-html"
    assert (rel_out / "help/index.html").read_text().strip() == "<h1>docs</h1>"
    assert (repo / "web/dist/version.json").read_text().strip() == '{"build": "index-v1aaaa11.js"}'
    cmds = cmd_trace.read_text().splitlines()
    assert any(f":out={rel_out}:build" in c for c in cmds)

    # 2. Default (no --out-dir): unsets any leaked DALTON_VITE_OUT_DIR and builds into web/dist
    cmd_trace.unlink()
    res_def = subprocess.run(
        ["bash", str(repo / "scripts/dalton/build_frontend_prod.sh"), "--require-supabase"],
        cwd=repo,
        env={**env, "DALTON_VITE_OUT_DIR": str(tmp_path / "should-be-ignored")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_def.returncode == 0, res_def.stdout + res_def.stderr
    assert (repo / "web/dist/index.html").read_text().strip() == "built-html"
    assert any(":out=<unset>:build" in c for c in cmd_trace.read_text().splitlines())

    # 3. Reject every forbidden --out-dir (existing release, current, web/dist, source dirs, outside repo, symlinks)
    outside = tmp_path / "outside-new"
    for bad_args in (
        ["--out-dir"],
        ["--out-dir", "/"],
        ["--out-dir", str(repo)],
        ["--out-dir", str(repo / "web")],
        ["--out-dir", "web/dist"],
        ["--out-dir", "web/src"],
        ["--out-dir", "scripts"],
        ["--out-dir", "scripts/newdir"],
        ["--out-dir", str(outside)],
        ["--out-dir", "web/releases/current"],
        ["--out-dir", "web/releases/abc123-20261126T100001Z"],  # already exists
        ["--out-dir", "web/releases/../dist"],
        ["--out-dir", "web/releases/nested/sub"],
    ):
        bad = subprocess.run(
            ["bash", str(repo / "scripts/dalton/build_frontend_prod.sh"), *bad_args],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert bad.returncode == 2, f"expected exit 2 for {bad_args}, got {bad.returncode}: {bad.stderr}"

    # Symlinked web/releases pointing outside must be rejected
    shutil.rmtree(repo / "web/releases")
    evil_target = tmp_path / "evil-target"
    evil_target.mkdir()
    os.symlink(evil_target, repo / "web/releases")
    bad_sym = subprocess.run(
        ["bash", str(repo / "scripts/dalton/build_frontend_prod.sh"), "--out-dir", "web/releases/new1"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_sym.returncode == 2, bad_sym.stderr


# ---------------------------------------------------------------------------
# 4. Failure paths (build, backend, gateway, public edge auto/migrating/reset-fail) & SIGTERM
# ---------------------------------------------------------------------------


def test_build_backend_and_gateway_failures_preserve_current_and_clean_unswitched_release(
    tmp_path: Path,
) -> None:
    for label, kwargs, expected_msg in (
        ("build_fail", {"build_fail": True}, "frontend build failed: cleaning staging and reverting"),
        ("backend_fail", {"backend_health_fail": True}, "backend health check failed: reverting"),
        (
            "gateway_fail",
            {"gateway_changed": True, "gateway_health_fail": True},
            "channel-gateway health check failed: rolling back gateway only",
        ),
    ):
        case_dir = tmp_path / label
        case_dir.mkdir()
        ctx = _setup_strict_deploy_env(case_dir, **kwargs)
        seed = Path(ctx["seed"])
        repo = Path(ctx["repo"])
        if not kwargs.get("gateway_changed"):
            (seed / "app.py").write_text("v2\n")
        _commit_and_push_seed(seed, f"v2 {label}")

        res = _run_via_wrapper(ctx, case_dir)
        assert res.returncode != 0, label
        assert expected_msg in res.stdout, f"{label}: {res.stdout}\n{res.stderr}"

        rel_dir = repo / "web/releases"
        assert os.readlink(rel_dir / "current") == "v1-initial", label
        remaining = [p.name for p in rel_dir.iterdir() if p.is_dir() and not p.is_symlink()]
        assert remaining == ["v1-initial"], f"{label}: unexpected release dirs {remaining}"
        # Only initial build ran (never rebuilt on revert)
        assert len(Path(ctx["build_trace"]).read_text().splitlines()) == 1, label
        # Lock released cleanly
        assert not _is_lock_held(Path(ctx["lock_file"])), label


def test_public_edge_failure_restores_current_across_auto_revert_migrating_and_git_reset_failure(
    tmp_path: Path,
) -> None:
    # Case A: non-migrating auto-revert -> current restored to v1-initial, no rebuild
    case_a = tmp_path / "auto_revert"
    case_a.mkdir()
    ctx_a = _setup_strict_deploy_env(case_a, public_edge_fail=True)
    (Path(ctx_a["seed"]) / "app.py").write_text("v2\n")
    _commit_and_push_seed(Path(ctx_a["seed"]), "v2 edge fail")
    res_a = _run_via_wrapper(ctx_a, case_a)
    assert res_a.returncode != 0
    assert "frontend current atomically restored to web/releases/v1-initial" in res_a.stdout
    assert os.readlink(Path(ctx_a["repo"]) / "web/releases/current") == "v1-initial"
    assert len(Path(ctx_a["build_trace"]).read_text().splitlines()) == 1

    # Case B: migrating deploy (auto-revert aborted) -> current STILL restored to v1-initial AND both releases preserved
    case_b = tmp_path / "migrating_revert"
    case_b.mkdir()
    ctx_b = _setup_strict_deploy_env(case_b, with_migration_change=True, public_edge_fail=True)
    _commit_and_push_seed(Path(ctx_b["seed"]), "v2 migrating edge fail")
    res_b = _run_via_wrapper(ctx_b, case_b)
    assert res_b.returncode != 0
    assert "AUTO-REVERT ABORTED" in res_b.stdout
    assert "frontend current atomically restored to web/releases/v1-initial" in res_b.stdout
    rel_b = Path(ctx_b["repo"]) / "web/releases"
    assert os.readlink(rel_b / "current") == "v1-initial"
    dirs_b = [p.name for p in rel_b.iterdir() if p.is_dir() and not p.is_symlink()]
    assert "v1-initial" in dirs_b and len(dirs_b) == 2  # Both old and new preserved for operator inspection!
    assert "manual_switch_frontend_release" in res_b.stdout

    # Case C: git reset --keep fails during auto-revert -> current STILL restored to v1-initial before git reset error
    case_c = tmp_path / "git_reset_fail"
    case_c.mkdir()
    ctx_c = _setup_strict_deploy_env(case_c, public_edge_fail=True, git_reset_fail=True)
    (Path(ctx_c["seed"]) / "app.py").write_text("v2\n")
    _commit_and_push_seed(Path(ctx_c["seed"]), "v2 reset fail")
    res_c = _run_via_wrapper(ctx_c, case_c)
    assert res_c.returncode != 0
    assert "CODE REVERT FAILED" in res_c.stdout
    assert os.readlink(Path(ctx_c["repo"]) / "web/releases/current") == "v1-initial"
    assert (Path(ctx_c["repo"]) / "web/releases/v1-initial/index.html").is_file()


def test_sigterm_before_and_after_switch_leaves_complete_current_and_preserves_old_release(
    tmp_path: Path,
) -> None:
    # Case A: SIGTERM during frontend build (before switch) -> unswitched dir cleaned, current == v1-initial
    case_a = tmp_path / "term_before_switch"
    case_a.mkdir()
    ready_a = case_a / "build.ready"
    go_a = case_a / "build.go"
    ctx_a = _setup_strict_deploy_env(case_a, build_barrier=(ready_a, go_a))
    (Path(ctx_a["seed"]) / "app.py").write_text("v2\n")
    _commit_and_push_seed(Path(ctx_a["seed"]), "v2 term before switch")
    cmd_a, env_a = _wrapper_env_and_cmd(ctx_a, case_a)
    proc_a = subprocess.Popen(cmd_a, env=env_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not ready_a.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_a.exists(), "build barrier never reached"
        proc_a.send_signal(signal.SIGTERM)
        out_a, err_a = proc_a.communicate(timeout=15)
    finally:
        if proc_a.poll() is None:
            proc_a.kill()
    assert proc_a.returncode != 0
    rel_a = Path(ctx_a["repo"]) / "web/releases"
    assert os.readlink(rel_a / "current") == "v1-initial"
    assert [p.name for p in rel_a.iterdir() if p.is_dir() and not p.is_symlink()] == ["v1-initial"]
    assert not _is_lock_held(Path(ctx_a["lock_file"]))

    # Case B: SIGTERM during check_public_edge.sh (after switch) -> current points to complete v2, v1-initial preserved
    case_b = tmp_path / "term_after_switch"
    case_b.mkdir()
    ready_b = case_b / "edge.ready"
    go_b = case_b / "edge.go"
    ctx_b = _setup_strict_deploy_env(case_b, edge_check_barrier=(ready_b, go_b))
    (Path(ctx_b["seed"]) / "app.py").write_text("v2\n")
    _commit_and_push_seed(Path(ctx_b["seed"]), "v2 term after switch")
    cmd_b, env_b = _wrapper_env_and_cmd(ctx_b, case_b)
    proc_b = subprocess.Popen(cmd_b, env=env_b, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not ready_b.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_b.exists(), "edge check barrier never reached"
        proc_b.send_signal(signal.SIGTERM)
        out_b, err_b = proc_b.communicate(timeout=15)
    finally:
        if proc_b.poll() is None:
            proc_b.kill()
    assert proc_b.returncode != 0
    rel_b = Path(ctx_b["repo"]) / "web/releases"
    cur_b = os.readlink(rel_b / "current")
    assert cur_b != "v1-initial"
    assert (rel_b / cur_b / "index.html").is_file()
    assert (rel_b / cur_b / "version.json").is_file()
    assert (rel_b / "v1-initial/index.html").is_file()  # Old release preserved!
    assert not _is_lock_held(Path(ctx_b["lock_file"]))


# ---------------------------------------------------------------------------
# 5. Re-exec protocol handshake, paired env guard, & flock mutex across parent+child
# ---------------------------------------------------------------------------


def test_reexec_handshake_runs_updated_script_once_under_same_lock(tmp_path: Path) -> None:
    ctx = _setup_strict_deploy_env(tmp_path)
    seed = Path(ctx["seed"])

    deploy_v2 = (seed / "scripts/dalton/ops/prod/deploy_main.sh").read_text()
    anchor = 'echo "$(date \'+%T\') resumed re-executed deploy script for ${TARGET} (protocol=${DEPLOY_RESUME_PROTOCOL}, PRE_FF=${PRE_FF}, backup=${DEPLOY_BACKUP_STATUS:-done})"'
    assert anchor in deploy_v2
    deploy_v2 = deploy_v2.replace(
        anchor,
        anchor + '\n    echo "NEW_DEPLOY_SCRIPT_V2_ACTIVE"',
        1,
    )
    (seed / "scripts/dalton/ops/prod/deploy_main.sh").write_text(deploy_v2)
    v2 = _commit_and_push_seed(seed, "v2 updates deploy_main.sh")

    res = _run_via_wrapper(ctx, tmp_path, args=["--bridge"])
    assert res.returncode == 0, res.stdout + res.stderr
    assert f"deploy script changed in {v2}; handshake ok (protocol=2); re-executing updated script" in res.stdout
    assert res.stdout.count("re-executing updated script") == 1
    assert res.stdout.count("NEW_DEPLOY_SCRIPT_V2_ACTIVE") == 1

    git_lines = Path(ctx["git_trace"]).read_text().splitlines()
    assert [line for line in git_lines if line.startswith("git:fetch")] == ["git:fetch -q origin"]
    assert len([line for line in git_lines if line.startswith("git:merge")]) == 1
    assert Path(ctx["wait_idle_trace"]).read_text().splitlines() == ["wait_idle_v1"]

    launchctl_lines = Path(ctx["launchctl_trace"]).read_text().splitlines()
    assert len([line for line in launchctl_lines if "kickstart" in line and "com.dalton.pg-backup" in line]) == 1
    assert len([line for line in launchctl_lines if "com.dalton.researchdata-bridge" in line]) == 1
    assert list((tmp_path / "reexec-tmpdir").glob("dalton-deploy-reexec.*")) == []
    assert not _is_lock_held(Path(ctx["lock_file"]))


def test_reexec_for_companion_wait_idle_change_and_no_reexec_when_unchanged(tmp_path: Path) -> None:
    # Case 1: only wait_backend_idle.sh changes
    c1 = tmp_path / "wait_idle_changed"
    c1.mkdir()
    ctx1 = _setup_strict_deploy_env(c1, busy_once_post_ff=True)
    wait_trace = Path(ctx1["wait_idle_trace"])
    (Path(ctx1["seed"]) / "scripts/dalton/ops/prod/wait_backend_idle.sh").write_text(
        f'#!/bin/bash\nset -euo pipefail\necho "wait_idle_v2" >> "{wait_trace}"\nexit 0\n'
    )
    (Path(ctx1["seed"]) / "scripts/dalton/ops/prod/wait_backend_idle.sh").chmod(0o755)
    v2 = _commit_and_push_seed(Path(ctx1["seed"]), "v2 updates wait_backend_idle.sh")

    res1 = _run_via_wrapper(ctx1, c1)
    assert res1.returncode == 0, res1.stdout + res1.stderr
    assert f"deploy script changed in {v2}; handshake ok (protocol=2); re-executing updated script" in res1.stdout
    assert wait_trace.read_text().splitlines() == ["wait_idle_v1", "wait_idle_v2"]

    # Case 2: neither script changes -> no re-exec
    c2 = tmp_path / "unchanged"
    c2.mkdir()
    ctx2 = _setup_strict_deploy_env(c2)
    (Path(ctx2["seed"]) / "app.py").write_text("v2\n")
    _commit_and_push_seed(Path(ctx2["seed"]), "v2 app only")

    res2 = _run_via_wrapper(ctx2, c2)
    assert res2.returncode == 0, res2.stdout + res2.stderr
    assert "re-executing updated script" not in res2.stdout


def test_reexec_handshake_stops_without_executing_child_on_incompatible_target(tmp_path: Path) -> None:
    for label, mutate_script, extra_args, expected_err in (
        (
            "legacy_no_protocol_flag",
            lambda _s: "#!/bin/bash\necho 'SHOULD_NOT_RUN_LEGACY' >&2\nexit 0\n",
            [],
            "re-exec protocol mismatch",
        ),
        (
            "protocol_version_99",
            lambda s: s.replace("DEPLOY_RESUME_PROTOCOL=2", "DEPLOY_RESUME_PROTOCOL=99", 1),
            [],
            "re-exec protocol mismatch",
        ),
        (
            "rejected_cli_arg",
            lambda s: s.replace("--bridge) BRIDGE=1;;", "# --bridge removed", 1),
            ["--bridge"],
            "rejected state format or CLI arguments",
        ),
        (
            "syntax_error",
            lambda _s: "#!/bin/bash\nif then fi\n",
            [],
            "failed bash -n syntax check",
        ),
    ):
        case_dir = tmp_path / label
        case_dir.mkdir()
        ctx = _setup_strict_deploy_env(case_dir)
        seed = Path(ctx["seed"])
        orig_text = (seed / "scripts/dalton/ops/prod/deploy_main.sh").read_text()
        (seed / "scripts/dalton/ops/prod/deploy_main.sh").write_text(mutate_script(orig_text))
        _commit_and_push_seed(seed, f"v2 {label}")

        res = _run_via_wrapper(ctx, case_dir, args=extra_args)
        assert res.returncode != 0, label
        assert expected_err in res.stderr, f"{label}: stderr={res.stderr}"
        assert "SHOULD_NOT_RUN_LEGACY" not in res.stderr
        # Manual instructions printed; services & frontend untouched
        assert "AUTO-REVERT ABORTED" in res.stdout
        assert os.readlink(Path(ctx["repo"]) / "web/releases/current") == "v1-initial"
        assert not Path(ctx["build_trace"]).exists()
        assert not _is_lock_held(Path(ctx["lock_file"]))


def test_reexec_rejects_unpaired_env_vars(tmp_path: Path) -> None:
    ctx = _setup_strict_deploy_env(tmp_path)
    (Path(ctx["seed"]) / "app.py").write_text("v2\n")
    _commit_and_push_seed(Path(ctx["seed"]), "v2")

    res_only_marker = _run_via_wrapper(ctx, tmp_path, extra_env={"DALTON_DEPLOY_REEXECED": "deadbeef"})
    assert res_only_marker.returncode == 2
    assert "must be set together" in res_only_marker.stderr
    assert not Path(ctx["git_trace"]).exists()

    res_only_state = _run_via_wrapper(
        ctx, tmp_path, extra_env={"DALTON_DEPLOY_STATE_FILE": str(tmp_path / "state.env")}
    )
    assert res_only_state.returncode == 2
    assert "must be set together" in res_only_state.stderr
    assert not Path(ctx["git_trace"]).exists()


def test_deploy_mutex_blocks_concurrent_deploy_across_parent_and_reexec_child(tmp_path: Path) -> None:
    # Pause a re-execed child inside build_frontend_prod.sh and verify a second deploy is blocked by the same flock
    ready_file = tmp_path / "child_build.ready"
    go_file = tmp_path / "child_build.go"
    ctx = _setup_strict_deploy_env(tmp_path, build_barrier=(ready_file, go_file))
    seed = Path(ctx["seed"])

    deploy_v2 = (seed / "scripts/dalton/ops/prod/deploy_main.sh").read_text()
    anchor = 'echo "$(date \'+%T\') resumed re-executed deploy script for ${TARGET} (protocol=${DEPLOY_RESUME_PROTOCOL}, PRE_FF=${PRE_FF}, backup=${DEPLOY_BACKUP_STATUS:-done})"'
    deploy_v2 = deploy_v2.replace(anchor, anchor + '\n    echo "CHILD_LOCK_HOLDING"', 1)
    (seed / "scripts/dalton/ops/prod/deploy_main.sh").write_text(deploy_v2)
    _commit_and_push_seed(seed, "v2 reexec mutex test")

    cmd1, env1 = _wrapper_env_and_cmd(ctx, tmp_path, wrapper_name="wrapper-1")
    proc1 = subprocess.Popen(cmd1, env=env1, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 15
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists(), "re-execed child never reached build barrier"

        # While re-execed child is running, a second independent deploy MUST fail immediately on flock
        cmd2, env2 = _wrapper_env_and_cmd(ctx, tmp_path, wrapper_name="wrapper-2")
        res2 = subprocess.run(cmd2, env=env2, capture_output=True, text=True, check=False, timeout=10)
        assert res2.returncode != 0
        assert "another deploy is already running" in res2.stderr

        # Release child barrier -> first deploy completes cleanly and releases lock
        go_file.write_text("go\n")
        out1, err1 = proc1.communicate(timeout=30)
        assert proc1.returncode == 0, out1 + err1
        assert "CHILD_LOCK_HOLDING" in out1
    finally:
        go_file.write_text("go\n")
        if proc1.poll() is None:
            proc1.kill()

    assert not _is_lock_held(Path(ctx["lock_file"]))


# ---------------------------------------------------------------------------
# 6. r3: Dedicated --first-releases-rollout, live mount drift, pre-recreate guard,
#        /__edge_health body, help-only version.json release identity,
#        bidirectional assets/ + help/_astro/ carry-over, unified FD 9 lock,
#        process-group SIGTERM termination, and exclusive mkdir reservation.
# ---------------------------------------------------------------------------


def test_first_releases_rollout_migrates_legacy_dist_without_version_json_and_rolls_back_on_edge_failure(
    tmp_path: Path,
) -> None:
    def _build_rollout_repo(case_dir: Path, *, edge_check_fail: bool, live_mounts: str) -> dict[str, Path | str]:
        repo = case_dir / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "deploy/dalton").mkdir(parents=True)
        (repo / "scripts/dalton/ops/prod").mkdir(parents=True)
        (repo / "web").mkdir(parents=True)

        # Commit v1: legacy web/dist -> /srv mount & Caddyfile
        (repo / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n"
            "  edge:\n"
            "    image: caddy:2.9.1-alpine\n"
            "    volumes:\n"
            "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
            "      - ./web/dist:/srv:ro\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(":8088 {\n\troot * /srv\n}\n")
        shutil.copy2(_DEPLOY_SCRIPT, repo / "scripts/dalton/ops/prod/deploy_main.sh")
        shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1 legacy dist mount"], check=True)
        v1 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

        # Commit v2: web/releases -> /srv-releases mount & releases Caddyfile
        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n"
            "  edge:\n"
            "    image: caddy:2.9.1-alpine\n"
            "    volumes:\n"
            "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
            "      - ./web/releases:/srv-releases:ro\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ":8088 {\n"
            "\troot * /srv-releases/current\n"
            "\thandle /__edge_health {\n"
            '\t\trespond "dalton-edge-ok releases-v1" 200\n'
            "\t}\n"
            "}\n"
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v2 releases mount"], check=True)

        # Seed legacy web/dist WITHOUT version.json (valid index.html, assets/, help/index.html)
        _write_release_bundle(
            repo / "web/dist",
            tag="legacy-prod",
            entry_chunk="index-old11111.js",
            help_astro_assets={"guide.Ab12Cd34.js": "console.log('help');\n"},
            include_version_json=False,
        )
        assert not (repo / "web/dist/version.json").exists()

        fake_bin = case_dir / "bin"
        fake_bin.mkdir()
        docker_trace = case_dir / "docker.trace"
        (fake_bin / "docker").write_text(
            f"""#!/bin/bash
set -euo pipefail
echo "docker:$*" >> "{docker_trace}"
case "${{1:-}}" in
  inspect)
    if [[ "$*" == *"State.Running"* ]]; then
      echo "true"
      exit 0
    fi
    if [[ "$*" == *"com.docker.compose.project"* ]]; then
      echo ""
      exit 0
    fi
    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then
      printf '%s\\n' "{live_mounts}"
      exit 0
    fi
    ;;
  ps)
    echo "edge-cid-1"
    exit 0
    ;;
  logs)
    echo "edge log line"
    exit 0
    ;;
  run)
    if [[ "$*" == *"--entrypoint caddy"* ]] && [[ "$*" == *"validate --config /etc/caddy/Caddyfile --adapter caddyfile"* ]]; then
      exit 0
    fi
    ;;
  compose)
    if [[ "$*" == *" config"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        rb_cf=""
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ] && [[ "$a" == *"docker-compose.dalton.yml" ]]; then rb_cf="$a"; fi
          prev="$a"
        done
        cat "$rb_cf"
      else
        cat "{repo}/docker-compose.dalton.yml"
      fi
      exit 0
    elif [[ "$*" == *"up -d --no-deps --force-recreate edge"* ]]; then
      exit 0
    fi
    ;;
esac
echo "UNEXPECTED_DOCKER:$*" >&2
exit 99
"""
        )
        (fake_bin / "curl").write_text(
            """#!/bin/bash
set -euo pipefail
if [[ "$*" == *":8088/__edge_health"* ]]; then
  printf 'dalton-edge-ok releases-v1\\n200'
  exit 0
fi
if [[ "$*" == *"http://127.0.0.1:8088/"* ]]; then
  printf '<!doctype html><div id="root">legacy</div>\\n200'
  exit 0
fi
echo "UNEXPECTED_CURL:$*" >&2
exit 99
"""
        )
        (fake_bin / "sleep").write_text("#!/bin/bash\nexit 0\n")
        for name in ("docker", "curl", "sleep"):
            (fake_bin / name).chmod(0o755)
        (fake_bin / "python3").symlink_to(sys.executable)

        stub_dir = _install_edge_http_stub(case_dir)
        env = {
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(case_dir),
            "DALTON_REPO_ROOT": str(repo),
            "DALTON_DEPLOY_LOCK_FILE": str(case_dir / "locks/deploy.lock"),
            "DALTON_EDGE_PORT": "8088",
            "PYTHONPATH": str(stub_dir),
            "TEST_EDGE_SERVE_ROOT": str(repo / "web/releases/current"),
        }
        if edge_check_fail:
            env["TEST_EDGE_OVERRIDE_BUILD"] = "index-wrong999.js"
        return {
            "repo": repo,
            "v1": v1,
            "docker_trace": docker_trace,
            "env": env,
            "lock_file": case_dir / "locks/deploy.lock",
        }

    # Case 1: Successful first rollout (web/dist lacks version.json -> generated legacy-* identity, web/dist preserved)
    c1 = tmp_path / "success"
    c1.mkdir()
    r1 = _build_rollout_repo(c1, edge_check_fail=False, live_mounts=f"{c1}/repo/web/dist->/srv")
    res1 = subprocess.run(
        ["bash", str(Path(r1["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=r1["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode == 0, res1.stdout + res1.stderr
    assert "first-releases-rollout completed" in res1.stdout
    repo1 = Path(r1["repo"])
    cur_rel = os.readlink(repo1 / "web/releases/current")
    assert cur_rel.startswith("legacy-")
    assert (repo1 / "web/dist/index.html").is_file()  # web/dist MUST be preserved
    ver_payload = json.loads((repo1 / "web/releases/current/version.json").read_text(encoding="utf-8"))
    assert ver_payload["legacy_generated"] is True
    assert ver_payload["legacy_id"].startswith("legacy-")
    assert ver_payload["release_id"] == cur_rel
    assert ver_payload["entry"] == "index-old11111.js"
    # Legacy copied JS only reads `build`, so migrated legacy bundle keeps `build == entry`
    # while `release` carries the unique help_hash + legacy release id checked by check_public_edge.sh.
    assert ver_payload["build"] == "index-old11111.js"
    assert ver_payload["help_hash"]
    assert ver_payload["release"] == f"index-old11111.js+help-{ver_payload['help_hash']}@{cur_rel}"
    assert not _is_lock_held(Path(r1["lock_file"]))

    # Case 2: Public edge check fails -> edge rolled back onto PRE_FF compose + Caddyfile mounting web/dist
    c2 = tmp_path / "rollback"
    c2.mkdir()
    r2 = _build_rollout_repo(c2, edge_check_fail=True, live_mounts=f"{c2}/repo/web/dist->/srv")
    res2 = subprocess.run(
        ["bash", str(Path(r2["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=r2["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode != 0
    assert "first-releases-rollout rollback: edge recreated onto web/dist" in res2.stdout
    repo2 = Path(r2["repo"])
    assert (repo2 / "web/dist/index.html").is_file()
    rb_root = repo2 / "web/releases/.first-rollout-rollback"
    attempts = sorted(p for p in rb_root.glob("attempt-*") if p.is_dir())
    assert len(attempts) == 1, attempts
    rb_dir = attempts[0]
    assert "root * /srv" in (rb_dir / "Caddyfile").read_text(encoding="utf-8")
    rb_compose = (rb_dir / "docker-compose.dalton.yml").read_text(encoding="utf-8")
    assert "./web/dist:/srv:ro" in rb_compose
    assert f"./web/releases/.first-rollout-rollback/{rb_dir.name}/Caddyfile:/etc/caddy/Caddyfile" in rb_compose
    assert (rb_dir / "resolved-compose.yml").is_file()
    docker_lines = Path(r2["docker_trace"]).read_text().splitlines()
    assert any(
        f".first-rollout-rollback/{rb_dir.name}/resolved-compose.yml" in line and "up -d --no-deps --force-recreate edge" in line
        for line in docker_lines
    ), docker_lines
    assert not _is_lock_held(Path(r2["lock_file"]))

    # Case 3: Running edge already mounts /srv-releases -> refused before touching anything
    c3 = tmp_path / "already_migrated"
    c3.mkdir()
    r3 = _build_rollout_repo(c3, edge_check_fail=False, live_mounts=f"{c3}/repo/web/releases->/srv-releases")
    res3 = subprocess.run(
        ["bash", str(Path(r3["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=r3["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res3.returncode != 0
    assert "requires running dalton-langalpha-edge-1 to currently bind-mount" in res3.stderr
    assert not (Path(r3["repo"]) / "web/releases/current").exists()
    assert not _is_lock_held(Path(r3["lock_file"]))


def test_normal_deploy_refuses_edge_recreate_without_servable_current_and_detects_live_mount_drift(
    tmp_path: Path,
) -> None:
    # Case A: Caddyfile changes (RECREATE_EDGE=1), target uses /srv-releases, but no servable current or web/dist exists
    case_a = tmp_path / "refuse_without_current"
    case_a.mkdir()
    ctx_a = _setup_strict_deploy_env(case_a, seed_legacy_dist=False, seed_v1_release=False)
    seed_a = Path(ctx_a["seed"])
    repo_a = Path(ctx_a["repo"])
    # Seed an UNSERVABLE legacy web/dist (missing help/index.html) so migration cannot produce a servable current
    (repo_a / "web/dist/assets").mkdir(parents=True)
    (repo_a / "web/dist/index.html").write_text("<html>incomplete</html>")
    (seed_a / "deploy/dalton").mkdir(parents=True, exist_ok=True)
    (seed_a / "deploy/dalton/Caddyfile").write_text(":8088 {\n\troot * /srv-releases/current\n}\n")
    _commit_and_push_seed(seed_a, "v2 caddyfile change")

    res_a = _run_via_wrapper(ctx_a, case_a)
    assert res_a.returncode != 0
    assert "refusing to recreate edge onto /srv-releases because" in res_a.stderr
    assert "web/releases/current is missing or unservable" in res_a.stderr
    docker_a = Path(ctx_a["docker_trace"]).read_text()
    assert "up -d --no-deps --force-recreate edge" not in docker_a
    assert not _is_lock_held(Path(ctx_a["lock_file"]))

    # Case B: Zero git diff (HEAD == TARGET), but docker inspect reports running edge mounts a stale host releases path
    case_b = tmp_path / "live_mount_drift"
    case_b.mkdir()
    ctx_b = _setup_strict_deploy_env(case_b, seed_v1_release=True)
    fake_docker_b = Path(ctx_b["fake_bin"]) / "docker"
    docker_text_b = fake_docker_b.read_text()
    docker_text_b = docker_text_b.replace(
        '  inspect)\n    echo "backend mem_limit=6442450944"\n    exit 0\n    ;;',
        '  inspect)\n'
        '    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi\n'
        '    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo ""; exit 0; fi\n'
        '    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then\n'
        '      echo "bind|/stale/other-checkout/web/releases->/srv-releases"\n'
        '      exit 0\n'
        '    fi\n'
        '    echo "backend mem_limit=6442450944"\n'
        '    exit 0\n'
        '    ;;',
        1,
    )
    fake_docker_b.write_text(docker_text_b)
    fake_docker_b.chmod(0o755)

    # No new commit pushed to seed_b -> HEAD == TARGET (zero git diff!)
    res_b = _run_via_wrapper(ctx_b, case_b)
    assert res_b.returncode == 0, res_b.stdout + res_b.stderr
    events_b = Path(ctx_b["events_trace"]).read_text()
    assert "docker_compose_up:compose up -d --no-deps --force-recreate edge" in events_b
    assert "edge_ready_probe:" in events_b


def test_edge_ready_rejects_spa_fallback_and_version_json_distinguishes_help_only_deploys(
    tmp_path: Path,
) -> None:
    # Part A: edge_ready rejects SPA HTML 200 and old "ok" 200, accepts "dalton-edge-ok releases-v1" 200
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    for body_out, expect_ok in (
        ('<!doctype html><div id="root">SPA fallback</div>', False),
        ("ok", False),
        ("dalton-edge-ok releases-v1", True),
    ):
        (fake_bin / "curl").write_text(
            f"""#!/bin/bash
set -euo pipefail
if [[ "$*" == *"http://127.0.0.1:8088/__edge_health"* ]]; then
  printf '%s\\n200' '{body_out}'
  exit 0
fi
echo "UNEXPECTED_CURL:$*" >&2
exit 99
"""
        )
        (fake_bin / "curl").chmod(0o755)
        res = subprocess.run(
            [
                "bash",
                "-c",
                f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1; DALTON_EDGE_PORT=8088; source "{_DEPLOY_SCRIPT}"; edge_ready 2',
            ],
            env={"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert (res.returncode == 0) is expect_ok, f"body={body_out!r} rc={res.returncode}"

    # Part B: Two builds with identical app entry chunk (index-same1234.js) and different docs-site content
    repo = tmp_path / "repo"
    (repo / "scripts/dalton").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)
    (repo / "docs-site").mkdir(parents=True)
    shutil.copy2(_BUILD_FRONTEND_SCRIPT, repo / "scripts/dalton/build_frontend_prod.sh")
    shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")
    (repo / "web/.env.dalton").write_text("VITE_BRAND_NAME=Everflow\nVITE_BRAND_MARK=everflow\n")
    (repo / ".env").write_text(
        "APP_ENV=dalton\nDALTON_AUTH_MODE=supabase\n"
        "SUPABASE_URL=https://example.supabase.co\nSUPABASE_PUBLISHABLE_KEY=pk_test_123\n"
    )
    help_ver_file = tmp_path / "help_version.txt"
    (fake_bin / "pnpm").write_text(
        f"""#!/bin/bash
set -euo pipefail
if [ "$(basename "$(pwd)")" = "docs-site" ]; then
  if [ "${{1:-}}" = "install" ]; then exit 0; fi
  if [ "${{1:-}}" = "build" ]; then
    mkdir -p dist/_astro
    hv=$(cat "{help_ver_file}")
    echo "<h1>Help ${{hv}}</h1>" > dist/index.html
    echo "console.log('${{hv}}');" > "dist/_astro/page.${{hv}}.js"
    exit 0
  fi
  exit 99
fi
if [ "${{1:-}}" = "build" ]; then
  out="${{DALTON_VITE_OUT_DIR:-dist}}"
  mkdir -p "${{out}}/assets"
  echo '<!doctype html><div id="root">app</div><script type="module" src="/assets/index-same1234.js"></script>' > "${{out}}/index.html"
  echo '{{"build":"index-same1234.js"}}' > "${{out}}/version.json"
  echo 'console.log("same-app");' > "${{out}}/assets/index-same1234.js"
  exit 0
fi
exit 99
"""
    )
    (fake_bin / "pnpm").chmod(0o755)
    build_env = {"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)}

    help_ver_file.write_text("helpv1")
    r1 = subprocess.run(
        ["bash", str(repo / "scripts/dalton/build_frontend_prod.sh"), "--require-supabase", "--out-dir", "web/releases/help-r1"],
        cwd=repo,
        env=build_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r1.returncode == 0, r1.stdout + r1.stderr

    help_ver_file.write_text("helpv2")
    r2 = subprocess.run(
        ["bash", str(repo / "scripts/dalton/build_frontend_prod.sh"), "--require-supabase", "--out-dir", "web/releases/help-r2"],
        cwd=repo,
        env=build_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert r2.returncode == 0, r2.stdout + r2.stderr

    v1_json = json.loads((repo / "web/releases/help-r1/version.json").read_text(encoding="utf-8"))
    v2_json = json.loads((repo / "web/releases/help-r2/version.json").read_text(encoding="utf-8"))
    assert v1_json["entry"] == "index-same1234.js"
    assert v2_json["entry"] == "index-same1234.js"
    assert v1_json["help_hash"] != v2_json["help_hash"]
    assert v1_json["build"] != v2_json["build"]
    assert v1_json["release"] != v2_json["release"]

    # check_public_edge.sh fails with ONLY --expected-build when edge still serves help-r1 while expecting help-r2
    stub_dir = _install_edge_http_stub(tmp_path)
    check_stale = subprocess.run(
        [
            "bash",
            str(repo / "scripts/dalton/check_public_edge.sh"),
            "--expected-build",
            v2_json["build"],
            "--dist-dir",
            str(repo / "web/releases/help-r2"),
        ],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "DALTON_EDGE_PORT": "8088",
            "PYTHONPATH": str(stub_dir),
            "TEST_EDGE_SERVE_ROOT": str(repo / "web/releases/help-r1"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert check_stale.returncode != 0
    assert "HTTP /version.json build mismatch" in check_stale.stderr

    # Once edge serves help-r2, check_public_edge.sh passes
    check_ok = subprocess.run(
        [
            "bash",
            str(repo / "scripts/dalton/check_public_edge.sh"),
            "--expected-build",
            v2_json["build"],
            "--expected-release",
            v2_json["release"],
            "--dist-dir",
            str(repo / "web/releases/help-r2"),
        ],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "DALTON_EDGE_PORT": "8088",
            "PYTHONPATH": str(stub_dir),
            "TEST_EDGE_SERVE_ROOT": str(repo / "web/releases/help-r2"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert check_ok.returncode == 0, check_ok.stdout + check_ok.stderr
    assert "ALL PUBLIC EDGE CHECKS PASSED." in check_ok.stdout


def test_forward_switch_keeps_old_help_astro_assets_200_and_rollback_keeps_new_assets_200(
    tmp_path: Path,
) -> None:
    def _probe_http(stub_dir: Path, serve_root: Path, url_path: str) -> int:
        res = subprocess.run(
            [
                sys.executable,
                "-c",
                f"import http.client; c = http.client.HTTPConnection('127.0.0.1', 8088); "
                f"c.request('GET', {url_path!r}); print(c.getresponse().status)",
            ],
            env={"PYTHONPATH": str(stub_dir), "TEST_EDGE_SERVE_ROOT": str(serve_root)},
            capture_output=True,
            text=True,
            check=True,
        )
        return int(res.stdout.strip())

    # Part A: Forward switch v1 -> v2 preserves HTTP 200 on old /help/_astro/* and /assets/*
    c1 = tmp_path / "forward"
    c1.mkdir()
    stub1 = _install_edge_http_stub(c1)
    ctx1 = _setup_strict_deploy_env(c1, seed_v1_release=False)
    repo1 = Path(ctx1["repo"])
    seed1 = Path(ctx1["seed"])
    rel1_v1 = repo1 / "web/releases/v1-initial"
    _write_release_bundle(
        rel1_v1,
        tag="v1",
        entry_chunk="index-v1aaaa11.js",
        extra_assets={"lazy-v1bbbb22.js": "v1-lazy\n"},
        help_astro_assets={
            "doc-v1.Ab12Cd34.js": "v1-help-js\n",
            "Starlight.Xy98Zw76.css": "/* v1 css */\n",
        },
    )
    os.symlink("v1-initial", repo1 / "web/releases/current")
    (seed1 / "scripts/dalton/build_frontend_prod.sh").write_text(
        _make_fake_build_script(
            tag="v2",
            entry_chunk="index-v2cccc33.js",
            extra_chunk="lazy-v2dddd44.js",
            help_astro_chunk="doc-v2.Ef56Gh78.js",
            trace_file=Path(ctx1["build_trace"]),
        )
    )
    (seed1 / "app.py").write_text("v2\n")
    _commit_and_push_seed(seed1, "v2 with help astro")

    res1 = _run_via_wrapper(ctx1, c1)
    assert res1.returncode == 0, res1.stdout + res1.stderr
    cur1 = repo1 / "web/releases/current"
    assert os.readlink(cur1) != "v1-initial"
    assert _probe_http(stub1, cur1, "/help/_astro/doc-v1.Ab12Cd34.js") == 200
    assert _probe_http(stub1, cur1, "/help/_astro/Starlight.Xy98Zw76.css") == 200
    assert _probe_http(stub1, cur1, "/help/_astro/doc-v2.Ef56Gh78.js") == 200
    assert _probe_http(stub1, cur1, "/assets/lazy-v1bbbb22.js") == 200
    assert _probe_http(stub1, cur1, "/assets/index-v2cccc33.js") == 200

    # Part B: Public edge check failure rollback v2 -> v1 preserves HTTP 200 on new /assets/* and /help/_astro/*
    c2 = tmp_path / "rollback"
    c2.mkdir()
    stub2 = _install_edge_http_stub(c2)
    ctx2 = _setup_strict_deploy_env(c2, seed_v1_release=True, public_edge_fail=True)
    repo2 = Path(ctx2["repo"])
    seed2 = Path(ctx2["seed"])
    (seed2 / "scripts/dalton/build_frontend_prod.sh").write_text(
        _make_fake_build_script(
            tag="v2",
            entry_chunk="index-v2cccc33.js",
            extra_chunk="lazy-v2dddd44.js",
            help_astro_chunk="doc-v2.Ef56Gh78.js",
            trace_file=Path(ctx2["build_trace"]),
        )
    )
    (seed2 / "app.py").write_text("v2\n")
    _commit_and_push_seed(seed2, "v2 edge fail rollback")

    res2 = _run_via_wrapper(ctx2, c2)
    assert res2.returncode != 0
    cur2 = repo2 / "web/releases/current"
    assert os.readlink(cur2) == "v1-initial"
    # Even after rollback deleted the unverified v2 dir, v2's hashed assets remain 200 under current!
    assert _probe_http(stub2, cur2, "/assets/index-v2cccc33.js") == 200
    assert _probe_http(stub2, cur2, "/assets/lazy-v2dddd44.js") == 200
    assert _probe_http(stub2, cur2, "/help/_astro/doc-v2.Ef56Gh78.js") == 200
    assert _probe_http(stub2, cur2, "/assets/index-v1aaaa11.js") == 200


def test_manual_switch_manual_build_and_prune_share_deploy_flock(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rel_dir = repo / "web/releases"
    rel_dir.mkdir(parents=True)
    (repo / "scripts/dalton").mkdir(parents=True)
    for idx in range(1, 5):
        _write_release_bundle(
            rel_dir / f"rel-v{idx}",
            tag=f"v{idx}",
            entry_chunk=f"index-v{idx}aaaa.js",
        )
        os.utime(rel_dir / f"rel-v{idx}", (1700000000 + idx * 100, 1700000000 + idx * 100))
    os.symlink("rel-v4", rel_dir / "current")

    ready_file = tmp_path / "manual_build.ready"
    go_file = tmp_path / "manual_build.go"
    build_trace = tmp_path / "manual_build.trace"
    (repo / "scripts/dalton/build_frontend_prod.sh").write_text(
        _make_fake_build_script(
            tag="v5",
            entry_chunk="index-v5eeee.js",
            trace_file=build_trace,
            block_on_barrier=(ready_file, go_file),
        )
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    lock_file = tmp_path / "locks/deploy.lock"
    lock_file.parent.mkdir(parents=True)
    env = {
        "PATH": f"{fake_bin}:/bin:/usr/bin",
        "HOME": str(tmp_path),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
    }

    # 1. Hold FD 9 flock externally -> manual_switch, manual_build, and prune MUST all fail immediately
    lock_fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for fn_call in (
            'manual_switch_frontend_release "rel-v1"',
            "manual_build_frontend_release",
            "prune_old_frontend_releases 2",
        ):
            res = subprocess.run(
                ["bash", "-c", f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; {fn_call}'],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            assert res.returncode != 0, f"{fn_call} unexpectedly succeeded while lock held"
            assert "another deploy is already running" in res.stderr, f"{fn_call}: {res.stderr}"
        assert os.readlink(rel_dir / "current") == "rel-v4"
        assert (rel_dir / "rel-v1").is_dir()
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    # 2. While manual_build_frontend_release runs (paused at barrier), prune & manual_switch are blocked by the same flock
    mb_proc = subprocess.Popen(
        ["bash", "-c", f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; manual_build_frontend_release'],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists(), "manual_build barrier not reached"

        for blocked_call in ('manual_switch_frontend_release "rel-v1"', "prune_old_frontend_releases 2"):
            res_blk = subprocess.run(
                ["bash", "-c", f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; {blocked_call}'],
                env=env,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            assert res_blk.returncode != 0, blocked_call
            assert "another deploy is already running" in res_blk.stderr, blocked_call

        go_file.write_text("go\n")
        out_mb, err_mb = mb_proc.communicate(timeout=15)
        assert mb_proc.returncode == 0, out_mb + err_mb
    finally:
        go_file.write_text("go\n")
        if mb_proc.poll() is None:
            mb_proc.kill()

    assert not _is_lock_held(lock_file)

    # 3. After lock released, manual_switch and prune both work and release lock cleanly
    res_sw = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; manual_switch_frontend_release "rel-v2"'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_sw.returncode == 0, res_sw.stdout + res_sw.stderr
    assert os.readlink(rel_dir / "current") == "rel-v2"
    assert not _is_lock_held(lock_file)

    res_pr = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1; source "{_DEPLOY_SCRIPT}"; prune_old_frontend_releases 2'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_pr.returncode == 0, res_pr.stdout + res_pr.stderr
    assert (rel_dir / "rel-v2").is_dir()
    assert not _is_lock_held(lock_file)


def test_sigterm_terminates_entire_build_process_group_before_cleanup_and_lock_release(
    tmp_path: Path,
) -> None:
    ready_file = tmp_path / "pgid_build.ready"
    go_file = tmp_path / "pgid_build.go"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    ctx = _setup_strict_deploy_env(tmp_path, build_barrier=(ready_file, go_file))
    seed = Path(ctx["seed"])
    (seed / "scripts/dalton/build_frontend_prod.sh").write_text(
        _make_fake_build_script(
            tag="v2",
            entry_chunk="index-v2cccc33.js",
            trace_file=Path(ctx["build_trace"]),
            block_on_barrier=(ready_file, go_file),
            spawn_grandchild_pid_file=grandchild_pid_file,
        )
    )
    (seed / "app.py").write_text("v2\n")
    _commit_and_push_seed(seed, "v2 pgid term test")

    cmd, env = _wrapper_env_and_cmd(ctx, tmp_path)
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    gpid = 0
    try:
        deadline = time.monotonic() + 15
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists(), "build barrier not reached"
        gpid = int(grandchild_pid_file.read_text(encoding="utf-8").strip())
        # Grandchild is alive before SIGTERM
        os.kill(gpid, 0)

        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode != 0, out + err

        # Immediately upon deploy_main.sh exit, the grandchild MUST already be terminated!
        grandchild_alive = True
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            grandchild_alive = False
        assert not grandchild_alive, f"grandchild PID {gpid} survived deploy_main.sh SIGTERM"
    finally:
        if proc.poll() is None:
            proc.kill()
        if gpid > 0:
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass

    rel_dir = Path(ctx["repo"]) / "web/releases"
    assert os.readlink(rel_dir / "current") == "v1-initial"
    assert [p.name for p in rel_dir.iterdir() if p.is_dir() and not p.is_symlink()] == ["v1-initial"]
    assert not _is_lock_held(Path(ctx["lock_file"]))


def test_build_frontend_prod_exclusive_mkdir_rejects_concurrent_reservation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts/dalton").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)
    (repo / "docs-site").mkdir(parents=True)
    shutil.copy2(_BUILD_FRONTEND_SCRIPT, repo / "scripts/dalton/build_frontend_prod.sh")
    (repo / "web/.env.dalton").write_text("VITE_BRAND_NAME=Everflow\nVITE_BRAND_MARK=everflow\n")
    (repo / ".env").write_text(
        "APP_ENV=dalton\nDALTON_AUTH_MODE=supabase\n"
        "SUPABASE_URL=https://example.supabase.co\nSUPABASE_PUBLISHABLE_KEY=pk_test_123\n"
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    real_tr = shutil.which("tr")
    assert real_tr
    pnpm_trace = tmp_path / "pnpm.trace"
    target_rel = repo / "web/releases/toctou-race-id"

    # Simulate a second concurrent builder winning the directory right after Step 0 validation
    # and right before Step 4's atomic `mkdir "$DIST_DIR"` reservation.
    (fake_bin / "tr").write_text(
        f"""#!/bin/bash
set -euo pipefail
if [ ! -d "{target_rel}" ]; then
  mkdir -p "{target_rel}"
  echo "winner-owner" > "{target_rel}/sentinel.txt"
fi
exec "{real_tr}" "$@"
"""
    )
    (fake_bin / "pnpm").write_text(
        f"""#!/bin/bash
echo "pnpm_should_not_run:$*" >> "{pnpm_trace}"
exit 99
"""
    )
    (fake_bin / "tr").chmod(0o755)
    (fake_bin / "pnpm").chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)

    res = subprocess.run(
        [
            "bash",
            str(repo / "scripts/dalton/build_frontend_prod.sh"),
            "--require-supabase",
            "--out-dir",
            "web/releases/toctou-race-id",
        ],
        cwd=repo,
        env={"PATH": f"{fake_bin}:/usr/bin:/bin", "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 2, res.stdout + res.stderr
    assert "already exists or was reserved concurrently" in res.stderr
    assert not pnpm_trace.exists()
    assert (target_rel / "sentinel.txt").read_text(encoding="utf-8").strip() == "winner-owner"


# ---------------------------------------------------------------------------
# 7. r4: Reproducing tests for Codex + Grok r3 review findings
# ---------------------------------------------------------------------------


def test_first_releases_rollout_prepares_rollback_before_recreate_and_rolls_back_on_sigterm(
    tmp_path: Path,
) -> None:
    def _make_env(
        case_dir: Path,
        *,
        health_barrier: tuple[Path, Path] | None = None,
        fail_rollback_config: bool = False,
    ) -> dict[str, Path | dict[str, str]]:
        repo = case_dir / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "deploy/dalton").mkdir(parents=True)
        (repo / "scripts/dalton/ops/prod").mkdir(parents=True)
        (repo / "web").mkdir(parents=True)

        (repo / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n"
            "  edge:\n"
            "    image: caddy:2.9.1-alpine\n"
            "    volumes:\n"
            "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
            "      - ./web/dist:/srv:ro\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ":8088 {\n\troot * /srv\n\thandle /__edge_health {\n\t\trespond \"ok\" 200\n\t}\n}\n"
        )
        shutil.copy2(_DEPLOY_SCRIPT, repo / "scripts/dalton/ops/prod/deploy_main.sh")
        shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1 legacy"], check=True)

        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n"
            "  edge:\n"
            "    image: caddy:2.9.1-alpine\n"
            "    volumes:\n"
            "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
            "      - ./web/releases:/srv-releases:ro\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ":8088 {\n"
            "\troot * /srv-releases/current\n"
            "\thandle /__edge_health {\n"
            '\t\trespond "dalton-edge-ok releases-v1" 200\n'
            "\t}\n"
            "}\n"
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v2 releases"], check=True)

        _write_release_bundle(
            repo / "web/dist",
            tag="legacy-prod",
            entry_chunk="index-old11111.js",
            include_version_json=False,
        )

        fake_bin = case_dir / "bin"
        fake_bin.mkdir()
        docker_trace = case_dir / "docker.trace"
        curl_trace = case_dir / "curl.trace"
        edge_state = case_dir / "edge.state"
        edge_state.write_text("legacy")
        (fake_bin / "docker").write_text(
            f"""#!/bin/bash
set -euo pipefail
echo "docker:$*" >> "{docker_trace}"
case "${{1:-}}" in
  inspect)
    if [[ "$*" == *"State.Running"* ]]; then
      echo "true"
      exit 0
    fi
    if [[ "$*" == *"com.docker.compose.project"* ]]; then
      echo "dalton-proj"
      exit 0
    fi
    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then
      echo "bind|{repo.resolve()}/web/dist->/srv"
      exit 0
    fi
    ;;
  ps)
    echo "edge-cid-1"
    exit 0
    ;;
  logs)
    echo "edge log"
    exit 0
    ;;
  run)
    if [[ "$*" == *"--entrypoint caddy"* ]] && [[ "$*" == *"validate --config /etc/caddy/Caddyfile --adapter caddyfile"* ]]; then
      exit 0
    fi
    ;;
  compose)
    if [[ "$*" == *" config"* ]]; then
      if [ "{1 if fail_rollback_config else 0}" = "1" ] && [[ "$*" == *".first-rollout-rollback"* ]]; then
        echo "ERROR: simulated invalid rollback compose" >&2
        exit 1
      fi
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        rb_cf=""
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ] && [[ "$a" == *"docker-compose.dalton.yml" ]]; then rb_cf="$a"; fi
          prev="$a"
        done
        cat "$rb_cf"
      else
        cat "{repo}/docker-compose.dalton.yml"
      fi
      exit 0
    fi
    if [[ "$*" == *"up -d --no-deps --force-recreate edge"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        echo "rolled_back" > "{edge_state}"
      else
        echo "new_mount" > "{edge_state}"
      fi
      exit 0
    fi
    ;;
esac
echo "UNEXPECTED_DOCKER:$*" >&2
exit 99
"""
        )
        barrier_code = ""
        if health_barrier is not None:
            r_file, g_file = health_barrier
            barrier_code = f"""
if [ "$(cat "{edge_state}")" = "new_mount" ]; then
  touch "{r_file}"
  while [ ! -f "{g_file}" ]; do
    "{sys.executable}" -c 'import time; time.sleep(0.02)'
  done
fi
"""
        (fake_bin / "curl").write_text(
            f"""#!/bin/bash
set -euo pipefail
echo "curl:$*:state=$(cat "{edge_state}")" >> "{curl_trace}"
if [[ "$*" == *":8088/__edge_health"* ]]; then
  {barrier_code}
  if [ "$(cat "{edge_state}")" = "rolled_back" ]; then
    printf 'ok\\n200'
  else
    printf 'dalton-edge-ok releases-v1\\n200'
  fi
  exit 0
fi
if [[ "$*" == *"http://127.0.0.1:8088/"* ]]; then
  printf '<!doctype html><div id="root">legacy-prod</div>\\n200'
  exit 0
fi
echo "UNEXPECTED_CURL:$*" >&2
exit 99
"""
        )
        (fake_bin / "sleep").write_text("#!/bin/bash\nexit 0\n")
        for name in ("docker", "curl", "sleep"):
            (fake_bin / name).chmod(0o755)
        (fake_bin / "python3").symlink_to(sys.executable)

        lock_file = case_dir / "locks/deploy.lock"
        env = {
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(case_dir),
            "DALTON_REPO_ROOT": str(repo),
            "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
            "DALTON_EDGE_PORT": "8088",
        }
        return {
            "repo": repo,
            "docker_trace": docker_trace,
            "curl_trace": curl_trace,
            "edge_state": edge_state,
            "lock_file": lock_file,
            "env": env,
        }

    # Case A: Rollback snapshot validation fails BEFORE recreate_edge -> edge is never touched
    c_a = tmp_path / "prep_fail"
    c_a.mkdir()
    ctx_a = _make_env(c_a, fail_rollback_config=True)
    res_a = subprocess.run(
        ["bash", str(Path(ctx_a["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=ctx_a["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_a.returncode != 0
    assert Path(ctx_a["edge_state"]).read_text().strip() == "legacy"
    assert "up -d --no-deps --force-recreate edge" not in Path(ctx_a["docker_trace"]).read_text()
    assert not _is_lock_held(Path(ctx_a["lock_file"]))

    # Case B: SIGTERM during wait_edge_ready after recreate_edge -> rolls back to web/dist and verifies old HTTP
    c_b = tmp_path / "sigterm_wait_ready"
    c_b.mkdir()
    ready_b = c_b / "edge_wait.ready"
    go_b = c_b / "edge_wait.go"
    ctx_b = _make_env(c_b, health_barrier=(ready_b, go_b))
    proc = subprocess.Popen(
        ["bash", str(Path(ctx_b["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=ctx_b["env"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready_b.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_b.exists(), "edge_ready barrier never reached"
        proc.send_signal(signal.SIGTERM)
        out_b, err_b = proc.communicate(timeout=15)
    finally:
        go_b.write_text("go\n")
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode != 0
    assert Path(ctx_b["edge_state"]).read_text().strip() == "rolled_back", out_b + err_b
    curl_lines = Path(ctx_b["curl_trace"]).read_text()
    assert "state=rolled_back" in curl_lines
    assert "http://127.0.0.1:8088/" in curl_lines
    assert (Path(ctx_b["repo"]) / "web/dist/index.html").is_file()
    assert not _is_lock_held(Path(ctx_b["lock_file"]))


def test_live_mount_drift_checks_source_type_and_running_state(tmp_path: Path) -> None:
    # Case A: Normal deploy detects different host checkout source mounted at /srv-releases (even with zero git diff)
    for label, mount_line in (
        ("other_host_source", "bind|/some/other/checkout/web/releases->/srv-releases"),
        ("wrong_mount_type", "volume|named_releases_vol->/srv-releases"),
    ):
        case_dir = tmp_path / label
        case_dir.mkdir()
        ctx = _setup_strict_deploy_env(case_dir, seed_v1_release=True)
        fake_docker = Path(ctx["fake_bin"]) / "docker"
        docker_text = fake_docker.read_text().replace(
            '  inspect)\n    echo "backend mem_limit=6442450944"\n    exit 0\n    ;;',
            f'  inspect)\n'
            f'    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi\n'
            f'    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo ""; exit 0; fi\n'
            f'    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then echo "{mount_line}"; exit 0; fi\n'
            f'    echo "backend mem_limit=6442450944"\n'
            f'    exit 0\n'
            f'    ;;',
            1,
        )
        fake_docker.write_text(docker_text)
        fake_docker.chmod(0o755)
        res = _run_via_wrapper(ctx, case_dir)
        assert res.returncode == 0, f"{label}: {res.stdout}\n{res.stderr}"
        events = Path(ctx["events_trace"]).read_text()
        assert "docker_compose_up:" in events and "--force-recreate edge" in events, f"{label}: {events}"

    # Case B: --first-releases-rollout rejects (i) stopped edge container and (ii) another checkout's /srv mount
    for label, running_val, mount_line in (
        ("stopped_edge", "false", "{repo}/web/dist->/srv"),
        ("other_checkout_srv", "true", "bind|/other/checkout/web/dist->/srv"),
    ):
        c_dir = tmp_path / label
        c_dir.mkdir()
        repo = c_dir / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "deploy/dalton").mkdir(parents=True)
        (repo / "scripts/dalton/ops/prod").mkdir(parents=True)
        (repo / "web").mkdir(parents=True)
        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n  edge:\n    volumes:\n      - ./web/releases:/srv-releases:ro\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ':8088 {\n\troot * /srv-releases/current\n\trespond "dalton-edge-ok releases-v1" 200\n}\n'
        )
        shutil.copy2(_DEPLOY_SCRIPT, repo / "scripts/dalton/ops/prod/deploy_main.sh")
        _write_release_bundle(repo / "web/dist", tag="v1", entry_chunk="index-v1.js")

        fake_bin = c_dir / "bin"
        fake_bin.mkdir()
        actual_mount = mount_line.format(repo=repo.resolve())
        (fake_bin / "docker").write_text(
            f"""#!/bin/bash
set -euo pipefail
case "${{1:-}}" in
  inspect)
    if [[ "$*" == *"State.Running"* ]]; then echo "{running_val}"; exit 0; fi
    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo ""; exit 0; fi
    echo "{actual_mount}"
    exit 0
    ;;
  compose)
    if [[ "$*" == *" config"* ]]; then cat "{repo}/docker-compose.dalton.yml"; exit 0; fi
    ;;
esac
echo "UNEXPECTED_DOCKER:$*" >&2
exit 99
"""
        )
        (fake_bin / "docker").chmod(0o755)
        (fake_bin / "python3").symlink_to(sys.executable)
        res = subprocess.run(
            ["bash", str(repo / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
            env={
                "PATH": f"{fake_bin}:/bin:/usr/bin",
                "HOME": str(c_dir),
                "DALTON_REPO_ROOT": str(repo),
                "DALTON_DEPLOY_LOCK_FILE": str(c_dir / "locks/deploy.lock"),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode != 0, f"{label} unexpectedly succeeded"
        assert not (repo / "web/releases/current").exists(), label


def test_manual_build_sigterm_terminates_process_group_cleans_unswitched_dir_and_releases_lock(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rel_dir = repo / "web/releases"
    rel_dir.mkdir(parents=True)
    (repo / "scripts/dalton").mkdir(parents=True)
    _write_release_bundle(rel_dir / "v1-initial", tag="v1", entry_chunk="index-v1aaaa11.js")
    os.symlink("v1-initial", rel_dir / "current")

    ready_file = tmp_path / "mb.ready"
    go_file = tmp_path / "mb.go"
    gpid_file = tmp_path / "mb_grandchild.pid"
    (repo / "scripts/dalton/build_frontend_prod.sh").write_text(
        _make_fake_build_script(
            tag="v2",
            entry_chunk="index-v2cccc33.js",
            trace_file=tmp_path / "mb.trace",
            block_on_barrier=(ready_file, go_file),
            spawn_grandchild_pid_file=gpid_file,
        )
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    lock_file = tmp_path / "locks/deploy.lock"
    env = {
        "PATH": f"{fake_bin}:/bin:/usr/bin",
        "HOME": str(tmp_path),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
    }

    proc = subprocess.Popen(
        [
            "bash",
            "-c",
            f'DALTON_DEPLOY_LIB_ONLY=1 source "{_DEPLOY_SCRIPT}" && manual_build_frontend_release',
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    gpid = 0
    try:
        deadline = time.monotonic() + 15
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists(), "manual_build barrier never reached"
        gpid = int(gpid_file.read_text(encoding="utf-8").strip())
        os.kill(gpid, 0)

        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=15)
        assert proc.returncode != 0, out + err

        alive = True
        try:
            os.kill(gpid, 0)
        except ProcessLookupError:
            alive = False
        assert not alive, f"manual_build grandchild PID {gpid} survived SIGTERM"
    finally:
        go_file.write_text("go\n")
        if proc.poll() is None:
            proc.kill()
        if gpid > 0:
            try:
                os.kill(gpid, signal.SIGKILL)
            except OSError:
                pass

    assert os.readlink(rel_dir / "current") == "v1-initial"
    assert [p.name for p in rel_dir.iterdir() if p.is_dir() and not p.is_symlink()] == ["v1-initial"]
    assert not _is_lock_held(lock_file)


def test_normal_deploy_refuses_legacy_dist_edge_switch_and_rollback_edge_ready_matches_active_caddyfile(
    tmp_path: Path,
) -> None:
    # Case A: Normal deploy detects running edge still mounts web/dist->/srv while target uses /srv-releases
    # -> refuses to recreate edge and instructs operator to use --first-releases-rollout.
    case_a = tmp_path / "refuse_legacy_normal"
    case_a.mkdir()
    ctx_a = _setup_strict_deploy_env(case_a, seed_v1_release=True)
    repo_a = Path(ctx_a["repo"])
    fake_docker_a = Path(ctx_a["fake_bin"]) / "docker"
    docker_text_a = fake_docker_a.read_text().replace(
        '  inspect)\n    echo "backend mem_limit=6442450944"\n    exit 0\n    ;;',
        f'  inspect)\n'
        f'    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi\n'
        f'    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo ""; exit 0; fi\n'
        f'    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then echo "bind|{repo_a.resolve()}/web/dist->/srv"; exit 0; fi\n'
        f'    echo "backend mem_limit=6442450944"\n'
        f'    exit 0\n'
        f'    ;;',
        1,
    )
    fake_docker_a.write_text(docker_text_a)
    fake_docker_a.chmod(0o755)

    res_a = _run_via_wrapper(ctx_a, case_a)
    assert res_a.returncode != 0
    assert "--first-releases-rollout" in res_a.stderr
    assert "--force-recreate edge" not in Path(ctx_a["docker_trace"]).read_text()
    assert not _is_lock_held(Path(ctx_a["lock_file"]))

    # Case B: edge_ready adapts expected body to the active Caddyfile (old "ok" vs new "dalton-edge-ok releases-v1")
    case_b = tmp_path / "caddyfile_aware_ready"
    repo_b = case_b / "repo"
    (repo_b / "deploy/dalton").mkdir(parents=True)
    fake_bin_b = case_b / "bin"
    fake_bin_b.mkdir()
    (fake_bin_b / "python3").symlink_to(sys.executable)

    for caddy_body, resp_body, expect_ok in (
        (':8088 {\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n', "ok", True),
        (':8088 {\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n', "<!doctype html><div id=\"root\">spa</div>", False),
        (':8088 {\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n', "ok", False),
        (':8088 {\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n', "dalton-edge-ok releases-v1", True),
    ):
        (repo_b / "deploy/dalton/Caddyfile").write_text(caddy_body)
        (fake_bin_b / "curl").write_text(
            f"""#!/bin/bash
set -euo pipefail
if [[ "$*" == *"http://127.0.0.1:8088/__edge_health"* ]]; then
  printf '%s\\n200' '{resp_body}'
  exit 0
fi
exit 99
"""
        )
        (fake_bin_b / "curl").chmod(0o755)
        res_b = subprocess.run(
            [
                "bash",
                "-c",
                f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1; DALTON_REPO_ROOT="{repo_b}"; DALTON_EDGE_PORT=8088; source "{_DEPLOY_SCRIPT}"; edge_ready 2',
            ],
            env={"PATH": f"{fake_bin_b}:/usr/bin:/bin", "HOME": str(case_b)},
            capture_output=True,
            text=True,
            check=False,
        )
        assert (res_b.returncode == 0) is expect_ok, f"resp={resp_body!r} rc={res_b.returncode}"


def test_first_rollout_uses_explicit_compose_files_and_project_name_without_compose_file_env(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "deploy/dalton").mkdir(parents=True)
    (repo / "scripts/dalton/ops/prod").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)

    (repo / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
    (repo / "docker-compose.dalton.yml").write_text(
        "services:\n  edge:\n    volumes:\n      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n      - ./web/dist:/srv:ro\n"
    )
    (repo / "deploy/dalton/Caddyfile").write_text(':8088 {\n\troot * /srv\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n')
    shutil.copy2(_DEPLOY_SCRIPT, repo / "scripts/dalton/ops/prod/deploy_main.sh")
    shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1"], check=True)

    (repo / "docker-compose.dalton.yml").write_text(
        "services:\n  edge:\n    volumes:\n      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n      - ./web/releases:/srv-releases:ro\n"
    )
    (repo / "deploy/dalton/Caddyfile").write_text(
        ':8088 {\n\troot * /srv-releases/current\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n'
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v2"], check=True)
    _write_release_bundle(repo / "web/dist", tag="v1", entry_chunk="index-v1aaaa11.js")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_trace = tmp_path / "docker.trace"
    # Strict fake docker: rejects ANY `docker compose` call missing `-p dalton-explicit-proj` or `-f ...docker-compose.dalton.yml`!
    (fake_bin / "docker").write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "docker:$*" >> "{docker_trace}"
case "${{1:-}}" in
  inspect)
    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-explicit-proj"; exit 0; fi
    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then echo "bind|{repo.resolve()}/web/dist->/srv"; exit 0; fi
    ;;
  ps) echo "edge-cid-1"; exit 0 ;;
  logs) echo "log"; exit 0 ;;
  run)
    if [[ "$*" == *"--entrypoint caddy"* ]] && [[ "$*" == *"validate --config /etc/caddy/Caddyfile --adapter caddyfile"* ]]; then
      exit 0
    fi
    ;;
  compose)
    if [[ "$*" != *"-p dalton-explicit-proj"* ]] || [[ "$*" != *"docker-compose.dalton.yml"* ]]; then
      echo "MISSING_EXPLICIT_COMPOSE_ARGS:$*" >&2
      exit 99
    fi
    if [[ "$*" == *" config"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        rb_cf=""
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ] && [[ "$a" == *"docker-compose.dalton.yml" ]]; then rb_cf="$a"; fi
          prev="$a"
        done
        cat "$rb_cf"
      else
        cat "{repo}/docker-compose.dalton.yml"
      fi
      exit 0
    fi
    if [[ "$*" == *"up -d --no-deps --force-recreate edge"* ]]; then
      exit 0
    fi
    ;;
esac
echo "UNEXPECTED_DOCKER:$*" >&2
exit 99
"""
    )
    (fake_bin / "curl").write_text(
        """#!/bin/bash
set -euo pipefail
if [[ "$*" == *":8088/__edge_health"* ]]; then
  printf 'dalton-edge-ok releases-v1\\n200'
  exit 0
fi
exit 99
"""
    )
    (fake_bin / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for name in ("docker", "curl", "sleep"):
        (fake_bin / name).chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)

    stub_dir = _install_edge_http_stub(tmp_path)
    res = subprocess.run(
        ["bash", str(repo / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(repo),
            "DALTON_DEPLOY_LOCK_FILE": str(tmp_path / "locks/deploy.lock"),
            "DALTON_EDGE_PORT": "8088",
            "PYTHONPATH": str(stub_dir),
            "TEST_EDGE_SERVE_ROOT": str(repo / "web/releases/current"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    compose_lines = [line for line in docker_trace.read_text().splitlines() if line.startswith("docker:compose ")]
    assert compose_lines
    assert all("-p dalton-explicit-proj" in line and "docker-compose.dalton.yml" in line for line in compose_lines)


def test_carry_over_includes_pagefind_hashed_files_and_legacy_r2_manifest_help_assets(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rel_dir = repo / "web/releases"
    rel_dir.mkdir(parents=True)
    r1 = rel_dir / "rel-r1"
    r2 = rel_dir / "rel-r2"
    r3 = rel_dir / "rel-r3"
    _write_release_bundle(
        r1,
        tag="v1",
        entry_chunk="index-v1aaaa11.js",
        extra_assets={"lazy-v1bbbb22.js": "v1-lazy\n"},
        help_astro_assets={"legacy-doc.Ab12Cd34.js": "v1-astro\n"},
    )
    # Seed Pagefind content-addressed files + unhashed pagefind-entry.json in r1
    (r1 / "help/pagefind/index").mkdir(parents=True)
    (r1 / "help/pagefind/fragment").mkdir(parents=True)
    (r1 / "help/pagefind/filter").mkdir(parents=True)
    (r1 / "help/pagefind/pagefind-entry.json").write_text('{"version":"v1"}\n')
    (r1 / "help/pagefind/pagefind.zh_1111aaaa.pf_meta").write_bytes(b"meta-v1")
    (r1 / "help/pagefind/index/zh_1111bbbb.pf_index").write_bytes(b"index-v1")
    (r1 / "help/pagefind/fragment/zh_1111cccc.pf_fragment").write_bytes(b"frag-v1")
    (r1 / "help/pagefind/filter/tag_1111dddd.pf_filter").write_bytes(b"filter-v1")
    # Simulate a legacy r2 manifest (bare filenames, no `# format=v2`, no help/ lines!)
    (r1 / ".deploy-own-assets").write_text("index-v1aaaa11.js\nlazy-v1bbbb22.js\n")

    _write_release_bundle(
        r2,
        tag="v2",
        entry_chunk="index-v2cccc33.js",
        help_astro_assets={"new-doc.Ef56Gh78.js": "v2-astro\n"},
    )
    (r2 / "help/pagefind/fragment").mkdir(parents=True)
    (r2 / "help/pagefind/pagefind-entry.json").write_text('{"version":"v2"}\n')
    (r2 / "help/pagefind/fragment/zh_2222eeee.pf_fragment").write_bytes(b"frag-v2")

    _write_release_bundle(r3, tag="v3", entry_chunk="index-v3eeee55.js")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    env = {"PATH": f"{fake_bin}:/bin:/usr/bin", "HOME": str(tmp_path), "DALTON_REPO_ROOT": str(repo)}

    script = f"""
set -euo pipefail
DALTON_DEPLOY_LIB_ONLY=1 source "{_DEPLOY_SCRIPT}"
_record_own_frontend_assets "{r2}"
_carry_over_hashed_assets "{r1}" "{r2}"
_record_own_frontend_assets "{r3}"
_carry_over_hashed_assets "{r2}" "{r3}"
"""
    res = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stdout + res.stderr

    # r2 keeps its own pagefind-entry.json (never overwritten by r1!)
    assert json.loads((r2 / "help/pagefind/pagefind-entry.json").read_text())["version"] == "v2"
    # Legacy r2 manifest in r1 still had its help/_astro and Pagefind hashed files carried into r2!
    assert (r2 / "help/_astro/legacy-doc.Ab12Cd34.js").is_file()
    assert (r2 / "help/pagefind/pagefind.zh_1111aaaa.pf_meta").read_bytes() == b"meta-v1"
    assert (r2 / "help/pagefind/index/zh_1111bbbb.pf_index").read_bytes() == b"index-v1"
    assert (r2 / "help/pagefind/fragment/zh_1111cccc.pf_fragment").read_bytes() == b"frag-v1"
    assert (r2 / "help/pagefind/filter/tag_1111dddd.pf_filter").read_bytes() == b"filter-v1"
    # r3 carries r2's own Pagefind hashed fragment without unboundedly carrying r1's!
    assert (r3 / "help/pagefind/fragment/zh_2222eeee.pf_fragment").read_bytes() == b"frag-v2"
    assert not (r3 / "help/pagefind/fragment/zh_1111cccc.pf_fragment").exists()


def test_carry_over_failure_aborts_manual_switch_and_preserves_new_release_on_auto_rollback(
    tmp_path: Path,
) -> None:
    # Case A: manual_switch_frontend_release aborts without switching when target assets/ is unwritable
    c1 = tmp_path / "manual_fail"
    repo1 = c1 / "repo"
    rel1 = repo1 / "web/releases"
    rel1.mkdir(parents=True)
    _write_release_bundle(rel1 / "rel-old", tag="old", entry_chunk="index-old11111.js")
    _write_release_bundle(rel1 / "rel-cur", tag="cur", entry_chunk="index-cur22222.js")
    os.symlink("rel-cur", rel1 / "current")
    fake_bin1 = c1 / "bin"
    fake_bin1.mkdir()
    (fake_bin1 / "python3").symlink_to(sys.executable)

    old_assets = rel1 / "rel-old/assets"
    old_assets.chmod(0o555)
    try:
        res1 = subprocess.run(
            [
                "bash",
                "-c",
                f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1 source "{_DEPLOY_SCRIPT}"; manual_switch_frontend_release "rel-old"',
            ],
            env={
                "PATH": f"{fake_bin1}:/bin:/usr/bin",
                "HOME": str(c1),
                "DALTON_REPO_ROOT": str(repo1),
                "DALTON_DEPLOY_LOCK_FILE": str(c1 / "locks/deploy.lock"),
            },
            capture_output=True,
            text=True,
            check=False,
        )
        assert res1.returncode != 0
        assert os.readlink(rel1 / "current") == "rel-cur"
        assert not _is_lock_held(c1 / "locks/deploy.lock")
    finally:
        old_assets.chmod(0o755)

    # Case B: Auto-rollback still restores current -> v1-initial, reports asset carry-over failure, and preserves new release dir
    c2 = tmp_path / "rollback_carry_fail"
    c2.mkdir()
    ctx2 = _setup_strict_deploy_env(c2, seed_v1_release=True, public_edge_fail=True)
    repo2 = Path(ctx2["repo"])
    seed2 = Path(ctx2["seed"])
    (seed2 / "app.py").write_text("v2\n")
    _commit_and_push_seed(seed2, "v2 rollback carry fail")

    v1_assets = repo2 / "web/releases/v1-initial/assets"
    v1_assets.chmod(0o555)
    try:
        res2 = _run_via_wrapper(ctx2, c2)
        assert res2.returncode != 0
        rel2 = repo2 / "web/releases"
        assert os.readlink(rel2 / "current") == "v1-initial"
        assert "frontend_asset_carryover" in res2.stdout or "failed to carry over" in res2.stderr + res2.stdout
        # Both v1-initial AND the unverified new release directory are preserved!
        dirs2 = [p.name for p in rel2.iterdir() if p.is_dir() and not p.is_symlink()]
        assert "v1-initial" in dirs2 and len(dirs2) == 2, f"expected new release preserved on carry-over failure, got {dirs2}"
    finally:
        v1_assets.chmod(0o755)


# ---------------------------------------------------------------------------
# 8. r5: Reproducing tests for Codex + Grok r4 review findings
# ---------------------------------------------------------------------------


def test_first_rollout_rollback_ignores_second_signal_and_keeps_lock_on_rollback_failure(
    tmp_path: Path,
) -> None:
    def _setup_rollout(
        case_dir: Path,
        *,
        rollback_barrier: tuple[Path, Path] | None = None,
        fail_rollback_up: bool = False,
    ) -> dict[str, Path | dict[str, str]]:
        repo = case_dir / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "deploy/dalton").mkdir(parents=True)
        (repo / "scripts/dalton/ops/prod").mkdir(parents=True)
        (repo / "web").mkdir(parents=True)

        (repo / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n"
            "  edge:\n"
            "    image: caddy:2.9.1-alpine\n"
            "    volumes:\n"
            "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
            "      - ./web/dist:/srv:ro\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ':8088 {\n\troot * /srv\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n'
        )
        shutil.copy2(_DEPLOY_SCRIPT, repo / "scripts/dalton/ops/prod/deploy_main.sh")
        shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1 legacy"], check=True)

        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n"
            "  edge:\n"
            "    image: caddy:2.9.1-alpine\n"
            "    volumes:\n"
            "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
            "      - ./web/releases:/srv-releases:ro\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ':8088 {\n\troot * /srv-releases/current\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n'
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v2 releases"], check=True)
        _write_release_bundle(repo / "web/dist", tag="legacy-prod", entry_chunk="index-old11111.js")

        fake_bin = case_dir / "bin"
        fake_bin.mkdir()
        curl_trace = case_dir / "curl.trace"
        edge_state = case_dir / "edge.state"
        edge_state.write_text("legacy")
        rb_barrier_code = ""
        if rollback_barrier is not None:
            rb_ready, rb_go = rollback_barrier
            rb_barrier_code = f"""
touch "{rb_ready}"
while [ ! -f "{rb_go}" ]; do
  "{sys.executable}" -c 'import time; time.sleep(0.02)'
done
"""
        (fake_bin / "docker").write_text(
            f"""#!/bin/bash
set -euo pipefail
case "${{1:-}}" in
  inspect)
    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-proj"; exit 0; fi
    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then echo "bind|{repo.resolve()}/web/dist->/srv"; exit 0; fi
    ;;
  ps) echo "edge-cid-1"; exit 0 ;;
  logs) echo "log"; exit 0 ;;
  run)
    if [[ "$*" == *"--entrypoint caddy"* ]] && [[ "$*" == *"validate --config /etc/caddy/Caddyfile --adapter caddyfile"* ]]; then
      exit 0
    fi
    ;;
  compose)
    if [[ "$*" == *" config"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        rb_cf=""
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ] && [[ "$a" == *"docker-compose.dalton.yml" ]]; then rb_cf="$a"; fi
          prev="$a"
        done
        cat "$rb_cf"
      else
        cat "{repo}/docker-compose.dalton.yml"
      fi
      exit 0
    fi
    if [[ "$*" == *"up -d --no-deps --force-recreate edge"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        {rb_barrier_code}
        if [ "{1 if fail_rollback_up else 0}" = "1" ]; then
          echo "ERROR: simulated rollback compose up failure" >&2
          exit 1
        fi
        echo "rolled_back" > "{edge_state}"
        exit 0
      else
        echo "new_mount" > "{edge_state}"
        exit 0
      fi
    fi
    ;;
esac
echo "UNEXPECTED_DOCKER:$*" >&2
exit 99
"""
        )
        (fake_bin / "curl").write_text(
            f"""#!/bin/bash
set -euo pipefail
echo "curl:$*:state=$(cat "{edge_state}")" >> "{curl_trace}"
if [[ "$*" == *":8088/__edge_health"* ]]; then
  if [ "$(cat "{edge_state}")" = "rolled_back" ]; then
    printf 'ok\\n200'
  else
    printf 'dalton-edge-ok releases-v1\\n200'
  fi
  exit 0
fi
if [[ "$*" == *"http://127.0.0.1:8088/"* ]]; then
  printf '<!doctype html><div id="root">legacy</div>\\n200'
  exit 0
fi
exit 99
"""
        )
        (fake_bin / "sleep").write_text("#!/bin/bash\nexit 0\n")
        for name in ("docker", "curl", "sleep"):
            (fake_bin / name).chmod(0o755)
        (fake_bin / "python3").symlink_to(sys.executable)

        stub_dir = _install_edge_http_stub(case_dir)
        lock_file = case_dir / "locks/deploy.lock"
        env = {
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(case_dir),
            "DALTON_REPO_ROOT": str(repo),
            "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
            "DALTON_EDGE_PORT": "8088",
            "PYTHONPATH": str(stub_dir),
            "TEST_EDGE_SERVE_ROOT": str(repo / "web/releases/current"),
            # Force check_public_edge.sh to fail so rollback triggers deterministically
            "TEST_EDGE_OVERRIDE_BUILD": "index-mismatch999.js",
        }
        return {
            "repo": repo,
            "curl_trace": curl_trace,
            "edge_state": edge_state,
            "lock_file": lock_file,
            "env": env,
        }

    # Case A: SIGTERM arrives WHILE rollback is already running -> ignored until old-site HTTP verification completes!
    c_a = tmp_path / "signal_during_rollback"
    c_a.mkdir()
    rb_ready = c_a / "rb.ready"
    rb_go = c_a / "rb.go"
    ctx_a = _setup_rollout(c_a, rollback_barrier=(rb_ready, rb_go))
    proc = subprocess.Popen(
        ["bash", str(Path(ctx_a["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=ctx_a["env"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not rb_ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert rb_ready.exists(), "rollback barrier never reached"
        # Send TWO signals while rollback compose up is running!
        proc.send_signal(signal.SIGTERM)
        time.sleep(0.1)
        proc.send_signal(signal.SIGINT)
        time.sleep(0.1)
        assert proc.poll() is None, "rollback exited prematurely on signal during rollback!"
        rb_go.write_text("go\n")
        out_a, err_a = proc.communicate(timeout=15)
    finally:
        rb_go.write_text("go\n")
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode in (130, 143), f"expected signal exit code after rollback, got {proc.returncode}: {out_a}\n{err_a}"
    assert Path(ctx_a["edge_state"]).read_text().strip() == "rolled_back"
    curl_lines = Path(ctx_a["curl_trace"]).read_text()
    assert "http://127.0.0.1:8088/" in curl_lines and "state=rolled_back" in curl_lines, curl_lines
    assert not _is_lock_held(Path(ctx_a["lock_file"]))

    # Case B: Rollback itself fails -> keeps persistent failure lock + snapshot diagnostics + manual steps, blocks next deploy
    c_b = tmp_path / "rollback_failed"
    c_b.mkdir()
    ctx_b = _setup_rollout(c_b, fail_rollback_up=True)
    res_b = subprocess.run(
        ["bash", str(Path(ctx_b["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=ctx_b["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_b.returncode != 0
    fail_lock = Path(str(ctx_b["lock_file"]) + ".rollback-failed")
    assert fail_lock.is_file(), f"expected failure lock sentinel {fail_lock} to be preserved:\n{res_b.stdout}\n{res_b.stderr}"
    rb_attempts = sorted(p for p in (Path(ctx_b["repo"]) / "web/releases/.first-rollout-rollback").glob("attempt-*") if p.is_dir())
    assert len(rb_attempts) == 1, rb_attempts
    assert (rb_attempts[0] / "Caddyfile").is_file()
    assert (rb_attempts[0] / "resolved-compose.yml").is_file()
    assert f"{rb_attempts[0].name}/resolved-compose.yml" in res_b.stdout + res_b.stderr
    assert "-p dalton-proj" in res_b.stdout + res_b.stderr
    assert "人工" in res_b.stdout + res_b.stderr or "manual" in (res_b.stdout + res_b.stderr).lower()

    # Next deploy / rollout attempt is blocked until operator clears the failure lock sentinel
    res_next = subprocess.run(
        ["bash", str(Path(ctx_b["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=ctx_b["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_next.returncode != 0
    assert "rollback-failed" in res_next.stderr


def test_normal_deploy_transitional_refusal_keeps_ff_so_first_rollout_works_immediately(
    tmp_path: Path,
) -> None:
    ctx = _setup_strict_deploy_env(tmp_path, seed_v1_release=False, seed_legacy_dist=True)
    seed = Path(ctx["seed"])
    repo = Path(ctx["repo"])

    # v1 in seed had no compose/Caddyfile; add legacy v1 -> push -> pull -> add releases v2 -> push
    (seed / "deploy/dalton").mkdir(parents=True, exist_ok=True)
    (seed / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
    (seed / "docker-compose.dalton.yml").write_text(
        "services:\n  edge:\n    volumes:\n      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n      - ./web/dist:/srv:ro\n"
    )
    (seed / "deploy/dalton/Caddyfile").write_text(':8088 {\n\troot * /srv\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n')
    v1_legacy = _commit_and_push_seed(seed, "v1 legacy compose")
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "--ff-only", v1_legacy], check=True)

    (seed / "docker-compose.dalton.yml").write_text(
        "services:\n  edge:\n    volumes:\n      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n      - ./web/releases:/srv-releases:ro\n"
    )
    (seed / "deploy/dalton/Caddyfile").write_text(
        ':8088 {\n\troot * /srv-releases/current\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n'
    )
    v2_releases = _commit_and_push_seed(seed, "v2 releases compose")

    fake_docker = Path(ctx["fake_bin"]) / "docker"
    docker_text = fake_docker.read_text().replace(
        '  inspect)\n    echo "backend mem_limit=6442450944"\n    exit 0\n    ;;',
        f'  inspect)\n'
        f'    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi\n'
        f'    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-proj"; exit 0; fi\n'
        f'    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then echo "bind|{repo.resolve()}/web/dist->/srv"; exit 0; fi\n'
        f'    echo "backend mem_limit=6442450944"\n'
        f'    exit 0\n'
        f'    ;;',
        1,
    ).replace(
        '    elif [ "${2:-}" = "config" ] || [[ "$*" == *" config"* ]]; then\n'
        '      echo "services:"\n'
        '      echo "  edge:"\n'
        '      echo "    volumes:"\n'
        '      echo "      - ./web/releases:/srv-releases:ro"\n'
        '      exit 0',
        f'    elif [ "${{2:-}}" = "config" ] || [[ "$*" == *" config"* ]]; then\n'
        f'      if [[ "$*" == *".first-rollout-rollback"* ]]; then\n'
        f'        rb_cf=""; prev=""\n'
        f'        for a in "$@"; do\n'
        f'          if [ "$prev" = "-f" ] && [[ "$a" == *"docker-compose.dalton.yml" ]]; then rb_cf="$a"; fi\n'
        f'          prev="$a"\n'
        f'        done\n'
        f'        cat "$rb_cf"\n'
        f'      else\n'
        f'        cat "{repo}/docker-compose.dalton.yml"\n'
        f'      fi\n'
        f'      exit 0',
        1,
    ).replace(
        '    elif [ "${2:-}" = "up" ]; then',
        '    elif [ "${2:-}" = "up" ] || [[ "$*" == *" up "* ]]; then',
        1,
    ).replace(
        '  compose)',
        '  run)\n'
        '    if [[ "$*" == *"--entrypoint caddy"* ]] && [[ "$*" == *"validate --config /etc/caddy/Caddyfile --adapter caddyfile"* ]]; then\n'
        '      exit 0\n'
        '    fi\n'
        '    ;;\n'
        '  compose)',
        1,
    )
    fake_docker.write_text(docker_text)
    fake_docker.chmod(0o755)

    # 1. Normal deploy refuses switching edge from legacy /srv BEFORE fast-forward -> exits code 3
    # leaving HEAD, Compose, Caddyfile, and working tree untouched at v1_legacy!
    res = _run_via_wrapper(ctx, tmp_path)
    assert res.returncode == 3, f"expected exit 3, got {res.returncode}: {res.stdout}\n{res.stderr}"
    assert "--first-releases-rollout" in res.stderr
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == v1_legacy
    assert "./web/dist:/srv:ro" in (repo / "docker-compose.dalton.yml").read_text(encoding="utf-8")
    assert "root * /srv" in (repo / "deploy/dalton/Caddyfile").read_text(encoding="utf-8")
    assert not _is_lock_held(Path(ctx["lock_file"]))

    # 2. Fast-forwarding explicitly to v2_releases and running --first-releases-rollout works immediately!
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "--ff-only", v2_releases], check=True)
    _, env2 = _wrapper_env_and_cmd(ctx, tmp_path, wrapper_name="wrapper-rollout")
    res_rollout = subprocess.run(
        ["bash", str(repo / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=env2,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_rollout.returncode == 0, res_rollout.stdout + res_rollout.stderr
    assert "first-releases-rollout completed" in res_rollout.stdout


def test_first_rollout_rollback_rewrites_long_syntax_caddyfile_and_freezes_config(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "deploy/dalton").mkdir(parents=True)
    (repo / "scripts/dalton/ops/prod").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)

    # Commit v1 uses LONG SYNTAX volumes for Caddyfile and web/dist!
    (repo / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
    (repo / "docker-compose.dalton.yml").write_text(
        "services:\n"
        "  edge:\n"
        "    image: caddy:2.9.1-alpine\n"
        "    volumes:\n"
        "      - type: bind\n"
        "        source: ./deploy/dalton/Caddyfile\n"
        "        target: /etc/caddy/Caddyfile\n"
        "        read_only: true\n"
        "      - type: bind\n"
        "        source: ./web/dist\n"
        "        target: /srv\n"
        "        read_only: true\n"
    )
    (repo / "deploy/dalton/Caddyfile").write_text(':8088 {\n\troot * /srv\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n')
    shutil.copy2(_DEPLOY_SCRIPT, repo / "scripts/dalton/ops/prod/deploy_main.sh")
    shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1 long syntax"], check=True)
    v1 = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    (repo / "docker-compose.dalton.yml").write_text(
        "services:\n"
        "  edge:\n"
        "    volumes:\n"
        "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
        "      - ./web/releases:/srv-releases:ro\n"
    )
    (repo / "deploy/dalton/Caddyfile").write_text(
        ':8088 {\n\troot * /srv-releases/current\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n'
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v2 releases"], check=True)
    _write_release_bundle(repo / "web/dist", tag="v1", entry_chunk="index-v1.js")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "docker").write_text(
        f"""#!/bin/bash
set -euo pipefail
case "${{1:-}}" in
  inspect)
    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-long-proj"; exit 0; fi
    echo "bind|{repo.resolve()}/web/dist->/srv"
    exit 0
    ;;
  run)
    if [[ "$*" == *"--entrypoint caddy"* ]] && [[ "$*" == *"validate --config /etc/caddy/Caddyfile --adapter caddyfile"* ]]; then
      exit 0
    fi
    ;;
  compose)
    if [[ "$*" == *" config"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        rb_cf=""
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ] && [[ "$a" == *"docker-compose.dalton.yml" ]]; then rb_cf="$a"; fi
          prev="$a"
        done
        cat "$rb_cf"
      else
        cat "{repo}/docker-compose.dalton.yml"
      fi
      exit 0
    fi
    ;;
esac
exit 99
"""
    )
    (fake_bin / "docker").chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)

    res = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1 source "{repo}/scripts/dalton/ops/prod/deploy_main.sh"; '
            f'cd "{repo}"; _prepare_first_rollout_rollback "{v1}"',
        ],
        env={"PATH": f"{fake_bin}:/bin:/usr/bin", "HOME": str(tmp_path), "DALTON_REPO_ROOT": str(repo)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
    rb_attempts = sorted(p for p in (repo / "web/releases/.first-rollout-rollback").glob("attempt-*") if p.is_dir())
    assert len(rb_attempts) == 1, rb_attempts
    rb_dir = rb_attempts[0]
    rb_compose = (rb_dir / "docker-compose.dalton.yml").read_text(encoding="utf-8")
    assert f".first-rollout-rollback/{rb_dir.name}/Caddyfile" in rb_compose
    assert "./deploy/dalton/Caddyfile" not in rb_compose
    assert (rb_dir / "resolved-compose.yml").is_file()
    compose_args_txt = (rb_dir / "compose.args").read_text(encoding="utf-8")
    assert "resolved-compose.yml" in compose_args_txt
    assert "dalton-long-proj" in compose_args_txt


def test_mount_drift_and_pre_recreate_gate_follow_resolved_compose_override_and_abs_compose_file(
    tmp_path: Path,
) -> None:
    # Case 1: Unsupported override (external releases dir, tmpfs, or external Caddyfile) is REJECTED
    # at pre-recreate precheck with clear reason and supported layout; edge is never recreated.
    for label, override_yaml in (
        (
            "external_dir",
            f"services:\n  edge:\n    volumes:\n      - {tmp_path}/external-releases:/srv-releases:ro\n",
        ),
        (
            "tmpfs_root",
            "services:\n  edge:\n    volumes:\n      - type: tmpfs\n        target: /srv-releases\n",
        ),
        (
            "external_caddyfile",
            "services:\n  edge:\n    volumes:\n"
            "      - ./web/releases:/srv-releases:ro\n"
            f"      - {tmp_path}/external.Caddyfile:/etc/caddy/Caddyfile:ro\n",
        ),
    ):
        case_dir = tmp_path / label
        case_dir.mkdir()
        ctx = _setup_strict_deploy_env(case_dir, seed_v1_release=True)
        repo = Path(ctx["repo"])
        seed = Path(ctx["seed"])
        override_yml = case_dir / "override.edge.yml"
        override_yml.write_text(override_yaml)
        (seed / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
        (seed / "docker-compose.dalton.yml").write_text(
            "services:\n  edge:\n    volumes:\n      - ./web/releases:/srv-releases:ro\n"
        )
        v2 = _commit_and_push_seed(seed, f"v2 compose {label}")
        subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
        subprocess.run(["git", "-C", str(repo), "merge", "-q", "--ff-only", v2], check=True)

        compose_file_env = f"{repo}/docker-compose.yml:{repo}/docker-compose.dalton.yml:{override_yml}"
        fake_docker = Path(ctx["fake_bin"]) / "docker"
        docker_text = fake_docker.read_text().replace(
            '  inspect)\n    echo "backend mem_limit=6442450944"\n    exit 0\n    ;;',
            f'  inspect)\n'
            f'    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi\n'
            f'    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-override-proj"; exit 0; fi\n'
            f'    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then echo "bind|{repo.resolve()}/web/releases->/srv-releases"; exit 0; fi\n'
            f'    echo "backend mem_limit=6442450944"\n'
            f'    exit 0\n'
            f'    ;;',
            1,
        ).replace(
            '    elif [ "${2:-}" = "config" ] || [[ "$*" == *" config"* ]]; then\n'
            '      echo "services:"\n'
            '      echo "  edge:"\n'
            '      echo "    volumes:"\n'
            '      echo "      - ./web/releases:/srv-releases:ro"\n'
            '      exit 0',
            f'    elif [ "${{2:-}}" = "config" ] || [[ "$*" == *" config"* ]]; then\n'
            f'      if [[ "$*" != *"-f {override_yml}"* ]]; then\n'
            f'        echo "CORRUPTED_ABS_COMPOSE_FILE:$*" >&2\n'
            f'        exit 99\n'
            f'      fi\n'
            f'      cat "{override_yml}"\n'
            f'      exit 0',
            1,
        )
        fake_docker.write_text(docker_text)
        fake_docker.chmod(0o755)

        res = _run_via_wrapper(ctx, case_dir, extra_env={"COMPOSE_FILE": compose_file_env})
        assert res.returncode != 0, f"{label} unexpectedly succeeded"
        assert "CORRUPTED_ABS_COMPOSE_FILE" not in res.stderr
        assert "unsupported edge" in res.stderr.lower(), f"{label}: {res.stderr}"
        assert "--force-recreate edge" not in Path(ctx["docker_trace"]).read_text()

    # Case 2: Explicit absolute COMPOSE_FILE paths pointing to STANDARD repo mounts (long syntax!)
    # + live edge mounting a stale /etc/caddy/Caddyfile from another checkout -> accepted by precheck,
    # drift detected on Caddyfile mount, and edge recreated with intact absolute COMPOSE_FILE paths!
    case_ok = tmp_path / "std_abs_long_syntax"
    case_ok.mkdir()
    ctx_ok = _setup_strict_deploy_env(case_ok, seed_v1_release=True)
    repo_ok = Path(ctx_ok["repo"])
    seed_ok = Path(ctx_ok["seed"])
    (seed_ok / "deploy/dalton").mkdir(parents=True, exist_ok=True)
    (seed_ok / "deploy/dalton/Caddyfile").write_text(
        ':8088 {\n\troot * /srv-releases/current\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n'
    )
    override_std = case_ok / "override.std.yml"
    override_std.write_text(
        "services:\n"
        "  edge:\n"
        "    volumes:\n"
        "      - type: bind\n"
        f"        source: {repo_ok.resolve()}/web/releases\n"
        "        target: /srv-releases\n"
        "        read_only: true\n"
        "      - type: bind\n"
        f"        source: {repo_ok.resolve()}/deploy/dalton/Caddyfile\n"
        "        target: /etc/caddy/Caddyfile\n"
        "        read_only: true\n"
    )
    (seed_ok / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
    (seed_ok / "docker-compose.dalton.yml").write_text(
        "services:\n  edge:\n    volumes:\n      - ./web/releases:/srv-releases:ro\n"
    )
    v2_ok = _commit_and_push_seed(seed_ok, "v2 standard compose")
    subprocess.run(["git", "-C", str(repo_ok), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(repo_ok), "merge", "-q", "--ff-only", v2_ok], check=True)

    compose_file_ok = f"{repo_ok}/docker-compose.yml:{repo_ok}/docker-compose.dalton.yml:{override_std}"
    fake_docker_ok = Path(ctx_ok["fake_bin"]) / "docker"
    docker_text_ok = fake_docker_ok.read_text().replace(
        '  inspect)\n    echo "backend mem_limit=6442450944"\n    exit 0\n    ;;',
        f'  inspect)\n'
        f'    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi\n'
        f'    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-std-proj"; exit 0; fi\n'
        f'    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then\n'
        f'      printf "bind|{repo_ok.resolve()}/web/releases->/srv-releases\\nbind|/other/checkout/deploy/dalton/Caddyfile->/etc/caddy/Caddyfile\\n"\n'
        f'      exit 0\n'
        f'    fi\n'
        f'    echo "backend mem_limit=6442450944"\n'
        f'    exit 0\n'
        f'    ;;',
        1,
    ).replace(
        '    elif [ "${2:-}" = "config" ] || [[ "$*" == *" config"* ]]; then\n'
        '      echo "services:"\n'
        '      echo "  edge:"\n'
        '      echo "    volumes:"\n'
        '      echo "      - ./web/releases:/srv-releases:ro"\n'
        '      exit 0',
        f'    elif [ "${{2:-}}" = "config" ] || [[ "$*" == *" config"* ]]; then\n'
        f'      if [[ "$*" != *"-f {override_std}"* ]]; then\n'
        f'        echo "CORRUPTED_ABS_COMPOSE_FILE:$*" >&2\n'
        f'        exit 99\n'
        f'      fi\n'
        f'      cat "{override_std}"\n'
        f'      exit 0',
        1,
    ).replace(
        '    elif [ "${2:-}" = "up" ]; then',
        f'    elif [ "${{2:-}}" = "up" ] || [[ "$*" == *" up "* ]]; then\n'
        f'      if [[ "$*" == *"edge"* ]] && [[ "$*" != *"-f {override_std}"* ]]; then\n'
        f'        echo "CORRUPTED_ABS_COMPOSE_FILE_UP:$*" >&2\n'
        f'        exit 99\n'
        f'      fi',
        1,
    )
    fake_docker_ok.write_text(docker_text_ok)
    fake_docker_ok.chmod(0o755)

    res_ok = _run_via_wrapper(ctx_ok, case_ok, extra_env={"COMPOSE_FILE": compose_file_ok})
    assert res_ok.returncode == 0, res_ok.stdout + res_ok.stderr
    assert "--force-recreate edge" in Path(ctx_ok["docker_trace"]).read_text()


def test_direct_build_frontend_prod_out_dir_shares_deploy_lock(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts/dalton").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)
    (repo / "docs-site").mkdir(parents=True)
    shutil.copy2(_BUILD_FRONTEND_SCRIPT, repo / "scripts/dalton/build_frontend_prod.sh")
    (repo / "web/.env.dalton").write_text("VITE_BRAND_NAME=Everflow\nVITE_BRAND_MARK=everflow\n")
    (repo / ".env").write_text(
        "APP_ENV=dalton\nDALTON_AUTH_MODE=supabase\n"
        "SUPABASE_URL=https://example.supabase.co\nSUPABASE_PUBLISHABLE_KEY=pk_test_123\n"
    )

    ready_file = tmp_path / "direct_build.ready"
    go_file = tmp_path / "direct_build.go"
    pnpm_trace = tmp_path / "pnpm.trace"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "pnpm").write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "pnpm:$(pwd):$*" >> "{pnpm_trace}"
if [ "$(basename "$(pwd)")" = "docs-site" ]; then
  if [ "${{1:-}}" = "install" ]; then exit 0; fi
  mkdir -p dist
  echo "<h1>help</h1>" > dist/index.html
  exit 0
fi
if [ "${{1:-}}" = "build" ]; then
  if [ "${{TEST_USE_BARRIER:-0}}" = "1" ]; then
    touch "{ready_file}"
    while [ ! -f "{go_file}" ]; do
      "{sys.executable}" -c 'import time; time.sleep(0.02)'
    done
  fi
  out="${{DALTON_VITE_OUT_DIR:-dist}}"
  mkdir -p "${{out}}/assets"
  echo '<!doctype html><div id="root">ok</div>' > "${{out}}/index.html"
  echo '{{"build":"index-direct11.js"}}' > "${{out}}/version.json"
  echo 'console.log(1);' > "${{out}}/assets/index-direct11.js"
  exit 0
fi
exit 99
"""
    )
    (fake_bin / "pnpm").chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)

    lock_file = tmp_path / "locks/deploy.lock"
    lock_file.parent.mkdir(parents=True)
    base_env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
    }

    # 1. External deploy holds FD 9 lock -> direct build_frontend_prod.sh --out-dir MUST fail immediately!
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res_blocked = subprocess.run(
            ["bash", str(repo / "scripts/dalton/build_frontend_prod.sh"), "--require-supabase", "--out-dir", "web/releases/direct-1"],
            cwd=repo,
            env=base_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert res_blocked.returncode != 0
        assert "lock" in res_blocked.stderr.lower()
        assert not pnpm_trace.exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    # 2. Direct build_frontend_prod.sh --out-dir holds lock while running -> second build & prune are blocked!
    proc = subprocess.Popen(
        ["bash", str(repo / "scripts/dalton/build_frontend_prod.sh"), "--require-supabase", "--out-dir", "web/releases/direct-2"],
        cwd=repo,
        env={**base_env, "TEST_USE_BARRIER": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while not ready_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_file.exists(), "direct build barrier not reached"

        res_second = subprocess.run(
            ["bash", str(repo / "scripts/dalton/build_frontend_prod.sh"), "--require-supabase", "--out-dir", "web/releases/direct-3"],
            cwd=repo,
            env=base_env,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert res_second.returncode != 0
        assert "lock" in res_second.stderr.lower()

        res_prune = subprocess.run(
            ["bash", "-c", f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1 source "{_DEPLOY_SCRIPT}"; prune_old_frontend_releases 2'],
            env={**base_env, "DALTON_REPO_ROOT": str(repo)},
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert res_prune.returncode != 0
        assert "another deploy is already running" in res_prune.stderr

        go_file.write_text("go\n")
        out_p, err_p = proc.communicate(timeout=15)
        assert proc.returncode == 0, out_p + err_p
    finally:
        go_file.write_text("go\n")
        if proc.poll() is None:
            proc.kill()

    assert not _is_lock_held(lock_file)


def test_carry_over_copy_fallback_uses_atomic_temp_file_and_cleans_up_on_copy_error(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rel_dir = repo / "web/releases"
    r1 = rel_dir / "rel-v1"
    r2 = rel_dir / "rel-v2"
    _write_release_bundle(
        r1,
        tag="v1",
        entry_chunk="index-v1aaaa11.js",
        extra_assets={"lazy-v1bbbb22.js": "console.log('full-content-1234567890');\n"},
    )
    _write_release_bundle(r2, tag="v2", entry_chunk="index-v2cccc33.js")

    # Inject a sitecustomize stub that forces os.link -> EXDEV and makes shutil.copy2 write
    # truncated bytes to its target path before raising ENOSPC.
    stub_dir = tmp_path / "enospc_stub"
    stub_dir.mkdir()
    (stub_dir / "sitecustomize.py").write_text(
        """import errno, os, shutil
from pathlib import Path

def _fail_link(src, dst):
    raise OSError(errno.EXDEV, "Invalid cross-device link")

def _fail_copy2(src, dst, *args, **kwargs):
    Path(dst).write_bytes(b"TRUNCATED")
    raise OSError(errno.ENOSPC, "No space left on device")

os.link = _fail_link
shutil.copy2 = _fail_copy2
""",
        encoding="utf-8",
    )

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    res = subprocess.run(
        [
            "bash",
            "-c",
            f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1 source "{_DEPLOY_SCRIPT}"; _carry_over_hashed_assets "{r1}" "{r2}"',
        ],
        env={
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(tmp_path),
            "DALTON_REPO_ROOT": str(repo),
            "PYTHONPATH": str(stub_dir),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    # Final target files MUST NOT exist as truncated partial files, and no temp file left behind!
    assert not (r2 / "assets/index-v1aaaa11.js").exists()
    assert not (r2 / "assets/lazy-v1bbbb22.js").exists()
    assert not list((r2 / "assets").glob("*.tmp.*"))


# ---------------------------------------------------------------------------
# 9. r6: Narrow fixes for Codex r5 review findings under production mount scope
# ---------------------------------------------------------------------------


def test_r6_first_rollout_long_syntax_pre_ff_and_caddy_validate_failure_aborts_before_edge(
    tmp_path: Path,
) -> None:
    def _build_case(case_dir: Path, *, caddy_validate_fail: bool) -> dict[str, Path | dict[str, str]]:
        repo = case_dir / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "deploy/dalton").mkdir(parents=True)
        (repo / "scripts/dalton/ops/prod").mkdir(parents=True)
        (repo / "web").mkdir(parents=True)

        # v1 uses LONG SYNTAX volumes for both /srv and /etc/caddy/Caddyfile (no '/srv:ro' literal!)
        (repo / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n"
            "  edge:\n"
            "    image: caddy:2.9.9-custom\n"
            "    volumes:\n"
            "      - type: bind\n"
            "        source: ./deploy/dalton/Caddyfile\n"
            "        target: /etc/caddy/Caddyfile\n"
            "        read_only: true\n"
            "      - type: bind\n"
            "        source: ./web/dist\n"
            "        target: /srv\n"
            "        read_only: true\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ':8088 {\n\troot * /srv\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n'
        )
        shutil.copy2(_DEPLOY_SCRIPT, repo / "scripts/dalton/ops/prod/deploy_main.sh")
        shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1 long syntax legacy"], check=True)

        # v2 uses LONG SYNTAX standard /srv-releases mount
        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n"
            "  edge:\n"
            "    image: caddy:2.9.1-alpine\n"
            "    volumes:\n"
            "      - type: bind\n"
            "        source: ./deploy/dalton/Caddyfile\n"
            "        target: /etc/caddy/Caddyfile\n"
            "        read_only: true\n"
            "      - type: bind\n"
            "        source: ./web/releases\n"
            "        target: /srv-releases\n"
            "        read_only: true\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ':8088 {\n\troot * /srv-releases/current\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n'
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v2 long syntax releases"], check=True)
        _write_release_bundle(repo / "web/dist", tag="v1", entry_chunk="index-long1111.js")

        fake_bin = case_dir / "bin"
        fake_bin.mkdir()
        docker_trace = case_dir / "docker.trace"
        (fake_bin / "docker").write_text(
            f"""#!/bin/bash
set -euo pipefail
echo "docker:$*" >> "{docker_trace}"
case "${{1:-}}" in
  inspect)
    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-r6-proj"; exit 0; fi
    echo "bind|{repo.resolve()}/web/dist->/srv"
    exit 0
    ;;
  ps) echo "edge-cid-1"; exit 0 ;;
  logs) echo "log"; exit 0 ;;
  run)
    if [[ "$*" == *"--entrypoint caddy"* ]] && [[ "$*" == *"caddy:2.9.9-custom validate --config /etc/caddy/Caddyfile --adapter caddyfile"* ]]; then
      if [ "{1 if caddy_validate_fail else 0}" = "1" ]; then
        echo "Error: invalid Caddyfile directive" >&2
        exit 1
      fi
      exit 0
    fi
    ;;
  compose)
    if [[ "$*" == *" config"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        rb_cf=""
        prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ] && [[ "$a" == *"docker-compose.dalton.yml" ]]; then rb_cf="$a"; fi
          prev="$a"
        done
        cat "$rb_cf"
      else
        cat "{repo}/docker-compose.dalton.yml"
      fi
      exit 0
    fi
    if [[ "$*" == *"up -d --no-deps --force-recreate edge"* ]]; then
      exit 0
    fi
    ;;
esac
echo "UNEXPECTED_DOCKER:$*" >&2
exit 99
"""
        )
        (fake_bin / "curl").write_text(
            """#!/bin/bash
set -euo pipefail
if [[ "$*" == *":8088/__edge_health"* ]]; then
  printf 'dalton-edge-ok releases-v1\\n200'
  exit 0
fi
exit 99
"""
        )
        (fake_bin / "sleep").write_text("#!/bin/bash\nexit 0\n")
        for name in ("docker", "curl", "sleep"):
            (fake_bin / name).chmod(0o755)
        (fake_bin / "python3").symlink_to(sys.executable)

        stub_dir = _install_edge_http_stub(case_dir)
        lock_file = case_dir / "locks/deploy.lock"
        env = {
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(case_dir),
            "DALTON_REPO_ROOT": str(repo),
            "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
            "DALTON_EDGE_PORT": "8088",
            "PYTHONPATH": str(stub_dir),
            "TEST_EDGE_SERVE_ROOT": str(repo / "web/releases/current"),
        }
        return {"repo": repo, "docker_trace": docker_trace, "lock_file": lock_file, "env": env}

    # Case 1: Long-syntax PRE_FF auto-discovered (without --pre-ff) & validated via old edge image caddy validate
    c_ok = tmp_path / "long_ok"
    c_ok.mkdir()
    r_ok = _build_case(c_ok, caddy_validate_fail=False)
    res_ok = subprocess.run(
        ["bash", str(Path(r_ok["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=r_ok["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_ok.returncode == 0, res_ok.stdout + res_ok.stderr
    assert "caddy:2.9.9-custom validate --config /etc/caddy/Caddyfile --adapter caddyfile" in Path(r_ok["docker_trace"]).read_text()

    # Case 2: caddy validate fails -> abort BEFORE touching running edge!
    c_fail = tmp_path / "caddy_fail"
    c_fail.mkdir()
    r_fail = _build_case(c_fail, caddy_validate_fail=True)
    res_fail = subprocess.run(
        ["bash", str(Path(r_fail["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=r_fail["env"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_fail.returncode != 0
    assert "caddy validate" in res_fail.stderr
    assert "up -d --no-deps --force-recreate edge" not in Path(r_fail["docker_trace"]).read_text()
    assert not _is_lock_held(Path(r_fail["lock_file"]))


def test_r6_resume_without_lock_owner_pid_rejected_clearly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    lock_file = tmp_path / "locks/deploy.lock"
    lock_file.parent.mkdir(parents=True)
    lock_file.write_text(f"pid={os.getpid()} started=2026-11-26T00:00:00Z repo={repo}\n", encoding="utf-8")
    state_file = tmp_path / "resume.state"
    state_file.write_text(
        "DALTON_RESUME_PROTOCOL=2\n"
        "PRE_FF=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\n"
        "TARGET=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb\n"
        "DEPLOY_FF_DONE=1\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)

    # Hold FD 9 open in a child python helper and exec deploy_main.sh WITHOUT DALTON_DEPLOY_LOCK_OWNER_PID
    env = {
        "PATH": f"{fake_bin}:/bin:/usr/bin",
        "HOME": str(tmp_path),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
        "DALTON_DEPLOY_LOCK_HELD_FD": "9",
        "DALTON_DEPLOY_REEXECED": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "DALTON_DEPLOY_STATE_FILE": str(state_file),
        # Intentionally omit DALTON_DEPLOY_LOCK_OWNER_PID!
    }
    helper = (
        "import fcntl, os, subprocess, sys\n"
        "lock_file, script = sys.argv[1], sys.argv[2]\n"
        "fd = os.open(lock_file, os.O_RDWR)\n"
        "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
        "os.dup2(fd, 9)\n"
        "os.set_inheritable(9, True)\n"
        "res = subprocess.run(['bash', script], pass_fds=(9,))\n"
        "sys.exit(res.returncode)\n"
    )
    res = subprocess.run(
        [sys.executable, "-c", helper, str(lock_file), str(_DEPLOY_SCRIPT)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode != 0
    assert "DALTON_DEPLOY_LOCK_OWNER_PID" in res.stderr
    assert "re-run deploy_main.sh" in res.stderr


def test_r6_multicast_second_sigterm_during_cleanup_and_direct_build_sigterm_plus_default_dist_lock(
    tmp_path: Path,
) -> None:
    # Subtest A: Double SIGTERM sent while deploy_main.sh is reaping build PGID -> grandchild still reaped cleanly
    case_a = tmp_path / "double_sigterm"
    case_a.mkdir()
    ready_a = case_a / "build.ready"
    go_a = case_a / "build.go"
    gpid_a_file = case_a / "grandchild.pid"
    ctx_a = _setup_strict_deploy_env(case_a, build_barrier=(ready_a, go_a))
    seed_a = Path(ctx_a["seed"])
    (seed_a / "scripts/dalton/build_frontend_prod.sh").write_text(
        _make_fake_build_script(
            tag="v2",
            entry_chunk="index-v2multicast.js",
            trace_file=Path(ctx_a["build_trace"]),
            block_on_barrier=(ready_a, go_a),
            spawn_grandchild_pid_file=gpid_a_file,
        )
    )
    (seed_a / "app.py").write_text("v2-multicast\n")
    _commit_and_push_seed(seed_a, "v2 multicast sigterm")

    cmd_a, env_a = _wrapper_env_and_cmd(ctx_a, case_a)
    proc_a = subprocess.Popen(cmd_a, env=env_a, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    gpid_a = 0
    try:
        deadline = time.monotonic() + 15
        while not ready_a.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_a.exists()
        gpid_a = int(gpid_a_file.read_text(encoding="utf-8").strip())
        os.kill(gpid_a, 0)
        proc_a.send_signal(signal.SIGTERM)
        time.sleep(0.05)
        proc_a.send_signal(signal.SIGTERM)
        out_a, err_a = proc_a.communicate(timeout=15)
        assert proc_a.returncode != 0, out_a + err_a
        alive_a = True
        try:
            os.kill(gpid_a, 0)
        except ProcessLookupError:
            alive_a = False
        assert not alive_a, f"grandchild {gpid_a} survived double SIGTERM"
    finally:
        go_a.write_text("go\n")
        if proc_a.poll() is None:
            proc_a.kill()
        if gpid_a > 0:
            try:
                os.kill(gpid_a, signal.SIGKILL)
            except OSError:
                pass
    assert not _is_lock_held(Path(ctx_a["lock_file"]))

    # Subtest B & C: Direct build_frontend_prod.sh (--out-dir SIGTERM kills pnpm grandchild + cleans reserved dir;
    # default web/dist build respects FD 9 deploy lock)
    case_b = tmp_path / "direct_build_pgid"
    repo_b = case_b / "repo"
    (repo_b / "scripts/dalton").mkdir(parents=True)
    (repo_b / "web").mkdir(parents=True)
    (repo_b / "docs-site").mkdir(parents=True)
    shutil.copy2(_BUILD_FRONTEND_SCRIPT, repo_b / "scripts/dalton/build_frontend_prod.sh")
    (repo_b / "web/.env.dalton").write_text("VITE_BRAND_NAME=Everflow\nVITE_BRAND_MARK=everflow\n")
    (repo_b / ".env").write_text(
        "APP_ENV=dalton\nDALTON_AUTH_MODE=supabase\n"
        "SUPABASE_URL=https://example.supabase.co\nSUPABASE_PUBLISHABLE_KEY=pk_test_123\n"
    )
    ready_b = case_b / "pnpm.ready"
    go_b = case_b / "pnpm.go"
    pnpm_gpid_file = case_b / "pnpm_grandchild.pid"
    pnpm_trace = case_b / "pnpm.trace"
    fake_bin_b = case_b / "bin"
    fake_bin_b.mkdir()
    (fake_bin_b / "pnpm").write_text(
        f"""#!/bin/bash
set -euo pipefail
echo "pnpm:$*" >> "{pnpm_trace}"
if [ "${{1:-}}" = "build" ]; then
  "{sys.executable}" -c 'import time; time.sleep(60)' &
  echo "$!" > "{pnpm_gpid_file}"
  touch "{ready_b}"
  while [ ! -f "{go_b}" ]; do
    "{sys.executable}" -c 'import time; time.sleep(0.02)'
  done
fi
exit 0
"""
    )
    (fake_bin_b / "pnpm").chmod(0o755)
    (fake_bin_b / "python3").symlink_to(sys.executable)
    lock_b = case_b / "locks/deploy.lock"
    lock_b.parent.mkdir(parents=True)
    env_b = {
        "PATH": f"{fake_bin_b}:/bin:/usr/bin",
        "HOME": str(case_b),
        "DALTON_DEPLOY_LOCK_FILE": str(lock_b),
    }

    # Subtest B: Direct --out-dir build receives SIGTERM -> pnpm grandchild killed, reserved dir removed, lock released
    proc_b = subprocess.Popen(
        ["bash", str(repo_b / "scripts/dalton/build_frontend_prod.sh"), "--require-supabase", "--out-dir", "web/releases/direct-sigterm"],
        cwd=repo_b,
        env=env_b,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    pnpm_gpid = 0
    try:
        deadline = time.monotonic() + 15
        while not ready_b.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready_b.exists(), "direct build pnpm barrier never reached"
        pnpm_gpid = int(pnpm_gpid_file.read_text(encoding="utf-8").strip())
        os.kill(pnpm_gpid, 0)
        proc_b.send_signal(signal.SIGTERM)
        out_b, err_b = proc_b.communicate(timeout=15)
        assert proc_b.returncode != 0, out_b + err_b
        alive_b = True
        try:
            os.kill(pnpm_gpid, 0)
        except ProcessLookupError:
            alive_b = False
        assert not alive_b, f"pnpm grandchild {pnpm_gpid} survived direct build_frontend_prod.sh SIGTERM"
    finally:
        go_b.write_text("go\n")
        if proc_b.poll() is None:
            proc_b.kill()
        if pnpm_gpid > 0:
            try:
                os.kill(pnpm_gpid, signal.SIGKILL)
            except OSError:
                pass
    assert not (repo_b / "web/releases/direct-sigterm").exists()
    assert not _is_lock_held(lock_b)

    # Subtest C: Default web/dist build (no --out-dir) respects FD 9 deploy lock!
    pnpm_trace.unlink(missing_ok=True)
    fd = os.open(lock_b, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        res_c = subprocess.run(
            ["bash", str(repo_b / "scripts/dalton/build_frontend_prod.sh"), "--require-supabase"],
            cwd=repo_b,
            env=env_b,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        assert res_c.returncode != 0
        assert "lock" in res_c.stderr.lower()
        assert not pnpm_trace.exists()
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_r6_rollback_failed_state_in_fd9_blocks_next_deploy_without_sentinel_file(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    (repo / "scripts/dalton").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)
    (repo / "docs-site").mkdir(parents=True)
    shutil.copy2(_BUILD_FRONTEND_SCRIPT, repo / "scripts/dalton/build_frontend_prod.sh")
    (repo / "web/.env.dalton").write_text("VITE_BRAND_NAME=Everflow\nVITE_BRAND_MARK=everflow\n")
    (repo / ".env").write_text(
        "APP_ENV=dalton\nDALTON_AUTH_MODE=supabase\n"
        "SUPABASE_URL=https://example.supabase.co\nSUPABASE_PUBLISHABLE_KEY=pk_test_123\n"
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "python3").symlink_to(sys.executable)
    lock_file = tmp_path / "locks/deploy.lock"
    lock_file.parent.mkdir(parents=True)
    # Only the lock file itself carries state=rollback_failed; NO .rollback-failed sentinel file exists!
    lock_file.write_text("state=rollback_failed\npid=12345\nreason=edge_rollback_failed\n", encoding="utf-8")
    assert not Path(f"{lock_file}.rollback-failed").exists()

    env = {
        "PATH": f"{fake_bin}:/bin:/usr/bin",
        "HOME": str(tmp_path),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
    }

    # 1. deploy_main.sh _acquire_deploy_lock re-checks FD 9 AFTER flock and refuses!
    res_deploy = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1 source "{_DEPLOY_SCRIPT}"; _acquire_deploy_lock'],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_deploy.returncode != 0
    assert "rollback-failed" in res_deploy.stderr or "rollback_failed" in res_deploy.stderr

    # 2. build_frontend_prod.sh --out-dir re-checks FD 9 AFTER flock and refuses!
    res_build = subprocess.run(
        ["bash", str(repo / "scripts/dalton/build_frontend_prod.sh"), "--require-supabase", "--out-dir", "web/releases/blocked-1"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_build.returncode != 0
    assert "rollback-failed" in res_build.stderr or "rollback_failed" in res_build.stderr
    assert not (repo / "web/releases/blocked-1").exists()


def test_r6_second_transitional_normal_deploy_omits_invalid_pre_ff_hint(tmp_path: Path) -> None:
    ctx = _setup_strict_deploy_env(tmp_path, seed_v1_release=False, seed_legacy_dist=True)
    seed = Path(ctx["seed"])
    repo = Path(ctx["repo"])

    (seed / "deploy/dalton").mkdir(parents=True, exist_ok=True)
    (seed / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
    (seed / "docker-compose.dalton.yml").write_text(
        "services:\n  edge:\n    volumes:\n      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n      - ./web/dist:/srv:ro\n"
    )
    (seed / "deploy/dalton/Caddyfile").write_text(':8088 {\n\troot * /srv\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n')
    v1_legacy = _commit_and_push_seed(seed, "v1 legacy")
    subprocess.run(["git", "-C", str(repo), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "--ff-only", v1_legacy], check=True)

    (seed / "docker-compose.dalton.yml").write_text(
        "services:\n  edge:\n    volumes:\n      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n      - ./web/releases:/srv-releases:ro\n"
    )
    (seed / "deploy/dalton/Caddyfile").write_text(
        ':8088 {\n\troot * /srv-releases/current\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n'
    )
    v2_releases = _commit_and_push_seed(seed, "v2 releases")

    fake_docker = Path(ctx["fake_bin"]) / "docker"
    docker_text = fake_docker.read_text().replace(
        '  inspect)\n    echo "backend mem_limit=6442450944"\n    exit 0\n    ;;',
        f'  inspect)\n'
        f'    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi\n'
        f'    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-proj"; exit 0; fi\n'
        f'    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then echo "bind|{repo.resolve()}/web/dist->/srv"; exit 0; fi\n'
        f'    echo "backend mem_limit=6442450944"\n'
        f'    exit 0\n'
        f'    ;;',
        1,
    ).replace(
        '    elif [ "${2:-}" = "config" ] || [[ "$*" == *" config"* ]]; then\n'
        '      echo "services:"\n'
        '      echo "  edge:"\n'
        '      echo "    volumes:"\n'
        '      echo "      - ./web/releases:/srv-releases:ro"\n'
        '      exit 0',
        f'    elif [ "${{2:-}}" = "config" ] || [[ "$*" == *" config"* ]]; then\n'
        f'      cat "{repo}/docker-compose.dalton.yml"\n'
        f'      exit 0',
        1,
    )
    fake_docker.write_text(docker_text)
    fake_docker.chmod(0o755)

    # First run (HEAD == v1_legacy): refuses BEFORE fast-forward, exits 3 WITH valid --pre-ff <v1_legacy>
    res1 = _run_via_wrapper(ctx, tmp_path)
    assert res1.returncode == 3, res1.stdout + res1.stderr
    assert f"--first-releases-rollout --pre-ff {v1_legacy}" in res1.stderr
    assert subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip() == v1_legacy

    # Second run after operator already fast-forwarded HEAD to v2_releases (NOT a legacy /srv commit!):
    # -> exits 3 with --first-releases-rollout and MUST NOT print invalid --pre-ff <v2_releases>!
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "--ff-only", v2_releases], check=True)
    res2 = _run_via_wrapper(ctx, tmp_path)
    assert res2.returncode == 3, res2.stdout + res2.stderr
    assert "--first-releases-rollout" in res2.stderr
    assert f"--pre-ff {v2_releases}" not in res2.stderr
    assert "--pre-ff" not in res2.stderr


# ---------------------------------------------------------------------------
# 10. r7: 3 final A-class fixes (pre-ff refusal, per-attempt rollback snapshots,
#     and PGID + deadline timeout for edge compose up & rollback)
# ---------------------------------------------------------------------------


def test_r7_first_rollout_isolates_per_attempt_rollback_snapshots_and_only_cleans_unbound(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    (repo / "deploy/dalton").mkdir(parents=True)
    (repo / "scripts/dalton/ops/prod").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)

    (repo / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
    (repo / "docker-compose.dalton.yml").write_text(
        "services:\n"
        "  edge:\n"
        "    image: caddy:2.9.1-alpine\n"
        "    volumes:\n"
        "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
        "      - ./web/dist:/srv:ro\n"
    )
    (repo / "deploy/dalton/Caddyfile").write_text(
        ':8088 {\n\troot * /srv\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n'
    )
    shutil.copy2(_DEPLOY_SCRIPT, repo / "scripts/dalton/ops/prod/deploy_main.sh")
    shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1 legacy"], check=True)

    (repo / "docker-compose.dalton.yml").write_text(
        "services:\n"
        "  edge:\n"
        "    image: caddy:2.9.1-alpine\n"
        "    volumes:\n"
        "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
        "      - ./web/releases:/srv-releases:ro\n"
    )
    (repo / "deploy/dalton/Caddyfile").write_text(
        ':8088 {\n\troot * /srv-releases/current\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n'
    )
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v2 releases"], check=True)
    _write_release_bundle(repo / "web/dist", tag="legacy-prod", entry_chunk="index-old11111.js")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    edge_state = tmp_path / "edge.state"
    edge_state.write_text("legacy")
    # Track which mounts each container currently holds so docker ps -aq + docker inspect are realistic!
    edge_caddy_mount = tmp_path / "edge_caddy.mount"
    edge_caddy_mount.write_text(f"{repo.resolve()}/deploy/dalton/Caddyfile")
    extra_bound_mount = tmp_path / "extra_bound.mount"
    extra_bound_mount.write_text("")
    caddy_validate_fail = tmp_path / "caddy_fail.flag"

    (fake_bin / "docker").write_text(
        f"""#!/bin/bash
set -euo pipefail
case "${{1:-}}" in
  inspect)
    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-r7-proj"; exit 0; fi
    if [[ "$*" == *"{{.Source}}"* ]] && [[ "$*" != *"Destination"* ]]; then
      if [ "$(cat "{edge_state}")" = "new_mount" ]; then
        printf '%s\\n%s\\n' "{repo.resolve()}/web/releases" "$(cat "{edge_caddy_mount}")"
      else
        printf '%s\\n%s\\n' "{repo.resolve()}/web/dist" "$(cat "{edge_caddy_mount}")"
      fi
      extra=$(cat "{extra_bound_mount}")
      [ -n "$extra" ] && printf '%s\\n' "$extra"
      exit 0
    fi
    if [[ "$*" == *"dalton-langalpha-edge-1"* ]]; then
      if [ "$(cat "{edge_state}")" = "new_mount" ]; then
        printf 'bind|%s->/srv-releases\\nbind|%s->/etc/caddy/Caddyfile\\n' "{repo.resolve()}/web/releases" "$(cat "{edge_caddy_mount}")"
      else
        printf 'bind|%s->/srv\\nbind|%s->/etc/caddy/Caddyfile\\n' "{repo.resolve()}/web/dist" "$(cat "{edge_caddy_mount}")"
      fi
      exit 0
    fi
    ;;
  ps)
    echo "edge-cid-1"
    exit 0
    ;;
  logs)
    echo "edge log"
    exit 0
    ;;
  run)
    if [[ "$*" == *"--entrypoint caddy"* ]] && [[ "$*" == *"validate --config /etc/caddy/Caddyfile --adapter caddyfile"* ]]; then
      if [ -f "{caddy_validate_fail}" ]; then
        echo "Error: simulated caddy validate failure on retry" >&2
        exit 1
      fi
      exit 0
    fi
    ;;
  compose)
    if [[ "$*" == *" config"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        rb_cf=""; prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ] && [[ "$a" == *"docker-compose.dalton.yml" ]]; then rb_cf="$a"; fi
          prev="$a"
        done
        cat "$rb_cf"
      else
        cat "{repo}/docker-compose.dalton.yml"
      fi
      exit 0
    fi
    if [[ "$*" == *"up -d --no-deps --force-recreate edge"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        rb_file=""; prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ]; then rb_file="$a"; fi
          prev="$a"
        done
        echo "$(dirname "$rb_file")/Caddyfile" > "{edge_caddy_mount}"
        echo "rolled_back" > "{edge_state}"
      else
        echo "{repo.resolve()}/deploy/dalton/Caddyfile" > "{edge_caddy_mount}"
        echo "new_mount" > "{edge_state}"
      fi
      exit 0
    fi
    ;;
esac
echo "UNEXPECTED_DOCKER:$*" >&2
exit 99
"""
    )
    (fake_bin / "curl").write_text(
        f"""#!/bin/bash
set -euo pipefail
if [[ "$*" == *":8088/__edge_health"* ]]; then
  if [ "$(cat "{edge_state}")" = "rolled_back" ]; then
    printf 'ok\\n200'
  else
    printf 'dalton-edge-ok releases-v1\\n200'
  fi
  exit 0
fi
if [[ "$*" == *"http://127.0.0.1:8088/"* ]]; then
  printf '<!doctype html><div id="root">legacy</div>\\n200'
  exit 0
fi
exit 99
"""
    )
    (fake_bin / "sleep").write_text("#!/bin/bash\nexit 0\n")
    for name in ("docker", "curl", "sleep"):
        (fake_bin / name).chmod(0o755)
    (fake_bin / "python3").symlink_to(sys.executable)

    stub_dir = _install_edge_http_stub(tmp_path)
    lock_file = tmp_path / "locks/deploy.lock"
    base_env = {
        "PATH": f"{fake_bin}:/bin:/usr/bin",
        "HOME": str(tmp_path),
        "DALTON_REPO_ROOT": str(repo),
        "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
        "DALTON_EDGE_PORT": "8088",
        "PYTHONPATH": str(stub_dir),
        "TEST_EDGE_SERVE_ROOT": str(repo / "web/releases/current"),
    }

    # Attempt 1: Public edge check fails -> rolls back edge onto attempt-1/Caddyfile and keeps attempt-1!
    res1 = subprocess.run(
        ["bash", str(repo / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env={**base_env, "TEST_EDGE_OVERRIDE_BUILD": "index-mismatch999.js"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert res1.returncode != 0
    rb_root = repo / "web/releases/.first-rollout-rollback"
    attempts_after_1 = sorted(p for p in rb_root.glob("attempt-*") if p.is_dir())
    assert len(attempts_after_1) == 1, attempts_after_1
    attempt_1 = attempts_after_1[0]
    assert (attempt_1 / "Caddyfile").is_file()
    assert edge_caddy_mount.read_text(encoding="utf-8").strip() == str(attempt_1 / "Caddyfile")

    # Attempt 2: Retry fails during precheck (caddy validate fails) -> MUST NOT delete attempt-1!
    caddy_validate_fail.write_text("1\n")
    res2 = subprocess.run(
        ["bash", str(repo / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res2.returncode != 0
    assert (attempt_1 / "Caddyfile").is_file(), "retry precheck failure deleted attempt-1 snapshot bound by running edge!"
    assert sorted(p for p in rb_root.glob("attempt-*") if p.is_dir()) == [attempt_1]
    caddy_validate_fail.unlink()

    # Attempt 3: Rollout succeeds while an extra stopped container still binds attempt-1/Caddyfile ->
    # attempt-3 (unbound) is cleaned up, while attempt-1 (still bound by container) is PRESERVED!
    extra_bound_mount.write_text(str(attempt_1 / "Caddyfile"))
    res3 = subprocess.run(
        ["bash", str(repo / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res3.returncode == 0, res3.stdout + res3.stderr
    assert sorted(p for p in rb_root.glob("attempt-*") if p.is_dir()) == [attempt_1]

    # Once no container binds attempt-1 anymore, cleanup removes it safely!
    extra_bound_mount.write_text("")
    res_clean = subprocess.run(
        ["bash", "-c", f'set -euo pipefail; DALTON_DEPLOY_LIB_ONLY=1 source "{repo}/scripts/dalton/ops/prod/deploy_main.sh"; _cleanup_unused_first_rollout_rollbacks'],
        env=base_env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res_clean.returncode == 0, res_clean.stdout + res_clean.stderr
    assert list(rb_root.glob("attempt-*")) == []


def test_r7_first_rollout_compose_up_sigterm_and_deadline_timeout_kill_pgid_and_rollback_or_lock(
    tmp_path: Path,
) -> None:
    def _build_hanging_env(
        case_dir: Path,
        *,
        hang_target: str,  # "forward" or "rollback"
        timeout_sec: int,
    ) -> dict[str, Path | dict[str, str]]:
        repo = case_dir / "repo"
        repo.mkdir(parents=True)
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
        (repo / "deploy/dalton").mkdir(parents=True)
        (repo / "scripts/dalton/ops/prod").mkdir(parents=True)
        (repo / "web").mkdir(parents=True)

        (repo / "docker-compose.yml").write_text("services:\n  backend:\n    image: dalton:latest\n")
        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n  edge:\n    image: caddy:2.9.1-alpine\n    volumes:\n"
            "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
            "      - ./web/dist:/srv:ro\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ':8088 {\n\troot * /srv\n\thandle /__edge_health {\n\t\trespond "ok" 200\n\t}\n}\n'
        )
        shutil.copy2(_DEPLOY_SCRIPT, repo / "scripts/dalton/ops/prod/deploy_main.sh")
        shutil.copy2(_CHECK_PUBLIC_EDGE_SCRIPT, repo / "scripts/dalton/check_public_edge.sh")
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v1 legacy"], check=True)

        (repo / "docker-compose.dalton.yml").write_text(
            "services:\n  edge:\n    image: caddy:2.9.1-alpine\n    volumes:\n"
            "      - ./deploy/dalton/Caddyfile:/etc/caddy/Caddyfile:ro\n"
            "      - ./web/releases:/srv-releases:ro\n"
        )
        (repo / "deploy/dalton/Caddyfile").write_text(
            ':8088 {\n\troot * /srv-releases/current\n\thandle /__edge_health {\n\t\trespond "dalton-edge-ok releases-v1" 200\n\t}\n}\n'
        )
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", "v2 releases"], check=True)
        _write_release_bundle(repo / "web/dist", tag="v1", entry_chunk="index-old11111.js")

        fake_bin = case_dir / "bin"
        fake_bin.mkdir()
        ready_file = case_dir / "compose.ready"
        gpid_file = case_dir / "compose_grandchild.pid"
        edge_state = case_dir / "edge.state"
        edge_state.write_text("legacy")
        hang_block = f"""
        "{sys.executable}" -c 'import time; time.sleep(60)' &
        echo "$!" > "{gpid_file}"
        touch "{ready_file}"
        while true; do "{sys.executable}" -c 'import time; time.sleep(0.1)'; done
"""
        (fake_bin / "docker").write_text(
            f"""#!/bin/bash
set -euo pipefail
case "${{1:-}}" in
  inspect)
    if [[ "$*" == *"State.Running"* ]]; then echo "true"; exit 0; fi
    if [[ "$*" == *"com.docker.compose.project"* ]]; then echo "dalton-hang-proj"; exit 0; fi
    echo "bind|{repo.resolve()}/web/dist->/srv"
    exit 0
    ;;
  ps) echo "edge-cid-1"; exit 0 ;;
  logs) echo "log"; exit 0 ;;
  run)
    if [[ "$*" == *"--entrypoint caddy"* ]] && [[ "$*" == *"validate --config /etc/caddy/Caddyfile --adapter caddyfile"* ]]; then
      exit 0
    fi
    ;;
  compose)
    if [[ "$*" == *" config"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        rb_cf=""; prev=""
        for a in "$@"; do
          if [ "$prev" = "-f" ] && [[ "$a" == *"docker-compose.dalton.yml" ]]; then rb_cf="$a"; fi
          prev="$a"
        done
        cat "$rb_cf"
      else
        cat "{repo}/docker-compose.dalton.yml"
      fi
      exit 0
    fi
    if [[ "$*" == *"up -d --no-deps --force-recreate edge"* ]]; then
      if [[ "$*" == *".first-rollout-rollback"* ]]; then
        if [ "{hang_target}" = "rollback" ]; then
          {hang_block}
        fi
        echo "rolled_back" > "{edge_state}"
        exit 0
      else
        if [ "{hang_target}" = "forward" ]; then
          {hang_block}
        fi
        echo "new_mount" > "{edge_state}"
        exit 0
      fi
    fi
    ;;
esac
echo "UNEXPECTED_DOCKER:$*" >&2
exit 99
"""
        )
        (fake_bin / "curl").write_text(
            f"""#!/bin/bash
set -euo pipefail
if [[ "$*" == *":8088/__edge_health"* ]]; then
  if [ "$(cat "{edge_state}")" = "rolled_back" ]; then
    printf 'ok\\n200'
  else
    printf 'dalton-edge-ok releases-v1\\n200'
  fi
  exit 0
fi
if [[ "$*" == *"http://127.0.0.1:8088/"* ]]; then
  printf '<!doctype html><div id="root">legacy</div>\\n200'
  exit 0
fi
exit 99
"""
        )
        (fake_bin / "sleep").write_text("#!/bin/bash\nexit 0\n")
        for name in ("docker", "curl", "sleep"):
            (fake_bin / name).chmod(0o755)
        (fake_bin / "python3").symlink_to(sys.executable)

        stub_dir = _install_edge_http_stub(case_dir)
        lock_file = case_dir / "locks/deploy.lock"
        env = {
            "PATH": f"{fake_bin}:/bin:/usr/bin",
            "HOME": str(case_dir),
            "DALTON_REPO_ROOT": str(repo),
            "DALTON_DEPLOY_LOCK_FILE": str(lock_file),
            "DALTON_EDGE_PORT": "8088",
            "DALTON_EDGE_COMPOSE_TIMEOUT": str(timeout_sec),
            "PYTHONPATH": str(stub_dir),
            "TEST_EDGE_SERVE_ROOT": str(repo / "web/releases/current"),
        }
        if hang_target == "rollback":
            env["TEST_EDGE_OVERRIDE_BUILD"] = "index-mismatch999.js"
        return {
            "repo": repo,
            "ready": ready_file,
            "gpid": gpid_file,
            "edge_state": edge_state,
            "lock_file": lock_file,
            "env": env,
        }

    # Subtest A: Forward compose up hangs -> SIGTERM immediately interrupts wait, kills PGID, rolls back!
    c_a = tmp_path / "fwd_sigterm"
    c_a.mkdir()
    ctx_a = _build_hanging_env(c_a, hang_target="forward", timeout_sec=60)
    proc_a = subprocess.Popen(
        ["bash", str(Path(ctx_a["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=ctx_a["env"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    gpid_a = 0
    try:
        deadline = time.monotonic() + 15
        while not Path(ctx_a["ready"]).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert Path(ctx_a["ready"]).exists(), "forward compose barrier never reached"
        gpid_a = int(Path(ctx_a["gpid"]).read_text(encoding="utf-8").strip())
        os.kill(gpid_a, 0)
        t0 = time.monotonic()
        proc_a.send_signal(signal.SIGTERM)
        out_a, err_a = proc_a.communicate(timeout=20)
        elapsed_a = time.monotonic() - t0
        assert elapsed_a < 15.0, f"SIGTERM blocked waiting for hanging compose up ({elapsed_a:.1f}s)"
        assert proc_a.returncode == 143, f"rc={proc_a.returncode}\n{out_a}\n{err_a}"
        alive_a = True
        try:
            os.kill(gpid_a, 0)
        except ProcessLookupError:
            alive_a = False
        assert not alive_a, f"forward compose grandchild {gpid_a} survived SIGTERM"
    finally:
        if proc_a.poll() is None:
            proc_a.kill()
        if gpid_a > 0:
            try:
                os.kill(gpid_a, signal.SIGKILL)
            except OSError:
                pass
    assert Path(ctx_a["edge_state"]).read_text().strip() == "rolled_back"
    assert not _is_lock_held(Path(ctx_a["lock_file"]))

    # Subtest B: Forward compose up hangs -> deadline timeout (2s) kills PGID and rolls back to web/dist!
    c_b = tmp_path / "fwd_timeout"
    c_b.mkdir()
    ctx_b = _build_hanging_env(c_b, hang_target="forward", timeout_sec=2)
    res_b = subprocess.run(
        ["bash", str(Path(ctx_b["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=ctx_b["env"],
        capture_output=True,
        text=True,
        check=False,
        timeout=25,
    )
    assert res_b.returncode != 0
    assert "timed out" in res_b.stderr
    gpid_b = int(Path(ctx_b["gpid"]).read_text(encoding="utf-8").strip())
    alive_b = True
    try:
        os.kill(gpid_b, 0)
    except ProcessLookupError:
        alive_b = False
    assert not alive_b, f"forward compose grandchild {gpid_b} survived deadline timeout"
    assert Path(ctx_b["edge_state"]).read_text().strip() == "rolled_back"
    assert not _is_lock_held(Path(ctx_b["lock_file"]))

    # Subtest C: Rollback compose up hangs -> deadline timeout (2s) kills rollback PGID,
    # persists failure lock (sentinel + FD 9) and preserves rollback snapshot for manual recovery!
    c_c = tmp_path / "rb_timeout"
    c_c.mkdir()
    ctx_c = _build_hanging_env(c_c, hang_target="rollback", timeout_sec=2)
    res_c = subprocess.run(
        ["bash", str(Path(ctx_c["repo"]) / "scripts/dalton/ops/prod/deploy_main.sh"), "--first-releases-rollout"],
        env=ctx_c["env"],
        capture_output=True,
        text=True,
        check=False,
        timeout=25,
    )
    assert res_c.returncode != 0
    assert "rollback docker compose up timed out" in res_c.stderr
    gpid_c = int(Path(ctx_c["gpid"]).read_text(encoding="utf-8").strip())
    alive_c = True
    try:
        os.kill(gpid_c, 0)
    except ProcessLookupError:
        alive_c = False
    assert not alive_c, f"rollback compose grandchild {gpid_c} survived deadline timeout"
    assert Path(f"{ctx_c['lock_file']}.rollback-failed").is_file()
    assert "state=rollback_failed" in Path(ctx_c["lock_file"]).read_text(encoding="utf-8")
    assert list((Path(ctx_c["repo"]) / "web/releases/.first-rollout-rollback").glob("attempt-*/resolved-compose.yml"))
