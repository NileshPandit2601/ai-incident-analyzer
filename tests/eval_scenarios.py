"""Scenario evaluation against the real local model.

    python tests/eval_scenarios.py                 # every sample once
    python tests/eval_scenarios.py --runs 3        # repeat to measure consistency
    python tests/eval_scenarios.py --only payment  # subset by name

Metrics per scenario:
  json_valid       model output parsed and validated against the schema
  severity         exact match / within the acceptable set
  cause_concepts   fraction of required concept groups mentioned in cause + reasoning + factors
  grounding        share of evidence claims verified against the log by the validator
  unsupported_nums numbers the model stated that never appear in the log
Writes tests/eval_results.json and tests/eval_results.md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from analyzer.config import get_settings  # noqa: E402
from analyzer.pipeline import analyze  # noqa: E402


def score_concepts(text: str, groups: list[list[str]]) -> tuple[float, list[str]]:
    text = text.lower()
    missing = [g[0] for g in groups if not any(k.lower() in text for k in g)]
    return (len(groups) - len(missing)) / len(groups), missing


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--only", default="", help="substring filter on sample file names")
    args = ap.parse_args()

    expected = json.loads((ROOT / "tests" / "expected.json").read_text(encoding="utf-8"))
    settings = get_settings()
    rows = []
    for name, exp in expected.items():
        if args.only and args.only not in name:
            continue
        text = (ROOT / "sample_logs" / name).read_text(encoding="utf-8")
        for run in range(1, args.runs + 1):
            print(f"[{datetime.now():%H:%M:%S}] {name} run {run}/{args.runs} ...", flush=True)
            r = analyze(text, settings=settings)
            row = {"sample": name, "run": run, "json_valid": r.ok, "attempts": r.attempts,
                   "rule_severity": r.rule_severity.level, "expected": exp["expected"],
                   "llm_time_s": round(r.llm_duration_s, 1), "prompt_tokens": r.prompt_tokens,
                   "output_tokens": r.output_tokens, "error": r.llm_error if not r.ok else None}
            if r.ok:
                a, v = r.llm, r.validation
                blob = " ".join([a.likely_cause.statement, a.likely_cause.reasoning, *a.contributing_factors])
                cscore, missing = score_concepts(blob, exp["cause_concepts"])
                row.update({
                    "ai_severity": v.llm_severity,
                    "severity_exact": v.llm_severity == exp["expected"],
                    "severity_ok": v.llm_severity in exp["acceptable"],
                    "rule_ok": r.rule_severity.level in exp["acceptable"],
                    "cause_concepts": round(cscore, 2), "missing_concepts": missing,
                    "grounding": round(v.grounding_score or 0, 2), "evidence_items": len(v.evidence),
                    "unsupported_nums": v.unsupported_numbers, "confidence": v.confidence,
                    "cause": a.likely_cause.statement,
                })
            rows.append(row)
            print("   ", {k: row.get(k) for k in ("ai_severity", "severity_ok", "cause_concepts", "grounding", "llm_time_s")}, flush=True)

    ok = [r for r in rows if r["json_valid"]]
    summary = {
        "model": settings.model, "num_ctx": settings.num_ctx, "runs": len(rows),
        "json_valid_rate": round(len(ok) / len(rows), 2) if rows else 0,
        "severity_acceptable_rate": round(sum(r["severity_ok"] for r in ok) / len(ok), 2) if ok else 0,
        "severity_exact_rate": round(sum(r["severity_exact"] for r in ok) / len(ok), 2) if ok else 0,
        "rule_severity_acceptable_rate": round(sum(r["rule_severity"] in expected[r["sample"]]["acceptable"] for r in rows) / len(rows), 2) if rows else 0,
        "mean_cause_concepts": round(statistics.mean(r["cause_concepts"] for r in ok), 2) if ok else 0,
        "mean_grounding": round(statistics.mean(r["grounding"] for r in ok), 2) if ok else 0,
        "mean_llm_time_s": round(statistics.mean(r["llm_time_s"] for r in rows), 1) if rows else 0,
    }
    if args.runs > 1:
        per = {}
        for r in ok:
            per.setdefault(r["sample"], []).append(r["ai_severity"])
        summary["severity_consistency"] = {k: f"{Counter(v).most_common(1)[0][1]}/{len(v)}" for k, v in per.items()}

    out_json = ROOT / "tests" / "eval_results.json"
    out_json.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")

    md = [f"# Evaluation results ({datetime.now():%Y-%m-%d %H:%M})", "",
          f"Model `{settings.model}`, num_ctx {settings.num_ctx}, temperature {settings.temperature}, runs {len(rows)}", "",
          "| Metric | Value |", "|---|---|"]
    md += [f"| {k} | {v} |" for k, v in summary.items() if k not in ("model", "num_ctx", "runs")]
    md += ["", "| Sample | Run | Expected | AI severity | Rule severity | Cause concepts | Grounding | Unsupported numbers | LLM time (s) |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        md.append(f"| {r['sample']} | {r['run']} | {r['expected']} | {r.get('ai_severity', 'FAILED')}"
                  f"{'' if r.get('severity_ok', False) else ' ✗'} | {r['rule_severity']} | {r.get('cause_concepts', '-')} | "
                  f"{r.get('grounding', '-')} | {', '.join(r.get('unsupported_nums') or []) or '-'} | {r['llm_time_s']} |")
    md += ["", "## Likely causes produced", ""]
    md += [f"- **{r['sample']}** (run {r['run']}): {r.get('cause', r.get('error'))}" for r in rows]
    (ROOT / "tests" / "eval_results.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
