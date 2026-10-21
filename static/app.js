/* WarRisk Monitor — интерфейс. Без сборщиков и фреймворков: просто Leaflet + fetch. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  fleets: [], fleetId: null, zonesRef: null, zonesFC: null, lastNotifId: null,
  selectedMmsi: null, notifications: [], unread: 0, firstNotifLoad: true,
  selectedZone: null, zonesVersion: null, zoneById: null, ports: null, portMarkers: [],
  zoneFilter: { q: "", types: null, changedOnly: false },
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
const portsRenderer = L.canvas({ padding: 0.5 });
const portsLayer = L.layerGroup();
const vesselsLayer = L.layerGroup().addTo(map);
const trackLayer = L.layerGroup().addTo(map);
const markers = new Map();

// Три слоя зон и типы внутри них. Цвет заливки — по типу; изменённые в последней версии — жёлтый пунктир
const LAYERS = {
  jwc: { label: "JWC — военные риски судна", hint: "Hull War, Piracy, Terrorism: доп. премия страховщику корпуса" },
  ibf: { label: "IBF/ITF — экипаж", hint: "Бонусы и права экипажа по коллективному договору IBF" },
  inl: { label: "INL (IWL) — навигационные ограничения", hint: "Лёд и полярные воды: заход только с согласия страховщика" },
};
const ZONE_TYPES = {
  country: { layer: "jwc", label: "Страна (порты и 12 миль)", color: "#ff5a4e" },
  sea: { layer: "jwc", label: "Морской район", color: "#ff9440" },
  conditional: { layer: "jwc", label: "Условная (offshore)", color: "#b48cff", dash: "4 4" },
  woa: { layer: "ibf", label: "WOA — военные действия", color: "#e8368f" },
  hra: { layer: "ibf", label: "HRA — высокий риск", color: "#f783ac" },
  erz: { layer: "ibf", label: "ERZ — расширенный риск", color: "#20c997" },
  erz2: { layer: "ibf", label: "ERZ-2 — порты", color: "#c0eb75" },
  inl_perm: { layer: "inl", label: "Круглый год", color: "#4dabf7" },
  inl_season: { layer: "inl", label: "Сезонные (лёд)", color: "#a5d8ff", dash: "5 4" },
};
const CHANGED_COLOR = "#ffe066";
state.zoneFilter.types = new Set(Object.keys(ZONE_TYPES));
state.showPorts = localStorageGet("showPorts") !== "0";
const PORT_COLORS = { jwc: "#ff5a4e", ibf: "#e8368f", inl: "#4dabf7", none: "#8fa6b0" };

const layerOf = (p) => p.layer || "jwc";
function zoneType(p) {
  const layer = layerOf(p);
  if (layer === "ibf") return ZONE_TYPES[p.category] ? p.category : "woa";
  if (layer === "inl") return p.season ? "inl_season" : "inl_perm";
  if (p.severity === "conditional") return "conditional";
  return p.kind === "defined" ? "sea" : "country";
}
const isChanged = (p) => p.change === "amended" || p.change === "added";

// Сезон INL: {"from": "12-10", "to": "05-25"} — как на сервере (zones.in_season)
function inSeason(p) {
  if (!p.season) return true;
  const now = new Date();
  const md = `${String(now.getUTCMonth() + 1).padStart(2, "0")}-${String(now.getUTCDate()).padStart(2, "0")}`;
  const { from, to } = p.season;
  return from <= to ? from <= md && md <= to : md >= from || md <= to;
}
const fmtDate = (iso) => (iso ? iso.split("-").reverse().join(".") : "—");
const today = () => new Date().toISOString().slice(0, 10);

function zoneStyle(f) {
  const p = f.properties;
  const t = ZONE_TYPES[zoneType(p)];
  const layer = layerOf(p);
  const s = { color: t.color, weight: 1, fillColor: t.color, fillOpacity: { jwc: 0.2, ibf: 0.18, inl: 0.1 }[layer] };
  if (t.dash) s.dashArray = t.dash;
  if (layer === "inl" && !inSeason(p)) s.fillOpacity = 0.03;
  if (isChanged(p)) Object.assign(s, { color: CHANGED_COLOR, weight: 2, dashArray: "6 4" });
  if (state.selectedZone === p.id) Object.assign(s, { color: "#ffffff", weight: 2.5, dashArray: null, fillOpacity: 0.45 });
  return s;
}

function selectZone(id) {
  state.selectedZone = id;
  zonesLayer.setStyle(zoneStyle);
  zonesLayer.eachLayer((l) => { if (l.feature.properties.id === id) l.bringToFront(); });
  $$("#zones-list .zone-item").forEach((el) =>
    el.classList.toggle("selected", state.zonesFC.features[+el.dataset.i].properties.id === id));
}

function zonePopup(p) {
  const layer = layerOf(p);
  const t = ZONE_TYPES[zoneType(p)];
  const tags = [
    `<span class="tag" style="color:${t.color};border-color:${t.color}">${esc(t.label)}</span>`,
    p.change && p.change !== "unchanged" ? `<span class="tag amended">${p.change === "added" ? "добавлена" : "изменена"}</span>` : "",
    p.severity === "conditional" ? `<span class="tag cond">условная</span>` : "",
    p.approximate ? `<span class="tag">≈ приблизительно</span>` : "",
    p.needs_review ? `<span class="tag review">проверить</span>` : "",
  ].join(" ");
  let extra = "";
  if (layer === "ibf") {
    const expired = p.valid_to && p.valid_to < today();
    extra = `<p class="small">${p.valid_to ? `Действует до <b>${fmtDate(p.valid_to)}</b>` : "Срок не указан"}
        ${expired ? `<span style="color:var(--amber)"> — срок истёк, ждём новый список</span>` : ""}
        <span class="muted"> · список IBF от ${fmtDate(p.list_date)}</span></p>
      <p class="small">${esc(p.benefits)}</p>`;
  } else if (layer === "inl") {
    extra = p.season
      ? `<p class="small">Сезон: ${esc(p.season.text)} · ${inSeason(p) ? `<b style="color:var(--amber)">сейчас действует</b>` : "сейчас не действует"}</p>`
      : `<p class="small">Действует круглый год.</p>`;
  }
  return `<h4>${esc(p.name)}</h4><div class="tags">${tags}</div>
    <div class="muted small">${esc(LAYERS[layer].hint)}${layer === "jwc" && p.region ? ` · ${esc(p.region)}` : ""}</div>
    ${extra}
    ${p.qualifier ? `<p>${esc(p.qualifier)}</p>` : ""}
    ${p.note ? `<p class="muted small">${esc(p.note)}</p>` : ""}
    ${p.needs_review ? `<p class="small" style="color:var(--red)">${esc(p.review_reason)}</p>` : ""}
    ${p.text && (p.kind === "defined" || layer === "ibf") ? `<div class="zt">${esc(p.text)}</div>` : ""}`;
}

function bindZone(f, layer) {
  layer.bindTooltip(esc(f.properties.name), { sticky: true, direction: "top", opacity: 0.95 });
  layer.bindPopup(() => zonePopup(f.properties), { minWidth: 260, maxWidth: 360 });
  layer.on("popupopen", () => selectZone(f.properties.id));
  layer.on("popupclose", () => { if (state.selectedZone === f.properties.id) selectZone(null); });
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
  if (!force && st && st.zones.version === state.zonesVersion && state.zonesFC) return;
  const fc = await api("/api/zones");
  state.zonesFC = fc;
  state.zonesVersion = st ? st.zones.version : null;
  state.zoneById = new Map(fc.features.map((f) => [f.properties.id, f.properties]));
  renderZoneLayer();
  renderZonesPanel();
  await loadPorts();
}

function zoneVisible(p) {
  const zf = state.zoneFilter;
  if (!zf.types.has(zoneType(p))) return false;
  if (zf.changedOnly && !isChanged(p)) return false;
  const q = zf.q.trim().toLowerCase();
  return !q || `${p.name} ${p.region || ""} ${p.title || ""}`.toLowerCase().includes(q);
}

function renderZoneLayer() {
  zonesLayer.clearLayers();
  if (!state.zonesFC) return;
  // INL рисуем первым (снизу): его районы огромные и не должны перекрывать клики по JWC и IBF
  const order = { inl: 0, jwc: 1, ibf: 2 };
  const feats = state.zonesFC.features.filter((f) => zoneVisible(f.properties))
    .sort((a, b) => order[layerOf(a.properties)] - order[layerOf(b.properties)]);
  zonesLayer.addData({ ...state.zonesFC, features: feats });
  zonesLayer.bringToBack();
}

function renderZoneFilters() {
  const fc = state.zonesFC;
  const counts = Object.fromEntries(Object.keys(ZONE_TYPES).map((t) => [t, 0]));
  let changed = 0;
  (fc ? fc.features : []).forEach((f) => { counts[zoneType(f.properties)]++; if (isChanged(f.properties)) changed++; });
  const zf = state.zoneFilter;
  const chip = (t, d) => `<button class="fchip ${zf.types.has(t) ? "on" : ""}" data-t="${t}" aria-pressed="${zf.types.has(t)}">
      <i class="sw" style="${swatchStyle(d)}"></i>${esc(d.label)} <span class="muted">${counts[t]}</span></button>`;
  $("#z-types").innerHTML = Object.entries(LAYERS).map(([layer, info]) => {
    const types = Object.entries(ZONE_TYPES).filter(([, d]) => d.layer === layer);
    const on = types.some(([t]) => zf.types.has(t));
    return `<div class="zf-layer">
      <label class="zf-layer-head" title="${esc(info.hint)}">
        <input type="checkbox" data-layer="${layer}" ${on ? "checked" : ""}> ${esc(info.label)}</label>
      <div class="z-types-row">${types.map(([t, d]) => chip(t, d)).join("")}</div></div>`;
  }).join("") + `<div class="z-types-row">
    <button class="fchip ${zf.changedOnly ? "on" : ""}" data-t="changed" aria-pressed="${zf.changedOnly}">
      <i class="sw z-changed"></i>Только изменённые <span class="muted">${changed}</span></button>
    <button class="fchip ${state.showPorts ? "on" : ""}" data-t="ports" aria-pressed="${state.showPorts}">
      <i class="dot" style="background:${PORT_COLORS.none}"></i>Порты <span class="muted">${(state.ports || []).length}</span></button></div>`;
  $$("#z-types .fchip").forEach((b) => b.onclick = () => {
    const t = b.dataset.t;
    if (t === "changed") zf.changedOnly = !zf.changedOnly;
    else if (t === "ports") { state.showPorts = !state.showPorts; localStorageSet("showPorts", state.showPorts ? "1" : "0"); updatePortsVisibility(); }
    else if (zf.types.has(t)) zf.types.delete(t);
    else zf.types.add(t);
    applyZoneFilter();
  });
  $$("#z-types [data-layer]").forEach((cb) => cb.onchange = () => {
    Object.entries(ZONE_TYPES).filter(([, d]) => d.layer === cb.dataset.layer)
      .forEach(([t]) => (cb.checked ? zf.types.add(t) : zf.types.delete(t)));
    applyZoneFilter();
  });
}

function renderLegend() {
  $("#legend-zones").innerHTML = Object.entries(LAYERS).map(([layer, info]) => `
    <div class="lg-title" title="${esc(info.hint)}">${esc(info.label)}</div>
    ${Object.values(ZONE_TYPES).filter((d) => d.layer === layer)
      .map((d) => `<div class="lg"><i class="sw" style="${swatchStyle(d)}"></i>${esc(d.label)}</div>`).join("")}`).join("");
}

function swatchStyle(d) {
  return `background:${d.color}55;border:1px ${d.dash ? "dashed" : "solid"} ${d.color}`;
}

function applyZoneFilter() {
  renderZoneLayer();
  renderZonesPanel();
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
  renderZoneFilters();
  const groups = {};
  let shown = 0;
  fc.features.forEach((f, i) => {
    if (!zoneVisible(f.properties)) return;
    shown++;
    (groups[f.properties.region || "Прочее"] ||= []).push([f, i]);
  });
  const ibfMeta = (p.layers || {}).ibf || {};
  $("#zones-meta").innerHTML = `<b>${esc(p.ref || "")}</b> · ${esc(p.issued || "")}
    ${ibfMeta.list_date ? ` · IBF от ${fmtDate(ibfMeta.list_date)}` : ""} ·
    ${shown === fc.features.length ? `${shown} зон` : `${shown} из ${fc.features.length} зон`}`;
  const portHits = portSearch(state.zoneFilter.q);
  const portsHtml = portHits.length ? `<div class="region">Порты</div>${portHits.map((pt) => `
    <div class="zone-item port-item" data-port="${pt.idx}">
      <i class="dot" style="background:${portColor(pt)}"></i>
      <span class="zname">${esc(pt.name)} <span class="muted small">${esc(pt.country)}</span></span>
      ${pt.zones.length ? `<span class="tag">${pt.zones.length} зон</span>` : ""}
    </div>`).join("")}` : "";
  if (!shown && !portHits.length) {
    box.innerHTML = `<div class="empty"><p class="muted">Под фильтр не попала ни одна зона.</p></div>`;
    return;
  }
  box.innerHTML = portsHtml + Object.entries(groups).map(([region, items]) => `
    <div class="region">${esc(region)}</div>
    ${items.map(([f, i]) => {
      const z = f.properties;
      const t = ZONE_TYPES[zoneType(z)];
      return `<div class="zone-item ${state.selectedZone === z.id ? "selected" : ""}" data-i="${i}">
        <i class="sw ${isChanged(z) ? "is-changed" : ""}" style="${swatchStyle(t)}"></i>
        <span class="zname">${esc(z.name)}</span>
        ${z.change && z.change !== "unchanged" ? `<span class="tag amended">${z.change === "added" ? "new" : "изм."}</span>` : ""}
        ${z.season && !inSeason(z) ? `<span class="tag" title="Сезон: ${esc(z.season.text)}">вне сезона</span>` : ""}
        ${z.valid_to && z.valid_to < today() ? `<span class="tag review" title="Срок истёк ${fmtDate(z.valid_to)}">срок</span>` : ""}
        ${z.approximate ? `<span class="tag" title="Контур приблизительный">≈</span>` : ""}
        ${z.needs_review ? `<span class="tag review" title="${esc(z.review_reason)}">!</span>` : ""}
      </div>`;
    }).join("")}`).join("");
  $$(".zone-item[data-i]", box).forEach((el) => el.onclick = () => focusZone(+el.dataset.i));
  $$(".port-item", box).forEach((el) => el.onclick = () => focusPort(+el.dataset.port));
}

function focusZone(i) {
  const f = state.zonesFC.features[i];
  const layer = L.geoJSON(f);
  map.fitBounds(layer.getBounds(), { padding: [30, 30], maxZoom: 7 });
  zonesLayer.eachLayer((l) => { if (l.feature.properties.id === f.properties.id) l.openPopup(layer.getBounds().getCenter()); });
}

/* ------------------------------------------------------------------ порты */
async function loadPorts() {
  const data = await api("/api/ports");
  state.ports = data.ports.map((p, idx) => ({ ...p, idx }));
  renderPorts();
  renderZoneFilters();
}

