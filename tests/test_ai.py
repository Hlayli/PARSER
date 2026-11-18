"""Тесты необязательной LLM-интеграции: статус, промпт, запрос к API.

Сеть не используется: httpx.AsyncClient подменяется заглушкой.

Запуск из папки PARSER:
    python -m unittest discover tests -p "test_ai.py" -v
"""

from __future__ import annotations

import json
import os
import unittest
from unittest import mock

import httpx

from backend import ai


_ENV_KEYS = (
    "LLM_API_KEY",
    "OPENAI_API_KEY",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LLM_TIMEOUT",
)


def guard_env(case: unittest.TestCase) -> None:
    """Временно прячет настройки LLM, не задевая остальное окружение."""
    patcher = mock.patch.dict(os.environ, {}, clear=False)
    patcher.start()
    case.addCleanup(patcher.stop)
    for name in _ENV_KEYS:
        os.environ.pop(name, None)


LEAD = {
    "id": "b-1",
    "name": "Кофейня Ромашка",
    "category": "Кофейня",
    "address": "ул. Ленина, 1",
    "website": "",
    "phone": "+7 (900) 111-22-33",
    "rating": 4.7,
    "reviews_count": 12,
    "hours": "пн-пт 08:00-20:00",
    "locations_count": 2,
    "sources": ["2gis", "yandex"],
    "status": "new",
    "note": "не показывать модели",
}

INTEL = {
    "score": 82,
    "label": "горячий",
    "summary": "У сети две точки, своего сайта нет.",
    "hooks": [
        "сайта нет",
        "в карточках только соцсети",
        "меню негде посмотреть",
        "нет онлайн-записи",
        "клиенты не находят часы работы",
        "лишний хук",
    ],
    "offer": "лендинг с меню и картой за неделю",
    "contacts": {"phone": "+7 (900) 111-22-33"},
}

GOOD_PITCH = (
    "Здравствуйте! Увидел «Кофейню Ромашка» в справочнике — 3 точки, "
    "рейтинг 4,7. Сайта нет: клиенту из поиска негде посмотреть меню и цены. "
    "Я делаю сайты для кофеен и небольших сетей. Начать можно с одной "
    "страницы: меню, адреса точек и кнопка WhatsApp. Могу прислать макет "
    "главной — посмотрите, подходит ли. Прислать?"
)


class StatusTestCase(unittest.TestCase):
    def setUp(self) -> None:
        guard_env(self)

    def test_status_without_key(self) -> None:
        data = ai.status()
        self.assertFalse(data["configured"])
        self.assertEqual(data["model"], "gpt-4o-mini")
        self.assertEqual(data["base_url"], "https://api.openai.com/v1")
        self.assertEqual(set(data), {"configured", "model", "base_url"})

    def test_status_with_key_hides_secret(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "sk-secret-value",
                "LLM_MODEL": "my-model",
                "LLM_BASE_URL": "http://localhost:11434/v1/",
            },
        ):
            data = ai.status()

        self.assertTrue(data["configured"])
        self.assertEqual(data["model"], "my-model")
        self.assertEqual(data["base_url"], "http://localhost:11434/v1")
        self.assertNotIn("sk-secret-value", repr(data))

    def test_status_falls_back_to_openai_key(self) -> None:
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-openai"}):
            self.assertTrue(ai.status()["configured"])

    def test_llm_key_wins_over_openai_key(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"LLM_API_KEY": "sk-llm", "OPENAI_API_KEY": "sk-openai"},
        ):
            self.assertTrue(ai.status()["configured"])
            self.assertEqual(ai._api_key(), "sk-llm")


