"""Tests for scalp_tools.py's deterministic snapshot tools and verification helpers (Phase 4).

Mocks ``tradingagents.dataflows.mt5_vendor.get_mt5_rates_range`` directly
(the only MT5 touchpoint scalp_tools.py has) rather than faking the
MetaTrader5 module -- see test_mt5_vendor.py for the lower-level fetch
tests, and test_scalp_features.py for scalp_features.py's own unit tests.
"""

from __future__ import annotations

import pandas as pd
import pytest

import tradingagents.agents.utils.scalp_tools as scalp_tools
from tradingagents.dataflows.errors import NoMarketDataError


def _trending_df(n: int, start_price: float = 2300.0, step: float = 1.0, freq: str = "15min") -> pd.DataFrame:
    times = pd.date_range("2026-01-01", periods=n, freq=freq)
    closes = [start_price + i * step for i in range(n)]
    return pd.DataFrame({
        "time": times,
        "open": closes,
        "high": [c + 0.5 for c in closes],
        "low": [c - 0.5 for c in closes],
        "close": closes,
    })


@pytest.mark.unit
class TestCandidateKeyZones:
    def test_includes_round_numbers_and_prior_levels(self):
        df = _trending_df(400, freq="1h")  # >2 weeks of 1H bars -> prior day+week populated
        zones = scalp_tools._candidate_key_zones(df, {"use_pivot_points": True})
        labels = {z["label"] for z in zones}
        assert "prior day high" in labels
        assert "round number" in labels
        assert "pivot PP" in labels

    def test_pivot_points_excluded_when_disabled(self):
        df = _trending_df(400, freq="1h")
        zones = scalp_tools._candidate_key_zones(df, {"use_pivot_points": False})
        assert all("pivot" not in z["label"] for z in zones)


@pytest.mark.unit
class TestSnapshotToolErrorHandling:
    def _raise(self, *args, **kwargs):
        raise NoMarketDataError("XAUUSD", detail="no bars")

    def test_get_htf_snapshot_reports_vendor_error(self, monkeypatch):
        monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", self._raise)
        result = scalp_tools.get_htf_snapshot.func("XAUUSD", "2026-01-01T00:00:00")
        assert "<error" in result

    def test_get_ltf_snapshot_reports_vendor_error(self, monkeypatch):
        monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", self._raise)
        result = scalp_tools.get_ltf_snapshot.func("XAUUSD", "2026-01-01T00:00:00")
        assert "<error" in result

    def test_get_entry_snapshot_reports_vendor_error(self, monkeypatch):
        monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", self._raise)
        result = scalp_tools.get_entry_snapshot.func("XAUUSD", "2026-01-01T00:00:00")
        assert "<error" in result


@pytest.mark.unit
class TestSnapshotToolsHappyPath:
    def test_get_htf_snapshot_returns_readable_text(self, monkeypatch):
        df = _trending_df(300, freq="1h")
        monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", lambda *a, **k: df.copy())
        result = scalp_tools.get_htf_snapshot.func("XAUUSD", "2026-02-01T00:00:00")
        assert "HTF snapshot" in result
        assert "Structure" in result

    def test_get_ltf_snapshot_returns_readable_text(self, monkeypatch):
        df = _trending_df(250, freq="15min")
        monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", lambda *a, **k: df.copy())
        result = scalp_tools.get_ltf_snapshot.func("XAUUSD", "2026-02-01T12:00:00")
        assert "LTF (15m) snapshot" in result

    def test_get_entry_snapshot_returns_readable_text(self, monkeypatch):
        df_5m = _trending_df(350, freq="5min")
        df_1h = _trending_df(250, freq="1h")

        def _fake_range(symbol, timeframe, date_from, date_to, utc_offset_hours=0.0):
            return df_1h.copy() if timeframe == "1H" else df_5m.copy()

        monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", _fake_range)
        result = scalp_tools.get_entry_snapshot.func("XAUUSD", "2026-02-01T12:00:00")
        assert "Entry (5m) snapshot" in result


@pytest.mark.unit
class TestComputeLtfAlignment:
    """Isolates the comparison logic from classify_structure's own correctness
    (already covered exhaustively by test_scalp_features.py)."""

    def _patch(self, monkeypatch, structure_label: str):
        monkeypatch.setattr(
            scalp_tools.mt5_vendor, "get_mt5_rates_range",
            lambda *a, **k: _trending_df(60, freq="15min"),
        )
        monkeypatch.setattr(scalp_tools.sf, "classify_structure", lambda swings: structure_label)

    def test_range_bias_never_agrees(self, monkeypatch):
        self._patch(monkeypatch, "bullish")
        assert scalp_tools.compute_ltf_alignment("XAUUSD", "2026-02-01T00:00:00", "range") is False

    def test_matching_direction_agrees(self, monkeypatch):
        self._patch(monkeypatch, "bullish")
        assert scalp_tools.compute_ltf_alignment("XAUUSD", "2026-02-01T00:00:00", "bullish") is True

    def test_mismatched_direction_disagrees(self, monkeypatch):
        self._patch(monkeypatch, "bearish")
        assert scalp_tools.compute_ltf_alignment("XAUUSD", "2026-02-01T00:00:00", "bullish") is False

    def test_vendor_error_returns_false(self, monkeypatch):
        def _raise(*a, **k):
            raise NoMarketDataError("XAUUSD", detail="no bars")
        monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", _raise)
        assert scalp_tools.compute_ltf_alignment("XAUUSD", "2026-02-01T00:00:00", "bullish") is False


@pytest.mark.unit
class TestGetEntryAtr:
    def test_returns_atr_value(self, monkeypatch):
        df = _trending_df(300, freq="5min")
        monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", lambda *a, **k: df.copy())
        atr = scalp_tools.get_entry_atr("XAUUSD", "2026-02-01T00:00:00")
        assert atr is not None
        assert atr > 0

    def test_vendor_error_returns_none(self, monkeypatch):
        def _raise(*a, **k):
            raise NoMarketDataError("XAUUSD", detail="no bars")
        monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", _raise)
        assert scalp_tools.get_entry_atr("XAUUSD", "2026-02-01T00:00:00") is None
