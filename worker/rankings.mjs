/** Build a current-group panel view; reuse Miao's score/damage/rank models. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { pathToFileURL } from 'node:url';
import { callPlatform, scope } from './main.mjs';

export async function prepareRanking(event, models, root) {
  const context = scope.getStore();
  const game = event.game;
  const { default: Cfg } = await import(pathToFileURL(path.join(root, 'plugins/miao-plugin/components/Cfg.js')).href);
  if (!Cfg.get('groupRank')) return;
  const name = event.msg.replace(/(#|星铁|原神|最强|最高分|第一|词条|双爆|双暴|极限|最高|最多|最牛|圣遗物|遗器|评分|群内|群|排名|排行|面板|面版|详情|榜)/g, '').trim();
  const character = models.Character.get(name, game);
  const refresh = /^(?:#)(?:星铁|原神)?(?:刷新|更新|重新加载)(?:群内|群|全部)*(?:排名|排行)$/.test(event.msg);
  if (!refresh && name && !character) return;
  if (!/^[1-9]\d*$/.test(event.group_id)) throw new Error('Invalid ranking group');

  const members = new Map();
  let offset = 0, bindingCount = 0;
  do {
    const page = await callPlatform('group_rank_profiles', { game, offset });
    bindingCount = page.bindings;
    for (const entry of page.players) {
      if (!/^\d{9,10}$/.test(entry.uid)) throw new Error('Invalid ranking UID');
      let previous = members.get(entry.uid);
      if (!previous) {
        previous = { ...entry, player: { ...entry.player, avatars: {} }, avatarStamps: new Map() };
        members.set(entry.uid, previous);
      }
      previous.has_credentials ||= entry.has_credentials;
      // The same UID can be bound by multiple members. Merge the newest valid
      // snapshot of each character, rather than dropping an older whole file.
      for (const raw of Object.values(entry.player.avatars)) {
        try {
          const avatar = new models.Avatar(raw, game);
          if (!avatar.char || avatar.game !== game || !avatar.isProfile || !avatar.hasData) continue;
          const parsed = typeof raw._time === 'number' ? raw._time : Date.parse(raw._time);
          const stamp = Number.isFinite(parsed) ? parsed : entry.stamp * 1000;
          if (!previous.avatarStamps.has(String(avatar.id)) || stamp > previous.avatarStamps.get(String(avatar.id))) {
            previous.player.avatars[avatar.id] = avatar.toJSON();
            previous.avatarStamps.set(String(avatar.id), stamp);
          }
        } catch {
          logger.warn('Invalid cached ranking avatar skipped');
        }
      }
    }
    if (page.next_offset !== null && page.next_offset <= offset) throw new Error('Invalid ranking continuation');
    offset = page.next_offset;
  } while (offset !== null);
  for (const [uid, entry] of members) {
    if (!Object.keys(entry.player.avatars).length) members.delete(uid);
  }

  // Only this request reads the group view. Ordinary Player/User caches retain
  // their original sender namespace and are never populated with other owners.
  context.rankDataRoot = path.join(root, 'rankings', context.botOwner, event.group_id, game);
  context.dataOwner = 'rank:' + createHash('sha256').update(JSON.stringify([context.botOwner, event.group_id, game, event.token])).digest('hex');
  context.rankUids = new Set(members.keys());
  const folder = path.join(context.rankDataRoot, 'data', 'PlayerData', game);
  fs.mkdirSync(folder, { recursive: true });
  for (const file of fs.readdirSync(folder)) {
    if (/^\d{9,10}\.json$/.test(file) && !members.has(file.slice(0, -5))) fs.unlinkSync(path.join(folder, file));
  }
  for (const [uid, entry] of members) fs.writeFileSync(path.join(folder, uid + '.json'), JSON.stringify(entry.player));

  const charId = character?.isTrailblazer && character.id % 2 === 0 ? character.id - 1 : character?.id;
  const keys = await redis.keys(`miao:rank:${event.group_id}:*`);
  for (const key of keys) {
    const parsed = await models.ProfileRank.getCharacterFromKey(key, game);
    if (parsed && (!charId || Number(parsed[1]) === Number(charId))) await redis.del(key);
  }

  let eligible = 0;
  for (const [uid, entry] of members) {
    const player = models.Player.create(uid, game);
    const profiles = player.getProfiles();
    // Derive eligibility from this game's actual group bindings; the legacy
    // UID-info key alone cannot authorize a different user's private API.
    await redis.set(`miao:rank:uid-info:${uid}`, JSON.stringify({
      qq: entry.qq, uidType: entry.has_credentials ? 'ck' : 'bind',
      totalCount: Object.keys(profiles).length,
      basicCount: Object.values(profiles).filter(p => ['安柏', '凯亚', '丽莎'].includes(p.name)).length,
    }), { EX: 365 * 86400 });
    const rank = await models.ProfileRank.create({ groupId: event.group_id, uid, qq: entry.qq });
    if (!rank.allowRank) continue;
    let included = false;
    for (const profile of Object.values(profiles)) {
      const profileId = profile.char.isTrailblazer && profile.id % 2 === 0 ? profile.id - 1 : profile.id;
      if (charId && Number(profileId) !== Number(charId)) continue;
      if (!profile.hasData) continue;
      await rank.getRank(profile, true);
      included = true;
    }
    if (included) eligible++;
  }
  event._groupRankSummary = { group: event.group_id, game, bindings: bindingCount, cached: members.size, eligible };
}
