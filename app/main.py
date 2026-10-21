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

from . import access, alerts, config, db, fleet_import, rules, screening, users, zones
from .ais import incidents, ingest, photos, shipinfo, traffic
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


def _user(request: Request) -> str:
    return request.session.get("user") or ""


# уведомления, которые видит пользователь: общие (без судна) и по судам его флотов
# параметры NOTIF_VISIBLE: username ×3
NOTIF_VISIBLE = (f"(CASE WHEN n.username IS NOT NULL THEN n.username = ? "
                 f"ELSE (n.mmsi IS NULL OR n.mmsi IN ({access.VISIBLE_MMSI_SQL})) END)")
NOTIF_UNREAD = "NOT EXISTS (SELECT 1 FROM notification_reads r WHERE r.username=? AND r.notification_id=n.id)"


def _unread(user: str) -> int:
    row = db.query_one(f"SELECT COUNT(*) AS n FROM notifications n WHERE {NOTIF_VISIBLE} AND {NOTIF_UNREAD}",
                       (user, user, user, user))
    return row["n"] if row else 0


def _authed(request: Request) -> bool:
    user = request.session.get("user")
    # пользователь из базы теряет доступ сразу после удаления, даже с открытой сессией
    return bool(user) and (user == config.ADMIN_USER or users.exists(user))


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
    if hmac.compare_digest(user, config.ADMIN_USER):
        ok = bool(config.ADMIN_PASSWORD) and hmac.compare_digest(pwd, config.ADMIN_PASSWORD)
    else:
        ok = await asyncio.to_thread(users.verify, user, pwd)
    if not ok:
        _failed[ip].append(now)
        await asyncio.sleep(1.0)
        msg = "Неверный логин или пароль"
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
    unread = _unread(_user(request))
    return JSONResponse({
        "ais": {"mode": config.AIS_MODE, "traffic": traffic.stats(), **({k: v for k, v in aisstream.status.items()}
                                            if config.AIS_MODE == "aisstream" else {})},
        "circular": {"active": active, "check": {**pipeline.state,
                                                 "last_check": db.kv_get("circular_last_check"),
                                                 "last_seen": db.kv_get("circular_last_seen")},
                     "interval_hours": config.CIRCULAR_CHECK_HOURS},
        "zones": {"ref": zones.active_ref(), "count": len(zones.index().zones), "version": zones.version()},
        "ibf": {k: v for k, v in ibf.status().items() if k != "log"},
        "unread": unread,
        "warn_distance_nm": config.WARN_DISTANCE_NM,
        "user": request.session.get("user"),
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
    fleet = access.mmsis(_user(request))         # суда своих флотов рисуются отдельно, в группы не входят
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
    user = _user(request)
    fleet = db.query("SELECT DISTINCT v.mmsi FROM vessels v LEFT JOIN ais_state a ON a.mmsi=v.mmsi "
                     f"WHERE v.mmsi IN ({access.VISIBLE_MMSI_SQL}) AND (v.mmsi LIKE ? OR v.imo LIKE ? "
                     "OR UPPER(v.name) LIKE ? OR UPPER(a.ais_name) LIKE ?) LIMIT 5",
                     (user, user, f"{q}%", f"{q}%", like, like))
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
    d["screening"] = screening.lookup(d["imo"])
    user = _user(request)
    d["fleets"] = [r["name"] for r in db.query(
        f"SELECT f.name FROM ({access.VISIBLE_FLEETS_SQL}) f JOIN vessels v ON v.fleet_id=f.id WHERE v.mmsi=?",
        (user, user, user, mmsi))]
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
async def api_screening(request: Request):
    if request.method == "POST":
        if screening.state["running"]:
            return JSONResponse({"ok": False, "message": "Обновление уже идёт"})
        msg = await asyncio.to_thread(screening.run_daily)
        return JSONResponse({"ok": True, "message": msg})
    return JSONResponse(screening.status())


@api
async def api_incidents(request: Request):
    days = int(request.query_params.get("days", 7) or 7)
    return JSONResponse({"items": await asyncio.to_thread(incidents.recent, days), "kinds": incidents.KINDS},
                        headers={"Cache-Control": "no-store"})


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
        "screening": screening.lookup(v["imo"] or st.get("imo")),
        "status": alerts.vessel_status(v["mmsi"]),
    }


@api
async def api_fleets(request: Request):
    user = _user(request)
    fleets = access.fleets(user)
    for f in fleets:
        f["vessels"] = [_vessel_dict(v) for v in db.query(
            "SELECT * FROM vessels WHERE fleet_id=? ORDER BY COALESCE(name, mmsi)", (f["id"],))]
        if f["access"] == "owner":
            f["shares"] = access.shares(f["id"])
    return JSONResponse({"fleets": fleets, "user": user})


@api
async def api_fleet_create(request: Request):
    body = await request.json()
    user = _user(request)
    name = str(body.get("name", "")).strip()[:80]
    if not name:
        raise ValueError("Укажите название флота")
    if db.query_one("SELECT id FROM fleets WHERE name=? AND owner=?", (name, user)):
        raise ValueError("У вас уже есть флот с таким названием")
    cur = db.execute("INSERT INTO fleets(name, owner, created_at) VALUES(?,?,?)", (name, user, db.now_iso()))
    return JSONResponse({"id": cur.lastrowid, "name": name})


@api
async def api_fleet_update(request: Request):
    fid = int(request.path_params["fid"])
    access.require(fid, _user(request), "owner")
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


@api
async def api_fleet_share(request: Request):
    """Владелец открывает или закрывает флот для другого пользователя; получатель может отказаться сам."""
    fid = int(request.path_params["fid"])
    user = _user(request)
    body = await request.json()
    target = str(body.get("username", "")).strip()
    if body.get("remove"):
        if target != user:
            access.require(fid, user, "owner")
        db.execute("DELETE FROM fleet_shares WHERE fleet_id=? AND username=?", (fid, target))
        return JSONResponse({"ok": True})
    access.require(fid, user, "owner")
    if not access.is_user(target):
        raise ValueError("Такого пользователя нет")
    if target == user:
        raise ValueError("Это ваш флот")
    db.execute("INSERT INTO fleet_shares(fleet_id, username, can_edit) VALUES(?,?,?) "
               "ON CONFLICT(fleet_id, username) DO UPDATE SET can_edit=excluded.can_edit",
               (fid, target, 1 if body.get("can_edit") else 0))
    return JSONResponse({"ok": True})


@api
async def api_users(request: Request):
    """Список пользователей — чтобы выбрать, с кем поделиться флотом."""
    me = _user(request)
    return JSONResponse({"users": [u for u in access.all_usernames() if u != me], "me": me})


def _add_vessel(fid: int, mmsi: str, imo: str, name: str) -> str:
    """Права проверяет вызывающий (access.require(..., "edit")).

    Без MMSI ищем судно по IMO в потоке AIS; не нашли — добавляем с временным номером IMO:…,
    настоящий MMSI подставится, когда судно передаст паспортные данные (см. ingest.resolve_imo).
    """
    if not db.query_one("SELECT id FROM fleets WHERE id=?", (fid,)):
        raise ValueError("Флот не найден")
    if not mmsi and not imo:
        raise ValueError("Укажите MMSI или IMO")
    if not mmsi:
        mmsi = traffic.find_imo(imo) or ingest.PENDING + imo
    if db.query_one("SELECT id FROM vessels WHERE fleet_id=? AND (mmsi=? OR (? <> '' AND imo=?))",
                    (fid, mmsi, imo, imo)):
        return "exists"
    db.execute("INSERT INTO vessels(fleet_id, mmsi, imo, name, created_at) VALUES(?,?,?,?,?)",
               (fid, mmsi, imo or None, name or None, db.now_iso()))
    if not mmsi.startswith(ingest.PENDING):
        ingest.backfill_from_traffic(mmsi)
    return "added"


def _vessel_check(mmsi: str, imo: str = "") -> dict:
    """Есть ли такое судно в потоке AIS и не похож ли номер на опечатку."""
    if not mmsi and imo:
        if not fleet_import.imo_valid(imo):
            return {"found": False, "by_imo": True, "flag": None,
                    "warnings": ["IMO не проходит проверку контрольной цифры — вероятно, опечатка"]}
        found = traffic.find_imo(imo)
        if not found:
            return {"found": False, "by_imo": True, "flag": None,
                    "warnings": ["Судна с таким IMO пока нет в потоке AIS. Добавить можно: MMSI подставится "
                                 "сам, когда судно передаст паспортные данные (обычно раз в несколько минут, "
                                 "если оно в зоне береговых станций)."]}
        return {**_vessel_check(found, imo), "mmsi": found, "by_imo": True}
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
    sc = screening.lookup(imo or d.get("imo"))
    if sc["sanctions"]:
        warnings.append("Судно в санкционных списках: " + ", ".join(x["label"] for x in sc["sanctions"]))
    return {"found": True, "warnings": warnings, "name": d["name"], "type_name": d["type_name"],
            "flag": d["flag"], "flag_iso": d["flag_iso"], "imo": d["imo"], "length": d["length"],
            "has_position": d["lat"] is not None, "age_s": d["age_s"]}


@api
async def api_ship_check(request: Request):
    mmsi = fleet_import.clean_mmsi(request.query_params.get("mmsi"))
    imo = fleet_import.clean_imo(request.query_params.get("imo"))
    if not mmsi and not imo:
        return JSONResponse({"error": "Нужен MMSI (9 цифр) или IMO (7 цифр)"}, status_code=400)
    return JSONResponse(_vessel_check(mmsi, imo))


@api
async def api_vessel_add(request: Request):
    fid = int(request.path_params["fid"])
    access.require(fid, _user(request), "edit")
    body = await request.json()
    mmsi = fleet_import.clean_mmsi(body.get("mmsi"))
    imo = fleet_import.clean_imo(body.get("imo"))
    if body.get("mmsi") and not mmsi:
        raise ValueError("MMSI должен состоять из 9 цифр")
    if body.get("imo") and not imo:
        raise ValueError("IMO должен состоять из 7 цифр")
    if not mmsi and imo and not fleet_import.imo_valid(imo):
        raise ValueError("IMO не проходит проверку контрольной цифры — проверьте номер")
    res = _add_vessel(fid, mmsi, imo, str(body.get("name", "")).strip()[:80])
    if res == "exists":
        raise ValueError("Это судно уже есть во флоте")
    await asyncio.to_thread(screening.screen_fleets)
    return JSONResponse({"ok": True, "check": _vessel_check(mmsi, imo)})


@api
async def api_vessel_upload(request: Request):
    fid = int(request.path_params["fid"])
    access.require(fid, _user(request), "edit")
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
        if not it["mmsi"] and not it["imo"]:
            no_mmsi.append(it["name"] or "?")
            continue
        r = _add_vessel(fid, it["mmsi"], it["imo"], it["name"])
        added += r == "added"
        exists += r == "exists"
        found = traffic.get(it["mmsi"]) if it["mmsi"] else traffic.find_imo(it["imo"])
        if r == "added" and not found:
            not_found.append(it["name"] or it["mmsi"] or f"IMO {it['imo']}")
    await asyncio.to_thread(screening.screen_fleets)
    return JSONResponse({"added": added, "exists": exists, "skipped_no_mmsi": no_mmsi[:50],
                         "skipped_count": len(no_mmsi), "total": len(items),
                         "not_found": not_found[:50], "not_found_count": len(not_found)})


@api
async def api_vessel_update(request: Request):
    vid = int(request.path_params["vid"])
    row = db.query_one("SELECT fleet_id FROM vessels WHERE id=?", (vid,))
    if not row:
        raise ValueError("Судно не найдено")
    access.require(row["fleet_id"], _user(request), "edit")
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


# --------------------------------------------------------------------------- API: правила и области
def _rule_target_name(r: dict) -> str:
    if r["target_type"] == "fleet":
        f = db.query_one("SELECT name FROM fleets WHERE id=?", (int(r["target"]),))
        return f"флот «{f['name']}»" if f else "удалённый флот"
    if r["target_type"] == "vessel":
        return alerts.vessel_label(r["target"])
    return "все мои флоты"


def _rule_body(body: dict, user: str) -> dict:
    kind = str(body.get("kind", ""))
    params = rules.validate(kind, body.get("params") or {}, user)
    ttype = body.get("target_type") if body.get("target_type") in ("all", "fleet", "vessel") else "all"
    target = str(body.get("target") or "")
    if ttype == "fleet" and not access.level(int(target or 0), user):
        raise ValueError("Флот не найден")
    if ttype == "vessel" and target not in access.mmsis(user):
        raise ValueError("Судно не найдено в ваших флотах")
    name = str(body.get("name") or "").strip()[:100] or rules.KINDS[kind]
    return {"kind": kind, "params": json.dumps(params, ensure_ascii=False), "target_type": ttype,
            "target": target if ttype != "all" else None, "name": name,
            "audience": "fleet" if body.get("audience") == "fleet" else "me",
            "severity": body.get("severity") if body.get("severity") in ("critical", "warning", "info") else "warning"}


@api
async def api_rules(request: Request):
    user = _user(request)
    if request.method == "POST":
        r = _rule_body(await request.json(), user)
        cur = db.execute("INSERT INTO rules(owner, name, enabled, kind, target_type, target, params, audience, severity, "
                         "created_at) VALUES(?,?,1,?,?,?,?,?,?,?)",
                         (user, r["name"], r["kind"], r["target_type"], r["target"], r["params"], r["audience"],
                          r["severity"], db.now_iso()))
        return JSONResponse({"ok": True, "id": cur.lastrowid})
    from .ais import incidents as inc
    items = []
    for row in db.query("SELECT * FROM rules WHERE owner=? ORDER BY id DESC", (user,)):
        r = {**row, "params": json.loads(row["params"])}
        items.append({**r, "description": rules.describe(r), "target_name": _rule_target_name(r)})
    return JSONResponse({"items": items, "kinds": rules.KINDS, "nav": rules.NAV_NAMES,
                         "incident_kinds": {k: v["label"] for k, v in inc.KINDS.items()}})


@api
async def api_rule(request: Request):
    user = _user(request)
    rid = int(request.path_params["rid"])
    if not db.query_one("SELECT id FROM rules WHERE id=? AND owner=?", (rid, user)):
        raise ValueError("Правило не найдено")
    if request.method == "DELETE":
        db.execute("DELETE FROM rules WHERE id=?", (rid,))
        return JSONResponse({"ok": True})
    body = await request.json()
    if set(body) == {"enabled"}:
        db.execute("UPDATE rules SET enabled=? WHERE id=?", (1 if body["enabled"] else 0, rid))
        return JSONResponse({"ok": True})
    r = _rule_body(body, user)
    db.execute("UPDATE rules SET name=?, kind=?, target_type=?, target=?, params=?, audience=?, severity=? WHERE id=?",
               (r["name"], r["kind"], r["target_type"], r["target"], r["params"], r["audience"], r["severity"], rid))
    db.execute("DELETE FROM rule_state WHERE rule_id=?", (rid,))      # условие поменялось — наблюдаем заново
    return JSONResponse({"ok": True})


def _check_polygon(geom: dict) -> dict:
    ring = (geom or {}).get("coordinates", [[]])[0] if (geom or {}).get("type") == "Polygon" else []
    pts = [(float(x), float(y)) for x, y in ring]
    if len(pts) < 4 or pts[0] != pts[-1]:
        raise ValueError("Область — замкнутый многоугольник минимум из 3 точек")
    if len(pts) > 500 or not all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in pts):
        raise ValueError("Некорректные координаты области")
    return {"type": "Polygon", "coordinates": [[[round(x, 5), round(y, 5)] for x, y in pts]]}


