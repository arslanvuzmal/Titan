#!/usr/bin/env python3
"""Fill in the passwords in secrets/mailboxes.json, without them passing through anybody.

Run this yourself, in your own terminal:

    python scripts/fill-mailbox-passwords.py

It reads the mailbox file, prompts for each address's password without echoing
it, proves the credential against the mail server before writing anything, and
puts it into both the SMTP and IMAP blocks for that mailbox.

Nothing is printed back and nothing is logged. The value goes from your
keyboard to the file and nowhere else -- not into a shell history, not into a
process list, not into an agent transcript.

Existing passwords are left alone unless you pass --replace: pressing Enter at
a prompt skips that mailbox, so this is safe to re-run after adding one.
"""

from __future__ import annotations

import argparse
import getpass
import imaplib
import json
import pathlib
import smtplib
import ssl
import sys

DEFAULT_PATH = pathlib.Path("secrets/mailboxes.json")

#: The same list titan.delivery.mailboxes refuses. A file still carrying one of
#: these has been created and not filled in.
PLACEHOLDERS = {
    "",
    "paste_app_password_here",
    "paste-app-password-here",
    "changeme",
    "change_me",
    "your_password",
    "your-password",
    "xxxxxxxx",
    "todo",
}


def is_placeholder(value: object) -> bool:
    return str(value or "").strip().lower() in PLACEHOLDERS


def check_smtp(host: str, port: int, security: str, user: str, password: str) -> bool:
    try:
        context = ssl.create_default_context()
        if security == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=25, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=25)
        with server:
            server.ehlo()
            if security == "starttls":
                server.starttls(context=context)
                server.ehlo()
            server.login(user, password)
        return True
    except smtplib.SMTPAuthenticationError as exc:
        print(f"    SMTP {host}:{port} rejected it -- {exc.smtp_code}")
    except Exception as exc:  # noqa: BLE001
        print(f"    SMTP {host}:{port}: {type(exc).__name__}: {str(exc)[:140]}")
    return False


def check_imap(host: str, port: int, user: str, password: str) -> bool:
    try:
        with imaplib.IMAP4_SSL(host, port, timeout=25) as client:
            client.login(user, password)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"    IMAP {host}:{port}: {type(exc).__name__}: {str(exc)[:140]}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--path", type=pathlib.Path, default=DEFAULT_PATH)
    parser.add_argument(
        "--replace",
        action="store_true",
        help="also prompt for mailboxes that already have a password set",
    )
    parser.add_argument(
        "--no-check",
        action="store_true",
        help="write without proving the credential first (not recommended)",
    )
    args = parser.parse_args()

    if not args.path.exists():
        print(f"{args.path} does not exist", file=sys.stderr)
        return 2

    document = json.loads(args.path.read_text(encoding="utf-8"))
    mailboxes = document.get("mailboxes")
    if not isinstance(mailboxes, list) or not mailboxes:
        print(f"{args.path} lists no mailboxes", file=sys.stderr)
        return 2

    changed = 0
    for entry in mailboxes:
        address = entry.get("from_email", "?")
        smtp = entry.get("smtp") or {}
        imap = entry.get("imap") or {}

        already = not is_placeholder(smtp.get("password"))
        if already and not args.replace:
            print(f"{address}: password already set, leaving it (--replace to change)")
            continue

        print()
        password = getpass.getpass(f"{address} password (not echoed, Enter to skip): ")
        if not password:
            print("    skipped")
            continue

        # A password copied out of a URL may still be percent-encoded, and that
        # failure looks exactly like a wrong password.
        if "%" in password:
            print("    note: contains '%' -- if it came from a link it may be")
            print("          percent-encoded (%2F is '/', %40 is '@')")

        if not args.no_check:
            host = smtp.get("host", "")
            ok = check_smtp(
                host,
                int(smtp.get("port", 465)),
                str(smtp.get("security", "ssl")),
                str(smtp.get("username") or address),
                password,
            )
            if imap:
                ok = check_imap(
                    str(imap.get("host", host)),
                    int(imap.get("port", 993)),
                    str(imap.get("username") or address),
                    password,
                ) and ok
            if not ok:
                print("    not written -- fix the password or the account and re-run")
                continue
            print("    verified")

        smtp["password"] = password
        entry["smtp"] = smtp
        if imap:
            imap["password"] = password
            entry["imap"] = imap
        changed += 1

    if not changed:
        print("\nNothing changed.")
        return 1

    args.path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    try:
        args.path.chmod(0o600)
    except OSError:
        pass

    print(f"\nWrote {changed} password(s) into {args.path}.")
    print()
    print("Next:")
    print("  docker compose exec api python -m titan.cli mailbox check")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
