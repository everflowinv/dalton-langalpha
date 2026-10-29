"""Redaction and window parsing for Dalton logs and exports (P0 + P0 fix1, 2026-10-28)."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

SCRIPTS_DALTON = Path(__file__).resolve().parents[3] / "scripts" / "dalton"
if str(SCRIPTS_DALTON) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DALTON))

import researchdata_bridge as rb  # noqa: E402
from log_redaction import (  # noqa: E402
    clean_log_field,
    http_status_from_text,
    main,
    parse_window_timestamp,
    redact_lines,
    redact_text,
)


def test_mcp_token_paths_are_redacted() -> None:
    assert (
        redact_text("POST /mcp/9f8e7d6c5b4a3210fef1e2d3 HTTP/1.1")
        == "POST /mcp/<redacted> HTTP/1.1"
    )
    # A sub-path beyond the token keeps its readable tail.
    assert "/mcp/<redacted>/tools/list" in redact_text(
        "http://127.0.0.1:8000/mcp/secrettoken/tools/list"
    )
    # Even a 40-char hex token in /mcp/<token> becomes /mcp/<redacted>.
    assert "/mcp/<redacted>/tools/call" in redact_text(
        "POST /mcp/0123456789abcdef0123456789abcdef01234567/tools/call HTTP/1.1"
    )


def test_bearer_and_basic_auth_headers_are_redacted() -> None:
    out_bearer = redact_text("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out_bearer
    assert "[REDACTED]" in out_bearer

    out_upper = redact_text("authorization: BEARER xyz987")
    assert "xyz987" not in out_upper
    assert "[REDACTED]" in out_upper

    out_basic = redact_text("Authorization: Basic dXNlcjpzdXBlcnNlY3JldA==")
    assert "dXNlcjpzdXBlcnNlY3JldA==" not in out_basic
    assert "[REDACTED]" in out_basic

    out_bare_bearer = redact_text("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9")
    assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in out_bare_bearer
    assert "Bearer [REDACTED]" in out_bare_bearer


@pytest.mark.parametrize(
    ("json_key", "secret_val"),
    [
        ("token", "tok_secret_111"),
        ("TOKEN", "tok_upper_222"),
        ("access_token", "acc_secret_333"),
        ("ACCESS_TOKEN", "acc_upper_444"),
        ("refresh_token", "ref_secret_555"),
        ("api_key", "ak_secret_666"),
        ("API_KEY", "ak_upper_777"),
        ("secret", "sec_val_888"),
        ("Secret", "sec_mixed_999"),
        ("password", "pw_val_000"),
        ("PASSWORD", "pw_upper_111"),
    ],
)
def test_json_secret_keys_are_redacted_case_insensitively(
    json_key: str, secret_val: str
) -> None:
    raw = f'{{"{json_key}": "{secret_val}", "ok": false}}'
    redacted = redact_text(raw)
    assert secret_val not in redacted
    assert f'"{json_key}": "[REDACTED]"' in redacted
    assert '"ok": false' in redacted


def test_key_value_and_quoted_forms_are_redacted() -> None:
    samples = [
        ("token=abc123&other=1", "abc123", "token=[REDACTED]"),
        ("access_token=eyJhbGciOi", "eyJhbGciOi", "access_token=[REDACTED]"),
        ("refresh_token=zzz;", "zzz", "refresh_token=[REDACTED]"),
        ("key: my_raw_key_secret", "my_raw_key_secret", "key: [REDACTED]"),
        ('key="quoted_key_secret"', "quoted_key_secret", 'key="[REDACTED]"'),
        ("api_key: bare_api_secret", "bare_api_secret", "api_key: [REDACTED]"),
        ('secret="my_quoted_secret"', "my_quoted_secret", 'secret="[REDACTED]"'),
        ("password: super_secret_password", "super_secret_password", "password: [REDACTED]"),
    ]
    for raw, leaked, expected_fragment in samples:
        redacted = redact_text(raw)
        assert leaked not in redacted, (raw, redacted)
        assert expected_fragment in redacted, (raw, redacted)


def test_cookie_and_set_cookie_whole_line_redacted() -> None:
    cookie_line = "Cookie: session=abc123; sid=privateSession456; theme=dark"
    set_cookie_line = "Set-Cookie: auth_tok=xyz987; Path=/; HttpOnly; Secure"
    r_cookie = redact_text(cookie_line)
    r_set_cookie = redact_text(set_cookie_line)
    assert "abc123" not in r_cookie
    assert "privateSession456" not in r_cookie
    assert "xyz987" not in r_set_cookie
    assert "Cookie: [REDACTED]" in r_cookie
    assert "Set-Cookie: [REDACTED]" in r_set_cookie


def test_common_secret_prefixes_are_redacted() -> None:
    raw = (
        "openai=sk-proj-AbCdEfGhIjKlMnOp123456 "
        "anthropic=sk-ant-api03-1234567890abcdef "
        "grok=xai-ZyXwVuTsRqPoNmLk987654 "
        "google=AIzaSyDaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe "
        "groq=gsk_1234567890abcdefghijklmnop "
        "tavily=tvly-1234567890abcdef "
        "firecrawl=fc-1234567890abcdef "
        "gh=ghp_1234567890abcdef "
        "ghpat=github_pat_11AA22BB_1234567890abcdef "
        "aws=AKIAIOSFODNN7EXAMPLE "
        "slacka=xoxa-123456789012-abcdef "
        "slackb=xoxb-123456789012-abcdef "
        "slackp=xoxp-123456789012-abcdef"
    )
    redacted = redact_text(raw)
    for leaked in (
        "sk-proj-AbCdEfGhIjKlMnOp123456",
        "sk-ant-api03-1234567890abcdef",
        "xai-ZyXwVuTsRqPoNmLk987654",
        "AIzaSyDaGmWKa4JsXZ-HjGw7ISLn_3namBGewQe",
        "gsk_1234567890abcdefghijklmnop",
        "tvly-1234567890abcdef",
        "fc-1234567890abcdef",
        "ghp_1234567890abcdef",
        "github_pat_11AA22BB_1234567890abcdef",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxa-123456789012-abcdef",
        "xoxb-123456789012-abcdef",
        "xoxp-123456789012-abcdef",
    ):
        assert leaked not in redacted, (leaked, redacted)


def test_backslash_escaped_json_secret_values_are_redacted() -> None:
    """P0 fix2 B: escaped JSON payloads such as `\\"token\\": \\"…\\"` must be
    redacted without breaking surrounding escaped quotes."""
    raw = r'payload="{\"token\": \"escaped_tok_123\", \"API_KEY\": \"escaped_key_456\", \"ok\": false}"'
    redacted = redact_text(raw)
    assert "escaped_tok_123" not in redacted, redacted
    assert "escaped_key_456" not in redacted, redacted
    assert r'\"ok\": false' in redacted, redacted


def test_url_query_params_including_aws_sig_and_oauth_code_are_redacted() -> None:
    url = (
        "https://s3.amazonaws.com/bucket/doc.pdf"
        "?X-Amz-Signature=deadbeefsig1"
        "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20261028"
        "&Signature=plainSig2"
        "&sig=shortSig3"
        "&token=urlTok4"
        "&key=urlKey5"
        "&api_key=urlApiKey6"
        "&access_token=urlAccTok7"
        "&code=oauthCallbackCode8"
    )
    redacted = redact_text(f"GET {url} failed")
    for leaked in (
        "deadbeefsig1",
        "AKIAIOSFODNN7EXAMPLE",
        "plainSig2",
        "shortSig3",
        "urlTok4",
        "urlKey5",
        "urlApiKey6",
        "urlAccTok7",
        "oauthCallbackCode8",
    ):
        assert leaked not in redacted, (leaked, redacted)
    for kept_param in (
        "X-Amz-Signature=",
        "X-Amz-Credential=",
        "Signature=",
        "sig=",
        "token=",
        "key=",
        "api_key=",
        "access_token=",
        "code=",
    ):
        assert kept_param in redacted


def test_shared_rules_match_bridge_failure_log_sanitizer() -> None:
    sample = (
        'upstream failed: {"api_key": "sekrit123"} '
        "Cookie: a=1; b=2 https://example.com/p?code=oauth123&sig=abc "
        "contact alice@example.com or 13800138000"
    )
    assert rb._sanitize_failure_log_excerpt(sample, max_len=500) == redact_text(sample)[:500]


def test_clean_log_field_redacts_strips_control_chars_and_truncates() -> None:
    dirty = 'reason with "token": "my_secret_tok"\nforged_line=1\r\tand\x00control ' + ("word " * 60)
    cleaned_reason = clean_log_field(dirty, max_len=200)
    assert "my_secret_tok" not in cleaned_reason
    assert "\n" not in cleaned_reason
    assert "\r" not in cleaned_reason
    assert "\x00" not in cleaned_reason
    assert len(cleaned_reason) == 200

    cleaned_code = clean_log_field("E_CODE_WITH_BEARER Bearer secret_jwt_token_123456\n" + ("word " * 20), max_len=64)
    assert "secret_jwt_token_123456" not in cleaned_code
    assert "\n" not in cleaned_code
    assert len(cleaned_code) == 64

    assert clean_log_field(None, max_len=64) == ""
    assert clean_log_field("", max_len=64) == ""


def test_redaction_leaves_ordinary_log_lines_intact() -> None:
    line = "2026-10-27 23:00:01,123 [INFO] tools/call corr=abc route=sec op=list-filings status=error elapsed_ms=1234"
    assert redact_text(line) == line


def test_window_filter_keeps_only_lines_inside_and_never_drops_unstamped_lines() -> None:
    lines = [
        "2026-10-27 22:00:00,000 [INFO] before window",
        "2026-10-27 23:00:01,123 [INFO] inside window",
        "Traceback (most recent call last):",  # continuation, no timestamp
        '  File "x.py", line 1',
        "2026-10-28 01:00:00,000 [INFO] after window",
    ]
    kept = redact_lines(
        lines,
        since=datetime(2026, 10, 27, 22, 30, 0),
        until=datetime(2026, 10, 27, 23, 30, 0),
    )
    assert "2026-10-27 23:00:01,123 [INFO] inside window" in kept
    assert "before window" not in kept
    assert "after window" not in kept
    assert "Traceback (most recent call last):" in kept
    assert '  File "x.py", line 1' in kept


def test_parse_window_timestamp_supports_rfc3339_offsets_and_docker_relative() -> None:
    fixed_now = datetime(2026, 10, 28, 12, 0, 0, tzinfo=timezone(timedelta(hours=-4)))
    assert parse_window_timestamp("2h", now=fixed_now) == datetime(2026, 10, 28, 10, 0, 0)
    assert parse_window_timestamp("1d30m", now=fixed_now) == datetime(2026, 10, 27, 11, 30, 0)
    # RFC3339 with Z (16:00 UTC == 12:00 EDT/-04:00)
    assert parse_window_timestamp("2026-10-28T16:00:00Z", now=fixed_now) == datetime(2026, 10, 28, 12, 0, 0)
    # RFC3339 with +08:00 offset (2026-10-29T00:00:00+08:00 == 2026-10-28 12:00 EDT/-04:00)
    assert parse_window_timestamp("2026-10-29T00:00:00+08:00", now=fixed_now) == datetime(2026, 10, 28, 12, 0, 0)
    # Plain naive timestamps
    assert parse_window_timestamp("2026-10-27 22:50", now=fixed_now) == datetime(2026, 10, 27, 22, 50, 0)

    with pytest.raises(ValueError):
        parse_window_timestamp("not-a-timestamp", now=fixed_now)
    with pytest.raises(ValueError):
        parse_window_timestamp("", now=fixed_now)


def test_parse_window_timestamp_uses_target_date_dst_offset_under_zoneinfo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0 fix2 C: in America/New_York, `2026-01-15T12:00:00Z` must convert to
    winter EST (`07:00`), even when `now` is in summer EDT (`-04:00`)."""
    from zoneinfo import ZoneInfo

    ny_tz = ZoneInfo("America/New_York")
    summer_now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=ny_tz)
    assert parse_window_timestamp("2026-01-15T12:00:00Z", now=summer_now) == datetime(
        2026, 1, 15, 7, 0, 0
    )

    monkeypatch.setenv("TZ", "America/New_York")
    assert parse_window_timestamp("2026-01-15T12:00:00Z") == datetime(
        2026, 1, 15, 7, 0, 0
    )


