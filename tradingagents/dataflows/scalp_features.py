"""Deterministic feature extraction for the gold-scalping pipeline.

Full design: docs/plans/gold-scalping-mt5/PLAN.md (Phase 1). Every function
here is a pure function of a DataFrame/params — no I/O, no LLM calls, no MT5
dependency — so they can be unit tested in isolation before the analysts
(Phase 4) reason over their output.

DataFrame contract: every function expects lowercase OHLC columns —
``time`` (a ``pd.Timestamp``), ``open``, ``high``, ``low``, ``close`` — one
row per bar, sorted ascending by time. This matches the field names MT5's
``copy_rates_*`` naturally returns (Phase 2), so no renaming layer is needed
between ``mt5_vendor.py`` and this module.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

import pandas as pd

# ---------------------------------------------------------------------------
# Shared result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Swing:
    """A single fractal swing point."""

    index: int
    time: object
    price: float
    kind: str  # "high" | "low"


@dataclass(frozen=True)
class EmaStack:
    fast: float
    slow: float
    fast_slope: float
    slow_slope: float
    alignment: str  # "bullish" | "bearish" | "compressed"


@dataclass(frozen=True)
class AtrRegime:
    atr: float
    percentile: float
    regime: str  # "low" | "normal" | "high"


@dataclass(frozen=True)
class PriorLevels:
    prior_day_high: float | None
    prior_day_low: float | None
    prior_day_close: float | None
    prior_week_high: float | None
    prior_week_low: float | None


@dataclass(frozen=True)
class PivotPoints:
    pp: float
    r1: float
    s1: float
    r2: float
    s2: float


@dataclass(frozen=True)
class StructureEvent:
    kind: str  # "BOS" | "CHoCH" | "none"
    direction: str | None  # "bullish" | "bearish" | None
    close_price: float | None
    broken_level: float | None
    displacement_atr: float | None
    bar_index: int | None


@dataclass(frozen=True)
class LiquiditySweepResult:
    detected: bool
    direction: str | None  # "bullish" | "bearish" | None
    swept_level: float | None
    wick_atr: float | None
    bar_index: int | None


@dataclass(frozen=True)
class OrderBlock:
    index: int
    time: object
    direction: str  # "bullish" | "bearish" -- direction of the displacement it precedes
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class FairValueGap:
    index: int
    time: object
    direction: str  # "bullish" | "bearish"
    gap_low: float
    gap_high: float
    filled: bool


@dataclass(frozen=True)
class MomentumSnapshot:
    rsi: float
    stoch_k: float
    stoch_d: float
    ema: float
    price_at_ema: bool
    momentum_turn: str  # "bullish" | "bearish" | "none"


# ---------------------------------------------------------------------------
# ATR helpers (shared by several functions below)
# ---------------------------------------------------------------------------


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    high_low = df["high"] - df["low"]
    high_prev_close = (df["high"] - prev_close).abs()
    low_prev_close = (df["low"] - prev_close).abs()
    return pd.concat([high_low, high_prev_close, low_prev_close], axis=1).max(axis=1)


def _atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder-smoothed ATR (matches the standard indicator, not a simple SMA of TR)."""
    tr = _true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


# ---------------------------------------------------------------------------
# Structure: swings, HH/HL classification, BOS/CHoCH
# ---------------------------------------------------------------------------


def detect_swings(df: pd.DataFrame, n: int = 2) -> list[Swing]:
    """Symmetric N-bar fractal swing-high/low detection.

    Bar ``i`` is a swing high when its high is strictly greater than every
    other high in the ``2n+1``-bar window centered on it (swing lows
    mirrored on lows). Requiring a *unique* max/min in the window excludes
    flat-top/bottom plateaus from producing multiple swing points at the
    same price.
    """
    if n < 1:
        raise ValueError("n must be >= 1")
    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    times = df["time"].to_numpy() if "time" in df.columns else [None] * len(df)

    swings: list[Swing] = []
    for i in range(n, len(df) - n):
        window_high = highs[i - n : i + n + 1]
        if highs[i] == window_high.max() and (window_high == highs[i]).sum() == 1:
            swings.append(Swing(i, times[i], float(highs[i]), "high"))
        window_low = lows[i - n : i + n + 1]
        if lows[i] == window_low.min() and (window_low == lows[i]).sum() == 1:
            swings.append(Swing(i, times[i], float(lows[i]), "low"))
    return sorted(swings, key=lambda s: s.index)


