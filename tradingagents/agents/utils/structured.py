"""Shared helpers for invoking an agent with structured output and a graceful fallback.

The Portfolio Manager, Trader, and Research Manager all follow the same
canonical pattern:

1. At agent creation, wrap the LLM with ``with_structured_output(Schema)``
   so the model returns a typed Pydantic instance. If the provider does
   not support structured output (rare; mostly older Ollama models), the
   wrap is skipped and the agent uses free-text generation instead.
2. At invocation, run the structured call and render the result back to
   markdown. If the structured call itself fails for any reason
   (malformed JSON from a weak model, transient provider issue), fall
   back to a plain ``llm.invoke`` so the pipeline never blocks.

Centralising the pattern here keeps the agent factories small and ensures
all three agents log the same warnings when fallback fires.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


def bind_structured(llm: Any, schema: type[T], agent_name: str) -> Any | None:
    """Return ``llm.with_structured_output(schema)`` or ``None`` if unsupported.

    Logs a warning when the binding fails so the user understands the agent
    will use free-text generation for every call instead of one-shot fallback.
    """
    try:
        return llm.with_structured_output(schema)
    except (NotImplementedError, AttributeError) as exc:
        logger.warning(
            "%s: provider does not support with_structured_output (%s); "
            "falling back to free-text generation",
            agent_name, exc,
        )
        return None


def invoke_structured_with_fallback(
    structured_llm: Any,
    prompt: Any,
    fallback: T,
    agent_name: str,
    attempts: int = 2,
) -> T:
    """Invoke a bound structured LLM, retrying once, then falling back to a safe typed default.

    Unlike ``invoke_structured_or_freetext`` (used by agents whose downstream
    consumer only needs rendered markdown), some pipelines read typed fields
    directly and have no free-text fallback to drop to. Local/small models
    can still answer with prose instead of the forced structured-output tool
    call -- observed with qwen3:8b via Ollama on reasoning-heavy prompts,
    even with ``tool_choice`` forced -- leaving the parser with nothing.
    Retried once since sampling is stochastic; if every attempt still comes
    back empty, returns ``fallback`` (expected to be a conservative
    "no signal" instance for that schema) instead of crashing on
    ``None`` or fabricating a directional call.
    """
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            result = structured_llm.invoke(prompt)
        except Exception as exc:
            last_exc = exc
            result = None
        if result is not None:
            return result
        logger.warning(
            "%s: structured output returned no parsed result (attempt %d/%d)%s",
            agent_name, attempt, attempts,
            f" -- {last_exc}" if last_exc else "",
        )
    logger.warning(
        "%s: falling back to a safe default after %d failed structured-output attempt(s)",
        agent_name, attempts,
    )
    return fallback


def invoke_structured_or_freetext(
    structured_llm: Any | None,
    plain_llm: Any,
    prompt: Any,
    render: Callable[[T], str],
    agent_name: str,
) -> str:
    """Run the structured call and render to markdown; fall back to free-text on any failure.

    ``prompt`` is whatever the underlying LLM accepts (a string for chat
    invocations, a list of message dicts for chat models that take that
    shape). The same value is forwarded to the free-text path so the
    fallback sees the same input the structured call did.
    """
    if structured_llm is not None:
        try:
            result = structured_llm.invoke(prompt)
            if result is None:
                # A thinking model can answer in plain text instead of calling
                # the tool, leaving the parser with nothing to return. Treat it
                # as a structured miss and fall back, with a clear reason.
                raise ValueError("structured output returned no parsed result")
            return render(result)
        except Exception as exc:
            logger.warning(
                "%s: structured-output invocation failed (%s); retrying once as free text",
                agent_name, exc,
            )

    response = plain_llm.invoke(prompt)
    return response.content
