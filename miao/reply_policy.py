"""Shared reply formatting and delivery for opted-in bot conversations.

Snapshot of the framework module this deployment patched in, taken 2026-11-05,
and shipped with this plugin so it also loads on a stock AstrBot that never
applied that patch.

``delivery.py`` uses the framework module whenever it exists; this copy answers
only when that import fails. Two deliberate differences from the framework file:
a missing redaction helper falls back to the raw text instead of raising, and the
comments carry no group numbers or bot name. Every rule, regex and constant is
otherwise unchanged, so delivery behaviour cannot drift. Keep the two files in
step when the framework copy changes.
"""

import asyncio
import json
import re
import uuid
from pathlib import Path

from astrbot import logger

MENTION = re.compile(
    r"(?<![\w@])@(\d{5,12})(?!\d)|"
    r"\[at[:\uff1a]\s*(\d{5,12}|all)\s*\]|"
    r"\[CQ:at,qq=(\d{5,12}|all)\]",
    re.IGNORECASE,
)

# AtTool's pre-send hook is skipped for streamed replies, so @all tags are
# resolved here instead; the permission gate lives in send_reply.
AT_ALL = re.compile(r"\[at[:\uff1a]\s*all\s*\]|\[CQ:at,qq=all\]", re.IGNORECASE)

# Invisible filler some models emit so a bare mention still counts as a message.
# str.strip() does not remove these, so without this they reach QQ unchanged and
# the bubble looks blank.
INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff]")

# Bubble-edge punctuation the persona drops: a bubble is a spoken line, so it
# never ends on one of these. Shared by the CHAT split and the EXPERT opening.
TRAILING_PUNCT = ".．。:：…~～,，;；!！?？、"

# --- 定制：闲聊气泡超量时是否硬合并（2026-09-12 置 False）---
# False 表示"写几行发几条"；改回 True 恢复"最多 2 条"的旧行为。
MERGE_CHAT_OVERFLOW = False

# --- 定制：类型标记允许出现在句中（2026-09-13）---
# 旧正则锚在句首（^\s*…），只认开头。但工具调用会让模型分多轮输出，而每一轮
# 都带一次标记：第一轮开头那次被消费掉之后，后面几轮的标记就再没人管 ——
# 2026-09-13 12:26:14 群里收到的「收到，这就去搜。[[EXPERT]]搜到了…」即由此而来。
# 现在改成"出现在哪里都认"，句中出现的那次换成换行：标记本身就代表"新的一轮
# 助手发言从这儿开始"，换成换行正好把两轮拆成两个气泡，否则两轮正文会粘成
# 一句。句首的照旧直接删掉（见 strip_reply_marker）。
MARKER = re.compile(r"\[\[(?:CHAT|EXPERT)\]\][ \t]*")
PROTECTED = re.compile(
    r"```|https?://|<md>|</md>|\$|\\(?:\[|\(|begin\{)|[=≤≥∑√]|^\s*(?:def |class |if .+:|for .+:|while .+:|"
    r"import |from .+ import |return |SELECT |\{|\[)|"
    r"\b\w+\([^\n]*\)|\b\w+\s*=[^=]",
    re.MULTILINE,
)
EXPERT_REQUEST = re.compile(
    r"代码|报错|配置|公式|推导|计算|教程|原理|解释|证明|分析|排查|步骤|"
    r"怎么(?:做|用|配置|安装|实现)|如何|为什么|什么是|区别|技术|专业|"
    r"python|sql|javascript|traceback",
    re.IGNORECASE,
)

MD_OPEN = "<md>"
MD_CLOSE = "</md>"
# Models occasionally emit <br> instead of a newline before a <md> block.
BR_TAG = re.compile(r"<br\s*/?>", re.IGNORECASE)# Stream tags that must never be cut in half by an eager bubble flush.
NORMAL_HOLD_TOKENS = (MD_OPEN, "<br>", "<br/>")
THINK_OPEN_TOKENS = ("<thinking>", "<think>")
THINK_CLOSE_TOKENS = ("</thinking>", "</think>")
AUTO_MD_START = re.compile(
    r"(?m)^[ \t]{0,3}(?:#{1,6}[ \t]+|(?:[-*+]|[0-9]+[.)])[ \t]+|>[ \t]?|\$\$)"
    r"|\*\*[^*\n]+\*\*|(?<!\$)\$[^$\n]+\$(?!\$)"
)

# --- 定制：CHAT 气泡 LaTeX 展平（2026-11-04）---
LATEX_BLOCK = re.compile(
    r"\$\$[\s\S]+?\$\$"          # $$...$$ 展示公式
    r"|\\\[[\s\S]+?\\\]"         # \[...\] 展示公式
    r"|\\\([\s\S]+?\\\)"         # \(...\) 行内公式（括号形式总是公式）
)
INLINE_MATH = re.compile(r"\$([^$\n]+)\$")
MATH_MARK = re.compile(r"[\\^_{}]")
# 命令后缀用 (?![a-zA-Z]) 而不是 \b：\ln2 里 n 和 2 都是 word 字符，\b 不成立
#（2026-11-04 容器实测，\ln2 的 ln 没被翻出来就是这个原因）。
LATEX_MAP = [
    (r"\\displaystyle(?![a-zA-Z])", ""),
    (r"\\left(?![a-zA-Z])|\\right(?![a-zA-Z])", ""),
    (r"\\dfrac(?![a-zA-Z])|\\tfrac(?![a-zA-Z])", ""),
    (r"\\frac\{([^{}]*)\}\{([^{}]*)\}", r"\1/\2"),
    (r"\\sqrt\{([^{}]*)\}", r"√(\1)"),
    (r"\\boxed\{([^{}]*)\}", r"\1"),
    (r"\\cdot(?![a-zA-Z])|\\times(?![a-zA-Z])", "×"),
    (r"\\div(?![a-zA-Z])", "÷"),
    (r"\\pm(?![a-zA-Z])", "±"),
    (r"\\alpha(?![a-zA-Z])", "α"),
    (r"\\beta(?![a-zA-Z])", "β"),
    (r"\\gamma(?![a-zA-Z])", "γ"),
    (r"\\delta(?![a-zA-Z])", "δ"),
    (r"\\varepsilon(?![a-zA-Z])|\\epsilon(?![a-zA-Z])", "ε"),
    (r"\\theta(?![a-zA-Z])", "θ"),
    (r"\\lambda(?![a-zA-Z])", "λ"),
    (r"\\mu(?![a-zA-Z])", "μ"),
    (r"\\sigma(?![a-zA-Z])", "σ"),
    (r"\\omega(?![a-zA-Z])", "ω"),
    (r"\\Delta(?![a-zA-Z])", "Δ"),
    (r"\\Omega(?![a-zA-Z])", "Ω"),
    (r"\\pi(?![a-zA-Z])", "π"),
    (r"\\infty(?![a-zA-Z])", "∞"),
    (r"\\approx(?![a-zA-Z])", "≈"),
    (r"\\equiv(?![a-zA-Z])", "≡"),
    (r"\\neq?(?![a-zA-Z])", "≠"),
    (r"\\leq?(?![a-zA-Z])", "≤"),
    (r"\\geq?(?![a-zA-Z])", "≥"),
    (r"\\to(?![a-zA-Z])|\\rightarrow(?![a-zA-Z])", "→"),
    (r"\\Rightarrow(?![a-zA-Z])", "⇒"),
    (r"\\in(?![a-zA-Z])", "∈"),
    (r"\\int(?![a-zA-Z])", "∫"),
    (r"\\sum(?![a-zA-Z])", "Σ"),
    (r"\\prod(?![a-zA-Z])", "∏"),
    (r"\\partial(?![a-zA-Z])", "∂"),
    (r"\\lim(?![a-zA-Z])", "lim"),
    (r"\\ln(?![a-zA-Z])", "ln"),
    (r"\\log(?![a-zA-Z])", "log"),
    (r"\\sin(?![a-zA-Z])", "sin"),
    (r"\\cos(?![a-zA-Z])", "cos"),
    (r"\\tan(?![a-zA-Z])", "tan"),
    (r"\\quad|\\qquad|\\,|\\;|\\:|\\!|\\ ", " "),
    (r"\\\$", "$"),
    (r"_\{([^{}]*)\}", r"_\1"),
    (r"\^\{([^{}]*)\}", r"^\1"),
    (r"\\[a-zA-Z]+", ""),  # 兜底：未知命令删掉，宁可少个怪词也不露码
    (r"[{}]", ""),
    (r"  +", " "),
]


