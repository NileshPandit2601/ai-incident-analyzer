# AI-Powered Incident Analyzer

Paste or upload application logs. The tool summarizes errors and patterns and proposes a likely cause, keeping cited
evidence separate from assumptions. It assigns a severity with reasons, recommends next steps and when to escalate,
and writes a shareable incident summary.

It runs **fully locally**: the model is served by [Ollama](https://ollama.com) (`qwen3.5:9b`), so no log data leaves the machine.

## Screenshots

A database connection-exhaustion log pasted into the app. The run also shows the safety checks at work: one evidence
claim is only partly supported, and the AI and rule severities disagree, so the result is flagged for human review.

| Paste logs and analyze | Overview: severity, key errors, timeline |
|---|---|
| ![Paste logs](screenshots/pasted_logs.png) | ![Overview](screenshots/overview.png) |
| **Root cause: evidence vs. assumptions, two severity opinions** | **Actions: ordered steps and escalation** |
| ![Root cause](screenshots/root_cause.png) | ![Actions](screenshots/actions.png) |

The incident summary exported from this run: [screenshots/incident_summary_example.md](screenshots/incident_summary_example.md).

## Quick start

**Prerequisites:** Python 3.10+ and [Ollama](https://ollama.com/download) running locally.

**1. Install**

```powershell
ollama pull qwen3.5:9b
python -m venv .venv
.\.venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

**2. Run**

```powershell
streamlit run app.py              # opens http://localhost:8501
```

In the app, choose **Sample log**, then `payment_db_timeout.log`, then **Analyze**.

**Other ways to run it**

| Command | What it does |
|---|---|
| `python cli.py sample_logs\payment_db_timeout.log` | Same analysis from the command line |
| `python cli.py <file> --no-llm` | Instant rule-based analysis, no Ollama needed |
| `python -m pytest -q tests` | 39 unit tests, no Ollama needed |
| `python tests\eval_scenarios.py` | Full evaluation against the model (~35 min) |

Settings (Ollama URL, model, context size) can be changed in the app sidebar or in a `.env` file (see `.env.example`).
On a machine without a large GPU, one analysis takes a few minutes; a smaller model such as `qwen3.5:4b` is faster.

## Approach: parse first, reason second, verify last

```
logs ─► Parser ─► Pattern analyzer ─► Rule severity ──────────────┐ (independent cross-check)
              └─ facts digest + numbered log lines ─► LLM (JSON schema) ─► Validator ─► UI / report
```

Code handles the facts (parsing, counting, grouping repeated errors, rule-based severity). The model reasons over
them, and its answer must cite log lines; the validator checks every citation, so guesses can't pass as evidence.
The design reasoning, risks, alternatives and lessons from testing are in **[APPROACH.md](APPROACH.md)**.

## Models and tools

* **Runtime:** Ollama with `qwen3.5:9b` (6.6 GB), using structured JSON output at temperature 0 with a fixed seed.
  No cloud APIs are called.
* **Development:** I built this with an AI coding assistant (Claude Code). My role was to turn the brief into
  requirements and set the design constraints: local-only inference, a model under 10 GB, and evidence kept separate
  from assumptions. I directed the implementation, chose and switched models based on quality and speed on my
  hardware, ran and reviewed the app and tests on the sample incidents, and prepared the submission.

## Testing

* **Unit tests** (39): parser formats, pattern grouping, severity per sample, validator (fake citations, invented
  numbers, severity disagreement), and the pipeline with a fake LLM (retry on invalid JSON, Ollama-down fallback,
  large logs).
* **Scenario evaluation** (`python tests\eval_scenarios.py`): runs six simulated incidents (Critical to Low) through
  the real model. It scores JSON validity, severity, required root-cause concepts and evidence grounding.
  The expected severity and required concepts for each sample were written down before running the model.

### Results (`qwen3.5:9b`, temperature 0, full run in [tests/eval_results.md](tests/eval_results.md))

| Sample | Expected | AI severity | Rule severity | Cause concepts found | Evidence verified |
|---|---|---|---|---|---|
| payment_db_timeout | Critical | Critical | Critical | 100% | 75% |
| memory_leak_oom | High | Critical | Critical | 100% | 100% |
| auth_token_expiry | High | High | High | 100% | 100% |
| mixed_json_format | High | **Critical ✗** | High | 100% | 100% |
| disk_space_warning | Medium | High | Medium | 100% | 67% |
| minor_retry_noise | Low | Medium | Low | 100% | 100% |

| Metric | Result |
|---|---|
| Valid structured output | 6/6 |
| Root cause identified (all required concepts) | 6/6 |
| Evidence claims verified against the log | 90% on average |
| AI severity within the accepted range / exact | 5/6 / 2/6 |
| Rule severity within the accepted range | 6/6 |
| Average analysis time (4 GB GPU laptop) | about 5.7 minutes |

**What this shows.** The model is strong at *explaining* incidents: it found the specific trigger every time, for
example the `rc-20261020` reconciliation job, the zero-second key-rotation grace period, and the legacy import
without warehouse mapping. But it **over-escalates severity**: 4 of 6 were rated one level too high. The one miss
called a partial failure Critical: only two products returned errors while others kept working. The transparent
rule score was better calibrated. This is the reason the design keeps two independent severity opinions and flags
large disagreements for human review instead of trusting the model alone.

## Assumptions and limitations

* Logs are mostly English text with timestamps and levels. Unknown formats are still kept, with a warning shown.
* The cause is a **hypothesis from logs alone**, with no metrics, traces or deploy history. The validator catches
  invented references but cannot prove the reasoning is right.
* Speed depends on hardware: about 8 minutes per analysis on a 4 GB GPU laptop, and seconds on a GPU that fits the model.
* Possible next steps: search over runbooks and past incidents, Jira/Slack export, and correlating several log files.

## Project structure

```
app.py, cli.py       Streamlit UI and command line
analyzer/            parser, patterns, severity, prompts, schema, llm_client, validator, pipeline, report
sample_logs/         six simulated incidents
tests/               unit tests and scenario evaluation
```
