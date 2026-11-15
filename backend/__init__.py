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
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_env()