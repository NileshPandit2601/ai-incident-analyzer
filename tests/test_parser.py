from datetime import datetime

from analyzer.parser import normalize_level, parse_logs, parse_timestamp


def test_standard_format():
    p = parse_logs("2026-10-20 14:03:09,880 ERROR [payment-service] Connection is not available")
    e = p.entries[0]
    assert e.level == "ERROR"
    assert e.service == "payment-service"
    assert e.timestamp == datetime(2026, 10, 20, 14, 3, 9, 880000)
    assert e.message == "Connection is not available"


def test_iso_z_timestamp_converted_to_naive_utc():
    p = parse_logs("2026-10-18T10:25:01.215Z ERROR [report-service] OOM")
    assert p.entries[0].timestamp == datetime(2026, 10, 18, 10, 25, 1, 215000)
    assert p.entries[0].timestamp.tzinfo is None


def test_bracketed_format():
    p = parse_logs("[2026-10-22 06:00:04] [ERROR] [log-shipper] Upload failed")
    e = p.entries[0]
    assert (e.level, e.service, e.message) == ("ERROR", "log-shipper", "Upload failed")


def test_syslog_format():
    p = parse_logs("Oct 21 09:00:12 api-02 profile-service[877]: ERROR JWT validation failed", default_year=2026)
    e = p.entries[0]
    assert e.level == "ERROR"
    assert e.service == "profile-service"
    assert e.timestamp == datetime(2026, 10, 21, 9, 0, 12)
    assert e.message == "JWT validation failed"


def test_json_lines_with_extra_fields():
    p = parse_logs('{"timestamp": "2026-10-23T07:30:45Z", "level": "warning", "service": "inv", "message": "x", "trace_id": "t-1"}')
    e = p.entries[0]
    assert (e.level, e.service) == ("WARN", "inv")
    assert "trace_id=t-1" in e.message
    assert p.format_counts == {"json": 1}


def test_stack_trace_lines_attach_to_previous_entry():
    text = "\n".join([
        "2026-10-20 14:03:09,881 ERROR [svc] Failed to persist",
        "\tat com.shop.Repo.save(Repo.java:58)",
        "Caused by: java.sql.SQLTransientConnectionException: timeout",
        "2026-10-20 14:03:10,002 INFO  [svc] next",
    ])
    p = parse_logs(text)
    assert len(p.entries) == 2
    assert len(p.entries[0].continuation) == 2
    assert p.entries[0].end_line_no == 3
    assert p.entry_for_line(3) is p.entries[0]
    assert p.entries[1].line_no == 4


def test_unknown_lines_are_kept_not_dropped():
    p = parse_logs("something happened ERROR here\njust text")
    assert len(p.entries) == 2
    assert p.entries[0].level == "ERROR"
    assert p.entries[1].level == "UNKNOWN"


def test_line_numbers_survive_blank_lines_and_crlf():
    p = parse_logs("2026-10-20 14:00:00 INFO [a] one\r\n\r\n2026-10-20 14:00:01 ERROR [a] two\r\n")
    assert [e.line_no for e in p.entries] == [1, 3]
    assert p.line(3).endswith("two")
    assert p.line(99) is None


def test_level_aliases():
    assert normalize_level("fatal") == "CRITICAL"
    assert normalize_level("Warning") == "WARN"
    assert normalize_level("SEVERE") == "ERROR"
    assert normalize_level(None) == "UNKNOWN"


def test_bad_timestamp_returns_none():
    assert parse_timestamp("not-a-date") is None
