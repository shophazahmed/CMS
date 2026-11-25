// Progressive enhancements only; every page works without JavaScript.
document.addEventListener("submit", (event) => {
  const message = event.target.dataset.confirm;
  if (message && !window.confirm(message)) event.preventDefault();
});

document.addEventListener("change", (event) => {
  if (event.target.matches("[data-autosubmit]")) event.target.form.requestSubmit();
});

document.addEventListener("click", (event) => {
  const link = event.target.closest("[data-back]");
  if (link && document.referrer.startsWith(location.origin) && history.length > 1) {
    event.preventDefault();
    history.back();
  }
});

// Canvassing status on the voter cards: save in the background so the list keeps its place.
document.addEventListener("change", async (event) => {
  const select = event.target.closest("[data-status-select]");
  if (!select) return;
  const form = select.form;
  const card = form.closest(".vcard");
  const state = form.querySelector(".save-state");
  const previous = [...card.classList].find((c) => c.startsWith("st-"));
  state.textContent = "Saving…";
  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    if (previous) card.classList.remove(previous);
    card.classList.add("st-" + data.status);
    const oldKey = previous ? previous.slice(3) : null;
    const bump = (key, delta) => {
      const el = document.querySelector(`[data-count="${key}"]`);
      if (el) el.textContent = Math.max(0, parseInt(el.textContent, 10) + delta);
    };
    if (data.changed && oldKey && oldKey !== data.status) { bump(oldKey, -1); bump(data.status, 1); }
    const who = card.querySelector("[data-who]");
    if (who && data.changed) {
      let line = who.querySelector("[data-who-status]");
      if (!line) { line = document.createElement("span"); line.dataset.whoStatus = ""; who.prepend(line); }
      const name = document.createElement("strong");
      name.textContent = data.by;
      line.replaceChildren(`${data.status === "new" ? "Changed" : data.label} by `, name, ` · ${data.at}`);
    }
    state.textContent = "Saved ✓";
    setTimeout(() => { state.textContent = ""; }, 2000);
  } catch (err) {
    state.textContent = "Not saved, try again";
    form.submit();  // fall back to a normal post (shows the error page if the session expired)
  }
});

// Type-to-filter for long lists (house assignments).
document.addEventListener("input", (event) => {
  const input = event.target.closest("[data-filter-rows]");
  if (!input) return;
  const term = input.value.trim().toLowerCase();
  document.querySelectorAll(`${input.dataset.filterRows} > [data-filter-text]`).forEach((row) => {
    row.hidden = term !== "" && !row.dataset.filterText.includes(term);
  });
});

// Confirm for a single button (forms use data-confirm on the form itself).
document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-confirm-click]");
  if (button && !window.confirm(button.dataset.confirmClick)) event.preventDefault();
});

// Small message at the bottom of the screen.
function toast(text) {
  const el = document.createElement("div");
  el.className = "toast";
  el.setAttribute("role", "status");
  el.textContent = text;
  document.body.append(el);
  setTimeout(() => el.remove(), 3500);
}

// Viber can't pre-fill text for a number: copy the campaign message, then open the chat.
document.addEventListener("click", async (event) => {
  const link = event.target.closest("a[data-copy]");
  if (!link) return;
  event.preventDefault();
  let copied = false;
  try {
    await navigator.clipboard.writeText(link.dataset.copy);
    copied = true;
  } catch (err) { /* no clipboard access: open the chat anyway */ }
  toast(copied ? "Message copied. In Viber, long-press the text box and tap Paste."
               : "Couldn't copy the message. Type it in Viber.");
  setTimeout(() => { window.location.href = link.href; }, 400);
});

// Send the campaign picture with the message through the phone's share sheet (pick WhatsApp,
// then the voter). Only shown on browsers that can share files.
(async () => {
  const button = document.querySelector("[data-share-picture]");
  if (!button || !navigator.canShare) return;
  let file = null;
  try {
    const response = await fetch(button.dataset.sharePicture);
    file = new File([await response.blob()], "campaign.jpg", { type: "image/jpeg" });
  } catch (err) { return; }
  if (!navigator.canShare({ files: [file] })) return;
  button.hidden = false;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(button.dataset.shareText).catch(() => {});
      await navigator.share({ files: [file], text: button.dataset.shareText });
    } catch (err) { /* cancelled */ }
  });
})();

// 100% confirmed toggle on the voter cards ('admin' account only): save in the background.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-confirm-toggle]");
  if (!form) return;
  event.preventDefault();
  const input = form.querySelector("input[name=confirmed]");
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { "X-Requested-With": "fetch" },
      credentials: "same-origin",
    });
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    input.value = data.confirmed ? "0" : "1";
    button.classList.toggle("on", data.confirmed);
    button.setAttribute("aria-pressed", data.confirmed ? "true" : "false");
    button.querySelector("span").textContent = data.confirmed ? "100% confirmed" : "Mark confirmed";
    toast(`${data.confirmed ? "Confirmed" : "Removed"} · ${data.metrics.confirmed} of ${data.metrics.total} (${data.metrics.pct}%)`);
  } catch (err) {
    form.submit();
  } finally {
    button.disabled = false;
  }
});

