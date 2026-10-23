"""Pipeline tests with a fake LLM client, so they run without Ollama."""

import json

import pytest

from analyzer.config import Settings
from analyzer.llm_client import ChatResult, OllamaError
from analyzer.pipeline import analyze
from analyzer.report import incident_markdown

LOG = """2026-10-20 14:00:10 WARN  [db] Slow query detected: duration=8421ms client=batch
2026-10-20 14:00:20 ERROR [payment] Connection is not available, request timed out after 30000ms"""

GOOD = {
    "error_summary": "Payment DB timeouts.", "patterns": ["slow query precedes timeouts"],
    "evidence": [{"claim": "Slow query 8421ms from batch", "line_numbers": [1]}],
    "likely_cause": {"statement": "Batch query holds DB connections", "confidence": "medium", "reasoning": "r"},
    "contributing_factors": [], "assumptions": ["batch job runs nightly"], "impact": "payments fail",
    "severity": {"level": "High", "reason": "payments failing"},
    "next_steps": [{"priority": 2, "action": "second", "rationale": "x"}, {"priority": 1, "action": "first", "rationale": "y"}],
    "escalation": {"required": True, "to_team": "DBA", "trigger_condition": "now"},
    "incident_summary": "Payments failing due to DB pool exhaustion.",
}


class FakeClient:
    model = "fake"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, messages, schema=None, options=None, on_token=None):
        self.calls.append({"messages": messages, "schema": schema, "options": options})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        if on_token:
            on_token(r)
        return ChatResult(r, 0.01, 100, 50)


def test_happy_path_and_step_ordering():
    client = FakeClient([json.dumps(GOOD)])
    r = analyze(LOG, settings=Settings(), client=client)
    assert r.ok and r.attempts == 1
    assert [s.action for s in r.llm.next_steps] == ["first", "second"]
    assert r.validation.grounding_score == 1.0
    opts = client.calls[0]["options"]
    assert opts["temperature"] == 0 and opts["num_ctx"] == Settings().num_ctx
    assert client.calls[0]["schema"]["type"] == "object"
    assert "L2:" in client.calls[0]["messages"][1]["content"]
    assert "Incident Summary: High" in incident_markdown(r)


def test_invalid_json_triggers_one_retry_with_error_feedback():
    client = FakeClient(["{not json", json.dumps(GOOD)])
    r = analyze(LOG, settings=Settings(), client=client)
    assert r.ok and r.attempts == 2
    assert "invalid" in client.calls[1]["messages"][-1]["content"]


def test_ollama_down_falls_back_to_rules():
    r = analyze(LOG, settings=Settings(), client=FakeClient([OllamaError("Cannot reach Ollama")]))
    assert not r.ok
    assert "Cannot reach Ollama" in r.llm_error
    assert r.severity == r.rule_severity.level
    assert "rule-based only" in incident_markdown(r)


def test_empty_input_rejected():
    with pytest.raises(ValueError):
        analyze("   ", settings=Settings(), client=FakeClient([]))


def test_large_log_is_sampled_within_budget():
    big = "\n".join(f"2026-10-20 14:{i // 60 % 60:02d}:{i % 60:02d} ERROR [svc] failure number {i} for order {i * 7}"
                    for i in range(5000))
    s = Settings()
    r = analyze(big, settings=s, use_llm=False)
    assert len(r.shown_lines) < 5000
    assert len(r.prompt) <= s.prompt_char_budget + 500
    assert any("selected lines" in w for w in r.log_warnings)
    assert r.patterns.error_count == 5000  # stats still cover every line
