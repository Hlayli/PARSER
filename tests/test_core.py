"""Тесты слоя хранения: дедупликация, статусы, факты, статистика.

Запуск из папки PARSER:
    python -m unittest discover tests -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Изолированная БД — обязательно до импорта backend.db.
_TEST_DB = Path(tempfile.gettempdir()) / "parser_unittest.db"
os.environ["PARSER_DB"] = str(_TEST_DB)
os.environ.setdefault("PARSER_DEMO", "1")

from backend import db  # noqa: E402
from backend.models import Place  # noqa: E402


def clear_db() -> None:
    db.init_db()
    with db.connect() as conn:
        for table in ("fact", "business_source", "business", "scan_cell", "scan_job"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


class DbTestCase(unittest.TestCase):
    def setUp(self) -> None:
        clear_db()
        self.conn = db.connect()

    def tearDown(self) -> None:
        self.conn.close()

    def place(self, **overrides) -> Place:
        data = dict(
            source="google",
            source_id="g-1",
            name="Кофейня Ромашка",
            category="Кофейня",
            address="ул. Ленина, 1",
            lat=57.1500,
            lon=65.5300,
            phone="+7 (900) 111-22-33",
            website="https://example.com",
            rating=4.7,
            reviews_count=12,
        )
        data.update(overrides)
        return Place(**data)

    def test_insert_and_read(self) -> None:
        business_id, is_new = db.upsert_place(self.conn, self.place())
        self.assertTrue(is_new)

        lead = db.get_lead(self.conn, business_id)
        self.assertIsNotNone(lead)
        self.assertEqual(lead["name"], "Кофейня Ромашка")
        self.assertEqual(lead["phone"], "+7 (900) 111-22-33")
        self.assertEqual(lead["sources"], ["google"])
        self.assertEqual(lead["status"], "new")
        self.assertFalse(lead["is_demo"])

    def test_dedupe_by_phone(self) -> None:
        first, _ = db.upsert_place(self.conn, self.place())
        second, is_new = db.upsert_place(
            self.conn,
            self.place(
                source="2gis",
                source_id="2g-1",
                name="Ромашка",
                phone="8 900 111-22-33",
                address="Ленина 1",
            ),
        )
        self.assertFalse(is_new)
        self.assertEqual(first, second)

        lead = db.get_lead(self.conn, first)
        self.assertEqual(set(lead["sources"]), {"google", "2gis"})
        self.assertEqual(db.stats(self.conn)["total"], 1)

    def test_dedupe_by_name_and_distance(self) -> None:
        first, _ = db.upsert_place(
            self.conn, self.place(phone="", source_id="g-1")
        )
        second, is_new = db.upsert_place(
            self.conn,
            self.place(source="yandex", source_id="y-1", phone="", lat=57.1505,
                       lon=65.5305),
        )
        self.assertFalse(is_new)
        self.assertEqual(first, second)

    def test_no_merge_by_name_without_coordinates(self) -> None:
        db.upsert_place(self.conn, self.place(
            phone="", source_id="g-1", lat=None, lon=None))
        second, is_new = db.upsert_place(self.conn, self.place(
            source="2gis", source_id="2g-1", phone="", lat=None, lon=None))
        self.assertTrue(is_new)
        self.assertNotEqual(second, "")
        self.assertEqual(db.stats(self.conn)["total"], 2)

    def test_far_places_not_merged(self) -> None:
        db.upsert_place(self.conn, self.place(phone="", source_id="g-1"))
        second, is_new = db.upsert_place(
            self.conn,
            self.place(source="yandex", source_id="y-1", phone="", lat=57.2500,
                       lon=65.5300),
        )
        self.assertTrue(is_new)
        self.assertEqual(db.stats(self.conn)["total"], 2)

    def test_demo_flag(self) -> None:
        business_id, _ = db.upsert_place(self.conn, self.place(
            source="demo", source_id="demo-1", name="ДЕМО — Кофейня"))
        lead = db.get_lead(self.conn, business_id)
        self.assertEqual(lead["is_demo"], 1)

    def test_status_favorite_note(self) -> None:
        business_id, _ = db.upsert_place(self.conn, self.place())

        self.assertTrue(db.set_status(self.conn, business_id, status="called"))
        self.assertTrue(db.set_status(self.conn, business_id, is_favorite=True))
        self.assertTrue(db.set_status(self.conn, business_id, note="перезвонить"))

        lead = db.get_lead(self.conn, business_id)
        self.assertEqual(lead["status"], "called")
        self.assertEqual(lead["is_favorite"], 1)
        self.assertEqual(lead["note"], "перезвонить")

        with self.assertRaises(ValueError):
            db.set_status(self.conn, business_id, status="выдуманный")

        self.assertFalse(db.set_status(self.conn, "нет-такого", status="called"))

    def test_facts_dedupe(self) -> None:
        business_id, _ = db.upsert_place(self.conn, self.place())

        self.assertTrue(db.add_fact(self.conn, business_id, "email",
                                    "info@example.com", "https://example.com/contacts"))
        self.assertFalse(db.add_fact(self.conn, business_id, "email",
                                     "info@example.com", "https://example.com/"))

        self.assertTrue(db.add_fact(self.conn, business_id, "price",
                                    "Кофе — 150 ₽", "https://example.com/menu"))
        self.assertFalse(db.add_fact(self.conn, business_id, "price", "",
                                     "https://example.com/menu"))

        lead = db.get_lead(self.conn, business_id)
        self.assertEqual(len(lead["facts"]["email"]), 1)
        self.assertEqual(lead["facts"]["email"][0]["source_url"],
                         "https://example.com/contacts")
        self.assertEqual(len(lead["facts"]["price"]), 1)

    def test_list_filters_and_sort(self) -> None:
        first, _ = db.upsert_place(self.conn, self.place(source_id="g-1"))
        second, _ = db.upsert_place(self.conn, self.place(
            source_id="y-1", name="Пекарня Крошка", phone="+7 900 222-33-44",
            lat=57.2, lon=65.6, rating=None, source="yandex"))
        db.set_status(self.conn, first, status="called", is_favorite=True)

        total, items = db.list_leads(self.conn)
        self.assertEqual(total, 2)
        self.assertEqual(len(items), 2)

        total, items = db.list_leads(self.conn, status="called")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], first)

        total, items = db.list_leads(self.conn, fav=True)
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], first)

        total, items = db.list_leads(self.conn, q="пекарня")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], second)

        total, items = db.list_leads(self.conn, source="yandex")
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["id"], second)

    def test_phone_normalization(self) -> None:
        self.assertEqual(db.normalize_phone("8 (900) 111-22-33"), "+79001112233")
        self.assertEqual(db.normalize_phone("+7 900 111 22 33"), "+79001112233")
        self.assertEqual(db.normalize_phone("9001112233"), "+79001112233")
        self.assertEqual(db.normalize_phone("123"), "")
        self.assertEqual(db.normalize_phone(""), "")

    def test_make_grid_squares(self) -> None:
        from backend.scanner import make_grid

        cells = make_grid((65.50, 57.10, 65.54, 57.14), 0.02)
        self.assertEqual(len(cells), 4)  # 2 x 2
        for ix, iy, bbox in cells:
            self.assertAlmostEqual(bbox[2] - bbox[0], 0.02, places=6)
            self.assertAlmostEqual(bbox[3] - bbox[1], 0.02, places=6)

    def test_make_grid_limit(self) -> None:
        from backend.scanner import MAX_CELLS, make_grid

        cells = make_grid((60.0, 50.0, 70.0, 60.0), 0.001)
        self.assertLessEqual(len(cells), MAX_CELLS)


class ImageKeyTestCase(unittest.TestCase):
    def test_imgproxy_variants_collapse(self) -> None:
        from backend.enrichment import _image_key

        first = ("https://imgproxy.example.com/AAA=/full-fit-in/256x256/"
                 "filters:format(webp)/https%3A%2F%2Fcdn.example.com%2Fphoto.jpg")
        second = ("https://imgproxy.example.com/BBB=/full-fit-in/128x128/"
                  "filters:format(webp)/https%3A%2F%2Fcdn.example.com%2Fphoto.jpg")
        self.assertEqual(_image_key(first), "https://cdn.example.com/photo.jpg")
        self.assertEqual(_image_key(first), _image_key(second))

    def test_different_photos_not_merged(self) -> None:
        from backend.enrichment import _image_key

        first = "https://cdn.example.com/photo.jpg?id=1"
        second = "https://cdn.example.com/photo.jpg?id=2"
        self.assertNotEqual(_image_key(first), _image_key(second))


class CategoriesTestCase(unittest.TestCase):
    def test_catalog_is_broad_and_queries_unique(self) -> None:
        from backend import categories as catalog

        data = catalog.catalog()
        self.assertGreaterEqual(data["total"], 100)
        self.assertEqual(len(data["groups"]), 12)
        queries = [
            item[2] for group in catalog.GROUPS for item in group["categories"]
        ]
        self.assertEqual(len(queries), len(set(queries)))

    def test_default_selection_and_custom_query(self) -> None:
        from backend import categories as catalog

        queries = catalog.resolve_queries(
            ["dentistry", "tire", "dentistry"], "  шиномонтаж  "
        )
        # Повтор категории и повтор текста не дублируются.
        self.assertEqual(queries, ["стоматология", "шиномонтаж"])

    def test_all_expands_to_every_category(self) -> None:
        from backend import categories as catalog

        queries = catalog.resolve_queries(["all"])
        self.assertEqual(len(queries), catalog.catalog()["total"])

    def test_unknown_category_rejected(self) -> None:
        from backend import categories as catalog

        with self.assertRaises(ValueError):
            catalog.resolve_queries(["no-such-category"])


class MultiQueryScanTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_two_queries_run_as_two_passes(self) -> None:
        """Каждая категория — отдельная волна по всей сетке."""
        from backend.scanner import ScanManager

        clear_db()
        manager = ScanManager()
        job_id = manager.start(
            ["кофейня", "стоматология"],
            (65.50, 57.10, 65.54, 57.14),
            0.02,
            ["demo"],
        )
        job = manager.job(job_id)
        self.assertEqual(job["queries"], ["кофейня", "стоматология"])
        self.assertEqual(job["queries_total"], 2)
        self.assertEqual(job["cells_total"], 4)

        await manager._tasks[job_id]

        job = manager.job(job_id)
        self.assertEqual(job["status"], "done")
        self.assertEqual(job["query_index"], 1)
        self.assertEqual(job["current_query"], "стоматология")
        self.assertEqual(job["cells_done"], 4)
        self.assertGreater(job["leads_found"], 0)

        with db.connect() as conn:
            statuses = {
                row["status"]
                for row in conn.execute("SELECT status FROM scan_cell")
            }
        self.assertEqual(statuses, {"done"})


class ProcessCancelTestCase(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_command_kills_child(self) -> None:
        """Остановка скана не должна оставлять дочерний парсер в памяти."""
        from backend.adapters.base import run_command

        marker = Path(tempfile.gettempdir()) / "parser_cancel_marker.txt"
        marker.unlink(missing_ok=True)
        script = (
            "import pathlib, sys, time\n"
            "time.sleep(3)\n"
            f"pathlib.Path({str(marker)!r}).write_text('alive', encoding='utf-8')\n"
        )
        task = asyncio.create_task(
            run_command([sys.executable, "-c", script], timeout=30)
        )
        await asyncio.sleep(0.6)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(3.2)
        self.assertFalse(marker.exists(), "дочерний процесс пережил отмену")
        marker.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()