from pathlib import Path

import pytest

from analyzer.parser import parse_logs
from analyzer.patterns import analyze_patterns
from analyzer.severity import score_severity

SAMPLES = Path(__file__).parent.parent / "sample_logs"


def sev(text: str):
    return score_severity(analyze_patterns(parse_logs(text)))


@pytest.mark.parametrize("name,allowed", [
    ("payment_db_timeout.log", {"Critical"}),
    ("memory_leak_oom.log", {"High", "Critical"}),
    ("auth_token_expiry.log", {"High", "Critical"}),
    ("mixed_json_format.log", {"Medium", "High"}),
    ("disk_space_warning.log", {"Medium"}),
    ("minor_retry_noise.log", {"Low"}),
])
def test_sample_baselines(name, allowed):
    assert sev((SAMPLES / name).read_text(encoding="utf-8")).level in allowed


def test_info_only_is_low():
    assert sev("2026-10-20 14:00:00 INFO [a] ok\n2026-10-20 14:00:01 INFO [a] ok").level == "Low"


def test_warnings_only_capped_at_medium():
    text = "\n".join(f"2026-10-20 14:00:{i:02d} WARN [checkout] Disk usage 97% HTTP 503 on /checkout" for i in range(30))
    assert sev(text).level == "Medium"


def test_every_finding_has_reason():
    s = sev((SAMPLES / "payment_db_timeout.log").read_text(encoding="utf-8"))
    assert s.findings and all(f.reason for f in s.findings)
    assert s.score == sum(f.points for f in s.findings)
