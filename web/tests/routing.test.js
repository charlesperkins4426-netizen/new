import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { api } from '../api.js';

const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const chat = readFileSync(new URL('../chat.js', import.meta.url), 'utf8');

for (const prefix of ['/', '/dialx/', '/team/gateway/']) {
  const page = `https://example.test${prefix}`;

  test(`admin requests stay inside ${prefix}`, async (t) => {
    const requests = [];
    t.mock.method(globalThis, 'fetch', async (path, options) => {
      requests.push({ url: new URL(path, page), options });
      return new Response(JSON.stringify({ ok: true }));
    });
    assert.deepEqual(await api('overview'), { ok: true });
    await api('accounts', { method: 'POST', body: { cookies: 'fixture' } });
    await api('logs?page=2&status=success');
    assert.deepEqual(requests.map(({ url }) => url.pathname + url.search), [
      `${prefix}api/admin/overview`, `${prefix}api/admin/accounts`,
      `${prefix}api/admin/logs?page=2&status=success`,
    ]);
    assert.equal(requests[1].options.credentials, 'same-origin');
    assert.equal(requests[1].options.headers['Content-Type'], 'application/json');
    assert.equal(requests[1].options.headers.Authorization, undefined);
  });

  test(`assets and chat stay inside ${prefix} without injected scripts`, () => {
    const stylesheet = html.match(/href="([^"]+styles\.css)"/)[1];
    const script = html.match(/src="([^"]+app\.js)"/)[1];
    const completion = chat.match(/fetch\('([^']+)'/)[1];
    assert.equal(new URL(stylesheet, page).pathname, `${prefix}static/styles.css`);
    assert.equal(new URL(script, page).pathname, `${prefix}static/app.js`);
    assert.equal(new URL(completion, page).pathname, `${prefix}v1/chat/completions`);
    assert.doesNotMatch(html, /<base\b|<script(?![^>]*\bsrc=)/i);
    assert.doesNotMatch(chat, /__BASE__/);
  });
}
