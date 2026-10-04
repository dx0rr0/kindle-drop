// Exercise the actual browser script with a small DOM and controlled HTTP responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.disabled = false; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
}
async function render(ready) {
  const elements = new Map();
  const info = { send_ready: ready, send_blocked_reason: ready ? null : 'Pair this profile with your Kindle before sending.',
    download_path: '/download/test-catalog', kindle: ready ? { host: '192.168.1.25', port: 2222 } : {}, lan: false };
  let sendResolve, removeResolve;
  let removed = false;
  const sandbox = { location: { hash: '', hostname: '127.0.0.1', pathname: '/' },
    sessionStorage: { getItem: () => null, setItem: () => {} }, history: { replaceState: () => {} },
    document: { getElementById: id => { if (!elements.has(id)) elements.set(id, new Element(id)); return elements.get(id); },
      createElement: tag => new Element(tag), createTextNode: text => ({ textContent: text }) },
    fetch: async (path, options = {}) => {
      if (path.endsWith('/remove')) return new Promise(resolve => { removeResolve = response => { if (response.ok) removed = true; resolve(response); }; });
      if (path.endsWith('/send')) return new Promise(resolve => { sendResolve = resolve; });
      const result = path === '/api/bootstrap' ? { token: 'test-token' } : path === '/api/info' ? info :
        removed ? [] : [{ id: '0123456789abcdef', title: 'A test article', author: 'Test author', size: 1024, status: 'Ready', filename: 'A test article.epub' }];
      return { ok: true, json: async () => result };
    }
  };
  vm.runInNewContext(fs.readFileSync('kindle_drop/static/ui.js', 'utf8'), sandbox);
  await new Promise(resolve => setImmediate(resolve));
  const actions = elements.get('books').children[0].children[1];
  const [download, button, remove] = actions.children;
  return { button, download, remove, elements, finish: response => sendResolve(response), finishRemove: response => removeResolve(response) };
}
(async () => {
  const pending = await render(false);
  assert.equal(pending.button.disabled, true);
  assert.equal(pending.download.disabled, false);
  assert.equal(pending.download.textContent, 'Download EPUB');
  assert.equal(pending.download.href, '/download/test-catalog/0123456789abcdef');
  assert.equal(pending.download.download, 'A test article.epub');
  assert.match(pending.button.title, /Pair this profile/);
  assert.match(pending.elements.get('connection').textContent, /Pair this profile/);
  assert.equal(pending.button.attributes['aria-busy'], undefined);
  const active = await render(true);
  assert.equal(active.button.disabled, false);
  assert.equal(active.download.href, '/download/test-catalog/0123456789abcdef');
  const sending = active.button.onclick();
  assert.equal(active.button.disabled, true);
  assert.equal(active.button.attributes['aria-busy'], 'true');
  assert.equal(active.button.textContent, 'Sending…');
  assert.equal(active.remove.disabled, true);
  active.finish({ ok: false, json: async () => ({ error: 'Kindle is asleep. Wake it and retry.' }) });
  await sending;
  assert.equal(active.button.disabled, false);
  assert.equal(active.button.attributes['aria-busy'], undefined);
  assert.equal(active.button.textContent, 'Send to Kindle');
  assert.match(active.elements.get('message').textContent, /Wake it and retry/);
  assert.equal(active.remove.disabled, false);
  const retry = active.button.onclick();
  active.finish({ ok: true, json: async () => ({ message: 'EPUB verified on Kindle.' }) });
  await retry;
  assert.equal(active.button.disabled, false);
  assert.equal(active.button.attributes['aria-busy'], undefined);
  const removing = active.remove.onclick();
  assert.equal(active.remove.disabled, true);
  assert.equal(active.button.disabled, true);
  active.finishRemove({ ok: false, json: async () => ({ error: 'Removal failed.' }) });
  await removing;
  assert.equal(active.remove.disabled, false);
  assert.match(active.elements.get('message').textContent, /Removal failed/);
  const removalRetry = active.remove.onclick();
  active.finishRemove({ ok: true, json: async () => ({ message: 'Removed. Kindle copy kept.' }) });
  await removalRetry;
  assert.equal(active.elements.get('books').children[0].className, 'empty');
  const unpairedRemoval = pending.remove.onclick();
  pending.finishRemove({ ok: true, json: async () => ({ message: 'Removed.' }) });
  await unpairedRemoval;
  assert.equal(pending.elements.get('books').children[0].className, 'empty');
  console.log('PASS: setup explanation, idle cursor state, sending progress, failure recovery and retry.');
})().catch(error => { console.error(error); process.exit(1); });
