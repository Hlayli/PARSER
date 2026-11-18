"""Интеграционные тесты: аналитика карточки, обогащение из payload'ов, pitch.

Сеть не используется: проверяются только данные, уже сохранённые в базе.

Запуск из папки PARSER:
    python -m unittest discover tests -p "test_integration.py" -v
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_TEST_DB = Path(tempfile.gettempdir()) / "parser_unittest.db"
os.environ["PARSER_DB"] = str(_TEST_DB)
os.environ["PARSER_DEMO"] = "1"

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend import db  # noqa: E402
from backend.enrichment import enrich  # noqa: E402
from backend.models import Place  # noqa: E402


def clear_db() -> None:
    db.init_db()
    with db.connect() as conn:
        for table in ("fact", "business_source", "business_location", "business",
                      "scan_cell", "scan_job"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


def add_source(conn, business_id: str, source: str, source_id: str,
               payload: dict, url: str = "") -> None:
    conn.execute(
        "INSERT OR REPLACE INTO business_source "
        "(business_id, source, source_id, url, payload_json) VALUES (?,?,?,?,?)",
        (business_id, source, source_id, url,
         json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


class IntelIntegrationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        clear_db()
        self.conn = db.connect()

    def tearDown(self) -> None:
        self.conn.close()

    def make_lead(self, **overrides) -> str:
        data = dict(
            source="google", source_id="g-intel-1", name="Кофейня Ромашка",
            category="Кофейня", address="ул. Ленина, 1",
            phone="+7 (900) 111-22-33", website="", lat=57.15, lon=65.53,
        )
        data.update(overrides)
        business_id, _ = db.upsert_place(self.conn, Place(**data))
        return business_id

    def test_get_lead_contains_intel_and_site_audit(self) -> None:
        business_id = self.make_lead(website="http://example.com")
        db.set_site_audit(self.conn, business_id, {
            "https": False, "has_viewport": False, "title": "Пример",
            "description": "", "has_form": False, "ok": True, "status": 200,
        })

        lead = db.get_lead(self.conn, business_id)
        self.assertEqual(lead["site_audit"]["https"], False)
        self.assertIn("intel", lead)
        intel = lead["intel"]
        self.assertIsInstance(intel["score"], int)
        self.assertTrue(intel["summary"])
        self.assertTrue(intel["pitch"].startswith("Здравствуйте!"))
        self.assertTrue(intel["site"]["has_site"])
        codes = {item["code"] for item in intel["hooks"]}
        self.assertIn("site_http", codes)

    def test_empty_card_does_not_look_promising(self) -> None:
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO business (id, source, source_id, first_seen, updated_at) "
                "VALUES ('empty-card', 'google', 'x', ?, ?)",
                (db.now(), db.now()),
            )
            conn.commit()
        with db.connect() as conn:
            lead = db.get_lead(conn, "empty-card")
        self.assertEqual(lead["intel"]["score"], 5)
        self.assertEqual(lead["intel"]["label"], "низкий")


class EnrichPayloadTestCase(unittest.TestCase):
    def setUp(self) -> None:
        clear_db()
        self.conn = db.connect()

    def tearDown(self) -> None:
        self.conn.close()

    def test_no_site_collects_contacts_from_2gis_payload(self) -> None:
        business_id, _ = db.upsert_place(self.conn, Place(
            source="2gis", source_id="2g-nosite-1", name="Чувства",
            category="Кофейня", address="", phone="", website="",
            lat=None, lon=None,
        ))
        add_source(self.conn, business_id, "2gis", "2g-nosite-1", {
            "Телефон 1": "+7 900 123-45-67",
            "ВКонтакте 1": "https://vk.com/chuvstva",
            "Telegram 1": "https://t.me/chuvstva",
            "Часы работы": "Пн-Вс: 09:00-21:00",
            "Рейтинг": "4.8",
            "Количество отзывов": "42",
            "Описание": "Кофейня и кондитерская",
        })

        result = asyncio.run(enrich(business_id))
        self.assertEqual(result["status"], "no_site")
        self.assertIn("соцсет", result["message"].lower())

        lead = db.get_lead(self.conn, business_id)
        self.assertEqual(lead["phone"], "+7 900 123-45-67")
        self.assertIn("09:00-21:00", lead["hours"])
        self.assertEqual(lead["rating"], 4.8)
        self.assertEqual(lead["reviews_count"], 42)
        social = [item["value"] for item in lead["facts"].get("social", [])]
        self.assertIn("https://vk.com/chuvstva", social)
        self.assertIn("https://t.me/chuvstva", social)
        types = {item["type"] for item in lead["intel"]["contacts"]}
        self.assertIn("phone", types)
        self.assertIn("vk", types)

    def test_yandex_payload_restores_address_and_point(self) -> None:
        business_id, _ = db.upsert_place(self.conn, Place(
            source="yandex", source_id="y-point-1", name="Coffee Bean",
            category="Кофейня", address="", phone="", website="",
            lat=None, lon=None,
        ))
        add_source(self.conn, business_id, "yandex", "y-point-1", {
            "address": "Рязань, улица Семашко, 3, стр. 1",
            "lat": "54.617517", "lon": "39.733461",
            "phone": "+74912306620",
            "url": "https://yandex.ru/maps/org/coffee_bean/1/",
        }, url="https://yandex.ru/maps/org/coffee_bean/1/")

        result = asyncio.run(enrich(business_id))
        self.assertEqual(result["status"], "no_site")

        lead = db.get_lead(self.conn, business_id)
        self.assertEqual(lead["address"], "Рязань, улица Семашко, 3, стр. 1")
        self.assertEqual(len(lead["locations"]), 1)
        self.assertEqual(lead["locations"][0]["address"],
                         "Рязань, улица Семашко, 3, стр. 1")
        self.assertAlmostEqual(lead["locations"][0]["lat"], 54.617517)
        self.assertAlmostEqual(lead["locations"][0]["lon"], 39.733461)

    def test_social_only_website_is_not_crawled(self) -> None:
        business_id, _ = db.upsert_place(self.conn, Place(
            source="google", source_id="g-social-1", name="Салон Аврора",
            category="Салон красоты", website="https://vk.com/avrora",
            phone="+7 900 111-22-33",
        ))
        result = asyncio.run(enrich(business_id))
        self.assertEqual(result["status"], "social_only")
        lead = db.get_lead(self.conn, business_id)
        self.assertTrue(lead["intel"]["site"]["social_only"])
        social = [item["value"] for item in lead["facts"].get("social", [])]
        self.assertIn("https://vk.com/avrora", social)


class PitchApiTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from fastapi.testclient import TestClient
        from backend.app import app

        cls.client = TestClient(app)
        cls.client.__enter__()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.__exit__(None, None, None)

    def setUp(self) -> None:
        clear_db()
        with db.connect() as conn:
            self.business_id, _ = db.upsert_place(conn, Place(
                source="2gis", source_id="2g-pitch-1", name="Кофейня Ромашка",
                category="Кофейня", address="ул. Ленина, 1",
                phone="+7 (900) 111-22-33", website="",
                lat=57.15, lon=65.53,
            ))

    def test_health_reports_ai_status(self) -> None:
        body = self.client.get("/api/health").json()
        self.assertIn("ai", body)
        self.assertIn("configured", body["ai"])
        self.assertNotIn("api_key", body["ai"])

    def test_pitch_falls_back_to_template_without_key(self) -> None:
        env = {key: value for key, value in os.environ.items()
               if key not in ("LLM_API_KEY", "OPENAI_API_KEY")}
        with patch.dict(os.environ, env, clear=True):
            response = self.client.post(f"/api/leads/{self.business_id}/pitch")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["source"], "template")
        self.assertTrue(body["text"].startswith("Здравствуйте!"))
        self.assertIn("Кофейня Ромашка", body["text"])

    def test_pitch_404(self) -> None:
        response = self.client.post("/api/leads/nonexistent/pitch")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
