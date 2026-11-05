"""Executes an APPROVED proposal against PayPal. The only module that writes to PayPal.

Safety rails, all enforced here rather than trusted from earlier steps:
  1. The proposal must be APPROVED (a human did that) and the decision must be executable.
  2. We re-fetch the dispute right before acting. If it changed since the proposal was made
     (update_time differs), or the merchant records / evidence file bytes no longer match the snapshot the
     human approved, we refuse: the human approved a stale picture.
  3. The action's link must still be offered by PayPal now, and its href must stay on the sandbox host.
  4. POSTs carry PayPal-Request-Id = dc-<proposal id>, so a retried execution cannot double-submit.
  5. Every attempt and outcome is audited. If PayPal's answer never arrives (timeout, 5xx) the proposal becomes
     UNCERTAIN, never FAILED: the request may have landed. reconcile() asks PayPal what happened; a human then
     retries (same Request-Id, so PayPal can deduplicate) or closes it.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx

from .config import is_sandbox_url
from .docs import DocError, evidence_fingerprint, load_document
from .models import parse_dispute
from urllib.parse import urlsplit
from .paypal import PayPalClient, PayPalUnreachable
from .store import Store

ACTOR = "executor"
DEFAULT_CONTEST_NOTE = "Merchant evidence attached: delivery/access records for this order."
DEFAULT_ESCALATE_NOTE = "Merchant disputes this claim and escalates for review; supporting evidence to follow."
DEFAULT_ACCEPT_NOTE = "Accepting the claim; refund will be issued."


def build_request(rel: str, decision: dict, draft: str | None, docs_dir: Path | None = None) -> dict:
    """Request kwargs for the PayPal client. Payload shapes marked [verified] were observed live in the sandbox."""
    if rel == "provide_evidence":  # [verified] multipart with a JSON `input` part
        types = decision.get("evidence_plan") or ["OTHER"]
        body = {"evidences": [{"evidence_type": t, "notes": draft or DEFAULT_CONTEST_NOTE} for t in types]}
        parts = [("input", (None, json.dumps(body), "application/json"))]
        for name in decision.get("documents") or []:  # raises DocError if a listed file is missing/unsafe
            parts.append(("evidence-file", load_document(docs_dir or Path("data/docs"), name)))
        return {"files": parts}
    if rel == "escalate":  # [verified] JSON {"note": ...}; moves an inquiry to a claim/chargeback
        return {"json": {"note": draft or DEFAULT_ESCALATE_NOTE}}
    if rel == "accept_claim":  # confirmed live (HTTP 200, dispute resolved); REFUND is the only accept type used
        return {"json": {"note": draft or DEFAULT_ACCEPT_NOTE, "accept_claim_type": "REFUND"}}
    raise ValueError(f"no executor for action link '{rel}'")


def _prior(p: dict) -> dict:
    r = p.get("result")
    return r if isinstance(r, dict) else {}


def execute(c: PayPalClient, store: Store, base_url: str, proposal_id: int, docs_dir: Path | None = None,
            orders=None, retry: bool = False) -> dict:
    """Send an APPROVED proposal. With retry=True it may also resend an UNCERTAIN one, but only after reconcile()
    found the action not applied; the same PayPal-Request-Id is reused so PayPal can deduplicate."""
    p = store.get_proposal(proposal_id)
    if p is None:
        raise KeyError(proposal_id)
    uncertain_retry = p["state"] == "UNCERTAIN" and retry
    if p["state"] != "APPROVED" and not uncertain_retry:
        raise PermissionError(f"proposal {proposal_id} is {p['state']}, not APPROVED")
    prior = _prior(p)
    if uncertain_retry and not prior.get("reconciled_not_applied"):
        raise PermissionError("check the status with PayPal (reconcile) before retrying")
    dec, did = p["decision"], p["dispute_id"]
    rel = dec.get("link_rel")

    def fail(reason: str, extra: dict | None = None) -> dict:
        res = {"ok": False, "reason": reason, **(extra or {})}
        if uncertain_retry and "status" not in res:
            # Refused before sending: the earlier attempt may still have landed, so stay UNCERTAIN (a human can close it).
            store.transition(proposal_id, "UNCERTAIN", result={**prior, "last_refusal": reason})
        else:
            store.transition(proposal_id, "FAILED", result=res)
        store.audit(ACTOR, "execute.refused" if "status" not in res else "execute.failed",
                    {"proposal_id": proposal_id, "dispute_id": did, **res})
        return res

    if not dec.get("executable") or not rel:
        return fail("decision is not executable")
    r = c.get(f"/v1/customer/disputes/{did}")
    store.audit(ACTOR, "paypal.get", {"dispute_id": did, "status": r.status, "debug_id": r.debug_id})
    if not r.ok or not isinstance(r.body, dict):
        return fail("could not re-fetch dispute", {"http_status": r.status})
    want_update, _, want_evidence = p["source_update"].partition("#")  # key is "<update_time>#<evidence hash>"
    if uncertain_retry:
        want_update = prior.get("update_time") or want_update   # the version the human saw when reconciling
    if (r.body.get("update_time") or "") != want_update:
        store.upsert_dispute(r.body)
        return fail("stale: dispute changed since the proposal was made; re-review the new version")
    if orders is None:
        return fail("cannot verify the evidence snapshot: no order store given")
    now_ev = orders.evidence_for(parse_dispute(r.body).seller_transaction_id)
    if evidence_fingerprint(now_ev, docs_dir) != want_evidence:
        return fail("stale: merchant records or evidence files changed since the proposal was made; re-review")
    link = parse_dispute(r.body).link(rel)
    if link is None:
        return fail(f"PayPal no longer offers '{rel}'" + (" (the earlier attempt may have been applied late)" if uncertain_retry else ""))
    method, href = link
    if not is_sandbox_url(href) or not is_sandbox_url(base_url, base_only=True):
        return fail("link points outside the sandbox host")
    try:
        kw = build_request(rel, dec, p["draft"], docs_dir)
    except (ValueError, DocError) as e:
        return fail(str(e))
    request_id = f"dc-{proposal_id}"
    store.audit(ACTOR, "execute.start", {"proposal_id": proposal_id, "dispute_id": did, "link": rel,
                                         "request_id": request_id, "retry": uncertain_retry})
    u = urlsplit(href)

    def uncertain(reason: str, extra: dict) -> dict:
        out = {"ok": False, "uncertain": True, "reason": reason, "request_id": request_id, **extra}
        store.transition(proposal_id, "UNCERTAIN", result=out)
        store.audit(ACTOR, "execute.uncertain", {"proposal_id": proposal_id, "dispute_id": did, **out})
        return out

    try:
        res = c.request(method, u.path + (f"?{u.query}" if u.query else ""), request_id=request_id, **kw)
    except PayPalUnreachable as ex:
        # No connection was ever made, so nothing reached PayPal: the state is unchanged and a retry is safe.
        store.audit(ACTOR, "execute.not_sent", {"proposal_id": proposal_id, "dispute_id": did, "error": str(ex)[:300]})
        return {"ok": False, "sent": False, "reason": "could not reach PayPal; nothing was sent. Try again."}
    except httpx.HTTPError as ex:
        return uncertain(f"no answer from PayPal ({type(ex).__name__}); the request may have been processed", {})
    if res.status >= 500:
        return uncertain(f"PayPal returned HTTP {res.status}; the request may have been processed",
                         {"status": res.status, "debug_id": res.debug_id})
    out = {"ok": res.ok, "status": res.status, "debug_id": res.debug_id, "body": res.body}
    if not res.ok:
        return fail(f"PayPal returned HTTP {res.status}", {"status": res.status, "debug_id": res.debug_id, "body": res.body})
    store.transition(proposal_id, "EXECUTED", result=out)
    store.audit(ACTOR, "execute.done", {"proposal_id": proposal_id, "dispute_id": did, "link": rel,
                                         "status": res.status, "debug_id": res.debug_id, "retry": uncertain_retry})
    return out


def _applied(rel: str | None, dec: dict, d) -> bool | None:
    """Does PayPal's current dispute show the action already took effect? None = cannot tell."""
    if rel == "accept_claim":
        return bool(d.outcome_code) or str(d.status or "").upper() == "RESOLVED"
    if rel == "escalate":
        before = dec.get("stage_at_proposal")
        return None if before is None else (d.stage != before)
    if rel == "provide_evidence":
        before = dec.get("submitted_at_proposal")
        return None if before is None else (len(d.submitted) > before)
    return None