// Bulk assign on Search: tick all on the page / clear, with a live count.
(() => {
  const tools = document.querySelector("[data-pick-tools]");
  if (!tools) return;
  const boxes = () => [...document.querySelectorAll("input.pick")];
  const count = tools.querySelector("[data-pick-count]");
  const scope = document.querySelector("#bulk-assign select[name=scope]");
  const update = () => { count.textContent = boxes().filter((b) => b.checked).length; };
  tools.hidden = false;
  tools.addEventListener("click", (event) => {
    const button = event.target.closest("[data-pick]");
    if (!button) return;
    boxes().forEach((b) => { b.checked = button.dataset.pick === "page"; });
    if (scope && button.dataset.pick === "page") scope.value = "selected";
    update();
  });
  document.addEventListener("change", (event) => {
    if (event.target.matches("input.pick")) { if (scope) scope.value = "selected"; update(); }
  });
  update();
})();

// PDF lists: on phones, open the share sheet with the file (pick WhatsApp, then the agent).
// Anywhere sharing files isn't possible, the link simply downloads the PDF.
document.addEventListener("click", async (event) => {
  const link = event.target.closest("[data-share-pdf]");
  if (!link || !navigator.canShare || !window.File) return;
  if (!navigator.canShare({ files: [new File([""], "x.pdf", { type: "application/pdf" })] })) return;
  event.preventDefault();
  if (link._file) {  // second tap: the PDF is ready, a fresh tap always opens the share sheet
    try { await navigator.share({ files: [link._file], title: link._file.name }); } catch (err) { /* cancelled */ }
    return;
  }
  const label = link.innerHTML;
  link.textContent = "Preparing…";
  try {
    const response = await fetch(link.href, { credentials: "same-origin" });
    if (!response.ok || !(response.headers.get("Content-Type") || "").includes("pdf")) {
      link.innerHTML = label;
      window.location = link.href;  // shows the error (e.g. too many voters) on the normal page
      return;
    }
    const name = (/filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "") || [])[1] || "voters.pdf";
    const file = new File([await response.blob()], name, { type: "application/pdf" });
    link.innerHTML = label;
    try {
      await navigator.share({ files: [file], title: name });
    } catch (err) {
      if (err && err.name === "NotAllowedError") {
        // iPhone: preparing took too long for this tap. Keep the file; the next tap sends it.
        link._file = file;
        link.textContent = "Tap to send PDF ›";
        link.classList.add("primary");
        setTimeout(() => { link._file = null; link.innerHTML = label; link.classList.remove("primary"); }, 120000);
      } else if (err && err.name !== "AbortError") {
        window.location = link.href;
      }
    }
  } catch (err) {
    link.innerHTML = label;
    window.location = link.href;
  }
});

// Share voter cards on WhatsApp (owner only). Step 1 prepares the cards; step 2 is a tap on
// "Send on WhatsApp", because phones only open the share sheet straight after a tap.
document.addEventListener("change", (event) => {
  const to = event.target.closest("[data-share-to]");
  if (!to) return;
  const form = to.form;
  form.querySelector("[data-share-number]").hidden = to.value !== "number";
  form.querySelector("[data-share-assign]").hidden = !/^\d+$/.test(to.value);
});

document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-share-form]");
  if (!form) return;
  event.preventDefault();
  const status = form.querySelector("[data-share-status]");
  const button = form.querySelector("button.primary");
  const data = new FormData(form);
  if (!data.getAll("ids").length) {
    document.querySelectorAll("input.pick:checked").forEach((b) => data.append("ids", b.value));
  }
  if (!data.getAll("ids").length) { status.textContent = "Tick the voters to share first."; return; }
  status.textContent = "Preparing…";
  let reply;
  try {
    const response = await fetch(form.action, { method: "POST", body: data, credentials: "same-origin",
                                                headers: { "X-Requested-With": "fetch" } });
    reply = await response.json();
    if (!reply.ok) { status.textContent = reply.error || "Couldn't share."; return; }
  } catch (err) { status.textContent = "Couldn't reach the portal. Try again."; return; }
  const who = reply.to ? ` for ${reply.to}` : "";
  const assigned = reply.assigned ? ` ${reply.assigned} assigned to ${reply.to}.` : "";
  if (reply.mode === "text") {
    status.textContent = `Opening WhatsApp${who}…${assigned}`;
    window.open(reply.wa_url, "_blank", "noopener");
    return;
  }
  const files = [];
  for (const [i, url] of reply.cards.entries()) {
    const blob = await (await fetch(url, { credentials: "same-origin" })).blob();
    files.push(new File([blob], reply.names[i], { type: "image/png" }));
  }
  if (navigator.canShare && navigator.canShare({ files })) {
    status.textContent = `Ready: ${files.length} card${files.length > 1 ? "s" : ""}${who}.${assigned}` +
      (reply.to ? ` In WhatsApp, pick ${reply.to}.` : "");
    const send = document.createElement("button");
    send.type = "button";
    send.className = "button primary";
    send.textContent = "Send on WhatsApp";
    send.addEventListener("click", async () => {
      try { await navigator.share({ files }); status.textContent = "Shared."; send.remove(); }
      catch (err) { if (err.name !== "AbortError") status.textContent = "Sharing didn't work; try 'Text only'."; }
    });
    button.after(send);
    return;
  }
  // A PC: save the cards, then open WhatsApp (Web) at the chat so they can be attached
  files.forEach((file) => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(file);
    a.download = file.name;
    a.click();
  });
  status.textContent = `Saved ${files.length} card${files.length > 1 ? "s" : ""}${who}. Attach ${files.length > 1 ? "them" : "it"} in WhatsApp.${assigned}`;
  if (reply.direct) window.open(reply.wa_url, "_blank", "noopener");
});