def classify_structure(swings: Sequence[Swing]) -> str:
    """Classify market structure from the two most recent swing highs/lows.

    HH + HL -> "bullish", LH + LL -> "bearish", anything else (including
    fewer than two highs or two lows to compare) -> "range".
    """
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if len(highs) < 2 or len(lows) < 2:
        return "range"

    higher_high = highs[-1].price > highs[-2].price
    higher_low = lows[-1].price > lows[-2].price
    lower_high = highs[-1].price < highs[-2].price
    lower_low = lows[-1].price < lows[-2].price

    if higher_high and higher_low:
        return "bullish"
    if lower_high and lower_low:
        return "bearish"
    return "range"


def detect_bos_choch(
    df: pd.DataFrame,
    swings: Sequence[Swing],
    min_displacement_atr: float = 0.25,
    atr_period: int = 14,
) -> StructureEvent:
    """Close-based BOS/CHoCH detection on the latest bar.

    BOS = close beyond the most recent same-direction swing point (agrees
    with the trend from ``classify_structure``) by >= ``min_displacement_atr``
    x ATR. CHoCH = close beyond the most recent opposite-direction swing
    point by the same threshold. Deliberately close-based only: a wick-only
    pierce that closes back inside never reaches this comparison because
    only ``close`` (never ``high``/``low``) is compared to the swing level.
    When no trend is established (``classify_structure`` returns "range"),
    any qualifying break is reported as BOS — there is no trend to change
    away from.
    """
    if df.empty or len(df) < 2:
        return StructureEvent("none", None, None, None, None, None)

    atr_series = _atr(df, atr_period)
    current_atr = atr_series.iloc[-1]
    if pd.isna(current_atr) or current_atr <= 0:
        return StructureEvent("none", None, None, None, None, None)

    last_idx = len(df) - 1
    last_close = float(df["close"].iloc[-1])

    prior_swings = [s for s in swings if s.index < last_idx]
    swing_highs = [s for s in prior_swings if s.kind == "high"]
    swing_lows = [s for s in prior_swings if s.kind == "low"]
    if not swing_highs and not swing_lows:
        return StructureEvent("none", None, last_close, None, None, last_idx)

    trend = classify_structure(prior_swings)

    def displacement(level: float) -> float:
        return abs(last_close - level) / current_atr

    last_swing_high = swing_highs[-1] if swing_highs else None
    last_swing_low = swing_lows[-1] if swing_lows else None

    broke_high = (
        last_swing_high is not None
        and last_close > last_swing_high.price
        and displacement(last_swing_high.price) >= min_displacement_atr
    )
    broke_low = (
        last_swing_low is not None
        and last_close < last_swing_low.price
        and displacement(last_swing_low.price) >= min_displacement_atr
    )

    if broke_high and broke_low:
        # Degenerate case (very tight recent swing range) -- take whichever
        # break has the larger displacement rather than picking arbitrarily.
        if displacement(last_swing_high.price) >= displacement(last_swing_low.price):
            direction, level = "bullish", last_swing_high.price
        else:
            direction, level = "bearish", last_swing_low.price
    elif broke_high:
        direction, level = "bullish", last_swing_high.price
    elif broke_low:
        direction, level = "bearish", last_swing_low.price
    else:
        return StructureEvent("none", None, last_close, None, None, last_idx)

    if trend == "bullish":
        kind = "BOS" if direction == "bullish" else "CHoCH"
    elif trend == "bearish":
        kind = "BOS" if direction == "bearish" else "CHoCH"
    else:
        kind = "BOS"

    return StructureEvent(
        kind, direction, last_close, level, round(displacement(level), 4), last_idx
    )


