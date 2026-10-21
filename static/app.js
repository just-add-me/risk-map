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

// Зоны, порты и суда — на одном холсте: тогда клик попадает в верхний объект (судно → порт → зона).
// С отдельными холстами или SVG зона перехватывала клики по портам внутри неё.
const mapCanvas = L.canvas({ padding: 0.5, tolerance: 3 });
const zonesLayer = L.geoJSON(null, { renderer: mapCanvas, style: zoneStyle, onEachFeature: bindZone }).addTo(map);
// Фигуры на canvas: стрелка — судно (по курсу), ромб — яхта, квадрат — порт
L.Canvas.include({
  _updateShape(layer) {
    if (!this._drawing || layer._empty()) return;
    const p = layer._point, ctx = this._ctx, r = Math.max(layer._radius, 1), o = layer.options;
    ctx.save();
    ctx.translate(p.x, p.y);
    if (o.rotation) ctx.rotate((o.rotation * Math.PI) / 180);
    ctx.beginPath();
    if (o.shape === "ship") {
      ctx.moveTo(0, -r * 1.8); ctx.lineTo(r * 1.05, r * 1.2); ctx.lineTo(0, r * 0.55); ctx.lineTo(-r * 1.05, r * 1.2);
    } else if (o.shape === "yacht") {
      ctx.moveTo(0, -r * 1.6); ctx.lineTo(r * 0.75, 0); ctx.lineTo(0, r * 1.3); ctx.lineTo(-r * 0.75, 0);
    } else {
      const k = r * 0.35;                       // скруглённый квадрат порта
      ctx.moveTo(-r + k, -r); ctx.arcTo(r, -r, r, r, k); ctx.arcTo(r, r, -r, r, k);
      ctx.arcTo(-r, r, -r, -r, k); ctx.arcTo(-r, -r, r, -r, k);
    }
    ctx.closePath();
    this._fillStroke(ctx, layer);
    if (o.shape === "port" && r >= 4.5) {       // якорь внутри, когда значок достаточно крупный
      ctx.beginPath();
      ctx.moveTo(0, -r * 0.6); ctx.lineTo(0, r * 0.6);
      ctx.moveTo(-r * 0.35, -r * 0.3); ctx.lineTo(r * 0.35, -r * 0.3);
      ctx.moveTo(-r * 0.55, r * 0.15); ctx.quadraticCurveTo(0, r * 0.95, r * 0.55, r * 0.15);
      ctx.strokeStyle = "#0b1a22"; ctx.lineWidth = Math.max(1.2, r / 5); ctx.lineCap = "round";
      ctx.stroke();
    }
    ctx.restore();
  },
});
const ShapeMarker = L.CircleMarker.extend({
  _updatePath() { this._renderer._updateShape(this); },
  // стрелка длиннее круга радиуса r — область нажатия берём по всей стрелке плюс запас в 4 px
  _containsPoint(p) { return p.distanceTo(this._point) <= this._radius * 1.8 + 4; },
});
const portsRenderer = mapCanvas;
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
  const s = { color: t.color, weight: layer === "ibf" ? 1.8 : 1, fillColor: t.color, fillOpacity: { jwc: 0.2, ibf: 0.2, inl: 0.1 }[layer] };
  if (t.dash) s.dashArray = t.dash;
  if (layer === "inl" && !inSeason(p)) s.fillOpacity = 0.03;
  if (isChanged(p)) Object.assign(s, { color: CHANGED_COLOR, weight: 2, dashArray: "6 4" });
  if (state.selectedZone === p.id) Object.assign(s, { color: "#ffffff", weight: 2.5, dashArray: null, fillOpacity: 0.45 });
  return s;
}

