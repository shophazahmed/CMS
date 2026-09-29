'use strict';
/* Social Hub portal - vanilla JS, no build step. Works in a browser and as a
   Telegram Mini App (window.Telegram.WebApp). */
const tgApp = window.Telegram && window.Telegram.WebApp && window.Telegram.WebApp.initData ? window.Telegram.WebApp : null;
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const cps = (s) => [...String(s || '')].length;
const fmtDate = (d) => (d ? new Date(d).toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }) : '—');
const ago = (d) => {
  if (!d) return 'never';
  const s = (Date.now() - new Date(d).getTime()) / 1000;
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};
const STATUS_LABEL = { PENDING_APPROVAL: 'Pending', PUBLISHED: 'Published', FAILED: 'Failed', REJECTED: 'Rejected', DRAFT: 'Draft', APPROVED: 'Publishing…' };
const pill = (s) => `<span class="pill ${esc(s)}">${esc(STATUS_LABEL[s] || s)}</span>`;

// ---------------------------------------------------------------------------
async function api(path, opts = {}) {
  const r = await fetch(path, {
    method: opts.method || 'GET',
    credentials: 'same-origin',
    headers: { 'content-type': 'application/json', 'x-requested-with': 'social-hub' },
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const j = await r.json().catch(() => ({}));
  if (r.status === 401 && !opts.noAuthRedirect) { showLogin(); throw new Error(j.error || 'Please sign in'); }
  if (!r.ok) throw new Error(j.error || `Request failed (${r.status})`);
  return j;
}

function toast(msg, ms = 3500) {
  const t = $('#toast');
  t.textContent = msg; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, ms);
}

async function busy(btn, fn) {
  const label = btn.textContent;
  btn.disabled = true; btn.textContent = '…';
  try { return await fn(); } catch (e) { toast('⚠️ ' + e.message, 6000); } finally { btn.disabled = false; btn.textContent = label; }
}

// ---------------------------------------------------------------------------
// Theme + auth
// ---------------------------------------------------------------------------
function applyTelegramTheme() {
  if (!tgApp) return;
  tgApp.ready(); tgApp.expand();
  const p = tgApp.themeParams || {};
  const root = document.documentElement.style;
  const map = { bg_color: '--bg', secondary_bg_color: '--card', text_color: '--text', hint_color: '--muted', button_color: '--accent', button_text_color: '--accent-text' };
  // Telegram's bg is usually the lighter one; cards use secondary_bg for contrast.
  if (p.secondary_bg_color) { root.setProperty('--bg', p.secondary_bg_color); root.setProperty('--card', p.bg_color || p.secondary_bg_color); }
  for (const [k, v] of Object.entries(map)) if (p[k] && !['bg_color', 'secondary_bg_color'].includes(k)) root.setProperty(v, p[k]);
  if (p.hint_color) root.setProperty('--line', p.hint_color + '40');
  document.documentElement.dataset.theme = tgApp.colorScheme === 'dark' ? 'dark' : 'light';
}

function showLogin(msg) {
  $('#tabs').hidden = true;
  document.querySelectorAll('.tab').forEach((t) => { t.hidden = true; });
  $('#login').hidden = false;
  if (msg) $('#login-error').textContent = msg;
  if (!tgApp) loadLoginWidget();
}

async function loadLoginWidget() {
  const box = $('#login-widget');
  if (box.dataset.loaded) return;
  box.dataset.loaded = '1';
  const cfg = await api('/api/config', { noAuthRedirect: true }).catch(() => ({}));
  if (!cfg.botUsername) { box.innerHTML = '<p class="muted small">Bot is not configured yet.</p>'; return; }
  window.onTelegramAuth = async (user) => {
    try { await api('/api/auth/widget', { method: 'POST', body: user, noAuthRedirect: true }); start(); }
    catch (e) { $('#login-error').textContent = e.message; }
  };
  const s = document.createElement('script');
  s.src = 'https://telegram.org/js/telegram-widget.js?22';
  s.async = true;
  s.dataset.telegramLogin = cfg.botUsername;
  s.dataset.size = 'large';
  s.dataset.onauth = 'onTelegramAuth(user)';
  s.dataset.requestAccess = 'write';
  box.appendChild(s);
}

async function signIn() {
  try { return await api('/api/me', { noAuthRedirect: true }); } catch { /* no session yet */ }
  if (tgApp) {
    await api('/api/auth/webapp', { method: 'POST', body: { initData: tgApp.initData }, noAuthRedirect: true });
    return api('/api/me', { noAuthRedirect: true });
  }
  return null;
}

async function start() {
  applyTelegramTheme();
  let me = null;
  try { me = await signIn(); } catch (e) { return showLogin(e.message); }
  if (!me) return showLogin();
  $('#who').textContent = me.name;
  $('#login').hidden = true;
  $('#tabs').hidden = false;
  const initial = (location.hash || '#dashboard').slice(1);
  show(TABS[initial] ? initial : 'dashboard');
  refreshBadge();
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
const TABS = {};
let current = null;
function show(name) {
  current = name;
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.tab').forEach((t) => { t.hidden = t.id !== 'tab-' + name; });
  history.replaceState(null, '', '#' + name);
  TABS[name]($('#tab-' + name)).catch((e) => { $('#tab-' + name).innerHTML = `<div class="card error">${esc(e.message)}</div>`; });
}
$('#tabs').addEventListener('click', (e) => { const b = e.target.closest('button[data-tab]'); if (b) show(b.dataset.tab); });
window.addEventListener('hashchange', () => {
  const t = location.hash.slice(1);
  if (TABS[t] && t !== current && !$('#tabs').hidden) show(t);
});

async function refreshBadge() {
  try {
    const s = await api('/api/summary');
    const n = s.status.PENDING_APPROVAL || 0;
    const b = $('#queue-badge'); b.textContent = n; b.hidden = !n;
  } catch { /* ignore */ }
}

// ---- Dashboard ------------------------------------------------------------
TABS.dashboard = async (el) => {
  el.innerHTML = '<div class="card muted">Loading…</div>';
  const [s, sys] = await Promise.all([api('/api/summary'), api('/api/system').catch(() => null)]);
  const st = s.status; const p = s.platform || {}; const m = s.mentions || {};
  const max = Math.max(1, ...s.daily.map((d) => Math.max(d.created, d.published + d.failed)));
  const bars = s.daily.map((d) => `<div class="col" title="${esc(d.day)}: ${d.published} published, ${d.failed} failed, ${d.created} drafted">
      <div class="seg pub" style="height:${(d.published / max) * 100}%"></div>
      <div class="seg fail" style="height:${(d.failed / max) * 100}%"></div>
      <div class="seg cre" style="height:${(Math.max(0, d.created - d.published - d.failed) / max) * 100}%"></div></div>`).join('');
  const days = s.daily.map((d, i) => `<span>${i % 2 ? '' : esc(d.day.slice(8))}</span>`).join('');
  const wfBad = sys && sys.workflows.filter((w) => !w.active).length;
  el.innerHTML = `
    <div class="grid">
      <div class="stat"><div class="n">${st.PENDING_APPROVAL || 0}</div><div class="l">Waiting for approval</div></div>
      <div class="stat"><div class="n">${st.PUBLISHED || 0}</div><div class="l">Published</div></div>
      <div class="stat"><div class="n">${st.FAILED || 0}</div><div class="l">Failed</div></div>
      <div class="stat"><div class="n">${st.REJECTED || 0}</div><div class="l">Rejected</div></div>
    </div>
    <div class="card">
      <h3>Last 14 days</h3>
      <div class="chart">${bars}</div><div class="chart-x">${days}</div>
      <div class="legend"><span><i style="background:var(--ok)"></i>Published</span><span><i style="background:var(--bad)"></i>Failed</span><span><i style="background:var(--accent);opacity:.35"></i>Drafted</span></div>
    </div>
    <div class="grid">
      <div class="stat"><div class="l">📘 Facebook (30 d)</div><div class="n">${p.fb_ok || 0}</div>
        <div class="l">${p.fb_fail ? `<span class="pill bad">${p.fb_fail} failed</span> ` : ''}last ${esc(ago(p.fb_last))}</div></div>
      <div class="stat"><div class="l">🐦 X (30 d)</div><div class="n">${p.x_ok || 0}</div>
        <div class="l">${p.x_fail ? `<span class="pill bad">${p.x_fail} failed</span> ` : ''}last ${esc(ago(p.x_last))}</div></div>
      <div class="stat"><div class="l">🔔 Mentions</div><div class="n">${(m.NOTIFIED || 0) + (m.RETWEETED || 0)}</div>
        <div class="l">${m.RETWEETED || 0} retweeted · ${m.FILTERED || 0} filtered</div></div>
      <div class="stat"><div class="l">⚙️ System</div><div class="n">${sys ? (sys.n8n_healthy && !wfBad ? '✅' : '⚠️') : '?'}</div>
        <div class="l">${sys ? `${sys.workflows.filter((w) => w.active).length}/${sys.workflows.length} workflows on` : 'unknown'}</div></div>
    </div>
    ${s.errors.length ? `<div class="card"><h3>Recent failures</h3>${s.errors.map((e) => `
      <div class="small" style="margin-bottom:8px"><b>#${e.id}</b> · ${esc(fmtDate(e.updated_at))}<br><span class="error">${esc(e.error_message)}</span></div>`).join('')}</div>` : ''}
    <p class="muted small">Last draft ${esc(ago(s.last_draft))} · last published ${esc(ago(s.last_published))}</p>`;
};

// ---- Post card (queue + history) --------------------------------------------
function postCard(p, { actions }) {
  const media = p.media_url ? `<img class="thumb" loading="lazy" src="/api/posts/${p.id}/media" alt="Post image" onerror="this.remove()">` : '';
  const links = [
    p.fb_post_id ? `📘 <a href="https://facebook.com/${esc(p.fb_post_id)}" target="_blank" rel="noopener">Facebook post</a>` : '',
    p.tweet_id ? `🐦 <a href="https://x.com/i/status/${esc(p.tweet_id)}" target="_blank" rel="noopener">Tweet</a>` : '',
    p.source_link ? `📰 <a href="${esc(p.source_link)}" target="_blank" rel="noopener">Source</a>` : '',
  ].filter(Boolean).join(' · ');
  return `<div class="card post" data-id="${p.id}">
    <div class="row spread"><div class="row"><b>#${p.id}</b> ${pill(p.status)}
      <span class="muted small">${p.created_by === 'TEAM_MEMBER' ? '👤 Team' : '🤖 AI'} · ${esc(p.campaign_name || '')}</span></div>
      <span class="muted small">${esc(fmtDate(p.published_at || p.created_at))}</span></div>
    <div class="label">🐦 X · ${cps(p.twitter_copy)}/280</div><div class="copy">${esc(p.twitter_copy)}</div>
    <div class="label">📘 Facebook</div><div class="copy">${esc(p.fb_copy)}</div>
    ${media}
    ${links ? `<div class="small" style="margin-top:8px">${links}</div>` : ''}
    ${p.error_message ? `<div class="small error" style="margin-top:6px">${esc(p.error_message)}</div>` : ''}
    ${p.approved_by ? `<div class="small muted" style="margin-top:4px">Decided by ${esc(p.approved_by)}</div>` : ''}
    ${actions ? `<div class="actions">
      <button class="btn primary" data-act="approve" data-target="all">✅ Publish all</button>
      <button class="btn" data-act="approve" data-target="fb">📘 FB only</button>
      <button class="btn" data-act="approve" data-target="tw">🐦 X only</button>
      <button class="btn" data-act="edit">✏️ Edit</button>
      <button class="btn" data-act="regenerate">♻️ Regenerate</button>
      <button class="btn danger" data-act="reject">❌ Reject</button></div>` : ''}
  </div>`;
}

function confirmBox(text) {
  return new Promise((resolve) => {
    if (tgApp && tgApp.showConfirm) tgApp.showConfirm(text, resolve); else resolve(window.confirm(text));
  });
}

// ---- Queue ----------------------------------------------------------------
TABS.queue = async (el) => {
  el.innerHTML = '<div class="card muted">Loading…</div>';
  const posts = await api('/api/posts?status=PENDING_APPROVAL');
  el.innerHTML = posts.length ? posts.map((p) => postCard(p, { actions: true })).join('')
    : '<div class="card center muted">Nothing waiting for approval. 🎉<br>New AI drafts arrive daily at 08:00, or create one in Compose.</div>';
  el.onclick = async (e) => {
    const b = e.target.closest('button[data-act]'); if (!b) return;
    const card = b.closest('.post'); const id = card.dataset.id; const act = b.dataset.act;
    if (act === 'edit') return editInline(card, posts.find((p) => String(p.id) === id));
    const what = { approve: `Publish draft #${id} to ${{ all: 'Facebook and X', fb: 'Facebook', tw: 'X' }[b.dataset.target]} now?`, reject: `Reject draft #${id}?`, regenerate: `Ask Claude for a new version of #${id}?` }[act];
    if (!(await confirmBox(what))) return;
    await busy(b, async () => {
      const r = await api(`/api/posts/${id}/${act}`, { method: 'POST', body: { target: b.dataset.target } });
      if (act === 'approve') toast(r.ok ? '✅ Published' : '⚠️ ' + (r.error || 'Publishing failed'), 6000);
      else toast(act === 'reject' ? 'Rejected' : '♻️ New version created');
      TABS.queue(el); refreshBadge();
    });
  };
};

function editInline(card, p) {
  card.innerHTML = `<b>Edit #${p.id}</b>
    <label>🐦 X text</label><textarea data-f="twitter_copy" maxlength="280">${esc(p.twitter_copy)}</textarea><div class="count"></div>
    <label>📘 Facebook text</label><textarea data-f="fb_copy" style="min-height:160px">${esc(p.fb_copy)}</textarea>
    <label class="row" style="font-weight:500"><input type="checkbox" data-f="resend" checked> Send the updated preview to Telegram</label>
    <div class="actions"><button class="btn primary" data-save>Save</button><button class="btn" data-cancel>Cancel</button></div>`;
  wireCounter(card.querySelector('[data-f=twitter_copy]'), card.querySelector('.count'));
  card.querySelector('[data-cancel]').onclick = (e) => { e.stopPropagation(); show('queue'); };
  card.querySelector('[data-save]').onclick = (e) => {
    e.stopPropagation();
    busy(e.target, async () => {
      await api(`/api/posts/${p.id}`, { method: 'PATCH', body: {
        twitter_copy: card.querySelector('[data-f=twitter_copy]').value,
        fb_copy: card.querySelector('[data-f=fb_copy]').value,
        resend_to_telegram: card.querySelector('[data-f=resend]').checked } });
      toast('Saved'); show('queue');
    });
  };
}

function wireCounter(ta, out) {
  const upd = () => { const n = cps(ta.value); out.textContent = `${n}/280`; out.classList.toggle('over', n > 280); };
  ta.addEventListener('input', upd); upd();
}

// ---- Compose --------------------------------------------------------------
TABS.compose = async (el) => {
  el.innerHTML = `<div class="card">
    <h2>New post</h2>
    <p class="muted small">Paste your text, add a photo if you like, let Claude tidy it up for each platform, then send it to Telegram for approval or publish straight away.</p>
    <label>Your text</label>
    <textarea id="c-raw" placeholder="What do you want to post about? Facts, names, links…"></textarea>
    <label>Photo (optional, JPG/PNG/WebP, max 10 MB)</label>
    <div class="drop" id="c-drop">Tap to choose a photo<input id="c-file" type="file" accept="image/jpeg,image/png,image/webp" hidden></div>
    <div class="actions"><button class="btn" id="c-ai">✨ Write FB + X versions with AI</button><button class="btn" id="c-same">Use my text as-is</button></div>
    <label>🐦 X text</label><textarea id="c-tw" maxlength="280"></textarea><div class="count" id="c-twc"></div>
    <label>📘 Facebook text</label><textarea id="c-fb" style="min-height:160px"></textarea>
    <label>Post to</label>
    <div class="seg-choice">
      <label><input type="radio" name="c-target" value="all" checked> Facebook + X</label>
      <label><input type="radio" name="c-target" value="fb"> Facebook only</label>
      <label><input type="radio" name="c-target" value="tw"> X only</label>
    </div>
    <div class="actions">
      <button class="btn primary" id="c-tg">📨 Send to Telegram for approval</button>
      <button class="btn" id="c-pub">🚀 Publish now</button>
    </div></div>`;
  let image = null;
  const drop = $('#c-drop', el); const file = $('#c-file', el);
  wireCounter($('#c-tw', el), $('#c-twc', el));
  drop.onclick = () => file.click();
  file.onchange = () => {
    const f = file.files[0]; if (!f) return;
    if (f.size > 10 * 1024 * 1024) { toast('Image must be 10 MB or smaller'); return; }
    const rd = new FileReader();
    rd.onload = () => {
      image = { name: f.name, type: f.type, data: String(rd.result).split(',')[1] };
      drop.innerHTML = `<img src="${rd.result}" alt="Selected photo"><div class="small">Tap to change</div>`;
      drop.appendChild(file);
    };
    rd.readAsDataURL(f);
  };
  $('#c-same', el).onclick = () => {
    const t = $('#c-raw', el).value.trim();
    $('#c-fb', el).value = t; $('#c-tw', el).value = [...t].slice(0, 280).join('');
    $('#c-tw', el).dispatchEvent(new Event('input'));
  };
  $('#c-ai', el).onclick = (e) => busy(e.target, async () => {
    const r = await api('/api/ai/adapt', { method: 'POST', body: { text: $('#c-raw', el).value, has_photo: !!image } });
    $('#c-tw', el).value = r.twitter_copy; $('#c-fb', el).value = r.fb_copy;
    $('#c-tw', el).dispatchEvent(new Event('input'));
    toast('✨ Drafted - review and edit before sending');
  });
  const submit = (then) => async (e) => {
    const target = el.querySelector('input[name=c-target]:checked').value;
    const tw = $('#c-tw', el).value.trim(); const fb = $('#c-fb', el).value.trim();
    if (!tw && !fb) { toast('Fill in the post text (or use the AI / as-is buttons)'); return; }
    if (cps(tw) > 280) { toast('X text is over 280 characters'); return; }
    if (then === 'publish' && !(await confirmBox(`Publish to ${{ all: 'Facebook and X', fb: 'Facebook', tw: 'X' }[target]} now?`))) return;
    busy(e.target, async () => {
      const r = await api('/api/posts', { method: 'POST', body: { twitter_copy: tw, fb_copy: fb, image, target, then } });
      if (then === 'publish') toast(r.ok ? `✅ Published (#${r.post.id})` : '⚠️ ' + (r.error || r.post.error_message || 'Failed'), 7000);
      else toast(`📨 Draft #${r.post.id} sent to Telegram for approval`);
      refreshBadge(); show(then === 'publish' ? 'history' : 'queue');
    });
  };
  $('#c-tg', el).onclick = submit('telegram');
  $('#c-pub', el).onclick = submit('publish');
};

// ---- History --------------------------------------------------------------
TABS.history = async (el, offset = 0) => {
  el.innerHTML = `<div class="card row"><select id="h-status" style="max-width:220px">
      <option value="PUBLISHED,FAILED,REJECTED">All finished</option><option value="PUBLISHED">Published</option>
      <option value="FAILED">Failed</option><option value="REJECTED">Rejected</option></select></div><div id="h-list"></div>`;
  const sel = $('#h-status', el);
  const load = async () => {
    const posts = await api(`/api/posts?status=${encodeURIComponent(sel.value)}&limit=30&offset=${offset}`);
    $('#h-list', el).innerHTML = posts.map((p) => postCard(p, { actions: false })).join('') || '<div class="card muted center">Nothing here yet.</div>';
  };
  sel.onchange = load; await load();
};

// ---- Campaigns ------------------------------------------------------------
const CAMP_FIELDS = [
  ['name', 'Name', 'text'], ['source_url', 'Website to read before writing', 'url'], ['cta_url', 'Call-to-action link', 'url'],
  ['brand_name', 'Brand / organisation', 'text'], ['audience', 'Audience', 'area'], ['tone_guidelines', 'Tone of voice', 'area'],
  ['topics', 'Topics / talking points', 'area'], ['hashtags', 'Hashtags', 'text'], ['language', 'Language (e.g. en, dv)', 'text'],
  ['priority', 'Priority (highest active campaign drives the daily draft)', 'number'],
];
TABS.campaigns = async (el) => {
  const list = await api('/api/campaigns');
  const form = (c) => `<form class="card" data-id="${c.id || ''}">
      <div class="row spread"><h3>${esc(c.name || 'New campaign')}</h3>
        <label class="row" style="margin:0;font-weight:500"><input type="checkbox" name="is_active" ${c.is_active !== false ? 'checked' : ''}> Active</label></div>
      ${CAMP_FIELDS.map(([k, l, t]) => `<label>${esc(l)}</label>${t === 'area'
        ? `<textarea name="${k}">${esc(c[k])}</textarea>`
        : `<input type="${t}" name="${k}" value="${esc(c[k] ?? '')}">`}`).join('')}
      <div class="actions"><button class="btn primary" type="submit">Save</button></div></form>`;
  el.innerHTML = list.map(form).join('') + `<details class="card"><summary>➕ Add a campaign</summary>${form({ is_active: true, priority: 0, language: 'en' })}</details>`;
  el.querySelectorAll('form').forEach((f) => {
    f.onsubmit = (e) => {
      e.preventDefault();
      const body = Object.fromEntries(CAMP_FIELDS.map(([k]) => [k, f.elements[k].value]));
      body.is_active = f.elements.is_active.checked;
      busy(f.querySelector('button[type=submit]'), async () => {
        const id = f.dataset.id;
        await api(id ? `/api/campaigns/${id}` : '/api/campaigns', { method: id ? 'PUT' : 'POST', body });
        toast('Campaign saved'); TABS.campaigns(el);
      });
    };
  });
};

// ---- System ---------------------------------------------------------------
TABS.system = async (el) => {
  el.innerHTML = '<div class="card muted">Loading…</div>';
  const [s, mentions] = await Promise.all([api('/api/system'), api('/api/mentions')]);
  const yes = (b, t = 'OK', f = 'Not set') => `<span class="pill ${b ? 'ok' : 'bad'}">${b ? t : f}</span>`;
  el.innerHTML = `
    <div class="card"><h3>Connections</h3><table>
      <tr><td>n8n automation</td><td>${yes(s.n8n_healthy, 'Running', 'Down')}</td></tr>
      <tr><td>Telegram bot</td><td>${s.telegram.bot ? esc(s.telegram.bot) : yes(false)}</td></tr>
      <tr><td>Approval chat</td><td>${yes(s.telegram.admin_chat_configured, 'Set')}</td></tr>
      <tr><td>Facebook Page</td><td>${s.facebook.page_id ? `<a href="https://facebook.com/${esc(s.facebook.page_id)}" target="_blank" rel="noopener">${esc(s.facebook.page_id)}</a>` : yes(false)}</td></tr>
      <tr><td>X account</td><td>${s.x.username ? `<a href="https://x.com/${esc(s.x.username)}" target="_blank" rel="noopener">@${esc(s.x.username)}</a>` : yes(false)}</td></tr>
      <tr><td>Timezone</td><td>${esc(s.timezone)}</td></tr>
      <tr><td>n8n errors (24 h)</td><td>${s.n8n_errors_24h == null ? '?' : `<span class="pill ${s.n8n_errors_24h ? 'bad' : 'ok'}">${s.n8n_errors_24h}</span>`}</td></tr>
    </table></div>
    <div class="card"><h3>Workflows</h3><table>
      ${s.workflows.map((w) => `<tr><td>${esc(w.name)}</td><td>${w.active ? '<span class="pill ok">On</span>' : '<span class="pill off">Off</span>'}</td>
        <td class="small muted">${w.last_run ? `${esc(ago(w.last_run))} · ${esc(w.last_status)}` : 'never run'}</td></tr>`).join('')}
    </table></div>
    <div class="card"><h3>Recent X mentions</h3>${mentions.length ? `<table>${mentions.slice(0, 15).map((m) => `<tr>
      <td><b>@${esc(m.author_username)}</b> <span class="muted small">${m.ai_score ?? '?'}/10</span><br><span class="small">${esc(m.text)}</span></td>
      <td>${pill(m.status)}</td></tr>`).join('')}</table>` : '<p class="muted small">No mentions yet.</p>'}</div>`;
};

// ---- Help -----------------------------------------------------------------
TABS.help = async (el) => {
  el.innerHTML = `<div class="card help">
    <h2>How posting works</h2>
    <p>Nothing is published until someone approves it, here or with the buttons in the Telegram approval group.</p>
    <h3>From Telegram (fastest)</h3>
    <ol>
      <li>In the approval group, send your text. Claude rewrites it into an X post and a Facebook post and replies with a preview and buttons.</li>
      <li><b>With an image:</b> send the photo and put your text in the <i>caption</i> (one message). The photo is attached to both posts.</li>
      <li>Start the text with <code>!raw</code> to post it exactly as written, without AI.</li>
      <li>Press ✅ Approve All / 📘 FB Only / 🐦 X Only, or ✏️ Edit and reply with new text (<code>TW:</code> and <code>FB:</code> lines set each separately).</li>
    </ol>
    <p class="small muted">Telegram captions are limited to 1024 characters. For longer text with a photo, use Compose here instead.</p>
    <h3>From this portal</h3>
    <ol>
      <li><b>Compose</b> → paste text, add a photo, tap ✨ to get FB + X versions, edit them.</li>
      <li>📨 <b>Send to Telegram for approval</b> puts it in the queue (and the group), or 🚀 <b>Publish now</b> posts immediately.</li>
      <li><b>Queue</b> shows everything waiting; approve, edit, regenerate or reject there.</li>
    </ol>
    <h3>Daily AI drafts</h3>
    <p>Every day at 08:00 Claude reads the active campaign's website and writes a draft about one item from it. Change the website, tone and topics in <b>Campaigns</b>.</p>
  </div>`;
};

start();
