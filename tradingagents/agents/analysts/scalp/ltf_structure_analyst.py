"""Step 2 -- 15m trend alignment & structure-shift analyst (Phase 4).

Full design: docs/plans/gold-scalping-mt5/PLAN.md. Same two-pass shape as
``htf_bias_analyst.py``. After the LLM produces its own best-effort
``LTFStructure`` (including its own guess at ``agrees_with_htf``), this node
overwrites ``agrees_with_htf`` with ``scalp_tools.compute_ltf_alignment``'s
deterministic result -- Step 2's "computed as a boolean, not left to LLM
judgment alone" gate -- and mirrors it into the top-level
``ScalpState.agrees_with_htf`` field for Phase 7's bucketing.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.scalp_schemas import (
    LTFStructure,
    render_htf_bias,
    render_ltf_structure,
)
from tradingagents.agents.utils.scalp_state import ScalpState
from tradingagents.agents.utils.scalp_tools import compute_ltf_alignment, get_ltf_snapshot
from tradingagents.agents.utils.structured import bind_structured, invoke_structured_with_fallback

_TOOLS = [get_ltf_snapshot]


def create_ltf_structure_analyst(llm):
    """Create the Step 2 (15m structure) analyst node.

    Requires structured-output support, same reasoning as
    ``create_htf_bias_analyst``.
    """
    structured_llm = bind_structured(llm, LTFStructure, "LTF Structure Analyst")
    if structured_llm is None:
        raise RuntimeError(
            "LTF Structure Analyst requires an LLM provider with "
            "structured-output support; the scalping pipeline depends on "
            "typed fields downstream, not free-text parsing."
        )

    def ltf_structure_analyst_node(state: ScalpState) -> dict:
        symbol = state["symbol"]
        as_of_utc = state["as_of_utc"]
        htf_bias = state["htf_bias"]
        active_lessons = state.get("active_lessons", "")
        messages = state["messages"]

        lessons_block = (
            f"\nRecent lessons from prior signals -- weigh these against the "
            f"current evidence, do not follow them blindly:\n{active_lessons}\n"
            if active_lessons else ""
        )

        system_message = (
            f"You are a gold ({symbol}) scalping analyst filtering the "
            "higher-timeframe bias through 15m structure and session/"
            "volatility context. The Step 1 higher-timeframe read is:\n"
            f"{render_htf_bias(htf_bias)}\n\n"
            f"Call get_ltf_snapshot exactly once for {symbol} as of "
            f"{as_of_utc} to retrieve the deterministic 15m structure, "
            "BOS/CHoCH event, session, and volatility regime. Only report "
            "the event/session/ATR readings present in that snapshot -- "
            "never invent one. Set tradeable=False when off-session, when "
            "volatility is low (chop) or an abnormal spike, or when 15m "
            "structure disagrees with the Step 1 bias without a clean "
            "CHoCH. Prefer entries after a retracement into the broken "
            f"level over chasing the breakout candle.{lessons_block}"
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
                    "only that data, produce your final LTFStructure now.",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )
        final_messages = final_prompt.format_messages(messages=messages)
        ltf_structure = invoke_structured_with_fallback(
            structured_llm,
            final_messages,
            fallback=LTFStructure(
                event="none",
                session="off_session",
                volatility_regime="low",
                agrees_with_htf=False,
                tradeable=False,
                rationale=(
                    "LLM did not return a parseable structured read this run; "
                    "skipping this cycle rather than forcing a trade decision."
                ),
            ),
            agent_name="LTF Structure Analyst",
        )

        agrees_with_htf = compute_ltf_alignment(symbol, as_of_utc, htf_bias.bias)
        ltf_structure = ltf_structure.model_copy(update={"agrees_with_htf": agrees_with_htf})

        return {
            "messages": [AIMessage(content=render_ltf_structure(ltf_structure))],
            "ltf_structure": ltf_structure,
            "agrees_with_htf": agrees_with_htf,
        }

    return ltf_structure_analyst_node
