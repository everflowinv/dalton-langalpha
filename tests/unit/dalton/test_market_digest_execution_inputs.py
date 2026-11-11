"""Execution binding for one prepared market-digest attempt.

Runners are the signature-correct substitutes from the preparation tests.
No test fetches mail or starts a service.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
import threading
import time
import uuid
from datetime import datetime
from unittest.mock import patch

import pytest

from scripts.dalton import market_digest_inputs as mod
from tests.unit.dalton import test_market_digest_inputs as prep

DATE = prep.DATE
NEXT_DATE = "2026-11-11"
NEXT_MORNING = datetime(2026, 11, 11, 9, 0, tzinfo=mod.HKT)


def _automation_id() -> str:
    return str(uuid.uuid4())


def _binding_path(layout: prep.Layout, automation_id: str) -> Path:
    return layout.state / "executions" / f"{automation_id}.json"


def _run(
    layout: prep.Layout,
    runner: prep.RecordingRunner,
    automation_id: str,
    now: datetime = prep.NOW,
    period: str = "AM",
) -> dict:
    return mod.prepare_execution_inputs(
        layout.config(),
        period,
        automation_id,
        now=now,
        runner=prep._spec_runner(runner),
        env_builder=prep._spec_env(),
    )


def _saved_binding(layout: prep.Layout, automation_id: str) -> dict:
    return json.loads(_binding_path(layout, automation_id).read_text(encoding="utf-8"))


def test_execution_signatures_match_the_runtime_contracts() -> None:
    config = mod.PrepareConfig(
        state_root=Path("/tmp/market-digest-state"),
        skill_dir=Path("/tmp/market-digest-skill"),
        tmtb_script=Path("/tmp/tmtb_alphaengine_sync.py"),
        python_executable=Path("/usr/bin/python3"),
        repo_root=Path("/tmp/market-digest-repo"),
    )
    inspect.signature(mod.prepare_execution_inputs).bind(
        config,
        "AM",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        now=prep.NOW,
        runner=mod.run_json_command,
        env_builder=mod.build_nonarchive_subprocess_env,
    )
    inspect.signature(mod.get_execution_binding).bind(
        Path("/tmp/market-digest-state"),
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )
    inspect.signature(mod._begin).bind(
        config,
        Path("/tmp/market-digest-state"),
        "AM",
        prep.NOW,
        mod.run_json_command,
        mod.build_nonarchive_subprocess_env,
    )
    assert "automation_execution_id" not in inspect.signature(mod.prepare_inputs).parameters


def test_first_prepare_binds_before_mail_and_calls_each_command_once(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    automation_id = _automation_id()
    seen: dict[str, object] = {}

    def before_spawn() -> None:
        attempt = json.loads(layout.attempt_path().read_text(encoding="utf-8"))
        saved = _saved_binding(layout, automation_id)
        seen["phase"] = attempt["phase"]
        seen["prepared"] = saved["prepared_execution_id"]
        seen["attempt"] = attempt["execution_id"]
        seen["authoritative_date"] = saved["authoritative_date"]
        seen["mail_written"] = layout.digest_path().exists()

    runner.before_spawn = before_spawn
    result = _run(layout, runner, automation_id, period="am")
    saved = _saved_binding(layout, automation_id)
    encoded = json.dumps(result)
    assert seen == {
        "phase": "digest_claimed",
        "prepared": result["execution_id"],
        "attempt": result["execution_id"],
        "authoritative_date": None,
        "mail_written": False,
    }
    assert result["preparation_status"] == "ready"
    assert result["digest_invoked"] is True and result["tmtb_invoked"] is True
    assert result["automation_execution_id"] == automation_id
    assert result["execution_id"] != automation_id
    assert result["period"] == "AM"
    assert saved["prepared_execution_id"] == result["execution_id"]
    assert saved["started_hkt_date"] == DATE
    assert saved["batch_id"] == f"AM/{DATE}"
    assert saved["hashes"]["input_sha256"] == result["hashes"]["input_sha256"]
    assert set(saved) == {
        "schema",
        "automation_execution_id",
        "period",
        "prepared_execution_id",
        "started_hkt_date",
        "authoritative_date",
        "batch_id",
        "hashes",
    }
    looked = mod.get_execution_binding(layout.state, automation_id)
    assert looked["ok"] is True and looked["code"] == "ok"
    assert looked["prepared_execution_id"] == result["execution_id"]
    assert prep.BODY not in encoded
    assert str(layout.state) not in encoded
    assert prep.BODY not in json.dumps(saved)
    assert (layout.state / "locks" / f"execution_{automation_id}.lock").is_file()
    assert prep._kinds(runner) == ["digest", "tmtb"]
    prep._assert_child_argv(runner, layout, date_value=DATE, period="AM")


def test_same_execution_next_day_resumes_the_original_attempt(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    automation_id = _automation_id()
    first = _run(layout, runner, automation_id)
    with patch.object(mod, "_begin", side_effect=AssertionError("new attempt")):
        second = _run(layout, runner, automation_id, now=NEXT_MORNING)
    assert second["reused"] is True
    assert second["digest_invoked"] is False and second["tmtb_invoked"] is False
    assert second["started_hkt_date"] == first["started_hkt_date"] == DATE
    assert second["authoritative_date"] == first["authoritative_date"] == DATE
    assert second["execution_id"] == first["execution_id"]
    assert second["hashes"]["input_sha256"] == first["hashes"]["input_sha256"]
    assert _saved_binding(layout, automation_id)["started_hkt_date"] == DATE
    assert prep._kinds(runner) == ["digest", "tmtb"]


def test_unbound_execution_may_select_the_next_daily_attempt(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    first_id = _automation_id()
    second_id = _automation_id()
    first = _run(layout, runner, first_id)
    original_binding = _binding_path(layout, first_id).read_bytes()

    def digest(_argv: list[str]) -> mod.ChildResult:
        prep._write_json(
            layout.digest_path(NEXT_DATE),
            prep._digest_document([prep._mail()], date_value=NEXT_DATE),
        )
        return prep._child(
            prep._digest_stdout(
                date_value=NEXT_DATE,
                output_file=str(layout.digest_path(NEXT_DATE)),
            )
        )

    def tmtb(_argv: list[str]) -> mod.ChildResult:
        prep._write_json(
            layout.tmtb_path(NEXT_DATE),
            prep._tmtb_document([{"doc_id": "d2", "text": "next-day"}], date_value=NEXT_DATE),
        )
        return prep._child(
            prep._tmtb_stdout(
                date_value=NEXT_DATE,
                status="new_docs",
                output_file=str(layout.tmtb_path(NEXT_DATE)),
                count=1,
                new_count=1,
            )
        )

    runner.digest = digest
    runner.tmtb = tmtb
    second = _run(layout, runner, second_id, now=NEXT_MORNING)
    assert second["digest_invoked"] is True and second["tmtb_invoked"] is True
    assert second["started_hkt_date"] == NEXT_DATE
    assert second["authoritative_date"] == NEXT_DATE
    assert second["execution_id"] != first["execution_id"]
    assert _saved_binding(layout, second_id)["prepared_execution_id"] == second["execution_id"]
    assert _binding_path(layout, first_id).read_bytes() == original_binding
    assert prep._kinds(runner).count("digest") == 2
    assert prep._kinds(runner).count("tmtb") == 2


def test_existing_attempt_is_bound_before_resume(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    prepared = prep._prepare(layout, runner)
    automation_id = _automation_id()
    real_resume = mod._resume
    seen = {"called": False}

    def spy(config, state_root, attempt, runner_fn, env_builder):
        saved = _saved_binding(layout, automation_id)
        assert saved["prepared_execution_id"] == attempt["execution_id"] == prepared["execution_id"]
        assert saved["started_hkt_date"] == prepared["started_hkt_date"]
        seen["called"] = True
        return real_resume(config, state_root, attempt, runner_fn, env_builder)

    with patch.object(mod, "_resume", spy):
        result = _run(layout, runner, automation_id)
    assert seen["called"] is True
    assert result["reused"] is True
    assert result["automation_execution_id"] == automation_id
    assert result["execution_id"] == prepared["execution_id"]
    assert "automation_execution_id" not in prepared
    assert prep._kinds(runner) == ["digest", "tmtb"]


def test_cross_period_reuse_does_not_collect(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    automation_id = _automation_id()
    _run(layout, runner, automation_id)
    original = _binding_path(layout, automation_id).read_bytes()
    with (
        patch.object(mod, "_begin", side_effect=AssertionError("begin")),
        patch.object(mod, "_resume", side_effect=AssertionError("resume")),
    ):
        refused = _run(layout, runner, automation_id, period="PM")
    assert refused["reason"] == "period_mismatch"
    assert refused["preparation_status"] == "period_mismatch"
    assert refused["digest_invoked"] is False and refused["tmtb_invoked"] is False
    assert refused["usable"] is False
    assert _binding_path(layout, automation_id).read_bytes() == original
    assert not (layout.state / "attempts" / "PM").exists()
    assert prep._kinds(runner) == ["digest", "tmtb"]


def test_missing_or_replaced_attempt_is_not_recollected(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    automation_id = _automation_id()
    first = _run(layout, runner, automation_id)
    original = _binding_path(layout, automation_id).read_bytes()
    layout.attempt_path().unlink()
    with (
        patch.object(mod, "_begin", side_effect=AssertionError("begin")),
        patch.object(mod, "_resume", side_effect=AssertionError("resume")),
    ):
        missing = _run(layout, runner, automation_id)
    assert missing["reason"] == "attempt_missing"
    assert missing["digest_invoked"] is False and missing["tmtb_invoked"] is False
    assert _binding_path(layout, automation_id).read_bytes() == original

    prep._write_json(
        layout.attempt_path(),
        {
            "schema": mod.SCHEMA,
            "execution_id": "b" * 32,
            "period": "AM",
            "started_hkt_date": DATE,
            "digest": {"claimed": True},
            "tmtb": {"claimed": False},
        },
    )
    with (
        patch.object(mod, "_begin", side_effect=AssertionError("begin")),
        patch.object(mod, "_resume", side_effect=AssertionError("resume")),
    ):
        replaced = _run(layout, runner, automation_id)
    assert replaced["reason"] == "attempt_mismatch"
    assert replaced["digest_invoked"] is False
    assert _saved_binding(layout, automation_id)["prepared_execution_id"] == first["execution_id"]
    assert prep._kinds(runner) == ["digest", "tmtb"]


def test_concurrent_same_execution_prepares_one_pair(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    automation_id = _automation_id()
    gate = threading.Event()
    original_digest = runner.digest
    assert original_digest is not None

    def digest(argv: list[str]) -> mod.ChildResult:
        gate.set()
        time.sleep(0.2)
        return original_digest(argv)

    runner.digest = digest
    results: list[dict] = []
    errors: list[BaseException] = []

    def work() -> None:
        try:
            results.append(_run(layout, runner, automation_id))
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
    assert prep._kinds(runner).count("digest") == 1
    assert prep._kinds(runner).count("tmtb") == 1
    invoked = [item for item in results if item["digest_invoked"]]
    reused = [item for item in results if item["reused"]]
    assert len(invoked) == 1 and len(reused) == 1
    assert invoked[0]["execution_id"] == reused[0]["execution_id"]
    assert invoked[0]["automation_execution_id"] == reused[0]["automation_execution_id"] == automation_id
    assert invoked[0]["hashes"]["input_sha256"] == reused[0]["hashes"]["input_sha256"]


def test_crash_after_digest_claim_recovers_without_a_second_digest(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    automation_id = _automation_id()

    def digest(_argv: list[str]) -> mod.ChildResult:
        prep._write_json(layout.digest_path(), prep._digest_document([prep._mail(body="RECOVERED-BODY")]))
        raise RuntimeError("crash-after-claim")

    def tmtb(_argv: list[str]) -> mod.ChildResult:
        prep._write_json(layout.tmtb_path(), prep._tmtb_document([{"doc_id": "d1", "text": "AFTER-CLAIM"}]))
        return prep._child(
            prep._tmtb_stdout(
                status="new_docs",
                output_file=str(layout.tmtb_path()),
                count=1,
                new_count=1,
            )
        )

    runner.digest = digest
    runner.tmtb = tmtb
    first = _run(layout, runner, automation_id)
    prepared_id = _saved_binding(layout, automation_id)["prepared_execution_id"]
    assert first["reason"] == "digest_runner_exception"
    assert first["digest_invoked"] is True and first["tmtb_invoked"] is False
    assert prepared_id == first["execution_id"]
    assert "crash-after-claim" not in json.dumps(first)
    with patch.object(mod, "_begin", side_effect=AssertionError("refetch")):
        second = _run(layout, runner, automation_id)
    assert second["preparation_status"] == "ready"
    assert second["digest_outcome"] == "recovered"
    assert second["digest_invoked"] is False and second["tmtb_invoked"] is True
    assert second["execution_id"] == prepared_id
    assert second["started_hkt_date"] == DATE
    assert _saved_binding(layout, automation_id)["prepared_execution_id"] == prepared_id
    assert prep._kinds(runner).count("digest") == 1
    assert prep._kinds(runner).count("tmtb") == 1


def test_corrupt_snapshot_and_binding_do_not_recollect(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    automation_id = _automation_id()
    _run(layout, runner, automation_id)
    binding_path = _binding_path(layout, automation_id)
    original_binding = binding_path.read_bytes()
    emails_path = layout.state / "batches" / "AM" / DATE / "emails.json"
    emails_path.write_text(emails_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with patch.object(mod, "_begin", side_effect=AssertionError("refetch")):
        mismatched = _run(layout, runner, automation_id)
    assert mismatched["preparation_status"] == "snapshot_hash_mismatch"
    assert mismatched["reason"] == "snapshot_hash_mismatch"
    assert mismatched["hashes"] is None
    assert mismatched["digest_invoked"] is False and mismatched["tmtb_invoked"] is False
    assert binding_path.read_bytes() == original_binding
    binding_path.write_text("{", encoding="utf-8")
    with (
        patch.object(mod, "_begin", side_effect=AssertionError("begin")),
        patch.object(mod, "_resume", side_effect=AssertionError("resume")),
    ):
        corrupt = _run(layout, runner, automation_id)
    assert corrupt["reason"] == "binding_corrupt"
    assert corrupt["digest_invoked"] is False and corrupt["tmtb_invoked"] is False
    assert mod.get_execution_binding(layout.state, automation_id)["code"] == "binding_corrupt"
    assert prep._kinds(runner) == ["digest", "tmtb"]


def test_symlink_binding_and_invalid_id_are_refused(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    automation_id = _automation_id()
    assert mod.get_execution_binding(layout.state, automation_id)["code"] == "binding_missing"
    _run(layout, runner, automation_id)
    outside = tmp_path / "outside.json"
    outside.write_text('{"secret":"OUTSIDE-SECRET"}', encoding="utf-8")
    binding_path = _binding_path(layout, automation_id)
    binding_path.unlink()
    binding_path.symlink_to(outside)
    with (
        patch.object(mod, "_begin", side_effect=AssertionError("begin")),
        patch.object(mod, "_resume", side_effect=AssertionError("resume")),
    ):
        refused = _run(layout, runner, automation_id)
    assert refused["reason"] == "binding_corrupt"
    assert "OUTSIDE-SECRET" not in json.dumps(refused)
    looked = mod.get_execution_binding(layout.state, automation_id)
    assert looked["code"] == "binding_corrupt"
    assert "OUTSIDE-SECRET" not in json.dumps(looked)

    invalid = _run(layout, runner, "../etc/passwd")
    assert invalid["reason"] == "invalid_automation_execution"
    assert invalid["digest_invoked"] is False and invalid["tmtb_invoked"] is False
    assert mod.get_execution_binding(layout.state, "NOT-A-UUID")["code"] == "invalid_automation_execution"
    uppercase = "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA"
    assert mod.get_execution_binding(layout.state, uppercase)["code"] == "invalid_automation_execution"
    with pytest.raises(mod.PrepareConfigError):
        _run(layout, runner, automation_id, period="noon")
    with pytest.raises(mod.PrepareConfigError):
        mod.prepare_execution_inputs(
            layout.config(),
            "AM",
            _automation_id(),
            now=datetime(2026, 11, 10, 9, 0),
            runner=prep._spec_runner(runner),
            env_builder=prep._spec_env(),
        )
    assert prep._kinds(runner) == ["digest", "tmtb"]


def test_prepare_inputs_stays_compatible(tmp_path: Path) -> None:
    layout = prep._layout(tmp_path)
    runner = prep.RecordingRunner()
    prep._successful_pair(layout, runner)
    result = prep._prepare(layout, runner)
    assert result["preparation_status"] == "ready"
    assert "automation_execution_id" not in result
    assert prep._kinds(runner) == ["digest", "tmtb"]
    assert not (layout.state / "executions").exists()
