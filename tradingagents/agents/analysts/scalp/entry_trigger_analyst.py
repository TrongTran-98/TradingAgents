"""Step 3+4 -- 5m entry trigger, invalidation & targets analyst (Phase 4).

Full design: docs/plans/gold-scalping-mt5/PLAN.md. Same two-pass shape as
``htf_bias_analyst.py``. Unlike the LTF analyst, this node does *not*
recompute ``passed_min_rr``/``passed_max_sl`` itself -- PLAN.md assigns that
deterministic verification to ``ScalpPipeline.run()`` (it needs a fresh
ATR(5m) read independent of whatever the LLM's tool call happened to
surface), so this node just returns the LLM's ``EntryTrigger`` as-is.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.scalp_schemas import (
    EntryTrigger,
    render_entry_trigger,
    render_htf_bias,
    render_ltf_structure,
)
from tradingagents.agents.utils.scalp_state import ScalpState
from tradingagents.agents.utils.scalp_tools import get_entry_snapshot
from tradingagents.agents.utils.structured import bind_structured

_TOOLS = [get_entry_snapshot]


def create_entry_trigger_analyst(llm):
    """Create the Step 3+4 (5m entry trigger) analyst node.

    Requires structured-output support, same reasoning as
    ``create_htf_bias_analyst``.
    """
    structured_llm = bind_structured(llm, EntryTrigger, "Entry Trigger Analyst")
    if structured_llm is None:
        raise RuntimeError(
            "Entry Trigger Analyst requires an LLM provider with "
            "structured-output support; the scalping pipeline depends on "
            "typed fields downstream (and Python-verifies its R:R/SL "
            "numbers), not free-text parsing."
        )

    def entry_trigger_analyst_node(state: ScalpState) -> dict:
        symbol = state["symbol"]
        as_of_utc = state["as_of_utc"]
        htf_bias = state["htf_bias"]
        ltf_structure = state["ltf_structure"]
        active_lessons = state.get("active_lessons", "")
        messages = state["messages"]

        lessons_block = (
            f"\nRecent lessons from prior signals -- weigh these against the "
            f"current evidence, do not follow them blindly:\n{active_lessons}\n"
            if active_lessons else ""
        )

        system_message = (
            f"You are a gold ({symbol}) scalping analyst confirming a 5m "
            "entry trigger, stop-loss, and targets. Higher-timeframe "
            f"context:\n{render_htf_bias(htf_bias)}\n\n15m structure "
            f"context:\n{render_ltf_structure(ltf_structure)}\n\n"
            f"Call get_entry_snapshot exactly once for {symbol} as of "
            f"{as_of_utc} to retrieve the deterministic 5m trigger "
            "candidates (liquidity sweep, order block retest, FVG fill, EMA "
            "pullback + momentum) with their confluence distance to the "
            "nearest key level. A trigger with no confluence (distance is "
            "'n/a' or clearly far) must be rejected -- report "
            "triggered=False rather than forcing an entry, and "
            "confluence_zone must be filled whenever triggered=True. Place "
            "the stop loss beyond the structural invalidation point plus "
            "the configured ATR(5m) buffer range, reject (triggered=False) "
            "if the structural stop distance exceeds the configured max SL "
            "ATR multiple, and reject if the resulting risk:reward to "
            "take_profit_1 is below the configured minimum -- the pipeline "
            "independently recomputes and enforces both checks in Python "
            f"regardless of what you report here.{lessons_block}"
        )

        has_tool_result = any(isinstance(m, ToolMessage) for m in messages)

        if not has_tool_result:
            prompt = ChatPromptTemplate.from_messages(
                [
                    ("system", system_message),
                    MessagesPlaceholder(variable_name="messages"),
                ]
            )
            chain = prompt | llm.bind_tools(_TOOLS)
            result = chain.invoke(messages)
            return {"messages": [result]}

        final_prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    system_message
                    + "\n\nThe snapshot has already been retrieved above. Using "
                    "only that data, produce your final EntryTrigger now.",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )
        final_messages = final_prompt.format_messages(messages=messages)
        entry_trigger = structured_llm.invoke(final_messages)

        return {
            "messages": [AIMessage(content=render_entry_trigger(entry_trigger))],
            "entry_trigger": entry_trigger,
        }

    return entry_trigger_analyst_node
