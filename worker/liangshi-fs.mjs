/** Narrow filesystem boundary for the pinned Liangshi extension, not Miao. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { scope } from './main.mjs';

const root = process.cwd();
const teamPattern = /^plugins\/liangshi-calc\/damage\/liangshi-(gs|sr)\/(.+)\/TeamData\.json$/;
const profilePattern = /^data\/PlayerData\/(gs|sr)\/(\d{9,10})\.json$/;

function locate(input, writing = false) {
  if (typeof input === 'number') throw new Error('File descriptors are not extension paths');
  const absolute = path.resolve(input instanceof URL ? fileURLToPath(input) : String(input));
  const relative = path.relative(root, absolute).split(path.sep).join('/');
  if (relative.startsWith('../') || relative === '..' || path.isAbsolute(relative)) throw new Error('Extension path escapes runtime');
  const context = scope.getStore();
  const profile = profilePattern.exec(relative);
  const team = teamPattern.exec(relative);
  if (relative.startsWith('data/') && !context?.dataOwner) throw new Error('Extension data requires an original sender');
  if (profile) {
    let target = path.join(miaoDataRoot(), relative);
    if (!writing && !fs.existsSync(target) && /^10000000[0-5]$/.test(profile[2])) {
      target = path.join(root, 'plugins/liangshi-calc/replace/data/1/PlayerData', profile[1], profile[2] + '.json');
    }
    return target;
  }
  if (team) {
    if (!context?.dataOwner || context.rankUids) return null;
    const session = createHash('sha256').update(String(context.event?.group_id || 'private')).digest('hex');
    return path.join(miaoDataRoot(), 'data', 'liangshi-team', team[1], session, team[2], 'TeamData.json');
  }
  if (relative.startsWith('data/')) return path.join(miaoDataRoot(), relative);
  if (writing && !(relative.startsWith('plugins/liangshi-calc/config/') && context?.event?.isMaster)) throw new Error('Extension cannot overwrite shared resources');
  return absolute;
}

const reads = new Set(['readFileSync', 'existsSync', 'readdirSync', 'statSync', 'lstatSync', 'createReadStream']);
const writes = new Set(['writeFileSync', 'appendFileSync', 'mkdirSync', 'unlinkSync', 'rmSync', 'createWriteStream']);
const scoped = new Proxy(fs, {
  get(target, method) {
    if (reads.has(method) || writes.has(method)) return (file, ...args) => {
      const writing = writes.has(method);
      const actual = locate(file, writing);
      if (!actual) return method === 'readFileSync' ? '{}' : method === 'existsSync' ? false : undefined;
      if (writing && method !== 'mkdirSync') fs.mkdirSync(path.dirname(actual), { recursive: true });
      return target[method](actual, ...args);
    };
    if (method === 'copyFileSync' || method === 'renameSync') return (from, to, ...args) => {
      const destination = locate(to, true);
      if (!destination) return;
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      return target[method](locate(from, method === 'renameSync'), destination, ...args);
    };
    if (method === 'promises') throw new Error('Async extension filesystem must use the owned adapter');
    if (typeof target[method] === 'function') throw new Error('Unreviewed extension filesystem operation: ' + String(method));
    return target[method];
  },
});

/** Avoid module-import-time snapshots of a different sender's team records. */
export function teamData(file) {
  return new Proxy({}, { get: (_, uid) => {
    try { return JSON.parse(scoped.readFileSync(file, 'utf8'))[uid]; }
    catch { return undefined; }
  } });
}

export default scoped;
