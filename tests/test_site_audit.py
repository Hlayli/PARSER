"""Тесты аудита сайта: признаки страницы и распознавание соцсетей.

Сеть не используется: на вход подаётся готовый HTML, как его вернул бы
загрузчик. Запуск из папки PARSER:
    python -m unittest discover tests -p "test_site_audit.py" -v
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime

from backend.site_audit import (
    audit_page,
    empty_audit,
    host_of,
    is_social_url,
    social_label,
)

EXPECTED_KEYS = {
    "url",
    "final_url",
    "status",
    "ok",
    "https",
    "redirected",
    "elapsed_ms",
    "html_kb",
    "title",
    "description",
    "has_viewport",
    "has_h1",
    "has_form",
    "has_booking",
    "has_menu",
    "has_prices",
    "has_contacts",
    "cms",
    "error",
    "checked_at",
}

BOOL_KEYS = (
    "ok",
    "https",
    "redirected",
    "has_viewport",
    "has_h1",
    "has_form",
    "has_booking",
    "has_menu",
    "has_prices",
    "has_contacts",
)

GOOD_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <title>  Кофейня «Ромашка» — кофе и десерты  </title>
  <meta name="description"
        content="Кофейня в центре города: свежий кофе, десерты и завтраки.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <h1>Кофейня «Ромашка»</h1>
  <p>Свежий кофе от 150 ₽. Забронируйте столик онлайн.</p>
  <nav><a href="/menu">Меню</a></nav>
  <form action="/subscribe">
    <input type="email" name="email" placeholder="Ваш email">
    <button type="submit">Отправить</button>
  </form>
  <a href="tel:+79001112233">+7 900 111-22-33</a>
  <a href="mailto:info@romashka.ru">info@romashka.ru</a>
</body>
</html>
"""

BAD_PAGE = """<!DOCTYPE html>
<html>
<head><title></title></head>
<body><p>Мы работаем ежедневно.</p></body>
</html>
"""

SEARCH_ONLY_PAGE = """<!DOCTYPE html>
<html>
<body>
  <form action="/search" role="search">
    <input type="search" name="q" placeholder="Поиск по сайту">
    <button type="submit">Найти</button>
  </form>
</body>
</html>
"""

WORDPRESS_PAGE = """<!DOCTYPE html>
<html>
<head>
  <meta name="generator" content="WordPress 6.4">
  <link rel="stylesheet" href="/wp-content/themes/romashka/style.css">
</head>
<body><h1>Блог</h1></body>
</html>
"""

TILDA_PAGE = """<!DOCTYPE html>
<html>
<head>
  <script src="https://static.tildacdn.com/js/tilda-scripts-3.0.min.js"></script>
</head>
<body><h1>Студия</h1></body>
</html>
"""


class EmptyAuditTestCase(unittest.TestCase):
    def test_shape_and_error(self) -> None:
        audit = empty_audit("https://example.com", "таймаут")

        self.assertEqual(set(audit), EXPECTED_KEYS)
        self.assertEqual(audit["url"], "https://example.com")
        self.assertEqual(audit["final_url"], "")
        self.assertEqual(audit["status"], 0)
        self.assertEqual(audit["elapsed_ms"], 0)
        self.assertEqual(audit["html_kb"], 0)
        self.assertEqual(audit["title"], "")
        self.assertEqual(audit["description"], "")
        self.assertEqual(audit["cms"], "")
        self.assertEqual(audit["error"], "таймаут")
        for key in BOOL_KEYS:
            self.assertFalse(audit[key], key)

        self.assertTrue(audit["checked_at"].endswith("Z"))
        datetime.strptime(audit["checked_at"], "%Y-%m-%dT%H:%M:%SZ")
        json.dumps(audit, ensure_ascii=False)


