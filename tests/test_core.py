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
    db.execute("INSERT INTO fleets(name, created_at) VALUES('Test', ?)", (db.now_iso(),))
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
    r = traffic.query(27, 40, 42, 48, 12)
    assert r["total"] == 1 and r["counts"]["tanker"] == 1 and r["ships"][0][0] == "538010472"
    assert traffic.query(170, 40, -170, 48, 12)["total"] == 0        # прямоугольник через 180°
    # группировка: два судна рядом на мелком масштабе — одна группа, на крупном — два судна
    traffic.update_position("538010473", 42.48, 28.26, 10.0, 0.0, None, "NEIGHBOUR", "A")
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
