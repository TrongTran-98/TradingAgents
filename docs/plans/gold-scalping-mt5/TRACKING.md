# Tracking — Gold (XAUUSD) MT5 Scalping Pipeline

Full design: [PLAN.md](PLAN.md). Update the status table and check off subtasks as work
lands; keep this file in sync with reality (if scope changes, edit here *and* in PLAN.md).

Status values: `todo` · `in-progress` · `blocked` · `done`

## Status overview

| # | Phase | Key files | Status |
|---|-------|-----------|--------|
| 0 | Scaffolding | `default_config.py`, `pyproject.toml` | done |
| 1 | Deterministic features | `dataflows/scalp_features.py` | done |
| 2 | MT5 integration | `dataflows/mt5_session.py`, `dataflows/mt5_vendor.py` | done |
| 3 | Schemas & state | `agents/utils/scalp_schemas.py`, `agents/utils/scalp_state.py` | done |
| 4 | Analysts & pipeline | `agents/utils/scalp_tools.py`, `agents/analysts/scalp/*`, `graph/scalp_pipeline.py` | done |
| 5 | Journal write path | `dataflows/scalp_journal.py` | done |
| 6 | Walk-forward resolver | `dataflows/scalp_journal.py` (resolver) | done |
| 7 | Weekly reflection | `graph/scalp_reflection.py` | done |
| 8 | CLI | `cli/scalp.py` | done |

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
- [x] Manual verification against real local MT5 terminal (Windows, terminal running, `XAUUSD`
      in Market Watch) — resolve the actual UTC offset for the user's broker and record it.
      **Done** (Phase 8 manual verification, 2026-07-23): this account's broker (Vantage
      Markets, "VantageMarkets-Live 11" server) lists gold as `XAUUSD.sc`, not the bare
      `XAUUSD` config default — confirmed via `mt5.symbols_get()`. Server clock is **UTC+3**,
      confirmed via `symbol_info_tick().time` vs `datetime.now(timezone.utc)`; recorded in
      `default_config.py`'s `scalping.mt5_server_utc_offset_hours` (was `0`, a placeholder — no
      broker had been verified yet). Also found and fixed a real wiring gap while doing this:
      nothing upstream of the CLI ever called `mt5_session.connect()`, so every real MT5 fetch
      failed with `VendorNotConfiguredError` until `cli/scalp.py` was written to open an
      `mt5_session.session()` around the pipeline/resolver calls (Phase 8's job as the final
      wiring layer).

