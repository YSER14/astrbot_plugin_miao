"""Run the one subscription this plugin owns: the daily Miyoushe check-in.

Example:
    signer = SignIn(accounts, gateway, data / "signins.sqlite3", hour=0, minute=7)
    signer.start()          # no-op unless the config turned it on
    await signer.close()

Nothing here runs because the plugin was imported. There is exactly one timer, it
is cancelled on unload, and every account is signed at most once per local day.
A failed attempt is recorded as failed; it is never reported as success.

Reminders and stamina pushes are deliberately absent: they need `dailyNote`, and
that endpoint answers a geetest challenge this process cannot solve. The one push
this file owns is the check-in outcome, and only when something needs attention
(a failure) or the owner explicitly asked for a daily line.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .accounts import Accounts
from .hoyolab import Hoyolab

LOG = logging.getLogger(__name__)
LOCAL = ZoneInfo("Asia/Shanghai")
RETRY_GAPS = (5, 30)
GAME_NAMES = {"gs": "原神", "sr": "星铁", "zzz": "绝区零"}


class SignIn:
    """Own one daily timer and a per-owner, per-day idempotency ledger."""

    def __init__(
        self,
        accounts: Accounts,
        gateway: Hoyolab,
        path: Path,
        *,
        hour: int = 0,
        minute: int = 7,
        notify: Callable[[str], Awaitable[object]] | None = None,
        notify_daily: bool = False,
    ):
        self.accounts = accounts
        self.gateway = gateway
        self.path = Path(path)
        self.hour = hour % 24
        self.minute = minute % 60
        self.notify = notify
        # A silent success is the default: only failures interrupt the owner.
        self.notify_daily = notify_daily
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS signins (
                    actor TEXT NOT NULL, game TEXT NOT NULL, uid TEXT NOT NULL,
                    day TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
                    updated REAL NOT NULL,
                    PRIMARY KEY(actor,game,uid,day)
                );
            """)
        self.task: asyncio.Task | None = None
        self.stopping = False

    # --- 台账 ---
    def record(self, owner: dict, day: date, status: str, detail: str = ""):
        """Store the outcome of one attempt; the last write wins for that day."""
        with sqlite3.connect(self.path) as db:
            db.execute(
                "INSERT OR REPLACE INTO signins VALUES(?,?,?,?,?,?,?)",
                (
                    owner["actor"],
                    owner["game"],
                    owner["uid"],
                    day.isoformat(),
                    status,
                    detail[:200],
                    datetime.now(LOCAL).timestamp(),
                ),
            )

    def today(self, owner: dict, day: date) -> str | None:
        """Return today's recorded status for one owner, if any."""
        with sqlite3.connect(self.path) as db:
            row = db.execute(
                "SELECT status FROM signins WHERE actor=? AND game=? AND uid=? AND day=?",
                (owner["actor"], owner["game"], owner["uid"], day.isoformat()),
            ).fetchone()
        return row[0] if row else None

    def history(self, actor: str, day: date) -> list[dict]:
        """Show one owner's results for a day without exposing credentials."""
        with sqlite3.connect(self.path) as db:
            rows = db.execute(
                "SELECT game,uid,status,detail FROM signins WHERE actor=? AND day=? ORDER BY game,uid",
                (actor, day.isoformat()),
            ).fetchall()
        return [dict(zip(("game", "uid", "status", "detail"), row)) for row in rows]

    # --- 调度 ---
    def start(self):
        """Start the single timer; repeated calls never create a second one.

        ``close()`` keeps the ledger, so starting again after the owner flips the
        switch back on resumes from the same per-day idempotency records.
        """
        if self.task is None or self.task.done():
            self.stopping = False
            self.task = asyncio.get_running_loop().create_task(self._loop())

    async def _loop(self):
        while not self.stopping:
            try:
                await asyncio.sleep(self._seconds_until_next())
                if self.stopping:
                    return
                await self.run_once(date.today())
            except asyncio.CancelledError:
                raise
            except Exception:
                # A crash inside one round must not kill tomorrow's round.
                LOG.warning("Miao daily sign-in round failed; it will retry tomorrow")

    def _seconds_until_next(self, now: datetime | None = None) -> float:
        """Seconds until the next local run time, always positive."""
        now = now or datetime.now(LOCAL)
        target = now.replace(
            hour=self.hour, minute=self.minute, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    async def run_once(self, day: date) -> dict:
        """Sign every logged-in binding once for ``day`` and return the tally.

        Args:
            day: Local date used as the idempotency key.

        Returns:
            ``{ok, already, failed}`` counts for this round.
        """
        tally = {"ok": 0, "already": 0, "failed": 0}
        attention = []
        for owner in self.accounts.credential_owners():
            if self.stopping:
                break
            done = self.today(owner, day)
            if done in {"ok", "already"}:
                tally["already"] += 1
                continue
            status, detail = await self._sign(owner)
            self.record(owner, day, status, detail)
            if status == "failed":
                label = GAME_NAMES.get(owner["game"], owner["game"])
                attention.append(f"{label} {owner['uid']}：{detail or '失败'}")
            tally[
                "ok"
                if status == "ok"
                else "already"
                if status == "already"
                else "failed"
            ] += 1
        if any(tally.values()):
            LOG.info(
                "Miao daily sign-in: ok=%s already=%s failed=%s",
                tally["ok"],
                tally["already"],
                tally["failed"],
            )
        await self._notify(day, tally, attention)
        return tally

    async def _notify(self, day: date, tally: dict, attention: list[str]):
        """Push only when something needs attention, or when asked to report daily."""
        if self.notify is None:
            return
        if not (tally["failed"] or (self.notify_daily and tally["ok"])):
            return
        lines = [
            f"米游社签到 · {day.isoformat()}",
            f"成功 {tally['ok']}，今天已签 {tally['already']}，失败 {tally['failed']}",
            *attention,
        ]
        try:
            await self.notify("\n".join(lines))
        except Exception:
            # A delivery failure must not corrupt the ledger or kill the timer.
            LOG.warning("Miao sign-in report could not be delivered")

    async def _sign(self, owner: dict) -> tuple[str, str]:
        """Try one binding, retrying only the failures worth retrying."""
        for gap in (0, *RETRY_GAPS):
            if gap:
                await asyncio.sleep(gap)
            if self.stopping:
                return "failed", "stopped"
            result = await self.gateway.query(
                owner["actor"], owner["game"], owner["uid"], "sign"
            )
            code = result.get("retcode")
            if code == 0:
                return "ok", ""
            if code == -5003:  # 今天已经签到
                return "already", "今天已经签到"
            if code in {-100, -101, 10001}:  # 登录失效：重试也没用
                return "failed", str(result.get("message") or "")
        return "failed", str(result.get("message") or "")

    async def close(self):
        """Cancel and join the timer so no request outlives the plugin."""
        self.stopping = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