class BuildMessagesTestCase(unittest.TestCase):
    def setUp(self) -> None:
        guard_env(self)

    def test_two_roles_and_rules(self) -> None:
        messages = ai.build_messages(LEAD, INTEL)
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[1]["role"], "user")

        system = messages[0]["content"].lower()
        self.assertIn("не выдумывай", system)
        self.assertIn("markdown", system)
        self.assertIn("350", system)
        self.assertIn("650", system)
        self.assertIn("[имя]", system)
        self.assertIn("созвон", system)
        self.assertIn("прислать", system)

    def test_user_json_has_facts_and_offer(self) -> None:
        user = json.loads(ai.build_messages(LEAD, INTEL)[1]["content"])
        self.assertEqual(user["name"], "Кофейня Ромашка")
        self.assertEqual(user["category"], "Кофейня")
        # Пустой сайт — это отсутствие факта, а не пустое поле.
        self.assertNotIn("website", user)
        self.assertEqual(user["intel"]["offer"], INTEL["offer"])
        self.assertEqual(user["intel"]["score"], 82)
        self.assertEqual(len(user["intel"]["hooks"]), 5)
        self.assertNotIn("note", user)
        self.assertNotIn("status", user)

    def test_site_audit_included_only_when_dict(self) -> None:
        with_audit = ai.build_messages(
            {**LEAD, "site_audit": {"https": False, "mobile": False}}, INTEL
        )
        user = json.loads(with_audit[1]["content"])
        self.assertEqual(user["site_audit"], {"https": False, "mobile": False})

        without = ai.build_messages(LEAD, INTEL)
        self.assertNotIn("site_audit", json.loads(without[1]["content"]))

        wrong_type = ai.build_messages({**LEAD, "site_audit": "нет сайта"}, INTEL)
        self.assertNotIn("site_audit", json.loads(wrong_type[1]["content"]))

    def test_missing_fields_do_not_raise(self) -> None:
        messages = ai.build_messages({}, {})
        self.assertEqual(len(messages), 2)
        self.assertIsInstance(json.loads(messages[1]["content"]), dict)

    def test_non_dict_input_does_not_raise(self) -> None:
        messages = ai.build_messages(None, None)  # type: ignore[arg-type]
        self.assertIsInstance(json.loads(messages[1]["content"]), dict)

    def test_message_size_is_limited(self) -> None:
        huge_lead = {
            **LEAD,
            "address": "очень длинный адрес " * 400,
            "site_audit": {
                "problems": ["проблема " * 200 for _ in range(30)],
            },
        }
        huge_intel = {
            **INTEL,
            "summary": "длинное описание " * 500,
            "offer": "предложение " * 300,
            "hooks": ["хук " * 200 for _ in range(20)],
        }
        content = ai.build_messages(huge_lead, huge_intel)[1]["content"]
        self.assertLessEqual(len(content), ai.MAX_MESSAGE_CHARS)
        parsed = json.loads(content)
        self.assertIn("intel", parsed)


class FakeClient:
    """Минимальная замена httpx.AsyncClient без сети."""

    def __init__(self, response: httpx.Response | None = None, error=None) -> None:
        self.response = response
        self.error = error
        self.calls: list[tuple[str, dict, dict]] = []

    async def __aenter__(self) -> "FakeClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def post(self, url: str, headers=None, json=None) -> httpx.Response:
        self.calls.append((url, headers or {}, json or {}))
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def make_response(status_code: int = 200, payload=None, text: str = "") -> httpx.Response:
    request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
    if payload is not None:
        return httpx.Response(status_code, request=request, json=payload)
    return httpx.Response(status_code, request=request, text=text)


def patch_client(case: unittest.TestCase, **kwargs) -> tuple[FakeClient, mock.Mock]:
    client = FakeClient(**kwargs)
    factory = mock.Mock(return_value=client)
    patcher = mock.patch.object(ai.httpx, "AsyncClient", factory)
    patcher.start()
    case.addCleanup(patcher.stop)
    return client, factory


class GeneratePitchTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        guard_env(self)

    async def test_without_key_does_not_touch_network(self) -> None:
        factory = mock.Mock(side_effect=AssertionError("сеть запрещена"))
        patcher = mock.patch.object(ai.httpx, "AsyncClient", factory)
        patcher.start()
        self.addCleanup(patcher.stop)

        result = await ai.generate_pitch(LEAD, INTEL)

        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["error"], "not_configured")
        self.assertEqual(result["model"], "gpt-4o-mini")
        factory.assert_not_called()

    async def test_successful_response(self) -> None:
        payload = {
            "choices": [
                {"message": {"content": f"  {GOOD_PITCH}  "}}
            ]
        }
        client, factory = patch_client(self, response=make_response(200, payload))

        with mock.patch.dict(
            os.environ,
            {
                "LLM_API_KEY": "sk-test-key",
                "LLM_MODEL": "test-model",
                "LLM_BASE_URL": "https://api.example.com/v1/",
            },
        ):
            result = await ai.generate_pitch(LEAD, INTEL)

        self.assertTrue(result["ok"])
        self.assertEqual(result["text"], GOOD_PITCH)
        self.assertEqual(result["error"], "")
        self.assertEqual(result["model"], "test-model")

        factory.assert_called_once()
        self.assertEqual(factory.call_args.kwargs["timeout"], 25.0)

        url, headers, body = client.calls[0]
        self.assertEqual(url, "https://api.example.com/v1/chat/completions")
        self.assertEqual(headers["Authorization"], "Bearer sk-test-key")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(body["model"], "test-model")
        self.assertEqual(body["temperature"], 0.6)
        self.assertEqual(body["max_tokens"], 500)
        self.assertEqual(body["messages"][0]["role"], "system")

    async def test_short_response_is_rejected(self) -> None:
        payload = {"choices": [{"message": {"content": "Здравствуйте! Прислать?"}}]}
        patch_client(self, response=make_response(200, payload))
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
            result = await ai.generate_pitch(LEAD, INTEL)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bad_length")

    async def test_template_phrases_are_rejected(self) -> None:
        text = GOOD_PITCH.replace("Прислать?", "Предлагаю вариант. Прислать?")
        payload = {"choices": [{"message": {"content": text}}]}
        patch_client(self, response=make_response(200, payload))
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
            result = await ai.generate_pitch(LEAD, INTEL)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "bad_style")

    async def test_empty_content(self) -> None:
        payload = {"choices": [{"message": {"content": "   "}}]}
        patch_client(self, response=make_response(200, payload))
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
            result = await ai.generate_pitch(LEAD, INTEL)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "empty_response")

    async def test_http_error_returns_short_error(self) -> None:
        response = make_response(500, text='{"error": "boom"}')
        patch_client(self, response=response)
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
            result = await ai.generate_pitch(LEAD, INTEL)

        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["error"], "http_500")

    async def test_timeout(self) -> None:
        patch_client(self, error=httpx.TimeoutException("timed out"))
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
            result = await ai.generate_pitch(LEAD, INTEL)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "timeout")

    async def test_unexpected_error_uses_class_name(self) -> None:
        patch_client(self, error=RuntimeError("boom"))
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
            result = await ai.generate_pitch(LEAD, INTEL)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "RuntimeError")

    async def test_broken_lead_does_not_raise(self) -> None:
        # Несериализуемое значение не должно доходить до сети и падать наружу.
        patch_client(self, response=make_response(200, {}))
        with mock.patch.dict(os.environ, {"LLM_API_KEY": "sk-test"}):
            result = await ai.generate_pitch({"name": object()}, INTEL)

        self.assertFalse(result["ok"])
        self.assertEqual(result["text"], "")
        self.assertEqual(result["error"], "TypeError")

    async def test_timeout_is_clamped(self) -> None:
        payload = {"choices": [{"message": {"content": GOOD_PITCH}}]}
        for raw, expected in (("1", 5.0), ("999", 60.0), ("abc", 25.0)):
            client, factory = patch_client(self, response=make_response(200, payload))
            with mock.patch.dict(
                os.environ, {"LLM_API_KEY": "sk-test", "LLM_TIMEOUT": raw}
            ):
                result = await ai.generate_pitch(LEAD, INTEL)

            self.assertTrue(result["ok"])
            self.assertEqual(factory.call_args.kwargs["timeout"], expected)
            self.assertEqual(len(client.calls), 1)


if __name__ == "__main__":
    unittest.main()
