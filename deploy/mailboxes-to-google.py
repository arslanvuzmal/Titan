#!/usr/bin/env python3
"""Point every ColdOps mailbox at Google Workspace, asking for each app password.

Run it yourself, in a terminal, on the server:

    ssh -t -i ~/.ssh/titan_hetzner root@168.119.161.220 \\
        python3 /opt/coldops/deploy/mailboxes-to-google.py

For each mailbox in secrets/mailboxes.json it shows the address and asks for
that user's Google *app password* (16 letters; spaces are ignored). Input is
hidden and never printed. Press Enter on an empty line to switch that mailbox
off instead -- a Spacemail password must never be sent to Google, and a
mailbox with no Google password cannot work there anyway.

Then it:
  - keeps a copy of the old file (mailboxes.json.bak-google-<time>)
  - writes smtp.gmail.com:587 starttls and imap.gmail.com:993 ssl
  - writes the file atomically, owned by uid 10001, mode 640
  - restarts the four services that read it
  - runs `coldops mailbox check`, which logs in to each mailbox and says
    which ones work

Nothing is sent. Python standard library only, so it runs on the host.

Without a terminal, ``--from-stdin`` reads lines of ``address app-password``
instead (spaces inside the password are fine), e.g. from a file kept in the
laptop's gitignored secrets/ folder:

    ssh ... "python3 /opt/coldops/deploy/mailboxes-to-google.py --from-stdin" < app-passwords.txt

A mailbox with no line is switched off, exactly as an empty answer would be.
"""

from __future__ import annotations

import datetime as dt
import getpass
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path("/opt/coldops")
FILE = ROOT / "secrets" / "mailboxes.json"
SMTP = {"host": "smtp.gmail.com", "port": 587, "security": "starttls"}
IMAP = {"host": "imap.gmail.com", "port": 993, "security": "ssl"}
SERVICES = ["api", "outbox-worker", "inbound-worker", "temporal-worker"]


def _read_stdin() -> dict[str, str]:
    """``address password`` per line; the password may contain spaces."""
    given: dict[str, str] = {}
    for line in sys.stdin.read().splitlines():
        parts = line.replace("﻿", "").strip().split(None, 1)
        if len(parts) == 2 and "@" in parts[0]:
            given[parts[0].lower()] = parts[1]
    return given


def main() -> int:
    if os.geteuid() != 0:
        print("run as root")
        return 1
    from_stdin = "--from-stdin" in sys.argv[1:]
    given = _read_stdin() if from_stdin else {}
    if not from_stdin and not sys.stdin.isatty():
        print("needs a terminal (ssh -t), or pass --from-stdin with a file")
        return 1
    data = json.loads(FILE.read_text(encoding="utf-8"))
    boxes = data["mailboxes"]
    print(f"{len(boxes)} mailboxes in {FILE}\n")

    changed = 0
    for box in boxes:
        address = box["from_email"]
        if from_stdin:
            secret = given.get(address.lower(), "")
        else:
            secret = getpass.getpass(f"{address}  Google app password (Enter = switch off): ")
        secret = secret.replace(" ", "").strip()
        if not secret:
            box["enabled"] = False
            print(f"  {address}: switched off")
            continue
        if len(secret) != 16 or not secret.isalpha():
            print(f"  {address}: that is not a 16-letter app password; switched off")
            box["enabled"] = False
            continue
        for side, target in (("smtp", SMTP), ("imap", IMAP)):
            block = box.setdefault(side, {})
            block.update(target)
            block["username"] = address
            block["password"] = secret
        box.pop("auth", None)
        box["enabled"] = True
        changed += 1
        print(f"  {address}: Google")

    if changed == 0:
        print("\nNo app passwords given; nothing written.")
        return 1

    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    backup = FILE.with_name(f"mailboxes.json.bak-google-{stamp}")
    shutil.copy2(FILE, backup)
    fd, tmp = tempfile.mkstemp(dir=FILE.parent, prefix=".mailboxes.")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    os.chown(tmp, 10001, 10001)
    os.chmod(tmp, 0o640)
    os.replace(tmp, FILE)
    print(f"\nWritten. Old file kept as {backup.name}.")

    compose = str(ROOT / "deploy" / "compose.sh")
    subprocess.run([compose, "restart", *SERVICES], check=True)
    print("\nChecking each mailbox can log in (nothing is sent):")
    return subprocess.run(
        ["docker", "exec", "deploy-api-1", "python", "-m", "coldops.cli", "mailbox", "check"]
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
