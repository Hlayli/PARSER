"""Тесты извлечения контактов из payload источников.

Запуск из папки PARSER:
    python -m unittest discover tests -p "test_source_facts.py" -v
"""

from __future__ import annotations

import unittest

from backend.source_facts import digits_only, extract_source_data

EMPTY = {
    "website": "",
    "websites": [],
    "social": [],
    "email": [],
    "phone": [],
    "hours": [],
    "description": [],
    "category": "",
    "rating": "",
    "reviews_count": None,
    "address": "",
    "lat": None,
    "lon": None,
}


class YandexPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "id": "1122334455",
            "name": "Coffee Bean",
            "category": "Кофейня",
            "site": "https://coffeebean.ru/",
            "social": "t.me/coffeebean",
            "phone": "+7 4912 25-05-05",
            "address": "Рязань, ул. Почтовая, 58",
            "lat": "54,626214",
            "lon": "39,741601",
            "rating": "5",
            "reviews": "534",
            "url": "https://yandex.ru/maps/org/coffee_bean/1122334455/",
        }
        self.facts = extract_source_data(self.payload, "yandex")

    def test_contacts(self) -> None:
        self.assertEqual(self.facts["website"], "https://coffeebean.ru/")
        self.assertEqual(self.facts["websites"], ["https://coffeebean.ru/"])
        self.assertEqual(self.facts["social"], ["https://t.me/coffeebean"])
        self.assertEqual(self.facts["phone"], ["+7 4912 25-05-05"])
        self.assertEqual(self.facts["email"], [])

    def test_place_fields(self) -> None:
        self.assertEqual(self.facts["address"], "Рязань, ул. Почтовая, 58")
        self.assertEqual(self.facts["lat"], 54.626214)
        self.assertEqual(self.facts["lon"], 39.741601)
        self.assertEqual(self.facts["category"], "Кофейня")
        self.assertEqual(self.facts["rating"], "5")
        self.assertEqual(self.facts["reviews_count"], 534)

    def test_shape_is_complete(self) -> None:
        self.assertEqual(set(self.facts), set(EMPTY))
        self.assertEqual(self.facts["hours"], [])
        self.assertEqual(self.facts["description"], [])


class TwoGisPayloadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "Наименование": "Coffee Bean",
            "Веб-сайт 1": "https://coffeebean.ru",
            "ВКонтакте 1": "https://vk.com/coffeebean",
            "Telegram 1": "t.me/coffeebean",
            "WhatsApp 1": "+7 900 123-45-67",
            "Телефон 1": "+7 (4912) 25-05-05",
            "Телефон 2": "+7 900 123-45-67",
            "E-mail 1": "hello@coffeebean.ru",
            "Часы работы": "Пн-Пт 8:00-22:00",
            "Описание": "Обжариваем кофе на месте",
            "Рубрики": "Кофейни; Кондитерские",
            "Рейтинг": "5.0",
            "Количество отзывов": "71",
            "Город": "Рязань",
            "Район": "Советский",
            "Микрорайон": "Центр",
            "Комментарий к адресу": "вход со двора",
        }
        self.facts = extract_source_data(self.payload, "2gis")

    def test_website_and_social(self) -> None:
        self.assertEqual(self.facts["website"], "https://coffeebean.ru")
        self.assertEqual(self.facts["websites"], ["https://coffeebean.ru"])
        self.assertEqual(
            self.facts["social"],
            [
                "https://vk.com/coffeebean",
                "https://t.me/coffeebean",
                "https://wa.me/79001234567",
            ],
        )

    def test_contact_lists(self) -> None:
        self.assertEqual(self.facts["email"], ["hello@coffeebean.ru"])
        self.assertEqual(
            self.facts["phone"],
            ["+7 (4912) 25-05-05", "+7 900 123-45-67"],
        )
        self.assertEqual(self.facts["hours"], ["Пн-Пт 8:00-22:00"])

    def test_card_fields(self) -> None:
        self.assertEqual(self.facts["category"], "Кофейни")
        self.assertEqual(self.facts["rating"], "5.0")
        self.assertEqual(self.facts["reviews_count"], 71)
        self.assertEqual(self.facts["description"], ["Обжариваем кофе на месте"])
        # Улица в payload отсутствует — адрес не собираем из города/района.
        self.assertEqual(self.facts["address"], "")


class SocialRoutingTest(unittest.TestCase):
    def test_social_link_in_website_column(self) -> None:
        payload = {
            "Веб-сайт 1": "https://vk.com/coffeebean",
            "Веб-сайт 2": "https://coffeebean.ru",
        }
        facts = extract_source_data(payload, "2gis")
        self.assertEqual(facts["website"], "https://coffeebean.ru")
        self.assertEqual(facts["websites"], ["https://coffeebean.ru"])
        self.assertEqual(facts["social"], ["https://vk.com/coffeebean"])

    def test_only_social_website_leaves_website_empty(self) -> None:
        payload = {
            "Веб-сайт 1": "https://vk.com/coffeebean",
            "Веб-сайт 2": "https://youtu.be/abcdef",
        }
        facts = extract_source_data(payload, "2gis")
        self.assertEqual(facts["website"], "")
        self.assertEqual(facts["websites"], [])
        self.assertEqual(
            facts["social"],
            ["https://vk.com/coffeebean", "https://youtu.be/abcdef"],
        )

    def test_handles_and_phone_whatsapp(self) -> None:
        payload = {
            "ВКонтакте 1": "@coffeebean",
            "Telegram 1": "@coffeebean",
            "YouTube 1": "@coffeebean",
            "WhatsApp 1": "wa.me/79001234567",
        }
        facts = extract_source_data(payload, "2gis")
        self.assertEqual(
            facts["social"],
            [
                "https://vk.com/coffeebean",
                "https://t.me/coffeebean",
                "https://wa.me/79001234567",
            ],
        )


