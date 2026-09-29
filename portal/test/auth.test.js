'use strict';
const test = require('node:test');
const assert = require('node:assert');
const crypto = require('crypto');
const { verifyWebAppInitData, verifyLoginWidget, signSession, readSession } = require('../auth');

const TOKEN = '123456:TESTTOKEN';
const now = Math.floor(Date.now() / 1000);

function initData(fields, token = TOKEN) {
  const p = new URLSearchParams(fields);
  const dcs = [...p.entries()].sort(([a], [b]) => (a < b ? -1 : 1)).map(([k, v]) => `${k}=${v}`).join('\n');
  const secret = crypto.createHmac('sha256', 'WebAppData').update(token).digest();
  p.set('hash', crypto.createHmac('sha256', secret).update(dcs).digest('hex'));
  return p.toString();
}
function widget(fields, token = TOKEN) {
  const dcs = Object.keys(fields).sort().map((k) => `${k}=${fields[k]}`).join('\n');
  const secret = crypto.createHash('sha256').update(token).digest();
  return { ...fields, hash: crypto.createHmac('sha256', secret).update(dcs).digest('hex') };
}
const user = JSON.stringify({ id: 7, first_name: 'Ana', username: 'ana_ops' });

test('valid Mini App initData returns the user', () => {
  const u = verifyWebAppInitData(initData({ user, auth_date: String(now), query_id: 'AAA' }), TOKEN);
  assert.equal(u.id, 7);
  assert.equal(u.username, 'ana_ops');
});

test('tampered, expired, wrong-token or unsigned initData is rejected', () => {
  const good = initData({ user, auth_date: String(now) });
  const tampered = good.replace('ana_ops', 'eve');
  assert.equal(verifyWebAppInitData(tampered, TOKEN), null);
  assert.equal(verifyWebAppInitData(initData({ user, auth_date: String(now - 2 * 86400) }), TOKEN), null);
  assert.equal(verifyWebAppInitData(initData({ user, auth_date: String(now) }, '999:OTHER'), TOKEN), null);
  assert.equal(verifyWebAppInitData(new URLSearchParams({ user, auth_date: String(now) }).toString(), TOKEN), null);
  assert.equal(verifyWebAppInitData('', TOKEN), null);
});

test('login widget signature is verified', () => {
  const d = widget({ id: 7, first_name: 'Ana', username: 'ana_ops', auth_date: now });
  assert.equal(verifyLoginWidget(d, TOKEN).id, 7);
  assert.equal(verifyLoginWidget({ ...d, id: 8 }, TOKEN), null);
  assert.equal(verifyLoginWidget(widget({ id: 7, auth_date: now - 3 * 86400 }), TOKEN), null);
});

test('session cookies round-trip and reject tampering/expiry', () => {
  const tok = signSession({ uid: '7', name: '@ana', exp: now + 60 }, 'secret');
  assert.equal(readSession(tok, 'secret').uid, '7');
  assert.equal(readSession(tok, 'other-secret'), null);
  const [body, mac] = tok.split('.');
  const forged = Buffer.from(JSON.stringify({ uid: '1', name: 'x', exp: now + 60 })).toString('base64url');
  assert.equal(readSession(`${forged}.${mac}`, 'secret'), null);
  assert.equal(readSession(signSession({ uid: '7', exp: now - 1 }, 'secret'), 'secret'), null);
  assert.equal(readSession(body, 'secret'), null);
});
