# Plan: 4H Day-Trading Mode

Status: **Phase 0 complete**
Owner: unassigned
Created: 2026-07-08

## Scope decisions (confirmed with user)

1. **Usage pattern**: manual, one 4H bar at a time. The user (re-)invokes the
   CLI/script whenever they want a fresh read; the framework analyzes the
   latest *closed* 4H bar and returns one decision. No scheduler/daemon in
   this phase.
2. **Data source**: resample yfinance 60-minute bars into synthetic 4H
   candles (4 hourly bars -> 1 four-hour candle). No new vendor or API key.
3. **Asset scope**: crypto / 24-7 tickers first (e.g. `BTC-USD`, `ETH-USD`),
   reusing the existing `AssetType.CRYPTO` detection in
   [cli/utils.py:81](../../cli/utils.py). Equity session-calendar alignment
   (regular trading hours, holidays) is an explicit follow-up, not in this
   plan.

## Why this is a real feature, not a config flag

The current framework is date-granular end-to-end: `curr_date` is a plain
`YYYY-mm-dd` string threaded through every tool, `load_ohlcv` in
[tradingagents/dataflows/stockstats_utils.py](../../tradingagents/dataflows/stockstats_utils.py)
downloads only daily bars, indicator lookbacks are counted in days, the
decision log/reflection in `trading_memory.md` scores a decision against
*next day's* return, and report directories are keyed by
`ticker/analysis_date`. 4H day trading needs a parallel intraday path through
data fetch, caching, indicators, agent prompts, graph state, CLI, reporting,
and memory — not just a new interval string.

## Guiding constraint

Every phase must leave `main` runnable in daily/EOD mode exactly as today.
Day-trading is strictly additive: a new `trading_style`/`timeframe` config
path, opt-in via CLI or config, never a change to existing default behavior.

---

## Phase 0 — Design lock-in

**Goal**: pin the few remaining ambiguous decisions before writing code, so
later phases don't thrash.

- [x] Decide the config shape: add `"timeframe": "1d"` (default) /
      `"4h"` to `DEFAULT_CONFIG` in
      [tradingagents/default_config.py](../../tradingagents/default_config.py),
      plus `TRADINGAGENTS_TIMEFRAME` env override following the existing
      `_ENV_OVERRIDES` pattern.
- [x] Decide how "current time" is represented for intraday runs. Recommendation:
      extend `trade_date` to accept `YYYY-mm-dd HH:MM` (space-separated,
      24h clock, UTC) when `timeframe != "1d"`, falling back to date-only
      parsing for daily mode. Document this as the one new user-facing input
      format.
      Extend `--date` / `get_analysis_date()` accordingly, and use a datetime primitive to parse both timestamp and date-only cases.
- [x] Decide the "latest closed bar" rule: given a requested timestamp, the
      4H bar used for analysis is the most recently *completed* 4H candle at
      or before that timestamp (never the still-forming bar) — this is what
      prevents look-ahead/self-referential bias.
- [x] Decide memory/decision-log granularity for intraday: one entry per
      `(ticker, bar_close_timestamp)`, reflection scored against the
      *next* 4H bar's close instead of next calendar day's close.

**Verification**: write the above as a short "Design Notes" section appended
to this file (not code) and re-read it once Phase 1 is done to confirm no
contradictions surfaced.

---

## Phase 1 — Intraday data fetch + resampling to 4H

**Goal**: a tested, cached function that returns clean 4H OHLCV for a crypto
ticker up to a given timestamp, with no look-ahead.

- [x] Add `load_intraday_ohlcv(symbol, curr_datetime, timeframe="4h")` in
      [tradingagents/dataflows/stockstats_utils.py](../../tradingagents/dataflows/stockstats_utils.py)
      (or a new sibling module `intraday.py` if it keeps the diff cleaner),
      modeled on the existing `load_ohlcv`:
      - Fetch 60m bars via `yf.download(..., interval="60m")` — note
        yfinance caps 60m history at ~730 days, so the 5-year window used by
        daily `load_ohlcv` must shrink for intraday.
      - Resample 60m -> 4H with `pandas.DataFrame.resample("4h", origin="epoch")`
        (or explicit anchor, e.g. 00:00/04:00/08:00 UTC boundaries) using
        OHLC-correct aggregation: `Open=first, High=max, Low=min, Close=last,
        Volume=sum`.
      - Filter out the still-forming trailing bar (see Phase 0 rule).
      - Filter rows to `<= curr_datetime` to preserve the existing
        look-ahead-bias guarantee.
      - Apply an analogous `_assert_ohlcv_not_stale` check, scaled for
        intraday (staleness threshold in hours, not the daily 10-day
        constant).
      - Cache to a distinct file name (must include the interval, e.g.
        `{symbol}-YFin-4h-data-{start}-{end}.csv`) — reusing daily's cache
        filename pattern would silently collide/corrupt the daily cache.
  - [x] Reuse `_clean_dataframe`, `_ensure_date_column`, `normalize_symbol`,
        `safe_ticker_component` as-is; do not duplicate their logic.

