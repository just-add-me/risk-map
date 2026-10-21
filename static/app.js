/* WarRisk Monitor — интерфейс. Без сборщиков и фреймворков: просто Leaflet + fetch. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  fleets: [], fleetId: null, zonesRef: null, zonesFC: null, lastNotifId: null,
  selectedMmsi: null, notifications: [], unread: 0, firstNotifLoad: true,
};

/* ------------------------------------------------------------------ API */
async function api(path, opts = {}) {
  const init = { credentials: "same-origin", ...opts };
  if (opts.json !== undefined) {
    init.method = init.method || "POST";
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(opts.json);
  }
  const r = await fetch(path, init);
  if (r.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || data.message || `Ошибка ${r.status}`);
  return data;
}

function toast(title, body = "", severity = "info", onClick = null) {
  const el = document.createElement("div");
  el.className = `toast ${severity}`;
  el.innerHTML = `<b>${esc(title)}</b>${body ? `<span>${esc(body)}</span>` : ""}`;
  el.onclick = () => { el.remove(); onClick && onClick(); };
  $("#toasts").prepend(el);
  setTimeout(() => el.remove(), severity === "critical" ? 15000 : 7000);
}

const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
};
const ago = (iso) => {
  if (!iso) return "нет данных";
  const m = Math.round((Date.now() - new Date(iso)) / 60000);
  if (m < 1) return "только что";
  if (m < 60) return `${m} мин назад`;
  if (m < 48 * 60) return `${Math.round(m / 60)} ч назад`;
  return `${Math.round(m / 1440)} сут назад`;
};

/* ------------------------------------------------------------------ карта */
const map = L.map("map", { zoomControl: true, worldCopyJump: true, minZoom: 2 }).setView([25, 40], 4);
// Тёмная подложка Esri: работает без ключа (CARTO стал требовать API-ключ)
L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}", {
  attribution: 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors',
  maxNativeZoom: 16, maxZoom: 18,
}).addTo(map);

const zonesLayer = L.geoJSON(null, { style: zoneStyle, onEachFeature: bindZone }).addTo(map);
const vesselsLayer = L.layerGroup().addTo(map);
const trackLayer = L.layerGroup().addTo(map);
const markers = new Map();

function zoneStyle(f) {
  const p = f.properties;
  if (p.severity === "conditional") return { color: "#b48cff", weight: 1.2, dashArray: "4 4", fillColor: "#b48cff", fillOpacity: 0.08 };
  if (p.change === "amended" || p.change === "added") return { color: "#f5b13d", weight: 1.2, fillColor: "#f5b13d", fillOpacity: 0.22 };
  return { color: "#ff5a4e", weight: 1, fillColor: "#ff5a4e", fillOpacity: 0.18 };
}

function zonePopup(p) {
  const tags = [
    p.change && p.change !== "unchanged" ? `<span class="tag amended">${p.change === "added" ? "добавлена" : "изменена"}</span>` : "",
    p.severity === "conditional" ? `<span class="tag cond">условная</span>` : "",
    p.approximate ? `<span class="tag">≈ приблизительно</span>` : "",
    p.needs_review ? `<span class="tag review">проверить</span>` : "",
  ].join(" ");
  return `<h4>${esc(p.name)}</h4><div>${tags}</div>
    ${p.region ? `<div class="muted small">${esc(p.region)}</div>` : ""}
    ${p.qualifier ? `<p>${esc(p.qualifier)}</p>` : ""}
    ${p.note ? `<p class="muted small">${esc(p.note)}</p>` : ""}
    ${p.needs_review ? `<p class="small" style="color:var(--red)">${esc(p.review_reason)}</p>` : ""}
    ${p.text && p.kind === "defined" ? `<div class="zt">${esc(p.text)}</div>` : ""}`;
}