@api
async def api_areas(request: Request):
    user = _user(request)
    if request.method == "POST":
        body = await request.json()
        name = str(body.get("name") or "").strip()[:80] or "Моя область"
        geom = _check_polygon(body.get("geometry"))
        cur = db.execute("INSERT INTO areas(owner, name, geometry, created_at) VALUES(?,?,?,?)",
                         (user, name, json.dumps(geom), db.now_iso()))
        rules.areas_changed()
        return JSONResponse({"ok": True, "id": cur.lastrowid})
    rows = db.query("SELECT id, name, geometry FROM areas WHERE owner=? ORDER BY name", (user,))
    return JSONResponse({"items": [{**r, "geometry": json.loads(r["geometry"])} for r in rows]})


@api
async def api_area(request: Request):
    user = _user(request)
    aid = int(request.path_params["aid"])
    if not db.query_one("SELECT id FROM areas WHERE id=? AND owner=?", (aid, user)):
        raise ValueError("Область не найдена")
    if request.method == "DELETE":
        used = db.query("SELECT name FROM rules WHERE owner=? AND params LIKE ?", (user, f'%"area:{aid}"%'))
        if used:
            raise ValueError("Область используется в правилах: " + ", ".join(r["name"] for r in used))
        db.execute("DELETE FROM areas WHERE id=?", (aid,))
    else:
        body = await request.json()
        db.execute("UPDATE areas SET name=? WHERE id=?", (str(body.get("name") or "").strip()[:80] or "Моя область", aid))
    rules.areas_changed()
    return JSONResponse({"ok": True})


