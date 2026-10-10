#!/usr/bin/env python3
"""Install the test inboxes ("seeds") ColdOps reads to see where its mail lands.

Run from the laptop, feeding it the gitignored file of ``address app-password``
lines (spaces inside the password are fine):

    ssh -i ~/.ssh/titan_hetzner root@168.119.161.220 \\
        "python3 /opt/coldops/deploy/seeds-setup.py" < secrets/seed-passwords.txt

It replaces secrets/seeds.json with exactly the inboxes in the file (the old
one is kept as a backup), points COLDOPS_SEED_FILE at it, restarts the
services that read it, and logs in to each inbox over IMAP to prove it opens.

A seed is only ever *read*: this writes no SMTP credentials, so a seed can
never end up sending mail. Gmail and Yahoo accept app passwords over IMAP;
Outlook.com does not (Microsoft needs OAuth for personal accounts), so an
Outlook address is refused here with the reason.

Python standard library only, so it runs on the host.
"""

from __future__ import annotations

import datetime as dt
import imaplib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path("/opt/coldops")
FILE = ROOT / "secrets" / "seeds.json"
ENV = ROOT / ".env"
CONTAINER_PATH = "/run/secrets/seeds.json"
SERVICES = ["api", "temporal-worker"]

#: Provider by the address's domain -> its IMAP host.
IMAP_HOSTS = {
    "gmail.com": "imap.gmail.com",
    "googlemail.com": "imap.gmail.com",
    "yahoo.com": "imap.mail.yahoo.com",
    "ymail.com": "imap.mail.yahoo.com",
    "aol.com": "imap.aol.com",
}
MICROSOFT = {"outlook.com", "hotmail.com", "live.com", "msn.com"}


def _host_for(address: str) -> str | None:
    domain = address.rsplit("@", 1)[-1].lower()
    if domain in IMAP_HOSTS:
        return IMAP_HOSTS[domain]
    if domain.startswith("yahoo."):
        return "imap.mail.yahoo.com"
    return None


def _set_env(key: str, value: str) -> None:
    text = ENV.read_text(encoding="utf-8")
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=", text, flags=re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    ENV.write_text(text, encoding="utf-8")


def main() -> int:
    if os.geteuid() != 0:
        print("run as root")
        return 1
    seeds: list[dict] = []
    for raw in sys.stdin.read().splitlines():
        parts = raw.replace("﻿", "").strip().split(None, 1)
        if len(parts) != 2 or "@" not in parts[0]:
            continue
        address, secret = parts[0].lower(), parts[1].replace(" ", "")
        domain = address.rsplit("@", 1)[-1]
        if domain in MICROSOFT:
            print(f"  {address}: skipped -- Outlook needs Microsoft sign-in, not an app password")
            continue
        host = _host_for(address)
        if host is None:
            print(f"  {address}: skipped -- no known IMAP host for {domain}")
            continue
        seeds.append(
            {
                "address": address,
                "label": f"seed at {domain}",
                "imap": {"host": host, "port": 993, "security": "ssl", "password": secret},
            }
        )
    if not seeds:
        print("No usable seed lines; nothing written.")
        return 1

    print("Checking each inbox opens (read-only login):")
    ok = 0
    for seed in seeds:
        imap = seed["imap"]
        try:
            client = imaplib.IMAP4_SSL(imap["host"], imap["port"], timeout=20)
            client.login(seed["address"], imap["password"])
            client.select("INBOX", readonly=True)
            client.logout()
            print(f"  {seed['address']}: ok")
            ok += 1
        except Exception as exc:  # report every inbox, not just the first failure
            seed["enabled"] = False
            print(f"  {seed['address']}: FAILED -- {type(exc).__name__}: {str(exc)[:120]}")
    if ok == 0:
        print("\nNo inbox could be opened; nothing written.")
        return 1

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    if FILE.exists():
        shutil.copy2(FILE, FILE.with_name(f"seeds.json.bak-{stamp}"))
    document = {
        "_comment": ["Test inboxes ColdOps reads to see where its mail lands. Read-only: no smtp."],
        "seeds": seeds,
    }
    fd, tmp = tempfile.mkstemp(dir=FILE.parent, prefix=".seeds.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    os.chown(tmp, 10001, 10001)
    os.chmod(tmp, 0o640)
    os.replace(tmp, FILE)
    shutil.copy2(ENV, ENV.with_name(f".env.bak-seeds-{stamp}"))
    _set_env("COLDOPS_SEED_FILE", CONTAINER_PATH)
    print(f"\nWritten {ok} working seed(s) to {FILE}; COLDOPS_SEED_FILE={CONTAINER_PATH}.")

    subprocess.run([str(ROOT / "deploy" / "compose.sh"), "up", "-d", *SERVICES], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