def _flatten_latex(text: str) -> str:
    for pattern, replacement in LATEX_MAP:
        text = re.sub(pattern, replacement, text)
    return text


def flatten_math(text: str) -> str:
    """把 CHAT 气泡里的 LaTeX 原文翻成可读文本。

    CHAT 约定模型不许输出 LaTeX（no_trailing_punct 的输出约束），但模型不总
    听话 —— 2026-11-04 某个群 两次把 $\\displaystyle\\prod...$ 和
    $$\\boxed{...}$$ 原样发进群。这是两条 CHAT 发送路径（流式/非流式）的公共
    兜底；EXPERT 的公式走 <md>→MathJax 图片，不经过这里。

    Args:
        text: 一段待发送的纯文本。

    Returns:
        LaTeX 已展平的文本；不含公式时原样返回。
    """
    if "$" not in text and "\\" not in text:
        return text

    def strip_block(match: re.Match) -> str:
        inner = match.group(0)
        for wrap in ("$$", "\\[", "\\]", "\\(", "\\)"):
            inner = inner.replace(wrap, "")
        return _flatten_latex(inner.strip())

    text = LATEX_BLOCK.sub(strip_block, text)

    def strip_inline(match: re.Match) -> str:
        inner = match.group(1)
        # $5 and $10 / 20$ 这种钱数没有数学标记，当普通文字保留。
        if not MATH_MARK.search(inner):
            return match.group(0)
        return _flatten_latex(inner.strip())

    return INLINE_MATH.sub(strip_inline, text)


def strip_reply_marker(text: str) -> str:
    """Remove the reply-mode marker from anywhere in a reply.

    Args:
        text: Reply text that may carry one ``[[CHAT]]`` or ``[[EXPERT]]`` marker.

    Returns:
        The text without any such marker. A marker at the start is dropped along
        with the whitespace it sat in; a marker further in becomes a line break,
        because it opens a later assistant turn in the tool loop and those two
        turns must not run together as a single bubble. Every occurrence goes:
        the chat compaction branch merges the whole turn into one component
        first, so a single strip would leave the second turn's marker behind.
    """
    while True:
        match = MARKER.search(text)
        if match is None:
            return text
        head = text[: match.start()]
        tail = text[match.end() :].lstrip("\r\n")
        text = tail if not head.strip() else head.rstrip() + "\n" + tail


