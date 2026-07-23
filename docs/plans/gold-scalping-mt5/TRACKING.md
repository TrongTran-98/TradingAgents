# Tracking — Gold (XAUUSD) MT5 Scalping Pipeline

Full design: [PLAN.md](PLAN.md). Update the status table and check off subtasks as work
lands; keep this file in sync with reality (if scope changes, edit here *and* in PLAN.md).

Status values: `todo` · `in-progress` · `blocked` · `done`

## Status overview

| # | Phase | Key files | Status |
|---|-------|-----------|--------|
| 0 | Scaffolding | `default_config.py`, `pyproject.toml` | done |
| 1 | Deterministic features | `dataflows/scalp_features.py` | todo |
| 2 | MT5 integration | `dataflows/mt5_session.py`, `dataflows/mt5_vendor.py` | todo |
| 3 | Schemas & state | `agents/utils/scalp_schemas.py`, `agents/utils/scalp_state.py` | todo |
| 4 | Analysts & pipeline | `agents/utils/scalp_tools.py`, `agents/analysts/scalp/*`, `graph/scalp_pipeline.py` | todo |
| 5 | Journal write path | `dataflows/scalp_journal.py` | todo |
| 6 | Walk-forward resolver | `dataflows/scalp_journal.py` (resolver) | todo |
| 7 | Weekly reflection | `graph/scalp_reflection.py` | todo |
| 8 | CLI | `cli/scalp.py` | todo |

---

## Phase 0 — Scaffolding (config & dependency)

Not a numbered phase in the design's build order, but everything downstream reads from it, so
land it first.

- [x] Add `"scalping": {...}` block to
      [tradingagents/default_config.py](../../../tradingagents/default_config.py) with all
      keys from PLAN.md's "Config & dependency additions" section (`symbol`,
      `mt5_server_utc_offset_hours`, `timeframes`, `sl_atr_buffer_min/max`,
      `max_sl_atr_multiple`, `min_risk_reward`, `min_displacement_atr`, `sessions_utc`,
      `max_holding_bars_5m`, `journal_path`, `lessons_path`, `min_lesson_sample_size`,
      `max_active_lessons`, `use_pivot_points`). Note: `journal_path`/`lessons_path` are built
      from the existing `~/.tradingagents` home via `os.path.join` (already expanded), matching
      the file's `results_dir`/`memory_log_path` idiom rather than literal `~/...` strings.
- [x] Add `mt5 = ["MetaTrader5>=5.0.45"]` optional extra to [pyproject.toml](../../../pyproject.toml),
      mirroring the existing `bedrock` extra.
- [x] Create `tradingagents/agents/analysts/scalp/__init__.py` (new package dir).
- [x] Confirm the `scalping` config block does **not** touch `AssetType`/`AnalystType` enums or
      `TradingAgentsGraph` — this pipeline stays fully additive (diff touches only
      `default_config.py`, `pyproject.toml`, and the new empty package).

**Definition of done**: `import default_config; default_config.DEFAULT_CONFIG["scalping"]`
resolves with all keys; `pip install -e ".[mt5]"` succeeds on a Windows box.

---

## Phase 1 — `scalp_features.py` (pure deterministic functions)

No MT5/LLM dependency. Highest risk of subtle bugs (threshold boundaries), so front-load unit
tests here before anything downstream depends on it.

- [ ] `detect_swings(df, n)` — symmetric N-bar fractal swing-high/low detection.
- [ ] `classify_structure(swings)` — HH/HL → bullish, LH/LL → bearish, else → range/mixed.
- [ ] `ema_stack_alignment(df, fast=50, slow=200)` — relative position + slope; "compressed"
      classification when EMAs are within X×ATR of each other.
- [ ] `atr_regime(df)` — current ATR vs its own rolling percentile → low/normal/high.
- [ ] `prior_day_week_levels(df)` — prior day/week high-low.
- [ ] `round_number_levels(price, step)` — configurable psychological levels for gold.
- [ ] `daily_pivot_points(df)` — classic PP/R1/S1/R2/S2, gated by `use_pivot_points` config.
- [ ] `detect_bos_choch(df, swings, min_displacement_atr)` — **close-based only**; BOS =
      close beyond most recent same-direction swing by ≥ threshold×ATR, CHoCH = opposite
      direction. Verify wick-only pierces that close back inside do **not** trigger either.
- [ ] `session_window(timestamp_utc, sessions_utc)` — asian/london/ny/london_ny_overlap/
      off_session classification.