function selectZone(id) {
  state.selectedZone = id;
  zonesLayer.setStyle(zoneStyle);
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
  $("#legend-shapes").innerHTML = `
    <div class="lg"><svg width="14" height="14" viewBox="-7 -7 14 14"><path d="M0-6 4 4 0 2-4 4z" fill="#6fa8dc"/></svg>Судно (стрелка — курс)</div>
    <div class="lg"><svg width="14" height="14" viewBox="-7 -7 14 14"><path d="M0-6 3 0 0 5-3 0z" fill="#5fe0c8"/></svg>Яхта</div>
    <div class="lg"><svg width="14" height="14" viewBox="-7 -7 14 14"><rect x="-4" y="-4" width="8" height="8" fill="#8fa6b0"/></svg>Порт (красный — в зоне JWC)</div>
    <div class="lg"><svg width="14" height="14" viewBox="-7 -7 14 14"><path d="M0-6 4 5 0 3-4 5z" fill="#ff5a4e" stroke="#fff"/></svg>Судно вашего флота</div>`;
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

async function openPort(p) {
  const popup = L.popup({ minWidth: 300, maxWidth: 320, className: "sc-popup" }).setLatLng([p.lat, p.lon])
    .setContent(portCard(p, null)).openOn(map);
  try {
    const d = await api(`/api/ports/${p.id}`);
    popup.setContent(portCard(p, d));
    $$(".pc-ship", popup.getElement()).forEach((b) => b.onclick = () => {
      const s = d.ships[+b.dataset.i];
      openTrafficShip(s.mmsi, L.latLng(s.lat, s.lon));
    });
  } catch (e) { /* остаётся краткая карточка */ }
}

// Карточка порта: страна, зоны риска, суда рядом (по данным AIS в радиусе 5 миль)
function portCard(p, d) {
  const zs = p.zones.map((id) => state.zoneById?.get(id)).filter(Boolean);
  const zones = zs.map((z) => {
    const t = ZONE_TYPES[zoneType(z)];
    const off = z.season && !inSeason(z) ? ` <span class="muted">(вне сезона)</span>` : "";
    return `<div><i class="sw" style="${swatchStyle(t)}"></i> ${esc(z.name)}${off}</div>`;
  }).join("") || `<div class="zok">Вне зон риска</div>`;
  const cats = d ? Object.entries(d.by_category).sort((a, b) => b[1] - a[1]).map(([c, n]) =>
    `<span class="pc-cat"><i class="dot" style="background:${TRAFFIC_COLORS[c]}"></i>${esc(d.categories[c])}: ${n}</span>`).join("") : "";
  const ships = d ? d.ships.map((s, i) => `
    <button class="pc-ship" type="button" data-i="${i}">
      ${flagImg(s.flag_iso)}<span class="sr-main"><b>${esc(s.name || "MMSI " + s.mmsi)}</b>
      <span class="muted small">${esc(s.type_name)}${s.length ? ` · ${Math.round(s.length)} м` : ""}${s.nav_status ? ` · ${esc(s.nav_status)}` : ""}</span></span>
      <span class="muted small">${s.distance_nm} миль</span></button>`).join("") : "";
  return `<div class="sc">
    <div class="sc-head"><svg width="26" height="26" viewBox="0 0 26 26"><rect x="3" y="3" width="20" height="20" rx="3"
      fill="${portColor(p)}"/><path d="M13 7v12M9 10h8M8 15a5 4 0 0 0 10 0" stroke="#0b1a22" stroke-width="2" fill="none"/></svg>
      <div><div class="sc-name">${esc(p.name)}</div><div class="sc-type">Порт · ${esc(p.country || "страна не определена")}</div></div></div>
    <div class="sc-zones pc-zones">${zones}</div>
    ${d ? `<div class="sc-grid">
      <div><span>Судов рядом</span><b>${d.ships_total}</b></div>
      <div><span>У причала</span><b>${d.status.moored}</b></div>
      <div><span>На якоре</span><b>${d.status.anchored}</b></div></div>
      ${cats ? `<div class="pc-cats">${cats}</div>` : ""}
      ${ships ? `<div class="pc-list"><div class="muted small pc-h">Крупнейшие суда рядом</div>${ships}</div>` : ""}`
    : `<div class="sc-foot">Загружаю суда рядом…</div>`}
    <div class="sc-foot">${p.lat.toFixed(4)}°, ${p.lon.toFixed(4)}° · суда в радиусе 5 миль по данным AIS</div>
    <div class="sc-links"><a href="https://www.google.com/maps/@${p.lat},${p.lon},13z/data=!3m1!1e3" target="_blank" rel="noopener">Спутниковый снимок</a></div>
  </div>`;
}

function renderPorts() {
  portsLayer.clearLayers();
  state.portMarkers = (state.ports || []).map((p) => {
    const inZone = p.zones.length > 0;
    return new ShapeMarker([p.lat, p.lon], {
      shape: "port", renderer: portsRenderer, radius: portRadius(inZone), weight: 1.2, color: "#0b1a22",
      fillColor: portColor(p), fillOpacity: 0.95,
    }).bindTooltip(esc(p.name), { direction: "top", offset: [0, -4] })
      .on("click", () => openPort(p))
      .addTo(portsLayer);
  });
  updatePortsVisibility();
}

// Порт растёт с масштабом: на обзорной карте небольшой квадрат, при приближении — с якорем внутри
function portRadius(inZone) {
  const z = map.getZoom();
  return (z <= 5 ? 3.6 : z <= 7 ? 5 : z <= 9 ? 6.5 : 7.5) * (inZone ? 1.1 : 1);
}

function updatePortsVisibility() {
  const show = state.showPorts && map.getZoom() >= 4;
  if (show && !map.hasLayer(portsLayer)) portsLayer.addTo(map);
  if (!show && map.hasLayer(portsLayer)) map.removeLayer(portsLayer);
}
map.on("zoomend", () => {
  updatePortsVisibility();
  (state.portMarkers || []).forEach((m, i) => m.setRadius(portRadius(state.ports[i].zones.length > 0)));
});

function portSearch(q) {
  q = q.trim().toLowerCase();
  if (q.length < 2 || !state.ports) return [];
  return state.ports.filter((p) => p.name.toLowerCase().includes(q) || p.country.toLowerCase() === q).slice(0, 8);
}

function focusPort(idx) {
  const p = state.ports[idx];
  map.setView([p.lat, p.lon], Math.max(map.getZoom(), 8));
  if (!state.showPorts) { state.showPorts = true; updatePortsVisibility(); renderZoneFilters(); }
  openPort(p);
}

/* ------------------------------------------------------------------ флот */
async function loadFleets() {
  const data = await api("/api/fleets");
  state.fleets = data.fleets;
  state.me = data.user;
  if (!state.fleets.find((f) => f.id === state.fleetId)) {
    const saved = +localStorageGet("fleetId");
    state.fleetId = (state.fleets.find((f) => f.id === saved) || state.fleets[0] || {}).id ?? null;
  }
  renderFleet();
  renderVessels();
}

// Доступ к флоту: список тех, кому открыт, и добавление нового пользователя
async function renderShares(f) {
  $("#share-list").innerHTML = (f.shares || []).map((sh) => `
    <div class="share-row"><b>${esc(sh.username)}</b>
      <select data-share-user="${esc(sh.username)}">
        <option value="0" ${sh.can_edit ? "" : "selected"}>только просмотр</option>
        <option value="1" ${sh.can_edit ? "selected" : ""}>может менять суда</option></select>
      <button class="text-btn danger" data-share-remove="${esc(sh.username)}" title="Закрыть доступ">✕</button></div>`).join("")
    || `<p class="muted small">Флот пока видите только вы.</p>`;
  $$("[data-share-user]").forEach((el) => el.onchange = () =>
    shareFleet(f.id, { username: el.dataset.shareUser, can_edit: el.value === "1" }));
  $$("[data-share-remove]").forEach((b) => b.onclick = () => {
    if (confirm(`Закрыть пользователю ${b.dataset.shareRemove} доступ к флоту «${f.name}»?`)) shareFleet(f.id, { username: b.dataset.shareRemove, remove: true });
  });
  if (!state.users) state.users = (await api("/api/users")).users;
  const taken = new Set((f.shares || []).map((sh) => sh.username));
  const free = state.users.filter((u) => !taken.has(u));
  $("#share-user").innerHTML = free.map((u) => `<option>${esc(u)}</option>`).join("") || `<option value="">все уже имеют доступ</option>`;
  $("#btn-share-add").disabled = !free.length;
}

async function shareFleet(fid, body) {
  try { await api(`/api/fleets/${fid}/share`, { json: body }); await loadFleets(); }
  catch (e) { toast("Не удалось изменить доступ", e.message, "critical"); }
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
  sel.innerHTML = state.fleets.map((f) => `<option value="${f.id}">${esc(f.name)} (${f.vessels.length})${
    f.access === "owner" ? "" : ` · от ${esc(f.owner)}${f.access === "view" ? ", просмотр" : ""}`}</option>`).join("");
  sel.value = state.fleetId ?? "";
  const empty = !state.fleets.length;
  $("#fleet-empty").hidden = !empty;
  $("#fleet-body").hidden = empty;
  sel.hidden = empty;
  const f = currentFleet();
  if (!f) return;
  const owner = f.access === "owner", canEdit = f.access !== "view";
  $("#add-vessel").hidden = !canEdit;
  $("#fleet-share").hidden = !owner;
  $("#fleet-foot-owner").hidden = !owner;
  $("#fleet-foot-guest").hidden = owner;
  $("#fleet-owner-note").textContent = owner ? "" : `Флот пользователя ${f.owner} · ${canEdit ? "можно менять суда" : "только просмотр"}`;
  if (owner) renderShares(f);
  const c = { inside: 0, near: 0, safe: 0 };
  f.vessels.forEach((v) => { if (v.position) c[v.status.level]++; });
  $("#fleet-summary").innerHTML = `
    <div class="stat inside"><b>${c.inside}</b><span>в зоне</span></div>
    <div class="stat near"><b>${c.near}</b><span>приближаются</span></div>
    <div class="stat safe"><b>${c.safe}</b><span>вне зон</span></div>`;
  if (!f.vessels.length && canEdit) $("#add-vessel").open = true;
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
        ${v.ship_type ? `<div class="vessel-meta"><i class="dot" style="background:${TRAFFIC_COLORS[v.category]}"></i> ${esc(v.type_name)}${v.length ? ` · ${Math.round(v.length)} м` : ""}${v.flag ? ` · ${esc(v.flag)}` : ""}</div>` : ""}
        ${zones ? `<div class="vessel-zones">${zones}</div>` : ""}
      </div>
      ${canEdit ? `<button class="text-btn danger vessel-del" data-id="${v.id}" title="Удалить из флота">✕</button>` : ""}
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
    if (m.getPopup()) { if (!m.isPopupOpen()) m.setPopupContent(vesselPopup(v)); }
    else {
      m.bindPopup(vesselPopup(v), { minWidth: 300, maxWidth: 320, className: "sc-popup" });
      m.on("popupopen", (e) => hydrateCard(e.popup, () => selectVessel(v.mmsi, false)));
    }
  }
  for (const [mmsi, m] of markers) if (!live.has(mmsi)) { vesselsLayer.removeLayer(m); markers.delete(mmsi); }
}

