"""Bounded stdin for run_json_command, and the embedding child process.

Children are local interpreters. No test calls an embedding API or reads a
production credential.
"""

from __future__ import annotations

import inspect
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton import market_digest_inputs as inputs  # noqa: E402

EMBED_SCRIPT = REPO_ROOT / "scripts" / "dalton" / "market_digest_embed.py"
MAX_STDIN = 2 * 1024 * 1024

_FAKE_TYPES = """
class HttpRetryOptions:
    def __init__(self, attempts):
        self.attempts = attempts


class HttpOptions:
    def __init__(self, timeout, retry_options):
        self.timeout = timeout
        self.retry_options = retry_options
"""

_FAKE_GENAI = """
import json
import os
from pathlib import Path


def _path():
    raw = os.environ.get("EMBED_CAPTURE", "")
    return Path(raw) if raw else None


def _load():
    path = _path()
    if path is None or not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _store(payload):
    path = _path()
    if path is not None:
        path.write_text(json.dumps(payload), encoding="utf-8")


_store({
    "imported": True,
    "parent_secret": os.environ.get("SECRET_PARENT", ""),
    "calls": [],
})


class _Emb:
    def __init__(self, values):
        self.values = values


class _Models:
    def embed_content(self, *, model, contents, config):
        payload = _load()
        payload.setdefault("calls", []).append({
            "model": model,
            "contents": list(contents),
            "config": config,
        })
        _store(payload)
        mode = os.environ.get("EMBED_MODE", "")
        if mode == "nan":
            embeddings = [_Emb([float("nan")])]
        elif mode == "short":
            embeddings = []
        else:
            embeddings = [_Emb([1.0, float(len(text))]) for text in contents]
        return type("Response", (), {"embeddings": embeddings})()


class Client:
    def __init__(self, *, api_key, http_options):
        payload = _load()
        payload["api_key"] = api_key
        payload["timeout"] = getattr(http_options, "timeout", None)
        retry = getattr(http_options, "retry_options", None)
        payload["attempts"] = getattr(retry, "attempts", None)
        _store(payload)
        self.models = _Models()
"""


def test_input_bytes_is_optional_on_the_runner() -> None:
    signature = inspect.signature(inputs.run_json_command)
    parameter = signature.parameters["input_bytes"]
    assert parameter.default is None
    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    protocol = inspect.signature(inputs.JsonCommandRunner.__call__)
    assert protocol.parameters["input_bytes"].default is None
    assert inputs.MAX_STDIN_BYTES == MAX_STDIN


def _env() -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _invoke(
    argv: list[str],
    *,
    timeout_seconds: float,
    stdout_limit_bytes: int,
    stderr_limit_bytes: int,
    input_bytes: bytes | None = None,
    join_seconds: float = 20,
) -> inputs.ChildResult:
    box: dict[str, object] = {}
    pids: list[int] = []

    def invoke() -> None:
        try:
            box["result"] = inputs.run_json_command(
                argv,
                env=_env(),
                timeout_seconds=timeout_seconds,
                stdout_limit_bytes=stdout_limit_bytes,
                stderr_limit_bytes=stderr_limit_bytes,
                input_bytes=input_bytes,
                on_spawn=pids.append,
            )
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=invoke, daemon=True)
    thread.start()
    thread.join(join_seconds)
    if thread.is_alive():
        if pids:
            try:
                os.killpg(pids[0], signal.SIGKILL)
            except OSError:
                pass
        pytest.fail("run_json_command did not return")
    error = box.get("error")
    if isinstance(error, Exception):
        raise error
    result = box.get("result")
    if not isinstance(result, inputs.ChildResult):
        pytest.fail("run_json_command returned nothing")
    return result


def _script(tmp_path: Path, name: str, source: str) -> Path:
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _alive(name: str) -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == name and thread.is_alive()]


