# Approach and Design Thinking

This document explains how I approached the problem before and during the build: how I framed it, which risks I
identified, which options I compared, and what changed once the prototype met real data. The README covers
*what* was built; this covers *why*.

---

## 1. Framing the problem

**Who uses it and when.** An on-call engineer, mid-incident, with a wall of logs and little time. What they need
is not a clever paragraph. They need fast answers they can trust:

1. What is breaking, and how much?
2. What probably started it?
3. How bad is it, and do I need to wake someone up?
4. What do I check first?

**What "good" means here.** In incident response, a confident wrong answer is worse than no answer. It sends
people down the wrong path while the outage continues. So I set the top success criterion as
**trustworthiness**, ahead of eloquence. The brief points the same way: it asks to keep *evidence separate from
assumptions*.

**Breaking the brief into sub-problems:**

| Sub-problem | Nature | Best solved by |
|---|---|---|
| Read many log formats, count errors, group repeats, build a timeline | Exact, mechanical | **Code** |
| Connect events into a cause-and-effect story | Judgment, domain knowledge | **LLM** |
| Rate severity | Partly rules (outage, volume, customer impact), partly judgment | **Both, independently** |
| Recommend steps and escalation | Judgment | **LLM**, constrained to cite evidence |
| Make sure nothing is invented | Verification | **Code** |

This table drove the whole architecture.

---

## 2. Risks identified up front

| Risk | Why it matters | Mitigation designed in |
|---|---|---|
| LLM **miscounts** or invents numbers | Wrong scale leads to wrong severity | Code computes every count. A validator flags any number in the output that appears nowhere in the log. |
| LLM **invents evidence** | Guesses presented as facts | Evidence must cite line numbers. The validator checks that each line exists, was shown to the model, and matches the claim. |
| **Symptom mistaken for cause** | Teams fix the wrong thing | Prompt asks what changed *before* the first problem. Code extracts change events (deploys, jobs, key rotations) and the first-error time. |
| **Silent context truncation** in Ollama | Model reasons over half the log without saying so | Prompt size is capped by a budget derived from `num_ctx`. Large logs send a code-selected subset, and the UI says so. |
| **Invalid / unparseable output** | App breaks during an incident | JSON-schema constrained output, Pydantic validation, one retry with the error fed back |
| **LLM unavailable** | Tool is useless exactly when needed | Every deterministic result still works; AI sections show a clear error. |
| **Sensitive data in logs** | Logs contain IPs, users, tokens | Local model only, so no data leaves the machine. |

---

## 3. Key design decisions and alternatives considered

### 3.1 "Parse first, reason second, verify last" vs. "send logs to the LLM"
The simplest design is to paste the log into a prompt and display the answer. I rejected it: it's untestable,
can't tell grounded claims from guesses, and fails on large logs. The hybrid pipeline costs more code but makes
every stage testable on its own. The deterministic core has exact unit tests, and only the reasoning step is
probabilistic.

### 3.2 Two independent severity opinions
The rule-based score is **deliberately left out of the prompt**. If the model saw it, it would tend to anchor on
it, and agreement would mean nothing. Kept independent, the two act as a cross-check: agreement builds
confidence, and a gap of two or more levels raises "needs human review" instead of hiding the conflict.

### 3.3 Structure that forces reasoning order
The output schema puts `evidence` **before** `likely_cause`, so the model must list observations before it
concludes. `assumptions` is a separate required field, so speculation has a designated place instead of
leaking into the evidence.

### 3.4 Signatures instead of exact-match grouping
`timeout for order 88122` and `timeout for order 88125` are the same failure. Messages are normalized by masking
IDs, numbers, IPs and UUIDs, but **HTTP status codes are kept**, because 503 vs. 401 is the difference between an
outage and an auth problem.

### 3.5 Local model vs. cloud API
A cloud model would be faster and stronger. I chose local inference (Ollama) because production logs often
contain sensitive data, and a tool that ships them to a third party is a hard sell to an operations team. The
cost is speed and model size. The guardrails above compensate for the weaker model.

### 3.6 Streamlit + a UI-independent pipeline
Streamlit gives a usable dashboard quickly. All logic lives in `analyzer/pipeline.py`, so the same analysis
runs from the CLI, the tests and the evaluation script. The UI is just one consumer.

---

## 4. What changed once it met real data

Planning gets you a design; testing tells you where it's wrong. Issues found and fixed during the build:

| Observation | Root cause | Fix |
|---|---|---|
| Different errors were being merged into one pattern | Masking placeholders contained digits, which a later masking pass re-masked | Placeholders use letters only; covered by a unit test |
| Requests timed out on slower hardware | A total-request timeout doesn't fit CPU-heavy inference (minutes per answer) | Streaming with a **per-chunk** timeout. Long runs still finish, and the UI shows live progress. |
| Prompts could exceed the model's context window | Ollama truncates oversized prompts silently | A character budget derived from `num_ctx` decides how many lines are sent |
| The model spent its time on hidden "thinking" before answering | The newer model has a reasoning mode enabled by default | Thinking is disabled. The schema's evidence-first order already gives it room to reason. |
| Hardware limits (4 GB GPU, 16 GB RAM) | A 9B model doesn't fit on the GPU | Context window set to 8192; model chosen within a 10 GB limit (`qwen2.5:7b` → `qwen3.5:9b`) for better reasoning at acceptable speed |
| Full evaluation: the AI found the right cause on 6/6 samples but rated severity one level too high on 4/6, including one outside the accepted range | The model treats any customer-visible error as an outage and doesn't weigh partial impact (e.g. 2 failing products while others work) | Confirms the decision to keep an independent rule score and flag disagreement. A next step is to give the model explicit severity definitions with examples and re-measure. |
| A short pasted DB-crash log: AI said Critical, rules said Medium | The log's shutdown lines were tagged `[SYSTEM]`/`[INFO]`, which the rules don't treat as outage signals | **Working as designed:** the disagreement was surfaced as "needs human review" rather than hidden. It also points to a rule improvement (see below). |

---

## 5. How correctness is measured

"It looks right" is not a test. Three layers:

1. **Unit tests (39)** for everything deterministic: parsing, grouping, severity rules, validator behavior
   (fake citations, invented numbers, severity disagreement), and pipeline failure paths, using a fake LLM.
2. **Scenario evaluation** on six simulated incidents ranging from Critical to Low. For each scenario I wrote
   the expected severity and the root-cause concepts a good answer must mention, *before* running the model. That
   way the test can't be fitted to the output.
3. **Run-time grounding score:** every analysis reports what share of its evidence was verified against the log,
   so the user sees reliability on each answer, not just in a report.

---

## 6. Trade-offs I accepted

* **Speed vs. privacy:** minutes per analysis on a laptop, in exchange for no data leaving the machine.
* **Lexical evidence check vs. semantic:** shared-key-term matching is simple and explainable but can be fooled by
  loose paraphrase. It catches invented line references reliably, which is the bigger risk.
* **Keyword-based rules:** transparent and predictable, but tuned for a typical web stack and would need
  per-organization tuning.

## 7. With more time

* Treat phrases like "emergency shutdown" or "stopped unexpectedly" as outage signals, whatever level the line is
  tagged with (the gap found in section 4).
* Retrieval over runbooks and past incident reports, so recommendations reflect the team's own procedures.
* Correlate with metrics and deploy history to confirm or reject the cause hypothesis.
* Feedback buttons on each analysis to build a labelled dataset and track accuracy over time.
* Export to Jira / ServiceNow / Slack.