function vesselPopup(v) {
  const p = v.position;
  const ageS = p ? Math.round((Date.now() - new Date(p.ts)) / 1000) : null;
  const d = { ...v, sog: p?.sog, cog: p?.cog };
  const fleets = state.fleets.filter((f) => f.vessels.some((x) => x.mmsi === v.mmsi)).map((f) => f.name);
  return shipCard(d, zoneLines(v.status.inside, v.status.near), {
    ageS, actions: `<span class="sc-tag">Флот: ${esc(fleets.join(", "))}</span>`,
  });
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

/* ------------------------------------------------------------------ карточка судна */
// Рисунок-заглушка, если фото нет: силуэт по типу судна, цвет — цвет категории на карте
function shipSilhouette(cat) {
  const c = TRAFFIC_COLORS[cat] || TRAFFIC_COLORS.other;
  const hull = {
    yacht: "M40 92 L250 92 L272 76 L60 76 Z",
    fishing: "M80 92 L220 92 L236 72 L70 72 Z",
    service: "M90 92 L210 92 L226 70 L84 70 Z",
  }[cat] || "M22 94 L262 94 L282 70 L14 70 Z";
  const top = {
    cargo: `<rect x="40" y="46" width="22" height="24" fill="#4a6b85"/><rect x="64" y="40" width="22" height="30" fill="#8a5a44"/>
      <rect x="88" y="46" width="22" height="24" fill="#3f6f5a"/><rect x="112" y="40" width="22" height="30" fill="#4a6b85"/>
      <rect x="136" y="46" width="22" height="24" fill="#8a7a44"/><rect x="160" y="40" width="22" height="30" fill="#6b4a85"/>
      <rect x="196" y="30" width="34" height="40" fill="#d8dee2"/><rect x="200" y="36" width="26" height="5" fill="#1d2b33"/>`,
    tanker: `<rect x="40" y="62" width="150" height="8" fill="#5b6b73"/><path d="M50 62 V54 M80 62 V54 M110 62 V54 M140 62 V54 M170 62 V54" stroke="#9aa8b0" stroke-width="3"/>
      <rect x="206" y="34" width="38" height="36" fill="#d8dee2"/><rect x="210" y="40" width="30" height="5" fill="#1d2b33"/><rect x="232" y="22" width="9" height="14" fill="${c}"/>`,
    passenger: `<rect x="40" y="56" width="220" height="14" fill="#e8edf0"/><rect x="56" y="42" width="180" height="14" fill="#dfe5e8"/>
      <rect x="76" y="30" width="130" height="12" fill="#d3dade"/><path d="M46 63 H252 M62 49 H230" stroke="#2a3a44" stroke-width="4" stroke-dasharray="8 5"/>`,
    yacht: `<path d="M86 76 L104 60 L226 60 L240 76 Z" fill="#eef2f4"/><path d="M118 60 L132 48 L206 48 L214 60 Z" fill="#e2e8eb"/>
      <path d="M110 66 H226" stroke="#1d2b33" stroke-width="5"/><path d="M138 54 H204" stroke="#1d2b33" stroke-width="4"/>`,
    fishing: `<rect x="176" y="46" width="34" height="26" fill="#d8dee2"/><rect x="180" y="52" width="26" height="5" fill="#1d2b33"/>
      <path d="M120 72 V30 L90 64 M120 34 L150 64" stroke="#9aa8b0" stroke-width="3" fill="none"/>`,
    service: `<rect x="120" y="44" width="46" height="26" fill="#d8dee2"/><rect x="126" y="34" width="34" height="12" fill="#e5eaed"/>
      <rect x="130" y="38" width="26" height="5" fill="#1d2b33"/><rect x="172" y="50" width="10" height="20" fill="${c}"/>`,
  }[cat] || `<rect x="190" y="40" width="40" height="30" fill="#d8dee2"/><rect x="194" y="46" width="32" height="5" fill="#1d2b33"/>`;
  return `<svg viewBox="0 0 300 120" class="sc-sil" aria-hidden="true">
    <rect width="300" height="120" fill="url(#sc-sky)"/>
    <defs><linearGradient id="sc-sky" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#1b3442"/><stop offset="1" stop-color="#10232d"/></linearGradient></defs>
    ${top}<path d="${hull}" fill="${c}"/><path d="M0 98 Q75 92 150 98 T300 98 V120 H0 Z" fill="#0b1a22" opacity=".9"/></svg>`;
}

const flagImg = (iso, title) => (iso
  ? `<img class="sc-flag" src="https://flagcdn.com/w40/${esc(iso)}.png" alt="" title="${esc(title || "")}" loading="lazy">` : "");

const fmtAge = (s) => (s == null ? "—" : s < 60 ? "только что" : s < 3600 ? `${Math.round(s / 60)} мин назад`
  : s < 172800 ? `${Math.round(s / 3600)} ч назад` : `${Math.round(s / 86400)} сут назад`);

/**
 * d — данные судна (общий слой или флот), zonesHtml — строки про зоны,
 * opts.actions — HTML кнопки «Во флот» / пометки, opts.ageS — давность позиции в секундах.
 */
function shipCard(d, zonesHtml, opts = {}) {
  const sogCog = `${d.sog != null ? d.sog.toFixed(1) + " уз" : "—"} / ${d.cog != null ? Math.round(d.cog) + "°" : "—"}`;
  const size = d.length ? `${Math.round(d.length)} × ${d.beam ? Math.round(d.beam) : "?"} м` : "—";
  const mt = `https://www.marinetraffic.com/en/ais/details/ships/mmsi:${encodeURIComponent(d.mmsi)}`;
  return `<div class="sc" data-imo="${esc(d.imo || "")}" data-mmsi="${esc(d.mmsi)}" data-cat="${esc(d.category || "other")}">
    <div class="sc-head">${flagImg(d.flag_iso, d.flag)}
      <div><div class="sc-name">${esc(d.name || "Без названия")}</div>
      <div class="sc-type"><i class="dot" style="background:${TRAFFIC_COLORS[d.category] || TRAFFIC_COLORS.other}"></i> ${esc(d.type_name || "Тип не передан")}</div></div></div>
    <div class="sc-photo">${shipSilhouette(d.category)}<div class="sc-credit"></div></div>
    <div class="sc-actions">${opts.actions || ""}
      <button class="btn ghost sc-track" type="button">Трек</button>
      <a class="btn" href="${mt}" target="_blank" rel="noopener">Подробнее ↗</a></div>
    <div class="sc-route"><div><span class="muted small">Пункт назначения</span><b>${esc(d.destination || "не указан")}</b></div>
      <div class="sc-eta"><span class="muted small">ETA</span><b>${esc(d.eta || "—")}</b></div></div>
    <div class="sc-zones">${zonesHtml}</div>
    <div class="sc-grid">
      <div><span>Статус</span><b>${esc(d.nav_status || "—")}</b></div>
      <div><span>Скорость / курс</span><b>${sogCog}</b></div>
      <div><span>Осадка</span><b>${d.draught ? d.draught.toFixed(1) + " м" : "—"}</b></div>
      <div><span>Размеры</span><b>${size}</b></div>
      <div><span>Флаг</span><b>${esc(d.flag || "—")}</b></div>
      <div><span>IMO / MMSI</span><b>${esc(d.imo || "—")}<br>${esc(d.mmsi)}</b></div>
    </div>
    <div class="sc-foot">Получено: <b>${fmtAge(opts.ageS)}</b>${d.ais_class ? ` · AIS класс ${esc(d.ais_class)}` : ""}${d.callsign ? ` · позывной ${esc(d.callsign)}` : ""}</div>
    <div class="sc-links">Тоннаж, год постройки, владелец:
      <a href="${mt}" target="_blank" rel="noopener">MarineTraffic</a> ·
      <a href="https://www.vesselfinder.com/vessels/details/${encodeURIComponent(d.imo || d.mmsi)}" target="_blank" rel="noopener">VesselFinder</a>
      ${d.imo ? ` · <a href="https://www.equasis.org/" target="_blank" rel="noopener" title="Бесплатно после регистрации, поиск по IMO ${esc(d.imo)}">Equasis</a>` : ""}</div>
  </div>`;
}

// После открытия карточки: фото с Wikimedia Commons (если есть) и кнопка трека
async function hydrateCard(popup, onTrack) {
  const el = popup.getElement()?.querySelector(".sc");
  if (!el) return;
  el.querySelector(".sc-track").onclick = onTrack;
  const imo = el.dataset.imo;
  if (!imo) return;
  try {
    const { photo } = await api(`/api/photo/${encodeURIComponent(imo)}`);
    if (!photo || !el.isConnected) return;
    const box = el.querySelector(".sc-photo");
    const img = new Image();
    img.alt = "";
    img.onload = () => {
      box.querySelector(".sc-sil")?.remove();
      box.prepend(img);
      box.querySelector(".sc-credit").innerHTML = `<a href="${esc(photo.page)}" target="_blank" rel="noopener">© ${esc(photo.author || "Wikimedia Commons")}${photo.license ? `, ${esc(photo.license)}` : ""}</a>`;
      popup.update();
    };
    img.src = photo.url;
  } catch { /* фото необязательно */ }
}

function zoneLines(inside, near) {
  return [
    ...inside.map((z) => `<div class="zin">● В зоне: ${esc(z.name)}${z.since ? ` <span class="muted">с ${fmtTime(z.since)}</span>` : ""}</div>`),
    ...near.map((z) => `<div class="znr">◌ ${esc(z.name)}: ${Math.round(z.distance_nm)} миль</div>`),
  ].join("") || `<div class="zok">Вне зон риска</div>`;
}

function drawTrack(points, color = "#4fc3d9") {
  trackLayer.clearLayers();
  const pts = points.map((p) => [p.lat, p.lon]);
  if (pts.length < 2) { toast("Трек пока короткий", "Точки копятся раз в 10 минут, пока судно передаёт AIS."); return; }
  L.polyline(pts, { color, weight: 2, opacity: 0.9, dashArray: "2 6" }).addTo(trackLayer);
  pts.slice(0, -1).forEach((ll) => L.circleMarker(ll, { radius: 2, weight: 0, fillColor: color, fillOpacity: 0.9 }).addTo(trackLayer));
}

/* ------------------------------------------------------------------ все суда (AIS) */
const TRAFFIC_COLORS = {
  cargo: "#6fa8dc", tanker: "#c27ba0", passenger: "#f6b26b", yacht: "#5fe0c8",
  fishing: "#93c47d", service: "#ffd966", other: "#8a9ba3",
};
const trafficRenderer = mapCanvas;
const trafficLayer = L.layerGroup().addTo(map);
state.traffic = { on: null, cats: new Set(Object.keys(TRAFFIC_COLORS)), data: null, seq: 0 };

function trafficDefault() {
  // по умолчанию слой включён, пока во флотах нет судов; дальше — как выбрал пользователь
  const saved = localStorageGet("showTraffic");
  return saved === null ? allVessels().length === 0 : saved === "1";
}

async function loadTraffic() {
  const t = state.traffic;
  if (t.on === null) t.on = trafficDefault();
  $("#traffic-toggle").checked = t.on;
  if (!t.on) { trafficLayer.clearLayers(); t.data = null; renderTrafficCats(); return; }
  const b = map.getBounds();
  const span = b.getEast() - b.getWest();
  const norm = (x) => ((x + 540) % 360) - 180;
  const bbox = span >= 360 ? "-180,-90,180,90"
    : [norm(b.getWest()), Math.max(b.getSouth(), -90), norm(b.getEast()), Math.min(b.getNorth(), 90)].map((x) => x.toFixed(3)).join(",");
  const seq = ++t.seq;
  const cats = [...t.cats].join(",");
  const data = await api(`/api/traffic?bbox=${bbox}&zoom=${map.getZoom()}&cats=${cats}&limit=5000`);
  if (seq !== t.seq) return;           // пришёл ответ на устаревший запрос
  t.data = data;
  renderTraffic();
}

function renderTraffic() {
  const t = state.traffic;
  trafficLayer.clearLayers();
  renderTrafficCats();
  if (!t.on || !t.data) return;
  for (const c of t.data.clusters) {
    L.marker([c.lat, c.lon], { icon: clusterIcon(c), zIndexOffset: 500 })
      .bindTooltip(clusterTooltip(c), { direction: "top", offset: [0, -14] })
      .on("click", () => map.fitBounds([[c.bbox[1], c.bbox[0]], [c.bbox[3], c.bbox[2]]], { padding: [50, 50], maxZoom: 12 }))
      .addTo(trafficLayer);
  }
  const fleet = new Set(allVessels().map((v) => v.mmsi));
  for (const [mmsi, lat, lon, cog, sog, cat, name, len] of t.data.ships) {
    if (fleet.has(mmsi)) continue;
    new ShapeMarker([lat, lon], {
      shape: cat === "yacht" ? "yacht" : "ship", rotation: cog ?? 0,
      renderer: trafficRenderer, radius: (len > 200 ? 4.5 : len > 100 ? 3.6 : 2.8) * iconScale(),
      weight: 0.6, color: "#0b1a22", fillColor: TRAFFIC_COLORS[cat], fillOpacity: sog != null && sog < 0.5 ? 0.55 : 0.95,
    }).bindTooltip(esc(name || `MMSI ${mmsi}`), { direction: "top", offset: [0, -4] })
      .on("click", (e) => openTrafficShip(mmsi, e.latlng))
      .addTo(trafficLayer);
  }
}

// Значки судов растут с масштабом: на обзорной карте мелкие, при приближении — крупнее
function iconScale() {
  const z = map.getZoom();
  return z <= 4 ? 0.9 : z <= 6 ? 1.25 : z <= 8 ? 1.6 : z <= 10 ? 1.85 : 2.1;
}

// Группа судов: кружок с числом; цвет обводки — самая частая категория в группе
function clusterIcon(c) {
  const size = c.n < 10 ? 26 : c.n < 100 ? 32 : c.n < 1000 ? 38 : 44;
  const top = Object.entries(c.cats).sort((a, b) => b[1] - a[1])[0][0];
  const label = c.n >= 1000 ? `${(c.n / 1000).toFixed(c.n >= 10000 ? 0 : 1)}k` : c.n;
  return L.divIcon({ className: "tcl", iconSize: [size, size],
    html: `<div style="border-color:${TRAFFIC_COLORS[top]}">${label}</div>` });
}

function clusterTooltip(c) {
  const names = (state.traffic.data || {}).categories || {};
  return `<b>${c.n} судов</b><br>` + Object.entries(c.cats).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `<i class="dot" style="background:${TRAFFIC_COLORS[k]}"></i> ${esc(names[k] || k)}: ${n}`).join("<br>");
}

