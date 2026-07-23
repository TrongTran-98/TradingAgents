# Running the scalp CLI

Quick reference for `tradingagents scalp run`/`review`. Full design: [PLAN.md](PLAN.md);
phase-by-phase status: [TRACKING.md](TRACKING.md).

## Prerequisites

- MT5 terminal installed, **running, and logged in** (the CLI attaches to it via
  `MetaTrader5.initialize()` — it does not launch or log in for you).
- Your broker's gold symbol added to Market Watch. It may not be the bare `XAUUSD` — e.g. this
  account's broker (Vantage Markets) lists it as `XAUUSD.sc`. Check with `--symbol` if unsure.
- For local inference: [Ollama](https://ollama.com) running (`ollama serve`, usually already a
  background service) with the model pulled, e.g. `ollama pull qwen3:8b`.

## Run once

```bash
tradingagents scalp run --symbol XAUUSD.sc --llm-provider ollama --llm-model qwen3:8b
```

Omit any flag to fall back to config/env defaults (`scalping.symbol`, `llm_provider`,
`quick_think_llm` in `default_config.py`, or the matching `TRADINGAGENTS_*` env vars). Prints
the HTF Bias / LTF Structure / Entry Trigger chain and appends the resulting signal to the
journal.

## Review (resolve outcomes + weekly reflection)

```bash
tradingagents scalp review --llm-provider ollama --llm-model qwen3:8b
```

Walk-forward resolves any pending (triggered, unresolved) signals against real MT5 bars, then
runs weekly reflection. Safe to run with zero pending signals or zero lesson-eligible
buckets — it just reports that and exits cleanly.

## Checking output / logs

Nothing is written to a dedicated log file — warnings (e.g. a structured-output retry/fallback,
or an MT5 vendor error) print straight to the console. To keep a persistent log, redirect the
run:

```bash
tradingagents scalp run --symbol XAUUSD.sc --llm-provider ollama --llm-model qwen3:8b > scalp_run.log 2>&1
```

Persisted state lives under `~/.tradingagents/scalp/` (paths come from
`scalping.journal_path`/`scalping.lessons_path`):

- `scalp_journal.jsonl` — one JSON record per signal (`ScalpJournal`). Every run's signal is
  appended here, including untriggered "no signal" ones.
- `scalp_lessons.md` / `.jsonl` — validated lessons from `scalp review`'s weekly reflection
  (human-readable / machine-readable, respectively).

```bash
tail -n 1 ~/.tradingagents/scalp/scalp_journal.jsonl   # most recent signal
cat ~/.tradingagents/scalp/scalp_lessons.md            # active lessons
```
