import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.orders import OrderStore
from app.paypal import PayPalClient
from app.store import Store
from app.web import create_app
from tests.test_web_exec import AUTH, BASE, ID, PP, PW

SIG = {"paypal-auth-algo": "SHA256withRSA", "paypal-cert-url": "https://api.sandbox.paypal.com/cert",
       "paypal-transmission-id": "tx-1", "paypal-transmission-sig": "sig", "paypal-transmission-time": "2026-11-02T20:00:00Z"}
EVENT = {"id": "WH-1", "event_type": "CUSTOMER.DISPUTE.CREATED", "resource": {"dispute_id": ID}}


class Hook(PP):
    def __init__(self, verdict="SUCCESS", **kw):
        super().__init__(**kw)
        self.verdict, self.verify_bodies, self.gets, self.verify_down = verdict, [], [], False

    def __call__(self, req):
        if req.url.path == "/v1/notifications/verify-webhook-signature":
            if self.verify_down:
                raise httpx.ConnectError("down", request=req)
            self.verify_bodies.append(req.content)
            return httpx.Response(200, json={"verification_status": self.verdict})
        if req.method == "GET" and req.url.path != "/v1/oauth2/token":
            self.gets.append(req.url.path)
        return super().__call__(req)


def make(hook=None, webhook_id="WID-1"):
    hook = hook or Hook()
    c = PayPalClient(Settings("i", "s", BASE), transport=httpx.MockTransport(hook), backoff=0, connect_attempts=1)
    s = Store()
    o = OrderStore(data={"76X00088A7532581F": {"digital_access_log": True}})
    return TestClient(create_app(c, s, o, BASE, PW, webhook_id=webhook_id)), s, hook


def post(client, event=EVENT, headers=SIG, raw=None):
    return client.post("/webhooks/paypal", content=raw if raw is not None else json.dumps(event), headers=headers)


def test_verified_event_triggers_fetch_and_proposal_without_basic_auth():
    client, s, hook = make()
    r = post(client)                                       # note: no auth header, PayPal cannot send one
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert f"/v1/customer/disputes/{ID}" in hook.gets      # we re-fetched; we did not trust the payload
    assert len(s.list_proposals("PENDING")) == 1 and hook.posts == []   # nothing written to PayPal
    assert any(x["event"] == "webhook.accepted" for x in s.audit_rows()) and s.verify_audit()[0]


def test_other_routes_still_need_the_password():
    client, *_ = make()
    assert client.get("/").status_code == 401


def test_verification_request_carries_headers_webhook_id_and_exact_raw_event():
    client, s, hook = make()
    raw = '{"id":"WH-1",  "event_type":"CUSTOMER.DISPUTE.CREATED","resource":{"dispute_id":"%s"},"x":1.0}' % ID
    post(client, raw=raw)
    body = hook.verify_bodies[0]
    sent = json.loads(body)
    assert sent["webhook_id"] == "WID-1" and sent["transmission_id"] == "tx-1" and sent["auth_algo"] == "SHA256withRSA"
    assert raw.encode() in body                            # byte-for-byte, odd spacing and 1.0 preserved


def test_forged_event_is_rejected_and_not_processed():
    client, s, hook = make(Hook(verdict="FAILURE"))
    r = post(client)
    assert r.status_code == 400 and s.list_proposals() == [] and hook.gets == []
    ev = [x for x in s.audit_rows() if x["event"] == "webhook.rejected"]
    assert ev and "signature" in ev[0]["payload"]


def test_replay_is_acknowledged_but_processed_once():
    client, s, hook = make()
    assert post(client).json() == {"ok": True}
    second = post(client)
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert hook.gets.count(f"/v1/customer/disputes/{ID}") == 1


def test_disabled_without_webhook_id_fails_closed():
    client, s, hook = make(webhook_id=None)
    assert post(client).status_code == 503 and hook.verify_bodies == [] and s.list_proposals() == []


def test_missing_headers_malformed_and_oversized_bodies():
    client, s, hook = make()
    assert post(client, headers={}).status_code == 400
    assert post(client, raw="not json").status_code == 400
    assert post(client, raw="[1,2]").status_code == 400
    assert post(client, raw="x" * (256 * 1024 + 1)).status_code == 413
    assert hook.verify_bodies == []                         # nothing reached the verifier


def test_paypal_unreachable_for_verification_returns_503_so_paypal_retries():
    hook = Hook(); hook.verify_down = True
    client, s, _ = make(hook)
    assert post(client).status_code == 503 and s.list_proposals() == []
    hook.verify_down = False
    assert post(client).status_code == 200                  # the retry succeeds and is not treated as a duplicate


def test_unhandled_event_type_and_odd_dispute_ids_are_ignored():
    client, s, hook = make()
    assert post(client, {"id": "WH-2", "event_type": "PAYMENT.SALE.COMPLETED", "resource": {"id": "X"}}).status_code == 200
    assert post(client, {"id": "WH-3", "event_type": "CUSTOMER.DISPUTE.UPDATED", "resource": {"dispute_id": "../../etc"}}).status_code == 200
    assert not any("etc" in g for g in hook.gets)                       # a hostile id is never used in a path


def test_ingest_failure_is_audited_and_never_crashes_the_endpoint():
    class Broken(Hook):
        def __call__(self, req):
            if req.method == "GET" and req.url.path.startswith("/v1/customer/disputes/"):
                raise RuntimeError("boom")
            return super().__call__(req)
    client, s, _ = make(Broken())
    assert post(client).status_code == 200
    assert any(x["event"] == "webhook.ingest_error" for x in s.audit_rows())


def test_verified_dispute_event_without_a_usable_id_falls_back_to_polling():
    client, s, hook = make()
    r = post(client, {"id": "WH-9", "event_type": "CUSTOMER.DISPUTE.CREATED", "resource": {"something": "else"}})
    assert r.status_code == 200
    assert any(g.startswith("/v1/customer/disputes") and "PP-" not in g for g in hook.gets)   # list call = poll
    assert any(x["event"] == "webhook.no_dispute_id" for x in s.audit_rows())