function renderTrafficCats() {
  const t = state.traffic;
  const d = t.data;
  $("#traffic-meta").textContent = !t.on ? "Слой выключен"
    : !d ? "Загружаю…"
      : !d.enabled ? "Нужен живой AIS: ключ aisstream.io в .env"
        : `В видимой области: ${d.total}. Число в кружке — сколько судов рядом; нажмите, чтобы приблизить.`;
  const cats = (d && d.categories) || {};
  $("#traffic-cats").innerHTML = t.on ? Object.keys(TRAFFIC_COLORS).map((c) => `
    <button class="fchip ${t.cats.has(c) ? "on" : ""}" data-cat="${c}" aria-pressed="${t.cats.has(c)}">
      <i class="dot" style="background:${TRAFFIC_COLORS[c]}"></i>${esc(cats[c] || c)}
      <span class="muted">${d ? d.counts[c] : ""}</span></button>`).join("") : "";
  $$("#traffic-cats .fchip").forEach((b) => b.onclick = () => {
    const c = b.dataset.cat;
    if (t.cats.has(c)) t.cats.delete(c); else t.cats.add(c);
    loadTraffic().catch(() => {});
  });
}

async function openTrafficShip(mmsi, latlng) {
  const popup = L.popup({ minWidth: 300, maxWidth: 320, className: "sc-popup" }).setLatLng(latlng)
    .setContent("Загружаю…").openOn(map);
  try {
    const s = await api(`/api/traffic/${encodeURIComponent(mmsi)}`);
    const zs = s.zones || [];
    const actions = s.fleets.length ? `<span class="sc-tag">Флот: ${s.fleets.map(esc).join(", ")}</span>`
      : state.fleets.some((f) => f.access !== "view") ? `<select class="sc-fleet">${state.fleets.filter((f) => f.access !== "view").map((f) => `<option value="${f.id}">${esc(f.name)}</option>`).join("")}</select>
        <button class="btn" type="button" data-add-fleet>Во флот</button>` : "";
    popup.setContent(shipCard(s, zoneLines(zs.filter((z) => z.status === "inside"), zs.filter((z) => z.status !== "inside")),
      { actions, ageS: s.age_s }));
    hydrateCard(popup, async () => drawTrack((await api(`/api/traffic/${encodeURIComponent(mmsi)}/track`)).points, "#f6b26b"));
    const btn = popup.getElement()?.querySelector("[data-add-fleet]");
    if (btn) btn.onclick = async () => {
      const fid = +popup.getElement().querySelector(".sc-fleet").value;
      try {
        await api(`/api/fleets/${fid}/vessels`, { json: { mmsi: s.mmsi, imo: s.imo || "", name: s.name || "" } });
        toast("Судно добавлено во флот", s.name || s.mmsi);
        map.closePopup(popup);
        await loadFleets();
        renderTraffic();
      } catch (e) { toast("Не удалось добавить", e.message, "critical"); }
    };
  } catch (e) { popup.setContent(esc(e.message)); }
}