function bindZone(f, layer) {
  layer.bindTooltip(esc(f.properties.name), { sticky: true, direction: "top", opacity: 0.95 });
  layer.bindPopup(() => zonePopup(f.properties));
}

function shipIcon(level, cog, selected) {
  const color = { inside: "#ff5a4e", near: "#f5b13d", safe: "#4fd18b" }[level] || "#7f9aa6";
  const size = selected ? 26 : 20;
  const rot = Number.isFinite(cog) ? cog : 0;
  return L.divIcon({
    className: "ship-icon", iconSize: [size, size], iconAnchor: [size / 2, size / 2],
    html: `<svg width="${size}" height="${size}" viewBox="0 0 20 20" style="transform:rotate(${rot}deg)">
      <path d="M10 1 16 18 10 14 4 18z" fill="${color}" stroke="${selected ? "#fff" : "#0b1a22"}" stroke-width="${selected ? 1.6 : 1}"/></svg>`,
  });
}

map.on("mousemove", (e) => {
  const { lat, lng } = e.latlng.wrap();
  $("#cursor").textContent = `${lat.toFixed(4)}°, ${lng.toFixed(4)}° · zoom ${map.getZoom()}`;
});

/* ------------------------------------------------------------------ зоны */
async function loadZones(force = false) {
  const st = state.status;
  if (!force && st && st.zones.ref === state.zonesRef && state.zonesFC) return;
  const fc = await api("/api/zones");
  state.zonesFC = fc;
  state.zonesRef = (fc.properties || {}).ref || null;
  renderZoneLayer();
  renderZonesPanel();
}

function renderZoneLayer() {
  zonesLayer.clearLayers();
  if (!state.zonesFC) return;
  const showCond = $("#z-show-conditional").checked;
  zonesLayer.addData({ ...state.zonesFC, features: state.zonesFC.features.filter((f) => showCond || f.properties.severity !== "conditional") });
  zonesLayer.bringToBack();
}

function renderZonesPanel() {
  const fc = state.zonesFC;
  const box = $("#zones-list");
  if (!fc || !fc.features.length) {
    box.innerHTML = `<div class="empty"><p>Зоны ещё не построены.</p><p class="muted">При первом запуске построение занимает 1–3 минуты.</p></div>`;
    $("#zones-meta").textContent = "";
    return;
  }
  const p = fc.properties || {};
  $("#zones-meta").innerHTML = `<b>${esc(p.ref || "")}</b> · ${esc(p.issued || "")} · ${fc.features.length} зон`;
  const groups = {};
  fc.features.forEach((f, i) => { (groups[f.properties.region || "Прочее"] ||= []).push([f, i]); });
  box.innerHTML = Object.entries(groups).map(([region, items]) => `
    <div class="region">${esc(region)}</div>
    ${items.map(([f, i]) => {
      const z = f.properties;
      return `<div class="zone-item" data-i="${i}">
        <i class="sw ${z.severity === "conditional" ? "z-conditional" : (z.change !== "unchanged" ? "z-amended" : "z-unchanged")}"></i>
        <span class="zname">${esc(z.name)}</span>
        ${z.change && z.change !== "unchanged" ? `<span class="tag amended">${z.change === "added" ? "new" : "изм."}</span>` : ""}
        ${z.approximate ? `<span class="tag" title="Контур приблизительный">≈</span>` : ""}
        ${z.needs_review ? `<span class="tag review" title="${esc(z.review_reason)}">!</span>` : ""}
      </div>`;
    }).join("")}`).join("");
  $$(".zone-item", box).forEach((el) => el.onclick = () => focusZone(+el.dataset.i));
}

function focusZone(i) {
  const f = state.zonesFC.features[i];
  const layer = L.geoJSON(f);
  map.fitBounds(layer.getBounds(), { padding: [30, 30], maxZoom: 7 });
  zonesLayer.eachLayer((l) => { if (l.feature === f) l.openPopup(layer.getBounds().getCenter()); });
}