// Coming back from a voter: return to that voter's card in the list, not the top of the page.
// Search pages aren't kept in the phone's memory (privacy), so the browser reloads them on Back;
// we remember only which card was opened, in this tab, and scroll to it.
(() => {
  const KEY = "lastVoterCard";
  const here = () => location.pathname + location.search;
  const store = (value) => { try { sessionStorage.setItem(KEY, JSON.stringify(value)); } catch (e) { /* private mode */ } };
  const recall = () => { try { return JSON.parse(sessionStorage.getItem(KEY) || "null"); } catch (e) { return null; } };
  document.addEventListener("click", (event) => {
    const card = event.target.closest(".vcard[id^='v-']");
    if (card && event.target.closest("a")) store({ page: here(), id: card.id });
  });
  const show = (id) => {
    const card = document.getElementById(id);
    if (!card) return false;
    card.scrollIntoView({ block: "center" });
    card.classList.add("just-viewed");
    setTimeout(() => card.classList.remove("just-viewed"), 2500);
    return true;
  };
  const restore = () => {
    if (location.hash.startsWith("#v-")) { show(location.hash.slice(1)); return; }
    const last = recall();
    if (last && last.page === here()) show(last.id);
  };
  window.addEventListener("pageshow", restore);
})();

// Copy a link to the clipboard (Agents page: dashboard links).
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const label = button.textContent;
  try { await navigator.clipboard.writeText(button.dataset.copy); button.textContent = "Copied ✓"; }
  catch (err) { window.prompt("Copy this link:", button.dataset.copy); }
  setTimeout(() => { button.textContent = label; }, 2000);
});

// Island map: zoom buttons, jump to the marked house, and (admins) tap to set a house's spot.
(() => {
  const frame = document.querySelector("[data-map-frame]");
  if (!frame) return;
  const svg = frame.querySelector("[data-map-svg]");
  const levels = ["z1", "z2", "z4", "z8", "z16"];
  let level = 1;
  const ring = svg.querySelector("[data-map-ring]");
  const centerOn = (el) => {
    const f = frame.getBoundingClientRect(), r = el.getBoundingClientRect();
    frame.scrollLeft += r.left + r.width / 2 - (f.left + f.width / 2);
    frame.scrollTop += r.top + r.height / 2 - (f.top + f.height / 2);
  };
  const setLevel = (next, keep) => {
    // keep the point in the middle of the frame in the middle after zooming
    const fx = (frame.scrollLeft + frame.clientWidth / 2) / frame.scrollWidth;
    const fy = (frame.scrollTop + frame.clientHeight / 2) / frame.scrollHeight;
    level = Math.max(0, Math.min(levels.length - 1, next));
    svg.classList.remove(...levels);
    svg.classList.add(levels[level]);
    if (keep !== false) {
      frame.scrollLeft = fx * frame.scrollWidth - frame.clientWidth / 2;
      frame.scrollTop = fy * frame.scrollHeight - frame.clientHeight / 2;
    }
  };
  document.querySelectorAll("[data-zoom]").forEach((b) =>
    b.addEventListener("click", () => setLevel(level + parseInt(b.dataset.zoom, 10))));
  const center = document.querySelector("[data-map-center]");
  if (ring) {
    setLevel(2, false);
    requestAnimationFrame(() => centerOn(ring));
    if (center) center.addEventListener("click", () => centerOn(ring));
  }
  const pending = svg.querySelector("[data-map-pending]");
  const form = document.querySelector("[data-map-set]");
  if (pending && form) {
    // Admins: "Wrong spot? Set it" switches this page into setting mode (no reload); Cancel leaves it.
    let setting = svg.dataset.setting === "1";
    const show = (on) => {
      setting = on;
      svg.classList.toggle("settable", on);
      document.querySelectorAll("[data-map-normal]").forEach((el) => { el.hidden = on; });
      document.querySelectorAll("[data-map-setting]").forEach((el) => { el.hidden = !on; });
      if (!on) {
        form.hidden = true;
        pending.setAttribute("cx", "-100");
        pending.setAttribute("cy", "-100");
      }
    };
    show(setting);
    const start = document.querySelector("[data-map-setmode]");
    if (start) start.addEventListener("click", (event) => { event.preventDefault(); show(true); });
    form.querySelector("[data-map-cancel]").addEventListener("click", () => show(false));
    svg.addEventListener("click", (event) => {
      if (!setting) return;
      const pt = svg.createSVGPoint();
      pt.x = event.clientX;
      pt.y = event.clientY;
      const p = pt.matrixTransform(svg.getScreenCTM().inverse());
      pending.setAttribute("cx", p.x.toFixed(2));
      pending.setAttribute("cy", p.y.toFixed(2));
      form.elements.x.value = p.x.toFixed(2);
      form.elements.y.value = p.y.toFixed(2);
      form.hidden = false;
    });
  }
})();

