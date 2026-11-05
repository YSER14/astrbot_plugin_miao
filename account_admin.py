"""Verify a private game login from the administrator terminal, never QQ chat.

Usage:
    python -m astrbot_plugin_miao.account_admin --help

Credentials are entered with hidden terminal input, verified against the service,
and stored in the plugin's existing account database. No credential is printed.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
from http.cookies import SimpleCookie
from pathlib import Path

from .accounts import Accounts, actor_key
from .hoyolab import Hoyolab


def parse_cookie(raw: str) -> dict[str, str]:
    """Parse a pasted browser Cookie header without interpreting executable text."""
    cookie = SimpleCookie()
    cookie.load(raw)
    values = {name: item.value for name, item in cookie.items()}
    if not values or len(raw) > 32_768 or "\n" in raw or "\r" in raw:
        raise ValueError("Invalid Cookie input")
    return values


async def run(args):
    """Verify one exact platform/bot/sender binding before persisting it."""
    if args.record_context:
        try:
            if args.record_context.stat().st_size > 16_384:
                raise ValueError("Record context exceeds limit")
            headers = json.loads(args.record_context.read_text(encoding="utf-8-sig"))
            result = await Hoyolab(Accounts(args.database)).verify_record_context(
                actor_key(args.platform, args.bot, args.user),
                args.game,
                args.uid,
                headers,
            )
        except (ValueError, OSError):
            print(
                "Device context verification failed; existing login and context retained."
            )
            return 1
        print(json.dumps({"verified": True, **result}))
        return 0
    raw = getpass.getpass("Private Cookie (hidden): ")
    try:
        cookies = parse_cookie(raw)
        store = Accounts(args.database)
        await Hoyolab(store).verify_credentials(
            actor_key(args.platform, args.bot, args.user), args.game, args.uid, cookies
        )
    except ValueError as exc:
        print(str(exc))
        return 1
    print(f"Verified {args.game} UID {args.uid} for the selected sender.")
    return 0


def main():
    """Parse non-secret arguments; never accept a Cookie as a command argument."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--user", required=True)
    parser.add_argument("--game", choices=("gs", "sr", "zzz"), required=True)
    parser.add_argument("--uid", required=True)
    parser.add_argument(
        "--record-context",
        type=Path,
        help="Private JSON header file; verify all characters before saving the context.",
    )
    raise SystemExit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
