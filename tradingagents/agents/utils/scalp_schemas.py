"""Pydantic schemas for the gold-scalping pipeline (Phase 3).

Full design: docs/plans/gold-scalping-mt5/PLAN.md. These are the LLM-facing
structured-output schemas for the three scalp analysts (mirrors
tradingagents/agents/schemas.py's role for the equity pipeline, via the same
``bind_structured``/``with_structured_output`` mechanism in
tradingagents/agents/utils/structured.py) plus the weekly-reflection schemas
consumed by Phase 7. No dependency on scalp_features.py or scalp_tools.py --
``KeyZone`` only needs a ``.price`` attribute, which is exactly what
scalp_features.confluence_distance already accepts duck-typed.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------------------
# Step 1 -- HTF (4H/1H) bias & key levels
# ---------------------------------------------------------------------------


class KeyZone(BaseModel):
    """A single price level relevant to bias/confluence.

    Populated from deterministic candidates (prior day/week high-low, round
    numbers, pivot points -- see scalp_features.py) that the LLM selects and
    ranks rather than invents; ``label``/``source_timeframe`` describe where
    the level came from, e.g. ``label="prior day high"``,
    ``source_timeframe="1D"``.
    """

    price: float = Field(description="The key level's price.")
    label: str = Field(
        description=(
            "Human-readable description of the level's origin, e.g. "
            "'prior day high', 'round number', 'pivot R1'."
        ),
    )
    source_timeframe: str = Field(
        description="Timeframe the level was derived from, e.g. '4H', '1H', '1D'.",
    )


class HTFBias(BaseModel):
    """Step 1 structured output: combined 4H+1H directional bias."""

    bias: Literal["bullish", "bearish", "range"] = Field(
        description=(
            "Overall higher-timeframe directional bias. 'range' when 4H/1H "
            "structure and EMA stack disagree or are compressed -- do not "
            "force a direction."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence in the bias call. 'high' only when 4H and 1H "
            "structure, EMA stack alignment, and ATR regime all agree; "
            "'low' when signals conflict or data is thin."
        ),
    )
    key_zones: list[KeyZone] = Field(
        default_factory=list,
        description=(
            "Ranked list (most relevant first) of key price levels from the "
            "supplied candidates that are likely to matter for this bias."
        ),
    )
    rationale: str = Field(
        description="Concise justification citing the specific structure/EMA/ATR evidence used.",
    )
    invalidation_note: str = Field(
        description="What price action would flip this bias (e.g. a specific close beyond a level).",
    )


# ---------------------------------------------------------------------------
# Step 2 -- 15m trend alignment & structure shift
# ---------------------------------------------------------------------------


class LTFStructure(BaseModel):
    """Step 2 structured output: 15m structure/session read.

    ``agrees_with_htf`` and ``tradeable`` are both filled by the LLM as
    best-effort reads but are always overwritten by the pipeline
    (``ltf_structure_analyst.py``) with deterministic values -- same "LLM
    proposes, Python verifies" pattern as ``EntryTrigger.passed_min_rr``/
    ``passed_max_sl`` below. ``tradeable`` is recomputed from ``confidence``
    (the LLM's actual judgment call) plus a hard session/volatility gate via
    ``scalp_tools.compute_ltf_tradeable``, thresholded against
    ``scalping.min_ltf_confidence`` -- so a single "no event this bar" read
    no longer has to collapse straight to a binary skip.
    """

    event: Literal["BOS", "CHoCH", "none"] = Field(
        description="Structure event on the latest 15m close, per detect_bos_choch.",
    )
    event_price: float | None = Field(
        default=None,
        description="Close price of the event bar, or null when event is 'none'.",
    )
    displacement_atr: float | None = Field(
        default=None,
        description="Displacement of the event beyond its broken level, in ATR(15m) units.",
    )
    session: str = Field(
        description=(
            "Session classification of the latest bar, per session_window "
            "(e.g. 'london', 'ny', 'london_ny_overlap', 'asian', 'off_session')."
        ),
    )
    volatility_regime: Literal["low", "normal", "high", "abnormal_spike"] = Field(
        description=(
            "'low' = chop (skip), 'abnormal_spike' = likely news event (skip "
            "unless explicitly trading news), 'normal'/'high' = tradeable."
        ),
    )
    agrees_with_htf: bool = Field(
        default=False,
        description=(
            "Whether 15m structure agrees with the Step-1 HTF bias direction. "
            "Best-effort read; the pipeline recomputes this deterministically "
            "and overwrites it regardless of what is filled in here."
        ),
    )
    confidence: Literal["low", "medium", "high"] = Field(
        description=(
            "Confidence that this setup is worth proceeding to Step 3. "
            "'high' only when session/volatility are clean and there is "
            "either a fresh, well-displaced BOS/CHoCH or strong HTF "
            "alignment; 'medium' when the case is mixed but still workable; "
            "'low' when there is no event, signals conflict, or evidence is "
            "thin. This is the actual judgment call -- the pipeline "
            "thresholds it against scalping.min_ltf_confidence rather than "
            "trusting a self-reported tradeable flag."
        ),
    )
    tradeable: bool = Field(
        default=False,
        description=(
            "Whether the pipeline proceeded to Step 3. Best-effort read; "
            "the pipeline always overwrites this deterministically from "
            "session/volatility hard gates plus scalping.min_ltf_confidence "
            "thresholded against ``confidence`` above -- never trust the "
            "LLM's own value here."
        ),
    )
    rationale: str = Field(
        description="Concise justification citing the specific event/session/volatility evidence used.",
    )


# ---------------------------------------------------------------------------
# Step 3 + 4 -- 5m entry trigger, invalidation & targets
# ---------------------------------------------------------------------------


class EntryTrigger(BaseModel):
    """Step 3+4 structured output: 5m entry trigger, stop-loss, targets.

    ``passed_min_rr``/``passed_max_sl`` are filled by the LLM as its own
    read but the pipeline recomputes both deterministically from the
    proposed ``entry_price``/``stop_loss``/``take_profit_1`` and forces them
    to ``False`` if the LLM's numbers don't actually pass (PLAN.md's
    "deterministic verification, not LLM self-policing").
    """

    trigger_type: Literal[
        "liquidity_sweep", "order_block_retest", "fvg_fill", "ema_pullback_momentum", "none"
    ] = Field(
        description="Which 5m trigger pattern (if any) fired on the latest bar(s).",
    )
    triggered: bool = Field(
        description="Whether a valid, confluent entry trigger fired.",
    )
    confluence_zone: KeyZone | None = Field(
        default=None,
        description=(
            "Nearest Step-1/Step-2 key zone the trigger is confluent with. "
            "Required (non-null) when triggered is True -- a trigger with no "
            "confluence must be rejected (triggered=False), not reported "
            "with a null zone."
        ),
    )
    entry_price: float | None = Field(default=None, description="Proposed entry price.")
    stop_loss: float | None = Field(
        default=None,
        description="Proposed stop-loss: structural invalidation point + ATR(5m) buffer.",
    )
    take_profit_1: float | None = Field(default=None, description="Proposed first target.")
    take_profit_2: float | None = Field(
        default=None, description="Proposed second target (R-multiple ladder or further HTF level)."
    )
    risk_reward_1: float | None = Field(
        default=None, description="Proposed reward:risk ratio to take_profit_1."
    )
    passed_min_rr: bool = Field(
        default=False,
        description=(
            "Whether risk_reward_1 meets scalping.min_risk_reward. Best-effort "
            "read; the pipeline recomputes this deterministically and "
            "overwrites it regardless of what is filled in here."
        ),
    )
    passed_max_sl: bool = Field(
        default=False,
        description=(
            "Whether the SL distance is within scalping.max_sl_atr_multiple. "
            "Best-effort read; the pipeline recomputes this deterministically "
            "and overwrites it regardless of what is filled in here."
        ),
    )
    rationale: str = Field(
        description="Concise justification citing the specific trigger/confluence/SL evidence used.",
    )

    @model_validator(mode="after")
    def _confluence_required_when_triggered(self) -> "EntryTrigger":
        if self.triggered and self.confluence_zone is None:
            raise ValueError("confluence_zone is required when triggered=True")
        return self


# ---------------------------------------------------------------------------
# Phase 6 -- walk-forward outcome resolution
# ---------------------------------------------------------------------------


class SignalOutcome(BaseModel):
    """Walk-forward resolution of one ScalpSignal's entry against later bars.

    Starts ``pending`` at signal-creation time (before any forward bars can
    exist) and is only ever moved forward by ``scalp_journal.resolve_outcome``,
    never re-opened. ``timeout`` means the full ``max_holding_bars_5m``
    window was scanned with no SL/TP hit (excluded from win/loss stats, kept
    visible in the journal); ``pending`` means not enough bars exist yet and
    the same signal is retried by a later run (same deferred-resolution
    idiom as ``TradingAgentsGraph._resolve_pending_entries``).
    """

    status: Literal["pending", "win", "loss", "timeout"] = "pending"
    exit_price: float | None = Field(
        default=None,
        description="Price at which SL or TP1 was hit; null while pending/timeout.",
    )
    exit_bar_time_utc: datetime | None = Field(
        default=None, description="UTC time of the bar that resolved this outcome."
    )
    bars_held: int | None = Field(
        default=None, description="Number of forward 5m bars scanned before resolution."
    )
    gap_through: bool = Field(
        default=False,
        description=(
            "True when the resolving bar arrived after an unusually large "
            "time gap (weekend/holiday reopen already past the level), "
            "False for an ordinary intrabar SL/TP touch -- lets weekly "
            "review separate 'bad read' losses from unavoidable gap losses."
        ),
    )
    resolved_at_utc: datetime | None = Field(
        default=None, description="When this outcome was computed; null while still pending."
    )


# ---------------------------------------------------------------------------
# Top-level artifact
# ---------------------------------------------------------------------------


class ScalpSignal(BaseModel):
    """Top-level decision-support artifact produced by one ScalpPipeline run."""

    signal_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    symbol: str
    generated_at_utc: datetime
    htf_bias: HTFBias
    ltf_structure: LTFStructure
    entry_trigger: EntryTrigger
    outcome: SignalOutcome = Field(default_factory=SignalOutcome)


# ---------------------------------------------------------------------------
# Render helpers -- carry a step's structured output forward as text context
# for the next analyst's prompt (Phase 4's per-stage message clearing means
# state fields, not accumulated messages, are how later steps see earlier
# ones -- see scalp_pipeline.py).
# ---------------------------------------------------------------------------


def render_htf_bias(bias: HTFBias) -> str:
    """Render an HTFBias to a compact text block for downstream analyst prompts."""
    zones = "\n".join(
        f"  - {z.price} -- {z.label} ({z.source_timeframe})" for z in bias.key_zones
    ) or "  (none)"
    return "\n".join([
        f"Bias: {bias.bias} (confidence: {bias.confidence})",
        f"Rationale: {bias.rationale}",
        f"Invalidation: {bias.invalidation_note}",
        "Key zones:",
        zones,
    ])


def render_ltf_structure(structure: LTFStructure) -> str:
    """Render an LTFStructure to a compact text block for the entry analyst's prompt."""
    displacement = (
        f"{structure.displacement_atr} ATR" if structure.displacement_atr is not None else "n/a"
    )
    event = f"{structure.event}"
    if structure.event_price is not None:
        event += f" at {structure.event_price}"
    return "\n".join([
        f"Event: {event}",
        f"Displacement: {displacement}",
        f"Session: {structure.session}",
        f"Volatility regime: {structure.volatility_regime}",
        f"Agrees with HTF bias: {structure.agrees_with_htf}",
        f"Confidence: {structure.confidence}",
        f"Tradeable: {structure.tradeable}",
        f"Rationale: {structure.rationale}",
    ])


def render_entry_trigger(trigger: EntryTrigger) -> str:
    """Render an EntryTrigger to a compact text block for logging/journal display."""
    lines = [
        f"Trigger type: {trigger.trigger_type}",
        f"Triggered: {trigger.triggered}",
    ]
    if trigger.confluence_zone is not None:
        lines.append(
            f"Confluence zone: {trigger.confluence_zone.price} -- "
            f"{trigger.confluence_zone.label} ({trigger.confluence_zone.source_timeframe})"
        )
    if trigger.entry_price is not None:
        lines.append(f"Entry: {trigger.entry_price}")
    if trigger.stop_loss is not None:
        lines.append(f"Stop loss: {trigger.stop_loss}")
    if trigger.take_profit_1 is not None:
        lines.append(f"Take profit 1: {trigger.take_profit_1}")
    if trigger.take_profit_2 is not None:
        lines.append(f"Take profit 2: {trigger.take_profit_2}")
    if trigger.risk_reward_1 is not None:
        lines.append(f"Risk:reward (TP1): {trigger.risk_reward_1}")
    lines.append(f"Passed min R:R: {trigger.passed_min_rr}")
    lines.append(f"Passed max SL: {trigger.passed_max_sl}")
    lines.append(f"Rationale: {trigger.rationale}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Phase 7 -- weekly reflection
# ---------------------------------------------------------------------------


class Lesson(BaseModel):
    """A single candidate lesson, gated by Python-side sample-size/citation checks (Phase 7).

    ``supporting_signal_ids`` and ``occurrences`` are cross-checked against
    the Python-computed bucket after the LLM call; a lesson whose citations
    don't check out is dropped, not trusted at face value.
    """

    bucket_key: str = Field(
        description=(
            "The fixed categorical bucket this lesson generalizes over, e.g. "
            "'setup_type=order_block_retest|session=ny|htf_ltf_agreement=False'."
        ),
    )
    lesson_text: str = Field(description="The actionable lesson, grounded in the cited signals.")
    supporting_signal_ids: list[str] = Field(
        description="signal_id values from the bucket that support this lesson.",
    )
    occurrences: int = Field(
        description="Count of supporting signals; must match the Python-computed bucket size.",
    )


class WeeklyReviewResult(BaseModel):
    """Structured output of ScalpReflector.weekly_review()."""

    lessons: list[Lesson] = Field(default_factory=list)
