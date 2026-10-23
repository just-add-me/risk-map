/* FleetRisk — интерфейс. Без сборщиков и фреймворков: просто Leaflet + fetch. */
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
// Сверху и снизу карту не утащить за край мира (85° — предел проекции); по долготе мир повторяется
const WORLD_LAT = 85.0511;
const map = L.map("map", { zoomControl: true, worldCopyJump: true, minZoom: 2,
  maxBounds: [[-WORLD_LAT, -1e5], [WORLD_LAT, 1e5]], maxBoundsViscosity: 1 }).setView([25, 40], 4);
// мир по высоте не меньше окна карты: иначе при отдалении сверху и снизу оставались бы пустые полосы
function fitMinZoom() {
  const z = Math.max(2, Math.log2(map.getSize().y / 256));
  map.setMinZoom(z);
  if (map.getZoom() < z) map.setZoom(z, { animate: false });
}
fitMinZoom();
map.on("resize", fitMinZoom);
// Плавные переходы к судну, событию, зоне. Обычный fitBounds/setView при разнице масштабов больше 4
// не анимируется, а перескакивает — с обзора мира к судну это резкий скачок. Перелёт — всегда плавный.
const FLY = { duration: 1.1, easeLinearity: 0.3 };
function smoothFit(bounds, opts = {}) {
  const b = L.latLngBounds(bounds);
  const target = map.getBoundsZoom(b, false, L.point(0, 0));
  // уже почти на месте — короткое движение без «подлёта» через отдаление
  if (Math.abs(target - map.getZoom()) < 1 && map.getBounds().contains(b)) return map.fitBounds(b, { ...opts, animate: true });
  return map.flyToBounds(b, { ...opts, ...FLY });
}
const smoothTo = (latlng, zoom) => map.flyTo(latlng, zoom, FLY);
// Подложка Esri (работает без ключа; CARTO стал требовать API-ключ) — тёмная или светлая, по теме
const isLight = () => document.documentElement.dataset.theme === "light";
const baseUrl = () => `https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_${isLight() ? "Light" : "Dark"}_Gray_Base/MapServer/tile/{z}/{y}/{x}`;
const baseLayer = L.tileLayer(baseUrl(), {
  attribution: 'Tiles &copy; <a href="https://www.esri.com/">Esri</a> &mdash; Esri, HERE, Garmin, &copy; OpenStreetMap contributors',
  maxNativeZoom: 16, maxZoom: 18,
}).addTo(map);

// Зоны, порты и суда — на одном холсте: тогда клик попадает в верхний объект (судно → порт → зона).
// С отдельными холстами или SVG зона перехватывала клики по портам внутри неё.
const mapCanvas = L.canvas({ padding: 0.5, tolerance: 3 });
// Зоны рисуются «как в играх»: еле заметная заливка, мягкое свечение от границы внутрь зоны
// (несколько обводок с обрезкой по контуру, от широкой и прозрачной к узкой и яркой) и чёткая граница.
// Зоны у 180-го меридиана разрезаны по нему (так устроен GeoJSON): отрезки, лежащие точно на
// меридиане 180° (и его копиях), не обводятся и не светятся — иначе виден шов.
const GLOW = [[14, 0.05], [8, 0.08], [4, 0.13]];         // [ширина, прозрачность] — от края внутрь

mapCanvas._updatePoly = function (layer, closed) {
  const proto = L.Canvas.prototype, o = layer.options;
  if (!closed || !this._drawing) return proto._updatePoly.call(this, layer, closed);
  const stroke = o.stroke;
  o.stroke = false;
  proto._updatePoly.call(this, layer, closed);          // заливка
  o.stroke = stroke;
  const parts = layer._parts;
  if (!parts.length || !stroke) return;
  const ctx = this._ctx;
  const W = this._map.getPixelWorldBounds().getSize().x;
  const x0 = this._map.latLngToLayerPoint([0, 180]).x;
  const seam = (x) => { const d = (((x - x0) % W) + W) % W; return d <= 1.5 || W - d <= 1.5; };   // ±1 px округления
  const edges = () => {
    ctx.beginPath();
    parts.forEach((ring) => {
      ring.forEach((p, j) => {
        if (!j) { ctx.moveTo(p.x, p.y); return; }
        const a = ring[j - 1];
        ctx[seam(a.x) && seam(p.x) ? "moveTo" : "lineTo"](p.x, p.y);
      });
      const a = ring[ring.length - 1], b = ring[0];      // замыкающий отрезок
      ctx[seam(a.x) && seam(b.x) ? "moveTo" : "lineTo"](b.x, b.y);
    });
  };
  if (o.glow) {
    ctx.save();
    ctx.beginPath();                                     // обрезка по контуру: свечение только внутрь
    parts.forEach((ring) => { ring.forEach((p, j) => ctx[j ? "lineTo" : "moveTo"](p.x, p.y)); ctx.closePath(); });
    ctx.clip(o.fillRule || "evenodd");
    edges();
    ctx.setLineDash([]);
    ctx.strokeStyle = o.color;
    ctx.lineJoin = "round";
    // при приближении узкие 12-мильные полосы на экране тонкие — свечение сужаем, иначе полоса светится целиком
    const z = this._map.getZoom(), k = z <= 5 ? 1 : Math.max(0.3, 1 - (z - 5) * 0.25);
    for (const [w, a] of GLOW) {
      ctx.globalAlpha = a * o.glow;
      ctx.lineWidth = w * k;
      ctx.stroke();
    }
    ctx.restore();
  }
  edges();
  const fill = o.fill;
  o.fill = false;
  this._fillStroke(ctx, layer);                          // чёткая граница
  o.fill = fill;
};
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
// icon — эмблема в центре зоны: war — скрещённые мечи (военные риски), ice — айсберг (льды, INL)
const ZONE_TYPES = {
  country: { layer: "jwc", label: "Страна (порты и 12 миль)", color: "#ff5a4e", icon: "war" },
  sea: { layer: "jwc", label: "Морской район", color: "#ff9440", icon: "war" },
  conditional: { layer: "jwc", label: "Условная (offshore)", color: "#b48cff", dash: "4 4", icon: "war" },
  woa: { layer: "ibf", label: "WOA — военные действия", color: "#e8368f", icon: "war" },
  hra: { layer: "ibf", label: "HRA — высокий риск", color: "#f783ac", icon: "war" },
  erz: { layer: "ibf", label: "ERZ — расширенный риск", color: "#20c997", icon: "war" },
  erz2: { layer: "ibf", label: "ERZ-2 — порты", color: "#c0eb75", icon: "war" },
  inl_perm: { layer: "inl", label: "Круглый год", color: "#4dabf7", icon: "ice" },
  inl_season: { layer: "inl", label: "Сезонные (лёд)", color: "#a5d8ff", dash: "5 4", icon: "ice" },
};