def strip_thinking_blocks(text: str) -> str:
    """Remove model-internal thinking blocks from a complete visible reply.

    Some OpenAI-compatible providers put hidden reasoning in the ordinary
    ``content`` field instead of ``reasoning_content``. The provider setting
    that hides reasoning cannot see that distinction, so this final delivery
    guard removes both complete and unfinished blocks before they reach QQ.
    """
    text = re.sub(
        r"<(?:thinking|think)\s*>.*?</(?:thinking|think)\s*>",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"<(?:thinking|think)\s*>.*\Z",
        "",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return re.sub(r"</(?:thinking|think)\s*>", "", text, flags=re.IGNORECASE)


def filter_thinking_stream(
    text: str,
    pending: str = "",
    in_thinking: bool = False,
    seen: list[bool] | None = None,
) -> tuple[str, str, bool]:
    """Filter thinking tags safely even when streaming splits every character.

    Returns visible text, a held partial tag prefix, and the updated block
    state. State must be retained for the whole agent/tool loop, because an
    opening tag and its closing tag can arrive in different model rounds.

    ``seen`` is an optional accumulator: a ``True`` is appended every time an
    opening tag is matched, so a caller can tell afterwards that the turn
    contained thinking content even when the block closed cleanly inside a
    single chunk (a closed block leaves ``in_thinking`` False either way).
    """
    data = pending + text
    visible: list[str] = []
    while data:
        lowered = data.lower()
        tokens = THINK_CLOSE_TOKENS if in_thinking else THINK_OPEN_TOKENS
        positions = [lowered.find(token) for token in tokens]
        positions = [position for position in positions if position >= 0]
        if positions:
            position = min(positions)
            token = next(
                token for token in tokens if lowered.startswith(token, position)
            )
            if not in_thinking:
                visible.append(data[:position])
                if seen is not None:
                    seen.append(True)
            data = data[position + len(token) :]
            in_thinking = not in_thinking
            continue

        safe = hold_partial_tag(lowered, tokens)
        if not in_thinking:
            visible.append(data[:safe])
        return "".join(visible), data[safe:], in_thinking
    return "".join(visible), "", in_thinking


# --- 定制：报错原文不许流进群（2026-09-13）---
# 输出美化插件本来就有一道"报错关键词"拦截，但那个 handler 挂在
# on_decorating_result 上，而 result_decorate/stage.py 对 STREAMING_RESULT 是
# 直接 return 的 —— 流式逐条往外发，钩子根本轮不到。2026-09-13 14:24 群里因此
# 收到一整段 opencode 的 429 原文（连工作区 URL 一起）。
# 这里在发送路径内做同一件事：命中就换一句人话发出去，原文一个字不留，并私聊
# 把脱敏后的原文报给管理员（更早的版本是静默丢弃，见下方 SUPPRESSED_REPLY）。
# 与插件那份关键词表**故意不重合**：那边是"可能出现报错字样就拦"的宽表（含
# "请求失败"这类日常用语），这里是只认"裸报错报告"的窄表，作为框架侧兜底。
ERROR_SIGNATURES = (
    "LLM 响应错误",
    "All chat models failed",
    "All available chat models are unavailable",
    "Traceback (most recent call last)",
)


def find_error_signature(text: str) -> str | None:
    """Return the error signature when ``text`` is a raw error report.

    Args:
        text: Outgoing bubble text about to be handed to the platform.

    Returns:
        The matched signature, or ``None`` when the text is ordinary prose.
    """
    for signature in ERROR_SIGNATURES:
        if signature in text:
            return signature
    return None


# --- 定制：内容审核的固定拒绝话不许进群（2026-09-24）---
# 商汤（sense-nova）这条线在内容审核拦下请求时，会先吐一句固定话，之后才以
# finish_reason=content_filter 结束；而框架的流式路径在拿到那个结束信号之前
# **已经把这句话当成回复发出去了** —— openai_source.py:697 的注释原话是
# "流式内容已通过 yield 发出，记录错误后正常结束即可"。日志里 18 次
# "rejected by the content filter"（SDK 的 ContentFilterFinishReasonError）与群里
# 收到的这句一一对应，最近一次是 2026-09-24 21:51。
# 所以这里**不是整条丢弃，而是把这句抠掉**：实测多数是「拒绝话 + 真回答」粘在
# 一起（如"你好，我无法给到相关内容。行，大人聊，小孩闭嘴"），抠掉之后剩下的
# 正是该发的内容；只有整条都是拒绝话时才不发。
FILTER_REFUSAL_SIGNATURES = ("我无法给到相关内容",)

# 连同紧贴在拒绝话前面的称呼、以及后面的标点一起抠掉。
_FILTER_REFUSAL = re.compile(
    r"(?:(?:你好|您好)\s*[，,]\s*)?我无法给到相关内容\s*[。.！!？?；;，,、\s]*"
)


def strip_filter_refusals(text: str) -> str:
    """Remove provider content-filter refusal sentences from outgoing text.

    Args:
        text: Outgoing bubble text about to be handed to the platform.

    Returns:
        ``text`` with every refusal sentence (plus the greeting and punctuation
        glued to it) removed and the remainder stripped. Empty string when the
        whole bubble was nothing but a refusal.
    """
    if not any(signature in text for signature in FILTER_REFUSAL_SIGNATURES):
        return text
    return _FILTER_REFUSAL.sub("", text).strip()


# --- 定制：命中报错后群里说一句人话 + 私聊告诉管理员（2026-09-13）---
# 原来命中就**静默丢弃**：群里一片安静（用户以为机器人死了），管理员也只有一行日志
# —— 而输出美化插件的那道报错处理本会私聊通知管理员，却被这个更外层的
# 拦截抢在前面，永远轮不到。现在命中后做两件事：把脱敏原文私聊报给管理员，
# 群里换成这一句人话继续走正常发送流程。
#
# 2026-10-22 需求方要求缩短：原来那句「我卡了一下，等下再说」读起来像在跟人解释
# 自己为什么卡住，啰嗦；改成两个字，也就是它平时的说话方式。**别再改回长句**——
# 这一句是事故通报，不是解释，越短越像本人。插件侧的 `error.custom_msg`
# （`data/config/astrbot_plugin_outputpro_config.json`）必须与这里逐字一致，
# 否则两条报错路径在群里的表现会不一样（当初就是为了这点才一起改的）。
SUPPRESSED_REPLY = "卡了"


async def notify_admins_about_suppressed_error(
    event, signature: str, redacted_text: str
) -> None:
    """Private-message every configured admin about a suppressed error report.

    Only the aiocqhttp platform exposes ``send_private_msg``; other platforms are
    skipped, so a missing capability can never break the reply path itself.

    Args:
        event: Event whose reply was suppressed.
        signature: Matched error signature.
        redacted_text: Original reply text, already stripped of secrets.
    """
    bot = getattr(event, "bot", None)
    if not callable(getattr(bot, "send_private_msg", None)):
        return

    try:
        from astrbot.core import astrbot_config

        admins = astrbot_config.get("admins_id", []) or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cannot read admins_id to report a suppressed error: %s", exc)
        return

    report = (
        "机器人这边出错了，这条回复没有发进群。\n"
        f"会话：{getattr(event, 'unified_msg_origin', '')}\n"
        f"特征：{signature}\n"
        "原文（已脱敏）：\n"
        f"{redacted_text[:1500]}"
    )
    for admin in admins:
        try:
            admin_id = int(str(admin).strip())
        except (TypeError, ValueError):
            continue
        try:
            await bot.send_private_msg(
                user_id=admin_id,
                message=[{"type": "text", "data": {"text": report}}],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to report a suppressed error to %s: %s", admin_id, exc
            )


def fallback_mode(prompt: str, contexts=None) -> str:
    """Choose a conservative mode if intent classification is unavailable."""
    if EXPERT_REQUEST.search(prompt) or PROTECTED.search(prompt):
        return "EXPERT"
    # Short follow-ups need the immediately preceding exchange on outages too.
    if re.search(
        r"^(?:继续|然后呢|为什么呢|举个例子|没看懂|详细点|展开说|那怎么办)[？?！!。\s]*$",
        prompt.strip(),
    ):
        recent = [
            m.get("content", "")
            for m in (contexts or [])[-4:]
            if m.get("role") in {"user", "assistant"}
            and isinstance(m.get("content"), str)
        ]
        if any(EXPERT_REQUEST.search(t) or PROTECTED.search(t) for t in recent):
            return "EXPERT"
    return "CHAT"


def split_text(text: str, mode: str) -> list[str]:
    """Split prose without corrupting code, URLs, or indivisible technical text.

    Args:
        text: Generated text; delivery never semantically rewrites it.
        mode: Independently classified CHAT or EXPERT mode.

    Returns:
        Bubble text in original order. CHAT returns one bubble per non-empty
        line, because the persona defines a line as a bubble and commas as
        in-line pauses; EXPERT keeps its clause grouping. Technical blocks
        retain their bytes. No bubble keeps a trailing line terminator, which
        is a bubble boundary rather than content.
    """
    text = strip_reply_marker(text)
    if not text.strip():
        return []
    # --- 定制：行尾换行符是气泡分界，不是正文（2026-09-12）---
    # 流式发送按行切，切出来的每一行都自带结尾的 "\n"。留在正文里 QQ 会在
    # 每个气泡下面多渲染一个空行（末条因为是剩余零头反而不带，看着就是
    # "除了最后一条都多一个换行"）。这里统一剪掉，只剪行尾换行，不动空格。
    text = text.rstrip("\r\n")
    # --- 定制结束 ---
    if PROTECTED.search(text):
        return [text]
    if mode == "EXPERT":
        # 技术正文（<md>、公式、代码、URL）在上面 PROTECTED 那条就整段返回了，
        # 标点一个都不会动；走到这里的都是开场白那种纯文本，只按逗号分组。
        parts = []
        for line in text.splitlines(keepends=True):
            start = 0
            commas = list(re.finditer(r"[，,]", line))
            for match in commas[2::3]:
                parts.append(line[start : match.start()])
                start = match.end()
            # 同上：段落内部的行尾换行同样属于分界，不能跟着正文发出去。
            tail = line[start:].rstrip("\r\n")
            if tail.strip():
                parts.append(tail)
        # --- 定制：EXPERT 开场白也剪句末标点（2026-09-14）---
        # 以前这里写的是"保留标点"，于是开场白的句号/省略号原样发进群
        # （2026-09-14 14:35 那道数学题的开场白就以「更大。」结尾）。
        # 现在按闲聊的同一套标点剪尾巴；剪不到标点的行原样保留（含尾部空白），
        # 免得动到排版。纯标点的碎片直接不发。
        trimmed = []
        for part in parts:
            if not part.strip():
                continue
            core = part.rstrip()
            body = core.rstrip(TRAILING_PUNCT)
            body = body if body != core else part
            if body.strip():
                trimmed.append(body)
        return trimmed
    # CHAT bubbles break on newlines only: one line is one bubble. Commas are
    # in-line pauses, not bubble boundaries, so a three-line persona reply is
    # delivered as three bubbles instead of one bubble per clause.
    output = []
    for line in text.splitlines():
        line = line.strip().rstrip(TRAILING_PUNCT)
        if line:
            output.append(line)
    return output


def save_pending(event, bubbles, error: str, session=None, path=None) -> Path:
    """Persist undelivered components without retrying ambiguous network failures.

    Args:
        event: Originating message event, including its routing information.
        bubbles: Remaining MessageChain objects in delivery order.
        error: Failure description for operator recovery.
        session: Explicit tool destination, if different from the origin.
        path: Existing stream failure record to update after draining the stream.

    Returns:
        Path to the durable pending-delivery record.
    """
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path

    root = Path(get_astrbot_data_path()) / "pending_replies"
    root.mkdir(parents=True, exist_ok=True)
    path = Path(path) if path is not None else root / f"{uuid.uuid4().hex}.json"
    payload = {
        "session": str(session or event.unified_msg_origin),
        "error": error,
        "delivery_status": "uncertain_first_bubble",
        "bubbles": [[c.toDict() for c in b.chain] for b in bubbles],
    }
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8"
    )
    temporary.replace(path)
    logger.error("Undelivered reply saved to %s; inspect before retrying", path)
    return path