/* ------------------------------------------------------------------ флот */
async function loadFleets() {
  const data = await api("/api/fleets");
  state.fleets = data.fleets;
  if (!state.fleets.find((f) => f.id === state.fleetId)) {
    const saved = +localStorageGet("fleetId");
    state.fleetId = (state.fleets.find((f) => f.id === saved) || state.fleets[0] || {}).id ?? null;
  }
  renderFleet();
  renderVessels();
}

function localStorageGet(k) { try { return localStorage.getItem(k); } catch { return null; } }
function localStorageSet(k, v) { try { localStorage.setItem(k, v); } catch { /* приватный режим */ } }

function currentFleet() { return state.fleets.find((f) => f.id === state.fleetId) || null; }
function allVessels() {
  const seen = new Map();
  state.fleets.forEach((f) => f.vessels.forEach((v) => { if (!seen.has(v.mmsi)) seen.set(v.mmsi, v); }));
  return [...seen.values()];
}

function renderFleet() {
  const sel = $("#fleet-select");
  sel.innerHTML = state.fleets.map((f) => `<option value="${f.id}">${esc(f.name)} (${f.vessels.length})</option>`).join("");
  sel.value = state.fleetId ?? "";
  const empty = !state.fleets.length;
  $("#fleet-empty").hidden = !empty;
  $("#fleet-body").hidden = empty;
  sel.hidden = empty;
  const f = currentFleet();
  if (!f) return;
  const c = { inside: 0, near: 0, safe: 0 };
  f.vessels.forEach((v) => { if (v.position) c[v.status.level]++; });
  $("#fleet-summary").innerHTML = `
    <div class="stat inside"><b>${c.inside}</b><span>в зоне</span></div>
    <div class="stat near"><b>${c.near}</b><span>приближаются</span></div>
    <div class="stat safe"><b>${c.safe}</b><span>вне зон</span></div>`;
  if (!f.vessels.length) $("#add-vessel").open = true;
  const list = $("#vessel-list");
  list.innerHTML = f.vessels.map((v) => {
    const lvl = v.position ? v.status.level : "none";
    const zones = [
      ...v.status.inside.map((z) => `<span class="in">● ${esc(z.name)}</span>`),
      ...v.status.near.slice(0, 2).map((z) => `<span class="nr">◌ ${esc(z.name)} · ${Math.round(z.distance_nm)} миль</span>`),
    ].join("<br>");
    return `<li class="vessel ${v.mmsi === state.selectedMmsi ? "selected" : ""}" data-mmsi="${esc(v.mmsi)}">
      <i class="dot s-${lvl}"></i>
      <div>
        <div class="vessel-name">${esc(v.name || v.ais_name || "Без названия")}</div>
        <div class="vessel-meta">MMSI ${esc(v.mmsi)}${v.imo ? ` · IMO ${esc(v.imo)}` : ""} · ${v.position ? ago(v.position.ts) : "ждём AIS"}</div>
        ${zones ? `<div class="vessel-zones">${zones}</div>` : ""}
      </div>
      <button class="text-btn danger vessel-del" data-id="${v.id}" title="Удалить из флота">✕</button>
    </li>`;
  }).join("") || `<li class="empty muted">Во флоте пока нет судов</li>`;
  $$(".vessel", list).forEach((el) => el.onclick = (e) => {
    if (e.target.closest(".vessel-del")) return;
    selectVessel(el.dataset.mmsi, true);
  });
  $$(".vessel-del", list).forEach((b) => b.onclick = async () => {
    if (!confirm("Удалить судно из флота?")) return;
    await api(`/api/vessels/${b.dataset.id}`, { method: "DELETE" });
    await loadFleets();
  });
}

