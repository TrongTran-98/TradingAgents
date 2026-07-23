"""ScalpPipeline graph-wiring regression guard (Phase 4).

Builds and compiles the real 3-stage LangGraph against a stub LLM (no real
provider calls) to catch node/edge wiring typos -- e.g. a conditional edge
pointing at a node name that was never registered -- which LangGraph raises
on at ``compile()`` time. Full behavioral verification (actual LLM tool
calls, structured output) needs a real provider and is covered by
TRACKING.md's manual end-to-end check.
"""

from __future__ import annotations

import pytest

from tradingagents.graph.scalp_pipeline import ScalpPipeline


class _StubLLM:
    """Minimal stand-in satisfying bind_structured/bind_tools at node-factory time."""

    def with_structured_output(self, schema):
        return self

    def bind_tools(self, tools):
        return self


@pytest.mark.unit
def test_scalp_pipeline_builds_and_compiles():
    pipeline = ScalpPipeline(_StubLLM())
    assert pipeline.graph is not None
    node_names = set(pipeline.graph.get_graph().nodes)
    assert {
        "HTF Bias", "tools_htf", "Msg Clear HTF",
        "LTF Structure", "tools_ltf", "Msg Clear LTF",
        "Entry Trigger", "tools_entry",
    } <= node_names
