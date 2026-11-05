"""Store owned gacha records and exchange UIGF archives without losing large IDs.

UIGF v4.0-v4.2 and the earlier Genshin/SRGF containers have separate readers.
Imports validate the entire document before opening the write transaction. A
duplicate never silently replaces a different record. Times remain raw server
strings; the archive's timezone is stored separately, never inferred locally.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from .accounts import game_server

GAME_KEYS = {"gs": "hk4e", "sr": "hkrpg", "zzz": "nap"}
POOLS = {
    "gs": {"100", "200", "301", "302", "400", "500"},
    "sr": {"1", "2", "11", "12", "21", "22"},
    "zzz": {"1", "2", "3", "5"},
}
SECRET_KEYS = {
    "authkey",
    "cookie",
    "cookies",
    "ltoken",
    "ltoken_v2",
    "stoken",
    "stoken_v2",
    "cookie_token",
    "cookie_token_v2",
}


def reject_secrets(value, *, depth: int = 0):
    """Reject credential-bearing archives rather than copying secrets to exports."""
    if depth > 20:
        raise ValueError("Archive nesting exceeds limit")
    if isinstance(value, dict):
        if any(str(key).lower() in SECRET_KEYS for key in value):
            raise ValueError("Archive contains credential fields")
        for child in value.values():
            reject_secrets(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            reject_secrets(child, depth=depth + 1)


def parse_archive(document: dict) -> list[dict]:
    """Validate supported account containers and preserve each original record.

    Returns:
        Normalized account entries. Unsupported games are not imported.

    Raises:
        ValueError: If a supported entry is invalid or has incomplete identity.
    """
    if (
        not isinstance(document, dict)
        or len(json.dumps(document, ensure_ascii=False)) > 32 * 1024 * 1024
    ):
        raise ValueError("Invalid archive or archive exceeds limit")
    reject_secrets(document)
    info = document.get("info")
    if not isinstance(info, dict):
        raise ValueError("Archive info is missing")
    groups = []
    version = info.get("version")
    if version in {"v4.0", "v4.1", "v4.2"}:
        for game, key in GAME_KEYS.items():
            entries = document.get(key, [])
            if not isinstance(entries, list):
                raise ValueError("Invalid game account list")
            groups.extend((game, entry) for entry in entries)
    elif info.get("uigf_version") in {"v2.2", "v2.3", "v2.4", "v3.0"}:
        groups.append(
            (
                "gs",
                {
                    "uid": info.get("uid"),
                    "timezone": info.get("region_time_zone"),
                    "lang": info.get("lang", "zh-cn"),
                    "list": document.get("list"),
                },
            )
        )
    elif info.get("srgf_version") == "v1.0":
        groups.append(
            (
                "sr",
                {
                    "uid": info.get("uid"),
                    "timezone": info.get("region_time_zone"),
                    "lang": info.get("lang", "zh-cn"),
                    "list": document.get("list"),
                },
            )
        )
    else:
        raise ValueError("Unsupported archive version")
    result = []
    total = 0
    seen_accounts = set()
    for game, entry in groups:
        if not isinstance(entry, dict):
            raise ValueError("Invalid game account")
        uid = str(entry.get("uid") or "")
        game_server(game, uid)
        timezone = entry.get("timezone")
        if type(timezone) is not int or not -12 <= timezone <= 14:
            raise ValueError("Archive server timezone is missing or invalid")
        lang = entry.get("lang", "zh-cn")
        if not isinstance(lang, str) or not re.fullmatch(r"[a-z]{2}-[a-z]{2}", lang):
            raise ValueError("Invalid archive language")
        records = entry.get("list")
        if not isinstance(records, list):
            raise ValueError("Archive record list is missing")
        total += len(records)
        if total > 250_000:
            raise ValueError("Archive record count exceeds limit")
        if (game, uid) in seen_accounts:
            raise ValueError("Duplicate game account container")
        seen_accounts.add((game, uid))
        cleaned = []
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("Invalid gacha record")
            record = dict(record)
            ident = record.get("id")
            if not isinstance(ident, str) or not re.fullmatch(r"[0-9]{1,19}", ident):
                raise ValueError("Record ID must be an exact decimal string")
            if record.get("uid") is not None and str(record["uid"]) != uid:
                raise ValueError("Record UID does not match its account")
            pool = record.get("gacha_type")
            if pool not in POOLS[game]:
                raise ValueError("Unsupported or invalid gacha pool type")
            if (
                not isinstance(record.get("item_id"), str)
                or not record["item_id"].isdigit()
            ):
                raise ValueError(
                    "Item IDs are required; legacy names must be resolved before import"
                )
            if not isinstance(record.get("time"), str) or not re.fullmatch(
                r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", record["time"]
            ):
                raise ValueError("Invalid raw record time")
            try:
                datetime.strptime(record["time"], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                raise ValueError("Invalid raw record time") from None
            if game == "gs":
                shared = "301" if pool == "400" else pool
                if record.get("uigf_gacha_type", shared) != shared:
                    raise ValueError("Inconsistent Genshin pity pool")
                record["uigf_gacha_type"] = shared
            elif game == "sr" and not isinstance(record.get("gacha_id"), str):
                raise ValueError("Star Rail banner ID is required")
            for optional in ("name", "item_type", "rank_type", "count", "gacha_id"):
                if optional in record and not isinstance(record[optional], str):
                    raise ValueError("Record text fields must use string values")
            cleaned.append(record)
        result.append(
            {
                "game": game,
                "uid": uid,
                "timezone": timezone,
                "lang": lang,
                "list": cleaned,
            }
        )
    if not result:
        raise ValueError("Archive has no supported game accounts")
    return result


class Wishes:
    """Keep complete history per trusted sender, game, UID and exact record ID."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS wish_accounts (
                    actor TEXT NOT NULL, game TEXT NOT NULL, uid TEXT NOT NULL,
                    timezone INTEGER NOT NULL, lang TEXT NOT NULL,
                    PRIMARY KEY(actor,game,uid)
                );
                CREATE TABLE IF NOT EXISTS wishes (
                    actor TEXT NOT NULL, game TEXT NOT NULL, uid TEXT NOT NULL,
                    id TEXT NOT NULL, pool TEXT NOT NULL, raw TEXT NOT NULL,
                    PRIMARY KEY(actor,game,uid,id)
                );
            """)

    def import_archive(self, actor: str, document: dict) -> dict:
        """Import all supported accounts atomically, rejecting conflicting records."""
        groups = parse_archive(document)
        inserted = duplicate = 0
        with sqlite3.connect(self.path) as db:
            for group in groups:
                scope = (actor, group["game"], group["uid"])
                metadata = db.execute(
                    "SELECT timezone,lang FROM wish_accounts WHERE actor=? AND game=? AND uid=?",
                    scope,
                ).fetchone()
                if metadata and metadata != (group["timezone"], group["lang"]):
                    raise ValueError(
                        "Existing history uses a different server timezone or language"
                    )
                db.execute(
                    "INSERT OR IGNORE INTO wish_accounts VALUES(?,?,?,?,?)",
                    (*scope, group["timezone"], group["lang"]),
                )
                for record in group["list"]:
                    raw = json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    existing = db.execute(
                        "SELECT raw FROM wishes WHERE actor=? AND game=? AND uid=? AND id=?",
                        (*scope, record["id"]),
                    ).fetchone()
                    if existing:
                        if existing[0] != raw:
                            raise ValueError(
                                "Duplicate ID conflicts with existing record; history was not changed"
                            )
                        duplicate += 1
                    else:
                        db.execute(
                            "INSERT INTO wishes VALUES(?,?,?,?,?,?)",
                            (*scope, record["id"], record["gacha_type"], raw),
                        )
                        inserted += 1
        return {
            "accounts": len(groups),
            "inserted": inserted,
            "duplicates": duplicate,
            "skipped_sections": [
                key
                for key in document
                if key not in {"info", "list", *GAME_KEYS.values()}
            ],
        }

    def export(
        self, actor: str, *, game: str | None = None, uid: str | None = None
    ) -> dict:
        """Export only this sender's selected histories in the current UIGF container."""
        result = {
            "info": {
                "export_timestamp": int(time.time()),
                "export_app": "AstrBot Miao",
                "export_app_version": "0.1.0-dev1",
                "version": "v4.2",
            }
        }
        with sqlite3.connect(self.path) as db:
            groups = db.execute(
                "SELECT game,uid,timezone,lang FROM wish_accounts WHERE actor=? AND (? IS NULL OR game=?) AND (? IS NULL OR uid=?) ORDER BY game,uid",
                (actor, game, game, uid, uid),
            ).fetchall()
            for key, account_uid, timezone, lang in groups:
                rows = db.execute(
                    "SELECT raw FROM wishes WHERE actor=? AND game=? AND uid=? ORDER BY length(id) DESC,id DESC",
                    (actor, key, account_uid),
                ).fetchall()
                result.setdefault(GAME_KEYS[key], []).append(
                    {
                        "uid": account_uid,
                        "timezone": timezone,
                        "lang": lang,
                        "list": [json.loads(row[0]) for row in rows],
                    }
                )
        return result

    def pools(self, actor: str, game: str, uid: str) -> dict[str, list[dict]]:
        """Return one chronological record list per pool for the upstream renderer.

        The pinned MIT worker reads its own per-pool JSON files and identifies an
        item by ``name`` plus ``item_type``; a record without both cannot be drawn
        or counted there. Only such displayable records are returned, so callers
        must not treat this as a count of everything stored -- ``summary`` is the
        honest total.

        Args:
            actor: The trusted sender key that owns these records.
            game: One of gs, sr or zzz.
            uid: The exact game UID the records belong to.

        Returns:
            Pool identifier to records, oldest first by raw server time.
        """
        grouped: dict[str, list[dict]] = {}
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT pool,raw FROM wishes WHERE actor=? AND game=? AND uid=?",
                (actor, game, uid),
            ).fetchall()
        for pool, raw in rows:
            record = json.loads(raw)
            if not record.get("name") or not record.get("item_type"):
                continue
            # GachaData sorts by Date(time); the raw server string parses as local time.
            grouped.setdefault(pool, []).append(record)
        for records in grouped.values():
            records.sort(key=lambda item: (item["time"], item["id"]))
        return grouped

    def summary(self, actor: str, game: str, uid: str) -> list[dict]:
        """Show every stored pool, including SR collaboration pools 21 and 22."""
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT pool,count(*) FROM wishes WHERE actor=? AND game=? AND uid=? GROUP BY pool ORDER BY pool",
                (actor, game, uid),
            ).fetchall()
        return [{"pool": pool, "count": count} for pool, count in rows]