function renderVessels() {
  const live = new Set();
  for (const v of allVessels()) {
    if (!v.position) continue;
    live.add(v.mmsi);
    const ll = [v.position.lat, v.position.lon];
    const sel = v.mmsi === state.selectedMmsi;
    const icon = shipIcon(v.status.level, v.position.cog ?? v.position.heading, sel);
    let m = markers.get(v.mmsi);
    if (!m) {
      m = L.marker(ll, { icon, zIndexOffset: 1000 }).addTo(vesselsLayer);
      m.on("click", () => selectVessel(v.mmsi, false));
      markers.set(v.mmsi, m);
    } else {
      m.setLatLng(ll);
      m.setIcon(icon);
    }
    const label = esc(v.name || v.ais_name || v.mmsi);
    if (m.getTooltip()) m.setTooltipContent(label); else m.bindTooltip(label, { direction: "top", offset: [0, -8] });
    if (m.getPopup()) m.setPopupContent(vesselPopup(v)); else m.bindPopup(vesselPopup(v));
  }
  for (const [mmsi, m] of markers) if (!live.has(mmsi)) { vesselsLayer.removeLayer(m); markers.delete(mmsi); }
}

function vesselPopup(v) {
  const p = v.position;
  const st = v.status;
  const lines = [
    ...st.inside.map((z) => `<div style="color:var(--red)">● В зоне: ${esc(z.name)} <span class="muted">с ${fmtTime(z.since)}</span></div>`),
    ...st.near.map((z) => `<div style="color:var(--amber)">◌ ${esc(z.name)}: ${Math.round(z.distance_nm)} миль</div>`),
  ].join("") || `<div style="color:var(--green)">Вне зон JWC</div>`;
  return `<h4>${esc(v.name || v.ais_name || "Судно")}</h4>${lines}
    <dl class="kv" style="margin-top:8px">
      <dt>MMSI</dt><dd>${esc(v.mmsi)}</dd>
      ${v.imo ? `<dt>IMO</dt><dd>${esc(v.imo)}</dd>` : ""}
      <dt>Позиция</dt><dd>${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}</dd>
      <dt>Скорость</dt><dd>${p.sog != null ? p.sog.toFixed(1) + " уз" : "—"}</dd>
      <dt>Курс</dt><dd>${p.cog != null ? Math.round(p.cog) + "°" : "—"}</dd>
      ${v.destination ? `<dt>Назначение</dt><dd>${esc(v.destination)}</dd>` : ""}
      <dt>Обновлено</dt><dd>${ago(p.ts)}${p.source === "demo" ? " (демо)" : ""}</dd>
    </dl>`;
}

async function selectVessel(mmsi, fly) {
  state.selectedMmsi = mmsi;
  const v = allVessels().find((x) => x.mmsi === mmsi);
  renderFleet();
  renderVessels();
  trackLayer.clearLayers();
  if (!v || !v.position) { toast("Позиция ещё не получена", "Ждём первое сообщение AIS от этого судна."); return; }
  if (fly) map.flyTo([v.position.lat, v.position.lon], Math.max(map.getZoom(), 6), { duration: 0.8 });
  const m = markers.get(mmsi);
  if (m) setTimeout(() => m.openPopup(), fly ? 850 : 0);
  try {
    const t = await api(`/api/track/${encodeURIComponent(mmsi)}`);
    const pts = t.points.map((p) => [p.lat, p.lon]);
    if (pts.length > 1) L.polyline(pts, { color: "#4fc3d9", weight: 2, opacity: 0.8, dashArray: "2 6" }).addTo(trackLayer);
  } catch { /* трек необязателен */ }
}

/* ------------------------------------------------------------------ уведомления */
async function loadNotifications() {
  const data = await api(`/api/notifications?limit=200${$("#n-unread-only").checked ? "&unread=1" : ""}`);
  const items = data.items;
  const maxId = items.reduce((m, n) => Math.max(m, n.id), 0);
  if (!state.firstNotifLoad && state.lastNotifId != null) {
    items.filter((n) => n.id > state.lastNotifId).reverse().slice(-4).forEach((n) =>
      toast(n.title, n.body.split("\n")[0], n.severity, () => openNotification(n)));
  }
  if (state.lastNotifId == null || maxId > state.lastNotifId) state.lastNotifId = maxId;
  state.firstNotifLoad = false;
  state.notifications = items;
  setUnread(data.unread);
  renderNotifications();
}