**Verification**:
- [x] New unit test `tests/test_intraday_resample.py`: feed a synthetic 60m
      DataFrame with known values, assert the resampled 4H OHLCV matches
      hand-computed expected Open/High/Low/Close/Volume for a couple of
      bars.
- [x] New unit test asserting a bar with `timestamp > curr_datetime` (or the
      still-forming bar) never appears in the returned frame (mirrors
      [tests/test_news_lookahead.py](../../tests/test_news_lookahead.py) /
      [tests/test_date_boundaries.py](../../tests/test_date_boundaries.py)
      style).
- [ ] Manual check: call `load_intraday_ohlcv("BTC-USD", "<recent UTC
      timestamp>")` in a REPL/script, print the tail of the frame, and
      confirm timestamps line up on 4H boundaries and the last row is
      actually closed (compare wall-clock time to bar close time).
- [x] `pytest tests/test_intraday_resample.py -v` passes.

---

## Phase 2 — Indicators on 4H bars

**Goal**: `stockstats`-based indicators (RSI, MACD, Bollinger, ATR, etc.)
computable on the 4H frame from Phase 1, with a lookback expressed in bars
(or hours), not calendar days.

- [x] Add an intraday counterpart to `_get_stock_stats_bulk` /
      `get_stock_stats_indicators_window` in
      [tradingagents/dataflows/y_finance.py](../../tradingagents/dataflows/y_finance.py)
      that:
      - Calls `load_intraday_ohlcv` instead of `load_ohlcv`.
      - Keys the per-bar dict by full timestamp string
        (`YYYY-mm-dd HH:MM`) instead of date-only.
      - Accepts `look_back_bars` (e.g. default 60 bars ≈ 10 days of 4H
        crypto bars) instead of `look_back_days`, or keep `look_back_days`
        and convert internally (`bars = look_back_days * 6` for a 24h
        crypto market) — pick one and document it in the docstring.
- [x] Route `get_indicators` (the `@tool` in
      [tradingagents/agents/utils/technical_indicators_tools.py](../../tradingagents/agents/utils/technical_indicators_tools.py))
      to the intraday path when `config["timeframe"] != "1d"`, via
      `route_to_vendor` / the existing `tool_vendors`/`data_vendors`
      dispatch in [tradingagents/dataflows/interface.py](../../tradingagents/dataflows/interface.py)
      — check how `route_to_vendor` currently threads config through before
      deciding whether the interval belongs in the vendor key or as a
      parallel dispatch dimension.

**Verification**:
- [x] Unit test computing `rsi`/`macd` on a known synthetic 4H frame,
      asserting values match a manually verified reference (e.g. compute
      independently with `pandas`/`ta` or by hand for a short series).
- [x] Confirm no regression: existing daily indicator tests
      ([tests/test_stockstats_date_column.py](../../tests/test_stockstats_date_column.py),
      [tests/test_market_toolnode.py](../../tests/test_market_toolnode.py))
      still pass unmodified.
- [x] `pytest tests/ -k "indicator or stockstats" -v` passes.

---

## Phase 3 — Config & CLI plumbing

**Goal**: a user can select "day trading (4H)" in the interactive CLI, or
set it non-interactively, and it reaches the graph as config.

- [x] `DEFAULT_CONFIG["timeframe"] = "1d"` +
      `TRADINGAGENTS_TIMEFRAME` env override (Phase 0).
- [x] CLI: add a `select_trading_style()`/`select_timeframe()` prompt in
      [cli/utils.py](../../cli/utils.py) alongside `select_analysts` /
      `select_research_depth`, offered **only** when
      `detect_asset_type(ticker) == AssetType.CRYPTO` (per the crypto-first
      scope) — for other asset types, keep today's daily-only flow
      unchanged and skip the prompt entirely.
