#!/usr/bin/env python3
"""Install the warm-up partners: outside accounts that trade friendly mail with ColdOps.

Run from the laptop, feeding it the gitignored file of ``address app-password``
lines:

    ssh -i ~/.ssh/titan_hetzner root@168.119.161.220 \\
        "python3 /opt/coldops/deploy/partners-setup.py" < secrets/partner-passwords.txt

A partner both receives and replies, so unlike a seed it needs SMTP as well as
IMAP. Each one is logged in to over both before anything is written. An address
that is also a seed is refused: a seed has to stay a neutral witness, and one
that is also trading warm-up mail is not. Outlook is refused too, because
Microsoft no longer accepts app passwords for SMTP on personal accounts.

Writes secrets/warmup-partners.json (backup kept), points
COLDOPS_WARMUP_PARTNER_FILE at it and restarts the services that read it. The
warm-up schedule picks the partners up on its next morning run.

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
import smtplib
import ssl
import subprocess
import sys
import tempfile

ROOT = pathlib.Path("/opt/coldops")
FILE = ROOT / "secrets" / "warmup-partners.json"
SEEDS = ROOT / "secrets" / "seeds.json"
ENV = ROOT / ".env"
CONTAINER_PATH = "/run/secrets/warmup-partners.json"
SERVICES = ["api", "temporal-worker"]

#: domain -> (smtp host, imap host)
HOSTS = {
    "gmail.com": ("smtp.gmail.com", "imap.gmail.com"),
    "googlemail.com": ("smtp.gmail.com", "imap.gmail.com"),
    "yahoo.com": ("smtp.mail.yahoo.com", "imap.mail.yahoo.com"),
    "ymail.com": ("smtp.mail.yahoo.com", "imap.mail.yahoo.com"),
}
MICROSOFT = {"outlook.com", "hotmail.com", "live.com", "msn.com"}


def _hosts(domain: str) -> tuple[str, str] | None:
    if domain in HOSTS:
        return HOSTS[domain]
    if domain.startswith("yahoo."):
        return HOSTS["yahoo.com"]
    return None


def _seed_addresses() -> set[str]:
    try:
        return {s["address"].lower() for s in json.loads(SEEDS.read_text())["seeds"]}
    except (OSError, ValueError, KeyError):
        return set()


def _set_env(key: str, value: str) -> None:
    text = ENV.read_text(encoding="utf-8")
    line = f"{key}={value}"
    if re.search(rf"^{re.escape(key)}=", text, flags=re.M):
        text = re.sub(rf"^{re.escape(key)}=.*$", line, text, flags=re.M)
    else:
        text = text.rstrip("\n") + "\n" + line + "\n"
    ENV.write_text(text, encoding="utf-8")


def _check(address: str, secret: str, smtp_host: str, imap_host: str) -> str | None:
    """None when both logins work, else the reason."""
    try:
        with smtplib.SMTP(smtp_host, 587, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(address, secret)
    except Exception as exc:
        return f"smtp: {type(exc).__name__}: {str(exc)[:100]}"
    try:
        client = imaplib.IMAP4_SSL(imap_host, 993, timeout=20)
        client.login(address, secret)
        client.select("INBOX", readonly=True)
        client.logout()
    except Exception as exc:
        return f"imap: {type(exc).__name__}: {str(exc)[:100]}"
    return None


def main() -> int:
    if os.geteuid() != 0:
        print("run as root")
        return 1
    seeds = _seed_addresses()
    partners: list[dict] = []
    print("Checking each partner can send and read (nothing is sent):")
    for raw in sys.stdin.read().splitlines():
        parts = raw.replace("﻿", "").strip().split(None, 1)
        if len(parts) != 2 or "@" not in parts[0]:
            continue
        address, secret = parts[0].lower(), parts[1].replace(" ", "")
        domain = address.rsplit("@", 1)[-1]
        if address in seeds:
            print(f"  {address}: skipped -- it is a test inbox (seed); a seed must not trade warm-up mail")
            continue
        if domain in MICROSOFT:
            print(f"  {address}: skipped -- Outlook no longer accepts app passwords for sending")
            continue
        hosts = _hosts(domain)
        if hosts is None:
            print(f"  {address}: skipped -- no known mail servers for {domain}")
            continue
        problem = _check(address, secret, *hosts)
        if problem:
            print(f"  {address}: FAILED -- {problem}")
            continue
        print(f"  {address}: ok")
        partners.append(
            {
                "from_email": address,
                "label": "warm-up partner",
                "enabled": True,
                "smtp": {"host": hosts[0], "port": 587, "security": "starttls",
                         "username": address, "password": secret},
                "imap": {"host": hosts[1], "port": 993, "security": "ssl",
                         "username": address, "password": secret},
            }
        )
    if not partners:
        print("\nNo partner could log in; nothing written.")
        return 1

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    if FILE.exists():
        shutil.copy2(FILE, FILE.with_name(f"warmup-partners.json.bak-{stamp}"))
    document = {
        "_comment": ["Warm-up partners: outside accounts that receive and reply. Never outreach."],
        "mailboxes": partners,
    }
    fd, tmp = tempfile.mkstemp(dir=FILE.parent, prefix=".partners.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    os.chown(tmp, 10001, 10001)
    os.chmod(tmp, 0o640)
    os.replace(tmp, FILE)
    shutil.copy2(ENV, ENV.with_name(f".env.bak-partners-{stamp}"))
    _set_env("COLDOPS_WARMUP_PARTNER_FILE", CONTAINER_PATH)
    print(f"\nWritten {len(partners)} partner(s) to {FILE}; "
          f"COLDOPS_WARMUP_PARTNER_FILE={CONTAINER_PATH}.")
    subprocess.run([str(ROOT / "deploy" / "compose.sh"), "up", "-d", *SERVICES], check=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
