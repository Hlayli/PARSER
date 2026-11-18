"""Необязательная генерация первого письма бизнесу через нейросеть.

Модуль работает с любым OpenAI-совместимым API: OpenAI, OpenRouter,
локальные Ollama или vLLM. Без ключа приложение полностью работоспособно:
``generate_pitch`` просто возвращает ошибку ``not_configured`` и не ходит
в сеть.

Ключ и настройки читаются из окружения при каждом вызове, поэтому их можно
менять без перезапуска процесса. Ключ никогда не попадает в ответы и логи.
"""

from __future__ import annotations

import copy
import json
import logging
import os

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.openai.com/v1"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT = 25.0
MIN_TIMEOUT = 5.0
MAX_TIMEOUT = 60.0

MAX_MESSAGE_CHARS = 6000
MAX_HOOKS = 5
MAX_SOURCES = 10

SYSTEM_PROMPT = """\
Ты помогаешь продавцу веб-студии писать первое сообщение малому бизнесу \
с предложением сделать сайт. Пиши по-русски, на «вы», спокойно и по делу.

Правила:
- Опирайся ТОЛЬКО на факты из JSON ниже. Не выдумывай имена, названия, \
цены, гарантии, конкурентов, выручку и любые другие данные, которых нет \
в JSON.
- Не используй markdown, заголовки, списки и эмодзи — только обычный текст.
- Не оставляй заглушек и шаблонов вроде [имя], [название], {{name}}.
- Объём письма — от 500 до 900 символов.
- Если сайта нет или в site_audit есть проблемы, назови это нейтрально и \
конкретно, без давления и оценок.
- Предложи именно то, что указано в intel.offer; другие услуги не придумывай.
- Закончи вопросом о коротком звонке или демонстрации.
"""


# ---------------------------------------------------------------------------
# Настройки из окружения (читаются при каждом вызове)
# ---------------------------------------------------------------------------


