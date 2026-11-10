import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.executor import execute
from app.ingest import poll
from app.orders import OrderStore
from app.paypal import PayPalClient
from app.store import Store
from app.web import create_app

FX = Path(__file__).parent / "fixtures"
BASE = "https://api-m.sandbox.paypal.com"
TOKEN = {"access_token": "T", "expires_in": 3000, "scope": "x"}
PW = "s3cret"
AUTH = ("laolex", PW)
ID = "PP-R-TSL-10195822"


def dispute(update="2026-11-02T10:00:00Z", rels=("provide_evidence", "accept_claim"), href_host=BASE):
    b = json.loads((FX / "inquiry_escalated.json").read_text())["body"]
    b["update_time"] = update
    b["seller_response_due_date"] = "2099-01-01T00:00:00Z"
    b["links"] = [{"rel": "self", "method": "GET", "href": f"{BASE}/x"}] + [
        {"rel": r, "method": "POST", "href": f"{href_host}/v1/customer/disputes/{ID}/{r.replace('_', '-')}"} for r in rels]
    return b


class PP:
    def __init__(self, **kw):
        self.kw, self.posts, self.headers, self.post_status = kw, [], [], 200

    def __call__(self, req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        if req.method == "POST":
            self.posts.append((req.url.path, req.content)); self.headers.append(req.headers)
            return httpx.Response(self.post_status, json={"ok": True}, headers={"paypal-debug-id": "dbg"})
        if req.url.path == "/v1/customer/disputes":
            return httpx.Response(200, json={"items": [{"dispute_id": ID}]})
        return httpx.Response(200, json=dispute(**self.kw))


def make(pp=None, orders=None):
    pp = pp or PP()
    c = PayPalClient(Settings("i", "s", BASE), transport=httpx.MockTransport(pp), backoff=0)
    s = Store()
    o = orders or OrderStore(data={"76X00088A7532581F": {"digital_access_log": True}})
    app = create_app(c, s, o, BASE, PW)
    return TestClient(app), s, c, pp, o


def token(client, pid, action):
    import re
    html = client.get(f"/proposals/{pid}", auth=AUTH).text
    return re.search(rf"/proposals/{pid}/{action}'.*?value='([0-9a-f]+)'", html, re.S).group(1)


def test_requires_password_and_refuses_empty_config():
    client, *_ = make()
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("a", "wrong")).status_code == 401
    assert client.get("/healthz").status_code == 200
    with pytest.raises(RuntimeError):
        create_app(None, Store(), None, BASE, "")


def test_nothing_sent_until_approved_then_executes_once():
    client, s, c, pp, o = make()
    pid = poll(c, s, o)[0]
    assert pp.posts == []                                  # proposal alone sends nothing
    r = client.get(f"/proposals/{pid}", auth=AUTH)
    assert "Approve and contest" in r.text
    r = client.post(f"/proposals/{pid}/approve", data={"token": token(client, pid, "approve")}, auth=AUTH, follow_redirects=False)
    assert r.status_code == 303 and len(pp.posts) == 1
    path, content = pp.posts[0]
    assert path == f"/v1/customer/disputes/{ID}/provide-evidence" and b"PROOF_OF_FULFILLMENT" in content
    assert pp.headers[0]["paypal-request-id"] == f"dc-{pid}"
    assert s.get_proposal(pid)["state"] == "EXECUTED" and s.verify_audit()[0]
    # replaying approve cannot send again
    import hashlib, hmac
    valid = hmac.new(PW.encode(), f"{pid}:approve".encode(), hashlib.sha256).hexdigest()
    r = client.post(f"/proposals/{pid}/approve", data={"token": valid}, auth=AUTH)
    assert r.status_code == 409 and len(pp.posts) == 1


def test_csrf_token_required_and_bound_to_action():
    client, s, c, pp, o = make()
    pid = poll(c, s, o)[0]
    assert client.post(f"/proposals/{pid}/approve", data={"token": "bad"}, auth=AUTH).status_code == 403
    assert client.post(f"/proposals/{pid}/approve", data={"token": token(client, pid, "reject")}, auth=AUTH).status_code == 403
    assert pp.posts == []


def test_reject_sends_nothing():
    client, s, c, pp, o = make()
    pid = poll(c, s, o)[0]
    client.post(f"/proposals/{pid}/reject", data={"token": token(client, pid, "reject")}, auth=AUTH)
    assert s.get_proposal(pid)["state"] == "REJECTED" and pp.posts == []


def test_stale_dispute_is_refused_at_execution():
    pp = PP(); client, s, c, _, o = make(pp)
    pid = poll(c, s, o)[0]
    pp.kw = {"update": "2026-11-02T12:00:00Z"}             # buyer acted after the proposal
    s.transition(pid, "APPROVED", by="x")
    res = execute(c, s, BASE, pid, orders=o)
    assert res["ok"] is False and "stale" in res["reason"] and pp.posts == []
    assert s.get_proposal(pid)["state"] == "FAILED"


