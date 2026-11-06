"""Polling ingester: PayPal -> store -> proposals. Idempotent and read-only toward PayPal.

For each dispute updated recently: fetch full detail (the list only returns summaries), store it,
and, if the seller can act, create one PENDING proposal per dispute version (keyed by update_time).
Webhooks will later just call `ingest_one` for the dispute id they carry.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from .docs import missing_documents
from .docs import evidence_fingerprint
from .evidence import evidence_report
from .risk import assess_risk
from .models import parse_dispute
from .orders import OrderStore
from .paypal import PayPalClient
from .rules import Action, Policy, decide
from .store import Store

ACTOR = "ingester"


def _decision_dict(dec, docs_dir=None, dispute=None, ev=None, opened_at=None, risk=None) -> dict:
    d = asdict(dec)
    if risk is not None:
        d["risk"] = risk   # advisory only; never read by the policy or the executor
    if dispute is not None and ev is not None:
        d["evidence_items"] = evidence_report(dispute, ev, docs_dir, opened_at)
    if dispute is not None:   # what the dispute looked like when the human was shown it; used to reconcile after a timeout
        d["stage_at_proposal"] = dispute.stage
        d["submitted_at_proposal"] = len(dispute.submitted)
    if dec.documents and docs_dir is not None:
        d['missing_documents'] = missing_documents(docs_dir, dec.documents)
    d["action"] = dec.action.value
    return d


def ingest_one(c: PayPalClient, store: Store, orders: OrderStore, dispute_id: str, policy: Policy = Policy(),
               agent=None, docs_dir=None) -> int | None:
    """Fetch, store and (maybe) propose. Returns the new proposal id, or None."""
    r = c.get(f"/v1/customer/disputes/{dispute_id}")
    store.audit(ACTOR, "paypal.get", {"dispute_id": dispute_id, "status": r.status, "debug_id": r.debug_id})
    if not r.ok or not isinstance(r.body, dict):
        return None
    changed = store.upsert_dispute(r.body)
    d = parse_dispute(r.body)
    ev = orders.evidence_for(d.seller_transaction_id)
    dec = decide(d, ev, policy)
    if changed:
        store.audit(ACTOR, "dispute.seen", {"dispute_id": d.id, "status": d.status, "stage": d.stage,
                                             "offered": sorted(d.offered), "action": dec.action.value, "rule": dec.rule_id})
    if dec.action is Action.WAIT or not dec.executable:
        # Nothing a human could approve right now: show no proposal, and retire any now-outdated pending one.
        for old in store.supersede_pending(d.id):
            store.audit(ACTOR, "proposal.superseded", {"proposal_id": old, "dispute_id": d.id, "reason": "action no longer available"})
        return None
    # The decision depends on PayPal's data AND on our merchant records, so the proposal key covers both:
    # editing the order store re-evaluates pending disputes and supersedes a now-outdated proposal.
    key = f"{r.body.get('update_time') or ''}#{evidence_fingerprint(ev, docs_dir)}"
    risk = assess_risk(d, r.body, ev, store.other_disputes(d.id))
    pid = store.create_proposal(d.id, key, _decision_dict(dec, docs_dir, d, ev, r.body.get("create_time"), risk))
    if pid:
        if risk["level"] != "low":
            store.audit(ACTOR, "risk.flagged", {"proposal_id": pid, "dispute_id": d.id, "level": risk["level"],
                                                 "signals": [x["id"] for x in risk["signals"] if x["severity"] != "info"]})
        store.audit(ACTOR, "proposal.created", {"proposal_id": pid, "dispute_id": d.id, "action": dec.action.value,
                                                 "rule": dec.rule_id, "executable": dec.executable})
        if agent is not None:
            _advise(store, agent, pid, d, dec, ev)
    return pid


def _advise(store: Store, agent, pid: int, d, dec, ev) -> None:
    """Attach the agent's analysis + draft. Failure never blocks the proposal (the human still decides)."""
    try:
        out = agent({"dispute": d, "decision": dec, "evidence": ev})
        rec = out.get("recommendation")
        rec_dict = None
        if rec is not None:
            from .agent_rule import AIRecommendation
            if isinstance(rec, AIRecommendation):
                rec_dict = {
                    "dispute_id": rec.dispute_id,
                    "recommended_action": rec.recommended_action.value,
                    "recommended_rule_id": rec.recommended_rule_id,
                    "reasons": rec.reasons,
                    "evidence_plan": rec.evidence_plan,
                    "confidence": rec.confidence,
                    "model": rec.model,
                }
            else:
                rec_dict = rec
        store.set_advice(pid, out["analysis"], out["draft"], rec_dict)
        store.audit("agent", "agent.advice", {"proposal_id": pid, "model": getattr(agent, "model", "?"),
                                              "fallback": out.get("used_fallback", False),
                                              "claim_type": out["analysis"]["claim_type"],
                                              "consistent": out["analysis"]["consistent_with_reason"],
                                              "ai_recommendation": rec.recommended_action.value if rec else None})
    except Exception as ex:  # noqa: BLE001
        store.audit("agent", "agent.error", {"proposal_id": pid, "error": type(ex).__name__})


def poll(c: PayPalClient, store: Store, orders: OrderStore, days: int = 30, page_size: int = 50,
         policy: Policy = Policy(), agent=None, docs_dir=None) -> list[int]:
    since = (datetime.now(timezone.utc) - timedelta(days=min(days, 180))).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = c.get("/v1/customer/disputes", params={"update_time_after": since, "page_size": page_size})
    store.audit(ACTOR, "paypal.list", {"status": r.status, "debug_id": r.debug_id})
    if not r.ok or not isinstance(r.body, dict):
        return []
    out = []
    for item in r.body.get("items", []):
        pid = ingest_one(c, store, orders, item["dispute_id"], policy, agent, docs_dir)
        if pid:
            out.append(pid)
    return out
