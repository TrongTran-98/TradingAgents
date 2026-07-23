"""Tests for the gold-scalping weekly reflection pipeline (Phase 7).

See docs/plans/gold-scalping-mt5/PLAN.md and TRACKING.md for the design and
checklist these tests verify against. Fixtures are synthetic journal
entries, independent of real accumulated volume.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from tradingagents.agents.utils.scalp_schemas import (
    EntryTrigger,
    HTFBias,
    KeyZone,
    Lesson,
    LTFStructure,
    ScalpSignal,
    SignalOutcome,
    WeeklyReviewResult,
)
from tradingagents.dataflows.scalp_journal import ScalpJournal
from tradingagents.graph.scalp_reflection import (
    ScalpLessonStore,
    ScalpReflector,
    bucket_resolved_signals,
    load_active_lessons,
)


def _key_zone(price: float = 2400.0) -> KeyZone:
    return KeyZone(price=price, label="prior day high", source_timeframe="1D")


def _signal(
    signal_id: str,
    trigger_type: str = "order_block_retest",
    session: str = "ny",
    agrees_with_htf: bool = True,
    status: str = "loss",
    triggered: bool = True,
) -> ScalpSignal:
    return ScalpSignal(
        signal_id=signal_id,
        symbol="XAUUSD",
        generated_at_utc=datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc),
        htf_bias=HTFBias(
            bias="bullish",
            confidence="high",
            key_zones=[_key_zone()],
            rationale="4H/1H HH/HL structure, EMA stack bullish.",
            invalidation_note="A 1H close below 2390 would flip this bias.",
        ),
        ltf_structure=LTFStructure(
            event="BOS",
            event_price=2402.0,
            displacement_atr=0.4,
            session=session,
            volatility_regime="normal",
            agrees_with_htf=agrees_with_htf,
            confidence="high",
            tradeable=True,
            rationale="15m BOS read.",
        ),
        entry_trigger=EntryTrigger(
            trigger_type=trigger_type,
            triggered=triggered,
            confluence_zone=_key_zone() if triggered else None,
            entry_price=2401.0 if triggered else None,
            stop_loss=2398.0 if triggered else None,
            take_profit_1=2407.0 if triggered else None,
            risk_reward_1=2.0 if triggered else None,
            passed_min_rr=triggered,
            passed_max_sl=triggered,
            rationale="Retest confluent with prior day high.",
        ),
        outcome=SignalOutcome(status=status),
    )


_BUCKET_KEY = "setup_type=order_block_retest|session=ny|htf_ltf_agreement=True"


def _losing_bucket(n: int, session: str = "ny") -> list[ScalpSignal]:
    return [_signal(f"sig-{i}", session=session, status="loss") for i in range(n)]


class _StructuredInvoker:
    def __init__(self, result: WeeklyReviewResult):
        self._result = result

    def invoke(self, messages):
        return self._result


class FakeLLM:
    def __init__(self, result: WeeklyReviewResult):
        self._result = result

    def with_structured_output(self, schema):
        assert schema is WeeklyReviewResult
        return _StructuredInvoker(self._result)


def make_journal(tmp_path, signals: list[ScalpSignal]) -> ScalpJournal:
    config = {"scalping": {"journal_path": str(tmp_path / "scalp_journal.jsonl")}}
    journal = ScalpJournal(config)
    for signal in signals:
        journal.append(signal)
    return journal


def make_store_config(tmp_path) -> dict:
    return {"scalping": {"lessons_path": str(tmp_path / "scalp_lessons.md")}}


# ---------------------------------------------------------------------------
# Python-side bucketing
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestBucketing:
    def test_untriggered_signals_excluded(self):
        signals = [_signal("sig-1", triggered=False, status="loss")]
        assert bucket_resolved_signals(signals) == {}

    def test_pending_and_win_signals_excluded(self):
        signals = [
            _signal("sig-1", status="pending"),
            _signal("sig-2", status="win"),
        ]
        assert bucket_resolved_signals(signals) == {}

    def test_loss_and_timeout_signals_bucketed_together(self):
        signals = [
            _signal("sig-1", status="loss"),
            _signal("sig-2", status="timeout"),
        ]
        buckets = bucket_resolved_signals(signals)
        assert len(buckets) == 1
        [(key, bucket)] = buckets.items()
        assert key == _BUCKET_KEY
        assert {s.signal_id for s in bucket} == {"sig-1", "sig-2"}

    def test_different_categorical_keys_produce_different_buckets(self):
        signals = [
            _signal("sig-1", session="ny"),
            _signal("sig-2", session="london"),
        ]
        buckets = bucket_resolved_signals(signals)
        assert len(buckets) == 2


# ---------------------------------------------------------------------------
# Sample-size gate
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSampleSizeGate:
    def test_below_threshold_never_calls_llm(self, tmp_path):
        journal = make_journal(tmp_path, _losing_bucket(2))
        config = {
            "scalping": {
                "journal_path": journal.journal_path and str(journal.journal_path),
                "lessons_path": str(tmp_path / "scalp_lessons.md"),
                "min_lesson_sample_size": 3,
                "max_active_lessons": 10,
            }
        }

        class ExplodingLLM:
            def with_structured_output(self, schema):
                raise AssertionError("LLM should not be invoked below the sample-size threshold")

        reflector = ScalpReflector(ExplodingLLM(), config)
        result = reflector.weekly_review(journal)
        assert result.lessons == []

    def test_at_or_above_threshold_becomes_candidate(self, tmp_path):
        journal = make_journal(tmp_path, _losing_bucket(3))
        config = {
            "scalping": {
                "journal_path": str(journal.journal_path),
                "lessons_path": str(tmp_path / "scalp_lessons.md"),
                "min_lesson_sample_size": 3,
                "max_active_lessons": 10,
            }
        }
        lesson = Lesson(
            bucket_key=_BUCKET_KEY,
            lesson_text="Order block retests during NY with HTF agreement keep losing.",
            supporting_signal_ids=["sig-0", "sig-1", "sig-2"],
            occurrences=3,
        )
        llm = FakeLLM(WeeklyReviewResult(lessons=[lesson]))
        reflector = ScalpReflector(llm, config)
        result = reflector.weekly_review(journal)
        assert [l.bucket_key for l in result.lessons] == [_BUCKET_KEY]


# ---------------------------------------------------------------------------
# Post-LLM citation validation (the hallucination guard)
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestCitationValidation:
    def _config(self, tmp_path, journal):
        return {
            "scalping": {
                "journal_path": str(journal.journal_path),
                "lessons_path": str(tmp_path / "scalp_lessons.md"),
                "min_lesson_sample_size": 3,
                "max_active_lessons": 10,
            }
        }

    def test_unknown_bucket_key_dropped(self, tmp_path, caplog):
        journal = make_journal(tmp_path, _losing_bucket(3))
        lesson = Lesson(
            bucket_key="setup_type=fvg_fill|session=asian|htf_ltf_agreement=False",
            lesson_text="Fabricated bucket.",
            supporting_signal_ids=["sig-0"],
            occurrences=1,
        )
        llm = FakeLLM(WeeklyReviewResult(lessons=[lesson]))
        reflector = ScalpReflector(llm, self._config(tmp_path, journal))
        with caplog.at_level("WARNING"):
            result = reflector.weekly_review(journal)
        assert result.lessons == []

    def test_fabricated_signal_id_dropped(self, tmp_path):
        journal = make_journal(tmp_path, _losing_bucket(3))
        lesson = Lesson(
            bucket_key=_BUCKET_KEY,
            lesson_text="Cites a signal that does not exist.",
            supporting_signal_ids=["sig-0", "sig-does-not-exist"],
            occurrences=2,
        )
        llm = FakeLLM(WeeklyReviewResult(lessons=[lesson]))
        reflector = ScalpReflector(llm, self._config(tmp_path, journal))
        result = reflector.weekly_review(journal)
        assert result.lessons == []

    def test_empty_supporting_ids_dropped(self, tmp_path):
        journal = make_journal(tmp_path, _losing_bucket(3))
        lesson = Lesson(
            bucket_key=_BUCKET_KEY,
            lesson_text="No citations at all.",
            supporting_signal_ids=[],
            occurrences=0,
        )
        llm = FakeLLM(WeeklyReviewResult(lessons=[lesson]))
        reflector = ScalpReflector(llm, self._config(tmp_path, journal))
        result = reflector.weekly_review(journal)
        assert result.lessons == []

    def test_mismatched_occurrences_dropped(self, tmp_path):
        journal = make_journal(tmp_path, _losing_bucket(3))
        lesson = Lesson(
            bucket_key=_BUCKET_KEY,
            lesson_text="Occurrences claim does not match the real bucket size.",
            supporting_signal_ids=["sig-0", "sig-1", "sig-2"],
            occurrences=5,
        )
        llm = FakeLLM(WeeklyReviewResult(lessons=[lesson]))
        reflector = ScalpReflector(llm, self._config(tmp_path, journal))
        result = reflector.weekly_review(journal)
        assert result.lessons == []

    def test_valid_lesson_survives_and_is_persisted(self, tmp_path):
        journal = make_journal(tmp_path, _losing_bucket(3))
        lesson = Lesson(
            bucket_key=_BUCKET_KEY,
            lesson_text="Valid, fully-cited lesson.",
            supporting_signal_ids=["sig-0", "sig-1", "sig-2"],
            occurrences=3,
        )
        llm = FakeLLM(WeeklyReviewResult(lessons=[lesson]))
        config = self._config(tmp_path, journal)
        reflector = ScalpReflector(llm, config)
        result = reflector.weekly_review(journal)
        assert [l.lesson_text for l in result.lessons] == ["Valid, fully-cited lesson."]

        store = ScalpLessonStore(config)
        [record] = store.read()
        assert record["bucket_key"] == _BUCKET_KEY
        assert record["occurrences"] == 3


# ---------------------------------------------------------------------------
# ScalpLessonStore rotation + persistence
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestScalpLessonStoreRotation:
    def test_add_lessons_writes_jsonl_and_markdown(self, tmp_path):
        store = ScalpLessonStore(make_store_config(tmp_path))
        lesson = Lesson(
            bucket_key=_BUCKET_KEY, lesson_text="text", supporting_signal_ids=["a"], occurrences=1
        )
        store.add_lessons([lesson], max_active_lessons=10)
        assert (tmp_path / "scalp_lessons.jsonl").exists()
        assert (tmp_path / "scalp_lessons.md").exists()
        md_text = (tmp_path / "scalp_lessons.md").read_text(encoding="utf-8")
        assert "text" in md_text
        assert _BUCKET_KEY in md_text

    def test_rotation_drops_oldest_first(self, tmp_path):
        config = make_store_config(tmp_path)
        store = ScalpLessonStore(config)
        for i in range(12):
            lesson = Lesson(
                bucket_key=f"bucket-{i}",
                lesson_text=f"lesson {i}",
                supporting_signal_ids=[f"sig-{i}"],
                occurrences=1,
            )
            store.add_lessons([lesson], max_active_lessons=10)

        records = store.read()
        assert len(records) == 10
        bucket_keys = [r["bucket_key"] for r in records]
        assert bucket_keys == [f"bucket-{i}" for i in range(2, 12)]

    def test_no_lessons_path_is_noop(self):
        store = ScalpLessonStore(config={"scalping": {}})
        lesson = Lesson(
            bucket_key=_BUCKET_KEY, lesson_text="text", supporting_signal_ids=["a"], occurrences=1
        )
        store.add_lessons([lesson], max_active_lessons=10)
        assert store.read() == []
        assert store.lessons_path is None

    def test_no_leftover_tmp_files(self, tmp_path):
        store = ScalpLessonStore(make_store_config(tmp_path))
        lesson = Lesson(
            bucket_key=_BUCKET_KEY, lesson_text="text", supporting_signal_ids=["a"], occurrences=1
        )
        store.add_lessons([lesson], max_active_lessons=10)
        assert list(tmp_path.glob("*.tmp")) == []


# ---------------------------------------------------------------------------
# load_active_lessons -- the read side of the Phase 4 prompt-injection seam
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestLoadActiveLessons:
    def test_empty_when_no_lessons(self, tmp_path):
        assert load_active_lessons(make_store_config(tmp_path)) == ""

    def test_renders_active_lessons_as_bullet_list(self, tmp_path):
        config = make_store_config(tmp_path)
        store = ScalpLessonStore(config)
        store.add_lessons(
            [
                Lesson(
                    bucket_key=_BUCKET_KEY,
                    lesson_text="Avoid this setup during NY without confluence.",
                    supporting_signal_ids=["a", "b", "c"],
                    occurrences=3,
                )
            ],
            max_active_lessons=10,
        )
        text = load_active_lessons(config)
        assert _BUCKET_KEY in text
        assert "Avoid this setup during NY without confluence." in text
