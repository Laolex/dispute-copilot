"""Deterministic contest-or-accept policy. No LLM, no I/O: same inputs, same decision, every time.

The agent may *draft* text and *gather* evidence, but this module alone decides the proposed action.
Every Decision carries the rule id and plain-language reasons so the approver (and the audit log)
can see exactly why. Nothing here sends anything: a human approves, then the executor follows the
PayPal-returned link named in `Decision.link_rel`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum

from .models import Dispute


class Action(str, Enum):
    CONTEST = "contest"          # provide evidence
    ACCEPT = "accept"            # accept the claim (merchant refunds)
    REVIEW = "review"            # policy cannot decide; human must
    WAIT = "wait"                # nothing for the seller to do right now


@dataclass(frozen=True)
class Shipment:
    """One shipment with where the claim came from. Existence of a tracking number is not proof of delivery."""
    carrier: str = ""
    tracking_number: str = ""
    status: str = "UNKNOWN"                 # DELIVERED, IN_TRANSIT, ...
    delivered_at: str | None = None         # ISO timestamp from the carrier
    source: str = ""                        # where we learned it, e.g. "UPS API", "Shopify fulfilment"
    verified: bool = False                  # True only when the carrier's own record was checked

    @property
    def proven(self) -> bool:
        return self.status.upper() == "DELIVERED" and self.verified and bool(self.delivered_at)


@dataclass(frozen=True)
class MerchantEvidence:
    """What the merchant's own systems can supply for this order (from the order/fulfilment store)."""
    tracking_numbers: tuple[str, ...] = ()
    delivery_confirmed: bool = False        # legacy flag: the merchant's record says delivered
    digital_access_log: bool = False        # download/login records for digital goods
    documents: tuple[str, ...] = ()         # e.g. listing/description, receipts, correspondence
    shipments: tuple[Shipment, ...] = ()    # structured shipments with carrier, delivery time, source, verification

    @property
    def delivery_proven(self) -> bool:
        return bool(self.tracking_numbers and self.delivery_confirmed) or any(x.proven for x in self.shipments)


@dataclass(frozen=True)
class Policy:
    min_contest_amount: Decimal = Decimal("5.00")  # below this, contesting costs more than it saves
    unknown_reason_goes_to_human: bool = True


@dataclass(frozen=True)
class Decision:
    action: Action
    rule_id: str
    reasons: tuple[str, ...]
    link_rel: str | None = None            # PayPal link needed to execute; None for WAIT/REVIEW
    executable: bool = False               # True only if PayPal currently offers link_rel
    evidence_plan: tuple[str, ...] = ()    # evidence types to submit when contesting
    documents: tuple[str, ...] = ()        # document file names to upload with the evidence
    requires_human: bool = True            # invariant: always True. Nothing auto-executes.


def _decision(action: Action, rule: str, reasons: list[str], d: Dispute, link_rel: str | None = None,
              plan: tuple[str, ...] = (), docs: tuple[str, ...] = ()) -> Decision:
    ok = link_rel is not None and link_rel in d.offered
    why = list(reasons)
    if link_rel and not ok:
        why.append(f"PayPal does not currently offer '{link_rel}' on this dispute, so it cannot be executed.")
    return Decision(action, rule, tuple(why), link_rel, ok, plan, docs)


