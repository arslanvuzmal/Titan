"""Reply poller entrypoint.

Run as: ``python -m titan.workers.inbound``

The counterpart to :mod:`titan.workers.outbox`. That one is the only process
holding an email provider client; this one is the only process holding a mailbox
password, and it never sends -- an automatic reply to a reply is the one thing a
system like this must not be able to do by accident.

**One poller per mailbox.** The sender pool sends from three addresses; a
poller reading one of them sees a third of the bounces and a third of the
unsubscribe requests, and the other two thirds look exactly like silence.
When ``TITAN_MAILBOX_FILE`` is configured, every mailbox in it that has IMAP
credentials gets its own collector and they run concurrently; the single
``TITAN_IMAP_*`` mailbox remains the path when no pool file is set.

One mailbox failing does not stop the others. A password that stopped working
on ``sales@`` must not take ``outreach@``'s unsubscribe handling down with it,
so each collector is supervised on its own and its failure is logged and
reported rather than propagated.

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
from titan.delivery.mailboxes import MailboxConfigError
from titan.delivery.reply_collector import ReplyCollector
from titan.observability.logging import configure_logging
from titan.runtime import configure_event_loop

logger = logging.getLogger("titan.workers.inbound")


def build_mailboxes() -> list[tuple[ImapMailbox, str]]:
    """Every mailbox this poller should read, with the address of each.

    The address is returned alongside because the collector needs it to
    recognise Titan's own outbound copies sitting in the folder.

    The pool file wins when it is set. It is the same file the outbox worker
    authenticates with, so a mailbox that can send is a mailbox that gets
    read -- rather than two lists of mailboxes maintained separately, which
    is how an address comes to send mail nobody is watching for replies to.
    """
    settings = get_settings()

    if settings.mailbox_file:
        from titan.delivery.mailboxes import load_mailboxes

        registry = load_mailboxes(settings.mailbox_file)
        pooled: list[tuple[ImapMailbox, str]] = []
        for account in registry.accounts():
            imap = account.imap
            if imap is None:
                continue
            pooled.append(
                (
                    ImapMailbox(
                        ImapConfig(
                            host=imap.host,
                            port=imap.port,
                            username=imap.username,
                            password=imap.password,
                            security=imap.security,
                            folder=settings.imap_folder,
                        )
                    ),
                    account.from_email,
                )
            )
        if pooled:
            unreadable = [
                account.from_email
                for account in registry.accounts()
                if account.imap is None
            ]
            if unreadable:
                # Named, because a sending mailbox nobody reads is where an
                # unsubscribe request goes to die.
                logger.warning(
                    "mailboxes can send but cannot be read; their bounces and "
                    "unsubscribe requests will not be collected",
                    extra={"mailboxes": unreadable},
                )
            return pooled

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
    return [(ImapMailbox(config), settings.imap_username)]


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

    # An unconfigured mailbox is a configuration state, not a fault.
    #
    # This used to raise, which under `restart: unless-stopped` is a permanent
    # crash loop -- 110 restarts and counting on the live stack, one container
    # flapping every thirty-five seconds and making a healthy system look like
    # it is falling over. Nothing was protected by it: raising does not collect
    # a single reply, it only makes the absence loud in the most expensive
    # possible way.
    #
    # Refusing to start was the right call when IMAP was the only intake, since
    # a quiet poller would have read as "no replies yet" rather than as "nobody
    # is listening". Replies now also arrive through
    # `collect_smartlead_replies` on the delivery poll, so the campaign path is
    # covered and this worker is the *additional* intake -- it sees mail sent
    # straight to the mailbox, outside any campaign.
    #
    # So it exits zero and says why. The log line is the signal; a container
    # that completed is not a container that failed.
    if not settings.mailbox_file:
        blockers = get_settings().reply_collection_errors()
        if blockers:
            logger.warning(
                "reply poller not started: IMAP is not configured. Campaign "
                "replies are still collected from the carrier on the delivery "
                "poll; mail sent directly to the mailbox is not.",
                extra={"blockers": blockers},
            )
            return

    try:
        mailboxes = build_mailboxes()
    except MailboxConfigError as exc:
        # Loud and finished, not a crash loop. This worker runs under a restart
        # policy, and a file with a placeholder still in it would otherwise
        # restart every thirty seconds forever -- which is how a healthy stack
        # came to look like it was falling over, 110 restarts ago. The outbox
        # worker still refuses to start on the same file, because there the
        # credential is what puts mail on the wire.
        logger.error(
            "reply poller not started: the mailbox file cannot be used",
            extra={"detail": str(exc)},
        )
        return
    if not mailboxes:
        logger.warning(
            "reply poller not started: no mailbox has IMAP credentials",
        )
        return
    default_workspace = _default_workspace()

    # Authenticate before entering the loop. A wrong password otherwise shows up
    # as a poller that runs forever and finds nothing, which reads as "no
    # replies yet" rather than as a fault.
    #
    # One bad mailbox no longer stops the process. It used to raise, which was
    # right when there was one mailbox and is wrong with three: a stale
    # password on the least important of them would otherwise stop the other
    # two from collecting anybody's unsubscribe request.
    checks = await asyncio.gather(*(mailbox.health_check() for mailbox, _ in mailboxes))
    usable: list[tuple[ImapMailbox, str]] = []
    for (mailbox, address), (ok, detail) in zip(mailboxes, checks, strict=True):
        if ok:
            usable.append((mailbox, address))
            continue
        logger.error(
            "cannot read mailbox; its replies and bounces will not be collected",
            extra={"mailbox": address, "detail": detail},
        )
    if not usable:
        raise RuntimeError(
            "no mailbox could be opened: "
            + "; ".join(
                f"{address}: {detail}"
                for (_, address), (_, detail) in zip(mailboxes, checks, strict=True)
            )
        )

    logger.info(
        "reply poller starting",
        extra={
            "mailboxes": [address for _, address in usable],
            "unreadable": len(mailboxes) - len(usable),
            "folder": settings.imap_folder,
            "poll_seconds": settings.imap_poll_seconds,
            "batch_size": settings.imap_batch_size,
            "default_workspace": str(default_workspace) if default_workspace else None,
        },
    )

    collectors = [
        ReplyCollector(
            mailbox,
            mailbox_address=address,
            default_workspace_id=default_workspace,
            batch_size=settings.imap_batch_size,
        )
        for mailbox, address in usable
    ]

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows does not support add_signal_handler for these.
            signal.signal(sig, lambda *_: stop.set())

    try:
        # return_exceptions so one collector dying is reported rather than
        # cancelling its siblings mid-batch.
        outcomes = await asyncio.gather(
            *(
                collector.run_forever(stop, interval_seconds=settings.imap_poll_seconds)
                for collector in collectors
            ),
            return_exceptions=True,
        )
        for (_, address), outcome in zip(usable, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                logger.error(
                    "reply collector stopped with an error",
                    extra={"mailbox": address, "error": repr(outcome)},
                )
    finally:
        await dispose_engine()
        logger.info(
            "reply poller stopped cleanly",
            extra={"mailboxes": [address for _, address in usable]},
        )


if __name__ == "__main__":
    configure_event_loop()
    asyncio.run(main())
