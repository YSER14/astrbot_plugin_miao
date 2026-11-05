# 喵喵 · 米哈游游戏

**AstrBot 插件** · 把 [miao-plugin](https://github.com/yoimiya-kokomi/miao-plugin) 的计算与渲染接到 AstrBot 上，QQ 侧只保留一个机器人。

![license](https://img.shields.io/badge/license-MIT-blue)
![python](https://img.shields.io/badge/python-3.12%2B-blue)
![node](https://img.shields.io/badge/node-%E2%89%A522.12-brightgreen)
![astrbot](https://img.shields.io/badge/astrbot-%E2%89%A54.28-6f42c1)

> 开发中，默认关闭。原喵喵 65 个入口与 genshin 67 条规则是验收基线，不是已支持的功能数量。

## 它是什么，它不是什么

**是**：适配层。AstrBot 侧拥有事件、账号、记录与任务生命周期；游戏计算与出图交给一个隔离的 Node 进程，复用锁定版本的喵喵算法、数据与模板。没有 Yunzai 主进程，也不开第二个 QQ 登录。

**不是**：开箱即用的完整插件包。`main.py` 还会导入 `addons`、`databackup`、`ledger`、`notify`，运行还需要自备上游 miao-plugin 的检出目录、Node ≥ 22.12、专用 Redis 与 headless Chromium。只把本目录拷进 AstrBot 是启动不起来的。

上游代码与玩家数据放在插件数据目录，与这里的适配源码分开。

## 能力一览

| 游戏 | 已接 | 未接 / 受限 |
| --- | --- | --- |
| 原神 | 面板、剧诗、幽境、七圣召唤、账本、签到、抽卡记录、直播兑换码、预设面板、群内同角色排名 | `spiralAbyss` 与 `basicInfo` 仍被 5003 拦住，暂无深渊记录与七圣总体数据 |
| 星铁 | 便笺、签到、养成计算、直播兑换码、预设「极限」面板 | 账本、剧诗、幽境未开放；养成未用真实账号验收 |
| 绝区零 | 便笺、签到、扫码自动绑定 | 直播兑换码与账本未做 |

## 运行前提

- Python 3.12+，依赖见 `requirements.txt`（`genshin[auth]`）
- Node ≥ 22.12，依赖用随包的 `worker/package-lock.json` 安装
- 一个专用 Redis，以及 headless Chromium（出图用）
- 上游 miao-plugin 的干净检出，固定提交见 `upstreams.lock.json`
- 打码是可选件，不装也能跑；依赖、放置与边界见下文「打码」一节

准备隔离运行目录：

```powershell
python miao/prepare.py --source <miao-checkout> --dest <runtime>
cd <runtime>
npm ci --ignore-scripts --no-audit --no-fund
```

`prepare.py` 会核对提交号、拒绝有改动的检出，并把兼容补丁记进 `runtime-receipt.json`。重新 prepare 会覆盖 `plugins/miao-plugin`，帮助卡片定制（`worker/help-config.js` → `<runtime>/plugins/miao-plugin/config/help.js`）要再拷一次。不要用开发版直接替换正式运行的插件。

## 指令速查

| 用途 | 指令 |
| --- | --- |
| 登录与账号 | `#扫码登录`、`#uid`、`#切换 序号` |
| 签到 | `#签到`、`#签到记录`、`#推送测试`（主人） |
| 面板 | `#更新面板`、`#米游社更新面板`、`@群友 #火神面板`、`@群友 #面板列表` |
| 账本与战绩 | `#账本 [月份]`、`#账本历史`、`#剧诗`、`#幽境`、`#幽境角色` |
| 七圣召唤 | `#七圣卡组`、`#七圣卡组 <序号>`、`#七圣卡牌 <牌名或技能名>` |
| 抽卡记录 | `#导入抽卡记录`、`#喵喵抽卡记录`、`#喵喵抽卡全部记录`、`#喵喵抽卡统计`、`#抽卡记录状态` |
| 兑换码与活动 | `#兑换码`、`#星铁兑换码`、`#活动` |
| 群内排名 | `#火神排名`、`#火神评分排名`、`#刷新排名` |
| 预设面板 | `#极限玛薇卡`、`#玛薇卡核爆面板`、`#核爆玛薇卡` |
| 星铁养成 | `*养成计算 流萤 80 80 6 10 10 10`、`*流萤养成` |
| 帮助 | `#帮助` / `#喵喵帮助` / `#原生功能` |

上游帮助卡片只覆盖原神那五组。星铁、账号、签到、账本、预设面板、群内查询这些入口追加在 `worker/help-config.js`；原生新增的入口（账本、剧诗、幽境、七圣、开关等）不在卡片里，用 `#原生功能`（`#原生指令` / `#喵喵功能`）分组查看。

## 几个关键行为

**扫码登录**　群里直接发 `#扫码登录`，二维码引用指令发出，账号始终绑定给发起人。检测到已扫码即撤回；群内 90 秒、私聊 180 秒有效。撤回只用本次二维码的真实发送回执，不动用户指令；撤回失败会提示，已保存的登录不受影响。米游社确认后自动绑定该账号实际拥有的国服角色，每个游戏默认选等级最高的一个。

**面板更新**　本人已登录时优先读米游社完整角色列表并分批取详情；未登录或查他人 UID 时走公开游戏展柜。列表失败、详情缺项或某批报错都不算全量成功，也不会把展柜缓存当成新数据。5003 是设备或安全校验拒绝，与交互验证码 1034 分开处理。

**设备上下文**　`accounts.sqlite3` 的 `record_contexts` 按发送者、游戏、UID 与凭据隔离保存设备 ID、FP 与客户端头。换号不继承，解绑即删。保留设备能少一次重复注册，但不保证免除官方验证；需要人工验证时走 `miao/record_device.py` 与 `miao/account_admin.py` 的终端入口。

**数据隔离**　绑定、凭据、设备上下文与面板缓存按真实 QQ 身份隔离；群排名按机器人、群、游戏隔离，退群或解绑在下次查询时排除。私有接口不会被群排名借用，@ 别人只改查询对象，不能当越权。

**投递兜底**　`reply_policy.py` 是框架回复策略的快照，`delivery.py` 在框架存在该模块时用框架的、没有时用快照，所以干净安装的 AstrBot 也能加载本插件。改了框架那份，记得同步快照。

**每日维护**　`databackup.py` 每天 04:17 备份四个数据库（保留最近 7 代，默认开启）；`ledger.py` 每天 00:17 把当月账本落库并回填可查的更早月份（`auto_ledger` 默认关）；`tasks.py` 按 `sign_hour` / `sign_minute` 自动签到（`auto_sign` 默认关，不打开时凌晨那次本身也不跑，签到结果可推给主人私聊）。

## 打码（极验点选，实验性）

米游社在设备注册、`dailyNote` 和部分角色接口上会回 1034，附带一次极验点选验证。走通它要五步：原接口回 1034 并从响应头取 `x-trace-id` → 用 `createVerification` 换回 `gt` 与 `challenge` → 在本地点选题图，拿到 `validate` → 把 `validate` 提交给 `verifyVerification`，换回一次性的 `x-rpc-challenge` → 带着这个头重发原请求。中间任何一步失败，都按原来的 1034 文案回给用户，不新增错误提示。

**代码在哪**　极验协议层 `crack.py` 和它要读的 `mousepath.json` 来自 [ravizhan/geetest-v3-click-crack](https://github.com/ravizhan/geetest-v3-click-crack)，AGPL-3.0，与本仓库的 MIT 冲突，所以不在本仓库里：部署时自行放进插件数据目录的 `solver/`。匹配那两层（大图图标检测、3×3 单元格比对）是自己写的，目前也还留在开发目录，尚未并入。

**依赖**　单独装，别写进 `requirements.txt`，否则干净安装会被图像库拖到几百 MB：`opencv-python 4.10.0.84`、`numpy 2.1.x`、`ddddocr 1.6.1`、`scipy 1.18.1`、`httpx 0.27.2`、`cryptography 43.0.3`。装进插件数据目录的 `vendor/`，容器重建时不会丢。模型不用装——这条链路不依赖 ONNX，开发目录里那 335MB 的 `clip-visual.onnx` 和 43MB 的 `yolov8s.onnx` 都用不上。

**现在的状态**　仍是终端脚本，没有接进任何指令、开关或定时任务；`dailyNote` 依旧按「风控」处理，体力与便笺提醒照旧没有。要用就在终端手动跑一次，拿到 `x-rpc-challenge` 后重发那次请求。

**别踩的**　只验过原神（`x-rpc-challenge_game=2`），星铁与绝区零对应的取值没验过，别猜着填；挑战约 10 分钟过期，`validate` 更短，取图到提交至少隔 2 秒，更快会回 `duration short`；容器每 6 小时重启，求解器按需起进程、用完就退，别做成常驻；5003 是设备或账号风控，不是打码能解的东西。

## 已知限制

- 账本、剧诗、幽境**只开放原神**：星铁与绝区零的回包形状不同且没有真实账号，命令直接回「只开放原神」而不猜。
- 体力/便笺提醒没做 —— `dailyNote` 卡在米游社 geetest 风控。打码求解器见「打码」一节，目前只到终端脚本，还没接进这条链路。
- 抽卡记录只做了 UIGF 导入与终端导出；authkey 拉取、群文件清单取得、旧档 `data/gachaJson` 导入器都没做，也尚未做 UIGF 标准认证。
- 星铁养成的四列材料表、强化形态与多 UID 尚未用真实账号验收。
- `#喵喵更新图像`、`#喵喵更新`、`#喵喵api` 仍只回「迁移中」：分别涉及 git 操作、重启进程与明文 token。
- 成就功能已撤下：没有消息处理器、配置开关或帮助入口，旧档案只作保留。

## 目录结构

```
main.py                 AstrBot 入口：事件、权限、发送
metadata.yaml           插件元数据（AstrBot 从这里读）
_conf_schema.json       配置项定义（WebUI 渲染用）
record-device-profile.json  Snap Hutao 设备兼容配置
worker/                 Node 运行层：渲染、计算、上游桥接

miao/                   游戏逻辑
  commands.py           原生指令分发（账本、剧诗、幽境、七圣、开关）
  accounts.py           绑定、凭据、设备上下文（SQLite）
  hoyolab.py            米游社接口与错误码映射
  login.py              扫码登录与自动绑定
  wishes.py             UIGF 抽卡记录
  ledger.py             账本落库与历史
  rankings.py           群内同角色排名
  growth.py             星铁养成计算
  tasks.py              每日签到
  databackup.py         数据库每日备份
  engine.py             带超时的双向 stdio 请求
  prepare.py            准备固定版本的隔离运行目录
  delivery.py           回复策略选择：框架优先，快照兜底
  reply_policy.py       框架回复策略快照
```

判 `miao/` 只是为了让根目录只留入口和元数据；同包相对导入，所以加模块仍然是「新建一个文件」。本仓库是适配层快照，未包含 `addons.py`、`notify.py` 等模块；完整插件以运行目录为准。

## 第三方与许可

本仓库代码以 MIT 发布，见 `LICENSE`。第三方来源、版本与许可全文记录在 `THIRD_PARTY_NOTICES.md`：liangshi-calc（MIT，预设数据）、Mora（GPL-3.0，运行时自行获取）、zolay achievements（MIT，已撤下但保留声明）、Snap Hutao（MIT，设备兼容配置）、Cocogoat（BSD-3-Clause，协议参考）、ravizhan/geetest-v3-click-crack（AGPL-3.0，打码协议层，不随本仓库分发）。

上游 miao-plugin 以 MIT 发布，按 `upstreams.lock.json` 的固定提交在运行时取用，不随本仓库分发。
