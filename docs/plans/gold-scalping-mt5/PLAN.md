# Gold (XAUUSD) MT5 Scalping — Market & Technical Analysis Pipeline

Design reference for the standalone MT5 scalping pipeline. This is the spec;
[TRACKING.md](TRACKING.md) is the phase-by-phase checklist derived from it — update
this file if the design changes, and mirror any scope change into the tracker.
[USAGE.md](USAGE.md) has a quick guide for running the CLI.

## Context

TradingAgents today runs one full LangGraph pass per `(ticker, calendar_date)`: daily-only
OHLCV (Yahoo/Alpha Vantage), a sequential analyst chain, then Bull/Bear debate → Trader →
3-way Risk debate → Portfolio Manager. None of this fits a Gold scalping use case:

- **OHLCV is daily-only by construction**, not just by config. `stockstats_utils.load_ohlcv`,
  `y_finance.py`, and `market_data_validator.py` all key caching and lookups on
  `(symbol, "YYYY-mm-dd")` with a 10-calendar-day staleness guard. There is no
  `timeframe`/`interval` concept anywhere in `AgentState`, `default_config.py`, or any tool
  signature.
- **The graph is a slow batch/debate pipeline**, not a fast trigger loop — wrong latency shape
  for 5-minute entries.
- **XAUUSD is currently misclassified.** `symbol_utils.normalize_symbol` maps it to `GC=F` and
  the CLI's `detect_asset_type` calls it `AssetType.STOCK`, so it would run through the
  Fundamentals analyst and SPY-benchmarked reflection — nonsensical for a commodity/CFD
  instrument, and irrelevant anyway since MT5 will supply real intraday bars directly.
- **No MetaTrader5 integration exists.**

Given this, the goal is a **new, standalone pipeline** — reusing the codebase's existing
patterns (vendor error taxonomy, tool-calling LLM analysts, `bind_structured` structured
output, append-only journaling with atomic writes) without touching `TradingAgentsGraph`, the
daily-equity `AssetType`/`AnalystType` enums, or the debate/risk/execution stages.
**Scope is the Market & Technical Analysis phase only** — the pipeline produces a `ScalpSignal`
(bias, structure, entry, invalidation, targets) as a decision-support artifact. No orders are
placed; execution is a future phase.

Signal reasoning is **LLM-interpreted from deterministically-computed features** — the same
split `market_analyst.py` already uses today (Python computes indicators, the LLM reasons over
them), just extended across three new timeframe-specific analysts instead of one daily one.

## Architecture

```
tradingagents/dataflows/mt5_session.py          # MT5 terminal connect/init/shutdown lifecycle
tradingagents/dataflows/mt5_vendor.py           # copy_rates_* -> OHLCV DataFrame fetch fns
tradingagents/dataflows/scalp_features.py       # deterministic feature extraction (pure fns, no LLM/network)
tradingagents/agents/utils/scalp_schemas.py     # Pydantic: KeyZone, HTFBias, LTFStructure, EntryTrigger, ScalpSignal
tradingagents/agents/utils/scalp_state.py       # ScalpState(MessagesState)
tradingagents/agents/utils/scalp_tools.py       # @tool-wrapped feature snapshots per analyst
tradingagents/agents/analysts/scalp/
    htf_bias_analyst.py                         # Step 1 (4H + 1H combined into one bias call)
    ltf_structure_analyst.py                    # Step 2 (15m)
    entry_trigger_analyst.py                    # Step 3 + 4 (5m entry, SL/TP)
tradingagents/graph/scalp_pipeline.py           # ScalpPipeline: 3-node sequential LangGraph
tradingagents/dataflows/scalp_journal.py        # ScalpJournal (JSONL) + walk-forward outcome resolution
tradingagents/graph/scalp_reflection.py         # ScalpReflector.weekly_review() -> scalp_lessons.md
cli/scalp.py                                    # thin `tradingagents scalp run|review` command group
default_config.py                               # + nested "scalping" block
pyproject.toml                                  # + optional extra `mt5 = ["MetaTrader5>=5.0.45"]`
```

Key deviations from a naive "reuse everything as-is" approach, and why:

- **MT5 is not registered into `interface.py`'s `VENDOR_METHODS`/`route_to_vendor`.** That
  abstraction is built around `(symbol, curr_date)` daily rows; intraday multi-timeframe bars
  don't fit it. `mt5_vendor.py` is called directly by `scalp_tools.py`, but reuses the same
  typed-error taxonomy (`NoMarketDataError`, `VendorNotConfiguredError` from
  `tradingagents/dataflows/errors.py`) so failure handling still reads consistently.