/* ------------------------------------------------------------------ поиск судна */
let searchTimer = null;
$("#ship-q").oninput = (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim();
  if (q.length < 2) { $("#ship-results").hidden = true; return; }
  searchTimer = setTimeout(() => runShipSearch(q).catch(() => {}), 250);
};
$("#ship-q").onkeydown = (e) => {
  if (e.key === "Enter") $("#ship-results .sr-item")?.click();
  if (e.key === "Escape") { $("#ship-results").hidden = true; e.target.blur(); }
};
document.addEventListener("click", (e) => { if (!e.target.closest(".ship-search")) $("#ship-results").hidden = true; });

async function runShipSearch(q) {
  const { items } = await api(`/api/ships/search?q=${encodeURIComponent(q)}`);
  if ($("#ship-q").value.trim() !== q) return;         // пользователь уже печатает дальше
  const box = $("#ship-results");
  box.innerHTML = items.length ? items.map((s, i) => `
    <button class="sr-item" type="button" data-i="${i}">
      ${flagImg(s.flag_iso, s.flag)}
      <span class="sr-main"><b>${esc(s.name || "Без названия")}</b>${s.in_fleet ? ` <span class="tag">во флоте</span>` : ""}
        <span class="muted small">${esc(s.type_name || "")} · MMSI ${esc(s.mmsi)}${s.imo ? ` · IMO ${esc(s.imo)}` : ""}</span></span>
      <span class="muted small">${s.lat != null ? fmtAge(s.age_s) : "нет позиции"}</span>
    </button>`).join("")
    : `<div class="sr-empty muted small">Не найдено. Судно может быть вне зоны покрытия береговых станций AIS.</div>`;
  box.hidden = false;
  $$(".sr-item", box).forEach((b) => b.onclick = () => {
    const s = items[+b.dataset.i];
    box.hidden = true;
    if (s.lat == null) { toast("Позиции нет", "Судно сейчас не передаёт AIS или вне зоны покрытия."); return; }
    map.flyTo([s.lat, s.lon], Math.max(map.getZoom(), 11), { duration: 0.8 });
    trackLayer.clearLayers();
    L.circleMarker([s.lat, s.lon], { radius: 14, weight: 2.5, color: "#ffffff", fill: false, dashArray: "4 3" })
      .addTo(trackLayer);
    setTimeout(() => {
      if (s.in_fleet) selectVessel(s.mmsi, false);
      else openTrafficShip(s.mmsi, L.latLng(s.lat, s.lon));
    }, 900);
  });
}