// Эмблемы зон — маленькие SVG: скрещённые мечи и айсберг
const ZONE_GLYPH = {
  war: `<path d="M5 19 19 5M19 19 5 5" /><path d="M4.5 14.5l5 5M19.5 14.5l-5 5" /><circle cx="3.6" cy="20.4" r="1.2" class="f"/><circle cx="20.4" cy="20.4" r="1.2" class="f"/>`,
  // айсберг: верхушка над водой, линия воды, большая подводная часть (бледнее)
  ice: `<path d="M6.5 11.5 9.5 5.5l2 2 2.5-3.5 3.5 7.5" class="f" /><path d="M2.5 11.5h19" /><path d="M5 11.5l1.5 6 5 3 5.5-2.5 2-6.5" opacity=".55" />`,
};
function zoneGlyph(kind, color, size = 14) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="${color}" stroke-width="2.1"
    stroke-linecap="round" stroke-linejoin="round" style="--c:${color}">${ZONE_GLYPH[kind]}</svg>`;
}

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

// Склеенные «территории» (с сервера, fc.outlines): зоны JWC и IBF одного вида — один контур, граница
// только по внешнему краю, без наложений между видами. Отдельные зоны лежат над ними невидимыми —
// для нажатия, выделения и пометки «изменена». При поиске по названию или «только изменённые» — по отдельности.
const territoryLayer = L.geoJSON(null, { renderer: mapCanvas, interactive: false, style: territoryStyle }).addTo(map);
const dissolved = () => !!state.zonesFC?.outlines?.length && !state.zoneFilter.q.trim() && !state.zoneFilter.changedOnly;

// «Внутренние воды» (Украина, юг Беларуси, Дон): контур — вся территория, чтобы речные порты были в зоне;
// рисуем пунктиром без заливки — зона по рекам и портам, а не «вся страна»
const isInland = (p) => !!p.inland || String(p.template || "").startsWith("inland_");
const INLAND = { fillOpacity: 0, dashArray: "6 5", glow: 0, weight: 1.3 };
function territoryStyle(f) {
  const t = ZONE_TYPES[f.properties.type], layer = f.properties.layer;
  const s = lightZone({ color: t.color, weight: layer === "ibf" ? 1.6 : 1.2, opacity: 0.95, fillColor: t.color,
    fillOpacity: layer === "ibf" ? 0 : 0.07, glow: layer === "ibf" ? 0.8 : 1, dashArray: t.dash || null, interactive: false });
  return isInland(f.properties) ? { ...s, ...INLAND } : s;
}
// светлая тема: зоны тонкой линией без неонового свечения, заливка чуть плотнее (на светлой карте 7% не видно)
function lightZone(s) {
  if (!isLight()) return s;
  return { ...s, glow: 0, weight: Math.min(s.weight || 1.2, 1.6), fillOpacity: s.fillOpacity ? Math.min(0.2, s.fillOpacity * 1.7) : s.fillOpacity };
}

function zoneStyle(f) {
  const p = f.properties;
  const t = ZONE_TYPES[zoneType(p)];
  const layer = layerOf(p);
  if (dissolved() && layer !== "inl") {        // видна «территория», сама зона — только для нажатия и выделения
    if (state.selectedZone === p.id) return lightZone({ color: t.color, weight: 2.2, opacity: 1, fillColor: t.color, fillOpacity: 0.14, glow: 1.6 });
    if (isChanged(p)) return { color: t.color, weight: 1.8, opacity: 0.9, dashArray: "7 4", fillOpacity: 0, glow: 0 };
    return { stroke: false, fill: true, fillOpacity: 0, glow: 0 };
  }
  const s = { color: t.color, weight: layer === "ibf" ? 1.6 : 1.2, opacity: 0.95, fillColor: t.color,
    // IBF лежит поверх JWC — без заливки (только граница и свечение), иначе полупрозрачные слои складываются в кашу
    fillOpacity: { jwc: 0.07, ibf: 0, inl: 0.035 }[layer], glow: { jwc: 1, ibf: 0.8, inl: 0.7 }[layer] };
  if (t.dash) s.dashArray = t.dash;
  if (layer === "inl" && !inSeason(p)) Object.assign(s, { fillOpacity: 0.02, glow: 0.35, opacity: 0.6 });   // вне сезона — бледнее
  if (isChanged(p)) Object.assign(s, { weight: s.weight + 0.6, dashArray: "7 4" });     // изменена: пунктир цветом самой зоны
  if (isInland(p)) Object.assign(s, INLAND);
  if (state.selectedZone === p.id) Object.assign(s, { weight: 2.2, dashArray: null, fillOpacity: 0.14, glow: 1.6 });
  return lightZone(s);
}

function selectZone(id) {
  state.selectedZone = id;
  zonesLayer.setStyle(zoneStyle);
  updateZoneEmblems();
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

// Цвет флота: кольцо вокруг значка судна, плашка в ленте, пункт легенды. Не красный/жёлтый/зелёный —
// эти цвета у статуса судна (в зоне / рядом / вне зон).
const FLEET_COLORS = ["#4dabf7", "#748ffc", "#b197fc", "#da77f2", "#f783ac", "#3bc9db", "#f1f3f5", "#e8a87c", "#a9746e", "#868e96"];
// цвет, выбранный владельцем флота (fleets.color), иначе — из палитры по номеру флота
const fleetColor = (fid) => state.fleets.find((f) => f.id === +fid)?.color || FLEET_COLORS[(+fid || 0) % FLEET_COLORS.length];
const fleetsOf = (mmsi) => state.fleets.filter((f) => f.vessels.some((v) => v.mmsi === mmsi));
state.hiddenFleets = new Set();
function fleetChips(mmsi) {
  return fleetsOf(mmsi).map((f) => `<span class="fleet-chip" style="--fc:${fleetColor(f.id)}">${esc(f.name)}</span>`).join("");
}

// Значок судна флота: стрелка залита цветом флота (fill), статус — «лампочка» у кормы:
// красная — в зоне, жёлтая — приближается, зелёная — вне зон. badges — санкции, задержание, новости.
const STATUS_COLORS = { inside: "#ff5a4e", near: "#f5b13d", safe: "#4fd18b" };
function shipIcon(level, cog, selected, badges = [], fill = null) {
  const status = STATUS_COLORS[level] || "#7f9aa6";
  const size = selected ? 28 : 23;
  const rot = Number.isFinite(cog) ? cog : 0;
  return L.divIcon({
    className: "ship-icon", iconSize: [size, size], iconAnchor: [size / 2, size / 2],
    html: `<svg width="${size}" height="${size}" viewBox="0 0 20 20" style="transform:rotate(${rot}deg)">
      <path d="M10 1 16 18 10 14 4 18z" fill="${fill || status}" stroke="${selected ? "#fff" : "#0b1a22"}" stroke-width="${selected ? 1.6 : 1}"/>
      ${fill ? `<circle cx="10" cy="16.6" r="2.9" fill="${status}" stroke="#0b1a22" stroke-width="1"/>` : ""}</svg>
      ${badges.length ? `<span class="ship-badges">${badges.map((k) =>
        `<i style="background:${INC_STYLE[k].color}">${INC_STYLE[k].glyph}</i>`).join("")}</span>` : ""}`,
  });
}

// Пометки судна флота: санкции — всегда, задержание — пока не освобождено или 30 дней, тревожные новости — 7 дней
function vesselBadges(v) {
  const sc = v.screening || {};
  const out = [];
  if (sc.sanctions?.length) out.push("sanction");
  const det = sc.detentions?.[0];
  if (det && (!det.released_on || Date.now() - Date.parse(det.detained_on) < 30 * 86400e3)) out.push("detention");
  if ((state.inc?.items || []).some((it) => it.kind === "news_risk" && it.mmsi === v.mmsi
      && Date.now() - Date.parse(it.last_seen) < 7 * 86400e3)) out.push("news_risk");
  return out;
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
  zonesLayer.addData({ ...state.zonesFC, features: [...feats, ...wrapCopies(feats)] });
  zonesLayer.bringToBack();
  territoryLayer.clearLayers();
  if (dissolved()) {
    const outs = state.zonesFC.outlines.filter((o) => state.zoneFilter.types.has(o.properties.type));
    territoryLayer.addData({ type: "FeatureCollection", features: [...outs, ...wrapCopies(outs)] });
    territoryLayer.bringToBack();               // под отдельными зонами (они принимают нажатия)
  }
  state.zoneEmblemFeats = feats.filter((f) => f.properties.label_pt);
  updateZoneEmblems();
  if (document.body.classList.contains("drawing")) setMapInteractive(false);
}

// Эмблема зоны (мечи / айсберг) в её «визуальном центре» — одна на зону, как метка области в играх.
// Только если зона на экране крупная, и без наложений: ближе EMBLEM_GAP пикселей остаётся одна (JWC и IBF важнее INL).
const zoneEmblems = L.layerGroup().addTo(map);
const EMBLEM_MIN_PX = 90, EMBLEM_GAP = 34;
function updateZoneEmblems() {
  zoneEmblems.clearLayers();
  const placed = [];
  const view = map.getBounds().pad(0.2);
  const rank = { jwc: 0, ibf: 1, inl: 2 };
  const feats = (state.zoneEmblemFeats || []).slice().sort((a, b) => rank[layerOf(a.properties)] - rank[layerOf(b.properties)]);
  for (const f of feats) {
    const p = f.properties, [w, s, e, n] = p.bbox;
    const ll = L.latLng(p.label_pt[0], p.label_pt[1]);
    if (!view.contains(ll)) continue;
    const a = map.latLngToContainerPoint([s, w]), b = map.latLngToContainerPoint([n, e]);
    if (Math.max(Math.abs(b.x - a.x), Math.abs(b.y - a.y)) < EMBLEM_MIN_PX) continue;
    const pt = map.latLngToContainerPoint(ll);
    if (placed.some((q) => q.distanceTo(pt) < EMBLEM_GAP)) continue;
    placed.push(pt);
    const t = ZONE_TYPES[zoneType(p)];
    const sel = state.selectedZone === p.id;
    L.marker(ll, {
      icon: L.divIcon({ className: `zone-emblem${sel ? " sel" : ""}${layerOf(p) === "inl" && !inSeason(p) ? " off" : ""}`,
        iconSize: [26, 26], iconAnchor: [13, 13], html: `<span style="--c:${t.color}">${zoneGlyph(t.icon, t.color)}</span>` }),
      zIndexOffset: -500, keyboard: false,
    }).bindTooltip(esc(p.name), { direction: "top", offset: [0, -12] })
      .on("click", () => zonesLayer.eachLayer((l) => {
        if (!l.feature.wrapCopy && l.feature.properties.id === p.id) l.openPopup(ll);
      }))
      .addTo(zoneEmblems);
  }
}
map.on("zoomend moveend", () => updateZoneEmblems());

// Карта повторяет мир по долготе (worldCopyJump), а векторные слои Leaflet — нет: у 180-го меридиана
// (Чукотка, Алеуты, Аляска) зоны обрывались. Зоны у края мира дорисовываем копией со сдвигом на 360°.
function shiftCoords(c, dx) {
  return typeof c[0] === "number" ? [c[0] + dx, ...c.slice(1)] : c.map((x) => shiftCoords(x, dx));
}

function lonRange(c, r = [Infinity, -Infinity]) {
  if (typeof c[0] === "number") { r[0] = Math.min(r[0], c[0]); r[1] = Math.max(r[1], c[0]); return r; }
  c.forEach((x) => lonRange(x, r));
  return r;
}

function wrapCopies(feats) {
  const out = [];
  feats.forEach((f) => {
    const [w, e] = lonRange(f.geometry.coordinates);
    [[w < -90, 360], [e > 90, -360]].forEach(([need, dx]) => {
      if (need) out.push({ ...f, wrapCopy: true, geometry: { ...f.geometry, coordinates: shiftCoords(f.geometry.coordinates, dx) } });
    });
  });
  return out;
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

// Легенда — это и фильтр: каждый пункт скрывает или показывает свой вид объектов.
// Состояние общее с вкладками «Зоны», «Флот» и «События», поэтому всё остаётся согласованным.
state.showFleet = true;
state.fleetLevels = new Set(["safe", "near", "inside"]);
const SHIP_ICON = (c) => `<svg width="14" height="14" viewBox="-7 -7 14 14"><path d="M0-6 4 4 0 2-4 4z" fill="${c}"/></svg>`;
const YACHT_ICON = (c) => `<svg width="14" height="14" viewBox="-7 -7 14 14"><path d="M0-6 3 0 0 5-3 0z" fill="${c}"/></svg>`;

function renderLegend() {
  const box = $("#legend-items");
  if (!box) return;
  const zf = state.zoneFilter, t = state.traffic;
  const row = (key, icon, label, on, extra = "") =>
    `<button type="button" class="lg lg-btn${on ? "" : " off"}${extra}" data-key="${key}" aria-pressed="${on}">${icon}<span>${esc(label)}</span></button>`;
  const head = (key, label, on, hint = "") =>
    `<button type="button" class="lg-title lg-btn${on ? "" : " off"}" data-key="${key}" aria-pressed="${on}" title="${esc(hint)}">${esc(label)}</button>`;
  let html = Object.entries(LAYERS).map(([layer, info]) => {
    const types = Object.entries(ZONE_TYPES).filter(([, d]) => d.layer === layer);
    return head(`layer:${layer}`, info.label, types.some(([k]) => zf.types.has(k)), info.hint)
      + types.map(([k, d]) => row(`zt:${k}`, `<i class="sw" style="${swatchStyle(d)}"></i>`, d.label, zf.types.has(k))).join("");
  }).join("");
  html += `<div class="lg"><i class="sw" style="border:1px dashed ${ZONE_TYPES.sea.color}"></i><span>Внутренние воды: реки и речные порты (Украина, Беларусь, Дон)</span></div>`;
  html += `<div class="lg"><i class="sw z-changed"></i><span>Изменена в последней версии</span></div>`;
  const cats = (t.data && t.data.categories) || {};
  html += head("traffic", "Все суда (AIS)", t.on);
  html += Object.keys(TRAFFIC_COLORS).map((c) => row(`cat:${c}`, (c === "yacht" ? YACHT_ICON : SHIP_ICON)(TRAFFIC_COLORS[c]),
    cats[c] || { cargo: "Грузовые", tanker: "Танкеры", passenger: "Пассажирские", yacht: "Яхты и прогулочные",
                 fishing: "Рыболовные", service: "Буксиры и служебные", other: "Прочие" }[c], t.on && t.cats.has(c))).join("");
  html += head("fleet", "Суда флота", state.showFleet);
  html += `<div class="lg-hint">Точка у кормы судна — где оно относительно зон риска (JWC, IBF, INL):</div>`;
  html += [["safe", "Вне зоны риска"], ["near", "Приближается к зоне риска"], ["inside", "В зоне риска"]].map(([lvl, label]) =>
    row(`lvl:${lvl}`, `<i class="dot s-${lvl}"></i>`, label, state.showFleet && state.fleetLevels.has(lvl))).join("");
  if (state.fleets.length > 1) {                    // флоты — цвет кольца вокруг значка судна
    html += `<div class="lg-title">Флоты (цвет судна)</div>`
      + state.fleets.map((f) => row(`fl:${f.id}`, SHIP_ICON(fleetColor(f.id)),
        f.name + (f.access === "owner" ? "" : ` (от ${f.owner})`), state.showFleet && !state.hiddenFleets.has(f.id))).join("");
  }
  // происшествия — по типам, только те, что есть за выбранный период (сколько — справа)
  const incOn = $("#inc-show").checked, counts = {};
  (state.inc?.items || []).forEach((it) => { if (!it.test || $("#inc-tests").checked) counts[it.kind] = (counts[it.kind] || 0) + 1; });
  const kinds = Object.entries(state.inc?.kinds || {}).filter(([k]) => counts[k] && INC_STYLE[k]);
  html += head("incidents", "Происшествия", incOn, "Скрыть или показать все происшествия на карте");
  const nRead = (state.inc?.items || []).filter((it) => it.read && it.lat != null).length;
  if (nRead) html += row("incread", `<span class="inc-dot lg-read">✓</span>`, `Просмотренные (${nRead})`, incOn && state.inc.showRead);
  html += kinds.map(([k, d]) => row(`inck:${k}`, `<span class="inc-dot" style="background:${INC_STYLE[k].color}">${INC_STYLE[k].glyph}</span>`,
    d.label, incOn && !state.inc.hidden.has(k)).replace("</span></button>", `</span><em class="lg-n">${counts[k]}</em></button>`)).join("")
    || `<div class="lg-hint">За выбранный период происшествий нет</div>`;
  html += `<div class="lg-title">Прочее</div>`
    + row("ports", `<svg width="14" height="14" viewBox="-7 -7 14 14"><rect x="-5" y="-5" width="10" height="10" rx="3" fill="#8fa6b0"/></svg>`,
      "Порты (красный — в зоне JWC)", state.showPorts)
    + row("areas", `<i class="sw" style="border:1.5px dashed #fff;background:transparent"></i>`, "Мои области", $("#areas-show").checked);
  box.innerHTML = html;
}

$("#legend-items").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-key]");
  if (!b) return;
  const [kind, val] = b.dataset.key.split(":");
  const zf = state.zoneFilter, t = state.traffic;
  if (kind === "layer") {
    const types = Object.entries(ZONE_TYPES).filter(([, d]) => d.layer === val).map(([k]) => k);
    const on = types.some((k) => zf.types.has(k));
    types.forEach((k) => (on ? zf.types.delete(k) : zf.types.add(k)));
    applyZoneFilter();
  } else if (kind === "zt") {
    if (zf.types.has(val)) zf.types.delete(val); else zf.types.add(val);
    applyZoneFilter();
  } else if (kind === "traffic" || kind === "cat") {
    if (kind === "cat") {
      if (!t.on) { t.cats = new Set([val]); t.on = true; }                 // включаем слой сразу с этой категорией
      else if (t.cats.has(val)) t.cats.delete(val); else t.cats.add(val);
    } else t.on = !t.on;
    localStorageSet("showTraffic", t.on ? "1" : "0");
    $("#traffic-toggle").checked = t.on;
    await loadTraffic().catch(() => {});
  } else if (kind === "fleet") {
    state.showFleet = !state.showFleet;
    renderVessels();
  } else if (kind === "fl") {
    state.showFleet = true;
    const id = +val;
    if (state.hiddenFleets.has(id)) state.hiddenFleets.delete(id); else state.hiddenFleets.add(id);
    renderVessels();
  } else if (kind === "lvl") {
    state.showFleet = true;
    if (state.fleetLevels.has(val)) state.fleetLevels.delete(val); else state.fleetLevels.add(val);
    renderVessels();
  } else if (kind === "ports") {
    state.showPorts = !state.showPorts;
    localStorageSet("showPorts", state.showPorts ? "1" : "0");
    updatePortsVisibility();
    renderZoneFilters();
  } else if (kind === "incidents") {
    $("#inc-show").checked = !$("#inc-show").checked;
    renderIncidents();
  } else if (kind === "incread") {
    if (!$("#inc-show").checked) $("#inc-show").checked = true;
    setIncShowRead(!state.inc.showRead);
  } else if (kind === "inck") {
    const h = state.inc.hidden;
    if (!$("#inc-show").checked) { $("#inc-show").checked = true; h.clear(); Object.keys(state.inc.kinds).forEach((k) => k !== val && h.add(k)); }
    else if (h.has(val)) h.delete(val); else h.add(val);
    saveIncHidden();
    renderIncidents();
  } else if (kind === "areas") {
    $("#areas-show").checked = !$("#areas-show").checked;
    renderAreas();
  }
  renderLegend();
});

function swatchStyle(d) {
  return `background:${d.color}22;box-shadow:inset 0 0 5px ${d.color}aa;border:1px ${d.dash ? "dashed" : "solid"} ${d.color}`;
}

function applyZoneFilter() {
  renderZoneLayer();
  renderZonesPanel();
  renderLegend();
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
  smoothFit(layer.getBounds(), { padding: [30, 30], maxZoom: 7 });
  zonesLayer.eachLayer((l) => { if (!l.feature.wrapCopy && l.feature.properties.id === f.properties.id) l.openPopup(layer.getBounds().getCenter()); });
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
    const all = [...d.ships, ...(d.wide || [])];
    $$(".pc-ship", popup.getElement()).forEach((b) => b.onclick = () => {
      const s = all[+b.dataset.i];
      openTrafficShip(s.mmsi, L.latLng(s.lat, s.lon));
    });
  } catch (e) { /* остаётся краткая карточка */ }
}

function shipState(s) {
  if (s.nav_status === "Ошвартовано") return "у причала";
  if (s.nav_status === "На якоре") return "на якоре";
  return (s.sog || 0) >= 1 ? `идёт ${s.sog.toFixed(1)} уз` : "стоит";
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
  // суда рядом списком по расстоянию; data-i — индекс в общем списке [...ships, ...wide]
  const row = (s, i) => `
    <button class="pc-ship" type="button" data-i="${i}">
      ${flagImg(s.flag_iso)}<span class="sr-main"><b>${esc(s.name || "MMSI " + s.mmsi)}</b>${s.in_fleet ? ` <span class="tag">флот</span>` : ""}
      <span class="muted small"><i class="dot" style="background:${TRAFFIC_COLORS[s.category] || TRAFFIC_COLORS.other}"></i> ${esc(s.type_name)}${
        s.length ? ` · ${Math.round(s.length)} м` : ""} · ${shipState(s)}</span></span>
      <span class="muted small nowrap">${s.distance_nm} миль</span></button>`;
  const near = d ? d.ships.map(row).join("") : "";
  const wide = d && d.wide?.length ? d.wide.map((s, i) => row(s, d.ships.length + i)).join("") : "";
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
      ${near ? `<div class="pc-list"><div class="muted small pc-h">${d.ships_total > d.ships.length
          ? `Ближайшие ${d.ships.length} из ${d.ships_total}` : `Суда рядом — ${d.ships.length}`}, по расстоянию</div>${near}</div>`
        : `<div class="sc-foot">В 5 милях судов по AIS нет: порт может быть вне зоны береговых станций, или суда выключили AIS.</div>`}
      ${wide ? `<div class="pc-list"><div class="muted small pc-h">Ближайшие в радиусе ${d.wide_nm} миль</div>${wide}</div>` : ""}`
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
  if (document.body.classList.contains("drawing")) setMapInteractive(false);
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
  smoothTo([p.lat, p.lon], Math.max(map.getZoom(), 8));
  if (!state.showPorts) { state.showPorts = true; updatePortsVisibility(); renderZoneFilters(); }
  openPort(p);
}

