/** Load the pinned MIT models/apps after installing the explicit runtime globals. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { callPlatform, scope } from './main.mjs';
import * as renderer from './render.mjs';
import { prepareRanking } from './rankings.mjs';
import { addonEnabled, loadAddons, preparePreset, recognizesPreset } from './addons.mjs';

const root = process.cwd();
const vendor = path.join(root, 'plugins', 'miao-plugin');
let registered;
let models;
let client;
let redisLoading;
let commandTail = Promise.resolve();
const logs = (...values) => console.error(...values);
const digest = (values) => createHash('sha256').update(JSON.stringify(values)).digest('hex');
// Caches and panel files are owned by one QQ account each. A panel command may
// name that owner with a mention instead of the sender.
const ownerOf = (event, user) => digest([event.platform, event.self_id, String(user)]);
// Group ranking rules build their own multi-member view, so they are the only
// profile rules that must keep running in the sender's namespace.
const GROUP_RANK_RULES = new Set(['rankList', 'groupProfile', 'refreshRank', 'resetRank', 'manageRank']);
// Profile rules that must resolve the subject's UID before they can answer; when
// the subject is a mention, its own bindings decide the result.
const UID_RULES = new Set(['profileList', 'profileDetail', 'artisList', 'profileRefresh', 'profileRefreshMys']);
// A mention selects another member only when it is a real QQ number that is not
// the sender; "@自己" keeps behaving exactly like a command without a mention.
const mentionedUser = (event) => /^\d{5,12}$/.test(event.at || '') && event.at !== event.user_id ? event.at : '';
globalThis.miaoCacheKey = (key) => /^(?:player|user):/.test(key) ? `${scope.getStore()?.dataOwner || 'system'}:${key}` : key;
globalThis.miaoDataRoot = () => {
  const directory = scope.getStore()?.rankDataRoot || path.join(root, 'instances', scope.getStore()?.dataOwner || 'system');
  for (const relative of ['data/UserData', 'data/PlayerData/gs', 'data/PlayerData/sr']) fs.mkdirSync(path.join(directory, relative), { recursive: true });
  return directory + path.sep;
};
globalThis.logger = { error: logs, info: logs, mark: logs, warn: logs, debug: () => {}, green: String, yellow: String, red: String };
globalThis.segment = {
  image: (file) => ({ type: 'image', file }),
  text: (text) => ({ type: 'text', text: String(text) }),
  at: (qq) => ({ type: 'at', qq: String(qq) }),
  button: (...rows) => ({ type: 'buttons', rows }),
};
globalThis.Bot = {
  get uin() { return scope.getStore()?.event?.self_id || ''; },
  logger,
  makeForwardMsg: async (nodes) => ({ type: 'forward', nodes }),
  restart: () => { throw new Error('Full bot restart is not a worker capability'); },
};
globalThis.redis = new Proxy({}, {
  get: (_, method) => async (...args) => {
    if (!redisLoading) redisLoading = (async () => {
      const configFile = path.join(root, 'runtime-settings.json');
      const config = fs.existsSync(configFile) ? JSON.parse(fs.readFileSync(configFile)) : {};
      if (!config.redis_url) throw new Error('Redis is not configured');
      const { createClient } = await import('redis');
      client = createClient({ url: config.redis_url, socket: { reconnectStrategy: false } });
      client.on('error', () => logger.warn('Redis connection error'));
      await client.connect();
      return client;
    })();
    const connection = await redisLoading;
    if (typeof connection[method] !== 'function') throw new Error('Redis operation is unavailable');
    const prefix = `astrbot-miao:${scope.getStore()?.botOwner || 'system'}:`;
    const key = args[0];
    args[0] = Array.isArray(key) ? key.map((value) => prefix + value) : prefix + key;
    const result = await connection[method](...args);
    return method === 'keys' ? result.map((value) => value.slice(prefix.length)) : result;
  },
});

async function load() {
  if (registered) return;
  const module = await import(pathToFileURL(path.join(vendor, 'apps', 'index.js')).href);
  models = await import(pathToFileURL(path.join(vendor, 'models', 'index.js')).href);
  registered = Object.entries(module.apps).map(([id, Type]) => ({ id, instance: new Type() }));
  registered.push(...await loadAddons(models));
  registered.sort((a, b) => a.instance.priority - b.instance.priority);
}

function incoming(packet) {
  const event = {
    ...packet, msg: packet.text, original_msg: packet.text, message: packet.segments || [],
    isGroup: Boolean(packet.group_id), isPrivate: !packet.group_id, isMaster: packet.is_master,
    isAdmin: packet.is_admin,
    member: { is_admin: Boolean(packet.is_admin || packet.is_master) },
    sender: { user_id: packet.user_id, card: packet.sender_name, nickname: packet.sender_name },
    at: packet.mentions?.find((id) => id !== packet.self_id && id !== 'all'),
    source: packet.reply_id ? { message_id: packet.reply_id } : undefined,
    user: { qq: packet.user_id },
  };
  Object.defineProperties(event, {
    isSr: { get: () => event.game === 'sr', set: (value) => { event.game = value ? 'sr' : 'gs'; } },
    isGs: { get: () => event.game === 'gs', set: (value) => { event.game = value ? 'gs' : 'sr'; } },
  });
  event.reply = (message, quote = false) => {
    event._sent = (event._sent || 0) + 1;
    // 上游存在 reply([[img]]) 这类多层嵌套写法（mora 的队伍图），QQ 发送端拿到
    // "装着数组的数组"就发不出去。这里把数组彻底拍平；消息段是对象，对象内部的
    // 字段（比如转发消息的 node 列表）不会被展开，所以不用担心误伤。
    const flat = Array.isArray(message) ? message.flat(Infinity) : message;
    return callPlatform('send', { message: flat, quote });
  };
  event.getReply = () => callPlatform('get_reply', {});
  event.bot = { uin: packet.self_id, getForwardMsg: (id) => callPlatform('get_forward', { id }) };
  event.group = packet.group_id ? {
    getMemberMap: () => callPlatform('group_members', {}).then((list) => new Map(list.map((member) => [Number(member.user_id), member]))),
    pickMember: (id) => ({ getAvatarUrl: () => callPlatform('avatar_url', { user_id: String(id) }) }),
    makeForwardMsg: async (nodes) => ({ type: 'forward', nodes }),
  } : undefined;
  event.friend = { makeForwardMsg: async (nodes) => ({ type: 'forward', nodes }) };
  const api = (uid) => ({ getData: (type, data = {}) => scope.getStore()?.rankUids || event._simulation ? Promise.resolve({ retcode: -100, message: '排名和预设仅使用已缓存的面板。' }) : callPlatform('game_api', { game: event.game, uid, type, data, target_user: event.at }) });
  event.runtime = {
    render: (plugin, relative, data, options) => {
      if (relative === 'character/rank-profile-list') event._rankRows = data.list?.length || 0;
      return renderer.render(event, plugin, relative, data, options);
    },
    get user() { return event.user; },
    getUid: async (game = event.game) => {
      if (scope.getStore()?.rankUids) return scope.getStore().rankUids.has(String(event.uid)) ? String(event.uid) : false;
      const explicit = /(?:^|\D)((?:18|[1-9])\d{8})(?:\D|$)/.exec(event.msg)?.[1];
      const info = await callPlatform('account_info', { game, uid: explicit, target_user: event.at });
      if (info?.uid) { event.uid = String(info.uid); event.user.uid = event.uid; }
      return event.uid || false;
    },
    getMysInfo: async (auth = 'all') => {
      if (scope.getStore()?.rankUids) return auth === 'cookie' || !scope.getStore().rankUids.has(String(event.uid)) ? false : { uid: String(event.uid), own: false, isSelf: false, ckInfo: { uid: '', ck: '' } };
      if (!event.uid) await event.runtime.getUid();
      const info = await callPlatform('account_info', { game: event.game, uid: event.uid, target_user: event.at, auth });
      if (!info?.uid && auth !== 'cookie' && !event._replyNeedUid) {
        event._replyNeedUid = true;
        // A mentioned target has its own bindings; asking the sender to bind would
        // silently point the next query at the wrong account.
        await event.reply(mentionedUser(event)
          ? [segment.at(mentionedUser(event)), ' 还没有绑定游戏UID，请 ta 先发 #绑定 <UID>。']
          : '请先绑定游戏UID，或在指令中指定UID。');
      }
      if (!info?.uid || (auth === 'cookie' && !info.has_credentials)) {
        if (event._fullPanelUpdate && !event._isReplyed) {
          const owner = mentionedUser(event);
          await event.reply(owner
            ? [segment.at(owner), ' 还没有扫码登录，全角色更新要 ta 本人发 #扫码登录。已有展柜缓存仍保留。']
            : '米游社全角色更新需要先扫码登录该UID本人的账号。已有展柜缓存仍保留。');
          event._isReplyed = true;
        }
        return false;
      }
      event.uid = String(info.uid);
      event.user.uid = event.uid;
      return {
        uid: event.uid, isSelf: Boolean(info.own && info.has_credentials),
        ckInfo: { uid: info.has_credentials ? event.uid : '', ck: info.has_credentials ? 'private-api-handle' : '' },
        ckUser: null,
        checkCode: async (result, type) => {
          if (result && result.retcode !== 0) {
            if (event._fullPanelUpdate) event._fullPanelFailed = true;
            if (!event._isReplyed) {
              await event.reply((event._fullPanelUpdate ? '米游社全角色更新未完成，已有缓存仍保留。\n' : '') + (result.message || '米游社查询暂不可用'));
              if (event._fullPanelUpdate) event._isReplyed = true;
            }
            return false;
          }
          if (event._fullPanelUpdate && type === 'characterDetail' && Array.isArray(result?.data?.list)) event._fullPanelFetched = result.data.list.length;
          if (event._fullPanelUpdate && type === 'avatarInfo' && Array.isArray(result?.data?.avatar_list)) event._fullPanelFetched = result.data.avatar_list.length;
          return result;
        },
      };
    },
    getMysApi: async () => api(event.uid || await event.runtime.getUid()),
    createMysApi: (uid) => api(String(uid)),
  };
  return event;
}

// --- 抽卡记录落盘 ---
// The pinned GachaData only reads per-pool JSON files under its data root; it has
// no chat listener of its own. Python owns the imported UIGF records, so they are
// written here just before the gacha handlers run. Files land inside this sender's
// own data root, so one sender can never read another sender's record file.
async function syncGacha(event) {
  const games = new Set([event.game]);
  // Gacha picks the game from the message text, not from the bot prefix, so
  // "#星铁抽卡记录" (no asterisk) resolves to Star Rail while event.game stays gs.
  if (/星铁/.test(event.msg)) games.add('sr');
  for (const game of games) {
    const uid = await event.runtime.getUid(game);
    if (!uid) continue;
    const pools = (await callPlatform('gacha_sync', { game, uid: String(uid) }))?.pools || {};
    const folder = game === 'sr' ? 'srJson' : 'gachaJson';
    for (const [pool, records] of Object.entries(pools)) {
      if (!/^\d{1,4}$/.test(pool) || !Array.isArray(records) || records.length === 0) continue;
      const file = path.join(miaoDataRoot(), 'data', folder, String(event.user.qq), String(uid), `${pool}.json`);
      fs.mkdirSync(path.dirname(file), { recursive: true });
      fs.writeFileSync(file, JSON.stringify(records));
    }
  }
}

// --- 管理功能里能安全做的两条 ---
// Upstream 的更新日志是 `execSync('git log')`，设置页写的是喵喵自己的 JS 配置。
// 这里换成等价但不碰 shell 的做法：日志读随包的 CHANGELOG.md，设置页读 AstrBot
// 侧的真实开关。剩下三条（更新图像/更新/api）仍然不执行，见 dispatch 里的分支。
async function adminUpdatelog(event) {
  let text;
  try {
    text = fs.readFileSync(path.join(vendor, 'CHANGELOG.md'), 'utf8');
  } catch {
    await event.reply('本地没有找到 CHANGELOG.md，无法列出更新日志。');
    return { handled: true, application: 'admin', target: 'updatelog' };
  }
  // 按 `# 2.5.20` 这样的版本标题切开，只取最新的几个。
  const sections = text.split(/^# /m).filter((part) => part.trim());
  let body = sections
    .slice(0, 3)
    .map((part) => '# ' + part.trim())
    .join('\n\n');
  if (!body) body = text.trim().slice(0, 1500);
  if (body.length > 1800) body = body.slice(0, 1800) + '\n…';
  await event.reply(
    `miao-plugin 更新日志（取自随包的 CHANGELOG.md，最新 ${Math.min(sections.length, 3)} 个版本；原版这里是 git log）\n\n${body}`
  );
  return { handled: true, application: 'admin', target: 'updatelog' };
}

async function adminSysCfg(event) {
  // 键名与允许改哪些，全由 Python 侧按 _conf_schema.json 决定，这里只转交原文。
  const rest = event.msg.replace(/^#?喵喵设置\s*/, '').trim();
  if (rest) {
    const result = await callPlatform('config_write', { text: rest });
    if (!result?.ok) {
      await event.reply(result?.reason || '设置没有生效。');
      return { handled: true, application: 'admin', target: 'sysCfg' };
    }
    await event.reply(`已把「${result.key}」设为${result.value ? '开启' : '关闭'}。`);
  }
  const state = await callPlatform('config_read', {});
  await renderer.render(
    event,
    'miao-plugin',
    'admin/index',
    {
      schema: [{ title: '喵喵移植 · AstrBot 侧配置（改值请用「#喵喵设置 项 开/关」或面板）', cfg: state.schema }],
      cfg: state.values,
      imgPlus: false,
      isMiao: false,
    },
    { e: event }
  );
  return { handled: true, application: 'admin', target: 'sysCfg' };
}

