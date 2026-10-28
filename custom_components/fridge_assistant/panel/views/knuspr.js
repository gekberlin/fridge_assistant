/* Knuspr order review UI. Orders are staged via fridge_assistant.stage_knuspr_orders. */
import { esc } from "../lib/format.js";
import { openModal } from "../lib/surface.js";

export function openKnuspr(panel) {
  const orders = panel._state.knuspr_orders || [];
  const imported = new Set(panel._state.knuspr_imported || []);
  const locations = panel._state.locations || [];
  const texts = panel._lang() === "nl"
    ? {title:"Knuspr-bestellingen", empty:"Nog geen bestellingen opgehaald. Configureer eerst de MCP-brug.", all:"Alles selecteren", import:"Geselecteerde artikelen toevoegen", done:"Al toegevoegd", choose:"Opslaglocatie", loading:"Overgenomen"}
    : {title:"Knuspr orders", empty:"No orders fetched yet. Configure the authenticated MCP bridge first.", all:"Select all", import:"Import selected items", done:"Already imported", choose:"Storage location", loading:"Imported"};
  const { modal, close } = openModal(panel, `<h2>${texts.title}</h2><div class="kn-controls"><button class="btn ghost" id="kn-login" type="button">Mit Knuspr verbinden</button><button class="btn primary" id="kn-fetch" type="button">Letzte 3 Bestellungen abrufen</button><button class="btn ghost" id="kn-discover" type="button">MCP-Verbindung prüfen</button></div><p id="kn-status" role="status"></p>
    ${!orders.length ? `<p>${texts.empty}</p>` : `
    <label class="field">Order <select id="kn-order">
      ${orders.map(o => `<option value="${esc(o.id)}">${esc(o.order_time || o.id)} · ${o.items.length} items</option>`).join("")}
    </select></label><div id="kn-items"></div>
    <button class="btn ghost" id="kn-all" type="button">${texts.all}</button>
    <button class="btn primary" id="kn-import" type="button">${texts.import}</button>`}`);
  const status = modal.querySelector("#kn-status");
  modal.querySelector("#kn-login").addEventListener("click", async ev => {
    ev.currentTarget.disabled = true;
    try {
      const { authorization_url } = await panel._call("knuspr_login");
      window.open(authorization_url, "_blank", "noopener,noreferrer");
      status.textContent = "Bitte im neuen Browserfenster bei Knuspr anmelden.";
    } catch (err) {
      status.textContent = String(err.message || err);
    } finally { ev.currentTarget.disabled = false; }
  });
  modal.querySelector("#kn-discover").addEventListener("click", async ev => {
    ev.currentTarget.disabled = true;
    try {
      const result = await panel._call("knuspr_discover");
      status.textContent = "OAuth discovery: " + JSON.stringify(result);
    } catch (err) { status.textContent = String(err.message || err); }
    finally { ev.currentTarget.disabled = false; }
  });
  modal.querySelector("#kn-fetch").addEventListener("click", async ev => {
    ev.currentTarget.disabled = true;
    status.textContent = "Knuspr wird abgefragt…";
    try {
      const result = await panel._call("knuspr_fetch");
      status.textContent = result.orders_staged + " Bestellungen abgerufen.";
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
  const list = modal.querySelector("#kn-items");
  const picker = modal.querySelector("#kn-order");
  const options = locations.map(loc => `<option value="${esc(loc)}">${esc(panel._locMeta(loc).label)}</option>`).join("");
  function render() {
    const order = orders.find(o => o.id === picker.value);
    list.innerHTML = order.items.map(row => {
      const done = imported.has(row.source_key);
      const suggested = locations.includes(row.location) ? row.location : locations[0];
      return `<div class="kn-row" data-key="${esc(row.source_key)}">
        <label><input type="checkbox" class="kn-select" ${done ? "disabled" : "checked"}>
        <span>${esc(row.name)} · ${esc(String(row.quantity || 1))} ${done ? "✓ " + texts.done : ""}</span></label>
        <select class="kn-location" aria-label="${texts.choose}" ${done ? "disabled" : ""}>${options}</select></div>`;
    }).join("");
    order.items.forEach((row, i) => {
      const sel = list.children[i]?.querySelector(".kn-location");
      if (sel) sel.value = locations.includes(row.location) ? row.location : locations[0];
    });
  }
  picker.addEventListener("change", render);
  modal.querySelector("#kn-all").addEventListener("click", () =>
    list.querySelectorAll(".kn-select:not(:disabled)").forEach(c => { c.checked = true; }));
  modal.querySelector("#kn-import").addEventListener("click", async ev => {
    const selected = [...list.querySelectorAll(".kn-row")].filter(el => el.querySelector(".kn-select").checked);
    if (!selected.length) return;
    const btn = ev.currentTarget; btn.disabled = true;
    try {
      const location_by_key = {};
      for (const el of selected) location_by_key[el.dataset.key] = el.querySelector(".kn-location").value;
      const result = await panel._call("knuspr_import_rows", {
        order_id: picker.value,
        source_keys: Object.keys(location_by_key),
        location_by_key,
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