/* ------------------------------------------------------------------ флот */
async function loadFleets() {
  const data = await api("/api/fleets");
  state.fleets = data.fleets;
  state.me = data.user;
  state.mineSet = null;                             // суда своих флотов — пересчитать
  if (!state.fleets.find((f) => f.id === state.fleetId)) {
    const saved = +localStorageGet("fleetId");
    state.fleetId = (state.fleets.find((f) => f.id === saved) || state.fleets[0] || {}).id ?? null;
  }
  renderFleet();
  renderVessels();
  renderLegend();                                   // список флотов в легенде
  if (state.inc?.items.length) renderIncidents();     // санкции судов флота — пометкой на их значках
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
  const fc = $("#fleet-color"), cur = currentFleet();
  fc.hidden = !cur;
  if (cur) {
    fc.style.setProperty("--fc", fleetColor(cur.id));
    fc.title = cur.access === "owner" ? "Цвет флота на карте — нажмите, чтобы выбрать" : `Цвет флота (меняет владелец, ${cur.owner})`;
  }
  const empty = !state.fleets.length;
  $("#fleet-empty").hidden = !empty;
  $("#fleet-body").hidden = empty;
  sel.hidden = empty;
  const f = currentFleet();
  if (!f) return;
  const owner = f.access === "owner", canEdit = f.access !== "view";
  $("#btn-add-toggle").hidden = !canEdit;
  $("#btn-share-toggle").hidden = !owner;
  $("#share-count").textContent = owner && f.shares?.length ? ` · ${f.shares.length}` : "";
  if (!canEdit) togglePanel("#add-vessel", false);
  if (!owner) togglePanel("#fleet-share", false);
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
  if (!f.vessels.length && canEdit) togglePanel("#add-vessel", true);
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
        <div class="vessel-meta">${v.mmsi.startsWith("IMO:") ? `IMO ${esc(v.imo)} · <span class="nowrap">MMSI ещё не известен</span>`
          : `MMSI ${esc(v.mmsi)}${v.imo ? ` · IMO ${esc(v.imo)}` : ""}`} ·
          <span class="nowrap">${v.position ? `обновлено ${ago(v.position.ts)}` : v.mmsi.startsWith("IMO:") ? "ждём паспортные данные AIS" : "ждём AIS"}</span></div>
        ${v.screening?.sanctions?.length ? `<div class="vessel-meta"><span class="tag review" title="${esc(v.screening.sanctions.map((x) => x.label).join(", "))}">САНКЦИИ: ${esc(v.screening.sanctions.map((x) => x.label.split(" ")[0]).join(", "))}</span></div>` : ""}
        ${v.screening?.detentions?.length ? `<div class="vessel-meta"><span class="tag amended">задержание ${fmtDate(v.screening.detentions[0].detained_on)}</span></div>` : ""}
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

/* ------------------------------------------------------------------ сводка над картой */
// Всегда на виду: сколько судов всех ваших флотов в зонах риска, сколько подходят, сколько новых событий.
// Нажатие на число — список этих судов (нажатие на судно открывает его карточку) или лента с новыми.
function riskGroups() {
  const vs = allVessels().filter((v) => v.position);
  return { inside: vs.filter((v) => v.status.level === "inside"), near: vs.filter((v) => v.status.level === "near") };
}
function updateRiskBar() {
  const bar = $("#riskbar");
  if (!bar) return;
  const g = riskGroups();
  bar.hidden = !allVessels().length;
  bar.querySelector("[data-rb=inside] b").textContent = g.inside.length;
  bar.querySelector("[data-rb=near] b").textContent = g.near.length;
  bar.querySelector("[data-rb=new] b").textContent = state.unread || 0;
  bar.querySelectorAll(".rb").forEach((b) => b.classList.toggle("zero", b.querySelector("b").textContent === "0"));
  if (!$("#rb-pop").hidden) showRiskPop($("#rb-pop").dataset.kind);       // список открыт — обновить
}
function showRiskPop(kind) {
  const pop = $("#rb-pop"), g = riskGroups()[kind] || [];
  pop.dataset.kind = kind;
  pop.innerHTML = `<div class="rb-h">${kind === "inside" ? "В зонах риска" : "Подходят к зонам риска"}</div>`
    + (g.map((v) => {
      const z = kind === "inside" ? v.status.inside : v.status.near;
      return `<button type="button" class="rb-v" data-mmsi="${esc(v.mmsi)}"><b>${esc(v.name || v.mmsi)}</b> ${fleetChips(v.mmsi)}
        <span class="rb-z">${z.map((x) => esc(x.name) + (kind === "inside" ? (x.since ? ` · с ${fmtTime(x.since)}` : "") : ` · ${Math.round(x.distance_nm)} миль`)).join("<br>")}</span></button>`;
    }).join("") || `<div class="rb-empty">Таких судов сейчас нет</div>`);
  pop.hidden = false;
  pop.querySelectorAll(".rb-v").forEach((b) => b.onclick = () => { pop.hidden = true; selectVessel(b.dataset.mmsi, true); });
}
$("#riskbar").addEventListener("click", (e) => {
  const b = e.target.closest(".rb");
  if (!b) return;
  const kind = b.dataset.rb, pop = $("#rb-pop");
  if (kind === "new") {                                   // новые события — лента «Мои суда», только новые
    pop.hidden = true;
    switchTab("events");
    $("#panel-events [data-feed=mine]").click();
    if (!$("#n-unread-only").checked) $("#feed-new").click();
    return;
  }
  if (!pop.hidden && pop.dataset.kind === kind) { pop.hidden = true; return; }
  showRiskPop(kind);
});
document.addEventListener("click", (e) => { if (!e.target.closest("#riskbar")) $("#rb-pop").hidden = true; });

function renderVessels() {
  updateRiskBar();
  const live = new Set();
  for (const v of allVessels()) {
    if (!v.position || !state.showFleet || !state.fleetLevels.has(v.status.level)) continue;
    const fl = fleetsOf(v.mmsi).filter((f) => !state.hiddenFleets.has(f.id));
    if (!fl.length) continue;                       // все флоты судна скрыты в легенде
    live.add(v.mmsi);
    const ll = [v.position.lat, v.position.lon];
    const sel = v.mmsi === state.selectedMmsi;
    const icon = shipIcon(v.status.level, v.position.cog ?? v.position.heading, sel, vesselBadges(v), fleetColor(fl[0].id));
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
  }
  for (const [mmsi, m] of markers) if (!live.has(mmsi)) { vesselsLayer.removeLayer(m); markers.delete(mmsi); }
}

function vesselPopup(v) {
  const p = v.position;
  const ageS = p ? Math.round((Date.now() - new Date(p.ts)) / 1000) : null;
  const d = { ...v, sog: p?.sog, cog: p?.cog };
  const fleets = state.fleets.filter((f) => f.vessels.some((x) => x.mmsi === v.mmsi)).map((f) => f.name);
  return shipCard(d, zoneLines(v.status.inside, v.status.near), {
    ageS, actions: `<span class="sc-tag">Флот: ${fleetChips(v.mmsi) || esc(fleets.join(", "))}</span>`,
  });
}

async function selectVessel(mmsi, fly) {
  state.selectedMmsi = mmsi;
  const v = allVessels().find((x) => x.mmsi === mmsi);
  renderFleet();
  renderVessels();
  trackLayer.clearLayers();
  if (!v) return;
  if (!v.position) {                     // вне покрытия AIS: карточка с паспортом из справочника, без позиции
    if (/^\d+$/.test(mmsi)) openTrafficShip(mmsi, map.getCenter());
    else toast("Позиция ещё не получена", "Ждём первое сообщение AIS от этого судна.");
    return;
  }
  if (fly) smoothTo([v.position.lat, v.position.lon], Math.max(map.getZoom(), 6));
  if (shipPanel.isOpen() && shipPanel.mmsi === mmsi) return;
  setTimeout(() => openFleetCard(v), fly ? 1150 : 0);
}

function openFleetCard(v) {
  shipPanel.open(v.mmsi, [v.position.lat, v.position.lon], vesselPopup(v));
  hydrateCard(shipPanel, `/api/track/${encodeURIComponent(v.mmsi)}`);
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
 * opts.actions — HTML кнопки «Добавить во флот» / пометки, opts.ageS — давность позиции в секундах.
 */
function shipCard(d, zonesHtml, opts = {}) {
  const sogCog = `${d.sog != null ? d.sog.toFixed(1) + " уз" : "—"} / ${d.cog != null ? Math.round(d.cog) + "°" : "—"}`;
  const size = d.length ? `${Math.round(d.length)} × ${d.beam ? Math.round(d.beam) : "?"} м` : "—";
  const mt = `https://www.marinetraffic.com/en/ais/details/ships/mmsi:${encodeURIComponent(d.mmsi)}`;
  return `<div class="sc" data-imo="${esc(d.imo || "")}" data-mmsi="${esc(d.mmsi)}" data-cat="${esc(d.category || "other")}">
    <div class="sc-head">${flagImg(d.flag_iso, d.flag)}
      <div><div class="sc-name">${esc(d.name || "Без названия")}</div>
      <div class="sc-type"><i class="dot" style="background:${TRAFFIC_COLORS[d.category] || TRAFFIC_COLORS.other}"></i> ${esc(d.type_name || "Тип не передан")}</div></div></div>
    <div class="sc-risk ${riskClass(zonesHtml)}">${zonesHtml}</div>
    ${screeningBlock(d.screening)}
    <div class="sc-actions">${opts.actions ? `<div class="sc-row">${opts.actions}</div>` : ""}
      <div class="sc-row"><button class="btn ghost sc-track" type="button">Скрыть путь</button>
      <a class="btn ghost" href="${mt}" target="_blank" rel="noopener">Подробнее ↗</a></div></div>
    ${opts.note ? `<div class="sc-note">${esc(opts.note)}</div>` : ""}
    <div class="sc-tabs"><button type="button" class="on" data-tab="info">Судно</button>
      <button type="button" data-tab="events">События <span class="sc-evn"></span></button></div>
    <div class="sc-pane" data-pane="events" hidden><div class="sc-events muted small">Загружаю…</div></div>
    <div class="sc-pane" data-pane="info">
    <div class="sc-route"><div><span class="muted small">Пункт назначения</span><b>${esc(d.destination || "не указан")}</b></div>
      <div class="sc-eta"><span class="muted small">ETA</span><b>${esc(d.eta || "—")}</b></div></div>
    <div class="sc-dest"></div>
    <div class="sc-grid">
      <div><span>Статус</span><b>${esc(d.nav_status || "—")}</b></div>
      <div><span>Скорость / курс</span><b>${sogCog}</b></div>
      <div><span>Осадка</span><b>${d.draught ? d.draught.toFixed(1) + " м" : "—"}</b></div>
      <div><span>Размеры</span><b>${size}</b></div>
      <div><span>Флаг</span><b>${esc(d.flag || "—")}</b></div>
      <div><span>IMO / MMSI${d.hin ? " / HIN" : ""}</span><b>${esc(d.imo || "—")}<br>${esc(d.mmsi)}${d.hin ? `<br><span class="sc-hin">${esc(d.hin)}</span>` : ""}</b></div>
    </div>
    <div class="sc-photo">${shipSilhouette(d.category)}<div class="sc-credit"></div></div>
    <div class="sc-foot">Обновлено: <b>${fmtAge(opts.ageS)}</b>${d.ais_class ? ` · AIS класс ${esc(d.ais_class)}` : ""}${d.callsign ? ` · позывной ${esc(d.callsign)}` : ""}</div>
    <div class="sc-links">Тоннаж, год постройки, владелец:
      <a href="${mt}" target="_blank" rel="noopener">MarineTraffic</a> ·
      <a href="https://www.vesselfinder.com/vessels/details/${encodeURIComponent(d.imo || d.mmsi)}" target="_blank" rel="noopener">VesselFinder</a>
      ${d.imo ? ` · <a href="https://www.equasis.org/" target="_blank" rel="noopener" title="Бесплатно после регистрации, поиск по IMO ${esc(d.imo)}">Equasis</a>` : ""}</div>
    </div>
  </div>`;
}

// Санкции и задержания по IMO — из официальных списков (обновляются раз в сутки)
function screeningBlock(sc) {
  if (!sc || (!sc.sanctions.length && !sc.detentions.length)) return "";
  const sanc = sc.sanctions.map((x) => `<a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.label)}</a>${
    x.program ? ` <span class="muted">(${esc(x.program.slice(0, 60))}${x.listed_on ? `, с ${fmtDate(x.listed_on)}` : ""})</span>` : ""}`).join("<br>");
  const det = sc.detentions[0];
  return `${sanc ? `<div class="sc-alert sanc"><b>⛔ Санкционные списки</b><div>${sanc}</div></div>` : ""}
    ${det ? `<div class="sc-alert det"><b>⚓ Задержание (Tokyo MoU)</b>
      <div>${esc(det.port)}, ${fmtDate(det.detained_on)} — ${det.released_on ? `освобождено ${fmtDate(det.released_on)}` : "ещё задержано"}</div>
      <div class="muted">${esc(det.reasons || "")}</div>
      <div class="muted">${[det.built && `построено ${esc(det.built)}`, det.gt && `GT ${esc(det.gt)}`, det.company && esc(det.company)].filter(Boolean).join(" · ")}</div></div>` : ""}`;
}

// После открытия карточки: фото с Wikimedia Commons (если есть) и кнопка трека
async function hydrateCard(popup, trackUrl) {      // popup — панель карточки (shipPanel)
  const el = popup.getElement()?.querySelector(".sc");
  if (!el) return;
  const btn = el.querySelector(".sc-track");
  btn.onclick = () => {
    const shown = map.hasLayer(pathLayer);
    if (shown) map.removeLayer(pathLayer); else pathLayer.addTo(map);
    btn.textContent = shown ? "Показать путь" : "Скрыть путь";
  };
  $$(".sc-tabs button", el).forEach((b) => b.onclick = () => {
    $$(".sc-tabs button", el).forEach((x) => x.classList.toggle("on", x === b));
    $$(".sc-pane", el).forEach((p) => { p.hidden = p.dataset.pane !== b.dataset.tab; });
  });
  loadShipEvents(el);
  loadShipPath(el, popup, trackUrl);
  const imo = el.dataset.imo;
  const credit = el.querySelector(".sc-credit");
  if (!imo) { credit.textContent = "фото нет: судно не передаёт IMO"; return; }
  // Commons отвечает не сразу (очередь, ограничение частоты) — спрашиваем, пока карточка открыта, до ~40 с
  try {
    let photo = null;
    credit.textContent = "фото загружается…";
    for (let i = 0; i < 10 && el.isConnected; i++) {
      const r = await api(`/api/photo/${encodeURIComponent(imo)}`);
      photo = r.photo;
      if (!r.pending) break;
      await new Promise((res) => setTimeout(res, 4000));
    }
    if (!el.isConnected) return;
    if (!photo) { credit.textContent = "фото нет в открытых источниках (Wikimedia Commons)"; return; }
    const box = el.querySelector(".sc-photo");
    const img = new Image();
    img.alt = "";
    img.onload = () => {
      box.querySelector(".sc-sil")?.remove();
      box.prepend(img);
      credit.innerHTML = `<a href="${esc(photo.page)}" target="_blank" rel="noopener">© ${esc(photo.author || "Wikimedia Commons")}${photo.license ? `, ${esc(photo.license)}` : ""}</a>`;
    };
    img.onerror = () => { credit.textContent = "фото не открылось — попробуйте позже"; };
    img.src = photo.url;
  } catch { credit.textContent = ""; /* фото необязательно */ }
}

// Карточка судна — панель у края карты, а не всплывающее окно над судном: окно закрывало путь.
// Методы как у L.Popup (getElement, getLatLng, once("remove")), чтобы карточке было всё равно.
const shipPanel = {
  el: $("#ship-panel"), body: $("#ship-panel .sp-body"), mmsi: null, latlng: null, handlers: [],
  open(mmsi, latlng, content) {
    this.close();
    this.mmsi = mmsi;
    this.latlng = L.latLng(latlng);
    this.body.innerHTML = content;
    this.el.hidden = false;
    this.el.scrollTop = 0;
    setShipOpen(true);
    return this;
  },
  setContent(content) { this.body.innerHTML = content; return this; },
  getElement() { return this.el.hidden ? null : this.el; },
  getLatLng() { return this.latlng; },
  isOpen() { return !this.el.hidden; },
  once(ev, fn) { if (ev === "remove") this.handlers.push(fn); return this; },
  close() {
    if (this.el.hidden) return;
    this.el.hidden = true;
    this.body.innerHTML = "";
    this.mmsi = null;
    setShipOpen(false);
    const hs = this.handlers;
    this.handlers = [];
    hs.forEach((fn) => fn());
  },
  // сколько места панель занимает у края карты: справа (широкий экран) или снизу (телефон)
  padding() {
    const m = map.getContainer().getBoundingClientRect(), r = this.el.getBoundingClientRect();
    const bottom = r.width > m.width * 0.6;
    const sb = $("#sidebar").getBoundingClientRect();             // на компьютере панель «парит» над картой слева
    const left = !isPhone() && !document.body.classList.contains("sb-collapsed") ? Math.max(40, sb.right - m.left + 30) : 40;
    return { paddingTopLeft: [left, 40], paddingBottomRight: bottom ? [40, m.bottom - r.top + 20] : [m.right - r.left + 20, 40] };
  },
};
$("#ship-panel-close").onclick = () => shipPanel.close();

/* ------------------------------------------------------------------ телефон: панели-«шторки» */
// Узкий экран: панель с вкладками — снизу, карточка судна — шторкой поверх карты. Пока открыта карточка,
// панель с вкладками спрятана (карте и карточке — весь экран), закрыли карточку — панель возвращается.
// Обе можно растянуть за ручку вплоть до всего экрана; короткое нажатие на ручку — «половина / весь экран».
const isPhone = () => matchMedia("(max-width: 820px)").matches;
function setShipOpen(on) {
  document.body.classList.toggle("ship-open", on);
  if (on && typeof shipSheet !== "undefined" && shipSheet.get() === "min") shipSheet.set("half");
  if (isPhone()) setTimeout(() => map.invalidateSize(), 0);      // карта поменяла размер
}

// Три положения: свёрнута (видно только верх), половина экрана, весь экран. Свайп вверх/вниз — в соседнее
// положение (тянуть до края не нужно), длинный жест — в ближайшее, нажатие на ручку — следующее.
const SHEET = ["min", "half", "full"];
function sheet(target, { box, cssVar, handles, minPx, initial = "half", storeKey, onDone }) {
  const avail = () => box().getBoundingClientRect().height;
  const px = (st) => (st === "min" ? minPx() : st === "half" ? avail() * 0.5 : avail());
  let state = (storeKey && SHEET.includes(localStorageGet(storeKey)) && localStorageGet(storeKey)) || initial;
  const apply = (st) => {
    state = st;
    target.style.setProperty(cssVar, st === "min" ? `${Math.round(minPx())}px` : st === "half" ? "50%" : "100%");
    target.classList.toggle("full", st === "full");
    target.classList.toggle("min", st === "min");
    target.dataset.sheet = st;
    if (storeKey) localStorageSet(storeKey, st);
    onDone && onDone();
  };
  let drag = null;
  for (const h of handles()) {
    h.addEventListener("pointerdown", (e) => {
      if (!isPhone() || e.button > 0) return;
      drag = { y: e.clientY, h: target.getBoundingClientRect().height, t: Date.now(), moved: false, id: e.pointerId, el: h, isGrip: h.classList.contains("sheet-grip") };
    });
    h.addEventListener("pointermove", (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const dy = e.clientY - drag.y;
      if (!drag.moved && Math.abs(dy) < 8) return;
      if (!drag.moved) { drag.moved = true; drag.el.setPointerCapture(e.pointerId); target.classList.add("dragging"); }
      target.style.setProperty(cssVar, `${Math.round(Math.min(avail(), Math.max(minPx(), drag.h - dy)))}px`);
    });
    const end = (e) => {
      if (!drag || e.pointerId !== drag.id) return;
      const d = drag;
      drag = null;
      target.classList.remove("dragging");
      if (!d.moved) {                                  // нажатие на ручку — следующее положение; на вкладку — обычный клик
        if (d.isGrip) apply(state === "min" ? "half" : state === "half" ? "full" : "half");
        return;
      }
      const dy = e.clientY - d.y, h = target.getBoundingClientRect().height;
      const near = SHEET.reduce((a, b) => (Math.abs(px(b) - h) < Math.abs(px(a) - h) ? b : a));
      const from = SHEET.indexOf(state);
      // короткий свайп — на шаг в сторону жеста, даже если до следующего положения далеко
      const step = Math.abs(dy) > 24 && near === state ? SHEET[Math.min(2, Math.max(0, from + (dy < 0 ? 1 : -1)))] : near;
      apply(step);
    };
    h.addEventListener("pointerup", end);
    h.addEventListener("pointercancel", end);
    h.addEventListener("click", (e) => { if (target.classList.contains("dragging")) e.preventDefault(); }, true);
  }
  apply(state);
  return { set: apply, get: () => state };
}
// панель с вкладками: свёрнута — видны ручка и вкладки
const sidebarSheet = sheet($("#sidebar"), { box: () => $(".layout"), cssVar: "--sb-h", storeKey: "sidebarSheet",
  handles: () => [$("#sidebar-grip"), $("#sidebar .tabs")],
  minPx: () => $("#sidebar-grip").offsetHeight + $("#sidebar .tabs").offsetHeight + 1,
  onDone: () => setTimeout(() => map.invalidateSize(), 220) });
// карточка судна: свёрнута — видно название судна
const shipSheet = sheet($("#ship-panel"), { box: () => $(".map-wrap"), cssVar: "--sp-h",
  handles: () => [$("#ship-grip")],
  minPx: () => $("#ship-grip").offsetHeight + ($("#ship-panel .sc-head")?.offsetHeight || 56) + 2 });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && shipPanel.isOpen()) shipPanel.close(); });

// Путь судна в карточке: пройденный трек (сплошная линия) + типовой путь до порта назначения (пунктир).
// Рисуется сам при открытии карточки, убирается при закрытии; кнопка в карточке прячет и показывает.
const pathLayer = L.layerGroup().addTo(map);
// светлая тема: голубой и почти белый на светло-серой карте не видны — темнее и контрастнее
const pathColors = () => isLight()
  ? { past: "#0e7490", ahead: "#1e293b", risk: "#dc2626", port: "#ffffff" }
  : { past: "#4fc3d9", ahead: "#e8f1f5", risk: "#ff5a4e", port: "#0b1a22" };