- **`ScalpJournal` uses JSONL, not `TradingMemoryLog`'s markdown format.** `TradingMemoryLog`
  (`tradingagents/agents/utils/memory.py`) assumes one entry per daily decision; a scalping
  journal has many signals/day and needs per-field querying for the weekly bucketing/
  sample-size guardrail, which is impractical to regex out of markdown at that scale. It copies
  `memory.py`'s safety mechanisms exactly: atomic temp-file + `os.replace()` writes, idempotent
  append. `scalp_lessons.md` (human-readable output) stays markdown.
- **3 analysts, not 4.** 4H and 1H are one bias judgment (Step 1), not two independent LLM
  calls — splitting them doubles cost/latency and creates disagreement with no arbiter.

## Step-by-step technical design

### Step 1 — 4H/1H Bias & Key Level Mapping (`scalp_features.py` + `htf_bias_analyst.py`)

Deterministic features computed in Python, handed to the LLM as a formatted snapshot (mirrors
`get_indicators` in `market_analyst.py`):

- **Structure**: fractal/pivot swing detection (`detect_swings`, symmetric N-bar fractal) →
  HH/HL sequence (bullish), LH/LL (bearish), or mixed (range) via `classify_structure`,
  computed independently on 4H and 1H.
- **EMA stack**: EMA50 vs EMA200 relative position + slope on both timeframes
  (`ema_stack_alignment`) — aligned bullish/bearish, or "compressed" (EMAs within X×ATR of each
  other → untradeable range regime).
- **Volatility regime**: current ATR vs its own rolling percentile (`atr_regime`) — low/normal/
  high, also feeds Step 4's SL sizing.
- **Key levels**: prior day/week high-low (`prior_day_week_levels`), round-number psychological
  levels relevant to gold (`round_number_levels`, configurable $ step), optional daily pivot
  points (`daily_pivot_points`, classic PP/R1/S1/R2/S2, gated by config).
- **LLM output** (`HTFBias` schema via `bind_structured`): `bias` (bullish/bearish/range),
  `confidence`, ranked `key_zones: list[KeyZone]`, `rationale`, `invalidation_note` (what would
  flip this bias).

### Step 2 — 15m Trend Alignment & Structure Shift (noise filtering)

- **Alignment gate**: 15m structure must agree with the Step-1 `bias` direction — computed as a
  boolean (`agrees_with_htf`), not left to LLM judgment alone.
- **BOS vs CHoCH** (`detect_bos_choch`): BOS = close beyond the most recent *same-direction*
  swing point by ≥ `min_displacement_atr` × ATR(15m) (continuation, confirms trend). CHoCH =
  close beyond the most recent *opposite-direction* swing point by the same threshold (possible
  reversal). **Close-based only** — a wick-only pierce that closes back inside does not count.
  This displacement threshold is the primary noise filter against fakeout breaks.
- **Retest preference**: prompt instructs preferring entries after a retracement into the
  broken level/order block over chasing the breakout candle.
- **Session filter** (`session_window`): classifies the bar's UTC timestamp into
  asian/london/ny/london_ny_overlap/off_session (default gold-relevant windows: London
  07:00–16:00 UTC, NY 12:00–21:00 UTC, overlap 12:00–16:00 UTC = highest-liquidity).
  Off-session bars are down-weighted/excluded.
- **Volatility filter**: `volatility_regime` flags "low" (chop, skip) or "abnormal_spike" (news
  event, skip unless explicitly trading news).
- **LLM output** (`LTFStructure`): `event` (BOS/CHoCH/none), `event_price`, `displacement_atr`,
  `session`, `volatility_regime`, `confidence` (low/medium/high — the LLM's actual judgment call
  on whether this setup is worth proceeding to Step 3), `rationale`.
- **Tradeable gate** (`tradeable: bool`, `scalp_tools.compute_ltf_tradeable`): computed
  deterministically, not left to LLM judgment alone — hard-skips off-session or low/
  abnormal-spike volatility regardless of confidence, otherwise proceeds to Step 3 only when
  `confidence` meets or exceeds `scalping.min_ltf_confidence` (default `"medium"`). Replaces an
  earlier design where the LLM self-reported `tradeable` directly, which collapsed a "no fresh
  BOS/CHoCH this bar" read straight into a hard skip even when session/volatility/HTF alignment
  were otherwise fine.

### Step 3 — 5m Entry Trigger + Step 4 — Invalidation & Targets (`entry_trigger_analyst.py`)

