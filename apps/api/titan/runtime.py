"""Process-level runtime setup that must happen before any async I/O.

Currently this exists for one reason: on Windows, Python's default asyncio
policy is :class:`ProactorEventLoop`, which psycopg 3 cannot drive in async
mode. Without this shim every database call fails with::

    psycopg.InterfaceError: Psycopg cannot use the 'ProactorEventLoop' to run
    in async mode.

The owner develops on Windows 11 while the containers run Linux, so the fix
belongs in the application rather than in each developer's shell. It is a no-op
on POSIX.

Import order matters: call :func:`configure_event_loop` before the first
``asyncio.run`` / ``uvicorn.run`` in every entrypoint (API, workers, CLI,
Alembic env).
"""

from __future__ import annotations

import asyncio
import sys


def configure_event_loop() -> None:
    """Install an asyncio policy compatible with psycopg's async mode."""
    if sys.platform != "win32":
        return
    selector_policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if selector_policy is None:  # pragma: no cover - non-Windows
        return
    if isinstance(asyncio.get_event_loop_policy(), selector_policy):
        return
    asyncio.set_event_loop_policy(selector_policy())


__all__ = ["configure_event_loop"]
