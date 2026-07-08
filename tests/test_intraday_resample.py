"""Phase 1 of the 4H day-trading plan: 60m -> 4H resampling and the
closed-bar / look-ahead guarantees of ``load_intraday_ohlcv``.

Mirrors the style of test_date_boundaries.py / test_news_lookahead.py:
synthetic frames via a monkeypatched ``yf.download``, per-test cache dir.
"""

import pandas as pd
import pytest

import tradingagents.dataflows.intraday as intraday
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.errors import NoMarketDataError


def _hourly_frame(start: str, hours: int, tz: str | None = None) -> pd.DataFrame:
    """Synthetic 60m OHLCV with per-row values derived from the row index.

    Row i: Open=100+i, High=110+i, Low=90-i, Close=105+i, Volume=10+i —
    deterministic so 4H aggregates can be hand-computed in assertions.
    """
    idx = pd.date_range(start=start, periods=hours, freq="60min", tz=tz)
    i = pd.RangeIndex(hours)
    return pd.DataFrame(
        {
            "Date": idx,
            "Open": 100.0 + i,
            "High": 110.0 + i,
            "Low": 90.0 - i,
            "Close": 105.0 + i,
            "Volume": 10 + i,
        }
    )


def _patch_download(monkeypatch, frame: pd.DataFrame):
    def fake_download(symbol, **kwargs):
        fake_download.calls.append(kwargs)
        return frame.set_index("Date")

    fake_download.calls = []
    monkeypatch.setattr(intraday.yf, "download", fake_download)
    return fake_download


@pytest.mark.unit
def test_resample_matches_hand_computed_ohlcv():
    # 8 hourly bars starting exactly on a 4H boundary -> two full 4H candles.
    frame = _hourly_frame("2026-01-05 00:00", 8)
    bars = intraday.resample_ohlcv(frame, "4h")

    assert list(bars["Date"]) == [
        pd.Timestamp("2026-01-05 00:00"),
        pd.Timestamp("2026-01-05 04:00"),
    ]
    # Bar 1 aggregates rows 0..3: Open=first row's Open, High=max, Low=min,
    # Close=last row's Close, Volume=sum.
    b0 = bars.iloc[0]
    assert b0["Open"] == 100.0
    assert b0["High"] == 113.0
    assert b0["Low"] == 87.0
    assert b0["Close"] == 108.0
    assert b0["Volume"] == 10 + 11 + 12 + 13
    # Bar 2 aggregates rows 4..7.
    b1 = bars.iloc[1]
    assert b1["Open"] == 104.0
    assert b1["High"] == 117.0
    assert b1["Low"] == 83.0
    assert b1["Close"] == 112.0
    assert b1["Volume"] == 14 + 15 + 16 + 17


@pytest.mark.unit
def test_resample_anchors_on_epoch_utc_boundaries():
    # Data starting mid-bar (02:00) must still bin into the 00:00-anchored
    # grid, not start a bar at 02:00.
    frame = _hourly_frame("2026-01-05 02:00", 6)
    bars = intraday.resample_ohlcv(frame, "4h")

    assert list(bars["Date"]) == [
        pd.Timestamp("2026-01-05 00:00"),
        pd.Timestamp("2026-01-05 04:00"),
    ]
    assert all(ts.hour % 4 == 0 and ts.minute == 0 for ts in bars["Date"])
    # First bin only has rows 02:00+03:00 (i=0,1): Open is the 02:00 row's.
    assert bars.iloc[0]["Open"] == 100.0
    assert bars.iloc[0]["Volume"] == 10 + 11


@pytest.mark.unit
def test_load_excludes_future_and_forming_bars(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    # Hourly data through 2026-01-05 23:00 (well in the past => the wall-clock
    # cap never binds and the requested timestamp drives the filter).
    _patch_download(monkeypatch, _hourly_frame("2026-01-05 00:00", 24))

    # Request mid-bar at 13:47: the 12:00-16:00 bar is still forming, the
    # last usable bar is 08:00-12:00 (labeled by open 08:00).
    bars = intraday.load_intraday_ohlcv("BTC-USD", "2026-01-05 13:47")
    assert bars["Date"].max() == pd.Timestamp("2026-01-05 08:00")
    # No bar in the frame closes after the requested time.
    assert ((bars["Date"] + pd.Timedelta("4h")) <= pd.Timestamp("2026-01-05 13:47")).all()


@pytest.mark.unit
def test_load_close_at_exactly_requested_time_counts_as_closed(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    _patch_download(monkeypatch, _hourly_frame("2026-01-05 00:00", 24))

    # At 12:00 sharp the 08:00-12:00 bar has just closed and is included.
    bars = intraday.load_intraday_ohlcv("BTC-USD", "2026-01-05 12:00")
    assert bars["Date"].max() == pd.Timestamp("2026-01-05 08:00")

    # One minute earlier it is still forming and must be absent.
    bars = intraday.load_intraday_ohlcv("BTC-USD", "2026-01-05 11:59")
    assert bars["Date"].max() == pd.Timestamp("2026-01-05 04:00")


@pytest.mark.unit
def test_load_normalizes_tz_aware_vendor_timestamps(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    # yfinance intraday frames are tz-aware UTC; output must be naive UTC.
    _patch_download(monkeypatch, _hourly_frame("2026-01-05 00:00", 24, tz="UTC"))

    bars = intraday.load_intraday_ohlcv("BTC-USD", "2026-01-05 13:47")
    assert bars["Date"].dt.tz is None
    assert bars["Date"].max() == pd.Timestamp("2026-01-05 08:00")


@pytest.mark.unit
def test_load_caches_raw_60m_under_interval_specific_name(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    fake = _patch_download(monkeypatch, _hourly_frame("2026-01-05 00:00", 24))

    intraday.load_intraday_ohlcv("BTC-USD", "2026-01-05 12:00")
    cache_files = list(tmp_path.iterdir())
    assert len(cache_files) == 1
    name = cache_files[0].name
    # Interval in the name => can never collide with daily's
    # ``{symbol}-YFin-data-*`` pattern (which has no interval component).
    assert "-YFin-4h-data-" in name
    assert fake.calls[0]["interval"] == "60m"

    # Second call within the same closed-bar window reuses the cache.
    intraday.load_intraday_ohlcv("BTC-USD", "2026-01-05 13:47")
    assert len(fake.calls) == 1


@pytest.mark.unit
def test_load_rejects_stale_frame(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    # Latest bar closes 2026-01-01 00:00; requesting 4 days later is far past
    # the 48h staleness budget.
    _patch_download(monkeypatch, _hourly_frame("2025-12-31 00:00", 24))

    with pytest.raises(NoMarketDataError):
        intraday.load_intraday_ohlcv("BTC-USD", "2026-01-05 00:00")


@pytest.mark.unit
def test_load_rejects_empty_vendor_frame(monkeypatch, tmp_path):
    set_config({"data_cache_dir": str(tmp_path)})
    _patch_download(monkeypatch, pd.DataFrame({"Date": pd.to_datetime([])}))

    with pytest.raises(NoMarketDataError):
        intraday.load_intraday_ohlcv("BTC-USD", "2026-01-05 12:00")
    # A failed fetch must not poison the cache with an empty file.
    assert not any(f.suffix == ".csv" and f.stat().st_size > 0 for f in tmp_path.iterdir())


@pytest.mark.unit
def test_load_rejects_timeframe_not_dividing_a_day():
    with pytest.raises(ValueError):
        intraday.load_intraday_ohlcv("BTC-USD", "2026-01-05 12:00", timeframe="5h")
