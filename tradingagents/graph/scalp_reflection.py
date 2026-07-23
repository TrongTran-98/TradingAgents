"""ScalpReflector: weekly learning without overfitting/hallucination (Phase 7).

Full design: docs/plans/gold-scalping-mt5/PLAN.md ("Step 5 -- Weekly learning
without overfitting/hallucination"). The guardrails are Python-side and run
*before* any LLM call:

1. Resolved losing/timeout signals are bucketed by fixed categorical keys
   (setup_type, session, HTF/LTF agreement) and counted deterministically.
2. Only buckets with >= ``min_lesson_sample_size`` occurrences are even
   offered to the LLM as candidate lesson material -- a single bad trade
   never becomes a "lesson".
3. The LLM only sees those structured, pre-bucketed journal fields (never
   free-text reports, never re-fetched market data) and must return a
   ``WeeklyReviewResult`` via ``bind_structured``.
4. Every returned ``Lesson`` is re-validated against the Python-computed
   buckets: unknown ``bucket_key``, fabricated ``supporting_signal_ids``, or
   a mismatched ``occurrences`` count gets the lesson dropped with a logged
   warning -- this is the concrete hallucination guard, not just a prompt
   instruction.

``ScalpLessonStore`` persists the validated lessons (JSONL, machine-readable
-- mirrors ``ScalpJournal``'s atomic temp-file + ``os.replace()`` idiom) and
renders ``scalp_lessons.md`` (human-readable) alongside it, rotating at
``max_active_lessons`` (oldest dropped first, same idiom as
``memory_log_max_entries``). ``load_active_lessons`` is the read-side of the
Phase 4 seam: it renders the active lesson set into the same text block each
scalp analyst already injects into its prompt.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from langchain_core.prompts import ChatPromptTemplate

from tradingagents.agents.utils.scalp_schemas import Lesson, ScalpSignal, WeeklyReviewResult
from tradingagents.agents.utils.structured import bind_structured
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.scalp_journal import ScalpJournal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Python-side bucketing (runs before any LLM call)
# ---------------------------------------------------------------------------


def _bucket_key(signal: ScalpSignal) -> str:
    return (
        f"setup_type={signal.entry_trigger.trigger_type}"
        f"|session={signal.ltf_structure.session}"
        f"|htf_ltf_agreement={signal.ltf_structure.agrees_with_htf}"
    )


def bucket_resolved_signals(signals: list[ScalpSignal]) -> dict[str, list[ScalpSignal]]:
    """Bucket triggered signals whose outcome resolved to loss/timeout.

    Untriggered signals never get a real outcome (they stay at the default
    ``pending``), so they're excluded even before checking ``status``.
    """
    buckets: dict[str, list[ScalpSignal]] = defaultdict(list)
    for signal in signals:
        if not signal.entry_trigger.triggered:
            continue
        if signal.outcome.status not in ("loss", "timeout"):
            continue
        buckets[_bucket_key(signal)].append(signal)
    return dict(buckets)


def _candidate_buckets(
    buckets: dict[str, list[ScalpSignal]], min_lesson_sample_size: int
) -> dict[str, list[ScalpSignal]]:
    return {k: v for k, v in buckets.items() if len(v) >= min_lesson_sample_size}


# ---------------------------------------------------------------------------
# LLM input construction -- structured journal fields only
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM_MESSAGE = (
    "You are reviewing a gold scalping journal's resolved losing/timeout "
    "signals, already grouped into fixed categorical buckets by setup "
    "type, session, and HTF/LTF agreement. Each bucket below already meets "
    "the minimum sample-size threshold to be considered. For each bucket "
    "where the signals reveal a genuine, actionable pattern, produce one "
    "Lesson with bucket_key copied *exactly* as given, occurrences equal "
    "to the count shown for that bucket, and supporting_signal_ids drawn "
    "only from the signal_id values listed under that bucket -- never "
    "invent a signal_id, never merge signals across buckets, never cite a "
    "subset if you intend occurrences to equal the full bucket count. Skip "
    "a bucket entirely if it does not show a real, generalizable pattern -- "
    "an empty lessons list is a valid answer. Use only the structured "
    "fields given below; do not assume any market context beyond them."
)


def _signal_summary_line(signal: ScalpSignal) -> str:
    return (
        f"    - signal_id={signal.signal_id} outcome={signal.outcome.status} "
        f"gap_through={signal.outcome.gap_through} "
        f"entry_rationale={signal.entry_trigger.rationale!r} "
        f"ltf_rationale={signal.ltf_structure.rationale!r}"
    )


def _build_review_messages(candidates: dict[str, list[ScalpSignal]]) -> list:
    blocks = []
    for bucket_key, signals in candidates.items():
        lines = [f"Bucket: {bucket_key} (occurrences={len(signals)})"]
        lines.extend(_signal_summary_line(s) for s in signals)
        blocks.append("\n".join(lines))

    prompt = ChatPromptTemplate.from_messages(
        [("system", _REVIEW_SYSTEM_MESSAGE), ("human", "\n\n".join(blocks))]
    )
    return prompt.format_messages()


# ---------------------------------------------------------------------------
# Post-LLM Python validation -- the hallucination guard
# ---------------------------------------------------------------------------


def _validate_lessons(
    lessons: list[Lesson], candidates: dict[str, list[ScalpSignal]]
) -> list[Lesson]:
    validated = []
    for lesson in lessons:
        bucket = candidates.get(lesson.bucket_key)
        if bucket is None:
            logger.warning(
                "ScalpReflector: dropping lesson with unknown bucket_key %r", lesson.bucket_key
            )
            continue

        real_ids = {s.signal_id for s in bucket}
        cited_ids = set(lesson.supporting_signal_ids)
        if not cited_ids or not cited_ids.issubset(real_ids):
            logger.warning(
                "ScalpReflector: dropping lesson for bucket %r -- "
                "supporting_signal_ids not all real (%s)",
                lesson.bucket_key, lesson.supporting_signal_ids,
            )
            continue

        if lesson.occurrences != len(bucket):
            logger.warning(
                "ScalpReflector: dropping lesson for bucket %r -- "
                "occurrences=%d does not match bucket size=%d",
                lesson.bucket_key, lesson.occurrences, len(bucket),
            )
            continue

        validated.append(lesson)
    return validated


# ---------------------------------------------------------------------------
# Lesson persistence -- scalp_lessons.jsonl (machine-readable) +
# scalp_lessons.md (human-readable rendering of the same records)
# ---------------------------------------------------------------------------


def _render_markdown(records: list[dict]) -> str:
    if not records:
        return "# Scalp Lessons\n\nNo active lessons yet.\n"
    lines = ["# Scalp Lessons", ""]
    for record in records:
        lines.append(f"## {record['bucket_key']}")
        lines.append(f"- Occurrences: {record['occurrences']}")
        lines.append(f"- Supporting signals: {', '.join(record['supporting_signal_ids'])}")
        lines.append(f"- Recorded: {record.get('created_at_utc', 'n/a')}")
        lines.append("")
        lines.append(record["lesson_text"])
        lines.append("")
    return "\n".join(lines)


class ScalpLessonStore:
    """Atomic JSONL store of validated lessons + a rendered markdown mirror.

    Same temp-file + ``os.replace()`` atomicity as ``ScalpJournal``, and the
    same uniquely-named-tmp-file + in-process lock reasoning (a fixed tmp
    name racing two writers can hit a Windows ``PermissionError``).
    """

    def __init__(self, config: dict | None = None):
        cfg = config or get_config()
        scalping_cfg = cfg.get("scalping", {})
        self._md_path: Path | None = None
        self._jsonl_path: Path | None = None
        path = scalping_cfg.get("lessons_path")
        if path:
            self._md_path = Path(path).expanduser()
            self._jsonl_path = self._md_path.with_suffix(".jsonl")
            self._md_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    @property
    def lessons_path(self) -> Path | None:
        return self._md_path

    def read(self) -> list[dict]:
        """Read persisted lesson records, oldest first."""
        if not self._jsonl_path or not self._jsonl_path.exists():
            return []
        records = []
        for line in self._jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
        return records

    def add_lessons(self, lessons: list[Lesson], max_active_lessons: int | None) -> None:
        """Append validated lessons and rotate, dropping the oldest first.

        No-op if no journal/lessons path is configured (mirrors
        ``ScalpJournal.append``'s config-less no-op).
        """
        if not lessons or not self._jsonl_path:
            return

        with self._lock:
            records = self.read()
            now = datetime.now(timezone.utc).isoformat()
            for lesson in lessons:
                record = json.loads(lesson.model_dump_json())
                record["created_at_utc"] = now
                records.append(record)

            if max_active_lessons and max_active_lessons > 0 and len(records) > max_active_lessons:
                records = records[-max_active_lessons:]

            self._write(records)

    def _write(self, records: list[dict]) -> None:
        jsonl_lines = [json.dumps(r, separators=(",", ":")) for r in records]
        jsonl_text = "\n".join(jsonl_lines) + ("\n" if jsonl_lines else "")
        tmp_jsonl = self._jsonl_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp_jsonl.write_text(jsonl_text, encoding="utf-8")
        tmp_jsonl.replace(self._jsonl_path)

        md_text = _render_markdown(records)
        tmp_md = self._md_path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        tmp_md.write_text(md_text, encoding="utf-8")
        tmp_md.replace(self._md_path)


def load_active_lessons(config: dict | None = None) -> str:
    """Render the active lesson set into the text block each scalp analyst injects.

    Empty string when no lessons are active yet -- each analyst already
    treats an empty ``active_lessons`` as "omit the lessons block entirely"
    (see htf_bias_analyst.py et al.), so this is the direct producer for
    ``ScalpPipeline.run(..., active_lessons=load_active_lessons(config))``.
    """
    store = ScalpLessonStore(config or get_config())
    records = store.read()
    if not records:
        return ""
    return "\n".join(f"- [{r['bucket_key']}] {r['lesson_text']}" for r in records)


# ---------------------------------------------------------------------------
# ScalpReflector -- weekly batch reflection
# ---------------------------------------------------------------------------


class ScalpReflector:
    """Weekly reflection over the scalp journal, guarded against overfitting/hallucination."""

    def __init__(self, llm, config: dict | None = None):
        self.llm = llm
        self.config = config or get_config()
        self.store = ScalpLessonStore(self.config)

    def weekly_review(self, journal: ScalpJournal | None = None) -> WeeklyReviewResult:
        """Bucket resolved losses/timeouts, ask the LLM for lessons, validate, persist.

        Returns an empty ``WeeklyReviewResult`` (no LLM call made) when no
        bucket meets ``min_lesson_sample_size`` -- a quiet week produces no
        lessons rather than forcing the LLM to manufacture one.
        """
        journal = journal or ScalpJournal(self.config)
        signals = journal.read_signals()

        scalping_cfg = self.config["scalping"]
        buckets = bucket_resolved_signals(signals)
        candidates = _candidate_buckets(buckets, scalping_cfg["min_lesson_sample_size"])
        if not candidates:
            return WeeklyReviewResult(lessons=[])

        structured_llm = bind_structured(self.llm, WeeklyReviewResult, "Scalp Reflector")
        if structured_llm is None:
            raise RuntimeError(
                "Scalp Reflector requires an LLM provider with structured-output "
                "support; weekly review depends on typed, citation-checkable "
                "Lesson fields, not free-text parsing."
            )

        messages = _build_review_messages(candidates)
        result = structured_llm.invoke(messages)
        raw_lessons = result.lessons if result is not None else []

        validated = _validate_lessons(raw_lessons, candidates)
        if validated:
            self.store.add_lessons(validated, scalping_cfg["max_active_lessons"])

        return WeeklyReviewResult(lessons=validated)
