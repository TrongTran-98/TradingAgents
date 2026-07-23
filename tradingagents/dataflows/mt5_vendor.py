"""MT5 OHLCV fetch functions: ``copy_rates_*`` -> normalized UTC OHLCV DataFrames.

Full design: docs/plans/gold-scalping-mt5/PLAN.md (Phase 2). Deliberately
**not** registered in ``interface.py``'s ``VENDOR_METHODS``/``route_to_vendor``
-- that routing abstraction is built around ``(symbol, curr_date)`` daily
rows, and intraday multi-timeframe bars don't fit it. This is an intentional
deviation, not an oversight: ``scalp_tools.py`` (Phase 4) calls the functions
below directly, while still reusing ``errors.py``'s typed vendor-error
taxonomy so failure handling reads consistently with the rest of the
dataflows package.

Known gotcha: MT5's ``copy_rates_*`` work in the broker's *server* time, not
UTC -- both the ``time`` field of returned bars and the ``date_from``/
``date_to`` range bounds. Every function here takes ``utc_offset_hours``
(``scalping.mt5_server_utc_offset_hours`` from config; convention:
``server_time = utc_time + offset``) and does the conversion at this
boundary, so every downstream consumer (``scalp_features.py``,
``session_window()``) only ever sees UTC timestamps.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

import pandas as pd

from . import mt5_session
from .errors import NoMarketDataError

_TIMEFRAME_ATTRS: dict[str, str] = {
    "1m": "TIMEFRAME_M1",
    "5m": "TIMEFRAME_M5",
    "15m": "TIMEFRAME_M15",
    "30m": "TIMEFRAME_M30",
    "1H": "TIMEFRAME_H1",
    "4H": "TIMEFRAME_H4",
    "1D": "TIMEFRAME_D1",
}

_RATE_COLUMNS: Sequence[str] = (
    "time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume",
)


def _resolve_timeframe(mt5, timeframe: str):
    attr = _TIMEFRAME_ATTRS.get(timeframe)
    if attr is None:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}; supported: {sorted(_TIMEFRAME_ATTRS)}"
        )
    return getattr(mt5, attr)


def _to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _rates_to_df(
    mt5, symbol: str, timeframe: str, rates, utc_offset_hours: float
) -> pd.DataFrame:
    """Normalize a raw ``copy_rates_*`` result into a UTC-sorted OHLCV DataFrame.

    Empty/``None`` results are always a data problem, never a valid "no
    bars" answer for a live symbol/timeframe pair, so they raise
    ``NoMarketDataError`` rather than returning an empty frame the caller
    might silently treat as "flat".
    """
    if rates is None or len(rates) == 0:
        code, description = mt5.last_error()
        raise NoMarketDataError(
            symbol, detail=f"MT5 returned no {timeframe} bars ({code}): {description}"
        )
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s") - pd.Timedelta(hours=utc_offset_hours)
    columns = [c for c in _RATE_COLUMNS if c in df.columns]
    return df[columns].sort_values("time").reset_index(drop=True)


def get_mt5_rates(
    symbol: str,
    timeframe: str,
    count: int,
    utc_offset_hours: float = 0.0,
) -> pd.DataFrame:
    """Most recent ``count`` bars for ``symbol``/``timeframe``, via ``copy_rates_from_pos``.

    Returns an ascending-by-time DataFrame with ``time`` already converted
    to UTC. Raises ``VendorNotConfiguredError`` (via
    ``mt5_session.ensure_symbol``) if the symbol isn't available, or
    ``NoMarketDataError`` if the terminal returns no bars.
    """
    mt5 = mt5_session.mt5_module()
    mt5_session.ensure_symbol(symbol)
    tf = _resolve_timeframe(mt5, timeframe)
    rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
    return _rates_to_df(mt5, symbol, timeframe, rates, utc_offset_hours)


def get_mt5_rates_range(
    symbol: str,
    timeframe: str,
    date_from: datetime,
    date_to: datetime,
    utc_offset_hours: float = 0.0,
) -> pd.DataFrame:
    """Bars for ``symbol``/``timeframe`` within ``[date_from, date_to]`` (UTC).

    ``date_from``/``date_to`` are interpreted as UTC (naive or tz-aware);
    this is what Phase 6's walk-forward resolver uses to fetch bars forward
    from a signal's ``generated_at_utc``. Converted to broker server time
    before querying MT5 -- same server-time gotcha as ``get_mt5_rates``.

    The resulting server-time value is passed to ``copy_rates_range`` as a
    **tz-aware UTC** datetime (``tzinfo=timezone.utc``), not a naive one --
    the ``MetaTrader5`` package silently reinterprets a naive datetime as
    the *host machine's local time* before converting it to an epoch, which
    silently shifts the query window by the host's local UTC offset on any
    machine not itself running in UTC (confirmed: a naive datetime here
    produced bars hours stale on a UTC+7 host). Tagging it UTC forces a
    literal digit-for-digit epoch conversion instead.
    """
    mt5 = mt5_session.mt5_module()
    mt5_session.ensure_symbol(symbol)
    tf = _resolve_timeframe(mt5, timeframe)

    offset = timedelta(hours=utc_offset_hours)
    server_from = (_to_utc_naive(date_from) + offset).replace(tzinfo=timezone.utc)
    server_to = (_to_utc_naive(date_to) + offset).replace(tzinfo=timezone.utc)

    rates = mt5.copy_rates_range(symbol, tf, server_from, server_to)
    return _rates_to_df(mt5, symbol, timeframe, rates, utc_offset_hours)