async function loadShipPath(el, popup, trackUrl) {
  const box = el.querySelector(".sc-dest");
  const mmsi = el.dataset.mmsi;
  pathLayer.clearLayers();
  if (!map.hasLayer(pathLayer)) pathLayer.addTo(map);
  popup.once("remove", () => { if (state.pathFor === mmsi) pathLayer.clearLayers(); });
  state.pathFor = mmsi;
  const [tr, rt] = await Promise.allSettled([api(trackUrl), api(`/api/traffic/${encodeURIComponent(mmsi)}/route`)]);
  if (!el.isConnected || state.pathFor !== mmsi) return;
  const here = popup.getLatLng();
  const C = pathColors();
  // пройдено
  const pts = (tr.status === "fulfilled" ? tr.value.points : []).map((p) => [p.lat, p.lon]);
  const first = tr.status === "fulfilled" ? tr.value.points[0] : null;
  const hours = first ? (first.age_s != null ? first.age_s / 3600 : (Date.now() - Date.parse(first.ts)) / 3600e3) : 0;
  if (pts.length > 1) {
    L.polyline([...pts, [here.lat, here.lng]], { color: C.past, weight: 2.5, opacity: 0.9, interactive: false }).addTo(pathLayer);
    L.circleMarker(pts[0], { radius: 3, weight: 0, fillColor: C.past, fillOpacity: 1, interactive: false }).addTo(pathLayer);
  }
  // впереди
  const { destination: dest, route: r } = rt.status === "fulfilled" ? rt.value : {};
  const zl = (zs) => zs.map((z) => esc(z.name)).join(", ");
  let html = "";
  if (r) {
    const color = r.port_zones.length ? C.risk : C.ahead;
    if (r.line) L.polyline(r.line, { color, weight: 2, opacity: isLight() ? 0.75 : 0.8, dashArray: "6 7", interactive: false }).addTo(pathLayer);
    L.circleMarker(r.line ? r.line[r.line.length - 1] : [r.lat, r.lon], { radius: 6, color, weight: 2, fillColor: C.port, fillOpacity: 1 })
      .bindTooltip(`Пункт назначения: ${esc(r.port)}`, { direction: "top" }).addTo(pathLayer);
    html = `<div class="small">→ <b>${esc(r.port)}</b> <span class="muted">(${esc(r.country)}, ${esc(r.code)})</span>${
      r.distance_nm != null ? ` · ${r.distance_nm.toLocaleString("ru-RU")} миль ${r.path === "sea" ? "по морю" : "по прямой"}` : ""}</div>
      ${r.port_zones.length ? `<div class="zin small">● Порт в зоне: ${zl(r.port_zones)}</div>` : `<div class="zok small">Порт вне зон риска</div>`}
      ${r.route_zones?.length ? `<div class="znr small" title="${r.path === "sea"
        ? "Типовой морской маршрут (searoute), а не план рейса — судно может идти иначе"
        : "Прямая линия: реальный путь обходит сушу"}">◌ ${r.path === "sea" ? "По пути" : "По прямой"} через: ${zl(r.route_zones)}</div>` : ""}`;
  } else if (dest) {
    html = `<span class="muted small">Порт «${esc(dest)}» не распознан — судно указало его не кодом и не названием из справочника UN/LOCODE</span>`;
  }
  const legend = [pts.length > 1 ? `<span><i class="lg-past"></i>пройдено${hours >= 1 ? ` за ${Math.round(hours)} ч` : ""}</span>` : "",
    r ? `<span><i class="lg-ahead${r.port_zones.length ? " risk" : ""}"></i>впереди${r.path === "sea" ? " (типовой маршрут)" : ""}</span>` : ""].join("");
  box.innerHTML = html + (legend ? `<div class="sc-legend">${legend}</div>` : "");
  // весь путь — в свободной части карты, не под панелью; совсем короткий путь не приближаем сильнее 10
  const bounds = L.latLngBounds([here]);
  pathLayer.eachLayer((l) => bounds.extend(l.getBounds ? l.getBounds() : l.getLatLng()));
  if (map.hasLayer(pathLayer)) smoothFit(bounds, { ...popup.padding(), maxZoom: Math.max(map.getZoom(), 10) });
}

// Новость — сюжет: одинаковые статьи разных изданий схлопнуты, count — сколько изданий написали
const isNews = (kind) => kind === "news" || kind === "news_risk";
function sourcesText(n) {
  const m10 = n % 10, m100 = n % 100;
  const w = m10 === 1 && m100 !== 11 ? "источник" : m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14) ? "источника" : "источников";
  return `${n} ${w}`;
}

// Вкладка «События» в карточке: происшествия AIS, санкции, задержания, уведомления по зонам и правилам
async function loadShipEvents(el) {
  const box = el.querySelector(".sc-events");
  try {
    const { items } = await api(`/api/traffic/${encodeURIComponent(el.dataset.mmsi)}/events`);
    if (!el.isConnected) return;
    el.querySelector(".sc-evn").textContent = items.length || "";
    box.classList.toggle("muted", !items.length);
    box.classList.toggle("feed-list", !!items.length);
    // чужое судно: входы в зоны, правила, новости и подозрительное поведение отслеживаются только у судов флота
    const own = allVessels().some((v) => v.mmsi === el.dataset.mmsi);
    const note = own ? "" : `<div class="sc-evnote muted small">Судно не в вашем флоте — здесь только общие события:
      аварийные сигналы, посадка на мель, задержания в портах, попадание в санкционные списки. Входы в зоны риска,
      ваши уведомления, новости и подозрительное поведение появятся после добавления судна во флот.</div>`;
    box.innerHTML = items.length ? items.map((e, i) => shipEventRow(e, i)).join("") + note
      : own ? "Событий по этому судну пока нет: здесь появятся входы в зоны риска, сработавшие уведомления, аварийные сигналы, санкции, задержания и новости."
      : note.replace("здесь только общие события", "событий пока нет. Для таких судов здесь видны только общие события");
    $$(".feed-item", box).forEach((row) => row.onclick = (ev) => {
      if (ev.target.closest("a")) return;
      const e = items[+row.dataset.i];
      if (e.lat == null && !/Позиция/.test(e.body || "")) return;
      showEventSpot({ ...e, mmsi: el.dataset.mmsi, title: e.source === "incident" ? e.title : e.title, ts: e.ts, body: e.body },
        notifPoint(e), { keepPanel: true });
    });
  } catch (e) { box.textContent = `Не удалось загрузить события: ${e.message}`; }
}

