"""Геокодирование города через Nominatim (OpenStreetMap).

Результат кэшируется в памяти на один час, чтобы не дёргать публичный
сервис на каждый запуск сканирования.
"""

from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "PARSER/1.0 (local lead research tool)"
CACHE_TTL = 3600.0
REQUEST_TIMEOUT = 10.0

BBox = tuple[float, float, float, float]

# key: нормализованное название города -> (время получения, bbox, display_name)
_CACHE: dict[str, tuple[float, BBox, str]] = {}


def _cache_key(city: str) -> str:
    return " ".join(city.replace("ё", "е").strip().lower().split())


def _parse_bbox(item: dict) -> BBox:
    raw = item.get("boundingbox")
    if not isinstance(raw, list) or len(raw) != 4:
        raise ValueError("город не найден")
    try:
        south, north, west, east = (float(value) for value in raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("город не найден") from exc

    # Nominatim: [min_lat, max_lat, min_lon, max_lon].
    if not (west < east and south < north):
        raise ValueError("город не найден")
    return (west, south, east, north)


async def city_bbox(city: str) -> tuple[BBox, str]:
    """Возвращает ``((min_lon, min_lat, max_lon, max_lat), display_name)``."""
    if not city or not city.strip():
        raise ValueError("город не найден")

    key = _cache_key(city)
    cached = _CACHE.get(key)
    if cached is not None:
        fetched_at, bbox, display_name = cached
        if time.monotonic() - fetched_at < CACHE_TTL:
            logger.debug("bbox из кэша для %r", city)
            return bbox, display_name
        _CACHE.pop(key, None)

    params = {
        "q": city.strip(),
        "format": "jsonv2",
        "limit": 1,
        "accept-language": "ru",
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "ru",
    }

    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = await client.get(NOMINATIM_URL, params=params)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Nominatim недоступен для %r: %s", city, exc)
        raise ValueError("город не найден") from exc

    if not isinstance(payload, list) or not payload:
        logger.info("Nominatim не нашёл город %r", city)
        raise ValueError("город не найден")

    item = payload[0]
    if not isinstance(item, dict):
        raise ValueError("город не найден")

    try:
        bbox = _parse_bbox(item)
    except ValueError:
        logger.info("Nominatim вернул результат без bbox для %r", city)
        raise

    display_name = str(item.get("display_name") or "").strip()
    if not display_name:
        raise ValueError("город не найден")

    _CACHE[key] = (time.monotonic(), bbox, display_name)
    logger.info("bbox для %r: %s (%s)", city, bbox, display_name)
    return bbox, display_name
