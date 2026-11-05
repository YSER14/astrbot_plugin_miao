/** Narrow replacements for Miao's three direct Yunzai common imports. */
export async function makeForwardMsg(event, messages, title = '') {
  return { type: 'forward', title, nodes: messages.map((message) => ({
    user_id: event.self_id, nickname: '喵喵', message: Array.isArray(message) ? message : [message],
  })) };
}

export async function relpyPrivate(user, message) {
  // The host bridge only exists in the deployment this adapter was built for.
  // A stock install has no bridge/main.mjs, so a private reply must report a
  // failure instead of rejecting the whole upstream handler.
  let callPlatform;
  try {
    ({ callPlatform } = await import('../../bridge/main.mjs'));
  } catch {
    return { status: 'failed', retcode: -1, message: 'private reply bridge unavailable' };
  }
  return callPlatform('send_private', { user_id: String(user), message });
}

export const sleep = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
export default { makeForwardMsg, relpyPrivate, sleep };
