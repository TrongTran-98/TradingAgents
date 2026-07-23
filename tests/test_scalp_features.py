"""Tests for the deterministic gold-scalping feature functions (Phase 1).

See docs/plans/gold-scalping-mt5/PLAN.md and TRACKING.md for the design and
checklist these tests verify against.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd
import pytest

import tradingagents.dataflows.scalp_features as sf


def _flat_df(n: int, high=101.0, low=99.0, close=100.0, open_=100.0, start="2026-01-01") -> pd.DataFrame:
    """n bars with a constant true range of 2 -> ATR converges to exactly 2.0."""
    times = pd.date_range(start, periods=n, freq="15min")
    return pd.DataFrame({
        "time": times,
        "open": [open_] * n,
        "high": [high] * n,
        "low": [low] * n,
        "close": [close] * n,
    })


# ---------------------------------------------------------------------------
# detect_swings
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDetectSwings:
    def test_known_peaks_and_troughs(self):
        prices = [10, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10, 11, 12, 13, 14, 13, 12]
        times = pd.date_range("2026-01-01", periods=len(prices), freq="15min")
        df = pd.DataFrame({
            "time": times, "open": prices, "high": prices, "low": prices, "close": prices,
        })
        swings = sf.detect_swings(df, n=2)
        found = {(s.index, s.kind, s.price) for s in swings}
        assert (3, "high", 13.0) in found
        assert (8, "low", 8.0) in found
        assert (14, "high", 14.0) in found

    def test_rejects_n_less_than_one(self):
        df = _flat_df(5)
        with pytest.raises(ValueError):
            sf.detect_swings(df, n=0)

    def test_flat_series_has_no_swings(self):
        df = _flat_df(10)
        assert sf.detect_swings(df, n=2) == []


# ---------------------------------------------------------------------------
# classify_structure
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestClassifyStructure:
    def test_higher_highs_and_higher_lows_is_bullish(self):
        swings = [
            sf.Swing(1, None, 100.0, "high"),
            sf.Swing(2, None, 95.0, "low"),
            sf.Swing(4, None, 105.0, "high"),
            sf.Swing(5, None, 98.0, "low"),
        ]
        assert sf.classify_structure(swings) == "bullish"

    def test_lower_highs_and_lower_lows_is_bearish(self):
        swings = [
            sf.Swing(1, None, 105.0, "high"),
            sf.Swing(2, None, 98.0, "low"),
            sf.Swing(4, None, 100.0, "high"),
            sf.Swing(5, None, 95.0, "low"),
        ]
        assert sf.classify_structure(swings) == "bearish"

    def test_mixed_is_range(self):
        swings = [
            sf.Swing(1, None, 100.0, "high"),
            sf.Swing(2, None, 95.0, "low"),
            sf.Swing(4, None, 105.0, "high"),
            sf.Swing(5, None, 90.0, "low"),  # lower low, higher high -> mixed
        ]
        assert sf.classify_structure(swings) == "range"

    def test_insufficient_swings_is_range(self):
        swings = [sf.Swing(1, None, 100.0, "high"), sf.Swing(2, None, 95.0, "low")]
        assert sf.classify_structure(swings) == "range"


# ---------------------------------------------------------------------------
# detect_bos_choch
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDetectBosChoch:
    def _bullish_trend_df(self, last_close: float, last_high: float | None = None):
        # 8 base bars (constant TR=2 -> ATR=2 once warmed up), swings define a
        # bullish trend (higher high 104 > 102, higher low 100 > 98), then a
        # final bar whose close is the parameter under test.
        df = _flat_df(9)
        if last_high is not None:
            df.loc[8, "high"] = last_high
        df.loc[8, "close"] = last_close
        swings = [
            sf.Swing(1, None, 102.0, "high"),
            sf.Swing(2, None, 98.0, "low"),
            sf.Swing(4, None, 104.0, "high"),
            sf.Swing(5, None, 100.0, "low"),
        ]
        return df, swings

    def test_bos_continuation_above_threshold(self):
        # displacement = (104.6 - 104) / atr(2) = 0.3 >= 0.25
        df, swings = self._bullish_trend_df(last_close=104.6)
        event = sf.detect_bos_choch(df, swings, min_displacement_atr=0.25, atr_period=3)
        assert event.kind == "BOS"
        assert event.direction == "bullish"
        assert event.broken_level == 104.0

    def test_just_under_threshold_is_none(self):
        # displacement = (104.3 - 104) / 2 = 0.15 < 0.25
        df, swings = self._bullish_trend_df(last_close=104.3)
        event = sf.detect_bos_choch(df, swings, min_displacement_atr=0.25, atr_period=3)
        assert event.kind == "none"

    def test_just_over_threshold_triggers(self):
        # displacement = (104.51 - 104) / 2 = 0.255 >= 0.25
        df, swings = self._bullish_trend_df(last_close=104.51)
        event = sf.detect_bos_choch(df, swings, min_displacement_atr=0.25, atr_period=3)
        assert event.kind == "BOS"

    def test_choch_break_of_opposite_swing_in_bullish_trend(self):
        # close breaks below the swing low (100) by displacement 0.3 while
        # trend is bullish -> reversal signal, not continuation.
        df, swings = self._bullish_trend_df(last_close=99.4)
        event = sf.detect_bos_choch(df, swings, min_displacement_atr=0.25, atr_period=3)
        assert event.kind == "CHoCH"
        assert event.direction == "bearish"
        assert event.broken_level == 100.0

    def test_wick_only_pierce_does_not_count(self):
        # huge wick above the swing high, but close stays well below it.
        df, swings = self._bullish_trend_df(last_close=103.0, last_high=110.0)
        event = sf.detect_bos_choch(df, swings, min_displacement_atr=0.25, atr_period=3)
        assert event.kind == "none"

    def test_empty_df_returns_none_event(self):
        event = sf.detect_bos_choch(pd.DataFrame(), [], min_displacement_atr=0.25)
        assert event.kind == "none"


# ---------------------------------------------------------------------------
# liquidity_sweep
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestLiquiditySweep:
    """ATR is patched to a fixed 2.0: the wick added to the final bar to
    trigger a sweep would otherwise inflate that same bar's own true range
    and (since ATR is causal/inclusive of the current bar) contaminate its
    own ATR reading -- the sweep-detection logic is what's under test here,
    not ATR's smoothing behavior (covered elsewhere)."""

    def test_bearish_sweep_detected_on_close_rejection(self, monkeypatch):
        df = _flat_df(9)
        swings = [sf.Swing(4, None, 104.0, "high")]
        df.loc[8, "high"] = 104.6  # wick 0.6 / atr(2) = 0.3 >= 0.25
        df.loc[8, "close"] = 103.0  # closes back below the swept level
        monkeypatch.setattr(sf, "_atr", lambda df, period: pd.Series([2.0] * len(df)))
        result = sf.liquidity_sweep(df, swings, wick_atr_ratio=0.25, atr_period=3)
        assert result.detected is True
        assert result.direction == "bearish"
        assert result.swept_level == 104.0

    def test_bullish_sweep_detected_on_close_rejection(self, monkeypatch):
        df = _flat_df(9)
        swings = [sf.Swing(4, None, 96.0, "low")]
        df.loc[8, "low"] = 95.4  # wick 0.6 / atr(2) = 0.3 >= 0.25
        df.loc[8, "close"] = 97.0  # closes back above the swept level
        monkeypatch.setattr(sf, "_atr", lambda df, period: pd.Series([2.0] * len(df)))
        result = sf.liquidity_sweep(df, swings, wick_atr_ratio=0.25, atr_period=3)
        assert result.detected is True
        assert result.direction == "bullish"
        assert result.swept_level == 96.0

    def test_no_sweep_when_close_confirms_the_break(self, monkeypatch):
        df = _flat_df(9)
        swings = [sf.Swing(4, None, 104.0, "high")]
        df.loc[8, "high"] = 104.6
        df.loc[8, "close"] = 104.6  # closes beyond -- a real break, not a sweep
        monkeypatch.setattr(sf, "_atr", lambda df, period: pd.Series([2.0] * len(df)))
        result = sf.liquidity_sweep(df, swings, wick_atr_ratio=0.25, atr_period=3)
        assert result.detected is False