function setUnread(n) {
  state.unread = n;
  $("#bell-count").hidden = !n;
  $("#bell-count").textContent = n > 99 ? "99+" : n;
  $("#tab-alert-count").hidden = !n;
  $("#tab-alert-count").textContent = n;
  document.title = n ? `(${n}) WarRisk Monitor` : "WarRisk Monitor";
}

function renderNotifications() {
  const list = $("#notif-list");
  list.innerHTML = state.notifications.map((n) => `
    <li class="notif ${n.severity} ${n.is_read ? "read" : "unread"}" data-id="${n.id}">
      <div class="notif-time">${fmtTime(n.ts)}</div>
      <div class="notif-title">${esc(n.title)}</div>
      ${n.body ? `<div class="notif-body">${esc(n.body)}</div>` : ""}
    </li>`).join("") || `<li class="empty muted">Уведомлений нет</li>`;
  $$(".notif", list).forEach((el) => el.onclick = () => openNotification(state.notifications.find((n) => n.id === +el.dataset.id)));
}

async function openNotification(n) {
  if (!n) return;
  if (!n.is_read) {
    await api("/api/notifications/read", { json: { ids: [n.id] } });
    n.is_read = 1;
    setUnread(Math.max(0, state.unread - 1));
    renderNotifications();
  }
  if (n.mmsi) { switchTab("fleet"); selectVessel(n.mmsi, true); }
  else if (n.kind === "circular") switchTab("circulars");
}

/* ------------------------------------------------------------------ циркуляры */
async function loadCirculars() {
  const data = await api("/api/circulars");
  const st = state.status || {};
  const chk = (st.circular || {}).check || {};
  const active = data.items.find((c) => c.status === "active");
  $("#circ-status").innerHTML = `
    <div><b>Действует:</b> ${active ? `${esc(active.ref)} от ${esc(active.issued || "—")}` : "—"}</div>
    <div class="muted small">Автопроверка сайта LMA каждые ${esc((st.circular || {}).interval_hours ?? 12)} ч ·
      последняя: ${chk.last_check ? ago(chk.last_check) : "ещё не было"}
      ${chk.last_seen ? ` · на сайте: ${esc(chk.last_seen.ref)}` : ""}</div>
    ${chk.last_error ? `<div class="small" style="color:var(--red)">Ошибка: ${esc(chk.last_error)}</div>` : ""}
    ${data.state.running ? `<div class="small" style="color:var(--amber)">⏳ ${esc(data.state.step)}</div>` : ""}`;
  $("#circ-list").innerHTML = data.items.map((c) => {
    const d = c.diff_names || {};
    const ch = [
      d.added?.length ? `<li>Добавлено: ${d.added.map(esc).join(", ")}</li>` : "",
      d.amended?.length ? `<li>Изменено: ${d.amended.map(esc).join(", ")}</li>` : "",
      d.removed?.length ? `<li>Исключено: ${d.removed.map(esc).join(", ")}</li>` : "",
      ...c.review.map((r) => `<li class="review">Проверить: ${esc(r.name)} — ${esc(r.reason)}</li>`),
    ].join("");
    const label = { active: "действует", superseded: "заменён", building: "строится", new: "новый", error: "ошибка" }[c.status] || c.status;
    return `<li class="circ">
      <div class="circ-head"><span class="circ-ref">${esc(c.ref)}</span><span class="st st-${esc(c.status)}">${label}</span></div>
      <div class="muted small">от ${esc(c.issued || "—")} · зон в перечне: ${c.entries}${c.diff?.base ? ` · сравнение с ${esc(c.diff.base)}` : ""}</div>
      ${ch ? `<ul>${ch}</ul>` : ""}
      <div class="circ-actions">
        ${c.file_name ? `<a href="/circulars/${encodeURIComponent(c.ref)}/file" target="_blank" rel="noopener">Документ</a>` : ""}
        ${c.source_url && c.source_url.startsWith("http") ? `<a href="${esc(c.source_url)}" target="_blank" rel="noopener">Источник</a>` : ""}
        <button class="text-btn" data-rebuild="${esc(c.ref)}">${{ active: "Перестроить", error: "Повторить построение" }[c.status] || "Сделать активным"}</button>
      </div>
      ${c.build_log ? `<details><summary class="muted small">Журнал построения</summary><pre class="log">${esc(c.build_log)}</pre></details>` : ""}
    </li>`;
  }).join("") || `<li class="empty muted">Циркуляров пока нет</li>`;
  $$("[data-rebuild]").forEach((b) => b.onclick = async () => {
    if (!confirm(`Построить зоны по ${b.dataset.rebuild} и сделать их активными?`)) return;
    b.disabled = true;
    toast("Строю зоны…", "Это займёт 1–3 минуты");
    try { const r = await api(`/api/circulars/${encodeURIComponent(b.dataset.rebuild)}/rebuild`, { method: "POST" }); toast(r.message); }
    catch (e) { toast("Ошибка", e.message, "critical"); }
    await refreshAll(true);
  });
}

