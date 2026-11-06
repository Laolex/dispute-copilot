"""One-way alerts to Telegram and/or WhatsApp (via Twilio) so a merchant sees a dispute before the deadline.

Design limits, on purpose:
  - Notifications only. Nothing here receives messages or can approve anything: approval stays in the
    authenticated web console, so a chat account takeover cannot move money.
  - Minimal content: dispute id, amount, reason, the policy's proposed action, the due date and a link.
    Buyer text and merchant evidence are never sent to a third party.
  - Every value is escaped for the target format; tokens are never logged (only the error type is).
  - A failed send never affects ingestion or execution; it is audited as notify.failed.
  - Each alert is sent at most once (claimed in the store before sending).
"""
from __future__ import annotations

import html
import json
import threading
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

TELEGRAM_HOST = "https://api.telegram.org"
TWILIO_HOST = "https://api.twilio.com"
ACTOR = "notifier"


class Telegram:
    name = "telegram"

    def __init__(self, token: str, chat_id: str, client: httpx.Client | None = None) -> None:
        self._token, self._chat, self._http = token, str(chat_id), client or httpx.Client(timeout=8.0)

    @staticmethod
    def bold(t: str) -> str:
        return f"<b>{html.escape(t, quote=False)}</b>"

    @staticmethod
    def text(t: str) -> str:
        return html.escape(t, quote=False)

    @staticmethod
    def link(label: str, url: str) -> str:
        return f'<a href="{html.escape(url, quote=True)}">{html.escape(label, quote=False)}</a>'

    def send(self, body: str) -> bool:
        r = self._http.post(f"{TELEGRAM_HOST}/bot{self._token}/sendMessage",
                            json={"chat_id": self._chat, "text": body, "parse_mode": "HTML", "disable_web_page_preview": True})
        return r.status_code == 200 and bool((r.json() or {}).get("ok"))


class WhatsApp:
    """Twilio's WhatsApp API. Business-initiated messages outside the 24-hour session need an approved template in
    production; the Twilio sandbox works for a number that has joined it."""
    name = "whatsapp"

    def __init__(self, sid: str, token: str, sender: str, to: str, client: httpx.Client | None = None) -> None:
        self._sid, self._token, self._from, self._to = sid, token, sender, to
        self._http = client or httpx.Client(timeout=8.0)

    @staticmethod
    def _clean(t: str) -> str:   # WhatsApp markup characters inside values would change the formatting
        return t.replace("*", "").replace("_", " ").replace("~", "").replace("`", "")

    def bold(self, t: str) -> str:
        return f"*{self._clean(t)}*"

    def text(self, t: str) -> str:
        return self._clean(t)

    def link(self, label: str, url: str) -> str:
        return f"{self._clean(label)}: {url}"

    def send(self, body: str) -> bool:
        r = self._http.post(f"{TWILIO_HOST}/2010-04-01/Accounts/{quote(self._sid, safe='')}/Messages.json",
                            data={"From": self._from, "To": self._to, "Body": body}, auth=(self._sid, self._token))
        return r.status_code in (200, 201)


def _money(raw: dict) -> str:
    a = raw.get("dispute_amount") or {}
    try:
        return f"{a.get('currency_code', '')} {float(a['value']):,.2f}".strip()
    except (KeyError, TypeError, ValueError):
        return "amount unknown"


