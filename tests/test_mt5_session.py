"""Tests for the MT5 terminal connect/init/shutdown lifecycle (Phase 2).

See docs/plans/gold-scalping-mt5/PLAN.md and TRACKING.md. No real MT5
terminal is used here -- see mt5_test_utils.py.
"""

from __future__ import annotations

import sys
import types

import pytest

import tradingagents.dataflows.mt5_session as mt5_session
from tradingagents.dataflows.errors import VendorNotConfiguredError

from .mt5_test_utils import FakeMT5


@pytest.fixture
def fake_mt5(monkeypatch):
    fake = FakeMT5()
    monkeypatch.setitem(sys.modules, "MetaTrader5", fake)
    monkeypatch.setattr(mt5_session, "_mt5", None)
    return fake


@pytest.mark.unit
class TestMt5Module:
    def test_raises_helpful_import_error_when_absent(self, monkeypatch):
        monkeypatch.setattr(mt5_session, "_mt5", None)
        monkeypatch.setitem(sys.modules, "MetaTrader5", None)  # force ImportError
        with pytest.raises(ImportError, match=r"mt5"):
            mt5_session.mt5_module()

    def test_caches_module_after_first_import(self, fake_mt5):
        first = mt5_session.mt5_module()
        second = mt5_session.mt5_module()
        assert first is fake_mt5
        assert second is fake_mt5


@pytest.mark.unit
class TestConnect:
    def test_success_calls_initialize_with_only_provided_kwargs(self, fake_mt5):
        mt5_session.connect(login=123, server="Broker-Live")
        assert fake_mt5.calls == [("initialize", {"login": 123, "server": "Broker-Live"})]

    def test_failure_raises_vendor_not_configured_with_last_error_detail(self, fake_mt5):
        fake_mt5.initialize_result = False
        fake_mt5.last_error_value = (-6, "Terminal not found")
        with pytest.raises(VendorNotConfiguredError, match="Terminal not found"):
            mt5_session.connect()


@pytest.mark.unit
class TestShutdown:
    def test_calls_underlying_shutdown(self, fake_mt5):
        mt5_session.shutdown()
        assert ("shutdown", {}) in fake_mt5.calls


@pytest.mark.unit
class TestEnsureSymbol:
    def test_visible_symbol_is_a_noop(self, fake_mt5):
        fake_mt5.symbol_info_value = types.SimpleNamespace(visible=True)
        mt5_session.ensure_symbol("XAUUSD")  # should not raise

    def test_unknown_symbol_raises_vendor_not_configured(self, fake_mt5):
        fake_mt5.symbol_info_value = None
        with pytest.raises(VendorNotConfiguredError, match="not found"):
            mt5_session.ensure_symbol("BOGUS")

    def test_hidden_symbol_is_selected_into_market_watch(self, fake_mt5):
        fake_mt5.symbol_info_value = types.SimpleNamespace(visible=False)
        fake_mt5.symbol_select_result = True
        mt5_session.ensure_symbol("XAUUSD")
        assert ("symbol_select", ("XAUUSD", True)) in fake_mt5.calls

    def test_hidden_symbol_that_fails_to_select_raises(self, fake_mt5):
        fake_mt5.symbol_info_value = types.SimpleNamespace(visible=False)
        fake_mt5.symbol_select_result = False
        fake_mt5.last_error_value = (-5, "Invalid params")
        with pytest.raises(VendorNotConfiguredError, match="Market Watch"):
            mt5_session.ensure_symbol("XAUUSD")


@pytest.mark.unit
class TestSessionContextManager:
    def test_connects_on_enter_and_shuts_down_on_exit(self, fake_mt5):
        with mt5_session.session(login=1):
            assert ("initialize", {"login": 1}) in fake_mt5.calls
            assert ("shutdown", {}) not in fake_mt5.calls
        assert ("shutdown", {}) in fake_mt5.calls

    def test_shuts_down_even_when_body_raises(self, fake_mt5):
        with pytest.raises(RuntimeError), mt5_session.session():
            raise RuntimeError("boom")
        assert ("shutdown", {}) in fake_mt5.calls
