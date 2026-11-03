"""Design system: colors, CSS refinements and small HTML building blocks."""
from datetime import datetime
from html import escape

import streamlit as st

C = {
    "bg": "#F7F8FA", "surface": "#FFFFFF", "text": "#172033", "muted": "#64748B", "accent": "#4F46E5",
    "success": "#16A34A", "warning": "#D97706", "error": "#DC2626", "border": "#E5E7EB", "subtle": "#F1F5F9",
}
# Chart fills (validated: no normal-vision collisions; always shown with labels + legend)
CHART = {"APPROVE": "#16A34A", "NEEDS_REVIEW": "#F59E0B", "REJECT": "#DC2626", "accent": "#4F46E5"}

DECISION_LABEL = {"APPROVE": "Approve", "REJECT": "Reject", "NEEDS_REVIEW": "Needs review"}
DECISION_TONE = {"APPROVE": "success", "REJECT": "error", "NEEDS_REVIEW": "warning"}

DOC_STATUS = {  # status -> (label, tone)
    "queued": ("Queued", "neutral"), "running": ("Processing", "accent"), "completed": ("Completed", "success"),
    "waiting_review": ("Waiting for review", "warning"), "reviewing": ("Review in progress", "accent"),
    "reviewed": ("Reviewed", "success"), "failed": ("Failed", "error"), "cancelled": ("Cancelled", "neutral"),
    "interrupted": ("Interrupted", "neutral"),
}
STAGE_ICON = {  # stage status -> (glyph, css class, label)
    "pending": ("○", "pending", "Pending"), "running": ("●", "running", "Running"),
    "completed": ("✓", "completed", "Completed"), "failed": ("✕", "failed", "Failed"),
    "waiting": ("Ⅱ", "waiting", "Waiting for human review"), "skipped": ("–", "skipped", "Skipped"),
}
OUTCOME_TONE = {"PASS": "success", "FAIL": "error", "UNKNOWN": "warning", "N/A": "neutral"}
# How a document was read (checker.graph.analysis_kind) -> (label, tone)
ANALYSIS = {
    "live": ("Live AI Analysis", "success"),
    "regex": ("AI unavailable — Regex-only analysis", "warning"),
    "replay": ("Recorded AI Response — Not Live", "warning"),
    "unknown": ("Not analysed yet", "neutral"),
}

