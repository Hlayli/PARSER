"""Слой хранения: SQLite, дедупликация, выборки.

Один файл БД, WAL, без ORM. Соединение открывается на операцию — так проще
переживать конкурентные задачи сканера.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .models import Place

DB_PATH = Path(os.environ.get("PARSER_DB", Path(__file__).resolve().parent.parent / "data" / "parser.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS business (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    phone TEXT NOT NULL DEFAULT '',
    phone_norm TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    rating REAL,
    reviews_count INTEGER,
    price_range TEXT NOT NULL DEFAULT '',
    hours TEXT NOT NULL DEFAULT '',
    photos_json TEXT NOT NULL DEFAULT '[]',
    url TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new',
    is_favorite INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    is_demo INTEGER NOT NULL DEFAULT 0,
    first_seen TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS business_source (
    business_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (business_id, source, source_id)
);

CREATE TABLE IF NOT EXISTS fact (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    business_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    value TEXT NOT NULL,
    source_url TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_job (
    id TEXT PRIMARY KEY,
    query TEXT NOT NULL,
    queries_json TEXT NOT NULL DEFAULT '[]',
    query_index INTEGER NOT NULL DEFAULT 0,
    city TEXT NOT NULL DEFAULT '',
    bbox_json TEXT NOT NULL,
    cell_deg REAL NOT NULL,
    sources_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    cells_total INTEGER NOT NULL DEFAULT 0,
    cells_done INTEGER NOT NULL DEFAULT 0,
    leads_found INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_cell (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL,
    ix INTEGER NOT NULL,
    iy INTEGER NOT NULL,
    bbox_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    found INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_business_phone ON business(phone_norm);
CREATE INDEX IF NOT EXISTS idx_business_status ON business(status);
CREATE INDEX IF NOT EXISTS idx_business_name ON business(name);
CREATE INDEX IF NOT EXISTS idx_fact_business ON fact(business_id, kind);
CREATE INDEX IF NOT EXISTS idx_cell_job ON scan_cell(job_id, status);
CREATE INDEX IF NOT EXISTS idx_bsource ON business_source(business_id);
"""

STATUSES = ("new", "in_progress", "called", "rejected")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # SQLite lower()/LIKE понимают регистр только для ASCII. Для русских
    # названий регистронезависимый поиск делаем своей функцией.
    conn.create_function("lower_ru", 1, lambda value: str(value or "").lower())
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """Добавить недостающие колонки в старую БД (без потери данных)."""
    columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(scan_job)")
    }
    if "city" not in columns:
        conn.execute("ALTER TABLE scan_job ADD COLUMN city TEXT NOT NULL DEFAULT ''")
    if "queries_json" not in columns:
        conn.execute(
            "ALTER TABLE scan_job ADD COLUMN queries_json TEXT NOT NULL DEFAULT '[]'"
        )
    if "query_index" not in columns:
        conn.execute(
            "ALTER TABLE scan_job ADD COLUMN query_index INTEGER NOT NULL DEFAULT 0"
        )
    conn.commit()


# --- нормализация -----------------------------------------------------------

