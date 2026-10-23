"""Deterministic pattern detection over parsed log entries.

Everything here is computed by code, never by the LLM, so counts, times and line numbers are facts.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from analyzer.parser import LEVEL_ORDER, PROBLEM_LEVELS, LogEntry, ParsedLog

# ---------------------------------------------------------------------------
# Signature normalization: "Timeout for order 88122" and "... order 88123" -> one signature.
# ---------------------------------------------------------------------------
_PROTECT = [
    re.compile(r"\bHTTP[ /]?\d(?:\.\d)?\s+\d{3}\b|\bHTTP \d{3}\b", re.I),
    re.compile(r"\bstatus(?:code)?\s*[:=]?\s*\d{3}\b", re.I),
    re.compile(r"\bexit code \d+\b", re.I),
]
_SUBS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<*>"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b"), "<IP>"),
    (re.compile(r"https?://\S+"), "<URL>"),
    (re.compile(r"[\w.\-]*\d[\w.\-]*"), "<*>"),
    (re.compile(r"(?:<\*>[\s,:/]*){2,}"), "<*> "),
]


def signature_of(message: str) -> str:
    protected: list[str] = []

    # Placeholders must not contain digits, otherwise the number substitution below eats them.
    def _stash(m: re.Match) -> str:
        if len(protected) >= 26:
            return m.group(0)
        protected.append(m.group(0))
        return f"\x00{chr(ord('A') + len(protected) - 1)}\x00"

    sig = message
    for p in _PROTECT:
        sig = p.sub(_stash, sig)
    for p, repl in _SUBS:
        sig = p.sub(repl, sig)
    sig = re.sub(r"\x00([A-Z])\x00", lambda m: protected[ord(m.group(1)) - ord("A")], sig)
    sig = re.sub(r"\s+", " ", sig).strip()
    return sig[:220]


# ---------------------------------------------------------------------------
# Keyword categories (used by severity rules and given to the LLM as hints)
# ---------------------------------------------------------------------------
CATEGORIES: dict[str, re.Pattern] = {
    "timeout": re.compile(r"timed? ?out|timeout|deadline exceeded", re.I),
    "database": re.compile(r"jdbc|connection pool|hikari|connection is not available|max_connections|\bdb\b|database|sql|postgres|mysql|mongo|deadlock", re.I),
    "network_connection": re.compile(r"connection (refused|reset)|econnrefused|econnreset|broken pipe|unreachable|dns", re.I),
    "memory": re.compile(r"outofmemory|oomkill|out of memory|heap|\bgc pause|memory", re.I),
    "http_5xx": re.compile(r"\b(HTTP|status(?:code)?|returned|returning)\W*5\d\d\b|\b5\d\d (Bad Gateway|Service Unavailable|Gateway Timeout|Internal Server Error)", re.I),
    "auth": re.compile(r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|\bjwt\b|token|authenticat|access ?denied|credential", re.I),
    "disk_storage": re.compile(r"\bdisk\b|no space left|inode|volume|storage", re.I),
    "tls_certificate": re.compile(r"\bssl\b|\btls\b|certificate|x509", re.I),
    "crash_restart": re.compile(r"crash|restart|oomkilled|exit code|liveness probe|readiness probe|panic|segfault|terminated", re.I),
    "circuit_breaker": re.compile(r"circuit ?breaker", re.I),
    "latency": re.compile(r"\bslow\b|latency|p9\d|gc pause|exceeds slo", re.I),
    "code_exception": re.compile(r"keyerror|nullpointer|null reference|valueerror|typeerror|attributeerror|indexerror|unhandled exception|traceback", re.I),
}

CRITICAL_SIGNALS = re.compile(
    r"oomkilled|health check failed|\bdb=down\b|service (down|unavailable)|circuit ?breaker.*\bopen\b|"
    r"\bfatal\b|panic|data (loss|corruption)|crashloop|cluster down|all replicas",
    re.I,
)
CUSTOMER_FACING = re.compile(r"checkout|payment|\blogin\b|\bcart\b|\border\b|orders|signup|customer|/api/", re.I)
RECOVERY = re.compile(r"recovered|resolved|back to normal|\(after \d+ retr|succeeded after|retry succeeded|health check ok", re.I)
RESOURCE_PCT = re.compile(r"(disk|heap|memory|pool|usage|cpu)[^%\n]{0,40}?\b(\d{2,3})%", re.I)
CHANGE_EVENT = re.compile(
    r"deploy|rolled out|rollout|release|\bversion\b|rotation|rotat|config(uration)? (change|reload)|migration|"
    r"starting .*job|job start|\bimport\b|scaled|upgrade|feature flag|cron",
    re.I,
)


@dataclass
class SignatureGroup:
    signature: str
    level: str
    count: int
    services: list[str]
    first_seen: datetime | None
    last_seen: datetime | None
    line_numbers: list[int]
    categories: list[str]
    example: str

    @property
    def id(self) -> str:  # stable short label for UI/prompt
        return f"L{self.line_numbers[0]}"


@dataclass
class PatternReport:
    total_entries: int
    level_counts: dict[str, int]
    services: dict[str, int]
    problem_services: dict[str, int]
    start: datetime | None
    end: datetime | None
    groups: list[SignatureGroup]  # problem (WARN+) signatures, ordered by first occurrence
    category_counts: dict[str, int]
    category_lines: dict[str, list[int]]
    max_errors_per_minute: int
    peak_minute: datetime | None
    error_burst_lines: list[int]
    errors_ongoing_at_end: bool
    recovery_lines: list[int]
    critical_signal_lines: list[int]
    customer_facing_lines: list[int]
    resource_peaks: dict[str, int]
    change_event_lines: list[int]
    first_problem_line: int | None
    timeline: list[dict] = field(default_factory=list)

    @property
    def duration(self) -> timedelta | None:
        if self.start and self.end:
            return self.end - self.start
        return None

    @property
    def error_count(self) -> int:
        return self.level_counts.get("ERROR", 0) + self.level_counts.get("CRITICAL", 0)

    @property
    def warn_count(self) -> int:
        return self.level_counts.get("WARN", 0)


def _max_in_window(times: list[datetime], window: timedelta) -> tuple[int, int]:
    """Largest number of events in any sliding window. Returns (count, start_index)."""
    best, best_i, j = 0, 0, 0
    for i in range(len(times)):
        while times[i] - times[j] > window:
            j += 1
        if i - j + 1 > best:
            best, best_i = i - j + 1, j
    return best, best_i


def _timeline(entries: list[LogEntry], start: datetime | None, end: datetime | None) -> list[dict]:
    timed = [e for e in entries if e.timestamp]
    if not timed or not start or not end:
        return []
    span = max((end - start).total_seconds(), 1)
    # Aim for roughly 30-60 buckets regardless of log duration.
    bucket = next((b for b in (10, 30, 60, 300, 600, 1800, 3600, 21600, 86400) if span / b <= 60), 86400)
    counts: Counter = Counter()
    for e in timed:
        offset = int((e.timestamp - start).total_seconds() // bucket) * bucket
        counts[(start + timedelta(seconds=offset), e.level)] += 1
    return [{"time": t, "level": lvl, "count": c} for (t, lvl), c in sorted(counts.items())]


def analyze_patterns(parsed: ParsedLog) -> PatternReport:
    entries = parsed.entries
    level_counts = Counter(e.level for e in entries)
    services = Counter(e.service for e in entries if e.service)
    problem_services = Counter(e.service for e in entries if e.service and e.level in ("ERROR", "CRITICAL"))

    times = sorted(e.timestamp for e in entries if e.timestamp)
    start, end = (times[0], times[-1]) if times else (None, None)

    # --- signature grouping (WARN and above) -------------------------------------------------
    groups: dict[tuple[str, str], dict] = {}
    for e in entries:
        if not e.is_problem:
            continue
        key = (e.level, signature_of(e.message))
        g = groups.setdefault(key, {"entries": []})
        g["entries"].append(e)

    sig_groups = []
    for (level, sig), g in groups.items():
        es: list[LogEntry] = g["entries"]
        ts = [e.timestamp for e in es if e.timestamp]
        text = " ".join(e.full_text for e in es[:5])
        sig_groups.append(
            SignatureGroup(
                signature=sig,
                level=level,
                count=len(es),
                services=sorted({e.service for e in es if e.service}),
                first_seen=min(ts) if ts else None,
                last_seen=max(ts) if ts else None,
                line_numbers=[e.line_no for e in es],
                categories=[c for c, p in CATEGORIES.items() if p.search(text)],
                example=es[0].raw,
            )
        )
    sig_groups.sort(key=lambda g: g.line_numbers[0])

    # --- categories ----------------------------------------------------------------------------
    category_lines: dict[str, list[int]] = {}
    for e in entries:
        if not e.is_problem:
            continue
        for c, p in CATEGORIES.items():
            if p.search(e.full_text):
                category_lines.setdefault(c, []).append(e.line_no)
    category_counts = {c: len(v) for c, v in sorted(category_lines.items(), key=lambda kv: -len(kv[1]))}

    # --- bursts / rate ----------------------------------------------------------------------------
    err_entries = sorted((e for e in entries if e.level in ("ERROR", "CRITICAL") and e.timestamp), key=lambda e: e.timestamp)
    err_times = [e.timestamp for e in err_entries]
    max_per_min, burst_start = _max_in_window(err_times, timedelta(seconds=60)) if err_times else (0, 0)
    peak_minute = err_times[burst_start] if err_times else None
    burst_lines = [e.line_no for e in err_entries[burst_start: burst_start + max_per_min]] if err_times else []

    # --- still failing at the end? ----------------------------------------------------------------
    ongoing = False
    errors_all = [e for e in entries if e.level in ("ERROR", "CRITICAL")]
    if errors_all:
        if start and end and err_times and end > start:
            ongoing = err_times[-1] >= end - (end - start) * 0.15
        else:
            ongoing = any(e.level in ("ERROR", "CRITICAL") for e in entries[-max(3, len(entries) // 10):])

    last_err_line = errors_all[-1].line_no if errors_all else 0
    recovery_lines = [e.line_no for e in entries if RECOVERY.search(e.full_text)]
    # Only count recovery that happens after (or around) the errors, not before them.
    recovery_lines = [n for n in recovery_lines if not errors_all or n >= errors_all[0].line_no]

    resource_peaks: dict[str, int] = {}
    for e in entries:
        for m in RESOURCE_PCT.finditer(e.message):
            k, v = m.group(1).lower(), int(m.group(2))
            if v <= 100:
                resource_peaks[k] = max(resource_peaks.get(k, 0), v)

    first_problem = next((e.line_no for e in entries if e.is_problem), None)

    return PatternReport(
        total_entries=len(entries),
        level_counts={lvl: level_counts[lvl] for lvl in LEVEL_ORDER if level_counts.get(lvl)},
        services=dict(services.most_common()),
        problem_services=dict(problem_services.most_common()),
        start=start,
        end=end,
        groups=sig_groups,
        category_counts=category_counts,
        category_lines=category_lines,
        max_errors_per_minute=max_per_min,
        peak_minute=peak_minute,
        error_burst_lines=burst_lines,
        errors_ongoing_at_end=ongoing,
        recovery_lines=recovery_lines,
        critical_signal_lines=[e.line_no for e in entries if e.level != "INFO" and CRITICAL_SIGNALS.search(e.full_text)
                               or e.level == "CRITICAL"],
        customer_facing_lines=[e.line_no for e in entries if e.level in PROBLEM_LEVELS and CUSTOMER_FACING.search(e.full_text)],
        resource_peaks=resource_peaks,
        change_event_lines=[e.line_no for e in entries if CHANGE_EVENT.search(e.message) and e.line_no <= max(last_err_line, first_problem or 0)],
        first_problem_line=first_problem,
        timeline=_timeline(entries, start, end),
    )