# ---------------------------------------------------------------------------
# EMA stack, ATR regime
# ---------------------------------------------------------------------------


def ema_stack_alignment(
    df: pd.DataFrame,
    fast: int = 50,
    slow: int = 200,
    slope_lookback: int = 5,
    compressed_atr_multiple: float = 0.5,
    atr_period: int = 14,
) -> EmaStack | None:
    """EMA(fast) vs EMA(slow) relative position + slope.

    Returns "compressed" (rather than bullish/bearish) whenever the two EMAs
    sit within ``compressed_atr_multiple`` x ATR of each other -- an
    untradeable range regime where "which one is on top" is noise, not
    signal. Returns ``None`` when there isn't enough history to compute a
    stable slow EMA and slope.
    """
    if len(df) < slow + slope_lookback:
        return None

    close = df["close"]
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()

    atr_series = _atr(df, atr_period)
    current_atr = atr_series.iloc[-1]
    if pd.isna(current_atr) or current_atr <= 0:
        return None

    fast_now, slow_now = float(ema_fast.iloc[-1]), float(ema_slow.iloc[-1])
    fast_slope = fast_now - float(ema_fast.iloc[-1 - slope_lookback])
    slow_slope = slow_now - float(ema_slow.iloc[-1 - slope_lookback])

    if abs(fast_now - slow_now) < compressed_atr_multiple * current_atr:
        alignment = "compressed"
    elif fast_now > slow_now:
        alignment = "bullish"
    else:
        alignment = "bearish"

    return EmaStack(
        fast=round(fast_now, 4),
        slow=round(slow_now, 4),
        fast_slope=round(fast_slope, 4),
        slow_slope=round(slow_slope, 4),
        alignment=alignment,
    )


def atr_regime(
    df: pd.DataFrame,
    period: int = 14,
    lookback: int = 100,
    low_threshold: float = 0.33,
    high_threshold: float = 0.66,
) -> AtrRegime | None:
    """Classify current ATR against its own rolling percentile history."""
    atr_series = _atr(df, period)
    valid = atr_series.dropna()
    if valid.empty:
        return None

    window = valid.iloc[-lookback:] if lookback else valid
    current = float(valid.iloc[-1])
    percentile = float((window <= current).mean())

    if percentile < low_threshold:
        regime = "low"
    elif percentile > high_threshold:
        regime = "high"
    else:
        regime = "normal"

    return AtrRegime(atr=round(current, 4), percentile=round(percentile, 4), regime=regime)


# ---------------------------------------------------------------------------
# Key levels: prior day/week, round numbers, pivots
# ---------------------------------------------------------------------------


def prior_day_week_levels(df: pd.DataFrame) -> PriorLevels:
    """Prior (most recent *complete*) calendar day and ISO week high/low.

    "Prior" excludes the in-progress day/week the last bar belongs to.
    Fields are ``None`` (never fabricated) when there isn't a full prior
    period in the supplied history.
    """
    if df.empty:
        return PriorLevels(None, None, None, None, None)

    times = pd.to_datetime(df["time"])
    dates = times.dt.date
    current_date = dates.iloc[-1]

    day_key = pd.Series(dates.to_numpy(), index=df.index)
    prior_day_high = prior_day_low = prior_day_close = None
    prior_days = sorted(d for d in day_key.unique() if d < current_date)
    if prior_days:
        prior_day = prior_days[-1]
        prior_df = df.loc[day_key == prior_day]
        prior_day_high = float(prior_df["high"].max())
        prior_day_low = float(prior_df["low"].min())
        prior_day_close = float(prior_df["close"].iloc[-1])

    iso = times.dt.isocalendar()
    week_key = pd.Series(list(zip(iso["year"], iso["week"], strict=True)), index=df.index)
    current_week = week_key.iloc[-1]
    prior_week_high = prior_week_low = None
    prior_weeks = sorted(w for w in week_key.unique() if w < current_week)
    if prior_weeks:
        prior_week = prior_weeks[-1]
        prior_week_df = df.loc[week_key == prior_week]
        prior_week_high = float(prior_week_df["high"].max())
        prior_week_low = float(prior_week_df["low"].min())

    return PriorLevels(
        prior_day_high, prior_day_low, prior_day_close, prior_week_high, prior_week_low
    )


