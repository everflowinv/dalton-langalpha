"""Shared helpers for standalone core E2E check scripts.

Uses Python standard library only so checks run on a bare Linux CI runner
(Ubuntu) without creating a virtual environment or installing packages.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import http.client
import io
import json
import os
from pathlib import Path
import posixpath
import re
import secrets
import subprocess
import tarfile
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

VALID_AUTH_MODES = ("oss", "local")
_AUTH_HEADER_RE = re.compile(r"^\s*#\s*auth\s*:\s*([a-z0-9_-]+)\s*$", re.IGNORECASE)
OWNER_SECRET_ENV_KEY = "LANGALPHA_OWNER_SECRET"
OWNER_HEADER_NAME = "X-LangAlpha-Owner"


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


SECRET_ENV_KEYS: tuple[str, ...] = (
    "DB_PASSWORD",
    "REDIS_PASSWORD",
    "MINIO_ROOT_USER",
    "MINIO_ROOT_PASSWORD",
    "DALTON_CHAT_UPLOAD_STORAGE_ACCESS_KEY_ID",
    "DALTON_CHAT_UPLOAD_STORAGE_SECRET_ACCESS_KEY",
    "DALTON_CHAT_UPLOAD_SIGNING_SECRET",
    "INTERNAL_SERVICE_TOKEN",
    "BYOK_ENCRYPTION_KEY",
    "EGRESS_RELAY_SECRET",
    "JWT_SIGNING_SECRET",
    "BOOTSTRAP_SETUP_TOKEN",
    "DALTON_RESEARCH_HOST_TOKEN",
    "DALTON_RESEARCH_ADMIN_TOKEN",
    OWNER_SECRET_ENV_KEY,
)

RUNTIME_SECRETS_ENV_VAR = "STANDALONE_RUNTIME_SECRETS_FILE"


def response_field_names(payload: Any) -> str:
    """Return a safe summary of JSON payload top-level field names (never values)."""
    if isinstance(payload, dict):
        return str(sorted(str(k) for k in payload.keys()))
    return f"<{type(payload).__name__}>"


def response_fields(resp: HttpResponse) -> str:
    """Return a safe summary of an :class:`HttpResponse` JSON payload's field names only."""
    try:
        payload = resp.json()
    except Exception:
        return "<non-json>"
    return response_field_names(payload)


