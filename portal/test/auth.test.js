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

const { hashPassword, verifyPassword } = require('../auth');
const { execFileSync } = require('child_process');

test('password hashes verify and reject wrong or malformed input', () => {
  const h = hashPassword('correct horse battery');
  assert.match(h, /^scrypt:16384:8:1:[\w-]+:[\w-]+$/);
  assert.ok(!h.includes('$'), 'no $ so docker compose does not interpolate it');
  assert.equal(verifyPassword('correct horse battery', h), true);
  assert.equal(verifyPassword('correct horse batterY', h), false);
  assert.equal(verifyPassword('', h), false);
  assert.equal(verifyPassword('x', ''), false);
  assert.equal(verifyPassword('x', 'scrypt:1:2:3'), false);
  assert.equal(verifyPassword('x', 'scrypt:16384:8:1:AAAA:'), false);
});

test('hash written by scripts/set-portal-password.sh (Python) verifies in Node', () => {
  const py = `import base64,hashlib
b=lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
s=b"0123456789abcdef"
k=hashlib.scrypt(b"Pw-from-python-1", salt=s, n=16384, r=8, p=1, dklen=64, maxmem=64*1024*1024)
print(f"scrypt:16384:8:1:{b(s)}:{b(k)}")`;
  const h = execFileSync('python3', ['-c', py]).toString().trim();
  assert.equal(verifyPassword('Pw-from-python-1', h), true);
  assert.equal(verifyPassword('Pw-from-python-2', h), false);
});
