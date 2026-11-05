/** Register reviewed upstream apps inside one Miao worker, without bootstraps. */
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { scope } from './main.mjs';

let installed;

export async function loadAddons(models) {
  if (!fs.existsSync('addon-receipt.json')) return [];
  if (installed) return installed;
  const { default: Plugin } = await import(pathToFileURL(path.resolve('plugins/miao-plugin/components/common/Plugin.js')).href);
  globalThis.plugin = Plugin;
  const { installLiangshi, liangshiApps } = await import('./liangshi.mjs');
  installLiangshi(models);
  const apps = await liangshiApps();
  // 已下线的两个：breadShop（面包小游戏，要按群存档且有空缓存报错）、
  // AkashaAbyss（虚空深渊使用率，源站 akashadata 的数据停在 4.6/2024-06，
  // 8 个 4.6 之后的角色一个都没有，给用户看等于发过期榜单）。要把哪个加回来，
  // 把它放回下面的列表即可。
  for (const name of ['roleGuides', 'bannerSchedule', 'abyssVersion', 'teamGuides']) {
    const module = await import(pathToFileURL(path.resolve('plugins/mora-plugin/apps', name + '.js')).href);
    const Type = Object.values(module).find(value => typeof value === 'function');
    const instance = new Type();
    // Existing Miao handlers win overlapping commands; Mora supplies extensions.
    instance.priority = Math.max(300, instance.priority);
    if (instance.init) await instance.init();
    apps.push({ id: 'mora:' + name, namespace: 'mora', instance });
  }
  installed = apps;
  return apps;
}

export function addonEnabled(namespace) {
  const settings = scope.getStore()?.addonSettings;
  return namespace === 'mora' ? Boolean(settings?.mora_enabled) : Boolean(settings?.liangshi_enabled);
}

export function recognizesPreset(event) {
  return /^#(?:星铁)?\S*(?:极限|核爆|辅助|平民|毕业|试用)\S*$/.test(event.msg);
}

/** Populate only the selected simulation, without overwriting personal edits. */
export async function preparePreset(event) {
  const match = /(?:^|\D)(10000000[0-5])(?:\D|$)/.exec(event.msg);
  if (!match) return false;
  const uid = match[1];
  if (!['gs', 'sr'].includes(event.game)) return false;
  const folder = path.join(miaoDataRoot(), 'data/PlayerData', event.game);
  const target = path.join(folder, uid + '.json');
  if (!fs.existsSync(target)) {
    const source = path.resolve('plugins/liangshi-calc/replace/data/1/PlayerData', event.game, uid + '.json');
    if (!fs.existsSync(source)) {
      await event.reply('这个预设目前没有该游戏的数据，请选择其他预设。');
      return true;
    }
    fs.mkdirSync(folder, { recursive: true });
    fs.copyFileSync(source, target);
  }
  const player = JSON.parse(fs.readFileSync(target, 'utf8'));
  if (!Object.keys(player.avatars || {}).length) {
    await event.reply('这个预设的原始资料目前为空，请选择其他预设。');
    return true;
  }
  event.uid = uid;
  event._simulation = true;
  event.original_msg = event.msg;
  return false;
}
