"""Entrypoint: `DC_ADMIN_PASSWORD=... uvicorn app.main:app`. Requires PAYPAL_CLIENT_ID/SECRET.

AI provider selection via DC_LLM_PROVIDER:
  - anthropic: Anthropic Claude (needs ANTHROPIC_API_KEY)
  - groq:      Groq free tier (needs GROQ_API_KEY, free signup)
  - ollama:    Local Ollama (completely free, no key; run `ollama pull llama3` first)
  - auto:      anthropic if ANTHROPIC_API_KEY, else groq if GROQ_API_KEY, else offline fallback (default).
               Ollama is used only when DC_LLM_PROVIDER=ollama.

Optional alerts (one-way; approval stays in the console): TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID, and/or
TWILIO_ACCOUNT_SID + TWILIO_AUTH_TOKEN + TWILIO_WHATSAPP_FROM + WHATSAPP_TO; DC_PUBLIC_URL (https) adds a link;
DC_NOTIFY_HOURS (default 48) sets the deadline warning window.
"""

import os

from .agent import AnthropicLLM, build_agent
from .config import Settings
from .llm_providers import GroqLLM, OllamaLLM
from .notify import from_env as notifier_from_env
from .orders import OrderStore
from .paypal import PayPalClient
from .store import Store
from .web import create_app

settings = Settings.from_env()

_provider = os.environ.get("DC_LLM_PROVIDER", "auto").lower()
_llm = None

if _provider == "anthropic":
    if os.environ.get("ANTHROPIC_API_KEY"):
        _llm = AnthropicLLM(os.environ.get("DC_MODEL", "claude-haiku-5-5"))
elif _provider == "groq":
    _llm = GroqLLM(os.environ.get("DC_MODEL", "llama-3.1-8b-instant"))
elif _provider == "ollama":
    _llm = OllamaLLM(os.environ.get("DC_MODEL", "llama3"))
elif _provider == "auto":
    if os.environ.get("ANTHROPIC_API_KEY"):
        _llm = AnthropicLLM(os.environ.get("DC_MODEL", "claude-haiku-5-5"))
    elif os.environ.get("GROQ_API_KEY"):
        _llm = GroqLLM(os.environ.get("DC_MODEL", "llama-3.1-8b-instant"))
    # auto never falls back to Ollama: a hosted deploy has no localhost model, so every dispute would wait on a dead socket.
else:
    raise ValueError(f"unknown DC_LLM_PROVIDER: {_provider}")

agent = build_agent(_llm)
_store = Store(os.environ.get("DC_DB", "dispute_copilot.db"))
notifier = notifier_from_env(_store, os.environ)   # Telegram / WhatsApp alerts, if configured
app = create_app(PayPalClient(settings), _store,
                 OrderStore(os.environ.get("DC_ORDERS", "data/orders.json")), settings.base_url,
                 os.environ.get("DC_ADMIN_PASSWORD", ""), agent,
                 os.environ.get("DC_DOCS", "data/docs"), os.environ.get("PAYPAL_WEBHOOK_ID") or None)
