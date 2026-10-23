# AI-Powered Incident Analyzer

A prototype tool for Technology Operations teams. You paste or upload application logs, and it:

1. **summarizes the key errors** and detects **repeated failures / patterns**
2. proposes a **likely cause**, with **evidence (cited log lines) kept separate from assumptions**
3. classifies **severity** (Critical / High / Medium / Low) and explains why
4. recommends **ordered troubleshooting steps** and says **when to escalate**
5. produces a **concise incident summary** to share with ops/engineering (Markdown/JSON download)

Everything runs **locally**: the LLM is served by [Ollama](https://ollama.com) (`qwen3.5:9b`), so no log data leaves the machine.

---

## Quick start

Prerequisites: Python 3.10+ (developed on 3.13), Ollama running locally with a model pulled.

```powershell
ollama pull qwen3.5:9b              # once
python -m venv .venv
.\.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

streamlit run app.py                # web UI  -> http://localhost:8501
python cli.py sample_logs\payment_db_timeout.log      # or the command line
python cli.py my.log --no-llm       # instant, deterministic analysis only
python -m pytest -q tests           # unit tests (no Ollama needed)
python tests\eval_scenarios.py      # evaluation against the real model
```

Optional settings go in a `.env` file (see `.env.example`): `OLLAMA_URL`, `OLLAMA_MODEL`, `OLLAMA_NUM_CTX`, `OLLAMA_TIMEOUT`.
You can also change the model and context window in the UI sidebar.

> **Performance note:** on the development laptop (GTX 1650 4 GB, `qwen3.5:9b` split ~75% CPU / 25% GPU) one analysis
> takes **~6–9 minutes**. The UI streams the model's output live so progress is visible, and results are cached per
> session. On a GPU that fits the model it takes seconds. A smaller model (e.g. `ollama pull qwen3.5:4b`) can be
> selected in the sidebar for faster demos, with some loss in reasoning quality.

---

## Approach: parse first, reason second, verify last

```
 logs ─► 1 Parser ─► 2 Pattern analyzer ─► 3 Rule severity ─┐
            │                 │                             │ (independent cross-check)
            └──── facts digest + numbered log lines ──► 4 LLM (Ollama, JSON schema) ─► 5 Validator ─► 6 UI / report
```

A 7B local model is good at *reasoning about* incidents but unreliable at *counting* and prone to *inventing*
details. So the work is split:

| Step | Who | What |
|---|---|---|
| 1 Parser (`analyzer/parser.py`) | code | Handles log4j/logback, ISO-8601, bracketed, syslog and JSON-lines formats; attaches stack traces to their record; keeps original line numbers; never drops unknown lines. |
| 2 Patterns (`analyzer/patterns.py`) | code | Normalizes messages into **signatures** (IDs, numbers, IPs, UUIDs masked; HTTP status codes kept), so `timeout for order 88122` and `…88125` group together. Computes counts, first/last seen, services, error bursts (max errors in 60 s), whether errors are still ongoing, resource peaks (pool/heap/disk %), keyword categories (timeout, database, memory, auth, 5xx…), change events (deploys, jobs, key rotations, imports) and a timeline. |
| 3 Rule severity (`analyzer/severity.py`) | code | Transparent point score (outage signals, error volume, 5xx, customer-facing flows, blast radius, ongoing, bursts, resource exhaustion, recovery). Every point has a stated reason. |
| 4 LLM (`analyzer/prompts.py`, `schema.py`, `llm_client.py`) | AI | Receives the **facts digest** plus log lines labelled `L<n>` (the whole log if it fits, otherwise a code-selected subset: first problem + context, change events, an example of every pattern, outage signals, tail). Output is constrained by a **JSON schema** (Ollama structured outputs, generated from Pydantic models). Temperature 0 and a fixed seed for repeatability. |
| 5 Validator (`analyzer/validator.py`) | code | Checks every evidence claim: cited lines must **exist**, must have been **shown to the model**, and must **share key terms** with the claim → `verified` / `weak` / `unverified`. Flags **numbers that appear nowhere in the log** (hallucination check). Compares AI severity with rule severity; two or more levels apart → "needs human review". Downgrades a "high confidence" cause if its evidence is poorly grounded. |
| 6 UI (`app.py`) / report (`analyzer/report.py`) | code | Streamlit dashboard with tabs (Overview, Patterns, Root cause, Actions, Incident summary, Under the hood) plus a Markdown/JSON export. |

### How "evidence vs assumptions" is enforced
* The schema has separate `evidence[]` (each with `line_numbers`) and `assumptions[]` fields, and the system prompt
  requires anything not visible in the logs to go into `assumptions`.
* The JSON schema orders `evidence` **before** `likely_cause`, so the model commits to observations before it concludes.
* The validator checks every citation against the real log and shows ✅ / ⚠️ / ❌ next to each claim. Clicking a claim
  shows the actual cited lines.
* The cause is always labelled a *hypothesis* with a confidence level.

### Why the rule-based severity is not given to the model
The rule score is deliberately left out of the prompt. That keeps the AI's severity an **independent second opinion**,
so agreement between the two means something, and disagreement is shown to the user instead of being hidden.

### Robustness
* Ollama down, model missing or timeout → the app still shows the full deterministic analysis and rule severity, plus a clear error.
* Invalid JSON from the model → one automatic retry, with the validation error fed back to the model.
* Large logs → the lines sent are capped by a budget derived from `num_ctx`, so Ollama never silently truncates the
  prompt. All statistics still cover every line, and the UI says when the model saw only a subset.
* Streaming with a per-chunk timeout, so slow CPU inference completes instead of timing out.

---

## AI tools / models used

| Where | What |
|---|---|
| Runtime inference | **Ollama 0.40.9**, model **`qwen3.5:9b`** (6.6 GB, thinking mode disabled for speed), local `/api/chat` with JSON-schema structured output |
| Development | Claude Code (AI coding assistant) was used to generate code, sample logs, tests and documentation. **TODO (candidate): describe your own contribution here: what you designed, reviewed, changed, tested and can explain.** |

No cloud AI APIs are called at runtime.

---

## Sample data (simulated, no production data)

| File | Scenario | Expected severity |
|---|---|---|
| `payment_db_timeout.log` | The brief's example: a batch reconciliation job's slow queries exhaust the DB pool → payment API timeouts → checkout 503/504, circuit breaker opens | Critical |
| `memory_leak_oom.log` | Unbounded template cache → heap grows → GC pauses → OutOfMemoryError → pod OOMKilled, leak resumes after restart | High |
| `auth_token_expiry.log` | Signing-key rotation with zero grace period; services hold a stale JWKS cache → widespread 401s | High |
| `mixed_json_format.log` | JSON + plain logs + Python traceback; KeyError after a deploy / legacy import without warehouse mapping → HTTP 500 | High |
| `disk_space_warning.log` | Log uploads failing (expired credentials) → rotated logs not deleted → disk climbing to 92% | Medium |
| `minor_retry_noise.log` | Occasional SMTP resets that succeed on retry; one template fallback | Low |

---

## Testing

1. **Unit tests** (`tests/`, 39 tests, no Ollama needed): parser formats, stack-trace attachment, signature
   normalization, pattern facts, severity baselines per sample, validator (fake/nonexistent/unshown citations,
   confidence downgrade, hallucinated numbers, severity disagreement), and the pipeline with a fake LLM (happy path,
   retry on invalid JSON, Ollama-down fallback, large-log sampling within budget).
2. **Scenario evaluation** (`tests/eval_scenarios.py`): runs every sample through the real model and scores JSON
   validity, severity (exact / acceptable), whether the cause mentions the required concepts (`tests/expected.json`),
   grounding (% of evidence verified), unsupported numbers and latency. Results go to `tests/eval_results.md`.
3. **Manual UI tests**: paste, upload, non-log text, Ollama stopped.

### Results

EVAL_RESULTS_PLACEHOLDER

---

## Assumptions

* Logs are text, mostly English, and most lines carry a timestamp and level. Unknown formats degrade gracefully (kept as
  UNKNOWN records, with a warning shown).
* Timestamps without a timezone are treated as one consistent local time. Syslog lines without a year get the current year.
* Keyword lists (customer-facing flows, outage signals) reflect a typical web/e-commerce stack and would be tuned per organization.
* Severity definitions: Critical = outage or core customer flow broken; High = major degradation or recurring crash;
  Medium = limited impact or imminent risk; Low = minor or self-recovering.

## Limitations

* The AI cause is a **hypothesis from logs alone**: there is no access to metrics, traces, deploy history or topology.
* A 7B local model can still misread or over-generalize. The validator catches invented line references and numbers,
  but cannot prove the *reasoning* is right. Hence the human-review flags.
* The evidence check is lexical (shared key terms), so a claim can be "verified" while paraphrasing loosely.
* Slow on CPU-bound hardware (minutes per analysis).
* One analysis per input. No streaming/tailing of live logs, no ticketing/Slack integration, no memory of past incidents.

## Possible next steps
RAG over runbooks and past incident reports; correlating with metrics/deploy events; export to Jira/ServiceNow/Slack;
multi-file upload with per-service correlation; feedback buttons to build a labelled evaluation set.

---

## Project structure

```
app.py                 Streamlit UI
cli.py                 command-line interface
analyzer/
  parser.py            log formats -> LogEntry records
  patterns.py          signatures, counts, bursts, categories, timeline
  severity.py          rule-based severity with reasons
  prompts.py           facts digest + line selection + system prompt
  schema.py            Pydantic output model -> JSON schema for Ollama
  llm_client.py        streaming Ollama client
  validator.py         evidence grounding, hallucination + severity checks
  pipeline.py          end-to-end orchestration (UI-independent)
  report.py            Markdown incident report
  config.py            settings (.env)
sample_logs/           simulated incidents
tests/                 unit tests, expected.json, eval_scenarios.py, eval results
```
