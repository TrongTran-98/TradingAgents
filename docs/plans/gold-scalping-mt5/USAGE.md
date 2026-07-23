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
- The project's `.venv` activated (see below) — `tradingagents` is a console script installed
  into it, not a global command.

## Activating the environment

The repo ships a `.venv` with `tradingagents` already installed (editable install via
`pip install -e .`). Activate it before running any `tradingagents scalp ...` command:

```powershell
# PowerShell
.venv\Scripts\Activate.ps1
```

```bash
# Git Bash / bash
source .venv/Scripts/activate
```

```bat
:: cmd.exe
.venv\Scripts\activate.bat
```

Your prompt gets a `(.venv)` prefix once active, and `tradingagents` resolves directly:

```bash
tradingagents scalp run --symbol XAUUSD.sc --llm-provider ollama --llm-model qwen3:8b
```

Run `deactivate` to leave the environment. If `.venv` doesn't exist yet (fresh clone), create it
and install first:

```bash
python -m venv .venv
source .venv/Scripts/activate        # or .venv\Scripts\Activate.ps1 on PowerShell
pip install -e ".[mt5]"              # mt5 extra required for MetaTrader5 (Windows only)
```

## Configuring MT5

The CLI never launches or logs into the terminal for you — it only attaches to whatever is
already running via `MetaTrader5.initialize()` (see [mt5_session.py](../../../tradingagents/dataflows/mt5_session.py)).
Before running `scalp run`/`review`:

1. **Start the MT5 terminal and log in to your broker account.** Leave it running for the
   duration of the CLI call — the session opens around the pipeline/resolver call and shuts
   down after (`cli/scalp.py`'s `with mt5_session.session():` block).
2. **Add your gold symbol to Market Watch** (right-click Market Watch → Symbols, or drag it in
   from the Symbols list). Confirm the exact ticker your broker uses — many brokers suffix it
   (`XAUUSD.sc`, `XAUUSD.m`, `GOLD`, etc.). If unsure, check in the terminal's Market Watch
   window, or query it from Python:
   ```bash
   python -c "import MetaTrader5 as mt5; mt5.initialize(); print([s.name for s in mt5.symbols_get() if 'XAU' in s.name.upper() or 'GOLD' in s.name.upper()])"
   ```
3. **Resolve your broker's server-time UTC offset.** `copy_rates_*` returns bars in broker
   *server* time, not UTC, and the pipeline's session classification (Asian/London/NY/overlap)
   is meaningless if this is wrong. Compare the terminal clock to real UTC:
   ```bash
   python -c "import MetaTrader5 as mt5; from datetime import datetime, timezone; mt5.initialize(); print(mt5.symbol_info_tick('XAUUSD.sc').time, datetime.now(timezone.utc).timestamp())"
   ```
   Set the result (hours, server minus UTC) in `default_config.py`'s
   `scalping.mt5_server_utc_offset_hours` (currently `3`, verified for Vantage Markets'
   "VantageMarkets-Live 11" server — **re-check this if you switch broker or server**, it is
   not portable across accounts).
4. **Install the optional MT5 dependency** if you haven't: `pip install -e ".[mt5]"` (Windows
   only — the `MetaTrader5` package has no Linux/Mac build).

### Config keys (`tradingagents/default_config.py`'s `scalping` block)

| Key | Default | Purpose |
|---|---|---|
| `symbol` | `"XAUUSD"` | Default symbol; override per-run with `--symbol`. |
| `mt5_server_utc_offset_hours` | `3` | Broker server time minus UTC. **Verify per broker** (step 3 above). |
| `timeframes` | `{"htf": ["4H","1H"], "ltf": "15m", "entry": "5m"}` | Bars fetched per pipeline stage. |
| `sessions_utc` | `{"london": [7,16], "ny": [12,21]}` | Session windows in UTC, used after offset correction. |
| `journal_path` / `lessons_path` | `~/.tradingagents/scalp/scalp_journal.jsonl` / `scalp_lessons.md` | Where signals/lessons persist. |

The rest (`sl_atr_buffer_*`, `min_risk_reward`, `min_displacement_atr`, `max_holding_bars_5m`,
`min_lesson_sample_size`, `max_active_lessons`, `use_pivot_points`) are strategy thresholds, not
connection settings — see [PLAN.md](PLAN.md) for what each controls. Change any of these by
editing `default_config.py` directly (no CLI flags or env vars expose the `scalping` block today
except `symbol`, which `--symbol` overrides).

**Symptom → fix**:
- `VendorNotConfiguredError` on every run → the CLI's `mt5_session.session()` call couldn't
  `initialize()` — terminal isn't running/logged in, or a firewall/AV is blocking the local IPC
  the Python package uses.
- `NoMarketDataError` → terminal is up but the symbol isn't in Market Watch, or the symbol name
  in config/`--symbol` doesn't match the broker's actual ticker.
- Session labels (asian/london/ny) look off by a few hours in the printed `LTFStructure` →
  `mt5_server_utc_offset_hours` is wrong for this broker/server — redo step 3.

## Configuring the LLM provider

`scalp run`/`review` use **one combined LLM** for all three analysts (no deep/quick split,
unlike the main `tradingagents` graph) — see `cli/scalp.py`'s `_build_llm`. It reads
`llm_provider` + `quick_think_llm` (+ `backend_url`) from config, in this precedence order:

```
--llm-provider/--llm-model/--backend-url flag  >  TRADINGAGENTS_* env var  >  default_config.py
```

### CLI flags (highest precedence, per-run)