class AuditPageTestCase(unittest.TestCase):
    def test_good_page_all_hooks(self) -> None:
        audit = audit_page(GOOD_PAGE, "https://romashka.ru/", elapsed_ms=123)

        self.assertEqual(set(audit), EXPECTED_KEYS)
        self.assertEqual(audit["url"], "https://romashka.ru/")
        self.assertEqual(audit["final_url"], "https://romashka.ru/")
        self.assertEqual(audit["status"], 200)
        self.assertTrue(audit["ok"])
        self.assertTrue(audit["https"])
        self.assertFalse(audit["redirected"])
        self.assertEqual(audit["elapsed_ms"], 123)
        self.assertGreaterEqual(audit["html_kb"], 1)

        self.assertEqual(
            audit["title"], "Кофейня «Ромашка» — кофе и десерты"
        )
        self.assertLessEqual(len(audit["title"]), 160)
        self.assertIn("центре города", audit["description"])
        self.assertLessEqual(len(audit["description"]), 300)

        self.assertTrue(audit["has_viewport"])
        self.assertTrue(audit["has_h1"])
        self.assertTrue(audit["has_form"])
        self.assertTrue(audit["has_booking"])
        self.assertTrue(audit["has_menu"])
        self.assertTrue(audit["has_prices"])
        self.assertTrue(audit["has_contacts"])
        self.assertEqual(audit["cms"], "")
        self.assertEqual(audit["error"], "")
        self.assertTrue(audit["checked_at"].endswith("Z"))

    def test_title_and_description_fall_back_to_og(self) -> None:
        html = """<html><head>
        <meta property="og:title" content="Студия маникюра">
        <meta property="og:description" content="Маникюр и педикюр в центре.">
        </head><body></body></html>"""
        audit = audit_page(html, "https://nails.ru/")
        self.assertEqual(audit["title"], "Студия маникюра")
        self.assertEqual(audit["description"], "Маникюр и педикюр в центре.")

    def test_bad_page_without_hooks(self) -> None:
        audit = audit_page(BAD_PAGE, "http://old.example.com/")

        self.assertTrue(audit["ok"])
        self.assertFalse(audit["https"])
        self.assertFalse(audit["redirected"])
        self.assertFalse(audit["has_viewport"])
        self.assertFalse(audit["has_h1"])
        self.assertFalse(audit["has_form"])
        self.assertFalse(audit["has_booking"])
        self.assertFalse(audit["has_menu"])
        self.assertFalse(audit["has_prices"])
        self.assertFalse(audit["has_contacts"])
        self.assertEqual(audit["title"], "")
        self.assertEqual(audit["description"], "")
        self.assertEqual(audit["error"], "")

    def test_error_status_is_not_ok(self) -> None:
        audit = audit_page("<html></html>", "https://example.com/", status=503)
        self.assertFalse(audit["ok"])

    def test_search_only_form_not_counted(self) -> None:
        audit = audit_page(SEARCH_ONLY_PAGE, "https://example.com/")
        self.assertFalse(audit["has_form"])

    def test_form_with_email_counted(self) -> None:
        html = '<html><body><form><input type="email" name="email"></form></body></html>'
        audit = audit_page(html, "https://example.com/")
        self.assertTrue(audit["has_form"])

    def test_cms_wordpress_and_tilda(self) -> None:
        wordpress = audit_page(WORDPRESS_PAGE, "https://blog.example.com/")
        self.assertEqual(wordpress["cms"], "WordPress")

        tilda = audit_page(TILDA_PAGE, "https://studio.example.com/")
        self.assertEqual(tilda["cms"], "Tilda")

    def test_menu_from_json_ld_and_facebook_is_not_booking(self) -> None:
        html = """<html><body>
        <a href="https://facebook.com/romashka">Мы в Facebook</a>
        <script type="application/ld+json">
        {"@type": "Menu", "name": "Меню"}
        </script>
        </body></html>"""
        audit = audit_page(html, "https://example.com/")
        self.assertTrue(audit["has_menu"])
        self.assertFalse(audit["has_booking"])

    def test_redirect_to_https_and_www(self) -> None:
        audit = audit_page(
            "<html></html>",
            "http://example.com/",
            final_url="https://www.example.com/",
        )
        self.assertTrue(audit["redirected"])
        self.assertTrue(audit["https"])
        self.assertEqual(audit["final_url"], "https://www.example.com/")

    def test_fragment_does_not_count_as_redirect(self) -> None:
        audit = audit_page(
            "<html></html>",
            "https://example.com/#top",
            final_url="https://example.com/",
        )
        self.assertFalse(audit["redirected"])

    def test_broken_html_does_not_raise(self) -> None:
        broken = (
            "<html><body><h1>Заголовок"
            "<form><input type=email name=email"
        )
        audit = audit_page(broken, "https://example.com/")
        self.assertEqual(set(audit), EXPECTED_KEYS)
        self.assertTrue(audit["has_h1"])
        self.assertTrue(audit["has_form"])

    def test_empty_html_does_not_raise(self) -> None:
        audit = audit_page("", "")
        self.assertEqual(set(audit), EXPECTED_KEYS)
        self.assertEqual(audit["html_kb"], 0)
        self.assertEqual(audit["error"], "")


class SocialTestCase(unittest.TestCase):
    def test_social_hosts_and_labels(self) -> None:
        cases = {
            "https://vk.com/club1": "ВКонтакте",
            "https://m.vk.com/club1": "ВКонтакте",
            "https://wa.me/79001112233": "WhatsApp",
            "https://t.me/romashka": "Telegram",
            "https://www.instagram.com/romashka": "Instagram",
            "https://youtube.com/@romashka": "YouTube",
            "https://ok.ru/romashka": "Одноклассники",
        }
        for url, label in cases.items():
            with self.subTest(url=url):
                self.assertTrue(is_social_url(url))
                self.assertEqual(social_label(url), label)

        self.assertFalse(is_social_url("https://example.com/"))
        self.assertEqual(social_label("https://example.com/"), "")

    def test_host_of(self) -> None:
        self.assertEqual(host_of("https://www.Example.COM/path"), "example.com")
        self.assertEqual(host_of("https://m.vk.com/club"), "m.vk.com")
        self.assertEqual(host_of(""), "")
        self.assertEqual(host_of("не url"), "")
        self.assertEqual(host_of("javascript:void(0)"), "")


if __name__ == "__main__":
    unittest.main()