// Строка события в карточке судна — как в ленте, но без названия судна (карточка и так про него)
function shipEventRow(e, i) {
  const where = notifPoint(e) ? `<span class="fi-map" title="Нажмите — покажу на карте">◎ на карте</span>` : "";
  if (e.source === "incident") {
    const link = e.url ? `<div class="fi-link"><a class="small" href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.outlet || "Открыть статью")} ↗</a>${
      isNews(e.kind) && e.count > 1 ? ` <span class="muted small">и ещё ${sourcesText(e.count - 1)}</span>` : ""}</div>` : "";
    return `<div class="feed-item ${e.severity} read" data-i="${i}">
      <div class="fi-meta"><span class="fi-src">${isNews(e.kind) ? "новости" : "происшествие"}</span>${where}<span class="fi-time">${fmtTime(e.ts)}${
        e.count > 1 && !isNews(e.kind) ? ` · ×${e.count}` : ""}</span></div>
      <div class="fi-event">${esc(e.title)}</div>
      ${e.body ? `<div class="fi-quote">${esc(e.body)}</div>` : ""}${link}</div>`;
  }
  const [first, ...rest] = (e.body || "").split("\n");
  // название судна в начале заголовка не нужно — карточка и так про это судно («FARAHI2: идёт в порт…»)
  const title = /\(MMSI \d+\): /.test(e.title || "") ? e.title : (e.title || "").replace(/^[^«:]+: /, "");
  const t = splitTitle(title, /\(MMSI \d+\): /.test(title) ? e.mmsi || "x" : null);
  return `<div class="feed-item ${e.severity} read" data-i="${i}">
    <div class="fi-meta"><span class="fi-src">${SYSTEM_KINDS.has(e.kind) ? "система" : "уведомление"}</span>${where}<span class="fi-time">${fmtTime(e.ts)}</span></div>
    <div class="fi-event">${esc(t.event)}</div>
    ${t.zone ? `<div class="fi-zone">${esc(t.zone)}</div>` : ""}
    ${detailLines(first).length ? `<ul class="fi-lines">${detailLines(first).map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}
    ${rest.length ? `<div class="fi-rule">${esc(rest.join(" ").replace(/^((?:Правило|Уведомление) «[^»]*»).*$/s, "$1").replace(/^Правило /, "Уведомление ").replace(/\.$/, ""))}</div>` : ""}</div>`;
}

// Карточка судна начинается с главного для брокера — где судно относительно зон риска
function riskClass(zonesHtml) {
  return zonesHtml.includes('class="zin"') ? "in" : zonesHtml.includes('class="znr"') ? "near" : "ok";
}
function zoneLines(inside, near) {
  return [
    ...inside.map((z) => `<div class="zin">● В зоне: ${esc(z.name)}${z.since ? ` <span class="muted">с ${fmtTime(z.since)}</span>` : ""}</div>`),
    ...near.map((z) => `<div class="znr">◌ ${esc(z.name)}: ${Math.round(z.distance_nm)} миль</div>`),
  ].join("") || `<div class="zok">Вне зон риска</div>`;
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
      .on("click", () => smoothFit([[c.bbox[1], c.bbox[0]], [c.bbox[3], c.bbox[2]]], { padding: [50, 50], maxZoom: 12 }))
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
  if (document.body.classList.contains("drawing")) setMapInteractive(false);
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
  renderLegend();
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

// Название судна в уведомлении, ленте, окне происшествия — ссылка на его карточку
function shipLink(name, mmsi) {
  const label = `<b>${esc(name)}</b>`;
  return /^(\d+|IMO:\d+)$/.test(mmsi || "")
    ? `<a href="#" class="ship-link" data-mmsi="${esc(mmsi)}" data-name="${esc(name)}" title="Открыть карточку судна">${label}</a>` : label;
}
// «MMSI 123» или «IMO 9478767» — у судов из санкционных списков и задержаний бывает известен только IMO
function shipIdText(mmsi) {
  return /^IMO:/.test(mmsi || "") ? `IMO ${esc(mmsi.slice(4))}` : `MMSI ${esc(mmsi)}`;
}
function openShip(mmsi, name) {
  map.closePopup();
  eventSpot.clearLayers();
  const imo = /^IMO:/.test(mmsi) ? mmsi.slice(4) : null;
  const own = allVessels().find((v) => v.mmsi === mmsi || (imo && String(v.imo || "") === imo));
  if (own) { selectVessel(own.mmsi, true); return; }
  openTrafficShip(mmsi, map.getCenter(), true, name);
}
document.addEventListener("click", (e) => {
  const a = e.target.closest(".ship-link");
  if (!a) return;
  e.preventDefault();
  e.stopPropagation();
  openShip(a.dataset.mmsi, a.dataset.name);
});

async function openTrafficShip(mmsi, latlng, fly = false, name = "") {
  const popup = shipPanel.open(mmsi, latlng, `<div class="sc-loading muted">Загружаю…</div>`);
  try {
    const s = await api(`/api/traffic/${encodeURIComponent(mmsi)}`);
    if (shipPanel.mmsi !== mmsi) return;            // пока грузили, открыли другое судно
    if (s.offline) { popup.setContent(offlineCard(s, name)); return; }
    if (fly && s.lat != null) {                      // открыли по названию — показываем, где судно сейчас
      shipPanel.latlng = L.latLng(s.lat, s.lon);
      smoothFit([shipPanel.latlng, shipPanel.latlng], { ...shipPanel.padding(), maxZoom: Math.max(map.getZoom(), 7) });
    }
    const zs = s.zones || [];
    const own = state.fleets.filter((f) => f.access !== "view");
    const actions = s.fleets.length ? `<span class="sc-tag">Флот: ${s.fleets.map(esc).join(", ")}</span>`
      : `<button class="btn" type="button" data-add-fleet>+ Добавить во флот</button>
        <select class="sc-fleet" title="В какой флот добавить">${own.map((f) => `<option value="${f.id}">${esc(f.name)}</option>`).join("")}
          <option value="new">+ Новый флот…</option></select>`;
    popup.setContent(shipCard(s, zoneLines(zs.filter((z) => z.status === "inside"), zs.filter((z) => z.status !== "inside")),
      { actions, ageS: s.age_s, note: s.archived ? (s.lat != null
        ? `Сейчас судно вне зоны покрытия AIS. Показано последнее, что о нём известно: позиция ${fmtAge(s.age_s)}.`
        : "Сейчас судно вне зоны покрытия AIS, позиция неизвестна. Показан паспорт из справочника.") : "" }));
    hydrateCard(popup, `/api/traffic/${encodeURIComponent(mmsi)}/track`);
    const btn = popup.getElement()?.querySelector("[data-add-fleet]");
    const sel = popup.getElement()?.querySelector(".sc-fleet");
    const add = async (fid, fleetName) => {
      try {
        await api(`/api/fleets/${fid}/vessels`, { json: { mmsi: s.mmsi, imo: s.imo || "", name: s.name || "" } });
        toast(fleetName ? `Создан флот «${fleetName}», судно добавлено` : "Судно добавлено во флот", s.name || s.mmsi);
        shipPanel.close();
        await loadFleets();
        renderTraffic();
      } catch (e) { toast("Не удалось добавить", e.message, "critical"); }
    };
    // «+ Новый флот…» — создаём флот с этим судном (своих флотов может и не быть: тогда это единственный пункт)
    const addToNew = async () => {
      const fleetName = prompt("Название нового флота:", "")?.trim();
      if (!fleetName) { if (own.length) sel.value = own[0].id; return; }
      try {
        const r = await api("/api/fleets/create", { json: { name: fleetName } });
        state.fleetId = r.id; localStorageSet("fleetId", r.id);
        await add(r.id, fleetName);
      } catch (e) { toast("Не удалось создать флот", e.message, "critical"); }
    };
    if (sel) sel.onchange = () => { if (sel.value === "new") addToNew(); };
    if (btn) btn.onclick = () => (sel.value === "new" ? addToNew() : add(+sel.value));
  } catch (e) { popup.setContent(`<div class="sc-loading muted">${esc(e.message)}</div>`); }
}

// Судно сейчас не передаёт AIS (или нашей сети его не видно): что знаем — IMO, санкции, задержания, ссылки
function offlineCard(s, name) {
  const id = s.imo || s.mmsi;
  return `<div class="sc">
    <div class="sc-head"><div><div class="sc-name">${esc(s.name || name || "Без названия")}</div>
      <div class="sc-type">${[s.imo && `IMO ${esc(s.imo)}`, s.mmsi && `MMSI ${esc(s.mmsi)}`].filter(Boolean).join(" · ")}</div></div></div>
    ${screeningBlock(s.screening)}
    <div class="sc-foot">Сейчас судно не видно в AIS: оно далеко от береговых станций, стоит с выключенным AIS
      или передаёт под другим MMSI. Позиции и пути нет.</div>
    ${id ? `<div class="sc-links">Где судно сейчас, владелец:
      <a href="https://www.vesselfinder.com/vessels/details/${encodeURIComponent(id)}" target="_blank" rel="noopener">VesselFinder</a>
      ${s.imo ? ` · <a href="https://www.equasis.org/" target="_blank" rel="noopener" title="Бесплатно после регистрации, поиск по IMO ${esc(s.imo)}">Equasis</a>` : ""}</div>` : ""}
  </div>`;
}

/* ------------------------------------------------------------------ правила (конструктор отслеживания) */
const SEV_COLORS = { critical: "var(--red)", warning: "var(--amber)", info: "var(--accent)" };
const areasLayer = L.layerGroup().addTo(map);
state.rules = { items: [], meta: null, areas: [], editId: null };

async function loadRules() {
  const [r, a] = await Promise.all([api("/api/rules"), api("/api/areas")]);
  state.rules.items = r.items;
  state.rules.meta = r;
  state.rules.areas = a.items;
  renderRules();
  renderAreas();
}

function renderRules() {
  const { items } = state.rules;
  $("#rule-list").innerHTML = items.map((r) => `
    <li class="rule-item ${r.enabled ? "" : "off"}" data-id="${r.id}">
      <input type="checkbox" class="ri-toggle" ${r.enabled ? "checked" : ""} title="Включено">
      <div class="ri-main">
        <div class="ri-name"><span class="ri-sev" style="background:${SEV_COLORS[r.severity]}"></span>${esc(r.name)}${
          r.standard ? ` <span class="tag" title="Стандартное уведомление: его можно изменить, выключить или удалить">стандартное</span>` : ""}</div>
        <div class="small">${esc(r.description)}</div>
        <div class="muted small">${esc(r.target_name)} · ${r.audience === "fleet" ? "всем, кто видит флот" : "только мне"}
          · ${r.fired_count ? `сработало ${r.fired_count} раз, последний ${ago(r.last_fired)}` : "ещё не срабатывало"}</div>
      </div>
      <div class="ri-actions"><button class="text-btn ri-edit" type="button">Изменить</button>
        <button class="text-btn danger ri-del" type="button" title="Удалить">✕</button></div>
    </li>`).join("") || `<li class="empty muted">Уведомлений пока нет. Нажмите «+ Новое уведомление» или добавьте готовый набор.</li>`;
  renderPresets();
  const miss = state.rules.meta?.defaults_missing || 0;
  $("#btn-rules-defaults").hidden = !miss;
  $("#btn-rules-defaults").textContent = `Вернуть стандартные уведомления (${miss})`;
  $$("#rule-list .rule-item").forEach((el) => {
    const r = items.find((x) => x.id === +el.dataset.id);
    el.querySelector(".ri-toggle").onchange = async (e) => {
      await api(`/api/rules/${r.id}`, { method: "PATCH", json: { enabled: e.target.checked } });
      await loadRules();
    };
    el.querySelector(".ri-edit").onclick = () => openRuleForm(r);
    el.querySelector(".ri-del").onclick = async () => {
      if (!confirm(`Удалить уведомление «${r.name}»?`)) return;
      await api(`/api/rules/${r.id}`, { method: "DELETE" });
      await loadRules();
    };
  });
}

// --- форма правила: список целей и параметры зависят от выбранного условия
function ruleTargets(selected) {
  const opts = [`<option value="all">Все мои флоты</option>`];
  const own = state.fleets;
  if (own.length) opts.push(`<optgroup label="Флот">${own.map((f) => `<option value="fleet:${f.id}">${esc(f.name)}${f.access === "owner" ? "" : ` (от ${esc(f.owner)})`}</option>`).join("")}</optgroup>`);
  const vs = allVessels().filter((v) => !v.mmsi.startsWith("IMO:"));
  if (vs.length) opts.push(`<optgroup label="Судно">${vs.map((v) => `<option value="vessel:${esc(v.mmsi)}">${esc(v.name || v.mmsi)}</option>`).join("")}</optgroup>`);
  $("#rule-target").innerHTML = opts.join("");
  if (selected) $("#rule-target").value = selected;
}

function scopeOptions(selected) {
  const zonesList = (state.zonesFC?.features || []).map((f) => f.properties);
  const byLayer = (l) => zonesList.filter((z) => (z.layer || "jwc") === l)
    .map((z) => `<option value="zone:${esc(z.id)}">${esc(z.name)}</option>`).join("");
  return `<optgroup label="Целым слоем">
      <option value="layer:any">Любая зона</option><option value="layer:jwc">Любая зона JWC</option>
      <option value="layer:ibf">Любая зона IBF/ITF</option><option value="layer:inl">Любой район INL</option></optgroup>
    ${state.rules.areas.length ? `<optgroup label="Мои области">${state.rules.areas.map((a) => `<option value="area:${a.id}">${esc(a.name)}</option>`).join("")}</optgroup>` : ""}
    <optgroup label="Зона JWC">${byLayer("jwc")}</optgroup>
    <optgroup label="Зона IBF/ITF">${byLayer("ibf")}</optgroup>
    <optgroup label="Район INL">${byLayer("inl")}</optgroup>`.replace(`value="${selected}"`, `value="${selected}" selected`);
}

function renderRuleParams(kind, p = {}) {
  const meta = state.rules.meta;
  const num = (name, label, val, step = "any") =>
    `<label>${label}<input type="number" name="${name}" value="${val}" step="${step}" min="0" required></label>`;
  let html = "";
  if (kind.startsWith("zone_")) {
    html = `<label>Какие зоны<select name="scope">${scopeOptions(p.scope || "layer:any")}</select></label>`
      + (kind === "zone_near" ? num("distance_nm", "Ближе, чем (мор. миль)", p.distance_nm ?? 30) : "")
      + (state.rules.areas.length ? "" : `<p class="muted small">Свою область можно нарисовать кнопкой «✎ Нарисовать область».</p>`);
  } else if (kind === "ais_lost") {
    html = num("hours", "Нет сигнала дольше (часов)", p.hours ?? 6);
  } else if (kind === "nav_status") {
    const on = new Set(p.statuses || [1, 5]);
    html = `<div><div class="muted small">Сообщить, когда статус станет:</div><div class="rule-checks">${
      Object.entries(meta.nav).map(([code, name]) => `<label><input type="checkbox" name="statuses" value="${code}" ${on.has(+code) ? "checked" : ""}> ${esc(name)}</label>`).join("")}</div></div>`;
  } else if (kind === "speed") {
    html = `<div class="rule-row"><label>Скорость<select name="op"><option value="below">ниже</option>
        <option value="above" ${p.op === "above" ? "selected" : ""}>выше</option></select></label>
      ${num("knots", "Узлов", p.knots ?? 1)}</div>${num("minutes", "Дольше (минут, 0 — сразу)", p.minutes ?? 30, 1)}`;
  } else if (kind === "destination") {
    html = `<label class="check"><input type="checkbox" name="eta" ${p.eta ? "checked" : ""}> сообщать и при смене ETA</label>`;
  } else if (kind.startsWith("port_")) {
    const name = p.port_id != null && state.ports?.[p.port_id] ? state.ports[p.port_id].name : "";
    html = `<label>Порт<input name="port" list="rule-ports" value="${esc(name)}" placeholder="любой порт — оставьте пустым"></label>
      <datalist id="rule-ports">${(state.ports || []).map((pt) => `<option value="${esc(pt.name)}">${esc(pt.country)}</option>`).join("")}</datalist>
      <p class="muted small">«В порту» — ближе 3 миль к порту и почти без хода; «вышло» — дальше 5 миль.</p>`;
  } else if (kind === "vessel_event") {
    const on = new Set(p.groups || Object.keys(meta.event_groups));
    html = `<div><div class="muted small">Сообщать, когда с судном:</div><div class="rule-checks">${
      Object.entries(meta.event_groups).map(([g, label]) => `<label><input type="checkbox" name="groups" value="${g}" ${on.has(g) ? "checked" : ""}> ${esc(label)}</label>`).join("")}</div>
      <p class="muted small">Важность уведомления берётся из самого события: санкции — критично, обычная новость — информация.</p></div>`;
  } else if (kind === "dest_zone") {
    html = `<label>Какие зоны<select name="scope">${["layer:any", "layer:jwc", "layer:ibf", "layer:inl"].map((v) =>
      `<option value="${v}" ${(p.scope || "layer:any") === v ? "selected" : ""}>${{ "layer:any": "любые", "layer:jwc": "JWC — военные риски", "layer:ibf": "IBF — экипаж", "layer:inl": "INL — навигация" }[v]}</option>`).join("")}</select></label>
      <p class="muted small">Порт назначения берётся из AIS и распознаётся по справочнику UN/LOCODE; сообщение — один раз на рейс.</p>`;
  } else if (kind === "incident") {
    const on = new Set(p.kinds || ["distress", "sart", "mob", "epirb", "aground"]);
    html = num("radius_nm", "В радиусе (мор. миль)", p.radius_nm ?? 50)
      + `<div><div class="muted small">Какие происшествия:</div><div class="rule-checks">${
        Object.entries(meta.incident_kinds).map(([k, label]) => `<label><input type="checkbox" name="kinds" value="${k}" ${on.has(k) ? "checked" : ""}> ${esc(label)}</label>`).join("")}</div></div>`;
  }
  $("#rule-params").innerHTML = html;
}

async function openRuleForm(r = null) {
  if (!state.rules.meta) await loadRules();
  const meta = state.rules.meta;
  const form = $("#rule-form");
  state.rules.editId = r ? r.id : null;
  $("#rule-kind").innerHTML = Object.entries(meta.kinds).map(([k, label]) => `<option value="${k}">${esc(label)}</option>`).join("");
  $("#rule-kind").value = r ? r.kind : "zone_enter";
  ruleTargets(r ? (r.target_type === "all" ? "all" : `${r.target_type}:${r.target}`) : "all");
  renderRuleParams($("#rule-kind").value, r ? r.params : {});
  form.elements.name.value = r ? r.name : "";
  form.severity.value = r ? r.severity : "warning";
  form.audience.value = r ? r.audience : "me";
  $("#btn-rule-save").textContent = r ? "Сохранить изменения" : "Создать уведомление";
  form.hidden = false;
  form.scrollIntoView({ behavior: "smooth", block: "nearest" });
}
$("#rule-kind").onchange = (e) => renderRuleParams(e.target.value);
$("#btn-rule-new").onclick = () => openRuleForm();
// Палитра цвета флота
$("#fleet-color").onclick = (e) => {
  e.stopPropagation();
  const f = currentFleet(), pal = $("#fleet-palette");
  if (!f || f.access !== "owner") { toast("Цвет флота", "Менять цвет может владелец флота"); return; }
  if (!pal.hidden) { pal.hidden = true; return; }
  pal.innerHTML = FLEET_COLORS.map((c) => `<button type="button" class="pal-sw${fleetColor(f.id) === c ? " on" : ""}" data-c="${c}"
    style="--c:${c}" title="${c}"></button>`).join("");
  pal.hidden = false;
  $$(".pal-sw", pal).forEach((b) => b.onclick = async () => {
    pal.hidden = true;
    try {
      await api(`/api/fleets/${f.id}`, { method: "PATCH", json: { color: b.dataset.c } });
      f.color = b.dataset.c;
      renderFleet(); renderVessels(); renderLegend(); renderFeed();
    } catch (err) { toast("Не удалось", err.message, "critical"); }
  });
};
document.addEventListener("click", (e) => { if (!e.target.closest(".fleet-color-wrap")) $("#fleet-palette").hidden = true; });

// Готовые наборы правил
function renderPresets() {
  const meta = state.rules.meta;
  if (!meta?.presets) return;
  const box = $("#preset-list");
  box.innerHTML = meta.presets.map((p) => {
    const left = p.rules.filter((r) => !r.added).length;
    return `<div class="preset">
      <div class="preset-head"><b>${esc(p.title)}</b>
        <button class="btn ${left ? "" : "ghost"}" type="button" data-preset="${p.id}" ${left ? "" : "disabled"}>${left ? (left === p.rules.length ? "Добавить" : `Добавить ещё ${left}`) : "Добавлено"}</button></div>
      <div class="muted small">${esc(p.hint)}</div>
      <ul class="preset-rules">${p.rules.map((r) => `<li class="${r.added ? "on" : ""}">${r.added ? "✓" : "+"} ${esc(r.name)}</li>`).join("")}</ul>
    </div>`;
  }).join("");
  $$("[data-preset]", box).forEach((b) => b.onclick = async () => {
    const { added } = await api("/api/rules/preset", { json: { id: b.dataset.preset } });
    toast("Набор добавлен", `Новых уведомлений: ${added}`);
    await loadRules();
  });
  // правил нет — наборы раскрыты; есть — свёрнуты (раскрыть можно самому)
  if (!state.rules.presetsTouched) $("#rule-presets").open = !state.rules.items.length;
}
$("#rule-presets").addEventListener("toggle", () => { state.rules.presetsTouched = true; });

$("#btn-rules-defaults").onclick = async () => {
  const { added } = await api("/api/rules/defaults", { method: "POST" });
  toast("Стандартные уведомления", added ? `Добавлено: ${added}` : "Все на месте");
  await loadRules();
};
$("#btn-rule-cancel").onclick = () => { $("#rule-form").hidden = true; };

$("#rule-form").onsubmit = async (e) => {
  e.preventDefault();
  const form = e.target;
  const kind = form.kind.value;
  const params = {};
  const fd = new FormData(form);
  for (const k of ["scope", "distance_nm", "hours", "op", "knots", "minutes", "radius_nm", "layer"]) if (fd.has(k)) params[k] = fd.get(k);
  if (kind === "vessel_event") params.groups = fd.getAll("groups");
  if (kind === "nav_status") params.statuses = fd.getAll("statuses").map(Number);
  if (kind === "incident") params.kinds = fd.getAll("kinds");
  if (kind === "destination") params.eta = fd.has("eta");
  if (kind.startsWith("port_")) {
    const name = (fd.get("port") || "").trim();
    const port = name ? (state.ports || []).find((p) => p.name.toLowerCase() === name.toLowerCase()) : null;
    if (name && !port) { toast("Порт не найден", "Выберите порт из подсказки или оставьте поле пустым", "warning"); return; }
    params.port_id = port ? port.id : "any";
  }
  // у <form> уже есть свойства name и target, поэтому эти поля берём через elements
  const tval = form.elements.target.value;
  const [ttype, target] = tval.includes(":") ? tval.split(/:(.*)/s) : ["all", ""];
  const body = { kind, params, target_type: ttype, target, name: form.elements.name.value, severity: form.severity.value,
                 audience: form.audience.value };
  try {
    const id = state.rules.editId;
    await api(id ? `/api/rules/${id}` : "/api/rules", { method: id ? "PATCH" : "POST", json: body });
    toast(id ? "Уведомление изменено" : "Уведомление создано", "Сообщения будут приходить в «Ленту» и на колокольчик");
    form.hidden = true;
    await loadRules();
  } catch (err) { toast("Не удалось сохранить", err.message, "critical"); }
};

// --- мои области: список и показ на карте
function renderAreas() {
  const areas = state.rules.areas;
  areasLayer.clearLayers();
  if ($("#areas-show").checked) {
    areas.forEach((a) => L.polygon(a.geometry.coordinates[0].map(([x, y]) => [y, x]), {
      renderer: mapCanvas, interactive: false, color: "#ffffff", weight: 1.5, dashArray: "6 5", fillOpacity: 0.06,
    }).addTo(areasLayer));
  }
  $("#area-list").innerHTML = areas.map((a) => `
    <li class="rule-item" data-id="${a.id}"><div class="ri-main"><div class="ri-name">${esc(a.name)}</div>
      <div class="muted small">${a.geometry.coordinates[0].length - 1} точек</div></div>
      <div class="ri-actions"><button class="text-btn ar-show" type="button">Показать</button>
        <button class="text-btn ar-rename" type="button">Переименовать</button>
        <button class="text-btn danger ar-del" type="button" title="Удалить">✕</button></div></li>`).join("")
    || `<li class="empty muted">Областей нет. Нарисуйте свою — её можно использовать в уведомлениях «вход/выход/подход к зоне».</li>`;
  $$("#area-list .rule-item").forEach((el) => {
    const a = areas.find((x) => x.id === +el.dataset.id);
    const ll = a.geometry.coordinates[0].map(([x, y]) => [y, x]);
    el.querySelector(".ar-show").onclick = () => map.fitBounds(ll, { padding: [40, 40] });
    el.querySelector(".ar-rename").onclick = async () => {
      const name = prompt("Новое название области:", a.name);
      if (!name) return;
      await api(`/api/areas/${a.id}`, { method: "PATCH", json: { name } });
      await loadRules();
    };
    el.querySelector(".ar-del").onclick = async () => {
      if (!confirm(`Удалить область «${a.name}»?`)) return;
      try { await api(`/api/areas/${a.id}`, { method: "DELETE" }); await loadRules(); }
      catch (e) { toast("Нельзя удалить", e.message, "warning"); }
    };
  });
}
$("#areas-show").onchange = () => { renderAreas(); renderLegend(); };

// --- рисование области: клики по карте ставят углы многоугольника
const draw = { on: false, pts: [], layer: L.layerGroup().addTo(map) };

function setMapInteractive(on) {
  // во время рисования клики по зонам, портам и судам не должны открывать карточки
  [zonesLayer, portsLayer, trafficLayer].forEach((g) => g.eachLayer((l) => { if (l.options) l.options.interactive = on; }));
}

function redrawSketch() {
  draw.layer.clearLayers();
  const ll = draw.pts;
  if (ll.length > 1) L.polyline(ll.length > 2 ? [...ll, ll[0]] : ll, { color: "#4fc3d9", weight: 2, dashArray: "5 4", interactive: false }).addTo(draw.layer);
  ll.forEach((p) => L.circleMarker(p, { radius: 4, color: "#fff", weight: 1.5, fillColor: "#4fc3d9", fillOpacity: 1, interactive: false }).addTo(draw.layer));
  $("#draw-count").textContent = `Точек: ${ll.length}`;
  $("#btn-draw-done").disabled = ll.length < 3;
}

function startDraw() {
  draw.on = true; draw.pts = [];
  setMapInteractive(false);
  document.body.classList.add("drawing");
  $("#draw-hint").hidden = false;
  redrawSketch();
}

function stopDraw() {
  draw.on = false; draw.pts = [];
  draw.layer.clearLayers();
  setMapInteractive(true);
  document.body.classList.remove("drawing");
  $("#draw-hint").hidden = true;
}

map.on("click", (e) => {
  if (!draw.on) return;
  const { lat, lng } = e.latlng.wrap();
  draw.pts.push([lat, lng]);
  redrawSketch();
});
$("#btn-area-draw").onclick = () => (draw.on ? stopDraw() : startDraw());
$("#btn-draw-undo").onclick = () => { draw.pts.pop(); redrawSketch(); };
$("#btn-draw-cancel").onclick = stopDraw;
document.addEventListener("keydown", (e) => { if (draw.on && e.key === "Escape") stopDraw(); });
$("#btn-draw-done").onclick = async () => {
  const name = prompt("Название области:", "Моя область");
  if (name === null) return;
  const ring = draw.pts.map(([lat, lng]) => [lng, lat]);
  ring.push(ring[0]);
  try {
    await api("/api/areas", { json: { name, geometry: { type: "Polygon", coordinates: [ring] } } });
    toast("Область сохранена", "Её можно выбрать в уведомлениях «вход / выход / подход к зоне»");
    stopDraw();
    await loadRules();
  } catch (err) { toast("Не удалось сохранить", err.message, "critical"); }
};

/* ------------------------------------------------------------------ происшествия */
const INC_STYLE = {
  distress: { color: "#ff3b30", glyph: "!" }, sart: { color: "#ff3b30", glyph: "S" },
  mob: { color: "#ff3b30", glyph: "M" }, epirb: { color: "#ff3b30", glyph: "E" },
  aground: { color: "#ff9440", glyph: "▼" }, nuc: { color: "#f5b13d", glyph: "?" },
  sar: { color: "#4dabf7", glyph: "✈" }, safety: { color: "#ffe066", glyph: "i" },
  sanction: { color: "#c084fc", glyph: "$" }, sanction_new: { color: "#a78bfa", glyph: "$" },
  detention: { color: "#fb923c", glyph: "⚓" },
  news_risk: { color: "#f472b6", glyph: "N" }, news: { color: "#64748b", glyph: "N" },
  dark: { color: "#a78bfa", glyph: "◐" }, sts: { color: "#f59e0b", glyph: "⇄" },         // [подозрительное-поведение]
  spoof: { color: "#ef4444", glyph: "⌁" }, identity: { color: "#38bdf8", glyph: "ID" },  // [подозрительное-поведение]
  world_critical: { color: "#e11d48", glyph: "◉" }, world_news: { color: "#0891b2", glyph: "◉" },  // [мировые-новости]
};
const incidentsLayer = L.layerGroup().addTo(map);
const incMarkers = new Map();          // id происшествия → его метка (в одной точке их бывает несколько)
// По умолчанию скрыто то, что чаще всего шум: «не управляется», аварийные маяки (обычно буи рыболовных сетей
// и тесты), служебные сообщения. Выбор пользователя запоминается. У судов своих флотов видно всё.
const INC_HIDDEN_DEFAULT = ["nuc", "sart", "epirb", "mob", "sar", "safety"];
function savedIncHidden() {
  try { const v = JSON.parse(localStorageGet("incHidden")); return Array.isArray(v) ? v : INC_HIDDEN_DEFAULT; }
  catch { return INC_HIDDEN_DEFAULT; }
}
function saveIncHidden() { localStorageSet("incHidden", JSON.stringify([...state.inc.hidden])); }
const isMine = (mmsi) => (state.mineSet ||= new Set(allVessels().map((v) => v.mmsi))).has(mmsi);
state.inc = { items: [], kinds: {}, hidden: new Set(savedIncHidden()), loadedAt: 0,
  showRead: false,                                    // просмотренные на карте — до перезагрузки (не «залипает»)
  peek: null };                                       // открыли из ленты — метку показываем, даже если просмотрена
$("#inc-read-show").checked = state.inc.showRead;
$("#inc-read-show").onchange = (e) => setIncShowRead(e.target.checked);
function setIncShowRead(on) {
  state.inc.showRead = on;
  $("#inc-read-show").checked = on;
  renderIncidents();
  renderLegend();
}

// Происшествие просмотрено (открыли на карте или в ленте): его метка уйдёт с карты, когда окно закроется
function markIncRead(it) {
  if (it.read) return;
  it.read = true;
  api("/api/feed/mark", { json: { items: [`i:${it.id}`], read: true } }).catch(() => {});
  // происшествие с судном вашего флота пришло в ленту уведомлением правила — оно тоже прочитано
  const notes = state.notifications.filter((n) => !n.is_read && sameEvent(n, it));
  if (notes.length) {
    notes.forEach((n) => { n.is_read = 1; });
    setUnread(Math.max(0, state.unread - notes.length));
    api("/api/notifications/read", { json: { ids: notes.map((n) => n.id) } }).catch(() => {});
  }
  renderFeed();
  renderLegend();
}
// уведомление «СУДНО (MMSI …): Пропал AIS…» и происшествие того же судна с тем же названием — одно событие
function sameEvent(n, it) {
  return n.kind === "rule_event" && n.mmsi === it.mmsi
    && (n.title || "").toLowerCase().endsWith(": " + (it.label || "").toLowerCase());
}

function incIcon(it) {
  const st = INC_STYLE[it.kind] || INC_STYLE.safety;
  const urgent = it.severity === "critical" && !it.test && !it.read;
  return L.divIcon({ className: `inc-icon${urgent ? " urgent" : ""}${it.test ? " test" : ""}${it.read ? " seen" : ""}`, iconSize: [24, 24],
    html: `<span style="background:${st.color}">${st.glyph}</span>` });
}

function incVisible(it) {
  return (isMine(it.mmsi) || !state.inc.hidden.has(it.kind)) && (it.test ? $("#inc-tests").checked : true);
}

async function loadIncidents(force = false) {
  if (!force && Date.now() - state.inc.loadedAt < 60000) return;     // раз в минуту достаточно
  const [d, sc] = await Promise.all([api(`/api/incidents?days=${$("#inc-days").value}`), api("/api/screening")]);
  renderScreeningStatus(sc);
  state.inc.items = d.items;
  state.inc.kinds = d.kinds;
  state.inc.loadedAt = Date.now();
  renderIncidents();
  renderLegend();                  // типы происшествий и их число в легенде
  renderVessels();                 // пометка «тревожные новости» на значках судов флота
}

function incPopup(it) {
  return `<h4>${esc(it.label)}${it.test ? ` <span class="tag">тест</span>` : ""}</h4>
    <div class="muted small">${it.mmsi ? `${shipLink(it.name || "MMSI " + it.mmsi, it.mmsi)} · ` : it.name ? `${esc(it.name)} · ` : ""}${fmtTime(it.ts)}${it.count > 1 ? (isNews(it.kind) ? ` · ${sourcesText(it.count)}, последний ${ago(it.last_seen)}`
      : ` · повторялось ${it.count} раз, последний ${ago(it.last_seen)}`) : ""}</div>
    ${it.text ? `<p class="inc-text">«${esc(it.text)}»</p>` : ""}
    ${/^https?:\/\//.test(it.extra || "") ? `<p><a href="${esc(it.extra)}" target="_blank" rel="noopener">Открыть статью ↗</a></p>`
      : it.extra ? `<p class="muted small">${esc(it.extra)}</p>` : ""}
    <dl class="kv">${it.mmsi ? `<dt>${/^IMO:/.test(it.mmsi) ? "IMO" : "MMSI"}</dt><dd>${esc(it.mmsi.replace(/^IMO:/, ""))}</dd>` : ""}
      ${it.lat != null ? `<dt>${it.vessel_bound ? "Сейчас" : it.kind.startsWith("world_") ? "Место (примерно)" : "Позиция"}</dt><dd>${it.lat.toFixed(4)}, ${it.lon.toFixed(4)}</dd>` : ""}
      <dt>Источник</dt><dd>${esc(it.source || "AIS")}</dd></dl>
    ${popActs(it.starred)}`;
}

// «Важное» и «Удалить» в окне события на карте — те же отметки, что на карточке в ленте
function popActs(starred) {
  return `<div class="pop-acts"><button type="button" class="btn ghost${starred ? " on" : ""}" data-pop="star">${starred ? "★ Важное" : "☆ Важное"}</button>
    <button type="button" class="btn ghost" data-pop="del">✕ Удалить</button></div>`;
}
function bindPopActs(popup, x, refresh) {
  const el = popup.getElement();
  if (!el) return;
  el.querySelector("[data-pop=star]")?.addEventListener("click", async () => {
    const item = x.n || x.it;
    await feedMark(x, { starred: !item.starred }).catch((e) => toast("Не получилось", e.message, "critical"));
    refresh();
  });
  el.querySelector("[data-pop=del]")?.addEventListener("click", () => {
    map.closePopup();
    feedMark(x, { hidden: true }).catch((e) => toast("Не получилось", e.message, "critical"));
  });
}

function renderIncidents() {
  const inc = state.inc;
  const counts = {};
  inc.items.forEach((it) => { if (!it.test || $("#inc-tests").checked) counts[it.kind] = (counts[it.kind] || 0) + 1; });
  // текстовые сообщения о безопасности и спасательную авиацию aisstream не передаёт (проверено 21.10.2026) —
  // показываем эти типы, только если такие события всё же появятся
  $("#inc-kinds").innerHTML = Object.entries(inc.kinds).filter(([k]) => !["safety", "sar"].includes(k) || counts[k])
    .map(([k, d]) => `
    <button class="fchip ${inc.hidden.has(k) ? "" : "on"}" data-kind="${k}">
      <span class="inc-dot" style="background:${INC_STYLE[k].color}">${INC_STYLE[k].glyph}</span>${esc(d.label)}
      <span class="muted">${counts[k] || 0}</span></button>`).join("");
  $$("#inc-kinds .fchip").forEach((b) => b.onclick = () => {
    if (inc.hidden.has(b.dataset.kind)) inc.hidden.delete(b.dataset.kind); else inc.hidden.add(b.dataset.kind);
    saveIncHidden();
    renderIncidents();
    renderLegend();
  });
  const shown = inc.items.filter(incVisible);
  incidentsLayer.clearLayers();
  incMarkers.clear();
  // санкции и задержания судов флота показаны пометкой на значке самого судна — отдельный значок не нужен
  const ownMarker = (it) => it.vessel_bound && markers.has(it.mmsi);
  if ($("#inc-show").checked) {
    shown.filter((it) => it.lat != null && !ownMarker(it) && (inc.showRead || !it.read || it.id === inc.peek)).forEach((it) =>
      incMarkers.set(it.id, L.marker([it.lat, it.lon], { icon: incIcon(it), zIndexOffset: 2000 }))
        .get(it.id)
        .bindTooltip(esc(`${it.label}: ${it.name || it.mmsi}`), { direction: "top", offset: [0, -10] })
        .bindPopup(incPopup(it), { minWidth: 240, maxWidth: 320 })
        .on("popupopen", (e) => {
          markIncRead(it);
          const m = e.target;
          const bind = () => bindPopActs(m.getPopup(), { type: "inc", it }, () => { m.setPopupContent(incPopup(it)); bind(); });
          bind();
        })
        .on("popupclose", () => {
          if (inc.peek === it.id) inc.peek = null;
          if (!inc.showRead) setTimeout(renderIncidents, 0);      // просмотренное убираем с карты
        }));
    placeIncMarkers();
  }
  renderFeed();
}

// На мелком масштабе соседние метки (ближе INC_CELL пикселей) — одним кружком с числом; нажатие — приблизить
const INC_CLUSTER_UNTIL = 8, INC_CELL = 46;
const SEV_RANK = { critical: 2, warning: 1, info: 0 };
function placeIncMarkers() {
  const z = map.getZoom();
  const items = state.inc.items.filter((it) => incMarkers.has(it.id));
  if (z >= INC_CLUSTER_UNTIL) { items.forEach((it) => incMarkers.get(it.id).addTo(incidentsLayer)); return; }
  const cells = new Map();
  for (const it of items) {
    const p = map.latLngToLayerPoint(incMarkers.get(it.id).getLatLng());
    const key = `${Math.floor(p.x / INC_CELL)}:${Math.floor(p.y / INC_CELL)}`;
    if (!cells.has(key)) cells.set(key, []);
    cells.get(key).push(it);
  }
  for (const g of cells.values()) {
    if (g.length < 2 || g.some((it) => it.id === state.inc.peek)) { g.forEach((it) => incMarkers.get(it.id).addTo(incidentsLayer)); continue; }
    const sev = g.reduce((a, it) => (SEV_RANK[it.severity] > SEV_RANK[a] ? it.severity : a), "info");
    const kinds = {};
    g.forEach((it) => { kinds[it.label] = (kinds[it.label] || 0) + 1; });
    const b = L.latLngBounds(g.map((it) => [it.lat, it.lon]));
    L.marker(b.getCenter(), {
      icon: L.divIcon({ className: `inc-cluster ${sev}`, iconSize: [30, 30], iconAnchor: [15, 15], html: `<span>${g.length}</span>` }),
      zIndexOffset: 1900, keyboard: false,
    }).bindTooltip(Object.entries(kinds).sort((a, c) => c[1] - a[1]).map(([k, n]) => `${esc(k)}: ${n}`).join("<br>"),
      { direction: "top", offset: [0, -14] })
      .on("click", () => smoothFit(b.pad(0.6), { maxZoom: Math.max(z + 3, 7) }))
      .addTo(incidentsLayer);
  }
}
let incZoomWasClustered = true;
map.on("zoomend", () => {                       // перегруппировка нужна только на мелком масштабе (или при переходе через порог)
  const now = map.getZoom() < INC_CLUSTER_UNTIL;
  if ((now || incZoomWasClustered) && state.inc?.items.length) renderIncidents();
  incZoomWasClustered = now;
});

/* ------------------------------------------------------------------ лента */
// Одна лента: уведомления (что сообщили ваши правила, системные сообщения) + происшествия в мире.
// События с вашими судами приходят только как уведомления правил, чтобы не дублировались.
state.feedFilter = "all";
const SYSTEM_KINDS = new Set(["circular", "system"]);

function feedItems() {
  const f = state.feedFilter, unreadOnly = $("#n-unread-only").checked;
  const mine = new Set(allVessels().map((v) => v.mmsi));
  const out = [];
  const starOnly = f === "starred";
  if (f !== "world") {
    for (const n of state.notifications) {
      const sys = SYSTEM_KINDS.has(n.kind) || !n.mmsi;
      if ((f === "mine" && sys) || (f === "system" && !sys) || (starOnly && !n.starred)) continue;
      out.push({ type: "notif", ts: n.ts, n, sys });
    }
  }
  if (f === "all" || f === "world" || starOnly) {
    for (const it of state.inc?.items || []) {
      if (starOnly ? !it.starred : (!incVisible(it) || mine.has(it.mmsi))) continue;
      if (unreadOnly && it.read) continue;
      out.push({ type: "inc", ts: it.last_seen, it });
    }
  }
  return out.sort((a, b) => (a.ts < b.ts ? 1 : -1)).slice(0, 400);
}

// «IKARIA ANGEL (MMSI 357321000): ВЫХОД из зоны «IBF ERZ…»» → судно, MMSI, событие, зона
function splitTitle(title, mmsi) {
  const m = mmsi && /^(.*?) \(MMSI (\d+)\): (.*)$/s.exec(title || "");
  const rest = m ? m[3] : (mmsi && /^MMSI \d+: /.test(title) ? title.replace(/^MMSI \d+: /, "") : title);
  const vessel = m ? m[1] : (mmsi && rest !== title ? "" : null);
  const z = /^(.*?)\s*«(.+)»\s*(.*)$/s.exec(rest || "");
  // «вход в «X»» → «вход в зону» (зона — отдельной строкой)
  let event = z ? (z[3] ? `${z[1]}${/ (в|из|к)$/.test(z[1]) ? " зону" : ""} ${z[3]}` : z[1]).trim() : rest || "";
  event = event.replace(/:$/, "");
  return { vessel, mmsi: m ? m[2] : mmsi, event: event.charAt(0).toUpperCase() + event.slice(1), zone: z ? z[2] : null };
}

// подробности по строкам: «Позиция …. Время в зоне: 3.2 ч.» → две строки
function detailLines(text) {
  return (text || "").split(/(?<=[.!])\s+(?=[А-ЯЁA-Z«])/).map((s) => s.trim().replace(/\.$/, "")).filter(Boolean);
}

function feedRow(x, i) {
  if (x.type === "notif") {
    const n = x.n;
    const [first, ...rest] = (n.body || "").split("\n");
    const t = splitTitle(n.title, n.mmsi);
    const lines = detailLines(first);
    return `<li class="feed-item ${n.severity} ${n.is_read ? "read" : "unread"}${n.starred ? " starred" : ""}" data-i="${i}">
      <div class="fi-meta"><span class="fi-src">${x.sys ? "система" : "мои суда"}</span>${notifPoint(n) ? `<span class="fi-map" title="Нажмите — покажу на карте">◎ на карте</span>` : ""}<span class="fi-time">${fmtTime(n.ts)}</span>${feedActs(n.starred)}</div>
      <div class="fi-event">${esc(t.event)}</div>
      ${t.vessel != null ? `<div class="fi-vessel">${t.vessel ? shipLink(t.vessel, t.mmsi) : ""} <span class="muted">MMSI ${esc(t.mmsi)}</span> ${n.mmsi ? fleetChips(n.mmsi) : ""}</div>` : ""}
      ${t.zone ? `<div class="fi-zone">${esc(t.zone)}</div>` : ""}
      ${lines.length ? `<ul class="fi-lines">${lines.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}
      ${rest.length ? `<div class="fi-rule">${esc(rest.join(" ").replace(/^((?:Правило|Уведомление) «[^»]*»).*$/s, "$1").replace(/^Правило /, "Уведомление ").replace(/\.$/, ""))}</div>` : ""}</li>`;
  }
  const it = x.it, st = INC_STYLE[it.kind] || INC_STYLE.safety;
  const link = /^https?:\/\//.test(it.extra || "") ? `<a class="small" href="${esc(it.extra)}" target="_blank" rel="noopener">${esc(it.source || "статья")} ↗</a>${
    isNews(it.kind) && it.count > 1 ? ` <span class="muted small">и ещё ${sourcesText(it.count - 1)}</span>` : ""}` : "";
  return `<li class="feed-item ${it.severity} ${it.read ? "read" : "unread"} world${it.starred ? " starred" : ""}" data-i="${i}">
    <div class="fi-meta"><span class="inc-dot" style="background:${st.color}">${st.glyph}</span><span class="fi-src">в мире</span><span class="fi-time">${fmtTime(it.last_seen)}${
      it.count > 1 && !isNews(it.kind) ? ` · повторялось ${it.count} раз` : ""}</span>${feedActs(it.starred)}</div>
    <div class="fi-event">${esc(it.label)}${it.test ? ` <span class="tag">тест</span>` : ""}</div>
    ${it.mmsi || it.name ? `<div class="fi-vessel">${it.name ? `${shipLink(it.name, it.mmsi)} ` : ""}${it.mmsi ? `<span class="muted">${shipIdText(it.mmsi)}</span>` : ""}</div>` : ""}
    ${it.text ? `<div class="fi-quote">${esc(it.text)}</div>` : ""}${link ? `<div class="fi-link">${link}</div>` : ""}</li>`;
}

// «Важное» и «удалить из ленты» — личные отметки пользователя
function feedActs(starred) {
  return `<span class="fi-acts"><button type="button" class="fi-act fi-star${starred ? " on" : ""}" data-act="star"
      title="${starred ? "Убрать из важного" : "Пометить важным"}" aria-pressed="${!!starred}">${starred ? "★" : "☆"}</button><button
      type="button" class="fi-act" data-act="del" title="Удалить из моей ленты" aria-label="Удалить">✕</button></span>`;
}
// Событие судна своего флота живёт в двух видах — уведомлением в ленте и меткой происшествия на карте.
// Для пользователя это одно событие: «важное» и «удалить» применяются к обоим сразу.
function eventTwins(x) {
  return x.n ? (state.inc?.items || []).filter((it) => sameEvent(x.n, it)).map((it) => ({ type: "inc", it }))
    : state.notifications.filter((n) => sameEvent(n, x.it)).map((n) => ({ type: "notif", n }));
}
async function feedMark(x, change) {
  const all = [x, ...eventTwins(x)];
  const keys = all.map((y) => (y.type === "notif" ? `n:${y.n.id}` : `i:${y.it.id}`));
  const r = await api("/api/feed/mark", { json: { items: keys, ...change } });
  if ("starred" in change) all.forEach((y) => { (y.n || y.it).starred = change.starred; });
  if (change.hidden) {
    const gone = new Set(all.map((y) => y.n || y.it));
    state.notifications = state.notifications.filter((n) => !gone.has(n));
    state.inc.items = state.inc.items.filter((it) => !gone.has(it));
    renderIncidents();
    setUnread(r.unread);
    loadTrash().then(updateTrashCount).catch(() => {});
    toast("Удалено в корзину", "Нажмите здесь, чтобы вернуть. В корзине хранится 7 дней", "info", async () => {
      const back = await api("/api/feed/mark", { json: { items: keys, hidden: false } });
      setUnread(back.unread);
      await Promise.all([loadNotifications(), loadIncidents(true)]);
    });
  }
  renderFeed();
}

// Корзина: удалённое за 7 дней, у каждого — «Вернуть»
state.trash = [];
async function loadTrash() {
  const d = await api("/api/feed/trash");
  state.trash = d.items;
  if (state.feedFilter === "trash") renderFeed();
}
// пара «уведомление + метка» одного события своего судна — в корзине одной строкой (уведомлением)
function trashTwin(a, b) {
  const [n, i] = a.type === "notif" ? [a, b] : [b, a];
  return n.type === "notif" && i.type === "inc" && n.mmsi && n.mmsi === i.mmsi
    && (n.title || "").toLowerCase().endsWith(": " + (i.title || "").toLowerCase());
}
function renderTrash() {
  const sevCls = (x) => x.severity || "info";
  const list = state.trash.filter((t) => !(t.type === "inc" && state.trash.some((o) => o !== t && trashTwin(o, t))));
  $("#feed-list").innerHTML = (list.length ? `<li class="trash-note muted small">Удалённое хранится здесь 7 дней, потом удаляется насовсем.</li>` : "")
    + list.map((x, i) => `<li class="feed-item ${sevCls(x)} read trash" data-i="${i}">
      <div class="fi-meta"><span class="fi-src">${x.type === "inc" ? "в мире" : x.mmsi ? "мои суда" : "система"}</span>
        <span class="fi-time">удалено ${fmtTime(x.deleted_at)}</span></div>
      <div class="fi-event">${esc(x.type === "inc" ? x.title : splitTitle(x.title, x.mmsi).event)}</div>
      ${x.mmsi ? `<div class="fi-vessel">${shipLink(x.type === "inc" ? (x.name || x.mmsi) : (splitTitle(x.title, x.mmsi).vessel || x.mmsi), x.mmsi)}
        <span class="muted">${shipIdText(x.mmsi)}</span></div>` : ""}
      ${x.text ? `<div class="fi-quote">${esc(x.text.slice(0, 200))}</div>` : ""}
      <div class="trash-row"><span class="muted small">событие от ${fmtTime(x.ts)}</span>
        <button class="btn ghost" type="button" data-restore="${i}">Вернуть в ленту</button></div></li>`).join("")
    || `<li class="empty muted">Корзина пуста. Удалённое из ленты (✕ на карточке) хранится здесь 7 дней.</li>`;
  $$("#feed-list [data-restore]").forEach((b) => b.onclick = async () => {
    const x = list[+b.dataset.restore];
    b.disabled = true;
    try {
      // вместе с «двойником» (уведомление ↔ метка того же события), если он тоже в корзине
      const twins = state.trash.filter((t) => t !== x && trashTwin(x, t));
      const r = await api("/api/feed/mark", { json: { items: [x.item, ...twins.map((t) => t.item)], hidden: false } });
      state.trash = state.trash.filter((t) => !twins.includes(t));
      setUnread(r.unread);
      state.trash = state.trash.filter((t) => t !== x);
      renderFeed();
      toast("Возвращено в ленту", x.title);
      await Promise.all([loadNotifications(), loadIncidents(true)]);
    } catch (e) { b.disabled = false; toast("Не получилось", e.message, "critical"); }
  });
  updateTrashCount();
}
function updateTrashCount() {
  const n = state.trash.filter((t) => !(t.type === "inc" && state.trash.some((o) => o !== t && trashTwin(o, t)))).length;
  const b = $("#panel-events [data-feed=trash] .ico-n");
  b.hidden = !n;
  b.textContent = n;
}

function renderFeed() {
  // в корзине «только новые» и «прочитать всё» ни к чему
  $("#feed-new").hidden = $("#btn-read-all").hidden = state.feedFilter === "trash";
  if (state.feedFilter === "trash") { renderTrash(); return; }
  const items = feedItems();
  const empty = { all: "Пока ничего нет.", mine: "По вашим судам событий нет. О чём сообщать — настраивается во вкладке «Уведомления».",
    world: "Происшествий в мире за выбранный период нет.", system: "Системных сообщений нет.",
    starred: "Важного пока нет. Нажмите ☆ на карточке события, чтобы оно попало сюда." }[state.feedFilter];
  $("#feed-list").innerHTML = items.map(feedRow).join("") || `<li class="empty muted">${empty}</li>`;
  $$("#feed-list .feed-item").forEach((el) => el.onclick = (e) => {
    if (e.target.closest("a")) return;           // ссылка на статью открывается сама
    const x = items[+el.dataset.i];
    const act = e.target.closest("[data-act]");
    if (act) {
      feedMark(x, act.dataset.act === "star" ? { starred: !(x.n || x.it).starred } : { hidden: true })
        .catch((err) => toast("Не получилось", err.message, "critical"));
      return;
    }
    if (x.type === "notif") { openNotification(x.n); return; }
    const it = x.it;
    if (it.lat == null) { markIncRead(it); return; }
    if (it.read && !state.inc.showRead) { state.inc.peek = it.id; renderIncidents(); }   // просмотренное — вернём метку на время
    smoothTo([it.lat, it.lon], Math.max(map.getZoom(), 9));
    setTimeout(() => incMarkers.get(it.id)?.openPopup(), 1250);
  });
}
$$("#panel-events [data-feed]").forEach((b) => b.onclick = () => {
  state.feedFilter = b.dataset.feed;
  $$("#panel-events [data-feed]").forEach((x) => x.classList.toggle("on", x === b));
  if (state.feedFilter === "trash") loadTrash().catch((e) => toast("Ошибка", e.message, "critical"));
  else if (state.feedFilter !== "mine" && state.feedFilter !== "system") loadIncidents(true).catch(() => {});
  renderFeed();
});
function renderScreeningStatus(sc) {
  const c = sc.sanctions?.counts || {};
  $("#screen-status").innerHTML = `<b>Санкции и задержания</b>
    <div class="muted small">Суда в списках: ${Object.entries(sc.sources).map(([k, v]) =>
      `<a href="${esc(v.page)}" target="_blank" rel="noopener">${esc(v.label)}</a> ${c[k] ?? "—"}`).join(" · ")}<br>
      Задержания: <a href="${esc(sc.tokyo_page)}" target="_blank" rel="noopener">Tokyo MoU</a> ${sc.detentions?.count ?? "—"}
      · обновлено ${sc.last_run ? ago(sc.last_run) : "ещё не было"}${sc.running ? " · ⏳ обновляется" : ""}</div>
    ${(sc.sanctions?.errors || []).map((e) => `<div class="small" style="color:var(--red)">${esc(e)}</div>`).join("")}
    ${sc.last_error ? `<div class="small" style="color:var(--red)">${esc(sc.last_error)}</div>` : ""}`;
}
$("#btn-screen-refresh").onclick = async (e) => {
  const b = e.target; b.disabled = true; b.textContent = "Обновляю…";
  try { const r = await api("/api/screening", { method: "POST" }); toast("Списки обновлены", r.message); }
  catch (err) { toast("Не удалось обновить", err.message, "critical"); }
  b.disabled = false; b.textContent = "Обновить списки сейчас";
  await loadIncidents(true); await loadFleets();
};
$("#inc-show").onchange = () => { renderIncidents(); renderLegend(); };
$("#inc-tests").onchange = renderIncidents;
$("#inc-days").onchange = () => loadIncidents(true).catch(() => {});

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
        <span class="muted small">${esc(s.type_name || "")}${/^IMO:/.test(s.mmsi) ? "" : ` · MMSI ${esc(s.mmsi)}`}${s.imo ? ` · IMO ${esc(s.imo)}` : ""}${s.hin ? ` · HIN ${esc(s.hin)}` : ""}</span></span>
      <span class="muted small">${s.lat != null ? (s.age_s > 7200 ? `видели ${fmtAge(s.age_s)}` : fmtAge(s.age_s)) : "нет позиции"}</span>
    </button>`).join("")
    : `<div class="sr-empty muted small">Не найдено. Такое судно ни разу не попадало в зону покрытия береговых станций AIS
      с тех пор, как сайт ведёт справочник, и его нет в санкционных списках и задержаниях.</div>`;
  box.hidden = false;
  $$(".sr-item", box).forEach((b) => b.onclick = () => {
    const s = items[+b.dataset.i];
    box.hidden = true;
    if (s.lat == null) { openShip(s.mmsi, s.name); return; }     // позиции нет — карточка с тем, что знаем
    smoothTo([s.lat, s.lon], Math.max(map.getZoom(), 11));
    trackLayer.clearLayers();
    L.circleMarker([s.lat, s.lon], { radius: 14, weight: 2.5, color: isLight() ? "#0f172a" : "#ffffff", fill: false, dashArray: "4 3" })
      .addTo(trackLayer);
    setTimeout(() => {
      if (s.in_fleet) selectVessel(s.mmsi, false);
      else openTrafficShip(s.mmsi, L.latLng(s.lat, s.lon));
    }, 1150);
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
  updateRiskBar();
  $("#bell-count").hidden = !n;
  $("#bell-count").textContent = n > 99 ? "99+" : n;
  $("#tab-alert-count").hidden = !n;
  $("#tab-alert-count").textContent = n;
  $("#rail-count").hidden = !n;                              // и на свёрнутой панели
  $("#rail-count").textContent = n > 99 ? "99+" : n;
  document.title = n ? `(${n}) FleetRisk` : "FleetRisk";
}

function renderNotifications() { renderFeed(); }
async function openNotification(n) {
  if (!n) return;
  // то же событие на карте — отдельная метка происшествия: она тоже просмотрена и уходит с карты
  const twins = (state.inc?.items || []).filter((it) => !it.read && sameEvent(n, it));
  if (twins.length) { twins.forEach(markIncRead); if (!state.inc.showRead) renderIncidents(); }
  if (!n.is_read) {
    await api("/api/notifications/read", { json: { ids: [n.id] } });
    n.is_read = 1;
    setUnread(Math.max(0, state.unread - 1));
    renderNotifications();
  }
  const at = notifPoint(n);
  if (at) showEventSpot(n, at);
  else if (n.mmsi) selectVessel(n.mmsi, true);
  else if (n.kind === "circular") openCirculars();
}

// Где произошло событие: точка из уведомления, у старых — «Позиция 31.8620, 34.6239» из текста
function notifPoint(n) {
  if (n.lat != null && n.lon != null) return [n.lat, n.lon];
  const m = /Позиция (-?\d+(?:\.\d+)?), (-?\d+(?:\.\d+)?)/.exec(n.body || "");
  return m ? [+m[1], +m[2]] : null;
}

// Метка события на карте: пульсирующее кольцо, краткое описание, связанная зона подсвечена
const eventSpot = L.layerGroup().addTo(map);
function showEventSpot(n, at, opts = {}) {
  eventSpot.clearLayers();
  if (!opts.keepPanel) shipPanel.close();
  const t = splitTitle(n.title, n.mmsi);
  const [first] = (n.body || "").split("\n");
  const color = { critical: "#ff5a4e", warning: "#f5b13d", info: "#4fc3d9" }[n.severity] || "#4fc3d9";
  const html = `<div class="ev-pop"><div class="fi-meta"><span class="fi-src">${n.mmsi ? "мои суда" : "система"}</span><span class="fi-time">${fmtTime(n.ts)}</span></div>
    <div class="fi-event">${esc(t.event)}</div>
    ${t.vessel != null ? `<div class="fi-vessel">${t.vessel ? `${shipLink(t.vessel, t.mmsi)} ` : ""}<span class="muted">MMSI ${esc(t.mmsi)}</span> ${n.mmsi ? fleetChips(n.mmsi) : ""}</div>` : ""}
    ${t.zone ? `<div class="fi-zone">${esc(t.zone)}</div>` : ""}
    <ul class="fi-lines">${detailLines(first).map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
    ${n.mmsi ? `<button class="btn ghost ev-open" type="button">Открыть судно</button>` : ""}${popActs(n.starred)}</div>`;
  const m = L.marker(at, {
    icon: L.divIcon({ className: "event-spot", iconSize: [34, 34], iconAnchor: [17, 17], html: `<span style="--c:${color}"></span>` }),
    zIndexOffset: 3000, keyboard: false,
  }).bindPopup(html, { minWidth: 260, maxWidth: 320, className: "ev-popup", offset: [0, -10] }).addTo(eventSpot);
  m.on("popupopen", (e) => {
    const b = e.popup.getElement()?.querySelector(".ev-open");
    if (b) b.onclick = () => { eventSpot.clearLayers(); selectVessel(n.mmsi, true); };
    bindPopActs(e.popup, { type: "notif", n }, () => {
      const btn = e.popup.getElement()?.querySelector("[data-pop=star]");
      if (btn) { btn.textContent = n.starred ? "★ Важное" : "☆ Важное"; btn.classList.toggle("on", !!n.starred); }
    });
  });
  m.on("popupclose", () => { eventSpot.clearLayers(); if (n.zone_id && state.selectedZone === n.zone_id) selectZone(null); });
  if (n.zone_id) selectZone(n.zone_id);
  if (opts.keepPanel && shipPanel.isOpen()) {        // карточка открыта — точка в свободной части карты
    smoothFit([at, at], { ...shipPanel.padding(), maxZoom: Math.max(map.getZoom(), 7) });
    setTimeout(() => m.openPopup(), 1200);
  } else {
    smoothTo(at, Math.max(map.getZoom(), 7));
    setTimeout(() => m.openPopup(), 1200);
  }
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
  if (st.server_time) state.clockSkew = st.server_time * 1000 - Date.now();   // часы сервера, а не компьютера пользователя
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

// Обновление раз в 10 с. Пока предыдущее не закончилось (медленная сеть) — новое не начинаем,
// в свёрнутой или фоновой вкладке не опрашиваем вовсе, при возврате на вкладку — обновляем сразу.
let refreshing = false;
async function refreshAll(forceZones = false) {
  if (refreshing) return;
  refreshing = true;
  try {
    // статус нужен, чтобы понять, изменились ли зоны; остальное грузим параллельно, а не по очереди
    await loadStatus();
    await Promise.all([loadZones(forceZones), loadFleets(), loadNotifications(), loadIncidents().catch(() => {})]);
    if ($("#panel-log").classList.contains("active")) loadZoneLog().catch(() => {});   // [журнал-заходов]
    await loadTraffic();                         // после флотов: суда флотов рисуются отдельно
    if (!$("#circ-modal").hidden) { await loadCirculars(); await loadIbf(); }
  } catch (e) { if (e.message !== "unauthorized") console.warn(e); }
  finally { refreshing = false; }
}

/* ------------------------------------------------------------------ журнал заходов в зоны [журнал-заходов] начало */
function logQuery() {
  const q = new URLSearchParams({ days: $("#log-days").value, layer: $("#log-layer").value });
  if ($("#log-fleet").value) q.set("fleet", $("#log-fleet").value);
  return q;
}

// Журнал пребываний: входы и выходы одного судна в одной зоне с перерывом меньше суток — одно пребывание
async function loadZoneLog() {
  const sel = $("#log-fleet"), cur = sel.value;
  sel.innerHTML = `<option value="">Все флоты</option>` + state.fleets.map((f) => `<option value="${f.id}">${esc(f.name)}</option>`).join("");
  sel.value = cur;
  const q = logQuery();
  const d = await api(`/api/zone-visits?${q}`);
  $("#log-xlsx").href = `/api/zone-visits?${q}&format=xlsx`;
  const items = d.items;
  $("#log-summary").innerHTML = items.length ? `Пребываний: <b>${items.length}</b> · сейчас в зоне: <b>${items.filter((v) => v.open).length}</b>
    · судов: <b>${new Set(items.map((v) => v.mmsi)).size}</b>` : "";
  const fmtH = (h) => h < 48 ? `${Math.round(h)} ч` : `${(h / 24).toFixed(1).replace(".", ",")} сут`;
  $("#log-list").innerHTML = items.map((v) => `
    <li class="log-item ${v.open ? "open" : ""}">
      <div class="log-top"><span>${shipLink(v.vessel.replace(/ \(MMSI \d+\)$/, ""), v.mmsi)} <span class="muted small">MMSI ${esc(v.mmsi)}</span></span>${v.open ? `<span class="log-now">в зоне сейчас</span>` : ""}</div>
      <div class="small">${esc(v.zone_name)} <span class="muted">· ${esc((v.layer || "").toUpperCase())}</span></div>
      <div class="small muted">${fmtTime(v.entered_at)}${v.initial ? " (уже было в зоне)" : ""} → ${v.open ? "сейчас" : fmtTime(v.exited_at)}
        · <b>${fmtH(v.span_h)}</b>${v.days ? ` · суток: ${v.days}` : ""}</div>
      ${v.crossings > 1 ? `<div class="small muted">пересекало границу ${v.crossings} раз, фактически в зоне ${fmtH(v.inside_h)}</div>` : ""}
    </li>`).join("") || `<li class="empty muted">За выбранный период пребываний в зонах этого слоя нет.</li>`;
}
["#log-fleet", "#log-layer", "#log-days"].forEach((id) => $(id).onchange = () => loadZoneLog().catch(() => {}));
/* [журнал-заходов] конец */

/* ------------------------------------------------------------------ события UI */
function switchTab(name) {
  if (isPhone() && sidebarSheet.get() === "min") sidebarSheet.set("half");   // свёрнутая панель — раскрываем
  if (!isPhone() && document.body.classList.contains("sb-collapsed")) setSidebarCollapsed(false);
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === (name === "log" ? "fleet" : name)));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === `panel-${name}`));
  if (name === "rules") loadRules().catch((e) => toast("Ошибка", e.message, "critical"));
  if (name === "log") loadZoneLog().catch((e) => toast("Ошибка", e.message, "critical"));   // [журнал-заходов]
}
$$(".tab").forEach((t) => t.onclick = () => switchTab(t.dataset.tab));
$("#btn-reports").onclick = () => switchTab("log");          // журнал и отчёты — из «Флота»
$("#btn-log-back").onclick = () => switchTab("fleet");

