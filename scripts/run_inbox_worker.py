"""
Standalone inbox worker entrypoint.

Usage:
    python -m scripts.run_inbox_worker

Or in Railway as a separate service:
    python scripts/run_inbox_worker.py

This runs the webhook inbox processor independently of the HTTP server,
so HTTP crashes don't stop webhook processing.
"""
import asyncio
import signal
import os
import sys

# Ensure the project root is on sys.path when run as a script
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import structlog
import logging

logging.basicConfig(format="%(message)s", level=logging.INFO)
structlog.configure(
    processors=[
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

from app.services.logging import get_logger
from app.services import database as db
from app.services.inbox_worker import run_worker

log = get_logger("inbox_worker_standalone")


async def main():
    log.info("standalone_worker.starting")

    # Initialize the DB pool
    await db.init_pool()
    log.info("standalone_worker.db_pool_ready")

    stop_event = asyncio.Event()

    # Handle SIGTERM/SIGINT for graceful shutdown
    def _signal_handler():
        log.info("standalone_worker.signal_received")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    try:
        await run_worker(stop_event)
    finally:
        from app.services.redis_client import close_redis
        await close_redis()
        log.info("standalone_worker.stopped")


if __name__ == "__main__":
    asyncio.run(main())
