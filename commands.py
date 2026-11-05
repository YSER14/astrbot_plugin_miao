"""Own account and daily-game commands that have no native Miao handler.

The original MIT worker remains the sole owner of panels, damage and scoring.
These commands share its bindings and query gateway rather than implementing a
second account system. Replies contain no private credential material.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from datetime import datetime

import httpx

from . import activities
from . import codes as codes_module
from .accounts import Accounts
from .engine import EngineError
from .hoyolab import Hoyolab
from .messages import Incoming
from .settings import NATIVE_COMMANDS
from .wishes import Wishes

NAMES = {"gs": "原神", "sr": "星铁", "zzz": "绝区零"}
PRIVATE_INPUT = re.compile(
    r"(?:ltoken(?:_v2)?|cookie_token(?:_v2)?|stoken(?:_v2)?|authkey)\s*=", re.I
)
# The archive parser rejects larger documents too; this stops the download first.
ARCHIVE_LIMIT = 32 * 1024 * 1024
# Upstream rules: #上传<角色>照片 / #上传<角色>面板图
UPLOAD = re.compile(r"^上传.*(?:照片|面板图)$")
# Upstream rule for #原图; without a quoted card it returns false and stays silent.
ORIGINAL = re.compile(r"^(?:获取|给我|我要|求|发|发下|发个|发一下)?原图(?:吧|呗)?$")


def duration(value) -> str:
    """Format a service-provided number of seconds without inventing missing data."""
    try:
        seconds = max(0, int(value))
    except (TypeError, ValueError):
        return "未知"
    if seconds == 0:
        return "已回满"
    minutes = (seconds + 59) // 60
    return f"{minutes // 60}小时{minutes % 60}分" if minutes >= 60 else f"{minutes}分"


def notes_text(game: str, uid: str, data: dict) -> str:
    """Present known real-time fields; omit unsupported or absent fields."""
    lines = [f"{NAMES[game]}实时便笺 · {uid}"]

    def pair(label, current, maximum, source=data):
        if current in source and maximum in source:
            lines.append(f"{label}：{source[current]}/{source[maximum]}")

    if game == "gs":
        pair("树脂", "current_resin", "max_resin")
        if "resin_recovery_time" in data:
            lines.append(f"恢复：{duration(data['resin_recovery_time'])}")
        pair("每日委托", "finished_task_num", "total_task_num")
        pair("洞天宝钱", "current_home_coin", "max_home_coin")
        pair("派遣", "current_expedition_num", "max_expedition_num")
        if "is_extra_task_reward_received" in data:
            lines.append(
                "委托额外奖励："
                + ("已领取" if data["is_extra_task_reward_received"] else "未领取")
            )
        transformer = data.get("transformer", {})
        if transformer.get("obtained") and "recovery_time" in transformer:
            recovery = transformer["recovery_time"]
            lines.append(
                "参量质变仪：" + ("可使用" if recovery.get("reached") else "冷却中")
            )
    elif game == "sr":
        pair("开拓力", "current_stamina", "max_stamina")
        if "stamina_recover_time" in data:
            lines.append(f"恢复：{duration(data['stamina_recover_time'])}")
        if "current_reserve_stamina" in data:
            lines.append(f"后备开拓力：{data['current_reserve_stamina']}")
        pair("每日实训", "current_train_score", "max_train_score")
        pair("模拟宇宙积分", "current_rogue_score", "max_rogue_score")
        pair("差分宇宙积分", "rogue_tourn_weekly_cur", "rogue_tourn_weekly_max")
        pair("派遣", "accepted_epedition_num", "total_expedition_num")
    else:
        energy = data.get("energy", {})
        pair("电量", "current", "max", energy.get("progress", energy))
        if "restore" in energy:
            lines.append(f"恢复：{duration(energy['restore'])}")
        pair("每日活跃", "current", "max", data.get("vitality", {}))
        if "card_sign" in data:
            lines.append(
                "刮刮卡："
                + {
                    "CardSignDone": "已完成",
                    "CardSignNotDone": "未完成",
                    "CardSignNo": "未完成",
                }.get(data["card_sign"], "状态未知")
            )
        state = data.get("vhs_sale", {}).get(
            "sale_state", data.get("video_store_state")
        )
        if state:
            lines.append(
                "录像店："
                + {
                    "SaleStateDone": "可领取收益",
                    "SaleStateNo": "等待营业",
                    "SaleStateDoing": "营业中",
                }.get(state, "状态未知")
            )
    expeditions = data.get("expeditions", [])
    if expeditions:
        completed = sum(item.get("status") == "Finished" for item in expeditions)
        lines.append(f"已完成派遣：{completed}/{len(expeditions)}")
    if len(lines) == 1:
        lines.append("接口未返回可展示的便笺字段。")
    return "\n".join(lines)


# Currency names differ per game; the ledger endpoints share one response shape.
LEDGER_UNITS = {
    "gs": ("原石", "摩拉"),
    "sr": ("星琼", "信用点"),
    "zzz": ("菲林", "丁尼"),
}


def stamp(node) -> str:
    """Format a service date object, or return 未知 when it is not a full date."""
    if not isinstance(node, dict):
        return "未知"
    try:
        return (
            f"{int(node['year'])}-{int(node['month']):02d}-{int(node['day']):02d} "
            f"{int(node['hour']):02d}:{int(node['minute']):02d}"
        )
    except (KeyError, TypeError, ValueError):
        return "未知"


def ledger_text(game: str, uid: str, data: dict) -> str:
    """Present a monthly ledger. Month totals come from the API, never from sums."""
    name = NAMES[game]
    unit, mora_unit = LEDGER_UNITS[game]
    month_data = data.get("month_data") or {}
    day_data = data.get("day_data") or {}
    month = data.get("data_month", data.get("month", "?"))
    lines = [f"{name} {uid} · {month} 月账本"]
    if "current_primogems" not in month_data:
        lines.append("账本接口返回的结构和预期不同，本次不展示数字。")
        return "\n".join(lines)
    lines.append(
        f"{unit} {month_data.get('current_primogems')}，"
        f"{mora_unit} {month_data.get('current_mora')}"
    )
    if "last_primogems" in month_data:
        previous = data.get("data_last_month", "上月")
        lines.append(
            f"上月（{previous} 月）：{unit} {month_data['last_primogems']}，"
            f"{mora_unit} {month_data.get('last_mora')}"
        )
    rate = month_data.get("primogems_rate")
    if isinstance(rate, int):
        lines.append(f"{unit}比上月 {rate:+d}%")
    groups = [row for row in month_data.get("group_by") or [] if row.get("num")]
    if groups:
        lines.append("来源：")
        for row in sorted(groups, key=lambda item: -item.get("num", 0)):
            lines.append(
                f"  {row.get('action', '未知')} {row['num']}"
                f"（{row.get('percent', 0)}%）"
            )
    else:
        lines.append("这个月还没有收入记录。")
    if "last_primogems" in day_data:
        lines.append(
            f"今天 {day_data.get('current_primogems', 0)}，"
            f"昨天 {day_data['last_primogems']}"
        )
    return "\n".join(lines)


def role_combat_text(uid: str, data: dict) -> str:
    """Present Imaginarium Theater progress for the current and previous period."""
    lines = [f"原神 {uid} · 幻想真境剧诗"]
    if not data.get("is_unlock"):
        lines.append("这个赛季还没解锁。")
        return "\n".join(lines)
    for index, entry in enumerate((data.get("data") or [])[:2]):
        if not isinstance(entry, dict):
            continue
        schedule = entry.get("schedule") or {}
        stat = entry.get("stat") or {}
        label = "本期" if index == 0 else "上期"
        lines.append(
            f"{label}（第 {schedule.get('schedule_id', '?')} 期） "
            f"{stamp(schedule.get('start_date_time'))} ~ "
            f"{stamp(schedule.get('end_date_time'))}"
        )
        if not entry.get("has_data"):
            lines.append("  没有战斗记录。")
            continue
        lines.append(
            f"  难度 {stat.get('difficulty_id', 0)}，"
            f"最高到第 {stat.get('max_round_id', 0)} 幕，"
            f"勋章 {stat.get('medal_num', 0)}"
        )
        extra = [
            f"花牌 {stat['coin_num']}" if "coin_num" in stat else "",
            f"助战 {stat['rent_cnt']}" if "rent_cnt" in stat else "",
            f"额外角色 {stat['avatar_bonus_num']}"
            if "avatar_bonus_num" in stat
            else "",
        ]
        extra = [item for item in extra if item]
        if extra:
            lines.append("  " + "，".join(extra))
    tarot = data.get("tarot_card_state") or {}
    if tarot.get("total_num"):
        lines.append(f"塔罗牌解锁 {tarot.get('curr_num', 0)}/{tarot['total_num']}")
    return "\n".join(lines)


def hard_challenge_text(uid: str, data: dict) -> str:
    """Present 幽境危战 best times per mode; missing modes stay absent."""
    lines = [f"原神 {uid} · 幽境危战"]
    if not data.get("is_unlock"):
        lines.append("这个赛季还没解锁。")
        return "\n".join(lines)
    entries = data.get("data") or []
    if not entries:
        lines.append("接口没有返回赛季数据。")
        return "\n".join(lines)
    for index, entry in enumerate(entries[:2]):
        if not isinstance(entry, dict):
            continue
        schedule = entry.get("schedule") or {}
        label = "本期" if index == 0 else "上期"
        state = "" if schedule.get("is_valid") else "（已结束）"
        lines.append(
            f"{label}{state}「{schedule.get('name', '未知赛季')}」 "
            f"{stamp(schedule.get('start_date_time'))} ~ "
            f"{stamp(schedule.get('end_date_time'))}"
        )
        recorded = False
        for key, mode in (("single", "单人"), ("mp", "多人")):
            block = entry.get(key) or {}
            if not isinstance(block, dict):
                continue
            if not block.get("has_data"):
                continue
            recorded = True
            best = block.get("best") or {}
            if best:
                seconds = best.get("second")
                cleared = (
                    f"{seconds // 60}分{seconds % 60}秒"
                    if isinstance(seconds, int)
                    else "未知"
                )
                lines.append(
                    f"  {mode}最佳：难度 {best.get('difficulty', '?')}，用时 {cleared}"
                )
            bosses = [
                boss.get("name")
                for boss in block.get("challenge") or []
                if isinstance(boss, dict) and boss.get("name")
            ]
            if bosses:
                lines.append(f"  {mode}关卡：" + "、".join(bosses))
        if not recorded:
            lines.append("  没有挑战记录。")
    return "\n".join(lines)


# The service returns element keys in English; unknown values stay out of the text.
ELEMENTS = {
    "Pyro": "火",
    "Hydro": "水",
    "Anemo": "风",
    "Electro": "雷",
    "Dendro": "草",
    "Cryo": "冰",
    "Geo": "岩",
}


def tarot_text(uid: str, data: dict) -> str:
    """List the 月谕圣牌 a role_combat payload carries, unlocked first."""
    tarot = data.get("tarot_card_state") or {}
    cards = [row for row in tarot.get("list") or [] if isinstance(row, dict)]
    total = tarot.get("total_num")
    if not cards or not isinstance(total, int):
        return "接口没有返回圣牌收藏数据。"
    opened = [row for row in cards if row.get("is_unlock")]
    closed = [row for row in cards if not row.get("is_unlock")]
    lines = [f"原神 {uid} · 月谕圣牌 {len(opened)}/{total}"]
    if opened:
        lines.append("已解锁：")
        for row in opened:
            name = str(row.get("name") or "?").strip()
            count = row.get("unlock_num")
            lines.append(
                f"  {name}（{count}）" if isinstance(count, int) else f"  {name}"
            )
    if closed:
        lines.append(
            "未解锁："
            + "、".join(str(row.get("name") or "?").strip() for row in closed)
        )
    return "\n".join(lines)


def deck_text(uid: str, data: dict) -> str:
    """Present the TCG deck list. Unnamed decks keep the service's own id."""
    decks = [row for row in data.get("deck_list") or [] if isinstance(row, dict)]
    lines = [f"原神 {uid} · 七圣召唤卡组（{len(decks)} 组）"]
    if not decks:
        lines.append("接口没有返回卡组。")
        return "\n".join(lines)
    for index, deck in enumerate(decks, 1):
        name = (
            str(deck.get("name") or "").strip() or f"未命名卡组 {deck.get('id', index)}"
        )
        state = "可用" if deck.get("is_valid") else "已失效"
        cards = [
            str(card.get("name")).strip()
            for card in deck.get("avatar_cards") or []
            if isinstance(card, dict) and card.get("name")
        ]
        lines.append(f"{index}. {name}（{state}）")
        if cards:
            lines.append("   角色牌：" + "、".join(cards))
        actions = [
            card for card in deck.get("action_cards") or [] if isinstance(card, dict)
        ]
        if actions:
            lines.append(f"   行动牌：{len(actions)} 种")
        code = str(deck.get("share_code") or "").strip()
        if code:
            lines.append(f"   分享码：{code}")
    return "\n".join(lines)


