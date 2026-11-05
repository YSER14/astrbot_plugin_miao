/** Reuse Liangshi calculations while preserving Miao scoring and rank scenarios. */
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import YAML from 'yaml';
import { scope } from './main.mjs';
import { teamData } from './liangshi-fs.mjs';

const root = process.cwd();
const vendor = path.join(root, 'plugins/liangshi-calc');
const defaults = { calcLiang: true, calcLiangQ: false, calcLiangT: false, calcLiangK: false,
  GsDisabled: false, SrDisabled: false, artisLiang: false, artisLiangZ: false,
  DisabledRankingProtection: false, rankingOnemodel: 'm', rankingTwomodel: 'hps',
  rankingThreemodel: 'dps', namemodel: 1, panelmodel: 1, energymodel: 0 };
const failed = new Set();
let installed = false;

export const LSconfig = {
  getConfig(type, name) {
    if (!/^[\w-]+$/.test(name)) throw new Error('Invalid Liangshi config');
    const file = path.join(vendor, 'config', type === 'def' ? 'system' : '', name + '.yaml');
    const data = fs.existsSync(file) ? YAML.parse(fs.readFileSync(file, 'utf8')) || {} : {};
    if (name !== 'config') return data;
    return new Proxy({ ...defaults, ...data }, { get: (object, key) => {
      if (['GsDisabled', 'SrDisabled', 'artisLiang', 'artisLiangZ'].includes(key)) return false;
      const mode = scope.getStore()?.addonSettings?.calc_mode || 'extended';
      if (key === 'calcLiangT') return mode === 'team';
      if (key === 'calcLiangQ') return mode === 'complete';
      if (key === 'calcLiangK') return mode === 'custom';
      return object[key];
    } });
  },
  writeData(type, name, data) {
    if (!scope.getStore()?.event?.isMaster || type !== 'user' || !/^[\w-]+$/.test(name)) throw new Error('Owner permission is required');
    fs.writeFileSync(path.join(vendor, 'config', name + '.yaml'), YAML.stringify(data));
  },
};
// Liangshi's calc scripts import Format through components/index.js, which points
// here. Mirror liangshi's own components/Format.js exactly — calc_basic.js calls
// percent()/number()/int() too, and a missing helper kills the whole command.
export const Format = {
  int: (value) => parseInt(value),
  comma: (value, fix = 0) => {
    const num = parseFloat((value * 1).toFixed(fix));
    const [integer, decimal] = String(num).split('.');
    const grouped = integer.replace(/\d(?=(\d{3})+$)/g, '$&,');
    return `${grouped}${fix > 0 ? '.' + (decimal || '0'.repeat(fix)) : ''}`;
  },
  pct: (value, fix = 1) => (value * 1).toFixed(fix) + '%',
  percent: (value, fix = 1) => Format.pct(value * 100, fix),
  number: (value, fix = 3) => (value * 1).toFixed(fix),
};

export function installLiangshi(models) {
  if (installed) return;
  installed = true;
  globalThis.miaoTeamData = teamData;
  const originalPath = models.ProfileDmg.dmgRulePath;
  const originalRule = models.ProfileDmg.prototype.getCalcRule;
  models.ProfileDmg.dmgRulePath = (name, game = 'gs') => {
    const context = scope.getStore();
    const base = originalPath(name, game);
    // `base` is `false` when Miao has no rule for this character, so the
    // optional chain has to cover `path` too — `false?.path.includes()` throws.
    if (!context?.addonSettings?.liangshi_enabled || context.rankUids || context.liangshiFallback || base?.path?.includes('calc_user')) return base;
    const resolved = name.replace(/^(星|穹)·/, '开拓者');
    if (resolved.includes('..') || path.isAbsolute(resolved)) return base;
    const mode = context.addonSettings.calc_mode || 'extended';
    const files = mode === 'complete' ? ['calc_complete', 'calc_basic'] : mode === 'team' ? ['calc_team', 'calc_basic'] : mode === 'custom' ? ['calc_develop', 'calc_basic'] : ['calc_basic'];
    for (const filename of files) {
      const file = path.join(vendor, 'damage', 'liangshi-' + game, resolved, filename + '.js');
      if (!failed.has(file) && fs.existsSync(file)) return { path: file, createdBy: mode === 'team' ? '梁氏组队' : '梁氏扩展' };
    }
    return base;
  };
  models.ProfileDmg.prototype.getCalcRule = async function () {
    try { return await originalRule.call(this); }
    catch (error) {
      const name = [10000005, 10000007, 20000000].includes(Number(this.char.id)) ? '旅行者/' + this.profile.elem : this.char.name;
      const candidate = models.ProfileDmg.dmgRulePath(name, this.char.game);
      if (!candidate?.path?.startsWith(vendor + path.sep)) throw error;
      failed.add(candidate.path);
      logger.warn('Liangshi rule unavailable; using original Miao: ' + this.char.name);
      const context = scope.getStore();
      context.liangshiFallback = true;
      try { return await originalRule.call(this); }
      finally { context.liangshiFallback = false; }
    }
  };
}

export async function liangshiApps() {
  const apps = [];
  for (const file of ['sharePanel.js', 'CalcEntry.js', '预设替换.js']) {
    const module = await import(pathToFileURL(path.join(vendor, 'apps', file)).href);
    const Type = Object.values(module).find(value => typeof value === 'function');
    apps.push({ id: 'liangshi:' + file.replace('.js', ''), namespace: 'liangshi', instance: new Type() });
  }
  return apps;
}