# --------------------------------------------------------------------------- API: уведомления
@api
async def api_notifications(request: Request):
    after = int(request.query_params.get("after_id", 0) or 0)
    limit = min(int(request.query_params.get("limit", 200) or 200), 500)
    only_unread = request.query_params.get("unread") == "1"
    user = _user(request)
    sql = (f"SELECT n.*, CASE WHEN {NOTIF_UNREAD} THEN 0 ELSE 1 END AS is_read FROM notifications n "
           f"WHERE n.id > ? AND {NOTIF_VISIBLE}" + (f" AND {NOTIF_UNREAD}" if only_unread else ""))
    params = [user, after, user, user, user] + ([user] if only_unread else [])
    rows = db.query(sql + " ORDER BY n.id DESC LIMIT ?", (*params, limit))
    for r in rows:
        r["zone_name"] = zones.zone_name(r["zone_id"]) if r["zone_id"] else None
    return JSONResponse({"items": rows, "unread": _unread(user)})


@api
async def api_notifications_read(request: Request):
    body = await request.json()
    user = _user(request)
    if body.get("all"):
        db.execute(f"INSERT OR IGNORE INTO notification_reads(username, notification_id) "
                   f"SELECT ?, n.id FROM notifications n WHERE {NOTIF_VISIBLE}", (user, user, user, user))
    else:
        ids = [int(i) for i in body.get("ids", [])][:500]
        for i in ids:
            db.execute("INSERT OR IGNORE INTO notification_reads(username, notification_id) VALUES(?,?)", (user, i))
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


