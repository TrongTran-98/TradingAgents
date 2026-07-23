"""ScalpJournal (Phase 5) + walk-forward outcome resolution (Phase 6).

Full design: docs/plans/gold-scalping-mt5/PLAN.md. ``ScalpJournal`` mirrors
``tradingagents/agents/utils/memory.py``'s ``TradingMemoryLog`` atomic-write
idiom (temp-file + ``os.replace()``) but stores one JSON record per line
instead of markdown -- the weekly-reflection guardrails (Phase 7) need
per-field bucketing/querying at the volume a scalping journal produces
(many signals/day), which is impractical to regex out of markdown at that
scale. ``scalp_lessons.md`` (Phase 7's human-readable output) stays
markdown; only this journal is JSONL.

``resolve_outcome()`` walk-forward-simulates a triggered signal's entry
against subsequent real 5m bars (bar-by-bar, first-touch-wins) and
``ScalpJournal.update_outcome()`` rewrites that one record's ``outcome``
field in place -- same atomic read-modify-write shape as
``TradingMemoryLog.update_with_outcome``.
"""

from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from tradingagents.agents.utils.scalp_schemas import ScalpSignal, SignalOutcome
from tradingagents.dataflows import mt5_vendor
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import NoMarketDataError

# Any gap between the previous scanned bar/anchor and the bar that resolves
# an outcome at least this large is tagged gap_through (weekend/holiday
# reopen already past the level) rather than an ordinary intrabar spike --
# comfortably above a single skipped/thin-liquidity 5m bar, well below a
# real weekend gap (typically 48h+).
_GAP_MINUTES_THRESHOLD = 60.0


class ScalpJournal:
    """Append-only JSONL journal of ``ScalpSignal`` records."""

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        scalping_cfg = cfg.get("scalping", {})
        self._journal_path: Path | None = None
        path = scalping_cfg.get("journal_path")
        if path:
            self._journal_path = Path(path).expanduser()
            self._journal_path.parent.mkdir(parents=True, exist_ok=True)
        # Serializes read-modify-write within this process. Doesn't protect
        # against a second OS process writing concurrently (same gap
        # TradingMemoryLog has), but without it, two threads' os.replace()
        # calls onto the same target can collide with a Windows
        # PermissionError even with distinct temp file names.
        self._lock = threading.Lock()

    @property
    def journal_path(self) -> Path | None:
        return self._journal_path

    def append(self, signal: ScalpSignal) -> None:
        """Append a signal to the journal.

        Idempotent on ``signal_id``: re-appending a signal whose ID is
        already present is a no-op rather than a duplicate line, so a
        retried pipeline run never corrupts the journal's per-signal
        bucketing (Phase 7 buckets/counts by signal). Written via a full
        read + temp-file + ``os.replace()`` rewrite, same atomicity
        guarantee as ``TradingMemoryLog``, so a crash mid-write never
        leaves a partially-written journal.
        """
        if not self._journal_path:
            return

        with self._lock:
            records = self._read_records()
            if any(r.get("signal_id") == signal.signal_id for r in records):
                return

            records.append(json.loads(signal.model_dump_json()))
            self._write_records(records)

    def read_signals(self) -> list[ScalpSignal]:
        """Parse every journaled record back into a ``ScalpSignal``."""
        return [ScalpSignal.model_validate(r) for r in self._read_records()]

    def update_outcome(self, signal_id: str, outcome: SignalOutcome) -> bool:
        """Overwrite one record's ``outcome`` field in place.

        Returns ``False`` (no-op) if ``signal_id`` isn't in the journal.
        Same read-modify-write + temp-file/``os.replace()`` atomicity as
        ``append()``.
        """
        if not self._journal_path:
            return False

        with self._lock:
            records = self._read_records()
            match = next((r for r in records if r.get("signal_id") == signal_id), None)
            if match is None:
                return False
            match["outcome"] = json.loads(outcome.model_dump_json())
            self._write_records(records)
            return True

    def pending_signals(self) -> list[ScalpSignal]:
        """Triggered signals whose outcome is still awaiting walk-forward resolution.

        Untriggered signals have no entry to resolve, so they're excluded
        even though their default ``outcome.status`` is also ``"pending"``.
        """
        return [
            s for s in self.read_signals()
            if s.entry_trigger.triggered and s.outcome.status == "pending"
        ]

    # --- Helpers ---

    def _read_records(self) -> list[dict]:
        if not self._journal_path or not self._journal_path.exists():
            return []
        records = []
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records

    def _write_records(self, records: list[dict]) -> None:
        lines = [json.dumps(r, separators=(",", ":")) for r in records]
        new_text = "\n".join(lines) + ("\n" if lines else "")
        # Unique per-call suffix (not a fixed ".tmp") so concurrent writers
        # never race on the same temp path -- os.replace() is atomic per
        # call, but two threads/processes both targeting one fixed tmp file
        # can still collide with a PermissionError on Windows.
        tmp_path = self._journal_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp_path.write_text(new_text, encoding="utf-8")
        tmp_path.replace(self._journal_path)