$("#events-days").onchange = (e) => { $("#events-xlsx").href = `/api/notifications/export?days=${e.target.value}`; };
$("#btn-bell").onclick = () => { switchTab("events"); $("#panel-events [data-feed=mine]").click(); };

// Источники зон (циркуляры JWC, IBF, INL) — окно поверх карты, открывается из шапки
function openCirculars() {
  $("#circ-modal").hidden = false;
  $("#circ-close").focus();
  loadCirculars().catch((e) => toast("Ошибка", e.message, "critical"));
  loadIbf().catch((e) => toast("Ошибка", e.message, "critical"));
}
function closeCirculars() { $("#circ-modal").hidden = true; $("#chip-circular").focus(); }
$("#chip-circular").onclick = openCirculars;
$("#circ-close").onclick = closeCirculars;
$("#circ-modal").onclick = (e) => { if (e.target.id === "circ-modal") closeCirculars(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#circ-modal").hidden) closeCirculars(); });
$("#legend-toggle").onclick = () => {
  const collapsed = $("#legend").classList.toggle("collapsed");
  localStorageSet("legendCollapsed", collapsed ? "1" : "0");
};
$("#z-search").oninput = (e) => { state.zoneFilter.q = e.target.value; applyZoneFilter(); };
$("#n-unread-only").onchange = loadNotifications;
$("#feed-new").onclick = () => {
  const cb = $("#n-unread-only");
  cb.checked = !cb.checked;
  $("#feed-new").setAttribute("aria-pressed", String(cb.checked));
  $("#feed-new").classList.toggle("on", cb.checked);
  loadNotifications().catch(() => {});
};
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
// «Добавить судно» и «Поделиться» — кнопки в одну строку, под ними раскрывается нужная панель
function togglePanel(sel, open) {
  const el = $(sel);
  el.open = open ?? !el.open;
  el.hidden = !el.open;
  const btn = { "#add-vessel": "#btn-add-toggle", "#fleet-share": "#btn-share-toggle" }[sel];
  $(btn).classList.toggle("on", el.open);
}
$("#btn-add-toggle").onclick = () => { togglePanel("#fleet-share", false); togglePanel("#add-vessel"); };
$("#btn-share-toggle").onclick = () => { togglePanel("#add-vessel", false); togglePanel("#fleet-share"); };

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
  if (!fd.mmsi.trim() && !fd.imo.trim()) { toast("Укажите MMSI или IMO", "", "warning"); return; }
  try {
    const r = await api(`/api/fleets/${f.id}/vessels`, { json: fd });
    e.target.reset();
    $("#mmsi-check").innerHTML = "";
    const c = r.check || {};
    if (r.hin_saved) { toast("HIN сохранён", `Судно уже было во флоте — дописан номер корпуса ${fd.hin}`); await loadFleets(); return; }
    if (c.found) {
      toast(`Добавлено: ${c.name || fd.mmsi || "IMO " + fd.imo}${c.by_imo ? ` (MMSI ${c.mmsi} найден по IMO)` : ""}`, [c.type_name, c.flag, c.has_position ? `позиция ${fmtAge(c.age_s)}` : "позиции пока нет",
        ...c.warnings].filter(Boolean).join(" · "), c.warnings.length ? "warning" : "info");
    } else {
      toast(c.by_imo ? "Судно добавлено, ждём его MMSI" : "Судно добавлено, но в потоке AIS его нет", c.warnings.join(" "), "warning");
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
  if (mmsi && mmsi.length !== 9) { box.innerHTML = `<span class="muted">MMSI — 9 цифр (сейчас ${mmsi.length})</span>`; return; }
  if (!mmsi && imo.length !== 7) { box.innerHTML = imo ? `<span class="muted">IMO — 7 цифр (сейчас ${imo.length})</span>` : ""; return; }
  checkTimer = setTimeout(async () => {
    try {
      const c = await api(`/api/ships/check?mmsi=${mmsi}&imo=${imo}`);
      const head = c.found
        ? `<div class="ok">✓ ${flagImg(c.flag_iso, c.flag)} <b>${esc(c.name || "без названия")}</b>${c.by_imo ? ` · MMSI ${esc(c.mmsi)}` : ""} · ${esc(c.type_name)}${c.length ? ` · ${Math.round(c.length)} м` : ""} · ${c.has_position ? `позиция ${fmtAge(c.age_s)}` : "позиции пока нет"}</div>`
        : `<div class="warn">Не найдено в потоке AIS${c.flag ? ` (флаг по номеру: ${esc(c.flag)})` : ""}</div>`;
      box.innerHTML = head + c.warnings.filter((w) => c.found || c.by_imo || !w.startsWith("Судна с таким")).map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("")
        + (c.found || c.by_imo ? "" : `<div class="muted">Возможна опечатка, или судно давно вне зоны покрытия. Добавить всё равно можно — позиция появится, когда оно выйдет на связь.</div>`);
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
    if (r.skipped_count) body += `. Пропущено без MMSI и IMO: ${r.skipped_count} (${r.skipped_no_mmsi.slice(0, 5).join(", ")}${r.skipped_count > 5 ? "…" : ""})`;
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

$("#btn-read-all").onclick = async () => {
  await api("/api/notifications/read", { json: { all: true } });
  // и происшествия в мире, которые сейчас в ленте, — их метки уйдут с карты (вернуть: «Просмотренные» в легенде)
  const fresh = (state.inc?.items || []).filter((it) => !it.read && incVisible(it));
  if (fresh.length) {
    await api("/api/feed/mark", { json: { items: fresh.map((it) => `i:${it.id}`), read: true } });
    fresh.forEach((it) => { it.read = true; });
    renderIncidents();
    renderLegend();
  }
  await loadNotifications();
};

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
  loadRules().catch(() => {});
  const vs = allVessels().filter((v) => v.position);
  if (vs.length) map.fitBounds(L.latLngBounds(vs.map((v) => [v.position.lat, v.position.lon])).pad(0.5), { maxZoom: 6 });
  setInterval(() => { if (!document.hidden) refreshAll(false); }, 10000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshAll(false); });
})();


/* ------------------------------------------------------------------ обучение */
// Короткий тур по интерфейсу: затемнение, подсвеченная область и карточка в 1–2 предложения.
const TOUR = [
  { el: "#ship-q", title: "Поиск судна",
    text: "Ищите по названию, MMSI, IMO или HIN (номер корпуса яхты). Находятся и суда вне зоны покрытия AIS — с последней известной позицией, "
      + "и суда из санкционных списков. Из карточки судно можно сразу добавить во флот.",
    ex: "клиент прислал IMO 9301847 — вбейте номер, откройте карточку и проверьте, нет ли санкций и где судно было последний раз." },
  { el: "#map", title: "Карта и зоны риска",
    text: "Цветные области — зоны риска: JWC (военные риски для страховки судна), IBF (риски для экипажа) и INL (навигационные ограничения и льды). "
      + "Нажмите на зону или судно, чтобы открыть подробности. Значки с буквами — происшествия: аварийные сигналы, задержания, новости.",
    ex: "судно идёт в Красное море — нажмите на зону у Йемена — увидите, что это за зона и из какого она списка." },
  { el: "#legend", title: "Легенда",
    text: "Расшифровка всех цветов и значков; нажатие на строку скрывает или показывает слой. Цвет стрелки судна — цвет его флота, "
      + "точка у кормы — где оно относительно зон: зелёная — вне зоны риска, жёлтая — приближается, красная — в зоне.",
    ex: "чтобы видеть только суда в опасности, скройте строки «Вне зоны риска» и «Приближается»." },
  { tab: "fleet", title: "Флот",
    text: "Суда, за которыми вы следите. Добавляйте по одному (MMSI или IMO) или загрузите список из Excel или CSV. "
      + "Флотов может быть несколько, у каждого свой цвет; флотом можно поделиться с коллегой.",
    ex: "заведите флот на каждого клиента-судовладельца и загрузите его список судов одним файлом." },
  { tab: "events", title: "Лента",
    text: "Всё, что произошло: «Мои суда» — сработали ваши уведомления, «В мире» — происшествия с любыми судами. "
      + "Новое — светлое, прочитанное — тёмное. Значки справа: только новые, ★ важное, корзина (удалённое за 7 дней), прочитать всё.",
    ex: "утром нажмите «Мои суда» и значок «только новые» — это всё, что случилось за ночь." },
  { tab: "rules", title: "Настройка уведомлений",
    text: "Шестерёнка — о чём сообщать: вход в зону риска, приближение к ней, заход в порт, смена пункта назначения, пропажа AIS, санкции, задержание. "
      + "Уведомление включается для всех судов, одного флота или одного судна. Не знаете, с чего начать, — выберите готовый набор.",
    ex: "уведомление «Подход к зоне» за 50 миль предупредит заранее, и клиент успеет заявить заход страховщику." },
  { tab: "log", title: "Отчёты",
    text: "Открываются кнопкой «Отчёты» во вкладке «Флот». Журнал событий и сколько каждое судно пробыло в каждой зоне риска: "
      + "когда вошло, когда вышло, сколько суток. Фильтры по флоту, слою зон и периоду, выгрузка в Excel.",
    ex: "в конце месяца выгрузите пребывания по флоту клиента — это основа для расчёта дополнительной премии за военные риски." },
  { el: "#chip-circular", title: "Источники зон",
    text: "Номер и дата действующего циркуляра JWC (Listed Areas). Нажмите, чтобы увидеть историю версий и что в них изменилось, а также списки IBF и INL. "
      + "Сайт сам проверяет обновления и пришлёт уведомление о новом циркуляре.",
    ex: "вышел новый циркуляр — откройте его и посмотрите, какие зоны добавили или расширили." },
];
const tour = { i: 0, box: null };

function tourTarget(step) {
  // свёрнутую легенду на её шаге раскрываем, потом возвращаем как было
  const lg = $("#legend");
  if (step.el === "#legend" && lg.classList.contains("collapsed")) { lg.classList.remove("collapsed"); tour.legendOpened = true; }
  else if (step.el !== "#legend" && tour.legendOpened) { lg.classList.add("collapsed"); tour.legendOpened = false; }
  if (step.tab) { switchTab(step.tab); return $("#sidebar"); }
  return $(step.el);
}

function tourShow() {
  const step = TOUR[tour.i];
  const target = tourTarget(step);
  if (!target || !target.getClientRects().length) {     // элемента нет на этом экране (на телефоне скрыт) — шаг пропускаем
    if (tour.i < TOUR.length - 1) tourGo(1); else tourEnd();
    return;
  }
  if (!tour.box) {
    tour.box = document.createElement("div");
    tour.box.className = "tour";
    tour.box.innerHTML = `<div class="tour-spot"></div><div class="tour-card" role="dialog" aria-live="polite"></div>`;
    tour.box.addEventListener("click", (e) => {
      const b = e.target.closest("[data-tour]");
      if (b) tourGo(b.dataset.tour === "next" ? 1 : b.dataset.tour === "back" ? -1 : 0);
      else if (e.target === tour.box) tourEnd();
    });
    document.body.appendChild(tour.box);
  }
  const last = tour.i === TOUR.length - 1;
  tour.box.querySelector(".tour-card").innerHTML = `
    <div class="tour-step">${tour.i + 1} из ${TOUR.length}</div>
    <button class="tour-x" type="button" data-tour="end" aria-label="Закрыть">✕</button>
    <h4>${esc(step.title)}</h4><p>${esc(step.text)}</p>
    ${step.ex ? `<p class="tour-ex"><b>Например:</b> ${esc(step.ex)}</p>` : ""}
    <div class="tour-nav">${tour.i ? `<button class="btn ghost" type="button" data-tour="back">Назад</button>` : `<span></span>`}
      <button class="btn" type="button" data-tour="${last ? "end" : "next"}">${last ? "Готово" : "Далее"}</button></div>`;
  tourPlace(target);
  tour.box.querySelector(".tour-card .btn:last-child").focus();
}

// подсветка по размеру элемента, карточка — рядом, где больше места
function tourPlace(target) {
  const r = target.getBoundingClientRect(), pad = 6, vw = innerWidth, vh = innerHeight;
  const spot = tour.box.querySelector(".tour-spot"), card = tour.box.querySelector(".tour-card");
  Object.assign(spot.style, { left: `${r.left - pad}px`, top: `${r.top - pad}px`, width: `${r.width + pad * 2}px`, height: `${r.height + pad * 2}px` });
  card.style.width = `${Math.min(380, vw - 32)}px`;
  const cw = Math.min(380, vw - 32), ch = card.offsetHeight, gap = 14;
  let x, y;
  if (r.width > vw * 0.6 && r.height > vh * 0.6) { x = (vw - cw) / 2; y = vh - ch - 40; }   // большая область — карточка внизу по центру
  else if (vh - r.bottom > ch + gap) { x = r.left; y = r.bottom + gap; }
  else if (vw - r.right > cw + gap) { x = r.right + gap; y = r.top; }
  else if (r.left > cw + gap) { x = r.left - cw - gap; y = r.top; }
  else { x = r.left; y = r.top - ch - gap; }
  card.style.width = `${cw}px`;
  card.style.left = `${Math.max(16, Math.min(x, vw - cw - 16))}px`;
  card.style.top = `${Math.max(16, Math.min(y, vh - ch - 16))}px`;
}

function tourGo(d) {
  if (!d) { tourEnd(); return; }
  tour.i = Math.max(0, Math.min(TOUR.length - 1, tour.i + d));
  tourShow();
}
function tourEnd() {
  if (tour.legendOpened) { $("#legend").classList.add("collapsed"); tour.legendOpened = false; }
  tour.box?.remove();
  tour.box = null;
  switchTab("fleet");
}
$("#btn-tour").onclick = () => { tour.i = 0; shipPanel.close(); tourShow(); };
document.addEventListener("keydown", (e) => {
  if (!tour.box) return;
  if (e.key === "Escape") tourEnd();
  else if (e.key === "ArrowRight") tourGo(1);
  else if (e.key === "ArrowLeft" && tour.i) tourGo(-1);
});
addEventListener("resize", () => { if (tour.box) tourPlace(tourTarget(TOUR[tour.i])); });

/* ------------------------------------------------------------------ часы сервера в шапке */
// Панель хранит и считает всё время в UTC; в ленте и карточках оно показано по часовому поясу браузера
state.clockSkew = 0;
function tickClock() {
  const now = new Date(Date.now() + state.clockSkew);
  const utc = now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  const off = -now.getTimezoneOffset() / 60;
  const local = off ? ` · у вас ${now.toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" })}` : "";
  const el = $("#chip-clock");
  el.textContent = `${utc} UTC${local}`;
  el.title = `Время сервера: ${utc} UTC. Панель хранит и сравнивает всё время в UTC.`
    + (off ? ` В ленте, карточках и журнале время показано по вашему часовому поясу (UTC${off > 0 ? "+" : ""}${off}).` : "");
}
tickClock();
setInterval(tickClock, 15000);

loadTrash().then(updateTrashCount).catch(() => {});

/* ------------------------------------------------------------------ компьютер: свернуть левую панель */
function setSidebarCollapsed(on) {
  document.body.classList.toggle("sb-collapsed", on);
  $("#sb-toggle").setAttribute("aria-expanded", String(!on));
  localStorageSet("sidebarCollapsed", on ? "1" : "0");
  setTimeout(() => map.invalidateSize(), 230);
}
$("#sb-toggle").onclick = () => setSidebarCollapsed(!document.body.classList.contains("sb-collapsed"));
$("#sb-rail").addEventListener("click", (e) => {        // значок на свёрнутой панели — развернуть сразу на этой вкладке
  const b = e.target.closest("[data-open]");
  if (!b) return;
  setSidebarCollapsed(false);
  if (b.dataset.open) switchTab(b.dataset.open);
});
if (localStorageGet("sidebarCollapsed") === "1") setSidebarCollapsed(true);

/* ------------------------------------------------------------------ тема: светлая / тёмная */
function setTheme(t, save = true) {
  document.documentElement.dataset.theme = t;
  if (save) localStorageSet("theme", t);
  baseLayer.setUrl(baseUrl());
  zonesLayer.setStyle(zoneStyle);
  territoryLayer.setStyle(territoryStyle);
}
$("#btn-theme").onclick = () => setTheme(isLight() ? "dark" : "light");
// пользователь тему не выбирал — следуем за системой (например, ночной режим по расписанию)
matchMedia("(prefers-color-scheme: light)").addEventListener?.("change", (e) => {
  if (!["light", "dark"].includes(localStorageGet("theme"))) setTheme(e.matches ? "light" : "dark", false);
});
