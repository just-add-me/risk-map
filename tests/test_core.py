"""Тесты логики, не требующие интернета и shapely.

Запуск:  python -m pytest -q tests   (или просто: python tests/test_core.py)
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="warrisk-test-")

from app import alerts, db, zones  # noqa: E402
from app.ais import ingest, simulator  # noqa: E402
from app.circular import coords, parser, pipeline  # noqa: E402
from app.fleet_import import parse_file  # noqa: E402
from app.geo.zone_index import ZoneIndex  # noqa: E402

FIXTURE = (ROOT / "tests" / "fixtures" / "JWLA-035.txt").read_text(encoding="utf-8")


def test_coords():
    pts = coords.find_points("from the Pakistan coastline at 25°19’15”N, 65°E; line to 10°48’N, 65°E")
    assert pts == [(65.0, 25.320833), (65.0, 10.8)]
    pts = coords.find_points("from Mnazi Bay at 10°19.6'S, 40°18.9'E to high seas point at 10° 10.3' \nS, 40° 34.44' E")
    assert pts == [(40.315, -10.326667), (40.574, -10.171667)]
    assert coords.find_latitude_line("south of Latitude 25.5°N, (not including") == 25.5
    assert coords.find_latitude_line("south of horizontal line at 52° 30’ N") == 52.5
    assert coords.find_longitude_line("to vertical line at 41° E") == 41.0
    # линия широты не должна склеиваться со следующей точкой
    pts = coords.find_points("by the Red Sea, south of Latitude 25.5°N; from 25°19’15”N, 65°E")
    assert pts == [(65.0, 25.320833)]


def test_parser_jwla035():
    s = parser.parse(FIXTURE)
    assert s["ref"] == "JWLA-035" and s["issued"] == "2026-09-16"
    ids = {e["id"]: e for e in s["entries"]}
    assert len(ids) == 31
    assert ids["europe-black-sea"]["template"] == "black_sea_basin"
    assert ids["europe-black-sea"]["params"]["keep_countries"] == ["Russia", "Ukraine"]
    assert ids["europe-black-sea"]["cover_change"] == "amended"
    assert ids["russia"]["cover_change"] == "unchanged"
    assert ids["guyana"]["severity"] == "conditional"
    assert ids["iraq"]["params"]["extras"] == "offshore_terminals"
    io = next(e for e in s["entries"] if e["template"] == "indian_ocean")
    assert io["params"]["red_sea_lat"] == 25.5 and io["params"]["exclude_egypt_tw"]
    assert len(io["params"]["points"]) == 5
    assert not any(e["needs_review"] for e in s["entries"])


def test_parser_diff():
    old = parser.parse(FIXTURE)
    new_text = FIXTURE.replace("Libya\n", "").replace("Togo\n", "Togo\nCameroon\n") \
                      .replace("south of Latitude 25.5°N", "south of Latitude 27°N")
    new = parser.parse(new_text.replace("JWLA-035", "JWLA-036"))
    d = parser.diff(old, new)
    assert d["added"] == ["cameroon"]
    assert d["removed"] == ["libya"]
    assert any("indian-ocean" in i for i in d["amended"])


def _square(lon0, lat0, size, hole=None):
    ring = [[lon0, lat0], [lon0 + size, lat0], [lon0 + size, lat0 + size], [lon0, lat0 + size], [lon0, lat0]]
    rings = [ring]
    if hole:
        hx, hy, hs = hole
        rings.append([[hx, hy], [hx + hs, hy], [hx + hs, hy + hs], [hx, hy + hs], [hx, hy]])
    return {"type": "Polygon", "coordinates": rings}


FAKE_ZONES = {
    "type": "FeatureCollection", "properties": {"ref": "TEST-001"},
    "features": [
        {"type": "Feature", "properties": {"id": "red-sea", "name": "Red Sea test", "severity": "full"},
         "geometry": _square(35, 12, 10, hole=(38, 15, 1))},
        {"type": "Feature", "properties": {"id": "cond", "name": "Conditional", "severity": "conditional"},
         "geometry": {"type": "MultiPolygon", "coordinates": [_square(-60, 8, 2)["coordinates"]]}},
    ],
}


def test_zone_index():
    idx = ZoneIndex(FAKE_ZONES)
    z = idx.get("red-sea")
    assert idx.contains(z, 40, 20)
    assert not idx.contains(z, 38.5, 15.5)        # «дырка»
    assert not idx.contains(z, 34, 20)
    d = idx.distance_nm(z, 34.0, 20.0, 100)       # 1° долготы на 20°N ≈ 56.4 мили
    assert 55 < d < 58, d
    assert idx.distance_nm(z, 30.0, 20.0, 50) is None
    r = idx.evaluate(-59, 9, 50)
    assert r["cond"]["status"] == "inside"


def test_alert_flow():
    db.connect()
    zones.set_active(FAKE_ZONES)
    db.execute("INSERT INTO fleets(name, owner, created_at) VALUES('Test', 'admin', ?)", (db.now_iso(),))
    db.execute("INSERT INTO vessels(fleet_id, mmsi, name, created_at) VALUES(1, '273000001', 'TEST ONE', ?)",
               (db.now_iso(),))
    kinds = lambda: [n["kind"] for n in db.query("SELECT kind FROM notifications ORDER BY id")]  # noqa: E731

    ingest.position("273000001", 20.0, 33.5, sog=12)      # далеко от зоны (≈84 мили)
    assert kinds() == []
    ingest.position("273000001", 20.0, 34.5, sog=12)      # ≈28 миль — приближается
    assert kinds() == ["zone_near"]
    ingest.position("273000001", 20.0, 34.8, sog=12)      # всё ещё рядом — без дублей
    assert kinds() == ["zone_near"]
    ingest.position("273000001", 20.0, 35.5, sog=12)      # внутри
    assert kinds() == ["zone_near", "zone_enter"]
    ingest.position("273000001", 20.0, 34.998, sog=12)   # в 0.1 мили снаружи — гистерезис держит «внутри»
    assert kinds() == ["zone_near", "zone_enter"]
    ingest.position("273000001", 20.0, 34.9, sog=12)      # вышел
    assert kinds() == ["zone_near", "zone_enter", "zone_exit"]
    st = alerts.vessel_status("273000001")
    assert st["level"] == "near"

    # смена зон: новая версия без red-sea, судно сидело внутри → уведомление с причиной
    ingest.position("273000001", 20.0, 36.0, sog=12)
    zones.set_active({**FAKE_ZONES, "features": FAKE_ZONES["features"][1:]})
    alerts.reevaluate_all("Причина: тест")
    last = db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT 1")[0]
    assert last["kind"] == "zone_exit" and "тест" in last["body"]
    assert alerts.vessel_status("273000001")["level"] == "safe"


def test_simulator_route():
    _, _, pts = simulator.ROUTES[0]
    lon, lat, cog = simulator.position_at(pts, 0)
    assert (round(lon, 2), round(lat, 2)) == pts[0]
    total = sum(simulator._seg_nm(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    lon, lat, _ = simulator.position_at(pts, total)
    assert abs(lon - pts[-1][0]) < 1e-6 and abs(lat - pts[-1][1]) < 1e-6
    lon, lat, _ = simulator.position_at(pts, 2 * total)  # вернулся обратно
    assert abs(lon - pts[0][0]) < 1e-6


def test_fleet_import():
    csv_data = "Vessel Name;IMO;MMSI\nNORDIC STAR;9123456;273111222\nNO MMSI;9234567;\n".encode()
    items = parse_file(csv_data, "fleet.csv")
    assert items[0] == {"mmsi": "273111222", "imo": "9123456", "name": "NORDIC STAR"}
    assert items[1]["mmsi"] == "" and items[1]["imo"] == "9234567"
    plain = parse_file(b"273111222\n273111333 OCEAN ONE\n", "list.txt")
    assert [i["mmsi"] for i in plain] == ["273111222", "273111333"] and plain[1]["name"] == "OCEAN ONE"


def test_find_latest_link():
    html = """<p>The current listed areas are linked below:</p>
      <a href="https://lmalloyds.com/wp-content/uploads/2026/03/JWLA-033_Iran.pdf">JWLA-033</a>
      <a href="/wp-content/uploads/2026/09/JWLA-035-Black-Sea.pdf">JWLA-035</a>
      <a href="https://lmalloyds.com/wp-content/uploads/2025/06/JWLA-034-Saudi-Arabia.docx">JWLA-034</a>
      <a href="https://lmalloyds.com/committee/">JWLA committee page</a>"""
    ref, n, url = pipeline.find_latest_link(html, "https://lmalloyds.com/committee/joint-war-committee/")
    assert (ref, n) == ("JWLA-035", 35)
    assert url == "https://lmalloyds.com/wp-content/uploads/2026/09/JWLA-035-Black-Sea.pdf"


if __name__ == "__main__":
    failed = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"✓ {name}")
            except Exception as exc:  # noqa: BLE001
                failed += 1
                print(f"✗ {name}: {exc!r}")
    print(json.dumps({"failed": failed}))
    sys.exit(1 if failed else 0)


# --------------------------------------------------------------------------- IBF / INL
from app.layers import ibf  # noqa: E402

IBF_FIXTURE = (ROOT / "tests" / "fixtures" / "IBF-2026-10-01.txt").read_text(encoding="utf-8")


def test_ibf_parse():
    spec = ibf.parse(IBF_FIXTURE)
    assert spec["list_date"] == "2026-10-01" and spec["list_until"] == "2026-10-22"
    ds = {d["number"]: d for d in spec["designations"]}
    assert len(ds) == 10
    assert [ds[n]["category"] for n in range(1, 11)] == \
        ["woa", "woa", "woa", "woa", "woa", "hra", "woa", "erz", "erz", "erz2"]
    assert all(d["template"] for d in ds.values())
    assert ds[7]["valid_from"] == "2026-03-06" and ds[7]["valid_to"] == "2026-10-22"
    assert ds[9]["valid_from"] == "2026-03-13"            # в PDF «1 3th of M arch» — рваный текст
    assert ds[6]["title"] == "IBF High Risk Area – Black Sea"
    # отпечатки границ совпадают с теми, по которым сделаны шаблоны
    assert all(d["fingerprint"] in ibf.KNOWN_FINGERPRINTS[d["template"]] for d in ds.values())


def test_ibf_diff_and_links():
    spec = ibf.parse(IBF_FIXTURE)
    prev = json.loads(json.dumps(spec))
    prev["designations"] = prev["designations"][:-1]
    prev["designations"][6]["valid_to"] = "2026-10-01"
    lines = ibf.diff(prev, spec)
    assert any(ln.startswith("Добавлена") for ln in lines)
    assert any(ln.startswith("Срок продлён до 2026-10-22") for ln in lines)
    html = ('<a href="/up/IBF-List-of-designated-risk-Areas-as-of-2nd-of-March-2026.pdf">old</a>'
            '<a href="https://x.org/IBF-List-of-designated-risk-Areas-as-of-1st-of-October-2026.pdf">new</a>'
            '<a href="/up/DMW_ADVISORY_60_2026_.pdf">other</a>')
    assert ibf.find_latest_pdf(html, "https://imec.org.uk/p").endswith("1st-of-October-2026.pdf")


def test_inl_season():
    from datetime import datetime
    winter = {"season": {"from": "12-10", "to": "05-25"}}
    assert zones.in_season(winter, datetime(2026, 1, 15))
    assert zones.in_season(winter, datetime(2026, 12, 10))
    assert not zones.in_season(winter, datetime(2026, 7, 1))
    assert zones.in_season({}, datetime(2026, 7, 1))


def test_shipinfo_and_traffic():
    from app.ais import aisstream, shipinfo, traffic
    assert shipinfo.category(37) == "yacht" and shipinfo.category(36) == "yacht"
    assert shipinfo.category(71) == "cargo" and shipinfo.category(84) == "tanker" and shipinfo.category(None) == "other"
    assert shipinfo.type_name(81) == "Танкер, опасный груз X"
    assert shipinfo.flag("273123456") == "Россия" and shipinfo.flag("538010472") == "Маршалловы о-ва"
    assert shipinfo.flag("992731234") is None
    assert shipinfo.flag_iso("538010472") == "mh" and shipinfo.nav_status(1) == "На якоре"
    assert shipinfo.dimensions({"A": 150, "B": 33, "C": 16, "D": 16}) == (183, 32)
    assert shipinfo.eta_text({"Month": 10, "Day": 23, "Hour": 6, "Minute": 30}) == "23.10 06:30 UTC"
    # поток aisstream: чужое судно — только в общий слой, без записи в базу
    aisstream._tracked = set()
    aisstream.handle(json.dumps({"MessageType": "ShipStaticData",
                                 "MetaData": {"MMSI": 538010472, "ShipName": "BELGRAVIA V"},
                                 "Message": {"ShipStaticData": {"Type": 80, "ImoNumber": 9300001,
                                             "Dimension": {"A": 200, "B": 50, "C": 20, "D": 22},
                                             "MaximumStaticDraught": 12.4, "Destination": "NOVOROSSIYSK"}}}))
    aisstream.handle(json.dumps({"MessageType": "PositionReport",
                                 "MetaData": {"MMSI": 538010472, "ShipName": "BELGRAVIA V"},
                                 "Message": {"PositionReport": {"Latitude": 42.47, "Longitude": 28.2,
                                                                "Sog": 13.3, "Cog": 45, "TrueHeading": 511}}}))
    d = traffic.details(traffic.get("538010472"))
    assert d["category"] == "tanker" and d["length"] == 250 and d["draught"] == 12.4 and d["heading"] is None
    traffic.reset_caches()
    r = traffic.query(27, 40, 42, 48, 12)
    assert r["total"] == 1 and r["counts"]["tanker"] == 1 and r["ships"][0][0] == "538010472"
    assert traffic.query(170, 40, -170, 48, 12)["total"] == 0        # прямоугольник через 180°
    # группировка: два судна рядом на мелком масштабе — одна группа, на крупном — два судна
    traffic.update_position("538010473", 42.48, 28.26, 10.0, 0.0, None, "NEIGHBOUR", "A")
    traffic.reset_caches()
    far = traffic.query(27, 40, 42, 48, 4)
    assert far["ships"] == [] and far["clusters"][0]["n"] == 2
    assert len(traffic.query(27, 40, 42, 48, 8)["ships"]) == 2       # двух судов мало для группы
    assert len(traffic.query(27, 40, 42, 48, 14)["ships"]) == 2
    assert traffic.query(27, 40, 42, 48, 5, cats={"cargo"})["total"] == 0
    assert db.query_one("SELECT * FROM ais_state WHERE mmsi='538010472'") is None
    assert len(traffic.trail("538010472")) == 2         # точка трека + текущая позиция
    aisstream._tracked = None


def test_traffic_snapshot():
    from app.ais import traffic
    traffic.update_position("273000099", 44.6, 33.5, 10.0, 90.0, None, "SNAP TEST", "A", 0)
    traffic.update_static("273000099", ship_type=70, length=150)
    assert traffic.save() >= 1
    traffic._ships.clear()
    assert traffic.load() >= 1
    d = traffic.details(traffic.get("273000099"))
    assert d["category"] == "cargo" and d["lat"] == 44.6 and d["nav_status"] == "Идёт под двигателем"


def test_ship_search():
    from app.ais import traffic
    traffic.update_position("636012345", 25.0, 55.0, 12.0, 10.0, None, "MSC ANTONIA", "A")
    traffic.update_static("636012345", imo="9619438", callsign="D5AB1")
    assert traffic.search("antonia")[0]["mmsi"] == "636012345"
    assert traffic.search("9619438")[0]["name"] == "MSC ANTONIA"
    assert traffic.search("6360123")[0]["mmsi"] == "636012345"
    assert traffic.search("d5ab1")[0]["mmsi"] == "636012345"
    assert traffic.search("x") == []


def test_imo_checksum_and_near():
    from app import fleet_import
    from app.ais import traffic
    assert fleet_import.imo_valid("9653795") and fleet_import.imo_valid("9811000")
    assert not fleet_import.imo_valid("9653796") and not fleet_import.imo_valid("123")
    traffic.update_position("273000777", 46.49, 30.76, 0.0, None, None, "PORT SHIP", "A", 5)
    near = traffic.near(46.50, 30.75, 5)
    assert any(s["mmsi"] == "273000777" and s["nav_status"] == "Ошвартовано" for s in near)
    assert not any(s["mmsi"] == "273000777" for s in traffic.near(44.0, 30.75, 5))


def test_users():
    from app import users
    pwd = users.add("tester1")
    assert users.verify("tester1", pwd) and not users.verify("tester1", pwd + "x")
    assert not users.verify("nobody", pwd)
    stored = db.query_one("SELECT pw_hash FROM users WHERE username='tester1'")["pw_hash"]
    assert pwd not in stored and stored.startswith("pbkdf2$")
    new = users.set_password("tester1")
    assert users.verify("tester1", new) and not users.verify("tester1", pwd)
    users.delete("tester1")
    assert not users.exists("tester1")
    try:
        users.add("x y")
        raise AssertionError("должна быть ошибка")
    except ValueError:
        pass


def test_fleet_access_and_migration():
    import sqlite3
    from app import access, config, users
    # владелец, «гость» на просмотр, «гость» с правом редактирования, посторонний
    for u in ("anna", "boris", "vera"):
        users.add(u, "pw-" + u)
    fid = db.execute("INSERT INTO fleets(name, owner, created_at) VALUES('Танкеры', 'anna', ?)",
                     (db.now_iso(),)).lastrowid
    db.execute("INSERT INTO vessels(fleet_id, mmsi, created_at) VALUES(?, '538000001', ?)", (fid, db.now_iso()))
    db.execute("INSERT INTO fleet_shares(fleet_id, username, can_edit) VALUES(?, 'boris', 0)", (fid,))
    db.execute("INSERT INTO fleet_shares(fleet_id, username, can_edit) VALUES(?, 'vera', 1)", (fid,))
    assert access.level(fid, "anna") == "owner" and access.level(fid, "boris") == "view"
    assert access.level(fid, "vera") == "edit" and access.level(fid, config.ADMIN_USER) is None
    access.require(fid, "vera", "edit")
    for user, need in (("boris", "edit"), ("vera", "owner"), (config.ADMIN_USER, "view")):
        try:
            access.require(fid, user, need)
            raise AssertionError(f"{user} не должен иметь {need}")
        except ValueError:
            pass
    assert "538000001" in access.mmsis("boris") and "538000001" not in access.mmsis(config.ADMIN_USER)
    # удаление владельца: флот переходит администратору
    assert users.delete("anna") == 1 and access.level(fid, config.ADMIN_USER) == "owner"
    # миграция старой базы: флоты без владельца → администратору, суда не теряются
    path = config.DATA_DIR / "old.db"
    old = sqlite3.connect(path)
    old.executescript("PRAGMA foreign_keys=ON; CREATE TABLE fleets(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, "
                      "created_at TEXT NOT NULL); CREATE TABLE vessels(id INTEGER PRIMARY KEY, fleet_id INTEGER NOT NULL "
                      "REFERENCES fleets(id) ON DELETE CASCADE, mmsi TEXT NOT NULL, imo TEXT, name TEXT, created_at TEXT "
                      "NOT NULL); CREATE TABLE notifications(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, severity TEXT, "
                      "title TEXT, body TEXT, mmsi TEXT, zone_id TEXT, is_read INTEGER DEFAULT 0);"
                      "INSERT INTO fleets VALUES(1, 'Старый', 'x'); INSERT INTO vessels VALUES(1, 1, '273000001', NULL, NULL, 'x');"
                      "INSERT INTO notifications(id, is_read) VALUES(5, 1);")
    old.isolation_level = None
    db._migrate_fleet_owner(old)
    assert old.execute("SELECT owner FROM fleets").fetchone()[0] == config.ADMIN_USER
    assert old.execute("SELECT COUNT(*) FROM vessels").fetchone()[0] == 1
    assert old.execute("SELECT notification_id FROM notification_reads").fetchone()[0] == 5
    old.close()


def test_add_by_imo_pending_resolves():
    from app.ais import aisstream, ingest
    fid = db.execute("INSERT INTO fleets(name, owner, created_at) VALUES('IMO-флот', 'admin', ?)",
                     (db.now_iso(),)).lastrowid
    db.execute("INSERT INTO vessels(fleet_id, mmsi, imo, created_at) VALUES(?, 'IMO:9811000', '9811000', ?)",
               (fid, db.now_iso()))
    assert "IMO:9811000" not in ingest.tracked_mmsis() and "9811000" in ingest.pending_imos()
    aisstream._tracked, aisstream._pending = set(), ingest.pending_imos()
    aisstream.handle(json.dumps({"MessageType": "ShipStaticData", "MetaData": {"MMSI": 353136000, "ShipName": "EVER GIVEN"},
                                 "Message": {"ShipStaticData": {"Type": 70, "ImoNumber": 9811000}}}))
    v = db.query_one("SELECT mmsi FROM vessels WHERE fleet_id=?", (fid,))
    assert v["mmsi"] == "353136000" and "353136000" in aisstream._tracked
    assert db.query_one("SELECT title FROM notifications WHERE mmsi='353136000'")["title"].startswith("EVER GIVEN")
    aisstream._tracked, aisstream._pending = None, set()


def test_incidents():
    from app.ais import aisstream, incidents
    assert incidents.classify("SART ACTIVE") == ("sart", False)
    assert incidents.classify("SART TEST") == ("sart", True)
    assert incidents.classify("MAYDAY MAYDAY FIRE ON BOARD")[0] == "distress"
    assert incidents.classify("DRIFTING CONTAINER 43-10N 029-50E")[0] == "safety"
    aisstream._tracked = {"273000555"}
    # аварийный маяк (MMSI 970…), статус «на мели» у судна флота, текстовое сообщение, повтор
    aisstream.handle(json.dumps({"MessageType": "PositionReport", "MetaData": {"MMSI": 970123456, "ShipName": "SART"},
                                 "Message": {"PositionReport": {"Latitude": 43.1, "Longitude": 29.5, "NavigationalStatus": 14}}}))
    # понтон с постоянным статусом «на мели» — не происшествие; судно, сменившее статус, — происшествие
    aisstream.handle(json.dumps({"MessageType": "PositionReport", "MetaData": {"MMSI": 211000001, "ShipName": "PONTON"},
                                 "Message": {"PositionReport": {"Latitude": 50.0, "Longitude": 8.6, "NavigationalStatus": 6}}}))
    for nav in (0, 6):
        aisstream.handle(json.dumps({"MessageType": "PositionReport", "MetaData": {"MMSI": 273000555, "ShipName": "STUCK"},
                                     "Message": {"PositionReport": {"Latitude": 44.0, "Longitude": 33.0, "NavigationalStatus": nav}}}))
    for _ in range(3):
        aisstream.handle(json.dumps({"MessageType": "SafetyBroadcastMessage",
                                     "MetaData": {"MMSI": 2734567, "latitude": 44.6, "longitude": 33.5},
                                     "Message": {"SafetyBroadcastMessage": {"Text": "NAVIGATIONAL WARNING MINE DANGER"}}}))
    items = {i["kind"]: i for i in incidents.recent(1)}
    assert items["sart"]["lat"] == 43.1 and not items["sart"]["test"]
    assert items["aground"]["name"] == "STUCK"
    assert not any(i["mmsi"] == "211000001" for i in incidents.recent(1))
    assert items["safety"]["text"] == "NAVIGATIONAL WARNING MINE DANGER" and items["safety"]["lat"] == 44.6
    assert len([i for i in incidents.recent(1) if i["kind"] == "safety"]) == 1      # повторы не плодят записи
    note = db.query_one("SELECT severity, title FROM notifications WHERE kind='incident' AND mmsi='273000555'")
    assert note["severity"] == "warning" and "мели" in note["title"]
    aisstream._tracked = None


def test_rules_engine():
    from app import rules, users
    from app.ais import ingest, incidents
    from app.layers import ports
    ports._ports = [{"name": "Testport", "lat": 44.0, "lon": 30.0, "country": "Testland"}]
    users.add("olga", "pw")
    users.add("pavel", "pw")
    fid = db.execute("INSERT INTO fleets(name, owner, created_at) VALUES('Правила', 'olga', ?)", (db.now_iso(),)).lastrowid
    db.execute("INSERT INTO fleet_shares(fleet_id, username, can_edit) VALUES(?, 'pavel', 0)", (fid,))
    m = "273777001"
    db.execute("INSERT INTO vessels(fleet_id, mmsi, name, created_at) VALUES(?, ?, 'RULE SHIP', ?)", (fid, m, db.now_iso()))
    area = db.execute("INSERT INTO areas(owner, name, geometry, created_at) VALUES('olga', 'Квадрат', ?, ?)",
                      (json.dumps({"type": "Polygon", "coordinates": [[[31, 44], [32, 44], [32, 45], [31, 45], [31, 44]]]}),
                       db.now_iso())).lastrowid
    rules.areas_changed()

    def add(kind, params, audience="me", target_type="fleet", target=str(fid)):
        p = rules.validate(kind, params, "olga")
        return db.execute("INSERT INTO rules(owner, name, kind, target_type, target, params, audience, severity, created_at) "
                          "VALUES('olga', ?, ?, ?, ?, ?, ?, 'warning', ?)",
                          (rules.KINDS[kind], kind, target_type, target, json.dumps(p), audience, db.now_iso())).lastrowid
    add("zone_enter", {"scope": f"area:{area}"}, audience="fleet")
    add("zone_exit", {"scope": f"area:{area}"})
    add("nav_status", {"statuses": [1]})
    add("speed", {"op": "below", "knots": 2, "minutes": 0})
    add("port_arrival", {"port_id": "any"})
    add("port_departure", {"port_id": "0"})
    add("destination", {"eta": False})
    add("incident", {"radius_nm": 30, "kinds": ["distress"]}, target_type="vessel", target=m)
    mine = lambda u: [n["title"].split(": ", 1)[1] for n in db.query(  # noqa: E731
        "SELECT title FROM notifications WHERE kind='rule' AND username=? ORDER BY id", (u,))]

    ingest.position(m, 44.5, 30.5, sog=10, nav=0)          # первое наблюдение — тишина
    assert mine("olga") == []
    ingest.position(m, 44.5, 31.5, sog=10, nav=0)          # вошло в свою область
    assert mine("olga") == ["Вход в зону"] and mine("pavel") == ["Вход в зону"]   # audience=fleet
    ingest.position(m, 44.5, 32.5, sog=10, nav=0)          # вышло; «выход» только владельцу
    assert mine("olga")[-1] == "Выход из зоны" and mine("pavel") == ["Вход в зону"]
    ingest.position(m, 44.01, 30.01, sog=0.5, nav=1)       # у порта, якорь, медленно
    assert {"Смена статуса", "Скорость", "Заход в порт"} <= set(mine("olga"))
    ingest.position(m, 44.5, 30.5, sog=12, nav=0)          # ушло дальше 5 миль от порта
    assert mine("olga")[-1] == "Выход из порта"
    ingest.static_data(m, destination="ODESSA")
    ingest.static_data(m, destination="CONSTANTA")
    assert mine("olga")[-1] == "Смена назначения или ETA"
    incidents.record("distress", "273999999", name="SOS SHIP", lat=44.6, lon=30.6, text="MAYDAY")
    assert mine("olga")[-1] == "Происшествие рядом"
    # «нет сигнала»: позиция 3 часа назад
    rid = add("ais_lost", {"hours": 2})
    db.execute("UPDATE ais_state SET ts=? WHERE mmsi=?", ("2020-01-01T00:00:00+00:00", m))
    rules.periodic()
    rules.periodic()                                         # повтор — без дубля
    assert mine("olga").count("Нет сигнала AIS") == 1
    # выключенные правила не срабатывают
    before = len(mine("olga"))
    db.execute("UPDATE rules SET enabled=0 WHERE id <> ?", (rid,))
    ingest.position(m, 44.5, 31.5, sog=10, nav=0)
    assert len(mine("olga")) == before
    try:
        rules.validate("zone_near", {"scope": "layer:jwc", "distance_nm": "abc"}, "olga")
        raise AssertionError("должна быть ошибка")
    except ValueError:
        pass
    ports._ports = None


def test_screening_parsers_and_fleet_hit():
    from app import screening
    from app.layers import ports
    ofac = ('4243,"EBANO","vessel","CUBA","-0- ","-0- ","General Cargo","2595","1865","Panama","-0- ",'
            '"Vessel Registration Identification IMO 7406784; f.k.a. \'ANA I\'."\n'
            '1,"SOMEONE","individual","SDGT","-0-","-0-","-0-","-0-","-0-","-0-","-0-","-0-"\n')
    assert [x["imo"] for x in screening.parse_ofac(ofac)] == ["7406784"]
    uk = ("Report Date: 16-Oct-2026\n"
          "Last Updated,Unique ID,Name 6,Name type,Regime Name,Designation Type,Date Designated,IMO number,Type of ship\n"
          "01/01/2026,RUS1,ASTRA,Primary Name,Russia,Ship,12/09/2025,IMO9273387,Crude oil tanker\n"
          "01/01/2026,RUS1,ASTRA II,Alias,Russia,Ship,12/09/2025,IMO9273387,Crude oil tanker\n"
          "01/01/2026,AFG1,X,Primary Name,Afghanistan,Entity,01/01/2020,,\n")
    u = screening.parse_uk(uk)
    assert len(u) == 1 and u[0]["name"] == "ASTRA" and u[0]["listed_on"] == "2025-09-12"
    eu = "﻿Vessel name;IMO number ;Date of application;Subject to;;\nAngara;9179842;25.06.2024;Article 3s;;\n"
    assert screening.parse_eu(eu)[0]["listed_on"] == "2024-06-25"
    un = ("<CONSOLIDATED_LIST><ENTITIES><ENTITY><FIRST_NAME>CHON MA SAN</FIRST_NAME><UN_LIST_TYPE>DPRK</UN_LIST_TYPE>"
          "<COMMENTS1>IMO: 8660313</COMMENTS1></ENTITY></ENTITIES></CONSOLIDATED_LIST>")
    assert screening.parse_un(un)[0]["imo"] == "8660313"
    tokyo = ("<tr><td>1</td><td>1130113</td><td>GIA ASPIRANT</td><td>Marshall Islands</td><td>2025-03-26</td>"
             "<td>44771</td><td>Bulk carrier</td><td>CCS</td><td>DNV</td><td>BSM</td><td>Vancouver, Canada</td>"
             "<td>02.10.2026</td><td>10.10.2026</td><td>15150 - ISM</td></tr>")
    d = screening.parse_tokyo(tokyo)[0]
    assert d["detained_on"] == "2026-10-02" and d["built"] == "2025" and d["port"] == "Vancouver, Canada"
    # судно флота нашлось в списке → происшествие и уведомление, повторная проверка — без дублей
    ports._ports = [{"name": "Vancouver", "lat": 49.3, "lon": -123.1, "country": "Canada"}]
    fid = db.execute("INSERT INTO fleets(name, owner, created_at) VALUES('Санкции', 'admin', ?)", (db.now_iso(),)).lastrowid
    db.execute("INSERT INTO vessels(fleet_id, mmsi, imo, created_at) VALUES(?, '538099001', '9273387', ?)", (fid, db.now_iso()))
    db.execute("INSERT INTO vessels(fleet_id, mmsi, imo, created_at) VALUES(?, '538099002', '1130113', ?)", (fid, db.now_iso()))
    now = db.now_iso()
    db.execute("INSERT INTO sanctions(imo, source, name, program, listed_on, url, first_seen, last_seen) "
               "VALUES('9273387', 'uk', 'ASTRA', 'Russia', '2025-09-12', 'u', ?, ?)", (now, now))
    db.execute("INSERT INTO detentions(source, imo, detained_on, name, port, released_on, reasons, first_seen) "
               "VALUES('tokyo', '1130113', '2026-10-02', 'GIA ASPIRANT', 'Vancouver, Canada', '2026-10-10', 'ISM', ?)", (now,))
    assert screening.screen_fleets() == 2 and screening.screen_fleets() == 0
    kinds = {r["kind"]: r for r in db.query("SELECT * FROM incidents WHERE mmsi IN ('538099001', '538099002')")}
    assert set(kinds) == {"sanction", "detention"} and kinds["detention"]["lat"] == 49.3
    assert db.query_one("SELECT severity FROM notifications WHERE mmsi='538099001'")["severity"] == "critical"
    assert screening.lookup("9273387")["sanctions"][0]["label"] == "Великобритания"
    ports._ports = None


def test_supervisor_restarts_and_notifications_cleanup():
    import asyncio
    from app import main
    calls = []

    async def flaky(stop):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("сбой")        # первые два запуска падают
        stop.set()

    async def run():
        stop = asyncio.Event()
        await asyncio.wait_for(main._supervise("тест", flaky, stop, first_delay=0.01), timeout=5)
    asyncio.run(run())
    assert len(calls) == 3
    old = db.notify("system", "info", "старое")
    db.execute("UPDATE notifications SET ts='2020-01-01T00:00:00+00:00' WHERE id=?", (old,))
    db.execute("INSERT INTO notification_reads(username, notification_id) VALUES('admin', ?)", (old,))
    fresh = db.notify("system", "info", "новое")
    assert db.cleanup_notifications() >= 1
    ids = {r["id"] for r in db.query("SELECT id FROM notifications")}
    assert old not in ids and fresh in ids
    assert not db.query("SELECT 1 FROM notification_reads WHERE notification_id=?", (old,))
