"""Walk the reference table against the live web.

A citation that 404s is worse than no citation: the one paragraph in the
message designed to establish that somebody checked their facts becomes proof
that nobody did. Documentation URLs rot -- MDN reorganised its HTTP tree, and
Google moves Search Central pages between sections -- so this is meant to be
run periodically, not once.

HEAD first, GET on anything that refuses it (several documentation hosts do).
A real browser User-Agent throughout: Cloudflare fronts a number of these and
answers Python's default agent with a 403, which is a fact about the agent
string and not about the link.

**A challenge is not a death.** w3.org sits behind Cloudflare's JavaScript
interstitial, which answers every scripted client with 403 and the words "Just
a moment..." however convincing the headers are. All three W3C references were
confirmed live in a real browser; reporting them as broken would train whoever
runs this to ignore its output, which is the only way a link checker fails.
They are reported as CHALLENGED and do not fail the run.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request

from titan.intelligence.references import all_references

#: Cloudflare's interstitial, which comes back with a 403 status and an HTML
#: body. Matched on the body rather than the status: a real 403 from a host
#: that has withdrawn a page is a genuine failure and must stay one.
CHALLENGE_MARKERS = (b"Just a moment", b"cf-browser-verification", b"__cf_chl")

AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"
)
TIMEOUT = 20


def status(url: str, method: str) -> tuple[int, bool]:
    """``(status_code, was_challenged)``."""
    request = urllib.request.Request(url, method=method, headers={"User-Agent": AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return int(response.status), False
    except urllib.error.HTTPError as error:
        body = b""
        try:
            body = error.read(2048)
        except Exception:
            pass
        challenged = any(marker in body for marker in CHALLENGE_MARKERS)
        return int(error.code), challenged


def main() -> int:
    bad: list[tuple[str, object]] = []
    challenges = 0
    for reference in sorted(all_references(), key=lambda r: r.url):
        try:
            code, challenged = status(reference.url, "HEAD")
            if code >= 400:
                code, challenged = status(reference.url, "GET")
        except Exception as error:  # network, TLS, DNS -- all reportable
            print(f"ERROR      {reference.url}  ({type(error).__name__}: {error})")
            bad.append((reference.url, error))
            continue
        if code < 400:
            mark = "ok"
        elif challenged:
            mark = "CHALLENGED"
            challenges += 1
        else:
            mark = "DEAD"
            bad.append((reference.url, code))
        print(f"{mark:<10} {code} {reference.url}")
    live = len(all_references()) - len(bad) - challenges
    print()
    print(f"{live} live, {challenges} challenged (verify by hand), {len(bad)} broken")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