```bash
tradingagents scalp run --symbol XAUUSD.sc --llm-provider ollama --llm-model qwen3:8b
tradingagents scalp run --llm-provider openai --llm-model gpt-5.4-mini
tradingagents scalp run --llm-provider anthropic --llm-model claude-sonnet-5 --backend-url https://my-proxy.example.com/v1
```

- `--llm-provider` — one of the values in the provider table below (case-insensitive).
- `--llm-model` — model name/ID for that provider (`qwen3:8b`, `gpt-5.4-mini`,
  `claude-sonnet-5`, ...).
- `--backend-url` — override the provider's endpoint (proxy/gateway, self-hosted
  OpenAI-compatible server, or a non-default Ollama host).

### Env vars / config defaults (persist across runs)

Set once in the shell or `default_config.py` so you don't have to pass flags every time:

```bash
export TRADINGAGENTS_LLM_PROVIDER=ollama
export TRADINGAGENTS_QUICK_THINK_LLM=qwen3:8b
```

or edit `default_config.py` directly:

```python
"llm_provider": "ollama",
"quick_think_llm": "qwen3:8b",
```

(`deep_think_llm` is unused by the scalp pipeline — only `quick_think_llm` feeds
`_build_llm`.)

### Supported providers

| `--llm-provider` value | API key env var | Default endpoint | Notes |
|---|---|---|---|
| `ollama` | *(none — local)* | `http://localhost:11434/v1` (override via `OLLAMA_BASE_URL` or `--backend-url`) | Local inference; any pulled model accepted, e.g. `qwen3:8b`. Used for Phase 8's real verification run. |
| `openai` | `OPENAI_API_KEY` | api.openai.com | Uses the Responses API natively. |
| `anthropic` | `ANTHROPIC_API_KEY` | api.anthropic.com | Native client, not OpenAI-compatible wire format. |
| `google` | `GOOGLE_API_KEY` | generativelanguage.googleapis.com | Native client. |
| `azure` | `AZURE_OPENAI_API_KEY` | *(your Azure resource)* | Requires `--backend-url` pointed at your deployment. |
| `bedrock` | *(AWS credential chain)* | — | No single key env; uses standard AWS auth. |
| `xai` | `XAI_API_KEY` | api.x.ai | |
| `deepseek` | `DEEPSEEK_API_KEY` | api.deepseek.com | |
| `qwen` / `qwen-cn` | `DASHSCOPE_API_KEY` / `DASHSCOPE_CN_API_KEY` | Dashscope intl/CN | Separate accounts, keys not interchangeable. |
| `glm` / `glm-cn` | `ZHIPU_API_KEY` / `ZHIPU_CN_API_KEY` | api.z.ai / open.bigmodel.cn | |
| `minimax` / `minimax-cn` | `MINIMAX_API_KEY` / `MINIMAX_CN_API_KEY` | api.minimax.io / api.minimaxi.com | |
| `openrouter` | `OPENROUTER_API_KEY` | openrouter.ai | |
| `mistral` | `MISTRAL_API_KEY` | api.mistral.ai | |
| `kimi` | `MOONSHOT_API_KEY` | api.moonshot.ai | Moonshot AI. |
| `groq` | `GROQ_API_KEY` | api.groq.com | |
| `nvidia` | `NVIDIA_API_KEY` | integrate.api.nvidia.com | NVIDIA NIM. |
| `openai_compatible` | `OPENAI_COMPATIBLE_API_KEY` (optional) | *(none — `--backend-url` required)* | Generic escape hatch for any other OpenAI-compatible server. |

Full source of truth: [`tradingagents/llm_clients/openai_client.py`](../../../tradingagents/llm_clients/openai_client.py)'s
`OPENAI_COMPATIBLE_PROVIDERS` registry and
[`tradingagents/llm_clients/api_key_env.py`](../../../tradingagents/llm_clients/api_key_env.py).
`anthropic`, `google`, `azure`, `bedrock` use their own native clients (not the OpenAI-compatible
registry) — see `llm_clients/factory.py`.

### Local Ollama setup (recommended default — no API key, no per-token cost)

```bash
ollama serve                 # usually already running as a background service
ollama pull qwen3:8b         # or any other local model
tradingagents scalp run --symbol XAUUSD.sc --llm-provider ollama --llm-model qwen3:8b
```

To point at a non-default Ollama host (e.g. running on another machine or a different port),
either set `OLLAMA_BASE_URL` or pass `--backend-url http://<host>:11434/v1`.

**Known robustness gap (Phase 4/8 finding)**: on longer, reasoning-heavy prompts — the Step 3
entry-trigger call especially — `qwen3:8b` sometimes answers in free-form prose instead of the
forced structured-output tool call, even with `tool_choice` forced. This is handled automatically
by `invoke_structured_with_fallback` (retries once, then falls back to a conservative
"no signal" default) — no user action needed, but if you see unexpectedly frequent "no signal"
results, check the console for a fallback warning and consider a stronger/larger local model.

### Switching to a hosted provider

```bash
export OPENAI_API_KEY=sk-...
tradingagents scalp run --symbol XAUUSD.sc --llm-provider openai --llm-model gpt-5.4-mini
```

```bash
export ANTHROPIC_API_KEY=sk-ant-...
tradingagents scalp run --symbol XAUUSD.sc --llm-provider anthropic --llm-model claude-sonnet-5
```

Any provider from the table works the same way: set its API-key env var (skip for `ollama` or a
keyless `openai_compatible` server), then pass `--llm-provider`/`--llm-model`. Missing/invalid
keys surface as a provider auth error at the first LLM call, not at CLI startup — the command
prints the pipeline's "Running scalp pipeline for ..." line first, then fails on `_build_llm`'s
first `.invoke()`.

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
