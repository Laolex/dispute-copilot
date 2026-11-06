import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.evidence import evidence_report
from app.models import parse_dispute
from app.orders import OrderStore
from app.rules import Action, MerchantEvidence, Shipment, decide

FX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


def physical(reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED"):
    b = json.loads((FX / "inquiry_escalated.json").read_text())["body"]
    b["reason"] = reason
    b["links"] = [{"rel": r, "method": "POST", "href": f"h/{r}"} for r in ("provide_evidence", "accept_claim")]
    b["seller_response_due_date"] = "2099-01-01T00:00:00Z"
    return b


GOOD = Shipment("UPS", "1Z999", "DELIVERED", "2026-10-20T10:00:00Z", "UPS tracking API", True)


def phys_dispute():
    b = physical()
    for t in b["disputed_transactions"]:
        for it in t.get("items", []):
            it["item_type"] = "PHYSICAL"
    d = parse_dispute(b)
    assert not d.is_digital
    return d


def test_tracking_number_alone_is_not_proof():
    d = phys_dispute()
    assert decide(d, MerchantEvidence(tracking_numbers=("1Z",)), now=NOW).action is Action.ACCEPT


def test_verified_shipment_is_proof_and_reason_names_carrier_and_source():
    dec = decide(phys_dispute(), MerchantEvidence(shipments=(GOOD,)), now=NOW)
    assert dec.action is Action.CONTEST and dec.rule_id == "R10_INR_PROVEN"
    assert "UPS 1Z999" in dec.reasons[-1] and "UPS tracking API" in dec.reasons[-1]


@pytest.mark.parametrize("ship", [
    Shipment("UPS", "1Z", "DELIVERED", "2026-10-20T10:00:00Z", "merchant note", False),   # not verified against the carrier
    Shipment("UPS", "1Z", "IN_TRANSIT", None, "UPS tracking API", True),                   # not delivered
    Shipment("UPS", "1Z", "DELIVERED", None, "UPS tracking API", True),                    # no delivery time
])
def test_unverified_shipment_goes_to_a_human_not_to_accept_or_contest(ship):
    dec = decide(phys_dispute(), MerchantEvidence(shipments=(ship,)), now=NOW)
    assert dec.action is Action.REVIEW and dec.rule_id == "R13_INR_UNVERIFIED_DELIVERY"


def test_report_labels_every_status(tmp_path):
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4 a")
    ev = MerchantEvidence(shipments=(GOOD, Shipment("DHL", "9", "IN_TRANSIT", None, "", False)),
                          documents=("a.pdf", "ghost.pdf"))
    items = {i["label"]: i for i in evidence_report(phys_dispute(), ev, tmp_path)}
    assert items["Shipment UPS 1Z999"]["status"] == "verified"
    assert items["Shipment DHL 9"]["status"] == "unverified" and "not verified" in items["Shipment DHL 9"]["detail"]
    assert items["a.pdf"]["status"] == "verified" and "sha256" in items["a.pdf"]["detail"]
    assert items["ghost.pdf"]["status"] == "missing"


def test_delivery_after_dispute_opened_is_flagged_as_conflict():
    ship = Shipment("UPS", "1Z", "DELIVERED", "2026-11-05T00:00:00Z", "UPS", True)
    items = evidence_report(phys_dispute(), MerchantEvidence(shipments=(ship,)), None, opened_at="2026-11-01T00:00:00Z")
    assert items[0]["status"] == "conflict" and "after the dispute was opened" in items[0]["detail"]


def test_missing_everything_is_reported_as_missing():
    items = evidence_report(phys_dispute(), MerchantEvidence(), None)
    assert [i["status"] for i in items] == ["missing", "missing"]


def test_order_store_parses_shipments_and_only_literal_true_counts_as_verified():
    o = OrderStore(data={"T": {"shipments": [{"carrier": "UPS", "tracking_number": "1Z", "status": "delivered",
                                              "delivered_at": "2026-10-20T10:00:00Z", "source": "UPS API", "verified": "yes"},
                                             {"carrier": "DHL", "tracking_number": "2", "status": "DELIVERED",
                                              "delivered_at": "2026-10-20T10:00:00Z", "verified": True}, "junk"]}})
    ev = o.evidence_for("T")
    assert len(ev.shipments) == 2 and not ev.shipments[0].proven and ev.shipments[1].proven


def test_proposal_stores_the_evidence_report_and_page_shows_it():
    from tests.test_web_exec import AUTH, make
    from app.ingest import poll
    client, s, c, pp, o = make()
    pid = poll(c, s, o)[0]
    items = s.get_proposal(pid)["decision"]["evidence_items"]
    assert items and items[0]["status"] == "verified"
    page = client.get(f"/proposals/{pid}", auth=AUTH).text
    assert "Evidence provenance" in page and "data-kind='evidence'" in page      # rendered by AG Grid from the API
    rows = client.get(f"/api/proposals/{pid}/evidence", auth=AUTH).json()
    assert any("Digital access" in r["label"] and r["status"] == "verified" for r in rows)
    assert client.get("/api/proposals/999/evidence", auth=AUTH).status_code == 404
    assert client.get(f"/api/proposals/{pid}/evidence").status_code == 401
