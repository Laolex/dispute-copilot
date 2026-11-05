import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.docs import DocError, load_document, missing_documents
from app.executor import execute
from app.ingest import poll
from app.orders import OrderStore
from app.paypal import PayPalClient
from app.store import Store
from app.web import create_app
from tests.test_web_exec import AUTH, BASE, PP, PW, token


@pytest.fixture
def docs(tmp_path):
    d = tmp_path / "docs"; d.mkdir()
    (d / "listing.pdf").write_bytes(b"%PDF-1.4 listing")
    (d / "delivery.png").write_bytes(b"\x89PNG delivery")
    return d


def test_load_document_accepts_plain_allowed_files(docs):
    name, data, mime = load_document(docs, "listing.pdf")
    assert (name, mime) == ("listing.pdf", "application/pdf") and data.startswith(b"%PDF")


@pytest.mark.parametrize("bad", ["../secret.pdf", "/etc/passwd", "sub/a.pdf", ".hidden.pdf", "run.exe", "nope.pdf"])
def test_load_document_rejects_unsafe_or_missing(docs, bad):
    with pytest.raises(DocError):
        load_document(docs, bad)


def test_oversized_file_rejected(docs, monkeypatch):
    monkeypatch.setattr("app.docs.MAX_BYTES", 5)
    with pytest.raises(DocError, match="larger"):
        load_document(docs, "listing.pdf")


def make(docs, names, rels=("provide_evidence", "accept_claim")):
    pp = PP(rels=rels)
    inner = pp.__call__

    def handler(req):  # a documented "not as described" dispute
        r = inner(req)
        if req.method == "GET" and req.url.path.startswith("/v1/customer/disputes/") and req.url.path != "/v1/customer/disputes":
            b = json.loads(r.content)
            b["reason"] = "MERCHANDISE_OR_SERVICE_NOT_AS_DESCRIBED"
            return httpx.Response(200, json=b)
        return r

    c = PayPalClient(Settings("i", "s", BASE), transport=httpx.MockTransport(handler), backoff=0)
    s = Store()
    o = OrderStore(data={"76X00088A7532581F": {"documents": names}})
    return pp, c, s, o


def test_evidence_upload_attaches_each_real_file(docs):
    pp, c, s, o = make(docs, ["listing.pdf", "delivery.png"])
    pid = poll(c, s, o, docs_dir=docs)[0]
    assert s.get_proposal(pid)["decision"]["documents"] == ["listing.pdf", "delivery.png"]
    assert s.get_proposal(pid)["decision"]["missing_documents"] == []
    s.transition(pid, "APPROVED", by="x")
    assert execute(c, s, BASE, pid, docs, o)["ok"] is True
    body = pp.posts[0][1]
    assert body.count(b'name="evidence-file"') == 2 and b'filename="listing.pdf"' in body
    assert b"%PDF-1.4 listing" in body and b"image/png" in body and b'name="input"' in body


def test_missing_file_blocks_approval_and_sends_nothing(docs):
    pp, c, s, o = make(docs, ["listing.pdf", "ghost.pdf"])
    pid = poll(c, s, o, docs_dir=docs)[0]
    assert s.get_proposal(pid)["decision"]["missing_documents"]
    client = TestClient(create_app(c, s, o, BASE, PW, docs_dir=docs))
    page = client.get(f"/proposals/{pid}", auth=AUTH).text
    assert "Cannot approve" in page and "ghost.pdf" in page and "Approve and" not in page
    # even if someone forces execution, the executor refuses before any POST
    s.transition(pid, "APPROVED", by="x")
    res = execute(c, s, BASE, pid, docs, o)
    assert res["ok"] is False and "ghost.pdf" in res["reason"] and pp.posts == []


def test_traversal_name_in_order_store_cannot_read_outside_docs(docs, tmp_path):
    (tmp_path / "secret.pdf").write_bytes(b"%PDF secret")
    pp, c, s, o = make(docs, ["../secret.pdf"])
    pid = poll(c, s, o, docs_dir=docs)[0]
    s.transition(pid, "APPROVED", by="x")
    assert execute(c, s, BASE, pid, docs, o)["ok"] is False
    assert pp.posts == [] and missing_documents(docs, ["../secret.pdf"])


def test_replaced_file_bytes_after_approval_block_execution(docs):
    pp, c, s, o = make(docs, ["listing.pdf"])
    pid = poll(c, s, o, docs_dir=docs)[0]
    s.transition(pid, "APPROVED", by="x")
    (docs / "listing.pdf").write_bytes(b"%PDF-1.4 a different document with the same name")
    res = execute(c, s, BASE, pid, docs, o)
    assert res["ok"] is False and "evidence" in res["reason"] and pp.posts == []


def test_edited_order_record_after_approval_blocks_execution(docs):
    pp, c, s, o = make(docs, ["listing.pdf"])
    pid = poll(c, s, o, docs_dir=docs)[0]
    s.transition(pid, "APPROVED", by="x")
    o._data["76X00088A7532581F"]["documents"] = ["listing.pdf", "delivery.png"]
    res = execute(c, s, BASE, pid, docs, o)
    assert res["ok"] is False and "evidence" in res["reason"] and pp.posts == []
