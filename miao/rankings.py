"""Project existing member panels into paged group-ranking views.

The event handler supplies verified group membership. This reader never obtains
Cookies and never changes an owner's cache. Example: Rankings(runtime).page(
    bindings, platform='qq', bot='bot', game='gs', offset=0).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .accounts import actor_key, game_server

AVATAR_FIELDS = frozenset(
    "name id elem level promote fetter costume cons talent trees weapon artis mysArtis _source _time _update _talent".split()
)


class Rankings:
    """Read only public panel fields from member-owned files."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    def page(
        self, bindings: list[dict], *, platform: str, bot: str, game: str, offset: int
    ) -> dict:
        """Return a bounded page of public panels from verified bindings.

        Args:
            bindings: Exact metadata authorized for this original group event.
            platform: Trusted platform instance.
            bot: Receiving bot.
            game: Requested gs or sr game.
            offset: Next position in the stable binding list.

        Returns:
            Public player snapshots, total bindings, and a continuation offset.

        Raises:
            ValueError: If game or pagination is invalid.
        """
        if (
            game not in {"gs", "sr"}
            or type(offset) is not int
            or not 0 <= offset <= len(bindings)
        ):
            raise ValueError("Invalid group ranking page")
        credential_uids = {
            str(binding["uid"])
            for binding in bindings
            if binding.get("has_credentials")
        }
        players, size, cursor = [], 0, offset
        while cursor < len(bindings) and cursor - offset < 8:
            binding = bindings[cursor]
            uid, qq = str(binding["uid"]), str(binding["qq"])
            game_server(game, uid)
            owner = hashlib.sha256(actor_key(platform, bot, qq).encode()).hexdigest()
            directory = self.root / "instances" / owner
            path = directory / "data/PlayerData" / game / (uid + ".json")
            try:
                if any(
                    part.is_symlink()
                    for part in (
                        directory,
                        directory / "data",
                        directory / "data/PlayerData",
                        path.parent,
                        path,
                    )
                ):
                    raise ValueError("Panel symlink is not allowed")
                if (
                    not path.resolve().is_relative_to(directory)
                    or path.stat().st_size > 2 * 1024 * 1024
                ):
                    raise ValueError("Invalid panel file")
                raw = json.loads(path.read_text(encoding="utf-8-sig"))
                if str(raw.get("uid", uid)) != uid:
                    raise ValueError("Panel UID does not match its binding")
                avatars = {}
                for ident, avatar in raw.get("avatars", {}).items():
                    if (
                        not str(ident).isdigit()
                        or not isinstance(avatar, dict)
                        or str(avatar.get("id")) != str(ident)
                    ):
                        continue
                    public = {
                        key: value
                        for key, value in avatar.items()
                        if key in AVATAR_FIELDS
                    }
                    weapon = public.get("weapon")
                    if isinstance(weapon, dict):
                        public["weapon"] = {
                            key: value
                            for key, value in weapon.items()
                            if key in {"id", "name", "level", "promote", "affix"}
                        }
                    avatars[str(ident)] = public
                if not avatars:
                    raise ValueError("No cached panel")
                player = {
                    key: raw[key]
                    for key in ("name", "level", "face", "_profile")
                    if key in raw
                }
                player.update(uid=uid, avatars=avatars)
                entry = {
                    "qq": qq,
                    "uid": uid,
                    "has_credentials": uid in credential_uids,
                    "player": player,
                    "stamp": path.stat().st_mtime,
                }
                length = len(
                    json.dumps(entry, ensure_ascii=False, allow_nan=False).encode()
                )
                if players and size + length > 4 * 1024 * 1024:
                    break
                players.append(entry)
                size += length
            except (OSError, ValueError, TypeError, AttributeError):
                pass
            cursor += 1
        return {
            "players": players,
            "bindings": len(bindings),
            "next_offset": cursor if cursor < len(bindings) else None,
        }
