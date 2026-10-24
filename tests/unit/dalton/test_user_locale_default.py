"""Dalton default locale for users with no saved ``users.locale``."""

from __future__ import annotations

import inspect
import pathlib

from src.dalton.user_locale import DEFAULT_USER_LOCALE, with_default_locale


def test_default_is_zh_cn():
    assert DEFAULT_USER_LOCALE == "zh-CN"


def test_null_or_empty_locale_gets_default():
    for value in (None, ""):
        profile = {"name": "A", "timezone": "Asia/Shanghai", "locale": value}
        out = with_default_locale(profile)
        assert out["locale"] == "zh-CN"
        assert out["name"] == "A"
        # Input (possibly cached) is not mutated.
        assert profile["locale"] == value


def test_missing_locale_key_gets_default():
    assert with_default_locale({"name": "A"})["locale"] == "zh-CN"


def test_saved_locale_is_kept():
    profile = {"locale": "en-US"}
    assert with_default_locale(profile) is profile


def test_failed_profile_read_stays_none():
    assert with_default_locale(None) is None


def test_frontend_default_matches():
    ts = pathlib.Path(__file__).resolve().parents[3] / "web/src/lib/dalton/userLocale.ts"
    assert f"DALTON_DEFAULT_USER_LOCALE: Locale = '{DEFAULT_USER_LOCALE}'" in ts.read_text()


def test_ptc_run_applies_default_to_prompt_profile():
    from src.server.handlers.chat import ptc_run

    src = inspect.getsource(ptc_run)
    i = src.index("user_profile = await get_user_profile_for_prompt(user_id)")
    assert "user_profile = with_default_locale(user_profile)" in src[i : i + 400]
