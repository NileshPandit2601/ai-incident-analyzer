"""Turn raw log text into structured entries.

Supports the common formats seen in application logs:
  * ``2026-10-20 14:03:09,880 ERROR [service] message``   (log4j / logback / python logging)
  * ``2026-10-20T14:03:09.880Z ERROR [service] message``  (ISO-8601)
  * ``[2026-10-20 14:03:09] [ERROR] [service] message``   (bracketed)
  * ``Oct 21 09:00:12 host service[pid]: ERROR message``  (syslog)
  * ``{"timestamp": ..., "level": ..., "message": ...}``  (JSON lines)

Lines that are not log records (stack traces, ``Caused by:``) are attached to the previous entry
as continuation lines. Unknown lines become entries with level UNKNOWN; nothing is ever dropped.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

LEVEL_ALIASES = {
    "FATAL": "CRITICAL", "CRITICAL": "CRITICAL", "CRIT": "CRITICAL", "EMERG": "CRITICAL",
    "EMERGENCY": "CRITICAL", "ALERT": "CRITICAL", "PANIC": "CRITICAL",
    "ERROR": "ERROR", "ERR": "ERROR", "SEVERE": "ERROR",
    "WARN": "WARN", "WARNING": "WARN",
    "INFO": "INFO", "NOTICE": "INFO", "INFORMATION": "INFO",
    "DEBUG": "DEBUG", "TRACE": "DEBUG", "FINE": "DEBUG",
}
LEVEL_ORDER = ["CRITICAL", "ERROR", "WARN", "INFO", "DEBUG", "UNKNOWN"]
PROBLEM_LEVELS = ("CRITICAL", "ERROR", "WARN")

_LEVEL = r"(?P<level>FATAL|CRITICAL|CRIT|EMERG|ALERT|PANIC|ERROR|ERR|SEVERE|WARNING|WARN|INFO|NOTICE|DEBUG|TRACE)"
_ISO_TS = r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)"

_PATTERNS = [
    # 2026-10-20 14:03:09,880 ERROR [service] message   /  ... ERROR service - message
    re.compile(rf"^{_ISO_TS}\s+\[?{_LEVEL}\]?\s+(?:\[(?P<service>[^\]]+)\]\s*[:-]?\s*)?(?P<msg>.*)$", re.I),
    # [2026-10-20 14:03:09] [ERROR] [service] message
    re.compile(rf"^\[{_ISO_TS}\]\s*\[?{_LEVEL}\]?\s*(?:\[(?P<service>[^\]]+)\]\s*)?(?P<msg>.*)$", re.I),
    # Oct 21 09:00:12 host service[123]: ERROR message
    re.compile(
        r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+(?P<service>[\w./-]+?)(?:\[\d+\])?:\s*"
        rf"(?:{_LEVEL}\b:?\s*)?(?P<msg>.*)$"
    ),
]
_ANY_TS = re.compile(_ISO_TS)
_ANY_LEVEL = re.compile(rf"\b{_LEVEL}\b")
_CONTINUATION = re.compile(
    r"^(\s+|at\s|Caused by:|Traceback|\.\.\.\s*\d+\s+more|[\w.]+(Error|Exception)(:|$)|File \")"
)

_JSON_KEYS = {
    "ts": ("timestamp", "@timestamp", "ts", "time", "datetime", "date"),
    "level": ("level", "severity", "lvl", "loglevel", "log.level"),
    "service": ("service", "service.name", "app", "application", "logger", "component", "source"),
    "msg": ("message", "msg", "log", "text", "event"),
}


@dataclass
class LogEntry:
    line_no: int  # 1-based line number of the record's first line in the original text
    timestamp: datetime | None
    level: str
    service: str | None
    message: str
    raw: str
    continuation: list[str] = field(default_factory=list)
    end_line_no: int = 0

    @property
    def full_text(self) -> str:
        return "\n".join([self.message, *self.continuation])

    @property
    def is_problem(self) -> bool:
        return self.level in PROBLEM_LEVELS


@dataclass
class ParsedLog:
    lines: list[str]  # original lines, index 0 == line 1
    entries: list[LogEntry]
    format_counts: dict[str, int]

    @property
    def line_count(self) -> int:
        return len(self.lines)

    def line(self, line_no: int) -> str | None:
        if 1 <= line_no <= len(self.lines):
            return self.lines[line_no - 1]
        return None

    def entry_for_line(self, line_no: int) -> LogEntry | None:
        for e in self.entries:
            if e.line_no <= line_no <= e.end_line_no:
                return e
        return None


def normalize_level(level: str | None) -> str:
    if not level:
        return "UNKNOWN"
    return LEVEL_ALIASES.get(level.strip().upper(), "UNKNOWN")


def parse_timestamp(value: str | None, default_year: int | None = None) -> datetime | None:
    """Parse a timestamp into a naive UTC-comparable datetime. Returns None if unparseable."""
    if not value:
        return None
    value = value.strip()
    iso = value.replace(",", ".").replace(" ", "T", 1)
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        dt = None
    if dt is None:
        try:  # syslog has no year
            dt = datetime.strptime(f"{default_year or datetime.now().year} {' '.join(value.split())}", "%Y %b %d %H:%M:%S")
        except ValueError:
            return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _first(d: dict, keys: tuple[str, ...]):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _parse_json_line(line: str):
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    msg = _first(obj, _JSON_KEYS["msg"])
    if msg is None:
        msg = json.dumps(obj)
    extras = {k: v for k, v in obj.items() if k not in sum(_JSON_KEYS.values(), ())}
    if extras:
        msg = f"{msg} " + " ".join(f"{k}={v}" for k, v in extras.items())
    return {
        "ts": str(_first(obj, _JSON_KEYS["ts"]) or ""),
        "level": str(_first(obj, _JSON_KEYS["level"]) or ""),
        "service": _first(obj, _JSON_KEYS["service"]),
        "msg": str(msg),
    }


def _parse_line(line: str):
    """Return (fields, format_name) or (None, None)."""
    stripped = line.strip()
    if stripped.startswith("{"):
        fields = _parse_json_line(stripped)
        if fields:
            return fields, "json"
    for i, pattern in enumerate(_PATTERNS):
        m = pattern.match(stripped)
        if m:
            return m.groupdict(), ("standard", "bracketed", "syslog")[i]
    return None, None


def parse_logs(text: str, default_year: int | None = None) -> ParsedLog:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()

    entries: list[LogEntry] = []
    format_counts: dict[str, int] = {}

    for idx, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        fields, fmt = _parse_line(line)

        if fields is None:
            # Stack traces etc. belong to the previous record.
            if entries and _CONTINUATION.match(line):
                entries[-1].continuation.append(line.rstrip())
                entries[-1].end_line_no = idx
                continue
            # Generic fallback: look for a timestamp / level anywhere in the line.
            ts_m, lvl_m = _ANY_TS.search(line), _ANY_LEVEL.search(line)
            fields = {
                "ts": ts_m.group("ts") if ts_m else None,
                "level": lvl_m.group("level") if lvl_m else None,
                "service": None,
                "msg": line.strip(),
            }
            fmt = "unstructured"

        format_counts[fmt] = format_counts.get(fmt, 0) + 1
        service = fields.get("service")
        entries.append(
            LogEntry(
                line_no=idx,
                timestamp=parse_timestamp(fields.get("ts"), default_year),
                level=normalize_level(fields.get("level")),
                service=str(service).strip() if service else None,
                message=(fields.get("msg") or "").strip(),
                raw=line.rstrip(),
                end_line_no=idx,
            )
        )

    return ParsedLog(lines=lines, entries=entries, format_counts=format_counts)
