"""Shared helpers for standalone core E2E check scripts.

Uses Python standard library only so checks run on a bare Linux CI runner
(Ubuntu) without creating a virtual environment or installing packages.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import http.client
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

VALID_AUTH_MODES = ("oss", "local")
_AUTH_HEADER_RE = re.compile(r"^\s*#\s*auth\s*:\s*([a-z0-9_-]+)\s*$", re.IGNORECASE)


def parse_check_auth(script_path: Path) -> str:
    """Parse the required ``# auth: <mode>`` declaration from a check script."""
    lines = script_path.read_text(encoding="utf-8").splitlines()[:25]
    for line in lines:
        match = _AUTH_HEADER_RE.match(line)
        if match:
            mode = match.group(1).lower()
            if mode not in VALID_AUTH_MODES:
                raise ValueError(
                    f"{script_path}: unsupported auth mode {mode!r}; "
                    f"expected one of {VALID_AUTH_MODES}"
                )
            return mode
    raise ValueError(
        f"{script_path}: missing '# auth: <oss|local>' header in first 25 lines"
    )


def discover_checks(checks_dir: Path) -> dict[str, list[Path]]:
    """Discover ``b*.py`` check scripts and group them by declared auth mode."""
    grouped: dict[str, list[Path]] = {}
    for path in sorted(checks_dir.glob("b*.py")):
        if not path.is_file():
            continue
        mode = parse_check_auth(path)
        grouped.setdefault(mode, []).append(path)

    ordered: dict[str, list[Path]] = {}
    for mode in VALID_AUTH_MODES:
        if mode in grouped:
            ordered[mode] = grouped.pop(mode)
    for mode in sorted(grouped):
        ordered[mode] = grouped[mode]
    return ordered


def load_env_file(env_path: Path) -> dict[str, str]:
    """Parse a dotenv file into a ``dict[str, str]`` without mutating ``os.environ``."""
    result: dict[str, str] = {}
    if not env_path.is_file():
        return result
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def http_request(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    data: bytes | None = None,
    timeout: float = 30.0,
) -> HttpResponse:
    """Perform an HTTP request and always return :class:`HttpResponse` (no raise on 4xx/5xx)."""
    req_headers: dict[str, str] = dict(headers or {})
    payload = data
    if json_body is not None:
        payload = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        if not any(k.lower() == "content-type" for k in req_headers):
            req_headers["Content-Type"] = "application/json"

    req = Request(url, data=payload, headers=req_headers, method=method.upper())
    try:
        with urlopen(req, timeout=timeout) as resp:
            return HttpResponse(
                status=resp.status,
                headers={k.lower(): v for k, v in resp.headers.items()},
                body=resp.read(),
            )
    except HTTPError as err:
        return HttpResponse(
            status=err.code,
            headers={k.lower(): v for k, v in err.headers.items()},
            body=err.read(),
        )


def wait_for_http_ready(
    url: str,
    *,
    timeout: float = 120.0,
    interval: float = 2.0,
    expected_statuses: tuple[int, ...] = (200,),
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    """Poll ``url`` until it returns one of ``expected_statuses`` or times out."""
    deadline = time.monotonic() + timeout
    last_error: str = "no response"
    while time.monotonic() < deadline:
        try:
            resp = http_request("GET", url, headers=headers, timeout=min(10.0, timeout))
            if resp.status in expected_statuses:
                return resp
            last_error = f"HTTP {resp.status}: {resp.text[:200]}"
        except (URLError, OSError, TimeoutError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(interval)
    raise TimeoutError(f"Service at {url} not ready within {timeout}s (last: {last_error})")


@dataclass(frozen=True)
class SSEEvent:
    event_id: str | None
    event: str
    raw_data: str
    data: Any


def parse_sse_block(lines: Sequence[str]) -> SSEEvent | None:
    """Parse accumulated non-empty lines of a single SSE frame."""
    event_id: str | None = None
    event_type = "message"
    data_lines: list[str] = []
    for raw in lines:
        if not raw or raw.startswith(":"):
            continue
        field, _, value = raw.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "id":
            event_id = value
        elif field == "event":
            event_type = value
        elif field == "data":
            data_lines.append(value)
    if not data_lines and event_id is None and event_type == "message":
        return None
    raw_data = "\n".join(data_lines)
    parsed: Any = raw_data
    if raw_data:
        try:
            parsed = json.loads(raw_data)
        except json.JSONDecodeError:
            parsed = raw_data
    return SSEEvent(
        event_id=event_id,
        event=event_type,
        raw_data=raw_data,
        data=parsed,
    )


@contextmanager
def open_sse_stream(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    json_body: Any = None,
    timeout: float = 120.0,
) -> Iterator[tuple[int, dict[str, str], Iterator[SSEEvent]]]:
    """Open a streaming SSE HTTP connection and yield parsed :class:`SSEEvent` items.

    Exiting the context manager closes the underlying TCP socket immediately,
    making mid-stream disconnect + reconnect tests deterministic.
    """
    parts = urlsplit(url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or (443 if parts.scheme == "https" else 80)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    conn: http.client.HTTPConnection
    if parts.scheme == "https":
        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)

    req_headers: dict[str, str] = {"Accept": "text/event-stream"}
    if headers:
        req_headers.update(headers)
    body_bytes: bytes | None = None
    if json_body is not None:
        body_bytes = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        if not any(k.lower() == "content-type" for k in req_headers):
            req_headers["Content-Type"] = "application/json"
        req_headers["Content-Length"] = str(len(body_bytes))

    try:
        conn.request(method.upper(), path, body=body_bytes, headers=req_headers)
        resp = conn.getresponse()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}

        def _iter() -> Iterator[SSEEvent]:
            buf: list[str] = []
            while True:
                raw_line = resp.readline()
                if not raw_line:
                    if buf:
                        ev = parse_sse_block(buf)
                        if ev is not None:
                            yield ev
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                if line == "":
                    if buf:
                        ev = parse_sse_block(buf)
                        buf = []
                        if ev is not None:
                            yield ev
                else:
                    buf.append(line)

        yield resp.status, resp_headers, _iter()
    finally:
        conn.close()


def extract_assistant_text(events: Sequence[SSEEvent]) -> str:
    """Collect streamed assistant text from backend SSE events."""
    chunks: list[str] = []
    for ev in events:
        if ev.event != "message_chunk" or not isinstance(ev.data, dict):
            continue
        content = ev.data.get("content")
        if isinstance(content, str):
            chunks.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, str):
                    chunks.append(block)
                elif isinstance(block, dict) and block.get("type") in (None, "text", "output_text"):
                    text = block.get("text")
                    if isinstance(text, str):
                        chunks.append(text)
    return "".join(chunks)