# ---------------------------------------------------------------------------
# detect_order_blocks
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDetectOrderBlocks:
    def test_finds_last_opposite_candle_before_bullish_displacement(self):
        df = _flat_df(6)  # indices 0-5, constant TR=2 -> ATR ~2 once warmed up
        # index 6: bearish candle (the order block candidate)
        # index 7: bullish displacement candle, body=3 >> 0.5*atr
        extra = pd.DataFrame({
            "time": pd.date_range("2026-01-01 02:00", periods=2, freq="15min"),
            "open": [100.0, 99.0],
            "high": [100.0, 102.0],
            "low": [98.0, 99.0],
            "close": [99.0, 102.0],
        })
        df = pd.concat([df, extra], ignore_index=True)
        blocks = sf.detect_order_blocks(df, displacement_atr=0.5, atr_period=3)
        assert len(blocks) == 1
        assert blocks[0].index == 6
        assert blocks[0].direction == "bullish"

    def test_no_blocks_when_no_displacement(self):
        df = _flat_df(20)
        assert sf.detect_order_blocks(df, displacement_atr=0.5, atr_period=3) == []


# ---------------------------------------------------------------------------
# detect_fair_value_gaps
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestDetectFairValueGaps:
    def test_bullish_gap_detected_and_marked_filled(self):
        rows = [
            {"open": 100, "high": 101, "low": 99, "close": 100.5},   # i-1 = 0
            {"open": 101, "high": 102, "low": 100.5, "close": 101.8},  # i = 1
            {"open": 103, "high": 105, "low": 103, "close": 104},    # i+1 = 2 (low 103 > high(0)=101 -> gap)
            {"open": 104, "high": 104.5, "low": 101.5, "close": 102},  # dips back into [101, 103] -> filled
        ]
        times = pd.date_range("2026-01-01", periods=len(rows), freq="15min")
        df = pd.DataFrame(rows)
        df.insert(0, "time", times)
        gaps = sf.detect_fair_value_gaps(df)
        assert len(gaps) == 1
        gap = gaps[0]
        assert gap.direction == "bullish"
        assert gap.gap_low == 101 and gap.gap_high == 103
        assert gap.filled is True

    def test_gap_not_revisited_is_unfilled(self):
        rows = [
            {"open": 100, "high": 101, "low": 99, "close": 100.5},
            {"open": 101, "high": 102, "low": 100.5, "close": 101.8},
            {"open": 103, "high": 105, "low": 103, "close": 104},
            {"open": 106, "high": 108, "low": 105.5, "close": 107},  # never dips back below 103
        ]
        times = pd.date_range("2026-01-01", periods=len(rows), freq="15min")
        df = pd.DataFrame(rows)
        df.insert(0, "time", times)
        gaps = sf.detect_fair_value_gaps(df)
        gap = next(g for g in gaps if g.index == 1)
        assert gap.gap_low == 101 and gap.gap_high == 103
        assert gap.filled is False

    def test_no_gap_when_ranges_overlap(self):
        df = _flat_df(5)
        assert sf.detect_fair_value_gaps(df) == []


