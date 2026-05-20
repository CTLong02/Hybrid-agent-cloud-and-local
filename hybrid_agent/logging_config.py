"""Structured logging setup.

All output flows through structlog so existing `logging.getLogger(__name__)`
calls keep working but get enriched with the contextvars bound by
`trace_context` — task_id, stage, attempt, run_id, etc. — without each module
having to thread those values manually.

Two sinks:
  - Console (always): RichHandler with the same color scheme as before.
  - JSON file (optional): one JSON object per line, ready for Loki / ELK.

Wire it up once at startup:
    setup_logging(level="INFO", log_file="agent.log", json_file="agent.jsonl")

Then bind context per pipeline stage:
    with trace_context(task_id="T001", stage="plan"):
        log.info("planning starts")
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import structlog
from rich.console import Console
from rich.logging import RichHandler

# Public contextvar — bind via trace_context().  Default is None (not `{}`)
# to avoid the shared-mutable-default anti-pattern; readers below treat None
# as "no bindings".
_trace_ctx: ContextVar[dict[str, Any] | None] = ContextVar("hybrid_agent_trace", default=None)


def _merge_trace(
    _: Any,
    __: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """structlog processor: merge our contextvar bindings into the event."""
    ctx = _trace_ctx.get()
    if ctx:
        for k, v in ctx.items():
            event_dict.setdefault(k, v)
    return event_dict


def _drop_color_message_key(
    _: Any,
    __: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """RichHandler injects 'color_message'; drop it before JSON renders."""
    event_dict.pop("color_message", None)
    return event_dict


def setup_logging(
    level: str = "INFO",
    log_file: str | None = None,
    json_file: str | None = None,
    console: Console | None = None,
) -> None:
    """Configure root logging once.

    Idempotent: calling again replaces existing handlers (useful for tests).

    Args:
        level: Log level name (DEBUG / INFO / WARNING / ERROR).
        log_file: If set, write a plain text copy to this file (legacy format).
        json_file: If set, write JSON-lines to this file (production format).
        console: Pre-built Rich Console (the CLI passes its shared one).
    """
    level_int = getattr(logging, level.upper(), logging.INFO)

    timestamper = structlog.processors.TimeStamper(fmt="iso", utc=True)

    # Processors that run on records coming from stdlib logging (foreign
    # to structlog) before they hit the formatter's renderer.
    foreign_pre_chain: list = [
        structlog.contextvars.merge_contextvars,
        _merge_trace,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        timestamper,
        _drop_color_message_key,
    ]

    # ---- Console handler (Rich, colored) ------------------------------------
    console_handler = RichHandler(
        console=console or Console(),
        show_path=False,
        rich_tracebacks=True,
        markup=False,
    )
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer(colors=False),
            foreign_pre_chain=foreign_pre_chain,
        )
    )

    handlers: list[logging.Handler] = [console_handler]

    # ---- Plain text file (legacy --log-file) --------------------------------
    if log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.dev.ConsoleRenderer(colors=False),
                foreign_pre_chain=foreign_pre_chain,
            )
        )
        handlers.append(fh)

    # ---- JSON file (production log shipping) --------------------------------
    if json_file:
        jh = logging.FileHandler(json_file, encoding="utf-8")
        jh.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                processor=structlog.processors.JSONRenderer(),
                foreign_pre_chain=foreign_pre_chain,
            )
        )
        handlers.append(jh)

    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in handlers:
        root.addHandler(h)
    root.setLevel(level_int)

    # Quiet noisy deps
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("watchfiles").setLevel(logging.WARNING)

    # Native structlog calls (`structlog.get_logger(...)`) use the same
    # processor chain, ending in wrap_for_formatter so the stdlib handler
    # renders them consistently with foreign records.
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _merge_trace,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            timestamper,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """structlog logger. `log.info("msg", task_id=...)` works natively.

    Existing `logging.getLogger(__name__)` callers don't need to switch — the
    same context bindings still appear because both go through the same
    handler chain.
    """
    return structlog.stdlib.get_logger(name)


@contextmanager
def trace_context(**bindings: Any) -> Iterator[None]:
    """Bind keys onto the trace contextvar for the duration of the block.

    contextvars propagate through `await` and `asyncio.create_task`, so a
    pipeline coroutine can bind once and every nested log call across every
    module sees the bindings.

    None values are skipped so callers can pass optional fields freely.
    """
    current = _trace_ctx.get() or {}
    new = {**current, **{k: v for k, v in bindings.items() if v is not None}}
    token = _trace_ctx.set(new)
    try:
        yield
    finally:
        _trace_ctx.reset(token)


def new_run_id() -> str:
    """Short random run identifier for grouping all logs from one CLI invocation."""
    return uuid.uuid4().hex[:8]


def get_trace_field(key: str) -> Any:
    """Read one field from the current trace contextvar. Returns None if unset.

    Used by sibling modules (e.g. cost.py) that want to attribute work to the
    currently-active task without taking it as an explicit argument.
    """
    ctx = _trace_ctx.get()
    return (ctx or {}).get(key)