Trigger patterns computed deterministically, LLM selects/confirms:

- **Liquidity sweep** (`liquidity_sweep`): wick pierces a prior swing high/low by ≥
  `wick_atr_ratio` × ATR(5m) then closes back inside (rejection) — close does *not* confirm the
  break.
- **Order block retest** (`detect_order_blocks`): last opposite-color candle before a
  displacement move (BOS), price returns to its body range.
- **FVG fill** (`detect_fair_value_gaps`): 3-candle imbalance revisited then rejected.
- **EMA pullback + momentum** (`rsi_stoch_momentum`): pullback to fast EMA within a
  micro-trend + RSI/stochastic turn confirming.
- **Confluence gate (the core win-rate + anti-overfitting lever)**: every candidate trigger is
  tagged with its distance (in ATR units) to the nearest Step-1/Step-2 `KeyZone`. A trigger
  with no confluence is rejected — enforced by requiring `EntryTrigger.confluence_zone` to be
  non-null when `triggered=True`.
- **Invalidation (SL)**: beyond the structural point that invalidates the trigger
  (swing/order-block edge) + a small ATR(5m) buffer (`sl_atr_buffer_min`–`sl_atr_buffer_max`,
  e.g. 0.10–0.25×ATR) to avoid wick-outs.
- **Max-SL cap**: if the structural SL distance exceeds `max_sl_atr_multiple` (default
  2.0×ATR(5m)), the setup is rejected rather than force-fit — this is what keeps stops "tight"
  and appropriate for XAUUSD's variable volatility instead of a fixed pip value.
- **Targets**: next Step-1/2 key level as TP1, R-multiple ladder / further HTF level as TP2.
- **Min R:R filter**: reject/flag if `risk_reward_1 < min_risk_reward` (default 1.5).
- **Deterministic verification, not LLM self-policing**: `ScalpPipeline.run()` recomputes
  `risk_reward_1` and the SL-distance check in Python from the LLM's proposed
  `entry_price`/`stop_loss`/`take_profit_1`, and forces `passed_min_rr`/`passed_max_sl = False`
  if the LLM's numbers don't actually pass — same "LLM proposes, Python verifies" philosophy as
  `get_verified_market_snapshot`.

### Step 5 — Weekly learning without overfitting/hallucination

Since execution is out of scope, outcomes are resolved by **walk-forward simulation** against
subsequent real MT5 bars, not live fills:

- `scalp_journal.resolve_outcome()`: fetches 5m bars forward from `generated_at_utc`
  (`mt5_vendor.get_mt5_rates_range`) up to `max_holding_bars_5m` (default 48 = 4h, *not* the
  equity pipeline's 5-day default). Bar-by-bar scan for whichever of SL/TP is hit first (long:
  SL if `low<=stop_loss`, TP if `high>=take_profit_1`; short mirrored).
- **Edge cases**: both hit in the same bar (gap/wide bar) → resolve conservatively as SL hit,
  tagged distinctly if it's a weekend-gap reopen (`gap_through`) vs an intrabar spike, so weekly
  review can separate "bad read" losses from "unavoidable gap" losses; neither hit within the
  window → `timeout` (excluded from win/loss stats, kept visible); no bars available yet →
  `pending`, retried on a later run (same deferred-resolution idiom as
  `TradingAgentsGraph._resolve_pending_entries`).
- **Weekly batch reflection** (`ScalpReflector.weekly_review`), guardrails against
  overfitting/hallucination:
  1. Python buckets resolved losing/timeout signals by fixed categorical keys (setup_type,
     session, HTF/LTF agreement) and counts them *before* any LLM call.
  2. Only buckets with `occurrences >= min_lesson_sample_size` (default 3) become candidate
     lesson material — a single bad trade never becomes a "lesson."
  3. LLM input is restricted to structured journal fields only (never free-text reports, never
     re-fetched data) and must return a structured `WeeklyReviewResult` (via `bind_structured`)
     where each `Lesson` cites `supporting_signal_ids` and an `occurrences` count.
  4. **Post-LLM Python validation**: any `Lesson` whose `supporting_signal_ids` aren't all real
     IDs from that bucket, or whose `occurrences` doesn't match the Python-computed count, is
     dropped with a logged warning — this is the concrete hallucination guard.
  5. `scalp_lessons.md`/`.jsonl` rotates at `max_active_lessons` (default 10, oldest dropped
     first — same idiom as `memory_log_max_entries`), and active lessons are injected into each
     analyst's prompt at pipeline start the same way `past_context` is injected into
     `AgentState` today.

