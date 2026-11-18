"""Offline unit tests for dalton-bdc-analyst skill."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec

import pytest
import requests
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SKILL_NAME = "dalton-bdc-analyst"
SKILL_DIR = REPO_ROOT / "plugins" / "dalton" / "skills" / SKILL_NAME
SCRIPTS_DIR = SKILL_DIR / "scripts"
TESTS_DIR = REPO_ROOT / "tests" / "unit" / "dalton"


def _load_script(module_name: str, filename: str):
    if str(SCRIPTS_DIR) not in sys.path:
        sys.path.insert(0, str(SCRIPTS_DIR))
    target = SCRIPTS_DIR / filename
    spec = importlib.util.spec_from_file_location(module_name, target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {target}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _reset_env_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    monkeypatch.delenv("EDGAR_IDENTITY", raising=False)


def _requests_get_sig(url: str, headers=None, timeout=None):
    return None


def _build_fake_arcc_soi_html(period_label: str, rows_data: list[tuple[str, str, float, float]]) -> bytes:
    """Construct a minimal 20-column ARCC-compatible SoI HTML document."""
    header_cells = ["<td>Portfolio Company</td>", "<td>Investment</td>", "<td>Amortized Cost</td>", "<td>Fair Value</td>"]
    header_cells.extend(f"<td>Col{i}</td>" for i in range(4, 20))
    header_html = "<tr>" + "".join(header_cells) + "</tr>"

    body_rows = []
    for company, invest, face_k, fair_k in rows_data:
        cells = [
            f"<td>{company}</td>",
            f"<td>{invest}</td>",
            f"<td>{face_k:,.0f}</td>",
            f"<td>{fair_k:,.0f}</td>",
        ]
        cells.extend("<td>-</td>" for _ in range(4, 20))
        body_rows.append("<tr>" + "".join(cells) + "</tr>")

    html = (
        "<html><body>"
        f"<p>Consolidated Schedule of Investments — As of {period_label}</p>"
        f"<table>{header_html}{''.join(body_rows)}</table>"
        "</body></html>"
    )
    return html.encode("utf-8")


# ---------------------------------------------------------------------------
# Registry, frontmatter, paths, layout, and ref checks
# ---------------------------------------------------------------------------


def test_registry_frontmatter_layout_and_refs() -> None:
    from ptc_agent.agent.middleware.skills.dalton_registry import (
        DALTON_SKILL_DEFINITIONS,
        _DALTON_SKILL_SPECS,
        read_skill_description,
    )
    from ptc_agent.agent.middleware.skills.registry import SKILL_REGISTRY

    assert (SKILL_NAME, None, ()) in _DALTON_SKILL_SPECS
    assert SKILL_NAME in DALTON_SKILL_DEFINITIONS
    assert SKILL_NAME in SKILL_REGISTRY

    skill_md = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert skill_md.startswith("---\n")
    end_idx = skill_md.find("\n---\n", 4)
    assert end_idx != -1
    meta = yaml.safe_load(skill_md[4:end_idx])
    assert set(meta) == {"name", "description"}
    assert meta["name"] == SKILL_NAME
    desc = meta["description"].strip()
    assert 0 < len(desc) <= 120
    assert "触发词" not in desc
    assert desc == read_skill_description(SKILL_NAME)

    for expected in (
        SCRIPTS_DIR / "harvest.py",
        SCRIPTS_DIR / "compare.py",
        SCRIPTS_DIR / "config.py",
        SCRIPTS_DIR / "bdc_analyzer.py",
        SCRIPTS_DIR / "normalize_names.py",
        SCRIPTS_DIR / "enrich_company_db.py",
        SCRIPTS_DIR / "state_store.py",
        SKILL_DIR / "references" / "VERIFIED_PARSERS.md",
        SKILL_DIR / "data" / "seed.tar.gz",
    ):
        assert expected.is_file(), f"Missing {expected}"

    profile_files = list((SCRIPTS_DIR / "bdc_profiles").glob("*.py"))
    assert len(profile_files) == 45

    for deprecated in (
        SKILL_DIR / "run.sh",
        SKILL_DIR / "requirements.txt",
        SKILL_DIR / "company_master.json",
        SCRIPTS_DIR / "run_bdc.py",
        SCRIPTS_DIR / "bdc_generic.py",
        SCRIPTS_DIR / "enrich_gics.py",
    ):
        assert not deprecated.exists(), f"Deprecated file should not exist: {deprecated}"

    dalton_paths = (REPO_ROOT / "scripts" / "dalton" / "dalton_paths.txt").read_text(encoding="utf-8")
    assert f"plugins/dalton/skills/{SKILL_NAME}/" in dalton_paths
    assert "tests/unit/dalton/test_bdc_analyst_skill.py" in dalton_paths

    sys.path.insert(0, str(TESTS_DIR))
    try:
        import test_skill_tool_calls as stc
    finally:
        sys.path.pop(0)
    registry = stc.build_tool_signature_registry()
    for md_path in sorted(SKILL_DIR.rglob("*.md")):
        rel_path = md_path.relative_to(stc.SKILLS_ROOT).as_posix()
        calls = stc.extract_tool_calls_from_markdown(md_path.read_text(encoding="utf-8"), registry)
        errors = stc.validate_extracted_calls(rel_path, calls, registry)
        assert not errors, f"Invalid tool calls in {rel_path}: {errors}"


# ---------------------------------------------------------------------------
# SEC identity precedence
# ---------------------------------------------------------------------------


def test_sec_identity_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    state_store = _load_script("dalton_bdc_state_store_test", "state_store.py")
    bdc_analyzer = _load_script("dalton_bdc_analyzer_test", "bdc_analyzer.py")
    _reset_env_identity(monkeypatch)

    ident, source = state_store.resolve_sec_identity()
    assert source == "default"
    assert ident == "DaltonResearch research@everflowinv.com"
    assert bdc_analyzer.get_headers() == {"User-Agent": "DaltonResearch research@everflowinv.com"}

    monkeypatch.setenv("EDGAR_IDENTITY", "EdgarUser edgar@example.com")
    ident, source = state_store.resolve_sec_identity()
    assert source == "EDGAR_IDENTITY"
    assert ident == "EdgarUser edgar@example.com"
    assert bdc_analyzer.get_headers() == {"User-Agent": "EdgarUser edgar@example.com"}

    monkeypatch.setenv("SEC_USER_AGENT", "DaltonBot research@example.com")
    ident, source = state_store.resolve_sec_identity()
    assert source == "SEC_USER_AGENT"
    assert ident == "DaltonBot research@example.com"
    assert bdc_analyzer.get_headers() == {"User-Agent": "DaltonBot research@example.com"}


# ---------------------------------------------------------------------------
# Missing --workspace-dir behavior
# ---------------------------------------------------------------------------


def test_harvest_errors_without_workspace_dir_and_compare_reads_seed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harvest = _load_script("dalton_bdc_harvest_test", "harvest.py")
    compare = _load_script("dalton_bdc_compare_test", "compare.py")

    seed_archive = SKILL_DIR / "data" / "seed.tar.gz"
    seed_before = seed_archive.read_bytes()

    # harvest.py CLI must fail without --workspace-dir
    monkeypatch.setattr(sys, "argv", ["harvest.py", "--ticker", "ARCC", "--period", "25Q4"])
    with pytest.raises(SystemExit) as exc:
        harvest.main()
    assert exc.value.code != 0
    assert "--workspace-dir" in capsys.readouterr().err

    # harvest_one programmatic call without workspace_dir also raises
    with pytest.raises(RuntimeError, match="--workspace-dir"):
        harvest.harvest_one("ARCC", "25Q4")

    # compare.py without --workspace-dir reads read-only seed and writes nothing
    monkeypatch.setattr(
        sys,
        "argv",
        ["compare.py", "--ticker", "ARCC", "--periodA", "25Q4", "--periodB", "25Q3", "--json"],
    )
    compare.main()
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    assert len(out) > 0
    assert out[0]["ticker"] == "ARCC"
    assert seed_archive.read_bytes() == seed_before
    assert not (SKILL_DIR / "company_db.json").exists()
    assert list((SKILL_DIR / "data").iterdir()) == [seed_archive]


# ---------------------------------------------------------------------------
# Seed extraction, non-overwrite, workspace precedence, and symlink escape
# ---------------------------------------------------------------------------


def test_seed_extraction_and_preserves_existing_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_store = _load_script("dalton_bdc_state_store_test", "state_store.py")
    compare = _load_script("dalton_bdc_compare_test", "compare.py")

    ws = tmp_path / "ws_seed"
    state_dir = ws / "state" / "dalton-bdc-analyst"
    data_dir = state_dir / "data"
    data_dir.mkdir(parents=True)

    # Pre-populate custom ARCC.json and custom company_db.json before first seed run
    custom_arcc = {
        "ticker": "ARCC",
        "periods": {
            "26Q2": {
                "period": "26Q2",
                "equity_usd": 1_000_000_000,
                "holdings": [
                    {"company": "CUSTOM BORROWER LLC", "face_m": 50.0, "fair_m": 40.0, "industry": "Tech", "sub_industry": "SaaS", "description": "Custom"}
                ],
            },
            "26Q1": {
                "period": "26Q1",
                "equity_usd": 1_000_000_000,
                "holdings": [
                    {"company": "CUSTOM BORROWER LLC", "face_m": 50.0, "fair_m": 48.0, "industry": "Tech", "sub_industry": "SaaS", "description": "Custom"}
                ],
            },
        },
    }
    (data_dir / "ARCC.json").write_text(json.dumps(custom_arcc), encoding="utf-8")
    custom_cdb = {"CUSTOM BORROWER LLC": {"industry": "Tech", "sub_industry": "SaaS", "description": "自定义"}}
    (state_dir / "company_db.json").write_text(json.dumps(custom_cdb), encoding="utf-8")

    state_store.ensure_workspace_state(ws)

    # Seeded missing files were extracted, existing files were NOT overwritten
    assert (data_dir / "BBDC.json").is_file()
    assert (data_dir / "_meta.json").is_file()
    assert json.loads((data_dir / "ARCC.json").read_text(encoding="utf-8")) == custom_arcc
    assert json.loads((state_dir / "company_db.json").read_text(encoding="utf-8")) == custom_cdb

    # compare with --workspace-dir reads workspace copy (26Q2 vs 26Q1 exists only in workspace)
    monkeypatch.setattr(
        sys,
        "argv",
        ["compare.py", "--workspace-dir", str(ws), "--ticker", "ARCC", "--periodA", "26Q2", "--periodB", "26Q1", "--json"],
    )
    compare.main()
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1
    assert rows[0]["company"] == "CUSTOM BORROWER LLC"
    assert rows[0]["ratio_change"] == pytest.approx(-0.16)


def test_symlink_escape_refuses_writes(tmp_path: Path) -> None:
    state_store = _load_script("dalton_bdc_state_store_test", "state_store.py")
    harvest = _load_script("dalton_bdc_harvest_test", "harvest.py")

    # 1. state dir symlink pointing outside workspace
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    ws_escape = tmp_path / "ws_escape"
    ws_escape.mkdir()
    (ws_escape / "state").symlink_to(outside_dir)

    with pytest.raises(PermissionError, match="outside workspace"):
        harvest.harvest_one("ARCC", "26Q1", workspace_dir=ws_escape)
    assert list(outside_dir.iterdir()) == []

    # 2. target file inside valid state dir symlinked outside workspace
    ws_target_escape = tmp_path / "ws_target_escape"
    data_dir = ws_target_escape / "state" / "dalton-bdc-analyst" / "data"
    data_dir.mkdir(parents=True)
    outside_file = tmp_path / "outside_arcc.json"
    (data_dir / "ARCC.json").symlink_to(outside_file)

    with pytest.raises(PermissionError, match="outside workspace"):
        state_store.ensure_workspace_state(ws_target_escape)
    assert not outside_file.exists()


# ---------------------------------------------------------------------------
# End-to-end offline harvest (fake 10-Q HTML) + compare ranking
# ---------------------------------------------------------------------------


def test_offline_harvest_fake_10q_parse_and_compare_sorting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bdc_analyzer = _load_script("dalton_bdc_analyzer_test", "bdc_analyzer.py")
    arcc_profile = _load_script("bdc_profiles.arcc", "bdc_profiles/arcc.py")
    harvest = _load_script("dalton_bdc_harvest_test", "harvest.py")
    compare = _load_script("dalton_bdc_compare_test", "compare.py")

    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")

    ws = tmp_path / "ws_e2e"
    seed_archive = SKILL_DIR / "data" / "seed.tar.gz"
    seed_before = seed_archive.read_bytes()

    url_q1 = "https://www.sec.gov/Archives/edgar/data/1287750/000128775026000010/arcc_q1.htm"
    url_q2 = "https://www.sec.gov/Archives/edgar/data/1287750/000128775026000020/arcc_q2.htm"

    html_q1 = _build_fake_arcc_soi_html(
        "March 31, 2026",
        [
            ("WORST DETERIORATION CO", "First Lien Loan", 100_000, 95_000),   # 0.95
            ("MILD DETERIORATION CO", "First Lien Loan", 100_000, 98_000),    # 0.98
            ("ABOVE PAR HEALTHY CO", "First Lien Loan", 100_000, 102_000),    # 1.02
        ],
    )
    html_q2 = _build_fake_arcc_soi_html(
        "June 30, 2026",
        [
            ("WORST DETERIORATION CO", "First Lien Loan", 100_000, 75_000),   # 0.75 -> delta -0.20
            ("MILD DETERIORATION CO", "First Lien Loan", 100_000, 92_000),    # 0.92 -> delta -0.06
            ("ABOVE PAR HEALTHY CO", "First Lien Loan", 100_000, 103_000),    # 1.03 (> 1.0, excluded)
        ],
    )

    submissions_payload = {
        "filings": {
            "recent": {
                "form": ["10-Q", "10-Q"],
                "reportDate": ["2026-06-30", "2026-03-31"],
                "filingDate": ["2026-08-05", "2026-05-05"],
                "accessionNumber": ["0001287750-26-000020", "0001287750-26-000010"],
                "primaryDocument": ["arcc_q2.htm", "arcc_q1.htm"],
            }
        }
    }
    tickers_payload = {"0": {"cik_str": 1287750, "ticker": "ARCC", "title": "Ares Capital Corp"}}
    facts_payload = {
        "facts": {
            "us-gaap": {
                "StockholdersEquity": {
                    "units": {
                        "USD": [
                            {"end": "2026-06-30", "val": 5_000_000_000, "frame": "CY2026Q2I"}
                        ]
                    }
                }
            }
        }
    }

    def fake_get(url: str, headers=None, timeout=None):
        assert headers == {"User-Agent": "DaltonResearch research@example.com"}

        def _raise():
            return None

        if url == "https://www.sec.gov/files/company_tickers.json":
            return SimpleNamespace(status_code=200, json=lambda: tickers_payload, raise_for_status=_raise)
        if url == "https://data.sec.gov/submissions/CIK0001287750.json":
            return SimpleNamespace(status_code=200, json=lambda: submissions_payload, raise_for_status=_raise)
        if url == "https://data.sec.gov/api/xbrl/companyfacts/CIK0001287750.json":
            return SimpleNamespace(status_code=200, json=lambda: facts_payload, raise_for_status=_raise)
        if url == url_q1:
            return SimpleNamespace(status_code=200, content=html_q1, raise_for_status=_raise)
        if url == url_q2:
            return SimpleNamespace(status_code=200, content=html_q2, raise_for_status=_raise)
        raise AssertionError(f"Unexpected URL requested: {url}")

    get_stub = create_autospec(_requests_get_sig, spec_set=True, side_effect=fake_get)
    monkeypatch.setattr(requests, "get", get_stub)
    monkeypatch.setattr(bdc_analyzer.requests, "get", get_stub)
    monkeypatch.setattr(arcc_profile.requests, "get", get_stub)

    e1 = harvest.harvest_one("ARCC", "26Q1", workspace_dir=ws)
    e2 = harvest.harvest_one("ARCC", "26Q2", workspace_dir=ws)
    assert e1 is not None and e1["holdings_count"] == 3
    assert e2 is not None and e2["holdings_count"] == 3
    assert e2["scale_detected"] == 1000

    # Seed in skill dir untouched, workspace state updated
    assert seed_archive.read_bytes() == seed_before
    arcc_ws = json.loads((ws / "state" / "dalton-bdc-analyst" / "data" / "ARCC.json").read_text(encoding="utf-8"))
    assert "26Q1" in arcc_ws["periods"]
    assert "26Q2" in arcc_ws["periods"]
    # Seed historical periods (e.g. 25Q4) also preserved
    assert "25Q4" in arcc_ws["periods"]

    # Compare 26Q2 vs 26Q1 via CLI
    monkeypatch.setattr(
        sys,
        "argv",
        ["compare.py", "--workspace-dir", str(ws), "--ticker", "ARCC", "--periodA", "26Q2", "--periodB", "26Q1", "--json"],
    )
    compare.main()
    results = json.loads(capsys.readouterr().out)
    assert [r["company"] for r in results] == [
        "WORST DETERIORATION CO",
        "MILD DETERIORATION CO",
    ]
    assert results[0]["ratio_change"] == pytest.approx(-0.20)
    assert results[1]["ratio_change"] == pytest.approx(-0.06)


def test_concurrent_harvest_same_bdc_preserves_both_periods(tmp_path: Path) -> None:
    """Two real processes write different periods of the same BDC; both must survive.

    Worker A loads ARCC.json inside the per-ticker lock and then waits for a go flag.
    While A waits, the test proves the lock is held with a non-blocking flock (which
    succeeds if the lock is removed), then starts worker B, then releases A. Without
    the lock, B would save first and A would overwrite it with its stale snapshot.
    """
    import fcntl

    state_store = _load_script("dalton_bdc_state_store_conc", "state_store.py")
    ws = tmp_path / "ws_concurrent"
    state_store.ensure_workspace_state(ws)
    state_dir = ws / "state" / "dalton-bdc-analyst"

    a_loaded = tmp_path / "a_loaded.flag"
    go = tmp_path / "go.flag"
    b_started = tmp_path / "b_started.flag"

    worker_a_code = "\n".join([
        "import sys, time",
        "from pathlib import Path",
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})",
        "import harvest",
        f"a_loaded, go = Path({str(a_loaded)!r}), Path({str(go)!r})",
        "orig_load = harvest._load_db",
        "def paused_load(ticker, state_dir):",
        "    db = orig_load(ticker, state_dir)",
        "    a_loaded.touch()",
        "    deadline = time.monotonic() + 30",
        "    while not go.exists():",
        "        assert time.monotonic() < deadline, 'never released'",
        "        time.sleep(0.005)",
        "    return db",
        "harvest._load_db = paused_load",
        f"harvest._save_period_entry('ARCC', '26Q1', {{'period': '26Q1', 'holdings_count': 1, 'total_face_m': 10.0, 'total_fair_m': 9.5, 'holdings': []}}, {str(ws)!r})",
    ])
    worker_b_code = "\n".join([
        "import sys",
        "from pathlib import Path",
        f"sys.path.insert(0, {str(SCRIPTS_DIR)!r})",
        "import harvest",
        f"Path({str(b_started)!r}).touch()",
        f"harvest._save_period_entry('ARCC', '26Q2', {{'period': '26Q2', 'holdings_count': 1, 'total_face_m': 10.0, 'total_fair_m': 8.0, 'holdings': []}}, {str(ws)!r})",
    ])

    proc_a = subprocess.Popen([sys.executable, "-c", worker_a_code])
    try:
        deadline = time.monotonic() + 30
        while not a_loaded.exists():
            assert proc_a.poll() is None and time.monotonic() < deadline, "worker A never loaded"
            time.sleep(0.005)

        with open(state_dir / ".ARCC.lock", "a+", encoding="utf-8") as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

        proc_b = subprocess.Popen([sys.executable, "-c", worker_b_code])
        while not b_started.exists():
            assert proc_b.poll() is None and time.monotonic() < deadline, "worker B never started"
            time.sleep(0.005)
        time.sleep(0.3)
        assert proc_b.poll() is None, "worker B finished while worker A held the lock"
    finally:
        go.touch()
    assert proc_a.wait(timeout=30) == 0
    assert proc_b.wait(timeout=30) == 0

    arcc_data = json.loads((state_dir / "data" / "ARCC.json").read_text(encoding="utf-8"))
    assert {"25Q4", "26Q1", "26Q2"} <= set(arcc_data["periods"])


def test_unreadable_sec_response_is_a_failure_not_an_unfiled_period(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bdc_analyzer = _load_script("dalton_bdc_analyzer_badjson", "bdc_analyzer.py")
    harvest = _load_script("dalton_bdc_harvest_badjson", "harvest.py")
    _reset_env_identity(monkeypatch)
    fetch = create_autospec(
        bdc_analyzer.fetch_filing_url_for_period,
        side_effect=json.JSONDecodeError("Expecting value", "<html>", 0),
    )
    monkeypatch.setattr(harvest, "fetch_filing_url_for_period", fetch)
    monkeypatch.setattr(harvest, "get_cik", create_autospec(bdc_analyzer.get_cik, return_value="0001287750"))
    ws = tmp_path / "ws_badjson"
    ws.mkdir()
    assert harvest.harvest_one("ARCC", "26Q3", workspace_dir=ws) is None


def test_concurrent_apply_keeps_both_processes_entries(tmp_path: Path) -> None:
    ws = tmp_path / "ws_apply"
    ws.mkdir()
    script = SCRIPTS_DIR / "enrich_company_db.py"
    files = []
    for i in range(2):
        f = tmp_path / f"upd{i}.json"
        f.write_text(json.dumps({f"NEW CO {i}": {"industry": f"Ind{i}", "description": f"d{i}"}}), encoding="utf-8")
        files.append(f)
    procs = [
        subprocess.Popen([sys.executable, str(script), "--workspace-dir", str(ws), "--apply", str(f)])
        for f in files
    ]
    assert [p.wait(timeout=60) for p in procs] == [0, 0]
    db = json.loads((ws / "state" / "dalton-bdc-analyst" / "company_db.json").read_text(encoding="utf-8"))
    assert db["NEW CO 0"]["industry"] == "Ind0"
    assert db["NEW CO 1"]["industry"] == "Ind1"


def test_seed_archive_contains_all_44_bdcs_and_company_db() -> None:
    config = _load_script("dalton_bdc_config_test", "config.py")
    seed_archive = SKILL_DIR / "data" / "seed.tar.gz"
    with tarfile.open(seed_archive, "r:gz") as tf:
        names = set(tf.getnames())
    assert "company_db.json" in names
    assert "data/_meta.json" in names
    for ticker in config.PROFILE_MAP:
        assert f"data/{ticker}.json" in names


def test_is_soi_heading_singular_and_plural() -> None:
    bdc_analyzer = _load_script("dalton_bdc_analyzer_test", "bdc_analyzer.py")
    assert bdc_analyzer.is_soi_heading("Schedule of Investments")
    assert bdc_analyzer.is_soi_heading("Consolidated Schedules of Investments (unaudited)")
    assert bdc_analyzer.is_soi_heading("consolidated schedules of investments", consolidated=True)
    assert not bdc_analyzer.is_soi_heading("Schedule of Investments", consolidated=True)
    assert not bdc_analyzer.is_soi_heading("Schedules of Realized Gains")


def test_arcc_real_html_snippet_fixtures_singular_and_plural(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests

    bdc_analyzer = _load_script("dalton_bdc_analyzer_test", "bdc_analyzer.py")
    arcc_profile = _load_script("bdc_profiles.arcc", "bdc_profiles/arcc.py")

    fixtures_dir = Path(__file__).resolve().parent / "fixtures" / "bdc"
    bytes_25q4 = (fixtures_dir / "arcc_25q4_snippet.htm").read_bytes()
    bytes_26q2 = (fixtures_dir / "arcc_26q2_snippet.htm").read_bytes()
    url_25q4 = "https://www.sec.gov/Archives/edgar/data/1287750/arcc_25q4_snippet.htm"
    url_26q2 = "https://www.sec.gov/Archives/edgar/data/1287750/arcc_26q2_snippet.htm"

    def fake_get(url: str, headers=None, timeout=None):
        payload = bytes_25q4 if url == url_25q4 else bytes_26q2
        return SimpleNamespace(status_code=200, content=payload, raise_for_status=lambda: None)

    get_stub = create_autospec(_requests_get_sig, spec_set=True, side_effect=fake_get)
    monkeypatch.setattr(requests, "get", get_stub)
    monkeypatch.setattr(bdc_analyzer.requests, "get", get_stub)
    monkeypatch.setattr(arcc_profile.requests, "get", get_stub)

    df_25q4 = arcc_profile.parse_period(url_25q4, 2025, 4)
    assert len(df_25q4) == 4
    assert "ACP AVENU MIDCO LLC" in set(df_25q4["DisplayKey"])

    df_26q2 = arcc_profile.parse_period(url_26q2, 2026, 2)
    assert len(df_26q2) == 3
    assert set(df_26q2["DisplayKey"]) == {
        "ACP AVENU MIDCO LLC",
        "ACTFY BUYER INC",
        "ACTIVATE HOLDINGS US CORP AND CROSSPOINT CAPITAL AS SPV LP",
    }


def test_harvest_all_skips_unavailable_and_continues_on_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = _load_script("dalton_bdc_config_test", "config.py")
    harvest = _load_script("dalton_bdc_harvest_test", "harvest.py")

    ws = tmp_path / "ws_all"
    called_tickers: list[str] = []

    def fake_harvest_one(
        ticker: str,
        period: str,
        force: bool = False,
        *,
        workspace_dir=None,
    ):
        called_tickers.append(ticker)
        if ticker == "ARCC":
            raise ValueError("Ticker ARCC not found")
        if ticker == "BBDC":
            return None
        return {"period": period, "holdings_count": 5, "total_face_m": 100.0, "total_fair_m": 99.0}

    monkeypatch.setattr(harvest, "harvest_one", fake_harvest_one)
    monkeypatch.setattr(harvest.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["harvest.py", "--workspace-dir", str(ws), "--ticker", "all", "--period", "26Q2", "--force"],
    )

    with pytest.raises(SystemExit) as exc_info:
        harvest.main()
    assert exc_info.value.code != 0

    err = capsys.readouterr().err
    # Unavailable tickers skipped cleanly in `--ticker all` without calling harvest_one
    for unavail in config.SEC_TICKER_UNAVAILABLE:
        assert unavail not in called_tickers
        assert unavail in err

    # Harvest continued past ARCC/BBDC errors to later tickers (e.g. BXSL, WHF)
    assert "ARCC" in called_tickers
    assert "BXSL" in called_tickers
    assert "WHF" in called_tickers
    assert "Failed tickers:" in err
    assert "ValueError: Ticker ARCC not found" in err


def test_unavailable_tickers_remain_readable_in_compare() -> None:
    config = _load_script("dalton_bdc_config_test", "config.py")
    compare = _load_script("dalton_bdc_compare_test", "compare.py")
    state_store = _load_script("dalton_bdc_state_store_test", "state_store.py")

    for ticker in config.SEC_TICKER_UNAVAILABLE:
        db = state_store.read_seed_json(f"data/{ticker}.json")
        assert db is not None
        periods = sorted(db.get("periods", {}).keys())
        assert len(periods) >= 2
        entry_latest = compare._load_period(ticker, periods[-1], state_dir=None)
        assert entry_latest is not None and entry_latest["holdings_count"] > 0
        deteriorated = compare.compare_ticker(
            ticker, periods[-1], periods[-2], workspace_dir=None
        )
        assert isinstance(deteriorated, list)


def test_get_shareholder_equity_prefers_latest_year_over_cy2025(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import requests

    bdc_analyzer = _load_script("dalton_bdc_analyzer_test", "bdc_analyzer.py")
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")

    facts_payload = {
        "facts": {
            "us-gaap": {
                "StockholdersEquity": {
                    "units": {
                        "USD": [
                            {"end": "2024-12-31", "filed": "2025-02-10", "val": 1_000_000_000, "frame": "CY2024Q4I"},
                            {"end": "2025-12-31", "filed": "2026-02-10", "val": 2_000_000_000, "frame": "CY2025Q4I"},
                            {"end": "2026-06-30", "filed": "2026-08-05", "val": 3_500_000_000, "frame": "CY2026Q2I"},
                        ]
                    }
                }
            }
        }
    }

    def fake_get(url: str, headers=None, timeout=None):
        return SimpleNamespace(status_code=200, json=lambda: facts_payload, raise_for_status=lambda: None)

    get_stub = create_autospec(_requests_get_sig, spec_set=True, side_effect=fake_get)
    monkeypatch.setattr(requests, "get", get_stub)
    monkeypatch.setattr(bdc_analyzer.requests, "get", get_stub)

    equity = bdc_analyzer.get_shareholder_equity("0001287750")
    assert equity == 3_500_000_000


def test_harvest_skips_a_period_not_filed_yet_instead_of_storing_an_older_filing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A September fiscal-year BDC has no 26Q3 filing until its 10-K lands; the
    nearest older 10-Q must not be parsed and stored as 26Q3."""
    bdc_analyzer = _load_script("dalton_bdc_analyzer_nofb", "bdc_analyzer.py")
    harvest = _load_script("dalton_bdc_harvest_nofb", "harvest.py")
    _reset_env_identity(monkeypatch)

    fetch = create_autospec(
        bdc_analyzer.fetch_filing_url_for_period,
        side_effect=harvest.FilingNotFoundForPeriod("No 10-Q/10-K filing found for period 26Q3"),
    )
    monkeypatch.setattr(harvest, "fetch_filing_url_for_period", fetch)
    monkeypatch.setattr(harvest, "get_cik", create_autospec(bdc_analyzer.get_cik, return_value="0001414932"))

    ws = tmp_path / "ws_nofb"
    ws.mkdir()
    with pytest.raises(harvest.PeriodNotFiledError):
        harvest.harvest_one("OCSL", "26Q3", workspace_dir=ws)
    fetch.assert_called_once_with("0001414932", "26Q3", allow_fallback=False, return_meta=True)
    data_dir = ws / "state" / "dalton-bdc-analyst" / "data"
    if data_dir.exists():
        stored = json.loads((data_dir / "OCSL.json").read_text()) if (data_dir / "OCSL.json").exists() else {}
        assert "26Q3" not in json.dumps(stored)


