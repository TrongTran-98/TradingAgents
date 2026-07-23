# Tracking — Gold (XAUUSD) MT5 Scalping Pipeline

Full design: [PLAN.md](PLAN.md). Update the status table and check off subtasks as work
lands; keep this file in sync with reality (if scope changes, edit here *and* in PLAN.md).

Status values: `todo` · `in-progress` · `blocked` · `done`

## Status overview

| # | Phase | Key files | Status |
|---|-------|-----------|--------|
| 0 | Scaffolding | `default_config.py`, `pyproject.toml` | done |
| 1 | Deterministic features | `dataflows/scalp_features.py` | done |
| 2 | MT5 integration | `dataflows/mt5_session.py`, `dataflows/mt5_vendor.py` | in-progress |
| 3 | Schemas & state | `agents/utils/scalp_schemas.py`, `agents/utils/scalp_state.py` | done |
| 4 | Analysts & pipeline | `agents/utils/scalp_tools.py`, `agents/analysts/scalp/*`, `graph/scalp_pipeline.py` | done |
| 5 | Journal write path | `dataflows/scalp_journal.py` | done |
| 6 | Walk-forward resolver | `dataflows/scalp_journal.py` (resolver) | done |
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

- [x] `detect_swings(df, n)` — symmetric N-bar fractal swing-high/low detection.
- [x] `classify_structure(swings)` — HH/HL → bullish, LH/LL → bearish, else → range/mixed.
- [x] `ema_stack_alignment(df, fast=50, slow=200)` — relative position + slope; "compressed"
      classification when EMAs are within X×ATR of each other.
- [x] `atr_regime(df)` — current ATR vs its own rolling percentile → low/normal/high.
- [x] `prior_day_week_levels(df)` — prior day/week high-low.
- [x] `round_number_levels(price, step)` — configurable psychological levels for gold.
- [x] `daily_pivot_points(df)` — classic PP/R1/S1/R2/S2, gated by `use_pivot_points` config.
- [x] `detect_bos_choch(df, swings, min_displacement_atr)` — **close-based only**; BOS =
      close beyond most recent same-direction swing by ≥ threshold×ATR, CHoCH = opposite
      direction. Verify wick-only pierces that close back inside do **not** trigger either.
- [x] `session_window(timestamp_utc, sessions_utc)` — asian/london/ny/london_ny_overlap/
      off_session classification.
- [x] `liquidity_sweep(df, swings, wick_atr_ratio)` — wick pierce ≥ ratio×ATR then closes back
      inside; close does not confirm the break.
- [x] `detect_order_blocks(df)` — last opposite-color candle before a displacement/BOS move.
- [x] `detect_fair_value_gaps(df)` — 3-candle imbalance detection + fill/rejection check.
- [x] `rsi_stoch_momentum(df)` — EMA pullback + RSI/stochastic turn confirmation.
- [x] `confluence_distance(price, key_zones)` — ATR-normalized distance from a candidate
      trigger price to the nearest `KeyZone`.

**Unit tests** (`tests/test_scalp_features.py`):
- [x] Swing detection on synthetic fixture DataFrames (known peaks/troughs).
- [x] BOS/CHoCH threshold boundary cases (just under vs. just over `min_displacement_atr`;
      wick-only pierce that closes back inside).
- [x] Order block / FVG / liquidity-sweep detection on constructed candle sequences.
- [x] Session classification across all UTC boundary edges (07:00, 12:00, 16:00, 21:00).
- [x] EMA "compressed" classification threshold.

**Definition of done**: `pytest tests/test_scalp_features.py -v` passes; every function is a
pure function of a DataFrame/params (no I/O, no LLM calls).

---

## Phase 2 — MT5 integration (`mt5_session.py` + `mt5_vendor.py`)

Only phase that needs a real Windows MT5 terminal to fully verify — automated tests use mocks.

