import argparse
import json

import httpx
import pytest

from app.config import ConfigError, Settings
from app.paypal import PayPalClient
from spike import spike

BASE = "https://api-m.sandbox.paypal.com"

DISPUTE = {
    "dispute_id": "PP-D-1", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED", "status": "WAITING_FOR_SELLER_RESPONSE",
    "dispute_life_cycle_stage": "INQUIRY", "dispute_amount": {"currency_code": "USD", "value": "25.00"},
    "seller_response_due_date": "2026-11-10T00:00:00.000Z", "disputed_transactions": [{}], "messages": [], "evidences": [],
    "links": [{"rel": "provide-evidence", "method": "POST", "href": f"{BASE}/v1/customer/disputes/PP-D-1/provide-evidence"}],
}


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        p = request.url.path
        if p == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "ACCESS-TOKEN-VALUE-123", "expires_in": 32000,
                                             "scope": "openid https://uri.paypal.com/services/disputes/update-seller https://uri.paypal.com/services/disputes/read-seller"})
        if p == "/v1/customer/disputes" and request.method == "GET":
            return httpx.Response(200, json={"items": [DISPUTE]})
        if p == "/v1/customer/disputes/PP-D-1" and request.method == "GET":
            return httpx.Response(200, json=DISPUTE)
        if p.endswith("/provide-evidence"):
            return httpx.Response(200, json={"links": []}, headers={"paypal-debug-id": "dbg1"})
        if p == "/v2/checkout/orders":
            return httpx.Response(201, json={"id": "ORD1", "links": [{"rel": "payer-action", "href": "https://sandbox.paypal.com/approve"}]})
        if p == "/v1/notifications/webhooks-event-types":
            return httpx.Response(200, json={"event_types": [{"name": "CUSTOMER.DISPUTE.CREATED", "description": "d"}, {"name": "PAYMENT.CAPTURE.COMPLETED"}]})
        return httpx.Response(404, json={"name": "NOT_FOUND"})


@pytest.fixture
def env(tmp_path):
    rec = Recorder()
    settings = Settings("id", "secret", BASE, str(tmp_path))
    client = PayPalClient(settings, transport=httpx.MockTransport(rec))
    return client, settings, rec, tmp_path


def ns(**kw):
    return argparse.Namespace(**kw)


def test_refuses_non_sandbox_url(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "a")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "b")
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_missing_credentials_fail_fast(monkeypatch):
    monkeypatch.delenv("PAYPAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("PAYPAL_CLIENT_SECRET", raising=False)
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_client_rejects_live_host():
    with pytest.raises(ValueError):
        PayPalClient(Settings("a", "b", "https://api-m.paypal.com"))


def test_token_is_cached_and_never_audited_with_secret(env):
    client, _s, rec, _ = env
    events: list[dict] = []
    client._audit = events.append  # noqa: SLF001
    client.get("/v1/customer/disputes")
    client.get("/v1/customer/disputes")
    assert sum(r.url.path == "/v1/oauth2/token" for r in rec.requests) == 1
    dumped = json.dumps(events)
    assert "ACCESS-TOKEN-VALUE-123" not in dumped and "secret" not in dumped


def test_post_sends_idempotency_header(env):
    client, _s, rec, _ = env
    client.post("/v2/checkout/orders", json={})
    assert rec.requests[-1].headers["paypal-request-id"]


def test_check_reports_disputes_scopes(env, capsys):
    client, s, _r, _ = env
    assert spike.cmd_check(client, s, ns()) == 0
    assert "disputes/update-seller" in capsys.readouterr().out


def test_check_fails_without_disputes_scope(env, capsys):
    client, s, _r, _ = env
    client._http = httpx.Client(base_url=BASE, transport=httpx.MockTransport(  # noqa: SLF001
        lambda r: httpx.Response(200, json={"access_token": "t", "expires_in": 100, "scope": "openid"})))
    assert spike.cmd_check(client, s, ns()) == 2
    assert "Enable Disputes" in capsys.readouterr().out


def test_events_filters_dispute_events(env, capsys):
    client, s, _r, _ = env
    spike.cmd_events(client, s, ns())
    out = capsys.readouterr().out
    assert "CUSTOMER.DISPUTE.CREATED" in out and "PAYMENT.CAPTURE.COMPLETED" not in out


def test_list_and_show_print_action_links_and_save_fixture(env, capsys):
    client, s, _r, tmp = env
    assert spike.cmd_list(client, s, ns(state=None)) == 0
    assert "PP-D-1" in capsys.readouterr().out
    assert spike.cmd_show(client, s, ns(dispute_id="PP-D-1")) == 0
    out = capsys.readouterr().out
    assert "provide-evidence" in out and "WAITING_FOR_SELLER_RESPONSE" in out
    assert any(p.name.endswith("dispute_PP-D-1.json") for p in tmp.iterdir())


def test_order_prints_buyer_approval_link(env, capsys):
    client, s, _r, _ = env
    assert spike.cmd_order(client, s, ns(amount="25.00")) == 0
    assert "https://sandbox.paypal.com/approve" in capsys.readouterr().out


def test_state_changing_probe_requires_yes(env):
    client, s, rec, tmp = env
    f = tmp / "e.txt"
    f.write_text("proof")
    with pytest.raises(SystemExit):
        spike.cmd_probe_evidence(client, s, ns(dispute_id="PP-D-1", file=str(f), yes=False))
    assert not any(r.url.path.endswith("provide-evidence") for r in rec.requests)


def test_probe_evidence_sends_multipart_evidence_file(env):
    client, s, rec, tmp = env
    f = tmp / "e.txt"
    f.write_text("proof")
    assert spike.cmd_probe_evidence(client, s, ns(dispute_id="PP-D-1", file=str(f), yes=True)) == 0
    req = rec.requests[-1]
    assert req.headers["content-type"].startswith("multipart/form-data")
    assert b'name="evidence-file"' in req.content


def test_probe_evidence_input_sends_json_part(env):
    client, s, rec, _ = env
    assert spike.cmd_probe_evidence_input(client, s, ns(dispute_id="PP-D-1", tracking="T123", carrier="DHL", yes=True)) == 0
    assert b'name="input"' in rec.requests[-1].content and b"T123" in rec.requests[-1].content
