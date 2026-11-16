"""Адаптер 2ГИС.

Два режима:

* если задан ``TWOGIS_API_KEY`` — официальный Catalog API 3.0
  (https://docs.2gis.com/api/search/places/overview);
* иначе — браузерный парсер ``parser-2gis.py`` из
  ``parsers/02-2gis-parser-eroloft``.

Браузерный парсер не умеет bbox: он собирает весь город одним прогоном.
Поэтому ``supports_bbox = False``, а результат потом фильтруется по bbox.
Готовый прогон кэшируется на 10 минут по паре (город, запрос), чтобы сканер
не запускал браузер на каждую ячейку.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from ..models import BBox, Place
from .base import (
    Adapter,
    AdapterError,
    bbox_center,
    clean_query,
    in_bbox,
    join_prefix,
    path_from_env,
    pick,
    pick_prefix,
    read_csv_rows,
    run_command,
    tail,
    to_float,
    to_int,
    utf8_env,
    validate_bbox,
)

log = logging.getLogger(__name__)

# Корень проекта (папка PARSER). Парсеры ищутся рядом с ним, без привязки
# к имени пользователя или букве диска.
ROOT = Path(__file__).resolve().parents[2]
_TWOGIS_SUBDIR = Path("parsers") / "02-2gis-parser-eroloft"


def _default_twogis_dir() -> Path:
    """Найти папку браузерного парсера 2ГИС рядом со скопированным проектом.

    Порядок: ``qwerty/parsers`` (исходная раскладка) →
    ``PARSER/parsers`` (переносимая раскладка).
    """
    for candidate in (ROOT.parent / _TWOGIS_SUBDIR, ROOT / _TWOGIS_SUBDIR):
        if candidate.is_dir():
            return candidate
    # Ничего не нашли: путь остаётся подсказкой в тексте ошибки.
    return ROOT.parent / _TWOGIS_SUBDIR


def _venv_python(directory: Path) -> Path:
    """Интерпретатор виртуального окружения парсера под текущую ОС."""
    if os.name == "nt":
        return directory / ".venv" / "Scripts" / "python.exe"
    return directory / ".venv" / "bin" / "python"


def _resolve_python(directory: Path, env_var: str) -> Path | None:
    """Python парсера: env-переопределение → venv → ``python3``/``python``."""
    raw = os.getenv(env_var, "").strip()
    if raw:
        return Path(raw).expanduser()
    venv = _venv_python(directory)
    if venv.is_file():
        return venv
    # На Windows ``python3`` часто оказывается заглушкой Microsoft Store,
    # поэтому сначала пробуем ``python``.
    names = ("python", "python3") if os.name == "nt" else ("python3", "python")
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _playwright_chromium() -> Path | None:
    """Найти Chromium, скачанный Playwright, под текущую ОС.

    Берётся самая свежая (наибольшая) ревизия ``chromium-<номер>``.
    """
    if sys.platform == "win32":
        base = Path(
            os.getenv("LOCALAPPDATA", "").strip()
            or Path.home() / "AppData" / "Local"
        ) / "ms-playwright"
        rels = ("chrome-win64/chrome.exe", "chrome-win/chrome.exe")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches" / "ms-playwright"
        rels = ("chrome-mac/Chromium.app/Contents/MacOS/Chromium",)
    else:
        base = Path.home() / ".cache" / "ms-playwright"
        rels = ("chrome-linux/chrome", "chrome-linux64/chrome")

    if not base.is_dir():
        return None

    revisions: list[tuple[int, Path]] = []
    try:
        entries = list(base.iterdir())
    except OSError:
        return None
    for entry in entries:
        match = re.fullmatch(r"chromium-(\d+)", entry.name)
        if match is not None:
            revisions.append((int(match.group(1)), entry))
    revisions.sort(key=lambda item: item[0], reverse=True)

    for _, directory in revisions:
        for relative in rels:
            candidate = directory / relative
            if candidate.is_file():
                return candidate
    return None


def _resolve_chromium(explicit: str | Path | None = None) -> Path | None:
    """Chromium: env-переопределение → явный аргумент → поиск Playwright."""
    raw = os.getenv("TWOGIS_CHROME", "").strip()
    if raw:
        return Path(raw).expanduser()
    if explicit:
        return Path(explicit)
    return _playwright_chromium()


DEFAULT_TWOGIS_DIR = _default_twogis_dir()
DEFAULT_CHROMIUM = _resolve_chromium()

CACHE_TTL = 600.0  # 10 минут
API_PAGE_SIZE = 50
API_MAX_PAGES = 10

# Транслитерация для slug города в URL 2ГИС (tyumen, moskva, ...).
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}
_TRANSLIT_TABLE = {ord(char): repl for char, repl in _TRANSLIT.items()}


def city_slug(city: str) -> str:
    """Превратить название города в slug для 2ГИС.

    Латиница возвращается как есть (в нижнем регистре), кириллица
    транслитерируется: ``Тюмень`` → ``tyumen``.
    """
    value = (city or "").strip().lower()
    if not value:
        return ""
    value = value.split(",", 1)[0].strip()
    value = re.sub(r"^г\.\s*", "", value)
    value = re.sub(r"^г\s+", "", value)
    value = re.sub(r"^город\s+", "", value)
    if not value:
        return ""
    if re.fullmatch(r"[a-z0-9-]+", value):
        return value
    value = value.translate(_TRANSLIT_TABLE)
    value = re.sub(r"[^a-z0-9]+", "-", value).strip("-")
    return value


class TwoGisAdapter(Adapter):
    """Поиск организаций через API 2ГИС или браузерный парсер."""

    name = "2gis"
    label = "2ГИС"
    supports_bbox = False

    # Общий кэш для всех экземпляров: (city_slug, query, limit) -> (время, места).
    _cache: dict[tuple[str, str, int], tuple[float, list[Place]]] = {}

    def __init__(
        self,
        city: str | None = None,
        directory: str | Path | None = None,
        chromium: str | Path | None = None,
    ) -> None:
        self.city = (
            city if city is not None else os.getenv("TWOGIS_CITY", "")
        ).strip()
        default_dir = (
            directory if directory is not None else _default_twogis_dir()
        )
        self.directory = path_from_env("TWOGIS_DIR", default_dir)
        self.chromium = _resolve_chromium(chromium)
        self.api_key = os.getenv("TWOGIS_API_KEY", "").strip()
        raw_timeout = os.getenv("TWOGIS_TIMEOUT", "280").strip()
        try:
            self.timeout = max(60.0, min(900.0, float(raw_timeout)))
        except ValueError:
            self.timeout = 280.0
        self._lock = asyncio.Lock()

    def with_city(self, city: str) -> "TwoGisAdapter":
        """Копия адаптера с городом из текущего задания.

        Экземпляры из реестра общие, поэтому город задания нельзя писать в
        общий объект: два одновременных скана разных городов перепутались бы.
        """
        clone = TwoGisAdapter(
            city=city,
            directory=self.directory,
            chromium=self.chromium,
        )
        clone.api_key = self.api_key
        return clone

    @property
    def python_exe(self) -> Path:
        """Интерпретатор парсера: TWOGIS_PYTHON, venv или Python из PATH."""
        exe = _resolve_python(self.directory, "TWOGIS_PYTHON")
        return exe if exe is not None else _venv_python(self.directory)

    @property
    def script(self) -> Path:
        """Путь к parser-2gis.py."""
        return self.directory / "parser-2gis.py"

    def available(self) -> tuple[bool, str]:
        """Проверить API-ключ либо наличие парсера и Chromium."""
        if self.api_key:
            return True, ""
        return self._browser_available()

    def _browser_available(self) -> tuple[bool, str]:
        """Проверить компоненты браузерного режима."""
        problems: list[str] = []
        python_exe = self.python_exe
        if not python_exe.is_file():
            problems.append(f"не найден Python парсера (TWOGIS_PYTHON): {python_exe}")
        if not self.script.is_file():
            problems.append(f"не найден parser-2gis.py (TWOGIS_DIR): {self.script}")
        if self.chromium is None:
            problems.append("не найден Chromium (задайте TWOGIS_CHROME)")
        elif not self.chromium.is_file():
            problems.append(f"не найден Chromium (TWOGIS_CHROME): {self.chromium}")
        if problems:
            return False, "; ".join(problems)
        return True, ""

    async def search(self, query: str, bbox: BBox, limit: int = 60) -> list[Place]:
        """Найти организации в bbox.

        При наличии ключа сначала пробуем официальный API. Если API вернул
        ошибку — целиком переключаемся на браузер (результаты не смешиваются).
        """
        query = clean_query(query)
        if not query:
            raise AdapterError("пустой поисковый запрос")

        bbox = validate_bbox(bbox)
        limit = max(1, int(limit or 60))

        if self.api_key:
            try:
                return await self._search_api(query, bbox, limit)
            except AdapterError as exc:
                ok, reason = self._browser_available()
                if not ok:
                    raise AdapterError(
                        f"API 2ГИС недоступен ({exc}); "
                        f"браузерный парсер тоже недоступен: {reason}"
                    ) from exc
                log.warning(
                    "2gis: API не сработал (%s), перехожу на браузер", exc,
                )

        return await self._search_browser(query, bbox, limit)

    # ------------------------------------------------------------------
    # Браузерный режим
    # ------------------------------------------------------------------

    async def _search_browser(
        self, query: str, bbox: BBox, limit: int,
    ) -> list[Place]:
        """Один прогон по городу с кэшем на 10 минут."""
        ok, reason = self._browser_available()
        if not ok:
            raise AdapterError(reason)

        key = (city_slug(self.city), query.casefold(), limit)
        async with self._lock:
            now = time.monotonic()
            cached = self._cache.get(key)
            if cached is not None and now - cached[0] < CACHE_TTL:
                log.info("2gis: использую кэш для %r", key)
                return self._filter(cached[1], bbox, limit)

            places = await self._run_browser(query, limit)
            self._cache[key] = (time.monotonic(), places)

            # Убираем устаревшие записи, чтобы кэш не рос бесконечно.
            for stale_key, (stamp, _) in list(self._cache.items()):
                if time.monotonic() - stamp >= CACHE_TTL:
                    self._cache.pop(stale_key, None)

            return self._filter(places, bbox, limit)

    def _search_url(self, query: str) -> str:
        """URL выдачи 2ГИС: с городом, если он задан, иначе без него."""
        slug = city_slug(self.city)
        encoded_query = quote(query, safe="")
        if slug:
            return f"https://2gis.ru/{slug}/search/{encoded_query}"
        return f"https://2gis.ru/search/{encoded_query}"

    def _max_records(self, limit: int) -> int:
        """Сколько записей разрешить браузерному парсеру за один прогон."""
        raw = os.getenv("TWOGIS_MAX_RECORDS", "500").strip()
        try:
            value = int(raw)
        except ValueError:
            value = 500
        value = max(10, min(6000, value))
        return max(value, min(limit, 6000))

    async def _run_browser(self, query: str, limit: int) -> list[Place]:
        """Запустить parser-2gis.py и разобрать его CSV."""
        tmp_dir = Path(tempfile.mkdtemp(prefix="parser-2gis-"))
        try:
            output_path = tmp_dir / "results.csv"
            url = self._search_url(query)
            args = [
                str(self.python_exe),
                str(self.script),
                "-i", url,
                "-o", str(output_path),
                "-f", "csv",
                "--chrome.binary_path", str(self.chromium),
                "--chrome.headless", "yes",
                "--chrome.disable-images", "yes",
                "--chrome.silent-browser", "yes",
                "--writer.verbose", "no",
                "--writer.encoding", "utf8",
                "--writer.csv.remove-empty-columns", "no",
                "--parser.max-records", str(self._max_records(limit)),
            ]
            log.info("2gis: браузерный прогон %s", url)
            code, stdout, stderr = await run_command(
                args,
                timeout=self.timeout,
                cwd=tmp_dir,
                env=utf8_env(),
            )

            if code != 0:
                details = tail(stderr) or tail(stdout)
                raise AdapterError(
                    f"parser-2gis.py завершился с кодом {code}"
                    + (f": {details}" if details else "")
                )
            if not output_path.is_file():
                raise AdapterError("parser-2gis.py не создал файл результатов")

            rows = read_csv_rows(output_path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        places: list[Place] = []
        seen: set[str] = set()
        for row in rows:
            place = self._to_place(row)
            if place is None or place.source_id in seen:
                continue
            seen.add(place.source_id)
            places.append(place)

        log.info("2gis: браузерный прогон вернул %d организаций", len(places))
        return places

    @staticmethod
    def _to_place(row: dict[str, str]) -> Place | None:
        """Преобразовать строку CSV 2ГИС в Place."""
        name = pick(row, "Наименование")
        if not name:
            return None

        url = pick(row, "2GIS URL")
        source_id = _firm_id(url)
        if not source_id:
            log.debug("2gis: пропускаю строку без 2GIS URL: %r", name)
            return None

        # raw — только реальные поля источника; тяжёлые дубли не нужны.
        raw = {
            key: value
            for key, value in row.items()
            if value and key not in {
                "Наименование", "Адрес", "Широта", "Долгота", "2GIS URL",
            }
        }

        rating = to_float(pick(row, "Рейтинг"))
        return Place(
            source="2gis",
            source_id=source_id,
            name=name,
            category=pick(row, "Рубрики"),
            address=pick(row, "Адрес"),
            lat=to_float(pick(row, "Широта")),
            lon=to_float(pick(row, "Долгота")),
            phone=join_prefix(row, "Телефон"),
            website=pick_prefix(row, "Веб-сайт"),
            rating=round(rating, 1) if rating and rating > 0 else None,
            reviews_count=to_int(pick(row, "Количество отзывов")),
            hours=pick(row, "Часы работы"),
            url=url,
            raw=raw,
        )

    @staticmethod
    def _filter(places: list[Place], bbox: BBox, limit: int) -> list[Place]:
        """Отфильтровать городской результат по bbox и ограничить количество."""
        result: list[Place] = []
        for place in places:
            if not in_bbox(place.lat, place.lon, bbox):
                continue
            result.append(place)
            if len(result) >= limit:
                break
        return result

    # ------------------------------------------------------------------
    # Официальный API 2ГИС
    # ------------------------------------------------------------------

    async def _search_api(
        self, query: str, bbox: BBox, limit: int,
    ) -> list[Place]:
        """Поиск через Catalog API 3.0 (только при наличии ключа)."""
        center_lat, center_lon = bbox_center(bbox)
        radius = _bbox_radius_m(bbox)
        page_size = min(API_PAGE_SIZE, max(10, limit))
        fields = (
            "items.point,items.address,items.contact_groups,"
            "items.rubrics,items.reviews"
        )
        deadline = time.monotonic() + self.timeout - 5.0

        places: list[Place] = []
        seen: set[str] = set()
        page = 1
        while len(places) < limit and page <= API_MAX_PAGES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError("2GIS API не уложился в таймаут поиска")

            params = {
                "q": query,
                "key": self.api_key,
                "point": f"{center_lon:.6f},{center_lat:.6f}",
                "radius": str(int(radius)),
                "page": str(page),
                "page_size": str(page_size),
                "fields": fields,
                "locale": "ru_RU",
            }
            url = "https://catalog.api.2gis.com/3.0/items?" + urlencode(params)
            log.info("2gis: API-запрос %r, страница %d", query, page)
            data = await asyncio.to_thread(
                _http_json, url, min(20.0, remaining),
            )

            meta = data.get("meta") or {}
            code = meta.get("code", meta.get("status_code"))
            if code is not None and str(code) != "200":
                error = meta.get("error")
                if isinstance(error, dict):
                    message = error.get("message") or error.get("type") or str(error)
                else:
                    message = str(error or f"код {code}")
                raise AdapterError(f"2GIS API: {message}")

            result = data.get("result")
            if not isinstance(result, dict):
                raise AdapterError("2GIS API вернул ответ без result")

            items = result.get("items") or []
            if not items:
                break

            for item in items:
                if not isinstance(item, dict):
                    continue
                place = self._api_item_to_place(item)
                if place is None or place.source_id in seen:
                    continue
                seen.add(place.source_id)
                if not in_bbox(place.lat, place.lon, bbox):
                    continue
                places.append(place)
                if len(places) >= limit:
                    break

            total = to_int(result.get("total"))
            if len(items) < page_size:
                break
            if total is not None and page * page_size >= total:
                break
            page += 1

        return places

    @staticmethod
    def _api_item_to_place(item: dict[str, Any]) -> Place | None:
        """Преобразовать элемент ``result.items`` API в Place."""
        source_id = str(item.get("id") or item.get("external_id") or "").strip()
        if not source_id:
            return None

        name = str(item.get("name") or "").strip()
        if not name:
            return None

        point = item.get("point") or {}
        lat = to_float(point.get("lat"))
        lon = to_float(point.get("lon"))

        address = str(item.get("address_name") or "").strip()
        if not address:
            address_value = item.get("address")
            if isinstance(address_value, dict):
                address = str(
                    address_value.get("name")
                    or address_value.get("address_name")
                    or ""
                ).strip()
            elif isinstance(address_value, str):
                address = address_value.strip()

        rubrics = item.get("rubrics") or []
        categories = [
            str(rubric.get("name") or "").strip()
            for rubric in rubrics
            if isinstance(rubric, dict)
        ]
        category = "; ".join(name for name in categories if name)

        phones: list[str] = []
        emails: list[str] = []
        websites: list[str] = []
        for group in item.get("contact_groups") or []:
            if not isinstance(group, dict):
                continue
            for contact in group.get("contacts") or []:
                if not isinstance(contact, dict):
                    continue
                contact_type = str(contact.get("type") or "").lower()
                value = str(
                    contact.get("text")
                    or contact.get("value")
                    or contact.get("url")
                    or ""
                ).strip()
                if not value:
                    continue
                if contact_type == "phone":
                    phones.append(value)
                elif contact_type == "email":
                    emails.append(value)
                elif contact_type == "website":
                    websites.append(value)

        reviews = item.get("reviews") or {}
        rating = to_float(
            reviews.get("general_rating") or reviews.get("rating")
        )
        reviews_count = to_int(
            reviews.get("general_review_count")
            or reviews.get("review_count")
        )

        url = str(item.get("url") or "").strip()
        if not url:
            # Каноническая карточка 2ГИС по id (не выдуманные данные о бизнесе).
            url = f"https://2gis.ru/firm/{source_id}"

        hours = ""
        schedule = item.get("schedule")
        if isinstance(schedule, dict):
            hours = str(schedule.get("text") or "").strip()
        elif isinstance(schedule, str):
            hours = schedule.strip()

        return Place(
            source="2gis",
            source_id=source_id,
            name=name,
            category=category,
            address=address,
            lat=lat,
            lon=lon,
            phone="; ".join(phones),
            website=websites[0] if websites else "",
            rating=rating,
            reviews_count=reviews_count,
            hours=hours,
            url=url,
            raw=dict(item),
        )


def _firm_id(url: str) -> str:
    """Извлечь id организации из ссылки 2ГИС."""
    if not url:
        return ""
    match = re.search(r"/firm/([^/?#]+)", url)
    if match:
        return match.group(1)
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if not parts:
        return ""
    last = parts[-1]
    if last in {"firm", "search"}:
        return ""
    return last


def _bbox_radius_m(bbox: BBox) -> float:
    """Радиус описанной окружности вокруг bbox в метрах (для API)."""
    min_lon, min_lat, max_lon, max_lat = bbox
    center_lat = (min_lat + max_lat) / 2.0
    width = (max_lon - min_lon) * 111_320.0 * math.cos(math.radians(center_lat))
    height = (max_lat - min_lat) * 111_320.0
    radius = 0.5 * math.hypot(width, height)
    return max(50.0, min(40_000.0, radius))


def _http_json(url: str, timeout: float) -> dict[str, Any]:
    """GET JSON без сторонних зависимостей (вызывается из потока)."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "PARSER/1.0", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except OSError:
            detail = ""
        raise AdapterError(
            f"HTTP {exc.code}" + (f": {detail}" if detail else "")
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AdapterError(f"сетевая ошибка: {exc}") from exc

    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AdapterError("2GIS API вернул не JSON") from exc
    if not isinstance(data, dict):
        raise AdapterError("2GIS API вернул JSON неожиданного формата")
    return data
