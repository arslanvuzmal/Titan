"""The hourly check that mails the operator once the day is done.

Hourly rather than daily, and that is the whole point. "After sending all
quota" is a condition, not a clock: the send windows on this workspace run
from Sydney to Vancouver, so there is no hour at which sending is reliably
finished. Checking every hour lets the report go out within the hour of the
last message actually leaving, on a day that spends its quota by noon as well
as on one that is still going at eleven.

The cost of checking hourly is one bounded read; the activity claims the day
before it mails, so twenty-four checks produce exactly one report.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from titan.activities.daily_report import DailyReportResult
    from titan.workflows.types import DailyReportInput

#: Reading the day and sending one mail.
TIMEOUT = timedelta(minutes=5)

#: Bounded. A failed send releases its claim, so the next hour tries again --
#: retrying hard against a mail server having a bad minute buys nothing that
#: waiting an hour does not.
RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=3,
)

#: Every hour at :47. Off the hour and away from the other jobs: housekeeping
#: runs at :17 and the delivery-event poll at :25, and this wants to read a
#: day the poll has already brought bounces back for.
DEFAULT_CRON = "47 * * * *"


@workflow.defn(name="DailyReportWorkflow")
class DailyReportWorkflow:
    """Mail the operator the day's sending, once, when the day is over."""

    @workflow.run
    async def run(self, request: DailyReportInput) -> DailyReportResult:
        return await workflow.execute_activity(
            "send_daily_report",
            request,
            start_to_close_timeout=TIMEOUT,
            retry_policy=RETRY,
            result_type=DailyReportResult,
        )


__all__ = ["DEFAULT_CRON", "DailyReportWorkflow"]
