"""Merchant order/fulfilment store: what *we* can prove about an order.

In production this is the merchant's own OMS. For the sandbox demo it is a JSON file keyed by the
PayPal seller_transaction_id. Unknown transactions yield no evidence, which makes the policy accept.
"""
from __future__ import annotations

import json
from pathlib import Path

from .rules import MerchantEvidence, Shipment


class OrderStore:
    def __init__(self, path: str | Path | None = None, data: dict | None = None) -> None:
        self._path = Path(path) if path else None
        self._mtime = None
        self.last_error: str | None = None
        self._data = data if data is not None else {}
        if data is None:
            self._reload()

    def _reload(self) -> None:
        """Re-read the file when it changes, so edits take effect without restarting the app."""
        if not self._path or not self._path.exists():
            return
        m = self._path.stat().st_mtime_ns
        if m == self._mtime:
            return
        try:
            self._data, self._mtime = json.loads(self._path.read_text()), m
        except json.JSONDecodeError as ex:
            msg = f"{self._path} is not valid JSON (line {ex.lineno}, column {ex.colno}): {ex.msg}"
            if self._mtime is None:      # first load: fail fast with a readable message
                raise ValueError(msg) from None
            self._mtime = m              # later edit: keep the last good data, don't crash a poll
            self.last_error = msg
            return
        self.last_error = None

    def evidence_for(self, seller_transaction_id: str | None) -> MerchantEvidence:
        if self._path:
            self._reload()
        e = self._data.get(seller_transaction_id or "", {})
        return MerchantEvidence(
            tracking_numbers=tuple(e.get("tracking_numbers", ())),
            delivery_confirmed=bool(e.get("delivery_confirmed", False)),
            digital_access_log=bool(e.get("digital_access_log", False)),
            documents=tuple(e.get("documents", ())),
            shipments=tuple(_shipment(x) for x in e.get("shipments", ()) if isinstance(x, dict)),
        )


def _shipment(x: dict) -> Shipment:
    return Shipment(carrier=str(x.get("carrier", "")), tracking_number=str(x.get("tracking_number", "")),
                    status=str(x.get("status", "UNKNOWN")), delivered_at=(str(x["delivered_at"]) if x.get("delivered_at") else None),
                    source=str(x.get("source", "")), verified=x.get("verified") is True)
