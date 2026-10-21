"""Проверка сайта регулятора → скачивание → разбор → построение зон → уведомления.

Что делает автоматически:
  1. Раз в CIRCULAR_CHECK_HOURS часов открывает страницу Joint War Committee на
     lmalloyds.com и ищет ссылку на самый свежий JWLA-xxx.
  2. Если номер новее текущего — скачивает PDF/DOCX, извлекает текст, разбирает.
  3. Строит зоны (в отдельном потоке, это ~1–3 минуты) и делает их активными.
  4. Пересчитывает все суда и пишет уведомления о том, что изменилось.
Если какую-то зону не удалось понять — она помечается «требует проверки», а в
панели появляется предупреждение.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from typing import Optional
from urllib.parse import urljoin

import requests

from .. import alerts, config, db, zones
from . import parser

log = logging.getLogger(__name__)

_build_lock = asyncio.Lock()
state: dict = {"running": False, "step": "", "last_check": None, "last_error": None}


# --------------------------------------------------------------------------
def find_latest_link(html: str, base_url: str) -> Optional[tuple[str, int, str]]:
    """Ищет на странице ссылки на циркуляры JWLA. Вернёт (ref, номер, url) самого нового."""
    found: dict[int, str] = {}
    for m in re.finditer(r"""href\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", html, re.I | re.S):
        href, label = m.group(1), re.sub(r"<[^>]+>", " ", m.group(2))
        hay = f"{href} {label}"
        mm = re.search(r"JWLA[-_\s]?0*(\d{2,4})", hay, re.I)
        if not mm or not re.search(r"\.(pdf|docx?)(\?|$)", href, re.I):
            continue
        n = int(mm.group(1))
        url = urljoin(base_url, href)
        # PDF предпочтительнее DOCX
        if n not in found or (url.lower().endswith(".pdf") and not found[n].lower().endswith(".pdf")):
            found[n] = url
    if not found:
        return None
    n = max(found)
    return f"JWLA-{n:03d}", n, found[n]


def extract_text(data: bytes, name: str) -> str:
    if name.lower().endswith(".pdf") or data[:4] == b"%PDF":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    if name.lower().endswith(".docx") or data[:2] == b"PK":
        import docx  # python-docx
        d = docx.Document(io.BytesIO(data))
        return "\n".join(p.text for p in d.paragraphs)
    return data.decode("utf-8", errors="ignore")


def _http_get(url: str) -> requests.Response:
    r = requests.get(url, timeout=60, headers={"User-Agent": config.HTTP_USER_AGENT,
                                               "Accept-Language": "en-GB,en;q=0.9"})
    r.raise_for_status()
    return r


def _human_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"Сайт LMA ответил ошибкой HTTP {exc.response.status_code}"
    if isinstance(exc, (requests.ConnectionError, requests.Timeout)):
        return "Нет соединения с сайтом lmalloyds.com"
    return str(exc)


def current_number() -> int:
    row = db.query_one("SELECT MAX(number) AS n FROM circulars WHERE status IN ('active','superseded')")
    return int(row["n"] or 0) if row else 0