def resolve_mentions(
    components: list,
    is_qq_group: bool,
    at_all_allowed: bool,
) -> list:
    """Translate ``[at:...]`` markers into native At components.

    Args:
        components: Message components to scan.
        is_qq_group: Whether the destination is a QQ group.
        at_all_allowed: Whether the sender may mention everyone.

    Returns:
        The scanned components with markers replaced. Non-Plain components and
        text that looks like code are passed through untouched.
    """
    from astrbot.core.message.components import At, AtAll, Plain

    resolved = []
    for component in components:
        if not is_qq_group or not isinstance(component, Plain):
            resolved.append(component)
            continue
        text = component.text
        if PROTECTED.search(MENTION.sub("", text)):
            resolved.append(component)
            continue
        last = 0
        for match in MENTION.finditer(text):
            if match.start() > last:
                resolved.append(Plain(text[last : match.start()]))
            target = next(value for value in match.groups() if value is not None)
            if target.lower() == "all":
                if at_all_allowed:
                    resolved.append(AtAll())
                else:
                    # A written-out "@全体成员" reaches nobody and reads as a
                    # broken mention; refuse the marker instead of faking it.
                    logger.warning("Refused an @all marker from an unauthorised sender")
            else:
                resolved.append(At(qq=target))
            last = match.end()
        if last < len(text):
            resolved.append(Plain(text[last:]))
    return resolved


def has_visible_content(components: list) -> bool:
    """Whether a bubble would show anything beyond a bare mention.

    Args:
        components: Components of one bubble.

    Returns:
        True when the bubble carries text or media. A bubble holding only
        mentions, whitespace, or invisible filler returns False, because it
        reaches the group as a blank message.
    """
    from astrbot.core.message.components import At, AtAll, Plain

    for component in components:
        if isinstance(component, (At, AtAll)):
            continue
        if isinstance(component, Plain):
            text = MENTION.sub("", INVISIBLE.sub("", component.text))
            if text.strip():
                return True
            continue
        return True
    return False


