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

from . import dedup
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
    name_key TEXT NOT NULL DEFAULT '',
    compact_key TEXT NOT NULL DEFAULT '',
    brand_key TEXT NOT NULL DEFAULT '',
    address_key TEXT NOT NULL DEFAULT '',
    phone_keys TEXT NOT NULL DEFAULT '',
    site_key TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS business_location (
    business_id TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    address TEXT NOT NULL DEFAULT '',
    address_key TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    lat REAL,
    lon REAL,
    url TEXT NOT NULL DEFAULT '',
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
"""

# Индексы создаём после миграции: в старой БД колонок *_key ещё нет.
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_business_phone ON business(phone_norm);
CREATE INDEX IF NOT EXISTS idx_business_status ON business(status);
CREATE INDEX IF NOT EXISTS idx_business_name ON business(name);
CREATE INDEX IF NOT EXISTS idx_business_name_key ON business(name_key);
CREATE INDEX IF NOT EXISTS idx_business_compact_key ON business(compact_key);
CREATE INDEX IF NOT EXISTS idx_business_brand_key ON business(brand_key);
CREATE INDEX IF NOT EXISTS idx_business_address_key ON business(address_key);
CREATE INDEX IF NOT EXISTS idx_business_site_key ON business(site_key);
CREATE INDEX IF NOT EXISTS idx_fact_business ON fact(business_id, kind);
CREATE INDEX IF NOT EXISTS idx_cell_job ON scan_cell(job_id, status);
CREATE INDEX IF NOT EXISTS idx_bsource ON business_source(business_id);
CREATE INDEX IF NOT EXISTS idx_location_business ON business_location(business_id);
CREATE INDEX IF NOT EXISTS idx_location_address ON business_location(address);
CREATE INDEX IF NOT EXISTS idx_location_address_key ON business_location(address_key);
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
        conn.executescript(INDEXES)
        backfill_keys(conn)
        backfill_locations(conn)


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
    business_columns = {row["name"] for row in conn.execute("PRAGMA table_info(business)")}
    for column in ("name_key", "compact_key", "brand_key", "address_key", "phone_keys", "site_key"):
        if column not in business_columns:
            conn.execute(
                f"ALTER TABLE business ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
            )
    location_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(business_location)")
    }
    if "address_key" not in location_columns:
        conn.execute(
            "ALTER TABLE business_location ADD COLUMN address_key TEXT NOT NULL DEFAULT ''"
        )
    conn.commit()


# --- нормализация -----------------------------------------------------------

def normalize_phone(raw: str) -> str:
    """E.164-подобная нормализация. См. :func:`backend.dedup.normalize_phone`."""
    return dedup.normalize_phone(raw)


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    return dedup.haversine(lat1, lon1, lat2, lon2)


def _norm_name(name: str) -> str:
    return dedup.normalize_name(name)


def make_id(source: str, source_id: str) -> str:
    return hashlib.sha1(f"{source}:{source_id}".encode()).hexdigest()[:16]


def _keys_for(place: Place) -> dict[str, str]:
    """Нормализованные ключи для поиска и сравнения дубликатов."""
    phones = dedup.split_phones(place.phone)
    return {
        "name_key": dedup.normalize_name(place.name),
        "compact_key": dedup.compact_name(place.name),
        "brand_key": dedup.brand_key(place.name),
        "address_key": dedup.address_key(place.address),
        "phone_keys": "".join(f"|{phone}" for phone in phones) + ("|" if phones else ""),
        "site_key": dedup.site_key(place.website),
    }


def backfill_keys(conn: sqlite3.Connection) -> int:
    """Пересчитать ключи у старых записей. Возвращает число обновлённых строк."""
    rows = conn.execute(
        "SELECT id, name, address, phone, website FROM business "
        "WHERE name_key = '' OR compact_key = '' OR address_key = '' "
        "OR (name != '' AND brand_key = '') "
        "OR (phone != '' AND phone_keys = '') OR (website != '' AND site_key = '')"
    ).fetchall()
    for row in rows:
        phones = dedup.split_phones(row["phone"])
        conn.execute(
            "UPDATE business SET name_key = ?, compact_key = ?, brand_key = ?, "
            "address_key = ?, phone_keys = ?, site_key = ? WHERE id = ?",
            (
                dedup.normalize_name(row["name"]),
                dedup.compact_name(row["name"]),
                dedup.brand_key(row["name"]),
                dedup.address_key(row["address"]),
                "".join(f"|{phone}" for phone in phones) + ("|" if phones else ""),
                dedup.site_key(row["website"]),
                row["id"],
            ),
        )
    if rows:
        conn.commit()
    return len(rows)


def _refresh_keys(conn: sqlite3.Connection, business_id: str) -> None:
    row = conn.execute(
        "SELECT id, name, address, phone, website, phone_keys FROM business WHERE id = ?",
        (business_id,),
    ).fetchone()
    if not row:
        return
    # Старые номера сохраняем: по ним тоже надо узнавать дубликаты.
    phones = set(dedup.split_phones(row["phone"]))
    phones.update(p for p in str(row["phone_keys"] or "").split("|") if p)
    conn.execute(
        "UPDATE business SET name_key = ?, compact_key = ?, brand_key = ?, "
        "address_key = ?, phone_keys = ?, site_key = ? WHERE id = ?",
        (
            dedup.normalize_name(row["name"]),
            dedup.compact_name(row["name"]),
            dedup.brand_key(row["name"]),
            dedup.address_key(row["address"]),
            "".join(f"|{phone}" for phone in sorted(phones)) + ("|" if phones else ""),
            dedup.site_key(row["website"]),
            business_id,
        ),
    )


# --- запись -----------------------------------------------------------------

def _candidates(conn: sqlite3.Connection, place: Place) -> list[sqlite3.Row]:
    """Кандидаты для сравнения: совпадение по id, названию, адресу или телефону."""
    keys = _keys_for(place)
    where: list[str] = ["id = ?"]
    params: list[object] = [make_id(place.source, place.source_id)]

    if keys["name_key"]:
        where.append("name_key = ?")
        params.append(keys["name_key"])
    if keys["compact_key"]:
        where.append("compact_key LIKE ?")
        params.append(keys["compact_key"][:5] + "%")
    if keys["address_key"]:
        where.append("address_key = ?")
        params.append(keys["address_key"])
    if keys["brand_key"]:
        where.append("(brand_key = ? AND is_demo = ?)")
        params.extend([keys["brand_key"], 1 if place.source == "demo" else 0])
    for phone in dedup.split_phones(place.phone):
        where.append("(phone_norm = ? OR phone_keys LIKE ?)")
        params.extend([phone, f"%|{phone}|%"])

    return conn.execute(
        "SELECT * FROM business WHERE " + " OR ".join(where) + " LIMIT 80",
        params,
    ).fetchall()


def _find_existing(conn: sqlite3.Connection, place: Place) -> str | None:
    """Найти карточку организации: id записи, сеть, затем та же точка.

    Сначала ищем бренд: у сети должна быть одна карточка, даже если точки
    в разных районах. Если название родовое («Кофейня»), сеть не определяем
    и склеиваем только одну и ту же точку по адресу/телефону/расстоянию.
    """
    rows = _candidates(conn, place)
    if not rows:
        return None

    source_id = make_id(place.source, place.source_id)
    for row in rows:
        if row["id"] == source_id:
            # Та же запись источника, просто повторно пришла.
            return row["id"]

    brand = dedup.brand_key(place.name)
    if brand:
        for row in rows:
            if row["brand_key"] == brand and row["is_demo"] == (1 if place.source == "demo" else 0):
                return row["id"]

    match = dedup.best_match(place, rows)
    return match[0]["id"] if match else None


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
            _refresh_keys(conn, existing)

        _link_source(conn, existing, place)
        _link_location(conn, existing, place)
        conn.commit()
        return existing, False

    bid = make_id(place.source, place.source_id)
    # коллизия по id при другом источнике — добавим суффикс
    while conn.execute("SELECT 1 FROM business WHERE id = ?", (bid,)).fetchone():
        bid = hashlib.sha1(f"{bid}:{place.source_id}:{place.name}".encode()).hexdigest()[:16]

    keys = _keys_for(place)
    conn.execute(
        """INSERT INTO business (id, source, source_id, name, category, address, lat, lon,
               phone, phone_norm, website, rating, reviews_count, price_range, hours,
               photos_json, url, status, is_favorite, note, is_demo, first_seen, updated_at,
               name_key, compact_key, brand_key, address_key, phone_keys, site_key)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'new', 0, '', ?, ?, ?,
                   ?,?,?,?,?,?)""",
        (
            bid, place.source, place.source_id, place.name, place.category, place.address,
            place.lat, place.lon, place.phone, normalize_phone(place.phone), place.website,
            place.rating, place.reviews_count, place.price_range, place.hours,
            json.dumps(place.photos, ensure_ascii=False), place.url,
            1 if place.source == "demo" else 0, ts, ts,
            keys["name_key"], keys["compact_key"], keys["brand_key"],
            keys["address_key"], keys["phone_keys"], keys["site_key"],
        ),
    )
    _link_source(conn, bid, place)
    _link_location(conn, bid, place)
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


def _link_location(conn: sqlite3.Connection, business_id: str, place: Place) -> None:
    """Запомнить точку сети: адрес, телефон и координаты конкретного филиала."""
    conn.execute(
        """INSERT INTO business_location
               (business_id, source, source_id, address, address_key, phone, lat, lon, url)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(business_id, source, source_id) DO UPDATE SET
               address = excluded.address, address_key = excluded.address_key,
               phone = excluded.phone, lat = excluded.lat, lon = excluded.lon,
               url = excluded.url""",
        (business_id, place.source, place.source_id, place.address,
         dedup.address_key(place.address), place.phone,
         place.lat, place.lon, place.url),
    )


def backfill_locations(conn: sqlite3.Connection) -> int:
    """Для старых карточек создать точку из самой записи."""
    rows = conn.execute(
        """SELECT b.* FROM business b WHERE NOT EXISTS (
               SELECT 1 FROM business_location l
               WHERE l.business_id = b.id AND l.source = b.source
                 AND l.source_id = b.source_id)"""
    ).fetchall()
    for row in rows:
        conn.execute(
            """INSERT OR REPLACE INTO business_location
                   (business_id, source, source_id, address, address_key, phone, lat, lon, url)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["source"], row["source_id"], row["address"],
             dedup.address_key(row["address"]), row["phone"],
             row["lat"], row["lon"], row["url"]),
        )
    pending = conn.execute(
        "SELECT business_id, source, source_id, address FROM business_location "
        "WHERE address_key = '' AND address != ''"
    ).fetchall()
    for row in pending:
        conn.execute(
            "UPDATE business_location SET address_key = ? "
            "WHERE business_id = ? AND source = ? AND source_id = ?",
            (dedup.address_key(row["address"]), row["business_id"],
             row["source"], row["source_id"]),
        )
    if rows or pending:
        conn.commit()
    return len(rows)


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
    locations_count = conn.execute(
        "SELECT COUNT(DISTINCT CASE WHEN address_key != '' THEN address_key "
        "ELSE '#' || rowid END) c FROM business_location WHERE business_id = ?",
        (row["id"],),
    ).fetchone()["c"] or 1
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
        "locations_count": locations_count,
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
            "OR lower_ru(b.phone) LIKE ? OR lower_ru(b.category) LIKE ? "
            "OR b.id IN (SELECT business_id FROM business_location "
            "WHERE lower_ru(address) LIKE ?))"
        )
        like = f"%{q.lower()}%"
        params.extend([like, like, like, like, like])
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
    # Один и тот же адрес из 2ГИС и Яндекса — одна точка: склеиваем строки,
    # но сохраняем оба источника.
    locations: list[dict] = []
    for r in conn.execute(
        "SELECT address, address_key, phone, lat, lon, source, url "
        "FROM business_location WHERE business_id = ? ORDER BY address_key, address, source",
        (business_id,),
    ):
        merged = None
        if r["address_key"]:
            for location in locations:
                if location["_key"] != r["address_key"]:
                    continue
                phone_a = dedup.normalize_phone(location["phone"])
                phone_b = dedup.normalize_phone(r["phone"])
                if not phone_a or not phone_b or phone_a == phone_b:
                    merged = location
                    break
        if merged is None:
            merged = {
                "_key": r["address_key"],
                "address": r["address"],
                "phone": r["phone"],
                "lat": r["lat"],
                "lon": r["lon"],
                "sources": [r["source"]],
                "url": r["url"],
            }
            locations.append(merged)
            continue
        if not merged["phone"] and r["phone"]:
            merged["phone"] = r["phone"]
        if not merged["url"] and r["url"]:
            merged["url"] = r["url"]
        if merged["lat"] is None and r["lat"] is not None:
            merged["lat"], merged["lon"] = r["lat"], r["lon"]
        if r["source"] not in merged["sources"]:
            merged["sources"].append(r["source"])
    for location in locations:
        location.pop("_key", None)
    lead["locations"] = locations
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


