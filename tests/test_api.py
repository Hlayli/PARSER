"""Тесты HTTP-API через TestClient.

Запуск из папки PARSER:
    python -m unittest discover tests -v
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

# Изолированная БД и демо-источник — до импорта приложения.
_TEST_DB = Path(tempfile.gettempdir()) / "parser_unittest.db"
os.environ["PARSER_DB"] = str(_TEST_DB)
os.environ["PARSER_DEMO"] = "1"

from fastapi.testclient import TestClient  # noqa: E402

from backend import db  # noqa: E402
from backend.app import app  # noqa: E402


def clear_db() -> None:
    db.init_db()
    with db.connect() as conn:
        for table in ("fact", "business_source", "business", "scan_cell", "scan_job"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


class ApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)

    def setUp(self) -> None:
        clear_db()

    def test_health(self) -> None:
        response = self.client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        names = {adapter["name"] for adapter in body["adapters"]}
        self.assertIn("demo", names)
        self.assertIn("google", names)

    def test_index_and_static(self) -> None:
        index = self.client.get("/")
        self.assertEqual(index.status_code, 200)
        self.assertIn("PARSER", index.text)

        script = self.client.get("/static/app.js")
        self.assertEqual(script.status_code, 200)
        self.assertIn("EventSource", script.text)

    def test_demo_scan_and_leads(self) -> None:
        response = self.client.post("/api/scan", json={
            "query": "кофейни",
            "bbox": [65.50, 57.10, 65.52, 57.12],
            "cell_deg": 0.05,
            "sources": ["demo"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        job_id = response.json()["job_id"]
        self.assertTrue(job_id)

        final = None
        for _ in range(40):
            result = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(result.status_code, 200)
            final = result.json()
            if final["status"] in ("done", "stopped", "error"):
                break
            time.sleep(0.25)

        self.assertEqual(final["status"], "done")
        self.assertEqual(final["cells_done"], final["cells_total"])
        self.assertGreaterEqual(final["leads_found"], 10)

        leads = self.client.get("/api/leads", params={"limit": 100}).json()
        self.assertGreaterEqual(leads["total"], 10)
        self.assertTrue(all(item["is_demo"] == 1 for item in leads["items"]))
        self.assertTrue(all(item["name"].startswith("ДЕМО") for item in leads["items"]))
        self.assertTrue(all(item["sources"] for item in leads["items"]))

        first = leads["items"][0]
        detail = self.client.get(f"/api/leads/{first['id']}").json()
        self.assertEqual(detail["name"], first["name"])
        self.assertIn("facts", detail)

    def test_patch_status_and_note(self) -> None:
        # создаём лид напрямую, чтобы не зависеть от скана
        from backend.models import Place

        with db.connect() as conn:
            business_id, _ = db.upsert_place(conn, Place(
                source="demo", source_id="demo-test-1",
                name="ДЕМО — Тестовая кофейня", phone="+7 900 000-00-99",
                lat=57.11, lon=65.51,
            ))

        response = self.client.patch(f"/api/leads/{business_id}", json={
            "status": "called", "is_favorite": True, "note": "проверка",
        })
        self.assertEqual(response.status_code, 200)
        lead = response.json()["lead"]
        self.assertEqual(lead["status"], "called")
        self.assertEqual(lead["is_favorite"], 1)
        self.assertEqual(lead["note"], "проверка")

        self.assertEqual(
            self.client.patch("/api/leads/nonexistent", json={"status": "called"}).status_code,
            404,
        )
        self.assertEqual(
            self.client.patch(f"/api/leads/{business_id}",
                              json={"status": "нечто"}).status_code,
            400,
        )

    def test_export_csv(self) -> None:
        from backend.models import Place

        with db.connect() as conn:
            db.upsert_place(conn, Place(
                source="demo", source_id="demo-export-1",
                name="ДЕМО — Экспорт", phone="+7 900 000-00-98",
                lat=57.12, lon=65.52,
            ))

        response = self.client.get("/api/export.csv")
        self.assertEqual(response.status_code, 200)
        text = response.content.decode("utf-8-sig")
        self.assertIn("Название", text.splitlines()[0])
        self.assertIn("ДЕМО — Экспорт", text)

    def test_scan_validation(self) -> None:
        bad_bbox = self.client.post("/api/scan", json={
            "query": "x", "bbox": [1, 2, 3], "sources": ["demo"],
        })
        self.assertEqual(bad_bbox.status_code, 400)

        no_area = self.client.post("/api/scan", json={
            "query": "x", "city": "", "sources": ["demo"],
        })
        self.assertEqual(no_area.status_code, 400)

        # Пустой запрос без категорий — ошибка бизнес-валидации, а не схемы.
        empty_query = self.client.post("/api/scan", json={
            "query": "", "bbox": [65.5, 57.1, 65.52, 57.12], "sources": ["demo"],
        })
        self.assertEqual(empty_query.status_code, 400)

    def test_categories_endpoint(self) -> None:
        response = self.client.get("/api/categories")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertGreaterEqual(body["total"], 100)
        self.assertEqual(len(body["groups"]), 12)
        self.assertIn("dentistry", body["default"])
        ids = [
            item["id"]
            for group in body["groups"]
            for item in group["categories"]
        ]
        self.assertEqual(len(ids), body["total"])

    def test_scan_estimate(self) -> None:
        response = self.client.post("/api/scan/estimate", json={
            "categories": ["dentistry", "tire"],
            "bbox": [65.5, 57.1, 65.54, 57.14],
            "cell_deg": 0.02,
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["queries"], 2)
        self.assertEqual(body["cells"], 4)
        self.assertEqual(body["requests"], 8)

    def test_scan_accepts_categories(self) -> None:
        response = self.client.post("/api/scan", json={
            "categories": ["dentistry", "tire"],
            "bbox": [65.5, 57.1, 65.54, 57.14],
            "cell_deg": 0.02,
            "sources": ["demo"],
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["queries"], 2)
        self.assertEqual(body["cells"], 4)

        job = self.client.get(f"/api/jobs/{body['job_id']}").json()
        self.assertEqual(job["queries_total"], 2)
        self.assertIn("стоматология", job["queries"])

        # Не оставляем фоновый скан следующему тесту.
        self.client.post(f"/api/jobs/{body['job_id']}/stop")

    def test_stats(self) -> None:
        from backend.models import Place

        with db.connect() as conn:
            business_id, _ = db.upsert_place(conn, Place(
                source="demo", source_id="demo-stats-1",
                name="ДЕМО — Статистика", phone="+7 900 000-00-97",
                lat=57.13, lon=65.53,
            ))
            db.set_status(conn, business_id, status="in_progress", is_favorite=True)

        stats = self.client.get("/api/stats").json()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["favorites"], 1)
        self.assertEqual(stats["by_status"].get("in_progress"), 1)


if __name__ == "__main__":
    unittest.main()