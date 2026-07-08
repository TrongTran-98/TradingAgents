"""Intraday OHLCV loading: yfinance 60m bars resampled into 4H candles.

Phase 1 of docs/plans/4h-day-trading-plan.md. Design decisions (timestamp
format, bar anchoring, closed-bar rule) are locked in that plan's Design
Notes; the invariants enforced here are:

- Bars anchor on fixed UTC boundaries (``origin="epoch"`` -> 00/04/08/12/16/20
  for 4h) and are labeled by their *open* time, matching yfinance's 60m labels.
- A bar opening at ``O`` is closed iff ``O + bar <= min(curr_datetime, now)``.
  The still-forming bar (and any partial trailing 60m row inside it) never
  reaches indicators or agents, and rows after ``curr_datetime`` are dropped to
  preserve the daily path's look-ahead guarantee.
- All returned timestamps are tz-naive UTC — the plan's canonical clock.
"""

import logging
import os

import pandas as pd
import yfinance as yf

from .config import get_config
from .errors import NoMarketDataError
from .stockstats_utils import _clean_dataframe, _ensure_date_column, yf_retry
from .symbol_utils import normalize_symbol
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)

# yfinance rejects 60m requests reaching further back than ~730 days; stay
# safely under the cap (the daily loader's 5-year window does not fit here).
INTRADAY_HISTORY_DAYS = 720

# A latest bar closing this many hours before the requested timestamp is
# treated as stale — the intraday analogue of MAX_OHLCV_STALE_DAYS. Crypto
# trades 24/7 so gaps are never legitimate; 48h is generous for vendor lag
# while still catching a months-old frame.
MAX_INTRADAY_STALE_HOURS = 48

_OHLCV_AGG = {
    "Open": "first",
    "High": "max",
    "Low": "min",
    "Close": "last",
    "Volume": "sum",
}


def _to_naive_utc(dates: pd.Series) -> pd.Series:
    """Normalize a datetime series to tz-naive UTC.

    yfinance intraday frames carry tz-aware UTC timestamps (and a CSV
    round-trip yields offset strings); the daily path is naive throughout, so
    the intraday path strips the offset after converting to UTC.
    """
    if dates.dtype == object:
        dates = pd.to_datetime(dates, utc=True, errors="coerce")
    if isinstance(dates.dtype, pd.DatetimeTZDtype):
        return dates.dt.tz_convert("UTC").dt.tz_localize(None)
    return dates


def _parse_curr_datetime(curr_datetime) -> pd.Timestamp:
    """Parse the requested timestamp (str or datetime) to tz-naive UTC."""
    parsed = pd.to_datetime(curr_datetime)
    if parsed.tzinfo is not None:
        parsed = parsed.tz_convert("UTC").tz_localize(None)
    return parsed


def resample_ohlcv(data: pd.DataFrame, timeframe: str = "4h") -> pd.DataFrame:
    """Resample 60m OHLCV rows into ``timeframe`` candles with OHLC-correct aggregation.

    Expects a frame with a parsed ``Date`` column (tz-naive UTC) as produced
    by ``_clean_dataframe`` + ``_to_naive_utc``. Output bars are labeled by
    open time on fixed epoch-anchored UTC boundaries; bins with no source rows
    (vendor gaps) are dropped rather than emitted as NaN candles.
    """
    frame = data.set_index("Date").sort_index()
    agg = {c: rule for c, rule in _OHLCV_AGG.items() if c in frame.columns}
    return (
        frame[list(agg)]
        .resample(timeframe.lower(), origin="epoch", label="left", closed="left")
        .agg(agg)
        .dropna(subset=["Close"])
        .reset_index()
    )


def _assert_intraday_not_stale(
    bars: pd.DataFrame,
    curr_dt: pd.Timestamp,
    bar: pd.Timedelta,
    symbol: str,
    canonical: str,
    *,
    max_stale_hours: int = MAX_INTRADAY_STALE_HOURS,
) -> None:
    """Reject a frame whose latest bar closed far before the requested time.

    Mirrors ``_assert_ohlcv_not_stale`` semantics (raise ``NoMarketDataError``
    so the router treats it as "no usable data"; empty frames are left to the
    caller) but measures staleness in hours from the latest bar *close*.
    """
    if bars is None or bars.empty:
        return
    latest_close = bars["Date"].max() + bar
    stale_hours = (curr_dt - latest_close) / pd.Timedelta(hours=1)
    if stale_hours > max_stale_hours:
        raise NoMarketDataError(
            symbol,
            canonical,
            f"latest {bar} bar closed {latest_close}, {stale_hours:.0f}h before "
            f"the requested {curr_dt} (stale) — refusing to use it",
        )