def plain_text(value) -> str:
    """Strip the card descriptions' HTML so a chat message stays readable."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"<img[^>]*>", "", value)
    text = re.sub(r"</?color[^>]*>", "", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\s+", " ", text).strip()


def cost_text(costs) -> str:
    """Join cost numbers without guessing what an unknown cost type means."""
    values = [
        str(cost["cost_value"])
        for cost in costs or []
        if isinstance(cost, dict) and isinstance(cost.get("cost_value"), int)
    ]
    return " + ".join(values)


def deck_detail_text(uid: str, data: dict, index: int) -> str:
    """Show one deck's cards. An out-of-range index reports the real count."""
    decks = [row for row in data.get("deck_list") or [] if isinstance(row, dict)]
    if not 1 <= index <= len(decks):
        return f"没有第 {index} 组卡组；这个账号目前有 {len(decks)} 组。"
    deck = decks[index - 1]
    name = str(deck.get("name") or "").strip() or f"未命名卡组 {deck.get('id', index)}"
    state = "可用" if deck.get("is_valid") else "已失效"
    lines = [f"原神 {uid} · 七圣卡组 {index}「{name}」（{state}）"]
    for card in deck.get("avatar_cards") or []:
        if not isinstance(card, dict) or not card.get("name"):
            continue
        head = f"● {card['name']}"
        if isinstance(card.get("hp"), int):
            head += f" HP{card['hp']}"
        if isinstance(card.get("proficiency"), int):
            head += f" 熟练度{card['proficiency']}"
        lines.append(head)
        for skill in card.get("card_skills") or []:
            if not isinstance(skill, dict):
                continue
            tag = str(skill.get("tag") or "").strip()
            label = (
                f"{tag}｜{skill.get('name')}" if tag else str(skill.get("name") or "")
            )
            if label:
                lines.append(f"   {label}")
    actions = [
        card for card in deck.get("action_cards") or [] if isinstance(card, dict)
    ]
    if actions:
        lines.append(f"行动牌 {len(actions)} 种：")
        for card in actions:
            row = str(card.get("name") or "?").strip()
            cost = cost_text(card.get("action_cost"))
            lines.append(f"   消耗 {cost}｜{row}" if cost else f"   {row}")
    code = str(deck.get("share_code") or "").strip()
    if code:
        lines.append(f"分享码：{code}")
    lines.append("看单张牌用「#七圣卡牌 牌名」。")
    return "\n".join(lines)


