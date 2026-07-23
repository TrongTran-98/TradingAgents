"""Tests for the gold-scalping walk-forward outcome resolver (Phase 6).

See docs/plans/gold-scalping-mt5/PLAN.md and TRACKING.md for the design and
checklist these tests verify against.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from tradingagents.agents.utils.scalp_schemas import (
    EntryTrigger,
    HTFBias,
    KeyZone,
    LTFStructure,
    ScalpSignal,
    SignalOutcome,
)
from tradingagents.dataflows import scalp_journal
from tradingagents.dataflows.errors import NoMarketDataError
from tradingagents.dataflows.scalp_journal import ScalpJournal, resolve_outcome

_GENERATED_AT = datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc)  # a Monday
_CONFIG = {"scalping": {"max_holding_bars_5m": 48, "mt5_server_utc_offset_hours": 0}}


def _key_zone(price: float = 2400.0) -> KeyZone:
    return KeyZone(price=price, label="prior day high", source_timeframe="1D")


def _signal(direction: str = "long", **overrides) -> ScalpSignal:
    if direction == "long":
        entry, stop, tp1 = 2400.0, 2396.0, 2410.0
    else:
        entry, stop, tp1 = 2400.0, 2404.0, 2390.0

    fields = dict(
        symbol="XAUUSD",
        generated_at_utc=_GENERATED_AT,
        htf_bias=HTFBias(
            bias="bullish" if direction == "long" else "bearish",
            confidence="high",
            key_zones=[_key_zone()],
            rationale="Structure/EMA/ATR agree.",
            invalidation_note="A close through the opposite level would flip this.",
        ),
        ltf_structure=LTFStructure(
            event="BOS",
            event_price=2402.0,
            displacement_atr=0.4,
            session="london_ny_overlap",
            volatility_regime="normal",
            agrees_with_htf=True,
            tradeable=True,
            rationale="15m BOS agrees with HTF bias during overlap session.",
        ),
        entry_trigger=EntryTrigger(
            trigger_type="order_block_retest",
            triggered=True,
            confluence_zone=_key_zone(),
            entry_price=entry,
            stop_loss=stop,
            take_profit_1=tp1,
            risk_reward_1=2.5,
            passed_min_rr=True,
            passed_max_sl=True,
            rationale="Retest confluent with prior day high.",
        ),
    )
    fields.update(overrides)
    return ScalpSignal(**fields)


def _bars(*rows: tuple) -> pd.DataFrame:
    """rows of (time, open, high, low, close)."""
    return pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])


def _cadence_times(n: int, start: datetime = _GENERATED_AT, minutes: int = 5) -> list[datetime]:
    naive_start = start.replace(tzinfo=None)
    return [naive_start + timedelta(minutes=minutes * (i + 1)) for i in range(n)]


@pytest.mark.unit
class TestCleanHits:
    def test_long_clean_sl_hit(self):
        times = _cadence_times(3)
        bars = _bars(
            (times[0], 2400.0, 2401.0, 2399.0, 2400.5),
            (times[1], 2400.5, 2401.0, 2400.0, 2400.5),
            (times[2], 2400.5, 2400.5, 2395.0, 2395.5),  # low <= stop_loss (2396.0)
        )
        outcome = resolve_outcome(_signal("long"), config=_CONFIG, bars=bars)
        assert outcome.status == "loss"
        assert outcome.exit_price == 2396.0
        assert outcome.bars_held == 3
        assert outcome.gap_through is False

    def test_short_clean_sl_hit(self):
        times = _cadence_times(2)
        bars = _bars(
            (times[0], 2400.0, 2401.0, 2399.0, 2400.5),
            (times[1], 2400.5, 2405.0, 2400.0, 2404.5),  # high >= stop_loss (2404.0)
        )
        outcome = resolve_outcome(_signal("short"), config=_CONFIG, bars=bars)
        assert outcome.status == "loss"
        assert outcome.exit_price == 2404.0
        assert outcome.gap_through is False

    def test_long_clean_tp_hit(self):
        times = _cadence_times(2)
        bars = _bars(
            (times[0], 2400.0, 2405.0, 2399.0, 2404.5),
            (times[1], 2404.5, 2411.0, 2404.0, 2410.5),  # high >= take_profit_1 (2410.0)
        )
        outcome = resolve_outcome(_signal("long"), config=_CONFIG, bars=bars)
        assert outcome.status == "win"
        assert outcome.exit_price == 2410.0
        assert outcome.bars_held == 2
        assert outcome.gap_through is False

    def test_short_clean_tp_hit(self):
        times = _cadence_times(1)
        bars = _bars(
            (times[0], 2400.0, 2401.0, 2389.0, 2390.5),  # low <= take_profit_1 (2390.0)
        )
        outcome = resolve_outcome(_signal("short"), config=_CONFIG, bars=bars)
        assert outcome.status == "win"
        assert outcome.exit_price == 2390.0
        assert outcome.bars_held == 1


@pytest.mark.unit
class TestSameBarBothHit:
    def test_long_ordinary_intrabar_spike_resolves_as_sl_not_gap(self):
        times = _cadence_times(2)
        bars = _bars(
            (times[0], 2400.0, 2401.0, 2399.0, 2400.5),
            (times[1], 2400.0, 2412.0, 2394.0, 2401.0),  # both SL (<=2396) and TP (>=2410) touched
        )
        outcome = resolve_outcome(_signal("long"), config=_CONFIG, bars=bars)
        assert outcome.status == "loss"
        assert outcome.exit_price == 2396.0
        assert outcome.gap_through is False

    def test_short_ordinary_intrabar_spike_resolves_as_sl(self):
        times = _cadence_times(1)
        bars = _bars(
            (times[0], 2400.0, 2406.0, 2388.0, 2395.0),  # both SL (>=2404) and TP (<=2390) touched
        )
        outcome = resolve_outcome(_signal("short"), config=_CONFIG, bars=bars)
        assert outcome.status == "loss"
        assert outcome.exit_price == 2404.0
        assert outcome.gap_through is False

    def test_long_gap_through_reopen_tagged_distinctly(self):
        # Weekend reopen: first forward bar arrives ~2 days later, already gapped past both levels.
        gap_time = _GENERATED_AT.replace(tzinfo=None) + timedelta(days=2)
        bars = _bars(
            (gap_time, 2390.0, 2413.0, 2389.0, 2412.0),
        )
        outcome = resolve_outcome(_signal("long"), config=_CONFIG, bars=bars)
        assert outcome.status == "loss"
        assert outcome.gap_through is True

    def test_short_gap_through_reopen_tagged_distinctly(self):
        gap_time = _GENERATED_AT.replace(tzinfo=None) + timedelta(days=2)
        bars = _bars(
            (gap_time, 2410.0, 2411.0, 2387.0, 2388.0),
        )
        outcome = resolve_outcome(_signal("short"), config=_CONFIG, bars=bars)
        assert outcome.status == "loss"
        assert outcome.gap_through is True


@pytest.mark.unit
class TestTimeoutAndPending:
    def test_timeout_when_window_scanned_with_no_hit(self):
        times = _cadence_times(48)
        rows = [(t, 2400.0, 2401.0, 2399.0, 2400.5) for t in times]  # never touches SL/TP
        outcome = resolve_outcome(_signal("long"), config=_CONFIG, bars=_bars(*rows))
        assert outcome.status == "timeout"
        assert outcome.bars_held == 48

    def test_timeout_when_now_past_window_even_with_fewer_bars(self):
        # Weekend/holiday shrank the available bar count, but wall-clock time has moved on.
        times = _cadence_times(10)
        rows = [(t, 2400.0, 2401.0, 2399.0, 2400.5) for t in times]
        now = _GENERATED_AT.replace(tzinfo=None) + timedelta(days=3)
        outcome = resolve_outcome(_signal("long"), config=_CONFIG, bars=_bars(*rows), now=now)
        assert outcome.status == "timeout"
        assert outcome.bars_held == 10

    def test_pending_when_no_bars_and_window_not_elapsed(self):
        bars = _bars()
        now = _GENERATED_AT.replace(tzinfo=None) + timedelta(minutes=10)
        outcome = resolve_outcome(_signal("long"), config=_CONFIG, bars=bars, now=now)
        assert outcome.status == "pending"
        assert outcome.bars_held == 0

    def test_pending_when_partial_bars_and_window_not_elapsed(self):
        times = _cadence_times(5)
        rows = [(t, 2400.0, 2401.0, 2399.0, 2400.5) for t in times]
        now = _GENERATED_AT.replace(tzinfo=None) + timedelta(minutes=30)
        outcome = resolve_outcome(_signal("long"), config=_CONFIG, bars=_bars(*rows), now=now)
        assert outcome.status == "pending"
        assert outcome.bars_held == 5

    def test_no_market_data_error_resolves_to_pending(self, monkeypatch):
        def _raise(*args, **kwargs):
            raise NoMarketDataError("XAUUSD", detail="no bars yet")

        monkeypatch.setattr(scalp_journal.mt5_vendor, "get_mt5_rates_range", _raise)
        outcome = resolve_outcome(_signal("long"), config=_CONFIG)
        assert outcome.status == "pending"


@pytest.mark.unit
class TestResolveOutcomeContract:
    def test_raises_when_not_triggered(self):
        signal = _signal("long", entry_trigger=EntryTrigger(
            trigger_type="none", triggered=False, rationale="No valid trigger."
        ))
        with pytest.raises(ValueError, match="triggered"):
            resolve_outcome(signal, config=_CONFIG, bars=_bars())

    def test_raises_when_prices_missing(self):
        signal = _signal("long")
        signal = signal.model_copy(
            update={"entry_trigger": signal.entry_trigger.model_copy(update={"stop_loss": None})}
        )
        with pytest.raises(ValueError, match="entry_price/stop_loss/take_profit_1"):
            resolve_outcome(signal, config=_CONFIG, bars=_bars())


@pytest.mark.unit
class TestJournalOutcomePersistence:
    def _journal(self, tmp_path):
        return ScalpJournal({"scalping": {"journal_path": str(tmp_path / "journal.jsonl")}})

    def test_update_outcome_persists_and_round_trips(self, tmp_path):
        journal = self._journal(tmp_path)
        signal = _signal("long", signal_id="sig-1")
        journal.append(signal)

        outcome = SignalOutcome(status="win", exit_price=2410.0, bars_held=5, resolved_at_utc=datetime.now(timezone.utc))
        assert journal.update_outcome("sig-1", outcome) is True

        [loaded] = journal.read_signals()
        assert loaded.outcome.status == "win"
        assert loaded.outcome.exit_price == 2410.0
        assert loaded.outcome.bars_held == 5

    def test_update_outcome_returns_false_for_unknown_signal_id(self, tmp_path):
        journal = self._journal(tmp_path)
        journal.append(_signal("long", signal_id="sig-1"))
        assert journal.update_outcome("does-not-exist", SignalOutcome(status="timeout")) is False

    def test_pending_signals_excludes_untriggered_and_resolved(self, tmp_path):
        journal = self._journal(tmp_path)
        untriggered = _signal("long", signal_id="sig-untriggered", entry_trigger=EntryTrigger(
            trigger_type="none", triggered=False, rationale="No valid trigger."
        ))
        pending = _signal("long", signal_id="sig-pending")
        resolved = _signal("long", signal_id="sig-resolved")
        journal.append(untriggered)
        journal.append(pending)
        journal.append(resolved)
        journal.update_outcome("sig-resolved", SignalOutcome(status="win", exit_price=2410.0))

        ids = [s.signal_id for s in journal.pending_signals()]
        assert ids == ["sig-pending"]
