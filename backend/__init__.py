"""Пакет backend.

При импорте подхватывает ``PARSER/.env`` (если файл есть), не переопределяя
уже заданные переменные окружения. Это позволяет запускать проект двойным
кликом по ``run.bat`` без ручной настройки каждого ключа.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: Path | None = None) -> None:
    """Прочитать простой ``KEY=VALUE`` файл. Ошибки формата пропускаются."""
    env_path = path or (Path(__file__).resolve().parent.parent / ".env")
    if not env_path.is_file():
        return
    try:
        lines = env_path.read_text(encoding="utf-8-sig").splitlines()
    except OSError:
        return

    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = raw.strip()
        # Значение в кавычках может содержать #; без кавычек # после пробела —
        # это комментарий, а не часть пути или ключа.
        if value[:1] in ("'", '"'):
            quote = value[0]
            end = value.find(quote, 1)
            value = value[1:end] if end != -1 else value[1:]
        else:
            for marker in (" #", "\t#"):
                if marker in value:
                    value = value.split(marker, 1)[0]
            value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


load_env()