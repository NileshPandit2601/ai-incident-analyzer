"""End-to-end analysis: parse -> patterns -> rule severity -> LLM -> validation.

UI-independent, so the Streamlit app, the CLI and the tests all use the same code path.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Callable

from pydantic import ValidationError

from analyzer.config import Settings, get_settings
from analyzer.llm_client import OllamaClient, OllamaError
from analyzer.parser import ParsedLog, parse_logs
from analyzer.patterns import PatternReport, analyze_patterns
from analyzer.prompts import SYSTEM_PROMPT, build_user_prompt
from analyzer.schema import LLMAnalysis, llm_json_schema
from analyzer.severity import RuleSeverity, score_severity
from analyzer.validator import ValidationReport, validate

MAX_ATTEMPTS = 2


@dataclass
class AnalysisResult:
    parsed: ParsedLog
    patterns: PatternReport
    rule_severity: RuleSeverity
    prompt: str
    shown_lines: list[int]
    model: str
    llm: LLMAnalysis | None = None
    validation: ValidationReport | None = None
    llm_error: str | None = None
    raw_response: str = ""
    attempts: int = 0
    llm_duration_s: float = 0.0
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    total_duration_s: float = 0.0
    log_warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.llm is not None

    @property
    def severity(self) -> str:
        return self.validation.final_severity if self.validation else self.rule_severity.level


def _call_llm(client: OllamaClient, prompt: str, settings: Settings, result: AnalysisResult, on_token=None) -> None:
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}]
    options = {"temperature": settings.temperature, "num_ctx": settings.num_ctx, "seed": settings.seed}
    schema = llm_json_schema()

    for attempt in range(1, MAX_ATTEMPTS + 1):
        result.attempts = attempt
        chat = client.chat(messages, schema=schema, options=options, on_token=on_token)
        result.raw_response = chat.content
        result.llm_duration_s += chat.duration_s
        result.prompt_tokens, result.output_tokens = chat.prompt_tokens, chat.output_tokens
        try:
            analysis = LLMAnalysis.model_validate(json.loads(chat.content))
            analysis.next_steps.sort(key=lambda s: s.priority)
            result.llm = analysis
            return
        except (json.JSONDecodeError, ValidationError) as exc:
            err = str(exc)[:800]
            result.llm_error = f"Attempt {attempt}: model output failed schema validation: {err}"
            # Self-correction: show the model its own output and the validation error.
            messages += [
                {"role": "assistant", "content": chat.content},
                {"role": "user", "content": f"Your JSON was invalid: {err}\nReturn the complete corrected JSON only."},
            ]
    # all attempts failed: keep llm_error, deterministic results are still returned


def analyze(
    text: str,
    settings: Settings | None = None,
    client: OllamaClient | None = None,
    use_llm: bool = True,
    on_token: Callable[[str], None] | None = None,
) -> AnalysisResult:
    if not text or not text.strip():
        raise ValueError("No log content provided.")
    settings = settings or get_settings()
    client = client or OllamaClient(settings.ollama_url, settings.model, settings.timeout_s)
    t0 = time.perf_counter()

    parsed = parse_logs(text)
    report = analyze_patterns(parsed)
    rule = score_severity(report)
    prompt, shown = build_user_prompt(parsed, report, settings.prompt_char_budget)

    result = AnalysisResult(parsed=parsed, patterns=report, rule_severity=rule, prompt=prompt,
                            shown_lines=shown, model=client.model)

    unparsed = parsed.format_counts.get("unstructured", 0)
    if parsed.entries and unparsed / len(parsed.entries) > 0.5:
        result.log_warnings.append(
            f"{unparsed} of {len(parsed.entries)} lines did not match a known log format; "
            "levels/timestamps may be incomplete, so pattern statistics are less reliable.")
    if not report.groups:
        result.log_warnings.append("No WARN/ERROR records were detected in this input.")
    if len(shown) < parsed.line_count:
        result.log_warnings.append(
            f"Log is large: the model saw {len(shown)} selected lines out of {parsed.line_count} "
            "(all counts and statistics still cover every line).")

    if use_llm:
        try:
            _call_llm(client, prompt, settings, result, on_token)
        except OllamaError as exc:
            result.llm_error = str(exc)
        if result.llm:
            result.validation = validate(result.llm, parsed, rule, shown)

    result.total_duration_s = time.perf_counter() - t0
    return result
