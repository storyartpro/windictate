"""Headless tests for dictate/llm.py (DeepSeek client). No network access.

Run: python tests/test_llm.py
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dictate import llm  # noqa: E402

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"ok   {name}")
    else:
        FAIL += 1
        print(f"FAIL {name} {detail}")


class FakeResponse:
    def __init__(self, payload: dict | str) -> None:
        self._raw = payload if isinstance(payload, str) else json.dumps(payload)

    def read(self) -> bytes:
        return self._raw.encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeUrlOpen:
    def __init__(self, payload: dict | str | urllib.error.HTTPError) -> None:
        self.payload = payload
        self.requests: list[urllib.request.Request] = []

    def __call__(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.payload, urllib.error.HTTPError):
            raise self.payload
        return FakeResponse(self.payload)

    def last_json(self) -> dict:
        return json.loads(self.requests[-1].data.decode("utf-8"))


def patch_urlopen(payload) -> FakeUrlOpen:
    fake = FakeUrlOpen(payload)
    llm.urllib.request.urlopen = fake  # type: ignore[assignment]
    return fake


def http_error(code: int, message: str) -> urllib.error.HTTPError:
    body = json.dumps({"error": {"message": message}}).encode("utf-8")
    return urllib.error.HTTPError(
        "https://api.deepseek.com/chat/completions", code, message, None, io.BytesIO(body)
    )


def completion(content: str) -> dict:
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def main() -> int:
    real_urlopen = llm.urllib.request.urlopen
    try:
        # --- prompt construction ------------------------------------------------
        messages = llm.build_messages("привет мир", preset="light", strength=1, quality=3)
        check("messages: system+user", [m["role"] for m in messages] == ["system", "user"])
        system_light = messages[0]["content"]
        check("prompt: light preset hint", "Не переписывай" in system_light)
        check("prompt: strength hint", "1/5" in system_light)
        check("prompt: quality hint", "Качество 3/5" in system_light)
        system_strong = llm.build_messages("x", preset="strong", strength=5, quality=5)[0]["content"]
        check("prompt: strong differs", "редактура" in system_strong and system_strong != system_light)
        clamped = llm.build_messages("x", preset="unknown", strength=99, quality=-3)
        check("prompt: clamps unknown preset/values", "5/5" in clamped[0]["content"] and "1/5" in clamped[0]["content"])

        # --- request payload ----------------------------------------------------
        fake = patch_urlopen(completion("Привет, мир."))
        result = llm.improve_text("привет мир", api_key="sk-test", preset="standard",
                                  strength=3, quality=3, timeout=5)
        check("request: returns improved text", result == "Привет, мир.", result)
        payload = fake.last_json()
        check("request: model", payload["model"] == "deepseek-chat")
        check("request: stream false", payload["stream"] is False)
        check("request: temperature for quality 3", payload.get("temperature") == 0.4)
        check("request: max_tokens set", payload.get("max_tokens", 0) >= 1024)
        check("request: auth header", fake.requests[-1].get_header("Authorization") == "Bearer sk-test")
        check("request: user text passed", payload["messages"][1]["content"] == "привет мир")

        # --- reasoner: no temperature ------------------------------------------
        fake = patch_urlopen(completion("Готово."))
        llm.improve_text("текст", api_key="k", model="deepseek-reasoner", quality=5)
        check("reasoner: temperature omitted", "temperature" not in fake.last_json())
        check("reasoner: model in payload", fake.last_json()["model"] == "deepseek-reasoner")

        # --- answer cleanup -----------------------------------------------------
        fake = patch_urlopen(completion("```\nПривет, мир.\n```"))
        check("cleanup: fenced block stripped",
              llm.improve_text("x", api_key="k") == "Привет, мир.")
        fake = patch_urlopen(completion("«Привет, мир.»"))
        check("cleanup: guillemets stripped",
              llm.improve_text("x", api_key="k") == "Привет, мир.")

        # --- errors -------------------------------------------------------------
        def expect_error(name: str, payload, **kwargs) -> None:
            patch_urlopen(payload)
            try:
                llm.improve_text("текст", api_key="k", **kwargs)
            except llm.LlmError as exc:
                check(name, bool(str(exc)), str(exc))
            else:
                check(name, False, "no LlmError")

        expect_error("error: 401 mentions key", http_error(401, "Authentication Fails"))
        expect_error("error: 402 mentions balance", http_error(402, "Insufficient Balance"))
        expect_error("error: 429 mentions rate limit", http_error(429, "Rate limit"))
        expect_error("error: other http code", http_error(500, "boom"))
        expect_error("error: invalid json", "{not json")
        expect_error("error: empty content", completion(""))

        patch_urlopen(completion("x"))
        try:
            llm.improve_text("текст", api_key="   ")
        except llm.LlmError as exc:
            check("error: empty key", "API-ключ" in str(exc), str(exc))
        else:
            check("error: empty key", False)
        try:
            llm.improve_text("   ", api_key="k")
        except llm.LlmError as exc:
            check("error: empty text", "Нет текста" in str(exc), str(exc))
        else:
            check("error: empty text", False)

        # network failure
        def broken(request, timeout=None):
            raise urllib.error.URLError("connection refused")

        llm.urllib.request.urlopen = broken  # type: ignore[assignment]
        try:
            llm.improve_text("текст", api_key="k")
        except llm.LlmError as exc:
            check("error: network", "недоступен" in str(exc), str(exc))
        else:
            check("error: network", False)

        # --- base url / check_key ----------------------------------------------
        os_environ = __import__("os").environ
        old = os_environ.get("WIN_DICTATE_LLM_BASE_URL")
        os_environ["WIN_DICTATE_LLM_BASE_URL"] = "http://127.0.0.1:9999/"
        fake = patch_urlopen({"data": [{"id": "deepseek-chat"}]})
        ok, message = llm.check_key("sk-test", timeout=5)
        check("check_key: ok", ok and message == "Ключ работает.", f"{ok} {message}")
        check("check_key: GET /models", fake.requests[-1].get_method() == "GET" and
              fake.requests[-1].full_url == "http://127.0.0.1:9999/models",
              fake.requests[-1].full_url)
        patch_urlopen(http_error(401, "bad key"))
        ok, message = llm.check_key("k")
        check("check_key: failure message", not ok and "401" in message, message)
        ok, message = llm.check_key("")
        check("check_key: empty key", not ok and "не задан" in message.lower(), message)
        if old is None:
            os_environ.pop("WIN_DICTATE_LLM_BASE_URL", None)
        else:
            os_environ["WIN_DICTATE_LLM_BASE_URL"] = old
    finally:
        llm.urllib.request.urlopen = real_urlopen  # type: ignore[assignment]

    print(f"\nPASS {PASS}/{PASS + FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
