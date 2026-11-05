import json
from pathlib import Path

import httpx
import pytest

from app.config import Settings
from app.ingest import ingest_one, poll
from app.orders import OrderStore
from app.paypal import PayPalClient
from app.store import InvalidTransition, Store

FX = Path(__file__).parent / "fixtures"
BASE = "https://api-m.sandbox.paypal.com"
TOKEN = {"access_token": "T", "expires_in": 3000, "scope": "x"}
ORDERS = OrderStore(data={"76X00088A7532581F": {"digital_access_log": True}})


def body(**kw):
    b = json.loads((FX / "inquiry_escalated.json").read_text())["body"]
    b["links"] = [{"rel": "self", "method": "GET", "href": "h"},
                  {"rel": "provide_evidence", "method": "POST", "href": f"{BASE}/v1/customer/disputes/PP-R-TSL-10195822/provide-evidence"},
                  {"rel": "accept_claim", "method": "POST", "href": f"{BASE}/v1/customer/disputes/PP-R-TSL-10195822/accept-claim"}]
    b["seller_response_due_date"] = "2099-01-01T00:00:00Z"
    b.update(kw)
    return b


class PP:
    def __init__(self, b):
        self.b, self.calls, self.params = b, [], []

    def __call__(self, req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        self.calls.append((req.method, req.url.path)); self.params.append(dict(req.url.params))
        if req.url.path == "/v1/customer/disputes":
            return httpx.Response(200, json={"items": [{"dispute_id": self.b["dispute_id"]}]})
        return httpx.Response(200, json=self.b)


def client(h):
    return PayPalClient(Settings("i", "s", BASE), transport=httpx.MockTransport(h), backoff=0)


def test_audit_chain_verifies_and_detects_tampering():
    s = Store()
    for i in range(3):
        s.audit("t", "e", {"i": i})
    assert s.verify_audit() == (True, None)
    s._db.execute("UPDATE audit SET payload='{\"i\":99}' WHERE seq=2"); s._db.commit()
    assert s.verify_audit() == (False, 2)


def test_audit_detects_deleted_row():
    s = Store()
    for i in range(3):
        s.audit("t", "e", {"i": i})
    s._db.execute("DELETE FROM audit WHERE seq=2"); s._db.commit()
    assert s.verify_audit()[0] is False


def test_poll_creates_one_pending_proposal_and_is_idempotent():
    s, h = Store(), PP(body())
    c = client(h)
    ids = poll(c, s, ORDERS)
    assert len(ids) == 1
    p = s.get_proposal(ids[0])
    assert p["state"] == "PENDING" and p["decision"]["action"] == "contest" and p["decision"]["executable"]
    assert poll(c, s, ORDERS) == []  # unchanged update_time -> nothing new
    assert len(s.list_proposals()) == 1
    assert all(m == "GET" for m, _ in h.calls)  # ingester never writes to PayPal
    assert s.verify_audit()[0]


def test_poll_sends_time_window_and_page_size():
    h = PP(body()); poll(client(h), Store(), ORDERS, days=500)
    q = h.params[0]
    assert q["update_time_after"].endswith("Z") and q["page_size"] == "50"


def test_no_proposal_when_seller_has_no_action():
    b = body(); b["links"] = [{"rel": "self", "method": "GET", "href": "h"}]
    s = Store()
    assert ingest_one(client(PP(b)), s, ORDERS, b["dispute_id"]) is None
    assert s.list_proposals() == [] and s.get_dispute(b["dispute_id"]) is not None


def test_new_version_supersedes_old_pending():
    s = Store()
    a = ingest_one(client(PP(body(update_time="2026-11-02T10:00:00Z"))), s, ORDERS, "PP-R-TSL-10195822")
    b = ingest_one(client(PP(body(update_time="2026-11-02T11:00:00Z"))), s, ORDERS, "PP-R-TSL-10195822")
    assert s.get_proposal(a)["state"] == "SUPERSEDED" and s.get_proposal(b)["state"] == "PENDING"


def test_state_machine_blocks_skipping_approval():
    s = Store(); pid = s.create_proposal("D", "u", {"action": "contest"})
    with pytest.raises(InvalidTransition):
        s.transition(pid, "EXECUTED")          # cannot execute without approval
    s.transition(pid, "APPROVED", by="laolex")
    s.transition(pid, "EXECUTED", result={"status": 200})
    with pytest.raises(InvalidTransition):
        s.transition(pid, "REJECTED")


def test_unknown_order_means_accept():
    s = Store()
    pid = ingest_one(client(PP(body())), s, OrderStore(data={}), "PP-R-TSL-10195822")
    assert s.get_proposal(pid)["decision"]["action"] == "accept"


def test_order_file_edits_apply_without_restart(tmp_path):
    f = tmp_path / "orders.json"
    f.write_text("{}")
    o = OrderStore(f)
    assert o.evidence_for("T1").documents == ()
    f.write_text('{"T1": {"documents": ["a.pdf"]}}')
    import os; os.utime(f, ns=(1, 2_000_000_000_000_000_000))  # force a distinct mtime
    assert o.evidence_for("T1").documents == ("a.pdf",)


def test_bad_order_file_fails_fast_at_startup_but_not_at_runtime(tmp_path):
    f = tmp_path / "orders.json"
    f.write_text('{"a": {}\n "b": {}}')
    with pytest.raises(ValueError, match="not valid JSON"):
        OrderStore(f)
    f.write_text('{"T1": {"documents": ["a.pdf"]}}')
    o = OrderStore(f)
    f.write_text("{ broken")
    import os; os.utime(f, ns=(1, 3_000_000_000_000_000_000))
    assert o.evidence_for("T1").documents == ("a.pdf",) and "not valid JSON" in o.last_error


def test_editing_merchant_records_replaces_the_pending_proposal(tmp_path):
    f = tmp_path / "orders.json"; f.write_text("{}")
    o = OrderStore(f); s = Store(); c = client(PP(body()))
    first = ingest_one(c, s, o, "PP-R-TSL-10195822")
    assert s.get_proposal(first)["decision"]["action"] == "accept"
    assert ingest_one(c, s, o, "PP-R-TSL-10195822") is None         # nothing changed: no duplicate
    f.write_text('{"76X00088A7532581F": {"digital_access_log": true}}')
    import os; os.utime(f, ns=(1, 4_000_000_000_000_000_000))
    second = ingest_one(c, s, o, "PP-R-TSL-10195822")                 # same dispute version, new evidence
    assert s.get_proposal(first)["state"] == "SUPERSEDED"
    assert s.get_proposal(second)["decision"]["action"] == "contest" and s.get_proposal(second)["state"] == "PENDING"


def test_outdated_pending_proposal_is_retired_when_paypal_stops_offering_the_action():
    s = Store()
    pid = ingest_one(client(PP(body())), s, ORDERS, "PP-R-TSL-10195822")
    assert s.get_proposal(pid)["state"] == "PENDING"
    moved = body(update_time="2026-11-02T13:00:00Z")
    moved["links"] = [{"rel": "self", "method": "GET", "href": "h"}]          # PayPal now offers nothing
    assert ingest_one(client(PP(moved)), s, ORDERS, "PP-R-TSL-10195822") is None
    assert s.get_proposal(pid)["state"] == "SUPERSEDED" and s.list_proposals("PENDING") == []


def test_non_executable_contest_creates_no_proposal():
    b = body(); b["links"] = [{"rel": "self", "method": "GET", "href": "h"},
                              {"rel": "accept_claim", "method": "POST", "href": f"{BASE}/a"}]  # contest decided, no evidence/escalate link
    s = Store()
    assert ingest_one(client(PP(b)), s, ORDERS, "PP-R-TSL-10195822") is None and s.list_proposals() == []


def test_ingest_attaches_agent_advice_and_audits_it():
    from app.agent import build_agent
    s = Store()
    pid = ingest_one(client(PP(body())), s, ORDERS, "PP-R-TSL-10195822", agent=build_agent(None))
    p = s.get_proposal(pid)
    assert p["draft"] and p["decision"]["analysis"]["claim_type"] == "not_received" and p["decision"]["action"] == "contest"
    ev = [r for r in s.audit_rows() if r["event"] == "agent.advice"]
    assert ev and json.loads(ev[0]["payload"])["fallback"] is True and s.verify_audit()[0]


def test_agent_crash_does_not_block_the_proposal():
    s = Store()

    def boom(_state):
        raise RuntimeError("model exploded")

    pid = ingest_one(client(PP(body())), s, ORDERS, "PP-R-TSL-10195822", agent=boom)
    assert pid and s.get_proposal(pid)["state"] == "PENDING"
    assert any(r["event"] == "agent.error" for r in s.audit_rows())
