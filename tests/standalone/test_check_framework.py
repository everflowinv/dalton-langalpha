"""Offline unit tests for the standalone check framework and release packager."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tarfile
from unittest.mock import create_autospec, patch

import pytest

from tests.standalone.checks import _lib

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKS_DIR = REPO_ROOT / "tests" / "standalone" / "checks"
PACKAGE_SCRIPT = REPO_ROOT / "scripts" / "standalone" / "package_release.sh"


def _make_docker_save_tar(
    layers: list[tuple[str, bytes]],
    *,
    manifest_layers: list[str] | None = None,
    include_manifest: bool = True,
) -> bytes:
    outer_buf = io.BytesIO()
    with tarfile.open(fileobj=outer_buf, mode="w") as outer:
        if include_manifest:
            declared = (
                manifest_layers
                if manifest_layers is not None
                else [name for name, _ in layers]
            )
            manifest_raw = json.dumps(
                [{"Config": "config.json", "RepoTags": ["test:latest"], "Layers": declared}]
            ).encode("utf-8")
            minfo = tarfile.TarInfo("manifest.json")
            minfo.size = len(manifest_raw)
            outer.addfile(minfo, io.BytesIO(manifest_raw))
        for name, raw in layers:
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            outer.addfile(info, io.BytesIO(raw))
    return outer_buf.getvalue()


def _make_layer_tar(entries: list[tuple[str, bytes]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as ltar:
        for path, data in entries:
            info = tarfile.TarInfo(path)
            info.size = len(data)
            ltar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_b1_dev_mode_declares_oss_auth_and_is_discovered() -> None:
    b1_path = CHECKS_DIR / "b1_dev_mode.py"
    assert _lib.parse_check_auth(b1_path) == "oss"

    groups = _lib.discover_checks(CHECKS_DIR)
    assert "oss" in groups
    assert b1_path in groups["oss"]


def test_discover_checks_groups_by_auth_and_rejects_missing_header(tmp_path: Path) -> None:
    (tmp_path / "b1_one.py").write_text("#!/usr/bin/env python3\n# auth: oss\n", encoding="utf-8")
    (tmp_path / "b4_two.py").write_text("# auth: local\n", encoding="utf-8")
    (tmp_path / "b2_three.py").write_text("# auth: oss\n", encoding="utf-8")

    groups = _lib.discover_checks(tmp_path)
    assert list(groups.keys()) == ["oss", "local"]
    assert [p.name for p in groups["oss"]] == ["b1_one.py", "b2_three.py"]
    assert [p.name for p in groups["local"]] == ["b4_two.py"]

    bad = tmp_path / "b9_bad.py"
    bad.write_text("# no auth header here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing '# auth"):
        _lib.discover_checks(tmp_path)


def test_load_env_file_parses_quotes_and_exports(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n"
        "APP_ENV=core\n"
        'export INTERNAL_SERVICE_TOKEN="tok-123"\n'
        "DB_NAME='dalton_core'\n"
        "\n",
        encoding="utf-8",
    )
    parsed = _lib.load_env_file(env_file)
    assert parsed == {
        "APP_ENV": "core",
        "INTERNAL_SERVICE_TOKEN": "tok-123",
        "DB_NAME": "dalton_core",
    }


def test_sse_block_parsing_and_extractors() -> None:
    ev1 = _lib.parse_sse_block(
        [
            "id: 1",
            "event: message_chunk",
            'data: {"thread_id": "t-1", "run_id": "r-1", "content": "Hello "}',
        ]
    )
    ev2 = _lib.parse_sse_block(
        [
            "id: 2",
            "event: message_chunk",
            'data: {"thread_id": "t-1", "content": [{"type": "text", "text": "world!"}]}',
        ]
    )
    assert ev1 is not None and ev2 is not None
    assert _lib.extract_thread_id([ev1, ev2]) == "t-1"
    assert _lib.extract_run_id([ev1, ev2]) == "r-1"
    assert _lib.extract_assistant_text([ev1, ev2]) == "Hello world!"


def test_scan_tar_stream_inspects_nested_layers_strictly(tmp_path: Path) -> None:
    l1 = _make_layer_tar([("app/scripts/dalton/researchdata_bridge.py", b"secret")])
    l2 = _make_layer_tar([("app/server.py", b"ok")])
    archive = _make_docker_save_tar([("layer1/layer.tar", l1), ("layer2/layer.tar", l2)])

    tar_file = tmp_path / "img.tar"
    tar_file.write_bytes(archive)
    hits, scanned = _lib.scan_tar_file_for_paths(
        tar_file,
        ("scripts/dalton/researchdata_bridge.py", "plugins/dalton_data/"),
    )
    assert scanned == 2
    assert hits == ["layer1/layer.tar:app/scripts/dalton/researchdata_bridge.py"]

    # Stream wrapper matches
    assert _lib.scan_tar_stream_for_paths(
        archive, ("scripts/dalton/researchdata_bridge.py",)
    ) == ["layer1/layer.tar:app/scripts/dalton/researchdata_bridge.py"]


def test_scan_tar_rejects_missing_manifest_zero_layers_missing_layer_and_corrupt_layer() -> None:
    l1 = _make_layer_tar([("app/server.py", b"ok")])

    # 1. Missing manifest.json
    no_manifest = _make_docker_save_tar([("layer1/layer.tar", l1)], include_manifest=False)
    with pytest.raises(RuntimeError, match="missing manifest.json"):
        _lib.scan_tar_stream_for_paths(no_manifest, ("forbidden",))

    # 2. 0 layers declared
    zero_layers = _make_docker_save_tar([], manifest_layers=[])
    with pytest.raises(RuntimeError, match="declared 0 layers"):
        _lib.scan_tar_stream_for_paths(zero_layers, ("forbidden",))

    # 3. Declared layer missing from archive
    missing_layer = _make_docker_save_tar([], manifest_layers=["ghost/layer.tar"])
    with pytest.raises(RuntimeError, match="missing from archive"):
        _lib.scan_tar_stream_for_paths(missing_layer, ("forbidden",))

    # 4. Declared layer is corrupted / unparseable tar
    corrupt_layer = _make_docker_save_tar([("bad/layer.tar", b"not-a-valid-tar-archive")])
    with pytest.raises(RuntimeError, match="Failed to parse declared layer"):
        _lib.scan_tar_stream_for_paths(corrupt_layer, ("forbidden",))


def test_scan_artifacts_for_secrets_and_compose_error_redaction(tmp_path: Path) -> None:
    secret_val = "abcdef0123456789abcdef0123456789"
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"INTERNAL_SERVICE_TOKEN={secret_val}\nDB_PASSWORD=dbsecret0123456789abcdef\n",
        encoding="utf-8",
    )
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    (artifacts / "clean.txt").write_text("all clear\n", encoding="utf-8")
    (artifacts / "baseline.dump").write_bytes(secret_val.encode("utf-8"))  # binary dump skipped
    assert _lib.scan_artifacts_for_secrets(artifacts, env_file) == []

    (artifacts / "compose-logs-oss.txt").write_text(
        f"GET /api/v1/users/me header={secret_val}\n", encoding="utf-8"
    )
    leaks = _lib.scan_artifacts_for_secrets(artifacts, env_file)
    assert leaks == ["compose-logs-oss.txt: leaked INTERNAL_SERVICE_TOKEN"]
    assert all(secret_val not in hit for hit in leaks)

    # ComposeContext.compose() must not print full command args or raw secrets on failure
    ctx = _lib.ComposeContext(
        work_dir=tmp_path,
        compose_files=[tmp_path / "docker-compose.yml"],
        artifacts_dir=artifacts,
    )
    fake_proc = subprocess.CompletedProcess(
        args=["docker", "compose"],
        returncode=1,
        stdout=b"",
        stderr=f"err with {secret_val}".encode("utf-8"),
    )
    mock_run = create_autospec(subprocess.run, return_value=fake_proc)
    with patch.object(subprocess, "run", mock_run):
        with pytest.raises(RuntimeError) as exc_info:
            ctx.compose("exec", "-T", "backend", "python", "-c", f"token={secret_val}")
    msg = str(exc_info.value)
    assert secret_val not in msg
    assert "<REDACTED:INTERNAL_SERVICE_TOKEN>" in msg
    assert "python" not in msg


def test_package_release_exports_clean_allowlist_and_include_ci_option(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def _touch(rel: str, content: str = "ok\n") -> None:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    for rel in (
        "LICENSE",
        "README.md",
        "Makefile",
        "pyproject.toml",
        "uv.lock",
        "server.py",
        "alembic.ini",
        "config.yaml",
        "config.core.yaml",
        "agent_config.yaml",
        "agent_config.core.yaml",
        "docker-compose.yml",
        "docker-compose.core.yml",
        "docker-compose.ci.yml",
        ".env.example",
        ".env.core.example",
        ".dockerignore",
        "Dockerfile.sandbox",
        "src/dalton/dalton.core.yaml",
        "src/dalton/settings.py",
        "libs/market-protocol/pyproject.toml",
        "libs/chartkit/pyproject.toml",
        "libs/edgar-engine/pyproject.toml",
        "migrations/env.py",
        "mcp_servers/_bootstrap.py",
        "plugins/README.md",
        "plugins/alternative_data/plugin.json",
        "plugins/dalton/plugin.json",
        "plugins/firecrawl/plugin.json",
        "plugins/langalpha_deliverables/plugin.json",
        "plugins/langalpha_market_data/plugin.json",
        "plugins/langalpha_research/plugin.json",
        "plugins/langalpha_service/plugin.json",
        "plugins/yfinance/plugin.json",
        "workflows/sample.md",
        "config/dalton/.gitkeep",
        "config/dalton/README.md",
        "deploy/Dockerfile.backend",
        "deploy/Dockerfile.dev",
        "deploy/Dockerfile.web",
        "deploy/standalone/Caddyfile",
        "scripts/configure.sh",
        "scripts/docker_entrypoint.sh",
        "scripts/_errors.py",
        "scripts/dalton/Dockerfile.dalton",
        "scripts/dalton/Dockerfile.postgres",
        "scripts/dalton/docker_entrypoint_dalton.sh",
        "scripts/dalton/configure_minio.sh",
        "scripts/dalton/log_redaction.py",
        "scripts/standalone/init.sh",
        "web/package.json",
        "docs-site/package.json",
        "docs-site/src/content/docs/everflow/monitors.md",
        "tests/fakes/fake_openai_server.py",
        "tests/standalone/run_checks.sh",
    ):
        _touch(rel)

    _touch("src/dalton/settings.py", 'CONTACT = "demo@everflowinv.com"\n')

    for excluded in (
        "plugins/dalton_data/plugin.json",
        "plugins/dalton_data/dalton_sec_mcp_server.py",
        "scripts/dalton/researchdata_bridge.py",
        "scripts/dalton/research_runtime/scripts/data_gateway.py",
        "scripts/dalton/ops/prod/deploy_main.sh",
        "agent_config.dalton.yaml",
        "config.dalton.yaml",
        "src/dalton/nested.dalton.yaml",
        "docker-compose.dalton.yml",
        "web/.env.dalton",
        "integrations/dalton-channel-gateway/package.json",
        "deploy/dalton/Caddyfile",
    ):
        _touch(excluded, "Proprietary everflowinv\n")

    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "ci@example.com"], cwd=str(repo), check=True
    )
    subprocess.run(["git", "config", "user.name", "CI"], cwd=str(repo), check=True)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(repo), check=True)

    # 1. Default release export: includes Makefile, scripts/configure.sh, full docs-site; excludes CI files
    out_dir = tmp_path / "dist"
    tar_path = tmp_path / "dist.tar.gz"
    report_path = tmp_path / "scan.txt"

    proc = subprocess.run(
        [
            "bash",
            str(PACKAGE_SCRIPT),
            "--repo",
            str(repo),
            "--ref",
            "HEAD",
            "--output",
            str(out_dir),
            "--tar",
            str(tar_path),
            "--report",
            str(report_path),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "Standalone Core Release Scan Report" in proc.stdout
    assert report_path.is_file()
    assert "everflowinv" in report_path.read_text(encoding="utf-8")
    assert tar_path.is_file() and tar_path.stat().st_size > 0

    for expected in (
        "Makefile",
        "scripts/configure.sh",
        "server.py",
        "pyproject.toml",
        "docker-compose.yml",
        "docker-compose.core.yml",
        "config.yaml",
        "config.core.yaml",
        "agent_config.yaml",
        "agent_config.core.yaml",
        "src/dalton/dalton.core.yaml",
        "deploy/standalone/Caddyfile",
        "scripts/standalone/init.sh",
        "scripts/dalton/Dockerfile.dalton",
        "scripts/dalton/Dockerfile.postgres",
        "docs-site/src/content/docs/everflow/monitors.md",
        ".release_git_sha",
    ):
        assert (out_dir / expected).exists(), f"Missing expected core file: {expected}"

    assert not (out_dir / "docker-compose.ci.yml").exists()
    assert not (out_dir / "tests/fakes").exists()
    assert not (out_dir / "tests/standalone").exists()

    for excluded in (
        "plugins/dalton_data",
        "scripts/dalton/researchdata_bridge.py",
        "scripts/dalton/research_runtime",
        "scripts/dalton/ops",
        "agent_config.dalton.yaml",
        "config.dalton.yaml",
        "src/dalton/nested.dalton.yaml",
        "docker-compose.dalton.yml",
        "web/.env.dalton",
        "integrations/dalton-channel-gateway",
        "deploy/dalton",
    ):
        assert not (out_dir / excluded).exists(), f"Excluded path leaked: {excluded}"

    assert not list(out_dir.rglob("*.dalton.yaml"))

    # 2. Explicit `--include-ci` export also includes docker-compose.ci.yml + tests/fakes + tests/standalone
    ci_out = tmp_path / "dist-ci"
    subprocess.run(
        [
            "bash",
            str(PACKAGE_SCRIPT),
            "--repo",
            str(repo),
            "--ref",
            "HEAD",
            "--include-ci",
            "--output",
            str(ci_out),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=True,
    )
    for expected_ci in (
        "docker-compose.ci.yml",
        "tests/fakes/fake_openai_server.py",
        "tests/standalone/run_checks.sh",
        "Makefile",
        "scripts/configure.sh",
    ):
        assert (ci_out / expected_ci).exists(), f"Missing CI export file: {expected_ci}"
    assert not (ci_out / "plugins/dalton_data").exists()


def test_package_release_rejects_existing_and_unsafe_output_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("ok\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, timeout=15)
    subprocess.run(
        ["git", "config", "user.email", "ci@example.com"], cwd=str(repo), check=True, timeout=15
    )
    subprocess.run(["git", "config", "user.name", "CI"], cwd=str(repo), check=True, timeout=15)
    subprocess.run(["git", "add", "."], cwd=str(repo), check=True, timeout=15)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(repo), check=True, timeout=15)

    # 1. Existing target directory is refused and untouched
    existing = tmp_path / "existing-dist"
    existing.mkdir()
    sentinel = existing / "keep.txt"
    sentinel.write_text("do-not-delete\n", encoding="utf-8")
    res_exists = subprocess.run(
        ["bash", str(PACKAGE_SCRIPT), "--repo", str(repo), "--output", str(existing)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res_exists.returncode != 0
    assert "already exists" in res_exists.stderr.lower()
    assert sentinel.read_text(encoding="utf-8") == "do-not-delete\n"

    # 2. Unsafe paths: empty, '.', '/', repo root, $HOME, and their parents
    fake_home = tmp_path / "home_parent" / "fake_home"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))

    for unsafe in (
        "",
        ".",
        "/",
        str(repo),
        str(repo.parent),
        str(fake_home),
        str(fake_home.parent),
    ):
        res = subprocess.run(
            ["bash", str(PACKAGE_SCRIPT), "--repo", str(repo), "--output", unsafe],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert res.returncode != 0, f"package_release.sh must reject unsafe --output={unsafe!r}"


def test_run_checks_refuses_existing_project_containers_or_volumes(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    checks_dir = tmp_path / "checks"
    artifacts_dir = tmp_path / "artifacts"
    fake_bin = tmp_path / "bin"
    for d in (work_dir, checks_dir, artifacts_dir, fake_bin):
        d.mkdir()

    for name in ("docker-compose.yml", "docker-compose.core.yml", "docker-compose.ci.yml"):
        (work_dir / name).write_text("services: {}\n", encoding="utf-8")
    (work_dir / ".env").write_text("APP_ENV=core\n", encoding="utf-8")
    (checks_dir / "_lib.py").write_text(
        (CHECKS_DIR / "_lib.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (checks_dir / "b1_stub.py").write_text("# auth: oss\n", encoding="utf-8")

    call_log = tmp_path / "docker_calls.log"
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$@" >> "{call_log}"\n'
        'if [[ "$1" == "ps" ]]; then\n'
        '  echo "deadbeef1234"\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1" == "volume" ]]; then\n'
        "  exit 0\n"
        "fi\n"
        "exit 0\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    res = subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "tests/standalone/run_checks.sh"),
            "--work-dir",
            str(work_dir),
            "--checks-dir",
            str(checks_dir),
            "--ci-compose",
            str(work_dir / "docker-compose.ci.yml"),
            "--artifacts-dir",
            str(artifacts_dir),
            "--project-name",
            "existing-prod-stack",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert res.returncode != 0
    assert "existing-prod-stack" in res.stderr
    logged = call_log.read_text(encoding="utf-8")
    assert "down" not in logged, f"run_checks.sh must never call compose down on existing project: {logged}"