## Config & dependency additions

`default_config.py` — new nested block:

```python
"scalping": {
    "symbol": "XAUUSD",
    "mt5_server_utc_offset_hours": 0,   # must be set correctly per broker — see Gotchas
    "timeframes": {"htf": ["4H", "1H"], "ltf": "15m", "entry": "5m"},
    "sl_atr_buffer_min": 0.10, "sl_atr_buffer_max": 0.25,
    "max_sl_atr_multiple": 2.0,
    "min_risk_reward": 1.5,
    "min_displacement_atr": 0.25,
    "sessions_utc": {"london": [7, 16], "ny": [12, 21]},
    "max_holding_bars_5m": 48,
    "journal_path": "~/.tradingagents/scalp/scalp_journal.jsonl",
    "lessons_path": "~/.tradingagents/scalp/scalp_lessons.md",
    "min_lesson_sample_size": 3,
    "max_active_lessons": 10,
    "use_pivot_points": True,
},
```

`pyproject.toml` — new optional extra mirroring the existing
`bedrock = ["langchain-aws>=1.5.0"]` pattern:

```toml
mt5 = ["MetaTrader5>=5.0.45"]
```

`MetaTrader5` is imported lazily inside `mt5_session.py`/`mt5_vendor.py` (not at package
`__init__`), matching how `llm_clients/factory.py` lazily imports provider SDKs, so a
non-Windows/no-extra install doesn't break.

**Known gotcha to flag explicitly**: MT5's `copy_rates_*` returns bars in **broker server
time**, not UTC. Session-window filtering (Step 2) is meaningless without correcting for this,
hence the explicit `mt5_server_utc_offset_hours` config key — this needs to be set correctly
for the user's actual broker during Phase 2, it can't be hardcoded.

## Phased build order

Each phase is independently verifiable and front-loads risk into pure unit tests. See
[TRACKING.md](TRACKING.md) for the granular checklist.

1. `scalp_features.py` — pure functions, no MT5/LLM dependency.
2. `mt5_session.py` + `mt5_vendor.py` — mocked in automated tests; verified manually against a
   real local MT5 terminal.
3. `scalp_schemas.py` — no dependencies, can be built alongside step 1.
4. `scalp_tools.py` + 3 analysts + `scalp_pipeline.py` — first point real LLM calls happen.
5. `scalp_journal.py` write path.
6. `WalkForwardResolver`.
7. `scalp_reflection.py`.
8. `cli/scalp.py` — wired last.

## Reused patterns (cite, don't reinvent)

- Tool-calling LLM analyst shape: `tradingagents/agents/analysts/market_analyst.py`
- Structured output + fallback: `tradingagents/agents/utils/structured.py` (`bind_structured`,
  `invoke_structured_or_freetext`)
- `MessagesState`-based state + `ToolNode` tool-loop wiring:
  `tradingagents/agents/utils/agent_states.py`, `tradingagents/graph/setup.py`,
  `tradingagents/graph/conditional_logic.py`
- Typed vendor errors: `tradingagents/dataflows/errors.py`
- Atomic append-only log writes + rotation: `tradingagents/agents/utils/memory.py`
  (`TradingMemoryLog`, temp-file + `os.replace()`, `memory_log_max_entries`)
- Deferred/pending resolution across runs:
  `tradingagents/graph/trading_graph.py::_resolve_pending_entries`
- Deterministic-verification-of-LLM-claims:
  `tradingagents/dataflows/market_data_validator.py::build_verified_market_snapshot`
- Optional heavy dependency pattern: `pyproject.toml`'s `bedrock` extra

## Verification

- `pytest tests/test_scalp_features.py tests/test_scalp_walkforward.py tests/test_scalp_journal.py tests/test_scalp_reflection.py -v`
  — pure logic, no external deps, should pass in CI.
- `tests/test_scalp_toolnode.py` — mirrors `test_market_toolnode.py`'s regression guard (every
  tool bound to an analyst's LLM must be registered in that analyst's `ToolNode`).
- Manual verification against a real local MT5 terminal (Windows, terminal running, symbol
  `XAUUSD` in Market Watch): run `tradingagents scalp run --symbol XAUUSD` and inspect the
  printed `ScalpSignal` for a sane bias/structure/entry chain; confirm
  `mt5_server_utc_offset_hours` produces correct session classification against known
  London/NY hours.
- After a few days of accumulated signals, run `tradingagents scalp review` and confirm
  `scalp_lessons.md` only contains lessons backed by ≥3 real occurrences with valid
  `supporting_signal_ids`.
