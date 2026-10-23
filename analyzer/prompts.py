"""Prompt construction.

The model never sees an unbounded raw log. It receives:
  1. a digest of facts computed by code (counts, times, categories, change events), and
  2. log lines labelled with their real line numbers: the whole log if it fits the budget,
     otherwise a selection (first problem + context, examples of every error pattern,
     change events such as deploys/jobs, and the tail).
Line labels let the validator check every citation the model makes.
"""

from __future__ import annotations

from analyzer.parser import ParsedLog
from analyzer.patterns import PatternReport

SYSTEM_PROMPT = """You are a senior Site Reliability Engineer helping a support engineer triage an application incident.
You are given (a) facts computed by code from the logs and (b) log lines labelled L<number>.

Rules:
- Base every conclusion on the provided logs. Cite line numbers (the number after L) for each evidence item.
- Only put things in "evidence" that are literally visible in the cited lines. Never cite a line you were not shown.
- Anything you infer but cannot see (e.g. config values, traffic levels, recent changes not logged) goes in "assumptions".
- Distinguish the root cause from downstream symptoms: errors that START FIRST are often closer to the cause.
  Then ask WHY that first problem happened: look at INFO lines just before it for a trigger such as a deploy,
  a batch job, a key/config rotation, an import, a traffic spike, or a resource steadily growing. Name the specific
  component, client, job or service shown in the logs (e.g. the client that issues the slow query), not a generic category.
- Phrase the likely cause as a hypothesis and set confidence honestly (high only if the logs show the mechanism clearly).
- Severity scale: Critical = outage or core customer flow broken now; High = major degradation or customer impact,
  or a crash that will recur; Medium = limited/partial impact or a risk that will become an outage if ignored;
  Low = minor, self-recovering, or cosmetic.
- Next steps must be concrete checks or actions an operations engineer can do, naming the specific service, job,
  query, key or resource from the logs. Order: 1) confirm the suspected cause / stop the bleeding (if a job, deploy
  or change is implicated, pausing or rolling it back is usually the fastest mitigation), 2) mitigate, 3) prevent recurrence. Avoid vague advice like "monitor performance" or "follow best practices".
- Be concise: short sentences, no repetition. Do not invent hostnames, metrics, or numbers that are not in the logs.
- Respond with JSON only, matching the requested schema."""

OUTPUT_GUIDE = """Return JSON with these fields (keep every string short):
error_summary (2-3 sentences), patterns (max 4), evidence (3-6 items of {claim, line_numbers}; quote or closely
paraphrase the cited lines), likely_cause {statement, confidence: low|medium|high, reasoning},
contributing_factors (max 3), assumptions (max 4, plain statements without line references), impact (1-2 sentences),
severity {level: Critical|High|Medium|Low, reason}, next_steps (3-5 items of {priority, action, rationale}; no generic
items such as "document the incident" or "monitor"), escalation {required, to_team, trigger_condition},
incident_summary (max 5 sentences: what happened, when, impact, likely cause, current status, next action)."""


