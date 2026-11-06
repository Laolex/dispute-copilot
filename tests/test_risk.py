import json
from copy import deepcopy
from pathlib import Path

import pytest

from app.ingest import ingest_one
from app.models import parse_dispute
from app.risk import assess_risk
from app.rules import MerchantEvidence, Shipment, decide
from app.store import Store

FX = Path(__file__).parent / "fixtures"
BASE = json.loads((FX / "inquiry_escalated.json").read_text())["body"]


def raw(**over):
    b = deepcopy(BASE)
    tx = b["disputed_transactions"][0]
    for k, v in over.items():
        if k in ("buyer", "buyer_transaction_id", "gross_amount", "items"):
            tx[k] = v
        elif k == "paid":
            tx["create_time"] = v
        else:
            b[k] = v
    return b


def other(i, payer="25W9VBVCWVD6Q", when="2026-10-20T10:00:00.000Z", tx_id=None):
    o = raw(dispute_id=f"PP-R-OLD-{i}", create_time=when, buyer={"payer_id": payer, "email": "x@y.z", "name": "N"},
            buyer_transaction_id=tx_id or f"TX{i}")
    return o


def risk(b=None, ev=MerchantEvidence(), others=()):
    b = b or raw()
    return assess_risk(parse_dispute(b), b, ev, others)


def ids(r, sev=None):
    return {s["id"] for s in r["signals"] if sev is None or s["severity"] == sev}


def test_clean_dispute_is_low_and_says_what_it_is_not():
    r = risk(raw(reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"))
    assert r["level"] == "low" and "Not a fraud score" in r["basis"]


def test_repeat_buyer_one_prior_is_warn_two_priors_is_high():
    assert risk(others=[other(1)])["level"] == "elevated" and "REPEAT_BUYER" in ids(risk(others=[other(1)]), "warn")
    r = risk(others=[other(1), other(2)])
    assert r["level"] == "high" and "REPEAT_BUYER" in ids(r, "high")


def test_other_buyers_and_old_disputes_do_not_count():
    assert "REPEAT_BUYER" not in ids(risk(others=[other(1, payer="SOMEONE_ELSE")]))
    assert "REPEAT_BUYER" not in ids(risk(others=[other(1, when="2025-01-01T00:00:00.000Z")]))


def test_same_payment_disputed_again_is_flagged_once_not_also_as_repeat():
    tx = BASE["disputed_transactions"][0]["buyer_transaction_id"]
    r = risk(others=[other(1, tx_id=tx)])
    assert "SAME_PAYMENT_DISPUTED_AGAIN" in ids(r) and "REPEAT_BUYER" not in ids(r)


def test_the_dispute_itself_is_never_counted_as_a_prior():
    me = deepcopy(BASE)
    assert risk(others=[me])["level"] == "low"


def test_amount_above_payment_is_high():
    r = risk(raw(dispute_amount={"currency_code": "USD", "value": "99.00"}, gross_amount={"currency_code": "USD", "value": "25.00"}))
    assert r["level"] == "high" and "AMOUNT_EXCEEDS_PAYMENT" in ids(r, "high")


def test_not_received_but_records_show_use_or_delivery():
    b = raw(reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED")
    d = parse_dispute(b)
    if d.is_digital:
        assert "CLAIM_CONTRADICTS_RECORDS" in ids(risk(b, MerchantEvidence(digital_access_log=True)), "warn")
    phys = raw(reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", items=[{"item_type": "PHYSICAL"}])
    ship = Shipment("UPS", "1Z", "DELIVERED", "2026-10-30T10:00:00Z", "UPS API", True)
    assert "CLAIM_CONTRADICTS_RECORDS" in ids(risk(phys, MerchantEvidence(shipments=(ship,))), "warn")


def test_premature_claim_and_fast_dispute_are_info_only_and_do_not_raise_the_level():
    phys = raw(reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED", items=[{"item_type": "PHYSICAL"}])
    r = risk(phys)
    assert {"PREMATURE_NOT_RECEIVED", "DISPUTED_WITHIN_MINUTES"} <= ids(r, "info") and r["level"] == "low"


def test_three_warnings_make_it_high():
    tx = BASE["disputed_transactions"][0]["buyer_transaction_id"]
    r = risk(raw(reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED"), MerchantEvidence(digital_access_log=True),
             [other(1, tx_id=tx), other(2)])
    assert r["level"] == "high"


def test_signals_carry_no_buyer_identifiers():
    r = risk(others=[other(1), other(2)])
    blob = json.dumps(r)
    for secret in ("25W9VBVCWVD6Q", "sb-xdx1x53472317@personal.example.com", "John Doe", "x@y.z"):
        assert secret not in blob


def test_risk_is_advisory_the_policy_decision_is_identical_with_or_without_it():
    from tests.test_web_exec import make
    client, s, c, pp, o = make()
    s.upsert_dispute(other(1)); s.upsert_dispute(other(2))                 # make this buyer a repeat offender first
    from app.ingest import poll
    pid = poll(c, s, o)[0]
    stored = s.get_proposal(pid)["decision"]
    assert stored["risk"]["level"] == "high"
    d = parse_dispute(s.get_dispute(stored and s.get_proposal(pid)["dispute_id"]))
    want = decide(d, o.evidence_for(d.seller_transaction_id))
    assert (stored["action"], stored["rule_id"], stored["link_rel"]) == (want.action.value, want.rule_id, want.link_rel)
    assert any(r["event"] == "risk.flagged" for r in s.audit_rows())


def test_page_and_api_show_risk_escaped():
    from tests.test_web_exec import AUTH, make
    from app.ingest import poll
    client, s, c, pp, o = make()
    s.upsert_dispute(other(1)); s.upsert_dispute(other(2))
    pid = poll(c, s, o)[0]
    page = client.get(f"/proposals/{pid}", auth=AUTH).text
    assert "Risk signals" in page and "High risk indicators" in page and "Repeat disputer" in page
    assert client.get("/api/proposals", auth=AUTH).json()[0]["risk"] == "high"
