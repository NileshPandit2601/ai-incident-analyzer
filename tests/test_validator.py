from analyzer.parser import parse_logs
from analyzer.patterns import analyze_patterns
from analyzer.schema import LLMAnalysis
from analyzer.severity import score_severity
from analyzer.validator import validate

LOG = """2026-10-20 14:00:00 INFO  [batch] Starting reconciliation job
2026-10-20 14:00:10 WARN  [db] Slow query detected: duration=8421ms client=batch
2026-10-20 14:00:20 ERROR [payment] Connection is not available, request timed out after 30000ms
2026-10-20 14:00:30 ERROR [gateway] HTTP 503 Service Unavailable for POST /checkout"""


def make(evidence, severity="High", confidence="high", cause="Slow batch query exhausts DB connections"):
    return LLMAnalysis.model_validate({
        "error_summary": "s", "patterns": [], "evidence": evidence,
        "likely_cause": {"statement": cause, "confidence": confidence, "reasoning": "r"},
        "contributing_factors": [], "assumptions": ["pool size is 20"], "impact": "checkout failing",
        "severity": {"level": severity, "reason": "x"},
        "next_steps": [{"priority": 1, "action": "a", "rationale": "b"}],
        "escalation": {"required": True, "to_team": "DBA", "trigger_condition": "now"},
        "incident_summary": "sum",
    })


def run(analysis, shown=None):
    parsed = parse_logs(LOG)
    rule = score_severity(analyze_patterns(parsed))
    return validate(analysis, parsed, rule, shown or list(range(1, 5))), rule


def test_verified_claim():
    v, _ = run(make([{"claim": "Slow query from batch client took 8421ms", "line_numbers": [2]}]))
    assert v.evidence[0].status == "verified"
    assert v.grounding_score == 1.0


def test_nonexistent_line_is_unverified():
    v, _ = run(make([{"claim": "Database crashed", "line_numbers": [42]}]))
    assert v.evidence[0].status == "unverified"
    assert v.grounding_score == 0.0
    assert v.needs_review


def test_claim_not_matching_line_is_weak():
    v, _ = run(make([{"claim": "Kubernetes evicted pods", "line_numbers": [3]}]))
    assert v.evidence[0].status == "weak"


def test_unrelated_extra_citation_is_dropped():
    v, _ = run(make([{"claim": "Connection is not available, request timed out", "line_numbers": [3, 1]}]))
    c = v.evidence[0]
    assert c.status == "verified"
    assert c.valid_lines == [3]
    assert "[1]" in c.note


def test_line_not_shown_to_model_is_weak():
    v, _ = run(make([{"claim": "Connection is not available", "line_numbers": [3]}]), shown=[1, 2])
    assert v.evidence[0].status == "weak"


def test_high_confidence_downgraded_when_poorly_grounded():
    v, _ = run(make([{"claim": "Kubernetes evicted pods", "line_numbers": [99]}], confidence="high"))
    assert v.confidence == "medium"


def test_severity_major_disagreement_flags_review():
    v, rule = run(make([{"claim": "Connection is not available after 30000ms", "line_numbers": [3]}], severity="Low"))
    assert rule.level in ("High", "Critical")  # 503 on /checkout + DB timeouts
    assert v.severity_agreement == "major_difference"
    assert v.needs_review
    assert v.final_severity == "Low"  # AI judgement is reported, disagreement is surfaced not hidden


def test_severity_agreement():
    v, rule = run(make([{"claim": "Connection is not available after 30000ms", "line_numbers": [3]}], severity="High"))
    assert v.severity_agreement in ("agree", "minor_difference")


def test_hallucinated_number_flagged():
    v, _ = run(make([{"claim": "Connection timed out after 30000ms", "line_numbers": [3]}],
                    cause="Pool of 75 connections exhausted"))
    assert "75" in v.unsupported_numbers
    assert "30000" not in " ".join(v.unsupported_numbers)
