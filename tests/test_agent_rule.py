import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.agent_rule import (
    AIRecommendation,
    AIRuleDraft,
    AIRuleSource,
    ai_recommend_action,
    draft_rule_from_policy,
    rule_draft_summary,
    validate_ai_recommendation,
)
from app.models import parse_dispute
from app.rules import Action, Decision, MerchantEvidence, Policy, decide

FX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)


class FakeLLM:
    name = "fake-llm"

    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.calls = []

    def complete(self, system, user, max_tokens=600):
        self.calls.append((system, user))
        o = self.outputs.pop(0)
        if isinstance(o, Exception):
            raise o
        return o


def dispute(claim="didnt get the goods", reason="MERCHANDISE_OR_SERVICE_NOT_RECEIVED"):
    b = json.loads((FX / "inquiry_escalated.json").read_text())["body"]
    b["reason"] = reason
    b["messages"] = [{"posted_by": "BUYER", "content": claim}]
    b["seller_response_due_date"] = "2099-01-01T00:00:00Z"
    b["links"] = [
        {"rel": "provide_evidence", "method": "POST", "href": "h/provide-evidence"},
        {"rel": "accept_claim", "method": "POST", "href": "h/accept-claim"},
    ]
    return parse_dispute(b)


# ---------------------------------------------------------------- draft_rule_from_policy

def test_draft_rule_from_policy_good_output():
    llm = FakeLLM(
        '{"rule_id": "R40_NEW", "reason_codes": ["MERCHANDISE_OR_SERVICE_NOT_RECEIVED"], '
        '"condition": "carrier-confirmed delivery on file", "action": "contest", '
        '"evidence_required": ["PROOF_OF_FULFILLMENT"], "confidence": 0.9, "notes": "new rule"}'
    )
    d = draft_rule_from_policy(llm, "test policy note")
    assert d is not None
    assert d.rule_id == "R40_NEW"
    assert d.reason_codes == ("MERCHANDISE_OR_SERVICE_NOT_RECEIVED",)
    assert d.action == Action.CONTEST
    assert d.evidence_required == ("PROOF_OF_FULFILLMENT",)
    assert d.confidence == 0.9
    assert d.approved is False
    assert d.source == AIRuleSource.LLM_DRAFT


def test_draft_rule_from_policy_defaults():
    llm = FakeLLM('{"rule_id": "R99", "action": "review"}')
    d = draft_rule_from_policy(llm, "x")
    assert d.action == Action.REVIEW
    assert d.confidence == 0.5
    assert d.evidence_required == ()
    assert d.reason_codes == ()


def test_draft_rule_from_policy_malformed_returns_none():
    llm = FakeLLM("not json at all")
    assert draft_rule_from_policy(llm, "x") is None


def test_draft_rule_from_policy_exception_returns_none():
    llm = FakeLLM(RuntimeError("model down"))
    assert draft_rule_from_policy(llm, "x") is None



# ---------------------------------------------------------------- ai_recommend_action

def test_ai_recommend_action_good_output():
    llm = FakeLLM(
        '{"recommended_action": "contest", "recommended_rule_id": "R10_INR_PROVEN", '
        '"reasons": ["delivery confirmed"], "evidence_plan": ["PROOF_OF_FULFILLMENT"], '
        '"confidence": 0.95}'
    )
    d = dispute()
    ev = MerchantEvidence(tracking_numbers=("1Z",), delivery_confirmed=True)
    rec = ai_recommend_action(d, ev, llm=llm)
    assert rec is not None
    assert rec.dispute_id == d.id
    assert rec.recommended_action == Action.CONTEST
    assert rec.recommended_rule_id == "R10_INR_PROVEN"
    assert rec.reasons == ("delivery confirmed",)
    assert rec.evidence_plan == ("PROOF_OF_FULFILLMENT",)
    assert rec.confidence == 0.95
    assert rec.model == "fake-llm"
    assert not hasattr(rec, "raw_model_output")  # verbatim model output is deliberately not kept


def test_ai_recommend_action_no_model_returns_none():
    d = dispute()
    ev = MerchantEvidence()
    assert ai_recommend_action(d, ev, llm=None) is None


def test_ai_recommend_action_malformed_returns_none():
    llm = FakeLLM("no json here")
    d = dispute()
    ev = MerchantEvidence()
    assert ai_recommend_action(d, ev, llm=llm) is None


def test_ai_recommend_action_exception_returns_none():
    llm = FakeLLM(RuntimeError("down"))
    d = dispute()
    ev = MerchantEvidence()
    assert ai_recommend_action(d, ev, llm=llm) is None


def test_ai_recommend_action_clamps_confidence():
    llm = FakeLLM('{"recommended_action": "accept", "confidence": 1.5}')


# ---------------------------------------------------------------- validate_ai_recommendation

