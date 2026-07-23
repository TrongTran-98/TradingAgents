"""Thin CLI for the gold-scalping pipeline (Phase 8 of docs/plans/gold-scalping-mt5).

No business logic lives here — every command delegates straight to
``ScalpPipeline``/``ScalpJournal``/``ScalpReflector``/``resolve_outcome``. This is a
decision-support pipeline only: nothing here (or in anything it calls) places an
MT5 order.
"""

from __future__ import annotations

from datetime import datetime, timezone

import typer
from rich.console import Console
from rich.panel import Panel

from tradingagents.agents.utils.scalp_schemas import (
    render_entry_trigger,
    render_htf_bias,
    render_ltf_structure,
)
from tradingagents.dataflows import mt5_session
from tradingagents.dataflows.config import get_config
from tradingagents.dataflows.errors import VendorError
from tradingagents.dataflows.scalp_journal import ScalpJournal, resolve_outcome
from tradingagents.graph.scalp_pipeline import ScalpPipeline
from tradingagents.graph.scalp_reflection import ScalpReflector, load_active_lessons
from tradingagents.llm_clients.factory import create_llm_client

console = Console()

scalp_app = typer.Typer(
    name="scalp",
    help="Gold (XAUUSD) MT5 scalping pipeline -- decision support only, no order placement.",
)


def _build_config(
    symbol: str | None,
    llm_provider: str | None,
    llm_model: str | None,
    backend_url: str | None,
) -> dict:
    """Assemble the run config from CLI overrides, honoring 'explicit flag wins'.

    Starts from ``get_config()`` (already a deep copy -- see
    ``tradingagents/dataflows/config.py``), safe to mutate in place. An
    unset (``None``) option leaves whatever DEFAULT_CONFIG/env already put
    there, same precedence rule ``cli/main.py`` uses for its own options.
    """
    config = get_config()
    if symbol:
        config["scalping"]["symbol"] = symbol
    if llm_provider:
        config["llm_provider"] = llm_provider.lower()
    if llm_model:
        config["quick_think_llm"] = llm_model
    if backend_url:
        config["backend_url"] = backend_url
    return config


def _build_llm(config: dict):
    """One combined LLM for all three scalp analysts (no deep/quick split -- see PLAN.md)."""
    client = create_llm_client(
        provider=config["llm_provider"],
        model=config["quick_think_llm"],
        base_url=config.get("backend_url"),
    )
    return client.get_llm()


def _print_signal(signal) -> None:
    console.print(
        Panel(render_htf_bias(signal.htf_bias), title="Step 1 -- HTF Bias", border_style="blue")
    )
    console.print(
        Panel(
            render_ltf_structure(signal.ltf_structure),
            title="Step 2 -- LTF Structure",
            border_style="magenta",
        )
    )
    console.print(
        Panel(
            render_entry_trigger(signal.entry_trigger),
            title="Step 3+4 -- Entry Trigger",
            border_style="green",
        )
    )
    console.print(
        f"[dim]signal_id={signal.signal_id} "
        f"generated_at_utc={signal.generated_at_utc.isoformat()}[/dim]"
    )


@scalp_app.command("run")
def run(
    symbol: str | None = typer.Option(
        None, "--symbol", help="Symbol to analyze (default: config's scalping.symbol)."
    ),
    llm_provider: str | None = typer.Option(
        None, "--llm-provider", help="LLM provider, e.g. 'ollama' (default: config's llm_provider)."
    ),
    llm_model: str | None = typer.Option(
        None, "--llm-model", help="Model name, e.g. 'qwen3:8b' (default: config's quick_think_llm)."
    ),
    backend_url: str | None = typer.Option(
        None, "--backend-url", help="Override the provider's backend URL."
    ),
):
    """Run the scalp pipeline once, print the signal, and append it to the journal."""
    config = _build_config(symbol, llm_provider, llm_model, backend_url)
    resolved_symbol = config["scalping"]["symbol"]

    console.print(
        f"[cyan]Running scalp pipeline for {resolved_symbol} "
        f"using {config['llm_provider']}/{config['quick_think_llm']}...[/cyan]"
    )

    try:
        llm = _build_llm(config)
        pipeline = ScalpPipeline(llm, config)
        active_lessons = load_active_lessons(config)
        # The pipeline's tools call mt5_vendor directly (PLAN.md's deliberate
        # deviation from interface.py's vendor routing); nothing upstream of
        # this CLI ever calls mt5_session.connect(), so this is the one place
        # that must -- attaches to an already-running, already-logged-in
        # terminal (Phase 2/8's manual-verification precondition).
        with mt5_session.session():
            signal = pipeline.run(
                resolved_symbol, datetime.now(timezone.utc), active_lessons=active_lessons
            )
    except VendorError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    _print_signal(signal)

    journal = ScalpJournal(config)
    journal.append(signal)
    if journal.journal_path:
        console.print(f"[green]Appended to journal:[/green] {journal.journal_path}")
    else:
        console.print(
            "[yellow]No scalping.journal_path configured -- signal was not persisted.[/yellow]"
        )


@scalp_app.command("review")
def review(
    llm_provider: str | None = typer.Option(
        None, "--llm-provider", help="LLM provider, e.g. 'ollama' (default: config's llm_provider)."
    ),
    llm_model: str | None = typer.Option(
        None, "--llm-model", help="Model name, e.g. 'qwen3:8b' (default: config's quick_think_llm)."
    ),
    backend_url: str | None = typer.Option(
        None, "--backend-url", help="Override the provider's backend URL."
    ),
):
    """Resolve pending journal entries against real MT5 bars, then run weekly reflection."""
    config = _build_config(None, llm_provider, llm_model, backend_url)
    journal = ScalpJournal(config)

    pending = journal.pending_signals()
    console.print(f"[cyan]Resolving {len(pending)} pending signal(s)...[/cyan]")

    counts = {"win": 0, "loss": 0, "timeout": 0, "pending": 0}
    try:
        if pending:
            # resolve_outcome fetches forward bars from MT5
            # (mt5_vendor.get_mt5_rates_range) unless bars are supplied
            # directly -- same session-lifecycle need as `run`. Skipped
            # entirely when there's nothing to resolve.
            with mt5_session.session():
                for signal in pending:
                    outcome = resolve_outcome(signal, config)
                    journal.update_outcome(signal.signal_id, outcome)
                    counts[outcome.status] += 1
    except VendorError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc

    console.print(f"[green]Outcome resolution:[/green] {counts}")

    llm = _build_llm(config)
    reflector = ScalpReflector(llm, config)
    result = reflector.weekly_review(journal)

    if not result.lessons:
        console.print(
            "[yellow]No new lessons this review (no bucket met the sample-size "
            "threshold, or none passed validation).[/yellow]"
        )
    else:
        console.print(f"[green]{len(result.lessons)} new lesson(s) written:[/green]")
        for lesson in result.lessons:
            console.print(f"  - [{lesson.bucket_key}] {lesson.lesson_text} (n={lesson.occurrences})")

    if reflector.store.lessons_path:
        console.print(f"[dim]Lessons file: {reflector.store.lessons_path}[/dim]")
