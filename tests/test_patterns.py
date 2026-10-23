from analyzer.parser import parse_logs
from analyzer.patterns import analyze_patterns, signature_of


def test_signature_groups_variable_ids():
    a = signature_of("Payment API call failed for order 88122: Read timed out after 30000ms")
    b = signature_of("Payment API call failed for order 88125: Read timed out after 30000ms")
    assert a == b


def test_signature_keeps_http_status_codes_distinct():
    a = signature_of("HTTP 504 Gateway Timeout returned for POST /checkout request_id=7f3a9c21")
    b = signature_of("HTTP 503 Service Unavailable returned for POST /checkout request_id=a19c7e55")
    assert "HTTP 504" in a and "HTTP 503" in b
    assert a != b


def test_signature_masks_ips_and_uuids():
    s = signature_of("conn from 10.0.0.12:5432 id=3f2a1b4c-1111-2222-3333-444455556666")
    assert "10.0.0.12" not in s and "3f2a1b4c" not in s


LOG = """2026-10-20 14:00:00 INFO  [order] Starting nightly reconciliation job
2026-10-20 14:00:10 WARN  [payment] pool usage at 95%
2026-10-20 14:00:20 ERROR [payment] Connection is not available, request timed out after 30000ms
2026-10-20 14:00:25 ERROR [payment] Connection is not available, request timed out after 30000ms
2026-10-20 14:00:30 ERROR [gateway] HTTP 503 Service Unavailable for POST /checkout
2026-10-20 14:00:40 ERROR [payment] Connection is not available, request timed out after 30000ms"""


def test_pattern_report_facts():
    r = analyze_patterns(parse_logs(LOG))
    assert r.level_counts == {"ERROR": 4, "WARN": 1, "INFO": 1}
    assert r.problem_services == {"payment": 3, "gateway": 1}
    top = next(g for g in r.groups if g.count == 3)
    assert top.line_numbers == [3, 4, 6]
    assert "timeout" in top.categories and "database" in top.categories
    assert r.first_problem_line == 2
    assert r.max_errors_per_minute == 4
    assert r.errors_ongoing_at_end is True
    assert r.resource_peaks["pool"] == 95
    assert 1 in r.change_event_lines
    assert "http_5xx" in r.category_lines
    assert r.customer_facing_lines  # /checkout


def test_groups_ordered_by_first_occurrence():
    r = analyze_patterns(parse_logs(LOG))
    firsts = [g.line_numbers[0] for g in r.groups]
    assert firsts == sorted(firsts)


def test_no_problems():
    r = analyze_patterns(parse_logs("2026-10-20 14:00:00 INFO [a] all good"))
    assert r.groups == [] and r.error_count == 0 and not r.errors_ongoing_at_end
