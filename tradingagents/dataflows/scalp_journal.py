"""ScalpJournal: JSONL-backed, append-only write path for ScalpSignal records (Phase 5).

Full design: docs/plans/gold-scalping-mt5/PLAN.md. Mirrors
``tradingagents/agents/utils/memory.py``'s ``TradingMemoryLog`` atomic-write
idiom (temp-file + ``os.replace()``) but stores one JSON record per line
instead of markdown -- the weekly-reflection guardrails (Phase 7) need
per-field bucketing/querying at the volume a scalping journal produces
(many signals/day), which is impractical to regex out of markdown at that
scale. ``scalp_lessons.md`` (Phase 7's human-readable output) stays
markdown; only this journal is JSONL.

Walk-forward outcome resolution (Phase 6) lands in this same module and
will rewrite existing records in place (same atomic read-modify-write
shape as ``TradingMemoryLog.update_with_outcome``) -- not implemented yet.
"""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path

from tradingagents.agents.utils.scalp_schemas import ScalpSignal


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
