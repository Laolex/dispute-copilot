import httpx
import pytest

from app.config import Settings
from app.paypal import PayPalClient, PayPalUnreachable
from spike import spike

BASE = "https://api-m.sandbox.paypal.com"
TOKEN = {"access_token": "T", "expires_in": 3000, "scope": "x"}


def client(handler, **kw):
    return PayPalClient(Settings("id", "secret", BASE), transport=httpx.MockTransport(handler), backoff=0, **kw)


def test_connect_timeout_is_retried_then_succeeds():
    calls = {"token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            calls["token"] += 1
            if calls["token"] < 3:
                raise httpx.ConnectTimeout("slow", request=req)
            return httpx.Response(200, json=TOKEN)
        return httpx.Response(200, json={"items": []})

    r = client(handler).get("/v1/customer/disputes")
    assert r.ok and calls["token"] == 3


def test_gives_up_with_friendly_error_after_all_attempts():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=req)

    with pytest.raises(PayPalUnreachable) as e:
        client(handler, connect_attempts=3).get("/v1/customer/disputes")
    assert "after 3 attempts" in str(e.value)


def test_read_timeout_is_not_retried():
    hits = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        hits["n"] += 1
        raise httpx.ReadTimeout("no reply", request=req)

    with pytest.raises(httpx.ReadTimeout):
        client(handler).post("/v2/checkout/orders", json={})
    assert hits["n"] == 1


def test_retried_post_reuses_the_same_idempotency_key():
    seen: list[str] = []
    state = {"fail": 1}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        seen.append(req.headers["paypal-request-id"])
        if state["fail"]:
            state["fail"] -= 1
            raise httpx.ConnectError("refused", request=req)
        return httpx.Response(201, json={"id": "O1"})

    assert client(handler).post("/v2/checkout/orders", json={}).status == 201
    assert len(seen) == 2 and seen[0] == seen[1]


def test_spike_main_prints_friendly_message_and_exit_3(monkeypatch, capsys):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "a")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "b")

    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=req)

    monkeypatch.setattr(spike, "PayPalClient", lambda settings, audit=None: PayPalClient(
        settings, audit=audit, transport=httpx.MockTransport(handler), backoff=0, connect_attempts=2))
    assert spike.main(["list"]) == 3
    assert "network error" in capsys.readouterr().err
