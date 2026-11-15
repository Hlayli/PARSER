"""Поиск и склейка дубликатов в существующей базе PARSER.

Запуск из папки PARSER:

    python -m backend.merge_duplicates           # только отчёт
    python -m backend.merge_duplicates --apply   # склеить (с резервной копией)
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from . import db


def _print_group(number: int, group: dict) -> None:
    keep = group["keep"]
    reasons = ", ".join(group["reasons"]) or f"score {group['score']}"
    print(f"\n[{number}] оставить: {keep['name']} — {keep['address'] or 'адрес не указан'}")
    print(
        f"    источник {keep['source']}, телефон {keep['phone'] or '—'}, "
        f"статус {keep['status']}  ({reasons})"
    )
    for drop in group["drop"]:
        print(
            f"    + склеить: {drop['name']} — {drop['address'] or 'адрес не указан'}"
            f"  [источник {drop['source']}, телефон {drop['phone'] or '—'},"
            f" статус {drop['status']}]"
        )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="Найти и склеить дубликаты организаций в базе PARSER."
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="склеить найденные дубликаты (по умолчанию только показать)",
    )
    parser.add_argument(
        "--db", type=Path, default=None,
        help="путь к файлу базы (по умолчанию PARSER_DB или data/parser.db)",
    )
    args = parser.parse_args(argv)

    if args.db is not None:
        db.DB_PATH = args.db

    db.init_db()
    with db.connect() as conn:
        before = db.stats(conn)["total"]
        groups = db.find_duplicate_groups(conn)
        if not groups:
            print(f"Дубликатов не найдено. Карточек в базе: {before}.")
            return 0

        duplicates = sum(len(group["drop"]) for group in groups)
        print(
            f"Найдено групп: {len(groups)}, лишних карточек: {duplicates} "
            f"(всего в базе {before})."
        )
        for number, group in enumerate(groups, start=1):
            _print_group(number, group)

        if not args.apply:
            print(
                "\nЭто только отчёт. Чтобы склеить: "
                "python -m backend.merge_duplicates --apply"
            )
            return 0

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = db.backup_database(db.DB_PATH.with_suffix(f".db.bak-{stamp}"))
        print(f"\nРезервная копия: {backup}")

        removed = db.merge_duplicate_groups(conn)
        after = db.stats(conn)["total"]
        print(f"Склеено карточек: {removed}. Было {before}, стало {after}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
