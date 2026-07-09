"""Phase 3 config & CLI plumbing for the crypto-only 4h day-trading mode.

Covers: the ``timeframe`` config key + TRADINGAGENTS_TIMEFRAME env override
(per tests/test_env_overrides.py conventions), the crypto-gated timeframe
prompt (per tests/test_cli_symbol_handling.py style — pure logic, no TTY),
and the timeframe-aware analysis-date validation.
"""

from __future__ import annotations

import datetime
import importlib

import pytest

import cli.utils as cli_utils
import tradingagents.default_config as default_config_module
from cli.models import AssetType
from tradingagents.dataflows.time_utils import parse_trade_datetime


def _reload_with_env(monkeypatch, **overrides):
    """Set/clear env vars then reload default_config to re-evaluate DEFAULT_CONFIG."""
    for key in list(default_config_module._ENV_OVERRIDES):
        monkeypatch.delenv(key, raising=False)
    for key, val in overrides.items():
        monkeypatch.setenv(key, val)
    return importlib.reload(default_config_module)


# --- timeframe config key + env override ---

def test_timeframe_defaults_to_daily(monkeypatch):
    dc = _reload_with_env(monkeypatch)
    assert dc.DEFAULT_CONFIG["timeframe"] == "1d"


def test_timeframe_env_override(monkeypatch):
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_TIMEFRAME="4h")
    assert dc.DEFAULT_CONFIG["timeframe"] == "4h"


@pytest.mark.parametrize("raw", ["4H", " 4h ", "4H "])
def test_timeframe_env_canonicalized_to_lowercase(monkeypatch, raw):
    """Case/whitespace variants canonicalize instead of silently running daily."""
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_TIMEFRAME=raw)
    assert dc.DEFAULT_CONFIG["timeframe"] == "4h"


@pytest.mark.parametrize("bad", ["1h", "daily", "4hr", "240m", "intraday"])
def test_invalid_timeframe_raises(monkeypatch, bad):
    """An unsupported timeframe must fail loudly at import, not run in daily mode."""
    monkeypatch.setenv("TRADINGAGENTS_TIMEFRAME", bad)
    with pytest.raises(ValueError, match="TRADINGAGENTS_TIMEFRAME"):
        importlib.reload(default_config_module)
    monkeypatch.delenv("TRADINGAGENTS_TIMEFRAME", raising=False)
    importlib.reload(default_config_module)


def test_empty_timeframe_env_is_passthrough(monkeypatch):
    dc = _reload_with_env(monkeypatch, TRADINGAGENTS_TIMEFRAME="")
    assert dc.DEFAULT_CONFIG["timeframe"] == "1d"


def test_4h_rejects_indicator_vendor_without_yfinance():
    """4h + an alpha_vantage-only indicator chain would silently compute daily
    indicators (the intraday path is yfinance-only) — must be rejected."""
    cfg = {
        "timeframe": "4h",
        "tool_vendors": {},
        "data_vendors": {"technical_indicators": "alpha_vantage"},
    }
    with pytest.raises(ValueError, match="yfinance"):
        default_config_module.validate_timeframe(cfg)


@pytest.mark.parametrize("chain", ["yfinance", "yfinance,alpha_vantage", "default"])
def test_4h_allows_vendor_chain_reaching_yfinance(chain):
    cfg = {
        "timeframe": "4h",
        "tool_vendors": {},
        "data_vendors": {"technical_indicators": chain},
    }
    assert default_config_module.validate_timeframe(cfg)["timeframe"] == "4h"


def test_daily_never_checks_vendors():
    """Daily mode is today's behavior — valid with any vendor configuration."""
    cfg = {"timeframe": "1d", "data_vendors": {"technical_indicators": "alpha_vantage"}}
    assert default_config_module.validate_timeframe(cfg)["timeframe"] == "1d"


# --- select_timeframe: prompt only for crypto, env skips it ---

def _forbid_prompt(monkeypatch):
    def _fail(*args, **kwargs):
        raise AssertionError("timeframe prompt must not be shown")

    monkeypatch.setattr(cli_utils.questionary, "select", _fail)


class _FakePrompt:
    def __init__(self, answer):
        self.answer = answer

    def ask(self):
        return self.answer