def normalize_phone(raw: str) -> str:
    """E.164-подобная нормализация для РФ/СНГ. Пустая строка, если не похоже на телефон."""
    if not raw:
        return ""
    raw = re.split(r"[;,/|]", str(raw))[0]
    digits = re.sub(r"\D", "", raw)
    if len(digits) < 10:
        return ""
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10:
        return "+7" + digits
    if len(digits) == 11 and digits.startswith("7"):
        return "+7" + digits[1:]
    return "+" + digits


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _norm_name(name: str) -> str:
    n = name.lower().replace("ё", "е")
    n = re.sub(r"[«»\"'`]", "", n)
    n = re.sub(r"\b(ооо|оао|зао|ип|ао|пао|нко)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def make_id(source: str, source_id: str) -> str:
    return hashlib.sha1(f"{source}:{source_id}".encode()).hexdigest()[:16]


# --- запись -----------------------------------------------------------------

def _find_existing(conn: sqlite3.Connection, place: Place) -> str | None:
    """Дедуп: телефон → имя + близость 250 м."""
    phone_norm = normalize_phone(place.phone)
    if phone_norm:
        row = conn.execute(
            "SELECT id FROM business WHERE phone_norm = ? LIMIT 1", (phone_norm,)
        ).fetchone()
        if row:
            return row["id"]

    name = _norm_name(place.name)
    if not name:
        return None

    rows = conn.execute(
        "SELECT id, name, lat, lon FROM business WHERE name IS NOT NULL AND name != '' "
        "AND lower_ru(replace(name,'ё','е')) LIKE ? LIMIT 50",
        (f"%{name[:24]}%",),
    ).fetchall()

    for row in rows:
        if _norm_name(row["name"]) != name:
            continue
        # Без координат склейка по имени опасна: «Кофейня» в разных районах
        # может быть разными бизнесами. Телефонный дедуп выше уже отработал.
        if place.lat is None or place.lon is None or row["lat"] is None or row["lon"] is None:
            continue
        if haversine(place.lat, place.lon, row["lat"], row["lon"]) <= 250:
            return row["id"]
    return None


def upsert_place(conn: sqlite3.Connection, place: Place) -> tuple[str, bool]:
    """Вставить или дополнить организацию. Возвращает (id, is_new)."""
    if place.is_empty():
        raise ValueError("place.name пустой")

    ts = now()
    existing = _find_existing(conn, place)

    if existing:
        row = conn.execute("SELECT * FROM business WHERE id = ?", (existing,)).fetchone()
        updates: dict[str, object] = {}
        pairs = {
            "category": place.category,
            "address": place.address,
            "phone": place.phone,
            "website": place.website,
            "price_range": place.price_range,
            "hours": place.hours,
            "url": place.url,
            "rating": place.rating,
            "reviews_count": place.reviews_count,
            "lat": place.lat,
            "lon": place.lon,
        }
        for field, value in pairs.items():
            if value in (None, "", 0) and field not in ("rating", "reviews_count"):
                continue
            if value is None:
                continue
            current = row[field]
            if current in (None, "", 0):
                updates[field] = value

        if place.phone:
            updates["phone_norm"] = normalize_phone(place.phone)
        if place.photos:
            merged = _merge_photos(row["photos_json"], place.photos)
            updates["photos_json"] = json.dumps(merged, ensure_ascii=False)

        if updates:
            updates["updated_at"] = ts
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(f"UPDATE business SET {sets} WHERE id = ?", (*updates.values(), existing))

        _link_source(conn, existing, place)
        conn.commit()
        return existing, False

    bid = make_id(place.source, place.source_id)
    # коллизия по id при другом источнике — добавим суффикс
    while conn.execute("SELECT 1 FROM business WHERE id = ?", (bid,)).fetchone():
        bid = hashlib.sha1(f"{bid}:{place.source_id}:{place.name}".encode()).hexdigest()[:16]

    conn.execute(
        """INSERT INTO business (id, source, source_id, name, category, address, lat, lon,
               phone, phone_norm, website, rating, reviews_count, price_range, hours,
               photos_json, url, status, is_favorite, note, is_demo, first_seen, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new', 0, '', ?, ?, ?)""",
        (
            bid, place.source, place.source_id, place.name, place.category, place.address,
            place.lat, place.lon, place.phone, normalize_phone(place.phone), place.website,
            place.rating, place.reviews_count, place.price_range, place.hours,
            json.dumps(place.photos, ensure_ascii=False), place.url,
            1 if place.source == "demo" else 0, ts, ts,
        ),
    )
    _link_source(conn, bid, place)
    conn.commit()
    return bid, True


def _link_source(conn: sqlite3.Connection, business_id: str, place: Place) -> None:
    conn.execute(
        """INSERT INTO business_source (business_id, source, source_id, url, payload_json)
           VALUES (?,?,?,?,?)
           ON CONFLICT(business_id, source, source_id) DO UPDATE SET
               url = excluded.url, payload_json = excluded.payload_json""",
        (business_id, place.source, place.source_id, place.url,
         json.dumps(place.raw, ensure_ascii=False)[:20000]),
    )


def _merge_photos(current_json: str, new_photos: list[str]) -> list[str]:
    try:
        current = json.loads(current_json or "[]")
        if not isinstance(current, list):
            current = []
    except (ValueError, TypeError):
        current = []
    seen = set(current)
    for url in new_photos:
        if url and url not in seen and len(current) < 12:
            current.append(url)
            seen.add(url)
    return current


# --- чтение -----------------------------------------------------------------

def _lead_row(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    sources = [
        r["source"] for r in conn.execute(
            "SELECT DISTINCT source FROM business_source WHERE business_id = ?", (row["id"],)
        )
    ]
    try:
        photos = json.loads(row["photos_json"] or "[]")
    except ValueError:
        photos = []
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "address": row["address"],
        "phone": row["phone"],
        "website": row["website"],
        "rating": row["rating"],
        "reviews_count": row["reviews_count"],
        "price_range": row["price_range"],
        "hours": row["hours"],
        "source": row["source"],
        "sources": sorted(sources) or [row["source"]],
        "status": row["status"],
        "is_favorite": row["is_favorite"],
        "is_demo": row["is_demo"],
        "note": row["note"],
        "has_site": bool(row["website"]),
        "photo": photos[0] if photos else "",
        "lat": row["lat"],
        "lon": row["lon"],
        "first_seen": row["first_seen"],
        "updated_at": row["updated_at"],
    }


def list_leads(
    conn: sqlite3.Connection,
    status: str | None = None,
    source: str | None = None,
    q: str | None = None,
    fav: bool = False,
    sort: str = "new",
    limit: int = 50,
    offset: int = 0,
) -> tuple[int, list[dict]]:
    where, params = [], []
    if status:
        where.append("b.status = ?")
        params.append(status)
    if fav:
        where.append("b.is_favorite = 1")
    if q:
        where.append(
            "(lower_ru(b.name) LIKE ? OR lower_ru(b.address) LIKE ? "
            "OR lower_ru(b.phone) LIKE ? OR lower_ru(b.category) LIKE ?)"
        )
        like = f"%{q.lower()}%"
        params.extend([like, like, like, like])
    if source:
        where.append("b.id IN (SELECT business_id FROM business_source WHERE source = ?)")
        params.append(source)

    clause = ("WHERE " + " AND ".join(where)) if where else ""
    order = {
        "new": "b.updated_at DESC",
        "rating": "b.rating DESC NULLS LAST, b.updated_at DESC",
        "name": "lower_ru(b.name) ASC",
    }.get(sort, "b.updated_at DESC")

    total = conn.execute(f"SELECT COUNT(*) AS c FROM business b {clause}", params).fetchone()["c"]
    rows = conn.execute(
        f"SELECT * FROM business b {clause} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, max(1, min(limit, 500)), max(0, offset)),
    ).fetchall()
    return total, [_lead_row(conn, r) for r in rows]


def get_lead(conn: sqlite3.Connection, business_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM business WHERE id = ?", (business_id,)).fetchone()
    if not row:
        return None
    lead = _lead_row(conn, row)
    try:
        lead["photos"] = json.loads(row["photos_json"] or "[]")
    except ValueError:
        lead["photos"] = []

    facts: dict[str, list[dict]] = {}
    for f in conn.execute(
        "SELECT kind, value, source_url, fetched_at FROM fact WHERE business_id = ? "
        "ORDER BY kind, fetched_at DESC", (business_id,)
    ):
        facts.setdefault(f["kind"], []).append(
            {"value": f["value"], "source_url": f["source_url"], "fetched_at": f["fetched_at"]}
        )
    lead["facts"] = facts
    lead["source_links"] = [
        {"source": r["source"], "url": r["url"]} for r in conn.execute(
            "SELECT source, url FROM business_source WHERE business_id = ?", (business_id,)
        )
    ]
    return lead


def set_status(
    conn: sqlite3.Connection,
    business_id: str,
    status: str | None = None,
    is_favorite: bool | None = None,
    note: str | None = None,
) -> bool:
    sets, params = [], []
    if status is not None:
        if status not in STATUSES:
            raise ValueError(f"недопустимый статус: {status}")
        sets.append("status = ?")
        params.append(status)
    if is_favorite is not None:
        sets.append("is_favorite = ?")
        params.append(1 if is_favorite else 0)
    if note is not None:
        sets.append("note = ?")
        params.append(note[:2000])
    if not sets:
        return False
    sets.append("updated_at = ?")
    params.extend([now(), business_id])
    cur = conn.execute(f"UPDATE business SET {', '.join(sets)} WHERE id = ?", params)
    conn.commit()
    return cur.rowcount > 0


def add_fact(conn: sqlite3.Connection, business_id: str, kind: str, value: str,
             source_url: str) -> bool:
    """Добавить факт, если такого ещё нет. Возвращает True, если добавлен."""
    value = (value or "").strip()
    if not value:
        return False
    exists = conn.execute(
        "SELECT 1 FROM fact WHERE business_id = ? AND kind = ? AND value = ? LIMIT 1",
        (business_id, kind, value),
    ).fetchone()
    if exists:
        return False
    conn.execute(
        "INSERT INTO fact (business_id, kind, value, source_url, fetched_at) VALUES (?,?,?,?,?)",
        (business_id, kind, value, source_url, now()),
    )
    conn.commit()
    return True


def stats(conn: sqlite3.Connection) -> dict:
    total = conn.execute("SELECT COUNT(*) c FROM business").fetchone()["c"]
    by_status = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM business GROUP BY status")}
    by_source = {r["source"]: r["c"] for r in conn.execute(
        "SELECT source, COUNT(*) c FROM business GROUP BY source")}
    favorites = conn.execute("SELECT COUNT(*) c FROM business WHERE is_favorite = 1").fetchone()["c"]
    with_site = conn.execute("SELECT COUNT(*) c FROM business WHERE website != ''").fetchone()["c"]
    return {
        "total": total,
        "by_status": by_status,
        "by_source": by_source,
        "favorites": favorites,
        "with_site": with_site,
        "no_site": total - with_site,
    }