def _fmt_time(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "n/a"


def build_digest(parsed: ParsedLog, report: PatternReport, max_groups: int = 25) -> str:
    out = ["## FACTS COMPUTED BY CODE (reliable)"]
    out.append(f"- Lines: {parsed.line_count}, log records: {report.total_entries}")
    out.append(f"- Time range: {_fmt_time(report.start)} -> {_fmt_time(report.end)} (duration {report.duration or 'unknown'})")
    out.append("- Levels: " + ", ".join(f"{k} {v}" for k, v in report.level_counts.items()))
    if report.services:
        out.append("- Services seen: " + ", ".join(f"{k} ({v})" for k, v in report.services.items()))
    if report.problem_services:
        out.append("- Services with ERRORs: " + ", ".join(f"{k} ({v})" for k, v in report.problem_services.items()))
    if report.category_counts:
        out.append("- Problem categories (keyword match, WARN+ records): "
                   + ", ".join(f"{k} ({v})" for k, v in report.category_counts.items()))
    if report.first_problem_line:
        out.append(f"- First WARN/ERROR record: L{report.first_problem_line}")
    out.append(f"- Peak error rate: {report.max_errors_per_minute} errors within 60s"
               + (f" starting {_fmt_time(report.peak_minute)}" if report.peak_minute else ""))
    out.append(f"- Errors still occurring at end of log: {'yes' if report.errors_ongoing_at_end else 'no'}")
    if report.first_problem_line:
        before = [e for e in parsed.entries if e.line_no < report.first_problem_line and e.level == "INFO"][-3:]
        if before:
            out.append("- Last INFO records before the first problem: " + ", ".join(f"L{e.line_no}" for e in before))
    if report.resource_peaks:
        out.append("- Peak resource usage mentioned: " + ", ".join(f"{k} {v}%" for k, v in report.resource_peaks.items()))
    if report.change_event_lines:
        out.append("- Possible change events (deploy/job/rotation/import) at: "
                   + ", ".join(f"L{n}" for n in report.change_event_lines[:10]))
    if report.recovery_lines:
        out.append("- Recovery / successful-retry messages at: " + ", ".join(f"L{n}" for n in report.recovery_lines[:10]))
    # The rule-based severity level is deliberately NOT given to the model, so its judgement stays an
    # independent second opinion that the validator can compare against.

    out.append("\n## WARN/ERROR PATTERNS (grouped, ordered by first occurrence)")
    for i, g in enumerate(report.groups[:max_groups], 1):
        lines = ", ".join(f"L{n}" for n in g.line_numbers[:8]) + (" ..." if len(g.line_numbers) > 8 else "")
        span = f"{_fmt_time(g.first_seen)} -> {_fmt_time(g.last_seen)}" if g.first_seen else "no timestamps"
        out.append(f"P{i}. [{g.level}] x{g.count} | services: {', '.join(g.services) or '-'} | {span} | lines: {lines}")
        out.append(f"    pattern: {g.signature}")
    if len(report.groups) > max_groups:
        out.append(f"... {len(report.groups) - max_groups} more patterns omitted")
    return "\n".join(out)


def select_lines(parsed: ParsedLog, report: PatternReport, char_budget: int) -> tuple[list[int], bool]:
    """Pick which line numbers to show the model. Returns (line_numbers, is_complete_log)."""
    total_chars = sum(len(l) + 8 for l in parsed.lines)
    if total_chars <= char_budget:
        return list(range(1, parsed.line_count + 1)), True

    chosen: list[int] = []

    def add_entry_lines(line_no: int):
        e = parsed.entry_for_line(line_no)
        rng = range(e.line_no, min(e.end_line_no, e.line_no + 6) + 1) if e else [line_no]
        chosen.extend(rng)

    # priority order: first problem + preceding context, change events, examples per pattern, outage signals, tail
    if report.first_problem_line:
        idx = next((i for i, e in enumerate(parsed.entries) if e.line_no == report.first_problem_line), 0)
        for e in parsed.entries[max(0, idx - 5): idx + 3]:
            add_entry_lines(e.line_no)
    for n in report.change_event_lines[:10]:
        add_entry_lines(n)
    for g in report.groups:
        add_entry_lines(g.line_numbers[0])
        if g.count > 1:
            add_entry_lines(g.line_numbers[-1])
    for n in report.critical_signal_lines[:10]:
        add_entry_lines(n)
    for e in parsed.entries[-5:]:
        add_entry_lines(e.line_no)

    result, used = [], 0
    for n in dict.fromkeys(chosen):  # de-dup, keep priority order
        cost = len(parsed.lines[n - 1][:400]) + 8
        if used + cost > char_budget:
            break
        result.append(n)
        used += cost
    return sorted(result), False


def build_user_prompt(parsed: ParsedLog, report: PatternReport, char_budget: int) -> tuple[str, list[int]]:
    digest = build_digest(parsed, report)
    shown, complete = select_lines(parsed, report, max(char_budget - len(digest) - len(OUTPUT_GUIDE), 2000))
    header = ("## FULL LOG (every line, labelled)" if complete else
              f"## SELECTED LOG LINES ({len(shown)} of {parsed.line_count}; chosen by code: first errors + context, "
              "change events, an example of every pattern, outage signals, end of log)")
    body, prev = [], 0
    for n in shown:
        if prev and n != prev + 1:
            body.append("   ...")
        body.append(f"L{n}: {parsed.lines[n - 1][:400]}")
        prev = n
    prompt = f"{digest}\n\n{header}\n" + "\n".join(body) + f"\n\n## TASK\nAnalyze this incident.\n{OUTPUT_GUIDE}"
    return prompt, shown
