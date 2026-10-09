"""Task queue names, in one place because two of them now matter.

Everything ran on a single queue until 9 September, when it produced a failure
worth naming. 281 research workflows were started at once to re-crawl a batch
of leads; each one is a browser crawl and an SMTP probe, and the worker is
deliberately bounded at eight concurrent activities. The hourly housekeeping
pass -- the job that sweeps stranded drafts, re-checks addresses, expires old
content and re-admits parked leads -- queued behind them and did not run.

**The repair pass has to be able to run while the estate is busy.** That is
close to the only time it matters: a saturated pipeline is exactly when drafts
strand, addresses go stale and leads pile up unadmitted. A maintenance job that
yields to the work it maintains is a maintenance job that runs when nothing
needs maintaining.

So the housekeeping activities get a queue of their own, served by a second
worker in the same process. Nothing about capacity changes -- it is the same
machine, and both workers are bounded -- but a crawl backlog can no longer
starve the pass that repairs the pipeline. They are separate concerns competing
for one resource, and the queue is where that is expressed.
"""

from __future__ import annotations

#: Research, discovery, orchestration -- the pipeline's own work. Bounded hard,
#: because every activity on it is a browser crawl or somebody else's mail
#: server, and an unbounded worker starts more than the browser worker can
#: serve and then times out on all of them.
RESEARCH_QUEUE = "titan-research"

#: Housekeeping and the repair passes. Cheap, bounded database work with no
#: crawling and no outbound connections beyond the address re-check, so it can
#: run at a low concurrency alongside a saturated research queue without
#: competing for anything scarce.
MAINTENANCE_QUEUE = "titan-maintenance"

__all__ = ["MAINTENANCE_QUEUE", "RESEARCH_QUEUE"]
