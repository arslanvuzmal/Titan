

# ==========================================================================
# The repair pass must run while the estate is busy
#
# On 9 September 281 research workflows were started at once to re-crawl a
# batch of leads. Each is a browser crawl and an SMTP probe against a worker
# bounded at eight concurrent activities. The hourly housekeeping pass queued
# behind them and did not run -- 3,162 leads stayed parked because the job that
# would have admitted them was waiting on a crawl backlog.
# ==========================================================================
def test_every_repair_activity_runs_on_the_maintenance_queue() -> None:
    """Planted violation: drop a task_queue and that activity silently rejoins
    the crawl queue, where a busy hour starves it again.

    Read from the source rather than from a recorder: the failure is one
    activity call being forgotten, and a test that exercises the happy path
    would pass with six of seven pinned.
    """
    import pathlib
    import re

    source = pathlib.Path(
        "titan/workflows/housekeeping.py"
    ).read_text(encoding="utf-8")

    starts = re.findall(r"start_to_close_timeout=TIMEOUT,\n(\s*)([a-z_]+)=", source)

    assert starts, "no activity calls found -- has the workflow been rewritten?"
    for _indent, following in starts:
        assert following == "task_queue", (
            "an activity in the housekeeping pass is not pinned to the "
            "maintenance queue, so a crawl backlog can starve it"
        )