let trafficTimer = null;
map.on("moveend", () => { clearTimeout(trafficTimer); trafficTimer = setTimeout(() => loadTraffic().catch(() => {}), 400); });
$("#traffic-toggle").onchange = (e) => {
  state.traffic.on = e.target.checked;
  localStorageSet("showTraffic", e.target.checked ? "1" : "0");
  loadTraffic().catch((err) => toast("Ошибка", err.message, "critical"));
};

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
    await loadTraffic();
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
$("#legend-toggle").onclick = () => {
  const collapsed = $("#legend").classList.toggle("collapsed");
  localStorageSet("legendCollapsed", collapsed ? "1" : "0");
};
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
$("#btn-share-add").onclick = async () => {
  const f = currentFleet(); const u = $("#share-user").value;
  if (!f || !u) return;
  await shareFleet(f.id, { username: u, can_edit: $("#share-edit").checked });
  $("#share-edit").checked = false;
  toast("Доступ открыт", `${u} увидит флот «${f.name}»`);
};
$("#btn-fleet-leave").onclick = async () => {
  const f = currentFleet(); if (!f) return;
  if (!confirm(`Убрать флот «${f.name}» из вашего списка? Владелец сможет открыть доступ снова.`)) return;
  await shareFleet(f.id, { username: state.me, remove: true });
  state.fleetId = null;
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
    const r = await api(`/api/fleets/${f.id}/vessels`, { json: fd });
    e.target.reset();
    $("#mmsi-check").innerHTML = "";
    const c = r.check || {};
    if (c.found) {
      toast(`Добавлено: ${c.name || fd.mmsi}`, [c.type_name, c.flag, c.has_position ? `позиция ${fmtAge(c.age_s)}` : "позиции пока нет",
        ...c.warnings].filter(Boolean).join(" · "), c.warnings.length ? "warning" : "info");
    } else {
      toast("Судно добавлено, но в потоке AIS его нет", c.warnings.join(" "), "warning");
    }
    await loadFleets();
  } catch (err) { toast("Не удалось добавить", err.message, "critical"); }
};

