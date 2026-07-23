"""Regression guard: every scalp snapshot tool must be registered in its
analyst's ToolNode (mirrors test_market_toolnode.py's wiring-gap guard).
"""
import pytest

from tradingagents.graph.scalp_pipeline import ScalpPipeline


@pytest.mark.unit
def test_scalp_toolnodes_register_all_snapshot_tools():
    # _create_tool_nodes is a staticmethod that builds no LLMs -> call directly.
    nodes = ScalpPipeline._create_tool_nodes()
    assert "get_htf_snapshot" in nodes["htf"].tools_by_name
    assert "get_ltf_snapshot" in nodes["ltf"].tools_by_name
    assert "get_entry_snapshot" in nodes["entry"].tools_by_name
