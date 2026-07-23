"""@tool-wrapped deterministic feature snapshots for the gold-scalping analysts (Phase 4).

Full design: docs/plans/gold-scalping-mt5/PLAN.md. Mirrors
``technical_indicators_tools.py``'s ``get_indicators``: the LLM calls a tool,
Python computes the deterministic features (``scalp_features.py``, Phase 1)
from real bars (``mt5_vendor.py``, Phase 2), and the tool returns a formatted
text snapshot the LLM reasons over -- it never invents structure/EMA/ATR
readings or price levels itself.

Each tool takes only ``symbol``/``as_of_utc`` (config -- offset, thresholds,
sessions -- is read from ``get_config()``), so the three tools stay static,
args-only functions a ``ToolNode`` can register once at pipeline build time
(``scalp_pipeline.py``'s ``_create_tool_nodes``, mirroring
``TradingAgentsGraph._create_tool_nodes``). In particular, ``get_entry_snapshot``
does *not* receive Step 1/2's chosen key zones as a hidden parameter --  it
independently recomputes the same deterministic candidate levels (pure
functions of the same historical bars), and the LLM cross-references them
against what Step 1/2 already said in the conversation.

``compute_ltf_alignment``/``get_entry_atr`` are plain (non-``@tool``) helpers
used by the analyst/pipeline code for *deterministic* verification --
"LLM proposes, Python verifies", the same philosophy as
``market_data_validator.build_verified_market_snapshot``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated

import pandas as pd
from langchain_core.tools import tool

from tradingagents.dataflows import mt5_vendor, scalp_features as sf
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorError

_TIMEFRAME_MINUTES: dict[str, int] = {"5m": 5, "15m": 15, "1H": 60, "4H": 240}

# Bar counts sized for the deepest indicator each timeframe feeds:
# ema_stack_alignment's EMA200 + slope lookback is the binding constraint for
# 4H/1H; 15m/5m just need enough recent history for swings/BOS/momentum.
_HTF_BARS = 260
_LTF_BARS = 200
_ENTRY_BARS = 300
_ENTRY_KEY_ZONE_BARS = 200

# Gold-relevant psychological level spacing. XAUUSD trades in the thousands,
# so whole-dollar levels are noise; $10 increments are the levels
# retail/institutional flow actually reacts to.
_ROUND_NUMBER_STEP = 10.0


def _parse_as_of(as_of_utc: str) -> datetime:
    dt = datetime.fromisoformat(as_of_utc)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _fetch_frame(symbol: str, timeframe: str, as_of: datetime, bars: int, utc_offset_hours: float) -> pd.DataFrame:
    """Fetch the most recent ``bars`` bars up to ``as_of`` (UTC) for ``timeframe``.

    Requests a 3x-wide window before trimming to ``bars`` -- generous enough
    to absorb weekend/holiday closures without under-filling the requested
    count.
    """
    minutes = _TIMEFRAME_MINUTES[timeframe]
    date_from = as_of - timedelta(minutes=minutes * bars * 3)
    df = mt5_vendor.get_mt5_rates_range(symbol, timeframe, date_from, as_of, utc_offset_hours)
    return df.tail(bars).reset_index(drop=True)


def _candidate_key_zones(df: pd.DataFrame, config: dict) -> list[dict]:
    """Deterministic candidate key-level zones: prior day/week levels, round numbers, pivots.

    Independently recomputed by each snapshot tool from its own fetched
    bars -- pure functions of the same historical data, so no hidden
    coupling to another analyst's output is needed (see module docstring).
    """
    zones: list[dict] = []
    levels = sf.prior_day_week_levels(df)
    if levels.prior_day_high is not None:
        zones.append({"label": "prior day high", "price": levels.prior_day_high, "source_timeframe": "1D"})
        zones.append({"label": "prior day low", "price": levels.prior_day_low, "source_timeframe": "1D"})
        zones.append({"label": "prior day close", "price": levels.prior_day_close, "source_timeframe": "1D"})
    if levels.prior_week_high is not None:
        zones.append({"label": "prior week high", "price": levels.prior_week_high, "source_timeframe": "1W"})
        zones.append({"label": "prior week low", "price": levels.prior_week_low, "source_timeframe": "1W"})

    if not df.empty:
        current_price = float(df["close"].iloc[-1])
        for level in sf.round_number_levels(current_price, step=_ROUND_NUMBER_STEP):
            zones.append({"label": "round number", "price": level, "source_timeframe": "n/a"})

    if config.get("use_pivot_points", True):
        pivots = sf.daily_pivot_points(df)
        if pivots is not None:
            for label, price in (
                ("pivot PP", pivots.pp), ("pivot R1", pivots.r1), ("pivot S1", pivots.s1),
                ("pivot R2", pivots.r2), ("pivot S2", pivots.s2),
            ):
                zones.append({"label": label, "price": price, "source_timeframe": "1D"})
    return zones


def _format_zones(zones: list[dict]) -> str:
    if not zones:
        return "  (none available)"
    return "\n".join(f"  - {z['price']} -- {z['label']} ({z['source_timeframe']})" for z in zones)


def _format_swings(swings, limit: int = 6) -> str:
    if not swings:
        return "  (none detected)"
    return "\n".join(f"  - {s.kind} {s.price} at index {s.index}" for s in swings[-limit:])


@tool
def get_htf_snapshot(
    symbol: Annotated[str, "instrument symbol, e.g. XAUUSD"],
    as_of_utc: Annotated[str, "ISO-8601 UTC timestamp to anchor the snapshot to"],
) -> str:
    """Deterministic 4H+1H structure, EMA stack, ATR regime, and candidate key levels.

    Call this once before producing the HTF bias call -- it is the source of
    truth for structure/EMA/ATR/key-level claims; do not invent a level or
    reading it doesn't report.
    """
    config = get_config()["scalping"]
    utc_offset = config["mt5_server_utc_offset_hours"]
    as_of = _parse_as_of(as_of_utc)

    try:
        df_4h = _fetch_frame(symbol, "4H", as_of, _HTF_BARS, utc_offset)
        df_1h = _fetch_frame(symbol, "1H", as_of, _HTF_BARS, utc_offset)
    except VendorError as exc:
        return f"<error fetching HTF bars for {symbol}>: {exc}"

    lines = [f"## HTF snapshot for {symbol.upper()} as of {as_of_utc}", ""]
    for label, df in (("4H", df_4h), ("1H", df_1h)):
        swings = sf.detect_swings(df, n=2)
        structure = sf.classify_structure(swings)
        ema_stack = sf.ema_stack_alignment(df)
        atr = sf.atr_regime(df)
        ema_desc = "n/a (insufficient history)"
        if ema_stack is not None:
            ema_desc = (
                f"{ema_stack.alignment} (fast={ema_stack.fast}, slow={ema_stack.slow}, "
                f"fast_slope={ema_stack.fast_slope}, slow_slope={ema_stack.slow_slope})"
            )
        atr_desc = "n/a"
        if atr is not None:
            atr_desc = f"{atr.regime} (atr={atr.atr}, percentile={atr.percentile})"
        lines += [
            f"### {label}",
            f"- Structure: {structure}",
            f"- EMA50/200: {ema_desc}",
            f"- ATR regime: {atr_desc}",
            f"- Recent swings:\n{_format_swings(swings)}",
            "",
        ]

    zones = _candidate_key_zones(df_1h, config)
    lines += ["### Candidate key levels (pick from these -- do not invent levels)", _format_zones(zones)]
    return "\n".join(lines)


@tool
def get_ltf_snapshot(
    symbol: Annotated[str, "instrument symbol, e.g. XAUUSD"],
    as_of_utc: Annotated[str, "ISO-8601 UTC timestamp to anchor the snapshot to"],
) -> str:
    """Deterministic 15m structure, BOS/CHoCH event, session, and volatility regime.

    Call this once before producing the LTF structure call -- it is the
    source of truth for the event/session/ATR readings; do not invent one.
    """
    config = get_config()["scalping"]
    utc_offset = config["mt5_server_utc_offset_hours"]
    as_of = _parse_as_of(as_of_utc)

    try:
        df = _fetch_frame(symbol, "15m", as_of, _LTF_BARS, utc_offset)
    except VendorError as exc:
        return f"<error fetching 15m bars for {symbol}>: {exc}"

    swings = sf.detect_swings(df, n=2)
    structure = sf.classify_structure(swings)
    event = sf.detect_bos_choch(df, swings, min_displacement_atr=config["min_displacement_atr"])
    atr = sf.atr_regime(df)
    session = sf.session_window(as_of, config["sessions_utc"])

    event_desc = event.kind
    if event.kind != "none":
        event_desc += (
            f" ({event.direction}, close={event.close_price}, "
            f"broken_level={event.broken_level}, displacement={event.displacement_atr} ATR)"
        )
    atr_desc = "n/a"
    if atr is not None:
        atr_desc = f"{atr.regime} (atr={atr.atr}, percentile={atr.percentile})"

    lines = [
        f"## LTF (15m) snapshot for {symbol.upper()} as of {as_of_utc}",
        "",
        f"- Structure: {structure}",
        f"- Event: {event_desc}",
        f"- Session: {session}",
        f"- ATR regime: {atr_desc}",
        f"- Latest close: {float(df['close'].iloc[-1]) if not df.empty else 'n/a'}",
    ]
    return "\n".join(lines)


@tool
def get_entry_snapshot(
    symbol: Annotated[str, "instrument symbol, e.g. XAUUSD"],
    as_of_utc: Annotated[str, "ISO-8601 UTC timestamp to anchor the snapshot to"],
) -> str:
    """Deterministic 5m entry-trigger candidates with confluence distance to key levels.

    Covers liquidity sweep, order-block retest, FVG fill, and EMA
    pullback + momentum candidates. Call this once before producing the
    entry trigger call.
    """
    config = get_config()["scalping"]
    utc_offset = config["mt5_server_utc_offset_hours"]
    as_of = _parse_as_of(as_of_utc)

    try:
        df = _fetch_frame(symbol, "5m", as_of, _ENTRY_BARS, utc_offset)
        df_1h = _fetch_frame(symbol, "1H", as_of, _ENTRY_KEY_ZONE_BARS, utc_offset)
    except VendorError as exc:
        return f"<error fetching 5m bars for {symbol}>: {exc}"

    swings = sf.detect_swings(df, n=2)
    sweep = sf.liquidity_sweep(df, swings, wick_atr_ratio=0.25)
    blocks = sf.detect_order_blocks(df)
    gaps = [g for g in sf.detect_fair_value_gaps(df) if not g.filled]
    momentum = sf.rsi_stoch_momentum(df)
    atr = sf.atr_regime(df)
    atr_value = atr.atr if atr is not None else None
    zones = _candidate_key_zones(df_1h, config)

    def _dist(price: float) -> str:
        d = sf.confluence_distance(price, zones, atr_value)
        return f"{d} ATR" if d is not None else "n/a"

    lines = [
        f"## Entry (5m) snapshot for {symbol.upper()} as of {as_of_utc}",
        "",
        f"- ATR(5m): {atr_value if atr_value is not None else 'n/a'}",
        f"- Latest close: {float(df['close'].iloc[-1]) if not df.empty else 'n/a'}",
        "",
        "### Liquidity sweep",
    ]
    if sweep.detected:
        lines.append(
            f"- Detected: {sweep.direction} sweep of {sweep.swept_level} "
            f"(wick={sweep.wick_atr} ATR); confluence distance: {_dist(sweep.swept_level)}"
        )
    else:
        lines.append("- None detected on the latest bar.")

    lines.append("")
    lines.append("### Order blocks (most recent first)")
    if blocks:
        for b in blocks[-3:][::-1]:
            mid = (b.open + b.close) / 2
            lines.append(
                f"- {b.direction} order block body [{min(b.open, b.close)}, {max(b.open, b.close)}]; "
                f"confluence distance: {_dist(mid)}"
            )
    else:
        lines.append("- None detected.")

    lines.append("")
    lines.append("### Unfilled fair value gaps (most recent first)")
    if gaps:
        for g in gaps[-3:][::-1]:
            mid = (g.gap_low + g.gap_high) / 2
            lines.append(f"- {g.direction} FVG [{g.gap_low}, {g.gap_high}]; confluence distance: {_dist(mid)}")
    else:
        lines.append("- None unfilled.")

    lines.append("")
    lines.append("### Momentum (RSI/stochastic/EMA pullback)")
    if momentum is not None:
        lines.append(
            f"- RSI={momentum.rsi}, Stoch %K={momentum.stoch_k}, %D={momentum.stoch_d}, "
            f"EMA21={momentum.ema}, price_at_ema={momentum.price_at_ema}, "
            f"momentum_turn={momentum.momentum_turn}"
        )
    else:
        lines.append("- n/a (insufficient history)")

    lines += ["", "### Candidate key levels (for confluence reference)", _format_zones(zones)]
    lines += [
        "",
        "### Config thresholds",
        f"- min_risk_reward: {config['min_risk_reward']}",
        f"- max_sl_atr_multiple: {config['max_sl_atr_multiple']}",
        f"- sl_atr_buffer range: {config['sl_atr_buffer_min']}-{config['sl_atr_buffer_max']} x ATR(5m)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deterministic verification helpers (not LLM-facing) -- "LLM proposes,
# Python verifies", same philosophy as market_data_validator.py.
# ---------------------------------------------------------------------------


def compute_ltf_alignment(symbol: str, as_of_utc: str, htf_bias_direction: str) -> bool:
    """Python-truth 15m/HTF alignment gate (Step 2's "computed as a boolean, not left to LLM judgment alone").

    Independently re-fetches 15m bars and reclassifies structure; returns
    True only when 15m structure matches ``htf_bias_direction`` exactly. A
    'range' HTF bias never agrees -- there is no direction to agree with.
    Returns False (rather than raising) on a vendor error, since this is a
    gate, not a required read.
    """
    if htf_bias_direction not in ("bullish", "bearish"):
        return False
    config = get_config()["scalping"]
    as_of = _parse_as_of(as_of_utc)
    try:
        df = _fetch_frame(symbol, "15m", as_of, _LTF_BARS, config["mt5_server_utc_offset_hours"])
    except VendorError:
        return False
    swings = sf.detect_swings(df, n=2)
    structure = sf.classify_structure(swings)
    return structure == htf_bias_direction


def get_entry_atr(symbol: str, as_of_utc: str) -> float | None:
    """Current ATR(5m) for ``symbol`` at ``as_of_utc``.

    Used by ``ScalpPipeline``'s deterministic SL/R:R verification, not by
    the LLM directly (the entry snapshot already surfaces ATR(5m) as
    context). Returns ``None`` on a vendor error or insufficient history.
    """
    config = get_config()["scalping"]
    as_of = _parse_as_of(as_of_utc)
    try:
        df = _fetch_frame(symbol, "5m", as_of, _ENTRY_BARS, config["mt5_server_utc_offset_hours"])
    except VendorError:
        return None
    atr = sf.atr_regime(df)
    return atr.atr if atr is not None else None
