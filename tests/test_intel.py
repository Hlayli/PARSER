"""Тесты продажной аналитики карточки лида.

Запуск из папки PARSER:
    python -m unittest discover tests -p "test_intel.py" -v
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.intel import build_intel, build_pitch  # noqa: E402


def make_lead(**overrides) -> dict:
    data = {
        "id": "lead-1",
        "name": "Кофейня Ромашка",
        "category": "Кофейня",
        "address": "ул. Ленина, 1",
        "phone": "+7 (900) 111-22-33",
        "website": "",
        "rating": 4.7,
        "reviews_count": 120,
        "price_range": "средний",
        "hours": "08:00-22:00",
        "source": "2gis",
        "sources": ["2gis"],
        "is_demo": 0,
        "photos": ["https://cdn.example.com/photo.jpg"],
        "locations": [
            {"address": "ул. Ленина, 1", "phone": "+7 (900) 111-22-33"},
            {"address": "ул. Мира, 2", "phone": "+7 (900) 111-22-34"},
            {"address": "ул. Гагарина, 3", "phone": "+7 (900) 111-22-35"},
        ],
        "locations_count": 3,
        "facts": {},
    }
    data.update(overrides)
    return data


def check_pitch_shape(test: unittest.TestCase, pitch: str) -> None:
    test.assertTrue(pitch.startswith("Здравствуйте!"))
    test.assertTrue(pitch.endswith("?"))
    test.assertGreaterEqual(len(pitch), 400)
    test.assertLessEqual(len(pitch), 900)
    test.assertNotIn("[", pitch)
    test.assertNotIn("ai", pitch.lower())
    test.assertNotIn("гарант", pitch.lower())


class NoSiteChainTestCase(unittest.TestCase):
    def test_no_site_chain_gets_high_score_and_food_offer(self) -> None:
        lead = make_lead()
        intel = build_intel(lead)

        self.assertGreaterEqual(intel["score"], 65)
        self.assertEqual(intel["hooks"][0]["code"], "no_site")
        self.assertEqual(
            intel["offer"]["title"],
            "Сайт с меню, адресами и онлайн-заказом",
        )
        self.assertIn("Кофейня Ромашка", intel["pitch"])
        self.assertIn("сайт", intel["pitch"].lower())

        contacts = {item["type"]: item for item in intel["contacts"]}
        self.assertIn("phone", contacts)
        self.assertIn("whatsapp", contacts)
        self.assertEqual(contacts["phone"]["href"], "tel:+79001112233")
        self.assertEqual(
            contacts["whatsapp"]["href"], "https://wa.me/79001112233"
        )
        check_pitch_shape(self, intel["pitch"])

    def test_no_site_summary_and_site_block(self) -> None:
        intel = build_intel(make_lead())
        self.assertIn("нет сайта", intel["summary"].lower())
        self.assertFalse(intel["site"]["has_site"])
        self.assertFalse(intel["site"]["social_only"])
        self.assertEqual(intel["site"]["checks"], [])
        self.assertEqual(intel["site"]["score"], 0)


class SocialOnlyTestCase(unittest.TestCase):
    def test_vk_page_is_social_only(self) -> None:
        lead = make_lead(
            name="Салон красоты Аврора",
            category="Салон красоты",
            website="https://vk.com/avrora",
            rating=4.3,
            reviews_count=12,
        )
        intel = build_intel(lead)

        self.assertTrue(intel["site"]["social_only"])
        self.assertFalse(intel["site"]["has_site"])
        self.assertEqual(intel["site"]["score"], 0)
        self.assertEqual(intel["site"]["checks"], [])
        self.assertEqual(intel["hooks"][0]["code"], "social_only")
        self.assertNotIn("no_site", [item["code"] for item in intel["gaps"]])
        self.assertIn("сайт + соцсети", intel["offer"]["why"])
        self.assertIn("ВКонтакте", [item["label"] for item in intel["contacts"]])
        check_pitch_shape(self, intel["pitch"])


class GoodSiteTestCase(unittest.TestCase):
    def test_good_site_lowers_need_and_shows_https(self) -> None:
        lead = make_lead(
            name="Шиномонтаж Колесо",
            category="Шиномонтаж",
            website="https://example.com",
            phone="+7 (900) 111-22-33",
            rating=4.8,
            reviews_count=120,
            site_audit={
                "ok": True,
                "https": True,
                "has_viewport": True,
                "title": "Шиномонтаж Колесо",
                "description": "Ремонт и балансировка колёс",
                "has_booking": True,
                "has_form": True,
                "has_menu": True,
                "elapsed_ms": 850,
                "html_kb": 120,
                "status": 200,
                "cms": "WordPress",
            },
        )
        intel = build_intel(lead)

        self.assertLessEqual(intel["score"], 35)
        self.assertNotIn("no_site", [item["code"] for item in intel["hooks"]])
        self.assertEqual(intel["gaps"], [])
        checks = {item["key"]: item for item in intel["site"]["checks"]}
        self.assertIn("https", checks)
        self.assertTrue(checks["https"]["ok"])
        self.assertGreaterEqual(intel["site"]["score"], 90)
        check_pitch_shape(self, intel["pitch"])


class WeakSignalsTestCase(unittest.TestCase):
    def test_low_rating_and_few_reviews_are_gaps(self) -> None:
        lead = make_lead(
            name="Барбершоп Барс",
            category="Барбершоп",
            website="http://bars.example",
            rating=3.5,
            reviews_count=4,
            hours="",
            photos=[],
        )
        intel = build_intel(lead)
        codes = [item["code"] for item in intel["gaps"]]

        self.assertIn("low_rating", codes)
        self.assertIn("few_reviews", codes)
        self.assertIn("site_http", codes)
        self.assertIn("no_hours", codes)
        self.assertIn("no_photos", codes)

    def test_empty_and_malformed_leads_do_not_raise(self) -> None:
        for lead in (
            {},
            {
                "name": None,
                "category": None,
                "phone": None,
                "website": None,
                "rating": None,
                "reviews_count": None,
                "hours": None,
                "photos": None,
                "locations": None,
                "locations_count": None,
                "facts": None,
                "site_audit": None,
            },
            {
                "name": 123,
                "rating": "нет",
                "reviews_count": "много",
                "website": 42,
                "facts": {"phone": [{"value": None}], "photo": "не список"},
                "site_audit": "битые данные",
            },
        ):
            intel = build_intel(lead)
            self.assertIsInstance(intel["score"], int)
            self.assertGreaterEqual(intel["score"], 5)
            self.assertLessEqual(intel["score"], 98)
            self.assertIsInstance(intel["summary"], str)
            self.assertTrue(intel["summary"])
            self.assertIsInstance(intel["hooks"], list)
            self.assertIsInstance(intel["strengths"], list)
            self.assertIsInstance(intel["gaps"], list)
            self.assertIsInstance(intel["contacts"], list)
            self.assertIsInstance(intel["site"], dict)
            self.assertGreaterEqual(len(intel["offer"]["bullets"]), 3)
            check_pitch_shape(self, intel["pitch"])

        empty = build_intel({})
        self.assertEqual(empty["contacts"], [])
        self.assertEqual(empty["site"]["score"], 0)
        self.assertEqual(empty["site"]["checks"], [])


class PitchTestCase(unittest.TestCase):
    def test_build_pitch_matches_intel(self) -> None:
        lead = make_lead()
        intel = build_intel(lead)
        self.assertEqual(build_pitch(lead, intel), intel["pitch"])
        self.assertEqual(build_pitch(lead, {}), intel["pitch"])

    def test_pitch_has_no_placeholders_or_guarantees(self) -> None:
        leads = [
            make_lead(),
            make_lead(website="https://vk.com/avrora", name="Салон Аврора"),
            make_lead(
                website="https://example.com",
                site_audit={"ok": True, "https": True, "has_viewport": True},
            ),
            {},
        ]
        for lead in leads:
            pitch = build_intel(lead)["pitch"]
            check_pitch_shape(self, pitch)


if __name__ == "__main__":
    unittest.main()