// Pledge sheet: tap a cell to cycle Yes -> Maybe -> No -> blank; saves in the background.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-pledge-form]");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector("button");
  const input = form.elements.value;
  const next = { "": "yes", yes: "maybe", maybe: "no", no: "" };
  button.disabled = true;
  try {
    const response = await fetch(form.action, {
      method: "POST", body: new FormData(form),
      headers: { "X-Requested-With": "fetch" }, credentials: "same-origin",
    });
    if (response.status === 409) {  // over the vote limit: nothing saved
      const refused = await response.json();
      toast(refused.error);
      return;
    }
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    button.className = "pl pl-" + (data.value || "blank");
    button.textContent = data.label || "·";
    input.value = next[data.value];
  } catch (err) {
    form.submit();
  } finally {
    button.disabled = false;
  }
});

// No-show cells on the pledge sheet: tap to mark / unmark, saved in the background.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-noshow-form]");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch(form.action, {
      method: "POST", body: new FormData(form),
      headers: { "X-Requested-With": "fetch" }, credentials: "same-origin",
    });
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    button.className = "pl " + (data.noshow ? "pl-ns" : "pl-blank");
    button.textContent = data.noshow ? "No-show" : "·";
    form.elements.value.value = data.noshow ? "0" : "1";
  } catch (err) {
    form.submit();
  } finally {
    button.disabled = false;
  }
});

// On / off cells (super pledge ★, a super voter's votes): tap to toggle, saved in the background.
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-toggle-form]");
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    const response = await fetch(form.action, {
      method: "POST", body: new FormData(form),
      headers: { "X-Requested-With": "fetch" }, credentials: "same-origin",
    });
    if (response.status === 409) {
      toast((await response.json()).error);
      return;
    }
    if (!response.ok) throw new Error(response.status);
    const data = await response.json();
    button.className = data.on ? form.dataset.onClass : "pl pl-blank";
    button.textContent = data.on ? form.dataset.on : form.dataset.off;
    form.elements.value.value = data.on ? "0" : "1";
  } catch (err) {
    form.submit();
  } finally {
    button.disabled = false;
  }
});