def test_stock_gets_no_prompt_and_stays_daily(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEFRAME", raising=False)
    _forbid_prompt(monkeypatch)
    assert cli_utils.select_timeframe(AssetType.STOCK) == "1d"


def test_stock_ignores_env_4h(monkeypatch):
    """Day trading is crypto-only: env-requested 4h on a stock stays daily."""
    monkeypatch.setenv("TRADINGAGENTS_TIMEFRAME", "4h")
    _forbid_prompt(monkeypatch)
    assert cli_utils.select_timeframe(AssetType.STOCK) == "1d"


def test_crypto_env_skips_prompt(monkeypatch):
    monkeypatch.setenv("TRADINGAGENTS_TIMEFRAME", "4H")
    _forbid_prompt(monkeypatch)
    assert cli_utils.select_timeframe(AssetType.CRYPTO) == "4h"


def test_crypto_prompts_without_env(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEFRAME", raising=False)
    calls = []

    def fake_select(*args, **kwargs):
        calls.append(args)
        return _FakePrompt("4h")

    monkeypatch.setattr(cli_utils.questionary, "select", fake_select)
    assert cli_utils.select_timeframe(AssetType.CRYPTO) == "4h"
    assert len(calls) == 1


def test_crypto_prompt_cancel_falls_back_to_daily(monkeypatch):
    monkeypatch.delenv("TRADINGAGENTS_TIMEFRAME", raising=False)
    monkeypatch.setattr(
        cli_utils.questionary, "select", lambda *a, **k: _FakePrompt(None)
    )
    assert cli_utils.select_timeframe(AssetType.CRYPTO) == "1d"


# --- canonicalize_analysis_date: format widens only in 4h mode ---

def test_daily_accepts_date():
    assert cli_utils.canonicalize_analysis_date("2026-07-08", "1d") == "2026-07-08"


def test_daily_rejects_timestamp():
    """Passing HH:MM to a daily run is a user error, never silently truncated."""
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        cli_utils.canonicalize_analysis_date("2026-07-08 12:00", "1d")


def test_daily_rejects_future_date():
    tomorrow = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    with pytest.raises(ValueError, match="future"):
        cli_utils.canonicalize_analysis_date(tomorrow, "1d")


def test_4h_accepts_timestamp():
    assert (
        cli_utils.canonicalize_analysis_date("2026-07-08 12:00", "4h")
        == "2026-07-08 12:00"
    )


def test_4h_date_only_means_end_of_day():
    """Date-only in 4h mode reads as 23:59 — the last closed 4H bar of that day."""
    assert (
        cli_utils.canonicalize_analysis_date("2026-07-08", "4h")
        == "2026-07-08 23:59"
    )


def test_4h_todays_date_is_accepted():
    """Date-only "today" expands to 23:59 but must not be rejected as future —
    the data layer caps at the newest closed bar."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    assert cli_utils.canonicalize_analysis_date(today, "4h") == f"{today} 23:59"


def test_4h_rejects_future_date():
    tomorrow = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)
    ).strftime("%Y-%m-%d %H:%M")
    with pytest.raises(ValueError, match="future"):
        cli_utils.canonicalize_analysis_date(tomorrow, "4h")


def test_4h_rejects_garbage():
    with pytest.raises(ValueError):
        cli_utils.canonicalize_analysis_date("not-a-date", "4h")


def test_4h_normalizes_zero_padding():
    assert (
        cli_utils.canonicalize_analysis_date("2026-7-8 9:05", "4h")
        == "2026-07-08 09:05"
    )


# --- parse_trade_datetime (shared helper, Phase 0 design note 2) ---

def test_parse_trade_datetime_timestamp():
    assert parse_trade_datetime("2026-07-08 12:00") == datetime.datetime(
        2026, 7, 8, 12, 0
    )


def test_parse_trade_datetime_date_only_is_midnight():
    assert parse_trade_datetime("2026-07-08") == datetime.datetime(2026, 7, 8)


def test_parse_trade_datetime_invalid_raises():
    with pytest.raises(ValueError, match="Invalid trade date"):
        parse_trade_datetime("07/08/2026")


# --- selections -> run config wiring ---

def test_build_run_config_threads_timeframe(monkeypatch):
    import cli.main as cli_main

    monkeypatch.delenv("TRADINGAGENTS_MAX_DEBATE_ROUNDS", raising=False)
    monkeypatch.delenv("TRADINGAGENTS_MAX_RISK_ROUNDS", raising=False)
    selections = {
        "timeframe": "4h",
        "research_depth": 1,
        "shallow_thinker": "model-a",
        "deep_thinker": "model-b",
        "backend_url": None,
        "llm_provider": "openai",
    }
    assert cli_main._build_run_config(selections, None)["timeframe"] == "4h"
    # A selections dict without the key (non-interactive callers) stays daily.
    selections.pop("timeframe")
    assert cli_main._build_run_config(selections, None)["timeframe"] == "1d"