- [x] Update `get_analysis_date()` (Phase 0 datetime format) to accept and
      validate `YYYY-mm-dd HH:MM` when timeframe is `4h`, and keep
      `YYYY-mm-dd` validation unchanged for `1d`.
- [x] Wire the selection into whatever `selections` dict
      [cli/main.py](../../cli/main.py) builds before constructing
      `DEFAULT_CONFIG`/`TradingAgentsGraph`.

**Verification**:
- [x] New test `tests/test_cli_timeframe_selection.py` mirroring
      [tests/test_cli_symbol_handling.py](../../tests/test_cli_symbol_handling.py)
      style: assert the prompt only appears for crypto tickers, assert
      `TRADINGAGENTS_TIMEFRAME=4h` overrides the default per
      [tests/test_env_overrides.py](../../tests/test_env_overrides.py)
      conventions.
- [x] Manual run: `tradingagents` interactive CLI with ticker `BTC-USD`,
      confirm the new prompt appears and a `4h`/`2026-07-08 12:00`-style
      input is accepted; with ticker `AAPL`, confirm the prompt is absent
      and behavior is identical to before this change.
- [x] `pytest tests/test_cli_timeframe_selection.py tests/test_cli_symbol_handling.py tests/test_env_overrides.py -v` passes.

---

## Phase 4 — Graph/state datetime propagation

**Goal**: `trade_date` flows through the LangGraph state as a full
timestamp when in 4H mode, without breaking the daily path or any node that
assumes a bare date.

- [ ] Audit every read of `state["trade_date"]` (start with
      [tradingagents/agents/analysts/market_analyst.py](../../tradingagents/agents/analysts/market_analyst.py),
      then grep the rest of `tradingagents/agents/**` and
      `tradingagents/graph/**`) and confirm each either (a) only needs the
      date portion (safe to `str.split(" ")[0]` at the call site) or (b)
      needs updating to timestamp-aware formatting.
- [ ] Check [tradingagents/graph/propagation.py](../../tradingagents/graph/propagation.py)
      and [tradingagents/graph/checkpointer.py](../../tradingagents/graph/checkpointer.py)
      for any date-based checkpoint keys or `strptime("%Y-%m-%d")` calls
      that would raise on a timestamp string — this is the most likely
      silent-breakage point.
- [ ] Update `get_stock_data`/`get_indicators` tool wrappers in
      [tradingagents/agents/utils/agent_utils.py](../../tradingagents/agents/utils/agent_utils.py)
      to route to the Phase 1/2 intraday functions when
      `state.get("timeframe") == "4h"`.

**Verification**:
- [ ] `pytest tests/test_checkpoint_resume.py tests/test_date_boundaries.py tests/test_analyst_execution.py -v` passes unmodified (daily path unaffected).
- [ ] New integration-style test: build a minimal state dict with
      `trade_date="2026-07-08 12:00"`, `timeframe="4h"`, call the market
      analyst node function directly (mocking the LLM per
      [tests/test_market_toolnode.py](../../tests/test_market_toolnode.py)
      conventions), assert it invokes the intraday tool path and doesn't
      raise on the timestamp format.

---

## Phase 5 — Agent prompts for day-trading semantics

**Goal**: agent reasoning reflects a 4H holding-period mindset, not
"buy-and-hold weeks" framing bleeding through from the daily prompts.

- [ ] Add a day-trading prompt variant (or a conditional paragraph) to
      [tradingagents/agents/analysts/market_analyst.py](../../tradingagents/agents/analysts/market_analyst.py):
      mention the 4H bar, favor faster indicators (10 EMA, RSI, MACD, ATR
      for tight stops) over 200 SMA-style long-horizon framing, and be
      explicit that "today's date" is actually a specific 4H bar close.
- [ ] Update [tradingagents/agents/trader/trader.py](../../tradingagents/agents/trader/trader.py)
      and the risk-management debators in
      [tradingagents/agents/risk_mgmt/](../../tradingagents/agents/risk_mgmt/)
      to size stops/targets in ATR-multiples appropriate for a 4H bar
      (much tighter than a daily-bar ATR stop) and to state the intended
      holding horizon (hours, not weeks) in the final decision text.