def tcg_card_text(uid: str, data: dict, wanted: str) -> str:
    """Look one card up by name across the returned decks, without fuzzy guessing."""
    target = wanted.strip()
    cards, seen = [], set()
    for deck in data.get("deck_list") or []:
        if not isinstance(deck, dict):
            continue
        for key in ("avatar_cards", "action_cards"):
            for card in deck.get(key) or []:
                if not isinstance(card, dict):
                    continue
                ident = (key, card.get("id"), card.get("name"))
                if ident in seen:
                    continue
                seen.add(ident)
                cards.append(card)
    matches = [card for card in cards if str(card.get("name") or "").strip() == target]
    # Users quote skill names as often as card names ("黎明" is 迪卢克's burst),
    # so a skill hit resolves to the card that owns it instead of a dead end.
    via_skill = None
    if not matches:
        for card in cards:
            hit = next(
                (
                    skill
                    for skill in card.get("card_skills") or []
                    if isinstance(skill, dict)
                    and str(skill.get("name") or "").strip() == target
                ),
                None,
            )
            if hit:
                matches = [card]
                via_skill = str(hit.get("name") or "").strip()
                break
    if not matches:
        names = sorted(
            {
                str(item.get("name")).strip()
                for card in cards
                for item in [card, *(card.get("card_skills") or [])]
                if isinstance(item, dict) and item.get("name")
            }
        )
        sample = "、".join(names[:8])
        return f"卡组里没有叫「{target}」的牌或技能。" + (
            f"现有：{sample}…" if sample else ""
        )
    card = matches[0]
    lines = [f"原神 {uid} · 七圣卡牌「{card['name']}」"]
    if via_skill:
        lines.append(f"（按技能名「{via_skill}」找到这张角色牌）")
    cost = cost_text(card.get("action_cost"))
    if cost:
        lines.append(f"消耗：{cost}")
    if isinstance(card.get("hp"), int) and card["hp"]:
        lines.append(f"生命：{card['hp']}")
    if isinstance(card.get("proficiency"), int) and card.get("card_skills"):
        lines.append(f"熟练度：{card['proficiency']}")
    for skill in card.get("card_skills") or []:
        if not isinstance(skill, dict) or not skill.get("name"):
            continue
        tag = f"{skill['tag']}｜" if skill.get("tag") else ""
        lines.append(f"{tag}{skill['name']}")
        body = plain_text(skill.get("desc"))
        if body:
            lines.append(f"  {body}")
    body = plain_text(card.get("desc"))
    if body:
        lines.append(body)
    sources = [
        str(item).strip()
        for item in card.get("card_sources") or []
        if isinstance(item, str) and item.strip()
    ]
    if sources:
        lines.append("来源：" + "、".join(sources))
    return "\n".join(lines)


