"""Normalize original platform messages without inventing sender identity.

Example:
    incoming = Incoming.from_event(event)
    packet = incoming.worker_payload()

Only the normalized Miao text changes; original text, IDs and game selection
remain available for privacy interception and platform callbacks.
"""

from __future__ import annotations

import base64
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname
from uuid import uuid4

from .accounts import actor_key

SR_PREFIX = re.compile(r"^(?:崩坏星穹铁道|星穹铁道|星铁|星轨|穹轨|星穹|崩铁|铁道|\*)+")
ZZZ_PREFIX = re.compile(r"^(?:绝区零|绝区|%|％)+")


def game_text(text: str) -> tuple[str, str]:
    """Normalize game prefixes to the upstream command contract.

    Args:
        text: Original plain message text.

    Returns:
        The game key and normalized command text.
    """
    text = text.strip()
    # An explicit hash command is a game-help alias, not ordinary conversation.
    # `#帮助` 之外的两个别名与 main.HELP_ALIASES 保持一致。
    if text in {"#帮助", "#米游帮助", "#米游功能"}:
        return "gs", "#喵喵帮助"
    body = text[1:] if text.startswith("#") else text
    match = SR_PREFIX.match(body)
    if match:
        return "sr", "#星铁" + body[match.end() :]
    match = ZZZ_PREFIX.match(body)
    if match:
        return "zzz", "#绝区零" + body[match.end() :]
    return "gs", text


@dataclass(frozen=True)
class Incoming:
    """Keep original identities immutable throughout the worker call."""

    platform: str
    self_id: str
    user_id: str
    group_id: str
    message_id: str
    original_text: str
    text: str
    game: str
    actor: str
    sender_name: str = ""
    is_master: bool = False
    is_admin: bool = False
    private: bool = False
    event_type: str = "message"
    target_id: str = ""
    reply_id: str = ""
    mentions: tuple[str, ...] = ()
    segments: tuple[dict, ...] = ()
    token: str = field(default_factory=lambda: uuid4().hex)

    @classmethod
    def from_event(cls, event):
        """Read original AstrBot components and OneBot notice fields.

        Args:
            event: The actual AstrBot event, before automatic wake mutations.

        Returns:
            An immutable incoming message with a new per-request token.
        """
        platform = str(event.get_platform_id())
        bot = str(event.get_self_id())
        user = str(event.get_sender_id())
        group = str(event.get_group_id() or "")
        raw = getattr(event.message_obj, "raw_message", {}) or {}
        segments, mentions, texts = [], [], []
        reply_id = ""
        for part in event.get_messages():
            kind = type(part).__name__.lower()
            if kind == "at":
                target = str(part.qq)
                mentions.append(target)
                segments.append({"type": "at", "qq": target})
            elif kind == "reply":
                reply_id = str(part.id)
                segments.append({"type": "reply", "id": reply_id})
            elif kind == "image":
                url = str(part.url or part.file or "")
                segments.extend(
                    miao_segments(
                        [
                            {
                                "type": "image",
                                "data": {
                                    "url": url,
                                    "file": str(part.file or "image.png"),
                                },
                            }
                        ]
                    )
                )
            elif kind == "plain":
                segments.append({"type": "text", "text": str(part.text)})
                texts.append(str(part.text))
            elif kind == "video":
                url = str(getattr(part, "url", "") or getattr(part, "file", "") or "")
                segments.append(
                    {
                        "type": "video",
                        "url": url if urlparse(url).scheme in {"http", "https"} else "",
                    }
                )
            elif kind == "file":
                url = str(getattr(part, "url", "") or "")
                segments.append(
                    {
                        "type": "file",
                        "id": str(getattr(part, "file_id", "") or ""),
                        "name": str(getattr(part, "name", "") or ""),
                        "url": url if urlparse(url).scheme in {"http", "https"} else "",
                    }
                )
        # AstrBot renders a mention back into message_str as " @昵称(12345) ", so
        # "@群友 #火神面板" would arrive as "@群友(12345) #火神面板" and no command
        # pattern would match. The command is the plain segments only; the mention
        # itself stays in `mentions`/`segments` for target resolution.
        command = "".join(texts)
        # original_text keeps the raw text for credential-leak interception.
        original = str(event.message_str or "")
        game, text = game_text(command)
        post_type = raw.get("post_type", "message")
        poke = post_type == "notice" and raw.get("sub_type") == "poke"
        return cls(
            platform=platform,
            self_id=bot,
            user_id=str(raw.get("operator_id") or user) if poke else user,
            group_id=group,
            message_id=str(event.message_obj.message_id or ""),
            original_text=original,
            text=text,
            game=game,
            actor=actor_key(
                platform, bot, str(raw.get("operator_id") or user) if poke else user
            ),
            sender_name=str(event.get_sender_name() or ""),
            is_master=bool(event.is_admin()),
            is_admin=raw.get("sender", {}).get("role") in {"admin", "owner"},
            private=event.is_private_chat(),
            event_type="poke" if poke else post_type,
            target_id=str(raw.get("target_id", "")),
            reply_id=reply_id,
            mentions=tuple(mentions),
            segments=tuple(segments),
        )

    def worker_payload(self) -> dict:
        """Return serializable data without a raw event object or credentials."""
        return asdict(self)


