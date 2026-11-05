import argparse
import json

import httpx
import pytest

from app.config import Settings
from app.paypal import PayPalClient
from spike import spike

BASE = "https://api-m.sandbox.paypal.com"
TOKEN = {"access_token": "T", "expires_in": 3000, "scope": "x"}


def make(handler, tmp_path):
    settings = Settings("id", "secret", BASE, str(tmp_path))
    return PayPalClient(settings, transport=httpx.MockTransport(handler), backoff=0), settings


def dispute(links=(), status="UNDER_REVIEW"):
    return {"dispute_id": "PP-D-9", "status": status, "dispute_life_cycle_stage": "INQUIRY",
            "links": [{"rel": r, "method": "POST",
                       "href": h if (h := globals().get("HREF_OVERRIDE")) else
                       f"{BASE}/v1/customer/disputes/PP-D-9/{r.replace('_', '-')}"} for r in links]}


def ns(**kw):
    return argparse.Namespace(**kw)


class Server:
    def __init__(self, links):
        self.links, self.posts, self.queries = links, [], []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        self.queries.append(dict(req.url.params))
        if req.method == "GET" and req.url.path == "/v1/customer/disputes":
            return httpx.Response(200, json={"items": []})
        if req.method == "GET":
            return httpx.Response(200, json=dispute(self.links))
        self.posts.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"links": []})


def test_require_evidence_refuses_when_link_not_offered(tmp_path, capsys):
    srv = Server(links=[])
    c, s = make(srv, tmp_path)
    rc = spike.cmd_require_evidence(c, s, ns(dispute_id="PP-D-9", party="seller", yes=True))
    assert rc == 4 and srv.posts == []
    assert "does not currently offer 'require-evidence'" in capsys.readouterr().out


def test_require_evidence_posts_seller_action_when_offered(tmp_path):
    srv = Server(links=["require_evidence"])
    c, s = make(srv, tmp_path)
    assert spike.cmd_require_evidence(c, s, ns(dispute_id="PP-D-9", party="seller", yes=True)) == 0
    assert srv.posts == [("/v1/customer/disputes/PP-D-9/require-evidence", {"action": "SELLER_EVIDENCE"})]


def test_adjudicate_maps_outcome_and_is_guarded(tmp_path):
    srv = Server(links=["adjudicate"])
    c, s = make(srv, tmp_path)
    assert spike.cmd_adjudicate(c, s, ns(dispute_id="PP-D-9", outcome="seller", yes=True)) == 0
    assert srv.posts[-1][1] == {"adjudication_outcome": "SELLER_FAVOR"}
    srv2 = Server(links=[])
    c2, s2 = make(srv2, tmp_path)
    assert spike.cmd_adjudicate(c2, s2, ns(dispute_id="PP-D-9", outcome="buyer", yes=True)) == 4
    assert srv2.posts == []


def test_foreign_host_href_is_refused(tmp_path, capsys, monkeypatch):
    srv = Server(links=["require_evidence"])
    c, s = make(srv, tmp_path)
    monkeypatch.setitem(globals(), "HREF_OVERRIDE", "https://evil.example.com/x")
    assert spike.cmd_require_evidence(c, s, ns(dispute_id="PP-D-9", party="seller", yes=True)) == 4
    assert srv.posts == [] and "outside" in capsys.readouterr().out


def test_find_link_matches_underscore_and_hyphen():
    d = {"links": [{"rel": "accept_claim", "href": "h"}]}
    assert spike.find_link(d, "accept-claim")["href"] == "h"
    assert spike.find_link(d, "accept_claim")["href"] == "h"
    assert spike.find_link(d, "make-offer") is None


def test_sandbox_actions_need_yes(tmp_path):
    c, s = make(Server(links=["require_evidence"]), tmp_path)
    with pytest.raises(SystemExit):
        spike.cmd_require_evidence(c, s, ns(dispute_id="PP-D-9", party="seller", yes=False))


