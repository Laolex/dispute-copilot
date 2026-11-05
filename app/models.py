"""Typed view of a PayPal dispute, parsed defensively from the raw API body.

Only fields we have seen in real sandbox responses are used. Unknown/missing fields degrade to
None/empty, never raise, so a PayPal schema addition cannot crash ingestion.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone


def norm_rel(rel: str) -> str:
    """PayPal names links with underscores (accept_claim) and paths with hyphens; normalize to underscores."""
    return str(rel).replace("-", "_")


def _money(d: dict | None) -> Decimal | None:
    try:
        return Decimal(str(d["value"])) if d and d.get("value") is not None else None
    except (InvalidOperation, KeyError, TypeError):
        return None


def _ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


@dataclass(frozen=True)
class EvidenceItem:
    type: str
    source: str
    notes: str = ""
    has_tracking: bool = False
    has_documents: bool = False


@dataclass(frozen=True)
class Dispute:
    id: str
    reason: str
    status: str
    state: str | None
    stage: str | None
    amount: Decimal | None
    currency: str | None
    held_amount: Decimal | None
    seller_response_due: datetime | None
    item_types: tuple[str, ...]
    seller_protection: str | None  # e.g. EXPANDED_SELLER_PROTECTION, or None when not eligible
    buyer_claim: str
    seller_transaction_id: str | None
    submitted: tuple[EvidenceItem, ...] = ()
    requested_types: tuple[str, ...] = ()
    offered: frozenset[str] = field(default_factory=frozenset)  # normalized rels PayPal offers right now
    links: tuple[tuple[str, str, str], ...] = ()  # (normalized rel, method, href)
    outcome_code: str | None = None      # e.g. RESOLVED_SELLER_FAVOUR (seen live once resolved)
    outcome_reason: str | None = None
    released_amount: Decimal | None = None  # HOLD_RELEASED credit back to the seller

    def link(self, rel: str) -> tuple[str, str] | None:
        for r, method, href in self.links:
            if r == norm_rel(rel):
                return method, href
        return None

    @property
    def is_digital(self) -> bool:
        return bool(self.item_types) and all(t == "DIGITAL_DOWNLOAD" for t in self.item_types)

    @property
    def missing_requested(self) -> tuple[str, ...]:
        have = {e.type for e in self.submitted}
        return tuple(t for t in self.requested_types if t not in have)


def parse_dispute(body: dict) -> Dispute:
    tx = (body.get("disputed_transactions") or [{}])[0]
    amount = body.get("dispute_amount") or tx.get("gross_amount")
    held = next((_money(f.get("amount")) for f in body.get("fund_movements", [])
                 if f.get("reason") == "HOLD_PLACED"), None)
    sp = tx.get("seller_protection_type") if tx.get("seller_protection_eligible") else None
    claim = " | ".join(m.get("content", "") for m in body.get("messages", []) if m.get("posted_by") == "BUYER")
    if not claim:
        claim = " | ".join(e.get("notes", "") for e in body.get("evidences", [])
                           if e.get("source") == "SUBMITTED_BY_BUYER" and e.get("notes"))
    submitted, requested = [], []
    for e in body.get("evidences", []):
        if e.get("source") == "SUBMITTED_BY_SELLER":
            info = e.get("evidence_info") or {}
            submitted.append(EvidenceItem(e.get("evidence_type", ""), e["source"], e.get("notes") or "",
                                          bool(info.get("tracking_info")), bool(e.get("documents"))))
        elif e.get("source") == "REQUESTED_FROM_SELLER":
            requested.append(e.get("evidence_type", ""))
    links = tuple((norm_rel(l.get("rel", "")), l.get("method", "GET"), l.get("href", ""))
                  for l in body.get("links", []))
    outcome = body.get("dispute_outcome") or {}
    released = next((_money(f.get("amount")) for f in body.get("fund_movements", [])
                     if f.get("reason") == "HOLD_RELEASED"), None)
    return Dispute(
        outcome_code=outcome.get("outcome_code"), outcome_reason=outcome.get("outcome_reason"),
        released_amount=released,
        id=body["dispute_id"], reason=body.get("reason", ""), status=body.get("status", ""),
        state=body.get("dispute_state"), stage=body.get("dispute_life_cycle_stage"),
        amount=_money(amount), currency=(amount or {}).get("currency_code"), held_amount=held,
        seller_response_due=_ts(body.get("seller_response_due_date")),
        item_types=tuple(i.get("item_type", "") for i in tx.get("items", []) if i.get("item_type")),
        seller_protection=sp, buyer_claim=claim, seller_transaction_id=tx.get("seller_transaction_id"),
        submitted=tuple(submitted), requested_types=tuple(requested),
        offered=frozenset(r for r, _, _ in links if r != "self"), links=links,
    )
