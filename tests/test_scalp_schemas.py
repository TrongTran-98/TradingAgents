"""Tests for the gold-scalping Pydantic schemas and state (Phase 3).

See docs/plans/gold-scalping-mt5/PLAN.md and TRACKING.md for the design and
checklist these tests verify against.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import tradingagents.dataflows.scalp_features as sf
from tradingagents.agents.utils.scalp_schemas import (
    EntryTrigger,
    HTFBias,
    KeyZone,
    Lesson,
    LTFStructure,
    ScalpSignal,
    WeeklyReviewResult,
)
from tradingagents.agents.utils.scalp_state import ScalpState


def _key_zone(price: float = 2400.0) -> KeyZone:
    return KeyZone(price=price, label="prior day high", source_timeframe="1D")


def _entry_trigger(**overrides) -> EntryTrigger:
    fields = dict(
        trigger_type="order_block_retest",
        triggered=True,
        confluence_zone=_key_zone(),
        entry_price=2401.0,
        stop_loss=2398.0,
        take_profit_1=2407.0,
        risk_reward_1=2.0,
        rationale="Retest of bullish order block confluent with prior day high.",
    )
    fields.update(overrides)
    return EntryTrigger(**fields)


@pytest.mark.unit
class TestEntryTriggerConfluenceInvariant:
    def test_triggered_without_confluence_zone_raises(self):
        with pytest.raises(ValidationError):
            _entry_trigger(confluence_zone=None)

    def test_triggered_with_confluence_zone_succeeds(self):
        trigger = _entry_trigger()
        assert trigger.triggered is True
        assert trigger.confluence_zone.price == 2400.0

    def test_not_triggered_without_confluence_zone_succeeds(self):
        trigger = _entry_trigger(
            triggered=False, trigger_type="none", confluence_zone=None, rationale="No valid trigger."
        )
        assert trigger.triggered is False
        assert trigger.confluence_zone is None


@pytest.mark.unit
class TestKeyZoneInteropWithScalpFeatures:
    def test_confluence_distance_accepts_key_zone_instances(self):
        zones = [_key_zone(2400.0), _key_zone(2410.0)]
        distance = sf.confluence_distance(2401.0, zones, atr=2.0)
        assert distance == pytest.approx(0.5)


@pytest.mark.unit
class TestScalpSignalConstruction:
    def test_full_signal_round_trips(self):
        htf_bias = HTFBias(
            bias="bullish",
            confidence="high",
            key_zones=[_key_zone()],
            rationale="4H/1H HH/HL structure, EMA stack bullish, ATR normal.",
            invalidation_note="A 1H close below 2390 would flip this bias.",
        )
        ltf_structure = LTFStructure(
            event="BOS",
            event_price=2402.0,
            displacement_atr=0.4,
            session="london_ny_overlap",
            volatility_regime="normal",
            agrees_with_htf=True,
            confidence="high",
            tradeable=True,
            rationale="15m BOS agrees with HTF bullish bias during overlap session.",
        )
        entry_trigger = _entry_trigger()

        signal = ScalpSignal(
            symbol="XAUUSD",
            generated_at_utc=datetime.now(timezone.utc),
            htf_bias=htf_bias,
            ltf_structure=ltf_structure,
            entry_trigger=entry_trigger,
        )

        assert signal.symbol == "XAUUSD"
        assert signal.signal_id  # default_factory populated a non-empty id
        dumped = signal.model_dump()
        assert dumped["htf_bias"]["bias"] == "bullish"
        assert dumped["entry_trigger"]["confluence_zone"]["price"] == 2400.0


@pytest.mark.unit
class TestWeeklyReviewSchemas:
    def test_lesson_and_result_construction(self):
        lesson = Lesson(
            bucket_key="setup_type=order_block_retest|session=ny|htf_ltf_agreement=False",
            lesson_text="Order-block retests against HTF bias in the NY session underperform.",
            supporting_signal_ids=["a", "b", "c"],
            occurrences=3,
        )
        result = WeeklyReviewResult(lessons=[lesson])
        assert result.lessons[0].occurrences == 3


@pytest.mark.unit
class TestScalpStateImports:
    def test_scalp_state_is_a_typed_dict_with_expected_keys(self):
        assert "htf_bias" in ScalpState.__annotations__
        assert "ltf_structure" in ScalpState.__annotations__
        assert "entry_trigger" in ScalpState.__annotations__
        assert "agrees_with_htf" in ScalpState.__annotations__
        assert "active_lessons" in ScalpState.__annotations__
