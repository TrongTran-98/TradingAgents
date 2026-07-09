"""Phase 2 of the 4H day-trading plan: stockstats indicators on 4H candles.

Indicator math is asserted against hand-verified references that do not depend
on the library's smoothing internals (RSI on monotonic series, MACD on a
constant series, Bollinger middle = 20-SMA on a linear ramp), plus the
window/keying/no-look-ahead behavior and the config-timeframe routing.
Conventions mirror test_intraday_resample.py / test_vendor_routing.py.
"""

import re

import pandas as pd
import pytest

import tradingagents.dataflows.intraday as intraday
import tradingagents.dataflows.y_finance as yfin
from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError

# "YYYY-mm-dd HH:MM: <value>" — one indicator value per closed 4H bar.
_LINE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}): (.+)$", re.M)


def _frame_from_closes(start: str, closes) -> pd.DataFrame:
    """A synthetic closed-4H-candle frame as load_intraday_ohlcv returns it."""
    closes = pd.Series(closes, dtype=float)
    idx = pd.date_range(start=start, periods=len(closes), freq="4h")
    return pd.DataFrame(
        {
            "Date": idx,
            "Open": closes,
            "High": closes + 1.0,
            "Low": closes - 1.0,
            "Close": closes,
            "Volume": [1000.0] * len(closes),
        }
    )


def _patch_loader(monkeypatch, frame: pd.DataFrame) -> list:
    """Replace the Phase 1 loader with a stub returning `frame`; record calls."""
    calls = []

    def fake_loader(symbol, curr_datetime, timeframe="4h"):
        calls.append((symbol, curr_datetime, timeframe))
        return frame.copy()

    monkeypatch.setattr(yfin, "load_intraday_ohlcv", fake_loader)
    return calls


def _parsed_lines(report: str) -> list[tuple[str, str]]:
    """(timestamp, value) pairs in report order (newest first)."""
    return [(m.group(1), m.group(2)) for m in _LINE.finditer(report)]


@pytest.mark.unit
class TestIndicatorValues:
    """Values checked against smoothing-agnostic, hand-computed references."""

    def test_rsi_is_100_on_strictly_rising_closes(self, monkeypatch):
        # Every delta is +1 => average loss is 0 => RSI = 100 by definition,
        # regardless of the smoothing flavor stockstats uses internally.
        _patch_loader(monkeypatch, _frame_from_closes("2026-01-05 00:00", [100.0 + i for i in range(30)]))
        report = yfin.get_intraday_stock_stats_indicators_window(
            "BTC-USD", "rsi", "2026-01-09 20:00", look_back_days=1
        )
        newest_value = _parsed_lines(report)[0][1]
        assert float(newest_value) == pytest.approx(100.0)

    def test_rsi_is_0_on_strictly_falling_closes(self, monkeypatch):
        # Every delta is -1 => average gain is 0 => RSI = 0.
        _patch_loader(monkeypatch, _frame_from_closes("2026-01-05 00:00", [200.0 - i for i in range(30)]))
        report = yfin.get_intraday_stock_stats_indicators_window(
            "BTC-USD", "rsi", "2026-01-09 20:00", look_back_days=1
        )
        newest_value = _parsed_lines(report)[0][1]
        assert float(newest_value) == pytest.approx(0.0)

    def test_macd_is_0_on_constant_closes(self, monkeypatch):
        # EMA12 == EMA26 == the constant => MACD = 0 exactly, independent of
        # the EMA adjust/min_periods details.
        _patch_loader(monkeypatch, _frame_from_closes("2026-01-05 00:00", [100.0] * 40))
        report = yfin.get_intraday_stock_stats_indicators_window(
            "BTC-USD", "macd", "2026-01-11 12:00", look_back_days=1
        )
        newest_value = _parsed_lines(report)[0][1]
        assert abs(float(newest_value)) < 1e-9

    def test_boll_middle_matches_hand_computed_20_sma(self, monkeypatch):
        # Closes 100, 101, ..., 129: the 20-bar SMA at the last bar is the
        # mean of 110..129 = 119.5 (hand-computed, no smoothing involved).
        _patch_loader(monkeypatch, _frame_from_closes("2026-01-05 00:00", [100.0 + i for i in range(30)]))
        report = yfin.get_intraday_stock_stats_indicators_window(
            "BTC-USD", "boll", "2026-01-09 20:00", look_back_days=1
        )
        newest_value = _parsed_lines(report)[0][1]
        assert float(newest_value) == pytest.approx(119.5)