class InvalidValuesTest(unittest.TestCase):
    def test_bad_values_are_ignored(self) -> None:
        payload = {
            "site": None,
            "phone": 123,
            "email": {"value": "hello@example.com"},
            "social": [],
            "address": ["ул. Ленина, 1"],
            "Веб-сайт 1": "javascript:alert(1)",
            "Веб-сайт 2": ["https://ok.example"],
            "Телефон 1": {"text": "+7 900 000-00-00"},
            "E-mail 1": "не почта",
            "Рейтинг": None,
            "Количество отзывов": {},
            "lat": 123,
            "lon": 999,
            "Широта": "abc",
            "Долгота": [],
        }
        facts = extract_source_data(payload)
        self.assertEqual(facts["website"], "https://ok.example")
        self.assertEqual(facts["websites"], ["https://ok.example"])
        self.assertEqual(facts["social"], [])
        self.assertEqual(facts["email"], [])
        self.assertEqual(facts["phone"], [])
        self.assertEqual(facts["address"], "ул. Ленина, 1")
        self.assertEqual(facts["rating"], "")
        self.assertIsNone(facts["reviews_count"])
        self.assertIsNone(facts["lat"])
        self.assertIsNone(facts["lon"])

    def test_non_dict_payloads(self) -> None:
        for payload in (None, [], "text", 42):  # type: ignore[arg-type]
            with self.subTest(payload=payload):
                self.assertEqual(extract_source_data(payload), EMPTY)


class EmptyPayloadTest(unittest.TestCase):
    def test_empty_payload(self) -> None:
        self.assertEqual(extract_source_data({}), EMPTY)

    def test_empty_payload_with_source(self) -> None:
        self.assertEqual(extract_source_data({}, "yandex"), EMPTY)
        self.assertEqual(extract_source_data({}, "2gis"), EMPTY)


class DedupeTest(unittest.TestCase):
    def test_repeated_links_and_emails(self) -> None:
        payload = {
            "site": [
                "https://example.com",
                "https://example.com",
                "https://EXAMPLE.com",
            ],
            "email": ["Sales@Example.com", "sales@example.com"],
            "social": ["https://vk.com/shop", "https://VK.com/shop"],
            "phone": ["+7 900 111-22-33", "+7 900 111-22-33"],
        }
        facts = extract_source_data(payload)
        self.assertEqual(facts["websites"], ["https://example.com"])
        self.assertEqual(facts["email"], ["Sales@Example.com"])
        self.assertEqual(facts["social"], ["https://vk.com/shop"])
        self.assertEqual(facts["phone"], ["+7 900 111-22-33"])


class GenericFallbackTest(unittest.TestCase):
    def test_english_keys(self) -> None:
        payload = {
            "Website": "HTTPS://Shop.Example/path",
            "Phone": ["+1 (555) 010-2000", "+1 (555) 010-3000"],
            "Email": "sales@shop.example",
            "Instagram": "https://instagram.com/shop",
            "Hours": "Mon-Fri 9:00-18:00\nSat 10:00-16:00",
            "Description": "Fresh bakery",
            "Category": "Bakery",
            "Address": "1 Main St",
            "Latitude": "54,626214",
            "Longitude": "39.741601",
            "Rating": "4.8",
            "Reviews": "1 234",
        }
        facts = extract_source_data(payload)
        self.assertEqual(facts["website"], "HTTPS://Shop.Example/path")
        self.assertEqual(facts["websites"], ["HTTPS://Shop.Example/path"])
        self.assertEqual(facts["social"], ["https://instagram.com/shop"])
        self.assertEqual(facts["email"], ["sales@shop.example"])
        self.assertEqual(
            facts["phone"],
            ["+1 (555) 010-2000", "+1 (555) 010-3000"],
        )
        self.assertEqual(
            facts["hours"],
            ["Mon-Fri 9:00-18:00", "Sat 10:00-16:00"],
        )
        self.assertEqual(facts["description"], ["Fresh bakery"])
        self.assertEqual(facts["category"], "Bakery")
        self.assertEqual(facts["address"], "1 Main St")
        self.assertEqual(facts["lat"], 54.626214)
        self.assertEqual(facts["lon"], 39.741601)
        self.assertEqual(facts["rating"], "4.8")
        self.assertEqual(facts["reviews_count"], 1234)


class DigitsOnlyTest(unittest.TestCase):
    def test_digits_only(self) -> None:
        self.assertEqual(digits_only("+7 (900) 123-45-67"), "79001234567")
        self.assertEqual(digits_only(""), "")
        self.assertEqual(digits_only("нет цифр"), "")


class NumericFieldsTest(unittest.TestCase):
    def test_numbers_are_accepted_for_numeric_fields(self) -> None:
        facts = extract_source_data({
            "lat": 54.626214,
            "lon": 39.741601,
            "rating": 4.5,
            "reviews": 42,
        })
        self.assertEqual(facts["lat"], 54.626214)
        self.assertEqual(facts["lon"], 39.741601)
        self.assertEqual(facts["rating"], "4.5")
        self.assertEqual(facts["reviews_count"], 42)


if __name__ == "__main__":
    unittest.main()
