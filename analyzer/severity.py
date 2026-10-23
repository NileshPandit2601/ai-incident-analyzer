"""Transparent rule-based severity scoring.

This is a baseline and sanity check for the LLM, not the final word. Each rule that fires
adds or removes points and records a human-readable reason with the evidence lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from analyzer.patterns import PatternReport

SEVERITY_LEVELS = ["Low", "Medium", "High", "Critical"]
THRESHOLDS = [(80, "Critical"), (55, "High"), (30, "Medium"), (0, "Low")]


@dataclass
class RuleFinding:
    points: int
    reason: str
    lines: list[int] = field(default_factory=list)


@dataclass
class RuleSeverity:
    level: str
    score: int
    findings: list[RuleFinding]

    @property
    def reasons(self) -> list[str]:
        return [f"{'+' if f.points >= 0 else ''}{f.points}: {f.reason}" for f in self.findings]


def severity_index(level: str) -> int:
    return SEVERITY_LEVELS.index(level) if level in SEVERITY_LEVELS else 0


def score_severity(report: PatternReport) -> RuleSeverity:
    f: list[RuleFinding] = []
    errors, warns = report.error_count, report.warn_count

    if report.critical_signal_lines:
        f.append(RuleFinding(35, "Outage signals present (service/health DOWN, OOMKilled, circuit breaker OPEN, FATAL)",
                             report.critical_signal_lines[:5]))

    if errors >= 20:
        f.append(RuleFinding(25, f"High error volume ({errors} ERROR/CRITICAL entries)"))
    elif errors >= 5:
        f.append(RuleFinding(15, f"Moderate error volume ({errors} ERROR/CRITICAL entries)"))
    elif errors >= 1:
        f.append(RuleFinding(8, f"Low error volume ({errors} ERROR/CRITICAL entries)"))

    if "http_5xx" in report.category_lines:
        f.append(RuleFinding(15, "Server errors (HTTP 5xx) returned to callers", report.category_lines["http_5xx"][:5]))

    if report.customer_facing_lines:
        f.append(RuleFinding(15, "Customer-facing flow affected (checkout/payment/login/orders/API)",
                             report.customer_facing_lines[:5]))

    n_svc = len(report.problem_services)
    if n_svc >= 3:
        f.append(RuleFinding(10, f"{n_svc} services logging errors (blast radius)"))
    elif n_svc == 2:
        f.append(RuleFinding(8, "2 services logging errors"))

    if report.errors_ongoing_at_end:
        f.append(RuleFinding(10, "Errors still occurring at the end of the log (not yet recovered)"))

    if report.max_errors_per_minute >= 10:
        f.append(RuleFinding(10, f"Error burst: {report.max_errors_per_minute} errors within 60 seconds",
                             report.error_burst_lines[:5]))

    total = report.total_entries
    if total >= 10 and (errors + warns) / total >= 0.5:
        f.append(RuleFinding(10, f"{(errors + warns) * 100 // total}% of log entries are WARN or worse"))

    hot = {k: v for k, v in report.resource_peaks.items() if v >= 90}
    if hot:
        f.append(RuleFinding(10, "Resource near exhaustion: " + ", ".join(f"{k} {v}%" for k, v in hot.items())))

    if report.recovery_lines and len(report.recovery_lines) >= errors + warns // 2:
        f.append(RuleFinding(-15, "Failures were retried/recovered successfully", report.recovery_lines[:5]))

    score = max(sum(x.points for x in f), 0)
    level = next(lvl for t, lvl in THRESHOLDS if score >= t)

    # Guard rails: no errors at all can't be High/Critical; nothing WARN+ is Low.
    if errors == 0 and warns == 0:
        level = "Low"
    elif errors == 0 and severity_index(level) > severity_index("Medium"):
        level = "Medium"
        f.append(RuleFinding(0, "Capped at Medium: only warnings, no errors"))

    return RuleSeverity(level=level, score=score, findings=f)
