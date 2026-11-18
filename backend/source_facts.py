"""Извлечение контактов из сохранённого payload источника.

Модуль читает ``business_source.payload_json`` (Яндекс.Карты, 2ГИС и
произвольные источники) и достаёт только те значения, которые реально
присутствуют в payload. Ничего не додумывается: нет поля — нет значения.

Формы payload пересекаются (английские ключи Яндекса, русские колонки 2ГИС
с числовыми суффиксами), поэтому используется один набор псевдонимов.
``source`` — необязательная подсказка об источнике; на результат она не
влияет, пока ключи разбираются однозначно.
"""

from __future__ import annotations

import math
import re
from typing import Any
from urllib.parse import urlsplit

__all__ = ["extract_source_data", "digits_only"]

# Ссылки в payload бывают в тексте через запятую/точку с запятой.
_URL_RE = re.compile(r"https?://[^\s<>\"',;]+", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_SPLIT_RE = re.compile(r"[;,\n\r]+")

# Домены, которые считаем социальными сетями (и только они).
_SOCIAL_DOMAINS = (
    "vk.com",
    "vk.ru",
    "t.me",
    "telegram.me",
    "telegram.dog",
    "wa.me",
    "whatsapp.com",
    "ok.ru",
    "instagram.com",
    "youtube.com",
    "youtu.be",
    "facebook.com",
    "fb.com",
    "tiktok.com",
)

# Типы карточек соцсетей: от них зависит разбор «голых» значений.
_SOCIAL_KINDS = {
    "vk",
    "telegram",
    "whatsapp",
    "youtube",
    "instagram",
    "facebook",
    "ok",
    "tiktok",
    "social",
}

_TRAILING_PUNCT = ".,;:!?»«\"'"

# Расширение TLD, которое почти всегда означает файл, а не почту.
_BAD_EMAIL_TLDS = {
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


def digits_only(value: str) -> str:
    """Оставить в значении только цифры (телефоны, счётчики)."""
    return re.sub(r"\D", "", str(value or ""))


def _collapse(value: str) -> str:
    """Схлопнуть любые пробелы (включая NBSP) в один обычный пробел."""
    text = str(value or "").replace("\u00a0", " ").replace("\u202f", " ")
    return " ".join(text.split())


def _empty_result() -> dict[str, Any]:
    """Пустой результат с полным и постоянным набором ключей."""
    return {
        "website": "",
        "websites": [],
        "social": [],
        "email": [],
        "phone": [],
        "hours": [],
        "description": [],
        "category": "",
        "rating": "",
        "reviews_count": None,
        "address": "",
        "lat": None,
        "lon": None,
    }


# ---------------------------------------------------------------------------
# Распознавание ключей payload
# ---------------------------------------------------------------------------


def _norm_key(value: str) -> str:
    """Нормализовать имя ключа: регистр, суффикс ``N``, пунктуация."""
    text = _collapse(value).casefold().replace("ё", "е")
    text = re.sub(r"\s+\d+$", "", text)
    text = re.sub(r"[^0-9a-zа-я]+", "", text)
    return re.sub(r"\d+$", "", text)


_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("website", (
        "website", "websites", "site", "сайт", "веб-сайт", "вебсайт",
        "web site", "homepage",
    )),
    ("vk", ("вконтакте", "vk", "vkontakte", "vk.com")),
    ("telegram", (
        "telegram", "телеграм", "телеграмм", "t.me", "telegram.me",
    )),
    ("whatsapp", ("whatsapp", "ватсап", "whats app", "wa.me")),
    ("youtube", ("youtube", "ютуб", "youtu.be")),
    ("instagram", ("instagram", "инстаграм", "inst")),
    ("facebook", ("facebook", "fb", "фейсбук")),
    ("ok", ("ok", "ok.ru", "одноклассники")),
    ("tiktok", ("tiktok", "тикток")),
    ("social", ("social", "соцсети", "социальные сети")),
    ("phone", (
        "phone", "phones", "телефон", "телефоны", "тел", "тел.",
    )),
    ("email", (
        "email", "e-mail", "e mail", "mail", "почта", "эл. почта",
        "электронная почта",
    )),
    ("hours", (
        "hours", "часы", "часы работы", "график работы", "время работы",
        "schedule",
    )),
    ("description", ("description", "описание")),
    ("category", (
        "category", "categories", "категория", "рубрика", "рубрики",
        "rubric", "rubrics",
    )),
    ("rating", ("rating", "рейтинг")),
    ("reviews", (
        "reviews", "reviews_count", "review_count", "reviewscount",
        "отзывы", "количество отзывов", "число отзывов",
    )),
    ("address", ("address", "адрес")),
    ("lat", ("lat", "latitude", "широта")),
    ("lon", ("lon", "lng", "longitude", "долгота")),
)

_ALIAS_TO_KIND: dict[str, str] = {
    _norm_key(alias): kind
    for kind, aliases in _ALIASES
    for alias in aliases
}


def _classify_key(key: Any) -> str:
    """Вернуть канонический тип поля или ``""`` для неизвестного ключа."""
    if not isinstance(key, str):
        return ""
    return _ALIAS_TO_KIND.get(_norm_key(key), "")


# ---------------------------------------------------------------------------
# Безопасное чтение значений
# ---------------------------------------------------------------------------


def _strings(value: Any) -> list[str]:
    """Только строковые значения (включая списки), с схлопнутыми пробелами."""
    if isinstance(value, str):
        text = _collapse(value)
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            result.extend(_strings(item))
        return result
    # None, числа, bool, словари и прочее просто игнорируются.
    return []


def _raw_strings(value: Any) -> list[str]:
    """Строковые значения без потери переводов строк (для часов/телефонов)."""
    if isinstance(value, str):
        text = value.replace("\u00a0", " ").replace("\u202f", " ").strip()
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        result: list[str] = []
        for item in value:
            result.extend(_raw_strings(item))
        return result
    return []


def _numeric_values(value: Any) -> list[Any]:
    """Числа и строки для числовых полей (rating/reviews/lat/lon)."""
    if isinstance(value, bool) or value is None:
        return []
    if isinstance(value, (int, float)):
        return [value]
    if isinstance(value, str):
        text = _collapse(value)
        return [text] if text else []
    if isinstance(value, (list, tuple)):
        result: list[Any] = []
        for item in value:
            result.extend(_numeric_values(item))
        return result
    return []


def _dedupe(values: list[str]) -> list[str]:
    """Дедупликация без учёта регистра с сохранением порядка."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = _collapse(value).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


# ---------------------------------------------------------------------------
# Ссылки и соцсети
# ---------------------------------------------------------------------------


def _clean_url(value: str) -> str:
    """Проверить http(s)-ссылку и убрать хвостовую пунктуацию."""
    text = _collapse(value).strip().strip("\"'<>")
    text = text.rstrip(_TRAILING_PUNCT)
    pairs = {")": "(", "]": "[", "}": "{"}
    while text and text[-1] in pairs and text.count(text[-1]) > text.count(
        pairs[text[-1]]
    ):
        text = text[:-1].rstrip(_TRAILING_PUNCT)
    if not text or any(char.isspace() for char in text):
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    if not parts.netloc or not parts.hostname:
        return ""
    return text


def _host(url: str) -> str:
    """Хост ссылки без ``www.`` или ``""`` для некорректной ссылки."""
    try:
        host = (urlsplit(url).hostname or "").casefold()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_social_url(url: str) -> bool:
    """Ссылка ведёт на известную социальную сеть."""
    host = _host(url)
    return bool(host) and any(
        host == domain or host.endswith("." + domain)
        for domain in _SOCIAL_DOMAINS
    )


def _normalize_social(value: str, kind: str) -> str:
    """Привести значение соцсети к http(s)-ссылке, не выдумывая домен."""
    text = _collapse(value).strip().strip("\"'")
    if not text:
        return ""

    if re.match(r"^https?://", text, re.IGNORECASE):
        url = _clean_url(text)
        return url if url and _is_social_url(url) else ""

    low = text.casefold()
    # Голые ссылки известных доменов: добавляем только схему.
    for domain in ("t.me/", "vk.com/", "wa.me/"):
        if low.startswith(domain):
            url = _clean_url("https://" + text)
            return url if url and _is_social_url(url) else ""

    if kind == "whatsapp":
        # WhatsApp иногда хранит телефон, а не ссылку.
        if re.fullmatch(r"[+\d\s().\-]+", text):
            digits = digits_only(text)
            if 10 <= len(digits) <= 15:
                return f"https://wa.me/{digits}"
        return ""

    if kind in ("telegram", "vk") and text.startswith("@"):
        handle = text[1:].strip()
        if re.fullmatch(r"[A-Za-z0-9_.\-]+", handle):
            prefix = "https://t.me/" if kind == "telegram" else "https://vk.com/"
            return prefix + handle
    return ""


def _social_values(value: Any, kind: str) -> list[str]:
    """Разобрать поле соцсети (одна ссылка, список или текст через запятую)."""
    result: list[str] = []
    for text in _strings(value):
        tokens = _URL_RE.findall(text)
        if not tokens:
            tokens = [part.strip() for part in re.split(r"[;,\n\r]+", text)]
        for token in tokens:
            if not token:
                continue
            url = _normalize_social(token, kind)
            if url:
                result.append(url)
    return result


# ---------------------------------------------------------------------------
# Остальные поля
# ---------------------------------------------------------------------------


def _website_values(value: Any) -> tuple[list[str], list[str]]:
    """Разделить значение «Веб-сайт» на обычные сайты и соцсети."""
    websites: list[str] = []
    social: list[str] = []
    for text in _strings(value):
        for token in _URL_RE.findall(text):
            url = _clean_url(token)
            if not url:
                continue
            if _is_social_url(url):
                social.append(url)
            else:
                websites.append(url)
    return websites, social


def _valid_email(value: str) -> bool:
    """Базовая проверка адреса почты без сетевых запросов."""
    text = value.strip()
    if text.count("@") != 1 or ".." in text:
        return False
    local, _, domain = text.partition("@")
    if not local or local.startswith(".") or local.endswith("."):
        return False
    labels = domain.split(".")
    if len(labels) < 2:
        return False
    if any(not label or label.startswith("-") or label.endswith("-")
           for label in labels):
        return False
    if not re.fullmatch(r"[A-Za-z]{2,}", labels[-1]):
        return False
    return labels[-1].casefold() not in _BAD_EMAIL_TLDS


def _email_values(value: Any) -> list[str]:
    """Найти адреса почты в строке/списке строк."""
    result: list[str] = []
    for text in _strings(value):
        for match in _EMAIL_RE.findall(text):
            candidate = match.rstrip(".")
            if _valid_email(candidate):
                result.append(candidate)
    return result


def _phone_values(value: Any) -> list[str]:
    """Телефоны как есть (комментарий в скобках сохраняется)."""
    result: list[str] = []
    for text in _raw_strings(value):
        for part in _PHONE_SPLIT_RE.split(text):
            phone = _collapse(part)
            if phone:
                result.append(phone)
    return result


def _hours_values(value: Any) -> list[str]:
    """Часы работы: по строке на элемент, одиночное значение целиком."""
    result: list[str] = []
    for text in _raw_strings(value):
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        for line in normalized.split("\n"):
            line = _collapse(line)
            if line:
                result.append(line)
    return result


def _first_text(value: Any) -> str:
    """Первое непустое строковое значение."""
    for text in _strings(value):
        return text
    return ""


def _first_category(value: Any) -> str:
    """Первая рубрика (2ГИС хранит их через ``;``)."""
    for text in _strings(value):
        first = text.split(";", 1)[0].strip()
        if first:
            return first
    return ""


def _first_rating(value: Any) -> str:
    """Рейтинг строкой, как он пришёл (число тоже допустимо)."""
    for item in _numeric_values(value):
        if isinstance(item, float):
            text = str(item)
        else:
            text = _collapse(str(item))
        if text:
            return text
    return ""


def _to_float(item: Any) -> float | None:
    """Разобрать число; запятая как десятичный разделитель допустима."""
    if isinstance(item, bool):
        return None
    if isinstance(item, (int, float)):
        number = float(item)
    else:
        text = _collapse(str(item)).replace(" ", "")
        if not text:
            return None
        if "," in text and "." not in text:
            text = text.replace(",", ".")
        try:
            number = float(text)
        except ValueError:
            return None
    return number if math.isfinite(number) else None


def _first_coord(value: Any, limit: float) -> float | None:
    """Первая корректная координата в диапазоне ``-limit..limit``."""
    for item in _numeric_values(value):
        number = _to_float(item)
        if number is not None and -limit <= number <= limit:
            return number
    return None


def _first_reviews(value: Any) -> int | None:
    """Количество отзывов: только цифры, иначе ``None``."""
    for item in _numeric_values(value):
        if isinstance(item, bool):
            continue
        if isinstance(item, int):
            return item
        if isinstance(item, float):
            if item.is_integer():
                return int(item)
            continue
        digits = digits_only(str(item))
        if digits:
            return int(digits)
    return None


# ---------------------------------------------------------------------------
# Публичная точка входа
# ---------------------------------------------------------------------------


def extract_source_data(payload: dict, source: str = "") -> dict:
    """Достать контакты из payload источника.

    Всегда возвращает полный словарь с одинаковым набором ключей. Не
    поднимает исключений: ``None``, не-словарь, вложенные словари, числа и
    списки неподходящих значений просто игнорируются.

    :param payload: сохранённый ``payload_json`` источника.
    :param source: подсказка об источнике (``yandex``, ``2gis``, ...).
    """
    result = _empty_result()
    if not isinstance(payload, dict):
        return result

    # Источник не меняет разбор: ключи трёх форм либо совпадают, либо
    # однозначно определяются по псевдониму.
    _ = source

    websites: list[str] = []
    social: list[str] = []
    emails: list[str] = []
    phones: list[str] = []
    hours: list[str] = []
    descriptions: list[str] = []

    for key, value in payload.items():
        kind = _classify_key(key)
        if not kind:
            continue

        if kind == "website":
            found_websites, found_social = _website_values(value)
            websites.extend(found_websites)
            social.extend(found_social)
        elif kind in _SOCIAL_KINDS:
            social.extend(_social_values(value, kind))
        elif kind == "phone":
            phones.extend(_phone_values(value))
        elif kind == "email":
            emails.extend(_email_values(value))
        elif kind == "hours":
            hours.extend(_hours_values(value))
        elif kind == "description":
            descriptions.extend(_strings(value))
        elif kind == "category":
            if not result["category"]:
                result["category"] = _first_category(value)
        elif kind == "rating":
            if not result["rating"]:
                result["rating"] = _first_rating(value)
        elif kind == "reviews":
            if result["reviews_count"] is None:
                result["reviews_count"] = _first_reviews(value)
        elif kind == "address":
            if not result["address"]:
                result["address"] = _first_text(value)
        elif kind == "lat":
            if result["lat"] is None:
                result["lat"] = _first_coord(value, 90.0)
        elif kind == "lon":
            if result["lon"] is None:
                result["lon"] = _first_coord(value, 180.0)

    result["websites"] = _dedupe(websites)
    result["social"] = _dedupe(social)
    result["website"] = result["websites"][0] if result["websites"] else ""
    result["email"] = _dedupe(emails)
    result["phone"] = _dedupe(phones)
    result["hours"] = _dedupe(hours)
    result["description"] = _dedupe(descriptions)
    return result
