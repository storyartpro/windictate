"""DeepSeek API client for the optional "improve text" action.

Nothing is sent anywhere unless the user presses «Улучшить» in the dictation window.
The API key is stored in settings.json (plain text, like every other local setting) and is
never written to logs.

Public API:

    improve_text(text, *, api_key, model, preset, strength, quality, timeout) -> str
    check_key(api_key, *, model, timeout) -> tuple[bool, str]

Both raise ``LlmError`` with a Russian, user-readable message on failure.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
MODELS = ("deepseek-chat", "deepseek-reasoner")
PRESETS = {"light": "Мягко", "standard": "Стандарт", "strong": "Сильно"}
DEFAULT_PRESET = "standard"

# Preset → (strength, quality) used when the user picks a preset in the UI.
PRESET_DEFAULTS = {
    "light": (1, 3),
    "standard": (3, 3),
    "strong": (5, 4),
}

_STRENGTH_HINT = {
    1: "Сила правок 1/5: только бесспорные ошибки, формулировки не менять.",
    2: "Сила правок 2/5: консервативно, меняй только явные ошибки и неловкие места.",
    3: "Сила правок 3/5: умеренная редактура, сохраняй авторский тон.",
    4: "Сила правок 4/5: заметная редактура, можно перестраивать предложения.",
    5: "Сила правок 5/5: смелая редактура, перестраивай как считаешь лучше — но не теряй смысл, факты и имена.",
}

_QUALITY_HINT = {
    1: "Качество 1/5: один быстрый проход.",
    2: "Качество 2/5: проверь пунктуацию и орфографию.",
    3: "Качество 3/5: проверь пунктуацию, орфографию и согласование.",
    4: "Качество 4/5: проверь пунктуацию, орфографию, падежи, времена, повторы и канцелярит.",
    5: "Качество 5/5: полная вычитка: орфография, пунктуация, падежи, времена, повторы, канцелярит; сверь числа и имена.",
}

_PRESET_HINT = {
    "light": "Задача: расставить знаки препинания, исправить орфографию и грамматику. Не переписывай формулировки и не меняй порядок слов, кроме явных ошибок.",
    "standard": "Задача: пунктуация, орфография, грамматика и аккуратная стилистика. Сохрани смысл, факты, числа, имена и тон автора; правь только то, что действительно улучшает текст.",
    "strong": "Задача: редактура. Исправь пунктуацию и грамматику, перепиши неудачные места яснее и короче. Сохрани смысл, факты, числа, имена и намерение автора.",
}


class LlmError(RuntimeError):
    """User-facing error while talking to the DeepSeek API."""


def _base_url() -> str:
    return os.environ.get("WIN_DICTATE_LLM_BASE_URL", BASE_URL).rstrip("/")


def _temperature(model: str, quality: int) -> float | None:
    """Reasoner does not accept a custom temperature; chat models do."""
    if model == "deepseek-reasoner":
        return None
    return {1: 0.2, 2: 0.3, 3: 0.4, 4: 0.55, 5: 0.7}.get(quality, 0.4)


def build_messages(text: str, *, preset: str, strength: int, quality: int) -> list[dict]:
    preset = preset if preset in PRESETS else DEFAULT_PRESET
    strength = max(1, min(5, int(strength)))
    quality = max(1, min(5, int(quality)))
    system = (
        "Ты — редактор русского текста. Пользователь присылает текст, распознанный с голоса. "
        "Верни только исправленный текст: без пояснений, без markdown, без кавычек вокруг "
        "всего текста. Не добавляй ничего, чего не было в исходнике.\n"
        f"{_PRESET_HINT[preset]}\n{_STRENGTH_HINT[strength]}\n{_QUALITY_HINT[quality]}"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": text},
    ]


def _request_json(path: str, payload: dict | None, api_key: str, timeout: float) -> dict:
    url = _base_url() + path
    headers = {
        "Authorization": "Bearer " + api_key,
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json",
    }
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=max(5.0, float(timeout))) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc)
        if exc.code == 401:
            raise LlmError("DeepSeek отклонил ключ (401). Проверьте API-ключ в настройках.") from exc
        if exc.code == 402:
            raise LlmError("На балансе DeepSeek закончились средства (402).") from exc
        if exc.code == 429:
            raise LlmError("Слишком много запросов к DeepSeek (429) — попробуйте позже.") from exc
        raise LlmError(f"Ошибка DeepSeek: HTTP {exc.code}{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise LlmError(f"DeepSeek недоступен: {reason}") from exc
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise LlmError("DeepSeek вернул некорректный JSON.") from exc


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
        message = (payload or {}).get("error", {}).get("message")
        if message:
            return f" — {message}"
    except Exception:  # noqa: BLE001 - error detail is best effort
        pass
    return ""


def improve_text(
    text: str,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    preset: str = DEFAULT_PRESET,
    strength: int = 3,
    quality: int = 3,
    timeout: float = 90.0,
) -> str:
    """Send the text to DeepSeek and return the improved version.

    Raises ``LlmError`` (never returns the unchanged text on failure).
    """
    key = (api_key or "").strip()
    if not key:
        raise LlmError("Не задан API-ключ DeepSeek — укажите его в настройках.")
    body = (text or "").strip()
    if not body:
        raise LlmError("Нет текста для улучшения.")
    model = model if model in MODELS else DEFAULT_MODEL
    quality = max(1, min(5, int(quality)))
    payload: dict = {
        "model": model,
        "messages": build_messages(body, preset=preset, strength=strength, quality=quality),
        "max_tokens": 4096,
        "stream": False,
    }
    temperature = _temperature(model, quality)
    if temperature is not None:
        payload["temperature"] = temperature
    data = _request_json("/chat/completions", payload, key, timeout)
    try:
        result = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LlmError("DeepSeek вернул ответ без текста.") from exc
    result = (result or "").strip()
    if not result:
        raise LlmError("DeepSeek вернул пустой ответ.")
    # Some models wrap the whole answer in quotes/fences; strip a single outer pair.
    if result.startswith("```") and result.endswith("```"):
        result = result.strip("`").strip()
        if "\n" in result:
            result = result.split("\n", 1)[1].rsplit("\n", 1)[0].strip()
    if len(result) >= 2 and result[0] in "\"«":
        closing = {"\"": "\"", "«": "»"}.get(result[0])
        if closing and result[-1] == closing:
            result = result[1:-1].strip()
    return result


def check_key(
    api_key: str,
    *,
    model: str = DEFAULT_MODEL,
    timeout: float = 30.0,
) -> tuple[bool, str]:
    """Lightweight key check via GET /models. Returns (ok, Russian message)."""
    key = (api_key or "").strip()
    if not key:
        return False, "Ключ не задан."
    try:
        _request_json("/models", None, key, timeout)
    except LlmError as exc:
        return False, str(exc)
    return True, "Ключ работает."
