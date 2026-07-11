"""Shared trade-date/timestamp parsing for daily and intraday modes.

``trade_date`` stays a plain string end-to-end (state, prompts, memory log);
only its format widens with the timeframe: daily runs use ``YYYY-mm-dd``,
intraday (4h) runs use ``YYYY-mm-dd HH:MM`` (24h clock, UTC). This module is
the one place that turns those strings back into datetimes — callers on the
intraday path must use it instead of scattering ``strptime`` format literals.
"""

from datetime import datetime

TRADE_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M"
TRADE_DATE_FORMAT = "%Y-%m-%d"


def parse_trade_datetime(value: str) -> datetime:
    """Parse a ``trade_date`` string into a naive-UTC ``datetime``.

    Tries the intraday timestamp format first, then falls back to the daily
    date-only format (midnight). Raises ``ValueError`` for anything else.
    """
    text = str(value).strip()
    for fmt in (TRADE_TIMESTAMP_FORMAT, TRADE_DATE_FORMAT):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"Invalid trade date {value!r}: expected 'YYYY-mm-dd' or "
        f"'YYYY-mm-dd HH:MM' (24h clock, UTC)"
    )


def trade_date_only(value: str) -> str:
    """Return just the ``YYYY-mm-dd`` portion of a ``trade_date`` string.

    For consumers that are day-granular by design (news, sentiment,
    fundamentals — their vendors parse dates with ``strptime("%Y-%m-%d")``),
    so an intraday ``YYYY-mm-dd HH:MM`` trade date must be truncated at the
    call site rather than widening every day-level vendor to timestamps.
    Validates the input so a malformed trade date still fails loudly.
    """
    return parse_trade_datetime(value).strftime(TRADE_DATE_FORMAT)


def filesystem_datetime_tag(value: str) -> str:
    """Make a trade-date string safe to embed in a filename.

    ``"2026-07-08 12:00"`` -> ``"2026-07-08_12-00"``; date-only strings pass
    through unchanged, so daily-mode filenames are byte-identical to before.
    (``:`` is invalid on Windows and the space is shell-hostile.)
    """
    return str(value).replace(" ", "_").replace(":", "-")
