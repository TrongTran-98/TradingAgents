# How to Run an Analysis

Quick reference for invoking TradingAgents. For full installation, API key
setup, and the 4H day-trading feature details, see the
[README "Installation and CLI" section](../../README.md#installation-and-cli).

## 1. Prerequisites

- Dependencies installed (`pip install .`) and a virtualenv activated, or use
  the project's `.venv` directly (`.venv/bin/python`).
- At least one LLM provider API key set (env var or `.env`), e.g.
  `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `OPENROUTER_API_KEY`. See the README
  for the full list and for local/enterprise providers (Ollama, Bedrock,
  Azure, OpenAI-compatible servers).

## 2. Launch the interactive CLI

```bash
tradingagents                  # installed console script (needs the venv active on PATH)
.venv/bin/tradingagents        # equivalent, no activation needed
python -m cli.main             # equivalent, run from source
.venv/bin/python -m cli.main   # equivalent, using the project venv directly
```

There is no subcommand — `python -m cli.main analyze` is **not** valid and
will fail with "unexpected extra argument(s)". The command itself launches
the interactive flow.

> **`zsh: command not found: tradingagents`?** The console script only
> exists on `PATH` inside the venv it was installed into. Either activate
> that venv first (`source .venv/bin/activate`, then plain `tradingagents`
> works), or skip activation and call the binary directly:
> `.venv/bin/tradingagents`. If neither exists yet, install with
> `pip install .` inside the venv.

## 3. Prompts you'll see, in order

1. **Ticker symbol** — any Yahoo Finance ticker (`AAPL`, `BTC-USD`,
   `0700.HK`, ...).
2. **Trading Timeframe** *(crypto tickers only)* — Daily (default) or Day
   trading (4H bars). Non-crypto tickers skip this prompt and stay
   daily-only.
3. **Analysis date** — `YYYY-MM-DD` for daily mode; in 4H mode also accepts
   a UTC timestamp `YYYY-MM-DD HH:MM` (date-only means "last closed 4H bar
   of that day").
4. **Output language**.
5. **Analysts to include** — space to toggle, `a` to select all, enter to
   confirm.
6. **Research depth** — Shallow / Medium / Deep (debate rounds).
7. **LLM provider / models** — skipped if `TRADINGAGENTS_LLM_PROVIDER`,
   `TRADINGAGENTS_DEEP_THINK_LLM`, and `TRADINGAGENTS_QUICK_THINK_LLM` are
   already set (via `.env` or the shell environment).

The graph then runs live, showing node-by-node progress (this is the debug
view — there's no separate `--debug` flag). At the end you're offered a save
path for the report tree.

## 4. Non-interactive / scripted runs

For scripted or CI-style runs (e.g. smoke-testing a change), drive the same
prompts programmatically with `pexpect` rather than typing them by hand.
[scripts/smoke_4h_cli.py](../../scripts/smoke_4h_cli.py) is a working
example that answers every prompt for a `BTC-USD` 4H run and logs the full
transcript to `reports/smoke_4h_cli_<timestamp>.log`:

```bash
.venv/bin/python scripts/smoke_4h_cli.py
```

## 5. Programmatic use (no CLI)

```python
from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph

config = DEFAULT_CONFIG.copy()
config["timeframe"] = "1d"  # or "4h" for crypto day-trading mode
ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("AAPL", "2026-07-08")
```

See the README's [Day Trading (4H)](../../README.md#day-trading-4h) section
for the 4H-specific config keys, bar-close semantics, and report/memory
layout.
