"""Offline unit tests for the standalone check framework and release packager."""

from __future__ import annotations

import io
from pathlib import Path
import subprocess
import tarfile

import pytest

from tests.standalone.checks import _lib

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKS_DIR = REPO_ROOT / "tests" / "standalone" / "checks"
PACKAGE_SCRIPT = REPO_ROOT / "scripts" / "standalone" / "package_release.sh"


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


def test_scan_tar_stream_inspects_nested_layers() -> None:
    # Build a synthetic `docker save`-style archive containing two layer tars,
    # with a forbidden file in layer 1 even though layer 2 deletes/omits it.
    layer1_buf = io.BytesIO()
    with tarfile.open(fileobj=layer1_buf, mode="w") as l1:
        info = tarfile.TarInfo("app/scripts/dalton/researchdata_bridge.py")
        data = b"secret"
        info.size = len(data)
        l1.addfile(info, io.BytesIO(data))

    layer2_buf = io.BytesIO()
    with tarfile.open(fileobj=layer2_buf, mode="w") as l2:
        info = tarfile.TarInfo("app/server.py")
        data = b"ok"
        info.size = len(data)
        l2.addfile(info, io.BytesIO(data))

    outer_buf = io.BytesIO()
    with tarfile.open(fileobj=outer_buf, mode="w") as outer:
        for name, raw in (
            ("layer1/layer.tar", layer1_buf.getvalue()),
            ("layer2/layer.tar", layer2_buf.getvalue()),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(raw)
            outer.addfile(info, io.BytesIO(raw))

    hits = _lib.scan_tar_stream_for_paths(
        outer_buf.getvalue(),
        ("scripts/dalton/researchdata_bridge.py", "plugins/dalton_data/"),
    )
    assert hits == ["layer1/layer.tar:app/scripts/dalton/researchdata_bridge.py"]


def test_package_release_exports_clean_allowlist_and_excludes_proprietary(
    tmp_path: Path,
) -> None:
    # Build a self-contained synthetic git repo under tmp_path so the test is
    # hermetic (no git alternates dependency and zero writes to the workspace).
    repo = tmp_path / "repo"
    repo.mkdir()

    def _touch(rel: str, content: str = "ok\n") -> None:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    # All required allowlist paths + optional core paths
    for rel in (
        "LICENSE",
        "README.md",
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
    ):
        _touch(rel)

    # Seed a scan-term hit inside an exported file to verify the report catches it
    _touch("src/dalton/settings.py", 'CONTACT = "demo@everflowinv.com"\n')

    # Proprietary / Everflow-only files that MUST be excluded
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
        "docs-site/src/content/docs/everflow/internal.md",
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
    report_text = report_path.read_text(encoding="utf-8")
    assert "everflowinv" in report_text
    assert "src/dalton/settings.py" in report_text
    assert tar_path.is_file() and tar_path.stat().st_size > 0

    # Essential core files present
    for expected in (
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
        ".release_git_sha",
    ):
        assert (out_dir / expected).exists(), f"Missing expected core file: {expected}"

    # Excluded proprietary / Everflow paths absent
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
        "docs-site/src/content/docs/everflow",
        "integrations/dalton-channel-gateway",
        "deploy/dalton",
    ):
        assert not (out_dir / excluded).exists(), f"Excluded path leaked: {excluded}"

    # No *.dalton.yaml anywhere in exported tree
    leaked_dalton_yamls = list(out_dir.rglob("*.dalton.yaml"))
    assert not leaked_dalton_yamls, f"Leaked *.dalton.yaml: {leaked_dalton_yamls}"