def test_link_gone_or_foreign_host_is_refused():
    pp = PP(); client, s, c, _, o = make(pp)
    pid = poll(c, s, o)[0]
    pp.kw = {"rels": ("accept_claim",)}; s.transition(pid, "APPROVED", by="x")
    assert "no longer offers" in execute(c, s, BASE, pid, orders=o)["reason"]
    pp2 = PP(href_host="https://evil.example.com"); client, s, c, _, o = make(pp2)
    pid = poll(c, s, o)[0]; s.transition(pid, "APPROVED", by="x")
    assert "outside the sandbox host" in execute(c, s, BASE, pid, orders=o)["reason"] and pp2.posts == []


def test_execute_requires_approved_state():
    client, s, c, pp, o = make()
    pid = poll(c, s, o)[0]
    with pytest.raises(PermissionError):
        execute(c, s, BASE, pid, orders=o)
    assert pp.posts == []


def test_paypal_error_marks_failed_and_records_body():
    pp = PP(); pp.post_status = 422
    client, s, c, _, o = make(pp)
    pid = poll(c, s, o)[0]; s.transition(pid, "APPROVED", by="x")
    res = execute(c, s, BASE, pid, orders=o)
    p = s.get_proposal(pid)
    assert res["ok"] is False and p["state"] == "FAILED" and p["result"]["status"] == 422


def test_accept_case_posts_accept_claim():
    client, s, c, pp, o = make(orders=OrderStore(data={}))
    pid = poll(c, s, o)[0]
    assert s.get_proposal(pid)["decision"]["action"] == "accept"
    client.post(f"/proposals/{pid}/approve", data={"token": token(client, pid, "approve")}, auth=AUTH)
    assert pp.posts[0][0].endswith("/accept-claim") and b"REFUND" in pp.posts[0][1]


def test_html_is_escaped():
    client, s, c, pp, o = make()
    b = dispute(); b["messages"][0]["content"] = "<script>alert(1)</script>"
    s.upsert_dispute(b)
    pid = s.create_proposal(ID, b["update_time"], {"action": "review", "rule_id": "R", "reasons": ["<b>x</b>"], "executable": False})
    text = client.get(f"/proposals/{pid}", auth=AUTH).text
    assert "<script>alert" not in text and "&lt;script&gt;" in text and "<b>x</b>" not in text


def test_escalate_contest_posts_note_json_and_next_poll_proposes_evidence():
    pp = PP(rels=("accept_claim", "send_message", "escalate", "make_offer"))
    client, s, c, _, o = make(pp)
    pid = poll(c, s, o)[0]
    dec = s.get_proposal(pid)["decision"]
    assert dec["action"] == "contest" and dec["link_rel"] == "escalate"
    client.post(f"/proposals/{pid}/approve", data={"token": token(client, pid, "approve")}, auth=AUTH)
    path, content = pp.posts[0]
    assert path.endswith("/escalate") and json.loads(content) == {"note": "Merchant disputes this claim and escalates for review; supporting evidence to follow."}
    assert s.get_proposal(pid)["state"] == "EXECUTED"
    # PayPal now moves it to chargeback and offers provide_evidence -> a fresh contest proposal appears
    pp.kw = {"update": "2026-11-02T13:00:00Z", "rels": ("accept_claim", "provide_evidence")}
    nxt = poll(c, s, o)[0]
    assert s.get_proposal(nxt)["decision"]["link_rel"] == "provide_evidence"


def test_page_shows_claim_mismatch_banner_and_draft_is_used_as_note():
    from app.agent import build_agent
    pp = PP(rels=("accept_claim", "escalate", "send_message", "make_offer"))
    inner = pp.__call__

    def handler(req):  # a change-of-mind claim filed as "not as described"
        r = inner(req)
        if req.method == "GET" and req.url.path.startswith("/v1/customer/disputes/"):
            b = json.loads(r.content)
            b["reason"] = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
            b["messages"] = [{"posted_by": "BUYER", "content": "dont want ths item anymore"}]
            return httpx.Response(200, json=b)
        return r

    c = PayPalClient(Settings("i", "s", BASE), transport=httpx.MockTransport(handler), backoff=0)
    s = Store()
    orders = OrderStore(data={"76X00088A7532581F": {"documents": ["listing.pdf"]}})
    cl = TestClient(create_app(c, s, orders, BASE, PW, agent=build_agent(None)))
    import hashlib, hmac
    cl.post("/poll", data={"token": hmac.new(PW.encode(), b"x:poll", hashlib.sha256).hexdigest()}, auth=AUTH)
    pid = s.list_proposals()[0]["id"]
    page = cl.get(f"/proposals/{pid}", auth=AUTH).text
    assert "does not match the dispute reason" in page and "change_of_mind" in page and "advisory only" in page
    cl.post(f"/proposals/{pid}/approve", data={"token": token(cl, pid, "approve")}, auth=AUTH)
    sent = [x for x in pp.posts if x[0].endswith("/escalate")]
    assert sent and b"disputes this claim" in sent[0][1]          # the agent's draft became the escalation note


def test_execution_result_is_shown_as_json_not_a_python_dict():
    from app.web import _pretty
    t = _pretty({"ok": True, "status": 200, "body": {"links": [{"rel": "detail"}]}, "debug_id": "x"})
    assert '"ok": true' in t and "True" not in t and "'" not in t and t.startswith("{\n")
    assert "object" in _pretty(object())      # never raises on odd values
