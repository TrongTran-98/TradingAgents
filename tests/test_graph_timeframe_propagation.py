"""Phase 4 of the 4H day-trading plan: graph/state datetime propagation.

An intraday run threads a full ``YYYY-mm-dd HH:MM`` trade date through the
LangGraph state. These tests pin the propagation contract:

- the initial state and checkpoint signature carry the timeframe;
- day-granular analysts (news, fundamentals, sentiment) see only the date
  portion, while the market analyst keeps the full timestamp;
- ``get_stock_data`` and the verified snapshot dispatch to the intraday
  loaders on ``timeframe != "1d"`` (mirroring test_intraday_indicators.py);
- state-log filenames stay filesystem-safe with a timestamp trade date.

Fake-LLM conventions follow test_structured_agents.py / test_market_toolnode.py
(no network, no real LLM).
"""

from types import SimpleNamespace

import pandas as pd
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable

import tradingagents.dataflows.market_data_validator as validator
import tradingagents.dataflows.y_finance as yfin
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.time_utils import filesystem_datetime_tag, trade_date_only
from tradingagents.graph.propagation import Propagator
from tradingagents.graph.trading_graph import TradingAgentsGraph

INTRADAY_STATE = {
    "trade_date": "2026-07-08 12:00",
    "timeframe": "4h",
    "company_of_interest": "BTC-USD",
    "asset_type": "crypto",
    "instrument_context": "The asset to analyze is `BTC-USD`.",
    "messages": [],
}


class _RecordingLLM:
    """Fake chat model: records every prompt it is invoked with."""

    def __init__(self):
        self.prompts = []

    def bind_tools(self, tools):
        outer = self

        class _Bound(Runnable):
            def invoke(self, input, config=None, **kwargs):
                outer.prompts.append(input)
                return AIMessage(content="stub report")

        return _Bound()

    def system_text(self) -> str:
        assert self.prompts, "the analyst never invoked the LLM"
        return self.prompts[-1].to_messages()[0].content


def _closed_4h_frame(start: str, n: int) -> pd.DataFrame:
    """A synthetic closed-4H-candle frame as load_intraday_ohlcv returns it."""
    closes = pd.Series(range(n), dtype=float) + 100.0
    return pd.DataFrame(
        {
            "Date": pd.date_range(start=start, periods=n, freq="4h"),
            "Open": closes,
            "High": closes + 1.0,
            "Low": closes - 1.0,
            "Close": closes,
            "Volume": [1000.0] * n,
        }
    )


@pytest.mark.unit
class TestTimeUtils:
    def test_trade_date_only_truncates_timestamp(self):
        assert trade_date_only("2026-07-08 12:00") == "2026-07-08"

    def test_trade_date_only_passes_through_date(self):
        assert trade_date_only("2026-07-08") == "2026-07-08"

    def test_trade_date_only_rejects_garbage(self):
        with pytest.raises(ValueError):
            trade_date_only("last tuesday")

    def test_filesystem_tag_sanitizes_timestamp(self):
        assert filesystem_datetime_tag("2026-07-08 12:00") == "2026-07-08_12-00"

    def test_filesystem_tag_keeps_daily_dates_identical(self):
        assert filesystem_datetime_tag("2026-07-08") == "2026-07-08"


@pytest.mark.unit
class TestStatePropagation:
    def test_initial_state_defaults_to_daily_timeframe(self):
        state = Propagator().create_initial_state("NVDA", "2026-07-08")
        assert state["timeframe"] == "1d"
        assert state["trade_date"] == "2026-07-08"

    def test_initial_state_carries_intraday_timeframe_and_timestamp(self):
        state = Propagator().create_initial_state(
            "BTC-USD", "2026-07-08 12:00", asset_type="crypto", timeframe="4h"
        )
        assert state["timeframe"] == "4h"
        assert state["trade_date"] == "2026-07-08 12:00"

    def test_run_signature_keyed_by_timeframe(self):
        # Bare instance to exercise the pure helper (see test_checkpoint_resume).
        g = object.__new__(TradingAgentsGraph)
        g.selected_analysts = ("market",)
        g.config = {"max_debate_rounds": 1, "max_risk_discuss_rounds": 1}
        daily_implicit = g._run_signature("crypto")
        g.config = {**g.config, "timeframe": "1d"}
        daily_explicit = g._run_signature("crypto")
        g.config = {**g.config, "timeframe": "4h"}
        intraday = g._run_signature("crypto")

        # A missing timeframe is daily; 4h must never resume a daily checkpoint.
        assert daily_implicit == daily_explicit
        assert intraday != daily_explicit