def _due(raw: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw["seller_response_due_date"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


class Notifier:
    def __init__(self, store, channels: list, public_url: str | None = None, hours: int = 48, sync: bool = False) -> None:
        self.store, self.channels, self.hours, self.sync = store, channels, hours, sync
        self.public_url = public_url.rstrip("/") if public_url and public_url.startswith("https://") else None

    # ---- message building (pure) ----
    def compose(self, ch, kind: str, pid: int) -> str | None:
        p = self.store.get_proposal(pid)
        if not p:
            return None
        raw = self.store.get_dispute(p["dispute_id"]) or {}
        dec = p["decision"]
        due = _due(raw)
        lines = []
        head = {"new": "Dispute needs your decision", "uncertain": "Action result unknown: check PayPal",
                "deadline": "Response deadline approaching"}[kind]
        risk = dec.get("risk") or {}
        flagged = risk.get("level") in ("elevated", "high") and kind != "uncertain"
        if flagged:
            head += " (risk flagged)"
        lines.append(ch.bold(head))
        lines.append(f"{ch.bold(p['dispute_id'])} · {ch.text(_money(raw))}")
        if raw.get("reason"):
            lines.append(ch.text(str(raw["reason"]).replace("_", " ").title()))
        if kind == "uncertain":
            lines.append(ch.text("PayPal did not confirm the request. It may have gone through. Open the proposal and use Check status."))
        else:
            lines.append(f"Policy proposes: {ch.bold(str(dec.get('action', 'review')).upper())} ({ch.text(str(dec.get('rule_id', '')))})")
        if flagged:
            titles = [str(x.get("title", "")) for x in risk.get("signals", []) if x.get("severity") in ("warn", "high")][:3]
            lines.append(f"Risk: {ch.bold(str(risk['level']).upper())}" + (f" · {ch.text('; '.join(titles))}" if titles else ""))
        if due:
            lines.append(f"Respond by: {ch.bold(due.strftime('%d %b %Y %H:%M UTC'))}")
        if self.public_url:
            lines.append(ch.link("Open to review", f"{self.public_url}/proposals/{pid}"))
        lines.append(ch.text("Nothing is sent to PayPal until you approve it in the console."))
        return "\n".join(lines)

    # ---- sending ----
    def _deliver(self, kind: str, pid: int) -> None:
        for ch in self.channels:
            key = f"{kind}:{pid}:{ch.name}"
            if kind != "uncertain" and not self.store.claim_notification(key):
                continue
            if kind == "uncertain" and not self.store.claim_notification(f"{key}:{self._attempt(pid)}"):
                continue
            try:
                body = self.compose(ch, kind, pid)
                ok = bool(body) and ch.send(body)
                self.store.audit(ACTOR, "notify.sent" if ok else "notify.failed",
                                 {"channel": ch.name, "kind": kind, "proposal_id": pid})
            except Exception as ex:  # noqa: BLE001 - never leak the URL (it contains the bot token)
                self.store.audit(ACTOR, "notify.failed", {"channel": ch.name, "kind": kind, "proposal_id": pid,
                                                           "error": type(ex).__name__})

    def _attempt(self, pid: int) -> int:
        n = 0
        for r in self.store.audit_rows():
            if r["event"] == "execute.uncertain":
                try:
                    n += json.loads(r["payload"]).get("proposal_id") == pid
                except (ValueError, AttributeError):
                    pass
        return n

    def _run(self, kind: str, pid: int) -> None:
        if not self.channels:
            return
        if self.sync:
            self._deliver(kind, pid)
        else:
            threading.Thread(target=self._deliver, args=(kind, pid), daemon=True).start()

    def handle(self, event: str, pid: int) -> None:
        """Store listener."""
        if event == "proposal.created":
            p = self.store.get_proposal(pid)
            if p and p["decision"].get("action") in ("contest", "accept", "review"):
                self._run("new", pid)
        elif event == "proposal.uncertain":
            self._run("uncertain", pid)

    def check_deadlines(self, now: datetime | None = None) -> int:
        """Alert once for each pending proposal whose response is due within `hours`. Returns how many were queued."""
        now, n = now or datetime.now(timezone.utc), 0
        for p in self.store.list_proposals("PENDING"):
            due = _due(self.store.get_dispute(p["dispute_id"]) or {})
            if due and now <= due <= now + timedelta(hours=self.hours):
                self._run("deadline", p["id"]); n += 1
        return n

    def start_deadline_loop(self, every_seconds: int = 900) -> threading.Thread:
        stop = threading.Event()

        def loop():
            while not stop.wait(every_seconds):
                try:
                    self.check_deadlines()
                except Exception:  # noqa: BLE001
                    pass
        t = threading.Thread(target=loop, daemon=True); t.start()
        return t


def from_env(store, env) -> Notifier | None:
    channels = []
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        channels.append(Telegram(env["TELEGRAM_BOT_TOKEN"], env["TELEGRAM_CHAT_ID"]))
    if all(env.get(k) for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_WHATSAPP_FROM", "WHATSAPP_TO")):
        channels.append(WhatsApp(env["TWILIO_ACCOUNT_SID"], env["TWILIO_AUTH_TOKEN"], env["TWILIO_WHATSAPP_FROM"], env["WHATSAPP_TO"]))
    if not channels:
        return None
    try:
        hours = max(1, int(env.get("DC_NOTIFY_HOURS", "48")))
    except ValueError:
        hours = 48   # a typo in an optional setting must not stop the app from starting
    n = Notifier(store, channels, env.get("DC_PUBLIC_URL"), hours)
    store.subscribe(n.handle)
    n.start_deadline_loop()
    return n
