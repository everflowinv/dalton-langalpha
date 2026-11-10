"""Unit tests for the market-digest input preparation stage.

Runners are signature-correct substitutes. No test fetches mail, starts a
service, or reads a production credential store.
"""

from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from unittest.mock import create_autospec, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.dalton import market_digest_inputs as mod

DATE = "2026-11-10"
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=mod.HKT)
BODY = "BODY-TOKEN-UNIQUE"
STDOUT_ONLY = "STDOUT-ONLY-TOKEN"
FOREIGN = "FOREIGN-SECRET-CONTENT"
DOTENV_MARKER = "dotenv-secret-marker"
GEMINI_VALUE = "gemini-secret-value"
OPENAI_VALUE = "openai-secret-value"
FEISHU_VALUE = "feishu-secret-value"
BASH_VALUE = "/tmp/bash-env-secret-marker"
PYTHONSTARTUP_VALUE = "/tmp/pythonstartup-secret-marker"


def _dead_pid() -> int:
    for candidate in (2**30 - 1, 2**22 - 1, 999_999):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except OSError:
            continue
    return 2**30 - 1


_DEAD_PID = _dead_pid()


class Layout:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.skill = (root / "skill").resolve()
        self.output = self.skill / "output"
        self.workspace = (root / "ws").resolve()
        self.tmtb_script = self.workspace / "scripts" / "tmtb_alphaengine_sync.py"
        self.tmtb_output = self.workspace / "state" / "tmtb-alphaengine"
        self.state = (root / "state").resolve()
        self.python = Path(sys.executable).resolve()
        self.repo = REPO_ROOT.resolve()
        (self.skill / "scripts").mkdir(parents=True)
        (self.skill / "scripts" / "market_digest_cli.py").write_text(
            "raise SystemExit('digest stub must not run')\n",
            encoding="utf-8",
        )
        self.output.mkdir()
        self.tmtb_script.parent.mkdir(parents=True)
        self.tmtb_script.write_text(
            "raise SystemExit('tmtb stub must not run')\n",
            encoding="utf-8",
        )
        self.tmtb_output.mkdir(parents=True)
        self.state.mkdir()

    def config(self, **overrides: Any) -> mod.PrepareConfig:
        values: dict[str, Any] = {
            "state_root": self.state,
            "skill_dir": self.skill,
            "tmtb_script": self.tmtb_script,
            "python_executable": self.python,
            "repo_root": self.repo,
        }
        values.update(overrides)
        return mod.PrepareConfig(**values)

    def digest_path(self, date_value: str = DATE, period: str = "AM") -> Path:
        return self.output / f"digest_{date_value}_{period}.json"

    def tmtb_path(self, date_value: str = DATE, period: str = "AM") -> Path:
        return self.tmtb_output / f"tmtb_{date_value}_{period}.json"

    def attempt_path(self, date_value: str = DATE, period: str = "AM") -> Path:
        return self.state / "attempts" / period / f"{date_value}.json"


def _layout(tmp_path: Path) -> Layout:
    return Layout(tmp_path)


def _mail(body: str = BODY, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": "m1",
        "from": "Desk <desk@example.com>",
        "subject": "Morning note",
        "date": DATE,
        "is_priority": False,
        "body": body,
        "source": "sales-notes",
        "research_eligible": True,
    }
    item.update(extra)
    return item


def _digest_document(
    emails: list[dict[str, Any]],
    *,
    date_value: str = DATE,
    period: str = "AM",
    status: str = "emails_fetched",
    funda: dict[str, Any] | None = None,
    images: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "status": status,
        "date": date_value,
        "period": period,
        "count": len(emails),
        "emails": emails,
        "funda_ingestion": funda or {"ok": True, "status": "empty", "received": 0},
        "image_ingestion": images or {"ok": True, "status": "ok", "sources": {}},
    }


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _child(
    stdout: str = "",
    *,
    returncode: int | None = 0,
    timed_out: bool = False,
    still_running: bool = False,
    truncated: bool = False,
    pid: int | None = None,
) -> mod.ChildResult:
    return mod.ChildResult(
        returncode=returncode,
        stdout=stdout,
        stdout_truncated=truncated,
        stderr_byte_count=0,
        stderr_truncated=False,
        timed_out=timed_out,
        pid=pid if pid is not None else _DEAD_PID,
        still_running=still_running,
    )


def _digest_stdout(
    *,
    date_value: str = DATE,
    period: str = "AM",
    status: str = "emails_fetched",
    output_file: str | None = None,
    count: int = 1,
    extra: dict[str, Any] | None = None,
) -> str:
    payload: dict[str, Any] = {
        "ok": True,
        "status": status,
        "date": date_value,
        "period": period,
        "count": count,
        "stdout_only": STDOUT_ONLY,
        "output_file": output_file,
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload)


def _tmtb_stdout(
    *,
    date_value: str = DATE,
    period: str = "AM",
    status: str = "new_docs",
    output_file: str | None = None,
    ok: bool = True,
    count: int = 0,
    new_count: int = 0,
    pending_count: int = 0,
    quota_hit: bool = False,
    failures: list[Any] | None = None,
) -> str:
    return json.dumps(
        {
            "ok": ok,
            "status": status,
            "date": date_value,
            "period": period,
            "output_file": output_file,
            "count": count,
            "new_count": new_count,
            "pending_count": pending_count,
            "quota_hit": quota_hit,
            "failures": failures or [],
            "titles": ["title-only"],
        }
    )


def _tmtb_document(
    docs: list[dict[str, Any]],
    pending: list[Any] | None = None,
    *,
    date_value: str = DATE,
    period: str = "AM",
) -> dict[str, Any]:
    return {
        "date": date_value,
        "period": period,
        "window": ["2026-11-03", date_value],
        "docs": docs,
        "pending_snippets": pending or [],
    }


