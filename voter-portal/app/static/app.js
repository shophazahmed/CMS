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
