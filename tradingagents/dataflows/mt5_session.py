"""MT5 terminal connect/init/shutdown lifecycle.

Full design: docs/plans/gold-scalping-mt5/PLAN.md (Phase 2). ``MetaTrader5``
is a Windows-only SDK, imported lazily -- not at module import time -- so
importing this module (or anything downstream of it) doesn't require the
optional ``[mt5]`` extra to be installed, matching
``llm_clients/factory.py``/``bedrock_client.py``'s lazy provider-SDK imports.

Known gotcha (see PLAN.md): MT5's ``copy_rates_*`` return bars in broker
**server** time, not UTC -- ``mt5_vendor.py`` is responsible for correcting
this with ``scalping.mt5_server_utc_offset_hours`` before any UTC-based
session classification runs.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

from .errors import VendorNotConfiguredError

_mt5 = None  # cached MetaTrader5 module, set by mt5_module()


def mt5_module():
    """Lazily import and cache the ``MetaTrader5`` module.

    Raises ``ImportError`` with an install hint when the optional ``[mt5]``
    extra is absent, mirroring ``llm_clients/bedrock_client.py``'s
    ``_bedrock_class()``. Called by ``mt5_vendor.py`` too, so it isn't
    underscore-prefixed despite being an internal helper.
    """
    global _mt5
    if _mt5 is not None:
        return _mt5
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:
        raise ImportError(
            "MT5 integration requires the optional 'MetaTrader5' dependency "
            "(Windows-only, needs a running MT5 terminal). Install it with: "
            'pip install "tradingagents[mt5]"'
        ) from exc
    _mt5 = mt5
    return _mt5


def connect(
    path: str | None = None,
    login: int | None = None,
    password: str | None = None,
    server: str | None = None,
    timeout: int | None = None,
) -> None:
    """Initialize the MT5 terminal connection.

    ``initialize()`` returning False means the terminal isn't running, the
    path/credentials are wrong, or the account rejected login -- an
    environment/config problem, not "no data for this query" -- so it's
    reported as ``VendorNotConfiguredError``, not ``NoMarketDataError``.
    """
    mt5 = mt5_module()
    kwargs = {
        k: v
        for k, v in {
            "path": path,
            "login": login,
            "password": password,
            "server": server,
            "timeout": timeout,
        }.items()
        if v is not None
    }
    if not mt5.initialize(**kwargs):
        code, description = mt5.last_error()
        raise VendorNotConfiguredError(
            f"MT5 terminal initialize() failed ({code}): {description}. "
            "Ensure the MT5 terminal is installed, running, and logged in."
        )


def shutdown() -> None:
    """Shut down the MT5 terminal connection. Safe to call even if never connected."""
    mt5_module().shutdown()


def ensure_symbol(symbol: str) -> None:
    """Ensure ``symbol`` is selected/visible in Market Watch before querying it.

    Distinct failure from ``NoMarketDataError``: this means the *symbol*
    itself isn't available on this broker/account, not that a query for it
    returned nothing.
    """
    mt5 = mt5_module()
    info = mt5.symbol_info(symbol)
    if info is None:
        raise VendorNotConfiguredError(
            f"Symbol {symbol!r} not found on this MT5 broker/account. "
            "Add it in Market Watch or check the symbol name/suffix."
        )
    if not info.visible and not mt5.symbol_select(symbol, True):
        code, description = mt5.last_error()
        raise VendorNotConfiguredError(
            f"Could not add {symbol!r} to Market Watch ({code}): {description}."
        )


@contextlib.contextmanager
def session(
    path: str | None = None,
    login: int | None = None,
    password: str | None = None,
    server: str | None = None,
    timeout: int | None = None,
) -> Iterator[None]:
    """Context manager: ``connect()`` on enter, ``shutdown()`` on exit (even on error)."""
    connect(path=path, login=login, password=password, server=server, timeout=timeout)
    try:
        yield
    finally:
        shutdown()