- [ ] `liquidity_sweep(df, swings, wick_atr_ratio)` — wick pierce ≥ ratio×ATR then closes back
      inside; close does not confirm the break.
- [ ] `detect_order_blocks(df)` — last opposite-color candle before a displacement/BOS move.
- [ ] `detect_fair_value_gaps(df)` — 3-candle imbalance detection + fill/rejection check.
- [ ] `rsi_stoch_momentum(df)` — EMA pullback + RSI/stochastic turn confirmation.
- [ ] `confluence_distance(price, key_zones)` — ATR-normalized distance from a candidate
      trigger price to the nearest `KeyZone`.

**Unit tests** (`tests/test_scalp_features.py`):
- [ ] Swing detection on synthetic fixture DataFrames (known peaks/troughs).
- [ ] BOS/CHoCH threshold boundary cases (just under vs. just over `min_displacement_atr`;
      wick-only pierce that closes back inside).
- [ ] Order block / FVG / liquidity-sweep detection on constructed candle sequences.
- [ ] Session classification across all UTC boundary edges (07:00, 12:00, 16:00, 21:00).
- [ ] EMA "compressed" classification threshold.

**Definition of done**: `pytest tests/test_scalp_features.py -v` passes; every function is a
pure function of a DataFrame/params (no I/O, no LLM calls).

---

## Phase 2 — MT5 integration (`mt5_session.py` + `mt5_vendor.py`)

Only phase that needs a real Windows MT5 terminal to fully verify — automated tests use mocks.

