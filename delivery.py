"""Send one prepared game chain through the best reply policy this install has.

The hub runs a patched AstrBot that ships ``astrbot.core.utils.reply_policy``.
A stock installation has no such module, so importing it unconditionally takes
the whole plugin down at load time. Importing the framework copy first keeps the
patched deployment's behaviour exactly as it is today; the snapshot beside this
file answers only when that module is absent.

Example:
    from .delivery import send_reply
    sent = await send_reply(event, chain, transport)
"""

from __future__ import annotations

try:  # Patched AstrBot: keep the framework's live policy.
    from astrbot.core.utils.reply_policy import send_reply
except ImportError:  # Stock AstrBot: the shipped snapshot keeps the same guards.
    from .reply_policy import send_reply

__all__ = ["send_reply"]
