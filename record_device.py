"""Register Snap Hutao's pinned MIT desktop compatibility profile with Miyoushe.

The profile describes an application client, not the user's phone. Keep the BBS
device ID and returned official fingerprint together. Never invent a fallback FP.
Source and license are preserved in record-device-profile.json and THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import json
import secrets
import string
import time
from pathlib import Path
from uuid import uuid4

from .accounts import Accounts


async def register_device() -> dict[str, str]:
    """Register one client and return its matched device and SDK constructor headers.

    Returns:
        Private client headers containing the actual registered BBS device pair.

    Raises:
        ValueError: If the official service does not accept the registration.
    """
    import aiohttp
    from genshin.client.routes import GET_FP_URL
    from genshin.types import Region

    profile = json.loads(
        Path(__file__)
        .with_name("record-device-profile.json")
        .read_text(encoding="utf-8")
    )
    alphabet = string.ascii_uppercase + string.digits
    device = "".join(secrets.choice(alphabet) for _ in range(12))
    product = "".join(secrets.choice(alphabet) for _ in range(6))
    fields = {
        key: value.replace("{device}", device).replace("{product}", product)
        if isinstance(value, str)
        else value
        for key, value in profile["fields"].items()
    }
    device_id = str(uuid4())
    body = {
        "device_id": secrets.token_hex(8),
        "seed_id": str(uuid4()),
        "platform": "2",
        "seed_time": str(int(time.time() * 1000)),
        "ext_fields": json.dumps(fields, separators=(",", ":")),
        "app_name": "bbs_cn",
        "bbs_device_id": device_id,
        "device_fp": secrets.token_hex(7)[:13],
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) miHoYoBBS/2.102.1",
        "x-rpc-app_version": "2.102.1",
        "x-rpc-client_type": "5",
        "x-rpc-device_id": device_id,
    }
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=12)
    ) as session:
        async with session.post(
            GET_FP_URL.get_url(Region.CHINESE),
            json=body,
            headers={**headers, "Accept": "application/json"},
        ) as response:
            response.raise_for_status()
            result = await response.json()
    data = result.get("data") or {}
    if result.get("retcode") != 0 or data.get("code") != 200:
        raise ValueError("Miyoushe rejected device registration")
    headers["x-rpc-device_fp"] = data.get("device_fp")
    return Accounts.check_record_context(headers)
