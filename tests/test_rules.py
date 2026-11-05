import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.models import parse_dispute, norm_rel
from app.rules import Action, MerchantEvidence, Policy, decide

FX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def load(name, **patch):
    body = json.loads((FX / name).read_text())["body"]
    body.update(patch)
    return body


def offer(body, *rels, due="2026-11-12T04:59:59.000Z", **fields):
    body.update(fields)
    body["links"] = [{"rel": "self", "method": "GET", "href": "h"}] + [
        {"rel": r, "method": "POST", "href": f"h/{r.replace('_', '-')}"} for r in rels]
    body["seller_response_due_date"] = due
    return body


PROVEN = MerchantEvidence(tracking_numbers=("1Z",), delivery_confirmed=True)
DIGITAL_PROOF = MerchantEvidence(digital_access_log=True)


def test_parse_real_fixture():
    d = parse_dispute(load("inquiry_escalated.json"))
    assert d.id == "PP-R-TSL-10195822" and d.amount == Decimal("25.00") and d.held_amount == Decimal("23.26")
    assert d.is_digital and d.seller_protection == "EXPANDED_SELLER_PROTECTION"
    assert d.buyer_claim == "didnt get the goods"
    assert [e.type for e in d.submitted] == ["PROOF_OF_FULFILLMENT", "OTHER"] and d.submitted[0].has_tracking


def test_parse_tolerates_missing_fields():
    d = parse_dispute({"dispute_id": "X"})
    assert d.amount is None and d.offered == frozenset() and d.item_types == ()


def test_requested_and_missing_types_come_from_paypal():
    body = load("inquiry_escalated.json")
    body["evidences"].append({"evidence_type": "PROOF_OF_REFUND", "source": "REQUESTED_FROM_SELLER"})
    body["evidences"].append({"evidence_type": "OTHER", "source": "REQUESTED_FROM_SELLER"})
    d = parse_dispute(body)
    assert d.missing_requested == ("PROOF_OF_REFUND",)


def test_rel_normalisation():
    assert norm_rel("accept-claim") == norm_rel("accept_claim") == "accept_claim"


def test_wait_when_paypal_offers_nothing():
    d = parse_dispute(load("inquiry_escalated.json"))  # only self link
    r = decide(d, PROVEN, now=NOW)
    assert r.action is Action.WAIT and not r.executable and r.link_rel is None


def test_digital_inr_with_access_log_contests():
    d = parse_dispute(offer(load("inquiry_escalated.json"), "provide_evidence", "accept_claim"))
    r = decide(d, DIGITAL_PROOF, now=NOW)
    assert r.action is Action.CONTEST and r.rule_id == "R10_INR_PROVEN"
    assert r.executable and r.link_rel == "provide_evidence" and r.requires_human


def test_tracking_does_not_prove_digital_delivery():
    d = parse_dispute(offer(load("inquiry_escalated.json"), "provide_evidence", "accept_claim"))
    r = decide(d, PROVEN, now=NOW)
    assert r.action is Action.ACCEPT and r.rule_id == "R11_INR_NO_PROOF"


def test_physical_inr_needs_carrier_confirmed_delivery():
    body = offer(load("inquiry_escalated.json"), "provide_evidence", "accept_claim")
    body["disputed_transactions"][0]["items"][0]["item_type"] = "PHYSICAL"
    d = parse_dispute(body)
    assert decide(d, MerchantEvidence(tracking_numbers=("1Z",)), now=NOW).action is Action.ACCEPT
    assert decide(d, PROVEN, now=NOW).action is Action.CONTEST


def test_unauthorized_needs_protection_and_proof():
    body = offer(load("inquiry_escalated.json"), "provide_evidence", "accept_claim", reason="UNAUTHORIZED")
    d = parse_dispute(body)
    assert decide(d, DIGITAL_PROOF, now=NOW).rule_id == "R20_UNAUTH_PROTECTED"
    body["disputed_transactions"][0]["seller_protection_eligible"] = False
    assert decide(parse_dispute(body), DIGITAL_PROOF, now=NOW).rule_id == "R21_UNAUTH_UNPROTECTED"