class RecordingRunner:
    """Stand-in for ``run_json_command`` with the same keyword-only signature."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.digest: Callable[[list[str]], mod.ChildResult] | None = None
        self.tmtb: Callable[[list[str]], mod.ChildResult] | None = None
        self.before_spawn: Callable[[], None] | None = None
        self.after_spawn: Callable[[], None] | None = None
        self._guard = threading.Lock()

    def __call__(
        self,
        argv: list[str],
        *,
        env: Mapping[str, str],
        timeout_seconds: float,
        stdout_limit_bytes: int,
        stderr_limit_bytes: int,
        on_spawn: Callable[[int], None] | None = None,
    ) -> mod.ChildResult:
        kind = "tmtb" if "sync" in argv else "digest" if "digest" in argv else "other"
        with self._guard:
            self.calls.append(
                {
                    "argv": list(argv),
                    "env": dict(env),
                    "timeout_seconds": timeout_seconds,
                    "stdout_limit_bytes": stdout_limit_bytes,
                    "stderr_limit_bytes": stderr_limit_bytes,
                    "kind": kind,
                }
            )
        if kind == "digest" and self.before_spawn is not None:
            self.before_spawn()
        if on_spawn is not None:
            on_spawn(_DEAD_PID)
        if kind == "digest" and self.after_spawn is not None:
            self.after_spawn()
        if kind == "digest" and self.digest is not None:
            return self.digest(argv)
        if kind == "tmtb" and self.tmtb is not None:
            return self.tmtb(argv)
        return _child("{}")


def _spec_runner(recorder: RecordingRunner) -> Any:
    return create_autospec(mod.run_json_command, side_effect=recorder)


def _static_env(
    base_env: Mapping[str, str] | None = None,
    *,
    extra_env: Mapping[str, str] | None = None,
    drop_keys: Any = (),
    allow_keys: Any = (),
    repo_root: Path | None = None,
) -> dict[str, str]:
    del base_env, extra_env, drop_keys, allow_keys, repo_root
    return {"PATH": "/usr/bin:/bin", "GEMINI_API_KEY": GEMINI_VALUE}


def _spec_env(fn: Callable[..., dict[str, str]] | None = None) -> Any:
    return create_autospec(mod.build_nonarchive_subprocess_env, side_effect=fn or _static_env)


def _prepare(
    layout: Layout,
    runner: RecordingRunner,
    now: datetime = NOW,
    period: str = "AM",
    *,
    env_builder: Any = None,
    config: mod.PrepareConfig | None = None,
) -> dict[str, Any]:
    return mod.prepare_inputs(
        config or layout.config(),
        period,
        now=now,
        runner=_spec_runner(runner),
        env_builder=env_builder or _spec_env(),
    )


def _kinds(runner: RecordingRunner) -> list[str]:
    return [str(item["kind"]) for item in runner.calls]


def _assert_child_argv(runner: RecordingRunner, layout: Layout, *, date_value: str, period: str) -> None:
    digests = [item["argv"] for item in runner.calls if item["kind"] == "digest"]
    tmtbs = [item["argv"] for item in runner.calls if item["kind"] == "tmtb"]
    assert digests == [
        [
            str(layout.python),
            str((layout.skill / "scripts" / "market_digest_cli.py").resolve()),
            "--json",
            "digest",
            "--period",
            period,
        ]
    ]
    assert tmtbs == [
        [
            str(layout.python),
            str(layout.tmtb_script.resolve()),
            "--json",
            "sync",
            "--date",
            date_value,
            "--period",
            period,
        ]
    ]
    flat = " ".join(" ".join(item["argv"]) for item in runner.calls)
    for banned in ("gws", "label", "modify", "addLabelIds", "lookback", "shell"):
        assert banned not in flat


def _tree_text(root: Path) -> str:
    parts: list[str] = []
    if not root.exists():
        return ""
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        parts.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(parts)


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def _successful_pair(
    layout: Layout,
    runner: RecordingRunner,
    *,
    period: str = "AM",
    date_value: str = DATE,
) -> None:
    document = _digest_document([_mail()], date_value=date_value, period=period)
    tmtb_doc = _tmtb_document(
        [{"doc_id": "d1", "title": "Note", "text": "TMTB-FULL-TEXT"}],
        date_value=date_value,
        period=period,
    )
    digest_path = layout.digest_path(date_value, period)
    tmtb_path = layout.tmtb_path(date_value, period)

    def digest(_argv: list[str]) -> mod.ChildResult:
        _write_json(digest_path, document)
        return _child(_digest_stdout(date_value=date_value, period=period, output_file=str(digest_path), count=1))

    def tmtb(_argv: list[str]) -> mod.ChildResult:
        _write_json(tmtb_path, tmtb_doc)
        return _child(
            _tmtb_stdout(
                date_value=date_value,
                period=period,
                status="new_docs",
                output_file=str(tmtb_path),
                count=1,
                new_count=1,
            )
        )

    runner.digest = digest
    runner.tmtb = tmtb


def test_signatures_match_the_real_contracts() -> None:
    inspect.signature(mod.run_json_command).bind(
        [str(Path(sys.executable).resolve()), "/tmp/market_digest_inputs_unused.py"],
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=1.0,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=0,
        on_spawn=None,
    )
    inspect.signature(mod.build_nonarchive_subprocess_env).bind(
        None,
        allow_keys=("GEMINI_API_KEY",),
        repo_root=REPO_ROOT,
    )
    inspect.signature(mod.prepare_inputs).bind(
        mod.PrepareConfig(
            state_root=Path("/tmp/state"),
            skill_dir=Path("/tmp/skill"),
            tmtb_script=Path("/tmp/tmtb.py"),
            python_executable=Path(sys.executable),
            repo_root=REPO_ROOT,
        ),
        "AM",
        now=NOW,
        runner=mod.run_json_command,
        env_builder=mod.build_nonarchive_subprocess_env,
    )
    read_params = inspect.signature(mod.read_inputs).parameters
    assert "path" not in read_params
    assert "output_file" not in read_params
    assert mod.DEFAULT_READ_MAX_BYTES == 32 * 1024 * 1024
    assert mod.HARD_READ_MAX_BYTES == 64 * 1024 * 1024
    source = Path(mod.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in source
    assert '"method": "tools/call"' not in source
    assert ".openclaw" not in source


def test_concurrent_threads_fetch_digest_once(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    gate = threading.Event()

    def digest(_argv: list[str]) -> mod.ChildResult:
        gate.set()
        time.sleep(0.2)
        _write_json(layout.digest_path(), _digest_document([_mail()]))
        return _child(_digest_stdout(output_file=str(layout.digest_path())))

    def tmtb(_argv: list[str]) -> mod.ChildResult:
        _write_json(layout.tmtb_path(), _tmtb_document([{"doc_id": "d1", "text": "TMTB-FULL-TEXT"}]))
        return _child(_tmtb_stdout(status="new_docs", output_file=str(layout.tmtb_path()), count=1, new_count=1))

    runner.digest = digest
    runner.tmtb = tmtb
    results: list[dict[str, Any]] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            results.append(_prepare(layout, runner))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work), threading.Thread(target=work)]
    for thread in threads:
        thread.start()
    assert gate.wait(5)
    for thread in threads:
        thread.join(10)
        assert not thread.is_alive()
    assert errors == []
    assert _kinds(runner).count("digest") == 1
    assert _kinds(runner).count("tmtb") == 1
    invoked = [item for item in results if item["digest_invoked"]]
    reused = [item for item in results if item["reused"]]
    assert len(invoked) == 1 and len(reused) == 1
    assert invoked[0]["hashes"]["input_sha256"] == reused[0]["hashes"]["input_sha256"]
    assert invoked[0]["preparation_status"] == "ready"
    _assert_child_argv(runner, layout, date_value=DATE, period="AM")


def test_concurrent_processes_fetch_digest_once(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    worker = tmp_path / "prepare_worker.py"
    worker.write_text(
        "\n".join(
            [
                "import json, os, sys, time",
                "from pathlib import Path",
                "sys.path.insert(0, os.environ['MARKET_DIGEST_REPO_ROOT'])",
                "from scripts.dalton import market_digest_inputs as mod",
                "job = json.loads(Path(os.environ['MARKET_DIGEST_JOB']).read_text(encoding='utf-8'))",
                "log_path = Path(job['log'])",
                "def _log(line):",
                "    fd = os.open(log_path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)",
                "    try:",
                "        import fcntl",
                "        fcntl.flock(fd, fcntl.LOCK_EX)",
                "        os.write(fd, (line + '\\n').encode())",
                "    finally:",
                "        os.close(fd)",
                "def runner(argv, *, env, timeout_seconds, stdout_limit_bytes, stderr_limit_bytes, on_spawn=None):",
                "    kind = 'digest' if 'digest' in argv else 'tmtb'",
                "    _log(kind)",
                "    if on_spawn is not None:",
                "        on_spawn(2**30 - 1)",
                "    if kind == 'digest':",
                "        time.sleep(0.4)",
                "        Path(job['digest']).write_text(job['digest_text'], encoding='utf-8')",
                "        return mod.ChildResult(0, job['digest_stdout'], False, 0, False, False, 2**30 - 1, False)",
                "    Path(job['tmtb']).write_text(job['tmtb_text'], encoding='utf-8')",
                "    return mod.ChildResult(0, job['tmtb_stdout'], False, 0, False, False, 2**30 - 1, False)",
                "def env_builder(base_env=None, *, extra_env=None, drop_keys=(), allow_keys=(), repo_root=None):",
                "    return {'PATH': '/usr/bin:/bin'}",
                "config = mod.PrepareConfig(",
                "    state_root=Path(job['state']), skill_dir=Path(job['skill']),",
                "    tmtb_script=Path(job['tmtb_script']), python_executable=Path(job['python']),",
                "    repo_root=Path(job['repo']),",
                ")",
                "now = __import__('datetime').datetime.fromisoformat(job['now'])",
                "result = mod.prepare_inputs(config, 'AM', now=now, runner=runner, env_builder=env_builder)",
                "Path(job['out']).write_text(json.dumps({'status': result['preparation_status'], 'hash': (result.get('hashes') or {}).get('input_sha256'), 'digest_invoked': result['digest_invoked'], 'reused': result['reused']}), encoding='utf-8')",
            ]
        ),
        encoding="utf-8",
    )
    digest_text = json.dumps(_digest_document([_mail(body="PROCESS-BODY")])) + "\n"
    tmtb_text = json.dumps(_tmtb_document([{"doc_id": "d1", "text": "PROCESS-TMTB"}])) + "\n"
    log_path = tmp_path / "calls.log"
    procs: list[subprocess.Popen[bytes]] = []
    stderr_paths: list[Path] = []
    for index in (1, 2):
        job = {
            "log": str(log_path),
            "out": str(tmp_path / f"out-{index}.json"),
            "state": str(layout.state),
            "skill": str(layout.skill),
            "tmtb_script": str(layout.tmtb_script),
            "python": str(layout.python),
            "repo": str(layout.repo),
            "digest": str(layout.digest_path()),
            "tmtb": str(layout.tmtb_path()),
            "digest_text": digest_text,
            "tmtb_text": tmtb_text,
            "digest_stdout": _digest_stdout(output_file=str(layout.digest_path())),
            "tmtb_stdout": _tmtb_stdout(status="new_docs", output_file=str(layout.tmtb_path()), count=1, new_count=1),
            "now": NOW.isoformat(),
        }
        job_path = tmp_path / f"job-{index}.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        err_path = tmp_path / f"err-{index}.txt"
        stderr_paths.append(err_path)
        env = os.environ.copy()
        env["MARKET_DIGEST_REPO_ROOT"] = str(REPO_ROOT)
        env["MARKET_DIGEST_JOB"] = str(job_path)
        env["PYTHONPATH"] = str(REPO_ROOT)
        procs.append(
            subprocess.Popen(
                [sys.executable, str(worker)],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=err_path.open("w", encoding="utf-8"),
            )
        )
    try:
        for proc, err_path in zip(procs, stderr_paths, strict=True):
            try:
                proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                raise AssertionError(err_path.read_text(encoding="utf-8")) from None
            assert proc.returncode == 0, err_path.read_text(encoding="utf-8")
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.kill()
    lines = log_path.read_text(encoding="utf-8").split()
    assert lines.count("digest") == 1
    assert lines.count("tmtb") == 1
    outs = [json.loads((tmp_path / f"out-{index}.json").read_text(encoding="utf-8")) for index in (1, 2)]
    assert {item["digest_invoked"] for item in outs} == {True, False}
    assert outs[0]["hash"] == outs[1]["hash"]
    assert outs[0]["status"] == "ready"


def test_persisted_success_is_reused_without_another_fetch(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    seen: dict[str, Any] = {}

    def before() -> None:
        saved = json.loads(layout.attempt_path().read_text(encoding="utf-8"))
        seen["phase"] = saved["phase"]
        seen["claimed"] = saved["digest"]["claimed"]
        seen["spawned"] = saved["digest"]["spawned"]

    def after() -> None:
        saved = json.loads(layout.attempt_path().read_text(encoding="utf-8"))
        seen["pid"] = saved["digest"]["pid"]
        seen["still_running"] = saved["digest"]["still_running"]

    runner.before_spawn = before
    runner.after_spawn = after
    _successful_pair(layout, runner)
    first = _prepare(layout, runner)
    second = _prepare(layout, runner)
    assert seen == {"phase": "digest_claimed", "claimed": True, "spawned": False, "pid": _DEAD_PID, "still_running": True}
    assert first["preparation_status"] == "ready"
    assert first["digest_invoked"] is True and first["tmtb_invoked"] is True and first["reused"] is False
    assert first["emails_count"] == 1
    assert second["reused"] is True
    assert second["digest_invoked"] is False and second["tmtb_invoked"] is False
    assert second["hashes"]["input_sha256"] == first["hashes"]["input_sha256"]
    assert second["execution_id"] == first["execution_id"]
    assert _kinds(runner) == ["digest", "tmtb"]
    _assert_child_argv(runner, layout, date_value=DATE, period="AM")
    assert _mode(layout.attempt_path()) == 0o600
    batch = layout.state / "batches" / "AM" / DATE
    assert _mode(batch / "emails.json") == 0o600
    assert _mode(batch) == 0o700
    attempt_text = layout.attempt_path().read_text(encoding="utf-8")
    assert BODY not in attempt_text
    assert STDOUT_ONLY not in attempt_text
    assert GEMINI_VALUE not in _tree_text(layout.state)
    assert STDOUT_ONLY not in _tree_text(layout.state)
    assert "_digest_doc" not in attempt_text
    assert BODY in (batch / "emails.json").read_text(encoding="utf-8")
    assert layout.digest_path().is_file()
    pm = RecordingRunner()
    _successful_pair(layout, pm, period="PM")
    pm_result = _prepare(layout, pm, period="PM")
    assert pm_result["digest_invoked"] is True
    assert _kinds(pm) == ["digest", "tmtb"]


def test_lost_stdout_recovers_the_expected_file_and_runs_tmtb_once(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()

    def digest(_argv: list[str]) -> mod.ChildResult:
        _write_json(layout.digest_path(), _digest_document([_mail(body="RECOVERED-BODY")]))
        return _child("", returncode=0)

    def tmtb(_argv: list[str]) -> mod.ChildResult:
        _write_json(layout.tmtb_path(), _tmtb_document([{"doc_id": "d1", "text": "AFTER-RECOVERY"}]))
        return _child(_tmtb_stdout(status="new_docs", output_file=str(layout.tmtb_path()), count=1, new_count=1))

    runner.digest = digest
    runner.tmtb = tmtb
    first = _prepare(layout, runner)
    second = _prepare(layout, runner)
    assert first["digest_outcome"] == "recovered"
    assert first["digest_outcome"] != "no_new_emails"
    assert first["emails_count"] == 1
    assert first["preparation_status"] == "ready"
    assert "RECOVERED-BODY" in (layout.state / "batches" / "AM" / DATE / "emails.json").read_text(encoding="utf-8")
    assert second["reused"] is True
    assert _kinds(runner) == ["digest", "tmtb"]


def test_interrupted_digest_without_output_never_refetches(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()

    def digest(_argv: list[str]) -> mod.ChildResult:
        return _child("", returncode=None, timed_out=True)

    runner.digest = digest
    first = _prepare(layout, runner)
    second = _prepare(layout, runner)
    assert first["preparation_status"] == "unresolved"
    assert first["reason"] == "digest_timeout_no_output"
    assert first["emails_count"] is None
    assert "no_new_emails" not in json.dumps(first)
    assert second["digest_invoked"] is False and second["tmtb_invoked"] is False
    assert _kinds(runner) == ["digest"]
    assert not list((layout.state / "batches").glob("*/*/*")) if (layout.state / "batches").exists() else True


def test_digest_runner_exception_does_not_retry_or_call_tmtb(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()

    def digest(_argv: list[str]) -> mod.ChildResult:
        raise RuntimeError("secret-child-exception")

    runner.digest = digest
    first = _prepare(layout, runner)
    second = _prepare(layout, runner)
    assert first["preparation_status"] == "unresolved"
    assert first["digest_invoked"] is True and first["tmtb_invoked"] is False
    assert first["emails_count"] is None
    assert "secret-child-exception" not in json.dumps(first)
    assert "secret-child-exception" not in _tree_text(layout.state)
    assert _kinds(runner) == ["digest"]
    assert second["digest_invoked"] is False
    assert _kinds(runner) == ["digest"]


def test_no_new_emails_still_runs_tmtb_once(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    returned = "2026-11-11"

    def digest(_argv: list[str]) -> mod.ChildResult:
        return _child(
            _digest_stdout(
                date_value=returned,
                status="no_new_emails",
                count=0,
                extra={"funda_ingestion": {"status": "empty", "received": 0}, "image_ingestion": {"status": "ok"}},
            )
        )

    def tmtb(argv: list[str]) -> mod.ChildResult:
        assert returned in argv
        _write_json(
            layout.tmtb_path(returned),
            _tmtb_document([], date_value=returned),
        )
        return _child(_tmtb_stdout(date_value=returned, status="no_new_docs", ok=True, output_file=None))

    runner.digest = digest
    runner.tmtb = tmtb
    now = datetime(2026, 11, 10, 23, 50, tzinfo=mod.HKT)
    first = _prepare(layout, runner, now=now)
    second = _prepare(layout, runner, now=now)
    assert first["digest_outcome"] == "no_new_emails"
    assert first["emails_count"] == 0
    assert first["authoritative_date"] == returned
    assert first["tmtb"]["status"] == "no_new_docs"
    assert first["preparation_status"] == "ready"
    assert second["reused"] is True
    assert _kinds(runner) == ["digest", "tmtb"]
    _assert_child_argv(runner, layout, date_value=returned, period="AM")


def test_nonempty_file_beats_contradictory_no_new_emails_stdout(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    document = _digest_document([_mail(body="KEPT-MAIL-BODY")])

    def digest(_argv: list[str]) -> mod.ChildResult:
        _write_json(layout.digest_path(), document)
        return _child(_digest_stdout(status="no_new_emails", count=0))

    runner.digest = digest
    runner.tmtb = lambda _argv: _child(_tmtb_stdout(status="no_new_docs"))
    result = _prepare(layout, runner)
    assert result["digest_outcome"] == "recovered"
    assert result["emails_count"] == 1
    assert "KEPT-MAIL-BODY" in (layout.state / "batches" / "AM" / DATE / "emails.json").read_text(encoding="utf-8")


def test_tmtb_variants_are_terminal(tmp_path: Path) -> None:
    cases = ["new_docs", "reused_existing", "no_new_docs", "quota", "unavailable", "output_missing"]
    for name in cases:
        layout = _layout(tmp_path / name)
        runner = RecordingRunner()
        emails = [_mail(body=f"MAIL-{name}")]
        _write_json(layout.digest_path(), _digest_document(emails))

        def digest(_argv: list[str], bound_layout: Layout = layout) -> mod.ChildResult:
            return _child(_digest_stdout(output_file=str(bound_layout.digest_path())))

        foreign = layout.root / "foreign-tmtb.json"
        _write_json(foreign, {"secret": FOREIGN})
        if name == "reused_existing":
            text = "REUSED-TMTB-TEXT"
            _write_json(layout.tmtb_path(), _tmtb_document([{"doc_id": "old", "text": text}]))
            os.utime(layout.tmtb_path(), (1, 1))

            def tmtb(_argv: list[str], bound_layout: Layout = layout, bound_foreign: Path = foreign) -> mod.ChildResult:
                return _child(
                    _tmtb_stdout(
                        status="reused_existing",
                        output_file=str(bound_foreign),
                        count=1,
                        ok=True,
                    )
                )
        elif name == "no_new_docs":
            _write_json(layout.tmtb_path(), _tmtb_document([{"doc_id": "old", "text": "OLD-TMTB-DOC"}]))
            os.utime(layout.tmtb_path(), (1, 1))

            def tmtb(_argv: list[str], bound_foreign: Path = foreign) -> mod.ChildResult:
                return _child(_tmtb_stdout(status="no_new_docs", output_file=str(bound_foreign), ok=True))
        elif name == "quota":
            snippet = "SUMMARY-ONLY-SNIPPET"

            def tmtb(_argv: list[str], bound_layout: Layout = layout, bound_snippet: str = snippet) -> mod.ChildResult:
                _write_json(
                    bound_layout.tmtb_path(),
                    _tmtb_document([], [{"doc_id": "p1", "text": bound_snippet}]),
                )
                return _child(
                    _tmtb_stdout(
                        status="no_new_docs",
                        output_file=str(bound_layout.tmtb_path()),
                        ok=False,
                        quota_hit=True,
                        pending_count=1,
                    )
                )
        elif name == "unavailable":
            _write_json(layout.tmtb_path(), _tmtb_document([{"doc_id": "old", "text": "UNAVAILABLE-TMTB-TEXT"}]))
            os.utime(layout.tmtb_path(), (1, 1))

            def tmtb(_argv: list[str]) -> mod.ChildResult:
                return _child(
                    _tmtb_stdout(status="alphaengine_unavailable", ok=False, output_file=None),
                    returncode=1,
                )
        elif name == "output_missing":

            def tmtb(_argv: list[str]) -> mod.ChildResult:
                return _child(_tmtb_stdout(status="new_docs", output_file=None, new_count=1, ok=True))
        else:

            def tmtb(_argv: list[str], bound_layout: Layout = layout) -> mod.ChildResult:
                _write_json(bound_layout.tmtb_path(), _tmtb_document([{"doc_id": "d1", "text": "NEW-TMTB-TEXT"}]))
                return _child(
                    _tmtb_stdout(
                        status="new_docs",
                        output_file=str(bound_layout.tmtb_path()),
                        count=1,
                        new_count=1,
                    )
                )

        runner.digest = digest
        runner.tmtb = tmtb
        first = _prepare(layout, runner)
        calls_after_first = list(_kinds(runner))
        second = _prepare(layout, runner)
        state_text = _tree_text(layout.state)
        assert FOREIGN not in state_text
        assert first["emails_count"] == 1
        assert first["usable"] is True
        assert second["reused"] is True
        assert _kinds(runner) == calls_after_first
        assert calls_after_first.count("tmtb") == 1
        saved = mod.read_inputs(layout.state, "AM", date=DATE, now=NOW)
        if name == "quota":
            assert first["preparation_status"] == "ready_degraded"
            assert first["tmtb"]["quota_hit"] is True
            assert first["tmtb"]["summary_only"] is True
            assert first["tmtb"]["pending_snippets_count"] == 1
            assert "SUMMARY-ONLY-SNIPPET" not in json.dumps(first)
            assert saved["pending_snippets"][0]["text"] == "SUMMARY-ONLY-SNIPPET"
        elif name == "unavailable":
            assert first["preparation_status"] == "ready_degraded"
            assert first["tmtb"]["status"] == "alphaengine_unavailable"
            assert first["tmtb"]["count"] == 0
            assert saved["tmtb_docs"] == []
            assert "UNAVAILABLE-TMTB-TEXT" not in state_text
            assert layout.tmtb_path().is_file()
        elif name == "output_missing":
            assert first["preparation_status"] == "ready_degraded"
            assert first["tmtb"]["reason"] == "output_missing"
            assert first["tmtb"]["count"] == 0
        elif name == "no_new_docs":
            assert first["preparation_status"] == "ready"
            assert first["tmtb"]["status"] == "no_new_docs"
            assert first["tmtb"]["count"] == 0
            assert "OLD-TMTB-DOC" not in state_text
            assert layout.tmtb_path().is_file()
        elif name == "reused_existing":
            assert first["tmtb"]["status"] == "reused_existing"
            assert saved["tmtb_docs"][0]["text"] == "REUSED-TMTB-TEXT"
            assert "REUSED-TMTB-TEXT" not in json.dumps(first)
        else:
            assert first["preparation_status"] == "ready"
            assert first["tmtb"]["status"] == "new_docs"
            assert first["tmtb"]["count"] == 1
            assert saved["tmtb_docs"][0]["text"] == "NEW-TMTB-TEXT"
        assert foreign.is_file()


def test_tmtb_timeout_is_terminal_with_or_without_a_fresh_file(tmp_path: Path) -> None:
    for name, write_file in (("bare", False), ("fresh", True)):
        layout = _layout(tmp_path / name)
        runner = RecordingRunner()
        _write_json(layout.digest_path(), _digest_document([_mail(body=f"MAIL-{name}")]))
        runner.digest = lambda _argv, bound=layout: _child(_digest_stdout(output_file=str(bound.digest_path())))

        def tmtb(_argv: list[str], bound_layout: Layout = layout, bound_write: bool = write_file) -> mod.ChildResult:
            if bound_write:
                _write_json(bound_layout.tmtb_path(), _tmtb_document([{"doc_id": "d1", "text": "TIMEOUT-KEPT-TEXT"}]))
            return _child("", returncode=None, timed_out=True)

        runner.tmtb = tmtb
        first = _prepare(layout, runner)
        second = _prepare(layout, runner)
        assert first["preparation_status"] == "ready_degraded"
        assert first["tmtb"]["degraded"] is True
        assert second["reused"] is True
        assert _kinds(runner).count("tmtb") == 1
        if write_file:
            assert first["tmtb"]["status"] == "recovered_after_timeout"
            saved = mod.read_inputs(layout.state, "AM", date=DATE, now=NOW)
            assert saved["tmtb_docs"][0]["text"] == "TIMEOUT-KEPT-TEXT"
        else:
            assert first["tmtb"]["status"] == "timeout"
            assert first["tmtb"]["count"] == 0


def test_hkt_midnight_association_consumes_the_returned_batch_once(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    start = datetime(2026, 11, 10, 23, 50, tzinfo=mod.HKT)
    returned = "2026-11-11"

    def digest(_argv: list[str]) -> mod.ChildResult:
        _write_json(
            layout.digest_path(returned),
            _digest_document([_mail(body="MIDNIGHT-BODY")], date_value=returned),
        )
        return _child(_digest_stdout(date_value=returned, output_file=str(layout.digest_path(returned))))

    def tmtb(argv: list[str]) -> mod.ChildResult:
        assert returned in argv
        _write_json(layout.tmtb_path(returned), _tmtb_document([{"doc_id": "d1", "text": "NIGHT"}], date_value=returned))
        return _child(_tmtb_stdout(date_value=returned, status="new_docs", output_file=str(layout.tmtb_path(returned)), count=1, new_count=1))

    runner.digest = digest
    runner.tmtb = tmtb
    first = _prepare(layout, runner, now=start)
    same_night = _prepare(layout, runner, now=start)
    next_morning = _prepare(layout, runner, now=datetime(2026, 11, 11, 0, 10, tzinfo=mod.HKT))
    assert first["started_hkt_date"] == DATE
    assert first["authoritative_date"] == returned
    assert first["batch_id"] == f"AM/{returned}"
    assert same_night["reused"] is True
    assert next_morning["reused"] is True
    assert _kinds(runner) == ["digest", "tmtb"]
    association = json.loads((layout.state / "associations" / "AM" / f"{DATE}.json").read_text(encoding="utf-8"))
    index = json.loads((layout.state / "index" / "AM" / f"{returned}.json").read_text(encoding="utf-8"))
    assert association["authoritative_date"] == returned
    assert association["started_hkt_date"] == DATE
    assert index["started_hkt_date"] == DATE
    attempt = json.loads(layout.attempt_path().read_text(encoding="utf-8"))
    assert attempt["may_cross_midnight"] is True


def test_evening_failure_does_not_block_the_next_calendar_day(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    evening = datetime(2026, 11, 10, 21, 0, tzinfo=mod.HKT)
    runner.digest = lambda _argv: _child("", returncode=None, timed_out=True)
    first = _prepare(layout, runner, now=evening, period="PM")
    second = _prepare(layout, runner, now=evening, period="PM")
    assert first["reason"] == "digest_timeout_no_output"
    assert json.loads(layout.attempt_path(period="PM").read_text(encoding="utf-8"))["may_cross_midnight"] is False
    assert _kinds(runner) == ["digest"]
    assert second["digest_invoked"] is False

    def digest(_argv: list[str]) -> mod.ChildResult:
        _write_json(layout.digest_path("2026-11-11", "PM"), _digest_document([_mail()], date_value="2026-11-11", period="PM"))
        return _child(_digest_stdout(date_value="2026-11-11", period="PM", output_file=str(layout.digest_path("2026-11-11", "PM"))))

    def tmtb(_argv: list[str]) -> mod.ChildResult:
        _write_json(layout.tmtb_path("2026-11-11", "PM"), _tmtb_document([{"doc_id": "d1", "text": "next"}], date_value="2026-11-11", period="PM"))
        return _child(_tmtb_stdout(date_value="2026-11-11", period="PM", status="new_docs", output_file=str(layout.tmtb_path("2026-11-11", "PM")), count=1, new_count=1))

    runner.digest = digest
    runner.tmtb = tmtb
    nxt = _prepare(layout, runner, now=datetime(2026, 11, 11, 21, 0, tzinfo=mod.HKT), period="PM")
    assert nxt["digest_invoked"] is True
    assert nxt["authoritative_date"] == "2026-11-11"
    assert _kinds(runner).count("digest") == 2


def test_late_timeout_blocks_only_the_crossed_date(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    late = datetime(2026, 11, 10, 23, 50, tzinfo=mod.HKT)
    runner.digest = lambda _argv: _child("", returncode=None, timed_out=True)
    _prepare(layout, runner, now=late, period="PM")
    blocked = _prepare(layout, runner, now=datetime(2026, 11, 11, 9, 0, tzinfo=mod.HKT), period="PM")
    assert blocked["digest_invoked"] is False
    assert _kinds(runner) == ["digest"]

    def digest(_argv: list[str]) -> mod.ChildResult:
        _write_json(layout.digest_path("2026-11-12", "PM"), _digest_document([_mail()], date_value="2026-11-12", period="PM"))
        return _child(_digest_stdout(date_value="2026-11-12", period="PM"))

    runner.digest = digest
    runner.tmtb = lambda _argv: _child(
        _tmtb_stdout(date_value="2026-11-12", period="PM", status="no_new_docs")
    )
    later = _prepare(layout, runner, now=datetime(2026, 11, 12, 9, 0, tzinfo=mod.HKT), period="PM")
    assert later["digest_invoked"] is True
    assert later["authoritative_date"] == "2026-11-12"
    assert _kinds(runner).count("digest") == 2


def test_wrong_traversal_and_symlink_outputs_are_rejected(tmp_path: Path) -> None:
    foreign = tmp_path / "outside.json"
    _write_json(foreign, {"emails": [_mail(body=FOREIGN)], "date": DATE, "period": "AM"})

    layout = _layout(tmp_path / "missing")
    runner = RecordingRunner()
    runner.digest = lambda _argv, bound=foreign: _child(
        _digest_stdout(output_file=str(bound / ".." / "outside.json"))
    )
    missing = _prepare(layout, runner)
    assert missing["preparation_status"] == "unresolved"
    assert missing["emails_count"] is None
    assert FOREIGN not in _tree_text(layout.state)
    assert foreign.is_file()
    assert _kinds(runner) == ["digest"]
    _prepare(layout, runner)
    assert _kinds(runner) == ["digest"]

    linked = _layout(tmp_path / "link")
    target = tmp_path / "linked-digest.json"
    _write_json(target, _digest_document([_mail(body="SYMLINK-BODY")]))
    linked.digest_path().symlink_to(target)
    link_runner = RecordingRunner()
    link_runner.digest = lambda _argv: _child(_digest_stdout(status="no_new_emails", count=0))
    link_result = _prepare(linked, link_runner)
    assert link_result["preparation_status"] == "unresolved"
    assert link_result["digest_outcome"] != "no_new_emails"
    assert "no_new_emails" not in json.dumps(link_result)
    assert "SYMLINK-BODY" not in _tree_text(linked.state)
    assert linked.digest_path().is_symlink()
    assert _kinds(link_runner) == ["digest"]

    dangling = _layout(tmp_path / "dangling")
    dangling.digest_path().symlink_to(tmp_path / "does-not-exist.json")
    dangling_runner = RecordingRunner()
    dangling_runner.digest = lambda _argv: _child(_digest_stdout(status="no_new_emails", count=0))
    dangling_result = _prepare(dangling, dangling_runner)
    assert dangling_result["preparation_status"] == "unresolved"
    assert dangling_result["digest_outcome"] != "no_new_emails"
    assert _kinds(dangling_runner) == ["digest"]

    mismatched = _layout(tmp_path / "date")
    bad = _digest_document([_mail(body="DATE-MISMATCH-BODY")])
    bad["date"] = "2026-11-09"
    _write_json(mismatched.digest_path(), bad)
    mismatch_runner = RecordingRunner()
    mismatch_runner.digest = lambda _argv, bound=mismatched: _child(
        _digest_stdout(output_file=str(bound.digest_path()))
    )
    mismatch = _prepare(mismatched, mismatch_runner)
    assert mismatch["preparation_status"] == "unresolved"
    assert "DATE-MISMATCH-BODY" not in _tree_text(mismatched.state)
    assert mismatched.digest_path().is_file()

    secret_file = tmp_path / "original-secret.txt"
    secret_file.write_text("ORIGINAL-PATH-SECRET-CONTENT", encoding="utf-8")
    kept = _layout(tmp_path / "kept")
    kept_runner = RecordingRunner()
    mail = _mail(body="VISIBLE-BODY", original_path=str(secret_file))
    _write_json(kept.digest_path(), _digest_document([mail]))
    kept_runner.digest = lambda _argv, bound=kept: _child(_digest_stdout(output_file=str(bound.digest_path())))
    kept_runner.tmtb = lambda _argv, bound=kept: (
        _write_json(bound.tmtb_path(), _tmtb_document([{"doc_id": "d1", "text": "ok"}]))
        or _child(_tmtb_stdout(status="new_docs", output_file=str(bound.tmtb_path()), count=1, new_count=1))
    )
    kept_result = _prepare(kept, kept_runner)
    assert kept_result["emails_count"] == 1
    snapshot = (kept.state / "batches" / "AM" / DATE / "emails.json").read_text(encoding="utf-8")
    assert "VISIBLE-BODY" in snapshot
    assert "ORIGINAL-PATH-SECRET-CONTENT" not in _tree_text(kept.state)
    assert secret_file.read_text(encoding="utf-8") == "ORIGINAL-PATH-SECRET-CONTENT"


def test_ambiguous_fresh_files_are_not_refetched(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    late = datetime(2026, 11, 10, 23, 50, tzinfo=mod.HKT)

    def digest(_argv: list[str]) -> mod.ChildResult:
        _write_json(layout.digest_path(DATE), _digest_document([_mail(body="DAY-ONE")]))
        _write_json(layout.digest_path("2026-11-11"), _digest_document([_mail(body="DAY-TWO")], date_value="2026-11-11"))
        return _child("", returncode=0)

    runner.digest = digest
    first = _prepare(layout, runner, now=late)
    second = _prepare(layout, runner, now=late)
    assert first["preparation_status"] == "unresolved"
    assert first["reason"] == "digest_ambiguous_output"
    assert "DAY-ONE" not in _tree_text(layout.state)
    assert "DAY-TWO" not in _tree_text(layout.state)
    assert second["digest_invoked"] is False
    assert _kinds(runner) == ["digest"]
    assert layout.digest_path(DATE).is_file()
    assert layout.digest_path("2026-11-11").is_file()


def test_funda_priority_and_image_backlog_are_retained(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    emails = [
        _mail(id="j", body="JEFFREY-BODY", **{"from": "Jeffrey Favuzza <jeff@example.com>"}, source="sales-notes"),
        _mail(id="p", body="PETER-BODY", **{"from": "Notes <peter.callahan@example.com>"}, sender="Peter Callahan"),
        _mail(id="o", body="OTHER-BODY", **{"from": "Other Desk <other@example.com>"}),
        _mail(id="fe", body="ELIGIBLE-FUNDA-BODY", source="funda-ai", research_eligible=True),
        _mail(id="fx", body="EXCLUDED-FUNDA-BODY", source="funda-ai", research_eligible=False),
        _mail(id="b", body="BROKER-BODY", source="broker-desk"),
    ]
    images = {
        "ok": False,
        "status": "partial",
        "sources": {
            "funda-ai": {"pending": 2, "historical_backlog": 9, "status": "pending"},
            "sales-notes": {"pending": 0, "historical_backlog": 1, "status": "ok"},
        },
    }
    funda = {"ok": False, "status": "partial", "received": 2, "pending_documents": 1}
    document = _digest_document(emails, funda=funda, images=images)
    secret_status = "X" * 200 + "\nBODY-IN-STATUS"
    document["funda_ingestion"] = {**funda, "status": secret_status}

    def digest(_argv: list[str]) -> mod.ChildResult:
        _write_json(layout.digest_path(), document)
        return _child(_digest_stdout(count=6, extra={"funda_ingestion": funda, "image_ingestion": images}))

    runner.digest = digest
    runner.tmtb = lambda _argv: (
        _write_json(layout.tmtb_path(), _tmtb_document([{"doc_id": "d1", "text": "T"}]))
        or _child(_tmtb_stdout(status="new_docs", output_file=str(layout.tmtb_path()), count=1, new_count=1))
    )
    result = _prepare(layout, runner)
    assert result["emails_count"] == 6
    assert result["priority_senders"] == {
        "Jeffrey Favuzza": True,
        "Peter Callahan": True,
        "Christian DeGrasse": False,
    }
    assert result["funda"]["research_eligible_count"] == 1
    assert result["funda"]["research_excluded_count"] == 1
    assert result["funda"]["index_pending"] is True
    assert result["funda"]["pending_documents"] == 1
    assert "\n" not in str(result["funda"]["status"])
    assert len(str(result["funda"]["status"])) <= 80
    assert result["image_backlog"]["sources"]["funda-ai"]["pending"] == 2
    assert result["image_backlog"]["sources"]["funda-ai"]["historical_backlog"] == 9
    saved = json.loads((layout.state / "batches" / "AM" / DATE / "emails.json").read_text(encoding="utf-8"))
    assert [item["source"] for item in saved] == [
        "sales-notes",
        "sales-notes",
        "sales-notes",
        "funda-ai",
        "funda-ai",
        "broker-desk",
    ]
    assert saved[4]["body"] == "EXCLUDED-FUNDA-BODY"
    assert "EXCLUDED-FUNDA-BODY" not in json.dumps(result)
    read_back = mod.read_inputs(layout.state, "AM", date=DATE, now=NOW)
    assert read_back["emails"][4]["body"] == "EXCLUDED-FUNDA-BODY"
    assert "JEFFREY-BODY" not in json.dumps(mod.status_inputs(layout.state, "AM", date=DATE, now=NOW))


def test_nonarchive_env_strips_secrets_and_ignores_skill_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    (layout.skill / ".env").write_text(f"DOTENV_MARKER={DOTENV_MARKER}\n", encoding="utf-8")
    monkeypatch.setenv("GEMINI_API_KEY", GEMINI_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", OPENAI_VALUE)
    monkeypatch.setenv("FEISHU_APP_SECRET", FEISHU_VALUE)
    monkeypatch.setenv("BASH_ENV", BASH_VALUE)
    monkeypatch.setenv("PYTHONSTARTUP", PYTHONSTARTUP_VALUE)
    runner = RecordingRunner()
    _successful_pair(layout, runner)
    env_mock = _spec_env(mod.build_nonarchive_subprocess_env)
    result = _prepare(layout, runner, env_builder=env_mock)
    assert result["preparation_status"] == "ready"
    assert len(env_mock.call_args_list) == 2
    for call in env_mock.call_args_list:
        assert call.args == (None,)
        assert tuple(call.kwargs["allow_keys"]) == ("GEMINI_API_KEY",)
        assert call.kwargs["repo_root"] == layout.repo
    child_env = runner.calls[0]["env"]
    assert child_env["GEMINI_API_KEY"] == GEMINI_VALUE
    assert OPENAI_VALUE not in child_env.values()
    assert FEISHU_VALUE not in child_env.values()
    assert "BASH_ENV" not in child_env
    assert "PYTHONSTARTUP" not in child_env
    assert DOTENV_MARKER not in child_env.values()
    state_text = _tree_text(layout.state)
    for secret in (GEMINI_VALUE, OPENAI_VALUE, FEISHU_VALUE, BASH_VALUE, PYTHONSTARTUP_VALUE, DOTENV_MARKER):
        assert secret not in state_text
    assert (layout.skill / ".env").is_file()


def test_snapshot_hash_mismatch_does_not_refetch_or_delete(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    _successful_pair(layout, runner)
    first = _prepare(layout, runner)
    emails_path = layout.state / "batches" / "AM" / DATE / "emails.json"
    original_attempt = layout.attempt_path().read_bytes()
    emails_path.write_text(emails_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    tampered = emails_path.read_text(encoding="utf-8")
    second = _prepare(layout, runner)
    assert second["preparation_status"] == "snapshot_hash_mismatch"
    assert second["hashes"] is None
    assert second["digest_invoked"] is False and second["tmtb_invoked"] is False
    assert _kinds(runner) == ["digest", "tmtb"]
    assert emails_path.read_text(encoding="utf-8") == tampered
    assert layout.attempt_path().read_bytes() == original_attempt
    assert layout.digest_path().is_file()
    assert first["hashes"]["input_sha256"]
    status = mod.status_inputs(layout.state, "AM", date=DATE, now=NOW)
    assert status["preparation_status"] == "snapshot_hash_mismatch"
    assert status["hashes"] is None


def test_absent_record_is_not_reported_as_no_new_emails(tmp_path: Path) -> None:
    state = tmp_path / "empty-state"
    state.mkdir()
    status = mod.status_inputs(state, "AM", date=DATE, now=NOW)
    assert status["preparation_status"] == "absent"
    assert status["reason"] == "no_record"
    assert status["emails_count"] is None
    assert "no_new_emails" not in json.dumps(status)
    code = mod.main(["status", "--period", "AM", "--state-root", str(state), "--date", DATE])
    assert code == 2


def test_cli_status_hides_bodies_and_read_is_bounded(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()
    _successful_pair(layout, runner)
    code = mod.main(
        [
            "prepare",
            "--period",
            "AM",
            "--state-root",
            str(layout.state),
            "--skill-dir",
            str(layout.skill),
            "--tmtb-script",
            str(layout.tmtb_script),
            "--python",
            str(layout.python),
            "--repo-root",
            str(layout.repo),
        ],
        runner=_spec_runner(runner),
        env_builder=_spec_env(),
        now=NOW,
    )
    prepared = json.loads(capsys.readouterr().out)
    assert code == 0
    assert prepared["preparation_status"] == "ready"
    assert BODY not in json.dumps(prepared)
    assert STDOUT_ONLY not in json.dumps(prepared)

    status_code = mod.main(
        ["status", "--period", "AM", "--state-root", str(layout.state), "--date", DATE],
        now=NOW,
    )
    status_out = capsys.readouterr().out
    assert status_code == 0
    assert BODY not in status_out
    assert "TMTB-FULL-TEXT" not in status_out

    read_code = mod.main(
        ["read", "--period", "AM", "--state-root", str(layout.state), "--date", DATE],
        now=NOW,
    )
    read_payload = json.loads(capsys.readouterr().out)
    assert read_code == 0
    assert read_payload["emails"][0]["body"] == BODY
    assert read_payload["tmtb_docs"][0]["text"] == "TMTB-FULL-TEXT"

    bound_code = mod.main(
        [
            "read",
            "--period",
            "AM",
            "--state-root",
            str(layout.state),
            "--date",
            DATE,
            "--max-bytes",
            "32",
        ],
        now=NOW,
    )
    bound = json.loads(capsys.readouterr().out)
    assert bound_code == 2
    assert bound["preparation_status"] == "content_exceeds_bound"
    assert bound["emails"] is None
    assert bound["tmtb_docs"] is None
    assert BODY not in json.dumps(bound)
    assert bound["hashes"]["input_sha256"] == prepared["hashes"]["input_sha256"]

    other_state = tmp_path / "other-state"
    other_state.mkdir()
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "state_root": str(other_state),
                "skill_dir": str(layout.skill),
                "tmtb_script": str(layout.tmtb_script),
                "python": str(layout.python),
                "repo_root": str(layout.repo),
                "GEMINI_API_KEY": "super-secret-value",
            }
        ),
        encoding="utf-8",
    )
    secret_code = mod.main(
        ["prepare", "--period", "AM", "--config", str(config_path)],
        runner=_spec_runner(RecordingRunner()),
        env_builder=_spec_env(),
        now=NOW,
    )
    secret_out = capsys.readouterr().out
    assert secret_code == 1
    assert "super-secret-value" not in secret_out
    assert "GEMINI_API_KEY" in secret_out

    relative_code = mod.main(
        [
            "prepare",
            "--period",
            "AM",
            "--state-root",
            "relative/secret-value-path",
            "--skill-dir",
            str(layout.skill),
            "--tmtb-script",
            str(layout.tmtb_script),
            "--python",
            str(layout.python),
        ],
        now=NOW,
    )
    relative_out = capsys.readouterr().out
    assert relative_code == 1
    assert "secret-value-path" not in relative_out

    usage_code = mod.main(["prepare"])
    assert usage_code == 1

    def explode(
        base_env: Mapping[str, str] | None = None,
        *,
        extra_env: Mapping[str, str] | None = None,
        drop_keys: Any = (),
        allow_keys: Any = (),
        repo_root: Path | None = None,
    ) -> dict[str, str]:
        del base_env, extra_env, drop_keys, allow_keys, repo_root
        raise RuntimeError("secret-traceback-value")

    fresh = _layout(tmp_path / "explode")
    internal = mod.main(
        [
            "prepare",
            "--period",
            "AM",
            "--state-root",
            str(fresh.state),
            "--skill-dir",
            str(fresh.skill),
            "--tmtb-script",
            str(fresh.tmtb_script),
            "--python",
            str(fresh.python),
            "--repo-root",
            str(fresh.repo),
        ],
        runner=_spec_runner(RecordingRunner()),
        env_builder=create_autospec(mod.build_nonarchive_subprocess_env, side_effect=explode),
        now=NOW,
    )
    internal_out = capsys.readouterr().out
    assert internal == 1
    assert "RuntimeError" in internal_out
    assert "secret-traceback-value" not in internal_out


def test_run_json_command_bounds_stdio_and_kills_the_group(tmp_path: Path) -> None:
    script = tmp_path / "sleeper.py"
    script.write_text(
        "\n".join(
            [
                "import signal, sys, time",
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
                "sys.stdout.write('Y' * 10000)",
                "sys.stdout.flush()",
                "sys.stderr.write('SECRET-STDERR' + ('E' * 100000))",
                "sys.stderr.flush()",
                "time.sleep(30)",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    seen: list[int] = []
    result = mod.run_json_command(
        [sys.executable, str(script)],
        env={"PATH": "/usr/bin:/bin"},
        timeout_seconds=1.0,
        stdout_limit_bytes=1024,
        stderr_limit_bytes=0,
        on_spawn=seen.append,
    )
    assert result.timed_out is True
    assert result.still_running is False
    assert result.stdout_truncated is True
    assert len(result.stdout) == 1024
    assert result.stderr_byte_count > 1000
    assert result.stderr_truncated is True
    assert "SECRET-STDERR" not in repr(result)
    assert seen and seen[0] > 1
    with pytest.raises(ProcessLookupError):
        os.kill(seen[0], 0)


def test_still_running_digest_blocks_a_second_caller(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    runner = RecordingRunner()

    def digest(_argv: list[str]) -> mod.ChildResult:
        return _child("", returncode=None, still_running=True, pid=os.getpid())

    runner.digest = digest
    first = _prepare(layout, runner)
    second = _prepare(layout, runner)
    assert first["reason"] == "digest_child_may_still_be_running"
    assert first["emails_count"] is None
    assert second["digest_invoked"] is False
    assert _kinds(runner) == ["digest"]
    assert "no_new_emails" not in json.dumps(second)


_GROUP_CHILD = r'''
import json, os, pathlib, signal, subprocess, sys, time
mode, directory = sys.argv[1:]
root = pathlib.Path(directory)
descendant_source = """
import json, os, pathlib, signal, sys, time
mode, directory = sys.argv[1:]
if mode == "term_parent_exits":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(directory, "descendant.json").write_text(json.dumps({
    "pid": os.getpid(), "pgid": os.getpgrp()}))
