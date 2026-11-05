"""Adapt the pinned HoYoLab SDK without passing credentials to the JS worker.

Examples:
    gateway = Hoyolab(accounts)
    result = await gateway.query(actor, "gs", uid, "dailyNote", {})

The supported operation table is deliberately closed: worker parameters cannot
choose URLs, replace the account owner, or select another account's Cookie.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping

from .accounts import Accounts, game_server

RECORDS = {
    "gs": {
        "index": ("index", "GET"),
        "character": ("character/list", "POST"),
        "characterDetail": ("character/detail", "POST"),
        "spiralAbyss": ("spiralAbyss", "GET"),
        "role_combat": ("role_combat", "GET"),
        "hard_challenge": ("hard_challenge", "GET"),
        "hard_challenge_popularity": ("hard_challenge/popularity", "GET"),
        "dailyNote": ("dailyNote", "GET"),
        "basicInfo": ("gcg/basicInfo", "GET"),
        "deckList": ("gcg/deckList", "GET"),
    },
    "sr": {
        "index": ("index", "GET"),
        "basicInfo": ("role/basicInfo", "GET"),
        "character": ("avatar/basic", "GET"),
        "avatarInfo": ("avatar/info", "GET"),
        "dailyNote": ("note", "GET"),
        "spiralAbyss": ("challenge", "GET"),
        "challenge_story": ("challenge_story", "GET"),
        "challenge_boss": ("challenge_boss", "GET"),
        "challenge_peak": ("challenge_peak", "GET"),
        "rogue": ("rogue", "GET"),
        "rogue_locust": ("rogue_locust", "GET"),
        "rogue_nous": ("rogue_nous", "GET"),
    },
    "zzz": {
        "index": ("index", "GET"),
        "dailyNote": ("note", "GET"),
        "character": ("avatar/basic", "GET"),
        "avatarInfo": ("avatar/info", "GET"),
        "buddy": ("buddy/info", "GET"),
    },
}

ERRORS = {
    -100: "米游社登录已失效，请重新登录。",
    -101: "米游社登录已失效，请重新登录。",
    10001: "米游社登录已失效，请重新登录。",
    # Official UI separates device/account risk (5003) from a solvable challenge (1034).
    5003: "米游社拒绝了本次设备或安全校验（5003）。登录凭据仍保留，但本次没有取得请求的数据。",
    10102: "该账号未开放对应战绩或实时便笺。",
    10103: "未找到该游戏角色。",
    10104: "该账号未开放对应战绩或实时便笺。",
    1034: "米游社要求安全验证，请由账号本人完成验证后重试。",
    -5003: "今天已经签到。",
}

# Legacy explicit device headers. Empty values are omitted. A matched device
# pair must come from a real client registration; a public sample is not proof
# of device validity. The official web SDK also registers brand-new browsers,
# so prior gameplay on that device is not a requirement established by evidence.
DEVICE_HEADERS = {
    "x-rpc-device_fp": "",
    "x-rpc-device_id": "",
}


def device_headers() -> dict:
    """Return only the device header pairs that carry a real value."""
    return {key: value for key, value in DEVICE_HEADERS.items() if value}


class IncompleteRoster(ValueError):
    """Reject a partial detail response before the worker updates any profiles."""


def sdk_client(
    cookies: dict,
    game: str,
    uid: str | None,
    server: str,
    *,
    headers: dict | None = None,
):
    """Create the actual pinned SDK client, loading its optional dependency lazily."""
    import genshin

    games = {
        "gs": genshin.Game.GENSHIN,
        "sr": genshin.Game.STARRAIL,
        "zzz": genshin.Game.ZZZ,
    }
    chinese = server in {"cn_gf01", "cn_qd01", "prod_gf_cn", "prod_qd_cn"}
    return genshin.Client(
        cookies,
        game=games[game],
        uid=int(uid) if uid is not None else None,
        region=genshin.Region.CHINESE if chinese else genshin.Region.OVERSEAS,
        lang="zh-cn",
        headers=headers,
    )


def failure(code: int, message: str) -> dict:
    """Return the original Miao response shape without private exception text."""
    return {"retcode": code, "message": message}


def positive_id(value) -> int:
    """Validate numeric character IDs before constructing an upstream payload."""
    if (
        isinstance(value, bool)
        or not str(value).isdigit()
        or not 0 < int(value) < 1_000_000_000
    ):
        raise ValueError("Invalid character ID")
    return int(value)


def record_payload(game: str, uid: str, operation: str, data: dict) -> dict:
    """Project supplied arguments onto the supported record API contract."""
    payload = {"role_id": int(uid), "server": game_server(game, uid)}
    if operation in {
        "spiralAbyss",
        "challenge_story",
        "challenge_boss",
        "challenge_peak",
    }:
        schedule = int(
            data.get("schedule_type", 3 if operation == "challenge_peak" else 1)
        )
        if schedule not in ({1, 3} if operation == "challenge_peak" else {1, 2}):
            raise ValueError("Invalid schedule")
        payload["schedule_type"] = schedule
    if operation in {"role_combat", "hard_challenge"}:
        payload["need_detail"] = "true" if data.get("need_detail", True) else "false"
    if operation == "characterDetail":
        ids = data.get("character_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 200:
            raise ValueError("Invalid character IDs")
        payload["character_ids"] = [positive_id(value) for value in ids]
    if operation == "avatarInfo":
        if game == "sr":
            payload["need_wiki"] = "true"
        elif game == "zzz":
            ids = data.get("id_list")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100:
                raise ValueError("Invalid agent IDs")
            payload["id_list[]"] = [positive_id(value) for value in ids]
    return payload


class Hoyolab:
    """Authorize each request using the immutable sender and selected game UID."""

    def __init__(
        self, accounts: Accounts, *, factory: Callable = sdk_client, timeout: float = 25
    ):
        self.accounts = accounts
        self.factory = factory
        self.timeout = timeout

    async def verify_credentials(
        self, actor: str, game: str, uid: str, cookies: dict
    ) -> dict:
        """Store a Cookie only after the service reports this exact owned account.

        Raises:
            ValueError: If login failed or the returned account does not match.
        """
        server = game_server(game, uid)
        if (
            not cookies
            or len(json.dumps(cookies)) > 32_768
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in cookies.items()
            )
        ):
            raise ValueError("Invalid login data")
        try:
            async with asyncio.timeout(self.timeout):
                client = self.factory(cookies, game, uid, server)
                found = await client.get_game_accounts()
        except Exception:
            raise ValueError("Login could not be verified") from None
        game_names = {"gs": "genshin", "sr": "hkrpg", "zzz": "nap"}
        matched = next(
            (
                item
                for item in found
                if str(item.uid) == uid
                and item.server == server
                and getattr(item.game, "value", item.game) == game_names[game]
            ),
            None,
        )
        if matched is None:
            raise ValueError("Login does not own the selected game account")
        self.accounts.put_credentials(actor, game, uid, cookies)
        return {"game": game, "uid": uid, "server": server, "verified": True}

    async def bind_login(
        self, actor: str, game: str, cookies: dict, preferred_uid: str | None = None
    ) -> dict | None:
        """Discover and bind supported CN roles from a confirmed QR login.

        Args:
            actor: Trusted sender who owns the QR session.
            game: Command game, used only for an optional UID selection preference.
            cookies: Credentials returned by the confirmed QR login.
            preferred_uid: Optional default preference, never proof of ownership.

        Returns:
            Public binding metadata and selected UIDs, or None when no supported
            CN roles exist. Existing bindings are retained in either case.

        Raises:
            ValueError: If login data or the authenticated role lookup fails.
        """
        if (
            game not in RECORDS
            or not cookies
            or len(json.dumps(cookies)) > 32_768
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in cookies.items()
            )
        ):
            raise ValueError("Invalid login data")
        try:
            async with asyncio.timeout(self.timeout):
                client = self.factory(
                    cookies, game, None, "cn_gf01" if game == "gs" else "prod_gf_cn"
                )
                found = await client.get_game_accounts()
        except Exception:
            raise ValueError("Login accounts could not be discovered") from None
        games = {"genshin": "gs", "hkrpg": "sr", "nap": "zzz"}
        discovered = {}
        for item in found:
            kind = games.get(getattr(item.game, "value", item.game))
            if kind is None or item.server not in {
                "cn_gf01",
                "cn_qd01",
                "prod_gf_cn",
                "prod_qd_cn",
            }:
                continue
            uid = str(item.uid)
            if game_server(kind, uid) != item.server:
                raise ValueError("Login account region does not match its UID")
            discovered[(kind, uid)] = {
                "game": kind,
                "uid": uid,
                "server": item.server,
                "level": int(getattr(item, "level", 0)),
            }
        if not discovered:
            return None
        roles = list(discovered.values())
        defaults = {}
        for kind in sorted({role["game"] for role in roles}):
            candidates = [role for role in roles if role["game"] == kind]
            # Match the SDK's default policy: select the highest-level role.
            selected = max(candidates, key=lambda role: role["level"])
            if kind == game and (kind, preferred_uid) in discovered:
                selected = discovered[(kind, preferred_uid)]
            defaults[kind] = selected["uid"]
        self.accounts.put_login(actor, cookies, roles, defaults)
        return {"accounts": roles, "defaults": defaults}

    async def prepare_login_devices(
        self, actor: str, roles: list[dict], *, registrar=None
    ) -> list[dict]:
        """Register one matched client context for the new login's CN Genshin roles.

        Args:
            actor: Owner of the already persisted login.
            roles: Authenticated roles returned by bind_login.
            registrar: Optional registration transport for isolated contract tests.

        Returns:
            Public per-role results. A 1034 result means registered but still
            awaiting human verification. Registration failure preserves login.
        """
        from .record_device import register_device

        results, pending = [], []
        for role in roles:
            if role["game"] != "gs":
                continue
            uid = role["uid"]
            if self.accounts.record_context(actor, "gs", uid):
                results.append({"uid": uid, "state": "retained"})
            else:
                pending.append(uid)
        if not pending:
            return results
        credentials = {
            uid: self.accounts.credentials(actor, "gs", uid) for uid in pending
        }
        try:
            async with asyncio.timeout(self.timeout):
                headers = self.accounts.check_record_context(
                    await (registrar or register_device)()
                )
                for uid in pending:
                    cookies = credentials[uid]
                    if not cookies or game_server("gs", uid) not in {
                        "cn_gf01",
                        "cn_qd01",
                    }:
                        raise ValueError(
                            "Device registration requires an owned CN login"
                        )
                    code, count = 0, None
                    try:
                        client = self.factory(
                            cookies, "gs", uid, game_server("gs", uid), headers=headers
                        )
                        roster = await self._query(client, "gs", uid, "character", {})
                        ids = [positive_id(row["id"]) for row in roster["list"]]
                        if not ids or len(ids) != len(set(ids)):
                            raise IncompleteRoster()
                        count = len(ids)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        response = getattr(exc, "response", {})
                        code = (
                            response.get("retcode", -1)
                            if isinstance(response, Mapping)
                            else -1
                        )
                    if self.accounts.credentials(actor, "gs", uid) != cookies:
                        results.append({"uid": uid, "state": "login_changed"})
                        continue
                    if code in {0, 1034}:
                        if not self.accounts.record_context(actor, "gs", uid):
                            self.accounts.put_record_context(actor, "gs", uid, headers)
                        results.append(
                            {
                                "uid": uid,
                                "state": "registered"
                                if code == 0
                                else "verification_required",
                                "retcode": code,
                                "characters": count,
                            }
                        )
                    else:
                        results.append({"uid": uid, "state": "failed", "retcode": code})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            done = {result["uid"] for result in results}
            results.extend(
                {
                    "uid": uid,
                    "state": "failed",
                    "retcode": -3 if isinstance(exc, TimeoutError) else -1,
                }
                for uid in pending
                if uid not in done
            )
        return results

    async def query(
        self, actor: str, game: str, uid: str, operation: str, data: dict | None = None
    ) -> dict:
        """Execute one allowlisted operation and return the raw Miao envelope.

        No SDK exception, raw HTTP body, or Cookie is returned through RPC.
        """
        try:
            server = game_server(game, uid)
            if (
                not isinstance(data or {}, dict)
                or len(json.dumps(data or {})) > 131_072
            ):
                raise ValueError("Invalid operation data")
            data = data or {}
            supported = operation in RECORDS[game] or operation in {
                "ys_ledger",
                "sign",
                "sign_info",
            }
            supported |= game == "gs" and operation in {
                "detail",
                "avatarSkill",
                "compute",
            }
            supported |= game == "sr" and operation in {"detail", "compute"}
            if not supported:
                return failure(-2, "此游戏尚未接入该查询类型。")
            cookies = self.accounts.credentials(actor, game, uid)
            if not cookies:
                return failure(
                    -100,
                    "此查询需要该账号本人的米游社登录；公开UID绑定不包含登录权限。",
                )
            async with asyncio.timeout(self.timeout):
                # The SDK overwrites per-request app/UA defaults. Its constructor
                # headers are applied last, preserving the registered context.
                headers = (
                    self.accounts.record_context(actor, game, uid)
                    if operation in RECORDS[game]
                    else {}
                )
                client = self.factory(
                    cookies,
                    game,
                    uid,
                    server,
                    **({"headers": headers} if headers else {}),
                )
                result = await self._query(client, game, uid, operation, data)
            if hasattr(result, "model_dump"):
                result = result.model_dump(mode="json")
            elif hasattr(result, "_asdict"):
                result = result._asdict()
            if not isinstance(result, (Mapping, list, type(None))):
                raise ValueError("Unexpected SDK result")
            return {"retcode": 0, "data": result or {}}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if isinstance(exc, IncompleteRoster):
                return failure(
                    -4,
                    "米游社没有返回完整的角色详情，本次未更新全量面板，保留已有缓存。",
                )
            response = getattr(exc, "response", {})
            code = response.get("retcode") if isinstance(response, Mapping) else None
            if isinstance(code, int) and code in ERRORS:
                return failure(code, ERRORS[code])
            if isinstance(exc, TimeoutError):
                return failure(-3, "米游社查询超时，请稍后重试。")
            return failure(-1, "米游社查询失败，请检查账号状态或稍后重试。")

    async def verify_record_context(
        self, actor: str, game: str, uid: str, headers: dict
    ) -> dict:
        """Verify the full roster before saving an administrator's device context.

        Args:
            actor: Trusted credential owner.
            game: Currently only the verified CN Genshin contract is supported.
            uid: This owner's selected game UID.
            headers: Registered device pair and matching client identity.

        Returns:
            Counts of successfully retrieved characters and details, with no IDs.

        Raises:
            ValueError: If ownership, context, region, or complete-roster checks fail.
        """
        headers = self.accounts.check_record_context(headers)
        server = game_server(game, uid)
        cookies = self.accounts.credentials(actor, game, uid)
        if game != "gs" or server not in {"cn_gf01", "cn_qd01"} or not cookies:
            raise ValueError("Record context requires an owned CN Genshin login")
        try:
            async with asyncio.timeout(self.timeout):
                client = self.factory(cookies, game, uid, server, headers=headers)
                roster = await self._query(client, game, uid, "character", {})
                ids = [positive_id(row["id"]) for row in roster["list"]]
                details = await self._query(
                    client, game, uid, "characterDetail", {"character_ids": ids}
                )
        except Exception:
            raise ValueError(
                "Full roster could not verify the device context"
            ) from None
        if self.accounts.credentials(actor, game, uid) != cookies:
            raise ValueError("Login changed while verifying the device context")
        self.accounts.put_record_context(actor, game, uid, headers)
        return {"characters": len(ids), "details": len(details["list"])}

    async def _query(self, client, game: str, uid: str, operation: str, data: dict):
        if game == "gs" and operation == "characterDetail":
            payload = record_payload(game, uid, operation, data)
            requested = list(dict.fromkeys(payload["character_ids"]))
            collected, merged = {}, None
            for offset in range(0, len(requested), 20):
                batch = requested[offset : offset + 20]
                extra = device_headers()
                result = await client.request_game_record(
                    "character/detail",
                    method="POST",
                    data={**payload, "character_ids": batch},
                    **({"headers": extra} if extra else {}),
                )
                rows = result.get("list") if isinstance(result, Mapping) else None
                if not isinstance(rows, list) or any(
                    not isinstance(row, Mapping) for row in rows
                ):
                    raise IncompleteRoster()
                # Raw detail DTOs keep identity under base; SDK model parsing
                # flattens that field, but the worker consumes the original DTO.
                returned = {
                    str(row.get("base", {}).get("id")): row
                    for row in rows
                    if isinstance(row.get("base"), Mapping)
                }
                if set(returned) != {str(value) for value in batch} or len(
                    returned
                ) != len(rows):
                    raise IncompleteRoster()
                if merged is None:
                    merged = dict(result)
                collected.update(returned)
            return {**merged, "list": [collected[str(value)] for value in requested]}
        if operation in RECORDS[game]:
            endpoint, method = RECORDS[game][operation]
            payload = record_payload(game, uid, operation, data)
            extra = device_headers()
            return await client.request_game_record(
                endpoint,
                method=method,
                **({"headers": extra} if extra else {}),
                **({"data": payload} if method == "POST" else {"params": payload}),
            )
        if operation == "ys_ledger":
            month = data.get("month")
            if month is not None and (
                not str(month).isdigit() or len(str(month)) not in {1, 2, 6}
            ):
                raise ValueError("Invalid ledger month")
            if game == "zzz":
                return await client.get_zzz_diary(
                    int(uid), month=str(month) if month else None
                )
            return await client.request_ledger(int(uid), month=month)
        if operation == "sign":
            return await client.claim_daily_reward()
        if operation == "sign_info":
            return await client.get_reward_info()
        if game == "sr" and operation in {"detail", "compute"}:
            host = (
                "https://api-takumi.miyoushe.com"
                if game_server(game, uid) in {"prod_gf_cn", "prod_qd_cn"}
                else "https://sg-public-api.hoyolab.com"
            )
            if operation == "detail":
                tab = data.get("tab_from", "TabOwned")
                if tab not in {"TabOwned", "TabAll"}:
                    raise ValueError("Invalid calculator tab")
                return await client.request_hoyolab(
                    host + "/event/rpgcalc/avatar/detail",
                    method="GET",
                    params={
                        "game": "hkrpg",
                        "lang": "zh-cn",
                        "item_id": positive_id(data.get("avatar_id")),
                        "tab_from": tab,
                        "change_target_level": 0,
                        "uid": uid,
                        "region": game_server(game, uid),
                    },
                )
            body = starrail_compute_payload(
                data.get("body"), uid, game_server(game, uid)
            )
            return await client.request_hoyolab(
                host + "/event/rpgcalc/compute",
                method="POST",
                params={"game": "hkrpg"},
                data=body,
            )
        if operation == "detail":
            return await client.request_calculator(
                "sync/avatar/detail",
                method="GET",
                params={
                    "uid": uid,
                    "region": game_server(game, uid),
                    "avatar_id": positive_id(data.get("avatar_id")),
                },
            )
        if operation == "avatarSkill":
            return await client.request_calculator(
                "avatarSkill/list",
                method="GET",
                params={"avatar_id": positive_id(data.get("avatar_id"))},
            )
        if operation == "compute":
            body = data.get("body")
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("items"), list)
                or not 1 <= len(body["items"]) <= 100
            ):
                raise ValueError("Invalid calculator items")
            body = {
                "items": body["items"],
                "uid": uid,
                "region": game_server(game, uid),
            }
            return await client.request_calculator(
                "batch_compute", api_version=3, data=body
            )
        raise ValueError("Unsupported operation")


def starrail_compute_payload(body, uid: str, server: str) -> dict:
    """Project rpgcalc inputs and inject the trusted selected role context.

    The modern calculator supplies UID and region for inventory context. Neither
    can be replaced by the worker's body. Genshin batch items and URLs are removed.
    """
    if not isinstance(body, dict) or not isinstance(body.get("avatar"), dict):
        raise ValueError("Invalid Star Rail calculator body")

    def item(source, maximum, minimum=1):
        if not isinstance(source, dict):
            raise ValueError("Invalid calculator item")
        current, target = source.get("cur_level"), source.get("target_level")
        if (
            type(current) is not int
            or type(target) is not int
            or not minimum <= current <= target <= maximum
        ):
            raise ValueError("Invalid calculator levels")
        return {
            "item_id": positive_id(source.get("item_id")),
            "cur_level": current,
            "target_level": target,
        }

    skills = body.get("skill_list", [])
    if not isinstance(skills, list) or len(skills) > 100:
        raise ValueError("Invalid calculator skills")
    result = {
        "game": "hkrpg",
        "uid": uid,
        "region": server,
        "avatar": item(body["avatar"], 80),
        "skill_list": [item(skill, 15, 0) for skill in skills],
    }
    if "equipment" in body:
        result["equipment"] = item(body["equipment"], 80)
    return result
