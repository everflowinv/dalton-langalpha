"""Unit tests for Standalone Core blind-trial fixes.

Covers:
1. Release tarball gate (`scripts/standalone/verify_release_tarball.py`) rejecting
   `._*` AppleDouble entries, `.DS_Store`, `LIBARCHIVE.xattr.*` / `SCHILY.xattr.*`
   pax headers, and missing single top-level directory.
2. `init.sh` failing non-zero when sandbox image build fails (and succeeding with
   `--skip-sandbox-build`), plus `-p` / `--env-file .env` in Next Steps.
3. `docker-compose.core.yml` declaring `build:` only on `backend` among the four
   backend-image services (`backend`, `crawler`, `migrate`, `minio-init`).
4. Overlay provider display names: `xai` -> `xAI (Grok)` and `doubao-coding` ->
   `Doubao Coding Plan` (distinct from `volcengine` -> `Doubao`).
5. `dalton_monitor` returning a full clickable URL and Markdown link when
   `PUBLIC_APP_URL` is configured.
6. `GET /api/v1/dalton/capabilities` exposing `ticker_logos`.
7. `STANDALONE-DEPLOY.md` and `.dockerignore` invariants.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
VERIFY_SCRIPT = REPO_ROOT / "scripts" / "standalone" / "verify_release_tarball.py"


def _load_verify_module():
    spec = importlib.util.spec_from_file_location("verify_release_tarball", VERIFY_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _build_tarball(
    dest: Path,
    entries: list[tuple[str, bytes, dict[str, str] | None]],
    *,
    global_pax: dict[str, str] | None = None,
) -> Path:
    with tarfile.open(dest, mode="w:gz", format=tarfile.PAX_FORMAT, pax_headers=global_pax or {}) as tf:
        for name, data, pax in entries:
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            info.mode = 0o644
            if pax:
                info.pax_headers = dict(pax)
            tf.addfile(info, io.BytesIO(data))
    return dest


class TestReleaseTarballGate:
    """Gate rejects macOS AppleDouble files, .DS_Store, and xattr pax headers."""

    def test_accepts_clean_tarball_with_single_top_dir(self, tmp_path: Path) -> None:
        verify = _load_verify_module()
        tar_path = _build_tarball(
            tmp_path / "clean.tar.gz",
            [
                ("dalton-core-abcdef123/docker-compose.core.yml", b"services: {}\n", None),
                ("dalton-core-abcdef123/docs-site/src/content/docs/404.md", b"---\ntitle: 404\n---\n", None),
            ],
        )
        assert verify.inspect_tarball(tar_path, expected_top_dir="dalton-core-abcdef123") == []

        res = subprocess.run(
            [
                sys.executable,
                str(VERIFY_SCRIPT),
                "--expected-top-dir",
                "dalton-core-abcdef123",
                str(tar_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, res.stderr
        assert "OK:" in res.stdout

    @pytest.mark.parametrize(
        "bad_member",
        [
            "dalton-core-abcdef123/._x",
            "dalton-core-abcdef123/docs-site/src/content/docs/._404.md",
            "dalton-core-abcdef123/.DS_Store",
            "dalton-core-abcdef123/web/.DS_Store",
        ],
    )
    def test_rejects_appledouble_and_ds_store_members(
        self, tmp_path: Path, bad_member: str
    ) -> None:
        verify = _load_verify_module()
        tar_path = _build_tarball(
            tmp_path / "dirty.tar.gz",
            [
                ("dalton-core-abcdef123/README.md", b"# Dalton\n", None),
                (bad_member, b"\x00\x05\x16\x07", None),
            ],
        )
        violations = verify.inspect_tarball(tar_path, expected_top_dir="dalton-core-abcdef123")
        assert violations, f"Expected violation for {bad_member}"
        assert any(bad_member in v for v in violations)

        res = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), str(tar_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 1
        assert bad_member in res.stderr

    @pytest.mark.parametrize(
        "pax_key",
        [
            "LIBARCHIVE.xattr.com.apple.provenance",
            "LIBARCHIVE.xattr.com.apple.quarantine",
            "SCHILY.xattr.user.dalton",
            "SCHILY.fflags",
            "LIBARCHIVE.fflags",
            "LIBARCHIVE.creationtime",
        ],
    )
    def test_rejects_xattr_and_macos_metadata_pax_headers(
        self, tmp_path: Path, pax_key: str
    ) -> None:
        verify = _load_verify_module()
        tar_path = _build_tarball(
            tmp_path / "xattr.tar.gz",
            [
                (
                    "dalton-core-abcdef123/docs-site/src/content/docs/404.md",
                    b"---\ntitle: 404\n---\n",
                    {pax_key: "opaque-value"},
                ),
            ],
        )
        violations = verify.inspect_tarball(tar_path, expected_top_dir="dalton-core-abcdef123")
        assert any(pax_key in v for v in violations)

        res = subprocess.run(
            [sys.executable, str(VERIFY_SCRIPT), str(tar_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 1
        assert pax_key in res.stderr

    def test_rejects_global_xattr_pax_header(self, tmp_path: Path) -> None:
        verify = _load_verify_module()
        tar_path = _build_tarball(
            tmp_path / "global_xattr.tar.gz",
            [("dalton-core-abcdef123/README.md", b"# ok\n", None)],
            global_pax={"LIBARCHIVE.xattr.com.apple.provenance": "global-val"},
        )
        violations = verify.inspect_tarball(tar_path, expected_top_dir="dalton-core-abcdef123")
        assert any("LIBARCHIVE.xattr.com.apple.provenance" in v for v in violations)

    def test_rejects_missing_expected_top_dir(self, tmp_path: Path) -> None:
        verify = _load_verify_module()
        tar_path = _build_tarball(
            tmp_path / "flat.tar.gz",
            [("docker-compose.core.yml", b"services: {}\n", None)],
        )
        violations = verify.inspect_tarball(tar_path, expected_top_dir="dalton-core-abcdef123")
        assert any("expected 'dalton-core-abcdef123'" in v for v in violations)

    def test_rejects_non_directory_top_level_entry_matching_expected_name(
        self, tmp_path: Path
    ) -> None:
        verify = _load_verify_module()
        tar_path = _build_tarball(
            tmp_path / "file_as_top.tar.gz",
            [("dalton-core-abcdef123", b"not a directory\n", None)],
        )
        violations = verify.inspect_tarball(tar_path, expected_top_dir="dalton-core-abcdef123")
        assert any("must be a directory" in v for v in violations)

    def test_accepts_implicit_top_dir_and_rejects_empty_dir_only_archive(
        self, tmp_path: Path
    ) -> None:
        verify = _load_verify_module()
        # Archive without explicit DIRTYPE entry (only nested files) is recognized and accepted.
        implicit_tar = _build_tarball(
            tmp_path / "implicit_top.tar.gz",
            [("dalton-core-abcdef123/README.md", b"# Dalton\n", None)],
        )
        assert verify.inspect_tarball(implicit_tar, expected_top_dir="dalton-core-abcdef123") == []

        # Archive with explicit top-level directory entry AND nested file is accepted.
        explicit_tar = tmp_path / "explicit_top.tar.gz"
        with tarfile.open(explicit_tar, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
            dir_info = tarfile.TarInfo(name="dalton-core-abcdef123")
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o755
            tf.addfile(dir_info)
            file_data = b"# Dalton\n"
            file_info = tarfile.TarInfo(name="dalton-core-abcdef123/README.md")
            file_info.size = len(file_data)
            file_info.mode = 0o644
            tf.addfile(file_info, io.BytesIO(file_data))
        assert verify.inspect_tarball(explicit_tar, expected_top_dir="dalton-core-abcdef123") == []

        # Archive with ONLY an empty top-level directory entry has no release files -> rejected.
        empty_dir_tar = tmp_path / "empty_dir_only.tar.gz"
        with tarfile.open(empty_dir_tar, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
            dir_info = tarfile.TarInfo(name="dalton-core-abcdef123")
            dir_info.type = tarfile.DIRTYPE
            dir_info.mode = 0o755
            tf.addfile(dir_info)
        violations = verify.inspect_tarball(empty_dir_tar, expected_top_dir="dalton-core-abcdef123")
        assert any("contains no release files" in v for v in violations)

        # Archive with only empty subdirectories (no actual files) -> rejected.
        empty_subdir_tar = tmp_path / "empty_subdir_only.tar.gz"
        with tarfile.open(empty_subdir_tar, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
            for dname in ("dalton-core-abcdef123", "dalton-core-abcdef123/empty_sub"):
                dinfo = tarfile.TarInfo(name=dname)
                dinfo.type = tarfile.DIRTYPE
                dinfo.mode = 0o755
                tf.addfile(dinfo)
        subdir_violations = verify.inspect_tarball(
            empty_subdir_tar, expected_top_dir="dalton-core-abcdef123"
        )
        assert any("contains no release files" in v for v in subdir_violations)

        # Backslash is NOT a POSIX tar separator -> `dalton-core-abcdef123\README.md` sits at top level -> rejected.
        backslash_tar = _build_tarball(
            tmp_path / "backslash_top.tar.gz",
            [(r"dalton-core-abcdef123\README.md", b"# fake\n", None)],
        )
        bs_violations = verify.inspect_tarball(
            backslash_tar, expected_top_dir="dalton-core-abcdef123"
        )
        assert any("expected 'dalton-core-abcdef123'" in v for v in bs_violations)

    def test_package_release_with_external_repo_missing_validator_succeeds(
        self, tmp_path: Path
    ) -> None:
        """package_release.sh resolves verify_release_tarball.py relative to itself, not --repo."""
        repo = tmp_path / "external-repo"
        repo.mkdir()
        required_files = [
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
            ".env.example",
            ".env.core.example",
            ".dockerignore",
            "Dockerfile.sandbox",
            "src/dalton/dalton.core.yaml",
            "libs/market-protocol/README.md",
            "libs/chartkit/README.md",
            "libs/edgar-engine/README.md",
            "migrations/README.md",
            "mcp_servers/README.md",
            "plugins/README.md",
            "plugins/alternative_data/README.md",
            "plugins/dalton/README.md",
            "plugins/firecrawl/README.md",
            "plugins/langalpha_deliverables/README.md",
            "plugins/langalpha_market_data/README.md",
            "plugins/langalpha_research/README.md",
            "plugins/langalpha_service/README.md",
            "plugins/yfinance/README.md",
            "workflows/README.md",
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
        ]
        for rel in required_files:
            p = repo / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("ok\n", encoding="utf-8")

        # Explicitly verify the target repo does NOT contain verify_release_tarball.py
        assert not (repo / "scripts/standalone/verify_release_tarball.py").exists()

        subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo), check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(repo), check=True)
        subprocess.run(["git", "add", "."], cwd=str(repo), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=str(repo), check=True)

        tar_path = tmp_path / "external-release.tar.gz"
        proc = subprocess.run(
            [
                "bash",
                str(REPO_ROOT / "scripts/standalone/package_release.sh"),
                "--repo",
                str(repo),
                "--ref",
                "HEAD",
                "--tar",
                str(tar_path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        assert tar_path.is_file() and tar_path.stat().st_size > 0
        assert "[verify_release_tarball] OK:" in proc.stdout

    def test_dockerignore_and_edge_dockerfile_drop_macos_metadata(self) -> None:
        dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
        assert "**/._*" in dockerignore
        assert "**/.DS_Store" in dockerignore

        edge_df = (REPO_ROOT / "deploy/standalone/Dockerfile.edge").read_text(encoding="utf-8")
        assert "._*" in edge_df and ".DS_Store" in edge_df

        pkg_sh = (REPO_ROOT / "scripts/standalone/package_release.sh").read_text(encoding="utf-8")
        assert "export COPYFILE_DISABLE=1" in pkg_sh
        assert "--no-xattrs --no-mac-metadata --no-fflags" in pkg_sh
        assert '"${SCRIPT_DIR}/verify_release_tarball.py"' in pkg_sh


class TestInitAndComposeSingleBuild:
    """Verify init.sh sandbox-build exit code, Next Steps flags, and single backend build."""

    def test_init_exits_nonzero_on_sandbox_build_failure_and_passes_with_skip(
        self, tmp_path: Path
    ) -> None:
        inst = tmp_path / "dalton"
        (inst / "scripts" / "standalone").mkdir(parents=True)
        init_dst = inst / "scripts" / "standalone" / "init.sh"
        init_dst.write_text(
            (REPO_ROOT / "scripts" / "standalone" / "init.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        init_dst.chmod(0o755)
        (inst / "scripts" / "standalone" / "env_tokens.sh").write_text(
            (REPO_ROOT / "scripts" / "standalone" / "env_tokens.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (inst / "scripts" / "standalone" / "_compose_env.sh").write_text(
            (REPO_ROOT / "scripts" / "standalone" / "_compose_env.sh").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (inst / ".env.core.example").write_text(
            (REPO_ROOT / ".env.core.example").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        (inst / "Dockerfile.sandbox").write_text("FROM scratch\n", encoding="utf-8")

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(
            "#!/usr/bin/env bash\n"
            'if [[ "$1" == "version" ]]; then echo "28.0.1"; exit 0; fi\n'
            'if [[ "$1" == "compose" && "$2" == "version" ]]; then echo "2.27.0"; exit 0; fi\n'
            'if [[ "$1" == "image" && "$2" == "inspect" ]]; then exit 1; fi\n'
            'if [[ "$1" == "build" ]]; then echo "429 Too Many Requests" >&2; exit 1; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        (bin_dir / "docker").chmod(0o755)

        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"

        # Without --skip-sandbox-build: docker build failure must fail init.sh with non-zero exit
        res_fail = subprocess.run(
            ["bash", str(init_dst), "--non-interactive", "--domain", "localhost"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res_fail.returncode != 0
        assert "Sandbox image build failed" in res_fail.stderr
        assert "Dockerfile.sandbox" in res_fail.stderr

        # With --skip-sandbox-build: succeeds and Next Steps uses -p + --env-file .env without hardcoded VITE_DALTON_AUTH
        res_skip = subprocess.run(
            [
                "bash",
                str(init_dst),
                "--non-interactive",
                "--skip-sandbox-build",
                "--domain",
                "localhost",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert res_skip.returncode == 0, res_skip.stderr
        assert "env -u VITE_DALTON_AUTH docker compose -p dalton -f docker-compose.yml -f docker-compose.core.yml --env-file .env build" in res_skip.stdout
        assert "docker compose -p dalton -f docker-compose.yml -f docker-compose.core.yml --env-file .env up -d" in res_skip.stdout
        assert "--build-arg VITE_DALTON_AUTH=" not in res_skip.stdout

    def test_compose_core_only_backend_declares_backend_image_build(self) -> None:
        class _ComposeLoader(yaml.SafeLoader):
            pass

        def _passthrough(loader: yaml.SafeLoader, node: yaml.Node) -> Any:
            if isinstance(node, yaml.MappingNode):
                return loader.construct_mapping(node)
            if isinstance(node, yaml.SequenceNode):
                return loader.construct_sequence(node)
            return loader.construct_scalar(node)

        _ComposeLoader.add_constructor("!reset", _passthrough)
        _ComposeLoader.add_constructor("!override", _passthrough)

        cfg = yaml.load(
            (REPO_ROOT / "docker-compose.core.yml").read_text(encoding="utf-8"),
            Loader=_ComposeLoader,
        )
        services = cfg["services"]
        assert "build" in services["backend"]
        for svc_name in ("crawler", "migrate", "minio-init"):
            assert "build" not in services[svc_name], f"{svc_name} must reuse backend image without build:"
            assert services[svc_name]["image"] == "${DALTON_BACKEND_IMAGE:-dalton-core-backend:local}"


class TestOverlayProvidersAndMonitorLinks:
    """Verify provider names, clickable monitor URLs, capabilities payload, and manual."""

    @pytest.mark.parametrize("app_env", ["core", "dalton"])
    def test_provider_display_names_fix_spacexsi_and_doubao_duplicate(
        self, monkeypatch: pytest.MonkeyPatch, app_env: str
    ) -> None:
        from src.dalton.llm.overlay import apply_model_overlay
        from src.llms.llm import ModelConfig

        monkeypatch.setenv("APP_ENV", app_env)
        monkeypatch.delenv("DALTON_PROFILE", raising=False)

        llm_cfg = json.loads(
            (REPO_ROOT / "src/llms/manifest/models.json").read_text(encoding="utf-8")
        )
        manifest = json.loads(
            (REPO_ROOT / "src/llms/manifest/providers.json").read_text(encoding="utf-8")
        )
        apply_model_overlay(llm_cfg, manifest)
        flat = ModelConfig._flatten_providers(manifest["provider_config"])

        assert flat["xai"]["display_name"] == "xAI (Grok)"
        assert flat["volcengine"]["display_name"] == "Doubao"
        assert flat["doubao-coding"]["display_name"] == "Doubao Coding Plan"
        assert flat["volcengine"]["display_name"] != flat["doubao-coding"]["display_name"]

    def test_monitor_result_returns_relative_escaped_markdown_link_and_docstring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.tools.monitor_tools import (
            _format_markdown_link,
            _format_monitor_result,
            build_dalton_monitor_tool,
        )

        detail = {
            "monitor": {
                "slug": "aapl-fy26-10k",
                "title": "Apple [Q4] 财报研究",
                "visibility": "own",
                "category": "company",
                "published_seq": 1,
                "keep_updated": False,
                "updated_at": "2026-11-24T15:00:00Z",
            },
            "versions": [{"published_at": "2026-11-24T15:00:00Z"}],
        }

        # Even when PUBLIC_APP_URL matches SERVER_BASE_URL (standard Standalone Core single-origin layout),
        # in-app links stay same-origin relative while external notifications get the absolute public_url.
        monkeypatch.delenv("DALTON_FRONTEND_URL", raising=False)
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        monkeypatch.setenv("SERVER_BASE_URL", "https://dalton.example.com")
        monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com/")

        res = json.loads(_format_monitor_result("publish", detail))
        assert res["ok"] is True
        assert res["path"] == "/monitors/aapl-fy26-10k"
        assert res["url"] == "/monitors/aapl-fy26-10k"
        assert res["markdown_link"] == r"[Apple \[Q4\] 财报研究](/monitors/aapl-fy26-10k)"
        assert res["public_url"] == "https://dalton.example.com/monitors/aapl-fy26-10k"
        assert (
            res["public_markdown_link"]
            == r"[Apple \[Q4\] 财报研究](https://dalton.example.com/monitors/aapl-fy26-10k)"
        )

        # Parentheses in slug/href and brackets in title are safely escaped.
        detail_parens = {
            **detail,
            "monitor": {**detail["monitor"], "slug": "aapl-(q4)", "title": "NVDA [FY26] (Live)"},
        }
        res_parens = json.loads(_format_monitor_result("publish", detail_parens))
        assert res_parens["url"] == "/monitors/aapl-%28q4%29"
        assert res_parens["markdown_link"] == r"[NVDA \[FY26\] (Live)](/monitors/aapl-%28q4%29)"
        assert res_parens["public_url"] == "https://dalton.example.com/monitors/aapl-%28q4%29"
        assert _format_markdown_link("A]B", "/monitors/x)y") == r"[A\]B](/monitors/x%29y)"

        # When no public frontend URL is configured, public_url fields are None.
        monkeypatch.delenv("PUBLIC_APP_URL", raising=False)
        res_no_public = json.loads(_format_monitor_result("publish", detail))
        assert res_no_public["public_url"] is None
        assert res_no_public["public_markdown_link"] is None

        # Tool docstring explicitly instructs the model to paste markdown_link and documents public_url.
        tool_obj = build_dalton_monitor_tool()
        assert "markdown_link" in (tool_obj.description or "")
        assert "public_url" in (tool_obj.description or "")

    def test_notify_formatter_and_gateway_fallback_rewrite_monitor_links(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.notify.dispatch import (
            _strip_non_http_markdown_links,
            rewrite_monitor_links_for_external,
        )

        monkeypatch.delenv("DALTON_FRONTEND_URL", raising=False)
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        # Single-origin Core setup: PUBLIC_APP_URL == SERVER_BASE_URL
        monkeypatch.setenv("SERVER_BASE_URL", "https://dalton.example.com")
        monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com/")

        text = (
            r"详见 [Apple \[Q4\] 财报](/monitors/aapl-fy26-10k) 与 [本地草稿](output/draft.md)。"
        )
        assert (
            _strip_non_http_markdown_links(text)
            == r"详见 [Apple \[Q4\] 财报](https://dalton.example.com/monitors/aapl-fy26-10k) 与 本地草稿。"
        )
        assert (
            rewrite_monitor_links_for_external(text)
            == r"详见 [Apple \[Q4\] 财报](https://dalton.example.com/monitors/aapl-fy26-10k) 与 [本地草稿](output/draft.md)。"
        )

        # Without public URL configured, preserve both link title and path text.
        monkeypatch.delenv("PUBLIC_APP_URL", raising=False)
        assert (
            _strip_non_http_markdown_links(r"查看 [Apple \[Q4\]](/monitors/aapl-fy26-10k)")
            == "查看 Apple [Q4] (/monitors/aapl-fy26-10k)"
        )
        assert (
            rewrite_monitor_links_for_external(r"查看 [Apple \[Q4\]](/monitors/aapl-fy26-10k)")
            == "查看 Apple [Q4] (/monitors/aapl-fy26-10k)"
        )

    @pytest.mark.asyncio
    async def test_automation_delivery_finish_run_rewrites_monitor_links_for_gateway(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.notify.dispatch import truncate_preserving_markdown_links
        from src.server.services import automation_delivery

        captured: dict[str, Any] = {}

        async def _fake_gateway_request(method: str, path: str, **kwargs: Any):
            captured["method"] = method
            captured["path"] = path
            captured["body"] = kwargs.get("body")
            return type("Resp", (), {"status": 200, "data": {"targets": []}})()

        monkeypatch.setattr(
            automation_delivery.messaging, "gateway_request", _fake_gateway_request
        )
        monkeypatch.delenv("DALTON_FRONTEND_URL", raising=False)
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        monkeypatch.setenv("SERVER_BASE_URL", "https://dalton.example.com")
        monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com")

        await automation_delivery.finish_run(
            {"user_id": "u-1"},
            "exec-1",
            "completed",
            targets=[],
            final_text="监控已更新：[Apple 财报](/monitors/aapl-fy26-10k)",
        )
        assert (
            captured["body"]["final_text"]
            == "监控已更新：[Apple 财报](https://dalton.example.com/monitors/aapl-fy26-10k)"
        )

        # Link-safe truncation: expanding a relative monitor link past 20,000 UTF-16 units never cuts mid-link.
        def _utf16_len(s: str) -> int:
            return len(s.encode("utf-16-le")) // 2

        link = "[Apple 财报](/monitors/aapl-fy26-10k)"
        near_limit = "A" * (20_000 - len(link)) + link
        await automation_delivery.finish_run(
            {"user_id": "u-1"},
            "exec-long",
            "completed",
            targets=[],
            final_text=near_limit,
        )
        truncated = captured["body"]["final_text"]
        assert _utf16_len(truncated) <= 20_000
        assert truncated == "A" * (20_000 - len(link)) + "…"
        assert "[Apple 财报](" not in truncated
        assert "https://dalton.example.com" not in truncated

        # Emoji-heavy text: Python codepoint length <= 20,000, but UTF-16 length > 20,000 after URL expansion.
        emoji_body = "📊" * 1_000 + "A" * 17_950 + link
        assert len(emoji_body) < 20_000
        await automation_delivery.finish_run(
            {"user_id": "u-1"},
            "exec-emoji",
            "completed",
            targets=[],
            final_text=emoji_body,
        )
        emoji_truncated = captured["body"]["final_text"]
        assert _utf16_len(emoji_truncated) <= 20_000
        assert emoji_truncated == "📊" * 1_000 + "A" * 17_950 + "…"
        assert "[Apple 财报](" not in emoji_truncated
        assert "https://dalton.example.com" not in emoji_truncated

        # Direct unit checks on truncate_preserving_markdown_links (UTF-16 & leading oversized link)
        sample = "prefix [Title](https://example.com/monitors/aapl) suffix"
        assert truncate_preserving_markdown_links(sample, _utf16_len(sample)) == sample
        assert truncate_preserving_markdown_links(sample, 20) == "prefix…"
        assert (
            truncate_preserving_markdown_links(
                "x [A](https://example.com/a) y [B](https://example.com/b)", 35
            )
            == "x [A](https://example.com/a) y…"
        )
        # Leading oversized link at index 0 preserves plain title text instead of cutting inside syntax
        leading = "[Apple 📊 FY26 财报](https://dalton.example.com/monitors/very-long-slug)"
        assert truncate_preserving_markdown_links(leading, 25) == "Apple 📊 FY26 财报…"
        assert _utf16_len(truncate_preserving_markdown_links(leading, 25)) <= 25
        # Leading oversized link whose title itself exceeds max_chars slices by UTF-16 cleanly
        leading_tight = truncate_preserving_markdown_links(leading, 12)
        assert _utf16_len(leading_tight) <= 12
        assert leading_tight == "Apple 📊 FY…"
        assert "](" not in leading_tight and "[" not in leading_tight
        # Whitespace-only prefix before oversized link also falls back to title
        assert (
            truncate_preserving_markdown_links(
                "   [NVDA 深度](https://dalton.example.com/monitors/nvda-long-slug)", 20
            )
            == "NVDA 深度…"
        )

        monkeypatch.delenv("PUBLIC_APP_URL", raising=False)
        await automation_delivery.finish_run(
            {"user_id": "u-1"},
            "exec-2",
            "completed",
            targets=[],
            final_text="监控已更新：[Apple 财报](/monitors/aapl-fy26-10k)",
        )
        assert (
            captured["body"]["final_text"]
            == "监控已更新：Apple 财报 (/monitors/aapl-fy26-10k)"
        )

    @pytest.mark.asyncio
    async def test_monitor_list_action_includes_relative_markdown_link(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.dalton.tools import monitor_tools

        monkeypatch.setattr(
            monitor_tools,
            "get_dalton_settings",
            lambda: type("S", (), {"enabled": True})(),
        )
        monkeypatch.setattr(monitor_tools, "is_artifact_publishing_enabled", lambda: True)
        monkeypatch.setattr(
            monitor_tools,
            "_extract_run_identity",
            lambda _cfg: ("u-1", "ws-1", "th-1", None),
        )

        async def _fake_list(_uid: str):
            return [
                {
                    "slug": "nvda-(q4)",
                    "title": "NVDA [Q4] Monitor",
                    "visibility": "own",
                    "category": "company",
                    "source_kind": "workspace",
                    "published_seq": 2,
                    "updated_at": "2026-11-24T15:00:00Z",
                    "keep_updated": False,
                    "can_manage": True,
                }
            ]

        monkeypatch.delenv("DALTON_FRONTEND_URL", raising=False)
        monkeypatch.delenv("FRONTEND_URL", raising=False)
        monkeypatch.delenv("SERVER_BASE_URL", raising=False)
        monkeypatch.setenv("PUBLIC_APP_URL", "https://dalton.example.com")

        monkeypatch.setattr(monitor_tools.monitor_service, "list_monitors_for_user", _fake_list)
        tool_obj = monitor_tools.build_dalton_monitor_tool()
        raw = await tool_obj.ainvoke({"action": "list"}, config={})
        data = json.loads(raw)
        assert data["ok"] is True
        assert data["count"] == 1
        item = data["monitors"][0]
        assert item["url"] == "/monitors/nvda-%28q4%29"
        assert item["markdown_link"] == r"[NVDA \[Q4\] Monitor](/monitors/nvda-%28q4%29)"
        assert item["public_url"] == "https://dalton.example.com/monitors/nvda-%28q4%29"
        assert (
            item["public_markdown_link"]
            == r"[NVDA \[Q4\] Monitor](https://dalton.example.com/monitors/nvda-%28q4%29)"
        )

    def test_capabilities_payload_exposes_ticker_logos_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import src.config.env as env_mod
        from src.dalton.capabilities import get_capabilities_payload

        monkeypatch.setenv("APP_ENV", "core")
        monkeypatch.delenv("DALTON_PROFILE", raising=False)
        monkeypatch.setattr(env_mod, "GINLIX_DATA_ENABLED", False)
        payload = get_capabilities_payload()
        assert payload["ticker_logos"] is False
        assert "claude_code_available" not in payload

        monkeypatch.setattr(env_mod, "GINLIX_DATA_ENABLED", True)
        payload_on = get_capabilities_payload()
        assert payload_on["ticker_logos"] is True

    def test_standalone_manual_and_backup_hint_cover_all_trial_items(self) -> None:
        doc = (REPO_ROOT / "docs/dalton/dev-guide/STANDALONE-DEPLOY.md").read_text(
            encoding="utf-8"
        )
        assert "--strip-components=1 -C /opt/dalton" in doc
        assert "部署在 `/opt/dalton` 时默认为 `dalton`" in doc
        assert "env -u VITE_DALTON_AUTH docker compose" in doc
        assert "http://127.0.0.1:8088/setup/admin" in doc
        assert "设置 → 团队管理 → 成员与权限" in doc
        assert "管理员" in doc and "内部成员" in doc and "外部成员" in doc
        assert "workspace_files" in doc
        assert "备份文件到数据库" in doc
        assert "未备份清单" in doc
        assert "100 MiB" in doc
        assert "docker ps -a --filter name=langalpha-sandbox-" in doc
        assert "C='<沙箱容器名>'" in doc
        assert "SRC='<容器内路径>'" in doc
        assert "DST='<宿主机目标路径>'" in doc
        assert 'docker exec "$C" find /home/workspace -type f -size +100M -print' in doc
        assert "前 5 条" in doc
        assert (
            'mkdir -p "$(dirname "$DST")" && docker cp "$C:$SRC" "$DST" && '
            'SRC_BYTES=$(docker exec "$C" stat -c %s "$SRC") && '
            'DST_BYTES=$(stat -c %s "$DST") && '
            'test -n "$SRC_BYTES" && test "$SRC_BYTES" = "$DST_BYTES" && '
            'echo "一致：$SRC_BYTES 字节"'
        ) in doc
        assert "看到「一致」再处理下一个文件" in doc
        assert "while IFS= read -r rel" not in doc
        assert "拉取 Docker Hub 镜像被限流（429）" in doc
        assert "registry-mirrors" in doc and "docker login" in doc
        assert "daemon.json.bak" in doc
        validate_cmd = (
            "sudo python3 -c 'import json, sys; sys.exit(None if "
            'isinstance(json.load(open(sys.argv[1])), dict) else "JSON 顶层必须是对象")\' '
            "/etc/docker/daemon.json"
        )
        assert validate_cmd in doc
        # JSON validation and Docker restart must be in separate code blocks
        idx_validate = doc.index(validate_cmd)
        idx_restart = doc.index("sudo systemctl restart docker")
        assert "```" in doc[idx_validate:idx_restart]
        # Backup retention sentence stays inside §7.8, and §7.9 Claude Code 404 note stands alone at the end
        idx_78 = doc.index("### 7.8 磁盘空间增长较快")
        idx_retain = doc.index("backup.sh --retain <N>")
        idx_79 = doc.index("### 7.9 浏览器开发者工具出现 `/api/v1/claude-code/setup` 返回 404")
        assert idx_78 < idx_retain < idx_79
        assert "/api/v1/claude-code/setup" in doc[idx_79:]
        assert "backup.sh --retain" not in doc[idx_79:]
        assert "apt-get install -y jq" not in doc
        assert "daemon.json.tmp" not in doc
        assert "| unique" not in doc

        backup_sh = (REPO_ROOT / "scripts/standalone/backup.sh").read_text(encoding="utf-8")
        assert '--env-passphrase-file "$ENV_PASSPHRASE_FILE"' in backup_sh

        design_doc = (REPO_ROOT / "docs/dalton/features/standalone-core.md").read_text(
            encoding="utf-8"
        )
        assert "COMPOSE_PROJECT_NAME" in design_doc
        assert 'docker compose -p "${PROJECT_NAME}"' in design_doc
        assert "docker compose -p dalton " not in design_doc

        ci_yml = (REPO_ROOT / ".github/workflows/standalone-core-e2e.yml").read_text(
            encoding="utf-8"
        )
        assert "/tmp/dalton-core-customer.tar.gz" in ci_yml
        assert "/tmp/dalton-core-ci-test.tar.gz" in ci_yml
        assert "dalton-customer-smoke" in ci_yml
        assert "http://127.0.0.1:8000/health" in ci_yml

        b6_py = (REPO_ROOT / "tests/standalone/checks/b6_tls_backup_upgrade.py").read_text(
            encoding="utf-8"
        )
        assert "_build_same_version_release_tar" not in b6_py
        assert "--skip-build" not in b6_py