def _api_key() -> str:
    for name in ("LLM_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


def _base_url() -> str:
    value = os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_BASE_URL
    return value.rstrip("/")


def _model() -> str:
    return os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL


def _timeout() -> float:
    raw = os.environ.get("LLM_TIMEOUT", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        logger.warning("LLM_TIMEOUT не является числом, использую %s", DEFAULT_TIMEOUT)
        return DEFAULT_TIMEOUT
    return min(MAX_TIMEOUT, max(MIN_TIMEOUT, value))


def status() -> dict:
    """Состояние интеграции. Ключ наружу не отдаётся — только флаг."""
    return {
        "configured": bool(_api_key()),
        "model": _model(),
        "base_url": _base_url(),
    }


# ---------------------------------------------------------------------------
# Сборка запроса
# ---------------------------------------------------------------------------


def _present(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict)):
        return bool(value)
    return True


def _lead_payload(lead: dict, intel: dict) -> dict:
    """Только полезные для письма поля, без мусора и служебных id."""
    lead = lead if isinstance(lead, dict) else {}
    intel = intel if isinstance(intel, dict) else {}

    data: dict[str, object] = {}
    for key in (
        "name",
        "category",
        "address",
        "website",
        "phone",
        "rating",
        "reviews_count",
        "hours",
        "locations_count",
    ):
        value = lead.get(key)
        if _present(value):
            data[key] = value

    sources = lead.get("sources")
    if isinstance(sources, str):
        if sources.strip():
            data["sources"] = sources
    elif isinstance(sources, (list, tuple)) and sources:
        data["sources"] = list(sources)[:MAX_SOURCES]

    audit = lead.get("site_audit")
    if not isinstance(audit, dict):
        audit = intel.get("site_audit")
    if isinstance(audit, dict):
        data["site_audit"] = audit

    brief: dict[str, object] = {}
    for key in ("score", "label", "summary", "offer", "contacts"):
        value = intel.get(key)
        if _present(value):
            brief[key] = value

    hooks = intel.get("hooks")
    if isinstance(hooks, (list, tuple)):
        hooks = [hook for hook in hooks if _present(hook)][:MAX_HOOKS]
        if hooks:
            brief["hooks"] = list(hooks)
    elif isinstance(hooks, str) and hooks.strip():
        brief["hooks"] = [hooks.strip()]

    data["intel"] = brief
    return data


def _dump(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _string_slots(node: object, path: tuple = ()) -> list[tuple[tuple, int, str]]:
    """Все строковые значения payload: (путь, длина, строка)."""
    slots: list[tuple[tuple, int, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            slots.extend(_string_slots(value, path + (key,)))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            slots.extend(_string_slots(value, path + (index,)))
    elif isinstance(node, str):
        slots.append((path, len(node), node))
    return slots


def _put(root: object, path: tuple, value: object) -> None:
    node = root
    for step in path[:-1]:
        node = node[step]
    node[path[-1]] = value


def _fit(data: dict, limit: int = MAX_MESSAGE_CHARS) -> str:
    """Компактный JSON не длиннее limit: длинные строки подрезаются.

    JSON остаётся валидным, поэтому модель всегда получает структуру,
    а не обрезанный посередине текст.
    """
    text = _dump(data)
    if len(text) <= limit:
        return text

    trimmed = copy.deepcopy(data)
    # Сначал убираем самые объёмные необязательные блоки.
    trimmed.pop("site_audit", None)
    intel = trimmed.get("intel")
    if isinstance(intel, dict):
        hooks = intel.get("hooks")
        if isinstance(hooks, list) and len(hooks) > 3:
            intel["hooks"] = hooks[:3]
    text = _dump(trimmed)
    if len(text) <= limit:
        return text

    # Затем постепенно укорачиваем самые длинные строки.
    while len(text) > limit:
        slots = _string_slots(trimmed)
        slots.sort(key=lambda item: item[1], reverse=True)
        if not slots or slots[0][1] <= 40:
            break
        path, length, value = slots[0]
        _put(trimmed, path, value[: max(40, length // 2)])
        text = _dump(trimmed)

    if len(text) <= limit:
        return text

    # Крайний случай: оставляем минимум, который нужен для письма.
    intel = trimmed.get("intel") if isinstance(trimmed.get("intel"), dict) else {}
    minimal = {}
    for key in ("name", "category", "website", "phone"):
        if _present(trimmed.get(key)):
            minimal[key] = trimmed[key]
    fallback: dict[str, object] = {"intel": {}}
    for key in ("label", "offer", "summary"):
        if _present(intel.get(key)):
            fallback["intel"][key] = str(intel[key])[:500]
    fallback.update(minimal)
    return _dump(fallback)[:limit]


def build_messages(lead: dict, intel: dict) -> list[dict]:
    """Готовые сообщения для chat/completions.

    Факты передаются одним компактным JSON: модель не должна ничего
    додумывать, а лишние поля только увеличивают риск выдумок.
    """
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _fit(_lead_payload(lead, intel))},
    ]


# ---------------------------------------------------------------------------
# Запрос к модели
# ---------------------------------------------------------------------------


def _result(ok: bool, text: str, error: str, model: str) -> dict:
    return {"ok": ok, "text": text, "error": error, "model": model}


def _content_text(data: object) -> str:
    """Достаёт текст ответа; поддерживает строку и список частей."""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return ""


async def generate_pitch(lead: dict, intel: dict) -> dict:
    """Пишет первое сообщение бизнесу. Никогда не бросает исключений.

    Возвращает ``{"ok": bool, "text": str, "error": str, "model": str}``.
    """
    model = _model()
    key = _api_key()
    if not key:
        return _result(False, "", "not_configured", model)

    url = f"{_base_url()}/chat/completions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    try:
        # Сборка сообщений тоже внутри try: кривые данные лида не должны
        # дойти до HTTP-слоя и уронить обработчик.
        payload = {
            "model": model,
            "messages": build_messages(lead, intel),
            "temperature": 0.6,
            "max_tokens": 500,
        }
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            data = response.json()
        text = _content_text(data).strip()
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        body = ""
        try:
            body = str(exc.response.text)[:300]
        except Exception:
            body = ""
        logger.warning("LLM ответил HTTP %s: %s", status_code, body)
        return _result(False, "", f"http_{status_code}", model)
    except httpx.TimeoutException:
        logger.warning("LLM не ответил за %s с", _timeout())
        return _result(False, "", "timeout", model)
    except Exception as exc:
        # Только класс ошибки: ни ключ, ни заголовки в лог не попадают.
        logger.warning("ошибка запроса к LLM: %s", exc.__class__.__name__)
        return _result(False, "", exc.__class__.__name__, model)

    if not text:
        logger.info("LLM вернул пустой текст")
        return _result(False, "", "empty_response", model)

    return _result(True, text, "", model)