function portLayers(p) {
  return new Set(p.zones.map((id) => layerOf(state.zoneById?.get(id) || {})));
}
function portColor(p) {
  const ls = portLayers(p);
  return PORT_COLORS[["jwc", "ibf", "inl"].find((l) => ls.has(l)) || "none"];
}

function portPopup(p) {
  const zs = p.zones.map((id) => state.zoneById?.get(id)).filter(Boolean);
  const list = zs.map((z) => {
    const t = ZONE_TYPES[zoneType(z)];
    const off = z.season && !inSeason(z) ? ` <span class="muted">(вне сезона)</span>` : "";
    return `<div class="small"><i class="sw" style="${swatchStyle(t)}"></i> ${esc(z.name)}${off}</div>`;
  }).join("") || `<div class="small" style="color:var(--green)">Вне зон риска</div>`;
  return `<h4>${esc(p.name)}</h4><div class="muted small">${esc(p.country || "")} · ${p.lat.toFixed(3)}°, ${p.lon.toFixed(3)}°</div>
    <div style="margin-top:6px;display:grid;gap:3px">${list}</div>`;
}

function renderPorts() {
  portsLayer.clearLayers();
  state.portMarkers = (state.ports || []).map((p) => {
    const inZone = p.zones.length > 0;
    return L.circleMarker([p.lat, p.lon], {
      renderer: portsRenderer, radius: inZone ? 4 : 3, weight: 1, color: "#0b1a22",
      fillColor: portColor(p), fillOpacity: 0.95,
    }).bindTooltip(esc(p.name), { direction: "top", offset: [0, -4] })
      .bindPopup(() => portPopup(p), { minWidth: 220, maxWidth: 340 })
      .addTo(portsLayer);
  });
  updatePortsVisibility();
}