@pytest.mark.unit
class TestWindowShape:
    def test_lines_keyed_by_full_bar_timestamps_newest_first(self, monkeypatch):
        _patch_loader(monkeypatch, _frame_from_closes("2026-01-05 00:00", [100.0 + i for i in range(30)]))
        report = yfin.get_intraday_stock_stats_indicators_window(
            "BTC-USD", "rsi", "2026-01-09 20:00", look_back_days=1
        )
        stamps = [ts for ts, _ in _parsed_lines(report)]
        # Bar 29 opens at 00:00 + 29*4h = 2026-01-09 20:00; newest listed first.
        assert stamps[0] == "2026-01-09 20:00"
        assert stamps == sorted(stamps, reverse=True)

    def test_look_back_days_converts_to_bars(self, monkeypatch):
        # 2 lookback days on a 24/7 4h market = 12 bars.
        _patch_loader(monkeypatch, _frame_from_closes("2026-01-05 00:00", [100.0 + i for i in range(30)]))
        report = yfin.get_intraday_stock_stats_indicators_window(
            "BTC-USD", "rsi", "2026-01-09 20:00", look_back_days=2
        )
        assert len(_parsed_lines(report)) == 12

    def test_unsupported_indicator_raises_value_error(self, monkeypatch):
        _patch_loader(monkeypatch, _frame_from_closes("2026-01-05 00:00", [100.0] * 10))
        with pytest.raises(ValueError, match="not supported"):
            yfin.get_intraday_stock_stats_indicators_window(
                "BTC-USD", "bogus_indicator", "2026-01-06 16:00", look_back_days=1
            )

    def test_empty_frame_raises_no_market_data(self, monkeypatch):
        empty = _frame_from_closes("2026-01-05 00:00", [100.0]).iloc[:0]
        _patch_loader(monkeypatch, empty)
        with pytest.raises(NoMarketDataError):
            yfin.get_intraday_stock_stats_indicators_window(
                "BTC-USD", "rsi", "2026-01-06 16:00", look_back_days=1
            )


@pytest.mark.unit
class TestTimeframeRouting:
    def test_default_timeframe_routes_to_daily_path(self, monkeypatch):
        monkeypatch.setattr(
            yfin, "get_stock_stats_indicators_window", lambda *a: "DAILY"
        )
        monkeypatch.setattr(
            yfin, "get_intraday_stock_stats_indicators_window", lambda *a: "INTRADAY"
        )
        out = interface.route_to_vendor("get_indicators", "AAPL", "rsi", "2026-01-05", 30)
        assert out == "DAILY"

    def test_4h_timeframe_routes_to_intraday_path(self, monkeypatch):
        set_config({"timeframe": "4h"})
        intraday_calls = []
        monkeypatch.setattr(
            yfin, "get_stock_stats_indicators_window", lambda *a: "DAILY"
        )
        monkeypatch.setattr(
            yfin,
            "get_intraday_stock_stats_indicators_window",
            lambda *a: intraday_calls.append(a) or "INTRADAY",
        )
        out = interface.route_to_vendor(
            "get_indicators", "BTC-USD", "rsi", "2026-01-05 12:00", 30
        )
        assert out == "INTRADAY"
        # The configured timeframe is forwarded so bars-per-day conversion and
        # the loader's resample interval both follow it.
        assert intraday_calls == [("BTC-USD", "rsi", "2026-01-05 12:00", 30, "4h")]


@pytest.mark.unit
def test_no_lookahead_through_full_intraday_chain(monkeypatch, tmp_path):
    """route_to_vendor -> dispatcher -> loader -> stockstats, with 60m source
    data: no listed bar may close after the requested timestamp, and the
    still-forming bar never appears (mirrors test_intraday_resample.py)."""
    set_config({"data_cache_dir": str(tmp_path), "timeframe": "4h"})

    hours = 24
    idx = pd.date_range(start="2026-01-05 00:00", periods=hours, freq="60min")
    i = pd.RangeIndex(hours)
    hourly = pd.DataFrame(
        {
            "Open": 100.0 + i,
            "High": 110.0 + i,
            "Low": 90.0 - i,
            "Close": 105.0 + i,
            "Volume": 10 + i,
        },
        index=idx,
    )
    monkeypatch.setattr(intraday.yf, "download", lambda symbol, **kwargs: hourly)

    report = interface.route_to_vendor(
        "get_indicators", "BTC-USD", "rsi", "2026-01-05 13:47", 30
    )
    stamps = [ts for ts, _ in _parsed_lines(report)]
    assert stamps, "expected at least one closed bar in the report"
    # 12:00-16:00 is still forming at 13:47; last usable bar opened at 08:00.
    assert stamps[0] == "2026-01-05 08:00"
    cutoff = pd.Timestamp("2026-01-05 13:47")
    assert all(pd.Timestamp(ts) + pd.Timedelta("4h") <= cutoff for ts in stamps)