# --------------------------------------------------------------------------
async def check_now(manual: bool = False) -> dict:
    """Проверка сайта регулятора. Безопасно вызывать часто: лишний раз ничего не скачает."""
    if state["running"]:
        return {"ok": False, "message": "Проверка уже идёт"}
    state.update(running=True, step="Открываю сайт LMA", last_error=None)
    state["last_check"] = db.now_iso()
    db.kv_set("circular_last_check", state["last_check"])
    try:
        html = (await asyncio.to_thread(_http_get, config.JWC_PAGE_URL)).text
        latest = find_latest_link(html, config.JWC_PAGE_URL)
        if not latest:
            raise RuntimeError("На странице JWC не найдено ссылок на циркуляры JWLA")
        ref, number, url = latest
        db.kv_set("circular_last_seen", {"ref": ref, "url": url})
        if db.kv_get("circular_fail_notified"):
            db.kv_set("circular_fail_notified", False)
        known = db.query_one("SELECT status, source_url FROM circulars WHERE ref=?", (ref,))
        if known and known["status"] == "active" and known["source_url"] == "seed":
            # стартовая версия была из встроенного текста — подтянем оригинальный PDF для скачивания
            resp = await asyncio.to_thread(_http_get, url)
            name = url.rsplit("/", 1)[-1].split("?")[0] or f"{ref}.pdf"
            stored = f"{ref}__{re.sub(r'[^A-Za-z0-9._-]+', '_', name)[:100]}"
            (config.CIRCULARS_DIR / stored).write_bytes(resp.content)
            db.execute("UPDATE circulars SET source_url=?, file_name=? WHERE ref=?", (url, stored, ref))
        if number <= current_number() or (known and known["status"] in ("active", "superseded", "building")):
            return {"ok": True, "message": f"Новых циркуляров нет (последний на сайте: {ref})", "ref": ref}
        state["step"] = f"Скачиваю {ref}"
        resp = await asyncio.to_thread(_http_get, url)
        name = url.rsplit("/", 1)[-1].split("?")[0] or f"{ref}.pdf"
        return await ingest_document(resp.content, name, source_url=url, auto=True)
    except Exception as exc:  # noqa: BLE001
        msg = _human_error(exc)
        state["last_error"] = msg
        log.warning("Проверка циркуляров не удалась: %s", exc)
        # уведомляем один раз, а не при каждой неудачной попытке
        if manual or not db.kv_get("circular_fail_notified"):
            db.notify("system", "warning", "Не удалось проверить сайт регулятора",
                      f"{msg}. Повторю автоматически; можно загрузить циркуляр вручную на вкладке «Циркуляры».")
            db.kv_set("circular_fail_notified", True)
        return {"ok": False, "message": msg}
    finally:
        state.update(running=False, step="")


async def ingest_document(data: bytes, file_name: str, source_url: str = "", auto: bool = False,
                          force: bool = False) -> dict:
    """Принимает документ (скачанный роботом или загруженный вручную) и делает его активным."""
    text = await asyncio.to_thread(extract_text, data, file_name)
    spec = parser.parse(text)
    if not spec["ref"] or not spec["entries"]:
        raise ValueError("Не похоже на циркуляр JWLA: не найден номер или перечень зон")
    ref = spec["ref"]
    existing = db.query_one("SELECT id, status FROM circulars WHERE ref=?", (ref,))
    if existing and existing["status"] == "active" and not force:
        return {"ok": True, "message": f"{ref} уже активен", "ref": ref}

    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file_name)[:100]
    stored = f"{ref}__{safe_name}"
    (config.CIRCULARS_DIR / stored).write_bytes(data)

    prev = db.query_one("SELECT spec FROM circulars WHERE status='active' ORDER BY number DESC LIMIT 1")
    prev_spec = json.loads(prev["spec"]) if prev and prev["spec"] else None
    if prev_spec and prev_spec.get("ref") == ref:
        prev_spec = None
    changes = parser.diff(prev_spec, spec)

    db.execute(
        "INSERT INTO circulars(ref, number, issued, source_url, file_name, text, spec, diff, status, created_at) "
        "VALUES(?,?,?,?,?,?,?,?, 'new', ?) ON CONFLICT(ref) DO UPDATE SET issued=excluded.issued, "
        "source_url=excluded.source_url, file_name=excluded.file_name, text=excluded.text, spec=excluded.spec, "
        "diff=excluded.diff, status='new'",
        (ref, spec["number"], spec["issued"], source_url, stored, text,
         json.dumps(spec, ensure_ascii=False), json.dumps(changes, ensure_ascii=False), db.now_iso()),
    )
    if auto:
        db.notify("circular", "warning", f"Обнаружен новый циркуляр {ref}",
                  f"От {spec['issued'] or '—'}. Строю зоны на карте, это займёт пару минут.")
    return await build_and_activate(ref)


