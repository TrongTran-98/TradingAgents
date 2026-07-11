from typing import Annotated

from langchain_core.tools import tool

from tradingagents.dataflows.interface import route_to_vendor


@tool
def get_stock_data(
    symbol: Annotated[str, "ticker symbol of the company"],
    start_date: Annotated[
        str, "Start date, yyyy-mm-dd (or 'yyyy-mm-dd HH:MM' UTC on intraday runs)"
    ],
    end_date: Annotated[
        str, "End date, yyyy-mm-dd (or 'yyyy-mm-dd HH:MM' UTC on intraday runs)"
    ],
) -> str:
    """
    Retrieve stock price data (OHLCV) for a given ticker symbol.
    Uses the configured core_stock_apis vendor. On intraday runs the rows are
    closed intraday bars and dates may carry an 'HH:MM' (UTC) time component.
    Args:
        symbol (str): Ticker symbol of the company, e.g. AAPL, TSM
        start_date (str): Start date in yyyy-mm-dd format
        end_date (str): End date in yyyy-mm-dd format
    Returns:
        str: A formatted dataframe containing the stock price data for the specified ticker symbol in the specified date range.
    """
    return route_to_vendor("get_stock_data", symbol, start_date, end_date)
