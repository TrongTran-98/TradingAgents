"""Tests for MT5 OHLCV fetch functions (Phase 2).

See docs/plans/gold-scalping-mt5/PLAN.md and TRACKING.md. No real MT5
terminal is used here -- see mt5_test_utils.py. Covers DataFrame shape/column
normalization, the broker-server-time -> UTC conversion gotcha, and error
mapping onto errors.py's typed vendor-error taxonomy.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

import tradingagents.dataflows.mt5_session as mt5_session
import tradingagents.dataflows.mt5_vendor as mt5_vendor
from tradingagents.dataflows.errors import NoMarketDataError, VendorNotConfiguredError

from .mt5_test_utils import FakeMT5

_RATE_DTYPE = np.dtype([
    ("time", "i8"),
    ("open", "f8"),
    ("high", "f8"),
    ("low", "f8"),
    ("close", "f8"),
    ("tick_volume", "i8"),
    ("spread", "i4"),
    ("real_volume", "i8"),
])


def _rates(*rows: tuple) -> np.ndarray:
    return np.array(list(rows), dtype=_RATE_DTYPE)


@pytest.fixture
def fake_mt5(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    monkeypatch.setattr(mt5_session, "_mt5", None)
    return fake


@pytest.mark.unit
class TestResolveTimeframe:
    def test_maps_known_timeframes(self, fake_mt5):
        mt5 = mt5_session.mt5_module()
        assert mt5_vendor._resolve_timeframe(mt5, "15m") == fake_mt5.TIMEFRAME_M15
        assert mt5_vendor._resolve_timeframe(mt5, "4H") == fake_mt5.TIMEFRAME_H4

    def test_unsupported_timeframe_raises_value_error(self, fake_mt5):
        mt5 = mt5_session.mt5_module()
        with pytest.raises(ValueError, match="Unsupported timeframe"):
            mt5_vendor._resolve_timeframe(mt5, "3m")


@pytest.mark.unit
class TestGetMt5Rates:
    def test_returns_shaped_ascending_utc_dataframe(self, fake_mt5):
        # 2026-01-01 03:00 and 03:15 server time, offset=+3 -> 00:00/00:15 UTC.
        epoch = int(datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc).timestamp())
        fake_mt5.copy_rates_from_pos_result = _rates(
            (epoch + 900, 101.0, 102.0, 100.5, 101.5, 5, 1, 0),  # out of order on purpose
            (epoch, 100.0, 101.0, 99.5, 100.5, 10, 1, 0),
        )

        df = mt5_vendor.get_mt5_rates("XAUUSD", "15m", 2, utc_offset_hours=3)

        assert list(df.columns) == [
            "time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume",
        ]
        assert list(df["time"]) == [
            pd.Timestamp("2026-01-01 00:00:00"),
            pd.Timestamp("2026-01-01 00:15:00"),
        ]
        assert df["open"].tolist() == [100.0, 101.0]

    def test_ensures_symbol_before_fetching(self, fake_mt5):
        fake_mt5.symbol_info_value = None
        with pytest.raises(VendorNotConfiguredError):
            mt5_vendor.get_mt5_rates("BOGUS", "5m", 10)
        assert ("copy_rates_from_pos" not in [c[0] for c in fake_mt5.calls])

    def test_empty_result_raises_no_market_data_error(self, fake_mt5):
        fake_mt5.copy_rates_from_pos_result = _rates()
        fake_mt5.last_error_value = (1, "no bars")
        with pytest.raises(NoMarketDataError):
            mt5_vendor.get_mt5_rates("XAUUSD", "5m", 10)

    def test_none_result_raises_no_market_data_error(self, fake_mt5):
        fake_mt5.copy_rates_from_pos_result = None
        with pytest.raises(NoMarketDataError):
            mt5_vendor.get_mt5_rates("XAUUSD", "5m", 10)

    def test_passes_correct_timeframe_and_count_to_copy_rates(self, fake_mt5):
        fake_mt5.copy_rates_from_pos_result = _rates((0, 1.0, 1.0, 1.0, 1.0, 1, 0, 0))
        mt5_vendor.get_mt5_rates("XAUUSD", "1H", 50, utc_offset_hours=0)
        call = next(c for c in fake_mt5.calls if c[0] == "copy_rates_from_pos")
        symbol, timeframe, start_pos, count = call[1]
        assert (symbol, timeframe, start_pos, count) == ("XAUUSD", fake_mt5.TIMEFRAME_H1, 0, 50)


@pytest.mark.unit
class TestGetMt5RatesRange:
    def test_converts_utc_range_to_server_time_before_querying(self, fake_mt5):
        fake_mt5.copy_rates_range_result = _rates((0, 1.0, 1.0, 1.0, 1.0, 1, 0, 0))
        date_from = datetime(2026, 1, 1, 0, 0)
        date_to = datetime(2026, 1, 1, 4, 0)

        mt5_vendor.get_mt5_rates_range(
            "XAUUSD", "5m", date_from, date_to, utc_offset_hours=3
        )

        call = next(c for c in fake_mt5.calls if c[0] == "copy_rates_range")
        _symbol, _timeframe, server_from, server_to = call[1]
        assert server_from == datetime(2026, 1, 1, 3, 0)
        assert server_to == datetime(2026, 1, 1, 7, 0)

    def test_accepts_tz_aware_datetimes(self, fake_mt5):
        fake_mt5.copy_rates_range_result = _rates((0, 1.0, 1.0, 1.0, 1.0, 1, 0, 0))
        date_from = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
        date_to = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)

        mt5_vendor.get_mt5_rates_range(
            "XAUUSD", "5m", date_from, date_to, utc_offset_hours=0
        )

        call = next(c for c in fake_mt5.calls if c[0] == "copy_rates_range")
        _symbol, _timeframe, server_from, server_to = call[1]
        assert server_from == datetime(2026, 1, 1, 0, 0)
        assert server_to == datetime(2026, 1, 1, 4, 0)

    def test_empty_result_raises_no_market_data_error(self, fake_mt5):
        fake_mt5.copy_rates_range_result = _rates()
        with pytest.raises(NoMarketDataError):
            mt5_vendor.get_mt5_rates_range(
                "XAUUSD", "5m", datetime(2026, 1, 1), datetime(2026, 1, 2)
            )
