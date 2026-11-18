"""Продажная аналитика карточки лида: оценка, зацепки, оффер и сообщение.

Модуль — чистая функция разметки: на входе словарь ``db.get_lead``, на выходе
готовый блок «с чем звонить». Никаких сетевых запросов, базы и чтения файлов:
только те данные, которые уже есть в карточке. Все строки — обычный текст.
"""

from __future__ import annotations

import html
import math
import re

SOCIAL_HOSTS = (
    "vk.com",
    "instagram.com",
    "t.me",
    "ok.ru",
    "wa.me",
    "youtube.com",
)

SPACE_RE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]*>")
NUMBER_RE = re.compile(r"-?\d+(?:[.,]\d+)?")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"']+", re.IGNORECASE)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE_DIGITS_RE = re.compile(r"\D")

# Порядок важен: «автосалон» должен стать авто-услугами, а не салоном
# красоты; «барбершоп» содержит «бар», поэтому короткие слова проверяются
# отдельным токеном.
OFFER_KEYWORDS = (
    (
        "auto",
        (
            "авто", "автосервис", "автомойк", "автосалон", "автошкол",
            "автозапчаст", "автотехцентр", "автоэлектрик", "шин", "ремонт",
            "строй", "мебел", "клининг", "юрист", "бухгал", "недвижим",
            "сервис",
        ),
    ),
    (
        "beauty",
        (
            "салон", "барбер", "космет", "ногт", "массаж", "спа", "фитнес",
            "йога", "стомат", "клиник",
        ),
    ),
    (
        "food",
        (
            "кофе", "кафе", "ресторан", "бар", "пекарн", "кондитер", "пицц",
            "суши", "столовая", "чай", "бургер", "шаурм", "еда",
        ),
    ),
    (
        "retail",
        (
            "магазин", "товар", "цвет", "зоо", "одежд", "обув", "техник",
            "продукт",
        ),
    ),
)

OFFERS = {
    "food": {
        "title": "Сайт с меню, адресами и онлайн-заказом",
        "bullets": [
            "Меню с ценами и фотографиями блюд",
            "Карта, адреса и часы работы",
            "Кнопки звонка и WhatsApp",
            "Онлайн-заказ или бронь столика",
            "Продвижение по поисковым запросам города",
        ],
    },
    "beauty": {
        "title": "Лендинг с онлайн-записью",
        "bullets": [
            "Онлайн-запись 24/7",
            "Услуги и цены",
            "Фото работ и мастеров",
            "Карта и контакты",
            "Напоминания о записи",
        ],
    },
    "auto": {
        "title": "Сайт-визитка с заявкой и прайсом",
        "bullets": [
            "Прайс на услуги и работы",
            "Форма заявки на расчёт",
            "Примеры выполненных работ",
            "Карта, телефон и WhatsApp",
            "Отзывы клиентов",
        ],
    },
    "retail": {
        "title": "Каталог с доставкой и самовывозом",
        "bullets": [
            "Каталог товаров с ценами",
            "Доставка и самовывоз",
            "Корзина и оформление заказа",
            "Карточки товаров с фото",
            "Контакты и карта",
        ],
    },
    "default": {
        "title": "Лендинг с контактами и формой заявки",
        "bullets": [
            "Первый экран с оффером и фото",
            "Услуги и цены",
            "Контакты и карта",
            "Форма заявки",
            "Кнопки звонка и WhatsApp",
        ],
    },
}


def _text(value: object) -> str:
    """Строка без лишних пробелов; сложные объекты не превращаем в мусор."""
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    text = str(value).replace("\u00a0", " ").replace("\u202f", " ")
    text = TAG_RE.sub(" ", html.unescape(text))
    return SPACE_RE.sub(" ", text).strip()


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    else:
        text = re.sub(r"(?<=\d)\s+(?=\d)", "", _text(value)).replace(",", ".")
        match = NUMBER_RE.search(text)
        if not match:
            return None
        try:
            result = float(match.group(0))
        except ValueError:
            return None
    return result if math.isfinite(result) else None


def _integer(value: object) -> int | None:
    result = _number(value)
    return None if result is None else int(result)


def _as_list(value: object) -> list[object]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _plural(value: int, one: str, two: str, many: str) -> str:
    value = abs(int(value)) % 100
    if 11 <= value <= 14:
        return many
    value %= 10
    if value == 1:
        return one
    if 2 <= value <= 4:
        return two
    return many


def _join_and(values: list[str]) -> str:
    if not values:
        return ""
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " и " + values[-1]


def _lower_first(value: str) -> str:
    value = _text(value)
    if len(value) > 1 and value[1].islower():
        return value[0].lower() + value[1:]
    return value


