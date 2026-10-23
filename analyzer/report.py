"""Render a shareable Markdown incident report."""

from __future__ import annotations

from datetime import datetime

from analyzer.pipeline import AnalysisResult


def _t(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "n/a"


def incident_markdown(r: AnalysisResult, source_name: str = "pasted logs") -> str:
    p, v, a = r.patterns, r.validation, r.llm
    out = [
        f"# Incident Summary: {r.severity}",
        "",
        f"*Generated {datetime.now():%Y-%m-%d %H:%M} by AI Incident Analyzer ({r.model}, local Ollama). "
        "AI-assisted analysis: verify before acting.*",
        "",
        f"**Source:** {source_name} · **Log window:** {_t(p.start)} → {_t(p.end)} · "
        f"**Records:** {p.total_entries} ({p.error_count} errors, {p.warn_count} warnings) · "
        f"**Services with errors:** {', '.join(p.problem_services) or 'none'}",
        "",
    ]

    if a is None:
        out += ["## Severity (rule-based only, AI analysis unavailable)", f"**{r.rule_severity.level}**", ""]
        out += [f"- {x}" for x in r.rule_severity.reasons]
        out += ["", "## Top error patterns"]
        out += [f"- [{g.level}] ×{g.count}: `{g.signature}` (first at line {g.line_numbers[0]})" for g in p.groups[:10]]
        if r.llm_error:
            out += ["", f"> AI analysis failed: {r.llm_error}"]
        return "\n".join(out)

    out += ["## Summary", a.incident_summary, ""]
    out += [f"## Severity: {v.final_severity}", a.severity.reason, "",
            f"*Rule-based cross-check: {v.rule_severity} ({v.severity_agreement.replace('_', ' ')}).*", ""]
    out += ["## Likely cause (hypothesis)", f"{a.likely_cause.statement} *(confidence: {v.confidence})*", ""]
    out += ["### Evidence (from logs)"]
    for c in v.evidence:
        mark = {"verified": "✔", "weak": "~", "unverified": "✘"}[c.status]
        lines = ", ".join(map(str, c.valid_lines or c.cited)) or "none"
        out.append(f"- {mark} {c.claim} (lines {lines}){' [' + c.status + ']' if c.status != 'verified' else ''}")
    out += ["", "### Assumptions (not confirmed by logs)"]
    out += [f"- {x}" for x in a.assumptions] or ["- none stated"]
    out += ["", "## Impact", a.impact, ""]
    out += ["## Recommended next steps"]
    out += [f"{i}. **{s.action}**: {s.rationale}" for i, s in enumerate(a.next_steps, 1)]
    esc = a.escalation
    out += ["", "## Escalation",
            f"**{'Escalate now' if esc.required else 'Not required yet'}** → {esc.to_team}. Trigger: {esc.trigger_condition}", ""]
    out += ["## Key error patterns"]
    out += [f"- [{g.level}] ×{g.count} ({', '.join(g.services) or '-'}): `{g.signature}`" for g in p.groups[:8]]
    if v.notes:
        out += ["", "## Analyzer notes"] + [f"- {n}" for n in v.notes]
    return "\n".join(out)
