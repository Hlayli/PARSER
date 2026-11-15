"""Ручная проверка реального адаптера: python tests/real_source.py google|yandex|2gis"""

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.adapters.registry import all_adapters  # noqa: E402

BBOX = (65.50, 57.10, 65.52, 57.12)  # Тюмень, центр
CITY_BBOX = (65.45, 57.05, 65.65, 57.25)  # весь город — для 2ГИС


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "google"
    adapter = all_adapters()[name]
    ok, reason = adapter.available()
    print(f"{name}: available={ok} reason={reason!r}")
    if not ok:
        return

    started = time.monotonic()
    bbox = CITY_BBOX if name == "2gis" else BBOX
    try:
        places = await adapter.search("кофейни", bbox, limit=5)
    except Exception as exc:  # noqa: BLE001
        print(f"{name}: ОШИБКА за {time.monotonic() - started:.1f} с: {exc}")
        return

    print(f"{name}: {len(places)} мест за {time.monotonic() - started:.1f} с")
    for place in places:
        print(f"- {place.name} | {place.category} | {place.address} | "
              f"{place.phone} | {place.website} | {place.rating} | "
              f"{place.lat},{place.lon} | фото: {len(place.photos)}")


asyncio.run(main())