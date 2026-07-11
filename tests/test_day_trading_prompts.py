"""Phase 5 of the 4H day-trading plan: agent prompts for day-trading semantics.

In 4H mode the market analyst must reason about a specific closed 4H bar with
fast indicators (10 EMA / RSI / MACD / ATR) instead of long-horizon 50/200 SMA
framing, and the decision-side agents (trader, risk debators, portfolio
manager) must size stops in 4H-ATR multiples, state a holding horizon in
hours, and treat the day-granular news/sentiment reports as background
context.  In daily mode ("1d" or timeframe absent) every prompt must be
byte-identical to before the feature.

Prompt-capture conventions follow test_graph_timeframe_propagation.py
(_RecordingLLM) and test_structured_agents.py (MagicMock structured LLMs);
no network, no real LLM.
"""

from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable

from tradingagents.agents.managers.portfolio_manager import create_portfolio_manager
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import (
    create_conservative_debator,
)
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
from tradingagents.agents.schemas import (
    PortfolioDecision,
    PortfolioRating,
    TraderAction,
    TraderProposal,
)
from tradingagents.agents.trader.trader import create_trader
from tradingagents.agents.utils.agent_utils import get_timeframe_context_from_state

MARKET_MARKER = "Day-trading mode (4H bars)"
DECISION_MARKER = "Day-trading context (4H bars)"


def _base_state(timeframe: str | None) -> dict:
    state = {
        "company_of_interest": "BTC-USD",
        "asset_type": "crypto",
        "instrument_context": "The asset to analyze is `BTC-USD`.",
        "messages": [],
    }
    if timeframe is not None:
        state["timeframe"] = timeframe
        state["trade_date"] = (
            "2026-07-08 12:00" if timeframe != "1d" else "2026-07-08"
        )
    else:
        state["trade_date"] = "2026-07-08"
    return state


# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestTimeframeContextHelper:
    def test_daily_returns_empty(self):
        assert get_timeframe_context_from_state({"timeframe": "1d"}) == ""

    def test_missing_timeframe_defaults_to_daily(self):
        assert get_timeframe_context_from_state({}) == ""

    def test_4h_covers_the_three_plan_requirements(self):
        ctx = get_timeframe_context_from_state({"timeframe": "4h"})
        assert DECISION_MARKER in ctx
        # Stops/targets in 4H-ATR multiples, tighter than daily.
        assert "multiples of the 4H-bar ATR" in ctx
        assert "tighter than daily-bar stops" in ctx
        # Holding horizon stated in hours, not weeks.
        assert "holding" in ctx and "hours" in ctx
        assert "state that horizon explicitly in the final decision" in ctx
        # News/sentiment is lower-frequency background context.
        assert "day-granular" in ctx
        assert "background regime context" in ctx


# ---------------------------------------------------------------------------
# Market analyst
# ---------------------------------------------------------------------------


class _RecordingLLM:
    """Fake chat model: records every prompt it is invoked with."""

    def __init__(self):
        self.prompts = []

    def bind_tools(self, tools):
        outer = self

        class _Bound(Runnable):
            def invoke(self, input, config=None, **kwargs):
                outer.prompts.append(input)
                return AIMessage(content="stub report")

        return _Bound()

    def system_text(self) -> str:
        assert self.prompts, "the analyst never invoked the LLM"
        return self.prompts[-1].to_messages()[0].content


def _market_system_text(timeframe: str | None) -> str:
    from tradingagents.agents.analysts.market_analyst import create_market_analyst

    llm = _RecordingLLM()
    create_market_analyst(llm)(_base_state(timeframe))
    return llm.system_text()


@pytest.mark.unit
class TestMarketAnalystPrompt:
    def test_4h_prompt_contains_day_trading_framing(self):
        text = _market_system_text("4h")
        assert MARKET_MARKER in text
        # "Today's date" is a specific 4H bar close, not a calendar day.
        assert "intraday UTC timestamp" in text
        assert "*closed* 4H bar" in text
        # Fast indicators favored over 200-SMA-style long-horizon framing.
        for fast in ("close_10_ema", "rsi", "macd", "atr"):
            assert fast in text.split(MARKET_MARKER, 1)[1]
        assert "background regime context only" in text

    def test_daily_prompt_unchanged(self):
        explicit = _market_system_text("1d")
        implicit = _market_system_text(None)
        assert MARKET_MARKER not in explicit
        assert explicit == implicit


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