def round_number_levels(price: float, step: float = 1.0, count: int = 3) -> list[float]:
    """Psychological round-number levels around ``price`` at multiples of ``step``."""
    if step <= 0:
        raise ValueError("step must be positive")
    base = math.floor(price / step) * step
    levels = {round(base + i * step, 8) for i in range(-count, count + 1)}
    return sorted(levels)


def daily_pivot_points(df: pd.DataFrame) -> PivotPoints | None:
    """Classic PP/R1/S1/R2/S2 from the prior day's high/low/close.

    Returns ``None`` when there isn't a complete prior day in ``df`` yet.
    Whether to compute/use this at all is a config decision
    (``scalping.use_pivot_points``) made by the caller, not by this function.
    """
    levels = prior_day_week_levels(df)
    if levels.prior_day_high is None or levels.prior_day_low is None or levels.prior_day_close is None:
        return None

    high, low, close = levels.prior_day_high, levels.prior_day_low, levels.prior_day_close
    pp = (high + low + close) / 3
    r1 = 2 * pp - low
    s1 = 2 * pp - high
    r2 = pp + (high - low)
    s2 = pp - (high - low)
    return PivotPoints(
        pp=round(pp, 4), r1=round(r1, 4), s1=round(s1, 4), r2=round(r2, 4), s2=round(s2, 4)
    )


# ---------------------------------------------------------------------------
# Session classification
# ---------------------------------------------------------------------------

# Built into every classification regardless of config: gold trades through
# the Asian session too, but only london/ny need broker-verified overlap
# hours, so only those live in scalping.sessions_utc.
_DEFAULT_ASIAN_SESSION_UTC = (22, 7)  # wraps midnight


