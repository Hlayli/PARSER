"""Реестр источников."""

from __future__ import annotations

import logging

from .base import Adapter
from .demo import DemoAdapter
from .google import GoogleAdapter
from .twogis import TwoGisAdapter
from .yandex import YandexAdapter

log = logging.getLogger(__name__)

_ORDER = ("google", "yandex", "2gis")
_instances: dict[str, Adapter] | None = None


def all_adapters() -> dict[str, Adapter]:
    """Все известные адаптеры, включая недоступные (для /api/health)."""
    global _instances
    if _instances is None:
        _instances = {
            "google": GoogleAdapter(),
            "yandex": YandexAdapter(),
            "2gis": TwoGisAdapter(),
            "demo": DemoAdapter(),
        }
    return _instances


def get_adapters(names: list[str], only_available: bool = True) -> list[Adapter]:
    """Вернуть адаптеры по именам в стабильном порядке.

    :param only_available: отбросить недоступные источники.
    """
    registry = all_adapters()
    wanted = [name for name in names if name in registry]
    # сохраняем порядок google → yandex → 2gis, demo — последним
    wanted.sort(key=lambda n: (_ORDER.index(n) if n in _ORDER else len(_ORDER), n))

    result: list[Adapter] = []
    for name in wanted:
        adapter = registry[name]
        if only_available:
            try:
                ok, reason = adapter.available()
            except Exception as exc:  # noqa: BLE001
                log.warning("adapter %s.available() упал: %s", name, exc)
                continue
            if not ok:
                log.info("источник %s недоступен: %s", name, reason)
                continue
        result.append(adapter)
    return result


def availability() -> list[dict]:
    """Сводка доступности для health-эндпоинта."""
    result = []
    for name, adapter in all_adapters().items():
        try:
            ok, reason = adapter.available()
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, str(exc)
        result.append({"name": name, "label": adapter.label,
                       "available": bool(ok), "reason": reason,
                       "supports_bbox": adapter.supports_bbox})
    return result
