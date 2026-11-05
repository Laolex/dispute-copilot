#!/usr/bin/env python3
"""Sandbox spike: prove the PayPal disputes path end to end before building any AI.

Run from the repo root:  python -m spike.spike <command> [options]

Commands (read-only unless noted):
  check                       token + the OAuth scopes your app was actually granted
  events                      webhook event types that mention DISPUTE   (answers: real event names)
  order [--amount 25.00]      create a sandbox order, print the buyer approval URL   (writes)
  capture ORDER_ID            capture an approved order, print capture/transaction ids (writes)
  list [--state STATE]        list disputes for the business account
  show DISPUTE_ID             full dispute: status, reason, due date, evidences, available action links
  probe-evidence DISPUTE_ID --file F --yes
                              try POST provide-evidence with multipart `evidence-file`   (writes, sandbox)
  probe-evidence-input DISPUTE_ID --tracking NUM --carrier C --yes
                              try multipart with a JSON `input` part carrying tracking info (writes, sandbox)

Every response is saved verbatim to spike/out/ (gitignored) so we can turn real shapes into test fixtures.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from urllib.parse import urlsplit

from app.config import ConfigError, Settings, is_sandbox_url
from app.paypal import ApiResult, PayPalClient, PayPalUnreachable


def save(settings: Settings, name: str, result: ApiResult) -> Path:
    out = Path(settings.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{int(time.time())}_{name}.json"
    path.write_text(json.dumps({"status": result.status, "debug_id": result.debug_id, "body": result.body}, indent=2))
    return path


def show_result(label: str, result: ApiResult, settings: Settings, name: str) -> None:
    saved = save(settings, name, result)
    print(f"[{label}] HTTP {result.status}  debug_id={result.debug_id}  saved={saved}")
    if not result.ok:
        print(json.dumps(result.body, indent=2) if not isinstance(result.body, str) else result.body)


def require_yes(args: argparse.Namespace) -> None:
    if not getattr(args, "yes", False):
        sys.exit("this command changes sandbox state; re-run with --yes")


def find_link(dispute: dict, rel: str) -> dict | None:
    """PayPal names links with underscores (accept_claim) but paths with hyphens; compare normalized."""
    want = rel.replace("_", "-")
    for link in dispute.get("links", []):
        if str(link.get("rel", "")).replace("_", "-") == want:
            return link
    return None


def money(d: dict | None) -> str:
    return f"{d.get('value')} {d.get('currency_code')}" if d else "-"


def cmd_check(c: PayPalClient, s: Settings, _a: argparse.Namespace) -> int:
    c._access_token()  # noqa: SLF001
    print("token OK")
    scopes = c.granted_scopes
    want = [x for x in scopes if "disputes" in x]
    print(f"granted scopes: {len(scopes)}")
    for sc in want:
        print("  ", sc)
    if not want:
        print("!! no disputes scopes granted. Enable Disputes in the app's feature options (developer dashboard).")
        return 2
    return 0


def cmd_events(c: PayPalClient, s: Settings, _a: argparse.Namespace) -> int:
    # Developer helper only (the app never calls it). If this path 404s, use the Webhooks events reference.
    r = c.get("/v1/notifications/webhooks-event-types")
    show_result("event types", r, s, "event_types")
    if r.ok and isinstance(r.body, dict):
        for e in r.body.get("event_types", []):
            if "DISPUTE" in e.get("name", ""):
                print(f"  {e['name']:<45} {e.get('description', '')}")
    return 0 if r.ok else 1


def cmd_order(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": "spike-order",
            "description": "Dispute Copilot sandbox test order",
            "amount": {"currency_code": "USD", "value": a.amount},
        }],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": "https://example.com/return", "cancel_url": "https://example.com/cancel",
            "user_action": "PAY_NOW"}}},
    }
    r = c.post("/v2/checkout/orders", json=body)
    show_result("create order", r, s, "order_create")
    if not r.ok:
        return 1
    link = next((l["href"] for l in r.body.get("links", []) if l.get("rel") in ("payer-action", "approve")), None)
    print(f"order id: {r.body['id']}")
    print(f"NEXT: open this link in a browser, log in as the SANDBOX PERSONAL (buyer) account, and approve:\n  {link}")
    print(f"THEN: python -m spike.spike capture {r.body['id']}")
    return 0


def cmd_capture(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    r = c.post(f"/v2/checkout/orders/{a.order_id}/capture", json={})
    show_result("capture", r, s, "order_capture")
    if not r.ok:
        return 1
    for pu in r.body.get("purchase_units", []):
        for cap in pu.get("payments", {}).get("captures", []):
            print(f"capture id: {cap['id']}  status: {cap['status']}  amount: {money(cap.get('amount'))}")
    print("NEXT (manual): log in at https://www.sandbox.paypal.com as the BUYER, open the transaction,")
    print("  and report a problem / open a dispute (e.g. 'item not received'). Then: python -m spike.spike list")
    return 0


def cmd_list(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    params: dict[str, Any] = {"page_size": getattr(a, "page_size", 20)}
    if a.state:
        params["dispute_state"] = a.state
    txn = getattr(a, "transaction_id", None)
    if txn:
        params["disputed_transaction_id"] = txn  # cannot be combined with start_time
    else:
        # Always send an explicit window: the deprecated start_time defaults to "now", which can hide new disputes.
        after = datetime.now(timezone.utc) - timedelta(days=min(getattr(a, "days", 30), 180))
        params["update_time_after"] = after.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    r = c.get("/v1/customer/disputes", params=params)
    show_result("list disputes", r, s, "disputes_list")
    if not r.ok:
        return 1
    items = (r.body or {}).get("items", [])
    print(f"{len(items)} dispute(s)")
    for d in items:
        print(f"  {d.get('dispute_id')}  reason={d.get('reason')}  status={d.get('status')}  "
              f"state={d.get('dispute_state')}  stage={d.get('dispute_life_cycle_stage')}  "
              f"amount={money(d.get('dispute_amount'))}")
    return 0


def cmd_show(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    r = c.get(f"/v1/customer/disputes/{a.dispute_id}")
    show_result("show dispute", r, s, f"dispute_{a.dispute_id}")
    if not r.ok:
        return 1
    d = r.body
    print(f"reason: {d.get('reason')}   status: {d.get('status')}   stage: {d.get('dispute_life_cycle_stage')}")
    print(f"amount: {money(d.get('dispute_amount'))}   seller_response_due_date: {d.get('seller_response_due_date')}")
    print(f"disputed_transactions: {len(d.get('disputed_transactions', []))}   "
          f"messages: {len(d.get('messages', []))}   evidences: {len(d.get('evidences', []))}")
    for m in d.get("messages", []):
        print(f"  message by {m.get('posted_by')} at {m.get('time_posted')}: {str(m.get('content', ''))[:120]}")
    for e in d.get("evidences", []):
        print(f"  evidence {e.get('evidence_type')} source={e.get('source')} notes={str(e.get('notes', ''))[:80]}")
    print("ACTION LINKS (an action is only allowed if its link is listed):")
    for l in d.get("links", []):
        print(f"  {l.get('rel'):<24} {l.get('method', 'GET'):<6} {l.get('href')}")
    return 0


def cmd_probe_evidence(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    require_yes(a)
    path = Path(a.file)
    mime = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
            ".gif": "image/gif"}.get(path.suffix.lower(), "text/plain")
    files = {"evidence-file": (path.name, path.read_bytes(), mime)}
    if getattr(a, "type", None):  # optionally label the upload, as the chargeback stage may require
        body = {"evidences": [{"evidence_type": a.type, "notes": getattr(a, "notes", None) or "Evidence attached."}]}
        files["input"] = (None, json.dumps(body), "application/json")
    r = c.post(f"/v1/customer/disputes/{a.dispute_id}/provide-evidence", files=files)
    show_result("provide-evidence (file)", r, s, f"probe_evidence_file_{a.dispute_id}")
    return 0 if r.ok else 1


def cmd_probe_evidence_input(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    require_yes(a)
    # EXPERIMENT: the reference shows only `evidence-file`; this tests whether a JSON `input` part
    # carrying tracking info is accepted. The saved response tells us either way.
    payload = {"evidences": [{"evidence_type": "PROOF_OF_FULFILLMENT",
                              "evidence_info": {"tracking_info": [
                                  {"carrier_name": a.carrier, "tracking_number": a.tracking}]},
                              "notes": getattr(a, "notes", None) or "Shipped; tracking provided."}]}
    files = {"input": ("input.json", json.dumps(payload).encode(), "application/json")}
    r = c.post(f"/v1/customer/disputes/{a.dispute_id}/provide-evidence", files=files)
    show_result("provide-evidence (input json)", r, s, f"probe_evidence_input_{a.dispute_id}")
    return 0 if r.ok else 1


def _guarded_sandbox_action(c: PayPalClient, s: Settings, dispute_id: str, rel: str, body: dict, name: str) -> int:
    """Invariant: only call an action PayPal currently offers (its link is present on the dispute)."""
    shown = c.get(f"/v1/customer/disputes/{dispute_id}")
    if not shown.ok:
        show_result("show dispute", shown, s, f"dispute_{dispute_id}")
        return 1
    d = shown.body or {}
    link = find_link(d, rel)
    if link is None:
        offered = sorted(l.get("rel", "") for l in d.get("links", []))
        print(f"refusing: PayPal does not currently offer '{rel}' on {dispute_id} "
              f"(status={d.get('status')}, stage={d.get('dispute_life_cycle_stage')}). Offered: {offered}")
        print("tip: sandbox actions can take a few minutes to appear after a dispute is created; run `show` again.")
        return 4
    # Follow PayPal's own link instead of building a path, and never leave the sandbox host.
    href = link.get("href", "")
    if not is_sandbox_url(href):
        print(f"refusing: link for '{rel}' points outside {s.base_url}: {href}")
        return 4
    r = c.request(link.get("method", "POST"), urlsplit(href).path, json=body)
    show_result(rel, r, s, f"{name}_{dispute_id}")
    return 0 if r.ok else 1


def cmd_require_evidence(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    require_yes(a)
    action = {"seller": "SELLER_EVIDENCE", "buyer": "BUYER_EVIDENCE"}[a.party]
    return _guarded_sandbox_action(c, s, a.dispute_id, "require-evidence", {"action": action}, "require_evidence")


def cmd_adjudicate(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    require_yes(a)
    outcome = {"seller": "SELLER_FAVOR", "buyer": "BUYER_FAVOR"}[a.outcome]
    return _guarded_sandbox_action(c, s, a.dispute_id, "adjudicate", {"adjudication_outcome": outcome}, "adjudicate")


def cmd_act(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    """Generic guarded action: only runs if PayPal currently offers the link."""
    require_yes(a)
    body = json.loads(a.body) if a.body else {}
    return _guarded_sandbox_action(c, s, a.dispute_id, a.rel, body, f"act_{a.rel}")


WEBHOOK_EVENTS = ["CUSTOMER.DISPUTE.CREATED", "CUSTOMER.DISPUTE.UPDATED", "CUSTOMER.DISPUTE.RESOLVED", "RISK.DISPUTE.CREATED"]


def cmd_webhook_create(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    """Register our receiver URL with PayPal for the dispute events. Prints the webhook id to export."""
    require_yes(a)
    if not a.url.startswith("https://"):
        print("refusing: PayPal only delivers webhooks to public https URLs"); return 4
    r = c.post("/v1/notifications/webhooks", json={"url": a.url, "event_types": [{"name": n} for n in WEBHOOK_EVENTS]})
    show_result("create webhook", r, s, "webhook_create")
    if r.ok and isinstance(r.body, dict):
        print(f"webhook id: {r.body.get('id')}\nNEXT: export PAYPAL_WEBHOOK_ID={r.body.get('id')}  (then restart the app)")
    return 0 if r.ok else 1


def cmd_webhook_list(c: PayPalClient, s: Settings, a: argparse.Namespace) -> int:
    r = c.get("/v1/notifications/webhooks")
    show_result("list webhooks", r, s, "webhook_list")
    for w in (r.body or {}).get("webhooks", []) if isinstance(r.body, dict) else []:
        print(f"  {w.get('id')}  {w.get('url')}  events={[e.get('name') for e in w.get('event_types', [])]}")
    return 0 if r.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("events")
    o = sub.add_parser("order"); o.add_argument("--amount", default="25.00")
    cp = sub.add_parser("capture"); cp.add_argument("order_id")
    ls = sub.add_parser("list"); ls.add_argument("--state", help="comma-separated: REQUIRED_ACTION, REQUIRED_OTHER_PARTY_ACTION, UNDER_PAYPAL_REVIEW, RESOLVED, OPEN_INQUIRIES, APPEALABLE")
    ls.add_argument("--days", type=int, default=30, help="look back this many days by update time (max 180)")
    ls.add_argument("--transaction-id", help="filter by disputed transaction id (replaces the time window)")
    ls.add_argument("--page-size", type=int, default=20)
    re_ = sub.add_parser("sandbox-require-evidence"); re_.add_argument("dispute_id"); re_.add_argument("--party", choices=["seller", "buyer"], default="seller"); re_.add_argument("--yes", action="store_true")
    wc = sub.add_parser("webhook-create"); wc.add_argument("--url", required=True); wc.add_argument("--yes", action="store_true")
    sub.add_parser("webhook-list")
    ac = sub.add_parser("act"); ac.add_argument("dispute_id"); ac.add_argument("rel"); ac.add_argument("--body", help="JSON body"); ac.add_argument("--yes", action="store_true")
    ad = sub.add_parser("sandbox-adjudicate"); ad.add_argument("dispute_id"); ad.add_argument("--outcome", choices=["seller", "buyer"], required=True); ad.add_argument("--yes", action="store_true")
    sh = sub.add_parser("show"); sh.add_argument("dispute_id")
    pe = sub.add_parser("probe-evidence"); pe.add_argument("dispute_id"); pe.add_argument("--file", required=True); pe.add_argument("--type"); pe.add_argument("--notes"); pe.add_argument("--yes", action="store_true")
    pi = sub.add_parser("probe-evidence-input"); pi.add_argument("dispute_id"); pi.add_argument("--tracking", required=True); pi.add_argument("--carrier", default="OTHER"); pi.add_argument("--notes"); pi.add_argument("--yes", action="store_true")
    return p


COMMANDS = {
    "webhook-create": cmd_webhook_create,
    "webhook-list": cmd_webhook_list,
    "act": cmd_act,
    "check": cmd_check, "events": cmd_events, "order": cmd_order, "capture": cmd_capture,
    "list": cmd_list, "show": cmd_show,
    "probe-evidence": cmd_probe_evidence, "probe-evidence-input": cmd_probe_evidence_input,
    "sandbox-require-evidence": cmd_require_evidence, "sandbox-adjudicate": cmd_adjudicate,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2
    client = PayPalClient(settings, audit=lambda e: print(f"  > {e['call']} -> {e['status']}", file=sys.stderr))
    try:
        return COMMANDS[args.cmd](client, settings, args)
    except PayPalUnreachable as e:
        print(f"network error: {e}", file=sys.stderr)
        return 3
    except httpx.TimeoutException as e:
        print(f"timed out waiting for PayPal ({type(e).__name__}). The request may or may not have been processed; "
              "for a command that changes state, check with `list`/`show` before retrying.", file=sys.stderr)
        return 3
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
