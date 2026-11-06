"""Dispute risk signals: advisory indicators of suspicious or abusive dispute activity.

What this is: deterministic checks over three things we actually hold: the dispute itself, the merchant's own
records, and the other disputes this app has already seen. Each signal names what was observed and why it matters.

What this is not: a fraud score or a prediction. We have no outcome data to calibrate one, so nothing here is a
probability. Signals never change the policy's decision, the PayPal action or the evidence plan; they are shown to
the reviewer and in the alert, so a person looks harder before approving. Signals contain no buyer identifiers.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Iterable

INR = "MERCHANDISE_OR_SERVICE_NOT_RECEIVED"
REPEAT_WINDOW_DAYS = 180
FAST_MINUTES = 10
PREMATURE_HOURS = 72
BASIS = ("Heuristic indicators from this dispute, the merchant's records and disputes this app has already seen. "
         "Not a fraud score; it does not change the policy decision.")


def _t(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc) if s else None
    except ValueError:
        return None


def _tx(raw: dict) -> dict:
    return (raw.get("disputed_transactions") or [{}])[0]


def _buyer_key(raw: dict) -> str | None:
    b = _tx(raw).get("buyer") or {}
    k = b.get("payer_id") or b.get("email")
    return str(k).strip().lower() if k else None


def _num(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError):
        return None


def assess_risk(d, raw: dict, ev, others: Iterable[dict] = ()) -> dict:
    sig: list[dict] = []

    def add(id_, severity, title, detail):
        sig.append({"id": id_, "severity": severity, "title": title, "detail": detail})

    tx, opened, paid = _tx(raw), _t(raw.get("create_time")), _t(_tx(raw).get("create_time"))
    me, tx_id, mine = _buyer_key(raw), tx.get("buyer_transaction_id"), raw.get("dispute_id")
    others = [o for o in others if o.get("dispute_id") != mine]

    same_tx = [o for o in others if tx_id and _tx(o).get("buyer_transaction_id") == tx_id]
    if same_tx:
        add("SAME_PAYMENT_DISPUTED_AGAIN", "warn", "Same payment disputed more than once",
            f"{len(same_tx)} other dispute(s) already exist for this payment.")
    if me and opened:
        recent = [o for o in others if o not in same_tx and _buyer_key(o) == me
                  and (_t(o.get("create_time")) or opened) >= opened - timedelta(days=REPEAT_WINDOW_DAYS)]
        if recent:
            n = len(recent) + 1
            add("REPEAT_BUYER", "high" if len(recent) >= 2 else "warn", "Repeat disputer",
                f"This buyer has opened {n} disputes with you in {REPEAT_WINDOW_DAYS} days.")

    gross, claimed = _num((tx.get("gross_amount") or {}).get("value")), _num((raw.get("dispute_amount") or {}).get("value"))
    if gross is not None and claimed is not None and claimed > gross:
        add("AMOUNT_EXCEEDS_PAYMENT", "high", "Claimed amount exceeds the payment",
            f"The dispute asks for {claimed} but the payment was {gross}.")

    if raw.get("reason") == INR:
        if d.is_digital and ev.digital_access_log:
            add("CLAIM_CONTRADICTS_RECORDS", "warn", "Buyer says not received, records show access",
                "Your access/download log shows this digital item was used.")
        elif not d.is_digital and ev.delivery_proven:
            add("CLAIM_CONTRADICTS_RECORDS", "warn", "Buyer says not received, records show delivery",
                "Your records show a verified delivery for this order.")
        elif not d.is_digital and opened and paid and opened - paid < timedelta(hours=PREMATURE_HOURS):
            hrs = max(0, int((opened - paid).total_seconds() // 3600))
            add("PREMATURE_NOT_RECEIVED", "info", "Non-delivery claimed very soon after payment",
                f"Opened {hrs} hour(s) after paying; the item may still be in transit.")

    if opened and paid and timedelta(0) <= opened - paid < timedelta(minutes=FAST_MINUTES):
        add("DISPUTED_WITHIN_MINUTES", "info", "Disputed within minutes of paying",
            f"Opened {int((opened - paid).total_seconds() // 60)} minute(s) after payment.")
    if (raw.get("extensions") or {}).get("merchant_contacted") is False:
        add("NO_MERCHANT_CONTACT", "info", "Buyer did not contact you first",
            "PayPal records no contact with the merchant before the dispute.")

    warns = sum(s["severity"] == "warn" for s in sig)
    level = "high" if any(s["severity"] == "high" for s in sig) or warns >= 3 else "elevated" if warns else "low"
    return {"level": level, "signals": sig, "basis": BASIS}
