"""Command-line entry point.

    python cli.py sample_logs/payment_db_timeout.log
    python cli.py my.log --json result.json --no-llm
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from analyzer.config import get_settings
from analyzer.pipeline import analyze
from analyzer.report import incident_markdown


def main() -> int:
    ap = argparse.ArgumentParser(description="AI-Powered Incident Analyzer (local Ollama)")
    ap.add_argument("logfile", help="log file to analyze, or - for stdin")
    ap.add_argument("--model", help="Ollama model (default from OLLAMA_MODEL or qwen2.5:7b)")
    ap.add_argument("--no-llm", action="store_true", help="deterministic analysis only")
    ap.add_argument("--json", metavar="PATH", help="also write the raw AI JSON + validation to this file")
    ap.add_argument("--show-prompt", action="store_true", help="print the prompt sent to the model")
    args = ap.parse_args()

    text = sys.stdin.read() if args.logfile == "-" else Path(args.logfile).read_text(encoding="utf-8", errors="replace")
    settings = get_settings()
    if args.model:
        settings.model = args.model

    received = [0]

    def progress(piece: str) -> None:
        received[0] += len(piece)
        print(f"\rgenerating... {received[0]} chars", end="", file=sys.stderr, flush=True)

    result = analyze(text, settings=settings, use_llm=not args.no_llm, on_token=progress)
    print(file=sys.stderr)
    if args.show_prompt:
        print(result.prompt, "\n" + "=" * 80)
    sys.stdout.reconfigure(encoding="utf-8")
    print(incident_markdown(result, Path(args.logfile).name))
    print(f"\n---\nllm: {result.llm_duration_s:.1f}s, attempts={result.attempts}, "
          f"tokens in/out={result.prompt_tokens}/{result.output_tokens}")

    if args.json and result.llm:
        Path(args.json).write_text(json.dumps({
            "analysis": result.llm.model_dump(),
            "validation": asdict(result.validation),
            "rule_severity": {"level": result.rule_severity.level, "score": result.rule_severity.score,
                              "reasons": result.rule_severity.reasons},
        }, indent=2, default=str), encoding="utf-8")
    return 0 if (result.ok or args.no_llm) else 1


if __name__ == "__main__":
    raise SystemExit(main())
