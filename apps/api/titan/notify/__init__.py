"""Telling the operator something happened.

The autonomy this system is asked for only works if the person at the other end
finds out about the things that need them. A fully unattended agent that
discovers a hot lead and files it silently is worse than no agent: the reply
goes stale, and the prospect concludes nobody was listening.

See :mod:`titan.notify.operator`.
"""

from titan.notify.operator import (
    NotificationKind,
    OperatorNotification,
    push_notification,
    record_notification,
)

__all__ = [
    "NotificationKind",
    "OperatorNotification",
    "push_notification",
    "record_notification",
]
