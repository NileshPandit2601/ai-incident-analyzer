"""Ground the LLM output against the actual log.

* Every evidence item must cite line numbers that exist, that the model was shown, and that share
  meaningful words with the claim. Otherwise it is downgraded to "weak" or "unverified".
* Numbers the model mentions that never appear in the log are flagged as possible hallucinations.
* The LLM severity is compared with the independent rule-based severity.
* A "high" confidence cause backed by poorly grounded evidence is downgraded to "medium".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from analyzer.parser import ParsedLog
from analyzer.schema import LLMAnalysis
from analyzer.severity import RuleSeverity, severity_index

_STOP = set(
    "that this with from were was have has been after before when then than into over under about there their they "
    "them which while within without also some many more most other such only very same each both because could would "
    "should line lines logs log shows show showing indicates indicate occurred occurs multiple several repeated repeatedly "
    "error errors warning warnings message messages entry entries".split()
)
_WORD = re.compile(r"[a-z][a-z0-9_\-.]{3,}|\d{2,}")
_NUM = re.compile(r"(?<![\w.])\d[\d,.]*\d(?![\w.])|(?<![\w.])\d{2,}(?![\w.])")


def _tokens(text: str) -> set[str]:
    return {t.strip(".-") for t in _WORD.findall(text.lower())} - _STOP


@dataclass
class EvidenceCheck:
    claim: str
    cited: list[int]
    valid_lines: list[int]
    status: str  # verified | weak | unverified
    note: str = ""


@dataclass
class ValidationReport:
    evidence: list[EvidenceCheck]
    grounding_score: float | None
    llm_severity: str
    rule_severity: str
    final_severity: str
    severity_agreement: str  # agree | minor_difference | major_difference
    needs_review: bool
    confidence: str
    unsupported_numbers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def check_evidence(claim: str, cited: list[int], parsed: ParsedLog, shown: set[int]) -> EvidenceCheck:
    existing = [n for n in dict.fromkeys(cited) if parsed.line(n) is not None]
    if not existing:
        note = "no line numbers cited" if not cited else f"cited line(s) {cited} do not exist in the log"
        return EvidenceCheck(claim, cited, [], "unverified", note)

    seen = [n for n in existing if n in shown]
    if not seen:
        return EvidenceCheck(claim, cited, existing, "weak", "cited lines were not part of what the model was shown")

    # Each cited line must support the claim on its own (stack-trace continuation lines count as part of the record).
    claim_tokens = _tokens(claim)
    needed = 1 if len(claim_tokens) < 4 else 2
    matching = []
    for n in seen:
        e = parsed.entry_for_line(n)
        text = e.raw + "\n" + "\n".join(e.continuation) if e else parsed.line(n)
        if len(claim_tokens & _tokens(text)) >= min(needed, len(claim_tokens) or 1):
            matching.append(n)
    if not matching:
        return EvidenceCheck(claim, cited, seen, "weak", "claim shares too few key terms with the cited lines")
    dropped = [n for n in cited if n not in matching]
    note = f"citations {dropped} do not match the claim and were ignored" if dropped else ""
    status = "verified" if len(matching) * 2 >= len(seen) else "weak"
    return EvidenceCheck(claim, cited, matching, status, note)


def find_unsupported_numbers(analysis: LLMAnalysis, parsed: ParsedLog) -> list[str]:
    """Numbers stated in evidence/cause that never occur anywhere in the log text."""
    log_text = "\n".join(parsed.lines)
    log_nums = {n.replace(",", "") for n in _NUM.findall(log_text)}
    texts = [e.claim for e in analysis.evidence] + [analysis.likely_cause.statement, analysis.impact]
    bad = []
    for t in texts:
        for n in _NUM.findall(t):
            k = n.replace(",", "")
            if k not in log_nums and k not in log_text and n not in bad:
                bad.append(n)
    return bad


def validate(analysis: LLMAnalysis, parsed: ParsedLog, rule: RuleSeverity, shown_lines: list[int]) -> ValidationReport:
    shown = set(shown_lines)
    checks = [check_evidence(e.claim, e.line_numbers, parsed, shown) for e in analysis.evidence]
    verified = sum(c.status == "verified" for c in checks)
    grounding = verified / len(checks) if checks else None
    notes: list[str] = []

    if not checks:
        notes.append("The model gave no evidence items, so its conclusions are not backed by cited log lines.")
    elif grounding is not None and grounding < 1:
        notes.append(f"{len(checks) - verified} of {len(checks)} evidence claims could not be fully verified against the log.")

    llm_sev, rule_sev = analysis.severity.level, rule.level
    diff = abs(severity_index(llm_sev) - severity_index(rule_sev))
    agreement = ("agree", "minor_difference")[diff] if diff < 2 else "major_difference"
    needs_review = diff >= 2
    if diff == 1:
        notes.append(f"AI severity ({llm_sev}) differs by one level from the rule-based baseline ({rule_sev}).")
    elif diff >= 2:
        notes.append(f"AI severity ({llm_sev}) and rule-based severity ({rule_sev}) disagree strongly: human review needed.")

    confidence = analysis.likely_cause.confidence
    if confidence == "high" and (grounding is None or grounding < 0.6):
        confidence = "medium"
        notes.append("Cause confidence lowered from high to medium because supporting evidence is weakly grounded.")

    unsupported = find_unsupported_numbers(analysis, parsed)
    if unsupported:
        notes.append("Numbers not found anywhere in the log (possible hallucination): " + ", ".join(unsupported[:8]))

    return ValidationReport(
        evidence=checks,
        grounding_score=grounding,
        llm_severity=llm_sev,
        rule_severity=rule_sev,
        final_severity=llm_sev,  # the AI's contextual judgement is reported; disagreements are surfaced, not hidden
        severity_agreement=agreement,
        needs_review=needs_review or (grounding is not None and grounding < 0.5),
        confidence=confidence,
        unsupported_numbers=unsupported,
        notes=notes,
    )
