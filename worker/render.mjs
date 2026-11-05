/** Render original art-template pages with a single owned browser page at a time. */
import fs from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { randomUUID } from 'node:crypto';
import template from 'art-template';
import puppeteer from 'puppeteer-core';

let browser;
let tail = Promise.resolve();
let waiting = 0;
let branding;
const allowedPlugins = new Set(['miao-plugin', 'mora-plugin', 'liangshi-calc']);

function launch(settings, profile) {
  return puppeteer.launch({
    executablePath: settings.browser_executable,
    headless: true,
    userDataDir: profile,
    args: ['--disable-dev-shm-usage', ...(settings.browser_no_sandbox ? ['--no-sandbox'] : [])],
    handleSIGINT: false, handleSIGTERM: false, handleSIGHUP: false,
  });
}

async function openPage() {
  const root = process.cwd();
  const settings = JSON.parse(await fs.readFile(path.join(root, 'runtime-settings.json'), 'utf8'));
  if (!settings.browser_executable) throw new Error('Browser is not configured');
  if (!browser?.connected) {
    try {
      browser = await launch(settings, path.join(root, 'temp', 'browser-profile'));
    } catch (error) {
      // 重载插件时上一个 worker 的 Chromium 会活下来继续占着共享 profile，之后
      // 每次启动都报 "The browser is already running"，所有面板一起变成不出图。
      // 这种情况下退回本进程私有的 profile，渲染自己恢复，不用人工去杀进程。
      if (!/already running|SingletonLock|ProcessSingleton/i.test(String(error?.message || ''))) throw error;
      const fallback = path.join(root, 'temp', 'browser-profile-' + process.pid);
      await fs.rm(fallback, { recursive: true, force: true });
      browser = await launch(settings, fallback);
      console.error('Render browser fell back to a private profile:', path.basename(fallback));
    }
  }
  return browser.newPage();
}

/** Serialize OCR and screenshots through the same owned browser and page limit. */
export async function withPage(run, signal) {
  if (++waiting > 8) { waiting--; throw new Error('Render capacity exceeded'); }
  const previous = tail;
  let release, page;
  tail = new Promise(resolve => { release = resolve; });
  await previous;
  const cancel = () => { if (page) void page.close().catch(() => {}); };
  try {
    if (signal?.aborted) throw new Error('Request was cancelled');
    page = await openPage();
    signal?.addEventListener('abort', cancel, { once: true });
    if (signal?.aborted) throw new Error('Request was cancelled');
    return await run(page);
  } finally {
    signal?.removeEventListener('abort', cancel);
    if (page && !page.isClosed()) await page.close();
    waiting--;
    release();
  }
}

// The default layout prints {{copyright}}; only upstream's Common.render sets it.
// Pages rendered through e.runtime.render would otherwise end with "undefined".
async function copyright() {
  if (branding !== undefined) return branding;
  const root = process.cwd();
  const runtime = JSON.parse(await fs.readFile(path.join(root, 'package.json'), 'utf8'));
  let version = '';
  try {
    const log = await fs.readFile(path.join(root, 'plugins', 'miao-plugin', 'CHANGELOG.md'), 'utf8');
    version = (/^# (\S+)/m.exec(log) || [])[1] || '';
  } catch {
    version = '';
  }
  branding = `Created By AstrBot<span class="version">${runtime.astrbotVersion || runtime.version}</span>`
    + (version ? `&amp; Miao-Plugin ${version}` : '');
  return branding;
}

export async function render(event, plugin, relative, parameters = {}, options = {}) {
  if (!allowedPlugins.has(plugin) || /(^|[\\/])\.\.([\\/]|$)/.test(relative) || path.isAbsolute(relative)) throw new Error('Unknown template scope');
  if (++waiting > 8) { waiting--; throw new Error('Render capacity exceeded'); }
  const previous = tail;
  let release;
  tail = new Promise((resolve) => { release = resolve; });
  await previous;
  let page;
  try {
    const root = process.cwd();
    const resources = path.join(root, 'plugins', plugin, 'resources');
    const filename = path.join(resources, relative.replace(/\.html$/, '') + '.html');
    const base = pathToFileURL(resources + path.sep).href;
    const mark = await copyright();
    let data = {
      copyright: mark, sys: { scale: 1 }, ...parameters, pluResPath: base, _res_path: base, _miao_path: base,
      _tpl_path: path.join(resources, 'common', 'tpl') + '/',
      defaultLayout: path.join(resources, 'common', 'layout', 'default.html'),
      elemLayout: path.join(resources, 'common', 'layout', 'elem.html'),
      tplFile: filename,
    };
    if (options.beforeRender) data = options.beforeRender({ data }) || data;
    const html = template(filename, data);
    const identity = randomUUID();
    const output = path.join(root, 'output', identity);
    await fs.mkdir(path.dirname(output), { recursive: true });
    await fs.writeFile(output + '.html', html);
    page = await openPage();
    await page.setViewport({ width: 1200, height: 900, deviceScaleFactor: 1.5 });
    await page.goto(pathToFileURL(output + '.html').href, { waitUntil: 'load', timeout: 20000 });
    await page.evaluate(async () => { await document.fonts.ready; });
    const size = await page.evaluate(() => {
      const element = document.querySelector('#container') || document.body;
      const box = element.getBoundingClientRect();
      return { width: Math.ceil(Math.max(box.width, element.scrollWidth)), height: Math.ceil(Math.max(box.height, element.scrollHeight)) };
    });
    if (!size.width || !size.height || size.width > 4000 || size.height > 30000) throw new Error('Invalid template dimensions');
    await page.setViewport({ width: size.width, height: Math.min(size.height, 2000), deviceScaleFactor: 1.5 });
    // 老模板（mora 的使用率/队伍）一屏能到 4800×2700，PNG 有 12MB，QQ 上传多半失败。
    // 上游 Yunzai 本来就用 jpeg，这里跟它对齐；miao 自己的卡片仍走 PNG。
    const jpeg = Boolean(options.jpeg);
    const file = output + (jpeg ? '.jpg' : '.png');
    await page.screenshot({ path: file, fullPage: true, ...(jpeg ? { type: 'jpeg', quality: 90 } : {}) });
    const image = { type: 'image', file: pathToFileURL(file).href };
    if (options.retType === 'base64') return image;
    const sent = await event.reply(image);
    return options.retType === 'msgId' ? sent : true;
  } finally {
    if (page) await page.close();
    waiting--;
    release();
  }
}

export async function close() {
  if (browser) await browser.close();
  browser = undefined;
}