def _hour_in_window(hour: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= hour < end
    return hour >= start or hour < end  # wraps midnight


def session_window(timestamp_utc: datetime, sessions_utc: dict[str, Sequence[int]] | None = None) -> str:
    """Classify a UTC timestamp into asian/london/ny/london_ny_overlap/off_session.

    Windows are half-open ``[start, end)`` in UTC hours, so e.g. London
    (07-16) and NY (12-21) share exactly the 12:00-16:00 overlap with no
    double-counting at the boundaries. Any two active sessions are joined
    alphabetically as ``"{a}_{b}_overlap"``.
    """
    if timestamp_utc.tzinfo is not None:
        timestamp_utc = timestamp_utc.astimezone(timezone.utc).replace(tzinfo=None)
    hour = timestamp_utc.hour

    windows: dict[str, tuple[int, int]] = {"asian": _DEFAULT_ASIAN_SESSION_UTC}
    if sessions_utc:
        windows.update({name: (int(bounds[0]), int(bounds[1])) for name, bounds in sessions_utc.items()})

    active = sorted(name for name, (start, end) in windows.items() if _hour_in_window(hour, start, end))
    if not active:
        return "off_session"
    if len(active) == 1:
        return active[0]
    return "_".join(active) + "_overlap"


# ---------------------------------------------------------------------------
# Entry triggers: liquidity sweep, order blocks, FVGs, momentum
# ---------------------------------------------------------------------------


def liquidity_sweep(
    df: pd.DataFrame,
    swings: Sequence[Swing],
    wick_atr_ratio: float = 0.25,
    atr_period: int = 14,
) -> LiquiditySweepResult:
    """Detect a liquidity sweep on the latest bar.

    A bearish sweep: the wick pierces above the most recent swing high by
    >= ``wick_atr_ratio`` x ATR but the bar *closes* back below it
    (rejection -- the close, not the wick, confirms/denies the break). A
    bullish sweep mirrors this on the most recent swing low.
    """
    if df.empty:
        return LiquiditySweepResult(False, None, None, None, None)

    atr_series = _atr(df, atr_period)
    current_atr = atr_series.iloc[-1]
    if pd.isna(current_atr) or current_atr <= 0:
        return LiquiditySweepResult(False, None, None, None, None)

    last_idx = len(df) - 1
    bar = df.iloc[-1]
    prior_swings = [s for s in swings if s.index < last_idx]
    swing_highs = [s for s in prior_swings if s.kind == "high"]
    swing_lows = [s for s in prior_swings if s.kind == "low"]

    if swing_highs:
        level = swing_highs[-1].price
        wick = float(bar["high"]) - level
        if wick > 0 and (wick / current_atr) >= wick_atr_ratio and float(bar["close"]) < level:
            return LiquiditySweepResult(True, "bearish", level, round(wick / current_atr, 4), last_idx)

    if swing_lows:
        level = swing_lows[-1].price
        wick = level - float(bar["low"])
        if wick > 0 and (wick / current_atr) >= wick_atr_ratio and float(bar["close"]) > level:
            return LiquiditySweepResult(True, "bullish", level, round(wick / current_atr, 4), last_idx)

    return LiquiditySweepResult(False, None, None, None, last_idx)


def detect_order_blocks(
    df: pd.DataFrame,
    displacement_atr: float = 0.5,
    atr_period: int = 14,
    lookback: int | None = None,
) -> list[OrderBlock]:
    """Last opposite-color candle before each displacement move.

    A displacement move is a bar whose body (``|close - open|``) is >=
    ``displacement_atr`` x ATR. The order block is the nearest preceding
    candle of the opposite color -- e.g. the last down candle before an up
    displacement is a bullish order block (retest zone for longs).
    """
    if len(df) < atr_period + 2:
        return []

    atr_series = _atr(df, atr_period)
    n = len(df)
    start = 1 if lookback is None else max(1, n - lookback)

    blocks: list[OrderBlock] = []
    for i in range(start, n):
        atr_i = atr_series.iloc[i]
        if pd.isna(atr_i) or atr_i <= 0:
            continue
        body = float(df["close"].iloc[i] - df["open"].iloc[i])
        if abs(body) / atr_i < displacement_atr:
            continue
        displacement_dir = "bullish" if body > 0 else "bearish"
        opposite_dir = "bearish" if displacement_dir == "bullish" else "bullish"

        for j in range(i - 1, -1, -1):
            prev_body = float(df["close"].iloc[j] - df["open"].iloc[j])
            if prev_body == 0:
                continue
            prev_color = "bullish" if prev_body > 0 else "bearish"
            if prev_color == opposite_dir:
                blocks.append(
                    OrderBlock(
                        index=j,
                        time=df["time"].iloc[j] if "time" in df.columns else None,
                        direction=displacement_dir,
                        open=float(df["open"].iloc[j]),
                        high=float(df["high"].iloc[j]),
                        low=float(df["low"].iloc[j]),
                        close=float(df["close"].iloc[j]),
                    )
                )
                break
    return blocks


def detect_fair_value_gaps(df: pd.DataFrame) -> list[FairValueGap]:
    """3-candle imbalance detection: gap between bar i-1's wick and bar i+1's wick.

    Bullish FVG: ``low[i+1] > high[i-1]``. Bearish FVG: ``high[i+1] < low[i-1]``.
    ``filled`` is True if any later bar's range overlaps the gap zone.
    """
    gaps: list[FairValueGap] = []
    n = len(df)
    for i in range(1, n - 1):
        prev_high = float(df["high"].iloc[i - 1])
        prev_low = float(df["low"].iloc[i - 1])
        next_high = float(df["high"].iloc[i + 1])
        next_low = float(df["low"].iloc[i + 1])

        if next_low > prev_high:
            gap_low, gap_high, direction = prev_high, next_low, "bullish"
        elif next_high < prev_low:
            gap_low, gap_high, direction = next_high, prev_low, "bearish"
        else:
            continue

        filled = False
        for k in range(i + 2, n):
            if float(df["low"].iloc[k]) <= gap_high and float(df["high"].iloc[k]) >= gap_low:
                filled = True
                break

        gaps.append(
            FairValueGap(
                index=i,
                time=df["time"].iloc[i] if "time" in df.columns else None,
                direction=direction,
                gap_low=gap_low,
                gap_high=gap_high,
                filled=filled,
            )
        )
    return gaps


def rsi_stoch_momentum(
    df: pd.DataFrame,
    rsi_period: int = 14,
    stoch_period: int = 14,
    stoch_smooth: int = 3,
    ema_period: int = 21,
    oversold: float = 30.0,
    overbought: float = 70.0,
) -> MomentumSnapshot | None:
    """EMA-pullback + RSI/stochastic momentum-turn confirmation on the latest bar.

    ``momentum_turn`` is "bullish" when %K crosses above %D from
    near-oversold territory while RSI is turning up (mirrored for
    "bearish"); otherwise "none". Returns ``None`` when there isn't enough
    history for a stable read.
    """
    if len(df) < max(rsi_period, stoch_period, ema_period) + 2:
        return None

    close = df["close"]
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / rsi_period, adjust=False, min_periods=rsi_period).mean()
    avg_loss = loss.ewm(alpha=1 / rsi_period, adjust=False, min_periods=rsi_period).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi_series = (100 - (100 / (1 + rs))).fillna(100.0)

    lowest_low = df["low"].rolling(stoch_period).min()
    highest_high = df["high"].rolling(stoch_period).max()
    denom = (highest_high - lowest_low).replace(0, float("nan"))
    stoch_k_series = ((close - lowest_low) / denom * 100).fillna(50.0)
    stoch_d_series = stoch_k_series.rolling(stoch_smooth).mean()

    ema_series = close.ewm(span=ema_period, adjust=False).mean()

    rsi_now, rsi_prev = float(rsi_series.iloc[-1]), float(rsi_series.iloc[-2])
    k_now, k_prev = float(stoch_k_series.iloc[-1]), float(stoch_k_series.iloc[-2])
    d_now, d_prev = float(stoch_d_series.iloc[-1]), float(stoch_d_series.iloc[-2])
    ema_now = float(ema_series.iloc[-1])

    bullish_cross = k_prev <= d_prev and k_now > d_now and k_prev <= oversold + 10
    bearish_cross = k_prev >= d_prev and k_now < d_now and k_prev >= overbought - 10

    if bullish_cross and rsi_now > rsi_prev:
        momentum_turn = "bullish"
    elif bearish_cross and rsi_now < rsi_prev:
        momentum_turn = "bearish"
    else:
        momentum_turn = "none"

    bar = df.iloc[-1]
    price_at_ema = float(bar["low"]) <= ema_now <= float(bar["high"])

    return MomentumSnapshot(
        rsi=round(rsi_now, 2),
        stoch_k=round(k_now, 2),
        stoch_d=round(d_now, 2),
        ema=round(ema_now, 4),
        price_at_ema=bool(price_at_ema),
        momentum_turn=momentum_turn,
    )


def _zone_price(zone: object) -> float:
    if isinstance(zone, (int, float)):
        return float(zone)
    if isinstance(zone, dict) and "price" in zone:
        return float(zone["price"])
    price = getattr(zone, "price", None)
    if price is not None:
        return float(price)
    raise TypeError(f"cannot extract a price from key zone {zone!r}")


def confluence_distance(price: float, key_zones: Sequence[object], atr: float | None) -> float | None:
    """ATR-normalized distance from ``price`` to the nearest key zone.

    Accepts raw floats, dicts with a ``"price"`` key, or any object exposing
    a ``.price`` attribute (e.g. Phase 3's ``KeyZone`` schema) -- this
    function has zero coupling to that schema so Phase 1 doesn't need to
    wait on Phase 3. Returns ``None`` when there are no zones or ATR is
    missing/non-positive (distance is undefined, not zero).
    """
    if not key_zones or atr is None or atr <= 0:
        return None
    distances = [abs(price - _zone_price(z)) for z in key_zones]
    return round(min(distances) / atr, 4)
