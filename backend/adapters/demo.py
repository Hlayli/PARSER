"""Демонстрационный адаптер.

Включается только при ``PARSER_DEMO=1``. Это единственный источник, которому
разрешено синтетическое содержимое; все записи явно помечены префиксом
«ДЕМО — », ``source="demo"`` и ``raw["demo"] = True``.
"""

from __future__ import annotations

import os

from ..models import BBox, Place
from .base import Adapter, AdapterError, validate_bbox

_DEMO_ORGS = [
    ("ДЕМО — Кофейня «Зерно»", "Кофейня"),
    ("ДЕМО — Пекарня «Крошка»", "Пекарня"),
    ("ДЕМО — Ресторан «Восход»", "Ресторан"),
    ("ДЕМО — Столовая «Домашняя»", "Столовая"),
    ("ДЕМО — Кофейня «Пар»", "Кофейня"),
    ("ДЕМО — Пиццерия «Круг»", "Пиццерия"),
    ("ДЕМО — Чайная «Лист»", "Чайная"),
    ("ДЕМО — Кондитерская «Сахар»", "Кондитерская"),
    ("ДЕМО — Бар «Вечер»", "Бар"),
    ("ДЕМО — Кулинария «Утро»", "Кулинария"),
]


class DemoAdapter(Adapter):
    """Синтетические организации для проверки интерфейса без парсеров."""

    name = "demo"
    label = "Демо-данные"
    supports_bbox = True

    def available(self) -> tuple[bool, str]:
        """Доступен только при явно включённом демо-режиме."""
        if os.getenv("PARSER_DEMO") == "1":
            return True, ""
        return False, "демо-режим выключен: задайте PARSER_DEMO=1"

    async def search(self, query: str, bbox: BBox, limit: int = 60) -> list[Place]:
        """Сгенерировать 8–12 демо-организаций внутри bbox."""
        ok, reason = self.available()
        if not ok:
            raise AdapterError(reason)

        bbox = validate_bbox(bbox)
        min_lon, min_lat, max_lon, max_lat = bbox
        width = max_lon - min_lon
        height = max_lat - min_lat
        limit = max(1, int(limit or 60))
        count = min(len(_DEMO_ORGS), limit)

        places: list[Place] = []
        for index in range(count):
            name, category = _DEMO_ORGS[index]
            # Раскладываем точки сеткой 4x3 с отступом от границ bbox.
            column = index % 4
            row = index // 4
            u = 0.12 + 0.76 * (column + 0.5) / 4.0
            v = 0.12 + 0.76 * (row + 0.5) / 3.0
            lat = round(min_lat + height * v, 6)
            lon = round(min_lon + width * u, 6)

            places.append(Place(
                source="demo",
                source_id=f"demo-{index + 1}",
                name=name,
                category=category,
                address=f"ДЕМО, ул. Примерная, {index + 1}",
                lat=lat,
                lon=lon,
                phone=f"+7 (900) 000-00-{index + 1:02d}",
                rating=round(4.0 + (index % 8) * 0.1, 1),
                reviews_count=10 + index * 17,
                price_range=("₽", "₽₽", "₽₽₽")[index % 3],
                hours="Ежедневно 09:00–21:00",
                raw={"demo": True},
            ))
        return places
