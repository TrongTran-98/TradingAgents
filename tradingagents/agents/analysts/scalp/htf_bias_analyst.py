"""Step 1 -- combined 4H/1H bias & key-level analyst (Phase 4).

Full design: docs/plans/gold-scalping-mt5/PLAN.md. Two-pass node, mirroring
``market_analyst.py``'s tool-calling shape but with a structured final
answer: pass 1 asks the LLM to call ``get_htf_snapshot`` (routed to
``tools_htf`` by ``scalp_pipeline.py``'s conditional edge); pass 2 -- once a
``ToolMessage`` is present -- discards free-text generation and calls the
structured-output LLM directly over the accumulated messages to produce a
typed ``HTFBias``. This differs from the daily-equity analysts
(``sentiment_analyst.py`` et al.), which never need a real tool round trip
because their data is pre-fetched; here the snapshot tool is genuinely
LLM-invoked (mirrors ``get_indicators``), so a real ToolNode round trip
happens first.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.scalp_schemas import HTFBias, render_htf_bias
from tradingagents.agents.utils.scalp_state import ScalpState
from tradingagents.agents.utils.scalp_tools import get_htf_snapshot
from tradingagents.agents.utils.structured import bind_structured, invoke_structured_with_fallback

_TOOLS = [get_htf_snapshot]


def create_htf_bias_analyst(llm):
    """Create the Step 1 (4H/1H bias) analyst node.

    Requires structured-output support: downstream steps (the LTF alignment
    gate, confluence checks) read typed ``HTFBias`` fields, unlike the daily
    equity analysts which only need rendered markdown for human debate.
    """
    structured_llm = bind_structured(llm, HTFBias, "HTF Bias Analyst")
    if structured_llm is None:
        raise RuntimeError(
            "HTF Bias Analyst requires an LLM provider with structured-output "
            "support (with_structured_output); the scalping pipeline depends "
            "on typed fields downstream, not free-text parsing."
        )

    def htf_bias_analyst_node(state: ScalpState) -> dict:
        symbol = state["symbol"]
        as_of_utc = state["as_of_utc"]
        active_lessons = state.get("active_lessons", "")
        messages = state["messages"]

        lessons_block = (
            f"\nRecent lessons from prior signals -- weigh these against the "
            f"current evidence, do not follow them blindly:\n{active_lessons}\n"
            if active_lessons else ""
        )

        system_message = (
            f"You are a gold ({symbol}) scalping analyst producing the "
            "higher-timeframe (4H/1H) directional bias for a fast intraday "
            f"pipeline. Call get_htf_snapshot exactly once for {symbol} as of "
            f"{as_of_utc} to retrieve the deterministic 4H/1H structure, EMA "
            "stack, ATR regime, and candidate key levels. Only use "
            "structure/EMA/ATR readings and key levels present in that "
            "snapshot -- never invent a level or reading it doesn't report. "
            "Pick up to 5 of the most relevant candidate key levels for "
            "key_zones, ranked most-relevant-first. Call bias 'range' when "
            "4H/1H structure or EMA stack disagree or are compressed; do not "
            f"force a direction.{lessons_block}"
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
                    "only that data, produce your final HTFBias now.",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )
        final_messages = final_prompt.format_messages(messages=messages)
        htf_bias = invoke_structured_with_fallback(
            structured_llm,
            final_messages,
            fallback=HTFBias(
                bias="range",
                confidence="low",
                key_zones=[],
                rationale=(
                    "LLM did not return a parseable structured bias this run; "
                    "treating as no-clear-bias rather than forcing a direction."
                ),
                invalidation_note="n/a",
            ),
            agent_name="HTF Bias Analyst",
        )

        return {
            "messages": [AIMessage(content=render_htf_bias(htf_bias))],
            "htf_bias": htf_bias,
        }

    return htf_bias_analyst_node