def extract_thread_id(events: Sequence[SSEEvent]) -> str | None:
    """Return the first ``thread_id`` seen across SSE event payloads."""
    for ev in events:
        if isinstance(ev.data, dict):
            tid = ev.data.get("thread_id")
            if isinstance(tid, str) and tid:
                return tid
    return None


def extract_run_id(events: Sequence[SSEEvent]) -> str | None:
    """Return the first ``run_id`` seen across SSE event payloads."""
    for ev in events:
        if isinstance(ev.data, dict):
            rid = ev.data.get("run_id")
            if isinstance(rid, str) and rid:
                return rid
    return None


def scan_tar_stream_for_paths(
    tar_bytes: bytes,
    forbidden_substrings: Sequence[str],
) -> list[str]:
    """Scan a ``docker save`` tar archive, inspecting every nested ``layer.tar``."""
    hits: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as outer:
        for member in outer.getmembers():
            if not member.isfile():
                continue
            name = member.name
            is_layer = name.endswith(".tar") or name.endswith(".tar.gz") or "/layer.tar" in name
            if not is_layer and not name.startswith("blobs/"):
                continue
            extracted = outer.extractfile(member)
            if extracted is None:
                continue
            raw_layer = extracted.read()
            try:
                with tarfile.open(fileobj=io.BytesIO(raw_layer), mode="r:*") as layer_tar:
                    for lmember in layer_tar.getmembers():
                        norm = lmember.name.lstrip("./")
                        for forbidden in forbidden_substrings:
                            if forbidden in norm:
                                hits.append(f"{name}:{norm}")
            except tarfile.TarError:
                # Non-tar blob (e.g., OCI JSON config/manifest)
                continue
    return sorted(set(hits))


