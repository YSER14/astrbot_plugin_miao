/** Mora needs public character aliases and pool metadata, never login state. */
import fs from 'node:fs';
import path from 'node:path';
import { Character, Weapon } from '#miao.models';

function zzzRole(name) {
  const directory = path.resolve('plugins/mora-plugin/resources/mora-plugin-res/ZenlessZoneZero/Roles/Guides');
  const simplify = text => text.replace(/[·\s「」【】()（）\-_]/g, '');
  let normalized = name;
  if (fs.existsSync(directory)) {
    for (const author of fs.readdirSync(directory)) {
      const folder = path.join(directory, author);
      if (!fs.statSync(folder).isDirectory()) continue;
      const matched = fs.readdirSync(folder).find(file => simplify(path.parse(file).name) === simplify(name));
      if (matched) { normalized = path.parse(matched).name; break; }
    }
  }
  return { name: normalized, alias: normalized, roleId: 0 };
}

export default {
  getRole(name, _unused = '', isSr = false, game = isSr ? 'sr' : 'gs') {
    if (game === 'zzz') return zzzRole(name);
    const character = Character.get(name, game);
    if (!character) return false;
    const traveler = /([风岩雷草水火冰])(?:主|旅行者|荧|空)/.exec(String(name));
    return { name: character.name, roleId: character.id, alias: traveler ? traveler[1] + '主' : character.sName || character.name,
      star: character.star, elem: character.elem };
  },
  getdefSet(type, game) {
    // Mora merges this with the same original Miao poolDetail arrays. Returning
    // an empty supplementary table avoids inventing unsupported ZZZ history.
    if (type === 'pool') return [];
    if (type === 'weapon') {
      const aliases = {};
      for (const name of ['无锋剑', '猎弓']) {
        const weapon = Weapon.get(name, String(game).startsWith('sr') ? 'sr' : 'gs');
        if (weapon) aliases[weapon.name] = weapon.name;
      }
      return { sortName: aliases };
    }
    return {};
  },
};