def miao_segments(segments) -> list[dict]:
    """Flatten OneBot message components to the original Miao input shape.

    Local transport paths are not exposed as image URLs. Image names are reduced
    to a basename because the upstream upload handler uses them as filenames.
    """
    if not isinstance(segments, (list, tuple)):
        return []
    result = []
    for segment in segments[:500]:
        if not isinstance(segment, dict):
            continue
        kind = segment.get("type")
        data = segment.get("data", segment)
        if not isinstance(data, dict):
            continue
        if kind == "text":
            result.append({"type": "text", "text": str(data.get("text", ""))})
        elif kind == "image":
            url = str(data.get("url") or data.get("file") or "")
            if urlparse(url).scheme not in {"http", "https"}:
                continue
            filename = (
                str(data.get("file") or "image.png").replace("\\", "/").split("/")[-1]
            )
            filename = filename.split("?")[0]
            filename = re.sub(r"[^\w.\-]", "_", filename)[:160]
            result.append(
                {"type": "image", "url": url, "file": filename or "image.png"}
            )
        elif kind in {"at", "reply", "forward"}:
            key = "qq" if kind == "at" else "id"
            result.append({"type": kind, key: str(data.get(key, ""))})
    return result


def onebot_segments(value, root: Path) -> list[dict]:
    """Convert typed game output without interpreting embedded CQ text.

    Args:
        value: Typed worker message or nested sequence of messages.
        root: The owned runtime; local media must stay inside it.

    Returns:
        OneBot JSON segments, with local images encoded for remote SL access.

    Raises:
        ValueError: If a media path escapes the owned runtime or a type is unknown.
    """
    if value is None or value is False:
        return []
    if isinstance(value, str):
        return [{"type": "text", "data": {"text": value}}]
    if isinstance(value, (tuple, list)):
        parts = [(item, onebot_segments(item, root)) for item in value]
        image_reply = any(
            segment["type"] == "image" for _, segments in parts for segment in segments
        )
        # QQ has no native version of the upstream button rows. Do not turn
        # those rows into command captions next to rendered game images.
        return [
            segment
            for item, segments in parts
            if not (
                image_reply and isinstance(item, dict) and item.get("type") == "buttons"
            )
            for segment in segments
        ]
    if not isinstance(value, dict):
        raise ValueError("Invalid game output")
    kind = value.get("type")
    if kind == "text":
        return [{"type": "text", "data": {"text": str(value.get("text", ""))}}]
    if kind == "at":
        return [{"type": "at", "data": {"qq": str(value["qq"])}}]
    if kind == "buttons":
        commands = []
        for row in value.get("rows", []):
            for button in row if isinstance(row, list) else [row]:
                if isinstance(button, dict):
                    command = (
                        button.get("callback")
                        or button.get("input")
                        or button.get("link")
                    )
                    if command:
                        commands.append(str(command))
        return (
            onebot_segments("\n" + " · ".join(dict.fromkeys(commands)), root)
            if commands
            else []
        )
    if kind == "image":
        file = str(value.get("file", ""))
        if file.startswith(("http://", "https://", "base64://")):
            return [{"type": "image", "data": {"file": file}}]
        path = (
            Path(url2pathname(urlparse(file).path))
            if file.startswith("file://")
            else Path(file)
        )
        path = path.resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            raise ValueError("Game image escapes the runtime")
        if path.stat().st_size > 24 * 1024 * 1024:
            raise ValueError("Game image exceeds the size limit")
        encoded = base64.b64encode(path.read_bytes()).decode()
        return [{"type": "image", "data": {"file": "base64://" + encoded}}]
    if kind == "file":
        file = str(value.get("file", ""))
        path = (
            Path(url2pathname(urlparse(file).path))
            if file.startswith("file://")
            else Path(file)
        )
        path = path.resolve()
        if (
            not path.is_relative_to(root.resolve() / "output" / "exports")
            or not path.is_file()
            or path.suffix != ".json"
        ):
            raise ValueError("Game export escapes the owned output")
        return [
            {
                "type": "file",
                "data": {
                    "file": path.as_uri(),
                    "name": str(value.get("name") or path.name),
                },
            }
        ]
    if kind == "forward":
        return [
            {
                "type": "node",
                "data": {
                    "user_id": str(node.get("user_id") or node.get("uin") or "0"),
                    "nickname": str(node.get("nickname") or node.get("name") or "喵喵"),
                    "content": onebot_segments(
                        node.get("message", node.get("content", [])), root
                    ),
                },
            }
            for node in value.get("nodes", [])
        ]
    raise ValueError("Unsupported game output type")


def game_chain(value, root: Path):
    """Build real native components so final send guards see the actual content.

    Args:
        value: Typed worker output.
        root: Owned runtime media root.

    Returns:
        A native AstrBot MessageChain without CHAT formatting metadata.
    """
    from astrbot.api.message_components import At, File, Image, Node, Plain
    from astrbot.core.message.message_event_result import MessageChain

    def component(segment):
        kind, data = segment["type"], segment["data"]
        if kind == "text":
            return Plain(data["text"], convert=False)
        if kind == "image":
            return Image(file=data["file"])
        if kind == "at":
            return At(qq=data["qq"])
        if kind == "file":
            return File(name=data["name"], file=data["file"])
        return Node(
            content=[component(child) for child in data["content"]],
            uin=data["user_id"],
            name=data["nickname"],
        )

    chain = MessageChain(
        [component(segment) for segment in onebot_segments(value, root)]
    )
    chain._reply_policy_prepared = True
    return chain