class ComposeContext:
    """Context object representing the running standalone core Docker Compose stack."""

    def __init__(
        self,
        *,
        work_dir: Path,
        compose_files: Sequence[Path],
        artifacts_dir: Path,
        edge_url: str = "http://127.0.0.1:8088",
    ) -> None:
        self.work_dir = work_dir.resolve()
        self.compose_files = [Path(p).resolve() for p in compose_files]
        self.artifacts_dir = artifacts_dir.resolve()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.edge_url = edge_url.rstrip("/")
        self.env = load_env_file(self.work_dir / ".env")

    @classmethod
    def from_environment(cls) -> ComposeContext:
        work_dir = Path(os.environ.get("STANDALONE_WORK_DIR", os.getcwd())).resolve()
        raw_files = os.environ.get("STANDALONE_COMPOSE_FILES", "")
        if raw_files:
            compose_files = [Path(p) for p in raw_files.split(os.pathsep) if p]
        else:
            compose_files = [
                work_dir / "docker-compose.yml",
                work_dir / "docker-compose.core.yml",
                work_dir / "docker-compose.ci.yml",
            ]
        artifacts_dir = Path(
            os.environ.get("STANDALONE_ARTIFACTS_DIR", str(work_dir / "ci-artifacts"))
        )
        edge_url = os.environ.get("STANDALONE_EDGE_URL", "http://127.0.0.1:8088")
        return cls(
            work_dir=work_dir,
            compose_files=compose_files,
            artifacts_dir=artifacts_dir,
            edge_url=edge_url,
        )

    @property
    def db_user(self) -> str:
        return self.env.get("DB_USER") or os.environ.get("DB_USER") or "postgres"

    @property
    def db_name(self) -> str:
        return self.env.get("DB_NAME") or os.environ.get("DB_NAME") or "postgres"

    @property
    def internal_service_token(self) -> str:
        token = (
            self.env.get("INTERNAL_SERVICE_TOKEN")
            or os.environ.get("INTERNAL_SERVICE_TOKEN")
            or ""
        )
        if not token:
            raise RuntimeError("INTERNAL_SERVICE_TOKEN is missing from .env and environment")
        return token

    def compose_cmd(self, *args: str) -> list[str]:
        cmd = ["docker", "compose", "--project-directory", str(self.work_dir)]
        for cf in self.compose_files:
            cmd.extend(["-f", str(cf)])
        cmd.extend(args)
        return cmd

    def compose(
        self,
        *args: str,
        check: bool = True,
        capture_output: bool = True,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        cmd = self.compose_cmd(*args)
        proc = subprocess.run(
            cmd,
            cwd=str(self.work_dir),
            input=input_bytes,
            capture_output=capture_output,
        )
        if check and proc.returncode != 0:
            stderr = (proc.stderr or b"").decode("utf-8", errors="replace")
            stdout = (proc.stdout or b"").decode("utf-8", errors="replace")
            raise RuntimeError(
                f"Command failed (rc={proc.returncode}): {' '.join(cmd)}\n"
                f"STDOUT:\n{stdout[-2000:]}\nSTDERR:\n{stderr[-2000:]}"
            )
        return proc

    def psql_query(self, sql: str) -> str:
        proc = self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            self.db_user,
            "-d",
            self.db_name,
            "-v",
            "ON_ERROR_STOP=1",
            "-Atqc",
            sql,
        )
        return proc.stdout.decode("utf-8").strip()

    def psql_exec(self, sql: str) -> None:
        self.compose(
            "exec",
            "-T",
            "postgres",
            "psql",
            "-U",
            self.db_user,
            "-d",
            self.db_name,
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        )

    def get_alembic_heads(self) -> tuple[str, str]:
        """Return ``(upstream_head, dalton_head)`` dynamically from alembic inside backend."""
        def _parse_head(output: str) -> str:
            for line in output.splitlines():
                token = line.strip().split(" ", 1)[0]
                if token and not token.startswith("INFO") and not token.startswith("WAR"):
                    return token
            raise RuntimeError(f"Could not parse alembic head from:\n{output}")

        up = self.compose(
            "exec", "-T", "backend", "uv", "run", "alembic", "heads"
        ).stdout.decode("utf-8")
        da = self.compose(
            "exec",
            "-T",
            "backend",
            "uv",
            "run",
            "alembic",
            "-c",
            "src/dalton/alembic.ini",
            "heads",
        ).stdout.decode("utf-8")
        return _parse_head(up), _parse_head(da)

    def get_db_alembic_versions(self) -> tuple[str, str]:
        """Return ``(alembic_version, alembic_version_dalton)`` from Postgres."""
        upstream = self.psql_query("SELECT version_num FROM alembic_version;")
        dalton = self.psql_query("SELECT version_num FROM alembic_version_dalton;")
        return upstream.strip(), dalton.strip()

    def dump_database(self, output_path: Path) -> Path:
        """Export a restorable custom-format PostgreSQL dump via ``pg_dump -Fc``."""
        output_path.parent.mkdir(parents=True, exist_ok=True)
        proc = self.compose(
            "exec",
            "-T",
            "postgres",
            "pg_dump",
            "-U",
            self.db_user,
            "-d",
            self.db_name,
            "-Fc",
        )
        output_path.write_bytes(proc.stdout)
        # Validate dump readability with pg_restore --list
        self.compose(
            "exec",
            "-T",
            "postgres",
            "pg_restore",
            "--list",
            input_bytes=proc.stdout,
        )
        return output_path

    def scan_service_image_layers(
        self,
        service: str,
        forbidden_substrings: Sequence[str],
    ) -> list[str]:
        """Run ``docker save`` on a service's built image and scan every ``layer.tar``."""
        image_id = (
            self.compose("images", "-q", service)
            .stdout.decode("utf-8")
            .strip()
            .splitlines()[0]
        )
        saved = subprocess.run(
            ["docker", "save", image_id],
            capture_output=True,
            check=True,
        )
        return scan_tar_stream_for_paths(saved.stdout, forbidden_substrings)