- [ ] Leave news/sentiment analysts' day-level lookback as-is for now
      (explicitly out of scope) — note in the report that news context is
      lower-frequency than the trading timeframe, so the trader agent
      doesn't overweight stale-feeling headlines.

**Verification**:
- [ ] Prompt-content unit test (pattern per
      [tests/test_news_analyst_prompt.py](../../tests/test_news_analyst_prompt.py)):
      assert the day-trading prompt variant contains the expected framing
      strings and the daily prompt is unchanged when `timeframe == "1d"`.
- [ ] Manual read-through: run one full graph propagate on `BTC-USD` at a
      recent timestamp in 4H mode with `debug=True`, read the market
      analyst and trader outputs end-to-end, confirm the language and
      numeric stops/targets are 4H-bar-scaled (e.g. stop distances in the
      tens-to-low-hundreds of dollars for BTC, not swing-sized).

---

## Phase 6 — Reporting & memory/reflection

**Goal**: report directories and the persistent decision log
(`trading_memory.md`) work at 4H granularity without colliding with daily
entries for the same ticker/day.

- [ ] Update the `results_dir` path construction in
      [cli/main.py:1021](../../cli/main.py) from
      `ticker/analysis_date` to `ticker/analysis_date_time` (e.g.
      `BTC-USD/2026-07-08_12-00`) when timeframe is `4h`, keeping the
      existing `ticker/YYYY-mm-dd` layout untouched for daily runs.
- [ ] Update the reflection/realized-return logic (memory log writer,
      likely in [tradingagents/agents/utils/memory.py](../../tradingagents/agents/utils/memory.py)
      or [tradingagents/graph/reflection.py](../../tradingagents/graph/reflection.py))
      so that for 4H entries, "realized return" is computed against the
      *next* 4H bar's close rather than next calendar day's close — reuse
      Phase 1's intraday loader to fetch that bar when the entry is later
      revisited.
- [ ] Confirm `~/.tradingagents/memory/trading_memory.md` entries are
      unambiguous about timeframe (e.g. an explicit `Timeframe: 4h` field
      per entry) so a later daily run for the same ticker doesn't get fed
      mismatched-horizon lessons — filter injected memory context by
      matching timeframe.

**Verification**:
- [ ] `pytest tests/test_memory_log.py tests/test_reporting.py -v`, extended
      with new cases for the 4h path, all pass.
- [ ] Manual run: execute two 4H-mode analyses on `BTC-USD` four hours
      apart (or two synthetic timestamps four hours apart against cached
      data), confirm the second run's Portfolio Manager prompt includes a
      reflection on the first run's realized 4H return, and that a
      subsequent **daily** run on `BTC-USD` does not get confused by the
      4H entries (either filtered out or clearly labeled).
- [ ] Inspect the generated report directory tree under `results_dir` and
      confirm no filename collisions between a daily and a 4H run for the
      same ticker/date.

---

## Phase 7 — End-to-end smoke test & docs

**Goal**: a cold user can actually use this, and it's documented.

- [ ] Full run: `tradingagents analyze` (or `python -m cli.main`) against
      `BTC-USD`, selecting 4H day-trading mode, through to a final
      BUY/HOLD/SELL decision, with `--debug` to watch node-by-node
      progress.
- [ ] Add a short "Day Trading (4H)" section to
      [README.md](../../README.md) documenting the new config key, CLI
      flow, crypto-only scope, and the yfinance 60m-history limitation
      (~730 days).
- [ ] Add a `CHANGELOG.md` entry under an "Unreleased"/next version heading.
- [ ] Run the full test suite once to confirm zero regressions:
      `pytest -q`.

**Verification**:
- [ ] `pytest -q` — full suite green.
- [ ] Screenshot or pasted transcript of one full CLI run attached to the
      PR description.
- [ ] Peer/self code review pass via `/code-review` before merge.

---

## Explicit non-goals (this plan)

- Equities/forex session-aware 4H bars (market-hours alignment, holiday
  calendar, EOD flatten rules).
- Automated/scheduled re-running every 4 hours (cron, daemon, background
  loop).
- Any intraday interval other than 4H (e.g. 1H, 15m scalping).
- A paid/alternate intraday data vendor (Alpha Vantage intraday, broker
  feeds) — yfinance 60m resampling only.

