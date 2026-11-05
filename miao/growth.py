"""Own Star Rail growth targets while retaining Miao's Genshin calculator.

This service projects the Star Rail calculator's avatar/equipment/skill contract.
It does not call the SDK's Genshin Calculator or invent an account's current
levels when the authenticated record endpoint failed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .accounts import Accounts
from .hoyolab import Hoyolab, positive_id
from .messages import Incoming

HELP = "星铁养成：*养成计算 流萤 80 80 6 10 10 10\n也可：*流萤养成 80 80 6 10 10 10\n依次为角色、光锥、普攻、战技、终结技、天赋目标；未填项目按最高等级计算。\n末尾加「全行迹」才会把额外行迹算进去（默认不算）。"
TRACE_NAMES = {
    "Point01": "普攻",
    "Point02": "战技",
    "Point03": "终结技",
    "Point04": "天赋",
}
# Extra trace nodes are binary unlocks; they stay out unless the user asks.
EXTRA_FLAGS = ("全行迹", "全部行迹", "额外行迹", "解锁行迹")


@dataclass(frozen=True)
class Targets:
    """Keep explicit target levels separate from authenticated current levels."""

    name: str
    levels: tuple[int, ...] = (80, 80, 6, 10, 10, 10)
    extras: bool = False

    @classmethod
    def parse(cls, text: str):
        """Parse the native prefix and legacy character-before-growth syntax."""
        body = re.sub(r"^#星铁", "", text).strip()
        extras = False
        for flag in EXTRA_FLAGS:
            if body.endswith(flag):
                body = body[: -len(flag)].rstrip()
                extras = True
                break
        prefix = re.fullmatch(r"(?:养成|培养)计算\s*(.+)", body)
        if prefix:
            match = re.fullmatch(
                r"(.+?)(?:\s+(\d+(?:[\s,，/]+\d+)*))?", prefix[1].strip()
            )
        else:
            match = re.fullmatch(r"(.+?)(?:养成|培养)(?:计算)?\s*([\d\s,，/]*)", body)
        if not match or not match[1].strip():
            raise ValueError("Invalid growth command")
        numbers = [int(value) for value in re.findall(r"\d+", match[2] or "")]
        maximum = (80, 80, 6, 10, 10, 10)
        if len(numbers) > 6 or any(
            not 1 <= value <= maximum[index] for index, value in enumerate(numbers)
        ):
            raise ValueError("Invalid growth target level")
        return cls(match[1].strip(), tuple(numbers) + maximum[len(numbers) :], extras)


def growth_body(
    character_id: int, detail: dict, owned: dict | None, targets: Targets
) -> dict:
    """Build rpgcalc input from verified levels and preserve minor trace targets."""
    avatar = detail.get("avatar", {})
    if str(avatar.get("item_id")) != str(character_id):
        raise ValueError("Calculator returned a different character")
    current = avatar.get("cur_level")
    if type(current) is not int or not 1 <= current <= 80:
        raise ValueError("Current character level is unavailable")
    skills = detail.get("skills")
    if not isinstance(skills, list) or not skills:
        raise ValueError("Trace details are unavailable")
    major = {
        skill.get("anchor"): skill
        for skill in skills
        if skill.get("anchor") in {"Point01", "Point02", "Point03", "Point04"}
    }
    if set(major) != {"Point01", "Point02", "Point03", "Point04"} or len(skills) != 4:
        raise ValueError("Trace target mapping needs an update")
    major_targets = {
        positive_id(major[f"Point{index + 1:02}"]["point_id"]): targets.levels[
            index + 2
        ]
        for index in range(4)
    }
    if len(major_targets) != 4:
        raise ValueError("Duplicate trace IDs")
    result = {
        "game": "hkrpg",
        "avatar": {
            "item_id": character_id,
            "cur_level": current,
            "target_level": max(current, targets.levels[0]),
        },
        "skill_list": [],
    }
    for skill in skills:
        ident = positive_id(skill.get("point_id"))
        current = skill.get("cur_level")
        maximum = skill.get("max_level")
        if (
            type(current) is not int
            or type(maximum) is not int
            or not 0 <= current <= maximum <= 15
        ):
            raise ValueError("Trace levels are unavailable")
        desired = major_targets[ident]
        if type(desired) is not int or not 0 <= desired <= maximum:
            raise ValueError("Trace target exceeds its actual maximum")
        result["skill_list"].append(
            {
                "item_id": ident,
                "cur_level": current,
                "target_level": max(current, desired),
            }
        )
    if targets.extras:
        # Extra traces are binary unlocks. They are opt-in because the calculator
        # may reject the additional node IDs; the caller reports the service answer.
        seen = {positive_id(skill.get("point_id")) for skill in skills}
        for extra in detail.get("skills_other") or []:
            ident = positive_id(extra.get("point_id"))
            current = extra.get("cur_level")
            maximum = extra.get("max_level")
            if (
                type(current) is not int
                or type(maximum) is not int
                or not 0 <= current <= maximum <= 15
            ):
                raise ValueError("Extra trace levels are unavailable")
            if ident in seen:
                raise ValueError("Duplicate extra trace IDs")
            seen.add(ident)
            result["skill_list"].append(
                {"item_id": ident, "cur_level": current, "target_level": maximum}
            )
    if (owned or {}).get("equip"):
        equipment = detail.get("equipment", {})
        if str(equipment.get("item_id")) != str(owned["equip"].get("id")):
            raise ValueError("Equipped light cone changed during the query")
        level = equipment.get("cur_level")
        if type(level) is not int or not 1 <= level <= 80:
            raise ValueError("Current light cone level is unavailable")
        result["equipment"] = {
            "item_id": positive_id(equipment.get("item_id")),
            "cur_level": level,
            "target_level": max(level, targets.levels[1]),
        }
    return result


def materials(result: dict) -> list[dict]:
    """Aggregate the calculator's item rows without converting or rounding counts."""
    if not isinstance(result, dict):
        raise ValueError("Invalid material result")
    totals = {}
    for key in ("avatar_consume", "skill_consume", "equipment_consume"):
        rows = result.get(key, [])
        if not isinstance(rows, list):
            raise ValueError("Invalid material result section")
        for row in rows:
            if not isinstance(row, dict) or "num" not in row:
                raise ValueError("Incomplete material row")
            ident = str(positive_id(row.get("item_id")))
            number = row["num"]
            if isinstance(number, bool) or not str(number).isdigit():
                raise ValueError("Invalid material amount")
            amount = int(number)
            if ident not in totals:
                totals[ident] = {
                    "id": ident,
                    "name": str(row.get("item_name") or ident),
                    "amount": 0,
                }
            totals[ident]["amount"] += amount
    return list(totals.values())


