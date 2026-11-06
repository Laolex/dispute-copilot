"""Real-browser check of the AG Grid console. Skipped unless Playwright + Chromium are installed
(pip install playwright && playwright install chromium). Hostile strings are planted where the grids display
data; they must render as text, execute nothing, and trip no console error or CSP violation."""
import json
import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
import uvicorn

sync_api = pytest.importorskip("playwright.sync_api")

from app.config import Settings  # noqa: E402
from app.orders import OrderStore  # noqa: E402
from app.paypal import PayPalClient  # noqa: E402
from app.store import Store  # noqa: E402
from app.web import create_app  # noqa: E402

BASE = "https://api-m.sandbox.paypal.com"
XSS = "<img src=x onerror=window.__xss=1>"
FX = Path(__file__).parent / "fixtures" / "inquiry_escalated.json"


def _seed(store: Store) -> None:
    base = json.loads(FX.read_text())["body"]
    now = datetime.now(timezone.utc)

    def add(did, amount, due_days, action, rel, state, risk=None):
        b = json.loads(json.dumps(base))
        b.update(dispute_id=did, update_time=now.isoformat(), dispute_amount={"currency_code": "USD", "value": amount},
                 seller_response_due_date=(now + timedelta(days=due_days)).isoformat().replace("+00:00", "Z"))
        store.upsert_dispute(b)
        dec = {"action": action, "rule_id": "R", "reasons": ["r"], "link_rel": rel, "executable": True,
               "evidence_plan": [], "documents": [], "requires_human": True}
        if risk:
            dec["risk"] = {"level": risk, "signals": [], "basis": "b"}
        pid = store.create_proposal(did, "k" + did, dec)
        if state == "EXECUTED":
            store.transition(pid, "APPROVED", by="t"); store.transition(pid, "EXECUTED", result={"ok": True})

    add("PP-R-AAA-1", "25.00", 9, "contest", "provide_evidence", "EXECUTED")
    add("PP-R-BBB-2", "40.00", 2, "contest", "escalate", "PENDING", risk="high")
    add("PP-R-CCC-3", "12.00", 6, "accept", "accept_claim", "PENDING")
    add(XSS, "9.99", 4, "accept", "accept_claim", "PENDING")
    store.audit("webhook", "webhook.rejected", {"reason": "signature verification failed", "note": XSS})


@pytest.fixture(scope="module")
def server():
    store = Store(); _seed(store)
    c = PayPalClient(Settings("i", "s", BASE), transport=httpx.MockTransport(lambda r: httpx.Response(500)), backoff=0)
    app = create_app(c, store, OrderStore(data={}), BASE, "pw")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    t = threading.Thread(target=srv.run, daemon=True); t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True; t.join(timeout=5)


@pytest.fixture
def page(server):
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch()
        except Exception as ex:  # noqa: BLE001
            pytest.skip(f"Chromium not available: {ex}")
        ctx = browser.new_context(http_credentials={"username": "u", "password": "pw"})
        pg = ctx.new_page()
        pg.problems = []
        pg.on("console", lambda m: pg.problems.append(m.text) if m.type in ("error", "warning") else None)
        pg.on("pageerror", lambda e: pg.problems.append(str(e)))
        yield pg
        browser.close()


def test_proposals_grid_renders_filters_and_stays_inert(page, server):
    page.goto(server + "/"); page.wait_for_selector(".ag-row")
    assert page.locator(".ag-row").count() == 4
    assert page.locator("#tiles .stat").count() == 4
    page.fill("#q", "escalate")
    sync_api.expect(page.locator(".ag-row")).to_have_count(1)        # waits out the row animation; searches the PayPal action
    assert page.evaluate("window.__xss === 1") is False
    assert page.evaluate("document.querySelectorAll('img[src=x]').length") == 0
    assert page.problems == []


def test_audit_grid_shows_hostile_payload_as_text_and_chain_status(page, server):
    page.goto(server + "/audit"); page.wait_for_selector(".ag-row")
    page.locator(".ag-row", has_text="webhook.rejected").first.click(); page.wait_for_timeout(200)
    assert XSS in page.locator("#detail").inner_text()               # shown literally, as text
    assert "hash chain verified" in page.locator("#count").inner_text()
    assert page.evaluate("window.__xss === 1") is False
    assert page.evaluate("document.querySelectorAll('img[src=x]').length") == 0
    assert page.problems == []


def test_quick_views_filter_the_queue_and_choice_survives_reload_state(page, server):
    page.goto(server + "/"); page.wait_for_selector(".ag-row")
    page.get_by_role("button", name="Awaiting approval").click()
    sync_api.expect(page.locator(".ag-row")).to_have_count(3)          # 3 of the 4 seeded proposals are PENDING
    assert page.locator("#shown").inner_text() == "3 of 4 rows"
    page.get_by_role("button", name="All", exact=True).click()
    sync_api.expect(page.locator(".ag-row")).to_have_count(4)
    # saved grid state: a typed column filter is remembered after a reload (per-viewer localStorage)
    page.fill("#q", "")
    page.locator(".ag-header-cell[col-id='dispute_id'] input").first.fill("BBB")
    sync_api.expect(page.locator(".ag-row")).to_have_count(1)
    page.reload(); page.wait_for_selector(".ag-row")
    sync_api.expect(page.locator(".ag-row")).to_have_count(1)
    assert page.problems == []


def test_csv_export_neutralises_formula_injection(page, server):
    page.goto(server + "/audit"); page.wait_for_selector(".ag-row")
    with page.expect_download() as dl:
        page.click("#export")
    text = open(dl.value.path(), encoding="utf-8").read()
    assert "webhook.rejected" in text
    for line in text.splitlines():
        for cell in line.split(","):
            assert not cell.strip('"').startswith(("=", "+", "@")), line


def test_risk_column_and_chip(page, server):
    page.goto(server + "/"); page.wait_for_selector(".ag-row")
    assert page.locator(".ag-row", has_text="High risk").count() == 1
    page.get_by_role("button", name="Risk flagged").click()
    sync_api.expect(page.locator(".ag-row")).to_have_count(1)
    assert page.problems == []
