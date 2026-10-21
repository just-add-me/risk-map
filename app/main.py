"""Веб-приложение: страница с картой + JSON API + фоновые задачи.

Запуск локально:  uvicorn app.main:app --reload
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import time
from collections import defaultdict

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import alerts, config, db, fleet_import, zones
from .ais import ingest, photos, shipinfo, traffic
from .circular import pipeline
from .layers import ibf, inl, ports

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("app")


def _secret_key() -> str:
    if config.SECRET_KEY:
        return config.SECRET_KEY
    f = config.DATA_DIR / "secret.key"
    if not f.exists():
        f.write_text(secrets.token_urlsafe(48))
        f.chmod(0o600)
    return f.read_text().strip()


# --------------------------------------------------------------------------- auth
_failed: dict[str, list[float]] = defaultdict(list)


def _authed(request: Request) -> bool:
    return request.session.get("user") == config.ADMIN_USER


def api(handler):
    async def wrapper(request: Request):
        if not _authed(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            return await handler(request)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            log.exception("Ошибка API %s", request.url.path)
            return JSONResponse({"error": f"Внутренняя ошибка: {exc}"}, status_code=500)
    return wrapper


LOGIN_HTML = (config.STATIC_DIR / "login.html")


async def login_page(request: Request):
    if request.method == "GET":
        return HTMLResponse(LOGIN_HTML.read_text(encoding="utf-8").replace("{{error}}", ""))
    ip = request.client.host if request.client else "?"
    now = time.time()
    _failed[ip] = [t for t in _failed[ip] if now - t < 900]
    if len(_failed[ip]) >= 10:
        return HTMLResponse(LOGIN_HTML.read_text(encoding="utf-8").replace(
            "{{error}}", "Слишком много попыток. Подождите 15 минут."), status_code=429)
    form = await request.form()
    user, pwd = str(form.get("username", "")), str(form.get("password", ""))
    ok = bool(config.ADMIN_PASSWORD) and hmac.compare_digest(user, config.ADMIN_USER) and \
        hmac.compare_digest(pwd, config.ADMIN_PASSWORD)
    if not ok:
        _failed[ip].append(now)
        await asyncio.sleep(1.0)
        msg = "Неверный логин или пароль" if config.ADMIN_PASSWORD else "ADMIN_PASSWORD не задан в .env"
        return HTMLResponse(LOGIN_HTML.read_text(encoding="utf-8").replace("{{error}}", msg), status_code=401)
    request.session["user"] = user
    return RedirectResponse("/", status_code=303)


async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


async def index(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(config.STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


# --------------------------------------------------------------------------- API: статус и зоны
@api
async def api_status(request: Request):
    from .ais import aisstream
    active = db.query_one("SELECT ref, issued, activated_at, source_url FROM circulars WHERE status='active'")
    unread = db.query_one("SELECT COUNT(*) AS n FROM notifications WHERE is_read=0")
    return JSONResponse({
        "ais": {"mode": config.AIS_MODE, "traffic": traffic.stats(), **({k: v for k, v in aisstream.status.items()}
                                            if config.AIS_MODE == "aisstream" else {})},
        "circular": {"active": active, "check": {**pipeline.state,
                                                 "last_check": db.kv_get("circular_last_check"),
                                                 "last_seen": db.kv_get("circular_last_seen")},
                     "interval_hours": config.CIRCULAR_CHECK_HOURS},
        "zones": {"ref": zones.active_ref(), "count": len(zones.index().zones), "version": zones.version()},
        "ibf": {k: v for k, v in ibf.status().items() if k != "log"},
        "unread": unread["n"] if unread else 0,
        "warn_distance_nm": config.WARN_DISTANCE_NM,
        "user": config.ADMIN_USER,
    })


@api
async def api_zones(request: Request):
    fc = zones.raw() or {"type": "FeatureCollection", "features": [], "properties": {}}
    return Response(json.dumps(fc, ensure_ascii=False, separators=(",", ":")),
                    media_type="application/json", headers={"Cache-Control": "no-store"})


@api
async def api_ports(request: Request):
    data = await asyncio.to_thread(ports.with_zones)
    return JSONResponse({"ports": data}, headers={"Cache-Control": "no-store"})


@api
async def api_traffic(request: Request):
    """Все суда AIS в видимой области: ?bbox=запад,юг,восток,север&zoom=6&cats=cargo,tanker."""
    q = request.query_params
    try:
        w, s_, e, n = (float(x) for x in q.get("bbox", "-180,-90,180,90").split(","))
        zoom = int(float(q.get("zoom", 20)))
        limit = min(int(q.get("limit", 5000)), 10000)
    except ValueError:
        return JSONResponse({"error": "bbox: запад,юг,восток,север"}, status_code=400)
    cats = set(q["cats"].split(",")) if "cats" in q else None
    fleet = set(ingest.tracked_mmsis())          # суда флотов рисуются отдельно, в группы не входят
    res = await asyncio.to_thread(traffic.query, w, s_, e, n, zoom, cats, limit, fleet)
    return JSONResponse({**res, "categories": shipinfo.CATEGORIES,
                         "enabled": config.AIS_MODE == "aisstream" and config.AIS_TRAFFIC},
                        headers={"Cache-Control": "no-store"})


@api
async def api_ship_search(request: Request):
    """Поиск судна: сначала среди судов флотов, потом во всём потоке AIS."""
    q = request.query_params.get("q", "").strip()
    if len(q) < 2:
        return JSONResponse({"items": []})
    like = f"%{q.upper()}%"
    fleet = db.query("SELECT DISTINCT v.mmsi FROM vessels v LEFT JOIN ais_state a ON a.mmsi=v.mmsi "
                     "WHERE v.mmsi LIKE ? OR v.imo LIKE ? OR UPPER(v.name) LIKE ? OR UPPER(a.ais_name) LIKE ? LIMIT 5",
                     (f"{q}%", f"{q}%", like, like))
    fleet_mmsi = {r["mmsi"] for r in fleet}
    items = await asyncio.to_thread(traffic.search, q, 12)
    for it in items:
        it["in_fleet"] = it["mmsi"] in fleet_mmsi
    known = {it["mmsi"] for it in items}
    for m in fleet_mmsi - known:          # судно флота, которого ещё нет в потоке
        v = db.query_one("SELECT v.mmsi, v.imo, COALESCE(v.name, a.ais_name) AS name, a.lat, a.lon, a.ship_type "
                         "FROM vessels v LEFT JOIN ais_state a ON a.mmsi=v.mmsi WHERE v.mmsi=?", (m,))
        items.insert(0, {"mmsi": v["mmsi"], "imo": v["imo"], "name": v["name"], "lat": v["lat"], "lon": v["lon"],
                         "category": shipinfo.category(v["ship_type"]), "type_name": shipinfo.type_name(v["ship_type"]),
                         "flag": shipinfo.flag(m), "flag_iso": shipinfo.flag_iso(m), "in_fleet": True})
    items.sort(key=lambda it: not it.get("in_fleet"))
    return JSONResponse({"items": items[:12]})


@api
async def api_traffic_ship(request: Request):
    mmsi = request.path_params["mmsi"]
    s = traffic.get(mmsi)
    if not s:
        return JSONResponse({"error": "Нет данных по судну"}, status_code=404)
    d = traffic.details(s)
    if d["lat"] is not None:
        idx = zones.index()
        found = idx.evaluate(d["lon"], d["lat"], config.WARN_DISTANCE_NM)
        d["zones"] = [{"id": zid, "name": zones.zone_name(zid), **r} for zid, r in found.items()
                      if zones.in_effect(zid)]
    d["fleets"] = [r["name"] for r in db.query(
        "SELECT f.name FROM vessels v JOIN fleets f ON f.id=v.fleet_id WHERE v.mmsi=?", (mmsi,))]
    return JSONResponse(d)


@api
async def api_port(request: Request):
    """Карточка порта: страна, зоны, суда рядом (в радиусе 5 миль) по категориям и статусам."""
    pid = int(request.path_params["pid"])
    plist = await asyncio.to_thread(ports.with_zones)
    if not 0 <= pid < len(plist):
        return JSONResponse({"error": "Порт не найден"}, status_code=404)
    p = plist[pid]
    ships = await asyncio.to_thread(traffic.near, p["lat"], p["lon"], 5.0)
    by_cat: dict[str, int] = {}
    for s in ships:
        by_cat[s["category"]] = by_cat.get(s["category"], 0) + 1
    status = {"moored": sum(1 for s in ships if s["nav_status"] == "Ошвартовано"),
              "anchored": sum(1 for s in ships if s["nav_status"] == "На якоре"),
              "moving": sum(1 for s in ships if (s["sog"] or 0) >= 1)}
    ships.sort(key=lambda s: -(s["length"] or 0))
    zone_info = [{"id": z, "name": zones.zone_name(z), "layer": (zones.index().get(z).props.get("layer")
                                                                if zones.index().get(z) else None)}
                 for z in p["zones"]]
    return JSONResponse({**p, "zones": zone_info, "ships_total": len(ships), "by_category": by_cat,
                         "status": status, "categories": shipinfo.CATEGORIES,
                         "ships": [{k: s[k] for k in ("mmsi", "name", "type_name", "category", "length",
                                                      "nav_status", "sog", "flag_iso", "distance_nm", "lat", "lon")}
                                   for s in ships[:12]]})


@api
async def api_traffic_track(request: Request):
    return JSONResponse({"points": traffic.trail(request.path_params["mmsi"])})


@api
async def api_ship_photo(request: Request):
    photo = await asyncio.to_thread(photos.get, request.path_params["imo"])
    return JSONResponse({"photo": photo}, headers={"Cache-Control": "private, max-age=3600"})


# --------------------------------------------------------------------------- API: флот
def _vessel_dict(v: dict) -> dict:
    st = db.query_one("SELECT * FROM ais_state WHERE mmsi=?", (v["mmsi"],)) or {}
    return {
        "id": v["id"], "fleet_id": v["fleet_id"], "mmsi": v["mmsi"], "imo": v["imo"] or st.get("imo"),
        "name": v["name"] or st.get("ais_name") or "", "ais_name": st.get("ais_name"),
        "position": ({"lat": st["lat"], "lon": st["lon"], "sog": st.get("sog"), "cog": st.get("cog"),
                      "heading": st.get("heading"), "ts": st.get("ts"), "source": st.get("source")}
                     if st.get("lat") is not None else None),
        "destination": st.get("destination"), "callsign": st.get("callsign"),
        "ship_type": st.get("ship_type"), "type_name": shipinfo.type_name(st.get("ship_type")),
        "category": shipinfo.category(st.get("ship_type")), "flag": shipinfo.flag(v["mmsi"]),
        "flag_iso": shipinfo.flag_iso(v["mmsi"]),
        "nav_status": shipinfo.nav_status((traffic.get(v["mmsi"]) or {}).get("nav")),
        "length": st.get("length"), "beam": st.get("beam"), "draught": st.get("draught"), "eta": st.get("eta"),
        "status": alerts.vessel_status(v["mmsi"]),
    }


@api
async def api_fleets(request: Request):
    fleets = db.query("SELECT * FROM fleets ORDER BY name")
    for f in fleets:
        f["vessels"] = [_vessel_dict(v) for v in db.query(
            "SELECT * FROM vessels WHERE fleet_id=? ORDER BY COALESCE(name, mmsi)", (f["id"],))]
    return JSONResponse({"fleets": fleets})


@api
async def api_fleet_create(request: Request):
    body = await request.json()
    name = str(body.get("name", "")).strip()[:80]
    if not name:
        raise ValueError("Укажите название флота")
    if db.query_one("SELECT id FROM fleets WHERE name=?", (name,)):
        raise ValueError("Флот с таким названием уже есть")
    cur = db.execute("INSERT INTO fleets(name, created_at) VALUES(?,?)", (name, db.now_iso()))
    return JSONResponse({"id": cur.lastrowid, "name": name})


@api
async def api_fleet_update(request: Request):
    fid = int(request.path_params["fid"])
    if request.method == "DELETE":
        db.execute("DELETE FROM fleets WHERE id=?", (fid,))
        ingest.cleanup_trails()
        return JSONResponse({"ok": True})
    body = await request.json()
    name = str(body.get("name", "")).strip()[:80]
    if not name:
        raise ValueError("Укажите название флота")
    db.execute("UPDATE fleets SET name=? WHERE id=?", (name, fid))
    return JSONResponse({"ok": True})


def _add_vessel(fid: int, mmsi: str, imo: str, name: str) -> str:
    if not db.query_one("SELECT id FROM fleets WHERE id=?", (fid,)):
        raise ValueError("Флот не найден")
    if not mmsi:
        raise ValueError("Нужен MMSI (9 цифр) — по нему идёт отслеживание AIS")
    if db.query_one("SELECT id FROM vessels WHERE fleet_id=? AND mmsi=?", (fid, mmsi)):
        return "exists"
    db.execute("INSERT INTO vessels(fleet_id, mmsi, imo, name, created_at) VALUES(?,?,?,?,?)",
               (fid, mmsi, imo or None, name or None, db.now_iso()))
    _backfill_from_traffic(mmsi)
    return "added"


def _backfill_from_traffic(mmsi: str) -> None:
    """Если судно уже есть в общем потоке AIS — сразу берём его данные, не ждём нового сообщения."""
    s = traffic.get(mmsi)
    if not s:
        return
    ingest.static_data(mmsi, name=s.get("name"), imo=s.get("imo"), callsign=s.get("callsign"),
                       ship_type=s.get("ship_type"), destination=s.get("destination"), length=s.get("length"),
                       beam=s.get("beam"), draught=s.get("draught"), eta=s.get("eta"))
    if s.get("lat") is not None:
        ingest.position(mmsi, s["lat"], s["lon"], sog=s.get("sog"), cog=s.get("cog"), heading=s.get("heading"),
                        source="aisstream", ais_name=s.get("name"))


def _vessel_check(mmsi: str, imo: str = "") -> dict:
    """Есть ли такое судно в потоке AIS и не похож ли номер на опечатку."""
    warnings = []
    if mmsi and mmsi[0] not in "234567":
        kinds = {"0": "береговой станции или группы судов", "1": "поисково-спасательного самолёта",
                 "8": "переносной рации", "9": "навигационного знака, аварийного буя или шлюпки"}
        warnings.append(f"MMSI на «{mmsi[0]}» — это номер {kinds.get(mmsi[0], 'не судна')}, а не судна")
    elif mmsi and not shipinfo.flag(mmsi):
        warnings.append(f"Код страны {mmsi[:3]} в начале MMSI не найден в таблице МСЭ — проверьте номер")
    if imo and not fleet_import.imo_valid(imo):
        warnings.append("IMO не проходит проверку контрольной цифры — вероятно, опечатка")
    s = traffic.get(mmsi) if mmsi else None
    if not s:
        warnings.append("Судна с таким MMSI нет в потоке AIS за последнюю неделю: возможна опечатка, "
                        "или судно давно вне зоны покрытия береговых станций. Позиция появится, когда оно выйдет на связь.")
        return {"found": False, "warnings": warnings, "flag": shipinfo.flag(mmsi) if mmsi else None}
    d = traffic.details(s)
    if imo and d.get("imo") and d["imo"] != imo:
        warnings.append(f"У судна с этим MMSI в AIS указан другой IMO: {d['imo']}")
    return {"found": True, "warnings": warnings, "name": d["name"], "type_name": d["type_name"],
            "flag": d["flag"], "flag_iso": d["flag_iso"], "imo": d["imo"], "length": d["length"],
            "has_position": d["lat"] is not None, "age_s": d["age_s"]}


@api
async def api_ship_check(request: Request):
    mmsi = fleet_import.clean_mmsi(request.query_params.get("mmsi"))
    if not mmsi:
        return JSONResponse({"error": "MMSI должен состоять из 9 цифр"}, status_code=400)
    return JSONResponse(_vessel_check(mmsi, fleet_import.clean_imo(request.query_params.get("imo"))))


@api
async def api_vessel_add(request: Request):
    fid = int(request.path_params["fid"])
    body = await request.json()
    mmsi = fleet_import.clean_mmsi(body.get("mmsi"))
    imo = fleet_import.clean_imo(body.get("imo"))
    if body.get("mmsi") and not mmsi:
        raise ValueError("MMSI должен состоять из 9 цифр")
    if body.get("imo") and not imo:
        raise ValueError("IMO должен состоять из 7 цифр")
    res = _add_vessel(fid, mmsi, imo, str(body.get("name", "")).strip()[:80])
    if res == "exists":
        raise ValueError("Это судно уже есть во флоте")
    return JSONResponse({"ok": True, "check": _vessel_check(mmsi, imo)})


@api
async def api_vessel_upload(request: Request):
    fid = int(request.path_params["fid"])
    form = await request.form()
    up = form.get("file")
    if up is None or isinstance(up, str):
        raise ValueError("Файл не получен")
    data = await up.read()
    if len(data) > 5 * 1024 * 1024:
        raise ValueError("Файл больше 5 МБ")
    items = fleet_import.parse_file(data, up.filename or "upload.csv")
    added = exists = 0
    no_mmsi, not_found = [], []
    for it in items:
        if not it["mmsi"]:
            no_mmsi.append(it["name"] or (f"IMO {it['imo']}" if it["imo"] else "?"))
            continue
        r = _add_vessel(fid, it["mmsi"], it["imo"], it["name"])
        added += r == "added"
        exists += r == "exists"
        if r == "added" and not traffic.get(it["mmsi"]):
            not_found.append(it["name"] or it["mmsi"])
    return JSONResponse({"added": added, "exists": exists, "skipped_no_mmsi": no_mmsi[:50],
                         "skipped_count": len(no_mmsi), "total": len(items),
                         "not_found": not_found[:50], "not_found_count": len(not_found)})


@api
async def api_vessel_update(request: Request):
    vid = int(request.path_params["vid"])
    if request.method == "DELETE":
        db.execute("DELETE FROM vessels WHERE id=?", (vid,))
        ingest.cleanup_trails()
        return JSONResponse({"ok": True})
    body = await request.json()
    imo = fleet_import.clean_imo(body.get("imo"))
    db.execute("UPDATE vessels SET name=?, imo=? WHERE id=?",
               (str(body.get("name", "")).strip()[:80] or None, imo or None, vid))
    return JSONResponse({"ok": True})


@api
async def api_track(request: Request):
    mmsi = request.path_params["mmsi"]
    rows = db.query("SELECT ts, lat, lon, sog FROM positions WHERE mmsi=? ORDER BY ts", (mmsi,))
    return JSONResponse({"mmsi": mmsi, "points": rows})


# --------------------------------------------------------------------------- API: уведомления
@api
async def api_notifications(request: Request):
    after = int(request.query_params.get("after_id", 0) or 0)
    limit = min(int(request.query_params.get("limit", 200) or 200), 500)
    only_unread = request.query_params.get("unread") == "1"
    sql = "SELECT * FROM notifications WHERE id > ?" + (" AND is_read=0" if only_unread else "")
    rows = db.query(sql + " ORDER BY id DESC LIMIT ?", (after, limit))
    unread = db.query_one("SELECT COUNT(*) AS n FROM notifications WHERE is_read=0")
    for r in rows:
        r["zone_name"] = zones.zone_name(r["zone_id"]) if r["zone_id"] else None
    return JSONResponse({"items": rows, "unread": unread["n"] if unread else 0})


@api
async def api_notifications_read(request: Request):
    body = await request.json()
    if body.get("all"):
        db.execute("UPDATE notifications SET is_read=1 WHERE is_read=0")
    else:
        ids = [int(i) for i in body.get("ids", [])][:500]
        if ids:
            db.execute(f"UPDATE notifications SET is_read=1 WHERE id IN ({','.join('?' * len(ids))})", ids)
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- API: циркуляры
@api
async def api_circulars(request: Request):
    rows = db.query("SELECT ref, number, issued, source_url, file_name, status, build_log, diff, created_at, "
                    "activated_at, spec FROM circulars ORDER BY number DESC")
    for r in rows:
        spec = json.loads(r.pop("spec") or "{}")
        r["diff"] = json.loads(r["diff"] or "{}")
        names = {e["id"]: e["name"] for e in spec.get("entries", [])}
        r["diff_names"] = {k: [names.get(i, i) for i in r["diff"].get(k, [])] for k in ("added", "amended")}
        r["diff_names"]["removed"] = r["diff"].get("removed_names", [])
        r["review"] = [{"name": e["name"], "reason": e.get("review_reason", "")}
                       for e in spec.get("entries", []) if e.get("needs_review")]
        r["entries"] = len(spec.get("entries", []))
    return JSONResponse({"items": rows, "state": pipeline.state})


@api
async def api_ibf(request: Request):
    return JSONResponse({**ibf.status(), "spec": db.kv_get("ibf_spec"),
                         "inl": {"version": inl.VERSION, "log": db.kv_get("inl_log")}})


@api
async def api_ibf_check(request: Request):
    res = await ibf.check_now(manual=True)
    return JSONResponse(res, status_code=200 if res.get("ok") else 502)


@api
async def api_circular_check(request: Request):
    res = await pipeline.check_now(manual=True)
    return JSONResponse(res, status_code=200 if res.get("ok") else 502)


@api
async def api_circular_upload(request: Request):
    form = await request.form()
    up = form.get("file")
    if up is None or isinstance(up, str):
        raise ValueError("Файл не получен")
    data = await up.read()
    if len(data) > 20 * 1024 * 1024:
        raise ValueError("Файл больше 20 МБ")
    res = await pipeline.ingest_document(data, up.filename or "circular.pdf", source_url="upload",
                                         force=form.get("force") == "1")
    return JSONResponse(res)


@api
async def api_circular_rebuild(request: Request):
    res = await pipeline.build_and_activate(request.path_params["ref"])
    return JSONResponse(res)


async def circular_file(request: Request):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    row = db.query_one("SELECT file_name FROM circulars WHERE ref=?", (request.path_params["ref"],))
    path = config.CIRCULARS_DIR / (row["file_name"] if row and row["file_name"] else "__none__")
    if not row or not path.is_file() or path.parent != config.CIRCULARS_DIR:
        return Response("Файл не найден", status_code=404)
    return FileResponse(path, filename=path.name.split("__", 1)[-1])


async def health(request: Request):
    return JSONResponse({"ok": True, "zones": len(zones.index().zones)})


# --------------------------------------------------------------------------- фоновые задачи
async def _circular_loop(stop: asyncio.Event):
    await pipeline.bootstrap()
    try:
        await asyncio.to_thread(inl.ensure_built)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось построить слой INL")
    await asyncio.sleep(20)
    while not stop.is_set():
        try:
            await pipeline.check_now()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка проверки циркуляров")
        try:
            await ibf.check_now()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка проверки списка IBF")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=config.CIRCULAR_CHECK_HOURS * 3600)


async def _traffic_snapshot_loop(stop: asyncio.Event):
    """Раз в 5 минут сохраняем все суда на диск; при остановке — ещё раз."""
    while not stop.is_set():
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=300)
        try:
            await asyncio.to_thread(traffic.save)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить снимок судов")


async def _maintenance_loop(stop: asyncio.Event):
    while not stop.is_set():
        try:
            ingest.cleanup_trails()
            alerts.check_stale()
        except Exception:  # noqa: BLE001
            log.exception("Ошибка обслуживания")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=600)


@contextlib.asynccontextmanager
async def lifespan(app):
    db.connect()
    zones.load_active()
    if not config.ADMIN_PASSWORD:
        log.warning("ADMIN_PASSWORD не задан — вход в панель невозможен. Укажите его в .env")
    stop = asyncio.Event()
    await asyncio.to_thread(traffic.load)
    if config.AIS_MODE == "aisstream":
        from .ais import aisstream as ais_source
    else:
        from .ais import simulator as ais_source
    tasks = [asyncio.create_task(ais_source.run(stop)),
             asyncio.create_task(_circular_loop(stop)),
             asyncio.create_task(_maintenance_loop(stop)),
             asyncio.create_task(_traffic_snapshot_loop(stop))]
    try:
        yield
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        try:
            n = traffic.save()
            log.info("Снимок судов сохранён при остановке: %d", n)
        except Exception:  # noqa: BLE001
            log.exception("Не удалось сохранить снимок судов")


routes = [
    Route("/", index),
    Route("/login", login_page, methods=["GET", "POST"]),
    Route("/logout", logout, methods=["POST", "GET"]),
    Route("/health", health),
    Route("/api/status", api_status),
    Route("/api/zones", api_zones),
    Route("/api/ports", api_ports),
    Route("/api/ports/{pid:int}", api_port),
    Route("/api/ships/check", api_ship_check),
    Route("/api/traffic", api_traffic),
    Route("/api/ships/search", api_ship_search),
    Route("/api/traffic/{mmsi}", api_traffic_ship),
    Route("/api/traffic/{mmsi}/track", api_traffic_track),
    Route("/api/photo/{imo}", api_ship_photo),
    Route("/api/ibf", api_ibf),
    Route("/api/ibf/check", api_ibf_check, methods=["POST"]),
    Route("/api/fleets", api_fleets),
    Route("/api/fleets/create", api_fleet_create, methods=["POST"]),
    Route("/api/fleets/{fid:int}", api_fleet_update, methods=["PATCH", "DELETE"]),
    Route("/api/fleets/{fid:int}/vessels", api_vessel_add, methods=["POST"]),
    Route("/api/fleets/{fid:int}/upload", api_vessel_upload, methods=["POST"]),
    Route("/api/vessels/{vid:int}", api_vessel_update, methods=["PATCH", "DELETE"]),
    Route("/api/track/{mmsi}", api_track),
    Route("/api/notifications", api_notifications),
    Route("/api/notifications/read", api_notifications_read, methods=["POST"]),
    Route("/api/circulars", api_circulars),
    Route("/api/circulars/check", api_circular_check, methods=["POST"]),
    Route("/api/circulars/upload", api_circular_upload, methods=["POST"]),
    Route("/api/circulars/{ref}/rebuild", api_circular_rebuild, methods=["POST"]),
    Route("/circulars/{ref}/file", circular_file),
    Mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static"),
]

app = Starlette(
    routes=routes,
    lifespan=lifespan,
    middleware=[Middleware(SessionMiddleware, secret_key=_secret_key(), session_cookie="warrisk_session",
                           max_age=14 * 24 * 3600, same_site="lax", https_only=config.COOKIE_SECURE)],
)
