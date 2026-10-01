'use strict';
// Social Hub portal: dashboard, approval queue, composer, campaigns and system
// status. Served at PORTAL_DOMAIN and opened inside Telegram as a Mini App.
//
// Reads/writes the CMS database directly for listing and simple edits; anything
// that publishes or talks to Claude goes through n8n workflow 05 (portal-action
// webhook), so the portal and the Telegram buttons share one code path.
const http = require('http');
const fs = require('fs');
const path = require('path');
const { Pool } = require('pg');
const { verifyWebAppInitData, verifyLoginWidget, signSession, readSession, verifyPassword } = require('./auth');

const env = process.env;
const PORT = Number(env.PORT || 3000);
const BOT_TOKEN = env.TELEGRAM_BOT_TOKEN || '';
const ADMIN_CHAT = String(env.TELEGRAM_ADMIN_CHAT_ID || '');
const ALLOWED = String(env.TELEGRAM_ALLOWED_USER_IDS || '').split(',').map((s) => s.trim()).filter(Boolean);
const TG_API = (env.TELEGRAM_API_BASE_URL || 'https://api.telegram.org').replace(/\/$/, '');
const N8N = (env.N8N_INTERNAL_URL || 'http://n8n:5678').replace(/\/$/, '');
const SESSION_SECRET = env.PORTAL_SESSION_SECRET || '';
const HOOK_SECRET = env.PORTAL_WEBHOOK_SECRET || '';
const COOKIE_SECURE = env.PORTAL_COOKIE_SECURE !== 'false';
const SESSION_HOURS = 12;
const MAX_BODY = 15 * 1024 * 1024;
const PUBLIC_DIR = path.join(__dirname, 'public');

if (!SESSION_SECRET || !HOOK_SECRET) {
  console.error('PORTAL_SESSION_SECRET and PORTAL_WEBHOOK_SECRET must be set');
  process.exit(1);
}

const pgBase = {
  host: env.PGHOST || 'postgres',
  port: Number(env.PGPORT || 5432),
  user: env.PGUSER,
  password: env.PGPASSWORD,
  max: 5,
};
const db = new Pool({ ...pgBase, database: env.PGDATABASE || 'social_hub' });
const n8nDb = new Pool({ ...pgBase, database: 'n8n', max: 2 });
const q = async (sql, params) => (await db.query(sql, params)).rows;

// ---------------------------------------------------------------------------
// Telegram helpers
// ---------------------------------------------------------------------------
async function tg(method, payload) {
  const opts = payload instanceof FormData
    ? { method: 'POST', body: payload }
    : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(payload || {}) };
  const r = await fetch(`${TG_API}/bot${BOT_TOKEN}/${method}`, { ...opts, signal: AbortSignal.timeout(30000) });
  const j = await r.json().catch(() => ({}));
  if (!j.ok) throw new Error(`Telegram ${method}: ${j.description || r.status}`);
  return j.result;
}

let botInfo = null;
async function getBot() {
  if (!botInfo && BOT_TOKEN && !BOT_TOKEN.startsWith('REPLACE_ME')) {
    try { botInfo = await tg('getMe'); } catch (e) { console.warn(e.message); }
  }
  return botInfo;
}

// Authorisation: explicit allow-list if configured, otherwise membership of the
// admin (approval) chat - the same people who can press the Telegram buttons.
const memberCache = new Map();
async function isAuthorised(userId) {
  const id = String(userId);
  if (ALLOWED.length) return ALLOWED.includes(id);
  if (!ADMIN_CHAT || ADMIN_CHAT.startsWith('REPLACE_ME')) return false;
  const hit = memberCache.get(id);
  if (hit && hit.until > Date.now()) return hit.ok;
  let ok = false;
  try {
    const m = await tg('getChatMember', { chat_id: ADMIN_CHAT, user_id: Number(id) });
    ok = ['creator', 'administrator', 'member'].includes(m.status) || (m.status === 'restricted' && m.is_member);
  } catch (e) {
    console.warn('getChatMember failed:', e.message);
  }
  memberCache.set(id, { ok, until: Date.now() + 10 * 60 * 1000 });
  return ok;
}

const displayName = (u) => (u.username ? '@' + u.username : [u.first_name, u.last_name].filter(Boolean).join(' ') || String(u.id));