@pytest.mark.unit
class TestStateLogFilename:
    def _log(self, tmp_path, trade_date):
        g = object.__new__(TradingAgentsGraph)
        g.config = {"results_dir": str(tmp_path)}
        g.ticker = "BTC-USD"
        g.log_states_dict = {}
        final_state = {
            "company_of_interest": "BTC-USD",
            "trade_date": trade_date,
            "timeframe": "4h",
            "market_report": "",
            "sentiment_report": "",
            "news_report": "",
            "fundamentals_report": "",
            "investment_debate_state": {
                "bull_history": "", "bear_history": "", "history": "",
                "current_response": "", "judge_decision": "",
            },
            "trader_investment_plan": "",
            "risk_debate_state": {
                "aggressive_history": "", "conservative_history": "",
                "neutral_history": "", "history": "", "judge_decision": "",
            },
            "investment_plan": "",
            "final_trade_decision": "",
        }
        g._log_state(trade_date, final_state)
        return tmp_path / "BTC-USD" / "TradingAgentsStrategy_logs"

    def test_timestamp_trade_date_writes_sanitized_filename(self, tmp_path):
        directory = self._log(tmp_path, "2026-07-08 12:00")
        assert (directory / "full_states_log_2026-07-08_12-00.json").exists()
        for path in directory.iterdir():
            assert " " not in path.name and ":" not in path.name

    def test_daily_trade_date_filename_unchanged(self, tmp_path):
        directory = self._log(tmp_path, "2026-07-08")
        assert (directory / "full_states_log_2026-07-08.json").exists()


@pytest.mark.unit
class TestStockDataTimeframeRouting:
    def test_default_timeframe_routes_to_daily_path(self, monkeypatch):
        monkeypatch.setattr(yfin, "get_YFin_data_online", lambda *a: "DAILY")
        monkeypatch.setattr(yfin, "get_intraday_YFin_data", lambda *a: "INTRADAY")
        out = interface.route_to_vendor(
            "get_stock_data", "AAPL", "2026-06-01", "2026-07-08"
        )
        assert out == "DAILY"

    def test_4h_timeframe_routes_to_intraday_path(self, monkeypatch):
        set_config({"timeframe": "4h"})
        calls = []
        monkeypatch.setattr(yfin, "get_YFin_data_online", lambda *a: "DAILY")
        monkeypatch.setattr(
            yfin, "get_intraday_YFin_data", lambda *a: calls.append(a) or "INTRADAY"
        )
        out = interface.route_to_vendor(
            "get_stock_data", "BTC-USD", "2026-07-01", "2026-07-08 12:00"
        )
        assert out == "INTRADAY"
        assert calls == [("BTC-USD", "2026-07-01", "2026-07-08 12:00", "4h")]


@pytest.mark.unit
class TestIntradayStockData:
    def _patch_loader(self, monkeypatch, frame):
        calls = []

        def fake_loader(symbol, curr_datetime, timeframe="4h"):
            calls.append((symbol, curr_datetime, timeframe))
            return frame.copy()

        monkeypatch.setattr(yfin, "load_intraday_ohlcv", fake_loader)
        return calls

    def test_rows_are_timestamped_and_start_filtered(self, monkeypatch):
        self._patch_loader(monkeypatch, _closed_4h_frame("2026-07-07 00:00", 9))
        out = yfin.get_intraday_YFin_data(
            "BTC-USD", "2026-07-08 00:00", "2026-07-08 12:00"
        )
        assert "2026-07-08 00:00" in out
        assert "2026-07-07" not in out.split("\n\n", 1)[1]  # start cut applied to rows
        assert "labeled by bar open time" in out

    def test_date_only_end_reads_as_end_of_day(self, monkeypatch):
        # Design Note 2: a bare date means "the last closed bar of that day",
        # i.e. a 23:59 cutoff — not midnight (which would drop the whole day).
        calls = self._patch_loader(monkeypatch, _closed_4h_frame("2026-07-07 00:00", 9))
        yfin.get_intraday_YFin_data("BTC-USD", "2026-07-07", "2026-07-08")
        (_, cutoff, _), = calls
        assert cutoff.strftime("%Y-%m-%d %H:%M") == "2026-07-08 23:59"

    def test_empty_after_start_filter_raises_no_market_data(self, monkeypatch):
        self._patch_loader(monkeypatch, _closed_4h_frame("2026-07-07 00:00", 3))
        with pytest.raises(NoMarketDataError):
            yfin.get_intraday_YFin_data(
                "BTC-USD", "2026-07-09 00:00", "2026-07-09 12:00"
            )