# ---------------------------------------------------------------------------
# session_window
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestSessionWindow:
    SESSIONS = {"london": [7, 16], "ny": [12, 21]}

    @pytest.mark.parametrize("hour,expected", [
        (6, "asian"),
        (7, "london"),           # london opens, asian just closed
        (11, "london"),
        (12, "london_ny_overlap"),  # ny opens while london still active
        (15, "london_ny_overlap"),
        (16, "ny"),               # london closes (exclusive), ny continues
        (20, "ny"),
        (21, "off_session"),      # ny closes (exclusive), asian not yet open
        (22, "asian"),
        (0, "asian"),
    ])
    def test_boundary_edges(self, hour, expected):
        ts = datetime(2026, 1, 5, hour, 0, tzinfo=timezone.utc)
        assert sf.session_window(ts, self.SESSIONS) == expected

    def test_naive_timestamp_treated_as_utc(self):
        ts = datetime(2026, 1, 5, 12, 0)
        assert sf.session_window(ts, self.SESSIONS) == "london_ny_overlap"

    def test_no_sessions_config_still_classifies_asian(self):
        ts = datetime(2026, 1, 5, 23, 0, tzinfo=timezone.utc)
        assert sf.session_window(ts, None) == "asian"


# ---------------------------------------------------------------------------
# ema_stack_alignment ("compressed" threshold)
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestEmaStackAlignment:
    def test_flat_series_is_compressed(self):
        df = _flat_df(20)  # fast == slow == 100, ATR = 2
        result = sf.ema_stack_alignment(
            df, fast=5, slow=10, slope_lookback=2, compressed_atr_multiple=0.5, atr_period=3,
        )
        assert result is not None
        assert result.alignment == "compressed"

    def test_strong_uptrend_is_bullish(self):
        n = 20
        closes = [100 + i * 3 for i in range(n)]  # steep ramp separates the EMAs
        times = pd.date_range("2026-01-01", periods=n, freq="15min")
        df = pd.DataFrame({
            "time": times,
            "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes], "close": closes,
        })
        result = sf.ema_stack_alignment(
            df, fast=5, slow=10, slope_lookback=2, compressed_atr_multiple=0.5, atr_period=3,
        )
        assert result is not None
        assert result.alignment == "bullish"
        assert result.fast_slope > 0

    def test_strong_downtrend_is_bearish(self):
        n = 20
        closes = [200 - i * 3 for i in range(n)]
        times = pd.date_range("2026-01-01", periods=n, freq="15min")
        df = pd.DataFrame({
            "time": times,
            "open": closes, "high": [c + 1 for c in closes], "low": [c - 1 for c in closes], "close": closes,
        })
        result = sf.ema_stack_alignment(
            df, fast=5, slow=10, slope_lookback=2, compressed_atr_multiple=0.5, atr_period=3,
        )
        assert result is not None
        assert result.alignment == "bearish"
        assert result.fast_slope < 0

    def test_returns_none_when_insufficient_history(self):
        df = _flat_df(5)
        assert sf.ema_stack_alignment(df, fast=5, slow=10) is None