/* ------------------------------------------------------------------ статус и цикл обновления */
async function loadStatus() {
  const st = await api("/api/status");
  state.status = st;
  const c = st.circular.active;
  $("#chip-circular").textContent = c ? `${c.ref} · ${c.issued || ""}` : "Циркуляр не загружен";
  const chip = $("#chip-ais");
  if (st.ais.mode === "demo") { chip.textContent = "AIS: демо"; chip.className = "chip demo"; chip.title = "Симулятор движения. Укажите AISSTREAM_API_KEY в .env для живых данных"; }
  else { chip.textContent = `AIS: live${st.ais.connected ? "" : " (нет связи)"}`; chip.className = `chip ${st.ais.connected ? "live" : "demo"}`; chip.title = st.ais.last_error || `сообщений: ${st.ais.messages}`; }
  $("#sb-refs").textContent = st.zones.ref ? `${st.zones.ref} · ${st.zones.count} зон · JWC (LMA/IUA)` : "";
  const banner = $("#build-banner");
  banner.hidden = !st.circular.check.running;
  banner.textContent = `⏳ ${st.circular.check.step || "Обновление зон"}`;
  setUnread(st.unread);
}

async function refreshAll(forceZones = false) {
  try {
    await loadStatus();
    await loadZones(forceZones);
    await loadFleets();
    await loadNotifications();
    if ($("#panel-circulars").classList.contains("active")) await loadCirculars();
  } catch (e) { if (e.message !== "unauthorized") console.warn(e); }
}