function updatePortsVisibility() {
  const show = state.showPorts && map.getZoom() >= 3;
  if (show && !map.hasLayer(portsLayer)) portsLayer.addTo(map);
  if (!show && map.hasLayer(portsLayer)) map.removeLayer(portsLayer);
}
map.on("zoomend", updatePortsVisibility);

function portSearch(q) {
  q = q.trim().toLowerCase();
  if (q.length < 2 || !state.ports) return [];
  return state.ports.filter((p) => p.name.toLowerCase().includes(q) || p.country.toLowerCase() === q).slice(0, 8);
}

function focusPort(idx) {
  const p = state.ports[idx];
  map.setView([p.lat, p.lon], Math.max(map.getZoom(), 8));
  if (!state.showPorts) { state.showPorts = true; updatePortsVisibility(); renderZoneFilters(); }
  state.portMarkers[idx].openPopup();
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
  ].join("") || `<div style="color:var(--green)">Вне зон риска</div>`;
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

async function loadIbf() {
  const d = await api("/api/ibf");
  const expired = d.list_until && d.list_until < today();
  const rows = ((d.spec || {}).designations || []).map((x) => {
    const t = ZONE_TYPES[x.category] || {};
    return `<tr><td><i class="sw" style="${swatchStyle(t.color ? t : { color: "#888" })}"></i></td>
      <td>${esc(x.title.replace(/^IBF\s+/, ""))}</td>
      <td class="muted" style="white-space:nowrap">до ${fmtDate(x.valid_to)}</td></tr>`;
  }).join("");
  const problems = (d.log || []).filter((l) => /^[⚠✖]/.test(l));
  $("#ibf-status").innerHTML = `
    <div><b>Действует:</b> ${d.list_date ? `список от ${fmtDate(d.list_date)}, продлён до
      <b style="${expired ? "color:var(--amber)" : ""}">${fmtDate(d.list_until)}</b>` : "ещё не загружен"}</div>
    ${expired ? `<div class="small" style="color:var(--amber)">Срок истёк, новая версия на сайте пока не найдена. Зоны остаются на карте.</div>` : ""}
    <div class="muted small">Автопроверка сайта IMEC вместе с проверкой JWC · последняя: ${d.last_check ? ago(d.last_check) : "ещё не было"}
      ${d.url ? ` · <a href="${esc(d.url)}" target="_blank" rel="noopener">PDF списка</a>` : ""}
      · <a href="${esc(d.page_url)}" target="_blank" rel="noopener">страница IMEC</a></div>
    ${d.last_error ? `<div class="small" style="color:var(--red)">Ошибка: ${esc(d.last_error)}</div>` : ""}
    ${d.running ? `<div class="small" style="color:var(--amber)">⏳ ${esc(d.step)}</div>` : ""}
    ${problems.map((l) => `<div class="small" style="color:var(--red)">${esc(l)}</div>`).join("")}
    ${rows ? `<table class="ibf-table">${rows}</table>` : ""}`;
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
  $("#sb-refs").textContent = st.zones.ref
    ? `JWC ${st.zones.ref}${st.ibf.list_date ? ` · IBF ${fmtDate(st.ibf.list_date)}` : ""} · ${st.zones.count} зон` : "";
  const banner = $("#build-banner");
  banner.hidden = !st.circular.check.running && !st.ibf.running;
  banner.textContent = `⏳ ${st.circular.check.step || st.ibf.step || "Обновление зон"}`;
  setUnread(st.unread);
}

