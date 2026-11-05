"""Chat-changeable switches and the panel schema projection.

These live apart from ``main.py`` because that module imports ``astrbot.api``, which
the test environment does not provide. Keeping the tables and the schema mapping in
a plain module makes the panel's behaviour testable.
"""

from __future__ import annotations

import json
from pathlib import Path

# Short titles for the panel and the chat list; the schema description is too long
# for a title line, so it goes into desc where the original layout expects it.
SWITCH_TITLES = {
    "enabled": "启用",
    "all_groups": "全部群",
    "all_private_users": "全部私聊",
    "bare_help": "帮助别名",
    "auto_sign": "自动签到",
    "notify_daily": "签到推送",
    "auto_ledger": "账本采集",
    "runtime_root": "运行目录",
    "node_executable": "Node 可执行文件",
    "groups": "允许的群",
    "private_users": "额外私聊用户",
    "sign_hour": "签到小时",
    "sign_minute": "签到分钟",
    "ledger_hour": "账本采集小时",
    "ledger_minute": "账本采集分钟",
    "backup_hour": "备份小时",
    "backup_minute": "备份分钟",
    "mora_enabled": "攻略配队",
    "liangshi_enabled": "扩展计算",
    "calc_mode": "计算模式",
}
CHAT_TOGGLES = {
    "enabled",
    "all_groups",
    "all_private_users",
    "bare_help",
    "auto_sign",
    "notify_daily",
    "auto_ledger",
    "mora_enabled",
    "liangshi_enabled",
}
CHAT_ALIASES = {
    "启用": "enabled",
    "开关": "enabled",
    "enabled": "enabled",
    "全部群": "all_groups",
    "全群": "all_groups",
    "all_groups": "all_groups",
    "全部私聊": "all_private_users",
    "所有私聊": "all_private_users",
    "all_private_users": "all_private_users",
    "帮助": "bare_help",
    "帮助别名": "bare_help",
    "bare_help": "bare_help",
    "自动签到": "auto_sign",
    "签到": "auto_sign",
    "auto_sign": "auto_sign",
    "签到推送": "notify_daily",
    "每日推送": "notify_daily",
    "notify_daily": "notify_daily",
    "账本采集": "auto_ledger",
    "账本自动采集": "auto_ledger",
    "auto_ledger": "auto_ledger",
    "攻略配队": "mora_enabled",
    "扩展计算": "liangshi_enabled",
}
OFF_WORDS = {"关", "关闭", "否", "不", "false", "0", "off", "no"}

# The upstream help card is built from its own rule list, so none of the native
# commands below appear there. This table is the only place they are listed.
NATIVE_COMMANDS = [
    ("账号", ["#扫码登录", "#绑定 <UID>", "#uid", "#删除ck"]),
    ("签到", ["#签到", "#签到状态", "#签到记录"]),
    ("账本", ["#账本 [月份]", "#账本历史"]),
    ("剧诗 / 幽境", ["#剧诗", "#剧诗圣牌", "#幽境", "#幽境角色"]),
    ("七圣召唤", ["#七圣卡组 [序号]", "#七圣卡牌 <牌名或技能名>"]),
    ("资讯 / 活动", ["#兑换码", "#星铁兑换码", "#活动"]),
    ("抽卡记录", ["#导入抽卡记录", "#抽卡记录状态"]),
    ("星铁养成", ["*养成计算 <角色> [等级…] [全行迹]"]),
    ("主人", ["#喵喵开关", "#推送测试", "#喵喵开发状态"]),
]

SCHEMA_PATH = Path(__file__).with_name("_conf_schema.json")


def config_schema() -> dict[str, dict]:
    """Describe this plugin's real switches in the shape the original page expects.

    Returns:
        cfgKey to ``{key, title, desc, type, def}`` read from ``_conf_schema.json``.
        Integer settings become ``num`` because the original layout renders any
        other type as a boolean badge, which would hide the value.
    """
    raw = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    kinds = {"bool": "bool", "int": "num", "float": "num", "string": "str"}
    return {
        key: {
            "key": key,
            "title": SWITCH_TITLES.get(key) or key,
            "desc": str(item.get("description") or ""),
            "type": kinds.get(str(item.get("type")), "str"),
            "def": item.get("default"),
        }
        for key, item in raw.items()
    }