- [x] `mt5_session.py`: connect/init/shutdown lifecycle, lazy `import MetaTrader5` (not at
      package `__init__`, matching `llm_clients/factory.py`'s lazy provider-SDK imports).
- [x] `mt5_vendor.py`: `get_mt5_rates(symbol, timeframe, count)` wrapping `copy_rates_from_pos`
      → OHLCV DataFrame.
- [x] `mt5_vendor.py`: `get_mt5_rates_range(symbol, timeframe, date_from, date_to)` wrapping
      `copy_rates_range`, used by the walk-forward resolver.
- [x] Raise `NoMarketDataError` / `VendorNotConfiguredError` (from
      `tradingagents/dataflows/errors.py`) on terminal-not-running / symbol-not-in-Market-Watch
      / empty-result cases — reuse the existing typed-error taxonomy, don't invent new
      exception types.
- [x] Explicitly do **not** register these in `interface.py`'s `VENDOR_METHODS`/
      `route_to_vendor` — called directly by `scalp_tools.py` instead. Note this as an
      intentional deviation, not an oversight, in code comments/PR description.
- [x] Document the **broker-server-time gotcha**: `copy_rates_*` returns bars in broker server
      time, not UTC. Apply `mt5_server_utc_offset_hours` correction before any UTC-based
      session classification (Step 2) runs.

**Tests**:
- [x] Mocked unit tests (no real terminal) for DataFrame shape/column normalization and error
      mapping. (`tests/test_mt5_session.py`, `tests/test_mt5_vendor.py`, 21 tests.)
- [ ] Manual verification against real local MT5 terminal (Windows, terminal running, `XAUUSD`
      in Market Watch) — resolve the actual UTC offset for the user's broker and record it.
      **Outstanding**: needs a live MT5 terminal + broker login, not available in this
      environment — do this before relying on Phase 2 in production.

**Definition of done**: mocked tests pass in CI; manual run against a live terminal returns
correctly-shaped OHLCV for 4H/1H/15m/5m and the correct broker UTC offset is confirmed against
known London/NY session hours.

---

## Phase 3 — Schemas & state (`scalp_schemas.py`, `scalp_state.py`)

No dependencies — can be built in parallel with Phase 1.

- [x] `KeyZone` — price level/zone + label (e.g. "prior day high", "round number", "pivot R1")
      + source timeframe.
- [x] `HTFBias` — `bias` (bullish/bearish/range), `confidence`, `key_zones: list[KeyZone]`,
      `rationale`, `invalidation_note`.
- [x] `LTFStructure` — `event` (BOS/CHoCH/none), `event_price`, `displacement_atr`, `session`,
      `volatility_regime`, `tradeable: bool`, `rationale`.
- [x] `EntryTrigger` — trigger type, `triggered: bool`, `confluence_zone` (non-null required
      when `triggered=True` — enforce with a Pydantic validator, not just a docstring),
      `entry_price`, `stop_loss`, `take_profit_1`, `take_profit_2`, `risk_reward_1`,
      `passed_min_rr`, `passed_max_sl`.
- [x] `ScalpSignal` — top-level artifact: symbol, `generated_at_utc`, `HTFBias`,
      `LTFStructure`, `EntryTrigger`, and a `signal_id` for journal cross-referencing.
- [x] `ScalpState(MessagesState)` in `scalp_state.py` — carries the three-step state across the
      pipeline's LangGraph nodes plus injected active lessons (see Phase 7).
- [x] `WeeklyReviewResult` / `Lesson` schemas (used by Phase 7, define here alongside the rest):
      `Lesson.supporting_signal_ids: list[str]`, `Lesson.occurrences: int`.

**Definition of done**: all schemas import cleanly with no circular deps on
`scalp_features.py`/`scalp_tools.py`; `EntryTrigger`'s confluence-required-when-triggered
invariant is enforced at the model level and covered by a quick construction test.

Implemented in [scalp_schemas.py](../../../tradingagents/agents/utils/scalp_schemas.py) and
[scalp_state.py](../../../tradingagents/agents/utils/scalp_state.py); covered by
[tests/test_scalp_schemas.py](../../../tests/test_scalp_schemas.py) (7 tests, all passing).
`agrees_with_htf`/`passed_min_rr`/`passed_max_sl` are modeled as LLM-fillable fields that
Phase 4's pipeline deterministically overwrites (documented in each schema's docstring), matching
the "LLM proposes, Python verifies" pattern used elsewhere in the design.

---

## Phase 4 — Analysts & pipeline (first real LLM calls)

- [x] `scalp_tools.py` — `@tool`-wrapped feature snapshots per analyst (`get_htf_snapshot`,
      `get_ltf_snapshot`, `get_entry_snapshot`), formatted the same way `market_analyst.py`'s
      `get_indicators` formats output for the LLM. Each tool takes only `symbol`/`as_of_utc`
      (config is read via `get_config()`) and independently recomputes candidate key zones from
      its own fetched bars, so all three stay static, args-only tools a `ToolNode` can register
      once — `get_entry_snapshot` does not receive Step 1/2's key zones as a hidden parameter;
      the LLM cross-references its own freshly-computed candidates against what Step 1/2 already
      said earlier in the conversation.