# --- склейка дубликатов -----------------------------------------------------


def _pick_keeper(rows: list[sqlite3.Row]) -> sqlite3.Row:
    """Какую карточку оставить: с ручной работой, затем с более полными данными."""

    def rank(row: sqlite3.Row) -> tuple[int, int, int]:
        manual = 0
        if row["status"] != "new":
            manual += 100
        if row["is_favorite"]:
            manual += 50
        if user_note_clean(row["note"]):
            manual += 20
        fields = sum(
            1
            for field in ("phone", "website", "address", "rating", "hours", "price_range")
            if row[field] not in (None, "", 0)
        )
        if row["photos_json"] not in ("", "[]", None):
            fields += 1
        return manual, fields, int(row["is_favorite"] or 0)

    return max(rows, key=rank)


def user_note_clean(note: object) -> str:
    return str(note or "").strip()


def _merge_rows(conn: sqlite3.Connection, keep_id: str, drop_id: str) -> None:
    """Перенести всё ценное из drop_id в keep_id и удалить дубль."""
    keep = conn.execute("SELECT * FROM business WHERE id = ?", (keep_id,)).fetchone()
    drop = conn.execute("SELECT * FROM business WHERE id = ?", (drop_id,)).fetchone()
    if not keep or not drop:
        return

    updates: dict[str, object] = {}
    for field in (
        "name", "category", "address", "phone", "website", "price_range",
        "hours", "url", "rating", "reviews_count", "lat", "lon",
    ):
        if keep[field] in (None, "", 0) and drop[field] not in (None, "", 0):
            updates[field] = drop[field]

    if drop["is_favorite"] and not keep["is_favorite"]:
        updates["is_favorite"] = 1
    if keep["status"] == "new" and drop["status"] != "new":
        updates["status"] = drop["status"]

    notes: list[str] = []
    for note in (keep["note"], drop["note"]):
        note = user_note_clean(note)
        if note and note not in notes:
            notes.append(note)
    if notes and " | ".join(notes) != user_note_clean(keep["note"]):
        updates["note"] = " | ".join(notes)[:2000]

    try:
        drop_photos = json.loads(drop["photos_json"] or "[]")
    except ValueError:
        drop_photos = []
    if not isinstance(drop_photos, list):
        drop_photos = []
    try:
        keep_photos = json.loads(keep["photos_json"] or "[]")
    except ValueError:
        keep_photos = []
    photos = _merge_photos(keep["photos_json"], drop_photos)
    if photos != keep_photos:
        updates["photos_json"] = json.dumps(photos, ensure_ascii=False)

    updates["is_demo"] = 1 if (keep["is_demo"] and drop["is_demo"]) else 0
    updates["first_seen"] = min(keep["first_seen"] or now(), drop["first_seen"] or now())
    updates["updated_at"] = now()

    sets = ", ".join(f"{field} = ?" for field in updates)
    conn.execute(f"UPDATE business SET {sets} WHERE id = ?", (*updates.values(), keep_id))

    for source in conn.execute(
        "SELECT source, source_id, url, payload_json FROM business_source WHERE business_id = ?",
        (drop_id,),
    ):
        conn.execute(
            """INSERT OR IGNORE INTO business_source
                   (business_id, source, source_id, url, payload_json)
               VALUES (?,?,?,?,?)""",
            (keep_id, source["source"], source["source_id"], source["url"],
             source["payload_json"]),
        )
    conn.execute("DELETE FROM business_source WHERE business_id = ?", (drop_id,))

    for fact in conn.execute(
        "SELECT kind, value, source_url, fetched_at FROM fact WHERE business_id = ?",
        (drop_id,),
    ):
        exists = conn.execute(
            "SELECT 1 FROM fact WHERE business_id = ? AND kind = ? AND value = ? LIMIT 1",
            (keep_id, fact["kind"], fact["value"]),
        ).fetchone()
        if not exists:
            conn.execute(
                "INSERT INTO fact (business_id, kind, value, source_url, fetched_at) "
                "VALUES (?,?,?,?,?)",
                (keep_id, fact["kind"], fact["value"], fact["source_url"],
                 fact["fetched_at"]),
            )
    conn.execute("DELETE FROM fact WHERE business_id = ?", (drop_id,))

    # Точки сети: адреса и телефоны филиалов переезжают в общую карточку.
    has_location = conn.execute(
        "SELECT 1 FROM business_location WHERE business_id = ? AND source = ? "
        "AND source_id = ?",
        (drop_id, drop["source"], drop["source_id"]),
    ).fetchone()
    if not has_location:
        conn.execute(
            """INSERT OR IGNORE INTO business_location
                   (business_id, source, source_id, address, address_key, phone, lat, lon, url)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (keep_id, drop["source"], drop["source_id"], drop["address"],
             dedup.address_key(drop["address"]), drop["phone"],
             drop["lat"], drop["lon"], drop["url"]),
        )
    for location in conn.execute(
        "SELECT source, source_id, address, address_key, phone, lat, lon, url "
        "FROM business_location WHERE business_id = ?", (drop_id,)
    ):
        conn.execute(
            """INSERT OR IGNORE INTO business_location
                   (business_id, source, source_id, address, address_key, phone, lat, lon, url)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (keep_id, location["source"], location["source_id"], location["address"],
             location["address_key"] or dedup.address_key(location["address"]),
             location["phone"], location["lat"], location["lon"], location["url"]),
        )
    conn.execute("DELETE FROM business_location WHERE business_id = ?", (drop_id,))

    conn.execute("DELETE FROM business WHERE id = ?", (drop_id,))
    _refresh_keys(conn, keep_id)


