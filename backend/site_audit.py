"""Аудит сайта бизнеса: техническое состояние и зацепки для продажи.

Модуль работает только с уже загруженным HTML: не ходит в сеть и не
пишет в БД. На вход приходит ответ (HTML, статус, заголовки), на выходе —
плоский словарь с признаками страницы: есть ли HTTPS, мобильная версия,
описание, форма, запись, меню, цены и контакты. Эти признаки нужны
менеджеру как повод для звонка, поэтому они только фиксируют факты,
найденные в разметке, и ничего не додумывают.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

# Соцсети и мессенджеры: хост -> человекочитаемая подпись.
SOCIAL_HOSTS = {
    "vk.com": "ВКонтакте",
    "instagram.com": "Instagram",
    "t.me": "Telegram",
    "ok.ru": "Одноклассники",
    "wa.me": "WhatsApp",
    "youtube.com": "YouTube",
}

# Слова-маркеры онлайн-записи. Кириллические основы ищем подстрокой,
# латинские — по границе слова, чтобы facebook не считался «booking».
BOOKING_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"бронир",
        r"запис",
        r"забронировать",
        r"выбрать время",
        r"\bbook(?:ing)?\b",
        r"\breserv(?:e|ation)\b",
        r"\bappointment\b",
        r"заказ",
        r"оформить заказ",
    )
)

MENU_MARKERS = ("меню", "menu")
PRICE_LINK_MARKERS = ("прайс", "цены", "цена", "стоимость")

CMS_GENERATOR_MARKERS = (
    ("WordPress", ("wordpress",)),
    ("1C-Битрикс", ("bitrix", "1c-bitrix", "1с-битрикс")),
    ("Tilda", ("tilda",)),
    ("Wix", ("wix",)),
    ("Shopify", ("shopify",)),
    ("Joomla", ("joomla",)),
    ("MODX", ("modx",)),
    ("uCoz", ("ucoz",)),
    ("OpenCart", ("opencart",)),
    ("InSales", ("insales",)),
    ("Craftum", ("craftum",)),
)

# Признаки CMS прямо в разметке (ссылки на статику, служебные пути).
CMS_HTML_MARKERS = (
    ("WordPress", ("wp-content", "wp-json", "wp-includes")),
    ("1C-Битрикс", ("bitrix", "1c-bitrix", "1с-битрикс")),
    ("Tilda", ("tilda", "tildacdn")),
    ("Wix", ("wixstatic", "wix.com")),
    ("Shopify", ("shopify", "myshopify")),
    ("Joomla", ("joomla",)),
    ("MODX", ("modx",)),
    ("uCoz", ("ucoz",)),
    ("OpenCart", ("opencart",)),
    ("InSales", ("insales",)),
    ("Craftum", ("craftum",)),
)

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

_SPLIT_SCHEME = re.compile(r"^[a-z][a-z0-9+.\-]*:", re.IGNORECASE)

CONTACT_SOCIAL_HOSTS = ("vk.com", "t.me", "wa.me")


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def _collapse(value: str) -> str:
    """Схлопывает любые пробелы (включая NBSP) в один обычный пробел."""
    return " ".join(
        str(value).replace("\u00a0", " ").replace("\u202f", " ").split()
    )


def _checked_at() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def _hosts_match(host: str, base: str) -> bool:
    return host == base or host.endswith("." + base)


def host_of(url: str) -> str:
    """Возвращает хост без ``www.`` в нижнем регистре или пустую строку."""
    text = str(url or "").strip()
    if not text:
        return ""

    try:
        parts = urlsplit(text)
        host = (parts.hostname or "").lower()
    except ValueError:
        return ""

    # Ссылки без схемы ("vk.com/foo") тоже разбираем, но не принимаем
    # "javascript:..." и прочие псевдо-схемы за имя хоста.
    if not host and "://" not in text and not _SPLIT_SCHEME.match(text):
        try:
            host = (urlsplit("//" + text).hostname or "").lower()
        except ValueError:
            return ""

    if host.startswith("www."):
        host = host[4:]
    host = host.rstrip(".")
    # Пробелы и слэши в «хосте» означают, что это не URL, а случайный текст.
    if not host or any(char.isspace() or char in "/\\" for char in host):
        return ""
    return host


def is_social_url(url: str) -> bool:
    """True, если ссылка ведёт на соцсеть/мессенджер из SOCIAL_HOSTS."""
    host = host_of(url)
    return any(_hosts_match(host, base) for base in SOCIAL_HOSTS)


def social_label(url: str) -> str:
    """Подпись соцсети («ВКонтакте», «Telegram», ...) или пустая строка."""
    host = host_of(url)
    for base, label in SOCIAL_HOSTS.items():
        if _hosts_match(host, base):
            return label
    return ""


def empty_audit(url: str = "", error: str = "") -> dict:
    """Аудит-заглушка: сайт не загрузился или разобрать его не удалось."""
    return {
        "url": str(url or ""),
        "final_url": "",
        "status": 0,
        "ok": False,
        "https": False,
        "redirected": False,
        "elapsed_ms": 0,
        "html_kb": 0,
        "title": "",
        "description": "",
        "has_viewport": False,
        "has_h1": False,
        "has_form": False,
        "has_booking": False,
        "has_menu": False,
        "has_prices": False,
        "has_contacts": False,
        "cms": "",
        "error": str(error or ""),
        "checked_at": _checked_at(),
    }


def _visible_text(soup: BeautifulSoup) -> str:
    """Текст страницы без скриптов, стилей и служебной разметки."""
    clone = BeautifulSoup(str(soup), "html.parser")
    for tag in clone.find_all(["script", "style", "noscript", "template", "svg"]):
        tag.decompose()
    raw = clone.get_text("\n", strip=True)
    lines = [_collapse(line) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def _link_values(soup: BeautifulSoup) -> list[str]:
    """Пары href/текст всех ссылок в нижнем регистре."""
    values: list[str] = []
    for anchor in soup.find_all("a", href=True):
        values.append(str(anchor.get("href") or "").strip().lower())
        text = _collapse(anchor.get_text(" ", strip=True)).lower()
        if text:
            values.append(text)
    return values


def _input_values(soup: BeautifulSoup) -> list[str]:
    """Placeholder/value/aria-label полей ввода в нижнем регистре."""
    values: list[str] = []
    for field in soup.find_all(["input", "textarea"]):
        for attribute in ("placeholder", "value", "aria-label"):
            value = _collapse(str(field.get(attribute) or ""))
            if value:
                values.append(value.lower())
    return values


def _meta_content(soup: BeautifulSoup, *names: str) -> str:
    """Первый непустой content среди meta с указанными name/property."""
    desired = {name.lower() for name in names}
    for meta in soup.find_all("meta"):
        keys = {
            str(meta.get("name") or "").strip().lower(),
            str(meta.get("property") or "").strip().lower(),
        }
        if not keys & desired:
            continue
        content = _collapse(str(meta.get("content") or ""))
        if content:
            return content
    return ""


def _page_title(soup: BeautifulSoup) -> str:
    if soup.title:
        title = _collapse(soup.title.get_text(" ", strip=True))
        if title:
            return title[:160].strip()
    return _meta_content(soup, "og:title")[:160].strip()


def _page_description(soup: BeautifulSoup) -> str:
    description = _meta_content(soup, "description")
    if not description:
        description = _meta_content(soup, "og:description")
    return description[:300].strip()


def _has_viewport(soup: BeautifulSoup) -> bool:
    for meta in soup.find_all("meta"):
        if str(meta.get("name") or "").strip().lower() == "viewport":
            return True
    return False


def _has_h1(soup: BeautifulSoup) -> bool:
    for heading in soup.find_all("h1"):
        if _collapse(heading.get_text(" ", strip=True)):
            return True
    return False


def _is_search_only_form(form) -> bool:
    """Форма поиска без полей заявки (email/tel/textarea) — не лид-форма."""
    has_search = False
    has_lead_field = False
    for field in form.find_all(["input", "textarea", "select"]):
        if field.name.lower() == "textarea":
            has_lead_field = True
            continue
        if field.name.lower() != "input":
            continue
        input_type = str(field.get("type") or "text").strip().lower()
        if input_type == "search":
            has_search = True
        elif input_type in ("email", "tel"):
            has_lead_field = True
    return has_search and not has_lead_field


def _has_form(soup: BeautifulSoup) -> bool:
    for form in soup.find_all("form"):
        if not _is_search_only_form(form):
            return True

    # Кнопка отправки вне формы — тоже признак формы (например, в SPA).
    for field in soup.find_all(["input", "button"]):
        if str(field.get("type") or "").strip().lower() != "submit":
            continue
        if field.find_parent("form"):
            continue
        return True
    return False


def _has_booking(text: str, link_values: list[str], input_values: list[str]) -> bool:
    haystack = "\n".join([text, *link_values, *input_values])
    return any(pattern.search(haystack) for pattern in BOOKING_PATTERNS)


def _json_ld(soup: BeautifulSoup) -> str:
    chunks: list[str] = []
    for script in soup.find_all("script"):
        if "ld+json" not in str(script.get("type") or "").lower():
            continue
        raw = script.string if script.string is not None else script.get_text()
        if raw:
            chunks.append(str(raw))
    return " ".join(chunks).lower()


def _has_menu(link_values: list[str], json_ld: str) -> bool:
    if any(marker in value for value in link_values for marker in MENU_MARKERS):
        return True
    return "menu" in json_ld


def _has_prices(text: str, link_values: list[str]) -> bool:
    lowered = text.lower()
    if "₽" in text or "руб" in lowered or "price" in lowered:
        return True
    return any(marker in value for value in link_values for marker in PRICE_LINK_MARKERS)


def _has_contacts(soup: BeautifulSoup, text: str) -> bool:
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        lowered = href.lower()
        if lowered.startswith(("tel:", "mailto:")):
            return True
        host = host_of(href)
        if any(_hosts_match(host, base) for base in CONTACT_SOCIAL_HOSTS):
            return True

    return bool(
        RE_EMAIL.search(text)
        or RE_PHONE_RU.search(text)
        or RE_PHONE_INTL.search(text)
    )


def _detect_cms(soup: BeautifulSoup, html_text: str, headers: dict | None) -> str:
    generator = ""
    for meta in soup.find_all("meta"):
        if str(meta.get("name") or "").strip().lower() == "generator":
            generator = _collapse(str(meta.get("content") or "")).lower()
            if generator:
                break

    header_text = ""
    if headers:
        try:
            header_text = " ".join(
                f"{key} {value}" for key, value in headers.items()
            ).lower()
        except Exception:
            header_text = ""

    haystack = f"{html_text.lower()} {header_text}"

    for name, markers in CMS_GENERATOR_MARKERS:
        if any(marker in generator for marker in markers):
            return name
    for name, markers in CMS_HTML_MARKERS:
        if any(marker in haystack for marker in markers):
            return name
    return ""


def _normalize_url(value: str) -> tuple[str, str, str, str] | None:
    """Схема, хост, путь и query для сравнения; фрагмент отбрасывается."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None

    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return None

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return (parts.scheme.lower(), host.rstrip("."), path, parts.query)