def _rating(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


def _yes_no(value: bool | None) -> str:
    if value is True:
        return "Да"
    if value is False:
        return "Нет"
    return ""


def _clip(value: str, limit: int) -> str:
    value = _text(value)
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _split_url(raw: object) -> tuple[str, str, str]:
    """(scheme, host, нормализованный_url). Пустой host — не URL."""
    text = _text(raw)
    if not text:
        return "", "", ""
    lowered = text.lower()
    explicit = lowered.startswith(("http://", "https://"))
    if explicit:
        scheme, rest = text.split("://", 1)
        scheme = scheme.lower()
    elif re.match(r"^[a-z][a-z0-9+.\-]*:", text, re.IGNORECASE):
        return "", "", ""  # mailto:, tel:, javascript:, ftp:
    else:
        scheme, rest = "https", text
    host = re.split(r"[/?#]", rest, maxsplit=1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if not host or " " in host:
        return "", "", ""
    if not explicit and "." not in host:
        return "", "", ""
    return scheme, host, f"{scheme}://{rest}"


def _is_social_host(host: str) -> bool:
    return any(host == item or host.endswith("." + item) for item in SOCIAL_HOSTS)


def _facts_map(lead: dict) -> dict[str, list[str]]:
    raw = lead.get("facts") if isinstance(lead, dict) else None
    result: dict[str, list[str]] = {}
    if not isinstance(raw, dict):
        return result
    for kind, items in raw.items():
        kind_text = _text(kind).lower()
        if not kind_text:
            continue
        for item in _as_list(items):
            if isinstance(item, dict):
                value = _text(item.get("value"))
            else:
                value = _text(item)
            if value:
                result.setdefault(kind_text, []).append(value)
    return result


def _first_phone(lead: dict, facts: dict[str, list[str]]) -> str:
    candidates = [_text(lead.get("phone"))] + facts.get("phone", [])
    for raw in candidates:
        raw = re.sub(r"^tel:\s*", "", raw, flags=re.IGNORECASE)
        digits = PHONE_DIGITS_RE.sub("", raw)
        if 10 <= len(digits) <= 15:
            return _text(raw)
    return ""


def _has_photos(lead: dict, facts: dict[str, list[str]]) -> bool:
    for value in _as_list(lead.get("photos")):
        if _text(value):
            return True
    if _text(lead.get("photo")):
        return True
    return bool(facts.get("photo"))


def _audit_sources(audit: object) -> list[dict]:
    if not isinstance(audit, dict):
        return []
    sources = [audit]
    for key in ("checks", "details", "results"):
        nested = audit.get(key)
        if isinstance(nested, dict):
            sources.append(nested)
    return sources


def _audit_raw(audit: object, *names: str) -> object:
    names = tuple(name.lower() for name in names)
    for source in _audit_sources(audit):
        for name in names:
            if name in source and source[name] is not None:
                return source[name]
    if isinstance(audit, dict):
        for key in ("checks", "details", "results"):
            nested = audit.get(key)
            if not isinstance(nested, list):
                continue
            for item in nested:
                if not isinstance(item, dict):
                    continue
                item_key = _text(item.get("key")).lower()
                if item_key in names:
                    return item.get("ok", item.get("value"))
    return None


def _audit_error(audit: object) -> str:
    raw = _audit_raw(audit, "error", "errors", "message")
    if isinstance(raw, (list, tuple)):
        for item in raw:
            text = _text(item.get("value") if isinstance(item, dict) else item)
            if text:
                return text
        return ""
    return _text(raw)


def _boolish(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return bool(value)
    text = _text(value).lower()
    if text in ("true", "1", "yes", "да", "есть", "ok"):
        return True
    if text in ("false", "0", "no", "нет"):
        return False
    if text in ("", "none", "null", "unknown", "неизвестно"):
        return None
    return True


def _text_flag(audit: object, *names: str) -> tuple[bool | None, str]:
    raw = _audit_raw(audit, *names)
    if raw is None:
        return None, ""
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return False, ""
        if stripped.lower() not in ("true", "false", "yes", "no", "да", "нет", "1", "0"):
            return True, _clip(stripped, 100)
    result = _boolish(raw)
    return result, _yes_no(result)


def _check(key: str, label: str, ok: bool | None, value: str = "") -> dict:
    return {"key": key, "label": label, "ok": ok, "value": _text(value)}


def _build_site_checks(website_scheme: str, audit: object) -> list[dict]:
    raw_https = _audit_raw(audit, "https", "has_https", "ssl", "is_https")
    if isinstance(raw_https, str) and raw_https.strip().lower() in ("http", "https"):
        https_ok = raw_https.strip().lower() == "https"
        https_value = raw_https.strip().upper()
    elif raw_https is None:
        https_ok = True if website_scheme == "https" else (
            False if website_scheme == "http" else None
        )
        https_value = _yes_no(https_ok)
    else:
        https_ok = _boolish(raw_https)
        https_value = _yes_no(https_ok)

    status_ok: bool | None = None
    status_value = ""
    error = _audit_error(audit)
    raw_status = _audit_raw(audit, "status", "status_code", "http_status")
    if error:
        status_ok, status_value = False, "Ошибка"
    elif raw_status is not None:
        code = _integer(raw_status)
        if code is not None:
            status_ok = 200 <= code < 300
            status_value = f"HTTP {code}"

    mobile_ok = _boolish(_audit_raw(audit, "has_viewport", "viewport", "mobile",
                                   "is_mobile", "has_mobile"))
    title_ok, title_value = _text_flag(audit, "title", "has_title", "page_title")
    description_ok, description_value = _text_flag(
        audit, "description", "has_description", "meta_description"
    )
    h1_ok, h1_value = _text_flag(audit, "h1", "has_h1")
    form_ok = _boolish(_audit_raw(audit, "form", "has_form", "has_lead_form",
                                 "lead_form"))
    booking_ok = _boolish(
        _audit_raw(audit, "booking", "has_booking", "online_booking",
                   "has_online_booking")
    )
    menu_ok = _boolish(_audit_raw(audit, "menu", "has_menu", "services"))
    prices_ok = _boolish(_audit_raw(audit, "prices", "has_prices", "price_list"))

    raw_elapsed = _audit_raw(audit, "elapsed_ms", "load_ms", "speed_ms",
                             "response_ms")
    speed_ok: bool | None = None
    speed_value = ""
    elapsed = _number(raw_elapsed)
    if elapsed is not None:
        speed_ok = elapsed <= 3000
        speed_value = (
            f"{elapsed / 1000:.1f} с".replace(".", ",") if elapsed >= 1000
            else f"{int(elapsed)} мс"
        )

    raw_size = _audit_raw(audit, "html_kb", "size_kb", "page_kb", "html_size_kb")
    size_ok: bool | None = None
    size_value = ""
    size_kb = _number(raw_size)
    if size_kb is not None:
        size_ok = size_kb <= 2048
        size_value = (
            f"{size_kb / 1024:.1f} МБ".replace(".", ",") if size_kb >= 1024
            else f"{int(size_kb)} КБ"
        )

    cms_value = _clip(_text(_audit_raw(audit, "cms", "platform", "generator")), 60)

    return [
        _check("https", "HTTPS", https_ok, https_value),
        _check("status", "Доступность", status_ok, status_value),
        _check("mobile", "Мобильная версия", mobile_ok, _yes_no(mobile_ok)),
        _check("title", "Заголовок", title_ok, title_value),
        _check("description", "Описание для поиска", description_ok,
               description_value),
        _check("h1", "Главный заголовок", h1_ok, h1_value),
        _check("form", "Форма заявки", form_ok, _yes_no(form_ok)),
        _check("booking", "Онлайн-запись/заказ", booking_ok, _yes_no(booking_ok)),
        _check("menu", "Меню/услуги", menu_ok, _yes_no(menu_ok)),
        _check("prices", "Цены", prices_ok, _yes_no(prices_ok)),
        _check("speed", "Скорость", speed_ok, speed_value),
        _check("size", "Размер страницы", size_ok, size_value),
        _check("cms", "Платформа", None, cms_value),
    ]


def _site_score(checks: list[dict]) -> int:
    lookup = {item["key"]: item["ok"] for item in checks}
    score = 40
    if lookup.get("https") is True:
        score += 10
    if lookup.get("mobile") is True:
        score += 10
    if lookup.get("title") is True:
        score += 10
    if lookup.get("description") is True:
        score += 10
    if lookup.get("booking") is True or lookup.get("form") is True:
        score += 10
    if lookup.get("menu") is True:
        score += 5
    if lookup.get("speed") is True:
        score += 5
    return max(0, min(100, score))


def _context(lead: object) -> dict:
    lead = lead if isinstance(lead, dict) else {}
    facts = _facts_map(lead)

    schedule = _text(lead.get("hours"))
    if not schedule:
        schedule = next(iter(facts.get("hours", [])), "")

    locations = _as_list(lead.get("locations"))
    stored_points = _integer(lead.get("locations_count")) or 0
    points = max(stored_points, len(locations), 1)

    scheme, host, url = _split_url(lead.get("website"))
    social_only = bool(host) and _is_social_host(host)
    has_site = bool(host) and not social_only
    audit = lead.get("site_audit")
    checks = _build_site_checks(scheme, audit) if has_site else []

    return {
        "facts": facts,
        "name": _clip(_text(lead.get("name")), 120),
        "category": _clip(_text(lead.get("category")), 120),
        "phone": _first_phone(lead, facts),
        "rating": _number(lead.get("rating")),
        "reviews": _integer(lead.get("reviews_count")),
        "hours": schedule,
        "points": points,
        "website": _text(lead.get("website")),
        "site_url": url,
        "social_only": social_only,
        "has_site": has_site,
        "audit": audit if isinstance(audit, dict) else None,
        "checks": checks,
        "has_photos": _has_photos(lead, facts),
    }


def _lead_score(ctx: dict) -> int:
    score = 30.0
    if not ctx["has_site"]:
        score += 30 if ctx["social_only"] else 38

    if ctx["points"] >= 5:
        score += 11
    elif ctx["points"] >= 2:
        score += 8

    rating = ctx["rating"]
    reviews = ctx["reviews"]
    if rating is not None and rating >= 4.5 and reviews is not None and reviews >= 50:
        score += 8
    if reviews is not None and reviews >= 20:
        score += 4
    if rating is not None and rating < 4.0:
        score -= 6
    if not ctx["hours"]:
        score += 3
    if not ctx["has_photos"]:
        score += 2
    if ctx["phone"]:
        score += 2

    if ctx["has_site"] and ctx["audit"] is not None:
        lookup = {item["key"]: item["ok"] for item in ctx["checks"]}
        audit_ok = _boolish(_audit_raw(ctx["audit"], "ok", "reachable", "success"))
        if lookup.get("https") is True:
            score -= 5
        if lookup.get("mobile") is True:
            score -= 5
        if lookup.get("title") is True:
            score -= 2
        if lookup.get("description") is True:
            score -= 3
        if lookup.get("booking") is True or lookup.get("form") is True:
            score -= 5
        if lookup.get("menu") is True:
            score -= 2
        if audit_ok is True:
            score -= 3
        strong = (
            audit_ok is True
            and lookup.get("https") is True
            and lookup.get("mobile") is True
            and lookup.get("description") is True
            and (lookup.get("booking") is True or lookup.get("form") is True)
        )
        if strong:
            score = min(score, 35)

    return max(5, min(98, int(round(score))))


def _gap(code: str, title: str, detail: str, weight: int) -> dict:
    return {
        "code": code,
        "title": title,
        "detail": detail,
        "kind": "gap",
        "weight": weight,
    }


def _strength(code: str, title: str, detail: str, weight: int) -> dict:
    return {
        "code": code,
        "title": title,
        "detail": detail,
        "kind": "strength",
        "weight": weight,
    }


def _build_gaps(ctx: dict) -> list[dict]:
    gaps: list[dict] = []
    facts = ctx["facts"]
    checks = {item["key"]: item["ok"] for item in ctx["checks"]}
    rating = ctx["rating"]
    reviews = ctx["reviews"]

    if not ctx["has_site"] and not ctx["social_only"]:
        gaps.append(_gap(
            "no_site", "Нет сайта",
            "Клиенты ищут меню, цены и адреса в картах и соцсетях, а не на вашем сайте.",
            40,
        ))
    if ctx["social_only"]:
        gaps.append(_gap(
            "social_only", "Вместо сайта — соцсеть",
            "Страница в соцсети не заменяет сайт: часть клиентов не видит там "
            "цены и адреса без входа.",
            34,
        ))
    if ctx["has_site"] and checks.get("https") is False:
        gaps.append(_gap(
            "site_http", "Нет HTTPS",
            "Сайт открывается по http — браузер может показывать предупреждение.",
            12,
        ))
    if ctx["has_site"] and checks.get("mobile") is False:
        gaps.append(_gap(
            "site_no_mobile", "Нет мобильной версии",
            "С телефона страница не подстраивается под экран.",
            12,
        ))
    if ctx["has_site"] and checks.get("description") is False:
        gaps.append(_gap(
            "site_no_description", "Нет описания для поиска",
            "В поисковой выдаче не видно, чем занимается бизнес.",
            8,
        ))
    if ctx["has_site"] and (
        (checks.get("booking") is False or checks.get("form") is False)
        and checks.get("booking") is not True and checks.get("form") is not True
    ):
        gaps.append(_gap(
            "site_no_booking", "Нет формы заявки",
            "Клиенту негде оставить заявку или записаться онлайн.",
            10,
        ))
    if ctx["has_site"] and (
        (checks.get("menu") is False or checks.get("prices") is False)
        and checks.get("menu") is not True and checks.get("prices") is not True
    ):
        gaps.append(_gap(
            "site_no_menu", "Нет меню или цен",
            "Меню, услуги и цены не собраны на сайте.",
            8,
        ))
    error = _audit_error(ctx["audit"])
    if ctx["has_site"] and error:
        gaps.append(_gap(
            "site_unreachable", "Сайт недоступен",
            f"Сайт не открывается: {_clip(error, 120)}.",
            20,
        ))
    if rating is not None and reviews is not None and reviews < 20:
        gaps.append(_gap(
            "few_reviews", "Мало отзывов",
            f"Рейтинг {_rating(rating)}, но отзывов всего {reviews}.",
            6,
        ))
    if rating is not None and rating < 4.0:
        gaps.append(_gap(
            "low_rating", "Низкий рейтинг",
            f"Рейтинг {_rating(rating)} — ниже 4,0.",
            6,
        ))
    if not ctx["hours"] and not facts.get("hours"):
        gaps.append(_gap(
            "no_hours", "Не указаны часы работы",
            "Клиенты не знают, когда можно позвонить.",
            4,
        ))
    if not ctx["has_photos"]:
        gaps.append(_gap(
            "no_photos", "Нет фотографий",
            "По карточке сложно оценить бизнес визуально.",
            3,
        ))
    return sorted(gaps, key=lambda item: item["weight"], reverse=True)


def _build_strengths(ctx: dict) -> list[dict]:
    strengths: list[dict] = []
    rating = ctx["rating"]
    reviews = ctx["reviews"]
    lookup = {item["key"]: item["ok"] for item in ctx["checks"]}

    if ctx["points"] >= 2:
        strengths.append(_strength(
            "chain", f"Сеть из {ctx['points']} точек",
            "Несколько точек можно показать на одной карте.",
            12,
        ))
    if rating is not None and rating >= 4.5 and reviews is not None and reviews >= 20:
        strengths.append(_strength(
            "high_rating", "Высокий рейтинг",
            f"Рейтинг {_rating(rating)} при "
            f"{reviews} {_plural(reviews, 'отзыве', 'отзывах', 'отзывах')}.",
            8,
        ))
    if ctx["phone"]:
        strengths.append(_strength(
            "has_phone", "Есть телефон",
            "Телефон указан в карточке — можно позвонить.",
            4,
        ))
    if ctx["has_site"]:
        audit_ok = _boolish(_audit_raw(ctx["audit"], "ok", "reachable", "success"))
        good_points = [
            lookup.get("https"), lookup.get("mobile"), lookup.get("title"),
            lookup.get("description"),
        ]
        booking_ok = lookup.get("booking") is True or lookup.get("form") is True
        if (
            ctx["audit"] is not None and audit_ok is True
            and sum(item is True for item in good_points) >= 3 and booking_ok
        ):
            passed = [
                label for key, label in (
                    ("https", "HTTPS"),
                    ("mobile", "мобильная версия"),
                    ("title", "заголовок"),
                    ("description", "описание"),
                )
                if lookup.get(key) is True
            ]
            if booking_ok:
                passed.append("заявка")
            strengths.append(_strength(
                "good_site", "Сайт в хорошем состоянии",
                "Пройдены проверки: " + ", ".join(passed[:4]) + ".",
                3,
            ))
        else:
            strengths.append(_strength(
                "has_site", "Есть сайт",
                "Сайт указан в карточке.",
                2,
            ))
    if reviews is not None and reviews >= 100:
        strengths.append(_strength(
            "many_reviews", "Много отзывов",
            f"{reviews} {_plural(reviews, 'отзыв', 'отзыва', 'отзывов')} "
            "в карточке — их можно показать на сайте.",
            6,
        ))
    return sorted(strengths, key=lambda item: item["weight"], reverse=True)


def _public(items: list[dict]) -> list[dict]:
    return [
        {"code": item["code"], "title": item["title"], "detail": item["detail"]}
        for item in items
    ]


def _summary(ctx: dict, site: dict, gaps: list[dict]) -> str:
    name = ctx["name"]
    subject = f"«{name}»" if name else "Карточка без названия"
    details: list[str] = []
    if ctx["category"]:
        details.append(_lower_first(ctx["category"]))
    if ctx["points"] > 1:
        details.append(
            f"{ctx['points']} {_plural(ctx['points'], 'точка', 'точки', 'точек')}"
        )
    if ctx["rating"] is not None:
        value = f"рейтинг {_rating(ctx['rating'])}"
        if ctx["reviews"] is not None:
            value += (
                f" ({ctx['reviews']} "
                f"{_plural(ctx['reviews'], 'отзыв', 'отзыва', 'отзывов')})"
            )
        details.append(value)
    elif ctx["reviews"] is not None:
        details.append(
            f"{ctx['reviews']} "
            f"{_plural(ctx['reviews'], 'отзыв', 'отзыва', 'отзывов')}"
        )
    first = subject + (" — " + ", ".join(details) if details else "") + "."

    if not site["has_site"]:
        if site["social_only"]:
            second = "У бизнеса нет сайта — вместо него только соцсеть."
        else:
            second = (
                "У бизнеса нет сайта — клиентам приходится искать информацию "
                "в картах и соцсетях."
            )
    else:
        issues = [item for item in gaps if item["code"].startswith("site_")]
        if any(item["code"] == "site_unreachable" for item in issues):
            second = "Сайт указан, но не открывается."
        elif issues:
            titles = [_lower_first(item["title"]) for item in issues[:2]]
            second = "Сайт есть, но " + _join_and(titles) + "."
        else:
            second = "Сайт есть."
    return first + " " + second


def _offer_group(text: str) -> str:
    for group, keywords in OFFER_KEYWORDS:
        for keyword in keywords:
            if keyword in ("бар", "спа", "еда", "авто"):
                pattern = rf"(?<![а-яёa-z]){keyword}(?![а-яёa-z])"
                if re.search(pattern, text):
                    return group
            elif keyword in text:
                return group
    return "default"


def _offer_why(ctx: dict, gaps: list[dict]) -> str:
    if ctx["social_only"]:
        return (
            "Сейчас сайт заменяет страница в соцсети — сделаем сайт + соцсети, "
            "чтобы цены и контакты были доступны всем."
        )
    if not ctx["has_site"]:
        return (
            "Сайта нет — на сайте можно собрать услуги, цены и контакты "
            "в одном месте."
        )
    top = next((item for item in gaps if item["kind"] == "gap"), None)
    if ctx["points"] >= 2:
        return (
            f"У вас {ctx['points']} "
            f"{_plural(ctx['points'], 'точка', 'точки', 'точек')} — на сайте "
            "можно показать их адреса и телефоны в одном месте."
        )
    if ctx["rating"] is not None and ctx["rating"] >= 4.5:
        return (
            f"Рейтинг {_rating(ctx['rating'])} уже говорит о спросе — сайт "
            "покажет услуги и цены тем, кто выбирает."
        )
    if top is not None:
        return f"На сайте есть что исправить: {_lower_first(top['title'])}."
    return "Клиентам нужны понятные цены, контакты и запись в одном месте."


def _build_offer(ctx: dict, gaps: list[dict]) -> dict:
    name_text = (ctx["category"] + " " + ctx["name"]).lower()
    group = _offer_group(name_text)
    template = OFFERS[group]
    bullets = list(template["bullets"])
    if group == "food" and ctx["points"] >= 2:
        bullets[1] = "Карта и адреса всех точек"
    if ctx["social_only"]:
        bullets = bullets[:4] + ["Сохраним соцсети как дополнительные каналы"]
    return {
        "title": template["title"],
        "why": _offer_why(ctx, gaps),
        "bullets": bullets[:5],
    }


def _link_candidates(ctx: dict) -> list[tuple[str, str]]:
    values = [ctx["website"]] + ctx["facts"].get("social", [])
    links: list[tuple[str, str]] = []
    for value in values:
        found = URL_RE.findall(value)
        if not found:
            _scheme, host, url = _split_url(value)
            if host:
                found = [url]
        for raw in found:
            cleaned = raw.rstrip(".,;:!?)»\"'")
            _scheme, host, url = _split_url(cleaned)
            if host:
                links.append((host, url or cleaned))
    return links


def _first_link(links: list[tuple[str, str]], *hosts: str) -> str:
    for host, url in links:
        if any(host == item or host.endswith("." + item) for item in hosts):
            return url
    return ""


def _contacts(ctx: dict) -> list[dict]:
    links = _link_candidates(ctx)
    result: list[dict] = []
    seen_href: set[str] = set()
    seen_type: set[str] = set()

    def add(kind: str, label: str, href: str) -> None:
        href = _text(href)
        if not href or kind in seen_type or len(result) >= 6:
            return
        key = href.lower()
        if key in seen_href:
            return
        seen_href.add(key)
        seen_type.add(kind)
        result.append({"type": kind, "label": label, "href": href})

    if ctx["phone"]:
        digits = PHONE_DIGITS_RE.sub("", ctx["phone"])
        plus = "+" if ctx["phone"].lstrip().startswith("+") else ""
        add("phone", ctx["phone"], "tel:" + plus + digits)

    whatsapp = _first_link(links, "wa.me")
    if whatsapp:
        add("whatsapp", "WhatsApp", whatsapp)
    elif ctx["phone"]:
        digits = PHONE_DIGITS_RE.sub("", ctx["phone"]).lstrip("+")
        if 10 <= len(digits) <= 15:
            add("whatsapp", "WhatsApp", "https://wa.me/" + digits)

    telegram = _first_link(links, "t.me")
    if telegram:
        add("telegram", "Telegram", telegram)
    vk = _first_link(links, "vk.com")
    if vk:
        add("vk", "ВКонтакте", vk)

    raw_website = ctx["website"]
    if raw_website.lower().startswith(("http://", "https://")):
        add("website", "Сайт", raw_website)

    for value in ctx["facts"].get("email", []):
        match = EMAIL_RE.search(value)
        if match:
            add("email", match.group(0), "mailto:" + match.group(0))
            break
    return result


ISSUE_TEXT = {
    "site_unreachable": "сайт не открывается",
    "site_http": "нет HTTPS",
    "site_no_mobile": "нет мобильной версии",
    "site_no_description": "не заполнено описание для поиска",
    "site_no_booking": "нет формы заявки или онлайн-записи",
    "site_no_menu": "нет раздела с меню или ценами",
}

PITCH_GROUP = {
    "food": {
        "what": "меню, цены и адреса точек",
        "action": "Заказать",
        "niche": "кофеен и небольших сетей",
        "offer": "меню с ценами, адреса точек и кнопка WhatsApp",
    },
    "beauty": {
        "what": "услуги, цены и свободное время",
        "action": "Записаться",
        "niche": "салонов и мастеров",
        "offer": "услуги, цены и онлайн-запись",
    },
    "auto": {
        "what": "прайс, примеры работ и расчёт",
        "action": "Оставить заявку на расчёт",
        "niche": "сервисов и подрядчиков",
        "offer": "прайс, примеры работ и форма расчёта",
    },
    "retail": {
        "what": "товары, цены и доставку",
        "action": "Заказать",
        "niche": "небольших магазинов",
        "offer": "каталог с ценами, доставка и оформление заказа",
    },
    "default": {
        "what": "услуги, цены и контакты",
        "action": "Оставить заявку",
        "niche": "небольших бизнесов",
        "offer": "услуги, цены, контакты и форма заявки",
    },
}


def _social_name(url: str) -> str:
    _scheme, host, _url = _split_url(url)
    if host.endswith("vk.com"):
        return "ВКонтакте"
    if host.endswith("t.me") or host.endswith("telegram.me"):
        return "Telegram"
    if "instagram" in host:
        return "Instagram"
    if host.endswith("ok.ru"):
        return "Одноклассники"
    if host.endswith("wa.me") or "whatsapp" in host:
        return "WhatsApp"
    if host.endswith("youtube.com"):
        return "YouTube"
    return "соцсеть"


def _pitch_category(value: str) -> str:
    text = _text(value)
    for separator in (";", "|", "•"):
        if separator in text:
            text = text.split(separator, 1)[0].strip()
    return _lower_first(_clip(text, 60))


def _pitch_hook(ctx: dict) -> str:
    """Первая фраза: конкретика, которая доказывает, что карточку открыли."""
    details: list[str] = []
    category = _pitch_category(ctx["category"])
    name_lower = ctx["name"].lower()
    first_word = category.split()[0] if category else ""
    if category and first_word not in name_lower:
        details.append(category)
    if ctx["points"] > 1:
        details.append(
            f"{ctx['points']} {_plural(ctx['points'], 'точка', 'точки', 'точек')}"
        )
    if ctx["rating"] is not None:
        rating = f"рейтинг {_rating(ctx['rating'])}"
        if ctx["reviews"] is not None:
            rating += (
                f" ({ctx['reviews']} "
                f"{_plural(ctx['reviews'], 'отзыв', 'отзыва', 'отзывов')})"
            )
        details.append(rating)

    if ctx["name"]:
        line = f"Здравствуйте! Увидел «{ctx['name']}» в справочнике"
    elif details:
        line = "Здравствуйте! Увидел вашу организацию в справочнике"
    else:
        return "Здравствуйте!"
    if details:
        line += " — " + ", ".join(details)
    return line + "."


def _pitch_problem(ctx: dict, group: str, gaps: list[dict]) -> str:
    """Наблюдение и его последствие для клиента, без диагнозов и наездов."""
    data = PITCH_GROUP[group]
    what = data["what"]
    action = data["action"]

    if ctx["social_only"]:
        return (
            f"Вместо сайта — только {_social_name(ctx['website'])}. "
            f"Подписчикам удобно, а новый клиент из поиска не увидит {what}. "
            "Оставить заявку там тоже неудобно."
        )
    if not ctx["has_site"]:
        if ctx["points"] >= 2:
            return (
                f"Сайта нет: {what} разбросаны по картам и соцсетям. "
                f"{action} можно только по телефону или в переписке."
            )
        if not ctx["name"] and not ctx["category"]:
            return (
                f"В карточке не указан сайт: клиенту из поиска негде "
                f"посмотреть {what} в одном месте. {action} можно только "
                "по телефону или в переписке."
            )
        return (
            f"Сайта нет: клиенту из поиска негде посмотреть {what} "
            f"в одном месте. {action} можно только по телефону или "
            "в переписке."
        )

    _scheme, host, _url = _split_url(ctx["website"])
    if any(gap["code"] == "site_unreachable" for gap in gaps):
        return (
            f"Сайт {host} сейчас не открывается — вместо {what} клиент "
            "видит ошибку."
        )

    issues: list[str] = []
    for gap in gaps:
        text = ISSUE_TEXT.get(gap["code"])
        if text and text not in issues:
            issues.append(text)
        if len(issues) == 2:
            break
    if issues:
        return (
            f"Открыл {host} с телефона: {_join_and(issues)}. "
            "С телефона это мешает дойти до заявки."
        )
    return (
        f"Сайт {host} у вас есть — вопрос не в наличии, а в том, "
        "сколько заявок он приносит."
    )


def _pitch_offer(ctx: dict, group: str, gaps: list[dict]) -> str:
    data = PITCH_GROUP[group]
    if not ctx["has_site"] or ctx["social_only"]:
        offer_line = data["offer"]
        if group == "food" and ctx["points"] < 2:
            offer_line = "меню с ценами, адрес и кнопка WhatsApp"
        return (
            f"Я делаю сайты для {data['niche']}. Начать можно с одной "
            f"страницы: {offer_line}."
        )
    if any(gap["code"] in ISSUE_TEXT for gap in gaps):
        return (
            f"Я делаю сайты для {data['niche']}. Могу переделать главную "
            f"так, чтобы {data['what']} были на виду, а заявка — в один клик."
        )
    return (
        f"Я делаю сайты для {data['niche']}. Могу посмотреть вашу главную "
        "и прислать 3 точки, где теряются заявки."
    )


def _pitch_cta(ctx: dict, gaps: list[dict]) -> str:
    if ctx["has_site"] and any(gap["code"] in ISSUE_TEXT for gap in gaps):
        return (
            "Могу прислать короткий разбор, что исправить в первую очередь. "
            "Прислать?"
        )
    if ctx["has_site"]:
        return "Прислать короткий разбор?"
    return "Могу прислать макет главной — посмотрите, подходит ли. Прислать?"


def _pitch_benefit(ctx: dict, gaps: list[dict]) -> str:
    if ctx["has_site"] and any(gap["code"] in ISSUE_TEXT for gap in gaps):
        return (
            "Клиенту проще дойти до заявки, а вам — не терять тех, "
            "кто уже пришёл."
        )
    if not ctx["has_site"] or ctx["social_only"]:
        return (
            "Клиенту проще выбрать и заказать, а вам — меньше пустых звонков."
        )
    return ""


def _trim_body(body: str, limit: int) -> str:
    if len(body) <= limit:
        return body
    cut = body[:limit]
    boundary = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if boundary >= 250:
        return cut[: boundary + 1].strip()
    fallback = cut.rstrip(" ,;:-")
    space = fallback.rfind(" ")
    if space >= 250:
        fallback = fallback[:space]
    return fallback.rstrip(" ,;:-")


def _compose_pitch(ctx: dict, offer: dict, gaps: list[dict]) -> str:
    """Короткое живое сообщение по схеме: наблюдение → проблема → оффер → CTA."""
    group = _offer_group((ctx["category"] + " " + ctx["name"]).lower())
    parts = [
        _pitch_hook(ctx),
        _pitch_problem(ctx, group, gaps),
        _pitch_offer(ctx, group, gaps),
    ]
    benefit = _pitch_benefit(ctx, gaps)
    if benefit:
        parts.append(benefit)
    parts.append(_pitch_cta(ctx, gaps))
    text = " ".join(part for part in parts if part).strip()

    limit = 750
    if len(text) > limit:
        closing = _pitch_cta(ctx, gaps)
        body = _trim_body(text[: -len(closing)].strip(), limit - len(closing) - 1)
        text = body + " " + closing
    return text


def build_intel(lead: dict) -> dict:
    """Собрать блок продажной аналитики из карточки лида.

    Функция ничего не меняет и не ходит наружу; любые пропуски и битые
    значения превращаются в пустые строки/None.
    """
    ctx = _context(lead)
    site = {
        "has_site": ctx["has_site"],
        "url": ctx["site_url"] if (ctx["has_site"] or ctx["social_only"]) else "",
        "social_only": ctx["social_only"],
        "checks": ctx["checks"],
        "score": _site_score(ctx["checks"]) if ctx["has_site"] else 0,
    }

    score = _lead_score(ctx)
    if score >= 65:
        label = "высокий"
    elif score >= 40:
        label = "средний"
    else:
        label = "низкий"

    gaps = _build_gaps(ctx)
    strengths = _build_strengths(ctx)
    ordered = (
        sorted(gaps, key=lambda item: item["weight"], reverse=True)
        + sorted(strengths, key=lambda item: item["weight"], reverse=True)
    )
    hooks = [
        {
            "code": item["code"],
            "title": item["title"],
            "detail": item["detail"],
            "kind": item["kind"],
            "weight": item["weight"],
        }
        for item in ordered[:6]
    ]

    offer = _build_offer(ctx, gaps)
    pitch = _compose_pitch(ctx, offer, gaps)

    return {
        "score": score,
        "label": label,
        "summary": _summary(ctx, site, gaps),
        "hooks": hooks,
        "strengths": _public(strengths),
        "gaps": _public(gaps),
        "offer": offer,
        "pitch": pitch,
        "contacts": _contacts(ctx),
        "site": site,
    }


def build_pitch(lead: dict, intel: dict) -> str:
    """Готовое сообщение: берём из intel, иначе собираем заново."""
    if isinstance(intel, dict):
        pitch = intel.get("pitch")
        if isinstance(pitch, str) and pitch.strip():
            return pitch.strip()
    return build_intel(lead)["pitch"]