def _running(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    completed = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    status = completed.stdout.strip()
    return bool(status) and not status.startswith("Z")


def test_stdin_none_empty_and_payload(tmp_path: Path) -> None:
    probe = _script(
        tmp_path,
        "probe.py",
        "\n".join(
            [
                "import os, stat, sys",
                "mode = os.fstat(0).st_mode",
                "kind = 'chr' if stat.S_ISCHR(mode) else 'fifo' if stat.S_ISFIFO(mode) else 'other'",
                "data = sys.stdin.buffer.read()",
                "sys.stdout.buffer.write(kind.encode('ascii') + b':' + data)",
            ]
        )
        + "\n",
    )
    argv = [sys.executable, str(probe)]
    untouched = _invoke(
        argv,
        timeout_seconds=10,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=0,
    )
    assert untouched.returncode == 0
    assert untouched.stdout == "chr:"
    assert untouched.timed_out is False
    empty = _invoke(
        argv,
        timeout_seconds=10,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=0,
        input_bytes=b"",
    )
    assert empty.returncode == 0
    assert empty.stdout == "fifo:"
    payload = "中文-payload".encode("utf-8")
    echoed = _invoke(
        argv,
        timeout_seconds=10,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=0,
        input_bytes=payload,
    )
    assert echoed.returncode == 0
    assert echoed.stdout == "fifo:" + payload.decode("utf-8")
    assert _alive("market-digest-stdin") == []


def test_stdin_limit_is_rejected_before_spawn(tmp_path: Path) -> None:
    marker = tmp_path / "spawned"
    script = _script(
        tmp_path,
        "marker.py",
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran', encoding='utf-8')\n",
    )
    argv = [sys.executable, str(script)]
    secret = b"SECRET-STDIN" + (b"x" * MAX_STDIN)
    with pytest.raises(inputs.PrepareConfigError, match="2MiB") as oversized:
        _invoke(
            argv,
            timeout_seconds=5,
            stdout_limit_bytes=64,
            stderr_limit_bytes=0,
            input_bytes=secret,
            join_seconds=5,
        )
    assert "SECRET-STDIN" not in str(oversized.value)
    assert not marker.exists()
    bad_input: Any = bytearray(b"SECRET-STDIN")
    with pytest.raises(inputs.PrepareConfigError, match="2MiB") as rejected:
        inputs.run_json_command(
            argv,
            env=_env(),
            timeout_seconds=5,
            stdout_limit_bytes=64,
            stderr_limit_bytes=0,
            input_bytes=bad_input,
        )
    assert "SECRET-STDIN" not in str(rejected.value)
    assert not marker.exists()
    exact = _script(
        tmp_path,
        "count.py",
        "import sys\nsys.stdout.write(str(len(sys.stdin.buffer.read())))\n",
    )
    counted = _invoke(
        [sys.executable, str(exact)],
        timeout_seconds=15,
        stdout_limit_bytes=64,
        stderr_limit_bytes=0,
        input_bytes=b"y" * MAX_STDIN,
    )
    assert counted.returncode == 0
    assert counted.stdout == str(MAX_STDIN)
    assert counted.timed_out is False


def test_large_stdout_does_not_deadlock_a_large_stdin(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "both.py",
        "\n".join(
            [
                "import sys",
                "sys.stdout.buffer.write(b'O' * (1024 * 1024))",
                "sys.stdout.buffer.flush()",
                "data = sys.stdin.buffer.read()",
                "sys.stdout.buffer.write(str(len(data)).encode('ascii'))",
                "sys.stdout.buffer.flush()",
            ]
        )
        + "\n",
    )
    payload = b"I" * (256 * 1024)
    result = _invoke(
        [sys.executable, str(script)],
        timeout_seconds=10,
        stdout_limit_bytes=2 * 1024 * 1024,
        stderr_limit_bytes=0,
        input_bytes=payload,
        join_seconds=15,
    )
    assert result.timed_out is False
    assert result.returncode == 0
    assert result.stdout_truncated is False
    assert result.stdout.startswith("O" * (1024 * 1024))
    assert result.stdout.endswith(str(len(payload)))
    assert _alive("market-digest-stdin") == []


def test_timeout_reaps_a_grandchild_and_the_stdin_writer(tmp_path: Path) -> None:
    pid_path = tmp_path / "grandchild.pid"
    ready = tmp_path / "grandchild.ready"
    grandchild = _script(
        tmp_path,
        "grandchild.py",
        "\n".join(
            [
                "import signal, sys, time",
                "from pathlib import Path",
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                "Path(sys.argv[1]).write_text('up', encoding='utf-8')",
                "time.sleep(30)",
            ]
        )
        + "\n",
    )
    script = _script(
        tmp_path,
        "ignore.py",
        "\n".join(
            [
                "import signal, subprocess, sys, time",
                "from pathlib import Path",
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                f"child = subprocess.Popen([{sys.executable!r}, {str(grandchild)!r}, {str(ready)!r}])",
                f"Path({str(pid_path)!r}).write_text(str(child.pid), encoding='utf-8')",
                "deadline = time.monotonic() + 3",
                f"while not Path({str(ready)!r}).is_file():",
                "    if time.monotonic() >= deadline:",
                "        break",
                "    time.sleep(0.01)",
                "time.sleep(30)",
            ]
        )
        + "\n",
    )
    result = _invoke(
        [sys.executable, str(script)],
        timeout_seconds=5,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=0,
        input_bytes=b"B" * (256 * 1024),
        join_seconds=12,
    )
    assert result.timed_out is True
    assert result.still_running is False
    assert ready.is_file()
    assert pid_path.is_file()
    grandchild_pid = int(pid_path.read_text(encoding="utf-8"))
    deadline = time.monotonic() + 2
    while _running(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _running(grandchild_pid)
    assert _alive("market-digest-stdin") == []


def test_stdio_limits_keep_stdout_and_drop_stderr_text(tmp_path: Path) -> None:
    script = _script(
        tmp_path,
        "limits.py",
        "\n".join(
            [
                "import sys",
                "sys.stdout.write('Y' * 5000)",
                "sys.stdout.flush()",
                "sys.stderr.write('SECRET-STDERR' + ('E' * 1000))",
                "sys.stderr.flush()",
                "sys.stdin.buffer.read()",
            ]
        )
        + "\n",
    )
    result = _invoke(
        [sys.executable, str(script)],
        timeout_seconds=10,
        stdout_limit_bytes=128,
        stderr_limit_bytes=0,
        input_bytes=b"z",
    )
    assert result.stdout_truncated is True
    assert len(result.stdout) == 128
    assert result.stderr_byte_count > 1000
    assert result.stderr_truncated is True
    assert "SECRET-STDERR" not in repr(result)
    assert result.timed_out is False


def _fake_genai(root: Path) -> None:
    package = root / "google" / "genai"
    package.mkdir(parents=True)
    (root / "google" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text(_FAKE_GENAI, encoding="utf-8")
    (package / "types.py").write_text(_FAKE_TYPES, encoding="utf-8")


def _child_env(fake: Path, capture: Path, **extra: str) -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(fake),
        "EMBED_CAPTURE": str(capture),
    }
    env.update(extra)
    return env


def _embed(
    payload: bytes,
    env: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(EMBED_SCRIPT)],
        input=payload,
        capture_output=True,
        env=env,
        timeout=15,
        check=False,
    )


def _code(proc: subprocess.CompletedProcess[bytes]) -> str:
    loaded = json.loads(proc.stdout.decode("utf-8"))
    assert loaded["ok"] is False
    return str(loaded["code"])


def test_embed_import_does_not_load_the_sdk() -> None:
    probe = (
        "import importlib.util, sys\n"
        "spec = importlib.util.spec_from_file_location('market_digest_embed_probe', sys.argv[1])\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "sys.stdout.write('LOADED' if 'google.genai' in sys.modules else 'CLEAN')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe, str(EMBED_SCRIPT)],
        capture_output=True,
        env=_env(),
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0
    assert completed.stdout == b"CLEAN"


def test_embed_child_uses_one_sdk_attempt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = tmp_path / "fake"
    _fake_genai(fake)
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("GEMINI_API_KEY", "inherited-process-key")
    monkeypatch.setenv("SECRET_PARENT", "super-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    env = _child_env(fake, capture, GEMINI_API_KEY="test-gemini-key")
    assert "SECRET_PARENT" not in env
    payload = json.dumps(
        {"texts": ["甲", "乙"], "model": "gemini-embedding-001"},
        ensure_ascii=False,
    ).encode("utf-8")
    completed = _embed(payload, env)
    assert completed.returncode == 0
    assert completed.stderr == b""
    assert b"test-gemini-key" not in completed.stdout
    assert b"inherited-process-key" not in completed.stdout
    body = json.loads(completed.stdout.decode("utf-8"))
    assert body == {"ok": True, "embeddings": [[1.0, 1.0], [1.0, 1.0]]}
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    assert recorded["api_key"] == "test-gemini-key"
    assert recorded["timeout"] == 60000
    assert recorded["attempts"] == 1
    assert recorded["parent_secret"] == ""
    assert recorded["calls"] == [
        {
            "model": "gemini-embedding-001",
            "contents": ["甲", "乙"],
            "config": {"task_type": "RETRIEVAL_DOCUMENT"},
        }
    ]


def test_embed_child_rejects_bad_requests_before_import(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    fake = tmp_path / "fake"
    _fake_genai(fake)
    capture = tmp_path / "capture.json"
    monkeypatch.setenv("GEMINI_API_KEY", "inherited-process-key")
    monkeypatch.setenv("SECRET_PARENT", "super-secret")
    env = _child_env(fake, capture)
    cases = [
        (json.dumps({"texts": ["a"], "model": "m", "extra": 1}).encode("utf-8"), "invalid_request"),
        (json.dumps({"texts": ["a"] * 101, "model": "gemini-embedding-001"}).encode("utf-8"), "invalid_texts"),
        (json.dumps({"texts": ["a"], "model": "gemini-embedding-001"}).encode("utf-8"), "embedding_not_configured"),
        (b"{" * (MAX_STDIN + 1), "invalid_request"),
    ]
    for payload, code in cases:
        capture.unlink(missing_ok=True)
        completed = _embed(payload, env)
        assert completed.returncode != 0
        assert completed.stderr == b""
        assert _code(completed) == code
        assert not capture.exists()
        assert b"inherited-process-key" not in completed.stdout
        assert b"super-secret" not in completed.stdout


def test_embed_child_hides_nonfinite_and_short_vectors(tmp_path: Path) -> None:
    fake = tmp_path / "fake"
    _fake_genai(fake)
    capture = tmp_path / "capture.json"
    payload = json.dumps({"texts": ["a"], "model": "gemini-embedding-001"}).encode("utf-8")
    for mode in ("nan", "short"):
        capture.unlink(missing_ok=True)
        env = _child_env(fake, capture, GEMINI_API_KEY="test-gemini-key", EMBED_MODE=mode)
        completed = _embed(payload, env)
        assert completed.returncode != 0
        assert completed.stderr == b""
        assert _code(completed) == "embed_failed"
        assert b"NaN" not in completed.stdout
        assert b"nan" not in completed.stdout.lower()
        recorded = json.loads(capture.read_text(encoding="utf-8"))
        assert len(recorded["calls"]) == 1