CSS = f"""
<style>
/* Top padding must clear Streamlit's fixed header bar (about 60px on Streamlit Cloud, with the
   Share / GitHub toolbar), or it covers the page title. Pixels, not rem: baseFontSize is 14px. */
.block-container, [data-testid="stMainBlockContainer"] {{
    padding-top: 96px !important; padding-bottom: 3rem; max-width: 1320px; }}
h1 {{ font-size: 1.5rem !important; font-weight: 600 !important; letter-spacing: -0.01em; }}
h2 {{ font-size: 1.15rem !important; font-weight: 600 !important; }}
h3 {{ font-size: 1rem !important; font-weight: 600 !important; }}
[data-testid="stMetric"] {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 8px;
    padding: 14px 16px; box-shadow: 0 1px 2px rgba(16,24,40,.04); }}
[data-testid="stMetricLabel"] p {{ color: {C['muted']}; font-size: .78rem; font-weight: 500; text-transform: none; }}
[data-testid="stMetricValue"] {{ font-size: 1.5rem; font-weight: 600; }}
[data-testid="stExpander"] details {{ background: {C['surface']}; }}
[data-testid="stSidebar"] hr {{ margin: .75rem 0; }}
[data-testid="stDecoration"] {{ display: none; }}

.si-header {{ display:flex; justify-content:space-between; align-items:flex-end; margin-bottom: .25rem; }}
.si-title {{ font-size: 1.5rem; font-weight: 600; color: {C['text']}; margin: 0; letter-spacing: -0.01em; }}
.si-sub {{ color: {C['muted']}; font-size: .9rem; margin: .2rem 0 1.1rem 0; }}
.si-section {{ font-size: .8rem; font-weight: 600; color: {C['muted']}; text-transform: uppercase;
    letter-spacing: .04em; margin: 1.4rem 0 .5rem 0; }}

.si-badge {{ display:inline-flex; align-items:center; gap:6px; padding: 2px 8px; border-radius: 999px;
    font-size: .74rem; font-weight: 500; line-height: 1.5; border: 1px solid transparent; white-space: nowrap; }}
.si-badge::before {{ content:""; width:6px; height:6px; border-radius:50%; background: currentColor; }}
.si-success {{ color: {C['success']}; background: #F0FDF4; border-color: #BBF7D0; }}
.si-warning {{ color: {C['warning']}; background: #FFFBEB; border-color: #FDE68A; }}
.si-error   {{ color: {C['error']};   background: #FEF2F2; border-color: #FECACA; }}
.si-accent  {{ color: {C['accent']};  background: #EEF2FF; border-color: #C7D2FE; }}
.si-neutral {{ color: {C['muted']};   background: {C['subtle']}; border-color: {C['border']}; }}

.si-card {{ background: {C['surface']}; border: 1px solid {C['border']}; border-radius: 8px; padding: 12px 14px;
    margin-bottom: 8px; box-shadow: 0 1px 2px rgba(16,24,40,.04); }}
.si-card.active {{ border-color: {C['accent']}; box-shadow: 0 0 0 3px #EEF2FF; }}
.si-card-row {{ display:flex; justify-content:space-between; align-items:center; gap: 10px; }}
.si-doc {{ font-weight: 600; font-size: .88rem; color: {C['text']}; }}
.si-meta {{ color: {C['muted']}; font-size: .78rem; }}
.si-track {{ display:flex; gap:3px; margin: 9px 0 6px 0; }}
.si-seg {{ flex:1; height:5px; border-radius: 3px; background: {C['border']}; }}
.si-seg.completed {{ background: {C['success']}; }}
.si-seg.running {{ background: {C['accent']}; animation: si-pulse 1.2s ease-in-out infinite; }}
.si-seg.failed {{ background: {C['error']}; }}
.si-seg.waiting {{ background: {C['warning']}; }}
.si-seg.skipped {{ background: #CBD5E1; }}
@keyframes si-pulse {{ 0%,100% {{ opacity: 1; }} 50% {{ opacity: .35; }} }}

.si-steps {{ margin: 4px 0 0 0; padding: 0; list-style: none; }}
.si-step {{ display:grid; grid-template-columns: 22px 1fr auto; gap: 8px; padding: 6px 0;
    border-bottom: 1px solid {C['subtle']}; font-size: .84rem; align-items: start; }}
.si-step:last-child {{ border-bottom: none; }}
.si-ico {{ font-weight: 700; text-align:center; }}
.si-ico.completed {{ color: {C['success']}; }} .si-ico.failed {{ color: {C['error']}; }}
.si-ico.running {{ color: {C['accent']}; animation: si-pulse 1.2s ease-in-out infinite; }}
.si-ico.waiting {{ color: {C['warning']}; }} .si-ico.pending, .si-ico.skipped {{ color: #94A3B8; }}
.si-step-name {{ color: {C['text']}; font-weight: 500; }}
.si-step-msg {{ color: {C['muted']}; font-size: .78rem; }}
.si-step-warn {{ color: {C['warning']}; font-size: .78rem; }}
.si-step-time {{ color: {C['muted']}; font-size: .76rem; font-variant-numeric: tabular-nums; white-space: nowrap; }}

.si-log {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: .76rem; line-height: 1.6; }}
.si-log-row {{ display:grid; grid-template-columns: 64px 1fr; gap: 8px; border-bottom: 1px solid {C['subtle']}; padding: 2px 0; }}
.si-log-ts {{ color: #94A3B8; }}
.si-log-failed {{ color: {C['error']}; }} .si-log-waiting {{ color: {C['warning']}; }}
.si-log-batch {{ color: {C['accent']}; font-weight: 600; }}

.si-kv {{ display:grid; grid-template-columns: 150px 1fr; gap: 6px 12px; font-size: .86rem; }}
.si-kv .k {{ color: {C['muted']}; }} .si-kv .v {{ color: {C['text']}; font-weight: 500; overflow-wrap: anywhere; }}
.si-quote {{ border-left: 3px solid {C['accent']}; background: #F8FAFF; padding: 6px 10px; margin: 4px 0 8px 0;
    font-size: .83rem; color: {C['text']}; border-radius: 0 4px 4px 0; font-family: ui-monospace, Consolas, monospace; }}
.si-rule {{ border: 1px solid {C['border']}; border-radius: 8px; padding: 10px 12px; margin-bottom: 8px; background: {C['surface']}; }}
.si-empty {{ border: 1px dashed #CBD5E1; border-radius: 8px; padding: 28px; text-align:center; color: {C['muted']};
    background: {C['surface']}; }}
.si-empty b {{ color: {C['text']}; display:block; margin-bottom: 4px; font-size: .95rem; }}
.si-progress-label {{ display:flex; justify-content:space-between; font-size: .84rem; color: {C['muted']}; margin-bottom: 4px; }}
.si-foot {{ font-size: .78rem; color: {C['muted']}; line-height: 1.7; }}
.si-foot b {{ color: {C['text']}; font-weight: 500; }}
</style>
"""


