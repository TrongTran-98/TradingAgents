"""LangGraph state for the gold-scalping pipeline (Phase 3).

Mirrors tradingagents/agents/utils/agent_states.py's ``AgentState`` role for
the equity pipeline: a ``MessagesState`` subclass carrying the three
sequential steps' outputs plus cross-cutting context across
``ScalpPipeline``'s nodes (graph/scalp_pipeline.py, Phase 4).
"""

from __future__ import annotations

from typing import Annotated

from langgraph.graph import MessagesState

from tradingagents.agents.utils.scalp_schemas import EntryTrigger, HTFBias, LTFStructure


class ScalpState(MessagesState):
    symbol: Annotated[str, "Instrument under analysis, e.g. XAUUSD"]
    as_of_utc: Annotated[str, "ISO-8601 UTC timestamp the pipeline run is anchored to"]
    signal_id: Annotated[str, "UUID identifying this run for journal cross-referencing"]

    htf_bias: Annotated[HTFBias | None, "Step 1 output: 4H/1H combined bias"]
    ltf_structure: Annotated[LTFStructure | None, "Step 2 output: 15m structure/session read"]
    agrees_with_htf: Annotated[
        bool, "Python-computed alignment gate: does 15m structure agree with the Step-1 bias"
    ]
    entry_trigger: Annotated[EntryTrigger | None, "Step 3+4 output: 5m entry trigger + SL/TP"]

    active_lessons: Annotated[
        str,
        "Weekly-reflection lessons injected into each analyst's prompt at pipeline start "
        "(seam for Phase 4, full write path lands in Phase 7; empty string until then)",
    ]