def inventory(result: dict, rows: list[dict]) -> list[dict]:
    """Join the account's owned counts onto the aggregated rows.

    The totals from :func:`materials` stay untouched: owned and missing amounts
    are a separate view, so a material can never be counted twice. Currency is
    skipped because "owning" credit says nothing about a material shortage.

    Args:
        result: The raw calculator response.
        rows: Aggregated rows returned by :func:`materials`.

    Returns:
        One record per row that has a usable owned count.
    """
    owned = result.get("user_owns_materials")
    if not isinstance(owned, dict):
        return []
    coin = result.get("coin_id")
    skip = str(coin) if coin is not None else None
    stocks = []
    for row in rows:
        if skip and row["id"] == skip:
            continue
        raw = owned.get(row["id"])
        if isinstance(raw, bool) or not isinstance(raw, (int, str)):
            continue
        text = str(raw)
        if not text.isdigit():
            continue
        have = int(text)
        stocks.append(
            {
                "id": row["id"],
                "name": row["name"],
                "need": row["amount"],
                "owned": have,
                "lack": max(0, row["amount"] - have),
            }
        )
    return stocks


class Growth:
    """Resolve a Miao character alias, query this sender, then render owned output."""

    def __init__(self, accounts: Accounts, gateway: Hoyolab, engine):
        self.accounts = accounts
        self.gateway = gateway
        self.engine = engine

    @staticmethod
    def matches(event: Incoming) -> bool:
        """Claim Star Rail growth only; leave Genshin to its original handler."""
        return event.game == "sr" and bool(
            re.fullmatch(
                r"#星铁(?:(?:养成|培养)计算.*"
                r"|.+?(?:养成|培养)(?:计算)?[\d\s,，/]*(?:全行迹|全部行迹|额外行迹|解锁行迹)?)",
                event.text,
            )
        )

    async def handle(self, event: Incoming, reply):
        """Send a deterministic game result; no LLM participates in calculation."""
        try:
            target = Targets.parse(event.text)
        except ValueError:
            await reply(HELP)
            return
        if any(user not in {event.self_id, event.user_id} for user in event.mentions):
            await reply("养成背包查询仅操作你本人绑定并登录的账号。")
            return
        account = self.accounts.selected(event.actor, "sr")
        if not account:
            await reply("请先绑定星铁UID并扫码登录。")
            return
        uid = account["uid"]
        if not self.accounts.credentials(event.actor, "sr", uid):
            await reply("星铁养成背包查询需要你本人登录米游社。")
            return
        character = await self.engine.request(
            "character", {"game": "sr", "name": target.name}
        )
        if not character or character.get("game") != "sr":
            await reply("没有找到这个星铁角色，请检查角色名或别名。")
            return
        record = await self.gateway.query(event.actor, "sr", uid, "avatarInfo")
        if record["retcode"] != 0:
            await reply(record["message"])
            return
        candidates = character.get("wire_candidates", [character["id"]])
        owned = next(
            (
                item
                for item in record["data"].get("avatar_list", [])
                if str(item.get("id")) in {str(value) for value in candidates}
            ),
            None,
        )
        wire_id = positive_id(
            (owned or {}).get("id") or character.get("wire_id", character["id"])
        )
        detail = await self.gateway.query(
            event.actor,
            "sr",
            uid,
            "detail",
            {
                "avatar_id": wire_id,
                "tab_from": "TabOwned" if owned else "TabAll",
            },
        )
        if detail["retcode"] != 0:
            await reply(detail["message"])
            return
        if character.get("enhanced") and not any(
            str(skill.get("point_id", "")).startswith("1" + str(wire_id))
            for skill in detail["data"].get("skills", [])
        ):
            await reply(
                "接口未返回这个强化形态的行迹，暂时无法可靠计算；可查询基础形态。"
            )
            return
        try:
            body = growth_body(wire_id, detail["data"], owned, target)
        except ValueError:
            await reply("返回的角色或行迹等级不完整，暂时无法可靠计算。")
            return
        computed = await self.gateway.query(
            event.actor, "sr", uid, "compute", {"body": body}
        )
        if computed["retcode"] != 0:
            hint = (
                "（这次带了额外行迹；如果一直是这个错，去掉「全行迹」再试）"
                if target.extras
                else ""
            )
            await reply(computed["message"] + hint)
            return
        try:
            rows = materials(computed["data"])
        except ValueError:
            await reply("返回的材料数量格式有变化，暂时无法可靠展示。")
            return
        if not rows:
            await reply("计算接口未返回材料项目。")
            return
        # Owned/missing are a display view only; the totals above stay additive.
        # A row without a usable owned count keeps its original shape.
        stocks = {row["id"]: row for row in inventory(computed["data"], rows)}
        for row in rows:
            stock = stocks.get(row["id"])
            if stock:
                row["owned"] = stock["owned"]
                row["lack"] = stock["lack"]
        data = {
            "character": character,
            "uid": uid,
            "body": body,
            "materials": rows,
            "inventory": bool(stocks),
            "unowned": owned is None,
        }
        by_id = {str(item["item_id"]): item for item in body["skill_list"]}
        data["traces"] = [
            {
                "name": TRACE_NAMES[skill["anchor"]],
                "current": by_id[str(skill["point_id"])]["cur_level"],
                "target": by_id[str(skill["point_id"])]["target_level"],
            }
            for skill in sorted(detail["data"]["skills"], key=lambda row: row["anchor"])
        ]
        if target.extras:
            # Named plainly: the service does not give these nodes a display name.
            for index, extra in enumerate(detail["data"].get("skills_other") or [], 1):
                entry = by_id.get(str(positive_id(extra.get("point_id"))))
                if not entry:
                    continue
                data["traces"].append(
                    {
                        "name": TRACE_NAMES.get(
                            extra.get("anchor"), f"额外行迹{index}"
                        ),
                        "current": entry["cur_level"],
                        "target": entry["target_level"],
                    }
                )
        image = await self.engine.request(
            "card",
            {"template": "growth-sr", "event": event.worker_payload(), "data": data},
        )
        await reply(image)