def test_validate_ai_recommendation_matches_policy():
    d = dispute()
    ev = MerchantEvidence(tracking_numbers=("1Z",), delivery_confirmed=True)
    dec = decide(d, ev, now=NOW)
    rec = AIRecommendation(
        dispute_id=d.id,
        recommended_action=dec.action,
        recommended_rule_id=dec.rule_id,
        reasons=tuple(dec.reasons),
        evidence_plan=(),
        confidence=0.5,
        model="test",
    )
    ok, problems = validate_ai_recommendation(rec, d, ev, now=NOW)
    assert ok is True
    assert problems == ()


def test_validate_ai_recommendation_action_mismatch():
    d = dispute()
    ev = MerchantEvidence()  # no proof -> accept
    dec = decide(d, ev, now=NOW)
    assert dec.action == Action.ACCEPT
    rec = AIRecommendation(
        dispute_id=d.id,
        recommended_action=Action.CONTEST,  # disagrees with policy
        recommended_rule_id=dec.rule_id,
        reasons=(),
        evidence_plan=(),
        confidence=0.5,
        model="test",
    )
    ok, problems = validate_ai_recommendation(rec, d, ev, now=NOW)
    assert ok is False
    assert any("recommends contest" in p for p in problems)


def test_validate_ai_recommendation_rule_id_mismatch():
    d = dispute()
    ev = MerchantEvidence(tracking_numbers=("1Z",), delivery_confirmed=True)
    dec = decide(d, ev, now=NOW)
    rec = AIRecommendation(
        dispute_id=d.id,
        recommended_action=dec.action,
        recommended_rule_id="R99_WRONG",
        reasons=tuple(dec.reasons),
        evidence_plan=(),
        confidence=0.5,
        model="test",
    )
    ok, problems = validate_ai_recommendation(rec, d, ev, now=NOW)
    assert ok is False
    assert any("R99_WRONG" in p for p in problems)


# ---------------------------------------------------------------- rule_draft_summary

def test_rule_draft_summary_unapproved():
    d = AIRuleDraft(
        rule_id="R1", reason_codes=("X",), condition="c", action=Action.ACCEPT,
        evidence_required=(), confidence=0.5, source=AIRuleSource.LLM_DRAFT,
        approved=False,
    )
    s = rule_draft_summary(d)
    assert "[PENDING_REVIEW]" in s


def test_rule_draft_summary_approved():
    d = AIRuleDraft(
        rule_id="R1", reason_codes=("X",), condition="c", action=Action.ACCEPT,
        evidence_required=(), confidence=0.5, source=AIRuleSource.LLM_DRAFT,
        approved=True,
    )
    s = rule_draft_summary(d)
    assert "[APPROVED]" in s



def test_validate_ai_recommendation_high_confidence_not_executable():
    # INR with digital goods + access log -> CONTEST via provide_evidence,
    # but PayPal only offers accept_claim (not provide_evidence) -> not executable.
    from app.models import Dispute as D2
    d = dispute()
    d = D2(
        id=d.id, reason=d.reason, status=d.status, state=d.state, stage=d.stage,
        amount=d.amount, currency=d.currency, held_amount=d.held_amount,
        seller_response_due=d.seller_response_due, item_types=("DIGITAL_DOWNLOAD",),
        seller_protection=d.seller_protection, buyer_claim=d.buyer_claim,
        seller_transaction_id=d.seller_transaction_id, submitted=d.submitted,
        requested_types=d.requested_types, offered=frozenset({"accept_claim"}),
        links=d.links, outcome_code=d.outcome_code, outcome_reason=d.outcome_reason,
        released_amount=d.released_amount,
    )
    ev = MerchantEvidence(digital_access_log=True)
    dec2 = decide(d, ev, now=NOW)
    assert dec2.action == Action.CONTEST
    assert not dec2.executable
    rec = AIRecommendation(
        dispute_id=d.id,
        recommended_action=dec2.action,
        recommended_rule_id=dec2.rule_id,
        reasons=(),
        evidence_plan=(),
        confidence=0.95,
        model="test",
    )
    ok, problems = validate_ai_recommendation(rec, d, ev, now=NOW)
    assert ok is False
    assert any("highly confident" in p for p in problems)


def test_ai_recommend_action_negative_confidence_clamped():
    llm = FakeLLM('{"recommended_action": "accept", "confidence": -0.5}')
    d = dispute()
    ev = MerchantEvidence()
    rec = ai_recommend_action(d, ev, llm=llm)
    assert rec is not None
    assert rec.confidence == 0.0





def test_llm_failure_log_names_the_provider_error_but_never_the_request(caplog):
    import httpx
    from app.agent_rule import log_llm_failure
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions", headers={"Authorization": "Bearer SECRETKEY"})
    resp = httpx.Response(404, json={"error": {"message": "The model `claude-haiku-5-5` does not exist"}}, request=req)
    with caplog.at_level("WARNING", logger="dispute_copilot.ai"):
        log_llm_failure("analyze", httpx.HTTPStatusError("x", request=req, response=resp))
    assert "HTTP 404" in caplog.text and "does not exist" in caplog.text and "SECRETKEY" not in caplog.text
