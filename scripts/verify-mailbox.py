#!/usr/bin/env python3
"""Check a mailbox's SMTP and IMAP credentials against the mail host.

Run this yourself, in your own terminal. It prompts for the password so the
value never reaches a shell history, a process list, a log, or an agent
transcript, and it prints only whether the login succeeded.

    python scripts/verify-mailbox.py sales@arslanvuzmallone.com

Use it before pasting a password into a sending platform. A 535 here means the
pair is wrong at the source; a success here plus a 535 in the platform means the
platform holds a different password than the one you just proved works.
"""

from __future__ import annotations

import argparse
import getpass
import imaplib
import smtplib
import ssl
import sys

DEFAULT_HOST = "mail.spacemail.com"


def check_smtp(host: str, port: int, user: str, password: str, *, implicit: bool) -> bool:
    label = f"SMTP {host}:{port} ({'SSL' if implicit else 'STARTTLS'})"
    try:
        context = ssl.create_default_context()
        if implicit:
            server = smtplib.SMTP_SSL(host, port, timeout=25, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=25)
        with server:
            server.ehlo()
            if not implicit:
                server.starttls(context=context)
                server.ehlo()
            server.login(user, password)
            print(f"  OK    {label}")
            return True
    except smtplib.SMTPAuthenticationError as exc:
        print(f"  FAIL  {label}: auth rejected -- {exc.smtp_code} {exc.smtp_error!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {label}: {type(exc).__name__}: {str(exc)[:160]}")
    return False


def check_imap(host: str, port: int, user: str, password: str) -> bool:
    label = f"IMAP {host}:{port} (SSL)"
    try:
        with imaplib.IMAP4_SSL(host, port, timeout=25) as client:
            client.login(user, password)
            print(f"  OK    {label}")
            return True
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  {label}: {type(exc).__name__}: {str(exc)[:160]}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("username", help="full email address")
    parser.add_argument("--host", default=DEFAULT_HOST)
    args = parser.parse_args()

    password = getpass.getpass(f"password for {args.username} (not echoed): ")
    if not password:
        print("no password entered", file=sys.stderr)
        return 2

    # A password that arrived through a URL, a query string or a copied link
    # may still be percent-encoded. "%2F" is "/", and pasting the encoded form
    # into a mail client is an auth failure that looks exactly like a wrong
    # password.
    if "%" in password:
        print(
            "\n  note: the password contains '%'. If it came from a URL it may be\n"
            "        percent-encoded -- %2F is '/', %20 is a space, %40 is '@'.\n"
            "        If this fails, try the decoded form.\n"
        )

    print(f"\nchecking {args.username} against {args.host}")
    results = [
        check_smtp(args.host, 465, args.username, password, implicit=True),
        check_smtp(args.host, 587, args.username, password, implicit=False),
        check_imap(args.host, 993, args.username, password),
    ]
    print()
    if all(results):
        print("All three succeeded. This password is correct at the source, so a")
        print("535 in a sending platform means the platform holds a different one.")
        return 0
    if any(results):
        print("Mixed result. SMTP and IMAP disagreeing on the same pair usually")
        print("means the account has outbound sending restricted, not a bad password.")
        return 1
    print("All three failed. The password is wrong, or the account is disabled.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
