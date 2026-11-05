"""Approval UI + API. Server-rendered pages; the proposals and audit lists use AG Grid Community (vendored).

Auth: HTTP Basic (password from DC_ADMIN_PASSWORD; the app refuses to start without one).
State-changing POSTs also need a per-action CSRF token (HMAC of password, proposal id, action),
because browsers resend Basic credentials automatically.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import re
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .executor import abandon, execute, reconcile
from .ingest import poll
from .models import parse_dispute
from .orders import OrderStore
from .paypal import PayPalClient
from .store import InvalidTransition, Store

e = html.escape
def _money(raw: dict) -> str:
    amt = (raw.get("dispute_amount") or {})
    try:
        return f"{amt.get('currency_code') or ''} {float(amt.get('value')):,.2f}".strip()
    except (TypeError, ValueError):
        return "Amount unavailable"


def _date(value: str | None) -> str:
    if not value:
        return "—"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%d %b %Y · %H:%M")
    except ValueError:
        return value


def _pill(value: str) -> str:
    safe = re.sub(r"[^a-z0-9-]", "", str(value or "unknown").lower().replace(" ", "-").replace("_", "-"))
    return f"<span class='pill pill-{safe}'>{e(str(value or 'unknown').replace('_', ' ').lower())}</span>"



def create_app(client: PayPalClient, store: Store, orders: OrderStore, base_url: str, password: str, agent=None,
               docs_dir: str | Path = "data/docs", webhook_id: str | None = None) -> FastAPI:
    if not password:
        raise RuntimeError("DC_ADMIN_PASSWORD is required")
    app = FastAPI(title="Dispute Copilot", docs_url=None, redoc_url=None, openapi_url=None)
    basic = HTTPBasic()
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "font-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp

    def auth(cred: HTTPBasicCredentials = Depends(basic)) -> str:
        ok = secrets.compare_digest(cred.password.encode(), password.encode())
        if not ok:
            raise HTTPException(401, "bad credentials", headers={"WWW-Authenticate": "Basic"})
        return cred.username or "approver"

    def csrf(pid: int | str, action: str) -> str:
        return hmac.new(password.encode(), f"{pid}:{action}".encode(), hashlib.sha256).hexdigest()

    def check_csrf(pid, action, token):
        if not hmac.compare_digest(csrf(pid, action), token):
            raise HTTPException(403, "bad CSRF token")

    def page(title: str, body: str, active: str = "overview", scripts: str = "") -> HTMLResponse:
        ok, bad = store.verify_audit()
        banner = "" if ok else f"<div class='chainbanner'>AUDIT CHAIN BROKEN at #{int(bad)}: records were edited or deleted.</div>"
        nav = [("overview", "▦", "Overview", "/"), ("audit", "◷", "Audit trail", "/audit")]
        links = "".join(f"<a class='{'active' if k == active else ''}' href='{u}'><span class='nav-ico'>{i}</span>{l}</a>"
                        for k, i, l, u in nav)
        return HTMLResponse(
            "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'><meta name='theme-color' content='#101d35'>"
            f"<title>{e(title)} · Dispute Copilot</title><link rel='stylesheet' href='/static/dashboard.css'></head><body>"
            "<div class='shell'><aside class='sidebar'><a class='brand' href='/'><div class='brand-mark'>D</div><div>"
            "<div class='brand-name'>dispute copilot</div><div class='brand-sub'>merchant operations</div></div></a>"
            f"<div><div class='nav-label'>Workspace</div><nav class='nav'>{links}</nav></div>"
            "<div class='side-bottom'><div class='sandbox'><strong><i class='dot'></i> PayPal sandbox</strong>"
            "<p>Sandbox only. Nothing is sent to PayPal until a human approves.</p></div>"
            "<div class='profile'><div class='avatar'>M</div><div><strong>Merchant workspace</strong><small>Administrator</small></div></div></div></aside>"
            f"<main class='main'><header class='topbar'><div class='crumb'>Workspace <span style='padding:0 7px;color:#c4ccd7'>/</span><b>{e(title)}</b></div></header>"
            f"{banner}{body}</main></div>{scripts}</body></html>")

    from .webhooks import add_webhook
    add_webhook(app, client, store, orders, webhook_id, agent, docs_dir)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    SCRIPTS = "<script src='/static/ag-grid-community.min.js'></script><script src='/static/app.js'></script>"

    @app.get("/api/proposals")
    def api_proposals(_: str = Depends(auth)):
        rows = []
        for p in store.list_proposals():
            raw = store.get_dispute(p["dispute_id"]) or {}
            d = parse_dispute(raw) if raw else None
            dec = p["decision"]
            an = dec.get("analysis") or {}
            rows.append({
                "id": p["id"], "dispute_id": p["dispute_id"], "action": dec["action"], "link_rel": dec.get("link_rel"),
                "rule_id": dec["rule_id"], "state": p["state"], "executable": bool(dec.get("executable")),
                "amount": float(d.amount) if d and d.amount is not None else None, "currency": d.currency if d else None,
                "stage": d.stage if d else None, "reason": d.reason if d else None,
                "claim_type": an.get("claim_type"),
                "ai_action": (dec.get("recommendation") or {}).get("recommended_action"),
                "ai_disagrees": bool(dec.get("recommendation")) and dec["recommendation"].get("recommended_action") != dec["action"], "mismatch": an.get("consistent_with_reason", True) is False,
                "due": d.seller_response_due.isoformat() if d and d.seller_response_due else None,
                "created_at": p["created_at"], "missing_documents": len(dec.get("missing_documents") or [])})
        return JSONResponse(rows)

    @app.get("/api/audit")
    def api_audit(_: str = Depends(auth)):
        ok, bad = store.verify_audit()
        rows = [{"seq": r["seq"], "ts": r["ts"], "actor": r["actor"], "event": r["event"], "payload": r["payload"]}
                for r in store.audit_rows()[-5000:]]
        return JSONResponse({"ok": ok, "bad_seq": bad, "rows": rows})

    @app.get("/", response_class=HTMLResponse)
    def index(_: str = Depends(auth)):
        ok, bad = store.verify_audit()
        poll_form = (f"<form method='post' action='/poll' style='margin:0'><input type='hidden' name='token' value='{csrf('x', 'poll')}'>"
                     "<button class='btn' type='submit'>↻ &nbsp; Sync PayPal disputes</button></form>")
        chain = (f"<div class='audit-status'>✓ Audit chain verified</div>" if ok
                 else f"<div class='audit-status bad'>! Integrity issue at record #{int(bad)}</div>")
        body = ("<div class='content'><div class='heading'><div><div class='eyebrow'>Merchant overview</div>"
                "<h1>Dispute desk</h1><p>Review what the policy proposes, check the evidence, then approve or reject.</p></div>"
                f"{poll_form}</div><div class='stats' id='tiles'></div>"
                "<section class='panel'><div class='panel-head'><div><h2>Dispute queue</h2>"
                "<p>Sort, filter and search every proposal</p></div></div><div class='grid-tools'>"
                "<input type='search' id='q' placeholder='Filter proposals (dispute, rule, state, reason...)' aria-label='Filter proposals'></div>"
                "<div id='grid' class='grid' data-kind='proposals'></div></section>"
                "<div class='two'><section class='insight'><div class='eyebrow'>Your control centre</div><h3>Decisions stay yours.</h3>"
                "<p>The policy proposes an action, the AI explains and drafts, and nothing reaches PayPal until you approve it. "
                "Approvals expire if the dispute or your evidence changes.</p></section>"
                "<section class='panel audit-card'><div class='panel-head'><div><h2>Audit integrity</h2>"
                f"<p>Append-only, hash-chained activity log</p></div></div><div class='panel-body'>{chain}</div></section></div></div>")
        return page("Overview", body, "overview", SCRIPTS)

    @app.post("/poll")
    def do_poll(token: str = Form(...), user: str = Depends(auth)):
        check_csrf("x", "poll", token)
        ids = poll(client, store, orders, agent=agent, docs_dir=Path(docs_dir))
        store.audit(user, "poll.manual", {"new_proposals": ids})
        return RedirectResponse("/", status_code=303)

    @app.get("/proposals/{pid}", response_class=HTMLResponse)
    def detail(pid: int, _: str = Depends(auth)):
        p = store.get_proposal(pid)
        if not p:
            raise HTTPException(404)
        raw = store.get_dispute(p["dispute_id"]) or {}
        d = parse_dispute(raw) if raw else None
        dec = p["decision"]
        action = dec.get("action", "review")

        def fact(label, value):
            return f"<div class='fact'><label>{e(label)}</label><strong>{e(str(value))}</strong></div>"

        facts = ""
        if d:
            title = lambda v: str(v or "—").replace("_", " ").title()
            facts = ("<div class='facts'>" + fact("Dispute reason", title(d.reason)) + fact("Dispute amount", f"{d.amount} {d.currency or ''}")
                     + fact("Held by PayPal", d.held_amount) + fact("Lifecycle stage", title(d.stage)) + fact("Current status", title(d.status))
                     + fact("Item type", ", ".join(d.item_types) or "—") + fact("Seller protection", title(d.seller_protection) if d.seller_protection else "Not indicated")
                     + fact("Response due", _date(raw.get("seller_response_due_date"))) + fact("PayPal requests", ", ".join(d.requested_types) or "—")
                     + fact("Already submitted", ", ".join(x.type for x in d.submitted) or "—") + "</div>"
                     f"<div class='claim'><b style='display:block;color:#344765;margin-bottom:5px'>Buyer's statement</b>"
                     f"{e(d.buyer_claim or 'No buyer statement supplied by PayPal.')}</div>")
        why = "".join(f"<li>{e(r)}</li>" for r in dec.get("reasons", [])) or "<li>No policy explanation supplied.</li>"
        an = dec.get("analysis")
        analysis = ""
        if an:
            flags = "".join(f"<li style='color:#b34d4d'>{e(x)}</li>" for x in an.get("red_flags", []))
            mismatch = ("" if an.get("consistent_with_reason", True) else
                        "<div class='warning'>The claim does not match the dispute reason. Review before taking action.</div>")
            analysis = ("<section class='panel'><div class='panel-head'><div><h2>Claim analysis</h2>"
                        f"<p>{e(an.get('source', ''))} · advisory only</p></div><span class='pill pill-review'>AI advisory</span></div>"
                        f"<div class='panel-body'><div class='ai-box'><div class='ai-label'>Claim classification · {e(an.get('claim_type', 'Unknown'))}</div>"
                        f"<p>{e(an.get('summary', ''))}</p>{mismatch}{f'<ul class=reason-list>{flags}</ul>' if flags else ''}</div></div></section>")
        rec = dec.get("recommendation")
        recommendation_html = ""
        if rec:
            rec_action = str(rec.get("recommended_action", "review"))
            conf = rec.get("confidence", 0)
            conf_txt = f"{conf:.0%} confidence" if isinstance(conf, (int, float)) else f"{e(str(conf))} confidence"
            if rec_action != action:
                flag = (f"<div class='error'><b>AI disagrees with policy.</b> Policy says {e(action)}; the model says {e(rec_action)}. "
                        "The policy decision stands. Look closely before approving.</div>")
            else:
                flag = "<p class='muted' style='font-size:11px;margin:0 0 12px'>The AI agrees with the policy decision.</p>"
            rec_reasons = "".join(f"<li>{e(r)}</li>" for r in (rec.get("reasons") or []))
            rec_evidence = "".join(f"<li>{e(x)}</li>" for x in (rec.get("evidence_plan") or []))
            ev_block = ("<h4 style='font-size:11px;margin:17px 0 5px'>Suggested evidence plan</h4>"
                        f"<ul class='reason-list'>{rec_evidence}</ul>") if rec_evidence else ""
            recommendation_html = ("<section class='panel'><div class='panel-head'><div><h2>AI recommendation</h2>"
                                   f"<p>{e(rec.get('model', 'Unknown model'))} · advisory only</p></div>{_pill(rec_action)}</div><div class='panel-body'>{flag}"
                                   f"<div class='facts'>{fact('Suggested action', rec_action.title())}{fact('Model confidence', conf_txt)}"
                                   f"{fact('Policy reference', rec.get('recommended_rule_id') or 'Not specified')}</div>"
                                   f"<ul class='reason-list'>{rec_reasons}</ul>{ev_block}</div></section>")
        controls = ""
        if p["state"] == "PENDING" and action in ("contest", "accept"):
            if dec.get("missing_documents"):
                controls = f"<div class='error'>Cannot approve yet. Required files are missing: {e('; '.join(dec['missing_documents']))}</div>"
            elif dec.get("executable"):
                via = e(str(dec.get("link_rel", "")).replace("_", " "))
                controls = (f"<div class='action-row'><form method='post' action='/proposals/{pid}/approve'>"
                            f"<input type='hidden' name='token' value='{csrf(pid, 'approve')}'>"
                            f"<button class='btn' type='submit'>Approve and {e(action)} ({via})</button></form>"
                            f"<form method='post' action='/proposals/{pid}/reject'><input type='hidden' name='token' value='{csrf(pid, 'reject')}'>"
                            "<button class='btn btn-danger' type='submit'>Reject</button></form></div>")
            else:
                controls = "<div class='error'>Not executable: PayPal is not offering the required action, so this proposal cannot be sent.</div>"
        elif p["state"] == "PENDING":
            controls = "<div class='warning'>This proposal is advisory only and has no executable PayPal action.</div>"
        elif p["state"] in ("APPROVED", "UNCERTAIN"):
            prior = p["result"] if isinstance(p["result"], dict) else {}
            if p["state"] == "UNCERTAIN":
                msg = (f"<b>Result unknown.</b> {e(str(prior.get('reason', 'PayPal did not confirm the request.')))} "
                       "Check with PayPal before doing anything else.")
                if prior.get("reconciled_not_applied"):
                    msg += " PayPal does not show it yet; retrying reuses the same request id so PayPal can deduplicate."
                if prior.get("last_refusal"):
                    msg += f" Last retry was refused: {e(str(prior['last_refusal']))}."
            else:
                msg = "<b>Approved but not confirmed.</b> The app may have stopped before the request finished."

            def btn(act, label, cls="btn btn-outline"):
                return (f"<form method='post' action='/proposals/{pid}/{act}'><input type='hidden' name='token' value='{csrf(pid, act)}'>"
                        f"<button class='{cls}' type='submit'>{label}</button></form>")
            can_retry = p["state"] == "APPROVED" or bool(prior.get("reconciled_not_applied"))
            controls = (f"<div class='warning'>{msg}</div><div class='action-row' style='flex-wrap:wrap'>"
                        + btn("reconcile", "Check status with PayPal", "btn")
                        + (btn("retry", "Retry (same request id)") if can_retry else "")
                        + btn("abandon", "Close as failed", "btn btn-danger") + "</div>")
        files = ""
        if dec.get("documents") and dec.get("link_rel") == "provide_evidence":
            files = ("<div class='panel-body' style='padding-top:4px'>" + "".join(
                f"<div class='file-row'><span class='file-ico'>▤</span><span>{e(n)}</span>"
                "<span style='margin-left:auto;color:#8b98aa'>Evidence file</span></div>" for n in dec["documents"]) + "</div>")
        result = (f"<section class='panel'><div class='panel-head'><div><h2>Execution result</h2><p>PayPal response captured by the executor</p></div></div>"
                  f"<div class='panel-body'><pre class='result'>{e(str(p['result']))}</pre></div></section>") if p["result"] else ""
        facts_panel = ("<section class='panel'><div class='panel-head'><div><h2>Dispute details</h2><p>Information retrieved from PayPal</p></div></div>"
                       f"<div class='panel-body'>{facts or 'No dispute data stored.'}</div></section>")
        policy_panel = (f"<section class='panel'><div class='panel-head'><div><h2>Policy assessment</h2><p>Deterministic rule · {e(dec.get('rule_id', ''))}</p></div>{_pill(action)}</div>"
                        f"<div class='panel-body'><p style='font-size:11px;color:#718096;margin:0'>Why this action was selected</p><ul class='reason-list'>{why}</ul></div></section>")
        items = dec.get("evidence_items") or []
        evidence_panel = ""
        if items:
            counts = {k: sum(1 for i in items if i.get("status") == k) for k in ("verified", "unverified", "missing", "conflict")}
            summary = " · ".join(f"{n} {k}" for k, n in counts.items() if n)
            rows = "".join(
                f"<div class='file-row' style='align-items:flex-start'><div style='flex:1;min-width:0'><b style='color:#30425f'>{e(str(i.get('label', '')))}</b>"
                f"<div class='case-sub' style='white-space:normal'>{e(str(i.get('detail', '')))}"
                f"{' · source: ' + e(str(i['source'])) if i.get('source') else ''}</div></div>{_pill(str(i.get('status', 'unknown')))}</div>"
                for i in items)
            evidence_panel = ("<section class='panel'><div class='panel-head'><div><h2>Evidence provenance</h2>"
                              f"<p>What the merchant held when this was proposed · {e(summary)}</p></div></div><div class='panel-body'>{rows}</div></section>")
        draft_panel = ("<section class='panel'><div class='panel-head'><div><h2>Response draft</h2><p>Generated from the available dispute context</p></div></div>"
                       f"<div class='panel-body'><div class='draft'>{e(p['draft'] or '(No response draft available)')}</div></div></section>")
        no_files = "<div class='panel-body'><p style='font-size:11px;color:#8390a3'>No files are uploaded with this action.</p></div>"
        side = ("<section class='panel action-card'><div class='panel-head'><div><h2>Merchant decision</h2><p>Human authorization required</p></div></div><div class='panel-body'>"
                f"<div style='display:flex;justify-content:space-between;align-items:center'><span style='font-size:11px;color:#8390a3'>Proposal status</span>{_pill(p['state'])}</div>"
                f"<div style='display:flex;justify-content:space-between;align-items:center;margin-top:17px'><span style='font-size:11px;color:#8390a3'>Policy action</span>{_pill(action)}</div>"
                f"{controls}<p style='font-size:10px;line-height:1.6;color:#929eae;margin:14px 0 0'>Approval authorizes the proposed action to be sent to PayPal. "
                "It is refused if the dispute, your records or your evidence files change first.</p></div></section>"
                "<section class='panel'><div class='panel-head'><div><h2>Evidence package</h2><p>Files that will be uploaded</p></div></div>"
                f"{files or no_files}</section>")
        body = (f"<div class='content'><div class='detail-top'><a class='text-link' href='/'>← Back to dispute queue</a><span>/</span><span>Proposal #{pid}</span></div>"
                f"<div class='heading'><div><div class='eyebrow'>Dispute review</div><h1>{e(p['dispute_id'])}</h1>"
                f"<p>Created {e(_date(p.get('created_at')))} · Proposal #{pid}</p></div><div style='display:flex;gap:8px;align-items:center'>{_pill(action)}{_pill(p['state'])}</div></div>"
                f"<div class='detail-layout'><div class='stack'>{facts_panel}{policy_panel}{evidence_panel}{analysis}{recommendation_html}{draft_panel}{result}</div><div class='stack'>{side}</div></div></div>")
        return page(f"Proposal #{pid}", body, "overview")

    @app.post("/proposals/{pid}/{action}")
    def decide_action(pid: int, action: str, token: str = Form(...), user: str = Depends(auth)):
        if action not in ("approve", "reject", "reconcile", "retry", "abandon"):
            raise HTTPException(404)
        check_csrf(pid, action, token)
        p = store.get_proposal(pid)
        if not p:
            raise HTTPException(404)
        try:
            if action == "reject":
                store.transition(pid, "REJECTED", by=user)
                store.audit(user, "proposal.rejected", {"proposal_id": pid})
            elif action == "approve":
                if not p["decision"].get("executable"):
                    raise HTTPException(409, "decision is not executable")
                store.transition(pid, "APPROVED", by=user)
                store.audit(user, "proposal.approved", {"proposal_id": pid, "action": p["decision"]["action"]})
                execute(client, store, base_url, pid, Path(docs_dir), orders)
            elif action == "reconcile":
                store.audit(user, "execute.reconcile_requested", {"proposal_id": pid})
                reconcile(client, store, pid)
            elif action == "retry":
                store.audit(user, "execute.retry_requested", {"proposal_id": pid})
                execute(client, store, base_url, pid, Path(docs_dir), orders, retry=True)
            else:
                abandon(store, pid, user)
        except (InvalidTransition, PermissionError) as ex:
            raise HTTPException(409, str(ex))
        return RedirectResponse(f"/proposals/{pid}", status_code=303)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_view(_: str = Depends(auth)):
        body = ("<div class='content'><div class='heading'><div><div class='eyebrow'>Accountability</div><h1>Audit trail</h1>"
                "<p>A chronological, hash-chained record of decisions and actions. Click a row for its full payload.</p></div></div>"
                "<section class='panel'><div class='panel-head'><div><h2>Activity history</h2><p id='count' class='muted'></p></div></div>"
                "<div class='grid-tools'><input type='search' id='q' placeholder='Filter audit events (actor, event, payload...)' aria-label='Filter audit log'></div>"
                "<div id='grid' class='grid' data-kind='audit'></div><div id='detail' class='detail'></div></section></div>")
        return page("Audit trail", body, "audit", SCRIPTS)

    return app
