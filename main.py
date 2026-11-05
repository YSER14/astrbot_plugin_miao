"""AstrBot owns Miao events and permissions; the worker owns game computation.

The development plugin is disabled by default. Prepare its owned runtime before
enabling a specific test scope. No login, QQ send, or background task runs merely
because this module is imported.
"""

from __future__ import annotations

import sys
import time
from itertools import groupby
from pathlib import Path

from astrbot.api import AstrBotConfig, logger, sp
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.star.session_plugin_manager import SessionPluginManager

from .accounts import Accounts, actor_key, game_server
from .addons import Addons, worker_settings
from .commands import Commands
from .databackup import DataBackup
from .delivery import send_reply
from .engine import Engine, EngineError
from .growth import Growth
from .hoyolab import Hoyolab
from .ledger import LedgerCollect
from .login import Logins
from .messages import Incoming, game_chain, miao_segments
from .notify import Targets
from .rankings import Rankings
from .settings import (
    CHAT_ALIASES,
    CHAT_TOGGLES,
    OFF_WORDS,
    SWITCH_TITLES,
    config_schema,
)
from .tasks import SignIn
from .wishes import Wishes

NAME = "astrbot_plugin_miao"


@register(NAME, "sombe", "喵喵与米哈游游戏功能迁移（开发版，默认关闭）", "0.1.0-dev1")
class MiaoPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.root = None
        self.accounts = None
        self.wishes = None
        self.engine = None
        self.gateway = None
        self.commands = None
        self.addons = None
        self.logins = None
        self.signins = None
        self.targets = None
        self.ledgers = None
        self.events: dict[str, tuple[AstrMessageEvent, Incoming]] = {}
        self.forward_refs: dict[str, set[str]] = {}

    async def initialize(self):
        """Open only this plugin's data; start no worker when disabled."""
        if not self.config.get("enabled", False):
            return
        data = StarTools.get_data_dir(NAME)
        # Optional pinned SDK installed with `--no-deps --target <data>/vendor`.
        # Keeping genshin off the image's site-packages means a plugin install or a
        # container rebuild cannot quietly change AstrBot's own shared dependencies.
        vendor = data / "vendor"
        if vendor.is_dir() and str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        self.root = Path(self.config.get("runtime_root") or data / "runtime").resolve()
        self.rankings = Rankings(self.root)
        self.accounts = Accounts(data / "accounts.sqlite3")
        self.gateway = Hoyolab(self.accounts)
        self.wishes = Wishes(data / "wishes.sqlite3")
        # Timers have no event, so the owner's own private session is remembered.
        self.targets = Targets(data / "notify.json")
        self.signins = SignIn(
            self.accounts,
            self.gateway,
            data / "signins.sqlite3",
            hour=int(self.config.get("sign_hour") or 0),
            minute=int(self.config.get("sign_minute") or 0),
            notify=self.notify_owner,
            notify_daily=bool(self.config.get("notify_daily", False)),
        )
        self.commands = Commands(self.accounts, self.gateway, self.wishes, self.signins)
        raw_minute = self.config.get("ledger_minute")
        self.ledgers = LedgerCollect(
            self.accounts,
            self.gateway,
            data / "ledger.sqlite3",
            hour=int(self.config.get("ledger_hour") or 0),
            minute=int(17 if raw_minute is None else raw_minute),
        )
        self.commands.ledgers = self.ledgers.store
        self.commands.switches = self.switch_rows
        self.commands.ledgers = self.ledgers.store
        self.logins = Logins(self.accounts, self.gateway, self.root / "output")
        self.engine = Engine(
            self.root,
            node=self.config.get("node_executable") or "node",
            callback=self.callback,
        )
        self.addons = Addons(self.root, self.config, self.engine)
        self.commands.growth = Growth(self.accounts, self.gateway, self.engine)
        # Timers have no event, so arm the delivery target from the sole logged-in
        # owner when no real message has been captured yet.
        logger.info(
            "Miao push target: %s",
            self.targets.ensure(self.accounts.credential_owners()) or "none yet",
        )
        # Signing mutates the owner's account, so it only ever starts when asked.
        if self.config.get("auto_sign", False):
            self.signins.start()
        # Collection only reads, but it still runs on a timer, so it is opt-in.
        if self.config.get("auto_ledger", False):
            self.ledgers.start()
        # The 2026-11-01 incident deleted the only real login with no data backup
        # to fall back on, so this one is unconditional: it protects user data and
        # switching it off has no benefit.
        self.backups = DataBackup(
            data,
            hour=int(self.config.get("backup_hour") or 4),
            minute=int(
                17
                if self.config.get("backup_minute") is None
                else self.config.get("backup_minute")
            ),
        )
        self.backups.start()

    def switch_rows(self) -> list[dict]:
        """Describe the chat-changeable switches without exposing other settings."""
        return [
            {
                "key": key,
                "title": title,
                "value": bool(self.config.get(key, False)),
            }
            for key, title in SWITCH_TITLES.items()
        ]

    async def notify_owner(self, text: str) -> bool:
        """Deliver one message to the stored owner session; False when unroutable."""
        target = self.targets.load() if self.targets else None
        if not target or not text:
            return False
        try:
            return bool(
                await self.context.send_message(target, MessageChain().message(text))
            )
        except Exception:
            logger.warning("Miao owner notification could not be delivered")
            return False

    async def allowed(self, event, incoming: Incoming) -> bool:
        """Apply session, scope, whitelist and silence gates before game handling."""
        if not self.config.get("enabled") or incoming.user_id == incoming.self_id:
            return False
        if incoming.private:
            if not self.config.get("all_private_users", False):
                gate = self.context.get_registered_star("astrbot_plugin_private_gate")
                if gate and gate.activated and not incoming.is_master:
                    return False
                if not incoming.is_master and incoming.user_id not in self.config.get(
                    "private_users", []
                ):
                    return False
            # Recognized game commands stop propagation in receive(). Unhandled
            # private chat still reaches the existing PrivateGate afterwards.
        elif not self.config.get(
            "all_groups"
        ) and incoming.group_id not in self.config.get("groups", []):
            return False
        origin = event.unified_msg_origin
        cfg = self.context.get_config(origin)
        settings = cfg.get("platform_settings", {})
        white = settings.get("id_whitelist", [])
        bypass = incoming.is_master and settings.get(
            "wl_ignore_admin_on_friend"
            if incoming.private
            else "wl_ignore_admin_on_group"
        )
        if (
            settings.get("enable_id_white_list")
            and white
            and not bypass
            and incoming.group_id not in white
            and origin not in white
        ):
            return False
        service = await sp.get_async(
            scope="umo", scope_id=origin, key="session_service_config", default={}
        )
        if service.get(
            "session_enabled"
        ) is False or not await SessionPluginManager.is_plugin_enabled_for_session(
            origin, NAME
        ):
            return False
        wake = self.context.get_registered_star("astrbot_plugin_wakepro")
        if wake and wake.activated and wake.star_cls:
            if {origin, incoming.user_id, incoming.group_id} & set(
                wake.star_cls.cfg.global_blacklist
            ):
                return False
            state = (
                wake.module.StateManager._groups.get(incoming.group_id)
                if wake.module
                else None
            )
            member = state.members.get(incoming.user_id) if state else None
            if state and (
                state.shutup_until > time.time()
                or (member and member.silence_until > time.time())
            ):
                return False
        return True

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100002)
    async def receive(self, event: AstrMessageEvent):
        """Claim only known game messages before automatic wake or merge handlers."""
        if event.get_platform_name() != "aiocqhttp" or self.engine is None:
            return
        incoming = Incoming.from_event(event)
        try:
            if not await self.allowed(event, incoming):
                return
            if incoming.event_type not in {"message", "poke"}:
                return
            if incoming.event_type == "poke" and incoming.target_id != incoming.self_id:
                return
            self.events[incoming.token] = (event, incoming)
            # Only the owner's own private chat becomes the background target.
            if incoming.is_master and incoming.private and self.targets:
                self.targets.save(
                    Targets.private(event.get_platform_id(), incoming.user_id)
                )

            async def notify_login(message):
                if not await self.allowed(event, incoming):
                    raise EngineError("Login destination is no longer permitted")
                return await self.send(
                    event, incoming, message, quote=not incoming.private
                )

            async def retract_login_qr(message_id):
                result = await event.bot.call_action(
                    "delete_msg", message_id=message_id, self_id=incoming.self_id
                )
                # OneBot's successful delete action may return null data.
                if result is not None and (
                    not isinstance(result, dict)
                    or result.get("status") == "failed"
                    or result.get("retcode", 0) != 0
                ):
                    raise EngineError("QR message withdrawal failed")

            if incoming.text == "#推送测试" and incoming.is_master:
                target = self.targets.load() if self.targets else None
                sent = await self.notify_owner("这是推送测试消息。")
                if sent:
                    message = "推送测试已发到你的私聊。"
                elif target:
                    message = f"目标已记下（{target}），但平台没有接受这次发送。"
                else:
                    message = (
                        "还没有投递目标：先私聊我发一条消息，我才会记住往哪儿推。"
                    )
                await self.send(event, incoming, message)
                event.stop_event()
                return
            if await self.logins.handle(
                incoming, notify_login, retract=retract_login_qr
            ):
                event.stop_event()
                return
            if getattr(self, "addons", None) and await self.addons.handle(
                incoming, lambda message: self.send(event, incoming, message)
            ):
                event.stop_event()
                return
            if await self.commands.handle(
                incoming, lambda message: self.send(event, incoming, message)
            ):
                event.stop_event()
                return
            if incoming.text == "#喵喵开发状态":
                if incoming.is_master:
                    await self.send(
                        event,
                        incoming,
                        "喵喵开发中：原内核已加载；账号与网络功能正在逐项接通。当前不是全功能版本。",
                    )
                    event.stop_event()
                return
            # Native account/login commands above work without the Node runtime.
            # Without this guard every other message would try to start a worker
            # that was never prepared and log a warning for each one.
            if not (self.engine.root / "bridge" / "main.mjs").is_file():
                return
            result = await self.engine.request(
                "dispatch",
                {
                    "event": incoming.worker_payload(),
                    "bare_help": bool(self.config.get("bare_help", False)),
                    "addons": worker_settings(self.config),
                },
            )
            if result.get("handled"):
                if result.get("rank_summary"):
                    logger.info("Miao group ranking: %s", result["rank_summary"])
                event.stop_event()
        except (EngineError, TimeoutError):
            # Never echo an upstream exception or claim a failed operation succeeded.
            logger.warning("Miao request failed or timed out")
            if (
                incoming.group_id
                and incoming.text.startswith("#")
                and any(word in incoming.text for word in ("排名", "排行", "最强"))
            ):
                event.stop_event()
                await self.send(event, incoming, "群排名暂不可用，请稍后重试。")
                return
            if incoming.text.startswith("#喵喵") or Growth.matches(incoming):
                event.stop_event()
                await self.send(
                    event, incoming, "喵喵运行环境暂不可用，请检查开发状态。"
                )
        finally:
            self.events.pop(incoming.token, None)
            self.forward_refs.pop(incoming.token, None)

    async def send(self, event, incoming: Incoming, payload, *, quote: bool = False):
        """Preserve game output and capture the real OneBot send receipt.

        Returns:
            The protocol response, including a real message_id when available.
        """
        chain = game_chain(payload, self.root)
        if not chain.chain:
            return {}
        if quote and incoming.message_id:
            from astrbot.api.message_components import Reply

            chain.chain.insert(0, Reply(id=incoming.message_id))
        response = {}

        async def transport(checked):
            nonlocal response
            from astrbot.api.message_components import Node, Nodes
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )

            target = (
                {"group_id": int(incoming.group_id)}
                if incoming.group_id
                else {"user_id": int(incoming.user_id)}
            )
            for forward, batch in groupby(
                checked.chain,
                key=lambda component: isinstance(component, (Node, Nodes)),
            ):
                components = list(batch)
                if forward:
                    segments = []
                    for component in components:
                        nodes = (
                            component.nodes
                            if isinstance(component, Nodes)
                            else [component]
                        )
                        segments.extend([await node.to_dict() for node in nodes])
                else:
                    segments = await AiocqhttpMessageEvent._parse_onebot_json(
                        checked.derive(components)
                    )
                if not segments:
                    continue
                method = (
                    (
                        "send_group_forward_msg"
                        if incoming.group_id
                        else "send_private_forward_msg"
                    )
                    if forward
                    else ("send_group_msg" if incoming.group_id else "send_private_msg")
                )
                receipt = await event.bot.call_action(
                    method,
                    **target,
                    self_id=incoming.self_id,
                    **({"messages": segments} if forward else {"message": segments}),
                )
                if (
                    not isinstance(receipt, dict)
                    or receipt.get("status") == "failed"
                    or receipt.get("retcode", 0) != 0
                ):
                    raise EngineError("Game output was not delivered")
                response = receipt.get("data", receipt)
                if not isinstance(response, dict) or not response.get("message_id"):
                    raise EngineError("Game delivery receipt is missing")
                event.set_extra(
                    "xiaoye_delivery_seq", event.get_extra("xiaoye_delivery_seq", 0) + 1
                )

        # Final project guards inspect real text/media; game pages skip CHAT compression.
        sent = await send_reply(event, chain, transport)
        if sent is False:
            raise EngineError("Game result was suppressed")
        return response or {}

    async def callback(self, method: str, params: dict):
        """Resolve a worker call against its original trusted event only."""
        pair = self.events.get(params.get("event_key"))
        if pair is None:
            raise EngineError("Original event is no longer active")
        event, incoming = pair
        if not await self.allowed(event, incoming):
            raise EngineError("Original event is no longer permitted")
        if method == "send":
            return await self.send(
                event, incoming, params.get("message"), quote=bool(params.get("quote"))
            )
        if method == "account_info":
            game = params.get("game", incoming.game)
            target_user = str(params.get("target_user") or incoming.user_id)
            if target_user != incoming.user_id and target_user not in incoming.mentions:
                raise EngineError(
                    "Query target was not addressed in the original message"
                )
            actor = actor_key(incoming.platform, incoming.self_id, target_user)
            uid = params.get("uid")
            bound = self.accounts.selected(actor, game, str(uid) if uid else None)
            uid = str(uid or (bound or {}).get("uid") or "")
            if not uid:
                return None
            game_server(game, uid)
            own = target_user == incoming.user_id
            return {
                "uid": uid,
                "own": own,
                # The credential that matters belongs to whoever owns this actor:
                # your own CK when you ask for yourself, theirs when you @ them.
                "has_credentials": bool(self.accounts.credentials(actor, game, uid)),
            }
        if method == "game_api":
            target_user = str(params.get("target_user") or incoming.user_id)
            if target_user != incoming.user_id:
                if target_user not in incoming.mentions:
                    raise EngineError(
                        "Query target was not addressed in the original message"
                    )
                # Panel fetches are the one documented exception: a mentioned
                # member's own stored CK may fill that member's panel. Gacha,
                # records, notes and everything else still belong to the owner.
                if params.get("type") not in {
                    "character",
                    "characterDetail",
                    "avatarInfo",
                }:
                    return {"retcode": -100, "message": "此查询需要账号本人操作。"}
                actor = actor_key(incoming.platform, incoming.self_id, target_user)
            else:
                actor = incoming.actor
            return await self.gateway.query(
                actor,
                params.get("game", incoming.game),
                str(params.get("uid") or ""),
                str(params.get("type") or ""),
                params.get("data"),
            )
        if method == "group_rank_profiles":
            if not incoming.group_id:
                raise EngineError("Group ranking requires an original group event")
            game = params.get("game", incoming.game)
            if game not in {"gs", "sr"}:
                raise EngineError("Unsupported ranking game")
            key = "miao_rank_bindings:" + game
            bindings = event.get_extra(key)
            if bindings is None:
                raw = await event.bot.call_action(
                    "get_group_member_list",
                    group_id=int(incoming.group_id),
                    self_id=incoming.self_id,
                )
                members = raw.get("data", raw) if isinstance(raw, dict) else raw
                if not isinstance(members, list):
                    raise EngineError("Group membership is unavailable")
                users = {
                    str(member["user_id"])
                    for member in members
                    if isinstance(member, dict)
                    and str(member.get("user_id", "")).isdigit()
                }
                if incoming.user_id not in users:
                    raise EngineError("Sender is no longer a group member")
                bindings = self.accounts.for_members(
                    incoming.platform, incoming.self_id, users, game
                )
                event.set_extra(key, bindings)
            return self.rankings.page(
                bindings,
                platform=incoming.platform,
                bot=incoming.self_id,
                game=game,
                offset=params.get("offset", 0),
            )
        if method == "gacha_sync":
            # Only the sender's own bound accounts; an explicit foreign UID gets nothing.
            game = params.get("game", incoming.game)
            uid = str(params.get("uid") or "")
            bound = {entry["uid"] for entry in self.accounts.list(incoming.actor, game)}
            if not self.wishes or uid not in bound:
                return {"pools": {}}
            return {"pools": self.wishes.pools(incoming.actor, game, uid)}
        if method == "config_read":
            if not incoming.is_master:
                raise EngineError("Owner permission is required")
            schema = config_schema()
            return {
                "schema": schema,
                "values": {key: self.config.get(key) for key in schema},
            }
        if method == "config_write":
            if not incoming.is_master:
                raise EngineError("Owner permission is required")
            parts = str(params.get("text") or "").split()
            key = CHAT_ALIASES.get(parts[0] if parts else "")
            if not key or key not in CHAT_TOGGLES:
                return {
                    "ok": False,
                    "reason": "聊天里只能改开关：启用 / 全部群 / 全部私聊 / 帮助别名 / 自动签到 / 签到推送 / 账本采集。其余请用面板。",
                }
            value = not (len(parts) > 1 and parts[1].lower() in OFF_WORDS)
            await self.config.save_config_async({key: value})
            # A saved flag alone changes nothing; the timer has to follow it.
            if key == "auto_sign" and self.signins:
                if value:
                    self.signins.start()
                else:
                    await self.signins.close()
            if key == "auto_ledger" and self.ledgers:
                if value:
                    self.ledgers.start()
                else:
                    await self.ledgers.close()
            logger.info("Miao switch changed by owner: %s=%s", key, value)
            return {"ok": True, "key": key, "value": value}
        if method == "admin":
            if not incoming.is_master:
                raise EngineError("Owner permission is required")
            return await self.send(
                event, incoming, "喵喵管理功能迁移中，当前未执行配置修改或更新。"
            )
        if method == "get_reply":
            # No quote means the handler still runs; it reports its own "no image" hint.
            if not incoming.reply_id:
                return {"message": []}
            raw = await event.bot.call_action(
                "get_msg", message_id=incoming.reply_id, self_id=incoming.self_id
            )
            raw = raw.get("data", raw)
            if incoming.group_id:
                if str(raw.get("group_id") or "") != incoming.group_id:
                    raise EngineError("Quoted message belongs to another conversation")
            elif str(
                raw.get("user_id") or raw.get("sender", {}).get("user_id") or ""
            ) not in {incoming.user_id, incoming.self_id}:
                raise EngineError("Quoted message belongs to another sender")
            segments = miao_segments(raw.get("message"))
            self.forward_refs.setdefault(incoming.token, set()).update(
                segment["id"] for segment in segments if segment["type"] == "forward"
            )
            return {
                "message_id": str(raw.get("message_id", incoming.reply_id)),
                "message": segments,
            }
        if method == "get_forward":
            ident = str(params.get("id") or "")
            if ident not in self.forward_refs.get(incoming.token, set()):
                raise EngineError("Forward was not present in the quoted message")
            raw = await event.bot.call_action(
                "get_forward_msg", id=ident, self_id=incoming.self_id
            )
            raw = raw.get("data", raw)
            return [
                {"message": miao_segments(node.get("content", node.get("message", [])))}
                for node in raw.get("messages", [])[:100]
            ]
        if method == "avatar_url":
            user_id = str(params.get("user_id") or "")
            if not user_id.isdigit() or not 5 <= len(user_id) <= 12:
                raise EngineError("Invalid avatar account")
            return f"https://q1.qlogo.cn/g?b=qq&nk={user_id}&s=640"
        if method == "group_members" and incoming.group_id:
            return await event.bot.call_action(
                "get_group_member_list",
                group_id=int(incoming.group_id),
                self_id=incoming.self_id,
            )
        raise EngineError("Worker callback is not connected")

    async def terminate(self):
        """Cancel child work on unload without touching existing QQ services."""
        if self.signins:
            await self.signins.close()
            self.signins = None
        if self.ledgers:
            await self.ledgers.close()
            self.ledgers = None
        if self.backups:
            await self.backups.close()
            self.backups = None
        if self.logins:
            await self.logins.close()
            self.logins = None
        if self.engine:
            await self.engine.close()
            self.engine = None
        self.events.clear()
        self.forward_refs.clear()
