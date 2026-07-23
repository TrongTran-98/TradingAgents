"""Unit tests for ScalpPipeline's deterministic post-LLM verification (Phase 4).

Full design: docs/plans/gold-scalping-mt5/PLAN.md's "deterministic
verification, not LLM self-policing" -- ScalpPipeline recomputes
risk_reward_1 and the max-SL check in Python from the LLM's proposed
entry_price/stop_loss/take_profit_1 and forces passed_min_rr/passed_max_sl
to False if the LLM's numbers don't actually pass, regardless of what the
LLM claimed.
"""

from __future__ import annotations

import pytest

from tradingagents.agents.utils.scalp_schemas import EntryTrigger, KeyZone
from tradingagents.graph import scalp_pipeline as sp

_CONFIG = {"min_risk_reward": 1.5, "max_sl_atr_multiple": 2.0}


def _zone() -> KeyZone:
    return KeyZone(price=2400.0, label="prior day high", source_timeframe="1D")


def _trigger(**overrides) -> EntryTrigger:
    fields = {
        "trigger_type": "order_block_retest",
        "triggered": True,
        "confluence_zone": _zone(),
        "entry_price": 2401.0,
        "stop_loss": 2399.0,       # risk = 2.0
        "take_profit_1": 2407.0,   # reward = 6.0 -> R:R = 3.0
        "passed_min_rr": False,
        "passed_max_sl": False,
        "rationale": "test",
    }
    fields.update(overrides)
    return EntryTrigger(**fields)


@pytest.mark.unit
class TestVerifyEntryTrigger:
    def test_good_rr_and_sl_pass(self, monkeypatch):
        monkeypatch.setattr(sp, "get_entry_atr", lambda symbol, as_of: 1.0)
        verified = sp._verify_entry_trigger(_trigger(), "XAUUSD", "2026-01-01T00:00:00", _CONFIG)
        assert verified.risk_reward_1 == pytest.approx(3.0)
        assert verified.passed_min_rr is True
        assert verified.passed_max_sl is True  # risk 2.0 / atr 1.0 = 2.0 <= 2.0

    def test_llm_overclaims_rr_is_overridden_false(self, monkeypatch):
        monkeypatch.setattr(sp, "get_entry_atr", lambda symbol, as_of: 1.0)
        bad = _trigger(take_profit_1=2402.0, passed_min_rr=True)  # reward=1.0, risk=2.0 -> R:R=0.5
        verified = sp._verify_entry_trigger(bad, "XAUUSD", "2026-01-01T00:00:00", _CONFIG)
        assert verified.risk_reward_1 == pytest.approx(0.5)
        assert verified.passed_min_rr is False

    def test_llm_overclaims_max_sl_is_overridden_false(self, monkeypatch):
        monkeypatch.setattr(sp, "get_entry_atr", lambda symbol, as_of: 0.5)
        # risk = 2.0, atr = 0.5 -> 4x ATR > max_sl_atr_multiple(2.0)
        bad = _trigger(passed_max_sl=True)
        verified = sp._verify_entry_trigger(bad, "XAUUSD", "2026-01-01T00:00:00", _CONFIG)
        assert verified.passed_max_sl is False

    def test_untriggered_forces_both_false(self, monkeypatch):
        monkeypatch.setattr(sp, "get_entry_atr", lambda symbol, as_of: 1.0)
        untriggered = _trigger(
            triggered=False, trigger_type="none", confluence_zone=None,
            passed_min_rr=True, passed_max_sl=True,
        )
        verified = sp._verify_entry_trigger(untriggered, "XAUUSD", "2026-01-01T00:00:00", _CONFIG)
        assert verified.passed_min_rr is False
        assert verified.passed_max_sl is False

    def test_missing_price_field_forces_both_false(self, monkeypatch):
        monkeypatch.setattr(sp, "get_entry_atr", lambda symbol, as_of: 1.0)
        incomplete = _trigger(take_profit_1=None, passed_min_rr=True, passed_max_sl=True)
        verified = sp._verify_entry_trigger(incomplete, "XAUUSD", "2026-01-01T00:00:00", _CONFIG)
        assert verified.passed_min_rr is False
        assert verified.passed_max_sl is False

    def test_no_atr_available_forces_max_sl_false(self, monkeypatch):
        monkeypatch.setattr(sp, "get_entry_atr", lambda symbol, as_of: None)
        verified = sp._verify_entry_trigger(
            _trigger(passed_max_sl=True), "XAUUSD", "2026-01-01T00:00:00", _CONFIG
        )
        assert verified.passed_max_sl is False