// Проверка номера прямо при вводе: есть ли судно в потоке AIS и не опечатка ли это
let checkTimer = null;
function scheduleVesselCheck() {
  clearTimeout(checkTimer);
  const form = $("#form-vessel");
  const mmsi = form.mmsi.value.replace(/\D/g, "");
  const imo = form.imo.value.replace(/\D/g, "");
  const box = $("#mmsi-check");
  if (mmsi.length !== 9) { box.innerHTML = mmsi ? `<span class="muted">MMSI — 9 цифр (сейчас ${mmsi.length})</span>` : ""; return; }
  checkTimer = setTimeout(async () => {
    try {
      const c = await api(`/api/ships/check?mmsi=${mmsi}&imo=${imo}`);
      const head = c.found
        ? `<div class="ok">✓ ${flagImg(c.flag_iso, c.flag)} <b>${esc(c.name || "без названия")}</b> · ${esc(c.type_name)}${c.length ? ` · ${Math.round(c.length)} м` : ""} · ${c.has_position ? `позиция ${fmtAge(c.age_s)}` : "позиции пока нет"}</div>`
        : `<div class="warn">Не найдено в потоке AIS${c.flag ? ` (флаг по номеру: ${esc(c.flag)})` : ""}</div>`;
      box.innerHTML = head + c.warnings.filter((w) => c.found || !w.startsWith("Судна с таким")).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("")
        + (c.found ? "" : `<div class="muted">Возможна опечатка, или судно давно вне зоны покрытия. Добавить всё равно можно — позиция появится, когда оно выйдет на связь.</div>`);
    } catch (err) { box.textContent = err.message; }
  }, 300);
}
$("#form-vessel").mmsi.addEventListener("input", scheduleVesselCheck);
$("#form-vessel").imo.addEventListener("input", scheduleVesselCheck);