def _trader_prompt(timeframe: str | None) -> list[dict]:
    captured = {}
    proposal = TraderProposal(action=TraderAction.HOLD, reasoning="stub")
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or proposal
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    state = _base_state(timeframe)
    state["investment_plan"] = "**Recommendation**: Hold"
    create_trader(llm)(state)
    return captured["prompt"]


@pytest.mark.unit
class TestTraderPrompt:
    def test_4h_system_message_contains_day_trading_context(self):
        system = _trader_prompt("4h")[0]["content"]
        assert DECISION_MARKER in system
        assert "multiples of the 4H-bar ATR" in system
        assert "hours" in system

    def test_daily_prompt_unchanged(self):
        explicit = _trader_prompt("1d")
        implicit = _trader_prompt(None)
        assert DECISION_MARKER not in explicit[0]["content"]
        assert explicit == implicit


# ---------------------------------------------------------------------------
# Risk debators
# ---------------------------------------------------------------------------


def _risk_state(timeframe: str | None) -> dict:
    state = _base_state(timeframe)
    state.update(
        {
            "market_report": "market report body",
            "sentiment_report": "sentiment report body",
            "news_report": "news report body",
            "fundamentals_report": "fundamentals report body",
            "trader_investment_plan": "**Action**: Hold",
            "investment_plan": "**Recommendation**: Hold",
            "risk_debate_state": {
                "history": "",
                "aggressive_history": "",
                "conservative_history": "",
                "neutral_history": "",
                "current_aggressive_response": "",
                "current_conservative_response": "",
                "current_neutral_response": "",
                "count": 0,
            },
        }
    )
    return state


DEBATOR_FACTORIES = [
    create_aggressive_debator,
    create_conservative_debator,
    create_neutral_debator,
]


def _debator_prompt(factory, timeframe: str | None) -> str:
    llm = MagicMock()
    llm.invoke.return_value = MagicMock(content="argument")
    factory(llm)(_risk_state(timeframe))
    (prompt,), _ = llm.invoke.call_args
    return prompt


@pytest.mark.unit
class TestRiskDebatorPrompts:
    @pytest.mark.parametrize("factory", DEBATOR_FACTORIES)
    def test_4h_prompt_contains_day_trading_context(self, factory):
        prompt = _debator_prompt(factory, "4h")
        assert DECISION_MARKER in prompt
        assert "multiples of the 4H-bar ATR" in prompt
        assert "hours" in prompt

    @pytest.mark.parametrize("factory", DEBATOR_FACTORIES)
    def test_daily_prompt_unchanged(self, factory):
        explicit = _debator_prompt(factory, "1d")
        implicit = _debator_prompt(factory, None)
        assert DECISION_MARKER not in explicit
        assert explicit == implicit


# ---------------------------------------------------------------------------
# Portfolio manager (writes the actual final_trade_decision)
# ---------------------------------------------------------------------------


def _pm_prompt(timeframe: str | None) -> str:
    captured = {}
    decision = PortfolioDecision(
        rating=PortfolioRating.HOLD,
        executive_summary="stub",
        investment_thesis="stub",
    )
    structured = MagicMock()
    structured.invoke.side_effect = lambda prompt: (
        captured.__setitem__("prompt", prompt) or decision
    )
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    create_portfolio_manager(llm)(_risk_state(timeframe))
    return captured["prompt"]


@pytest.mark.unit
class TestPortfolioManagerPrompt:
    def test_4h_prompt_contains_day_trading_context(self):
        prompt = _pm_prompt("4h")
        assert DECISION_MARKER in prompt
        assert "state that horizon explicitly in the final decision" in prompt

    def test_daily_prompt_unchanged(self):
        explicit = _pm_prompt("1d")
        implicit = _pm_prompt(None)
        assert DECISION_MARKER not in explicit
        assert explicit == implicit