def hard_challenge_roles_text(uid: str, data: dict) -> str:
    """List the characters the service returns for the current 幽境危战 season."""
    rows = [row for row in data.get("avatar_list") or [] if isinstance(row, dict)]
    lines = [f"原神 {uid} · 幽境危战本期角色（{len(rows)}）"]
    if not rows:
        lines.append("接口没有返回角色。")
        return "\n".join(lines)
    names = []
    for row in rows:
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        element = ELEMENTS.get(row.get("element"))
        names.append(f"{name}（{element}）" if element else name)
    if not names:
        lines.append("接口没有返回可展示的角色名。")
        return "\n".join(lines)
    lines.append("、".join(names))
    return "\n".join(lines)


class Commands:
    """Handle explicit account mutations using only the actual event sender."""

    def __init__(
        self,
        accounts: Accounts,
        gateway: Hoyolab,
        wishes: Wishes | None = None,
        signins=None,
        growth=None,
        ledgers=None,
        switches=None,
    ):
        self.accounts = accounts
        self.gateway = gateway
        self.wishes = wishes
        self.signins = signins
        self.growth = growth
        self.ledgers = ledgers
        self.switches = switches

    async def handle(
        self, event: Incoming, reply: Callable[[str], Awaitable[object]]
    ) -> bool:
        """Return True only when this message was handled by the native service."""
        if PRIVATE_INPUT.search(event.original_text):
            await reply(
                "请通过受控登录入口配置游戏凭据，不在聊天中粘贴。抽卡链接请使用专门的记录导入入口。"
            )
            return True
        text = (
            re.sub(r"^#(?:原神|星铁|绝区零)?", "", event.text)
            if event.text.startswith("#")
            else ""
        )
        if not text:
            return False
        # The upstream upload handlers expect a quoted image and throw without one.
        # Answering here keeps a missing attachment from surfacing as a worker error.
        if (
            UPLOAD.match(text)
            and not event.reply_id
            and not any(
                segment.get("type") in {"image", "file"} for segment in event.segments
            )
        ):
            await reply(
                "把图片和这条指令一起发，或者引用一张图再发；这条指令需要一张图。"
            )
            return True
        # #原图 reads the original behind a quoted card. Without a quote upstream
        # returns false, which lets the message fall through to the chat model.
        if ORIGINAL.match(text) and not event.reply_id:
            await reply("引用一张我发过的图再发 #原图，我才能找回它对应的原图。")
            return True
        game, actor = event.game, event.actor
        name = NAMES[game]
        if self.growth and self.growth.matches(event):
            try:
                await self.growth.handle(event, reply)
            except (EngineError, TimeoutError):
                await reply("星铁养成暂时无法完成，请稍后重试。")
            return True
        if text in {
            "兑换码",
            "直播兑换码",
            "兑换码查询",
            "兑换码口令",
            "星铁兑换码",
            "星穹铁道兑换码",
        }:
            # Official miyoushe endpoints; no account is involved in reading them.
            # The game prefix (星铁/…) was already stripped from `text`, so the
            # detection must look at the original text.
            game = "sr" if "星" in (event.text or "") else "gs"
            try:
                payload = await codes_module.fetch_codes(game)
            except codes_module.LiveError as error:
                await reply(str(error))
                return True
            except httpx.HTTPError:
                await reply("米游社兑换码接口暂时连不上，请稍后再试。")
                return True
            lines = [f"{payload['title']}（{len(payload['codes'])} 个）"]
            for entry in payload["codes"]:
                lines.append(
                    f"{entry['code']}　{entry['items']}"
                    if entry["items"]
                    else entry["code"]
                )
            if payload["article"]:
                lines.append(f"兑换入口与说明：{payload['article']}")
            lines.append("兑换码有时效，尽快在游戏内兑换。")
            await reply("\n".join(lines))
            return True
        if text in {"活动", "进行中活动", "活动列表"}:
            # In-game announcement lists are public; no account is involved.
            try:
                rows = await activities.fetch(datetime.now())
            except httpx.HTTPError:
                await reply("米游社活动接口暂时连不上，请稍后再试。")
                return True
            if not rows:
                await reply("三个游戏都没有查询到进行中的活动。")
                return True
            lines = ["进行中的活动（按结束时间排序）"]
            current = None
            for row in rows:
                if row.game != current:
                    current = row.game
                    lines.append(f"{activities.GAMES[current]['name']}：")
                left = (
                    f"剩{row.days}天{row.hours}小时"
                    if row.days
                    else f"剩{row.hours}小时"
                )
                lines.append(f"  {row.title}（{left}，{row.ends} 截止）")
            await reply("\n".join(lines))
            return True
        if text in {"原生功能", "原生指令", "喵喵功能"}:
            # The upstream help card is generated from its own rules, so the native
            # commands have no other place to be discovered.
            lines = ["原生功能（帮助卡片里没有这些）"]
            for group, items in NATIVE_COMMANDS:
                lines.append(f"{group}：{'、'.join(items)}")
            lines.append("完整的上游入口看 #喵喵帮助。")
            await reply("\n".join(lines))
            return True
        if text in {"喵喵开关", "开关状态"} and self.switches:
            rows = self.switches() or []
            if not rows:
                await reply("没有可显示的开关。")
                return True
            await reply(
                "\n".join(
                    ["喵喵开关（改：发「#喵喵设置 开关名 开/关」）"]
                    + [
                        f"{row['title']}：{'开' if row['value'] else '关'}"
                        for row in rows
                    ]
                )
            )
            return True
        if text in {"账本历史", "本地账本"} and self.ledgers:
            if game != "gs":
                await reply("本地只记录原神账本；星铁和绝区零暂时不采集。")
                return True
            selected = self.accounts.selected(actor, game)
            if not selected:
                await reply(f"请先绑定{name} UID。")
                return True
            months = self.ledgers.months(actor, "gs", selected["uid"])
            if not months:
                await reply(
                    "本地还没有账本记录。在面板打开「账本自动采集」后，每天会存一次当月数字。"
                )
                return True
            lines = [f"{name} {selected['uid']} · 本地账本（{len(months)} 个月）"]
            for month in months:
                row = self.ledgers.get(actor, "gs", selected["uid"], month)
                if not row:
                    continue
                lines.append(
                    f"{month}：原石 {row['primogems']}，摩拉 {row['mora']}"
                    f"（采集于 {row['collected']}）"
                )
            await reply("\n".join(lines))
            return True
        if text in {"签到记录", "自动签到记录"} and self.signins:
            from datetime import date

            rows = self.signins.history(event.actor, date.today())
            labels = {"ok": "成功", "already": "今天已签过", "failed": "失败"}
            await reply(
                "\n".join(
                    [f"今日米游社签到（{date.today().isoformat()}）"]
                    + [
                        f"{NAMES.get(row['game'], row['game'])} {row['uid']}："
                        f"{labels.get(row['status'], row['status'])}"
                        + (f"（{row['detail']}）" if row["detail"] else "")
                        for row in rows
                    ]
                )
                if rows
                else "今天还没有签到记录。"
            )
            return True
        if text in {"抽卡记录状态", "记录状态"} and self.wishes:
            selected = self.accounts.selected(actor, game)
            if not selected:
                await reply(f"请先绑定{name} UID。")
            else:
                rows = self.wishes.summary(actor, game, selected["uid"])
                await reply(
                    "\n".join(
                        [f"{name}本地抽卡记录 · {selected['uid']}"]
                        + [f"卡池 {row['pool']}：{row['count']} 条" for row in rows]
                    )
                    if rows
                    else "本地还没有该账号的抽卡记录。"
                )
                if not rows:
                    await reply(
                        "导入办法：把 UIGF 的 json 文件和「#导入抽卡记录」一起发过来。"
                    )
            return True
        if text in {"导入抽卡记录", "导入记录"} and self.wishes:
            files = [
                segment
                for segment in event.segments
                if segment.get("type") == "file" and segment.get("url")
            ]
            if not files:
                await reply(
                    "把 UIGF 的 json 文件跟这条指令一起发过来；只会读你自己这条消息里的附件。"
                )
                return True
            try:
                # Bounded streaming: the parser also refuses huge documents, but the
                # download must not pull an arbitrary file into memory first.
                async with httpx.AsyncClient(
                    timeout=60, follow_redirects=True
                ) as client:
                    async with client.stream("GET", files[0]["url"]) as response:
                        response.raise_for_status()
                        chunks, total = [], 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > ARCHIVE_LIMIT:
                                raise ValueError("Archive exceeds limit")
                            chunks.append(chunk)
                document = json.loads(b"".join(chunks).decode("utf-8-sig"))
            except (ValueError, UnicodeDecodeError):
                await reply("这个文件不是可识别的 UIGF 存档，或者超过了大小限制。")
                return True
            except httpx.HTTPError:
                await reply("文件下载失败或链接已过期，请重新发送一次文件。")
                return True
            try:
                result = self.wishes.import_archive(event.actor, document)
            except ValueError as error:
                await reply(f"导入没有生效：{error}。原有记录未被改动。")
                return True
            await reply(
                f"已导入 {result['accounts']} 个账号：新增 {result['inserted']} 条，"
                f"重复 {result['duplicates']} 条。"
            )
            return True
        bound = re.fullmatch(r"绑定(?:uid)?[\s+]*(\d{8,10})", text, re.I)
        if bound:
            try:
                self.accounts.bind(actor, game, bound[1], default=True)
                await reply(f"已绑定{name} UID {bound[1]}，设为当前账号。")
            except ValueError:
                await reply(f"无法识别这个{name} UID，请检查位数及区服。")
            return True
        if re.fullmatch(r"绑定(?:uid)?", text, re.I):
            await reply(
                f"请发送 #{name if game != 'gs' else ''}绑定 你的UID；UID绑定不包含米游社登录。"
            )
            return True
        show = re.fullmatch(r"(?:我的)?uid(\d{0,2})", text, re.I)
        switch = re.fullmatch(r"(?:切换|默认)(?:uid)?[\s+]*(\d{1,2})", text, re.I)
        remove = re.fullmatch(r"(?:删除|解绑)uid[\s+]*(\d{0,2})", text, re.I)
        if show or switch or remove:
            entries = self.accounts.list(actor, game)
            match = switch or remove or show
            index = int(match[1]) - 1 if match[1] else None
            if index is not None and not 0 <= index < len(entries):
                await reply(
                    "账号序号不存在，请先查看 #"
                    + (name if game != "gs" else "")
                    + "uid。"
                )
                return True
            if remove:
                selected = (
                    entries[index]
                    if index is not None
                    else self.accounts.selected(actor, game)
                )
                if selected:
                    self.accounts.unbind(actor, game, selected["uid"])
                    await reply(
                        f"已解绑{name} UID {selected['uid']}，并删除该绑定的登录数据。"
                    )
                else:
                    await reply(f"还没有绑定{name} UID。")
            else:
                if index is not None:
                    self.accounts.select(actor, game, entries[index]["uid"])
                    entries = self.accounts.list(actor, game)
                await reply(
                    "\n".join(
                        [f"{name}账号"]
                        + [
                            f"{i}. {entry['uid']}{'（当前）' if entry['default'] else ''}"
                            for i, entry in enumerate(entries, 1)
                        ]
                    )
                    if entries
                    else f"还没有绑定{name} UID。"
                )
            return True
        if re.fullmatch(r"删除ck|删除c(?:oo)?k(?:ie)?", text, re.I):
            self.accounts.revoke(actor, game)
            await reply(f"已删除你在本机器人上的{name}登录数据，保留UID绑定。")
            return True
        if re.fullmatch(r"(?:检查|我的)?c(?:oo)?k(?:ie)?(?:状态)?", text, re.I):
            entries = self.accounts.list(actor, game)
            await reply(
                "\n".join(
                    [f"{name}登录状态（本地存储）"]
                    + [
                        f"{entry['uid']}：{'已保存，健康状态需查询验证' if entry['has_credentials'] else '未登录'}"
                        for entry in entries
                    ]
                )
                if entries
                else f"还没有绑定{name} UID。"
            )
            return True
        ledger_month = None
        ledger_match = re.fullmatch(
            r"(?:原石|星琼|菲林)?账本(?:\s*(\d{1,2}|20\d{4}))?", text
        )
        if ledger_match:
            text = "账本"
            ledger_month = ledger_match[1]
        deck_index = None
        deck_match = re.fullmatch(r"七圣(?:召唤)?卡组(?:\s*(\d{1,3}))?", text)
        if deck_match:
            text = "七圣卡组"
            deck_index = int(deck_match[1]) if deck_match[1] else None
        card_name = None
        card_match = re.fullmatch(r"七圣(?:召唤)?卡牌\s*(.+)", text)
        if card_match:
            text = "七圣卡牌"
            card_name = card_match[1].strip()
        operations = {
            "体力": "dailyNote",
            "树脂": "dailyNote",
            "开拓力": "dailyNote",
            "电量": "dailyNote",
            "便笺": "dailyNote",
            "实时便笺": "dailyNote",
            "签到": "sign",
            "签到状态": "sign_info",
            "账本": "ys_ledger",
            "剧诗": "role_combat",
            "剧诗数据": "role_combat",
            "剧诗圣牌": "role_combat",
            "圣牌收藏": "role_combat",
            "剧诗圣牌收藏": "role_combat",
            "幻想剧诗": "role_combat",
            "幻想真境剧诗": "role_combat",
            "幽境": "hard_challenge",
            "幽境危战": "hard_challenge",
            "幽境危战数据": "hard_challenge",
            "七圣卡组": "deckList",
            "七圣召唤卡组": "deckList",
            "七圣卡牌": "deckList",
            "幽境角色": "hard_challenge_popularity",
            "幽境可选角色": "hard_challenge_popularity",
        }
        operation = operations.get(text)
        if not operation:
            return False
        if any(
            target not in {event.self_id, event.user_id} for target in event.mentions
        ):
            await reply("便笺、签到和战绩只查询你本人绑定并登录的账号。")
            return True
        # Only Genshin's ledger was verified against a real account; the endpoint
        # name is shared, but the other two games return a different shape.
        if operation == "ys_ledger" and game != "gs":
            await reply(f"{name}账本还没有用真实账号验证过，目前只开放原神。")
            return True
        selected = self.accounts.selected(actor, game)
        if not selected:
            await reply(f"请先绑定{name} UID。")
            return True
        result = await self.gateway.query(
            actor,
            game,
            selected["uid"],
            operation,
            {"month": ledger_month} if ledger_month else None,
        )
        if result["retcode"] != 0:
            await reply(result["message"])
        elif operation == "dailyNote":
            await reply(notes_text(game, selected["uid"], result["data"]))
        elif operation == "ys_ledger":
            await reply(ledger_text(game, selected["uid"], result["data"]))
        elif operation == "role_combat":
            if text in {"剧诗圣牌", "圣牌收藏", "剧诗圣牌收藏"}:
                await reply(tarot_text(selected["uid"], result["data"]))
            else:
                await reply(role_combat_text(selected["uid"], result["data"]))
        elif operation == "hard_challenge":
            await reply(hard_challenge_text(selected["uid"], result["data"]))
        elif operation == "deckList":
            if card_name:
                await reply(tcg_card_text(selected["uid"], result["data"], card_name))
            elif deck_index:
                await reply(
                    deck_detail_text(selected["uid"], result["data"], deck_index)
                )
            else:
                await reply(deck_text(selected["uid"], result["data"]))
        elif operation == "hard_challenge_popularity":
            await reply(hard_challenge_roles_text(selected["uid"], result["data"]))
        elif operation == "sign":
            reward = result["data"]
            details = (
                f"：{reward.get('name', '')} × {reward['amount']}"
                if reward.get("name") and "amount" in reward
                else ""
            )
            await reply(f"{name}米游社签到成功{details}。")
        else:
            info = result["data"]
            await reply(
                f"{name}米游社签到：{'今日已签' if info.get('signed_in') else '今日未签'}；本月累计 {info.get('claimed_rewards', '未知')} 天。"
            )
        return True