# ---------------------------------------------------------------------------
# atr_regime
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestAtrRegime:
    """Percentile-classification logic tested against a controlled ATR series
    (monkeypatched) so it's independent of the EMA-smoothing internals of
    ``_atr``, which are exercised elsewhere (e.g. TestEmaStackAlignment)."""

    def test_current_at_top_of_history_is_high(self, monkeypatch):
        series = pd.Series([1.0, 2, 3, 4, 5, 6, 7, 8, 9, 10])  # current (last) is the max
        monkeypatch.setattr(sf, "_atr", lambda df, period: series)
        result = sf.atr_regime(pd.DataFrame({"close": range(10)}), period=3, lookback=10)
        assert result is not None
        assert result.regime == "high"
        assert result.percentile == 1.0

    def test_current_at_bottom_of_history_is_low(self, monkeypatch):
        series = pd.Series([10.0, 9, 8, 7, 6, 5, 4, 3, 2, 1])  # current (last) is the min
        monkeypatch.setattr(sf, "_atr", lambda df, period: series)
        result = sf.atr_regime(pd.DataFrame({"close": range(10)}), period=3, lookback=10)
        assert result is not None
        assert result.regime == "low"

    def test_current_in_middle_of_history_is_normal(self, monkeypatch):
        series = pd.Series([10.0, 9, 8, 7, 6, 5, 4, 3, 2, 5])  # current (last)=5 -> pct 0.5
        monkeypatch.setattr(sf, "_atr", lambda df, period: series)
        result = sf.atr_regime(pd.DataFrame({"close": range(10)}), period=3, lookback=10)
        assert result is not None
        assert result.regime == "normal"
        assert result.percentile == 0.5

    def test_returns_none_when_no_data(self):
        assert sf.atr_regime(pd.DataFrame({"open": [], "high": [], "low": [], "close": []})) is None


