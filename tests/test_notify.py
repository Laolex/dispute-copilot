import json
from datetime import datetime, timedelta, timezone

import httpx

from app.notify import Notifier, Telegram, WhatsApp, from_env
from app.store import Store

DISPUTE = {"dispute_id": "PP-R-ABC-1", "reason": "MERCHANDISE_OR_SERVICE_NOT_RECEIVED",
           "dispute_amount": {"currency_code": "USD", "value": "25.00"},
           "seller_response_due_date": (datetime.now(timezone.utc) + timedelta(hours=20)).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "update_time": "2026-11-05T10:00:00Z", "status": "WAITING_FOR_SELLER_RESPONSE"}


class Sink:
    def __init__(self, ok=True):
        self.reqs, self.ok = [], ok

    def __call__(self, req):
        self.reqs.append(req)
        if not self.ok:
            raise httpx.ConnectError("boom: " + str(req.url), request=req)
        return httpx.Response(200, json={"ok": True, "sid": "SM1"})


def setup(channels_for, **kw):
    s = Store(); s.upsert_dispute(DISPUTE)
    sink = Sink(kw.pop("ok", True))
    cl = httpx.Client(transport=httpx.MockTransport(sink))
    n = Notifier(s, channels_for(cl), "https://dc.example.com", sync=True, **kw)
    s.subscribe(n.handle)
    return s, n, sink


def tg(cl):
    return [Telegram("123:SECRET-TOKEN", "-100", cl)]


def new_proposal(s, **dec):
    return s.create_proposal("PP-R-ABC-1", "u1#e", {"action": "contest", "rule_id": "R10_INR_PROVEN", "executable": True, **dec})


def test_new_proposal_sends_one_bold_telegram_message_with_link_and_no_buyer_text():
    s, n, sink = setup(tg)
    pid = new_proposal(s, reasons=["buyer said <script>x</script>"])
    assert len(sink.reqs) == 1
    body = json.loads(sink.reqs[0].content)
    assert sink.reqs[0].url.host == "api.telegram.org" and body["parse_mode"] == "HTML"
    t = body["text"]
    assert "<b>PP-R-ABC-1</b>" in t and "<b>CONTEST</b>" in t and "USD 25.00" in t
    assert f"https://dc.example.com/proposals/{pid}" in t and "script" not in t
    assert any(r["event"] == "notify.sent" for r in s.audit_rows())


def test_each_alert_is_sent_once_even_if_asked_twice():
    s, n, sink = setup(tg)
    pid = new_proposal(s)
    n.handle("proposal.created", pid)
    assert len(sink.reqs) == 1


def test_whatsapp_uses_star_bold_and_strips_markup_characters():
    s, n, sink = setup(lambda cl: [WhatsApp("ACsid", "tok", "whatsapp:+14155238886", "whatsapp:+2348000000000", cl)])
    new_proposal(s, rule_id="R_*bad*_rule")
    req = sink.reqs[0]
    assert req.url.host == "api.twilio.com"
    form = dict(x.split("=", 1) for x in req.content.decode().split("&"))
    from urllib.parse import unquote_plus
    body = unquote_plus(form["Body"])
    assert "*PP-R-ABC-1*" in body and "*CONTEST*" in body and "*bad*" not in body


def test_failure_never_breaks_the_store_and_never_leaks_the_token():
    s, n, sink = setup(tg, ok=False)
    pid = new_proposal(s)
    assert pid and s.get_proposal(pid)["state"] == "PENDING"
    rows = [r for r in s.audit_rows() if r["event"] == "notify.failed"]
    assert rows and "SECRET-TOKEN" not in json.dumps(s.audit_rows())


def test_uncertain_execution_alerts_and_wait_decisions_do_not():
    s, n, sink = setup(tg)
    pid = new_proposal(s); sink.reqs.clear()
    s.transition(pid, "APPROVED", by="x"); assert sink.reqs == []
    s.audit("executor", "execute.uncertain", {"proposal_id": pid})
    s.transition(pid, "UNCERTAIN", result={"uncertain": True})
    assert len(sink.reqs) == 1 and "result unknown" in json.loads(sink.reqs[0].content)["text"].lower()
    s2, n2, sink2 = setup(tg)
    s2.create_proposal("PP-R-ABC-1", "u2#e", {"action": "wait", "executable": False})
    assert sink2.reqs == []


def test_deadline_warning_once_within_window_and_not_outside():
    s, n, sink = setup(tg, hours=48)
    pid = new_proposal(s); sink.reqs.clear()
    assert n.check_deadlines() == 1 and n.check_deadlines() == 1       # queued twice, delivered once
    assert len(sink.reqs) == 1 and "deadline" in json.loads(sink.reqs[0].content)["text"].lower()
    s3, n3, sink3 = setup(tg, hours=48); new_proposal(s3); sink3.reqs.clear()
    assert n3.check_deadlines(now=datetime.now(timezone.utc) - timedelta(days=10)) == 0


def test_http_link_is_ignored_and_config_needs_all_fields():
    s = Store()
    assert Notifier(s, [], "http://insecure.example").public_url is None
    assert from_env(s, {"TELEGRAM_BOT_TOKEN": "x"}) is None
    assert from_env(s, {"TWILIO_ACCOUNT_SID": "a", "TWILIO_AUTH_TOKEN": "b"}) is None


def test_risk_flagged_alert_names_signals_but_never_buyer_identifiers():
    s, n, sink = setup(tg)
    new_proposal(s, risk={"level": "high", "basis": "x", "signals": [
        {"id": "REPEAT_BUYER", "severity": "high", "title": "Repeat disputer", "detail": "buyer sb-xdx@personal.example.com"},
        {"id": "NO_MERCHANT_CONTACT", "severity": "info", "title": "Buyer did not contact you first", "detail": "d"}]})
    t = json.loads(sink.reqs[0].content)["text"]
    assert "(risk flagged)" in t and "<b>HIGH</b>" in t and "Repeat disputer" in t
    assert "Buyer did not contact" not in t and "personal.example.com" not in t     # info signals and details stay off the chat


def test_no_risk_line_when_risk_is_low():
    s, n, sink = setup(tg)
    new_proposal(s, risk={"level": "low", "signals": [], "basis": "x"})
    assert "risk" not in json.loads(sink.reqs[0].content)["text"].lower().replace("nothing is sent", "")
