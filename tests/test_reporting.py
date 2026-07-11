"""Report parity: the shared writer produces the report tree for the CLI and the
programmatic API alike (#1037)."""

from types import SimpleNamespace

import pytest

from tradingagents.dataflows.time_utils import filesystem_datetime_tag
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.reporting import write_report_tree


def _state():
    return {
        "market_report": "MKT",
        "news_report": "NEWS",
        "investment_debate_state": {"judge_decision": "RM PLAN"},
        "trader_investment_plan": "TRADE",
        "risk_debate_state": {"judge_decision": "PM DECISION"},
    }


@pytest.mark.unit
def test_write_report_tree_creates_files(tmp_path):
    out = write_report_tree(_state(), "AAPL", tmp_path)
    assert out.name == "complete_report.md"
    assert (tmp_path / "1_analysts" / "market.md").read_text() == "MKT"
    assert (tmp_path / "1_analysts" / "news.md").read_text() == "NEWS"
    assert (tmp_path / "2_research" / "manager.md").read_text() == "RM PLAN"
    assert (tmp_path / "3_trading" / "trader.md").read_text() == "TRADE"
    assert (tmp_path / "5_portfolio" / "decision.md").read_text() == "PM DECISION"
    complete = out.read_text()
    assert "Trading Analysis Report: AAPL" in complete
    assert "MKT" in complete and "PM DECISION" in complete


@pytest.mark.unit
def test_save_reports_explicit_path(tmp_path):
    # Unbound: with an explicit save_path, the method doesn't touch self/config.
    out = TradingAgentsGraph.save_reports(None, _state(), "AAPL", save_path=tmp_path)
    assert (tmp_path / "complete_report.md").exists()
    assert out == tmp_path / "complete_report.md"


@pytest.mark.unit
def test_save_reports_defaults_under_results_dir(tmp_path):
    mock_self = SimpleNamespace(config={"results_dir": str(tmp_path)})
    out = TradingAgentsGraph.save_reports(mock_self, _state(), "AAPL")
    assert out.exists()
    assert out.parent.parent.name == "reports"  # results_dir/reports/AAPL_<stamp>/...
    assert out.parent.name.startswith("AAPL_")


@pytest.mark.unit
def test_results_dir_leaf_daily_vs_4h_no_collision(tmp_path):
    """The CLI builds its run directory as
    ticker/<filesystem_datetime_tag(analysis_date)> (cli/main.py). A 4H run
    and a daily run for the same ticker/day must land in distinct,
    filesystem-safe leaves, with the daily layout byte-identical to before."""
    daily = tmp_path / "BTC-USD" / filesystem_datetime_tag("2026-07-08")
    intraday = tmp_path / "BTC-USD" / filesystem_datetime_tag("2026-07-08 12:00")

    assert daily.name == "2026-07-08"  # daily path untouched
    assert intraday.name == "2026-07-08_12-00"  # plan's Phase 6 layout
    assert daily != intraday
    # No characters that break Windows paths or shell quoting.
    assert ":" not in intraday.name and " " not in intraday.name

    # Both trees can coexist on disk with the CLI's reports/ substructure.
    for leaf in (daily, intraday):
        (leaf / "reports").mkdir(parents=True)
        (leaf / "message_tool.log").touch()
    assert (daily / "reports").is_dir() and (intraday / "reports").is_dir()