def test_harvest_all_corrupted_json_does_not_abort_and_exits_nonzero(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harvest = _load_script("dalton_bdc_harvest_corrupt", "harvest.py")
    ws = tmp_path / "ws_corrupt"
    state_dir = harvest.ensure_workspace_state(ws)
    (state_dir / "data" / "ARCC.json").write_text("{corrupted-json", encoding="utf-8")

    called: list[str] = []

    def fake_harvest_one(ticker: str, period: str, force: bool = False, *, workspace_dir=None):
        called.append(ticker)
        return {"period": period, "holdings_count": 3, "total_face_m": 50.0, "total_fair_m": 49.0}

    monkeypatch.setattr(harvest, "harvest_one", fake_harvest_one)
    monkeypatch.setattr(harvest.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["harvest.py", "--workspace-dir", str(ws), "--ticker", "all", "--period", "26Q2"],
    )

    with pytest.raises(SystemExit) as exc_info:
        harvest.main()
    assert exc_info.value.code != 0

    err = capsys.readouterr().err
    assert "BXSL" in called and "WHF" in called
    assert "失败 / Failed tickers:" in err
    assert "ARCC: JSONDecodeError" in err


def test_harvest_all_all_unfiled_exits_zero_and_lists_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    harvest = _load_script("dalton_bdc_harvest_unfiled", "harvest.py")
    ws = tmp_path / "ws_all_unfiled"

    def fake_unfiled(ticker: str, period: str, force: bool = False, *, workspace_dir=None):
        raise harvest.PeriodNotFiledError(ticker, period)

    monkeypatch.setattr(harvest, "harvest_one", fake_unfiled)
    monkeypatch.setattr(harvest.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        sys,
        "argv",
        ["harvest.py", "--workspace-dir", str(ws), "--ticker", "ARCC,BXSL", "--period", "26Q3", "--force"],
    )

    # All periods unfiled must return cleanly (exit code 0) and report skipped tickers
    harvest.main()
    err = capsys.readouterr().err
    assert "尚未申报（跳过）(2): ARCC, BXSL" in err
    assert "Failed tickers:" not in err


def test_get_shareholder_equity_cross_concept_prefers_latest_end_date_and_priority_tiebreak(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bdc_analyzer = _load_script("dalton_bdc_analyzer_eq_cross", "bdc_analyzer.py")
    _reset_env_identity(monkeypatch)
    monkeypatch.setenv("SEC_USER_AGENT", "DaltonResearch research@example.com")

    payload_newer_assets_net = {
        "facts": {
            "us-gaap": {
                "StockholdersEquity": {
                    "units": {
                        "USD": [
                            {"end": "2025-12-31", "filed": "2026-02-20", "val": 2_000_000_000, "frame": "CY2025Q4I"}
                        ]
                    }
                },
                "AssetsNet": {
                    "units": {
                        "USD": [
                            {"end": "2026-06-30", "filed": "2026-08-05", "val": 2_450_000_000, "frame": "CY2026Q2I"}
                        ]
                    }
                },
            }
        }
    }
    payload_same_date = {
        "facts": {
            "us-gaap": {
                "StockholdersEquity": {
                    "units": {
                        "USD": [
                            {"end": "2026-06-30", "filed": "2026-08-05", "val": 3_100_000_000, "frame": "CY2026Q2I"}
                        ]
                    }
                },
                "AssetsNet": {
                    "units": {
                        "USD": [
                            {"end": "2026-06-30", "filed": "2026-08-10", "val": 2_900_000_000, "frame": "CY2026Q2I"}
                        ]
                    }
                },
            }
        }
    }

    current_payload = [payload_newer_assets_net]

    def fake_get(url: str, headers=None, timeout=None):
        return SimpleNamespace(status_code=200, json=lambda: current_payload[0], raise_for_status=lambda: None)

    get_stub = create_autospec(_requests_get_sig, spec_set=True, side_effect=fake_get)
    monkeypatch.setattr(requests, "get", get_stub)
    monkeypatch.setattr(bdc_analyzer.requests, "get", get_stub)

    assert bdc_analyzer.get_shareholder_equity("0001287750") == 2_450_000_000
    current_payload[0] = payload_same_date
    assert bdc_analyzer.get_shareholder_equity("0001287750") == 3_100_000_000


def test_enrich_company_db_apply_updates_via_save_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    enrich = _load_script("dalton_bdc_enrich_apply", "enrich_company_db.py")
    ws = tmp_path / "ws_enrich"
    updates_file = tmp_path / "updates.json"
    updates_file.write_text(
        json.dumps(
            {
                "NEW TEST BORROWER LLC": {
                    "industry": "Software",
                    "sub_industry": "Enterprise SaaS",
                    "description": "企业SaaS软件供应商。",
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["enrich_company_db.py", "--workspace-dir", str(ws), "--apply", str(updates_file)],
    )
    enrich.main()

    stored = json.loads((ws / "state" / "dalton-bdc-analyst" / "company_db.json").read_text(encoding="utf-8"))
    assert stored["NEW TEST BORROWER LLC"]["industry"] == "Software"
    assert len(stored) > 100