def reconcile(c: PayPalClient, store: Store, proposal_id: int) -> dict:
    """Ask PayPal what actually happened to an APPROVED/UNCERTAIN proposal. Read-only toward PayPal.
    Applied -> EXECUTED. Not applied -> an UNCERTAIN proposal records the dispute version seen, which unlocks retry."""
    p = store.get_proposal(proposal_id)
    if p is None:
        raise KeyError(proposal_id)
    if p["state"] not in ("APPROVED", "UNCERTAIN"):
        raise PermissionError(f"proposal {proposal_id} is {p['state']}; nothing to reconcile")
    dec, did, rel = p["decision"], p["dispute_id"], p["decision"].get("link_rel")
    r = c.get(f"/v1/customer/disputes/{did}")
    store.audit(ACTOR, "paypal.get", {"dispute_id": did, "status": r.status, "debug_id": r.debug_id})
    if not r.ok or not isinstance(r.body, dict):
        return {"ok": False, "outcome": "unknown", "reason": f"could not read the dispute (HTTP {r.status})"}
    store.upsert_dispute(r.body)
    d = parse_dispute(r.body)
    verdict = _applied(rel, dec, d)
    if verdict:
        out = {"ok": True, "reconciled": True, "note": "PayPal shows this action was applied.",
               "update_time": r.body.get("update_time"), "status": d.status, "stage": d.stage}
        store.transition(proposal_id, "EXECUTED", result=out)
        store.audit(ACTOR, "execute.reconciled", {"proposal_id": proposal_id, "dispute_id": did, "link": rel, "applied": True})
        return {"ok": True, "outcome": "applied", **out}
    outcome = "not_applied" if verdict is False else "unknown"
    if p["state"] == "UNCERTAIN":
        store.transition(proposal_id, "UNCERTAIN", result={**_prior(p), "reconciled_not_applied": verdict is False,
                                                          "update_time": r.body.get("update_time")})
    store.audit(ACTOR, "execute.reconciled", {"proposal_id": proposal_id, "dispute_id": did, "link": rel, "applied": verdict})
    return {"ok": True, "outcome": outcome,
            "reason": ("PayPal does not show the action yet. Sandbox status can lag by minutes; check again or retry."
                       if verdict is False else "Cannot tell from PayPal's response whether the action was applied.")}


def abandon(store: Store, proposal_id: int, by: str) -> None:
    """A human closes a stuck APPROVED/UNCERTAIN proposal as failed (nothing further is sent)."""
    p = store.get_proposal(proposal_id)
    if p is None:
        raise KeyError(proposal_id)
    store.transition(proposal_id, "FAILED", by=by, result={"ok": False, "reason": f"closed by {by} without a confirmed result",
                                                           "previous": _prior(p)})
    store.audit(by, "execute.abandoned", {"proposal_id": proposal_id})