// ---------------------------------------------------------------------------
// n8n portal-action webhook
// ---------------------------------------------------------------------------
async function n8nAction(body) {
  const r = await fetch(`${N8N}/webhook/portal-action`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'X-Portal-Secret': HOOK_SECRET },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(180000),
  });
  const text = await r.text();
  let j = null;
  try { j = JSON.parse(text); } catch { /* empty or non-JSON */ }
  if (!r.ok || !j) {
    const msg = (j && (j.error || j.message)) || text.slice(0, 200) || `n8n responded ${r.status}`;
    const e = new Error(msg); e.status = r.status === 404 ? 503 : 502; throw e;
  }
  return j;
}

// ---------------------------------------------------------------------------
// HTTP plumbing
// ---------------------------------------------------------------------------
class HttpError extends Error { constructor(status, msg) { super(msg); this.status = status; } }

function send(res, status, obj, headers = {}) {
  const body = JSON.stringify(obj);
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store', ...headers });
  res.end(body);
}

function readJson(req) {
  return new Promise((resolve, reject) => {
    let size = 0; const chunks = [];
    req.on('data', (c) => {
      size += c.length;
      if (size > MAX_BODY) { reject(new HttpError(413, 'Request too large (max 15 MB)')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', () => {
      if (!chunks.length) return resolve({});
      try { resolve(JSON.parse(Buffer.concat(chunks).toString())); } catch { reject(new HttpError(400, 'Invalid JSON')); }
    });
    req.on('error', reject);
  });
}

function cookies(req) {
  return Object.fromEntries(String(req.headers.cookie || '').split(';').map((c) => c.trim().split('=')).filter((p) => p[0])
    .map(([k, ...v]) => [k, decodeURIComponent(v.join('='))]));
}

function sessionCookie(value, maxAge) {
  // SameSite=None is required inside Telegram's in-app webview on some clients.
  return `sh_session=${value}; Path=/; HttpOnly; Max-Age=${maxAge}; SameSite=${COOKIE_SECURE ? 'None; Secure' : 'Lax'}`;
}

const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };
function serveStatic(req, res) {
  let p = decodeURIComponent(new URL(req.url, 'http://x').pathname);
  if (p === '/') p = '/index.html';
  const file = path.normalize(path.join(PUBLIC_DIR, p));
  if (!file.startsWith(PUBLIC_DIR) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    // SPA fallback
    return serveFile(res, path.join(PUBLIC_DIR, 'index.html'));
  }
  serveFile(res, file);
}
function serveFile(res, file) {
  res.writeHead(200, {
    'content-type': MIME[path.extname(file)] || 'application/octet-stream',
    'cache-control': file.endsWith('.html') ? 'no-cache' : 'public, max-age=300',
    'x-content-type-options': 'nosniff',
    'referrer-policy': 'same-origin',
  });
  fs.createReadStream(file).pipe(res);
}

// ---------------------------------------------------------------------------
// Routes
// ---------------------------------------------------------------------------
const routes = [];
const route = (method, pattern, handler, { auth = true } = {}) =>
  routes.push({ method, re: new RegExp('^' + pattern.replace(/:(\w+)/g, '(?<$1>[^/]+)') + '$'), handler, auth });

const intId = (v) => {
  const n = Number.parseInt(v, 10);
  if (!Number.isFinite(n) || n <= 0) throw new HttpError(400, 'Invalid id');
  return n;
};
const clampText = (s, n) => String(s ?? '').trim().slice(0, n);
const TARGETS = { all: 'ALL', fb: 'FACEBOOK_ONLY', tw: 'TWITTER_ONLY' };

route('GET', '/healthz', async () => ({ ok: true }), { auth: false });

route('GET', '/api/config', async () => {
  const bot = await getBot();
  return { botUsername: bot ? bot.username : null, passwordLogin: PASSWORD_LOGIN };
}, { auth: false });

// ---- username + password (browser) -----------------------------------------
// One admin account from .env (set with scripts/set-portal-password.sh).
// Failed attempts are throttled per client IP and per username.
const ADMIN_USER = String(env.PORTAL_ADMIN_USER || 'admin').trim().toLowerCase();
const ADMIN_HASH = String(env.PORTAL_ADMIN_PASSWORD_HASH || '').trim();
const PASSWORD_LOGIN = ADMIN_HASH.startsWith('scrypt:');
const LOCK_AFTER = 5;
const LOCK_WINDOW_MS = 15 * 60 * 1000;
const failures = new Map(); // key -> { n, until }

function clientIp(req) {
  // Behind Traefik/Caddy the real client is the last address the proxy appended.
  const xff = String(req.headers['x-forwarded-for'] || '').split(',').map((s) => s.trim()).filter(Boolean);
  return xff.length ? xff[xff.length - 1] : req.socket.remoteAddress || '?';
}
function lockedFor(keys) {
  const now = Date.now();
  return Math.max(0, ...keys.map((k) => {
    const f = failures.get(k);
    return f && f.n >= LOCK_AFTER && f.until > now ? f.until - now : 0;
  }));
}
function recordFailure(keys) {
  const now = Date.now();
  for (const k of keys) {
    const f = failures.get(k);
    failures.set(k, f && f.until > now ? { n: f.n + 1, until: now + LOCK_WINDOW_MS } : { n: 1, until: now + LOCK_WINDOW_MS });
  }
  if (failures.size > 10000) for (const [k, f] of failures) if (f.until < now) failures.delete(k);
}

route('POST', '/api/auth/password', async ({ req, body }) => {
  if (!PASSWORD_LOGIN) throw new HttpError(404, 'Password login is not enabled on this server.');
  const user = String(body.username || '').trim().toLowerCase().slice(0, 64);
  const keys = [`ip:${clientIp(req)}`, `user:${user}`];
  const wait = lockedFor(keys);
  if (wait) throw new HttpError(429, `Too many failed attempts. Try again in ${Math.ceil(wait / 60000)} min.`);
  // Always run scrypt so a wrong username takes as long as a wrong password.
  const okPass = verifyPassword(String(body.password || '').slice(0, 256), ADMIN_HASH);
  if (!okPass || user !== ADMIN_USER) {
    recordFailure(keys);
    throw new HttpError(401, 'Wrong username or password.');
  }
  for (const k of keys) failures.delete(k);
  const s = { uid: `pw:${ADMIN_USER}`, name: ADMIN_USER, exp: Math.floor(Date.now() / 1000) + SESSION_HOURS * 3600 };
  return { body: { ok: true, user: { id: s.uid, name: s.name } }, headers: { 'set-cookie': sessionCookie(signSession(s, SESSION_SECRET), SESSION_HOURS * 3600) } };
}, { auth: false });

async function startSession(user) {
  if (!(await isAuthorised(user.id))) throw new HttpError(403, 'Your Telegram account is not allowed to use this portal. Ask an admin to add you to the approval group.');
  const s = { uid: String(user.id), name: displayName(user), exp: Math.floor(Date.now() / 1000) + SESSION_HOURS * 3600 };
  return { body: { ok: true, user: { id: s.uid, name: s.name } }, headers: { 'set-cookie': sessionCookie(signSession(s, SESSION_SECRET), SESSION_HOURS * 3600) } };
}

route('POST', '/api/auth/webapp', async ({ body }) => {
  const user = verifyWebAppInitData(body.initData, BOT_TOKEN);
  if (!user) throw new HttpError(401, 'Telegram sign-in could not be verified. Close and reopen the app.');
  return startSession(user);
}, { auth: false });

route('POST', '/api/auth/widget', async ({ body }) => {
  const user = verifyLoginWidget(body, BOT_TOKEN);
  if (!user) throw new HttpError(401, 'Telegram login could not be verified.');
  return startSession(user);
}, { auth: false });

route('POST', '/api/auth/logout', async () => ({ body: { ok: true }, headers: { 'set-cookie': sessionCookie('', 0) } }), { auth: false });

route('GET', '/api/me', async ({ session }) => ({ id: session.uid, name: session.name }));

route('GET', '/api/summary', async () => {
  const [status, daily, platform, mentions, errors, last] = await Promise.all([
    q(`SELECT status, count(*)::int AS n FROM content_queue GROUP BY status`),
    q(`SELECT to_char(d, 'YYYY-MM-DD') AS day,
              (SELECT count(*)::int FROM content_queue WHERE created_at::date = d) AS created,
              (SELECT count(*)::int FROM content_queue WHERE status = 'PUBLISHED' AND published_at::date = d) AS published,
              (SELECT count(*)::int FROM content_queue WHERE status = 'FAILED' AND updated_at::date = d) AS failed
         FROM generate_series(current_date - 13, current_date, interval '1 day') AS d`),
    q(`SELECT count(*) FILTER (WHERE fb_post_id IS NOT NULL)::int AS fb_ok,
              count(*) FILTER (WHERE tweet_id IS NOT NULL)::int AS x_ok,
              count(*) FILTER (WHERE status = 'FAILED' AND error_message ILIKE '%Facebook:%')::int AS fb_fail,
              count(*) FILTER (WHERE status = 'FAILED' AND error_message ILIKE '%X:%')::int AS x_fail,
              max(published_at) FILTER (WHERE fb_post_id IS NOT NULL) AS fb_last,
              max(published_at) FILTER (WHERE tweet_id IS NOT NULL) AS x_last
         FROM content_queue WHERE updated_at > now() - interval '30 days'`),
    q(`SELECT status, count(*)::int AS n FROM mentions GROUP BY status`),
    q(`SELECT id, error_message, updated_at FROM content_queue WHERE status = 'FAILED' ORDER BY updated_at DESC LIMIT 5`),
    q(`SELECT max(published_at) AS last_published, max(created_at) AS last_draft FROM content_queue`),
  ]);
  return {
    status: Object.fromEntries(status.map((r) => [r.status, r.n])),
    daily, platform: platform[0], mentions: Object.fromEntries(mentions.map((r) => [r.status, r.n])),
    errors, ...last[0],
  };
});

const POST_COLS = `q.id, q.status, q.created_by, q.source, q.fb_copy, q.twitter_copy, q.media_url, q.source_link,
  q.target_platform, q.fb_post_id, q.tweet_id, q.error_message, q.approved_by, q.regen_count,
  q.created_at, q.updated_at, q.published_at, c.name AS campaign_name`;

route('GET', '/api/posts', async ({ url }) => {
  const status = url.searchParams.get('status');
  const limit = Math.min(Number(url.searchParams.get('limit')) || 30, 100);
  const offset = Math.max(Number(url.searchParams.get('offset')) || 0, 0);
  const statuses = status ? status.split(',').map((s) => s.trim().toUpperCase()) : null;
  return q(`SELECT ${POST_COLS} FROM content_queue q LEFT JOIN campaigns c ON c.id = q.campaign_id
             WHERE ($1::text[] IS NULL OR q.status = ANY($1)) ORDER BY q.id DESC LIMIT $2 OFFSET $3`,
  [statuses, limit, offset]);
});

async function getPost(id) {
  const rows = await q(`SELECT ${POST_COLS} FROM content_queue q LEFT JOIN campaigns c ON c.id = q.campaign_id WHERE q.id = $1`, [id]);
  if (!rows.length) throw new HttpError(404, 'Post not found');
  return rows[0];
}
route('GET', '/api/posts/:id', async ({ params }) => getPost(intId(params.id)));

async function requirePending(id) {
  const p = await getPost(id);
  if (p.status !== 'PENDING_APPROVAL') throw new HttpError(409, `Draft #${id} is ${p.status.toLowerCase().replace('_', ' ')}, not pending approval.`);
  return p;
}

// Edit copy of a pending draft; optionally push a fresh preview to Telegram.
route('PATCH', '/api/posts/:id', async ({ params, body, session }) => {
  const id = intId(params.id);
  await requirePending(id);
  const tw = clampText(body.twitter_copy, 280);
  const fb = clampText(body.fb_copy, 60000);
  if (!tw && !fb) throw new HttpError(400, 'Nothing to save');
  await q(`UPDATE content_queue SET twitter_copy = COALESCE(NULLIF($2,''), twitter_copy),
             fb_copy = COALESCE(NULLIF($3,''), fb_copy), approved_by = NULL WHERE id = $1`, [id, tw, fb]);
  if (body.resend_to_telegram) await n8nAction({ action: 'preview', post_id: id, user: session.name });
  return getPost(id);
});

for (const [action, verb] of [['approve', 'approve'], ['reject', 'reject'], ['regenerate', 'regen'], ['send-to-telegram', 'preview']]) {
  route('POST', `/api/posts/:id/${action}`, async ({ params, body, session }) => {
    const id = intId(params.id);
    await requirePending(id);
    const target = TARGETS[body.target] ? body.target : 'all';
    const result = await n8nAction({ action: verb, post_id: id, target, user: session.name });
    return { ...result, post: await getPost(id) };
  });
}

route('POST', '/api/ai/adapt', async ({ body, session }) => {
  const text = clampText(body.text, 6000);
  if (!text && !body.has_photo) throw new HttpError(400, 'Write something first');
  return n8nAction({ action: 'adapt', text, has_photo: !!body.has_photo, user: session.name });
});

// Create a post from the composer. Images are uploaded to the approval chat
// (so the team sees them) and stored as tg://<file_id>, the same format the
// Telegram flow uses; the publisher fetches them from Telegram at publish time.
route('POST', '/api/posts', async ({ body, session }) => {
  const tw = clampText(body.twitter_copy, 280);
  const fb = clampText(body.fb_copy, 60000);
  if (!tw && !fb) throw new HttpError(400, 'Write the post text first');
  const then = ['telegram', 'publish', 'save'].includes(body.then) ? body.then : 'telegram';
  const target = TARGETS[body.target] ? body.target : 'all';

  let mediaUrl = null;
  if (body.image && body.image.data) {
    const type = String(body.image.type || '');
    if (!/^image\/(jpeg|png|webp)$/.test(type)) throw new HttpError(400, 'Image must be JPG, PNG or WebP');
    const buf = Buffer.from(String(body.image.data), 'base64');
    if (buf.length > 10 * 1024 * 1024) throw new HttpError(400, 'Image must be 10 MB or smaller');
    const form = new FormData();
    form.append('chat_id', ADMIN_CHAT);
    form.append('caption', `🖼 Image uploaded in the portal by ${session.name}`);
    form.append('photo', new Blob([buf], { type }), clampText(body.image.name, 80) || 'image.jpg');
    const msg = await tg('sendPhoto', form);
    const sizes = msg.photo || [];
    if (!sizes.length) throw new HttpError(502, 'Telegram did not return the uploaded photo');
    mediaUrl = 'tg://' + sizes[sizes.length - 1].file_id;
  } else if (body.media_url && /^https:\/\//.test(body.media_url)) {
    mediaUrl = clampText(body.media_url, 1000);
  }

  const [row] = await q(
    `INSERT INTO content_queue (campaign_id, created_by, source, fb_copy, twitter_copy, media_url, status, approved_by)
     VALUES ((SELECT id FROM campaigns WHERE is_active ORDER BY priority DESC, id LIMIT 1),
             'TEAM_MEMBER', 'MANUAL_TEAM', $1, $2, $3, 'PENDING_APPROVAL', NULL) RETURNING id`,
    [fb || tw, tw || fb.slice(0, 280), mediaUrl]);
  const id = row.id;

  let result = { ok: true, status: 'PENDING_APPROVAL' };
  if (then === 'telegram') result = await n8nAction({ action: 'preview', post_id: id, user: session.name });
  if (then === 'publish') result = await n8nAction({ action: 'approve', post_id: id, target, user: session.name });
  return { ...result, post: await getPost(id) };
});

// Image thumbnails for the portal (Telegram files need the bot token, so proxy).
route('GET', '/api/posts/:id/media', async ({ params, res }) => {
  const p = await getPost(intId(params.id));
  if (!p.media_url) throw new HttpError(404, 'No media');
  if (/^https?:\/\//.test(p.media_url)) { res.writeHead(302, { location: p.media_url }); res.end(); return null; }
  const file = await tg('getFile', { file_id: p.media_url.slice(5) });
  const r = await fetch(`${TG_API}/file/bot${BOT_TOKEN}/${file.file_path}`, { signal: AbortSignal.timeout(30000) });
  if (!r.ok) throw new HttpError(502, 'Could not fetch the image from Telegram');
  res.writeHead(200, { 'content-type': r.headers.get('content-type') || 'image/jpeg', 'cache-control': 'private, max-age=3600' });
  res.end(Buffer.from(await r.arrayBuffer()));
  return null;
});

const CAMPAIGN_FIELDS = ['name', 'is_active', 'priority', 'brand_name', 'audience', 'tone_guidelines', 'topics', 'hashtags', 'cta_url', 'source_url', 'language'];
route('GET', '/api/campaigns', async () => q(`SELECT id, ${CAMPAIGN_FIELDS.join(', ')}, updated_at FROM campaigns ORDER BY priority DESC, id`));

function campaignValues(body) {
  const v = {};
  for (const f of CAMPAIGN_FIELDS) {
    if (!(f in body)) continue;
    if (f === 'is_active') v[f] = !!body[f];
    else if (f === 'priority') v[f] = Number.parseInt(body[f], 10) || 0;
    else v[f] = clampText(body[f], 4000) || null;
  }
  for (const u of ['cta_url', 'source_url']) {
    if (v[u] && !/^https?:\/\//.test(v[u])) throw new HttpError(400, `${u} must start with http:// or https://`);
  }
  return v;
}
route('PUT', '/api/campaigns/:id', async ({ params, body }) => {
  const v = campaignValues(body);
  const keys = Object.keys(v);
  if (!keys.length) throw new HttpError(400, 'Nothing to update');
  if ('name' in v && !v.name) throw new HttpError(400, 'Name is required');
  const rows = await q(`UPDATE campaigns SET ${keys.map((k, i) => `${k} = $${i + 2}`).join(', ')} WHERE id = $1 RETURNING *`,
    [intId(params.id), ...keys.map((k) => v[k])]);
  if (!rows.length) throw new HttpError(404, 'Campaign not found');
  return rows[0];
});
route('POST', '/api/campaigns', async ({ body }) => {
  const v = campaignValues(body);
  if (!v.name) throw new HttpError(400, 'Name is required');
  const keys = Object.keys(v);
  const [row] = await q(`INSERT INTO campaigns (${keys.join(', ')}) VALUES (${keys.map((_, i) => `$${i + 1}`).join(', ')}) RETURNING *`,
    keys.map((k) => v[k]));
  return row;
});

route('GET', '/api/mentions', async ({ url }) => {
  const status = url.searchParams.get('status');
  return q(`SELECT id, tweet_id, author_username, author_followers, text, ai_score, ai_verdict, ai_reason, status, created_at
              FROM mentions WHERE ($1::text IS NULL OR status = $1) ORDER BY id DESC LIMIT 50`, [status ? status.toUpperCase() : null]);
});

route('GET', '/api/system', async () => {
  const health = await fetch(`${N8N}/healthz`, { signal: AbortSignal.timeout(5000) }).then((r) => r.ok).catch(() => false);
  let workflows = [];
  let errors24h = null;
  try {
    workflows = (await n8nDb.query(
      `SELECT w.id, w.name, w.active, e.status AS last_status, e."startedAt" AS last_run
         FROM workflow_entity w
         LEFT JOIN LATERAL (SELECT status, "startedAt" FROM execution_entity x
                             WHERE x."workflowId" = w.id ORDER BY x.id DESC LIMIT 1) e ON true
        WHERE w.id LIKE 'sh%' ORDER BY w.name`)).rows;
    errors24h = (await n8nDb.query(
      `SELECT count(*)::int AS n FROM execution_entity WHERE status IN ('error','crashed') AND "startedAt" > now() - interval '24 hours'`)).rows[0].n;
  } catch (e) { console.warn('n8n db:', e.message); }
  const bot = await getBot();
  return {
    n8n_healthy: health, workflows, n8n_errors_24h: errors24h,
    telegram: { bot: bot ? '@' + bot.username : null, admin_chat_configured: !!ADMIN_CHAT && !ADMIN_CHAT.startsWith('REPLACE_ME') },
    facebook: { page_id: env.FACEBOOK_PAGE_ID && !env.FACEBOOK_PAGE_ID.startsWith('REPLACE_ME') ? env.FACEBOOK_PAGE_ID : null },
    x: { username: env.TWITTER_USERNAME || null },
    timezone: env.TZ || 'UTC',
  };
});

// ---------------------------------------------------------------------------
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const r = routes.find((x) => x.method === req.method && x.re.test(url.pathname));
  if (!r) {
    if (req.method === 'GET' && !url.pathname.startsWith('/api/')) return serveStatic(req, res);
    return send(res, 404, { error: 'Not found' });
  }
  try {
    let session = null;
    if (r.auth) {
      // Custom header blocks cross-site form posts (CSRF) in addition to SameSite.
      if (req.method !== 'GET' && req.headers['x-requested-with'] !== 'social-hub') throw new HttpError(403, 'Bad request origin');
      session = readSession(cookies(req).sh_session, SESSION_SECRET);
      if (!session) throw new HttpError(401, 'Please sign in');
    }
    const body = ['POST', 'PUT', 'PATCH'].includes(req.method) ? await readJson(req) : {};
    const out = await r.handler({ req, res, url, params: url.pathname.match(r.re).groups || {}, body, session });
    if (out === null) return; // handler wrote the response
    if (out && out.body && out.headers) return send(res, 200, out.body, out.headers);
    send(res, 200, out);
  } catch (e) {
    const status = e.status || 500;
    if (status >= 500) console.error(req.method, url.pathname, e);
    send(res, status, { error: status >= 500 && !e.status ? 'Server error' : e.message });
  }
});

server.listen(PORT, () => console.log(`portal listening on :${PORT}`));
for (const sig of ['SIGTERM', 'SIGINT']) process.on(sig, () => server.close(() => process.exit(0)));
