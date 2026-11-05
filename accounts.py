"""Store game bindings and owner-only credentials outside chat history.

Example:
    store = Accounts(data_directory / "accounts.sqlite3")
    store.bind(actor_key("qq", "bot", "sender"), "gs", "123456789")

The actor key identifies a platform, bot and sender, not a group. Public UID
queries never imply permission to use the queried account's credentials.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from pathlib import Path

RECORD_CONTEXT_HEADERS = frozenset(
    {
        "x-rpc-device_id",
        "x-rpc-device_fp",
        "x-rpc-app_version",
        "x-rpc-client_type",
        "User-Agent",
        "X-Requested-With",
        "Origin",
        "Referer",
    }
)


def actor_key(platform: str, bot: str, user: str) -> str:
    """Return an unambiguous owner key from trusted event identity.

    Args:
        platform: The platform instance ID.
        bot: The receiving bot's ID.
        user: The actual sender's ID.

    Returns:
        A JSON tuple suitable for an exact database key.
    """
    if not all((platform, bot, user)):
        raise ValueError("Incomplete account owner identity")
    return json.dumps([str(platform), str(bot), str(user)], separators=(",", ":"))


def game_server(game: str, uid: str) -> str:
    """Validate supported UID shapes and infer the upstream service region.

    Args:
        game: One of gs, sr or zzz.
        uid: A game UID, never a QQ user ID.

    Returns:
        The game's service-region identifier.

    Raises:
        ValueError: If the game, shape, or region cannot be identified.
    """
    uid = str(uid)
    if game not in {"gs", "sr", "zzz"}:
        raise ValueError("Unknown game")
    minimum = 7 if game == "zzz" else 8
    if not re.fullmatch(rf"[1-9]\d{{{minimum},9}}", uid):
        raise ValueError("Invalid game UID")
    if game == "zzz":
        if len(uid) == 8:
            return "prod_gf_cn"
        region = (
            {
                "10": "prod_gf_us",
                "15": "prod_gf_eu",
                "13": "prod_gf_jp",
                "17": "prod_gf_sg",
            }.get(uid[:2])
            if len(uid) == 10
            else None
        )
    else:
        prefix = uid[:-8]
        indices = {
            "1": 0,
            "2": 0,
            "5": 1,
            "6": 2,
            "7": 3,
            "8": 4,
            "9": 5,
        }
        if game == "gs":
            indices.update({"3": 0, "18": 4})
        index = indices.get(prefix)
        regions = (
            ["cn_gf01", "cn_qd01", "os_usa", "os_euro", "os_asia", "os_cht"]
            if game == "gs"
            else [
                "prod_gf_cn",
                "prod_qd_cn",
                "prod_official_usa",
                "prod_official_eur",
                "prod_official_asia",
                "prod_official_cht",
            ]
        )
        region = regions[index] if index is not None else None
    if region is None:
        raise ValueError("Game UID region cannot be identified")
    return region


class Accounts:
    """Own account bindings, defaults, credential replacement and deletion."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS accounts (
                    actor TEXT NOT NULL,
                    game TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    server TEXT NOT NULL,
                    cookies TEXT,
                    updated REAL NOT NULL,
                    PRIMARY KEY(actor, game, uid)
                );
                CREATE TABLE IF NOT EXISTS account_defaults (
                    actor TEXT NOT NULL,
                    game TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    PRIMARY KEY(actor, game)
                );
                CREATE TABLE IF NOT EXISTS record_contexts (
                    actor TEXT NOT NULL,
                    game TEXT NOT NULL,
                    uid TEXT NOT NULL,
                    headers TEXT NOT NULL,
                    credential_digest TEXT NOT NULL,
                    updated REAL NOT NULL,
                    PRIMARY KEY(actor, game, uid)
                );
            """)

    def bind(self, actor: str, game: str, uid: str, *, default: bool = False):
        """Bind a public UID without granting private-game access.

        Args:
            actor: Trusted sender scope.
            game: Game identifier.
            uid: UID to bind.
            default: Whether to select this UID as the default.
        """
        uid = str(uid)
        server = game_server(game, uid)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT INTO accounts VALUES(?,?,?,?,NULL,?) "
                "ON CONFLICT(actor,game,uid) DO UPDATE SET server=excluded.server,updated=excluded.updated",
                (actor, game, uid, server, time.time()),
            )
            if default:
                db.execute(
                    "INSERT OR REPLACE INTO account_defaults VALUES(?,?,?)",
                    (actor, game, uid),
                )
            else:
                db.execute(
                    "INSERT OR IGNORE INTO account_defaults VALUES(?,?,?)",
                    (actor, game, uid),
                )

    def list(self, actor: str, game: str | None = None) -> list[dict]:
        """Return binding metadata with no Cookie values.

        Args:
            actor: Exact owner scope.
            game: Optional game filter.

        Returns:
            Public account summaries suitable for a private status response.
        """
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT a.game,a.uid,a.server,a.cookies IS NOT NULL,a.uid=d.uid "
                "FROM accounts a LEFT JOIN account_defaults d ON a.actor=d.actor AND a.game=d.game "
                "WHERE a.actor=? AND (? IS NULL OR a.game=?) ORDER BY a.game,a.uid",
                (actor, game, game),
            ).fetchall()
        return [
            dict(zip(("game", "uid", "server", "has_credentials", "default"), row))
            for row in rows
        ]

    def for_members(
        self, platform: str, bot: str, members: set[str], game: str
    ) -> list[dict]:
        """List all bindings of verified group members without reading credentials.

        Args:
            platform: Trusted platform instance.
            bot: Receiving bot identity.
            members: Member IDs read from the current group's platform API.
            game: Requested game.

        Returns:
            Binding metadata, including non-default UIDs and a credential flag.
        """
        owners = {actor_key(platform, bot, user): user for user in members}
        keys, result = sorted(owners), []
        with sqlite3.connect(self.path) as db:
            for offset in range(0, len(keys), 300):
                batch = keys[offset : offset + 300]
                placeholders = ",".join("?" for _ in batch)
                rows = db.execute(
                    f"SELECT actor,uid,cookies IS NOT NULL FROM accounts WHERE game=? AND actor IN ({placeholders}) ORDER BY actor,uid",
                    (game, *batch),
                ).fetchall()
                result.extend(
                    {
                        "qq": owners[actor],
                        "uid": uid,
                        "has_credentials": bool(credential),
                    }
                    for actor, uid, credential in rows
                )
        return result

    def credential_owners(self) -> list[dict]:
        """List every binding that actually holds a stored Cookie.

        Scheduled work has no incoming message to derive an owner from, so this is
        the only supported way to enumerate accounts. It never returns Cookie
        values, only who they belong to.

        Returns:
            One ``{actor, game, uid}`` entry per logged-in binding, oldest first.
        """
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT actor,game,uid FROM accounts WHERE cookies IS NOT NULL ORDER BY updated,actor,game,uid"
            ).fetchall()
        return [dict(zip(("actor", "game", "uid"), row)) for row in rows]

    def selected(self, actor: str, game: str, uid: str | None = None) -> dict | None:
        """Read public binding data; an explicit UID never searches other owners.

        Args:
            actor: Exact owner scope.
            game: Game identifier.
            uid: Optional explicitly selected UID.

        Returns:
            Binding metadata, or None if it is not owned by this actor.
        """
        accounts = self.list(actor, game)
        return (
            next((a for a in accounts if a["uid"] == str(uid)), None)
            if uid
            else next((a for a in accounts if a["default"]), None)
        )

    def select(self, actor: str, game: str, uid: str):
        """Choose an owned binding without changing another owner's default.

        Raises:
            ValueError: If the UID is not bound by this owner.
        """
        if self.selected(actor, game, uid) is None:
            raise ValueError("Account is not bound by this sender")
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO account_defaults VALUES(?,?,?)",
                (actor, game, str(uid)),
            )

    def put_credentials(self, actor: str, game: str, uid: str, cookies: dict[str, str]):
        """Persist credentials only after the caller verified the game account.

        Args:
            actor: Owner established by the login request, not a model argument.
            game: Verified game identifier.
            uid: UID returned by the verified game-account lookup.
            cookies: Cookie names and values, never a public return value.
        """
        if not cookies or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in cookies.items()
        ):
            raise ValueError("Invalid credentials")
        self.bind(actor, game, uid)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "UPDATE accounts SET cookies=?,updated=? WHERE actor=? AND game=? AND uid=?",
                (json.dumps(cookies), time.time(), actor, game, str(uid)),
            )

    def credentials(self, actor: str, game: str, uid: str) -> dict[str, str] | None:
        """Read only the exact owner's credential for an internal API operation.

        Returns:
            A private Cookie mapping, or None. Never send this through chat RPC.
        """
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT cookies FROM accounts WHERE actor=? AND game=? AND uid=?",
                (actor, game, str(uid)),
            ).fetchone()
        return json.loads(row[0]) if row and row[0] else None

    def put_login(
        self,
        actor: str,
        cookies: dict[str, str],
        roles: list[dict],
        defaults: dict[str, str],
    ):
        """Atomically bind the authenticated account's roles and select defaults.

        Args:
            actor: Sender who completed this authenticated login session.
            cookies: Credentials used to obtain the authenticated role list.
            roles: Supported roles returned by the service, never supplied by chat.
            defaults: One selected UID for every game present in the role list.

        Raises:
            ValueError: If credentials, roles, regions, or defaults are inconsistent.
        """
        if not cookies or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in cookies.items()
        ):
            raise ValueError("Invalid credentials")
        encoded = json.dumps(cookies)
        if len(encoded) > 32_768 or not 1 <= len(roles) <= 100:
            raise ValueError("Invalid login accounts")
        identities = set()
        rows = []
        now = time.time()
        for role in roles:
            game, uid = role["game"], str(role["uid"])
            server = game_server(game, uid)
            if server != role["server"] or server not in {
                "cn_gf01",
                "cn_qd01",
                "prod_gf_cn",
                "prod_qd_cn",
            }:
                raise ValueError("Invalid login account region")
            identities.add((game, uid))
            rows.append((actor, game, uid, server, encoded, now))
        if (
            len(identities) != len(rows)
            or set(defaults) != {game for game, _ in identities}
            or any((game, uid) not in identities for game, uid in defaults.items())
        ):
            raise ValueError("Invalid login account defaults")
        with sqlite3.connect(self.path) as db:
            # Read the old credentials under the same write lock as replacement.
            # A renewed token for the same verified V2 account keeps its device.
            db.execute("BEGIN IMMEDIATE")
            new_digest = hashlib.sha256(
                json.dumps(cookies, sort_keys=True).encode()
            ).hexdigest()
            transfers = []
            for game, uid in identities:
                old = db.execute(
                    "SELECT a.cookies,c.headers,c.credential_digest FROM accounts a "
                    "JOIN record_contexts c USING(actor,game,uid) "
                    "WHERE a.actor=? AND a.game=? AND a.uid=?",
                    (actor, game, uid),
                ).fetchone()
                if not old or not old[0]:
                    continue
                try:
                    previous = json.loads(old[0])
                    if not isinstance(previous, dict):
                        continue
                    old_digest = hashlib.sha256(
                        json.dumps(previous, sort_keys=True).encode()
                    ).hexdigest()
                    if old_digest != old[2]:
                        continue
                    self.check_record_context(json.loads(old[1]))
                    subjects = []
                    for candidate in (previous, cookies):
                        aids = {
                            candidate[key].strip()
                            for key in (
                                "ltuid_v2",
                                "account_id_v2",
                                "ltuid",
                                "account_id",
                            )
                            if candidate.get(key)
                        }
                        mids = {
                            candidate[key].strip()
                            for key in ("ltmid_v2", "account_mid_v2")
                            if candidate.get(key)
                        }
                        subjects.append(
                            (next(iter(aids)), next(iter(mids)))
                            if len(aids) == len(mids) == 1 and "" not in aids | mids
                            else None
                        )
                    if subjects[0] is not None and subjects[0] == subjects[1]:
                        transfers.append((new_digest, actor, game, uid, old_digest))
                except (ValueError, TypeError, AttributeError):
                    # Corrupt or incomplete old metadata never blocks a valid login.
                    continue
            db.executemany(
                "INSERT INTO accounts VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(actor,game,uid) DO UPDATE SET "
                "server=excluded.server,cookies=excluded.cookies,updated=excluded.updated",
                rows,
            )
            db.executemany(
                "UPDATE record_contexts SET credential_digest=? "
                "WHERE actor=? AND game=? AND uid=? AND credential_digest=?",
                transfers,
            )
            db.executemany(
                "INSERT OR REPLACE INTO account_defaults VALUES(?,?,?)",
                [(actor, game, uid) for game, uid in defaults.items()],
            )

    @staticmethod
    def check_record_context(headers: dict) -> dict[str, str]:
        """Validate a registered context without allowing auth or routing overrides.

        Args:
            headers: Device identity and client headers from a verified context.

        Returns:
            A copy containing only permitted string headers.

        Raises:
            ValueError: If the device pair is incomplete or contains unsafe headers.
        """
        if (
            not isinstance(headers, dict)
            or not set(headers) <= RECORD_CONTEXT_HEADERS
            or not all(
                isinstance(value, str)
                and 0 < len(value) <= 1024
                and not any(character in value for character in "\r\n\0")
                for value in headers.values()
            )
            or not headers.get("x-rpc-device_id")
            or not re.fullmatch(r"[a-f0-9]{13}", headers.get("x-rpc-device_fp", ""))
        ):
            raise ValueError("Invalid record device context")
        return dict(headers)

    def put_record_context(self, actor: str, game: str, uid: str, headers: dict):
        """Save an officially registered device context for this credential owner.

        Registration does not claim that a pending 1034 challenge has been passed.

        Args:
            actor: Trusted sender scope.
            game: Game identifier.
            uid: Owned game UID.
            headers: Registered device pair and matching client headers.

        Raises:
            ValueError: If the context is invalid or this owner has no credentials.
        """
        headers = self.check_record_context(headers)
        cookies = self.credentials(actor, game, uid)
        if not cookies:
            raise ValueError("Record context requires owned credentials")
        digest = hashlib.sha256(
            json.dumps(cookies, sort_keys=True).encode()
        ).hexdigest()
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO record_contexts VALUES(?,?,?,?,?,?)",
                (actor, game, uid, json.dumps(headers), digest, time.time()),
            )

    def record_context(self, actor: str, game: str, uid: str) -> dict[str, str]:
        """Read a device context only while its exact owner's credentials match.

        Args:
            actor: Trusted sender scope.
            game: Game identifier.
            uid: Owned game UID.

        Returns:
            Matching private headers, or an empty mapping when absent or stale.
        """
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT c.headers,c.credential_digest,a.cookies FROM record_contexts c "
                "JOIN accounts a USING(actor,game,uid) "
                "WHERE c.actor=? AND c.game=? AND c.uid=?",
                (actor, game, uid),
            ).fetchone()
        if not row or not row[2]:
            return {}
        digest = hashlib.sha256(
            json.dumps(json.loads(row[2]), sort_keys=True).encode()
        ).hexdigest()
        if digest != row[1]:
            return {}
        return self.check_record_context(json.loads(row[0]))

    def unbind(self, actor: str, game: str, uid: str):
        """Remove one owner's binding and credentials, selecting a remaining UID."""
        with sqlite3.connect(self.path) as db:
            db.execute(
                "DELETE FROM record_contexts WHERE actor=? AND game=? AND uid=?",
                (actor, game, str(uid)),
            )
            db.execute(
                "DELETE FROM accounts WHERE actor=? AND game=? AND uid=?",
                (actor, game, str(uid)),
            )
            db.execute(
                "DELETE FROM account_defaults WHERE actor=? AND game=? AND uid=?",
                (actor, game, str(uid)),
            )
            remaining = db.execute(
                "SELECT uid FROM accounts WHERE actor=? AND game=? ORDER BY uid LIMIT 1",
                (actor, game),
            ).fetchone()
            if remaining:
                db.execute(
                    "INSERT OR IGNORE INTO account_defaults VALUES(?,?,?)",
                    (actor, game, remaining[0]),
                )

    def revoke(self, actor: str, game: str):
        """Delete this sender's game credentials while retaining public UID bindings."""
        with sqlite3.connect(self.path) as db:
            db.execute(
                "DELETE FROM record_contexts WHERE actor=? AND game=?", (actor, game)
            )
            db.execute(
                "UPDATE accounts SET cookies=NULL,updated=? WHERE actor=? AND game=?",
                (time.time(), actor, game),
            )