def record_runtime_secret(
    label: str,
    value: str,
    *,
    secrets_file: Path | None = None,
) -> None:
    """Append a runtime-generated secret to the 0600 sidecar file for leak scanning.

    No-op when neither ``secrets_file`` nor ``STANDALONE_RUNTIME_SECRETS_FILE`` is set,
    or when ``value`` is empty. Never logs or prints the secret value.
    """
    cleaned = (value or "").strip()
    if not cleaned:
        return
    target = secrets_file
    if target is None:
        raw_path = (os.environ.get(RUNTIME_SECRETS_ENV_VAR) or "").strip()
        if not raw_path:
            return
        target = Path(raw_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    record = json.dumps({"label": label, "value": cleaned}, ensure_ascii=False) + "\n"
    fd = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with fh:
        fh.write(record)


def load_runtime_secrets(secrets_file: Path | None) -> list[tuple[str, str]]:
    """Load ``(label, value)`` pairs from a runtime-secrets JSONL file."""
    if secrets_file is None or not secrets_file.is_file():
        return []
    loaded: list[tuple[str, str]] = []
    for raw_line in secrets_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        label = str(obj.get("label") or "").strip()
        val = str(obj.get("value") or "").strip()
        if label and val:
            loaded.append((label, val))
    return loaded


def ensure_oss_owner_secret(env_path: Path, *, num_bytes: int = 32) -> None:
    """Generate a fresh random ``LANGALPHA_OWNER_SECRET`` and write it to ``env_path``.

    Preserves all other lines/comments in ``env_path``, replaces any existing
    ``LANGALPHA_OWNER_SECRET`` assignment (or appends one if absent), enforces
    mode ``0600`` atomically, and never prints or returns the secret value.
    """
    secret_val = secrets.token_hex(num_bytes)
    existing_lines: list[str] = []
    file_mode = 0o600
    if env_path.is_file():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines(keepends=True)
        existing_mode = env_path.stat().st_mode & 0o777
        # Keep owner-only permissions; tighten if group/other readable.
        file_mode = existing_mode & 0o600 or 0o600

    out: list[str] = []
    replaced = False
    for raw_line in existing_lines:
        stripped = raw_line.strip()
        if stripped and not stripped.startswith("#"):
            key_part = stripped[len("export ") :].strip() if stripped.startswith("export ") else stripped
            if "=" in key_part and key_part.split("=", 1)[0].strip() == OWNER_SECRET_ENV_KEY:
                if not replaced:
                    out.append(f"{OWNER_SECRET_ENV_KEY}={secret_val}\n")
                    replaced = True
                continue
        out.append(raw_line)

    if out and not out[-1].endswith("\n"):
        out[-1] = out[-1] + "\n"
    if not replaced:
        out.append(f"{OWNER_SECRET_ENV_KEY}={secret_val}\n")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".env.owner.", dir=str(env_path.parent))
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(fd, file_mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("".join(out))
        os.replace(tmp_path, env_path)
        os.chmod(env_path, file_mode)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _resolve_oss_owner_secret(
    ctx: ComposeContext | None = None,
    *,
    env_map: Mapping[str, str] | None = None,
) -> str:
    if env_map is not None:
        val = (env_map.get(OWNER_SECRET_ENV_KEY) or "").strip()
        if val:
            return val
    if ctx is not None:
        val = (ctx.env.get(OWNER_SECRET_ENV_KEY) or "").strip()
        if val:
            return val
    return (os.environ.get(OWNER_SECRET_ENV_KEY) or "").strip()


def oss_owner_headers(
    ctx: ComposeContext | None = None,
    *,
    extra: Mapping[str, str] | None = None,
    env_map: Mapping[str, str] | None = None,
    owner_secret: str | None = None,
    required: bool = False,
) -> dict[str, str]:
    """Build request headers carrying ``X-LangAlpha-Owner`` for ``auth: oss`` edge calls.

    Resolves ``LANGALPHA_OWNER_SECRET`` from ``owner_secret``, ``env_map``, ``ctx.env``,
    and finally ``os.environ``. Any ``extra`` headers are merged on top without mutating
    the caller's mapping. Never embeds any secret value in error messages.
    """
    secret = (
        owner_secret.strip()
        if owner_secret is not None
        else _resolve_oss_owner_secret(ctx, env_map=env_map)
    )
    if required and not secret:
        raise RuntimeError(
            f"{OWNER_SECRET_ENV_KEY} is missing from .env and environment; "
            "required for auth: oss requests through Caddy edge"
        )
    merged: dict[str, str] = {}
    if secret:
        merged[OWNER_HEADER_NAME] = secret
    if extra:
        merged.update(extra)
    return merged


def _redact_secrets_text(text: str, env: Mapping[str, str]) -> str:
    redacted = text
    for key in SECRET_ENV_KEYS:
        val = (env.get(key) or "").strip()
        if len(val) >= 12:
            redacted = redacted.replace(val, f"<REDACTED:{key}>")
    return redacted


def scan_artifacts_for_secrets(
    artifacts_dir: Path,
    env_path: Path,
    *,
    min_secret_len: int = 12,
    runtime_secrets_path: Path | None = None,
    extra_secrets: Sequence[tuple[str, str]] | None = None,
) -> list[str]:
    """Scan text/log files in ``artifacts_dir`` for secret values from ``env_path`` and runtime sources.

    Covers all ``SECRET_ENV_KEYS`` in ``env_path`` (including Research Host tokens and
    ``LANGALPHA_OWNER_SECRET``), plus any runtime-generated credentials (access tokens,
    WebSocket tickets, invite tokens, refresh cookies, passwords) recorded in
    ``runtime_secrets_path`` / ``STANDALONE_RUNTIME_SECRETS_FILE`` or passed via
    ``extra_secrets``.

    Never returns or logs any secret value — only ``<relpath>: leaked <KEY>``.
    """
    env_map = load_env_file(env_path)
    secret_entries: list[tuple[str, str]] = []
    for key in SECRET_ENV_KEYS:
        val = (env_map.get(key) or "").strip()
        if len(val) >= min_secret_len:
            secret_entries.append((key, val))

    resolved_runtime_path = runtime_secrets_path
    if resolved_runtime_path is None:
        env_rt = (os.environ.get(RUNTIME_SECRETS_ENV_VAR) or "").strip()
        if env_rt:
            resolved_runtime_path = Path(env_rt)
    for label, val in load_runtime_secrets(resolved_runtime_path):
        if len(val) >= min_secret_len:
            secret_entries.append((label, val))

    if extra_secrets:
        for label, raw_val in extra_secrets:
            val = (raw_val or "").strip()
            if label and len(val) >= min_secret_len:
                secret_entries.append((label, val))

    if not secret_entries or not artifacts_dir.is_dir():
        return []

    skip_resolved = resolved_runtime_path.resolve() if resolved_runtime_path is not None else None
    hits: list[str] = []
    seen_hits: set[tuple[str, str]] = set()
    for path in sorted(artifacts_dir.rglob("*")):
        if not path.is_file():
            continue
        if skip_resolved is not None and path.resolve() == skip_resolved:
            continue
        # Skip binary PostgreSQL custom dumps
        if path.suffix.lower() in (".dump", ".gz", ".tar", ".tgz", ".png", ".jpg"):
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(artifacts_dir))
        for key, val in secret_entries:
            if val in content and (rel, key) not in seen_hits:
                seen_hits.add((rel, key))
                hits.append(f"{rel}: leaked {key}")
    return hits