- [ ] `mt5_session.py`: connect/init/shutdown lifecycle, lazy `import MetaTrader5` (not at
      package `__init__`, matching `llm_clients/factory.py`'s lazy provider-SDK imports).
- [ ] `mt5_vendor.py`: `get_mt5_rates(symbol, timeframe, count)` wrapping `copy_rates_from_pos`
      → OHLCV DataFrame.
- [ ] `mt5_vendor.py`: `get_mt5_rates_range(symbol, timeframe, date_from, date_to)` wrapping
      `copy_rates_range`, used by the walk-forward resolver.
- [ ] Raise `NoMarketDataError` / `VendorNotConfiguredError` (from
      `tradingagents/dataflows/errors.py`) on terminal-not-running / symbol-not-in-Market-Watch
      / empty-result cases — reuse the existing typed-error taxonomy, don't invent new
      exception types.
- [ ] Explicitly do **not** register these in `interface.py`'s `VENDOR_METHODS`/
      `route_to_vendor` — called directly by `scalp_tools.py` instead. Note this as an
      intentional deviation, not an oversight, in code comments/PR description.
- [ ] Document the **broker-server-time gotcha**: `copy_rates_*` returns bars in broker server
      time, not UTC. Apply `mt5_server_utc_offset_hours` correction before any UTC-based
      session classification (Step 2) runs.

**Tests**:
- [ ] Mocked unit tests (no real terminal) for DataFrame shape/column normalization and error
      mapping.
- [ ] Manual verification against real local MT5 terminal (Windows, terminal running, `XAUUSD`
      in Market Watch) — resolve the actual UTC offset for the user's broker and record it.

**Definition of done**: mocked tests pass in CI; manual run against a live terminal returns
correctly-shaped OHLCV for 4H/1H/15m/5m and the correct broker UTC offset is confirmed against
known London/NY session hours.

---

## Phase 3 — Schemas & state (`scalp_schemas.py`, `scalp_state.py`)

No dependencies — can be built in parallel with Phase 1.

- [ ] `KeyZone` — price level/zone + label (e.g. "prior day high", "round number", "pivot R1")
      + source timeframe.
- [ ] `HTFBias` — `bias` (bullish/bearish/range), `confidence`, `key_zones: list[KeyZone]`,
      `rationale`, `invalidation_note`.
- [ ] `LTFStructure` — `event` (BOS/CHoCH/none), `event_price`, `displacement_atr`, `session`,
      `volatility_regime`, `tradeable: bool`, `rationale`.
- [ ] `EntryTrigger` — trigger type, `triggered: bool`, `confluence_zone` (non-null required
      when `triggered=True` — enforce with a Pydantic validator, not just a docstring),
      `entry_price`, `stop_loss`, `take_profit_1`, `take_profit_2`, `risk_reward_1`,
      `passed_min_rr`, `passed_max_sl`.
- [ ] `ScalpSignal` — top-level artifact: symbol, `generated_at_utc`, `HTFBias`,
      `LTFStructure`, `EntryTrigger`, and a `signal_id` for journal cross-referencing.
- [ ] `ScalpState(MessagesState)` in `scalp_state.py` — carries the three-step state across the
      pipeline's LangGraph nodes plus injected active lessons (see Phase 7).
- [ ] `WeeklyReviewResult` / `Lesson` schemas (used by Phase 7, define here alongside the rest):
      `Lesson.supporting_signal_ids: list[str]`, `Lesson.occurrences: int`.

**Definition of done**: all schemas import cleanly with no circular deps on
`scalp_features.py`/`scalp_tools.py`; `EntryTrigger`'s confluence-required-when-triggered
invariant is enforced at the model level and covered by a quick construction test.

---

## Phase 4 — Analysts & pipeline (first real LLM calls)

- [ ] `scalp_tools.py` — `@tool`-wrapped feature snapshots per analyst (HTF snapshot, LTF
      snapshot, entry snapshot), formatted the same way `market_analyst.py`'s `get_indicators`
      formats output for the LLM.
- [ ] `agents/analysts/scalp/htf_bias_analyst.py` — Step 1, combined 4H+1H call, outputs
      `HTFBias` via `bind_structured`.
- [ ] `agents/analysts/scalp/ltf_structure_analyst.py` — Step 2, 15m call, outputs
      `LTFStructure`; computes `agrees_with_htf` gate in Python, not LLM judgment.
- [ ] `agents/analysts/scalp/entry_trigger_analyst.py` — Steps 3+4, 5m call, outputs
      `EntryTrigger`.
- [ ] `graph/scalp_pipeline.py` — `ScalpPipeline`: 3-node sequential LangGraph wiring the three
      analysts + `ToolNode`s.
- [ ] `ScalpPipeline.run()` — **deterministic post-LLM verification**: recompute
      `risk_reward_1` and the max-SL check in Python from the LLM's proposed
      `entry_price`/`stop_loss`/`take_profit_1`; force `passed_min_rr`/`passed_max_sl = False`
      if the LLM's numbers don't actually pass (mirrors `build_verified_market_snapshot`'s
      "LLM proposes, Python verifies" pattern).
- [ ] Wire active-lessons injection point into `ScalpState` at pipeline start (full
      implementation lands in Phase 7, but the injection seam should exist here).

**Tests**:
- [ ] `tests/test_scalp_toolnode.py` — mirrors `test_market_toolnode.py`: every tool bound to
      an analyst's LLM must be registered in that analyst's `ToolNode`.
- [ ] End-to-end run against historical `as_of_utc` timestamps with debug streaming (mirroring
      `trading_graph.py`'s debug trace) before trusting output.

**Definition of done**: `tests/test_scalp_toolnode.py` passes; a manual end-to-end run against
a historical timestamp produces a `ScalpSignal` with internally consistent
bias→structure→entry reasoning, and a deliberately-bad LLM R:R claim is caught and flagged by
the Python verification step (test this by temporarily forcing a bad LLM output or a unit test
on the verification function in isolation).

---

## Phase 5 — Journal write path (`scalp_journal.py`)

- [ ] `ScalpJournal` — JSONL-backed, append-only writer for `ScalpSignal` records.
- [ ] Atomic writes: temp-file + `os.replace()`, copied exactly from `memory.py`'s
      `TradingMemoryLog` pattern.
- [ ] Idempotent append (same `signal_id` written twice does not duplicate).
- [ ] Journal path resolution from `config["scalping"]["journal_path"]` (expand `~`).

**Tests** (`tests/test_scalp_journal.py`):
- [ ] Write path produces valid JSONL, one record per line.
- [ ] Idempotency check.
- [ ] Concurrent-write-safety smoke test (atomic replace behavior).

**Definition of done**: `pytest tests/test_scalp_journal.py -v` passes; a Phase-4 pipeline run
piped into `ScalpJournal.append()` produces a readable, re-parseable JSONL file.

---

## Phase 6 — Walk-forward outcome resolver

- [ ] `scalp_journal.resolve_outcome(signal)` — fetch 5m bars forward from
      `generated_at_utc` via `mt5_vendor.get_mt5_rates_range`, up to `max_holding_bars_5m`
      (default 48).
- [ ] Bar-by-bar scan, first-touch wins: long → SL if `low <= stop_loss`, TP if
      `high >= take_profit_1`; short mirrored.
- [ ] Same-bar both-hit → resolve conservatively as SL hit.
- [ ] Distinguish `gap_through` (weekend-gap reopen) from an ordinary intrabar spike when
      tagging a same-bar SL hit.
- [ ] No SL/TP hit within window → `timeout` (excluded from win/loss stats, kept visible in
      journal).
- [ ] No bars available yet → `pending`, retried on a later run (deferred-resolution idiom
      matching `TradingAgentsGraph._resolve_pending_entries`).

**Tests** (`tests/test_scalp_walkforward.py`):
- [ ] Synthetic bar sequences covering: clean SL hit, clean TP hit, same-bar both-hit,
      gap-through same-bar hit, timeout, pending/no-bars-yet.
- [ ] Long and short mirrored cases for each scenario above.

**Definition of done**: `pytest tests/test_scalp_walkforward.py -v` passes covering every edge
case listed; a manual run resolves real previously-journaled signals against live MT5 history
without error.

---

## Phase 7 — Weekly reflection (`scalp_reflection.py`)

Guardrails against overfitting/hallucination are the point of this phase — implement the
Python-side checks *before* wiring the LLM call, not after.

- [ ] Python bucketing of resolved losing/timeout signals by fixed categorical keys
      (setup_type, session, HTF/LTF agreement), counted **before** any LLM call.
- [ ] Sample-size gate: only buckets with `occurrences >= min_lesson_sample_size` (default 3)
      become candidate lesson material.
- [ ] LLM input restricted to structured journal fields only (never free-text reports, never
      re-fetched data).
- [ ] `ScalpReflector.weekly_review()` calls the LLM via `bind_structured` to produce
      `WeeklyReviewResult`, each `Lesson` citing `supporting_signal_ids` + `occurrences`.
- [ ] **Post-LLM Python validation**: drop any `Lesson` whose `supporting_signal_ids` aren't
      all real IDs from that bucket, or whose `occurrences` doesn't match the Python-computed
      count; log a warning on drop.
- [ ] Rotation: `scalp_lessons.md`/`.jsonl` capped at `max_active_lessons` (default 10, oldest
      dropped first — same idiom as `memory_log_max_entries`).
- [ ] Wire active-lesson injection into each analyst's prompt at pipeline start (completes the
      Phase 4 seam), the same way `past_context` is injected into `AgentState` today.

**Tests** (`tests/test_scalp_reflection.py`, synthetic journal fixtures, independent of real
accumulated volume):
- [ ] Sample-size gate rejects buckets below threshold.
- [ ] Citation validation drops lessons with fabricated/mismatched `supporting_signal_ids`.
- [ ] Citation validation drops lessons with mismatched `occurrences` counts.
- [ ] Rotation drops oldest lessons once `max_active_lessons` is exceeded.

**Definition of done**: `pytest tests/test_scalp_reflection.py -v` passes; after a few days of
accumulated real signals, `tradingagents scalp review` produces a `scalp_lessons.md` containing
only lessons backed by ≥3 real occurrences with valid `supporting_signal_ids`.

---

## Phase 8 — CLI (`cli/scalp.py`)

Wired last, once everything underneath it is verified independently.

- [ ] `tradingagents scalp run [--symbol XAUUSD]` — runs `ScalpPipeline` once, prints the
      resulting `ScalpSignal`, appends to journal.
- [ ] `tradingagents scalp review` — runs `resolve_outcome` over pending journal entries, then
      `ScalpReflector.weekly_review()`, writes `scalp_lessons.md`.
- [ ] Thin command group only — no business logic duplicated here; delegates to
      `ScalpPipeline`/`ScalpJournal`/`ScalpReflector`.

**Definition of done**: manual run against a real local MT5 terminal (Windows, terminal
running, `XAUUSD` in Market Watch) — `tradingagents scalp run --symbol XAUUSD` prints a sane
bias/structure/entry chain end to end.

---

## Cross-cutting acceptance checklist (run once all phases are done)

- [ ] `pytest tests/test_scalp_features.py tests/test_scalp_walkforward.py tests/test_scalp_journal.py tests/test_scalp_reflection.py -v`
      passes in CI (pure logic, no external deps).
- [ ] `tests/test_scalp_toolnode.py` passes.
- [ ] No changes made to `TradingAgentsGraph`, the daily-equity `AssetType`/`AnalystType`
      enums, or the debate/risk/execution stages — confirm with `git diff` scoped to those
      files before merging.
- [ ] `mt5_server_utc_offset_hours` verified against the user's actual broker and confirmed
      correct for London/NY session classification.
- [ ] `tradingagents scalp review` output spot-checked for hallucination-guard compliance
      (every lesson has ≥3 occurrences and real signal IDs).
