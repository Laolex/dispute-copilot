"""Free / local LLM providers implementing the app/agent.LLM Protocol.

No paid API key required:
- OllamaLLM: runs models locally via http://localhost:11434 (completely free, no key)
- GroqLLM: Groq free tier (console.groq.com) - free signup, no credit card

Both reuse httpx, already a project dependency. Nothing here talks to PayPal.
"""

from __future__ import annotations

import os
from typing import Protocol

import httpx

from app.agent import LLM


class OllamaLLM:
    """Local Ollama runner. Completely free; no API key.

    Requires Ollama installed and a model pulled, e.g.:
        ollama pull llama3
    """

    def __init__(self, model: str = "llama3", base_url: str = "http://localhost:11434") -> None:
        self.name = f"ollama-{model}"
        self._model = model.strip()
        self._base = base_url.rstrip("/")
        self._http = httpx.Client(timeout=60)

    def complete(self, system: str, user: str, max_tokens: int = 600) -> str:
        prompt = f"System: {system}\n\nUser: {user}\n\nAssistant:"
        r = self._http.post(
            f"{self._base}/api/generate",
            json={
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": max_tokens},
            },
        )
        r.raise_for_status()
        return r.json().get("response", "")


class GroqLLM:
    """Groq chat completion. Free tier (signup at console.groq.com, no credit card)."""

    def __init__(self, model: str = "llama-3.1-8b-instant", api_key: str | None = None) -> None:
        self.name = f"groq-{model}"
        self._model = model
        self._key = (api_key or os.environ.get("GROQ_API_KEY", "")).strip()
        if not self._key:
            raise ValueError("GROQ_API_KEY is required for GroqLLM")
        self._http = httpx.Client(
            timeout=60,
            headers={"Authorization": f"Bearer {self._key}"},
        )

    def complete(self, system: str, user: str, max_tokens: int = 600) -> str:
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": 0.3,
        }
        if "gpt-oss" in self._model:
            # Reasoning models spend completion tokens thinking before they answer. Keep that short and leave
            # room, otherwise a 600-token cap can be used up before any answer text is produced.
            body["reasoning_effort"] = "low"
            body["max_tokens"] = max(max_tokens, 2000)
        r = self._http.post("https://api.groq.com/openai/v1/chat/completions", json=body)
        r.raise_for_status()
        return r.json()["choices"][0]["message"].get("content") or ""