// ---- Election-day War Room -------------------------------------------------------------------
// Marks go through an outbox kept on the phone: if the signal drops they wait and are sent when it
// comes back (each has a key, so a resend never counts twice). Pages ask for changes every few
// seconds; voters who voted drop off lists straight away.
(() => {
  const page = document.querySelector("[data-war-poll]");
  if (!page) return;
  const OUTBOX = "war-outbox";
  const sync = page.querySelector("[data-war-sync]");
  const syncText = page.querySelector("[data-war-sync-text]");
  const csrf = (document.querySelector("input[name=csrf_token]") || {}).value
    || (document.getElementById("war-card") || { dataset: {} }).dataset.csrf || "";
  let since = Number(page.dataset.since || 0);
  let online = true;

  const load = () => { try { return JSON.parse(localStorage.getItem(OUTBOX) || "[]"); } catch (e) { return []; } };
  const save = (list) => { try { localStorage.setItem(OUTBOX, JSON.stringify(list)); } catch (e) { /* private mode */ } };
  const key = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, "").slice(0, 32);

  function status() {
    const waiting = load().length;
    sync.classList.toggle("off", !online);
    sync.classList.toggle("wait", waiting > 0);
    syncText.textContent = !online ? (waiting ? `No signal · ${waiting} waiting to send` : "No signal")
      : waiting ? `Sending ${waiting}…` : "Live";
  }

  function gone(nid) {  // a voter voted: take them off the list
    page.querySelectorAll(`[data-nid="${CSS.escape(nid)}"].war-item`).forEach((el) => {
      el.classList.add("war-gone");
      setTimeout(() => el.remove(), 600);
    });
    const left = page.querySelector("[data-war-left]");
    if (left) left.textContent = String(page.querySelectorAll(".war-item:not(.war-gone)").length);
  }

  async function post(url, fields) {
    const body = new URLSearchParams(fields);
    const r = await fetch(url, { method: "POST", body, credentials: "same-origin", headers: { "X-Requested-With": "fetch" } });
    return r;
  }

  async function flush() {
    let list = load();
    while (list.length) {
      const item = list[0];
      try {
        const r = await post(item.url, item.fields);
        if (r.status >= 500) throw new Error(r.status);
        if (!r.ok) {
          const data = await r.json().catch(() => ({}));
          toast(data.error || "Couldn't save one mark (" + r.status + ").");
        }
        list = load().slice(1);
        save(list);
        online = true;
      } catch (e) {
        online = false;
        break;
      }
    }
    status();
  }

  window.warQueue = (url, fields) => queue(url, fields);
  function queue(url, fields) {
    fields.key = fields.key || key();
    const list = load();
    list.push({ url, fields });
    save(list);
    status();
    return flush();
  }

  // Mark voted / undo (tier 1 recent list, tier 2 list)
  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-war-mark]");
    if (!form) return;
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(form));
    const item = form.closest(".war-item, li");
    if (fields.value === "1") gone(fields.national_id); else if (item) item.remove();
    queue(form.getAttribute("action"), fields);
  });

  // Call outcomes (tier 3)
  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-war-call]");
    if (!form) return;
    event.preventDefault();
    const fields = Object.fromEntries(new FormData(form));
    const item = form.closest(".war-item");
    const tag = item && item.querySelector("[data-oc]");
    if (tag) { tag.hidden = false; tag.textContent = form.querySelector("button").textContent; }
    if (item) item.className = item.className.replace(/\boc-\w+/g, "").trim() + " oc-" + fields.outcome;
    queue(form.getAttribute("action"), fields);
  });

  // Tier 1 lookup: one voter by number or ID, check the face, tap Voted
  const lookup = document.querySelector("[data-war-lookup]");
  const slot = document.getElementById("war-card");
  if (lookup && slot) {
    const input = lookup.querySelector("input[name=q]");
    lookup.addEventListener("submit", async (event) => {
      event.preventDefault();
      slot.replaceChildren();
      const params = new URLSearchParams(new FormData(lookup));
      let data;
      try {
        const r = await fetch(lookup.action + "?" + params, { credentials: "same-origin" });
        data = await r.json().catch(() => ({ ok: false, error: r.status === 429 ? "Too many lookups; wait a minute." : "Not found." }));
      } catch (e) {
        online = false; status();
        toast("No signal. Type the number again when it's back, or write it down.");
        return;
      }
      online = true; status();
      if (!data.ok) { slot.append(el("p", "flash flash-error", data.error)); input.select(); return; }
      slot.append(card(data.voter));
    });

    function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }

    function card(v) {
      const box = el("div", "card war-card" + (v.voted ? " done" : ""));
      const head = el("div", "war-card-head");
      if (v.photo) { const img = el("img", "avatar lg"); img.src = v.photo; img.alt = ""; head.append(img); }
      else head.append(el("span", "avatar lg", (v.name || "?").slice(0, 1)));
      const who = el("div");
      who.append(el("p", "war-no", v.list_no != null ? "No. " + v.list_no : ""), el("h2", null, v.name),
        el("p", "muted", [v.national_id, v.sex, v.age, v.house].filter(Boolean).join(" · ")));
      head.append(who);
      box.append(head);
      if (v.voted) { box.append(el("p", "war-already", "Already marked voted at " + v.voted_at)); return box; }
      const btn = el("button", "primary big wide war-voted-btn", "✓ Voted");
      btn.type = "button";
      btn.addEventListener("click", () => {
        const fields = { csrf_token: csrf, national_id: v.national_id, value: "1" };
        if (slot.dataset.box) fields.box = slot.dataset.box;
        queue(slot.dataset.markUrl, fields);
        const recent = document.getElementById("war-recent");
        if (recent) {
          const empty = recent.querySelector("[data-empty]"); if (empty) empty.remove();
          const li = el("li"); li.dataset.nid = v.national_id;
          li.append(el("span", "mono", v.list_no != null ? String(v.list_no) : ""), " ", el("strong", null, v.name), " ",
            el("span", "muted small-text", new Date().toTimeString().slice(0, 5)));
          recent.prepend(li);
        }
        const voted = page.querySelector("[data-war-voted]");
        if (voted) voted.textContent = String(Number(voted.textContent) + 1);
        slot.replaceChildren(el("p", "flash flash-success", "✓ " + v.name + " marked voted."));
        input.value = ""; input.focus();
      });
      box.append(btn);
      return box;
    }
  }

  // Changes since the last check
  async function poll() {
    try {
      const sep = page.dataset.url.includes("?") ? "&" : "?";
      const r = await fetch(page.dataset.url + sep + "since=" + since, { credentials: "same-origin" });
      if (r.status === 423 || r.status === 403) { location.reload(); return; }
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      since = data.cursor;
      (data.voted || []).forEach(gone);
      document.dispatchEvent(new CustomEvent("war:changes", { detail: data }));
      if (data.stats) {
        Object.entries(data.stats).forEach(([k, v]) => {
          page.querySelectorAll(`[data-k="${k}"]`).forEach((e) => { e.textContent = String(v); });
        });
      }
      online = true;
    } catch (e) {
      online = false;
    }
    status();
    if (online && load().length) flush();
  }

  window.addEventListener("online", () => { online = true; flush(); poll(); });
  window.addEventListener("offline", () => { online = false; status(); });
  status();
  flush();
  setInterval(poll, page.dataset.mode === "stats" ? 15000 : 10000);
})();

