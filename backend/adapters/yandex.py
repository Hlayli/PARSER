"""Адаптер Яндекс.Карт на базе yamaps_parser.py.

Используется готовый парсер из ``parsers/03-yandex-maps-parser``. Он умеет
ходить по сетке внутри bbox, поэтому адаптер передаёт ``--bbox`` и явный URL
поиска с центром/масштабом (``--url``), чтобы не зависеть от геокодинга
названия города.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from ..models import BBox, Place
from .base import (
    Adapter,
    AdapterError,
    DEFAULT_SEARCH_TIMEOUT,
    clean_query,
    path_from_env,
    pick,
    read_csv_rows,
    run_command,
    tail,
    to_float,
    to_int,
    utf8_env,
    validate_bbox,
)

log = logging.getLogger(__name__)

DEFAULT_YAMAPS_DIR = (
    r"C:\Users\Hlusha\Desktop\qwerty\parsers\03-yandex-maps-parser"
)


class YandexAdapter(Adapter):
    """Поиск организаций через парсер Яндекс.Карт."""

    name = "yandex"
    label = "Яндекс.Карты"
    supports_bbox = True

    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = path_from_env(
            "YAMAPS_DIR", directory or DEFAULT_YAMAPS_DIR,
        )

    @property
    def python_exe(self) -> Path:
        """Интерпретатор виртуального окружения парсера."""
        return self.directory / ".venv" / "Scripts" / "python.exe"

    @property
    def script(self) -> Path:
        """Путь к yamaps_parser.py."""
        return self.directory / "yamaps_parser.py"

    def available(self) -> tuple[bool, str]:
        """Проверить наличие venv и скрипта парсера."""
        problems: list[str] = []
        if not self.python_exe.is_file():
            problems.append(f"не найден интерпретатор: {self.python_exe}")
        if not self.script.is_file():
            problems.append(f"не найден yamaps_parser.py: {self.script}")
        if problems:
            return False, "; ".join(problems)
        return True, ""

    async def search(self, query: str, bbox: BBox, limit: int = 60) -> list[Place]:
        """Найти организации внутри bbox через сетку Яндекс.Карт."""
        query = clean_query(query)
        if not query:
            raise AdapterError("пустой поисковый запрос")

        ok, reason = self.available()
        if not ok:
            raise AdapterError(reason)

        bbox = validate_bbox(bbox)
        min_lon, min_lat, max_lon, max_lat = bbox
        limit = max(1, int(limit or 60))

        width = max_lon - min_lon
        height = max_lat - min_lat
        center_lon = (min_lon + max_lon) / 2.0
        center_lat = (min_lat + max_lat) / 2.0
        span_lon = max(width, 0.001)
        span_lat = max(height, 0.001)

        # URL строим сами: позиционные place/query парсер всё равно игнорирует
        # при --url, зато не тратим время на геокодинг города.
        search_url = "https://yandex.ru/maps/?" + urlencode({
            "text": query,
            "ll": f"{center_lon:.6f},{center_lat:.6f}",
            "spn": f"{span_lon:.6f},{span_lat:.6f}",
        })

        # Ограничиваем число ячеек сетки: для ячейки сканера (обычно ~0.02°)
        # получится один проход, для большого bbox — максимум 3x3.
        cell_lon = max(0.03, width / 3.0 if width > 0 else 0.03)
        cell_lat = max(0.02, height / 3.0 if height > 0 else 0.02)
        bbox_text = (
            f"{min_lon:.6f},{min_lat:.6f},"
            f"{max_lon:.6f},{max_lat:.6f}"
        )

        tmp_dir = Path(tempfile.mkdtemp(prefix="parser-yandex-"))
        try:
            output_path = tmp_dir / "results.csv"
            args = [
                str(self.python_exe),
                str(self.script),
                "bbox",  # позиционный place, игнорируется при --url
                query,
                "--url", search_url,
                "--bbox", bbox_text,
                "--cell", f"{cell_lon:.6f},{cell_lat:.6f}",
                "--limit", str(limit),
                "-o", str(output_path),
            ]
            log.info(
                "yandex: запрос %r, bbox %s, limit=%d",
                query, bbox_text, limit,
            )
            code, stdout, stderr = await run_command(
                args,
                timeout=DEFAULT_SEARCH_TIMEOUT,
                cwd=tmp_dir,
                env=utf8_env(),
            )

            if code != 0:
                combined = f"{stderr}\n{stdout}".lower()
                if "ничего не собрано" in combined:
                    log.info("yandex: по запросу %r ничего не найдено", query)
                    return []
                details = tail(stderr) or tail(stdout)
                raise AdapterError(
                    f"yamaps_parser.py завершился с кодом {code}"
                    + (f": {details}" if details else "")
                )
            if not output_path.is_file():
                raise AdapterError("yamaps_parser.py не создал файл результатов")

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
            if len(places) >= limit:
                break

        log.info("yandex: получено %d организаций", len(places))
        return places

    @staticmethod
    def _to_place(row: dict[str, str]) -> Place | None:
        """Преобразовать строку CSV парсера в Place."""
        name = pick(row, "name")
        if not name:
            return None

        source_id = pick(row, "id", "url")
        if not source_id:
            return None

        rating = to_float(pick(row, "rating"))
        return Place(
            source="yandex",
            source_id=source_id,
            name=name,
            category=pick(row, "category"),
            address=pick(row, "address"),
            lat=to_float(pick(row, "lat")),
            lon=to_float(pick(row, "lon")),
            phone=pick(row, "phone"),
            website=pick(row, "site"),
            rating=round(rating, 1) if rating and rating > 0 else None,
            reviews_count=to_int(pick(row, "reviews")),
            hours=pick(row, "hours"),
            url=pick(row, "url"),
            raw={key: value for key, value in row.items() if value},
        )