def _row_candidates(conn: sqlite3.Connection, row: sqlite3.Row) -> list[sqlite3.Row]:
    where: list[str] = []
    params: list[object] = []
    if row["name_key"]:
        where.append("name_key = ?")
        params.append(row["name_key"])
    if row["compact_key"]:
        where.append("compact_key LIKE ?")
        params.append(row["compact_key"][:5] + "%")
    if row["brand_key"]:
        where.append("(brand_key = ? AND is_demo = ?)")
        params.extend([row["brand_key"], row["is_demo"]])
    if row["address_key"]:
        where.append("address_key = ?")
        params.append(row["address_key"])
    for phone in str(row["phone_keys"] or "").split("|"):
        if phone:
            where.append("phone_keys LIKE ?")
            params.append(f"%|{phone}|%")
    if not where:
        return []
    return conn.execute(
        f"SELECT * FROM business WHERE ({' OR '.join(where)}) AND id != ? LIMIT 80",
        (*params, row["id"]),
    ).fetchall()


def _pair_match(left: sqlite3.Row, right: sqlite3.Row) -> dedup.Match | None:
    """Дубликат ли это: одна точка или один бренд сети."""
    match = dedup.compare(
        dedup.Candidate.from_row(left), dedup.Candidate.from_row(right)
    )
    if match.is_duplicate:
        return match
    brand = dedup.brand_key(left["name"])
    if (
        brand
        and brand == dedup.brand_key(right["name"])
        and left["is_demo"] == right["is_demo"]
    ):
        return dedup.Match(score=100, reasons=("сеть",))
    return None