function recognizes(packet, bareHelp) {
  const event = incoming(packet);
  if (event.event_type === 'poke') return event.target_id === event.self_id;
  if (!bareHelp && /^(?:#|\/)?(?:帮助|菜单|help|命令|说明|功能|指令|使用说明)$/.test(event.msg)) return false;
  if (addonEnabled('liangshi') && recognizesPreset(event)) return true;
  for (const { id, instance, namespace } of registered) {
    if (id === 'poke') continue;
    if (namespace && !addonEnabled(namespace)) continue;
    if (instance.event === 'message.group' && !event.group_id) continue;
    // Pinned App.accept runs the two synchronous event-local keyword checks.
    if (!id.includes(':') && instance.accept) instance.accept(event);
    if (instance.rule.some((rule) => (new RegExp(rule.reg)).test(event.msg))) return true;
  }
  return false;
}

export async function handle(method, params) {
  // Direct catalog/health calls (test harnesses) have no request scope to hold this.
  if (scope.getStore()) scope.getStore().addonSettings = params.addons || {};
  await load();
  if (method === 'catalog') return {
    applications: registered.filter(app => !app.namespace).map(({ id, instance }) => ({
      id, event: instance.event, rules: instance.rule.map(({ reg, fnc }) => ({ pattern: String(reg), target: fnc })),
    })),
    addons: registered.filter(app => app.namespace).map(({ id, namespace, instance }) => ({
      id, namespace, event: instance.event,
      rules: instance.rule.map(({ reg, fnc, permission }) => ({ pattern: String(reg), target: fnc, permission })),
    })),
  };
  if (method === 'addon_help') {
    const event = incoming(params.event);
    scope.getStore().event = event;
    return renderer.render(event, 'miao-plugin', 'astrbot/addon-help', { groups: params.groups }, { retType: 'base64' });
  }
  if (method === 'character') {
    const character = models.Character.get(params.name, params.game || 'gs');
    if (!character) return null;
    let wireId = Number(character.id);
    const base = Number('1' + String(wireId).slice(1));
    if (character.game === 'sr' && String(wireId).startsWith('2') && models.Character.enhancedCharIds.includes(base)) wireId = base;
    const odd = wireId % 2 ? wireId : wireId - 1;
    const candidates = character.game === 'sr' && character.isTrailblazer ? [odd, odd + 1] : [wireId];
    return { id: character.id, name: character.name, game: character.game, element: character.elem,
      wire_id: wireId, wire_candidates: candidates, enhanced: Number(character.id) !== wireId,
      detail: character.getDetail(), images: character.imgs };
  }
  if (method === 'recognize') return { matched: recognizes(params.event, params.bare_help) };
  if (method === 'card') {
    if (!['growth-sr'].includes(params.template)) throw new Error('Unknown native card');
    const event = incoming(params.event);
    scope.getStore().event = event;
    return renderer.render(event, 'miao-plugin', 'astrbot/' + params.template, params.data, { retType: 'base64' });
  }
  if (method === 'dispatch') {
    if (!recognizes(params.event, params.bare_help)) return { handled: false };
    const previous = commandTail;
    let release;
    commandTail = new Promise((resolve) => { release = resolve; });
    await previous;
    try {
      if (scope.getStore().signal.aborted) throw new Error('Request was cancelled');
      const event = incoming(params.event);
      scope.getStore().event = event;
      scope.getStore().botOwner = digest([event.platform, event.self_id]);
      scope.getStore().dataOwner = ownerOf(event, event.user_id);
      if (event.event_type === 'poke' && event.target_id !== event.self_id) return { handled: false };
      if (!params.bare_help && /^(?:#|\/)?(?:帮助|菜单|help|命令|说明|功能|指令|使用说明)$/.test(event.msg)) return { handled: false };
      // 梁氏那套「理论预设面板」用固定 uid 100000000-100000005（顺序：极限/核爆/
      // 辅助/平民/毕业/试用）。这里只把写法翻成原生 "#角色面板 <uid>"，渲染仍走
      // 喵喵自己的面板路径；预设数据按游戏放在 <runtime>/presets/<game>/<uid>.json。
      const PRESET_KEYWORDS = ['极限', '核爆', '辅助', '平民', '毕业', '试用'];
      const presetPanel = /^#(?:原神|星铁)?([^#\s]+?)(极限|核爆|辅助|平民|毕业|试用)(?:面板|面版)?$/.exec(event.msg)
        || /^#(?:原神|星铁)?(极限|核爆|辅助|平民|毕业|试用)([^#\s]+)$/.exec(event.msg);
      if (presetPanel && !/(排名|排行|榜)/.test(event.msg)) {
        const keywordFirst = PRESET_KEYWORDS.includes(presetPanel[1]);
        const keyword = keywordFirst ? presetPanel[1] : presetPanel[2];
        const name = (keywordFirst ? presetPanel[2] : presetPanel[1]).replace(/(面板|面版)$/, '');
        const uid = String(100000000 + PRESET_KEYWORDS.indexOf(keyword));
        const preset = path.join(root, 'presets', event.game, uid + '.json');
        const character = models.Character.get(name, event.game);
        let presetData = null;
        try {
          presetData = JSON.parse(fs.readFileSync(preset, 'utf8'));
        } catch {
          presetData = null;
        }
        if (!presetData) {
          await event.reply(`「${keyword}」这套预设面板还没装数据，暂时看不了。`);
          return { handled: true, application: 'profile', target: 'presetPanel' };
        }
        if (!character || !presetData.avatars || !presetData.avatars[character.id]) {
          await event.reply(`「${keyword}」预设里没有「${name}」的配装。`);
          return { handled: true, application: 'profile', target: 'presetPanel' };
        }
        const owner = mentionedUser(event) || event.user_id;
        const folder = path.join(root, 'instances', ownerOf(event, owner), 'data/PlayerData', event.game);
        fs.mkdirSync(folder, { recursive: true });
        const cached = path.join(folder, uid + '.json');
        // 缓存里可能躺着一条被 miao 的刷新流程写成空 avatars 的同名记录（100000000
        // 这串 UID 是我们自己发出去的，不会撞上用户的真实数据）。只看文件在不在
        // 会把这种空壳当成“已经有了”，面板就永远回「暂无数据」。所以判断的是这条
        // 角色的面板在不在。
        let cachedReady = false;
        try {
          cachedReady = Boolean(JSON.parse(fs.readFileSync(cached, 'utf8')).avatars?.[character.id]);
        } catch {
          cachedReady = false;
        }
        if (!cachedReady) fs.copyFileSync(preset, cached);
        event.msg = '#' + name + '面板 ' + uid;
        event.original_msg = event.msg;
        event._simulation = true;
      }
      // 梁氏的预设关键词自带固定编号（极限→100000000）；用户若又手写了编号，
      // 它的 accept 会把编号再拼一次，得到 18 位 "…#玛薇卡面板100000000100000000"，
      // 原生 profile 的 (\d{9,10}) 解析不了，整条指令静默丢掉。先去掉多余的那个。
      if (addonEnabled('liangshi') && /极限|核爆|辅助|平民|毕业|试用/.test(event.msg)) {
        event.msg = event.msg.replace(/(10000000[0-5])$/, '').trim();
      }
      for (const { id, instance, namespace } of registered) {
        if ((id === 'poke') !== (event.event_type === 'poke')) continue;
        if (namespace && !addonEnabled(namespace)) continue;
        if (instance.event === 'message.group' && !event.group_id) continue;
        instance.e = event;
        if (instance.accept) await instance.accept(event);
        if (id === 'liangshi:预设替换' && await preparePreset(event)) return { handled: true, application: id, target: 'preset' };
        for (const rule of instance.rule) {
          if (!(new RegExp(rule.reg)).test(event.msg)) continue;
          if (rule.permission === 'master' && !event.isMaster) {
            await event.reply('此操作需要主人权限。');
            return { handled: true, application: id, target: rule.fnc };
          }
          if (['owner', 'admin'].includes(rule.permission) && !event.isMaster && !event.isAdmin) {
            await event.reply('此操作需要群管理员或主人权限。');
            return { handled: true, application: id, target: rule.fnc };
          }
          // "@群友 #火神面板" 与 "@群友 #更新面板" 指的是群友自己的面板数据，
          // 所以这一段要把缓存与文件命名空间切到被 @ 的人；他人在 Python 侧
          // 只暴露绑定 UID 与公开面板，绝不交出凭据或私有战绩。
          // 其它指令（抽卡、签到、私有 API）仍按发送者隔离，@ 不能当越权用。
          const panelTarget = mentionedUser(event);
          if (id === 'profile' && panelTarget && !GROUP_RANK_RULES.has(rule.fnc)) {
            scope.getStore().dataOwner = ownerOf(event, panelTarget);
            // 面板详情与更新都得先知道查的是谁的 UID。被 @ 的人没绑定就直说，
            // 不要沉默，也不要悄悄改查发送者自己的账号。
            if (UID_RULES.has(rule.fnc) && !await event.runtime.getUid()) {
              await event.reply([segment.at(panelTarget), ' 还没有绑定游戏UID，请 ta 先发 #绑定 <UID>。']);
              return { handled: true, application: id, target: rule.fnc };
            }
          }
          // Imported records are only materialized for a gacha command, not every message.
          if (id === 'gacha') await syncGacha(event);
          if (id === 'admin') {
            if (!event.isMaster) {
              await event.reply('此操作需要主人权限');
              return { handled: true, application: id, target: rule.fnc };
            }
            if (rule.fnc === 'updatelog') return adminUpdatelog(event);
            if (rule.fnc === 'sysCfg') return adminSysCfg(event);
            // update / updateRes / miaoApiInfo still run shell, wipe local changes,
            // restart the bot, or post a token to a third-party host.
            await event.reply('喵喵管理功能迁移中，当前未执行配置修改或更新。');
            return { handled: true, application: id, target: rule.fnc };
          }
          if (id === 'profile' && ['profileRefresh', 'profileRefreshMys'].includes(rule.fnc)) {
            // Both spellings share one update path. Private account data is used for
            // whichever account owns the target UID — the sender's own, or a
            // mentioned member's stored CK. Only a UID with no CK falls back to the
            // public showcase (which costs the mentioned member nothing).
            const uid = await event.runtime.getUid();
            const info = uid ? await callPlatform('account_info', { game: event.game, uid, target_user: event.at }) : null;
            if (info?.has_credentials) {
              event._fullPanelUpdate = true;
              await instance.profileRefreshMys(event);
              if (event._fullPanelFetched && !event._fullPanelFailed) {
                const updated = models.Player.create(event)._update?.length || 0;
                if (updated < event._fullPanelFetched) await event.reply('未写入的角色需核对本地角色资源；本次不视为全量更新成功。');
                return { handled: true, application: id, target: 'profileRefresh' };
              }
              // A failed full-account request must not disable the old showcase
              // path or let its result be reported as a full-account success.
              event._fullPanelUpdate = false;
              event._isReplyed = false;
              await event.reply('全角色更新未完成，继续尝试游戏展柜更新。');
            } else if (uid) {
              await event.reply(panelTarget
                ? [segment.at(panelTarget), ' 还没有扫码登录，这次只能用公开展柜更新；全角色更新要 ta 自己发 #扫码登录。']
                : '你还没有扫码登录，这次只能用公开展柜更新；全角色更新请先发 #扫码登录。');
            }
            await instance.profileRefresh(event);
            return { handled: true, application: id, target: 'profileRefresh' };
          }
          if (id === 'profile' && event.group_id && ['rankList', 'groupProfile', 'refreshRank'].includes(rule.fnc) && !/极限/.test(event.msg)) {
            if (rule.fnc === 'refreshRank' && !event.isMaster && !event.isAdmin) {
              await event.reply('只有主人及群管理员可刷新排名...');
              return { handled: true, application: id, target: rule.fnc };
            }
            await prepareRanking(event, models, root);
            if (rule.fnc === 'refreshRank') {
              await event.reply(event._groupRankSummary ? `本群排名已刷新，共刷新${event._groupRankSummary.eligible}个UID数据...` : '群排名总开关尚未开启。');
              return { handled: true, application: id, target: rule.fnc, rank_summary: event._groupRankSummary };
            }
          }
          const result = await instance[rule.fnc](event);
          // Upstream returns false to stop its own rule chain, sometimes after replying.
          // A message it already answered is consumed, otherwise AstrBot answers it again.
          if (result !== false) return { handled: true, application: id, target: rule.fnc, rank_summary: event._groupRankSummary && { ...event._groupRankSummary, displayed: event._rankRows } };
          if (event._sent) return { handled: true, replied: true, application: id, target: rule.fnc, rank_summary: event._groupRankSummary && { ...event._groupRankSummary, displayed: event._rankRows } };
          // Nothing was answered and the loop continues: the next app must not inherit
          // the mentioned account's cache namespace.
          scope.getStore().dataOwner = ownerOf(event, event.user_id);
        }
      }
      return { handled: false };
    } finally { release(); }
  }
  const error = new Error('Action is not connected');
  error.publicMessage = 'This Miao action is not connected yet';
  throw error;
}

export async function close() {
  await renderer.close();
  if (client?.isOpen) await client.quit();
}
