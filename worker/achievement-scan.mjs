/** Reuse Cocogoat's public scanner protocol; keep uploads inside this actor. */
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { scope } from './main.mjs';
import { withPage } from './render.mjs';

export async function scanAchievements(params) {
  const event = params.event;
  if (!event?.actor || !Array.isArray(params.files) || params.files.length < 1 || params.files.length > 12) throw new Error('Invalid scan request');
  if (params.video && params.files.length !== 1) throw new Error('Only one recording is accepted');
  const owner = createHash('sha256').update(event.actor).digest('hex');
  const owned = path.resolve('output', 'exports', owner) + path.sep;
  const files = params.files.map(file => {
    const actual = fs.realpathSync(file);
    if (!actual.startsWith(owned) || !fs.statSync(actual).isFile() || fs.statSync(actual).size > (params.video ? 64 : 8) * 1024 * 1024) throw new Error('Scan file does not belong to this sender');
    return actual;
  });
  return withPage(async page => {
    // Cocogoat deliberately posts only to a parent window. A top-level scanner
    // loads its models but never emits load/result, so keep an owned host frame.
    // An opaque about:blank parent makes Chromium deny the scanner's storage.
    // Serve only this page's blank host at the scanner origin, as the official
    // site does, while keeping TLS checks and other render pages unchanged.
    const host = 'https://cocogoat.work/__astrbot_miao_scanner_host__';
    await page.setBypassServiceWorker(true);
    await page.setRequestInterception(true);
    const serveHost = request => request.isNavigationRequest() && request.frame() === page.mainFrame() && request.url() === host
      ? request.respond({ status: 200, contentType: 'text/html', body: '<!doctype html><html><body></body></html>' })
      : request.continue();
    page.on('request', serveHost);
    try {
      await page.goto(host, { waitUntil: 'domcontentloaded', timeout: 30000 });
    } finally {
      page.off('request', serveHost);
      await page.setRequestInterception(false);
    }
    await page.evaluate(() => {
      window.__miaoScanner = { loaded: false, failed: false, result: null };
      window.addEventListener('message', message => {
        const scanner = document.querySelector('#miao-achievement-scanner');
        if (message.source !== scanner?.contentWindow || message.origin !== 'https://cocogoat.work' || message.data?.app !== 'cocogoat.scanner.achievement') return;
        const { event, data } = message.data;
        if (event === 'load') { window.__miaoScanner.loaded = data === true; window.__miaoScanner.failed = data === false; }
        if (event === 'result') window.__miaoScanner.result = data;
      });
    });
    await page.evaluate(url => {
      const scanner = document.createElement('iframe');
      scanner.id = 'miao-achievement-scanner';
      scanner.src = url;
      document.body.append(scanner);
    }, 'https://cocogoat.work/frames/achievement-scanner/' + (params.video ? 'rvfc' : 'screenshot'));
    await page.waitForFunction(() => window.__miaoScanner?.loaded || window.__miaoScanner?.failed, { timeout: 45000 });
    if (await page.evaluate(() => window.__miaoScanner.failed)) throw new Error('Cocogoat failed to initialize');
    const element = await page.waitForSelector('#miao-achievement-scanner');
    const scanner = await element.contentFrame();
    const input = await scanner.waitForSelector('#toki div.top input[type=file]', { timeout: 10000 });
    await input.uploadFile(...files);
    await scanner.waitForFunction(() => {
      const input = document.querySelector('#toki div.top input[type=file]');
      const button = document.querySelector('#toki div.top button');
      return input?.files?.length > 0 && button && !button.disabled;
    }, { timeout: 15000 });
    await scanner.click('#toki div.top button');
    await page.waitForFunction(() => window.__miaoScanner?.result !== null, { timeout: params.video ? 120000 : 90000 });
    return page.evaluate(() => {
      const data = window.__miaoScanner.result;
      if (!Array.isArray(data?.result)) throw new Error('Invalid Cocogoat result');
      return data.result.filter(item => item.success && item.done).map(item => ({
        id: item.achievement?.id, status: item.status, date: item.date,
      }));
    });
  }, scope.getStore()?.signal);
}
