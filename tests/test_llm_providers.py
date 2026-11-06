import json

import httpx
import pytest

from app.llm_providers import GroqLLM, OllamaLLM


class Recorder:
    def __init__(self, response: httpx.Response):
        self.requests: list[httpx.Request] = []
        self._response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._response


# ---------------------------------------------------------------- OllamaLLM

def test_ollama_complete_sends_generate_request():
    rec = Recorder(httpx.Response(200, json={"response": "This is the assistant reply."}))
    llm = OllamaLLM(model="llama3", base_url="http://localhost:11434")
    llm._http = httpx.Client(transport=httpx.MockTransport(rec))  # noqa: SLF001

    out = llm.complete("You are helpful.", "Say hello.", max_tokens=100)

    assert out == "This is the assistant reply."
    req = rec.requests[0]
    assert req.url.path == "/api/generate"
    assert req.method == "POST"
    payload = json.loads(req.content)
    assert payload["model"] == "llama3"
    assert payload["stream"] is False
    assert payload["options"]["num_predict"] == 100
    assert "System: You are helpful." in payload["prompt"]
    assert "User: Say hello." in payload["prompt"]


def test_ollama_name_includes_model():
    llm = OllamaLLM(model="mistral")
    assert llm.name == "ollama-mistral"


def test_ollama_raises_on_non_200():
    rec = Recorder(httpx.Response(500, text="server error"))
    llm = OllamaLLM(model="llama3")
    llm._http = httpx.Client(transport=httpx.MockTransport(rec))  # noqa: SLF001
    with pytest.raises(httpx.HTTPStatusError):
        llm.complete("sys", "user")


# ---------------------------------------------------------------- GroqLLM

def test_groq_complete_sends_chat_completions():
    rec = Recorder(
        httpx.Response(200, json={"choices": [{"message": {"content": "Groq reply here"}}]})
    )
    llm = GroqLLM(model="llama-3.1-8b-instant", api_key="gsk_test123")
    llm._http = httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(rec),
        headers={"Authorization": "Bearer gsk_test123"},
    )

    out = llm.complete("You are helpful.", "Say hello.", max_tokens=150)

    assert out == "Groq reply here"
    req = rec.requests[0]
    assert req.url.path == "/openai/v1/chat/completions"
    assert req.method == "POST"
    assert req.headers["authorization"] == "Bearer gsk_test123"
    payload = json.loads(req.content)
    assert payload["model"] == "llama-3.1-8b-instant"
    assert payload["max_tokens"] == 150
    assert payload["temperature"] == 0.3
    assert payload["messages"][0]["role"] == "system"
    assert payload["messages"][0]["content"] == "You are helpful."
    assert payload["messages"][1]["role"] == "user"
    assert payload["messages"][1]["content"] == "Say hello."


def test_groq_name_includes_model():
    llm = GroqLLM(model="mixtral-8x7b-32768", api_key="gsk_test123")
    assert llm.name == "groq-mixtral-8x7b-32768"


def test_groq_raises_on_non_200():
    rec = Recorder(httpx.Response(401, text="unauthorized"))
    llm = GroqLLM(model="llama-3.1-8b-instant", api_key="bad")
    llm._http = httpx.Client(  # noqa: SLF001
        transport=httpx.MockTransport(rec),
        headers={"Authorization": "Bearer bad"},
    )
    with pytest.raises(httpx.HTTPStatusError):
        llm.complete("sys", "user")


def test_groq_requires_api_key():
    with pytest.raises(ValueError, match="GROQ_API_KEY"):
        GroqLLM(api_key="")


def test_groq_reasoning_model_gets_low_effort_and_room_to_answer():
    seen = {}

    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    llm = GroqLLM(model="openai/gpt-oss-20b", api_key="gsk_x")
    llm._http = httpx.Client(transport=httpx.MockTransport(handler))
    assert llm.complete("s", "u", max_tokens=600) == "ok"
    assert seen["reasoning_effort"] == "low" and seen["max_tokens"] >= 2000
    seen.clear()
    plain = GroqLLM(model="llama-3.1-8b-instant", api_key="gsk_x")
    plain._http = httpx.Client(transport=httpx.MockTransport(handler))
    plain.complete("s", "u", max_tokens=600)
    assert "reasoning_effort" not in seen and seen["max_tokens"] == 600


def _groq(model, handler):
    import httpx
    from app.llm_providers import GroqLLM
    g = GroqLLM(model, api_key="k")
    g._http = httpx.Client(transport=httpx.MockTransport(handler))
    return g


def test_gpt_oss_gets_short_hidden_reasoning_and_a_bigger_budget():
    import httpx, json
    seen = []

    def h(req):
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    assert _groq("openai/gpt-oss-20b", h).complete("s", "u", 600) == "ok"
    assert seen[0]["reasoning_effort"] == "low" and seen[0]["include_reasoning"] is False and seen[0]["max_tokens"] >= 2000


def test_reasoning_options_rejected_with_400_retry_once_without_them_and_other_models_never_get_them():
    import httpx, json
    seen = []

    def h(req):
        b = json.loads(req.content); seen.append(b)
        return httpx.Response(400, json={}) if "reasoning_effort" in b else httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})
    assert _groq("openai/gpt-oss-20b", h).complete("s", "u") == "ok" and len(seen) == 2 and "reasoning_effort" not in seen[1]
    seen.clear()
    assert _groq("qwen/qwen3.8-27b", h).complete("s", "u") == "ok" and len(seen) == 1 and "reasoning_effort" not in seen[0]
