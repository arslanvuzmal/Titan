"""Outbox worker entrypoint.

Run as: ``python -m titan.workers.outbox``

This is the only process in the system that holds an email provider client.
It shuts down gracefully: on SIGTERM it stops claiming new rows and lets the
in-flight one finish, so a deploy cannot orphan a message between "provider
accepted" and "recorded as sent".
"""

from __future__ import annotations

import asyncio
import logging
import signal

from titan.config import get_settings
from titan.db.session import dispose_engine
from titan.delivery.outbox_worker import OutboxWorker, worker_identity
from titan.delivery.providers.base import EmailProvider
from titan.observability.logging import configure_logging
from titan.runtime import configure_event_loop

logger = logging.getLogger("titan.workers.outbox")


def build_provider() -> EmailProvider:
    """Resolve the configured provider.

    Defaults to the mock. Selecting a real provider requires both an explicit
    ``TITAN_EMAIL_PROVIDER`` and its credential -- a missing key is a startup
    failure rather than a silent downgrade, because a worker that quietly runs
    on the mock while the operator believes it is live is worse than one that
    refuses to start.
    """
    settings = get_settings()

    if settings.email_provider == "resend":
        if settings.resend_api_key is None:
            raise RuntimeError(
                "TITAN_EMAIL_PROVIDER=resend but TITAN_RESEND_API_KEY is not set"
            )
        from titan.delivery.providers.resend import ResendProvider

        return ResendProvider(
            api_key=settings.resend_api_key.get_secret_value(),
            webhook_secret=(
                settings.resend_webhook_secret.get_secret_value()
                if settings.resend_webhook_secret
                else None
            ),
        )

    from titan.delivery.providers.mock import MockEmailProvider

    return MockEmailProvider()


async def main() -> None:
    settings = get_settings()
    configure_logging(
        level=settings.log_level,
        service="titan-outbox-worker",
        environment=settings.environment.value,
    )

    owner = worker_identity()
    blockers = settings.sending_preflight_errors()
    logger.info(
        "outbox worker starting",
        extra={
            "owner": owner,
            "provider": settings.email_provider,
            "process_sending_enabled": settings.production_sending_enabled,
            # Logged at startup so an operator can see immediately why nothing
            # is being delivered, instead of inferring it from an empty queue.
            "preflight_blockers": blockers,
        },
    )

    provider = build_provider()
    worker = OutboxWorker(provider, settings, owner=owner)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows does not support add_signal_handler for these.
            signal.signal(sig, lambda *_: stop.set())

    try:
        await worker.run_forever(stop)
    finally:
        aclose = getattr(provider, "aclose", None)
        if aclose is not None:
            await aclose()
        await dispose_engine()
        logger.info("outbox worker stopped cleanly", extra={"owner": owner})


if __name__ == "__main__":
    configure_event_loop()
    asyncio.run(main())