def inject_css() -> None:
    st.html(CSS)


# ---------------------------------------------------------------- html helpers
def badge(text: str, tone: str = "neutral") -> str:
    return f'<span class="si-badge si-{tone}">{escape(text)}</span>'


def decision_badge(decision: str | None) -> str:
    if not decision:
        return badge("Pending", "neutral")
    return badge(DECISION_LABEL.get(decision, decision), DECISION_TONE.get(decision, "neutral"))


def status_badge(status: str | None) -> str:
    label, tone = DOC_STATUS.get(status or "", (status or "-", "neutral"))
    return badge(label, tone)


def analysis_badge(source: str | None) -> str:
    from checker.graph import analysis_kind

    label, tone = ANALYSIS[analysis_kind(source)]
    return badge(label, tone)


def outcome_badge(outcome: str) -> str:
    return badge(outcome, OUTCOME_TONE.get(outcome, "neutral"))


def page_header(title: str, subtitle: str) -> None:
    st.html(f'<div class="si-title">{escape(title)}</div><div class="si-sub">{escape(subtitle)}</div>')


def section(title: str) -> None:
    st.html(f'<div class="si-section">{escape(title)}</div>')


def empty_state(title: str, body: str) -> None:
    st.html(f'<div class="si-empty"><b>{escape(title)}</b>{escape(body)}</div>')


def kv(rows: list[tuple[str, str]]) -> str:
    """Key/value grid. Values are escaped; pass pre-built HTML with ('k', ('html', '<...>'))."""
    out = []
    for k, v in rows:
        val = v[1] if isinstance(v, tuple) and v[0] == "html" else escape(str(v if v not in (None, "") else "-"))
        out.append(f'<div class="k">{escape(k)}</div><div class="v">{val}</div>')
    return f'<div class="si-kv">{"".join(out)}</div>'


def quote(text: str) -> str:
    return f'<div class="si-quote">{escape(text)}</div>'


# ---------------------------------------------------------------- formatting
def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "-"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.1f} MB"


def fmt_seconds(s: float | None) -> str:
    if s is None:
        return "-"
    if s < 1:
        return f"{s * 1000:.0f} ms"
    if s < 60:
        return f"{s:.1f} s"
    return f"{int(s // 60)} min {int(s % 60)} s"


def fmt_ms(ms: float | None) -> str:
    return "" if ms is None else fmt_seconds(ms / 1000)


def fmt_dt(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y, %H:%M:%S")
    except ValueError:
        return iso


def fmt_ago(iso: str | None) -> str:
    if not iso:
        return "-"
    secs = (datetime.now() - datetime.fromisoformat(iso)).total_seconds()
    if secs < 60:
        return f"{int(secs)} s"
    if secs < 3600:
        return f"{int(secs // 60)} min"
    if secs < 86400:
        return f"{int(secs // 3600)} h {int(secs % 3600 // 60)} min"
    return f"{int(secs // 86400)} d"


SOURCE_LABEL = {"llm": "LLM", "regex": "REGEX", "llm+regex": "LLM + REGEX"}


def explain_error(msg: str | None) -> str | None:
    """Actionable wording for the errors the backend actually produces."""
    if not msg:
        return None
    m = msg.lower()
    if "could not open pdf" in m:
        return "The file is not a readable PDF (it may be corrupted or not a PDF). A person must decide."
    if "no readable text" in m:
        return "The PDF has no text layer (it may be a scanned image). OCR is not supported, so a person must decide."
    if "llm extraction unavailable" in m and "openai_api_key is not set" in m:
        return ("No OpenAI API key is configured. The document was analysed with the offline (regex) "
                "extractor only and sent to a person.")
    if "llm extraction unavailable" in m and "no saved llm answer" in m:
        return ("Replay (testing) mode has no recorded AI response for this document, and recorded responses of "
                "other documents are never reused. It was analysed with the regex extractor only and sent to a person.")
    if "llm extraction unavailable" in m:
        detail = msg.split(":", 1)[-1].strip()
        return (f"AI extraction failed ({detail[:160]}). The document was analysed with the offline (regex) "
                "extractor only and sent to a person.")
    if "does not look like a supplier quotation" in m:
        return "No item line, quantity or price was found. This may not be a supplier quotation; a person must decide."
    if "step failed" in m:
        return f"A pipeline step failed: {msg}. The remaining checks were skipped and a person must decide."
    return msg
