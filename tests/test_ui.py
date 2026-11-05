import json

from fastapi.testclient import TestClient

from app.agent import build_agent
from app.ingest import poll
from tests.test_web_exec import AUTH, PW, ID, PP, make


def seeded():
    client, s, c, pp, o = make()
    pid = poll(c, s, o, agent=build_agent(None))[0]
    return client, s, pid


def test_api_endpoints_require_the_password():
    client, *_ = seeded()
    for path in ("/api/proposals", "/api/audit"):
        assert client.get(path).status_code == 401
        assert client.get(path, auth=("a", "wrong")).status_code == 401


def test_api_proposals_shape_and_values():
    client, s, pid = seeded()
    rows = client.get("/api/proposals", auth=AUTH).json()
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == pid and r["dispute_id"] == ID and r["action"] == "contest" and r["state"] == "PENDING"
    assert r["amount"] == 25.0 and r["currency"] == "USD" and r["executable"] is True
    assert r["link_rel"] == "provide_evidence" and r["rule_id"] == "R10_INR_PROVEN"
    assert r["claim_type"] == "not_received" and r["mismatch"] is False and r["due"].startswith("2099")


def test_api_flags_claim_reason_mismatch():
    client, s, pid = seeded()
    p = s.get_proposal(pid)
    s.set_advice(pid, {"claim_type": "change_of_mind", "consistent_with_reason": False, "summary": "x",
                       "red_flags": [], "source": "heuristic"}, p["draft"])
    assert client.get("/api/proposals", auth=AUTH).json()[0]["mismatch"] is True


def test_api_audit_reports_chain_status_and_detects_tampering():
    client, s, pid = seeded()
    res = client.get("/api/audit", auth=AUTH).json()
    assert res["ok"] is True and res["bad_seq"] is None and res["rows"]
    assert {"seq", "ts", "actor", "event", "payload"} <= set(res["rows"][0])
    s._db.execute("UPDATE audit SET payload='{}' WHERE seq=1"); s._db.commit()
    res = client.get("/api/audit", auth=AUTH).json()
    assert res["ok"] is False and res["bad_seq"] == 1
    assert "AUDIT CHAIN BROKEN" in client.get("/", auth=AUTH).text


def test_pages_are_shells_with_no_inline_script_and_no_dispute_data():
    client, s, pid = seeded()
    s.set_advice(pid, {"claim_type": "x", "consistent_with_reason": True, "summary": "<script>alert(1)</script>",
                       "red_flags": [], "source": "heuristic"}, "<img src=x onerror=alert(1)>")
    for path in ("/", "/audit"):
        text = client.get(path, auth=AUTH).text
        assert "/static/ag-grid-community.min.js" in text and "/static/app.js" in text
        assert "<script>" not in text.replace("<script src=", "")      # only external scripts
        assert ID not in text and "onerror" not in text                 # data arrives via the JSON API only


def test_security_headers_on_pages_and_static():
    client, *_ = seeded()
    for resp in (client.get("/", auth=AUTH), client.get("/healthz"), client.get("/static/app.js")):
        csp = resp.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "default-src 'self'" in csp
        assert resp.headers["x-content-type-options"] == "nosniff"


def test_vendored_assets_are_served_and_intact():
    client, *_ = seeded()
    js = client.get("/static/ag-grid-community.min.js")
    assert js.status_code == 200 and len(js.content) > 500_000 and b"createGrid" in js.content
    app_js = client.get("/static/app.js").text
    assert "innerHTML" not in app_js.replace("overlayNoRowsTemplate", "")   # data is never written as HTML
    assert client.get("/static/AG-GRID-LICENSE.txt").status_code == 200


def test_detail_and_approval_flow_still_work_after_ui_change():
    client, s, pid = seeded()
    page = client.get(f"/proposals/{pid}", auth=AUTH).text
    assert "Approve and contest" in page and "Claim analysis" in page


def test_ai_recommendation_is_shown_escaped_and_labelled_advisory():
    client, s, pid = seeded()
    p = s.get_proposal(pid)
    rec = {"recommended_action": "<b>accept</b>", "recommended_rule_id": "<img src=x onerror=alert(1)>",
           "reasons": ["<script>alert(1)</script>"], "evidence_plan": ["<i>x</i>"], "confidence": "<u>1</u>",
           "model": "groq-<x>"}
    s.set_advice(pid, {"claim_type": "x", "consistent_with_reason": True, "summary": "s", "red_flags": [],
                       "source": "heuristic"}, p["draft"], rec)
    text = client.get(f"/proposals/{pid}", auth=AUTH).text
    assert "AI recommendation" in text and "advisory only" in text
    for raw in ("<script>alert(1)</script>", "<img src=x", "<b>accept</b>", "<u>1</u>", "<i>x</i>"):
        assert raw not in text
    assert s.get_proposal(pid)["decision"]["action"] == "contest"      # the policy decision is untouched


def _with_rec(action):
    client, s, pid = seeded()
    p = s.get_proposal(pid)
    s.set_advice(pid, {"claim_type": "x", "consistent_with_reason": True, "summary": "s", "red_flags": [],
                       "source": "heuristic"}, p["draft"],
                 {"recommended_action": action, "recommended_rule_id": None, "reasons": [], "evidence_plan": [],
                  "confidence": 0.8, "model": "m"})
    return client, pid


def test_disagreement_between_ai_and_policy_is_flagged_everywhere():
    client, pid = _with_rec("accept")                       # seeded policy decision is "contest"
    assert "AI disagrees with policy" in client.get(f"/proposals/{pid}", auth=AUTH).text
    assert client.get("/api/proposals", auth=AUTH).json()[0]["ai_disagrees"] is True


def test_agreement_is_not_flagged():
    client, pid = _with_rec("contest")
    assert "AI disagrees" not in client.get(f"/proposals/{pid}", auth=AUTH).text
    assert client.get("/api/proposals", auth=AUTH).json()[0]["ai_disagrees"] is False
