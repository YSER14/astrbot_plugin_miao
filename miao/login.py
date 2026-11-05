"""Own bounded Miyoushe QR login sessions and withdraw the delivered QR message.

The SDK's public QR helper opens a desktop window and polls forever. This
adapter uses its two pinned CN methods, sends a QR to the original conversation, then discovers
and binds the authenticated account's supported roles without a preselected UID.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from .accounts import Accounts
from .hoyolab import Hoyolab
from .messages import Incoming

LOG = logging.getLogger(__name__)


def qr_client():
    """Use CN only; genshin 1.7.31 has no corresponding overseas QR API."""
    import genshin

    return genshin.Client(region=genshin.Region.CHINESE, lang="zh-cn")


def qr_image(url: str, path: Path):
    """Encode the service-provided login URL without opening a desktop window."""
    import qrcode

    if urlparse(url).scheme != "https" or len(url) > 8192:
        raise ValueError("Invalid login URL")
    qrcode.make(url).save(path)


@dataclass
class Session:
    """Keep one login generation owned by an immutable sender key."""

    game: str
    preferred_uid: str | None
    image: Path
    deadline: float
    lifetime: float = 180
    group: bool = False
    retract: Callable[[str | int], Awaitable[object]] | None = None
    qr_message_id: str | int | None = None
    task: asyncio.Task | None = None


class Logins:
    """Cancel on replacement, expiry and plugin unload; never print Cookie data."""

    def __init__(
        self,
        accounts: Accounts,
        gateway: Hoyolab,
        output: Path,
        *,
        factory=qr_client,
        image_writer=qr_image,
        device_preparer=None,
        lifetime: float = 180,
        interval: float = 2,
        timeout: float = 12,
    ):
        self.accounts = accounts
        self.gateway = gateway
        self.output = Path(output).resolve()
        self.factory = factory
        self.image_writer = image_writer
        self.device_preparer = device_preparer or gateway.prepare_login_devices
        self.lifetime = lifetime
        self.interval = interval
        self.timeout = timeout
        self.sessions: dict[str, Session] = {}
        self.lock = asyncio.Lock()
        self.closed = False

    async def handle(self, incoming: Incoming, notify, *, retract=None) -> bool:
        """Claim login commands in the original private or group conversation.

        No previous UID binding is required. An optional UID is only a preference
        among the roles returned by the authenticated account lookup.
        """
        body = re.sub(r"^#(?:原神|星铁|绝区零)?", "", incoming.text).strip()
        command = re.fullmatch(
            r"(扫码登录|米游社登录|绑定ck|绑定CK|绑定Cookie|取消登录)(?:\s+(\d{8,10}))?",
            body,
        )
        if not command:
            return False
        name, explicit = command[1], command[2]
        if name == "取消登录":
            await self.cancel(incoming.actor)
            await notify("已取消你当前的游戏登录。")
            return True
        if not incoming.private and retract is None:
            await notify("群扫码撤回通道暂不可用，请稍后重试。")
            return True
        await self.start(
            incoming.actor,
            incoming.game,
            explicit,
            notify,
            group=not incoming.private,
            retract=retract,
        )
        return True

    async def cancel(self, actor: str):
        """Cancel and join this sender's previous session before replacing it."""
        previous = self.sessions.pop(actor, None)
        if previous and previous.task:
            previous.task.cancel()
            await asyncio.gather(previous.task, return_exceptions=True)

    async def start(
        self,
        actor: str,
        game: str,
        preferred_uid: str | None,
        notify,
        *,
        group: bool = False,
        retract=None,
    ):
        """Create one finite session; group QR exposure is bounded to 90 seconds."""
        async with self.lock:
            if self.closed:
                return
            await self.cancel(actor)
            if len(self.sessions) >= 32:
                await notify("当前登录会话较多，请稍后重试。")
                return
            self.output.mkdir(parents=True, exist_ok=True)
            lifetime = min(self.lifetime, 90) if group else self.lifetime
            session = Session(
                game,
                preferred_uid,
                self.output / f"login-{uuid4().hex}.png",
                time.monotonic() + lifetime,
                lifetime=lifetime,
                group=group,
                retract=retract,
            )
            self.sessions[actor] = session
            session.task = asyncio.create_task(self._poll(actor, session, notify))
            session.task.add_done_callback(
                lambda task: None if task.cancelled() else task.exception()
            )

    async def _retract_qr(self, session: Session):
        """Withdraw only this session's delivered bot QR, retrying at later states.

        Args:
            session: Own QR receipt and original-conversation transport callback.
        """
        if session.qr_message_id is None or session.retract is None:
            return
        try:
            await asyncio.wait_for(session.retract(session.qr_message_id), self.timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("Miao QR withdrawal failed (%s)", type(exc).__name__)
        else:
            session.qr_message_id = None
            LOG.info("Miao QR message withdrawn (group=%s)", session.group)

    async def _poll(self, actor: str, session: Session, notify):
        phase, saved = "create", False
        try:
            async with asyncio.timeout(
                max(0, session.deadline - time.monotonic())
            ) as qr_timeout:
                client = self.factory()
                created = await asyncio.wait_for(client._create_qrcode(), self.timeout)
                self.image_writer(created.url, session.image)
                receipt = await notify(
                    [
                        f"请由指令发起人用米游社扫描并确认登录，无需预先绑定UID；检测到已扫码后会自动撤回二维码，并绑定该账号的国服游戏角色。二维码有效期约{int(session.lifetime)}秒。",
                        {"type": "image", "file": session.image.as_uri()},
                    ]
                )
                if isinstance(receipt, dict):
                    message_id = receipt.get("message_id")
                    if (
                        isinstance(message_id, (str, int))
                        and not isinstance(message_id, bool)
                        and message_id != ""
                    ):
                        session.qr_message_id = message_id
                if session.group and (
                    session.qr_message_id is None or session.retract is None
                ):
                    raise RuntimeError("Group QR withdrawal receipt is unavailable")
                scanned = False
                phase = "poll"
                while time.monotonic() < session.deadline:
                    status, cookies = await asyncio.wait_for(
                        client._check_qrcode(created.ticket), self.timeout
                    )
                    state = getattr(status, "value", status)
                    if state == "Scanned" and not scanned:
                        await self._retract_qr(session)
                        await notify("已扫码，请在米游社中确认登录。")
                        scanned = True
                    if state == "Confirmed":
                        if self.closed or self.sessions.get(actor) is not session:
                            return
                        # Confirmation ends QR expiry. Discovery and registration
                        # each keep their own bounded network timeout afterwards.
                        qr_timeout.reschedule(None)
                        await self._retract_qr(session)
                        values = {
                            name: getattr(value, "value", value)
                            for name, value in cookies.items()
                        }
                        phase = "discover"
                        result = await self.gateway.bind_login(
                            actor, session.game, values, session.preferred_uid
                        )
                        if result is None:
                            await notify(
                                "扫码已确认，但该米游社账号没有可绑定的原神、星铁或绝区零国服角色；原有账号保留。"
                            )
                            return
                        saved, phase = True, "device"
                        try:
                            devices = await self.device_preparer(
                                actor, result["accounts"]
                            )
                        except Exception as exc:
                            LOG.warning(
                                "Miao post-login device preparation failed (%s)",
                                type(exc).__name__,
                            )
                            devices = [{"state": "failed"}]
                        phase = "notify"
                        names = {"gs": "原神", "sr": "星铁", "zzz": "绝区零"}
                        lines = [
                            f"登录已确认，自动绑定并保存 {len(result['accounts'])} 个游戏角色："
                        ]
                        for role in result["accounts"]:
                            selected = result["defaults"][role["game"]] == role["uid"]
                            lines.append(
                                f"{names[role['game']]} UID {role['uid']}"
                                + ("（当前）" if selected else "")
                            )
                        lines.append(
                            "标注「当前」的角色可直接使用；可用 #uid 查看，#切换 序号 切换。星铁/绝区零指令加对应游戏前缀。"
                        )
                        if any(
                            item["state"] == "verification_required" for item in devices
                        ):
                            lines.append(
                                "原神设备已登记，但米游社还要求本人完成安全验证（1034）；登录和绑定已保存。"
                            )
                        elif any(
                            item["state"] in {"failed", "login_changed"}
                            for item in devices
                        ):
                            lines.append(
                                "登录和绑定已保存，原神设备初始化尚未完成；面板暂可能受限。"
                            )
                        elif devices:
                            lines.append(
                                "原神设备上下文已保存，可发送 #更新面板。"
                            )
                        await notify("\n".join(lines))
                        return
                    await asyncio.sleep(self.interval)
                await notify("游戏登录二维码已过期，请重新发送扫码登录。")
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            await notify("游戏登录已超时，请重新发送扫码登录。")
        except Exception as exc:
            LOG.warning(
                "Miao QR login failed during %s (%s, saved=%s)",
                phase,
                type(exc).__name__,
                saved,
            )
            try:
                if saved:
                    await notify(
                        "登录与自动绑定已保存，但结果通知未完成；请发送 #uid 查看。"
                    )
                elif phase == "discover":
                    await notify(
                        "扫码已确认，但读取米游社游戏角色失败，未保存新的凭据；请稍后重新扫码。"
                    )
                else:
                    await notify(
                        "游戏登录未完成，未保存新的凭据；请重新扫码或稍后重试。"
                    )
            except Exception:
                pass
        finally:
            await self._retract_qr(session)
            if session.retract and session.qr_message_id is not None:
                try:
                    await notify(
                        "未能确认二维码撤回结果，请管理员检查群内二维码；登录状态以绑定结果为准。"
                    )
                except Exception:
                    pass
            if self.sessions.get(actor) is session:
                self.sessions.pop(actor, None)
            session.image.unlink(missing_ok=True)

    async def close(self):
        """Stop all polling and remove owned temporary QR images on unload."""
        async with self.lock:
            self.closed = True
            await asyncio.gather(*(self.cancel(actor) for actor in list(self.sessions)))
