"""Evidence provenance: what we hold for a dispute, where it came from, and whether it is usable.

The report is built when the proposal is made and stored with it, so a reviewer (and the audit trail) can see
exactly what the merchant knew at decision time. It explains the policy's inputs; it never changes the decision.
Statuses: verified (usable as proof), unverified (on record but not proof), missing (needed, absent),
conflict (records contradict the dispute).
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from .docs import DocError, load_document


def _when(s: str | None):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc) if s else None
    except ValueError:
        return None


def evidence_report(d, ev, docs_dir: Path | None, opened_at: str | None = None) -> list[dict]:
    items: list[dict] = []

    def add(kind, label, status, detail, source=""):
        items.append({"kind": kind, "label": label, "status": status, "detail": detail, "source": source})

    opened = _when(opened_at)
    if d.is_digital:
        if ev.digital_access_log:
            add("access", "Digital access / download log", "verified", "Access records are on file for this order.", "order record")
        else:
            add("access", "Digital access / download log", "missing", "Needed to prove a digital item was delivered.")
    else:
        for x in ev.shipments:
            label = f"Shipment {x.carrier or 'carrier'} {x.tracking_number}".strip()
            when = _when(x.delivered_at)
            if x.proven and opened and when and when > opened:
                add("delivery", label, "conflict", f"Marked delivered {x.delivered_at}, after the dispute was opened.", x.source)
            elif x.proven:
                add("delivery", label, "verified", f"Delivered {x.delivered_at}.", x.source)
            else:
                why = []
                if x.status.upper() != "DELIVERED":
                    why.append(f"status is {x.status}")
                if not x.verified:
                    why.append("not verified against the carrier")
                if not x.delivered_at:
                    why.append("no delivery time")
                add("delivery", label, "unverified", "On record, but " + ", ".join(why) + ".", x.source)
        if not ev.shipments:
            if ev.tracking_numbers and ev.delivery_confirmed:
                add("delivery", f"Tracking {', '.join(ev.tracking_numbers)}", "verified",
                    "Merchant record marks it delivered (no carrier detail stored).", "order record")
            elif ev.tracking_numbers:
                add("delivery", f"Tracking {', '.join(ev.tracking_numbers)}", "unverified",
                    "A tracking number exists, but delivery is not confirmed.", "order record")
            else:
                add("delivery", "Carrier-confirmed delivery", "missing", "No shipment or tracking on record for this order.")
    for name in ev.documents:
        try:
            _, data, _m = load_document(Path(docs_dir), name) if docs_dir else (name, b"", "")
            if docs_dir is None:
                add("document", name, "unverified", "File not checked (no documents folder configured).", "order record")
            else:
                add("document", name, "verified", f"{len(data) / 1024:.0f} KB · sha256 {hashlib.sha256(data).hexdigest()[:12]}… "
                    "(re-checked before anything is sent)", "documents folder")
        except DocError as e:
            add("document", name, "missing", str(e), "documents folder")
    if not ev.documents:
        add("document", "Supporting documents", "missing", "No listing, receipt or correspondence on record.")
    return items