def test_list_sends_explicit_time_window_by_default(tmp_path):
    srv = Server(links=[])
    c, s = make(srv, tmp_path)
    spike.cmd_list(c, s, ns(state=None, days=30, transaction_id=None, page_size=20))
    q = srv.queries[-1]
    assert q["update_time_after"].endswith("Z") and "disputed_transaction_id" not in q


def test_list_by_transaction_id_omits_time_window(tmp_path):
    srv = Server(links=[])
    c, s = make(srv, tmp_path)
    spike.cmd_list(c, s, ns(state="OPEN_INQUIRIES", days=30, transaction_id="76X00088A7532581F", page_size=10))
    q = srv.queries[-1]
    assert q["disputed_transaction_id"] == "76X00088A7532581F" and "update_time_after" not in q
    assert q["dispute_state"] == "OPEN_INQUIRIES"


def test_parser_accepts_new_commands():
    p = spike.build_parser()
    assert p.parse_args(["sandbox-require-evidence", "PP-D-9", "--yes"]).party == "seller"
    assert p.parse_args(["sandbox-adjudicate", "PP-D-9", "--outcome", "buyer", "--yes"]).outcome == "buyer"
    assert p.parse_args(["list", "--days", "7", "--transaction-id", "ABC"]).days == 7


def test_show_prints_messages_and_evidence(tmp_path, capsys):
    body = {**dispute(), "messages": [{"posted_by": "BUYER", "time_posted": "t", "content": "never arrived"}],
            "evidences": [{"evidence_type": "OTHER", "source": "SUBMITTED_BY_BUYER", "notes": "n"}]}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        return httpx.Response(200, json=body)

    c, s = make(handler, tmp_path)
    assert spike.cmd_show(c, s, ns(dispute_id="PP-D-9")) == 0
    out = capsys.readouterr().out
    assert "never arrived" in out and "SUBMITTED_BY_BUYER" in out


def test_act_posts_only_offered_link_with_body(tmp_path):
    srv = Server(links=["escalate"])
    c, s = make(srv, tmp_path)
    a = ns(dispute_id="PP-D-9", rel="escalate", body='{"note": "n"}', yes=True)
    assert spike.cmd_act(c, s, a) == 0
    assert srv.posts == [("/v1/customer/disputes/PP-D-9/escalate", {"note": "n"})]
    srv2 = Server(links=[])
    c2, s2 = make(srv2, tmp_path)
    assert spike.cmd_act(c2, s2, a) == 4 and srv2.posts == []


def test_file_probe_sends_labelled_input_part_and_image_mime(tmp_path):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        seen["body"] = req.content
        return httpx.Response(200, json={})

    c, s = make(handler, tmp_path)
    f = tmp_path / "receipt.png"
    f.write_bytes(b"\x89PNG fake")
    a = ns(dispute_id="PP-D-9", file=str(f), type="PROOF_OF_FULFILLMENT", notes="n", yes=True)
    assert spike.cmd_probe_evidence(c, s, a) == 0
    assert b"image/png" in seen["body"] and b"PROOF_OF_FULFILLMENT" in seen["body"]


def test_webhook_create_registers_dispute_events_and_prints_id(tmp_path, capsys):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json=TOKEN)
        seen["path"], seen["body"] = req.url.path, json.loads(req.content)
        return httpx.Response(201, json={"id": "WH-ID-9"})

    c, s = make(handler, tmp_path)
    assert spike.cmd_webhook_create(c, s, ns(url="https://demo.example.org/webhooks/paypal", yes=True)) == 0
    assert seen["path"] == "/v1/notifications/webhooks"
    assert {e["name"] for e in seen["body"]["event_types"]} == set(spike.WEBHOOK_EVENTS)
    assert "PAYPAL_WEBHOOK_ID=WH-ID-9" in capsys.readouterr().out


def test_webhook_create_refuses_non_https_and_needs_yes(tmp_path):
    c, s = make(lambda r: httpx.Response(200, json=TOKEN), tmp_path)
    assert spike.cmd_webhook_create(c, s, ns(url="http://localhost:8000/x", yes=True)) == 4
    with pytest.raises(SystemExit):
        spike.cmd_webhook_create(c, s, ns(url="https://a.example/x", yes=False))