## Design Notes (Phase 0 lock-in, 2026-07-08)

These four decisions are binding for Phases 1–7. Re-read after Phase 1 to
confirm no contradictions surfaced.

### 1. Config shape

- `DEFAULT_CONFIG["timeframe"] = "1d"` in
  [tradingagents/default_config.py](../../tradingagents/default_config.py).
  Allowed values: `"1d"` (default, today's behavior) and `"4h"`. Any other
  value raises `ValueError` at startup — consistent with the fail-loud
  philosophy already documented on `_coerce` (a typo like `"4H "` must not
  silently run in daily mode). Validation lives next to `_apply_env_overrides`
  so it covers both the env path and programmatic config dicts; comparison is
  case-insensitive with the canonical lowercase form stored back.
- Env override: `"TRADINGAGENTS_TIMEFRAME": "timeframe"` added to
  `_ENV_OVERRIDES`. No coercion changes needed — the default is a string, so
  `_coerce` passes the value through unchanged.
- The key is named `timeframe` (not `trading_style`): it names the bar
  interval, which is the mechanical fact every downstream branch keys on.
  "Day trading" is CLI presentation copy only.

### 2. "Current time" representation

- `trade_date` remains a **string** end-to-end (state, prompts, memory log) to
  avoid churning every consumer; only its format widens.
  - Daily mode (`timeframe == "1d"`): `YYYY-mm-dd`, validation unchanged.
    Timestamps are **rejected** in daily mode — passing `12:00` to a daily run
    is a user error, not something to silently truncate.
  - 4H mode: canonical format `YYYY-mm-dd HH:MM` (space-separated, 24h clock,
    **UTC** — crypto trades 24/7 so UTC is unambiguous and matches yfinance
    crypto timestamps). Date-only input is also accepted in 4H mode and is
    interpreted as `23:59` of that day, i.e. "the last closed 4H bar of that
    day" — the friendly reading of "analyze July 8".
- One shared parsing helper (new, in `cli/utils.py` or a small
  `tradingagents/dataflows/time_utils.py`), signature
  `parse_trade_datetime(s: str) -> datetime`: try `"%Y-%m-%d %H:%M"` first,
  fall back to `"%Y-%m-%d"`. All later phases call this helper; no scattered
  `strptime("%Y-%m-%d")` calls on the new path.
- `get_analysis_date()` in [cli/utils.py](../../cli/utils.py) gains a
  `timeframe` parameter and widens its regex/validation only when
  `timeframe == "4h"`.

### 3. "Latest closed bar" rule

- 4H bars are anchored on fixed UTC boundaries: opens at 00:00, 04:00, 08:00,
  12:00, 16:00, 20:00 (`resample("4h", origin="epoch")` on UTC timestamps
  yields exactly these). Bars are **labeled by open time** (pandas default,
  `label="left"`), matching how yfinance labels its 60m bars.
- A bar opening at `O` closes at `O + 4h`. Given requested timestamp `T`, the
  analysis bar is the last bar with `close <= T` — closure at exactly `T`
  counts as closed (a 12:00 request at 12:00 sharp analyzes the 08:00–12:00
  bar). Equivalently, when filtering a frame labeled by open time: keep rows
  where `open_label + 4h <= T`.
- `T` is additionally capped by data availability: if the user passes a future
  timestamp, the newest bar yfinance has already closed is used. The
  still-forming bar (present in yfinance output as a partial 60m tail) is
  always dropped **before** the `<= T` filter, so no partially-formed OHLCV
  ever reaches indicators or agents.
- Example: `T = 2026-07-08 13:47` → analysis bar is 08:00–12:00 (labeled
  `2026-07-08 08:00`, close `12:00`). The 12:00–16:00 bar is forming and
  excluded.

### 4. Memory / decision-log granularity

- One log entry per `(ticker, bar_close_timestamp)`. Implementation: for 4H
  entries the `trade_date` slot in the existing
  `[trade_date | ticker | rating | pending]` tag (see
  [tradingagents/agents/utils/memory.py](../../tradingagents/agents/utils/memory.py))
  holds the full `YYYY-mm-dd HH:MM` **bar close** timestamp. The tag format
  itself is unchanged, so existing parse/resolve code keeps working and 4H
  entries can never collide with a daily entry for the same day (daily tags
  have no `HH:MM`).
- Each 4H entry body carries an explicit `Timeframe: 4h` field; daily entries
  stay field-free (implicitly `1d`) for backward compatibility. Memory
  injection filters by matching timeframe so daily runs never ingest 4H
  lessons and vice versa (Phase 6).
- Reflection scoring: a 4H entry's realized return is `next bar close /
  decision bar close - 1`, i.e. scored against the close of the **next 4H
  bar**, fetched via the Phase 1 intraday loader when the entry is resolved
  on a later run.
- **Alpha is omitted for 4H entries** (raw return only, labeled as such).
  The daily path benchmarks against SPY via `_resolve_benchmark` /
  `_fetch_returns` in
  [tradingagents/graph/trading_graph.py](../../tradingagents/graph/trading_graph.py),
  but SPY has no 24/7 4H bars aligned with crypto bars — any equity benchmark
  over a 4-hour crypto window (often outside US market hours) would be noise.
  A crypto-native benchmark (e.g. BTC-USD for alts) is a possible follow-up,
  deliberately out of scope.

---

## Progress log

_(Append a dated bullet here each time a phase is completed, with the commit
hash and any deviations from the plan above.)_

- 2026-07-08 — Phase 0 complete: Design Notes appended above (docs-only, no
  code). Decisions grounded against current `main`: `_ENV_OVERRIDES` string
  coercion needs no changes; `get_analysis_date()` validation widening is
  additive; the memory-log tag format absorbs timestamps without a schema
  change. One addition beyond the original bullets: alpha-vs-benchmark is
  explicitly dropped for 4H entries (raw return only).
- 2026-07-08 — Phase 1 implemented: new module
  `tradingagents/dataflows/intraday.py` (sibling module chosen over growing
  `stockstats_utils.py`) with `load_intraday_ohlcv` / `resample_ohlcv` /
  `_assert_intraday_not_stale`, plus `tests/test_intraday_resample.py`
  (9 tests). Deviations from the plan text: (a) the cache stores the **raw
  60m download**, not resampled 4H candles, so a partial trailing hourly row
  is never frozen into a candle — filename still embeds the timeframe
  (`{sym}-YFin-4h-data-{start}-{boundary}.csv`) per the collision rule;
  (b) the cache filename's end component is the current **closed-bar
  boundary** (not a date), because a date-granular window would serve
  bars frozen at 08:00 to a 16:00 run all day. ⚠️ Test run + manual BTC-USD
  check still pending — the sandbox's command-permission service was down
  when the code landed; run `pytest tests/test_intraday_resample.py -v`
  (plus `pytest -q` for regressions) before ticking the remaining
  verification boxes.
- 2026-07-08 — Phase 2 implemented: intraday indicator path in
  `tradingagents/dataflows/y_finance.py` (`_get_intraday_stock_stats_bulk` +
  `get_intraday_stock_stats_indicators_window`, keyed by full
  `YYYY-mm-dd HH:MM` bar-open timestamps) and a `get_indicators_window`
  dispatcher registered as the yfinance impl for `get_indicators` in
  `interface.py`. Decisions within the plan's open choices: (a) kept the
  tool's `look_back_days` contract, converting internally
  (`bars = look_back_days * 24h/timeframe`, 6/day for 4h) — no tool-signature
  or routing change needed; (b) the interval is **not** a vendor-key /
  dispatch dimension: `route_to_vendor` never threads config into impls
  (vendors read `get_config()` themselves), so the dispatcher reads
  `config.get("timeframe", "1d")` — daily default behavior is byte-identical,
  and the key works before Phase 3 adds it to `DEFAULT_CONFIG`; (c) the
  shared indicator-description dict was hoisted to module-level
  `_INDICATOR_PARAMS` (pure data move, both paths reuse it). Known
  limitation for Phase 3: with `timeframe=4h` and vendor `alpha_vantage`,
  indicators silently stay daily — config validation should reject that
  combo. Tests in `tests/test_intraday_indicators.py` use
  smoothing-agnostic hand-verified references (RSI=100/0 on monotonic
  series, MACD=0 on constant, boll=20-SMA on a linear ramp — formulas
  confirmed against the installed stockstats source). ⚠️ Test run still
  pending — the command-permission service stayed down the whole session
  (same outage as Phase 1); before ticking the verification boxes run:
  `pytest tests/test_intraday_indicators.py tests/test_intraday_resample.py -v`
  then `pytest tests/ -k "indicator or stockstats" -v` and `pytest -q`.
- 2026-07-09 — Phase 1 & 2 verification unblocked and run (command-permission
  outage resolved). Note: system `python3` on this machine is Xcode's bundled
  3.9.6, which fails to import `tradingagents.dataflows.config` (`dict | None`
  syntax needs 3.10+) — use the project's `.venv/bin/python` (3.13.7) instead.
  Results: `pytest tests/test_intraday_indicators.py
  tests/test_intraday_resample.py -v` → 20/20 passed;
  `pytest tests/ -k "indicator or stockstats" -v` → 19/19 passed (includes
  `test_stockstats_date_column.py`); `tests/test_market_toolnode.py` run
  separately → 1/1 passed, unmodified. Full `pytest -q` → 579 passed, 2
  skipped (pre-existing, unrelated: missing `langchain_aws` optional dep, no
  `DEEPSEEK_API_KEY`), zero regressions. Phase 1 and Phase 2 automated
  verification boxes ticked; remaining open items are the manual
  REPL/CLI smoke checks (not yet run in this session).
- 2026-07-09 — Phase 3 implemented: `timeframe` config key ("1d" default) +
  `TRADINGAGENTS_TIMEFRAME` env override in `tradingagents/default_config.py`,
  with `canonicalize_timeframe` / `validate_timeframe` living next to
  `_apply_env_overrides` per the Design Notes (case-insensitive, fail-loud;
  also rejects the Phase 2 known-limitation combo of intraday timeframe +
  an indicator vendor chain that can never reach yfinance). New shared
  parser `tradingagents/dataflows/time_utils.py::parse_trade_datetime`
  (Design Note 2). CLI: `select_timeframe(asset_type)` in `cli/utils.py`
  (crypto-only prompt, env skips it, non-crypto ignores an env-requested 4h
  with a warning) plus `canonicalize_analysis_date(date_str, timeframe)`
  — daily keeps `YYYY-mm-dd`-only validation, 4h also accepts
  `YYYY-mm-dd HH:MM` UTC and reads date-only input as 23:59 of that day.
  Deviation/clarification: the `get_analysis_date()` the interactive flow
  actually uses is the local one in `cli/main.py` (the `cli/utils.py` one was
  dead code for the CLI) — both now take a `timeframe` param and delegate to
  the shared `canonicalize_analysis_date`, so validation logic exists once.
  Future-timestamp rule: a future *date* is rejected in both modes, but a
  future *time* on the current UTC day is allowed (the loader caps at the
  newest closed bar — Design Note 3). Wiring: selection threads through the
  `selections` dict into `_build_run_config`, which sets
  `config["timeframe"]` and re-validates the merged config;
  `TradingAgentsGraph` already does `set_config(self.config)`, so the Phase 2
  dispatcher picks it up with no further plumbing. Tests in
  `tests/test_cli_timeframe_selection.py` (env-override reload conventions
  from `test_env_overrides.py`, prompt gating in `test_cli_symbol_handling.py`
  style, date canonicalization, run-config wiring). ⚠️ Test run pending —
  the sandbox command-permission service was down again when the code
  landed; before ticking the verification boxes run:
  `pytest tests/test_cli_timeframe_selection.py tests/test_cli_symbol_handling.py tests/test_env_overrides.py -v`
  then `pytest -q`, plus the manual BTC-USD / AAPL interactive CLI check.
- 2026-07-09 — Phase 3 verification complete: all automated + manual checks
  passed. Unit test suite (82 tests in
  `test_cli_timeframe_selection.py` + symbol/env suites) ✓; full regression
  suite (613 passed, 2 pre-existing skips, zero failures) ✓; manual CLI:
  BTC-USD (crypto) shows timeframe prompt + accepts 4h env override ✓; AAPL
  (equity) skips prompt, warns on 4h env ✓; date validation (daily rejects
  timestamps, 4h accepts both timestamp and date-only-as-23:59) ✓; config
  threading (timeframe canonicalized and reaches `_build_run_config`) ✓;
  vendor validation (4h rejects non-yfinance indicators) ✓. Indicator
  dispatcher in `y_finance.py::get_indicators_window` correctly routes on
  timeframe. All Phase 3 verification boxes from the plan ticked.