def test_cli_roundtrip_redacts_file_and_normalizes_window(tmp_path: Path) -> None:
    infile = tmp_path / "in.log"
    outfile = tmp_path / "out.log"
    infile.write_text(
        "2026-10-27 23:00:01,123 [INFO] GET /mcp/tok1234/tools/list\n"
        "2026-10-27 23:00:02,000 [INFO] Authorization: Bearer aaa.bbb.ccc\n",
        encoding="utf-8",
    )
    rc = main([str(infile), str(outfile)])
    assert rc == 0
    text = outfile.read_text(encoding="utf-8")
    assert "/mcp/<redacted>/tools/list" in text
    assert "[REDACTED]" in text
    assert "tok1234" not in text
    assert "aaa.bbb.ccc" not in text

    assert main(["--normalize-window", "2h"]) == 0
    assert main(["--normalize-window", "invalid-window"]) == 2


def test_export_script_captures_docker_stderr_and_fails_on_invalid_window(
    tmp_path: Path,
) -> None:
    script = SCRIPTS_DALTON / "export_review_window.sh"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_docker = fake_bin / "docker"
    fake_docker.write_text(
        "#!/usr/bin/env bash\n"
        'if [[ "$1" == "inspect" ]]; then\n'
        '  echo "sha256:fakeimage"\n'
        "  exit 0\n"
        "fi\n"
        'echo "2026-10-27 23:00:02,000 [INFO] stdout line api_key=stdout_secret_111"\n'
        'echo "2026-10-27 23:00:01,000 [ERROR] stderr root logger Bearer stderr_secret_222" >&2\n',
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    (log_dir / "dalton-researchdata-bridge.err.log").write_text(
        "2026-10-27 22:00:00,000 [INFO] outside window before\n"
        "2026-10-27 23:00:03,000 [INFO] inside window /mcp/bridge_secret_333\n",
        encoding="utf-8",
    )
    (log_dir / "dalton-web-search-bridge.err.log").write_text(
        '2026-10-27 23:00:04,000 [INFO] web search {"password": "ws_secret_444"}\n',
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}",
        "DALTON_LOG_DIR": str(log_dir),
        "DALTON_PYTHON": sys.executable,
    }
    out_file = tmp_path / "exported.txt"
    proc = subprocess.run(
        [
            "bash",
            str(script),
            "--since",
            "2026-10-27 22:50:00",
            "--until",
            "2026-10-27 23:10:00",
            "-o",
            str(out_file),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    exported = out_file.read_text(encoding="utf-8")
    # Both docker stdout and docker stderr are present, sorted by timestamp, and redacted.
    assert "stderr root logger" in exported
    assert "stdout line" in exported
    assert exported.index("stderr root logger") < exported.index("stdout line")
    for secret in (
        "stdout_secret_111",
        "stderr_secret_222",
        "bridge_secret_333",
        "ws_secret_444",
    ):
        assert secret not in exported
    # Window filter dropped the 22:00 line from the bridge log.
    assert "outside window before" not in exported
    assert "/mcp/<redacted>" in exported

    # Invalid --since or --until must fail loudly and never export the bridge log.
    bad_out = tmp_path / "bad_export.txt"
    bad_proc = subprocess.run(
        [
            "bash",
            str(script),
            "--since",
            "not-a-valid-window",
            "-o",
            str(bad_out),
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert bad_proc.returncode != 0
    assert not bad_out.exists()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("HTTP 429 rate limited", 429),
        ("status 503 service unavailable", 503),
        ("429 Too Many Requests", 429),
        ('{"code":"E_UPSTREAM_API","status_code":403,"message":"Forbidden"}', 403),
        ("{'code': 'E_UPSTREAM_API', 'status_code': 403, 'message': 'Forbidden'}", 403),
    ],
)
def test_http_status_from_text_variants(text: str, expected: int) -> None:
    assert http_status_from_text(text) == expected


# P0 fix3 (lead): review samples that slipped past fix2.
def test_prefix_keys_ending_in_a_dash_are_fully_redacted():
    import log_redaction as r

    google = "AIza" + "A" * 34 + "-"
    for text in (f"upstream rejected {google} end", f"upstream rejected {google}"):
        assert google not in r.redact_text(text)
    assert "sk-abcdefgh12345678-" not in r.redact_text("key sk-abcdefgh12345678-")


def test_relative_window_crossing_a_dst_change_is_exact():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    import log_redaction as r

    now = datetime(2026, 11, 1, 2, 30, tzinfo=ZoneInfo("America/New_York"))
    # Two real hours before 02:30 EST (after the fall-back) is 01:30 EDT.
    assert r.parse_window_timestamp("2h", now=now) == datetime(2026, 11, 1, 1, 30)


def test_a_forged_status_value_cannot_split_a_log_line_or_leak_a_token():
    import log_redaction as r

    cleaned = r.clean_log_field(
        "error\n/mcp/0123456789abcdef0123456789abcdef\u001b[31mforged=1", max_len=64
    )
    assert "\n" not in cleaned and "\x1b" not in cleaned
    assert "0123456789abcdef0123456789abcdef" not in cleaned