def _int_value(value: object) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _html_kb(html: str | bytes) -> int:
    if isinstance(html, (bytes, bytearray)):
        size = len(html)
    else:
        size = len(str(html or "").encode("utf-8", errors="replace"))
    return (size + 1023) // 1024 if size else 0


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------


def audit_page(
    html: str,
    url: str,
    *,
    status: int = 200,
    elapsed_ms: int = 0,
    final_url: str | None = None,
    headers: dict | None = None,
) -> dict:
    """Разбирает HTML и возвращает признаки страницы.

    Никогда не бросает исключений: при любой ошибке разбора возвращает
    ``empty_audit`` с текстом ошибки.
    """
    try:
        return _audit_page(
            html,
            url,
            status=status,
            elapsed_ms=elapsed_ms,
            final_url=final_url,
            headers=headers,
        )
    except Exception as exc:
        return empty_audit(url, f"ошибка разбора: {exc}")


def _audit_page(
    html: str,
    url: str,
    *,
    status: int,
    elapsed_ms: int,
    final_url: str | None,
    headers: dict | None,
) -> dict:
    raw_html = html if html is not None else ""
    if isinstance(raw_html, (bytes, bytearray)):
        raw_bytes = bytes(raw_html)
        soup = BeautifulSoup(raw_bytes, "html.parser")
    else:
        html_text = str(raw_html)
        raw_bytes = html_text.encode("utf-8", errors="replace")
        soup = BeautifulSoup(html_text, "html.parser")

    page_url = str(url or "").strip()
    effective_url = str(final_url or page_url).strip()

    status_int = _int_value(status)
    elapsed_int = _int_value(elapsed_ms)

    origin = _normalize_url(page_url)
    destination = _normalize_url(effective_url)
    redirected = bool(origin and destination and origin != destination)

    try:
        scheme = urlsplit(effective_url).scheme.lower()
    except ValueError:
        scheme = ""

    text = _visible_text(soup)
    link_values = _link_values(soup)
    input_values = _input_values(soup)

    return {
        "url": page_url,
        "final_url": effective_url,
        "status": status_int,
        "ok": 200 <= status_int < 400,
        "https": scheme == "https",
        "redirected": redirected,
        "elapsed_ms": elapsed_int,
        "html_kb": _html_kb(raw_bytes),
        "title": _page_title(soup),
        "description": _page_description(soup),
        "has_viewport": _has_viewport(soup),
        "has_h1": _has_h1(soup),
        "has_form": _has_form(soup),
        "has_booking": _has_booking(text, link_values, input_values),
        "has_menu": _has_menu(link_values, _json_ld(soup)),
        "has_prices": _has_prices(text, link_values),
        "has_contacts": _has_contacts(soup, text),
        "cms": _detect_cms(soup, str(soup), headers),
        "error": "",
        "checked_at": _checked_at(),
    }
