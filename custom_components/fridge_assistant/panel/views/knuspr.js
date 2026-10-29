/* Knuspr order review UI. Orders are staged via fridge_assistant.stage_knuspr_orders. */
import { esc } from "../lib/format.js";
import { openModal } from "../lib/surface.js";

export function openKnuspr(panel) {
  const orders = panel._state.knuspr_orders || [];
  const imported = new Set(panel._state.knuspr_imported || []);
  const locations = panel._state.locations || [];
  const matched = new Map();
  const texts = panel._lang() === "nl"
    ? {title:"Knuspr-bestellingen", empty:"Nog geen bestellingen geladen. Verbind eerst je Knuspr-account en haal de bestellingen op.", all:"Alles selecteren", import:"Geselecteerde artikelen toevoegen", done:"Al toegevoegd", choose:"Opslaglocatie", loading:"Overgenomen"}
    : {title:"Knuspr orders", empty:"Noch keine Bestellungen geladen. Verbinde zuerst deinen Knuspr-Account per Apple-Login, danach kannst du abrufen.", all:"Alle auswählen", import:"Ausgewählte Artikel einbuchen", done:"Bereits eingebucht", choose:"Lagerort", loading:"Eingebucht"};
  const { modal, close } = openModal(panel, `<h2>${texts.title}</h2><div class="kn-controls"><button class="btn ghost" id="kn-auth-start" type="button">Mit Apple / Knuspr verbinden</button><button class="btn ghost" id="kn-auth-status" type="button">Loginstatus prüfen</button><button class="btn primary" id="kn-fetch" type="button">Letzte 3 Bestellungen abrufen</button></div><label class="kn-auto-option"><input id="kn-auto" type="checkbox"> Eindeutig zuordenbare Artikel beim Abruf automatisch einbuchen</label><p id="kn-status" role="status"></p>
    ${!orders.length ? `<p>${texts.empty}</p>` : `
    <label class="field">Order <select id="kn-order">
      ${orders.map(o => `<option value="${esc(o.id)}">${esc(o.order_time || o.id)} · ${o.items.length} items</option>`).join("")}
    </select></label><div id="kn-items"></div>
    <button class="btn ghost" id="kn-all" type="button">${texts.all}</button>
    <button class="btn primary" id="kn-import" type="button">${texts.import}</button>`}`);
  const status = modal.querySelector("#kn-status");
  const authBox = document.createElement("div");
  authBox.className = "kn-auth-box";
  authBox.innerHTML = '<p id="kn-auth-message" role="status"></p><div id="kn-auth-link"></div><label>Weiterleitungs-URL nach Apple-Login<input id="kn-auth-callback" type="url" placeholder="http://localhost:8765/callback?code=…"></label><button id="kn-auth-submit" class="btn ghost" type="button">URL übermitteln</button>';
  modal.querySelector(".kn-controls").after(authBox);
  authBox.hidden = true;
  const authMessage = authBox.querySelector("#kn-auth-message");
  const authLink = authBox.querySelector("#kn-auth-link");
  const showStatus = async () => {
    const result = await panel._call("knuspr_auth_status");
    authBox.hidden = !(result.login_url || result.awaiting_callback || result.running);
    if (result.login_url) {
      // Never render the authorization URL into HTML, where it could be interpreted.
      authLink.replaceChildren();
      const link = document.createElement("a");
      link.href = result.login_url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.textContent = "Knuspr-Login im Browser öffnen";
      authLink.appendChild(link);
      authMessage.textContent = "Nach dem Login die komplette localhost-Weiterleitungs-URL kopieren.";
    } else if (result.ready) {
      authLink.replaceChildren();
      authMessage.textContent = "Knuspr erfolgreich verbunden.";
      authBox.hidden = true;
    } else if (result.running) {
      authMessage.textContent = "Anmeldung läuft. Erneut Loginstatus prüfen.";
    } else {
      authMessage.textContent = result.error || "Noch nicht verbunden.";
    }
  };
  const checkLoginAfterStart = async () => {
    for (let attempt = 0; attempt < 12; attempt++) {
      await new Promise(resolve => setTimeout(resolve, 500));
      const current = await panel._call("knuspr_auth_status");
      if (current.login_url || current.error || current.ready || !current.running) {
        await showStatus();
        return;
      }
    }
    await showStatus();
  };
  modal.querySelector("#kn-auth-start").addEventListener("click", async ev => {
    ev.currentTarget.disabled = true;
    try {
      authBox.hidden = false;
      await panel._call("knuspr_auth_start");
      await checkLoginAfterStart();
    } catch (err) {
      authMessage.textContent = String(err.message || err);
    } finally { ev.currentTarget.disabled = false; }
  });
  modal.querySelector("#kn-auth-status").addEventListener("click", async () => {
    try { await showStatus(); } catch (err) { authMessage.textContent = String(err.message || err); }
  });
  authBox.querySelector("#kn-auth-submit").addEventListener("click", async ev => {
    ev.currentTarget.disabled = true;
    try {
      const url = authBox.querySelector("#kn-auth-callback").value.trim();
      await panel._call("knuspr_auth_callback", {callback_url: url});
      authBox.querySelector("#kn-auth-callback").value = "";
      authMessage.textContent = "Rückruf übermittelt. Verbinde…";
      await checkLoginAfterStart();
    } catch (err) { authMessage.textContent = String(err.message || err); }
    finally { ev.currentTarget.disabled = false; }
  });
  modal.querySelector("#kn-fetch").addEventListener("click", async ev => {
    ev.currentTarget.disabled = true;
    status.textContent = "Knuspr wird abgefragt…";
    try {
      const result = await panel._call("knuspr_fetch", {auto_import: modal.querySelector("#kn-auto").checked});
      status.textContent = result.orders_staged + " Bestellungen abgerufen, " + (result.auto_imported || 0) + " eingebucht, " + (result.pending_review || 0) + " zur Prüfung.";
      close();
      // Updated state is delivered by the normal websocket subscription.
      await panel._call("get_state").then(state => { panel._state = state; });
      openKnuspr(panel);
    } catch (err) {
      status.textContent = String(err.message || err);
      ev.currentTarget.disabled = false;
    }
  });
  if (!orders.length) return;
  authBox.hidden = true;
  const list = modal.querySelector("#kn-items");
  const picker = modal.querySelector("#kn-order");
  const options = locations.map(loc => `<option value="${esc(loc)}">${esc(panel._locMeta(loc).label)}</option>`).join("");
  function render() {
    const order = orders.find(o => o.id === picker.value);
    list.innerHTML = order.items.map(row => {
      const done = imported.has(row.source_key);
      return `<div class="kn-row" data-key="${esc(row.source_key)}">
        <label><input type="checkbox" class="kn-select" ${done ? "disabled" : "checked"}>
        <span class="kn-product-title">${esc(row.name)} · ${esc(String(row.quantity || 1))} ${done ? "✓ " + texts.done : ""}</span></label>
        <select class="kn-location" aria-label="${texts.choose}" ${done ? "disabled" : ""}>${options}</select>
        <button type="button" class="btn ghost kn-off-search" ${done ? "disabled" : ""} title="Produkt bei Open Food Facts suchen">Produktdaten suchen</button>
        <div class="kn-off-candidates" aria-live="polite"></div></div>`;
    }).join("");
    order.items.forEach((row, i) => {
      const sel = list.children[i]?.querySelector(".kn-location");
      if (sel) sel.value = locations.includes(row.location) ? row.location : locations[0];
    });
  }
  list.addEventListener("click", async event => {
    const button = event.target.closest(".kn-off-search");
    if (!button) return;
    const element = button.closest(".kn-row");
    const key = element.dataset.key;
    const panelArea = element.querySelector(".kn-off-candidates");
    const order = orders.find(o => o.id === picker.value);
    button.disabled = true;
    panelArea.textContent = "Open Food Facts wird durchsucht…";
    try {
      const result = await panel._call("knuspr_off_search", {
        order_id: order.id, source_key: key,
      });
      panelArea.replaceChildren();
      const candidates = result.candidates || [];
      if (!candidates.length) {
        panelArea.textContent = "Kein passender Treffer. Originalbezeichnung bleibt erhalten.";
      }
      for (const candidate of candidates) {
        const choice = document.createElement("button");
        choice.type = "button";
        choice.className = "kn-off-candidate";
        if (candidate.photo) {
          const img = document.createElement("img");
          img.src = candidate.photo;
          img.alt = "";
          img.loading = "lazy";
          choice.appendChild(img);
        }
        const text = document.createElement("span");
        text.textContent = (candidate.brands ? candidate.brands + " · " : "") +
          candidate.name + (candidate.quantity ? " · " + candidate.quantity : "") +
          " (" + candidate.barcode + ")";
        choice.appendChild(text);
        choice.addEventListener("click", () => {
          matched.set(key, candidate);
          element.querySelector(".kn-product-title").textContent =
            candidate.name + " · " + (order.items.find(r => r.source_key === key)?.quantity || 1) + " ✓";
          panelArea.replaceChildren();
          const confirmed = document.createElement("small");
          confirmed.textContent = "Open Food Facts Produkt ausgewählt: " + candidate.barcode;
          panelArea.appendChild(confirmed);
        });
        panelArea.appendChild(choice);
      }
    } catch (err) {
      panelArea.textContent = "Produktsuche nicht verfügbar: " + String(err.message || err);
    } finally { button.disabled = false; }
  });
  picker.addEventListener("change", render);
  modal.querySelector("#kn-all").addEventListener("click", () =>
    list.querySelectorAll(".kn-select:not(:disabled)").forEach(c => { c.checked = true; }));
  modal.querySelector("#kn-import").addEventListener("click", async ev => {
    const selected = [...list.querySelectorAll(".kn-row")].filter(el => el.querySelector(".kn-select").checked);
    if (!selected.length) return;
    const btn = ev.currentTarget; btn.disabled = true;
    try {
      const location_by_key = {};
      const product_by_key = {};
      for (const el of selected) {
        location_by_key[el.dataset.key] = el.querySelector(".kn-location").value;
        if (matched.has(el.dataset.key)) {
          const candidate = matched.get(el.dataset.key);
          product_by_key[el.dataset.key] = {name: candidate.name, barcode: candidate.barcode, photo: candidate.photo || ""};
        }
      }
      const result = await panel._call("knuspr_import_rows", {
        order_id: picker.value,
        source_keys: Object.keys(location_by_key),
        location_by_key,
        product_by_key,
      });
      panel._toast(`${texts.loading}: ${result.imported_ids.length}`);
      close();
    } catch (err) {
      panel._toast(String(err.message || err));
      btn.disabled = false;
    }
  });
  render();
}
