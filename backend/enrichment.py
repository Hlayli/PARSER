"""Обогащение карточки бизнеса фактами с его сайта.

Модуль обходит главную страницу и несколько служебных разделов одного
домена и сохраняет в БД только те значения, которые реально присутствуют
в HTML. Ничего не додумывается: нет данных на странице — нет факта.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import deque
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

import httpx
from bs4 import BeautifulSoup

from .db import (
    add_fact,
    add_location,
    connect,
    fill_empty_fields,
    get_lead,
    set_site_audit,
)
from . import site_audit, source_facts

logger = logging.getLogger(__name__)

MAX_PAGES = 6
MAX_ATTEMPTS = 20
PAGE_TIMEOUT = 10.0
TOTAL_BUDGET = 60.0
MAX_PHOTOS = 12
MAX_ERRORS = 20
MAX_HTML_BYTES = 3_000_000

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

CANDIDATE_PATHS = (
    "/contacts",
    "/contact",
    "/about",
    "/o-nas",
    "/price",
    "/prices",
    "/menu",
    "/uslugi",
    "/services",
)

# Признаки служебных страниц в URL и тексте ссылки.
PATH_HINTS = (
    "contact",
    "kontakt",
    "about",
    "o-nas",
    "onas",
    "o-kompanii",
    "price",
    "prices",
    "ceny",
    "tseny",
    "menu",
    "uslug",
    "service",
)
TEXT_HINTS = (
    "контакт",
    "о нас",
    "о компании",
    "прайс",
    "цен",
    "меню",
    "услуг",
    "about",
    "contact",
    "price",
    "menu",
    "service",
)

SOCIAL_HOSTS = {"vk.com", "t.me", "wa.me", "ok.ru"}
SHARE_MARKERS = ("share", "sharer", "intent", "/send")

BAD_EMAIL_TLDS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "svg",
    "webp",
    "bmp",
    "ico",
    "css",
    "js",
}

_SP = " \t\u00a0\u202f"

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

RE_PHONE_RU = re.compile(
    r"(?<![\d+])(?:\+7|8|7)"
    r"[ \t\u00a0\u202f\-()]*\d{3}"
    r"[ \t\u00a0\u202f\-()]*\d{3}"
    r"[ \t\u00a0\u202f\-]*\d{2}"
    r"[ \t\u00a0\u202f\-]*\d{2}(?!\d)"
)

RE_PHONE_INTL = re.compile(
    r"(?<![\d+])\+[ \t\u00a0\u202f\-()]*\d"
    r"(?:[ \t\u00a0\u202f\-()]*\d){8,14}(?!\d)"
)

RE_DIRECTOR = re.compile(
    r"(?<![А-Яа-яЁё])"
    r"(?:(?i:генеральный\s+директор|директор|руководитель|основатель|ИП))"
    r"\s*[:\-—]?\s*"
    r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ][а-яё]+(?:\s+[А-ЯЁ][а-яё]+)?)"
)

RE_INN = re.compile(r"ИНН\D{0,10}(\d{12}|\d{10})(?!\d)", re.IGNORECASE)

RE_PRICE_RUB = re.compile(
    r"(?:от\s+)?(?:\d{1,3}(?:[ \u00a0\u202f]\d{3})+|\d{1,7})"
    r"(?:[.,]\d{1,2})?\s*(?:₽|руб(?:лей|ля|\.)?|р\.)",
    re.IGNORECASE,
)
RE_PRICE_USD = re.compile(
    r"(?:\$\s*\d{1,7}(?:[.,]\d{1,2})?|\d{1,7}(?:[.,]\d{1,2})?\s*\$)"
)

_DAY = (
    r"(?:пн|вт|ср|чт|пт|сб|вс|"
    r"понедельник|вторник|среда|четверг|пятница|суббота|воскресенье)\.?"
)
RE_HOURS_DAYS = re.compile(
    rf"{_DAY}"
    rf"(?:\s*[-–—]\s*{_DAY})?"
    rf"(?:\s*,?\s*{_DAY})*"
    r"\s*[:\-–—]?\s*(?:с\s*)?\d{1,2}[:.]\d{2}"
    r"\s*(?:[-–—]|до)\s*\d{1,2}[:.]\d{2}",
    re.IGNORECASE,
)
RE_HOURS_EVERYDAY = re.compile(
    r"(?:ежедневно|без выходных|круглосуточно)"
    r"(?:\s*,?\s*(?:с\s*)?\d{1,2}[:.]\d{2}"
    r"\s*(?:[-–—]|до)\s*\d{1,2}[:.]\d{2})?",
    re.IGNORECASE,
)
RE_HOURS_RANGE = re.compile(
    r"\bс\s*\d{1,2}[:.]\d{2}\s*до\s*\d{1,2}[:.]\d{2}\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _collapse(value: str) -> str:
    """Схлопывает любые пробелы (включая NBSP) в один обычный пробел."""
    return " ".join(value.replace("\u00a0", " ").replace("\u202f", " ").split())


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _valid_phone(value: str) -> bool:
    digits = _digits(value)
    return 10 <= len(digits) <= 15


def _phone_key(value: str) -> str:
    digits = _digits(value)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return digits


def _uniq(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _dedupe_substrings(values: list[str]) -> list[str]:
    """Убирает короткие фразы, целиком входящие в более длинные."""
    unique = sorted(dict.fromkeys(values), key=len, reverse=True)
    kept: list[str] = []
    for value in unique:
        if any(value in longer for longer in kept):
            continue
        kept.append(value)
    return kept


def _strip_fragment(url: str) -> str:
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _page_key(url: str) -> tuple[str, str, str]:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    return (host, parts.path or "/", parts.query)


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _same_site(candidate: str, root: str) -> bool:
    host = _host(candidate)
    return bool(host) and host == _host(root)


def _is_http_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


def _absolute_image(raw: str, base_url: str) -> str:
    raw = (raw or "").strip()
    if not raw or raw.lower().startswith(("data:", "blob:", "javascript:")):
        return ""
    absolute = urljoin(base_url, raw)
    if not _is_http_url(absolute):
        return ""
    return _strip_fragment(absolute)


def _image_key(url: str) -> str:
    """Канонический ключ изображения для дедупликации.

    Сайты нередко отдают одно и то же фото через imgproxy/ресайзер: у ссылок
    разные подписи и размеры, а исходный файл один. Если внутри ссылки зашит
    исходный URL (``.../https%3A%2F%2F...``) — берём его. Иначе оставляем
    ссылку как есть, чтобы не склеить разные фото с query-параметрами.
    """
    text = str(url or "").strip()
    if not text:
        return ""
    marker = re.search(r"https?%3a%2f%2f", text, re.IGNORECASE)
    if marker:
        embedded = unquote(text[marker.start():])
        if _is_http_url(embedded):
            return _strip_fragment(embedded)
    return _strip_fragment(text)


def _dimension(value: object) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    if not match:
        return None
    return int(match.group(0))


def _visible_text(soup: BeautifulSoup) -> str:
    for tag in soup.find_all(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    raw = soup.get_text("\n", strip=True)
    lines = [_collapse(line) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def _phone_values(soup: BeautifulSoup, text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = _collapse(value).strip(" ,;")
        if not _valid_phone(value):
            return
        key = _phone_key(value)
        if key in seen:
            return
        seen.add(key)
        values.append(value)

    # tel: ссылки приоритетнее обычного текста.
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href.lower().startswith("tel:"):
            continue
        raw = unquote(href[4:].split(";")[0]).strip()
        label = _collapse(anchor.get_text(" ", strip=True))
        add(label if _valid_phone(label) else raw)

    for match in RE_PHONE_RU.finditer(text):
        add(match.group(0))
    for match in RE_PHONE_INTL.finditer(text):
        add(match.group(0))

    return values


def _email_values(soup: BeautifulSoup, text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        value = unquote(value).strip().strip(".,;:")
        if not RE_EMAIL.fullmatch(value):
            return
        lowered = value.lower()
        if lowered.rsplit(".", 1)[-1] in BAD_EMAIL_TLDS:
            return
        if lowered in seen:
            return
        seen.add(lowered)
        values.append(value)

    # mailto: ссылки приоритетнее обычного текста.
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if href.lower().startswith("mailto:"):
            add(href[7:].split("?")[0])

    for match in RE_EMAIL.finditer(text):
        add(match.group(0))

    return values


def _social_values(soup: BeautifulSoup) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        # Только уже абсолютные ссылки: относительные для фактов не достраиваем.
        if not href.lower().startswith(("http://", "https://")):
            continue
        try:
            parts = urlsplit(href)
        except ValueError:
            continue
        host = (parts.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in SOCIAL_HOSTS:
            continue
        path_and_query = (parts.path + "?" + parts.query).lower()
        if any(marker in path_and_query for marker in SHARE_MARKERS):
            continue
        if host == "wa.me" and "text=" in parts.query.lower():
            continue
        url = urlunsplit(
            (parts.scheme.lower(), parts.netloc, parts.path, parts.query, "")
        )
        if url in seen:
            continue
        seen.add(url)
        values.append(url)

    return values


def _photo_values(soup: BeautifulSoup, page_url: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()

    def add(raw: object, *, absolute_only: bool = False) -> None:
        raw_text = str(raw or "").strip()
        if not raw_text:
            return
        if absolute_only and not raw_text.lower().startswith(("http://", "https://")):
            return
        url = _absolute_image(raw_text, page_url)
        key = _image_key(url)
        if not url or not key or key in seen:
            return
        seen.add(key)
        values.append(url)

    for meta in soup.find_all("meta"):
        key = str(meta.get("property") or meta.get("name") or "").strip().lower()
        if key in ("og:image", "og:image:url", "og:image:secure_url",
                   "twitter:image", "twitter:image:src"):
            add(meta.get("content"))

    for image in soup.find_all("img"):
        src = image.get("src")
        if not src:
            continue
        width = _dimension(image.get("width"))
        height = _dimension(image.get("height"))
        dimensions = [value for value in (width, height) if value is not None]
        if dimensions and max(dimensions) < 400:
            continue
        add(src, absolute_only=True)

    return values[:MAX_PHOTOS]


def _clip_fragment(sentence: str, match: re.Match[str], limit: int = 200) -> str:
    if len(sentence) <= limit:
        return sentence.strip()
    start = max(0, match.start() - 60)
    end = min(len(sentence), start + limit)
    if end < match.end():
        end = min(len(sentence), match.end())
        start = max(0, end - limit)
    fragment = sentence[start:end]
    if start > 0 and " " in fragment:
        fragment = fragment.split(" ", 1)[1]
    if end < len(sentence) and " " in fragment:
        fragment = fragment.rsplit(" ", 1)[0]
    return fragment.strip()


def _price_values(text: str) -> list[str]:
    values: list[str] = []
    for block in re.split(r"[\r\n]+", text):
        block = _collapse(block)
        if not block:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", block):
            for pattern in (RE_PRICE_RUB, RE_PRICE_USD):
                for match in pattern.finditer(sentence):
                    fragment = _clip_fragment(sentence, match)
                    if fragment and fragment not in values:
                        values.append(fragment)
    return values


def _director_values(text: str) -> list[str]:
    values: list[str] = []
    for match in RE_DIRECTOR.finditer(text):
        value = _collapse(match.group(0)).strip(" .,;:")
        if value and value not in values:
            values.append(value)
    return values


def _inn_values(text: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for match in RE_INN.finditer(text):
        digits = match.group(1)
        if digits in seen:
            continue
        seen.add(digits)
        value = _collapse(match.group(0)).strip()
        if value:
            values.append(value)
    return values


def _hours_values(text: str) -> list[str]:
    found: list[str] = []
    for pattern in (RE_HOURS_DAYS, RE_HOURS_EVERYDAY, RE_HOURS_RANGE):
        for match in pattern.finditer(text):
            value = _collapse(match.group(0)).strip(" .,;:")
            if value:
                found.append(value)
    return _dedupe_substrings(found)


def _description_value(soup: BeautifulSoup) -> str:
    for meta in soup.find_all("meta"):
        name = str(meta.get("name") or "").strip().lower()
        if name == "description":
            content = _collapse(str(meta.get("content") or ""))
            if content:
                return content[:300].strip()

    for paragraph in soup.find_all("p"):
        if paragraph.find_parent(["nav", "footer"]):
            continue
        value = _collapse(paragraph.get_text(" ", strip=True))
        if len(value) < 40:
            continue
        lowered = value.lower()
        if lowered.startswith("©") or "cookie" in lowered or "куки" in lowered:
            continue
        if "политика конфиденциальности" in lowered:
            continue
        if not re.search(r"[A-Za-zА-Яа-яЁё]", value):
            continue
        return value[:300].strip()

    return ""


def _extract_page(soup: BeautifulSoup, page_url: str) -> dict[str, list[str]]:
    """Извлекает факты одной страницы. Возвращает kind -> список значений."""
    text = _visible_text(soup)

    return {
        "phone": _phone_values(soup, text),
        "email": _email_values(soup, text),
        "social": _social_values(soup),
        "photo": _photo_values(soup, page_url),
        "price": _price_values(text),
        "director": _director_values(text),
        "inn": _inn_values(text),
        "description": _uniq([_description_value(soup)]),
        "hours": _hours_values(text),
    }


def _discover_links(soup: BeautifulSoup, page_url: str, root_url: str) -> list[str]:
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.lower().startswith(
            ("#", "mailto:", "tel:", "javascript:", "data:", "blob:")
        ):
            continue
        absolute = _strip_fragment(urljoin(page_url, href))
        if not _is_http_url(absolute) or not _same_site(absolute, root_url):
            continue
        path = urlsplit(absolute).path.lower()
        anchor_text = _collapse(anchor.get_text(" ", strip=True)).lower()
        if (
            any(hint in path for hint in PATH_HINTS)
            or any(hint in anchor_text for hint in TEXT_HINTS)
        ):
            links.append(absolute)
    return _uniq(links)


def _load_existing_facts(
    conn, business_id: str
) -> tuple[set[tuple[str, str]], set[str], set[str]]:
    rows = conn.execute(
        "SELECT kind, value FROM fact WHERE business_id = ?", (business_id,)
    ).fetchall()
    facts: set[tuple[str, str]] = set()
    phone_keys: set[str] = set()
    photo_keys: set[str] = set()
    for row in rows:
        kind = str(row["kind"])
        value = str(row["value"])
        facts.add((kind, value))
        if kind == "phone":
            phone_keys.add(_phone_key(value))
        elif kind == "photo":
            photo_keys.add(_image_key(value))
    return facts, phone_keys, photo_keys


def _merge_photos(conn, lead: dict, business_id: str, photos: list[str]) -> None:
    current: list[str] = []
    lead_photos = lead.get("photos")
    if isinstance(lead_photos, list):
        current = [str(item).strip() for item in lead_photos if str(item).strip()]
    else:
        raw = lead.get("photos_json")
        if raw:
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                logger.warning("photos_json не является корректным JSON: %s", business_id)
                parsed = []
            if isinstance(parsed, list):
                current = [str(item).strip() for item in parsed if str(item).strip()]

    merged: list[str] = []
    seen: set[str] = set()
    for url in current + photos:
        url = url.strip()
        key = _image_key(url)
        if not url or not key or key in seen:
            continue
        seen.add(key)
        merged.append(url)
        if len(merged) >= MAX_PHOTOS:
            break

    if merged == current:
        return

    conn.execute(
        "UPDATE business SET photos_json = ? WHERE id = ?",
        (json.dumps(merged, ensure_ascii=False), business_id),
    )
    conn.commit()


def _note_error(errors: list[str], message: str) -> None:
    if len(errors) < MAX_ERRORS:
        errors.append(message)


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def _apply_source_payloads(
    conn,
    business_id: str,
    remember,
) -> dict:
    """Собрать контакты и адреса из сохранённых payload'ов 2ГИС/Яндекса.

    Ничего не додумываем: берём только явные поля источников. Заполняем
    исключительно пустые поля бизнеса, чтобы не затереть ручные правки.
    """
    found = {
        "website": "",
        "phone": "",
        "hours": "",
        "rating": "",
        "reviews_count": None,
        "category": "",
        "address": "",
        "lat": None,
        "lon": None,
        "locations_added": 0,
    }

    rows = conn.execute(
        "SELECT source, source_id, url, payload_json FROM business_source "
        "WHERE business_id = ? ORDER BY rowid",
        (business_id,),
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        try:
            data = source_facts.extract_source_data(payload, str(row["source"]))
        except Exception:  # noqa: BLE001 — один битый payload не ломает обогащение
            logger.exception("не удалось разобрать payload %s", business_id)
            continue

        source_url = str(payload.get("url") or row["url"] or "")
        for kind in ("phone", "email", "social", "hours", "description"):
            for value in data.get(kind, []):
                remember(kind, str(value), source_url)

        if not found["website"] and data.get("website"):
            found["website"] = str(data["website"])
        if not found["phone"] and data.get("phone"):
            found["phone"] = str(data["phone"][0])
        if not found["hours"] and data.get("hours"):
            found["hours"] = str(data["hours"][0])
        if not found["rating"] and data.get("rating"):
            found["rating"] = str(data["rating"])
        if found["reviews_count"] is None and data.get("reviews_count") is not None:
            found["reviews_count"] = data["reviews_count"]
        if not found["category"] and data.get("category"):
            found["category"] = str(data["category"])

        address = str(data.get("address") or "").strip()
        if address:
            if not found["address"]:
                found["address"] = address
            if found["lat"] is None and data.get("lat") is not None:
                found["lat"] = data["lat"]
            if found["lon"] is None and data.get("lon") is not None:
                found["lon"] = data["lon"]
            try:
                added_location = add_location(
                    conn,
                    business_id,
                    str(row["source"]),
                    str(row["source_id"]),
                    address,
                    phone=str((data.get("phone") or [""])[0]),
                    lat=data.get("lat"),
                    lon=data.get("lon"),
                    url=str(payload.get("url") or row["url"] or ""),
                )
            except Exception:  # noqa: BLE001
                logger.exception("не удалось сохранить точку %s", business_id)
            else:
                if added_location:
                    found["locations_added"] += 1

    values = {
        "website": found["website"],
        "phone": found["phone"],
        "hours": found["hours"],
        "rating": found["rating"],
        "reviews_count": found["reviews_count"],
        "category": found["category"],
        "address": found["address"],
        "lat": found["lat"],
        "lon": found["lon"],
    }
    fill_empty_fields(conn, business_id, values)
    return found


async def enrich(business_id: str) -> dict:
    """Собрать факты из сохранённых источников и с сайта бизнеса.

    Сначала используются payload'ы 2ГИС/Яндекса (контакты, соцсети, адреса),
    затем обходится сайт, а его техническое состояние сохраняется для карточки.
    Возвращает словарь с ``status``, ``message``, ``added`` и ``errors``.
    """
    added: dict[str, int] = {}
    errors: list[str] = []

    conn = connect()
    try:
        lead = get_lead(conn, business_id)
        if not lead:
            return {
                "status": "error",
                "message": "Организация не найдена.",
                "added": added,
                "errors": ["организация не найдена"],
            }

        seen_facts, seen_phone_keys, seen_photo_keys = _load_existing_facts(
            conn, business_id
        )

        def remember(kind: str, value: str, source_url: str) -> bool:
            value = _collapse(value).strip()
            if not value:
                return False
            key = (kind, value)
            if key in seen_facts:
                return False
            if kind == "phone":
                phone_key = _phone_key(value)
                if phone_key in seen_phone_keys:
                    return False
                seen_phone_keys.add(phone_key)
            elif kind == "photo":
                photo_key = _image_key(value)
                if not photo_key or photo_key in seen_photo_keys:
                    return False
                if len(seen_photo_keys) >= MAX_PHOTOS:
                    return False
                seen_photo_keys.add(photo_key)
            inserted = add_fact(
                conn, business_id, kind, value, source_url=source_url
            )
            # core-реализация возвращает bool; None считаем успехом (контракт).
            if inserted is False:
                return False
            seen_facts.add(key)
            added[kind] = added.get(kind, 0) + 1
            return True

        # 1. Контакты и адреса из уже сохранённых источников — без сети.
        payload_found = _apply_source_payloads(conn, business_id, remember)
        lead = get_lead(conn, business_id) or lead

        website = str(lead.get("website") or "").strip()
        if not _is_http_url(website):
            set_site_audit(conn, business_id, {})
            socials = [
                item["value"] for item in (lead.get("facts") or {}).get("social", [])
            ]
            if added or payload_found["locations_added"]:
                message = "Сайта нет — собрали контакты и данные из источников."
            else:
                message = (
                    "Сайта нет, и новых данных в источниках не нашлось. "
                    "Можно работать по телефону из карточки."
                )
            if socials:
                message += f" Найдены соцсети: {len(socials)}."
            return {
                "status": "no_site",
                "message": message,
                "website": "",
                "site_audit": None,
                "added": added,
                "errors": errors,
            }

        if site_audit.is_social_url(website):
            # Вместо сайта указана соцсеть: обходить нечего, но ссылка полезна.
            remember("social", website, website)
            set_site_audit(conn, business_id, {})
            message = (
                "Вместо сайта указана соцсеть — обходить нечего. "
                "Собрали контакты из источников; сайт можно предложить как развитие."
            )
            return {
                "status": "social_only",
                "message": message,
                "website": website,
                "site_audit": None,
                "added": added,
                "errors": errors,
            }

        queue: deque[str] = deque()
        queued: set[tuple[str, str, str]] = set()

        def push(url: str) -> None:
            cleaned = _strip_fragment(url)
            if not _is_http_url(cleaned) or not _same_site(cleaned, website):
                return
            key = _page_key(cleaned)
            if key in queued:
                return
            queued.add(key)
            queue.append(cleaned)

        push(website)
        for path in CANDIDATE_PATHS:
            push(urljoin(website, path))

        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,*/*;q=0.8"
            ),
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        }

        deadline = time.monotonic() + TOTAL_BUDGET
        processed = 0
        attempts = 0
        discovered_photos: list[str] = []
        audit_main: dict | None = None
        audit_fallback: dict | None = None
        audit_error = ""
        main_key = _page_key(_strip_fragment(website))

        async with httpx.AsyncClient(
            timeout=PAGE_TIMEOUT,
            follow_redirects=True,
            verify=False,
            headers=headers,
        ) as client:
            while queue and processed < MAX_PAGES and attempts < MAX_ATTEMPTS:
                remaining = deadline - time.monotonic()
                if remaining <= 0.5:
                    _note_error(errors, "бюджет времени исчерпан")
                    break

                url = queue.popleft()
                attempts += 1
                timeout = min(PAGE_TIMEOUT, remaining)

                try:
                    started = time.monotonic()
                    response = await asyncio.wait_for(client.get(url), timeout=timeout)
                except (asyncio.TimeoutError, httpx.TimeoutException):
                    if attempts == 1 and not audit_error:
                        audit_error = "таймаут"
                    _note_error(errors, f"{url}: таймаут")
                    logger.info("таймаут при загрузке %s", url)
                    continue
                except httpx.HTTPError as exc:
                    if attempts == 1 and not audit_error:
                        audit_error = exc.__class__.__name__
                    _note_error(errors, f"{url}: {exc.__class__.__name__}")
                    logger.info("ошибка загрузки %s: %s", url, exc)
                    continue
                except Exception:
                    if attempts == 1 and not audit_error:
                        audit_error = "ошибка загрузки"
                    _note_error(errors, f"{url}: ошибка загрузки")
                    logger.exception("неожиданная ошибка загрузки %s", url)
                    continue

                if response.status_code in (404, 410):
                    logger.debug("страница не найдена, пропускаю: %s", url)
                    continue
                if response.status_code >= 400:
                    if attempts == 1 and not audit_error:
                        audit_error = f"HTTP {response.status_code}"
                    _note_error(errors, f"{url}: HTTP {response.status_code}")
                    logger.info("HTTP %s для %s", response.status_code, url)
                    continue

                final_url = _strip_fragment(str(response.url))
                if not _same_site(final_url, website):
                    logger.info("редирект на чужой домен, пропускаю: %s", final_url)
                    continue

                content_type = response.headers.get("content-type", "").lower()
                if content_type and "html" not in content_type and "xml" not in content_type:
                    if attempts == 1 and not audit_error:
                        audit_error = f"не HTML ({content_type})"
                    logger.debug("не HTML, пропускаю: %s (%s)", final_url, content_type)
                    continue
                if len(response.content) > MAX_HTML_BYTES:
                    if attempts == 1 and not audit_error:
                        audit_error = "страница слишком большая"
                    _note_error(errors, f"{final_url}: страница слишком большая")
                    logger.info("страница больше %s байт: %s", MAX_HTML_BYTES, final_url)
                    continue

                if audit_main is None:
                    try:
                        page_audit = site_audit.audit_page(
                            response.text,
                            website,
                            status=response.status_code,
                            elapsed_ms=int((time.monotonic() - started) * 1000),
                            final_url=final_url,
                            headers=dict(response.headers),
                        )
                    except Exception:  # noqa: BLE001 — аудит не должен ломать обход
                        logger.exception("не удалось построить аудит %s", final_url)
                    else:
                        if _page_key(final_url) == main_key:
                            audit_main = page_audit
                        elif audit_fallback is None:
                            audit_fallback = page_audit

                try:
                    soup = BeautifulSoup(response.content, "html.parser")
                    page_url = final_url
                    page_data = _extract_page(soup, page_url)
                    page_photos = page_data["photo"]
                    page_links = _discover_links(soup, page_url, website)
                except Exception:
                    _note_error(errors, f"{final_url}: ошибка разбора")
                    logger.exception("не удалось разобрать страницу %s", final_url)
                    continue

                processed += 1
                logger.info("обработана страница %s", page_url)

                for kind in (
                    "phone",
                    "email",
                    "social",
                    "director",
                    "inn",
                    "hours",
                    "description",
                    "price",
                    "photo",
                ):
                    for value in page_data.get(kind, []):
                        remember(kind, value, page_url)

                for photo in page_photos:
                    if photo not in discovered_photos:
                        discovered_photos.append(photo)

                if page_photos:
                    _merge_photos(conn, lead, business_id, discovered_photos)

                conn.commit()

                for link in page_links:
                    if len(queue) < MAX_ATTEMPTS:
                        push(link)

        audit = audit_main or audit_fallback
        if audit is None:
            audit = site_audit.empty_audit(
                website, error=audit_error or "сайт не открылся"
            )
        set_site_audit(conn, business_id, audit)

        total_added = sum(added.values()) + payload_found["locations_added"]
        if processed:
            message = (
                f"Сайт проверен, добавлено фактов: {total_added}."
                if total_added
                else "Сайт проверен, новых данных не найдено."
            )
        else:
            message = (
                "Сайт не открылся — сохранили отметку о недоступности. "
                "Контакты из источников собраны."
            )

        logger.info(
            "обогащение %s завершено: страниц=%s, фактов=%s, ошибок=%s",
            business_id,
            processed,
            total_added,
            len(errors),
        )
        return {
            "status": "ok" if processed else "error",
            "message": message,
            "website": website,
            "site_audit": audit,
            "added": added,
            "errors": errors,
        }
    finally:
        conn.close()