# ---------------------------------------------------------------------------
# Phase 6 -- walk-forward outcome resolution
# ---------------------------------------------------------------------------


def _to_utc_naive(dt: datetime) -> datetime:
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _scan_bars(
    bars: pd.DataFrame,
    direction: str,
    stop_loss: float,
    take_profit: float,
    max_holding_bars: int,
    generated_at_utc: datetime,
    now: datetime,
    window_end: datetime,
) -> SignalOutcome:
    """Bar-by-bar, first-touch-wins scan of ``bars`` for an SL/TP hit.

    ``bars`` must already be ascending-by-time UTC bars strictly after
    ``generated_at_utc`` (``mt5_vendor.get_mt5_rates_range``'s shape).
    Same-bar both-hit resolves conservatively as an SL hit -- there's no way
    to know from OHLC alone which level traded first within the bar.
    """
    prev_time = generated_at_utc
    scanned = 0
    for _, bar in bars.iterrows():
        if scanned >= max_holding_bars:
            break
        scanned += 1
        bar_time = pd.Timestamp(bar["time"]).to_pydatetime()

        if direction == "long":
            sl_hit = bar["low"] <= stop_loss
            tp_hit = bar["high"] >= take_profit
        else:
            sl_hit = bar["high"] >= stop_loss
            tp_hit = bar["low"] <= take_profit

        if sl_hit or tp_hit:
            gap_minutes = (bar_time - prev_time).total_seconds() / 60.0
            status, exit_price = ("loss", stop_loss) if sl_hit else ("win", take_profit)
            return SignalOutcome(
                status=status,
                exit_price=exit_price,
                exit_bar_time_utc=bar_time.replace(tzinfo=timezone.utc),
                bars_held=scanned,
                gap_through=gap_minutes >= _GAP_MINUTES_THRESHOLD,
                resolved_at_utc=now.replace(tzinfo=timezone.utc),
            )
        prev_time = bar_time

    if scanned >= max_holding_bars or now >= window_end:
        return SignalOutcome(
            status="timeout", bars_held=scanned, resolved_at_utc=now.replace(tzinfo=timezone.utc)
        )
    return SignalOutcome(status="pending", bars_held=scanned)


def resolve_outcome(
    signal: ScalpSignal,
    config: dict | None = None,
    now: datetime | None = None,
    bars: pd.DataFrame | None = None,
) -> SignalOutcome:
    """Walk-forward resolve ``signal``'s entry against subsequent 5m bars.

    Fetches bars forward from ``generated_at_utc`` via
    ``mt5_vendor.get_mt5_rates_range``, up to ``scalping.max_holding_bars_5m``
    (default 48). Pass ``bars`` directly to bypass the MT5 fetch (unit tests
    supply synthetic sequences this way). Requires a *triggered* signal with
    ``entry_price``/``stop_loss``/``take_profit_1`` all set -- callers are
    expected to only call this on ``ScalpJournal.pending_signals()``, which
    are already filtered to triggered entries.

    No bars available yet (``NoMarketDataError``, or an empty/partial
    ``bars`` before the holding window has fully elapsed) resolves to
    ``"pending"``, to be retried on a later run -- same deferred-resolution
    idiom as ``TradingAgentsGraph._resolve_pending_entries``.
    """
    trigger = signal.entry_trigger
    if not trigger.triggered:
        raise ValueError(
            f"resolve_outcome requires a triggered signal (signal_id={signal.signal_id!r})"
        )
    entry, stop, tp1 = trigger.entry_price, trigger.stop_loss, trigger.take_profit_1
    if entry is None or stop is None or tp1 is None:
        raise ValueError(
            "resolve_outcome requires entry_price/stop_loss/take_profit_1 "
            f"(signal_id={signal.signal_id!r})"
        )

    cfg = (config or get_config())["scalping"]
    max_holding_bars = cfg["max_holding_bars_5m"]
    direction = "long" if tp1 > entry else "short"

    now = _to_utc_naive(now) if now is not None else datetime.now(timezone.utc).replace(tzinfo=None)
    generated_at = _to_utc_naive(signal.generated_at_utc)
    window_end = generated_at + timedelta(minutes=5 * max_holding_bars)

    if bars is None:
        try:
            bars = mt5_vendor.get_mt5_rates_range(
                signal.symbol,
                "5m",
                generated_at,
                window_end,
                utc_offset_hours=cfg["mt5_server_utc_offset_hours"],
            )
        except NoMarketDataError:
            return SignalOutcome(status="pending")

    bars = bars[bars["time"] > generated_at].reset_index(drop=True)
    return _scan_bars(bars, direction, stop, tp1, max_holding_bars, generated_at, now, window_end)