async def build_and_activate(ref: str) -> dict:
    async with _build_lock:
        row = db.query_one("SELECT * FROM circulars WHERE ref=?", (ref,))
        if not row:
            raise ValueError(f"{ref} не найден")
        spec = json.loads(row["spec"])
        changes = json.loads(row["diff"] or "{}")
        db.execute("UPDATE circulars SET status='building' WHERE ref=?", (ref,))
        state.update(running=True, step=f"Строю зоны {ref}")
        try:
            from ..geo import builder  # тяжёлый импорт (shapely) — только когда нужен
            fc, logs = await asyncio.to_thread(
                builder.build, spec, None, lambda name: state.update(step=f"{ref}: {name}"))
        except Exception as exc:
            db.execute("UPDATE circulars SET status='error', build_log=? WHERE ref=?", (str(exc), ref))
            db.notify("circular", "critical", f"Не удалось построить зоны {ref}", str(exc))
            state.update(running=False, step="")
            raise
        state.update(running=False, step="")

    # помечаем изменения: added / amended / unchanged — для подсветки на карте
    added, amended = set(changes.get("added", [])), set(changes.get("amended", []))
    for f in fc["features"]:
        src = f["properties"]["source_entry"]
        f["properties"]["change"] = "added" if src in added else ("amended" if src in amended else "unchanged")
    fc["properties"].update({"ref": ref, "issued": spec.get("issued"), "built_at": db.now_iso()})

    (config.ZONES_DIR / f"{ref}.geojson").write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    zones.set_active(fc)
    db.execute("UPDATE circulars SET status='superseded' WHERE status='active' AND ref<>?", (ref,))
    db.execute("UPDATE circulars SET status='active', activated_at=?, build_log=?, spec=? WHERE ref=?",
               (db.now_iso(), "\n".join(logs), json.dumps(spec, ensure_ascii=False), ref))

    review = [e for e in spec["entries"] if e.get("needs_review")]
    names = {e["id"]: e["name"] for e in spec["entries"]}
    lines = []
    if changes.get("added"):
        lines.append("Добавлено: " + ", ".join(names.get(i, i) for i in changes["added"]))
    if changes.get("amended"):
        lines.append("Изменено: " + ", ".join(names.get(i, i) for i in changes["amended"]))
    if changes.get("removed_names"):
        lines.append("Исключено: " + ", ".join(changes["removed_names"]))
    if review:
        lines.append("⚠ Требует ручной проверки: " + "; ".join(f"{e['name']} — {e.get('review_reason', '')}"
                                                              for e in review))
    db.notify("circular", "warning" if review else "info", f"Карта обновлена по циркуляру {ref}",
              "\n".join(lines) or "Изменений в перечне зон не обнаружено.")

    reason = f"Причина: вступил в силу циркуляр {ref}."
    n = await asyncio.to_thread(alerts.reevaluate_all, reason)
    log.info("Активирован %s, зон: %d, уведомлений по судам: %d", ref, len(fc["features"]), n)
    return {"ok": True, "message": f"{ref} активирован, зон: {len(fc['features'])}", "ref": ref,
            "needs_review": len(review)}


async def bootstrap() -> None:
    """Первый запуск: если зон ещё нет — берём встроенный JWLA-035, затем сверяемся с сайтом."""
    if zones.active_ref():
        return
    active = db.query_one("SELECT ref FROM circulars WHERE status='active'")
    if active and (config.ZONES_DIR / f"{active['ref']}.geojson").exists():
        zones.set_active(json.loads((config.ZONES_DIR / f"{active['ref']}.geojson").read_text(encoding="utf-8")))
        return
    seed = config.STATIC_DIR.parent / "seed" / "JWLA-035.txt"
    if seed.exists():
        log.info("Первый запуск: строю зоны из встроенного JWLA-035")
        try:
            await ingest_document(seed.read_bytes(), "JWLA-035.txt", source_url="seed")
        except Exception:  # noqa: BLE001
            log.exception("Не удалось построить стартовые зоны")