def decide(d: Dispute, ev: MerchantEvidence, policy: Policy = Policy(), now: datetime | None = None) -> Decision:
    now = now or datetime.now(timezone.utc)
    if "provide_evidence" not in d.offered and "accept_claim" not in d.offered and "make_offer" not in d.offered:
        return _decision(Action.WAIT, "R0_NO_SELLER_ACTION",
                         [f"status={d.status}: PayPal offers the seller no response action right now."], d)
    if d.seller_response_due and d.seller_response_due < now:
        return _decision(Action.REVIEW, "R1_PAST_DUE",
                         [f"Response deadline {d.seller_response_due:%Y-%m-%d %H:%M} UTC has passed."], d)
    if d.amount is None:
        return _decision(Action.REVIEW, "R2_NO_AMOUNT", ["Dispute amount missing from PayPal response."], d)

    strong, plan = _evidence_strength(d, ev)
    small = d.amount < policy.min_contest_amount
    why = [f"reason={d.reason}", f"amount={d.amount} {d.currency}"]
    if d.seller_protection:
        why.append(f"seller protection: {d.seller_protection}")

    if d.reason == "MERCHANDISE_OR_SERVICE_NOT_RECEIVED":
        if strong and small:
            return _below_threshold("R12_INR_BELOW_THRESHOLD", why, d, policy)
        if strong:
            return _contest("R10_INR_PROVEN", why + strong, d, plan, ev.documents)
        if ev.shipments and not d.is_digital:
            return _decision(Action.REVIEW, "R13_INR_UNVERIFIED_DELIVERY",
                             why + ["A shipment is on record but its delivery is not verified by the carrier "
                                    "(status, delivery time or verification is missing). A person should check it."], d)
        return _decision(Action.ACCEPT, "R11_INR_NO_PROOF",
                         why + ["No delivery proof (carrier-confirmed tracking or digital access log) on file."],
                         d, "accept_claim")
    if d.reason == "UNAUTHORIZED":
        if d.seller_protection and strong and small:
            return _below_threshold("R22_UNAUTH_BELOW_THRESHOLD", why, d, policy)
        if d.seller_protection and strong:
            return _contest("R20_UNAUTH_PROTECTED", why + strong, d, plan, ev.documents)
        return _decision(Action.ACCEPT, "R21_UNAUTH_UNPROTECTED",
                         why + ["Not seller-protected and/or no delivery proof."], d, "accept_claim")
    if d.reason == "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED":
        if ev.documents and not small:
            return _contest("R30_NAD_DOCUMENTED",
                            why + [f"{len(ev.documents)} supporting document(s) on file."], d, ("OTHER",), ev.documents)
        return _decision(Action.ACCEPT, "R31_NAD_UNDOCUMENTED",
                         why + ["No supporting documents, or amount below contest threshold."], d, "accept_claim")
    if policy.unknown_reason_goes_to_human:
        return _decision(Action.REVIEW, "R90_UNKNOWN_REASON", why + ["No rule covers this reason."], d)
    return _decision(Action.ACCEPT, "R91_DEFAULT_ACCEPT", why, d, "accept_claim")


def _below_threshold(rule: str, why: list[str], d: Dispute, policy: Policy) -> Decision:
    """One threshold, one meaning: below min_contest_amount we accept whatever the evidence, for every reason."""
    return _decision(Action.ACCEPT, rule,
                     why + [f"Evidence exists, but {d.amount} {d.currency} is below the contest threshold "
                            f"({policy.min_contest_amount}); contesting costs more than it saves."], d, "accept_claim")


def _contest(rule: str, why: list[str], d: Dispute, plan: tuple[str, ...], docs: tuple[str, ...] = ()) -> Decision:
    """Contest via the link PayPal actually offers: evidence if open, else escalate (inquiry -> claim)."""
    if "provide_evidence" in d.offered or "escalate" not in d.offered:
        return _decision(Action.CONTEST, rule, why, d, "provide_evidence", plan, docs)
    return _decision(Action.CONTEST, rule,
                     why + ["Inquiry stage: PayPal accepts evidence only after escalation. Escalating first; "
                            "evidence is proposed once PayPal offers it."], d, "escalate", ())


def _evidence_strength(d: Dispute, ev: MerchantEvidence) -> tuple[list[str], tuple[str, ...]]:
    """Return (reasons, evidence types to submit). Empty reasons == no usable proof."""
    reasons: list[str] = []
    plan: list[str] = []
    if d.is_digital:
        if ev.digital_access_log:
            reasons.append("Digital goods with access/download log on file."); plan.append("PROOF_OF_FULFILLMENT")
    else:
        proven = [x for x in ev.shipments if x.proven]
        if proven:
            reasons.append("Verified delivery: " + "; ".join(
                f"{x.carrier or 'carrier'} {x.tracking_number} delivered {x.delivered_at} (source: {x.source or 'unspecified'})"
                for x in proven) + ".")
            plan.append("PROOF_OF_FULFILLMENT")
        elif ev.tracking_numbers and ev.delivery_confirmed:
            reasons.append(f"Carrier-confirmed delivery ({len(ev.tracking_numbers)} tracking number(s)).")
            plan.append("PROOF_OF_FULFILLMENT")
    return reasons, tuple(plan)
