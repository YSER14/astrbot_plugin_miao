"""Import and export owned UIGF files without exposing archives to the LLM.

Example:
    python -m astrbot_plugin_miao.record_admin --help

This administrator terminal entry is independent of the pending QQ file import
flow. It does not delete the input, replace an existing export, or fetch authkeys.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .accounts import actor_key
from .wishes import Wishes


def main():
    """Run one explicit file operation against an exact sender's game records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--bot", required=True)
    parser.add_argument("--user", required=True)
    actions = parser.add_subparsers(dest="action", required=True)
    importing = actions.add_parser("import")
    importing.add_argument("--file", type=Path, required=True)
    exporting = actions.add_parser("export")
    exporting.add_argument("--file", type=Path, required=True)
    exporting.add_argument("--game", choices=("gs", "sr", "zzz"))
    exporting.add_argument("--uid")
    args = parser.parse_args()
    actor = actor_key(args.platform, args.bot, args.user)
    store = Wishes(args.database)
    try:
        if args.action == "import":
            if args.file.stat().st_size > 32 * 1024 * 1024:
                raise ValueError("Archive exceeds limit")
            document = json.loads(args.file.read_text(encoding="utf-8-sig"))
            print(json.dumps(store.import_archive(actor, document), ensure_ascii=False))
        else:
            document = store.export(actor, game=args.game, uid=args.uid)
            args.file.parent.mkdir(parents=True, exist_ok=True)
            with args.file.open("x", encoding="utf-8") as stream:
                json.dump(document, stream, ensure_ascii=False, indent=2)
            print("Owned UIGF export saved.")
    except (ValueError, OSError):
        parser.exit(
            1,
            "Record operation failed; check archive format, conflicts and file permissions.\n",
        )


if __name__ == "__main__":
    main()
