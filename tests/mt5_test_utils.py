"""Shared fake ``MetaTrader5`` module stub for mt5_session/mt5_vendor tests.

MetaTrader5 is a Windows-only SDK not installed in CI, so tests stub
``sys.modules["MetaTrader5"]`` with an object exposing just the subset of the
real API surface ``mt5_session.py``/``mt5_vendor.py`` call.
"""

from __future__ import annotations

import types


class FakeMT5:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240
    TIMEFRAME_D1 = 1440

    def __init__(self):
        self.initialize_result = True
        self.last_error_value = (1, "Success")
        self.symbol_info_value = types.SimpleNamespace(visible=True)
        self.symbol_select_result = True
        self.copy_rates_from_pos_result = None
        self.copy_rates_range_result = None
        self.calls: list[tuple[str, object]] = []

    def initialize(self, **kwargs):
        self.calls.append(("initialize", kwargs))
        return self.initialize_result

    def shutdown(self):
        self.calls.append(("shutdown", {}))

    def last_error(self):
        return self.last_error_value

    def symbol_info(self, symbol):
        self.calls.append(("symbol_info", symbol))
        return self.symbol_info_value

    def symbol_select(self, symbol, enable):
        self.calls.append(("symbol_select", (symbol, enable)))
        return self.symbol_select_result

    def copy_rates_from_pos(self, symbol, timeframe, start_pos, count):
        self.calls.append(("copy_rates_from_pos", (symbol, timeframe, start_pos, count)))
        return self.copy_rates_from_pos_result

    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        self.calls.append(("copy_rates_range", (symbol, timeframe, date_from, date_to)))
        return self.copy_rates_range_result
