"""
Structured logging factory for Mesio.

Uses structlog if available (preferred), falls back to stdlib logging with a
JSON-ish formatter. Import and use:

    from app.services.logging import get_logger
    log = get_logger(__name__)
    log.info("order_placed", restaurant_id=42, order_id="ORD-123")
    log.exception("inventory_deduction_failed", restaurant_id=42)  # also captures exc_info
"""

import logging
import os

_HAS_STRUCTLOG = False
try:
    import structlog  # type: ignore
    _HAS_STRUCTLOG = True
except ImportError:
    pass


# ── stdlib fallback ──────────────────────────────────────────────────────────

class _JsonishFormatter(logging.Formatter):
    """Single-line key=value formatter for easier grepping in Railway logs."""

    def format(self, record: logging.LogRecord) -> str:
        import json as _json
        import traceback as _tb

        base = {
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "pid": os.getpid(),
        }
        # Structured extras attached via LoggerAdapter.extra
        for key, val in getattr(record, "_ctx", {}).items():
            base[key] = val

        if record.exc_info:
            base["exc"] = _tb.format_exception(*record.exc_info)

        return _json.dumps(base, ensure_ascii=False, default=str)


class _ContextAdapter(logging.LoggerAdapter):
    """Wraps stdlib Logger to support keyword-argument context (structlog style)."""

    def process(self, msg, kwargs):
        extra = kwargs.pop("extra", {})
        # Capture any extra keyword arguments as structured context
        ctx = {k: v for k, v in list(kwargs.items())
               if k not in ("exc_info", "stack_info", "stacklevel")}
        for k in ctx:
            kwargs.pop(k)
        extra["_ctx"] = ctx
        kwargs["extra"] = extra
        return msg, kwargs

    # Convenience: mirror structlog's .exception() signature
    def exception(self, msg, *args, **kwargs):
        kwargs.setdefault("exc_info", True)
        self.error(msg, *args, **kwargs)


def _setup_stdlib() -> None:
    root = logging.getLogger("mesio")
    if root.handlers:
        return  # already set up
    handler = logging.StreamHandler()
    handler.setFormatter(_JsonishFormatter())
    root.addHandler(handler)
    root.setLevel(logging.DEBUG if os.getenv("DEBUG") else logging.INFO)
    root.propagate = False


# ── structlog setup ──────────────────────────────────────────────────────────

def _setup_structlog() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if os.getenv("DEBUG") else logging.INFO
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )


# ── Public factory ───────────────────────────────────────────────────────────

def get_logger(name: str, **initial_ctx):
    """
    Return a logger bound with *initial_ctx* context.

    Usage:
        log = get_logger(__name__, restaurant_id=restaurant_id)
        log.info("order_placed", order_id=order_id)
    """
    if _HAS_STRUCTLOG:
        _setup_structlog()
        return structlog.get_logger(name).bind(pid=os.getpid(), **initial_ctx)
    else:
        _setup_stdlib()
        inner = logging.getLogger(f"mesio.{name}")
        return _ContextAdapter(inner, extra={"_ctx": initial_ctx})
