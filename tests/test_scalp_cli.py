"""cli/scalp.py wiring tests (Phase 8).

Mocks ``create_llm_client``/``ScalpPipeline``/``ScalpJournal``/``resolve_outcome``/
``ScalpReflector`` so these run with no MT5 terminal and no real LLM -- the point is
to catch wiring bugs (wrong args passed through, missing error handling), not to
re-test the pipeline/journal/reflector internals those modules already cover.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from typer.testing import CliRunner

import cli.scalp as scalp_cli
from tradingagents.agents.utils.scalp_schemas import (
    EntryTrigger,
    HTFBias,
    KeyZone,
    LTFStructure,
    ScalpSignal,
    WeeklyReviewResult,
)
from tradingagents.dataflows.errors import NoMarketDataError

runner = CliRunner()


def _sample_signal() -> ScalpSignal:
    return ScalpSignal(
        signal_id="sig-1",
        symbol="XAUUSD.sc",
        generated_at_utc=datetime(2026, 1, 1, tzinfo=timezone.utc),
        htf_bias=HTFBias(
            bias="bullish",
            confidence="high",
            key_zones=[KeyZone(price=2350.0, label="prior day high", source_timeframe="1D")],
            rationale="HH/HL on 4H and 1H, EMA50>EMA200 both timeframes.",
            invalidation_note="Close below 2340 flips this.",
        ),
        ltf_structure=LTFStructure(
            event="BOS",
            event_price=2352.0,
            displacement_atr=0.4,
            session="london_ny_overlap",
            volatility_regime="normal",
            agrees_with_htf=True,
            confidence="high",
            tradeable=True,
            rationale="BOS confirms continuation into overlap session.",
        ),
        entry_trigger=EntryTrigger(
            trigger_type="order_block_retest",
            triggered=True,
            confluence_zone=KeyZone(price=2351.0, label="order block", source_timeframe="5m"),
            entry_price=2351.5,
            stop_loss=2349.0,
            take_profit_1=2356.0,
            take_profit_2=2360.0,
            risk_reward_1=1.8,
            passed_min_rr=True,
            passed_max_sl=True,
            rationale="Retest of the breakout order block with confluence.",
        ),
    )


class _FakeLLMClient:
    def __init__(self, provider, model, base_url=None, **kwargs):
        self.provider = provider
        self.model = model
        self.base_url = base_url

    def get_llm(self):
        return f"fake-llm[{self.provider}/{self.model}]"


class _FakePipeline:
    instances = []

    def __init__(self, llm, config):
        self.llm = llm
        self.config = config
        self.run_calls = []
        _FakePipeline.instances.append(self)

    def run(self, symbol, as_of_utc, active_lessons=""):
        self.run_calls.append((symbol, as_of_utc, active_lessons))
        return _sample_signal()


class _FakeJournal:
    instances = []

    def __init__(self, config):
        self.config = config
        self.appended = []
        self.updated = []
        self._pending = []
        self.journal_path = "/fake/scalp_journal.jsonl"
        _FakeJournal.instances.append(self)

    def append(self, signal):
        self.appended.append(signal)

    def pending_signals(self):
        return self._pending

    def update_outcome(self, signal_id, outcome):
        self.updated.append((signal_id, outcome))


class _FakeReflectorStore:
    lessons_path = "/fake/scalp_lessons.md"


class _FakeReflector:
    instances = []

    def __init__(self, llm, config):
        self.llm = llm
        self.config = config
        self.store = _FakeReflectorStore()
        self.weekly_review_calls = []
        _FakeReflector.instances.append(self)

    def weekly_review(self, journal):
        self.weekly_review_calls.append(journal)
        return WeeklyReviewResult(lessons=[])


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakePipeline.instances.clear()
    _FakeJournal.instances.clear()
    _FakeReflector.instances.clear()
    yield


class _FakeSession:
    def __enter__(self):
        return None

    def __exit__(self, *exc_info):
        return False


@pytest.fixture
def patch_collaborators(monkeypatch):
    monkeypatch.setattr(scalp_cli, "create_llm_client", lambda **kwargs: _FakeLLMClient(**kwargs))
    monkeypatch.setattr(scalp_cli, "ScalpPipeline", _FakePipeline)
    monkeypatch.setattr(scalp_cli, "ScalpJournal", _FakeJournal)
    monkeypatch.setattr(scalp_cli, "ScalpReflector", _FakeReflector)
    monkeypatch.setattr(scalp_cli, "load_active_lessons", lambda config: "")
    monkeypatch.setattr(scalp_cli.mt5_session, "session", lambda *a, **kw: _FakeSession())


def test_run_happy_path(patch_collaborators):
    result = runner.invoke(scalp_cli.scalp_app, ["run"])

    assert result.exit_code == 0, result.output
    assert "HTF Bias" in result.output
    assert "LTF Structure" in result.output
    assert "Entry Trigger" in result.output

    pipeline = _FakePipeline.instances[0]
    assert pipeline.run_calls[0][0] == pipeline.config["scalping"]["symbol"]

    journal = _FakeJournal.instances[0]
    assert len(journal.appended) == 1
    assert journal.appended[0].signal_id == "sig-1"


def test_run_overrides_symbol_and_llm(patch_collaborators):
    result = runner.invoke(
        scalp_cli.scalp_app,
        ["run", "--symbol", "XAUUSD.sc", "--llm-provider", "ollama", "--llm-model", "qwen3:8b"],
    )

    assert result.exit_code == 0, result.output
    pipeline = _FakePipeline.instances[0]
    assert pipeline.config["scalping"]["symbol"] == "XAUUSD.sc"
    assert pipeline.config["llm_provider"] == "ollama"
    assert pipeline.config["quick_think_llm"] == "qwen3:8b"
    assert pipeline.llm == "fake-llm[ollama/qwen3:8b]"
    assert pipeline.run_calls[0][0] == "XAUUSD.sc"


def test_run_vendor_error_is_clean(monkeypatch, patch_collaborators):
    class _ExplodingPipeline(_FakePipeline):
        def run(self, symbol, as_of_utc, active_lessons=""):
            raise NoMarketDataError("XAUUSD.sc", "mt5")

    monkeypatch.setattr(scalp_cli, "ScalpPipeline", _ExplodingPipeline)

    result = runner.invoke(scalp_cli.scalp_app, ["run"])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    # The pipeline raised before ScalpJournal was ever constructed -- nothing
    # got journaled.
    assert not _FakeJournal.instances


def test_review_with_no_pending_signals(patch_collaborators):
    result = runner.invoke(scalp_cli.scalp_app, ["review"])

    assert result.exit_code == 0, result.output
    assert "Resolving 0 pending signal(s)" in result.output
    assert "No new lessons" in result.output

    reflector = _FakeReflector.instances[0]
    assert len(reflector.weekly_review_calls) == 1


def test_review_resolves_pending_signals(monkeypatch, patch_collaborators):
    signal = _sample_signal()

    class _JournalWithPending(_FakeJournal):
        def __init__(self, config):
            super().__init__(config)
            self._pending = [signal]

    monkeypatch.setattr(scalp_cli, "ScalpJournal", _JournalWithPending)

    calls = []

    def _fake_resolve_outcome(sig, config):
        calls.append(sig.signal_id)
        from tradingagents.agents.utils.scalp_schemas import SignalOutcome
        return SignalOutcome(status="win", exit_price=2356.0)

    monkeypatch.setattr(scalp_cli, "resolve_outcome", _fake_resolve_outcome)

    result = runner.invoke(scalp_cli.scalp_app, ["review"])

    assert result.exit_code == 0, result.output
    assert calls == ["sig-1"]
    journal = _JournalWithPending.instances[0]
    assert journal.updated[0][0] == "sig-1"
    assert journal.updated[0][1].status == "win"
    assert "'win': 1" in result.output