def find_duplicate_groups(conn: sqlite3.Connection) -> list[dict]:
    """Найти группы дубликатов в существующей базе (без изменений)."""
    rows = conn.execute("SELECT * FROM business ORDER BY first_seen, id").fetchall()
    by_id = {row["id"]: row for row in rows}
    index: dict[str, int] = {}
    groups: list[list[str]] = []
    seen_pairs: set[tuple[str, str]] = set()

    for row in rows:
        for other in _row_candidates(conn, row):
            pair = tuple(sorted((row["id"], other["id"])))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            match = _pair_match(row, other)
            if match is None:
                continue
            left, right = index.get(row["id"]), index.get(other["id"])
            if left is not None and right is not None:
                continue  # не склеиваем группы транзитивно
            if left is None and right is None:
                index[row["id"]] = index[other["id"]] = len(groups)
                groups.append([row["id"], other["id"]])
                continue
            base_id = groups[left][0] if left is not None else groups[right][0]
            base = by_id[base_id]
            incoming = other if left is not None else row
            if _pair_match(base, incoming) is None:
                continue
            target = left if left is not None else right
            index[incoming["id"]] = target
            groups[target].append(incoming["id"])

    report: list[dict] = []
    for members in groups:
        group_rows = [by_id[business_id] for business_id in members]
        keeper = _pick_keeper(group_rows)
        dropped = [row for row in group_rows if row["id"] != keeper["id"]]
        if not dropped:
            continue
        match = _pair_match(keeper, dropped[0])
        report.append(
            {
                "keep": dict(keeper),
                "drop": [dict(row) for row in dropped],
                "score": match.score,
                "reasons": match.reasons,
            }
        )
    return report


def merge_duplicate_groups(conn: sqlite3.Connection) -> int:
    """Склеить найденные дубликаты. Возвращает число удалённых карточек."""
    removed = 0
    for group in find_duplicate_groups(conn):
        keep_id = group["keep"]["id"]
        for drop in group["drop"]:
            _merge_rows(conn, keep_id, str(drop["id"]))
            removed += 1
        conn.commit()
    return removed


def backup_database(destination: Path) -> Path:
    """Согласованная копия базы (SQLite backup API) — перед склейкой дублей."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with connect() as source:
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
    return destination
