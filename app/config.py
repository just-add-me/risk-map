"""Настройки приложения. Всё берётся из переменных окружения (файл .env)."""
import os
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


DATA_DIR = Path(_env("DATA_DIR", "./data")).resolve()
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# --- Доступ ---------------------------------------------------------------
ADMIN_USER = _env("ADMIN_USER", "admin")
ADMIN_PASSWORD = _env("ADMIN_PASSWORD", "")          # обязателен в проде
SECRET_KEY = _env("SECRET_KEY", "")                  # если пусто — сгенерируем и сохраним в data/
COOKIE_SECURE = _env("COOKIE_SECURE", "0") == "1"    # 1, когда сайт работает по HTTPS

# --- AIS ------------------------------------------------------------------
AISSTREAM_API_KEY = _env("AISSTREAM_API_KEY")
# demo — симулятор движения судов; aisstream — живые данные aisstream.io
AIS_MODE = _env("AIS_MODE") or ("aisstream" if AISSTREAM_API_KEY else "demo")
AISSTREAM_URL = _env("AISSTREAM_URL", "wss://stream.aisstream.io/v0/stream")
DEMO_SPEED = float(_env("DEMO_SPEED", "40"))         # во сколько раз ускорено время в демо
DEMO_TICK_SECONDS = float(_env("DEMO_TICK_SECONDS", "5"))

# --- Алерты ---------------------------------------------------------------
WARN_DISTANCE_NM = float(_env("WARN_DISTANCE_NM", "50"))   # «приближается к зоне» ближе этого
EXIT_HYSTERESIS_NM = float(_env("EXIT_HYSTERESIS_NM", "0.3"))
STALE_AIS_HOURS = float(_env("STALE_AIS_HOURS", "24"))     # «нет данных AIS» дольше этого
TRAIL_HOURS = float(_env("TRAIL_HOURS", "72"))             # сколько хранить трек

# --- Циркуляры регулятора -------------------------------------------------
JWC_PAGE_URL = _env("JWC_PAGE_URL", "https://lmalloyds.com/committee/joint-war-committee/")
CIRCULAR_CHECK_HOURS = float(_env("CIRCULAR_CHECK_HOURS", "12"))   # 2 раза в сутки
HTTP_USER_AGENT = _env(
    "HTTP_USER_AGENT",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36",
)

# --- Геоданные ------------------------------------------------------------
NE_COUNTRIES_URL = _env(
    "NE_COUNTRIES_URL",
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/"
    "ne_10m_admin_0_countries.geojson",
)

DB_PATH = DATA_DIR / "app.db"
CIRCULARS_DIR = DATA_DIR / "circulars"
ZONES_DIR = DATA_DIR / "zones"
GEODATA_DIR = DATA_DIR / "geodata"

for _d in (DATA_DIR, CIRCULARS_DIR, ZONES_DIR, GEODATA_DIR):
    _d.mkdir(parents=True, exist_ok=True)