// War Room transport buttons: save, then show the fresh list; any page with a transport list
// reloads when the queue changes (unless the user is typing).
(() => {
  document.addEventListener("submit", async (event) => {
    const form = event.target.closest("[data-war-transport]");
    if (!form) return;
    event.preventDefault();
    const button = form.querySelector("button");
    button.disabled = true;
    try {
      const r = await fetch(form.getAttribute("action"), { method: "POST", body: new FormData(form), credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" } });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) toast(data.error || "Couldn't save (" + r.status + ").");
      location.reload();
    } catch (e) {
      toast("No signal. Try again in a moment.");
      button.disabled = false;
    }
  });
  const page = document.querySelector("[data-trev]");
  if (page) {
    const url = page.dataset.url;
    setInterval(async () => {
      if (document.activeElement && document.activeElement.matches("input, textarea, select")) return;
      try {
        const r = await fetch(url + (url.includes("?") ? "&" : "?") + "since=" + (page.dataset.since || 0), { credentials: "same-origin" });
        const data = await r.json();
        if (data.trev && data.trev !== page.dataset.trev) location.reload();
      } catch (e) { /* no signal: try again later */ }
    }, 15000);
  }
  document.querySelectorAll("[data-print]").forEach((b) => b.addEventListener("click", () => window.print()));
})();

// Assign agents sheet: a row's agent saves as soon as it's picked; tick-all and the ticked count.
(() => {
  document.addEventListener("change", async (event) => {
    const select = event.target.closest("[data-agent-form] select");
    if (!select) return;
    const form = select.form;
    select.disabled = true;
    try {
      const body = new FormData(form);
      body.set("agent", select.value);
      const r = await fetch(form.getAttribute("action"), { method: "POST", body, credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" } });
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      select.classList.toggle("set", select.value !== "");
      toast(data.agent ? `Saved: ${data.agent}` : "Saved: back with the house agent");
    } catch (e) {
      form.submit();
    } finally {
      select.disabled = false;
    }
  });
  const all = document.querySelector("[data-tick-all]");
  const count = document.querySelector("[data-ticked]");
  if (!all || !count) return;
  const boxes = () => [...document.querySelectorAll('input[type=checkbox][name=ids]')];
  const update = () => { count.textContent = String(boxes().filter((b) => b.checked).length); };
  all.addEventListener("change", () => { boxes().forEach((b) => { b.checked = all.checked; }); update(); });
  document.addEventListener("change", (e) => { if (e.target.matches('input[name=ids]')) update(); });
})();

// War Room tier 2: tap what the voter said; saved in the background (resent if the signal drops).
document.addEventListener("submit", async (event) => {
  const form = event.target.closest("[data-war-contact]");
  if (!form) return;
  event.preventDefault();
  const item = form.closest(".war-item");
  const button = form.querySelector("button");
  const was = button.classList.contains("on");
  const body = new FormData(form);
  if (was) body.set("status", "");  // tapping the chosen one again clears it
  item.querySelectorAll(".cs-btn").forEach((b) => { b.classList.remove("on"); b.setAttribute("aria-pressed", "false"); });
  try {
    const r = await fetch(form.getAttribute("action"), { method: "POST", body, credentials: "same-origin",
      headers: { "X-Requested-With": "fetch" } });
    if (!r.ok) throw new Error(r.status);
    const data = await r.json();
    if (data.status) { button.classList.add("on"); button.setAttribute("aria-pressed", "true"); }
    item.className = item.className.replace(/\bcs-\w+/g, "").trim() + " cs-" + (data.status || "none");
    const tag = item.querySelector("[data-cs-tag]");
    if (tag) tag.textContent = data.status ? data.label + " · " + new Date().toTimeString().slice(0, 5) : "Not reached yet";
    if (data.status === "transport") toast("Added to the transport queue.");
  } catch (e) {
    if (was) button.classList.add("on");
    toast("Not saved: no signal. Tap again when it's back.");
  }
});
document.addEventListener("focusin", (e) => { if (e.target.matches("[data-select-all]")) e.target.select(); });

// Left menu: a drawer on phones and tablets (Menu button), always open on big screens.
(() => {
  const body = document.body;
  const openers = document.querySelectorAll("[data-nav-open]");
  if (!openers.length) return;
  const set = (open) => {
    body.classList.toggle("nav-open", open);
    openers.forEach((b) => b.setAttribute("aria-expanded", open ? "true" : "false"));
    if (open) { const first = document.querySelector(".sidebar .side-link"); if (first) first.focus({ preventScroll: true }); }
  };
  openers.forEach((b) => b.addEventListener("click", () => set(!body.classList.contains("nav-open"))));
  document.querySelectorAll("[data-nav-close]").forEach((b) => b.addEventListener("click", () => set(false)));
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && body.classList.contains("nav-open")) set(false); });
  // keep the current page in view in a long menu
  const here = document.querySelector(".side-link[aria-current]");
  if (here) here.scrollIntoView({ block: "nearest" });
})();

// Pin / unpin the left menu on big screens; remembered in a cookie so the page opens the same way.
(() => {
  const pin = document.querySelector("[data-nav-pin]");
  if (!pin) return;
  pin.addEventListener("click", () => {
    const rail = !document.body.classList.contains("nav-rail");
    document.body.classList.toggle("nav-rail", rail);
    pin.setAttribute("aria-pressed", rail ? "false" : "true");
    pin.title = rail ? "Pin the menu open" : "Unpin: shrink the menu to icons";
    document.cookie = "nav=" + (rail ? "rail" : "pinned") + "; path=/; max-age=31536000; SameSite=Lax; Secure";
    pin.blur();
  });
})();

// Vote count: tap each ballot; taps queue on the phone and are sent in order (each with a key).
(() => {
  const page = document.querySelector("[data-count]");
  if (!page || page.dataset.final) return;
  const OUTBOX = "count-outbox";
  const sync = page.querySelector("[data-war-sync]");
  const syncText = page.querySelector("[data-war-sync-text]");
  const load = () => { try { return JSON.parse(localStorage.getItem(OUTBOX) || "[]"); } catch (e) { return []; } };
  const save = (l) => { try { localStorage.setItem(OUTBOX, JSON.stringify(l)); } catch (e) { /* private mode */ } };
  const key = () => (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random()).replace(/-/g, "").slice(0, 32);
  let busy = false;
  const status = (online) => {
    const waiting = load().length;
    sync.classList.toggle("off", !online);
    sync.classList.toggle("wait", waiting > 0);
    syncText.textContent = !online ? `No signal · ${waiting} waiting` : waiting ? `Sending ${waiting}…` : "Live";
  };
  const show = (data) => {
    page.querySelector("[data-c-ballots]").textContent = data.ballots;
    const inv = page.querySelector("[data-c-invalid]"); if (inv) inv.textContent = data.invalid;
    page.querySelectorAll("[data-v]").forEach((e) => { e.textContent = data.votes[e.dataset.v] || 0; });
    page.querySelectorAll("[data-n]").forEach((e) => { e.textContent = data.tally[e.dataset.n] || 0; });
  };
  async function flush() {
    if (busy) return;
    busy = true;
    let list = load();
    while (list.length) {
      const body = new URLSearchParams(list[0]);
      try {
        const r = await fetch(page.dataset.url, { method: "POST", body, credentials: "same-origin",
          headers: { "X-Requested-With": "fetch" } });
        if (r.status >= 500) throw new Error(r.status);
        const data = await r.json().catch(() => ({}));
        if (!r.ok) toast(data.error || "Not saved (" + r.status + ").");
        else if (!data.repeat) show(data);
        list = load().slice(1); save(list);
      } catch (e) { busy = false; status(false); return; }
    }
    busy = false;
    status(true);
  }
  const bump = (combo, d) => {
    const n = page.querySelector(`[data-n="${CSS.escape(combo)}"]`);
    if (n) n.textContent = Math.max(0, Number(n.textContent) + d);
    const b = page.querySelector("[data-c-ballots]"); b.textContent = Math.max(0, Number(b.textContent) + d);
  };
  let last = [];
  const send = (fields) => {
    fields.csrf_token = page.dataset.csrf; fields.key = key();
    if (page.dataset.box) fields.box = page.dataset.box;
    const list = load(); list.push(fields); save(list); status(navigator.onLine); flush();
  };
  page.querySelectorAll("[data-combo]").forEach((btn) => btn.addEventListener("click", () => {
    bump(btn.dataset.combo, 1); last.push(btn.dataset.combo);
    btn.classList.add("hit"); setTimeout(() => btn.classList.remove("hit"), 180);
    send({ combo: btn.dataset.combo });
  }));
  const undo = page.querySelector("[data-count-undo]");
  if (undo) undo.addEventListener("click", () => {
    const combo = last.pop();
    if (combo) bump(combo, -1);
    send({ undo: "1" });
  });
  window.addEventListener("online", flush);
  status(navigator.onLine);
  flush();
  setInterval(flush, 10000);
})();

// Pages that refresh themselves (vote count results), unless the user is typing.
(() => {
  const el = document.querySelector("[data-autoreload]");
  if (!el) return;
  setInterval(() => {
    if (document.activeElement && document.activeElement.matches("input, select, textarea")) return;
    location.reload();
  }, Number(el.dataset.autoreload) * 1000);
})();

// Box agent's voter list: voted / not voted, live; search; mark voted from the list.
(() => {
  const list = document.querySelector("[data-box-list]");
  if (!list) return;
  const rows = [...list.querySelectorAll(".bl-row")];
  const votedEl = list.querySelector("[data-bl-voted]");
  const leftEl = list.querySelector("[data-bl-left]");
  const search = list.querySelector("[data-bl-search]");
  const page = document.querySelector("[data-war-poll]");
  const card = document.getElementById("war-card");
  let show = "all";
  const counts = () => {
    const v = rows.filter((r) => r.classList.contains("is-voted")).length;
    votedEl.textContent = v; leftEl.textContent = rows.length - v;
  };
  const filter = () => {
    const q = (search.value || "").trim().toLowerCase();
    rows.forEach((r) => {
      const voted = r.classList.contains("is-voted");
      const ok = (show === "all" || (show === "yes") === voted) && (!q || r.dataset.text.includes(q));
      r.hidden = !ok;
    });
  };
  const setVoted = (nid, on, when) => {
    const r = list.querySelector(`.bl-row[data-nid="${CSS.escape(nid)}"]`);
    if (!r) return;
    r.classList.toggle("is-voted", on);
    r.querySelector("[data-bl-state]").textContent = on ? "✓ Voted " + (when || new Date().toTimeString().slice(0, 5)) : "Not yet";
    r.querySelector("[data-bl-mark]").hidden = on;
  };
  list.querySelectorAll("[data-bl-show]").forEach((b) => b.addEventListener("click", () => {
    show = b.dataset.blShow;
    list.querySelectorAll("[data-bl-show]").forEach((x) => x.classList.toggle("on", x === b));
    filter();
  }));
  search.addEventListener("input", filter);
  list.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-bl-mark]");
    if (!btn) return;
    const r = btn.closest(".bl-row");
    const name = r.querySelector("strong").textContent;
    if (!confirm(`Mark ${name} as voted?`)) return;
    const fields = { csrf_token: (card && card.dataset.csrf) || (document.querySelector("input[name=csrf_token]") || {}).value,
                     national_id: r.dataset.nid, value: "1" };
    if (card && card.dataset.box) fields.box = card.dataset.box;
    setVoted(r.dataset.nid, true); counts(); filter();
    if (window.warQueue) window.warQueue(list.dataset.markUrl, fields);
  });
  document.addEventListener("war:changes", (e) => {
    (e.detail.voted || []).forEach((nid) => setVoted(nid, true));
    (e.detail.undone || []).forEach((nid) => setVoted(nid, false));
    counts(); filter();
  });
  // a voter marked from the lookup card above
  if (card) card.addEventListener("click", (e) => {
    if (e.target.closest(".war-voted-btn")) setTimeout(() => {
      const voted = document.querySelectorAll(".war-recent li[data-nid]");
      if (voted[0]) { setVoted(voted[0].dataset.nid, true); counts(); filter(); }
    }, 50);
  });
})();

// Pledge / super sheets: the Connector field saves when it's left (or Enter is pressed).
(() => {
  const save = async (input) => {
    const form = input.form;
    if (input.value === input.defaultValue) return;
    try {
      const r = await fetch(form.getAttribute("action"), { method: "POST", body: new FormData(form), credentials: "same-origin",
        headers: { "X-Requested-With": "fetch" } });
      if (!r.ok) throw new Error(r.status);
      const data = await r.json();
      input.value = data.connector;
      input.defaultValue = data.connector;
      input.classList.add("saved");
      toast(data.connector ? `Saved: ${data.connector}` : "Connector cleared");
    } catch (e) {
      toast("Couldn't save the connector. Try again.");
    }
  };
  document.addEventListener("change", (event) => {
    const input = event.target.closest("[data-connector-form] input[name=connector]");
    if (input) save(input);
  });
  document.addEventListener("submit", (event) => {
    const form = event.target.closest("[data-connector-form]");
    if (!form) return;
    event.preventDefault();
    save(form.elements.connector);
  });
})();
