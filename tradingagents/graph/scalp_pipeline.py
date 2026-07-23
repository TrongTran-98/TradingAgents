"""ScalpPipeline: 3-stage sequential LangGraph for the gold-scalping pipeline (Phase 4).

Full design: docs/plans/gold-scalping-mt5/PLAN.md. Mirrors
``TradingAgentsGraph``/``graph/setup.py``'s analyst-node + ToolNode +
clear-node wiring, scoped to the three scalp analysts with no debate/risk
stages: HTF Bias -> LTF Structure -> Entry Trigger. The LTF Structure ->
Entry Trigger edge is gated by ``LTFStructure.tradeable`` -- an
untradeable read skips Step 3+4 entirely rather than forcing an entry
attempt.

Deliberately standalone from ``TradingAgentsGraph``: different state shape
(``ScalpState``, not ``AgentState``), no debate/risk stages, and no shared
``ConditionalLogic`` (its methods are keyed to the daily-equity analyst set).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage, RemoveMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents.analysts.scalp.entry_trigger_analyst import create_entry_trigger_analyst
from tradingagents.agents.analysts.scalp.htf_bias_analyst import create_htf_bias_analyst
from tradingagents.agents.analysts.scalp.ltf_structure_analyst import create_ltf_structure_analyst
from tradingagents.agents.utils.scalp_schemas import EntryTrigger, ScalpSignal
from tradingagents.agents.utils.scalp_state import ScalpState
from tradingagents.agents.utils.scalp_tools import (
    get_entry_atr,
    get_entry_snapshot,
    get_htf_snapshot,
    get_ltf_snapshot,
)
from tradingagents.default_config import DEFAULT_CONFIG


def _create_scalp_msg_clear():
    """Clear the tool-loop scratch messages between stages.

    Mirrors ``agent_utils.create_msg_delete``: the meaningful hand-off
    between stages travels through ``ScalpState``'s typed
    ``htf_bias``/``ltf_structure`` fields (rendered into each next stage's
    own system message), not the accumulated message list, so it's safe to
    wipe it and reseed a placeholder before the next stage's tool loop.
    """

    def clear_messages(state: ScalpState) -> dict:
        removal_operations = [RemoveMessage(id=m.id) for m in state["messages"]]
        placeholder = HumanMessage(
            content=(
                f"Proceed with your assigned scalping analysis stage for "
                f"{state['symbol']} as of {state['as_of_utc']} UTC."
            )
        )
        return {"messages": removal_operations + [placeholder]}

    return clear_messages


def _has_pending_tool_call(state: ScalpState) -> bool:
    last_message = state["messages"][-1]
    return bool(getattr(last_message, "tool_calls", None))


def _should_continue_htf(state: ScalpState) -> str:
    return "tools_htf" if _has_pending_tool_call(state) else "Msg Clear HTF"


def _should_continue_ltf(state: ScalpState) -> str:
    if _has_pending_tool_call(state):
        return "tools_ltf"
    ltf_structure = state["ltf_structure"]
    if ltf_structure is not None and not ltf_structure.tradeable:
        return END
    return "Msg Clear LTF"


def _should_continue_entry(state: ScalpState) -> str:
    return "tools_entry" if _has_pending_tool_call(state) else END


def _verify_entry_trigger(
    entry_trigger: EntryTrigger, symbol: str, as_of_utc: str, config: dict
) -> EntryTrigger:
    """Deterministic post-LLM verification of R:R and max-SL (PLAN.md's "LLM proposes, Python verifies").

    Recomputes ``risk_reward_1`` and the max-SL-distance check from the
    LLM's proposed ``entry_price``/``stop_loss``/``take_profit_1`` and
    forces ``passed_min_rr``/``passed_max_sl`` to False whenever the LLM's
    numbers don't actually pass -- mirrors
    ``market_data_validator.build_verified_market_snapshot``'s philosophy.
    A fresh ATR(5m) read is used rather than whatever the LLM's tool call
    happened to surface, so this check can't be gamed by a stale snapshot.
    """
    if not entry_trigger.triggered:
        return entry_trigger.model_copy(update={"passed_min_rr": False, "passed_max_sl": False})

    entry, stop, tp1 = entry_trigger.entry_price, entry_trigger.stop_loss, entry_trigger.take_profit_1
    if entry is None or stop is None or tp1 is None:
        return entry_trigger.model_copy(update={"passed_min_rr": False, "passed_max_sl": False})

    risk = abs(entry - stop)
    reward = abs(tp1 - entry)
    risk_reward_1 = round(reward / risk, 4) if risk > 0 else None
    passed_min_rr = risk_reward_1 is not None and risk_reward_1 >= config["min_risk_reward"]

    atr_5m = get_entry_atr(symbol, as_of_utc)
    passed_max_sl = (
        atr_5m is not None and atr_5m > 0 and risk / atr_5m <= config["max_sl_atr_multiple"]
    )

    return entry_trigger.model_copy(
        update={
            "risk_reward_1": risk_reward_1,
            "passed_min_rr": passed_min_rr,
            "passed_max_sl": passed_max_sl,
        }
    )


class ScalpPipeline:
    """3-stage sequential LangGraph: HTF Bias -> LTF Structure -> Entry Trigger."""

    def __init__(self, llm, config: dict[str, Any] | None = None):
        self.llm = llm
        self.config = config or DEFAULT_CONFIG
        self.tool_nodes = self._create_tool_nodes()
        self.graph = self._build_graph().compile()

    @staticmethod
    def _create_tool_nodes() -> dict[str, ToolNode]:
        """Static tool registration -- callable without an LLM (see test_scalp_toolnode.py)."""
        return {
            "htf": ToolNode([get_htf_snapshot]),
            "ltf": ToolNode([get_ltf_snapshot]),
            "entry": ToolNode([get_entry_snapshot]),
        }

    def _build_graph(self) -> StateGraph:
        workflow = StateGraph(ScalpState)

        workflow.add_node("HTF Bias", create_htf_bias_analyst(self.llm))
        workflow.add_node("tools_htf", self.tool_nodes["htf"])
        workflow.add_node("Msg Clear HTF", _create_scalp_msg_clear())

        workflow.add_node("LTF Structure", create_ltf_structure_analyst(self.llm))
        workflow.add_node("tools_ltf", self.tool_nodes["ltf"])
        workflow.add_node("Msg Clear LTF", _create_scalp_msg_clear())

        workflow.add_node("Entry Trigger", create_entry_trigger_analyst(self.llm))
        workflow.add_node("tools_entry", self.tool_nodes["entry"])

        workflow.add_edge(START, "HTF Bias")
        workflow.add_conditional_edges(
            "HTF Bias",
            _should_continue_htf,
            {"tools_htf": "tools_htf", "Msg Clear HTF": "Msg Clear HTF"},
        )
        workflow.add_edge("tools_htf", "HTF Bias")
        workflow.add_edge("Msg Clear HTF", "LTF Structure")

        workflow.add_conditional_edges(
            "LTF Structure",
            _should_continue_ltf,
            {"tools_ltf": "tools_ltf", "Msg Clear LTF": "Msg Clear LTF", END: END},
        )
        workflow.add_edge("tools_ltf", "LTF Structure")
        workflow.add_edge("Msg Clear LTF", "Entry Trigger")

        workflow.add_conditional_edges(
            "Entry Trigger", _should_continue_entry, {"tools_entry": "tools_entry", END: END}
        )
        workflow.add_edge("tools_entry", "Entry Trigger")

        return workflow

    def run(self, symbol: str, as_of_utc: datetime | str, active_lessons: str = "") -> ScalpSignal:
        """Run the pipeline once and return the resulting ScalpSignal.

        ``as_of_utc`` anchors every tool's data fetch -- a real-time caller
        passes the current UTC time, a backtest/manual-verification caller
        passes a historical timestamp (TRACKING.md Phase 4's end-to-end
        check). When Step 2 is untradeable, Step 3+4 never runs and the
        returned signal's ``entry_trigger`` is a synthetic
        ``triggered=False`` placeholder.
        """
        if isinstance(as_of_utc, datetime):
            if as_of_utc.tzinfo is not None:
                as_of_utc = as_of_utc.astimezone(timezone.utc).replace(tzinfo=None)
            as_of_str = as_of_utc.isoformat()
        else:
            as_of_str = as_of_utc

        signal_id = uuid.uuid4().hex
        initial_state = {
            "messages": [
                HumanMessage(content=f"Analyze {symbol} as of {as_of_str} UTC for a scalp signal.")
            ],
            "symbol": symbol,
            "as_of_utc": as_of_str,
            "signal_id": signal_id,
            "htf_bias": None,
            "ltf_structure": None,
            "agrees_with_htf": False,
            "entry_trigger": None,
            "active_lessons": active_lessons,
        }

        scalping_config = self.config["scalping"]
        final_state = self.graph.invoke(initial_state, {"recursion_limit": 50})

        entry_trigger = final_state.get("entry_trigger")
        if entry_trigger is not None:
            entry_trigger = _verify_entry_trigger(entry_trigger, symbol, as_of_str, scalping_config)
        else:
            entry_trigger = EntryTrigger(
                trigger_type="none",
                triggered=False,
                rationale="Pipeline stopped before Step 3+4: LTF structure was not tradeable.",
            )

        return ScalpSignal(
            signal_id=signal_id,
            symbol=symbol,
            generated_at_utc=datetime.fromisoformat(as_of_str),
            htf_bias=final_state["htf_bias"],
            ltf_structure=final_state["ltf_structure"],
            entry_trigger=entry_trigger,
        )