def strip_markdown(text: str, force: bool = False) -> str:
    """Remove Markdown decoration that QQ bubbles would show as literal noise.

    Streaming delivers each line the moment it is generated, so plugins that
    clean the finished response run after the bubble already reached the group.
    Applying the same rules at delivery keeps every path covered.

    Args:
        text: Candidate bubble text.
        force: Strip technical markup only after an image-render failure.

    Returns:
        Text with emphasis, heading, quote, and list markers removed. Code
        fences, inline code, links, URLs, ``<md>`` blocks, and formula-like
        text are returned byte-for-byte.
    """
    if not force and PROTECTED.search(text):
        return text
    if force:
        protected = []

        def preserve(match):
            protected.append(
                next(group for group in match.groups() if group is not None)
            )
            return f"\ufff0{len(protected) - 1}\ufff1"

        text = re.sub(
            r"```[^\n]*\n([\s\S]*?)(?:```|\Z)|`([^`\n]+)`|"
            r"\$\$([\s\S]*?)\$\$|(?<!\$)\$([^$\n]+)\$(?!\$)|"
            r"\\\[([\s\S]*?)\\\]|\\\((.*?)\\\)",
            preserve,
            text,
        )
    text = re.sub(r"```(?:[a-zA-Z0-9+\-]*\s+)?([\s\S]*?)```", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", r"\1 \2", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", text)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    text = re.sub(r"__(.*?)__", r"\1", text)
    # Strict single-marker rules keep multiplication and snake_case intact.
    text = re.sub(r"(?<!\*)\*(?!\s)(.*?)(?<!\s)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!\w)_(?!\s)(.*?)(?<!\s)_(?!\w)", r"\1", text)
    text = re.sub(r"~~(.*?)~~", r"\1", text)
    text = re.sub(r"^(#{1,6})\s+(.*)", r"\2", text, flags=re.MULTILINE)
    text = re.sub(r"^(?:>\s*)+(.*)", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+(.*)", r"\1", text, flags=re.MULTILINE)
    if force:
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        for index, content in enumerate(protected):
            text = text.replace(f"\ufff0{index}\ufff1", content)
    return text


def fuse_plain(components: list) -> list:
    """Glue neighbouring text segments so a newline separator survives.

    The aiocqhttp transport drops every Plain that is only whitespace, so a
    standalone "\\n" separator never reaches QQ and the lines it was meant to
    separate arrive glued into one run-on sentence.

    Args:
        components: Components of one merged bubble.

    Returns:
        The same components with adjacent Plain segments concatenated.
    """
    from astrbot.core.message.components import Plain

    fused = []
    for component in components:
        if fused and isinstance(fused[-1], Plain) and isinstance(component, Plain):
            fused[-1] = Plain(fused[-1].text + component.text)
        else:
            fused.append(component)
    return fused


async def send_reply(event, message, send, session=None):
    """Apply the same policy to normal, tool, and streaming replies.

    Args:
        event: Originating event with optional reply-policy settings.
        message: Message chain to deliver.
        send: Transport callback; prepared chains bypass repeated formatting.
        session: Explicit tool destination for failure recovery.
    """
    from astrbot.core.message.components import Plain

    # Final non-streaming guard. The streaming path additionally keeps state
    # across chunks below so the opener, body and closer can never be emitted as
    # separate bubbles before this complete-message guard gets a chance to run.
    cleaned_components = []
    thinking_removed = False
    for component in message.chain:
        if isinstance(component, Plain):
            cleaned = strip_thinking_blocks(component.text)
            thinking_removed = thinking_removed or cleaned != component.text
            if cleaned:
                cleaned_components.append(Plain(cleaned))
        else:
            cleaned_components.append(component)
    if thinking_removed:
        logger.warning("Suppressed model thinking content before delivery")
        message = message.derive(cleaned_components)
        if not has_visible_content(message.chain):
            return True

    # 定制（2026-09-13）：报错原文一律不进群。放在 prepared 判定之前，否则
    # 已被处理过的链会绕过这里 —— 流式那条路正是这么漏出去的。
    raw_text = message.get_plain_text()
    signature = find_error_signature(raw_text)
    if signature:
        # 定制（2026-09-13）：原来这里是 return True —— 静默丢弃，
        # 群里一片安静、管理员也毫不知情。现在改成：脱敏后私聊报给管理员，群里换成
        # 一句人话继续走正常流程（原文仍然一个字都不进群）。
        try:
            from astrbot.core.utils.error_redaction import redact_sensitive_text
        except ImportError:  # Stock AstrBot has no redaction helper.
            redact_sensitive_text = None
        redacted = redact_sensitive_text(raw_text) if redact_sensitive_text else raw_text
        logger.warning(
            "Suppressed a raw error report before sending (matched %r): %s",
            signature,
            redacted,
        )
        await notify_admins_about_suppressed_error(event, signature, redacted)
        message = message.derive([Plain(SUPPRESSED_REPLY)])
    else:
        # 定制（2026-09-24）：把内容审核的固定拒绝话抠掉，剩下的照发；
        # 整条都是拒绝话时才不发（静默丢弃，只留一条日志）。
        cleaned = strip_filter_refusals(raw_text)
        if cleaned != raw_text:
            if not cleaned:
                logger.warning(
                    "Suppressed a content-filter refusal bubble before sending: %r",
                    raw_text,
                )
                return
            logger.warning(
                "Stripped a content-filter refusal before sending: %r -> %r",
                raw_text,
                cleaned,
            )
            message = message.derive([Plain(cleaned)])

    if getattr(message, "_reply_policy_prepared", False):
        return await send(message)
    # Mentions are parsed on every path; @all additionally requires the sender to
    # hold the right, because AtTool's pre-send gate never runs for streamed,
    # tool-originated, or cron-driven replies.
    # A cron event reports platform "cron" and carries no group_id, so the
    # explicit tool destination is the only reliable signal on that path.
    platform_name = getattr(event, "get_platform_name", lambda: "")()
    group_id = getattr(event, "get_group_id", lambda: "")()
    if session is not None:
        platform_name = str(getattr(session, "platform_name", "") or platform_name)
        message_type = getattr(session, "message_type", None)
        is_group_session = (
            getattr(message_type, "value", message_type) == "GroupMessage"
        )
        if not is_group_session:
            group_id = ""
        elif not group_id:
            # Last resort for destinations the event cannot describe: a cron
            # event never carries a group_id. Under session isolation this is
            # "<user>_<group>", which no group lookup accepts, so the @all
            # check fails closed instead of granting the mention.
            group_id = str(getattr(session, "session_id", "") or "")
    is_qq_group = platform_name == "aiocqhttp" and bool(group_id)
    at_all_allowed = False
    if is_qq_group and any(
        isinstance(component, Plain) and AT_ALL.search(component.text)
        for component in message.chain
    ):
        cached = event.get_extra("xiaoye_at_all_allowed")
        if cached is None:
            # --- 定制：@全体 只认管理员名单（2026-09-12）---
            # 原来还查群成员身份、放行 owner/admin，于是每个群的群管理员都能
            # 一句话让机器人 @全体。需求方要求收紧成"只有管理员"：is_admin() 就是
            # admins_id 名单，其余一律拒绝。
            try:
                cached = bool(
                    callable(getattr(event, "is_admin", None)) and event.is_admin()
                )
            except Exception:
                logger.warning("At-all permission check failed; denying the mention")
                cached = False
            event.set_extra("xiaoye_at_all_allowed", cached)
        at_all_allowed = bool(cached)
    if not event.get_extra("xiaoye_reply_policy", False):
        # Cron jobs and the send_message_to_user tool run the agent outside the
        # message pipeline, so no_trailing_punct's hook never enables the style
        # policy, and AtTool's on_decorating_result converter is not reached
        # either. Markers still have to become real At components before sending.
        components = list(message.chain)
        if any(
            isinstance(component, Plain) and MENTION.search(component.text)
            for component in components
        ):
            components = resolve_mentions(components, is_qq_group, at_all_allowed)
        # --- 定制：策略关闭时也要剪掉类型标记（2026-09-12）---
        # 这条路上原本只处理 @ 标记与隐形字符，[[CHAT]]/[[EXPERT]] 会原样漏到
        # 群里（定时任务与工具直发都会走这里）。补上与其他路径同一套清理。
        components = [
            Plain(strip_reply_marker(INVISIBLE.sub("", component.text)))
            if isinstance(component, Plain)
            else component
            for component in components
        ]
        # --- 定制结束 ---
        if not has_visible_content(components):
            # Nothing but a mention and filler would reach the group as a blank
            # bubble. Report success: this is a policy drop, not a send failure.
            logger.warning(
                "Dropped a mention-only reply to %s", event.unified_msg_origin
            )
            return True
        translated = message.derive(components)
        translated._reply_policy_prepared = True
        return await send(translated)
    mode = event.get_extra("xiaoye_reply_mode", "EXPERT")
    md_fallback = getattr(message, "_reply_policy_md_fallback", False)
    bubbles = []
    current = []
    components = list(message.chain)
    processor = event.get_extra("xiaoye_stream_output_processor")
    if (
        event.get_extra("xiaoye_stream_delivery", False)
        and not event.get_extra("xiaoye_output_processing", False)
        and processor
    ):
        processed = await processor(event, message.derive(components))
        components = processed.chain
    for component in components:
        if not isinstance(component, Plain):
            current.append(component)
            continue
        text = strip_reply_marker(component.text)
        sanitize = event.get_extra("xiaoye_reply_sanitize")
        if mode == "CHAT" and not PROTECTED.search(text) and sanitize:
            text = sanitize(text)
        # The streamed path hands each finished line straight to the platform,
        # so response-level markdown cleanup always loses the race with it.
        text = INVISIBLE.sub("", text)
        # --- 定制：CHAT 气泡里的 LaTeX 展平（2026-11-04）---
        # 2026-11-04 17:05 / 19:28（某个群）模型在 CHAT 轮里吐了        # $\displaystyle\prod...$ 和 $$\boxed{...}$$ 原文直发到群里。CHAT 约定
        # （no_trailing_punct 的输出约束）禁止 CHAT 出 LaTeX，但模型不总听话；
        # markdown_killer 的正则清单没有 LaTeX，而且 PROTECTED 一见 $ 就整段放行。
        # 这里是两条 CHAT 路径（流式/非流式）的公共汇点，在这里展平兜底；
        # EXPERT 的公式走 <md>→MathJax 图片，不受影响。
        if mode == "CHAT" and ("$" in text or "\\" in text):
            text = flatten_math(text)
        if md_fallback:
            # The original Markdown has already been converted to readable
            # text. Running decoration again can turn a*b*c into abc.
            current.append(Plain(text))
            bubbles.append(message.derive(current))
            current = []
            continue
        text = strip_markdown(text)
        if mode == "CHAT" and not PROTECTED.search(MENTION.sub("", text)):
            # Keep component boundaries (including native At) inside a line.
            # Validate the complete turn after resolving component boundaries.
            for line in text.splitlines(keepends=True):
                current.append(Plain(line.rstrip("\r\n")))
                if line.endswith(("\n", "\r")):
                    bubbles.append(message.derive(current))
                    current = []
        else:
            for part in split_text(text, mode):
                current.append(Plain(part))
                bubbles.append(message.derive(current))
                current = []
    if current:
        bubbles.append(message.derive(current))
    prepared = []
    for bubble in bubbles:
        components = list(bubble.chain)
        if mode == "CHAT" and not PROTECTED.search(
            MENTION.sub("", bubble.get_plain_text())
        ):
            # Trim only the bubble edges; spaces around inline mentions matter.
            while components and isinstance(components[0], Plain):
                text = components[0].text.lstrip()
                if text:
                    components[0] = Plain(text)
                    break
                components.pop(0)
            while components and isinstance(components[-1], Plain):
                text = components[-1].text.rstrip().rstrip(TRAILING_PUNCT).rstrip()
                if text:
                    components[-1] = Plain(text)
                    break
                components.pop()
        resolved = resolve_mentions(components, is_qq_group, at_all_allowed)
        if resolved:
            prepared.append(bubble.derive(resolved))
    bubbles = prepared
    folded = []
    carry: list = []
    for bubble in bubbles:
        components = list(bubble.chain)
        if not has_visible_content(components):
            # A thrown-away bubble that only mentions people arrives as a blank
            # message; let the mention ride along with the next bubble instead.
            carry.extend(components)
            continue
        folded.append(bubble.derive(carry + components) if carry else bubble)
        carry = []
    bubbles = folded
    # --- 定制：闲聊精简 2026-09-12 已停用 ---
    # 回调由 astrbot_plugin_no_trailing_punct 的 COMPACT_CHAT_ENABLED 控制，
    # 现在是 False，所以这里拿不到 compact，整块跳过。要恢复就把那个开关改回
    # True —— 这段逻辑本身没动。send_reply_stream 里"攒齐再发"的那条分支同理。
    compact = event.get_extra("xiaoye_compact_chat")
    if (
        mode == "CHAT"
        and compact
        and (len(bubbles) > 2 or any(len(b.get_plain_text()) > 15 for b in bubbles))
    ):
        original = "\n".join(b.get_plain_text() for b in bubbles)
        try:
            shortened = await compact(original)
            lines = [line.strip() for line in shortened.splitlines() if line.strip()]
            if (
                not 1 <= len(lines) <= 2
                or any(len(line) > 15 for line in lines)
                or shortened.strip() == "UNFIT"
            ):
                raise ValueError("Chat compaction did not meet the reply budget")
            if re.findall(r"\d+", original) != re.findall(r"\d+", shortened):
                raise ValueError("Chat compaction changed numeric facts")
            extras = [c for b in bubbles for c in b.chain if not isinstance(c, Plain)]
            bubbles = [message.derive(extras + [Plain(lines[0])])]
            bubbles.extend(message.derive([Plain(line)]) for line in lines[1:])
        except Exception as exc:
            # Shortening is best effort. A rejected or failed compaction says
            # nothing about the reply itself, so deliver the original bubbles
            # instead of losing the whole turn. "UNFIT" is a documented answer
            # from the compactor, not an error.
            logger.warning(
                "Chat compaction skipped (%s: %s); sending the original %s bubble(s)",
                type(exc).__name__,
                exc,
                len(bubbles),
            )
    # --- 定制：合并开关（2026-09-12 置 False，改回逐条发送）---
    # True  = 超过 2 条气泡就硬并成 2 条（原行为，被批评为"把话压变形"）
    # False = 写几行发几条，不再合并
    # 停用合并是为了不再掩盖模型超标；恢复原行为把这里改回 True 即可，
    # 合并本身需要的换行保护（fuse_plain）仍在位。
    # --- 定制结束 ---
    if MERGE_CHAT_OVERFLOW and mode == "CHAT" and len(bubbles) > 2:
        # Merge overflow without rewriting or dropping any text/media components.
        merged = []
        for bubble in bubbles[1:]:
            if merged:
                merged.append(Plain("\n"))
            merged.extend(bubble.chain)
        bubbles = bubbles[:1] + [message.derive(fuse_plain(merged))]
    for index, bubble in enumerate(bubbles):
        try:
            annotate = event.get_extra("xiaoye_stream_image_summary")
            if event.get_extra("xiaoye_stream_delivery", False) and annotate:
                await annotate(event, bubble)
            bubble._reply_policy_prepared = True
            outcome = await send(bubble)
            if outcome is False:
                raise RuntimeError("Reply transport did not find the target platform")
        except Exception as exc:
            event.set_extra("xiaoye_pending_reply", bubbles[index:])
            pending_path = save_pending(
                event, bubbles[index:], str(exc), session=session
            )
            event.set_extra("xiaoye_pending_path", pending_path)
            raise
        if index < len(bubbles) - 1:
            await asyncio.sleep(0.6)
    logger.info(
        "Reply delivered: mode=%s, bubbles=%s, text_lengths=%s",
        mode,
        len(bubbles),
        [len(b.get_plain_text()) for b in bubbles],
    )
    return bool(bubbles)


def hold_partial_tag(text: str, tokens: tuple[str, ...]) -> int:
    """Length of text safe to emit without cutting a partial tag prefix.

    Args:
        text: Buffered text whose tail may be an incomplete tag.
        tokens: Tags to protect; any suffix of text that is a proper prefix
            of a token forces that suffix to stay buffered.

    Returns:
        Index up to which text can be flushed safely.
    """
    safe = len(text)
    longest = max(len(token) for token in tokens) - 1
    for size in range(1, min(len(text), longest) + 1):
        suffix = text[-size:]
        if any(token.startswith(suffix) for token in tokens):
            safe = len(text) - size
    return safe


def _find_md_renderer():
    """Locate the markdown2img plugin's renderer across naming quirks.

    StarMetadata.name comes from metadata.yaml while @register uses a
    different id, so match on the stable substring in either the name or
    the plugin folder name.

    Returns:
        The plugin's _process_text_with_markdown coroutine, or None.
    """
    from astrbot.core.star.star import star_map

    for metadata in list(star_map.values()):
        haystack = "{} {}".format(
            getattr(metadata, "name", "") or "",
            getattr(metadata, "root_dir_name", "") or "",
        )
        if "markdown2img" not in haystack and "md2img" not in haystack:
            continue
        processor = getattr(metadata.star_cls, "_process_text_with_markdown", None)
        if processor is not None:
            return processor
        logger.warning(
            "markdown2img plugin found but not active (star_cls=%r)",
            metadata.star_cls,
        )
    return None


async def render_md_components(content: str) -> list | None:
    """Render a markdown block into image components via the md2img plugin.

    Args:
        content: Markdown text extracted from between <md> tags.

    Returns:
        Component list from the plugin (Image on success), or None when the
        renderer is unavailable so callers can fall back to plain text.
    """
    try:
        processor = _find_md_renderer()
        if processor is None:
            logger.warning("markdown2img renderer not found; <md> block sent as text")
            return None
        components = await processor(f"<md>{content}</md>")
        if components:
            if any(type(component).__name__ == "Image" for component in components):
                return components
            logger.warning(
                "markdown2img returned no image; using readable text fallback"
            )
            return None
        logger.warning("markdown2img returned empty components for <md> block")
        return None
    except Exception as exc:
        logger.warning("markdown image renderer unavailable: %s", exc)
        return None


async def send_reply_stream(event, generator):
    """Stream clauses through send_reply; <md> blocks render as one image.

    Args:
        event: Platform event whose send method uses the shared policy.
        generator: Incoming stream of MessageChain fragments.
    """
    from astrbot.core.message.components import Plain
    from astrbot.core.message.message_event_result import MessageChain

    event.set_extra("xiaoye_stream_delivery", True)
    think_pending = ""
    in_thinking = False

    # Validate the entire chat turn before sending; expert streaming is unchanged.
    if event.get_extra("xiaoye_reply_mode") == "CHAT" and event.get_extra(
        "xiaoye_compact_chat"
    ):
        collected = []
        async for chain in generator:
            if isinstance(chain, MessageChain):
                for component in chain.chain:
                    if isinstance(component, Plain):
                        text, think_pending, in_thinking = filter_thinking_stream(
                            component.text, think_pending, in_thinking
                        )
                        if not text:
                            continue
                        component = Plain(text)
                    if (
                        isinstance(component, Plain)
                        and collected
                        and isinstance(collected[-1], Plain)
                    ):
                        collected[-1] = Plain(collected[-1].text + component.text)
                    else:
                        collected.append(component)
        if think_pending and not in_thinking:
            component = Plain(think_pending)
            if collected and isinstance(collected[-1], Plain):
                collected[-1] = Plain(collected[-1].text + component.text)
            else:
                collected.append(component)
        if collected:
            # --- 定制：这条分支绕过了 send_reply，标记要在这里剪（2026-09-12）---
            # 它把整轮收齐后直接调 event.send，不经过 send_reply，于是
            # MARKER 的清理从没跑过 —— 2026-09-12 17:55:03 群里就是因此收到
            # 一条以 "[[CHAT]]" 开头的原文。这里补上同一套清理。
            collected = [
                Plain(strip_reply_marker(component.text))
                if isinstance(component, Plain)
                else component
                for component in collected
            ]
            # --- 定制结束 ---
            # 同上：这条路也不经过 send_reply，报错拦截同样要在这里补一次。
            compact_text = MessageChain(collected).get_plain_text()
            compact_signature = find_error_signature(compact_text)
            if compact_signature:
                # 与 send_reply 入口同一套处理 —— 脱敏记日志、私聊报管理员、
                # 群里换成一句人话（而不是静默 return）。
                from astrbot.core.utils.error_redaction import redact_sensitive_text

                compact_redacted = redact_sensitive_text(compact_text)
                logger.warning(
                    "Suppressed a raw error report before sending (matched %r): %s",
                    compact_signature,
                    compact_redacted,
                )
                await notify_admins_about_suppressed_error(
                    event, compact_signature, compact_redacted
                )
                collected = [Plain(SUPPRESSED_REPLY)]
            outcome = await event.send(MessageChain(collected))
            if outcome is False:
                raise RuntimeError("Reply transport did not find the target platform")
        return

    buffer = ""
    block = ""
    md_block = ""
    md_extras: list = []
    fenced = False
    beginning = True
    in_md = False
    auto_md = False
    md_processed = False
    pending = []
    failure = None
    pending_path = None
    last_delivery = 0.0
    chat_sent = 0
    chat_tail = []
    held_mentions: list = []
    thinking_seen: list[bool] = []

    async def stream_transport(message):
        nonlocal chat_sent
        if MERGE_CHAT_OVERFLOW and event.get_extra("xiaoye_reply_mode") == "CHAT":
            if chat_sent >= 1:
                chat_tail.append(message)
                return True
            result = await event.send(message)
            if result is not False:
                chat_sent += 1
            return result
        return await event.send(message)

    async def emit(message):
        nonlocal failure, pending_path, last_delivery
        # Only drop a standalone leftover control tag after a Markdown block.
        # Literal tags in code, prose, or replies without a block remain intact.
        if (
            md_processed
            and not fenced
            and len(message.chain) == 1
            and isinstance(message.chain[0], Plain)
            and message.chain[0].text.strip() == MD_CLOSE
        ):
            return
        if failure is not None:
            pending.append(message)
            return
        chain = list(message.chain)
        if not has_visible_content(chain):
            # Streaming has no lookahead, so hold a bare mention until the next
            # bubble arrives rather than sending a blank message now.
            held_mentions.extend(chain)
            return
        if held_mentions:
            md_fallback = getattr(message, "_reply_policy_md_fallback", False)
            message = message.derive(held_mentions + chain)
            if md_fallback:
                message._reply_policy_md_fallback = True
            held_mentions.clear()
        try:
            delay = last_delivery + 0.6 - asyncio.get_running_loop().time()
            if delay > 0:
                await asyncio.sleep(delay)
            await send_reply(event, message, stream_transport)
            last_delivery = asyncio.get_running_loop().time()
        except Exception as exc:
            failure = exc
            pending.extend(event.get_extra("xiaoye_pending_reply", [message]))
            pending_path = event.get_extra("xiaoye_pending_path")

    async def emit_lines(chunk: str) -> str:
        """Emit complete lines from chunk; return the unconsumed tail."""
        nonlocal fenced, block
        while chunk:
            # Bubbles break on newlines only, in CHAT and EXPERT alike; a
            # partial mention tag cannot span a newline.
            boundary = re.search(r"\n", chunk)
            if boundary is None:
                break
            line, chunk = chunk[: boundary.end()], chunk[boundary.end() :]
            if line.lstrip().startswith("```"):
                fenced = not fenced
                block += line
                if not fenced:
                    await emit(MessageChain([Plain(block)]))
                    block = ""
            elif fenced:
                block += line
            elif line.strip():
                await emit(MessageChain([Plain(line)]))
        return chunk

    async def emit_markdown() -> None:
        """Render the buffered <md> block to one image bubble, then extras."""
        nonlocal md_block, md_extras, auto_md, md_processed
        content = md_block.strip()
        if (
            auto_md
            and content.splitlines()
            and content.splitlines()[-1].strip() == MD_CLOSE
        ):
            # An inferred block can retain a trailing close without an opener.
            # Do not treat a tag inside an unfinished code fence as a control.
            code_fence = None
            for line in content.splitlines():
                match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)
                if match is None:
                    continue
                delimiter, suffix = match.groups()
                if code_fence is None:
                    code_fence = delimiter
                elif (
                    delimiter[0] == code_fence[0]
                    and len(delimiter) >= len(code_fence)
                    and not suffix.strip()
                ):
                    code_fence = None
            if code_fence is None:
                content = content[: content.rfind(MD_CLOSE)].rstrip()
        extras = md_extras
        md_block, md_extras, auto_md = "", [], False
        if not content:
            return
        md_processed = True
        components = await render_md_components(content)
        if components is None:
            fallback = MessageChain([Plain(strip_markdown(content, force=True))])
            fallback._reply_policy_md_fallback = True
            await emit(fallback)
        else:
            await emit(MessageChain(components))
        for component in extras:
            await emit(MessageChain([component]))

    try:
        async for chain in generator:
            if not isinstance(chain, MessageChain):
                continue
            for component in chain.chain:
                if not isinstance(component, Plain):
                    if in_md:
                        md_extras.append(component)
                        continue
                    if buffer and not fenced:
                        await emit(MessageChain([Plain(buffer)]))
                        buffer = ""
                    await emit(MessageChain([component]))
                    continue
                visible, think_pending, in_thinking = filter_thinking_stream(
                    component.text, think_pending, in_thinking, thinking_seen
                )
                if not visible:
                    continue
                buffer += visible
                if beginning:
                    if "[[CHAT]]".startswith(buffer.strip()) or "[[EXPERT]]".startswith(
                        buffer.strip()
                    ):
                        continue
                    buffer = strip_reply_marker(buffer)
                    beginning = False
                if not in_md:
                    buffer = BR_TAG.sub("\n", buffer)
                # Newlines delimit semantic units; fenced code stays together.
                while True:
                    if in_md:
                        if auto_md:
                            md_block += buffer
                            buffer = ""
                            break
                        close_at = buffer.find(MD_CLOSE)
                        if close_at == -1:
                            keep = hold_partial_tag(buffer, (MD_CLOSE,))
                            md_block += buffer[:keep]
                            buffer = buffer[keep:]
                            break
                        md_block += buffer[:close_at]
                        buffer = buffer[close_at + len(MD_CLOSE) :]
                        in_md = False
                        await emit_markdown()
                        continue
                    open_at = buffer.find(MD_OPEN)
                    auto_match = (
                        AUTO_MD_START.search(buffer)
                        if event.get_extra("xiaoye_reply_mode") == "EXPERT"
                        and not fenced
                        else None
                    )
                    if auto_match and (open_at == -1 or auto_match.start() < open_at):
                        head, buffer = (
                            buffer[: auto_match.start()],
                            buffer[auto_match.start() :],
                        )
                        in_md = auto_md = True
                        tail = await emit_lines(head)
                        if tail:
                            await emit(MessageChain([Plain(tail)]))
                        continue
                    if open_at != -1:
                        head = buffer[:open_at]
                        buffer = buffer[open_at + len(MD_OPEN) :]
                        in_md = True
                        tail = await emit_lines(head)
                        if tail:
                            await emit(MessageChain([Plain(tail)]))
                        continue
                    safe = hold_partial_tag(buffer, NORMAL_HOLD_TOKENS)
                    chunk, buffer = buffer[:safe], buffer[safe:]
                    buffer = await emit_lines(chunk) + buffer
                    break
        if think_pending and not in_thinking:
            buffer += think_pending
            think_pending = ""
        if in_thinking:
            logger.warning("Suppressed an unfinished model thinking block")
        if in_md:
            md_block += buffer
            buffer = ""
            await emit_markdown()
        if block or buffer:
            await emit(MessageChain([Plain(block + buffer)]))
        elif thinking_seen:
            # 模型把整段回答塞进了 <think>…</think>：逐块过滤后一个字都不剩，
            # emit() 从未被调用 → send_reply() 从未跑过 → 连 "Reply delivered"
            # 都没有。以前这种情况**一条日志都不留**（只有"思考块未闭合"才告警），
            # 排查时完全无从下手，所以这里必须留下痕迹。
            logger.warning(
                "Suppressed the whole reply as model thinking content; "
                "nothing visible was left to send"
            )
    except Exception as exc:
        failure = failure or exc
        if in_md and (md_block or buffer):
            pending.append(MessageChain([Plain(md_block + buffer)]))
            md_block = buffer = ""
        if block or buffer:
            pending.append(MessageChain([Plain(block + buffer)]))
            block = buffer = ""
    if chat_tail:
        merged = []
        for part in chat_tail:
            if merged:
                merged.append(Plain("\n"))
            merged.extend(part.chain)
        last = MessageChain(fuse_plain(merged))
        last._reply_policy_prepared = True
        if failure is not None:
            pending.insert(0, last)
        else:
            try:
                outcome = await event.send(last)
                if outcome is False:
                    raise RuntimeError(
                        "Reply transport did not find the target platform"
                    )
            except Exception as exc:
                failure = exc
                pending.insert(0, last)
    if failure is not None:
        if pending:
            event.set_extra("xiaoye_pending_reply", pending)
            saved = save_pending(event, pending, str(failure), path=pending_path)
            event.set_extra("xiaoye_pending_path", saved)
        raise failure