- [x] `agents/analysts/scalp/htf_bias_analyst.py` — Step 1, combined 4H+1H call, outputs
      `HTFBias` via `bind_structured`. Two-pass node: pass 1 asks the LLM to call
      `get_htf_snapshot` (routed to `tools_htf`); pass 2, once a `ToolMessage` is present, calls
      the structured-output LLM directly over the accumulated messages instead of free text.
- [x] `agents/analysts/scalp/ltf_structure_analyst.py` — Step 2, 15m call, outputs
      `LTFStructure`; computes `agrees_with_htf` gate in Python via
      `scalp_tools.compute_ltf_alignment` (independently re-fetches 15m bars and reclassifies
      structure), overwriting both the schema field and the top-level `ScalpState` field
      regardless of the LLM's own guess.
- [x] `agents/analysts/scalp/entry_trigger_analyst.py` — Steps 3+4, 5m call, outputs
      `EntryTrigger`. Does not self-verify R:R/max-SL — that's `ScalpPipeline.run()`'s job (needs
      a fresh ATR(5m) read independent of the LLM's tool call).
- [x] `graph/scalp_pipeline.py` — `ScalpPipeline`: sequential LangGraph wiring the three
      analysts + `ToolNode`s + message-clearing nodes between stages (mirrors
      `graph/setup.py`'s analyst/tool/clear-node triple). The LTF→Entry edge is gated on
      `LTFStructure.tradeable`; an untradeable read skips Step 3+4 and returns a synthetic
      `triggered=False` `EntryTrigger`.
- [x] `ScalpPipeline.run()` — **deterministic post-LLM verification** via `_verify_entry_trigger`:
      recomputes `risk_reward_1` and the max-SL check in Python from the LLM's proposed
      `entry_price`/`stop_loss`/`take_profit_1` (plus a fresh `get_entry_atr` read), forcing
      `passed_min_rr`/`passed_max_sl = False` if the LLM's numbers don't actually pass (mirrors
      `build_verified_market_snapshot`'s "LLM proposes, Python verifies" pattern).
- [x] Wired the active-lessons injection seam: `ScalpPipeline.run(..., active_lessons=str)` seeds
      `ScalpState.active_lessons`, and all three analyst prompts inject it when non-empty (full
      write path lands in Phase 7).

**Tests**:
- [x] `tests/test_scalp_toolnode.py` — mirrors `test_market_toolnode.py`: every tool bound to
      an analyst's LLM must be registered in that analyst's `ToolNode`.
- [x] `tests/test_scalp_tools.py` — candidate key zones, per-tool vendor-error handling, snapshot
      happy paths, and `compute_ltf_alignment`/`get_entry_atr` in isolation.
- [x] `tests/test_scalp_pipeline_verification.py` — `_verify_entry_trigger` unit tests: good
      R:R/SL, LLM-overclaimed R:R forced False, LLM-overclaimed max-SL forced False, untriggered
      forces both False, missing price fields forces both False, no-ATR forces max-SL False.
- [x] `tests/test_scalp_pipeline_construction.py` — graph builds/compiles against a stub LLM
      (catches node/edge wiring typos at `compile()` time).
- [x] `tests/test_scalp_pipeline_e2e.py` — full `ScalpPipeline.run()` against a scripted fake LLM
      and mocked MT5 bars: exercises the two-pass tool-call/structured node logic, message
      clearing between stages, the tradeable gate, and confirms the pipeline overwrites a
      deliberately-wrong `agrees_with_htf`/`passed_min_rr` claim from the (fake) LLM. Stands in
      for TRACKING's original "debug streaming against historical `as_of_utc`" ask, since that
      needs a real LLM provider.

**Definition of done**: `tests/test_scalp_toolnode.py` passes; a manual end-to-end run against
a historical timestamp produces a `ScalpSignal` with internally consistent
bias→structure→entry reasoning, and a deliberately-bad LLM R:R claim is caught and flagged by
the Python verification step (test this by temporarily forcing a bad LLM output or a unit test
on the verification function in isolation). **Outstanding**: the manual run against a real LLM
provider + historical timestamps hasn't been done in this environment (no live provider call
here); `test_scalp_pipeline_e2e.py`'s scripted-LLM run is the automated substitute and passes.

---

## Phase 5 — Journal write path (`scalp_journal.py`)

- [x] `ScalpJournal` — JSONL-backed, append-only writer for `ScalpSignal` records.
- [x] Atomic writes: temp-file + `os.replace()`, copied from `memory.py`'s `TradingMemoryLog`
      pattern. Deviation: each write uses a uniquely-named tmp file (`.{uuid4}.tmp`, not a fixed
      `.tmp` suffix) plus an in-process `threading.Lock` around read-modify-write, since a fixed
      tmp name and no lock let two threads' `os.replace()` calls race into a Windows
      `PermissionError` (reproduced by the concurrent-write smoke test below). Does not protect
      against a second OS *process* writing concurrently — same gap `TradingMemoryLog` already
      has.
- [x] Idempotent append (same `signal_id` written twice does not duplicate) — checked by
      reading all existing records before writing.
- [x] Journal path resolution from `config["scalping"]["journal_path"]` (expand `~`).
- [x] `read_signals()` — parses the JSONL back into `ScalpSignal` instances (not in the original
      checklist, but needed for idempotency's own read-back and as the read path Phase 6/7 will
      build on).

**Tests** (`tests/test_scalp_journal.py`, 13 tests):
- [x] Write path produces valid JSONL, one record per line.
- [x] Idempotency check (including same-ID-different-content — first write wins).
- [x] Concurrent-write-safety smoke test (`threading`-based, 20 concurrent appends): asserts no
      corrupt lines and no duplicate `signal_id`s land on disk.

**Definition of done**: `pytest tests/test_scalp_journal.py -v` passes (13/13); a Phase-4
pipeline run piped into `ScalpJournal.append()` produces a readable, re-parseable JSONL file —
verified via `read_signals()` round-tripping a full `ScalpSignal` (nested `HTFBias`/
`LTFStructure`/`EntryTrigger`/`KeyZone`) losslessly.

---

## Phase 6 — Walk-forward outcome resolver

- [x] `scalp_journal.resolve_outcome(signal)` — fetch 5m bars forward from
      `generated_at_utc` via `mt5_vendor.get_mt5_rates_range`, up to `max_holding_bars_5m`
      (default 48). Accepts an optional `bars` override so tests (and, later, the CLI) can
      supply an already-fetched frame instead of hitting MT5 again.
- [x] Bar-by-bar scan, first-touch wins: long → SL if `low <= stop_loss`, TP if
      `high >= take_profit_1`; short mirrored (`_scan_bars`; direction inferred from
      `take_profit_1 > entry_price`).
- [x] Same-bar both-hit → resolve conservatively as SL hit.
- [x] Distinguish `gap_through` (weekend-gap reopen) from an ordinary intrabar spike when
      tagging a same-bar SL hit — time gap vs. the previous scanned bar (or `generated_at_utc`
      for the first bar) ≥ 60 minutes tags `gap_through=True`.
- [x] No SL/TP hit within window → `timeout` (excluded from win/loss stats, kept visible in
      journal) — triggered once `max_holding_bars_5m` bars have been scanned, or once
      wall-clock `now` has passed the holding window even with fewer bars (thin
      weekend/holiday history).
- [x] No bars available yet → `pending`, retried on a later run (deferred-resolution idiom
      matching `TradingAgentsGraph._resolve_pending_entries`) — covers both an empty/partial
      `bars` frame before the window has elapsed and a real `NoMarketDataError` from
      `get_mt5_rates_range`.
- [x] `ScalpJournal.update_outcome(signal_id, outcome)` — atomic read-modify-write rewrite of
      one record's `outcome` field in place (same idiom as `TradingMemoryLog.update_with_outcome`),
      plus `ScalpJournal.pending_signals()` (triggered + `outcome.status == "pending"`) as the
      thin read path Phase 8's `review` command will iterate over.
- [x] Added a `SignalOutcome` schema (`scalp_schemas.py`) and an `outcome: SignalOutcome` field
      (default `pending`) on `ScalpSignal`, so every journaled signal carries its resolution
      state directly instead of a side table.

**Tests** (`tests/test_scalp_walkforward.py`, 20 tests):
- [x] Synthetic bar sequences covering: clean SL hit, clean TP hit, same-bar both-hit,
      gap-through same-bar hit, timeout, pending/no-bars-yet.
- [x] Long and short mirrored cases for each scenario above.
- [x] `resolve_outcome` contract guards (raises on an untriggered signal or missing
      entry/stop/TP1 prices) and `ScalpJournal.update_outcome`/`pending_signals` coverage.

**Definition of done**: `pytest tests/test_scalp_walkforward.py -v` passes (20/20) covering every
edge case listed. **Outstanding**: the manual run resolving real previously-journaled signals
against live MT5 history hasn't been done in this environment (no live terminal available) — do
this before relying on Phase 6 in production.

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
