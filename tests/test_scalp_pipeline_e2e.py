"""Full ScalpPipeline.run() smoke test with a scripted fake LLM (Phase 4).

Exercises the real 3-stage graph end-to-end -- two-pass tool-call/structured
node logic, message clearing between stages, the LTF tradeable gate, the
Python-recomputed agrees_with_htf alignment gate, and ScalpPipeline.run()'s
deterministic R:R/max-SL verification -- with MT5 mocked out (same
technique as test_scalp_tools.py) and a scripted LLM stand-in. Real
provider/LLM behavior needs an actual model and a live MT5 terminal; see
TRACKING.md's manual end-to-end check for that.
"""

from __future__ import annotations

from collections.abc import Callable

import pandas as pd
import pytest
from langchain_core.messages import AIMessage

import tradingagents.agents.utils.scalp_tools as scalp_tools
from tradingagents.agents.utils.scalp_schemas import EntryTrigger, HTFBias, KeyZone, LTFStructure
from tradingagents.graph.scalp_pipeline import ScalpPipeline

# Known-good zigzag (validated by test_scalp_features.py's swing-detection
# tests): produces swing highs at 13 then 14 and swing lows at 8 then 8.5 --
# a clean HH+HL bullish read. Tiled once so there's enough history for
# ATR(14)'s min_periods.
_BULLISH_ZIGZAG = [10, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10, 11, 12, 13, 14, 13, 12, 11, 10, 9, 8.5, 9, 10, 11, 12]


def _bullish_zigzag_df(freq: str) -> pd.DataFrame:
    prices = [2300.0 + p for p in _BULLISH_ZIGZAG * 2]
    times = pd.date_range("2026-01-01", periods=len(prices), freq=freq)
    return pd.DataFrame({
        "time": times,
        "open": prices,
        "high": [p + 0.3 for p in prices],
        "low": [p - 0.3 for p in prices],
        "close": prices,
    })


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


class _StructuredInvoker:
    def __init__(self, builder: Callable[[], object]):
        self._builder = builder

    def invoke(self, prompt):
        return self._builder()


class ScriptedFakeLLM:
    """``bind_tools`` always requests the single bound tool; ``with_structured_output``
    returns a fixed, schema-appropriate object regardless of prompt content."""

    def __init__(self, structured_builders: dict[str, Callable[[], object]]):
        self._structured_builders = structured_builders

    def bind_tools(self, tools):
        tool = tools[0]

        def _call(messages):
            return AIMessage(
                content="",
                tool_calls=[{
                    "name": tool.name,
                    "args": {"symbol": "XAUUSD", "as_of_utc": "2026-02-01T12:00:00"},
                    "id": f"call_{tool.name}",
                }],
            )

        return _call

    def with_structured_output(self, schema):
        return _StructuredInvoker(self._structured_builders[schema.__name__])


def _make_llm() -> ScriptedFakeLLM:
    def _htf_bias():
        return HTFBias(
            bias="bullish",
            confidence="high",
            key_zones=[KeyZone(price=2400.0, label="prior day high", source_timeframe="1D")],
            rationale="4H/1H HH/HL, EMA stack bullish.",
            invalidation_note="A 1H close below 2390 would flip this.",
        )

    def _ltf_structure():
        return LTFStructure(
            event="BOS",
            event_price=2402.0,
            displacement_atr=0.4,
            session="london_ny_overlap",
            volatility_regime="normal",
            agrees_with_htf=False,  # deliberately wrong -- the pipeline must overwrite this
            tradeable=True,
            rationale="15m BOS agrees with HTF bullish bias during overlap session.",
        )

    def _entry_trigger():
        return EntryTrigger(
            trigger_type="order_block_retest",
            triggered=True,
            confluence_zone=KeyZone(price=2400.0, label="prior day high", source_timeframe="1D"),
            entry_price=2401.0,
            stop_loss=2399.0,
            take_profit_1=2407.0,
            passed_min_rr=False,  # deliberately wrong -- the pipeline must recompute this
            rationale="Retest of bullish order block confluent with prior day high.",
        )

    return ScriptedFakeLLM({
        "HTFBias": _htf_bias,
        "LTFStructure": _ltf_structure,
        "EntryTrigger": _entry_trigger,
    })


@pytest.mark.unit
def test_scalp_pipeline_run_end_to_end(monkeypatch):
    df_by_tf = {
        "4H": _trending_df(300, freq="4h"),
        "1H": _trending_df(300, freq="1h"),
        "15m": _bullish_zigzag_df(freq="15min"),
        "5m": _trending_df(350, freq="5min"),
    }

    def _fake_range(symbol, timeframe, date_from, date_to, utc_offset_hours=0.0):
        return df_by_tf[timeframe].copy()

    monkeypatch.setattr(scalp_tools.mt5_vendor, "get_mt5_rates_range", _fake_range)

    pipeline = ScalpPipeline(_make_llm())
    signal = pipeline.run("XAUUSD", "2026-02-01T12:00:00")

    assert signal.symbol == "XAUUSD"
    assert signal.htf_bias.bias == "bullish"
    assert signal.ltf_structure.tradeable is True
    # Python-recomputed from the real (mocked) 15m bars, not the LLM's
    # (deliberately wrong) claim above -- the zigzag fixture genuinely
    # classifies as bullish structure, matching the HTF bias direction.
    assert signal.ltf_structure.agrees_with_htf is True
    assert signal.entry_trigger.triggered is True
    # entry=2401, stop=2399 (risk=2), tp1=2407 (reward=6) -> R:R = 3.0,
    # deterministically recomputed by ScalpPipeline.run(), not the LLM's
    # (deliberately wrong) passed_min_rr=False claim above.
    assert signal.entry_trigger.risk_reward_1 == pytest.approx(3.0)
    assert signal.entry_trigger.passed_min_rr is True