@pytest.mark.unit
class TestVerifiedSnapshotTimeframe:
    def test_intraday_snapshot_uses_intraday_loader_and_timestamps(self, monkeypatch):
        set_config({"timeframe": "4h"})
        calls = []

        def fake_loader(symbol, curr_datetime, timeframe="4h"):
            calls.append((symbol, curr_datetime, timeframe))
            return _closed_4h_frame("2026-07-07 00:00", 30)

        monkeypatch.setattr(validator, "load_intraday_ohlcv", fake_loader)
        monkeypatch.setattr(
            validator, "load_ohlcv",
            lambda *a: pytest.fail("daily loader must not run in 4h mode"),
        )
        out = validator.build_verified_market_snapshot("BTC-USD", "2026-07-08 12:00")
        assert calls == [("BTC-USD", "2026-07-08 12:00", "4h")]
        # Bar 29 opens at 00:00 + 29*4h = 2026-07-11 20:00, but the cutoff at
        # 2026-07-08 12:00 keeps only bars opening at or before it.
        assert "| 2026-07-08 08:00 |" in out
        assert "labeled by bar open time" in out

    def test_daily_snapshot_still_uses_daily_loader(self, monkeypatch):
        frame = pd.DataFrame(
            {
                "Date": pd.date_range("2026-06-01", periods=30, freq="D"),
                "Open": 100.0, "High": 101.0, "Low": 99.0,
                "Close": 100.5, "Volume": 1000.0,
            }
        )
        monkeypatch.setattr(validator, "load_ohlcv", lambda s, d: frame)
        monkeypatch.setattr(
            validator, "load_intraday_ohlcv",
            lambda *a: pytest.fail("intraday loader must not run in daily mode"),
        )
        out = validator.build_verified_market_snapshot("AAPL", "2026-06-30")
        assert "| 2026-06-30 |" in out


@pytest.mark.unit
class TestAnalystTradeDateHandling:
    """Plan audit outcome: market keeps the timestamp; the day-granular
    analysts truncate to the date so their strict-%Y-%m-%d vendors never see
    an intraday timestamp."""

    def test_market_analyst_prompt_keeps_full_timestamp(self):
        from tradingagents.agents.analysts.market_analyst import create_market_analyst

        llm = _RecordingLLM()
        result = create_market_analyst(llm)(dict(INTRADAY_STATE))
        assert "Today's date is 2026-07-08 12:00" in llm.system_text()
        assert result["market_report"] == "stub report"

    def test_news_analyst_prompt_truncates_to_date(self):
        from tradingagents.agents.analysts.news_analyst import create_news_analyst

        llm = _RecordingLLM()
        create_news_analyst(llm)(dict(INTRADAY_STATE))
        assert "Today's date is 2026-07-08;" in llm.system_text()
        assert "12:00" not in llm.system_text()

    def test_fundamentals_analyst_prompt_truncates_to_date(self):
        from tradingagents.agents.analysts.fundamentals_analyst import (
            create_fundamentals_analyst,
        )

        llm = _RecordingLLM()
        create_fundamentals_analyst(llm)(dict(INTRADAY_STATE))
        assert "Today's date is 2026-07-08;" in llm.system_text()
        assert "12:00" not in llm.system_text()

    def test_sentiment_analyst_fetches_day_granular_news_window(self, monkeypatch):
        import tradingagents.agents.analysts.sentiment_analyst as sa

        news_calls = []
        monkeypatch.setattr(
            sa, "get_news",
            SimpleNamespace(func=lambda t, s, e: news_calls.append((t, s, e)) or "news"),
        )
        monkeypatch.setattr(sa, "fetch_stocktwits_messages", lambda t, limit=30: "st")
        monkeypatch.setattr(sa, "fetch_reddit_posts", lambda t: "reddit")
        monkeypatch.setattr(sa, "bind_structured", lambda llm, schema, name: llm)
        monkeypatch.setattr(
            sa, "invoke_structured_or_freetext", lambda *a, **k: "sentiment report"
        )

        result = sa.create_sentiment_analyst(object())(dict(INTRADAY_STATE))
        # 7-day window computed from the date portion — a raw timestamp would
        # have raised in _seven_days_back's strict strptime.
        assert news_calls == [("BTC-USD", "2026-07-01", "2026-07-08")]
        assert result["sentiment_report"] == "sentiment report"


@pytest.mark.unit
def test_market_analyst_intraday_tools_accept_timestamp(monkeypatch):
    """Plan Phase 4 integration check: with timeframe=4h, the tools the market
    analyst calls with its timestamp date all reach the intraday path without
    raising on the format."""
    set_config({"timeframe": "4h"})
    monkeypatch.setattr(yfin, "load_intraday_ohlcv", lambda *a: _closed_4h_frame("2026-07-05 00:00", 30))

    from tradingagents.agents.utils.core_stock_tools import get_stock_data
    from tradingagents.agents.utils.technical_indicators_tools import get_indicators

    prices = get_stock_data.func("BTC-USD", "2026-07-07 00:00", "2026-07-08 12:00")
    assert "2026-07-08 08:00" in prices  # last closed bar at 12:00

    rsi = get_indicators.func("BTC-USD", "rsi", "2026-07-08 12:00", 1)
    assert "4h bars" in rsi
