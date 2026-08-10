"""Reply poller entrypoint.

Run as: ``python -m titan.workers.inbound``

The counterpart to :mod:`titan.workers.outbox`. That one is the only process
holding an email provider client; this one is the only process holding a mailbox
password, and it never sends -- an automatic reply to a reply is the one thing a
system like this must not be able to do by accident.

Shuts down the same way the outbox worker does: on SIGTERM it stops starting new
poll cycles and lets the current one finish, so a deploy cannot interrupt a
batch between "recorded in Postgres" and "marked read on the server". Even if it
did, the ordering in :mod:`titan.delivery.reply_collector` makes the worst case
a re-read that deduplicates.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import uuid

from titan.config import get_settings
from titan.db.session import dispose_engine
from titan.delivery.mailbox import ImapConfig, ImapMailbox
from titan.delivery.reply_collector import ReplyCollector
from titan.observability.logging import configure_logging
from titan.runtime import configure_event_loop

logger = logging.getLogger("titan.workers.inbound")


def build_mailbox() -> tuple[ImapMailbox, str]:
    """Resolve the configured mailbox, or refuse to start.

    Returns the mailbox and the address it reads, which the collector needs in
    order to recognise Titan's own outbound copies sitting in the folder.
    """
    settings = get_settings()
    blockers = settings.reply_collection_errors()
    if blockers:
        raise RuntimeError("reply poller cannot start: " + "; ".join(blockers))

    assert settings.imap_host is not None  # narrowed by reply_collection_errors
    assert settings.imap_username is not None
    assert settings.imap_password is not None

    config = ImapConfig(
        host=settings.imap_host,
        port=settings.imap_port,
        username=settings.imap_username,
        password=settings.imap_password.get_secret_value(),
        security=settings.imap_security,
        folder=settings.imap_folder,
    )
    return ImapMailbox(config), settings.imap_username


def _default_workspace() -> uuid.UUID | None:
    settings = get_settings()
    if not settings.imap_workspace_id:
        return None
    try:
        return uuid.UUID(settings.imap_workspace_id)
    except ValueError as exc:
        raise RuntimeError(
            f"TITAN_IMAP_WORKSPACE_ID is not a UUID: {settings.imap_workspace_id!r}"
        ) from exc


async def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service="titan-inbound-worker",
        environment=settings.environment.value,
    )

    mailbox, address = build_mailbox()
    default_workspace = _default_workspace()

    # Authenticate before entering the loop. A wrong password otherwise shows up
    # as a poller that runs forever and finds nothing, which reads as "no
    # replies yet" rather than as a fault.
    ok, detail = await mailbox.health_check()
    if not ok:
        raise RuntimeError(f"cannot read mailbox {address}: {detail}")

    logger.info(
        "reply poller starting",
        extra={
            "mailbox": address,
            "folder": settings.imap_folder,
            "poll_seconds": settings.imap_poll_seconds,
            "batch_size": settings.imap_batch_size,
            "default_workspace": str(default_workspace) if default_workspace else None,
            "mailbox_check": detail,
        },
    )

    collector = ReplyCollector(
        mailbox,
        mailbox_address=address,
        default_workspace_id=default_workspace,
        batch_size=settings.imap_batch_size,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows does not support add_signal_handler for these.
            signal.signal(sig, lambda *_: stop.set())

    try:
        await collector.run_forever(stop, interval_seconds=settings.imap_poll_seconds)
    finally:
        await dispose_engine()
        logger.info("reply poller stopped cleanly", extra={"mailbox": address})


if __name__ == "__main__":
    configure_event_loop()
    asyncio.run(main())
