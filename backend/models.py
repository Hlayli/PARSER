"""Модели данных PARSER."""

from dataclasses import dataclass, field

# (min_lon, min_lat, max_lon, max_lat) — как у yamaps_parser --bbox
BBox = tuple[float, float, float, float]


@dataclass
class Place:
    """Одна организация, как её вернул источник.

    Никаких вычисленных или додуманных полей: только то, что реально пришло.
    """

    source: str
    source_id: str
    name: str
    category: str = ""
    address: str = ""
    lat: float | None = None
    lon: float | None = None
    phone: str = ""
    website: str = ""
    rating: float | None = None
    reviews_count: int | None = None
    price_range: str = ""
    hours: str = ""
    photos: list[str] = field(default_factory=list)
    url: str = ""
    raw: dict = field(default_factory=dict)

    def is_empty(self) -> bool:
        return not self.name.strip()