async def _screening_loop(stop: asyncio.Event):
    """Санкционные списки и задержания: проверяем раз в 6 часов, обновляем, если прошло больше 20 часов."""
    await asyncio.sleep(60)
    while not stop.is_set():
        if screening.due() and not screening.state["running"]:
            try:
                log.info("Санкции и задержания: %s", await asyncio.to_thread(screening.run_daily))
            except Exception:  # noqa: BLE001
                log.exception("Ошибка обновления санкций и задержаний")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=6 * 3600)


async def _maintenance_loop(stop: asyncio.Event):
    while not stop.is_set():
        try:
            ingest.cleanup_trails()
            alerts.check_stale()
            incidents.cleanup()
            rules.periodic()
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
             asyncio.create_task(_traffic_snapshot_loop(stop)),
             asyncio.create_task(_screening_loop(stop))]
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
    Route("/api/incidents", api_incidents),
    Route("/api/screening", api_screening, methods=["GET", "POST"]),
    Route("/api/rules", api_rules, methods=["GET", "POST"]),
    Route("/api/rules/{rid:int}", api_rule, methods=["PATCH", "DELETE"]),
    Route("/api/areas", api_areas, methods=["GET", "POST"]),
    Route("/api/areas/{aid:int}", api_area, methods=["PATCH", "DELETE"]),
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
    Route("/api/fleets/{fid:int}/share", api_fleet_share, methods=["POST"]),
    Route("/api/users", api_users),
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
