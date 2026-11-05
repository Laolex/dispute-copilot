"""Minimal PayPal REST client. Sandbox only.

Every request goes through `request()`, which:
  - logs method/path/status to the audit sink (never credentials, tokens, or bodies of auth calls),
  - sends a PayPal-Request-Id for POSTs (idempotency),
  - returns an `ApiResult` with status, parsed JSON (or text) and the PayPal debug id.
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx

from .config import Settings, is_sandbox_url

AuditSink = Callable[[dict], None]


class PayPalUnreachable(RuntimeError):
    """Could not open a connection to PayPal after retrying."""


@dataclass
class ApiResult:
    status: int
    body: Any
    debug_id: str | None = None
    headers: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class PayPalClient:
    def __init__(
        self,
        settings: Settings,
        audit: AuditSink | None = None,
        transport: httpx.BaseTransport | None = None,
        connect_attempts: int = 4,
        backoff: float = 2.0,
    ) -> None:
        if not is_sandbox_url(settings.base_url, base_only=True):
            raise ValueError("PayPalClient is sandbox-only")
        self._connect_attempts = connect_attempts
        self._backoff = backoff
        self._s = settings
        self._audit = audit or (lambda _e: None)
        self._http = httpx.Client(base_url=settings.base_url, timeout=30, transport=transport)
        self._token: str | None = None
        self._token_exp = 0.0
        self.granted_scopes: list[str] = []

    def close(self) -> None:
        self._http.close()

    def _with_connect_retry(self, send: Callable[[], httpx.Response]) -> httpx.Response:
        """Retry ONLY when no connection was established (so nothing reached PayPal).

        Read timeouts are deliberately not retried: the request may have been processed.
        """
        last: Exception | None = None
        for attempt in range(1, self._connect_attempts + 1):
            try:
                return send()
            except (httpx.ConnectTimeout, httpx.ConnectError) as exc:
                last = exc
                self._audit({"call": f"connect-retry {attempt}/{self._connect_attempts}",
                             "status": 0, "error": type(exc).__name__})
                if attempt < self._connect_attempts:
                    time.sleep(self._backoff * attempt)
        raise PayPalUnreachable(
            f"could not connect to {self._s.base_url} after {self._connect_attempts} attempts "
            f"({type(last).__name__}); check your network and try again"
        ) from last

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = self._with_connect_retry(lambda: self._http.post(
            "/v1/oauth2/token",
            auth=(self._s.client_id, self._s.client_secret),
            data={"grant_type": "client_credentials"},
        ))
        self._audit({"call": "POST /v1/oauth2/token", "status": r.status_code})
        r.raise_for_status()
        body = r.json()
        self._token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 300))
        self.granted_scopes = sorted((body.get("scope") or "").split())
        return self._token

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: Any = None,
        files: dict | None = None,
        data: dict | None = None,
        content: bytes | None = None,
        request_id: str | None = None,
    ) -> ApiResult:
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        if method.upper() == "POST":
            headers["PayPal-Request-Id"] = request_id or uuid.uuid4().hex
        if json is not None or content is not None:
            headers["Content-Type"] = "application/json"
        r = self._with_connect_retry(lambda: self._http.request(
            method, path, params=params, json=json, files=files, data=data, content=content, headers=headers))
        try:
            body: Any = r.json() if r.content else None
        except ValueError:
            body = r.text
        debug_id = r.headers.get("paypal-debug-id")
        self._audit({"call": f"{method.upper()} {path}", "status": r.status_code, "debug_id": debug_id})
        return ApiResult(r.status_code, body, debug_id, dict(r.headers))

    # Convenience wrappers -------------------------------------------------
    def get(self, path: str, **kw: Any) -> ApiResult:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> ApiResult:
        return self.request("POST", path, **kw)