/* ------------------------------------------------------------------ события UI */
function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${name}`));
  if (name === "circulars") loadCirculars().catch((e) => toast("Ошибка", e.message, "critical"));
}
$$(".tab").forEach((t) => t.onclick = () => switchTab(t.dataset.tab));
$("#btn-bell").onclick = () => switchTab("alerts");
$("#legend-toggle").onclick = () => $("#legend").classList.toggle("collapsed");
$("#z-show-conditional").onchange = renderZoneLayer;
$("#n-unread-only").onchange = loadNotifications;
$("#fleet-select").onchange = (e) => { state.fleetId = +e.target.value; localStorageSet("fleetId", state.fleetId); renderFleet(); };

$("#btn-fleet-new").onclick = async () => {
  const name = prompt("Название нового флота:");
  if (!name) return;
  try { const r = await api("/api/fleets/create", { json: { name } }); state.fleetId = r.id; localStorageSet("fleetId", r.id); await loadFleets(); }
  catch (e) { toast("Не удалось создать флот", e.message, "critical"); }
};
$("#btn-fleet-rename").onclick = async () => {
  const f = currentFleet(); if (!f) return;
  const name = prompt("Новое название флота:", f.name);
  if (!name) return;
  await api(`/api/fleets/${f.id}`, { method: "PATCH", json: { name } }).catch((e) => toast("Ошибка", e.message, "critical"));
  await loadFleets();
};
$("#btn-fleet-delete").onclick = async () => {
  const f = currentFleet(); if (!f) return;
  if (!confirm(`Удалить флот «${f.name}» и все его суда? Действие необратимо.`)) return;
  await api(`/api/fleets/${f.id}`, { method: "DELETE" });
  state.fleetId = null;
  await loadFleets();
};
$("#form-vessel").onsubmit = async (e) => {
  e.preventDefault();
  const f = currentFleet(); if (!f) return;
  const fd = Object.fromEntries(new FormData(e.target));
  try {
    await api(`/api/fleets/${f.id}/vessels`, { json: fd });
    e.target.reset();
    toast("Судно добавлено", "Позиция появится после первого сообщения AIS.");
    await loadFleets();
  } catch (err) { toast("Не удалось добавить", err.message, "critical"); }
};

async function uploadFleetFile(file) {
  const f = currentFleet(); if (!f || !file) return;
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await api(`/api/fleets/${f.id}/upload`, { method: "POST", body: fd });
    let body = `Добавлено: ${r.added}, уже были: ${r.exists}`;
    if (r.skipped_count) body += `. Пропущено без MMSI: ${r.skipped_count} (${r.skipped_no_mmsi.slice(0, 5).join(", ")}${r.skipped_count > 5 ? "…" : ""})`;
    toast("Файл обработан", body, r.skipped_count ? "warning" : "info");
    await loadFleets();
  } catch (e) { toast("Ошибка загрузки", e.message, "critical"); }
}
$("#pick-file").onclick = (e) => { e.preventDefault(); $("#file-input").click(); };
$("#file-input").onchange = (e) => { uploadFleetFile(e.target.files[0]); e.target.value = ""; };
const drop = $("#drop");
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => uploadFleetFile(e.dataTransfer.files[0]));

$("#btn-read-all").onclick = async () => { await api("/api/notifications/read", { json: { all: true } }); await loadNotifications(); };

$("#btn-circ-check").onclick = async (e) => {
  const b = e.target; b.disabled = true; b.textContent = "Проверяю…";
  try { const r = await api("/api/circulars/check", { method: "POST" }); toast("Проверка завершена", r.message); }
  catch (err) { toast("Проверка не удалась", err.message, "critical"); }
  b.disabled = false; b.textContent = "Проверить сайт LMA сейчас";
  await refreshAll(true);
};
$("#btn-circ-upload").onclick = () => $("#circ-file").click();
$("#circ-file").onchange = async (e) => {
  const file = e.target.files[0]; e.target.value = "";
  if (!file) return;
  const fd = new FormData(); fd.append("file", file); fd.append("force", "1");
  toast("Загружаю и строю зоны…", "Это займёт 1–3 минуты");
  try { const r = await api("/api/circulars/upload", { method: "POST", body: fd }); toast("Готово", r.message); }
  catch (err) { toast("Не удалось обработать документ", err.message, "critical"); }
  await refreshAll(true);
};

/* ------------------------------------------------------------------ старт */
if (window.innerWidth < 820) $("#legend").classList.add("collapsed");
(async () => {
  await refreshAll(true);
  const vs = allVessels().filter((v) => v.position);
  if (vs.length) map.fitBounds(L.latLngBounds(vs.map((v) => [v.position.lat, v.position.lon])).pad(0.5), { maxZoom: 6 });
  setInterval(() => refreshAll(false), 10000);
})();
