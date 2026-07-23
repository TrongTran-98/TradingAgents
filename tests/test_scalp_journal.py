"""Tests for the gold-scalping journal write path (Phase 5).

See docs/plans/gold-scalping-mt5/PLAN.md and TRACKING.md for the design and
checklist these tests verify against.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from tradingagents.agents.utils.scalp_schemas import EntryTrigger, HTFBias, KeyZone, LTFStructure, ScalpSignal
from tradingagents.dataflows.scalp_journal import ScalpJournal


def _key_zone(price: float = 2400.0) -> KeyZone:
    return KeyZone(price=price, label="prior day high", source_timeframe="1D")


def _signal(**overrides) -> ScalpSignal:
    fields = dict(
        symbol="XAUUSD",
        generated_at_utc=datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc),
        htf_bias=HTFBias(
            bias="bullish",
            confidence="high",
            key_zones=[_key_zone()],
            rationale="4H/1H HH/HL structure, EMA stack bullish, ATR normal.",
            invalidation_note="A 1H close below 2390 would flip this bias.",
        ),
        ltf_structure=LTFStructure(
            event="BOS",
            event_price=2402.0,
            displacement_atr=0.4,
            session="london_ny_overlap",
            volatility_regime="normal",
            agrees_with_htf=True,
            tradeable=True,
            rationale="15m BOS agrees with HTF bullish bias during overlap session.",
        ),
        entry_trigger=EntryTrigger(
            trigger_type="order_block_retest",
            triggered=True,
            confluence_zone=_key_zone(),
            entry_price=2401.0,
            stop_loss=2398.0,
            take_profit_1=2407.0,
            risk_reward_1=2.0,
            passed_min_rr=True,
            passed_max_sl=True,
            rationale="Retest of bullish order block confluent with prior day high.",
        ),
    )
    fields.update(overrides)
    return ScalpSignal(**fields)


def make_journal(tmp_path, filename="scalp_journal.jsonl"):
    config = {"scalping": {"journal_path": str(tmp_path / filename)}}
    return ScalpJournal(config)


@pytest.mark.unit
class TestScalpJournalWritePath:
    def test_append_creates_file(self, tmp_path):
        journal = make_journal(tmp_path)
        assert not (tmp_path / "scalp_journal.jsonl").exists()
        journal.append(_signal())
        assert (tmp_path / "scalp_journal.jsonl").exists()

    def test_append_produces_valid_jsonl_one_record_per_line(self, tmp_path):
        journal = make_journal(tmp_path)
        journal.append(_signal(signal_id="sig-1"))
        journal.append(_signal(signal_id="sig-2"))
        journal.append(_signal(signal_id="sig-3"))

        text = (tmp_path / "scalp_journal.jsonl").read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line.strip()]
        assert len(lines) == 3
        for line in lines:
            record = json.loads(line)
            assert "signal_id" in record
            assert record["symbol"] == "XAUUSD"

    def test_append_preserves_order(self, tmp_path):
        journal = make_journal(tmp_path)
        journal.append(_signal(signal_id="sig-1"))
        journal.append(_signal(signal_id="sig-2"))
        signals = journal.read_signals()
        assert [s.signal_id for s in signals] == ["sig-1", "sig-2"]

    def test_read_signals_round_trips(self, tmp_path):
        journal = make_journal(tmp_path)
        original = _signal(signal_id="sig-1")
        journal.append(original)
        [loaded] = journal.read_signals()
        assert loaded.signal_id == "sig-1"
        assert loaded.symbol == "XAUUSD"
        assert loaded.htf_bias.bias == "bullish"
        assert loaded.entry_trigger.confluence_zone.price == 2400.0
        assert loaded.generated_at_utc == original.generated_at_utc

    def test_read_signals_empty_when_no_file(self, tmp_path):
        journal = make_journal(tmp_path)
        assert journal.read_signals() == []

    def test_no_journal_path_is_noop(self):
        journal = ScalpJournal(config=None)
        journal.append(_signal())
        assert journal.read_signals() == []
        assert journal.journal_path is None

    def test_journal_path_expands_user_and_creates_parents(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))
        config = {"scalping": {"journal_path": "~/scalp_home/scalp_journal.jsonl"}}
        journal = ScalpJournal(config)
        assert journal.journal_path == tmp_path / "scalp_home" / "scalp_journal.jsonl"
        assert journal.journal_path.parent.exists()


@pytest.mark.unit
class TestScalpJournalIdempotency:
    def test_appending_same_signal_id_twice_does_not_duplicate(self, tmp_path):
        journal = make_journal(tmp_path)
        signal = _signal(signal_id="sig-dup")
        journal.append(signal)
        journal.append(signal)
        assert len(journal.read_signals()) == 1

    def test_appending_same_signal_id_with_different_content_is_still_a_noop(self, tmp_path):
        """Idempotency keys off signal_id only -- the first write wins."""
        journal = make_journal(tmp_path)
        journal.append(_signal(signal_id="sig-dup", symbol="XAUUSD"))
        journal.append(_signal(signal_id="sig-dup", symbol="EURUSD"))
        [loaded] = journal.read_signals()
        assert loaded.symbol == "XAUUSD"

    def test_distinct_signal_ids_both_kept(self, tmp_path):
        journal = make_journal(tmp_path)
        journal.append(_signal(signal_id="sig-1"))
        journal.append(_signal(signal_id="sig-2"))
        assert len(journal.read_signals()) == 2


@pytest.mark.unit
class TestScalpJournalAtomicWrites:
    def test_stale_tmp_file_does_not_interfere(self, tmp_path):
        """A leftover tmp file from an unrelated write (or crash) must not be
        read back or corrupt subsequent appends -- writes use a fresh,
        uniquely-named tmp file each time, not a fixed name reused as scratch."""
        journal = make_journal(tmp_path)
        journal.append(_signal(signal_id="sig-1"))
        stale_tmp = tmp_path / "scalp_journal.tmp"
        stale_tmp.write_text("GARBAGE CONTENT — should be ignored", encoding="utf-8")
        journal.append(_signal(signal_id="sig-2"))
        assert len(journal.read_signals()) == 2

    def test_no_leftover_tmp_files_after_append(self, tmp_path):
        journal = make_journal(tmp_path)
        journal.append(_signal(signal_id="sig-1"))
        journal.append(_signal(signal_id="sig-2"))
        leftovers = list(tmp_path.glob("*.tmp"))
        assert leftovers == []

    def test_concurrent_appends_leave_file_readable_and_valid(self, tmp_path):
        """Smoke test: many threads appending distinct signals never leaves a
        half-written or corrupt file -- every line that ends up on disk is
        valid JSON with a unique signal_id, even if some racing writes are
        lost (read-modify-write without cross-process locking)."""
        journal = make_journal(tmp_path)
        n = 20

        def _append(i):
            journal.append(_signal(signal_id=f"sig-{i}"))

        threads = [threading.Thread(target=_append, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        text = (tmp_path / "scalp_journal.jsonl").read_text(encoding="utf-8")
        lines = [line for line in text.splitlines() if line.strip()]
        records = [json.loads(line) for line in lines]
        ids = [r["signal_id"] for r in records]
        assert len(ids) == len(set(ids)), "no duplicate signal_ids after concurrent appends"
        assert len(ids) > 0