def _resolve_tar_member(
    members_by_name: Mapping[str, tarfile.TarInfo],
    raw_name: str,
) -> tarfile.TarInfo | None:
    """Resolve a path inside a docker-save tar, following internal symlinks."""
    current = posixpath.normpath(raw_name.lstrip("./"))
    for _ in range(8):
        member = members_by_name.get(current)
        if member is None:
            return None
        if member.issym() or member.islnk():
            target = member.linkname
            if not target:
                return None
            if target.startswith("/"):
                current = posixpath.normpath(target.lstrip("/"))
            else:
                parent = posixpath.dirname(current)
                current = posixpath.normpath(posixpath.join(parent, target))
            continue
        return member
    return None


def _visit_opened_docker_save_tar(
    outer: tarfile.TarFile,
    visitor: Callable[[str, tarfile.TarFile, tarfile.TarInfo], None],
) -> int:
    """Read ``manifest.json`` and visit every member of every declared layer strictly.

    Raises :class:`RuntimeError` if ``manifest.json`` is missing/invalid,
    declares 0 layers, references a missing layer, or contains an unparseable
    layer archive.
    """
    members_by_name: dict[str, tarfile.TarInfo] = {}
    for member in outer.getmembers():
        norm = posixpath.normpath(member.name.lstrip("./"))
        members_by_name[norm] = member

    manifest_member = _resolve_tar_member(members_by_name, "manifest.json")
    if manifest_member is None:
        raise RuntimeError("docker save archive is missing manifest.json")
    manifest_fp = outer.extractfile(manifest_member)
    if manifest_fp is None:
        raise RuntimeError("docker save manifest.json is not a regular file")
    try:
        manifest = json.loads(manifest_fp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Failed to parse docker save manifest.json: {exc}") from exc

    if not isinstance(manifest, list) or not manifest:
        raise RuntimeError("docker save manifest.json must be a non-empty list")

    declared_layers: list[str] = []
    for entry in manifest:
        if not isinstance(entry, dict):
            raise RuntimeError("Invalid non-object entry in docker save manifest.json")
        layers = entry.get("Layers")
        if not isinstance(layers, list):
            raise RuntimeError("docker save manifest entry is missing Layers list")
        for layer in layers:
            if not isinstance(layer, str) or not layer.strip():
                raise RuntimeError(f"Invalid layer path in manifest.json: {layer!r}")
            declared_layers.append(layer)

    if not declared_layers:
        raise RuntimeError("docker save manifest.json declared 0 layers")

    scanned = 0
    for layer_name in declared_layers:
        layer_member = _resolve_tar_member(members_by_name, layer_name)
        if layer_member is None:
            raise RuntimeError(
                f"Declared layer {layer_name!r} in manifest.json is missing from archive"
            )
        extracted = outer.extractfile(layer_member)
        if extracted is None:
            raise RuntimeError(
                f"Declared layer {layer_name!r} could not be extracted from archive"
            )
        try:
            with tarfile.open(fileobj=extracted, mode="r|*") as layer_tar:
                for lmember in layer_tar:
                    visitor(layer_name, layer_tar, lmember)
        except (tarfile.TarError, EOFError, OSError) as exc:
            raise RuntimeError(
                f"Failed to parse declared layer {layer_name!r}: {exc}"
            ) from exc
        scanned += 1

    if scanned == 0:
        raise RuntimeError("Scanned 0 image layers")
    return scanned


def visit_docker_save_tar(
    tar_path: Path,
    visitor: Callable[[str, tarfile.TarFile, tarfile.TarInfo], None],
) -> int:
    """Visit every member across all ``manifest.json`` layers of an on-disk ``docker save`` tar."""
    with tarfile.open(tar_path, mode="r:*") as outer:
        return _visit_opened_docker_save_tar(outer, visitor)


def _scan_opened_docker_save_tar(
    outer: tarfile.TarFile,
    forbidden_substrings: Sequence[str],
) -> tuple[list[str], int]:
    """Read ``manifest.json`` and inspect every declared layer strictly."""
    hits: list[str] = []

    def _check_member(
        layer_name: str,
        _layer_tar: tarfile.TarFile,
        lmember: tarfile.TarInfo,
    ) -> None:
        norm = lmember.name.lstrip("./")
        for forbidden in forbidden_substrings:
            if forbidden in norm:
                hits.append(f"{layer_name}:{norm}")

    scanned = _visit_opened_docker_save_tar(outer, _check_member)
    return sorted(set(hits)), scanned


def scan_tar_file_for_paths(
    tar_path: Path,
    forbidden_substrings: Sequence[str],
) -> tuple[list[str], int]:
    """Scan an on-disk ``docker save`` archive strictly by ``manifest.json`` layers."""
    with tarfile.open(tar_path, mode="r:*") as outer:
        return _scan_opened_docker_save_tar(outer, forbidden_substrings)


def scan_tar_stream_for_paths(
    tar_bytes: bytes,
    forbidden_substrings: Sequence[str],
) -> list[str]:
    """Scan an in-memory ``docker save`` tar archive strictly by ``manifest.json`` layers."""
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:*") as outer:
        hits, _ = _scan_opened_docker_save_tar(outer, forbidden_substrings)
        return hits


class ComposeContext:
    """Context object representing the running standalone core Docker Compose stack."""

    def __init__(
        self,
        *,
        work_dir: Path,
        compose_files: Sequence[Path],
        artifacts_dir: Path,
        edge_url: str = "http://127.0.0.1:8088",
        project_name: str | None = None,
    ) -> None:
        self.work_dir = work_dir.resolve()
        self.compose_files = [Path(p).resolve() for p in compose_files]
        self.artifacts_dir = artifacts_dir.resolve()
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self.edge_url = edge_url.rstrip("/")
        self.project_name = (project_name or "").strip() or None
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
        project_name = os.environ.get("COMPOSE_PROJECT_NAME") or None
        return cls(
            work_dir=work_dir,
            compose_files=compose_files,
            artifacts_dir=artifacts_dir,
            edge_url=edge_url,
            project_name=project_name,
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
        cmd = ["docker", "compose"]
        if self.project_name:
            cmd.extend(["-p", self.project_name])
        cmd.extend(["--project-directory", str(self.work_dir)])
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
            stderr = _redact_secrets_text(
                (proc.stderr or b"").decode("utf-8", errors="replace"), self.env
            )
            stdout = _redact_secrets_text(
                (proc.stdout or b"").decode("utf-8", errors="replace"), self.env
            )
            subcmd = " ".join(args[:2]) if args else "<empty>"
            raise RuntimeError(
                f"docker compose {subcmd} failed (rc={proc.returncode}, args={len(args)})\n"
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

    @contextmanager
    def saved_service_image_tar(self, service: str) -> Iterator[Path]:
        """Run ``docker save -o <tmp.tar>`` on a service's image and yield the tar path."""
        raw_ids = [
            line.strip()
            for line in self.compose("images", "-q", service).stdout.decode("utf-8").splitlines()
            if line.strip()
        ]
        if not raw_ids:
            raise RuntimeError(f"No image ID returned by `docker compose images -q {service}`")
        image_id = raw_ids[0]
        # `compose images -q` prints the bare 64-hex ID; `docker save` rejects that
        # form as a repository name and needs the digest-qualified `sha256:` form.
        if len(image_id) == 64 and all(c in "0123456789abcdef" for c in image_id):
            image_id = f"sha256:{image_id}"
        with tempfile.TemporaryDirectory(prefix=f"scan-{service}-") as tmpdir:
            tar_path = Path(tmpdir) / f"{service}.tar"
            proc = subprocess.run(
                ["docker", "save", "-o", str(tar_path), image_id],
                capture_output=True,
                check=False,
            )
            if proc.returncode != 0:
                stderr = _redact_secrets_text(
                    (proc.stderr or b"").decode("utf-8", errors="replace"), self.env
                )
                raise RuntimeError(
                    f"docker save failed for service {service} (rc={proc.returncode}): {stderr[-1000:]}"
                )
            yield tar_path

    def scan_service_image_layers(
        self,
        service: str,
        forbidden_substrings: Sequence[str],
    ) -> tuple[list[str], int]:
        """Run ``docker save -o <tmp.tar>`` on a service's image and scan every manifest layer."""
        with self.saved_service_image_tar(service) as tar_path:
            return scan_tar_file_for_paths(tar_path, forbidden_substrings)


FAKE_PROVIDER_NAME = "fake-openai"
FAKE_MODEL_NAME = "fake-gpt-6-luna"
FAKE_BASE_URL = "http://fake-llm:8099/v1"
FAKE_API_KEY = "sk-fake-standalone-ci-0001"


def configure_fake_llm_model(
    ctx: ComposeContext,
    *,
    provider_name: str = FAKE_PROVIDER_NAME,
    model_name: str = FAKE_MODEL_NAME,
    model_id: str | None = None,
    base_url: str = FAKE_BASE_URL,
    api_key: str = FAKE_API_KEY,
    headers: Mapping[str, str] | None = None,
) -> str:
    """Register the CI fake OpenAI Responses API provider/model via the HTTP API.

    Idempotent across check scripts sharing the same Compose stack (e.g. ``b1`` and ``b6``)
    and self-contained when a check script is invoked on a fresh stack.
    """
    resolved_model_id = model_id or os.environ.get("FAKE_LLM_MODEL", "gpt-6-luna")
    req_headers = dict(headers) if headers is not None else oss_owner_headers(ctx)
    pref_resp = http_request(
        "PUT",
        f"{ctx.edge_url}/api/v1/users/me/preferences",
        headers=req_headers,
        json_body={
            "model_preference": {
                "custom_providers": [
                    {
                        "name": provider_name,
                        "parent_provider": "openai",
                        "use_response_api": True,
                    }
                ],
                "custom_models": [
                    {
                        "name": model_name,
                        "model_id": resolved_model_id,
                        "provider": provider_name,
                        "input_modalities": ["text"],
                    }
                ],
                "preferred_model": model_name,
            }
        },
    )
    assert pref_resp.status == 200, (
        f"Failed to save fake provider/model preferences: {pref_resp.status} {pref_resp.text}"
    )

    key_resp = http_request(
        "PUT",
        f"{ctx.edge_url}/api/v1/users/me/api-keys",
        headers=req_headers,
        json_body={
            "byok_enabled": True,
            "api_keys": {provider_name: api_key},
            "base_urls": {provider_name: base_url},
        },
    )
    assert key_resp.status == 200, (
        f"Failed to save fake provider API key: {key_resp.status} {key_resp.text}"
    )

    test_key_resp = http_request(
        "POST",
        f"{ctx.edge_url}/api/v1/keys/test",
        headers=req_headers,
        json_body={
            "provider": provider_name,
            "api_key": api_key,
            "base_url": base_url,
        },
    )
    assert test_key_resp.status == 200, (
        f"POST /api/v1/keys/test against fake-llm failed: "
        f"{test_key_resp.status} {test_key_resp.text}"
    )
    return model_name
