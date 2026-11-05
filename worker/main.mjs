/** Bounded JSON-line transport. Logs never share the stdout protocol stream. */
import readline from 'node:readline';
import { AsyncLocalStorage } from 'node:async_hooks';

const output = process.stdout.write.bind(process.stdout);
const diagnostic = (...args) => process.stderr.write(`[miao] ${args.map(String).join(' ')}\n`);
console.log = diagnostic;
console.info = diagnostic;
console.warn = diagnostic;
console.error = diagnostic;
export const scope = new AsyncLocalStorage();
const running = new Map();
const callbacks = new Map();
let sequence = 0;
let handler;

function send(packet) {
  output(JSON.stringify(packet) + '\n');
}

export function callPlatform(method, params) {
  const context = scope.getStore();
  if (!context || context.signal.aborted) throw new Error('Request was cancelled');
  const id = `callback:${++sequence}`;
  return new Promise((resolve, reject) => {
    const stop = () => { callbacks.delete(id); reject(new Error('Request was cancelled')); };
    context.signal.addEventListener('abort', stop, { once: true });
    callbacks.set(id, {
      resolve(value) { context.signal.removeEventListener('abort', stop); resolve(value); },
      reject(error) { context.signal.removeEventListener('abort', stop); reject(error); },
    });
    send({ kind: 'callback', id, method, params: { ...params, request_id: context.id, event_key: context.event?.token } });
  });
}

async function dispatch(method, params) {
  if (method === 'health') return { runtime: 'astrbot-miao', protocol: 1, node: process.version, loaded: Boolean(handler) };
  if (method === 'shutdown') {
    for (const controller of running.values()) controller.abort();
    if (handler?.close) await handler.close();
    setImmediate(() => process.exit(0));
    return true;
  }
  if (!handler) handler = await import('./runtime.mjs');
  return handler.handle(method, params);
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
input.on('line', (line) => {
  let packet;
  try {
    if (Buffer.byteLength(line) > 8 * 1024 * 1024) throw new Error('Packet too large');
    packet = JSON.parse(line);
  } catch { diagnostic('Invalid input packet'); return; }
  if (packet.kind === 'callback_result') {
    const pending = callbacks.get(packet.id);
    if (pending) {
      callbacks.delete(packet.id);
      packet.error ? pending.reject(new Error(packet.error)) : pending.resolve(packet.result);
    }
    return;
  }
  if (packet.kind === 'cancel') { running.get(String(packet.id))?.abort(); return; }
  if (packet.kind !== 'request') return;
  const id = String(packet.id);
  if (running.size >= 16 || running.has(id)) {
    send({ id, error: 'Worker capacity exceeded' });
    return;
  }
  const controller = new AbortController();
  running.set(id, controller);
  scope.run({ id, signal: controller.signal }, async () => {
    try {
      const result = await dispatch(packet.method, packet.params || {});
      if (!controller.signal.aborted) send({ id, result });
    } catch (error) {
      // 运维要原因，群里不能要原因：message 与栈只进 stderr，回给用户的仍是
      // 通用文案。之前只打 error.name（一律 "Error"），唯一的线索等于丢掉，
      // 排障时只能重跑一遍进程内复现，白花时间。
      diagnostic(`Action failed: ${packet.method}: ${error?.name || 'Error'}: ${error?.message || ''}`);
      const frames = String(error?.stack || '').split('\n').slice(1, 5).join(' | ').trim();
      if (frames) diagnostic(`Action stack: ${frames}`);
      if (!controller.signal.aborted) send({ id, error: error?.publicMessage || 'Miao action failed' });
    } finally { running.delete(id); }
  });
});
input.on('close', async () => {
  for (const controller of running.values()) controller.abort();
  if (handler?.close) await handler.close();
  process.exit(0);
});
