"""Streamlit UI for the AI-Powered Incident Analyzer.

Run:  streamlit run app.py
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from analyzer.config import get_settings
from analyzer.llm_client import OllamaClient, OllamaError
from analyzer.pipeline import AnalysisResult, analyze
from analyzer.report import incident_markdown

SAMPLES_DIR = Path(__file__).parent / "sample_logs"
SEV_COLORS = {"Critical": "#b91c1c", "High": "#ea580c", "Medium": "#ca8a04", "Low": "#15803d"}
LEVEL_COLORS = {"CRITICAL": "#7f1d1d", "ERROR": "#dc2626", "WARN": "#f59e0b", "INFO": "#60a5fa",
                "DEBUG": "#a1a1aa", "UNKNOWN": "#d4d4d8"}
STATUS_ICON = {"verified": "✅", "weak": "⚠️", "unverified": "❌"}

st.set_page_config(page_title="AI Incident Analyzer", page_icon="🚨", layout="wide")
st.markdown(
    """<style>
    .sev-badge{display:inline-block;padding:.35rem .9rem;border-radius:999px;color:#fff;font-weight:700;font-size:1.1rem}
    .card{border:1px solid rgba(128,128,128,.3);border-radius:.6rem;padding:.9rem 1rem;margin-bottom:.6rem}
    .muted{opacity:.7;font-size:.85rem}
    </style>""",
    unsafe_allow_html=True,
)

settings = get_settings()
st.session_state.setdefault("cache", {})

# ---------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("⚙️ Settings")
    settings.ollama_url = st.text_input("Ollama URL", settings.ollama_url)
    try:
        models = OllamaClient(settings.ollama_url).list_models()
    except OllamaError as exc:
        models = []
        st.error(str(exc))
    if models:
        default = models.index(settings.model) if settings.model in models else 0
        settings.model = st.selectbox("Model", models, index=default)
        ok, msg = OllamaClient(settings.ollama_url, settings.model).health()
        (st.success if ok else st.warning)(msg)
    settings.num_ctx = st.select_slider("Context window (num_ctx)", [4096, 8192, 16384, 32768], value=settings.num_ctx,
                                        help="Bigger fits more log lines but is slower and needs more memory.")
    use_llm = st.toggle("Use AI analysis", value=bool(models),
                        help="Off = deterministic parsing and rule-based severity only (instant).")
    st.divider()
    st.caption("Pipeline: **parse → detect patterns → rule severity → local LLM (JSON schema) → verify citations**. "
               "Logs never leave this machine.")

# ---------------------------------------------------------------------------- input
st.title("🚨 AI-Powered Incident Analyzer")
st.caption("Paste or upload application logs. The tool summarizes errors, finds patterns, proposes a likely cause "
           "(evidence kept separate from assumptions), rates severity and recommends next steps.")

src = st.radio("Log source", ["Sample log", "Upload file", "Paste text"], horizontal=True)
text, source_name = "", "pasted logs"
if src == "Sample log":
    samples = sorted(p.name for p in SAMPLES_DIR.glob("*.log"))
    choice = st.selectbox("Sample", samples)
    if choice:
        text, source_name = (SAMPLES_DIR / choice).read_text(encoding="utf-8"), choice
    with st.expander("Preview log", expanded=False):
        st.code(text, language="log")
elif src == "Upload file":
    up = st.file_uploader("Log file", type=["log", "txt", "json", "jsonl", "out"])
    if up:
        text, source_name = up.getvalue().decode("utf-8", errors="replace"), up.name
        with st.expander("Preview log", expanded=False):
            st.code(text[:20000], language="log")
else:
    text = st.text_area("Paste logs here", height=260, placeholder="2026-10-20 14:03:09,880 ERROR [payment-service] ...")

cache_key = hashlib.sha256(f"{text}|{settings.model}|{settings.num_ctx}|{use_llm}".encode()).hexdigest()
run = st.button("🔍 Analyze", type="primary", disabled=not text.strip())

if run:
    if cache_key in st.session_state.cache:
        st.session_state.current = cache_key
    else:
        with st.status("Analyzing…", expanded=True) as status:
            st.write("Parsing logs and detecting patterns (deterministic)…")
            progress = st.empty()
            preview = st.empty()
            buf: list[str] = []

            def on_token(piece: str) -> None:
                buf.append(piece)
                if len(buf) % 15 == 0:
                    n = sum(map(len, buf))
                    progress.write(f"🧠 {settings.model} is writing the analysis… {n} characters so far")
                    preview.code("".join(buf)[-600:], language="json")

            if use_llm:
                st.write(f"Asking local model **{settings.model}** (this can take a few minutes on CPU)…")
            result = analyze(text, settings=settings, use_llm=use_llm, on_token=on_token)
            preview.empty()
            label = "Analysis complete" if (result.ok or not use_llm) else "AI step failed: showing deterministic analysis"
            status.update(label=f"{label} ({result.total_duration_s:.0f}s)", state="complete" if result.ok or not use_llm else "error",
                          expanded=False)
        st.session_state.cache[cache_key] = (result, source_name)
        st.session_state.current = cache_key

if "current" not in st.session_state:
    st.info("Pick a sample (try **payment_db_timeout.log**) or provide your own logs, then click **Analyze**.")
    st.stop()

r: AnalysisResult
r, source_name = st.session_state.cache[st.session_state.current]
p, a, v = r.patterns, r.llm, r.validation

# ---------------------------------------------------------------------------- header
st.divider()
sev = r.severity
c0, c1, c2, c3, c4, c5 = st.columns([1.4, 1, 1, 1, 1, 1.2])
with c0:
    st.markdown(f"<span class='sev-badge' style='background:{SEV_COLORS[sev]}'>{sev}</span>", unsafe_allow_html=True)
    st.markdown(f"<div class='muted'>{source_name}</div>", unsafe_allow_html=True)
c1.metric("Records", p.total_entries)
c2.metric("Errors", p.error_count)
c3.metric("Warnings", p.warn_count)
c4.metric("Services w/ errors", len(p.problem_services))
if v and v.grounding_score is not None:
    c5.metric("Evidence verified", f"{v.grounding_score:.0%}", help="Share of AI evidence claims whose cited log lines exist and match the claim.")
else:
    c5.metric("Rule score", r.rule_severity.score)

for w in r.log_warnings:
    st.warning(w)
if r.llm_error and not a:
    st.error(f"AI analysis unavailable: {r.llm_error}. Showing deterministic results only.")
if v and v.needs_review:
    st.warning("⚠️ **Needs human review**: " + " ".join(v.notes))

tabs = st.tabs(["📊 Overview", "🔁 Patterns", "🧩 Root cause", "🛠️ Actions", "📝 Incident summary", "🔬 Under the hood"])

# ---------------------------------------------------------------------------- overview
with tabs[0]:
    if a:
        st.subheader("Key errors")
        st.write(a.error_summary)
        st.markdown(f"**Impact:** {a.impact}")
    left, right = st.columns([2, 1])
    with left:
        if p.timeline:
            df = pd.DataFrame(p.timeline)
            fig = px.bar(df, x="time", y="count", color="level", color_discrete_map=LEVEL_COLORS,
                         title="Log records over time", category_orders={"level": list(LEVEL_COLORS)})
            fig.update_layout(height=320, margin=dict(l=10, r=10, t=40, b=10), legend_title=None, bargap=0.1)
            st.plotly_chart(fig, width="stretch")
        else:
            st.info("No timestamps found, so no timeline is available.")
    with right:
        st.markdown("**Window**")
        st.write(f"{p.start or 'n/a'} → {p.end or 'n/a'}")
        st.markdown(f"**Peak:** {p.max_errors_per_minute} errors / 60s")
        st.markdown(f"**Still failing at end of log:** {'yes 🔴' if p.errors_ongoing_at_end else 'no 🟢'}")
        if p.category_counts:
            st.markdown("**Problem categories**")
            st.dataframe(pd.DataFrame(p.category_counts.items(), columns=["category", "records"]),
                         hide_index=True, width="stretch")

# ---------------------------------------------------------------------------- patterns
with tabs[1]:
    if a and a.patterns:
        st.subheader("Patterns observed (AI)")
        for item in a.patterns:
            st.markdown(f"- {item}")
    st.subheader("Repeated WARN/ERROR signatures (computed by code)")
    if p.groups:
        gdf = pd.DataFrame([{
            "first line": g.line_numbers[0], "level": g.level, "count": g.count, "services": ", ".join(g.services),
            "first seen": g.first_seen, "last seen": g.last_seen, "categories": ", ".join(g.categories),
            "signature": g.signature,
        } for g in p.groups])
        st.dataframe(gdf, hide_index=True, width="stretch")
        pick = st.selectbox("Show raw lines for signature", range(len(p.groups)),
                            format_func=lambda i: f"[{p.groups[i].level}] ×{p.groups[i].count} {p.groups[i].signature[:90]}")
        g = p.groups[pick]
        st.code("\n".join(f"L{n}: {r.parsed.line(n)}" for n in g.line_numbers[:30]), language="log")
    else:
        st.info("No WARN/ERROR records detected.")

# ---------------------------------------------------------------------------- root cause
with tabs[2]:
    if not a:
        st.info("Root-cause analysis needs the AI step. Rule-based severity reasons:")
        for x in r.rule_severity.reasons:
            st.markdown(f"- {x}")
    else:
        conf_color = {"high": "🟢", "medium": "🟡", "low": "🟠"}[v.confidence]
        st.markdown(f"<div class='card'><b>Likely cause (hypothesis)</b> · confidence {conf_color} {v.confidence}<br>"
                    f"{a.likely_cause.statement}</div>", unsafe_allow_html=True)
        st.markdown(f"**Reasoning:** {a.likely_cause.reasoning}")
        if a.contributing_factors:
            st.markdown("**Contributing factors:** " + "; ".join(a.contributing_factors))

        ev_col, as_col = st.columns(2)
        with ev_col:
            st.subheader("✅ Evidence (from the logs)")
            st.caption("Each claim is checked: cited lines must exist, have been shown to the model, and match the claim.")
            for c in v.evidence:
                with st.expander(f"{STATUS_ICON[c.status]} {c.claim}"):
                    st.caption(f"status: **{c.status}** {('· ' + c.note) if c.note else ''}")
                    lines = c.valid_lines or []
                    if lines:
                        st.code("\n".join(f"L{n}: {r.parsed.line(n)}" for n in lines[:12]), language="log")
        with as_col:
            st.subheader("💭 Assumptions (not confirmed)")
            st.caption("Inferences the model could not see directly in the logs. Verify before acting.")
            for x in a.assumptions or ["(none stated)"]:
                st.markdown(f"- {x}")

        st.subheader("Severity")
        s1, s2 = st.columns(2)
        with s1:
            st.markdown(f"**AI assessment:** <span class='sev-badge' style='background:{SEV_COLORS[v.llm_severity]};font-size:.85rem'>"
                        f"{v.llm_severity}</span>", unsafe_allow_html=True)
            st.write(a.severity.reason)
        with s2:
            st.markdown(f"**Rule-based cross-check:** <span class='sev-badge' style='background:{SEV_COLORS[v.rule_severity]};"
                        f"font-size:.85rem'>{v.rule_severity}</span> (score {r.rule_severity.score}) · "
                        f"{v.severity_agreement.replace('_', ' ')}", unsafe_allow_html=True)
            for x in r.rule_severity.reasons:
                st.markdown(f"<span class='muted'>{x}</span>", unsafe_allow_html=True)

# ---------------------------------------------------------------------------- actions
with tabs[3]:
    if not a:
        st.info("Recommendations need the AI step.")
    else:
        st.subheader("Troubleshooting steps (in order)")
        for i, s in enumerate(a.next_steps, 1):
            st.checkbox(f"**{i}. {s.action}**: {s.rationale}", key=f"step-{st.session_state.current}-{i}")
        esc = a.escalation
        box = st.error if esc.required else st.info
        box(f"**Escalation: {'required now' if esc.required else 'not required yet'}** → {esc.to_team}\n\n"
            f"**Trigger:** {esc.trigger_condition}")

# ---------------------------------------------------------------------------- summary
with tabs[4]:
    md = incident_markdown(r, source_name)
    st.markdown(md)
    st.divider()
    d1, d2 = st.columns(2)
    d1.download_button("⬇️ Download incident report (.md)", md, file_name=f"incident_{Path(source_name).stem}.md")
    if a:
        d2.download_button("⬇️ Download analysis (.json)", json.dumps(
            {"analysis": a.model_dump(), "validation": asdict(v)}, indent=2, default=str),
            file_name=f"incident_{Path(source_name).stem}.json")
    with st.expander("Copy as plain text"):
        st.code(md, language="markdown")

# ---------------------------------------------------------------------------- transparency
with tabs[5]:
    st.markdown(f"**Model:** `{r.model}` · **AI time:** {r.llm_duration_s:.1f}s · **attempts:** {r.attempts} · "
                f"**tokens in/out:** {r.prompt_tokens}/{r.output_tokens} · **lines shown to model:** "
                f"{len(r.shown_lines)}/{r.parsed.line_count} · **formats:** {r.parsed.format_counts}")
    if v and v.notes:
        st.markdown("**Validator notes**")
        for n in v.notes:
            st.markdown(f"- {n}")
    with st.expander("Prompt sent to the model"):
        st.code(r.prompt, language="markdown")
    with st.expander("Raw model output (JSON)"):
        st.code(r.raw_response or "(none)", language="json")