# ---------------------------------------------------------------------------
# prior_day_week_levels / daily_pivot_points
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestPriorLevels:
    def _two_day_df(self) -> pd.DataFrame:
        day1 = pd.date_range("2026-01-05 00:00", periods=4, freq="6h")   # Mon
        day2 = pd.date_range("2026-01-06 00:00", periods=4, freq="6h")   # Tue (current, in-progress)
        times = list(day1) + list(day2)
        highs = [101, 103, 102, 104] + [105, 106, 105.5, 107]
        lows = [99, 100, 99.5, 101] + [102, 103, 102.5, 104]
        closes = [100, 102, 101, 103] + [104, 105, 104.5, 106]
        opens = [99.5, 101, 100.5, 102] + [103, 104, 103.5, 105]
        return pd.DataFrame({"time": times, "open": opens, "high": highs, "low": lows, "close": closes})

    def test_prior_day_levels(self):
        df = self._two_day_df()
        levels = sf.prior_day_week_levels(df)
        assert levels.prior_day_high == 104
        assert levels.prior_day_low == 99
        assert levels.prior_day_close == 103

    def test_no_prior_day_returns_none_fields(self):
        df = self._two_day_df()
        single_day = df.iloc[:4]
        levels = sf.prior_day_week_levels(single_day)
        assert levels.prior_day_high is None
        assert levels.prior_day_low is None
        assert levels.prior_day_close is None

    def test_daily_pivot_points_from_prior_day(self):
        df = self._two_day_df()
        pivots = sf.daily_pivot_points(df)
        assert pivots is not None
        high, low, close = 104, 99, 103
        expected_pp = (high + low + close) / 3
        assert pivots.pp == round(expected_pp, 4)
        assert pivots.r1 == round(2 * expected_pp - low, 4)
        assert pivots.s1 == round(2 * expected_pp - high, 4)

    def test_daily_pivot_points_none_without_prior_day(self):
        df = self._two_day_df().iloc[:4]
        assert sf.daily_pivot_points(df) is None


# ---------------------------------------------------------------------------
# round_number_levels
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRoundNumberLevels:
    def test_levels_around_price(self):
        levels = sf.round_number_levels(2413.7, step=5.0, count=2)
        assert levels == [2400.0, 2405.0, 2410.0, 2415.0, 2420.0]

    def test_rejects_non_positive_step(self):
        with pytest.raises(ValueError):
            sf.round_number_levels(2400.0, step=0)


# ---------------------------------------------------------------------------
# confluence_distance
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestConfluenceDistance:
    def test_nearest_zone_in_atr_units(self):
        zones = [2400.0, 2410.0, {"price": 2415.0}]
        dist = sf.confluence_distance(2411.0, zones, atr=2.0)
        assert dist == 0.5  # nearest zone is 2410, |2411-2410|/2 = 0.5

    def test_none_when_no_zones(self):
        assert sf.confluence_distance(2400.0, [], atr=2.0) is None

    def test_none_when_atr_invalid(self):
        assert sf.confluence_distance(2400.0, [2400.0], atr=0) is None
        assert sf.confluence_distance(2400.0, [2400.0], atr=None) is None


# ---------------------------------------------------------------------------
# rsi_stoch_momentum
# ---------------------------------------------------------------------------

@pytest.mark.unit
class TestRsiStochMomentum:
    def test_returns_none_when_insufficient_history(self):
        df = _flat_df(5)
        assert sf.rsi_stoch_momentum(df, rsi_period=14, stoch_period=14, ema_period=21) is None

    def test_output_within_expected_bounds(self):
        n = 40
        closes = [100 + (i % 5) - 2 for i in range(n)]
        times = pd.date_range("2026-01-01", periods=n, freq="5min")
        df = pd.DataFrame({
            "time": times,
            "open": closes, "high": [c + 0.5 for c in closes], "low": [c - 0.5 for c in closes], "close": closes,
        })
        snap = sf.rsi_stoch_momentum(df, rsi_period=5, stoch_period=5, stoch_smooth=3, ema_period=5)
        assert snap is not None
        assert 0 <= snap.rsi <= 100
        assert 0 <= snap.stoch_k <= 100
        assert 0 <= snap.stoch_d <= 100
        assert snap.momentum_turn in {"bullish", "bearish", "none"}
