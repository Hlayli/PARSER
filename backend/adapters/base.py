"""Базовый интерфейс адаптеров-источников и общие утилиты.

Адаптер — тонкая обёртка над проверенным внешним парсером. Он не придумывает
данные: всё, что попало в :class:`~backend.models.Place`, взято из ответа
источника. Общие операции (запуск процесса, чтение CSV, разбор чисел)
вынесены сюда, чтобы не дублировать их в каждом адаптере.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..models import BBox, Place

log = logging.getLogger(__name__)

# Контракт: таймаут на весь search — не больше 120 секунд.
DEFAULT_SEARCH_TIMEOUT = 115.0


class AdapterError(Exception):
    """Источник не смог выполнить поиск: нет инструмента, сеть, парсинг и т.п."""


class Adapter:
    """Общий интерфейс источника."""

    name: str = ""
    label: str = ""
    supports_bbox: bool = True

    def available(self) -> tuple[bool, str]:
        """Вернуть ``(доступен, причина_если_нет)``.

        Проверка не должна ходить в сеть: только наличие бинарников, папок
        и ключей из переменных окружения.
        """
        raise NotImplementedError

    async def search(self, query: str, bbox: BBox, limit: int = 60) -> list[Place]:
        """Выполнить реальный поиск.

        :raises AdapterError: если источник недоступен или вернул ошибку.
        """
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Геометрия
# ---------------------------------------------------------------------------


def validate_bbox(bbox: BBox) -> BBox:
    """Проверить и нормализовать bbox ``(min_lon, min_lat, max_lon, max_lat)``."""
    try:
        min_lon, min_lat, max_lon, max_lat = (float(value) for value in bbox)
    except (TypeError, ValueError) as exc:
        raise AdapterError(
            "некорректный bbox: ожидается (min_lon, min_lat, max_lon, max_lat)"
        ) from exc

    if min_lon > max_lon or min_lat > max_lat:
        raise AdapterError("некорректный bbox: минимумы больше максимумов")
    if not (-180.0 <= min_lon <= 180.0 and -180.0 <= max_lon <= 180.0):
        raise AdapterError("некорректный bbox: долгота вне диапазона -180..180")
    if not (-90.0 <= min_lat <= 90.0 and -90.0 <= max_lat <= 90.0):
        raise AdapterError("некорректный bbox: широта вне диапазона -90..90")
    return (min_lon, min_lat, max_lon, max_lat)


def bbox_center(bbox: BBox) -> tuple[float, float]:
    """Вернуть ``(lat, lon)`` центра bbox."""
    min_lon, min_lat, max_lon, max_lat = bbox
    return ((min_lat + max_lat) / 2.0, (min_lon + max_lon) / 2.0)


def in_bbox(lat: float | None, lon: float | None, bbox: BBox,
            *, keep_missing: bool = True) -> bool:
    """Проверить, лежит ли точка в bbox.

    Если координат нет, по умолчанию точку не отбрасываем: адаптер не должен
    терять реальную запись только из-за пустого поля.
    """
    if lat is None or lon is None:
        return keep_missing
    min_lon, min_lat, max_lon, max_lat = bbox
    return min_lat <= lat <= max_lat and min_lon <= lon <= max_lon


# ---------------------------------------------------------------------------
# Запуск внешних процессов
# ---------------------------------------------------------------------------


def utf8_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Копия окружения с принудительным UTF-8 для дочерних Python-процессов.

    На Windows консольная кодировка часто cp1251/cp1252, и парсеры падают на
    печати кириллицы. ``PYTHONIOENCODING`` решает это, не меняя данные.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if extra:
        env.update({str(key): str(value) for key, value in extra.items()})
    return env


async def run_command(
    args: Sequence[str],
    *,
    timeout: float = DEFAULT_SEARCH_TIMEOUT,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, str, str]:
    """Запустить команду и вернуть ``(код_возврата, stdout, stderr)``.

    Процесс принудительно убивается при превышении таймаута. Вывод читается
    как UTF-8 с заменой некорректных байтов.
    """
    command = [str(part) for part in args]
    program = Path(command[0]).name if command else "процесс"

    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env) if env is not None else None,
        )
    except OSError as exc:
        raise AdapterError(f"не удалось запустить {program}: {exc}") from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.communicate(), timeout=5.0)
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        raise AdapterError(
            f"{program} не завершился за {timeout:.0f} с — процесс остановлен"
        ) from None
    except asyncio.CancelledError:
        # Пользователь остановил скан: не оставляем дочерний парсер висеть.
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise

    code = proc.returncode if proc.returncode is not None else -1
    stdout = (stdout_bytes or b"").decode("utf-8", "replace")
    stderr = (stderr_bytes or b"").decode("utf-8", "replace")
    return code, stdout, stderr


def tail(text: str, limit: int = 600) -> str:
    """Сжать хвост вывода процесса для сообщения об ошибке/лога."""
    compact = re.sub(r"\s+", " ", (text or "")).strip()
    if len(compact) <= limit:
        return compact
    return "…" + compact[-limit:]


# ---------------------------------------------------------------------------
# CSV и значения
# ---------------------------------------------------------------------------


def read_csv_rows(path: str | Path) -> list[dict[str, str]]:
    """Прочитать CSV в список словарей.

    Поддерживает BOM (``utf-8-sig``), первую строку ``sep=,`` (формат 2ГИС)
    и многострочные значения в кавычках.
    """
    file_path = Path(path)
    if not file_path.is_file():
        return []

    text = file_path.read_text(encoding="utf-8-sig", errors="replace")
    first_line, _, rest = text.partition("\n")
    if first_line.strip().lower().startswith("sep="):
        text = rest
    if not text.strip():
        return []

    rows: list[dict[str, str]] = []
    for raw_row in csv.DictReader(io.StringIO(text)):
        row: dict[str, str] = {}
        for key, value in raw_row.items():
            if key is None:  # лишние поля без заголовка
                continue
            row[key.strip()] = (value or "").strip()
        if row:
            rows.append(row)
    return rows


def pick(row: Mapping[str, str], *names: str) -> str:
    """Вернуть первое непустое значение по точным именам колонок."""
    for name in names:
        value = row.get(name)
        if value:
            return value.strip()
    return ""


def pick_prefix(row: Mapping[str, str], prefix: str) -> str:
    """Вернуть первое непустое значение колонки ``prefix`` или ``prefix N``."""
    for key, value in row.items():
        if not value:
            continue
        if key == prefix or key.startswith(prefix + " "):
            return value.strip()
    return ""


def join_prefix(row: Mapping[str, str], prefix: str, sep: str = "; ") -> str:
    """Склеить все непустые значения колонок ``prefix`` и ``prefix N``."""
    values: list[str] = []
    for key, value in row.items():
        if not value:
            continue
        if key == prefix or key.startswith(prefix + " "):
            values.append(value.strip())
    return sep.join(values)


def to_float(value: Any) -> float | None:
    """Разобрать число; пустое или непонятное значение — ``None``."""
    if value is None:
        return None
    text = str(value).strip().replace("\xa0", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def to_int(value: Any) -> int | None:
    """Разобрать целое число; пустое или непонятное значение — ``None``."""
    number = to_float(value)
    if number is None:
        return None
    try:
        return int(number)
    except (ValueError, OverflowError):
        return None


def parse_photos(value: Any) -> list[str]:
    """Безопасно разобрать поле ``images`` (JSON-массив URL).

    Пустая строка, битый JSON или элементы не-URL просто игнорируются.
    """
    if not value:
        return []

    items: list[Any]
    if isinstance(value, (list, tuple)):
        items = list(value)
    else:
        text = str(value).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            # Иногда вместо JSON приходит простая строка с URL(ами).
            if text.lower().startswith(("http://", "https://")):
                items = [part.strip() for part in text.split(",")]
            else:
                return []
        else:
            if isinstance(parsed, list):
                items = parsed
            elif isinstance(parsed, str):
                items = [parsed]
            else:
                return []

    photos: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            item = item.get("url") or item.get("src") or ""
        if not isinstance(item, str):
            continue
        url = item.strip()
        if not url.lower().startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        photos.append(url)
    return photos


def path_from_env(var_name: str, default: str | Path) -> Path:
    """Вернуть путь из переменной окружения или значение по умолчанию."""
    raw = os.getenv(var_name, "").strip()
    return Path(raw).expanduser() if raw else Path(default)


def clean_query(query: str) -> str:
    """Схлопнуть пробелы и переводы строк в запросе."""
    return " ".join(str(query or "").split())
