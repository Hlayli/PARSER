"""Адаптер Google Maps на базе gmsc.exe (gosom/google-maps-scraper).

Проверенный запуск:
    gmsc.exe -input q.txt -results out.csv -depth 2 -lang ru -geo "57.15,65.53"

Важно: ``-geo`` принимает ``lat,lon`` (широта, долгота), а не наоборот.
Радиус поиска рассчитывается по размеру ячейки, чтобы сканирование шло
квадратами, а не перекрывало полгорода на каждом шаге.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import tempfile
from pathlib import Path

from ..models import BBox, Place
from .base import (
    Adapter,
    AdapterError,
    DEFAULT_SEARCH_TIMEOUT,
    bbox_center,
    clean_query,
    in_bbox,
    parse_photos,
    path_from_env,
    pick,
    read_csv_rows,
    run_command,
    tail,
    to_float,
    to_int,
    validate_bbox,
)

log = logging.getLogger(__name__)

# Корень проекта (папка PARSER). Парсеры ищутся рядом с ним, без привязки
# к имени пользователя или букве диска.
ROOT = Path(__file__).resolve().parents[2]
_GMSC_SUBDIR = Path("parsers") / "01-gosom-google-maps-scraper" / "gmsc.exe"


def _default_gmsc_path() -> Path:
    """Найти gmsc.exe в PATH или рядом со скопированным проектом.

    Порядок: PATH → ``qwerty/parsers`` (исходная раскладка) →
    ``PARSER/parsers`` (переносимая раскладка).
    """
    for name in ("gmsc", "gmsc.exe"):
        found = shutil.which(name)
        if found:
            return Path(found)
    for candidate in (ROOT.parent / _GMSC_SUBDIR, ROOT / _GMSC_SUBDIR):
        if candidate.is_file():
            return candidate
    # Ничего не нашли: путь остаётся подсказкой в тексте ошибки.
    return ROOT.parent / _GMSC_SUBDIR


DEFAULT_GMSC_PATH = _default_gmsc_path()

# Поля, которые могут быть очень большими и не нужны в payload.
_HEAVY_FIELDS = {"images", "user_reviews", "user_reviews_extended"}


class GoogleAdapter(Adapter):
    """Поиск организаций через Google Maps Scraper."""

    name = "google"
    label = "Google Maps"
    supports_bbox = True

    def __init__(self, binary: str | Path | None = None) -> None:
        default = binary if binary is not None else _default_gmsc_path()
        self.binary = path_from_env("GMSC_PATH", default)

    def available(self) -> tuple[bool, str]:
        """Проверить наличие gmsc.exe."""
        if not self.binary.is_file():
            return False, f"не найден gmsc.exe (GMSC_PATH): {self.binary}"
        return True, ""

    async def search(self, query: str, bbox: BBox, limit: int = 60) -> list[Place]:
        """Найти организации вокруг центра bbox.

        gmsc не умеет произвольный bbox в режиме ``-geo``, поэтому поиск идёт
        от центра ячейки с радиусом по умолчанию (10 км). Сканер дедуплицирует
        результаты между ячейками.
        """
        query = clean_query(query)
        if not query:
            raise AdapterError("пустой поисковый запрос")

        ok, reason = self.available()
        if not ok:
            raise AdapterError(reason)

        bbox = validate_bbox(bbox)
        center_lat, center_lon = bbox_center(bbox)
        radius = self._radius_m(bbox)
        limit = max(1, int(limit or 60))
        depth = self._depth()

        tmp_dir = Path(tempfile.mkdtemp(prefix="parser-google-"))
        try:
            input_path = tmp_dir / "queries.txt"
            output_path = tmp_dir / "results.csv"
            input_path.write_text(query + "\n", encoding="utf-8")

            args = [
                str(self.binary),
                "-input", str(input_path),
                "-results", str(output_path),
                "-depth", str(depth),
                "-lang", "ru",
                "-c", "1",
                "-geo", f"{center_lat:.6f},{center_lon:.6f}",
                "-radius", f"{radius:.0f}",
            ]
            log.info(
                "google: запрос %r, центр %.5f,%.5f, радиус %d м, depth=%s",
                query, center_lat, center_lon, radius, depth,
            )
            code, stdout, stderr = await run_command(
                args, timeout=DEFAULT_SEARCH_TIMEOUT, cwd=tmp_dir,
            )

            if code != 0:
                details = tail(stderr) or tail(stdout)
                raise AdapterError(
                    f"gmsc.exe завершился с кодом {code}"
                    + (f": {details}" if details else "")
                )
            if not output_path.is_file():
                raise AdapterError("gmsc.exe не создал файл результатов")

            rows = read_csv_rows(output_path)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        places: list[Place] = []
        seen: set[str] = set()
        min_lon, min_lat, max_lon, max_lat = bbox
        # 10 % запаса на погрешность координат и пограничные точки.
        pad_lon = (max_lon - min_lon) * 0.1
        pad_lat = (max_lat - min_lat) * 0.1
        padded = (min_lon - pad_lon, min_lat - pad_lat,
                  max_lon + pad_lon, max_lat + pad_lat)
        for row in rows:
            place = self._to_place(row)
            if place is None or place.source_id in seen:
                continue
            if not in_bbox(place.lat, place.lon, padded):
                continue
            seen.add(place.source_id)
            places.append(place)
            if len(places) >= limit:
                break

        log.info("google: получено %d организаций", len(places))
        return places

    def _depth(self) -> int:
        """Глубина прокрутки выдачи; по умолчанию как в проверенном запуске."""
        raw = os.getenv("GMSC_DEPTH", "2").strip()
        try:
            return max(1, min(10, int(raw)))
        except ValueError:
            return 2

    @staticmethod
    def _radius_m(bbox: BBox) -> int:
        """Радиус поиска, покрывающий ячейку, но не весь город.

        Половина диагонали bbox + 10 % запаса. Ограничение — 300..10000 м,
        чтобы крошечная ячейка не теряла соседние дома, а большая не
        превращалась в общегородской поиск.
        """
        raw = os.getenv("GMSC_RADIUS", "").strip()
        if raw:
            try:
                return max(100, min(50000, int(float(raw))))
            except ValueError:
                log.warning("GMSC_RADIUS=%r не число, считаю по ячейке", raw)

        min_lon, min_lat, max_lon, max_lat = bbox
        mid_lat = (min_lat + max_lat) / 2.0
        height_m = (max_lat - min_lat) * 111_320.0
        width_m = (max_lon - min_lon) * 111_320.0 * math.cos(math.radians(mid_lat))
        half_diagonal = math.hypot(width_m, height_m) / 2.0
        return int(max(300.0, min(10_000.0, half_diagonal * 1.1)))

    @staticmethod
    def _to_place(row: dict[str, str]) -> Place | None:
        """Преобразовать строку CSV gmsc в Place без домысливания полей."""
        name = pick(row, "title")
        if not name:
            return None

        # У gmsc идентификаторы могут быть в разных колонках в зависимости
        # от версии/режима. Берём первый реально заполненный.
        source_id = pick(row, "place_id", "cid", "data_id", "link")
        if not source_id:
            return None

        raw = {
            key: value
            for key, value in row.items()
            if value and key not in _HEAVY_FIELDS
        }

        photos = parse_photos(pick(row, "images"))
        # В gmsc 1.18.x массив images часто пуст, но thumbnail — реальная
        # ссылка на изображение места, её и показываем как фото источника.
        thumbnail = pick(row, "thumbnail")
        if thumbnail.startswith(("http://", "https://")) and thumbnail not in photos:
            photos.append(thumbnail)

        rating = to_float(pick(row, "review_rating"))
        return Place(
            source="google",
            source_id=source_id,
            name=name,
            category=pick(row, "category"),
            address=pick(row, "address", "complete_address"),
            lat=to_float(pick(row, "latitude")),
            lon=to_float(pick(row, "longitude")),
            phone=pick(row, "phone"),
            website=pick(row, "website"),
            rating=rating if rating and rating > 0 else None,
            reviews_count=to_int(pick(row, "review_count")),
            price_range=pick(row, "price_range"),
            hours=pick(row, "open_hours"),
            photos=photos,
            url=pick(row, "link"),
            raw=raw,
        )
