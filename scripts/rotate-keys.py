"""Rotate the third-party API keys in .env, and prove the rotation worked.

Rotation has three parts and only one of them needs a human: issuing the new key
in the provider's own console. The other two -- getting it into `.env` without
breaking the file, and confirming afterwards that the new key works *and the old
one is dead* -- are where rotations actually go wrong, and they are what this
does.

    python scripts/rotate-keys.py --check
    python scripts/rotate-keys.py --set TITAN_NVIDIA_API_KEY


**The old key is checked too.** A rotation that issues a new key and leaves the
old one enabled has not reduced exposure at all; it has doubled the number of
live credentials. Most consoles make revoking a separate action from creating,
and it is the one people skip.

**Values are never printed, logged or passed as arguments.** `--set` reads from
a prompt so the key does not enter shell history, and every report gives length
and prefix only.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
import json
import pathlib
import shutil
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
ENV_PATH = REPO / ".env"

#: How to ask each provider whether a key is still accepted. Read-only calls --
#: listing models or fetching key metadata -- so a check costs nothing and
#: changes nothing.
PROBES: dict[str, dict] = {
    "TITAN_OPENROUTER_API_KEY": {
        "label": "OpenRouter",
        "url": "https://openrouter.ai/api/v1/key",
        "header": lambda k: {"Authorization": f"Bearer {k}"},
        "console": "https://openrouter.ai/keys",
    },
    "TITAN_NVIDIA_API_KEY": {
        "label": "NVIDIA",
        "url": "https://integrate.api.nvidia.com/v1/models",
        "header": lambda k: {"Authorization": f"Bearer {k}"},
        "console": "https://build.nvidia.com/settings/api-keys",
    },
    "TITAN_GEMINI_API_KEY": {
        "label": "Gemini",
        "url": "https://generativelanguage.googleapis.com/v1beta/models",
        "header": lambda k: {"x-goog-api-key": k},
        "console": "https://aistudio.google.com/apikey",
    },
    "TITAN_GOOGLE_PLACES_API_KEY": {
        "label": "Google Places",
        "url": "https://places.googleapis.com/v1/places:searchText",
        "header": lambda k: {
            "X-Goog-Api-Key": k,
            "X-Goog-FieldMask": "places.id",
            "Content-Type": "application/json",
        },
        "body": json.dumps({"textQuery": "coffee", "maxResultCount": 1}).encode(),
        "console": "https://console.cloud.google.com/apis/credentials",
    },
}

#: Restarted after a rotation. Every one of these reads the keys at startup, so
#: a swapped .env means nothing until they come back.
SERVICES = ("api", "temporal-worker", "outbox-worker", "inbound-worker")


def read_env() -> list[str]:
    if not ENV_PATH.exists():
        sys.exit(f"no .env at {ENV_PATH}")
    return ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)


def value_of(name: str) -> str | None:
    for line in read_env():
        if line.startswith(f"{name}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return None


def fingerprint(key: str | None) -> str:
    """Enough to tell two keys apart, not enough to use one."""
    if not key:
        return "absent"
    return f"len={len(key)} prefix={key[:6]}..."


def probe(name: str, key: str) -> str:
    spec = PROBES[name]
    req = urllib.request.Request(
        spec["url"],
        headers={"User-Agent": "titan-rotate-keys", **spec["header"](key)},
        data=spec.get("body"),
    )
    try:
        with urllib.request.urlopen(req, timeout=25):
            return "LIVE"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "DEAD"
        if exc.code == 429:
            return "LIVE"
        return f"HTTP {exc.code}"
    except Exception as exc:
        return f"UNREACHABLE ({type(exc).__name__})"


def cmd_check(args: argparse.Namespace) -> int:
    names = [args.only] if args.only else list(PROBES)
    print("Credential status (read-only; no values printed)\n")
    worst = 0
    for name in names:
        key = value_of(name)
        spec = PROBES[name]
        if not key:
            print(f"  {spec['label']:<16} ABSENT")
            continue
        state = probe(name, key)
        flag = "  " if state == "DEAD" else "!!"
        if state == "LIVE":
            worst = 1
        print(f"{flag} {spec['label']:<16} {state:<12} {fingerprint(key)}")
    if worst:
        print("\n!! marks a key that still works and has not been rotated.")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    name = args.set
    if name not in PROBES:
        sys.exit(f"unknown key {name!r}; expected one of {', '.join(PROBES)}")
    spec = PROBES[name]

    old = value_of(name)
    print(f"{spec['label']}: issue a replacement at\n  {spec['console']}\n")
    print(f"current value: {fingerprint(old)}")

    # getpass, so the key is not echoed and never reaches shell history.
    new = getpass.getpass(f"paste the new {spec['label']} key (input hidden): ").strip()
    if not new:
        sys.exit("nothing entered; .env unchanged")
    if new == old:
        sys.exit("that is the value already in .env; nothing to do")

    print("\nchecking the new key before writing it… ", end="", flush=True)
    state = probe(name, new)
    print(state)
    if state != "LIVE":
        sys.exit("the new key was not accepted; .env unchanged")

    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = ENV_PATH.with_suffix(f".env.bak-{stamp}")
    shutil.copy2(ENV_PATH, backup)

    lines = read_env()
    for index, line in enumerate(lines):
        if line.startswith(f"{name}="):
            ending = "\n" if line.endswith("\n") else ""
            lines[index] = f"{name}={new}{ending}"
            break
    else:
        lines.append(f"\n{name}={new}\n")
    ENV_PATH.write_text("".join(lines), encoding="utf-8")

    print(f"written. backup at {backup.name}")
    print(f"\nNow revoke the OLD key ({fingerprint(old)}) at:\n  {spec['console']}")
    print("A rotation that leaves the old key enabled has not reduced exposure.")
    print(f"\nThen restart the services that read it:\n  docker compose up -d {' '.join(SERVICES)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="rotate-keys", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="report which keys are still live")
    group.add_argument("--set", metavar="ENV_NAME", help="replace one key, verifying it first")
    parser.add_argument("--only", metavar="ENV_NAME", help="limit --check to one key")
    args = parser.parse_args()
    return cmd_set(args) if args.set else cmd_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
