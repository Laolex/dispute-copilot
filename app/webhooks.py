"""PayPal webhook receiver. The webhook is only a *trigger*.

  1. The signature is verified with PayPal's verify-webhook-signature API before anything else
     (fail closed: no configured webhook id means the endpoint is disabled; any non-SUCCESS is rejected).
  2. The event body is never trusted for decisions. We take only the dispute id from it and re-fetch the
     dispute over the authenticated API, exactly like polling does.
  3. Event ids are recorded, so retries/replays are acknowledged but not processed twice.
  4. We acknowledge immediately and ingest in the background so PayPal does not time out and retry.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from .ingest import ingest_one, poll
from .orders import OrderStore
from .paypal import PayPalClient, PayPalUnreachable
from .store import Store

MAX_BODY = 256 * 1024
HANDLED = {"CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED", "RISK.DISPUTE.CREATED"}
REQUIRED_HEADERS = ("paypal-auth-algo", "paypal-cert-url", "paypal-transmission-id",
                    "paypal-transmission-sig", "paypal-transmission-time")
ACTOR = "webhook"


def verify_signature(c: PayPalClient, webhook_id: str, headers, raw: bytes) -> bool:
    """Ask PayPal to verify. The raw event bytes are spliced in unchanged so re-serialisation cannot break the signature."""
    head = {"auth_algo": headers["paypal-auth-algo"], "cert_url": headers["paypal-cert-url"],
            "transmission_id": headers["paypal-transmission-id"], "transmission_sig": headers["paypal-transmission-sig"],
            "transmission_time": headers["paypal-transmission-time"], "webhook_id": webhook_id}
    body = json.dumps(head)[:-1].encode() + b', "webhook_event": ' + raw + b"}"
    r = c.post("/v1/notifications/verify-webhook-signature", content=body)
    return r.ok and isinstance(r.body, dict) and r.body.get("verification_status") == "SUCCESS"


def dispute_id_of(event: dict) -> str | None:
    res = event.get("resource") or {}
    did = res.get("dispute_id") or res.get("id")  # shape from PayPal's docs; confirm on the first live delivery (the audit log records the id we found)
    return did if isinstance(did, str) and did.startswith("PP-") else None


def add_webhook(app: FastAPI, client: PayPalClient, store: Store, orders: OrderStore, webhook_id: str | None,
                agent=None, docs_dir: str | Path = "data/docs") -> None:
    def process(dispute_id: str) -> None:
        try:
            ingest_one(client, store, orders, dispute_id, agent=agent, docs_dir=Path(docs_dir))
        except Exception as ex:  # noqa: BLE001 - never crash the worker; polling is the safety net
            store.audit(ACTOR, "webhook.ingest_error", {"dispute_id": dispute_id, "error": type(ex).__name__})

    def process_all() -> None:
        try:
            poll(client, store, orders, agent=agent, docs_dir=Path(docs_dir))
        except Exception as ex:  # noqa: BLE001
            store.audit(ACTOR, "webhook.ingest_error", {"dispute_id": None, "error": type(ex).__name__})

    @app.post("/webhooks/paypal")
    async def receive(request: Request, bg: BackgroundTasks):
        if not webhook_id:
            return JSONResponse({"error": "webhook receiver not configured"}, status_code=503)
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return JSONResponse({"error": "too large"}, status_code=413)
        h = {k: request.headers.get(k) for k in REQUIRED_HEADERS}
        if not all(h.values()):
            store.audit(ACTOR, "webhook.rejected", {"reason": "missing signature headers"})
            return JSONResponse({"error": "missing headers"}, status_code=400)
        try:
            event = json.loads(raw)
            assert isinstance(event, dict)
        except (ValueError, AssertionError):
            store.audit(ACTOR, "webhook.rejected", {"reason": "malformed body"})
            return JSONResponse({"error": "malformed"}, status_code=400)
        try:
            ok = verify_signature(client, webhook_id, h, raw)
        except PayPalUnreachable:
            return JSONResponse({"error": "cannot verify right now"}, status_code=503)  # PayPal will retry
        if not ok:
            store.audit(ACTOR, "webhook.rejected", {"reason": "signature verification failed",
                                                     "transmission_id": h["paypal-transmission-id"]})
            return JSONResponse({"error": "bad signature"}, status_code=400)
        etype, eid = str(event.get("event_type", "")), str(event.get("id", h["paypal-transmission-id"]))
        if not store.claim_webhook_event(eid, etype):
            return {"ok": True, "duplicate": True}
        did = dispute_id_of(event)
        store.audit(ACTOR, "webhook.accepted", {"event_id": eid, "event_type": etype, "dispute_id": did})
        if etype in HANDLED and did:
            bg.add_task(process, did)
        elif etype in HANDLED:
            # A verified dispute event whose payload has no dispute id we recognise (the shape is only seen in docs,
            # not yet in a live delivery): fall back to a full poll rather than dropping the event.
            store.audit(ACTOR, "webhook.no_dispute_id", {"event_id": eid, "event_type": etype})
            bg.add_task(process_all)
        return {"ok": True}