async function uploadFleetFile(file) {
  const f = currentFleet(); if (!f || !file) return;
  const fd = new FormData(); fd.append("file", file);
  try {
    const r = await api(`/api/fleets/${f.id}/upload`, { method: "POST", body: fd });
    let body = `Добавлено: ${r.added}, уже были: ${r.exists}`;
    if (r.skipped_count) body += `. Пропущено без MMSI: ${r.skipped_count} (${r.skipped_no_mmsi.slice(0, 5).join(", ")}${r.skipped_count > 5 ? "…" : ""})`;
    if (r.not_found_count) body += `. Нет в потоке AIS (проверьте номера): ${r.not_found_count} — ${r.not_found.slice(0, 5).join(", ")}${r.not_found_count > 5 ? "…" : ""}`;
    toast("Файл обработан", body, r.skipped_count || r.not_found_count ? "warning" : "info");
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
// легенда длинная: по умолчанию свёрнута, раскрытие запоминается
if (window.innerWidth < 820 || localStorageGet("legendCollapsed") !== "0") $("#legend").classList.add("collapsed");
renderLegend();
(async () => {
  await refreshAll(true);
  const vs = allVessels().filter((v) => v.position);
  if (vs.length) map.fitBounds(L.latLngBounds(vs.map((v) => [v.position.lat, v.position.lon])).pad(0.5), { maxZoom: 6 });
  setInterval(() => refreshAll(false), 10000);
})();
