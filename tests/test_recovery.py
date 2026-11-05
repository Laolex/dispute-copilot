"""Execution recovery: a write whose answer never arrives must not become FAILED or be silently repeated."""
import json

import httpx
import pytest

from app.executor import abandon, execute, reconcile
from app.ingest import poll
from tests.test_web_exec import AUTH, ID, PP, token, make


class Flaky(PP):
    """POSTs time out (or 503) until `heal` is set; GETs return the dispute in `kw`/`override` shape."""
    def __init__(self, mode="timeout", **kw):
        super().__init__(**kw)
        self.mode, self.override, self.attempts = mode, None, []

    def __call__(self, req):
        if req.method == "POST" and req.url.path != "/v1/oauth2/token":
            self.attempts.append(req.headers.get("paypal-request-id"))
            if self.mode == "timeout":
                raise httpx.ReadTimeout("no answer", request=req)
            if self.mode == "503":
                return httpx.Response(503, json={"name": "SERVICE_UNAVAILABLE"})
        if req.method == "GET" and req.url.path.startswith("/v1/customer/disputes/") and self.override:
            body = self.override(super().__call__(req).json())
            return httpx.Response(200, json=body)
        return super().__call__(req)


def approved(pp):
    client, s, c, _, o = make(pp)
    pid = poll(c, s, o)[0]
    s.transition(pid, "APPROVED", by="x")
    return client, s, c, o, pid


@pytest.mark.parametrize("mode", ["timeout", "503"])
def test_unanswered_write_becomes_uncertain_not_failed(mode):
    pp = Flaky(mode); client, s, c, o, pid = approved(pp)
    res = execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o)
    assert res["uncertain"] is True and s.get_proposal(pid)["state"] == "UNCERTAIN"
    assert len(pp.attempts) == 1                                   # exactly one send; never repeated automatically
    assert any(r["event"] == "execute.uncertain" for r in s.audit_rows())


def test_reconcile_applied_closes_as_executed():
    pp = Flaky("timeout"); client, s, c, o, pid = approved(pp)
    execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o)
    # the proposal is a provide_evidence contest: PayPal now lists one more submitted evidence item
    def more(b):
        b = json.loads(json.dumps(b)); b["evidences"] = (b.get("evidences") or []) + [{"evidence_type": "PROOF_OF_FULFILLMENT", "source": "SUBMITTED_BY_SELLER"}]
        return b
    pp.override = more
    out = reconcile(c, s, pid)
    assert out["outcome"] == "applied" and s.get_proposal(pid)["state"] == "EXECUTED"
    assert s.get_proposal(pid)["result"]["reconciled"] is True


def test_retry_requires_reconcile_first_then_reuses_request_id():
    pp = Flaky("timeout"); client, s, c, o, pid = approved(pp)
    execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o)
    with pytest.raises(PermissionError):
        execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o, retry=True)      # not reconciled yet
    out = reconcile(c, s, pid)
    assert out["outcome"] == "not_applied"
    pp.mode = "ok"                                                                         # PayPal is back
    res = execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o, retry=True)
    assert res["ok"] is True and s.get_proposal(pid)["state"] == "EXECUTED"
    assert pp.attempts == [f"dc-{pid}", f"dc-{pid}"] and pp.posts[-1][0].endswith("provide-evidence")  # both tries carry the same idempotency key


def test_retry_refused_if_dispute_changed_since_reconcile_stays_uncertain():
    pp = Flaky("timeout"); client, s, c, o, pid = approved(pp)
    execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o)
    reconcile(c, s, pid)
    pp.mode = "ok"; pp.kw = {"update": "2026-11-02T15:00:00Z"}                            # buyer acted meanwhile
    res = execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o, retry=True)
    assert res["ok"] is False and "stale" in res["reason"]
    assert s.get_proposal(pid)["state"] == "UNCERTAIN" and pp.posts == []


def test_unreachable_paypal_sends_nothing_and_keeps_approved():
    pp = Flaky("ok"); client, s, c, o, pid = approved(pp)
    real = pp.__call__
    def refuse(req):
        if req.method == "POST" and req.url.path != "/v1/oauth2/token":
            raise httpx.ConnectError("down", request=req)
        return real(req)
    c._http = httpx.Client(base_url="https://api-m.sandbox.paypal.com", transport=httpx.MockTransport(refuse))
    res = execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o)
    assert res["sent"] is False and s.get_proposal(pid)["state"] == "APPROVED"


def test_human_can_close_a_stuck_proposal_and_it_is_audited():
    pp = Flaky("timeout"); client, s, c, o, pid = approved(pp)
    execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o)
    abandon(s, pid, "laolex")
    assert s.get_proposal(pid)["state"] == "FAILED"
    assert any(r["event"] == "execute.abandoned" for r in s.audit_rows())


def test_web_shows_recovery_controls_and_routes_work():
    pp = Flaky("timeout"); client, s, c, o, pid = approved(pp)
    execute(c, s, "https://api-m.sandbox.paypal.com", pid, orders=o)
    page = client.get(f"/proposals/{pid}", auth=AUTH).text
    assert "Result unknown" in page and "Check status with PayPal" in page and "Close as failed" in page
    assert "Retry (same request id)" not in page                       # only offered once reconcile says not applied
    r = client.post(f"/proposals/{pid}/reconcile", data={"token": token(client, pid, "reconcile")}, auth=AUTH, follow_redirects=False)
    assert r.status_code == 303
    assert "Retry (same request id)" in client.get(f"/proposals/{pid}", auth=AUTH).text
    bad = client.post(f"/proposals/{pid}/retry", data={"token": "nope"}, auth=AUTH)
    assert bad.status_code == 403 and len(pp.attempts) == 1