**Definition of done**: mocked tests pass in CI; manual run against a live terminal returns
correctly-shaped OHLCV for 4H/1H/15m/5m and the correct broker UTC offset is confirmed against
known London/NY session hours. **Met** — see the manual-verification note above.

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
      deliberately-wrong `agrees_with_htf`/`passed_min_rr` claim from the (fake) LLM. Also covers
      (added during Phase 8's real-provider verification) a structured-output call that returns
      `None` once or repeatedly, exercising `invoke_structured_with_fallback`'s retry-then-safe-
      default path.

**Definition of done**: `tests/test_scalp_toolnode.py` passes; a manual end-to-end run against
a historical timestamp produces a `ScalpSignal` with internally consistent
bias→structure→entry reasoning, and a deliberately-bad LLM R:R claim is caught and flagged by
the Python verification step (test this by temporarily forcing a bad LLM output or a unit test
on the verification function in isolation). **Met** (Phase 8 manual verification, 2026-07-23):
ran the full pipeline against a real local Ollama `qwen3:8b` model and the live MT5 terminal
(`tradingagents scalp run --symbol XAUUSD.sc --llm-provider ollama --llm-model qwen3:8b`) —
produced an internally consistent `ScalpSignal` (Step 1 "range/low-confidence" bias from
genuinely conflicting 4H/1H reads → Step 2 correctly gated `tradeable=False` on HTF/LTF
disagreement → Step 3+4 correctly skipped rather than forcing an entry). This run also
surfaced a real robustness gap: on longer, reasoning-heavy prompts (the Step 3 entry-trigger
call in particular), qwen3:8b sometimes answers in free-form prose instead of the forced
structured-output tool call — even with `tool_choice` forced — leaving
`with_structured_output(...).invoke()` returning `None` and crashing the analyst node's
`render_*(None)` call. Fixed by adding `invoke_structured_with_fallback` (in
`tradingagents/agents/utils/structured.py`) to all three scalp analysts: retries once, then
falls back to a conservative, schema-valid "no signal" default (`bias="range"` /
`tradeable=False` / `triggered=False` as appropriate) instead of crashing or fabricating a
directional call — covered by new tests in `tests/test_structured_agents.py` and
`tests/test_scalp_pipeline_e2e.py`.

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

- [x] Python bucketing of resolved losing/timeout signals by fixed categorical keys
      (setup_type, session, HTF/LTF agreement), counted **before** any LLM call
      (`bucket_resolved_signals`/`_bucket_key`). Untriggered signals and non-loss/timeout
      outcomes are excluded before bucketing even starts.
- [x] Sample-size gate: only buckets with `occurrences >= min_lesson_sample_size` (default 3)
      become candidate lesson material (`_candidate_buckets`) — below-threshold buckets never
      reach the LLM call at all (verified by an exploding-LLM test).
- [x] LLM input restricted to structured journal fields only (never free-text reports, never
      re-fetched data) — `_build_review_messages` feeds only `signal_id`/`outcome.status`/
      `gap_through`/rationale strings already sitting in the journal record.
- [x] `ScalpReflector.weekly_review()` calls the LLM via `bind_structured` to produce
      `WeeklyReviewResult`, each `Lesson` citing `supporting_signal_ids` + `occurrences`.
- [x] **Post-LLM Python validation**: drop any `Lesson` whose `supporting_signal_ids` aren't
      all real IDs from that bucket, or whose `occurrences` doesn't match the Python-computed
      count; log a warning on drop (`_validate_lessons`). Also drops lessons citing an unknown
      `bucket_key` or an empty `supporting_signal_ids` list.
- [x] Rotation: `scalp_lessons.md`/`.jsonl` capped at `max_active_lessons` (default 10, oldest
      dropped first — same idiom as `memory_log_max_entries`) via `ScalpLessonStore`. The
      `.jsonl` path is derived from the configured `lessons_path` (`.md` -> `.jsonl`, same
      suffix-swap idiom `scalp_journal.py` uses for its own tmp files); `.md` is a rendered
      mirror of the same records, both written atomically (temp-file + `os.replace()`,
      unique-per-call tmp names, in-process lock — mirrors `ScalpJournal`).
- [x] Wire active-lesson injection into each analyst's prompt at pipeline start (completes the
      Phase 4 seam): `load_active_lessons(config)` renders the persisted lesson store into the
      same bullet-list text block each analyst already injects
      (`ScalpPipeline.run(..., active_lessons=load_active_lessons(config))` is the call site;
      wired for real by Phase 8's CLI). Empty string when no lessons are active yet, which each
      analyst already treats as "omit the lessons block."

**Tests** (`tests/test_scalp_reflection.py`, 17 tests, synthetic journal fixtures, independent
of real accumulated volume):
- [x] Sample-size gate rejects buckets below threshold (and never invokes the LLM at all).
- [x] Citation validation drops lessons with fabricated/mismatched `supporting_signal_ids`
      (including an unknown `bucket_key` and an empty citation list).
- [x] Citation validation drops lessons with mismatched `occurrences` counts.
- [x] Rotation drops oldest lessons once `max_active_lessons` is exceeded.
- [x] `ScalpLessonStore` atomic-write coverage (jsonl + markdown both written, no leftover tmp
      files, no-lessons-path no-op) and `load_active_lessons` rendering.

**Definition of done**: `pytest tests/test_scalp_reflection.py -v` passes (17/17). The
end-to-end "`tradingagents scalp review` produces a `scalp_lessons.md`" check needed Phase 8's
CLI wiring, now done (see below) — verified with `scalp review` against a zero-pending,
zero-candidate-bucket journal (this account's first real run produced an untriggered signal,
so there was nothing to resolve/bucket yet); the CLI printed a clean "no new lessons" result
and the correct `scalp_lessons.md` path without crashing. **Still outstanding**: exercising the
non-empty-bucket path (≥3 real losing/timeout signals, real `ScalpReflector.weekly_review`
LLM call) needs a few days of accumulated real signals — re-run `scalp review` once that
volume exists and spot-check the hallucination guard (see cross-cutting checklist).

---

## Phase 8 — CLI (`cli/scalp.py`)

Wired last, once everything underneath it is verified independently.

- [x] `tradingagents scalp run [--symbol XAUUSD]` — runs `ScalpPipeline` once, prints the
      resulting `ScalpSignal`, appends to journal. Also accepts `--llm-provider`/`--llm-model`/
      `--backend-url` overrides (not in the original checklist, but needed to point at a local
      Ollama model without touching global config/env).
- [x] `tradingagents scalp review` — runs `resolve_outcome` over pending journal entries, then
      `ScalpReflector.weekly_review()`, writes `scalp_lessons.md`.
- [x] Thin command group only — no business logic duplicated here; delegates to
      `ScalpPipeline`/`ScalpJournal`/`ScalpReflector`. One addition beyond pure delegation: this
      is also the first (and only) place that calls `mt5_session.session()` around the MT5-
      touching calls — nothing upstream of the CLI ever opened an MT5 session, so every real
      fetch failed until this was wired in here, which is exactly this phase's job as the final
      integration layer.
- [x] `tests/test_scalp_cli.py` (5 tests, `typer.testing.CliRunner`) — mocks
      `create_llm_client`/`ScalpPipeline`/`ScalpJournal`/`resolve_outcome`/`ScalpReflector`/
      `mt5_session.session` so these run with no MT5 terminal and no real LLM: `run`'s happy
      path and CLI-option overrides, a clean error message (not a traceback) on `VendorError`,
      and `review`'s pending-signal resolution + empty-lessons path.

**Definition of done**: manual run against a real local MT5 terminal (Windows, terminal
running, `XAUUSD` in Market Watch) — `tradingagents scalp run --symbol XAUUSD` prints a sane
bias/structure/entry chain end to end. **Met** (2026-07-23): ran
`tradingagents scalp run --symbol XAUUSD.sc --llm-provider ollama --llm-model qwen3:8b`
against this account's live-connected MT5 terminal (Vantage Markets; note the account is a
**live real-money account**, not a demo — every call this pipeline makes is read-only
`copy_rates_*` market data, never `order_send`/order placement) and local Ollama. Produced an
internally consistent signal end to end, then `tradingagents scalp review` ran cleanly against
the resulting journal. This surfaced and fixed two real gaps beyond the CLI file itself (see
Phase 2 and Phase 4 notes above): the missing `mt5_session.connect()` wiring, and qwen3:8b's
occasional structured-output misses on reasoning-heavy prompts.

---

## Cross-cutting acceptance checklist (run once all phases are done)

- [x] `pytest tests/test_scalp_features.py tests/test_scalp_walkforward.py tests/test_scalp_journal.py tests/test_scalp_reflection.py -v`
      passes in CI (pure logic, no external deps).
- [x] `tests/test_scalp_toolnode.py` passes.
- [x] No changes made to `TradingAgentsGraph`, the daily-equity `AssetType`/`AnalystType`
      enums, or the debate/risk/execution stages — confirmed via `git diff`: this work only
      touched `cli/scalp.py` (new), `cli/main.py` (added `app.add_typer`), `default_config.py`
      (the `mt5_server_utc_offset_hours` value), the 3 scalp analyst files + `structured.py`
      (the None-fallback fix), and their tests.
- [x] `mt5_server_utc_offset_hours` verified against the user's actual broker and confirmed
      correct for London/NY session classification — `3` for this account's Vantage Markets
      "VantageMarkets-Live 11" server (see Phase 2 note above).
- [ ] `tradingagents scalp review` output spot-checked for hallucination-guard compliance
      (every lesson has ≥3 occurrences and real signal IDs).
