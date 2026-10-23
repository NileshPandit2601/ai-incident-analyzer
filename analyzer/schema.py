"""Structured output contract for the LLM.

The JSON schema generated from these models is passed to Ollama's ``format`` parameter, so the
model is constrained to produce exactly this shape. Field order matters: evidence is produced
*before* the likely cause, so the model commits to observations before drawing conclusions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Evidence(BaseModel):
    claim: str = Field(description="A factual observation that is directly visible in the logs")
    line_numbers: list[int] = Field(description="Log line numbers (the L<n> labels) that show this observation")


class LikelyCause(BaseModel):
    statement: str = Field(description="The most likely root cause, phrased as a hypothesis")
    confidence: Literal["low", "medium", "high"]
    reasoning: str = Field(description="How the evidence leads to this cause, separating cause from symptoms")


class SeverityAssessment(BaseModel):
    level: Literal["Critical", "High", "Medium", "Low"]
    reason: str


class NextStep(BaseModel):
    priority: int = Field(description="1 = check first")
    action: str
    rationale: str


class Escalation(BaseModel):
    required: bool
    to_team: str = Field(description="Which team should be engaged, e.g. Database/DBA, Platform, Security, Application dev")
    trigger_condition: str = Field(description="When to escalate (now, or the condition that should trigger it)")


class LLMAnalysis(BaseModel):
    error_summary: str = Field(description="2-4 sentences summarizing the key errors")
    patterns: list[str] = Field(description="Repeated failures, sequences and correlations observed")
    evidence: list[Evidence]
    likely_cause: LikelyCause
    contributing_factors: list[str]
    assumptions: list[str] = Field(description="Anything believed but NOT directly shown in the logs")
    impact: str
    severity: SeverityAssessment
    next_steps: list[NextStep]
    escalation: Escalation
    incident_summary: str = Field(description="Concise summary to share with operations/engineering teams")


def _inline_refs(schema: dict) -> dict:
    """Replace $ref pointers with their definitions (simpler grammar for small local models)."""
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, dict):
            if "$ref" in node:
                return walk(dict(defs[node["$ref"].split("/")[-1]]))
            return {k: walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema)


# Upper bounds enforced by the decoding grammar (not by Pydantic, so nothing is rejected after the fact).
# Output tokens are the main cost of local inference, and the bounds also stop padded, generic advice.
_MAX_ITEMS = {"patterns": 4, "evidence": 6, "contributing_factors": 3, "assumptions": 4, "next_steps": 5}


def llm_json_schema() -> dict:
    schema = _inline_refs(LLMAnalysis.model_json_schema())
    for name, limit in _MAX_ITEMS.items():
        schema["properties"][name]["maxItems"] = limit
        schema["properties"][name]["minItems"] = 1 if name in ("evidence", "next_steps") else 0
    return schema