def test_not_as_described_needs_documents_and_size():
    body = offer(load("inquiry_escalated.json"), "provide_evidence", "accept_claim",
                 reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED")
    d = parse_dispute(body)
    assert decide(d, MerchantEvidence(documents=("listing.pdf",)), now=NOW).action is Action.CONTEST
    assert decide(d, MerchantEvidence(), now=NOW).action is Action.ACCEPT
    assert decide(d, MerchantEvidence(documents=("a",)), Policy(min_contest_amount=Decimal("50")), NOW).action is Action.ACCEPT


def test_unknown_reason_goes_to_human():
    d = parse_dispute(offer(load("inquiry_escalated.json"), "provide_evidence", reason="CREDIT_NOT_PROCESSED"))
    r = decide(d, PROVEN, now=NOW)
    assert r.action is Action.REVIEW and r.link_rel is None


def test_past_due_is_review_not_action():
    d = parse_dispute(offer(load("inquiry_escalated.json"), "provide_evidence", due="2026-10-01T00:00:00Z"))
    assert decide(d, DIGITAL_PROOF, now=NOW).rule_id == "R1_PAST_DUE"


def test_decision_not_executable_when_link_missing():
    d = parse_dispute(offer(load("inquiry_escalated.json"), "accept_claim"))  # no provide_evidence
    r = decide(d, DIGITAL_PROOF, now=NOW)
    assert r.action is Action.CONTEST and not r.executable
    assert "cannot be executed" in r.reasons[-1]


def test_decide_is_deterministic():
    d = parse_dispute(offer(load("inquiry_escalated.json"), "provide_evidence", "accept_claim"))
    assert decide(d, DIGITAL_PROOF, now=NOW) == decide(d, DIGITAL_PROOF, now=NOW)


def test_contest_at_inquiry_escalates_when_evidence_not_offered():
    # Real shape from PP-R-FVM-10195934: accept_claim, send_message, escalate, make_offer; no provide_evidence.
    body = offer(load("inquiry_escalated.json"), "accept_claim", "send_message", "escalate", "make_offer",
                 reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED")
    r = decide(parse_dispute(body), MerchantEvidence(documents=("listing.pdf",)), now=NOW)
    assert r.action is Action.CONTEST and r.link_rel == "escalate" and r.executable
    assert r.evidence_plan == () and "Escalating first" in r.reasons[-1]


def test_contest_prefers_provide_evidence_when_offered():
    body = offer(load("inquiry_escalated.json"), "accept_claim", "escalate", "provide_evidence",
                 reason="MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED")
    r = decide(parse_dispute(body), MerchantEvidence(documents=("listing.pdf",)), now=NOW)
    assert r.link_rel == "provide_evidence" and r.evidence_plan == ("OTHER",)



@pytest.mark.parametrize("reason,ev,small_rule,big_rule", [
    ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED", DIGITAL_PROOF, "R12_INR_BELOW_THRESHOLD", "R10_INR_PROVEN"),
    ("UNAUTHORIZED", DIGITAL_PROOF, "R22_UNAUTH_BELOW_THRESHOLD", "R20_UNAUTH_PROTECTED"),
    ("MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED", MerchantEvidence(documents=("a.pdf",)), "R31_NAD_UNDOCUMENTED", "R30_NAD_DOCUMENTED"),
])
def test_min_contest_amount_means_the_same_for_every_reason(reason, ev, small_rule, big_rule):
    body = offer(load("inquiry_escalated.json"), "provide_evidence", "accept_claim", reason=reason)
    body["dispute_amount"]["value"] = "3.00"
    small = decide(parse_dispute(body), ev, now=NOW)
    assert small.rule_id == small_rule and small.action is Action.ACCEPT
    body["dispute_amount"]["value"] = "25.00"
    big = decide(parse_dispute(body), ev, now=NOW)
    assert big.rule_id == big_rule and big.action is Action.CONTEST
