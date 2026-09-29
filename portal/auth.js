'use strict';
// Telegram authentication for the portal.
//
// * Mini App: Telegram hands the page `initData`, signed with a key derived from
//   the bot token (HMAC-SHA256("WebAppData", token)).
// * Browser: the Telegram Login Widget returns fields signed with SHA256(token).
// Both are verified here; the portal then issues its own short signed session.
const crypto = require('crypto');

const MAX_AGE_S = 24 * 3600;

function safeEqualHex(a, b) {
  const x = Buffer.from(String(a), 'hex');
  const y = Buffer.from(String(b), 'hex');
  return x.length === y.length && x.length > 0 && crypto.timingSafeEqual(x, y);
}

function checkAge(authDate, now) {
  const t = Number(authDate);
  return Number.isFinite(t) && t > 0 && now - t < MAX_AGE_S && t - now < 300;
}

/** Validates Mini App initData. Returns the Telegram user object or null. */
function verifyWebAppInitData(initData, botToken, now = Math.floor(Date.now() / 1000)) {
  if (!initData || !botToken) return null;
  const params = new URLSearchParams(initData);
  const hash = params.get('hash');
  if (!hash) return null;
  params.delete('hash');
  const dataCheck = [...params.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${k}=${v}`)
    .join('\n');
  const secret = crypto.createHmac('sha256', 'WebAppData').update(botToken).digest();
  const calc = crypto.createHmac('sha256', secret).update(dataCheck).digest('hex');
  if (!safeEqualHex(calc, hash) || !checkAge(params.get('auth_date'), now)) return null;
  try {
    const user = JSON.parse(params.get('user') || 'null');
    return user && user.id ? user : null;
  } catch {
    return null;
  }
}

/** Validates Telegram Login Widget data. Returns a user-like object or null. */
function verifyLoginWidget(data, botToken, now = Math.floor(Date.now() / 1000)) {
  if (!data || !data.hash || !botToken) return null;
  const fields = Object.keys(data)
    .filter((k) => k !== 'hash' && data[k] !== undefined && data[k] !== null)
    .sort()
    .map((k) => `${k}=${data[k]}`)
    .join('\n');
  const secret = crypto.createHash('sha256').update(botToken).digest();
  const calc = crypto.createHmac('sha256', secret).update(fields).digest('hex');
  if (!safeEqualHex(calc, data.hash) || !checkAge(data.auth_date, now)) return null;
  return { id: Number(data.id), first_name: data.first_name, last_name: data.last_name, username: data.username };
}

// ---- portal session cookie: base64url(json).hmac --------------------------
function signSession(payload, secret) {
  const body = Buffer.from(JSON.stringify(payload)).toString('base64url');
  const mac = crypto.createHmac('sha256', secret).update(body).digest('base64url');
  return `${body}.${mac}`;
}

function readSession(token, secret, now = Math.floor(Date.now() / 1000)) {
  if (!token || !secret) return null;
  const [body, mac] = String(token).split('.');
  if (!body || !mac) return null;
  const want = crypto.createHmac('sha256', secret).update(body).digest('base64url');
  const a = Buffer.from(mac);
  const b = Buffer.from(want);
  if (a.length !== b.length || !crypto.timingSafeEqual(a, b)) return null;
  try {
    const s = JSON.parse(Buffer.from(body, 'base64url').toString());
    return s.exp > now ? s : null;
  } catch {
    return null;
  }
}

module.exports = { verifyWebAppInitData, verifyLoginWidget, signSession, readSession };