while True:
    time.sleep(0.05)
"""
if mode == "term_parent_exits":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
subprocess.Popen([sys.executable, "-c", descendant_source, mode, directory])
deadline = time.monotonic() + 1.5
while not (root / "descendant.json").exists():
    if time.monotonic() >= deadline:
        raise SystemExit(3)
    time.sleep(0.01)
print('{"synthetic":true}', flush=True)
if mode == "leader_exits":
    raise SystemExit(0)
while True:
    time.sleep(0.05)
'''


def _running_process(pid: int) -> bool:
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


def _assert_owned_group_ends(tmp_path: Path, mode: str) -> None:
    work = tmp_path / mode
    work.mkdir()
    script = work / "synthetic_child.py"
    script.write_text(_GROUP_CHILD, encoding="utf-8")
    command_record = work / "command.json"
    box: dict[str, Any] = {}

    def on_spawn(pid: int) -> None:
        command_record.write_text(json.dumps({"pid": pid}), encoding="utf-8")

    def invoke() -> None:
        box["result"] = mod.run_json_command(
            [sys.executable, str(script), mode, str(work)],
            env={"PATH": os.defpath, "PYTHONDONTWRITEBYTECODE": "1"},
            timeout_seconds=1.0,
            stdout_limit_bytes=128,
            stderr_limit_bytes=0,
            on_spawn=on_spawn,
        )

    thread = threading.Thread(target=invoke, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(8)
    if thread.is_alive():
        if command_record.is_file():
            try:
                pid = int(json.loads(command_record.read_text(encoding="utf-8"))["pid"])
                os.killpg(pid, signal.SIGKILL)
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass
        pytest.fail("run_json_command did not return within the owned-group lifetime")
    result = box["result"]
    descendant = json.loads((work / "descendant.json").read_text(encoding="utf-8"))
    descendant_pid = int(descendant["pid"])
    deadline = time.monotonic() + 2
    while _running_process(descendant_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert time.monotonic() - started < 8
    assert result.still_running is False
    assert not _running_process(descendant_pid)
    assert "synthetic" in result.stdout
    if mode == "term_parent_exits":
        assert result.timed_out is True
    else:
        assert result.timed_out is False


def test_run_json_command_reaps_descendants_after_leader_exit(tmp_path: Path) -> None:
    _assert_owned_group_ends(tmp_path, "leader_exits")


def test_run_json_command_kills_descendant_that_ignores_term(tmp_path: Path) -> None:
    _assert_owned_group_ends(tmp_path, "term_parent_exits")


def test_pipe_reader_stop_leaves_a_later_descriptor_open() -> None:
    import gc

    read_fd, write_fd = os.pipe()
    stream = os.fdopen(read_fd, "rb")
    box: dict[str, tuple[bytes, int, bool]] = {}
    reader = mod._start_pipe_reader(stream, 64, box)
    assert reader is not None
    started = time.monotonic()
    try:
        os.write(write_fd, b"queued-before-stop")
        mod._finish_pipe(reader, stream)
        thread, _stop = reader
        assert not thread.is_alive()
        assert time.monotonic() - started < 2
        assert box["value"][0] == b"queued-before-stop"
        replacement = os.open(os.devnull, os.O_RDONLY)
        try:
            stream.close()
            stream.close()
            del stream
            gc.collect()
            os.fstat(replacement)
            again = os.open(os.devnull, os.O_RDONLY)
            try:
                gc.collect()
                os.fstat(replacement)
                os.fstat(again)
            finally:
                os.close(again)
        finally:
            os.close(replacement)
    finally:
        os.close(write_fd)


def test_tmtb_degraded_facts_survive_snapshot_interruption(tmp_path: Path) -> None:
    cases = (
        ("quota", True, [], "quota", True),
        ("partial", False, [{"error": "synthetic_failure"}], "partial", False),
    )
    for name, quota, failures, reason, summary_only in cases:
        layout = _layout(tmp_path / name)
        runner = RecordingRunner()
        emails = [_mail(body="RECOVERY-MAIL-BODY", id="synthetic_fresh")]
        docs = [{"doc_id": "synthetic_document", "title": "Synthetic document", "text": "Complete synthetic document body"}]
        pending = [{"doc_id": "synthetic_pending", "snippet": "Synthetic pending excerpt"}]

        def digest(_argv: list[str], bound_layout: Layout = layout, bound_emails: list[dict[str, Any]] = emails) -> mod.ChildResult:
            _write_json(bound_layout.digest_path(), _digest_document(bound_emails))
            return _child(_digest_stdout(output_file=str(bound_layout.digest_path())))

        def tmtb(
            _argv: list[str],
            bound_layout: Layout = layout,
            bound_docs: list[dict[str, Any]] = docs,
            bound_pending: list[dict[str, Any]] = pending,
            bound_quota: bool = quota,
            bound_failures: list[dict[str, Any]] = failures,
        ) -> mod.ChildResult:
            _write_json(
                bound_layout.tmtb_path(),
                _tmtb_document(bound_docs, bound_pending),
            )
            return _child(
                _tmtb_stdout(
                    status="new_docs",
                    output_file=str(bound_layout.tmtb_path()),
                    ok=not bound_quota and not bound_failures,
                    count=1,
                    new_count=1,
                    pending_count=1,
                    quota_hit=bound_quota,
                    failures=bound_failures,
                )
            )

        runner.digest = digest
        runner.tmtb = tmtb
        original = mod._write_snapshots
        injected = {"count": 0}

        def fail_once(state_root: Path, attempt: dict[str, Any]) -> dict[str, Any]:
            if injected["count"] == 0:
                injected["count"] += 1
                raise RuntimeError("snapshot_interrupted")
            return original(state_root, attempt)

        with patch.object(mod, "_write_snapshots", new=fail_once):
            with pytest.raises(RuntimeError, match="snapshot_interrupted"):
                _prepare(layout, runner)
        persisted = json.loads(layout.attempt_path().read_text(encoding="utf-8"))
        assert persisted["tmtb"]["claimed"] is True
        assert persisted["tmtb"]["outcome"] == "new_docs"
        assert persisted["tmtb"]["record"]["quota_hit"] is quota
        assert persisted["tmtb"]["record"]["failure_count"] == (0 if quota else 1)
        assert persisted["tmtb"]["record"]["reason"] == reason
        assert persisted["tmtb"]["record"]["summary_only"] is summary_only
        assert persisted["tmtb"]["record"]["new_count"] == 1
        assert "synthetic_failure" not in layout.attempt_path().read_text(encoding="utf-8")
        assert "RECOVERY-MAIL-BODY" not in layout.attempt_path().read_text(encoding="utf-8")
        recovered = _prepare(layout, runner)
        saved = mod.read_inputs(layout.state, "AM", date=DATE, now=NOW)
        status = mod.status_inputs(layout.state, "AM", date=DATE, now=NOW)
        assert injected["count"] == 1
        assert _kinds(runner) == ["digest", "tmtb"]
        assert recovered["preparation_status"] == "ready_degraded"
        assert status["preparation_status"] == "ready_degraded"
        assert recovered["tmtb"]["quota_hit"] is quota
        assert recovered["tmtb"]["failure_count"] == (0 if quota else 1)
        assert recovered["tmtb"]["degraded"] is True
        assert recovered["tmtb"]["reason"] == reason
        assert recovered["tmtb"]["new_count"] == 1
        assert recovered["tmtb"]["summary_only"] is summary_only
        assert recovered["tmtb"]["count"] == 1
        assert recovered["tmtb"]["pending_snippets_count"] == 1
        assert saved["emails"] == emails
        assert saved["tmtb_docs"] == docs
        assert saved["pending_snippets"] == pending
        public = json.dumps(recovered)
        assert "synthetic_failure" not in public
        assert "RECOVERY-MAIL-BODY" not in public
        assert "Complete synthetic document body" not in public


def test_digest_output_file_must_name_the_expected_file(tmp_path: Path) -> None:
    mismatch = _layout(tmp_path / "mismatch")
    configured = (tmp_path / "configured-output").resolve()
    configured.mkdir()
    old_path = configured / f"digest_{DATE}_AM.json"
    old_email = _mail(body="synthetic_old-body", id="synthetic_old")
    _write_json(old_path, _digest_document([old_email]))
    os.utime(old_path, (1, 1))
    fresh_email = _mail(body="synthetic_fresh-body", id="synthetic_fresh")
    fresh_path = mismatch.digest_path()
    runner = RecordingRunner()

    def digest(_argv: list[str]) -> mod.ChildResult:
        _write_json(fresh_path, _digest_document([fresh_email]))
        return _child(_digest_stdout(output_file=str(fresh_path)))

    def tmtb(_argv: list[str]) -> mod.ChildResult:
        raise AssertionError("tmtb must not run for a mismatched digest path")

    runner.digest = digest
    runner.tmtb = tmtb
    rejected = _prepare(mismatch, runner, config=mismatch.config(digest_output_dir=configured))
    assert rejected["preparation_status"] == "unresolved"
    assert rejected["usable"] is False
    assert rejected["reason"] == "digest_output_path_mismatch"
    assert rejected["emails_count"] is None
    assert "synthetic_old" not in _tree_text(mismatch.state)
    assert "synthetic_fresh" not in _tree_text(mismatch.state)
    assert json.loads(old_path.read_text(encoding="utf-8"))["emails"] == [old_email]
    assert json.loads(fresh_path.read_text(encoding="utf-8"))["emails"] == [fresh_email]
    again = _prepare(mismatch, runner, config=mismatch.config(digest_output_dir=configured))
    assert again["digest_invoked"] is False
    assert again["preparation_status"] == "unresolved"
    assert _kinds(runner) == ["digest"]

    legal = _layout(tmp_path / "legal-old")
    legal_email = _mail(body="LEGAL-OLD-BODY", id="legal_old")
    _write_json(legal.digest_path(), _digest_document([legal_email], status="reused_existing"))
    os.utime(legal.digest_path(), (1, 1))
    legal_runner = RecordingRunner()
    legal_runner.digest = lambda _argv, bound=legal: _child(
        _digest_stdout(status="reused_existing", output_file=str(bound.digest_path()))
    )
    legal_runner.tmtb = lambda _argv: _child(_tmtb_stdout(status="no_new_docs"))
    accepted = _prepare(legal, legal_runner)
    assert accepted["preparation_status"] == "ready"
    assert accepted["digest_outcome"] == "reused_existing"
    assert accepted["emails_count"] == 1
    saved = mod.read_inputs(legal.state, "AM", date=DATE, now=NOW)
    assert saved["emails"] == [legal_email]
    assert json.loads(legal.digest_path().read_text(encoding="utf-8"))["emails"] == [legal_email]
    assert _kinds(legal_runner) == ["digest", "tmtb"]

    linked = _layout(tmp_path / "linked-dir")
    alias = tmp_path / "output-alias"
    alias.symlink_to(linked.output, target_is_directory=True)
    linked_email = _mail(body="LINKED-DIR-BODY", id="linked_dir")
    _write_json(linked.digest_path(), _digest_document([linked_email]))
    linked_runner = RecordingRunner()
    linked_runner.digest = lambda _argv, bound_alias=alias, bound=linked: _child(
        _digest_stdout(output_file=str(bound_alias / bound.digest_path().name))
    )
    linked_runner.tmtb = lambda _argv: _child(_tmtb_stdout(status="no_new_docs"))
    via_alias = _prepare(linked, linked_runner)
    assert via_alias["preparation_status"] == "ready"
    assert mod.read_inputs(linked.state, "AM", date=DATE, now=NOW)["emails"] == [linked_email]
    assert "LINKED-DIR-BODY" in (linked.state / "batches" / "AM" / DATE / "emails.json").read_text(encoding="utf-8")
