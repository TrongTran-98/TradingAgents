"""End-to-end smoke test: drive the interactive CLI through a full 4H run.

Spawns ``python -m cli.main`` in a pty (the questionary prompts need
a real TTY) and answers every prompt the way a cold user would, letting the
full multi-agent graph run against BTC-USD in 4H day-trading mode with
whatever provider/model the project ``.env`` configures. The complete CLI
transcript (including the live per-node progress display) is written to
``reports/smoke_4h_cli_<timestamp>.log``.

Selections made:
    ticker            BTC-USD
    timeframe         4h  (down-arrow + enter on the crypto-only prompt)
    analysis date     <default = current UTC timestamp>
    output language   English (default)
    analysts          all (press 'a')
    research depth    Shallow (1 round)
    provider/models   from .env -> prompts skipped (set
                      TRADINGAGENTS_LLM_PROVIDER / _DEEP_THINK_LLM /
                      _QUICK_THINK_LLM, or the driver will time out on the
                      interactive provider menu)
Post-run: saves the report to the default path, skips the on-screen dump.

Requires ``pexpect`` (not a runtime dependency): ``pip install pexpect``.
Run from anywhere: ``.venv/bin/python scripts/smoke_4h_cli.py``.
"""

import datetime
import os
import sys
import time
from pathlib import Path

import pexpect

REPO = Path(__file__).resolve().parent.parent
LOG = (
    REPO
    / "reports"
    / f"smoke_4h_cli_{datetime.datetime.now():%Y%m%d_%H%M%S}.log"
)
LOG.parent.mkdir(parents=True, exist_ok=True)

env = dict(os.environ)
env["PYTHONPATH"] = str(REPO)  # never pick up a stale site-packages install
env.setdefault("TERM", "xterm-256color")

child = pexpect.spawn(
    sys.executable,
    ["-m", "cli.main"],
    cwd=str(REPO),
    env=env,
    encoding="utf-8",
    codec_errors="replace",
    timeout=300,
    dimensions=(50, 200),
)
logf = open(LOG, "w", encoding="utf-8")
child.logfile_read = logf


def step(label, pattern, send=None, sendline=None, timeout=300, settle=1.0):
    print(f"[driver] waiting for: {label}", flush=True)
    child.expect(pattern, timeout=timeout)
    time.sleep(settle)  # let questionary/rich finish rendering
    if send is not None:
        child.send(send)
    if sendline is not None:
        child.sendline(sendline)
    print(f"[driver] done: {label}", flush=True)


try:
    step("ticker prompt", "ticker symbol", sendline="BTC-USD")
    # crypto-only timeframe select: Daily is highlighted first; one down + enter
    step("timeframe select", "Trading Timeframe")
    child.expect("arrow keys")
    time.sleep(1.0)
    child.send("\x1b[B")
    time.sleep(0.5)
    child.send("\r")
    print("[driver] timeframe: picked 4h", flush=True)
    # typer.prompt with a default like [<today> HH:MM] -> accept the default
    step("analysis date", "Analysis Date", settle=1.5)
    child.expect(r"\[%d" % datetime.datetime.now(datetime.timezone.utc).year)
    time.sleep(0.5)
    child.sendline("")
    print("[driver] date: accepted default (now UTC)", flush=True)
    step("output language", "Select Output Language", send="\r")
    step("analysts checkbox", "Press Space", send="a")
    time.sleep(0.5)
    child.send("\r")
    print("[driver] analysts: selected all", flush=True)
    step("research depth", "Research Depth")
    child.expect("arrow keys")
    time.sleep(1.0)
    child.send("\r")  # Shallow
    print("[driver] depth: Shallow", flush=True)

    # provider / models / thinking config come from .env -> no prompts.
    # Now the full graph runs; give it plenty of time.
    step(
        "analysis complete -> save prompt",
        "Save report",
        timeout=5400,
        sendline="Y",
    )
    step("save path", "Save path", sendline="")
    step("display full report", "Display full report", sendline="N")
    child.expect(pexpect.EOF, timeout=300)
    print("[driver] CLI exited cleanly", flush=True)
except Exception as exc:  # noqa: BLE001
    tail = child.before[-2000:] if child.before else ""
    print(f"[driver] FAILED: {type(exc).__name__}: {exc}", flush=True)
    print(f"[driver] last 2000 chars of buffer:\n{tail}", flush=True)
    child.close(force=True)
    logf.close()
    print(f"[driver] partial transcript: {LOG}", flush=True)
    sys.exit(1)

child.close()
logf.close()
print(f"[driver] exit status: {child.exitstatus}", flush=True)
print(f"[driver] full transcript: {LOG}", flush=True)
sys.exit(child.exitstatus or 0)