def load_intraday_ohlcv(symbol: str, curr_datetime, timeframe: str = "4h") -> pd.DataFrame:
    """Fetch 60m bars with caching, resampled to closed ``timeframe`` candles.

    Intraday counterpart of ``load_ohlcv``. ``curr_datetime`` is a tz-naive
    UTC timestamp (``YYYY-mm-dd HH:MM`` string or datetime); the returned
    frame's ``Date`` column holds bar *open* times and every bar is fully
    closed at or before ``min(curr_datetime, now)`` — closure at exactly the
    requested time counts as closed.

    Caching: the raw 60m download is cached (not the resampled candles, so a
    partial trailing hourly row is never frozen into a candle). The filename
    embeds the timeframe and the current closed-bar boundary, so a new file is
    fetched once per closed bar and can never collide with the daily cache's
    ``{symbol}-YFin-data-*`` pattern.
    """
    canonical = normalize_symbol(symbol)
    safe_symbol = safe_ticker_component(canonical)

    bar = pd.Timedelta(timeframe)
    if bar <= pd.Timedelta(0) or pd.Timedelta("1d") % bar != pd.Timedelta(0):
        raise ValueError(
            f"timeframe {timeframe!r} must evenly divide 24h so epoch-anchored "
            f"bars fall on fixed daily UTC boundaries"
        )

    curr_dt = _parse_curr_datetime(curr_datetime)
    config = get_config()

    now_utc = pd.Timestamp.now(tz="UTC").tz_localize(None)
    # Close of the most recently closed bar by wall clock: cache freshness is
    # keyed on it, so a run in a later bar window re-fetches instead of serving
    # a frame frozen at the previous bar (the daily loader's per-day window is
    # too coarse for intraday).
    boundary = now_utc.floor(bar)
    start_str = (now_utc - pd.Timedelta(days=INTRADAY_HISTORY_DAYS)).strftime("%Y-%m-%d")
    # yfinance ``end`` is EXCLUSIVE; request tomorrow so today's rows are
    # included. Look-ahead is still prevented by the closed-bar filter below.
    end_str = (now_utc + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    boundary_tag = boundary.strftime("%Y-%m-%dT%H%M")
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-{timeframe}-data-{start_str}-{boundary_tag}.csv",
    )

    # Treat an empty/columnless cache as a miss (see load_ohlcv).
    data = None
    if os.path.exists(data_file):
        cached = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
        if not cached.empty and "Close" in cached.columns:
            data = cached

    if data is None:
        downloaded = yf_retry(lambda: yf.download(
            canonical,
            start=start_str,
            end=end_str,
            interval="60m",
            multi_level_index=False,
            progress=False,
            auto_adjust=True,
        ))
        downloaded = _ensure_date_column(downloaded.reset_index())
        if downloaded.empty or "Close" not in downloaded.columns:
            raise NoMarketDataError(
                symbol, canonical, "Yahoo Finance returned no 60m rows"
            )
        downloaded.to_csv(data_file, index=False, encoding="utf-8")
        data = downloaded

    data = _clean_dataframe(data)
    data["Date"] = _to_naive_utc(data["Date"])
    data = data.dropna(subset=["Date"])

    bars = resample_ohlcv(data, timeframe)

    # Closed-bar + look-ahead filter: keep bars whose close (open label + bar
    # duration) is at or before the requested time, capped by wall clock so
    # the still-forming bar is dropped even for future-dated requests.
    cutoff = min(curr_dt, now_utc)
    bars = bars[bars["Date"] + bar <= cutoff].reset_index(drop=True)

    _assert_intraday_not_stale(bars, curr_dt, bar, symbol, canonical)

    return bars
