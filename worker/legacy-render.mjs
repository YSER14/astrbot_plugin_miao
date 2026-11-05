/** Route reviewed legacy templates through AstrBot's one owned browser. */
import path from 'node:path';
import { scope } from './main.mjs';
import { render } from './render.mjs';

export default {
  async screenshot(_name, data) {
    const filename = path.resolve(data.tplFile || '');
    for (const plugin of ['mora-plugin', 'liangshi-calc', 'achievements-plugin']) {
      const resources = path.resolve('plugins', plugin, 'resources');
      const relative = path.relative(resources, filename);
      if (relative.startsWith('..') || path.isAbsolute(relative) || !relative.endsWith('.html')) continue;
      const event = scope.getStore()?.event;
      if (!event) throw new Error('Template requires an original message event');
      // 老模板跟上游一样出 jpeg：这些图动辄 4800×2700，PNG 太大发不出去。
      return render(event, plugin, relative.replace(/\.html$/, ''), data, { retType: 'base64', jpeg: true });
    }
    throw new Error('Unknown legacy template scope');
  },
};
