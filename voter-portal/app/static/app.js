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