async function refreshAll(forceZones = false) {
  try {
    await loadStatus();
    await loadZones(forceZones);
    await loadFleets();
    await loadNotifications();
    if ($("#panel-circulars").classList.contains("active")) { await loadCirculars(); await loadIbf(); }
  } catch (e) { if (e.message !== "unauthorized") console.warn(e); }
}

/* ------------------------------------------------------------------ события UI */
function switchTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${name}`));
  if (name === "circulars") {
    loadCirculars().catch((e) => toast("Ошибка", e.message, "critical"));
    loadIbf().catch((e) => toast("Ошибка", e.message, "critical"));
  }
}
$$(".tab").forEach((t) => t.onclick = () => switchTab(t.dataset.tab));
$("#btn-bell").onclick = () => switchTab("alerts");
$("#legend-toggle").onclick = () => $("#legend").classList.toggle("collapsed");
$("#z-search").oninput = (e) => { state.zoneFilter.q = e.target.value; applyZoneFilter(); };
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
$("#btn-ibf-check").onclick = async (e) => {
  const b = e.target; b.disabled = true; b.textContent = "Проверяю…";
  try { const r = await api("/api/ibf/check", { method: "POST" }); toast("Проверка IBF завершена", r.message); }
  catch (err) { toast("Проверка IBF не удалась", err.message, "critical"); }
  b.disabled = false; b.textContent = "Проверить список IBF сейчас";
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
renderLegend();
(async () => {
  await refreshAll(true);
  const vs = allVessels().filter((v) => v.position);
  if (vs.length) map.fitBounds(L.latLngBounds(vs.map((v) => [v.position.lat, v.position.lon])).pad(0.5), { maxZoom: 6 });
  setInterval(() => refreshAll(false), 10000);
})();
